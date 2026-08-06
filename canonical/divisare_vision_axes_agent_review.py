"""Run a blinded, resumable Codex-assisted review of the fresh axes N50."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from canonical import divisare_vision_axes_review as review_contract
from canonical import divisare_vision_axes_review_inputs as review_inputs
from canonical.divisare_image_smoke import canonical_json, file_sha256
from canonical.divisare_vision_axes import (
    AXIS_OUTPUT_SCHEMA,
    compose_axes_prompt,
    normalize_axes_result,
)
from canonical.divisare_vision_axes_benchmark import AXIS_FIELDS, EVALUATION_FIELDS
from canonical.divisare_vision_gold_finalize import parse_json_strict
from canonical.divisare_vision_runtime import (
    DEFAULT_MODEL,
    DEFAULT_SERVICE_TIER,
    VisionRuntimeResult,
    run_codex_vision_batch,
)


RUN_VERSION = "divisare-vision-axes-agent-review-v1.0.0"
DEFAULT_BATCH_SIZE = 5
DEFAULT_REASONING = "high"
ORDER_MODES = ("forward", "reverse")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _annotation_from_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    review_id = str(raw.get("asset_id") or "")
    normalized = normalize_axes_result(dict(raw), review_id)
    uncertain = set(normalized["uncertain_axes"])
    clarity: dict[str, str] = {
        "in_scope": "boundary" if "scope" in uncertain else "clear",
        "reject_reason": "boundary" if "scope" in uncertain else "clear",
    }
    for field in AXIS_FIELDS:
        value = normalized[field]
        if value == "not_applicable":
            clarity[field] = "not_judgeable"
        elif field in uncertain:
            clarity[field] = "boundary"
        else:
            clarity[field] = "clear"
    return {
        "review_id": review_id,
        "in_scope": normalized["in_scope"],
        "reject_reason": normalized["reject_reason"],
        **{field: normalized[field] for field in AXIS_FIELDS},
        "uncertain_axes": list(normalized["uncertain_axes"]),
        "resolution_insufficient": normalized["resolution_insufficient"],
        "evidence": normalized["evidence"],
        "clarity": {field: clarity[field] for field in EVALUATION_FIELDS},
    }


def _review_prompt(review_ids: Sequence[str], reviewer_id: str) -> str:
    prefix = (
        "You are performing a blinded QA annotation pass, not evaluating or "
        "imitating another model. Judge visible pixels independently and use "
        "uncertainty only where the controlled labels have genuine visible "
        "support. Do not inspect the filesystem, infer source metadata, or use "
        "anything except the attached images and this codebook. Reviewer lane: "
        + reviewer_id
        + ".\n\n"
    )
    return prefix + compose_axes_prompt(review_ids)


def _load_public_staging(
    *, candidate_manifest_path: Path, staging_dir: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str, str]:
    candidate, manifest_file_sha, manifest_logical_sha = (
        review_inputs._load_frozen_manifest(candidate_manifest_path)
    )
    selected = review_inputs._selected_rows(candidate, "all")
    expected_rows = [dict(public) for public, _sample in selected]
    public_path = staging_dir / review_inputs.REVIEW_INPUTS_FILENAME
    raw = public_path.read_bytes()
    public = parse_json_strict(raw, label=review_inputs.REVIEW_INPUTS_FILENAME)
    review_inputs._validate_staging_directory(
        staging_dir,
        public,
        expected_rows=expected_rows,
        manifest_file_sha=manifest_file_sha,
        manifest_logical_sha=manifest_logical_sha,
    )
    return (
        candidate,
        [dict(row) for row in public["review_rows"]],
        manifest_file_sha,
        manifest_logical_sha,
        _sha256_bytes(raw),
    )


def _checkpoint_contract(
    *,
    reviewer_id: str,
    review_context_id: str,
    candidate_file_sha: str,
    candidate_logical_sha: str,
    review_inputs_file_sha: str,
    model: str,
    reasoning: str,
    service_tier: str,
    batch_size: int,
    order_mode: str,
) -> dict[str, Any]:
    return {
        "run_version": RUN_VERSION,
        "reviewer_id": reviewer_id,
        "review_context_id": review_context_id,
        "candidate_manifest_file_sha256": candidate_file_sha,
        "candidate_manifest_logical_sha256": candidate_logical_sha,
        "review_inputs_file_sha256": review_inputs_file_sha,
        "axis_prompt_version": review_contract.AXIS_PROMPT_VERSION,
        "codebook_sha256": review_contract.axes_review_codebook_sha256(),
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "batch_size": batch_size,
        "order_mode": order_mode,
    }


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".partial", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _load_or_create_checkpoint(
    path: Path,
    *,
    contract: Mapping[str, Any],
    resume: bool,
) -> dict[str, Any]:
    if path.exists():
        if not resume:
            raise FileExistsError("review checkpoint exists; use --resume: %s" % path)
        payload = parse_json_strict(path.read_bytes(), label="review checkpoint")
        if payload.get("contract") != dict(contract):
            raise ValueError("review checkpoint contract mismatch")
        if payload.get("status") not in {"running", "complete"}:
            raise ValueError("review checkpoint status is invalid")
        if not isinstance(payload.get("records"), dict) or not isinstance(
            payload.get("batches"), list
        ):
            raise ValueError("review checkpoint content is invalid")
        return payload
    if resume:
        raise FileNotFoundError("review checkpoint does not exist: %s" % path)
    payload = {
        "contract": dict(contract),
        "status": "running",
        "records": {},
        "batches": [],
    }
    _write_checkpoint(path, payload)
    return payload


def _isolated_batch(
    *,
    scratch_root: Path,
    staging_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    prompt: str,
    codex_bin: str | Path,
    model: str,
    reasoning: str,
    service_tier: str,
    timeout_seconds: float,
    batch_runner: Callable[..., VisionRuntimeResult],
    output_schema: Mapping[str, Any] = AXIS_OUTPUT_SCHEMA,
) -> VisionRuntimeResult:
    scratch_root = scratch_root.resolve()
    if not scratch_root.is_dir():
        raise FileNotFoundError("review scratch root does not exist: %s" % scratch_root)
    with tempfile.TemporaryDirectory(
        prefix="divisare-axis-review-", dir=scratch_root
    ) as temporary_name:
        workdir = Path(temporary_name)
        schema_path = workdir / "axis_output_schema.json"
        schema_path.write_text(
            json.dumps(
                output_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            encoding="utf-8",
            newline="\n",
        )
        image_paths: list[Path] = []
        ids: list[str] = []
        for row in rows:
            review_id = str(row["review_id"])
            source = staging_dir / str(row["file_name"])
            target = workdir / (review_id + ".jpg")
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
            image_paths.append(target)
            ids.append(review_id)
        return batch_runner(
            prompt=prompt,
            image_paths=image_paths,
            output_schema_path=schema_path,
            expected_asset_ids=ids,
            codex_bin=codex_bin,
            model=model,
            reasoning=reasoning,
            service_tier=service_tier,
            timeout_seconds=timeout_seconds,
            records_key="results",
            asset_id_field="asset_id",
            working_directory=workdir,
        )


def run_agent_review(
    *,
    candidate_manifest_path: Path,
    staging_dir: Path,
    checkpoint_path: Path,
    output_path: Path,
    reviewer_id: str,
    review_context_id: str,
    codex_bin: str | Path,
    scratch_root: Path,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    batch_size: int = DEFAULT_BATCH_SIZE,
    order_mode: str = "forward",
    timeout_seconds: float = 900,
    resume: bool = False,
    stop_after_batches: int | None = None,
    batch_runner: Callable[..., VisionRuntimeResult] = run_codex_vision_batch,
) -> dict[str, Any]:
    """Run or resume one isolated reviewer lane and seal its annotations."""
    candidate_manifest_path = candidate_manifest_path.resolve()
    staging_dir = staging_dir.resolve()
    checkpoint_path = checkpoint_path.resolve()
    output_path = output_path.resolve()
    if output_path.exists():
        raise FileExistsError("review output already exists: %s" % output_path)
    if order_mode not in ORDER_MODES:
        raise ValueError("order_mode must be forward or reverse")
    if batch_size != DEFAULT_BATCH_SIZE:
        raise ValueError("fresh review batch_size is frozen at 5")
    if stop_after_batches is not None and stop_after_batches < 1:
        raise ValueError("stop_after_batches must be positive")

    candidate, public_rows, candidate_file_sha, candidate_logical_sha, inputs_sha = (
        _load_public_staging(
            candidate_manifest_path=candidate_manifest_path,
            staging_dir=staging_dir,
        )
    )
    contract = _checkpoint_contract(
        reviewer_id=reviewer_id,
        review_context_id=review_context_id,
        candidate_file_sha=candidate_file_sha,
        candidate_logical_sha=candidate_logical_sha,
        review_inputs_file_sha=inputs_sha,
        model=model,
        reasoning=reasoning,
        service_tier=service_tier,
        batch_size=batch_size,
        order_mode=order_mode,
    )
    checkpoint = _load_or_create_checkpoint(
        checkpoint_path, contract=contract, resume=resume
    )
    ordered = public_rows if order_mode == "forward" else list(reversed(public_rows))
    completed = dict(checkpoint["records"])
    new_batches = 0
    for offset in range(0, len(ordered), batch_size):
        rows = ordered[offset : offset + batch_size]
        ids = [str(row["review_id"]) for row in rows]
        if all(review_id in completed for review_id in ids):
            continue
        if any(review_id in completed for review_id in ids):
            raise ValueError("review checkpoint contains a partial batch")
        prompt = _review_prompt(ids, reviewer_id)
        result = _isolated_batch(
            scratch_root=scratch_root,
            staging_dir=staging_dir,
            rows=rows,
            prompt=prompt,
            codex_bin=codex_bin,
            model=model,
            reasoning=reasoning,
            service_tier=service_tier,
            timeout_seconds=timeout_seconds,
            batch_runner=batch_runner,
        )
        if not result.ok:
            raise RuntimeError(
                "review batch failed (%s): %s"
                % (result.status, result.error_message)
            )
        annotations = [_annotation_from_result(row) for row in result.records]
        for annotation in annotations:
            completed[str(annotation["review_id"])] = annotation
        usage = result.usage
        checkpoint["records"] = completed
        checkpoint["batches"].append(
            {
                "batch_index": offset // batch_size + 1,
                "review_ids": ids,
                "prompt_sha256": result.provenance.prompt_sha256,
                "output_schema_sha256": result.provenance.output_schema_sha256,
                "elapsed_seconds": result.elapsed_seconds,
                "input_tokens": usage.input_tokens if usage else None,
                "cached_input_tokens": usage.cached_input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
            }
        )
        _write_checkpoint(checkpoint_path, checkpoint)
        new_batches += 1
        if stop_after_batches is not None and new_batches >= stop_after_batches:
            return {
                "status": "running",
                "completed_count": len(completed),
                "remaining_count": 50 - len(completed),
                "checkpoint_path": str(checkpoint_path),
                "output_written": False,
            }

    expected_ids = [str(row["review_id"]) for row in public_rows]
    if set(completed) != set(expected_ids) or len(completed) != 50:
        raise RuntimeError("review checkpoint does not contain all 50 annotations")
    template = review_contract.build_reviewer_annotation_template(
        candidate_dev_manifest_path=candidate_manifest_path,
        reviewer_id=reviewer_id,
        review_context_id=review_context_id,
    )
    template["annotations"] = [copy.deepcopy(completed[value]) for value in expected_ids]
    template["logical_sha256"] = review_contract.annotation_logical_sha256(template)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, draft_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".draft", dir=output_path.parent
    )
    draft_path = Path(draft_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(template) + "\n")
        sealed = review_contract.seal_reviewer_annotation_file(
            candidate_dev_manifest_path=candidate_manifest_path,
            draft_path=draft_path,
            output_path=output_path,
        )
    finally:
        draft_path.unlink(missing_ok=True)

    checkpoint["status"] = "complete"
    checkpoint["output_file_sha256"] = file_sha256(output_path)
    checkpoint["output_logical_sha256"] = sealed["logical_sha256"]
    _write_checkpoint(checkpoint_path, checkpoint)
    totals = {
        key: sum(int(row.get(key) or 0) for row in checkpoint["batches"])
        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    }
    return {
        "status": "complete",
        "completed_count": 50,
        "checkpoint_path": str(checkpoint_path),
        "output_path": str(output_path),
        "output_file_sha256": checkpoint["output_file_sha256"],
        "output_logical_sha256": sealed["logical_sha256"],
        "token_usage": totals,
    }
