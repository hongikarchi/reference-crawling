from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from canonical.cross_source_semantic_fetch import FetchFailure
from canonical import vnext_identity_n100 as n100


V3_SCHEMA = """
CREATE TABLE runs(run_id TEXT PRIMARY KEY,status TEXT NOT NULL);
CREATE TABLE candidate_pairs(
 run_id TEXT,candidate_id TEXT PRIMARY KEY,left_source TEXT,left_source_building_id TEXT,
 right_source TEXT,right_source_building_id TEXT,proposal TEXT,auto_merge_eligible INTEGER,
 positive_family_count INTEGER,name_strict_score REAL,record_sha256 TEXT
);
CREATE TABLE evidence_families(
 run_id TEXT,candidate_id TEXT,family TEXT,status TEXT,score REAL,evidence_json TEXT,record_sha256 TEXT
);
CREATE TABLE hard_conflicts(
 run_id TEXT,candidate_id TEXT,conflict_type TEXT,detail_json TEXT,record_sha256 TEXT
);
CREATE TABLE candidate_policy_flags(
 run_id TEXT,candidate_id TEXT,flag_type TEXT,detail_json TEXT,record_sha256 TEXT
);
CREATE TABLE prior_review_exclusions(
 run_id TEXT,candidate_id TEXT,prior_version TEXT,prior_opaque_review_id TEXT,record_sha256 TEXT
);
CREATE TABLE sample_neighbourhoods(
 run_id TEXT,sample_rank INTEGER,stratum TEXT,candidate_id TEXT,opaque_review_id TEXT,
 frozen_at TEXT,seed_rank_sha256 TEXT,record_sha256 TEXT
);
CREATE TABLE blind_reviews(
 run_id TEXT,opaque_review_id TEXT,label TEXT,rationale TEXT,hard_conflict_observed TEXT,
 reviewer TEXT,review_version TEXT,reviewed_at TEXT,record_sha256 TEXT
);
CREATE TABLE adjudications(
 run_id TEXT,opaque_review_id TEXT,reviewer_label TEXT,adjudicator_label TEXT,agrees INTEGER,
 final_gold_label TEXT,mapped_decision TEXT,rationale TEXT,adjudicator TEXT,
 adjudication_version TEXT,adjudicated_at TEXT,record_sha256 TEXT
);
CREATE TABLE source_buildings(
 run_id TEXT,source TEXT,source_building_id TEXT,name TEXT,aliases_json TEXT,firms_json TEXT,
 country TEXT,locality TEXT,completion_year_min INTEGER,completion_year_max INTEGER,record_sha256 TEXT
);
CREATE TABLE candidate_origins(run_id TEXT,candidate_id TEXT,origin TEXT,record_sha256 TEXT);
CREATE TABLE direct_image_evidence(
 run_id TEXT,candidate_id TEXT,evidence_kind TEXT,phash_distance INTEGER,e2_evidence_id TEXT,
 left_source_asset_id TEXT,right_source_asset_id TEXT
);
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _make_v3(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(V3_SCHEMA)
    run_id = "v3-test-run"
    connection.execute("INSERT INTO runs VALUES(?,?)", (run_id, "pass"))

    def add_candidate(
        candidate_id: str,
        *,
        proposal: str,
        auto: int,
        positive_count: int,
        name_score: float,
        families: tuple[str, ...],
        conflict: str | None = None,
        fanout: bool = False,
    ) -> None:
        left_id, right_id = f"left-{candidate_id}", f"right-{candidate_id}"
        connection.execute(
            "INSERT INTO candidate_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                candidate_id,
                "divisare",
                left_id,
                "architizer",
                right_id,
                proposal,
                auto,
                positive_count,
                name_score,
                _digest("pair-" + candidate_id),
            ),
        )
        for source, building_id in (("divisare", left_id), ("architizer", right_id)):
            connection.execute(
                "INSERT INTO source_buildings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    source,
                    building_id,
                    f"Building {candidate_id}",
                    "[]",
                    '["Studio"]',
                    "KR",
                    "Seoul",
                    2020,
                    2020,
                    _digest(f"building-{source}-{building_id}"),
                ),
            )
        for family in families:
            connection.execute(
                "INSERT INTO evidence_families VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    candidate_id,
                    family,
                    "positive",
                    1.0,
                    "{}",
                    _digest(f"family-{candidate_id}-{family}"),
                ),
            )
        if conflict:
            connection.execute(
                "INSERT INTO hard_conflicts VALUES(?,?,?,?,?)",
                (run_id, candidate_id, conflict, "{}", _digest("conflict-" + candidate_id)),
            )
        if fanout:
            connection.execute(
                "INSERT INTO candidate_policy_flags VALUES(?,?,?,?,?)",
                (run_id, candidate_id, "fanout_conflict", "{}", _digest("flag-" + candidate_id)),
            )
        connection.execute(
            "INSERT INTO candidate_origins VALUES(?,?,?,?)",
            (run_id, candidate_id, "metadata_block", _digest("origin-" + candidate_id)),
        )

    for index in range(90):
        add_candidate(
            f"a{index:03d}",
            proposal="merge_candidate",
            auto=1,
            positive_count=3,
            name_score=95,
            families=(
                "name_alias_identity",
                "verified_pixel_image_overlap",
                "architect_firm_identity",
            ),
        )

    b1 = list(n100.CARRY_FORWARD["B1_year_gate"]) + [f"b1-{index}" for index in range(9)]
    b2 = list(n100.CARRY_FORWARD["B2_phash_veto"]) + [f"b2-{index}" for index in range(9)]
    b3 = list(n100.CARRY_FORWARD["B3_fanout_defer"]) + [f"b3-{index}" for index in range(7)]
    b4 = [f"b4-{index}" for index in range(10)]
    for candidate_id in b1:
        add_candidate(
            candidate_id,
            proposal="reject",
            auto=0,
            positive_count=2,
            name_score=95,
            families=("name_alias_identity", "architect_firm_identity"),
            conflict="matcher_architect_year_gate_block",
        )
    for candidate_id in b2:
        add_candidate(
            candidate_id,
            proposal="reject",
            auto=0,
            positive_count=2,
            name_score=95,
            families=("name_alias_identity", "architect_firm_identity"),
            conflict="verified_phash_veto",
        )
    for candidate_id in b3:
        add_candidate(
            candidate_id,
            proposal="defer",
            auto=0,
            positive_count=1,
            name_score=70,
            families=("name_alias_identity",),
            conflict="fanout_conflict",
            fanout=True,
        )
    for candidate_id in b4:
        add_candidate(
            candidate_id,
            proposal="reject",
            auto=0,
            positive_count=1,
            name_score=92,
            families=("name_alias_identity",),
        )

    prior_ids = [f"prior-{index:02d}" for index in range(20)]
    v3_other = [f"v3-reviewed-{index}" for index in range(5)]
    for candidate_id in prior_ids + v3_other:
        add_candidate(
            candidate_id,
            proposal="related",
            auto=0,
            positive_count=1,
            name_score=50,
            families=("name_alias_identity",),
        )
    for index, candidate_id in enumerate(prior_ids):
        version = "v1" if index < 10 else "v2"
        connection.execute(
            "INSERT INTO prior_review_exclusions VALUES(?,?,?,?,?)",
            (run_id, candidate_id, version, f"old-{index}", _digest("old-" + candidate_id)),
        )
    current_ids = [item for values in n100.CARRY_FORWARD.values() for item in values] + v3_other
    for index, candidate_id in enumerate(current_ids, 1):
        opaque = f"v3-blind-{index}"
        connection.execute(
            "INSERT INTO sample_neighbourhoods VALUES(?,?,?,?,?,?,?,?)",
            (run_id, index, "strong_merge", candidate_id, opaque, "now", _digest("rank-" + candidate_id), _digest("sample-" + candidate_id)),
        )
        connection.execute(
            "INSERT INTO blind_reviews VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, opaque, "same", "v3 reviewer rationale", "none", "v3 reviewer", "v3", "now", _digest("review-" + candidate_id)),
        )
        connection.execute(
            "INSERT INTO adjudications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, opaque, "same", "same", 1, "same", "merge", "v3 adjudicator rationale", "v3 adjudicator", "v3", "now", _digest("adjudication-" + candidate_id)),
        )
    connection.commit()
    connection.close()


def _build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    v3 = tmp_path / "v3.db"
    _make_v3(v3)
    protocol = tmp_path / "protocol.md"
    order = tmp_path / "order.md"
    protocol.write_text("protocol", encoding="utf-8")
    order.write_text("order", encoding="utf-8")
    output = tmp_path / "n100.db"
    n100.build_sample(
        repo_root=tmp_path,
        v3_path=v3,
        output_path=output,
        manifest_path=tmp_path / "sample.json",
        protocol_path=protocol,
        order_path=order,
        verify_v3_pin=False,
    )
    return v3, output


def _make_image_inputs(tmp_path: Path, v3: Path):
    from PIL import Image

    e2 = tmp_path / "e2.db"
    connection = sqlite3.connect(e2)
    connection.executescript(
        """
        CREATE TABLE e2_runs(run_id TEXT);
        CREATE TABLE assets(
          run_id TEXT,source TEXT,source_asset_id TEXT,fingerprint_status TEXT,
          canonical_url TEXT,fetch_url TEXT,final_url TEXT,raw_response_sha256 TEXT,
          normalized_pixel_sha256 TEXT,phash_hex TEXT,original_width INTEGER,
          original_height INTEGER,source_record_sha256 TEXT
        );
        CREATE TABLE building_assets(run_id TEXT,source TEXT,source_building_id TEXT,source_asset_id TEXT);
        """
    )
    connection.execute("INSERT INTO e2_runs VALUES('e2-run')")
    specs = {
        "divisare": ("shared-divisare", "1" * 64, "3" * 64),
        "architizer": ("shared-architizer", "2" * 64, "4" * 64),
    }
    for source, (asset_id, raw_sha, pixel_sha) in specs.items():
        connection.execute(
            "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e2-run", source, asset_id, "success", "https://example.invalid", "https://example.invalid", "https://example.invalid", raw_sha, pixel_sha, "0" * 16, 64, 64, _digest(source)),
        )
    source_db = sqlite3.connect(v3)
    for source, building_id in source_db.execute("SELECT source,source_building_id FROM source_buildings"):
        connection.execute(
            "INSERT INTO building_assets VALUES(?,?,?,?)",
            ("e2-run", source, building_id, specs[source][0]),
        )
    source_db.close()
    connection.commit()
    connection.close()

    e1_specs = {}
    for source, (asset_id, raw_sha, pixel_sha) in specs.items():
        path = tmp_path / f"e1-{source}.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE fingerprint_runs(run_id TEXT);
            CREATE TABLE source_assets(run_id TEXT,source_asset_id TEXT,fetch_url TEXT,source_record_sha256 TEXT);
            CREATE TABLE fingerprints(
              run_id TEXT,source_asset_id TEXT,status TEXT,raw_response_sha256 TEXT,
              normalized_pixel_sha256 TEXT,selected_attempt_no INTEGER
            );
            """
        )
        connection.execute("INSERT INTO fingerprint_runs VALUES('e1-run')")
        connection.execute(
            "INSERT INTO source_assets VALUES(?,?,?,?)",
            ("e1-run", asset_id, "https://example.invalid", _digest("e1-" + source)),
        )
        connection.execute(
            "INSERT INTO fingerprints VALUES(?,?,?,?,?,?)",
            ("e1-run", asset_id, "success", raw_sha, pixel_sha, 1),
        )
        connection.commit()
        connection.close()
        e1_specs[source] = (path, path.stat().st_size, n100.sha256_file(path))

    cache = tmp_path / "read-cache"
    for index, (_source, (_asset_id, _raw_sha, pixel_sha)) in enumerate(specs.items()):
        target = cache / n100._cache_relative(pixel_sha, n100.n10_review.TRANSFORM_VERSION)
        target.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (64, 64), (50 + index * 100, 80, 120))
        image.save(target, format="JPEG", quality=92)
    count, total, tree = n100.cache_tree(cache)
    caches = (
        n100.CacheSpec(
            "fixture_cache", cache, n100.n10_review.TRANSFORM_VERSION, count, total, tree
        ),
    )
    return (e2, e2.stat().st_size, n100.sha256_file(e2)), e1_specs, caches


def test_hierarchical_hamilton_and_cp95() -> None:
    populations = {("both", "x"): 2687, ("image", "x"): 1436, ("name", "x"): 27}
    assert n100.hierarchical_hamilton(populations, 70) == {
        ("both", "x"): 45,
        ("image", "x"): 24,
        ("name", "x"): 1,
    }
    low, high = n100.clopper_pearson(70, 70)
    assert low == pytest.approx(0.948666, abs=1e-6)
    assert high == 1.0


def test_b_predicate_boundaries() -> None:
    base = {
        "candidate_id": "x",
        "proposal": "reject",
        "positive_family_count": 2,
        "name_pos": 1,
        "image_pos": 0,
        "name_strict_score": 91,
    }
    conflicts = {"x": frozenset({"matcher_architect_year_gate_block"})}
    assert [row["candidate_id"] for row in n100._b_pool("B1_year_gate", [base], conflicts, set())] == ["x"]
    assert not n100._b_pool(
        "B1_year_gate", [{**base, "positive_family_count": 1}], conflicts, set()
    )
    assert not n100._b_pool(
        "B1_year_gate", [base], {"x": frozenset({"matcher_architect_year_gate_block", "extra"})}, set()
    )
    assert not n100._b_pool(
        "B2_phash_veto",
        [{**base, "name_pos": 0, "image_pos": 1}],
        {"x": frozenset({"verified_phash_veto"})},
        set(),
    )
    fanout = {**base, "proposal": "defer"}
    assert n100._b_pool(
        "B3_fanout_defer", [fanout], {"x": frozenset({"fanout_conflict"})}, {"x"}
    )
    assert not n100._b_pool(
        "B3_fanout_defer", [fanout], {"x": frozenset({"fanout_conflict"})}, set()
    )


def test_choose_assets_never_adds_representative_after_two_direct_assets() -> None:
    v3 = sqlite3.connect(":memory:")
    v3.row_factory = sqlite3.Row
    v3.execute(
        "CREATE TABLE direct_image_evidence("
        "candidate_id TEXT,evidence_kind TEXT,phash_distance INTEGER,e2_evidence_id TEXT,"
        "left_source_asset_id TEXT,right_source_asset_id TEXT)"
    )
    v3.executemany(
        "INSERT INTO direct_image_evidence VALUES(?,?,?,?,?,?)",
        [
            ("candidate", "exact_pixel", 0, "e1", "left-direct-1", "right-direct-1"),
            ("candidate", "identical_phash", 0, "e2", "left-direct-2", "right-direct-2"),
        ],
    )
    e2 = sqlite3.connect(":memory:")
    e2.row_factory = sqlite3.Row
    e2.executescript(
        """
        CREATE TABLE e2_runs(run_id TEXT);
        INSERT INTO e2_runs VALUES('run');
        CREATE TABLE assets(
          run_id TEXT,source TEXT,source_asset_id TEXT,fingerprint_status TEXT,
          original_width INTEGER,original_height INTEGER
        );
        CREATE TABLE building_assets(
          run_id TEXT,source TEXT,source_building_id TEXT,source_asset_id TEXT
        );
        """
    )
    for source, building_id, asset_id in (
        ("divisare", "left-building", "left-representative"),
        ("architizer", "right-building", "right-representative"),
    ):
        e2.execute(
            "INSERT INTO assets VALUES(?,?,?,?,?,?)",
            ("run", source, asset_id, "success", 4000, 3000),
        )
        e2.execute(
            "INSERT INTO building_assets VALUES(?,?,?,?)",
            ("run", source, building_id, asset_id),
        )
    selected = n100._choose_assets(
        v3,
        e2,
        {
            "candidate_id": "candidate",
            "left_source": "divisare",
            "left_source_building_id": "left-building",
            "right_source": "architizer",
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
    assert all(len(items) == n100.MAX_ASSETS_PER_SIDE for items in selected.values())
    v3.close()
    e2.close()


def test_build_freezes_70_plus_40_and_carry_provenance(tmp_path: Path) -> None:
    _v3, output = _build_fixture(tmp_path)
    connection = sqlite3.connect(output)
    connection.row_factory = sqlite3.Row
    counts = connection.execute(
        "SELECT cohort,sample_origin,count(*) count FROM sample_pairs GROUP BY cohort,sample_origin"
    ).fetchall()
    assert {(row["cohort"], row["sample_origin"]): row["count"] for row in counts} == {
        ("A", "fresh"): 70,
        ("B", "fresh"): 35,
        ("B", "carry_forward"): 5,
    }
    assert connection.execute("SELECT count(*) FROM carry_forward_gold").fetchone()[0] == 5
    assert connection.execute("SELECT count(*) FROM review_exclusions").fetchone()[0] == 30
    run = connection.execute("SELECT * FROM runs").fetchone()
    assert run["seed"] == "vnext-wp23-identity-n100-v1-20260826"
    assert run["sample_logical_sha256"] == n100._logical_sha(connection, run["run_id"])
    assert connection.execute(
        "SELECT count(*) FROM sample_pairs WHERE candidate_id IN (SELECT candidate_id FROM sample_pairs GROUP BY candidate_id HAVING count(*)>1)"
    ).fetchone()[0] == 0
    connection.close()


def _make_form_ready(tmp_path: Path, artifact: Path) -> tuple[Path, Path]:
    connection = sqlite3.connect(artifact)
    connection.row_factory = sqlite3.Row
    run_id = connection.execute("SELECT run_id FROM runs").fetchone()[0]
    connection.execute("UPDATE runs SET status='fetching' WHERE run_id=?", (run_id,))
    fresh = connection.execute(
        "SELECT sample_id FROM sample_pairs WHERE sample_origin='fresh' ORDER BY sample_rank"
    ).fetchall()
    lane_items: dict[str, list[dict[str, object]]] = {"r1": [], "r2": []}
    for lane in ("r1", "r2"):
        for order, row in enumerate(fresh, 1):
            opaque = f"{lane}-opaque-{order:03d}"
            sheet = tmp_path / f"{opaque}.png"
            sheet.write_bytes(f"sheet-{lane}-{order}".encode())
            digest = n100.sha256_file(sheet)
            payload = {
                "lane": lane,
                "display_order": order,
                "opaque_review_id": opaque,
                "sample_id": row["sample_id"],
                "sheet_path": sheet.as_posix(),
                "sheet_sha256": digest,
                "sheet_bytes": sheet.stat().st_size,
            }
            connection.execute(
                "INSERT INTO review_items VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, lane, order, opaque, row["sample_id"], sheet.as_posix(), digest, sheet.stat().st_size, n100._record(payload)),
            )
            lane_items[lane].append(
                {
                    "display_order": order,
                    "opaque_review_id": opaque,
                    "sheet_path": sheet.as_posix(),
                    "sheet_sha256": digest,
                    "label": "same",
                    "rationale": "independent identity evidence agrees",
                    "hard_conflict_observed": "none",
                }
            )
    connection.execute("UPDATE runs SET status='review_ready' WHERE run_id=?", (run_id,))
    n100._refresh_run_record(connection, run_id)
    connection.commit()
    connection.close()
    paths = []
    for lane, reviewer in (("r1", "Reviewer One"), ("r2", "Reviewer Two")):
        form = n100._form_payload(run_id, lane, lane_items[lane])
        form["reviewer"] = reviewer
        form["reviewed_at"] = "2026-08-26T00:00:00Z"
        path = tmp_path / f"{lane}-completed.json"
        path.write_text(json.dumps(form), encoding="utf-8")
        paths.append(path)
    return paths[0], paths[1]


def test_dual_lock_unblind_and_final_gold_gate(tmp_path: Path) -> None:
    _v3, artifact = _build_fixture(tmp_path)
    r1_form, r2_form = _make_form_ready(tmp_path, artifact)
    n100.lock_review(
        repo_root=tmp_path,
        artifact_path=artifact,
        lane="r1",
        completed_form_path=r1_form,
        locked_output_path=tmp_path / "r1-locked.json",
    )
    with pytest.raises(n100.IdentityN100Error, match="both review lanes"):
        n100.unblind_reviews(
            repo_root=tmp_path,
            artifact_path=artifact,
            packet_path=tmp_path / "too-early-packet.json",
            adjudication_form_path=tmp_path / "too-early-form.json",
        )
    assert not (tmp_path / "too-early-packet.json").exists()
    same_actor = json.loads(r2_form.read_text(encoding="utf-8"))
    same_actor["reviewer"] = " reviewer one "
    r2_form.write_text(json.dumps(same_actor), encoding="utf-8")
    with pytest.raises(n100.IdentityN100Error, match="distinct reviewers"):
        n100.lock_review(
            repo_root=tmp_path,
            artifact_path=artifact,
            lane="r2",
            completed_form_path=r2_form,
            locked_output_path=tmp_path / "r2-bad-locked.json",
        )
    same_actor["reviewer"] = "Reviewer Two"
    r2_form.write_text(json.dumps(same_actor), encoding="utf-8")
    n100.lock_review(
        repo_root=tmp_path,
        artifact_path=artifact,
        lane="r2",
        completed_form_path=r2_form,
        locked_output_path=tmp_path / "r2-locked.json",
    )
    packet, adjudication_form = n100.unblind_reviews(
        repo_root=tmp_path,
        artifact_path=artifact,
        packet_path=tmp_path / "packet.json",
        adjudication_form_path=tmp_path / "adjudication.json",
    )
    assert len(json.loads(packet.read_text(encoding="utf-8"))["items"]) == 105
    completed = json.loads(adjudication_form.read_text(encoding="utf-8"))
    completed["adjudicator"] = "Adjudicator Three"
    completed["adjudicated_at"] = "2026-08-26T01:00:00Z"
    for item in completed["items"]:
        item["adjudicator_label"] = "same"
        item["rationale"] = "both blind reviews and frozen evidence agree"
    adjudication_form.write_text(json.dumps(completed), encoding="utf-8")
    result = n100.finalize_review(
        repo_root=tmp_path,
        artifact_path=artifact,
        completed_adjudication_path=adjudication_form,
        locked_adjudication_path=tmp_path / "adjudication-locked.json",
        gate_path=tmp_path / "gate.json",
        report_path=tmp_path / "report.json",
        manifest_path=tmp_path / "manifest.json",
    )
    assert result.status == "PASS"
    assert result.false_merge_count == 0
    assert result.unreviewable_count == 0
    assert result.cp95_low == pytest.approx(0.948666, abs=1e-6)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["review_completion"] == {
        "r1_fresh": "105/105",
        "r2_fresh": "105/105",
        "fresh_adjudication": "105/105",
        "carry_provenance": "5/5",
    }
    assert report["diagnostic_b"]["B1_year_gate"]["fresh"]["denominator"] == 9
    assert report["diagnostic_b"]["B1_year_gate"]["combined_descriptive"]["denominator"] == 10
    assert json.loads((tmp_path / "gate.json").read_text(encoding="utf-8"))["human_approval_required"] is True


def test_prepare_reuses_global_pinned_cache_without_network(tmp_path: Path) -> None:
    v3, artifact = _build_fixture(tmp_path)
    e2_spec, e1_specs, caches = _make_image_inputs(tmp_path, v3)

    def forbidden_fetch(*_args, **_kwargs):
        raise AssertionError("network must not be called on a complete cache hit")

    result = n100.prepare_review(
        repo_root=tmp_path,
        artifact_path=artifact,
        output_cache_path=tmp_path / "output-cache",
        sheets_root=tmp_path / "sheets",
        r1_form_path=tmp_path / "r1-draft.json",
        r2_form_path=tmp_path / "r2-draft.json",
        fetch_manifest_path=tmp_path / "fetch-manifest.json",
        fetch_report_path=tmp_path / "fetch-report.json",
        e2_spec=e2_spec,
        e1_specs=e1_specs,
        read_caches=caches,
        workers=2,
        fetcher=forbidden_fetch,
        verify_source_pins=True,
        verify_cache_pins=True,
    )
    assert result.asset_occurrence_count == 210
    assert result.unique_asset_count == 2
    assert result.cache_hits == 2
    assert result.logical_http_fetches == 0
    assert result.wire_http_requests == 0
    assert result.sheet_count == 210
    assert len(json.loads((tmp_path / "r1-draft.json").read_text(encoding="utf-8"))["items"]) == 105


def test_fetch_wrapper_forces_zero_redirects() -> None:
    observed: list[int] = []

    def blocked(source: str, url: str, *, max_redirects: int):
        observed.append(max_redirects)
        raise FetchFailure(
            "blocked",
            "blocked in unit test",
            retryable=False,
            request_url=url,
            request_count=1,
        )

    outcome = n100._fetch_one(
        {
            "asset_key": "asset",
            "source": "divisare",
            "expected_url": "https://example.invalid/image.jpg",
            "expected_raw_sha256": "0" * 64,
            "expected_pixel_sha256": "1" * 64,
        },
        blocked,
    )
    assert observed == [0]
    assert outcome.status == "failed"
    assert outcome.request_count == 1
    assert n100.MAX_ASSET_OCCURRENCES == 420
    assert n100.MAX_LOGICAL_HTTP_FETCHES == 420
