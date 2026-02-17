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
