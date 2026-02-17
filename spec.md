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
