from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from canonical.cross_source_image_selection_pipeline import (
    BuildConfig,
    build_cross_source_image_selection,
)
from canonical.cross_source_image_selection_sidecar import (
    FORBIDDEN_POLICY_TABLE_NAMES,
)
from canonical.cross_source_image_selection_validator import validate_e3_artifact
from tests.test_cross_source_image_selection_sources import (
    RUN_ID as E2_RUN_ID,
    _create_e2_fixture,
    _spec,
)
from tools.build_cross_source_image_selection_e3 import main as build_cli_main


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _add_six_candidate_building(path: Path) -> None:
    """Make b4 large enough to prove queue previews cap top-3 at three."""

    connection = sqlite3.connect(path)
    try:
        for ordinal in range(1, 6):
            asset_id = f"ordinary-extra-{ordinal}"
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
                    E2_RUN_ID,
                    "divisare",
                    asset_id,
                    "success",
                    f"https://canonical/{asset_id}",
                    f"https://fetch/{asset_id}",
                    None,
                    f"pixel-{asset_id}",
                    f"phash-{asset_id}",
                    1024,
                    768 - ordinal,
                    512,
                    384,
                    _digest(f"asset:{asset_id}"),
                    '{"quality_flags":[]}',
                ),
            )
            connection.execute(
                "INSERT INTO project_assets VALUES(?,?,?,?,?)",
                (E2_RUN_ID, "divisare", "p4", asset_id, ordinal),
            )
            connection.execute(
                "INSERT INTO building_assets VALUES(?,?,?,?,?,?)",
                (
                    E2_RUN_ID,
                    "divisare",
                    "b4",
                    asset_id,
                    '["gallery"]',
                    _digest(f"relation:divisare:b4:{asset_id}"),
                ),
            )
            connection.execute(
                "INSERT INTO phash_node_members VALUES(?,?,?,?)",
                (E2_RUN_ID, f"node-{asset_id}", "divisare", asset_id),
            )
        for source, asset_id in connection.execute(
            """
            SELECT source,source_asset_id FROM assets
            WHERE fingerprint_status='success'
            """
        ).fetchall():
            connection.execute(
                """
                UPDATE assets SET normalized_pixel_sha256=?,phash_hex=?
                WHERE source=? AND source_asset_id=?
                """,
                (
                    _digest(f"pixel:{source}:{asset_id}"),
                    _digest(f"phash:{source}:{asset_id}"),
                    source,
                    asset_id,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _fixture(path: Path) -> object:
    _create_e2_fixture(path)
    _add_six_candidate_building(path)
    return _spec(path)


def _config(e2_path: Path, output: Path, *, sample_seed: str = "pipeline-seed"):
    spec = _spec(e2_path)
    return BuildConfig(
        e2_path=e2_path,
        output_path=output,
        expected_e2_size=spec.expected_size,
        expected_e2_sha256=spec.expected_sha256,
        expected_e2_logical_sha256=spec.expected_logical_sha256,
        expected_e2_contract_version=spec.expected_contract_version,
        expected_e2_builder_version=spec.expected_builder_version,
        sample_size=6,
        sample_seed=sample_seed,
        shortlist_size=5,
        batch_size=2,
    )


def _open_immutable(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1",
        uri=True,
    )


def test_sample_build_is_valid_candidate_only_and_accounts_no_success(
    tmp_path: Path,
) -> None:
    e2_path = tmp_path / "e2.db"
    _fixture(e2_path)
    output = tmp_path / "e3.db"

    result = build_cross_source_image_selection(_config(e2_path, output))
    report = validate_e3_artifact(output)

    assert result.status == "complete"
    assert result.selected_buildings == 6
    assert report.passed, report.failed_check_names
    connection = _open_immutable(output)
    try:
        run = connection.execute(
            """
            SELECT network_requests,vision_requests,llm_requests,
                   authoritative,artifact_scope
            FROM selection_runs
            """
        ).fetchone()
        assert run == (0, 0, 0, 0, "candidate_only")

        no_success = "divisare:building:b0"
        assert connection.execute(
            "SELECT count(*) FROM selected_buildings WHERE selection_id=?",
            (no_success,),
        ).fetchone()[0] == 1
        for table in ("image_candidates", "policy_rankings", "shortlist_items"):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE selection_id=?",  # noqa: S608
                (no_success,),
            ).fetchone()[0] == 0

        queue_rows = connection.execute(
            """
            SELECT policy_id,json_extract(detail_json,'$.scenario'),
                   estimated_queue_items
            FROM queue_estimates
            ORDER BY policy_id,estimate_id
            """
        ).fetchall()
        assert len(queue_rows) == 12
        assert {row[1] for row in queue_rows} == {
            "top1_no_reuse",
            "top1_exact_reuse",
            "top3_no_reuse",
            "top3_exact_reuse",
        }
        for policy_id, scenario, estimated in queue_rows:
            if scenario != "top3_no_reuse":
                continue
            expected = connection.execute(
                """
                SELECT count(*) FROM shortlist_items
                WHERE policy_id=? AND shortlist_rank<=3
                """,
                (policy_id,),
            ).fetchone()[0]
            assert estimated == expected

        schema_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        assert not schema_names.intersection(FORBIDDEN_POLICY_TABLE_NAMES)
    finally:
        connection.close()


def test_logical_sha_is_path_independent_and_output_is_no_clobber(
    tmp_path: Path,
) -> None:
    e2_path = tmp_path / "e2.db"
    _fixture(e2_path)
    left_path = tmp_path / "left.db"
    right_path = tmp_path / "right.db"
    config = _config(e2_path, left_path, sample_seed="stable-seed")

    left = build_cross_source_image_selection(config)
    right = build_cross_source_image_selection(
        _config(e2_path, right_path, sample_seed="stable-seed")
    )

    assert left.logical_sha256 == right.logical_sha256
    assert left.run_id == right.run_id
    before = left_path.read_bytes()
    with pytest.raises(FileExistsError):
        build_cross_source_image_selection(config)
    assert left_path.read_bytes() == before


def test_cli_builds_and_reports_offline_non_authoritative_sample(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    e2_path = tmp_path / "e2.db"
    spec = _fixture(e2_path)
    output = tmp_path / "cli.db"

    exit_code = build_cli_main(
        [
            "--output",
            str(output),
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
            "--sample-size",
            "6",
            "--sample-seed",
            "cli-seed",
            "--shortlist-size",
            "5",
            "--batch-size",
            "2",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert payload["selected_buildings"] == 6
    assert payload["network_requests"] == 0
    assert payload["vision_requests"] == 0
    assert payload["llm_requests"] == 0
    assert payload["authoritative"] is False
    assert payload["representative_selection"] is False
    assert validate_e3_artifact(output).passed
