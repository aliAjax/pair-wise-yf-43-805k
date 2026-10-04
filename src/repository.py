import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
    CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        data TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_entities_kind_status
        ON entities(kind, status);
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        action TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_audit_entity
        ON audit_log(entity_id, id);
    CREATE TABLE IF NOT EXISTS idempotency (
        actor_id TEXT NOT NULL,
        idem_key TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(actor_id, idem_key)
    );
    CREATE TABLE IF NOT EXISTS review_items (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        ref_type TEXT NOT NULL,
        ref_id TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        reason_key TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        resolution TEXT,
        decided_by TEXT,
        decision_note TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_review_open_unique
        ON review_items(ref_type, ref_id)
        WHERE status = 'open';
    CREATE INDEX IF NOT EXISTS idx_review_status
        ON review_items(status, kind);
    CREATE TABLE IF NOT EXISTS pending_changes (
        id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        data TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        requested_role TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_open_unique
        ON pending_changes(entity_id, action)
        WHERE status = 'pending';
    CREATE INDEX IF NOT EXISTS idx_pending_status
        ON pending_changes(status, created_at);
"""


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _entity_from_row(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "version": int(row["version"]),
        "data": json.loads(row["data"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _review_from_row(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "ref_type": row["ref_type"],
        "ref_id": row["ref_id"],
        "status": row["status"],
        "reason": row["reason"],
        "reason_key": row["reason_key"],
        "detail": json.loads(row["detail"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "resolution": row["resolution"],
        "decided_by": row["decided_by"],
        "decision_note": row["decision_note"],
    }


def _pending_from_row(row):
    return {
        "id": row["id"],
        "entity_id": row["entity_id"],
        "action": row["action"],
        "data": json.loads(row["data"]),
        "requested_by": row["requested_by"],
        "requested_role": row["requested_role"],
        "status": row["status"],
        "attempts": int(row["attempts"]),
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class Transaction:
    """All operations participate in one BEGIN IMMEDIATE transaction."""

    def __init__(self, connection):
        self.connection = connection

    def get_entity(self, entity_id):
        row = self.connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return _entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [_entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        row = self.connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        self.connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ?",
            (status, _dumps(data), now, entity_id),
        )
        return self.get_entity(entity_id)

    def insert_audit(self, entity_id, actor_id, actor_role, action,
                     from_status, to_status, detail):
        self.connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
            "from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                _dumps(detail or {}),
                utcnow(),
            ),
        )

    def create_review_item(self, item):
        now = utcnow()
        self.connection.execute(
            "INSERT INTO review_items(id, kind, ref_type, ref_id, status, reason, "
            "reason_key, detail, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (
                item["id"],
                item["kind"],
                item["ref_type"],
                item["ref_id"],
                item["reason"],
                item["reason_key"],
                _dumps(item.get("detail", {})),
                item.get("created_by", "system"),
                now,
            ),
        )
        return self.get_review_item(item["id"])

    def get_review_item(self, review_id):
        row = self.connection.execute(
            "SELECT * FROM review_items WHERE id = ?", (review_id,)
        ).fetchone()
        return _review_from_row(row) if row else None

    def find_open_review(self, ref_type, ref_id):
        row = self.connection.execute(
            "SELECT * FROM review_items WHERE ref_type = ? AND ref_id = ? "
            "AND status = 'open'",
            (ref_type, ref_id),
        ).fetchone()
        return _review_from_row(row) if row else None

    def list_review_items(self, status=None, kind=None):
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM review_items" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [_review_from_row(row) for row in rows]

    def resolve_review_item(self, review_id, resolution, decided_by, note):
        existing = self.get_review_item(review_id)
        if not existing:
            raise NotFoundError("review item not found: " + review_id)
        if existing["status"] != "open":
            raise ConflictError("review item already resolved: " + review_id)
        self.connection.execute(
            "UPDATE review_items SET status = 'resolved', resolution = ?, "
            "decided_by = ?, decision_note = ?, resolved_at = ? WHERE id = ?",
            (resolution, decided_by, note, utcnow(), review_id),
        )
        return self.get_review_item(review_id)

    def enqueue_pending(self, change):
        now = utcnow()
        self.connection.execute(
            "INSERT INTO pending_changes(id, entity_id, action, data, requested_by, "
            "requested_role, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
            (
                change["id"],
                change["entity_id"],
                change["action"],
                _dumps(change.get("data", {})),
                change["requested_by"],
                change["requested_role"],
                now,
                now,
            ),
        )
        return self.get_pending(change["id"])

    def get_pending(self, pending_id):
        row = self.connection.execute(
            "SELECT * FROM pending_changes WHERE id = ?", (pending_id,)
        ).fetchone()
        return _pending_from_row(row) if row else None

    def find_open_pending(self, entity_id, action):
        row = self.connection.execute(
            "SELECT * FROM pending_changes WHERE entity_id = ? AND action = ? "
            "AND status = 'pending'",
            (entity_id, action),
        ).fetchone()
        return _pending_from_row(row) if row else None

    def list_pending(self, status=None):
        if status:
            rows = self.connection.execute(
                "SELECT * FROM pending_changes WHERE status = ? ORDER BY created_at, id",
                (status,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM pending_changes ORDER BY created_at, id"
            ).fetchall()
        return [_pending_from_row(row) for row in rows]

    def mark_pending(self, pending_id, status, error=None):
        self.connection.execute(
            "UPDATE pending_changes SET status = ?, attempts = attempts + 1, "
            "last_error = ?, updated_at = ? WHERE id = ?",
            (status, error, utcnow(), pending_id),
        )
        return self.get_pending(pending_id)

    def cancel_pending(self, pending_id, reason, actor_id):
        existing = self.get_pending(pending_id)
        if not existing:
            raise NotFoundError("pending change not found: " + pending_id)
        if existing["status"] != "pending":
            raise ConflictError(
                "pending change is %s: %s" % (existing["status"], pending_id)
            )
        self.connection.execute(
            "UPDATE pending_changes SET status = 'cancelled', last_error = ?, "
            "updated_at = ? WHERE id = ?",
            ("cancelled by %s: %s" % (actor_id, reason), utcnow(), pending_id),
        )
        return self.get_pending(pending_id)


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield Transaction(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, _dumps(data), actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return _entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [_entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        with self.transaction() as tx:
            return tx.update_entity(entity_id, expected_version, status, data)

    def append_audit(self, entity_id, actor_id, actor_role, action,
                     from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                "from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    _dumps(detail or {}),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id",
                    (entity_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_log ORDER BY id"
                ).fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, "
                "created_at) VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # Read-only accessors for the durable review / retry queues.
    def get_review_item(self, review_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_items WHERE id = ?", (review_id,)
            ).fetchone()
        return _review_from_row(row) if row else None

    def list_review_items(self, status=None, kind=None):
        with self.transaction() as tx:
            return tx.list_review_items(status=status, kind=kind)

    def get_pending(self, pending_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_changes WHERE id = ?", (pending_id,)
            ).fetchone()
        return _pending_from_row(row) if row else None

    def list_pending(self, status=None):
        with self.transaction() as tx:
            return tx.list_pending(status=status)

    def cancel_pending(self, pending_id, reason, actor_id):
        with self.transaction() as tx:
            return tx.cancel_pending(pending_id, reason, actor_id)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
