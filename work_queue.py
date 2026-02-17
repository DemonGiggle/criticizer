from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

VALID_STATUSES = ("queued", "running", "completed", "failed")


@dataclass(frozen=True)
class MutationResult:
    ok: bool
    rows_affected: int
    diagnostics: dict[str, Any]


class WorkQueueStore:
    """Persistence helpers for work_queue state transitions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # Define now() once so every mutation can use DB-side now() in SQL.
        self.conn.create_function("now", 0, lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS work_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT,
                status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                claimed_by TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT (now()),
                updated_at TEXT NOT NULL DEFAULT (now())
            )
            """
        )
        self.conn.commit()

    def enqueue(self, payload: str) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO work_queue (payload, status)
            VALUES (?, 'queued')
            """,
            (payload,),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim(self, job_id: int, worker_id: str) -> MutationResult:
        rows = self.conn.execute(
            """
            UPDATE work_queue
            SET status = 'running', claimed_by = ?, updated_at = now()
            WHERE id = ? AND status = 'queued'
            """,
            (worker_id, job_id),
        ).rowcount
        self.conn.commit()
        if rows == 0:
            current = self._get_status(job_id)
            return MutationResult(
                ok=False,
                rows_affected=0,
                diagnostics={
                    "code": "invalid_transition",
                    "from": current,
                    "to": "running",
                    "allowed_from": ["queued"],
                },
            )
        return MutationResult(ok=True, rows_affected=rows, diagnostics={"code": "ok"})

    def heartbeat(self, job_id: int, worker_id: str) -> MutationResult:
        rows = self.conn.execute(
            """
            UPDATE work_queue
            SET lease_expires_at = datetime(now(), '+30 seconds'),
                updated_at = now()
            WHERE id = ? AND claimed_by = ? AND status = 'running'
            """,
            (job_id, worker_id),
        ).rowcount
        self.conn.commit()
        return self._owner_guard_result(rows, job_id, worker_id, action="heartbeat")

    def complete(self, job_id: int, worker_id: str) -> MutationResult:
        return self._finalize(job_id, worker_id, "completed")

    def fail(self, job_id: int, worker_id: str) -> MutationResult:
        return self._finalize(job_id, worker_id, "failed")

    def _finalize(self, job_id: int, worker_id: str, target_status: str) -> MutationResult:
        if target_status not in ("completed", "failed"):
            return MutationResult(
                ok=False,
                rows_affected=0,
                diagnostics={
                    "code": "invalid_status",
                    "status": target_status,
                    "valid_statuses": list(VALID_STATUSES),
                },
            )

        rows = self.conn.execute(
            """
            UPDATE work_queue
            SET status = ?,
                claimed_by = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE id = ? AND claimed_by = ? AND status = 'running'
            """,
            (target_status, job_id, worker_id),
        ).rowcount
        self.conn.commit()

        if rows == 0:
            current = self._get_status(job_id)
            owner = self._get_owner(job_id)
            reason = "not_owner" if owner is not None and owner != worker_id else "invalid_transition"
            return MutationResult(
                ok=False,
                rows_affected=0,
                diagnostics={
                    "code": reason,
                    "action": "finalize",
                    "job_id": job_id,
                    "requested_by": worker_id,
                    "owner": owner,
                    "from": current,
                    "to": target_status,
                    "required_from": "running",
                },
            )

        return MutationResult(ok=True, rows_affected=rows, diagnostics={"code": "ok", "to": target_status})

    def _owner_guard_result(self, rows: int, job_id: int, worker_id: str, action: str) -> MutationResult:
        if rows:
            return MutationResult(ok=True, rows_affected=rows, diagnostics={"code": "ok"})
        owner = self._get_owner(job_id)
        status = self._get_status(job_id)
        reason = "not_owner" if owner is not None and owner != worker_id else "invalid_transition"
        return MutationResult(
            ok=False,
            rows_affected=0,
            diagnostics={
                "code": reason,
                "action": action,
                "job_id": job_id,
                "requested_by": worker_id,
                "owner": owner,
                "status": status,
                "required_status": "running",
            },
        )

    def _get_status(self, job_id: int) -> str | None:
        row = self.conn.execute("SELECT status FROM work_queue WHERE id = ?", (job_id,)).fetchone()
        return row[0] if row else None

    def _get_owner(self, job_id: int) -> str | None:
        row = self.conn.execute("SELECT claimed_by FROM work_queue WHERE id = ?", (job_id,)).fetchone()
        return row[0] if row else None

    def get_job(self, job_id: int) -> sqlite3.Row:
        self.conn.row_factory = sqlite3.Row
        row = self.conn.execute("SELECT * FROM work_queue WHERE id = ?", (job_id,)).fetchone()
        assert row is not None
        return row
