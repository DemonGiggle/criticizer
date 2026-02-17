# Criticizer

Criticizer is a Python reference implementation for a review-processing pipeline described in `spec.md`.
It currently focuses on:

- Lease-safe work queue execution (`work_queue.py`)
- Strict `ReviewResult` validation + diagnostics (`request_validation.py`)
- File path reconciliation utilities (`reconciliation.py`)
- Job idempotency + versioned rerun controls (`job_dispatch.py`)
- Notification outbox delivery semantics (`notification_outbox.py`)
- Failure handling / dead-letter replay workflow (`failure_pipeline.py`)
- Secure Perforce change ingestion (`change_ingest.py`)

## Repository layout

- `spec.md` — normative behavior and invariants
- `work_queue.py` — queue storage + lease claims/heartbeats/finalization + worker runtime helpers
- `request_validation.py` — parser/validator for model output with coercion + drop diagnostics
- `reconciliation.py` — canonical repo path normalization and changed-file reconciliation
- `job_dispatch.py` — job creation, idempotency, and rerun gating by `review_version`
- `notification_outbox.py` — deterministic outbox keys and retry-safe delivery sequencing
- `failure_pipeline.py` — non-retryable failure routing to dead-letter and replay orchestration
- `change_ingest.py` — allow-list-enforced changelist fetch + enqueueing
- `work_queue_sweeper.py` — periodic sweeper CLI/loop for requeuing expired leases
- `tests/` — unit coverage for all modules above

## Quick start

### Requirements

- Python 3.10+
- `pytest`

### Run tests

```bash
pytest -q
```

### Run the lease sweeper

```bash
python -m work_queue_sweeper --db-path /path/to/work_queue.db --interval-seconds 5
```

## Current implementation status

Implemented behaviors include:

- Atomic queue claiming with lease assignment, expiry requeue, owner-guarded heartbeat/finalize, and max-active-running capacity checks.
- `ReviewResult` contract checks including schema/prompt version compatibility, finding-level validation, safe coercions, per-finding drops, and machine-readable diagnostics.
- Notification dedupe via `(changelist_id, recipient, review_version)` and deterministic provider idempotency keys.
- Job-level idempotency on `idempotency_key`, plus rerun blocking/allow rules tied to prior succeeded versions.
- Dead-letter storage for non-retryable failures and guarded replay execution with remediation evidence.
- Secure `p4` invocation (`shell=False`, argumentized command, timeout) with depot allow-list enforcement at request and fetched-file stages.

## Notes

- Persistence is SQLite-backed for local determinism and testability.
- This codebase is structured as a spec-aligned prototype: it prioritizes correctness and auditable state transitions over deployment wiring.

## Next reading

- See `TODO.md` for known follow-up work.
- See `AGEMTS.md` for AI-assistant operating notes for this repository.
