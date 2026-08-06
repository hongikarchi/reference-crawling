"""Blinded double-review and gold finalization for Divisare image axes.

Reviewer files expose only the opaque ``review_id`` and pixel-derived axis
decisions.  This module joins those decisions back to either the frozen
development set or the fresh one-shot holdout only after both reviews are
complete.  The two manifest contracts remain distinct and strictly validated.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from canonical.divisare_image_smoke import file_sha256
from canonical import divisare_vision_axes_devset as devset_contract
from canonical.divisare_vision_axes import (
    AXIS_CONTRACT_VERSION,
    AXIS_OUTPUT_SCHEMA,
    AXIS_PROMPT_VERSION,
    compose_axes_prompt,
    normalize_axes_result,
)
from canonical.divisare_vision_axes_benchmark import (
    AXIS_FIELDS,
    AXIS_GOLD_MANIFEST_VERSION,
    CLARITIES,
    DEVELOPMENT_PURPOSE,
    EVALUATION_FIELDS,
    FIELD_VALUES,
    FROZEN_SAMPLE_COUNT,
    GoldDecision,
    SELECTION_POLICY_VERSION,
    SUPPORTED_LIMITS,
    _gold_row_for_derivation,
    axis_gold_logical_sha256,
    axis_gold_manifest_sha256,
)
from canonical.divisare_vision_axes import derive_classification
from canonical.divisare_vision_gold_finalize import parse_json_strict


REVIEW_ANNOTATION_VERSION = "divisare-vision-axes-review-annotations-v1.0.0"
ADJUDICATION_VERSION = "divisare-vision-axes-adjudication-v1.0.0"
GOLD_FINALIZER_VERSION = "divisare-vision-axes-gold-finalizer-v1.0.0"
FRESH_HOLDOUT_PURPOSE = "fresh_blind_one_shot_final_prompt_holdout"
HOLDOUT_AXIS_GOLD_MANIFEST_VERSION = (
    "divisare-vision-axes-fresh-holdout-gold-v1.0.0"
)
HOLDOUT_GOLD_FINALIZER_VERSION = (
    "divisare-vision-axes-fresh-holdout-gold-finalizer-v1.0.0"
)
SOURCE_VISIBILITY = "pixels_and_opaque_id_only"
FIXED_REVIEW_LONG_EDGE = 1024

ANNOTATION_FIELDS = (
    "review_id",
    "in_scope",
    "reject_reason",
    *AXIS_FIELDS,
    "uncertain_axes",
    "resolution_insufficient",
    "evidence",
    "clarity",
)
ANNOTATION_TOP_LEVEL_FIELDS = frozenset(
    {
        "manifest_version",
        "purpose",
        "development_only",
        "independent_human",
        "reviewer_id",
        "review_context_id",
        "source_visibility",
        "image_long_edge",
        "candidate_dev_manifest_file_sha256",
        "candidate_dev_manifest_logical_sha256",
        "codebook_sha256",
        "axis_contract_version",
        "axis_prompt_version",
        "annotations",
        "logical_sha256",
    }
)
ADJUDICATION_TOP_LEVEL_FIELDS = frozenset(
    {
        "manifest_version",
        "purpose",
        "development_only",
        "independent_human",
        "adjudicator_id",
        "candidate_dev_manifest_file_sha256",
        "candidate_dev_manifest_logical_sha256",
        "reviewer_annotation_file_sha256s",
        "adjudications",
        "logical_sha256",
    }
)
ADJUDICATION_FIELDS = frozenset(
    {
        "review_id",
        "field",
        "primary",
        "acceptable_labels",
        "clarity",
        "reason",
        "evidence",
    }
)
ADJUDICATION_REASONS = frozenset(
    {"reviewer_disagreement", "gold_invariant_resolution"}
)


@dataclass(frozen=True)
class ReviewerAnnotation:
    reviewer_id: str
    review_context_id: str
    review_id: str
    values: Mapping[str, Any]
    clarity: Mapping[str, str]
    uncertain_axes: tuple[str, ...]
    resolution_insufficient: bool
    evidence: str


@dataclass(frozen=True)
class LoadedReview:
    reviewer_id: str
    review_context_id: str
    file_sha256: str
    logical_sha256: str
    annotations: Mapping[str, ReviewerAnnotation]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CandidateReviewContract:
    samples: tuple[Mapping[str, Any], ...]
    review_order: tuple[str, ...]
    logical_sha256: str
    codebook_sha256: str
    purpose: str
    development_only: bool
    kind: str
    gold_manifest_version: str
    finalizer_version: str
    selection_policy_version: str
    selection_salt: str


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _without_field(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    clean = dict(payload)
    clean.pop(field, None)
    return clean


def annotation_logical_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_without_field(payload, "logical_sha256"))


def adjudication_logical_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_without_field(payload, "logical_sha256"))


def axis_output_schema_sha256() -> str:
    return _sha256_value(AXIS_OUTPUT_SCHEMA)


def axes_review_codebook_sha256() -> str:
    """Hash the exact controlled vocabulary shown to both reviewers."""

    return _sha256_value(
        {
            "review_annotation_version": REVIEW_ANNOTATION_VERSION,
            "axis_contract_version": AXIS_CONTRACT_VERSION,
            "axis_prompt_version": AXIS_PROMPT_VERSION,
            "prompt_reference_sha256": _sha256_bytes(
                compose_axes_prompt(["axis-reference0000"]).encode("utf-8")
            ),
            "evaluation_fields": list(EVALUATION_FIELDS),
            "field_values": {
                field: list(FIELD_VALUES[field]) for field in EVALUATION_FIELDS
            },
            "clarities": list(CLARITIES),
            "source_visibility": SOURCE_VISIBILITY,
            "image_long_edge": FIXED_REVIEW_LONG_EDGE,
            "not_applicable_clarity": "not_judgeable",
            "unknown_is_applicable": True,
            "out_of_scope_medium_is_judged": True,
        }
    )


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _require_nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_sha(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _candidate_samples(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = payload.get("samples", payload.get("audit_samples"))
    if not isinstance(raw, list) or len(raw) != FROZEN_SAMPLE_COUNT:
        raise ValueError("candidate development manifest must contain exactly 50 samples")
    samples: list[Mapping[str, Any]] = []
    for index, value in enumerate(raw, 1):
        sample = _require_mapping(value, f"candidate sample {index}")
        rank = sample.get("sample_rank", sample.get("audit_rank"))
        if rank != index:
            raise ValueError(f"candidate sample rank mismatch at {index}")
        samples.append(sample)
    return samples


def _candidate_review_id(sample: Mapping[str, Any], rank: int) -> str:
    value = sample.get("review_id", sample.get("opaque_review_id"))
    review_id = _require_nonempty_string(value, f"candidate sample {rank} review_id")
    if not review_id.startswith("axis-"):
        raise ValueError(f"candidate sample {rank} review_id is not opaque")
    return review_id


def _candidate_logical_sha(payload: Mapping[str, Any]) -> str:
    for key in ("logical_sha256", "manifest_sha256"):
        value = payload.get(key)
        if isinstance(value, str) and len(value) == 64:
            return _require_sha(value, f"candidate development {key}")
    raise ValueError("candidate development manifest has no logical SHA")


def _candidate_review_order(payload: Mapping[str, Any]) -> list[str]:
    raw = payload.get("review_rows")
    if not isinstance(raw, list) or len(raw) != FROZEN_SAMPLE_COUNT:
        raise ValueError("candidate development review_rows must contain exactly 50 rows")
    review_ids: list[str] = []
    for rank, value in enumerate(raw, 1):
        row = _require_mapping(value, f"candidate review row {rank}")
        if set(row) != {"review_rank", "review_id"} or row.get("review_rank") != rank:
            raise ValueError(f"candidate review row mismatch at {rank}")
        review_ids.append(_candidate_review_id(row, rank))
    if len(set(review_ids)) != len(review_ids):
        raise ValueError("candidate review_rows contain duplicate review IDs")
    return review_ids


def _validate_candidate_manifest(
    payload: Mapping[str, Any], *, file_sha: str
) -> CandidateReviewContract:
    is_holdout = payload.get("independent_holdout") is True
    if is_holdout:
        # Lazy import avoids the intentional holdout-selection -> review
        # dependency used to freeze the codebook and output-schema hashes.
        from canonical import divisare_vision_axes_holdout_selection as holdout_contract

        holdout_contract.validate_selection_manifest(payload)
        selection = _require_mapping(
            payload.get("selection_contract"), "fresh holdout selection contract"
        )
        purpose = _require_nonempty_string(
            selection.get("purpose"), "fresh holdout purpose"
        )
        if purpose != FRESH_HOLDOUT_PURPOSE:
            raise ValueError("fresh holdout purpose mismatch")
        if payload.get("development_only") is not False:
            raise ValueError("fresh holdout must not be marked development-only")
        kind = "fresh_holdout"
        development_only = False
        gold_manifest_version = HOLDOUT_AXIS_GOLD_MANIFEST_VERSION
        finalizer_version = HOLDOUT_GOLD_FINALIZER_VERSION
        selection_policy_version = _require_nonempty_string(
            selection.get("selector_version"), "fresh holdout selector version"
        )
    else:
        devset_contract.validate_devset_manifest(payload)
        if payload.get("development_only") is not True:
            raise ValueError("development candidate must be marked development-only")
        selection = _require_mapping(
            payload.get("selection_policy", payload.get("selection_contract")),
            "candidate selection policy",
        )
        purpose = DEVELOPMENT_PURPOSE
        kind = "development"
        development_only = True
        gold_manifest_version = AXIS_GOLD_MANIFEST_VERSION
        finalizer_version = GOLD_FINALIZER_VERSION
        selection_policy_version = SELECTION_POLICY_VERSION

    samples = _candidate_samples(payload)
    review_order = _candidate_review_order(payload)
    logical_sha = _candidate_logical_sha(payload)
    codebook_sha = axes_review_codebook_sha256()
    seen: set[str] = set()
    for rank, sample in enumerate(samples, 1):
        review_id = _candidate_review_id(sample, rank)
        if review_id in seen:
            raise ValueError(f"duplicate candidate review_id: {review_id}")
        seen.add(review_id)
    if set(review_order) != seen:
        raise ValueError("candidate review_rows and audit_samples review IDs differ")
    _require_sha(file_sha, "candidate development manifest file SHA")
    selection_salt = _require_nonempty_string(
        selection.get("selection_salt", selection.get("blind_id_version")),
        "selection_salt",
    )
    return CandidateReviewContract(
        samples=tuple(samples),
        review_order=tuple(review_order),
        logical_sha256=logical_sha,
        codebook_sha256=codebook_sha,
        purpose=purpose,
        development_only=development_only,
        kind=kind,
        gold_manifest_version=gold_manifest_version,
        finalizer_version=finalizer_version,
        selection_policy_version=selection_policy_version,
        selection_salt=selection_salt,
    )


def _normalize_annotation(
    raw: Any, *, reviewer_id: str, review_context_id: str
) -> ReviewerAnnotation:
    row = _require_mapping(raw, "review annotation")
    if set(row) != set(ANNOTATION_FIELDS):
        missing = sorted(set(ANNOTATION_FIELDS) - set(row))
        extra = sorted(set(row) - set(ANNOTATION_FIELDS))
        raise ValueError(
            f"review annotation fields mismatch: missing={missing}, unexpected={extra}"
        )
    review_id = _require_nonempty_string(row.get("review_id"), "annotation review_id")
    clarity_raw = _require_mapping(row.get("clarity"), f"{review_id} clarity")
    if set(clarity_raw) != set(EVALUATION_FIELDS):
        raise ValueError(f"{review_id} clarity must contain exactly the scored fields")
    clarity: dict[str, str] = {}
    for field in EVALUATION_FIELDS:
        value = clarity_raw.get(field)
        if value not in CLARITIES:
            raise ValueError(f"{review_id} {field} clarity is invalid")
        clarity[field] = str(value)

    model_row = {
        "asset_id": review_id,
        "in_scope": row["in_scope"],
        "reject_reason": row["reject_reason"],
        **{field: row[field] for field in AXIS_FIELDS},
        "uncertain_axes": row["uncertain_axes"],
        "resolution_insufficient": row["resolution_insufficient"],
        "evidence": row["evidence"],
    }
    normalized = normalize_axes_result(model_row, review_id)
    values = {field: normalized[field] for field in EVALUATION_FIELDS}

    for field in AXIS_FIELDS:
        value = values[field]
        field_clarity = clarity[field]
        if value == "not_applicable" and field_clarity != "not_judgeable":
            raise ValueError(
                f"{review_id} {field}=not_applicable requires clarity=not_judgeable"
            )
        if value != "not_applicable" and field_clarity == "not_judgeable":
            raise ValueError(
                f"{review_id} applicable {field} cannot be not_judgeable"
            )
    if clarity["in_scope"] == "not_judgeable":
        raise ValueError(f"{review_id} in_scope cannot be not_judgeable")
    if clarity["reject_reason"] == "not_judgeable":
        raise ValueError(f"{review_id} reject_reason cannot be not_judgeable")
    uncertain = set(normalized["uncertain_axes"])
    scope_boundary = (
        clarity["in_scope"] == "boundary"
        or clarity["reject_reason"] == "boundary"
    )
    if ("scope" in uncertain) != scope_boundary:
        raise ValueError(
            f"{review_id} scope boundary clarity and uncertain_axes must agree"
        )
    for field in AXIS_FIELDS:
        if (field in uncertain) != (clarity[field] == "boundary"):
            raise ValueError(
                f"{review_id} {field} boundary clarity and uncertain_axes must agree"
            )
    if not normalized["in_scope"]:
        for field in AXIS_FIELDS[1:]:
            if clarity[field] != "not_judgeable":
                raise ValueError(
                    f"{review_id} out-of-scope semantic axes must be not_judgeable"
                )

    return ReviewerAnnotation(
        reviewer_id=reviewer_id,
        review_context_id=review_context_id,
        review_id=review_id,
        values=values,
        clarity=clarity,
        uncertain_axes=tuple(normalized["uncertain_axes"]),
        resolution_insufficient=bool(normalized["resolution_insufficient"]),
        evidence=str(normalized["evidence"]),
    )


def load_reviewer_annotations(
    path: Path,
    *,
    expected_review_ids: Sequence[str],
    candidate_file_sha: str,
    candidate_logical_sha: str,
    codebook_sha: str,
    expected_purpose: str = DEVELOPMENT_PURPOSE,
    expected_development_only: bool = True,
) -> LoadedReview:
    raw_bytes = path.read_bytes()
    payload = parse_json_strict(raw_bytes, label=f"review annotations {path.name}")
    if set(payload) != ANNOTATION_TOP_LEVEL_FIELDS:
        missing = sorted(ANNOTATION_TOP_LEVEL_FIELDS - set(payload))
        extra = sorted(set(payload) - ANNOTATION_TOP_LEVEL_FIELDS)
        raise ValueError(
            f"review annotation manifest fields mismatch: missing={missing}, unexpected={extra}"
        )
    if payload.get("manifest_version") != REVIEW_ANNOTATION_VERSION:
        raise ValueError("review annotation manifest version mismatch")
    if payload.get("purpose") != expected_purpose:
        raise ValueError("review annotation purpose mismatch")
    if payload.get("development_only") is not expected_development_only:
        raise ValueError("review annotation development_only mismatch")
    if payload.get("independent_human") is not False:
        raise ValueError("agent review must record independent_human=false")
    if payload.get("source_visibility") != SOURCE_VISIBILITY:
        raise ValueError("review annotations must be pixels-and-opaque-ID only")
    if payload.get("image_long_edge") != FIXED_REVIEW_LONG_EDGE:
        raise ValueError("review annotations must use 1024-pixel images")
    if payload.get("candidate_dev_manifest_file_sha256") != candidate_file_sha:
        raise ValueError("review annotations bind the wrong candidate file SHA")
    if payload.get("candidate_dev_manifest_logical_sha256") != candidate_logical_sha:
        raise ValueError("review annotations bind the wrong candidate logical SHA")
    if payload.get("codebook_sha256") != codebook_sha:
        raise ValueError("review annotations bind the wrong codebook SHA")
    if payload.get("axis_contract_version") != AXIS_CONTRACT_VERSION:
        raise ValueError("review annotations axis contract version mismatch")
    if payload.get("axis_prompt_version") != AXIS_PROMPT_VERSION:
        raise ValueError("review annotations axis prompt version mismatch")
    if payload.get("logical_sha256") != annotation_logical_sha256(payload):
        raise ValueError("review annotation logical SHA mismatch")

    reviewer_id = _require_nonempty_string(payload.get("reviewer_id"), "reviewer_id")
    context_id = _require_nonempty_string(
        payload.get("review_context_id"), "review_context_id"
    )
    raw_annotations = payload.get("annotations")
    if not isinstance(raw_annotations, list) or len(raw_annotations) != len(
        expected_review_ids
    ):
        raise ValueError("review annotations must contain all 50 rows exactly once")
    annotations: dict[str, ReviewerAnnotation] = {}
    for raw in raw_annotations:
        annotation = _normalize_annotation(
            raw, reviewer_id=reviewer_id, review_context_id=context_id
        )
        if annotation.review_id in annotations:
            raise ValueError(f"duplicate review annotation: {annotation.review_id}")
        annotations[annotation.review_id] = annotation
    if list(annotations) != list(expected_review_ids):
        missing = sorted(set(expected_review_ids) - set(annotations))
        extra = sorted(set(annotations) - set(expected_review_ids))
        if missing or extra:
            raise ValueError(
                f"review IDs do not match candidate manifest: missing={missing}, extra={extra}"
            )
        raise ValueError("review annotations must preserve frozen N10/N20/N50 order")
    return LoadedReview(
        reviewer_id=reviewer_id,
        review_context_id=context_id,
        file_sha256=_sha256_bytes(raw_bytes),
        logical_sha256=str(payload["logical_sha256"]),
        annotations=annotations,
        payload=payload,
    )


def _normalize_gold_decision(raw: Mapping[str, Any], field: str, label: str) -> dict[str, Any]:
    if set(raw) != {"primary", "acceptable_labels", "clarity"}:
        raise ValueError(f"{label} must contain primary/acceptable_labels/clarity")
    clarity = raw.get("clarity")
    if clarity not in CLARITIES:
        raise ValueError(f"{label} clarity is invalid")
    primary = raw.get("primary")
    acceptable = raw.get("acceptable_labels")
    if not isinstance(acceptable, list):
        raise ValueError(f"{label} acceptable_labels must be a list")
    if len(acceptable) != len({_canonical_json_bytes(value) for value in acceptable}):
        raise ValueError(f"{label} acceptable_labels must be unique")
    allowed = FIELD_VALUES[field]
    if clarity == "not_judgeable":
        if primary is not None or acceptable:
            raise ValueError(f"{label} not_judgeable must use null and []")
        return {"primary": None, "acceptable_labels": [], "clarity": clarity}
    if primary not in allowed or primary not in acceptable:
        raise ValueError(f"{label} primary/acceptable_labels are invalid")
    if any(value not in allowed for value in acceptable):
        raise ValueError(f"{label} acceptable_labels contain an invalid value")
    if clarity == "clear" and acceptable != [primary]:
        raise ValueError(f"{label} clear decision must accept only primary")
    if clarity == "boundary" and len(acceptable) < 2:
        raise ValueError(f"{label} boundary decision needs at least two values")
    ordered = [value for value in allowed if value in acceptable]
    return {"primary": primary, "acceptable_labels": ordered, "clarity": clarity}


def _load_adjudication(
    path: Path | None,
    *,
    expected_review_ids: Sequence[str],
    candidate_file_sha: str,
    candidate_logical_sha: str,
    review_file_shas: Sequence[str],
    expected_purpose: str,
    expected_development_only: bool,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any], str]:
    if path is None:
        absent = {"provided": False, "status": "not_required"}
        return {}, absent, _sha256_value(absent)
    raw_bytes = path.read_bytes()
    payload = parse_json_strict(raw_bytes, label=f"adjudication {path.name}")
    if set(payload) != ADJUDICATION_TOP_LEVEL_FIELDS:
        missing = sorted(ADJUDICATION_TOP_LEVEL_FIELDS - set(payload))
        extra = sorted(set(payload) - ADJUDICATION_TOP_LEVEL_FIELDS)
        raise ValueError(
            f"adjudication manifest fields mismatch: missing={missing}, unexpected={extra}"
        )
    if payload.get("manifest_version") != ADJUDICATION_VERSION:
        raise ValueError("adjudication manifest version mismatch")
    if payload.get("purpose") != expected_purpose:
        raise ValueError("adjudication purpose mismatch")
    if payload.get("development_only") is not expected_development_only:
        raise ValueError("adjudication development_only mismatch")
    if payload.get("independent_human") is not False:
        raise ValueError("agent adjudication must record independent_human=false")
    if payload.get("candidate_dev_manifest_file_sha256") != candidate_file_sha:
        raise ValueError("adjudication binds the wrong candidate file SHA")
    if payload.get("candidate_dev_manifest_logical_sha256") != candidate_logical_sha:
        raise ValueError("adjudication binds the wrong candidate logical SHA")
    if payload.get("reviewer_annotation_file_sha256s") != list(review_file_shas):
        raise ValueError("adjudication binds the wrong reviewer annotation files")
    if payload.get("logical_sha256") != adjudication_logical_sha256(payload):
        raise ValueError("adjudication logical SHA mismatch")
    _require_nonempty_string(payload.get("adjudicator_id"), "adjudicator_id")
    raw_rows = payload.get("adjudications")
    if not isinstance(raw_rows, list):
        raise ValueError("adjudications must be a list")
    allowed_review_ids = set(expected_review_ids)
    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    normalized_rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        row = _require_mapping(raw, "adjudication row")
        if set(row) != ADJUDICATION_FIELDS:
            raise ValueError("adjudication row fields mismatch")
        review_id = _require_nonempty_string(row.get("review_id"), "adjudication review_id")
        if review_id not in allowed_review_ids:
            raise ValueError(f"unknown adjudication review_id: {review_id}")
        field = row.get("field")
        if field not in EVALUATION_FIELDS:
            raise ValueError(f"unsupported adjudication field: {field}")
        key = (review_id, str(field))
        if key in decisions:
            raise ValueError(f"duplicate adjudication: {review_id}/{field}")
        reason = row.get("reason")
        if reason not in ADJUDICATION_REASONS:
            raise ValueError(f"invalid adjudication reason: {reason}")
        evidence = _require_nonempty_string(row.get("evidence"), "adjudication evidence")
        if len(evidence) > 500:
            raise ValueError("adjudication evidence must be at most 500 characters")
        decision = _normalize_gold_decision(
            {
                "primary": row.get("primary"),
                "acceptable_labels": row.get("acceptable_labels"),
                "clarity": row.get("clarity"),
            },
            str(field),
            f"{review_id}/{field}",
        )
        decisions[key] = decision
        normalized_rows.append(dict(row))
    audit = {
        "provided": True,
        "filename": path.name,
        "file_sha256": _sha256_bytes(raw_bytes),
        "logical_sha256": payload["logical_sha256"],
        "adjudicator_id": payload["adjudicator_id"],
        "rows": normalized_rows,
    }
    return decisions, audit, _sha256_bytes(raw_bytes)


def _automatic_decision(
    left: ReviewerAnnotation, right: ReviewerAnnotation, field: str
) -> dict[str, Any] | None:
    if left.values[field] != right.values[field] or left.clarity[field] != right.clarity[field]:
        return None
    value = left.values[field]
    clarity = left.clarity[field]
    if clarity == "clear":
        return {"primary": value, "acceptable_labels": [value], "clarity": "clear"}
    if clarity == "not_judgeable":
        return {"primary": None, "acceptable_labels": [], "clarity": "not_judgeable"}
    return None


def _review_snapshot(annotation: ReviewerAnnotation) -> dict[str, Any]:
    return {
        "reviewer_id": annotation.reviewer_id,
        "review_context_id": annotation.review_context_id,
        "values": dict(annotation.values),
        "clarity": dict(annotation.clarity),
        "uncertain_axes": list(annotation.uncertain_axes),
        "resolution_insufficient": annotation.resolution_insufficient,
        "evidence": annotation.evidence,
    }


def _source_section(sample: Mapping[str, Any]) -> Mapping[str, Any]:
    source = sample.get("source_identity")
    if isinstance(source, Mapping):
        return source
    return sample


def _evidence_section(sample: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = sample.get("image_evidence")
    if isinstance(evidence, Mapping):
        return evidence
    return sample


def _copy_source_identity(sample: Mapping[str, Any]) -> dict[str, Any]:
    source = _source_section(sample)
    selection_audit = sample.get("selection_audit")
    if not isinstance(selection_audit, Mapping):
        selection_audit = {}
    required = (
        "candidate_id",
        "asset_key",
        "article_id",
        "building_id",
        "generation_group",
        "url_generation",
        "request_url",
    )
    output: dict[str, Any] = {}
    for field in required:
        value = source.get(field)
        if value is None and field == "candidate_id":
            value = sample.get(field)
        if value is None and field == "generation_group":
            value = selection_audit.get("generation_group")
        if value is None:
            raise ValueError(f"candidate sample missing source identity field {field}")
        output[field] = value
    return output


def _copy_image_evidence(sample: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _evidence_section(sample)
    required = ("content_sha256", "pixel_sha256", "phash_256")
    output: dict[str, Any] = {}
    for field in required:
        value = evidence.get(field)
        if value is None:
            raise ValueError(f"candidate sample missing image evidence field {field}")
        output[field] = _require_sha(value, f"candidate image evidence {field}")
    return output


def _provenance_sha(
    provenance: Mapping[str, Any], *keys: str, name: str
) -> str:
    for key in keys:
        if provenance.get(key) is not None:
            return _require_sha(provenance.get(key), f"candidate provenance {key}")
    raise ValueError(f"candidate provenance missing {name}")


def _gold_provenance(
    candidate: Mapping[str, Any],
    *,
    contract: CandidateReviewContract,
    candidate_file_sha: str,
    candidate_logical_sha: str,
    codebook_sha: str,
    reviews: Sequence[LoadedReview],
    adjudication_sha: str,
) -> dict[str, Any]:
    source = _require_mapping(candidate.get("provenance"), "candidate provenance")
    candidate_self_sha = candidate.get("manifest_sha256", candidate_logical_sha)
    if contract.kind == "fresh_holdout":
        # Preserve the frozen selection provenance byte-for-value at the same
        # nesting, then append only review/finalization lineage.
        output = deepcopy(dict(source))
        additions = {
            "candidate_holdout_manifest_sha256": _require_sha(
                candidate_self_sha, "candidate holdout manifest SHA"
            ),
            "candidate_holdout_manifest_file_sha256": candidate_file_sha,
            "candidate_holdout_manifest_logical_sha256": candidate_logical_sha,
            "codebook_sha256": codebook_sha,
            "axis_output_schema_sha256": axis_output_schema_sha256(),
            "adjudication_sha256": adjudication_sha,
            "reviewer_annotation_sha256s": [
                review.file_sha256 for review in reviews
            ],
            "reviewer": "+".join(review.reviewer_id for review in reviews),
            "independent_human": False,
            "axis_contract_version": AXIS_CONTRACT_VERSION,
            "axis_prompt_version": AXIS_PROMPT_VERSION,
        }
        collisions = set(output) & set(additions)
        if collisions:
            raise ValueError(
                "fresh holdout provenance collides with review lineage: "
                + ", ".join(sorted(collisions))
            )
        output.update(additions)
        return output

    parent_candidate = _require_mapping(
        source.get("parent_candidate_manifest"),
        "candidate provenance parent_candidate_manifest",
    )
    parent_reviewed = _require_mapping(
        source.get("parent_reviewed_pool"),
        "candidate provenance parent_reviewed_pool",
    )
    old_gold = _require_mapping(
        source.get("old_gold_manifest"),
        "candidate provenance old_gold_manifest",
    )
    old_n100 = _require_mapping(
        source.get("old_n100_benchmark"),
        "candidate provenance old_n100_benchmark",
    )
    return {
        "source_db_sha256": _provenance_sha(
            source, "source_db_sha256", name="source DB SHA"
        ),
        "candidate_dev_manifest_sha256": _require_sha(
            candidate_self_sha, "candidate development manifest SHA"
        ),
        "candidate_dev_manifest_file_sha256": candidate_file_sha,
        "candidate_dev_manifest_logical_sha256": candidate_logical_sha,
        "parent_candidate_manifest_sha256": _provenance_sha(
            parent_candidate,
            "manifest_sha256",
            name="parent candidate manifest SHA",
        ),
        "parent_candidate_manifest_file_sha256": _provenance_sha(
            parent_candidate,
            "file_sha256",
            name="parent candidate file SHA",
        ),
        "parent_reviewed_pool_sha256": _provenance_sha(
            parent_reviewed,
            "reviewed_pool_sha256",
            name="parent reviewed pool SHA",
        ),
        "parent_reviewed_pool_file_sha256": _provenance_sha(
            parent_reviewed,
            "file_sha256",
            name="parent reviewed pool file SHA",
        ),
        "old_gold_manifest_sha256": _provenance_sha(
            old_gold, "gold_manifest_sha256", name="old gold manifest SHA"
        ),
        "old_gold_manifest_file_sha256": _provenance_sha(
            old_gold, "file_sha256", name="old gold file SHA"
        ),
        "old_n100_db_file_sha256": _provenance_sha(
            old_n100, "file_sha256", name="old N100 DB file SHA"
        ),
        "old_n100_db_logical_sha256": _provenance_sha(
            old_n100, "logical_sha256", name="old N100 DB logical SHA"
        ),
        "codebook_sha256": codebook_sha,
        "axis_output_schema_sha256": axis_output_schema_sha256(),
        "adjudication_sha256": adjudication_sha,
        "reviewer_annotation_sha256s": [review.file_sha256 for review in reviews],
        "reviewer": "+".join(review.reviewer_id for review in reviews),
        "independent_human": False,
        "axis_contract_version": AXIS_CONTRACT_VERSION,
        "axis_prompt_version": AXIS_PROMPT_VERSION,
    }


def _subset_membership(rank: int) -> list[str]:
    return [
        name
        for name, limit in (("N10", 10), ("N20", 20), ("N50", 50))
        if rank <= limit
    ]


def _write_json_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite existing output: {path}"
            ) from exc
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_reviewer_annotation_template(
    *,
    candidate_dev_manifest_path: Path,
    reviewer_id: str,
    review_context_id: str,
) -> dict[str, Any]:
    """Build an opaque, source-free annotation skeleton in public review order."""

    reviewer_id = _require_nonempty_string(reviewer_id, "reviewer_id")
    review_context_id = _require_nonempty_string(
        review_context_id, "review_context_id"
    )
    candidate_bytes = candidate_dev_manifest_path.read_bytes()
    candidate = parse_json_strict(
        candidate_bytes, label="axes candidate development manifest"
    )
    candidate_file_sha = _sha256_bytes(candidate_bytes)
    contract = _validate_candidate_manifest(candidate, file_sha=candidate_file_sha)
    annotations = [
        {
            "review_id": review_id,
            "in_scope": None,
            "reject_reason": None,
            **{field: None for field in AXIS_FIELDS},
            "uncertain_axes": [],
            "resolution_insufficient": None,
            "evidence": None,
            "clarity": {field: None for field in EVALUATION_FIELDS},
        }
        for review_id in contract.review_order
    ]
    payload: dict[str, Any] = {
        "manifest_version": REVIEW_ANNOTATION_VERSION,
        "purpose": contract.purpose,
        "development_only": contract.development_only,
        "independent_human": False,
        "reviewer_id": reviewer_id,
        "review_context_id": review_context_id,
        "source_visibility": SOURCE_VISIBILITY,
        "image_long_edge": FIXED_REVIEW_LONG_EDGE,
        "candidate_dev_manifest_file_sha256": candidate_file_sha,
        "candidate_dev_manifest_logical_sha256": contract.logical_sha256,
        "codebook_sha256": contract.codebook_sha256,
        "axis_contract_version": AXIS_CONTRACT_VERSION,
        "axis_prompt_version": AXIS_PROMPT_VERSION,
        "annotations": annotations,
    }
    payload["logical_sha256"] = annotation_logical_sha256(payload)
    return payload


def write_reviewer_annotation_template(
    *,
    candidate_dev_manifest_path: Path,
    reviewer_id: str,
    review_context_id: str,
    output_path: Path,
) -> dict[str, Any]:
    payload = build_reviewer_annotation_template(
        candidate_dev_manifest_path=candidate_dev_manifest_path,
        reviewer_id=reviewer_id,
        review_context_id=review_context_id,
    )
    _write_json_no_clobber(output_path, payload)
    return payload


def seal_reviewer_annotation_file(
    *,
    candidate_dev_manifest_path: Path,
    draft_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Rehash and validate a filled skeleton, writing a new immutable file."""

    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_path}")
    draft = parse_json_strict(draft_path.read_bytes(), label=f"review draft {draft_path.name}")
    if set(draft) != ANNOTATION_TOP_LEVEL_FIELDS:
        raise ValueError("review draft envelope fields mismatch")
    expected = build_reviewer_annotation_template(
        candidate_dev_manifest_path=candidate_dev_manifest_path,
        reviewer_id=_require_nonempty_string(draft.get("reviewer_id"), "reviewer_id"),
        review_context_id=_require_nonempty_string(
            draft.get("review_context_id"), "review_context_id"
        ),
    )
    immutable_fields = ANNOTATION_TOP_LEVEL_FIELDS - {"annotations", "logical_sha256"}
    for field in immutable_fields:
        if draft.get(field) != expected.get(field):
            raise ValueError(f"review draft changed immutable envelope field: {field}")
    sealed = dict(draft)
    sealed["logical_sha256"] = annotation_logical_sha256(sealed)

    candidate_file_sha = str(sealed["candidate_dev_manifest_file_sha256"])
    candidate_logical_sha = str(sealed["candidate_dev_manifest_logical_sha256"])
    codebook_sha = str(sealed["codebook_sha256"])
    review_order = [row["review_id"] for row in expected["annotations"]]
    # Validate the completed values before making the output visible.
    temporary_payload_path = output_path.parent / f".{output_path.name}.validation-only"
    _write_json_no_clobber(temporary_payload_path, sealed)
    try:
        load_reviewer_annotations(
            temporary_payload_path,
            expected_review_ids=review_order,
            candidate_file_sha=candidate_file_sha,
            candidate_logical_sha=candidate_logical_sha,
            codebook_sha=codebook_sha,
            expected_purpose=str(expected["purpose"]),
            expected_development_only=bool(expected["development_only"]),
        )
        _write_json_no_clobber(output_path, sealed)
    finally:
        temporary_payload_path.unlink(missing_ok=True)
    return sealed


def finalize_axes_gold_files(
    *,
    candidate_dev_manifest_path: Path,
    reviewer_annotation_paths: Sequence[Path],
    output_path: Path,
    adjudication_path: Path | None = None,
) -> dict[str, Any]:
    """Validate two blinded reviews and write one immutable axes gold file."""

    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_path}")
    if len(reviewer_annotation_paths) != 2:
        raise ValueError("exactly two independent reviewer annotation files are required")
    candidate_bytes = candidate_dev_manifest_path.read_bytes()
    candidate = parse_json_strict(candidate_bytes, label="axes candidate development manifest")
    candidate_file_sha = _sha256_bytes(candidate_bytes)
    contract = _validate_candidate_manifest(candidate, file_sha=candidate_file_sha)
    sample_review_ids = [
        _candidate_review_id(sample, rank)
        for rank, sample in enumerate(contract.samples, 1)
    ]

    reviews = [
        load_reviewer_annotations(
            path,
            expected_review_ids=contract.review_order,
            candidate_file_sha=candidate_file_sha,
            candidate_logical_sha=contract.logical_sha256,
            codebook_sha=contract.codebook_sha256,
            expected_purpose=contract.purpose,
            expected_development_only=contract.development_only,
        )
        for path in reviewer_annotation_paths
    ]
    if reviews[0].reviewer_id == reviews[1].reviewer_id:
        raise ValueError("reviewer_id must differ between independent reviews")
    if reviews[0].review_context_id == reviews[1].review_context_id:
        raise ValueError("review_context_id must differ between independent reviews")
    if reviews[0].file_sha256 == reviews[1].file_sha256:
        raise ValueError("independent reviewer annotation files must differ")

    overlay, adjudication_audit, adjudication_sha = _load_adjudication(
        adjudication_path,
        expected_review_ids=sample_review_ids,
        candidate_file_sha=candidate_file_sha,
        candidate_logical_sha=contract.logical_sha256,
        review_file_shas=[review.file_sha256 for review in reviews],
        expected_purpose=contract.purpose,
        expected_development_only=contract.development_only,
    )
    conflicts: set[tuple[str, str]] = set()
    output_samples: list[dict[str, Any]] = []
    used_overlay: set[tuple[str, str]] = set()
    for rank, (candidate_sample, review_id) in enumerate(
        zip(contract.samples, sample_review_ids), 1
    ):
        left = reviews[0].annotations[review_id]
        right = reviews[1].annotations[review_id]
        decisions: dict[str, dict[str, Any]] = {}
        sample_overlay: list[dict[str, Any]] = []
        for field in EVALUATION_FIELDS:
            key = (review_id, field)
            automatic = _automatic_decision(left, right, field)
            if key in overlay:
                decisions[field] = overlay[key]
                used_overlay.add(key)
                sample_overlay.append(
                    next(
                        row
                        for row in adjudication_audit.get("rows", [])
                        if row["review_id"] == review_id and row["field"] == field
                    )
                )
            elif automatic is not None:
                decisions[field] = automatic
            else:
                conflicts.add(key)
                continue
        if any(key[0] == review_id for key in conflicts):
            continue

        gold = {
            "in_scope": decisions["in_scope"],
            "reject_reason": decisions["reject_reason"],
            "axes": {field: decisions[field] for field in AXIS_FIELDS},
        }
        derivation_decisions = {
            field: GoldDecision(
                primary=decisions[field]["primary"],
                acceptable=tuple(decisions[field]["acceptable_labels"]),
                clarity=decisions[field]["clarity"],
            )
            for field in EVALUATION_FIELDS
        }
        derivation_row = _gold_row_for_derivation(derivation_decisions)
        if decisions["in_scope"]["clarity"] == "not_judgeable":
            raise ValueError(f"{review_id} final in_scope cannot be not_judgeable")
        if decisions["reject_reason"]["clarity"] == "not_judgeable":
            raise ValueError(f"{review_id} final reject_reason cannot be not_judgeable")
        for field in AXIS_FIELDS:
            primary = decisions[field]["primary"]
            clarity = decisions[field]["clarity"]
            if primary is None and field == "medium":
                raise ValueError(f"{review_id} final medium must remain judgeable")
            if primary is None and derivation_row[field] != "not_applicable":
                raise ValueError(f"{review_id} applicable {field} must remain judgeable")
            if primary is not None and primary == "not_applicable":
                raise ValueError(
                    f"{review_id} not_applicable {field} must be not_judgeable"
                )
        try:
            derived = derive_classification(derivation_row)
        except ValueError as exc:
            raise ValueError(
                f"{review_id} adjudicated primary fields violate the axes schema: {exc}"
            ) from exc
        gold["derived_classification"] = {
            "primary_class": derived["primary_class"],
            "secondary_classes": list(derived["secondary_classes"]),
            "usage_status": derived["usage_status"],
        }
        output_samples.append(
            {
                "sample_rank": rank,
                "sample_id": f"axis-sample-{rank:04d}",
                "review_id": review_id,
                "subset_membership": _subset_membership(rank),
                "source_identity": _copy_source_identity(candidate_sample),
                "image_evidence": _copy_image_evidence(candidate_sample),
                "human_review": gold,
                "review_provenance": {
                    "source_reviews": [
                        _review_snapshot(left),
                        _review_snapshot(right),
                    ],
                    "adjudications": sample_overlay,
                },
            }
        )
    if conflicts:
        display = ", ".join(f"{review_id}/{field}" for review_id, field in sorted(conflicts))
        raise ValueError("reviewer disagreement requires adjudication: " + display)
    unused = sorted(set(overlay) - used_overlay)
    if unused:
        display = ", ".join(f"{review_id}/{field}" for review_id, field in unused)
        raise ValueError("unused adjudication rows: " + display)
    if len(output_samples) != FROZEN_SAMPLE_COUNT:
        raise AssertionError("internal error: final gold sample count is not 50")

    output: dict[str, Any] = {
        "manifest_version": contract.gold_manifest_version,
        "finalizer_version": contract.finalizer_version,
        "purpose": contract.purpose,
        "development_only": contract.development_only,
        "provenance": _gold_provenance(
            candidate,
            contract=contract,
            candidate_file_sha=candidate_file_sha,
            candidate_logical_sha=contract.logical_sha256,
            codebook_sha=contract.codebook_sha256,
            reviews=reviews,
            adjudication_sha=adjudication_sha,
        ),
        "selection_policy": {
            "policy_version": contract.selection_policy_version,
            "prefix_limits": list(SUPPORTED_LIMITS),
            "selection_salt": contract.selection_salt,
        },
        "review_process": {
            "source_visibility": SOURCE_VISIBILITY,
            "image_long_edge": FIXED_REVIEW_LONG_EDGE,
            "independent_human": False,
            "reviewers": [
                {
                    "reviewer_id": review.reviewer_id,
                    "review_context_id": review.review_context_id,
                    "file_sha256": review.file_sha256,
                    "logical_sha256": review.logical_sha256,
                }
                for review in reviews
            ],
            "adjudication": adjudication_audit,
        },
        "samples": output_samples,
    }
    output["logical_sha256"] = axis_gold_logical_sha256(output)
    output["gold_manifest_sha256"] = axis_gold_manifest_sha256(output)
    if file_sha256(candidate_dev_manifest_path) != candidate_file_sha:
        raise RuntimeError("candidate development manifest changed during finalization")
    for path, review in zip(reviewer_annotation_paths, reviews):
        if file_sha256(path) != review.file_sha256:
            raise RuntimeError(f"review annotation changed during finalization: {path}")
    if adjudication_path is not None:
        if file_sha256(adjudication_path) != adjudication_sha:
            raise RuntimeError("adjudication changed during finalization")
    _write_json_no_clobber(output_path, output)
    return output
