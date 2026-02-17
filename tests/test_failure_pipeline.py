import sqlite3

import pytest

from failure_pipeline import FailureHandlingPipeline


STAGES = ["ingest", "enrich", "publish"]


def make_pipeline() -> FailureHandlingPipeline:
    return FailureHandlingPipeline(sqlite3.connect(":memory:"), stages=STAGES)


def test_non_retryable_failure_moves_to_dead_letter_with_error_metadata_and_payload_ref():
    pipeline = make_pipeline()
    run_id = pipeline.create_run("payload://42")

    dead_letter = pipeline.record_failure(
        run_id=run_id,
        failed_stage="enrich",
        error_class="ValidationError",
        error_message="schema mismatch",
        error_metadata={"field": "severity", "reason": "invalid_enum_value"},
        retryable=False,
    )

    assert dead_letter is not None
    row = pipeline.get_dead_letter(dead_letter.id)
    assert row["status"] == "open"
    assert row["failed_stage"] == "enrich"
    assert row["error_class"] == "ValidationError"
    assert row["error_metadata"] == '{"field": "severity", "reason": "invalid_enum_value"}'
    assert row["original_payload_ref"] == "payload://42"


def test_replay_requires_remediation_evidence_and_defaults_to_failed_stage_restart():
    pipeline = make_pipeline()
    run_id = pipeline.create_run("payload://job")
    dead_letter = pipeline.record_failure(
        run_id=run_id,
        failed_stage="enrich",
        error_class="PolicyError",
        error_message="blocked",
        error_metadata={"code": "blocked"},
        retryable=False,
    )
    assert dead_letter is not None

    with pytest.raises(ValueError, match="remediation evidence required"):
        pipeline.start_replay(dead_letter.id)

    pipeline.record_remediation_evidence(dead_letter.id, operator_id="oncall-1", evidence="ticket INC-7")
    replay_plan = pipeline.start_replay(dead_letter.id)

    assert replay_plan.restart_stage == "enrich"
    assert replay_plan.full_restart is False

    replay_plan_full = pipeline.start_replay(dead_letter.id, full_restart=True)
    assert replay_plan_full.restart_stage == "ingest"
    assert replay_plan_full.full_restart is True


def test_successful_replay_verifies_downstream_completion_and_sets_resolution_notes():
    pipeline = make_pipeline()
    run_id = pipeline.create_run("payload://abc")
    dead_letter = pipeline.record_failure(
        run_id=run_id,
        failed_stage="enrich",
        error_class="BusinessRuleError",
        error_message="missing owner",
        error_metadata={"field": "owner"},
        retryable=False,
    )
    assert dead_letter is not None

    pipeline.record_remediation_evidence(dead_letter.id, operator_id="oncall-2", evidence="owner backfilled")
    pipeline.start_replay(dead_letter.id)

    with pytest.raises(ValueError, match="downstream completion verification failed"):
        pipeline.complete_replay(
            dead_letter.id,
            completed_stages=["enrich"],
            resolution_notes="incomplete",
        )

    pipeline.complete_replay(
        dead_letter.id,
        completed_stages=["enrich", "publish"],
        resolution_notes="replayed from enrich and publish confirmed",
    )

    row = pipeline.get_dead_letter(dead_letter.id)
    assert row["status"] == "resolved"
    assert row["resolution_notes"] == "replayed from enrich and publish confirmed"


def test_same_non_retryable_error_class_after_replay_escalates_automatically():
    pipeline = make_pipeline()
    run_id = pipeline.create_run("payload://escalate")
    dead_letter = pipeline.record_failure(
        run_id=run_id,
        failed_stage="publish",
        error_class="TerminalProviderError",
        error_message="provider rejected payload",
        error_metadata={"provider": "x", "code": "hard_fail"},
        retryable=False,
    )
    assert dead_letter is not None

    pipeline.record_remediation_evidence(dead_letter.id, operator_id="oncall-3", evidence="provider config checked")
    pipeline.start_replay(dead_letter.id)
    pipeline.fail_replay(
        dead_letter.id,
        error_class="TerminalProviderError",
        error_message="provider rejected payload again",
        error_metadata={"provider": "x", "code": "hard_fail"},
        retryable=False,
    )

    row = pipeline.get_dead_letter(dead_letter.id)
    assert row["status"] == "escalated"
    assert row["escalated_at"] is not None
    assert row["error_class"] == "TerminalProviderError"
