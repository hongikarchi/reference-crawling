from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from canonical import vnext_country_norm as country
from canonical import vnext_identity_candidates as base
from canonical.vnext_identity_candidates_v3 import (
    V3_BASE_SCHEMA_SQL,
    apply_fanout_guard,
    evaluate_candidate,
)
from tools.canonical_v2_c21_make_web_polish import _normalize_country_full as legacy_normalize


def test_iso_snapshot_and_additions_are_complete() -> None:
    assert len(country.ISO_3166_1_UPSTREAM_NAMES) == 249
    assert sum(row[1] is not None for row in country.ISO_3166_1_UPSTREAM_NAMES.values()) == 173
    assert sum(row[2] is not None for row in country.ISO_3166_1_UPSTREAM_NAMES.values()) == 11
    assert len(country.ISO_COUNTRY_ALIASES) == 424
    assert len(country.KNOWN_COUNTRIES) == 248
    assert len(country.ISO_COUNTRY_ADDITIONS["known_countries"]) == 159
    assert len(country.ISO_COUNTRY_ADDITIONS["aliases"]) == 398


def test_all_iso_names_replay_and_legacy_aliases_are_not_overwritten() -> None:
    for code, (name, official_name, common_name) in country.ISO_3166_1_UPSTREAM_NAMES.items():
        expected = country.ISO_COUNTRY_CANONICAL_BY_CODE[code]
        for value in (name, official_name, common_name):
            if value is None:
                continue
            result = country.classify_country(value)
            if value.casefold() == "georgia":
                assert (result.status, result.canonical) == ("ambiguous", None)
            else:
                assert (result.status, result.canonical) == ("known", expected)
    legacy_keys = set(country._COUNTRY_ALIASES_EXT) | set(country._NATIVE_COUNTRY_ALIASES)
    for value in legacy_keys:
        assert country.normalize_country_full(value) == legacy_normalize(value)


def test_senegal_ecuador_and_ambiguity_regressions() -> None:
    for value, expected in (
        ("Senegal", "Senegal"),
        ("Republic of Senegal", "Senegal"),
        ("Ecuador", "Ecuador"),
        ("Republic of Ecuador", "Ecuador"),
    ):
        result = country.classify_country(value)
        assert (result.status, result.canonical) == ("known", expected)
    assert (country.classify_country("Georgia").status, country.classify_country("Georgia").canonical) == (
        "ambiguous", None
    )


def _building(
    source: str,
    building_id: str,
    name: str,
    country_value: str = "France",
    locality: str = "Paris",
    firm: str = "Same Firm",
    year: int = 2020,
) -> dict[str, object]:
    normalized = country.classify_country(country_value)
    return {
        "source": source,
        "source_building_id": building_id,
        "name": name,
        "aliases": (name,),
        "alias_norms": (base.normalize_text(name, ascii_fold=True),),
        "firms": (firm,),
        "firm_keys": (base.firm_core(firm, ascii_fold=True),),
        "country": country_value,
        "country_norm": normalized.canonical or "",
        "country_normalization_status": normalized.status,
        "country_normalization_reason": normalized.reason,
        "locality": locality,
        "locality_norm": base.normalize_text(locality, ascii_fold=True),
        "year_min": year,
        "year_max": year,
        "source_urls": (),
        "external_ids": (),
    }


def test_anchor_is_required_but_proposal_can_remain_merge_candidate() -> None:
    left = _building("divisare", "d1", "Alpha House")
    right = _building("architizer", "a1", "Completely Different")
    result = evaluate_candidate(left, right, {"matcher_architect_year"}, {}, {}, {})
    assert result["positive_family_count"] == 3  # firm + location + year
    assert result["proposal"] == "merge_candidate"
    assert result["auto_merge_eligible"] == 0
    assert [row["flag_type"] for row in result["policy_flags"]] == ["anchor_missing"]

    named = evaluate_candidate(left, _building("architizer", "a2", "Alpha House"), set(), {}, {}, {})
    assert named["auto_merge_eligible"] == 1
    image = evaluate_candidate(
        left,
        right,
        {"e2_candidate"},
        {"phash_le8_pair_count": 1},
        {"unique_pixel_count": 2},
        {"unique_pixel_count": 2},
    )
    assert image["auto_merge_eligible"] == 1


def _candidate(candidate_id: str, left_id: str, right_id: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "left_id": left_id,
        "right_id": right_id,
        "auto_merge_eligible": 1,
        "proposal": "merge_candidate",
        "conflicts": [],
        "hard_conflict_count": 0,
        "policy_flags": [],
    }


def test_fanout_guard_demotes_unrelated_incident_edges_simultaneously() -> None:
    buildings = {
        ("divisare", "hub"): _building("divisare", "hub", "Hub"),
        ("architizer", "a1"): _building("architizer", "a1", "First Project"),
        ("architizer", "a2"): _building("architizer", "a2", "Second Project"),
        ("architizer", "a3"): _building("architizer", "a3", "Third Project"),
    }
    evaluated = {
        "c1": _candidate("c1", "hub", "a1"),
        "c2": _candidate("c2", "hub", "a2"),
        "c3": _candidate("c3", "hub", "a3"),
    }
    groups = apply_fanout_guard(evaluated, buildings)
    assert len(groups) == 1 and groups[0]["status"] == "conflict"
    assert groups[0]["pre_guard_degree"] == 3
    for row in evaluated.values():
        assert (row["proposal"], row["auto_merge_eligible"], row["hard_conflict_count"]) == ("defer", 0, 1)
        assert row["conflicts"][0]["conflict_type"] == "fanout_conflict"


def test_fanout_exception_requires_direct_pairwise_relation() -> None:
    buildings = {
        ("divisare", "hub"): _building("divisare", "hub", "Hub"),
        ("architizer", "a1"): _building("architizer", "a1", "Duplicate Project"),
        ("architizer", "a2"): _building("architizer", "a2", "Duplicate Project"),
    }
    evaluated = {"c1": _candidate("c1", "hub", "a1"), "c2": _candidate("c2", "hub", "a2")}
    groups = apply_fanout_guard(evaluated, buildings)
    assert len(groups) == 1 and groups[0]["status"] == "exempt"
    assert all(row["auto_merge_eligible"] == 1 for row in evaluated.values())


def test_v3_schema_decouples_proposal_from_auto_eligibility() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(V3_BASE_SCHEMA_SQL)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO runs(run_id,contract_version,builder_version,blocker_version,policy_version,normalizer_version,"
        "sample_version,sample_seed,status,started_at,normalizer_sha256,policy_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("r", "c", "b", "k", "p", "n", "s", "seed", "building", "now", "x", "y"),
    )
    connection.execute(
        "INSERT INTO candidate_pairs(run_id,candidate_id,left_source,left_source_building_id,right_source,"
        "right_source_building_id,origin_e2_metadata,origin_e2_candidate,origin_matcher_architect_year,origin_matcher_pass2,"
        "origin_count,exact_asset_pair_count,identical_phash_pair_count,phash_le8_pair_count,phash_9_16_pair_count,"
        "name_strict_score,name_loose_score,name_token_set_score,firm_strict_score,positive_family_count,"
        "negative_family_count,hard_conflict_count,proposal,auto_merge_eligible,record_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("r", "c1", "divisare", "d1", "architizer", "a1", 0, 0, 1, 0, 1, 0, 0, 0, 0,
         10, 10, 10, 100, 2, 0, 0, "merge_candidate", 0, "z"),
    )


def test_contract_records_anchor_iso_and_fanout_rules() -> None:
    text = Path("docs/vnext/CONTRACT_IDENTITY.md").read_text(encoding="utf-8")
    assert "architect/firm+연도 조합만으로는" in text
    assert "ISO 3166-1" in text
    assert "fanout_conflict" in text
    assert "transitive closure와 순차 재계산은 금지" in text


def test_v2_artifact_pin_is_immutable() -> None:
    path = Path("data/enrichment/vnext_identity_candidates_v2.db")
    assert path.stat().st_size == 581_292_032
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    assert digest.hexdigest() == "08e28e4e5a1ab5aba9ea5ddd53ca615f417458a6693cdfb1aefc822d57b5b0c4"
