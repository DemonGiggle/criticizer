import json
import sqlite3
import subprocess

import pytest

from change_ingest import ChangeFetcher, ChangeIngestService
from job_dispatch import JobDispatchStore
from work_queue import WorkQueueStore


class FakeRunner:
    def __init__(self, *, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.stdout, stderr="")


def make_service(runner: FakeRunner) -> ChangeIngestService:
    conn = sqlite3.connect(":memory:")
    fetcher = ChangeFetcher(allowlist_prefixes=["//depot/project/..."], runner=runner)
    return ChangeIngestService(fetcher=fetcher, job_dispatch=JobDispatchStore(conn), queue=WorkQueueStore(conn))


def test_change_fetcher_uses_safe_argumentized_subprocess_invocation():
    runner = FakeRunner(stdout="... depotFile //depot/project/main.py\n")
    fetcher = ChangeFetcher(allowlist_prefixes=["//depot/project/..."], runner=runner)

    result = fetcher.fetch_change(123)

    assert result["files"] == ["//depot/project/main.py"]
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["cmd"] == ["p4", "-ztag", "describe", "-s", "123"]
    assert call["shell"] is False
    assert call["capture_output"] is True
    assert call["text"] is True


def test_change_fetcher_denies_requested_paths_outside_allowlist():
    runner = FakeRunner(stdout="... depotFile //depot/project/main.py\n")
    fetcher = ChangeFetcher(allowlist_prefixes=["//depot/project/..."], runner=runner)

    with pytest.raises(PermissionError):
        fetcher.fetch_change(456, requested_paths=["//depot/other/secret.txt"])

    assert fetcher.security_events == [{"path": "//depot/other/secret.txt", "reason": "requested_path_not_allowed"}]
    assert runner.calls == []


def test_change_fetcher_denies_fetched_paths_outside_allowlist():
    runner = FakeRunner(stdout="... depotFile //depot/project/main.py\n... depotFile //depot/other/secret.txt\n")
    fetcher = ChangeFetcher(allowlist_prefixes=["//depot/project/..."], runner=runner)

    with pytest.raises(PermissionError):
        fetcher.fetch_change(789)

    assert fetcher.security_events == [{"path": "//depot/other/secret.txt", "reason": "fetched_path_not_allowed"}]


def test_ingest_enqueues_payload_for_new_job():
    runner = FakeRunner(stdout="... depotFile //depot/project/app.py\n")
    service = make_service(runner)

    result = service.ingest_change(
        changelist_id=12,
        review_version=1,
        idempotency_key="cl12-v1",
    )

    assert result.status == "enqueued"
    assert result.job_id is not None
    assert result.queue_id is not None

    queued = service.queue.get_job(result.queue_id)
    payload = json.loads(queued["payload"])
    assert payload["job_id"] == result.job_id
    assert payload["changelist_id"] == 12
    assert payload["review_version"] == 1
    assert payload["files"] == ["//depot/project/app.py"]


def test_ingest_duplicate_request_does_not_enqueue_second_queue_job():
    runner = FakeRunner(stdout="... depotFile //depot/project/app.py\n")
    service = make_service(runner)

    first = service.ingest_change(changelist_id=33, review_version=1, idempotency_key="dup-key")
    second = service.ingest_change(changelist_id=33, review_version=1, idempotency_key="dup-key")

    assert first.status == "enqueued"
    assert second.status == "duplicate_idempotency"
    assert second.queue_id is None

    queue_count = service.queue.conn.execute("SELECT COUNT(*) FROM work_queue").fetchone()[0]
    assert queue_count == 1
