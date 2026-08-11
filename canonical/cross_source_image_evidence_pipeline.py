"""Offline E2 builder for Divisare--Architizer image evidence.

The builder reads immutable curated metadata and E1 fingerprint sidecars.  It
never downloads images, runs Vision, selects representative images, or decides
that two buildings are identical.  Its output is a factual, source-qualified
evidence ledger that can support those later decisions.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from canonical.cross_source_image_evidence import (
    E2_EVIDENCE_VERSION,
    E2_SCHEMA_VERSION,
    METADATA_NORMALIZATION_VERSION,
    PHASH_BAND_COUNT,
    PHASH_BAND_VERSION,
    PHASH_PAIR_POLICY_VERSION,
    SAMPLE_POLICY_VERSION,
    canonical_json,
    canonical_sha256,
    classify_phash_pair,
    deterministic_sample_score,
    normalize_block_text,
    phash_band_key,
    stable_edge_id,
    stable_exact_id,
    stable_node_id,
    stable_phash_id,
)
from canonical.cross_source_image_evidence_sidecar import (
    SidecarSchemaError,
    acquire_build_lock,
    finalize_sidecar,
    initialize_sidecar,
    open_sidecar,
    prepare_immutable_sidecar,
    recover_sidecar,
    sqlite_sidecar_paths,
    validate_sidecar,
)
from canonical.cross_source_image_evidence_sources import (
    SourceAssetFingerprint,
    SourceBuilding,
    SourceMembership,
    SourceOccurrence,
    SourceProject,
    open_architizer_sources,
    open_divisare_sources,
)


PIPELINE_VERSION = "archibe-e2-cross-source-image-evidence-pipeline-v5"
DEFAULT_SAMPLE_SEED = "archibe-e2-real-smoke-v1"
DEFAULT_BATCH_SIZE = 5_000
INPUT_HASH_CHUNK_SIZE = 8 * 1024 * 1024


@dataclass(frozen=True)
class InputSpec:
    role: str
    source: str
    path: Path
    expected_size: int
    expected_sha256: str


@dataclass(frozen=True)
class InputSnapshot:
    role: str
    source: str
    path: Path
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class BuildConfig:
    output_path: Path
    inputs: tuple[InputSpec, ...]
    sample_size: int | None = None
    sample_seed: str = DEFAULT_SAMPLE_SEED
    batch_size: int = DEFAULT_BATCH_SIZE

    @property
    def mode(self) -> str:
        return "full" if self.sample_size is None else "sample"


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    run_id: str
    status: str
    elapsed_seconds: float
    logical_sha256: str | None
    metrics: dict[str, int | float | str | None]


def default_input_specs(repo_root: Path | str) -> tuple[InputSpec, ...]:
    root = Path(repo_root).resolve()
    return (
        InputSpec(
            "divisare_curated",
            "divisare",
            root / "data/curated/divisare_metadata_v2_4.db",
            2_225_299_456,
            "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f",
        ),
        InputSpec(
            "architizer_curated",
            "architizer",
            root / "data/curated/architizer_curated_v2_0.db",
            8_767_438_848,
            "605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc",
        ),
        InputSpec(
            "divisare_e1",
            "divisare",
            root / "data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db",
            2_646_114_304,
            "869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819",
        ),
        InputSpec(
            "architizer_e1",
            "architizer",
            root / "data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db",
            4_373_962_752,
            "58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937",
        ),
    )


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(INPUT_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_sidecars(path: Path | str) -> tuple[Path, ...]:
    database = Path(path)
    return tuple(
        candidate
        for suffix in ("-wal", "-shm", "-journal")
        if (candidate := Path(str(database) + suffix)).exists()
    )


def snapshot_input(spec: InputSpec) -> InputSnapshot:
    path = spec.path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    sidecars = sqlite_sidecars(path)
    if sidecars:
        raise RuntimeError(
            "immutable input has SQLite sidecars: "
            + ", ".join(str(item) for item in sidecars)
        )
    byte_size = path.stat().st_size
    if byte_size != spec.expected_size:
        raise ValueError(
            f"{spec.role} byte size mismatch: {byte_size} != {spec.expected_size}"
        )
    digest = sha256_file(path)
    if digest != spec.expected_sha256:
        raise ValueError(
            f"{spec.role} SHA-256 mismatch: {digest} != {spec.expected_sha256}"
        )
    return InputSnapshot(spec.role, spec.source, path, byte_size, digest)


def snapshot_inputs(specs: Iterable[InputSpec]) -> tuple[InputSnapshot, ...]:
    snapshots = tuple(snapshot_input(spec) for spec in specs)
    roles = [snapshot.role for snapshot in snapshots]
    if len(roles) != len(set(roles)):
        raise ValueError("input roles must be unique")
    required = {
        "divisare_curated",
        "architizer_curated",
        "divisare_e1",
        "architizer_e1",
    }
    if set(roles) != required:
        raise ValueError(f"input roles must be exactly {sorted(required)}")
    return snapshots


def open_immutable(path: Path | str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _attached_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def _attach_inputs(
    connection: sqlite3.Connection, snapshots: Sequence[InputSnapshot]
) -> None:
    aliases = {
        "divisare_curated": "divmeta",
        "architizer_curated": "archmeta",
        "divisare_e1": "dive1",
        "architizer_e1": "arche1",
    }
    for snapshot in snapshots:
        connection.execute(
            f"ATTACH DATABASE ? AS {aliases[snapshot.role]}",
            (_attached_uri(snapshot.path),),
        )


def _single_e1_run(connection: sqlite3.Connection, alias: str, source: str) -> str:
    rows = connection.execute(
        f"""
        SELECT run_id,source_name,status,selection_mode
        FROM {alias}.fingerprint_runs
        """
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"{source} E1 input must contain exactly one run")
    row = rows[0]
    if str(row["source_name"]) != source:
        raise ValueError(f"{source} E1 source_name mismatch")
    if str(row["status"]) not in {"complete", "complete_with_failures"}:
        raise ValueError(f"{source} E1 run is not terminal-success")
    if str(row["selection_mode"]) != "full":
        raise ValueError(f"{source} E1 input is not a full selection")
    pending = int(
        connection.execute(
            f"SELECT count(*) FROM {alias}.fingerprints WHERE status='pending'"
        ).fetchone()[0]
    )
    if pending:
        raise ValueError(f"{source} E1 input contains {pending} pending rows")
    return str(row["run_id"])


def _quality_flags(metadata_json: str | None) -> str:
    if not metadata_json:
        return "[]"
    try:
        value = json.loads(metadata_json)
    except (TypeError, ValueError):
        return canonical_json(["metadata_json_invalid"])
    flags = value.get("quality_flags", []) if isinstance(value, dict) else []
    if not isinstance(flags, list):
        return canonical_json(["quality_flags_invalid"])
    return canonical_json(sorted({str(flag) for flag in flags}))


def _source_asset_node_id(source: str, asset_id: object) -> str:
    return stable_node_id(source, str(asset_id))


def _sample_score(source: str, asset_id: object, seed: str) -> str:
    return deterministic_sample_score(seed, f"{source}:{asset_id}")


def _chunked(cursor: sqlite3.Cursor, size: int) -> Iterator[list[sqlite3.Row]]:
    while rows := cursor.fetchmany(size):
        yield rows


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _insert_metric(
    connection: sqlite3.Connection,
    run_id: str,
    phase: str,
    name: str,
    value: int | float | str | None,
    *,
    stratum: dict[str, object] | None = None,
) -> None:
    if value is None:
        value = "null"
    value_integer = int(value) if isinstance(value, (bool, int)) else None
    value_real = float(value) if isinstance(value, float) else None
    value_text = str(value) if not isinstance(value, (bool, int, float)) else None
    connection.execute(
        """
        INSERT OR REPLACE INTO e2_metrics(
            run_id,phase,metric_name,stratum_json,
            value_integer,value_real,value_text,recorded_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            phase,
            name,
            canonical_json(stratum or {}),
            value_integer,
            value_real,
            value_text,
            _utc_now(),
        ),
    )


def _input_manifest(snapshots: Sequence[InputSnapshot], config: BuildConfig) -> str:
    return canonical_sha256(
        {
            "algorithm_versions": {
                "e2": E2_EVIDENCE_VERSION,
                "metadata": METADATA_NORMALIZATION_VERSION,
                "phash_band": PHASH_BAND_VERSION,
                "phash_pair": PHASH_PAIR_POLICY_VERSION,
                "pipeline": PIPELINE_VERSION,
                "sample": SAMPLE_POLICY_VERSION,
                "schema": E2_SCHEMA_VERSION,
            },
            "inputs": [
                {
                    "role": item.role,
                    "sha256": item.sha256,
                    "size": item.byte_size,
                    "source": item.source,
                }
                for item in sorted(snapshots, key=lambda value: value.role)
            ],
            "mode": config.mode,
            "sample_seed": config.sample_seed if config.sample_size is not None else None,
            "sample_size": config.sample_size,
        }
    )


def _run_id(input_manifest_sha256: str) -> str:
    return "e2-" + input_manifest_sha256[:24]


def _host_pid_json() -> str:
    return canonical_json(
        {"hostname": socket.gethostname(), "pid": os.getpid()}
    )


def _validate_config(config: BuildConfig) -> None:
    if config.batch_size < 1:
        raise ValueError("batch_size must be positive")
    if config.sample_size is not None and config.sample_size < 10:
        raise ValueError("real E2 smoke samples must contain at least 10 assets")
    if not config.sample_seed.strip():
        raise ValueError("sample_seed must be non-empty")


def _snapshot_by_role(
    snapshots: Sequence[InputSnapshot],
) -> dict[str, InputSnapshot]:
    return {snapshot.role: snapshot for snapshot in snapshots}


def _schema_manifest(path: Path) -> tuple[int, int, str]:
    connection = open_immutable(path)
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        rows = [
            {
                "name": str(row[1]),
                "sql": row[4],
                "table": str(row[2]),
                "type": str(row[0]),
            }
            for row in connection.execute(
                """
                SELECT type,name,tbl_name,rootpage,sql
                FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type,name
                """
            )
        ]
        return application_id, user_version, canonical_sha256(rows)
    finally:
        connection.close()


def _record_run_and_inputs(
    connection: sqlite3.Connection,
    run_id: str,
    manifest_sha256: str,
    snapshots: Sequence[InputSnapshot],
    config: BuildConfig,
) -> None:
    started_at = _utc_now()
    connection.execute(
        """
        INSERT INTO e2_runs(
          run_id,contract_version,builder_version,selection_mode,
          sample_size,sample_seed,config_json,status,started_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            E2_EVIDENCE_VERSION,
            PIPELINE_VERSION,
            config.mode,
            config.sample_size,
            config.sample_seed if config.sample_size is not None else None,
            canonical_json(
                {
                    "batch_size": config.batch_size,
                    "input_manifest_sha256": manifest_sha256,
                    "metadata_block": "exact_conservative_normalized_building_name",
                    "network_requests": 0,
                    "representative_selection": False,
                    "vision_requests": 0,
                }
            ),
            "building",
            started_at,
        ),
    )
    for snapshot in snapshots:
        application_id, user_version, schema_sha = _schema_manifest(snapshot.path)
        role = "e1_sidecar" if snapshot.role.endswith("_e1") else "source_db"
        connection.execute(
            """
            INSERT INTO e2_inputs(
              run_id,input_name,source,input_role,file_path,size_bytes,
              sha256_before,application_id,user_version,
              schema_manifest_sha256,recorded_at,detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                snapshot.role,
                snapshot.source,
                role,
                str(snapshot.path),
                snapshot.byte_size,
                snapshot.sha256,
                application_id,
                user_version,
                schema_sha,
                started_at,
                canonical_json({"immutable": True, "sqlite_sidecars": []}),
            ),
        )
    connection.commit()


def _add_sample_item(
    selected: dict[tuple[str, str], set[str]],
    source: str,
    asset_id: object,
    reason: str,
) -> None:
    selected.setdefault((source, str(asset_id)), set()).add(reason)


def _sample_special_rows(
    connection: sqlite3.Connection,
    selected: dict[tuple[str, str], set[str]],
    *,
    include_metadata_pair: bool,
) -> None:
    exact = connection.execute(
        """
        SELECT d.source_asset_id,a.source_asset_id
        FROM dive1.fingerprints d
        JOIN arche1.fingerprints a
          ON a.normalized_pixel_sha256=d.normalized_pixel_sha256
        WHERE d.status='success' AND a.status='success'
        ORDER BY d.normalized_pixel_sha256,d.source_asset_id,a.source_asset_id
        LIMIT 1
        """
    ).fetchone()
    if exact is not None:
        _add_sample_item(selected, "divisare", exact[0], "cross_source_exact")
        _add_sample_item(selected, "architizer", exact[1], "cross_source_exact")

    identical = connection.execute(
        """
        SELECT d.source_asset_id,a.source_asset_id
        FROM dive1.fingerprints d
        JOIN arche1.fingerprints a ON a.phash_hex=d.phash_hex
        WHERE d.status='success' AND a.status='success'
          AND d.normalized_pixel_sha256<>a.normalized_pixel_sha256
          AND d.metadata_json NOT LIKE '%low_information%'
          AND a.metadata_json NOT LIKE '%low_information%'
        ORDER BY d.phash_hex,d.source_asset_id,a.source_asset_id
        LIMIT 1
        """
    ).fetchone()
    if identical is not None:
        _add_sample_item(selected, "divisare", identical[0], "cross_source_same_phash")
        _add_sample_item(selected, "architizer", identical[1], "cross_source_same_phash")

    for source, alias in (("divisare", "dive1"), ("architizer", "arche1")):
        failed = connection.execute(
            f"""
            SELECT source_asset_id FROM {alias}.fingerprints
            WHERE status='failed' ORDER BY source_asset_id LIMIT 1
            """
        ).fetchone()
        if failed is not None:
            _add_sample_item(selected, source, failed[0], "terminal_failed")
        excluded = connection.execute(
            f"""
            SELECT source_asset_id FROM {alias}.source_asset_exclusions
            ORDER BY reason_code,source_asset_id LIMIT 1
            """
        ).fetchone()
        if excluded is not None:
            _add_sample_item(selected, source, excluded[0], "source_excluded")
        low_information = connection.execute(
            f"""
            SELECT source_asset_id FROM {alias}.fingerprints
            WHERE status='success' AND metadata_json LIKE '%low_information%'
            ORDER BY source_asset_id LIMIT 1
            """
        ).fetchone()
        if low_information is not None:
            _add_sample_item(
                selected, source, low_information[0], "low_information_qa"
            )

    if include_metadata_pair:
        div_names: dict[str, tuple[str, str]] = {}
        for row in connection.execute(
            """
            SELECT building_id,primary_article_id,name_normalized
            FROM divmeta.buildings
            WHERE length(trim(name_normalized))>=4
            ORDER BY name_normalized,building_id
            """
        ):
            div_names.setdefault(str(row[2]), (str(row[0]), str(row[1])))
        for row in connection.execute(
            """
            SELECT building_id,primary_project_id,normalized_name
            FROM archmeta.buildings
            WHERE length(trim(normalized_name))>=4
            ORDER BY normalized_name,building_id
            """
        ):
            name = str(row[2])
            div = div_names.get(name)
            if div is None:
                continue
            div_asset = connection.execute(
                """
                SELECT o.asset_key
                FROM divmeta.source_image_occurrences o
                JOIN dive1.fingerprints f ON f.source_asset_id=o.asset_key
                WHERE o.article_id=? AND f.status='success'
                ORDER BY o.role,o.position,o.asset_key LIMIT 1
                """,
                (div[1],),
            ).fetchone()
            arch_asset = connection.execute(
                """
                SELECT o.asset_id
                FROM archmeta.source_image_occurrences o
                JOIN arche1.fingerprints f ON f.source_asset_id=o.asset_id
                WHERE o.source_project_id=? AND f.status='success'
                ORDER BY o.role,o.ordinal,o.asset_id LIMIT 1
                """,
                (str(row[1]),),
            ).fetchone()
            if div_asset is not None and arch_asset is not None:
                _add_sample_item(
                    selected, "divisare", div_asset[0], "metadata_block_endpoint"
                )
                _add_sample_item(
                    selected, "architizer", arch_asset[0], "metadata_block_endpoint"
                )
                break


def _select_sample_assets(
    connection: sqlite3.Connection,
    *,
    size: int,
    seed: str,
) -> tuple[tuple[str, str, str, str], ...]:
    selected: dict[tuple[str, str], set[str]] = {}
    _sample_special_rows(
        connection, selected, include_metadata_pair=size >= 100
    )
    if len(selected) > size:
        selected = dict(
            sorted(
                selected.items(),
                key=lambda item: (
                    deterministic_sample_score(seed, f"{item[0][0]}:{item[0][1]}"),
                    item[0],
                ),
            )[:size]
        )

    remaining = size - len(selected)
    heap: list[tuple[int, str, str, str]] = []
    if remaining:
        for source, alias in (("divisare", "dive1"), ("architizer", "arche1")):
            cursor = connection.execute(
                f"""
                SELECT source_asset_id FROM {alias}.source_assets
                UNION ALL
                SELECT source_asset_id FROM {alias}.source_asset_exclusions
                ORDER BY source_asset_id
                """
            )
            for row in cursor:
                asset_id = str(row[0])
                if (source, asset_id) in selected:
                    continue
                score = deterministic_sample_score(seed, f"{source}:{asset_id}")
                score_number = int(score, 16)
                entry = (-score_number, source, asset_id, score)
                if len(heap) < remaining:
                    heapq.heappush(heap, entry)
                elif score_number < -heap[0][0]:
                    heapq.heapreplace(heap, entry)
        for _, source, asset_id, _ in heap:
            _add_sample_item(selected, source, asset_id, "deterministic_fill")

    if len(selected) != size:
        raise RuntimeError(f"sample selection produced {len(selected)} rows, expected {size}")
    result = []
    for (source, asset_id), reasons in selected.items():
        score = deterministic_sample_score(seed, f"{source}:{asset_id}")
        result.append((source, asset_id, "+".join(sorted(reasons)), score))
    return tuple(sorted(result, key=lambda row: (row[3], row[0], row[1])))


def _record_sample_manifest(
    connection: sqlite3.Connection,
    run_id: str,
    selected: Sequence[tuple[str, str, str, str]],
    seed: str,
) -> str:
    manifest_name = f"real_n{len(selected)}"
    records = [
        {
            "asset_id": asset_id,
            "rank": rank,
            "reason": reason,
            "score": score,
            "source": source,
        }
        for rank, (source, asset_id, reason, score) in enumerate(selected, 1)
    ]
    manifest_sha = canonical_sha256(records)
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO smoke_manifests(
          run_id,manifest_name,sample_size,sample_seed,selection_version,
          ordered_manifest_sha256,selection_scope_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            manifest_name,
            len(selected),
            seed,
            SAMPLE_POLICY_VERSION,
            manifest_sha,
            canonical_json(
                {
                    "entity": "source_asset",
                    "special_strata": [
                        "cross_source_exact",
                        "cross_source_same_phash",
                        "terminal_failed",
                        "source_excluded",
                        "low_information_qa",
                        "metadata_block_endpoint",
                    ],
                }
            ),
            now,
        ),
    )
    for record in records:
        connection.execute(
            """
            INSERT INTO smoke_manifest_items(
              run_id,manifest_name,selection_rank,entity_kind,source,
              source_entity_id,stratum,score_sha256,item_record_sha256,detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                manifest_name,
                record["rank"],
                "asset",
                record["source"],
                record["asset_id"],
                record["reason"],
                record["score"],
                canonical_sha256(record),
                canonical_json({}),
            ),
        )
    connection.commit()
    return manifest_sha


def _project_record(project: SourceProject) -> dict[str, object]:
    return {
        "city": project.city,
        "country": project.country,
        "firm_name": project.firm_name,
        "firm_slug": project.firm_slug,
        "global_id": project.global_id,
        "name": project.name,
        "normalized_name_source": project.normalized_name,
        "slug": project.slug,
        "source": project.source,
        "source_project_id": project.source_project_id,
        "source_status": project.source_status,
        "source_url": project.source_url,
        "year": project.year,
    }


def _building_record(building: SourceBuilding) -> dict[str, object]:
    return {
        "city": building.city,
        "country": building.country,
        "firm_name": building.firm_name,
        "firm_slug": building.firm_slug,
        "identity_status": building.identity_status,
        "name": building.name,
        "normalized_name_source": building.normalized_name,
        "primary_source_project_id": building.primary_source_project_id,
        "source": building.source,
        "source_building_id": building.source_building_id,
        "year": building.year,
    }


def _membership_record(membership: SourceMembership, ordinal: int) -> dict[str, object]:
    return {
        "confidence": membership.confidence,
        "is_primary": membership.is_primary,
        "membership_role": membership.membership_role,
        "membership_status": membership.membership_status,
        "ordinal": ordinal,
        "rule_id": membership.rule_id,
        "source": membership.source,
        "source_building_id": membership.source_building_id,
        "source_project_id": membership.source_project_id,
    }


def _occurrence_record(occurrence: SourceOccurrence) -> dict[str, object]:
    return {
        "asset_id": occurrence.source_asset_id,
        "image_type": occurrence.image_type,
        "occurrence_id": occurrence.occurrence_id,
        "ordinal": occurrence.ordinal,
        "parse_error": occurrence.parse_error,
        "parse_status": occurrence.parse_status,
        "project_id": occurrence.source_project_id,
        "raw_url": occurrence.raw_url,
        "role": occurrence.role,
        "source": occurrence.source,
        "source_field": occurrence.source_field,
    }


def _insert_project(
    connection: sqlite3.Connection, run_id: str, project: SourceProject
) -> None:
    record = _project_record(project)
    record_sha = project.source_record_sha256 or canonical_sha256(record)
    connection.execute(
        """
        INSERT OR IGNORE INTO source_projects(
          run_id,source,source_project_id,canonical_url,slug,global_id,name,
          normalized_name,country,locality,completion_year_min,
          completion_year_max,source_record_sha256,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            project.source,
            project.source_project_id,
            project.source_url,
            project.slug,
            project.global_id,
            project.name,
            normalize_block_text(project.normalized_name or project.name),
            project.country,
            project.city,
            project.year,
            project.year,
            record_sha,
            canonical_json(record),
        ),
    )


def _insert_building(
    connection: sqlite3.Connection, run_id: str, building: SourceBuilding
) -> None:
    record = _building_record(building)
    connection.execute(
        """
        INSERT OR IGNORE INTO source_buildings(
          run_id,source,source_building_id,name,normalized_name,country,
          locality,completion_year_min,completion_year_max,
          source_record_sha256,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            building.source,
            building.source_building_id,
            building.name,
            normalize_block_text(building.normalized_name or building.name),
            building.country,
            building.city,
            building.year,
            building.year,
            canonical_sha256(record),
            canonical_json(record),
        ),
    )


def _insert_memberships(
    connection: sqlite3.Connection,
    run_id: str,
    memberships: Iterable[SourceMembership],
) -> int:
    count = 0
    previous: tuple[str, str] | None = None
    ordinal = 0
    for membership in memberships:
        group = (membership.source, membership.source_building_id)
        ordinal = ordinal + 1 if group == previous else 0
        previous = group
        record = _membership_record(membership, ordinal)
        connection.execute(
            """
            INSERT OR IGNORE INTO source_project_buildings(
              run_id,source,source_project_id,source_building_id,
              membership_reason,membership_ordinal,source_record_sha256,detail_json
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                membership.source,
                membership.source_project_id,
                membership.source_building_id,
                membership.rule_id
                or membership.membership_role
                or membership.membership_status,
                ordinal,
                canonical_sha256(record),
                canonical_json(record),
            ),
        )
        count += 1
    return count


def _insert_assets(
    connection: sqlite3.Connection,
    run_id: str,
    assets: Iterable[SourceAssetFingerprint],
    *,
    included_ids: set[str] | None,
    batch_size: int,
) -> int:
    rows: list[tuple[object, ...]] = []
    count = 0
    query = """
        INSERT OR IGNORE INTO assets(
          run_id,source,source_asset_id,e1_run_id,fingerprint_status,
          canonical_url,fetch_url,raw_response_sha256,
          normalized_pixel_sha256,phash_hex,original_width,original_height,
          normalized_width,normalized_height,source_record_sha256,
          provenance_json,error_kind,error_message
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    for asset in assets:
        if included_ids is not None and asset.source_asset_id not in included_ids:
            continue
        provenance = {
            "e1_metadata_json": asset.metadata_json,
            "exclusion_detail_json": asset.exclusion_detail_json,
            "exclusion_reason": asset.exclusion_reason,
            "ledger_status": asset.ledger_status,
            "quality_flags": list(asset.quality_flags),
            "source_asset_key": asset.source_asset_key,
        }
        rows.append(
            (
                run_id,
                asset.source,
                asset.source_asset_id,
                asset.e1_run_id,
                asset.fingerprint_status,
                asset.canonical_url,
                asset.fetch_url,
                asset.raw_response_sha256,
                asset.normalized_pixel_sha256,
                asset.phash_hex,
                asset.original_width,
                asset.original_height,
                asset.normalized_width,
                asset.normalized_height,
                asset.source_record_sha256,
                canonical_json(provenance),
                asset.error_kind,
                asset.error_message,
            )
        )
        count += 1
        if len(rows) >= batch_size:
            connection.executemany(query, rows)
            rows.clear()
    if rows:
        connection.executemany(query, rows)
    return count


def _insert_occurrences(
    connection: sqlite3.Connection,
    run_id: str,
    occurrences: Iterable[SourceOccurrence],
    *,
    included_asset_ids: set[str] | None,
    batch_size: int,
) -> int:
    rows: list[tuple[object, ...]] = []
    count = 0
    query = """
        INSERT OR IGNORE INTO project_asset_occurrences(
          run_id,source,occurrence_id,source_project_id,raw_asset_key,
          source_asset_id,resolution_status,role,ordinal,occurrence_url,
          source_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """
    for occurrence in occurrences:
        asset_id = occurrence.source_asset_id
        if included_asset_ids is not None and asset_id not in included_asset_ids:
            continue
        if asset_id is None:
            resolution_status = (
                "malformed" if occurrence.parse_status == "malformed" else "missing"
            )
            raw_key = "unresolved:" + canonical_sha256(_occurrence_record(occurrence))
        else:
            resolution_status = "linked"
            raw_key = asset_id
        record = _occurrence_record(occurrence)
        rows.append(
            (
                run_id,
                occurrence.source,
                occurrence.occurrence_id,
                occurrence.source_project_id,
                raw_key,
                asset_id,
                resolution_status,
                occurrence.role,
                occurrence.ordinal,
                occurrence.raw_url,
                canonical_sha256(record),
                canonical_json(
                    {
                        "image_type": occurrence.image_type,
                        "parse_error": occurrence.parse_error,
                        "parse_status": occurrence.parse_status,
                        "source_field": occurrence.source_field,
                    }
                ),
            )
        )
        count += 1
        if len(rows) >= batch_size:
            connection.executemany(query, rows)
            rows.clear()
    if rows:
        connection.executemany(query, rows)
    return count


def _populate_one_source(
    connection: sqlite3.Connection,
    run_id: str,
    adapter: object,
    *,
    selected_asset_ids: set[str] | None,
    batch_size: int,
) -> dict[str, int]:
    if selected_asset_ids is None:
        projects = adapter.iter_projects()
        buildings = adapter.iter_buildings()
        memberships = adapter.iter_memberships()
        occurrences: Iterable[SourceOccurrence] = adapter.iter_occurrences()
        project_count = building_count = 0
        for project in projects:
            _insert_project(connection, run_id, project)
            project_count += 1
        for building in buildings:
            _insert_building(connection, run_id, building)
            building_count += 1
        membership_count = _insert_memberships(connection, run_id, memberships)
    else:
        occurrence_rows = [
            occurrence
            for occurrence in adapter.iter_occurrences()
            if occurrence.source_asset_id in selected_asset_ids
        ]
        project_ids = {row.source_project_id for row in occurrence_rows}
        membership_rows = [
            membership
            for membership in adapter.iter_memberships()
            if membership.source_project_id in project_ids
        ]
        building_ids = {row.source_building_id for row in membership_rows}
        project_rows = [
            project
            for project in adapter.iter_projects()
            if project.source_project_id in project_ids
        ]
        building_rows = [
            building
            for building in adapter.iter_buildings()
            if building.source_building_id in building_ids
        ]
        for project in project_rows:
            _insert_project(connection, run_id, project)
        for building in building_rows:
            _insert_building(connection, run_id, building)
        membership_count = _insert_memberships(connection, run_id, membership_rows)
        projects = project_rows
        buildings = building_rows
        occurrences = occurrence_rows
        project_count = len(project_rows)
        building_count = len(building_rows)

    asset_count = _insert_assets(
        connection,
        run_id,
        adapter.iter_assets(),
        included_ids=selected_asset_ids,
        batch_size=batch_size,
    )
    occurrence_count = _insert_occurrences(
        connection,
        run_id,
        occurrences,
        included_asset_ids=selected_asset_ids,
        batch_size=batch_size,
    )
    connection.commit()
    return {
        "assets": asset_count,
        "buildings": building_count,
        "memberships": membership_count,
        "occurrences": occurrence_count,
        "projects": project_count,
    }


def _checkpoint(
    connection: sqlite3.Connection,
    run_id: str,
    phase: str,
    completed_rows: int,
    *,
    complete: bool = True,
    cursor: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO build_checkpoints(
          run_id,phase,cursor_json,completed_rows,phase_complete,updated_at
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(run_id,phase) DO UPDATE SET
          cursor_json=excluded.cursor_json,
          completed_rows=excluded.completed_rows,
          phase_complete=excluded.phase_complete,
          updated_at=excluded.updated_at
        """,
        (
            run_id,
            phase,
            canonical_json(cursor or {}),
            completed_rows,
            int(complete),
            _utc_now(),
        ),
    )
    connection.commit()


def _selection_manifest_from_assets(
    connection: sqlite3.Connection, run_id: str
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(
        """
        SELECT source,source_asset_id,fingerprint_status
        FROM assets WHERE run_id=? ORDER BY source,source_asset_id
        """,
        (run_id,),
    ):
        record = canonical_json(
            {"asset_id": str(row[1]), "source": str(row[0]), "status": str(row[2])}
        )
        digest.update(record.encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _flush_project_asset(
    connection: sqlite3.Connection,
    run_id: str,
    key: tuple[str, str, str],
    occurrence_count: int,
    roles: set[str],
    first_ordinal: int | None,
) -> None:
    source, project_id, asset_id = key
    record = {
        "asset_id": asset_id,
        "first_ordinal": first_ordinal,
        "occurrence_count": occurrence_count,
        "project_id": project_id,
        "roles": sorted(roles),
        "source": source,
    }
    connection.execute(
        """
        INSERT OR REPLACE INTO project_assets(
          run_id,source,source_project_id,source_asset_id,occurrence_count,
          roles_json,first_ordinal,relation_record_sha256
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            source,
            project_id,
            asset_id,
            occurrence_count,
            canonical_json(sorted(roles)),
            first_ordinal,
            canonical_sha256(record),
        ),
    )


def _materialize_project_assets(
    connection: sqlite3.Connection, run_id: str, batch_size: int
) -> int:
    cursor = connection.execute(
        """
        SELECT source,source_project_id,source_asset_id,role,ordinal
        FROM project_asset_occurrences
        WHERE run_id=? AND resolution_status='linked'
        ORDER BY source,source_project_id,source_asset_id,role,ordinal
        """,
        (run_id,),
    )
    current: tuple[str, str, str] | None = None
    count = occurrence_count = 0
    roles: set[str] = set()
    first_ordinal: int | None = None
    for row in cursor:
        key = (str(row[0]), str(row[1]), str(row[2]))
        if current is not None and key != current:
            _flush_project_asset(
                connection, run_id, current, occurrence_count, roles, first_ordinal
            )
            count += 1
            if count % batch_size == 0:
                connection.commit()
        if key != current:
            current = key
            occurrence_count = 0
            roles = set()
            first_ordinal = None
        occurrence_count += 1
        roles.add(str(row[3]))
        ordinal = int(row[4]) if row[4] is not None else None
        if ordinal is not None and (first_ordinal is None or ordinal < first_ordinal):
            first_ordinal = ordinal
    if current is not None:
        _flush_project_asset(
            connection, run_id, current, occurrence_count, roles, first_ordinal
        )
        count += 1
    connection.commit()
    return count


def _flush_building_asset(
    connection: sqlite3.Connection,
    run_id: str,
    key: tuple[str, str, str],
    project_ids: set[str],
    occurrence_count: int,
    roles: set[str],
) -> None:
    source, building_id, asset_id = key
    record = {
        "asset_id": asset_id,
        "building_id": building_id,
        "occurrence_count": occurrence_count,
        "project_count": len(project_ids),
        "projects": sorted(project_ids),
        "roles": sorted(roles),
        "source": source,
    }
    connection.execute(
        """
        INSERT OR REPLACE INTO building_assets(
          run_id,source,source_building_id,source_asset_id,project_count,
          occurrence_count,roles_json,relation_record_sha256
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            source,
            building_id,
            asset_id,
            len(project_ids),
            occurrence_count,
            canonical_json(sorted(roles)),
            canonical_sha256(record),
        ),
    )


def _materialize_building_assets(
    connection: sqlite3.Connection, run_id: str, batch_size: int
) -> int:
    cursor = connection.execute(
        """
        SELECT m.source,m.source_building_id,p.source_asset_id,
               p.source_project_id,p.occurrence_count,p.roles_json
        FROM source_project_buildings m
        JOIN project_assets p
          ON p.run_id=m.run_id AND p.source=m.source
         AND p.source_project_id=m.source_project_id
        WHERE m.run_id=?
        ORDER BY m.source,m.source_building_id,p.source_asset_id,p.source_project_id
        """,
        (run_id,),
    )
    current: tuple[str, str, str] | None = None
    count = occurrence_count = 0
    project_ids: set[str] = set()
    roles: set[str] = set()
    for row in cursor:
        key = (str(row[0]), str(row[1]), str(row[2]))
        if current is not None and key != current:
            _flush_building_asset(
                connection, run_id, current, project_ids, occurrence_count, roles
            )
            count += 1
            if count % batch_size == 0:
                connection.commit()
        if key != current:
            current = key
            occurrence_count = 0
            project_ids = set()
            roles = set()
        project_ids.add(str(row[3]))
        occurrence_count += int(row[4])
        parsed_roles = json.loads(str(row[5]))
        roles.update(str(role) for role in parsed_roles)
    if current is not None:
        _flush_building_asset(
            connection, run_id, current, project_ids, occurrence_count, roles
        )
        count += 1
    connection.commit()
    return count


def _flush_exact_cluster(
    connection: sqlite3.Connection,
    run_id: str,
    pixel_sha: str,
    members: list[tuple[str, str]],
) -> bool:
    if len(members) < 2:
        return False
    cluster_id = stable_exact_id(pixel_sha)
    sources = {source for source, _ in members}
    connection.execute(
        """
        INSERT OR REPLACE INTO exact_pixel_clusters(
          run_id,cluster_id,normalized_pixel_sha256,member_count,source_count,
          project_count,building_count,is_cross_source
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            cluster_id,
            pixel_sha,
            len(members),
            len(sources),
            0,
            0,
            int(len(sources) == 2),
        ),
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO exact_pixel_cluster_members(
          run_id,cluster_id,source,source_asset_id
        ) VALUES(?,?,?,?)
        """,
        [(run_id, cluster_id, source, asset_id) for source, asset_id in members],
    )
    return True


def _materialize_exact_clusters(
    connection: sqlite3.Connection, run_id: str, batch_size: int
) -> tuple[int, int]:
    cursor = connection.execute(
        """
        SELECT normalized_pixel_sha256,source,source_asset_id
        FROM assets
        WHERE run_id=? AND fingerprint_status='success'
          AND normalized_pixel_sha256 IS NOT NULL
        ORDER BY normalized_pixel_sha256,source,source_asset_id
        """,
        (run_id,),
    )
    current_sha: str | None = None
    members: list[tuple[str, str]] = []
    cluster_count = member_count = 0
    for row in cursor:
        pixel_sha = str(row[0])
        if current_sha is not None and pixel_sha != current_sha:
            if _flush_exact_cluster(connection, run_id, current_sha, members):
                cluster_count += 1
                member_count += len(members)
                if cluster_count % batch_size == 0:
                    connection.commit()
            members = []
        current_sha = pixel_sha
        members.append((str(row[1]), str(row[2])))
    if current_sha is not None and _flush_exact_cluster(
        connection, run_id, current_sha, members
    ):
        cluster_count += 1
        member_count += len(members)
    # CROSS JOIN fixes the loop order at the small duplicate-member ledger and
    # probes the relation indexes once per member.  A correlated UPDATE lets
    # SQLite choose the reverse plan (scan ~1.4M relations per cluster).
    connection.executescript(
        """
        CREATE TEMP TABLE IF NOT EXISTS e2_work_exact_project_counts (
          cluster_id TEXT PRIMARY KEY,
          relation_count INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TEMP TABLE IF NOT EXISTS e2_work_exact_building_counts (
          cluster_id TEXT PRIMARY KEY,
          relation_count INTEGER NOT NULL
        ) WITHOUT ROWID;
        DELETE FROM e2_work_exact_project_counts;
        DELETE FROM e2_work_exact_building_counts;
        """
    )
    connection.execute(
        """
        INSERT INTO e2_work_exact_project_counts(cluster_id,relation_count)
        SELECT cluster_id,count(*) FROM (
          SELECT DISTINCT m.cluster_id,p.source,p.source_project_id
          FROM exact_pixel_cluster_members m
          CROSS JOIN project_assets p INDEXED BY idx_project_assets_asset
          WHERE m.run_id=? AND p.run_id=m.run_id AND p.source=m.source
            AND p.source_asset_id=m.source_asset_id
        ) GROUP BY cluster_id
        """,
        (run_id,),
    )
    connection.execute(
        """
        INSERT INTO e2_work_exact_building_counts(cluster_id,relation_count)
        SELECT cluster_id,count(*) FROM (
          SELECT DISTINCT m.cluster_id,b.source,b.source_building_id
          FROM exact_pixel_cluster_members m
          CROSS JOIN building_assets b INDEXED BY idx_building_assets_asset
          WHERE m.run_id=? AND b.run_id=m.run_id AND b.source=m.source
            AND b.source_asset_id=m.source_asset_id
        ) GROUP BY cluster_id
        """,
        (run_id,),
    )
    connection.execute(
        """
        UPDATE exact_pixel_clusters AS c
        SET project_count=coalesce((
              SELECT relation_count FROM e2_work_exact_project_counts p
              WHERE p.cluster_id=c.cluster_id
            ),0),
            building_count=coalesce((
              SELECT relation_count FROM e2_work_exact_building_counts b
              WHERE b.cluster_id=c.cluster_id
            ),0)
        WHERE c.run_id=?
        """,
        (run_id,),
    )
    connection.commit()
    return cluster_count, member_count


def _flush_phash_node(
    connection: sqlite3.Connection,
    run_id: str,
    phash: str,
    members: list[tuple[str, str]],
) -> None:
    node_id = stable_phash_id(phash)
    sources = {source for source, _ in members}
    connection.execute(
        """
        INSERT OR REPLACE INTO phash_nodes(
          run_id,node_id,phash_hex,member_count,source_count,is_cross_source
        ) VALUES(?,?,?,?,?,?)
        """,
        (run_id, node_id, phash, len(members), len(sources), int(len(sources) == 2)),
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO phash_node_members(
          run_id,node_id,source,source_asset_id
        ) VALUES(?,?,?,?)
        """,
        [(run_id, node_id, source, asset_id) for source, asset_id in members],
    )


def _materialize_phash_nodes(
    connection: sqlite3.Connection, run_id: str, batch_size: int
) -> tuple[int, int]:
    cursor = connection.execute(
        """
        SELECT phash_hex,source,source_asset_id
        FROM assets
        WHERE run_id=? AND fingerprint_status='success'
          AND phash_hex IS NOT NULL
        ORDER BY phash_hex,source,source_asset_id
        """,
        (run_id,),
    )
    current_phash: str | None = None
    members: list[tuple[str, str]] = []
    node_count = member_count = 0
    for row in cursor:
        phash = str(row[0])
        if current_phash is not None and phash != current_phash:
            _flush_phash_node(connection, run_id, current_phash, members)
            node_count += 1
            member_count += len(members)
            if node_count % batch_size == 0:
                connection.commit()
            members = []
        current_phash = phash
        members.append((str(row[1]), str(row[2])))
    if current_phash is not None:
        _flush_phash_node(connection, run_id, current_phash, members)
        node_count += 1
        member_count += len(members)
    connection.commit()
    return node_count, member_count


def _build_global_phash_candidates(
    connection: sqlite3.Connection,
    run_id: str,
    batch_size: int,
) -> tuple[int, int, int]:
    connection.executescript(
        """
        CREATE TEMP TABLE IF NOT EXISTS e2_work_band_members (
          band_key TEXT NOT NULL,
          node_id TEXT NOT NULL,
          PRIMARY KEY(band_key,node_id)
        ) WITHOUT ROWID;
        CREATE TEMP TABLE IF NOT EXISTS e2_work_phash_pairs (
          left_node_id TEXT NOT NULL,
          right_node_id TEXT NOT NULL,
          band_mask INTEGER NOT NULL,
          shared_band_count INTEGER NOT NULL,
          PRIMARY KEY(left_node_id,right_node_id),
          CHECK(left_node_id<right_node_id)
        ) WITHOUT ROWID;
        DELETE FROM e2_work_band_members;
        DELETE FROM e2_work_phash_pairs;
        """
    )
    connection.commit()
    for band_index in range(PHASH_BAND_COUNT):
        connection.execute("DELETE FROM e2_work_band_members")
        digest = hashlib.sha256()
        inserted = 0
        rows: list[tuple[str, str]] = []
        for row in connection.execute(
            "SELECT node_id,phash_hex FROM phash_nodes WHERE run_id=? ORDER BY node_id",
            (run_id,),
        ):
            node_id = str(row[0])
            key = phash_band_key(str(row[1]), band_index)
            rows.append((key, node_id))
            digest.update(f"{key}\0{node_id}\n".encode("ascii"))
            inserted += 1
            if len(rows) >= batch_size:
                connection.executemany(
                    "INSERT INTO e2_work_band_members(band_key,node_id) VALUES(?,?)",
                    rows,
                )
                rows.clear()
        if rows:
            connection.executemany(
                "INSERT INTO e2_work_band_members(band_key,node_id) VALUES(?,?)", rows
            )
        max_bucket = int(
            connection.execute(
                """
                SELECT coalesce(max(n),0) FROM (
                  SELECT count(*) AS n FROM e2_work_band_members GROUP BY band_key
                )
                """
            ).fetchone()[0]
        )
        if max_bucket > 10_000:
            raise RuntimeError(
                f"pHash band {band_index} bucket {max_bucket} exceeds safety limit"
            )
        band_mask = 1 << band_index
        connection.execute(
            """
            INSERT INTO e2_work_phash_pairs(
              left_node_id,right_node_id,band_mask,shared_band_count
            )
            SELECT a.node_id,b.node_id,?,1
            FROM e2_work_band_members a
            JOIN e2_work_band_members b
              ON b.band_key=a.band_key AND a.node_id<b.node_id
            WHERE 1
            ON CONFLICT(left_node_id,right_node_id) DO UPDATE SET
              band_mask=e2_work_phash_pairs.band_mask | excluded.band_mask,
              shared_band_count=e2_work_phash_pairs.shared_band_count+1
            """,
            (band_mask,),
        )
        _checkpoint(
            connection,
            run_id,
            f"phash_band_{band_index}",
            inserted,
            cursor={
                "band_index": band_index,
                "band_manifest_sha256": digest.hexdigest(),
                "max_bucket_size": max_bucket,
            },
        )

    candidate_count = edge_count = rejected_count = 0
    candidate_rows: list[tuple[object, ...]] = []
    edge_rows: list[tuple[object, ...]] = []
    candidate_query = """
        INSERT OR REPLACE INTO phash_candidates(
          run_id,candidate_id,left_node_id,right_node_id,candidate_scope,
          shared_band_count,recomputed_distance,passed_threshold,
          candidate_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
    """
    edge_query = """
        INSERT OR REPLACE INTO phash_edges(
          run_id,edge_id,left_node_id,right_node_id,hamming_distance,
          edge_scope,candidate_id,edge_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?)
    """
    cursor = connection.execute(
        """
        SELECT p.left_node_id,p.right_node_id,p.band_mask,p.shared_band_count,
               l.phash_hex,r.phash_hex
        FROM e2_work_phash_pairs p
        CROSS JOIN phash_nodes l
        CROSS JOIN phash_nodes r
        WHERE l.run_id=? AND l.node_id=p.left_node_id
          AND r.run_id=? AND r.node_id=p.right_node_id
        ORDER BY p.left_node_id,p.right_node_id
        """,
        (run_id, run_id),
    )
    for row in cursor:
        left_node = str(row[0])
        right_node = str(row[1])
        decision = classify_phash_pair(str(row[4]), str(row[5]))
        candidate_id = stable_edge_id(
            left_node, right_node, "phash-global-le8-candidate"
        )
        detail = {
            "band_mask": int(row[2]),
            "band_version": PHASH_BAND_VERSION,
            "reason_code": decision.reason_code,
        }
        candidate_record = {
            "candidate_id": candidate_id,
            "distance": decision.distance,
            "left_node_id": left_node,
            "right_node_id": right_node,
            "scope": "global_le8",
            "shared_band_count": int(row[3]),
        }
        candidate_rows.append(
            (
                run_id,
                candidate_id,
                left_node,
                right_node,
                "global_le8",
                int(row[3]),
                decision.distance,
                int(decision.distance <= 8),
                canonical_sha256(candidate_record),
                canonical_json(detail),
            )
        )
        candidate_count += 1
        if 1 <= decision.distance <= 8:
            edge_id = stable_edge_id(left_node, right_node, "phash-global-le8")
            edge_record = {
                "candidate_id": candidate_id,
                "distance": decision.distance,
                "edge_id": edge_id,
                "left_node_id": left_node,
                "right_node_id": right_node,
                "scope": "global_le8",
            }
            edge_rows.append(
                (
                    run_id,
                    edge_id,
                    left_node,
                    right_node,
                    decision.distance,
                    "global_le8",
                    candidate_id,
                    canonical_sha256(edge_record),
                    canonical_json({"direct_edge": True, **detail}),
                )
            )
            edge_count += 1
        else:
            rejected_count += 1
        if len(candidate_rows) >= batch_size:
            connection.executemany(candidate_query, candidate_rows)
            candidate_rows.clear()
        if len(edge_rows) >= batch_size:
            # Every edge has a foreign key to its candidate.  With a dense
            # passing set the edge buffer can fill before the next candidate
            # batch boundary, so flush the remaining parent rows first.
            if candidate_rows:
                connection.executemany(candidate_query, candidate_rows)
                candidate_rows.clear()
            connection.executemany(edge_query, edge_rows)
            edge_rows.clear()
        if candidate_count % (batch_size * 5) == 0:
            connection.commit()
    if candidate_rows:
        connection.executemany(candidate_query, candidate_rows)
    if edge_rows:
        connection.executemany(edge_query, edge_rows)
    connection.commit()
    return candidate_count, edge_count, rejected_count


def _pair_id(prefix: str, left_id: str, right_id: str) -> str:
    return prefix + canonical_sha256(
        {"left": left_id, "right": right_id, "version": E2_EVIDENCE_VERSION}
    )


def _ensure_building_candidate(
    connection: sqlite3.Connection,
    run_id: str,
    div_building_id: str,
    arch_building_id: str,
    *,
    metadata_pair_id: str | None,
    initial_evidence_kind: str | None = None,
) -> str:
    candidate_id = _pair_id("e2bc_", div_building_id, arch_building_id)
    initial = {
        "architizer_building_id": arch_building_id,
        "candidate_id": candidate_id,
        "divisare_building_id": div_building_id,
        "metadata_pair_id": metadata_pair_id,
        "initial_evidence_kind": initial_evidence_kind,
    }
    initial_counts = {
        "exact_pixel": 0,
        "identical_phash": 0,
        "phash_le8": 0,
        "phash_9_16": 0,
    }
    if initial_evidence_kind is not None:
        initial_counts[initial_evidence_kind] = 1
    connection.execute(
        """
        INSERT OR IGNORE INTO cross_source_building_candidates(
          run_id,building_candidate_id,left_source,left_source_building_id,
          right_source,right_source_building_id,metadata_pair_id,
          exact_asset_pair_count,identical_phash_pair_count,
          phash_le8_pair_count,phash_9_16_pair_count,
          discovery_basis_json,candidate_record_sha256
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            candidate_id,
            "divisare",
            div_building_id,
            "architizer",
            arch_building_id,
            metadata_pair_id,
            initial_counts["exact_pixel"],
            initial_counts["identical_phash"],
            initial_counts["phash_le8"],
            initial_counts["phash_9_16"],
            canonical_json(
                {
                    "evidence_only": True,
                    "metadata_pair": metadata_pair_id is not None,
                }
            ),
            canonical_sha256(initial),
        ),
    )
    if metadata_pair_id is not None:
        connection.execute(
            """
            UPDATE cross_source_building_candidates
            SET metadata_pair_id=coalesce(metadata_pair_id,?)
            WHERE run_id=? AND building_candidate_id=?
            """,
            (metadata_pair_id, run_id, candidate_id),
        )
    return candidate_id


def _materialize_metadata_pairs(
    connection: sqlite3.Connection,
    run_id: str,
    batch_size: int,
) -> int:
    count = 0
    cursor = connection.execute(
        """
        SELECT d.source_building_id,d.normalized_name,d.country,d.locality,
               d.completion_year_min,d.completion_year_max,
               a.source_building_id,a.normalized_name,a.country,a.locality,
               a.completion_year_min,a.completion_year_max
        FROM source_buildings d INDEXED BY idx_source_buildings_name
        CROSS JOIN source_buildings a INDEXED BY idx_source_buildings_name
        WHERE d.run_id=? AND d.source='divisare'
          AND a.run_id=d.run_id AND a.source='architizer'
          AND a.normalized_name=d.normalized_name
          AND length(d.normalized_name)>0
          AND EXISTS(
            SELECT 1 FROM source_project_buildings dm
            WHERE dm.run_id=d.run_id AND dm.source=d.source
              AND dm.source_building_id=d.source_building_id
          )
          AND EXISTS(
            SELECT 1 FROM source_project_buildings am
            WHERE am.run_id=a.run_id AND am.source=a.source
              AND am.source_building_id=a.source_building_id
          )
        ORDER BY d.source_building_id,a.source_building_id
        """,
        (run_id,),
    )
    for row in cursor:
        div_id = str(row[0])
        arch_id = str(row[6])
        metadata_pair_id = _pair_id("e2mp_", div_id, arch_id)
        country_equal = int(
            bool(normalize_block_text(row[2]) and normalize_block_text(row[8]))
            and normalize_block_text(row[2]) == normalize_block_text(row[8])
        )
        locality_equal = int(
            bool(normalize_block_text(row[3]) and normalize_block_text(row[9]))
            and normalize_block_text(row[3]) == normalize_block_text(row[9])
        )
        div_year = int(row[4]) if row[4] is not None else None
        arch_year = int(row[10]) if row[10] is not None else None
        year_overlap = int(
            div_year is not None and arch_year is not None and div_year == arch_year
        )
        evidence = {
            "architizer_name_key": str(row[7]),
            "architizer_year": arch_year,
            "country_equal": bool(country_equal),
            "divisare_name_key": str(row[1]),
            "divisare_year": div_year,
            "locality_equal": bool(locality_equal),
            "no_identity_decision": True,
            "year_distance": (
                abs(div_year - arch_year)
                if div_year is not None and arch_year is not None
                else None
            ),
        }
        record = {
            "architizer_building_id": arch_id,
            "blocker_version": METADATA_NORMALIZATION_VERSION,
            "discovery_reason": "exact_conservative_normalized_name",
            "divisare_building_id": div_id,
            "evidence": evidence,
            "metadata_pair_id": metadata_pair_id,
        }
        connection.execute(
            """
            INSERT OR IGNORE INTO metadata_building_pairs(
              run_id,metadata_pair_id,left_source,left_source_building_id,
              right_source,right_source_building_id,blocker_version,
              discovery_reason,normalized_name_equal,country_equal,
              locality_equal,year_overlap,metadata_record_sha256,evidence_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                metadata_pair_id,
                "divisare",
                div_id,
                "architizer",
                arch_id,
                METADATA_NORMALIZATION_VERSION,
                "exact_conservative_normalized_name",
                1,
                country_equal,
                locality_equal,
                year_overlap,
                canonical_sha256(record),
                canonical_json(evidence),
            ),
        )
        _ensure_building_candidate(
            connection,
            run_id,
            div_id,
            arch_id,
            metadata_pair_id=metadata_pair_id,
        )
        count += 1
        if count % batch_size == 0:
            connection.commit()
    connection.commit()
    return count


def _evidence_id(
    building_candidate_id: str,
    div_asset_id: str,
    arch_asset_id: str,
    evidence_kind: str,
) -> str:
    return "e2ie_" + canonical_sha256(
        {
            "architizer_asset_id": arch_asset_id,
            "building_candidate_id": building_candidate_id,
            "divisare_asset_id": div_asset_id,
            "evidence_kind": evidence_kind,
            "version": E2_EVIDENCE_VERSION,
        }
    )


def _insert_candidate_image_evidence(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    div_building_id: str,
    arch_building_id: str,
    div_asset_id: str,
    arch_asset_id: str,
    evidence_kind: str,
    exact_cluster_id: str | None = None,
    phash_edge_id: str | None = None,
    phash_distance: int | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    candidate_id = _ensure_building_candidate(
        connection,
        run_id,
        div_building_id,
        arch_building_id,
        metadata_pair_id=None,
        initial_evidence_kind=evidence_kind,
    )
    evidence_id = _evidence_id(
        candidate_id, div_asset_id, arch_asset_id, evidence_kind
    )
    record = {
        "architizer_asset_id": arch_asset_id,
        "building_candidate_id": candidate_id,
        "divisare_asset_id": div_asset_id,
        "evidence_id": evidence_id,
        "evidence_kind": evidence_kind,
        "exact_cluster_id": exact_cluster_id,
        "phash_distance": phash_distance,
        "phash_edge_id": phash_edge_id,
    }
    connection.execute(
        """
        INSERT OR IGNORE INTO candidate_image_evidence(
          run_id,evidence_id,building_candidate_id,project_pair_id,
          left_source,left_source_asset_id,right_source,right_source_asset_id,
          evidence_kind,exact_cluster_id,phash_edge_id,phash_distance,
          direct_evidence,evidence_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            evidence_id,
            candidate_id,
            None,
            "divisare",
            div_asset_id,
            "architizer",
            arch_asset_id,
            evidence_kind,
            exact_cluster_id,
            phash_edge_id,
            phash_distance,
            1,
            canonical_sha256(record),
            canonical_json(detail or {}),
        ),
    )


def _materialize_global_image_evidence(
    connection: sqlite3.Connection,
    run_id: str,
    batch_size: int,
) -> dict[str, int]:
    counts = {"exact_pixel": 0, "identical_phash": 0, "phash_le8": 0}
    queries = (
        (
            "exact_pixel",
            """
            SELECT db.source_building_id,ab.source_building_id,
                   dm.source_asset_id,am.source_asset_id,dm.cluster_id,
                   NULL,NULL,da.provenance_json,aa.provenance_json
            FROM exact_pixel_cluster_members dm INDEXED BY idx_exact_members_asset
            CROSS JOIN exact_pixel_cluster_members am
              INDEXED BY sqlite_autoindex_exact_pixel_cluster_members_1
            CROSS JOIN building_assets db INDEXED BY idx_building_assets_asset
            CROSS JOIN building_assets ab INDEXED BY idx_building_assets_asset
            CROSS JOIN assets da
            CROSS JOIN assets aa
            WHERE dm.run_id=? AND dm.source='divisare'
              AND am.run_id=dm.run_id AND am.cluster_id=dm.cluster_id
              AND am.source='architizer'
              AND db.run_id=dm.run_id AND db.source=dm.source
              AND db.source_asset_id=dm.source_asset_id
              AND ab.run_id=am.run_id AND ab.source=am.source
              AND ab.source_asset_id=am.source_asset_id
              AND da.run_id=dm.run_id AND da.source=dm.source
              AND da.source_asset_id=dm.source_asset_id
              AND aa.run_id=am.run_id AND aa.source=am.source
              AND aa.source_asset_id=am.source_asset_id
            """,
        ),
        (
            "identical_phash",
            """
            SELECT db.source_building_id,ab.source_building_id,
                   dm.source_asset_id,am.source_asset_id,NULL,NULL,0,
                   da.provenance_json,aa.provenance_json
            FROM phash_node_members dm INDEXED BY idx_phash_members_asset
            CROSS JOIN phash_node_members am
              INDEXED BY sqlite_autoindex_phash_node_members_1
            CROSS JOIN building_assets db INDEXED BY idx_building_assets_asset
            CROSS JOIN building_assets ab INDEXED BY idx_building_assets_asset
            CROSS JOIN assets da
            CROSS JOIN assets aa
            WHERE dm.run_id=? AND dm.source='divisare'
              AND am.run_id=dm.run_id AND am.node_id=dm.node_id
              AND am.source='architizer'
              AND db.run_id=dm.run_id AND db.source=dm.source
              AND db.source_asset_id=dm.source_asset_id
              AND ab.run_id=am.run_id AND ab.source=am.source
              AND ab.source_asset_id=am.source_asset_id
              AND da.run_id=dm.run_id AND da.source=dm.source
              AND da.source_asset_id=dm.source_asset_id
              AND aa.run_id=am.run_id AND aa.source=am.source
              AND aa.source_asset_id=am.source_asset_id
            """,
        ),
        (
            "phash_le8",
            """
            SELECT db.source_building_id,ab.source_building_id,
                   dm.source_asset_id,am.source_asset_id,NULL,e.edge_id,
                   e.hamming_distance,da.provenance_json,aa.provenance_json
            FROM phash_edges e INDEXED BY idx_phash_edges_distance
            CROSS JOIN phash_node_members dm
              INDEXED BY sqlite_autoindex_phash_node_members_1
            CROSS JOIN phash_node_members am
              INDEXED BY sqlite_autoindex_phash_node_members_1
            CROSS JOIN building_assets db INDEXED BY idx_building_assets_asset
            CROSS JOIN building_assets ab INDEXED BY idx_building_assets_asset
            CROSS JOIN assets da
            CROSS JOIN assets aa
            WHERE e.run_id=? AND e.edge_scope='global_le8'
              AND e.hamming_distance BETWEEN 1 AND 8
              AND dm.run_id=e.run_id
              AND dm.node_id IN (e.left_node_id,e.right_node_id)
              AND dm.source='divisare'
              AND am.run_id=e.run_id
              AND am.node_id IN (e.left_node_id,e.right_node_id)
              AND am.source='architizer' AND am.node_id<>dm.node_id
              AND db.run_id=dm.run_id AND db.source=dm.source
              AND db.source_asset_id=dm.source_asset_id
              AND ab.run_id=am.run_id AND ab.source=am.source
              AND ab.source_asset_id=am.source_asset_id
              AND da.run_id=dm.run_id AND da.source=dm.source
              AND da.source_asset_id=dm.source_asset_id
              AND aa.run_id=am.run_id AND aa.source=am.source
              AND aa.source_asset_id=am.source_asset_id
            """,
        ),
    )
    for kind, query in queries:
        for row in connection.execute(query, (run_id,)):
            qa_only = "low_information" in str(row[7]) or "low_information" in str(
                row[8]
            )
            _insert_candidate_image_evidence(
                connection,
                run_id,
                div_building_id=str(row[0]),
                arch_building_id=str(row[1]),
                div_asset_id=str(row[2]),
                arch_asset_id=str(row[3]),
                evidence_kind=kind,
                exact_cluster_id=str(row[4]) if row[4] is not None else None,
                phash_edge_id=str(row[5]) if row[5] is not None else None,
                phash_distance=int(row[6]) if row[6] is not None else None,
                detail={"qa_only": qa_only, "reason": "low_information" if qa_only else None},
            )
            counts[kind] += 1
            if sum(counts.values()) % batch_size == 0:
                connection.commit()
    connection.commit()
    return counts


def _building_phash_assets(
    connection: sqlite3.Connection,
    run_id: str,
    source: str,
    building_id: str,
) -> dict[str, tuple[str, list[tuple[str, bool]]]]:
    result: dict[str, tuple[str, list[tuple[str, bool]]]] = {}
    for row in connection.execute(
        """
        SELECT n.node_id,n.phash_hex,b.source_asset_id,a.provenance_json
        FROM building_assets b
        JOIN phash_node_members m
          ON m.run_id=b.run_id AND m.source=b.source
         AND m.source_asset_id=b.source_asset_id
        JOIN phash_nodes n ON n.run_id=m.run_id AND n.node_id=m.node_id
        JOIN assets a ON a.run_id=b.run_id AND a.source=b.source
                     AND a.source_asset_id=b.source_asset_id
        WHERE b.run_id=? AND b.source=? AND b.source_building_id=?
        ORDER BY n.node_id,b.source_asset_id
        """,
        (run_id, source, building_id),
    ):
        node_id = str(row[0])
        phash = str(row[1])
        asset = (str(row[2]), "low_information" in str(row[3]))
        if node_id not in result:
            result[node_id] = (phash, [])
        result[node_id][1].append(asset)
    return result


def _materialize_metadata_phash_review(
    connection: sqlite3.Connection,
    run_id: str,
    batch_size: int,
) -> dict[str, int]:
    counts = {
        "asset_evidence_9_16": 0,
        "compared_node_pairs": 0,
        "identical_node_pairs": 0,
        "metadata_candidates_distinct": 0,
        "metadata_edges_9_16_distinct": 0,
    }
    candidate_before = int(
        connection.execute(
            "SELECT count(*) FROM phash_candidates WHERE run_id=? AND candidate_scope='metadata_le16'",
            (run_id,),
        ).fetchone()[0]
    )
    edge_before = int(
        connection.execute(
            "SELECT count(*) FROM phash_edges WHERE run_id=? AND edge_scope='metadata_9_16'",
            (run_id,),
        ).fetchone()[0]
    )
    pairs = connection.execute(
        """
        SELECT metadata_pair_id,left_source_building_id,right_source_building_id,
               evidence_json
        FROM metadata_building_pairs WHERE run_id=?
        ORDER BY left_source_building_id,right_source_building_id
        """,
        (run_id,),
    )
    for pair_index, pair in enumerate(pairs, 1):
        metadata_pair_id = str(pair[0])
        div_building_id = str(pair[1])
        arch_building_id = str(pair[2])
        div_nodes = _building_phash_assets(
            connection, run_id, "divisare", div_building_id
        )
        arch_nodes = _building_phash_assets(
            connection, run_id, "architizer", arch_building_id
        )
        pair_compared = pair_identical = pair_review = pair_above = 0
        for div_node_id, (div_phash, div_assets) in div_nodes.items():
            for arch_node_id, (arch_phash, arch_assets) in arch_nodes.items():
                if div_node_id == arch_node_id:
                    pair_identical += 1
                    counts["identical_node_pairs"] += 1
                    continue
                pair_compared += 1
                counts["compared_node_pairs"] += 1
                left_node, right_node = sorted((div_node_id, arch_node_id))
                decision = classify_phash_pair(
                    div_phash, arch_phash, metadata_blocked=True
                )
                candidate_id = stable_edge_id(
                    left_node, right_node, "phash-metadata-le16-candidate"
                )
                candidate_record = {
                    "candidate_id": candidate_id,
                    "distance": decision.distance,
                    "left_node_id": left_node,
                    "right_node_id": right_node,
                    "scope": "metadata_le16",
                }
                connection.execute(
                    """
                    INSERT OR IGNORE INTO phash_candidates(
                      run_id,candidate_id,left_node_id,right_node_id,
                      candidate_scope,shared_band_count,recomputed_distance,
                      passed_threshold,candidate_record_sha256,detail_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        candidate_id,
                        left_node,
                        right_node,
                        "metadata_le16",
                        0,
                        decision.distance,
                        int(decision.distance <= 16),
                        canonical_sha256(candidate_record),
                        canonical_json(
                            {
                                "comparison": "direct_cartesian_within_frozen_metadata_pair",
                                "pair_policy_version": PHASH_PAIR_POLICY_VERSION,
                            }
                        ),
                    ),
                )
                if 9 <= decision.distance <= 16:
                    pair_review += 1
                    edge_id = stable_edge_id(
                        left_node, right_node, "phash-metadata-9-16"
                    )
                    edge_record = {
                        "candidate_id": candidate_id,
                        "distance": decision.distance,
                        "edge_id": edge_id,
                        "left_node_id": left_node,
                        "right_node_id": right_node,
                        "scope": "metadata_9_16",
                    }
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO phash_edges(
                          run_id,edge_id,left_node_id,right_node_id,
                          hamming_distance,edge_scope,candidate_id,
                          edge_record_sha256,detail_json
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            edge_id,
                            left_node,
                            right_node,
                            decision.distance,
                            "metadata_9_16",
                            candidate_id,
                            canonical_sha256(edge_record),
                            canonical_json(
                                {
                                    "direct_edge": True,
                                    "metadata_blocked": True,
                                    "no_transitive_identity": True,
                                }
                            ),
                        ),
                    )
                    for div_asset_id, div_qa in div_assets:
                        for arch_asset_id, arch_qa in arch_assets:
                            _insert_candidate_image_evidence(
                                connection,
                                run_id,
                                div_building_id=div_building_id,
                                arch_building_id=arch_building_id,
                                div_asset_id=div_asset_id,
                                arch_asset_id=arch_asset_id,
                                evidence_kind="phash_9_16",
                                phash_edge_id=edge_id,
                                phash_distance=decision.distance,
                                detail={
                                    "metadata_pair_id": metadata_pair_id,
                                    "qa_only": bool(div_qa or arch_qa),
                                },
                            )
                            counts["asset_evidence_9_16"] += 1
                elif decision.distance > 16:
                    pair_above += 1

        evidence = json.loads(str(pair[3]))
        evidence["phash_cartesian_accounting"] = {
            "architizer_node_count": len(arch_nodes),
            "compared_distinct_node_pairs": pair_compared,
            "divisare_node_count": len(div_nodes),
            "distance_9_16_pairs": pair_review,
            "distance_above_16_pairs": pair_above,
            "identical_node_pairs": pair_identical,
            "total_cartesian_node_pairs": len(div_nodes) * len(arch_nodes),
        }
        record = {
            "architizer_building_id": arch_building_id,
            "blocker_version": METADATA_NORMALIZATION_VERSION,
            "divisare_building_id": div_building_id,
            "evidence": evidence,
            "metadata_pair_id": metadata_pair_id,
        }
        connection.execute(
            """
            UPDATE metadata_building_pairs
            SET evidence_json=?,metadata_record_sha256=?
            WHERE run_id=? AND metadata_pair_id=?
            """,
            (
                canonical_json(evidence),
                canonical_sha256(record),
                run_id,
                metadata_pair_id,
            ),
        )
        if pair_index % max(1, batch_size // 10) == 0:
            connection.commit()
    counts["metadata_candidates_distinct"] = int(
        connection.execute(
            "SELECT count(*) FROM phash_candidates WHERE run_id=? AND candidate_scope='metadata_le16'",
            (run_id,),
        ).fetchone()[0]
    ) - candidate_before
    counts["metadata_edges_9_16_distinct"] = int(
        connection.execute(
            "SELECT count(*) FROM phash_edges WHERE run_id=? AND edge_scope='metadata_9_16'",
            (run_id,),
        ).fetchone()[0]
    ) - edge_before
    connection.commit()
    return counts


def _finalize_building_candidates(
    connection: sqlite3.Connection, run_id: str, batch_size: int
) -> int:
    count = 0
    cursor = connection.execute(
        """
        SELECT c.building_candidate_id,c.left_source_building_id,
               c.right_source_building_id,c.metadata_pair_id,
               coalesce(sum(e.evidence_kind='exact_pixel'),0),
               coalesce(sum(e.evidence_kind='identical_phash'),0),
               coalesce(sum(e.evidence_kind='phash_le8'),0),
               coalesce(sum(e.evidence_kind='phash_9_16'),0),
               min(e.phash_distance),
               coalesce(sum(json_extract(e.detail_json,'$.qa_only')=1),0),
               count(e.evidence_id)
        FROM cross_source_building_candidates c
        LEFT JOIN candidate_image_evidence e
          ON e.run_id=c.run_id
         AND e.building_candidate_id=c.building_candidate_id
        WHERE c.run_id=?
        GROUP BY c.building_candidate_id,c.left_source_building_id,
                 c.right_source_building_id,c.metadata_pair_id
        ORDER BY c.building_candidate_id
        """,
        (run_id,),
    )
    for row in cursor:
        exact_count = int(row[4])
        identical_count = int(row[5])
        le8_count = int(row[6])
        review_count = int(row[7])
        evidence_total = int(row[10])
        basis = {
            "evidence_only": True,
            "has_exact_pixel": exact_count > 0,
            "has_identical_phash": identical_count > 0,
            "has_metadata_pair": row[3] is not None,
            "has_phash_9_16": review_count > 0,
            "has_phash_le8": le8_count > 0,
            "low_information_evidence_count": int(row[9]),
            "no_final_match_decision": True,
            "transitive_paths_not_counted": True,
        }
        record = {
            "architizer_building_id": str(row[2]),
            "candidate_id": str(row[0]),
            "counts": {
                "exact_pixel": exact_count,
                "identical_phash": identical_count,
                "phash_9_16": review_count,
                "phash_le8": le8_count,
            },
            "divisare_building_id": str(row[1]),
            "metadata_pair_id": str(row[3]) if row[3] is not None else None,
            "min_phash_distance": int(row[8]) if row[8] is not None else None,
        }
        if row[3] is None and evidence_total == 0:
            raise RuntimeError(f"candidate {row[0]} has neither metadata nor image evidence")
        connection.execute(
            """
            UPDATE cross_source_building_candidates
            SET exact_asset_pair_count=?,identical_phash_pair_count=?,
                phash_le8_pair_count=?,phash_9_16_pair_count=?,
                min_phash_distance=?,discovery_basis_json=?,
                candidate_record_sha256=?
            WHERE run_id=? AND building_candidate_id=?
            """,
            (
                exact_count,
                identical_count,
                le8_count,
                review_count,
                int(row[8]) if row[8] is not None else None,
                canonical_json(basis),
                canonical_sha256(record),
                run_id,
                str(row[0]),
            ),
        )
        count += 1
        if count % batch_size == 0:
            connection.commit()
    connection.commit()
    return count


def _materialize_project_image_evidence(
    connection: sqlite3.Connection, run_id: str, batch_size: int
) -> int:
    count = 0
    cursor = connection.execute(
        """
        WITH distinct_evidence AS MATERIALIZED (
          SELECT DISTINCT left_source_asset_id,right_source_asset_id,
                          evidence_kind,phash_distance
          FROM candidate_image_evidence
          WHERE run_id=?
        )
        SELECT d.source_project_id,a.source_project_id,
               sum(e.evidence_kind='exact_pixel'),
               sum(e.evidence_kind='identical_phash'),
               sum(e.evidence_kind='phash_le8'),
               sum(e.evidence_kind='phash_9_16'),
               min(e.phash_distance)
        FROM distinct_evidence e
        CROSS JOIN project_assets d INDEXED BY idx_project_assets_asset
        CROSS JOIN project_assets a INDEXED BY idx_project_assets_asset
        WHERE d.run_id=? AND d.source='divisare'
          AND d.source_asset_id=e.left_source_asset_id
          AND a.run_id=? AND a.source='architizer'
          AND a.source_asset_id=e.right_source_asset_id
        GROUP BY d.source_project_id,a.source_project_id
        """,
        (run_id, run_id, run_id),
    )
    for row in cursor:
        div_project = str(row[0])
        arch_project = str(row[1])
        project_pair_id = _pair_id("e2pp_", div_project, arch_project)
        counts = {
            "exact_pixel": int(row[2]),
            "identical_phash": int(row[3]),
            "phash_le8": int(row[4]),
            "phash_9_16": int(row[5]),
        }
        record = {
            "architizer_project_id": arch_project,
            "counts": counts,
            "divisare_project_id": div_project,
            "min_phash_distance": int(row[6]) if row[6] is not None else None,
            "project_pair_id": project_pair_id,
        }
        connection.execute(
            """
            INSERT OR REPLACE INTO cross_source_project_image_evidence(
              run_id,project_pair_id,left_source,left_source_project_id,
              right_source,right_source_project_id,exact_asset_pair_count,
              identical_phash_pair_count,phash_le8_pair_count,
              phash_9_16_pair_count,min_phash_distance,
              evidence_record_sha256,evidence_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                project_pair_id,
                "divisare",
                div_project,
                "architizer",
                arch_project,
                counts["exact_pixel"],
                counts["identical_phash"],
                counts["phash_le8"],
                counts["phash_9_16"],
                int(row[6]) if row[6] is not None else None,
                canonical_sha256(record),
                canonical_json(
                    {
                        "direct_asset_pairs_only": True,
                        "no_project_identity_decision": True,
                    }
                ),
            ),
        )
        count += 1
        if count % batch_size == 0:
            connection.commit()
    connection.commit()
    return count


LOGICAL_EVIDENCE_TABLES = (
    "source_projects",
    "source_buildings",
    "source_project_buildings",
    "assets",
    "project_asset_occurrences",
    "project_assets",
    "building_assets",
    "exact_pixel_clusters",
    "exact_pixel_cluster_members",
    "phash_nodes",
    "phash_node_members",
    "phash_candidates",
    "phash_edges",
    "metadata_building_pairs",
    "cross_source_project_image_evidence",
    "candidate_image_evidence",
    "cross_source_building_candidates",
    "smoke_manifests",
    "smoke_manifest_items",
)


def _table_primary_key_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(
        str(row[1])
        for row in sorted((row for row in rows if int(row[5]) > 0), key=lambda row: int(row[5]))
    )


def logical_evidence_manifest(
    connection: sqlite3.Connection,
    run_id: str,
) -> tuple[str, dict[str, dict[str, object]]]:
    table_manifests: dict[str, dict[str, object]] = {}
    for table in LOGICAL_EVIDENCE_TABLES:
        info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = [str(row[1]) for row in info]
        excluded = {"created_at", "recorded_at", "updated_at"}
        included = [column for column in columns if column not in excluded]
        primary_key = _table_primary_key_columns(connection, table)
        order = ",".join(f'"{column}"' for column in primary_key)
        selected = ",".join(f'"{column}"' for column in included)
        query = f'SELECT {selected} FROM "{table}" WHERE run_id=? ORDER BY {order}'
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(query, (run_id,)):
            value = {column: row[index] for index, column in enumerate(included)}
            digest.update(canonical_json(value).encode("utf-8"))
            digest.update(b"\n")
            count += 1
        table_manifests[table] = {"count": count, "sha256": digest.hexdigest()}
    return canonical_sha256(table_manifests), table_manifests


def _record_validation(
    connection: sqlite3.Connection,
    run_id: str,
    name: str,
    passed: bool,
    expected: object,
    actual: object,
    *,
    severity: str = "error",
    detail: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO e2_validations(
          run_id,validation_name,severity,passed,expected,actual,
          detail_json,recorded_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            name,
            severity,
            int(passed),
            canonical_json(expected),
            canonical_json(actual),
            canonical_json(detail or {}),
            _utc_now(),
        ),
    )


def _validate_edge_distances(
    connection: sqlite3.Connection, run_id: str
) -> tuple[int, int]:
    checked = mismatches = 0
    cursor = connection.execute(
        """
        SELECT e.hamming_distance,e.edge_scope,l.phash_hex,r.phash_hex
        FROM phash_edges e
        JOIN phash_nodes l ON l.run_id=e.run_id AND l.node_id=e.left_node_id
        JOIN phash_nodes r ON r.run_id=e.run_id AND r.node_id=e.right_node_id
        WHERE e.run_id=? ORDER BY e.edge_id
        """,
        (run_id,),
    )
    for row in cursor:
        decision = classify_phash_pair(
            str(row[2]),
            str(row[3]),
            metadata_blocked=str(row[1]) == "metadata_9_16",
        )
        expected_scope = (
            "global_le8" if 1 <= decision.distance <= 8 else "metadata_9_16"
        )
        mismatches += int(
            int(row[0]) != decision.distance or str(row[1]) != expected_scope
        )
        checked += 1
    return checked, mismatches


def _validate_metadata_cartesian(
    connection: sqlite3.Connection, run_id: str
) -> tuple[int, int]:
    checked = mismatches = 0
    for row in connection.execute(
        """
        SELECT evidence_json FROM metadata_building_pairs
        WHERE run_id=? ORDER BY metadata_pair_id
        """,
        (run_id,),
    ):
        evidence = json.loads(str(row[0]))
        accounting = evidence.get("phash_cartesian_accounting")
        checked += 1
        if not isinstance(accounting, dict):
            mismatches += 1
            continue
        left = int(accounting.get("divisare_node_count", -1))
        right = int(accounting.get("architizer_node_count", -1))
        compared = int(accounting.get("compared_distinct_node_pairs", -1))
        identical = int(accounting.get("identical_node_pairs", -1))
        total = int(accounting.get("total_cartesian_node_pairs", -1))
        mismatches += int(total != left * right or total != compared + identical)
    return checked, mismatches


def _validate_exact_relation_counts(
    connection: sqlite3.Connection, run_id: str
) -> tuple[int, int]:
    """Recompute exact-cluster project/building counts from raw relations.

    This check intentionally does not reuse the temporary count tables from
    materialization.  The fixed join order starts from the small exact-member
    ledger and probes the source-qualified asset indexes, avoiding the
    relation-table-per-cluster plan that is prohibitive at full scale.
    """

    row = connection.execute(
        """
        WITH project_aggregate AS (
          SELECT cluster_id,count(*) AS relation_count FROM (
            SELECT DISTINCT m.cluster_id,p.source,p.source_project_id
            FROM exact_pixel_cluster_members m
            CROSS JOIN project_assets p INDEXED BY idx_project_assets_asset
            WHERE m.run_id=? AND p.run_id=m.run_id AND p.source=m.source
              AND p.source_asset_id=m.source_asset_id
          ) GROUP BY cluster_id
        ), building_aggregate AS (
          SELECT cluster_id,count(*) AS relation_count FROM (
            SELECT DISTINCT m.cluster_id,b.source,b.source_building_id
            FROM exact_pixel_cluster_members m
            CROSS JOIN building_assets b INDEXED BY idx_building_assets_asset
            WHERE m.run_id=? AND b.run_id=m.run_id AND b.source=m.source
              AND b.source_asset_id=m.source_asset_id
          ) GROUP BY cluster_id
        )
        SELECT count(*),coalesce(sum(
          CASE WHEN c.project_count<>coalesce(p.relation_count,0)
                  OR c.building_count<>coalesce(b.relation_count,0)
               THEN 1 ELSE 0 END
        ),0)
        FROM exact_pixel_clusters c
        LEFT JOIN project_aggregate p ON p.cluster_id=c.cluster_id
        LEFT JOIN building_aggregate b ON b.cluster_id=c.cluster_id
        WHERE c.run_id=?
        """,
        (run_id, run_id, run_id),
    ).fetchone()
    return int(row[0]), int(row[1])


def _run_build_validations(
    connection: sqlite3.Connection,
    run_id: str,
    config: BuildConfig,
    snapshots_before: Sequence[InputSnapshot],
) -> tuple[bool, str, dict[str, dict[str, object]]]:
    quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    fk = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
    _record_validation(connection, run_id, "sqlite_quick_check", quick == "ok", "ok", quick)
    _record_validation(
        connection, run_id, "sqlite_integrity_check", integrity == "ok", "ok", integrity
    )
    _record_validation(connection, run_id, "foreign_key_check", fk == 0, 0, fk)

    success_assets = int(
        connection.execute(
            "SELECT count(*) FROM assets WHERE run_id=? AND fingerprint_status='success'",
            (run_id,),
        ).fetchone()[0]
    )
    node_members = int(
        connection.execute(
            "SELECT count(*) FROM phash_node_members WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    _record_validation(
        connection,
        run_id,
        "success_asset_phash_membership",
        success_assets == node_members,
        success_assets,
        node_members,
    )
    duplicate_pixel_assets = int(
        connection.execute(
            """
            SELECT coalesce(sum(n),0) FROM (
              SELECT count(*) AS n FROM assets
              WHERE run_id=? AND fingerprint_status='success'
              GROUP BY normalized_pixel_sha256 HAVING count(*)>1
            )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    exact_members = int(
        connection.execute(
            "SELECT count(*) FROM exact_pixel_cluster_members WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    _record_validation(
        connection,
        run_id,
        "duplicate_pixel_membership",
        duplicate_pixel_assets == exact_members,
        duplicate_pixel_assets,
        exact_members,
    )
    exact_clusters_checked, exact_relation_mismatches = (
        _validate_exact_relation_counts(connection, run_id)
    )
    _record_validation(
        connection,
        run_id,
        "exact_cluster_relation_counts",
        exact_relation_mismatches == 0,
        0,
        exact_relation_mismatches,
        detail={"clusters_checked": exact_clusters_checked},
    )
    edge_checked, edge_mismatches = _validate_edge_distances(connection, run_id)
    _record_validation(
        connection,
        run_id,
        "direct_phash_edge_distance",
        edge_mismatches == 0,
        0,
        edge_mismatches,
        detail={"checked": edge_checked},
    )
    pair_checked, pair_mismatches = _validate_metadata_cartesian(connection, run_id)
    _record_validation(
        connection,
        run_id,
        "metadata_cartesian_accounting",
        pair_mismatches == 0,
        0,
        pair_mismatches,
        detail={"checked": pair_checked},
    )
    candidate_mismatches = int(
        connection.execute(
            """
            SELECT count(*) FROM cross_source_building_candidates c
            WHERE c.run_id=? AND (
              c.exact_asset_pair_count<>(SELECT count(*) FROM candidate_image_evidence e
                WHERE e.run_id=c.run_id AND e.building_candidate_id=c.building_candidate_id
                  AND e.evidence_kind='exact_pixel')
              OR c.identical_phash_pair_count<>(SELECT count(*) FROM candidate_image_evidence e
                WHERE e.run_id=c.run_id AND e.building_candidate_id=c.building_candidate_id
                  AND e.evidence_kind='identical_phash')
              OR c.phash_le8_pair_count<>(SELECT count(*) FROM candidate_image_evidence e
                WHERE e.run_id=c.run_id AND e.building_candidate_id=c.building_candidate_id
                  AND e.evidence_kind='phash_le8')
              OR c.phash_9_16_pair_count<>(SELECT count(*) FROM candidate_image_evidence e
                WHERE e.run_id=c.run_id AND e.building_candidate_id=c.building_candidate_id
                  AND e.evidence_kind='phash_9_16')
            )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    _record_validation(
        connection,
        run_id,
        "building_candidate_aggregate_counts",
        candidate_mismatches == 0,
        0,
        candidate_mismatches,
    )

    snapshots_after = snapshot_inputs(config.inputs)
    before_by_role = {item.role: item.sha256 for item in snapshots_before}
    after_by_role = {item.role: item.sha256 for item in snapshots_after}
    inputs_unchanged = before_by_role == after_by_role
    for item in snapshots_after:
        connection.execute(
            "UPDATE e2_inputs SET sha256_after=? WHERE run_id=? AND input_name=?",
            (item.sha256, run_id, item.role),
        )
    _record_validation(
        connection,
        run_id,
        "immutable_input_sha256",
        inputs_unchanged,
        before_by_role,
        after_by_role,
    )
    no_input_sidecars = not any(sqlite_sidecars(item.path) for item in snapshots_after)
    _record_validation(
        connection,
        run_id,
        "immutable_input_sqlite_sidecars",
        no_input_sidecars,
        [],
        [str(path) for item in snapshots_after for path in sqlite_sidecars(item.path)],
    )
    logical_sha, table_manifests = logical_evidence_manifest(connection, run_id)
    _insert_metric(connection, run_id, "validation", "output_logical_sha256", logical_sha)
    _insert_metric(connection, run_id, "validation", "network_requests", 0)
    _insert_metric(connection, run_id, "validation", "vision_requests", 0)
    _insert_metric(connection, run_id, "validation", "llm_requests", 0)
    _record_validation(
        connection,
        run_id,
        "logical_manifest_present",
        len(logical_sha) == 64,
        64,
        len(logical_sha),
        detail={"tables": table_manifests},
    )
    connection.commit()
    failed = int(
        connection.execute(
            """
            SELECT count(*) FROM e2_validations
            WHERE run_id=? AND severity='error' AND passed=0
            """,
            (run_id,),
        ).fetchone()[0]
    )
    return failed == 0, logical_sha, table_manifests


def _read_metrics(path: Path, run_id: str) -> dict[str, int | float | str | None]:
    connection = open_sidecar(path, readonly=True, immutable=True)
    try:
        result: dict[str, int | float | str | None] = {}
        for row in connection.execute(
            """
            SELECT phase,metric_name,stratum_json,
                   value_integer,value_real,value_text
            FROM e2_metrics WHERE run_id=?
            ORDER BY phase,metric_name,stratum_json
            """,
            (run_id,),
        ):
            key = f"{row[0]}.{row[1]}"
            if str(row[2]) != "{}":
                key += "." + str(row[2])
            value = row[3] if row[3] is not None else row[4]
            if value is None:
                value = row[5]
            result[key] = value
        return result
    finally:
        connection.close()


def build_cross_source_image_evidence(config: BuildConfig) -> BuildResult:
    """Build one no-clobber E2 evidence artifact completely offline."""

    _validate_config(config)
    started = time.monotonic()
    output = config.output_path.resolve()
    snapshots = snapshot_inputs(config.inputs)
    input_manifest_sha = _input_manifest(snapshots, config)
    run_id = _run_id(input_manifest_sha)
    lock_path = Path(str(output) + ".lock")
    connection: sqlite3.Connection | None = None
    logical_sha: str | None = None
    status = "failed_validation"
    with acquire_build_lock(lock_path):
        connection = initialize_sidecar(output)
        try:
            _record_run_and_inputs(
                connection, run_id, input_manifest_sha, snapshots, config
            )
            paths = _snapshot_by_role(snapshots)
            selected: tuple[tuple[str, str, str, str], ...] = ()
            selected_by_source: dict[str, set[str] | None] = {
                "divisare": None,
                "architizer": None,
            }
            if config.sample_size is not None:
                selector = sqlite3.connect(":memory:", uri=True)
                try:
                    _attach_inputs(selector, snapshots)
                    selected = _select_sample_assets(
                        selector, size=config.sample_size, seed=config.sample_seed
                    )
                finally:
                    selector.close()
                selected_by_source = {
                    source: {
                        asset_id
                        for item_source, asset_id, _, _ in selected
                        if item_source == source
                    }
                    for source in ("divisare", "architizer")
                }
                _record_sample_manifest(
                    connection, run_id, selected, config.sample_seed
                )

            with open_divisare_sources(
                paths["divisare_curated"].path,
                paths["divisare_e1"].path,
                batch_size=config.batch_size,
            ) as div_adapter, open_architizer_sources(
                paths["architizer_curated"].path,
                paths["architizer_e1"].path,
                batch_size=config.batch_size,
            ) as arch_adapter:
                div_counts = _populate_one_source(
                    connection,
                    run_id,
                    div_adapter,
                    selected_asset_ids=selected_by_source["divisare"],
                    batch_size=config.batch_size,
                )
                _checkpoint(
                    connection,
                    run_id,
                    "source_divisare",
                    sum(div_counts.values()),
                    cursor=div_counts,
                )
                arch_counts = _populate_one_source(
                    connection,
                    run_id,
                    arch_adapter,
                    selected_asset_ids=selected_by_source["architizer"],
                    batch_size=config.batch_size,
                )
                _checkpoint(
                    connection,
                    run_id,
                    "source_architizer",
                    sum(arch_counts.values()),
                    cursor=arch_counts,
                )
                if config.sample_size is None:
                    expected_assets = {
                        "divisare": div_adapter.e1_run.source_total_count,
                        "architizer": arch_adapter.e1_run.source_total_count,
                    }
                else:
                    expected_assets = {
                        source: len(selected_by_source[source] or ())
                        for source in ("divisare", "architizer")
                    }

            for source, counts in (("divisare", div_counts), ("architizer", arch_counts)):
                for name, value in counts.items():
                    _insert_metric(
                        connection,
                        run_id,
                        "source_import",
                        name,
                        value,
                        stratum={"source": source},
                    )
                _record_validation(
                    connection,
                    run_id,
                    f"{source}_selected_asset_count",
                    counts["assets"] == expected_assets[source],
                    expected_assets[source],
                    counts["assets"],
                )

            asset_manifest_count, asset_manifest_sha = _selection_manifest_from_assets(
                connection, run_id
            )
            selection_manifest_sha = asset_manifest_sha
            if config.sample_size is not None:
                _record_validation(
                    connection,
                    run_id,
                    "sample_selected_asset_count",
                    asset_manifest_count == config.sample_size,
                    config.sample_size,
                    asset_manifest_count,
                )
            connection.execute(
                """
                UPDATE e2_runs SET ordered_selection_manifest_sha256=?
                WHERE run_id=?
                """,
                (selection_manifest_sha, run_id),
            )
            connection.commit()

            project_asset_count = _materialize_project_assets(
                connection, run_id, config.batch_size
            )
            building_asset_count = _materialize_building_assets(
                connection, run_id, config.batch_size
            )
            _insert_metric(
                connection, run_id, "relations", "project_assets", project_asset_count
            )
            _insert_metric(
                connection, run_id, "relations", "building_assets", building_asset_count
            )
            _checkpoint(
                connection,
                run_id,
                "relations",
                project_asset_count + building_asset_count,
            )

            exact_clusters, exact_members = _materialize_exact_clusters(
                connection, run_id, config.batch_size
            )
            phash_nodes, phash_members = _materialize_phash_nodes(
                connection, run_id, config.batch_size
            )
            for name, value in (
                ("exact_clusters", exact_clusters),
                ("exact_cluster_members", exact_members),
                ("phash_nodes", phash_nodes),
                ("phash_node_members", phash_members),
            ):
                _insert_metric(connection, run_id, "hash_index", name, value)
            _checkpoint(
                connection,
                run_id,
                "hash_nodes",
                exact_clusters + exact_members + phash_nodes + phash_members,
            )

            phash_candidates, global_edges, rejected_candidates = (
                _build_global_phash_candidates(connection, run_id, config.batch_size)
            )
            _insert_metric(
                connection, run_id, "phash_global", "candidates", phash_candidates
            )
            _insert_metric(connection, run_id, "phash_global", "edges_le8", global_edges)
            _insert_metric(
                connection,
                run_id,
                "phash_global",
                "rejected_after_distance",
                rejected_candidates,
            )
            _checkpoint(
                connection, run_id, "phash_global", phash_candidates, complete=True
            )

            metadata_pairs = _materialize_metadata_pairs(
                connection, run_id, config.batch_size
            )
            global_evidence = _materialize_global_image_evidence(
                connection, run_id, config.batch_size
            )
            metadata_review = _materialize_metadata_phash_review(
                connection, run_id, config.batch_size
            )
            _insert_metric(
                connection, run_id, "candidate_generation", "metadata_pairs", metadata_pairs
            )
            for name, value in global_evidence.items():
                _insert_metric(connection, run_id, "candidate_generation", name, value)
            for name, value in metadata_review.items():
                _insert_metric(connection, run_id, "candidate_generation", name, value)

            building_candidates = _finalize_building_candidates(
                connection, run_id, config.batch_size
            )
            project_pairs = _materialize_project_image_evidence(
                connection, run_id, config.batch_size
            )
            _insert_metric(
                connection,
                run_id,
                "candidate_generation",
                "building_candidates",
                building_candidates,
            )
            _insert_metric(
                connection,
                run_id,
                "candidate_generation",
                "project_image_pairs",
                project_pairs,
            )
            _checkpoint(
                connection,
                run_id,
                "candidate_generation",
                building_candidates + project_pairs,
            )

            passed, logical_sha, table_manifests = _run_build_validations(
                connection, run_id, config, snapshots
            )
            _insert_metric(
                connection,
                run_id,
                "validation",
                "logical_table_count",
                len(table_manifests),
            )
            connection.commit()
            status = "complete" if passed else "failed_validation"
            finalize_sidecar(
                connection,
                status=status,
                error=None if passed else "one or more error validations failed",
            )
            connection = None
            prepare_immutable_sidecar(output)
            local_validation = validate_sidecar(output, immutable=True)
            if status == "complete" and not local_validation.passed:
                raise SidecarSchemaError(
                    f"terminal sidecar validation failed: {local_validation}"
                )
        except Exception as exc:
            if connection is not None:
                try:
                    rows = connection.execute("SELECT run_id,status FROM e2_runs").fetchall()
                    if len(rows) == 1 and str(rows[0][1]) == "building":
                        _record_validation(
                            connection,
                            run_id,
                            "pipeline_exception",
                            False,
                            "no exception",
                            f"{type(exc).__name__}: {exc}",
                        )
                        connection.commit()
                        finalize_sidecar(
                            connection,
                            status="failed_validation",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        connection = None
                except Exception:
                    if connection is not None:
                        connection.close()
                        connection = None
            raise

    elapsed = time.monotonic() - started
    metrics = _read_metrics(output, run_id)
    return BuildResult(output, run_id, status, elapsed, logical_sha, metrics)


__all__ = [
    "BuildConfig",
    "BuildResult",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_SAMPLE_SEED",
    "InputSnapshot",
    "InputSpec",
    "PIPELINE_VERSION",
    "build_cross_source_image_evidence",
    "default_input_specs",
    "logical_evidence_manifest",
    "open_immutable",
    "sha256_file",
    "snapshot_input",
    "snapshot_inputs",
    "sqlite_sidecars",
]
