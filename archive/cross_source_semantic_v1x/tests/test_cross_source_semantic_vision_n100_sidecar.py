from __future__ import annotations

import hashlib
import json
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest

import canonical.cross_source_semantic_vision_n100_sidecar as n100
from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage_n100 import (
    SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN,
)
from canonical.cross_source_semantic_fetch import (
    DecodedImageInfo,
    FetchFailure,
    FetchPayload,
)
from canonical.divisare_vision_benchmark import decode_source, prepare_derivative
from canonical.divisare_vision_runtime import (
    TokenUsage,
    VisionRuntimeProvenance,
    VisionRuntimeResult,
)
from canonical.image_fingerprint import fingerprint_bytes


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _jpeg(index: int) -> bytes:
    from PIL import Image

    output = BytesIO()
    color = ((index * 47) % 256, (index * 89) % 256, (index * 137) % 256)
    Image.new("RGB", (64, 48), color).save(output, format="JPEG", quality=91)
    return output.getvalue()


def _artifact(path: Path, content: bytes) -> dict[str, object]:
    path.write_bytes(content)
    return {
        "application_id": 1,
        "byte_sha256": hashlib.sha256(content).hexdigest(),
        "logical_sha256": _sha(path.stem + "-logical"),
        "run_id": path.stem + "-run",
        "size_bytes": len(content),
        "user_version": 1,
    }


def _fixture(
    tmp_path: Path,
) -> tuple[
    n100.RuntimeContract,
    Path,
    Path,
    Path,
    Path,
    dict[str, bytes],
]:
    images = {f"image-{index}": _jpeg(index) for index in range(1, 101)}
    e2_path = tmp_path / "e2.db"
    e3_path = tmp_path / "e3.db"
    e2 = _artifact(e2_path, b"e2-frozen-input")
    e3 = _artifact(e3_path, b"e3-frozen-input")
    n10_path = tmp_path / "n10.db"
    n10_path.write_bytes(b"frozen-n10-fixture")
    n10_sha = hashlib.sha256(n10_path.read_bytes()).hexdigest()
    selected: list[dict[str, object]] = []
    for index in range(1, 101):
        source = "architizer" if index <= 67 else "divisare"
        host = (
            "architizer-prod.imgix.net"
            if source == "architizer"
            else "images.divisare.com"
        )
        identity = fingerprint_bytes(images[f"image-{index}"])
        selection_id = f"{source}:building:n100-{index:03d}"
        candidate = {
            "candidate_id": f"candidate-{index:03d}",
            "source": source,
            "source_building_id": f"building-{index:03d}",
            "source_asset_id": f"asset-{index:03d}",
            "fetch_url": f"https://{host}/image-{index}.jpg",
            "raw_response_sha256": identity.response_sha256,
            "normalized_pixel_sha256": identity.pixel_sha256,
            "normalized_width": identity.normalized_width,
            "normalized_height": identity.normalized_height,
            "e2_asset_record_sha256": _sha(f"e2-asset-{index}"),
            "e2_building_relation_record_sha256": _sha(f"e2-rel-{index}"),
            "e3_candidate_record_sha256": _sha(f"e3-candidate-{index}"),
            "e3_ranking_record_sha256": _sha(f"e3-ranking-{index}"),
            "e3_shortlist_item_record_sha256": _sha(f"e3-short-{index}"),
        }
        occurrence = {
            "occurrence": {
                "candidate": candidate,
                "occurrence_rank": 1,
            },
            "occurrence_record_sha256": _sha(f"occurrence-{index}"),
        }
        building = {
            "selection_id": selection_id,
            "source": source,
            "source_building_id": f"building-{index:03d}",
            "population_stratum": "ordinary",
            "qa_fallback": False,
            "selection_record_sha256": _sha(f"building-{index}"),
        }
        selected_building = {
            "building": building,
            "coverage_plan": {
                "selection_id": selection_id,
                "selected_occurrences": [occurrence],
            },
            "coverage_plan_record_sha256": _sha(f"plan-{index}"),
            "sampling_stratum": f"{source}:ordinary:gallery_1_3",
        }
        selected.append(
            {
                "selected_building": selected_building,
                "selected_building_record_sha256": _sha(f"selected-{index}"),
            }
        )
    body: dict[str, object] = {
        "e2_input": e2,
        "e3_input": e3,
        "ordered_building_manifest_sha256": _sha("ordered-buildings"),
        "ordered_occurrence_manifest_sha256": _sha("ordered-occurrences"),
        "planned_occurrence_count": 100,
        "planned_unique_e1_pixel_count": 100,
        "sample_seed": "archibe-semantic-coverage-n100-v1",
        "sample_size_buildings": 100,
        "selected_buildings": selected,
        "selection_mode": "offline_semantic_coverage_n100_population_sample",
        "n10_semantic_sidecar_input": {
            "byte_sha256": n10_sha,
            "path": str(n10_path.resolve()),
        },
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN, "manifest": body}
    )
    raw = (canonical_json(body) + "\n").encode("utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(raw)
    contract = n100.RuntimeContract(
        manifest_byte_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_self_sha256=str(body["semantic_coverage_manifest_sha256"]),
        sample_seed="archibe-semantic-coverage-n100-v1",
        building_count=100,
        occurrence_count=100,
        ordered_building_manifest_sha256=_sha("ordered-buildings"),
        ordered_occurrence_manifest_sha256=_sha("ordered-occurrences"),
    )
    return contract, manifest_path, e2_path, e3_path, n10_path, images


def _exclusions(n10_path: Path, pixels: frozenset[str] = frozenset()) -> n100.N10Exclusions:
    return n100.N10Exclusions(
        hashlib.sha256(n10_path.read_bytes()).hexdigest(),
        frozenset({"architizer:building:n10-only"}),
        pixels,
    )


def _loader(exclusions: n100.N10Exclusions):
    def load(path: Path, *, expected_sha256: str) -> n100.N10Exclusions:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256
        assert exclusions.sidecar_sha256 == expected_sha256
        return exclusions

    return load


def _fetcher(images: dict[str, bytes]):
    def fetch(source: str, url: str) -> FetchPayload:
        key = Path(url.split("?", 1)[0]).stem
        raw = images[key]
        return FetchPayload(
            request_url=url,
            final_url=url,
            http_status=200,
            content_type="image/jpeg",
            body=raw,
            raw_response_sha256=hashlib.sha256(raw).hexdigest(),
            decoded_image=DecodedImageInfo("JPEG", 64, 48, "RGB", 1),
            elapsed_seconds=0.01,
            request_count=1,
            redirect_chain=(),
        )

    return fetch


def _record(asset_id: str) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "in_scope": True,
        "reject_reason": "none",
        "medium": "photograph",
        "spatial_context": "exterior",
        "framing_scale": "overall",
        "camera_angle": "eye_level",
        "drawing_kind": "not_applicable",
        "project_state": "visibly_finished",
        "project_legibility": "high",
        "uncertain_axes": [],
        "resolution_insufficient": False,
        "evidence": "A coherent building facade is visibly dominant.",
    }


def _vision_result(kwargs: dict[str, object], *, ok: bool) -> VisionRuntimeResult:
    ids = tuple(str(value) for value in kwargs["expected_asset_ids"])
    schema_path = Path(kwargs["output_schema_path"])
    prompt = str(kwargs["prompt"])
    records = tuple(_record(value) for value in ids) if ok else ()
    provenance = VisionRuntimeProvenance(
        runtime_version="test-runtime",
        codex_bin=str(kwargs["codex_bin"]),
        model=str(kwargs["model"]),
        reasoning=str(kwargs["reasoning"]),
        service_tier=str(kwargs["service_tier"]),
        cli_image_detail="high",
        sandbox="read-only",
        working_directory=str(kwargs["working_directory"]),
        output_schema_path=str(schema_path),
        output_schema_sha256=hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        image_paths=tuple(str(path) for path in kwargs["image_paths"]),
        expected_asset_ids=ids,
        timeout_seconds=int(kwargs["timeout_seconds"]),
        command_without_prompt=("codex", "exec"),
    )
    return VisionRuntimeResult(
        status="success" if ok else "failed",
        records=records,
        final_assistant_text=canonical_json({"results": records}) if ok else "",
        usage=TokenUsage(100, 10, 20) if ok else None,
        stdout="test-event\n",
        stderr="" if ok else "synthetic failure",
        raw_events=(),
        non_json_stdout_lines=(),
        elapsed_seconds=0.1,
        returncode=0 if ok else 1,
        error_kind=None if ok else "synthetic",
        error_message=None if ok else "synthetic Vision failure",
        provenance=provenance,
    )


def _run_args(tmp_path: Path):
    contract, manifest, e2, e3, n10_path, images = _fixture(tmp_path)
    exclusions = _exclusions(n10_path)
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    args = {
        "contract": contract,
        "manifest_path": manifest,
        "e2_path": e2,
        "e3_path": e3,
        "n10_sidecar_path": n10_path,
        "output_db": tmp_path / "n100.db",
        "report_path": tmp_path / "n100.md",
        "codex_bin": Path("codex"),
        "model": "test-model",
        "reasoning": "low",
        "service_tier": "test",
        "cli_version": "test-cli",
        "expected_n10_sidecar_sha256": exclusions.sidecar_sha256,
        "n10_loader": _loader(exclusions),
        "sleeper": sleep,
        "clock": lambda: now[0],
    }
    return args, images, exclusions


def test_contract_caps_population_and_planned_batches() -> None:
    common = {
        "manifest_byte_sha256": _sha("manifest"),
        "manifest_self_sha256": _sha("self"),
        "sample_seed": "seed",
        "building_count": 100,
        "ordered_building_manifest_sha256": _sha("buildings"),
        "ordered_occurrence_manifest_sha256": _sha("occurrences"),
    }
    accepted = n100.RuntimeContract(occurrence_count=600, **common)
    assert accepted.planned_batch_count == 120
    with pytest.raises(ValueError, match="between 100 and 600"):
        n100.RuntimeContract(occurrence_count=601, **common)
    with pytest.raises(ValueError, match="exactly 100"):
        n100.RuntimeContract(occurrence_count=100, **{**common, "building_count": 99})


def test_http_circuit_breaker_commits_attempts_and_resume_preserves_budget(
    tmp_path: Path,
) -> None:
    args, _images, _ = _run_args(tmp_path)

    def fail(_source: str, url: str) -> FetchPayload:
        raise FetchFailure(
            "http_503",
            "HTTP 503",
            retryable=True,
            request_url=url,
            final_url=url,
            http_status=503,
            content_type="text/html",
            elapsed_seconds=0.01,
            request_count=1,
        )

    with pytest.raises(n100.N100RunPaused) as first:
        n100.run_semantic_vision_n100(
            **args,
            fetcher=fail,
            executor=lambda **_kwargs: pytest.fail("Vision ran before fetch gate"),
        )
    assert first.value.reason == "http_circuit_open"
    partial = Path(str(args["output_db"]) + ".partial")
    assert partial.is_file()
    assert not args["output_db"].exists()
    connection = sqlite3.connect(partial)
    try:
        assert connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 5
        assert connection.execute(
            "SELECT status FROM semantic_runs"
        ).fetchone()[0] == "running"
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_events WHERE event_type='http_circuit_open'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT MAX(attempt_no) FROM fetch_attempts WHERE inference_id='semv100_000002'"
        ).fetchone()[0] == 2
    finally:
        connection.close()

    with pytest.raises(n100.N100RunPaused):
        n100.run_semantic_vision_n100(
            **{**args, "resume": True},
            fetcher=fail,
            executor=lambda **_kwargs: pytest.fail("Vision ran before fetch gate"),
        )
    connection = sqlite3.connect(partial)
    try:
        assert connection.execute(
            "SELECT MAX(attempt_no) FROM fetch_attempts WHERE inference_id='semv100_000002'"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM fetch_attempts WHERE attempt_no>3"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM semantic_runs"
        ).fetchone()[0] == "running"
    finally:
        connection.close()


def test_global_fetch_retry_cap_pauses_before_unapproved_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _images, _ = _run_args(tmp_path)
    monkeypatch.setattr(n100, "HTTP_CIRCUIT_THRESHOLD", 999)
    monkeypatch.setattr(n100, "MAX_TOTAL_FETCH_RETRIES", 1)

    def fail(_source: str, url: str) -> FetchPayload:
        raise FetchFailure(
            "http_503",
            "HTTP 503",
            retryable=True,
            request_url=url,
            final_url=url,
            http_status=503,
            elapsed_seconds=0.01,
            request_count=1,
        )

    with pytest.raises(n100.N100RunPaused) as caught:
        n100.run_semantic_vision_n100(
            **args,
            fetcher=fail,
            executor=lambda **_kwargs: pytest.fail("Vision must not run"),
        )
    assert caught.value.reason == "fetch_retry_cap"
    partial = Path(str(args["output_db"]) + ".partial")
    connection = sqlite3.connect(partial)
    try:
        assert connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 2
        assert connection.execute(
            "SELECT MAX(attempt_no) FROM fetch_attempts"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_n10_actual_derivative_overlap_fails_before_any_vision(
    tmp_path: Path,
) -> None:
    args, images, _ = _run_args(tmp_path)
    first_derivative = prepare_derivative(
        decode_source(images["image-1"]), "long1024", 1024
    ).pixel_sha256
    exclusions = _exclusions(args["n10_sidecar_path"], frozenset({first_derivative}))
    args.update(
        expected_n10_sidecar_sha256=exclusions.sidecar_sha256,
        n10_loader=_loader(exclusions),
    )
    vision_calls = 0

    def forbidden(**_kwargs):
        nonlocal vision_calls
        vision_calls += 1
        raise AssertionError("Vision ran before derivative exclusion gate")

    with pytest.raises(n100.N100DataContractError, match="overlaps N10"):
        n100.run_semantic_vision_n100(
            **args,
            fetcher=_fetcher(images),
            executor=forbidden,
        )
    assert vision_calls == 0
    partial = Path(str(args["output_db"]) + ".partial")
    connection = sqlite3.connect(partial)
    try:
        assert connection.execute(
            "SELECT status FROM vision_inputs WHERE inference_id='semv100_000001'"
        ).fetchone()[0] == "ready"
        assert connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM semantic_runs").fetchone()[0] == "running"
    finally:
        connection.close()


def test_vision_circuit_breaker_keeps_remaining_rows_ready_and_unpublished(
    tmp_path: Path,
) -> None:
    args, images, _ = _run_args(tmp_path)
    calls = 0

    def fail_vision(**kwargs):
        nonlocal calls
        calls += 1
        return _vision_result(kwargs, ok=False)

    with pytest.raises(n100.N100RunPaused) as caught:
        n100.run_semantic_vision_n100(
            **args,
            fetcher=_fetcher(images),
            executor=fail_vision,
        )
    assert caught.value.reason == "vision_circuit_open"
    assert calls == n100.VISION_CIRCUIT_THRESHOLD
    partial = Path(str(args["output_db"]) + ".partial")
    connection = sqlite3.connect(partial)
    try:
        assert connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 100
        assert connection.execute("SELECT COUNT(*) FROM vision_attempts").fetchone()[0] == 3
        counts = dict(
            connection.execute(
                "SELECT status,COUNT(*) FROM vision_inputs GROUP BY status"
            ).fetchall()
        )
        assert counts == {"ready": 95, "vision_failed": 5}
        assert connection.execute("SELECT status FROM semantic_runs").fetchone()[0] == "running"
    finally:
        connection.close()
    assert not args["output_db"].exists()
    assert not args["report_path"].exists()


def test_executor_exception_is_committed_and_resume_consumes_attempt_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, images, _ = _run_args(tmp_path)
    monkeypatch.setattr(n100, "VISION_CIRCUIT_THRESHOLD", 1)

    def crash(**_kwargs):
        raise RuntimeError("synthetic executor crash")

    with pytest.raises(n100.N100RunPaused, match="vision_circuit_open"):
        n100.run_semantic_vision_n100(
            **args,
            fetcher=_fetcher(images),
            executor=crash,
        )

    partial = Path(str(args["output_db"]) + ".partial")
    connection = sqlite3.connect(partial)
    connection.row_factory = sqlite3.Row
    try:
        attempt = connection.execute(
            "SELECT * FROM vision_attempts ORDER BY attempt_id"
        ).fetchall()
        assert len(attempt) == 1
        assert attempt[0]["batch_no"] == 1
        assert attempt[0]["attempt_no"] == 1
        assert attempt[0]["status"] == "failed"
        assert attempt[0]["error_kind"] == "RuntimeError"
        assert attempt[0]["error_message"] == "synthetic executor crash"
        assert connection.execute(
            "SELECT COUNT(*) FROM vision_attempt_payloads"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_events "
            "WHERE event_type='vision_circuit_open'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM semantic_runs"
        ).fetchone()[0] == "running"
    finally:
        connection.close()

    with pytest.raises(n100.N100RunPaused, match="vision_circuit_open"):
        n100.run_semantic_vision_n100(
            **{**args, "resume": True},
            fetcher=lambda *_args: pytest.fail("resume refetched ready input"),
            executor=crash,
        )

    connection = sqlite3.connect(partial)
    try:
        assert connection.execute(
            "SELECT GROUP_CONCAT(attempt_no, ',') FROM "
            "(SELECT attempt_no FROM vision_attempts WHERE batch_no=1 "
            "ORDER BY attempt_no)"
        ).fetchone()[0] == "1,2"
        assert connection.execute(
            "SELECT COUNT(*) FROM vision_attempts WHERE batch_no=1 AND attempt_no>2"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM fetch_attempts"
        ).fetchone()[0] == 100
    finally:
        connection.close()


def test_complete_run_validates_no_clobber_and_completed_resume_uses_zero_requests(
    tmp_path: Path,
) -> None:
    args, images, _ = _run_args(tmp_path)
    vision_calls = 0

    def success(**kwargs):
        nonlocal vision_calls
        vision_calls += 1
        return _vision_result(kwargs, ok=True)

    result = n100.run_semantic_vision_n100(
        **args,
        fetcher=_fetcher(images),
        executor=success,
    )
    assert result.status == "complete"
    assert result.requests_made == 100
    assert result.vision_requests == 20
    assert vision_calls == 20
    assert args["output_db"].is_file()
    assert args["report_path"].is_file()
    assert not Path(str(args["output_db"]) + ".partial").exists()
    assert not Path(str(args["output_db"]) + ".partial.inputs").exists()
    status, logical, metrics = n100.validate_terminal_n100(
        args["output_db"], args["report_path"], args["contract"]
    )
    assert status == "complete"
    assert logical == result.logical_sha256
    assert metrics["results"] == 100

    with pytest.raises(FileExistsError, match="immutable N100 output"):
        n100.run_semantic_vision_n100(
            **args,
            fetcher=lambda *_args: pytest.fail("existing output fetched"),
            executor=lambda **_kwargs: pytest.fail("existing output ran Vision"),
        )
    resumed = n100.run_semantic_vision_n100(
        **{**args, "resume": True},
        fetcher=lambda *_args: pytest.fail("completed resume fetched"),
        executor=lambda **_kwargs: pytest.fail("completed resume ran Vision"),
    )
    assert resumed.already_complete is True
    assert resumed.requests_made == 0
    assert resumed.vision_requests == 0


def test_two_requests_per_second_limiter_uses_fake_clock() -> None:
    now = [5.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = n100._GlobalRequestRateLimiter(2.0, lambda: now[0], sleep)
    limiter.wait()
    now[0] += 0.1
    limiter.wait()
    now[0] += 0.1
    limiter.wait()
    assert sleeps == pytest.approx([0.4, 0.4])
