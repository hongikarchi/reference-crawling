from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import (
    SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
)
from canonical.cross_source_semantic_coverage_sources import (
    ArtifactSpec,
    build_semantic_coverage_manifest,
    sha256_file,
    write_semantic_coverage_manifest,
)
from canonical.cross_source_semantic_coverage_validator import (
    validate_semantic_coverage_manifest,
)
from tests.test_cross_source_semantic_coverage_sources import (
    BUILDINGS,
    E2_RUN,
    E3_RUN,
    _sha,
    create_semantic_fixture,
)
from tools import validate_cross_source_semantic_coverage as validator_cli


def _refreshed(spec: ArtifactSpec) -> ArtifactSpec:
    return replace(
        spec,
        expected_size=spec.path.stat().st_size,
        expected_sha256=sha256_file(spec.path),
    )


def _upgrade_fixture(
    tmp_path: Path,
) -> tuple[ArtifactSpec, ArtifactSpec]:
    """Add the independent E2 relations omitted by the compact source fixture."""

    e2_spec, e3_spec = create_semantic_fixture(tmp_path)
    e2 = sqlite3.connect(e2_spec.path)
    e2.executescript(
        """
        CREATE TABLE source_buildings(
          run_id TEXT,source TEXT,source_building_id TEXT,name TEXT,
          source_record_sha256 TEXT,
          PRIMARY KEY(run_id,source,source_building_id)
        );
        CREATE TABLE cross_source_building_candidates(
          run_id TEXT,left_source TEXT,left_source_building_id TEXT,
          right_source TEXT,right_source_building_id TEXT
        );
        CREATE TABLE source_project_buildings(
          run_id TEXT,source TEXT,source_project_id TEXT,source_building_id TEXT
        );
        CREATE INDEX idx_source_project_buildings_building
          ON source_project_buildings(run_id,source,source_building_id);
        CREATE TABLE project_assets(
          run_id TEXT,source TEXT,source_project_id TEXT,
          source_asset_id TEXT,first_ordinal INTEGER
        );
        CREATE INDEX idx_project_assets_asset
          ON project_assets(run_id,source,source_project_id,source_asset_id);
        CREATE TABLE exact_pixel_cluster_members(
          run_id TEXT,source TEXT,source_asset_id TEXT,cluster_id TEXT
        );
        CREATE TABLE phash_node_members(
          run_id TEXT,node_id TEXT,source TEXT,source_asset_id TEXT
        );
        """
    )
    for source, building, stratum, _qa, _p1, _p2, _cross, count in BUILDINGS:
        building_sha = _sha(f"building:{source}:{building}")
        e2.execute(
            "INSERT INTO source_buildings VALUES(?,?,?,?,?)",
            (E2_RUN, source, building, building, building_sha),
        )
        project = f"project:{source}:{building}"
        e2.execute(
            "INSERT INTO source_project_buildings VALUES(?,?,?,?)",
            (E2_RUN, source, project, building),
        )
        for index in range(count):
            asset = f"{building}-asset-{index}"
            roles = (
                '["cover","gallery"]'
                if index == 0 and stratum != "gallery_fallback"
                else '["gallery"]'
            )
            e2.execute(
                """
                UPDATE building_assets SET roles_json=?
                WHERE run_id=? AND source=? AND source_building_id=?
                  AND source_asset_id=?
                """,
                (roles, E2_RUN, source, building, asset),
            )
            e2.execute(
                "INSERT INTO project_assets VALUES(?,?,?,?,?)",
                (E2_RUN, source, project, asset, index),
            )
            phash = _sha(f"phash:{source}:{asset}")
            e2.execute(
                "INSERT INTO phash_node_members VALUES(?,?,?,?)",
                (E2_RUN, f"node:{phash}", source, asset),
            )
    e2.execute(
        "INSERT INTO cross_source_building_candidates VALUES(?,?,?,?,?)",
        (E2_RUN, "architizer", "a-cross", "divisare", "d-cross"),
    )
    e2.commit()
    e2.close()
    e2_spec = _refreshed(e2_spec)

    e3 = sqlite3.connect(e3_spec.path)
    e3.row_factory = sqlite3.Row
    for source, building, stratum, _qa, _p1, _p2, cross, count in BUILDINGS:
        selection_id = f"{source}:building:{building}"
        for index in range(count):
            asset = f"{building}-asset-{index}"
            roles = (
                '["cover","gallery"]'
                if index == 0 and stratum != "gallery_fallback"
                else '["gallery"]'
            )
            e3.execute(
                """
                UPDATE image_candidates
                SET roles_json=?,primary_role=?,role_rank=?,ordinal_is_derived=1
                WHERE run_id=? AND selection_id=? AND source_asset_id=?
                """,
                (
                    roles,
                    "cover" if roles.startswith('["cover"') else "gallery",
                    0 if roles.startswith('["cover"') else 1,
                    E3_RUN,
                    selection_id,
                    asset,
                ),
            )
        row = e3.execute(
            "SELECT detail_json FROM selected_buildings WHERE run_id=? AND selection_id=?",
            (E3_RUN, selection_id),
        ).fetchone()
        detail = json.loads(str(row[0]))
        detail["building_summary"]["successful_cover_count"] = (
            0 if stratum == "gallery_fallback" else 1
        )
        detail["building_summary"]["cross_source_candidate"] = cross
        e3.execute(
            "UPDATE selected_buildings SET detail_json=? WHERE run_id=? AND selection_id=?",
            (canonical_json(detail), E3_RUN, selection_id),
        )
    e3.execute(
        """
        UPDATE selection_runs
        SET e2_size_bytes=?,e2_byte_sha256=? WHERE run_id=?
        """,
        (e2_spec.expected_size, e2_spec.expected_sha256, E3_RUN),
    )
    e3.execute(
        """
        UPDATE selection_inputs
        SET size_bytes=?,sha256_before=?,sha256_after=? WHERE run_id=?
          AND input_role='e2_evidence'
        """,
        (
            e2_spec.expected_size,
            e2_spec.expected_sha256,
            e2_spec.expected_sha256,
            E3_RUN,
        ),
    )
    e3.commit()
    e3.close()
    return e2_spec, _refreshed(e3_spec)


def _manifest(
    tmp_path: Path,
) -> tuple[ArtifactSpec, ArtifactSpec, Path, dict[str, object]]:
    e2_spec, e3_spec = _upgrade_fixture(tmp_path)
    payload = build_semantic_coverage_manifest(
        e2_spec,
        e3_spec,
        seed="validator-fixture-seed",
        enforce_production_counts=False,
    )
    path = write_semantic_coverage_manifest(tmp_path / "manifest.json", payload)
    return e2_spec, e3_spec, path, payload


def _validate(
    path: Path,
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
):
    return validate_semantic_coverage_manifest(
        path,
        e2_spec=e2_spec,
        e3_spec=e3_spec,
        expected_sample_size=10,
        expected_sample_seed="validator-fixture-seed",
        expected_max_images_per_building=6,
    )


def _rewrite_manifest(path: Path, payload: dict[str, object]) -> None:
    body = dict(payload)
    body.pop("semantic_coverage_manifest_sha256", None)
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN, "manifest": body}
    )
    path.write_text(canonical_json(body) + "\n", encoding="utf-8", newline="\n")


def test_valid_manifest_passes_independent_replay_and_preserves_inputs(
    tmp_path: Path,
) -> None:
    e2_spec, e3_spec, path, _payload = _manifest(tmp_path)
    before = {
        "e2": (e2_spec.path.stat().st_size, sha256_file(e2_spec.path)),
        "e3": (e3_spec.path.stat().st_size, sha256_file(e3_spec.path)),
        "manifest": (path.stat().st_size, sha256_file(path)),
    }
    report = _validate(path, e2_spec, e3_spec)
    after = {
        "e2": (e2_spec.path.stat().st_size, sha256_file(e2_spec.path)),
        "e3": (e3_spec.path.stat().st_size, sha256_file(e3_spec.path)),
        "manifest": (path.stat().st_size, sha256_file(path)),
    }
    assert report.passed, report.failed_check_names
    assert report.selected_building_count == 10
    assert report.selected_image_count == 60
    assert before == after
    assert not Path(str(e2_spec.path) + "-wal").exists()
    assert not Path(str(e3_spec.path) + "-wal").exists()


def test_recomputed_self_hash_does_not_hide_selected_record_tamper(
    tmp_path: Path,
) -> None:
    e2_spec, e3_spec, path, payload = _manifest(tmp_path)
    selected = payload["selected_buildings"]
    assert isinstance(selected, list)
    first = selected[0]["selected_building"]["coverage_plan"]
    first["selected_occurrences"][0]["occurrence"]["candidate"][
        "canonical_url"
    ] = "https://tampered.invalid/image.jpg"
    _rewrite_manifest(path, payload)
    report = _validate(path, e2_spec, e3_spec)
    assert not report.passed
    assert "per_building_selection_and_e2_join_replay" in report.failed_check_names
    assert "manifest_self_sha256" in {
        value.name for value in report.checks if value.passed
    }


def test_reordered_buildings_with_fresh_self_hash_is_rejected(tmp_path: Path) -> None:
    e2_spec, e3_spec, path, payload = _manifest(tmp_path)
    selected = payload["selected_buildings"]
    assert isinstance(selected, list)
    selected[0], selected[1] = selected[1], selected[0]
    _rewrite_manifest(path, payload)
    report = _validate(path, e2_spec, e3_spec)
    assert not report.passed
    assert "guarded_n10_replay" in report.failed_check_names
    assert "entire_manifest_replay" in report.failed_check_names


def test_input_sidecar_is_rejected_without_writing(tmp_path: Path) -> None:
    e2_spec, e3_spec, path, _payload = _manifest(tmp_path)
    sidecar = Path(str(e3_spec.path) + "-wal")
    sidecar.write_bytes(b"fixture-sidecar")
    before = e3_spec.path.read_bytes()
    with pytest.raises(RuntimeError, match="sidecars"):
        _validate(path, e2_spec, e3_spec)
    assert e3_spec.path.read_bytes() == before


def test_validator_cli_accepts_fixture(tmp_path: Path, capsys) -> None:
    e2_spec, e3_spec, path, _payload = _manifest(tmp_path)
    code = validator_cli.main(
        [
            "--manifest",
            str(path),
            "--e2",
            str(e2_spec.path),
            "--e3",
            str(e3_spec.path),
            "--expected-e2-size",
            str(e2_spec.expected_size),
            "--expected-e2-sha256",
            e2_spec.expected_sha256,
            "--expected-e2-logical-sha256",
            e2_spec.expected_logical_sha256,
            "--expected-e2-run-id",
            e2_spec.expected_run_id,
            "--expected-e2-application-id",
            str(e2_spec.expected_application_id),
            "--expected-e3-size",
            str(e3_spec.expected_size),
            "--expected-e3-sha256",
            e3_spec.expected_sha256,
            "--expected-e3-logical-sha256",
            e3_spec.expected_logical_sha256,
            "--expected-e3-run-id",
            e3_spec.expected_run_id,
            "--expected-e3-application-id",
            str(e3_spec.expected_application_id),
            "--expected-sample-seed",
            "validator-fixture-seed",
        ]
    )
    parsed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert parsed["status"] == "pass"
    assert parsed["network_requests"] == parsed["vision_requests"] == 0


def test_validator_does_not_call_planner_selection_functions() -> None:
    source = Path(
        "canonical/cross_source_semantic_coverage_validator.py"
    ).read_text(encoding="utf-8")
    imports = source.split("@dataclass", 1)[0]
    assert "select_guarded_n10" not in imports
    assert "select_building_coverage" not in imports
    assert "build_semantic_coverage_manifest" not in imports
