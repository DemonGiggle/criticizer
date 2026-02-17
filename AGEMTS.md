# AGEMTS.md

AI operating notes for this repository.

## Primary objective

Keep implementation behavior aligned with `spec.md` using small, test-backed, auditable changes.

## Before editing

1. Read `spec.md` sections relevant to the module you are changing.
2. Read the corresponding tests under `tests/` first to preserve intent.
3. Prefer extending existing store/service patterns over introducing parallel abstractions.

## Code rules

- Preserve monotonic, timestamped state transitions (`updated_at` semantics).
- Keep operations idempotent under at-least-once execution assumptions.
- For ownership-sensitive writes, retain owner/lease predicates in SQL updates.
- Keep diagnostics machine-readable with stable `code`-style reasons.
- Use argumentized subprocess invocation only (`shell=False`) for command execution.

## Testing rules

- Add or update tests in `tests/` for every behavior change.
- Prefer deterministic unit tests using in-memory SQLite/fakes.
- Cover both success and failure/reconciliation paths.

## Documentation rules

- Update `README.md` when module responsibilities or workflow expectations change.
- Update `TODO.md` when work is completed or newly identified.
- Keep docs explicit about what is implemented now vs planned.

## Commit hygiene

- Keep commits focused and descriptive.
- Mention affected module(s) and invariant(s) in commit messages.
