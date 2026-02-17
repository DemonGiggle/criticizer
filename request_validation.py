from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from reconciliation import normalize_repo_path, reconcile_changed_file

REQUIRED_FINDING_FIELDS = {"id", "severity", "category", "title", "file", "line", "message"}
ALLOWED_SEVERITIES = {"critical", "high", "medium", "low", "info"}
ALLOWED_CATEGORIES = {"correctness", "security", "performance", "reliability", "maintainability", "style", "test"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
TOP_LEVEL_REQUIRED_FIELDS = {"schema_version", "prompt_version", "findings"}
TOP_LEVEL_OPTIONAL_FIELDS = {"summary", "meta"}
TOP_LEVEL_ALLOWED_FIELDS = TOP_LEVEL_REQUIRED_FIELDS | TOP_LEVEL_OPTIONAL_FIELDS

SUPPORTED_SCHEMA_VERSION = "1.0"
SUPPORTED_PROMPT_VERSION = "1.0.0"
SCHEMA_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")
PROMPT_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")


@dataclass(frozen=True)
class ValidationOutcome:
    review_result: dict[str, Any]
    diagnostics: list[dict[str, Any]]
    rejected: bool


class DiagnosticRecorder:
    """Collects diagnostics and emits audit logs with correlation IDs."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def emit(
        self,
        *,
        correlation_id: str,
        code: str,
        field: str,
        reason: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "correlation_id": correlation_id,
            "code": code,
            "field": field,
            "reason": reason,
            "action": action,
        }
        if details:
            entry["details"] = details
        self.entries.append(entry)


def _parse_schema_version(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = SCHEMA_VERSION_RE.fullmatch(value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _parse_prompt_version(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = PROMPT_VERSION_RE.fullmatch(value)
    if not match:
        return None
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return int(match.group(1)), int(match.group(2)), patch


def validate_and_reconcile_review_result(
    raw_payload: str,
    *,
    changed_files: list[str],
    correlation_id: str,
    recorder: DiagnosticRecorder | None = None,
) -> ValidationOutcome:
    recorder = recorder or DiagnosticRecorder()

    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        recorder.emit(
            correlation_id=correlation_id,
            code="invalid_json",
            field="payload",
            reason="json_parse_error",
            action="reject",
            details={"error": str(exc)},
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    if not isinstance(parsed, dict):
        recorder.emit(
            correlation_id=correlation_id,
            code="schema_mismatch",
            field="payload",
            reason="top_level_not_object",
            action="reject",
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    missing_top_level_fields = sorted(TOP_LEVEL_REQUIRED_FIELDS.difference(parsed))
    if missing_top_level_fields:
        recorder.emit(
            correlation_id=correlation_id,
            code="missing_required_field",
            field="payload",
            reason="missing_required_top_level_field",
            action="reject",
            details={"missing": missing_top_level_fields},
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    unexpected_top_level_fields = sorted(set(parsed).difference(TOP_LEVEL_ALLOWED_FIELDS))
    if unexpected_top_level_fields:
        recorder.emit(
            correlation_id=correlation_id,
            code="schema_mismatch",
            field="payload",
            reason="additional_properties_not_allowed",
            action="reject",
            details={"additional_properties": unexpected_top_level_fields},
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    schema_version = parsed.get("schema_version")
    parsed_schema_version = _parse_schema_version(schema_version)
    supported_schema_version = _parse_schema_version(SUPPORTED_SCHEMA_VERSION)
    if parsed_schema_version is None:
        recorder.emit(
            correlation_id=correlation_id,
            code="schema_mismatch",
            field="schema_version",
            reason="invalid_schema_version_format",
            action="reject",
            details={"value": schema_version, "expected_pattern": "major.minor"},
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    if supported_schema_version is None:
        raise RuntimeError("SUPPORTED_SCHEMA_VERSION must follow major.minor format")

    # Backward-compatibility policy: accept equal or newer minor in the same major line.
    if (
        parsed_schema_version[0] != supported_schema_version[0]
        or parsed_schema_version[1] < supported_schema_version[1]
    ):
        recorder.emit(
            correlation_id=correlation_id,
            code="incompatible_version",
            field="schema_version",
            reason="unsupported_schema_version",
            action="reject",
            details={"received": schema_version, "supported": SUPPORTED_SCHEMA_VERSION},
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    prompt_version = parsed.get("prompt_version")
    parsed_prompt_version = _parse_prompt_version(prompt_version)
    supported_prompt_version = _parse_prompt_version(SUPPORTED_PROMPT_VERSION)
    if parsed_prompt_version is None:
        recorder.emit(
            correlation_id=correlation_id,
            code="schema_mismatch",
            field="prompt_version",
            reason="invalid_prompt_version_format",
            action="reject",
            details={"value": prompt_version, "expected_pattern": "major.minor[.patch]"},
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    if supported_prompt_version is None:
        raise RuntimeError("SUPPORTED_PROMPT_VERSION must follow major.minor[.patch] format")

    # Backward-compatibility policy: allow patch drift within the same major/minor.
    if parsed_prompt_version[:2] != supported_prompt_version[:2]:
        recorder.emit(
            correlation_id=correlation_id,
            code="incompatible_version",
            field="prompt_version",
            reason="unsupported_prompt_version",
            action="reject",
            details={"received": prompt_version, "supported": SUPPORTED_PROMPT_VERSION},
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    findings = parsed.get("findings")
    if not isinstance(findings, list):
        recorder.emit(
            correlation_id=correlation_id,
            code="schema_mismatch",
            field="findings",
            reason="findings_not_array",
            action="reject",
        )
        return ValidationOutcome(review_result={"findings": []}, diagnostics=recorder.entries, rejected=True)

    changed_set = {normalize_repo_path(path) for path in changed_files}
    kept_findings: list[dict[str, Any]] = []

    for idx, finding in enumerate(findings):
        if not isinstance(finding, dict):
            recorder.emit(
                correlation_id=correlation_id,
                code="schema_mismatch",
                field=f"findings[{idx}]",
                reason="finding_not_object",
                action="drop",
            )
            continue

        if not REQUIRED_FINDING_FIELDS.issubset(finding):
            missing = sorted(REQUIRED_FINDING_FIELDS.difference(finding))
            recorder.emit(
                correlation_id=correlation_id,
                code="missing_required_field",
                field=f"findings[{idx}]",
                reason="missing_required_finding_field",
                action="drop",
                details={"missing": missing},
            )
            continue

        coerced = dict(finding)

        for field_name in ("id", "severity", "category", "title", "file", "message"):
            value = coerced.get(field_name)
            if isinstance(value, str):
                trimmed = value.strip()
                if trimmed != value:
                    recorder.emit(
                        correlation_id=correlation_id,
                        code="coercion_applied",
                        field=field_name,
                        reason="trim_whitespace",
                        action="coerce",
                        details={"old": value, "new": trimmed, "finding_index": idx},
                    )
                    coerced[field_name] = trimmed

        if isinstance(coerced.get("file"), str):
            normalized = normalize_repo_path(coerced["file"])
            if normalized != coerced["file"]:
                recorder.emit(
                    correlation_id=correlation_id,
                    code="coercion_applied",
                    field="file",
                    reason="normalize_path",
                    action="coerce",
                    details={"old": coerced['file'], "new": normalized, "finding_index": idx},
                )
                coerced["file"] = normalized

        for numeric_field in ("line", "end_line"):
            value = coerced.get(numeric_field)
            if isinstance(value, str) and value.isdigit():
                coerced[numeric_field] = int(value)
                recorder.emit(
                    correlation_id=correlation_id,
                    code="coercion_applied",
                    field=numeric_field,
                    reason="numeric_string_to_int",
                    action="coerce",
                    details={"old": value, "new": coerced[numeric_field], "finding_index": idx},
                )

        if coerced["severity"] not in ALLOWED_SEVERITIES:
            recorder.emit(
                correlation_id=correlation_id,
                code="invalid_enum_value",
                field="severity",
                reason="unsupported_severity",
                action="drop",
                details={"finding_index": idx, "value": coerced["severity"]},
            )
            continue

        if coerced["category"] not in ALLOWED_CATEGORIES:
            recorder.emit(
                correlation_id=correlation_id,
                code="invalid_enum_value",
                field="category",
                reason="unsupported_category",
                action="drop",
                details={"finding_index": idx, "value": coerced["category"]},
            )
            continue

        confidence = coerced.get("confidence")
        if confidence is not None and confidence not in ALLOWED_CONFIDENCE:
            recorder.emit(
                correlation_id=correlation_id,
                code="invalid_enum_value",
                field="confidence",
                reason="unsupported_confidence",
                action="drop",
                details={"finding_index": idx, "value": confidence},
            )
            continue

        line = coerced["line"]
        end_line = coerced.get("end_line")
        if not isinstance(line, int) or line < 1:
            recorder.emit(
                correlation_id=correlation_id,
                code="invalid_line_range",
                field="line",
                reason="line_must_be_positive_int",
                action="drop",
                details={"finding_index": idx, "value": line},
            )
            continue

        if end_line is not None and (not isinstance(end_line, int) or end_line < line):
            recorder.emit(
                correlation_id=correlation_id,
                code="invalid_line_range",
                field="end_line",
                reason="end_line_must_be_int_and_gte_line",
                action="drop",
                details={"finding_index": idx, "line": line, "end_line": end_line},
            )
            continue

        if not reconcile_changed_file(coerced["file"], changed_set):
            recorder.emit(
                correlation_id=correlation_id,
                code="file_not_in_changed_files",
                field="file",
                reason="unmatched_changed_file",
                action="drop",
                details={"finding_index": idx, "file": coerced["file"]},
            )
            continue

        kept_findings.append(coerced)

    if not kept_findings:
        recorder.emit(
            correlation_id=correlation_id,
            code="all_findings_dropped",
            field="findings",
            reason="no_valid_findings_after_validation",
            action="warn",
        )

    result = dict(parsed)
    result["findings"] = kept_findings
    return ValidationOutcome(review_result=result, diagnostics=recorder.entries, rejected=False)
