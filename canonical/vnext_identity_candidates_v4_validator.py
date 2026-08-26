"""Independent validator for the WP2.4 identity-candidate v4 replay.

The validator intentionally does not import the v4 producer.  It treats the
SHA-pinned WP2.2 v3 database as the comparison baseline, independently applies
R1--R5, rebuilds the post-policy fan-out graph and review queue, and binds the
four regression gates to the frozen WP2.3 N100 gold.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from canonical import vnext_identity_candidates_validator as core


VALIDATOR_VERSION = "vnext-identity-wp24-independent-validator-v4"
APPLICATION_ID = 0x564E5832
SCHEMA_VERSION = 4
CONTRACT_VERSION = "vnext-identity-contract-wp24-v1.1"
BUILDER_VERSION = "vnext-identity-candidates-builder-v4"
NORMALIZER_VERSION = "vnext-identity-country-iso3166-v3"
POLICY_VERSION = "vnext-identity-n100-rule-revision-v4"
SAMPLE_VERSION = "vnext-identity-wp24-regression-only-no-fresh-n10-v4"
SAMPLE_SEED = "vnext-wp24-no-fresh-n10-20260826"
FULL_LOGICAL_VERSION = "vnext-identity-stage-a-wp24-transition-logical-v4"
STAGE_A_LOGICAL_VERSION = "vnext-identity-stage-a-logical-v1"

NAME_POSITIVE_THRESHOLD = 90.0
FIRM_POSITIVE_THRESHOLD = 90.0
YEAR_POSITIVE_MAX_GAP = 1
YEAR_WEAK_NEGATIVE_MIN_GAP = 6

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_candidates_v4.db")
DEFAULT_V3 = Path("data/enrichment/vnext_identity_candidates_v3.db")
DEFAULT_N100 = Path("data/enrichment/vnext_identity_n100_v1.db")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_candidates_v4_manifest.json")
DEFAULT_STAGE_A_REPORT = Path("data/reports/vnext_identity_candidates_v4_stage_a_report.json")
DEFAULT_REGRESSION_GATE_REPORT = Path(
    "data/reports/vnext_identity_candidates_v4_regression_gate.json"
)
DEFAULT_REPORT = Path("data/reports/vnext_identity_candidates_v4_validation.json")

EXPECTED_SOURCE_BUILDINGS = 91_803
EXPECTED_CANDIDATES = 68_049
EXPECTED_NORMALIZER_SHA256 = "968c9e089293b3f6180947ae5fe792d8ebc91adc794017094920df9bc0e3602e"
EXPECTED_POLICY_SHA256 = "1b7e98c2a7ca9cd32f67c961631314f22afaee842282d4ffee195af1ea55cac9"
EXPECTED_GATE_COUNTS = {
    "b4_name_only_reject": 10,
    "wp22_country_conflict_reject": 4,
    "b2_different_phash_hard_conflict": 4,
    "b1_b3_human_review_queue": 15,
}
WP22_REGRESSION_IDS = frozenset(
    {
        "idc_016b59163550fcd3bfe2dcfde694745a",
        "idc_31831329e77427910c173d3142b4dabe",
        "idc_55b2f5fc5c91de7d9239c392bc3f08b3",
        "idc_4142468fdbf954126c43359abad4e3b3",
    }
)

# Copied from the frozen contracts, never imported from the v4 producer.
INPUT_PINS: Mapping[str, tuple[Path, int, str, str]] = {
    "architizer_curated_metadata": (
        Path("data/curated/architizer_curated_v2_0.db"),
        8_767_438_848,
        "605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc",
        "candidate_and_evidence",
    ),
    "build_strict_country_aliases": (
        Path("tools/build_strict_canonical.py"),
        25_498,
        "97aedf10755fa80fdef6abc39651da200cc6e19e573ba78b980dfd55bde67d06",
        "normalizer_legacy_provenance",
    ),
    "building_matcher": (
        Path("canonical/match_buildings_sequential.py"),
        26_812,
        "3cf9592a9e54cd4e07512461ca08dc5ca7f8dd5e6cd97b2d6c118dd0b265d87b",
        "candidate_blocking_only",
    ),
    "c17_country_aliases_ext": (
        Path("tools/canonical_v2_c17_make_web_polish.py"),
        16_202,
        "13334b47ae4e2c9b4909984b2f1e591249ab384b7c4027668441c4a66edce569",
        "normalizer_legacy_provenance",
    ),
    "c21_country_normalizer": (
        Path("tools/canonical_v2_c21_make_web_polish.py"),
        19_534,
        "60c188bbae3a5fadf03d72de919fe5ad41bf0d3a75b9d72d7ce0058222cbbb92",
        "normalizer_legacy_provenance",
    ),
    "c23_known_countries": (
        Path("tools/canonical_v2_c23_final.py"),
        15_197,
        "f8adf319e388119c659a8c04435e1a1b4e5f056baf560aea37d9ede50cebb0c5",
        "normalizer_legacy_provenance",
    ),
    "country_normalizer_module": (
        Path("canonical/vnext_country_norm.py"),
        22_896,
        "98e3cf971525f17393d772f45bf52ac3dfe37dbb5d4bc593019483eafab36015",
        "normalizer_implementation",
    ),
    "cross_source_e2_full_v5": (
        Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"),
        10_164_682_752,
        "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19",
        "candidate_and_evidence",
    ),
    "divisare_curated_metadata": (
        Path("data/curated/divisare_metadata_v2_4.db"),
        2_225_299_456,
        "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f",
        "candidate_and_evidence",
    ),
    "identity_contract": (
        Path("docs/vnext/CONTRACT_IDENTITY.md"),
        9_879,
        "502e6258b9c7803ea58bc415ddbe73fbcc50faa0005d8b9aaaaaeab04838e3e8",
        "policy_contract",
    ),
    "n100_gold_artifact": (
        DEFAULT_N100,
        2_183_168,
        "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b",
        "regression_gold_binding_only_not_candidate_evidence",
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
    "phash_matcher": (
        Path("canonical/match_phash_check.py"),
        5_450,
        "3dcc73152d822fe2b8eaccbb333625672378a3f6cfa59d3099cda0ca28e64fe7",
        "image_policy_provenance",
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
    "v3_candidate_artifact": (
        DEFAULT_V3,
        580_710_400,
        "c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f",
        "comparison_only_not_candidate_evidence",
    ),
    "wp24_rule_revision_order": (
        Path(".claude/ops/jobs/20260826_vnext_wp24_rule_revision_order.md"),
        5_585,
        "e118437909c7b8cbcf5842c6eadecea39a84b958af569bbe2801d03b38a31b2a",
        "issued_policy_order",
    ),
    "workspace_freeze_manifest": (
        Path("data/reports/vnext_workspace_freeze_v1.json"),
        246_838,
        "cd787a0088161fd0e8dddfe23e1943c0a41992f1e1a214b67fcce5d3ae17dcc3",
        "pin_manifest_only",
    ),
}

REQUIRED_TABLES = core.REQUIRED_TABLES | {
    "country_normalizations",
    "candidate_policy_flags",
    "fanout_groups",
    "human_review_queue",
    "candidate_transitions",
    "regression_bindings",
    "regression_results",
    "regression_gate_results",
}
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


@dataclass
class CandidateState:
    candidate_id: str
    left_id: str
    right_id: str
    row: dict[str, Any]
    v3_row: dict[str, Any]
    families: dict[str, dict[str, Any]]
    conflicts: dict[str, dict[str, Any]]
    flags: dict[str, dict[str, Any]]
    v3_conflicts: tuple[str, ...]
    v3_flags: tuple[str, ...]
    anchor_families: tuple[str, ...]
    r1_before: dict[str, Any]
    r1_after: dict[str, Any]
    r2_before: dict[str, Any]
    r2_after: dict[str, Any]
    r3_before: dict[str, Any]
    r3_after: dict[str, Any]
    fanout_triggers: list[dict[str, Any]]


class IdentityCandidateV4ValidationError(RuntimeError):
    """Raised for a validator setup or no-clobber failure."""


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _relative_display(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _best_full(left: Sequence[str], right: Sequence[str]) -> tuple[float, float, float, str, str]:
    best: tuple[float, float, float, str, str] = (0.0, 0.0, 0.0, "", "")
    for left_value in left:
        for right_value in right:
            strict, loose, token_set = core._sim(left_value, right_value)
            candidate = (strict, loose, token_set, left_value, right_value)
            if candidate[:3] > best[:3]:
                best = candidate
    return best


def _year_gap(left: Mapping[str, Any], right: Mapping[str, Any]) -> int | None:
    left_years = [x for x in (left.get("year_min"), left.get("year_max")) if x is not None]
    right_years = [x for x in (right.get("year_min"), right.get("year_max")) if x is not None]
    if not left_years or not right_years:
        return None
    left_min, left_max = min(left_years), max(left_years)
    right_min, right_max = min(right_years), max(right_years)
    if left_max < right_min:
        return int(right_min - left_max)
    if right_max < left_min:
        return int(left_min - right_max)
    return 0


def _family_names(state: CandidateState, status: str = "positive") -> tuple[str, ...]:
    return tuple(sorted(name for name, row in state.families.items() if row["status"] == status))


def _snapshot(state: CandidateState) -> dict[str, Any]:
    return {
        "proposal": state.row["proposal"],
        "auto_merge_eligible": int(state.row["auto_merge_eligible"]),
        "positive_family_count": int(state.row["positive_family_count"]),
        "negative_family_count": int(state.row["negative_family_count"]),
        "positive_families": list(_family_names(state, "positive")),
        "negative_families": list(_family_names(state, "negative")),
        "anchor_families": list(state.anchor_families),
        "hard_conflicts": sorted(state.conflicts),
        "policy_flags": sorted(state.flags),
    }


def _refresh_counts_and_decision(
    state: CandidateState, *, legacy_matcher_year_conflict: bool = False
) -> None:
    positive = _family_names(state, "positive")
    negative = _family_names(state, "negative")
    state.row["positive_family_count"] = len(positive)
    state.row["negative_family_count"] = len(negative)
    state.row["hard_conflict_count"] = len(state.conflicts)
    name_positive = "name_alias_identity" in positive
    firm_positive = "architect_firm_identity" in positive
    site_positive = "location_site_identity" in positive
    if not state.conflicts and len(positive) >= 2:
        proposal = "merge_candidate"
    elif state.conflicts:
        phase_conflicts = {"project_phase_incompatible"}
        if legacy_matcher_year_conflict:
            phase_conflicts.add("matcher_architect_year_gate_block")
        related_signal = (
            name_positive
            and (firm_positive or site_positive)
            and bool(phase_conflicts & set(state.conflicts))
            and "country_site_incompatible" not in state.conflicts
        )
        proposal = "related_but_separate" if related_signal else "reject"
    else:
        proposal = "defer"
    state.row["proposal"] = proposal
    eligible = int(proposal == "merge_candidate" and len(positive) >= 2 and not state.conflicts)
    if "phash_veto_review_required" in state.flags:
        eligible = 0
    state.row["auto_merge_eligible"] = eligible


def _apply_anchor(state: CandidateState) -> None:
    anchors = tuple(
        family
        for family in ("name_alias_identity", "verified_pixel_image_overlap")
        if state.families[family]["status"] == "positive"
    )
    state.anchor_families = anchors
    state.flags.pop("anchor_missing", None)
    if state.row["proposal"] == "merge_candidate" and not anchors:
        state.flags["anchor_missing"] = {
            "positive_family_count": state.row["positive_family_count"],
            "required_anchor_families": ["name_alias_identity", "verified_pixel_image_overlap"],
            "name_strict_score": round(float(state.row["name_strict_score"]), 6),
            "strong_image_count": int(
                state.row["exact_asset_pair_count"]
                + state.row["identical_phash_pair_count"]
                + state.row["phash_le8_pair_count"]
            ),
            "proposal_preserved": state.row["proposal"],
        }
        state.row["auto_merge_eligible"] = 0


def _load_v3(v3: sqlite3.Connection) -> tuple[str, dict[tuple[str, str], dict[str, Any]], dict[str, CandidateState]]:
    run_rows = v3.execute("SELECT run_id FROM runs").fetchall()
    if len(run_rows) != 1:
        raise IdentityCandidateV4ValidationError("v3 must contain exactly one run")
    run_id = str(run_rows[0][0])
    country_rows = {
        (str(row["source"]), str(row["source_building_id"])): (
            str(row["normalization_status"]), str(row["canonical_country"] or "")
        )
        for row in v3.execute("SELECT * FROM country_normalizations WHERE run_id=?", (run_id,))
    }
    buildings: dict[tuple[str, str], dict[str, Any]] = {}
    for row in v3.execute("SELECT * FROM source_buildings WHERE run_id=?", (run_id,)):
        key = (str(row["source"]), str(row["source_building_id"]))
        status, canonical = country_rows[key]
        buildings[key] = {
            "source": key[0],
            "id": key[1],
            "name": str(row["name"] or ""),
            "aliases": tuple(
                sorted(
                    {
                        core._norm(str(x), ascii_fold=True)
                        for x in _json_value(row["aliases_json"], [])
                        if str(x)
                    }
                )
            ),
            "firm_keys": tuple(str(x) for x in _json_value(row["firm_keys_json"], [])),
            "country_status": status,
            "country_norm": canonical,
            "locality_norm": str(row["locality_normalized"] or ""),
            "year_min": row["completion_year_min"],
            "year_max": row["completion_year_max"],
        }
    families: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in v3.execute("SELECT * FROM evidence_families WHERE run_id=?", (run_id,)):
        families[str(row["candidate_id"])][str(row["family"])] = {
            "status": str(row["status"]),
            "score": row["score"],
            "detail": _json_value(row["evidence_json"], {}),
        }
    conflicts: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in v3.execute("SELECT * FROM hard_conflicts WHERE run_id=?", (run_id,)):
        conflicts[str(row["candidate_id"])][str(row["conflict_type"])] = _json_value(
            row["detail_json"], {}
        )
    flags: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in v3.execute("SELECT * FROM candidate_policy_flags WHERE run_id=?", (run_id,)):
        flags[str(row["candidate_id"])][str(row["flag_type"])] = _json_value(row["detail_json"], {})
    states: dict[str, CandidateState] = {}
    for candidate in v3.execute("SELECT * FROM candidate_pairs WHERE run_id=?", (run_id,)):
        row = dict(candidate)
        candidate_id = str(row["candidate_id"])
        state = CandidateState(
            candidate_id=candidate_id,
            left_id=str(row["left_source_building_id"]),
            right_id=str(row["right_source_building_id"]),
            row=row,
            v3_row=dict(row),
            families=json.loads(json.dumps(families[candidate_id])),
            conflicts=json.loads(json.dumps(conflicts[candidate_id])),
            flags=json.loads(json.dumps(flags[candidate_id])),
            v3_conflicts=tuple(sorted(conflicts[candidate_id])),
            v3_flags=tuple(sorted(flags[candidate_id])),
            anchor_families=(),
            r1_before={},
            r1_after={},
            r2_before={},
            r2_after={},
            r3_before={},
            r3_after={},
            fanout_triggers=[],
        )
        _apply_anchor(state)
        states[candidate_id] = state
    return run_id, buildings, states


def _apply_r1_r2(states: Mapping[str, CandidateState]) -> None:
    for state in states.values():
        # v3 fan-out was a terminal hard conflict; R3 must rebuild it from a
        # fresh post-R1/R2/anchor graph rather than inherit it.
        state.conflicts.pop("fanout_conflict", None)
        state.flags = {}
        _refresh_counts_and_decision(state, legacy_matcher_year_conflict=True)
        _apply_anchor(state)
        state.r1_before = _snapshot(state)
        country_detail = state.conflicts.get("country_site_incompatible")
        phash_detail = state.conflicts.get("verified_phash_veto")
        state.conflicts = {}
        if country_detail is not None:
            state.conflicts["country_site_incompatible"] = country_detail

        year = state.families["year_phase_compatibility"]
        gap = state.row["year_gap"]
        if gap is not None and int(gap) <= YEAR_POSITIVE_MAX_GAP:
            year["status"] = "positive"
        elif gap is not None and int(gap) >= YEAR_WEAK_NEGATIVE_MIN_GAP:
            year["status"] = "negative"
        else:
            year["status"] = "unknown"
        year["detail"]["positive_tolerance"] = YEAR_POSITIVE_MAX_GAP
        year["detail"]["neutral_gap_min"] = 2
        year["detail"]["neutral_gap_max"] = 5
        year["detail"]["weak_negative_min_gap"] = YEAR_WEAK_NEGATIVE_MIN_GAP
        year["detail"]["negative_strength"] = (
            "weak" if year["status"] == "negative" else None
        )
        year["detail"]["matcher_architect_year_gate_is_hard_conflict"] = False

        positive = {name for name, row in state.families.items() if row["status"] == "positive"}
        if (
            gap is not None
            and int(gap) >= YEAR_WEAK_NEGATIVE_MIN_GAP
            and "verified_pixel_image_overlap" not in positive
            and "architect_firm_identity" not in positive
        ):
            state.conflicts["project_phase_incompatible"] = {
                "year_gap": int(gap),
                "hard_gap_min_inclusive": YEAR_WEAK_NEGATIVE_MIN_GAP,
                "verified_pixel_image_overlap_positive": False,
                "architect_firm_identity_positive": False,
                "rule": "gap>=6 and neither image nor firm positive",
            }
        if phash_detail is not None:
            state.conflicts["verified_phash_veto"] = phash_detail
        state.flags = {}
        _refresh_counts_and_decision(state)
        _apply_anchor(state)
        state.r1_after = _snapshot(state)

        state.r2_before = _snapshot(state)
        positive = set(_family_names(state))
        if (
            "verified_phash_veto" in state.conflicts
            and "name_alias_identity" in positive
            and "architect_firm_identity" in positive
        ):
            veto_detail = state.conflicts.pop("verified_phash_veto")
            other_conflicts = sorted(state.conflicts)
            state.flags["phash_veto_review_required"] = {
                "downgraded_conflict": {
                    "conflict_type": "verified_phash_veto",
                    "detail": veto_detail,
                },
                "name_alias_identity_positive": True,
                "architect_firm_identity_positive": True,
                "image_family_status_preserved": state.families[
                    "verified_pixel_image_overlap"
                ]["status"],
                "auto_merge_eligible": False,
                "other_hard_conflicts": other_conflicts,
            }
        _refresh_counts_and_decision(state)
        _apply_anchor(state)
        state.r2_after = _snapshot(state)
        state.r3_before = _snapshot(state)


def _same_source_related(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    name_strict, _name_loose, _name_set, name_left, name_right = _best_full(
        left["aliases"], right["aliases"]
    )
    firm_strict, _firm_loose, _firm_set, firm_left, firm_right = _best_full(
        left["firm_keys"], right["firm_keys"]
    )
    exact_firms = sorted(set(left["firm_keys"]) & set(right["firm_keys"]))
    firm_positive = bool(exact_firms) or firm_strict >= FIRM_POSITIVE_THRESHOLD
    gap = _year_gap(left, right)
    country_conflict = bool(
        left["country_status"] == "known"
        and right["country_status"] == "known"
        and left["country_norm"] != right["country_norm"]
    )
    site_positive = bool(
        left["country_status"] == "known"
        and right["country_status"] == "known"
        and left["country_norm"] == right["country_norm"]
        and left["locality_norm"]
        and left["locality_norm"] == right["locality_norm"]
    )
    year_positive = gap is not None and gap <= YEAR_POSITIVE_MAX_GAP
    phase_conflict = bool(
        gap is not None and gap >= YEAR_WEAK_NEGATIVE_MIN_GAP and not firm_positive
    )
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
    related = bool(name_strict >= NAME_POSITIVE_THRESHOLD and additional and not hard_conflicts)
    return related, {
        "related": related,
        "name_strict_score": round(name_strict, 6),
        "name_left_alias": name_left,
        "name_right_alias": name_right,
        "name_threshold": NAME_POSITIVE_THRESHOLD,
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


def _apply_fanout(
    states: Mapping[str, CandidateState], buildings: Mapping[tuple[str, str], Mapping[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[CandidateState]] = defaultdict(list)
    for state in states.values():
        if state.row["auto_merge_eligible"]:
            grouped[("divisare", state.left_id)].append(state)
            grouped[("architizer", state.right_id)].append(state)
    groups: list[dict[str, Any]] = []
    triggers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (hub_source, hub_id), incident in sorted(grouped.items()):
        if len(incident) < 2:
            continue
        opponent_source = "architizer" if hub_source == "divisare" else "divisare"
        opponent_ids = sorted(
            {state.right_id if hub_source == "divisare" else state.left_id for state in incident}
        )
        pair_checks: list[dict[str, Any]] = []
        unrelated: list[list[str]] = []
        for first_id, second_id in combinations(opponent_ids, 2):
            related, detail = _same_source_related(
                buildings[(opponent_source, first_id)], buildings[(opponent_source, second_id)]
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
        incident_ids = sorted(state.candidate_id for state in incident)
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
            for state in incident:
                triggers[state.candidate_id].append(trigger)
    for candidate_id, candidate_triggers in sorted(triggers.items()):
        state = states[candidate_id]
        detail = {
            "snapshot": "post_R1_R2_anchor_pre_fanout",
            "triggers": candidate_triggers,
        }
        positive_count = int(state.row["positive_family_count"])
        if positive_count >= 2 and state.anchor_families:
            state.row["proposal"] = "merge_candidate"
            state.flags["fanout_review_required"] = {
                **detail,
                "positive_family_count": positive_count,
                "anchor_families": list(state.anchor_families),
                "auto_merge_eligible": False,
            }
        else:
            state.row["proposal"] = "defer"
        state.row["auto_merge_eligible"] = 0
        state.fanout_triggers = candidate_triggers
    for state in states.values():
        state.r3_after = _snapshot(state)
    return groups


def _queue_reasons(state: CandidateState) -> tuple[str, ...]:
    reasons: list[str] = []
    if state.row["proposal"] == "merge_candidate":
        reasons.append("merge_candidate")
    if (
        state.row["proposal"] == "defer"
        and state.row["positive_family_count"] >= 2
        and state.anchor_families
    ):
        reasons.append("r4_anchor_two_family_defer")
    return tuple(reasons)


def _evidence_band(state: CandidateState) -> str:
    if not _queue_reasons(state):
        return "not_queued"
    positive = set(_family_names(state))
    if len(positive) >= 3 and {
        "name_alias_identity",
        "architect_firm_identity",
        "verified_pixel_image_overlap",
    } <= positive:
        return "name_firm_image_3plus"
    if len(positive) >= 3 and state.anchor_families:
        return "other_anchor_3plus"
    if len(positive) == 2 and state.anchor_families:
        return "anchor_2"
    return "no_anchor_2plus"


def _replay_policy(
    v3: sqlite3.Connection,
) -> tuple[str, dict[tuple[str, str], dict[str, Any]], dict[str, CandidateState], list[dict[str, Any]]]:
    v3_run_id, buildings, states = _load_v3(v3)
    _apply_r1_r2(states)
    fanout_groups = _apply_fanout(states, buildings)
    return v3_run_id, buildings, states, fanout_groups


def _record_hash_failures(database: sqlite3.Connection, run_id: str) -> list[str]:
    """Recompute every non-empty v4 record hash from its canonical payload."""
    failures: list[str] = []

    def verify(table: str, identity: str, payload: Mapping[str, Any], stored: Any) -> None:
        if core._sha(payload) != str(stored):
            failures.append(f"{table}:{identity}")

    for row in database.execute("SELECT * FROM input_pins WHERE run_id=?", (run_id,)):
        payload = {
            "role": row["role"],
            "path": row["path"],
            "size_bytes": row["size_bytes"],
            "sha256_before": row["sha256_before"],
            "sha256_after": row["sha256_after"],
            "usage": row["usage"],
        }
        verify("input_pins", str(row["role"]), payload, row["record_sha256"])

    for row in database.execute("SELECT * FROM source_buildings WHERE run_id=?", (run_id,)):
        payload = {
            "source": row["source"],
            "source_building_id": row["source_building_id"],
            "name": row["name"],
            "aliases": _json_value(row["aliases_json"], []),
            "firms": _json_value(row["firms_json"], []),
            "firm_keys": _json_value(row["firm_keys_json"], []),
            "country": row["country"],
            "country_normalized": row["country_normalized"],
            "locality": row["locality"],
            "locality_normalized": row["locality_normalized"],
            "completion_year_min": row["completion_year_min"],
            "completion_year_max": row["completion_year_max"],
            "source_urls": _json_value(row["source_urls_json"], []),
            "external_ids": _json_value(row["external_ids_json"], []),
            "e2_source_record_sha256": row["e2_source_record_sha256"],
        }
        identity = f"{row['source']}:{row['source_building_id']}"
        verify("source_buildings", identity, payload, row["record_sha256"])

    for row in database.execute("SELECT * FROM building_image_stats WHERE run_id=?", (run_id,)):
        payload = {
            "source": row["source"],
            "source_building_id": row["source_building_id"],
            "asset_count": row["asset_count"],
            "successful_asset_count": row["successful_asset_count"],
            "unique_pixel_count": row["unique_pixel_count"],
            "representative": _json_value(row["representative_json"], None),
        }
        identity = f"{row['source']}:{row['source_building_id']}"
        verify("building_image_stats", identity, payload, row["record_sha256"])

    origins: dict[str, list[str]] = defaultdict(list)
    for row in database.execute(
        "SELECT * FROM candidate_origins WHERE run_id=? ORDER BY candidate_id,origin", (run_id,)
    ):
        candidate_id = str(row["candidate_id"])
        origins[candidate_id].append(str(row["origin"]))
        payload = {
            "candidate_id": candidate_id,
            "origin": row["origin"],
            "detail": _json_value(row["origin_detail_json"], {}),
        }
        verify(
            "candidate_origins",
            f"{candidate_id}:{row['origin']}",
            payload,
            row["record_sha256"],
        )

    family_statuses: dict[str, dict[str, str]] = defaultdict(dict)
    for row in database.execute(
        "SELECT * FROM evidence_families WHERE run_id=? ORDER BY candidate_id,family", (run_id,)
    ):
        candidate_id = str(row["candidate_id"])
        family_statuses[candidate_id][str(row["family"])] = str(row["status"])
        payload = {
            "candidate_id": candidate_id,
            "family": row["family"],
            "status": row["status"],
            "score": row["score"],
            "detail": _json_value(row["evidence_json"], {}),
        }
        verify(
            "evidence_families",
            f"{candidate_id}:{row['family']}",
            payload,
            row["record_sha256"],
        )

    flag_types: dict[str, list[str]] = defaultdict(list)
    for row in database.execute(
        "SELECT * FROM candidate_policy_flags WHERE run_id=? ORDER BY candidate_id,flag_type",
        (run_id,),
    ):
        candidate_id = str(row["candidate_id"])
        flag_types[candidate_id].append(str(row["flag_type"]))
        payload = {
            "candidate_id": candidate_id,
            "flag_type": row["flag_type"],
            "detail": _json_value(row["detail_json"], {}),
        }
        verify(
            "candidate_policy_flags",
            f"{candidate_id}:{row['flag_type']}",
            payload,
            row["record_sha256"],
        )

    for row in database.execute("SELECT * FROM candidate_pairs WHERE run_id=?", (run_id,)):
        candidate_id = str(row["candidate_id"])
        positives = family_statuses[candidate_id]
        anchors = [
            family
            for family in ("name_alias_identity", "verified_pixel_image_overlap")
            if positives.get(family) == "positive"
        ]
        image_counts = {
            "exact": row["exact_asset_pair_count"],
            "identical": row["identical_phash_pair_count"],
            "le8": row["phash_le8_pair_count"],
            "weak": row["phash_9_16_pair_count"],
        }
        payload = {
            "candidate_id": candidate_id,
            "left_source_building_id": row["left_source_building_id"],
            "right_source_building_id": row["right_source_building_id"],
            "origins": origins[candidate_id],
            "e2_metadata_pair_id": row["e2_metadata_pair_id"],
            "e2_building_candidate_id": row["e2_building_candidate_id"],
            "image_counts": image_counts,
            "min_phash_distance": row["min_phash_distance"],
            "name_strict_score": row["name_strict_score"],
            "name_loose_score": row["name_loose_score"],
            "name_token_set_score": row["name_token_set_score"],
            "firm_strict_score": row["firm_strict_score"],
            "year_gap": row["year_gap"],
            "positive_family_count": row["positive_family_count"],
            "negative_family_count": row["negative_family_count"],
            "hard_conflict_count": row["hard_conflict_count"],
            "proposal": row["proposal"],
            "auto_merge_eligible": row["auto_merge_eligible"],
            "anchor_families": anchors,
            "policy_flags": flag_types[candidate_id],
        }
        verify("candidate_pairs", candidate_id, payload, row["record_sha256"])

    for row in database.execute("SELECT * FROM hard_conflicts WHERE run_id=?", (run_id,)):
        payload = {
            "candidate_id": row["candidate_id"],
            "conflict_type": row["conflict_type"],
            "detail": _json_value(row["detail_json"], {}),
        }
        verify(
            "hard_conflicts",
            f"{row['candidate_id']}:{row['conflict_type']}",
            payload,
            row["record_sha256"],
        )

    for row in database.execute("SELECT * FROM direct_image_evidence WHERE run_id=?", (run_id,)):
        payload = {
            "candidate_id": row["candidate_id"],
            "e2_evidence_id": row["e2_evidence_id"],
            "evidence_kind": row["evidence_kind"],
            "left_source_asset_id": row["left_source_asset_id"],
            "right_source_asset_id": row["right_source_asset_id"],
            "phash_distance": row["phash_distance"],
            "e2_evidence_record_sha256": row["e2_evidence_record_sha256"],
        }
        verify(
            "direct_image_evidence",
            str(row["e2_evidence_id"]),
            payload,
            row["record_sha256"],
        )

    for row in database.execute("SELECT * FROM country_normalizations WHERE run_id=?", (run_id,)):
        payload = {
            "source": row["source"],
            "source_building_id": row["source_building_id"],
            "raw_country": row["raw_country"],
            "normalization_status": row["normalization_status"],
            "canonical_country": row["canonical_country"],
            "reason": row["reason"],
        }
        identity = f"{row['source']}:{row['source_building_id']}"
        verify("country_normalizations", identity, payload, row["record_sha256"])

    for row in database.execute("SELECT * FROM fanout_groups WHERE run_id=?", (run_id,)):
        payload = {
            "hub_source": row["hub_source"],
            "hub_source_building_id": row["hub_source_building_id"],
            "pre_guard_degree": row["pre_guard_degree"],
            "status": row["status"],
            "incident_candidate_ids": _json_value(row["incident_candidate_ids_json"], []),
            "opponent_source_building_ids": _json_value(
                row["opponent_source_building_ids_json"], []
            ),
            "unrelated_opponent_pairs": _json_value(
                row["unrelated_opponent_pairs_json"], []
            ),
            "detail": _json_value(row["detail_json"], {}),
        }
        identity = f"{row['hub_source']}:{row['hub_source_building_id']}"
        verify("fanout_groups", identity, payload, row["record_sha256"])

    for row in database.execute("SELECT * FROM human_review_queue WHERE run_id=?", (run_id,)):
        payload = {
            "candidate_id": row["candidate_id"],
            "proposal": row["proposal"],
            "auto_merge_eligible": row["auto_merge_eligible"],
            "queue_reasons": _json_value(row["queue_reasons_json"], []),
            "positive_families": _json_value(row["positive_families_json"], []),
            "anchor_families": _json_value(row["anchor_families_json"], []),
            "policy_flags": _json_value(row["policy_flags_json"], []),
            "evidence_strength_band": row["evidence_strength_band"],
        }
        verify(
            "human_review_queue",
            str(row["candidate_id"]),
            payload,
            row["record_sha256"],
        )

    transition_json = {
        "v3_hard_conflicts_json": "v3_hard_conflicts",
        "v4_hard_conflicts_json": "v4_hard_conflicts",
        "v3_policy_flags_json": "v3_policy_flags",
        "v4_policy_flags_json": "v4_policy_flags",
        "r1_before_json": "r1_before",
        "r1_after_json": "r1_after",
        "r2_before_json": "r2_before",
        "r2_after_json": "r2_after",
        "r3_before_json": "r3_before",
        "r3_after_json": "r3_after",
        "r4_queue_reasons_json": "r4_queue_reasons",
        "reasons_json": "reasons",
    }
    for row in database.execute("SELECT * FROM candidate_transitions WHERE run_id=?", (run_id,)):
        payload: dict[str, Any] = {}
        for column in row.keys():
            if column in {"run_id", "record_sha256"}:
                continue
            key = transition_json.get(column, column)
            payload[key] = _json_value(row[column], []) if column in transition_json else row[column]
        verify(
            "candidate_transitions",
            str(row["candidate_id"]),
            payload,
            row["record_sha256"],
        )

    for row in database.execute("SELECT * FROM regression_bindings WHERE run_id=?", (run_id,)):
        payload = {
            "case_id": row["case_id"],
            "gate_name": row["gate_name"],
            "subgroup": row["subgroup"],
            "candidate_id": row["candidate_id"],
            "source_artifact_role": row["source_artifact_role"],
            "source_sample_id": row["source_sample_id"],
            "source_stratum": row["source_stratum"],
            "sample_origin": row["sample_origin"],
            "gold_label": row["gold_label"],
            "sample_record_sha256": row["sample_record_sha256"],
            "gold_record_sha256": row["gold_record_sha256"],
            "expected": _json_value(row["expected_json"], {}),
        }
        verify(
            "regression_bindings", str(row["case_id"]), payload, row["record_sha256"]
        )

    for row in database.execute("SELECT * FROM regression_results WHERE run_id=?", (run_id,)):
        payload = {
            "case_id": row["case_id"],
            "candidate_id": row["candidate_id"],
            "actual_proposal": row["actual_proposal"],
            "actual_auto_merge_eligible": row["actual_auto_merge_eligible"],
            "actual_hard_conflicts": _json_value(row["actual_hard_conflicts_json"], []),
            "actual_policy_flags": _json_value(row["actual_policy_flags_json"], []),
            "actual_human_review_queue": row["actual_human_review_queue"],
            "passed": row["passed"],
            "detail": _json_value(row["detail_json"], {}),
        }
        verify(
            "regression_results", str(row["case_id"]), payload, row["record_sha256"]
        )

    for row in database.execute(
        "SELECT * FROM regression_gate_results WHERE run_id=?", (run_id,)
    ):
        payload = {
            "gate_name": row["gate_name"],
            "expected_case_count": row["expected_case_count"],
            "actual_case_count": row["actual_case_count"],
            "passed_case_count": row["passed_case_count"],
            "failed_case_count": row["failed_case_count"],
            "status": row["status"],
            "detail": _json_value(row["detail_json"], {}),
        }
        verify(
            "regression_gate_results",
            str(row["gate_name"]),
            payload,
            row["record_sha256"],
        )

    return failures


def _expected_transition(state: CandidateState) -> dict[str, Any]:
    old = state.v3_row
    old_queue = int(str(old["proposal"]) == "merge_candidate")
    queue_reasons = list(_queue_reasons(state))
    new_queue = int(bool(queue_reasons))
    proposal_changed = int(str(old["proposal"]) != str(state.row["proposal"]))
    eligibility_changed = int(
        int(old["auto_merge_eligible"]) != int(state.row["auto_merge_eligible"])
    )
    r1_changed = int(core._json(state.r1_before) != core._json(state.r1_after))
    r2_changed = int(core._json(state.r2_before) != core._json(state.r2_after))
    r3_changed = int(core._json(state.r3_before) != core._json(state.r3_after))
    decision_changed = int(
        proposal_changed
        or eligibility_changed
        or int(old["positive_family_count"]) != int(state.row["positive_family_count"])
        or int(old["negative_family_count"]) != int(state.row["negative_family_count"])
        or list(state.v3_conflicts) != sorted(state.conflicts)
        or list(state.v3_flags) != sorted(state.flags)
        or old_queue != new_queue
    )
    reasons: list[str] = []
    if r1_changed:
        reasons.append("R1_year_changed")
    if r2_changed:
        reasons.append("R2_phash_changed")
    if r3_changed:
        reasons.append("R3_fanout_changed")
    if old_queue != new_queue:
        reasons.append("R4_review_queue_changed")
    if proposal_changed:
        reasons.append("proposal_changed")
    if eligibility_changed:
        reasons.append("auto_merge_eligibility_changed")
    return {
        "candidate_id": state.candidate_id,
        "left_source_building_id": state.left_id,
        "right_source_building_id": state.right_id,
        "v3_proposal": str(old["proposal"]),
        "v4_proposal": str(state.row["proposal"]),
        "v3_auto_merge_eligible": int(old["auto_merge_eligible"]),
        "v4_auto_merge_eligible": int(state.row["auto_merge_eligible"]),
        "v3_positive_family_count": int(old["positive_family_count"]),
        "v4_positive_family_count": int(state.row["positive_family_count"]),
        "v3_negative_family_count": int(old["negative_family_count"]),
        "v4_negative_family_count": int(state.row["negative_family_count"]),
        "v3_hard_conflicts": list(state.v3_conflicts),
        "v4_hard_conflicts": sorted(state.conflicts),
        "v3_policy_flags": list(state.v3_flags),
        "v4_policy_flags": sorted(state.flags),
        "r1_before": state.r1_before,
        "r1_after": state.r1_after,
        "r1_changed": r1_changed,
        "r2_before": state.r2_before,
        "r2_after": state.r2_after,
        "r2_changed": r2_changed,
        "r3_before": state.r3_before,
        "r3_after": state.r3_after,
        "r3_changed": r3_changed,
        "v3_human_review_queue": old_queue,
        "v4_human_review_queue": new_queue,
        "r4_queue_reasons": queue_reasons,
        "evidence_strength_band": _evidence_band(state),
        "proposal_changed": proposal_changed,
        "eligibility_changed": eligibility_changed,
        "decision_changed": decision_changed,
        "reasons": reasons,
    }


def _load_actual_maps(database: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    candidates = {
        str(row["candidate_id"]): dict(row)
        for row in database.execute("SELECT * FROM candidate_pairs WHERE run_id=?", (run_id,))
    }
    conflicts: dict[str, set[str]] = defaultdict(set)
    for row in database.execute(
        "SELECT candidate_id,conflict_type FROM hard_conflicts WHERE run_id=?", (run_id,)
    ):
        conflicts[str(row[0])].add(str(row[1]))
    flags: dict[str, set[str]] = defaultdict(set)
    for row in database.execute(
        "SELECT candidate_id,flag_type FROM candidate_policy_flags WHERE run_id=?", (run_id,)
    ):
        flags[str(row[0])].add(str(row[1]))
    families: dict[str, dict[str, str]] = defaultdict(dict)
    for row in database.execute(
        "SELECT candidate_id,family,status FROM evidence_families WHERE run_id=?", (run_id,)
    ):
        families[str(row[0])][str(row[1])] = str(row[2])
    queues = {
        str(row["candidate_id"]): {
            "proposal": str(row["proposal"]),
            "eligible": int(row["auto_merge_eligible"]),
            "reasons": _json_value(row["queue_reasons_json"], []),
            "positive": _json_value(row["positive_families_json"], []),
            "anchors": _json_value(row["anchor_families_json"], []),
            "flags": _json_value(row["policy_flags_json"], []),
            "band": str(row["evidence_strength_band"]),
        }
        for row in database.execute("SELECT * FROM human_review_queue WHERE run_id=?", (run_id,))
    }
    return {
        "candidates": candidates,
        "conflicts": conflicts,
        "flags": flags,
        "families": families,
        "queues": queues,
    }


def _transition_semantic(row: Mapping[str, Any]) -> dict[str, Any]:
    json_columns = {
        "v3_hard_conflicts_json": "v3_hard_conflicts",
        "v4_hard_conflicts_json": "v4_hard_conflicts",
        "v3_policy_flags_json": "v3_policy_flags",
        "v4_policy_flags_json": "v4_policy_flags",
        "r1_before_json": "r1_before",
        "r1_after_json": "r1_after",
        "r2_before_json": "r2_before",
        "r2_after_json": "r2_after",
        "r3_before_json": "r3_before",
        "r3_after_json": "r3_after",
        "r4_queue_reasons_json": "r4_queue_reasons",
        "reasons_json": "reasons",
    }
    result: dict[str, Any] = {}
    for column, value in row.items():
        if column in {"run_id", "record_sha256"}:
            continue
        key = json_columns.get(column, column)
        result[key] = _json_value(value, []) if column in json_columns else value
    return result


def _n100_gold(n100: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    run_rows = n100.execute("SELECT run_id FROM runs").fetchall()
    if len(run_rows) != 1:
        raise IdentityCandidateV4ValidationError("N100 must contain exactly one run")
    run_id = str(run_rows[0][0])
    rows: list[dict[str, Any]] = []
    for sample in n100.execute(
        "SELECT * FROM sample_pairs WHERE run_id=? AND cohort='B' ORDER BY sample_rank", (run_id,)
    ):
        origin = str(sample["sample_origin"])
        if origin == "fresh":
            gold = n100.execute(
                "SELECT final_gold_label,record_sha256 FROM adjudications "
                "WHERE run_id=? AND sample_id=?",
                (run_id, sample["sample_id"]),
            ).fetchone()
        else:
            gold = n100.execute(
                "SELECT final_gold_label,record_sha256 FROM carry_forward_gold "
                "WHERE run_id=? AND sample_id=?",
                (run_id, sample["sample_id"]),
            ).fetchone()
        if gold is None:
            raise IdentityCandidateV4ValidationError("N100 diagnostic gold row is missing")
        rows.append(
            {
                "candidate_id": str(sample["candidate_id"]),
                "sample_id": str(sample["sample_id"]),
                "stratum": str(sample["stratum"]),
                "sample_origin": origin,
                "gold_label": str(gold[0]),
                "sample_record_sha256": str(sample["record_sha256"]),
                "gold_record_sha256": str(gold[1]),
            }
        )
    return {
        "b4_name_only_reject": [row for row in rows if row["stratum"] == "B4_high_name_reject"],
        "b2_different_phash_hard_conflict": [
            row
            for row in rows
            if row["stratum"] == "B2_phash_veto" and row["gold_label"] == "different"
        ],
        "b1_b3_human_review_queue": [
            row
            for row in rows
            if (
                row["stratum"] == "B1_year_gate" and row["sample_origin"] == "fresh"
            )
            or (row["stratum"] == "B3_fanout_defer" and row["gold_label"] == "same")
        ],
    }


def _regression_actual(
    states: Mapping[str, CandidateState], gold: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, dict[str, bool]]:
    output: dict[str, dict[str, bool]] = {}
    output["b4_name_only_reject"] = {
        str(row["candidate_id"]): states[str(row["candidate_id"])].row["proposal"] == "reject"
        for row in gold["b4_name_only_reject"]
    }
    output["wp22_country_conflict_reject"] = {
        candidate_id: (
            states[candidate_id].row["proposal"] == "reject"
            and "country_site_incompatible" in states[candidate_id].conflicts
        )
        for candidate_id in sorted(WP22_REGRESSION_IDS)
    }
    output["b2_different_phash_hard_conflict"] = {
        str(row["candidate_id"]): (
            "verified_phash_veto" in states[str(row["candidate_id"])].conflicts
            and "phash_veto_review_required" not in states[str(row["candidate_id"])].flags
        )
        for row in gold["b2_different_phash_hard_conflict"]
    }
    output["b1_b3_human_review_queue"] = {
        str(row["candidate_id"]): bool(_queue_reasons(states[str(row["candidate_id"])]))
        for row in gold["b1_b3_human_review_queue"]
    }
    return output


def _wp22_expected_binding(candidate_id: str) -> dict[str, Any]:
    gate_name = "wp22_country_conflict_reject"
    return {
        "case_id": "rgr_"
        + core._stable(POLICY_VERSION, gate_name, "WP22", candidate_id)[:32],
        "subgroup": "WP22_country",
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


def _stage_a_logical(database: sqlite3.Connection, run_id: str) -> str:
    digest = hashlib.sha256()
    digest.update((STAGE_A_LOGICAL_VERSION + "\n").encode("utf-8"))
    for table, order in core.LOGICAL_TABLES:
        digest.update((table + "\n").encode("utf-8"))
        for row in database.execute(
            f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)
        ):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _full_logical(database: sqlite3.Connection, run_id: str, stage_a: str) -> str:
    digest = hashlib.sha256()
    digest.update((FULL_LOGICAL_VERSION + "\n" + stage_a + "\n").encode("utf-8"))
    for table, order in FULL_LOGICAL_TABLES:
        digest.update((table + "\n").encode("utf-8"))
        for row in database.execute(
            f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)
        ):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _expected_run_id() -> str:
    material = {
        "builder_version": BUILDER_VERSION,
        "normalizer_sha256": EXPECTED_NORMALIZER_SHA256,
        "policy_sha256": EXPECTED_POLICY_SHA256,
        "sample_version": SAMPLE_VERSION,
        "inputs": {role: spec[2] for role, spec in sorted(INPUT_PINS.items())},
    }
    return "wp24-" + core._sha(material)[:24]


def _fanout_semantic(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "hub_source": str(row["hub_source"]),
        "hub_source_building_id": str(row["hub_source_building_id"]),
        "pre_guard_degree": int(row["pre_guard_degree"]),
        "status": str(row["status"]),
        "incident_candidate_ids": _json_value(row["incident_candidate_ids_json"], []),
        "opponent_source_building_ids": _json_value(
            row["opponent_source_building_ids_json"], []
        ),
        "unrelated_opponent_pairs": _json_value(row["unrelated_opponent_pairs_json"], []),
        "detail": _json_value(row["detail_json"], {}),
    }


def _rule_impact(transitions: Sequence[Mapping[str, Any]], rule: str) -> dict[str, Any]:
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


def _write_json_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise IdentityCandidateV4ValidationError(f"refusing to clobber validation report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise IdentityCandidateV4ValidationError(f"stale validation report temporary: {temporary}")
    temporary.write_text(core._json(payload) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _failure_summary(values: Sequence[str]) -> Any:
    if not values:
        return []
    return {"count": len(values), "first": list(values[:50])}


def validate_identity_candidates_v4(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_ARTIFACT,
    manifest_path: Path = DEFAULT_MANIFEST,
    stage_a_report_path: Path = DEFAULT_STAGE_A_REPORT,
    regression_gate_path: Path = DEFAULT_REGRESSION_GATE_REPORT,
    report_path: Path | None = DEFAULT_REPORT,
    verify_full_hashes: bool = True,
) -> core.ValidationResult:
    """Independently validate the complete WP2.4 v4 offline replay."""
    root = repo_root.resolve()
    artifact = _resolve(root, artifact_path)
    manifest_file = _resolve(root, manifest_path)
    stage_report_file = _resolve(root, stage_a_report_path)
    gate_file = _resolve(root, regression_gate_path)
    validation_file = None if report_path is None else _resolve(root, report_path)
    if validation_file is not None and validation_file.exists():
        raise IdentityCandidateV4ValidationError(
            f"refusing to clobber validation report: {validation_file}"
        )
    if not artifact.is_file():
        raise IdentityCandidateV4ValidationError(f"v4 artifact is missing: {artifact}")

    checks: list[core.Check] = []
    manifest = _load_json(manifest_file)
    stage_report = _load_json(stage_report_file)
    gate_report = _load_json(gate_file)
    core._check(checks, "manifest_json", True, manifest is not None)
    core._check(checks, "stage_a_volume_report_json", True, stage_report is not None)
    core._check(checks, "regression_gate_report_json", True, gate_report is not None)
    manifest = manifest or {}
    stage_report = stage_report or {}
    gate_report = gate_report or {}

    pin_actual: dict[str, dict[str, Any]] = {}
    for role, (relative, size, expected_sha, usage) in INPUT_PINS.items():
        path = _resolve(root, relative)
        exists = path.is_file()
        actual_size = path.stat().st_size if exists else -1
        core._check(checks, f"input_{role}_size", size, actual_size)
        digest = (
            core._sha_file(path)
            if exists and verify_full_hashes
            else (expected_sha if exists else "missing")
        )
        core._check(checks, f"input_{role}_sha256", expected_sha, digest)
        sidecars = [str(path) + suffix for suffix in ("-journal", "-shm", "-wal")]
        if path.suffix.lower() == ".db":
            core._check(
                checks,
                f"input_{role}_sqlite_sidecars_absent",
                [],
                [value for value in sidecars if Path(value).exists()],
            )
        pin_actual[role] = {
            "path": relative.as_posix(),
            "size_bytes": size,
            "sha256_before": expected_sha,
            "sha256_after": expected_sha,
            "usage": usage,
        }

    artifact_sidecars = [str(artifact) + suffix for suffix in ("-journal", "-shm", "-wal")]
    core._check(
        checks,
        "v4_sqlite_sidecars_absent",
        [],
        [value for value in artifact_sidecars if Path(value).exists()],
    )
    database = core._open(artifact)
    v3 = core._open(_resolve(root, DEFAULT_V3))
    n100 = core._open(_resolve(root, DEFAULT_N100))
    try:
        integrity = str(database.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
        core._check(checks, "sqlite_integrity", "ok", integrity)
        core._check(checks, "sqlite_foreign_key_check", [], [tuple(row) for row in foreign_keys])
        core._check(
            checks,
            "application_id",
            APPLICATION_ID,
            int(database.execute("PRAGMA application_id").fetchone()[0]),
        )
        core._check(
            checks,
            "schema_version",
            SCHEMA_VERSION,
            int(database.execute("PRAGMA user_version").fetchone()[0]),
        )
        tables = {
            str(row[0])
            for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        core._check(checks, "required_tables", [], sorted(REQUIRED_TABLES - tables))
        forbidden_tables = sorted(
            table
            for table in tables
            if any(fragment in table.lower() for fragment in core.FORBIDDEN_TABLE_FRAGMENTS)
        )
        core._check(checks, "registry_component_crosswalk_tables_absent", [], forbidden_tables)
        expected_columns = {
            "human_review_queue": [
                "run_id", "candidate_id", "proposal", "auto_merge_eligible",
                "queue_reasons_json", "positive_families_json", "anchor_families_json",
                "policy_flags_json", "evidence_strength_band", "record_sha256",
            ],
            "regression_gate_results": [
                "run_id", "gate_name", "expected_case_count", "actual_case_count",
                "passed_case_count", "failed_case_count", "status", "detail_json",
                "record_sha256",
            ],
        }
        for table, expected in expected_columns.items():
            actual = [str(row[1]) for row in database.execute(f"PRAGMA table_info({table})")]
            core._check(checks, f"{table}_columns", expected, actual)

        run_rows = database.execute("SELECT * FROM runs").fetchall()
        core._check(checks, "single_run", 1, len(run_rows))
        if not run_rows:
            raise IdentityCandidateV4ValidationError("v4 has no run row")
        run = run_rows[0]
        run_id = str(run["run_id"])
        expected_run_id = _expected_run_id()
        core._check(checks, "deterministic_run_id", expected_run_id, run_id)
        core._check(
            checks,
            "run_versions",
            (
                CONTRACT_VERSION, BUILDER_VERSION, POLICY_VERSION, NORMALIZER_VERSION,
                SAMPLE_VERSION, SAMPLE_SEED,
            ),
            (
                str(run["contract_version"]), str(run["builder_version"]),
                str(run["policy_version"]), str(run["normalizer_version"]),
                str(run["sample_version"]), str(run["sample_seed"]),
            ),
        )
        core._check(checks, "run_status", "stage_a_complete", str(run["status"]))
        core._check(
            checks,
            "run_policy_hashes",
            (EXPECTED_NORMALIZER_SHA256, EXPECTED_POLICY_SHA256),
            (str(run["normalizer_sha256"]), str(run["policy_sha256"])),
        )
        zero_run_fields = {
            "stage_a_network_requests": int(run["stage_a_network_requests"]),
            "stage_a_vision_requests": int(run["stage_a_vision_requests"]),
            "stage_a_llm_requests": int(run["stage_a_llm_requests"]),
            "legacy_snapshot_reads": int(run["legacy_snapshot_reads"]),
            "merge_executions": int(run["merge_executions"]),
            "vnx_id_issuances": int(run["vnx_id_issuances"]),
            "stage_b_http_requests": int(run["stage_b_http_requests"]),
            "stage_b_vision_requests": int(run["stage_b_vision_requests"]),
            "stage_b_llm_requests": int(run["stage_b_llm_requests"]),
        }
        core._check(checks, "forbidden_run_counters_zero", {key: 0 for key in zero_run_fields}, zero_run_fields)
        core._check(
            checks,
            "run_population_counts",
            (EXPECTED_SOURCE_BUILDINGS, EXPECTED_CANDIDATES),
            (int(run["source_building_count"]), int(run["candidate_count"])),
        )

        stored_pins = {
            str(row["role"]): {
                "path": str(row["path"]),
                "size_bytes": int(row["size_bytes"]),
                "sha256_before": str(row["sha256_before"]),
                "sha256_after": str(row["sha256_after"]),
                "usage": str(row["usage"]),
            }
            for row in database.execute("SELECT * FROM input_pins WHERE run_id=?", (run_id,))
        }
        core._check(checks, "artifact_input_pins_exact", pin_actual, stored_pins)
        core._check(checks, "manifest_input_pins_exact", pin_actual, manifest.get("input_pins"))
        core._check(
            checks,
            "manifest_policy_sha_replay",
            EXPECTED_POLICY_SHA256,
            core._sha(manifest.get("policy_spec")),
        )
        core._check(
            checks,
            "manifest_normalizer_sha_replay",
            EXPECTED_NORMALIZER_SHA256,
            core._sha(manifest.get("normalizer_spec")),
        )
        policy_spec = manifest.get("policy_spec", {})
        required_policy = {
            "rule_application_order": [
                "R1_year", "R2_phash", "anchor", "R3_fanout", "R4_queue", "R5_regression"
            ],
            "R4_queue": "all merge_candidate plus anchored positive-family>=2 defer",
            "R5_unchanged": ["name_only_band", "known_country_conflict", "anchor_requirement"],
            "fresh_n10": False,
        }
        core._check(
            checks,
            "manifest_r1_r5_policy_contract",
            required_policy,
            {key: policy_spec.get(key) for key in required_policy},
        )

        v3_run_id, _buildings, states, expected_groups = _replay_policy(v3)
        core._check(checks, "independent_replay_candidate_count", EXPECTED_CANDIDATES, len(states))
        actual = _load_actual_maps(database, run_id)
        missing_candidates = sorted(set(states) - set(actual["candidates"]))
        extra_candidates = sorted(set(actual["candidates"]) - set(states))
        core._check(
            checks,
            "candidate_universe_exact",
            [],
            _failure_summary(
                [f"missing:{value}" for value in missing_candidates]
                + [f"extra:{value}" for value in extra_candidates]
            ),
        )

        candidate_failures: list[str] = []
        family_failures: list[str] = []
        conflict_failures: list[str] = []
        flag_failures: list[str] = []
        queue_failures: list[str] = []
        static_fields = (
            "left_source", "left_source_building_id", "right_source",
            "right_source_building_id", "origin_e2_metadata", "origin_e2_candidate",
            "origin_matcher_architect_year", "origin_matcher_pass2", "origin_count",
            "e2_metadata_pair_id", "e2_building_candidate_id", "exact_asset_pair_count",
            "identical_phash_pair_count", "phash_le8_pair_count", "phash_9_16_pair_count",
            "min_phash_distance", "name_strict_score", "name_loose_score",
            "name_token_set_score", "firm_strict_score", "year_gap",
        )
        for candidate_id, state in states.items():
            stored = actual["candidates"].get(candidate_id)
            if stored is None:
                continue
            expected_core = (
                str(state.row["proposal"]), int(state.row["auto_merge_eligible"]),
                int(state.row["positive_family_count"]), int(state.row["negative_family_count"]),
                len(state.conflicts),
            )
            stored_core = (
                str(stored["proposal"]), int(stored["auto_merge_eligible"]),
                int(stored["positive_family_count"]), int(stored["negative_family_count"]),
                int(stored["hard_conflict_count"]),
            )
            if expected_core != stored_core or any(
                state.v3_row[field] != stored[field] for field in static_fields
            ):
                candidate_failures.append(candidate_id)
            expected_family_statuses = {
                family: str(value["status"]) for family, value in state.families.items()
            }
            if expected_family_statuses != actual["families"].get(candidate_id, {}):
                family_failures.append(candidate_id)
            if set(state.conflicts) != actual["conflicts"].get(candidate_id, set()):
                conflict_failures.append(candidate_id)
            if set(state.flags) != actual["flags"].get(candidate_id, set()):
                flag_failures.append(candidate_id)
            reasons = list(_queue_reasons(state))
            expected_queue = None
            if reasons:
                expected_queue = {
                    "proposal": str(state.row["proposal"]),
                    "eligible": int(state.row["auto_merge_eligible"]),
                    "reasons": reasons,
                    "positive": list(_family_names(state)),
                    "anchors": list(state.anchor_families),
                    "flags": sorted(state.flags),
                    "band": _evidence_band(state),
                }
            if expected_queue != actual["queues"].get(candidate_id):
                queue_failures.append(candidate_id)
        core._check(checks, "candidate_rows_independent_replay", [], _failure_summary(candidate_failures))
        core._check(checks, "six_family_status_independent_replay", [], _failure_summary(family_failures))
        core._check(checks, "hard_conflicts_independent_replay", [], _failure_summary(conflict_failures))
        core._check(checks, "policy_flags_independent_replay", [], _failure_summary(flag_failures))
        core._check(checks, "human_review_queue_independent_replay", [], _failure_summary(queue_failures))

        family_detail_failures: list[str] = []
        for row in database.execute(
            "SELECT * FROM evidence_families WHERE run_id=? ORDER BY candidate_id,family",
            (run_id,),
        ):
            candidate_id = str(row["candidate_id"])
            family = str(row["family"])
            expected_family = states[candidate_id].families.get(family)
            actual_family = {
                "status": str(row["status"]),
                "score": row["score"],
                "detail": _json_value(row["evidence_json"], {}),
            }
            if expected_family != actual_family:
                family_detail_failures.append(f"{candidate_id}:{family}")
        conflict_detail_failures: list[str] = []
        for row in database.execute(
            "SELECT * FROM hard_conflicts WHERE run_id=? ORDER BY candidate_id,conflict_type",
            (run_id,),
        ):
            candidate_id = str(row["candidate_id"])
            conflict_type = str(row["conflict_type"])
            if states[candidate_id].conflicts.get(conflict_type) != _json_value(
                row["detail_json"], {}
            ):
                conflict_detail_failures.append(f"{candidate_id}:{conflict_type}")
        flag_detail_failures: list[str] = []
        for row in database.execute(
            "SELECT * FROM candidate_policy_flags WHERE run_id=? ORDER BY candidate_id,flag_type",
            (run_id,),
        ):
            candidate_id = str(row["candidate_id"])
            flag_type = str(row["flag_type"])
            if states[candidate_id].flags.get(flag_type) != _json_value(row["detail_json"], {}):
                flag_detail_failures.append(f"{candidate_id}:{flag_type}")
        core._check(
            checks,
            "six_family_details_independent_replay",
            [],
            _failure_summary(family_detail_failures),
        )
        core._check(
            checks,
            "hard_conflict_details_independent_replay",
            [],
            _failure_summary(conflict_detail_failures),
        )
        core._check(
            checks,
            "policy_flag_details_independent_replay",
            [],
            _failure_summary(flag_detail_failures),
        )

        actual_groups = {
            (str(row["hub_source"]), str(row["hub_source_building_id"])): _fanout_semantic(row)
            for row in database.execute("SELECT * FROM fanout_groups WHERE run_id=?", (run_id,))
        }
        expected_group_map = {
            (str(row["hub_source"]), str(row["hub_source_building_id"])): row
            for row in expected_groups
        }
        group_failures = sorted(
            f"{key[0]}:{key[1]}"
            for key in set(actual_groups) | set(expected_group_map)
            if actual_groups.get(key) != expected_group_map.get(key)
        )
        core._check(checks, "fanout_graph_full_replay", [], _failure_summary(group_failures))

        transition_failures: list[str] = []
        transition_count = 0
        transition_counts: Counter[str] = Counter()
        eligibility_counts: Counter[str] = Counter()
        relevant_transitions: list[dict[str, Any]] = []
        decision_changed_ids: list[str] = []
        for row in database.execute(
            "SELECT * FROM candidate_transitions WHERE run_id=? ORDER BY candidate_id", (run_id,)
        ):
            transition_count += 1
            candidate_id = str(row["candidate_id"])
            expected = _expected_transition(states[candidate_id])
            stored = _transition_semantic(dict(row))
            if stored != expected:
                transition_failures.append(candidate_id)
            transition_counts[f"{expected['v3_proposal']}->{expected['v4_proposal']}"] += 1
            eligibility_counts[
                f"{expected['v3_auto_merge_eligible']}->{expected['v4_auto_merge_eligible']}"
            ] += 1
            if expected["decision_changed"]:
                decision_changed_ids.append(candidate_id)
            if expected["r1_changed"] or expected["r2_changed"] or expected["r3_changed"]:
                relevant_transitions.append(expected)
        core._check(checks, "transition_row_count", EXPECTED_CANDIDATES, transition_count)
        core._check(checks, "v3_v4_transition_full_replay", [], _failure_summary(transition_failures))

        record_failures = _record_hash_failures(database, run_id)
        core._check(checks, "all_record_sha256_replay", [], _failure_summary(record_failures))

        expected_proposals = Counter(str(state.row["proposal"]) for state in states.values())
        expected_eligible = sum(int(state.row["auto_merge_eligible"]) for state in states.values())
        expected_queue = [state for state in states.values() if _queue_reasons(state)]
        expected_bands = Counter(_evidence_band(state) for state in expected_queue)
        expected_flags = Counter(flag for state in states.values() for flag in state.flags)
        expected_fanout_status = Counter(str(group["status"]) for group in expected_groups)
        core._check(
            checks,
            "proposal_distribution",
            dict(sorted(expected_proposals.items())),
            {
                str(row[0]): int(row[1])
                for row in database.execute(
                    "SELECT proposal,count(*) FROM candidate_pairs WHERE run_id=? GROUP BY proposal",
                    (run_id,),
                )
            },
        )
        core._check(
            checks,
            "eligible_and_queue_volume",
            (expected_eligible, len(expected_queue)),
            (
                int(database.execute(
                    "SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1",
                    (run_id,),
                ).fetchone()[0]),
                len(actual["queues"]),
            ),
        )

        gold = _n100_gold(n100)
        gate_expectations = _regression_actual(states, gold)
        core._check(
            checks,
            "regression_population_counts",
            EXPECTED_GATE_COUNTS,
            {name: len(rows) for name, rows in gate_expectations.items()},
        )
        failed_gate_cases = sorted(
            f"{gate}:{candidate_id}"
            for gate, rows in gate_expectations.items()
            for candidate_id, passed in rows.items()
            if not passed
        )
        core._check(checks, "four_regression_gates_independent_replay", [], failed_gate_cases)
        expected_binding_rows: dict[tuple[str, str], dict[str, Any]] = {}

        def add_binding(
            gate_name: str,
            subgroup: str,
            rows: Sequence[Mapping[str, Any]],
            expected: Mapping[str, Any],
        ) -> None:
            for item in rows:
                candidate_id = str(item["candidate_id"])
                expected_binding_rows[(gate_name, candidate_id)] = {
                    "case_id": "rgr_"
                    + core._stable(POLICY_VERSION, gate_name, subgroup, candidate_id)[:32],
                    "subgroup": subgroup,
                    "source_artifact_role": "n100_gold_artifact",
                    "source_sample_id": item["sample_id"],
                    "source_stratum": item["stratum"],
                    "sample_origin": item["sample_origin"],
                    "gold_label": item["gold_label"],
                    "sample_record_sha256": item["sample_record_sha256"],
                    "gold_record_sha256": item["gold_record_sha256"],
                    "expected": dict(expected),
                }

        add_binding(
            "b4_name_only_reject", "B4", gold["b4_name_only_reject"],
            {"proposal": "reject"},
        )
        add_binding(
            "b2_different_phash_hard_conflict", "B2_different",
            gold["b2_different_phash_hard_conflict"],
            {"hard_conflict": "verified_phash_veto"},
        )
        b1_rows = [
            row
            for row in gold["b1_b3_human_review_queue"]
            if row["stratum"] == "B1_year_gate"
        ]
        b3_rows = [
            row
            for row in gold["b1_b3_human_review_queue"]
            if row["stratum"] == "B3_fanout_defer"
        ]
        add_binding(
            "b1_b3_human_review_queue", "B1_year", b1_rows,
            {"human_review_queue": True},
        )
        add_binding(
            "b1_b3_human_review_queue", "B3_same", b3_rows,
            {"human_review_queue": True},
        )
        for candidate_id in WP22_REGRESSION_IDS:
            gate_name = "wp22_country_conflict_reject"
            expected_binding_rows[(gate_name, candidate_id)] = _wp22_expected_binding(
                candidate_id
            )
        stored_binding_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in database.execute(
            "SELECT * FROM regression_bindings WHERE run_id=?", (run_id,)
        ):
            stored_binding_rows[(str(row["gate_name"]), str(row["candidate_id"]))] = {
                "case_id": str(row["case_id"]),
                "subgroup": str(row["subgroup"]),
                "source_artifact_role": str(row["source_artifact_role"]),
                "source_sample_id": row["source_sample_id"],
                "source_stratum": row["source_stratum"],
                "sample_origin": row["sample_origin"],
                "gold_label": str(row["gold_label"]),
                "sample_record_sha256": row["sample_record_sha256"],
                "gold_record_sha256": row["gold_record_sha256"],
                "expected": _json_value(row["expected_json"], {}),
            }
        binding_failures = sorted(
            f"{key[0]}:{key[1]}"
            for key in set(expected_binding_rows) | set(stored_binding_rows)
            if expected_binding_rows.get(key) != stored_binding_rows.get(key)
        )
        core._check(
            checks,
            "regression_binding_gold_chain_full_replay",
            [],
            _failure_summary(binding_failures),
        )
        expected_result_rows: dict[str, dict[str, Any]] = {}
        for (gate_name, candidate_id), binding in expected_binding_rows.items():
            state = states[candidate_id]
            hard_conflicts = sorted(state.conflicts)
            policy_flags = sorted(state.flags)
            in_queue = bool(_queue_reasons(state))
            expected_values = binding["expected"]
            passed = True
            result_checks: dict[str, Any] = {}
            if "proposal" in expected_values:
                result_checks["proposal"] = {
                    "expected": expected_values["proposal"],
                    "actual": state.row["proposal"],
                }
                passed = passed and state.row["proposal"] == expected_values["proposal"]
            if "hard_conflict" in expected_values:
                result_checks["hard_conflict"] = {
                    "expected": expected_values["hard_conflict"],
                    "actual": hard_conflicts,
                }
                passed = passed and expected_values["hard_conflict"] in hard_conflicts
            if "human_review_queue" in expected_values:
                result_checks["human_review_queue"] = {
                    "expected": bool(expected_values["human_review_queue"]),
                    "actual": in_queue,
                }
                passed = passed and in_queue == bool(expected_values["human_review_queue"])
            case_id = str(binding["case_id"])
            expected_result_rows[case_id] = {
                "candidate_id": candidate_id,
                "actual_proposal": str(state.row["proposal"]),
                "actual_auto_merge_eligible": int(state.row["auto_merge_eligible"]),
                "actual_hard_conflicts": hard_conflicts,
                "actual_policy_flags": policy_flags,
                "actual_human_review_queue": int(in_queue),
                "passed": int(passed),
                "detail": {
                    "gate_name": gate_name,
                    "subgroup": binding["subgroup"],
                    "checks": result_checks,
                },
            }
        stored_result_rows = {
            str(row["case_id"]): {
                "candidate_id": str(row["candidate_id"]),
                "actual_proposal": str(row["actual_proposal"]),
                "actual_auto_merge_eligible": int(row["actual_auto_merge_eligible"]),
                "actual_hard_conflicts": _json_value(row["actual_hard_conflicts_json"], []),
                "actual_policy_flags": _json_value(row["actual_policy_flags_json"], []),
                "actual_human_review_queue": int(row["actual_human_review_queue"]),
                "passed": int(row["passed"]),
                "detail": _json_value(row["detail_json"], {}),
            }
            for row in database.execute(
                "SELECT * FROM regression_results WHERE run_id=?", (run_id,)
            )
        }
        result_failures = sorted(
            case_id
            for case_id in set(expected_result_rows) | set(stored_result_rows)
            if expected_result_rows.get(case_id) != stored_result_rows.get(case_id)
        )
        core._check(
            checks,
            "regression_results_full_independent_replay",
            [],
            _failure_summary(result_failures),
        )
        stored_gate_rows = {
            str(row["gate_name"]): {
                "gate_name": str(row["gate_name"]),
                "expected_case_count": int(row["expected_case_count"]),
                "actual_case_count": int(row["actual_case_count"]),
                "passed_case_count": int(row["passed_case_count"]),
                "failed_case_count": int(row["failed_case_count"]),
                "status": str(row["status"]),
                "detail": _json_value(row["detail_json"], {}),
            }
            for row in database.execute(
                "SELECT * FROM regression_gate_results WHERE run_id=?", (run_id,)
            )
        }
        expected_gate_rows = {
            name: {
                "gate_name": name,
                "expected_case_count": count,
                "actual_case_count": count,
                "passed_case_count": count,
                "failed_case_count": 0,
                "status": "PASS",
                "detail": {
                    "gate_name": name,
                    "case_ids": sorted(
                        value["case_id"]
                        for (gate_name, _candidate_id), value in expected_binding_rows.items()
                        if gate_name == name
                    ),
                    "failed_case_ids": [],
                },
            }
            for name, count in EXPECTED_GATE_COUNTS.items()
        }
        core._check(checks, "regression_gate_db_results", expected_gate_rows, stored_gate_rows)
        core._check(
            checks,
            "regression_gate_json_results",
            expected_gate_rows,
            gate_report.get("gates"),
        )
        core._check(
            checks,
            "regression_gate_json_header",
            {
                "status": "PASS", "run_id": run_id, "gate_count": 4,
                "passed_gate_count": 4, "failed_gate_count": 0,
                "fresh_n10_run": False, "network_requests": 0,
                "vision_requests": 0, "llm_requests": 0,
            },
            {
                key: gate_report.get(key)
                for key in (
                    "status", "run_id", "gate_count", "passed_gate_count",
                    "failed_gate_count", "fresh_n10_run", "network_requests",
                    "vision_requests", "llm_requests",
                )
            },
        )

        empty_stage_b = {
            table: int(database.execute(f"SELECT count(*) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0])
            for table in ("sample_neighbourhoods", "sample_neighbourhood_pairs", "review_assets", "blind_reviews", "adjudications")
        }
        core._check(checks, "fresh_n10_and_review_rows_zero", {key: 0 for key in empty_stage_b}, empty_stage_b)

        stage_logical = _stage_a_logical(database, run_id)
        full_logical = _full_logical(database, run_id, stage_logical)
        core._check(checks, "run_stage_a_logical_sha", stage_logical, str(run["stage_a_logical_sha256"]))
        core._check(checks, "manifest_stage_a_logical_sha", stage_logical, manifest.get("stage_a_logical_sha256"))
        core._check(checks, "manifest_full_logical_sha", full_logical, manifest.get("full_logical_sha256"))
        core._check(checks, "volume_report_stage_a_logical_sha", stage_logical, stage_report.get("stage_a_logical_sha256"))
        core._check(checks, "volume_report_full_logical_sha", full_logical, stage_report.get("full_logical_sha256"))

        artifact_size = artifact.stat().st_size
        artifact_sha = core._sha_file(artifact) if verify_full_hashes else str(
            manifest.get("stage_a_artifact_sha256", "")
        )
        core._check(checks, "manifest_artifact_size", artifact_size, manifest.get("stage_a_artifact_size_bytes"))
        core._check(checks, "manifest_artifact_sha", artifact_sha, manifest.get("stage_a_artifact_sha256"))
        core._check(checks, "volume_report_artifact_size", artifact_size, stage_report.get("stage_a_artifact_size_bytes"))
        core._check(checks, "volume_report_artifact_sha", artifact_sha, stage_report.get("stage_a_artifact_sha256"))
        core._check(
            checks,
            "regression_gate_json_version_and_artifact",
            {
                "gate_version": "vnext-identity-wp24-four-regression-gates-v1",
                "v4_artifact": {
                    "path": _relative_display(root, artifact),
                    "size_bytes": artifact_size,
                    "sha256": artifact_sha,
                },
            },
            {
                "gate_version": gate_report.get("gate_version"),
                "v4_artifact": gate_report.get("v4_artifact"),
            },
        )

        gate_size = gate_file.stat().st_size if gate_file.is_file() else -1
        gate_sha = core._sha_file(gate_file) if gate_file.is_file() else "missing"
        volume_size = stage_report_file.stat().st_size if stage_report_file.is_file() else -1
        volume_sha = core._sha_file(stage_report_file) if stage_report_file.is_file() else "missing"
        expected_gate_pin = {
            "path": _relative_display(root, gate_file), "size_bytes": gate_size,
            "sha256": gate_sha, "status": "PASS",
        }
        expected_volume_pin = {
            "path": _relative_display(root, stage_report_file), "size_bytes": volume_size,
            "sha256": volume_sha, "status": "PASS",
        }
        core._check(checks, "manifest_regression_gate_pin", expected_gate_pin, manifest.get("regression_gate"))
        core._check(checks, "manifest_volume_report_pin", expected_volume_pin, manifest.get("volume_report"))
        stage_gate = stage_report.get("regression_gate", {})
        core._check(
            checks,
            "volume_report_regression_gate_pin",
            expected_gate_pin,
            {key: stage_gate.get(key) for key in expected_gate_pin},
        )
        core._check(
            checks,
            "volume_report_regression_gate_results",
            expected_gate_rows,
            stage_gate.get("gate_results"),
        )

        expected_rule_impacts = {
            rule.upper(): _rule_impact(relevant_transitions, rule) for rule in ("r1", "r2", "r3")
        }
        core._check(checks, "volume_report_rule_impacts", expected_rule_impacts, stage_report.get("rule_impacts"))
        core._check(
            checks,
            "volume_report_core_volumes",
            {
                "candidate_count": EXPECTED_CANDIDATES,
                "proposal_counts_v4": dict(sorted(expected_proposals.items())),
                "transition_counts": dict(sorted(transition_counts.items())),
                "eligibility_transition_counts": dict(sorted(eligibility_counts.items())),
                "auto_merge_eligible_v4": expected_eligible,
                "merge_candidate_total": expected_proposals["merge_candidate"],
                "decision_changed_count": len(decision_changed_ids),
                "policy_flag_counts": dict(sorted(expected_flags.items())),
                "fanout_group_status_counts": dict(sorted(expected_fanout_status.items())),
            },
            {
                key: stage_report.get(key)
                for key in (
                    "candidate_count", "proposal_counts_v4", "transition_counts",
                    "eligibility_transition_counts", "auto_merge_eligible_v4",
                    "merge_candidate_total", "decision_changed_count", "policy_flag_counts",
                    "fanout_group_status_counts",
                )
            },
        )
        queue_report = stage_report.get("human_review_queue", {})
        core._check(
            checks,
            "volume_report_queue",
            {
                "total": len(expected_queue),
                "proposal_counts": dict(sorted(Counter(state.row["proposal"] for state in expected_queue).items())),
                "evidence_strength_bands": dict(sorted(expected_bands.items())),
                "policy_flag_subsets_overlapping": dict(
                    sorted(Counter(flag for state in expected_queue for flag in state.flags).items())
                ),
                "definition": "all merge_candidate plus anchored positive-family>=2 defer",
                "human_approval_required": True,
            },
            queue_report,
        )

        forbidden = {
            "network_requests": 0, "vision_requests": 0, "llm_requests": 0,
            "legacy_snapshot_reads": 0, "merge_executions": 0, "vnx_id_issuances": 0,
            "fresh_n10_runs": 0, "neon_writes": 0, "r2_writes": 0,
        }
        core._check(checks, "manifest_forbidden_actions_zero", forbidden, manifest.get("forbidden_actions"))
        core._check(checks, "manifest_human_approval_required", True, manifest.get("human_approval_required"))
        core._check(
            checks,
            "manifest_versions_and_status",
            ("PASS", run_id, CONTRACT_VERSION, BUILDER_VERSION, POLICY_VERSION, NORMALIZER_VERSION),
            (
                manifest.get("status"), manifest.get("run_id"), manifest.get("contract_version"),
                manifest.get("builder_version"), manifest.get("policy_version"),
                manifest.get("normalizer_version"),
            ),
        )

        metrics = {
            "candidate_count": len(states),
            "proposal_counts": dict(sorted(expected_proposals.items())),
            "auto_merge_eligible": expected_eligible,
            "human_review_queue": len(expected_queue),
            "evidence_strength_bands": dict(sorted(expected_bands.items())),
            "policy_flags": dict(sorted(expected_flags.items())),
            "fanout_groups": dict(sorted(expected_fanout_status.items())),
            "decision_changed": len(decision_changed_ids),
            "regression_gate_counts": EXPECTED_GATE_COUNTS,
            "stage_a_logical_sha256": stage_logical,
            "full_logical_sha256": full_logical,
            "artifact_size_bytes": artifact_size,
            "artifact_sha256": artifact_sha,
            "verify_full_hashes": verify_full_hashes,
            "network_requests": 0,
            "vision_requests": 0,
            "llm_requests": 0,
        }
    finally:
        database.close()
        v3.close()
        n100.close()

    result = core.ValidationResult(
        artifact,
        validation_file,
        run_id if "run_id" in locals() else None,
        tuple(checks),
        metrics if "metrics" in locals() else {},
    )
    if validation_file is not None:
        payload = {
            "status": "PASS" if result.passed else "FAIL",
            "validator_version": VALIDATOR_VERSION,
            "run_id": result.run_id,
            "artifact": artifact.relative_to(root).as_posix(),
            "verify_full_hashes": verify_full_hashes,
            "passed_checks": sum(check.passed for check in result.checks),
            "total_checks": len(result.checks),
            "failed_checks": list(result.failed_check_names),
            "checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "expected": core._json_safe(check.expected),
                    "actual": core._json_safe(check.actual),
                    "detail": check.detail,
                }
                for check in result.checks
            ],
            "metrics": core._json_safe(result.metrics),
        }
        _write_json_no_clobber(validation_file, payload)
    return result


__all__ = [
    "DEFAULT_ARTIFACT",
    "DEFAULT_MANIFEST",
    "DEFAULT_REGRESSION_GATE_REPORT",
    "DEFAULT_REPORT",
    "DEFAULT_STAGE_A_REPORT",
    "IdentityCandidateV4ValidationError",
    "VALIDATOR_VERSION",
    "validate_identity_candidates_v4",
]
