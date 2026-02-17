from __future__ import annotations


def normalize_repo_path(path: str) -> str:
    """Normalize model-emitted paths for repository reconciliation."""
    return path.strip().replace('\\', '/').removeprefix('./')


def reconcile_changed_file(path: str, changed_files: set[str]) -> bool:
    normalized = normalize_repo_path(path)
    return normalized in {normalize_repo_path(item) for item in changed_files}
