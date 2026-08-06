"""Deterministic Codex CLI runtime for batched Divisare Vision analysis.

This module only attaches existing local image files. Downloading, resizing,
and deleting temporary images remain responsibilities of the calling pipeline.
Codex CLI ``--image`` currently emits ``detail=high``; that observed behavior
is recorded in provenance instead of being presented as a configurable option.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Sequence


RUNTIME_VERSION = "divisare-codex-vision-runtime-v1.1.0"
DEFAULT_CODEX_BIN = "codex"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "low"
DEFAULT_SERVICE_TIER = "fast"
CLI_IMAGE_DETAIL = "high"
RUNTIME_SANDBOX = "read-only"

RuntimeStatus = Literal[
    "success",
    "timeout",
    "exec_error",
    "parse_error",
    "validation_error",
]
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        """Total billed input/output units reported by Codex.

        Cached input is a subset of input tokens and must not be added again.
        """

        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ParsedCodexEvents:
    final_assistant_text: str | None
    usage: TokenUsage | None
    raw_events: tuple[Any, ...]
    non_json_stdout_lines: tuple[str, ...]


@dataclass(frozen=True)
class VisionRuntimeProvenance:
    runtime_version: str
    codex_bin: str
    model: str
    reasoning: str
    service_tier: str
    cli_image_detail: str
    sandbox: str
    working_directory: str
    output_schema_path: str
    output_schema_sha256: str
    prompt_sha256: str
    image_paths: tuple[str, ...]
    expected_asset_ids: tuple[str, ...]
    timeout_seconds: float
    command_without_prompt: tuple[str, ...]


@dataclass(frozen=True)
class VisionRuntimeResult:
    status: RuntimeStatus
    records: tuple[dict[str, Any], ...]
    final_assistant_text: str | None
    usage: TokenUsage | None
    stdout: str
    stderr: str
    raw_events: tuple[Any, ...]
    non_json_stdout_lines: tuple[str, ...]
    elapsed_seconds: float
    returncode: int | None
    error_kind: str | None
    error_message: str | None
    provenance: VisionRuntimeProvenance

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def timed_out(self) -> bool:
        return self.status == "timeout"


def _nonnegative_int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    number = int(value)
    if number < 0:
        raise ValueError("token counts must be non-negative")
    return number


def _usage_from_event(event: dict[str, Any]) -> TokenUsage | None:
    usage: Any = None
    if event.get("type") == "turn.completed":
        usage = event.get("usage")
    elif event.get("type") == "response.completed":
        response = event.get("response")
        if isinstance(response, dict):
            usage = response.get("usage")
    if not isinstance(usage, dict):
        return None

    details = usage.get("input_tokens_details")
    nested_cached = details.get("cached_tokens") if isinstance(details, dict) else None
    try:
        return TokenUsage(
            input_tokens=_nonnegative_int(usage.get("input_tokens")),
            cached_input_tokens=_nonnegative_int(
                usage.get("cached_input_tokens", nested_cached)
            ),
            output_tokens=_nonnegative_int(usage.get("output_tokens")),
        )
    except (TypeError, ValueError):
        return None


def _assistant_text_from_item(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") not in {"agent_message", "assistant_message"}:
        return None
    if isinstance(item.get("text"), str):
        return item["text"]

    content = item.get("content")
    if not isinstance(content, list):
        return None
    text_parts = [
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type") in {"output_text", "input_text", "text"}
        and isinstance(part.get("text"), str)
    ]
    return "".join(text_parts) if text_parts else None


def parse_codex_jsonl(stdout: str) -> ParsedCodexEvents:
    """Parse Codex ``exec --json`` JSONL without discarding diagnostics."""

    events: list[Any] = []
    non_json: list[str] = []
    final_text: str | None = None
    usage: TokenUsage | None = None

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            non_json.append(raw_line)
            continue
        events.append(event)
        if not isinstance(event, dict):
            continue

        if event.get("type") == "item.completed":
            text = _assistant_text_from_item(event.get("item"))
            if text is not None:
                final_text = text
        event_usage = _usage_from_event(event)
        if event_usage is not None:
            usage = event_usage

    return ParsedCodexEvents(
        final_assistant_text=final_text,
        usage=usage,
        raw_events=tuple(events),
        non_json_stdout_lines=tuple(non_json),
    )


def _coerce_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_inputs(
    *,
    prompt: str,
    image_paths: Sequence[str | Path],
    output_schema_path: str | Path,
    expected_asset_ids: Sequence[str],
    codex_bin: str | Path,
    model: str,
    reasoning: str,
    service_tier: str,
    timeout_seconds: float,
    working_directory: str | Path | None,
) -> tuple[tuple[Path, ...], Path, tuple[str, ...], Path]:
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if not str(codex_bin).strip():
        raise ValueError("codex_bin must not be empty")
    if not model.strip() or not reasoning.strip() or not service_tier.strip():
        raise ValueError("model, reasoning, and service_tier must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    resolved_images = tuple(Path(path).resolve() for path in image_paths)
    if not resolved_images:
        raise ValueError("at least one image path is required")
    missing_images = [str(path) for path in resolved_images if not path.is_file()]
    if missing_images:
        raise ValueError(f"image paths do not exist: {missing_images}")

    schema = Path(output_schema_path).resolve()
    if not schema.is_file():
        raise ValueError(f"output schema does not exist: {schema}")

    workdir = Path(working_directory).resolve() if working_directory else schema.parent
    if not workdir.is_dir():
        raise ValueError(f"working directory does not exist: {workdir}")
    outside = [
        str(path)
        for path in (*resolved_images, schema)
        if not path.is_relative_to(workdir)
    ]
    if outside:
        raise ValueError(
            "images and output schema must be contained by the isolated working "
            f"directory: {outside}"
        )

    expected = tuple(expected_asset_ids)
    if not expected or any(not isinstance(value, str) or not value for value in expected):
        raise ValueError("expected_asset_ids must contain non-empty strings")
    duplicates = sorted({value for value in expected if expected.count(value) > 1})
    if duplicates:
        raise ValueError(f"expected_asset_ids contains duplicates: {duplicates}")
    return resolved_images, schema, expected, workdir


def _decode_records(
    final_text: str,
    *,
    records_key: str,
) -> list[dict[str, Any]]:
    value = json.loads(final_text)
    if isinstance(value, dict):
        value = value.get(records_key)
    if not isinstance(value, list):
        raise ValueError(
            f"final assistant JSON must be an array or an object containing {records_key!r}"
        )
    if not all(isinstance(record, dict) for record in value):
        raise ValueError("every Vision result must be a JSON object")
    return value


def _validate_records(
    records: Sequence[dict[str, Any]],
    expected_asset_ids: tuple[str, ...],
    *,
    asset_id_field: str,
) -> tuple[dict[str, Any], ...]:
    actual_ids: list[str] = []
    for index, record in enumerate(records):
        asset_id = record.get(asset_id_field)
        if not isinstance(asset_id, str) or not asset_id:
            raise ValueError(
                f"record {index} has no non-empty string {asset_id_field!r}"
            )
        actual_ids.append(asset_id)

    duplicates = sorted({value for value in actual_ids if actual_ids.count(value) > 1})
    actual_set = set(actual_ids)
    expected_set = set(expected_asset_ids)
    missing = [value for value in expected_asset_ids if value not in actual_set]
    unexpected = sorted(actual_set - expected_set)
    if duplicates or missing or unexpected or len(actual_ids) != len(expected_asset_ids):
        raise ValueError(
            "asset ID mismatch: "
            f"missing={missing}, unexpected={unexpected}, duplicates={duplicates}, "
            f"expected_count={len(expected_asset_ids)}, actual_count={len(actual_ids)}"
        )

    by_id = {record[asset_id_field]: record for record in records}
    return tuple(by_id[asset_id] for asset_id in expected_asset_ids)


def run_codex_vision_batch(
    *,
    prompt: str,
    image_paths: Sequence[str | Path],
    output_schema_path: str | Path,
    expected_asset_ids: Sequence[str],
    codex_bin: str | Path = DEFAULT_CODEX_BIN,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    timeout_seconds: float = 600,
    records_key: str = "results",
    asset_id_field: str = "asset_id",
    working_directory: str | Path | None = None,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.perf_counter,
) -> VisionRuntimeResult:
    """Run one batched Codex Vision request and return complete diagnostics.

    A successful result contains exactly one record for every expected asset ID,
    reordered to match ``expected_asset_ids``. Runtime, parsing, and validation
    failures are returned as typed statuses rather than silently dropping rows.
    """

    images, schema, expected, workdir = _validate_inputs(
        prompt=prompt,
        image_paths=image_paths,
        output_schema_path=output_schema_path,
        expected_asset_ids=expected_asset_ids,
        codex_bin=codex_bin,
        model=model,
        reasoning=reasoning,
        service_tier=service_tier,
        timeout_seconds=timeout_seconds,
        working_directory=working_directory,
    )

    command = [
        str(codex_bin),
        "exec",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        RUNTIME_SANDBOX,
        "-C",
        str(workdir),
        "-m",
        model,
        "-c",
        f"model_reasoning_effort={reasoning}",
        "-c",
        f"service_tier={service_tier}",
        "--output-schema",
        str(schema),
    ]
    for image_path in images:
        command.extend(["-i", str(image_path)])
    command.extend(["--", prompt])

    prompt_sha = _sha256_bytes(prompt.encode("utf-8"))
    provenance = VisionRuntimeProvenance(
        runtime_version=RUNTIME_VERSION,
        codex_bin=str(codex_bin),
        model=model,
        reasoning=reasoning,
        service_tier=service_tier,
        cli_image_detail=CLI_IMAGE_DETAIL,
        sandbox=RUNTIME_SANDBOX,
        working_directory=str(workdir),
        output_schema_path=str(schema),
        output_schema_sha256=_sha256_bytes(schema.read_bytes()),
        prompt_sha256=prompt_sha,
        image_paths=tuple(str(path) for path in images),
        expected_asset_ids=expected,
        timeout_seconds=float(timeout_seconds),
        command_without_prompt=tuple(command[:-1] + [f"<prompt sha256={prompt_sha}>"]),
    )

    started = clock()
    try:
        process = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            cwd=str(workdir),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_stream(exc.stdout)
        stderr = _coerce_stream(exc.stderr)
        parsed = parse_codex_jsonl(stdout)
        return VisionRuntimeResult(
            status="timeout",
            records=(),
            final_assistant_text=parsed.final_assistant_text,
            usage=parsed.usage,
            stdout=stdout,
            stderr=stderr,
            raw_events=parsed.raw_events,
            non_json_stdout_lines=parsed.non_json_stdout_lines,
            elapsed_seconds=max(0.0, clock() - started),
            returncode=None,
            error_kind="timeout",
            error_message=f"codex exec exceeded {timeout_seconds} seconds",
            provenance=provenance,
        )
    except OSError as exc:
        return VisionRuntimeResult(
            status="exec_error",
            records=(),
            final_assistant_text=None,
            usage=None,
            stdout="",
            stderr=str(exc),
            raw_events=(),
            non_json_stdout_lines=(),
            elapsed_seconds=max(0.0, clock() - started),
            returncode=None,
            error_kind=exc.__class__.__name__,
            error_message=str(exc),
            provenance=provenance,
        )

    elapsed = max(0.0, clock() - started)
    stdout = _coerce_stream(process.stdout)
    stderr = _coerce_stream(process.stderr)
    parsed = parse_codex_jsonl(stdout)

    def result(
        status: RuntimeStatus,
        *,
        records: tuple[dict[str, Any], ...] = (),
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> VisionRuntimeResult:
        return VisionRuntimeResult(
            status=status,
            records=records,
            final_assistant_text=parsed.final_assistant_text,
            usage=parsed.usage,
            stdout=stdout,
            stderr=stderr,
            raw_events=parsed.raw_events,
            non_json_stdout_lines=parsed.non_json_stdout_lines,
            elapsed_seconds=elapsed,
            returncode=process.returncode,
            error_kind=error_kind,
            error_message=error_message,
            provenance=provenance,
        )

    if process.returncode:
        return result(
            "exec_error",
            error_kind="nonzero_exit",
            error_message=f"codex exec exited with return code {process.returncode}",
        )
    if parsed.final_assistant_text is None:
        return result(
            "parse_error",
            error_kind="missing_final_assistant_text",
            error_message="Codex JSONL contained no completed assistant message",
        )
    if parsed.usage is None:
        return result(
            "parse_error",
            error_kind="missing_token_usage",
            error_message="Codex JSONL contained no valid completion token usage",
        )

    try:
        decoded = _decode_records(parsed.final_assistant_text, records_key=records_key)
    except (json.JSONDecodeError, ValueError) as exc:
        return result(
            "parse_error",
            error_kind="invalid_final_json",
            error_message=str(exc),
        )
    try:
        validated = _validate_records(
            decoded,
            expected,
            asset_id_field=asset_id_field,
        )
    except ValueError as exc:
        return result(
            "validation_error",
            error_kind="asset_id_mismatch",
            error_message=str(exc),
        )
    return result("success", records=validated)
