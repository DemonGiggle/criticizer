# Review Criticizer Spec

## 5. Output Contract

### 5.3 ReviewResult

`ReviewResult` is the canonical top-level response object emitted by the reviewer.

#### 5.3.1 Canonical JSON schema

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

#### 5.3.2 Validation behavior

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

#### 5.3.3 File-path reconciliation

Each finding `file` MUST reconcile with the changed files list for the review target:
- The canonicalized finding path must exactly match one path in `changed_files`, OR
- Match after repository-normalization (e.g., remove leading `./`).

If no match is found, the finding is dropped and a diagnostic reason `file_not_in_changed_files` is emitted.

#### 5.3.4 Partial parse fallback behavior

If a response is partially parseable:
- Keep all findings that pass full validation + reconciliation.
- Drop invalid findings individually.
- Emit parser diagnostics with per-finding failure reasons.
- Return a successful `ReviewResult` only if at least one valid finding remains.
- If zero valid findings remain, return `findings: []` with a top-level warning diagnostic `all_findings_dropped` instead of hard-failing the entire review.

## 7. Prompting Strategy

### 7.1 Prompt/schema versioning

Prompts MUST explicitly pin both:
- `prompt_version`: version of instruction text/behavior contract.
- `schema_version`: version of output JSON schema.

Both fields are required in model output and validated before findings are consumed.

### 7.2 Compatibility expectations

Compatibility policy:
- **Patch update** (`x.y.z` for prompt, optional third segment): backward-compatible clarifications only.
- **Minor update** (`x.y` for schema): additive, backward-compatible fields/enums.
- **Major update** (first segment change): breaking changes; requires coordinated parser/prompt rollout.

Runtime behavior:
- Parser accepts exact `schema_version` match or newer compatible minor version in the same major line.
- Parser accepts exact `prompt_version` by default; can allow configured patch drift within the same major/minor.
- On incompatible versions, parser rejects the response before finding-level validation and emits `incompatible_version` diagnostics.

### 7.3 Prompt construction rules

Prompt templates MUST:
1. Include current `prompt_version` and required `schema_version` literals.
2. Re-state required vs optional fields and enum sets.
3. Instruct the model to emit only findings whose `file` is present in `changed_files`.
4. Instruct the model that invalid or uncertain findings should be omitted rather than fabricated.
5. Require strict JSON output with no prose wrappers.

### 7.4 Rollout strategy

When upgrading prompt or schema:
1. Ship parser support first for new version (read-compat mode).
2. Enable new prompt version for a small traffic slice.
3. Monitor diagnostics: coercions, dropped findings, and incompatible-version failures.
4. Promote to full rollout only when dropped-finding rate remains within SLO.
5. Remove legacy compatibility paths in a subsequent major release.
