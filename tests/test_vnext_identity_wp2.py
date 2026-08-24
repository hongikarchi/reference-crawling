from __future__ import annotations

import sqlite3
from pathlib import Path

from canonical.vnext_identity_candidates import (
    EVIDENCE_FAMILIES,
    SCHEMA_SQL,
    best_similarity,
    candidate_id,
    distinctive_name_tokens,
    evaluate_candidate,
    firm_keys,
    normalize_country,
    normalize_text,
)


def test_independent_validator_does_not_import_producer() -> None:
    source = Path("canonical/vnext_identity_candidates_validator.py").read_text(encoding="utf-8")
    assert "from canonical.vnext_identity_candidates import" not in source
    assert "import canonical.vnext_identity_candidates" not in source


def _building(
    source: str,
    building_id: str,
    name: str,
    firm: str,
    *,
    country: str = "United States",
    locality: str = "New York",
    year: int | None = 2020,
) -> dict[str, object]:
    return {
        "source": source,
        "source_building_id": building_id,
        "name": name,
        "aliases": (name,),
        "alias_norms": (normalize_text(name, ascii_fold=True),),
        "firms": (firm,),
        "firm_keys": firm_keys((firm,)),
        "country": country,
        "country_norm": normalize_country(country),
        "locality": locality,
        "locality_norm": normalize_text(locality, ascii_fold=True),
        "year_min": year,
        "year_max": year,
        "source_urls": (),
        "external_ids": (),
    }


def test_schema_has_candidate_review_without_registry_or_component_tables() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_SQL)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"candidate_pairs", "evidence_families", "sample_neighbourhoods", "blind_reviews", "adjudications"} <= tables
    assert not {"registry", "components", "legacy_crosswalk"} & tables


def test_normalizers_are_deterministic_and_conservative() -> None:
    assert normalize_text("Snøhetta", ascii_fold=True) == "snohetta"
    assert normalize_country("United States of America") == "united states"
    assert "foster" in firm_keys(("Foster + Partners - Architizer",))
    assert firm_keys(("Architecture Studio",)) == ()
    assert distinctive_name_tokens("The Museum House Project") == set()


def test_strict_similarity_is_minimum_of_sort_and_set_lineage() -> None:
    strict, loose, set_score, _left, _right = best_similarity(("alpha beta",), ("alpha beta annex",))
    assert strict < loose
    assert loose == set_score == 100.0


def test_two_positive_families_zero_conflict_is_merge_candidate() -> None:
    left = _building("divisare", "d1", "River Arts Center", "OMA")
    right = _building("architizer", "a1", "River Arts Center", "OMA - Architizer")
    result = evaluate_candidate(
        left,
        right,
        {"e2_metadata"},
        {},
        {"unique_pixel_count": 1},
        {"unique_pixel_count": 1},
    )
    assert len(result["families"]) == len(EVIDENCE_FAMILIES) == 6
    assert result["positive_family_count"] >= 2
    assert result["hard_conflict_count"] == 0
    assert result["proposal"] == "merge_candidate"
    assert result["auto_merge_eligible"] == 1


def test_country_conflict_or_sufficient_zero_overlap_blocks_merge() -> None:
    left = _building("divisare", "d1", "River Arts Center", "OMA", country="France")
    right = _building("architizer", "a1", "River Arts Center", "OMA", country="Japan")
    result = evaluate_candidate(
        left,
        right,
        {"matcher_architect_year"},
        {},
        {"unique_pixel_count": 3},
        {"unique_pixel_count": 3},
    )
    conflict_types = {row["conflict_type"] for row in result["conflicts"]}
    assert {"country_site_incompatible", "verified_phash_veto"} <= conflict_types
    assert result["proposal"] != "merge_candidate"
    assert result["auto_merge_eligible"] == 0


def test_direct_image_overlap_cancels_phash_veto_but_never_adds_more_than_one_family() -> None:
    left = _building("divisare", "d1", "North Hall", "Alpha Workshop", country="", locality="", year=None)
    right = _building("architizer", "a1", "Completely Different", "Beta Workshop", country="", locality="", year=None)
    result = evaluate_candidate(
        left,
        right,
        {"e2_candidate"},
        {"phash_le8_pair_count": 7},
        {"unique_pixel_count": 10},
        {"unique_pixel_count": 12},
    )
    image_family = next(row for row in result["families"] if row["family"] == "verified_pixel_image_overlap")
    assert image_family["status"] == "positive"
    assert result["positive_family_count"] == 1
    assert result["proposal"] == "defer"


def test_candidate_id_is_direction_fixed_and_stable() -> None:
    assert candidate_id("div_bld_1", "atz_bld_1") == candidate_id("div_bld_1", "atz_bld_1")
    assert candidate_id("div_bld_1", "atz_bld_1") != candidate_id("div_bld_2", "atz_bld_1")
