from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import canonical.cross_source_image_selection_sources as selection_sources
from canonical.cross_source_image_selection_sources import (
    BUILDING_STRATA,
    E2ArtifactSpec,
    SelectionSourceError,
    open_e2_selection_sources,
)


RUN_ID = "e2-selection-source-fixture"
LOGICAL_SHA = "1" * 64


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _spec(path: Path) -> E2ArtifactSpec:
    return E2ArtifactSpec(
        path=path,
        expected_size=path.stat().st_size,
        expected_sha256=_sha256(path),
        expected_logical_sha256=LOGICAL_SHA,
        expected_contract_version="e2-contract-test",
        expected_builder_version="e2-builder-v5-test",
    )


def _create_e2_fixture(
    path: Path,
    *,
    status: str = "complete",
    selection_mode: str = "full",
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE e2_runs (
          run_id TEXT PRIMARY KEY,
          contract_version TEXT NOT NULL,
          builder_version TEXT NOT NULL,
          selection_mode TEXT NOT NULL,
          ordered_selection_manifest_sha256 TEXT,
          status TEXT NOT NULL
        );
        CREATE TABLE e2_metrics (
          run_id TEXT NOT NULL,
          phase TEXT NOT NULL,
          metric_name TEXT NOT NULL,
          stratum_json TEXT NOT NULL,
          value_text TEXT,
          PRIMARY KEY(run_id,phase,metric_name,stratum_json)
        );
        CREATE TABLE e2_inputs (
          run_id TEXT NOT NULL,
          input_name TEXT NOT NULL,
          source TEXT NOT NULL,
          input_role TEXT NOT NULL,
          file_path TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          sha256_before TEXT NOT NULL,
          sha256_after TEXT,
          application_id INTEGER,
          user_version INTEGER,
          schema_manifest_sha256 TEXT,
          PRIMARY KEY(run_id,input_name)
        );
        CREATE TABLE source_buildings (
          run_id TEXT NOT NULL,
          source TEXT NOT NULL,
          source_building_id TEXT NOT NULL,
          name TEXT NOT NULL,
          source_record_sha256 TEXT NOT NULL,
          PRIMARY KEY(run_id,source,source_building_id)
        );
        CREATE TABLE assets (
          run_id TEXT NOT NULL,
          source TEXT NOT NULL,
          source_asset_id TEXT NOT NULL,
          fingerprint_status TEXT NOT NULL,
          canonical_url TEXT,
          fetch_url TEXT,
          final_url TEXT,
          normalized_pixel_sha256 TEXT,
          phash_hex TEXT,
          original_width INTEGER,
          original_height INTEGER,
          normalized_width INTEGER,
          normalized_height INTEGER,
          source_record_sha256 TEXT NOT NULL,
          provenance_json TEXT NOT NULL,
          PRIMARY KEY(run_id,source,source_asset_id)
        );
        CREATE TABLE building_assets (
          run_id TEXT NOT NULL,
          source TEXT NOT NULL,
          source_building_id TEXT NOT NULL,
          source_asset_id TEXT NOT NULL,
          roles_json TEXT NOT NULL,
          relation_record_sha256 TEXT NOT NULL,
          PRIMARY KEY(run_id,source,source_building_id,source_asset_id)
        );
        CREATE TABLE source_project_buildings (
          run_id TEXT NOT NULL,
          source TEXT NOT NULL,
          source_project_id TEXT NOT NULL,
          source_building_id TEXT NOT NULL,
          PRIMARY KEY(run_id,source,source_project_id,source_building_id)
        );
        CREATE INDEX idx_source_project_buildings_building
          ON source_project_buildings(run_id,source,source_building_id);
        CREATE TABLE project_assets (
          run_id TEXT NOT NULL,
          source TEXT NOT NULL,
          source_project_id TEXT NOT NULL,
          source_asset_id TEXT NOT NULL,
          first_ordinal INTEGER NOT NULL,
          PRIMARY KEY(run_id,source,source_project_id,source_asset_id)
        );
        CREATE INDEX idx_project_assets_asset
          ON project_assets(run_id,source,source_asset_id);
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
        CREATE TABLE cross_source_building_candidates (
          run_id TEXT NOT NULL,
          building_candidate_id TEXT NOT NULL,
          left_source TEXT NOT NULL,
          left_source_building_id TEXT NOT NULL,
          right_source TEXT NOT NULL,
          right_source_building_id TEXT NOT NULL,
          PRIMARY KEY(run_id,building_candidate_id)
        );
        CREATE TABLE phash_edges (
          run_id TEXT NOT NULL,
          edge_id TEXT NOT NULL,
          left_node_id TEXT NOT NULL,
          right_node_id TEXT NOT NULL,
          hamming_distance INTEGER NOT NULL,
          edge_scope TEXT NOT NULL,
          edge_record_sha256 TEXT NOT NULL,
          PRIMARY KEY(run_id,edge_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO e2_runs VALUES (?,?,?,?,?,?)",
        (
            RUN_ID,
            "e2-contract-test",
            "e2-builder-v5-test",
            selection_mode,
            "2" * 64,
            status,
        ),
    )
    connection.execute(
        "INSERT INTO e2_metrics VALUES (?,?,?,?,?)",
        (RUN_ID, "validation", "output_logical_sha256", "{}", LOGICAL_SHA),
    )
    connection.execute(
        "INSERT INTO e2_inputs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            RUN_ID,
            "divisare_e1",
            "divisare",
            "image_fingerprint",
            "C:/frozen/divisare-e1.db",
            123,
            "3" * 64,
            "3" * 64,
            42,
            7,
            "4" * 64,
        ),
    )

    buildings = [
        ("divisare", "b0", "No success"),
        ("divisare", "b1", "Risk cover"),
        ("divisare", "b2", "Gallery fallback"),
        ("divisare", "b3", "Cross candidate"),
        ("divisare", "b4", "Ordinary"),
        ("architizer", "a0", "Cross counterpart"),
    ]
    connection.executemany(
        "INSERT INTO source_buildings VALUES (?,?,?,?,?)",
        [
            (RUN_ID, source, building_id, name, _digest(f"building:{building_id}"))
            for source, building_id, name in buildings
        ],
    )

    assets = [
        ("divisare", "failed", "failed", None, None, None, None, None, None, None, ()),
        (
            "divisare", "risk-dim", "success", "pixel-risk-dim",
            "phash-risk-dim", 200, 512, 200, 512,
            "https://fetch/risk-dim", (),
        ),
        (
            "divisare", "risk-low", "success", "pixel-risk-low",
            "phash-risk-low", 512, 512, 512, 512,
            "https://fetch/risk-low", ("low_information",),
        ),
        (
            "divisare", "gallery", "success", "pixel-gallery",
            "phash-gallery", 768, 512, 512, 341,
            "https://fetch/gallery", (),
        ),
        (
            "divisare", "cross", "success", "pixel-cross", "phash-cross",
            1024, 640, 512, 320, "https://fetch/cross", (),
        ),
        (
            "divisare", "ordinary", "success", "pixel-ordinary",
            "phash-ordinary", 1024, 768, 512, 384,
            "https://fetch/ordinary", (),
        ),
        (
            "architizer", "arch", "success", "pixel-arch", "phash-arch",
            1024, 683, 512, 342, "https://fetch/arch", (),
        ),
    ]
    connection.executemany(
        """
        INSERT INTO assets(
          run_id,source,source_asset_id,fingerprint_status,
          canonical_url,fetch_url,final_url,
          normalized_pixel_sha256,phash_hex,
          original_width,original_height,normalized_width,normalized_height,
          source_record_sha256,provenance_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                RUN_ID,
                source,
                asset_id,
                status_value,
                f"https://canonical/{asset_id}",
                fetch_url,
                None,
                pixel,
                phash,
                original_width,
                original_height,
                normalized_width,
                normalized_height,
                _digest(f"asset:{asset_id}"),
                '{"quality_flags":' + str(list(flags)).replace("'", '"') + "}",
            )
            for (
                source,
                asset_id,
                status_value,
                pixel,
                phash,
                original_width,
                original_height,
                normalized_width,
                normalized_height,
                fetch_url,
                flags,
            ) in assets
        ],
    )

    relations = [
        ("divisare", "b0", "p0", "failed", '["cover"]', 0),
        ("divisare", "b1", "p1", "risk-dim", '["cover"]', 0),
        ("divisare", "b1", "p1", "risk-low", '["cover"]', 1),
        ("divisare", "b2", "p2", "gallery", '["gallery"]', 7),
        ("divisare", "b3", "p3", "cross", '["cover"]', 0),
        ("divisare", "b4", "p4", "ordinary", '["cover"]', 0),
        ("architizer", "a0", "ap0", "arch", '["cover"]', 0),
    ]
    connection.executemany(
        "INSERT INTO source_project_buildings VALUES (?,?,?,?)",
        sorted(
            {
                (RUN_ID, source, project, building)
                for source, building, project, *_ in relations
            }
        ),
    )
    connection.executemany(
        "INSERT INTO project_assets VALUES (?,?,?,?,?)",
        [
            (RUN_ID, source, project, asset, ordinal)
            for source, _, project, asset, _, ordinal in relations
        ],
    )
    connection.executemany(
        "INSERT INTO building_assets VALUES (?,?,?,?,?,?)",
        [
            (
                RUN_ID,
                source,
                building,
                asset,
                roles,
                _digest(f"relation:{source}:{building}:{asset}"),
            )
            for source, building, _, asset, roles, _ in relations
        ],
    )
    connection.execute(
        "INSERT INTO exact_pixel_cluster_members VALUES (?,?,?,?)",
        (RUN_ID, "exact-gallery", "divisare", "gallery"),
    )
    successful = [row for row in assets if row[2] == "success"]
    connection.executemany(
        "INSERT INTO phash_node_members VALUES (?,?,?,?)",
        [
            (RUN_ID, f"node-{asset_id}", source, asset_id)
            for source, asset_id, *_ in successful
        ],
    )
    connection.execute(
        "INSERT INTO cross_source_building_candidates VALUES (?,?,?,?,?,?)",
        (RUN_ID, "candidate-1", "divisare", "b3", "architizer", "a0"),
    )
    connection.executemany(
        "INSERT INTO phash_edges VALUES (?,?,?,?,?,?,?)",
        [
            (RUN_ID, "edge-1", "node-gallery", "node-cross", 4, "global_le8", _digest("edge:1")),
            (RUN_ID, "edge-2", "node-cross", "node-ordinary", 6, "global_le8", _digest("edge:2")),
            (
                RUN_ID, "edge-review", "node-gallery", "node-ordinary", 10,
                "metadata_9_16", _digest("edge:review"),
            ),
        ],
    )
    connection.commit()
    connection.close()


def test_verified_lineage_and_version_binding(tmp_path: Path) -> None:
    path = tmp_path / "e2.db"
    _create_e2_fixture(path)
    spec = _spec(path)

    with open_e2_selection_sources(spec, batch_size=2) as source:
        assert source.run_id == RUN_ID
        assert source.contract_version == "e2-contract-test"
        assert source.builder_version == "e2-builder-v5-test"
        assert source.stored_logical_sha256 == LOGICAL_SHA
        assert source.lineage.artifact_size == path.stat().st_size
        assert source.lineage.artifact_sha256 == spec.expected_sha256
        assert len(source.input_lineage) == 1
        assert source.input_lineage[0].sha256_before == "3" * 64
        assert source.input_lineage[0].sha256_after == "3" * 64

    with pytest.raises(SelectionSourceError, match="byte SHA"):
        open_e2_selection_sources(
            replace(spec, expected_sha256="0" * 64),
        )
    with pytest.raises(SelectionSourceError, match="logical SHA"):
        open_e2_selection_sources(
            replace(spec, expected_logical_sha256="0" * 64),
        )


@pytest.mark.parametrize(
    ("status", "selection_mode"),
    [("building", "full"), ("complete", "sample")],
)
def test_rejects_nonterminal_or_nonfull_run(
    tmp_path: Path,
    status: str,
    selection_mode: str,
) -> None:
    path = tmp_path / f"e2-{status}-{selection_mode}.db"
    _create_e2_fixture(path, status=status, selection_mode=selection_mode)
    with pytest.raises(SelectionSourceError, match="terminal complete"):
        open_e2_selection_sources(_spec(path))


def test_rejects_sqlite_or_lock_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "e2.db"
    _create_e2_fixture(path)
    Path(str(path) + "-wal").write_bytes(b"not a real WAL")

    with pytest.raises(SelectionSourceError, match="sidecars"):
        open_e2_selection_sources(_spec(path))


def test_rejects_multiple_runs_and_mutated_input_lineage(tmp_path: Path) -> None:
    multiple_path = tmp_path / "multiple.db"
    _create_e2_fixture(multiple_path)
    connection = sqlite3.connect(multiple_path)
    connection.execute(
        "INSERT INTO e2_runs VALUES (?,?,?,?,?,?)",
        (
            "second-run",
            "e2-contract-test",
            "e2-builder-v5-test",
            "full",
            "5" * 64,
            "complete",
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SelectionSourceError, match="exactly one run"):
        open_e2_selection_sources(_spec(multiple_path))

    mutated_path = tmp_path / "mutated-input.db"
    _create_e2_fixture(mutated_path)
    connection = sqlite3.connect(mutated_path)
    connection.execute("UPDATE e2_inputs SET sha256_after=?", ("6" * 64,))
    connection.commit()
    connection.close()
    with pytest.raises(SelectionSourceError, match="input mutated"):
        open_e2_selection_sources(_spec(mutated_path))


def test_building_summary_strata_use_required_precedence(tmp_path: Path) -> None:
    path = tmp_path / "e2.db"
    _create_e2_fixture(path)
    with open_e2_selection_sources(_spec(path), batch_size=2) as source:
        summaries = list(source.iter_building_summaries("divisare"))
        assert [item.source_building_id for item in summaries] == [
            "b0",
            "b1",
            "b2",
            "b3",
            "b4",
        ]
        assert [item.stratum for item in summaries] == list(BUILDING_STRATA)
        assert summaries[0].successful_asset_count == 0
        assert summaries[1].successful_cover_count == 2
        assert summaries[1].quality_risk_cover_count == 2
        assert summaries[3].cross_source_candidate
        assert source.building_stratum_counts("divisare") == {
            stratum: 1 for stratum in BUILDING_STRATA
        }
        assert source.building_population_count("divisare") == 5


def test_candidates_preserve_guard_tie_and_lineage_fields(tmp_path: Path) -> None:
    path = tmp_path / "e2.db"
    _create_e2_fixture(path)
    with open_e2_selection_sources(_spec(path), batch_size=1) as source:
        risk = list(source.iter_candidates("divisare", "b1"))
        assert [item.source_asset_id for item in risk] == ["risk-dim", "risk-low"]
        assert risk[0].decoded_min_edge == 200
        assert risk[0].quality_risk
        assert risk[0].lowest_project_ordinal == 0
        assert risk[1].quality_flags == ("low_information",)
        assert risk[1].quality_risk
        assert risk[1].lowest_project_ordinal == 1

        gallery = list(source.iter_candidates("divisare", "b2"))[0]
        assert gallery.roles == ("gallery",)
        assert not gallery.is_cover
        assert gallery.lowest_project_ordinal == 7
        assert gallery.exact_cluster_id == "exact-gallery"
        assert gallery.phash_node_id == "node-gallery"
        assert gallery.normalized_pixel_sha256 == "pixel-gallery"
        assert gallery.canonical_url == "https://canonical/gallery"
        assert gallery.fetch_url == "https://fetch/gallery"
        assert gallery.source_asset_record_sha256 == _digest("asset:gallery")
        assert gallery.building_relation_record_sha256 == _digest(
            "relation:divisare:b2:gallery"
        )


def test_iter_all_candidates_streams_source_building_asset_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "e2.db"
    _create_e2_fixture(path)
    with open_e2_selection_sources(_spec(path), batch_size=2) as source:
        observed = [
            (item.source, item.source_building_id, item.source_asset_id)
            for item in source.iter_all_candidates()
        ]
    assert observed == sorted(observed)
    assert observed == [
        ("architizer", "a0", "arch"),
        ("divisare", "b1", "risk-dim"),
        ("divisare", "b1", "risk-low"),
        ("divisare", "b2", "gallery"),
        ("divisare", "b3", "cross"),
        ("divisare", "b4", "ordinary"),
    ]


def test_full_candidate_plan_uses_correlated_indexed_ordinal_lookup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "e2.db"
    _create_e2_fixture(path)
    connection = sqlite3.connect(path)
    try:
        details = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + selection_sources._CANDIDATE_SQL,
                (RUN_ID, None, None, None, None),
            )
        ]
    finally:
        connection.close()

    assert not any("MATERIALIZE derived_ordinals" in detail for detail in details)
    building_at = next(
        index
        for index, detail in enumerate(details)
        if detail.startswith(
            "SEARCH ba USING INDEX sqlite_autoindex_building_assets_1"
        )
    )
    asset_at = next(
        index
        for index, detail in enumerate(details)
        if detail.startswith("SEARCH a USING INDEX sqlite_autoindex_assets_1")
    )
    assert building_at < asset_at
    assert any("CORRELATED SCALAR SUBQUERY" in detail for detail in details)
    assert any("idx_project_assets_asset" in detail for detail in details)
    assert any("idx_source_project_buildings_building" in detail for detail in details)
    assert not any("TEMP B-TREE FOR ORDER BY" in detail for detail in details)


def test_full_candidate_resume_plan_uses_composite_keyset(tmp_path: Path) -> None:
    path = tmp_path / "e2.db"
    _create_e2_fixture(path)
    connection = sqlite3.connect(path)
    try:
        details = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + selection_sources._CANDIDATE_AFTER_SQL,
                (RUN_ID, None, None, None, None, "divisare", "b2"),
            )
        ]
    finally:
        connection.close()

    assert any(
        "sqlite_autoindex_building_assets_1" in detail
        and "(source,source_building_id)>(?,?)" in detail
        for detail in details
    )
    assert any("idx_project_assets_asset" in detail for detail in details)


def test_direct_phash_pairs_never_form_transitive_components(
    tmp_path: Path,
) -> None:
    path = tmp_path / "e2.db"
    _create_e2_fixture(path)
    with open_e2_selection_sources(_spec(path), batch_size=1) as source:
        all_edges = list(source.direct_phash_pairs())
        assert [edge.edge_id for edge in all_edges] == ["edge-2", "edge-1"]
        assert all(1 <= edge.hamming_distance <= 8 for edge in all_edges)
        assert [
            edge.edge_id
            for edge in source.direct_phash_pairs(
                {"node-gallery", "node-cross"}
            )
        ] == ["edge-1"]
        assert list(
            source.direct_phash_pairs({"node-gallery", "node-ordinary"})
        ) == []
