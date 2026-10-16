from __future__ import annotations

import json
import sqlite3

from canonical.cross_source_semantic_vision_sidecar import (
    SIDECAR_SCHEMA,
    _LOGICAL_TABLES as RUNNER_LOGICAL_TABLES,
    logical_sha256 as runner_logical_sha256,
)
from canonical.cross_source_semantic_vision_validator import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    SemanticVisionValidationResult,
    ValidationCheck,
    _LOGICAL_TABLES as VALIDATOR_LOGICAL_TABLES,
    _fetch_attempt_replay_errors,
    _parse_stdout_events,
    _vision_attempt_replay_errors,
    independent_logical_sha256,
)


def test_independent_logical_manifest_matches_runner_table_contract() -> None:
    assert VALIDATOR_LOGICAL_TABLES == RUNNER_LOGICAL_TABLES
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SIDECAR_SCHEMA)
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert independent_logical_sha256(connection) == runner_logical_sha256(connection)
    finally:
        connection.close()


def test_validation_json_never_claims_network_or_model_requests() -> None:
    result = SemanticVisionValidationResult(
        passed=True,
        sidecar_path="result.db",
        sidecar_size=123,
        sidecar_sha256="a" * 64,
        run_id="run",
        run_status="complete",
        logical_sha256="b" * 64,
        counts={"results": 57},
        tokens={"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3},
        checks=(ValidationCheck("ok", "error", True, 1, 1),),
    )
    payload = result.as_dict()
    assert payload["network_requests"] == 0
    assert payload["vision_requests"] == 0
    assert payload["llm_requests"] == 0
    assert payload["failed_error_checks"] == []


def test_failed_error_check_is_reported_but_warning_is_not_a_hard_failure() -> None:
    checks = (
        ValidationCheck("warning", "warning", False, "good", "bad"),
        ValidationCheck("error", "error", False, "good", "bad"),
    )
    result = SemanticVisionValidationResult(
        passed=False,
        sidecar_path="result.db",
        sidecar_size=0,
        sidecar_sha256="a" * 64,
        run_id="run",
        run_status="complete_with_failures",
        logical_sha256="b" * 64,
        counts={},
        tokens={},
        checks=checks,
    )
    assert result.as_dict()["failed_error_checks"] == ["error"]


def test_stdout_jsonl_parser_extracts_last_assistant_results_and_usage() -> None:
    records = [{"asset_id": "semv_000001", "in_scope": True}]
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps({"results": records}),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 123,
                        "cached_input_tokens": 45,
                        "output_tokens": 67,
                    },
                }
            ),
        ]
    ).encode()
    parsed = _parse_stdout_events(stdout)
    assert parsed["results"] == records
    assert parsed["usage"] == (123, 45, 67)
    assert parsed["event_count"] == 2
    assert parsed["non_json_lines"] == 0


def test_stdout_jsonl_parser_preserves_invalid_failed_assistant_as_evidence() -> None:
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "assistant_message", "text": "not JSON"},
        }
    ).encode()
    parsed = _parse_stdout_events(stdout)
    assert parsed["results"] is None
    assert parsed["results_error"]
    assert parsed["final_text"] == "not JSON"


def test_fetch_replay_accepts_legitimate_crash_resume_checkpoints() -> None:
    retryable_failure = {
        "attempt_no": 1,
        "outcome": "http_failed",
        "retryable": 1,
        "scheduled_delay_seconds": 1.0,
    }
    assert (
        _fetch_attempt_replay_errors(
            [retryable_failure],
            input_status="pending",
            selected_attempt_no=None,
        )
        == []
    )

    exact = {
        "attempt_no": 2,
        "outcome": "exact_match",
        "retryable": 0,
        "scheduled_delay_seconds": None,
    }
    assert (
        _fetch_attempt_replay_errors(
            [retryable_failure, exact],
            input_status="ready",
            selected_attempt_no=2,
        )
        == []
    )
    assert (
        _fetch_attempt_replay_errors(
            [retryable_failure, exact],
            input_status="success",
            selected_attempt_no=2,
        )
        == []
    )


def test_fetch_replay_rejects_refetch_after_ready_and_invalid_retry_budget() -> None:
    exact_then_refetch = [
        {
            "attempt_no": 1,
            "outcome": "exact_match",
            "retryable": 0,
            "scheduled_delay_seconds": None,
        },
        {
            "attempt_no": 2,
            "outcome": "exact_match",
            "retryable": 0,
            "scheduled_delay_seconds": None,
        },
    ]
    errors = _fetch_attempt_replay_errors(
        exact_then_refetch,
        input_status="ready",
        selected_attempt_no=2,
    )
    assert any(
        error["error"] == "non-retryable attempt followed by another"
        for error in errors
    )

    exhausted_retryable = [
        {
            "attempt_no": number,
            "outcome": "http_failed",
            "retryable": 1,
            "scheduled_delay_seconds": 0 if number == 2 else 1.0,
        }
        for number in range(1, 4)
    ]
    errors = _fetch_attempt_replay_errors(
        exhausted_retryable,
        input_status="fetch_failed",
        selected_attempt_no=None,
    )
    assert any(error["error"] == "retryable attempt exhausts fetch budget" for error in errors)
    assert any(
        error["error"] == "retryable attempt lacks positive scheduled delay"
        for error in errors
    )
    assert any(error["error"] == "terminal fetch failure remains retryable" for error in errors)


def test_vision_replay_distinguishes_resumable_and_terminal_retry_chains() -> None:
    failed = {"attempt_no": 1, "status": "failed"}
    succeeded = {"attempt_no": 2, "status": "success"}
    assert _vision_attempt_replay_errors([failed], terminal_run=False) == []
    assert _vision_attempt_replay_errors(
        [failed, succeeded], terminal_run=True
    ) == []

    stopped_early = _vision_attempt_replay_errors([failed], terminal_run=True)
    assert any(
        error["error"]
        == "terminal Vision retry chain stopped before budget exhaustion"
        for error in stopped_early
    )


def test_vision_replay_rejects_attempt_after_success_and_budget_overrun() -> None:
    rows = [
        {"attempt_no": 1, "status": "success"},
        {"attempt_no": 2, "status": "failed"},
        {"attempt_no": 3, "status": "failed"},
    ]
    errors = _vision_attempt_replay_errors(rows, terminal_run=True)
    assert any(
        error["error"] == "successful Vision attempt followed by another"
        for error in errors
    )
    assert any(error["error"] == "Vision retry budget exceeded" for error in errors)
