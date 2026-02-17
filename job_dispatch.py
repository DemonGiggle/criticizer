from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from notification_outbox import NotificationOutboxStore


@dataclass(frozen=True)
class JobSubmissionResult:
    status: str
    job: sqlite3.Row
    created: bool


class JobDispatchStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("now", 0, lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        self._ensure_schema()
        self.outbox = NotificationOutboxStore(conn)

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changelist_id INTEGER NOT NULL,
                review_version INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                created_at TEXT NOT NULL DEFAULT (now()),
                updated_at TEXT NOT NULL DEFAULT (now()),
                UNIQUE(idempotency_key)
            )
            """
        )
        self.conn.commit()

    def submit_job(
        self,
        *,
        changelist_id: int,
        review_version: int,
        idempotency_key: str,
        rerun_requested: bool = False,
    ) -> JobSubmissionResult:
        existing_by_key = self._get_by_idempotency_key(idempotency_key)
        if existing_by_key is not None:
            return JobSubmissionResult(status="duplicate_idempotency", job=existing_by_key, created=False)

        prior_success = self.conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE changelist_id = ?
              AND status = 'succeeded'
            ORDER BY review_version DESC, id DESC
            LIMIT 1
            """,
            (changelist_id,),
        ).fetchone()

        if prior_success is not None:
            if review_version == prior_success["review_version"]:
                return JobSubmissionResult(status="already_succeeded_same_version", job=prior_success, created=False)
            if review_version > prior_success["review_version"] and not rerun_requested:
                return JobSubmissionResult(status="rerun_required", job=prior_success, created=False)
            if review_version < prior_success["review_version"]:
                return JobSubmissionResult(status="stale_review_version", job=prior_success, created=False)

        cur = self.conn.execute(
            """
            INSERT INTO jobs (changelist_id, review_version, idempotency_key, status)
            VALUES (?, ?, ?, 'queued')
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (changelist_id, review_version, idempotency_key),
        )
        self.conn.commit()
        existing = self._get_by_idempotency_key(idempotency_key)
        assert existing is not None
        created = cur.rowcount == 1
        return JobSubmissionResult(status="created" if created else "duplicate_idempotency", job=existing, created=created)

    def mark_succeeded(self, job_id: int) -> None:
        self.conn.execute(
            """
            UPDATE jobs
            SET status = 'succeeded',
                updated_at = now()
            WHERE id = ?
            """,
            (job_id,),
        )
        self.conn.commit()

    def prepare_notifications(self, *, job_id: int, recipients: list[str], payload: dict) -> None:
        job = self.get_job(job_id)
        self.outbox.prepare_rows(
            changelist_id=job["changelist_id"],
            review_version=job["review_version"],
            recipients=recipients,
            payload=payload,
        )

    def get_job(self, job_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row is not None
        return row

    def _get_by_idempotency_key(self, idempotency_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
