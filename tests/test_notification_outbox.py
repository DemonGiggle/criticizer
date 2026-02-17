import sqlite3

from notification_outbox import NotificationOutboxStore


class FakeProvider:
    def __init__(self):
        self.send_calls: list[tuple[str, str, str]] = []
        self.sent_by_idempotency: dict[str, str] = {}
        self.lookup_calls: list[str] = []
        self.lookup_existing: set[str] = set()

    def send(self, recipient: str, payload: str, *, idempotency_key: str) -> str:
        self.send_calls.append((recipient, payload, idempotency_key))
        if idempotency_key in self.sent_by_idempotency:
            return self.sent_by_idempotency[idempotency_key]
        message_id = f"msg-{len(self.sent_by_idempotency) + 1}"
        self.sent_by_idempotency[idempotency_key] = message_id
        self.lookup_existing.add(message_id)
        return message_id

    def lookup(self, provider_message_id: str) -> bool:
        self.lookup_calls.append(provider_message_id)
        return provider_message_id in self.lookup_existing


def make_store() -> NotificationOutboxStore:
    return NotificationOutboxStore(sqlite3.connect(":memory:"))


def test_prepare_rows_enforces_unique_outbox_key():
    store = make_store()

    store.prepare_rows(
        changelist_id=10,
        review_version=2,
        recipients=["a@example.com", "a@example.com"],
        payload={"summary": "ready"},
    )

    rows = store.unsent_rows(changelist_id=10, review_version=2)
    assert len(rows) == 1


def test_deliver_pending_builds_rows_and_skips_already_notified():
    store = make_store()
    provider = FakeProvider()
    store.prepare_rows(
        changelist_id=1,
        review_version=1,
        recipients=["a@example.com", "b@example.com"],
        payload={"body": "hello"},
    )

    first = store.deliver_pending(changelist_id=1, review_version=1, provider=provider)
    assert [r.status for r in first] == ["sent", "sent"]
    assert len(provider.send_calls) == 2

    second = store.deliver_pending(changelist_id=1, review_version=1, provider=provider)
    assert second == []
    assert len(provider.send_calls) == 2


def test_retry_with_notified_at_does_not_resend():
    store = make_store()
    provider = FakeProvider()
    store.prepare_rows(
        changelist_id=3,
        review_version=1,
        recipients=["x@example.com"],
        payload={"body": "ping"},
    )
    row = store.unsent_rows(changelist_id=3, review_version=1)[0]

    delivered = store.deliver_row(row["id"], provider)
    assert delivered.status == "sent"
    send_count = len(provider.send_calls)

    replay = store.deliver_row(row["id"], provider)
    assert replay.status == "already_sent"
    assert len(provider.send_calls) == send_count


def test_retry_crash_state_notification_id_present_reconciles_without_resend():
    store = make_store()
    provider = FakeProvider()
    store.prepare_rows(
        changelist_id=4,
        review_version=7,
        recipients=["x@example.com"],
        payload={"body": "ping"},
    )
    row = store.unsent_rows(changelist_id=4, review_version=7)[0]
    fake_message_id = "msg-preexisting"
    provider.lookup_existing.add(fake_message_id)

    # Simulate crash after recording provider id but before notified_at/status update.
    store.conn.execute(
        "UPDATE notification_outbox SET notification_id = ? WHERE id = ?",
        (fake_message_id, row["id"]),
    )
    store.conn.commit()

    result = store.deliver_row(row["id"], provider)
    assert result.status == "reconciled"
    assert provider.lookup_calls == [fake_message_id]
    assert provider.send_calls == []

    persisted = store.get_row(row["id"])
    assert persisted["status"] == "sent"
    assert persisted["notified_at"] is not None
    assert persisted["notification_id"] == fake_message_id


def test_retry_crash_state_lookup_miss_permits_safe_resend():
    store = make_store()
    provider = FakeProvider()
    store.prepare_rows(
        changelist_id=5,
        review_version=9,
        recipients=["x@example.com"],
        payload={"body": "ping"},
    )
    row = store.unsent_rows(changelist_id=5, review_version=9)[0]
    stale_message_id = "msg-missing"
    store.conn.execute(
        "UPDATE notification_outbox SET notification_id = ? WHERE id = ?",
        (stale_message_id, row["id"]),
    )
    store.conn.commit()

    result = store.deliver_row(row["id"], provider)
    assert result.status == "sent"
    assert provider.lookup_calls == [stale_message_id]
    assert len(provider.send_calls) == 1

    persisted = store.get_row(row["id"])
    assert persisted["notified_at"] is not None
    assert persisted["notification_id"] == result.provider_message_id


def test_idempotency_key_is_deterministic_from_tuple():
    key1 = NotificationOutboxStore.idempotency_key(123, "r@example.com", 4)
    key2 = NotificationOutboxStore.idempotency_key(123, "r@example.com", 4)
    key3 = NotificationOutboxStore.idempotency_key(124, "r@example.com", 4)

    assert key1 == key2
    assert key1 != key3
