from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import (
    DEFAULT_SAMPLE_SEED,
    SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
)
import canonical.cross_source_semantic_vision_sidecar as sidecar
from canonical.cross_source_semantic_fetch import (
    DecodedImageInfo,
    FetchFailure,
    FetchPayload,
)
from canonical.cross_source_semantic_vision_sidecar import (
    _GlobalRequestRateLimiter,
    _acquire_lock,
    _cleanup_internal_materializations,
    _load_manifest,
    _materialization_cache_accounting,
    _prepare_input,
    _publish,
    _release_lock,
    _report_binding_sha256,
    _render_report,
    _sanitize_capture,
    _write_vision_attempt,
    SemanticVisionError,
    initialize_sidecar,
    validate_terminal_sidecar,
)
from canonical.divisare_vision_runtime import (
    TokenUsage,
    VisionRuntimeProvenance,
    VisionRuntimeResult,
)
from canonical.image_fingerprint import fingerprint_bytes


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _manifest(
    tmp_path: Path,
    *,
    first_source_raw: bytes | None = None,
) -> tuple[Path, dict[str, object], bytes]:
    selected = []
    occurrence_total = 0
    for building_index in range(10):
        source = "architizer" if building_index < 5 else "divisare"
        host = (
            "architizer-prod.imgix.net"
            if source == "architizer"
            else "images.divisare.com"
        )
        selection_id = f"{source}:building:b{building_index}"
        count = 6 if building_index < 7 else 5
        occurrences = []
        for local_rank in range(1, count + 1):
            occurrence_total += 1
            token = f"item-{occurrence_total}"
            identity = (
                fingerprint_bytes(first_source_raw)
                if occurrence_total == 1 and first_source_raw is not None
                else None
            )
            candidate = {
                "candidate_id": f"candidate-{occurrence_total}",
                "source": source,
                "source_building_id": f"b{building_index}",
                "source_asset_id": f"asset-{occurrence_total}",
                "fetch_url": f"https://{host}/image-{occurrence_total}.jpg",
                "raw_response_sha256": identity.response_sha256
                if identity is not None
                else _sha("raw-" + token),
                "normalized_pixel_sha256": identity.pixel_sha256
                if identity is not None
                else _sha("pixel-" + token),
                "normalized_width": identity.normalized_width
                if identity is not None
                else 512,
                "normalized_height": identity.normalized_height
                if identity is not None
                else 384,
                "e2_asset_record_sha256": _sha("e2a-" + token),
                "e2_building_relation_record_sha256": _sha("e2r-" + token),
                "e3_candidate_record_sha256": _sha("e3c-" + token),
                "e3_ranking_record_sha256": _sha("e3r-" + token),
                "e3_shortlist_item_record_sha256": _sha("e3s-" + token),
            }
            occurrence = {
                "candidate": candidate,
                "occurrence_rank": local_rank,
            }
            occurrences.append(
                {
                    "occurrence": occurrence,
                    "occurrence_record_sha256": _sha("occ-" + token),
                }
            )
        building = {
            "selection_id": selection_id,
            "source": source,
            "source_building_id": f"b{building_index}",
            "population_stratum": "ordinary",
            "qa_fallback": False,
            "selection_record_sha256": _sha(f"building-{building_index}"),
        }
        selected_building = {
            "building": building,
            "coverage_plan": {
                "selection_id": selection_id,
                "selected_occurrences": occurrences,
            },
            "coverage_plan_record_sha256": _sha(f"plan-{building_index}"),
            "guard_name": "ordinary",
        }
        selected.append(
            {
                "selected_building": selected_building,
                "selected_building_record_sha256": _sha(f"selected-{building_index}"),
            }
        )
    assert occurrence_total == 57
    artifact = {
        "application_id": 1,
        "byte_sha256": _sha("artifact"),
        "logical_sha256": _sha("logical"),
        "run_id": "run",
        "size_bytes": 1,
        "user_version": 1,
    }
    body: dict[str, object] = {
        "e2_input": artifact,
        "e3_input": artifact,
        "ordered_building_manifest_sha256": _sha("buildings"),
        "ordered_occurrence_manifest_sha256": _sha("occurrences"),
        "planned_occurrence_count": 57,
        "planned_unique_e1_pixel_count": 57,
        "sample_seed": DEFAULT_SAMPLE_SEED,
        "sample_size_buildings": 10,
        "selected_buildings": selected,
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN, "manifest": body}
    )
    raw = (canonical_json(body) + "\n").encode("utf-8")
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    return path, body, raw


def _initialized(tmp_path: Path) -> tuple[sqlite3.Connection, sqlite3.Row]:
    path, expected, raw = _manifest(tmp_path)
    payload, loaded = _load_manifest(
        path,
        expected_byte_sha256=hashlib.sha256(raw).hexdigest(),
        expected_self_sha256=str(expected["semantic_coverage_manifest_sha256"]),
    )
    assert loaded == raw
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_sidecar(
        connection,
        run_id="test-run",
        manifest_path=path,
        manifest_raw=raw,
        payload=payload,
        e2_path=Path("e2.db"),
        e3_path=Path("e3.db"),
        model="test-model",
        reasoning="low",
        service_tier="test",
        cli_version="test",
    )
    row = connection.execute(
        "SELECT * FROM selected_occurrences ORDER BY input_rank LIMIT 1"
    ).fetchone()
    assert row is not None
    return connection, row


def _jpeg_bytes() -> bytes:
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (640, 480), (17, 83, 149)).save(
        output,
        format="JPEG",
        quality=91,
    )
    return output.getvalue()


def _initialized_with_image(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, sqlite3.Row, bytes]:
    raw_image = _jpeg_bytes()
    path, expected, raw = _manifest(tmp_path, first_source_raw=raw_image)
    payload, loaded = _load_manifest(
        path,
        expected_byte_sha256=hashlib.sha256(raw).hexdigest(),
        expected_self_sha256=str(expected["semantic_coverage_manifest_sha256"]),
    )
    assert loaded == raw
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_sidecar(
        connection,
        run_id="test-run",
        manifest_path=path,
        manifest_raw=raw,
        payload=payload,
        e2_path=Path("e2.db"),
        e3_path=Path("e3.db"),
        model="test-model",
        reasoning="low",
        service_tier="test",
        cli_version="test",
        materialization_cache_dir=tmp_path / "cache",
        retain_review_cache=True,
    )
    row = connection.execute(
        "SELECT * FROM selected_occurrences ORDER BY input_rank LIMIT 1"
    ).fetchone()
    assert row is not None
    return connection, row, raw_image


def _fetch_payload(row: sqlite3.Row, raw: bytes) -> FetchPayload:
    return FetchPayload(
        request_url=str(row["fetch_url"]),
        final_url=str(row["fetch_url"]),
        http_status=200,
        content_type="image/jpeg",
        body=raw,
        raw_response_sha256=hashlib.sha256(raw).hexdigest(),
        decoded_image=DecodedImageInfo("JPEG", 640, 480, "RGB", 1),
        elapsed_seconds=0.01,
        request_count=1,
        redirect_chain=(),
    )


def test_fixed_manifest_initializes_10_buildings_and_57_inputs(tmp_path: Path) -> None:
    connection, _ = _initialized(tmp_path)
    try:
        assert connection.execute("SELECT status FROM semantic_runs").fetchone()[0] == "running"
        assert connection.execute("SELECT COUNT(*) FROM selected_buildings").fetchone()[0] == 10
        assert connection.execute("SELECT COUNT(*) FROM selected_occurrences").fetchone()[0] == 57
        assert connection.execute("SELECT COUNT(*) FROM vision_inputs WHERE status='pending'").fetchone()[0] == 57
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_retryable_fetch_failure_commits_all_attempt_evidence(tmp_path: Path) -> None:
    connection, row = _initialized(tmp_path)

    def fail(_source: str, url: str):
        raise FetchFailure(
            "http_429",
            "HTTP 429",
            retryable=True,
            request_url=url,
            final_url=url,
            http_status=429,
            content_type="text/html",
            response_bytes=123,
            retry_after_seconds=2,
            elapsed_seconds=0.1,
            request_count=1,
        )

    try:
        encoded, requests = _prepare_input(
            connection,
            run_id="test-run",
            row=row,
            fetcher=fail,
            sleeper=lambda _seconds: None,
            clock=lambda: 0.0,
            review_cache_dir=None,
        )
        assert encoded is None
        assert requests == 3
        attempts = connection.execute(
            "SELECT attempt_no,http_status,response_bytes,retryable,retry_after_seconds,scheduled_delay_seconds FROM fetch_attempts ORDER BY attempt_no"
        ).fetchall()
        assert [tuple(value) for value in attempts] == [
            (1, 429, 123, 1, 2.0, 2.0),
            (2, 429, 123, 1, 2.0, 2.0),
            (3, 429, 123, 0, 2.0, None),
        ]
        assert connection.execute(
            "SELECT status FROM vision_inputs WHERE inference_id=?",
            (row["inference_id"],),
        ).fetchone()[0] == "fetch_failed"
    finally:
        connection.close()


def test_terminal_run_blocks_new_derived_rows(tmp_path: Path) -> None:
    connection, _ = _initialized(tmp_path)
    try:
        before = connection.execute("SELECT e2_sha256_before,e3_sha256_before FROM semantic_runs").fetchone()
        connection.execute(
            "UPDATE semantic_runs SET status='complete',e2_sha256_after=?,e3_sha256_after=?,completed_at='done'",
            tuple(before),
        )
        with pytest.raises(sqlite3.IntegrityError, match="coverage assignment requires running run"):
            connection.execute(
                "INSERT INTO coverage_slot_assignments VALUES(?,?,?,?,?,?,?)",
                (
                    "test-run",
                    "architizer:building:b0",
                    "interior",
                    0,
                    "not_observed_in_sample",
                    None,
                    _sha("slot"),
                ),
            )
    finally:
        connection.close()


def test_vision_attempt_uses_frozen_run_cli_version_and_retains_payload(
    tmp_path: Path,
) -> None:
    connection, row = _initialized(tmp_path)
    provenance = VisionRuntimeProvenance(
        runtime_version="runtime",
        codex_bin="codex",
        model="test-model",
        reasoning="low",
        service_tier="test",
        cli_image_detail="high",
        sandbox="read-only",
        working_directory="opaque-temp",
        output_schema_path="schema.json",
        output_schema_sha256=_sha("schema"),
        prompt_sha256=_sha("prompt"),
        image_paths=("semv_000001.jpg",),
        expected_asset_ids=(row["inference_id"],),
        timeout_seconds=600,
        command_without_prompt=("codex", "exec"),
    )
    result = VisionRuntimeResult(
        status="success",
        records=(),
        final_assistant_text="{}",
        usage=TokenUsage(10, 2, 3),
        stdout="one-jsonl-event\n",
        stderr="",
        raw_events=(),
        non_json_stdout_lines=(),
        elapsed_seconds=1.25,
        returncode=0,
        error_kind=None,
        error_message=None,
        provenance=provenance,
    )
    try:
        attempt_id = _write_vision_attempt(
            connection,
            run_id="test-run",
            batch_no=1,
            attempt_no=1,
            inference_ids=[row["inference_id"]],
            result=result,
            status="success",
            started_at="start",
        )
        attempt = connection.execute(
            "SELECT cli_version,input_tokens,cached_input_tokens,output_tokens FROM vision_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        assert tuple(attempt) == ("test", 10, 2, 3)
        assert connection.execute(
            "SELECT stdout_bytes FROM vision_attempt_payloads WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0] == len("one-jsonl-event\n".encode("utf-8"))
    finally:
        connection.close()


def test_ready_input_resumes_from_sha_verified_derivative_without_refetch(
    tmp_path: Path,
) -> None:
    connection, row, raw_image = _initialized_with_image(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    calls = 0

    def fetch(_source: str, _url: str) -> FetchPayload:
        nonlocal calls
        calls += 1
        return _fetch_payload(row, raw_image)

    try:
        encoded, requests = _prepare_input(
            connection,
            run_id="test-run",
            row=row,
            fetcher=fetch,
            sleeper=lambda _seconds: None,
            clock=lambda: 0.0,
            review_cache_dir=cache,
        )
        assert encoded is not None
        assert requests == 1
        assert calls == 1
        assert connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 1

        resumed, resumed_requests = _prepare_input(
            connection,
            run_id="test-run",
            row=row,
            fetcher=lambda _source, _url: pytest.fail("ready input refetched"),
            sleeper=lambda _seconds: None,
            clock=lambda: 10.0,
            review_cache_dir=cache,
        )
        assert resumed == encoded
        assert resumed_requests == 0
        assert connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 1
        passed, detail = _materialization_cache_accounting(
            connection,
            run_id="test-run",
            directory=cache,
        )
        assert passed, detail
        provenance = json.loads(
            connection.execute(
                "SELECT metrics_json FROM semantic_runs"
            ).fetchone()[0]
        )["runtime_provenance"]["materialization_cache"]
        assert provenance == {
            "path": str(cache.resolve()),
            "retained_for_review": True,
            "write_contract": "atomic-exclusive-sha256-v1",
        }
    finally:
        connection.close()


def test_ready_input_fails_closed_on_materialization_tamper(tmp_path: Path) -> None:
    connection, row, raw_image = _initialized_with_image(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    try:
        encoded, _ = _prepare_input(
            connection,
            run_id="test-run",
            row=row,
            fetcher=lambda _source, _url: _fetch_payload(row, raw_image),
            sleeper=lambda _seconds: None,
            clock=lambda: 0.0,
            review_cache_dir=cache,
        )
        assert encoded is not None
        path = cache / f"{row['inference_id']}.jpg"
        path.write_bytes(b"tampered")
        with pytest.raises(SemanticVisionError, match="durable derivative identity"):
            _prepare_input(
                connection,
                run_id="test-run",
                row=row,
                fetcher=lambda _source, _url: pytest.fail("tampered ready input refetched"),
                sleeper=lambda _seconds: None,
                clock=lambda: 0.0,
                review_cache_dir=cache,
            )
        assert connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 1
    finally:
        connection.close()


def test_decode_failed_kind_is_preserved_as_decode_outcome(tmp_path: Path) -> None:
    connection, row = _initialized(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()

    def fail(_source: str, url: str):
        raise FetchFailure(
            "decode_failed",
            "cannot decode",
            retryable=False,
            request_url=url,
            final_url=url,
            http_status=200,
            content_type="image/jpeg",
            response_bytes=12,
            elapsed_seconds=0.1,
            request_count=1,
        )

    try:
        encoded, requests = _prepare_input(
            connection,
            run_id="test-run",
            row=row,
            fetcher=fail,
            sleeper=lambda _seconds: None,
            clock=lambda: 0.0,
            review_cache_dir=cache,
        )
        assert encoded is None
        assert requests == 1
        assert tuple(
            connection.execute(
                "SELECT outcome,error_kind FROM fetch_attempts"
            ).fetchone()
        ) == ("decode_failed", "decode_failed")
    finally:
        connection.close()


def test_global_rate_limiter_uses_fake_clock_at_two_requests_per_second() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = _GlobalRequestRateLimiter(2.0, lambda: now[0], sleep)
    limiter.wait()
    now[0] += 0.1
    limiter.wait()
    now[0] += 0.1
    limiter.wait()
    assert sleeps == pytest.approx([0.4, 0.4])


def test_persistent_lock_file_is_reusable_and_never_unlinked(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    first = _acquire_lock(lock)
    _release_lock(lock, first)
    assert lock.read_bytes() == b"1"
    second = _acquire_lock(lock)
    _release_lock(lock, second)
    assert lock.read_bytes() == b"1"


def test_publish_rolls_back_db_link_on_any_report_link_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial_db = tmp_path / "result.db.partial"
    output_db = tmp_path / "result.db"
    partial_report = tmp_path / "result.md.partial"
    report = tmp_path / "result.md"
    partial_db.write_bytes(b"db")
    partial_report.write_bytes(b"report")
    real_link = os.link

    def flaky_link(source: Path, target: Path) -> None:
        if Path(target) == report:
            raise PermissionError("synthetic report publish failure")
        real_link(source, target)

    monkeypatch.setattr(sidecar.os, "link", flaky_link)
    with pytest.raises(PermissionError, match="synthetic"):
        _publish(partial_db, output_db, partial_report, report)
    assert not output_db.exists()
    assert not report.exists()
    assert partial_db.read_bytes() == b"db"
    assert partial_report.read_bytes() == b"report"


def test_vision_payload_redacts_credentials_before_hashing_and_storage(
    tmp_path: Path,
) -> None:
    connection, row = _initialized(tmp_path)
    provenance = VisionRuntimeProvenance(
        runtime_version="runtime",
        codex_bin="codex",
        model="test-model",
        reasoning="low",
        service_tier="test",
        cli_image_detail="high",
        sandbox="read-only",
        working_directory="opaque-temp",
        output_schema_path="schema.json",
        output_schema_sha256=_sha("schema"),
        prompt_sha256=_sha("prompt"),
        image_paths=("semv_000001.jpg",),
        expected_asset_ids=(row["inference_id"],),
        timeout_seconds=600,
        command_without_prompt=("codex", "exec"),
    )
    result = VisionRuntimeResult(
        status="failed",
        records=(),
        final_assistant_text="",
        usage=None,
        stdout='{"api_key":"sk-ABCDEFGHIJKLMNOPQRST"}\n',
        stderr="Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        raw_events=(),
        non_json_stdout_lines=(),
        elapsed_seconds=0.1,
        returncode=1,
        error_kind="runtime",
        error_message="cookie=session-secret",
        provenance=provenance,
    )
    try:
        attempt_id = _write_vision_attempt(
            connection,
            run_id="test-run",
            batch_no=1,
            attempt_no=1,
            inference_ids=[row["inference_id"]],
            result=result,
            status="failed",
            started_at="start",
        )
        payload = connection.execute(
            "SELECT stdout_gzip,stdout_sha256,stderr_gzip,stderr_excerpt "
            "FROM vision_attempt_payloads WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        stdout = gzip.decompress(payload[0]).decode("utf-8")
        stderr = gzip.decompress(payload[2]).decode("utf-8")
        joined = stdout + stderr + str(payload[3])
        assert "sk-ABCDEFGHIJKLMNOPQRST" not in joined
        assert "abcdefghijklmnopqrstuvwxyz" not in joined
        assert "[REDACTED]" in joined
        assert payload[1] == hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        assert "session-secret" not in connection.execute(
            "SELECT error_message FROM vision_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
    finally:
        connection.close()


def test_capture_storage_is_bounded() -> None:
    with pytest.raises(SemanticVisionError, match="bounded limit"):
        _sanitize_capture("x" * 17, label="test", max_bytes=16)


def test_report_binding_ignores_only_logical_value_and_binds_metrics() -> None:
    metrics = {
        "input_status": {"success": 57},
        "fetch_attempts": 57,
        "downloaded_bytes": 100,
        "vision_attempts": 12,
        "results": 57,
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 3,
    }
    first_report = _render_report("complete", "a" * 64, metrics)
    second_report = _render_report("complete", "b" * 64, metrics)
    assert first_report != second_report
    first_binding = _report_binding_sha256("complete", metrics)
    changed = dict(metrics, results=56)
    assert _report_binding_sha256("complete", changed) != first_binding


def test_terminal_report_is_bound_and_tamper_is_rejected(tmp_path: Path) -> None:
    manifest_path, expected, manifest_raw = _manifest(tmp_path)
    payload, _ = _load_manifest(
        manifest_path,
        expected_byte_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        expected_self_sha256=str(expected["semantic_coverage_manifest_sha256"]),
    )
    db = tmp_path / "terminal.db"
    report = tmp_path / "terminal.md"
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    initialize_sidecar(
        connection,
        run_id="test-run",
        manifest_path=manifest_path,
        manifest_raw=manifest_raw,
        payload=payload,
        e2_path=Path("e2.db"),
        e3_path=Path("e3.db"),
        model="test-model",
        reasoning="low",
        service_tier="test",
        cli_version="test",
        materialization_cache_dir=tmp_path / "spool",
        retain_review_cache=False,
    )
    metrics = sidecar._metrics(connection, "test-run")
    metrics["materialization_cache"] = {
        "accounting": {"actual_entries": 0, "expected_jpegs": 0, "failures": []},
        "path": str((tmp_path / "spool").resolve()),
        "retained_for_review": False,
        "write_contract": "atomic-exclusive-sha256-v1",
    }
    status = "complete_with_failures"
    binding = _report_binding_sha256(status, metrics)
    for name in sidecar.REQUIRED_VALIDATIONS:
        value = binding if name == "report_binding" else "ok"
        connection.execute(
            "INSERT INTO validations VALUES(?,?,?,?,?,?,?)",
            ("test-run", name, "error", 1, value, value, None),
        )
    logical = sidecar.logical_sha256(connection)
    raw = _render_report(status, logical, metrics).encode("utf-8")
    metrics["report_sha256"] = hashlib.sha256(raw).hexdigest()
    metrics["report_size_bytes"] = len(raw)
    before = connection.execute(
        "SELECT e2_sha256_before,e3_sha256_before FROM semantic_runs"
    ).fetchone()
    connection.execute(
        """
        UPDATE semantic_runs SET status=?,e2_sha256_after=?,e3_sha256_after=?,
          completed_at='done',metrics_json=?,logical_sha256=?
        WHERE run_id='test-run'
        """,
        (status, before[0], before[1], canonical_json(metrics), logical),
    )
    connection.commit()
    connection.close()
    report.write_bytes(raw)

    sidecar.validate_terminal_sidecar(db)
    immutable = sidecar.open_immutable(db)
    try:
        sidecar._verify_report_binding(immutable, report)
    finally:
        immutable.close()
    report.write_bytes(raw + b"tampered")
    immutable = sidecar.open_immutable(db)
    try:
        with pytest.raises(SemanticVisionError, match="report content"):
            sidecar._verify_report_binding(immutable, report)
    finally:
        immutable.close()


def test_terminal_validation_never_calls_writable_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OpenedReadOnly(RuntimeError):
        pass

    monkeypatch.setattr(
        sidecar,
        "recover_sqlite",
        lambda _path: pytest.fail("terminal validation attempted writable recovery"),
    )
    monkeypatch.setattr(
        sidecar,
        "open_immutable",
        lambda _path: (_ for _ in ()).throw(OpenedReadOnly()),
    )
    with pytest.raises(OpenedReadOnly):
        validate_terminal_sidecar(tmp_path / "terminal.db")


def test_internal_materialization_cleanup_targets_only_owned_opaque_files(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "semv_000001.jpg").write_bytes(b"one")
    _cleanup_internal_materializations(clean, ["semv_000001"])
    assert not clean.exists()

    guarded = tmp_path / "guarded"
    guarded.mkdir()
    owned = guarded / "semv_000001.jpg"
    neighbor = guarded / "do-not-touch.txt"
    owned.write_bytes(b"one")
    neighbor.write_bytes(b"neighbor")
    with pytest.raises(SemanticVisionError, match="not empty"):
        _cleanup_internal_materializations(guarded, ["semv_000001"])
    assert not owned.exists()
    assert neighbor.read_bytes() == b"neighbor"
