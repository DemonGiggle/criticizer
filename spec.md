# Review Notification Processing Spec

## 6. Processing Flow

1. **Job creation and dedupe gate**
   - A new review-processing job MUST be created with a caller-provided `idempotency_key`.
   - The `jobs.idempotency_key` column MUST have a unique constraint.
   - If an insert violates that unique constraint, the service MUST treat it as a duplicate request and return the existing job instead of creating a second job.

2. **Determine processing mode**
   - If the job previously reached `succeeded`, reprocessing is:
     - **Blocked** when the request uses the same `review_version` (no-op; return existing result).
     - **Allowed** only when the caller explicitly requests a **versioned rerun** with a strictly higher `review_version`.
   - A versioned rerun MUST create a new logical attempt tied to the same `changelist_id` but a new `review_version`.

3. **Prepare recipients and outbox keying**
   - Before sending, build recipient list and compute outbox identity key: `(changelist_id, recipient, review_version)`.
   - Persist an outbox/delivery-log row per recipient keyed by that tuple.
   - If a row already exists with `notified_at` populated, skip send for that recipient (already delivered).

4. **Send-and-mark exact sequence (retry-safe)**
   - For each unsent outbox row, execute in this exact order:
     1. Call provider send API using deterministic message payload and provider idempotency token derived from `(changelist_id, recipient, review_version)`.
     2. On provider success, capture provider message id as `notification_id`.
     3. Persist DB update in a single write: set `notification_id`, set `notified_at = now()`, and mark delivery status `sent`.
   - Retries MUST re-read the outbox row first:
     - If `notified_at` is set, do not send again.
     - If `notified_at` is null and `notification_id` is null, safe to attempt send.
     - If `notification_id` exists but `notified_at` is null, treat as "send may have happened" and recover via provider-id lookup before deciding to resend.

5. **Job finalization**
   - Mark job `succeeded` only when all required recipients for the target `review_version` have delivery rows with `notified_at` set.
   - Otherwise keep job `retryable_failed` or `in_progress` depending on retry policy.

## 8. Error Handling

### 8.1 General rules
- All delivery attempts MUST be recorded in an outbox or delivery log keyed by `(changelist_id, recipient, review_version)`.
- Transient provider/network failures SHOULD produce retryable states with exponential backoff.
- Permanent failures (invalid recipient, policy rejection) SHOULD mark recipient delivery row failed and require operator or caller intervention.

### 8.2 Failure matrix

| Scenario | Immediate state | Recovery action | Duplicate-send risk | Required invariant |
|---|---|---|---|---|
| **Email sent but DB write failed** (provider accepted send, then DB update for `notified_at`/`notification_id` failed) | Outbox row still appears unsent or partially updated | On retry, first attempt provider lookup by deterministic key / prior `notification_id`; if found delivered, backfill `notification_id` and `notified_at` without resending | High unless guarded; mitigated by provider idempotency key + lookup-before-resend | Never mark failed permanently until reconciliation attempted |
| **DB write succeeded but send failed** (DB transaction incorrectly marked sent before provider confirmation, or send call fails after optimistic write) | Inconsistent local state indicates sent but provider has no record | Reconcile by provider lookup; if not delivered, clear sent markers (`notified_at`, `notification_id`) and requeue send; this path SHOULD alert because ordering contract was violated | Medium; depends on correction timing | The canonical contract is send first, then mark; any violation must be detectable and repaired |

### 8.3 Ordering contract enforcement
- The system MUST enforce `send` -> `mark sent` ordering in code review and tests.
- If implementation detects a row with `notified_at` set but no provider evidence, it MUST transition that row into a reconciliation workflow before any further sends.

## 10. Idempotency

1. **Job-level idempotency**
   - `idempotency_key` MUST be unique for job creation requests.
   - Duplicate `idempotency_key` submissions MUST return the originally created job record and MUST NOT create a second job.

2. **Delivery-level idempotency**
   - Notification dedupe MUST be enforced by outbox/delivery-log uniqueness on `(changelist_id, recipient, review_version)`.
   - This key defines the unique intent "recipient has been notified for this changelist version".

3. **Send retry behavior**
   - Retries MUST be safe under process crash, network timeout, and worker restart.
   - A retry worker MUST always check persisted delivery state (`notified_at`, `notification_id`) before send.
   - Provider idempotency keys SHOULD be deterministic and derived from the same tuple to prevent duplicate external deliveries.

4. **Reprocessing succeeded jobs**
   - Reprocessing a succeeded job with identical `review_version` is **not allowed** as a new send operation; it should return prior success.
   - Reprocessing is **allowed** as a versioned rerun only with a new `review_version` (e.g., v3 -> v4), which creates new delivery intents under new keys.
