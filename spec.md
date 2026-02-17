# Criticizer Specification

## 9. Security Considerations

This section defines implementation-level security requirements. These requirements are normative and use **MUST**, **SHOULD**, and **MAY** language.

### 9.1 Depot path allow-list policy and `ChangeFetcher` enforcement

1. The service **MUST** maintain a canonical allow-list of depot path prefixes (for example: `//depot/projectA/...`, `//depot/libs/security/...`).
2. The allow-list **MUST** be loaded from configuration at startup and validated for:
   - Non-empty entries.
   - Canonical Perforce depot format beginning with `//`.
   - No wildcard broadening beyond approved scope.
3. `ChangeFetcher` is the enforcement point for all changelist retrieval and file expansion operations. It **MUST**:
   - Reject any user- or event-supplied path outside the allow-list before issuing `p4` commands.
   - Resolve and normalize candidate paths prior to comparison.
   - Apply allow-list checks both at changelist query time and again at per-file fetch time (defense-in-depth).
4. Violations **MUST** fail closed:
   - No partial fetches for denied paths.
   - A structured security event log is emitted with denied path and reason.
5. Allow-list changes **MUST** be auditable (who changed, when, and what changed).

### 9.2 Safe subprocess invocation for `p4`

1. `p4` commands **MUST** be executed with argumentized subprocess APIs (e.g., `execve`/`subprocess.run([...], shell=False)`).
2. Shell interpolation is prohibited:
   - The implementation **MUST NOT** use `shell=True`, `os.system`, backticks, or equivalent shell-mediated execution.
   - Untrusted input **MUST NOT** be concatenated into command strings.
3. Command wrappers **MUST**:
   - Provide a fixed executable path or trusted resolver for `p4`.
   - Pass each argument as an individual token.
   - Apply explicit timeouts and non-zero exit handling.
4. Logs for command execution **MUST** include sanitized argument metadata only (never raw secrets or untrusted payloads).

### 9.3 Secret handling for SMTP and LLM credentials

1. SMTP and LLM credentials **MUST** come from approved secret sources only:
   - Environment variables injected by runtime secret managers, or
   - Dedicated secret manager integrations.
2. Credentials **MUST NOT** be hardcoded in source, default config files, or tests.
3. Logging and telemetry **MUST** mask secrets:
   - Full tokens/passwords are never emitted.
   - Partial previews (if needed for debugging) are limited to fixed short prefixes/suffixes and marked redacted.
4. In-memory handling **SHOULD** minimize lifetime and copying of secret values.
5. Rotation guidance:
   - Credentials **SHOULD** be rotated at least every 90 days or per provider policy, whichever is stricter.
   - Emergency rotation **MUST** be supported for suspected compromise.
   - Rollout **SHOULD** support dual-key overlap to avoid downtime.

### 9.4 Artifact retention and access controls

1. Review artifacts (diff snapshots, prompt payloads, model responses, email payload metadata) **MUST** have explicit retention TTLs:
   - Raw artifacts containing sensitive content: default TTL ≤ 7 days.
   - Redacted artifacts used for analytics/debugging: default TTL ≤ 30 days.
2. Stored artifacts **MUST** be encrypted at rest using platform-managed encryption keys at minimum; customer-managed keys are recommended where available.
3. Access control requirements:
   - Least-privilege RBAC for read/write/delete operations.
   - Access to raw artifacts restricted to authorized operational roles only.
   - Access attempts and reads **MUST** be audit-logged.
4. Storage segregation:
   - Raw and redacted artifacts **MUST** be stored in separate logical namespaces/buckets.
   - LLM-bound data paths **MUST** reference redacted artifacts only.
5. TTL enforcement **MUST** include automatic deletion jobs and periodic verification.

### 9.5 Redaction patterns and required tests before LLM submission

1. All content destined for LLM submission **MUST** pass through a deterministic redaction pipeline.
2. At minimum, the redaction pipeline **MUST** detect and redact:
   - API keys and bearer tokens (generic high-entropy token formats and known prefixes).
   - Password assignments in config/code snippets.
   - Private keys and PEM blocks.
   - SMTP credentials and connection URIs containing embedded auth.
   - Email addresses when policy requires de-identification.
   - Internal hostnames/IPs if marked confidential by policy.
3. Redaction output **MUST** preserve enough structure for review usefulness while removing recoverable secret material.
4. Required test coverage (must run in CI):
   - Positive tests for each pattern class above with representative samples.
   - Negative tests to ensure non-sensitive text is not over-redacted.
   - Boundary tests for multiline secrets (e.g., PEM) and truncated tokens.
   - Regression tests for previously reported leakage cases.
5. A pre-submit guard **MUST** block LLM requests when redaction fails, is bypassed, or produces parser errors.
# Criticizer Processing Specification

## 8. Error Handling

This section defines failure classification, retry behavior, attempt budgeting, dead-letter payload shape, and operator recovery procedures.

### 8.1 Failure Classification

All failures MUST be classified as either **retryable** (automatic retries allowed) or **non-retryable** (fail immediately to dead-letter queue).

| Failure category | Examples | Classification | Notes |
| --- | --- | --- | --- |
| Network transient | DNS timeout, socket timeout, TCP reset, 502/503/504 from upstream | Retryable | Use exponential backoff with jitter. |
| Rate limiting / quota throttling | HTTP 429, provider `rate_limit_exceeded`, temporary quota windows | Retryable | Honor upstream `Retry-After` header when present; otherwise use backoff policy. |
| Upstream internal service errors | LLM `internal_error`, webhook provider 5xx | Retryable | Retry until attempt budget exhausted. |
| Idempotency or optimistic-lock conflicts | Duplicate write race, version conflict | Retryable | Safe only for idempotent operations. |
| Validation and schema errors | Missing required fields, malformed JSON payload, schema mismatch | Non-retryable | Requires payload/data correction before replay. |
| Authentication/authorization | Invalid API key, expired token without refresh path, permission denied (403) | Non-retryable | Requires credential or policy remediation. |
| Not found / permanent resource state | 404 for immutable input artifact, deleted destination channel | Non-retryable | Retry should not be attempted unless metadata changes. |
| Content policy / hard business-rule reject | Moderation block, explicit deny policy | Non-retryable | Escalate for operator review if unexpected. |
| Configuration/runtime bug | Null dereference, illegal state, invariant violation, bad deploy config | Non-retryable | Dead-letter and page operator; replay only after fix. |

### 8.2 Retry Backoff Policy

For all retryable failures, the system MUST apply the following backoff parameters unless an upstream `Retry-After` value is larger:

- `initial_delay`: **1s**
- `multiplier`: **2.0**
- `max_delay`: **60s**
- `jitter`: **full jitter** (`actual_delay = random(0, min(max_delay, initial_delay * multiplier^(attempt-1)))`)

Additional requirements:

1. Backoff timers MUST be recomputed per attempt (no precomputed static schedule).
2. If `Retry-After` is present, effective delay is `max(calculated_backoff, retry_after)`, capped by an operational ceiling of 5 minutes.
3. Retries MUST stop immediately when a failure is reclassified to non-retryable.

### 8.3 Attempt Budget Model

Attempt budget is **per stage**, not global.

- Stages: `fetch`, `llm`, `notify`
- `max_attempts_per_stage`: **5** (inclusive of first attempt)
- Failure in one stage MUST NOT consume attempts from other stages.
- If a stage exhausts budget, processing transitions to dead-letter with stage-specific metadata.

Rationale: per-stage budgets isolate flaky dependencies and preserve useful work from completed stages.

### 8.4 Dead-Letter Payload Requirements

When processing is dead-lettered, payload MUST include at minimum:

- `error_class`: stable classification identifier (for example `NETWORK_TIMEOUT`, `AUTH_DENIED`, `SCHEMA_INVALID`).
- `last_stack`: most recent stack trace or error chain from the failing stage.
- `sanitized_context`: sanitized operational context (request IDs, stage, attempt counts, upstream response code, truncated payload hashes/IDs).

Required safety constraints:

1. `sanitized_context` MUST exclude secrets, access tokens, API keys, full prompts containing sensitive data, and raw PII.
2. Stack traces MUST be redacted if they contain sensitive literals.
3. Include `first_failure_at`, `last_failure_at`, and `stage` to support replay triage.

### 8.5 Operator Remediation and Replay Workflow

Operators MUST use the following workflow before replaying dead-letter items:

1. **Triage**
   - Confirm `error_class`, failing `stage`, and retry exhaustion details.
   - Determine whether issue is transient, data-related, auth/config-related, or code defect.
2. **Remediate**
   - Transient: verify upstream recovery and capacity.
   - Data: correct source payload or mapping.
   - Auth/config: rotate/fix credentials, permissions, endpoints.
   - Code defect: deploy fix and validate in staging.
3. **Pre-replay validation**
   - Ensure remediation evidence is recorded in ticket/incident.
   - Confirm replay is idempotent or safe for duplicate effects.
4. **Replay**
   - Trigger replay from dead-letter queue using original payload reference and sanitized context.
   - Replay starts at the failed stage (not from workflow start) unless operator explicitly requests full restart.
5. **Post-replay verification**
   - Verify successful completion and downstream delivery.
   - Annotate dead-letter item with resolution notes and close incident.

If replay fails again with the same non-retryable `error_class`, item MUST be re-queued to dead-letter and escalated for engineering review.
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
