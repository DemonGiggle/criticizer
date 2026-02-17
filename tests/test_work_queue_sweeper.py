from __future__ import annotations

import sqlite3

from work_queue import WorkQueueStore
from work_queue_sweeper import run_sweeper_loop, sweep_once


def test_sweep_once_requeues_expired_running_jobs(tmp_path):
    db_path = tmp_path / "sweeper.db"
    conn = sqlite3.connect(db_path)
    store = WorkQueueStore(conn)

    expired = store.enqueue("expired", run_at="2000-01-01 00:00:00")
    active = store.enqueue("active", run_at="2000-01-01 00:00:00")
    store.claim(expired, "worker-expired")
    store.claim(active, "worker-active")

    store.conn.execute(
        """
        UPDATE work_queue
        SET lease_expires_at = datetime(now(), '-20 seconds')
        WHERE id = ?
        """,
        (expired,),
    )
    store.conn.execute(
        """
        UPDATE work_queue
        SET lease_expires_at = datetime(now(), '+20 seconds')
        WHERE id = ?
        """,
        (active,),
    )
    store.conn.commit()
    conn.close()

    result = sweep_once(str(db_path))
    assert result.ok is True
    assert result.rows_affected == 1

    verify_conn = sqlite3.connect(db_path)
    verify_store = WorkQueueStore(verify_conn)
    expired_row = verify_store.get_job(expired)
    active_row = verify_store.get_job(active)
    assert expired_row["status"] == "queued"
    assert expired_row["claimed_by"] is None
    assert active_row["status"] == "running"
    assert active_row["claimed_by"] == "worker-active"
    verify_conn.close()


def test_run_sweeper_loop_emits_events_and_sleeps_between_iterations(tmp_path):
    db_path = tmp_path / "sweeper-loop.db"
    conn = sqlite3.connect(db_path)
    store = WorkQueueStore(conn)

    first = store.enqueue("first", run_at="2000-01-01 00:00:00")
    second = store.enqueue("second", run_at="2000-01-01 00:00:00")
    store.claim(first, "w1")
    store.claim(second, "w2")
    store.conn.execute(
        """
        UPDATE work_queue
        SET lease_expires_at = datetime(now(), '-20 seconds')
        WHERE id IN (?, ?)
        """,
        (first, second),
    )
    store.conn.commit()
    conn.close()

    sleeps: list[float] = []
    events: list[dict[str, object]] = []

    report = run_sweeper_loop(
        str(db_path),
        interval_seconds=0.5,
        iterations=2,
        sleep_fn=sleeps.append,
        emit_fn=events.append,
    )

    assert report.iterations == 2
    assert report.total_requeued == 2
    assert sleeps == [0.5]
    assert [event["iteration"] for event in events] == [1, 2]
    assert [event["rows_requeued"] for event in events] == [2, 0]


def test_run_sweeper_loop_validates_inputs(tmp_path):
    db_path = tmp_path / "invalid.db"
    sqlite3.connect(db_path).close()

    try:
        run_sweeper_loop(str(db_path), interval_seconds=0, iterations=1)
        raise AssertionError("expected interval_seconds validation")
    except ValueError as exc:
        assert "interval_seconds" in str(exc)

    try:
        run_sweeper_loop(str(db_path), interval_seconds=1.0, iterations=0)
        raise AssertionError("expected iterations validation")
    except ValueError as exc:
        assert "iterations" in str(exc)
