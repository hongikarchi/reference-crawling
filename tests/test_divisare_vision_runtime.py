from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from canonical.divisare_vision_runtime import (
    CLI_IMAGE_DETAIL,
    DEFAULT_MODEL,
    TokenUsage,
    parse_codex_jsonl,
    run_codex_vision_batch,
)


def _event_stream(payload: object, *, input_tokens: int = 120) -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-test"}),
            "non-json diagnostic",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(payload, sort_keys=True),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": 20,
                        "output_tokens": 9,
                    },
                }
            ),
        ]
    )


def _inputs(tmp_path: Path) -> tuple[list[Path], Path]:
    images = [tmp_path / "one.jpg", tmp_path / "two.png"]
    for index, image in enumerate(images):
        image.write_bytes(b"test-image-" + bytes([index]))
    schema = tmp_path / "vision.schema.json"
    schema.write_text(
        json.dumps({"type": "object", "properties": {"results": {"type": "array"}}}),
        encoding="utf-8",
    )
    return images, schema


def test_parse_codex_jsonl_uses_last_assistant_message_and_usage() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "old"},
                }
            ),
            "warning text",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "assistant_message",
                        "content": [{"type": "output_text", "text": "new"}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "usage": {
                            "input_tokens": 50,
                            "input_tokens_details": {"cached_tokens": 7},
                            "output_tokens": 6,
                        }
                    },
                }
            ),
        ]
    )

    parsed = parse_codex_jsonl(stdout)

    assert parsed.final_assistant_text == "new"
    assert parsed.usage == TokenUsage(50, 7, 6)
    assert parsed.usage.total_tokens == 56
    assert parsed.non_json_stdout_lines == ("warning text",)
    assert len(parsed.raw_events) == 3


def test_run_batch_builds_expected_command_and_returns_diagnostics(tmp_path: Path) -> None:
    images, schema = _inputs(tmp_path)
    calls: list[tuple[list[str], dict]] = []
    payload = {
        "results": [
            {"asset_id": "asset-b", "kind": "interior"},
            {"asset_id": "asset-a", "kind": "exterior"},
        ]
    }

    def fake_runner(command: list[str], **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_event_stream(payload),
            stderr="codex warning",
        )

    ticks = iter([100.0, 101.25])
    result = run_codex_vision_batch(
        prompt="Classify the attached images.",
        image_paths=images,
        output_schema_path=schema,
        expected_asset_ids=["asset-a", "asset-b"],
        codex_bin=r"C:\Tools\codex.exe",
        timeout_seconds=42,
        runner=fake_runner,
        clock=lambda: next(ticks),
    )

    assert result.ok
    assert result.status == "success"
    assert [row["asset_id"] for row in result.records] == ["asset-a", "asset-b"]
    assert result.usage == TokenUsage(120, 20, 9)
    assert result.stdout == _event_stream(payload)
    assert result.stderr == "codex warning"
    assert len(result.raw_events) == 3
    assert result.non_json_stdout_lines == ("non-json diagnostic",)
    assert result.elapsed_seconds == 1.25
    assert result.provenance.model == DEFAULT_MODEL
    assert result.provenance.reasoning == "low"
    assert result.provenance.service_tier == "fast"
    assert result.provenance.cli_image_detail == CLI_IMAGE_DETAIL == "high"
    assert result.provenance.sandbox == "read-only"
    assert result.provenance.working_directory == str(tmp_path.resolve())
    assert result.provenance.expected_asset_ids == ("asset-a", "asset-b")
    assert "Classify the attached images." not in result.provenance.command_without_prompt

    command, kwargs = calls[0]
    assert command[:5] == [
        r"C:\Tools\codex.exe",
        "exec",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
    ]
    assert command[command.index("-m") + 1] == "gpt-5.6-sol"
    assert "model_reasoning_effort=low" in command
    assert "service_tier=fast" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("-C") + 1] == str(tmp_path.resolve())
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--output-schema") + 1] == str(schema.resolve())
    assert [command[index + 1] for index, value in enumerate(command) if value == "-i"] == [
        str(path.resolve()) for path in images
    ]
    assert command[-2:] == ["--", "Classify the attached images."]
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 42,
        "check": False,
        "cwd": str(tmp_path.resolve()),
    }


@pytest.mark.parametrize(
    ("results", "message_part"),
    [
        ([{"asset_id": "asset-a"}], "missing=['asset-b']"),
        (
            [
                {"asset_id": "asset-a"},
                {"asset_id": "asset-a"},
                {"asset_id": "asset-b"},
            ],
            "duplicates=['asset-a']",
        ),
        (
            [{"asset_id": "asset-a"}, {"asset_id": "asset-c"}],
            "unexpected=['asset-c']",
        ),
    ],
)
def test_run_batch_rejects_nonexact_asset_ids(
    tmp_path: Path,
    results: list[dict[str, str]],
    message_part: str,
) -> None:
    images, schema = _inputs(tmp_path)

    def fake_runner(command: list[str], **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_event_stream({"results": results}),
            stderr="",
        )

    result = run_codex_vision_batch(
        prompt="Classify.",
        image_paths=images,
        output_schema_path=schema,
        expected_asset_ids=["asset-a", "asset-b"],
        runner=fake_runner,
    )

    assert result.status == "validation_error"
    assert not result.ok
    assert result.error_kind == "asset_id_mismatch"
    assert message_part in (result.error_message or "")
    assert result.records == ()


def test_run_batch_returns_timeout_with_partial_events(tmp_path: Path) -> None:
    images, schema = _inputs(tmp_path)
    partial_stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "partial"},
        }
    ).encode()

    def timeout_runner(_command: list[str], **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd="codex",
            timeout=5,
            output=partial_stdout,
            stderr=b"still running",
        )

    ticks = iter([10.0, 15.0])
    result = run_codex_vision_batch(
        prompt="Classify.",
        image_paths=images,
        output_schema_path=schema,
        expected_asset_ids=["asset-a", "asset-b"],
        timeout_seconds=5,
        runner=timeout_runner,
        clock=lambda: next(ticks),
    )

    assert result.status == "timeout"
    assert result.timed_out
    assert result.final_assistant_text == "partial"
    assert result.stdout == partial_stdout.decode()
    assert result.stderr == "still running"
    assert len(result.raw_events) == 1
    assert result.elapsed_seconds == 5.0
    assert result.returncode is None


def test_run_batch_preserves_nonzero_exit_output(tmp_path: Path) -> None:
    images, schema = _inputs(tmp_path)

    def failed_runner(command: list[str], **_kwargs):
        return subprocess.CompletedProcess(
            command,
            23,
            stdout=_event_stream({"results": []}),
            stderr="backend rejected model",
        )

    result = run_codex_vision_batch(
        prompt="Classify.",
        image_paths=images,
        output_schema_path=schema,
        expected_asset_ids=["asset-a", "asset-b"],
        runner=failed_runner,
    )

    assert result.status == "exec_error"
    assert result.returncode == 23
    assert result.stderr == "backend rejected model"
    assert result.final_assistant_text is not None
    assert result.usage == TokenUsage(120, 20, 9)


def test_input_validation_happens_before_runner(tmp_path: Path) -> None:
    _, schema = _inputs(tmp_path)
    called = False

    def forbidden_runner(_command: list[str], **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("runner must not be called")

    with pytest.raises(ValueError, match="image paths do not exist"):
        run_codex_vision_batch(
            prompt="Classify.",
            image_paths=[tmp_path / "missing.jpg"],
            output_schema_path=schema,
            expected_asset_ids=["asset-a"],
            runner=forbidden_runner,
        )

    assert not called


def test_isolated_workdir_rejects_images_outside_root(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"image")
    schema = isolated / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")

    with pytest.raises(ValueError, match="isolated working directory"):
        run_codex_vision_batch(
            prompt="Classify.",
            image_paths=[outside],
            output_schema_path=schema,
            expected_asset_ids=["sample-0001"],
            working_directory=isolated,
        )
