from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Callable

import pytest
from PIL import Image

import canonical.image_fingerprint_merge as image_fingerprint_merge
from canonical.image_fingerprint_adapters import SourceAsset
from canonical.image_fingerprint_merge import (
    MERGE_LINEAGE_KIND,
    merge_image_fingerprint_recoveries,
    validate_image_fingerprint_recovery_merge,
)
from canonical.image_fingerprint_pipeline import (
    FetchFailure,
    FetchResponse,
    PipelineError,
    run_image_fingerprint_pipeline,
)
from canonical.image_fingerprint_recovery import (
    ALL_NON404_PLUS_404_SAMPLE,
    RECOVERY_LINEAGE_ADDITIVE_FIELDS,
    RecoveryError,
    run_failure_recovery,
    validate_failure_recovery_sidecar,
)
from canonical.image_fingerprint_sidecar import open_sidecar
from canonical.image_fingerprint_validator import validate_image_fingerprint_sidecar


def _png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (80, 60), color)
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def _source_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES('immutable source')")
        connection.commit()
    finally:
        connection.close()


def _asset(number: int, *, width: int = 3) -> SourceAsset:
    asset_id = f"asset-{number:0{width}d}"
    source_url = f"https://images.divisare.com/images/v1/{asset_id}/image.jpg"
    return SourceAsset(
        source="divisare",
        source_asset_id=asset_id,
        source_asset_key=asset_id,
        normalized_url=source_url,
        selected_raw_url=source_url,
        effective_fetch_url=(
            "https://images.divisare.com/images/"
            f"c_limit,f_jpg,h_1024,q_85,w_1024/v1/{asset_id}/image.jpg"
        ),
        source_urls=(source_url,),
        occurrence_count=1,
        parent_count=1,
        roles=("gallery",),
        format_lane="raster",
        fetch_profile_version="fixture-max1024-v1",
    )


class _BaseFetcher:
    def __init__(self) -> None:
        self.network_requests = 0

    def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
        del attempt_no
        self.network_requests += 1
        number = int(asset.source_asset_id.rsplit("-", 1)[1])
        if number >= 2:
            raise FetchFailure(
                "http_404",
                "fixture HTTP 404",
                http_status=404,
                final_url=asset.effective_fetch_url,
            )
        return FetchResponse(
            200,
            asset.effective_fetch_url,
            "image/png",
            _png((30 + number, 80, 160)),
        )


class _RecoveryFetcher:
    def __init__(self) -> None:
        self.network_requests = 0

    def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
        del attempt_no
        self.network_requests += 1
        if asset.source_asset_id == "asset-003":
            raise FetchFailure(
                "http_404",
                "still absent",
                http_status=404,
                final_url=asset.effective_fetch_url,
            )
        return FetchResponse(
            200,
            asset.effective_fetch_url,
            "image/png",
            _png((200, 70, 40)),
        )


def _fingerprints(path: Path) -> dict[str, tuple[object, ...]]:
    connection = open_sidecar(path, readonly=True)
    try:
        rows = connection.execute(
            """
            SELECT source_asset_id,status,selected_attempt_no,raw_response_sha256,
                   normalized_pixel_sha256,phash_hex,decoded_format,original_width,
                   original_height,normalized_width,normalized_height,metadata_json,
                   completed_at,error_kind,error_message
            FROM fingerprints ORDER BY source_asset_id
            """
        ).fetchall()
        return {str(row[0]): tuple(row[1:]) for row in rows}
    finally:
        connection.close()


def _merge_inputs(
    tmp_path: Path,
    count: int = 4,
    *,
    recovery_sample_size: int = 100,
) -> tuple[Path, Path, Path, tuple[SourceAsset, ...]]:
    source = tmp_path / "source.db"
    base = tmp_path / "base.db"
    recovery = tmp_path / "recovery.db"
    _source_db(source)
    width = 5 if count > 999 else 3
    assets = tuple(_asset(index, width=width) for index in range(count))
    run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=base,
        sample_size=None,
        inventory_factory=lambda: iter(assets),
        fetcher=_BaseFetcher(),
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    run_failure_recovery(
        source="divisare",
        source_db=source,
        base_sidecar=base,
        output=recovery,
        strategy=ALL_NON404_PLUS_404_SAMPLE,
        http_404_sample_size=recovery_sample_size,
        inventory_factory=lambda: iter(assets),
        fetcher=_RecoveryFetcher(),
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    return source, base, recovery, assets


def _merge_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, tuple[SourceAsset, ...]]:
    source, base, recovery, assets = _merge_inputs(tmp_path)
    merged = tmp_path / "merged.db"
    merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(recovery,),
        output=merged,
        inventory_factory=lambda: iter(assets),
    )
    return source, base, recovery, merged, assets


def test_recovery_merge_is_new_valid_artifact_and_never_clobbers_success(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    base = tmp_path / "base.db"
    recovery = tmp_path / "recovery.db"
    merged = tmp_path / "merged.db"
    _source_db(source)
    assets = tuple(_asset(index) for index in range(4))
    run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=base,
        sample_size=None,
        inventory_factory=lambda: iter(assets),
        fetcher=_BaseFetcher(),
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    base_sha = hashlib.sha256(base.read_bytes()).hexdigest()
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    before = _fingerprints(base)
    recovery_result = run_failure_recovery(
        source="divisare",
        source_db=source,
        base_sidecar=base,
        output=recovery,
        strategy=ALL_NON404_PLUS_404_SAMPLE,
        http_404_sample_size=100,
        inventory_factory=lambda: iter(assets),
        fetcher=_RecoveryFetcher(),
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    assert recovery_result.status_counts == {"failed": 1, "success": 1}
    recovery_sha = hashlib.sha256(recovery.read_bytes()).hexdigest()

    result = merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(recovery,),
        output=merged,
        inventory_factory=lambda: iter(assets),
    )
    assert result.base_success == 2
    assert result.base_failed == 2
    assert result.recovered == 1
    assert result.final_success == 3
    assert result.final_failed == 1
    assert result.run_status == "complete_with_failures"
    assert result.base_sidecar_sha256 == base_sha
    assert result.recovery_sidecar_sha256s == (recovery_sha,)
    assert hashlib.sha256(base.read_bytes()).hexdigest() == base_sha
    assert hashlib.sha256(recovery.read_bytes()).hexdigest() == recovery_sha
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha

    after = _fingerprints(merged)
    assert after["asset-000"] == before["asset-000"]
    assert after["asset-001"] == before["asset-001"]
    assert before["asset-002"][0] == "failed"
    assert after["asset-002"][0] == "success"
    assert after["asset-003"][0] == "failed"

    validation = validate_image_fingerprint_sidecar(
        merged,
        source,
        inventory_factory=lambda: iter(assets),
    )
    assert validation.passed
    connection = open_sidecar(merged, readonly=True)
    try:
        attempts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT source_asset_id,count(*) FROM fetch_attempts GROUP BY source_asset_id"
            )
        }
        assert attempts == {
            "asset-000": 1,
            "asset-001": 1,
            "asset-002": 2,
            "asset-003": 2,
        }
        assert connection.execute(
            "SELECT count(*) FROM recovery_merge_decisions"
        ).fetchone()[0] == 2
    finally:
        connection.close()

    with pytest.raises(FileExistsError, match="clobber"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery,),
            output=merged,
            inventory_factory=lambda: iter(assets),
        )


def _rewrite_terminal_dependency(
    path: Path, mutate: Callable[[dict[str, object]], None]
) -> None:
    connection = sqlite3.connect(path)
    try:
        trigger_names = (
            "fingerprint_runs_provenance_immutable",
            "fingerprint_runs_terminal_immutable",
        )
        trigger_sql = [
            str(
                connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()[0]
            )
            for name in trigger_names
        ]
        for name in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        raw = str(
            connection.execute(
                "SELECT dependency_manifest_json FROM fingerprint_runs"
            ).fetchone()[0]
        )
        document = json.loads(raw)
        mutate(document)
        updated = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        connection.execute(
            """UPDATE fingerprint_runs SET dependency_manifest_json=?,
               dependency_manifest_sha256=?""",
            (updated, hashlib.sha256(updated.encode("ascii")).hexdigest()),
        )
        for statement in trigger_sql:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _mutate_terminal_rows(
    path: Path,
    *,
    trigger_names: tuple[str, ...],
    statement: str,
) -> None:
    """Apply a controlled corruption and restore the production triggers."""

    connection = sqlite3.connect(path)
    try:
        trigger_sql = [
            str(
                connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()[0]
            )
            for name in trigger_names
        ]
        for name in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(statement)
        for trigger in trigger_sql:
            connection.execute(trigger)
        connection.commit()
    finally:
        connection.close()


def test_merge_dependency_lineage_offsets_triggers_and_independent_validator(
    tmp_path: Path,
) -> None:
    source, base, recovery, merged, assets = _merge_fixture(tmp_path)
    validation = validate_image_fingerprint_recovery_merge(
        merged,
        source,
        base,
        (recovery,),
        inventory_factory=lambda: iter(assets),
    )
    assert validation.passed, validation.checks

    connection = open_sidecar(merged, readonly=True)
    try:
        dependency = json.loads(
            connection.execute(
                "SELECT dependency_manifest_json FROM fingerprint_runs"
            ).fetchone()[0]
        )
        lineage = dependency["_run_lineage"]
        assert lineage["kind"] == MERGE_LINEAGE_KIND
        assert lineage["base_sidecar_sha256"] == hashlib.sha256(
            base.read_bytes()
        ).hexdigest()
        assert lineage["recovery_sidecar_sha256s"] == [
            hashlib.sha256(recovery.read_bytes()).hexdigest()
        ]
        decisions = {
            str(row[0]): (int(row[1]), int(row[2]))
            for row in connection.execute(
                """SELECT source_asset_id,attempt_offset,attempt_count
                   FROM recovery_merge_decisions"""
            )
        }
        assert decisions == {"asset-002": (1, 1), "asset-003": (1, 1)}
        assert [
            int(row[0])
            for row in connection.execute(
                """SELECT attempt_no FROM fetch_attempts
                   WHERE source_asset_id='asset-002' ORDER BY attempt_no"""
            )
        ] == [1, 2]
    finally:
        connection.close()

    writable = sqlite3.connect(merged)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            writable.execute(
                "UPDATE recovery_merge_lineage SET manifest_json='{}'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            writable.execute("DELETE FROM recovery_merge_decisions")
    finally:
        writable.close()


def test_merge_validator_detects_lineage_tamper(tmp_path: Path) -> None:
    source, base, recovery, merged, assets = _merge_fixture(tmp_path)
    tampered = tmp_path / "tampered-merged.db"
    shutil.copyfile(merged, tampered)
    connection = sqlite3.connect(tampered)
    try:
        trigger_sql = str(
            connection.execute(
                """SELECT sql FROM sqlite_schema WHERE type='trigger'
                   AND name='recovery_merge_lineage_immutable_update'"""
            ).fetchone()[0]
        )
        connection.execute(
            "DROP TRIGGER recovery_merge_lineage_immutable_update"
        )
        connection.execute(
            "UPDATE recovery_merge_lineage SET manifest_json='{}'"
        )
        connection.execute(trigger_sql)
        connection.commit()
    finally:
        connection.close()
    validation = validate_image_fingerprint_recovery_merge(
        tampered,
        source,
        base,
        (recovery,),
        inventory_factory=lambda: iter(assets),
    )
    assert not validation.passed
    assert any(
        name == "merge_lineage_manifest" and not passed
        for name, passed, _, _ in validation.checks
    )


@pytest.mark.parametrize(
    ("case", "trigger_names", "statement", "failed_check"),
    (
        (
            "mutated-base-attempt",
            ("fetch_attempts_immutable_update",),
            """UPDATE fetch_attempts SET error_message='tampered base evidence'
               WHERE source_asset_id='asset-000' AND attempt_no=1""",
            "base_attempt_prefix_exact",
        ),
        (
            "missing-base-attempt",
            ("fetch_attempts_immutable_delete",),
            """DELETE FROM fetch_attempts
               WHERE source_asset_id='asset-003' AND attempt_no=1""",
            "base_attempt_prefix_exact",
        ),
        (
            "unauthorized-extra-attempt",
            ("fetch_attempts_running_insert",),
            """
            INSERT INTO fetch_attempts(
              run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
              elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
              raw_response_sha256,error_kind,error_message,retry_after_seconds,
              scheduled_delay_seconds,worker_no
            )
            SELECT run_id,source_asset_id,2,request_url,started_at,completed_at,
                   elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
                   raw_response_sha256,error_kind,error_message,retry_after_seconds,
                   scheduled_delay_seconds,worker_no
            FROM fetch_attempts
            WHERE source_asset_id='asset-000' AND attempt_no=1
            """,
            "merge_attempt_accounting",
        ),
    ),
)
def test_merge_validator_detects_base_attempt_ledger_tamper(
    tmp_path: Path,
    case: str,
    trigger_names: tuple[str, ...],
    statement: str,
    failed_check: str,
) -> None:
    source, base, recovery, merged, assets = _merge_fixture(tmp_path)
    tampered = tmp_path / f"{case}.db"
    shutil.copyfile(merged, tampered)
    _mutate_terminal_rows(
        tampered,
        trigger_names=trigger_names,
        statement=statement,
    )

    validation = validate_image_fingerprint_recovery_merge(
        tampered,
        source,
        base,
        (recovery,),
        inventory_factory=lambda: iter(assets),
    )
    assert not validation.passed
    assert any(
        name == failed_check and not passed
        for name, passed, _, _ in validation.checks
    ), validation.checks


def test_merge_validator_detects_unselected_failed_fingerprint_tamper(
    tmp_path: Path,
) -> None:
    source, base, recovery, assets = _merge_inputs(
        tmp_path,
        recovery_sample_size=1,
    )
    merged = tmp_path / "sample-merged.db"
    merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(recovery,),
        output=merged,
        inventory_factory=lambda: iter(assets),
    )
    connection = open_sidecar(merged, readonly=True)
    try:
        untouched = str(
            connection.execute(
                """
                SELECT f.source_asset_id
                FROM fingerprints AS f
                LEFT JOIN recovery_merge_decisions AS d
                  ON d.merge_id=f.run_id AND d.source_asset_id=f.source_asset_id
                WHERE f.status='failed' AND d.source_asset_id IS NULL
                ORDER BY f.source_asset_id LIMIT 1
                """
            ).fetchone()[0]
        )
    finally:
        connection.close()

    tampered = tmp_path / "untouched-failed-tamper.db"
    shutil.copyfile(merged, tampered)
    _mutate_terminal_rows(
        tampered,
        trigger_names=("fingerprints_running_update",),
        statement=(
            "UPDATE fingerprints SET error_message='tampered untouched failure' "
            f"WHERE source_asset_id='{untouched}'"
        ),
    )
    validation = validate_image_fingerprint_recovery_merge(
        tampered,
        source,
        base,
        (recovery,),
        inventory_factory=lambda: iter(assets),
    )
    assert not validation.passed
    assert any(
        name == "base_unrecovered_fingerprints_exact" and not passed
        for name, passed, _, _ in validation.checks
    ), validation.checks


@pytest.mark.parametrize("unsupported_input", ("base", "recovery"))
def test_merge_rejects_skipped_status_before_output_creation(
    tmp_path: Path,
    unsupported_input: str,
) -> None:
    source, base, recovery, assets = _merge_inputs(tmp_path)
    target = base if unsupported_input == "base" else recovery
    source_asset_id = "asset-003"
    _mutate_terminal_rows(
        target,
        trigger_names=("fingerprints_running_update",),
        statement=(
            "UPDATE fingerprints SET status='skipped' "
            f"WHERE source_asset_id='{source_asset_id}'"
        ),
    )
    output = tmp_path / f"never-{unsupported_input}-skipped.db"
    with pytest.raises(PipelineError, match="unsupported fingerprint statuses"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery,),
            output=output,
            inventory_factory=lambda: iter(assets),
        )
    assert not output.exists()
    assert not Path(str(output) + ".partial").exists()


def test_merge_rejects_dependency_mismatch_lineage_spoof_and_overlap(
    tmp_path: Path,
) -> None:
    source, base, recovery, _merged, assets = _merge_fixture(tmp_path)

    dependency_mismatch = tmp_path / "dependency-mismatch.db"
    shutil.copyfile(recovery, dependency_mismatch)
    _rewrite_terminal_dependency(
        dependency_mismatch,
        lambda document: document.__setitem__("spoof_dependency", "1"),
    )
    with pytest.raises(PipelineError, match="dependency|lineage validation"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(dependency_mismatch,),
            output=tmp_path / "never-dependency.db",
            inventory_factory=lambda: iter(assets),
        )


def test_legacy_lineage_upgrade_is_explicit_derived_and_bound(
    tmp_path: Path,
) -> None:
    source, base, recovery, _merged, assets = _merge_fixture(tmp_path)
    legacy = tmp_path / "legacy-recovery.db"
    shutil.copyfile(recovery, legacy)

    def remove_additive(document: dict[str, object]) -> None:
        lineage = document["_run_lineage"]
        assert isinstance(lineage, dict)
        for field in RECOVERY_LINEAGE_ADDITIVE_FIELDS:
            lineage.pop(field)

    _rewrite_terminal_dependency(legacy, remove_additive)
    with pytest.raises(RecoveryError, match="missing required fields"):
        validate_failure_recovery_sidecar(
            legacy,
            source,
            base,
            inventory_factory=lambda: iter(assets),
        )

    output = tmp_path / "legacy-upgraded-merge.db"
    result = merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(legacy,),
        output=output,
        inventory_factory=lambda: iter(assets),
    )
    assert result.recovered == 1
    validation = validate_image_fingerprint_recovery_merge(
        output,
        source,
        base,
        (legacy,),
        inventory_factory=lambda: iter(assets),
    )
    assert validation.passed, validation.checks
    connection = open_sidecar(output, readonly=True)
    try:
        dependency = json.loads(
            connection.execute(
                "SELECT dependency_manifest_json FROM fingerprint_runs"
            ).fetchone()[0]
        )
        bound = dependency["_run_lineage"]["recovery_lineage_validations"][0]
        assert bound["validation_mode"] == "legacy_additive_upgrade"
        assert set(bound["missing_additive_fields"]) == set(
            RECOVERY_LINEAGE_ADDITIVE_FIELDS
        )
        assert set(bound["derived_fields"]) == set(
            RECOVERY_LINEAGE_ADDITIVE_FIELDS
        )
        manifest = json.loads(
            connection.execute(
                "SELECT manifest_json FROM recovery_merge_lineage"
            ).fetchone()[0]
        )
        assert manifest["recoveries"][0]["lineage_validation"] == bound
    finally:
        connection.close()


def test_legacy_upgrade_never_infers_missing_core_lineage(tmp_path: Path) -> None:
    source, base, recovery, _merged, assets = _merge_fixture(tmp_path)
    invalid = tmp_path / "legacy-missing-core.db"
    shutil.copyfile(recovery, invalid)

    def remove_core(document: dict[str, object]) -> None:
        lineage = document["_run_lineage"]
        assert isinstance(lineage, dict)
        for field in RECOVERY_LINEAGE_ADDITIVE_FIELDS:
            lineage.pop(field)
        lineage.pop("ordered_recovery_manifest_sha256")

    _rewrite_terminal_dependency(invalid, remove_core)
    with pytest.raises(RecoveryError, match="missing required core fields"):
        validate_failure_recovery_sidecar(
            invalid,
            source,
            base,
            inventory_factory=lambda: iter(assets),
            allow_legacy_lineage_upgrade=True,
        )
    with pytest.raises(PipelineError, match="lineage"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(invalid,),
            output=tmp_path / "never-core.db",
            inventory_factory=lambda: iter(assets),
        )

    lineage_spoof = tmp_path / "lineage-spoof.db"
    shutil.copyfile(recovery, lineage_spoof)
    _rewrite_terminal_dependency(
        lineage_spoof,
        lambda document: document["_run_lineage"].__setitem__(
            "base_run_id", "spoofed-parent"
        ),
    )
    with pytest.raises(PipelineError, match="base run|lineage"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(lineage_spoof,),
            output=tmp_path / "never-lineage.db",
            inventory_factory=lambda: iter(assets),
        )

    with pytest.raises(PipelineError, match="overlap"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery, recovery),
            output=tmp_path / "never-overlap.db",
            inventory_factory=lambda: iter(assets),
        )


@pytest.mark.parametrize(
    ("stop_phase", "expected_status"),
    (
        ("inventory_assets", "initializing"),
        ("base_attempts", "running"),
        ("recoveries", "running"),
    ),
)
def test_merge_resumes_exactly_after_durable_batch_checkpoints(
    tmp_path: Path,
    stop_phase: str,
    expected_status: str,
) -> None:
    source, base, recovery, assets = _merge_inputs(tmp_path)
    merged = tmp_path / f"resumed-{stop_phase}.db"
    stopped = False

    def stop_after_first_target_checkpoint(phase: str, cursor: int) -> None:
        nonlocal stopped
        assert cursor >= 1
        if phase == stop_phase and not stopped:
            stopped = True
            raise RuntimeError(f"fixture interruption at {phase}")

    with pytest.raises(RuntimeError, match=stop_phase):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery,),
            output=merged,
            inventory_factory=lambda: iter(assets),
            batch_size=1,
            _checkpoint_hook=stop_after_first_target_checkpoint,
        )
    partial = Path(str(merged) + ".partial")
    assert stopped and partial.is_file() and not merged.exists()
    connection = open_sidecar(partial, readonly=True)
    try:
        assert connection.execute(
            "SELECT status FROM fingerprint_runs"
        ).fetchone()[0] == expected_status
        progress = connection.execute(
            "SELECT * FROM recovery_merge_progress"
        ).fetchone()
        assert progress["phase"] == stop_phase
        if stop_phase == "recoveries":
            assert connection.execute(
                "SELECT count(*) FROM recovery_merge_decisions"
            ).fetchone()[0] == 1
    finally:
        connection.close()

    with pytest.raises(FileExistsError, match="--resume"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery,),
            output=merged,
            inventory_factory=lambda: iter(assets),
            batch_size=1,
        )
    result = merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(recovery,),
        output=merged,
        inventory_factory=lambda: iter(assets),
        batch_size=1,
        resume=True,
    )
    assert result.recovered == 1
    assert result.final_success == 3
    assert result.final_failed == 1
    assert merged.is_file() and not partial.exists()

    connection = open_sidecar(merged, readonly=True)
    try:
        gaps = connection.execute(
            """SELECT count(*) FROM (
                 SELECT source_asset_id FROM fetch_attempts
                 GROUP BY source_asset_id
                 HAVING min(attempt_no)<>1 OR max(attempt_no)<>count(*)
               )"""
        ).fetchone()[0]
        assert gaps == 0
        assert connection.execute(
            "SELECT count(*) FROM recovery_merge_decisions"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_merge_resume_rejects_a_different_exact_manifest_and_preserves_partial(
    tmp_path: Path,
) -> None:
    source, base, recovery, assets = _merge_inputs(tmp_path)
    merged = tmp_path / "manifest-bound.db"

    def interrupt(phase: str, cursor: int) -> None:
        del cursor
        if phase == "inventory_assets":
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery,),
            output=merged,
            inventory_factory=lambda: iter(assets),
            batch_size=1,
            _checkpoint_hook=interrupt,
        )
    partial = Path(str(merged) + ".partial")
    copied_recovery = tmp_path / "same-bytes-different-lineage-path.db"
    shutil.copyfile(recovery, copied_recovery)
    with pytest.raises(PipelineError, match="manifest|provenance"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(copied_recovery,),
            output=merged,
            inventory_factory=lambda: iter(assets),
            batch_size=1,
            resume=True,
        )
    assert partial.is_file() and not merged.exists()
    result = merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(recovery,),
        output=merged,
        inventory_factory=lambda: iter(assets),
        batch_size=1,
        resume=True,
    )
    assert result.recovered == 1


def test_merge_terminal_partial_resume_publishes_without_recopy(
    tmp_path: Path,
) -> None:
    source, base, recovery, assets = _merge_inputs(tmp_path)
    merged = tmp_path / "terminal-resume.db"

    def stop_after_terminal(phase: str, cursor: int) -> None:
        del cursor
        if phase == "terminal":
            raise RuntimeError("power loss before publish")

    with pytest.raises(RuntimeError, match="power loss"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery,),
            output=merged,
            inventory_factory=lambda: iter(assets),
            batch_size=1,
            _checkpoint_hook=stop_after_terminal,
        )
    partial = Path(str(merged) + ".partial")
    connection = open_sidecar(partial, readonly=True)
    try:
        assert connection.execute(
            "SELECT status FROM fingerprint_runs"
        ).fetchone()[0] == "complete_with_failures"
        before_attempts = connection.execute(
            "SELECT count(*) FROM fetch_attempts"
        ).fetchone()[0]
    finally:
        connection.close()

    result = merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(recovery,),
        output=merged,
        inventory_factory=lambda: iter(assets),
        batch_size=1,
        resume=True,
    )
    assert result.run_status == "complete_with_failures"
    connection = open_sidecar(merged, readonly=True)
    try:
        assert connection.execute(
            "SELECT count(*) FROM fetch_attempts"
        ).fetchone()[0] == before_attempts
    finally:
        connection.close()


def test_merge_completed_output_resume_is_no_clobber_and_no_work(
    tmp_path: Path,
) -> None:
    source, base, recovery, merged, assets = _merge_fixture(tmp_path)
    before_sha = hashlib.sha256(merged.read_bytes()).hexdigest()

    def forbidden_checkpoint(phase: str, cursor: int) -> None:
        raise AssertionError(f"completed resume performed work: {phase}:{cursor}")

    result = merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(recovery,),
        output=merged,
        inventory_factory=lambda: iter(assets),
        resume=True,
        _checkpoint_hook=forbidden_checkpoint,
    )
    assert result.output_sha256 == before_sha
    assert hashlib.sha256(merged.read_bytes()).hexdigest() == before_sha


def test_merge_resume_recovers_partial_before_immutable_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base, recovery, assets = _merge_inputs(tmp_path)
    merged = tmp_path / "recovered-before-resume.db"

    def interrupt(phase: str, cursor: int) -> None:
        del cursor
        if phase == "inventory_assets":
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery,),
            output=merged,
            inventory_factory=lambda: iter(assets),
            batch_size=1,
            _checkpoint_hook=interrupt,
        )

    calls: list[Path] = []
    real_recover = image_fingerprint_merge.recover_sidecar

    def observed_recover(path: Path | str) -> None:
        calls.append(Path(path).resolve())
        real_recover(path)

    monkeypatch.setattr(image_fingerprint_merge, "recover_sidecar", observed_recover)
    merge_image_fingerprint_recoveries(
        source_db=source,
        base_sidecar=base,
        recovery_sidecars=(recovery,),
        output=merged,
        inventory_factory=lambda: iter(assets),
        batch_size=1,
        resume=True,
    )
    assert calls == [Path(str(merged) + ".partial").resolve()]


def test_merge_inventory_10001_is_checkpointed_in_bounded_5000_row_batches(
    tmp_path: Path,
) -> None:
    source, base, recovery, assets = _merge_inputs(tmp_path, count=10_001)
    merged = tmp_path / "bounded-inventory.db"
    cursors: list[int] = []

    def record_inventory_batches(phase: str, cursor: int) -> None:
        if phase == "inventory_assets":
            cursors.append(cursor)
            if len(cursors) == 3:
                raise RuntimeError("inventory checkpoints observed")

    with pytest.raises(RuntimeError, match="checkpoints observed"):
        merge_image_fingerprint_recoveries(
            source_db=source,
            base_sidecar=base,
            recovery_sidecars=(recovery,),
            output=merged,
            inventory_factory=lambda: iter(assets),
            _checkpoint_hook=record_inventory_batches,
        )
    assert cursors == [5_000, 10_000, 10_001]
    assert [cursors[0], cursors[1] - cursors[0], cursors[2] - cursors[1]] == [
        5_000,
        5_000,
        1,
    ]
