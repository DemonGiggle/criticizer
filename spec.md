# Specification

This document uses normative language from RFC 2119/8174: **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**.

## Document guardrails

### Global invariants
- All state transitions MUST be monotonic and auditable via `updated_at` (or equivalent event log).
- Any operation that can be retried MUST be safe under at-least-once execution.
- Ownership-sensitive writes MUST include an owner/lease predicate to prevent stale-worker mutation.
- Validation and reconciliation MUST emit machine-readable diagnostics for every dropped or coerced field.

### Time and clock assumptions
- All persisted timestamps MUST be UTC.
- `now()` references MUST use the database clock (not application host clock) during transactional updates.
- Implementations SHOULD define an operational skew budget (recommended <= 2 seconds) between app hosts and DB.

## WorkQueue

The WorkQueue stores processable jobs and coordinates safe concurrent worker execution.

### States and transitions
- Valid states: `queued`, `running`, `completed`, `failed`.
- A worker may only start work by **atomically** transitioning a single job from `queued` to `running`.
- `running -> completed|failed` is only valid for the worker that currently owns the lease (`claimed_by`).

### Lease model
Each row includes lease metadata:
- `claimed_by`: unique worker identifier that currently owns the job lease.
- `lease_expires_at`: UTC timestamp indicating when ownership expires unless renewed.

Lease rules:
- Claiming a job sets `claimed_by` and initializes `lease_expires_at = now() + lease_duration`.
- Workers must heartbeat/renew before expiry (recommended at 1/3 to 1/2 of lease duration).
- Heartbeat is an update guarded by `id` + `claimed_by` + `status='running'` so only the owner can renew.
- If heartbeat fails (0 rows updated), the worker must stop processing and treat the lease as lost.

### Requeue of expired leases
- Any `running` job with `lease_expires_at <= now()` is considered expired and no longer owned.
- Requeue operation transitions expired rows to `queued`, clears `claimed_by`, and clears or resets `lease_expires_at`.
- Requeue may run in a periodic sweeper and/or inline in claim logic.
- Requeue must be idempotent and safe under concurrency.

### Crash recovery and worker limits
- If a worker crashes, it stops heartbeating; on lease expiry, the job becomes eligible for requeue and reclaim.
- Recovery is lease-driven (no manual worker tombstone required for correctness).
- Max concurrent workers (`W`) limits active claims globally: at most `W` jobs should be in `running` due to new claims at any point in time.
- Expired `running` rows do not count as active capacity once lease expiry is detected; a sweeper/claim path should requeue promptly so capacity is restored.

### Claim transaction and isolation guarantees
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

## WorkQueue processing flow

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

## Output contract

### ReviewResult

`ReviewResult` is the canonical top-level response object emitted by the reviewer.

#### Canonical JSON schema

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "prompt_version", "findings"],
  "properties": {
    "schema_version": {
      "type": "string",
      "pattern": "^[0-9]+\\.[0-9]+$"
    },
    "prompt_version": {
      "type": "string",
      "pattern": "^[0-9]+\\.[0-9]+(\\.[0-9]+)?$"
    },
    "summary": {
      "type": "string"
    },
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "severity", "category", "title", "file", "line", "message"],
        "properties": {
          "id": { "type": "string", "minLength": 1 },
          "severity": {
            "type": "string",
            "enum": ["critical", "high", "medium", "low", "info"]
          },
          "category": {
            "type": "string",
            "enum": ["correctness", "security", "performance", "reliability", "maintainability", "style", "test"]
          },
          "title": { "type": "string", "minLength": 1 },
          "file": { "type": "string", "minLength": 1 },
          "line": { "type": "integer", "minimum": 1 },
          "end_line": { "type": "integer", "minimum": 1 },
          "message": { "type": "string", "minLength": 1 },
          "suggestion": { "type": "string" },
          "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"]
          },
          "rule_id": { "type": "string" }
        }
      }
    },
    "meta": {
      "type": "object",
      "additionalProperties": true
    }
  }
}
```

Required fields:
- Top level: `schema_version`, `prompt_version`, `findings`
- Per finding: `id`, `severity`, `category`, `title`, `file`, `line`, `message`

Optional fields:
- Top level: `summary`, `meta`
- Per finding: `end_line`, `suggestion`, `confidence`, `rule_id`

#### Validation behavior

Validation MUST execute in this order:
1. Parse JSON.
2. Validate top-level schema.
3. Validate each finding.
4. Apply repository-aware reconciliation checks.

Handling invalid values:
- **Reject response** when required top-level fields are missing/invalid, `findings` is not an array, or enum values outside allowed sets appear at top level.
- **Coerce** only for safe scalar normalization:
  - Trim surrounding whitespace for string fields.
  - Normalize path separators in `file` to `/`.
  - Convert integral numeric strings for `line`/`end_line` to integers.
- **Drop finding** (do not fail whole response) when the finding has:
  - Missing required finding fields.
  - Invalid finding enum values (`severity`, `category`, `confidence`).
  - Non-positive `line`/`end_line`, or `end_line < line`.

Any coercion MUST be logged in parser diagnostics.

Required parser diagnostics:
- `coercion_applied`: includes finding `id` when available, field name, old value (redacted if sensitive), new value.
- `finding_dropped`: includes reason code and stable location metadata (`file`, `line`) when parseable.
- `response_rejected`: includes top-level reason code when the entire payload is rejected.

Recommended reason codes (non-exhaustive, implementation MAY extend):
- `invalid_json`
- `schema_mismatch`
- `missing_required_field`
- `invalid_enum_value`
- `invalid_line_range`
- `file_not_in_changed_files`
- `incompatible_version`
- `all_findings_dropped`

#### File-path reconciliation

Each finding `file` MUST reconcile with the changed files list for the review target:
- The canonicalized finding path must exactly match one path in `changed_files`, OR
- Match after repository-normalization (e.g., remove leading `./`).

If no match is found, the finding is dropped and a diagnostic reason `file_not_in_changed_files` is emitted.

#### Partial parse fallback behavior

If a response is partially parseable:
- Keep all findings that pass full validation + reconciliation.
- Drop invalid findings individually.
- Emit parser diagnostics with per-finding failure reasons.
- Return a successful `ReviewResult` only if at least one valid finding remains.
- If zero valid findings remain, return `findings: []` with a top-level warning diagnostic `all_findings_dropped` instead of hard-failing the entire review.

## Prompting strategy

### Prompt/schema versioning

Prompts MUST explicitly pin both:
- `prompt_version`: version of instruction text/behavior contract.
- `schema_version`: version of output JSON schema.

Both fields are required in model output and validated before findings are consumed.

### Compatibility expectations

Compatibility policy:
- **Patch update** (`x.y.z` for prompt, optional third segment): backward-compatible clarifications only.
- **Minor update** (`x.y` for schema): additive, backward-compatible fields/enums.
- **Major update** (first segment change): breaking changes; requires coordinated parser/prompt rollout.

Runtime behavior:
- Parser accepts exact `schema_version` match or newer compatible minor version in the same major line.
- Parser accepts exact `prompt_version` by default; can allow configured patch drift within the same major/minor.
- On incompatible versions, parser rejects the response before finding-level validation and emits `incompatible_version` diagnostics.

### Prompt construction rules

Prompt templates MUST:
1. Include current `prompt_version` and required `schema_version` literals.
2. Re-state required vs optional fields and enum sets.
3. Instruct the model to emit only findings whose `file` is present in `changed_files`.
4. Instruct the model that invalid or uncertain findings should be omitted rather than fabricated.
5. Require strict JSON output with no prose wrappers.
6. Require deterministic field names and forbid undocumented keys unless explicitly allowed by schema.
7. Include a short reminder that unknown enum values cause drops/rejection.

### Rollout strategy

When upgrading prompt or schema:
1. Ship parser support first for new version (read-compat mode).
2. Enable new prompt version for a small traffic slice.
3. Monitor diagnostics: coercions, dropped findings, and incompatible-version failures.
4. Promote to full rollout only when dropped-finding rate remains within SLO.
5. Remove legacy compatibility paths in a subsequent major release.

Rollout gates SHOULD include:
- A canary alarm when `response_rejected` exceeds baseline by more than 2x for 30 minutes.
- A rollback trigger when valid findings/job drops below agreed SLO threshold.

## Security considerations

This section defines implementation-level security requirements. These requirements are normative and use **MUST**, **SHOULD**, and **MAY** language.

### Depot path allow-list policy and `ChangeFetcher` enforcement

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

### Safe subprocess invocation for `p4`

1. `p4` commands **MUST** be executed with argumentized subprocess APIs (e.g., `execve`/`subprocess.run([...], shell=False)`).
2. Shell interpolation is prohibited:
   - The implementation **MUST NOT** use `shell=True`, `os.system`, backticks, or equivalent shell-mediated execution.
   - Untrusted input **MUST NOT** be concatenated into command strings.
3. Command wrappers **MUST**:
   - Provide a fixed executable path or trusted resolver for `p4`.
   - Pass each argument as an individual token.
   - Apply explicit timeouts and non-zero exit handling.
4. Logs for command execution **MUST** include sanitized argument metadata only (never raw secrets or untrusted payloads).

### Secret handling for SMTP and LLM credentials

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

### Artifact retention and access controls

1. Review artifacts (diff snapshots, prompt payloads, model responses, email payload metadata) **MUST** have explicit retention TTLs:
   - Raw artifacts containing sensitive content: default TTL <= 7 days.
   - Redacted artifacts used for analytics/debugging: default TTL <= 30 days.
2. Stored artifacts **MUST** be encrypted at rest using platform-managed encryption keys at minimum; customer-managed keys are recommended where available.
3. Access control requirements:
   - Least-privilege RBAC for read/write/delete operations.
   - Access to raw artifacts restricted to authorized operational roles only.
   - Access attempts and reads **MUST** be audit-logged.
4. Storage segregation:
   - Raw and redacted artifacts **MUST** be stored in separate logical namespaces/buckets.
   - LLM-bound data paths **MUST** reference redacted artifacts only.
5. TTL enforcement **MUST** include automatic deletion jobs and periodic verification.

### Redaction patterns and required tests before LLM submission

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

## Processing error handling

This section defines failure classification, retry behavior, attempt budgeting, dead-letter payload shape, and operator recovery procedures.

### Failure classification

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

### Retry backoff policy

For all retryable failures, the system MUST apply the following backoff parameters unless an upstream `Retry-After` value is larger:

- `initial_delay`: **1s**
- `multiplier`: **2.0**
- `max_delay`: **60s**
- `jitter`: **full jitter** (`actual_delay = random(0, min(max_delay, initial_delay * multiplier^(attempt-1)))`)

Additional requirements:

1. Backoff timers MUST be recomputed per attempt (no precomputed static schedule).
2. If `Retry-After` is present, effective delay is `max(calculated_backoff, retry_after)`, capped by an operational ceiling of 5 minutes.
3. Retries MUST stop immediately when a failure is reclassified to non-retryable.

### Attempt budget model

Attempt budget is **per stage**, not global.

- Stages: `fetch`, `llm`, `notify`
- `max_attempts_per_stage`: **5** (inclusive of first attempt)
- Failure in one stage MUST NOT consume attempts from other stages.
- If a stage exhausts budget, processing transitions to dead-letter with stage-specific metadata.

Rationale: per-stage budgets isolate flaky dependencies and preserve useful work from completed stages.

### Dead-letter payload requirements

When processing is dead-lettered, payload MUST include at minimum:

- `error_class`: stable classification identifier (for example `NETWORK_TIMEOUT`, `AUTH_DENIED`, `SCHEMA_INVALID`).
- `last_stack`: most recent stack trace or error chain from the failing stage.
- `sanitized_context`: sanitized operational context (request IDs, stage, attempt counts, upstream response code, truncated payload hashes/IDs).

Required safety constraints:

1. `sanitized_context` MUST exclude secrets, access tokens, API keys, full prompts containing sensitive data, and raw PII.
2. Stack traces MUST be redacted if they contain sensitive literals.
3. Include `first_failure_at`, `last_failure_at`, and `stage` to support replay triage.

### Operator remediation and replay workflow

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

## Notification processing flow

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

## Notification error handling

### General rules
- All delivery attempts MUST be recorded in an outbox or delivery log keyed by `(changelist_id, recipient, review_version)`.
- Transient provider/network failures SHOULD produce retryable states with exponential backoff.
- Permanent failures (invalid recipient, policy rejection) SHOULD mark recipient delivery row failed and require operator or caller intervention.

### Failure matrix

| Scenario | Immediate state | Recovery action | Duplicate-send risk | Required invariant |
|---|---|---|---|---|
| **Email sent but DB write failed** (provider accepted send, then DB update for `notified_at`/`notification_id` failed) | Outbox row still appears unsent or partially updated | On retry, first attempt provider lookup by deterministic key / prior `notification_id`; if found delivered, backfill `notification_id` and `notified_at` without resending | High unless guarded; mitigated by provider idempotency key + lookup-before-resend | Never mark failed permanently until reconciliation attempted |
| **DB write succeeded but send failed** (DB transaction incorrectly marked sent before provider confirmation, or send call fails after optimistic write) | Inconsistent local state indicates sent but provider has no record | Reconcile by provider lookup; if not delivered, clear sent markers (`notified_at`, `notification_id`) and requeue send; this path SHOULD alert because ordering contract was violated | Medium; depends on correction timing | The canonical contract is send first, then mark; any violation must be detectable and repaired |

### Ordering contract enforcement
- The system MUST enforce `send` -> `mark sent` ordering in code review and tests.
- If implementation detects a row with `notified_at` set but no provider evidence, it MUST transition that row into a reconciliation workflow before any further sends.

## Idempotency

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
