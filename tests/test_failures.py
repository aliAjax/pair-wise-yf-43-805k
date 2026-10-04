import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def test_permission_denied(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'instrument', {'name': 'I', 'serial': 'S'}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                entity["id"],
                'send_calibration',
                {},
            )

    def test_late_commit_is_rebased_onto_latest_version(self):
        # A stale expected_version must not overwrite blindly: the late commit
        # is validated and applied against the newest version instead.
        entity = self.service.create(
            Actor("admin", "admin"), 'instrument', {'name': 'I', 'serial': 'S'}
        )
        stale_version = entity["version"]
        # Another actor moves the instrument to calibrating first.
        self.service.transition(
            Actor("tech", "technician"), entity["id"], 'send_calibration', {}
        )
        # Late quarantine commit referencing the old version: rebased onto
        # current status -> still blocked as an invalid transition, queued.
        deferred = self.service.transition(
            Actor("met", "metrology"),
            entity["id"],
            'quarantine',
            {'reason': 'late request'},
            expected_version=stale_version,
        )
        self.assertEqual(deferred["status"], "pending_retry")
        pending = self.service.list_pending(status="pending")
        self.assertEqual(len(pending), 1)
        # Once the instrument returns to active, the queued commit goes through.
        self.service.transition(
            Actor("met", "metrology"),
            entity["id"],
            'calibrate',
            {'due_at': '2099-01-01', 'passed': True},
        )
        self.service.drain_pending()
        self.assertEqual(
            self.service.get(entity["id"])["status"], "quarantined"
        )
        self.assertEqual(self.service.list_pending(status="pending"), [])

    def test_repository_optimistic_lock_still_guards_writes(self):
        # The storage layer never writes on a stale row version.
        entity = self.service.create(
            Actor("admin", "admin"), 'instrument', {'name': 'I', 'serial': 'S'}
        )
        with self.assertRaises(ConflictError):
            self.repo.update_entity(
                entity["id"], 999, "calibrating", entity["data"]
            )

    def test_duplicate_idempotency_key_returns_same_entity(self):
        first = self.service.create(
            Actor("admin", "admin"),
            'instrument',
            {'name': 'I', 'serial': 'S'},
            idempotency_key="duplicate-check",
        )
        second = self.service.create(
            Actor("admin", "admin"),
            'instrument',
            {'name': 'I', 'serial': 'S'},
            idempotency_key="duplicate-check",
        )
        self.assertEqual(first["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
