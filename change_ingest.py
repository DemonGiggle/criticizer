from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from job_dispatch import JobDispatchStore
from work_queue import WorkQueueStore

DEPOT_PATH_RE = re.compile(r"^//[^\s]+")
_DEPOT_FILE_LINE_RE = re.compile(r"^\.\.\. depotFile (//[^\s]+)$", re.MULTILINE)


@dataclass(frozen=True)
class IngestResult:
    status: str
    job_id: int | None
    queue_id: int | None


class ChangeFetcher:
    """Fetches Perforce changelist files with allow-list and safe subprocess execution."""

    def __init__(
        self,
        *,
        allowlist_prefixes: Sequence[str],
        p4_binary: str = "p4",
        timeout_seconds: int = 15,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ):
        self.allowlist_prefixes = self._validate_allowlist(allowlist_prefixes)
        self.p4_binary = p4_binary
        self.timeout_seconds = timeout_seconds
        self.runner = runner or subprocess.run
        self.security_events: list[dict[str, str]] = []

    def fetch_change(self, changelist_id: int, *, requested_paths: Sequence[str] | None = None) -> dict[str, object]:
        candidate_paths = list(requested_paths or [])
        for path in candidate_paths:
            normalized = self._normalize_depot_path(path)
            if not self._is_allowed(normalized):
                self._record_security_event(path=normalized, reason="requested_path_not_allowed")
                raise PermissionError(f"requested path outside allowlist: {normalized}")

        cmd = [self.p4_binary, "-ztag", "describe", "-s", str(changelist_id)]
        completed = self.runner(
            cmd,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"p4 describe failed with code {completed.returncode}")

        files = [self._normalize_depot_path(path) for path in _DEPOT_FILE_LINE_RE.findall(completed.stdout)]
        for path in files:
            if not self._is_allowed(path):
                self._record_security_event(path=path, reason="fetched_path_not_allowed")
                raise PermissionError(f"fetched path outside allowlist: {path}")

        return {"changelist_id": changelist_id, "files": files}

    @staticmethod
    def _validate_allowlist(prefixes: Sequence[str]) -> tuple[str, ...]:
        validated: list[str] = []
        for raw in prefixes:
            normalized = raw.strip().rstrip("/")
            if not normalized:
                raise ValueError("allowlist entries must be non-empty")
            if not normalized.startswith("//"):
                raise ValueError("allowlist entries must start with //")
            if "..." in normalized and not normalized.endswith("..."):
                raise ValueError("allowlist wildcard is only allowed as trailing ...")
            validated.append(normalized)

        if not validated:
            raise ValueError("allowlist_prefixes must not be empty")
        return tuple(validated)

    @staticmethod
    def _normalize_depot_path(path: str) -> str:
        normalized = path.strip()
        if not DEPOT_PATH_RE.match(normalized):
            raise ValueError(f"invalid depot path: {path}")
        return normalized

    def _is_allowed(self, depot_path: str) -> bool:
        for prefix in self.allowlist_prefixes:
            if prefix.endswith("..."):
                if depot_path.startswith(prefix[:-3]):
                    return True
            elif depot_path == prefix or depot_path.startswith(prefix + "/"):
                return True
        return False

    def _record_security_event(self, *, path: str, reason: str) -> None:
        self.security_events.append({"path": path, "reason": reason})


class ChangeIngestService:
    """Receives changelist input and enqueues first-pass review jobs."""

    def __init__(
        self,
        *,
        fetcher: ChangeFetcher,
        job_dispatch: JobDispatchStore,
        queue: WorkQueueStore,
    ):
        self.fetcher = fetcher
        self.job_dispatch = job_dispatch
        self.queue = queue

    def ingest_change(
        self,
        *,
        changelist_id: int,
        review_version: int,
        idempotency_key: str,
        rerun_requested: bool = False,
        requested_paths: Sequence[str] | None = None,
        priority: int = 0,
    ) -> IngestResult:
        change = self.fetcher.fetch_change(changelist_id, requested_paths=requested_paths)
        submit = self.job_dispatch.submit_job(
            changelist_id=changelist_id,
            review_version=review_version,
            idempotency_key=idempotency_key,
            rerun_requested=rerun_requested,
        )

        if not submit.created:
            return IngestResult(status=submit.status, job_id=submit.job["id"], queue_id=None)

        payload = json.dumps(
            {
                "job_id": submit.job["id"],
                "changelist_id": changelist_id,
                "review_version": review_version,
                "files": change["files"],
            },
            sort_keys=True,
        )
        queue_id = self.queue.enqueue(payload, priority=priority)
        return IngestResult(status="enqueued", job_id=submit.job["id"], queue_id=queue_id)
