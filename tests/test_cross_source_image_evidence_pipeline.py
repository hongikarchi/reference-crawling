from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

import canonical.cross_source_image_evidence_pipeline as evidence_pipeline
from canonical.cross_source_image_evidence_pipeline import (
    BuildConfig,
    InputSpec,
    build_cross_source_image_evidence,
    sha256_file,
    sqlite_sidecars,
)
from canonical.cross_source_image_evidence_sidecar import (
    initialize_sidecar,
    open_sidecar,
)
from test_cross_source_image_evidence_sources import (
    _create_architizer,
    _create_divisare,
    _create_e1,
)


def _finish_e1_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "ALTER TABLE fingerprint_runs ADD COLUMN selection_mode TEXT"
    )
    connection.execute("UPDATE fingerprint_runs SET selection_mode='full'")
    connection.commit()
    connection.close()


def _replace_divisare_images(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM source_image_occurrences")
    connection.execute("DELETE FROM image_assets")
    connection.executemany(
        "INSERT INTO image_assets VALUES (?)",
        [(f"asset-{suffix}",) for suffix in "abcde"],
    )
    connection.executemany(
        "INSERT INTO source_image_occurrences VALUES (?,?,?,?,?,?,?)",
        [
            (1, "cover", 0, "https://images.test/a.jpg", "parsed", None, "asset-a"),
            (1, "gallery", 1, "https://images.test/a.jpg", "parsed", None, "asset-a"),
            (1, "gallery", 2, "https://images.test/b.jpg", "parsed", None, "asset-b"),
            (2, "gallery", 0, "https://images.test/c.jpg", "parsed", None, "asset-c"),
            (2, "gallery", 1, "https://images.test/d.jpg", "parsed", None, "asset-d"),
            (2, "gallery", 2, "https://images.test/e.jpg", "parsed", None, "asset-e"),
        ],
    )
    connection.commit()
    connection.close()


def _replace_architizer_images_and_metadata(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        UPDATE source_projects
        SET name='Alpha House',normalized_name='alpha house',
            location_country_raw='Italy',location_city_raw='Rome',
            completion_year_raw=2021
        """
    )
    connection.execute(
        """
        UPDATE buildings
        SET preferred_name='Alpha House',normalized_name='alpha house',
            location_country='Italy',location_city='Rome',completion_year=2021
        """
    )
    connection.execute("DELETE FROM source_image_occurrences")
    connection.execute("DELETE FROM image_assets")
    connection.executemany(
        "INSERT INTO image_assets VALUES (?,?)",
        [
            (f"global-{index}", f"imgix:/{index}.jpg")
            for index in range(1, 6)
        ],
    )
    connection.executemany(
        "INSERT INTO source_image_occurrences VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("occ-10-cover", 10, "cover", 0, "https://imgix.test/1.jpg", "global-1", "parsed", None, "cover", "photo"),
            ("occ-10-gallery-1", 10, "gallery", 1, "https://imgix.test/1.jpg", "global-1", "parsed", None, "gallery", "photo"),
            ("occ-10-gallery-2", 10, "gallery", 2, "https://imgix.test/2.jpg", "global-2", "parsed", None, "gallery", "photo"),
            ("occ-11-gallery-1", 11, "gallery", 0, "https://imgix.test/3.jpg", "global-3", "parsed", None, "gallery", "photo"),
            ("occ-11-gallery-2", 11, "gallery", 1, "https://imgix.test/4.jpg", "global-4", "parsed", None, "gallery", "photo"),
            ("occ-11-gallery-3", 11, "gallery", 2, "https://imgix.test/5.jpg", "global-5", "parsed", None, "gallery", "photo"),
        ],
    )
    connection.commit()
    connection.close()


def _add_multi_project_building_exact_reuse(
    divisare_path: Path,
    architizer_path: Path,
) -> None:
    divisare = sqlite3.connect(divisare_path)
    divisare.execute(
        "INSERT INTO buildings VALUES (?,?,?,?,?,?,?,?)",
        (
            "div-b2",
            2,
            "Alpha Annex",
            "alpha annex",
            "Italy",
            "Rome",
            2021,
            "resolved",
        ),
    )
    divisare.execute(
        "INSERT INTO active_building_membership_v2_3 VALUES (?,?,?,?,?)",
        (2, "div-b2", "primary", 1.0, "fixture_exact_reuse"),
    )
    divisare.execute(
        "INSERT INTO source_image_occurrences VALUES (?,?,?,?,?,?,?)",
        (
            2,
            "gallery",
            3,
            "https://images.test/a-reused.jpg",
            "parsed",
            None,
            "asset-a",
        ),
    )
    divisare.commit()
    divisare.close()

    architizer = sqlite3.connect(architizer_path)
    architizer.execute(
        "INSERT INTO buildings VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "arch-b2",
            11,
            "Alpha Annex",
            "alpha annex",
            "Italy",
            "Rome",
            2021,
            "firm-b",
            "resolved",
        ),
    )
    architizer.execute(
        "INSERT INTO building_projects VALUES (?,?,?,?,?)",
        ("arch-b2", 11, 1, "accepted", "fixture_exact_reuse"),
    )
    architizer.execute(
        "INSERT INTO source_image_occurrences VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "occ-11-exact-reuse",
            11,
            "gallery",
            3,
            "https://imgix.test/one-reused.jpg",
            "global-1",
            "parsed",
            None,
            "gallery",
            "photo",
        ),
    )
    architizer.commit()
    architizer.close()


def _input_spec(role: str, source: str, path: Path) -> InputSpec:
    return InputSpec(
        role=role,
        source=source,
        path=path,
        expected_size=path.stat().st_size,
        expected_sha256=sha256_file(path),
    )


def _sql_from_function(function: object, marker: str) -> str:
    source = inspect.getsource(function)
    marker_at = source.index(marker)
    sql_start = source.rfind('"""', 0, marker_at) + 3
    sql_end = source.index('"""', marker_at)
    return source[sql_start:sql_end]


def _global_evidence_query_plan(marker: str) -> list[str]:
    query = _sql_from_function(
        evidence_pipeline._materialize_global_image_evidence,
        marker,
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE assets (
              run_id TEXT NOT NULL,
              source TEXT NOT NULL,
              source_asset_id TEXT NOT NULL,
              provenance_json TEXT NOT NULL,
              PRIMARY KEY(run_id,source,source_asset_id)
            );
            CREATE TABLE exact_pixel_cluster_members (
              run_id TEXT NOT NULL,
              cluster_id TEXT NOT NULL,
              source TEXT NOT NULL,
              source_asset_id TEXT NOT NULL,
              PRIMARY KEY(run_id,cluster_id,source,source_asset_id),
              UNIQUE(run_id,source,source_asset_id)
            );
            CREATE INDEX idx_exact_members_asset
              ON exact_pixel_cluster_members(run_id,source,source_asset_id);
            CREATE TABLE phash_node_members (
              run_id TEXT NOT NULL,
              node_id TEXT NOT NULL,
              source TEXT NOT NULL,
              source_asset_id TEXT NOT NULL,
              PRIMARY KEY(run_id,node_id,source,source_asset_id),
              UNIQUE(run_id,source,source_asset_id)
            );
            CREATE INDEX idx_phash_members_asset
              ON phash_node_members(run_id,source,source_asset_id);
            CREATE TABLE phash_edges (
              run_id TEXT NOT NULL,
              edge_id TEXT NOT NULL,
              left_node_id TEXT NOT NULL,
              right_node_id TEXT NOT NULL,
              hamming_distance INTEGER NOT NULL,
              edge_scope TEXT NOT NULL,
              PRIMARY KEY(run_id,edge_id)
            );
            CREATE INDEX idx_phash_edges_distance
              ON phash_edges(
                run_id,hamming_distance,left_node_id,right_node_id
              );
            CREATE TABLE building_assets (
              run_id TEXT NOT NULL,
              source TEXT NOT NULL,
              source_building_id TEXT NOT NULL,
              source_asset_id TEXT NOT NULL,
              PRIMARY KEY(
                run_id,source,source_building_id,source_asset_id
              )
            );
            CREATE INDEX idx_building_assets_asset
              ON building_assets(run_id,source,source_asset_id);
            """
        )
        return [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + query,
                ("representative-run",),
            )
        ]
    finally:
        connection.close()


def _build_fixture_inputs(
    tmp_path: Path,
    *,
    multi_relation_exact_reuse: bool = False,
) -> tuple[InputSpec, ...]:
    divisare = tmp_path / "divisare.db"
    architizer = tmp_path / "architizer.db"
    divisare_e1 = tmp_path / "divisare-e1.db"
    architizer_e1 = tmp_path / "architizer-e1.db"

    _create_divisare(divisare)
    _replace_divisare_images(divisare)
    _create_architizer(architizer)
    _replace_architizer_images_and_metadata(architizer)
    if multi_relation_exact_reuse:
        _add_multi_project_building_exact_reuse(divisare, architizer)

    _create_e1(
        divisare_e1,
        source="divisare",
        eligible=[
            {"source_asset_id": "asset-a", "status": "success", "pixel_sha": "a" * 64, "phash": "0" * 64},
            {"source_asset_id": "asset-b", "status": "success", "pixel_sha": "b" * 64, "phash": "f" * 64},
            {"source_asset_id": "asset-c", "status": "success", "pixel_sha": "d" * 64, "phash": "0" * 63 + "1"},
            {"source_asset_id": "asset-d", "status": "failed", "error_kind": "http_404"},
        ],
        excluded=[
            {
                "source_asset_id": "asset-e",
                "source_asset_key": "asset-e",
                "reason_code": "unsupported_source_url",
            }
        ],
    )
    _finish_e1_fixture(divisare_e1)
    _create_e1(
        architizer_e1,
        source="architizer",
        eligible=[
            {"source_asset_id": "global-1", "status": "success", "pixel_sha": "a" * 64, "phash": "0" * 64},
            {"source_asset_id": "global-2", "status": "success", "pixel_sha": "c" * 64, "phash": "f" * 64},
            {"source_asset_id": "global-3", "status": "success", "pixel_sha": "e" * 64, "phash": "0" * 63 + "3"},
            {"source_asset_id": "global-4", "status": "failed", "error_kind": "empty_response"},
        ],
        excluded=[
            {
                "source_asset_id": "global-5",
                "source_asset_key": "imgix:/5.jpg",
                "reason_code": "placeholder_candidate",
            }
        ],
    )
    _finish_e1_fixture(architizer_e1)

    return (
        _input_spec("divisare_curated", "divisare", divisare),
        _input_spec("architizer_curated", "architizer", architizer),
        _input_spec("divisare_e1", "divisare", divisare_e1),
        _input_spec("architizer_e1", "architizer", architizer_e1),
    )


def test_full_build_is_offline_complete_no_clobber_evidence_ledger(
    tmp_path: Path,
) -> None:
    inputs = _build_fixture_inputs(tmp_path)
    before = {item.role: sha256_file(item.path) for item in inputs}
    output = tmp_path / "e2.db"
    config = BuildConfig(
        output_path=output,
        inputs=inputs,
        sample_size=None,
        batch_size=2,
    )

    result = build_cross_source_image_evidence(config)

    assert result.status == "complete"
    assert result.logical_sha256 is not None
    assert len(result.logical_sha256) == 64
    assert {item.role: sha256_file(item.path) for item in inputs} == before
    assert not any(sqlite_sidecars(item.path) for item in inputs)
    assert sqlite_sidecars(output) == ()

    connection = open_sidecar(output, readonly=True, immutable=True)
    try:
        run = connection.execute(
            "SELECT status,selection_mode FROM e2_runs"
        ).fetchone()
        assert tuple(run) == ("complete", "full")
        assert connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 10
        assert connection.execute(
            "SELECT count(*) FROM assets WHERE fingerprint_status='success'"
        ).fetchone()[0] == 6
        assert connection.execute(
            "SELECT count(*) FROM assets WHERE fingerprint_status='failed'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM assets WHERE fingerprint_status='excluded'"
        ).fetchone()[0] == 2

        assert connection.execute(
            "SELECT count(*) FROM exact_pixel_clusters WHERE is_cross_source=1"
        ).fetchone()[0] >= 1
        assert connection.execute(
            """SELECT count(*) FROM exact_pixel_clusters
               WHERE project_count>0 AND building_count>0"""
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT count(*) FROM candidate_image_evidence WHERE evidence_kind='exact_pixel'"
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT count(*) FROM candidate_image_evidence WHERE evidence_kind='identical_phash'"
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT count(*) FROM phash_edges WHERE edge_scope='global_le8' AND hamming_distance BETWEEN 1 AND 8"
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT count(*) FROM metadata_building_pairs WHERE normalized_name_equal=1 AND country_equal=1 AND locality_equal=1 AND year_overlap=1"
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT count(*) FROM cross_source_building_candidates"
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT count(*) FROM cross_source_project_image_evidence"
        ).fetchone()[0] >= 1

        assert connection.execute(
            "SELECT count(*) FROM source_project_buildings"
        ).fetchone()[0] == 4
        assert connection.execute("SELECT count(*) FROM project_assets").fetchone()[0] == 10
        assert connection.execute("SELECT count(*) FROM building_assets").fetchone()[0] == 10
        assert connection.execute(
            "SELECT occurrence_count FROM project_assets WHERE source='divisare' AND source_asset_id='asset-a'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT occurrence_count FROM project_assets WHERE source='architizer' AND source_asset_id='global-1'"
        ).fetchone()[0] == 2

        forbidden = {
            "representatives",
            "representative_images",
            "vision_queue",
        }
        observed = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type IN ('table','view')"
            )
        }
        assert forbidden.isdisjoint(observed)
        assert connection.execute(
            "SELECT count(*) FROM e2_validations WHERE severity='error' AND passed=0"
        ).fetchone()[0] == 0
    finally:
        connection.close()

    with pytest.raises(FileExistsError):
        build_cross_source_image_evidence(config)
    assert {item.role: sha256_file(item.path) for item in inputs} == before
    assert sqlite_sidecars(output) == ()


def test_exact_cluster_counts_reused_asset_across_projects_and_buildings(
    tmp_path: Path,
) -> None:
    inputs = _build_fixture_inputs(tmp_path, multi_relation_exact_reuse=True)
    output = tmp_path / "e2-exact-reuse.db"

    result = build_cross_source_image_evidence(
        BuildConfig(
            output_path=output,
            inputs=inputs,
            sample_size=None,
            batch_size=2,
        )
    )

    assert result.status == "complete"
    connection = open_sidecar(output, readonly=True, immutable=True)
    try:
        cluster = connection.execute(
            """
            SELECT member_count,source_count,project_count,building_count
            FROM exact_pixel_clusters
            WHERE normalized_pixel_sha256=?
            """,
            ("a" * 64,),
        ).fetchone()
        assert tuple(cluster) == (2, 2, 4, 4)
        validation = connection.execute(
            """
            SELECT passed,actual,detail_json FROM e2_validations
            WHERE validation_name='exact_cluster_relation_counts'
            """
        ).fetchone()
        assert int(validation[0]) == 1
        assert validation[1] == "0"
        assert '"clusters_checked":1' in str(validation[2])
    finally:
        connection.close()


def test_exact_cluster_relation_count_mismatch_fails_build_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _build_fixture_inputs(tmp_path)
    output = tmp_path / "e2-exact-corrupt.db"
    original = evidence_pipeline._materialize_exact_clusters

    def materialize_with_corrupt_count(
        connection: sqlite3.Connection,
        run_id: str,
        batch_size: int,
    ) -> tuple[int, int]:
        result = original(connection, run_id, batch_size)
        connection.execute(
            """
            UPDATE exact_pixel_clusters
            SET project_count=project_count+1
            WHERE run_id=?
            """,
            (run_id,),
        )
        connection.commit()
        return result

    monkeypatch.setattr(
        evidence_pipeline,
        "_materialize_exact_clusters",
        materialize_with_corrupt_count,
    )
    result = build_cross_source_image_evidence(
        BuildConfig(
            output_path=output,
            inputs=inputs,
            sample_size=None,
            batch_size=2,
        )
    )

    assert result.status == "failed_validation"
    connection = open_sidecar(output, readonly=True, immutable=True)
    try:
        validation = connection.execute(
            """
            SELECT passed,expected,actual FROM e2_validations
            WHERE validation_name='exact_cluster_relation_counts'
            """
        ).fetchone()
        assert tuple(validation) == (0, "0", "1")
    finally:
        connection.close()


def test_global_phash_candidate_query_keeps_pair_ledger_as_outer_loop() -> None:
    source = inspect.getsource(
        evidence_pipeline._build_global_phash_candidates
    )
    marker = (
        "SELECT p.left_node_id,p.right_node_id,p.band_mask,p.shared_band_count,"
    )
    marker_at = source.index(marker)
    sql_start = source.rfind('"""', 0, marker_at) + 3
    sql_end = source.index('"""', marker_at)
    candidate_sql = source[sql_start:sql_end]

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE phash_nodes (
              run_id TEXT NOT NULL,
              node_id TEXT NOT NULL,
              phash_hex TEXT NOT NULL,
              PRIMARY KEY(run_id,node_id),
              UNIQUE(run_id,phash_hex)
            );
            CREATE TEMP TABLE e2_work_phash_pairs (
              left_node_id TEXT NOT NULL,
              right_node_id TEXT NOT NULL,
              band_mask INTEGER NOT NULL,
              shared_band_count INTEGER NOT NULL,
              PRIMARY KEY(left_node_id,right_node_id)
            ) WITHOUT ROWID;
            """
        )
        details = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + candidate_sql,
                ("representative-run", "representative-run"),
            )
        ]
    finally:
        connection.close()

    assert details[0] == "SCAN p"
    assert any(
        detail.startswith("SEARCH l USING INDEX")
        and "(run_id=? AND node_id=?)" in detail
        for detail in details
    )
    assert any(
        detail.startswith("SEARCH r USING INDEX")
        and "(run_id=? AND node_id=?)" in detail
        for detail in details
    )
    assert not any("SEARCH p" in detail for detail in details)
    assert not any("TEMP B-TREE" in detail for detail in details)


def test_metadata_pair_query_uses_name_index_for_cross_source_lookup() -> None:
    source = inspect.getsource(evidence_pipeline._materialize_metadata_pairs)
    marker = (
        "SELECT d.source_building_id,d.normalized_name,d.country,d.locality,"
    )
    marker_at = source.index(marker)
    sql_start = source.rfind('"""', 0, marker_at) + 3
    sql_end = source.index('"""', marker_at)
    metadata_sql = source[sql_start:sql_end]

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE source_buildings (
              run_id TEXT NOT NULL,
              source TEXT NOT NULL,
              source_building_id TEXT NOT NULL,
              normalized_name TEXT,
              country TEXT,
              locality TEXT,
              completion_year_min INTEGER,
              completion_year_max INTEGER,
              PRIMARY KEY(run_id,source,source_building_id)
            );
            CREATE TABLE source_project_buildings (
              run_id TEXT NOT NULL,
              source TEXT NOT NULL,
              source_project_id TEXT NOT NULL,
              source_building_id TEXT NOT NULL,
              PRIMARY KEY(run_id,source,source_project_id,source_building_id)
            );
            CREATE INDEX idx_source_buildings_name
              ON source_buildings(run_id,source,normalized_name);
            CREATE INDEX idx_source_project_buildings_building
              ON source_project_buildings(run_id,source,source_building_id);
            """
        )
        details = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + metadata_sql,
                ("representative-run",),
            )
        ]
    finally:
        connection.close()

    divisare_detail = next(detail for detail in details if " d " in f" {detail} ")
    architizer_detail = next(detail for detail in details if " a " in f" {detail} ")
    assert divisare_detail.startswith(
        "SEARCH d USING INDEX idx_source_buildings_name"
    )
    assert "run_id=? AND source=?" in divisare_detail
    assert architizer_detail.startswith(
        "SEARCH a USING INDEX idx_source_buildings_name"
    )
    assert "run_id=? AND source=? AND normalized_name=?" in architizer_detail
    assert not any(
        detail == "SCAN a" or detail.startswith("SCAN a ") for detail in details
    )
    temp_details = [detail for detail in details if "TEMP B-TREE" in detail]
    assert temp_details in ([], ["USE TEMP B-TREE FOR ORDER BY"])


@pytest.mark.parametrize(
    ("marker", "expected_prefixes"),
    [
        (
            "FROM exact_pixel_cluster_members dm INDEXED BY idx_exact_members_asset",
            (
                "SEARCH dm USING INDEX idx_exact_members_asset",
                "SEARCH am USING COVERING INDEX sqlite_autoindex_exact_pixel_cluster_members_1",
                "SEARCH db USING INDEX idx_building_assets_asset",
                "SEARCH ab USING INDEX idx_building_assets_asset",
                "SEARCH da USING INDEX sqlite_autoindex_assets_1",
                "SEARCH aa USING INDEX sqlite_autoindex_assets_1",
            ),
        ),
        (
            "FROM phash_node_members dm INDEXED BY idx_phash_members_asset",
            (
                "SEARCH dm USING INDEX idx_phash_members_asset",
                "SEARCH am USING COVERING INDEX sqlite_autoindex_phash_node_members_1",
                "SEARCH db USING INDEX idx_building_assets_asset",
                "SEARCH ab USING INDEX idx_building_assets_asset",
                "SEARCH da USING INDEX sqlite_autoindex_assets_1",
                "SEARCH aa USING INDEX sqlite_autoindex_assets_1",
            ),
        ),
        (
            "FROM phash_edges e INDEXED BY idx_phash_edges_distance",
            (
                "SEARCH e USING INDEX idx_phash_edges_distance",
                "SEARCH dm USING COVERING INDEX sqlite_autoindex_phash_node_members_1",
                "SEARCH am USING COVERING INDEX sqlite_autoindex_phash_node_members_1",
                "SEARCH db USING INDEX idx_building_assets_asset",
                "SEARCH ab USING INDEX idx_building_assets_asset",
                "SEARCH da USING INDEX sqlite_autoindex_assets_1",
                "SEARCH aa USING INDEX sqlite_autoindex_assets_1",
            ),
        ),
    ],
)
def test_global_image_evidence_queries_keep_evidence_before_relations(
    marker: str,
    expected_prefixes: tuple[str, ...],
) -> None:
    details = _global_evidence_query_plan(marker)

    assert len(details) == len(expected_prefixes)
    assert all(
        detail.startswith(expected)
        for detail, expected in zip(details, expected_prefixes, strict=True)
    )
    for alias in ("db", "ab"):
        relation_probe = next(
            detail for detail in details if detail.startswith(f"SEARCH {alias} ")
        )
        assert "(run_id=? AND source=? AND source_asset_id=?)" in relation_probe
    assert not any(
        detail == f"SCAN {alias}" or detail.startswith(f"SCAN {alias} ")
        for alias in ("db", "ab")
        for detail in details
    )
    assert not any("TEMP B-TREE" in detail for detail in details)


def test_project_image_evidence_materializes_then_probes_asset_indexes() -> None:
    query = _sql_from_function(
        evidence_pipeline._materialize_project_image_evidence,
        "WITH distinct_evidence AS MATERIALIZED",
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE candidate_image_evidence (
              run_id TEXT NOT NULL,
              evidence_id TEXT NOT NULL,
              left_source_asset_id TEXT NOT NULL,
              right_source_asset_id TEXT NOT NULL,
              evidence_kind TEXT NOT NULL,
              phash_distance INTEGER,
              PRIMARY KEY(run_id,evidence_id)
            );
            CREATE TABLE project_assets (
              run_id TEXT NOT NULL,
              source TEXT NOT NULL,
              source_project_id TEXT NOT NULL,
              source_asset_id TEXT NOT NULL,
              PRIMARY KEY(run_id,source,source_project_id,source_asset_id)
            );
            CREATE INDEX idx_project_assets_asset
              ON project_assets(run_id,source,source_asset_id);
            """
        )
        details = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + query,
                (
                    "representative-run",
                    "representative-run",
                    "representative-run",
                ),
            )
        ]
    finally:
        connection.close()

    assert details.count("MATERIALIZE distinct_evidence") == 1
    scan_at = details.index("SCAN e")
    divisare_at = next(
        index
        for index, detail in enumerate(details)
        if detail.startswith("SEARCH d USING INDEX idx_project_assets_asset")
    )
    architizer_at = next(
        index
        for index, detail in enumerate(details)
        if detail.startswith("SEARCH a USING INDEX idx_project_assets_asset")
    )
    assert details.index("MATERIALIZE distinct_evidence") < scan_at
    assert scan_at < divisare_at < architizer_at
    assert "(run_id=? AND source=? AND source_asset_id=?)" in details[divisare_at]
    assert "(run_id=? AND source=? AND source_asset_id=?)" in details[architizer_at]
    assert not any(
        detail == f"SCAN {alias}" or detail.startswith(f"SCAN {alias} ")
        for alias in ("d", "a")
        for detail in details
    )
    assert not any("TEMP B-TREE FOR ORDER BY" in detail for detail in details)


def test_global_phash_edge_flush_never_precedes_referenced_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "phash-buffer-order.db"
    connection = initialize_sidecar(path)
    run_id = "e2-phash-buffer-order"
    connection.execute(
        """
        INSERT INTO e2_runs(
          run_id,contract_version,builder_version,selection_mode,
          config_json,status,started_at
        ) VALUES(?,?,?,?,?,'building','2026-08-10T00:00:00Z')
        """,
        (
            run_id,
            "e2-test-contract",
            "e2-test-builder",
            "full",
            "{}",
        ),
    )
    phashes = (
        "0" * 64,
        "0" * 63 + "1",
        "0" * 63 + "3",
        "f" * 64,
    )
    connection.executemany(
        """
        INSERT INTO phash_nodes(
          run_id,node_id,phash_hex,member_count,source_count,is_cross_source
        ) VALUES(?,?,?,?,?,?)
        """,
        [
            (
                run_id,
                evidence_pipeline.stable_phash_id(phash),
                phash,
                1,
                1,
                0,
            )
            for phash in phashes
        ],
    )
    connection.commit()
    monkeypatch.setattr(
        evidence_pipeline,
        "phash_band_key",
        lambda _phash, band_index: f"{band_index}:shared",
    )

    try:
        candidate_count, edge_count, rejected_count = (
            evidence_pipeline._build_global_phash_candidates(
                connection,
                run_id,
                batch_size=3,
            )
        )
        assert (candidate_count, edge_count, rejected_count) == (6, 3, 3)
        assert connection.execute(
            "SELECT count(*) FROM phash_candidates WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == 6
        assert connection.execute(
            "SELECT count(*) FROM phash_edges WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == 3
        assert connection.execute(
            """
            SELECT count(*)
            FROM phash_edges e
            LEFT JOIN phash_candidates c
              ON c.run_id=e.run_id AND c.candidate_id=e.candidate_id
            WHERE e.run_id=? AND c.candidate_id IS NULL
            """,
            (run_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()
