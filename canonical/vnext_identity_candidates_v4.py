"""WP2.4 N100-diagnosed identity policy replay and regression gates.

This producer is deliberately offline.  Frozen source inputs are evaluated again,
the final WP2.2 v3 artifact is comparison-only, and the WP2.3 sidecar is used only
to bind the already-reviewed regression gold.  It does not merge buildings, issue
IDs, or write Neon/R2.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from canonical import vnext_identity_candidates as base
from canonical import vnext_identity_candidates_v3 as prior
from canonical.vnext_country_norm import (
    AMBIGUOUS_COUNTRY_KEYS,
    COUNTRY_NORMALIZER_SPEC,
    ISO_COUNTRY_ADDITIONS,
    NORMALIZER_VERSION as COUNTRY_NORMALIZER_VERSION,
    SOURCE_PROVENANCE,
    classify_country,
)


CONTRACT_VERSION = "vnext-identity-contract-wp24-v1.1"
BUILDER_VERSION = "vnext-identity-candidates-builder-v4"
NORMALIZER_VERSION = prior.NORMALIZER_VERSION
POLICY_VERSION = "vnext-identity-n100-rule-revision-v4"
SAMPLE_VERSION = "vnext-identity-wp24-regression-only-no-fresh-n10-v4"
SAMPLE_SEED = "vnext-wp24-no-fresh-n10-20260826"
FULL_LOGICAL_VERSION = "vnext-identity-stage-a-wp24-transition-logical-v4"
APPLICATION_ID = base.APPLICATION_ID
SCHEMA_VERSION = 4

YEAR_WEAK_NEGATIVE_MIN_GAP = 6

DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_candidates_v4.db")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_candidates_v4_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_candidates_v4_stage_a_report.json")
DEFAULT_GATE = Path("data/reports/vnext_identity_candidates_v4_regression_gate.json")
DEFAULT_V3 = Path("data/enrichment/vnext_identity_candidates_v3.db")
DEFAULT_N100 = Path("data/enrichment/vnext_identity_n100_v1.db")

WP22_REGRESSION_IDS = prior.WP22_REGRESSION_IDS
REGRESSION_GATE_NAMES = (
    "b4_name_only_reject",
    "wp22_country_conflict_reject",
    "b2_different_phash_hard_conflict",
    "b1_b3_human_review_queue",
)
EXPECTED_REGRESSION_GATE_COUNTS = {
    "b4_name_only_reject": 10,
    "wp22_country_conflict_reject": 4,
    "b2_different_phash_hard_conflict": 4,
    "b1_b3_human_review_queue": 15,
}

INPUT_PINS: Mapping[str, tuple[Path, int, str, str]] = {
    "divisare_curated_metadata": (*base.INPUT_SPECS["divisare_curated_metadata"], "candidate_and_evidence"),
    "architizer_curated_metadata": (*base.INPUT_SPECS["architizer_curated_metadata"], "candidate_and_evidence"),
    "cross_source_e2_full_v5": (*base.INPUT_SPECS["cross_source_e2_full_v5"], "candidate_and_evidence"),
    "identity_contract": (
        base.DEFAULT_CONTRACT,
        9_879,
        "502e6258b9c7803ea58bc415ddbe73fbcc50faa0005d8b9aaaaaeab04838e3e8",
        "policy_contract",
    ),
    "building_matcher": (*base.FILE_PINS["building_matcher"], "candidate_blocking_only"),
    "phash_matcher": (*base.FILE_PINS["phash_matcher"], "image_policy_provenance"),
    "workspace_freeze_manifest": (
        base.DEFAULT_FREEZE,
        246_838,
        "cd787a0088161fd0e8dddfe23e1943c0a41992f1e1a214b67fcce5d3ae17dcc3",
        "pin_manifest_only",
    ),
    "v3_candidate_artifact": (
        DEFAULT_V3,
        580_710_400,
        "c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f",
        "comparison_only_not_candidate_evidence",
    ),
    "n100_gold_artifact": (
        DEFAULT_N100,
        2_183_168,
        "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b",
        "regression_gold_binding_only_not_candidate_evidence",
    ),
    "wp24_rule_revision_order": (
        Path(".claude/ops/jobs/20260826_vnext_wp24_rule_revision_order.md"),
        5_585,
        "e118437909c7b8cbcf5842c6eadecea39a84b958af569bbe2801d03b38a31b2a",
        "issued_policy_order",
    ),
    "n100_final_manifest": (
        Path("data/reports/vnext_identity_n100_v1_manifest.json"),
        3_579,
        "e4accff5634ce97712f5cb226e9a5557a75aef7699f6d532a6fcf514f2e3e9da",
        "regression_gold_chain_manifest",
    ),
    "n100_locked_adjudication": (
        Path("data/reports/vnext_identity_n100_v1_adjudication_locked.json"),
        63_713,
        "c4fec6261cefb6a00c25baea9e36fccb9579e4832fbe52d80fd72fa36e9d3337",
        "regression_gold_chain_locked_adjudication",
    ),
    "v1_builder_code": (
        Path("canonical/vnext_identity_candidates.py"),
        75_798,
        "3997998b029fc1306ab5a632102af96594ef3034fa6ef44ef88543eb40179447",
        "shared_stage_a_engine_provenance",
    ),
    "v3_builder_code": (
        Path("canonical/vnext_identity_candidates_v3.py"),
        57_034,
        "94bf799f4ce3279b24a8b0dee734127e74a600331d97bd234d49349c398d915c",
        "comparison_artifact_provenance_and_schema_lineage",
    ),
    "country_normalizer_module": (
        Path("canonical/vnext_country_norm.py"),
        22_896,
        "98e3cf971525f17393d772f45bf52ac3dfe37dbb5d4bc593019483eafab36015",
        "normalizer_implementation",
    ),
    **{
        role: (path, size, digest, "normalizer_legacy_provenance")
        for role, (path, size, digest) in SOURCE_PROVENANCE.items()
    },
}

NORMALIZER_SPEC = {
    **base.NORMALIZER_SPEC,
    "version": NORMALIZER_VERSION,
    "country_normalizer_version": COUNTRY_NORMALIZER_VERSION,
    "country": COUNTRY_NORMALIZER_SPEC,
}
POLICY_SPEC = {
    **base.POLICY_SPEC,
    "version": POLICY_VERSION,
    "rule_application_order": ["R1_year", "R2_phash", "anchor", "R3_fanout", "R4_queue", "R5_regression"],
    "R1_year": {
        "positive_gap_max": 1,
        "neutral_gap": [2, 5],
        "weak_negative_gap_min": YEAR_WEAK_NEGATIVE_MIN_GAP,
        "matcher_architect_year_gate_is_hard_conflict": False,
        "project_phase_hard_conflict": "gap>=6 and no positive image and no positive firm",
    },
    "R2_phash": {
        "downgrade_when": "name_alias_identity positive and architect_firm_identity positive",
        "policy_flag": "phash_veto_review_required",
        "auto_merge_eligible": False,
    },
    "R3_fanout": {
        "snapshot": "post_R1_R2_anchor_pre_fanout",
        "qualifying_result": "merge_candidate+fanout_review_required+eligible_false",
        "under_threshold_result": "defer",
        "hard_conflict_written": False,
        "transitive_closure": False,
    },
    "R4_queue": "all merge_candidate plus anchored positive-family>=2 defer",
    "R5_unchanged": ["name_only_band", "known_country_conflict", "anchor_requirement"],
    "country_positive_rule": "both known canonical equal and exact locality",
    "country_hard_conflict_rule": "both known canonical and unequal",
    "country_unknown_ambiguous_rule": "neutral_no_positive_no_conflict",
    "ambiguous_country_keys": sorted(AMBIGUOUS_COUNTRY_KEYS),
    "v3_artifact_usage": "comparison_only_not_candidate_evidence",
    "n100_artifact_usage": "regression_gold_binding_only_not_candidate_evidence",
    "fresh_n10": False,
}


V4_BASE_SCHEMA_SQL = prior.V3_BASE_SCHEMA_SQL
V4_SCHEMA_SQL = """
CREATE TABLE country_normalizations(
  run_id TEXT NOT NULL REFERENCES runs(run_id), source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, raw_country TEXT NOT NULL,
  normalization_status TEXT NOT NULL CHECK(normalization_status IN ('known','unknown','ambiguous')),
  canonical_country TEXT, reason TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id),
  FOREIGN KEY(run_id,source,source_building_id) REFERENCES source_buildings(run_id,source,source_building_id)
);
CREATE TABLE candidate_policy_flags(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  flag_type TEXT NOT NULL CHECK(flag_type IN (
    'anchor_missing','phash_veto_review_required','fanout_review_required'
  )),
  detail_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id,flag_type),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id)
);
CREATE TABLE fanout_groups(
  run_id TEXT NOT NULL REFERENCES runs(run_id), hub_source TEXT NOT NULL,
  hub_source_building_id TEXT NOT NULL, pre_guard_degree INTEGER NOT NULL CHECK(pre_guard_degree>=2),
  status TEXT NOT NULL CHECK(status IN ('exempt','conflict')),
  incident_candidate_ids_json TEXT NOT NULL, opponent_source_building_ids_json TEXT NOT NULL,
  unrelated_opponent_pairs_json TEXT NOT NULL, detail_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,hub_source,hub_source_building_id)
);
CREATE TABLE human_review_queue(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  proposal TEXT NOT NULL, auto_merge_eligible INTEGER NOT NULL CHECK(auto_merge_eligible IN (0,1)),
  queue_reasons_json TEXT NOT NULL, positive_families_json TEXT NOT NULL,
  anchor_families_json TEXT NOT NULL, policy_flags_json TEXT NOT NULL,
  evidence_strength_band TEXT NOT NULL CHECK(evidence_strength_band IN (
    'name_firm_image_3plus','other_anchor_3plus','anchor_2','no_anchor_2plus'
  )),
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,candidate_id),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id)
);
CREATE TABLE candidate_transitions(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  left_source_building_id TEXT NOT NULL, right_source_building_id TEXT NOT NULL,
  v3_proposal TEXT NOT NULL, v4_proposal TEXT NOT NULL,
  v3_auto_merge_eligible INTEGER NOT NULL CHECK(v3_auto_merge_eligible IN (0,1)),
  v4_auto_merge_eligible INTEGER NOT NULL CHECK(v4_auto_merge_eligible IN (0,1)),
  v3_positive_family_count INTEGER NOT NULL, v4_positive_family_count INTEGER NOT NULL,
  v3_negative_family_count INTEGER NOT NULL, v4_negative_family_count INTEGER NOT NULL,
  v3_hard_conflicts_json TEXT NOT NULL, v4_hard_conflicts_json TEXT NOT NULL,
  v3_policy_flags_json TEXT NOT NULL, v4_policy_flags_json TEXT NOT NULL,
  r1_before_json TEXT NOT NULL, r1_after_json TEXT NOT NULL,
  r1_changed INTEGER NOT NULL CHECK(r1_changed IN (0,1)),
  r2_before_json TEXT NOT NULL, r2_after_json TEXT NOT NULL,
  r2_changed INTEGER NOT NULL CHECK(r2_changed IN (0,1)),
  r3_before_json TEXT NOT NULL, r3_after_json TEXT NOT NULL,
  r3_changed INTEGER NOT NULL CHECK(r3_changed IN (0,1)),
  v3_human_review_queue INTEGER NOT NULL CHECK(v3_human_review_queue IN (0,1)),
  v4_human_review_queue INTEGER NOT NULL CHECK(v4_human_review_queue IN (0,1)),
  r4_queue_reasons_json TEXT NOT NULL, evidence_strength_band TEXT NOT NULL,
  proposal_changed INTEGER NOT NULL CHECK(proposal_changed IN (0,1)),
  eligibility_changed INTEGER NOT NULL CHECK(eligibility_changed IN (0,1)),
  decision_changed INTEGER NOT NULL CHECK(decision_changed IN (0,1)),
  reasons_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id)
);
CREATE TABLE regression_bindings(
  run_id TEXT NOT NULL REFERENCES runs(run_id), case_id TEXT NOT NULL,
  gate_name TEXT NOT NULL CHECK(gate_name IN (
    'b4_name_only_reject','wp22_country_conflict_reject',
    'b2_different_phash_hard_conflict','b1_b3_human_review_queue'
  )),
  subgroup TEXT NOT NULL, candidate_id TEXT NOT NULL,
  source_artifact_role TEXT NOT NULL, source_sample_id TEXT, source_stratum TEXT,
  sample_origin TEXT, gold_label TEXT, sample_record_sha256 TEXT,
  gold_record_sha256 TEXT, expected_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,case_id), UNIQUE(run_id,gate_name,subgroup,candidate_id),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id)
);
CREATE TABLE regression_results(
  run_id TEXT NOT NULL REFERENCES runs(run_id), case_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL, actual_proposal TEXT NOT NULL,
  actual_auto_merge_eligible INTEGER NOT NULL CHECK(actual_auto_merge_eligible IN (0,1)),
  actual_hard_conflicts_json TEXT NOT NULL, actual_policy_flags_json TEXT NOT NULL,
  actual_human_review_queue INTEGER NOT NULL CHECK(actual_human_review_queue IN (0,1)),
  passed INTEGER NOT NULL CHECK(passed IN (0,1)), detail_json TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,case_id),
  FOREIGN KEY(run_id,case_id) REFERENCES regression_bindings(run_id,case_id)
);
CREATE TABLE regression_gate_results(
  run_id TEXT NOT NULL REFERENCES runs(run_id), gate_name TEXT NOT NULL,
  expected_case_count INTEGER NOT NULL, actual_case_count INTEGER NOT NULL,
  passed_case_count INTEGER NOT NULL, failed_case_count INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PASS','FAIL')), detail_json TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,gate_name)
);
"""

FULL_LOGICAL_TABLES = (
    ("country_normalizations", "source,source_building_id"),
    ("candidate_policy_flags", "candidate_id,flag_type"),
    ("fanout_groups", "hub_source,hub_source_building_id"),
    ("human_review_queue", "candidate_id"),
    ("candidate_transitions", "candidate_id"),
    ("regression_bindings", "case_id"),
    ("regression_results", "case_id"),
    ("regression_gate_results", "gate_name"),
)


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    manifest_path: Path
    report_path: Path
    gate_path: Path
    run_id: str
    logical_sha256: str
    full_logical_sha256: str
    candidate_count: int
    merge_candidate_count: int
    auto_merge_eligible_count: int
    human_review_queue_count: int
    changed_transition_count: int
    regression_gate_status: str
    elapsed_seconds: float


class IdentityCandidateV4Error(RuntimeError):
    """Raised when a WP2.4 input, policy, or regression invariant fails closed."""


def _artifact_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _verify_pins(root: Path, full_hashes: bool) -> dict[str, dict[str, Any]]:
    pins: dict[str, dict[str, Any]] = {}
    for role, (relative, size, expected, usage) in INPUT_PINS.items():
        path = root / relative
        if not path.is_file() or path.stat().st_size != size:
            raise IdentityCandidateV4Error(f"input size/missing mismatch: {role}")
        if path.suffix == ".db":
            base._assert_no_sidecars(path)
        digest = base.sha256_file(path) if full_hashes else expected
        if digest != expected:
            raise IdentityCandidateV4Error(f"input SHA mismatch: {role}")
        pins[role] = {
            "path": relative.as_posix(),
            "size_bytes": size,
            "sha256_before": digest,
            "sha256_after": None,
            "usage": usage,
        }
    return pins


def _apply_country_normalization(buildings: Mapping[tuple[str, str], dict[str, Any]]) -> None:
    for building in buildings.values():
        normalized = classify_country(building["country"])
        building["country_norm"] = normalized.canonical or ""
        building["country_normalization_status"] = normalized.status
        building["country_normalization_reason"] = normalized.reason


def _family(result: Mapping[str, Any], family_name: str) -> dict[str, Any]:
    return next(row for row in result["families"] if row["family"] == family_name)


def _positive_families(result: Mapping[str, Any]) -> list[str]:
    return sorted(row["family"] for row in result["families"] if row["status"] == "positive")


def _negative_families(result: Mapping[str, Any]) -> list[str]:
    return sorted(row["family"] for row in result["families"] if row["status"] == "negative")


def _anchor_families(result: Mapping[str, Any]) -> list[str]:
    positive = set(_positive_families(result))
    return [
        family
        for family in ("name_alias_identity", "verified_pixel_image_overlap")
        if family in positive
    ]


def _policy_flag_types(result: Mapping[str, Any]) -> list[str]:
    return sorted(str(row["flag_type"]) for row in result.get("policy_flags", []))


def _conflict_types(result: Mapping[str, Any]) -> list[str]:
    return sorted(str(row["conflict_type"]) for row in result["conflicts"])


def _state_snapshot(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "proposal": str(result["proposal"]),
        "auto_merge_eligible": int(result["auto_merge_eligible"]),
        "positive_family_count": int(result["positive_family_count"]),
        "negative_family_count": int(result["negative_family_count"]),
        "positive_families": _positive_families(result),
        "negative_families": _negative_families(result),
        "anchor_families": _anchor_families(result),
        "hard_conflicts": _conflict_types(result),
        "policy_flags": _policy_flag_types(result),
    }


def _snapshot_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    return base.canonical_json(before) != base.canonical_json(after)


def _remove_policy_flag(result: dict[str, Any], flag_type: str) -> None:
    result["policy_flags"] = [
        row for row in result.get("policy_flags", []) if row["flag_type"] != flag_type
    ]


def _replace_conflict(
    result: dict[str, Any], conflict_types: set[str], replacement: Mapping[str, Any] | None = None
) -> None:
    result["conflicts"] = [
        row for row in result["conflicts"] if row["conflict_type"] not in conflict_types
    ]
    if replacement is not None:
        result["conflicts"].append(dict(replacement))


def _recompute_decision(result: dict[str, Any]) -> None:
    positive = len(_positive_families(result))
    negative = len(_negative_families(result))
    conflict_types = set(_conflict_types(result))
    hard_count = len(conflict_types)
    name_positive = _family(result, "name_alias_identity")["status"] == "positive"
    firm_positive = _family(result, "architect_firm_identity")["status"] == "positive"
    site_positive = _family(result, "location_site_identity")["status"] == "positive"
    if hard_count == 0 and positive >= 2:
        proposal = "merge_candidate"
    elif hard_count:
        related_signal = bool(
            name_positive
            and (firm_positive or site_positive)
            and "project_phase_incompatible" in conflict_types
            and "country_site_incompatible" not in conflict_types
        )
        proposal = "related_but_separate" if related_signal else "reject"
    else:
        proposal = "defer"
    result["positive_family_count"] = positive
    result["negative_family_count"] = negative
    result["hard_conflict_count"] = hard_count
    result["proposal"] = proposal
    result["auto_merge_eligible"] = int(proposal == "merge_candidate")


def _apply_anchor_policy(result: dict[str, Any]) -> None:
    _remove_policy_flag(result, "anchor_missing")
    anchors = _anchor_families(result)
    result["anchor_families"] = anchors
    if result["proposal"] == "merge_candidate" and not anchors:
        result["auto_merge_eligible"] = 0
        result["policy_flags"].append(
            {
                "flag_type": "anchor_missing",
                "detail": {
                    "positive_family_count": result["positive_family_count"],
                    "required_anchor_families": [
                        "name_alias_identity",
                        "verified_pixel_image_overlap",
                    ],
                    "name_strict_score": round(float(result["name_strict"]), 6),
                    "strong_image_count": int(result["strong_image_count"]),
                    "proposal_preserved": result["proposal"],
                },
            }
        )


def _apply_r1_year(result: dict[str, Any]) -> None:
    gap = result["year_gap"]
    firm_positive = _family(result, "architect_firm_identity")["status"] == "positive"
    image_positive = _family(result, "verified_pixel_image_overlap")["status"] == "positive"
    year = _family(result, "year_phase_compatibility")
    if gap is None:
        year_status = "unknown"
    elif int(gap) <= base.YEAR_POSITIVE_TOLERANCE:
        year_status = "positive"
    elif int(gap) >= YEAR_WEAK_NEGATIVE_MIN_GAP:
        year_status = "negative"
    else:
        year_status = "unknown"
    year["status"] = year_status
    year["detail"].update(
        {
            "positive_tolerance": base.YEAR_POSITIVE_TOLERANCE,
            "neutral_gap_min": 2,
            "neutral_gap_max": 5,
            "weak_negative_min_gap": YEAR_WEAK_NEGATIVE_MIN_GAP,
            "negative_strength": "weak" if year_status == "negative" else None,
            "matcher_architect_year_gate_is_hard_conflict": False,
        }
    )
    phase_conflict = bool(
        gap is not None
        and int(gap) >= YEAR_WEAK_NEGATIVE_MIN_GAP
        and not image_positive
        and not firm_positive
    )
    replacement = None
    if phase_conflict:
        replacement = {
            "conflict_type": "project_phase_incompatible",
            "detail": {
                "year_gap": int(gap),
                "hard_gap_min_inclusive": YEAR_WEAK_NEGATIVE_MIN_GAP,
                "verified_pixel_image_overlap_positive": image_positive,
                "architect_firm_identity_positive": firm_positive,
                "rule": "gap>=6 and neither image nor firm positive",
            },
        }
    _replace_conflict(
        result,
        {"matcher_architect_year_gate_block", "project_phase_incompatible"},
        replacement,
    )
    _recompute_decision(result)
    _apply_anchor_policy(result)


def _apply_r2_phash(result: dict[str, Any]) -> None:
    _remove_policy_flag(result, "phash_veto_review_required")
    name_positive = _family(result, "name_alias_identity")["status"] == "positive"
    firm_positive = _family(result, "architect_firm_identity")["status"] == "positive"
    veto_rows = [
        row for row in result["conflicts"] if row["conflict_type"] == "verified_phash_veto"
    ]
    downgraded = bool(veto_rows and name_positive and firm_positive)
    if downgraded:
        _replace_conflict(result, {"verified_phash_veto"})
    _recompute_decision(result)
    _apply_anchor_policy(result)
    if downgraded:
        result["auto_merge_eligible"] = 0
        result["policy_flags"].append(
            {
                "flag_type": "phash_veto_review_required",
                "detail": {
                    "downgraded_conflict": veto_rows[0],
                    "name_alias_identity_positive": True,
                    "architect_firm_identity_positive": True,
                    "image_family_status_preserved": _family(
                        result, "verified_pixel_image_overlap"
                    )["status"],
                    "auto_merge_eligible": False,
                    "other_hard_conflicts": _conflict_types(result),
                },
            }
        )


def evaluate_candidate(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    origins: set[str],
    e2_row: Mapping[str, Any],
    left_stats: Mapping[str, Any],
    right_stats: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one candidate and retain sequential R1/R2 audit snapshots."""
    result = base.evaluate_candidate(left, right, origins, e2_row, left_stats, right_stats)
    location = _family(result, "location_site_identity")
    location["detail"].update(
        {
            "left_country_status": left["country_normalization_status"],
            "right_country_status": right["country_normalization_status"],
            "left_country_canonical": left["country_norm"] or None,
            "right_country_canonical": right["country_norm"] or None,
            "rule": "both-known-canonical; equal+exact-locality positive; unequal conflict; otherwise neutral",
        }
    )
    for conflict in result["conflicts"]:
        if conflict["conflict_type"] == "country_site_incompatible":
            conflict["detail"].update(
                {
                    "left_country_canonical": left["country_norm"],
                    "right_country_canonical": right["country_norm"],
                    "known_on_both_sides": True,
                }
            )
    result["policy_flags"] = []
    _apply_anchor_policy(result)
    result["r1_before_snapshot"] = _state_snapshot(result)
    _apply_r1_year(result)
    result["r1_after_snapshot"] = _state_snapshot(result)
    result["r1_changed"] = int(
        _snapshot_changed(result["r1_before_snapshot"], result["r1_after_snapshot"])
    )
    result["r2_before_snapshot"] = _state_snapshot(result)
    _apply_r2_phash(result)
    result["r2_after_snapshot"] = _state_snapshot(result)
    result["r2_changed"] = int(
        _snapshot_changed(result["r2_before_snapshot"], result["r2_after_snapshot"])
    )
    return result


def _same_source_counterpart_relation(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Direct-only relation for the R3 clique exception under revised R1."""
    name_strict, _name_loose, _name_set, name_left, name_right = base.best_similarity(
        left["alias_norms"], right["alias_norms"]
    )
    firm_strict, _firm_loose, _firm_set, firm_left, firm_right = base.best_similarity(
        left["firm_keys"], right["firm_keys"]
    )
    exact_firms = sorted(set(left["firm_keys"]) & set(right["firm_keys"]))
    firm_positive = bool(exact_firms) or firm_strict >= base.FIRM_POSITIVE_THRESHOLD
    gap = base._year_gap(left, right)
    country_conflict = bool(
        left["country_norm"]
        and right["country_norm"]
        and left["country_norm"] != right["country_norm"]
    )
    phase_conflict = bool(
        gap is not None and gap >= YEAR_WEAK_NEGATIVE_MIN_GAP and not firm_positive
    )
    site_positive = bool(
        left["country_norm"]
        and left["country_norm"] == right["country_norm"]
        and left["locality_norm"]
        and left["locality_norm"] == right["locality_norm"]
    )
    year_positive = bool(gap is not None and gap <= base.YEAR_POSITIVE_TOLERANCE)
    additional: list[str] = []
    if firm_positive:
        additional.append("architect_firm_identity")
    if site_positive:
        additional.append("location_site_identity")
    if year_positive:
        additional.append("year_phase_compatibility")
    hard_conflicts: list[str] = []
    if country_conflict:
        hard_conflicts.append("country_site_incompatible")
    if phase_conflict:
        hard_conflicts.append("project_phase_incompatible")
    related = bool(
        name_strict >= base.NAME_POSITIVE_THRESHOLD and additional and not hard_conflicts
    )
    return related, {
        "related": related,
        "name_strict_score": round(name_strict, 6),
        "name_left_alias": name_left,
        "name_right_alias": name_right,
        "name_threshold": base.NAME_POSITIVE_THRESHOLD,
        "additional_positive_families": additional,
        "firm_strict_score": round(firm_strict, 6),
        "firm_left_key": firm_left,
        "firm_right_key": firm_right,
        "exact_firm_keys": exact_firms,
        "year_gap": gap,
        "hard_conflicts": hard_conflicts,
        "image_evidence_available": False,
        "rule_version": "WP2.4-R1-R3",
    }


def apply_fanout_guard(
    evaluated: Mapping[str, dict[str, Any]],
    buildings: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply R3 simultaneously from one post-R1/R2/anchor graph snapshot."""
    for row in evaluated.values():
        row["r3_before_snapshot"] = _state_snapshot(row)
        row["r3_triggered"] = 0
    eligible = [row for row in evaluated.values() if row["auto_merge_eligible"]]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        grouped[("divisare", str(row["left_id"]))].append(row)
        grouped[("architizer", str(row["right_id"]))].append(row)
    triggers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    groups: list[dict[str, Any]] = []
    for (hub_source, hub_id), incident in sorted(grouped.items()):
        if len(incident) < 2:
            continue
        opponent_source = "architizer" if hub_source == "divisare" else "divisare"
        opponent_ids = sorted(
            {
                str(row["right_id"] if hub_source == "divisare" else row["left_id"])
                for row in incident
            }
        )
        pair_checks: list[dict[str, Any]] = []
        unrelated: list[list[str]] = []
        for first_id, second_id in combinations(opponent_ids, 2):
            related, detail = _same_source_counterpart_relation(
                buildings[(opponent_source, first_id)],
                buildings[(opponent_source, second_id)],
            )
            pair_checks.append(
                {
                    "first_source_building_id": first_id,
                    "second_source_building_id": second_id,
                    **detail,
                }
            )
            if not related:
                unrelated.append([first_id, second_id])
        status = "conflict" if unrelated else "exempt"
        incident_ids = sorted(str(row["candidate_id"]) for row in incident)
        detail = {
            "snapshot": "post_R1_R2_anchor_pre_fanout",
            "hub_source": hub_source,
            "hub_source_building_id": hub_id,
            "pre_guard_degree": len(incident),
            "opponent_source": opponent_source,
            "incident_candidate_ids": incident_ids,
            "opponent_source_building_ids": opponent_ids,
            "pair_checks": pair_checks,
            "all_opponent_pairs_direct_related": not unrelated,
            "transitive_closure_used": False,
        }
        group = {
            "hub_source": hub_source,
            "hub_source_building_id": hub_id,
            "pre_guard_degree": len(incident),
            "status": status,
            "incident_candidate_ids": incident_ids,
            "opponent_source_building_ids": opponent_ids,
            "unrelated_opponent_pairs": unrelated,
            "detail": detail,
        }
        groups.append(group)
        if status == "conflict":
            trigger = {
                "hub_source": hub_source,
                "hub_source_building_id": hub_id,
                "pre_guard_degree": len(incident),
                "unrelated_opponent_pairs": unrelated,
            }
            for row in incident:
                triggers[str(row["candidate_id"])].append(trigger)
    for candidate_id, candidate_triggers in sorted(triggers.items()):
        row = evaluated[candidate_id]
        row["r3_triggered"] = 1
        qualifying = bool(row["positive_family_count"] >= 2 and _anchor_families(row))
        row["auto_merge_eligible"] = 0
        if qualifying:
            row["proposal"] = "merge_candidate"
            row["policy_flags"].append(
                {
                    "flag_type": "fanout_review_required",
                    "detail": {
                        "snapshot": "post_R1_R2_anchor_pre_fanout",
                        "triggers": candidate_triggers,
                        "positive_family_count": row["positive_family_count"],
                        "anchor_families": _anchor_families(row),
                        "auto_merge_eligible": False,
                    },
                }
            )
        else:
            row["proposal"] = "defer"
    for row in evaluated.values():
        row["r3_after_snapshot"] = _state_snapshot(row)
        row["r3_changed"] = int(
            _snapshot_changed(row["r3_before_snapshot"], row["r3_after_snapshot"])
        )
    return groups


def human_review_queue_reasons(result: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if result["proposal"] == "merge_candidate":
        reasons.append("merge_candidate")
    if (
        result["proposal"] == "defer"
        and int(result["positive_family_count"]) >= 2
        and _anchor_families(result)
    ):
        reasons.append("r4_anchor_two_family_defer")
    return reasons


def evidence_strength_band(result: Mapping[str, Any]) -> str:
    if not human_review_queue_reasons(result):
        return "not_queued"
    positive = set(_positive_families(result))
    anchors = set(_anchor_families(result))
    if {
        "name_alias_identity",
        "architect_firm_identity",
        "verified_pixel_image_overlap",
    }.issubset(positive):
        return "name_firm_image_3plus"
    if anchors and len(positive) >= 3:
        return "other_anchor_3plus"
    if anchors and len(positive) == 2:
        return "anchor_2"
    return "no_anchor_2plus"


def _insert_country_rows(
    database: sqlite3.Connection,
    run_id: str,
    buildings: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    for key in sorted(buildings):
        row = buildings[key]
        payload = {
            "source": key[0],
            "source_building_id": key[1],
            "raw_country": str(row["country"] or ""),
            "normalization_status": row["country_normalization_status"],
            "canonical_country": row["country_norm"] or None,
            "reason": row["country_normalization_reason"],
        }
        database.execute(
            "INSERT INTO country_normalizations VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                key[0],
                key[1],
                payload["raw_country"],
                payload["normalization_status"],
                payload["canonical_country"],
                payload["reason"],
                base._record_sha(payload),
            ),
        )


def _insert_candidates(
    database: sqlite3.Connection,
    run_id: str,
    buildings: Mapping[tuple[str, str], Mapping[str, Any]],
    image_stats: Mapping[tuple[str, str], Mapping[str, Any]],
    universe: Mapping[tuple[str, str], set[str]],
    e2_summaries: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    evaluated: dict[str, dict[str, Any]] = {}
    for left_id, right_id in sorted(universe):
        pair = (left_id, right_id)
        candidate_id = base.candidate_id(left_id, right_id)
        left = buildings[("divisare", left_id)]
        right = buildings[("architizer", right_id)]
        e2_row = e2_summaries.get(pair, {})
        result = evaluate_candidate(
            left,
            right,
            set(universe[pair]),
            e2_row,
            image_stats.get(("divisare", left_id), {}),
            image_stats.get(("architizer", right_id), {}),
        )
        evaluated[candidate_id] = {
            **result,
            "left_id": left_id,
            "right_id": right_id,
            "candidate_id": candidate_id,
            "e2_row": e2_row,
        }
    fanout_groups = apply_fanout_guard(evaluated, buildings)

    for candidate_id in sorted(evaluated):
        result = evaluated[candidate_id]
        left_id = str(result["left_id"])
        right_id = str(result["right_id"])
        e2_row = result["e2_row"]
        origins = set(result["origins"])
        image_counts = {
            "exact": int(e2_row.get("exact_asset_pair_count", 0) or 0),
            "identical": int(e2_row.get("identical_phash_pair_count", 0) or 0),
            "le8": int(e2_row.get("phash_le8_pair_count", 0) or 0),
            "weak": int(e2_row.get("phash_9_16_pair_count", 0) or 0),
        }
        payload = {
            "candidate_id": candidate_id,
            "left_source_building_id": left_id,
            "right_source_building_id": right_id,
            "origins": sorted(origins),
            "e2_metadata_pair_id": e2_row.get("metadata_pair_id"),
            "e2_building_candidate_id": e2_row.get("e2_building_candidate_id"),
            "image_counts": image_counts,
            "min_phash_distance": e2_row.get("min_phash_distance"),
            "name_strict_score": round(float(result["name_strict"]), 6),
            "name_loose_score": round(float(result["name_loose"]), 6),
            "name_token_set_score": round(float(result["name_set"]), 6),
            "firm_strict_score": round(float(result["firm_strict"]), 6),
            "year_gap": result["year_gap"],
            "positive_family_count": result["positive_family_count"],
            "negative_family_count": result["negative_family_count"],
            "hard_conflict_count": result["hard_conflict_count"],
            "proposal": result["proposal"],
            "auto_merge_eligible": result["auto_merge_eligible"],
            "anchor_families": _anchor_families(result),
            "policy_flags": _policy_flag_types(result),
        }
        database.execute(
            "INSERT INTO candidate_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                candidate_id,
                "divisare",
                left_id,
                "architizer",
                right_id,
                int("e2_metadata" in origins),
                int("e2_candidate" in origins),
                int("matcher_architect_year" in origins),
                int("matcher_pass2" in origins),
                len(origins),
                e2_row.get("metadata_pair_id"),
                e2_row.get("e2_building_candidate_id"),
                image_counts["exact"],
                image_counts["identical"],
                image_counts["le8"],
                image_counts["weak"],
                e2_row.get("min_phash_distance"),
                round(float(result["name_strict"]), 6),
                round(float(result["name_loose"]), 6),
                round(float(result["name_set"]), 6),
                round(float(result["firm_strict"]), 6),
                result["year_gap"],
                result["positive_family_count"],
                result["negative_family_count"],
                result["hard_conflict_count"],
                result["proposal"],
                result["auto_merge_eligible"],
                base._record_sha(payload),
            ),
        )
        for origin in sorted(origins):
            detail = {
                "origin": origin,
                "pinned_matcher_sha256": (
                    base.FILE_PINS["building_matcher"][2] if origin.startswith("matcher_") else None
                ),
                "e2_metadata_record_sha256": (
                    e2_row.get("e2_metadata_record_sha256") if origin == "e2_metadata" else None
                ),
                "e2_candidate_record_sha256": (
                    e2_row.get("e2_candidate_record_sha256") if origin == "e2_candidate" else None
                ),
            }
            database.execute(
                "INSERT INTO candidate_origins VALUES(?,?,?,?,?)",
                (
                    run_id,
                    candidate_id,
                    origin,
                    base.canonical_json(detail),
                    base._record_sha(
                        {"candidate_id": candidate_id, "origin": origin, "detail": detail}
                    ),
                ),
            )
        for family in result["families"]:
            family_payload = {
                "candidate_id": candidate_id,
                "family": family["family"],
                "status": family["status"],
                "score": family["score"],
                "detail": family["detail"],
            }
            database.execute(
                "INSERT INTO evidence_families VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    candidate_id,
                    family["family"],
                    family["status"],
                    family["score"],
                    base.canonical_json(family["detail"]),
                    base._record_sha(family_payload),
                ),
            )
        for conflict in sorted(result["conflicts"], key=lambda row: str(row["conflict_type"])):
            conflict_payload = {
                "candidate_id": candidate_id,
                "conflict_type": conflict["conflict_type"],
                "detail": conflict["detail"],
            }
            database.execute(
                "INSERT INTO hard_conflicts VALUES(?,?,?,?,?)",
                (
                    run_id,
                    candidate_id,
                    conflict["conflict_type"],
                    base.canonical_json(conflict["detail"]),
                    base._record_sha(conflict_payload),
                ),
            )
        for flag in sorted(result["policy_flags"], key=lambda row: str(row["flag_type"])):
            flag_payload = {
                "candidate_id": candidate_id,
                "flag_type": flag["flag_type"],
                "detail": flag["detail"],
            }
            database.execute(
                "INSERT INTO candidate_policy_flags VALUES(?,?,?,?,?)",
                (
                    run_id,
                    candidate_id,
                    flag["flag_type"],
                    base.canonical_json(flag["detail"]),
                    base._record_sha(flag_payload),
                ),
            )
        queue_reasons = human_review_queue_reasons(result)
        if queue_reasons:
            queue_payload = {
                "candidate_id": candidate_id,
                "proposal": result["proposal"],
                "auto_merge_eligible": result["auto_merge_eligible"],
                "queue_reasons": queue_reasons,
                "positive_families": _positive_families(result),
                "anchor_families": _anchor_families(result),
                "policy_flags": _policy_flag_types(result),
                "evidence_strength_band": evidence_strength_band(result),
            }
            database.execute(
                "INSERT INTO human_review_queue VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    candidate_id,
                    queue_payload["proposal"],
                    queue_payload["auto_merge_eligible"],
                    base.canonical_json(queue_reasons),
                    base.canonical_json(queue_payload["positive_families"]),
                    base.canonical_json(queue_payload["anchor_families"]),
                    base.canonical_json(queue_payload["policy_flags"]),
                    queue_payload["evidence_strength_band"],
                    base._record_sha(queue_payload),
                ),
            )

    for group in fanout_groups:
        group_payload = {
            "hub_source": group["hub_source"],
            "hub_source_building_id": group["hub_source_building_id"],
            "pre_guard_degree": group["pre_guard_degree"],
            "status": group["status"],
            "incident_candidate_ids": group["incident_candidate_ids"],
            "opponent_source_building_ids": group["opponent_source_building_ids"],
            "unrelated_opponent_pairs": group["unrelated_opponent_pairs"],
            "detail": group["detail"],
        }
        database.execute(
            "INSERT INTO fanout_groups VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                group["hub_source"],
                group["hub_source_building_id"],
                group["pre_guard_degree"],
                group["status"],
                base.canonical_json(group["incident_candidate_ids"]),
                base.canonical_json(group["opponent_source_building_ids"]),
                base.canonical_json(group["unrelated_opponent_pairs"]),
                base.canonical_json(group["detail"]),
                base._record_sha(group_payload),
            ),
        )
    return evaluated, fanout_groups


def _load_v3_maps(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    candidates = {
        str(row["candidate_id"]): dict(row)
        for row in connection.execute("SELECT * FROM candidate_pairs WHERE run_id=?", (run_id,))
    }
    conflicts: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        "SELECT candidate_id,conflict_type FROM hard_conflicts WHERE run_id=?", (run_id,)
    ):
        conflicts[str(row[0])].add(str(row[1]))
    flags: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        "SELECT candidate_id,flag_type FROM candidate_policy_flags WHERE run_id=?", (run_id,)
    ):
        flags[str(row[0])].add(str(row[1]))
    families: dict[str, dict[str, str]] = defaultdict(dict)
    for row in connection.execute(
        "SELECT candidate_id,family,status FROM evidence_families WHERE run_id=?", (run_id,)
    ):
        families[str(row[0])][str(row[1])] = str(row[2])
    return {
        "candidates": candidates,
        "conflicts": conflicts,
        "flags": flags,
        "families": families,
    }


def _insert_transitions(
    database: sqlite3.Connection,
    run_id: str,
    v3: sqlite3.Connection,
    v3_run_id: str,
    evaluated: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    old_maps = _load_v3_maps(v3, v3_run_id)
    if set(old_maps["candidates"]) != set(evaluated):
        raise IdentityCandidateV4Error("v3/v4 candidate universe drift")
    transitions: list[dict[str, Any]] = []
    columns = (
        "run_id", "candidate_id", "left_source_building_id", "right_source_building_id",
        "v3_proposal", "v4_proposal", "v3_auto_merge_eligible", "v4_auto_merge_eligible",
        "v3_positive_family_count", "v4_positive_family_count",
        "v3_negative_family_count", "v4_negative_family_count",
        "v3_hard_conflicts_json", "v4_hard_conflicts_json",
        "v3_policy_flags_json", "v4_policy_flags_json",
        "r1_before_json", "r1_after_json", "r1_changed",
        "r2_before_json", "r2_after_json", "r2_changed",
        "r3_before_json", "r3_after_json", "r3_changed",
        "v3_human_review_queue", "v4_human_review_queue",
        "r4_queue_reasons_json", "evidence_strength_band",
        "proposal_changed", "eligibility_changed", "decision_changed",
        "reasons_json", "record_sha256",
    )
    statement = (
        f"INSERT INTO candidate_transitions({','.join(columns)}) VALUES("
        + ",".join("?" for _ in columns)
        + ")"
    )
    for candidate_id in sorted(evaluated):
        old = old_maps["candidates"][candidate_id]
        new = evaluated[candidate_id]
        old_conflicts = sorted(old_maps["conflicts"].get(candidate_id, set()))
        new_conflicts = _conflict_types(new)
        old_flags = sorted(old_maps["flags"].get(candidate_id, set()))
        new_flags = _policy_flag_types(new)
        r1_before = dict(new["r1_before_snapshot"])
        r1_after = dict(new["r1_after_snapshot"])
        r2_before = dict(new["r2_before_snapshot"])
        r2_after = dict(new["r2_after_snapshot"])
        r3_before = dict(new["r3_before_snapshot"])
        r3_after = dict(new["r3_after_snapshot"])
        v3_queue = int(str(old["proposal"]) == "merge_candidate")
        queue_reasons = human_review_queue_reasons(new)
        v4_queue = int(bool(queue_reasons))
        proposal_changed = int(str(old["proposal"]) != str(new["proposal"]))
        eligibility_changed = int(
            int(old["auto_merge_eligible"]) != int(new["auto_merge_eligible"])
        )
        decision_changed = int(
            proposal_changed
            or eligibility_changed
            or int(old["positive_family_count"]) != int(new["positive_family_count"])
            or int(old["negative_family_count"]) != int(new["negative_family_count"])
            or old_conflicts != new_conflicts
            or old_flags != new_flags
            or v3_queue != v4_queue
        )
        reasons: list[str] = []
        if new["r1_changed"]:
            reasons.append("R1_year_changed")
        if new["r2_changed"]:
            reasons.append("R2_phash_changed")
        if new["r3_changed"]:
            reasons.append("R3_fanout_changed")
        if v3_queue != v4_queue:
            reasons.append("R4_review_queue_changed")
        if proposal_changed:
            reasons.append("proposal_changed")
        if eligibility_changed:
            reasons.append("auto_merge_eligibility_changed")
        payload = {
            "candidate_id": candidate_id,
            "left_source_building_id": str(new["left_id"]),
            "right_source_building_id": str(new["right_id"]),
            "v3_proposal": str(old["proposal"]),
            "v4_proposal": str(new["proposal"]),
            "v3_auto_merge_eligible": int(old["auto_merge_eligible"]),
            "v4_auto_merge_eligible": int(new["auto_merge_eligible"]),
            "v3_positive_family_count": int(old["positive_family_count"]),
            "v4_positive_family_count": int(new["positive_family_count"]),
            "v3_negative_family_count": int(old["negative_family_count"]),
            "v4_negative_family_count": int(new["negative_family_count"]),
            "v3_hard_conflicts": old_conflicts,
            "v4_hard_conflicts": new_conflicts,
            "v3_policy_flags": old_flags,
            "v4_policy_flags": new_flags,
            "r1_before": r1_before,
            "r1_after": r1_after,
            "r1_changed": int(new["r1_changed"]),
            "r2_before": r2_before,
            "r2_after": r2_after,
            "r2_changed": int(new["r2_changed"]),
            "r3_before": r3_before,
            "r3_after": r3_after,
            "r3_changed": int(new["r3_changed"]),
            "v3_human_review_queue": v3_queue,
            "v4_human_review_queue": v4_queue,
            "r4_queue_reasons": queue_reasons,
            "evidence_strength_band": evidence_strength_band(new),
            "proposal_changed": proposal_changed,
            "eligibility_changed": eligibility_changed,
            "decision_changed": decision_changed,
            "reasons": reasons,
        }
        values = (
            run_id,
            payload["candidate_id"],
            payload["left_source_building_id"],
            payload["right_source_building_id"],
            payload["v3_proposal"],
            payload["v4_proposal"],
            payload["v3_auto_merge_eligible"],
            payload["v4_auto_merge_eligible"],
            payload["v3_positive_family_count"],
            payload["v4_positive_family_count"],
            payload["v3_negative_family_count"],
            payload["v4_negative_family_count"],
            base.canonical_json(old_conflicts),
            base.canonical_json(new_conflicts),
            base.canonical_json(old_flags),
            base.canonical_json(new_flags),
            base.canonical_json(r1_before),
            base.canonical_json(r1_after),
            payload["r1_changed"],
            base.canonical_json(r2_before),
            base.canonical_json(r2_after),
            payload["r2_changed"],
            base.canonical_json(r3_before),
            base.canonical_json(r3_after),
            payload["r3_changed"],
            v3_queue,
            v4_queue,
            base.canonical_json(queue_reasons),
            payload["evidence_strength_band"],
            proposal_changed,
            eligibility_changed,
            decision_changed,
            base.canonical_json(reasons),
            base._record_sha(payload),
        )
        database.execute(statement, values)
        transitions.append(payload)
    return transitions


def _verify_n100_gold_chain(root: Path, n100: sqlite3.Connection) -> str:
    manifest = json.loads((root / "data/reports/vnext_identity_n100_v1_manifest.json").read_text(encoding="utf-8"))
    locked = json.loads(
        (root / "data/reports/vnext_identity_n100_v1_adjudication_locked.json").read_text(
            encoding="utf-8"
        )
    )
    run_rows = n100.execute("SELECT run_id FROM runs").fetchall()
    if len(run_rows) != 1:
        raise IdentityCandidateV4Error("N100 gold artifact must contain exactly one run")
    run_id = str(run_rows[0][0])
    expected = {
        "run_id": run_id,
        "status": "PASS",
        "sidecar_sha": INPUT_PINS["n100_gold_artifact"][2],
        "locked_sha": INPUT_PINS["n100_locked_adjudication"][2],
        "v3_sha": INPUT_PINS["v3_candidate_artifact"][2],
    }
    actual = {
        "run_id": str(manifest.get("run_id")),
        "status": str(manifest.get("status")),
        "sidecar_sha": str(manifest.get("sidecar", {}).get("sha256")),
        "locked_sha": str(manifest.get("artifacts", {}).get("locked_adjudication", {}).get("sha256")),
        "v3_sha": str(manifest.get("v3_immutable", {}).get("sha256_after")),
    }
    if actual != expected:
        raise IdentityCandidateV4Error(f"N100 manifest chain mismatch: {actual}")
    review_rows = manifest.get("review_rows", {})
    if review_rows != {"adjudicated": 105, "carry": 5, "r1": 105, "r2": 105}:
        raise IdentityCandidateV4Error("N100 manifest review-row accounting mismatch")
    if str(locked.get("run_id")) != run_id or int(locked.get("fresh_row_count", -1)) != 105:
        raise IdentityCandidateV4Error("N100 locked adjudication header mismatch")
    locked_items = {str(row["sample_id"]): row for row in locked.get("items", [])}
    if len(locked_items) != 105:
        raise IdentityCandidateV4Error("N100 locked adjudication must contain 105 unique rows")
    database_items = {
        str(row["sample_id"]): dict(row)
        for row in n100.execute(
            "SELECT a.*,s.candidate_id FROM adjudications a JOIN sample_pairs s "
            "ON s.run_id=a.run_id AND s.sample_id=a.sample_id WHERE a.run_id=?",
            (run_id,),
        )
    }
    if set(locked_items) != set(database_items):
        raise IdentityCandidateV4Error("N100 locked adjudication/DB sample set mismatch")
    compare_fields = (
        "candidate_id",
        "r1_label",
        "r2_label",
        "adjudicator_label",
        "final_gold_label",
        "reviewer_agreement",
        "unresolved_disagreement",
        "rationale",
        "adjudicator",
        "adjudicated_at",
    )
    for sample_id in sorted(locked_items):
        locked_row = locked_items[sample_id]
        database_row = database_items[sample_id]
        for field in compare_fields:
            if locked_row.get(field) != database_row.get(field):
                raise IdentityCandidateV4Error(
                    f"N100 locked adjudication/DB mismatch: {sample_id}:{field}"
                )
    return run_id


def _load_regression_bindings(
    root: Path, n100: sqlite3.Connection, n100_run_id: str
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in n100.execute(
            "SELECT s.sample_id,s.candidate_id,s.stratum,s.sample_origin,s.record_sha256 AS sample_sha,"
            "a.final_gold_label AS adjudicated_label,a.record_sha256 AS adjudicated_sha,"
            "g.final_gold_label AS carry_label,g.record_sha256 AS carry_sha "
            "FROM sample_pairs s "
            "LEFT JOIN adjudications a ON a.run_id=s.run_id AND a.sample_id=s.sample_id "
            "LEFT JOIN carry_forward_gold g ON g.run_id=s.run_id AND g.sample_id=s.sample_id "
            "WHERE s.run_id=? AND s.cohort='B' ORDER BY s.sample_rank",
            (n100_run_id,),
        )
    ]
    for row in rows:
        row["gold_label"] = row["adjudicated_label"] or row["carry_label"]
        row["gold_sha"] = row["adjudicated_sha"] or row["carry_sha"]
        if not row["gold_label"] or not row["gold_sha"]:
            raise IdentityCandidateV4Error("N100 B regression row has no locked gold")

    selections = {
        "b4": [row for row in rows if row["stratum"] == "B4_high_name_reject"],
        "b2": [
            row
            for row in rows
            if row["stratum"] == "B2_phash_veto"
            and row["sample_origin"] == "fresh"
            and row["gold_label"] == "different"
        ],
        "b1": [
            row
            for row in rows
            if row["stratum"] == "B1_year_gate" and row["sample_origin"] == "fresh"
        ],
        "b3": [
            row
            for row in rows
            if row["stratum"] == "B3_fanout_defer" and row["gold_label"] == "same"
        ],
    }
    actual_counts = {key: len(value) for key, value in selections.items()}
    if actual_counts != {"b4": 10, "b2": 4, "b1": 9, "b3": 6}:
        raise IdentityCandidateV4Error(f"N100 regression population drift: {actual_counts}")
    if Counter(row["gold_label"] for row in selections["b4"]) != {
        "different": 6,
        "insufficient": 4,
    }:
        raise IdentityCandidateV4Error("B4 gold-label distribution drift")

    bindings: list[dict[str, Any]] = []

    def add_n100(
        gate_name: str,
        subgroup: str,
        selected: Sequence[Mapping[str, Any]],
        expected: Mapping[str, Any],
    ) -> None:
        for row in selected:
            candidate_id = str(row["candidate_id"])
            bindings.append(
                {
                    "case_id": "rgr_"
                    + base.stable_hash(POLICY_VERSION, gate_name, subgroup, candidate_id)[:32],
                    "gate_name": gate_name,
                    "subgroup": subgroup,
                    "candidate_id": candidate_id,
                    "source_artifact_role": "n100_gold_artifact",
                    "source_sample_id": str(row["sample_id"]),
                    "source_stratum": str(row["stratum"]),
                    "sample_origin": str(row["sample_origin"]),
                    "gold_label": str(row["gold_label"]),
                    "sample_record_sha256": str(row["sample_sha"]),
                    "gold_record_sha256": str(row["gold_sha"]),
                    "expected": dict(expected),
                }
            )

    add_n100(
        "b4_name_only_reject", "B4", selections["b4"], {"proposal": "reject"}
    )
    add_n100(
        "b2_different_phash_hard_conflict",
        "B2_different",
        selections["b2"],
        {"hard_conflict": "verified_phash_veto"},
    )
    add_n100(
        "b1_b3_human_review_queue", "B1_year", selections["b1"], {"human_review_queue": True}
    )
    add_n100(
        "b1_b3_human_review_queue", "B3_same", selections["b3"], {"human_review_queue": True}
    )
    for candidate_id in WP22_REGRESSION_IDS:
        bindings.append(
            {
                "case_id": "rgr_"
                + base.stable_hash(
                    POLICY_VERSION, "wp22_country_conflict_reject", "WP22", candidate_id
                )[:32],
                "gate_name": "wp22_country_conflict_reject",
                "subgroup": "WP22_country",
                "candidate_id": candidate_id,
                "source_artifact_role": "v3_candidate_artifact",
                "source_sample_id": None,
                "source_stratum": None,
                "sample_origin": None,
                "gold_label": "different",
                "sample_record_sha256": None,
                "gold_record_sha256": None,
                "expected": {
                    "proposal": "reject",
                    "hard_conflict": "country_site_incompatible",
                },
            }
        )
    bindings.sort(key=lambda row: (row["gate_name"], row["subgroup"], row["candidate_id"]))
    gate_counts = Counter(str(row["gate_name"]) for row in bindings)
    if dict(gate_counts) != EXPECTED_REGRESSION_GATE_COUNTS:
        raise IdentityCandidateV4Error(f"regression binding count drift: {dict(gate_counts)}")
    return bindings


def _insert_and_evaluate_regressions(
    database: sqlite3.Connection,
    run_id: str,
    bindings: Sequence[Mapping[str, Any]],
    evaluated: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for binding in bindings:
        candidate_id = str(binding["candidate_id"])
        if candidate_id not in evaluated:
            raise IdentityCandidateV4Error(f"regression candidate absent: {candidate_id}")
        binding_payload = {key: binding[key] for key in binding}
        database.execute(
            "INSERT INTO regression_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                binding["case_id"],
                binding["gate_name"],
                binding["subgroup"],
                candidate_id,
                binding["source_artifact_role"],
                binding["source_sample_id"],
                binding["source_stratum"],
                binding["sample_origin"],
                binding["gold_label"],
                binding["sample_record_sha256"],
                binding["gold_record_sha256"],
                base.canonical_json(binding["expected"]),
                base._record_sha(binding_payload),
            ),
        )
        row = evaluated[candidate_id]
        conflicts = _conflict_types(row)
        flags = _policy_flag_types(row)
        queue = bool(human_review_queue_reasons(row))
        expected = binding["expected"]
        passed = True
        checks: dict[str, Any] = {}
        if "proposal" in expected:
            checks["proposal"] = {
                "expected": expected["proposal"],
                "actual": row["proposal"],
            }
            passed = passed and row["proposal"] == expected["proposal"]
        if "hard_conflict" in expected:
            checks["hard_conflict"] = {
                "expected": expected["hard_conflict"],
                "actual": conflicts,
            }
            passed = passed and expected["hard_conflict"] in conflicts
        if "human_review_queue" in expected:
            checks["human_review_queue"] = {
                "expected": bool(expected["human_review_queue"]),
                "actual": queue,
            }
            passed = passed and queue is bool(expected["human_review_queue"])
        detail = {
            "gate_name": binding["gate_name"],
            "subgroup": binding["subgroup"],
            "checks": checks,
        }
        result_payload = {
            "case_id": binding["case_id"],
            "candidate_id": candidate_id,
            "actual_proposal": row["proposal"],
            "actual_auto_merge_eligible": int(row["auto_merge_eligible"]),
            "actual_hard_conflicts": conflicts,
            "actual_policy_flags": flags,
            "actual_human_review_queue": int(queue),
            "passed": int(passed),
            "detail": detail,
        }
        database.execute(
            "INSERT INTO regression_results VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                result_payload["case_id"],
                candidate_id,
                result_payload["actual_proposal"],
                result_payload["actual_auto_merge_eligible"],
                base.canonical_json(conflicts),
                base.canonical_json(flags),
                result_payload["actual_human_review_queue"],
                result_payload["passed"],
                base.canonical_json(detail),
                base._record_sha(result_payload),
            ),
        )
        results.append(result_payload)

    gate_results: dict[str, dict[str, Any]] = {}
    for gate_name in REGRESSION_GATE_NAMES:
        gate_rows = [
            row
            for row in results
            if next(
                binding["gate_name"]
                for binding in bindings
                if binding["case_id"] == row["case_id"]
            )
            == gate_name
        ]
        expected_count = EXPECTED_REGRESSION_GATE_COUNTS[gate_name]
        passed_count = sum(int(row["passed"]) for row in gate_rows)
        failed_count = len(gate_rows) - passed_count
        status = "PASS" if len(gate_rows) == expected_count and failed_count == 0 else "FAIL"
        detail = {
            "gate_name": gate_name,
            "case_ids": sorted(row["case_id"] for row in gate_rows),
            "failed_case_ids": sorted(row["case_id"] for row in gate_rows if not row["passed"]),
        }
        gate_payload = {
            "gate_name": gate_name,
            "expected_case_count": expected_count,
            "actual_case_count": len(gate_rows),
            "passed_case_count": passed_count,
            "failed_case_count": failed_count,
            "status": status,
            "detail": detail,
        }
        database.execute(
            "INSERT INTO regression_gate_results VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                gate_name,
                expected_count,
                len(gate_rows),
                passed_count,
                failed_count,
                status,
                base.canonical_json(detail),
                base._record_sha(gate_payload),
            ),
        )
        gate_results[gate_name] = gate_payload
    if any(row["status"] != "PASS" for row in gate_results.values()):
        failed = [name for name, row in gate_results.items() if row["status"] != "PASS"]
        raise IdentityCandidateV4Error("WP2.4 regression gate failure: " + ",".join(failed))
    return gate_results


def full_logical_sha256(
    database: sqlite3.Connection, run_id: str, stage_a_logical: str
) -> str:
    digest = hashlib.sha256()
    digest.update((FULL_LOGICAL_VERSION + "\n" + stage_a_logical + "\n").encode("utf-8"))
    for table, order in FULL_LOGICAL_TABLES:
        digest.update((table + "\n").encode("utf-8"))
        for row in database.execute(
            f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)
        ):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _rule_impact(
    transitions: Sequence[Mapping[str, Any]], rule: str
) -> dict[str, Any]:
    changed_key = f"{rule}_changed"
    before_key = f"{rule}_before"
    after_key = f"{rule}_after"
    changed = [row for row in transitions if row[changed_key]]
    proposal_transitions = Counter(
        f"{row[before_key]['proposal']}->{row[after_key]['proposal']}" for row in changed
    )
    eligibility_transitions = Counter(
        f"{row[before_key]['auto_merge_eligible']}->{row[after_key]['auto_merge_eligible']}"
        for row in changed
    )
    added_conflicts: Counter[str] = Counter()
    removed_conflicts: Counter[str] = Counter()
    added_flags: Counter[str] = Counter()
    removed_flags: Counter[str] = Counter()
    for row in changed:
        before = row[before_key]
        after = row[after_key]
        added_conflicts.update(set(after["hard_conflicts"]) - set(before["hard_conflicts"]))
        removed_conflicts.update(set(before["hard_conflicts"]) - set(after["hard_conflicts"]))
        added_flags.update(set(after["policy_flags"]) - set(before["policy_flags"]))
        removed_flags.update(set(before["policy_flags"]) - set(after["policy_flags"]))
    return {
        "changed_count": len(changed),
        "proposal_transition_counts": dict(sorted(proposal_transitions.items())),
        "eligibility_transition_counts": dict(sorted(eligibility_transitions.items())),
        "hard_conflicts_added": dict(sorted(added_conflicts.items())),
        "hard_conflicts_removed": dict(sorted(removed_conflicts.items())),
        "policy_flags_added": dict(sorted(added_flags.items())),
        "policy_flags_removed": dict(sorted(removed_flags.items())),
        "changed_candidate_ids": sorted(str(row["candidate_id"]) for row in changed),
    }


def _transition_report_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"],
        "left_source_building_id": row["left_source_building_id"],
        "right_source_building_id": row["right_source_building_id"],
        "v3_proposal": row["v3_proposal"],
        "v4_proposal": row["v4_proposal"],
        "v3_auto_merge_eligible": row["v3_auto_merge_eligible"],
        "v4_auto_merge_eligible": row["v4_auto_merge_eligible"],
        "v3_hard_conflicts": row["v3_hard_conflicts"],
        "v4_hard_conflicts": row["v4_hard_conflicts"],
        "v3_policy_flags": row["v3_policy_flags"],
        "v4_policy_flags": row["v4_policy_flags"],
        "r1_changed": row["r1_changed"],
        "r2_changed": row["r2_changed"],
        "r3_changed": row["r3_changed"],
        "v3_human_review_queue": row["v3_human_review_queue"],
        "v4_human_review_queue": row["v4_human_review_queue"],
        "r4_queue_reasons": row["r4_queue_reasons"],
        "evidence_strength_band": row["evidence_strength_band"],
        "reasons": row["reasons"],
    }


def _assert_policy_invariants(
    database: sqlite3.Connection, run_id: str, expected_candidate_count: int
) -> None:
    scalar = lambda sql: int(database.execute(sql, (run_id,)).fetchone()[0])
    if scalar("SELECT count(*) FROM candidate_pairs WHERE run_id=?") != expected_candidate_count:
        raise IdentityCandidateV4Error("candidate population accounting mismatch")
    if scalar("SELECT count(*) FROM sample_neighbourhoods WHERE run_id=?") != 0:
        raise IdentityCandidateV4Error("WP2.4 must not create a fresh N10")
    if scalar(
        "SELECT count(*) FROM hard_conflicts WHERE run_id=? AND conflict_type IN "
        "('matcher_architect_year_gate_block','fanout_conflict')"
    ):
        raise IdentityCandidateV4Error("retired R1/R3 hard conflict remains")
    if scalar(
        "SELECT count(*) FROM candidate_pairs p WHERE p.run_id=? AND p.auto_merge_eligible=1 "
        "AND (p.proposal<>'merge_candidate' OR p.positive_family_count<2 OR p.hard_conflict_count<>0)"
    ):
        raise IdentityCandidateV4Error("auto-merge structural invariant violation")
    if scalar(
        "SELECT count(*) FROM candidate_pairs p WHERE p.run_id=? AND p.auto_merge_eligible=1 "
        "AND NOT EXISTS(SELECT 1 FROM evidence_families e WHERE e.run_id=p.run_id "
        "AND e.candidate_id=p.candidate_id AND e.status='positive' "
        "AND e.family IN ('name_alias_identity','verified_pixel_image_overlap'))"
    ):
        raise IdentityCandidateV4Error("auto-merge anchor invariant violation")
    if scalar(
        "SELECT count(*) FROM candidate_pairs p WHERE p.run_id=? AND p.proposal='merge_candidate' "
        "AND NOT EXISTS(SELECT 1 FROM human_review_queue q WHERE q.run_id=p.run_id "
        "AND q.candidate_id=p.candidate_id)"
    ):
        raise IdentityCandidateV4Error("R4 omitted a merge_candidate from human review")
    if scalar(
        "SELECT count(*) FROM candidate_pairs p WHERE p.run_id=? AND p.proposal='defer' "
        "AND p.positive_family_count>=2 AND EXISTS(SELECT 1 FROM evidence_families e "
        "WHERE e.run_id=p.run_id AND e.candidate_id=p.candidate_id AND e.status='positive' "
        "AND e.family IN ('name_alias_identity','verified_pixel_image_overlap')) "
        "AND NOT EXISTS(SELECT 1 FROM human_review_queue q WHERE q.run_id=p.run_id "
        "AND q.candidate_id=p.candidate_id)"
    ):
        raise IdentityCandidateV4Error("R4 omitted an anchored two-family defer")
    if scalar(
        "SELECT count(*) FROM candidate_policy_flags f WHERE f.run_id=? "
        "AND f.flag_type='phash_veto_review_required' AND ("
        "EXISTS(SELECT 1 FROM hard_conflicts h WHERE h.run_id=f.run_id AND h.candidate_id=f.candidate_id "
        "AND h.conflict_type='verified_phash_veto') OR "
        "EXISTS(SELECT 1 FROM candidate_pairs p WHERE p.run_id=f.run_id AND p.candidate_id=f.candidate_id "
        "AND p.auto_merge_eligible<>0))"
    ):
        raise IdentityCandidateV4Error("R2 veto downgrade invariant violation")
    if scalar(
        "SELECT count(*) FROM hard_conflicts h JOIN candidate_pairs p "
        "ON p.run_id=h.run_id AND p.candidate_id=h.candidate_id WHERE h.run_id=? "
        "AND h.conflict_type='project_phase_incompatible' AND (p.year_gap<6 OR "
        "EXISTS(SELECT 1 FROM evidence_families e WHERE e.run_id=p.run_id "
        "AND e.candidate_id=p.candidate_id AND e.status='positive' "
        "AND e.family IN ('architect_firm_identity','verified_pixel_image_overlap')))"
    ):
        raise IdentityCandidateV4Error("R1 conditional phase conflict invariant violation")
    if scalar(
        "SELECT count(*) FROM candidate_pairs p WHERE p.run_id=? AND p.year_gap>=6 "
        "AND NOT EXISTS(SELECT 1 FROM evidence_families e WHERE e.run_id=p.run_id "
        "AND e.candidate_id=p.candidate_id AND e.status='positive' "
        "AND e.family IN ('architect_firm_identity','verified_pixel_image_overlap')) "
        "AND NOT EXISTS(SELECT 1 FROM hard_conflicts h WHERE h.run_id=p.run_id "
        "AND h.candidate_id=p.candidate_id AND h.conflict_type='project_phase_incompatible')"
    ):
        raise IdentityCandidateV4Error("R1 required phase conflict is missing")
    if scalar(
        "SELECT count(*) FROM candidate_pairs p JOIN evidence_families e "
        "ON e.run_id=p.run_id AND e.candidate_id=p.candidate_id "
        "WHERE p.run_id=? AND e.family='year_phase_compatibility' AND NOT ("
        "(p.year_gap IS NULL AND e.status='unknown') OR "
        "(p.year_gap<=1 AND e.status='positive') OR "
        "(p.year_gap BETWEEN 2 AND 5 AND e.status='unknown') OR "
        "(p.year_gap>=6 AND e.status='negative'))"
    ):
        raise IdentityCandidateV4Error("R1 year-family band invariant violation")
    if scalar(
        "SELECT count(*) FROM candidate_policy_flags f JOIN candidate_pairs p "
        "ON p.run_id=f.run_id AND p.candidate_id=f.candidate_id WHERE f.run_id=? "
        "AND f.flag_type='fanout_review_required' AND "
        "(p.proposal<>'merge_candidate' OR p.auto_merge_eligible<>0 OR p.hard_conflict_count<>0)"
    ):
        raise IdentityCandidateV4Error("R3 review fanout invariant violation")


def build_identity_candidates_v4(
    *,
    repo_root: Path,
    output_path: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST,
    report_path: Path = DEFAULT_REPORT,
    gate_path: Path = DEFAULT_GATE,
    verify_full_hashes: bool = True,
) -> BuildResult:
    """Replay WP2.4 Stage A offline and emit the volume and regression artifacts."""
    started = time.monotonic()
    root = repo_root.resolve()
    output = output_path if output_path.is_absolute() else root / output_path
    manifest = manifest_path if manifest_path.is_absolute() else root / manifest_path
    report = report_path if report_path.is_absolute() else root / report_path
    gate = gate_path if gate_path.is_absolute() else root / gate_path
    for path in (output, manifest, report, gate):
        if path.exists():
            raise IdentityCandidateV4Error(f"refusing to clobber existing artifact: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".building")
    if temporary.exists():
        raise IdentityCandidateV4Error(f"stale temporary artifact: {temporary}")

    pins = _verify_pins(root, verify_full_hashes)
    normalizer_sha = base.canonical_sha256(NORMALIZER_SPEC)
    policy_sha = base.canonical_sha256(POLICY_SPEC)
    run_material = {
        "builder_version": BUILDER_VERSION,
        "normalizer_sha256": normalizer_sha,
        "policy_sha256": policy_sha,
        "sample_version": SAMPLE_VERSION,
        "inputs": {role: row["sha256_before"] for role, row in sorted(pins.items())},
    }
    run_id = "wp24-" + base.canonical_sha256(run_material)[:24]
    database: sqlite3.Connection | None = None
    connections: list[sqlite3.Connection] = []
    try:
        divisare = base._open_immutable(root / base.DEFAULT_DIVISARE)
        architizer = base._open_immutable(root / base.DEFAULT_ARCHITIZER)
        e2 = base._open_immutable(root / base.DEFAULT_E2)
        v3 = base._open_immutable(root / DEFAULT_V3)
        n100 = base._open_immutable(root / DEFAULT_N100)
        connections = [divisare, architizer, e2, v3, n100]
        v3_run_rows = v3.execute("SELECT run_id FROM runs").fetchall()
        if len(v3_run_rows) != 1:
            raise IdentityCandidateV4Error("v3 comparison artifact must contain one run")
        v3_run_id = str(v3_run_rows[0][0])
        v3_candidate_count = int(
            v3.execute(
                "SELECT count(*) FROM candidate_pairs WHERE run_id=?", (v3_run_id,)
            ).fetchone()[0]
        )
        v3_proposals = {
            str(row[0]): int(row[1])
            for row in v3.execute(
                "SELECT proposal,count(*) FROM candidate_pairs WHERE run_id=? GROUP BY proposal",
                (v3_run_id,),
            )
        }
        v3_eligible = int(
            v3.execute(
                "SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1",
                (v3_run_id,),
            ).fetchone()[0]
        )
        n100_run_id = _verify_n100_gold_chain(root, n100)
        regression_bindings = _load_regression_bindings(root, n100, n100_run_id)

        buildings = base.load_source_buildings(divisare, architizer, e2)
        _apply_country_normalization(buildings)
        image_stats = base.load_image_stats(e2)
        universe, e2_summaries = base.build_candidate_universe(buildings, e2)

        database = sqlite3.connect(temporary)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(V4_BASE_SCHEMA_SQL)
        database.executescript(V4_SCHEMA_SQL)
        database.execute(
            "INSERT INTO runs(run_id,contract_version,builder_version,blocker_version,policy_version,"
            "normalizer_version,sample_version,sample_seed,status,started_at,normalizer_sha256,policy_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                CONTRACT_VERSION,
                BUILDER_VERSION,
                base.BLOCKER_VERSION,
                POLICY_VERSION,
                NORMALIZER_VERSION,
                SAMPLE_VERSION,
                SAMPLE_SEED,
                "building",
                base.utc_now(),
                normalizer_sha,
                policy_sha,
            ),
        )
        for role, row in sorted(pins.items()):
            pin_payload = {
                "role": role,
                "path": row["path"],
                "size_bytes": row["size_bytes"],
                "sha256_before": row["sha256_before"],
                "usage": row["usage"],
            }
            database.execute(
                "INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    role,
                    row["path"],
                    row["size_bytes"],
                    row["sha256_before"],
                    None,
                    row["usage"],
                    base._record_sha(pin_payload),
                ),
            )
        base._insert_source_rows(database, run_id, buildings, image_stats)
        _insert_country_rows(database, run_id, buildings)
        evaluated, fanout_groups = _insert_candidates(
            database, run_id, buildings, image_stats, universe, e2_summaries
        )
        direct_count = base._insert_direct_image_evidence(
            database, run_id, e2, e2_summaries
        )
        transitions = _insert_transitions(database, run_id, v3, v3_run_id, evaluated)
        gate_results = _insert_and_evaluate_regressions(
            database, run_id, regression_bindings, evaluated
        )
        database.commit()

        if len(buildings) != base.EXPECTED_SOURCE_TOTAL or len(evaluated) != v3_candidate_count:
            raise IdentityCandidateV4Error("Stage A population accounting mismatch")
        _assert_policy_invariants(database, run_id, v3_candidate_count)
        for value in ("Senegal", "Republic of Senegal", "Ecuador", "Republic of Ecuador"):
            if classify_country(value).status != "known":
                raise IdentityCandidateV4Error("R5 known-country regression failed: " + value)
        if classify_country("Georgia").status != "ambiguous":
            raise IdentityCandidateV4Error("R5 Georgia ambiguity regression failed")

        for connection in connections:
            connection.close()
        connections = []
        for role, (relative, _size, expected, _usage) in INPUT_PINS.items():
            actual = base.sha256_file(root / relative) if verify_full_hashes else expected
            if actual != pins[role]["sha256_before"]:
                raise IdentityCandidateV4Error("input changed during build: " + role)
            pins[role]["sha256_after"] = actual
            row = pins[role]
            pin_payload = {
                "role": role,
                "path": row["path"],
                "size_bytes": row["size_bytes"],
                "sha256_before": row["sha256_before"],
                "sha256_after": actual,
                "usage": row["usage"],
            }
            database.execute(
                "UPDATE input_pins SET sha256_after=?,record_sha256=? WHERE run_id=? AND role=?",
                (actual, base._record_sha(pin_payload), run_id, role),
            )

        proposal_counts = Counter(str(row["proposal"]) for row in evaluated.values())
        merge_candidate_count = proposal_counts["merge_candidate"]
        eligible_count = sum(int(row["auto_merge_eligible"]) for row in evaluated.values())
        queue_rows = [row for row in evaluated.values() if human_review_queue_reasons(row)]
        queue_count = len(queue_rows)
        changed_transitions = [row for row in transitions if row["decision_changed"]]
        fanout_status_counts = Counter(str(row["status"]) for row in fanout_groups)
        policy_flag_counts = Counter(
            flag for row in evaluated.values() for flag in _policy_flag_types(row)
        )
        queue_band_counts = Counter(evidence_strength_band(row) for row in queue_rows)
        queue_proposal_counts = Counter(str(row["proposal"]) for row in queue_rows)
        queue_flag_counts = Counter(
            flag for row in queue_rows for flag in _policy_flag_types(row)
        )
        transition_counts = Counter(
            f"{row['v3_proposal']}->{row['v4_proposal']}" for row in transitions
        )
        eligibility_transition_counts = Counter(
            f"{row['v3_auto_merge_eligible']}->{row['v4_auto_merge_eligible']}"
            for row in transitions
        )
        rule_impacts = {
            rule.upper(): _rule_impact(transitions, rule) for rule in ("r1", "r2", "r3")
        }

        metric_rows = (
            ("source_buildings", len(buildings)),
            ("candidate_pairs", len(evaluated)),
            ("direct_image_evidence", direct_count),
            ("merge_candidates", merge_candidate_count),
            ("auto_merge_eligible", eligible_count),
            ("human_review_queue", queue_count),
            ("decision_changed_pairs", len(changed_transitions)),
            ("r1_changed_pairs", rule_impacts["R1"]["changed_count"]),
            ("r2_changed_pairs", rule_impacts["R2"]["changed_count"]),
            ("r3_changed_pairs", rule_impacts["R3"]["changed_count"]),
            ("regression_cases", len(regression_bindings)),
            ("regression_gates_passed", 4),
            ("n10_sample", 0),
            ("fresh_n10_runs", 0),
            ("network_requests", 0),
            ("vision_requests", 0),
            ("llm_requests", 0),
            ("legacy_snapshot_reads", 0),
            ("merge_executions", 0),
            ("vnx_id_issuances", 0),
        )
        for name, value in metric_rows:
            database.execute(
                "INSERT INTO metrics VALUES(?,?,?,?)", (run_id, "stage_a", name, value)
            )
        logical = base.stage_a_logical_sha256(database, run_id)
        full_logical = full_logical_sha256(database, run_id, logical)
        database.execute(
            "UPDATE runs SET status='stage_a_complete',completed_at=?,source_building_count=?,"
            "candidate_count=?,stage_a_logical_sha256=? WHERE run_id=?",
            (base.utc_now(), len(buildings), len(evaluated), logical, run_id),
        )
        database.commit()
        integrity = str(database.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise IdentityCandidateV4Error("output integrity_check failed: " + integrity)
        database.close()
        database = None
        os.replace(temporary, output)
        artifact_size = output.stat().st_size
        artifact_sha = base.sha256_file(output)

        gate_payload = {
            "status": "PASS",
            "gate_version": "vnext-identity-wp24-four-regression-gates-v1",
            "run_id": run_id,
            "gate_count": 4,
            "passed_gate_count": 4,
            "failed_gate_count": 0,
            "gates": {name: gate_results[name] for name in REGRESSION_GATE_NAMES},
            "v4_artifact": {
                "path": _artifact_path(output, root),
                "size_bytes": artifact_size,
                "sha256": artifact_sha,
            },
            "fresh_n10_run": False,
            "network_requests": 0,
            "vision_requests": 0,
            "llm_requests": 0,
        }
        base._atomic_json(gate, gate_payload)
        gate_size = gate.stat().st_size
        gate_sha = base.sha256_file(gate)

        proposal_delta = {
            key: proposal_counts.get(key, 0) - v3_proposals.get(key, 0)
            for key in sorted(set(proposal_counts) | set(v3_proposals))
        }
        report_payload = {
            "status": "PASS",
            "report_version": "vnext-identity-wp24-transition-volume-v4",
            "run_id": run_id,
            "stage": "WP2.4 Stage A v4 offline replay",
            "candidate_count": len(evaluated),
            "proposal_counts_v3": dict(sorted(v3_proposals.items())),
            "proposal_counts_v4": dict(sorted(proposal_counts.items())),
            "proposal_count_delta": proposal_delta,
            "transition_counts": dict(sorted(transition_counts.items())),
            "eligibility_transition_counts": dict(sorted(eligibility_transition_counts.items())),
            "auto_merge_eligible_v3": v3_eligible,
            "auto_merge_eligible_v4": eligible_count,
            "auto_merge_eligible_delta": eligible_count - v3_eligible,
            "merge_candidate_total": merge_candidate_count,
            "human_review_queue": {
                "total": queue_count,
                "proposal_counts": dict(sorted(queue_proposal_counts.items())),
                "evidence_strength_bands": dict(sorted(queue_band_counts.items())),
                "policy_flag_subsets_overlapping": dict(sorted(queue_flag_counts.items())),
                "definition": "all merge_candidate plus anchored positive-family>=2 defer",
                "human_approval_required": True,
            },
            "policy_flag_counts": dict(sorted(policy_flag_counts.items())),
            "fanout_group_status_counts": dict(sorted(fanout_status_counts.items())),
            "rule_impacts": rule_impacts,
            "decision_changed_count": len(changed_transitions),
            "all_decision_changed_pairs": [
                _transition_report_row(row) for row in changed_transitions
            ],
            "regression_gate": {
                "status": "PASS",
                "path": _artifact_path(gate, root),
                "size_bytes": gate_size,
                "sha256": gate_sha,
                "gate_results": {name: gate_results[name] for name in REGRESSION_GATE_NAMES},
            },
            "fresh_n10": {"run": False, "sample_count": 0, "reason": "WP2.4 order forbids fresh N10"},
            "stage_a_logical_sha256": logical,
            "full_logical_sha256": full_logical,
            "stage_a_artifact_size_bytes": artifact_size,
            "stage_a_artifact_sha256": artifact_sha,
            "accounting": {
                "source_buildings": len(buildings),
                "six_family_rows": len(evaluated) * 6,
                "direct_image_evidence_rows": direct_count,
                "transition_rows": len(transitions),
                "regression_binding_rows": len(regression_bindings),
                "regression_result_rows": len(regression_bindings),
                "registries_created": 0,
                "new_ids_issued": 0,
            },
            "cost": {
                "network_requests": 0,
                "paid_api_requests": 0,
                "vision_requests": 0,
                "llm_requests": 0,
                "estimated_usd": 0.0,
            },
        }
        base._atomic_json(report, report_payload)
        report_size = report.stat().st_size
        report_sha = base.sha256_file(report)

        manifest_payload = {
            "status": "PASS",
            "run_id": run_id,
            "contract_version": CONTRACT_VERSION,
            "builder_version": BUILDER_VERSION,
            "blocker_version": base.BLOCKER_VERSION,
            "policy_version": POLICY_VERSION,
            "policy_sha256": policy_sha,
            "normalizer_version": NORMALIZER_VERSION,
            "normalizer_sha256": normalizer_sha,
            "normalizer_spec": NORMALIZER_SPEC,
            "additions": ISO_COUNTRY_ADDITIONS,
            "policy_spec": POLICY_SPEC,
            "sample_version": SAMPLE_VERSION,
            "sample_seed": SAMPLE_SEED,
            "stage_a_logical_sha256": logical,
            "full_logical_sha256": full_logical,
            "stage_a_artifact_size_bytes": artifact_size,
            "stage_a_artifact_sha256": artifact_sha,
            "regression_gate": {
                "path": _artifact_path(gate, root),
                "size_bytes": gate_size,
                "sha256": gate_sha,
                "status": "PASS",
            },
            "volume_report": {
                "path": _artifact_path(report, root),
                "size_bytes": report_size,
                "sha256": report_sha,
                "status": "PASS",
            },
            "input_pins": pins,
            "v3_usage": "comparison_only; never candidate evidence",
            "n100_usage": "regression gold binding only; never candidate evidence",
            "human_approval_required": True,
            "forbidden_actions": {
                "network_requests": 0,
                "vision_requests": 0,
                "llm_requests": 0,
                "legacy_snapshot_reads": 0,
                "merge_executions": 0,
                "vnx_id_issuances": 0,
                "fresh_n10_runs": 0,
                "neon_writes": 0,
                "r2_writes": 0,
            },
        }
        base._atomic_json(manifest, manifest_payload)
        return BuildResult(
            output,
            manifest,
            report,
            gate,
            run_id,
            logical,
            full_logical,
            len(evaluated),
            merge_candidate_count,
            eligible_count,
            queue_count,
            len(changed_transitions),
            "PASS",
            time.monotonic() - started,
        )
    except Exception:
        if database is not None:
            database.close()
        for connection in connections:
            connection.close()
        if temporary.exists():
            failed = temporary.with_name(temporary.name + ".failed")
            if not failed.exists():
                os.replace(temporary, failed)
        raise


__all__ = [
    "APPLICATION_ID",
    "BUILDER_VERSION",
    "BuildResult",
    "CONTRACT_VERSION",
    "DEFAULT_GATE",
    "DEFAULT_MANIFEST",
    "DEFAULT_N100",
    "DEFAULT_OUTPUT",
    "DEFAULT_REPORT",
    "DEFAULT_V3",
    "FULL_LOGICAL_VERSION",
    "INPUT_PINS",
    "IdentityCandidateV4Error",
    "NORMALIZER_SPEC",
    "NORMALIZER_VERSION",
    "POLICY_SPEC",
    "POLICY_VERSION",
    "REGRESSION_GATE_NAMES",
    "SAMPLE_SEED",
    "SAMPLE_VERSION",
    "SCHEMA_VERSION",
    "V4_BASE_SCHEMA_SQL",
    "V4_SCHEMA_SQL",
    "WP22_REGRESSION_IDS",
    "YEAR_WEAK_NEGATIVE_MIN_GAP",
    "apply_fanout_guard",
    "build_identity_candidates_v4",
    "evaluate_candidate",
    "evidence_strength_band",
    "full_logical_sha256",
    "human_review_queue_reasons",
]
