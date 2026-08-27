from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from canonical import vnext_identity_band_precision as wp25
from canonical import vnext_identity_instrument_probe as probe
from canonical.cross_source_image_selection import canonical_sha256


REPO_ROOT = Path(__file__).resolve().parents[1]


def _population_fixture() -> tuple[sqlite3.Connection, sqlite3.Connection, sqlite3.Connection]:
    n100 = sqlite3.connect(":memory:"); n100.row_factory = sqlite3.Row
    v4 = sqlite3.connect(":memory:"); v4.row_factory = sqlite3.Row
    band = sqlite3.connect(":memory:"); band.row_factory = sqlite3.Row
    n100.executescript("""
        CREATE TABLE sample_pairs(
          run_id TEXT, sample_rank INTEGER, sample_id TEXT, candidate_id TEXT,
          cohort TEXT, stratum TEXT
        );
        CREATE TABLE adjudications(
          run_id TEXT, sample_id TEXT, final_gold_label TEXT, record_sha256 TEXT
        );
    """)
    v4.executescript("""
        CREATE TABLE candidate_pairs(candidate_id TEXT PRIMARY KEY);
        CREATE TABLE candidate_policy_flags(candidate_id TEXT, flag_type TEXT);
    """)
    band.execute("CREATE TABLE sample_pairs(candidate_id TEXT)")
    rows: list[tuple[str, str, str, str]] = [
        ("p1_i1", "A", "A70", "insufficient"),
        ("p1_i2", "A", "A70", "insufficient"),
        ("p1_r1", "A", "A70", "related"),
        ("p1_flagged", "A", "A70", "different"),
        ("p2_i1", "B", "B3_fanout_defer", "insufficient"),
        ("p2_i2", "B", "B3_fanout_defer", "insufficient"),
        ("p2_i3", "B", "B3_fanout_defer", "insufficient"),
        ("p2_r1", "B", "B3_fanout_defer", "related"),
        ("p3_i1", "B", "B2_phash_veto", "insufficient"),
        ("p3_i2", "B", "B2_phash_veto", "insufficient"),
    ]
    rows.extend((f"d_{index:02d}", "A", "A70", "same") for index in range(66))
    for rank, (candidate_id, cohort, stratum, gold) in enumerate(rows, 1):
        sample_id = f"n100_{candidate_id}"
        n100.execute("INSERT INTO sample_pairs VALUES(?,?,?,?,?,?)",
                     ("n100", rank, sample_id, candidate_id, cohort, stratum))
        n100.execute("INSERT INTO adjudications VALUES(?,?,?,?)",
                     ("n100", sample_id, gold, hashlib.sha256(sample_id.encode()).hexdigest()))
        v4.execute("INSERT INTO candidate_pairs VALUES(?)", (candidate_id,))
    v4.execute("INSERT INTO candidate_policy_flags VALUES(?,?)",
               ("p1_flagged", "ambiguous_scope"))
    # P3 membership must come from the frozen N100 stratum, not current flags.
    v4.execute("INSERT INTO candidate_policy_flags VALUES(?,?)", ("p3_i1", "phash_veto"))
    band.executemany("INSERT INTO sample_pairs VALUES(?)",
                     [(f"wp25_{index:03d}",) for index in range(221)])
    n100.commit(); v4.commit(); band.commit()
    return n100, v4, band


def _insert_run(connection: sqlite3.Connection, *, status: str,
                run_id: str = "probe-run") -> None:
    payload = {"run_id": run_id, "status": status}
    connection.execute(
        "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,"
        "review_version,adjudication_version,gate_version,seed,status,sample_count,"
        "created_at,record_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, probe.SCHEMA_VERSION, probe.RUNNER_VERSION, probe.SAMPLE_VERSION,
         probe.REVIEW_VERSION, probe.ADJUDICATION_VERSION, probe.GATE_VERSION,
         probe.SEED, status, probe.TOTAL_COUNT, wp25.utc_now(), probe._record(payload)),
    )


def _minimal_frozen_sidecar(root: Path) -> tuple[Path, Path]:
    artifact = root / "probe.db"; manifest = root / "sample.json"
    manifest.write_text("{}\n", encoding="utf-8")
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.executescript(probe.PROBE_SCHEMA_SQL)
    _insert_run(connection, status="sample_frozen")
    observed = datetime.now(timezone.utc).isoformat()
    signals = [{"source": "test", "account_index": 0,
                "observed_at": observed, "remaining_percent": 99.0}]
    quota = {"observation_no": 1, "phase": "pre_freeze", "account_index": 0,
             "observed_at": observed, "signals": signals,
             "gate_basis_remaining_percent": 99.0, "start_allowed": 1,
             "stop_required": 0, "limitation": "synthetic"}
    connection.execute("INSERT INTO quota_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       ("probe-run", 1, "pre_freeze", 0, observed,
                        wp25.canonical_json(signals), 99.0, 1, 0, "synthetic",
                        probe._record(quota)))
    connection.execute("INSERT INTO artifact_paths VALUES(?,?,?,?,?)",
                       ("probe-run", "sample_manifest", manifest.as_posix(), "freeze",
                        probe._record({"role": "sample_manifest", "path": manifest.as_posix(),
                                       "stage": "freeze"})))
    logical = probe._logical_sha(connection, "probe-run")
    connection.execute("UPDATE runs SET sample_logical_sha256=? WHERE run_id='probe-run'",
                       (logical,))
    probe._refresh(connection, "probe-run")
    connection.commit(); connection.close()
    return artifact, manifest


def test_wp25_producer_pin_and_isolated_clone_adapter() -> None:
    producer = REPO_ROOT / probe.DEFAULT_WP25_PRODUCER
    assert (producer.stat().st_size, wp25.sha256_file(producer)) == probe.WP25_PRODUCER_SPEC
    before = (wp25.TOTAL_COUNT, wp25.SAMPLE_VERSION, wp25.REVIEW_VERSION,
              wp25.MAX_ASSET_OCCURRENCES)
    adapter = probe._wp25_adapter(root=REPO_ROOT, artifact=Path("unused"), prepare=False)
    prepare = adapter["prepare_review"]; lock = adapter["lock_review"]
    assert prepare.__code__ is wp25.prepare_review.__code__
    assert lock.__code__ is wp25.lock_review.__code__
    assert prepare.__globals__ is not wp25.prepare_review.__globals__
    assert prepare.__globals__["TOTAL_COUNT"] == 24
    assert prepare.__globals__["MAX_ASSET_OCCURRENCES"] == 96
    assert prepare.__globals__["REVIEW_VERSION"] == probe.REVIEW_VERSION
    assert (wp25.TOTAL_COUNT, wp25.SAMPLE_VERSION, wp25.REVIEW_VERSION,
            wp25.MAX_ASSET_OCCURRENCES) == before


def test_scrubbed_renderer_bridge_is_memory_only() -> None:
    source_tuple = ("left", "1", "right", "2")
    json_adapter = probe._ProbeJsonAdapter({source_tuple: "ctl_opaque"})
    payload = json_adapter.loads(json.dumps({
        "left_source": "left", "left_source_building_id": "1",
        "right_source": "right", "right_source_building_id": "2",
    }))
    assert payload["candidate_id"] == "ctl_opaque"
    connection = sqlite3.connect(":memory:"); connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE direct_image_evidence(candidate_id TEXT,evidence_kind TEXT)")
    connection.execute("INSERT INTO direct_image_evidence VALUES('real-id','exact_pixel')")
    proxy = probe._V4ConnectionAdapter(connection, {"ctl_opaque": "real-id"})
    row = proxy.execute(
        "SELECT * FROM direct_image_evidence WHERE candidate_id=?",
        ("ctl_opaque",),
    ).fetchone()
    assert row["candidate_id"] == "real-id"
    with pytest.raises(probe.IdentityInstrumentProbeError, match="unknown opaque"):
        proxy.execute("SELECT * FROM direct_image_evidence WHERE candidate_id=?", ("real-id",))
    proxy.close()


def test_control_universes_disjointness_and_frozen_p3() -> None:
    n100, v4, band = _population_fixture()
    try:
        selected, metrics = probe._select_controls(n100, v4, band)
    finally:
        n100.close(); v4.close(); band.close()
    assert Counter(row["group"] for row in selected) == probe.GROUP_QUOTAS
    assert metrics == {"p1_raw_non_same": 4, "p1_eligible": 3,
                       "p2_eligible": 4, "p3_eligible": 2,
                       "d_universe": 66, "d_selected": 15,
                       "wp25_population": 221, "wp25_overlap": 0}
    assert {row["candidate_id"] for row in selected if row["group"] == "P3"} == {
        "p3_i1", "p3_i2"}
    expected_d = sorted(
        [f"d_{index:02d}" for index in range(66)],
        key=lambda candidate_id: (probe._seed_rank("D", candidate_id), candidate_id),
    )[:15]
    assert {row["candidate_id"] for row in selected if row["group"] == "D"} == set(expected_d)


def test_overlap_and_wrong_group_universe_fail_closed() -> None:
    n100, v4, band = _population_fixture()
    band.execute("UPDATE sample_pairs SET candidate_id='p1_i1' WHERE candidate_id='wp25_000'")
    with pytest.raises(probe.IdentityInstrumentProbeError, match="overlaps"):
        probe._select_controls(n100, v4, band)
    band.close(); n100.close(); v4.close()

    n100, v4, band = _population_fixture()
    n100.execute("DELETE FROM sample_pairs WHERE candidate_id='d_65'")
    with pytest.raises(probe.IdentityInstrumentProbeError, match="D frozen same universe drift"):
        probe._select_controls(n100, v4, band)
    band.close(); n100.close(); v4.close()


def test_seed_contract_and_threshold_boundaries() -> None:
    candidate_id = "candidate-x"
    expected = hashlib.sha256(
        f"{probe.SAMPLE_VERSION}\0{probe.SEED}\0D\0{candidate_id}".encode()
    ).hexdigest()
    assert probe._seed_rank("D", candidate_id) == expected
    assert probe._seed_rank("D", candidate_id) != probe._seed_rank("P1", candidate_id)

    passing = {"P1": ["same", "related", "insufficient"],
               "P2": ["same", "related", "insufficient", "same"],
               "P3": ["different", "same"],
               "D": ["same"] * 13 + ["related", "insufficient"]}
    result = probe._score_gate(passing)
    assert result["status"] == "PASS"
    assert {key: value["actual"] for key, value in result["predicates"].items()} == {
        "p1_non_same": 2, "p23_non_same": 3, "d_same": 13}
    for key, degraded in {
        "p1": {**passing, "P1": ["same", "same", "related"]},
        "p23": {**passing, "P2": ["same"] * 4, "P3": ["related", "same"]},
        "d": {**passing, "D": ["same"] * 12 + ["related"] * 3},
    }.items():
        assert probe._score_gate(degraded)["status"] == "FAIL", key


def test_pre_reveal_schema_and_payload_are_scrubbed() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(probe.PROBE_SCHEMA_SQL)
    _insert_run(connection, status="sample_frozen")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO sample_pairs VALUES(?,?,?,?,?,?,?)",
                           ("probe-run", 1, "sample", "P1", "real", "rank", "sha"))
    assert "criteria_declared" not in probe.PROBE_SCHEMA_SQL
    assert "source_adjudication_record_sha256 TEXT" not in probe.PROBE_SCHEMA_SQL
    connection.close()
    probe._assert_blind_payload({"opaque_probe_id": "adj_x", "reviews": []}, "test")
    for hidden in ("candidate_id", "group_code", "n100_gold_label", "proposal",
                   "numeric_score", "policy_flags", "seed_rank"):
        with pytest.raises(probe.IdentityInstrumentProbeError, match="leaks hidden field"):
            probe._assert_blind_payload({hidden: "secret"}, "test")


def test_step2_is_immutable_self_transition_and_prepare_is_gated(tmp_path: Path) -> None:
    artifact, manifest = _minimal_frozen_sidecar(tmp_path)
    declared = probe.declare_gate(repo_root=tmp_path, artifact_path=artifact,
                                  sample_manifest_path=manifest)
    repeated = probe.declare_gate(repo_root=tmp_path, artifact_path=artifact,
                                  sample_manifest_path=manifest)
    assert repeated == declared
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    run = connection.execute("SELECT * FROM runs").fetchone()
    event = connection.execute("SELECT * FROM state_events").fetchone()
    assert run["status"] == "sample_frozen" and run["gate_status"] == "declared"
    assert (event["from_status"], event["to_status"]) == ("sample_frozen", "sample_frozen")
    assert json.loads(event["detail_json"])["logical_stage"] == "criteria_declared"
    assert connection.execute("SELECT count(*) FROM gate_contract").fetchone()[0] == 1
    connection.close()


def test_ordered_quota_denial_is_durable(tmp_path: Path) -> None:
    artifact, manifest = _minimal_frozen_sidecar(tmp_path)
    probe.declare_gate(repo_root=tmp_path, artifact_path=artifact,
                       sample_manifest_path=manifest)
    with pytest.raises(probe.IdentityInstrumentProbeError, match="wave denied"):
        probe.record_quota_observation(
            repo_root=tmp_path, artifact_path=artifact, phase="pre_prepare",
            remaining_percent=30.0, source="test",
            observed_at=datetime.now(timezone.utc).isoformat(), limitation="synthetic")
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM quota_observations ORDER BY observation_no DESC LIMIT 1"
    ).fetchone()
    assert row["phase"] == "pre_prepare" and row["start_allowed"] == 0
    assert row["stop_required"] == 0
    connection.close()


def test_adjudication_and_reveal_are_forbidden_before_locks(tmp_path: Path) -> None:
    artifact, _ = _minimal_frozen_sidecar(tmp_path)
    with pytest.raises(probe.IdentityInstrumentProbeError, match="both reviewer locks"):
        probe.prepare_adjudication(repo_root=tmp_path, artifact_path=artifact,
                                   packet_path=Path("packet.json"),
                                   adjudication_form_path=Path("form.json"))
    with pytest.raises(probe.IdentityInstrumentProbeError, match="immutable adjudication lock"):
        probe.reveal_score(repo_root=tmp_path, artifact_path=artifact,
                           report_path=Path("report.json"),
                           manifest_path=Path("manifest.json"))


def test_adjudicator_actor_reuse_rolls_back(tmp_path: Path) -> None:
    artifact = tmp_path / "actor.db"; sheet = tmp_path / "sheet.html"
    blank = tmp_path / "blank.json"; completed = tmp_path / "completed.json"
    locked = tmp_path / "locked.json"
    sheet.write_text("evidence", encoding="utf-8"); blank.write_text("{}\n", encoding="utf-8")
    sheet_sha = wp25.sha256_file(sheet)
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.executescript(probe.PROBE_SCHEMA_SQL)
    _insert_run(connection, status="review_ready")
    for index, (lane, reviewer) in enumerate((("r1", "Alice"), ("r2", "Bob"))):
        connection.execute("INSERT INTO review_locks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           ("probe-run", lane, reviewer, 24, "completed", "0" * 64,
                            "locked", "1" * 64, "2" * 64, wp25.utc_now(), "3" * 64))
        if index == 0:
            connection.execute("UPDATE runs SET status='reviews_partial' WHERE run_id='probe-run'")
    connection.execute("UPDATE runs SET status='reviews_locked' WHERE run_id='probe-run'")
    connection.execute("INSERT INTO unblind_artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                       ("probe-run", "packet", "0" * 64, blank.as_posix(),
                        wp25.sha256_file(blank), "1" * 64, "2" * 64,
                        wp25.utc_now(), "3" * 64))
    connection.execute("UPDATE runs SET status='unblinded' WHERE run_id='probe-run'")
    contract_items = []
    form_items = []
    for index in range(1, 25):
        opaque, sample_id = f"adj_{index:02d}", f"probe_{index:02d}"
        connection.execute("INSERT INTO probe_adjudication_items VALUES(?,?,?,?,?,?,?,?,?)",
                           ("probe-run", index, opaque, sample_id, sheet.as_posix(), sheet_sha,
                            "same", "same", "4" * 64))
        frozen = {"display_order": index, "opaque_probe_id": opaque,
                  "sheet_path": sheet.as_posix(), "sheet_sha256": sheet_sha,
                  "r1_label": "same", "r2_label": "same"}
        contract_items.append(frozen)
        form_items.append({**frozen, "adjudicator_label": "same",
                           "unresolved_disagreement": False, "rationale": "reviewed"})
    contract = {"adjudication_version": probe.ADJUDICATION_VERSION,
                "run_id": "probe-run", "items": contract_items}
    payload = {**contract, "form_contract_sha256": canonical_sha256(contract),
               "adjudicator": " alice ", "adjudicated_at": wp25.utc_now(),
               "allowed_labels": sorted(probe.ALLOWED_LABELS), "items": form_items}
    completed.write_text(json.dumps(payload), encoding="utf-8")
    connection.commit(); connection.close()
    with pytest.raises(probe.IdentityInstrumentProbeError, match="distinct from both reviewers"):
        probe.lock_adjudication(repo_root=tmp_path, artifact_path=artifact,
                                completed_form_path=completed, locked_output_path=locked)
    connection = sqlite3.connect(artifact)
    assert connection.execute("SELECT count(*) FROM probe_adjudications").fetchone()[0] == 0
    assert not locked.exists()
    connection.close()


def _terminal_recovery_fixture(
        root: Path, *, legacy_manifest: bool,
        frozen_manifest_sha: str | None = None,
        ) -> tuple[Path, Path, Path, bytes, bytes]:
    artifact = root / "terminal.db"
    report = root / "out" / "report.json"
    manifest = root / "out" / "manifest.json"
    report.parent.mkdir(parents=True)
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.executescript(probe.PROBE_SCHEMA_SQL)
    _insert_run(connection, status="adjudication_locked")
    rank = 0
    for group, count in probe.GROUP_QUOTAS.items():
        for index in range(count):
            rank += 1
            sample_id = f"sample_{rank:02d}"
            payload = {
                "sample_id": sample_id, "candidate_id": f"candidate_{rank:02d}",
                "group": group, "n100_sample_id": f"n100_{rank:02d}",
                "n100_gold_label": "same", "final_probe_label": "same",
                "commitment_sha256": hashlib.sha256(sample_id.encode()).hexdigest(),
            }
            connection.execute(
                "INSERT INTO probe_reveals VALUES(?,?,?,?,?,?,?,?,?)",
                ("probe-run", payload["sample_id"], payload["candidate_id"], group,
                 payload["n100_sample_id"], payload["n100_gold_label"],
                 payload["final_probe_label"], payload["commitment_sha256"],
                 probe._record(payload)),
            )
    completed = "2026-08-27T00:00:00Z"
    connection.execute(
        "UPDATE runs SET status='fail',gate_status='fail',completed_at=? "
        "WHERE run_id='probe-run'", (completed,))
    probe._refresh(connection, "probe-run")
    report_payload = probe._report_payload(connection, "probe-run", "FAIL", completed)
    report_raw = wp25._json_bytes(report_payload)
    report_sha = hashlib.sha256(report_raw).hexdigest()
    relative_report = "out/report.json"
    manifest_payload = probe._manifest_payload(
        connection, "probe-run", "FAIL", completed, relative_report, report_sha,
        legacy_windows_report_path=legacy_manifest)
    manifest_raw = wp25._json_bytes(manifest_payload)
    manifest_sha = frozen_manifest_sha or hashlib.sha256(manifest_raw).hexdigest()
    terminal = {
        "status": "FAIL", "report_path": relative_report,
        "report_sha256": report_sha, "manifest_path": "out/manifest.json",
        "manifest_sha256": manifest_sha, "completed_at": completed,
    }
    connection.execute(
        "INSERT INTO probe_terminal VALUES(?,?,?,?,?,?,?,?)",
        ("probe-run", terminal["status"], terminal["report_path"], report_sha,
         terminal["manifest_path"], manifest_sha, completed, probe._record(terminal)),
    )
    connection.commit(); connection.close()
    report.write_bytes(report_raw); manifest.write_bytes(manifest_raw)
    return artifact, report, manifest, report_raw, manifest_raw


def test_future_manifest_uses_posix_report_path_and_recovers(tmp_path: Path) -> None:
    artifact, report, manifest, report_raw, manifest_raw = _terminal_recovery_fixture(
        tmp_path, legacy_manifest=False)
    payload = json.loads(manifest_raw)
    assert payload["report"]["path"] == "out/report.json"
    result = probe.reveal_score(
        repo_root=tmp_path, artifact_path=artifact,
        report_path=Path("out/report.json"), manifest_path=Path("out/manifest.json"))
    assert result.status == "FAIL"
    assert report.read_bytes() == report_raw and manifest.read_bytes() == manifest_raw


def test_legacy_windows_manifest_bytes_recover_from_terminal_sha(tmp_path: Path) -> None:
    artifact, report, manifest, report_raw, manifest_raw = _terminal_recovery_fixture(
        tmp_path, legacy_manifest=True)
    payload = json.loads(manifest_raw)
    assert payload["report"]["path"] == "out\\report.json"
    result = probe.reveal_score(
        repo_root=tmp_path, artifact_path=artifact,
        report_path=Path("out/report.json"), manifest_path=Path("out/manifest.json"))
    assert result.status == "FAIL"
    assert report.read_bytes() == report_raw and manifest.read_bytes() == manifest_raw


def test_terminal_recovery_rejects_unbound_normalized_and_legacy_bytes(
        tmp_path: Path) -> None:
    artifact, report, manifest, report_raw, manifest_raw = _terminal_recovery_fixture(
        tmp_path, legacy_manifest=True, frozen_manifest_sha="0" * 64)
    with pytest.raises(probe.IdentityInstrumentProbeError,
                       match="terminal recovery bytes changed"):
        probe.reveal_score(
            repo_root=tmp_path, artifact_path=artifact,
            report_path=Path("out/report.json"), manifest_path=Path("out/manifest.json"))
    assert report.read_bytes() == report_raw and manifest.read_bytes() == manifest_raw


def test_prepare_fetch_manifest_writer_corrects_only_target_payload(
        tmp_path: Path) -> None:
    target = tmp_path / "fetch.json"; other = tmp_path / "other.json"
    payload = {
        "status": "PASS", "run_id": "probe-run",
        "hard_caps": dict(probe.LEGACY_FETCH_HARD_CAPS),
        "fetch_assets": [],
    }
    original = json.loads(json.dumps(payload))
    writer = probe._fetch_manifest_atomic_writer(target)
    writer(target, payload, allow_identical=True)
    writer(target, payload, allow_identical=True)
    writer(other, payload)
    assert payload == original
    assert json.loads(target.read_text(encoding="utf-8"))["hard_caps"] == dict(
        probe.CORRECTED_FETCH_HARD_CAPS)
    assert other.read_bytes() == wp25._json_bytes(payload)
    bad = tmp_path / "bad.json"
    with pytest.raises(probe.IdentityInstrumentProbeError,
                       match="unexpected inherited"):
        probe._fetch_manifest_atomic_writer(bad)(
            bad, {**payload, "hard_caps": {"asset_occurrences": 97}})
    assert not bad.exists()


def _fetch_cap_correction_fixture(
        root: Path, *, corrected_original: bool = False,
        ) -> tuple[Path, Path, Path, Path, Path]:
    artifact = root / "terminal.db"
    fetch_manifest = root / "fetch.json"
    report = root / "report.json"
    manifest = root / "manifest.json"
    output = root / "correction.json"
    report.write_bytes(wp25._json_bytes({"status": "FAIL", "run_id": "probe-run"}))
    manifest.write_bytes(wp25._json_bytes({"status": "FAIL", "run_id": "probe-run"}))
    caps = (probe.CORRECTED_FETCH_HARD_CAPS if corrected_original
            else probe.LEGACY_FETCH_HARD_CAPS)
    fetch_payload = {
        "status": "PASS", "run_id": "probe-run",
        "asset_occurrence_count": 93, "unique_asset_count": 93,
        "logical_http_fetches": 17, "wire_http_requests": 17,
        "hard_caps": dict(caps), "fetch_assets": [],
    }
    fetch_manifest.write_bytes(wp25._json_bytes(fetch_payload))

    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.executescript(probe.PROBE_SCHEMA_SQL)
    _insert_run(connection, status="fetching")
    for index in range(93):
        asset_key = f"asset_{index:03d}"
        connection.execute(
            "INSERT INTO fetch_assets(run_id,asset_key,source,source_asset_id,"
            "status,request_count,record_sha256) VALUES(?,?,?,?,?,?,?)",
            ("probe-run", asset_key, "test", asset_key,
             "fetch_failed" if index < 17 else "cache_hit",
             1 if index < 17 else 0, "0" * 64),
        )
        if index < 17:
            connection.execute(
                "INSERT INTO fetch_attempts(run_id,asset_key,attempt_no,status,"
                "request_count,redirect_chain_json,started_at,completed_at,"
                "error_kind,record_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("probe-run", asset_key, 1, "failed", 1, "[]",
                 "2026-08-27T00:00:00Z", "2026-08-27T00:00:01Z",
                 "connection", "1" * 64),
            )
    fetch_binding = {
        "role": "fetch_manifest", "path": fetch_manifest.name,
        "size_bytes": fetch_manifest.stat().st_size,
        "sha256": wp25.sha256_file(fetch_manifest), "stage": "prepare",
    }
    connection.execute(
        "INSERT INTO artifact_bindings VALUES(?,?,?,?,?,?,?)",
        ("probe-run", fetch_binding["role"], fetch_binding["path"],
         fetch_binding["size_bytes"], fetch_binding["sha256"],
         fetch_binding["stage"], probe._record(fetch_binding)),
    )
    connection.execute(
        "UPDATE runs SET status='fail',asset_occurrence_count=93,unique_asset_count=93,"
        "logical_http_fetches=17,wire_http_requests=17,gate_status='fail',"
        "completed_at='2026-08-27T00:00:00Z' WHERE run_id='probe-run'")
    terminal = {
        "status": "FAIL", "report_path": report.name,
        "report_sha256": wp25.sha256_file(report),
        "manifest_path": manifest.name,
        "manifest_sha256": wp25.sha256_file(manifest),
        "completed_at": "2026-08-27T00:00:00Z",
    }
    connection.execute(
        "INSERT INTO probe_terminal VALUES(?,?,?,?,?,?,?,?)",
        ("probe-run", terminal["status"], terminal["report_path"],
         terminal["report_sha256"], terminal["manifest_path"],
         terminal["manifest_sha256"], terminal["completed_at"],
         probe._record(terminal)),
    )
    connection.commit(); connection.close()
    return artifact, fetch_manifest, report, manifest, output


def test_fetch_cap_correction_projection_and_idempotent_recovery(
        tmp_path: Path) -> None:
    artifact, fetch_manifest, report, manifest, output = (
        _fetch_cap_correction_fixture(tmp_path))
    producer = REPO_ROOT / "canonical/vnext_identity_instrument_probe.py"
    protected = {path: (path.stat().st_size, wp25.sha256_file(path)) for path in (
        artifact, fetch_manifest, report, manifest)}
    first = probe.create_fetch_cap_correction(
        repo_root=tmp_path, artifact_path=artifact,
        fetch_manifest_path=fetch_manifest, report_path=report,
        manifest_path=manifest, output_path=output,
        current_producer_path=producer)
    frozen = output.read_bytes()
    second = probe.create_fetch_cap_correction(
        repo_root=tmp_path, artifact_path=artifact,
        fetch_manifest_path=fetch_manifest, report_path=report,
        manifest_path=manifest, output_path=output,
        current_producer_path=producer)
    assert first == second and output.read_bytes() == frozen
    payload = json.loads(frozen)
    assert set(payload) == {
        "status", "correction_version", "run_id", "scope", "cause",
        "original_fetch_manifest", "corrected_contract",
        "corrected_projection", "terminal_bindings", "producer_bindings",
    }
    assert payload["corrected_contract"] == {
        "hard_caps": dict(probe.CORRECTED_FETCH_HARD_CAPS),
        "actual_counts": {
            "asset_occurrences": 93, "logical_http_fetches": 17,
            "wire_http_requests": 17, "redirects": 0, "retries": 0,
        },
        "within_hard_caps": True,
    }
    original = json.loads(fetch_manifest.read_text(encoding="utf-8"))
    original["hard_caps"] = dict(probe.CORRECTED_FETCH_HARD_CAPS)
    assert payload["corrected_projection"]["sha256"] == canonical_sha256(original)
    assert payload["producer_bindings"]["runtime"]["sha256"] == (
        probe.RUNTIME_PRODUCER_SPEC[1])
    assert payload["producer_bindings"]["current"]["sha256"] == (
        wp25.sha256_file(producer))
    assert protected == {path: (path.stat().st_size, wp25.sha256_file(path))
                         for path in protected}


def test_fetch_cap_correction_is_fail_closed_and_no_clobber(
        tmp_path: Path) -> None:
    producer = REPO_ROOT / "canonical/vnext_identity_instrument_probe.py"
    corrected_root = tmp_path / "corrected"; corrected_root.mkdir()
    artifact, fetch_manifest, report, manifest, output = (
        _fetch_cap_correction_fixture(corrected_root, corrected_original=True))
    with pytest.raises(probe.IdentityInstrumentProbeError,
                       match="already has the corrected"):
        probe.create_fetch_cap_correction(
            repo_root=corrected_root, artifact_path=artifact,
            fetch_manifest_path=fetch_manifest, report_path=report,
            manifest_path=manifest, output_path=output,
            current_producer_path=producer)
    assert not output.exists()

    legacy_root = tmp_path / "legacy"; legacy_root.mkdir()
    artifact, fetch_manifest, report, manifest, output = (
        _fetch_cap_correction_fixture(legacy_root))
    probe.create_fetch_cap_correction(
        repo_root=legacy_root, artifact_path=artifact,
        fetch_manifest_path=fetch_manifest, report_path=report,
        manifest_path=manifest, output_path=output,
        current_producer_path=producer)
    output.write_text("not the correction\n", encoding="utf-8")
    with pytest.raises(probe.ERROR_BASE, match="refusing to clobber"):
        probe.create_fetch_cap_correction(
            repo_root=legacy_root, artifact_path=artifact,
            fetch_manifest_path=fetch_manifest, report_path=report,
            manifest_path=manifest, output_path=output,
            current_producer_path=producer)
    assert output.read_text(encoding="utf-8") == "not the correction\n"
