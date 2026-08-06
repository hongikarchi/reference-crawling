"""Adjudicate only disagreements from two blinded Divisare axes reviews."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from canonical import divisare_vision_axes_agent_review as agent_review
from canonical import divisare_vision_axes_review as review_contract
from canonical.divisare_image_smoke import canonical_json, file_sha256
from canonical.divisare_vision_axes import (
    AXIS_OUTPUT_SCHEMA,
    compose_axes_prompt,
    normalize_axes_result,
)
from canonical.divisare_vision_axes_benchmark import (
    AXIS_FIELDS,
    EVALUATION_FIELDS,
    FIELD_VALUES,
)
from canonical.divisare_vision_gold_finalize import parse_json_strict
from canonical.divisare_vision_runtime import (
    DEFAULT_MODEL,
    DEFAULT_SERVICE_TIER,
    VisionRuntimeResult,
    run_codex_vision_batch,
)


RUN_VERSION = "divisare-vision-axes-agent-adjudication-v1.0.0"
DEFAULT_REASONING = "high"
DEFAULT_BATCH_SIZE = 5


def adjudication_output_schema() -> dict[str, Any]:
    schema = copy.deepcopy(AXIS_OUTPUT_SCHEMA)
    item = schema["properties"]["results"]["items"]
    item["required"].append("acceptable_labels")
    properties: dict[str, Any] = {}
    for field in EVALUATION_FIELDS:
        allowed = list(FIELD_VALUES[field])
        item_schema: dict[str, Any]
        if field == "in_scope":
            item_schema = {"type": "boolean"}
        else:
            item_schema = {"type": "string", "enum": allowed}
        properties[field] = {"type": "array", "items": item_schema}
    item["properties"]["acceptable_labels"] = {
        "type": "object",
        "additionalProperties": False,
        "required": list(EVALUATION_FIELDS),
        "properties": properties,
    }
    return schema


def _load_contract_and_reviews(
    *, candidate_path: Path, review_paths: Sequence[Path]
) -> tuple[
    review_contract.CandidateReviewContract,
    str,
    list[review_contract.LoadedReview],
]:
    if len(review_paths) != 2:
        raise ValueError("exactly two reviewer annotation files are required")
    raw = candidate_path.read_bytes()
    candidate = parse_json_strict(raw, label="fresh holdout candidate")
    candidate_file_sha = review_contract._sha256_bytes(raw)
    contract = review_contract._validate_candidate_manifest(
        candidate, file_sha=candidate_file_sha
    )
    kwargs = {
        "expected_review_ids": contract.review_order,
        "candidate_file_sha": candidate_file_sha,
        "candidate_logical_sha": contract.logical_sha256,
        "codebook_sha": contract.codebook_sha256,
        "expected_purpose": contract.purpose,
        "expected_development_only": contract.development_only,
    }
    reviews = [
        review_contract.load_reviewer_annotations(path, **kwargs)
        for path in review_paths
    ]
    if reviews[0].reviewer_id == reviews[1].reviewer_id:
        raise ValueError("reviewer IDs must differ")
    return contract, candidate_file_sha, reviews


def find_conflicts(
    contract: review_contract.CandidateReviewContract,
    reviews: Sequence[review_contract.LoadedReview],
) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for review_id in contract.review_order:
        fields = [
            field
            for field in EVALUATION_FIELDS
            if review_contract._automatic_decision(
                reviews[0].annotations[review_id],
                reviews[1].annotations[review_id],
                field,
            )
            is None
        ]
        if fields:
            output[review_id] = fields
    return output


def _conflict_summary(
    review_ids: Sequence[str],
    *,
    conflicts: Mapping[str, Sequence[str]],
    reviews: Sequence[review_contract.LoadedReview],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for review_id in review_ids:
        left = reviews[0].annotations[review_id]
        right = reviews[1].annotations[review_id]
        output[review_id] = {
            "conflict_fields": list(conflicts[review_id]),
            "review_a": {
                "values": {field: left.values[field] for field in conflicts[review_id]},
                "clarity": {field: left.clarity[field] for field in conflicts[review_id]},
                "visible_evidence": left.evidence,
            },
            "review_b": {
                "values": {field: right.values[field] for field in conflicts[review_id]},
                "clarity": {field: right.clarity[field] for field in conflicts[review_id]},
                "visible_evidence": right.evidence,
            },
        }
    return output


def _prompt(
    review_ids: Sequence[str],
    *,
    conflicts: Mapping[str, Sequence[str]],
    reviews: Sequence[review_contract.LoadedReview],
) -> str:
    codebook = compose_axes_prompt(review_ids).replace(
        "Return exactly these fields and only controlled values:",
        "Return these classification fields using only controlled values:",
    )
    summary = canonical_json(
        _conflict_summary(
            review_ids, conflicts=conflicts, reviews=reviews
        )
    )
    return (
        "You are the blinded adjudicator for two prior QA reviews. Re-read the "
        "pixels and make one coherent full classification. Prior choices are "
        "shown only to identify disputed fields; do not resolve a conflict by "
        "majority or compromise. A field is clear when one value is visibly "
        "best, even if the reviewers disagreed. Mark it uncertain only when "
        "two or more controlled values remain genuinely supported. Do not "
        "inspect the filesystem or infer source metadata.\n\n"
        + codebook
        + "\n\nAdd one acceptable_labels object to every result, with exactly "
        + ", ".join(EVALUATION_FIELDS)
        + ". For every clear applicable field, return exactly [the chosen "
        "value]. For every uncertain field, return the chosen value plus all "
        "other visibly plausible controlled values (at least two total). For "
        "an axis whose chosen value is not_applicable, return an empty list. "
        "Scope uncertainty applies to both in_scope and reject_reason: include "
        "both true/false and both none/the plausible rejection reason. Return "
        "full classifications for schema consistency, but only the listed "
        "conflict fields will become adjudications.\n\n"
        "Prior blinded review conflicts:\n"
        + summary
        + "\nOutput JSON only."
    )


def _decision_rows(
    record: Mapping[str, Any], *, conflict_fields: Sequence[str]
) -> list[dict[str, Any]]:
    review_id = str(record.get("asset_id") or "")
    base = {key: value for key, value in record.items() if key != "acceptable_labels"}
    normalized = normalize_axes_result(base, review_id)
    acceptable_raw = record.get("acceptable_labels")
    if not isinstance(acceptable_raw, Mapping) or set(acceptable_raw) != set(
        EVALUATION_FIELDS
    ):
        raise ValueError("adjudicator acceptable_labels fields mismatch")
    uncertain = set(normalized["uncertain_axes"])
    rows: list[dict[str, Any]] = []
    for field in conflict_fields:
        value = normalized[field]
        raw_values = acceptable_raw[field]
        if not isinstance(raw_values, list):
            raise ValueError("adjudicator acceptable labels must be lists")
        ordered = [allowed for allowed in FIELD_VALUES[field] if allowed in raw_values]
        if len(ordered) != len(raw_values):
            raise ValueError("adjudicator acceptable labels are invalid or duplicated")
        uncertainty_key = "scope" if field in {"in_scope", "reject_reason"} else field
        if value == "not_applicable":
            primary = None
            labels: list[Any] = []
            clarity = "not_judgeable"
            if raw_values:
                raise ValueError("not_applicable adjudication must accept no labels")
        elif uncertainty_key in uncertain:
            primary = value
            labels = ordered
            clarity = "boundary"
            if primary not in labels or len(labels) < 2:
                raise ValueError("boundary adjudication needs primary plus an alternative")
        else:
            primary = value
            labels = ordered
            clarity = "clear"
            if labels != [primary]:
                raise ValueError("clear adjudication must accept only its primary")
        evidence = "Visible adjudication: " + str(normalized["evidence"])
        rows.append(
            {
                "review_id": review_id,
                "field": field,
                "primary": primary,
                "acceptable_labels": labels,
                "clarity": clarity,
                "reason": "reviewer_disagreement",
                "evidence": evidence[:500],
            }
        )
    return rows


def run_agent_adjudication(
    *,
    candidate_path: Path,
    review_paths: Sequence[Path],
    staging_dir: Path,
    checkpoint_path: Path,
    output_path: Path,
    adjudicator_id: str,
    codex_bin: str | Path,
    scratch_root: Path,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    timeout_seconds: float = 900,
    resume: bool = False,
    batch_runner: Callable[..., VisionRuntimeResult] = run_codex_vision_batch,
) -> dict[str, Any]:
    candidate_path = candidate_path.resolve()
    review_paths = [path.resolve() for path in review_paths]
    staging_dir = staging_dir.resolve()
    checkpoint_path = checkpoint_path.resolve()
    output_path = output_path.resolve()
    if output_path.exists():
        raise FileExistsError("adjudication output already exists: %s" % output_path)

    contract, candidate_file_sha, reviews = _load_contract_and_reviews(
        candidate_path=candidate_path, review_paths=review_paths
    )
    conflicts = find_conflicts(contract, reviews)
    if not conflicts:
        raise ValueError("there are no reviewer conflicts to adjudicate")
    _candidate, public_rows, staged_candidate_sha, staged_logical_sha, inputs_sha = (
        agent_review._load_public_staging(
            candidate_manifest_path=candidate_path,
            staging_dir=staging_dir,
        )
    )
    if staged_candidate_sha != candidate_file_sha or staged_logical_sha != contract.logical_sha256:
        raise ValueError("review staging binds a different candidate manifest")
    public_by_id = {str(row["review_id"]): row for row in public_rows}
    conflict_ids = [value for value in contract.review_order if value in conflicts]
    input_review_shas = [review.file_sha256 for review in reviews]
    checkpoint_contract = {
        "run_version": RUN_VERSION,
        "candidate_file_sha256": candidate_file_sha,
        "candidate_logical_sha256": contract.logical_sha256,
        "review_file_sha256s": input_review_shas,
        "review_inputs_file_sha256": inputs_sha,
        "adjudicator_id": adjudicator_id,
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "batch_size": DEFAULT_BATCH_SIZE,
        "conflicts": {key: list(value) for key, value in conflicts.items()},
    }
    checkpoint = agent_review._load_or_create_checkpoint(
        checkpoint_path, contract=checkpoint_contract, resume=resume
    )
    completed = dict(checkpoint["records"])
    schema = adjudication_output_schema()
    for offset in range(0, len(conflict_ids), DEFAULT_BATCH_SIZE):
        ids = conflict_ids[offset : offset + DEFAULT_BATCH_SIZE]
        if all(value in completed for value in ids):
            continue
        if any(value in completed for value in ids):
            raise ValueError("adjudication checkpoint contains a partial batch")
        rows = [public_by_id[value] for value in ids]
        prompt = _prompt(ids, conflicts=conflicts, reviews=reviews)
        result = agent_review._isolated_batch(
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
            output_schema=schema,
        )
        if not result.ok:
            raise RuntimeError(
                "adjudication batch failed (%s): %s"
                % (result.status, result.error_message)
            )
        for record in result.records:
            review_id = str(record["asset_id"])
            completed[review_id] = _decision_rows(
                record, conflict_fields=conflicts[review_id]
            )
        usage = result.usage
        checkpoint["records"] = completed
        checkpoint["batches"].append(
            {
                "batch_index": offset // DEFAULT_BATCH_SIZE + 1,
                "review_ids": ids,
                "prompt_sha256": result.provenance.prompt_sha256,
                "output_schema_sha256": result.provenance.output_schema_sha256,
                "elapsed_seconds": result.elapsed_seconds,
                "input_tokens": usage.input_tokens if usage else None,
                "cached_input_tokens": usage.cached_input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
            }
        )
        agent_review._write_checkpoint(checkpoint_path, checkpoint)

    if set(completed) != set(conflict_ids):
        raise RuntimeError("adjudication checkpoint is incomplete")
    decisions = [row for review_id in conflict_ids for row in completed[review_id]]
    if len(decisions) != sum(len(value) for value in conflicts.values()):
        raise RuntimeError("adjudication decision count mismatch")
    payload: dict[str, Any] = {
        "manifest_version": review_contract.ADJUDICATION_VERSION,
        "purpose": contract.purpose,
        "development_only": contract.development_only,
        "independent_human": False,
        "adjudicator_id": adjudicator_id,
        "candidate_dev_manifest_file_sha256": candidate_file_sha,
        "candidate_dev_manifest_logical_sha256": contract.logical_sha256,
        "reviewer_annotation_file_sha256s": input_review_shas,
        "adjudications": decisions,
    }
    payload["logical_sha256"] = review_contract.adjudication_logical_sha256(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".validation", dir=output_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(payload) + "\n")
        review_contract._load_adjudication(
            temporary,
            expected_review_ids=contract.review_order,
            candidate_file_sha=candidate_file_sha,
            candidate_logical_sha=contract.logical_sha256,
            review_file_shas=input_review_shas,
            expected_purpose=contract.purpose,
            expected_development_only=contract.development_only,
        )
        review_contract._write_json_no_clobber(output_path, payload)
    finally:
        temporary.unlink(missing_ok=True)

    checkpoint["status"] = "complete"
    checkpoint["output_file_sha256"] = file_sha256(output_path)
    checkpoint["output_logical_sha256"] = payload["logical_sha256"]
    agent_review._write_checkpoint(checkpoint_path, checkpoint)
    totals = {
        key: sum(int(row.get(key) or 0) for row in checkpoint["batches"])
        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    }
    return {
        "status": "complete",
        "conflict_image_count": len(conflict_ids),
        "adjudication_count": len(decisions),
        "output_path": str(output_path),
        "output_file_sha256": checkpoint["output_file_sha256"],
        "output_logical_sha256": payload["logical_sha256"],
        "token_usage": totals,
    }
