import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, CalibrationOverlapError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, covering_calibration, intervals_overlap
from src.service import DomainService


class FixedClock:
    def __init__(self, day):
        self.day = day

    def now(self):
        from datetime import date

        return date.fromisoformat(self.day)

    def set(self, day):
        self.day = day


ADMIN = Actor("admin", "admin")
MET = Actor("met-1", "metrology")
QA = Actor("qa-1", "authorizer")
ANALYST = Actor("lab-1", "analyst")


class TimelinessChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FixedClock("2026-02-01")
        self.repo = SQLiteRepository(Path(self.tmp.name) / "chain.db")
        self.service = DomainService(self.repo, RuleEngine(clock=self.clock))

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------ fixtures

    def _setup_lab(self, due_at="2026-06-30", factor=None):
        instrument = self.service.create(
            ADMIN, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        calibration = self.service.create(
            MET,
            "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        calibration = self.service.transition(
            MET,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-01-10",
                "uncertainty": 0.01,
                "due_at": due_at,
            },
        )
        calibration = self.service.transition(
            QA,
            calibration["id"],
            "approve",
            {"authorized_by": "QA-1", "approved_at": "2026-01-10"},
        )
        parameters = {"range": [0, 10]}
        if factor is not None:
            parameters["correction_factor"] = factor
        method = self.service.create(
            QA, "method", {"name": "Assay-A", "version": "v1"}
        )
        method = self.service.transition(
            QA,
            method["id"],
            "validate_method",
            {"parameters": parameters, "instrument_ids": [instrument["id"]]},
        )
        return instrument, calibration, method

    def _release_result(self, instrument, method, sample="S-1", value=4.0, at="2026-02-01"):
        result = self.service.create(
            ANALYST, "result", {"sample_id": sample, "measurement": value}
        )
        return self.service.transition(
            ANALYST,
            result["id"],
            "release",
            {
                "instrument_id": instrument["id"],
                "method_id": method["id"],
                "value": value,
                "unit": "mg/L",
                "released_at": at,
            },
        )

    # ------------------------------------------------------------- helpers

    def _overlapping_calibration(self, instrument, start="2026-03-01", end="2026-09-30"):
        calib = self.service.create(
            MET,
            "calibration",
            {"instrument_id": instrument["id"], "requested_at": start},
        )
        calib = self.service.transition(
            MET,
            calib["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": start,
                "uncertainty": 0.02,
                "due_at": end,
            },
        )
        return calib

    # ---------------------------------------------------------------- tests

    def test_release_binds_calibration_and_method_snapshot(self):
        instrument, calibration, method = self._setup_lab()
        result = self._release_result(instrument, method)
        data = result["data"]
        self.assertEqual(data["calibration_id"], calibration["id"])
        self.assertEqual(data["calibration_version"], calibration["version"])
        self.assertEqual(data["calibration_due"], "2026-06-30")
        self.assertEqual(data["method_version"], "v1")

    def test_calibration_expiry_recalculates_and_queues_for_review(self):
        instrument, calibration, method = self._setup_lab(
            due_at="2026-03-31", factor=1.0
        )
        result = self._release_result(instrument, method, value=4.0, at="2026-02-01")
        self.assertEqual(result["status"], "released")

        # Time passes: calibration is now expired. Reconcile must flag it.
        self.clock.set("2026-04-15")
        summary = self.service.reconcile()
        self.assertEqual(summary["chain_sweep"]["flagged"], 1)

        updated = self.service.get(result["id"])
        self.assertEqual(updated["status"], "review_pending")
        # Original released value is preserved.
        self.assertEqual(updated["data"]["original_release"]["value"], 4.0)
        self.assertIn("calibration_expired", updated["data"]["invalid_reasons"])
        # Recalculated against the still-valid method factor.
        self.assertEqual(updated["data"]["recalculated_value"], 4.0)

        reviews = self.service.list_reviews()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["kind"], "result_chain")
        self.assertEqual(reviews[0]["ref_id"], result["id"])

        # Sweep is idempotent: no duplicate reviews on a second reconcile.
        self.service.reconcile()
        self.assertEqual(len(self.service.list_reviews()), 1)

    def test_recalculation_uses_current_method_factor(self):
        instrument, calibration, method = self._setup_lab(factor=2.0)
        result = self._release_result(instrument, method, value=5.0)
        self.clock.set("2026-08-01")  # after due 2026-06-30
        self.service.reconcile()
        updated = self.service.get(result["id"])
        self.assertEqual(updated["data"]["recalculated_value"], 10.0)
        self.assertEqual(updated["data"]["original_release"]["value"], 5.0)

    def test_method_revocation_flags_bound_results_without_overwriting_value(self):
        instrument, calibration, method = self._setup_lab()
        result = self._release_result(instrument, method, value=3.5)
        outcome = self.service.transition(
            QA,
            method["id"],
            "revoke_method",
            {"reason": "bias found"},
        )
        self.assertEqual(outcome["chain_review"]["flagged"], 1)
        updated = self.service.get(result["id"])
        self.assertEqual(updated["status"], "review_pending")
        self.assertIn("method_revoked", updated["data"]["invalid_reasons"])
        # Recomputation is impossible once the method is revoked.
        self.assertIsNone(updated["data"]["recalculated_value"])
        self.assertEqual(updated["data"]["original_release"]["value"], 3.5)
        review = self.service.list_reviews(kind="result_chain")[0]
        # Reviewer keeps the original reading with justification.
        resolved = self.service.resolve_review(
            QA, review["id"], "keep", "原始读数经比对方法 v2 可接受"
        )
        self.assertEqual(resolved["status"], "resolved")
        final = self.service.get(result["id"])
        self.assertEqual(final["status"], "released")
        self.assertEqual(final["data"]["value"], 3.5)
        self.assertEqual(final["data"]["original_release"]["value"], 3.5)

    def test_review_requeue_blocks_result_for_reanalysis(self):
        instrument, calibration, method = self._setup_lab()
        result = self._release_result(instrument, method)
        self.service.transition(
            QA, method["id"], "revoke_method", {"reason": "x"}
        )
        review = self.service.list_reviews()[0]
        self.service.resolve_review(QA, review["id"], "requeue", "需要复测")
        self.assertEqual(self.service.get(result["id"])["status"], "blocked")
        self.assertEqual(self.service.list_reviews(status="open"), [])

    def test_overlapping_calibration_is_held_not_overwritten(self):
        instrument, calibration, method = self._setup_lab()
        second = self._overlapping_calibration(instrument)
        # The service never lets one calibration overwrite the other: the
        # approval is stopped and converted into a comparison to-do.
        held = self.service.transition(
            QA, second["id"], "approve", {"authorized_by": "QA-2"}
        )
        self.assertEqual(held["status"], "overlap_review")
        # Nothing was approved or overwritten.
        first = self.service.get(calibration["id"])
        held_entity = self.service.get(second["id"])
        self.assertEqual(first["status"], "approved")
        self.assertEqual(held_entity["status"], "passed")

        reviews = self.service.list_reviews(kind="calibration_overlap")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["ref_id"], second["id"])
        # The late approval is persisted and waits for the comparison.
        pending = self.service.list_pending(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["entity_id"], second["id"])

        # Retrying before the decision changes nothing.
        drain = self.service.drain_pending()
        self.assertEqual(drain["held"], 1)
        self.assertEqual(self.service.get(second["id"])["status"], "passed")

        # Explicit decision: new calibration supersedes the old one.
        resolved = self.service.resolve_review(
            QA, reviews[0]["id"], "supersede", "新校准溯源链更完整"
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(self.service.get(second["id"])["status"], "approved")
        self.assertEqual(self.service.get(calibration["id"])["status"], "superseded")
        self.assertEqual(
            self.service.get(calibration["id"])["data"]["superseded_by"],
            second["id"],
        )
        # The queued approval was applied during resolution and cleared.
        self.assertEqual(self.service.list_pending(status="pending"), [])

    def test_overlap_reject_new_keeps_existing_calibration(self):
        instrument, calibration, method = self._setup_lab()
        second = self._overlapping_calibration(instrument)
        self.service.transition(
            QA, second["id"], "approve", {"authorized_by": "QA-2"}
        )
        review = self.service.list_reviews(kind="calibration_overlap")[0]
        self.service.resolve_review(
            QA, review["id"], "reject_new", "新校准不确定度超限"
        )
        self.assertEqual(self.service.get(second["id"])["status"], "rejected")
        self.assertEqual(self.service.get(calibration["id"])["status"], "approved")
        # Queued approval for the rejected record is cancelled, not retried.
        self.assertEqual(self.service.list_pending(status="pending"), [])

    def test_superseding_recalculates_results_bound_to_old_calibration(self):
        instrument, calibration, method = self._setup_lab(factor=1.0)
        result = self._release_result(instrument, method, value=2.0)
        second = self._overlapping_calibration(instrument)
        self.service.transition(
            QA, second["id"], "approve", {"authorized_by": "QA-2"}
        )
        review = self.service.list_reviews(kind="calibration_overlap")[0]
        self.service.resolve_review(QA, review["id"], "supersede", "更换标准器")
        # Result was bound to the now-superseded calibration -> re-review.
        updated = self.service.get(result["id"])
        self.assertEqual(updated["status"], "review_pending")
        self.assertIn("calibration_not_approved", updated["data"]["invalid_reasons"])

    def test_amend_due_shorten_triggers_chain_and_rejects_overlap(self):
        instrument, calibration, method = self._setup_lab(due_at="2026-12-31")
        result = self._release_result(instrument, method, value=1.0, at="2026-02-01")
        # Shortening the window past the release date invalidates the chain.
        amended = self.service.transition(
            MET,
            calibration["id"],
            "amend_due",
            {"due_at": "2026-01-31", "reason": "登记错误"},
        )
        self.assertEqual(amended["status"], "approved")
        self.assertEqual(amended["data"]["due_at"], "2026-01-31")
        updated = self.service.get(result["id"])
        self.assertEqual(updated["status"], "review_pending")
        self.assertIn(
            "calibration_expired_at_release", updated["data"]["invalid_reasons"]
        )

    def test_amend_due_into_another_calibration_is_blocked(self):
        instrument, calibration, method = self._setup_lab(due_at="2026-03-31")
        # A later non-overlapping approved calibration.
        later = self._overlapping_calibration(instrument, start="2026-05-01", end="2026-09-30")
        later = self.service.transition(
            QA, later["id"], "approve", {"authorized_by": "QA-1"}
        )
        # Extending the first one into the second is an overlap: stop.
        held = self.service.transition(
            MET,
            calibration["id"],
            "amend_due",
            {"due_at": "2026-08-01", "reason": "延期"},
        )
        self.assertEqual(held["status"], "overlap_review")
        review = self.service.get_review(held["review_id"])
        self.assertEqual(review["kind"], "calibration_overlap")

    def test_state_and_todos_survive_restart(self):
        instrument, calibration, method = self._setup_lab()
        result = self._release_result(instrument, method)
        # Expire via clock + reconcile.
        self.clock.set("2026-09-01")
        self.service.reconcile()
        # A late, currently-inapplicable commit stays queued: quarantine is
        # invalid while the instrument is calibrating, so queue a late
        # quarantine referencing the old version.
        tech = Actor("tech-1", "technician")
        self.service.transition(tech, instrument["id"], "send_calibration", {})
        deferred = self.service.transition(
            MET,
            instrument["id"],
            "quarantine",
            {"reason": "late"},
            expected_version=1,
        )
        self.assertEqual(deferred["status"], "pending_retry")

        reviews_before = self.service.list_reviews()
        pending_before = self.service.list_pending(status="pending")

        # "Restart": brand new service/repository over the same database.
        restarted_repo = SQLiteRepository(self.repo.path)
        restarted = DomainService(restarted_repo, RuleEngine(clock=self.clock))
        summary = restarted.reconcile()
        self.assertEqual(summary["open_reviews"], len(reviews_before))
        self.assertEqual(summary["pending_changes"], len(pending_before))
        self.assertEqual(
            [item["id"] for item in restarted.list_reviews()],
            [item["id"] for item in reviews_before],
        )
        self.assertEqual(
            [item["id"] for item in restarted.list_pending(status="pending")],
            [item["id"] for item in pending_before],
        )
        self.assertEqual(restarted.get(result["id"])["status"], "review_pending")

        # After the instrument returns to active, the queued quarantine
        # applies on the next retry opportunity.
        restarted.transition(
            MET, instrument["id"], "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        restarted.drain_pending()
        self.assertEqual(restarted.get(instrument["id"])["status"], "quarantined")
        self.assertEqual(restarted.list_pending(status="pending"), [])

    def test_late_commit_that_still_applies_is_rebased_directly(self):
        # Two concurrent approvals on one calibration: the late one carries a
        # stale version, but approve is not repeatable. Use the instrument
        # lifecycle instead: a late restore sent while the instrument was
        # quarantined by an earlier commit applies immediately after rebase.
        instrument = self.service.create(
            ADMIN, "instrument", {"name": "Q", "serial": "Q-1"}
        )
        stale_version = instrument["version"]
        self.service.transition(MET, instrument["id"], "quarantine", {"reason": "fault"})
        # The late restore references the pre-quarantine version: it still
        # applies because restore is valid from the current status.
        restored = self.service.transition(
            MET, instrument["id"], "restore", {}, expected_version=stale_version
        )
        self.assertEqual(restored["status"], "active")
        self.assertEqual(self.service.list_pending(status="pending"), [])

    def test_interval_overlap_predicate(self):
        self.assertTrue(
            intervals_overlap(("2026-01-01", "2026-06-30"), ("2026-06-30", "2026-09-30"))
        )
        self.assertFalse(
            intervals_overlap(("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-09-30"))
        )

    def test_covering_calibration_picks_the_window(self):
        instrument, calibration, method = self._setup_lab(due_at="2026-06-30")
        lookup = self.service._lookup
        self.assertEqual(
            covering_calibration(lookup, instrument["id"], "2026-03-01")["id"],
            calibration["id"],
        )
        self.assertIsNone(
            covering_calibration(lookup, instrument["id"], "2026-07-01")
        )


if __name__ == "__main__":
    unittest.main()
