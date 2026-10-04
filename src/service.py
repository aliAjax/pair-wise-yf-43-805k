from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError, Actor
from .repository import utcnow
from .rules import (
    RuleEngine,
    calibration_current,
    current_calibration,
    recalc_value,
)


class DomainService:
    MAX_RECALC_ATTEMPTS = 100

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

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

    # ---- 动作分发：按 (kind, action) 路由到带副作用的专用方法 ----

    def dispatch(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = entity["kind"]
        if kind == "method" and action == "revoke_method":
            return self.revoke_method(
                actor, entity_id, (data or {}).get("reason"), data, expected_version
            )
        if kind == "calibration" and action == "approve":
            return self.approve_calibration(
                actor, entity_id, (data or {}).get("authorized_by"), data, expected_version
            )
        return self.transition(actor, entity_id, action, data, expected_version)

    # ---- 校准记录审批：同步仪器到期日 ----

    def approve_calibration(self, actor, calibration_id, authorized_by, data=None, expected_version=None):
        updated = self.transition(
            actor, calibration_id, "approve",
            {"authorized_by": authorized_by}, expected_version,
        )
        cal = self.repository.get_entity(calibration_id)
        instrument = self.repository.get_entity(cal["data"].get("instrument_id"))
        if instrument:
            due = cal["data"].get("due_at")
            merged = dict(instrument["data"])
            if due:
                merged["due_at"] = due
            next_status = "active" if instrument["status"] == "calibrating" else instrument["status"]
            self.repository.update_entity(instrument["id"], instrument["version"], next_status, merged)
        return updated

    # ---- 方法撤销：触发关联结果重算 ----

    def revoke_method(self, actor, method_id, reason, data=None, expected_version=None):
        updated = self.transition(
            actor, method_id, "revoke_method", {"reason": reason}, expected_version
        )
        self._enqueue_results_for_method(method_id, "method_revoked")
        return updated

    def _enqueue_results_for_method(self, method_id, trigger):
        results = self.repository.list_entities(kind="result", status="released")
        for result in results:
            if result["data"].get("method_id") == method_id:
                self._enqueue_recalc(result["id"], trigger)

    def _enqueue_results_for_instrument(self, instrument_id, trigger):
        results = self.repository.list_entities(kind="result", status="released")
        for result in results:
            if result["data"].get("instrument_id") == instrument_id:
                self._enqueue_recalc(result["id"], trigger)

    def _enqueue_recalc(self, entity_id, trigger):
        existing = self.repository.find_open_recalc_job(entity_id, trigger)
        if existing:
            return existing
        return self.repository.create_recalc_job(str(uuid4()), entity_id, trigger)

    # ---- 过期检查 ----

    def check_expirations(self):
        instruments = self.repository.list_entities(kind="instrument")
        count = 0
        for instrument in instruments:
            if instrument["status"] != "active":
                continue
            cal = current_calibration(instrument["id"], self._lookup)
            if not cal:
                self._enqueue_results_for_instrument(instrument["id"], "calibration_expired")
                count += 1
                continue
            due = cal["data"].get("due_at", "")
            if not calibration_current(due, self.rules.today()):
                self._enqueue_results_for_instrument(instrument["id"], "calibration_expired")
                count += 1
        return count

    # ---- 重算任务处理 ----

    def process_recalc_jobs(self):
        jobs = self.repository.find_pending_recalc_jobs()
        processed = []
        for job in jobs:
            try:
                updated = self._process_recalc_job(job)
                self.repository.mark_recalc_job(job["id"], "done")
                processed.append({"job_id": job["id"], "status": "done", "updated": updated})
            except Exception as exc:
                attempts = int(job["attempts"]) + 1
                if attempts >= self.MAX_RECALC_ATTEMPTS:
                    self.repository.mark_recalc_job(job["id"], "failed", last_error=str(exc))
                else:
                    self.repository.mark_recalc_job(job["id"], "pending", last_error=str(exc))
                processed.append({"job_id": job["id"], "status": "pending", "error": str(exc)})
        return processed

    def _process_recalc_job(self, job):
        result = self.repository.get_entity(job["entity_id"])
        if not result:
            raise NotFoundError("result not found: " + job["entity_id"])
        if result["status"] != "released":
            return None
        method = self.repository.get_entity(result["data"].get("method_id"))
        recalculated = recalc_value(result["data"].get("value"), method)
        data = {
            "reason": job["trigger"],
            "recalculated_value": recalculated,
            "recalculated_at": utcnow(),
        }
        system = Actor("system", "admin")
        updated = self.transition(
            system, result["id"], "send_to_review", data, result["version"]
        )
        self.repository.create_review_todo(
            str(uuid4()),
            result["id"],
            job["trigger"],
            result["data"].get("value"),
            recalculated,
            {
                "instrument_id": result["data"].get("instrument_id"),
                "method_id": result["data"].get("method_id"),
            },
        )
        return updated

    # ---- 复核待办 ----

    def list_review_todos(self, status=None):
        return self.repository.list_review_todos(status=status)

    def close_review_todo(self, todo_id, actor, decision, comment=None):
        todo = self.repository.get_review_todo(todo_id)
        if not todo:
            raise NotFoundError("review todo not found: " + todo_id)
        if todo["status"] != "open":
            raise ValidationError("review todo is already closed")
        result = self.repository.get_entity(todo["entity_id"])
        if decision == "approve":
            updated = self.transition(
                actor, result["id"], "approve_review", {"comment": comment}, result["version"]
            )
        elif decision == "reject":
            updated = self.transition(
                actor, result["id"], "reject_review",
                {"reason": comment or "review rejected"}, result["version"]
            )
        else:
            raise ValidationError("decision must be approve or reject")
        self.repository.close_review_todo(todo_id)
        return updated

    def list_recalc_jobs(self, status=None):
        return self.repository.list_recalc_jobs(status=status)

    # ---- 启动恢复 ----

    def run_checks(self):
        expired = self.check_expirations()
        jobs = self.process_recalc_jobs()
        return {"expired_instruments": expired, "jobs": jobs}

    def resume(self):
        return self.run_checks()
