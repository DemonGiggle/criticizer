from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from work_queue import MutationResult, WorkQueueStore


@dataclass(frozen=True)
class SweeperReport:
    iterations: int
    total_requeued: int


def sweep_once(db_path: str) -> MutationResult:
    conn = sqlite3.connect(db_path)
    try:
        store = WorkQueueStore(conn)
        return store.requeue_expired_running()
    finally:
        conn.close()


def run_sweeper_loop(
    db_path: str,
    *,
    interval_seconds: float,
    iterations: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    emit_fn: Callable[[dict[str, object]], None] | None = None,
) -> SweeperReport:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    if iterations is not None and iterations <= 0:
        raise ValueError("iterations must be > 0 when provided")

    completed_iterations = 0
    total_requeued = 0

    while iterations is None or completed_iterations < iterations:
        result = sweep_once(db_path)
        completed_iterations += 1
        total_requeued += result.rows_affected

        if emit_fn is not None:
            emit_fn(
                {
                    "code": "work_queue_sweep",
                    "ok": result.ok,
                    "rows_requeued": result.rows_affected,
                    "iteration": completed_iterations,
                }
            )

        if iterations is None or completed_iterations < iterations:
            sleep_fn(interval_seconds)

    return SweeperReport(iterations=completed_iterations, total_requeued=total_requeued)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Periodic sweeper for expired work_queue leases")
    parser.add_argument("--db-path", required=True, help="Path to SQLite database file")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=5.0,
        help="Seconds to sleep between sweeps (default: 5.0)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Run a fixed number of iterations (default: run forever)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    def emit(event: dict[str, object]) -> None:
        print(json.dumps(event, sort_keys=True))

    report = run_sweeper_loop(
        args.db_path,
        interval_seconds=args.interval_seconds,
        iterations=args.iterations,
        emit_fn=emit,
    )
    print(
        json.dumps(
            {
                "code": "work_queue_sweeper_complete",
                "iterations": report.iterations,
                "total_requeued": report.total_requeued,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
