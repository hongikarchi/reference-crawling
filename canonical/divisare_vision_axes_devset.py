"""Frozen, development-only Divisare image-axis candidate set.

This module deliberately separates the two views of the artifact:

* ``review_rows`` contains opaque identifiers only and is safe to show to a
  reviewer or model.
* ``audit_samples`` retains the source identity, URLs, frozen hashes, and the
  reason the sample was selected.  It must not be included in a review prompt.

The set is development data.  It reuses examples inspected by the earlier
five-class benchmark and is not an independent holdout.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from canonical import divisare_vision_gold_finalize as gold_contract
from canonical import divisare_vision_n100 as n100_contract


MANIFEST_VERSION = "divisare-vision-axes-dev-candidates-v1.0.0"
SELECTOR_VERSION = "divisare-vision-axes-dev-selector-v1.0.0"
BLIND_ID_VERSION = "axis-review-v1"
REVIEW_ORDER_VERSION = "axis-review-order-v1"

EXPECTED_SOURCE_DB_SHA256 = (
    "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f"
)
EXPECTED_PARENT_CANDIDATE_MANIFEST_SHA256 = (
    "480f28d33f90210a479a19e4ab858a71d99ebc7336f8ee0e638be8d96e46f626"
)
EXPECTED_PARENT_CANDIDATE_FILE_SHA256 = (
    "faecad6bd355f38d4553657a0ecb45ff77ba8ebee8f950889e3968f068fc39ad"
)
EXPECTED_REVIEWED_POOL_SHA256 = (
    "ec84ac1962e7a84e468db503c6ec45bd992f4ced6d6a2dfed832855f8760c049"
)
EXPECTED_REVIEWED_POOL_FILE_SHA256 = (
    "d99a3ba4f77ab4250e3acbd930f9f4d14852749f92040cb314eae84a79a96c07"
)
EXPECTED_OLD_GOLD_MANIFEST_SHA256 = (
    "3d2273e20839247db666f2bdc31b8ba6f99dc1be276cec6a6dee42e9ca26492d"
)
EXPECTED_OLD_GOLD_FILE_SHA256 = (
    "d82b775f62cbc9e39f49ce878b44a39948da732cef26901c96f03f3a27ce7462"
)
EXPECTED_OLD_GOLD_LOGICAL_SHA256 = (
    "0bc48387334db6cb6c5afbc5f094793509dde90d4100879dba067b15397b74bb"
)
EXPECTED_OLD_N100_DB_FILE_SHA256 = (
    "3dd65bc7286e70e4c5bfc6adfee2ab0d386e987834ccd9c39d353501c23b426b"
)
EXPECTED_OLD_N100_LOGICAL_SHA256 = (
    "5cca9b4f9cab4de281dd4f5035a65d84faae11c000390d579454d0b3e8c7e3cd"
)

N10_IDS = (
    "candidate-0065",
    "candidate-0081",
    "candidate-0087",
    "candidate-0002",
    "candidate-0014",
    "candidate-0009",
    "candidate-0182",
    "candidate-0449",
    "candidate-0325",
    "candidate-0093",
)

N20_ADDITIONAL_IDS = (
    "candidate-0098",
    "candidate-0064",
    "candidate-0229",
    "candidate-0034",
    "candidate-0094",
    "candidate-0108",
    "candidate-0067",
    "candidate-0165",
    "candidate-0454",
    "candidate-0013",
)

STRATUM_IDS: dict[str, tuple[str, ...]] = {
    "prior_1024_error": (
        "candidate-0002",
        "candidate-0003",
        "candidate-0014",
        "candidate-0271",
        "candidate-0098",
        "candidate-0110",
        "candidate-0127",
        "candidate-0160",
    ),
    "axis_boundary": (
        "candidate-0009",
        "candidate-0064",
        "candidate-0229",
        "candidate-0007",
        "candidate-0072",
        "candidate-0012",
        "candidate-0085",
        "candidate-0059",
        "candidate-0218",
        "candidate-0501",
    ),
    "clear_control": (
        "candidate-0065",
        "candidate-0081",
        "candidate-0087",
        "candidate-0034",
        "candidate-0094",
        "candidate-0108",
        "candidate-0067",
        "candidate-0005",
        "candidate-0092",
        "candidate-0075",
        "candidate-0046",
        "candidate-0331",
    ),
    "medium_or_state": (
        "candidate-0182",
        "candidate-0449",
        "candidate-0325",
        "candidate-0165",
        "candidate-0454",
        "candidate-0463",
        "candidate-0448",
        "candidate-0492",
        "candidate-0495",
        "candidate-0552",
    ),
    "out_of_scope": (
        "candidate-0093",
        "candidate-0013",
        "candidate-0044",
        "candidate-0157",
        "candidate-0168",
        "candidate-0200",
        "candidate-0306",
        "candidate-0363",
        "candidate-0407",
        "candidate-0548",
    ),
}

# These are historical review dispositions, used only to prove that the
# medium/state and out-of-scope strata came from the intended frozen review.
# They are never copied into reviewer-facing rows or used as new-axis labels.
EXPECTED_PRIOR_DISPOSITIONS = {
    **{candidate_id: "include" for candidate_id in STRATUM_IDS["prior_1024_error"]},
    **{candidate_id: "include" for candidate_id in STRATUM_IDS["axis_boundary"]},
    **{candidate_id: "include" for candidate_id in STRATUM_IDS["clear_control"]},
    **{
        candidate_id: ("include" if candidate_id == "candidate-0492" else "exclude")
        for candidate_id in STRATUM_IDS["medium_or_state"]
    },
    **{candidate_id: "exclude" for candidate_id in STRATUM_IDS["out_of_scope"]},
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
SOURCE_IDENTITY_FIELDS = frozenset(
    {
        "candidate_id",
        "candidate_rank",
        "asset_key",
        "article_id",
        "building_id",
        "generation_group",
        "url_generation",
        "request_url",
        "review_url",
    }
)
IMAGE_EVIDENCE_FIELDS = frozenset(
    {
        "probe_status",
        "probe_final_url",
        "probe_completed_at",
        "probe_elapsed_ms",
        "http_status",
        "response_mime",
        "response_bytes",
        "content_sha256",
        "pixel_sha256",
        "phash_256",
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
        "is_exact_pixel_duplicate",
        "exact_duplicate_group",
        "duplicate_of",
        "auto_exclude_exact_duplicate",
        "has_phash_le8_candidate",
        "phash_le8_matches",
    }
)
SELECTION_AUDIT_FIELDS = frozenset(
    {"stratum", "stratum_rank", "selection_basis"}
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVIEW_ID_RE = re.compile(r"^axis-[0-9a-f]{12}$")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(gold_contract.canonical_json_bytes(value))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _selected_id_set_sha256(candidate_ids: Sequence[str]) -> str:
    raw = "".join(candidate_id + "\n" for candidate_id in sorted(candidate_ids))
    return _sha256_bytes(raw.encode("ascii"))


def _remaining_order_key(candidate_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(
        (SELECTOR_VERSION + "|remaining|" + candidate_id).encode("ascii")
    ).hexdigest()
    return digest, candidate_id


ALL_SELECTED_IDS = frozenset(
    candidate_id for values in STRATUM_IDS.values() for candidate_id in values
)
N20_IDS = N10_IDS + N20_ADDITIONAL_IDS
ORDERED_N50_IDS = N20_IDS + tuple(
    sorted(ALL_SELECTED_IDS - set(N20_IDS), key=_remaining_order_key)
)
EXPECTED_SELECTED_ID_SET_SHA256 = (
    "559b965de336499a62dadb1f65a2fae040f8bb4cc9ae8b3a5ab1717e26940dd7"
)

if len(ALL_SELECTED_IDS) != 50 or len(ORDERED_N50_IDS) != 50:
    raise RuntimeError("axes development selection must contain exactly 50 unique IDs")
if len(set(N10_IDS)) != 10 or len(set(N20_IDS)) != 20:
    raise RuntimeError("axes N10/N20 prefixes must be unique")
if not set(N20_IDS).issubset(ALL_SELECTED_IDS):
    raise RuntimeError("axes N20 prefix contains an ID outside N50")
if _selected_id_set_sha256(ORDERED_N50_IDS) != EXPECTED_SELECTED_ID_SET_SHA256:
    raise RuntimeError("axes development selected-ID contract changed")

STRATUM_BY_ID = {
    candidate_id: stratum
    for stratum, candidate_ids in STRATUM_IDS.items()
    for candidate_id in candidate_ids
}
STRATUM_RANK_BY_ID = {
    candidate_id: rank
    for candidate_ids in STRATUM_IDS.values()
    for rank, candidate_id in enumerate(candidate_ids, 1)
}


@dataclass(frozen=True)
class N100Audit:
    file_sha256: str
    logical_sha256: str
    status: str
    benchmark_version: str
    gold_manifest_file_sha256: str
    gold_manifest_sha256: str
    gold_logical_sha256: str
    source_db_sha256: str
    long1024_by_candidate: Mapping[str, Mapping[str, Any]]


def opaque_review_id(candidate_id: str) -> str:
    digest = hashlib.sha256(
        (BLIND_ID_VERSION + "|" + candidate_id).encode("ascii")
    ).hexdigest()
    return "axis-" + digest[:12]


def _review_order_key(review_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(
        (REVIEW_ORDER_VERSION + "|" + review_id).encode("ascii")
    ).hexdigest()
    return digest, review_id


def _require_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError("%s must be 64 lowercase hexadecimal characters" % name)
    return value


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError("%s fields mismatch; missing=%r extra=%r" % (name, missing, extra))


def _expect(value: Any, expected: Any, name: str) -> None:
    if value != expected:
        raise ValueError("%s mismatch: expected %r, got %r" % (name, expected, value))


def _input_contract_checks(
    *,
    candidate_manifest: Mapping[str, Any],
    reviewed_pool: Mapping[str, Any],
    old_gold: Mapping[str, Any],
    n100_audit: N100Audit,
    candidate_file_sha256: str,
    reviewed_pool_file_sha256: str,
    old_gold_file_sha256: str,
) -> None:
    expected = (
        (candidate_file_sha256, EXPECTED_PARENT_CANDIDATE_FILE_SHA256, "candidate file SHA"),
        (
            candidate_manifest.get("manifest_sha256"),
            EXPECTED_PARENT_CANDIDATE_MANIFEST_SHA256,
            "candidate manifest SHA",
        ),
        (reviewed_pool_file_sha256, EXPECTED_REVIEWED_POOL_FILE_SHA256, "reviewed pool file SHA"),
        (
            reviewed_pool.get("reviewed_pool_sha256"),
            EXPECTED_REVIEWED_POOL_SHA256,
            "reviewed pool SHA",
        ),
        (old_gold_file_sha256, EXPECTED_OLD_GOLD_FILE_SHA256, "old gold file SHA"),
        (
            old_gold.get("gold_manifest_sha256"),
            EXPECTED_OLD_GOLD_MANIFEST_SHA256,
            "old gold manifest SHA",
        ),
        (
            old_gold.get("logical_sha256"),
            EXPECTED_OLD_GOLD_LOGICAL_SHA256,
            "old gold logical SHA",
        ),
        (n100_audit.file_sha256, EXPECTED_OLD_N100_DB_FILE_SHA256, "old N100 DB file SHA"),
        (
            n100_audit.logical_sha256,
            EXPECTED_OLD_N100_LOGICAL_SHA256,
            "old N100 logical SHA",
        ),
        (
            candidate_manifest.get("source_db_sha256"),
            EXPECTED_SOURCE_DB_SHA256,
            "source DB SHA",
        ),
    )
    for actual, wanted, name in expected:
        _expect(actual, wanted, name)

    _expect(reviewed_pool.get("source_db_sha256"), EXPECTED_SOURCE_DB_SHA256, "review source SHA")
    old_provenance = old_gold.get("provenance")
    if not isinstance(old_provenance, Mapping):
        raise ValueError("old gold provenance must be an object")
    _expect(old_provenance.get("source_db_sha256"), EXPECTED_SOURCE_DB_SHA256, "old gold source SHA")
    _expect(n100_audit.source_db_sha256, EXPECTED_SOURCE_DB_SHA256, "old N100 source SHA")
    _expect(
        reviewed_pool.get("candidate_manifest_sha256"),
        EXPECTED_PARENT_CANDIDATE_MANIFEST_SHA256,
        "review/candidate binding",
    )
    _expect(
        old_provenance.get("candidate_manifest_sha256"),
        EXPECTED_PARENT_CANDIDATE_MANIFEST_SHA256,
        "old gold/candidate binding",
    )
    _expect(
        old_provenance.get("reviewed_pool_sha256"),
        EXPECTED_REVIEWED_POOL_SHA256,
        "old gold/review binding",
    )
    _expect(
        n100_audit.gold_manifest_file_sha256,
        EXPECTED_OLD_GOLD_FILE_SHA256,
        "old N100/gold file binding",
    )
    _expect(
        n100_audit.gold_manifest_sha256,
        EXPECTED_OLD_GOLD_MANIFEST_SHA256,
        "old N100/gold manifest binding",
    )
    _expect(
        n100_audit.gold_logical_sha256,
        EXPECTED_OLD_GOLD_LOGICAL_SHA256,
        "old N100/gold logical binding",
    )
    _expect(n100_audit.status, "failed_quality_gate", "old N100 status")


def _validate_selection_priors(
    decisions: Sequence[Mapping[str, Any]], n100_audit: N100Audit
) -> None:
    decisions_by_id = {str(row.get("candidate_id")): row for row in decisions}
    if len(decisions_by_id) != len(decisions):
        raise ValueError("reviewed pool candidate IDs are not unique")
    for candidate_id, expected in EXPECTED_PRIOR_DISPOSITIONS.items():
        decision = decisions_by_id.get(candidate_id)
        if decision is None:
            raise ValueError("reviewed pool is missing selected candidate: %s" % candidate_id)
        _expect(decision.get("disposition"), expected, "%s prior disposition" % candidate_id)

    rows = n100_audit.long1024_by_candidate
    for candidate_id in STRATUM_IDS["prior_1024_error"]:
        row = rows.get(candidate_id)
        if row is None or row.get("clarity") != "clear" or row.get("primary_correct") != 0:
            raise ValueError("hard-case N100 audit mismatch: %s" % candidate_id)
    for candidate_id in STRATUM_IDS["axis_boundary"]:
        row = rows.get(candidate_id)
        if row is None or row.get("clarity") != "boundary":
            raise ValueError("boundary N100 audit mismatch: %s" % candidate_id)
    for candidate_id in STRATUM_IDS["clear_control"]:
        row = rows.get(candidate_id)
        if row is None or row.get("clarity") != "clear" or row.get("primary_correct") != 1:
            raise ValueError("clear-control N100 audit mismatch: %s" % candidate_id)


def _subset_membership(sample_rank: int) -> list[str]:
    if sample_rank <= 10:
        return ["n10", "n20", "n50"]
    if sample_rank <= 20:
        return ["n20", "n50"]
    return ["n50"]


def _source_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(candidate[field]) for field in SOURCE_IDENTITY_FIELDS}


def _image_evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(candidate[field]) for field in IMAGE_EVIDENCE_FIELDS}


def _selection_metrics(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    generation_counts = Counter(
        str(row["source_identity"]["generation_group"]) for row in samples
    )
    url_generation_counts = Counter(
        str(row["source_identity"]["url_generation"]) for row in samples
    )
    return {
        "sample_count": len(samples),
        "subset_counts": {"n10": 10, "n20": 20, "n50": 50},
        "stratum_counts": {key: len(value) for key, value in STRATUM_IDS.items()},
        "generation_counts": dict(sorted(generation_counts.items())),
        "url_generation_counts": dict(sorted(url_generation_counts.items())),
        "unique_candidate_count": len({row["source_identity"]["candidate_id"] for row in samples}),
        "unique_asset_count": len({row["source_identity"]["asset_key"] for row in samples}),
        "unique_article_count": len({row["source_identity"]["article_id"] for row in samples}),
        "unique_building_count": len({row["source_identity"]["building_id"] for row in samples}),
        "unique_pixel_sha256_count": len({row["image_evidence"]["pixel_sha256"] for row in samples}),
        "probe_success_count": sum(row["image_evidence"]["probe_status"] == "success" for row in samples),
        "exact_duplicate_flag_count": sum(bool(row["image_evidence"]["is_exact_pixel_duplicate"]) for row in samples),
        "phash_le8_flag_count": sum(bool(row["image_evidence"]["has_phash_le8_candidate"]) for row in samples),
        "selected_id_set_sha256": _selected_id_set_sha256(
            [str(row["source_identity"]["candidate_id"]) for row in samples]
        ),
    }


def build_devset_payload(
    *,
    candidate_manifest: Mapping[str, Any],
    reviewed_pool: Mapping[str, Any],
    old_gold: Mapping[str, Any],
    n100_audit: N100Audit,
    candidate_file_sha256: str,
    reviewed_pool_file_sha256: str,
    old_gold_file_sha256: str,
) -> dict[str, Any]:
    """Build the frozen N50 payload from already loaded immutable inputs."""
    candidates = gold_contract.validate_enriched_candidate_manifest(candidate_manifest)
    decisions = gold_contract.validate_reviewed_pool(reviewed_pool, candidate_manifest)
    gold_contract.validate_gold_manifest(old_gold)
    _input_contract_checks(
        candidate_manifest=candidate_manifest,
        reviewed_pool=reviewed_pool,
        old_gold=old_gold,
        n100_audit=n100_audit,
        candidate_file_sha256=candidate_file_sha256,
        reviewed_pool_file_sha256=reviewed_pool_file_sha256,
        old_gold_file_sha256=old_gold_file_sha256,
    )
    _validate_selection_priors(decisions, n100_audit)

    candidates_by_id = {str(row["candidate_id"]): row for row in candidates}
    if len(candidates_by_id) != len(candidates):
        raise ValueError("candidate manifest IDs are not unique")
    missing = sorted(ALL_SELECTED_IDS - set(candidates_by_id))
    if missing:
        raise ValueError("candidate manifest is missing frozen N50 IDs: " + ", ".join(missing))

    audit_samples: list[dict[str, Any]] = []
    for sample_rank, candidate_id in enumerate(ORDERED_N50_IDS, 1):
        candidate = candidates_by_id[candidate_id]
        if candidate.get("probe_status") != "success":
            raise ValueError("selected candidate probe did not succeed: %s" % candidate_id)
        if candidate.get("is_exact_pixel_duplicate") is not False:
            raise ValueError("selected candidate has exact-duplicate flag: %s" % candidate_id)
        if candidate.get("auto_exclude_exact_duplicate") is not False:
            raise ValueError("selected candidate has auto-exclude duplicate flag: %s" % candidate_id)
        if candidate.get("exact_duplicate_group") is not None or candidate.get("duplicate_of") is not None:
            raise ValueError("selected candidate has exact-duplicate identity: %s" % candidate_id)
        if candidate.get("has_phash_le8_candidate") is not False or candidate.get("phash_le8_matches") != []:
            raise ValueError("selected candidate has pHash <=8 evidence: %s" % candidate_id)
        review_id = opaque_review_id(candidate_id)
        audit_samples.append(
            {
                "sample_rank": sample_rank,
                "review_id": review_id,
                "subset_membership": _subset_membership(sample_rank),
                "selection_audit": {
                    "stratum": STRATUM_BY_ID[candidate_id],
                    "stratum_rank": STRATUM_RANK_BY_ID[candidate_id],
                    "selection_basis": "frozen_prior_audit_not_new_axis_gold",
                },
                "source_identity": _source_identity(candidate),
                "image_evidence": _image_evidence(candidate),
            }
        )

    review_ids = [str(row["review_id"]) for row in audit_samples]
    if len(set(review_ids)) != 50:
        raise ValueError("opaque review ID collision")
    ordered_review_ids = sorted(review_ids, key=_review_order_key)
    review_rows = [
        {"review_rank": rank, "review_id": review_id}
        for rank, review_id in enumerate(ordered_review_ids, 1)
    ]
    metrics = _selection_metrics(audit_samples)
    if any(metrics[field] != 50 for field in (
        "sample_count",
        "unique_candidate_count",
        "unique_asset_count",
        "unique_article_count",
        "unique_building_count",
        "unique_pixel_sha256_count",
        "probe_success_count",
    )):
        raise ValueError("selected N50 identity/probe accounting mismatch")
    if metrics["exact_duplicate_flag_count"] or metrics["phash_le8_flag_count"]:
        raise ValueError("selected N50 contains duplicate flags")
    if metrics["selected_id_set_sha256"] != EXPECTED_SELECTED_ID_SET_SHA256:
        raise ValueError("selected N50 ID-set SHA mismatch")

    hashes = [
        (
            str(row["source_identity"]["candidate_id"]),
            str(row["image_evidence"]["pixel_sha256"]),
            str(row["image_evidence"]["phash_256"]),
        )
        for row in audit_samples
    ]
    for index, (left_id, left_pixel, left_phash) in enumerate(hashes):
        _require_sha(left_pixel, "%s pixel SHA" % left_id)
        _require_sha(left_phash, "%s pHash" % left_id)
        for right_id, right_pixel, right_phash in hashes[index + 1 :]:
            if left_pixel == right_pixel:
                raise ValueError("selected exact pixel duplicate: %s/%s" % (left_id, right_id))
            if gold_contract.phash_distance(left_phash, right_phash) <= 8:
                raise ValueError("selected pHash <=8 pair: %s/%s" % (left_id, right_id))

    candidate_contract = candidate_manifest["contract"]
    provenance = {
        "source_db_sha256": EXPECTED_SOURCE_DB_SHA256,
        "parent_candidate_manifest": {
            "filename": "divisare_vision_gold_candidates_v1_2_probed.json",
            "file_sha256": candidate_file_sha256,
            "manifest_sha256": candidate_manifest["manifest_sha256"],
            "probe_logical_sha256": candidate_manifest["probe_contract"]["logical_sha256"],
        },
        "parent_reviewed_pool": {
            "filename": "divisare_vision_reviewed_pool_agent_v2.json",
            "file_sha256": reviewed_pool_file_sha256,
            "reviewed_pool_sha256": reviewed_pool["reviewed_pool_sha256"],
            "reviewer": reviewed_pool["reviewer"],
            "exported_at": reviewed_pool["exported_at"],
            "independent_human_review": False,
        },
        "old_gold_manifest": {
            "filename": "divisare_vision_gold_n100_v1.json",
            "file_sha256": old_gold_file_sha256,
            "gold_manifest_sha256": old_gold["gold_manifest_sha256"],
            "logical_sha256": old_gold["logical_sha256"],
        },
        "old_n100_benchmark": {
            "filename": "divisare_vision_resolution_n100_v1.db",
            "file_sha256": n100_audit.file_sha256,
            "logical_sha256": n100_audit.logical_sha256,
            "benchmark_version": n100_audit.benchmark_version,
            "status": n100_audit.status,
            "audit_lane": "long1024",
        },
    }
    selection_contract = {
        "development_only": True,
        "purpose": "prompt_and_schema_development_not_final_holdout",
        "selector_version": SELECTOR_VERSION,
        "blind_id_version": BLIND_ID_VERSION,
        "review_order_version": REVIEW_ORDER_VERSION,
        "sample_count": 50,
        "nested_prefixes": {"n10": 10, "n20": 20, "n50": 50},
        "remaining_order": "sha256(selector_version|remaining|candidate_id)",
        "selected_id_set_sha256": EXPECTED_SELECTED_ID_SET_SHA256,
        "source_profile": candidate_contract["source_profile"],
        "review_profile": candidate_contract["review_profile"],
        "identity_profile": candidate_contract["identity_profile"],
        "pixel_hash_version": candidate_contract["pixel_hash_version"],
        "phash_version": candidate_contract["phash_version"],
        "images_persisted": False,
        "reviewer_payload": "review_rows_only",
        "audit_payload": "audit_samples_never_model_facing",
        "prior_labels_are_new_axis_gold": False,
    }
    payload: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "development_only": True,
        "selector_version": SELECTOR_VERSION,
        "provenance": provenance,
        "selection_contract": selection_contract,
        "selection_metrics": metrics,
        "review_rows": review_rows,
        "audit_samples": audit_samples,
    }
    payload["logical_sha256"] = logical_sha256(payload)
    payload["manifest_sha256"] = manifest_sha256(payload)
    validate_devset_manifest(payload)
    return payload


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


def logical_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_logical_payload(payload))


def manifest_sha256(payload: Mapping[str, Any]) -> str:
    value = dict(payload)
    value.pop("manifest_sha256", None)
    return _sha256_value(value)


def validate_devset_manifest(payload: Mapping[str, Any]) -> None:
    gold_contract.canonical_json_bytes(payload)
    expected_top = frozenset(
        {
            "manifest_version",
            "development_only",
            "selector_version",
            "logical_sha256",
            "manifest_sha256",
            "provenance",
            "selection_contract",
            "selection_metrics",
            "review_rows",
            "audit_samples",
        }
    )
    _require_exact_fields(payload, expected_top, "axes dev manifest")
    _expect(payload.get("manifest_version"), MANIFEST_VERSION, "manifest version")
    _expect(payload.get("development_only"), True, "development_only")
    _expect(payload.get("selector_version"), SELECTOR_VERSION, "selector version")
    _expect(_require_sha(payload.get("logical_sha256"), "logical SHA"), logical_sha256(payload), "logical SHA")
    _expect(_require_sha(payload.get("manifest_sha256"), "manifest SHA"), manifest_sha256(payload), "manifest SHA")

    contract = payload.get("selection_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("selection_contract must be an object")
    _expect(contract.get("development_only"), True, "selection development_only")
    _expect(contract.get("selector_version"), SELECTOR_VERSION, "selection selector version")
    _expect(contract.get("blind_id_version"), BLIND_ID_VERSION, "blind ID version")
    _expect(contract.get("review_order_version"), REVIEW_ORDER_VERSION, "review order version")
    _expect(contract.get("nested_prefixes"), {"n10": 10, "n20": 20, "n50": 50}, "nested prefixes")
    _expect(contract.get("selected_id_set_sha256"), EXPECTED_SELECTED_ID_SET_SHA256, "selected ID set SHA")
    _expect(contract.get("reviewer_payload"), "review_rows_only", "reviewer payload contract")
    _expect(contract.get("prior_labels_are_new_axis_gold"), False, "prior-label contract")

    review_rows = payload.get("review_rows")
    audit_samples = payload.get("audit_samples")
    if not isinstance(review_rows, list) or len(review_rows) != 50:
        raise ValueError("review_rows must contain exactly 50 rows")
    if not isinstance(audit_samples, list) or len(audit_samples) != 50:
        raise ValueError("audit_samples must contain exactly 50 rows")

    for rank, row in enumerate(review_rows, 1):
        if not isinstance(row, Mapping):
            raise ValueError("public review row must be an object")
        _require_exact_fields(row, PUBLIC_REVIEW_FIELDS, "public review row")
        _expect(row.get("review_rank"), rank, "public review rank")
        if not isinstance(row.get("review_id"), str) or not REVIEW_ID_RE.fullmatch(row["review_id"]):
            raise ValueError("invalid opaque review ID")

    candidate_ids: list[str] = []
    review_ids: list[str] = []
    assets: list[str] = []
    articles: list[int] = []
    buildings: list[str] = []
    pixels: list[str] = []
    phashes: list[str] = []
    for rank, row in enumerate(audit_samples, 1):
        if not isinstance(row, Mapping):
            raise ValueError("audit sample must be an object")
        _require_exact_fields(row, AUDIT_SAMPLE_FIELDS, "audit sample")
        _expect(row.get("sample_rank"), rank, "sample rank")
        source = row.get("source_identity")
        evidence = row.get("image_evidence")
        selection = row.get("selection_audit")
        if not isinstance(source, Mapping) or not isinstance(evidence, Mapping) or not isinstance(selection, Mapping):
            raise ValueError("audit sample nested fields must be objects")
        _require_exact_fields(source, SOURCE_IDENTITY_FIELDS, "source identity")
        _require_exact_fields(evidence, IMAGE_EVIDENCE_FIELDS, "image evidence")
        _require_exact_fields(selection, SELECTION_AUDIT_FIELDS, "selection audit")
        candidate_id = str(source.get("candidate_id"))
        expected_id = ORDERED_N50_IDS[rank - 1]
        _expect(candidate_id, expected_id, "ordered N50 candidate ID")
        expected_review_id = opaque_review_id(candidate_id)
        _expect(row.get("review_id"), expected_review_id, "opaque review ID")
        _expect(row.get("subset_membership"), _subset_membership(rank), "subset membership")
        _expect(selection.get("stratum"), STRATUM_BY_ID[candidate_id], "selection stratum")
        _expect(selection.get("stratum_rank"), STRATUM_RANK_BY_ID[candidate_id], "selection stratum rank")
        _expect(selection.get("selection_basis"), "frozen_prior_audit_not_new_axis_gold", "selection basis")
        if evidence.get("probe_status") != "success":
            raise ValueError("audit sample is not a successful probe: %s" % candidate_id)
        if evidence.get("is_exact_pixel_duplicate") is not False or evidence.get("auto_exclude_exact_duplicate") is not False:
            raise ValueError("audit sample carries exact-duplicate flag: %s" % candidate_id)
        if evidence.get("exact_duplicate_group") is not None or evidence.get("duplicate_of") is not None:
            raise ValueError("audit sample carries exact-duplicate identity: %s" % candidate_id)
        if evidence.get("has_phash_le8_candidate") is not False or evidence.get("phash_le8_matches") != []:
            raise ValueError("audit sample carries pHash <=8 evidence: %s" % candidate_id)
        for url_field in ("request_url", "review_url"):
            url = source.get(url_field)
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("source identity has invalid %s: %s" % (url_field, candidate_id))
        candidate_ids.append(candidate_id)
        review_ids.append(expected_review_id)
        assets.append(str(source.get("asset_key")))
        articles.append(source.get("article_id"))
        buildings.append(str(source.get("building_id")))
        pixels.append(_require_sha(evidence.get("pixel_sha256"), "%s pixel SHA" % candidate_id))
        phashes.append(_require_sha(evidence.get("phash_256"), "%s pHash" % candidate_id))

    for name, values in (
        ("candidate", candidate_ids),
        ("review", review_ids),
        ("asset", assets),
        ("article", articles),
        ("building", buildings),
        ("pixel SHA", pixels),
    ):
        if len(set(values)) != 50:
            raise ValueError("audit samples do not have 50 unique %s values" % name)
    expected_public = [
        {"review_rank": rank, "review_id": review_id}
        for rank, review_id in enumerate(sorted(review_ids, key=_review_order_key), 1)
    ]
    _expect(review_rows, expected_public, "public blinded review order")
    for index, left in enumerate(phashes):
        for right in phashes[index + 1 :]:
            if gold_contract.phash_distance(left, right) <= 8:
                raise ValueError("audit samples contain a pHash <=8 pair")

    metrics = payload.get("selection_metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("selection_metrics must be an object")
    _expect(dict(metrics), _selection_metrics(audit_samples), "selection metrics")
    _expect(metrics.get("selected_id_set_sha256"), EXPECTED_SELECTED_ID_SET_SHA256, "metrics ID-set SHA")

    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("provenance must be an object")
    _expect(provenance.get("source_db_sha256"), EXPECTED_SOURCE_DB_SHA256, "provenance source SHA")
    for key in (
        "parent_candidate_manifest",
        "parent_reviewed_pool",
        "old_gold_manifest",
        "old_n100_benchmark",
    ):
        if not isinstance(provenance.get(key), Mapping):
            raise ValueError("provenance.%s must be an object" % key)


def inspect_n100_db(path: Path, *, file_sha: str) -> N100Audit:
    path = path.resolve()
    _expect(file_sha, EXPECTED_OLD_N100_DB_FILE_SHA256, "old N100 DB file SHA")
    uri = path.as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise ValueError("old N100 SQLite quick_check failed: %s" % quick_check)
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ValueError("old N100 SQLite has foreign-key violations")
        columns = [row[1] for row in conn.execute("PRAGMA table_info(benchmark_run)")]
        rows = conn.execute("SELECT * FROM benchmark_run ORDER BY run_id").fetchall()
        if len(rows) != 1:
            raise ValueError("old N100 must contain exactly one benchmark_run")
        run = dict(zip(columns, rows[0]))
        computed_logical = n100_contract.logical_sha256(conn)
        if run.get("logical_sha256") != computed_logical:
            raise ValueError("old N100 logical SHA mismatch")
        result_rows = conn.execute(
            """
            SELECT g.candidate_id,g.gold_label,g.clarity,v.predicted_label,
                   v.primary_correct,v.acceptable_correct
            FROM gold_samples g
            JOIN vision_results v USING(asset_key)
            WHERE v.lane='long1024'
            ORDER BY g.sample_rank
            """
        ).fetchall()
        if len(result_rows) != 100:
            raise ValueError("old N100 must contain 100 long1024 results")
        result_columns = (
            "candidate_id",
            "gold_label",
            "clarity",
            "predicted_label",
            "primary_correct",
            "acceptable_correct",
        )
        by_candidate = {
            str(row[0]): dict(zip(result_columns, row)) for row in result_rows
        }
        if len(by_candidate) != 100:
            raise ValueError("old N100 candidate IDs are not unique")
        audit = N100Audit(
            file_sha256=file_sha,
            logical_sha256=computed_logical,
            status=str(run["status"]),
            benchmark_version=str(run["benchmark_version"]),
            gold_manifest_file_sha256=str(run["gold_manifest_file_sha256"]),
            gold_manifest_sha256=str(run["gold_manifest_sha256"]),
            gold_logical_sha256=str(run["gold_logical_sha256"]),
            source_db_sha256=str(run["source_sha256_before"]),
            long1024_by_candidate=by_candidate,
        )
    return audit


def write_json_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError("immutable axes development manifest already exists: %s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".partial", dir=path.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(gold_contract.canonical_json_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError as exc:
            raise FileExistsError(
                "immutable axes development manifest already exists: %s" % path
            ) from exc
    finally:
        temp.unlink(missing_ok=True)


def build_devset_files(
    *,
    candidate_manifest_path: Path,
    reviewed_pool_path: Path,
    old_gold_path: Path,
    old_n100_db_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    paths = [
        candidate_manifest_path.resolve(),
        reviewed_pool_path.resolve(),
        old_gold_path.resolve(),
        old_n100_db_path.resolve(),
        output_path.resolve(),
    ]
    candidate_path, review_path, gold_path, n100_path, output_path = paths
    if len(set(paths)) != len(paths):
        raise ValueError("all input and output paths must be distinct")
    if output_path.exists():
        raise FileExistsError("immutable axes development manifest already exists: %s" % output_path)

    candidate_raw = candidate_path.read_bytes()
    review_raw = review_path.read_bytes()
    gold_raw = gold_path.read_bytes()
    candidate_file_sha = _sha256_bytes(candidate_raw)
    review_file_sha = _sha256_bytes(review_raw)
    gold_file_sha = _sha256_bytes(gold_raw)
    n100_file_sha = file_sha256(n100_path)

    candidate = gold_contract.parse_json_strict(candidate_raw, label="probed candidate manifest")
    review = gold_contract.parse_json_strict(review_raw, label="reviewed pool")
    old_gold = gold_contract.parse_json_strict(gold_raw, label="old N100 gold manifest")
    n100_audit = inspect_n100_db(n100_path, file_sha=n100_file_sha)
    payload = build_devset_payload(
        candidate_manifest=candidate,
        reviewed_pool=review,
        old_gold=old_gold,
        n100_audit=n100_audit,
        candidate_file_sha256=candidate_file_sha,
        reviewed_pool_file_sha256=review_file_sha,
        old_gold_file_sha256=gold_file_sha,
    )

    if candidate_path.read_bytes() != candidate_raw:
        raise RuntimeError("probed candidate manifest changed during selection")
    if review_path.read_bytes() != review_raw:
        raise RuntimeError("reviewed pool changed during selection")
    if gold_path.read_bytes() != gold_raw:
        raise RuntimeError("old gold manifest changed during selection")
    if file_sha256(n100_path) != n100_file_sha:
        raise RuntimeError("old N100 DB changed during selection")
    write_json_no_clobber(output_path, payload)
    return payload
