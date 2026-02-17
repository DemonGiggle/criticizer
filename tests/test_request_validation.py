import json

from request_validation import DiagnosticRecorder, validate_and_reconcile_review_result


def _base_finding(**overrides):
    finding = {
        "id": "f1",
        "severity": "high",
        "category": "correctness",
        "title": "Title",
        "file": "src/main.py",
        "line": 10,
        "message": "Message",
    }
    finding.update(overrides)
    return finding


def test_emits_coercion_diagnostics_for_trim_path_and_line_conversion():
    payload = {
        "schema_version": "1.0",
        "prompt_version": "1.0.0",
        "findings": [
            _base_finding(id="  f1  ", title=" Title ", file=" ./src\\main.py ", line="10"),
        ],
    }
    outcome = validate_and_reconcile_review_result(
        json.dumps(payload),
        changed_files=["src/main.py"],
        correlation_id="corr-1",
    )

    assert outcome.rejected is False
    assert len(outcome.review_result["findings"]) == 1
    finding = outcome.review_result["findings"][0]
    assert finding["id"] == "f1"
    assert finding["title"] == "Title"
    assert finding["file"] == "src/main.py"
    assert finding["line"] == 10

    coerce_entries = [d for d in outcome.diagnostics if d["action"] == "coerce"]
    assert {d["field"] for d in coerce_entries} >= {"id", "title", "file", "line"}
    assert all(entry["correlation_id"] == "corr-1" for entry in outcome.diagnostics)


def test_drops_for_missing_required_fields_and_invalid_enums_and_line_ranges_and_reconciliation():
    payload = {
        "schema_version": "1.0",
        "prompt_version": "1.0.0",
        "findings": [
            {"id": "missing-stuff"},
            _base_finding(id="bad-sev", severity="urgent"),
            _base_finding(id="bad-cat", category="ux"),
            _base_finding(id="bad-confidence", confidence="certain"),
            _base_finding(id="bad-line", line=0),
            _base_finding(id="bad-end", end_line=2),
            _base_finding(id="bad-file", file="src/elsewhere.py"),
        ],
    }
    outcome = validate_and_reconcile_review_result(
        json.dumps(payload),
        changed_files=["src/main.py"],
        correlation_id="corr-2",
    )

    assert outcome.rejected is False
    assert outcome.review_result["findings"] == []

    codes = [d["code"] for d in outcome.diagnostics]
    assert "missing_required_field" in codes
    assert codes.count("invalid_enum_value") == 3
    assert codes.count("invalid_line_range") == 2
    assert "file_not_in_changed_files" in codes
    assert "all_findings_dropped" in codes


def test_rejects_invalid_json_and_findings_non_array_with_standard_diagnostic_schema():
    bad_json = validate_and_reconcile_review_result(
        "{",
        changed_files=["src/main.py"],
        correlation_id="corr-3",
    )
    assert bad_json.rejected is True
    assert bad_json.diagnostics[0]["code"] == "invalid_json"
    assert {"code", "field", "reason", "action"}.issubset(bad_json.diagnostics[0])

    bad_findings = validate_and_reconcile_review_result(
        json.dumps({"schema_version": "1.0", "prompt_version": "1.0.0", "findings": {}}),
        changed_files=["src/main.py"],
        correlation_id="corr-4",
    )
    assert bad_findings.rejected is True
    assert bad_findings.diagnostics[0]["code"] == "schema_mismatch"




def test_rejects_missing_required_top_level_keys():
    missing_schema = validate_and_reconcile_review_result(
        json.dumps({"prompt_version": "1.0.0", "findings": []}),
        changed_files=["src/main.py"],
        correlation_id="corr-6",
    )
    assert missing_schema.rejected is True
    assert missing_schema.diagnostics[0]["code"] == "missing_required_field"
    assert missing_schema.diagnostics[0]["details"]["missing"] == ["schema_version"]


def test_rejects_top_level_additional_properties():
    outcome = validate_and_reconcile_review_result(
        json.dumps({
            "schema_version": "1.0",
            "prompt_version": "1.0.0",
            "findings": [],
            "unexpected": True,
        }),
        changed_files=["src/main.py"],
        correlation_id="corr-7",
    )

    assert outcome.rejected is True
    assert outcome.diagnostics[0]["code"] == "schema_mismatch"
    assert outcome.diagnostics[0]["reason"] == "additional_properties_not_allowed"


def test_rejects_incompatible_or_malformed_contract_versions():
    malformed_schema = validate_and_reconcile_review_result(
        json.dumps({"schema_version": "v1", "prompt_version": "1.0.0", "findings": []}),
        changed_files=["src/main.py"],
        correlation_id="corr-8",
    )
    assert malformed_schema.rejected is True
    assert malformed_schema.diagnostics[0]["code"] == "schema_mismatch"
    assert malformed_schema.diagnostics[0]["field"] == "schema_version"

    incompatible_schema = validate_and_reconcile_review_result(
        json.dumps({"schema_version": "2.0", "prompt_version": "1.0.0", "findings": []}),
        changed_files=["src/main.py"],
        correlation_id="corr-9",
    )
    assert incompatible_schema.rejected is True
    assert incompatible_schema.diagnostics[0]["code"] == "incompatible_version"
    assert incompatible_schema.diagnostics[0]["field"] == "schema_version"

    incompatible_prompt = validate_and_reconcile_review_result(
        json.dumps({"schema_version": "1.0", "prompt_version": "1.1.0", "findings": []}),
        changed_files=["src/main.py"],
        correlation_id="corr-10",
    )
    assert incompatible_prompt.rejected is True
    assert incompatible_prompt.diagnostics[0]["code"] == "incompatible_version"
    assert incompatible_prompt.diagnostics[0]["field"] == "prompt_version"


def test_accepts_backward_compatible_contract_versions():
    outcome = validate_and_reconcile_review_result(
        json.dumps({
            "schema_version": "1.2",
            "prompt_version": "1.0.9",
            "findings": [_base_finding()],
        }),
        changed_files=["src/main.py"],
        correlation_id="corr-11",
    )

    assert outcome.rejected is False
    assert len(outcome.review_result["findings"]) == 1

def test_recorder_can_be_supplied_for_audit_replay_capture():
    recorder = DiagnosticRecorder()
    payload = {
        "schema_version": "1.0",
        "prompt_version": "1.0.0",
        "findings": [_base_finding(file="src/unmatched.py")],
    }
    validate_and_reconcile_review_result(
        json.dumps(payload),
        changed_files=["src/main.py"],
        correlation_id="corr-5",
        recorder=recorder,
    )

    assert recorder.entries
    assert recorder.entries[0]["correlation_id"] == "corr-5"
