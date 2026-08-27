from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from canonical.cross_source_image_selection import canonical_sha256
from canonical import vnext_identity_instrument_v2_probe as probe


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _source_db(root: Path) -> Path:
    path = root / "source.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runs(run_id TEXT, sample_version TEXT, seed TEXT,
          sample_logical_sha256 TEXT, sample_count INTEGER);
        CREATE TABLE sample_pairs(run_id TEXT, sample_id TEXT, sample_rank INTEGER,
          seed_rank_sha256 TEXT, record_sha256 TEXT);
        CREATE TABLE candidate_snapshots(run_id TEXT, sample_id TEXT, snapshot_sha256 TEXT);
        CREATE TABLE control_commitments(run_id TEXT, sample_id TEXT,
          gold_commitment_sha256 TEXT);
        CREATE TABLE review_items(run_id TEXT, lane TEXT, display_order INTEGER,
          opaque_review_id TEXT, sample_id TEXT, sheet_path TEXT, sheet_sha256 TEXT,
          sheet_bytes INTEGER, record_sha256 TEXT);
        CREATE TABLE sample_assets(run_id TEXT, sample_id TEXT, side TEXT,
          asset_order INTEGER, asset_key TEXT, source TEXT, source_building_id TEXT,
          source_asset_id TEXT, selection_reason TEXT, record_sha256 TEXT);
        CREATE TABLE fetch_assets(run_id TEXT, asset_key TEXT, source TEXT,
          source_asset_id TEXT, expected_url TEXT, expected_raw_sha256 TEXT,
          expected_pixel_sha256 TEXT, status TEXT, cache_role TEXT, cache_path TEXT,
          actual_final_url TEXT, actual_raw_sha256 TEXT, actual_pixel_sha256 TEXT,
          http_status INTEGER, response_bytes INTEGER, request_count INTEGER,
          error_kind TEXT, error_message TEXT, record_sha256 TEXT);
        CREATE TABLE fetch_attempts(run_id TEXT, asset_key TEXT, attempt_no INTEGER,
          status TEXT, request_count INTEGER, redirect_chain_json TEXT,
          started_at TEXT, completed_at TEXT, error_kind TEXT, record_sha256 TEXT);
        CREATE TABLE cache_pins(run_id TEXT, role TEXT, path TEXT,
          transform_version TEXT, access_mode TEXT, file_count_before INTEGER,
          total_bytes_before INTEGER, tree_sha256_before TEXT, file_count_after INTEGER,
          total_bytes_after INTEGER, tree_sha256_after TEXT, record_sha256 TEXT);
        CREATE TABLE probe_reveals(run_id TEXT, sample_id TEXT, candidate_id TEXT,
          group_code TEXT, n100_sample_id TEXT, n100_gold_label TEXT,
          final_probe_label TEXT, commitment_sha256 TEXT);
        """
    )
    connection.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?)",
        ("fixture-wp26", probe.SOURCE_SAMPLE_VERSION, probe.SOURCE_SEED,
         probe.SOURCE_LOGICAL_SHA256, 24),
    )
    source_record = "a" * 64
    for rank in range(1, 25):
        sample_id = f"sample_{rank:02d}"
        commitment = hashlib.sha256(f"control:{sample_id}".encode()).hexdigest()
        connection.execute(
            "INSERT INTO sample_pairs VALUES(?,?,?,?,?)",
            ("fixture-wp26", sample_id, rank,
             hashlib.sha256(f"seed:{rank}".encode()).hexdigest(), source_record),
        )
        connection.execute(
            "INSERT INTO candidate_snapshots VALUES(?,?,?)",
            ("fixture-wp26", sample_id, hashlib.sha256(f"snapshot:{rank}".encode()).hexdigest()),
        )
        connection.execute(
            "INSERT INTO control_commitments VALUES(?,?,?)",
            ("fixture-wp26", sample_id, commitment),
        )
        for lane in ("r1", "r2"):
            sheet = root / "sheets" / f"{lane}_{rank:02d}.png"
            sheet.parent.mkdir(parents=True, exist_ok=True)
            raw = b"fixture-png\0" + lane.encode() + bytes([rank])
            sheet.write_bytes(raw)
            connection.execute(
                "INSERT INTO review_items VALUES(?,?,?,?,?,?,?,?,?)",
                ("fixture-wp26", lane, rank, f"{lane}_opaque_{rank:02d}", sample_id,
                 sheet.relative_to(root).as_posix(), hashlib.sha256(raw).hexdigest(),
                 len(raw), source_record),
            )
        if rank <= 15:
            group, gold = "D", "same"
        elif rank in (16, 17):
            group, gold = "P1", "insufficient"
        elif rank == 18:
            group, gold = "P1", "related"
        elif rank in (19, 20, 21):
            group, gold = "P2", "insufficient"
        elif rank == 22:
            group, gold = "P2", "related"
        else:
            group, gold = "P3", "insufficient"
        connection.execute(
            "INSERT INTO probe_reveals VALUES(?,?,?,?,?,?,?,?)",
            ("fixture-wp26", sample_id, f"candidate_{rank:02d}", group,
             f"n100_{rank:02d}", gold, "same", commitment),
        )

    assets: list[str] = []
    serial = 0
    for rank in range(1, 25):
        per_sample = 4 if rank <= 21 else 3
        slots = (("left", 1), ("left", 2), ("right", 1), ("right", 2))[:per_sample]
        for side, order in slots:
            serial += 1
            asset_key = f"asset_{serial:03d}"
            assets.append(asset_key)
            connection.execute(
                "INSERT INTO sample_assets VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("fixture-wp26", f"sample_{rank:02d}", side, order, asset_key,
                 "fixture", f"building_{rank:02d}", asset_key, "frozen", source_record),
            )
    assert len(assets) == 93
    for index, asset_key in enumerate(assets, 1):
        status = "cache_hit" if index <= 76 else "failed"
        connection.execute(
            "INSERT INTO fetch_assets VALUES(" + ",".join("?" for _ in range(19)) + ")",
            ("fixture-wp26", asset_key, "fixture", asset_key, None, None, None,
             status, None, None, None, None, None, None, None,
             0 if status == "cache_hit" else 1,
             None if status == "cache_hit" else "fixture_failure", None, source_record),
        )
        if status == "failed":
            connection.execute(
                "INSERT INTO fetch_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("fixture-wp26", asset_key, 1, "failed", 1, "[]", _now(), _now(),
                 "fixture_failure", source_record),
            )
    connection.commit()
    connection.close()
    return path


def _inputs(root: Path) -> dict[str, Path]:
    paths = {
        "protocol_path": root / "protocol.md",
        "order_path": root / "order.md",
        "source_sample_manifest_path": root / "wp26_sample.json",
        "source_report_path": root / "wp26_report.json",
        "source_manifest_path": root / "wp26_manifest.json",
        "source_fetch_manifest_path": root / "wp26_fetch.json",
        "source_fetch_correction_path": root / "wp26_correction.json",
        "source_producer_path": root / "wp26_producer.py",
        "source_adjudication_locked_path": root / "wp26_adjudication_locked.json",
    }
    for name, path in paths.items():
        path.write_text(name + "\n", encoding="utf-8")
    paths["source_db_path"] = _source_db(root)
    return paths


def _observation(rank: int) -> dict[str, str]:
    if rank <= 15:
        image, scope = "same_object", "same"
    elif rank in (18, 22):
        image, scope = "same_object", "component_vs_umbrella"
    else:
        image, scope = "inconclusive", "unclear"
    return {
        "image": image, "name": "agree", "firm": "agree", "place": "agree",
        "year": "compatible", "scope": scope, "basis": f"visible evidence row {rank}",
    }


def _complete_review(blank: Path, completed: Path, reviewer: str) -> None:
    payload = json.loads(blank.read_text(encoding="utf-8"))
    payload["reviewer"] = reviewer
    payload["reviewed_at"] = _now()
    for rank, item in enumerate(payload["items"], 1):
        item.update(_observation(rank))
    _write_json(completed, payload)


def _complete_adjudication(blank: Path, completed: Path) -> None:
    payload = json.loads(blank.read_text(encoding="utf-8"))
    payload["adjudicator"] = "Reviewer Three"
    payload["adjudicated_at"] = _now()
    for rank, item in enumerate(payload["items"], 1):
        item.update(_observation(rank))
    _write_json(completed, payload)


def _through_criteria(root: Path) -> Path:
    inputs = _inputs(root)
    artifact = root / "wp27.db"
    probe.freeze_instrument(
        repo_root=root, output_path=artifact, quota_remaining_percent=80,
        quota_observed_at=_now(), quota_source="fixture", verify_fixed_pins=False,
        **inputs,
    )
    probe.freeze_sample(
        repo_root=root, artifact_path=artifact, sample_manifest_path=root / "sample.json",
        enforce_source_contract=False,
    )
    probe.declare_gate(repo_root=root, artifact_path=artifact)
    return artifact


def test_derivation_first_match_and_exact_observation_contract() -> None:
    base = {"name": "unclear", "firm": "unknown", "place": "unclear",
            "year": "unclear", "basis": "visible evidence"}
    assert probe.derive_label({**base, "image": "inconclusive", "scope": "unrelated"}) == (
        "insufficient", 1
    )
    assert probe.derive_label({**base, "image": "different_objects",
                               "scope": "component_vs_umbrella"}) == ("different", 2)
    assert probe.derive_label({**base, "image": "same_object",
                               "scope": "phase_or_extension"}) == ("related", 3)
    assert probe.derive_label({**base, "image": "same_object", "scope": "same"}) == (
        "same", 4
    )
    assert probe.derive_label({**base, "image": "same_object", "scope": "unclear"}) == (
        "insufficient", 5
    )
    with pytest.raises(probe.IdentityInstrumentV2Error):
        probe.validate_observation({**base, "image": "Same_Object", "scope": "same"})
    with pytest.raises(probe.IdentityInstrumentV2Error):
        probe.validate_observation({**base, "image": "same_object", "scope": "same",
                                    "basis": " untrimmed"})


@pytest.mark.parametrize("actor", [" Reviewer One", "Reviewer  One", "Reviewer One ", 7])
def test_actor_must_already_be_a_normalized_string(actor: object) -> None:
    with pytest.raises(probe.IdentityInstrumentV2Error):
        probe._actor(actor, "reviewer")


def test_prepare_review_rejects_aliased_outputs_before_db_mutation(tmp_path: Path) -> None:
    artifact = _through_criteria(tmp_path)
    probe.record_quota_observation(
        repo_root=tmp_path, artifact_path=artifact, phase="pre_r1_review",
        remaining_percent=80, source="fixture", observed_at=_now(),
    )
    shared = tmp_path / "same.json"
    with pytest.raises(probe.IdentityInstrumentV2Error, match="distinct"):
        probe.prepare_review(
            repo_root=tmp_path, artifact_path=artifact,
            r1_form_path=shared, r2_form_path=shared,
            fetch_manifest_path=tmp_path / "fetch.json",
            fetch_report_path=tmp_path / "fetch_report.json",
        )
    connection = sqlite3.connect(artifact)
    assert connection.execute("SELECT status FROM runs").fetchone()[0] == "criteria_frozen"
    assert connection.execute(
        "SELECT count(*) FROM artifacts WHERE role IN "
        "('r1_form','r2_form','fetch_manifest','fetch_report')"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM state_events").fetchone()[0] == 3
    connection.close()
    assert not shared.exists()


def test_full_workflow_blinding_gate_hashes_and_recovery(tmp_path: Path) -> None:
    artifact = _through_criteria(tmp_path)
    probe.record_quota_observation(
        repo_root=tmp_path, artifact_path=artifact, phase="pre_r1_review",
        remaining_percent=80, source="fixture", observed_at=_now(),
    )
    prepared = probe.prepare_review(
        repo_root=tmp_path, artifact_path=artifact,
        r1_form_path=tmp_path / "r1_form.json", r2_form_path=tmp_path / "r2_form.json",
        fetch_manifest_path=tmp_path / "fetch_manifest.json",
        fetch_report_path=tmp_path / "fetch_report.json",
    )
    r1_completed, r1_locked = tmp_path / "r1_completed.json", tmp_path / "r1_locked.json"
    _complete_review(prepared.form_paths[0], r1_completed, "Reviewer One")
    probe.lock_review(repo_root=tmp_path, artifact_path=artifact, lane="r1",
                      completed_form_path=r1_completed, locked_output_path=r1_locked)
    probe.record_quota_observation(
        repo_root=tmp_path, artifact_path=artifact, phase="pre_r2_review",
        remaining_percent=79, source="fixture", observed_at=_now(),
    )
    r2_completed, r2_locked = tmp_path / "r2_completed.json", tmp_path / "r2_locked.json"
    _complete_review(prepared.form_paths[1], r2_completed, "Reviewer Two")
    probe.lock_review(repo_root=tmp_path, artifact_path=artifact, lane="r2",
                      completed_form_path=r2_completed, locked_output_path=r2_locked)
    probe.record_quota_observation(
        repo_root=tmp_path, artifact_path=artifact, phase="pre_adjudication_review",
        remaining_percent=78, source="fixture", observed_at=_now(),
    )
    packet, adj_form = probe.prepare_adjudication(
        repo_root=tmp_path, artifact_path=artifact,
        packet_path=tmp_path / "adjudication_packet.json",
        adjudication_form_path=tmp_path / "adjudication_form.json",
    )
    adj_completed, adj_locked = tmp_path / "adjudication_completed.json", tmp_path / "adj_locked.json"
    _complete_adjudication(adj_form, adj_completed)
    probe.lock_adjudication(
        repo_root=tmp_path, artifact_path=artifact, completed_form_path=adj_completed,
        locked_output_path=adj_locked,
    )
    probe.record_quota_observation(
        repo_root=tmp_path, artifact_path=artifact, phase="pre_reveal_score",
        remaining_percent=77, source="fixture", observed_at=_now(),
    )
    result = probe.reveal_score(
        repo_root=tmp_path, artifact_path=artifact, report_path=tmp_path / "report.json",
        manifest_path=tmp_path / "manifest.json",
    )
    assert result.status.casefold() == "pass"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert len(report["three_way_comparison"]) == 24
    assert report["gate"]["counts"] == {
        "d_same": 15, "p1_non_same": 3,
        "gold_insufficient_to_insufficient": 7, "gold_related_to_related": 2,
    }
    forbidden = probe.FORBIDDEN_INPUT_FIELDS
    for actor_path in (*prepared.form_paths, r1_locked, r2_locked, packet, adj_form, adj_locked):
        actor_payload = json.loads(actor_path.read_text(encoding="utf-8"))
        assert actor_payload["instructions"] == probe._axis_instructions()
        serialized = json.dumps(actor_payload)
        assert not any(f'"{key}"' in serialized for key in forbidden)
    r1_form_payload = json.loads(prepared.form_paths[0].read_text(encoding="utf-8"))
    review_contract = {
        key: r1_form_payload[key] for key in (
            "run_id", "review_version", "lane", "instrument_contract_sha256", "instructions"
        )
    }
    review_contract["items"] = [
        {key: item[key] for key in (
            "display_order", "opaque_review_id", "sheet_path", "sheet_sha256"
        )}
        for item in r1_form_payload["items"]
    ]
    assert r1_form_payload["form_contract_sha256"] == canonical_sha256(review_contract)
    packet_payload = json.loads(packet.read_text(encoding="utf-8"))
    packet_contract = {key: value for key, value in packet_payload.items()
                       if key != "packet_contract_sha256"}
    assert packet_payload["packet_contract_sha256"] == canonical_sha256(packet_contract)
    assert json.loads(adj_locked.read_text(encoding="utf-8"))["packet_contract_sha256"] == (
        packet_payload["packet_contract_sha256"]
    )

    connection = sqlite3.connect(artifact)
    connection.row_factory = sqlite3.Row
    states = [row[0] for row in connection.execute(
        "SELECT to_status FROM state_events ORDER BY event_no"
    )]
    assert states == [*probe.STATE_SEQUENCE[:-2], "pass"]
    criteria = connection.execute(
        "SELECT detail_json,occurred_at FROM state_events WHERE to_status='criteria_frozen'"
    ).fetchone()
    assert json.loads(criteria["detail_json"])["pre_form_count"] == 0
    assert all(row[0] >= criteria["occurred_at"] for row in connection.execute(
        "SELECT reviewed_at FROM review_observations"
    ))
    for table_row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        table = table_row[0]
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        if "record_sha256" not in columns:
            continue
        for row in connection.execute(f'SELECT * FROM "{table}"'):
            payload = {key: row[key] for key in row.keys() if key != "record_sha256"}
            assert row["record_sha256"] == canonical_sha256(payload), table
    connection.close()

    # DB-first byte-identical recovery remains available after terminal state.
    expected = {
        "sample": (tmp_path / "sample.json").read_bytes(),
        "r1_form": prepared.form_paths[0].read_bytes(),
        "r1_completed": r1_completed.read_bytes(),
        "r1_locked": r1_locked.read_bytes(),
    }
    for path in (tmp_path / "sample.json", prepared.form_paths[0], r1_completed, r1_locked):
        path.unlink()
    probe.freeze_sample(repo_root=tmp_path, artifact_path=artifact,
                        sample_manifest_path=tmp_path / "sample.json", enforce_source_contract=False)
    probe.prepare_review(
        repo_root=tmp_path, artifact_path=artifact,
        r1_form_path=prepared.form_paths[0], r2_form_path=prepared.form_paths[1],
        fetch_manifest_path=tmp_path / "fetch_manifest.json",
        fetch_report_path=tmp_path / "fetch_report.json",
    )
    probe.lock_review(repo_root=tmp_path, artifact_path=artifact, lane="r1",
                      completed_form_path=r1_completed, locked_output_path=r1_locked)
    assert (tmp_path / "sample.json").read_bytes() == expected["sample"]
    assert prepared.form_paths[0].read_bytes() == expected["r1_form"]
    assert r1_completed.read_bytes() == expected["r1_completed"]
    assert r1_locked.read_bytes() == expected["r1_locked"]
    prepared.form_paths[0].write_bytes(b"{}\n")
    with pytest.raises(probe.IdentityInstrumentV2Error, match="clobber"):
        probe.prepare_review(
            repo_root=tmp_path, artifact_path=artifact,
            r1_form_path=prepared.form_paths[0], r2_form_path=prepared.form_paths[1],
            fetch_manifest_path=tmp_path / "fetch_manifest.json",
            fetch_report_path=tmp_path / "fetch_report.json",
        )


def test_quota_sticky_stop_and_criteria_before_forms(tmp_path: Path) -> None:
    artifact = _through_criteria(tmp_path)
    with pytest.raises(probe.IdentityInstrumentV2Error, match="sticky stop"):
        probe.record_quota_observation(
            repo_root=tmp_path, artifact_path=artifact, phase="pre_r1_review",
            remaining_percent=20, source="fixture", observed_at=_now(),
        )
    with pytest.raises(probe.IdentityInstrumentV2Error, match="sticky"):
        probe.prepare_review(
            repo_root=tmp_path, artifact_path=artifact,
            r1_form_path=tmp_path / "r1.json", r2_form_path=tmp_path / "r2.json",
            fetch_manifest_path=tmp_path / "fetch.json", fetch_report_path=tmp_path / "fetch_report.json",
        )
    connection = sqlite3.connect(artifact)
    assert connection.execute("SELECT count(*) FROM artifacts WHERE role IN ('r1_form','r2_form')").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM state_events").fetchone()[0] == 3
    connection.close()


def test_quota_denial_can_be_refreshed_in_the_same_phase(tmp_path: Path) -> None:
    artifact = _through_criteria(tmp_path)
    with pytest.raises(probe.IdentityInstrumentV2Error, match="wave denied"):
        probe.record_quota_observation(
            repo_root=tmp_path, artifact_path=artifact, phase="pre_r1_review",
            remaining_percent=30, source="fixture", observed_at=_now(),
        )
    refreshed = probe.record_quota_observation(
        repo_root=tmp_path, artifact_path=artifact, phase="pre_r1_review",
        remaining_percent=79, source="fresh fixture", observed_at=_now(),
    )
    assert refreshed["observation_no"] == 3
    assert refreshed["start_allowed"] == 1
    prepared = probe.prepare_review(
        repo_root=tmp_path, artifact_path=artifact,
        r1_form_path=tmp_path / "r1.json", r2_form_path=tmp_path / "r2.json",
        fetch_manifest_path=tmp_path / "fetch.json",
        fetch_report_path=tmp_path / "fetch_report.json",
    )
    assert all(path.is_file() for path in prepared.form_paths)
