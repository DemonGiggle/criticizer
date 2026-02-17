import json
import sqlite3
import subprocess

from change_ingest import ChangeFetcher, ChangeIngestService
from job_dispatch import JobDispatchStore
from notification_outbox import NotificationOutboxStore
from request_validation import validate_and_reconcile_review_result
from work_queue import WorkQueueStore


class FakeRunner:
    def __init__(self, *, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.stdout, stderr="")


class FakeProvider:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def send(self, recipient: str, payload: str, *, idempotency_key: str) -> str:
        self.sent.append((recipient, payload, idempotency_key))
        return f"msg-{len(self.sent)}"

    def lookup(self, provider_message_id: str) -> bool:
        return False


def _make_pipeline() -> tuple[ChangeIngestService, JobDispatchStore, WorkQueueStore, NotificationOutboxStore]:
    conn = sqlite3.connect(":memory:")
    queue = WorkQueueStore(conn)
    dispatch = JobDispatchStore(conn)
    fetcher = ChangeFetcher(
        allowlist_prefixes=["//depot/project/..."],
        runner=FakeRunner(stdout="... depotFile //depot/project/src/main.py\n"),
    )
    ingest = ChangeIngestService(fetcher=fetcher, job_dispatch=dispatch, queue=queue)
    return ingest, dispatch, queue, dispatch.outbox


def test_end_to_end_pipeline_ingest_to_outbox_to_finalize():
    ingest, dispatch, queue, outbox = _make_pipeline()

    ingest_result = ingest.ingest_change(
        changelist_id=4242,
        review_version=1,
        idempotency_key="cl4242-v1",
        priority=5,
    )
    assert ingest_result.status == "enqueued"
    assert ingest_result.queue_id is not None
    assert ingest_result.job_id is not None

    claimed = queue.claim_next("worker-e2e", lease_duration_seconds=30)
    assert claimed is not None
    payload = json.loads(claimed["payload"])
    assert payload["job_id"] == ingest_result.job_id

    review_outcome = validate_and_reconcile_review_result(
        json.dumps(
            {
                "schema_version": "1.0",
                "prompt_version": "1.0.0",
                "findings": [
                    {
                        "id": "f-1",
                        "severity": "high",
                        "category": "correctness",
                        "title": "Incorrect branch condition",
                        "file": "//depot/project/src/main.py",
                        "line": 12,
                        "message": "Condition can never be true.",
                    }
                ],
            }
        ),
        changed_files=payload["files"],
        correlation_id="corr-e2e-1",
    )

    assert review_outcome.rejected is False
    assert len(review_outcome.review_result["findings"]) == 1

    dispatch.mark_succeeded(ingest_result.job_id)
    dispatch.prepare_notifications(
        job_id=ingest_result.job_id,
        recipients=["reviewer@example.com"],
        payload={"summary": "1 finding", "findings": review_outcome.review_result["findings"]},
    )

    provider = FakeProvider()
    deliveries = outbox.deliver_pending(changelist_id=4242, review_version=1, provider=provider)
    assert [result.status for result in deliveries] == ["sent"]

    complete_result = queue.complete(claimed["id"], "worker-e2e")
    assert complete_result.ok is True

    queue_row = queue.get_job(claimed["id"])
    job_row = dispatch.get_job(ingest_result.job_id)
    outbox_rows = outbox.unsent_rows(changelist_id=4242, review_version=1)

    assert queue_row["status"] == "completed"
    assert job_row["status"] == "succeeded"
    assert outbox_rows == []
