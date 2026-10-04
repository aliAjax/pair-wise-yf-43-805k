from datetime import date, datetime

from .domain import (
    CalibrationOverlapError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def today_iso(clock=None):
    if clock is None:
        return date.today().isoformat()
    if isinstance(clock, str):
        return clock[:10]
    value = clock.now() if callable(getattr(clock, "now", None)) else clock()
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _as_date(value):
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def calibration_current(due_at, as_of):
    return str(due_at)[:10] >= str(as_of)[:10]


def calibration_window(calibration):
    """Return [start, end] dates of a calibration record, or None when not effective."""
    if calibration.get("status") != "approved":
        return None
    data = calibration.get("data", {})
    start_raw = data.get("approved_at") or data.get("performed_at")
    end_raw = data.get("due_at")
    if not start_raw or not end_raw:
        return None
    start, end = _as_date(start_raw), _as_date(end_raw)
    if end < start:
        return None
    return start, end


def intervals_overlap(first, second):
    """Inclusive-day overlap of two [start, end] windows."""
    return not (first[1] < second[0] or second[1] < first[0])


def covering_calibration(lookup, instrument_id, on_date):
    """The single approved calibration of an instrument that covers on_date.

    Multiple overlapping approved windows are a data conflict callers must
    surface instead of silently picking one.
    """
    if not instrument_id or not on_date:
        return None
    target = _as_date(on_date)
    matches = []
    for candidate in lookup("calibration", "instrument_id", instrument_id) or []:
        window = calibration_window(candidate)
        if window and window[0] <= target <= window[1]:
            matches.append(candidate)
    if len(matches) > 1:
        latest = max(matches, key=lambda item: item["version"])
        return latest
    return matches[0] if matches else None


def overlaps_existing(lookup, instrument_id, window, exclude_id=None):
    for candidate in lookup("calibration", "instrument_id", instrument_id) or []:
        if candidate.get("id") == exclude_id:
            continue
        other = calibration_window(candidate)
        if other and intervals_overlap(window, other):
            return candidate
    return None


def evaluate_result_chain(entity, lookup, as_of=None):
    """Assess the validity chain of a result.

    Returns (is_current, reasons, context). A released result is current only
    when its instrument is active, a covering approved calibration exists at
    release time *and* is still unexpired (not superseded), and the bound
    method version is still validated.
    """
    as_of = as_of or today_iso()
    data = entity.get("data", {})
    reasons = []
    context = {}

    instrument_id = data.get("instrument_id")
    method_id = data.get("method_id")
    released_at = data.get("released_at") or as_of
    instrument = _find_one(lookup, "instrument", "id", instrument_id)
    if not instrument:
        reasons.append("instrument_missing")
    else:
        context["instrument_status"] = instrument["status"]
        if instrument["status"] != "active":
            reasons.append("instrument_inactive")

    method = _find_one(lookup, "method", "id", method_id) if method_id else None
    if not method_id:
        reasons.append("method_unbound")
    elif not method:
        reasons.append("method_missing")
    else:
        context["method_status"] = method["status"]
        context["method_version"] = method["data"].get("version")
        if method["status"] != "validated":
            reasons.append("method_revoked")
        elif instrument_id not in method["data"].get("instrument_ids", []):
            reasons.append("method_not_for_instrument")

    bound_calibration_id = data.get("calibration_id")
    calibration = (
        _find_one(lookup, "calibration", "id", bound_calibration_id)
        if bound_calibration_id
        else None
    )
    if not bound_calibration_id:
        # Results recorded before the chain was introduced: derive the
        # covering record rather than forcing an immediate re-review.
        calibration = covering_calibration(lookup, instrument_id, released_at)
        bound_calibration_id = calibration["id"] if calibration else None
    if not calibration:
        reasons.append("calibration_missing")
    else:
        window = calibration_window(calibration)
        context["calibration_id"] = calibration["id"]
        context["calibration_status"] = calibration["status"]
        context["calibration_version"] = calibration["version"]
        if window is None:
            reasons.append("calibration_not_approved")
        else:
            start, end = window
            context["calibration_start"] = start.isoformat()
            context["calibration_due"] = end.isoformat()
            release_day = _as_date(released_at)
            if not (start <= release_day <= end):
                reasons.append("calibration_expired_at_release")
            elif end < _as_date(as_of):
                reasons.append("calibration_expired")
    return not reasons, reasons, context


def recompute_result(entity, lookup):
    """Recompute a result value from the current method configuration.

    Demonstration model: method parameters may carry a numeric correction
    factor applied to the raw measurement. When the method is revoked or no
    longer applies, recomputation is not possible and None is returned.
    """
    data = entity.get("data", {})
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    raw = data.get("measurement_raw")
    if raw is None:
        raw = data.get("value")
    try:
        raw_value = float(raw)
    except (TypeError, ValueError):
        return None, {"recompute_note": "raw measurement is not numeric"}
    factor = 1.0
    note = "recomputed_without_factor"
    if method and method["status"] == "validated":
        parameters = method["data"].get("parameters") or {}
        candidate = parameters.get("correction_factor")
        if candidate is not None:
            try:
                factor = float(candidate)
                note = "recomputed_with_current_method"
            except (TypeError, ValueError):
                note = "method_factor_invalid"
    elif method and method["status"] != "validated":
        return None, {"recompute_note": "method_is_%s" % method["status"]}
    else:
        return None, {"recompute_note": "method_missing"}
    return round(raw_value * factor, 6), {"recompute_note": note, "recompute_factor": factor}


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")
    requested_at = data.get("requested_at")
    if requested_at:
        _as_date(requested_at)


def _validate_perform(actor, entity, data, lookup, as_of=None):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")
    performed_at = data.get("performed_at")
    due_at = data.get("due_at")
    if performed_at:
        _as_date(performed_at)
    if data.get("result") == "passed" and _as_date(due_at) < _as_date(performed_at):
        raise ValidationError("due_at must not be earlier than performed_at")


def _validate_approve(actor, entity, data, lookup, as_of=None):
    instrument_id = entity["data"].get("instrument_id")
    start_raw = data.get("approved_at") or entity["data"].get("performed_at")
    due_at = data.get("due_at") or entity["data"].get("due_at")
    if not data.get("authorized_by"):
        raise ValidationError("approval requires authorized_by")
    if not start_raw or not due_at:
        raise ValidationError("approval requires an effective interval")
    window = (_as_date(start_raw), _as_date(due_at))
    if window[1] < window[0]:
        raise ValidationError("due_at must not be earlier than performed_at")
    existing = overlaps_existing(lookup, instrument_id, window, exclude_id=entity["id"])
    if existing:
        existing_window = calibration_window(existing)
        raise CalibrationOverlapError(
            "calibration %s overlaps approved calibration %s for instrument %s; "
            "compare and resolve before approving"
            % (entity["id"], existing["id"], instrument_id),
            instrument_id=instrument_id,
            incoming_calibration_id=entity["id"],
            existing_calibration_id=existing["id"],
            intervals={
                "incoming": [window[0].isoformat(), window[1].isoformat()],
                "existing": [existing_window[0].isoformat(), existing_window[1].isoformat()]
                if existing_window
                else None,
            },
        )
    patch = {"approved_at": data.get("approved_at") or today_iso(as_of)}
    if data.get("due_at"):
        patch["due_at"] = data["due_at"]
    return patch


def _validate_amend_due(actor, entity, data, lookup, as_of=None):
    new_due = data.get("due_at")
    if not new_due:
        raise ValidationError("amend_due requires due_at")
    if not data.get("reason"):
        raise ValidationError("amend_due requires reason")
    window = calibration_window(entity)
    if window is None:
        raise ValidationError("only an approved calibration with an interval can be amended")
    new_window = (window[0], _as_date(new_due))
    existing = overlaps_existing(
        lookup, entity["data"].get("instrument_id"), new_window, exclude_id=entity["id"]
    )
    if existing:
        existing_window = calibration_window(existing)
        raise CalibrationOverlapError(
            "amended calibration %s would overlap approved calibration %s"
            % (entity["id"], existing["id"]),
            instrument_id=entity["data"].get("instrument_id"),
            incoming_calibration_id=entity["id"],
            existing_calibration_id=existing["id"],
            intervals={
                "incoming": [new_window[0].isoformat(), new_window[1].isoformat()],
                "existing": [existing_window[0].isoformat(), existing_window[1].isoformat()],
            },
        )
    return {"due_at": new_due}


def _validate_result_release(actor, entity, data, lookup, as_of=None):
    as_of = as_of or today_iso()
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    released_at = data.get("released_at") or as_of
    calibration = covering_calibration(lookup, instrument["id"], released_at)
    if not calibration:
        raise ValidationError("no approved calibration covers the release date")
    window = calibration_window(calibration)
    if window[1] < _as_date(as_of):
        raise ValidationError("instrument calibration is not current")
    return {
        "released_by": actor.user_id,
        "released_at": released_at,
        "calibration_id": calibration["id"],
        "calibration_version": calibration["version"],
        "calibration_start": window[0].isoformat(),
        "calibration_due": window[1].isoformat(),
        "method_version": method["data"].get("version"),
        "measurement_raw": data.get("value", entity.get("data", {}).get("measurement")),
    }


CUSTOM_CREATE = {"calibration": _validate_calibration}
CUSTOM_TRANSITIONS = {
    ("calibration", "perform"): _validate_perform,
    ("calibration", "approve"): _validate_approve,
    ("calibration", "amend_due"): _validate_amend_due,
    ("result", "release"): _validate_result_release,
}


class RuleEngine:
    ALIASES = {
        "instruments": "instrument",
        "calibrations": "calibration",
        "methods": "method",
        "results": "result",
    }
    INITIAL_STATUS = {
        "instrument": "active",
        "calibration": "requested",
        "method": "draft",
        "result": "pending",
    }
    TRANSITIONS = {
        "instrument": {
            "send_calibration": (("active",), "calibrating"),
            "calibrate": (("calibrating",), "active"),
            "quarantine": (("active",), "quarantined"),
            "restore": (("quarantined",), "active"),
        },
        "calibration": {
            "perform": (("requested", "failed"), "passed"),
            "approve": (("passed",), "approved"),
            "reject": (("failed",), "rejected"),
            "amend_due": (("approved",), "approved"),
        },
        "method": {
            "validate_method": (("draft",), "validated"),
            "revoke_method": (("validated",), "revoked"),
        },
        "result": {
            "release": (("pending",), "released"),
            "block": (("pending", "review_pending"), "blocked"),
            "reanalyze": (("blocked", "review_pending"), "pending"),
            "keep_result": (("review_pending",), "released"),
            "requeue_result": (("review_pending",), "blocked"),
        },
    }
    CREATE_REQUIRED = {
        "instrument": ("name", "serial"),
        "calibration": ("instrument_id", "requested_at"),
        "method": ("name", "version"),
        "result": ("sample_id", "measurement"),
    }
    ACTION_REQUIRED = {
        ("instrument", "calibrate"): ("due_at", "passed"),
        ("instrument", "quarantine"): ("reason",),
        ("calibration", "perform"): ("result", "performed_at", "uncertainty"),
        ("calibration", "approve"): ("authorized_by",),
        ("calibration", "reject"): ("reason",),
        ("calibration", "amend_due"): ("due_at", "reason"),
        ("method", "validate_method"): ("parameters", "instrument_ids"),
        ("method", "revoke_method"): ("reason",),
        ("result", "release"): ("instrument_id", "method_id", "value", "unit"),
        ("result", "block"): ("reason",),
        ("result", "reanalyze"): ("reason",),
        ("result", "keep_result"): ("reason",),
        ("result", "requeue_result"): ("reason",),
    }
    CREATE_ROLES = {
        "instrument": ("admin", "technician"),
        "calibration": ("admin", "metrology"),
        "method": ("admin", "authorizer"),
        "result": ("admin", "analyst"),
    }
    ROLE_ACTIONS = {
        "send_calibration": ("admin", "technician"),
        "calibrate": ("admin", "metrology"),
        "quarantine": ("admin", "metrology"),
        "restore": ("admin", "metrology"),
        "perform": ("admin", "metrology"),
        "approve": ("admin", "authorizer"),
        "reject": ("admin", "authorizer"),
        "amend_due": ("admin", "metrology"),
        "validate_method": ("admin", "authorizer"),
        "revoke_method": ("admin", "authorizer"),
        "release": ("admin", "analyst"),
        "block": ("admin", "analyst"),
        "reanalyze": ("admin", "analyst"),
        "keep_result": ("admin", "authorizer"),
        "requeue_result": ("admin", "authorizer"),
    }

    def __init__(self, clock=None):
        self.clock = clock

    def now_date(self):
        return today_iso(self.clock)

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = (
            custom(actor, entity, data, lookup, self.now_date()) if custom else {}
        )
        patch = dict(data)
        if extra:
            patch.update(extra)
        resolved_status = next_status
        if next_status is None:
            resolved_status = entity["status"]
        return resolved_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None or value is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None
