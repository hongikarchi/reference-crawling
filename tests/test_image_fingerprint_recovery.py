from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from canonical.image_fingerprint_adapters import (
    SourceAsset,
    source_asset_record_json,
    source_record_sha256,
)
from canonical.image_fingerprint_pipeline import (
    FetchFailure,
    FetchResponse,
    run_image_fingerprint_pipeline,
)
from canonical.image_fingerprint_recovery import (
    ALL_NON404_PLUS_404_SAMPLE,
    PER_ERROR_N10,
    FailedSourceAsset,
    RecoveryError,
    run_failure_recovery,
    select_failed_assets,
)
from canonical.image_fingerprint_sidecar import open_sidecar
from tools import recover_image_fingerprints as cli


def _png() -> bytes:
    image = Image.new("RGB", (80, 60), (35, 95, 170))
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


def _asset(number: int) -> SourceAsset:
    asset_id = f"asset-{number:03d}"
    source_url = f"https://images.divisare.com/images/v1/{asset_id}/image.jpg"
    fetch_url = (
        "https://images.divisare.com/images/"
        f"c_limit,f_jpg,h_1024,q_85,w_1024/v1/{asset_id}/image.jpg"
    )
    return SourceAsset(
        source="divisare",
        source_asset_id=asset_id,
        source_asset_key=asset_id,
        normalized_url=source_url,
        selected_raw_url=source_url,
        effective_fetch_url=fetch_url,
        source_urls=(source_url,),
        occurrence_count=1,
        parent_count=1,
        roles=("gallery",),
        format_lane="raster",
        fetch_profile_version="fixture-max1024-v1",
    )


class _BaseFetcher:
    def __init__(self, body: bytes):
        self.body = body
        self.network_requests = 0

    def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
        del attempt_no
        self.network_requests += 1
        number = int(asset.source_asset_id.rsplit("-", 1)[1])
        if number < 12:
            raise FetchFailure(
                "http_404",
                "fixture HTTP 404",
                http_status=404,
                final_url=asset.effective_fetch_url,
            )
        if number < 15:
            return FetchResponse(200, asset.effective_fetch_url, "image/png", b"")
        return FetchResponse(
            200, asset.effective_fetch_url, "image/png", self.body
        )


class _SuccessFetcher:
    def __init__(self, body: bytes):
        self.body = body
        self.network_requests = 0

    def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
        del attempt_no
        self.network_requests += 1
        return FetchResponse(
            200, asset.effective_fetch_url, "image/png", self.body
        )


def _base_fixture(tmp_path: Path) -> tuple[Path, Path, tuple[SourceAsset, ...]]:
    source = tmp_path / "source.db"
    base = tmp_path / "base.db"
    _source_db(source)
    assets = tuple(_asset(number) for number in range(17))
    result = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=base,
        sample_size=None,
        inventory_factory=lambda: iter(assets),
        fetcher=_BaseFetcher(_png()),
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    assert result.run_status == "complete_with_failures"
    assert result.status_counts == {"failed": 15, "success": 2}
    return source, base, assets


def _failed(asset: SourceAsset, error_kind: str, score: str) -> FailedSourceAsset:
    record_sha = source_record_sha256(source_asset_record_json(asset))
    return FailedSourceAsset(
        asset=asset,
        error_kind=error_kind,
        source_record_sha256=record_sha,
        selection_score_sha256=hashlib.sha256(score.encode("ascii")).hexdigest(),
    )


def test_selection_strategies_are_deterministic_and_error_aware() -> None:
    failures = tuple(
        [_failed(_asset(index), "http_404", f"404-{index}") for index in range(14)]
        + [_failed(_asset(100 + index), "decode:decode", f"decode-{index}") for index in range(12)]
        + [_failed(_asset(200 + index), "empty_response", f"empty-{index}") for index in range(3)]
    )
    per_error = select_failed_assets(
        failures,
        strategy=PER_ERROR_N10,
        seed="fixed-seed",
    )
    assert per_error.selected_error_counts == {
        "decode:decode": 10,
        "empty_response": 3,
        "http_404": 10,
    }
    assert len(per_error.selected) == 23
    assert per_error == select_failed_assets(
        failures,
        strategy=PER_ERROR_N10,
        seed="fixed-seed",
    )

    broad = select_failed_assets(
        failures,
        strategy=ALL_NON404_PLUS_404_SAMPLE,
        seed="fixed-seed",
        http_404_sample_size=2,
    )
    assert broad.selected_error_counts == {
        "decode:decode": 12,
        "empty_response": 3,
        "http_404": 2,
    }
    assert len(broad.selected) == 17
    assert tuple(item.asset.source_asset_id for item in broad.selected) == tuple(
        sorted(item.asset.source_asset_id for item in broad.selected)
    )


def test_failure_only_recovery_lineage_manifest_no_clobber_and_resume_zero(
    tmp_path: Path,
) -> None:
    source, base, assets = _base_fixture(tmp_path)
    output = tmp_path / "recovery.db"
    manifest = tmp_path / "recovery.manifest.json"
    fetcher = _SuccessFetcher(_png())
    result = run_failure_recovery(
        source="divisare",
        source_db=source,
        base_sidecar=base,
        output=output,
        manifest=manifest,
        strategy=PER_ERROR_N10,
        inventory_factory=lambda: iter(assets),
        fetcher=fetcher,
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    assert result.base_failed_count == 15
    assert result.selected_assets == 13
    assert result.run_status == "complete"
    assert result.status_counts == {"success": 13}
    assert result.network_requests == 13
    assert fetcher.network_requests == 13
    manifest_before = manifest.read_bytes()
    document = json.loads(manifest_before)
    payload_bytes = json.dumps(
        document["payload"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    assert document["payload_sha256"] == hashlib.sha256(payload_bytes).hexdigest()
    assert document["payload"]["base"]["failed_count"] == 15
    assert document["payload"]["selection"]["selected_error_counts"] == {
        "empty_response": 3,
        "http_404": 10,
    }

    connection = open_sidecar(output, readonly=True)
    try:
        dependency = json.loads(
            connection.execute(
                "SELECT dependency_manifest_json FROM fingerprint_runs"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    lineage = dependency["_run_lineage"]
    assert lineage["kind"] == "failure_recovery_v1"
    assert lineage["base_sidecar_sha256"] == hashlib.sha256(base.read_bytes()).hexdigest()
    assert lineage["ordered_recovery_manifest_sha256"] == result.selection_manifest_sha256

    with pytest.raises(FileExistsError):
        run_failure_recovery(
            source="divisare",
            source_db=source,
            base_sidecar=base,
            output=output,
            manifest=manifest,
            strategy=PER_ERROR_N10,
            inventory_factory=lambda: iter(assets),
            fetcher=fetcher,
            workers=1,
            requests_per_second=1_000_000,
            max_attempts=1,
        )
    assert fetcher.network_requests == 13

    resumed = run_failure_recovery(
        source="divisare",
        source_db=source,
        base_sidecar=base,
        output=output,
        manifest=manifest,
        strategy=PER_ERROR_N10,
        resume=True,
        inventory_factory=lambda: iter(assets),
        fetcher=fetcher,
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    assert resumed.network_requests == 0
    assert resumed.resumed is True
    assert resumed.already_complete is True
    assert fetcher.network_requests == 13
    assert manifest.read_bytes() == manifest_before

    # A crash after durable manifest bytes but before hard-link publication is
    # resumed without overwriting either the E1 sidecar or an existing target.
    manifest.unlink()
    manifest_partial = Path(str(manifest) + ".partial")
    manifest_partial.write_bytes(manifest_before)
    partial_resumed = run_failure_recovery(
        source="divisare",
        source_db=source,
        base_sidecar=base,
        output=output,
        manifest=manifest,
        strategy=PER_ERROR_N10,
        resume=True,
        inventory_factory=lambda: iter(assets),
        fetcher=fetcher,
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    assert partial_resumed.network_requests == 0
    assert manifest.read_bytes() == manifest_before
    assert not manifest_partial.exists()

    manifest.write_bytes(b"{}\n")
    with pytest.raises(RecoveryError, match="manifest does not match"):
        run_failure_recovery(
            source="divisare",
            source_db=source,
            base_sidecar=base,
            output=output,
            manifest=manifest,
            strategy=PER_ERROR_N10,
            resume=True,
            inventory_factory=lambda: iter(assets),
            fetcher=fetcher,
            workers=1,
            requests_per_second=1_000_000,
            max_attempts=1,
        )
    assert fetcher.network_requests == 13


def test_all_non404_plus_deterministic_404_sample_and_failed_only(
    tmp_path: Path,
) -> None:
    source, base, assets = _base_fixture(tmp_path)
    result = run_failure_recovery(
        source="divisare",
        source_db=source,
        base_sidecar=base,
        output=tmp_path / "broad.db",
        strategy=ALL_NON404_PLUS_404_SAMPLE,
        http_404_sample_size=2,
        inventory_factory=lambda: iter(assets),
        fetcher=_SuccessFetcher(_png()),
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    assert result.selected_assets == 5
    manifest = json.loads(result.manifest_path.read_text(encoding="ascii"))
    assert manifest["payload"]["selection"]["selected_error_counts"] == {
        "empty_response": 3,
        "http_404": 2,
    }
    selected_ids = {
        row["source_asset_id"]
        for row in manifest["payload"]["selection"]["records"]
    }
    assert not ({"asset-015", "asset-016"} & selected_ids)


def test_failure_lineage_can_publish_an_all_failed_recovery(tmp_path: Path) -> None:
    source, base, assets = _base_fixture(tmp_path)
    result = run_failure_recovery(
        source="divisare",
        source_db=source,
        base_sidecar=base,
        output=tmp_path / "all-failed.db",
        strategy=PER_ERROR_N10,
        inventory_factory=lambda: iter(assets),
        fetcher=_BaseFetcher(_png()),
        workers=1,
        requests_per_second=1_000_000,
        max_attempts=1,
    )
    assert result.run_status == "complete_with_failures"
    assert result.status_counts == {"failed": 13}
    assert result.manifest_path.is_file()


def test_source_record_mismatch_stops_before_recovery_fetch(tmp_path: Path) -> None:
    source, base, assets = _base_fixture(tmp_path)
    changed = list(assets)
    original = changed[0]
    changed[0] = SourceAsset(
        **{
            **original.__dict__,
            "occurrence_count": original.occurrence_count + 1,
        }
    )
    fetcher = _SuccessFetcher(_png())
    with pytest.raises(RecoveryError, match="validation|SHA mismatch"):
        run_failure_recovery(
            source="divisare",
            source_db=source,
            base_sidecar=base,
            output=tmp_path / "never.db",
            strategy=PER_ERROR_N10,
            inventory_factory=lambda: iter(changed),
            fetcher=fetcher,
            workers=1,
            requests_per_second=1_000_000,
            max_attempts=1,
        )
    assert fetcher.network_requests == 0
    assert not (tmp_path / "never.db").exists()


def test_partial_manifest_collision_stops_before_recovery_fetch(tmp_path: Path) -> None:
    source, base, assets = _base_fixture(tmp_path)
    output = tmp_path / "blocked.db"
    Path(str(output) + ".manifest.json.partial").write_bytes(b"occupied")
    fetcher = _SuccessFetcher(_png())
    with pytest.raises(FileExistsError, match="partial recovery manifest"):
        run_failure_recovery(
            source="divisare",
            source_db=source,
            base_sidecar=base,
            output=output,
            strategy=PER_ERROR_N10,
            inventory_factory=lambda: iter(assets),
            fetcher=fetcher,
            workers=1,
            requests_per_second=1_000_000,
            max_attempts=1,
        )
    assert fetcher.network_requests == 0
    assert not output.exists()


def test_cli_recovery_contract_and_worker_bound() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--source",
            "architizer",
            "--source-db",
            "source.db",
            "--base-sidecar",
            "base.db",
            "--output",
            "recovery.db",
            "--strategy",
            PER_ERROR_N10,
        ]
    )
    assert args.http_404_sample_size == 100
    assert args.workers == 4
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--source",
                "architizer",
                "--source-db",
                "source.db",
                "--base-sidecar",
                "base.db",
                "--output",
                "recovery.db",
                "--strategy",
                PER_ERROR_N10,
                "--workers",
                "9",
            ]
        )
