import sqlite3
import threading
import time

from work_queue import WorkQueueStore, WorkerRuntime


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


def test_requeue_expired_running_is_idempotent():
    store = make_store()
    expired = store.enqueue("expired", run_at="2000-01-01 00:00:00")
    active = store.enqueue("active", run_at="2000-01-01 00:00:00")

    store.claim(expired, "worker-expired")
    store.claim(active, "worker-active")
    store.conn.execute(
        """
        UPDATE work_queue
        SET lease_expires_at = datetime(now(), '-10 seconds')
        WHERE id = ?
        """,
        (expired,),
    )
    store.conn.commit()

    first = store.requeue_expired_running()
    assert first.ok is True
    assert first.rows_affected == 1

    expired_job = store.get_job(expired)
    assert expired_job["status"] == "queued"
    assert expired_job["claimed_by"] is None
    assert expired_job["lease_expires_at"] is None

    active_job = store.get_job(active)
    assert active_job["status"] == "running"
    assert active_job["claimed_by"] == "worker-active"

    second = store.requeue_expired_running()
    assert second.ok is True
    assert second.rows_affected == 0


def test_requeue_and_claim_next_are_concurrency_safe(tmp_path):
    db_path = tmp_path / "queue-requeue.db"
    conn1 = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    conn2 = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    store1 = WorkQueueStore(conn1)
    store2 = WorkQueueStore(conn2)
    job_id = store1.enqueue("expired-shared", run_at="2000-01-01 00:00:00")
    store1.claim(job_id, "owner")
    store1.conn.execute(
        """
        UPDATE work_queue
        SET lease_expires_at = datetime(now(), '-30 seconds')
        WHERE id = ?
        """,
        (job_id,),
    )
    store1.conn.commit()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def sweeper() -> None:
        barrier.wait()
        results["requeue"] = store1.requeue_expired_running().rows_affected

    def claimer() -> None:
        barrier.wait()
        row = store2.claim_next("claimer", lease_duration_seconds=20)
        results["claim_id"] = row["id"] if row else None

    t1 = threading.Thread(target=sweeper)
    t2 = threading.Thread(target=claimer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["requeue"] in {0, 1}
    assert results["claim_id"] == job_id

    job = store1.get_job(job_id)
    assert job["status"] == "running"
    assert job["claimed_by"] == "claimer"



def test_runtime_periodically_renews_lease_while_processing():
    store = make_store()
    job_id = store.enqueue("heartbeat-job")
    store.claim(job_id, "worker-1")

    clock = {"now": 0.0}

    def now_fn() -> float:
        return clock["now"]

    runtime = WorkerRuntime(store, "worker-1", now_fn=now_fn)

    steps = {"count": 0}

    def process_step() -> bool:
        steps["count"] += 1
        clock["now"] += 1.0
        return steps["count"] < 5

    result = runtime.process_running_job(job_id, process_step, lease_duration_seconds=3)
    assert result.status == "processing_complete"
    assert result.lease_lost is False
    heartbeat_events = [event for event in result.events if event.type == "heartbeat_renewed"]
    assert len(heartbeat_events) >= 2
    assert all(event.payload["status"] == "running" for event in heartbeat_events)


def test_runtime_stops_immediately_and_emits_event_when_lease_is_lost():
    store = make_store()
    job_id = store.enqueue("lost-lease-job")
    store.claim(job_id, "owner")

    clock = {"now": 0.0}

    def now_fn() -> float:
        return clock["now"]

    runtime = WorkerRuntime(store, "owner", now_fn=now_fn)

    steps = {"count": 0}

    def process_step() -> bool:
        steps["count"] += 1
        if steps["count"] == 1:
            store.conn.execute(
                "UPDATE work_queue SET claimed_by = 'other-worker' WHERE id = ?",
                (job_id,),
            )
            store.conn.commit()
        clock["now"] += 1.0
        return True

    result = runtime.process_running_job(job_id, process_step, lease_duration_seconds=3)

    assert result.status == "lease_lost"
    assert result.lease_lost is True
    assert runtime.lease_lost is True
    assert steps["count"] == 1
    assert len(result.events) == 1
    lease_lost_event = result.events[0]
    assert lease_lost_event.type == "lease_lost"
    assert lease_lost_event.payload["status"] == "lease_lost"
    assert lease_lost_event.payload["diagnostics"]["code"] == "not_owner"


def test_claim_next_enforces_global_max_active_running_capacity():
    store = make_store()
    max_running = 2

    for idx in range(4):
        store.enqueue(f"job-{idx}", run_at="2000-01-01 00:00:00")

    first = store.claim_next("worker-1", max_active_running=max_running)
    second = store.claim_next("worker-2", max_active_running=max_running)
    blocked = store.claim_next("worker-3", max_active_running=max_running)

    assert first is not None
    assert second is not None
    assert blocked is None

    active_non_expired = store.conn.execute(
        """
        SELECT COUNT(*)
        FROM work_queue
        WHERE status = 'running'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at > now()
        """
    ).fetchone()[0]
    assert active_non_expired == max_running


def test_claim_next_reclaims_expired_running_before_capacity_check():
    store = make_store()
    max_running = 1

    expired = store.enqueue("expired", run_at="2000-01-01 00:00:00")
    queued = store.enqueue("queued", run_at="2000-01-01 00:00:00")

    claimed = store.claim_next("worker-1", max_active_running=max_running)
    assert claimed is not None
    assert claimed["id"] == expired

    store.conn.execute(
        """
        UPDATE work_queue
        SET lease_expires_at = datetime(now(), '-30 seconds')
        WHERE id = ?
        """,
        (expired,),
    )
    store.conn.commit()

    next_claim = store.claim_next("worker-2", max_active_running=max_running)
    assert next_claim is not None
    assert next_claim["id"] == expired

    reclaimed = store.get_job(expired)
    assert reclaimed["status"] == "running"
    assert reclaimed["claimed_by"] == "worker-2"

    still_queued = store.get_job(queued)
    assert still_queued["status"] == "queued"
