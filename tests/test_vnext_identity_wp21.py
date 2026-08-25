from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from canonical import vnext_country_norm as country
from canonical import vnext_identity_candidates as base
from canonical.vnext_identity_candidates_v2 import V2_BASE_SCHEMA_SQL, evaluate_candidate
from tools.canonical_v2_c21_make_web_polish import _normalize_country_full as legacy_c21_normalize


def _sha(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        while chunk:=handle.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()


def test_country_module_has_no_legacy_runtime_import() -> None:
    source=Path("canonical/vnext_country_norm.py").read_text(encoding="utf-8")
    assert "from tools." not in source
    assert "import tools." not in source


def test_country_provenance_sources_are_sha_pinned() -> None:
    for path,size,digest in country.SOURCE_PROVENANCE.values():
        assert path.stat().st_size==size
        assert _sha(path)==digest


def test_representative_aliases_match_c21_verbatim() -> None:
    values=(
        None,"","Türkiye","turkiye","Deutschland","Österreich","España","The Netherlands",
        "대한민국","中国","Iran, Islamic Republic of","United States of America","France","Georgia","Atlantis",
    )
    assert [country.normalize_country_full(value) for value in values]==[legacy_c21_normalize(value) for value in values]


def test_known_unknown_and_ambiguous_are_distinct() -> None:
    assert country.classify_country("Türkiye").status=="known"
    assert country.classify_country("Türkiye").canonical=="Turkey"
    assert country.classify_country("Turkey").canonical=="Turkey"
    assert country.classify_country("Georgia").status=="ambiguous"
    assert country.classify_country("Georgia").canonical is None
    assert country.classify_country("Atlantis").status=="unknown"
    assert country.classify_country("Atlantis").canonical is None


def _building(source: str, building_id: str, country_value: str, locality: str = "Same City") -> dict[str, object]:
    normalization=country.classify_country(country_value)
    return {
        "source":source,"source_building_id":building_id,"name":"Specific Project","aliases":("Specific Project",),
        "alias_norms":("specific project",),"firms":("Exact Firm",),"firm_keys":("exact firm",),
        "country":country_value,"country_norm":normalization.canonical or "",
        "country_normalization_status":normalization.status,"country_normalization_reason":normalization.reason,
        "locality":locality,"locality_norm":base.normalize_text(locality,ascii_fold=True),"year_min":2020,"year_max":2020,
        "source_urls":(),"external_ids":(),
    }


def _location_result(left_country: str, right_country: str) -> tuple[str, set[str]]:
    result=evaluate_candidate(_building("divisare","d1",left_country),_building("architizer","a1",right_country),
        {"matcher_architect_year"},{},{"unique_pixel_count":1},{"unique_pixel_count":1})
    location=next(row for row in result["families"] if row["family"]=="location_site_identity")
    return str(location["status"]),{row["conflict_type"] for row in result["conflicts"]}


def test_country_conflict_requires_two_known_different_countries() -> None:
    status,conflicts=_location_result("France","Japan")
    assert status=="negative"
    assert "country_site_incompatible" in conflicts
    status,conflicts=_location_result("Turkey","Türkiye")
    assert status=="positive"
    assert "country_site_incompatible" not in conflicts


def test_unknown_or_ambiguous_country_is_neutral() -> None:
    for left,right in (("Atlantis","Atlantis"),("Georgia","Georgia"),("France","Atlantis")):
        status,conflicts=_location_result(left,right)
        assert status=="unknown"
        assert "country_site_incompatible" not in conflicts


def test_contract_states_known_only_country_rule() -> None:
    contract=Path("docs/vnext/CONTRACT_IDENTITY.md").read_text(encoding="utf-8")
    assert "known canonical 값으로 정규화될 때만" in contract
    assert "정규화 불가·모호 값은 둘 다 중립" in contract


def test_v2_schema_explicitly_allows_state_transition_stratum() -> None:
    connection=sqlite3.connect(":memory:")
    connection.executescript(V2_BASE_SCHEMA_SQL)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO runs(run_id,contract_version,builder_version,blocker_version,policy_version,normalizer_version,"
        "sample_version,sample_seed,status,started_at,normalizer_sha256,policy_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("r","c","b","k","p","n","s","seed","building","now","x","y"),
    )
    connection.execute(
        "INSERT INTO candidate_pairs(run_id,candidate_id,left_source,left_source_building_id,right_source,"
        "right_source_building_id,origin_e2_metadata,origin_e2_candidate,origin_matcher_architect_year,origin_matcher_pass2,"
        "origin_count,exact_asset_pair_count,identical_phash_pair_count,phash_le8_pair_count,phash_9_16_pair_count,"
        "name_strict_score,name_loose_score,name_token_set_score,firm_strict_score,"
        "positive_family_count,negative_family_count,hard_conflict_count,proposal,auto_merge_eligible,record_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("r","c1","divisare","d1","architizer","a1",0,0,1,0,1,0,0,0,0,100,100,100,100,2,0,0,"merge_candidate",1,"z"),
    )
    connection.execute(
        "INSERT INTO sample_neighbourhoods VALUES(?,?,?,?,?,?,?,?)",
        ("r",1,"state_transition","c1","blind_x","now","seed-rank","record"),
    )
