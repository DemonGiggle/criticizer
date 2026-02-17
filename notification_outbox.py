from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class NotificationProvider(Protocol):
    def send(self, recipient: str, payload: str, *, idempotency_key: str) -> str:
        """Send message and return provider message id."""

    def lookup(self, provider_message_id: str) -> bool:
        """Return True when message id exists at provider side."""


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    row_id: int
    provider_message_id: str | None = None


class NotificationOutboxStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("now", 0, lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changelist_id INTEGER NOT NULL,
                recipient TEXT NOT NULL,
                review_version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent')),
                notification_id TEXT,
                notified_at TEXT,
                created_at TEXT NOT NULL DEFAULT (now()),
                updated_at TEXT NOT NULL DEFAULT (now()),
                UNIQUE(changelist_id, recipient, review_version)
            )
            """
        )
        self.conn.commit()

    @staticmethod
    def idempotency_key(changelist_id: int, recipient: str, review_version: int) -> str:
        raw = f"{changelist_id}:{recipient}:{review_version}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def prepare_rows(
        self,
        *,
        changelist_id: int,
        review_version: int,
        recipients: list[str],
        payload: dict,
    ) -> None:
        payload_json = json.dumps(payload, sort_keys=True)
        for recipient in recipients:
            self.conn.execute(
                """
                INSERT INTO notification_outbox
                    (changelist_id, recipient, review_version, payload, idempotency_key)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(changelist_id, recipient, review_version) DO NOTHING
                """,
                (
                    changelist_id,
                    recipient,
                    review_version,
                    payload_json,
                    self.idempotency_key(changelist_id, recipient, review_version),
                ),
            )
        self.conn.commit()

    def unsent_rows(self, *, changelist_id: int, review_version: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT *
            FROM notification_outbox
            WHERE changelist_id = ?
              AND review_version = ?
              AND notified_at IS NULL
            ORDER BY recipient ASC, id ASC
            """,
            (changelist_id, review_version),
        ).fetchall()

    def deliver_row(self, row_id: int, provider: NotificationProvider) -> DeliveryResult:
        row = self.conn.execute("SELECT * FROM notification_outbox WHERE id = ?", (row_id,)).fetchone()
        assert row is not None

        if row["notified_at"] is not None:
            return DeliveryResult(status="already_sent", row_id=row_id, provider_message_id=row["notification_id"])

        provider_message_id = row["notification_id"]
        if provider_message_id is not None:
            if provider.lookup(provider_message_id):
                self.conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'sent',
                        notified_at = now(),
                        updated_at = now()
                    WHERE id = ?
                    """,
                    (row_id,),
                )
                self.conn.commit()
                return DeliveryResult(status="reconciled", row_id=row_id, provider_message_id=provider_message_id)

        provider_message_id = provider.send(
            row["recipient"],
            row["payload"],
            idempotency_key=row["idempotency_key"],
        )
        self.conn.execute(
            """
            UPDATE notification_outbox
            SET notification_id = ?,
                status = 'sent',
                notified_at = now(),
                updated_at = now()
            WHERE id = ?
            """,
            (provider_message_id, row_id),
        )
        self.conn.commit()
        return DeliveryResult(status="sent", row_id=row_id, provider_message_id=provider_message_id)

    def deliver_pending(
        self,
        *,
        changelist_id: int,
        review_version: int,
        provider: NotificationProvider,
    ) -> list[DeliveryResult]:
        results: list[DeliveryResult] = []
        for row in self.unsent_rows(changelist_id=changelist_id, review_version=review_version):
            results.append(self.deliver_row(row["id"], provider))
        return results

    def get_row(self, row_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM notification_outbox WHERE id = ?", (row_id,)).fetchone()
        assert row is not None
        return row
