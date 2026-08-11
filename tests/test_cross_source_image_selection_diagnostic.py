from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from canonical.cross_source_image_selection_diagnostic import (
    DIRECT_PHASH_LE8,
    EVIDENCE_KINDS,
    EXACT_PIXEL,
    IDENTICAL_PHASH,
    DiagnosticBuilding,
    build_diagnostic_sample_plan,
    collect_diagnostic_inventory,
    select_diagnostic_sample,
    write_diagnostic_manifest,
)
from canonical.cross_source_image_selection import canonical_json
from canonical.cross_source_image_selection_diagnostic_validator import (
    validate_diagnostic_manifest,
)
from canonical.cross_source_image_selection_sources import (
    BuildingSummary,
    open_e2_selection_sources,
)
from tests.test_cross_source_image_selection_sources import (
    RUN_ID,
    _create_e2_fixture,
    _digest,
    _spec,
)
from tools.plan_cross_source_image_selection_e3_diagnostic import main as cli_main
from tools.validate_cross_source_image_selection_e3_diagnostic import (
    main as validator_cli_main,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _diagnostic_building(
    source: str,
    building_id: str,
    kind: str,
) -> DiagnosticBuilding:
    counts = tuple((value, int(value == kind)) for value in EVIDENCE_KINDS)
    return DiagnosticBuilding(
        summary=BuildingSummary(
            source=source,
            source_building_id=building_id,
            name=f"{source} {building_id}",
            source_record_sha256=_hash(f"summary:{source}:{building_id}"),
            successful_asset_count=2,
            successful_cover_count=1,
            quality_risk_cover_count=0,
            cross_source_candidate=False,
            stratum="ordinary",
        ),
        candidate_count=2,
        intrinsic_pair_counts=counts,
        p2_suppression_counts=counts,
        candidate_evidence_manifest_sha256=_hash(
            f"candidates:{source}:{building_id}"
        ),
        direct_edge_manifest_sha256=_hash(f"edges:{source}:{building_id}"),
    )


def _add_evidence_buildings(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for source in ("architizer", "divisare"):
            prefix = "a" if source == "architizer" else "d"
            for kind in EVIDENCE_KINDS:
                building = f"{prefix}-{kind}"
                project = f"project-{building}"
                connection.execute(
                    "INSERT INTO source_buildings VALUES(?,?,?,?,?)",
                    (
                        RUN_ID,
                        source,
                        building,
                        f"Diagnostic {building}",
                        _digest(f"building:{building}"),
                    ),
                )
                connection.execute(
                    "INSERT INTO source_project_buildings VALUES(?,?,?,?)",
                    (RUN_ID, source, project, building),
                )
                node_ids: list[str] = []
                for ordinal in range(2):
                    asset = f"{building}-asset-{ordinal}"
                    if kind == EXACT_PIXEL:
                        pixel = _digest(f"pixel:{building}:same")
                        node = f"node-{building}-same"
                    elif kind == IDENTICAL_PHASH:
                        pixel = _digest(f"pixel:{building}:{ordinal}")
                        node = f"node-{building}-same"
                    else:
                        pixel = _digest(f"pixel:{building}:{ordinal}")
                        node = f"node-{building}-{ordinal}"
                    node_ids.append(node)
                    connection.execute(
                        """
                        INSERT INTO assets(
                          run_id,source,source_asset_id,fingerprint_status,
                          canonical_url,fetch_url,final_url,
                          normalized_pixel_sha256,phash_hex,
                          original_width,original_height,normalized_width,
                          normalized_height,source_record_sha256,provenance_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            RUN_ID,
                            source,
                            asset,
                            "success",
                            f"https://canonical/{asset}",
                            f"https://fetch/{asset}",
                            None,
                            pixel,
                            _digest(f"phash:{node}"),
                            1024,
                            768 - ordinal,
                            512,
                            384,
                            _digest(f"asset:{asset}"),
                            '{"quality_flags":[]}',
                        ),
                    )
                    connection.execute(
                        "INSERT INTO project_assets VALUES(?,?,?,?,?)",
                        (RUN_ID, source, project, asset, ordinal),
                    )
                    connection.execute(
                        "INSERT INTO building_assets VALUES(?,?,?,?,?,?)",
                        (
                            RUN_ID,
                            source,
                            building,
                            asset,
                            '["cover"]' if ordinal == 0 else '["gallery"]',
                            _digest(f"relation:{source}:{building}:{asset}"),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO phash_node_members VALUES(?,?,?,?)",
                        (RUN_ID, node, source, asset),
                    )
                    if kind == EXACT_PIXEL:
                        connection.execute(
                            "INSERT INTO exact_pixel_cluster_members VALUES(?,?,?,?)",
                            (RUN_ID, f"cluster-{building}", source, asset),
                        )
                if kind == DIRECT_PHASH_LE8:
                    edge_id = f"edge-{building}"
                    connection.execute(
                        "INSERT INTO phash_edges VALUES(?,?,?,?,?,?,?)",
                        (
                            RUN_ID,
                            edge_id,
                            node_ids[0],
                            node_ids[1],
                            6,
                            "global_le8",
                            _digest(edge_id),
                        ),
                    )
        connection.commit()
    finally:
        connection.close()


def test_n10_guarantees_every_available_source_evidence_cell() -> None:
    inventory = tuple(
        _diagnostic_building(source, f"{kind}-{index}", kind)
        for source in ("architizer", "divisare")
        for kind in EVIDENCE_KINDS
        for index in range(2)
    )

    plan = select_diagnostic_sample(
        reversed(inventory), sample_size=10, seed="fixed-diagnostic-seed"
    )
    replay = select_diagnostic_sample(
        inventory, sample_size=10, seed="fixed-diagnostic-seed"
    )

    assert [value.identity for value in plan.selected] == [
        value.identity for value in replay.selected
    ]
    assert plan.ordered_selection_manifest_sha256 == (
        replay.ordered_selection_manifest_sha256
    )
    selected_cells = {
        (value.source, kind)
        for value in plan.selected
        for kind in value.evidence_kinds
    }
    assert selected_cells == {
        (source, kind)
        for source in ("architizer", "divisare")
        for kind in EVIDENCE_KINDS
    }


def test_inventory_uses_actual_p2_suppression_reasons(tmp_path: Path) -> None:
    e2_path = tmp_path / "e2.db"
    _create_e2_fixture(e2_path)
    _add_evidence_buildings(e2_path)

    with open_e2_selection_sources(_spec(e2_path), batch_size=2) as source:
        inventory = collect_diagnostic_inventory(source)

    target = {
        (value.source, value.source_building_id): dict(
            value.p2_suppression_counts
        )
        for value in inventory
        if "-exact_pixel" in value.source_building_id
        or "-identical_phash_distinct_pixel" in value.source_building_id
        or "-direct_phash_le8" in value.source_building_id
    }
    assert len(target) == 6
    for (_source, building), counts in target.items():
        expected_kind = next(kind for kind in EVIDENCE_KINDS if kind in building)
        assert counts[expected_kind] == 1
        assert sum(counts.values()) == 1


def test_plan_manifest_is_no_clobber_and_binds_e2(tmp_path: Path) -> None:
    e2_path = tmp_path / "e2.db"
    _create_e2_fixture(e2_path)
    _add_evidence_buildings(e2_path)
    spec = _spec(e2_path)
    with open_e2_selection_sources(spec, batch_size=3) as source:
        plan = build_diagnostic_sample_plan(
            source, sample_size=6, seed="manifest-seed"
        )
        output = write_diagnostic_manifest(
            tmp_path / "diagnostic.json",
            plan,
            e2_path=e2_path,
            e2_size_bytes=source.lineage.artifact_size,
            e2_byte_sha256=source.lineage.artifact_sha256,
            e2_logical_sha256=source.lineage.stored_logical_sha256,
        )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["selection_mode"] == "diagnostic_sample"
    assert payload["network_requests"] == 0
    assert payload["vision_requests"] == 0
    assert payload["llm_requests"] == 0
    assert len(payload["diagnostic_manifest_sha256"]) == 64
    assert payload["e2_input"]["byte_sha256"] == spec.expected_sha256
    assert len(payload["selected"]) == 6
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_diagnostic_manifest(
            output,
            plan,
            e2_path=e2_path,
            e2_size_bytes=spec.expected_size,
            e2_byte_sha256=spec.expected_sha256,
            e2_logical_sha256=spec.expected_logical_sha256,
        )
    assert output.read_bytes() == before


def test_cli_supports_separate_diagnostic_seed_and_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_e2_fixture(e2_path)
    _add_evidence_buildings(e2_path)
    spec = _spec(e2_path)
    output = tmp_path / "diagnostic-n6.json"

    exit_code = cli_main(
        [
            "--output",
            str(output),
            "--sample-size",
            "6",
            "--diagnostic-seed",
            "cli-diagnostic-seed",
            "--e2",
            str(e2_path),
            "--expected-e2-size",
            str(spec.expected_size),
            "--expected-e2-sha256",
            spec.expected_sha256,
            "--expected-e2-logical-sha256",
            spec.expected_logical_sha256,
            "--expected-e2-contract-version",
            str(spec.expected_contract_version),
            "--expected-e2-builder-version",
            str(spec.expected_builder_version),
            "--batch-size",
            "2",
        ]
    )

    assert exit_code == 0
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["selection_mode"] == "diagnostic_sample"
    assert stdout["sample_size"] == 6
    assert stdout["network_requests"] == 0
    assert output.is_file()


def _built_manifest(tmp_path: Path) -> tuple[Path, Path, object]:
    e2_path = tmp_path / "e2.db"
    _create_e2_fixture(e2_path)
    _add_evidence_buildings(e2_path)
    spec = _spec(e2_path)
    with open_e2_selection_sources(spec, batch_size=2) as source:
        plan = build_diagnostic_sample_plan(
            source, sample_size=6, seed="validator-seed"
        )
        manifest = write_diagnostic_manifest(
            tmp_path / "diagnostic.json",
            plan,
            e2_path=e2_path,
            e2_size_bytes=source.lineage.artifact_size,
            e2_byte_sha256=source.lineage.artifact_sha256,
            e2_logical_sha256=source.lineage.stored_logical_sha256,
        )
    return e2_path, manifest, spec


def test_independent_validator_replays_complete_manifest(tmp_path: Path) -> None:
    e2_path, manifest, spec = _built_manifest(tmp_path)

    result = validate_diagnostic_manifest(
        manifest,
        e2_spec=spec,
        expected_sample_size=6,
        expected_sample_seed="validator-seed",
        batch_size=2,
    )

    assert result.passed, result.failed_check_names
    assert result.selected_count == 6
    assert result.inventory_count >= 6
    assert result.e2_byte_sha256 == spec.expected_sha256
    assert result.e2_logical_sha256 == spec.expected_logical_sha256
    assert result.diagnostic_manifest_sha256
    assert e2_path.is_file()


def test_independent_validator_detects_suppression_tamper(tmp_path: Path) -> None:
    _e2_path, manifest, spec = _built_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    counts = payload["selected"][0]["diagnostic_record"][
        "p2_suppression_counts"
    ]
    first_kind = next(iter(counts))
    counts[first_kind] += 1
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")

    result = validate_diagnostic_manifest(
        manifest,
        e2_spec=spec,
        expected_sample_size=6,
        expected_sample_seed="validator-seed",
        batch_size=2,
    )

    assert not result.passed
    assert "diagnostic_manifest_sha256" in result.failed_check_names
    assert "selected_order_and_records" in result.failed_check_names
    assert "entire_manifest_replay" in result.failed_check_names


def test_independent_validator_detects_noncanonical_json_bytes(
    tmp_path: Path,
) -> None:
    _e2_path, manifest, spec = _built_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    result = validate_diagnostic_manifest(
        manifest,
        e2_spec=spec,
        expected_sample_size=6,
        expected_sample_seed="validator-seed",
        batch_size=2,
    )

    assert not result.passed
    assert result.failed_check_names == ("canonical_json_bytes",)


def test_validator_cli_exit_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    e2_path, manifest, spec = _built_manifest(tmp_path)
    common = [
        "--manifest",
        str(manifest),
        "--expected-sample-size",
        "6",
        "--expected-diagnostic-seed",
        "validator-seed",
        "--e2",
        str(e2_path),
        "--expected-e2-size",
        str(spec.expected_size),
        "--expected-e2-sha256",
        spec.expected_sha256,
        "--expected-e2-logical-sha256",
        spec.expected_logical_sha256,
        "--expected-e2-contract-version",
        str(spec.expected_contract_version),
        "--expected-e2-builder-version",
        str(spec.expected_builder_version),
        "--batch-size",
        "2",
    ]
    assert validator_cli_main(common) == 0
    valid_payload = json.loads(capsys.readouterr().out)
    assert valid_payload["status"] == "pass"
    assert valid_payload["network_requests"] == 0

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["sample_seed"] = "tampered-seed"
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    assert validator_cli_main(common) == 1
    invalid_payload = json.loads(capsys.readouterr().out)
    assert invalid_payload["status"] == "fail"

    missing_args = list(common)
    missing_args[1] = str(tmp_path / "missing.json")
    assert validator_cli_main(missing_args) == 2
    error_payload = json.loads(capsys.readouterr().err)
    assert error_payload["status"] == "error"


@pytest.mark.parametrize("sample_size", [0, 1, 2])
def test_rejects_too_small_diagnostic_sample(sample_size: int) -> None:
    inventory = tuple(
        _diagnostic_building("divisare", f"b-{kind}", kind)
        for kind in EVIDENCE_KINDS
    )
    with pytest.raises((TypeError, ValueError), match="at least three"):
        select_diagnostic_sample(
            inventory, sample_size=sample_size, seed="small"
        )
