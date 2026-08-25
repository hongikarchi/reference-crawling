"""Independent validator for the WP2.2 v3 identity-candidate artifact.

This module deliberately does not import the v3 producer.  It loads the
SHA-pinned country specification from the manifest, reconstructs all 91,803
source buildings and 68,049 cross-source candidates from the frozen inputs,
and independently replays the country, anchor, frozen fan-out, transition,
and fresh-N10 policies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from canonical import vnext_identity_candidates_validator as core


VALIDATOR_VERSION = "vnext-identity-wp22-independent-validator-v3"
APPLICATION_ID = 0x564E5832
SCHEMA_VERSION = 3
CONTRACT_VERSION = "vnext-identity-contract-wp22-v3"
BUILDER_VERSION = "vnext-identity-candidates-builder-v3.1"
NORMALIZER_VERSION = "vnext-identity-country-iso3166-v3"
POLICY_VERSION = "vnext-identity-anchor-fanout-policy-v3"
SAMPLE_VERSION = "vnext-identity-wp22-fresh-n10-v3"
SAMPLE_SEED = "vnext-wp22-country-anchor-fanout-fresh-n10-20260825"
FULL_LOGICAL_VERSION = "vnext-identity-stage-a-wp22-transition-logical-v3.1"

EXPECTED_NORMALIZER_SHA256 = "968c9e089293b3f6180947ae5fe792d8ebc91adc794017094920df9bc0e3602e"
EXPECTED_POLICY_SHA256 = "c1162891a18e1d692fd5880a71f7fee2f7d1c07acfb56fce60e60a42bd6d0264"
V2_SHA256 = "08e28e4e5a1ab5aba9ea5ddd53ca615f417458a6693cdfb1aefc822d57b5b0c4"

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_candidates_v3.db")
DEFAULT_V2 = Path("data/enrichment/vnext_identity_candidates_v2.db")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_candidates_v3_manifest.json")
DEFAULT_STAGE_A_REPORT = Path("data/reports/vnext_identity_candidates_v3_stage_a_report.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_candidates_v3_validation.json")
DEFAULT_GATE_REPORT = Path("data/reports/vnext_identity_candidates_v3_n10_gate.json")

WP22_REGRESSION_IDS = frozenset({
    "idc_016b59163550fcd3bfe2dcfde694745a",
    "idc_31831329e77427910c173d3142b4dabe",
    "idc_55b2f5fc5c91de7d9239c392bc3f08b3",
    "idc_4142468fdbf954126c43359abad4e3b3",
})
EXPECTED_STRATA = {
    "new_known_country_conflict": 2,
    "fanout_demotion": 1,
    "anchor_ineligible": 1,
    "strong_merge": 2,
    "metadata_only": 1,
    "image_evidence_only": 1,
    "conflict_boundary": 2,
}
EXPECTED_COUNTS = {"source_buildings": 91_803, "candidate_pairs": 68_049}

REQUIRED_TABLES = core.REQUIRED_TABLES | {
    "country_normalizations", "prior_review_exclusions", "candidate_policy_flags",
    "fanout_groups", "candidate_transitions",
}
FULL_LOGICAL_TABLES = (
    ("country_normalizations", "source,source_building_id"),
    ("prior_review_exclusions", "candidate_id,prior_version"),
    ("candidate_policy_flags", "candidate_id,flag_type"),
    ("fanout_groups", "hub_source,hub_source_building_id"),
    ("candidate_transitions", "candidate_id"),
)

# These are copied from the v3 manifest contract, not imported from the
# producer.  The validator checks both the artifact rows and the manifest
# representation against this independent pin set.
INPUT_PINS: Mapping[str, tuple[Path, int, str, str]] = {
    "architizer_curated_metadata": (
        Path("data/curated/architizer_curated_v2_0.db"), 8_767_438_848,
        "605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc",
        "candidate_and_evidence",
    ),
    "build_strict_country_aliases": (
        Path("tools/build_strict_canonical.py"), 25_498,
        "97aedf10755fa80fdef6abc39651da200cc6e19e573ba78b980dfd55bde67d06",
        "normalizer_legacy_provenance",
    ),
    "building_matcher": (
        Path("canonical/match_buildings_sequential.py"), 26_812,
        "3cf9592a9e54cd4e07512461ca08dc5ca7f8dd5e6cd97b2d6c118dd0b265d87b",
        "candidate_blocking",
    ),
    "c17_country_aliases_ext": (
        Path("tools/canonical_v2_c17_make_web_polish.py"), 16_202,
        "13334b47ae4e2c9b4909984b2f1e591249ab384b7c4027668441c4a66edce569",
        "normalizer_legacy_provenance",
    ),
    "c21_country_normalizer": (
        Path("tools/canonical_v2_c21_make_web_polish.py"), 19_534,
        "60c188bbae3a5fadf03d72de919fe5ad41bf0d3a75b9d72d7ce0058222cbbb92",
        "normalizer_legacy_provenance",
    ),
    "c23_known_countries": (
        Path("tools/canonical_v2_c23_final.py"), 15_197,
        "f8adf319e388119c659a8c04435e1a1b4e5f056baf560aea37d9ede50cebb0c5",
        "normalizer_legacy_provenance",
    ),
    "country_normalizer_module": (
        Path("canonical/vnext_country_norm.py"), 22_896,
        "98e3cf971525f17393d772f45bf52ac3dfe37dbb5d4bc593019483eafab36015",
        "normalizer_implementation",
    ),
    "cross_source_e2_full_v5": (
        Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"), 10_164_682_752,
        "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19",
        "candidate_and_evidence",
    ),
    "divisare_curated_metadata": (
        Path("data/curated/divisare_metadata_v2_4.db"), 2_225_299_456,
        "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f",
        "candidate_and_evidence",
    ),
    "identity_contract": (
        Path("docs/vnext/CONTRACT_IDENTITY.md"), 8_165,
        "0f31e6586f9ab862ab7f00ac22d21fb457f7d49e1dae24df29da79d737a978b3",
        "policy_contract",
    ),
    "phash_matcher": (
        Path("canonical/match_phash_check.py"), 5_450,
        "3dcc73152d822fe2b8eaccbb333625672378a3f6cfa59d3099cda0ca28e64fe7",
        "image_policy_provenance",
    ),
    "v1_builder_code": (
        Path("canonical/vnext_identity_candidates.py"), 75_798,
        "3997998b029fc1306ab5a632102af96594ef3034fa6ef44ef88543eb40179447",
        "shared_stage_a_engine_provenance",
    ),
    "v2_builder_code": (
        Path("canonical/vnext_identity_candidates_v2.py"), 38_264,
        "a967763ae8dc947d7e9d91d949d64645a9a18ae967a28e1928f036f6d4975f35",
        "comparison_artifact_provenance",
    ),
    "v2_candidate_artifact": (
        DEFAULT_V2, 581_292_032, V2_SHA256,
        "comparison_and_prior_review_exclusion_only",
    ),
    "workspace_freeze_manifest": (
        Path("data/reports/vnext_workspace_freeze_v1.json"), 246_838,
        "cd787a0088161fd0e8dddfe23e1943c0a41992f1e1a214b67fcce5d3ae17dcc3",
        "pin_manifest_only",
    ),
}


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _country_key(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).replace("\u00a0", " ").split()).casefold()


class _CountryClassifier:
    """Executable country classifier reconstructed only from the pinned manifest."""

    def __init__(self, normalizer_spec: Mapping[str, Any]):
        country = normalizer_spec.get("country")
        if not isinstance(country, Mapping):
            raise ValueError("manifest normalizer_spec.country is missing")
        iso = country.get("iso_3166_1")
        if not isinstance(iso, Mapping):
            raise ValueError("manifest ISO specification is missing")
        self.country_spec = country
        self.native = {str(k): str(v) for k, v in dict(country.get("native_aliases", {})).items()}
        self.extended = {str(k): str(v) for k, v in dict(country.get("extended_aliases", {})).items()}
        self.iso_aliases = {str(k): str(v) for k, v in dict(iso.get("executable_aliases", {})).items()}
        self.known = {str(x) for x in country.get("known_countries", [])}
        self.legacy = {str(x) for x in country.get("legacy_known_countries", [])}
        self.ambiguous = {str(x) for x in country.get("ambiguous_country_keys", [])}

    def classify(self, value: Any) -> tuple[str, str | None, str]:
        raw = "" if value is None else str(value).strip()
        if not raw:
            return "unknown", None, "empty"
        key = _country_key(raw)
        if key in self.ambiguous:
            return "ambiguous", None, "fixed_ambiguous_key"
        canonical = self.native.get(key)
        if canonical is None:
            canonical = self.extended.get(key, self.iso_aliases.get(key, raw))
        if canonical in self.known:
            reason = "legacy_known_country" if canonical in self.legacy else "iso_3166_1_known_country"
            return "known", canonical, reason
        return "unknown", None, "not_in_pinned_iso_country_set"


def _best_full(left_values: Sequence[str], right_values: Sequence[str]) -> tuple[float, float, float, str, str]:
    best: tuple[float, float, float, str, str] = (0.0, 0.0, 0.0, "", "")
    for left in left_values:
        for right in right_values:
            strict, loose, token_set = core._sim(left, right)
            candidate = (strict, loose, token_set, left, right)
            if candidate[:3] > best[:3]:
                best = candidate
    return best


def _image_veto(
    left: Mapping[str, Any], right: Mapping[str, Any],
    left_stats: Mapping[str, Any], right_stats: Mapping[str, Any], strong: int,
) -> tuple[bool, str | None]:
    if strong:
        return False, None
    left_n = int(left_stats.get("unique_pixel_count", 0))
    right_n = int(right_stats.get("unique_pixel_count", 0))
    if left_n >= core.PHASH_STRONG_MIN and right_n >= core.PHASH_STRONG_MIN:
        return True, "strong_3x3_zero_direct_overlap"
    primary_set = core._sim(core._norm(left["name"], ascii_fold=True), core._norm(right["name"], ascii_fold=True))[2]
    gap = core._year_gap(left, right)
    if (
        left_n >= core.PHASH_THIN_MIN and right_n >= core.PHASH_THIN_MIN
        and primary_set < core.NAME_THRESHOLD and gap is not None
        and gap > core.PASS2_YEAR_TOLERANCE
    ):
        return True, "thin_2x2_text_confirmed_zero_direct_overlap"
    return False, None


def _evaluate_candidate(
    left: Mapping[str, Any], right: Mapping[str, Any], origins: set[str],
    summary: Mapping[str, Any], left_stats: Mapping[str, Any], right_stats: Mapping[str, Any],
) -> dict[str, Any]:
    """Independent six-family + country + anchor replay (before fan-out)."""
    name_strict, name_loose, name_set, name_left, name_right = _best_full(left["alias_norms"], right["alias_norms"])
    firm_strict, firm_loose, _firm_set, firm_left, firm_right = _best_full(left["firm_keys"], right["firm_keys"])
    exact_firms = sorted(set(left["firm_keys"]) & set(right["firm_keys"]))
    firm_positive = bool(exact_firms) or firm_strict >= core.FIRM_THRESHOLD
    gap = core._year_gap(left, right)
    exact = int(summary.get("exact", 0) or 0)
    identical = int(summary.get("identical", 0) or 0)
    le8 = int(summary.get("le8", 0) or 0)
    weak = int(summary.get("weak", 0) or 0)
    strong = exact + identical + le8
    veto, veto_reason = _image_veto(left, right, left_stats, right_stats, strong)

    conflicts: list[dict[str, Any]] = []
    if left["country_norm"] and right["country_norm"] and left["country_norm"] != right["country_norm"]:
        conflicts.append({
            "conflict_type": "country_site_incompatible",
            "detail": {
                "left_country": left["country"], "right_country": right["country"],
                "left_country_canonical": left["country_norm"],
                "right_country_canonical": right["country_norm"], "known_on_both_sides": True,
            },
        })
    if firm_positive and gap is not None and gap > core.YEAR_BLOCK_TOLERANCE:
        conflicts.append({
            "conflict_type": "matcher_architect_year_gate_block",
            "detail": {"year_gap": gap, "tolerance": core.YEAR_BLOCK_TOLERANCE},
        })
    if gap is not None and gap > core.PHASE_HARD_GAP:
        conflicts.append({
            "conflict_type": "project_phase_incompatible",
            "detail": {"year_gap": gap, "hard_gap": core.PHASE_HARD_GAP},
        })
    if veto:
        conflicts.append({
            "conflict_type": "verified_phash_veto",
            "detail": {
                "reason": veto_reason,
                "left_unique_pixels": int(left_stats.get("unique_pixel_count", 0)),
                "right_unique_pixels": int(right_stats.get("unique_pixel_count", 0)),
                "direct_overlap_count": strong,
            },
        })

    site_positive = bool(
        left["country_norm"] and left["country_norm"] == right["country_norm"]
        and left["locality_norm"] and left["locality_norm"] == right["locality_norm"]
    )
    site_negative = any(row["conflict_type"] == "country_site_incompatible" for row in conflicts)
    year_positive = gap is not None and gap <= core.YEAR_POSITIVE_TOLERANCE
    year_negative = gap is not None and gap > core.PHASE_HARD_GAP
    families = [
        {
            "family": "name_alias_identity", "status": "positive" if name_strict >= core.NAME_THRESHOLD else "unknown",
            "score": round(name_strict, 6),
            "detail": {"strict": round(name_strict, 6), "loose": round(name_loose, 6),
                       "token_set": round(name_set, 6), "left_alias": name_left, "right_alias": name_right,
                       "threshold": core.NAME_THRESHOLD},
        },
        {
            "family": "architect_firm_identity", "status": "positive" if firm_positive else "unknown",
            "score": round(firm_strict, 6),
            "detail": {"strict": round(firm_strict, 6), "loose": round(firm_loose, 6),
                       "left_key": firm_left, "right_key": firm_right, "exact_keys": exact_firms,
                       "threshold": core.FIRM_THRESHOLD},
        },
        {
            "family": "location_site_identity",
            "status": "negative" if site_negative else ("positive" if site_positive else "unknown"),
            "score": 1.0 if site_positive else (0.0 if site_negative else None),
            "detail": {
                "left_country": left["country"], "right_country": right["country"],
                "left_locality": left["locality"], "right_locality": right["locality"],
                "rule": "both-known-canonical; equal+exact-locality positive; unequal conflict; otherwise neutral",
                "left_country_status": left["country_status"], "right_country_status": right["country_status"],
                "left_country_canonical": left["country_norm"] or None,
                "right_country_canonical": right["country_norm"] or None,
            },
        },
        {
            "family": "year_phase_compatibility",
            "status": "negative" if year_negative else ("positive" if year_positive else "unknown"),
            "score": None if gap is None else float(gap),
            "detail": {"left_year_min": left["year_min"], "left_year_max": left["year_max"],
                       "right_year_min": right["year_min"], "right_year_max": right["year_max"],
                       "year_gap": gap, "positive_tolerance": core.YEAR_POSITIVE_TOLERANCE},
        },
        {
            "family": "source_url_external_stable_id", "status": "unknown", "score": None,
            "detail": {"left_source_urls": list(left["urls"]), "right_source_urls": list(right["urls"]),
                       "left_external_ids": list(left["external_ids"]), "right_external_ids": list(right["external_ids"]),
                       "reason": "only source-namespaced IDs/URLs are present; no independent shared stable ID"},
        },
        {
            "family": "verified_pixel_image_overlap",
            "status": "negative" if veto else ("positive" if strong else "unknown"),
            "score": float(strong) if strong else None,
            "detail": {"exact_asset_pair_count": exact, "identical_phash_pair_count": identical,
                       "phash_le8_pair_count": le8, "phash_9_16_review_only_count": weak,
                       "direct_only": True, "phash_veto": veto, "phash_veto_reason": veto_reason},
        },
    ]
    positive = sum(row["status"] == "positive" for row in families)
    negative = sum(row["status"] == "negative" for row in families)
    conflict_types = {row["conflict_type"] for row in conflicts}
    if not conflicts and positive >= 2:
        proposal = "merge_candidate"
    elif conflicts:
        related = (
            name_strict >= core.NAME_THRESHOLD and (firm_positive or site_positive)
            and bool(conflict_types & {"matcher_architect_year_gate_block", "project_phase_incompatible"})
            and "country_site_incompatible" not in conflict_types
        )
        proposal = "related_but_separate" if related else "reject"
    else:
        proposal = "defer"
    anchor_families: list[str] = []
    if name_strict >= core.NAME_THRESHOLD:
        anchor_families.append("name_alias_identity")
    if strong > 0:
        anchor_families.append("verified_pixel_image_overlap")
    auto = int(positive >= 2 and not conflicts)
    flags: list[dict[str, Any]] = []
    if auto and not anchor_families:
        auto = 0
        flags.append({
            "flag_type": "anchor_missing",
            "detail": {"positive_family_count": positive,
                       "required_anchor_families": ["name_alias_identity", "verified_pixel_image_overlap"],
                       "name_strict_score": round(name_strict, 6), "strong_image_count": strong,
                       "proposal_preserved": proposal},
        })
    return {
        "origins": sorted(origins), "name_strict": name_strict, "name_loose": name_loose,
        "name_set": name_set, "firm_strict": firm_strict, "year_gap": gap,
        "strong": strong, "weak": weak, "families": families, "conflicts": conflicts,
        "positive": positive, "negative": negative, "hard": len(conflicts),
        "proposal": proposal, "auto": auto, "anchor_families": anchor_families,
        "flags": flags,
    }


def _same_source_relation(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    name_strict, _nl, _ns, name_left, name_right = _best_full(left["alias_norms"], right["alias_norms"])
    firm_strict, _fl, _fs, firm_left, firm_right = _best_full(left["firm_keys"], right["firm_keys"])
    exact_firms = sorted(set(left["firm_keys"]) & set(right["firm_keys"]))
    firm_positive = bool(exact_firms) or firm_strict >= core.FIRM_THRESHOLD
    gap = core._year_gap(left, right)
    country_conflict = bool(left["country_norm"] and right["country_norm"] and left["country_norm"] != right["country_norm"])
    year_gate = bool(firm_positive and gap is not None and gap > core.YEAR_BLOCK_TOLERANCE)
    phase = bool(gap is not None and gap > core.PHASE_HARD_GAP)
    site_positive = bool(
        left["country_norm"] and left["country_norm"] == right["country_norm"]
        and left["locality_norm"] and left["locality_norm"] == right["locality_norm"]
    )
    year_positive = bool(gap is not None and gap <= core.YEAR_POSITIVE_TOLERANCE)
    additional = []
    if firm_positive:
        additional.append("architect_firm_identity")
    if site_positive:
        additional.append("location_site_identity")
    if year_positive:
        additional.append("year_phase_compatibility")
    conflicts = []
    if country_conflict:
        conflicts.append("country_site_incompatible")
    if year_gate:
        conflicts.append("matcher_architect_year_gate_block")
    if phase:
        conflicts.append("project_phase_incompatible")
    related = bool(name_strict >= core.NAME_THRESHOLD and additional and not conflicts)
    return related, {
        "related": related, "name_strict_score": round(name_strict, 6),
        "name_left_alias": name_left, "name_right_alias": name_right,
        "name_threshold": core.NAME_THRESHOLD, "additional_positive_families": additional,
        "firm_strict_score": round(firm_strict, 6), "firm_left_key": firm_left,
        "firm_right_key": firm_right, "exact_firm_keys": exact_firms,
        "year_gap": gap, "hard_conflicts": conflicts,
    }


def _apply_fanout(
    evaluated: Mapping[str, dict[str, Any]], buildings: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in evaluated.values():
        if not row["auto"]:
            continue
        grouped[("divisare", str(row["left_id"]))].append(row)
        grouped[("architizer", str(row["right_id"]))].append(row)
    triggers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    groups: list[dict[str, Any]] = []
    for (hub_source, hub_id), incident in sorted(grouped.items()):
        if len(incident) < 2:
            continue
        opponent_source = "architizer" if hub_source == "divisare" else "divisare"
        opponent_ids = sorted({str(row["right_id"] if hub_source == "divisare" else row["left_id"]) for row in incident})
        pair_checks = []
        unrelated = []
        for first_id, second_id in combinations(opponent_ids, 2):
            related, detail = _same_source_relation(
                buildings[(opponent_source, first_id)], buildings[(opponent_source, second_id)]
            )
            pair_checks.append({"first_source_building_id": first_id, "second_source_building_id": second_id, **detail})
            if not related:
                unrelated.append([first_id, second_id])
        status = "conflict" if unrelated else "exempt"
        incident_ids = sorted(str(row["candidate_id"]) for row in incident)
        detail = {
            "snapshot": "post_country_post_anchor_pre_fanout", "hub_source": hub_source,
            "hub_source_building_id": hub_id, "pre_guard_degree": len(incident),
            "opponent_source": opponent_source, "incident_candidate_ids": incident_ids,
            "opponent_source_building_ids": opponent_ids, "pair_checks": pair_checks,
            "all_opponent_pairs_direct_related": not unrelated, "transitive_closure_used": False,
        }
        group = {
            "hub_source": hub_source, "hub_source_building_id": hub_id,
            "pre_guard_degree": len(incident), "status": status,
            "incident_candidate_ids": incident_ids, "opponent_source_building_ids": opponent_ids,
            "unrelated_opponent_pairs": unrelated, "detail": detail,
        }
        groups.append(group)
        if status == "conflict":
            trigger = {"hub_source": hub_source, "hub_source_building_id": hub_id,
                       "pre_guard_degree": len(incident), "unrelated_opponent_pairs": unrelated}
            for row in incident:
                triggers[str(row["candidate_id"])].append(trigger)
    for candidate_id, candidate_triggers in sorted(triggers.items()):
        row = evaluated[candidate_id]
        detail = {"snapshot": "post_country_post_anchor_pre_fanout", "triggers": candidate_triggers}
        row["conflicts"].append({"conflict_type": "fanout_conflict", "detail": detail})
        row["hard"] = len(row["conflicts"])
        row["proposal"] = "defer"
        row["auto"] = 0
        row["flags"].append({"flag_type": "fanout_conflict", "detail": detail})
    return groups


def _full_logical(connection: sqlite3.Connection, run_id: str, stage_a_logical: str) -> str:
    digest = hashlib.sha256()
    digest.update((FULL_LOGICAL_VERSION + "\n" + stage_a_logical + "\n").encode("utf-8"))
    for table, order in FULL_LOGICAL_TABLES:
        digest.update((table + "\n").encode("utf-8"))
        for row in connection.execute(
            f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)
        ):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _v2_maps(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    candidates = {
        str(row["candidate_id"]): dict(row)
        for row in connection.execute("SELECT * FROM candidate_pairs WHERE run_id=?", (run_id,))
    }
    locations = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT candidate_id,status FROM evidence_families WHERE run_id=? AND family='location_site_identity'",
            (run_id,),
        )
    }
    origins: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        "SELECT candidate_id,origin FROM candidate_origins WHERE run_id=?", (run_id,)
    ):
        origins[str(row[0])].add(str(row[1]))
    conflicts: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        "SELECT candidate_id,conflict_type FROM hard_conflicts WHERE run_id=?", (run_id,)
    ):
        conflicts[str(row[0])].add(str(row[1]))
    country_norm = {
        (str(row["source"]), str(row["source_building_id"])): (
            str(row["normalization_status"]), str(row["canonical_country"] or "")
        )
        for row in connection.execute("SELECT * FROM country_normalizations WHERE run_id=?", (run_id,))
    }
    return {"candidates": candidates, "locations": locations, "origins": origins,
            "conflicts": conflicts, "country_norm": country_norm}


def _expected_transitions(
    v2: sqlite3.Connection, v2_run_id: str, evaluated: Mapping[str, Mapping[str, Any]],
    buildings: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    old_maps = _v2_maps(v2, v2_run_id)
    if set(old_maps["candidates"]) != set(evaluated):
        raise ValueError("v2/v3 candidate universe differs")
    transitions: dict[str, dict[str, Any]] = {}
    for candidate_id in sorted(evaluated):
        new = evaluated[candidate_id]
        old = old_maps["candidates"][candidate_id]
        left_id, right_id = str(new["left_id"]), str(new["right_id"])
        old_conflicts = sorted(old_maps["conflicts"].get(candidate_id, set()))
        new_conflicts = sorted(row["conflict_type"] for row in new["conflicts"])
        old_origins = sorted(old_maps["origins"].get(candidate_id, set()))
        new_origins = sorted(str(origin) for origin in new["origins"])
        new_location = next(row["status"] for row in new["families"] if row["family"] == "location_site_identity")
        new_country = {
            ("divisare", left_id): (
                str(buildings[("divisare", left_id)]["country_status"]),
                str(buildings[("divisare", left_id)]["country_norm"]),
            ),
            ("architizer", right_id): (
                str(buildings[("architizer", right_id)]["country_status"]),
                str(buildings[("architizer", right_id)]["country_norm"]),
            ),
        }
        country_set_changed = any(
            old_maps["country_norm"].get(endpoint) != new_country[endpoint]
            for endpoint in new_country
        )
        flag_types = {str(row["flag_type"]) for row in new["flags"]}
        country_conflict_added = (
            "country_site_incompatible" not in old_conflicts
            and "country_site_incompatible" in new_conflicts
        )
        anchor_ineligible = "anchor_missing" in flag_types
        fanout_demoted = "fanout_conflict" in flag_types
        origins_changed = old_origins != new_origins
        proposal_changed = str(old["proposal"]) != str(new["proposal"])
        old_location = old_maps["locations"].get(candidate_id)
        decision_changed = bool(
            proposal_changed or int(old["auto_merge_eligible"]) != int(new["auto"])
            or old_conflicts != new_conflicts or old_location != new_location
        )
        reasons = []
        if country_set_changed:
            reasons.append("country_set_changed")
        if country_conflict_added:
            reasons.append("country_conflict_added")
        if anchor_ineligible:
            reasons.append("anchor_ineligible")
        if fanout_demoted:
            reasons.append("fanout_demoted")
        if origins_changed:
            reasons.append("origins_changed")
        if proposal_changed:
            reasons.append("proposal_changed")
        if int(old["auto_merge_eligible"]) != int(new["auto"]):
            reasons.append("auto_merge_eligibility_changed")
        if old_location != new_location:
            reasons.append("location_family_status_changed")
        transitions[candidate_id] = {
            "candidate_id": candidate_id, "left_source_building_id": left_id,
            "right_source_building_id": right_id, "v2_origins": old_origins, "v3_origins": new_origins,
            "v2_location_status": old_location,
            "v3_location_status": new_location, "v2_hard_conflicts": old_conflicts,
            "v3_hard_conflicts": new_conflicts, "v2_proposal": str(old["proposal"]),
            "v3_proposal": str(new["proposal"]),
            "v2_auto_merge_eligible": int(old["auto_merge_eligible"]),
            "v3_auto_merge_eligible": int(new["auto"]),
            "country_set_changed": int(country_set_changed),
            "country_conflict_added": int(country_conflict_added),
            "anchor_ineligible": int(anchor_ineligible), "fanout_demoted": int(fanout_demoted),
            "origins_changed": int(origins_changed),
            "proposal_changed": int(proposal_changed), "decision_changed": int(decision_changed),
            "reasons": reasons,
        }
    return transitions


def _prior_exclusions(v2: sqlite3.Connection, v2_run_id: str) -> tuple[list[dict[str, str]], set[str]]:
    rows: list[dict[str, str]] = []
    for row in v2.execute(
        "SELECT candidate_id,v1_opaque_review_id FROM v1_review_exclusions WHERE run_id=? ORDER BY candidate_id",
        (v2_run_id,),
    ):
        rows.append({"candidate_id": str(row[0]), "prior_version": "v1", "prior_opaque_review_id": str(row[1])})
    for row in v2.execute(
        "SELECT candidate_id,opaque_review_id FROM sample_neighbourhoods WHERE run_id=? ORDER BY sample_rank",
        (v2_run_id,),
    ):
        rows.append({"candidate_id": str(row[0]), "prior_version": "v2", "prior_opaque_review_id": str(row[1])})
    return rows, {row["candidate_id"] for row in rows}


def _select_fresh_n10(
    evaluated: Mapping[str, Mapping[str, Any]], transitions: Mapping[str, Mapping[str, Any]], excluded: set[str],
) -> list[tuple[str, str]]:
    selected: list[tuple[str, Mapping[str, Any]]] = []
    used_left: set[str] = set()
    used_right: set[str] = set()

    def transition(row: Mapping[str, Any]) -> Mapping[str, Any]:
        return transitions[str(row["candidate_id"])]

    def add(stratum: str, count: int, predicate: Any, preference: Any) -> None:
        pool = [row for row in evaluated.values() if str(row["candidate_id"]) not in excluded and predicate(row)]
        pool.sort(key=lambda row: (preference(row), core._stable(SAMPLE_SEED, stratum, str(row["candidate_id"]))))
        for row in pool:
            if str(row["left_id"]) in used_left or str(row["right_id"]) in used_right:
                continue
            selected.append((stratum, row))
            used_left.add(str(row["left_id"]))
            used_right.add(str(row["right_id"]))
            if sum(item[0] == stratum for item in selected) == count:
                return
        raise ValueError("fresh N10 stratum insufficient: " + stratum)

    add(
        "new_known_country_conflict", 2,
        lambda row: bool(transition(row)["country_conflict_added"]),
        lambda row: (0 if row["candidate_id"] in WP22_REGRESSION_IDS else 1,
                     0 if transition(row)["v2_auto_merge_eligible"] else 1, -int(row["positive"])),
    )
    add(
        "fanout_demotion", 1, lambda row: bool(transition(row)["fanout_demoted"]),
        lambda row: (-int(transition(row)["v2_auto_merge_eligible"]), -int(row["positive"])),
    )
    add(
        "anchor_ineligible", 1,
        lambda row: bool(transition(row)["anchor_ineligible"]) and not transition(row)["fanout_demoted"],
        lambda row: (0 if row["proposal"] == "merge_candidate" else 1, -int(row["positive"])),
    )
    add(
        "strong_merge", 2,
        lambda row: bool(row["auto"]) and row["strong"] > 0 and row["name_strict"] >= 90,
        lambda row: (-int(row["positive"]), -int(row["strong"])),
    )
    add(
        "metadata_only", 1,
        lambda row: "e2_metadata" in row["origins"] and row["strong"] == 0,
        lambda row: (-int(row["positive"]), int(row["hard"])),
    )
    add(
        "image_evidence_only", 1,
        lambda row: "e2_candidate" in row["origins"] and "e2_metadata" not in row["origins"] and row["strong"] > 0,
        lambda row: (int("matcher_architect_year" in row["origins"] or "matcher_pass2" in row["origins"]), -int(row["strong"])),
    )
    add(
        "conflict_boundary", 2,
        lambda row: row["hard"] > 0 or (row["proposal"] == "defer" and row["positive"] == 1),
        lambda row: (0 if row["hard"] > 0 else 1, int(row["hard"]), -int(row["positive"])),
    )
    return [(stratum, str(row["candidate_id"])) for stratum, row in selected]


def _transition_spec(row: Mapping[str, Any], buildings: Mapping[tuple[str, str], Mapping[str, Any]]) -> dict[str, Any]:
    left = buildings[("divisare", str(row["left_source_building_id"]))]
    right = buildings[("architizer", str(row["right_source_building_id"]))]
    return {
        "candidate_id": row["candidate_id"],
        "left_source_building_id": row["left_source_building_id"],
        "right_source_building_id": row["right_source_building_id"],
        "left_name": left["name"], "right_name": right["name"],
        "left_country_raw": left["country"], "right_country_raw": right["country"],
        "left_country_status": left["country_status"], "right_country_status": right["country_status"],
        "left_country_canonical": left["country_norm"] or None,
        "right_country_canonical": right["country_norm"] or None,
        "v2_origins": row["v2_origins"], "v3_origins": row["v3_origins"],
        "v2_location_status": row["v2_location_status"], "v3_location_status": row["v3_location_status"],
        "v2_hard_conflicts": row["v2_hard_conflicts"], "v3_hard_conflicts": row["v3_hard_conflicts"],
        "v2_proposal": row["v2_proposal"], "v3_proposal": row["v3_proposal"],
        "v2_auto_merge_eligible": row["v2_auto_merge_eligible"],
        "v3_auto_merge_eligible": row["v3_auto_merge_eligible"],
        "country_set_changed": row["country_set_changed"], "country_conflict_added": row["country_conflict_added"],
        "anchor_ineligible": row["anchor_ineligible"], "fanout_demoted": row["fanout_demoted"],
        "origins_changed": row["origins_changed"],
        "proposal_changed": row["proposal_changed"], "decision_changed": row["decision_changed"],
        "reasons": row["reasons"],
    }


def _report_value(value: Any) -> Any:
    """Keep validation reports auditable without serializing 68k-row sets."""
    safe = core._json_safe(value)
    if isinstance(safe, list) and len(safe) > 50:
        return {"count": len(safe), "canonical_sha256": core._sha(safe), "sample": safe[:10]}
    if isinstance(safe, dict) and len(safe) > 50:
        keys = sorted(safe)[:10]
        return {"count": len(safe), "canonical_sha256": core._sha(safe),
                "sample": {key: safe[key] for key in keys}}
    if isinstance(safe, list):
        return [_report_value(item) for item in safe]
    if isinstance(safe, dict):
        return {str(key): _report_value(item) for key, item in safe.items()}
    return safe


def _write_report(result: core.ValidationResult, report: Path) -> None:
    if report.exists():
        raise FileExistsError(f"refusing to clobber validation report: {report}")
    payload = {
        "status": "PASS" if result.passed else "FAIL", "validator_version": VALIDATOR_VERSION,
        "artifact_path": str(result.artifact_path), "run_id": result.run_id,
        "passed_checks": sum(row.passed for row in result.checks), "total_checks": len(result.checks),
        "failed_checks": list(result.failed_check_names),
        "checks": [{"name": row.name, "passed": row.passed, "expected": _report_value(row.expected),
                    "actual": _report_value(row.actual), "detail": row.detail} for row in result.checks],
        "metrics": _report_value(result.metrics),
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = report.with_name(report.name + ".building")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report)


def validate_identity_candidates_v3(
    *, repo_root: Path, artifact_path: Path = DEFAULT_ARTIFACT,
    report_path: Path | None = DEFAULT_REPORT, verify_full_hashes: bool = True,
) -> core.ValidationResult:
    root = repo_root.resolve()
    artifact = artifact_path if artifact_path.is_absolute() else root / artifact_path
    report = None if report_path is None else (report_path if report_path.is_absolute() else root / report_path)
    checks: list[core.Check] = []
    metrics: dict[str, Any] = {}
    run_id: str | None = None
    file_hashes: dict[Path, str] = {}
    if not artifact.is_file():
        core._check(checks, "artifact_exists", True, False)
        return core.ValidationResult(artifact, report, None, tuple(checks), metrics)

    manifest_path = root / DEFAULT_MANIFEST
    stage_report_path = root / DEFAULT_STAGE_A_REPORT
    manifest = _load_json(manifest_path)
    stage_report = _load_json(stage_report_path)
    core._check(checks, "manifest_exists_and_is_json", True, manifest is not None)
    core._check(checks, "stage_a_report_exists_and_is_json", True, stage_report is not None)
    if manifest is None:
        result = core.ValidationResult(artifact, report, None, tuple(checks), metrics)
        if report is not None:
            _write_report(result, report)
        return result

    normalizer_spec = manifest.get("normalizer_spec")
    policy_spec = manifest.get("policy_spec")
    normalizer_actual_sha = core._sha(normalizer_spec) if isinstance(normalizer_spec, Mapping) else "invalid"
    policy_actual_sha = core._sha(policy_spec) if isinstance(policy_spec, Mapping) else "invalid"
    core._check(checks, "manifest_normalizer_spec_sha", EXPECTED_NORMALIZER_SHA256, normalizer_actual_sha)
    core._check(checks, "manifest_policy_spec_sha", EXPECTED_POLICY_SHA256, policy_actual_sha)
    try:
        classifier = _CountryClassifier(normalizer_spec if isinstance(normalizer_spec, Mapping) else {})
    except ValueError as exc:
        core._check(checks, "manifest_country_spec_executable", True, False, str(exc))
        result = core.ValidationResult(artifact, report, None, tuple(checks), metrics)
        if report is not None:
            _write_report(result, report)
        return result
    core._check(checks, "manifest_country_spec_executable", True, True)

    country_spec = classifier.country_spec
    iso_spec = country_spec.get("iso_3166_1", {})
    canonical_by_code = dict(iso_spec.get("canonical_by_alpha2", {})) if isinstance(iso_spec, Mapping) else {}
    executable_aliases = dict(iso_spec.get("executable_aliases", {})) if isinstance(iso_spec, Mapping) else {}
    additions = manifest.get("additions", {})
    expected_additions = country_spec.get("additions")
    core._check(checks, "iso_snapshot_counts_249_173_11", (249, 173, 11), (
        int(iso_spec.get("entry_count", -1)), int(iso_spec.get("official_name_count", -1)),
        int(iso_spec.get("common_name_count", -1)),
    ))
    core._check(checks, "iso_alpha2_canonical_count", 249, len(canonical_by_code))
    core._check(checks, "iso_executable_alias_count", 424, len(executable_aliases))
    core._check(checks, "iso_known_country_count", 248, len(classifier.known))
    core._check(checks, "legacy_known_country_count", 89, len(classifier.legacy))
    core._check(checks, "manifest_additions_match_normalizer_spec", expected_additions, additions)
    core._check(checks, "iso_additions_counts_159_398", (159, 398), (
        len(additions.get("known_countries", [])) if isinstance(additions, Mapping) else -1,
        len(additions.get("aliases", [])) if isinstance(additions, Mapping) else -1,
    ))
    core._check(checks, "iso_known_additions_exact_set_difference",
                sorted(classifier.known - classifier.legacy), additions.get("known_countries", []))
    expected_alias_additions = [
        {"alias_key": key, "canonical": canonical}
        for key, canonical in sorted(classifier.iso_aliases.items())
        if key not in classifier.extended and key not in classifier.native
    ]
    core._check(checks, "iso_alias_additions_exact_diff", expected_alias_additions, additions.get("aliases", []))
    country_regressions = {
        value: classifier.classify(value)
        for value in ("Senegal", "Republic of Senegal", "Ecuador", "Republic of Ecuador", "Georgia")
    }
    core._check(checks, "senegal_iso_known", ("known", "Senegal"), country_regressions["Senegal"][:2])
    core._check(checks, "senegal_official_name_known", ("known", "Senegal"), country_regressions["Republic of Senegal"][:2])
    core._check(checks, "ecuador_iso_known", ("known", "Ecuador"), country_regressions["Ecuador"][:2])
    core._check(checks, "ecuador_official_name_known", ("known", "Ecuador"), country_regressions["Republic of Ecuador"][:2])
    core._check(checks, "georgia_remains_ambiguous", ("ambiguous", None, "fixed_ambiguous_key"), country_regressions["Georgia"])

    connection = core._open(artifact)
    v2 = core._open(root / DEFAULT_V2)
    divisare = core._open(root / core.DIVISARE)
    architizer = core._open(root / core.ARCHITIZER)
    e2 = core._open(root / core.E2)
    try:
        core._check(checks, "sqlite_application_id", APPLICATION_ID, int(connection.execute("PRAGMA application_id").fetchone()[0]))
        core._check(checks, "sqlite_schema_version", SCHEMA_VERSION, int(connection.execute("PRAGMA user_version").fetchone()[0]))
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        core._check(checks, "required_tables", True, REQUIRED_TABLES <= tables)
        forbidden = sorted(table for table in tables if any(fragment in table.casefold() for fragment in core.FORBIDDEN_TABLE_FRAGMENTS))
        core._check(checks, "no_registry_component_crosswalk_tables", [], forbidden)
        run_rows = connection.execute("SELECT * FROM runs").fetchall()
        core._check(checks, "single_run", 1, len(run_rows))
        if len(run_rows) != 1:
            result = core.ValidationResult(artifact, report, None, tuple(checks), metrics)
            if report is not None:
                _write_report(result, report)
            return result
        run = run_rows[0]
        run_id = str(run["run_id"])
        version_actual = tuple(str(run[column]) for column in (
            "contract_version", "builder_version", "normalizer_version", "policy_version",
            "sample_version", "sample_seed",
        ))
        version_expected = (
            CONTRACT_VERSION, BUILDER_VERSION, NORMALIZER_VERSION, POLICY_VERSION, SAMPLE_VERSION, SAMPLE_SEED,
        )
        core._check(checks, "run_versions", version_expected, version_actual)
        core._check(checks, "run_normalizer_sha", EXPECTED_NORMALIZER_SHA256, str(run["normalizer_sha256"]))
        core._check(checks, "run_policy_sha", EXPECTED_POLICY_SHA256, str(run["policy_sha256"]))
        forbidden_counts = tuple(int(run[column]) for column in (
            "stage_a_network_requests", "stage_a_vision_requests", "stage_a_llm_requests",
            "legacy_snapshot_reads", "merge_executions", "vnx_id_issuances",
        ))
        core._check(checks, "stage_a_forbidden_actions_zero", (0, 0, 0, 0, 0, 0), forbidden_counts)
        core._check(checks, "run_status_supported", True, str(run["status"]) in {"stage_a_complete", "pass", "fail"})
        core._check(checks, "foreign_key_check", [], [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")])
        core._check(checks, "sqlite_integrity", "ok", str(connection.execute("PRAGMA integrity_check").fetchone()[0]))

        stored_pins = {str(row["role"]): row for row in connection.execute("SELECT * FROM input_pins WHERE run_id=?", (run_id,))}
        manifest_pins = manifest.get("input_pins", {})
        pin_failures: list[str] = []
        sidecars: list[str] = []
        if set(stored_pins) != set(INPUT_PINS):
            pin_failures.append("artifact_role_set")
        if not isinstance(manifest_pins, Mapping) or set(manifest_pins) != set(INPUT_PINS):
            pin_failures.append("manifest_role_set")
        for role, (relative, size, digest, usage) in INPUT_PINS.items():
            path = root / relative
            if not path.is_file() or path.stat().st_size != size:
                pin_failures.append(role + ":missing_or_size")
                continue
            if path.suffix == ".db":
                for suffix in ("-wal", "-shm", "-journal"):
                    if Path(str(path) + suffix).exists():
                        sidecars.append(relative.as_posix() + suffix)
            actual_digest = core._cached_file_sha(path, file_hashes, verify_full_hashes, digest)
            if actual_digest != digest:
                pin_failures.append(role + ":file_sha")
            row = stored_pins.get(role)
            expected_tuple = (relative.as_posix(), size, digest, digest, usage)
            actual_tuple = None if row is None else tuple(row[column] for column in (
                "path", "size_bytes", "sha256_before", "sha256_after", "usage",
            ))
            payload = {"role": role, "path": relative.as_posix(), "size_bytes": size,
                       "sha256_before": digest, "sha256_after": digest, "usage": usage}
            if actual_tuple != expected_tuple or (row is not None and str(row["record_sha256"]) != core._sha(payload)):
                pin_failures.append(role + ":artifact_record")
            manifest_row = manifest_pins.get(role) if isinstance(manifest_pins, Mapping) else None
            expected_manifest = {"path": relative.as_posix(), "size_bytes": size,
                                 "sha256_before": digest, "sha256_after": digest, "usage": usage}
            if manifest_row != expected_manifest:
                pin_failures.append(role + ":manifest_record")
        core._check(checks, "input_pins_manifest_artifact_and_files", [], pin_failures)
        core._check(checks, "stage_a_inputs_no_sqlite_sidecars", [], sidecars)
        core._check(checks, "v2_artifact_sha256_immutable", V2_SHA256,
                    core._sha_file(root / DEFAULT_V2) if verify_full_hashes else V2_SHA256)

        buildings = core._load_buildings(divisare, architizer, e2)
        for building in buildings.values():
            status, canonical, reason = classifier.classify(building["country"])
            building["country_norm"] = canonical or ""
            building["country_status"] = status
            building["country_reason"] = reason
        stats = core._image_stats(e2)
        universe, summaries = core._universe(buildings, e2)
        core._check(checks, "source_population_recomputed", EXPECTED_COUNTS["source_buildings"], len(buildings))
        core._check(checks, "candidate_population_recomputed", EXPECTED_COUNTS["candidate_pairs"], len(universe))
        core._check(checks, "run_population_counts", (91_803, 68_049),
                    (int(run["source_building_count"]), int(run["candidate_count"])))

        stored_sources = {
            (str(row["source"]), str(row["source_building_id"])): row
            for row in connection.execute("SELECT * FROM source_buildings WHERE run_id=?", (run_id,))
        }
        stored_stats = {
            (str(row["source"]), str(row["source_building_id"])): row
            for row in connection.execute("SELECT * FROM building_image_stats WHERE run_id=?", (run_id,))
        }
        stored_country = {
            (str(row["source"]), str(row["source_building_id"])): row
            for row in connection.execute("SELECT * FROM country_normalizations WHERE run_id=?", (run_id,))
        }
        source_failures: list[str] = []
        stats_failures: list[str] = []
        country_failures: list[str] = []
        for key, building in buildings.items():
            label = key[0] + "/" + key[1]
            source_payload = {
                "source": key[0], "source_building_id": key[1], "name": building["name"],
                "aliases": list(building["aliases"]), "firms": list(building["firms"]),
                "firm_keys": list(building["firm_keys"]), "country": building["country"],
                "country_normalized": building["country_norm"], "locality": building["locality"],
                "locality_normalized": building["locality_norm"], "completion_year_min": building["year_min"],
                "completion_year_max": building["year_max"], "source_urls": list(building["urls"]),
                "external_ids": list(building["external_ids"]), "e2_source_record_sha256": building["e2_sha"],
            }
            source_row = stored_sources.get(key)
            source_expected = (
                building["name"], core._json(list(building["aliases"])), core._json(list(building["firms"])),
                core._json(list(building["firm_keys"])), building["country"], building["country_norm"],
                building["locality"], building["locality_norm"], building["year_min"], building["year_max"],
                core._json(list(building["urls"])), core._json(list(building["external_ids"])), building["e2_sha"],
                core._sha(source_payload),
            )
            source_actual = None if source_row is None else tuple(source_row[column] for column in (
                "name", "aliases_json", "firms_json", "firm_keys_json", "country", "country_normalized",
                "locality", "locality_normalized", "completion_year_min", "completion_year_max",
                "source_urls_json", "external_ids_json", "e2_source_record_sha256", "record_sha256",
            ))
            if source_actual != source_expected and len(source_failures) < 10:
                source_failures.append(label)
            source_stat = stats.get(key, {"asset_count": 0, "successful_asset_count": 0,
                                          "unique_pixel_count": 0, "representative": None})
            representative = source_stat.get("representative")
            stat_payload = {"source": key[0], "source_building_id": key[1],
                            "asset_count": int(source_stat["asset_count"]),
                            "successful_asset_count": int(source_stat["successful_asset_count"]),
                            "unique_pixel_count": int(source_stat["unique_pixel_count"]),
                            "representative": representative}
            stat_row = stored_stats.get(key)
            stat_expected = (stat_payload["asset_count"], stat_payload["successful_asset_count"],
                             stat_payload["unique_pixel_count"], representative.get("source_asset_id") if representative else None,
                             core._json(representative) if representative else None, core._sha(stat_payload))
            stat_actual = None if stat_row is None else tuple(stat_row[column] for column in (
                "asset_count", "successful_asset_count", "unique_pixel_count", "representative_source_asset_id",
                "representative_json", "record_sha256",
            ))
            if stat_actual != stat_expected and len(stats_failures) < 10:
                stats_failures.append(label)
            country_payload = {"source": key[0], "source_building_id": key[1],
                               "raw_country": str(building["country"] or ""),
                               "normalization_status": building["country_status"],
                               "canonical_country": building["country_norm"] or None,
                               "reason": building["country_reason"]}
            country_row = stored_country.get(key)
            country_expected = (country_payload["raw_country"], country_payload["normalization_status"],
                                country_payload["canonical_country"], country_payload["reason"], core._sha(country_payload))
            country_actual = None if country_row is None else tuple(country_row[column] for column in (
                "raw_country", "normalization_status", "canonical_country", "reason", "record_sha256",
            ))
            if country_actual != country_expected and len(country_failures) < 10:
                country_failures.append(label)
        core._check(checks, "source_rows_and_record_sha_replay", [], source_failures)
        core._check(checks, "image_stats_rows_and_record_sha_replay", [], stats_failures)
        core._check(checks, "country_rows_and_record_sha_replay", [], country_failures)
        core._check(checks, "country_status_partition", len(buildings), sum(
            int(value) for _status, value in connection.execute(
                "SELECT normalization_status,count(*) FROM country_normalizations WHERE run_id=? GROUP BY normalization_status",
                (run_id,),
            )
        ))

        # E2 lineage details omitted by the compact independent universe helper.
        lineage: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
        for row in e2.execute("SELECT * FROM metadata_building_pairs ORDER BY metadata_pair_id"):
            pair = core._endpoint(row)
            lineage[pair].update(metadata_pair_id=str(row["metadata_pair_id"]),
                                 e2_metadata_record_sha256=str(row["metadata_record_sha256"]))
        for row in e2.execute("SELECT * FROM cross_source_building_candidates ORDER BY building_candidate_id"):
            pair = core._endpoint(row)
            lineage[pair].update(
                e2_building_candidate_id=str(row["building_candidate_id"]),
                metadata_pair_id=row["metadata_pair_id"],
                e2_candidate_record_sha256=str(row["candidate_record_sha256"]),
            )

        evaluated: dict[str, dict[str, Any]] = {}
        for left_id, right_id in sorted(universe):
            cid = core._candidate_id(left_id, right_id)
            decision = _evaluate_candidate(
                buildings[("divisare", left_id)], buildings[("architizer", right_id)], set(universe[(left_id, right_id)]),
                summaries.get((left_id, right_id), {}), stats.get(("divisare", left_id), {}),
                stats.get(("architizer", right_id), {}),
            )
            evaluated[cid] = {**decision, "candidate_id": cid, "left_id": left_id, "right_id": right_id,
                              "summary": summaries.get((left_id, right_id), {}),
                              "lineage": lineage.get((left_id, right_id), {})}
        fanout_groups = _apply_fanout(evaluated, buildings)

        artifact_pairs = {
            (str(row["left_source_building_id"]), str(row["right_source_building_id"])): row
            for row in connection.execute("SELECT * FROM candidate_pairs WHERE run_id=?", (run_id,))
        }
        stored_origins = {(str(row["candidate_id"]), str(row["origin"])): row for row in connection.execute(
            "SELECT * FROM candidate_origins WHERE run_id=?", (run_id,))}
        stored_families = {(str(row["candidate_id"]), str(row["family"])): row for row in connection.execute(
            "SELECT * FROM evidence_families WHERE run_id=?", (run_id,))}
        stored_conflicts = {(str(row["candidate_id"]), str(row["conflict_type"])): row for row in connection.execute(
            "SELECT * FROM hard_conflicts WHERE run_id=?", (run_id,))}
        stored_flags = {(str(row["candidate_id"]), str(row["flag_type"])): row for row in connection.execute(
            "SELECT * FROM candidate_policy_flags WHERE run_id=?", (run_id,))}
        core._check(checks, "candidate_universe_exact", set(universe), set(artifact_pairs))
        candidate_failures: list[str] = []
        origin_failures: list[str] = []
        family_failures: list[str] = []
        conflict_failures: list[str] = []
        flag_failures: list[str] = []
        expected_origin_keys: set[tuple[str, str]] = set()
        expected_family_keys: set[tuple[str, str]] = set()
        expected_conflict_keys: set[tuple[str, str]] = set()
        expected_flag_keys: set[tuple[str, str]] = set()
        matcher_sha = INPUT_PINS["building_matcher"][2]
        for cid, decision in evaluated.items():
            pair = (str(decision["left_id"]), str(decision["right_id"]))
            summary = decision["summary"]
            lineage_row = decision["lineage"]
            counts = {"exact": int(summary.get("exact", 0) or 0), "identical": int(summary.get("identical", 0) or 0),
                      "le8": int(summary.get("le8", 0) or 0), "weak": int(summary.get("weak", 0) or 0)}
            candidate_payload = {
                "candidate_id": cid, "left_source_building_id": pair[0], "right_source_building_id": pair[1],
                "origins": sorted(decision["origins"]), "e2_metadata_pair_id": lineage_row.get("metadata_pair_id"),
                "e2_building_candidate_id": lineage_row.get("e2_building_candidate_id"), "image_counts": counts,
                "min_phash_distance": summary.get("min_distance"),
                "name_strict_score": round(decision["name_strict"], 6),
                "name_loose_score": round(decision["name_loose"], 6),
                "name_token_set_score": round(decision["name_set"], 6),
                "firm_strict_score": round(decision["firm_strict"], 6), "year_gap": decision["year_gap"],
                "positive_family_count": decision["positive"], "negative_family_count": decision["negative"],
                "hard_conflict_count": decision["hard"], "proposal": decision["proposal"],
                "auto_merge_eligible": decision["auto"], "anchor_families": list(decision["anchor_families"]),
                "policy_flags": sorted(row["flag_type"] for row in decision["flags"]),
            }
            row = artifact_pairs.get(pair)
            expected_tuple = (
                cid, int("e2_metadata" in decision["origins"]), int("e2_candidate" in decision["origins"]),
                int("matcher_architect_year" in decision["origins"]), int("matcher_pass2" in decision["origins"]),
                len(decision["origins"]), lineage_row.get("metadata_pair_id"), lineage_row.get("e2_building_candidate_id"),
                counts["exact"], counts["identical"], counts["le8"], counts["weak"], summary.get("min_distance"),
                round(decision["name_strict"], 6), round(decision["name_loose"], 6), round(decision["name_set"], 6),
                round(decision["firm_strict"], 6), decision["year_gap"], decision["positive"], decision["negative"],
                decision["hard"], decision["proposal"], decision["auto"], core._sha(candidate_payload),
            )
            actual_tuple = None if row is None else tuple(row[column] for column in (
                "candidate_id", "origin_e2_metadata", "origin_e2_candidate", "origin_matcher_architect_year",
                "origin_matcher_pass2", "origin_count", "e2_metadata_pair_id", "e2_building_candidate_id",
                "exact_asset_pair_count", "identical_phash_pair_count", "phash_le8_pair_count", "phash_9_16_pair_count",
                "min_phash_distance", "name_strict_score", "name_loose_score", "name_token_set_score",
                "firm_strict_score", "year_gap", "positive_family_count", "negative_family_count", "hard_conflict_count",
                "proposal", "auto_merge_eligible", "record_sha256",
            ))
            if actual_tuple != expected_tuple and len(candidate_failures) < 10:
                candidate_failures.append(cid)
            for origin in sorted(decision["origins"]):
                key = (cid, origin); expected_origin_keys.add(key)
                detail = {"origin": origin, "pinned_matcher_sha256": matcher_sha if origin.startswith("matcher_") else None,
                          "e2_metadata_record_sha256": lineage_row.get("e2_metadata_record_sha256") if origin == "e2_metadata" else None,
                          "e2_candidate_record_sha256": lineage_row.get("e2_candidate_record_sha256") if origin == "e2_candidate" else None}
                payload = {"candidate_id": cid, "origin": origin, "detail": detail}
                stored = stored_origins.get(key)
                actual = None if stored is None else (str(stored["origin_detail_json"]), str(stored["record_sha256"]))
                expected = (core._json(detail), core._sha(payload))
                if actual != expected and len(origin_failures) < 10:
                    origin_failures.append(cid + "/" + origin)
            for family in decision["families"]:
                key = (cid, str(family["family"])); expected_family_keys.add(key)
                payload = {"candidate_id": cid, "family": family["family"], "status": family["status"],
                           "score": family["score"], "detail": family["detail"]}
                stored = stored_families.get(key)
                actual = None if stored is None else (stored["status"], stored["score"], stored["evidence_json"], stored["record_sha256"])
                expected = (family["status"], family["score"], core._json(family["detail"]), core._sha(payload))
                if actual != expected and len(family_failures) < 10:
                    family_failures.append(cid + "/" + str(family["family"]))
            for conflict in decision["conflicts"]:
                key = (cid, str(conflict["conflict_type"])); expected_conflict_keys.add(key)
                payload = {"candidate_id": cid, "conflict_type": conflict["conflict_type"], "detail": conflict["detail"]}
                stored = stored_conflicts.get(key)
                actual = None if stored is None else (stored["detail_json"], stored["record_sha256"])
                expected = (core._json(conflict["detail"]), core._sha(payload))
                if actual != expected and len(conflict_failures) < 10:
                    conflict_failures.append(cid + "/" + str(conflict["conflict_type"]))
            for flag in decision["flags"]:
                key = (cid, str(flag["flag_type"])); expected_flag_keys.add(key)
                payload = {"candidate_id": cid, "flag_type": flag["flag_type"], "detail": flag["detail"]}
                stored = stored_flags.get(key)
                actual = None if stored is None else (stored["detail_json"], stored["record_sha256"])
                expected = (core._json(flag["detail"]), core._sha(payload))
                if actual != expected and len(flag_failures) < 10:
                    flag_failures.append(cid + "/" + str(flag["flag_type"]))
        core._check(checks, "candidate_rows_and_record_sha_replay", [], candidate_failures)
        core._check(checks, "candidate_origins_keyset", expected_origin_keys, set(stored_origins))
        core._check(checks, "candidate_origins_and_record_sha_replay", [], origin_failures)
        core._check(checks, "six_family_keyset", expected_family_keys, set(stored_families))
        core._check(checks, "six_family_rows_and_record_sha_replay", [], family_failures)
        core._check(checks, "hard_conflict_keyset", expected_conflict_keys, set(stored_conflicts))
        core._check(checks, "hard_conflicts_and_record_sha_replay", [], conflict_failures)
        core._check(checks, "policy_flag_keyset", expected_flag_keys, set(stored_flags))
        core._check(checks, "policy_flags_and_record_sha_replay", [], flag_failures)
        core._check(checks, "anchor_required_for_every_auto_merge", 0, sum(
            row["auto"] and not row["anchor_families"] for row in evaluated.values()
        ))
        core._check(checks, "auto_merge_hard_conflict_zero", 0, sum(
            row["auto"] and row["hard"] for row in evaluated.values()
        ))

        stored_groups = {(str(row["hub_source"]), str(row["hub_source_building_id"])): row for row in connection.execute(
            "SELECT * FROM fanout_groups WHERE run_id=?", (run_id,))}
        group_failures: list[str] = []
        expected_group_keys = {(str(group["hub_source"]), str(group["hub_source_building_id"])) for group in fanout_groups}
        for group in fanout_groups:
            key = (str(group["hub_source"]), str(group["hub_source_building_id"]))
            payload = {"hub_source": group["hub_source"], "hub_source_building_id": group["hub_source_building_id"],
                       "pre_guard_degree": group["pre_guard_degree"], "status": group["status"],
                       "incident_candidate_ids": group["incident_candidate_ids"],
                       "opponent_source_building_ids": group["opponent_source_building_ids"],
                       "unrelated_opponent_pairs": group["unrelated_opponent_pairs"], "detail": group["detail"]}
            stored = stored_groups.get(key)
            expected = (group["pre_guard_degree"], group["status"], core._json(group["incident_candidate_ids"]),
                        core._json(group["opponent_source_building_ids"]), core._json(group["unrelated_opponent_pairs"]),
                        core._json(group["detail"]), core._sha(payload))
            actual = None if stored is None else tuple(stored[column] for column in (
                "pre_guard_degree", "status", "incident_candidate_ids_json", "opponent_source_building_ids_json",
                "unrelated_opponent_pairs_json", "detail_json", "record_sha256",
            ))
            if actual != expected and len(group_failures) < 10:
                group_failures.append(key[0] + "/" + key[1])
        core._check(checks, "fanout_group_keyset", expected_group_keys, set(stored_groups))
        core._check(checks, "frozen_pairwise_clique_fanout_replay", [], group_failures)

        # Direct image evidence is independently projected from E2.
        e2_to_candidate = {
            str(info["e2_building_candidate_id"]): core._candidate_id(pair[0], pair[1])
            for pair, info in lineage.items() if info.get("e2_building_candidate_id")
        }
        stored_direct = {str(row["e2_evidence_id"]): row for row in connection.execute(
            "SELECT * FROM direct_image_evidence WHERE run_id=?", (run_id,))}
        direct_failures: list[str] = []
        expected_direct_ids: set[str] = set()
        for row in e2.execute(
            "SELECT evidence_id,building_candidate_id,evidence_kind,left_source,left_source_asset_id,"
            "right_source,right_source_asset_id,phash_distance,evidence_record_sha256 "
            "FROM candidate_image_evidence ORDER BY evidence_id"
        ):
            evidence_id = str(row["evidence_id"]); expected_direct_ids.add(evidence_id)
            cid = e2_to_candidate[str(row["building_candidate_id"])]
            if row["left_source"] == "divisare":
                left_asset, right_asset = str(row["left_source_asset_id"]), str(row["right_source_asset_id"])
            else:
                left_asset, right_asset = str(row["right_source_asset_id"]), str(row["left_source_asset_id"])
            payload = {"candidate_id": cid, "e2_evidence_id": evidence_id, "evidence_kind": str(row["evidence_kind"]),
                       "left_source_asset_id": left_asset, "right_source_asset_id": right_asset,
                       "phash_distance": row["phash_distance"],
                       "e2_evidence_record_sha256": str(row["evidence_record_sha256"])}
            stored = stored_direct.get(evidence_id)
            expected = (cid, payload["evidence_kind"], left_asset, right_asset, payload["phash_distance"],
                        payload["e2_evidence_record_sha256"], core._sha(payload))
            actual = None if stored is None else tuple(stored[column] for column in (
                "candidate_id", "evidence_kind", "left_source_asset_id", "right_source_asset_id",
                "phash_distance", "e2_evidence_record_sha256", "record_sha256",
            ))
            if actual != expected and len(direct_failures) < 10:
                direct_failures.append(evidence_id)
        core._check(checks, "direct_image_evidence_keyset", expected_direct_ids, set(stored_direct))
        core._check(checks, "direct_image_evidence_and_record_sha_replay", [], direct_failures)

        v2_run_rows = v2.execute("SELECT run_id FROM runs").fetchall()
        core._check(checks, "v2_single_run", 1, len(v2_run_rows))
        v2_run_id = str(v2_run_rows[0][0])
        transitions = _expected_transitions(v2, v2_run_id, evaluated, buildings)
        v2_maps_for_metrics = _v2_maps(v2, v2_run_id)
        stored_transitions = {str(row["candidate_id"]): row for row in connection.execute(
            "SELECT * FROM candidate_transitions WHERE run_id=?", (run_id,))}
        transition_failures: list[str] = []
        for cid, payload in transitions.items():
            stored = stored_transitions.get(cid)
            expected = (
                payload["left_source_building_id"], payload["right_source_building_id"],
                core._json(payload["v2_origins"]), core._json(payload["v3_origins"]),
                payload["v2_location_status"], payload["v3_location_status"],
                core._json(payload["v2_hard_conflicts"]), core._json(payload["v3_hard_conflicts"]),
                payload["v2_proposal"], payload["v3_proposal"], payload["v2_auto_merge_eligible"],
                payload["v3_auto_merge_eligible"], payload["country_set_changed"], payload["country_conflict_added"],
                payload["anchor_ineligible"], payload["fanout_demoted"], payload["origins_changed"], payload["proposal_changed"],
                payload["decision_changed"], core._json(payload["reasons"]), core._sha(payload),
            )
            actual = None if stored is None else tuple(stored[column] for column in (
                "left_source_building_id", "right_source_building_id", "v2_origins_json", "v3_origins_json",
                "v2_location_status", "v3_location_status",
                "v2_hard_conflicts_json", "v3_hard_conflicts_json", "v2_proposal", "v3_proposal",
                "v2_auto_merge_eligible", "v3_auto_merge_eligible", "country_set_changed", "country_conflict_added",
                "anchor_ineligible", "fanout_demoted", "origins_changed", "proposal_changed", "decision_changed",
                "reasons_json", "record_sha256",
            ))
            if actual != expected and len(transition_failures) < 10:
                transition_failures.append(cid)
        core._check(checks, "candidate_transition_keyset", set(transitions), set(stored_transitions))
        core._check(checks, "candidate_transitions_full_replay", [], transition_failures)
        core._check(checks, "origin_changed_pair_count", 7,
                    sum(row["origins_changed"] for row in transitions.values()))

        exclusion_rows, exclusion_set = _prior_exclusions(v2, v2_run_id)
        expected_exclusions = {(row["candidate_id"], row["prior_version"]): row for row in exclusion_rows}
        stored_exclusions = {(str(row["candidate_id"]), str(row["prior_version"])): row for row in connection.execute(
            "SELECT * FROM prior_review_exclusions WHERE run_id=?", (run_id,))}
        exclusion_failures: list[str] = []
        for key, payload in expected_exclusions.items():
            stored = stored_exclusions.get(key)
            expected = (payload["prior_opaque_review_id"], core._sha(payload))
            actual = None if stored is None else (str(stored["prior_opaque_review_id"]), str(stored["record_sha256"]))
            if actual != expected:
                exclusion_failures.append("/".join(key))
        core._check(checks, "prior_review_exclusion_keyset", set(expected_exclusions), set(stored_exclusions))
        core._check(checks, "prior_review_exclusion_records", [], exclusion_failures)

        expected_sample = _select_fresh_n10(evaluated, transitions, exclusion_set)
        sample_rows = connection.execute(
            "SELECT * FROM sample_neighbourhoods WHERE run_id=? ORDER BY sample_rank", (run_id,)
        ).fetchall()
        actual_sample = [(str(row["stratum"]), str(row["candidate_id"])) for row in sample_rows]
        core._check(checks, "fresh_disjoint_n10_exact", expected_sample, actual_sample)
        core._check(checks, "fresh_n10_strata", EXPECTED_STRATA, dict(sorted(Counter(x[0] for x in actual_sample).items())))
        core._check(checks, "fresh_n10_prior_review_overlap_zero", set(), {cid for _stratum, cid in actual_sample} & exclusion_set)
        core._check(checks, "fresh_n10_distinct_endpoints", 20,
                    len({evaluated[cid]["left_id"] for _s, cid in actual_sample})
                    + len({evaluated[cid]["right_id"] for _s, cid in actual_sample}))
        sample_record_failures: list[str] = []
        expected_neighbour_rows: dict[tuple[str, int], tuple[str, str, str]] = {}
        for rank, ((stratum, cid), stored) in enumerate(zip(expected_sample, sample_rows), 1):
            opaque = "blind_" + core._stable(SAMPLE_SEED, "opaque", cid)[:24]
            seed_rank = core._stable(SAMPLE_SEED, stratum, cid)
            payload = {"sample_rank": rank, "stratum": stratum, "candidate_id": cid,
                       "opaque_review_id": opaque, "seed_rank_sha256": seed_rank}
            actual = tuple(stored[column] for column in (
                "sample_rank", "stratum", "candidate_id", "opaque_review_id", "seed_rank_sha256", "record_sha256",
            ))
            expected = (rank, stratum, cid, opaque, seed_rank, core._sha(payload))
            if actual != expected:
                sample_record_failures.append(cid)
            focal = evaluated[cid]
            neighbours = [(cid, "focal")]
            contextual = []
            for other_cid, other in evaluated.items():
                if other_cid == cid:
                    continue
                relationship = None
                if other["left_id"] == focal["left_id"]:
                    relationship = "shares_left"
                elif other["right_id"] == focal["right_id"]:
                    relationship = "shares_right"
                if relationship:
                    contextual.append((core._stable(SAMPLE_SEED, opaque, other_cid), other_cid, relationship))
            neighbours.extend((candidate, relationship) for _score, candidate, relationship in sorted(contextual)[:4])
            for display_order, (candidate, relationship) in enumerate(neighbours, 1):
                pair_payload = {"opaque_review_id": opaque, "display_order": display_order,
                                "candidate_id": candidate, "relationship": relationship}
                expected_neighbour_rows[(opaque, display_order)] = (candidate, relationship, core._sha(pair_payload))
        core._check(checks, "fresh_n10_sample_records", [], sample_record_failures)
        stored_neighbours = {(str(row["opaque_review_id"]), int(row["display_order"])): (
            str(row["candidate_id"]), str(row["relationship"]), str(row["record_sha256"])
        ) for row in connection.execute("SELECT * FROM sample_neighbourhood_pairs WHERE run_id=?", (run_id,))}
        core._check(checks, "fresh_n10_neighbourhood_rows", expected_neighbour_rows, stored_neighbours)

        regression_actual = {
            cid for cid in WP22_REGRESSION_IDS
            if cid in transitions and (
                transitions[cid]["v2_proposal"], transitions[cid]["v3_proposal"],
                transitions[cid]["v2_auto_merge_eligible"], transitions[cid]["v3_auto_merge_eligible"],
                transitions[cid]["country_conflict_added"],
            ) == ("merge_candidate", "reject", 1, 0, 1)
        }
        core._check(checks, "wp22_four_pair_regression_to_reject", WP22_REGRESSION_IDS, regression_actual)

        stage_a_logical = core._logical(connection, run_id)
        full_logical = _full_logical(connection, run_id, stage_a_logical)
        core._check(checks, "stage_a_logical_sha", stage_a_logical, str(run["stage_a_logical_sha256"]))
        core._check(checks, "manifest_stage_a_logical_sha", stage_a_logical, str(manifest.get("stage_a_logical_sha256")))
        core._check(checks, "manifest_full_logical_sha", full_logical, str(manifest.get("full_logical_sha256")))
        core._check(checks, "manifest_run_and_versions", (run_id, CONTRACT_VERSION, BUILDER_VERSION, NORMALIZER_VERSION, POLICY_VERSION), (
            str(manifest.get("run_id")), str(manifest.get("contract_version")), str(manifest.get("builder_version")),
            str(manifest.get("normalizer_version")), str(manifest.get("policy_version")),
        ))
        core._check(checks, "manifest_forbidden_actions_zero", {
            "network_requests": 0, "vision_requests": 0, "llm_requests": 0, "legacy_snapshot_reads": 0,
            "merge_executions": 0, "vnx_id_issuances": 0, "n100_runs": 0,
        }, manifest.get("forbidden_actions"))
        if str(run["status"]) == "stage_a_complete":
            artifact_sha = core._sha_file(artifact) if verify_full_hashes else str(manifest.get("stage_a_artifact_sha256"))
            core._check(checks, "stage_a_artifact_size", int(manifest.get("stage_a_artifact_size_bytes", -1)), artifact.stat().st_size)
            core._check(checks, "stage_a_artifact_sha", str(manifest.get("stage_a_artifact_sha256")), artifact_sha)

        proposal_v2 = dict(sorted(Counter(row["v2_proposal"] for row in transitions.values()).items()))
        proposal_v3 = dict(sorted(Counter(row["v3_proposal"] for row in transitions.values()).items()))
        transition_counts = dict(sorted(Counter(
            f"{row['v2_proposal']}->{row['v3_proposal']}" for row in transitions.values()
        ).items()))
        change_sets = {
            "all_country_set_changed_pairs": [row for row in transitions.values() if row["country_set_changed"]],
            "all_country_conflict_added_pairs": [row for row in transitions.values() if row["country_conflict_added"]],
            "all_anchor_ineligible_pairs": [row for row in transitions.values() if row["anchor_ineligible"]],
            "all_fanout_demoted_pairs": [row for row in transitions.values() if row["fanout_demoted"]],
            "all_origin_changed_pairs": [row for row in transitions.values() if row["origins_changed"]],
            "all_proposal_changed_pairs": [row for row in transitions.values() if row["proposal_changed"]],
        }
        stage_report_failures: list[str] = []
        if stage_report is None:
            stage_report_failures.append("missing_or_invalid")
        else:
            expected_scalar = {
                "run_id": run_id, "candidate_count": len(evaluated), "candidate_count_delta_vs_v2": 0,
                "proposal_counts_v2": proposal_v2, "proposal_counts_v3": proposal_v3,
                "transition_counts": transition_counts,
                "decision_changed_count": sum(row["decision_changed"] for row in transitions.values()),
                "proposal_changed_count": len(change_sets["all_proposal_changed_pairs"]),
                "country_set_changed_pair_count": len(change_sets["all_country_set_changed_pairs"]),
                "country_conflict_added_pair_count": len(change_sets["all_country_conflict_added_pairs"]),
                "anchor_ineligible_count": len(change_sets["all_anchor_ineligible_pairs"]),
                "fanout_demoted_count": len(change_sets["all_fanout_demoted_pairs"]),
                "origin_changed_count": len(change_sets["all_origin_changed_pairs"]),
                "stage_a_logical_sha256": stage_a_logical, "full_logical_sha256": full_logical,
                "n100_status": "not_run_waiting_for_separate_order", "prior_review_pair_overlap_count": 0,
            }
            for key, value in expected_scalar.items():
                if stage_report.get(key) != value:
                    stage_report_failures.append(key)
            for key, rows in change_sets.items():
                expected_specs = [_transition_spec(row, buildings) for row in rows]
                if stage_report.get(key) != expected_specs:
                    stage_report_failures.append(key)
            if stage_report.get("wp22_four_pair_regression") != [
                _transition_spec(transitions[cid], buildings) for cid in (
                    "idc_016b59163550fcd3bfe2dcfde694745a", "idc_31831329e77427910c173d3142b4dabe",
                    "idc_55b2f5fc5c91de7d9239c392bc3f08b3", "idc_4142468fdbf954126c43359abad4e3b3",
                )
            ]:
                stage_report_failures.append("wp22_four_pair_regression")
            if stage_report.get("fanout_groups") != fanout_groups:
                stage_report_failures.append("fanout_groups")
            expected_n10_report = [{
                "sample_rank": rank, "stratum": stratum, "candidate_id": cid,
                "v2_proposal": transitions[cid]["v2_proposal"], "v3_proposal": transitions[cid]["v3_proposal"],
                "v2_auto_merge_eligible": transitions[cid]["v2_auto_merge_eligible"],
                "v3_auto_merge_eligible": transitions[cid]["v3_auto_merge_eligible"],
            } for rank, (stratum, cid) in enumerate(expected_sample, 1)]
            if stage_report.get("fresh_disjoint_n10") != expected_n10_report:
                stage_report_failures.append("fresh_disjoint_n10")
            if stage_report.get("cost") != {"network_requests": 0, "paid_api_requests": 0,
                                             "vision_requests": 0, "llm_requests": 0, "estimated_usd": 0.0}:
                stage_report_failures.append("cost")
        core._check(checks, "stage_a_transition_report_full_replay", [], stage_report_failures)

        expected_metrics = {
            "source_buildings": len(buildings), "candidate_pairs": len(evaluated),
            "direct_image_evidence": len(expected_direct_ids),
            "auto_merge_eligible": sum(int(row["auto"]) for row in evaluated.values()),
            "candidate_transitions": len(transitions),
            "decision_changed_pairs": sum(int(row["decision_changed"]) for row in transitions.values()),
            "country_set_changed_source_buildings": sum(
                1 for key, building in buildings.items()
                if v2_maps_for_metrics["country_norm"].get(key) != (building["country_status"], building["country_norm"])
            ),
            "country_set_changed_pairs": len(change_sets["all_country_set_changed_pairs"]),
            "country_conflict_added_pairs": len(change_sets["all_country_conflict_added_pairs"]),
            "anchor_ineligible_pairs": len(change_sets["all_anchor_ineligible_pairs"]),
            "fanout_demoted_pairs": len(change_sets["all_fanout_demoted_pairs"]),
            "origin_changed_pairs": len(change_sets["all_origin_changed_pairs"]),
            "fanout_conflict_groups": sum(group["status"] == "conflict" for group in fanout_groups),
            "fanout_exempt_groups": sum(group["status"] == "exempt" for group in fanout_groups),
            "n10_sample": 10, "network_requests": 0, "vision_requests": 0, "llm_requests": 0,
            "legacy_snapshot_reads": 0, "merge_executions": 0, "vnx_id_issuances": 0, "n100_runs": 0,
        }
        actual_metrics = {str(row[0]): int(row[1]) for row in connection.execute(
            "SELECT metric_name,metric_value FROM metrics WHERE run_id=? AND phase='stage_a'", (run_id,))}
        core._check(checks, "stage_a_metrics_full_replay", expected_metrics, actual_metrics)

        stage_b_started = str(run["stage_b_status"]) != "not_started" or str(run["status"]) in {"pass", "fail"}
        if not stage_b_started:
            core._check(checks, "stage_a_complete_before_stage_b", (
                "stage_a_complete", "not_started", "not_started", "not_started", "pending",
            ), tuple(str(run[column]) for column in (
                "status", "stage_b_status", "blind_review_status", "adjudication_status", "gate_status",
            )))
            core._check(checks, "pre_stage_b_review_rows_zero", (0, 0, 0), (
                int(connection.execute("SELECT count(*) FROM review_assets WHERE run_id=?", (run_id,)).fetchone()[0]),
                int(connection.execute("SELECT count(*) FROM blind_reviews WHERE run_id=?", (run_id,)).fetchone()[0]),
                int(connection.execute("SELECT count(*) FROM adjudications WHERE run_id=?", (run_id,)).fetchone()[0]),
            ))
        elif str(run["stage_b_status"]) == "complete" or str(run["status"]) in {"pass", "fail"}:
            core._validate_stage_b(
                connection=connection, e2=e2, root=root, run_id=run_id, run=run, tables=tables,
                checks=checks, metrics=metrics, verify_full_hashes=verify_full_hashes,
                file_hashes=file_hashes, gate_report_path=DEFAULT_GATE_REPORT,
            )
        else:
            core._check(checks, "stage_b_not_left_partial", True, False,
                        f"status={run['status']} stage_b_status={run['stage_b_status']}")

        metrics.update(
            source_buildings=len(buildings), candidate_pairs=len(evaluated),
            auto_merge_eligible=sum(int(row["auto"]) for row in evaluated.values()),
            country_status_counts=dict(sorted(Counter(row["country_status"] for row in buildings.values()).items())),
            anchor_ineligible=sum(any(flag["flag_type"] == "anchor_missing" for flag in row["flags"])
                                  for row in evaluated.values()),
            fanout_demoted=sum(any(flag["flag_type"] == "fanout_conflict" for flag in row["flags"])
                               for row in evaluated.values()),
            decision_changed=sum(row["decision_changed"] for row in transitions.values()),
            fresh_n10_strata=dict(sorted(Counter(x[0] for x in expected_sample).items())),
            n100_status="not_run_waiting_for_separate_order",
        )
    finally:
        connection.close()
        v2.close()
        divisare.close()
        architizer.close()
        e2.close()

    result = core.ValidationResult(artifact, report, run_id, tuple(checks), metrics)
    if report is not None:
        _write_report(result, report)
    return result


__all__ = ["DEFAULT_ARTIFACT", "DEFAULT_REPORT", "VALIDATOR_VERSION", "validate_identity_candidates_v3"]
