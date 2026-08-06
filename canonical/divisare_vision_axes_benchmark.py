"""Divisare Vision benchmark for independent image axes.

The v1 benchmark forced every image into one of five incompatible buckets.
This runner instead evaluates independent visual facts at a single, frozen
1024-pixel input size. It accepts either the immutable 50-row development gold
or the separately frozen one-shot holdout gold and exposes their nested
N10/N20/N50 prefixes.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from canonical.divisare_image_smoke import (
    FetchPayload,
    canonical_json,
    file_sha256,
    network_fetch,
    utc_now,
)
from canonical.divisare_vision_axes import (
    AXIS_CONTRACT_VERSION,
    AXIS_OUTPUT_SCHEMA,
    AXIS_PROMPT_VERSION,
    CAMERA_ANGLE_VALUES,
    DRAWING_KIND_VALUES,
    FRAMING_SCALE_VALUES,
    MEDIUM_VALUES,
    PROJECT_STATE_VALUES,
    REJECT_REASON_VALUES,
    SEMANTIC_AXIS_FIELDS,
    SPATIAL_CONTEXT_VALUES,
    compose_axes_prompt,
    derive_classification,
    normalize_axes_batch,
)
from canonical.divisare_vision_benchmark import (
    LOCAL_DERIVATIVE_VERSION,
    SOURCE_DERIVATIVE_VERSION,
    DecodedSource,
    PreparedDerivative,
    decode_source,
    prepare_derivative,
)
from canonical.divisare_vision_gold import SOURCE_PROFILE
from canonical.divisare_vision_gold_finalize import parse_json_strict, phash_distance
from canonical.divisare_vision_runtime import (
    CLI_IMAGE_DETAIL,
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
    RUNTIME_VERSION,
    VisionRuntimeResult,
    run_codex_vision_batch,
)


SCHEMA_VERSION = 4
BENCHMARK_VERSION = "divisare-vision-axes-development-1024-v1.3.0"
AXIS_GOLD_MANIFEST_VERSION = "divisare-vision-axes-gold-development-v1.0.0"
DEVELOPMENT_PURPOSE = "development_only_not_holdout"
HOLDOUT_AXIS_GOLD_MANIFEST_VERSION = (
    "divisare-vision-axes-fresh-holdout-gold-v1.0.0"
)
FRESH_HOLDOUT_PURPOSE = "fresh_blind_one_shot_final_prompt_holdout"
HOLDOUT_GOLD_FINALIZER_VERSION = (
    "divisare-vision-axes-fresh-holdout-gold-finalizer-v1.0.0"
)
SELECTION_POLICY_VERSION = "divisare-vision-axes-prefix-selection-v1.0.0"
HOLDOUT_SELECTION_POLICY_VERSION = (
    "divisare-vision-axes-fresh-holdout-selector-v1.1.0"
)
HOLDOUT_SELECTION_SALT = "axis-fresh-holdout-blind-v1"
HOLDOUT_PROMPT_FREEZE_POLICY = (
    "any later prompt or schema change invalidates this holdout"
)
HOLDOUT_GOLD_FILENAME = "divisare_vision_axes_holdout_gold_n50_v1.json"
HOLDOUT_CANDIDATE_FILENAME = (
    "divisare_vision_axes_holdout_n50_candidates_v1_1.json"
)
HOLDOUT_PROBED_N100_FILENAME = (
    "divisare_vision_axes_holdout_candidates_n100_v1_probed.json"
)
HOLDOUT_CANDIDATE_N100_FILENAME = (
    "divisare_vision_axes_holdout_candidates_n100_v1.json"
)
HOLDOUT_PRIOR_N560_FILENAME = "divisare_vision_gold_candidates_v1_2_probed.json"
HOLDOUT_REVIEWER_FILENAMES = (
    "divisare_axes_holdout_reviewer_a_v1_1.json",
    "divisare_axes_holdout_reviewer_b_v1_1.json",
)
HOLDOUT_ADJUDICATION_FILENAME = "divisare_axes_holdout_adjudication_v1_1.json"

EXPECTED_HOLDOUT_SOURCE_DB_SHA256 = (
    "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f"
)
EXPECTED_HOLDOUT_GOLD_FILE_SHA256 = (
    "0429e2299434d03f5c1ec19db4bd551a7461cf459b0cebf8180200553574898f"
)
EXPECTED_HOLDOUT_GOLD_MANIFEST_SHA256 = (
    "2624f190af691f014d17879bf77840352badb0f3ba47118097f672919fd25393"
)
EXPECTED_HOLDOUT_GOLD_LOGICAL_SHA256 = (
    "d7086edc1e9220c8e3652e6376dabc53237fb52b606ce3f088083c4f2b7e5026"
)
EXPECTED_HOLDOUT_CANDIDATE_FILE_SHA256 = (
    "60c66722a5dc4f133687ec0b7d665487d7889c901256694e22d7d0e23dcb7fcb"
)
EXPECTED_HOLDOUT_CANDIDATE_MANIFEST_SHA256 = (
    "415715652524c5b7714335c5065563e9fd48283aa629fa74c09c2f1020ef6ea7"
)
EXPECTED_HOLDOUT_CANDIDATE_LOGICAL_SHA256 = (
    "d8a2666f187ddfec563a170bdd7a6497ff88c21373e9090b932b76013f880422"
)
EXPECTED_HOLDOUT_PROBED_N100_FILE_SHA256 = (
    "c1fdce889eafbb144ed29759ef3aabe37da73a448e0bf8dacbd9f6491bff856a"
)
EXPECTED_HOLDOUT_PROBED_N100_MANIFEST_SHA256 = (
    "1eb4a639e05fb8eb712c681d1da2115e75d4cb3bf4aa73af504d3675feedae19"
)
EXPECTED_HOLDOUT_BASE_PROBE_LOGICAL_SHA256 = (
    "de72260840d8eb69d83d23240d26b0a029f77b7a6259a6942c804682899feed7"
)
EXPECTED_HOLDOUT_CROSS_LOGICAL_SHA256 = (
    "cd9d9a69de0e5999d0290001397f0b90bae3517e95ef811c9debe53cf79bf036"
)
EXPECTED_HOLDOUT_CANDIDATE_N100_FILE_SHA256 = (
    "d12f69c17570ae6ef04b43c03ed9d914ffcf717c7d704fd180ffc8f6f06154c3"
)
EXPECTED_HOLDOUT_CANDIDATE_N100_MANIFEST_SHA256 = (
    "55223915f3d6aab0659bb52a4dc2add4752a3791c451f5d61872c6738941934d"
)
EXPECTED_HOLDOUT_PRIOR_N560_FILE_SHA256 = (
    "faecad6bd355f38d4553657a0ecb45ff77ba8ebee8f950889e3968f068fc39ad"
)
EXPECTED_HOLDOUT_PRIOR_N560_MANIFEST_SHA256 = (
    "480f28d33f90210a479a19e4ab858a71d99ebc7336f8ee0e638be8d96e46f626"
)
EXPECTED_HOLDOUT_CODEBOOK_SHA256 = (
    "6bd6642cad29ef109c1d24f16a6c444535ad88fb644c2d8fa394d4b75d2bbc07"
)
EXPECTED_HOLDOUT_OUTPUT_SCHEMA_SHA256 = (
    "93a1d601b1373cb3f61b3f2a4a53f324c272a051ec8b491f44d43fd8d43e34dc"
)
EXPECTED_HOLDOUT_PROMPT_REFERENCE_SHA256 = (
    "7c3991d95a6cae5be742af15fbdcf38f03815a114d5fc57547054613fa47423c"
)
EXPECTED_HOLDOUT_REVIEWER_FILE_SHA256S = (
    "a4fb0c83cde24b27b8ecae9bd65ab931ee79fffea93e0c7e8519afd4ee9be88e",
    "376868e51f5bee0bd1030749091e9881c6a30bf993c6e070ee1d783c9b7bebba",
)
EXPECTED_HOLDOUT_REVIEWER_LOGICAL_SHA256S = (
    "c0ea80a15637adb076496de307513bdf0e38083444a13ebde06851720219177a",
    "b55a3025e65c005e2a46f7071b9dc16dd8c7a2656708bc06b11162f637fd0e50",
)
EXPECTED_HOLDOUT_REVIEWER_IDS = ("codex-reviewer-a", "codex-reviewer-b")
EXPECTED_HOLDOUT_REVIEW_CONTEXT_IDS = (
    "fresh-holdout-a-20260805",
    "fresh-holdout-b-20260805",
)
EXPECTED_HOLDOUT_ADJUDICATION_FILE_SHA256 = (
    "e130373699c96a59b4c2ebbcd72be9eb7ad8060bed1a8743aac04826883be707"
)
EXPECTED_HOLDOUT_ADJUDICATION_LOGICAL_SHA256 = (
    "eae680b50a6a1a0791fc474fc22369d5d79873f51db5a07952bda51f85eb342d"
)
EXPECTED_HOLDOUT_ADJUDICATOR_ID = "codex-adjudicator-c"
HOLDOUT_ONE_SHOT_RECEIPT_VERSION = (
    "divisare-vision-axes-holdout-one-shot-receipt-v1.0.0"
)
HOLDOUT_ONE_SHOT_RECEIPT_ROOT = (
    Path(__file__).resolve().parents[1] / "data" / "smoke" / "one_shot_receipts"
)
SUPPORTED_LIMITS = (10, 20, 50)
FROZEN_SAMPLE_COUNT = 50
FIXED_BATCH_SIZE = 5
LANE = "long1024"
MAX_LONG_EDGE = 1024

AXIS_FIELDS = (
    "medium",
    "spatial_context",
    "framing_scale",
    "camera_angle",
    "drawing_kind",
    "project_state",
)
EVALUATION_FIELDS = ("in_scope", "reject_reason", *AXIS_FIELDS)
CLARITIES = ("clear", "boundary", "not_judgeable")

FIELD_VALUES: dict[str, tuple[Any, ...]] = {
    "in_scope": (True, False),
    "reject_reason": REJECT_REASON_VALUES,
    "medium": MEDIUM_VALUES,
    "spatial_context": SPATIAL_CONTEXT_VALUES,
    "framing_scale": FRAMING_SCALE_VALUES,
    "camera_angle": CAMERA_ANGLE_VALUES,
    "drawing_kind": DRAWING_KIND_VALUES,
    "project_state": PROJECT_STATE_VALUES,
}


@dataclass(frozen=True)
class GoldDecision:
    primary: Any
    acceptable: tuple[Any, ...]
    clarity: str


@dataclass(frozen=True)
class AxisGoldSample:
    sample_rank: int
    sample_id: str
    review_id: str
    candidate_id: str
    asset_key: str
    article_id: str
    building_id: str
    generation_group: str
    url_generation: str
    request_url: str
    expected_content_sha256: str
    pixel_sha256: str
    phash_256: str
    gold: Mapping[str, GoldDecision]
    declared_derived: Mapping[str, Any]
    computed_derived: Mapping[str, Any]


@dataclass(frozen=True)
class AxisGoldManifestContract:
    manifest_version: str
    purpose: str
    development_only: bool
    selection_policy_version: str


@dataclass(frozen=True)
class HoldoutOneShotReceipt:
    path: Path
    payload: Mapping[str, Any]
    file_sha256: str
    sample_order_sha256: str


DEVELOPMENT_GOLD_CONTRACT = AxisGoldManifestContract(
    manifest_version=AXIS_GOLD_MANIFEST_VERSION,
    purpose=DEVELOPMENT_PURPOSE,
    development_only=True,
    selection_policy_version=SELECTION_POLICY_VERSION,
)
HOLDOUT_GOLD_CONTRACT = AxisGoldManifestContract(
    manifest_version=HOLDOUT_AXIS_GOLD_MANIFEST_VERSION,
    purpose=FRESH_HOLDOUT_PURPOSE,
    development_only=False,
    selection_policy_version=HOLDOUT_SELECTION_POLICY_VERSION,
)


class _FrozenContentMismatch(RuntimeError):
    pass


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_sha(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError("%s must be lowercase SHA-256 hex" % name)
    return value


def _schema_sha256() -> str:
    return _sha256_bytes(canonical_json(AXIS_OUTPUT_SCHEMA).encode("utf-8"))


def _runtime_prompt_reference_sha256() -> str:
    return _sha256_bytes(
        compose_axes_prompt(["axis-reference0000"]).encode("utf-8")
    )


def axis_gold_logical_sha256(payload: Mapping[str, Any]) -> str:
    clean = dict(payload)
    clean.pop("gold_manifest_sha256", None)
    clean.pop("logical_sha256", None)
    return _sha256_bytes(canonical_json(clean).encode("utf-8"))


def axis_gold_manifest_sha256(payload: Mapping[str, Any]) -> str:
    clean = dict(payload)
    clean.pop("gold_manifest_sha256", None)
    return _sha256_bytes(canonical_json(clean).encode("utf-8"))


def _axis_gold_manifest_contract(
    payload: Mapping[str, Any],
) -> AxisGoldManifestContract:
    for contract in (DEVELOPMENT_GOLD_CONTRACT, HOLDOUT_GOLD_CONTRACT):
        if (
            payload.get("manifest_version") == contract.manifest_version
            and payload.get("purpose") == contract.purpose
            and payload.get("development_only") is contract.development_only
        ):
            return contract
    raise ValueError(
        "unsupported axis gold manifest contract: version/purpose/development_only "
        "must match one frozen contract"
    )


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("%s must be an object" % name)
    return value


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str] | frozenset[str], name: str
) -> None:
    if set(value) != set(expected):
        raise ValueError("%s fields mismatch" % name)


def _require_nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty string" % name)
    return value


def _validate_reviewer_shas(
    provenance: Mapping[str, Any], *, exact_count: int | None = None
) -> list[str]:
    reviewer_shas = provenance.get("reviewer_annotation_sha256s")
    if not isinstance(reviewer_shas, list):
        raise ValueError("reviewer_annotation_sha256s must be a list")
    if exact_count is None:
        if len(reviewer_shas) < 2:
            raise ValueError("at least two reviewer annotation SHAs are required")
    elif len(reviewer_shas) != exact_count:
        raise ValueError("exactly %d reviewer annotation SHAs are required" % exact_count)
    normalized = [
        _require_sha(value, "reviewer_annotation_sha256s[%d]" % index)
        for index, value in enumerate(reviewer_shas)
    ]
    if len(set(normalized)) != len(normalized):
        raise ValueError("reviewer annotation SHAs must be unique")
    return normalized


def _validate_holdout_parent_provenance(provenance: Mapping[str, Any]) -> None:
    parent_specs = {
        "parent_probed_n100": (
            "filename",
            "file_sha256",
            "manifest_sha256",
            "base_probe_logical_sha256",
            "cross_logical_sha256",
        ),
        "parent_candidate_n100": (
            "filename",
            "file_sha256",
            "manifest_sha256",
        ),
        "prior_probed_n560": (
            "filename",
            "file_sha256",
            "manifest_sha256",
        ),
    }
    for key, fields in parent_specs.items():
        parent = _require_mapping(provenance.get(key), "provenance.%s" % key)
        _require_exact_fields(parent, set(fields), "provenance.%s" % key)
        _require_nonempty_string(parent.get("filename"), "provenance.%s.filename" % key)
        for field in fields:
            if field != "filename":
                _require_sha(parent.get(field), "provenance.%s.%s" % (key, field))
    expected = {
        "parent_probed_n100": {
            "filename": HOLDOUT_PROBED_N100_FILENAME,
            "file_sha256": EXPECTED_HOLDOUT_PROBED_N100_FILE_SHA256,
            "manifest_sha256": EXPECTED_HOLDOUT_PROBED_N100_MANIFEST_SHA256,
            "base_probe_logical_sha256": EXPECTED_HOLDOUT_BASE_PROBE_LOGICAL_SHA256,
            "cross_logical_sha256": EXPECTED_HOLDOUT_CROSS_LOGICAL_SHA256,
        },
        "parent_candidate_n100": {
            "filename": HOLDOUT_CANDIDATE_N100_FILENAME,
            "file_sha256": EXPECTED_HOLDOUT_CANDIDATE_N100_FILE_SHA256,
            "manifest_sha256": EXPECTED_HOLDOUT_CANDIDATE_N100_MANIFEST_SHA256,
        },
        "prior_probed_n560": {
            "filename": HOLDOUT_PRIOR_N560_FILENAME,
            "file_sha256": EXPECTED_HOLDOUT_PRIOR_N560_FILE_SHA256,
            "manifest_sha256": EXPECTED_HOLDOUT_PRIOR_N560_MANIFEST_SHA256,
        },
    }
    for key, expected_value in expected.items():
        if dict(provenance[key]) != expected_value:
            raise ValueError("fresh holdout %s does not match frozen lineage" % key)


def _validate_holdout_review_process(
    payload: Mapping[str, Any], provenance: Mapping[str, Any]
) -> None:
    if payload.get("finalizer_version") != HOLDOUT_GOLD_FINALIZER_VERSION:
        raise ValueError("fresh holdout gold finalizer version mismatch")
    process = _require_mapping(payload.get("review_process"), "holdout review_process")
    _require_exact_fields(
        process,
        {
            "source_visibility",
            "image_long_edge",
            "independent_human",
            "reviewers",
            "adjudication",
        },
        "holdout review_process",
    )
    if process.get("source_visibility") != "pixels_and_opaque_id_only":
        raise ValueError("holdout review source visibility mismatch")
    if process.get("image_long_edge") != 1024:
        raise ValueError("holdout review image size mismatch")
    if process.get("independent_human") is not False:
        raise ValueError("holdout review must record independent_human=false")

    reviewer_shas = _validate_reviewer_shas(provenance, exact_count=2)
    raw_reviewers = process.get("reviewers")
    if not isinstance(raw_reviewers, list) or len(raw_reviewers) != 2:
        raise ValueError("holdout review_process must contain exactly two reviewers")
    reviewer_ids: list[str] = []
    context_ids: list[str] = []
    process_file_shas: list[str] = []
    for index, raw_reviewer in enumerate(raw_reviewers):
        reviewer = _require_mapping(raw_reviewer, "holdout reviewer %d" % index)
        _require_exact_fields(
            reviewer,
            {"reviewer_id", "review_context_id", "file_sha256", "logical_sha256"},
            "holdout reviewer %d" % index,
        )
        reviewer_ids.append(
            _require_nonempty_string(
                reviewer.get("reviewer_id"), "holdout reviewer %d ID" % index
            )
        )
        context_ids.append(
            _require_nonempty_string(
                reviewer.get("review_context_id"),
                "holdout reviewer %d context ID" % index,
            )
        )
        process_file_shas.append(
            _require_sha(
                reviewer.get("file_sha256"),
                "holdout reviewer %d file SHA" % index,
            )
        )
        _require_sha(
            reviewer.get("logical_sha256"),
            "holdout reviewer %d logical SHA" % index,
        )
    if len(set(reviewer_ids)) != 2 or len(set(context_ids)) != 2:
        raise ValueError("holdout reviewer IDs and context IDs must be distinct")
    if process_file_shas != reviewer_shas:
        raise ValueError("holdout review_process does not bind reviewer file SHAs")
    if provenance.get("reviewer") != "+".join(reviewer_ids):
        raise ValueError("holdout provenance reviewer identifier mismatch")
    if tuple(reviewer_ids) != EXPECTED_HOLDOUT_REVIEWER_IDS:
        raise ValueError("holdout reviewer IDs do not match the frozen review")
    if tuple(context_ids) != EXPECTED_HOLDOUT_REVIEW_CONTEXT_IDS:
        raise ValueError("holdout review contexts do not match the frozen review")
    if tuple(process_file_shas) != EXPECTED_HOLDOUT_REVIEWER_FILE_SHA256S:
        raise ValueError("holdout reviewer files do not match the frozen review")
    if tuple(
        str(row["logical_sha256"]) for row in raw_reviewers
    ) != EXPECTED_HOLDOUT_REVIEWER_LOGICAL_SHA256S:
        raise ValueError("holdout reviewer logical SHAs do not match the frozen review")

    adjudication = _require_mapping(
        process.get("adjudication"), "holdout review adjudication"
    )
    if adjudication.get("provided") is True:
        _require_exact_fields(
            adjudication,
            {
                "provided",
                "filename",
                "file_sha256",
                "logical_sha256",
                "adjudicator_id",
                "rows",
            },
            "holdout review adjudication",
        )
        _require_nonempty_string(
            adjudication.get("filename"), "holdout adjudication filename"
        )
        _require_nonempty_string(
            adjudication.get("adjudicator_id"), "holdout adjudicator ID"
        )
        adjudication_file_sha = _require_sha(
            adjudication.get("file_sha256"), "holdout adjudication file SHA"
        )
        _require_sha(
            adjudication.get("logical_sha256"), "holdout adjudication logical SHA"
        )
        if not isinstance(adjudication.get("rows"), list):
            raise ValueError("holdout adjudication rows must be a list")
        if provenance.get("adjudication_sha256") != adjudication_file_sha:
            raise ValueError("holdout provenance does not bind adjudication file SHA")
        if adjudication.get("filename") != HOLDOUT_ADJUDICATION_FILENAME:
            raise ValueError("holdout adjudication filename mismatch")
        if adjudication_file_sha != EXPECTED_HOLDOUT_ADJUDICATION_FILE_SHA256:
            raise ValueError("holdout adjudication file does not match the frozen review")
        if (
            adjudication.get("logical_sha256")
            != EXPECTED_HOLDOUT_ADJUDICATION_LOGICAL_SHA256
        ):
            raise ValueError("holdout adjudication logical SHA mismatch")
        if adjudication.get("adjudicator_id") != EXPECTED_HOLDOUT_ADJUDICATOR_ID:
            raise ValueError("holdout adjudicator ID mismatch")
    elif adjudication == {"provided": False, "status": "not_required"}:
        expected_absent_sha = _sha256_bytes(
            canonical_json(dict(adjudication)).encode("utf-8")
        )
        if provenance.get("adjudication_sha256") != expected_absent_sha:
            raise ValueError("holdout provenance does not bind absent adjudication")
    else:
        raise ValueError("holdout adjudication audit is invalid")


def _validate_holdout_provenance(
    payload: Mapping[str, Any], provenance: Mapping[str, Any]
) -> None:
    expected_fields = {
        "source_db_sha256",
        "parent_probed_n100",
        "parent_candidate_n100",
        "prior_probed_n560",
        "prompt_freeze",
        "candidate_holdout_manifest_sha256",
        "candidate_holdout_manifest_file_sha256",
        "candidate_holdout_manifest_logical_sha256",
        "codebook_sha256",
        "axis_output_schema_sha256",
        "adjudication_sha256",
        "reviewer_annotation_sha256s",
        "reviewer",
        "independent_human",
        "axis_contract_version",
        "axis_prompt_version",
    }
    _require_exact_fields(provenance, expected_fields, "fresh holdout provenance")
    _validate_holdout_parent_provenance(provenance)
    for key in (
        "source_db_sha256",
        "candidate_holdout_manifest_sha256",
        "candidate_holdout_manifest_file_sha256",
        "candidate_holdout_manifest_logical_sha256",
        "codebook_sha256",
        "axis_output_schema_sha256",
        "adjudication_sha256",
    ):
        _require_sha(provenance.get(key), "provenance.%s" % key)
    expected_scalar_lineage = {
        "source_db_sha256": EXPECTED_HOLDOUT_SOURCE_DB_SHA256,
        "candidate_holdout_manifest_sha256": EXPECTED_HOLDOUT_CANDIDATE_MANIFEST_SHA256,
        "candidate_holdout_manifest_file_sha256": EXPECTED_HOLDOUT_CANDIDATE_FILE_SHA256,
        "candidate_holdout_manifest_logical_sha256": EXPECTED_HOLDOUT_CANDIDATE_LOGICAL_SHA256,
        "codebook_sha256": EXPECTED_HOLDOUT_CODEBOOK_SHA256,
        "axis_output_schema_sha256": EXPECTED_HOLDOUT_OUTPUT_SCHEMA_SHA256,
        "adjudication_sha256": EXPECTED_HOLDOUT_ADJUDICATION_FILE_SHA256,
    }
    for key, expected in expected_scalar_lineage.items():
        if provenance.get(key) != expected:
            raise ValueError("fresh holdout provenance.%s is not frozen" % key)
    if tuple(_validate_reviewer_shas(provenance, exact_count=2)) != (
        EXPECTED_HOLDOUT_REVIEWER_FILE_SHA256S
    ):
        raise ValueError("fresh holdout reviewer SHAs are not frozen")

    prompt_freeze = _require_mapping(
        provenance.get("prompt_freeze"), "provenance.prompt_freeze"
    )
    _require_exact_fields(
        prompt_freeze,
        {
            "axis_contract_version",
            "axis_prompt_version",
            "codebook_sha256",
            "axis_output_schema_sha256",
            "policy",
        },
        "provenance.prompt_freeze",
    )
    if prompt_freeze.get("axis_contract_version") != AXIS_CONTRACT_VERSION:
        raise ValueError("holdout prompt freeze axis contract mismatch")
    if prompt_freeze.get("axis_prompt_version") != AXIS_PROMPT_VERSION:
        raise ValueError("holdout prompt freeze axis prompt mismatch")
    prompt_codebook_sha = _require_sha(
        prompt_freeze.get("codebook_sha256"), "holdout prompt freeze codebook SHA"
    )
    if prompt_codebook_sha != EXPECTED_HOLDOUT_CODEBOOK_SHA256:
        raise ValueError("holdout prompt freeze codebook is not frozen")
    if prompt_codebook_sha != provenance.get("codebook_sha256"):
        raise ValueError("holdout prompt freeze codebook SHA mismatch")
    if prompt_freeze.get("axis_output_schema_sha256") != _schema_sha256():
        raise ValueError("holdout prompt freeze output schema mismatch")
    if _schema_sha256() != EXPECTED_HOLDOUT_OUTPUT_SCHEMA_SHA256:
        raise ValueError("runtime output schema differs from the frozen holdout schema")
    if _runtime_prompt_reference_sha256() != EXPECTED_HOLDOUT_PROMPT_REFERENCE_SHA256:
        raise ValueError("runtime prompt body differs from the frozen holdout prompt")
    if prompt_freeze.get("axis_output_schema_sha256") != provenance.get(
        "axis_output_schema_sha256"
    ):
        raise ValueError("holdout prompt freeze and gold output schema differ")
    if prompt_freeze.get("policy") != HOLDOUT_PROMPT_FREEZE_POLICY:
        raise ValueError("holdout prompt freeze policy mismatch")
    if provenance.get("axis_contract_version") != AXIS_CONTRACT_VERSION:
        raise ValueError("holdout provenance axis contract mismatch")
    if provenance.get("axis_prompt_version") != AXIS_PROMPT_VERSION:
        raise ValueError("holdout provenance axis prompt mismatch")
    if provenance.get("independent_human") is not False:
        raise ValueError("holdout provenance must record independent_human=false")
    if provenance.get("reviewer") != "+".join(EXPECTED_HOLDOUT_REVIEWER_IDS):
        raise ValueError("holdout provenance reviewer is not frozen")
    _validate_holdout_review_process(payload, provenance)


def _read_frozen_holdout_artifact(
    path: Path, *, expected_file_sha256: str, label: str
) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("frozen holdout %s is missing: %s" % (label, path)) from exc
    actual_sha = _sha256_bytes(raw)
    if actual_sha != expected_file_sha256:
        raise ValueError(
            "frozen holdout %s file SHA mismatch: expected %s, got %s"
            % (label, expected_file_sha256, actual_sha)
        )
    return parse_json_strict(raw, label="frozen holdout %s" % label)


def _validate_frozen_holdout_artifacts(
    *,
    manifest_path: Path,
    payload: Mapping[str, Any],
    manifest_file_sha256: str,
    source_sha256: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    expected_gold_path = (
        repo_root / "data" / "review" / HOLDOUT_GOLD_FILENAME
    ).resolve()
    if manifest_path.resolve() != expected_gold_path:
        raise ValueError(
            "fresh holdout gold must use the canonical frozen path: %s"
            % expected_gold_path
        )
    if manifest_file_sha256 != EXPECTED_HOLDOUT_GOLD_FILE_SHA256:
        raise ValueError("fresh holdout gold file SHA does not match the frozen gold")
    if payload.get("gold_manifest_sha256") != EXPECTED_HOLDOUT_GOLD_MANIFEST_SHA256:
        raise ValueError("fresh holdout gold self SHA does not match the frozen gold")
    if payload.get("logical_sha256") != EXPECTED_HOLDOUT_GOLD_LOGICAL_SHA256:
        raise ValueError("fresh holdout gold logical SHA does not match the frozen gold")
    if source_sha256 != EXPECTED_HOLDOUT_SOURCE_DB_SHA256:
        raise ValueError("fresh holdout source DB does not match the frozen source")

    directory = expected_gold_path.parent
    candidate = _read_frozen_holdout_artifact(
        directory / HOLDOUT_CANDIDATE_FILENAME,
        expected_file_sha256=EXPECTED_HOLDOUT_CANDIDATE_FILE_SHA256,
        label="N50 candidate",
    )
    if (
        candidate.get("manifest_sha256")
        != EXPECTED_HOLDOUT_CANDIDATE_MANIFEST_SHA256
        or candidate.get("logical_sha256")
        != EXPECTED_HOLDOUT_CANDIDATE_LOGICAL_SHA256
    ):
        raise ValueError("frozen holdout N50 candidate internal hashes mismatch")

    probed = _read_frozen_holdout_artifact(
        directory / HOLDOUT_PROBED_N100_FILENAME,
        expected_file_sha256=EXPECTED_HOLDOUT_PROBED_N100_FILE_SHA256,
        label="probed N100",
    )
    if probed.get("manifest_sha256") != EXPECTED_HOLDOUT_PROBED_N100_MANIFEST_SHA256:
        raise ValueError("frozen holdout probed N100 manifest SHA mismatch")
    parent_candidate = _read_frozen_holdout_artifact(
        directory / HOLDOUT_CANDIDATE_N100_FILENAME,
        expected_file_sha256=EXPECTED_HOLDOUT_CANDIDATE_N100_FILE_SHA256,
        label="candidate N100",
    )
    if (
        parent_candidate.get("manifest_sha256")
        != EXPECTED_HOLDOUT_CANDIDATE_N100_MANIFEST_SHA256
    ):
        raise ValueError("frozen holdout candidate N100 manifest SHA mismatch")
    prior = _read_frozen_holdout_artifact(
        directory / HOLDOUT_PRIOR_N560_FILENAME,
        expected_file_sha256=EXPECTED_HOLDOUT_PRIOR_N560_FILE_SHA256,
        label="prior probed N560",
    )
    if prior.get("manifest_sha256") != EXPECTED_HOLDOUT_PRIOR_N560_MANIFEST_SHA256:
        raise ValueError("frozen holdout prior N560 manifest SHA mismatch")

    for index, (filename, file_sha, logical_sha) in enumerate(
        zip(
            HOLDOUT_REVIEWER_FILENAMES,
            EXPECTED_HOLDOUT_REVIEWER_FILE_SHA256S,
            EXPECTED_HOLDOUT_REVIEWER_LOGICAL_SHA256S,
        )
    ):
        review = _read_frozen_holdout_artifact(
            directory / filename,
            expected_file_sha256=file_sha,
            label="reviewer %d" % (index + 1),
        )
        if review.get("logical_sha256") != logical_sha:
            raise ValueError("frozen holdout reviewer %d logical SHA mismatch" % (index + 1))
        if review.get("candidate_dev_manifest_file_sha256") != (
            EXPECTED_HOLDOUT_CANDIDATE_FILE_SHA256
        ) or review.get("candidate_dev_manifest_logical_sha256") != (
            EXPECTED_HOLDOUT_CANDIDATE_LOGICAL_SHA256
        ):
            raise ValueError("frozen holdout reviewer %d candidate binding mismatch" % (index + 1))
        if review.get("codebook_sha256") != EXPECTED_HOLDOUT_CODEBOOK_SHA256:
            raise ValueError("frozen holdout reviewer %d codebook mismatch" % (index + 1))

    adjudication = _read_frozen_holdout_artifact(
        directory / HOLDOUT_ADJUDICATION_FILENAME,
        expected_file_sha256=EXPECTED_HOLDOUT_ADJUDICATION_FILE_SHA256,
        label="adjudication",
    )
    if (
        adjudication.get("logical_sha256")
        != EXPECTED_HOLDOUT_ADJUDICATION_LOGICAL_SHA256
    ):
        raise ValueError("frozen holdout adjudication logical SHA mismatch")
    if adjudication.get("reviewer_annotation_file_sha256s") != list(
        EXPECTED_HOLDOUT_REVIEWER_FILE_SHA256S
    ):
        raise ValueError("frozen holdout adjudication reviewer binding mismatch")


def _normalized_receipt_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _holdout_sample_order_sha256(samples: Sequence[AxisGoldSample]) -> str:
    order = [
        {
            "sample_rank": sample.sample_rank,
            "sample_id": sample.sample_id,
            "review_id": sample.review_id,
            "asset_key": sample.asset_key,
        }
        for sample in samples
    ]
    return _sha256_bytes(canonical_json(order).encode("utf-8"))


def _build_holdout_one_shot_receipt(
    *,
    samples: Sequence[AxisGoldSample],
    source_db: Path,
    source_sha256: str,
    gold_manifest_path: Path,
    gold_manifest_payload: Mapping[str, Any],
    gold_manifest_file_sha256: str,
    output_db: Path,
    report_path: Path,
    partial_db: Path,
    codex_bin: Path,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: Optional[str],
) -> HoldoutOneShotReceipt:
    if len(samples) != FROZEN_SAMPLE_COUNT:
        raise ValueError("fresh holdout receipt requires the complete frozen N50")
    paths = {
        "source_db": _normalized_receipt_path(source_db),
        "gold_manifest": _normalized_receipt_path(gold_manifest_path),
        "output_db": _normalized_receipt_path(output_db),
        "report": _normalized_receipt_path(report_path),
        "partial_db": _normalized_receipt_path(partial_db),
        "codex_bin": _normalized_receipt_path(codex_bin),
    }
    order_sha = _holdout_sample_order_sha256(samples)
    payload: dict[str, Any] = {
        "receipt_version": HOLDOUT_ONE_SHOT_RECEIPT_VERSION,
        "purpose": FRESH_HOLDOUT_PURPOSE,
        "gold_manifest_version": HOLDOUT_AXIS_GOLD_MANIFEST_VERSION,
        "gold_manifest_file_sha256": gold_manifest_file_sha256,
        "gold_manifest_logical_sha256": gold_manifest_payload["logical_sha256"],
        "source_sha256": source_sha256,
        "sample_count": len(samples),
        "sample_order_sha256": order_sha,
        "paths": paths,
        "paths_sha256": _sha256_bytes(canonical_json(paths).encode("utf-8")),
        "benchmark_version": BENCHMARK_VERSION,
        "schema_version": SCHEMA_VERSION,
        "batch_size": FIXED_BATCH_SIZE,
        "lane": LANE,
        "max_long_edge": MAX_LONG_EDGE,
        "source_derivative_version": SOURCE_DERIVATIVE_VERSION,
        "local_derivative_version": LOCAL_DERIVATIVE_VERSION,
        "source_profile": SOURCE_PROFILE,
        "axis_contract_version": AXIS_CONTRACT_VERSION,
        "axis_prompt_version": AXIS_PROMPT_VERSION,
        "axis_prompt_reference_sha256": _runtime_prompt_reference_sha256(),
        "axis_output_schema_sha256": _schema_sha256(),
        "runtime_version": RUNTIME_VERSION,
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "cli_version": cli_version,
        "image_detail": CLI_IMAGE_DETAIL,
    }
    raw = (canonical_json(payload) + "\n").encode("utf-8")
    receipt_path = (
        Path(HOLDOUT_ONE_SHOT_RECEIPT_ROOT)
        / (str(gold_manifest_payload["logical_sha256"]) + ".json")
    ).resolve()
    return HoldoutOneShotReceipt(
        path=receipt_path,
        payload=payload,
        file_sha256=_sha256_bytes(raw),
        sample_order_sha256=order_sha,
    )


def _validate_existing_holdout_receipt(receipt: HoldoutOneShotReceipt) -> None:
    expected = (canonical_json(receipt.payload) + "\n").encode("utf-8")
    try:
        actual = receipt.path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError("fresh holdout one-shot receipt is missing") from exc
    if actual != expected:
        raise RuntimeError(
            "fresh holdout one-shot receipt binds a different run contract"
        )
    if _sha256_bytes(actual) != receipt.file_sha256:
        raise RuntimeError("fresh holdout one-shot receipt SHA mismatch")


def _claim_holdout_receipt(receipt: HoldoutOneShotReceipt) -> None:
    receipt.path.parent.mkdir(parents=True, exist_ok=True)
    raw = (canonical_json(receipt.payload) + "\n").encode("utf-8")
    handle, temporary_name = tempfile.mkstemp(
        prefix=".%s." % receipt.path.name,
        suffix=".tmp",
        dir=str(receipt.path.parent),
    )
    linked = False
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, receipt.path)
            linked = True
        except FileExistsError as exc:
            raise RuntimeError(
                "fresh holdout one-shot receipt was already claimed"
            ) from exc
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    if not linked:
        raise RuntimeError("fresh holdout one-shot receipt claim failed")


def _partial_sidecar_is_preclaim(conn: sqlite3.Connection) -> bool:
    try:
        counts = [
            int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
            for table in (
                "fetch_attempts",
                "fetch_results",
                "derived_inputs",
                "vision_attempts",
                "vision_results",
            )
        ]
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("cannot validate pre-claim holdout sidecar") from exc
    return all(count == 0 for count in counts)


def _validate_request_url(value: Any, sample_id: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("%s request_url is missing" % sample_id)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "images.divisare.com":
        raise ValueError("%s request_url must use images.divisare.com HTTPS" % sample_id)
    if SOURCE_PROFILE not in parsed.path.split("/"):
        raise ValueError("%s request_url must use the frozen max2048 profile" % sample_id)
    return value


def _validate_decision(raw: Any, field: str, sample_id: str) -> GoldDecision:
    if not isinstance(raw, Mapping):
        raise ValueError("%s %s gold decision must be an object" % (sample_id, field))
    if set(raw) != {"primary", "acceptable_labels", "clarity"}:
        raise ValueError(
            "%s %s gold decision must contain only primary/acceptable_labels/clarity"
            % (sample_id, field)
        )
    clarity = raw.get("clarity")
    if clarity not in CLARITIES:
        raise ValueError("%s %s clarity is invalid" % (sample_id, field))
    primary = raw.get("primary")
    acceptable = raw.get("acceptable_labels")
    if not isinstance(acceptable, list) or len(acceptable) != len(
        {canonical_json(value) for value in acceptable}
    ):
        raise ValueError("%s %s acceptable must be a unique list" % (sample_id, field))
    allowed = FIELD_VALUES[field]
    if clarity == "not_judgeable":
        if primary is not None or acceptable:
            raise ValueError(
                "%s %s not_judgeable must use primary=null and acceptable=[]"
                % (sample_id, field)
            )
    else:
        if primary not in allowed or not acceptable or primary not in acceptable:
            raise ValueError("%s %s gold values are invalid" % (sample_id, field))
        if any(value not in allowed for value in acceptable):
            raise ValueError("%s %s acceptable contains an invalid value" % (sample_id, field))
        if clarity == "clear" and acceptable != [primary]:
            raise ValueError("%s %s clear decision must accept only primary" % (sample_id, field))
        if clarity == "boundary" and len(acceptable) < 2:
            raise ValueError("%s %s boundary decision needs at least two answers" % (sample_id, field))
    return GoldDecision(primary=primary, acceptable=tuple(acceptable), clarity=str(clarity))


def _gold_row_for_derivation(gold: Mapping[str, GoldDecision]) -> dict[str, Any]:
    in_scope = gold["in_scope"].primary
    row: dict[str, Any] = {
        "in_scope": in_scope,
        "reject_reason": gold["reject_reason"].primary
        or ("none" if in_scope is True else "other"),
    }
    for field in AXIS_FIELDS:
        value = gold[field].primary
        if value is not None:
            row[field] = value
        else:
            row[field] = "unknown" if field == "medium" else "not_applicable"
    uncertain: list[str] = []
    if gold["in_scope"].clarity == "boundary" or gold["reject_reason"].clarity == "boundary":
        uncertain.append("scope")
    for field in AXIS_FIELDS:
        decision = gold[field]
        if decision.clarity == "boundary" or row[field] == "unknown":
            uncertain.append(field)
    row["uncertain_axes"] = uncertain
    row["resolution_insufficient"] = False
    return row


def _normalize_derived(value: Mapping[str, Any]) -> dict[str, Any]:
    primary = value.get("primary_class")
    secondary = value.get("secondary_classes")
    usage = value.get("usage_status")
    if not isinstance(primary, str) or not primary:
        raise ValueError("derived primary_class must be a non-empty string")
    if not isinstance(secondary, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in secondary
    ):
        raise ValueError("derived secondary_classes must be strings")
    if len(set(secondary)) != len(secondary):
        raise ValueError("derived secondary_classes must be unique")
    if not isinstance(usage, str) or not usage:
        raise ValueError("derived usage_status must be a non-empty string")
    return {
        "primary_class": primary,
        "secondary_classes": list(secondary),
        "usage_status": usage,
    }


def _load_axis_gold_samples(payload: Mapping[str, Any]) -> list[AxisGoldSample]:
    contract = _axis_gold_manifest_contract(payload)
    if payload.get("logical_sha256") != axis_gold_logical_sha256(payload):
        raise ValueError("axis gold logical SHA mismatch")
    if payload.get("gold_manifest_sha256") != axis_gold_manifest_sha256(payload):
        raise ValueError("axis gold manifest self SHA mismatch")
    provenance = payload.get("provenance")
    policy = payload.get("selection_policy", payload.get("selection_contract"))
    if not isinstance(provenance, Mapping) or not isinstance(policy, Mapping):
        raise ValueError("axis gold provenance and selection_policy are required")
    if contract is HOLDOUT_GOLD_CONTRACT:
        _validate_holdout_provenance(payload, provenance)
    else:
        for key in (
            "source_db_sha256",
            "candidate_dev_manifest_sha256",
            "candidate_dev_manifest_file_sha256",
            "candidate_dev_manifest_logical_sha256",
            "parent_candidate_manifest_sha256",
            "parent_candidate_manifest_file_sha256",
            "parent_reviewed_pool_sha256",
            "parent_reviewed_pool_file_sha256",
            "old_gold_manifest_sha256",
            "old_gold_manifest_file_sha256",
            "old_n100_db_file_sha256",
            "old_n100_db_logical_sha256",
            "codebook_sha256",
            "axis_output_schema_sha256",
            "adjudication_sha256",
        ):
            _require_sha(provenance.get(key), "provenance.%s" % key)
        reviewer_shas = provenance.get("reviewer_annotation_sha256s")
        if not isinstance(reviewer_shas, list) or len(reviewer_shas) < 2:
            raise ValueError("at least two reviewer annotation SHAs are required")
        for index, value in enumerate(reviewer_shas):
            _require_sha(value, "reviewer_annotation_sha256s[%d]" % index)
    if provenance.get("axis_output_schema_sha256") != _schema_sha256():
        raise ValueError("axis output schema SHA does not match runtime schema")
    if provenance.get("axis_contract_version") != AXIS_CONTRACT_VERSION:
        raise ValueError("axis contract version mismatch")
    gold_prompt_version = provenance.get("axis_prompt_version")
    if not isinstance(gold_prompt_version, str) or not gold_prompt_version:
        raise ValueError("provenance.axis_prompt_version is required")
    if not isinstance(provenance.get("reviewer"), str) or not provenance.get("reviewer"):
        raise ValueError("provenance.reviewer is required")
    if not isinstance(provenance.get("independent_human"), bool):
        raise ValueError("provenance.independent_human must be boolean")
    if policy.get("policy_version") != contract.selection_policy_version:
        raise ValueError("axis gold selection policy version mismatch")
    if policy.get("prefix_limits") != list(SUPPORTED_LIMITS):
        raise ValueError("axis gold must freeze nested N10/N20/N50 prefixes")
    if not isinstance(policy.get("selection_salt"), str) or not policy.get("selection_salt"):
        raise ValueError("axis gold selection salt is required")
    if (
        contract is HOLDOUT_GOLD_CONTRACT
        and policy.get("selection_salt") != HOLDOUT_SELECTION_SALT
    ):
        raise ValueError("fresh holdout selection salt mismatch")

    raw_samples = payload.get("samples", payload.get("audit_samples"))
    if not isinstance(raw_samples, list) or len(raw_samples) != FROZEN_SAMPLE_COUNT:
        raise ValueError("axis gold manifest must contain exactly 50 samples")

    samples: list[AxisGoldSample] = []
    unique: dict[str, set[str]] = {
        name: set()
        for name in ("sample_id", "review_id", "candidate_id", "asset_key", "article_id", "building_id", "request_url", "pixel_sha256")
    }
    hashes: list[tuple[str, str]] = []
    for expected_rank, raw in enumerate(raw_samples, 1):
        if not isinstance(raw, Mapping):
            raise ValueError("axis gold sample must be an object")
        sample_id = raw.get("sample_id", "axis-sample-%04d" % expected_rank)
        review_id = raw.get("review_id")
        if raw.get("sample_rank") != expected_rank or sample_id != "axis-sample-%04d" % expected_rank:
            raise ValueError("axis gold sample rank/ID mismatch at %d" % expected_rank)
        if not isinstance(review_id, str) or not review_id or review_id == sample_id:
            raise ValueError("%s requires a distinct opaque review_id" % sample_id)
        source = raw.get("source_identity")
        evidence = raw.get("image_evidence")
        review = raw.get("human_review")
        if not all(isinstance(value, Mapping) for value in (source, evidence, review)):
            raise ValueError("%s source/evidence/review sections are required" % sample_id)
        assert isinstance(source, Mapping) and isinstance(evidence, Mapping) and isinstance(review, Mapping)
        membership = raw.get("subset_membership")
        expected_membership = [
            name
            for name, threshold in (("N10", 10), ("N20", 20), ("N50", 50))
            if expected_rank <= threshold
        ]
        if membership != expected_membership:
            raise ValueError("%s subset_membership does not match its frozen rank" % sample_id)
        identity = {
            "sample_id": str(sample_id),
            "review_id": review_id,
            "candidate_id": str(source.get("candidate_id") or ""),
            "asset_key": str(source.get("asset_key") or ""),
            "article_id": str(source.get("article_id") or ""),
            "building_id": str(source.get("building_id") or ""),
        }
        if any(not value for value in identity.values()):
            raise ValueError("%s has an empty identity" % sample_id)
        request_url = _validate_request_url(source.get("request_url"), str(sample_id))
        identity["request_url"] = request_url
        pixel_sha = _require_sha(evidence.get("pixel_sha256"), "%s pixel SHA" % sample_id)
        identity["pixel_sha256"] = pixel_sha
        for name, value in identity.items():
            if value in unique[name]:
                raise ValueError("axis gold %s must be globally unique: %s" % (name, value))
            unique[name].add(value)
        generation = source.get("generation_group")
        if generation not in ("modern", "legacy"):
            raise ValueError("%s generation_group is invalid" % sample_id)
        gold: dict[str, GoldDecision] = {}
        gold["in_scope"] = _validate_decision(review.get("in_scope"), "in_scope", str(sample_id))
        gold["reject_reason"] = _validate_decision(
            review.get("reject_reason"), "reject_reason", str(sample_id)
        )
        axes = review.get("axes")
        if not isinstance(axes, Mapping) or set(axes) != set(AXIS_FIELDS):
            raise ValueError("%s must contain exactly the six review axes" % sample_id)
        for field in AXIS_FIELDS:
            gold[field] = _validate_decision(axes.get(field), field, str(sample_id))
        for required_field in ("in_scope", "reject_reason", "medium"):
            if gold[required_field].clarity == "not_judgeable":
                raise ValueError(
                    "%s %s must be judgeable" % (sample_id, required_field)
                )
        if gold["in_scope"].primary is False and any(
            gold[field].clarity != "not_judgeable" for field in SEMANTIC_AXIS_FIELDS
        ):
            raise ValueError("%s out-of-scope axes must be not_judgeable" % sample_id)

        declared_raw = review.get("derived_classification")
        if not isinstance(declared_raw, Mapping):
            raise ValueError("%s derived_classification is required" % sample_id)
        declared = _normalize_derived(declared_raw)
        computed = _normalize_derived(derive_classification(_gold_row_for_derivation(gold)))
        if declared != computed:
            raise ValueError("%s derived classification does not match primary axes" % sample_id)
        phash = _require_sha(evidence.get("phash_256"), "%s pHash" % sample_id)
        hashes.append((str(sample_id), phash))
        samples.append(
            AxisGoldSample(
                sample_rank=expected_rank,
                sample_id=str(sample_id),
                review_id=review_id,
                candidate_id=identity["candidate_id"],
                asset_key=identity["asset_key"],
                article_id=identity["article_id"],
                building_id=identity["building_id"],
                generation_group=str(generation),
                url_generation=str(source.get("url_generation") or ""),
                request_url=request_url,
                expected_content_sha256=_require_sha(
                    evidence.get("content_sha256"), "%s content SHA" % sample_id
                ),
                pixel_sha256=pixel_sha,
                phash_256=phash,
                gold=gold,
                declared_derived=declared,
                computed_derived=computed,
            )
        )
    for (left_id, left_hash), (right_id, right_hash) in itertools.combinations(hashes, 2):
        if phash_distance(left_hash, right_hash) <= 8:
            raise ValueError("axis gold pHash <=8 pair: %s/%s" % (left_id, right_id))
    return samples


def load_axis_gold_manifest(
    manifest_path: Path, source_db: Path, limit: int
) -> tuple[dict[str, Any], list[AxisGoldSample], str, str]:
    if limit not in SUPPORTED_LIMITS:
        raise ValueError("limit must be one of 10, 20, or 50")
    raw = manifest_path.read_bytes()
    payload = parse_json_strict(raw, label="axis gold manifest")
    contract = _axis_gold_manifest_contract(payload)
    if contract is HOLDOUT_GOLD_CONTRACT and limit != FROZEN_SAMPLE_COUNT:
        raise ValueError(
            "fresh holdout permits only one N50 run; derive N10/N20 metrics "
            "from that N50 result"
        )
    samples = _load_axis_gold_samples(payload)
    source_sha = file_sha256(source_db)
    expected_source_sha = _require_sha(
        payload["provenance"].get("source_db_sha256"), "source DB SHA"
    )
    if source_sha != expected_source_sha:
        raise ValueError(
            "source DB SHA does not match frozen axis gold: expected %s, got %s"
            % (expected_source_sha, source_sha)
        )
    manifest_file_sha = _sha256_bytes(raw)
    if contract is HOLDOUT_GOLD_CONTRACT:
        _validate_frozen_holdout_artifacts(
            manifest_path=manifest_path,
            payload=payload,
            manifest_file_sha256=manifest_file_sha,
            source_sha256=source_sha,
        )
    return payload, samples[:limit], manifest_file_sha, source_sha


SIDECAR_SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE benchmark_run(
  run_id INTEGER PRIMARY KEY CHECK(run_id=1),
  status TEXT NOT NULL CHECK(status IN ('running','complete','failed_validation')),
  benchmark_version TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  purpose TEXT NOT NULL,
  requested_limit INTEGER NOT NULL,
  gold_manifest_path TEXT NOT NULL,
  gold_manifest_version TEXT NOT NULL,
  gold_manifest_file_sha256 TEXT NOT NULL,
  gold_manifest_logical_sha256 TEXT NOT NULL,
  one_shot_receipt_path TEXT,
  one_shot_receipt_sha256 TEXT CHECK(one_shot_receipt_sha256 IS NULL OR length(one_shot_receipt_sha256)=64),
  gold_sample_order_sha256 TEXT CHECK(gold_sample_order_sha256 IS NULL OR length(gold_sample_order_sha256)=64),
  reviewer_identifier TEXT NOT NULL,
  independent_human INTEGER NOT NULL CHECK(independent_human IN (0,1)),
  gold_axis_prompt_version TEXT NOT NULL,
  source_db_path TEXT NOT NULL,
  source_sha256_before TEXT NOT NULL,
  source_sha256_after TEXT,
  batch_size INTEGER NOT NULL,
  batch_count INTEGER NOT NULL,
  lane TEXT NOT NULL,
  max_long_edge INTEGER NOT NULL,
  source_derivative_version TEXT NOT NULL,
  local_derivative_version TEXT NOT NULL,
  source_profile TEXT NOT NULL,
  axis_contract_version TEXT NOT NULL,
  axis_prompt_version TEXT NOT NULL,
  axis_output_schema_sha256 TEXT NOT NULL,
  model TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  service_tier TEXT NOT NULL,
  runtime_version TEXT NOT NULL,
  cli_version TEXT,
  image_detail TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  technical_gate_passed INTEGER CHECK(technical_gate_passed IS NULL OR technical_gate_passed IN (0,1)),
  metrics_json TEXT CHECK(metrics_json IS NULL OR json_valid(metrics_json)),
  logical_sha256 TEXT,
  error TEXT
);

CREATE TABLE gold_samples(
  sample_rank INTEGER PRIMARY KEY,
  sample_id TEXT NOT NULL UNIQUE,
  review_id TEXT NOT NULL UNIQUE,
  candidate_id TEXT NOT NULL UNIQUE,
  asset_key TEXT NOT NULL UNIQUE,
  article_id TEXT NOT NULL UNIQUE,
  building_id TEXT NOT NULL UNIQUE,
  generation_group TEXT NOT NULL CHECK(generation_group IN ('modern','legacy')),
  url_generation TEXT NOT NULL,
  request_url TEXT NOT NULL UNIQUE,
  expected_content_sha256 TEXT NOT NULL CHECK(length(expected_content_sha256)=64),
  gold_pixel_sha256 TEXT NOT NULL CHECK(length(gold_pixel_sha256)=64),
  gold_phash_256 TEXT NOT NULL CHECK(length(gold_phash_256)=64),
  gold_review_json TEXT NOT NULL CHECK(json_valid(gold_review_json)),
  gold_derived_json TEXT NOT NULL CHECK(json_valid(gold_derived_json))
);

CREATE TABLE fetch_attempts(
  fetch_attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_key TEXT NOT NULL REFERENCES gold_samples(asset_key),
  batch_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('success','failed','content_mismatch')),
  expected_content_sha256 TEXT NOT NULL,
  actual_content_sha256 TEXT,
  response_bytes INTEGER,
  response_mime TEXT,
  http_status INTEGER,
  final_url TEXT,
  elapsed_ms INTEGER NOT NULL,
  error_kind TEXT,
  error_message TEXT
);

CREATE TABLE fetch_results(
  asset_key TEXT PRIMARY KEY REFERENCES gold_samples(asset_key),
  status TEXT NOT NULL CHECK(status IN ('success','failed','content_mismatch')),
  expected_content_sha256 TEXT NOT NULL,
  actual_content_sha256 TEXT,
  response_bytes INTEGER,
  response_mime TEXT,
  http_status INTEGER,
  final_url TEXT,
  decoded_format TEXT,
  width INTEGER,
  height INTEGER,
  elapsed_ms INTEGER NOT NULL,
  error_kind TEXT,
  error_message TEXT
);

CREATE TABLE derived_inputs(
  asset_key TEXT PRIMARY KEY REFERENCES gold_samples(asset_key),
  lane TEXT NOT NULL CHECK(lane='long1024'),
  max_long_edge INTEGER NOT NULL CHECK(max_long_edge=1024),
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  raw_patch_count INTEGER NOT NULL,
  encoded_bytes INTEGER NOT NULL,
  encoded_sha256 TEXT NOT NULL CHECK(length(encoded_sha256)=64),
  pixel_sha256 TEXT NOT NULL CHECK(length(pixel_sha256)=64)
);

CREATE TABLE vision_attempts(
  attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_no INTEGER NOT NULL,
  review_ids_json TEXT NOT NULL CHECK(json_valid(review_ids_json)),
  asset_keys_json TEXT NOT NULL CHECK(json_valid(asset_keys_json)),
  status TEXT NOT NULL CHECK(status IN ('success','failed')),
  model TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  service_tier TEXT NOT NULL,
  runtime_version TEXT NOT NULL,
  cli_version TEXT,
  codex_bin TEXT NOT NULL,
  image_detail TEXT NOT NULL,
  sandbox TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256)=64),
  output_schema_sha256 TEXT NOT NULL CHECK(length(output_schema_sha256)=64),
  elapsed_ms INTEGER NOT NULL,
  input_tokens INTEGER,
  cached_input_tokens INTEGER,
  output_tokens INTEGER,
  raw_events_sha256 TEXT,
  stdout_excerpt TEXT,
  stderr_excerpt TEXT,
  non_json_lines_json TEXT NOT NULL CHECK(json_valid(non_json_lines_json)),
  error_kind TEXT,
  error_message TEXT
);

CREATE TABLE vision_results(
  asset_key TEXT PRIMARY KEY REFERENCES gold_samples(asset_key),
  review_id TEXT NOT NULL UNIQUE,
  result_json TEXT NOT NULL CHECK(json_valid(result_json)),
  derived_primary_class TEXT NOT NULL,
  derived_secondary_classes_json TEXT NOT NULL CHECK(json_valid(derived_secondary_classes_json)),
  derived_usage_status TEXT NOT NULL
);

CREATE TABLE axis_metrics(
  field_name TEXT NOT NULL,
  scope TEXT NOT NULL CHECK(scope IN ('all','clear','boundary','not_judgeable')),
  total INTEGER NOT NULL,
  judged INTEGER NOT NULL,
  primary_correct INTEGER NOT NULL,
  acceptable_correct INTEGER NOT NULL,
  applicability_judged INTEGER NOT NULL,
  applicability_correct INTEGER NOT NULL,
  primary_accuracy REAL,
  acceptable_accuracy REAL,
  applicability_accuracy REAL,
  counts_json TEXT NOT NULL CHECK(json_valid(counts_json)),
  PRIMARY KEY(field_name,scope)
);

CREATE TABLE validations(
  validation_name TEXT PRIMARY KEY,
  severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
  passed INTEGER NOT NULL CHECK(passed IN (0,1)),
  expected TEXT,
  actual TEXT NOT NULL,
  detail TEXT
);

CREATE INDEX idx_attempt_batch ON vision_attempts(batch_no,attempt_id);
"""


def _gold_json(sample: AxisGoldSample) -> dict[str, Any]:
    return {
        field: {
            "primary": decision.primary,
            "acceptable_labels": list(decision.acceptable),
            "clarity": decision.clarity,
        }
        for field, decision in sample.gold.items()
    }


GOLD_SAMPLE_COLUMNS = (
    "sample_rank",
    "sample_id",
    "review_id",
    "candidate_id",
    "asset_key",
    "article_id",
    "building_id",
    "generation_group",
    "url_generation",
    "request_url",
    "expected_content_sha256",
    "gold_pixel_sha256",
    "gold_phash_256",
    "gold_review_json",
    "gold_derived_json",
)


def _gold_sample_rows(samples: Sequence[AxisGoldSample]) -> list[tuple[Any, ...]]:
    return [
        (
            sample.sample_rank,
            sample.sample_id,
            sample.review_id,
            sample.candidate_id,
            sample.asset_key,
            sample.article_id,
            sample.building_id,
            sample.generation_group,
            sample.url_generation,
            sample.request_url,
            sample.expected_content_sha256,
            sample.pixel_sha256,
            sample.phash_256,
            canonical_json(_gold_json(sample)),
            canonical_json(sample.computed_derived),
        )
        for sample in samples
    ]


def initialize_sidecar(
    conn: sqlite3.Connection,
    *,
    samples: Sequence[AxisGoldSample],
    manifest_path: Path,
    manifest_payload: Mapping[str, Any],
    manifest_file_sha256: str,
    source_db: Path,
    source_sha256: str,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: Optional[str],
    one_shot_receipt: HoldoutOneShotReceipt | None = None,
) -> None:
    limit = len(samples)
    conn.executescript(SIDECAR_SCHEMA)
    provenance = manifest_payload["provenance"]
    conn.execute(
        """
        INSERT INTO benchmark_run(
          run_id,status,benchmark_version,schema_version,purpose,requested_limit,
          gold_manifest_path,gold_manifest_version,gold_manifest_file_sha256,
          gold_manifest_logical_sha256,one_shot_receipt_path,one_shot_receipt_sha256,
          gold_sample_order_sha256,reviewer_identifier,independent_human,
          gold_axis_prompt_version,
          source_db_path,source_sha256_before,batch_size,batch_count,lane,max_long_edge,
          source_derivative_version,local_derivative_version,source_profile,model,
          axis_contract_version,axis_prompt_version,axis_output_schema_sha256,
          reasoning,service_tier,runtime_version,cli_version,image_detail,started_at
        ) VALUES(1,'running',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            BENCHMARK_VERSION,
            SCHEMA_VERSION,
            str(manifest_payload["purpose"]),
            limit,
            str(manifest_path),
            str(manifest_payload["manifest_version"]),
            manifest_file_sha256,
            manifest_payload["logical_sha256"],
            str(one_shot_receipt.path) if one_shot_receipt else None,
            one_shot_receipt.file_sha256 if one_shot_receipt else None,
            one_shot_receipt.sample_order_sha256 if one_shot_receipt else None,
            str(provenance["reviewer"]),
            int(provenance["independent_human"]),
            str(provenance["axis_prompt_version"]),
            str(source_db),
            source_sha256,
            FIXED_BATCH_SIZE,
            limit // FIXED_BATCH_SIZE,
            LANE,
            MAX_LONG_EDGE,
            SOURCE_DERIVATIVE_VERSION,
            LOCAL_DERIVATIVE_VERSION,
            SOURCE_PROFILE,
            model,
            AXIS_CONTRACT_VERSION,
            AXIS_PROMPT_VERSION,
            _schema_sha256(),
            reasoning,
            service_tier,
            RUNTIME_VERSION,
            cli_version,
            CLI_IMAGE_DETAIL,
            utc_now(),
        ),
    )
    conn.executemany(
        "INSERT INTO gold_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _gold_sample_rows(samples),
    )
    conn.commit()


def _validate_resume(
    conn: sqlite3.Connection,
    *,
    samples: Sequence[AxisGoldSample],
    manifest_path: Path,
    manifest_payload: Mapping[str, Any],
    manifest_file_sha256: str,
    source_db: Path,
    source_sha256: str,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: Optional[str],
    one_shot_receipt: HoldoutOneShotReceipt | None = None,
) -> None:
    conn.row_factory = sqlite3.Row
    raw = conn.execute("SELECT * FROM benchmark_run WHERE run_id=1").fetchone()
    if raw is None:
        raise RuntimeError("partial axes benchmark has no benchmark_run")
    actual = dict(raw)
    expected = {
        "status": "running",
        "benchmark_version": BENCHMARK_VERSION,
        "schema_version": SCHEMA_VERSION,
        "purpose": str(manifest_payload["purpose"]),
        "requested_limit": len(samples),
        "gold_manifest_path": str(manifest_path),
        "gold_manifest_version": str(manifest_payload["manifest_version"]),
        "gold_manifest_file_sha256": manifest_file_sha256,
        "gold_manifest_logical_sha256": manifest_payload["logical_sha256"],
        "one_shot_receipt_path": (
            str(one_shot_receipt.path) if one_shot_receipt else None
        ),
        "one_shot_receipt_sha256": (
            one_shot_receipt.file_sha256 if one_shot_receipt else None
        ),
        "gold_sample_order_sha256": (
            one_shot_receipt.sample_order_sha256 if one_shot_receipt else None
        ),
        "reviewer_identifier": str(manifest_payload["provenance"]["reviewer"]),
        "independent_human": int(
            manifest_payload["provenance"]["independent_human"]
        ),
        "gold_axis_prompt_version": str(
            manifest_payload["provenance"]["axis_prompt_version"]
        ),
        "source_db_path": str(source_db),
        "source_sha256_before": source_sha256,
        "batch_size": FIXED_BATCH_SIZE,
        "batch_count": len(samples) // FIXED_BATCH_SIZE,
        "lane": LANE,
        "max_long_edge": MAX_LONG_EDGE,
        "source_derivative_version": SOURCE_DERIVATIVE_VERSION,
        "local_derivative_version": LOCAL_DERIVATIVE_VERSION,
        "source_profile": SOURCE_PROFILE,
        "axis_contract_version": AXIS_CONTRACT_VERSION,
        "axis_prompt_version": AXIS_PROMPT_VERSION,
        "axis_output_schema_sha256": _schema_sha256(),
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "runtime_version": RUNTIME_VERSION,
        "cli_version": cli_version,
        "image_detail": CLI_IMAGE_DETAIL,
    }
    mismatches = {
        key: {"actual": actual.get(key), "expected": value}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise RuntimeError("resume contract mismatch: %s" % canonical_json(mismatches))
    expected_gold_rows = _gold_sample_rows(samples)
    try:
        actual_gold_rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT %s FROM gold_samples ORDER BY sample_rank"
                % ",".join(GOLD_SAMPLE_COLUMNS)
            ).fetchall()
        ]
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("resume gold_samples schema/read failure") from exc
    if actual_gold_rows != expected_gold_rows:
        expected_sha = _sha256_bytes(canonical_json(expected_gold_rows).encode("utf-8"))
        actual_sha = _sha256_bytes(canonical_json(actual_gold_rows).encode("utf-8"))
        raise RuntimeError(
            "resume gold_samples mismatch: expected rows=%d sha=%s, actual rows=%d sha=%s"
            % (
                len(expected_gold_rows),
                expected_sha,
                len(actual_gold_rows),
                actual_sha,
            )
        )


def _derivative_tuple(value: PreparedDerivative) -> tuple[Any, ...]:
    return (
        value.lane,
        value.max_long_edge,
        value.width,
        value.height,
        value.raw_patch_count,
        len(value.encoded_bytes),
        value.encoded_sha256,
        value.pixel_sha256,
    )


def _write_fetch_attempt(
    conn: sqlite3.Connection,
    *,
    sample: AxisGoldSample,
    batch_no: int,
    status: str,
    elapsed_ms: int,
    payload: FetchPayload | None = None,
    actual_sha: str | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fetch_attempts(asset_key,batch_no,status,expected_content_sha256,actual_content_sha256,response_bytes,response_mime,http_status,final_url,elapsed_ms,error_kind,error_message) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sample.asset_key,
            batch_no,
            status,
            sample.expected_content_sha256,
            actual_sha,
            len(payload.raw) if payload else None,
            payload.mime_type if payload else None,
            payload.http_status if payload else None,
            payload.final_url if payload else None,
            elapsed_ms,
            error_kind,
            (error_message or "")[:1000] or None,
        ),
    )


def _retain_or_write_input(
    conn: sqlite3.Connection,
    *,
    sample: AxisGoldSample,
    payload: FetchPayload,
    decoded: DecodedSource,
    derivative: PreparedDerivative,
    elapsed_ms: int,
) -> None:
    actual_sha = _sha256_bytes(payload.raw)
    prior = conn.execute(
        "SELECT status,actual_content_sha256 FROM fetch_results WHERE asset_key=?",
        (sample.asset_key,),
    ).fetchone()
    values = _derivative_tuple(derivative)
    if prior is not None and prior[0] == "success":
        if prior[1] != actual_sha:
            raise RuntimeError("resume source response changed for %s" % sample.sample_id)
        retained = conn.execute(
            "SELECT lane,max_long_edge,width,height,raw_patch_count,encoded_bytes,encoded_sha256,pixel_sha256 FROM derived_inputs WHERE asset_key=?",
            (sample.asset_key,),
        ).fetchone()
        if retained is None or tuple(retained) != values:
            raise RuntimeError("resume 1024 derivative changed for %s" % sample.sample_id)
        return
    conn.execute(
        "INSERT OR REPLACE INTO fetch_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sample.asset_key,
            "success",
            sample.expected_content_sha256,
            actual_sha,
            len(payload.raw),
            payload.mime_type,
            payload.http_status,
            payload.final_url,
            decoded.decoded_format,
            decoded.width,
            decoded.height,
            elapsed_ms,
            None,
            None,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO derived_inputs VALUES(?,?,?,?,?,?,?,?,?)",
        (sample.asset_key, *values),
    )


def _write_failed_fetch_result(
    conn: sqlite3.Connection,
    *,
    sample: AxisGoldSample,
    status: str,
    elapsed_ms: int,
    payload: FetchPayload | None,
    actual_sha: str | None,
    error_kind: str,
    error_message: str,
) -> None:
    if conn.execute(
        "SELECT 1 FROM fetch_results WHERE asset_key=? AND status='success'", (sample.asset_key,)
    ).fetchone():
        return
    conn.execute(
        "INSERT OR REPLACE INTO fetch_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sample.asset_key,
            status,
            sample.expected_content_sha256,
            actual_sha,
            len(payload.raw) if payload else None,
            payload.mime_type if payload else None,
            payload.http_status if payload else None,
            payload.final_url if payload else None,
            None,
            None,
            None,
            elapsed_ms,
            error_kind,
            error_message[:1000],
        ),
    )


def _write_vision_attempt(
    conn: sqlite3.Connection,
    *,
    batch_no: int,
    samples: Sequence[AxisGoldSample],
    result: VisionRuntimeResult,
    status: str,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    usage = result.usage
    cli_version = conn.execute("SELECT cli_version FROM benchmark_run WHERE run_id=1").fetchone()[0]
    conn.execute(
        """
        INSERT INTO vision_attempts(
          batch_no,review_ids_json,asset_keys_json,status,model,reasoning,service_tier,
          runtime_version,cli_version,codex_bin,image_detail,sandbox,prompt_sha256,
          output_schema_sha256,elapsed_ms,input_tokens,cached_input_tokens,output_tokens,
          raw_events_sha256,stdout_excerpt,stderr_excerpt,non_json_lines_json,error_kind,error_message
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_no,
            canonical_json([sample.review_id for sample in samples]),
            canonical_json([sample.asset_key for sample in samples]),
            status,
            result.provenance.model,
            result.provenance.reasoning,
            result.provenance.service_tier,
            result.provenance.runtime_version,
            cli_version,
            result.provenance.codex_bin,
            result.provenance.cli_image_detail,
            result.provenance.sandbox,
            result.provenance.prompt_sha256,
            result.provenance.output_schema_sha256,
            round(result.elapsed_seconds * 1000),
            usage.input_tokens if usage else None,
            usage.cached_input_tokens if usage else None,
            usage.output_tokens if usage else None,
            _sha256_bytes(result.stdout.encode("utf-8")) if result.stdout else None,
            result.stdout[-8000:] if result.stdout else None,
            result.stderr[-8000:] if result.stderr else None,
            canonical_json(list(result.non_json_stdout_lines)),
            error_kind or result.error_kind,
            (error_message or result.error_message or "")[:1000] or None,
        ),
    )


def _write_vision_results(
    conn: sqlite3.Connection,
    samples: Sequence[AxisGoldSample],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    values = []
    for sample, row in zip(samples, rows):
        derived = _normalize_derived(derive_classification(row))
        declared = _normalize_derived(row)
        if declared != derived:
            raise ValueError(
                "model derived classification disagrees with its normalized axes for %s"
                % sample.review_id
            )
        values.append(
            (
                sample.asset_key,
                sample.review_id,
                canonical_json(row),
                derived["primary_class"],
                canonical_json(derived["secondary_classes"]),
                derived["usage_status"],
            )
        )
    conn.executemany("INSERT OR REPLACE INTO vision_results VALUES(?,?,?,?,?,?)", values)


def _predicted_field(row: Mapping[str, Any], field: str) -> Any:
    return row.get(field)


def _is_applicable(field: str, value: Any) -> bool:
    if field in ("in_scope", "reject_reason"):
        return value is not None
    return value not in (None, "not_applicable")


def _semantic_axis_applicable(in_scope: bool, medium: str, field: str) -> bool:
    if field in ("in_scope", "reject_reason", "medium"):
        return True
    if not in_scope:
        return False
    if medium == "photograph":
        return field in {
            "spatial_context",
            "framing_scale",
            "camera_angle",
            "project_state",
        }
    if medium == "drawing":
        return field == "drawing_kind"
    if medium == "rendering":
        return field in {
            "spatial_context",
            "framing_scale",
            "camera_angle",
            "drawing_kind",
        }
    return False


def _gold_applicability_options(
    gold: Mapping[str, Mapping[str, Any]], field: str
) -> frozenset[bool]:
    """Return every applicability state allowed by scope/medium gold.

    A boundary scope or medium can make a downstream axis conditionally
    applicable. Such rows must not be forced into a binary applicability score.
    """

    if field in ("in_scope", "reject_reason", "medium"):
        return frozenset({True})
    scopes = gold["in_scope"]["acceptable_labels"]
    media = gold["medium"]["acceptable_labels"]
    if not scopes or not media:
        raise ValueError("scope and medium must remain judgeable in gold")
    return frozenset(
        _semantic_axis_applicable(bool(in_scope), str(medium), field)
        for in_scope in scopes
        for medium in media
    )


def _gold_uncertain_axes(gold: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    expected: set[str] = set()
    if (
        gold["in_scope"]["clarity"] == "boundary"
        or gold["reject_reason"]["clarity"] == "boundary"
    ):
        expected.add("scope")
    expected.update(
        field
        for field in AXIS_FIELDS
        if gold[field]["clarity"] == "boundary"
        or gold[field]["primary"] == "unknown"
    )
    return frozenset(expected)


def _gold_derived_options(
    gold: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Derive every classification allowed by the gold alternatives.

    Gold decisions are stored per axis, while the legacy search classification
    is derived from the axes jointly.  Boundary labels therefore need to be
    evaluated as coherent, valid branches rather than only against the
    reviewers' preferred (primary) branch.

    A downstream axis can be not-judgeable under the preferred scope/medium
    branch but applicable under another accepted branch.  In that case every
    controlled applicable value is allowed for derivation; invalid combinations
    are removed by the normal axis invariant validator inside
    ``derive_classification``.
    """

    expected_uncertain = _gold_uncertain_axes(gold)
    option_by_key: dict[str, dict[str, Any]] = {}
    for in_scope, reject_reason, medium in itertools.product(
        gold["in_scope"]["acceptable_labels"],
        gold["reject_reason"]["acceptable_labels"],
        gold["medium"]["acceptable_labels"],
    ):
        field_options: list[tuple[Any, ...]] = []
        for field in SEMANTIC_AXIS_FIELDS:
            if not _semantic_axis_applicable(bool(in_scope), str(medium), field):
                field_options.append(("not_applicable",))
                continue
            decision = gold[field]
            if decision["clarity"] == "not_judgeable":
                field_options.append(
                    tuple(value for value in FIELD_VALUES[field] if value != "not_applicable")
                )
            else:
                field_options.append(tuple(decision["acceptable_labels"]))

        for values in itertools.product(*field_options):
            row: dict[str, Any] = {
                "in_scope": bool(in_scope),
                "reject_reason": reject_reason,
                "medium": medium,
                **dict(zip(SEMANTIC_AXIS_FIELDS, values)),
                "resolution_insufficient": False,
            }
            uncertain = []
            if "scope" in expected_uncertain:
                uncertain.append("scope")
            for field in AXIS_FIELDS:
                if row[field] == "not_applicable":
                    continue
                if field in expected_uncertain or row[field] == "unknown":
                    uncertain.append(field)
            row["uncertain_axes"] = uncertain
            try:
                derived = _normalize_derived(derive_classification(row))
            except ValueError:
                continue
            option_by_key[canonical_json(derived)] = derived

    if not option_by_key:
        raise ValueError("gold alternatives do not produce a valid derived classification")
    return tuple(option_by_key[key] for key in sorted(option_by_key))


def _derived_key(value: Mapping[str, Any]) -> tuple[str, tuple[str, ...], str]:
    return (
        str(value["primary_class"]),
        tuple(value["secondary_classes"]),
        str(value["usage_status"]),
    )


def _best_secondary_counts(
    predicted: Sequence[str], options: Sequence[Mapping[str, Any]]
) -> tuple[int, int, int]:
    """Choose the accepted branch that best matches predicted secondary labels."""

    predicted_set = set(predicted)
    candidates: list[tuple[tuple[float, int, int, int], tuple[int, int, int]]] = []
    for option in options:
        expected_set = set(option["secondary_classes"])
        tp = len(predicted_set & expected_set)
        fp = len(predicted_set - expected_set)
        fn = len(expected_set - predicted_set)
        denominator = 2 * tp + fp + fn
        f1 = 2 * tp / denominator if denominator else 1.0
        candidates.append(((f1, tp, -fp, -fn), (tp, fp, fn)))
    return max(candidates, key=lambda item: item[0])[1]


def _metrics(
    conn: sqlite3.Connection,
    *,
    max_sample_rank: int | None = None,
    write_axis_metrics: bool = True,
    include_usage: bool = True,
) -> dict[str, Any]:
    rank_filter = "" if max_sample_rank is None else "WHERE g.sample_rank<=?"
    parameters: tuple[Any, ...] = () if max_sample_rank is None else (max_sample_rank,)
    joined = conn.execute(
        """
        SELECT g.gold_review_json,g.gold_derived_json,r.result_json,
               r.derived_primary_class,r.derived_secondary_classes_json,r.derived_usage_status
        FROM gold_samples g JOIN vision_results r ON r.asset_key=g.asset_key
        %s
        ORDER BY g.sample_rank
        """
        % rank_filter,
        parameters,
    ).fetchall()
    records = [
        {
            "gold": json.loads(row[0]),
            "gold_derived": json.loads(row[1]),
            "gold_derived_options": _gold_derived_options(json.loads(row[0])),
            "predicted": json.loads(row[2]),
            "predicted_derived": {
                "primary_class": row[3],
                "secondary_classes": json.loads(row[4]),
                "usage_status": row[5],
            },
        }
        for row in joined
    ]
    output: dict[str, Any] = {}
    sql_rows: list[tuple[Any, ...]] = []
    for field in EVALUATION_FIELDS:
        by_scope: dict[str, Any] = {}
        for scope in ("all", *CLARITIES):
            subset = records if scope == "all" else [
                row for row in records if row["gold"][field]["clarity"] == scope
            ]
            judged_rows = [
                row for row in subset if row["gold"][field]["clarity"] != "not_judgeable"
            ]
            primary_correct = sum(
                _predicted_field(row["predicted"], field) == row["gold"][field]["primary"]
                for row in judged_rows
            )
            acceptable_correct = sum(
                _predicted_field(row["predicted"], field)
                in row["gold"][field]["acceptable_labels"]
                for row in judged_rows
            )
            applicability_rows = []
            for row in subset:
                options = _gold_applicability_options(row["gold"], field)
                if len(options) == 1:
                    applicability_rows.append((row, next(iter(options))))
            applicability_correct = sum(
                _is_applicable(field, _predicted_field(row["predicted"], field))
                == expected
                for row, expected in applicability_rows
            )
            counts: dict[str, dict[str, int]] = {}
            for row in judged_rows:
                gold_value = canonical_json(row["gold"][field]["primary"])
                predicted_value = canonical_json(_predicted_field(row["predicted"], field))
                key = "%s -> %s" % (gold_value, predicted_value)
                counts[key] = {"count": counts.get(key, {}).get("count", 0) + 1}
            value = {
                "total": len(subset),
                "judged": len(judged_rows),
                "primary_correct": primary_correct,
                "primary_accuracy": round(primary_correct / len(judged_rows), 6) if judged_rows else None,
                "acceptable_correct": acceptable_correct,
                "acceptable_accuracy": round(acceptable_correct / len(judged_rows), 6) if judged_rows else None,
                "applicability_judged": len(applicability_rows),
                "applicability_correct": applicability_correct,
                "applicability_accuracy": round(
                    applicability_correct / len(applicability_rows), 6
                )
                if applicability_rows
                else None,
                "counts": counts,
            }
            by_scope[scope] = value
            sql_rows.append(
                (
                    field,
                    scope,
                    value["total"],
                    value["judged"],
                    primary_correct,
                    acceptable_correct,
                    value["applicability_judged"],
                    applicability_correct,
                    value["primary_accuracy"],
                    value["acceptable_accuracy"],
                    value["applicability_accuracy"],
                    canonical_json(counts),
                )
            )
        output[field] = by_scope
    if write_axis_metrics:
        conn.execute("DELETE FROM axis_metrics")
        conn.executemany(
            "INSERT INTO axis_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", sql_rows
        )

    total = len(records)
    pooled_total = 0
    pooled_correct = 0
    all_fields_correct = 0
    for row in records:
        sample_total = 0
        sample_correct = 0
        for field in EVALUATION_FIELDS:
            decision = row["gold"][field]
            if decision["clarity"] == "not_judgeable":
                continue
            sample_total += 1
            sample_correct += int(
                _predicted_field(row["predicted"], field)
                in decision["acceptable_labels"]
            )
        pooled_total += sample_total
        pooled_correct += sample_correct
        all_fields_correct += int(sample_total > 0 and sample_correct == sample_total)

    secondary_counts = [
        _best_secondary_counts(
            row["predicted_derived"]["secondary_classes"],
            row["gold_derived_options"],
        )
        for row in records
    ]
    secondary_tp = sum(counts[0] for counts in secondary_counts)
    secondary_fp = sum(counts[1] for counts in secondary_counts)
    secondary_fn = sum(counts[2] for counts in secondary_counts)
    secondary_micro_f1 = (
        2 * secondary_tp / (2 * secondary_tp + secondary_fp + secondary_fn)
        if 2 * secondary_tp + secondary_fp + secondary_fn
        else 1.0
    )
    derived = {
        "total": total,
        "accepted_branch_option_count": sum(
            len(row["gold_derived_options"]) for row in records
        ),
        "ambiguous_derived_sample_count": sum(
            len(row["gold_derived_options"]) > 1 for row in records
        ),
        "classification_tuple_correct": sum(
            _derived_key(row["predicted_derived"])
            in {_derived_key(option) for option in row["gold_derived_options"]}
            for row in records
        ),
        "primary_class_correct": sum(
            row["predicted_derived"]["primary_class"]
            in {
                option["primary_class"]
                for option in row["gold_derived_options"]
            }
            for row in records
        ),
        "secondary_classes_exact": sum(
            any(
                set(option["secondary_classes"])
                == set(row["predicted_derived"]["secondary_classes"])
                for option in row["gold_derived_options"]
            )
            for row in records
        ),
        "usage_status_correct": sum(
            row["predicted_derived"]["usage_status"]
            in {
                option["usage_status"]
                for option in row["gold_derived_options"]
            }
            for row in records
        ),
        "secondary_classes_micro_f1": round(secondary_micro_f1, 6),
        "secondary_classes_tp": secondary_tp,
        "secondary_classes_fp": secondary_fp,
        "secondary_classes_fn": secondary_fn,
    }
    for count_key, accuracy_key in (
        ("classification_tuple_correct", "classification_tuple_accuracy"),
        ("primary_class_correct", "primary_class_accuracy"),
        ("secondary_classes_exact", "secondary_classes_exact_accuracy"),
        ("usage_status_correct", "usage_status_accuracy"),
    ):
        derived[accuracy_key] = round(derived[count_key] / total, 6) if total else None

    clear_records = [
        row for row in records if not _gold_uncertain_axes(row["gold"])
    ]
    clear_total = len(clear_records)
    derived["clear_sample_count"] = clear_total
    for count_name, accuracy_name, predicate in (
        (
            "clear_primary_class_correct",
            "clear_primary_class_accuracy",
            lambda row: row["gold_derived"]["primary_class"]
            == row["predicted_derived"]["primary_class"],
        ),
        (
            "clear_secondary_classes_exact",
            "clear_secondary_classes_exact_accuracy",
            lambda row: set(row["gold_derived"]["secondary_classes"])
            == set(row["predicted_derived"]["secondary_classes"]),
        ),
        (
            "clear_usage_status_correct",
            "clear_usage_status_accuracy",
            lambda row: row["gold_derived"]["usage_status"]
            == row["predicted_derived"]["usage_status"],
        ),
    ):
        correct = sum(predicate(row) for row in clear_records)
        derived[count_name] = correct
        derived[accuracy_name] = (
            round(correct / clear_total, 6) if clear_total else None
        )

    boundary_images = 0
    boundary_images_flagged = 0
    uncertain_tp = 0
    uncertain_fp = 0
    uncertain_fn = 0
    uncertain_skipped_conditional = 0
    predicted_uncertain_occurrences = 0
    for row in records:
        expected = _gold_uncertain_axes(row["gold"])
        predicted = frozenset(row["predicted"]["uncertain_axes"])
        if expected:
            boundary_images += 1
            boundary_images_flagged += int(bool(expected & predicted))
        uncertain_tp += len(expected & predicted)
        uncertain_fn += len(expected - predicted)
        predicted_uncertain_occurrences += len(predicted)
        for axis in predicted - expected:
            if (
                axis != "scope"
                and len(_gold_applicability_options(row["gold"], axis)) > 1
            ):
                uncertain_skipped_conditional += 1
            else:
                uncertain_fp += 1
    uncertainty = {
        "gold_boundary_images": boundary_images,
        "gold_boundary_axis_occurrences": uncertain_tp + uncertain_fn,
        "boundary_images_flagged_uncertain": boundary_images_flagged,
        "predicted_uncertain_axis_occurrences": predicted_uncertain_occurrences,
        "uncertain_axis_scored_predicted_occurrences": uncertain_tp + uncertain_fp,
        "uncertain_axis_skipped_conditional_applicability": uncertain_skipped_conditional,
        "uncertain_axis_true_positive": uncertain_tp,
        "uncertain_axis_false_positive": uncertain_fp,
        "uncertain_axis_false_negative": uncertain_fn,
        "uncertain_axis_recall": round(
            uncertain_tp / (uncertain_tp + uncertain_fn), 6
        )
        if uncertain_tp + uncertain_fn
        else None,
        "uncertain_axis_precision": round(
            uncertain_tp / (uncertain_tp + uncertain_fp), 6
        )
        if uncertain_tp + uncertain_fp
        else None,
        "resolution_insufficient_count": sum(
            bool(row["predicted"]["resolution_insufficient"]) for row in records
        ),
    }
    aggregate = {
        "sample_count": total,
        "all_judged_fields_acceptable": all_fields_correct,
        "all_judged_fields_acceptable_accuracy": round(all_fields_correct / total, 6)
        if total
        else None,
        "applicable_field_acceptable_correct": pooled_correct,
        "applicable_field_acceptable_total": pooled_total,
        "applicable_field_acceptable_accuracy": round(pooled_correct / pooled_total, 6)
        if pooled_total
        else None,
    }
    metrics = {
        "aggregate": aggregate,
        "axes": output,
        "derived_classification": derived,
        "uncertainty": uncertainty,
    }
    if include_usage:
        metrics["usage"] = _usage_metrics(conn)
    return metrics


def _usage_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*),SUM(status='success'),SUM(status='failed'),
               COALESCE(SUM(input_tokens),0),COALESCE(SUM(cached_input_tokens),0),
               COALESCE(SUM(output_tokens),0),COALESCE(SUM(elapsed_ms),0)
        FROM vision_attempts
        """
    ).fetchone()
    return {
        "vision_attempts": int(row[0]),
        "successful_attempts": int(row[1] or 0),
        "failed_attempts": int(row[2] or 0),
        "input_tokens": int(row[3] or 0),
        "cached_input_tokens": int(row[4] or 0),
        "output_tokens": int(row[5] or 0),
        "model_elapsed_ms": int(row[6] or 0),
        "fetch_attempts": int(conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0]),
        "downloaded_bytes": int(
            conn.execute("SELECT COALESCE(SUM(response_bytes),0) FROM fetch_attempts").fetchone()[0]
        ),
    }


def _nested_prefix_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "N%d" % prefix: _metrics(
            conn,
            max_sample_rank=prefix,
            write_axis_metrics=False,
            include_usage=False,
        )
        for prefix in SUPPORTED_LIMITS
    }


def _validations(
    conn: sqlite3.Connection,
    *,
    source_sha_after: str,
    manifest_file_sha_after: str,
) -> list[dict[str, Any]]:
    run = conn.execute(
        "SELECT requested_limit,batch_count,source_sha256_before,gold_manifest_file_sha256 FROM benchmark_run WHERE run_id=1"
    ).fetchone()
    limit, batch_count = int(run[0]), int(run[1])
    successful = conn.execute(
        "SELECT batch_no,review_ids_json,asset_keys_json,input_tokens,output_tokens FROM vision_attempts WHERE status='success' ORDER BY attempt_id"
    ).fetchall()
    expected_calls = []
    for batch_no in range(1, batch_count + 1):
        rows = conn.execute(
            "SELECT review_id,asset_key FROM gold_samples WHERE sample_rank BETWEEN ? AND ? ORDER BY sample_rank",
            ((batch_no - 1) * FIXED_BATCH_SIZE + 1, batch_no * FIXED_BATCH_SIZE),
        ).fetchall()
        expected_calls.append((batch_no, [row[0] for row in rows], [row[1] for row in rows]))
    actual_calls = [(int(row[0]), json.loads(row[1]), json.loads(row[2])) for row in successful]
    schema_matches = int(
        conn.execute(
            "SELECT COUNT(*) FROM vision_attempts WHERE status='success' AND output_schema_sha256=?",
            (_schema_sha256(),),
        ).fetchone()[0]
    )
    integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    fk_count = len(conn.execute("PRAGMA foreign_key_check").fetchall())

    def value(name: str, passed: bool, expected: Any, actual: Any, detail: str | None = None):
        return {
            "validation_name": name,
            "severity": "error",
            "passed": passed,
            "expected": str(expected),
            "actual": str(actual),
            "detail": detail,
        }

    return [
        value("nested_prefix_sample_count", conn.execute("SELECT COUNT(*) FROM gold_samples").fetchone()[0] == limit, limit, conn.execute("SELECT COUNT(*) FROM gold_samples").fetchone()[0]),
        value("fetch_success", conn.execute("SELECT COUNT(*) FROM fetch_results WHERE status='success'").fetchone()[0] == limit, limit, conn.execute("SELECT COUNT(*) FROM fetch_results WHERE status='success'").fetchone()[0]),
        value("frozen_content_sha_match", conn.execute("SELECT COUNT(*) FROM fetch_results WHERE status='success' AND expected_content_sha256=actual_content_sha256").fetchone()[0] == limit, limit, conn.execute("SELECT COUNT(*) FROM fetch_results WHERE status='success' AND expected_content_sha256=actual_content_sha256").fetchone()[0]),
        value("local_1024_derivative_count", conn.execute("SELECT COUNT(*) FROM derived_inputs WHERE lane='long1024' AND max_long_edge=1024 AND width<=1024 AND height<=1024").fetchone()[0] == limit, limit, conn.execute("SELECT COUNT(*) FROM derived_inputs").fetchone()[0]),
        value("vision_result_accounting", conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0] == limit, limit, conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0]),
        value("successful_call_accounting", len(successful) == batch_count, batch_count, len(successful)),
        value("ordered_batch_contract", actual_calls == expected_calls, "exact ordered batches of five", "match" if actual_calls == expected_calls else "mismatch"),
        value("token_usage_present", all(row[3] is not None and row[4] is not None for row in successful), batch_count, sum(row[3] is not None and row[4] is not None for row in successful)),
        value("axis_output_schema_contract", schema_matches == batch_count, batch_count, schema_matches),
        value("source_immutable", source_sha_after == run[2], run[2], source_sha_after),
        value("gold_manifest_immutable", manifest_file_sha_after == run[3], run[3], manifest_file_sha_after),
        value("sqlite_quick_check", integrity == "ok", "ok", integrity),
        value("foreign_keys", fk_count == 0, 0, fk_count),
    ]


def _insert_validations(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> None:
    conn.execute("DELETE FROM validations")
    conn.executemany(
        "INSERT INTO validations VALUES(?,?,?,?,?,?)",
        [
            (
                row["validation_name"],
                row["severity"],
                int(bool(row["passed"])),
                row.get("expected"),
                row["actual"],
                row.get("detail"),
            )
            for row in rows
        ],
    )


def logical_sha256(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, order_by in (
        ("benchmark_run", "run_id"),
        ("gold_samples", "sample_rank"),
        ("fetch_results", "asset_key"),
        ("derived_inputs", "asset_key"),
        ("vision_results", "asset_key"),
        ("axis_metrics", "field_name,scope"),
        ("validations", "validation_name"),
    ):
        columns = [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]
        excluded = {"started_at", "completed_at", "logical_sha256"} if table == "benchmark_run" else set()
        selected = [column for column in columns if column not in excluded]
        digest.update((table + "\0").encode("utf-8"))
        for row in conn.execute(
            "SELECT %s FROM %s ORDER BY %s" % (",".join(selected), table, order_by)
        ):
            digest.update(canonical_json(dict(zip(selected, row))).encode("utf-8") + b"\n")
    return digest.hexdigest()


def _percentage(value: Any) -> str:
    return "n/a" if value is None else "%.1f%%" % (float(value) * 100)


def render_report(conn: sqlite3.Connection, artifact_path: Path) -> str:
    conn.row_factory = sqlite3.Row
    run = dict(conn.execute("SELECT * FROM benchmark_run WHERE run_id=1").fetchone())
    metrics = json.loads(run["metrics_json"])
    usage = metrics["usage"]
    aggregate = metrics["aggregate"]
    label_note = (
        "independent human review"
        if run["independent_human"]
        else "agent/model-assisted review; this is not independent-human accuracy"
    )
    is_holdout = run["purpose"] == FRESH_HOLDOUT_PURPOSE
    report_title = (
        "# Divisare 1024px axis fresh holdout benchmark"
        if is_holdout
        else "# Divisare 1024px axis development benchmark"
    )
    interpretation_note = (
        "- This is the frozen one-shot holdout result for the final prompt. "
        "Because its labels are agent/model-assisted, it is not a production "
        "approval by itself."
        if is_holdout
        else "- This is development feedback for the prompt and codebook. It is not a final or production accuracy claim."
    )
    overall_heading = (
        "## Overall holdout result" if is_holdout else "## Overall development result"
    )
    lines = [
        report_title,
        "",
        "## What this run means",
        "",
        "- Transport/schema result: **%s**" % ("PASS" if run["technical_gate_passed"] else "FAIL"),
        "- Images tested: `%d` from the frozen N50 prefix" % run["requested_limit"],
        "- SQLite artifact: `%s`" % artifact_path,
        "- Input size: one `1024px` long-edge derivative per image",
        "- Images kept after analysis: `no`",
        "- Label source: %s" % label_note,
        interpretation_note,
        "- This PASS/FAIL says whether files, hashes, batches, and schemas worked; it does not say the classifications are good enough.",
        "",
        "## How to read the scores",
        "",
        "- Exact answer: the model chose the reviewers' preferred answer.",
        "- Accepted answer: the model chose any answer reviewers considered reasonable for an ambiguous image.",
        "- Applicability: the model correctly recognized whether that axis could be judged for the image.",
        "- Applicability omits rows where an accepted scope/medium alternative would change whether the downstream axis applies.",
        "- `n/a`: reviewers deliberately marked that axis as not judgeable, so no forced right/wrong score was assigned.",
        "",
        overall_heading,
        "",
        "- Images where every judgeable field was acceptable: `%d/%d` (%s)"
        % (
            aggregate["all_judged_fields_acceptable"],
            aggregate["sample_count"],
            _percentage(aggregate["all_judged_fields_acceptable_accuracy"]),
        ),
        "- Accepted field answers across all judgeable fields: `%d/%d` (%s)"
        % (
            aggregate["applicable_field_acceptable_correct"],
            aggregate["applicable_field_acceptable_total"],
            _percentage(aggregate["applicable_field_acceptable_accuracy"]),
        ),
        "",
        "## Axis results",
        "",
        "| Image fact | Judged | Exact answer | Accepted answer | Applicability |",
        "|---|---:|---:|---:|---:|",
    ]
    if is_holdout:
        meaning_index = lines.index("## How to read the scores")
        lines[meaning_index:meaning_index] = [
            "- One-shot receipt: `%s`" % run["one_shot_receipt_path"],
            "- One-shot receipt SHA-256: `%s`" % run["one_shot_receipt_sha256"],
            "- The receipt permanently blocks a second logical run and permits "
            "only same-output resume. A process crash during an external model "
            "request can still require retrying that unfinished batch.",
            "",
        ]
        prefix_lines = [
            "## Nested prefix results from this N50 run",
            "",
            "N10 and N20 below are calculated from the first 10 and 20 rows of "
            "this same N50 execution; they are not separate model runs.",
            "",
            "| Prefix | Images | Every judged field acceptable | Accepted field answers |",
            "|---|---:|---:|---:|",
        ]
        for prefix_name in ("N10", "N20", "N50"):
            prefix_aggregate = metrics["prefixes"][prefix_name]["aggregate"]
            prefix_lines.append(
                "| %s | %d | %s | %s |"
                % (
                    prefix_name,
                    prefix_aggregate["sample_count"],
                    _percentage(
                        prefix_aggregate["all_judged_fields_acceptable_accuracy"]
                    ),
                    _percentage(
                        prefix_aggregate["applicable_field_acceptable_accuracy"]
                    ),
                )
            )
        prefix_lines.append("")
        axis_heading_index = lines.index("## Axis results")
        lines[axis_heading_index:axis_heading_index] = prefix_lines
    labels = {
        "in_scope": "Usable project image",
        "reject_reason": "Reason not usable",
        "medium": "Representation medium",
        "spatial_context": "Inside / outside",
        "framing_scale": "View scale",
        "camera_angle": "Camera angle",
        "drawing_kind": "Drawing kind",
        "project_state": "Visible project state",
    }
    for field in EVALUATION_FIELDS:
        row = metrics["axes"][field]["all"]
        lines.append(
            "| %s | %d/%d | %s | %s | %s |"
            % (
                labels[field],
                row["judged"],
                row["total"],
                _percentage(row["primary_accuracy"]),
                _percentage(row["acceptable_accuracy"]),
                "%s (%d/%d)"
                % (
                    _percentage(row["applicability_accuracy"]),
                    row["applicability_correct"],
                    row["applicability_judged"],
                ),
            )
        )
    derived = metrics["derived_classification"]
    lines.extend(
        [
            "",
            "## Search classification derived from the axes",
            "",
            "These values were recomputed from each result; model-supplied derived values were not trusted blindly.",
            "For ambiguous gold, a result is accepted when it matches a coherent branch allowed by the reviewers' accepted axis labels.",
            "",
            "- Entire derived classification matched one accepted branch: `%d/%d` (%s)"
            % (
                derived["classification_tuple_correct"],
                derived["total"],
                _percentage(derived["classification_tuple_accuracy"]),
            ),
            "- Main class matched an accepted branch: `%d/%d` (%s)"
            % (derived["primary_class_correct"], derived["total"], _percentage(derived["primary_class_accuracy"])),
            "- Supporting classes matched an accepted branch: `%d/%d` (%s)"
            % (derived["secondary_classes_exact"], derived["total"], _percentage(derived["secondary_classes_exact_accuracy"])),
            "- Supporting search classes overlap score: `%s`"
            % _percentage(derived["secondary_classes_micro_f1"]),
            "- Use/exclude status matched an accepted branch: `%d/%d` (%s)"
            % (derived["usage_status_correct"], derived["total"], _percentage(derived["usage_status_accuracy"])),
            "- Ambiguous images with more than one possible derived result: `%d`"
            % derived["ambiguous_derived_sample_count"],
            "- On `%d` images with no reviewer boundary or unknown axis: main `%d/%d` (%s), supporting `%d/%d` (%s), use/exclude `%d/%d` (%s)"
            % (
                derived["clear_sample_count"],
                derived["clear_primary_class_correct"],
                derived["clear_sample_count"],
                _percentage(derived["clear_primary_class_accuracy"]),
                derived["clear_secondary_classes_exact"],
                derived["clear_sample_count"],
                _percentage(derived["clear_secondary_classes_exact_accuracy"]),
                derived["clear_usage_status_correct"],
                derived["clear_sample_count"],
                _percentage(derived["clear_usage_status_accuracy"]),
            ),
            "",
            "## Uncertainty behavior",
            "",
            "- Reviewer-uncertain images given a matching uncertainty flag: `%d/%d`"
            % (
                metrics["uncertainty"]["boundary_images_flagged_uncertain"],
                metrics["uncertainty"]["gold_boundary_images"],
            ),
            "- Reviewer-uncertain axes flagged uncertain by the model: `%d/%d` (%s)"
            % (
                metrics["uncertainty"]["uncertain_axis_true_positive"],
                metrics["uncertainty"]["gold_boundary_axis_occurrences"],
                _percentage(metrics["uncertainty"]["uncertain_axis_recall"]),
            ),
            "- Extra uncertainty flags among scored axes: `%d`"
            % metrics["uncertainty"]["uncertain_axis_false_positive"],
            "- Model uncertainty flags scored: `%d`; skipped because accepted scope/medium branches disagree on axis applicability: `%d`"
            % (
                metrics["uncertainty"]["uncertain_axis_scored_predicted_occurrences"],
                metrics["uncertainty"]["uncertain_axis_skipped_conditional_applicability"],
            ),
            "- Images marked resolution-insufficient at 1024px: `%d`"
            % metrics["uncertainty"]["resolution_insufficient_count"],
            "",
            "## Run accounting",
            "",
            "- Successful model calls: `%d`" % usage["successful_attempts"],
            "- Failed model calls retained for audit: `%d`" % usage["failed_attempts"],
            "- Input tokens: `%d`" % usage["input_tokens"],
            "- Cached input tokens (already included in input): `%d`" % usage["cached_input_tokens"],
            "- Output tokens: `%d`" % usage["output_tokens"],
            "- Downloaded bytes across attempts: `%d`" % usage["downloaded_bytes"],
            "- Source SHA before: `%s`" % run["source_sha256_before"],
            "- Source SHA after: `%s`" % run["source_sha256_after"],
            "- Gold manifest file SHA: `%s`" % run["gold_manifest_file_sha256"],
            "- Logical output SHA: `%s`" % run["logical_sha256"],
            "- Model: `%s`" % run["model"],
            "- Axis contract: `%s`" % run["axis_contract_version"],
            "- Gold labels were created under prompt: `%s`"
            % run["gold_axis_prompt_version"],
            "- Evaluated prompt contract: `%s`" % run["axis_prompt_version"],
            "",
            "## Technical checks",
            "",
        ]
    )
    for row in conn.execute(
        "SELECT validation_name,passed,expected,actual,detail FROM validations ORDER BY validation_name"
    ):
        lines.append(
            "- `%s`: **%s** (expected `%s`, actual `%s`)%s"
            % (
                row[0],
                "PASS" if row[1] else "FAIL",
                row[2] or "",
                row[3],
                " - " + row[4] if row[4] else "",
            )
        )
    return "\n".join(lines) + "\n"


def _publish_pair(partial_db: Path, output_db: Path, partial_report: Path, report: Path) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    linked_db = False
    try:
        os.link(partial_db, output_db)
        linked_db = True
        os.link(partial_report, report)
    except FileExistsError as exc:
        if linked_db:
            output_db.unlink()
        raise FileExistsError("immutable output or report already exists") from exc
    partial_db.unlink()
    partial_report.unlink()


def run_axes_benchmark(
    *,
    source_db: Path,
    gold_manifest_path: Path,
    output_db: Path,
    report_path: Path,
    limit: int,
    codex_bin: Path,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cli_version: Optional[str] = None,
    resume: bool = False,
    fetcher: Callable[[str], FetchPayload] = network_fetch,
    executor: Callable[..., VisionRuntimeResult] = run_codex_vision_batch,
) -> dict[str, Any]:
    """Run a frozen N10/N20/N50 1024-only development or holdout benchmark."""
    source_db = source_db.resolve()
    gold_manifest_path = gold_manifest_path.resolve()
    output_db = output_db.resolve()
    report_path = report_path.resolve()
    if len({source_db, gold_manifest_path, output_db, report_path}) != 4:
        raise ValueError("source, gold, output, and report paths must be distinct")
    if output_db.exists():
        raise FileExistsError("immutable output already exists: %s" % output_db)
    if report_path.exists():
        raise FileExistsError("immutable report already exists: %s" % report_path)
    partial_db = output_db.with_name(output_db.name + ".partial")
    partial_report = report_path.with_name(report_path.name + ".partial")
    if partial_report.exists():
        raise FileExistsError("stale partial report exists: %s" % partial_report)

    payload, samples, manifest_file_sha, source_sha = load_axis_gold_manifest(
        gold_manifest_path, source_db, limit
    )
    manifest_contract = _axis_gold_manifest_contract(payload)
    one_shot_receipt: HoldoutOneShotReceipt | None = None
    receipt_preexisting = False
    if manifest_contract is HOLDOUT_GOLD_CONTRACT:
        one_shot_receipt = _build_holdout_one_shot_receipt(
            samples=samples,
            source_db=source_db,
            source_sha256=source_sha,
            gold_manifest_path=gold_manifest_path,
            gold_manifest_payload=payload,
            gold_manifest_file_sha256=manifest_file_sha,
            output_db=output_db,
            report_path=report_path,
            partial_db=partial_db,
            codex_bin=codex_bin,
            model=model,
            reasoning=reasoning,
            service_tier=service_tier,
            cli_version=cli_version,
        )
        receipt_preexisting = one_shot_receipt.path.exists()
        if receipt_preexisting:
            _validate_existing_holdout_receipt(one_shot_receipt)
            if not resume:
                raise RuntimeError(
                    "fresh holdout one-shot was already claimed; only same-run resume is allowed"
                )
            if not partial_db.exists():
                raise RuntimeError(
                    "fresh holdout receipt exists but its running partial sidecar is missing"
                )
    partial_db.parent.mkdir(parents=True, exist_ok=True)
    partial_existed = partial_db.exists()
    conn: sqlite3.Connection | None = None
    try:
        if partial_db.exists():
            if not resume:
                raise FileExistsError("partial sidecar exists; pass resume=True: %s" % partial_db)
            conn = sqlite3.connect(partial_db)
            conn.execute("PRAGMA foreign_keys=ON")
            _validate_resume(
                conn,
                samples=samples,
                manifest_path=gold_manifest_path,
                manifest_payload=payload,
                manifest_file_sha256=manifest_file_sha,
                source_db=source_db,
                source_sha256=source_sha,
                model=model,
                reasoning=reasoning,
                service_tier=service_tier,
                cli_version=cli_version,
                one_shot_receipt=one_shot_receipt,
            )
        else:
            conn = sqlite3.connect(partial_db)
            conn.execute("PRAGMA foreign_keys=ON")
            initialize_sidecar(
                conn,
                samples=samples,
                manifest_path=gold_manifest_path,
                manifest_payload=payload,
                manifest_file_sha256=manifest_file_sha,
                source_db=source_db,
                source_sha256=source_sha,
                model=model,
                reasoning=reasoning,
                service_tier=service_tier,
                cli_version=cli_version,
                one_shot_receipt=one_shot_receipt,
            )
        if one_shot_receipt is not None and not receipt_preexisting:
            if partial_existed and not _partial_sidecar_is_preclaim(conn):
                raise RuntimeError(
                    "fresh holdout partial sidecar has external work but no one-shot receipt"
                )
            _claim_holdout_receipt(one_shot_receipt)
    except Exception:
        if conn is not None:
            conn.close()
        raise
    assert conn is not None

    schema_text = canonical_json(AXIS_OUTPUT_SCHEMA)
    metrics: dict[str, Any] = {}
    logical = ""
    try:
        for batch_index in range(limit // FIXED_BATCH_SIZE):
            batch_no = batch_index + 1
            batch = samples[batch_index * FIXED_BATCH_SIZE : (batch_index + 1) * FIXED_BATCH_SIZE]
            needed = [
                sample
                for sample in batch
                if conn.execute(
                    "SELECT 1 FROM vision_results WHERE asset_key=?", (sample.asset_key,)
                ).fetchone()
                is None
            ]
            if not needed:
                continue
            if len(needed) != FIXED_BATCH_SIZE:
                raise RuntimeError(
                    "partial successful Vision batch detected; refusing a non-frozen batch retry"
                )
            derivatives: dict[str, PreparedDerivative] = {}
            for sample in needed:
                started = time.perf_counter()
                fetched: FetchPayload | None = None
                actual_sha: str | None = None
                try:
                    fetched = fetcher(sample.request_url)
                    actual_sha = _sha256_bytes(fetched.raw)
                    elapsed_ms = round((time.perf_counter() - started) * 1000)
                    if actual_sha != sample.expected_content_sha256:
                        message = "frozen response SHA mismatch for %s" % sample.sample_id
                        _write_fetch_attempt(
                            conn,
                            sample=sample,
                            batch_no=batch_no,
                            status="content_mismatch",
                            elapsed_ms=elapsed_ms,
                            payload=fetched,
                            actual_sha=actual_sha,
                            error_kind="content_sha256_mismatch",
                            error_message=message,
                        )
                        _write_failed_fetch_result(
                            conn,
                            sample=sample,
                            status="content_mismatch",
                            elapsed_ms=elapsed_ms,
                            payload=fetched,
                            actual_sha=actual_sha,
                            error_kind="content_sha256_mismatch",
                            error_message=message,
                        )
                        conn.commit()
                        raise _FrozenContentMismatch(message)
                    decoded = decode_source(fetched.raw)
                    derivative = prepare_derivative(decoded, LANE, MAX_LONG_EDGE)
                    _write_fetch_attempt(
                        conn,
                        sample=sample,
                        batch_no=batch_no,
                        status="success",
                        elapsed_ms=elapsed_ms,
                        payload=fetched,
                        actual_sha=actual_sha,
                    )
                    _retain_or_write_input(
                        conn,
                        sample=sample,
                        payload=fetched,
                        decoded=decoded,
                        derivative=derivative,
                        elapsed_ms=elapsed_ms,
                    )
                    derivatives[sample.asset_key] = derivative
                    conn.commit()
                except _FrozenContentMismatch:
                    raise
                except Exception as exc:
                    elapsed_ms = round((time.perf_counter() - started) * 1000)
                    kind = str(getattr(exc, "kind", exc.__class__.__name__))
                    _write_fetch_attempt(
                        conn,
                        sample=sample,
                        batch_no=batch_no,
                        status="failed",
                        elapsed_ms=elapsed_ms,
                        payload=fetched,
                        actual_sha=actual_sha,
                        error_kind=kind,
                        error_message=str(exc),
                    )
                    _write_failed_fetch_result(
                        conn,
                        sample=sample,
                        status="failed",
                        elapsed_ms=elapsed_ms,
                        payload=fetched,
                        actual_sha=actual_sha,
                        error_kind=kind,
                        error_message=str(exc),
                    )
                    conn.commit()
                    raise RuntimeError("axes benchmark fetch failed for %s" % sample.sample_id) from exc

            with tempfile.TemporaryDirectory(prefix="divisare-vision-axes-") as temp_name:
                temp_dir = Path(temp_name)
                schema_path = temp_dir / "output.schema.json"
                schema_path.write_text(schema_text, encoding="utf-8", newline="\n")
                image_paths: list[Path] = []
                review_ids = [sample.review_id for sample in needed]
                for index, sample in enumerate(needed, 1):
                    path = temp_dir / ("image-%02d.jpg" % index)
                    path.write_bytes(derivatives[sample.asset_key].encoded_bytes)
                    image_paths.append(path)
                result = executor(
                    prompt=compose_axes_prompt(review_ids),
                    image_paths=image_paths,
                    output_schema_path=schema_path,
                    expected_asset_ids=review_ids,
                    codex_bin=codex_bin,
                    model=model,
                    reasoning=reasoning,
                    service_tier=service_tier,
                    working_directory=temp_dir,
                    timeout_seconds=600,
                )
                if not result.ok:
                    _write_vision_attempt(
                        conn,
                        batch_no=batch_no,
                        samples=needed,
                        result=result,
                        status="failed",
                    )
                    conn.commit()
                    raise RuntimeError(
                        "axes Vision batch %d failed: %s" % (batch_no, result.error_message)
                    )
                try:
                    normalized = normalize_axes_batch(result.records, review_ids)
                    _write_vision_results(conn, needed, normalized)
                except Exception as exc:
                    _write_vision_attempt(
                        conn,
                        batch_no=batch_no,
                        samples=needed,
                        result=result,
                        status="failed",
                        error_kind="semantic_schema",
                        error_message=str(exc),
                    )
                    conn.commit()
                    raise
                _write_vision_attempt(
                    conn,
                    batch_no=batch_no,
                    samples=needed,
                    result=result,
                    status="success",
                )
                conn.commit()

        incomplete = int(
            conn.execute(
                "SELECT COUNT(*) FROM gold_samples g WHERE NOT EXISTS (SELECT 1 FROM vision_results r WHERE r.asset_key=g.asset_key)"
            ).fetchone()[0]
        )
        if incomplete:
            raise RuntimeError(
                "axes benchmark remains incomplete for %d samples; resume partial sidecar"
                % incomplete
            )
        source_after = file_sha256(source_db)
        manifest_after = file_sha256(gold_manifest_path)
        validations = _validations(
            conn,
            source_sha_after=source_after,
            manifest_file_sha_after=manifest_after,
        )
        _insert_validations(conn, validations)
        failures = [row for row in validations if not row["passed"]]
        if failures:
            conn.execute(
                "UPDATE benchmark_run SET status='failed_validation',source_sha256_after=?,technical_gate_passed=0,error=? WHERE run_id=1",
                (source_after, canonical_json(failures)),
            )
            conn.commit()
            raise RuntimeError("axes benchmark technical validation failed: %s" % canonical_json(failures))
        metrics = _metrics(conn)
        if payload["purpose"] == FRESH_HOLDOUT_PURPOSE:
            metrics["prefixes"] = _nested_prefix_metrics(conn)
        conn.execute(
            "UPDATE benchmark_run SET status='complete',source_sha256_after=?,completed_at=?,technical_gate_passed=1,metrics_json=?,error=NULL WHERE run_id=1",
            (source_after, utc_now(), canonical_json(metrics)),
        )
        conn.commit()
        logical = logical_sha256(conn)
        conn.execute("UPDATE benchmark_run SET logical_sha256=? WHERE run_id=1", (logical,))
        conn.commit()
        partial_report.parent.mkdir(parents=True, exist_ok=True)
        partial_report.write_text(render_report(conn, output_db), encoding="utf-8", newline="\n")
    finally:
        conn.close()

    _publish_pair(partial_db, output_db, partial_report, report_path)
    return {
        "output_db": str(output_db),
        "report_path": str(report_path),
        "source_sha256": source_sha,
        "gold_manifest_file_sha256": manifest_file_sha,
        "gold_manifest_logical_sha256": payload["logical_sha256"],
        "logical_sha256": logical,
        "technical_gate_passed": True,
        "development_only": bool(payload["development_only"]),
        "one_shot_receipt_path": (
            str(one_shot_receipt.path) if one_shot_receipt else None
        ),
        "one_shot_receipt_sha256": (
            one_shot_receipt.file_sha256 if one_shot_receipt else None
        ),
        "limit": limit,
        "metrics": metrics,
    }
