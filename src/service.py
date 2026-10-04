from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    Actor,
    CalibrationOverlapError,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDenied,
)
from .rules import (
    RuleEngine,
    evaluate_result_chain,
    recompute_result,
)

SYSTEM_ACTOR = Actor("system", "system")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    # ------------------------------------------------------------------ basics

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _tx_lookup(self, tx):
        def lookup(kind, field, value):
            return tx.find_entities(self.rules.normalize_kind(kind), field, value)

        return lookup

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------- chain evaluation

    def evaluate_result(self, entity_id):
        entity = self.get(entity_id)
        current, reasons, context = evaluate_result_chain(
            entity, self._lookup, self.rules.now_date()
        )
        return {
            "id": entity_id,
            "status": entity["status"],
            "current": current,
            "reasons": reasons,
            "context": context,
        }

    def _affected_results(self, tx, action, entity):
        lookup = self._tx_lookup(tx)
        results = []
        if entity["kind"] == "calibration":
            instrument_id = entity["data"].get("instrument_id")
            for result in tx.list_entities(kind="result", status="released"):
                if result["data"].get("instrument_id") == instrument_id:
                    results.append(result)
        elif entity["kind"] == "method":
            method_id = entity["id"]
            for result in tx.list_entities(kind="result", status="released"):
                if result["data"].get("method_id") == method_id:
                    results.append(result)
        elif entity["kind"] == "instrument":
            for result in tx.list_entities(kind="result", status="released"):
                if result["data"].get("instrument_id") == entity["id"]:
                    results.append(result)
        return results, lookup

    def _sweep_results(self, tx, lookup, as_of, triggered_by=None):
        """Re-evaluate released results; recalc + re-queue broken ones."""
        flagged, recalculated = [], 0
        for result in tx.list_entities(kind="result", status="released"):
            current, reasons, context = evaluate_result_chain(result, lookup, as_of)
            if current:
                continue
            recomputed, recompute_meta = recompute_result(result, lookup)
            data = dict(result["data"])
            # Original released value is preserved, never overwritten.
            data.setdefault(
                "original_release",
                {
                    "value": data.get("value"),
                    "unit": data.get("unit"),
                    "released_at": data.get("released_at"),
                    "released_by": data.get("released_by"),
                    "calibration_id": data.get("calibration_id"),
                    "calibration_version": data.get("calibration_version"),
                    "method_version": data.get("method_version"),
                },
            )
            data["value"] = recomputed
            data["recalculated_value"] = recomputed
            data["recalculated_at"] = as_of
            data["recalculation"] = recompute_meta
            data["invalid_reasons"] = reasons
            data["chain_context"] = context
            data["invalidated_by"] = triggered_by
            updated = tx.update_entity(
                result["id"], result["version"], "review_pending", data
            )
            reason_key = "chain:" + "+".join(sorted(reasons))
            if not tx.find_open_review("result", result["id"]):
                item = tx.create_review_item(
                    {
                        "id": "rev-" + str(uuid4()),
                        "kind": "result_chain",
                        "ref_type": "result",
                        "ref_id": result["id"],
                        "reason": "校准/方法时效链失效，结果已重算，待人工复核",
                        "reason_key": reason_key,
                        "detail": {
                            "reasons": reasons,
                            "context": context,
                            "original_value": data["original_release"]["value"],
                            "recalculated_value": recomputed,
                            "triggered_by": triggered_by,
                        },
                        "created_by": "system",
                    }
                )
                tx.insert_audit(
                    result["id"],
                    "system",
                    "system",
                    "chain_invalidated",
                    "released",
                    "review_pending",
                    {
                        "reasons": reasons,
                        "review_id": item["id"],
                        "triggered_by": triggered_by,
                        "original_value": data["original_release"]["value"],
                        "recalculated_value": recomputed,
                    },
                )
            flagged.append(updated)
            recalculated += 1
        return {"flagged": len(flagged), "recalculated": recalculated}

    # ------------------------------------------------------------- transitions

    def _enqueue_for_retry(self, tx, actor, entity_id, action, data, error,
                           review_id=None):
        pending = tx.find_open_pending(entity_id, action)
        if not pending:
            pending = tx.enqueue_pending(
                {
                    "id": "pend-" + str(uuid4()),
                    "entity_id": entity_id,
                    "action": action,
                    "data": dict(data or {}),
                    "requested_by": actor.user_id,
                    "requested_role": actor.role,
                }
            )
        return {
            "queued": "pending_retry",
            "pending_id": pending["id"],
            "review_id": review_id,
            "error": error,
            "entity": tx.get_entity(entity_id),
        }

    def _apply_transition(self, tx, actor, entity_id, action, data,
                          expected_version=None, enqueue_on_overlap=True):
        """Validate + apply one transition inside an open transaction.

        A stale expected_version is a late commit: it is rebased against the
        newest version. If it no longer applies, it is persisted as a pending
        change and retried later instead of surfacing a hard conflict.
        Overlap conflicts are always held for explicit human comparison.
        """
        lookup = self._tx_lookup(tx)
        entity = tx.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        stale = (
            expected_version is not None
            and int(expected_version) != entity["version"]
        )

        def validate_against(latest):
            next_status, patch = self.rules.validate_transition(
                actor, latest, action, dict(data or {}), lookup
            )
            merged = dict(latest["data"])
            merged.update(patch)
            return next_status, patch, merged

        try:
            next_status, patch, merged = validate_against(entity)
            write_version = entity["version"] if stale else expected_version
            updated = tx.update_entity(entity_id, write_version, next_status, merged)
        except CalibrationOverlapError as exc:
            if enqueue_on_overlap:
                return self._hold_overlap(tx, actor, exc, data)
            raise
        except DomainError as exc:
            if stale:
                descriptor = self._enqueue_for_retry(
                    tx, actor, entity_id, action, data, str(exc)
                )
                tx.insert_audit(
                    entity_id,
                    actor.user_id,
                    actor.role,
                    "late_commit_deferred",
                    entity["status"],
                    entity["status"],
                    {
                        "expected_version": expected_version,
                        "current_version": entity["version"],
                        "action": action,
                        "pending_id": descriptor["pending_id"],
                        "error": str(exc),
                    },
                )
                return entity, descriptor
            raise

        tx.insert_audit(
            entity_id,
            actor.user_id,
            actor.role,
            action,
            entity["status"],
            updated["status"],
            {
                "patch": patch,
                "rebased": stale,
                "expected_version": expected_version if stale else None,
            },
        )
        return updated, None

    def _hold_overlap(self, tx, actor, exc, data):
        incoming_id = exc.incoming_calibration_id
        existing_id = exc.existing_calibration_id
        reason_key = "overlap:%s:%s" % (incoming_id, existing_id)
        item = tx.find_open_review("calibration", incoming_id)
        if not item:
            item = tx.create_review_item(
                {
                    "id": "rev-" + str(uuid4()),
                    "kind": "calibration_overlap",
                    "ref_type": "calibration",
                    "ref_id": incoming_id,
                    "reason": "同一仪器两条校准记录时间重叠，必须比对后裁决，禁止覆盖",
                    "reason_key": reason_key,
                    "detail": {
                        "instrument_id": exc.instrument_id,
                        "incoming_calibration_id": incoming_id,
                        "existing_calibration_id": existing_id,
                        "intervals": exc.intervals,
                        "submitted_by": actor.user_id,
                    },
                    "created_by": actor.user_id,
                }
            )
            tx.insert_audit(
                incoming_id,
                actor.user_id,
                actor.role,
                "calibration_overlap_held",
                "passed",
                "passed",
                {
                    "review_id": item["id"],
                    "existing_calibration_id": existing_id,
                    "intervals": exc.intervals,
                },
            )
        # Keep the late approval request around so it is retried once the
        # reviewer removes the obstruction.
        pending = tx.find_open_pending(incoming_id, "approve")
        if not pending:
            pending = tx.enqueue_pending(
                {
                    "id": "pend-" + str(uuid4()),
                    "entity_id": incoming_id,
                    "action": "approve",
                    "data": dict(data or {}),
                    "requested_by": actor.user_id,
                    "requested_role": actor.role,
                }
            )
        held = tx.get_entity(incoming_id)
        return held, {
            "queued": "overlap_review",
            "review_id": item["id"],
            "pending_id": pending["id"],
            "entity": held,
        }

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        with self.repository.transaction() as tx:
            updated, held = self._apply_transition(
                tx, actor, entity_id, action, data, expected_version
            )
            if held:
                # Nothing changed; return the durable queue descriptor.
                return {"status": held.get("queued", "held"), **held}
            sweep_info, _ = self._post_transition_sweep(tx, updated, actor)
            result = dict(updated)
            if sweep_info["flagged"]:
                result["chain_review"] = sweep_info
        # Try queued late commits against the freshly committed state.
        drain = self.drain_pending()
        if drain.get("applied") or drain.get("failed") or drain.get("held"):
            result["pending_drain"] = drain
        return result

    def _post_transition_sweep(self, tx, updated, actor):
        results, lookup = self._affected_results(tx, None, updated)
        if not results:
            return {"flagged": 0, "recalculated": 0}, lookup
        info = self._sweep_results(
            tx,
            lookup,
            self.rules.now_date(),
            triggered_by={
                "kind": updated["kind"],
                "id": updated["id"],
                "status": updated["status"],
                "by": actor.user_id,
            },
        )
        return info, lookup

    # ---------------------------------------------------- durable late commits

    def submit_pending(self, actor, entity_id, action, data=None):
        change_id = "pend-" + str(uuid4())
        with self.repository.transaction() as tx:
            change = tx.enqueue_pending(
                {
                    "id": change_id,
                    "entity_id": entity_id,
                    "action": action,
                    "data": dict(data or {}),
                    "requested_by": actor.user_id,
                    "requested_role": actor.role,
                }
            )
            tx.insert_audit(
                entity_id,
                actor.user_id,
                actor.role,
                "pending_enqueued",
                None,
                None,
                {"pending_id": change_id, "action": action},
            )
        applied = self.drain_pending()
        return {"pending_id": change_id, "status": change["status"], "drain": applied}

    def _retry_one(self, tx, change):
        actor = Actor(change["requested_by"], change["requested_role"])
        try:
            updated, held = self._apply_transition(
                tx,
                actor,
                change["entity_id"],
                change["action"],
                change["data"],
                enqueue_on_overlap=True,
            )
        except DomainError as exc:
            # Not applicable yet: keep the change pending for next retry.
            tx.mark_pending(change["id"], "pending", str(exc))
            return "failed", str(exc)
        if held:
            tx.mark_pending(
                change["id"],
                "pending",
                "awaiting overlap review " + str(held.get("review_id")),
            )
            return "held", held
        tx.mark_pending(change["id"], "applied")
        tx.insert_audit(
            change["entity_id"],
            actor.user_id,
            actor.role,
            "pending_applied",
            None,
            updated["status"],
            {"pending_id": change["id"]},
        )
        self._post_transition_sweep(tx, updated, actor)
        return "applied", None

    def drain_pending(self):
        """Retry every pending late commit against the newest state."""
        summary = {"applied": 0, "failed": 0, "held": 0, "errors": []}
        # Loop until no progress: a retried commit may unblock the next one.
        while True:
            progress = False
            with self.repository.transaction() as tx:
                queued = tx.list_pending(status="pending")
                for change in queued:
                    outcome, info = self._retry_one(tx, change)
                    if outcome == "applied":
                        summary["applied"] += 1
                        progress = True
                        # Commit so the new state is visible, then re-enter.
                        break
                    if outcome == "held":
                        summary["held"] += 1
                        continue
                    summary["failed"] += 1
                    summary["errors"].append({"id": change["id"], "error": info})
            if not progress:
                return summary

    def list_pending(self, status=None):
        return self.repository.list_pending(status=status)

    def cancel_pending(self, actor, pending_id, reason):
        if actor.role not in ("admin", "authorizer"):
            raise PermissionDenied("only admin/authorizer can cancel pending changes")
        if not reason:
            from .domain import ValidationError

            raise ValidationError("cancellation requires a reason")
        with self.repository.transaction() as tx:
            change = tx.cancel_pending(pending_id, reason, actor.user_id)
            tx.insert_audit(
                change["entity_id"],
                actor.user_id,
                actor.role,
                "pending_cancelled",
                None,
                None,
                {"pending_id": pending_id, "reason": reason},
            )
        return change

    # ------------------------------------------------------------- review queue

    def list_reviews(self, status="open", kind=None):
        return self.repository.list_review_items(status=status, kind=kind)

    def get_review(self, review_id):
        item = self.repository.get_review_item(review_id)
        if not item:
            raise NotFoundError("review item not found: " + review_id)
        return item

    def resolve_review(self, actor, review_id, decision, reason):
        if actor.role not in ("admin", "authorizer"):
            raise PermissionDenied("only admin/authorizer can resolve review items")
        if not reason:
            from .domain import ValidationError

            raise ValidationError("resolution requires a reason")
        with self.repository.transaction() as tx:
            item = tx.get_review_item(review_id)
            if not item:
                raise NotFoundError("review item not found: " + review_id)
            if item["status"] != "open":
                raise ConflictError("review item already resolved: " + review_id)
            if item["kind"] == "calibration_overlap":
                self._resolve_overlap(tx, actor, item, decision, reason)
            elif item["kind"] == "result_chain":
                self._resolve_result(tx, actor, item, decision, reason)
            else:
                from .domain import ValidationError

                raise ValidationError("unknown review kind: " + item["kind"])
            resolved = tx.resolve_review_item(review_id, decision, actor.user_id, reason)
        self.drain_pending()
        return resolved

    def _resolve_overlap(self, tx, actor, item, decision, reason):
        incoming = tx.get_entity(item["ref_id"])
        if not incoming:
            raise NotFoundError("calibration no longer exists: " + item["ref_id"])
        detail = item.get("detail", {})
        existing_id = detail.get("existing_calibration_id")
        existing = tx.get_entity(existing_id) if existing_id else None
        if decision == "supersede":
            # Explicit choice: the incoming calibration wins; the older one
            # is marked superseded instead of being deleted/overwritten.
            if incoming["status"] != "passed":
                raise ConflictError(
                    "incoming calibration is %s, nothing to supersede" % incoming["status"]
                )
            data = dict(incoming["data"])
            data["supersedes"] = existing_id
            data["resolution_reason"] = reason
            data["approved_at"] = data.get("approved_at") or self.rules.now_date()
            data["authorized_by"] = data.get("authorized_by") or actor.user_id
            updated = tx.update_entity(
                incoming["id"], incoming["version"], "approved", data
            )
            tx.insert_audit(
                incoming["id"],
                actor.user_id,
                actor.role,
                "overlap_resolved_supersede",
                "passed",
                "approved",
                {"review_id": item["id"], "supersedes": existing_id, "reason": reason},
            )
            if existing and existing["status"] == "approved":
                old_data = dict(existing["data"])
                old_data["superseded_by"] = incoming["id"]
                old_data["superseded_reason"] = reason
                tx.update_entity(existing["id"], existing["version"], "superseded", old_data)
                tx.insert_audit(
                    existing["id"],
                    actor.user_id,
                    actor.role,
                    "calibration_superseded",
                    "approved",
                    "superseded",
                    {"by": incoming["id"], "review_id": item["id"], "reason": reason},
                )
            # The decision itself performs the approval; the queued request
            # is now redundant and must not be retried.
            for change in tx.list_pending(status="pending"):
                if change["entity_id"] == incoming["id"] and change["action"] == "approve":
                    tx.cancel_pending(
                        change["id"], "approved by overlap resolution: " + reason, actor.user_id
                    )
            self._sweep_results(
                tx,
                self._tx_lookup(tx),
                self.rules.now_date(),
                triggered_by={
                    "kind": "calibration",
                    "id": incoming["id"],
                    "resolution": "supersede",
                    "by": actor.user_id,
                },
            )
        elif decision == "reject_new":
            # Explicit choice: keep the existing calibration, reject the new.
            if incoming["status"] != "passed":
                raise ConflictError(
                    "incoming calibration is %s and cannot be rejected here"
                    % incoming["status"]
                )
            data = dict(incoming["data"])
            data["rejection_reason"] = reason
            tx.update_entity(incoming["id"], incoming["version"], "rejected", data)
            tx.insert_audit(
                incoming["id"],
                actor.user_id,
                actor.role,
                "overlap_resolved_reject",
                "passed",
                "rejected",
                {"review_id": item["id"], "kept": existing_id, "reason": reason},
            )
            # Any queued approval of the rejected record can never succeed.
            for change in tx.list_pending(status="pending"):
                if change["entity_id"] == incoming["id"] and change["action"] == "approve":
                    tx.cancel_pending(change["id"], "calibration rejected: " + reason, actor.user_id)
        else:
            from .domain import ValidationError

            raise ValidationError(
                "overlap decision must be 'supersede' or 'reject_new'"
            )

    def _resolve_result(self, tx, actor, item, decision, reason):
        result = tx.get_entity(item["ref_id"])
        if not result:
            raise NotFoundError("result no longer exists: " + item["ref_id"])
        if result["status"] != "review_pending":
            raise ConflictError(
                "result is %s, review is stale" % result["status"]
            )
        data = dict(result["data"])
        data["reviewed_by"] = actor.user_id
        data["review_note"] = reason
        if decision == "keep":
            # Reviewer accepts the recalculated (or original) value and
            # re-releases; the original snapshot stays in the record.
            if data.get("recalculated_value") is not None:
                data["value"] = data["recalculated_value"]
            else:
                data["value"] = data.get("original_release", {}).get("value")
            data["released_by"] = actor.user_id
            data["released_at"] = self.rules.now_date()
            next_status = "released"
            audit_action = "review_keep_released"
        elif decision == "requeue":
            next_status = "blocked"
            audit_action = "review_requeue_blocked"
        else:
            from .domain import ValidationError

            raise ValidationError("result decision must be 'keep' or 'requeue'")
        tx.update_entity(result["id"], result["version"], next_status, data)
        tx.insert_audit(
            result["id"],
            actor.user_id,
            actor.role,
            audit_action,
            "review_pending",
            next_status,
            {
                "review_id": item["id"],
                "reason": reason,
                "original_value": data.get("original_release", {}).get("value"),
            },
        )

    # ------------------------------------------------------------- reconcile

    def reconcile(self, actor=None):
        """Replay queued late commits, then re-audit the whole chain."""
        drain = self.drain_pending()
        with self.repository.transaction() as tx:
            sweep = self._sweep_results(
                tx, self._tx_lookup(tx), self.rules.now_date(),
                triggered_by={"kind": "reconcile", "by": actor.user_id if actor else "system"},
            )
        open_reviews = self.repository.list_review_items(status="open")
        pending = self.repository.list_pending(status="pending")
        return {
            "pending_drain": drain,
            "chain_sweep": sweep,
            "open_reviews": len(open_reviews),
            "pending_changes": len(pending),
            "as_of": self.rules.now_date(),
        }

    # Convenience kept for tests / scripts using a fixed clock.
    def now_date(self):
        return self.rules.now_date()
