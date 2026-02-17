import sqlite3
import threading
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


def test_claim_next_claims_only_runnable_jobs_by_priority_then_age():
    store = make_store()
    future_job = store.enqueue("future", priority=100, run_at="2999-01-01 00:00:00")
    low_old = store.enqueue("low-old", priority=1, run_at="2000-01-01 00:00:00")
    high = store.enqueue("high", priority=10, run_at="2000-01-01 00:00:00")

    claimed = store.claim_next("worker-a", lease_duration_seconds=45)
    assert claimed is not None
    assert claimed["id"] == high
    assert claimed["status"] == "running"
    assert claimed["claimed_by"] == "worker-a"
    assert claimed["lease_expires_at"] is not None
    assert claimed["started_at"] is not None

    next_claim = store.claim_next("worker-b")
    assert next_claim is not None
    assert next_claim["id"] == low_old

    no_work = store.claim_next("worker-c")
    assert no_work is None

    future = store.get_job(future_job)
    assert future["status"] == "queued"


def test_claim_next_is_concurrency_safe_for_single_job(tmp_path):
    db_path = tmp_path / "queue.db"
    conn1 = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    conn2 = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    store1 = WorkQueueStore(conn1)
    store2 = WorkQueueStore(conn2)
    job_id = store1.enqueue("shared")

    barrier = threading.Barrier(2)
    results: list[tuple[str, int | None]] = []

    def worker_claim(store: WorkQueueStore, worker_id: str) -> None:
        barrier.wait()
        row = store.claim_next(worker_id)
        results.append((worker_id, row["id"] if row else None))

    t1 = threading.Thread(target=worker_claim, args=(store1, "worker-1"))
    t2 = threading.Thread(target=worker_claim, args=(store2, "worker-2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    claimed_ids = [row_id for _, row_id in results if row_id is not None]
    assert claimed_ids == [job_id]

    job = store1.get_job(job_id)
    assert job["status"] == "running"
    assert job["claimed_by"] in {"worker-1", "worker-2"}
