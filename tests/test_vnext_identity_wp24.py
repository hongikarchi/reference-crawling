from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from canonical import vnext_identity_candidates as base
from canonical import vnext_identity_candidates_v4 as v4
from canonical.vnext_country_norm import classify_country


def _building(
    source: str,
    building_id: str,
    name: str,
    *,
    firm: str = "",
    country: str = "",
    locality: str = "",
    year: int | None = 2020,
) -> dict[str, object]:
    normalized = classify_country(country)
    return {
        "source": source,
        "source_building_id": building_id,
        "name": name,
        "aliases": (name,),
        "alias_norms": (base.normalize_text(name, ascii_fold=True),),
        "firms": (firm,) if firm else (),
        "firm_keys": (base.firm_core(firm, ascii_fold=True),) if firm else (),
        "country": country,
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


def _family(result: dict[str, object], name: str) -> dict[str, object]:
    return next(row for row in result["families"] if row["family"] == name)  # type: ignore[index,union-attr]


def _conflicts(result: dict[str, object]) -> set[str]:
    return {row["conflict_type"] for row in result["conflicts"]}  # type: ignore[index,union-attr]


def _flags(result: dict[str, object]) -> set[str]:
    return {row["flag_type"] for row in result["policy_flags"]}  # type: ignore[index,union-attr]


@pytest.mark.parametrize(
    ("gap", "expected_status", "phase_conflict"),
    [
        (0, "positive", False),
        (1, "positive", False),
        (2, "unknown", False),
        (5, "unknown", False),
        (6, "negative", True),
    ],
)
def test_r1_year_bands_and_inclusive_phase_boundary(
    gap: int, expected_status: str, phase_conflict: bool
) -> None:
    result = v4.evaluate_candidate(
        _building("divisare", "d", "Shared Name", firm="Left Firm", year=2020),
        _building(
            "architizer", "a", "Shared Name", firm="Right Firm", year=2020 + gap
        ),
        set(),
        {},
        {},
        {},
    )
    assert _family(result, "year_phase_compatibility")["status"] == expected_status
    assert ("project_phase_incompatible" in _conflicts(result)) is phase_conflict
    assert "matcher_architect_year_gate_block" not in _conflicts(result)


def test_r1_phase_conflict_cannot_override_positive_firm_or_image() -> None:
    left = _building("divisare", "d", "Shared Name", firm="Shared Firm", year=2020)
    right = _building("architizer", "a", "Shared Name", firm="Shared Firm", year=2026)
    firm_anchored = v4.evaluate_candidate(left, right, set(), {}, {}, {})
    assert _family(firm_anchored, "year_phase_compatibility")["status"] == "negative"
    assert not _conflicts(firm_anchored)
    assert firm_anchored["proposal"] == "merge_candidate"

    image_anchored = v4.evaluate_candidate(
        _building("divisare", "d2", "Shared Name", firm="Left Firm", year=2020),
        _building("architizer", "a2", "Shared Name", firm="Right Firm", year=2026),
        {"e2_candidate"},
        {"phash_le8_pair_count": 1},
        {"unique_pixel_count": 3},
        {"unique_pixel_count": 3},
    )
    assert _family(image_anchored, "verified_pixel_image_overlap")["status"] == "positive"
    assert "project_phase_incompatible" not in _conflicts(image_anchored)
    assert image_anchored["proposal"] == "merge_candidate"


def test_r2_phash_veto_downgrades_only_for_positive_name_and_firm() -> None:
    strong_stats = {"unique_pixel_count": 3}
    downgraded = v4.evaluate_candidate(
        _building("divisare", "d", "Shared House", firm="Shared Firm", year=2020),
        _building("architizer", "a", "Shared House", firm="Shared Firm", year=2020),
        set(),
        {},
        strong_stats,
        strong_stats,
    )
    assert "verified_phash_veto" not in _conflicts(downgraded)
    assert _flags(downgraded) == {"phash_veto_review_required"}
    assert (downgraded["proposal"], downgraded["auto_merge_eligible"]) == (
        "merge_candidate",
        0,
    )
    assert _family(downgraded, "verified_pixel_image_overlap")["status"] == "negative"

    retained = v4.evaluate_candidate(
        _building("divisare", "d2", "Shared House", firm="Left Firm", year=2020),
        _building("architizer", "a2", "Shared House", firm="Right Firm", year=2020),
        set(),
        {},
        strong_stats,
        strong_stats,
    )
    assert _conflicts(retained) == {"verified_phash_veto"}
    assert "phash_veto_review_required" not in _flags(retained)
    assert (retained["proposal"], retained["auto_merge_eligible"]) == ("reject", 0)


def _fanout_candidate(candidate_id: str, left_id: str, right_id: str) -> dict[str, object]:
    result = v4.evaluate_candidate(
        _building("divisare", left_id, "Hub", firm="Shared Firm", year=2020),
        _building(
            "architizer", right_id, f"Opponent {right_id}", firm="Shared Firm", year=2020
        ),
        {"e2_candidate"},
        {"phash_le8_pair_count": 1},
        {"unique_pixel_count": 3},
        {"unique_pixel_count": 3},
    )
    assert result["auto_merge_eligible"] == 1
    return {**result, "candidate_id": candidate_id, "left_id": left_id, "right_id": right_id}


def test_r3_fanout_becomes_review_flag_not_hard_conflict() -> None:
    buildings = {
        ("divisare", "hub"): _building(
            "divisare", "hub", "Hub", firm="Shared Firm", year=2020
        ),
        ("architizer", "a1"): _building(
            "architizer", "a1", "First Project", firm="Shared Firm", year=2020
        ),
        ("architizer", "a2"): _building(
            "architizer", "a2", "Second Project", firm="Shared Firm", year=2020
        ),
    }
    evaluated = {
        "c1": _fanout_candidate("c1", "hub", "a1"),
        "c2": _fanout_candidate("c2", "hub", "a2"),
    }
    groups = v4.apply_fanout_guard(evaluated, buildings)
    assert len(groups) == 1
    assert groups[0]["status"] == "conflict"
    assert groups[0]["pre_guard_degree"] == 2
    assert groups[0]["detail"]["transitive_closure_used"] is False
    for result in evaluated.values():
        assert (result["proposal"], result["auto_merge_eligible"]) == (
            "merge_candidate",
            0,
        )
        assert not _conflicts(result)
        assert _flags(result) == {"fanout_review_required"}
        assert v4.human_review_queue_reasons(result) == ["merge_candidate"]


def _queue_result(proposal: str, positive: tuple[str, ...]) -> dict[str, object]:
    family_names = (
        "name_alias_identity",
        "architect_firm_identity",
        "location_site_identity",
        "year_phase_compatibility",
        "source_url_external_stable_id",
        "verified_pixel_image_overlap",
    )
    return {
        "proposal": proposal,
        "positive_family_count": len(positive),
        "families": [
            {"family": family, "status": "positive" if family in positive else "unknown"}
            for family in family_names
        ],
    }


def test_r4_queue_membership_and_evidence_strength_bands() -> None:
    merge_without_anchor = _queue_result(
        "merge_candidate", ("architect_firm_identity", "year_phase_compatibility")
    )
    assert v4.human_review_queue_reasons(merge_without_anchor) == ["merge_candidate"]
    assert v4.evidence_strength_band(merge_without_anchor) == "no_anchor_2plus"

    anchored_defer = _queue_result(
        "defer", ("verified_pixel_image_overlap", "architect_firm_identity")
    )
    assert v4.human_review_queue_reasons(anchored_defer) == [
        "r4_anchor_two_family_defer"
    ]
    assert v4.evidence_strength_band(anchored_defer) == "anchor_2"

    unanchored_defer = _queue_result(
        "defer", ("architect_firm_identity", "year_phase_compatibility")
    )
    assert v4.human_review_queue_reasons(unanchored_defer) == []
    assert v4.evidence_strength_band(unanchored_defer) == "not_queued"

    other_anchor_three = _queue_result(
        "merge_candidate",
        (
            "verified_pixel_image_overlap",
            "architect_firm_identity",
            "location_site_identity",
        ),
    )
    assert v4.evidence_strength_band(other_anchor_three) == "other_anchor_3plus"
    name_firm_image = _queue_result(
        "merge_candidate",
        (
            "name_alias_identity",
            "architect_firm_identity",
            "verified_pixel_image_overlap",
        ),
    )
    assert v4.evidence_strength_band(name_firm_image) == "name_firm_image_3plus"


def test_r5_country_name_only_and_anchor_guards_remain_strict() -> None:
    country_conflict = v4.evaluate_candidate(
        _building(
            "divisare",
            "d",
            "Shared Project",
            firm="Shared Firm",
            country="France",
            locality="Paris",
            year=2020,
        ),
        _building(
            "architizer",
            "a",
            "Shared Project",
            firm="Shared Firm",
            country="Senegal",
            locality="Dakar",
            year=2020,
        ),
        set(),
        {},
        {},
        {},
    )
    assert country_conflict["proposal"] == "reject"
    assert country_conflict["auto_merge_eligible"] == 0
    assert "country_site_incompatible" in _conflicts(country_conflict)

    name_only = v4.evaluate_candidate(
        _building("divisare", "d2", "House H", firm="Left Firm", year=2020),
        _building("architizer", "a2", "House H", firm="Right Firm", year=2023),
        set(),
        {},
        {},
        {},
    )
    assert name_only["positive_family_count"] == 1
    assert (name_only["proposal"], name_only["auto_merge_eligible"]) == ("defer", 0)

    no_anchor = v4.evaluate_candidate(
        _building(
            "divisare",
            "d3",
            "First Name",
            firm="Shared Firm",
            country="France",
            locality="Paris",
            year=2020,
        ),
        _building(
            "architizer",
            "a3",
            "Second Name",
            firm="Shared Firm",
            country="France",
            locality="Paris",
            year=2020,
        ),
        set(),
        {},
        {},
        {},
    )
    assert no_anchor["proposal"] == "merge_candidate"
    assert no_anchor["auto_merge_eligible"] == 0
    assert "anchor_missing" in _flags(no_anchor)


def test_wp24_contract_and_frozen_input_pins() -> None:
    contract = v4.INPUT_PINS["identity_contract"]
    assert contract == (
        Path("docs/vnext/CONTRACT_IDENTITY.md"),
        9_879,
        "502e6258b9c7803ea58bc415ddbe73fbcc50faa0005d8b9aaaaaeab04838e3e8",
        "policy_contract",
    )
    assert v4.INPUT_PINS["v3_candidate_artifact"] == (
        Path("data/enrichment/vnext_identity_candidates_v3.db"),
        580_710_400,
        "c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f",
        "comparison_only_not_candidate_evidence",
    )
    assert v4.INPUT_PINS["n100_gold_artifact"] == (
        Path("data/enrichment/vnext_identity_n100_v1.db"),
        2_183_168,
        "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b",
        "regression_gold_binding_only_not_candidate_evidence",
    )
    assert v4.INPUT_PINS["n100_final_manifest"] == (
        Path("data/reports/vnext_identity_n100_v1_manifest.json"),
        3_579,
        "e4accff5634ce97712f5cb226e9a5557a75aef7699f6d532a6fcf514f2e3e9da",
        "regression_gold_chain_manifest",
    )
    assert v4.INPUT_PINS["n100_locked_adjudication"] == (
        Path("data/reports/vnext_identity_n100_v1_adjudication_locked.json"),
        63_713,
        "c4fec6261cefb6a00c25baea9e36fccb9579e4832fbe52d80fd72fa36e9d3337",
        "regression_gold_chain_locked_adjudication",
    )
    assert v4.POLICY_SPEC["fresh_n10"] is False
    assert "no-fresh-n10" in v4.SAMPLE_VERSION


B4_IDS = {
    "idc_c6ead83e5d7219b4ec0318b1ed39567c",
    "idc_d0c097e8b20afc2a14f508b91f9ff6b9",
    "idc_9e2f4da22275f8ec633fb29a66da2a3d",
    "idc_cc669cc8b780023220c2fc4090159648",
    "idc_5dfd2e925881bad8ad40abe7a846d635",
    "idc_1ec7623590c23d3d4395d725fafcc6e3",
    "idc_d34dba7f027f1e4783323fb5b2cd519a",
    "idc_7d9327e32edbacaa61ac1389e382d5cd",
    "idc_4e925163e0cb6f70bbfadd23582481cb",
    "idc_20c724a3d5e9b2c24b68034a08f7a997",
}
B2_DIFFERENT_IDS = {
    "idc_61b9140a4e8267101925ffc8faf6799c",
    "idc_c545c21b4db0fd90353416243a7d08ff",
    "idc_4cae1265fc274d0d778b86d2fef1f535",
    "idc_4d179c5bcd1defec0318d63101160917",
}
B1_IDS = {
    "idc_bdd8056c763a1bf41f837b5e21a3c0b0",
    "idc_a5a4d84ac6a26ee4b5cb3263ed283176",
    "idc_d66626bec7bd8f7428c2da45ec3ac36a",
    "idc_db49025e39fe2b5dc8a291b29f84f3a7",
    "idc_2172e9f8e49c0cd7b354c3bac00cb761",
    "idc_574da909dac501eeb8b1fa5dc0412589",
    "idc_def43782af0a08f8bb63766ded8f1ff2",
    "idc_25f2470f6648f53fee3af81503a0ddce",
    "idc_d0894c3fea4c175470762a283571da52",
}
B3_SAME_IDS = {
    "idc_25f0e80b1bb146f7a9de69dae1479cb9",
    "idc_343acf338f4c133a4cfaae1b5847aa12",
    "idc_51000add613d4e0ab68693fd52b9cf93",
    "idc_32aae04217168527d72cde93a966437f",
    "idc_681fba1ce7f631d10ddc1e5054e36134",
    "idc_89752449d8e8cb76e5728f71c352c410",
}


def test_regression_gold_derives_exact_33_frozen_cases_without_drafts() -> None:
    root = Path.cwd()
    n100_path = (root / v4.DEFAULT_N100).resolve()
    connection = sqlite3.connect(
        f"file:{n100_path.as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        run_id = v4._verify_n100_gold_chain(root, connection)
        bindings = v4._load_regression_bindings(root, connection, run_id)
    finally:
        connection.close()

    assert len(bindings) == 33
    assert Counter(row["gate_name"] for row in bindings) == Counter(
        v4.EXPECTED_REGRESSION_GATE_COUNTS
    )
    by_subgroup = {
        subgroup: {row["candidate_id"] for row in bindings if row["subgroup"] == subgroup}
        for subgroup in ("B4", "B2_different", "B1_year", "B3_same", "WP22_country")
    }
    assert by_subgroup == {
        "B4": B4_IDS,
        "B2_different": B2_DIFFERENT_IDS,
        "B1_year": B1_IDS,
        "B3_same": B3_SAME_IDS,
        "WP22_country": set(v4.WP22_REGRESSION_IDS),
    }
    n100_rows = [row for row in bindings if row["source_artifact_role"] == "n100_gold_artifact"]
    assert len(n100_rows) == 29
    assert all(len(row["sample_record_sha256"]) == 64 for row in n100_rows)
    assert all(len(row["gold_record_sha256"]) == 64 for row in n100_rows)
    assert all("draft" not in row["source_artifact_role"] for row in bindings)
    assert sum(
        row["subgroup"] == "B3_same" and row["sample_origin"] == "carry_forward"
        for row in bindings
    ) == 3
    assert Counter(row["gold_label"] for row in bindings if row["subgroup"] == "B4") == {
        "different": 6,
        "insufficient": 4,
    }


def test_stage_a_schema_defaults_to_zero_and_blocks_mutating_actions() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(v4.V4_BASE_SCHEMA_SQL)
    required = (
        "run_id,contract_version,builder_version,blocker_version,policy_version,"
        "normalizer_version,sample_version,sample_seed,status,started_at,"
        "normalizer_sha256,policy_sha256"
    )
    values = (
        "r",
        v4.CONTRACT_VERSION,
        v4.BUILDER_VERSION,
        base.BLOCKER_VERSION,
        v4.POLICY_VERSION,
        v4.NORMALIZER_VERSION,
        v4.SAMPLE_VERSION,
        v4.SAMPLE_SEED,
        "building",
        "now",
        "n",
        "p",
    )
    connection.execute(
        f"INSERT INTO runs({required}) VALUES({','.join('?' for _ in values)})", values
    )
    row = connection.execute(
        "SELECT stage_a_network_requests,stage_a_vision_requests,stage_a_llm_requests,"
        "stage_b_http_requests,stage_b_vision_requests,stage_b_llm_requests,"
        "merge_executions,vnx_id_issuances FROM runs"
    ).fetchone()
    assert row == (0, 0, 0, 0, 0, 0, 0, 0)
    for column in (
        "stage_a_network_requests",
        "stage_a_vision_requests",
        "stage_a_llm_requests",
        "stage_b_vision_requests",
        "stage_b_llm_requests",
        "merge_executions",
        "vnx_id_issuances",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE runs SET {column}=1 WHERE run_id='r'")
    connection.close()
