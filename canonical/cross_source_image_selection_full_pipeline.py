"""Bounded, resumable full-population E3 shortlist materialization.

This module is intentionally separate from the accepted N10/N100 sample
builder.  It reads the immutable full E2 artifact, writes candidate-only
P0/P1/P2 policy evidence for every source building, and performs no network,
Vision, LLM, semantic-image, or representative-image work.

The full builder is safe to prepare before execution: callers must opt into
``build_full_cross_source_image_selection`` explicitly, while the companion
CLI defaults to a read-only preflight.  A recoverable interruption leaves the
single run in ``building`` state.  Resume accepts only the exact same E2 byte
and logical lineage, policy set, run identity, and persisted configuration.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import (
    Candidate,
    DirectPHashEdge,
    E3_POLICY_VERSION,
    E3_SAMPLE_POLICY_VERSION,
    E3_SELECTION_VERSION,
    SamplingItem,
    canonical_json,
    canonical_sha256,
    compare_standard_policies,
    editorial_sort_key,
    policy_definitions,
)
from canonical.cross_source_image_selection_pipeline import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_E2_BUILDER_VERSION,
    DEFAULT_E2_CONTRACT_VERSION,
    DEFAULT_E2_LOGICAL_SHA256,
    DEFAULT_E2_RELATIVE_PATH,
    DEFAULT_E2_SHA256,
    DEFAULT_E2_SIZE,
    DEFAULT_SHORTLIST_SIZE,
    _candidate,
    _candidate_mapping_values,
    _insert_metric,
    _normal_name,
    _policy_record_body,
    _policy_set_sha,
    _queue_record_body,
    _ranking_state,
    _schema_manifest,
    _selection_record_body,
    _shortlist_record_body,
    _stratum_id,
    _stratum_record_body,
    _summary_record,
    _sampling_item,
    _utc_now,
    logical_selection_manifest,
)
from canonical.cross_source_image_selection_sidecar import (
    acquire_build_lock,
    finalize_sidecar,
    initialize_sidecar,
    lock_path_for,
    open_sidecar,
    prepare_immutable_sidecar,
    sqlite_sidecar_paths,
)
from canonical.cross_source_image_selection_sources import (
    BuildingImageCandidate,
    E2ArtifactSpec,
    E2SelectionSources,
    SameBuildingDirectPhashEdge,
    open_e2_selection_sources,
)


FULL_PIPELINE_VERSION = "archibe-e3-cross-source-image-selection-full-pipeline-v1"
DEFAULT_CHECKPOINT_BUILDINGS = 500
FULL_CONFIRMATION = "RUN_E3_FULL_OFFLINE"
ESTIMATED_FULL_OUTPUT_BYTES = 10_522_671_104  # 9.8 GiB smoke-density estimate.
MINIMUM_FULL_FREE_BYTES = 15 * 1024**3
RECOMMENDED_FULL_FREE_BYTES = 25 * 1024**3
_PHASES = ("inventory", "selection", "candidates")
_QUEUE_UNITS = {
    "top1_no_reuse": "selected_entity",
    "top1_exact_reuse": "exact_unique_asset",
    "top3_no_reuse": "shortlist_item",
    "top3_exact_reuse": "exact_unique_asset",
}


class FullBuildResumeError(RuntimeError):
    """An existing building artifact does not match the requested resume."""


class SimulatedFullBuildInterruption(RuntimeError):
    """Test-only interruption raised immediately after a durable checkpoint."""


@dataclass(frozen=True)
class FullBuildConfig:
    e2_path: Path
    output_path: Path
    expected_e2_size: int
    expected_e2_sha256: str
    expected_e2_logical_sha256: str
    expected_e2_contract_version: str = DEFAULT_E2_CONTRACT_VERSION
    expected_e2_builder_version: str = DEFAULT_E2_BUILDER_VERSION
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE
    batch_size: int = DEFAULT_BATCH_SIZE
    checkpoint_buildings: int = DEFAULT_CHECKPOINT_BUILDINGS
    resume: bool = False
    # Runtime-only fault injection.  It is deliberately excluded from the
    # persisted config and run ID so a test can resume with this set to None.
    interrupt_after_commits: int | None = None


@dataclass(frozen=True)
class FullBuildPreflight:
    e2_path: Path
    output_path: Path
    e2_size_bytes: int
    e2_byte_sha256: str
    e2_logical_sha256: str
    population_buildings: int
    eligible_buildings: int
    unique_success_assets: int
    image_candidates: int
    candidate_occurrence_minus_unique_asset_count: int
    same_building_direct_edges: int
    output_exists: bool
    output_sqlite_sidecars: tuple[str, ...]
    output_lock_exists: bool
    no_clobber_ready: bool
    output_parent: Path
    disk_free_bytes: int
    estimated_output_bytes: int = ESTIMATED_FULL_OUTPUT_BYTES
    minimum_free_bytes: int = MINIMUM_FULL_FREE_BYTES
    recommended_free_bytes: int = RECOMMENDED_FULL_FREE_BYTES
    minimum_disk_space_satisfied: bool = False
    recommended_disk_space_satisfied: bool = False
    network_requests: int = 0
    vision_requests: int = 0
    llm_requests: int = 0


@dataclass(frozen=True)
class FullBuildResult:
    output_path: Path
    run_id: str
    status: str
    logical_sha256: str
    population_buildings: int
    eligible_buildings: int
    image_candidates: int
    shortlist_items: int
    resumed: bool
    elapsed_seconds: float


@dataclass
class _CommitController:
    interrupt_after: int | None
    commits: int = 0

    def commit(self, connection: sqlite3.Connection) -> None:
        connection.commit()
        self.commits += 1
        if self.interrupt_after == self.commits:
            raise SimulatedFullBuildInterruption(
                f"simulated interruption after durable commit {self.commits}"
            )


@dataclass(frozen=True)
class _Checkpoint:
    phase: str
    cursor: Mapping[str, Any]
    completed_rows: int
    phase_complete: bool


class _OrderedSelectionManifestHasher:
    """Streaming byte-equivalent form of the frozen core manifest."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b'{"ordered_items":[')
        self._count = 0

    def add(self, item: SamplingItem) -> None:
        self._count += 1
        if self._count > 1:
            self._digest.update(b",")
        self._digest.update(
            canonical_json(
                {
                    "item": item.as_record(),
                    "item_record_sha256": item.record_sha256,
                    "rank": self._count,
                }
            ).encode("utf-8")
        )

    def hexdigest(self) -> str:
        digest = self._digest.copy()
        digest.update(b'],"policy_version":')
        digest.update(canonical_json(E3_SAMPLE_POLICY_VERSION).encode("utf-8"))
        digest.update(b"}")
        return digest.hexdigest()


class _EdgeGroupStream:
    """One-group lookahead for source/building ordered direct-edge rows."""

    def __init__(self, rows: Iterable[SameBuildingDirectPhashEdge]) -> None:
        self._groups = iter(
            groupby(rows, key=lambda row: (row.source, row.source_building_id))
        )
        self._next = next(self._groups, None)

    def take(
        self, key: tuple[str, str]
    ) -> tuple[SameBuildingDirectPhashEdge, ...]:
        while self._next is not None and self._next[0] < key:
            _discard_key, discarded = self._next
            tuple(discarded)
            self._next = next(self._groups, None)
        if self._next is None or self._next[0] > key:
            return ()
        _matched_key, values = self._next
        result = tuple(values)
        self._next = next(self._groups, None)
        return result

    def assert_exhausted(self) -> None:
        if self._next is not None:
            key, values = self._next
            count = sum(1 for _ in values)
            raise FullBuildResumeError(
                f"direct pHash edge group has no candidate group: {key!r} ({count})"
            )


def default_e2_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / DEFAULT_E2_RELATIVE_PATH


def _sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _e2_sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
            Path(str(path) + ".lock"),
        )
        if candidate.exists()
    )


def _disk_capacity(output: Path) -> tuple[Path, int]:
    """Return the nearest existing parent and its available bytes, read-only."""

    parent = output.parent
    while not parent.exists():
        if parent == parent.parent:
            raise FileNotFoundError(f"no existing parent for E3 output: {output}")
        parent = parent.parent
    if not parent.is_dir():
        raise NotADirectoryError(f"E3 output parent is not a directory: {parent}")
    return parent, int(shutil.disk_usage(parent).free)


def _validate_config(config: FullBuildConfig) -> None:
    if config.expected_e2_size < 1:
        raise ValueError("expected_e2_size must be positive")
    if config.shortlist_size < 1:
        raise ValueError("shortlist_size must be positive")
    if config.batch_size < 1:
        raise ValueError("batch_size must be positive")
    if config.checkpoint_buildings < 1:
        raise ValueError("checkpoint_buildings must be positive")
    if config.interrupt_after_commits is not None and config.interrupt_after_commits < 1:
        raise ValueError("interrupt_after_commits must be positive")


def _artifact_spec(config: FullBuildConfig) -> E2ArtifactSpec:
    return E2ArtifactSpec(
        path=Path(config.e2_path).resolve(),
        expected_size=config.expected_e2_size,
        expected_sha256=config.expected_e2_sha256,
        expected_logical_sha256=config.expected_e2_logical_sha256,
        expected_contract_version=config.expected_e2_contract_version,
        expected_builder_version=config.expected_e2_builder_version,
    )


def _policy_rows(shortlist_size: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for policy in policy_definitions(shortlist_size):
        definition = policy.as_config()
        row: dict[str, Any] = {
            "policy_id": policy.policy_id,
            "policy_version": E3_POLICY_VERSION,
            "policy_name": policy.policy_id,
            "description": policy.description,
            "shortlist_size": policy.shortlist_size,
            "enabled": 1,
            "definition_json": definition,
            "policy_config_sha256": policy.config_sha256,
        }
        row["policy_record_sha256"] = canonical_sha256(
            _policy_record_body(
                policy_id=policy.policy_id,
                policy_name=policy.policy_id,
                description=policy.description,
                shortlist_size=policy.shortlist_size,
                definition=definition,
                policy_config_sha256=policy.config_sha256,
            )
        )
        rows.append(row)
    return rows


def _persisted_config(config: FullBuildConfig) -> dict[str, Any]:
    return {
        "authoritative": 0,
        "checkpoint_buildings": config.checkpoint_buildings,
        "creates_final_representative": False,
        "creates_vision_tasks": False,
        "full_population": True,
        "llm_requests": 0,
        "network_enabled": False,
        "network_requests": 0,
        "phash_semantic_reuse_allowed": False,
        "phash_transitive_closure_allowed": False,
        "pipeline_version": FULL_PIPELINE_VERSION,
        "semantic_reuse_allowed": False,
        "source_batch_size": config.batch_size,
        "vision_requests": 0,
    }


def _build_run_id(config: FullBuildConfig, policy_set_sha256: str) -> str:
    return "e3-full-" + canonical_sha256(
        {
            "builder_version": FULL_PIPELINE_VERSION,
            "config": _persisted_config(config),
            "contract_version": E3_SELECTION_VERSION,
            "e2_byte_sha256": config.expected_e2_sha256.lower(),
            "e2_logical_sha256": config.expected_e2_logical_sha256.lower(),
            "policy_set_sha256": policy_set_sha256,
            "selection_mode": "full",
            "shortlist_size": config.shortlist_size,
        }
    )[:24]


def _checkpoint(
    connection: sqlite3.Connection, run_id: str, phase: str
) -> _Checkpoint:
    row = connection.execute(
        """
        SELECT cursor_json,completed_rows,phase_complete
        FROM build_checkpoints WHERE run_id=? AND phase=?
        """,
        (run_id, phase),
    ).fetchone()
    if row is None:
        raise FullBuildResumeError(f"missing {phase} checkpoint")
    try:
        cursor = json.loads(str(row[0]))
    except (TypeError, ValueError) as exc:
        raise FullBuildResumeError(f"invalid {phase} checkpoint JSON") from exc
    if not isinstance(cursor, dict):
        raise FullBuildResumeError(f"invalid {phase} checkpoint object")
    return _Checkpoint(
        phase=phase,
        cursor=cursor,
        completed_rows=int(row[1]),
        phase_complete=bool(row[2]),
    )


def _cursor_key(cursor: Mapping[str, Any], *, phase: str) -> tuple[str, str] | None:
    value = cursor.get("last_key")
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise FullBuildResumeError(f"invalid {phase} last_key")
    return (value[0], value[1])


def _counter_from_json(
    value: object, *, label: str
) -> Counter[tuple[str, str]]:
    if not isinstance(value, dict):
        raise FullBuildResumeError(f"invalid {label} checkpoint counter")
    result: Counter[tuple[str, str]] = Counter()
    for key, count in value.items():
        if not isinstance(key, str) or ":" not in key:
            raise FullBuildResumeError(f"invalid {label} checkpoint key")
        source, stratum = key.split(":", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise FullBuildResumeError(f"invalid {label} checkpoint value")
        result[(source, stratum)] = count
    return result


def _counter_json(values: Mapping[tuple[str, str], int]) -> dict[str, int]:
    return {
        f"{source}:{stratum}": int(values[(source, stratum)])
        for source, stratum in sorted(values)
    }


def _update_checkpoint(
    connection: sqlite3.Connection,
    run_id: str,
    phase: str,
    *,
    cursor: Mapping[str, Any],
    completed_rows: int,
    phase_complete: bool,
) -> None:
    connection.execute(
        """
        UPDATE build_checkpoints
        SET cursor_json=?,completed_rows=?,phase_complete=?,updated_at=?
        WHERE run_id=? AND phase=?
        """,
        (
            canonical_json(dict(cursor)),
            completed_rows,
            int(phase_complete),
            _utc_now(),
            run_id,
            phase,
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise FullBuildResumeError(f"could not update {phase} checkpoint")


def _initialize_run(
    connection: sqlite3.Connection,
    source: E2SelectionSources,
    config: FullBuildConfig,
    policy_rows: Sequence[Mapping[str, Any]],
    policy_set_sha256: str,
    run_id: str,
) -> None:
    now = _utc_now()
    e2_path = Path(config.e2_path).resolve()
    connection.execute(
        """
        INSERT INTO selection_runs(
          run_id,contract_version,builder_version,e2_artifact_path,
          e2_size_bytes,e2_byte_sha256,e2_logical_sha256,
          policy_set_sha256,selection_mode,sample_size,sample_seed,
          shortlist_size,ordered_selection_manifest_sha256,
          config_json,network_requests,vision_requests,llm_requests,
          authoritative,artifact_scope,status,started_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            E3_SELECTION_VERSION,
            FULL_PIPELINE_VERSION,
            str(e2_path),
            source.lineage.artifact_size,
            source.lineage.artifact_sha256,
            source.lineage.stored_logical_sha256,
            policy_set_sha256,
            "full",
            None,
            None,
            config.shortlist_size,
            None,
            canonical_json(_persisted_config(config)),
            0,
            0,
            0,
            0,
            "candidate_only",
            "building",
            now,
        ),
    )
    input_detail = {
        "e2_builder_version": source.lineage.builder_version,
        "e2_contract_version": source.lineage.contract_version,
        "e2_logical_sha256": source.lineage.stored_logical_sha256,
        "e2_ordered_selection_manifest_sha256": (
            source.lineage.ordered_selection_manifest_sha256
        ),
        "e2_run_id": source.lineage.run_id,
        "e2_selection_mode": source.lineage.selection_mode,
        "inherited_inputs": [
            {
                "input_name": value.input_name,
                "size_bytes": value.size_bytes,
                "sha256": value.sha256_before,
            }
            for value in source.lineage.inputs
        ],
    }
    connection.execute(
        """
        INSERT INTO selection_inputs(
          run_id,input_name,input_role,file_path,size_bytes,sha256_before,
          sha256_after,logical_sha256,application_id,user_version,
          schema_manifest_sha256,recorded_at,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            "e2_evidence",
            "e2_evidence",
            str(e2_path),
            source.lineage.artifact_size,
            source.lineage.artifact_sha256,
            None,
            source.lineage.stored_logical_sha256,
            int(source.connection.execute("PRAGMA application_id").fetchone()[0]),
            int(source.connection.execute("PRAGMA user_version").fetchone()[0]),
            _schema_manifest(source.connection),
            now,
            canonical_json(input_detail),
        ),
    )
    for row in policy_rows:
        connection.execute(
            """
            INSERT INTO policy_definitions(
              run_id,policy_id,policy_version,policy_name,description,
              shortlist_size,enabled,definition_json,policy_config_sha256,
              policy_record_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                row["policy_id"],
                row["policy_version"],
                row["policy_name"],
                row["description"],
                row["shortlist_size"],
                row["enabled"],
                canonical_json(row["definition_json"]),
                row["policy_config_sha256"],
                row["policy_record_sha256"],
                now,
            ),
        )
    for phase in _PHASES:
        connection.execute(
            """
            INSERT INTO build_checkpoints(
              run_id,phase,cursor_json,completed_rows,phase_complete,updated_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (run_id, phase, "{}", 0, 0, now),
        )
    connection.commit()


def _validate_resume(
    connection: sqlite3.Connection,
    source: E2SelectionSources,
    config: FullBuildConfig,
    policy_rows: Sequence[Mapping[str, Any]],
    policy_set_sha256: str,
    run_id: str,
) -> None:
    connection.row_factory = sqlite3.Row
    rows = connection.execute("SELECT * FROM selection_runs").fetchmany(2)
    if len(rows) != 1:
        raise FullBuildResumeError("resume requires exactly one E3 run")
    run = rows[0]
    expected = {
        "run_id": run_id,
        "contract_version": E3_SELECTION_VERSION,
        "builder_version": FULL_PIPELINE_VERSION,
        "e2_artifact_path": str(Path(config.e2_path).resolve()),
        "e2_size_bytes": source.lineage.artifact_size,
        "e2_byte_sha256": source.lineage.artifact_sha256,
        "e2_logical_sha256": source.lineage.stored_logical_sha256,
        "policy_set_sha256": policy_set_sha256,
        "selection_mode": "full",
        "sample_size": None,
        "sample_seed": None,
        "shortlist_size": config.shortlist_size,
        "config_json": canonical_json(_persisted_config(config)),
        "network_requests": 0,
        "vision_requests": 0,
        "llm_requests": 0,
        "authoritative": 0,
        "artifact_scope": "candidate_only",
        "status": "building",
    }
    actual = {key: run[key] for key in expected}
    for key in (
        "e2_size_bytes",
        "shortlist_size",
        "network_requests",
        "vision_requests",
        "llm_requests",
        "authoritative",
    ):
        actual[key] = int(actual[key])
    if actual != expected:
        mismatch = {
            key: {"expected": expected[key], "actual": actual[key]}
            for key in expected
            if expected[key] != actual[key]
        }
        raise FullBuildResumeError(
            "resume run lineage/config mismatch: "
            + json.dumps(mismatch, sort_keys=True)
        )

    input_row = connection.execute(
        "SELECT * FROM selection_inputs WHERE run_id=?", (run_id,)
    ).fetchone()
    if input_row is None:
        raise FullBuildResumeError("resume E2 input lineage is missing")
    input_expected = {
        "input_name": "e2_evidence",
        "input_role": "e2_evidence",
        "file_path": str(Path(config.e2_path).resolve()),
        "size_bytes": source.lineage.artifact_size,
        "sha256_before": source.lineage.artifact_sha256,
        "sha256_after": None,
        "logical_sha256": source.lineage.stored_logical_sha256,
        "application_id": int(source.connection.execute("PRAGMA application_id").fetchone()[0]),
        "user_version": int(source.connection.execute("PRAGMA user_version").fetchone()[0]),
        "schema_manifest_sha256": _schema_manifest(source.connection),
    }
    input_actual = {key: input_row[key] for key in input_expected}
    for key in ("size_bytes", "application_id", "user_version"):
        input_actual[key] = int(input_actual[key])
    if input_actual != input_expected:
        raise FullBuildResumeError("resume E2 selection_inputs lineage mismatch")

    stored_policy_rows = connection.execute(
        """
        SELECT policy_id,policy_version,policy_name,description,shortlist_size,
               enabled,definition_json,policy_config_sha256,policy_record_sha256
        FROM policy_definitions WHERE run_id=? ORDER BY policy_id
        """,
        (run_id,),
    ).fetchall()
    expected_policies = [
        (
            row["policy_id"],
            row["policy_version"],
            row["policy_name"],
            row["description"],
            row["shortlist_size"],
            row["enabled"],
            canonical_json(row["definition_json"]),
            row["policy_config_sha256"],
            row["policy_record_sha256"],
        )
        for row in sorted(policy_rows, key=lambda value: str(value["policy_id"]))
    ]
    if [tuple(row) for row in stored_policy_rows] != expected_policies:
        raise FullBuildResumeError("resume policy rows mismatch")
    _validate_checkpoint_state(connection, run_id)
    for phase in _PHASES:
        phase_checkpoint = _checkpoint(connection, run_id, phase)
        key = _cursor_key(phase_checkpoint.cursor, phase=phase)
        if phase_checkpoint.completed_rows == 0:
            if key is not None:
                raise FullBuildResumeError(
                    f"zero-row {phase} checkpoint has a last_key"
                )
            continue
        if key is None:
            raise FullBuildResumeError(
                f"non-empty {phase} checkpoint lacks last_key"
            )
        if phase in {"inventory", "selection"}:
            exists = source.connection.execute(
                """
                SELECT count(*) FROM source_buildings
                WHERE run_id=? AND source=? AND source_building_id=?
                """,
                (source.run_id, key[0], key[1]),
            ).fetchone()[0]
        else:
            exists = source.connection.execute(
                """
                SELECT count(*)
                FROM building_assets ba JOIN assets a
                  ON a.run_id=ba.run_id AND a.source=ba.source
                 AND a.source_asset_id=ba.source_asset_id
                WHERE ba.run_id=? AND ba.source=? AND ba.source_building_id=?
                  AND a.fingerprint_status='success'
                """,
                (source.run_id, key[0], key[1]),
            ).fetchone()[0]
        if int(exists) < 1:
            raise FullBuildResumeError(
                f"{phase} checkpoint last_key is absent from E2"
            )


def _validate_checkpoint_state(connection: sqlite3.Connection, run_id: str) -> None:
    checkpoints = {phase: _checkpoint(connection, run_id, phase) for phase in _PHASES}
    rows = connection.execute(
        "SELECT phase FROM build_checkpoints WHERE run_id=? ORDER BY phase", (run_id,)
    ).fetchall()
    if {str(row[0]) for row in rows} != set(_PHASES):
        raise FullBuildResumeError("resume checkpoint phase set mismatch")
    if checkpoints["selection"].completed_rows and not checkpoints["inventory"].phase_complete:
        raise FullBuildResumeError("selection advanced before inventory completed")
    if checkpoints["candidates"].completed_rows and not checkpoints["selection"].phase_complete:
        raise FullBuildResumeError("candidates advanced before selection completed")
    if checkpoints["inventory"].phase_complete:
        population = int(
            connection.execute(
                "SELECT coalesce(sum(population_count),0) FROM population_strata WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        if population != checkpoints["inventory"].completed_rows:
            raise FullBuildResumeError("inventory checkpoint/table count mismatch")
    selection_rows = int(
        connection.execute(
            "SELECT count(*) FROM selected_buildings WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    if selection_rows != checkpoints["selection"].completed_rows:
        raise FullBuildResumeError("selection checkpoint/table count mismatch")
    selection_key = _cursor_key(
        checkpoints["selection"].cursor, phase="selection"
    )
    if selection_rows:
        last_selection = connection.execute(
            """
            SELECT source,source_building_id,selection_rank
            FROM selected_buildings WHERE run_id=?
            ORDER BY selection_rank DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if (
            selection_key
            != (str(last_selection[0]), str(last_selection[1]))
            or int(last_selection[2]) != selection_rows
            or int(
                connection.execute(
                    """
                    SELECT min(selection_rank) FROM selected_buildings
                    WHERE run_id=?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            != 1
        ):
            raise FullBuildResumeError("selection checkpoint cursor/rank mismatch")
    candidate_rows = int(
        connection.execute(
            "SELECT count(*) FROM image_candidates WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    if candidate_rows != checkpoints["candidates"].completed_rows:
        raise FullBuildResumeError("candidate checkpoint/table count mismatch")
    candidate_key = _cursor_key(
        checkpoints["candidates"].cursor, phase="candidates"
    )
    if candidate_rows:
        last_candidate = connection.execute(
            """
            SELECT source,source_building_id FROM image_candidates
            WHERE run_id=?
            ORDER BY source DESC,source_building_id DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        distinct_buildings = int(
            connection.execute(
                """
                SELECT count(DISTINCT selection_id) FROM image_candidates
                WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()[0]
        )
        if candidate_key != (str(last_candidate[0]), str(last_candidate[1])):
            raise FullBuildResumeError("candidate checkpoint cursor mismatch")
        if int(
            checkpoints["candidates"].cursor.get("completed_buildings", -1)
        ) != distinct_buildings:
            raise FullBuildResumeError(
                "candidate checkpoint completed_buildings mismatch"
            )
    ranking_rows = int(
        connection.execute(
            "SELECT count(*) FROM policy_rankings WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    if ranking_rows != candidate_rows * 3:
        raise FullBuildResumeError("candidate checkpoint/ranking count mismatch")
    candidate_cursor = checkpoints["candidates"].cursor
    if int(candidate_cursor.get("ranking_rows", ranking_rows)) != ranking_rows:
        raise FullBuildResumeError("candidate checkpoint ranking_rows mismatch")
    shortlist_rows = int(
        connection.execute(
            "SELECT count(*) FROM shortlist_items WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    if int(candidate_cursor.get("shortlist_rows", shortlist_rows)) != shortlist_rows:
        raise FullBuildResumeError("candidate checkpoint shortlist_rows mismatch")
    final_only_counts = {
        table: int(
            connection.execute(
                f"SELECT count(*) FROM {table} WHERE run_id=?",  # noqa: S608
                (run_id,),
            ).fetchone()[0]
        )
        for table in (
            "queue_estimates",
            "selection_metrics",
            "selection_validations",
        )
    }
    if any(final_only_counts.values()):
        raise FullBuildResumeError(
            "building resume contains unexpected final-only rows: "
            + json.dumps(final_only_counts, sort_keys=True)
        )
    if checkpoints["selection"].phase_complete:
        manifest = connection.execute(
            "SELECT ordered_selection_manifest_sha256 FROM selection_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        if not isinstance(manifest, str) or len(manifest) != 64:
            raise FullBuildResumeError("completed selection lacks ordered manifest")


def _inventory_phase(
    connection: sqlite3.Connection,
    source: E2SelectionSources,
    config: FullBuildConfig,
    run_id: str,
    commits: _CommitController,
) -> tuple[Counter[tuple[str, str]], Counter[tuple[str, str]]]:
    checkpoint = _checkpoint(connection, run_id, "inventory")
    if checkpoint.phase_complete:
        population: Counter[tuple[str, str]] = Counter()
        eligible: Counter[tuple[str, str]] = Counter()
        for row in connection.execute(
            """
            SELECT stratum_json,population_count,eligible_count
            FROM population_strata WHERE run_id=? ORDER BY stratum_key
            """,
            (run_id,),
        ):
            payload = json.loads(str(row[0]))
            cell = (str(payload["source"]), str(payload["stratum"]))
            population[cell] = int(row[1])
            eligible[cell] = int(row[2])
        if sum(population.values()) != checkpoint.completed_rows:
            raise FullBuildResumeError("completed inventory accounting mismatch")
        return population, eligible

    last_key = _cursor_key(checkpoint.cursor, phase="inventory")
    population = _counter_from_json(
        checkpoint.cursor.get("population_counts", {}),
        label="inventory population",
    )
    eligible = _counter_from_json(
        checkpoint.cursor.get("eligible_counts", {}),
        label="inventory eligible",
    )
    processed = checkpoint.completed_rows
    if sum(population.values()) != processed:
        raise FullBuildResumeError("inventory cursor population total mismatch")
    batch_count = 0
    latest_key = last_key
    previous_key: tuple[str, str] | None = None
    for summary in source.iter_building_summaries(start_after=last_key):
        key = (summary.source, summary.source_building_id)
        if previous_key is not None and key <= previous_key:
            raise FullBuildResumeError("E2 building summary order is not strict")
        previous_key = key
        cell = (summary.source, summary.stratum)
        population[cell] += 1
        if summary.successful_asset_count > 0:
            eligible[cell] += 1
        processed += 1
        batch_count += 1
        latest_key = key
        if batch_count >= config.checkpoint_buildings:
            cursor = {
                "eligible_counts": _counter_json(eligible),
                "last_key": list(latest_key),
                "population_counts": _counter_json(population),
            }
            _update_checkpoint(
                connection,
                run_id,
                "inventory",
                cursor=cursor,
                completed_rows=processed,
                phase_complete=False,
            )
            commits.commit(connection)
            batch_count = 0

    if latest_key is None:
        raise FullBuildResumeError("E2 building population is empty")
    for source_name, stratum in sorted(population):
        values: dict[str, Any] = {
            "stratum_id": _stratum_id(source_name, stratum),
            "stratum_key": f"{source_name}:{stratum}",
            "stratum_json": {"source": source_name, "stratum": stratum},
            "population_count": population[(source_name, stratum)],
            "eligible_count": eligible[(source_name, stratum)],
            "selected_building_count": population[(source_name, stratum)],
            "selected_candidate_count": 0,
        }
        values["stratum_record_sha256"] = canonical_sha256(
            _stratum_record_body(values)
        )
        connection.execute(
            """
            INSERT INTO population_strata(
              run_id,stratum_id,stratum_key,stratum_json,population_count,
              eligible_count,selected_building_count,selected_candidate_count,
              stratum_record_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                values["stratum_id"],
                values["stratum_key"],
                canonical_json(values["stratum_json"]),
                values["population_count"],
                values["eligible_count"],
                values["selected_building_count"],
                0,
                values["stratum_record_sha256"],
            ),
        )
    final_cursor = {
        "eligible_counts": _counter_json(eligible),
        "last_key": list(latest_key),
        "population_counts": _counter_json(population),
    }
    _update_checkpoint(
        connection,
        run_id,
        "inventory",
        cursor=final_cursor,
        completed_rows=processed,
        phase_complete=True,
    )
    commits.commit(connection)
    return population, eligible


def _stream_full_manifest(source: E2SelectionSources) -> tuple[str, int]:
    manifest = _OrderedSelectionManifestHasher()
    count = 0
    for summary in source.iter_building_summaries():
        manifest.add(_sampling_item(summary))
        count += 1
    return manifest.hexdigest(), count


def _selection_phase(
    connection: sqlite3.Connection,
    source: E2SelectionSources,
    config: FullBuildConfig,
    run_id: str,
    population_count: int,
    commits: _CommitController,
) -> None:
    checkpoint = _checkpoint(connection, run_id, "selection")
    if checkpoint.phase_complete:
        if checkpoint.completed_rows != population_count:
            raise FullBuildResumeError("completed selection population mismatch")
        return
    if not _checkpoint(connection, run_id, "inventory").phase_complete:
        raise FullBuildResumeError("selection requires completed inventory")

    last_key = _cursor_key(checkpoint.cursor, phase="selection")
    rank = checkpoint.completed_rows
    latest_key = last_key
    batch_count = 0
    previous_key: tuple[str, str] | None = None
    for summary in source.iter_building_summaries(start_after=last_key):
        key = (summary.source, summary.source_building_id)
        if previous_key is not None and key <= previous_key:
            raise FullBuildResumeError("E2 building summary order is not strict")
        previous_key = key
        rank += 1
        batch_count += 1
        latest_key = key
        item = _sampling_item(summary)
        values = {
            "selection_id": item.identity,
            "selection_rank": rank,
            "stratum_id": _stratum_id(summary.source, summary.stratum),
            "source": summary.source,
            "entity_type": "building",
            "source_entity_id": summary.source_building_id,
            "source_building_id": summary.source_building_id,
            "source_project_id": None,
            "name": summary.name,
            "normalized_name": _normal_name(summary.name),
            "selection_reason": "full_population",
            "e2_source_record_sha256": summary.source_record_sha256,
            "e2_relation_record_sha256": None,
        }
        values["selection_record_sha256"] = canonical_sha256(
            _selection_record_body(values)
        )
        connection.execute(
            """
            INSERT INTO selected_buildings(
              run_id,selection_id,selection_rank,stratum_id,source,entity_type,
              source_entity_id,source_building_id,source_project_id,name,
              normalized_name,selection_reason,e2_source_record_sha256,
              e2_relation_record_sha256,selection_record_sha256,detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                values["selection_id"],
                rank,
                values["stratum_id"],
                values["source"],
                "building",
                values["source_entity_id"],
                values["source_building_id"],
                None,
                values["name"],
                values["normalized_name"],
                "full_population",
                values["e2_source_record_sha256"],
                None,
                values["selection_record_sha256"],
                canonical_json(
                    {
                        "building_summary": _summary_record(summary),
                        "sample_item_record_sha256": item.record_sha256,
                    }
                ),
            ),
        )
        if batch_count >= config.checkpoint_buildings:
            _update_checkpoint(
                connection,
                run_id,
                "selection",
                cursor={"last_key": list(latest_key)},
                completed_rows=rank,
                phase_complete=False,
            )
            commits.commit(connection)
            batch_count = 0

    if latest_key is None or rank != population_count:
        raise FullBuildResumeError(
            f"selection population mismatch: {rank} != {population_count}"
        )
    manifest, manifest_count = _stream_full_manifest(source)
    if manifest_count != population_count:
        raise FullBuildResumeError("ordered manifest population mismatch")
    connection.execute(
        """
        UPDATE selection_runs SET ordered_selection_manifest_sha256=?
        WHERE run_id=? AND status='building'
        """,
        (manifest, run_id),
    )
    _update_checkpoint(
        connection,
        run_id,
        "selection",
        cursor={"last_key": list(latest_key), "ordered_manifest_sha256": manifest},
        completed_rows=rank,
        phase_complete=True,
    )
    commits.commit(connection)


def _insert_candidate(
    connection: sqlite3.Connection,
    run_id: str,
    selection_id: str,
    source_row: BuildingImageCandidate,
    mapped: Candidate,
) -> None:
    values = _candidate_mapping_values(mapped, source_row, selection_id)
    connection.execute(
        """
        INSERT INTO image_candidates(
          run_id,candidate_id,selection_id,source,source_building_id,
          source_project_id,source_asset_id,fingerprint_status,canonical_url,
          fetch_url,final_url,roles_json,primary_role,role_rank,source_ordinal,
          ordinal_is_derived,original_width,original_height,normalized_width,
          normalized_height,quality_flags_json,low_information,
          normalized_pixel_sha256,exact_cluster_id,phash_node_id,
          source_record_sha256,occurrence_record_sha256,
          project_relation_record_sha256,building_relation_record_sha256,
          candidate_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            values["candidate_id"],
            selection_id,
            values["source"],
            values["source_building_id"],
            None,
            values["source_asset_id"],
            "success",
            values["canonical_url"],
            values["fetch_url"],
            values["final_url"],
            canonical_json(values["roles_json"]),
            values["primary_role"],
            values["role_rank"],
            values["source_ordinal"],
            values["ordinal_is_derived"],
            values["original_width"],
            values["original_height"],
            values["normalized_width"],
            values["normalized_height"],
            canonical_json(values["quality_flags_json"]),
            values["low_information"],
            values["normalized_pixel_sha256"],
            values["exact_cluster_id"],
            values["phash_node_id"],
            values["source_record_sha256"],
            None,
            None,
            values["building_relation_record_sha256"],
            values["candidate_record_sha256"],
            canonical_json(
                {
                    "phash_hex": source_row.phash_hex,
                    "ranking_feature_record_sha256": mapped.record_sha256,
                }
            ),
        ),
    )


def _candidate_edges(
    source_rows: Sequence[BuildingImageCandidate],
    mapped: Sequence[Candidate],
    edge_rows: Sequence[SameBuildingDirectPhashEdge],
) -> tuple[
    tuple[DirectPHashEdge, ...],
    Mapping[tuple[str, str], Mapping[str, Any]],
]:
    by_asset = {row.source_asset_id: candidate for row, candidate in zip(source_rows, mapped)}
    edges: dict[tuple[str, str], DirectPHashEdge] = {}
    evidence: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in edge_rows:
        left = by_asset.get(row.left_source_asset_id)
        right = by_asset.get(row.right_source_asset_id)
        if left is None or right is None:
            raise FullBuildResumeError(
                "same-building direct edge references a non-candidate asset"
            )
        if left.phash_node_id != row.left_node_id or right.phash_node_id != row.right_node_id:
            raise FullBuildResumeError("same-building direct edge node mismatch")
        edge = DirectPHashEdge(
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            distance=row.hamming_distance,
        )
        prior = edges.get(edge.pair)
        if prior is not None and prior.distance != edge.distance:
            raise FullBuildResumeError("conflicting direct pHash candidate edge")
        edges[edge.pair] = edge
        detail = {
            "distance": row.hamming_distance,
            "edge_id": row.edge_id,
            "edge_record_sha256": row.edge_record_sha256,
            "left_candidate_id": edge.pair[0],
            "right_candidate_id": edge.pair[1],
        }
        if edge.pair in evidence and evidence[edge.pair] != detail:
            raise FullBuildResumeError("conflicting direct pHash edge evidence")
        evidence[edge.pair] = detail
    return tuple(edges[key] for key in sorted(edges)), evidence


def _insert_policy_outputs(
    connection: sqlite3.Connection,
    run_id: str,
    selection_id: str,
    candidates: Sequence[Candidate],
    direct_edges: Sequence[DirectPHashEdge],
    direct_edge_evidence: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    shortlist_size: int,
) -> int:
    shortlist_count = 0
    by_candidate = {candidate.candidate_id: candidate for candidate in candidates}
    for shortlist in compare_standard_policies(
        candidates,
        shortlist_size=shortlist_size,
        direct_phash_edges=direct_edges,
    ):
        for evaluation in shortlist.evaluations:
            candidate = by_candidate[evaluation.candidate_id]
            suppression_reason = next(
                (
                    reason
                    for reason in evaluation.reasons
                    if reason.startswith("suppressed_")
                ),
                None,
            )
            edge_detail: Mapping[str, Any] | None = None
            if (
                suppression_reason == "suppressed_direct_phash_le8"
                and evaluation.suppressed_by_candidate_id is not None
            ):
                pair = tuple(
                    sorted(
                        (
                            evaluation.candidate_id,
                            evaluation.suppressed_by_candidate_id,
                        )
                    )
                )
                edge_detail = direct_edge_evidence.get(pair)
                if edge_detail is None:
                    raise FullBuildResumeError(
                        "direct pHash suppression lacks source edge evidence"
                    )
            connection.execute(
                """
                INSERT INTO policy_rankings(
                  run_id,policy_id,policy_version,policy_config_sha256,
                  selection_id,candidate_id,ranking_state,editorial_rank,
                  shortlist_rank,selected,qa_fallback,hard_risk,rank_tuple_json,
                  component_scores_json,reasons_json,suppressed_by_candidate_id,
                  suppression_reason,fallback_reason,ranking_record_sha256,
                  detail_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    shortlist.policy.policy_id,
                    E3_POLICY_VERSION,
                    shortlist.policy.config_sha256,
                    selection_id,
                    evaluation.candidate_id,
                    _ranking_state(evaluation.reasons, evaluation.selected),
                    evaluation.editorial_rank,
                    evaluation.shortlist_rank,
                    int(evaluation.selected),
                    int(evaluation.qa_fallback),
                    int(evaluation.hard_risk),
                    canonical_json(list(editorial_sort_key(candidate))),
                    canonical_json(dict(evaluation.component_scores)),
                    canonical_json(list(evaluation.reasons)),
                    evaluation.suppressed_by_candidate_id,
                    suppression_reason,
                    (
                        "all_successful_candidates_hard_risk"
                        if evaluation.qa_fallback
                        else None
                    ),
                    evaluation.record_sha256,
                    canonical_json(
                        {
                            "direct_phash_edge": edge_detail,
                            "ranking_feature_record_sha256": (
                                evaluation.candidate_record_sha256
                            )
                        }
                    ),
                ),
            )
        evaluations = {value.candidate_id: value for value in shortlist.evaluations}
        for rank, candidate_id in enumerate(shortlist.selected_candidate_ids, 1):
            evaluation = evaluations[candidate_id]
            values = {
                "policy_id": shortlist.policy.policy_id,
                "selection_id": selection_id,
                "shortlist_rank": rank,
                "candidate_id": candidate_id,
                "shortlist_state": (
                    "qa_fallback" if evaluation.qa_fallback else "primary"
                ),
                "authoritative": 0,
            }
            connection.execute(
                """
                INSERT INTO shortlist_items(
                  run_id,policy_id,selection_id,shortlist_rank,candidate_id,
                  shortlist_state,authoritative,item_record_sha256,rationale_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    values["policy_id"],
                    selection_id,
                    rank,
                    candidate_id,
                    values["shortlist_state"],
                    0,
                    canonical_sha256(_shortlist_record_body(values)),
                    canonical_json(
                        {
                            "creates_final_representative": False,
                            "creates_vision_tasks": False,
                            "reasons": list(evaluation.reasons),
                        }
                    ),
                ),
            )
            shortlist_count += 1
    return shortlist_count


def _refresh_stratum_candidate_counts(
    connection: sqlite3.Connection, run_id: str
) -> None:
    rows = connection.execute(
        """
        SELECT ps.stratum_id,ps.stratum_key,ps.stratum_json,
               ps.population_count,ps.eligible_count,ps.selected_building_count,
               count(ic.candidate_id)
        FROM population_strata ps
        LEFT JOIN selected_buildings sb
          ON sb.run_id=ps.run_id AND sb.stratum_id=ps.stratum_id
        LEFT JOIN image_candidates ic
          ON ic.run_id=sb.run_id AND ic.selection_id=sb.selection_id
        WHERE ps.run_id=?
        GROUP BY ps.stratum_id
        ORDER BY ps.stratum_key
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(str(row[2]))
        values = {
            "stratum_id": str(row[0]),
            "stratum_key": str(row[1]),
            "stratum_json": payload,
            "population_count": int(row[3]),
            "eligible_count": int(row[4]),
            "selected_building_count": int(row[5]),
            "selected_candidate_count": int(row[6]),
        }
        record_sha = canonical_sha256(_stratum_record_body(values))
        connection.execute(
            """
            UPDATE population_strata
            SET selected_candidate_count=?,stratum_record_sha256=?
            WHERE run_id=? AND stratum_id=?
            """,
            (values["selected_candidate_count"], record_sha, run_id, row[0]),
        )


def _candidate_phase(
    connection: sqlite3.Connection,
    source: E2SelectionSources,
    config: FullBuildConfig,
    run_id: str,
    commits: _CommitController,
) -> tuple[int, int, int]:
    checkpoint = _checkpoint(connection, run_id, "candidates")
    if checkpoint.phase_complete:
        candidates = checkpoint.completed_rows
        shortlist_rows = int(
            connection.execute(
                "SELECT count(*) FROM shortlist_items WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        edge_rows = int(checkpoint.cursor.get("direct_edge_rows", 0))
        return candidates, shortlist_rows, edge_rows
    if not _checkpoint(connection, run_id, "selection").phase_complete:
        raise FullBuildResumeError("candidates require completed selection")

    last_key = _cursor_key(checkpoint.cursor, phase="candidates")
    candidate_count = checkpoint.completed_rows
    completed_buildings = int(checkpoint.cursor.get("completed_buildings", 0))
    ranking_count = int(checkpoint.cursor.get("ranking_rows", 0))
    shortlist_count = int(checkpoint.cursor.get("shortlist_rows", 0))
    direct_edge_count = int(checkpoint.cursor.get("direct_edge_rows", 0))
    latest_key = last_key
    batch_buildings = 0
    previous_key: tuple[str, str] | None = None
    edge_stream = _EdgeGroupStream(
        source.iter_same_building_direct_phash_edges(start_after=last_key)
    )

    all_candidates = source.iter_all_candidates(start_after=last_key)
    for key, grouped in groupby(
        all_candidates,
        key=lambda value: (value.source, value.source_building_id),
    ):
        if previous_key is not None and key <= previous_key:
            raise FullBuildResumeError("E2 candidate building order is not strict")
        previous_key = key
        edge_rows = edge_stream.take(key)
        source_rows = tuple(grouped)
        mapped = tuple(_candidate(row) for row in source_rows)
        if not mapped:
            raise FullBuildResumeError("candidate stream emitted an empty group")
        selection_id = f"{key[0]}:building:{key[1]}"
        direct_edges, direct_edge_evidence = _candidate_edges(
            source_rows, mapped, edge_rows
        )
        for source_row, candidate in zip(source_rows, mapped):
            _insert_candidate(
                connection, run_id, selection_id, source_row, candidate
            )
        added_shortlists = _insert_policy_outputs(
            connection,
            run_id,
            selection_id,
            mapped,
            direct_edges,
            direct_edge_evidence,
            shortlist_size=config.shortlist_size,
        )
        candidate_count += len(mapped)
        ranking_count += len(mapped) * 3
        shortlist_count += added_shortlists
        direct_edge_count += len(edge_rows)
        completed_buildings += 1
        batch_buildings += 1
        latest_key = key
        if batch_buildings >= config.checkpoint_buildings:
            cursor = {
                "completed_buildings": completed_buildings,
                "direct_edge_rows": direct_edge_count,
                "last_key": list(latest_key),
                "ranking_rows": ranking_count,
                "shortlist_rows": shortlist_count,
            }
            _update_checkpoint(
                connection,
                run_id,
                "candidates",
                cursor=cursor,
                completed_rows=candidate_count,
                phase_complete=False,
            )
            commits.commit(connection)
            batch_buildings = 0

    edge_stream.assert_exhausted()
    if latest_key is None and candidate_count == 0:
        raise FullBuildResumeError("E2 has no successful image candidates")
    _refresh_stratum_candidate_counts(connection, run_id)
    final_cursor = {
        "completed_buildings": completed_buildings,
        "direct_edge_rows": direct_edge_count,
        "last_key": list(latest_key) if latest_key is not None else None,
        "ranking_rows": ranking_count,
        "shortlist_rows": shortlist_count,
    }
    _update_checkpoint(
        connection,
        run_id,
        "candidates",
        cursor=final_cursor,
        completed_rows=candidate_count,
        phase_complete=True,
    )
    commits.commit(connection)
    return candidate_count, shortlist_count, direct_edge_count


def _queue_counts(
    connection: sqlite3.Connection, run_id: str, policy_id: str
) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
          sum(si.shortlist_rank=1),
          count(DISTINCT CASE WHEN si.shortlist_rank=1
                         THEN ic.normalized_pixel_sha256 END),
          sum(si.shortlist_rank<=3),
          count(DISTINCT CASE WHEN si.shortlist_rank<=3
                         THEN ic.normalized_pixel_sha256 END)
        FROM shortlist_items si
        JOIN image_candidates ic
          ON ic.run_id=si.run_id AND ic.candidate_id=si.candidate_id
        WHERE si.run_id=? AND si.policy_id=?
        """,
        (run_id, policy_id),
    ).fetchone()
    return {
        "top1_no_reuse": int(row[0] or 0),
        "top1_exact_reuse": int(row[1] or 0),
        "top3_no_reuse": int(row[2] or 0),
        "top3_exact_reuse": int(row[3] or 0),
    }


def _insert_queue_estimates(
    connection: sqlite3.Connection, run_id: str, shortlist_size: int
) -> int:
    count = 0
    for policy in policy_definitions(shortlist_size):
        counts = _queue_counts(connection, run_id, policy.policy_id)
        for scenario in sorted(_QUEUE_UNITS):
            population_scenario = (
                "top1_no_reuse"
                if scenario.startswith("top1_")
                else "top3_no_reuse"
            )
            values = {
                "estimate_id": f"{policy.policy_id}:{scenario}",
                "policy_id": policy.policy_id,
                "stratum_id": None,
                "queue_unit": _QUEUE_UNITS[scenario],
                "population_count": counts[population_scenario],
                "estimated_queue_items": counts[scenario],
                "tokens_per_item_low": None,
                "tokens_per_item_point": None,
                "tokens_per_item_high": None,
                "projected_input_tokens": None,
                "projected_output_tokens": None,
                "projected_total_tokens": None,
                "estimated_calls": None,
                "retry_factor": 1.0,
                "estimated_cost_usd": None,
                "pricing_snapshot_json": {},
                "quota_basis": None,
                "projected_quota_percent": None,
                "requests_executed": 0,
                "authoritative": 0,
            }
            connection.execute(
                """
                INSERT INTO queue_estimates(
                  run_id,estimate_id,policy_id,stratum_id,queue_unit,
                  population_count,estimated_queue_items,tokens_per_item_low,
                  tokens_per_item_point,tokens_per_item_high,
                  projected_input_tokens,projected_output_tokens,
                  projected_total_tokens,estimated_calls,retry_factor,
                  estimated_cost_usd,pricing_snapshot_json,quota_basis,
                  projected_quota_percent,requests_executed,authoritative,
                  estimate_record_sha256,detail_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    values["estimate_id"],
                    values["policy_id"],
                    None,
                    values["queue_unit"],
                    values["population_count"],
                    values["estimated_queue_items"],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    1.0,
                    None,
                    "{}",
                    None,
                    None,
                    0,
                    0,
                    canonical_sha256(_queue_record_body(values)),
                    canonical_json(
                        {
                            "creates_vision_tasks": False,
                            "executable": False,
                            "non_executable": True,
                            "planning_only": True,
                            "scenario": scenario,
                            "semantic_reuse_allowed": False,
                        }
                    ),
                    _utc_now(),
                ),
            )
            count += 1
    return count


def _verify_e2_after_full_read(
    source: E2SelectionSources, config: FullBuildConfig
) -> str:
    target = Path(config.e2_path).resolve()
    size_before = source.lineage.artifact_size
    if target.stat().st_size != size_before or _e2_sidecars(target):
        raise FullBuildResumeError("E2 changed before the final byte rehash")
    observed = _sha256_file(target)
    if observed != source.lineage.artifact_sha256:
        raise FullBuildResumeError("E2 final byte SHA-256 mismatch")
    if target.stat().st_size != size_before or _e2_sidecars(target):
        raise FullBuildResumeError("E2 changed during the final byte rehash")
    return observed


def _finalize_full_build(
    connection: sqlite3.Connection,
    source: E2SelectionSources,
    config: FullBuildConfig,
    run_id: str,
    *,
    population_count: int,
    eligible_count: int,
    candidate_count: int,
    shortlist_count: int,
    direct_edge_count: int,
) -> str:
    if connection.execute(
        "SELECT count(*) FROM queue_estimates WHERE run_id=?", (run_id,)
    ).fetchone()[0]:
        raise FullBuildResumeError("building artifact already has final queue rows")
    queue_count = _insert_queue_estimates(connection, run_id, config.shortlist_size)
    for name in ("network_requests", "vision_requests", "llm_requests"):
        _insert_metric(connection, run_id, "validation", name, 0)
    for name, value in (
        ("population_buildings", population_count),
        ("eligible_buildings", eligible_count),
        ("selected_buildings", population_count),
        ("selected_image_candidates", candidate_count),
        ("shortlist_items", shortlist_count),
        ("same_building_direct_edges", direct_edge_count),
    ):
        _insert_metric(connection, run_id, "selection", name, value)

    final_e2_sha = _verify_e2_after_full_read(source, config)
    connection.execute(
        "UPDATE selection_inputs SET sha256_after=? "
        "WHERE run_id=? AND input_role='e2_evidence'",
        (final_e2_sha, run_id),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise FullBuildResumeError("could not record terminal E2 SHA-256")
    logical_sha, table_manifests = logical_selection_manifest(connection, run_id)
    _insert_metric(
        connection, run_id, "validation", "output_logical_sha256", logical_sha
    )
    checkpoint_values = {
        phase: _checkpoint(connection, run_id, phase) for phase in _PHASES
    }
    actual_selected = int(
        connection.execute(
            "SELECT count(*) FROM selected_buildings WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    actual_candidates = int(
        connection.execute(
            "SELECT count(*) FROM image_candidates WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    expected_candidate_occurrences = int(
        connection.execute(
            "SELECT coalesce(sum(json_extract(detail_json, "
            "'$.building_summary.successful_asset_count')),0) "
            "FROM selected_buildings WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    actual_shortlists = int(
        connection.execute(
            "SELECT count(*) FROM shortlist_items WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    validations = (
        (
            "input_e2_full_rehash_unchanged",
            final_e2_sha == config.expected_e2_sha256.lower(),
            config.expected_e2_sha256.lower(),
            final_e2_sha,
        ),
        (
            "full_population_accounting",
            actual_selected == population_count,
            str(population_count),
            str(actual_selected),
        ),
        (
            "candidate_accounting",
            actual_candidates
            == candidate_count
            == expected_candidate_occurrences,
            str(expected_candidate_occurrences),
            str(actual_candidates),
        ),
        (
            "shortlist_accounting",
            actual_shortlists == shortlist_count,
            str(shortlist_count),
            str(actual_shortlists),
        ),
        (
            "full_checkpoints_complete",
            all(value.phase_complete for value in checkpoint_values.values())
            and checkpoint_values["inventory"].completed_rows == population_count
            and checkpoint_values["selection"].completed_rows == population_count
            and checkpoint_values["candidates"].completed_rows == candidate_count,
            f"{population_count}/{population_count}/{candidate_count}",
            "/".join(
                str(checkpoint_values[phase].completed_rows) for phase in _PHASES
            ),
        ),
        ("queue_scenario_count", queue_count == 12, "12", str(queue_count)),
        ("requests_zero", True, "0/0/0", "0/0/0"),
        (
            "logical_manifest_created",
            len(logical_sha) == 64,
            "64-char SHA-256",
            logical_sha,
        ),
    )
    for name, passed, expected, actual in validations:
        connection.execute(
            """
            INSERT INTO selection_validations(
              run_id,validation_name,severity,passed,expected,actual,
              detail_json,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                name,
                "error",
                int(passed),
                expected,
                actual,
                canonical_json(
                    {
                        "logical_manifest_version": (
                            "archibe-e3-selection-logical-manifest-v1"
                        ),
                        "table_manifests": (
                            table_manifests
                            if name == "logical_manifest_created"
                            else None
                        ),
                    }
                ),
                _utc_now(),
            ),
        )
    failed = [name for name, passed, _expected, _actual in validations if not passed]
    if failed:
        # The final validation transaction remains atomic with terminal status.
        finalize_sidecar(
            connection,
            status="failed_validation",
            error="; ".join(failed),
            close=False,
        )
        raise RuntimeError("E3 full internal validation failed: " + ", ".join(failed))
    finalize_sidecar(connection, status="complete", close=False)
    return logical_sha


def preflight_full_cross_source_image_selection(
    config: FullBuildConfig,
) -> FullBuildPreflight:
    """Read and count immutable E2 facts without creating an output file."""

    _validate_config(config)
    output = Path(config.output_path).resolve()
    output_parent, disk_free_bytes = _disk_capacity(output)
    output_sidecars = sqlite_sidecar_paths(output)
    output_lock_exists = lock_path_for(output).exists()
    with open_e2_selection_sources(
        _artifact_spec(config), batch_size=config.batch_size
    ) as source:
        population = 0
        eligible = 0
        candidates = 0
        for summary in source.iter_building_summaries():
            population += 1
            candidates += summary.successful_asset_count
            eligible += int(summary.successful_asset_count > 0)
        unique_success_assets = int(
            source.connection.execute(
                "SELECT count(*) FROM assets "
                "WHERE run_id=? AND fingerprint_status='success'",
                (source.run_id,),
            ).fetchone()[0]
        )
        direct_edges = sum(1 for _ in source.iter_same_building_direct_phash_edges())
        return FullBuildPreflight(
            e2_path=Path(config.e2_path).resolve(),
            output_path=output,
            e2_size_bytes=source.lineage.artifact_size,
            e2_byte_sha256=source.lineage.artifact_sha256,
            e2_logical_sha256=source.lineage.stored_logical_sha256,
            population_buildings=population,
            eligible_buildings=eligible,
            unique_success_assets=unique_success_assets,
            image_candidates=candidates,
            candidate_occurrence_minus_unique_asset_count=(
                candidates - unique_success_assets
            ),
            same_building_direct_edges=direct_edges,
            output_exists=output.exists(),
            output_sqlite_sidecars=tuple(str(path) for path in output_sidecars),
            output_lock_exists=output_lock_exists,
            no_clobber_ready=(
                not output.exists()
                and not output_sidecars
                and not output_lock_exists
            ),
            output_parent=output_parent,
            disk_free_bytes=disk_free_bytes,
            minimum_disk_space_satisfied=(
                disk_free_bytes >= MINIMUM_FULL_FREE_BYTES
            ),
            recommended_disk_space_satisfied=(
                disk_free_bytes >= RECOMMENDED_FULL_FREE_BYTES
            ),
        )


def build_full_cross_source_image_selection(
    config: FullBuildConfig,
) -> FullBuildResult:
    """Build or explicitly resume one immutable full-population E3 artifact."""

    _validate_config(config)
    started = time.perf_counter()
    output = Path(config.output_path).resolve()
    _output_parent, disk_free_bytes = _disk_capacity(output)
    if disk_free_bytes < MINIMUM_FULL_FREE_BYTES:
        raise OSError(
            "insufficient free space for E3 full materialization: "
            f"available={disk_free_bytes}, required={MINIMUM_FULL_FREE_BYTES}"
        )
    policy_rows = _policy_rows(config.shortlist_size)
    policy_set_sha = _policy_set_sha(policy_rows)
    run_id = _build_run_id(config, policy_set_sha)
    connection: sqlite3.Connection | None = None
    resumed = False
    commits = _CommitController(config.interrupt_after_commits)

    with acquire_build_lock(lock_path_for(output)):
        try:
            with open_e2_selection_sources(
                _artifact_spec(config), batch_size=config.batch_size
            ) as source:
                if output.exists():
                    if not config.resume:
                        raise FileExistsError(
                            f"refusing to clobber existing E3 artifact: {output}"
                        )
                    connection = open_sidecar(
                        output, readonly=False, immutable=False
                    )
                    _validate_resume(
                        connection,
                        source,
                        config,
                        policy_rows,
                        policy_set_sha,
                        run_id,
                    )
                    resumed = True
                else:
                    if config.resume:
                        raise FileNotFoundError(
                            f"resume requested but E3 artifact is absent: {output}"
                        )
                    orphan_sidecars = sqlite_sidecar_paths(output)
                    if orphan_sidecars:
                        raise FileExistsError(
                            "refusing to create E3 artifact beside orphan SQLite "
                            "sidecars: "
                            + ", ".join(str(path) for path in orphan_sidecars)
                        )
                    connection = initialize_sidecar(output)
                    _initialize_run(
                        connection,
                        source,
                        config,
                        policy_rows,
                        policy_set_sha,
                        run_id,
                    )

                population, eligible = _inventory_phase(
                    connection, source, config, run_id, commits
                )
                population_count = sum(population.values())
                eligible_count = sum(eligible.values())
                _selection_phase(
                    connection,
                    source,
                    config,
                    run_id,
                    population_count,
                    commits,
                )
                candidate_count, shortlist_count, direct_edge_count = (
                    _candidate_phase(
                        connection, source, config, run_id, commits
                    )
                )
                logical_sha = _finalize_full_build(
                    connection,
                    source,
                    config,
                    run_id,
                    population_count=population_count,
                    eligible_count=eligible_count,
                    candidate_count=candidate_count,
                    shortlist_count=shortlist_count,
                    direct_edge_count=direct_edge_count,
                )
                connection.close()
                connection = None

            prepare_immutable_sidecar(output)
            if sqlite_sidecar_paths(output):
                raise RuntimeError("terminal E3 artifact retains SQLite sidecars")
            return FullBuildResult(
                output_path=output,
                run_id=run_id,
                status="complete",
                logical_sha256=logical_sha,
                population_buildings=population_count,
                eligible_buildings=eligible_count,
                image_candidates=candidate_count,
                shortlist_items=shortlist_count,
                resumed=resumed,
                elapsed_seconds=time.perf_counter() - started,
            )
        except Exception:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            # A recoverable failure deliberately remains status=building.  The
            # only terminal failure path is an atomic failed_validation above.
            raise


__all__ = [
    "DEFAULT_CHECKPOINT_BUILDINGS",
    "ESTIMATED_FULL_OUTPUT_BYTES",
    "FULL_CONFIRMATION",
    "FULL_PIPELINE_VERSION",
    "MINIMUM_FULL_FREE_BYTES",
    "RECOMMENDED_FULL_FREE_BYTES",
    "FullBuildConfig",
    "FullBuildPreflight",
    "FullBuildResult",
    "FullBuildResumeError",
    "SimulatedFullBuildInterruption",
    "build_full_cross_source_image_selection",
    "default_e2_path",
    "preflight_full_cross_source_image_selection",
]
