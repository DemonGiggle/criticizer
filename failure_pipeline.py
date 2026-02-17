from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class DeadLetterEntry:
    id: int
    run_id: int
    failed_stage: str
    error_class: str
    status: str
    original_payload_ref: str


@dataclass(frozen=True)
class ReplayPlan:
    dead_letter_id: int
    run_id: int
    restart_stage: str
    full_restart: bool


class FailureHandlingPipeline:
    """Tracks non-retryable failures and controlled replay workflows."""

    def __init__(self, conn: sqlite3.Connection, *, stages: list[str]):
        if not stages:
            raise ValueError("stages must not be empty")
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.stages = stages
        self.conn.create_function("now", 0, lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_ref TEXT NOT NULL,
                current_stage TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running', 'failed', 'completed')),
                created_at TEXT NOT NULL DEFAULT (now()),
                updated_at TEXT NOT NULL DEFAULT (now())
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dead_letter_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                failed_stage TEXT NOT NULL,
                error_class TEXT NOT NULL,
                error_message TEXT,
                error_metadata TEXT NOT NULL,
                original_payload_ref TEXT NOT NULL,
                remediation_evidence TEXT,
                replay_start_stage TEXT,
                replay_count INTEGER NOT NULL DEFAULT 0,
                resolution_notes TEXT,
                status TEXT NOT NULL CHECK (status IN ('open', 'replaying', 'resolved', 'escalated')),
                escalated_at TEXT,
                resolved_at TEXT,
                created_at TEXT NOT NULL DEFAULT (now()),
                updated_at TEXT NOT NULL DEFAULT (now()),
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(id)
            )
            """
        )
        self.conn.commit()

    def create_run(self, payload_ref: str) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO pipeline_runs (payload_ref, current_stage, status)
            VALUES (?, ?, 'running')
            """,
            (payload_ref, self.stages[0]),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_failure(
        self,
        *,
        run_id: int,
        failed_stage: str,
        error_class: str,
        error_message: str,
        error_metadata: dict[str, Any],
        retryable: bool,
        original_payload_ref: str | None = None,
    ) -> DeadLetterEntry | None:
        if failed_stage not in self.stages:
            raise ValueError("unknown failed_stage")

        self.conn.execute(
            """
            UPDATE pipeline_runs
            SET current_stage = ?,
                status = 'failed',
                updated_at = now()
            WHERE id = ?
            """,
            (failed_stage, run_id),
        )

        if retryable:
            self.conn.commit()
            return None

        payload_ref = original_payload_ref or self._get_run(run_id)["payload_ref"]
        cur = self.conn.execute(
            """
            INSERT INTO dead_letter_entries
                (run_id, failed_stage, error_class, error_message, error_metadata, original_payload_ref, status)
            VALUES (?, ?, ?, ?, ?, ?, 'open')
            """,
            (run_id, failed_stage, error_class, error_message, json.dumps(error_metadata, sort_keys=True), payload_ref),
        )
        self.conn.commit()
        row = self.get_dead_letter(int(cur.lastrowid))
        return DeadLetterEntry(
            id=row["id"],
            run_id=row["run_id"],
            failed_stage=row["failed_stage"],
            error_class=row["error_class"],
            status=row["status"],
            original_payload_ref=row["original_payload_ref"],
        )

    def record_remediation_evidence(self, dead_letter_id: int, *, operator_id: str, evidence: str) -> None:
        self.conn.execute(
            """
            UPDATE dead_letter_entries
            SET remediation_evidence = ?,
                updated_at = now()
            WHERE id = ?
            """,
            (f"operator={operator_id}; evidence={evidence}", dead_letter_id),
        )
        self.conn.commit()

    def start_replay(self, dead_letter_id: int, *, full_restart: bool = False) -> ReplayPlan:
        dead_letter = self.get_dead_letter(dead_letter_id)
        if dead_letter["remediation_evidence"] is None:
            raise ValueError("remediation evidence required before replay")

        restart_stage = self.stages[0] if full_restart else dead_letter["failed_stage"]
        self.conn.execute(
            """
            UPDATE dead_letter_entries
            SET status = 'replaying',
                replay_start_stage = ?,
                replay_count = replay_count + 1,
                updated_at = now()
            WHERE id = ?
            """,
            (restart_stage, dead_letter_id),
        )
        self.conn.execute(
            """
            UPDATE pipeline_runs
            SET current_stage = ?,
                status = 'running',
                updated_at = now()
            WHERE id = ?
            """,
            (restart_stage, dead_letter["run_id"]),
        )
        self.conn.commit()
        return ReplayPlan(
            dead_letter_id=dead_letter_id,
            run_id=dead_letter["run_id"],
            restart_stage=restart_stage,
            full_restart=full_restart,
        )

    def complete_replay(self, dead_letter_id: int, *, completed_stages: list[str], resolution_notes: str) -> None:
        dead_letter = self.get_dead_letter(dead_letter_id)
        restart_stage = dead_letter["replay_start_stage"] or dead_letter["failed_stage"]
        expected = self.stages[self.stages.index(restart_stage) :]
        if completed_stages != expected:
            raise ValueError("downstream completion verification failed")

        self.conn.execute(
            """
            UPDATE pipeline_runs
            SET current_stage = ?,
                status = 'completed',
                updated_at = now()
            WHERE id = ?
            """,
            (expected[-1], dead_letter["run_id"]),
        )
        self.conn.execute(
            """
            UPDATE dead_letter_entries
            SET status = 'resolved',
                resolution_notes = ?,
                resolved_at = now(),
                updated_at = now()
            WHERE id = ?
            """,
            (resolution_notes, dead_letter_id),
        )
        self.conn.commit()

    def fail_replay(
        self,
        dead_letter_id: int,
        *,
        error_class: str,
        error_message: str,
        error_metadata: dict[str, Any],
        retryable: bool,
    ) -> None:
        dead_letter = self.get_dead_letter(dead_letter_id)
        run_id = dead_letter["run_id"]
        escalated = (not retryable) and error_class == dead_letter["error_class"]
        status = "escalated" if escalated else "open"
        self.conn.execute(
            """
            UPDATE pipeline_runs
            SET status = 'failed',
                current_stage = ?,
                updated_at = now()
            WHERE id = ?
            """,
            (dead_letter["failed_stage"], run_id),
        )
        self.conn.execute(
            """
            UPDATE dead_letter_entries
            SET status = ?,
                error_class = ?,
                error_message = ?,
                error_metadata = ?,
                escalated_at = CASE WHEN ? THEN now() ELSE escalated_at END,
                updated_at = now()
            WHERE id = ?
            """,
            (status, error_class, error_message, json.dumps(error_metadata, sort_keys=True), 1 if escalated else 0, dead_letter_id),
        )
        self.conn.commit()

    def get_dead_letter(self, dead_letter_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM dead_letter_entries WHERE id = ?", (dead_letter_id,)).fetchone()
        assert row is not None
        return row

    def _get_run(self, run_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
        assert row is not None
        return row
