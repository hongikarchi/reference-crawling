"""Immutable E2 source adapter for deterministic image selection.

This module reads facts from one completed, full E2 evidence artifact.  It
does not choose a representative, form perceptual-hash components, or perform
network/Vision work.  The ``original_*`` dimensions exposed here describe the
decoded response from E1's frozen 1024-pixel request contract; they are useful
only as guards and deterministic tie-breakers, not as native-resolution or
semantic-quality measurements.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Iterator, Mapping, Sequence


DEFAULT_BATCH_SIZE = 1_000
BUILDING_STRATA = (
    "no_success",
    "cover_quality_risk",
    "gallery_fallback",
    "cross_source_candidate",
    "ordinary",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class SelectionSourceError(RuntimeError):
    """Raised when an E2 artifact does not satisfy the immutable contract."""


@dataclass(frozen=True)
class E2ArtifactSpec:
    path: Path
    expected_size: int
    expected_sha256: str
    expected_logical_sha256: str
    expected_contract_version: str | None = None
    expected_builder_version: str | None = None


@dataclass(frozen=True)
class E2InputLineage:
    input_name: str
    source: str
    input_role: str
    file_path: str
    size_bytes: int
    sha256_before: str
    sha256_after: str
    application_id: int | None
    user_version: int | None
    schema_manifest_sha256: str | None


@dataclass(frozen=True)
class E2RunLineage:
    run_id: str
    contract_version: str
    builder_version: str
    selection_mode: str
    ordered_selection_manifest_sha256: str
    stored_logical_sha256: str
    artifact_size: int
    artifact_sha256: str
    inputs: tuple[E2InputLineage, ...]


@dataclass(frozen=True)
class BuildingSummary:
    source: str
    source_building_id: str
    name: str
    source_record_sha256: str
    successful_asset_count: int
    successful_cover_count: int
    quality_risk_cover_count: int
    cross_source_candidate: bool
    stratum: str


@dataclass(frozen=True)
class BuildingImageCandidate:
    source: str
    source_building_id: str
    source_asset_id: str
    canonical_url: str | None
    fetch_url: str | None
    final_url: str | None
    original_width: int
    original_height: int
    normalized_width: int
    normalized_height: int
    quality_flags: tuple[str, ...]
    roles: tuple[str, ...]
    lowest_project_ordinal: int | None
    normalized_pixel_sha256: str
    exact_cluster_id: str | None
    phash_hex: str
    phash_node_id: str
    source_asset_record_sha256: str
    building_relation_record_sha256: str

    @property
    def decoded_min_edge(self) -> int:
        """Minimum edge of the decoded E1 1024-response image."""

        return min(self.original_width, self.original_height)

    @property
    def quality_risk(self) -> bool:
        return (
            "low_information" in self.quality_flags
            or self.decoded_min_edge < 256
        )

    @property
    def is_cover(self) -> bool:
        return "cover" in self.roles


@dataclass(frozen=True)
class DirectPhashEdge:
    edge_id: str
    left_node_id: str
    right_node_id: str
    hamming_distance: int
    edge_record_sha256: str


@dataclass(frozen=True)
class SameBuildingDirectPhashEdge:
    """One direct pHash edge expanded only inside a source building.

    This is the bounded full-build form of :class:`DirectPhashEdge`.  It is
    deliberately candidate-level and ordered by source/building/asset so a
    caller can merge it with ``iter_all_candidates`` without retaining the
    global pHash node ledger in memory.
    """

    source: str
    source_building_id: str
    left_source_asset_id: str
    right_source_asset_id: str
    left_node_id: str
    right_node_id: str
    hamming_distance: int
    edge_id: str
    edge_record_sha256: str


def _sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_sidecars(path: Path) -> tuple[Path, ...]:
    candidates = (
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
        Path(str(path) + ".lock"),
    )
    return tuple(candidate for candidate in candidates if candidate.exists())


def _iter_rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[object] = (),
    *,
    batch_size: int,
) -> Iterator[sqlite3.Row]:
    cursor = connection.execute(query, tuple(parameters))
    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield from rows
    finally:
        try:
            cursor.close()
        except sqlite3.ProgrammingError:
            # A process-style interruption can close the immutable source
            # connection before Python finalizes a suspended generator.
            pass


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_sha256(value: object, *, label: str) -> str:
    text = str(value).lower()
    if _SHA256_RE.fullmatch(text) is None:
        raise SelectionSourceError(
            f"{label} must be a lowercase 64-character SHA-256"
        )
    return text


def _json_string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError) as exc:
        raise SelectionSourceError(f"invalid {field} JSON") from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise SelectionSourceError(f"{field} must be a JSON string array")
    return tuple(parsed)


def _quality_flags(provenance_json: object) -> tuple[str, ...]:
    try:
        payload = json.loads(str(provenance_json or "{}"))
    except (TypeError, ValueError) as exc:
        raise SelectionSourceError("invalid asset provenance_json") from exc
    if not isinstance(payload, dict):
        raise SelectionSourceError("asset provenance_json must be an object")
    flags = payload.get("quality_flags", [])
    if not isinstance(flags, list) or not all(
        isinstance(flag, str) for flag in flags
    ):
        raise SelectionSourceError("quality_flags must be a JSON string array")
    return tuple(flags)


def _building_stratum(
    *,
    successful_asset_count: int,
    successful_cover_count: int,
    quality_risk_cover_count: int,
    cross_source_candidate: bool,
) -> str:
    if successful_asset_count == 0:
        return "no_success"
    if quality_risk_cover_count > 0:
        return "cover_quality_risk"
    if successful_cover_count == 0:
        return "gallery_fallback"
    if cross_source_candidate:
        return "cross_source_candidate"
    return "ordinary"


_BUILDING_SUMMARY_SQL = """
WITH asset_counts AS MATERIALIZED (
  SELECT ba.source,ba.source_building_id,
         sum(a.fingerprint_status='success') AS successful_asset_count,
         sum(a.fingerprint_status='success'
             AND instr(ba.roles_json,'"cover"')>0) AS successful_cover_count,
         sum(a.fingerprint_status='success'
             AND instr(ba.roles_json,'"cover"')>0
             AND (instr(a.provenance_json,'low_information')>0
                  OR min(a.original_width,a.original_height)<256))
             AS quality_risk_cover_count
  FROM building_assets ba
  JOIN assets a
    ON a.run_id=ba.run_id AND a.source=ba.source
   AND a.source_asset_id=ba.source_asset_id
  WHERE ba.run_id=?
  GROUP BY ba.source,ba.source_building_id
), candidate_buildings AS MATERIALIZED (
  SELECT left_source AS source,left_source_building_id AS source_building_id
  FROM cross_source_building_candidates WHERE run_id=?
  UNION
  SELECT right_source AS source,right_source_building_id AS source_building_id
  FROM cross_source_building_candidates WHERE run_id=?
)
SELECT b.source,b.source_building_id,b.name,b.source_record_sha256,
       coalesce(ac.successful_asset_count,0),
       coalesce(ac.successful_cover_count,0),
       coalesce(ac.quality_risk_cover_count,0),
       cb.source_building_id IS NOT NULL AS cross_source_candidate
FROM source_buildings b
LEFT JOIN asset_counts ac
  ON ac.source=b.source AND ac.source_building_id=b.source_building_id
LEFT JOIN candidate_buildings cb
  ON cb.source=b.source AND cb.source_building_id=b.source_building_id
WHERE b.run_id=? AND (? IS NULL OR b.source=?)
ORDER BY b.source,b.source_building_id
"""

_BUILDING_SUMMARY_AFTER_SQL = (
    _BUILDING_SUMMARY_SQL.replace(
        "WHERE ba.run_id=?\n  GROUP BY",
        "WHERE ba.run_id=? AND (ba.source,ba.source_building_id)>(?,?)\n"
        "  GROUP BY",
    )
    .replace(
        "FROM cross_source_building_candidates WHERE run_id=?\n  UNION",
        "FROM cross_source_building_candidates WHERE run_id=?\n"
        "    AND (left_source,left_source_building_id)>(?,?)\n  UNION",
    )
    .replace(
        "FROM cross_source_building_candidates WHERE run_id=?\n)",
        "FROM cross_source_building_candidates WHERE run_id=?\n"
        "    AND (right_source,right_source_building_id)>(?,?)\n)",
    )
    .replace(
        "ORDER BY b.source,b.source_building_id",
        "AND (b.source,b.source_building_id)>(?,?) "
        "ORDER BY b.source,b.source_building_id",
    )
)


_CANDIDATE_SQL = """
SELECT ba.source,ba.source_building_id,ba.source_asset_id,
       a.canonical_url,a.fetch_url,a.final_url,
       a.original_width,a.original_height,
       a.normalized_width,a.normalized_height,
       a.provenance_json,ba.roles_json,
       (SELECT min(pa.first_ordinal)
        FROM source_project_buildings spb
          INDEXED BY idx_source_project_buildings_building
        CROSS JOIN project_assets pa
          INDEXED BY idx_project_assets_asset
        WHERE spb.run_id=ba.run_id AND spb.source=ba.source
          AND spb.source_building_id=ba.source_building_id
          AND pa.run_id=spb.run_id AND pa.source=spb.source
          AND pa.source_project_id=spb.source_project_id
          AND pa.source_asset_id=ba.source_asset_id
       ) AS lowest_project_ordinal,
       a.normalized_pixel_sha256,em.cluster_id,
       a.phash_hex,pm.node_id,
       a.source_record_sha256,ba.relation_record_sha256
FROM building_assets ba INDEXED BY sqlite_autoindex_building_assets_1
CROSS JOIN assets a INDEXED BY sqlite_autoindex_assets_1
LEFT JOIN exact_pixel_cluster_members em INDEXED BY idx_exact_members_asset
  ON em.run_id=ba.run_id AND em.source=ba.source
 AND em.source_asset_id=ba.source_asset_id
LEFT JOIN phash_node_members pm INDEXED BY idx_phash_members_asset
  ON pm.run_id=ba.run_id AND pm.source=ba.source
 AND pm.source_asset_id=ba.source_asset_id
WHERE ba.run_id=?
  AND a.run_id=ba.run_id AND a.source=ba.source
  AND a.source_asset_id=ba.source_asset_id
  AND a.fingerprint_status='success'
  AND (? IS NULL OR ba.source=?)
  AND (? IS NULL OR ba.source_building_id=?)
ORDER BY ba.source,ba.source_building_id,ba.source_asset_id
"""

_CANDIDATE_AFTER_SQL = _CANDIDATE_SQL.replace(
    "ORDER BY ba.source,ba.source_building_id,ba.source_asset_id",
    "AND (ba.source,ba.source_building_id)>(?,?) "
    "ORDER BY ba.source,ba.source_building_id,ba.source_asset_id",
)


_SAME_BUILDING_DIRECT_PHASH_SQL = """
SELECT left_ba.source,left_ba.source_building_id,
       left_member.source_asset_id,right_member.source_asset_id,
       edge.left_node_id,edge.right_node_id,edge.hamming_distance,
       edge.edge_id,edge.edge_record_sha256
FROM phash_edges edge
CROSS JOIN phash_node_members left_member
  ON left_member.run_id=edge.run_id
 AND left_member.node_id=edge.left_node_id
CROSS JOIN building_assets left_ba
  ON left_ba.run_id=left_member.run_id
 AND left_ba.source=left_member.source
 AND left_ba.source_asset_id=left_member.source_asset_id
CROSS JOIN phash_node_members right_member
  ON right_member.run_id=edge.run_id
 AND right_member.node_id=edge.right_node_id
 AND right_member.source=left_ba.source
CROSS JOIN building_assets right_ba
  ON right_ba.run_id=right_member.run_id
 AND right_ba.source=right_member.source
 AND right_ba.source_asset_id=right_member.source_asset_id
 AND right_ba.source_building_id=left_ba.source_building_id
WHERE edge.run_id=? AND edge.edge_scope='global_le8'
  AND edge.hamming_distance BETWEEN 1 AND 8
ORDER BY left_ba.source,left_ba.source_building_id,
         left_member.source_asset_id,right_member.source_asset_id,edge.edge_id
"""

_SAME_BUILDING_DIRECT_PHASH_AFTER_SQL = _SAME_BUILDING_DIRECT_PHASH_SQL.replace(
    "ORDER BY left_ba.source,left_ba.source_building_id,",
    "AND (left_ba.source,left_ba.source_building_id)>(?,?) "
    "ORDER BY left_ba.source,left_ba.source_building_id,",
)


class E2SelectionSources:
    """Verified, immutable streaming view of one full E2 artifact."""

    def __init__(
        self,
        spec: E2ArtifactSpec,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self.path = Path(spec.path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        sidecars = _sqlite_sidecars(self.path)
        if sidecars:
            raise SelectionSourceError(
                "E2 artifact has SQLite/lock sidecars: "
                + ", ".join(str(path) for path in sidecars)
            )
        size_before = self.path.stat().st_size
        if size_before != spec.expected_size:
            raise SelectionSourceError(
                f"E2 byte size mismatch: {size_before} != {spec.expected_size}"
            )
        expected_byte_sha = _required_sha256(
            spec.expected_sha256,
            label="expected E2 byte SHA-256",
        )
        expected_logical_sha = _required_sha256(
            spec.expected_logical_sha256,
            label="expected E2 logical SHA-256",
        )
        observed_sha = _sha256_file(self.path)
        if observed_sha.lower() != expected_byte_sha:
            raise SelectionSourceError("E2 byte SHA-256 mismatch")
        if self.path.stat().st_size != size_before or _sqlite_sidecars(self.path):
            raise SelectionSourceError("E2 artifact changed while binding")

        self.connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            self.lineage = self._bind_lineage(
                spec,
                artifact_size=size_before,
                artifact_sha256=observed_sha,
                expected_logical_sha256=expected_logical_sha,
            )
        except Exception:
            self.connection.close()
            raise

    def _bind_lineage(
        self,
        spec: E2ArtifactSpec,
        *,
        artifact_size: int,
        artifact_sha256: str,
        expected_logical_sha256: str,
    ) -> E2RunLineage:
        try:
            rows = self.connection.execute(
                """
                SELECT run_id,contract_version,builder_version,selection_mode,
                       ordered_selection_manifest_sha256,status
                FROM e2_runs ORDER BY run_id
                """
            ).fetchmany(2)
        except sqlite3.DatabaseError as exc:
            raise SelectionSourceError("not a readable E2 artifact") from exc
        if len(rows) != 1:
            raise SelectionSourceError("E2 artifact must contain exactly one run")
        row = rows[0]
        if str(row["status"]) != "complete" or str(row["selection_mode"]) != "full":
            raise SelectionSourceError(
                "E2 run must be terminal complete with selection_mode=full"
            )
        run_id = str(row["run_id"])
        contract_version = str(row["contract_version"])
        builder_version = str(row["builder_version"])
        if (
            spec.expected_contract_version is not None
            and contract_version != spec.expected_contract_version
        ):
            raise SelectionSourceError("E2 contract version mismatch")
        if (
            spec.expected_builder_version is not None
            and builder_version != spec.expected_builder_version
        ):
            raise SelectionSourceError("E2 builder version mismatch")

        logical_rows = self.connection.execute(
            """
            SELECT value_text FROM e2_metrics
            WHERE run_id=? AND phase='validation'
              AND metric_name='output_logical_sha256'
              AND stratum_json='{}'
            """,
            (run_id,),
        ).fetchmany(2)
        if len(logical_rows) != 1 or _optional_text(logical_rows[0][0]) is None:
            raise SelectionSourceError("E2 stored logical SHA is missing or ambiguous")
        stored_logical = _required_sha256(
            logical_rows[0][0],
            label="stored E2 logical SHA-256",
        )
        if stored_logical != expected_logical_sha256:
            raise SelectionSourceError("E2 logical SHA-256 mismatch")

        input_rows = self.connection.execute(
            """
            SELECT input_name,source,input_role,file_path,size_bytes,
                   sha256_before,sha256_after,application_id,user_version,
                   schema_manifest_sha256
            FROM e2_inputs WHERE run_id=? ORDER BY input_name
            """,
            (run_id,),
        ).fetchall()
        if not input_rows:
            raise SelectionSourceError("E2 input lineage is empty")
        inputs: list[E2InputLineage] = []
        for input_row in input_rows:
            before = _required_sha256(
                input_row["sha256_before"],
                label=f"{input_row['input_name']} input SHA-256 before",
            )
            after = _optional_text(input_row["sha256_after"])
            if after is None or _required_sha256(
                after,
                label=f"{input_row['input_name']} input SHA-256 after",
            ) != before:
                raise SelectionSourceError(
                    f"E2 input mutated: {input_row['input_name']}"
                )
            inputs.append(
                E2InputLineage(
                    input_name=str(input_row["input_name"]),
                    source=str(input_row["source"]),
                    input_role=str(input_row["input_role"]),
                    file_path=str(input_row["file_path"]),
                    size_bytes=int(input_row["size_bytes"]),
                    sha256_before=before,
                    sha256_after=before,
                    application_id=(
                        None
                        if input_row["application_id"] is None
                        else int(input_row["application_id"])
                    ),
                    user_version=(
                        None
                        if input_row["user_version"] is None
                        else int(input_row["user_version"])
                    ),
                    schema_manifest_sha256=_optional_text(
                        input_row["schema_manifest_sha256"]
                    ),
                )
            )
        manifest_value = _optional_text(row["ordered_selection_manifest_sha256"])
        if manifest_value is None:
            raise SelectionSourceError("E2 ordered selection manifest is missing")
        manifest_sha = _required_sha256(
            manifest_value,
            label="E2 ordered selection manifest SHA-256",
        )
        return E2RunLineage(
            run_id=run_id,
            contract_version=contract_version,
            builder_version=builder_version,
            selection_mode=str(row["selection_mode"]),
            ordered_selection_manifest_sha256=manifest_sha,
            stored_logical_sha256=stored_logical,
            artifact_size=artifact_size,
            artifact_sha256=artifact_sha256,
            inputs=tuple(inputs),
        )

    @property
    def run_id(self) -> str:
        return self.lineage.run_id

    @property
    def contract_version(self) -> str:
        return self.lineage.contract_version

    @property
    def builder_version(self) -> str:
        return self.lineage.builder_version

    @property
    def stored_logical_sha256(self) -> str:
        return self.lineage.stored_logical_sha256

    @property
    def input_lineage(self) -> tuple[E2InputLineage, ...]:
        return self.lineage.inputs

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> E2SelectionSources:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def iter_building_summaries(
        self,
        source: str | None = None,
        *,
        start_after: tuple[str, str] | None = None,
    ) -> Iterator[BuildingSummary]:
        if source is not None and source not in {"divisare", "architizer"}:
            raise ValueError(f"unsupported source: {source}")
        if start_after is not None:
            start_source, start_building = start_after
            if start_source not in {"divisare", "architizer"} or not start_building:
                raise ValueError("invalid building-summary start_after key")
        else:
            start_source = start_building = None
        if start_after is None:
            query = _BUILDING_SUMMARY_SQL
            parameters: tuple[object, ...] = (
                self.run_id,
                self.run_id,
                self.run_id,
                self.run_id,
                source,
                source,
            )
        else:
            query = _BUILDING_SUMMARY_AFTER_SQL
            parameters = (
                self.run_id,
                start_source,
                start_building,
                self.run_id,
                start_source,
                start_building,
                self.run_id,
                start_source,
                start_building,
                self.run_id,
                source,
                source,
                start_source,
                start_building,
            )
        for row in _iter_rows(
            self.connection,
            query,
            parameters,
            batch_size=self.batch_size,
        ):
            success = int(row[4])
            cover = int(row[5])
            risk = int(row[6])
            cross_source = bool(row[7])
            yield BuildingSummary(
                source=str(row[0]),
                source_building_id=str(row[1]),
                name=str(row[2]),
                source_record_sha256=_required_sha256(
                    row[3],
                    label="building source record SHA-256",
                ),
                successful_asset_count=success,
                successful_cover_count=cover,
                quality_risk_cover_count=risk,
                cross_source_candidate=cross_source,
                stratum=_building_stratum(
                    successful_asset_count=success,
                    successful_cover_count=cover,
                    quality_risk_cover_count=risk,
                    cross_source_candidate=cross_source,
                ),
            )

    def building_stratum_counts(
        self,
        source: str | None = None,
    ) -> Mapping[str, int]:
        counts = {stratum: 0 for stratum in BUILDING_STRATA}
        for summary in self.iter_building_summaries(source):
            counts[summary.stratum] += 1
        return counts

    def building_population_count(self, source: str | None = None) -> int:
        return sum(self.building_stratum_counts(source).values())

    def _candidate_rows(
        self,
        *,
        source: str | None,
        building_id: str | None,
        start_after: tuple[str, str] | None,
    ) -> Iterator[BuildingImageCandidate]:
        if start_after is not None:
            start_source, start_building = start_after
            if start_source not in {"divisare", "architizer"} or not start_building:
                raise ValueError("invalid candidate start_after key")
        else:
            start_source = start_building = None
        parameters: tuple[object, ...] = (
            self.run_id,
            source,
            source,
            building_id,
            building_id,
        )
        query = _CANDIDATE_SQL
        if start_after is not None:
            query = _CANDIDATE_AFTER_SQL
            parameters += (start_source, start_building)
        for row in _iter_rows(
            self.connection,
            query,
            parameters,
            batch_size=self.batch_size,
        ):
            dimensions = row[6:10]
            if any(value is None for value in dimensions):
                raise SelectionSourceError(
                    f"successful asset lacks decoded dimensions: {row[0]}:{row[2]}"
                )
            if row[13] is None or row[15] is None or row[16] is None:
                raise SelectionSourceError(
                    f"successful asset lacks E2 hash membership: {row[0]}:{row[2]}"
                )
            yield BuildingImageCandidate(
                source=str(row[0]),
                source_building_id=str(row[1]),
                source_asset_id=str(row[2]),
                canonical_url=_optional_text(row[3]),
                fetch_url=_optional_text(row[4]),
                final_url=_optional_text(row[5]),
                original_width=int(row[6]),
                original_height=int(row[7]),
                normalized_width=int(row[8]),
                normalized_height=int(row[9]),
                quality_flags=_quality_flags(row[10]),
                roles=_json_string_tuple(row[11], field="roles_json"),
                lowest_project_ordinal=(
                    None if row[12] is None else int(row[12])
                ),
                normalized_pixel_sha256=str(row[13]),
                exact_cluster_id=_optional_text(row[14]),
                phash_hex=str(row[15]),
                phash_node_id=str(row[16]),
                source_asset_record_sha256=_required_sha256(
                    row[17],
                    label="asset source record SHA-256",
                ),
                building_relation_record_sha256=_required_sha256(
                    row[18],
                    label="building relation record SHA-256",
                ),
            )

    def iter_candidates(
        self,
        source: str,
        building_id: str,
    ) -> Iterator[BuildingImageCandidate]:
        if source not in {"divisare", "architizer"}:
            raise ValueError(f"unsupported source: {source}")
        if not building_id:
            raise ValueError("building_id must be non-empty")
        return self._candidate_rows(
            source=source,
            building_id=building_id,
            start_after=None,
        )

    def iter_all_candidates(
        self,
        *,
        start_after: tuple[str, str] | None = None,
    ) -> Iterator[BuildingImageCandidate]:
        """Stream every successful building asset in source/building/asset order."""

        return self._candidate_rows(
            source=None,
            building_id=None,
            start_after=start_after,
        )

    def direct_phash_pairs(
        self,
        node_ids: Collection[str] | None = None,
    ) -> Iterator[DirectPhashEdge]:
        """Yield direct global <=8 edges, never transitive components.

        When ``node_ids`` is supplied, both endpoints must be in the requested
        set.  Filtering is deliberately performed while streaming the small
        direct-edge ledger, avoiding SQLite variable limits and temporary
        component materialization.
        """

        selected = None if node_ids is None else frozenset(str(item) for item in node_ids)
        query = """
        SELECT edge_id,left_node_id,right_node_id,hamming_distance,
               edge_record_sha256
        FROM phash_edges
        WHERE run_id=? AND edge_scope='global_le8'
          AND hamming_distance BETWEEN 1 AND 8
        ORDER BY left_node_id,right_node_id,edge_id
        """
        for row in _iter_rows(
            self.connection,
            query,
            (self.run_id,),
            batch_size=self.batch_size,
        ):
            left = str(row[1])
            right = str(row[2])
            if selected is not None and (left not in selected or right not in selected):
                continue
            yield DirectPhashEdge(
                edge_id=str(row[0]),
                left_node_id=left,
                right_node_id=right,
                hamming_distance=int(row[3]),
                edge_record_sha256=_required_sha256(
                    row[4],
                    label="direct pHash edge record SHA-256",
                ),
            )

    def iter_same_building_direct_phash_edges(
        self,
        *,
        start_after: tuple[str, str] | None = None,
    ) -> Iterator[SameBuildingDirectPhashEdge]:
        """Stream direct <=8 edges expanded only within one building.

        The SQL performs the node-to-asset expansion inside SQLite and emits
        rows in the same source/building order as ``iter_all_candidates``.
        Full E3 construction therefore needs memory proportional only to the
        current building, not to every global pHash node or edge.
        """

        if start_after is not None:
            start_source, start_building = start_after
            if start_source not in {"divisare", "architizer"} or not start_building:
                raise ValueError("invalid direct-edge start_after key")
        else:
            start_source = start_building = None
        query = _SAME_BUILDING_DIRECT_PHASH_SQL
        parameters: tuple[object, ...] = (self.run_id,)
        if start_after is not None:
            query = _SAME_BUILDING_DIRECT_PHASH_AFTER_SQL
            parameters += (start_source, start_building)
        for row in _iter_rows(
            self.connection,
            query,
            parameters,
            batch_size=self.batch_size,
        ):
            yield SameBuildingDirectPhashEdge(
                source=str(row[0]),
                source_building_id=str(row[1]),
                left_source_asset_id=str(row[2]),
                right_source_asset_id=str(row[3]),
                left_node_id=str(row[4]),
                right_node_id=str(row[5]),
                hamming_distance=int(row[6]),
                edge_id=str(row[7]),
                edge_record_sha256=_required_sha256(
                    row[8],
                    label="same-building direct pHash edge record SHA-256",
                ),
            )


def open_e2_selection_sources(
    spec: E2ArtifactSpec,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> E2SelectionSources:
    return E2SelectionSources(spec, batch_size=batch_size)


__all__ = [
    "BUILDING_STRATA",
    "BuildingImageCandidate",
    "BuildingSummary",
    "DirectPhashEdge",
    "E2ArtifactSpec",
    "E2InputLineage",
    "E2RunLineage",
    "E2SelectionSources",
    "SelectionSourceError",
    "SameBuildingDirectPhashEdge",
    "open_e2_selection_sources",
]
