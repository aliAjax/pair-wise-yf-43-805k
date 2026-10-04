import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
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
                CREATE TABLE IF NOT EXISTS recalc_jobs (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recalc_jobs_status
                    ON recalc_jobs(status);
                CREATE INDEX IF NOT EXISTS idx_recalc_jobs_entity
                    ON recalc_jobs(entity_id);
                CREATE TABLE IF NOT EXISTS review_todos (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    original_value TEXT,
                    recalculated_value TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_review_todos_status
                    ON review_todos(status);
                CREATE INDEX IF NOT EXISTS idx_review_todos_entity
                    ON review_todos(entity_id);
            """)

    @staticmethod
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

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
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
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
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
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
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
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def create_recalc_job(self, job_id, entity_id, trigger):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO recalc_jobs(id, entity_id, trigger, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', 0, ?, ?)",
                (job_id, entity_id, trigger, now, now),
            )
        return self.get_recalc_job(job_id)

    def get_recalc_job(self, job_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recalc_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._recalc_job_from_row(row) if row else None

    def _recalc_job_from_row(self, row):
        return {
            "id": row["id"],
            "entity_id": row["entity_id"],
            "trigger": row["trigger"],
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def find_pending_recalc_jobs(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recalc_jobs WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [self._recalc_job_from_row(row) for row in rows]

    def find_open_recalc_job(self, entity_id, trigger):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recalc_jobs WHERE entity_id = ? AND trigger = ? AND status = 'pending' "
                "ORDER BY id LIMIT 1",
                (entity_id, trigger),
            ).fetchone()
        return self._recalc_job_from_row(row) if row else None

    def list_recalc_jobs(self, status=None):
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM recalc_jobs WHERE status = ? ORDER BY id", (status,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM recalc_jobs ORDER BY id"
                ).fetchall()
        return [self._recalc_job_from_row(row) for row in rows]

    def mark_recalc_job(self, job_id, status, last_error=None):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE recalc_jobs SET status = ?, attempts = attempts + 1, last_error = ?, updated_at = ? "
                "WHERE id = ?",
                (status, last_error, now, job_id),
            )
        return self.get_recalc_job(job_id)

    def create_review_todo(self, todo_id, entity_id, trigger, original_value, recalculated_value, detail):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO review_todos(id, entity_id, trigger, status, original_value, recalculated_value, detail, created_at) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
                (
                    todo_id,
                    entity_id,
                    trigger,
                    json.dumps(original_value, ensure_ascii=False, sort_keys=True),
                    json.dumps(recalculated_value, ensure_ascii=False, sort_keys=True),
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return self.get_review_todo(todo_id)

    def get_review_todo(self, todo_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_todos WHERE id = ?", (todo_id,)
            ).fetchone()
        return self._review_todo_from_row(row) if row else None

    def _review_todo_from_row(self, row):
        return {
            "id": row["id"],
            "entity_id": row["entity_id"],
            "trigger": row["trigger"],
            "status": row["status"],
            "original_value": json.loads(row["original_value"]) if row["original_value"] else None,
            "recalculated_value": json.loads(row["recalculated_value"]) if row["recalculated_value"] else None,
            "detail": json.loads(row["detail"]),
            "created_at": row["created_at"],
            "closed_at": row["closed_at"],
        }

    def find_open_review_todo(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_todos WHERE entity_id = ? AND status = 'open' ORDER BY id LIMIT 1",
                (entity_id,),
            ).fetchone()
        return self._review_todo_from_row(row) if row else None

    def list_review_todos(self, status=None):
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM review_todos WHERE status = ? ORDER BY id", (status,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM review_todos ORDER BY id"
                ).fetchall()
        return [self._review_todo_from_row(row) for row in rows]

    def close_review_todo(self, todo_id):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE review_todos SET status = 'closed', closed_at = ? WHERE id = ?",
                (now, todo_id),
            )
        return self.get_review_todo(todo_id)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
