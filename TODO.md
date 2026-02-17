# TODO

This checklist tracks work still needed to fully align implementation with `spec.md` and to harden production readiness.

## High priority

- [ ] Expand persistence model from in-memory/local SQLite usage patterns to a deployable DB profile with explicit transactional isolation guidance and migration strategy.
- [ ] Add end-to-end integration tests that exercise the full flow: ingest -> queue claim -> validation -> outbox delivery -> job finalization.
- [ ] Add structured observability (metrics + logs + trace correlation IDs) for:
  - queue claims/lease loss
  - finding coercions/drops/rejections
  - outbox retries/reconciliation paths
  - dead-letter replay outcomes

## Work queue

- [ ] Add a periodic sweeper executable/entrypoint (currently functionality exists in store methods, but no daemonized loop/ops wrapper).
- [ ] Add explicit backoff/jitter strategy examples for idle workers.
- [ ] Add tests for long-running lease renewals under high contention and clock-skew scenarios.

## Request validation and reconciliation

- [ ] Emit richer standardized reason-code catalogs and dashboards for parser diagnostics.
- [ ] Add property-based tests for coercion and finding drop edge cases.
- [ ] Add compatibility fixtures for version-rollout scenarios (exact match, minor compatible, incompatible major).

## Notification delivery

- [ ] Add explicit handling for permanent-recipient failures (invalid address/policy rejection) with operator workflows.
- [ ] Add reconciliation jobs for rows stuck in ambiguous states (`notification_id` present + `notified_at` null).
- [ ] Add provider adapter interface docs and sample implementations (SMTP/API-backed).

## Failure handling

- [ ] Persist dead-letter records to durable storage beyond in-process memory and add retention policy controls.
- [ ] Add an operator CLI/API for replay planning, remediation evidence attachment, and replay audit export.
- [ ] Add escalation routing integrations (ticketing/paging hooks).

## Security + operations

- [ ] Add startup config loader for depot allow-list with audited change history.
- [ ] Integrate secret-manager-backed credential loading and redaction tests.
- [ ] Add incident runbooks for replay, rollback, and credential rotation.

## Documentation

- [ ] Add architecture diagram covering module boundaries and control/data flow.
- [ ] Document local development commands (lint/type-check/format) once toolchain is standardized.
- [ ] Add API contract examples for external callers (ingest request, review result payload, replay command).
