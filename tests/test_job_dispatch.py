import sqlite3

from job_dispatch import JobDispatchStore


def make_store() -> JobDispatchStore:
    return JobDispatchStore(sqlite3.connect(":memory:"))


def test_duplicate_idempotency_key_returns_existing_job():
    store = make_store()

    first = store.submit_job(changelist_id=42, review_version=1, idempotency_key="dup-key")
    second = store.submit_job(changelist_id=42, review_version=1, idempotency_key="dup-key")

    assert first.created is True
    assert first.status == "created"
    assert second.created is False
    assert second.status == "duplicate_idempotency"
    assert second.job["id"] == first.job["id"]


def test_same_version_rerun_is_blocked_when_prior_job_succeeded():
    store = make_store()
    first = store.submit_job(changelist_id=55, review_version=2, idempotency_key="cl55-v2")
    store.mark_succeeded(first.job["id"])

    rerun = store.submit_job(
        changelist_id=55,
        review_version=2,
        idempotency_key="cl55-v2-rerun",
        rerun_requested=True,
    )

    assert rerun.created is False
    assert rerun.status == "already_succeeded_same_version"
    assert rerun.job["id"] == first.job["id"]


def test_higher_version_rerun_creates_new_attempt_and_uses_new_outbox_keys():
    store = make_store()
    v1 = store.submit_job(changelist_id=77, review_version=1, idempotency_key="cl77-v1")
    store.mark_succeeded(v1.job["id"])

    blocked_without_rerun_flag = store.submit_job(
        changelist_id=77,
        review_version=2,
        idempotency_key="cl77-v2",
    )
    assert blocked_without_rerun_flag.created is False
    assert blocked_without_rerun_flag.status == "rerun_required"

    v2 = store.submit_job(
        changelist_id=77,
        review_version=2,
        idempotency_key="cl77-v2-rerun",
        rerun_requested=True,
    )
    assert v2.created is True
    assert v2.status == "created"
    assert v2.job["changelist_id"] == v1.job["changelist_id"]
    assert v2.job["id"] != v1.job["id"]

    store.prepare_notifications(job_id=v1.job["id"], recipients=["a@example.com"], payload={"ok": True})
    store.prepare_notifications(job_id=v2.job["id"], recipients=["a@example.com"], payload={"ok": True})

    v1_rows = store.outbox.unsent_rows(changelist_id=77, review_version=1)
    v2_rows = store.outbox.unsent_rows(changelist_id=77, review_version=2)

    assert len(v1_rows) == 1
    assert len(v2_rows) == 1
    assert v1_rows[0]["idempotency_key"] != v2_rows[0]["idempotency_key"]
