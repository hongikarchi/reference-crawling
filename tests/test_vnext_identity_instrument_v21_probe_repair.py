from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from canonical import vnext_identity_instrument_v21_probe as v21
from canonical import vnext_identity_instrument_v21_probe_repair as repair


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(value: dict[str, object]) -> str:
    return repair.canonical_sha256(value)


def _projection_sources(root: Path) -> tuple[Path, Path]:
    repaired = root / "repaired.db"
    controls = root / "controls.db"
    sheet = root / "sheet.bin"
    cache = root / "cache.bin"
    correction = root / "correction.json"
    sheet.write_bytes(b"sheet")
    cache.write_bytes(b"cache")
    correction.write_text("{}\n", encoding="utf-8")
    connection = sqlite3.connect(repaired)
    connection.executescript(
        """
        CREATE TABLE runs(run_id TEXT,sample_version TEXT,seed TEXT,status TEXT,
          sample_logical_sha256 TEXT,created_at TEXT);
        CREATE TABLE candidate_snapshots(sample_id TEXT,snapshot_sha256 TEXT);
        CREATE TABLE control_commitments(sample_id TEXT,gold_commitment_sha256 TEXT);
        CREATE TABLE sample_pairs(sample_rank INTEGER,sample_id TEXT,stratum TEXT,
          seed_rank_sha256 TEXT,record_sha256 TEXT);
        CREATE TABLE review_items(lane TEXT,display_order INTEGER,opaque_review_id TEXT,
          sample_id TEXT,sheet_path TEXT,sheet_sha256 TEXT,sheet_bytes INTEGER,
          record_sha256 TEXT);
        CREATE TABLE sample_assets(sample_id TEXT,side TEXT,asset_order INTEGER,
          asset_key TEXT,source TEXT,source_building_id TEXT,source_asset_id TEXT,
          selection_reason TEXT,record_sha256 TEXT);
        CREATE TABLE fetch_assets(asset_key TEXT,source TEXT,source_asset_id TEXT,
          expected_url TEXT,expected_raw_sha256 TEXT,expected_pixel_sha256 TEXT,
          status TEXT,cache_role TEXT,cache_path TEXT,actual_final_url TEXT,
          actual_raw_sha256 TEXT,actual_pixel_sha256 TEXT,http_status INTEGER,
          response_bytes INTEGER,request_count INTEGER,error_kind TEXT,
          error_message TEXT,record_sha256 TEXT);
        CREATE TABLE fetch_attempts(asset_key TEXT,attempt_no INTEGER,status TEXT,
          request_count INTEGER,redirect_chain_json TEXT,started_at TEXT,
          completed_at TEXT,error_kind TEXT,record_sha256 TEXT);
        CREATE TABLE cache_pins(role TEXT,path TEXT,transform_version TEXT,
          access_mode TEXT,file_count_before INTEGER,total_bytes_before INTEGER,
          tree_sha256_before TEXT,file_count_after INTEGER,total_bytes_after INTEGER,
          tree_sha256_after TEXT,record_sha256 TEXT);
        """
    )
    connection.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?)",
        ("source", "sample", "seed", "review_ready", "logical", "2026-01-01Z"),
    )
    connection.execute("INSERT INTO candidate_snapshots VALUES('s1','snapshot')")
    connection.execute("INSERT INTO control_commitments VALUES('s1','commitment')")
    connection.execute("INSERT INTO sample_pairs VALUES(1,'s1','probe','rank','pair')")
    for lane in ("r1", "r2"):
        connection.execute(
            "INSERT INTO review_items VALUES(?,?,?,?,?,?,?,?)",
            (lane, 1, f"opaque-{lane}", "s1", str(sheet), _sha(sheet), 5, f"sheet-{lane}"),
        )
    for side, key in (("left", "a-left"), ("right", "a-right")):
        connection.execute(
            "INSERT INTO sample_assets VALUES(?,?,?,?,?,?,?,?,?)",
            ("s1", side, 1, key, "src", "b", key, "selected", f"asset-{key}"),
        )
        connection.execute(
            "INSERT INTO fetch_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                "src",
                key,
                "https://example.invalid/original",
                None,
                None,
                "cache_hit",
                "repair",
                str(cache),
                "https://example.invalid/final",
                "raw",
                "pixel",
                200,
                5,
                0,
                None,
                None,
                f"fetch-{key}",
            ),
        )
    connection.execute(
        "INSERT INTO cache_pins VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("repair", str(root), "none", "read_only", 2, 10, "tree", 2, 10, "tree", "cache"),
    )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(controls)
    connection.executescript(
        """
        CREATE TABLE runs(run_id TEXT);
        CREATE TABLE input_pins(role TEXT,path TEXT,size_bytes INTEGER,sha256 TEXT,usage TEXT);
        CREATE TABLE control_reveals(sample_id TEXT,candidate_id TEXT,group_code TEXT,
          n100_sample_id TEXT,n100_gold_label TEXT,v1_probe_label TEXT,
          v2_derived_label TEXT,derivation_rule_no INTEGER,commitment_sha256 TEXT,
          revealed_at TEXT,reveal_payload_sha256 TEXT);
        """
    )
    connection.execute("INSERT INTO runs VALUES('control')")
    connection.execute(
        "INSERT INTO input_pins VALUES(?,?,?,?,?)",
        ("wp26_fetch_correction", str(correction), correction.stat().st_size, _sha(correction), "pin"),
    )
    connection.execute(
        "INSERT INTO control_reveals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("s1", "candidate", "P1", "n100", "related", "same", "related", 4,
         "commitment", "2026-01-01Z", "reveal"),
    )
    connection.commit()
    connection.close()
    return repaired, controls


def test_frozen_contract_and_conservative_gate_math() -> None:
    files, semantics = repair.assert_frozen_contract(Path.cwd())
    assert len(files) == 17
    assert len(semantics) == 4
    assert all(row["matched"] for row in files + semantics)
    order = Path(repair.DEFAULT_REPAIR_ORDER)
    assert order.stat().st_size == repair.EXPECTED_REPAIR_ORDER_SIZE
    assert _sha(order) == repair.EXPECTED_REPAIR_ORDER_SHA256
    assert repair.conservative_threshold("d_image_same_object", 15) == 14
    assert repair.conservative_threshold("d_scope_same", 15) == 14
    assert repair.conservative_threshold("n100_image_non_same_object", 4) == 3
    assert repair.conservative_threshold("p1_non_same_label", 2) == 2


def test_proof_records_hash_the_persisted_integer_flag() -> None:
    files, semantics = repair.assert_frozen_contract(Path.cwd())
    connection = sqlite3.connect(":memory:")
    connection.executescript(repair.REPAIR_SCHEMA)
    for row in files:
        assert type(row["matched"]) is int
        payload = {"run_id": "run", **row}
        connection.execute(
            "INSERT INTO repair_file_proofs VALUES(?,?,?,?,?,?,?,?)",
            (
                "run",
                row["family"],
                row["role"],
                row["path"],
                row["expected_sha256"],
                row["actual_sha256"],
                row["matched"],
                _record(payload),
            ),
        )
    for row in semantics:
        assert type(row["matched"]) is int
        payload = {"run_id": "run", **row}
        connection.execute(
            "INSERT INTO repair_semantic_proofs VALUES(?,?,?,?,?,?)",
            (
                "run",
                row["name"],
                row["expected_sha256"],
                row["actual_sha256"],
                row["matched"],
                _record(payload),
            ),
        )
    connection.row_factory = sqlite3.Row
    for table in ("repair_file_proofs", "repair_semantic_proofs"):
        for persisted in connection.execute(f"SELECT * FROM {table}"):
            payload = dict(persisted)
            actual = payload.pop("record_sha256")
            assert actual == _record(payload)
    connection.close()


def test_projection_maps_cache_sha_in_the_compatibility_column(tmp_path: Path) -> None:
    repaired, controls = _projection_sources(tmp_path)
    output, manifest = tmp_path / "projection.db", tmp_path / "projection.json"
    contract = repair.build_source_projection(
        repo_root=tmp_path,
        repaired_source_db_path=repaired,
        control_source_db_path=controls,
        projection_path=output,
        manifest_path=manifest,
        enforce_production_contract=False,
    )
    connection = sqlite3.connect(output)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT cache_path,cache_file_sha256,actual_final_url FROM source_fetch_assets "
        "WHERE asset_key='a-left'"
    ).fetchone()
    group = connection.execute("SELECT group_code FROM control_reveals").fetchone()[0]
    connection.close()
    assert row["cache_file_sha256"] == _sha(tmp_path / "cache.bin")
    assert row["actual_final_url"] == "https://example.invalid/final"
    assert group == "P1"
    assert contract.sha256 == _sha(output)
    manifest_raw = manifest.read_bytes()
    assert json.loads(manifest_raw)["status"] == "PASS"
    manifest.unlink()
    recovered = repair.build_source_projection(
        repo_root=tmp_path,
        repaired_source_db_path=repaired,
        control_source_db_path=controls,
        projection_path=output,
        manifest_path=manifest,
        enforce_production_contract=False,
    )
    assert recovered.sha256 == contract.sha256
    assert manifest.read_bytes() == manifest_raw


def test_projection_rejects_source_output_collision(tmp_path: Path) -> None:
    repaired, controls = _projection_sources(tmp_path)
    with pytest.raises(repair.IdentityInstrumentV21RepairError, match="path collision"):
        repair.build_source_projection(
            repo_root=tmp_path,
            repaired_source_db_path=repaired,
            control_source_db_path=controls,
            projection_path=repaired,
            manifest_path=tmp_path / "manifest.json",
            enforce_production_contract=False,
        )


def _gate_fixture(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    artifact, projection = tmp_path / "artifact.db", tmp_path / "projection.db"
    ids = [f"sample-{index:02d}" for index in range(23)] + [repair.EXPECTED_EXCLUDED_SAMPLE]
    d_ids, p1_ids = ids[:15], ids[15:17] + [repair.EXPECTED_EXCLUDED_SAMPLE]
    p2_ids, p3_ids = ids[17:21], ids[21:23]
    groups = {**{x: "D" for x in d_ids}, **{x: "P1" for x in p1_ids},
              **{x: "P2" for x in p2_ids}, **{x: "P3" for x in p3_ids}}
    eligible = [ids[17], ids[18], ids[21], ids[22], repair.EXPECTED_EXCLUDED_SAMPLE]

    connection = sqlite3.connect(projection)
    connection.executescript(repair.PROJECTION_SCHEMA)
    for sample_id, group in sorted(groups.items()):
        connection.execute(
            "INSERT INTO control_reveals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("projection", sample_id, "candidate", group, "n100", "related", "same",
             "related", 4, "commitment", "2026-01-01Z", "reveal", "record"),
        )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(artifact)
    connection.executescript(
        """
        CREATE TABLE runs(run_id TEXT,status TEXT);
        CREATE TABLE gate_contract(run_id TEXT,gate_json TEXT,gate_sha256 TEXT,
          d_image_min INTEGER,d_image_n INTEGER,d_scope_min INTEGER,d_scope_n INTEGER,
          n100_image_non_same_min INTEGER,n100_image_non_same_n INTEGER,
          p1_min_non_same INTEGER,p1_n INTEGER,eligible_sample_ids_json TEXT,
          declared_at TEXT,record_sha256 TEXT);
        CREATE TABLE artifacts(role TEXT);
        CREATE TABLE review_observations(sample_id TEXT);
        CREATE TABLE adjudicated_observations(run_id TEXT,sample_id TEXT,image TEXT,
          scope TEXT,derived_label TEXT);
        CREATE TABLE control_reveals(run_id TEXT,sample_id TEXT,group_code TEXT);
        """
    )
    connection.executescript(repair.REPAIR_SCHEMA)
    connection.execute("INSERT INTO runs VALUES('run','criteria_frozen')")
    connection.execute(
        "INSERT INTO gate_contract VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run", "{}", repair.BASE_GATE_SHA256, 14, 15, 14, 15, 3, 5, 2, 3,
            json.dumps(eligible), "2026-01-01Z", "record",
        ),
    )
    exclusion = {
        "run_id": "run",
        "sample_id": repair.EXPECTED_EXCLUDED_SAMPLE,
        "reason": "no_assets",
        "excluded_from_gate_scoring": 1,
    }
    connection.execute(
        "INSERT INTO sample_exclusions VALUES(?,?,?,?,?)",
        (*exclusion.values(), _record(exclusion)),
    )
    connection.commit()
    connection.close()
    return artifact, projection, ids


def test_effective_gate_uses_reveal_groups_not_probe_stratum(tmp_path: Path) -> None:
    artifact, projection, _ = _gate_fixture(tmp_path)
    gate = repair.freeze_effective_gate(
        repo_root=tmp_path,
        artifact_path=artifact,
        projection_path=projection,
        enforce_production_contract=True,
    )
    actual = {
        row["predicate"]: (row["effective_denominator"], row["effective_threshold"])
        for row in gate["predicates"]
    }
    assert actual == {
        "d_image_same_object": (15, 14),
        "d_scope_same": (15, 14),
        "n100_image_non_same_object": (4, 3),
        "p1_non_same_label": (2, 2),
    }
    connection = sqlite3.connect(artifact)
    connection.row_factory = sqlite3.Row
    authority = dict(connection.execute("SELECT * FROM score_authority").fetchone())
    connection.close()
    authority_payload = dict(authority)
    authority_record = authority_payload.pop("record_sha256")
    assert authority_record == _record(authority_payload)
    assert authority["core_score_role"] == "counterfactual_unadjusted_raw"
    assert authority["core_decision_authority"] == 0
    assert authority["effective_score_role"] == "wp28r_authoritative"
    assert authority["effective_decision_authority"] == 1
    assert authority["effective_gate_sha256"] == gate["effective_gate_sha256"]


def test_effective_score_excludes_only_the_declared_missing_sample(tmp_path: Path) -> None:
    artifact, projection, ids = _gate_fixture(tmp_path)
    repair.freeze_effective_gate(
        repo_root=tmp_path,
        artifact_path=artifact,
        projection_path=projection,
        enforce_production_contract=True,
    )
    source = sqlite3.connect(projection)
    groups = dict(source.execute("SELECT sample_id,group_code FROM control_reveals"))
    source.close()
    eligible = {ids[17], ids[18], ids[21], ids[22]}
    connection = sqlite3.connect(artifact)
    for sample_id, group in groups.items():
        connection.execute(
            "INSERT INTO control_reveals VALUES(?,?,?)", ("run", sample_id, group)
        )
        image = "different_object" if sample_id in eligible or group == "P1" else "same_object"
        label = "related" if group == "P1" else "same"
        connection.execute(
            "INSERT INTO adjudicated_observations VALUES(?,?,?,?,?)",
            ("run", sample_id, image, "same", label),
        )
    connection.commit()
    connection.row_factory = sqlite3.Row
    score = repair._effective_score(connection, "run")
    connection.close()
    assert score["status"] == "pass"
    assert score["predicates"]["n100_image_non_same_object"]["denominator"] == 4
    assert score["predicates"]["p1_non_same_label"]["denominator"] == 2


def test_source_context_restores_all_frozen_globals(tmp_path: Path) -> None:
    values = {name: getattr(v21, name) for name in (
        "SOURCE_RUN_ID", "SOURCE_SAMPLE_VERSION", "SOURCE_SEED",
        "SOURCE_LOGICAL_SHA256", "SOURCE_PACKET_BINDING_SHA256",
        "SOURCE_VISUAL_INVENTORY_SHA256", "WP27_RUN_ID", "WP27_DB_SIZE",
        "WP27_DB_SHA256",
    )}
    contract = repair.ProjectionContract(
        path=tmp_path / "projection.db", run_id="projection", source_run_id="source",
        source_sample_version="sample", source_seed="seed", source_logical_sha256="logical",
        packet_binding_sha256="packet", visual_inventory_sha256="visual",
        size_bytes=1, sha256="database",
    )
    with repair._repaired_source_context(contract):
        assert v21.WP27_RUN_ID == "projection"
        assert v21.SOURCE_PACKET_BINDING_SHA256 == "packet"
    assert {name: getattr(v21, name) for name in values} == values


def test_terminal_schema_is_immutable() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(repair.REPAIR_SCHEMA)
    payload = {
        "run_id": "run",
        "status": "fail",
        "base_status": "fail",
        "effective_gate_sha256": "gate",
        "completed_at": "2026-01-01Z",
    }
    connection.execute(
        "INSERT INTO repair_terminal VALUES(?,?,?,?,?,?)",
        (*payload.values(), _record(payload)),
    )
    with pytest.raises(sqlite3.IntegrityError, match="repair terminal frozen"):
        connection.execute("UPDATE repair_terminal SET status='pass'")
    connection.close()


def test_initialize_is_recoverable_after_criteria_freeze(tmp_path: Path) -> None:
    observed_at = datetime.now(timezone.utc).isoformat()
    arguments = {
        "repo_root": Path.cwd(),
        "quota_remaining_percent": 79.0,
        "quota_observed_at": observed_at,
        "quota_source": "focused_recovery_test",
        "projection_path": tmp_path / "projection.db",
        "projection_manifest_path": tmp_path / "projection.json",
        "artifact_path": tmp_path / "artifact.db",
        "sample_manifest_path": tmp_path / "sample.json",
    }
    first = repair.initialize_repair(**arguments)
    second = repair.initialize_repair(**arguments)
    assert first.run_id == second.run_id
    assert second.status == "criteria_frozen"
    assert second.excluded_sample_ids == (repair.EXPECTED_EXCLUDED_SAMPLE,)
    connection = sqlite3.connect(second.artifact_path)
    assert connection.execute("SELECT count(*) FROM repair_contract").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM score_authority").fetchone()[0] == 1
    connection.close()
