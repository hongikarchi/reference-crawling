from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from canonical.cross_source_image_selection import (
    ordered_sample_manifest_sha256,
)
from canonical.cross_source_image_selection_full_pipeline import (
    FULL_CONFIRMATION,
    FullBuildConfig,
    FullBuildResumeError,
    SimulatedFullBuildInterruption,
    build_full_cross_source_image_selection,
    preflight_full_cross_source_image_selection,
)
from canonical.cross_source_image_selection_pipeline import _sampling_item
from canonical.cross_source_image_selection_sidecar import (
    SidecarSchemaError,
    sqlite_sidecar_paths,
)
from canonical.cross_source_image_selection_sources import (
    E2SelectionSources,
    open_e2_selection_sources,
)
from canonical.cross_source_image_selection_validator import validate_e3_artifact
from tests.test_cross_source_image_selection_sources import (
    RUN_ID as E2_RUN_ID,
    _create_e2_fixture,
    _spec,
)
from tools.build_cross_source_image_selection_e3_full import main as full_cli_main


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _create_full_fixture(path: Path) -> None:
    _create_e2_fixture(path)
    connection = sqlite3.connect(path)
    try:
        # Put the fixture's A--B--C direct edge chain inside b4.  A--C is a
        # metadata-only distance-10 edge and must never be expanded here.
        connection.executemany(
            "INSERT INTO project_assets VALUES(?,?,?,?,?)",
            [
                (E2_RUN_ID, "divisare", "p4", "gallery", 1),
                (E2_RUN_ID, "divisare", "p4", "cross", 2),
            ],
        )
        connection.executemany(
            "INSERT INTO building_assets VALUES(?,?,?,?,?,?)",
            [
                (
                    E2_RUN_ID,
                    "divisare",
                    "b4",
                    "gallery",
                    '["gallery"]',
                    _digest("full:b4:gallery"),
                ),
                (
                    E2_RUN_ID,
                    "divisare",
                    "b4",
                    "cross",
                    '["gallery"]',
                    _digest("full:b4:cross"),
                ),
            ],
        )
        # The E2 source fixture uses readable labels; the E3 sidecar contract
        # correctly requires real SHA-256 values.
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


def _config(e2_path: Path, output: Path, **changes: object) -> FullBuildConfig:
    spec = _spec(e2_path)
    config = FullBuildConfig(
        e2_path=e2_path,
        output_path=output,
        expected_e2_size=spec.expected_size,
        expected_e2_sha256=spec.expected_sha256,
        expected_e2_logical_sha256=spec.expected_logical_sha256,
        expected_e2_contract_version=spec.expected_contract_version,
        expected_e2_builder_version=spec.expected_builder_version,
        shortlist_size=3,
        batch_size=2,
        checkpoint_buildings=2,
    )
    return replace(config, **changes)


def _open_immutable(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )


def test_full_build_is_independently_valid_and_includes_no_success(
    tmp_path: Path,
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    output = tmp_path / "e3-full.db"

    result = build_full_cross_source_image_selection(_config(e2_path, output))
    report = validate_e3_artifact(output)

    assert result.status == "complete"
    assert result.population_buildings == 6
    assert result.eligible_buildings == 5
    assert result.image_candidates == 8
    assert report.passed, report.failed_check_names
    assert sqlite_sidecar_paths(output) == ()
    connection = _open_immutable(output)
    try:
        run = connection.execute(
            """
            SELECT selection_mode,sample_size,sample_seed,status,
                   network_requests,vision_requests,llm_requests
            FROM selection_runs
            """
        ).fetchone()
        assert run == ("full", None, None, "complete", 0, 0, 0)
        assert connection.execute(
            "SELECT count(*) FROM selected_buildings"
        ).fetchone()[0] == 6
        assert connection.execute(
            """
            SELECT count(*) FROM selected_buildings
            WHERE selection_id='divisare:building:b0'
            """
        ).fetchone()[0] == 1
        for table in ("image_candidates", "policy_rankings", "shortlist_items"):
            assert connection.execute(
                f"SELECT count(*) FROM {table} "  # noqa: S608
                "WHERE selection_id='divisare:building:b0'"
            ).fetchone()[0] == 0
        checkpoints = connection.execute(
            """
            SELECT phase,completed_rows,phase_complete,cursor_json
            FROM build_checkpoints ORDER BY phase
            """
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in checkpoints] == [
            ("candidates", 8, 1),
            ("inventory", 6, 1),
            ("selection", 6, 1),
        ]
        candidate_cursor = json.loads(checkpoints[0][3])
        assert candidate_cursor["direct_edge_rows"] == 2
        assert connection.execute(
            "SELECT count(*) FROM queue_estimates"
        ).fetchone()[0] == 12
        direct_detail = connection.execute(
            """
            SELECT detail_json FROM policy_rankings
                WHERE policy_id='p2_quality_exact_direct_phash_shortlist'
              AND suppression_reason='suppressed_direct_phash_le8'
            """
        ).fetchone()
        assert direct_detail is not None
        evidence = json.loads(direct_detail[0])["direct_phash_edge"]
        assert evidence["distance"] == 6
        assert evidence["edge_id"] == "edge-2"
        assert evidence["edge_record_sha256"] == _digest("edge:2")
    finally:
        connection.close()


def test_streaming_full_manifest_is_byte_identical_to_core_contract(
    tmp_path: Path,
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    output = tmp_path / "e3-full.db"
    build_full_cross_source_image_selection(_config(e2_path, output))
    spec = _spec(e2_path)
    with open_e2_selection_sources(spec, batch_size=2) as source:
        items = tuple(_sampling_item(row) for row in source.iter_building_summaries())
    expected = ordered_sample_manifest_sha256(items)
    connection = _open_immutable(output)
    try:
        actual = connection.execute(
            "SELECT ordered_selection_manifest_sha256 FROM selection_runs"
        ).fetchone()[0]
    finally:
        connection.close()
    assert actual == expected


def test_candidate_phase_interruption_resumes_to_same_logical_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    clean_output = tmp_path / "clean.db"
    resumed_output = tmp_path / "resumed.db"
    clean = build_full_cross_source_image_selection(_config(e2_path, clean_output))

    with pytest.raises(SimulatedFullBuildInterruption):
        build_full_cross_source_image_selection(
            _config(e2_path, resumed_output, interrupt_after_commits=9)
        )
    interrupted = sqlite3.connect(resumed_output)
    try:
        assert interrupted.execute(
            "SELECT status FROM selection_runs"
        ).fetchone()[0] == "building"
        phase = interrupted.execute(
            """
            SELECT completed_rows,phase_complete FROM build_checkpoints
            WHERE phase='candidates'
            """
        ).fetchone()
        assert phase[0] > 0
        assert phase[1] == 0
    finally:
        interrupted.close()

    candidate_starts: list[tuple[str, str] | None] = []
    edge_starts: list[tuple[str, str] | None] = []
    original_candidates = E2SelectionSources.iter_all_candidates
    original_edges = E2SelectionSources.iter_same_building_direct_phash_edges

    def counted_candidates(self, *, start_after=None):
        candidate_starts.append(start_after)
        return original_candidates(self, start_after=start_after)

    def counted_edges(self, *, start_after=None):
        edge_starts.append(start_after)
        return original_edges(self, start_after=start_after)

    monkeypatch.setattr(
        E2SelectionSources, "iter_all_candidates", counted_candidates
    )
    monkeypatch.setattr(
        E2SelectionSources,
        "iter_same_building_direct_phash_edges",
        counted_edges,
    )
    resumed = build_full_cross_source_image_selection(
        _config(e2_path, resumed_output, resume=True)
    )
    assert resumed.resumed
    assert resumed.logical_sha256 == clean.logical_sha256
    assert candidate_starts[0] is not None
    assert edge_starts[0] == candidate_starts[0]
    assert validate_e3_artifact(resumed_output).passed


def test_inventory_resume_uses_summary_keyset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    output = tmp_path / "inventory-resume.db"
    with pytest.raises(SimulatedFullBuildInterruption):
        build_full_cross_source_image_selection(
            _config(e2_path, output, interrupt_after_commits=1)
        )

    starts: list[tuple[str, str] | None] = []
    original = E2SelectionSources.iter_building_summaries

    def counted(self, source=None, *, start_after=None):
        starts.append(start_after)
        return original(self, source, start_after=start_after)

    monkeypatch.setattr(E2SelectionSources, "iter_building_summaries", counted)
    result = build_full_cross_source_image_selection(
        _config(e2_path, output, resume=True)
    )
    assert result.resumed
    assert starts[0] is not None
    assert result.population_buildings == 6


def test_wrong_resume_is_rejected_and_terminal_artifact_is_no_clobber(
    tmp_path: Path,
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    interrupted_output = tmp_path / "interrupted.db"
    with pytest.raises(SimulatedFullBuildInterruption):
        build_full_cross_source_image_selection(
            _config(e2_path, interrupted_output, interrupt_after_commits=1)
        )
    with pytest.raises(FullBuildResumeError, match="lineage/config mismatch"):
        build_full_cross_source_image_selection(
            _config(
                e2_path,
                interrupted_output,
                resume=True,
                checkpoint_buildings=3,
            )
        )

    terminal = tmp_path / "terminal.db"
    build_full_cross_source_image_selection(_config(e2_path, terminal))
    with pytest.raises(FileExistsError, match="clobber"):
        build_full_cross_source_image_selection(_config(e2_path, terminal))
    with pytest.raises(SidecarSchemaError, match="terminal"):
        build_full_cross_source_image_selection(
            _config(e2_path, terminal, resume=True)
        )


def test_same_building_direct_edge_stream_is_candidate_scoped(
    tmp_path: Path,
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    with open_e2_selection_sources(_spec(e2_path), batch_size=1) as source:
        edges = tuple(source.iter_same_building_direct_phash_edges())
    assert [
        (
            edge.source,
            edge.source_building_id,
            edge.left_source_asset_id,
            edge.right_source_asset_id,
            edge.hamming_distance,
        )
        for edge in edges
    ] == [
        ("divisare", "b4", "cross", "ordinary", 6),
        ("divisare", "b4", "gallery", "cross", 4),
    ]


def test_source_keyset_iterators_start_strictly_after_building(
    tmp_path: Path,
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    with open_e2_selection_sources(_spec(e2_path), batch_size=1) as source:
        summaries = [
            (row.source, row.source_building_id)
            for row in source.iter_building_summaries(
                start_after=("divisare", "b2")
            )
        ]
        candidates = [
            (row.source, row.source_building_id, row.source_asset_id)
            for row in source.iter_all_candidates(
                start_after=("divisare", "b2")
            )
        ]
        edges = [
            (row.source, row.source_building_id)
            for row in source.iter_same_building_direct_phash_edges(
                start_after=("divisare", "b3")
            )
        ]
    assert summaries == [
        ("divisare", "b3"),
        ("divisare", "b4"),
    ]
    assert candidates == [
        ("divisare", "b3", "cross"),
        ("divisare", "b4", "cross"),
        ("divisare", "b4", "gallery"),
        ("divisare", "b4", "ordinary"),
    ]
    assert edges == [("divisare", "b4"), ("divisare", "b4")]


def test_preflight_and_unconfirmed_cli_create_no_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    output = tmp_path / "not-created.db"
    config = _config(e2_path, output)

    preflight = preflight_full_cross_source_image_selection(config)
    assert preflight.population_buildings == 6
    assert preflight.image_candidates == 8
    assert preflight.unique_success_assets == 6
    assert preflight.candidate_occurrence_minus_unique_asset_count == 2
    assert preflight.same_building_direct_edges == 2
    assert preflight.disk_free_bytes >= preflight.minimum_free_bytes
    assert preflight.minimum_disk_space_satisfied
    assert preflight.output_parent.exists()
    assert preflight.no_clobber_ready
    assert preflight.output_sqlite_sidecars == ()
    assert not output.exists()

    arguments = [
        "--e2",
        str(e2_path),
        "--output",
        str(output),
        "--expected-e2-size",
        str(config.expected_e2_size),
        "--expected-e2-sha256",
        config.expected_e2_sha256,
        "--expected-e2-logical-sha256",
        config.expected_e2_logical_sha256,
        "--expected-e2-contract-version",
        config.expected_e2_contract_version,
        "--expected-e2-builder-version",
        config.expected_e2_builder_version,
        "--batch-size",
        "2",
        "--checkpoint-buildings",
        "2",
    ]
    assert full_cli_main(arguments) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "preflight"
    assert payload["authorized"] is False
    assert payload["creates_output"] is False
    assert payload["minimum_disk_space_satisfied"] is True
    assert payload["no_clobber_ready"] is True
    assert not output.exists()

    with pytest.raises(SystemExit):
        full_cli_main(arguments + ["--execute-full"])
    assert not output.exists()
    assert FULL_CONFIRMATION == "RUN_E3_FULL_OFFLINE"


def test_full_cli_runs_directly_from_repo_root(tmp_path: Path) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    output = tmp_path / "not-created-direct.db"
    config = _config(e2_path, output)
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "tools" / "build_cross_source_image_selection_e3_full.py"),
            "--e2",
            str(e2_path),
            "--output",
            str(output),
            "--expected-e2-size",
            str(config.expected_e2_size),
            "--expected-e2-sha256",
            config.expected_e2_sha256,
            "--expected-e2-logical-sha256",
            config.expected_e2_logical_sha256,
            "--expected-e2-contract-version",
            config.expected_e2_contract_version,
            "--expected-e2-builder-version",
            config.expected_e2_builder_version,
            "--batch-size",
            "2",
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["action"] == "preflight"
    assert payload["creates_output"] is False
    assert payload["minimum_disk_space_satisfied"] is True
    assert payload["no_clobber_ready"] is True
    assert not output.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_full_build_rejects_orphan_sqlite_sidecar(
    tmp_path: Path, suffix: str
) -> None:
    e2_path = tmp_path / "e2.db"
    _create_full_fixture(e2_path)
    output = tmp_path / "orphan-output.db"
    orphan = Path(str(output) + suffix)
    orphan.write_bytes(b"orphan")

    preflight = preflight_full_cross_source_image_selection(
        _config(e2_path, output)
    )
    assert not preflight.no_clobber_ready
    assert preflight.output_sqlite_sidecars == (str(orphan.resolve()),)
    with pytest.raises(FileExistsError, match="orphan SQLite sidecars"):
        build_full_cross_source_image_selection(_config(e2_path, output))
    assert not output.exists()
    assert orphan.read_bytes() == b"orphan"
