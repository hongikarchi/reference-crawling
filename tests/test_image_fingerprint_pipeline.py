from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

import canonical.image_fingerprint_pipeline as pipeline
from canonical.image_fingerprint_adapters import SourceAsset
from canonical.image_fingerprint_pipeline import (
    FetchResponse,
    PipelineError,
    _host_allowed,
    run_image_fingerprint_pipeline,
)
from canonical.image_fingerprint_sidecar import open_sidecar, validate_sidecar
from tools.run_image_fingerprints import _sample_size


def _source_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES('read-only source')")
        connection.commit()
    finally:
        connection.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _png() -> bytes:
    image = Image.new("RGB", (80, 50), (35, 90, 170))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _asset(
    number: int,
    *,
    host: str = "images.divisare.com",
    lane: str = "raster",
    roles: tuple[str, ...] = ("gallery",),
    parent_count: int = 1,
) -> SourceAsset:
    asset_id = f"asset-{number:03d}"
    raw = f"https://{host}/images/f_auto/v1/{asset_id}/image.jpg"
    fetch = f"https://{host}/images/c_limit,w_1024/v1/{asset_id}/image.jpg"
    return SourceAsset(
        source="divisare",
        source_asset_id=asset_id,
        source_asset_key=asset_id,
        normalized_url=raw,
        selected_raw_url=raw,
        effective_fetch_url=fetch,
        source_urls=(raw,),
        occurrence_count=2 if parent_count > 1 else 1,
        parent_count=parent_count,
        roles=roles,
        format_lane=lane,
        fetch_profile_version="fixture-fetch-v1",
    )


class FakeFetcher:
    def __init__(self, body: bytes, behavior=None):
        self.body = body
        self.behavior = behavior
        self.calls: list[tuple[str, int]] = []

    def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
        self.calls.append((asset.source_asset_id, attempt_no))
        if self.behavior is not None:
            response = self.behavior(asset, attempt_no, len(self.calls))
            if response is not None:
                return response
        return FetchResponse(200, asset.effective_fetch_url, "image/png", self.body)


def _run(
    tmp_path: Path,
    assets: tuple[SourceAsset, ...],
    fetcher,
    *,
    output_name: str = "e1.db",
    resume: bool = False,
    max_attempts: int = 3,
    run_lineage: dict[str, object] | None = None,
):
    source = tmp_path / "source.db"
    if not source.exists():
        _source_db(source)
    return run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=tmp_path / output_name,
        sample_size=10,
        resume=resume,
        asset_factory=lambda: iter(assets),
        fetcher=fetcher,
        requests_per_second=10_000,
        cooldown_seconds=0,
        sleep=lambda _: None,
        max_attempts=max_attempts,
        run_lineage=run_lineage,
    )


def test_pipeline_publishes_valid_sidecar_without_mutating_source(tmp_path: Path) -> None:
    assets = tuple(_asset(index) for index in range(10))
    fetcher = FakeFetcher(_png())
    source = tmp_path / "source.db"
    _source_db(source)
    before = _sha(source)

    result = _run(tmp_path, assets, fetcher)

    assert result.selected_assets == 10
    assert result.run_status == "complete"
    assert result.status_counts == {"success": 10}
    assert result.network_requests == 10
    assert not result.resumed
    assert _sha(source) == before == result.source_sha256
    assert validate_sidecar(result.output_path).passed
    assert not Path(str(result.output_path) + ".partial").exists()
    lock_path = Path(str(result.output_path) + ".lock")
    assert lock_path.exists()
    descriptor = pipeline._acquire_lock(lock_path)
    pipeline._release_lock(lock_path, descriptor)
    assert not Path(str(source) + "-wal").exists()
    assert not Path(str(source) + "-shm").exists()
    assert not Path(str(source) + "-journal").exists()

    connection = open_sidecar(result.output_path)
    try:
        run = connection.execute("SELECT * FROM fingerprint_runs").fetchone()
        assert run["source_db_sha256_before"] == run["source_db_sha256_after"]
        assert run["dependency_manifest_json"] == json.dumps(
            pipeline.dependency_versions(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        assert hashlib.sha256(run["dependency_manifest_json"].encode()).hexdigest() == run[
            "dependency_manifest_sha256"
        ]
        validations = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT validation_name,passed FROM validations"
            )
        }
        assert validations["quick_check"] == 1
        assert validations["integrity_check"] == 1
        assert validations["foreign_key_check"] == 1
        assert validations["source_inventory_accounting"] == 1
    finally:
        connection.close()


def test_completed_resume_makes_zero_requests_and_plain_rerun_refuses_clobber(
    tmp_path: Path,
) -> None:
    assets = tuple(_asset(index) for index in range(10))
    _run(tmp_path, assets, FakeFetcher(_png()))

    never = FakeFetcher(_png(), behavior=lambda *_: pytest.fail("fetch called"))
    resumed = _run(tmp_path, assets, never, resume=True)
    assert resumed.already_complete
    assert resumed.resumed
    assert resumed.network_requests == 0
    assert never.calls == []

    with pytest.raises(FileExistsError, match="clobber"):
        _run(tmp_path, assets, FakeFetcher(_png()))
    lock_path = tmp_path / "e1.db.lock"
    descriptor = pipeline._acquire_lock(lock_path)
    pipeline._release_lock(lock_path, descriptor)


def test_interrupted_partial_resumes_only_pending_assets(tmp_path: Path) -> None:
    assets = tuple(_asset(index) for index in range(10))

    def interrupt(_asset, _attempt, call_no):
        if call_no == 2:
            raise KeyboardInterrupt
        return None

    first = FakeFetcher(_png(), behavior=interrupt)
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, assets, first)

    partial = tmp_path / "e1.db.partial"
    assert partial.exists()
    assert not (tmp_path / "e1.db").exists()
    lock_path = tmp_path / "e1.db.lock"
    descriptor = pipeline._acquire_lock(lock_path)
    pipeline._release_lock(lock_path, descriptor)
    connection = open_sidecar(partial)
    try:
        assert connection.execute(
            "SELECT count(*) FROM fingerprints WHERE status='success'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM fingerprints WHERE status='pending'"
        ).fetchone()[0] == 9
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT attempt_no,outcome,error_kind FROM fetch_attempts ORDER BY rowid"
            )
        ] == [(1, "success", None), (1, "failed", "interrupted")]
    finally:
        connection.close()

    second = FakeFetcher(_png())
    result = _run(tmp_path, assets, second, resume=True)
    assert result.resumed
    assert result.network_requests == 9
    assert len(second.calls) == 9
    assert any(attempt_no == 2 for _, attempt_no in second.calls)
    assert result.status_counts == {"success": 10}


def test_resume_refuses_dependency_drift_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tuple(_asset(index) for index in range(10))

    def interrupt(_asset, _attempt, call_no):
        if call_no == 2:
            raise KeyboardInterrupt
        return None

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, assets, FakeFetcher(_png(), behavior=interrupt))

    original = pipeline.dependency_versions()
    monkeypatch.setattr(
        pipeline,
        "dependency_versions",
        lambda: {**original, "pillow": "changed-for-test"},
    )
    never = FakeFetcher(_png())
    with pytest.raises(PipelineError, match="dependency"):
        _run(tmp_path, assets, never, resume=True)
    assert never.calls == []


def test_retryable_http_is_recorded_then_succeeds(tmp_path: Path) -> None:
    assets = tuple(_asset(index) for index in range(10))

    def retry_first(asset, attempt_no, _call_no):
        if asset.source_asset_id == "asset-000" and attempt_no == 1:
            return FetchResponse(503, asset.effective_fetch_url, "text/plain", b"")
        return None

    result = _run(tmp_path, assets, FakeFetcher(_png(), behavior=retry_first))
    assert result.network_requests == 11
    connection = open_sidecar(result.output_path)
    try:
        rows = connection.execute(
            """SELECT attempt_no,outcome,http_status,error_kind
               FROM fetch_attempts WHERE source_asset_id='asset-000'
               ORDER BY attempt_no"""
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "failed", 503, "http_503"),
            (2, "success", 200, None),
        ]
    finally:
        connection.close()


def test_invalid_hosts_are_skipped_without_calling_fetcher(tmp_path: Path) -> None:
    assets = tuple(_asset(index, host="example.com") for index in range(10))
    never = FakeFetcher(_png())
    with pytest.raises(PipelineError, match="failed final validation"):
        _run(tmp_path, assets, never)
    assert never.calls == []
    partial = tmp_path / "e1.db.partial"
    assert partial.exists()
    connection = open_sidecar(partial)
    try:
        assert connection.execute(
            "SELECT status FROM fingerprint_runs"
        ).fetchone()[0] == "failed_validation"
        assert connection.execute(
            "SELECT count(*) FROM fingerprints WHERE status='skipped'"
        ).fetchone()[0] == 10
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("source", "url", "expected"),
    [
        ("divisare", "https://images.divisare.com/images/v1/a.jpg", True),
        ("divisare", "https://images.divisare.com:443/images/v1/a.jpg", False),
        ("architizer", "https://architizer-prod.imgix.net/a.jpg", True),
        ("architizer", "https://other.imgix.net/a.jpg", False),
        ("architizer", "https://architizer-prod.imgix.net:443/a.jpg", False),
    ],
)
def test_fetch_host_allowlist_is_exact(
    source: str, url: str, expected: bool
) -> None:
    assert _host_allowed(source, url) is expected


def test_terminal_http_and_decode_failures_are_accounted(tmp_path: Path) -> None:
    assets = tuple(_asset(index) for index in range(10))

    def failures(asset, _attempt_no, _call_no):
        if asset.source_asset_id == "asset-000":
            return FetchResponse(404, asset.effective_fetch_url, "text/plain", b"missing")
        if asset.source_asset_id == "asset-001":
            return FetchResponse(200, asset.effective_fetch_url, "image/jpeg", b"not-image")
        return None

    result = _run(tmp_path, assets, FakeFetcher(_png(), behavior=failures))
    assert result.run_status == "complete_with_failures"
    assert result.status_counts == {"failed": 2, "success": 8}
    connection = open_sidecar(result.output_path)
    try:
        errors = dict(
            connection.execute(
                "SELECT source_asset_id,error_kind FROM fingerprints WHERE status='failed'"
            ).fetchall()
        )
        assert errors == {"asset-000": "http_404", "asset-001": "decode:decode"}
    finally:
        connection.close()


def test_full_preserves_adapter_order_and_manifest_is_repeatable(tmp_path: Path) -> None:
    assets = (_asset(8), _asset(2), _asset(5))
    source = tmp_path / "source.db"
    _source_db(source)

    def run(name: str):
        return run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=tmp_path / name,
            sample_size=None,
            asset_factory=lambda: iter(assets),
            fetcher=FakeFetcher(_png()),
            requests_per_second=10_000,
            cooldown_seconds=0,
        )

    first = run("full-a.db")
    second = run("full-b.db")
    assert first.selection_manifest_sha256 == second.selection_manifest_sha256
    connection = open_sidecar(first.output_path)
    try:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT source_asset_id FROM source_assets ORDER BY selection_rank"
            )
        ] == ["asset-008", "asset-002", "asset-005"]
    finally:
        connection.close()


def test_all_failed_standard_run_still_fails_validation(tmp_path: Path) -> None:
    assets = tuple(_asset(index) for index in range(10))

    def missing(asset, _attempt_no, _call_no):
        return FetchResponse(404, asset.effective_fetch_url, "text/plain", b"missing")

    with pytest.raises(PipelineError, match="failed final validation"):
        _run(tmp_path, assets, FakeFetcher(_png(), behavior=missing), max_attempts=1)

    partial = tmp_path / "e1.db.partial"
    connection = open_sidecar(partial)
    try:
        assert connection.execute(
            "SELECT status FROM fingerprint_runs"
        ).fetchone()[0] == "failed_validation"
        assert connection.execute(
            "SELECT count(*) FROM fingerprints WHERE status='failed'"
        ).fetchone()[0] == 10
    finally:
        connection.close()


def test_failure_recovery_lineage_all_failed_is_valid_terminal_sidecar(
    tmp_path: Path,
) -> None:
    assets = tuple(_asset(index) for index in range(10))
    # The generic pipeline only persists and shape-checks this internal
    # lineage capability.  Production recovery trust is established by
    # validate_failure_recovery_sidecar(), which independently binds these
    # fields to the immutable parent/source and deterministic selection; a
    # generic E1 validation alone is intentionally not that proof.
    lineage = {
        "kind": "failure_recovery_v1",
        "base_run_id": "base-run",
        "base_selection_manifest_sha256": "b" * 64,
        "base_sidecar_path": "base.db",
        "base_sidecar_sha256": "a" * 64,
        "base_source_db_sha256_before": "c" * 64,
        "base_source_db_sha256_after": "c" * 64,
        "base_fingerprint_contract_version": "fixture-contract-v1",
        "base_selection_version": "fixture-selection-v1",
        "base_dependency_manifest_sha256": "d" * 64,
        "http_404_sample_size": None,
        "ordered_recovery_manifest_sha256": "e" * 64,
        "recovery_policy_version": "failure-only-v1",
        "recovery_seed": "fixture-seed",
        "recovery_selection_count": 10,
        "recovery_strategy": "per_error_n10",
    }

    def missing(asset, _attempt_no, _call_no):
        return FetchResponse(404, asset.effective_fetch_url, "text/plain", b"missing")

    result = _run(
        tmp_path,
        assets,
        FakeFetcher(_png(), behavior=missing),
        max_attempts=1,
        run_lineage=lineage,
    )

    assert result.run_status == "complete_with_failures"
    assert result.status_counts == {"failed": 10}
    assert validate_sidecar(result.output_path).passed
    connection = open_sidecar(result.output_path)
    try:
        run = connection.execute("SELECT * FROM fingerprint_runs").fetchone()
        manifest = json.loads(run["dependency_manifest_json"])
        assert manifest.pop("_run_lineage") == lineage
        assert manifest == pipeline.dependency_versions()
        assert run["runner_version"].startswith("archibe-e1-pipeline-v2+deps-")
        required = dict(
            connection.execute(
                "SELECT validation_name,passed FROM validations WHERE severity='error'"
            )
        )
        assert all(required[name] == 1 for name in pipeline.REQUIRED_VALIDATIONS)
    finally:
        connection.close()

    never = FakeFetcher(_png(), behavior=lambda *_: pytest.fail("fetch called"))
    resumed = _run(
        tmp_path,
        assets,
        never,
        resume=True,
        max_attempts=1,
        run_lineage=lineage,
    )
    assert resumed.already_complete
    assert resumed.network_requests == 0
    assert never.calls == []


def test_incomplete_failure_recovery_lineage_cannot_authorize_all_failed(
    tmp_path: Path,
) -> None:
    assets = tuple(_asset(index) for index in range(10))

    def missing(asset, _attempt_no, _call_no):
        return FetchResponse(404, asset.effective_fetch_url, "text/plain", b"missing")

    with pytest.raises(PipelineError, match="failed final validation"):
        _run(
            tmp_path,
            assets,
            FakeFetcher(_png(), behavior=missing),
            max_attempts=1,
            run_lineage={"kind": "failure_recovery_v1"},
        )


def test_pending_batch_query_uses_rank_keyset_without_temp_sort(
    tmp_path: Path,
) -> None:
    assets = tuple(_asset(index) for index in range(10))
    result = _run(tmp_path, assets, FakeFetcher(_png()))
    connection = open_sidecar(result.output_path)
    try:
        run_id = connection.execute(
            "SELECT run_id FROM fingerprint_runs"
        ).fetchone()[0]
        plan = [
            str(row[-1])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + pipeline._PENDING_BATCH_SQL,
                (run_id, 0, 128),
            )
        ]
        assert any("idx_source_assets_run_rank" in detail for detail in plan)
        assert all("TEMP B-TREE" not in detail for detail in plan)
    finally:
        connection.close()


def test_exclusive_lock_refuses_a_second_writer(tmp_path: Path) -> None:
    assets = tuple(_asset(index) for index in range(10))
    lock = tmp_path / "e1.db.lock"
    descriptor = pipeline._acquire_lock(lock)
    try:
        with pytest.raises(PipelineError, match="exclusive runner lock is held"):
            _run(tmp_path, assets, FakeFetcher(_png()))
    finally:
        pipeline._release_lock(lock, descriptor)


def test_n10_hash_sample_scans_full_inventory_and_covers_important_strata(
    tmp_path: Path,
) -> None:
    assets = [_asset(index) for index in range(100)]
    assets[97] = _asset(97, parent_count=2)
    assets[98] = _asset(98, roles=("cover",))
    assets[99] = _asset(99, lane="convertible")
    result = _run(tmp_path, tuple(assets), FakeFetcher(_png()))

    connection = open_sidecar(result.output_path)
    try:
        records = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT provenance_json FROM source_assets ORDER BY selection_rank"
            )
        ]
        selected_ids = {record["source_asset_id"] for record in records}
        assert selected_ids != {f"asset-{index:03d}" for index in range(10)}
        assert any(record["format_lane"] == "convertible" for record in records)
        assert any("cover" in record["roles"] for record in records)
        assert any("gallery" in record["roles"] for record in records)
        assert any(record["parent_count"] > 1 for record in records)
        assert all(record["selection_stratum"] for record in records)
        assert any(record["selection_reason"].startswith("coverage:") for record in records)
        accounting = json.loads(
            connection.execute(
                "SELECT actual FROM validations WHERE validation_name='source_inventory_accounting'"
            ).fetchone()[0]
        )
        assert accounting["eligible"] == 100
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("10", 10), ("100", 100), ("1000", 1000), ("full", None)],
)
def test_cli_n_ladder(raw: str, expected: int | None) -> None:
    assert _sample_size(raw) == expected
