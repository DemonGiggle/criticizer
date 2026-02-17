from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

VALID_STATUSES = ("queued", "running", "completed", "failed")


@dataclass(frozen=True)
class MutationResult:
    ok: bool
    rows_affected: int
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class WorkerEvent:
    type: str
    job_id: int
    worker_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class WorkerRunResult:
    status: str
    lease_lost: bool
    events: list[WorkerEvent]


class WorkQueueStore:
    """Persistence helpers for work_queue state transitions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
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
                priority INTEGER NOT NULL DEFAULT 0,
                run_at TEXT NOT NULL DEFAULT (now()),
                claimed_by TEXT,
                lease_expires_at TEXT,
                started_at TEXT,
                created_at TEXT NOT NULL DEFAULT (now()),
                updated_at TEXT NOT NULL DEFAULT (now())
            )
            """
        )
        self.conn.commit()

    def enqueue(self, payload: str, *, priority: int = 0, run_at: str | None = None) -> int:
        scheduled_run_at = run_at or self.conn.execute("SELECT now()").fetchone()[0]
        cur = self.conn.execute(
            """
            INSERT INTO work_queue (payload, status, priority, run_at)
            VALUES (?, 'queued', ?, ?)
            """,
            (payload, priority, scheduled_run_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_next(
        self,
        worker_id: str,
        lease_duration_seconds: int = 30,
        *,
        max_active_running: int | None = None,
    ) -> sqlite3.Row | None:
        if max_active_running is not None and max_active_running < 0:
            raise ValueError("max_active_running must be >= 0")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                UPDATE work_queue
                SET status = 'queued',
                    claimed_by = NULL,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= now()
                """
            )
            row = self.conn.execute(
                """
                WITH active_capacity AS (
                    SELECT COUNT(*) AS active_running
                    FROM work_queue
                    WHERE status = 'running'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > now()
                ),
                candidate AS (
                    SELECT id
                    FROM work_queue
                    WHERE status = 'queued'
                      AND run_at <= now()
                      AND (? IS NULL OR (SELECT active_running FROM active_capacity) < ?)
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                )
                UPDATE work_queue
                SET status = 'running',
                    claimed_by = ?,
                    lease_expires_at = datetime(now(), '+' || ? || ' seconds'),
                    started_at = now(),
                    updated_at = now()
                WHERE id = (SELECT id FROM candidate)
                RETURNING *
                """,
                (max_active_running, max_active_running, worker_id, lease_duration_seconds),
            ).fetchone()
            self.conn.commit()
            return row
        except Exception:
            self.conn.rollback()
            raise

    def requeue_expired_running(self) -> MutationResult:
        rows = self.conn.execute(
            """
            UPDATE work_queue
            SET status = 'queued',
                claimed_by = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE status = 'running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= now()
            """
        ).rowcount
        self.conn.commit()
        return MutationResult(ok=True, rows_affected=rows, diagnostics={"code": "ok"})

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

    def heartbeat(self, job_id: int, worker_id: str, lease_duration_seconds: int = 30) -> MutationResult:
        rows = self.conn.execute(
            """
            UPDATE work_queue
            SET lease_expires_at = datetime(now(), '+' || ? || ' seconds'),
                updated_at = now()
            WHERE id = ? AND claimed_by = ? AND status = 'running'
            """,
            (lease_duration_seconds, job_id, worker_id),
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
        row = self.conn.execute("SELECT * FROM work_queue WHERE id = ?", (job_id,)).fetchone()
        assert row is not None
        return row


class WorkerRuntime:
    """Runs lease-bound processing loops and emits structured events."""

    def __init__(
        self,
        store: WorkQueueStore,
        worker_id: str,
        *,
        now_fn: Callable[[], float] | None = None,
    ):
        self.store = store
        self.worker_id = worker_id
        self.now_fn = now_fn or time.monotonic
        self.lease_lost = False

    def process_running_job(
        self,
        job_id: int,
        process_step: Callable[[], bool],
        *,
        lease_duration_seconds: int = 30,
    ) -> WorkerRunResult:
        heartbeat_every = max(1, lease_duration_seconds // 3)
        next_heartbeat_at = self.now_fn() + heartbeat_every
        events: list[WorkerEvent] = []

        while True:
            if self.now_fn() >= next_heartbeat_at:
                renewal = self.store.heartbeat(job_id, self.worker_id, lease_duration_seconds=lease_duration_seconds)
                if not renewal.ok:
                    self.lease_lost = True
                    events.append(
                        WorkerEvent(
                            type="lease_lost",
                            job_id=job_id,
                            worker_id=self.worker_id,
                            payload={"status": "lease_lost", "diagnostics": renewal.diagnostics},
                        )
                    )
                    return WorkerRunResult(status="lease_lost", lease_lost=True, events=events)

                events.append(
                    WorkerEvent(
                        type="heartbeat_renewed",
                        job_id=job_id,
                        worker_id=self.worker_id,
                        payload={"status": "running", "lease_duration_seconds": lease_duration_seconds},
                    )
                )
                next_heartbeat_at = self.now_fn() + heartbeat_every

            if not process_step():
                return WorkerRunResult(status="processing_complete", lease_lost=False, events=events)
