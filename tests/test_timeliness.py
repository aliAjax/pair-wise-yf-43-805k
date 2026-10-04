import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TimelinessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_released_result(self, factor=2.0, due_at="2026-12-31"):
        inst = self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.actor, inst["id"], "send_calibration", {})
        self.service.transition(
            self.actor, inst["id"], "calibrate", {"due_at": due_at, "passed": True}
        )
        cal = self.service.create(
            self.actor,
            "calibration",
            {"instrument_id": inst["id"], "requested_at": "2026-01-01"},
        )
        self.service.transition(
            self.actor,
            cal["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-01-02",
                "uncertainty": 0.01,
                "due_at": due_at,
            },
        )
        self.service.approve_calibration(self.actor, cal["id"], "QA-1")
        method = self.service.create(
            self.actor, "method", {"name": "Assay-A", "version": "v1"}
        )
        self.service.transition(
            self.actor,
            method["id"],
            "validate_method",
            {
                "parameters": {"range": [0, 10], "factor": factor},
                "instrument_ids": [inst["id"]],
            },
        )
        result = self.service.create(
            self.actor, "result", {"sample_id": "S-1", "measurement": "initial"}
        )
        result = self.service.transition(
            self.actor,
            result["id"],
            "release",
            {
                "instrument_id": inst["id"],
                "method_id": method["id"],
                "value": 10.0,
                "unit": "mg/L",
            },
        )
        return inst, cal, method, result

    def test_calibration_expiration_triggers_recalc(self):
        inst, cal, method, result = self._setup_released_result()
        with patch("src.rules._today", return_value="2027-01-01"):
            count = self.service.check_expirations()
            self.assertGreaterEqual(count, 1)
            self.service.process_recalc_jobs()
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["reason"], "calibration_expired")
        self.assertIn("recalculated_value", result["data"])
        self.assertEqual(result["data"]["recalculated_value"], 20.0)
        self.assertEqual(result["data"]["value"], 10.0)
        todos = self.service.list_review_todos(status="open")
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["entity_id"], result["id"])
        self.assertEqual(todos[0]["trigger"], "calibration_expired")

    def test_method_revoked_triggers_recalc(self):
        inst, cal, method, result = self._setup_released_result()
        self.service.revoke_method(self.actor, method["id"], "method discontinued")
        self.service.process_recalc_jobs()
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["reason"], "method_revoked")
        todos = self.service.list_review_todos(status="open")
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["trigger"], "method_revoked")

    def test_overlapping_calibration_raises_conflict(self):
        inst, cal1, method, result = self._setup_released_result()
        cal2 = self.service.create(
            self.actor,
            "calibration",
            {"instrument_id": inst["id"], "requested_at": "2026-06-01"},
        )
        self.service.transition(
            self.actor,
            cal2["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-06-01",
                "uncertainty": 0.02,
                "due_at": "2027-06-01",
            },
        )
        with self.assertRaises(ConflictError):
            self.service.approve_calibration(self.actor, cal2["id"], "QA-1")

    def test_recalc_uses_latest_version(self):
        inst, cal, method, result = self._setup_released_result(factor=2.0)
        self.service._enqueue_recalc(result["id"], "calibration_expired")
        self.repo.update_entity(
            result["id"], result["version"], "released",
            {**result["data"], "value": 20.0},
        )
        self.service.process_recalc_jobs()
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["recalculated_value"], 40.0)

    def test_recalc_failure_keeps_pending_and_retries(self):
        inst, cal, method, result = self._setup_released_result()
        self.service._enqueue_recalc(result["id"], "calibration_expired")
        original_update = self.repo.update_entity
        calls = [0]

        def failing_update(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise ConflictError("simulated concurrent modification")
            return original_update(*args, **kwargs)

        with patch.object(self.repo, "update_entity", side_effect=failing_update):
            self.service.process_recalc_jobs()
        jobs = self.service.list_recalc_jobs(status="pending")
        self.assertEqual(len(jobs), 1)
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "released")
        self.service.process_recalc_jobs()
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "pending_review")
        jobs = self.service.list_recalc_jobs(status="done")
        self.assertEqual(len(jobs), 1)

    def test_resume_processes_pending_jobs(self):
        inst, cal, method, result = self._setup_released_result()
        self.service._enqueue_recalc(result["id"], "calibration_expired")
        service2 = DomainService(self.repo, RuleEngine())
        service2.resume()
        result = service2.get(result["id"])
        self.assertEqual(result["status"], "pending_review")
        todos = service2.list_review_todos(status="open")
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["entity_id"], result["id"])

    def test_review_decision_closes_todo(self):
        inst, cal, method, result = self._setup_released_result()
        self.service.revoke_method(self.actor, method["id"], "reason")
        self.service.process_recalc_jobs()
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "pending_review")
        todos = self.service.list_review_todos(status="open")
        self.assertEqual(len(todos), 1)
        self.service.close_review_todo(
            todos[0]["id"], self.actor, "approve", "review passed"
        )
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "released")
        open_todos = self.service.list_review_todos(status="open")
        self.assertEqual(len(open_todos), 0)
        closed_todos = self.service.list_review_todos(status="closed")
        self.assertEqual(len(closed_todos), 1)

    def test_release_rejects_expired_calibration(self):
        inst, cal, method, result = self._setup_released_result()
        result2 = self.service.create(
            self.actor, "result", {"sample_id": "S-2", "measurement": "initial"}
        )
        with patch("src.rules._today", return_value="2027-01-01"):
            with self.assertRaises(ValidationError):
                self.service.transition(
                    self.actor,
                    result2["id"],
                    "release",
                    {
                        "instrument_id": inst["id"],
                        "method_id": method["id"],
                        "value": 10.0,
                        "unit": "mg/L",
                    },
                )


if __name__ == "__main__":
    unittest.main()
