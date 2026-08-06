"""Freeze a blinded N50 from the fresh Divisare image-axis holdout pool."""

from __future__ import annotations

import copy
import hashlib
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from canonical import divisare_vision_axes as axes
from canonical import divisare_vision_axes_holdout as holdout
from canonical import divisare_vision_axes_holdout_probe as holdout_probe
from canonical import divisare_vision_axes_review as axes_review
from canonical import divisare_vision_probe as base_probe
from canonical.divisare_image_smoke import canonical_json, file_sha256
from canonical.divisare_vision_gold_finalize import parse_json_strict


MANIFEST_VERSION = "divisare-vision-axes-fresh-holdout-n50-v1.1.0"
SELECTOR_VERSION = "divisare-vision-axes-fresh-holdout-selector-v1.1.0"
BLIND_ID_VERSION = "axis-fresh-holdout-blind-v1"
REVIEW_ORDER_VERSION = "axis-fresh-holdout-review-order-v1"

EXPECTED_SOURCE_DB_SHA256 = (
    "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f"
)
EXPECTED_CANDIDATE_FILE_SHA256 = (
    "d12f69c17570ae6ef04b43c03ed9d914ffcf717c7d704fd180ffc8f6f06154c3"
)
EXPECTED_CANDIDATE_MANIFEST_SHA256 = (
    "55223915f3d6aab0659bb52a4dc2add4752a3791c451f5d61872c6738941934d"
)
EXPECTED_PRIOR_FILE_SHA256 = (
    "faecad6bd355f38d4553657a0ecb45ff77ba8ebee8f950889e3968f068fc39ad"
)
EXPECTED_PRIOR_MANIFEST_SHA256 = (
    "480f28d33f90210a479a19e4ab858a71d99ebc7336f8ee0e638be8d96e46f626"
)
EXPECTED_PROBED_FILE_SHA256 = (
    "c1fdce889eafbb144ed29759ef3aabe37da73a448e0bf8dacbd9f6491bff856a"
)
EXPECTED_PROBED_MANIFEST_SHA256 = (
    "1eb4a639e05fb8eb712c681d1da2115e75d4cb3bf4aa73af504d3675feedae19"
)
EXPECTED_BASE_PROBE_LOGICAL_SHA256 = (
    "de72260840d8eb69d83d23240d26b0a029f77b7a6259a6942c804682899feed7"
)
EXPECTED_CROSS_LOGICAL_SHA256 = (
    "cd9d9a69de0e5999d0290001397f0b90bae3517e95ef811c9debe53cf79bf036"
)
EXPECTED_SELECTED_ID_SET_SHA256 = (
    "45e810d0d6e1dfebf06b03e4a09118cd83521e84475021949ebb80ba68dc8cdf"
)
EXPECTED_SELECTED_ORDER_SHA256 = (
    "c8d55e6a0dc58faee0919229b0537610b04b3a9de7193cad8a53435273fa3c0f"
)
EXPECTED_SELECTED_AUDIT_SHA256 = (
    "b811e9bdc3766aa3ab27ee40cf542a34c433eaacf782d76181a477a14cbc09b5"
)

Cell = tuple[str, str, str]


def _cell(proxy: str, generation: str, role: str) -> Cell:
    return proxy, generation, role


# The first 10 and first 20 are balanced prefixes of the same frozen N50.
# Final cell counts halve the N100 allocation with deterministic integer
# rounding while preserving 35/15 generations and 22/28 cover/gallery roles.
SAMPLE_CELL_SEQUENCE: tuple[Cell, ...] = (
    _cell("exterior", "modern", "cover"),
    _cell("interior", "modern", "gallery"),
    _cell("drawing", "modern", "gallery"),
    _cell("detail", "modern", "gallery"),
    _cell("aerial", "modern", "cover"),
    _cell("out_of_scope", "modern", "gallery"),
    _cell("exterior", "legacy", "gallery"),
    _cell("interior", "legacy", "cover"),
    _cell("drawing", "modern", "cover"),
    _cell("detail", "legacy", "gallery"),
    _cell("exterior", "modern", "cover"),
    _cell("exterior", "modern", "gallery"),
    _cell("interior", "modern", "cover"),
    _cell("interior", "legacy", "gallery"),
    _cell("drawing", "modern", "gallery"),
    _cell("drawing", "legacy", "cover"),
    _cell("detail", "modern", "cover"),
    _cell("aerial", "modern", "gallery"),
    _cell("aerial", "legacy", "gallery"),
    _cell("out_of_scope", "modern", "cover"),
    _cell("exterior", "modern", "gallery"),
    _cell("interior", "modern", "gallery"),
    _cell("drawing", "modern", "cover"),
    _cell("detail", "modern", "gallery"),
    _cell("aerial", "modern", "cover"),
    _cell("out_of_scope", "legacy", "cover"),
    _cell("exterior", "legacy", "cover"),
    _cell("interior", "modern", "cover"),
    _cell("drawing", "modern", "gallery"),
    _cell("detail", "legacy", "cover"),
    _cell("aerial", "modern", "gallery"),
    _cell("out_of_scope", "legacy", "gallery"),
    _cell("exterior", "modern", "gallery"),
    _cell("interior", "legacy", "gallery"),
    _cell("drawing", "legacy", "gallery"),
    _cell("detail", "modern", "cover"),
    _cell("aerial", "legacy", "cover"),
    _cell("out_of_scope", "modern", "cover"),
    _cell("exterior", "modern", "cover"),
    _cell("interior", "modern", "gallery"),
    _cell("drawing", "modern", "cover"),
    _cell("detail", "modern", "gallery"),
    _cell("out_of_scope", "modern", "gallery"),
    _cell("exterior", "legacy", "cover"),
    _cell("interior", "modern", "cover"),
    _cell("drawing", "legacy", "gallery"),
    _cell("detail", "modern", "gallery"),
    _cell("exterior", "modern", "gallery"),
    _cell("interior", "modern", "gallery"),
    _cell("drawing", "modern", "gallery"),
)

if len(SAMPLE_CELL_SEQUENCE) != 50:
    raise RuntimeError("fresh holdout selection must contain 50 cell slots")

CELL_QUOTAS = Counter(SAMPLE_CELL_SEQUENCE)
EXPECTED_PROXY_COUNTS = {
    "aerial": 6,
    "detail": 8,
    "drawing": 10,
    "exterior": 10,
    "interior": 10,
    "out_of_scope": 6,
}
EXPECTED_GENERATION_COUNTS = {"legacy": 15, "modern": 35}
EXPECTED_ROLE_COUNTS = {"cover": 22, "gallery": 28}
EXPECTED_PREFIX_COUNTS = {
    "n10": {
        "proxy": {
            "aerial": 1,
            "detail": 2,
            "drawing": 2,
            "exterior": 2,
            "interior": 2,
            "out_of_scope": 1,
        },
        "generation": {"legacy": 3, "modern": 7},
        "role": {"cover": 4, "gallery": 6},
    },
    "n20": {
        "proxy": {
            "aerial": 3,
            "detail": 3,
            "drawing": 4,
            "exterior": 4,
            "interior": 4,
            "out_of_scope": 2,
        },
        "generation": {"legacy": 6, "modern": 14},
        "role": {"cover": 9, "gallery": 11},
    },
}

PUBLIC_REVIEW_FIELDS = frozenset({"review_rank", "review_id"})
AUDIT_SAMPLE_FIELDS = frozenset(
    {
        "sample_rank",
        "review_id",
        "subset_membership",
        "selection_audit",
        "source_identity",
        "image_evidence",
    }
)
SELECTION_AUDIT_FIELDS = frozenset(
    {
        "proxy_class",
        "generation_group",
        "role",
        "cell_key",
        "source_cell_rank",
        "eligible_cell_count",
        "selection_basis",
    }
)
SOURCE_IDENTITY_FIELDS = (
    "candidate_id",
    "candidate_rank",
    "asset_key",
    "article_id",
    "building_id",
    "request_url",
    "review_url",
    "url_generation",
)
IMAGE_EVIDENCE_FIELDS = (
    "probe_status",
    "probe_final_url",
    "http_status",
    "response_mime",
    "response_bytes",
    "content_sha256",
    "original_format",
    "original_mode",
    "original_width",
    "original_height",
    "frame_count",
    "exif_orientation",
    "orientation_applied",
    "oriented_width",
    "oriented_height",
    "alpha_composited",
    "icc_profile_present",
    "color_normalization",
    "normalized_width",
    "normalized_height",
    "pixel_sha256",
    "phash_256",
    "exact_duplicate_group",
    "is_exact_pixel_duplicate",
    "duplicate_of",
    "auto_exclude_exact_duplicate",
    "phash_le8_matches",
    "has_phash_le8_candidate",
    "prior_content_sha_matches",
    "prior_pixel_sha_matches",
    "prior_phash_le8_matches",
    "prior_phash_9_16_matches",
    "has_prior_exact_pixel_match",
    "has_prior_phash_le8_match",
    "has_prior_phash_9_16_match",
    "probe_attempt_count",
    "probe_elapsed_ms",
    "probe_completed_at",
    "probe_error_kind",
    "probe_error_message",
)


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _cell_key(cell: Cell) -> str:
    return "|".join(cell)


def _selected_id_set_sha256(candidate_ids: Sequence[str]) -> str:
    return hashlib.sha256(
        "".join(value + "\n" for value in sorted(candidate_ids)).encode("ascii")
    ).hexdigest()


def _selected_order_sha256(candidate_ids: Sequence[str]) -> str:
    return hashlib.sha256(
        "".join(value + "\n" for value in candidate_ids).encode("ascii")
    ).hexdigest()


def opaque_review_id(candidate: Mapping[str, Any]) -> str:
    value = "%s|%s|%s" % (
        BLIND_ID_VERSION,
        candidate["candidate_id"],
        candidate["pixel_sha256"],
    )
    return "axis-" + hashlib.sha256(value.encode("ascii")).hexdigest()[:12]


def _review_order_key(review_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(
        (REVIEW_ORDER_VERSION + "|" + review_id).encode("ascii")
    ).hexdigest()
    return digest, review_id


def _subset_membership(rank: int) -> list[str]:
    if rank <= 10:
        return ["n10", "n20", "n50"]
    if rank <= 20:
        return ["n20", "n50"]
    return ["n50"]


def _near_duplicate_ids(payload: Mapping[str, Any]) -> set[str]:
    rejected: set[str] = set()
    for group in payload.get("exact_pixel_duplicate_groups", []):
        rejected.update(str(value) for value in group["member_candidate_ids"])
    for pair in payload.get("phash_duplicate_pairs_le_8", []):
        rejected.add(str(pair["candidate_id_a"]))
        rejected.add(str(pair["candidate_id_b"]))
    for field in (
        "prior_content_sha_matches",
        "prior_pixel_sha_matches",
        "prior_phash_pairs_le_8",
    ):
        for pair in payload.get(field, []):
            rejected.add(str(pair["holdout_candidate_id"]))
    return rejected


def _exclusion_reason(
    candidate: Mapping[str, Any], *, near_duplicate_ids: set[str]
) -> str | None:
    if candidate.get("probe_status") != "success":
        return "probe_failed"
    candidate_id = str(candidate["candidate_id"])
    if candidate_id in near_duplicate_ids:
        return "duplicate_or_near_duplicate_phash_le16"
    forbidden = (
        candidate.get("is_exact_pixel_duplicate"),
        candidate.get("auto_exclude_exact_duplicate"),
        candidate.get("has_phash_le8_candidate"),
        candidate.get("has_prior_exact_pixel_match"),
        candidate.get("has_prior_phash_le8_match"),
    )
    if any(value is not False for value in forbidden):
        return "duplicate_evidence_inconsistent"
    for field in (
        "phash_le8_matches",
        "prior_content_sha_matches",
        "prior_pixel_sha_matches",
        "prior_phash_le8_matches",
    ):
        if candidate.get(field) != []:
            return "duplicate_evidence_inconsistent"
    return None


def _select_candidates(
    probed: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[Cell, int]]:
    near_duplicate_ids = _near_duplicate_ids(probed)
    queues: dict[Cell, list[dict[str, Any]]] = defaultdict(list)
    exclusion_counts: Counter[str] = Counter()
    for source in probed["candidates"]:
        candidate = dict(source)
        reason = _exclusion_reason(candidate, near_duplicate_ids=near_duplicate_ids)
        if reason is not None:
            exclusion_counts[reason] += 1
            continue
        cell = (
            str(candidate["proxy_class"]),
            str(candidate["generation_group"]),
            str(candidate["role"]),
        )
        queues[cell].append(candidate)
    for rows in queues.values():
        rows.sort(
            key=lambda row: (
                int(row["cell_rank"]),
                str(row["stable_order"]),
                str(row["candidate_id"]),
            )
        )

    available = {cell: len(rows) for cell, rows in queues.items()}
    shortfalls = {
        _cell_key(cell): {"required": quota, "available": available.get(cell, 0)}
        for cell, quota in CELL_QUOTAS.items()
        if available.get(cell, 0) < quota
    }
    if shortfalls:
        raise ValueError("fresh holdout cell quota shortfall: %r" % shortfalls)

    offsets: Counter[Cell] = Counter()
    selected: list[dict[str, Any]] = []
    for cell in SAMPLE_CELL_SEQUENCE:
        selected.append(queues[cell][offsets[cell]])
        offsets[cell] += 1
    return selected, dict(sorted(exclusion_counts.items())), available


def _counts(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row[field]) for row in rows).items()))


def _prefix_metrics(rows: Sequence[Mapping[str, Any]], limit: int) -> dict[str, Any]:
    prefix = rows[:limit]
    return {
        "proxy": _counts(prefix, "proxy_class"),
        "generation": _counts(prefix, "generation_group"),
        "role": _counts(prefix, "role"),
    }


def _selection_metrics(
    selected: Sequence[Mapping[str, Any]],
    *,
    exclusion_counts: Mapping[str, int],
    eligible_count: int,
) -> dict[str, Any]:
    candidate_ids = [str(row["candidate_id"]) for row in selected]
    return {
        "parent_candidate_count": 100,
        "parent_success_count": 96,
        "parent_failure_count": 4,
        "eligible_count": eligible_count,
        "selected_count": len(selected),
        "unselected_eligible_count": eligible_count - len(selected),
        "exclusion_counts": dict(exclusion_counts),
        "proxy_counts": _counts(selected, "proxy_class"),
        "generation_counts": _counts(selected, "generation_group"),
        "role_counts": _counts(selected, "role"),
        "cell_counts": dict(
            sorted(
                (_cell_key(cell), count)
                for cell, count in Counter(
                    (
                        str(row["proxy_class"]),
                        str(row["generation_group"]),
                        str(row["role"]),
                    )
                    for row in selected
                ).items()
            )
        ),
        "prefix_counts": {
            "n10": _prefix_metrics(selected, 10),
            "n20": _prefix_metrics(selected, 20),
        },
        "unique_asset_count": len({str(row["asset_key"]) for row in selected}),
        "unique_article_count": len({int(row["article_id"]) for row in selected}),
        "unique_building_count": len({str(row["building_id"]) for row in selected}),
        "unique_content_sha256_count": len(
            {str(row["content_sha256"]) for row in selected}
        ),
        "unique_pixel_sha256_count": len(
            {str(row["pixel_sha256"]) for row in selected}
        ),
        "selected_id_set_sha256": _selected_id_set_sha256(candidate_ids),
        "selected_order_sha256": _selected_order_sha256(candidate_ids),
    }


def _logical_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(payload))
    value.pop("manifest_sha256", None)
    value.pop("logical_sha256", None)
    provenance = value.get("provenance")
    if isinstance(provenance, dict):
        for item in provenance.values():
            if isinstance(item, dict):
                item.pop("filename", None)
                item.pop("file_sha256", None)
    return value


def selected_audit_sha256(samples: Sequence[Mapping[str, Any]]) -> str:
    value = [
        {
            "review_id": row["review_id"],
            "selection_audit": row["selection_audit"],
            "source_identity": row["source_identity"],
            "image_evidence": row["image_evidence"],
        }
        for row in samples
    ]
    return _sha256_value(value)


def logical_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_logical_payload(payload))


def manifest_sha256(payload: Mapping[str, Any]) -> str:
    value = dict(payload)
    value.pop("manifest_sha256", None)
    return _sha256_value(value)


def _source_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(candidate[field]) for field in SOURCE_IDENTITY_FIELDS}


def _image_evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(candidate[field]) for field in IMAGE_EVIDENCE_FIELDS}


def build_selection_payload(
    probed: Mapping[str, Any],
    *,
    probed_file_sha256: str,
    candidate_file_sha256: str,
    prior_file_sha256: str,
) -> dict[str, Any]:
    selected, exclusion_counts, available = _select_candidates(probed)
    eligible_count = len(probed["candidates"]) - sum(exclusion_counts.values())
    metrics = _selection_metrics(
        selected, exclusion_counts=exclusion_counts, eligible_count=eligible_count
    )

    audit_samples: list[dict[str, Any]] = []
    for rank, candidate in enumerate(selected, 1):
        cell = (
            str(candidate["proxy_class"]),
            str(candidate["generation_group"]),
            str(candidate["role"]),
        )
        review_id = opaque_review_id(candidate)
        audit_samples.append(
            {
                "sample_rank": rank,
                "review_id": review_id,
                "subset_membership": _subset_membership(rank),
                "selection_audit": {
                    "proxy_class": cell[0],
                    "generation_group": cell[1],
                    "role": cell[2],
                    "cell_key": _cell_key(cell),
                    "source_cell_rank": int(candidate["cell_rank"]),
                    "eligible_cell_count": available[cell],
                    "selection_basis": "frozen_proxy_balance_without_axis_labels",
                },
                "source_identity": _source_identity(candidate),
                "image_evidence": _image_evidence(candidate),
            }
        )

    review_ids = [str(row["review_id"]) for row in audit_samples]
    if len(set(review_ids)) != 50:
        raise ValueError("fresh holdout opaque review ID collision")
    ordered_review_ids = sorted(review_ids, key=_review_order_key)
    review_rows = [
        {"review_rank": rank, "review_id": review_id}
        for rank, review_id in enumerate(ordered_review_ids, 1)
    ]

    probe_contract = probed["probe_contract"]
    cross_contract = probed["holdout_cross_duplicate_contract"]
    prompt_freeze = {
        "axis_contract_version": axes.AXIS_CONTRACT_VERSION,
        "axis_prompt_version": axes.AXIS_PROMPT_VERSION,
        "codebook_sha256": axes_review.axes_review_codebook_sha256(),
        "axis_output_schema_sha256": axes_review.axis_output_schema_sha256(),
        "policy": "any later prompt or schema change invalidates this holdout",
    }
    payload: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "development_only": False,
        "independent_holdout": True,
        "selector_version": SELECTOR_VERSION,
        "provenance": {
            "source_db_sha256": EXPECTED_SOURCE_DB_SHA256,
            "parent_probed_n100": {
                "filename": "divisare_vision_axes_holdout_candidates_n100_v1_probed.json",
                "file_sha256": probed_file_sha256,
                "manifest_sha256": probed["manifest_sha256"],
                "base_probe_logical_sha256": probe_contract["logical_sha256"],
                "cross_logical_sha256": cross_contract["logical_sha256"],
            },
            "parent_candidate_n100": {
                "filename": "divisare_vision_axes_holdout_candidates_n100_v1.json",
                "file_sha256": candidate_file_sha256,
                "manifest_sha256": probe_contract["input_manifest_sha256"],
            },
            "prior_probed_n560": {
                "filename": "divisare_vision_gold_candidates_v1_2_probed.json",
                "file_sha256": prior_file_sha256,
                "manifest_sha256": cross_contract["prior_manifest_sha256"],
            },
            "prompt_freeze": prompt_freeze,
        },
        "selection_contract": {
            "purpose": "fresh_blind_one_shot_final_prompt_holdout",
            "selector_version": SELECTOR_VERSION,
            "blind_id_version": BLIND_ID_VERSION,
            "review_order_version": REVIEW_ORDER_VERSION,
            "sample_count": 50,
            "nested_prefixes": {"n10": 10, "n20": 20, "n50": 50},
            "sample_cell_sequence": [_cell_key(cell) for cell in SAMPLE_CELL_SEQUENCE],
            "cell_quotas": dict(
                sorted((_cell_key(cell), count) for cell, count in CELL_QUOTAS.items())
            ),
            "eligibility": {
                "probe_status": "success",
                "identity_uniqueness": "asset_article_building",
                "within_pool_exact_or_phash_max_distance": "reject_lte_8",
                "prior_pool_exact_or_phash_max_distance": "reject_lte_8",
                "phash_distance_9_to_16": "audit_only",
                "images_persisted": False,
            },
            "quota_adjustments": [],
            "reviewer_payload": "review_rows_only",
            "audit_payload": "audit_samples_never_reviewer_or_model_facing",
            "selected_id_set_sha256": metrics["selected_id_set_sha256"],
            "selected_order_sha256": metrics["selected_order_sha256"],
        },
        "selection_metrics": metrics,
        "review_rows": review_rows,
        "audit_samples": audit_samples,
    }
    payload["logical_sha256"] = logical_sha256(payload)
    payload["manifest_sha256"] = manifest_sha256(payload)
    validate_selection_manifest(payload, parent_probed=probed)
    return payload


def _assert_equal(actual: Any, expected: Any, name: str) -> None:
    if actual != expected:
        raise ValueError("%s mismatch" % name)


def validate_selection_manifest(
    payload: Mapping[str, Any], *, parent_probed: Mapping[str, Any] | None = None
) -> None:
    expected_top = {
        "manifest_version",
        "development_only",
        "independent_holdout",
        "selector_version",
        "logical_sha256",
        "manifest_sha256",
        "provenance",
        "selection_contract",
        "selection_metrics",
        "review_rows",
        "audit_samples",
    }
    if set(payload) != expected_top:
        raise ValueError("fresh holdout selection fields changed")
    _assert_equal(payload.get("manifest_version"), MANIFEST_VERSION, "manifest version")
    _assert_equal(payload.get("development_only"), False, "development_only")
    _assert_equal(payload.get("independent_holdout"), True, "independent_holdout")
    _assert_equal(payload.get("selector_version"), SELECTOR_VERSION, "selector version")
    _assert_equal(payload.get("logical_sha256"), logical_sha256(payload), "logical SHA")
    _assert_equal(payload.get("manifest_sha256"), manifest_sha256(payload), "manifest SHA")

    provenance = payload.get("provenance")
    contract = payload.get("selection_contract")
    metrics = payload.get("selection_metrics")
    if not all(isinstance(value, Mapping) for value in (provenance, contract, metrics)):
        raise ValueError("fresh holdout provenance, contract, and metrics are required")
    _assert_equal(provenance.get("source_db_sha256"), EXPECTED_SOURCE_DB_SHA256, "source DB SHA")
    parent_probed_provenance = provenance.get("parent_probed_n100")
    parent_candidate = provenance.get("parent_candidate_n100")
    prior = provenance.get("prior_probed_n560")
    if not all(
        isinstance(value, Mapping)
        for value in (parent_probed_provenance, parent_candidate, prior)
    ):
        raise ValueError("fresh holdout parent provenance is required")
    expected_parent_values = {
        "parent probed filename": (parent_probed_provenance.get("filename"), "divisare_vision_axes_holdout_candidates_n100_v1_probed.json"),
        "parent probed file SHA": (parent_probed_provenance.get("file_sha256"), EXPECTED_PROBED_FILE_SHA256),
        "parent probed manifest SHA": (parent_probed_provenance.get("manifest_sha256"), EXPECTED_PROBED_MANIFEST_SHA256),
        "base probe logical SHA": (parent_probed_provenance.get("base_probe_logical_sha256"), EXPECTED_BASE_PROBE_LOGICAL_SHA256),
        "cross logical SHA": (parent_probed_provenance.get("cross_logical_sha256"), EXPECTED_CROSS_LOGICAL_SHA256),
        "parent candidate filename": (parent_candidate.get("filename"), "divisare_vision_axes_holdout_candidates_n100_v1.json"),
        "parent candidate file SHA": (parent_candidate.get("file_sha256"), EXPECTED_CANDIDATE_FILE_SHA256),
        "parent candidate manifest SHA": (parent_candidate.get("manifest_sha256"), EXPECTED_CANDIDATE_MANIFEST_SHA256),
        "prior filename": (prior.get("filename"), "divisare_vision_gold_candidates_v1_2_probed.json"),
        "prior file SHA": (prior.get("file_sha256"), EXPECTED_PRIOR_FILE_SHA256),
        "prior manifest SHA": (prior.get("manifest_sha256"), EXPECTED_PRIOR_MANIFEST_SHA256),
    }
    for name, (actual, expected) in expected_parent_values.items():
        _assert_equal(actual, expected, name)
    prompt = provenance.get("prompt_freeze")
    if not isinstance(prompt, Mapping):
        raise ValueError("prompt freeze is required")
    _assert_equal(prompt.get("axis_contract_version"), axes.AXIS_CONTRACT_VERSION, "axis contract")
    _assert_equal(prompt.get("axis_prompt_version"), axes.AXIS_PROMPT_VERSION, "axis prompt")
    _assert_equal(prompt.get("codebook_sha256"), axes_review.axes_review_codebook_sha256(), "codebook SHA")
    _assert_equal(prompt.get("axis_output_schema_sha256"), axes_review.axis_output_schema_sha256(), "output schema SHA")
    _assert_equal(contract.get("sample_cell_sequence"), [_cell_key(cell) for cell in SAMPLE_CELL_SEQUENCE], "cell sequence")
    _assert_equal(contract.get("cell_quotas"), dict(sorted((_cell_key(cell), count) for cell, count in CELL_QUOTAS.items())), "cell quotas")
    expected_contract_values = {
        "purpose": "fresh_blind_one_shot_final_prompt_holdout",
        "selector_version": SELECTOR_VERSION,
        "blind_id_version": BLIND_ID_VERSION,
        "review_order_version": REVIEW_ORDER_VERSION,
        "sample_count": 50,
        "nested_prefixes": {"n10": 10, "n20": 20, "n50": 50},
        "eligibility": {
            "probe_status": "success",
            "identity_uniqueness": "asset_article_building",
            "within_pool_exact_or_phash_max_distance": "reject_lte_8",
            "prior_pool_exact_or_phash_max_distance": "reject_lte_8",
            "phash_distance_9_to_16": "audit_only",
            "images_persisted": False,
        },
        "quota_adjustments": [],
        "reviewer_payload": "review_rows_only",
        "audit_payload": "audit_samples_never_reviewer_or_model_facing",
    }
    for field, expected in expected_contract_values.items():
        _assert_equal(contract.get(field), expected, "selection contract %s" % field)

    samples = payload.get("audit_samples")
    review_rows = payload.get("review_rows")
    if not isinstance(samples, list) or len(samples) != 50:
        raise ValueError("fresh holdout audit_samples must contain 50 rows")
    if not isinstance(review_rows, list) or len(review_rows) != 50:
        raise ValueError("fresh holdout review_rows must contain 50 rows")

    parent_by_id: dict[str, Mapping[str, Any]] = {}
    expected_selected: list[dict[str, Any]] | None = None
    parent_available: dict[Cell, int] = {}
    exclusion_counts: dict[str, int] = {"probe_failed": 4}
    eligible_count = 96
    if parent_probed is not None:
        expected_selected, exclusion_counts, parent_available = _select_candidates(parent_probed)
        eligible_count = len(parent_probed["candidates"]) - sum(exclusion_counts.values())
        parent_by_id = {
            str(row["candidate_id"]): row for row in parent_probed["candidates"]
        }

    selected_rows: list[dict[str, Any]] = []
    review_ids: list[str] = []
    for rank, sample in enumerate(samples, 1):
        if not isinstance(sample, Mapping) or set(sample) != AUDIT_SAMPLE_FIELDS:
            raise ValueError("fresh holdout audit sample fields changed")
        _assert_equal(sample.get("sample_rank"), rank, "sample rank")
        source = sample.get("source_identity")
        evidence = sample.get("image_evidence")
        audit = sample.get("selection_audit")
        if not all(isinstance(value, Mapping) for value in (source, evidence, audit)):
            raise ValueError("fresh holdout audit sample nesting changed")
        if set(source) != set(SOURCE_IDENTITY_FIELDS):
            raise ValueError("fresh holdout source identity fields changed")
        if set(evidence) != set(IMAGE_EVIDENCE_FIELDS):
            raise ValueError("fresh holdout image evidence fields changed")
        if set(audit) != SELECTION_AUDIT_FIELDS:
            raise ValueError("fresh holdout selection audit fields changed")
        candidate_id = str(source["candidate_id"])
        merged = {
            **source,
            **evidence,
            "proxy_class": audit["proxy_class"],
            "generation_group": audit["generation_group"],
            "role": audit["role"],
        }
        if expected_selected is not None:
            expected = expected_selected[rank - 1]
            _assert_equal(candidate_id, expected["candidate_id"], "selected candidate order")
            _assert_equal(dict(source), _source_identity(expected), "source identity")
            _assert_equal(dict(evidence), _image_evidence(expected), "image evidence")
            _assert_equal(parent_by_id[candidate_id]["pixel_sha256"], evidence["pixel_sha256"], "parent pixel SHA")
        expected_review_id = opaque_review_id(merged)
        _assert_equal(sample.get("review_id"), expected_review_id, "opaque review ID")
        _assert_equal(sample.get("subset_membership"), _subset_membership(rank), "subset membership")
        cell = SAMPLE_CELL_SEQUENCE[rank - 1]
        _assert_equal(
            (audit.get("proxy_class"), audit.get("generation_group"), audit.get("role")),
            cell,
            "sample cell",
        )
        _assert_equal(audit.get("cell_key"), _cell_key(cell), "sample cell key")
        _assert_equal(
            audit.get("selection_basis"),
            "frozen_proxy_balance_without_axis_labels",
            "selection basis",
        )
        source_cell_rank = audit.get("source_cell_rank")
        eligible_cell_count = audit.get("eligible_cell_count")
        if (
            isinstance(source_cell_rank, bool)
            or not isinstance(source_cell_rank, int)
            or source_cell_rank < 1
            or isinstance(eligible_cell_count, bool)
            or not isinstance(eligible_cell_count, int)
            or eligible_cell_count < CELL_QUOTAS[cell]
        ):
            raise ValueError("selection audit ranks are invalid")
        if expected_selected is not None:
            _assert_equal(source_cell_rank, int(expected["cell_rank"]), "source cell rank")
            _assert_equal(eligible_cell_count, parent_available[cell], "eligible cell count")
        if _exclusion_reason(merged, near_duplicate_ids=set()) is not None:
            raise ValueError("selected sample contains duplicate or failed evidence")
        for url_field in ("request_url", "review_url"):
            parsed = urlsplit(str(source[url_field]))
            if parsed.scheme != "https" or parsed.hostname != "images.divisare.com":
                raise ValueError("selected source URL is not Divisare HTTPS")
        selected_rows.append(merged)
        review_ids.append(expected_review_id)

    for field in ("asset_key", "article_id", "building_id", "content_sha256", "pixel_sha256"):
        if len({row[field] for row in selected_rows}) != 50:
            raise ValueError("selected holdout does not have 50 unique %s values" % field)
    for index, left in enumerate(selected_rows):
        for right in selected_rows[index + 1 :]:
            if base_probe.phash_distance(left["phash_256"], right["phash_256"]) <= 8:
                raise ValueError("selected holdout contains a pHash <=8 pair")

    expected_public = [
        {"review_rank": rank, "review_id": review_id}
        for rank, review_id in enumerate(sorted(review_ids, key=_review_order_key), 1)
    ]
    for row in review_rows:
        if not isinstance(row, Mapping) or set(row) != PUBLIC_REVIEW_FIELDS:
            raise ValueError("reviewer-facing row leaked fields")
    _assert_equal(review_rows, expected_public, "blinded review order")

    expected_metrics = _selection_metrics(
        selected_rows,
        exclusion_counts=exclusion_counts,
        eligible_count=eligible_count,
    )
    _assert_equal(dict(metrics), expected_metrics, "selection metrics")
    _assert_equal(metrics.get("proxy_counts"), EXPECTED_PROXY_COUNTS, "proxy counts")
    _assert_equal(metrics.get("generation_counts"), EXPECTED_GENERATION_COUNTS, "generation counts")
    _assert_equal(metrics.get("role_counts"), EXPECTED_ROLE_COUNTS, "role counts")
    for name, expected in EXPECTED_PREFIX_COUNTS.items():
        _assert_equal(metrics["prefix_counts"][name], expected, "%s prefix counts" % name)
    _assert_equal(contract.get("selected_id_set_sha256"), metrics["selected_id_set_sha256"], "selected set SHA")
    _assert_equal(contract.get("selected_order_sha256"), metrics["selected_order_sha256"], "selected order SHA")
    _assert_equal(metrics.get("selected_id_set_sha256"), EXPECTED_SELECTED_ID_SET_SHA256, "frozen selected set SHA")
    _assert_equal(metrics.get("selected_order_sha256"), EXPECTED_SELECTED_ORDER_SHA256, "frozen selected order SHA")
    _assert_equal(selected_audit_sha256(samples), EXPECTED_SELECTED_AUDIT_SHA256, "frozen selected audit SHA")


def load_selection_inputs(
    *,
    probed_path: Path,
    candidate_path: Path,
    prior_path: Path,
) -> tuple[dict[str, Any], str, str, str]:
    probed_path = probed_path.resolve()
    candidate_path = candidate_path.resolve()
    prior_path = prior_path.resolve()
    candidate, _rows, candidate_file_sha, prior, _prior_rows, prior_file_sha, exclusion = (
        holdout_probe.load_probe_inputs(candidate_path, prior_path)
    )
    raw = probed_path.read_bytes()
    probed = parse_json_strict(raw, label="fresh holdout probed manifest")
    probed_file_sha = hashlib.sha256(raw).hexdigest()
    holdout_probe.validate_probed_holdout_manifest(
        probed,
        input_manifest=candidate,
        input_manifest_file_sha256=candidate_file_sha,
        prior_manifest=prior,
        prior_manifest_file_sha256=prior_file_sha,
        exclusion=exclusion,
    )
    expected = {
        "candidate file SHA": (candidate_file_sha, EXPECTED_CANDIDATE_FILE_SHA256),
        "candidate manifest SHA": (candidate["manifest_sha256"], EXPECTED_CANDIDATE_MANIFEST_SHA256),
        "prior file SHA": (prior_file_sha, EXPECTED_PRIOR_FILE_SHA256),
        "prior manifest SHA": (prior["manifest_sha256"], EXPECTED_PRIOR_MANIFEST_SHA256),
        "probed file SHA": (probed_file_sha, EXPECTED_PROBED_FILE_SHA256),
        "probed manifest SHA": (probed["manifest_sha256"], EXPECTED_PROBED_MANIFEST_SHA256),
        "base probe logical SHA": (probed["probe_contract"]["logical_sha256"], EXPECTED_BASE_PROBE_LOGICAL_SHA256),
        "cross logical SHA": (probed["holdout_cross_duplicate_contract"]["logical_sha256"], EXPECTED_CROSS_LOGICAL_SHA256),
        "source DB SHA": (probed["source_db_sha256"], EXPECTED_SOURCE_DB_SHA256),
    }
    for name, (actual, wanted) in expected.items():
        _assert_equal(actual, wanted, name)
    return probed, probed_file_sha, candidate_file_sha, prior_file_sha


def write_selection_manifest(
    *,
    probed_path: Path,
    candidate_path: Path,
    prior_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    output_path = output_path.resolve()
    if output_path.exists():
        raise FileExistsError("immutable fresh holdout N50 already exists: %s" % output_path)
    probed, probed_file_sha, candidate_file_sha, prior_file_sha = load_selection_inputs(
        probed_path=probed_path,
        candidate_path=candidate_path,
        prior_path=prior_path,
    )
    payload = build_selection_payload(
        probed,
        probed_file_sha256=probed_file_sha,
        candidate_file_sha256=candidate_file_sha,
        prior_file_sha256=prior_file_sha,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".partial", dir=output_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        input_hashes = (
            (probed_path, probed_file_sha),
            (candidate_path, candidate_file_sha),
            (prior_path, prior_file_sha),
        )
        for path, expected in input_hashes:
            if file_sha256(path) != expected:
                raise RuntimeError("fresh holdout selection input changed: %s" % path)
        try:
            os.link(temporary, output_path)
        except FileExistsError as exc:
            raise FileExistsError(
                "immutable fresh holdout N50 already exists: %s" % output_path
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return payload
