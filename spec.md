# Specification

## 4.2 WorkQueue

The WorkQueue stores processable jobs and coordinates safe concurrent worker execution.

### 4.2.1 States and transitions
- Valid states: `queued`, `running`, `completed`, `failed`.
- A worker may only start work by **atomically** transitioning a single job from `queued` to `running`.
- `running -> completed|failed` is only valid for the worker that currently owns the lease (`claimed_by`).

### 4.2.2 Lease model
Each row includes lease metadata:
- `claimed_by`: unique worker identifier that currently owns the job lease.
- `lease_expires_at`: UTC timestamp indicating when ownership expires unless renewed.

Lease rules:
- Claiming a job sets `claimed_by` and initializes `lease_expires_at = now() + lease_duration`.
- Workers must heartbeat/renew before expiry (recommended at 1/3 to 1/2 of lease duration).
- Heartbeat is an update guarded by `id` + `claimed_by` + `status='running'` so only the owner can renew.
- If heartbeat fails (0 rows updated), the worker must stop processing and treat the lease as lost.

### 4.2.3 Requeue of expired leases
- Any `running` job with `lease_expires_at <= now()` is considered expired and no longer owned.
- Requeue operation transitions expired rows to `queued`, clears `claimed_by`, and clears or resets `lease_expires_at`.
- Requeue may run in a periodic sweeper and/or inline in claim logic.
- Requeue must be idempotent and safe under concurrency.

### 4.2.4 Crash recovery and worker limits
- If a worker crashes, it stops heartbeating; on lease expiry, the job becomes eligible for requeue and reclaim.
- Recovery is lease-driven (no manual worker tombstone required for correctness).
- Max concurrent workers (`W`) limits active claims globally: at most `W` jobs should be in `running` due to new claims at any point in time.
- Expired `running` rows do not count as active capacity once lease expiry is detected; a sweeper/claim path should requeue promptly so capacity is restored.

### 4.2.5 Claim transaction and isolation guarantees
Claim/update operations require DB semantics that prevent double-claim:
- Claim must execute in a single transaction.
- The selected candidate row must be locked (e.g., `FOR UPDATE SKIP LOCKED` in PostgreSQL).
- Isolation must guarantee that two workers cannot both observe and transition the same row from `queued` to `running`.
- Owner-only updates (`heartbeat`, `complete`, `fail`) must include `WHERE id=? AND claimed_by=? AND status='running'`.
- If DB lacks `SKIP LOCKED`, equivalent mutual-exclusion semantics are required (advisory lock, compare-and-swap token, etc.).

Concrete claim example (PostgreSQL-style pseudo-SQL):

```sql
BEGIN;

WITH candidate AS (
  SELECT id
  FROM work_queue
  WHERE status = 'queued'
    AND run_at <= now()
  ORDER BY priority DESC, created_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE work_queue w
SET status = 'running',
    claimed_by = :worker_id,
    lease_expires_at = now() + interval '30 seconds',
    started_at = now(),
    updated_at = now()
FROM candidate
WHERE w.id = candidate.id
RETURNING w.id, w.payload, w.lease_expires_at;

COMMIT;
```

If `RETURNING` yields no row, no job was claimed.

## 6. Processing Flow

1. **Optional lease cleanup**
   - Requeue expired leases (`running` + `lease_expires_at <= now()`) by setting `status='queued'`, `claimed_by=NULL`, `lease_expires_at=NULL`.

2. **Atomic claim**
   - Worker starts a DB transaction and performs the atomic claim (`queued -> running`) with row locking.
   - If no row is claimed, worker sleeps/backs off and retries.

3. **Process under lease**
   - Worker performs business logic while lease is valid.
   - Worker sends periodic heartbeat renewals (`lease_expires_at = now() + lease_duration`) using owner-guarded updates.

4. **Finalize**
   - On success: owner updates `running -> completed` and clears lease fields.
   - On terminal failure: owner updates `running -> failed` and clears lease fields (or schedules retry policy if applicable).

5. **Lease-loss handling**
   - If heartbeat/finalize update affects 0 rows, worker assumes lease lost (expired or stolen via requeue) and stops side effects requiring ownership.

6. **Crash recovery path**
   - If worker crashes at any point after claim, no heartbeat occurs.
   - After `lease_expires_at`, sweeper/claim path requeues the item; another worker may claim it.
   - Processing logic must be idempotent or guarded by deduplication keys to tolerate at-least-once execution after recovery.

7. **Concurrency bound interaction**
   - Scheduler/worker pool should not exceed configured max concurrent workers.
   - Capacity accounting should treat only non-expired leases as active; expired leases are reclaimed via requeue before or during claim loops.
