import sqlite3
import time

from work_queue import WorkQueueStore


def make_store() -> WorkQueueStore:
    conn = sqlite3.connect(":memory:")
    return WorkQueueStore(conn)


def test_valid_transitions_succeed_and_update_timestamps():
    store = make_store()
    job_id = store.enqueue("hello")
    first = store.get_job(job_id)

    time.sleep(1)
    claim_result = store.claim(job_id, "worker-1")
    assert claim_result.ok is True

    running = store.get_job(job_id)
    assert running["status"] == "running"
    assert running["updated_at"] > first["updated_at"]

    time.sleep(1)
    hb = store.heartbeat(job_id, "worker-1")
    assert hb.ok is True
    after_hb = store.get_job(job_id)
    assert after_hb["updated_at"] > running["updated_at"]

    time.sleep(1)
    done = store.complete(job_id, "worker-1")
    assert done.ok is True
    completed = store.get_job(job_id)
    assert completed["status"] == "completed"
    assert completed["claimed_by"] is None
    assert completed["updated_at"] > after_hb["updated_at"]


def test_invalid_transitions_are_blocked_with_diagnostics():
    store = make_store()
    job_id = store.enqueue("hello")

    # queued -> completed is invalid
    result = store.complete(job_id, "worker-1")
    assert result.ok is False
    assert result.rows_affected == 0
    assert result.diagnostics["code"] == "invalid_transition"

    # failed -> running is invalid through guarded claim
    store.claim(job_id, "worker-1")
    store.fail(job_id, "worker-1")
    claim_again = store.claim(job_id, "worker-2")
    assert claim_again.ok is False
    assert claim_again.diagnostics["code"] == "invalid_transition"


def test_non_owner_finalize_and_heartbeat_update_zero_rows():
    store = make_store()
    job_id = store.enqueue("hello")
    store.claim(job_id, "owner")

    hb = store.heartbeat(job_id, "other-worker")
    assert hb.ok is False
    assert hb.rows_affected == 0
    assert hb.diagnostics["code"] == "not_owner"

    fin = store.fail(job_id, "other-worker")
    assert fin.ok is False
    assert fin.rows_affected == 0
    assert fin.diagnostics["code"] == "not_owner"

    job = store.get_job(job_id)
    assert job["status"] == "running"
    assert job["claimed_by"] == "owner"
