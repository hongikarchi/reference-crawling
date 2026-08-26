from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from canonical import vnext_identity_n100_validator as validator


def _candidate(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "proposal": "reject",
        "positive_family_count": 2,
        "name_strict_score": 90.0,
        "exact_asset_pair_count": 0,
        "identical_phash_pair_count": 0,
        "phash_le8_pair_count": 0,
    }
    row.update(overrides)
    return row


def test_protocol_accounting_and_hierarchical_quotas() -> None:
    assert validator.A_FULL_POPULATION == 4_169
    assert validator.A_FULL_POPULATION - validator.A_REVIEWED_ELIGIBLE == 4_150
    assert sum(validator.A_FRAME_COUNTS.values()) == validator.A_FRAME_POPULATION
    assert sum(validator.A_QUOTAS.values()) == validator.A_SAMPLE_COUNT == 70
    by_anchor: dict[str, int] = {}
    for (anchor, _auxiliary), quota in validator.A_QUOTAS.items():
        by_anchor[anchor] = by_anchor.get(anchor, 0) + quota
    assert by_anchor == {"both": 45, "image": 24, "name": 1}
    assert sum(validator.B_TOTAL_SAMPLE_COUNTS.values()) == 40
    assert validator.A_SAMPLE_COUNT + validator.B_SAMPLE_COUNT == 110
    assert validator.FRESH_SAMPLE_COUNT + validator.CARRY_FORWARD_COUNT == 110
    assert validator.REVIEW_ROWS_PER_REVIEWER == 105
    assert validator.TOTAL_DUAL_REVIEW_ROWS == 210
    assert validator.FRESH_ADJUDICATION_COUNT == 105
    assert validator.MAX_ASSET_OCCURRENCES == 105 * 2 * 2 == 420
    assert validator.MAX_LOGICAL_HTTP_FETCHES == 420
    assert validator.MAX_WIRE_HTTP_REQUESTS == 500


def test_carry_forward_is_exactly_five_reviewed_sentinels() -> None:
    flattened = [candidate_id for ids in validator.CARRY_FORWARD.values() for candidate_id in ids]
    assert len(flattened) == len(set(flattened)) == 5
    assert {lane: len(ids) for lane, ids in validator.CARRY_FORWARD.items()} == {
        "B1_year_gate": 1,
        "B2_phash_veto": 1,
        "B3_fanout_defer": 3,
        "B4_high_name_reject": 0,
    }
    for lane in validator.B_FRESH_SAMPLE_COUNTS:
        assert (
            validator.B_FRESH_SAMPLE_COUNTS[lane]
            + len(validator.CARRY_FORWARD[lane])
            == validator.B_TOTAL_SAMPLE_COUNTS[lane]
        )


def test_anchor_and_auxiliary_classification_are_independent() -> None:
    assert validator.anchor_type(_candidate(name_strict_score=90.0)) == "name"
    assert validator.anchor_type(
        _candidate(name_strict_score=89.99, phash_le8_pair_count=1)
    ) == "image"
    assert validator.anchor_type(
        _candidate(name_strict_score=90.0, exact_asset_pair_count=1)
    ) == "both"
    assert validator.anchor_type(_candidate(name_strict_score=89.99)) == "none"
    statuses = {
        "architect_firm_identity": "positive",
        "location_site_identity": "unknown",
        "source_url_external_stable_id": "unknown",
        "year_phase_compatibility": "positive",
        "name_alias_identity": "positive",
        "verified_pixel_image_overlap": "positive",
    }
    assert (
        validator.auxiliary_family_key(statuses)
        == "architect_firm_identity+year_phase_compatibility"
    )


def test_b_predicates_are_mechanism_specific_and_precedenced() -> None:
    name_anchor = _candidate()
    image_anchor = _candidate(name_strict_score=80.0, exact_asset_pair_count=1)
    assert validator.is_b1(image_anchor, ["matcher_architect_year_gate_block"])
    assert not validator.is_b1(
        image_anchor,
        ["matcher_architect_year_gate_block", "project_phase_incompatible"],
    )
    assert validator.is_b2(name_anchor, ["verified_phash_veto"])
    assert not validator.is_b2(image_anchor, ["verified_phash_veto"])
    assert validator.is_b3(
        _candidate(proposal="defer"), ["anchor_missing", "fanout_conflict"]
    )
    assert validator.is_b4(name_anchor, ["verified_phash_veto"])
    assert not validator.is_b4(
        name_anchor, ["verified_phash_veto"], assigned_upstream=True
    )


def test_clopper_pearson_95_replays_boundary_and_interior_values() -> None:
    lower, upper = validator.clopper_pearson_95(70, 70)
    assert lower == pytest.approx(0.9486662028489763, abs=1e-14)
    assert upper == 1.0
    lower, upper = validator.clopper_pearson_95(0, 70)
    assert lower == 0.0
    assert upper == pytest.approx(0.05133379715102376, abs=1e-14)
    lower, upper = validator.clopper_pearson_95(35, 70)
    assert lower == pytest.approx(1.0 - upper, abs=1e-14)
    assert lower < 0.5 < upper
    with pytest.raises(ValueError):
        validator.clopper_pearson_95(1, 0)
    with pytest.raises(ValueError):
        validator.clopper_pearson_95(71, 70)


def test_validator_source_never_imports_the_producer() -> None:
    source_path = Path("canonical/vnext_identity_n100_validator.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert "canonical.vnext_identity_n100" not in imports
    assert "vnext_identity_n100" not in imports


def test_missing_artifact_fails_closed_without_a_report(tmp_path: Path) -> None:
    result = validator.validate_identity_n100(
        repo_root=tmp_path,
        artifact_path=Path("missing.db"),
        report_path=None,
        verify_full_hashes=False,
    )
    assert not result.passed
    assert "v3_artifact_exists" in result.failed_check_names
    assert "artifact_exists" in result.failed_check_names


def test_json_safe_handles_heterogeneous_collections_canonically() -> None:
    value = {
        "tuple": (1, ["pass"]),
        "list": [3, "2", (1, "x")],
        "set": frozenset({1, "1", (2, "x")}),
        1: "integer key",
        ("cell", 2): {Path("a"), 4},
    }
    first = validator._json_safe(value)
    second = validator._json_safe(value)
    assert first == second
    assert first["tuple"] == [1, ["pass"]]
    assert first["list"] == [3, "2", [1, "x"]]
    assert json.dumps(first, ensure_ascii=False, sort_keys=True)


def test_report_writer_serializes_mixed_tuple_without_sorting(tmp_path: Path) -> None:
    report = tmp_path / "sample_validation.json"
    result = validator.ValidationResult(
        artifact_path=tmp_path / "sample.db",
        report_path=report,
        run_id="run",
        checks=(
            validator.Check(
                "v3_single_final_pass_run",
                True,
                (1, ["pass"]),
                (1, ["pass"]),
            ),
        ),
        metrics={"mixed": {1, "1", (2, "x")}},
        phase="sample",
    )
    validator._write_report(result, report)
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["phase"] == "sample"
    assert payload["checks"][0]["expected"] == [1, ["pass"]]


def test_invalid_phase_is_rejected_before_io(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="phase"):
        validator.validate_identity_n100(
            repo_root=tmp_path,
            artifact_path=Path("missing.db"),
            report_path=None,
            verify_full_hashes=False,
            phase="prepare",
        )


def test_sample_logical_sha_excludes_post_freeze_source_pins() -> None:
    connection = sqlite3.connect(":memory:")
    for table in (
        "input_pins",
        "review_exclusions",
        "carry_forward_gold",
        "population_cells",
        "sample_pairs",
        "candidate_snapshots",
    ):
        connection.execute(
            f"CREATE TABLE {table}(run_id TEXT, role TEXT, candidate_id TEXT, "
            "prior_version TEXT, cohort TEXT, stratum TEXT, anchor_type TEXT, "
            "auxiliary_mask TEXT, sample_rank INTEGER, sample_id TEXT, record_sha256 TEXT)"
        )
    for role in ("v3_candidates_final", "n100_protocol", "wp23_order", "e2_image_evidence"):
        connection.execute(
            "INSERT INTO input_pins(run_id,role,record_sha256) VALUES('run',?,?)",
            (role, hashlib.sha256(role.encode()).hexdigest()),
        )
    before = validator._sample_logical_sha(connection, "run")
    connection.execute(
        "UPDATE input_pins SET record_sha256=? WHERE role='e2_image_evidence'",
        ("f" * 64,),
    )
    assert validator._sample_logical_sha(connection, "run") == before
    connection.execute(
        "UPDATE input_pins SET record_sha256=? WHERE role='n100_protocol'",
        ("e" * 64,),
    )
    assert validator._sample_logical_sha(connection, "run") != before
    connection.close()


def test_record_sha_replay_detects_semantic_json_tamper() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE metrics(run_id TEXT,phase TEXT,metric TEXT,value_json TEXT,record_sha256 TEXT)"
    )
    payload = {"phase": "gate", "metric": "core", "value": {"same": 70}}
    connection.execute(
        "INSERT INTO metrics VALUES(?,?,?,?,?)",
        ("run", "gate", "core", '{"same":70}', validator._record_sha(payload)),
    )
    assert validator._record_sha_failures(connection, {"metrics"}) == []
    connection.execute("UPDATE metrics SET value_json='{\"same\":69}'")
    assert validator._record_sha_failures(connection, {"metrics"}) == ["metrics[1]"]
    connection.close()


def test_blank_form_origin_hashes_survive_in_place_completion() -> None:
    reviewer_items = [
        {
            "display_order": 1,
            "opaque_review_id": "r1_opaque",
            "sheet_path": "sheets/r1_opaque.html",
            "sheet_sha256": "a" * 64,
        }
    ]
    blank_reviewer = validator._blank_reviewer_form_payload(
        "run", "r1", reviewer_items
    )
    blank_reviewer_sha = validator._artifact_json_sha(blank_reviewer)
    completed_reviewer = json.loads(json.dumps(blank_reviewer))
    completed_reviewer.update(
        {"reviewer": "reviewer-a", "reviewed_at": "2026-08-26T00:00:00Z"}
    )
    completed_reviewer["items"][0].update(
        {
            "label": "same",
            "rationale": "same project",
            "hard_conflict_observed": "none",
        }
    )
    assert validator._artifact_json_sha(completed_reviewer) != blank_reviewer_sha
    assert (
        validator._artifact_json_sha(
            validator._blank_reviewer_form_payload("run", "r1", reviewer_items)
        )
        == blank_reviewer_sha
    )

    samples = [
        {"sample_rank": 1, "sample_id": "sample-1", "candidate_id": "candidate-1"}
    ]
    reviews = [
        {"sample_id": "sample-1", "lane": "r1", "label": "same"},
        {"sample_id": "sample-1", "lane": "r2", "label": "related"},
    ]
    blank_adjudication = validator._blank_adjudication_form_payload(
        "run", samples, reviews
    )
    blank_adjudication_sha = validator._artifact_json_sha(blank_adjudication)
    completed_adjudication = json.loads(json.dumps(blank_adjudication))
    completed_adjudication.update(
        {
            "adjudicator": "adjudicator-c",
            "adjudicated_at": "2026-08-26T00:01:00Z",
        }
    )
    completed_adjudication["items"][0].update(
        {"adjudicator_label": "same", "rationale": "evidence resolves it"}
    )
    assert (
        validator._artifact_json_sha(completed_adjudication)
        != blank_adjudication_sha
    )
    assert (
        validator._artifact_json_sha(
            validator._blank_adjudication_form_payload("run", samples, reviews)
        )
        == blank_adjudication_sha
    )


def test_asset_selection_never_adds_representative_after_direct_cap() -> None:
    v3 = sqlite3.connect(":memory:")
    v3.row_factory = sqlite3.Row
    v3.execute(
        "CREATE TABLE direct_image_evidence(candidate_id TEXT,evidence_kind TEXT,"
        "phash_distance INTEGER,e2_evidence_id TEXT,left_source_asset_id TEXT,"
        "right_source_asset_id TEXT)"
    )
    v3.executemany(
        "INSERT INTO direct_image_evidence VALUES('candidate',?,?,?,?,?)",
        [
            ("exact_pixel", 0, "e1", "left-direct-1", "right-direct-1"),
            ("identical_phash", 0, "e2", "left-direct-2", "right-direct-2"),
        ],
    )

    e2 = sqlite3.connect(":memory:")
    e2.row_factory = sqlite3.Row
    e2.execute("CREATE TABLE e2_runs(run_id TEXT)")
    e2.execute("INSERT INTO e2_runs VALUES('e2-run')")
    e2.execute(
        "CREATE TABLE building_assets(run_id TEXT,source TEXT,"
        "source_building_id TEXT,source_asset_id TEXT)"
    )
    e2.execute(
        "CREATE TABLE assets(run_id TEXT,source TEXT,source_asset_id TEXT,"
        "original_width INTEGER,original_height INTEGER,fingerprint_status TEXT)"
    )
    for source, building, asset in (
        ("left-source", "left-building", "left-representative"),
        ("right-source", "right-building", "right-representative"),
    ):
        e2.execute(
            "INSERT INTO building_assets VALUES('e2-run',?,?,?)",
            (source, building, asset),
        )
        e2.execute(
            "INSERT INTO assets VALUES('e2-run',?,?,2000,1500,'success')",
            (source, asset),
        )

    selected = validator._expected_assets_for_candidate(
        v3,
        e2,
        {
            "candidate_id": "candidate",
            "left_source": "left-source",
            "left_source_building_id": "left-building",
            "right_source": "right-source",
            "right_source_building_id": "right-building",
        },
    )
    assert selected == {
        "left": [
            ("left-direct-1", "direct_evidence_1"),
            ("left-direct-2", "direct_evidence_2"),
        ],
        "right": [
            ("right-direct-1", "direct_evidence_1"),
            ("right-direct-2", "direct_evidence_2"),
        ],
    }
    v3.close()
    e2.close()


def test_cache_tree_hash_binds_relative_path_bytes_and_size(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    nested = cache / "aa"
    nested.mkdir(parents=True)
    asset = nested / "asset.jpg"
    asset.write_bytes(b"first")
    before = validator._cache_tree(cache)
    assert before[:2] == (1, 5)
    asset.write_bytes(b"other")
    after = validator._cache_tree(cache)
    assert after[:2] == (1, 5)
    assert after[2] != before[2]


def test_state_transition_validator_requires_lock_barrier_order() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE state_events(run_id TEXT,event_no INTEGER,from_status TEXT,"
        "to_status TEXT,detail_json TEXT,occurred_at TEXT,record_sha256 TEXT)"
    )
    statuses = [
        "building", "sample_frozen", "fetching", "review_ready",
        "reviews_partial", "reviews_locked", "unblinded", "pass",
    ]
    for number, (source, target) in enumerate(zip(statuses, statuses[1:]), 1):
        connection.execute(
            "INSERT INTO state_events VALUES('run',?,?,?,?,?,?)",
            (number, source, target, "{}", f"2026-08-26T00:00:0{number}Z", "x"),
        )
    checks: list[validator.Check] = []
    validator._validate_state_events(connection, {"run_id": "run", "status": "pass"}, checks)
    assert all(check.passed for check in checks)
    connection.execute(
        "UPDATE state_events SET to_status='unblinded' WHERE event_no=5"
    )
    checks = []
    validator._validate_state_events(connection, {"run_id": "run", "status": "pass"}, checks)
    assert "state_transition_chain" in {check.name for check in checks if not check.passed}
    connection.close()


def test_sample_state_transition_stops_at_freeze() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE state_events(run_id TEXT,event_no INTEGER,from_status TEXT,"
        "to_status TEXT,detail_json TEXT,occurred_at TEXT,record_sha256 TEXT)"
    )
    connection.execute(
        "INSERT INTO state_events VALUES('run',1,'building','sample_frozen','{}',"
        "'2026-08-26T00:00:01Z','x')"
    )
    checks: list[validator.Check] = []
    validator._validate_state_events(
        connection,
        {"run_id": "run", "status": "sample_frozen"},
        checks,
        phase="sample",
    )
    assert all(check.passed for check in checks)
    connection.close()
