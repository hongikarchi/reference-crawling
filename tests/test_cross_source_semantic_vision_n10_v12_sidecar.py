from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import shutil

import pytest
from PIL import Image

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_fetch import (
    DecodedImageInfo,
    FetchFailure,
    FetchPayload,
)
from canonical.cross_source_semantic_fetch_v12 import PhysicalRequestCompletion
from canonical.cross_source_semantic_vision_n10_v12_sidecar import (
    PopulationContract,
    N10V12Paused,
    SemanticContract,
    _initialize,
    _load_manifest,
    _manifest_rows,
    _reconcile_open_reservations,
    _reserve_call,
    run_semantic_vision_n10_v12,
)
from canonical.cross_source_semantic_vision_v12 import (
    CONTRACT_VERSION,
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    TRANSFORM_VERSION,
    compose_prompt,
    derive_coverage_slots,
    derive_hero_decision,
    normalize_batch,
)
from canonical.divisare_vision_runtime import (
    CLI_IMAGE_DETAIL,
    RUNTIME_SANDBOX,
    RUNTIME_VERSION,
    TokenUsage,
    VisionRuntimeProvenance,
    VisionRuntimeResult,
)
from canonical.image_fingerprint import fingerprint_bytes


DOMAIN = "test-semantic-n10-v12-manifest"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _jpeg(index: int) -> bytes:
    output = BytesIO()
    Image.new(
        "RGB",
        (32, 24),
        ((index * 17) % 251, (index * 29) % 251, (index * 43) % 251),
    ).save(
        output, format="JPEG", quality=92
    )
    return output.getvalue()


def _artifact(path: Path, raw: bytes) -> dict[str, object]:
    path.write_bytes(raw)
    return {
        "application_id": 1,
        "byte_sha256": hashlib.sha256(raw).hexdigest(),
        "logical_sha256": _sha(path.stem + "-logical"),
        "run_id": path.stem + "-run",
        "size_bytes": len(raw),
        "user_version": 1,
    }


def _fixture(tmp_path: Path, occurrence_count: int = 10):
    images = {index: _jpeg(index) for index in range(1, occurrence_count + 1)}
    e2_path = tmp_path / "e2.db"
    e3_path = tmp_path / "e3.db"
    e2 = _artifact(e2_path, b"immutable-e2")
    e3 = _artifact(e3_path, b"immutable-e3")
    selected = []
    occurrence_index = 0
    per_building = [
        occurrence_count // 10 + (1 if index <= occurrence_count % 10 else 0)
        for index in range(1, 11)
    ]
    for index in range(1, 11):
        source = "architizer" if index <= 5 else "divisare"
        host = (
            "architizer-prod.imgix.net"
            if source == "architizer"
            else "images.divisare.com"
        )
        selection_id = f"{source}:building:v12-{index}"
        occurrences = []
        for occurrence_rank in range(1, per_building[index - 1] + 1):
            occurrence_index += 1
            identity = fingerprint_bytes(images[occurrence_index])
            candidate = {
                "candidate_id": f"candidate-{occurrence_index}",
                "source": source,
                "source_building_id": f"building-{index}",
                "source_asset_id": f"asset-{occurrence_index}",
                "fetch_url": f"https://{host}/image-{occurrence_index}.jpg",
                "raw_response_sha256": identity.response_sha256,
                "normalized_pixel_sha256": identity.pixel_sha256,
                "normalized_width": identity.normalized_width,
                "normalized_height": identity.normalized_height,
                "e2_asset_record_sha256": _sha(f"e2-asset-{occurrence_index}"),
                "e2_building_relation_record_sha256": _sha(f"e2-rel-{occurrence_index}"),
                "e3_candidate_record_sha256": _sha(f"e3-candidate-{occurrence_index}"),
                "e3_ranking_record_sha256": _sha(f"e3-rank-{occurrence_index}"),
                "e3_shortlist_item_record_sha256": _sha(f"e3-short-{occurrence_index}"),
            }
            occurrences.append(
                {
                    "occurrence": {
                        "candidate": candidate,
                        "occurrence_rank": occurrence_rank,
                    },
                    "occurrence_record_sha256": _sha(
                        f"occurrence-{occurrence_index}"
                    ),
                }
            )
        building = {
            "selection_id": selection_id,
            "source": source,
            "source_building_id": f"building-{index}",
            "population_stratum": "ordinary",
            "qa_fallback": False,
            "selection_record_sha256": _sha(f"selection-{index}"),
        }
        selected_body = {
            "available_count": 100,
            "building": building,
            "building_record_sha256": canonical_sha256(building),
            "coverage_plan": {
                "selection_id": selection_id,
                "selected_occurrences": occurrences,
            },
            "coverage_record_sha256": _sha(f"coverage-{index}"),
            "lane": "population_core" if index <= 8 else "risk_sentinel",
            "model_result_selection_used": False,
            "quota": 1,
            "rank": index,
            "rank_in_quota": 1,
            "score_sha256": _sha(f"score-{index}"),
            "source": source,
            "stratum": "ordinary",
        }
        selected.append(
            {
                "selected_building": selected_body,
                "selected_building_record_sha256": canonical_sha256(selected_body),
            }
        )
    body: dict[str, object] = {
        "e2_input": e2,
        "e3_input": e3,
        "exclusions": {
            "prior_run_inputs": [
                {"name": "semantic_n10_v1"},
                {"name": "semantic_n100_v1"},
                {"name": "semantic_n10_v1_1"},
            ],
            "prior_successful_derivative_pixel_count": 0,
            "records": {"occurrence_records": []},
        },
        "ordered_building_manifest_sha256": _sha("buildings"),
        "ordered_occurrence_manifest_sha256": _sha("occurrences"),
        "ordered_fetch_url_manifest_sha256": _sha("urls"),
        "ordered_e1_pixel_manifest_sha256": _sha("pixels"),
        "planned_occurrence_count": occurrence_count,
        "planned_unique_e1_pixel_count": occurrence_count,
        "sample_seed": "test-n10-v12",
        "sample_size_buildings": 10,
        "selected_buildings": selected,
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": DOMAIN, "manifest": body}
    )
    raw = (canonical_json(body) + "\n").encode("utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(raw)
    population = PopulationContract(
        manifest_domain=DOMAIN,
        manifest_byte_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_self_sha256=str(body["semantic_coverage_manifest_sha256"]),
        sample_seed="test-n10-v12",
        occurrence_count=occurrence_count,
        ordered_building_manifest_sha256=_sha("buildings"),
        ordered_occurrence_manifest_sha256=_sha("occurrences"),
        ordered_fetch_url_manifest_sha256=_sha("urls"),
        ordered_e1_pixel_manifest_sha256=_sha("pixels"),
        inference_id_prefix="semv12_",
        max_model_call_reservations=(occurrence_count + 4) // 5 + 2,
        observed_input_token_stop_threshold=10_000,
        observed_output_token_stop_threshold=2_000,
        observed_total_token_stop_threshold=12_000,
        prior_run_names=("semantic_n10_v1", "semantic_n100_v1", "semantic_n10_v1_1"),
        prior_successful_derivative_count=0,
    )
    semantics = SemanticContract(
        contract_version=CONTRACT_VERSION,
        prompt_version=PROMPT_VERSION,
        transform_version=TRANSFORM_VERSION,
        output_schema=OUTPUT_SCHEMA,
        compose_prompt=compose_prompt,
        normalize_batch=normalize_batch,
        derive_hero_decision=derive_hero_decision,
        derive_coverage_slots=derive_coverage_slots,
        coverage_slots=(
            "aerial_context",
            "construction_or_archive",
            "detail",
            "drawing_other",
            "drawing_plan",
            "drawing_section",
            "exterior_context",
            "exterior_overall",
            "interior",
            "model_or_render",
        ),
    )
    return population, semantics, manifest, e2_path, e3_path, images


def _fetcher(images: dict[int, bytes]):
    def fetch(source: str, url: str, *, callbacks, **kwargs) -> FetchPayload:
        index = int(Path(url).stem.split("-")[-1])
        reservation = callbacks.reserve(url, 1)
        callbacks.complete(
            PhysicalRequestCompletion(
                reservation,
                url,
                1,
                0.01,
                200,
                "response_received",
            )
        )
        raw = images[index]
        return FetchPayload(
            request_url=url,
            final_url=url,
            http_status=200,
            content_type="image/jpeg",
            body=raw,
            raw_response_sha256=hashlib.sha256(raw).hexdigest(),
            decoded_image=DecodedImageInfo("JPEG", 32, 24, "RGB", 1),
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
        "evidence": "A coherent building facade is clearly visible.",
    }


def _executor(**kwargs) -> VisionRuntimeResult:
    ids = tuple(kwargs["expected_asset_ids"])
    records = tuple(_record(value) for value in ids)
    schema_path = Path(kwargs["output_schema_path"])
    prompt = kwargs["prompt"]
    provenance = VisionRuntimeProvenance(
        runtime_version=RUNTIME_VERSION,
        codex_bin=str(kwargs["codex_bin"]),
        model=kwargs["model"],
        reasoning=kwargs["reasoning"],
        service_tier=kwargs["service_tier"],
        cli_image_detail=CLI_IMAGE_DETAIL,
        sandbox=RUNTIME_SANDBOX,
        working_directory=str(kwargs["working_directory"]),
        output_schema_path=str(schema_path),
        output_schema_sha256=hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        image_paths=tuple(str(value) for value in kwargs["image_paths"]),
        expected_asset_ids=ids,
        timeout_seconds=600,
        command_without_prompt=("codex", "exec"),
    )
    events = (
        canonical_json(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": canonical_json({"results": records}),
                },
            }
        ),
        canonical_json(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 5,
                    "output_tokens": 20,
                },
            }
        ),
    )
    stdout = "\n".join(events) + "\n"
    return VisionRuntimeResult(
        status="success",
        records=records,
        final_assistant_text=canonical_json({"results": records}),
        usage=TokenUsage(100, 5, 20),
        stdout=stdout,
        stderr="",
        raw_events=tuple(json.loads(value) for value in events),
        non_json_stdout_lines=(),
        elapsed_seconds=0.1,
        returncode=0,
        error_kind=None,
        error_message=None,
        provenance=provenance,
    )


def _args(tmp_path: Path):
    population, semantics, manifest, e2, e3, images = _fixture(tmp_path)
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return {
        "population": population,
        "semantics": semantics,
        "manifest_path": manifest,
        "e2_path": e2,
        "e3_path": e3,
        "output_db": tmp_path / "semantic-v12.db",
        "report_path": tmp_path / "semantic-v12.md",
        "codex_bin": Path("codex"),
        "model": "test-model",
        "reasoning": "low",
        "service_tier": "test",
        "cli_version": "codex-cli 0.146.0",
        "fetcher": _fetcher(images),
        "executor": _executor,
        "sleeper": sleep,
        "clock": lambda: now[0],
    }


def test_planner_lane_and_coverage_keys_decode(tmp_path: Path) -> None:
    population, _, manifest, _, _, _ = _fixture(tmp_path)
    payload, _ = _load_manifest(manifest, population)
    buildings, occurrences = _manifest_rows(payload, population)

    assert len(buildings) == len(occurrences) == 10
    assert buildings[0]["guard_name"] == "population_core"
    assert buildings[-1]["guard_name"] == "risk_sentinel"
    assert buildings[0]["coverage_plan_record_sha256"] == _sha("coverage-1")


def test_complete_run_reserves_all_calls_and_completed_resume_is_zero_call(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    first = run_semantic_vision_n10_v12(**args)

    assert first.status == "complete"
    assert first.physical_http_reservations == 10
    assert first.model_call_reservations == 2
    assert first.metrics["open_reservations"] == 0

    def forbidden(*args, **kwargs):
        raise AssertionError("completed resume must make zero external calls")

    resumed = run_semantic_vision_n10_v12(
        **{**args, "resume": True, "fetcher": forbidden, "executor": forbidden}
    )
    assert resumed.already_complete is True
    assert resumed.physical_http_reservations == 0
    assert resumed.model_call_reservations == 0


def test_empty_initialization_partial_resumes_without_reselection(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    partial = Path(str(args["output_db"]) + ".partial")
    partial.touch()
    cache = tmp_path / "resume-cache"
    cache.mkdir()
    result = run_semantic_vision_n10_v12(
        **{**args, "resume": True, "review_cache_dir": cache}
    )
    assert result.status == "complete"
    assert result.resumed is True


def test_db_only_publication_recovers_report_with_zero_external_calls(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    cache = tmp_path / "review-cache"
    args["review_cache_dir"] = cache
    first = run_semantic_vision_n10_v12(**args)
    partial = Path(str(args["output_db"]) + ".partial")
    shutil.copyfile(first.output_db, partial)
    first.report_path.unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("publication recovery must make zero external calls")

    recovered = run_semantic_vision_n10_v12(
        **{**args, "resume": True, "fetcher": forbidden, "executor": forbidden}
    )
    assert recovered.already_complete is True
    assert recovered.physical_http_reservations == 0
    assert recovered.model_call_reservations == 0
    assert recovered.report_path.is_file()


def test_open_vision_reservation_is_consumed_as_unknown_attempt(tmp_path: Path) -> None:
    population, semantics, manifest, e2, e3, _ = _fixture(tmp_path)
    payload, raw = _load_manifest(manifest, population)
    db = tmp_path / "partial.db"
    cache = tmp_path / "cache"
    cache.mkdir()
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    _initialize(
        connection,
        run_id="run",
        manifest_path=manifest,
        manifest_raw=raw,
        payload=payload,
        population=population,
        semantics=semantics,
        e2_path=e2,
        e3_path=e3,
        model="test-model",
        reasoning="low",
        service_tier="test",
        cli_version="codex-cli 0.146.0",
        cache_dir=cache,
        retained_for_review=True,
    )
    ids = ["semv12_000001"]
    _reserve_call(
        connection,
        run_id="run",
        kind="vision",
        logical_key="vision:1:1",
        inference_id=None,
        batch_no=1,
        attempt_no=1,
        hop_no=None,
        request={
            "codex_bin": "codex",
            "inference_ids": ids,
            "prompt_sha256": _sha("prompt"),
        },
    )

    accounting = _reconcile_open_reservations(connection, run_id="run")

    assert accounting == {
        "open_completions_closed": 1,
        "http_attempts_consumed": 0,
        "vision_attempts_consumed": 1,
    }
    attempt = connection.execute("SELECT * FROM vision_attempts").fetchone()
    assert attempt["status"] == "failed"
    assert attempt["error_kind"] == "interrupted_unknown"
    assert attempt["input_tokens"] is None
    completion = connection.execute("SELECT outcome FROM external_call_completions").fetchone()[0]
    assert completion == "interrupted_unknown"
    connection.close()


def test_actual_prior_derivative_collision_fails_before_model_call(tmp_path: Path) -> None:
    args = _args(tmp_path)
    population = args["population"]
    payload = json.loads(Path(args["manifest_path"]).read_text(encoding="utf-8"))
    # Use the exact derivative produced by the first source JPEG as a prior
    # successful derivative.  Re-sign/re-pin the otherwise deterministic test
    # manifest so the runtime reaches the actual post-materialization gate.
    from canonical.divisare_vision_benchmark import decode_source, prepare_derivative

    # Reuse the fixture's first source bytes through the fetcher closure is not
    # inspectable; reconstructing the deterministic JPEG is equivalent.
    derivative = prepare_derivative(decode_source(_jpeg(1)), "long1024", 1024)
    payload["exclusions"]["records"]["occurrence_records"] = [
        {"derivative_pixel_sha256": derivative.pixel_sha256}
    ]
    payload["exclusions"]["prior_successful_derivative_pixel_count"] = 1
    payload.pop("semantic_coverage_manifest_sha256")
    payload["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": DOMAIN, "manifest": payload}
    )
    raw = (canonical_json(payload) + "\n").encode("utf-8")
    Path(args["manifest_path"]).write_bytes(raw)
    args["population"] = PopulationContract(
        **{
            **population.__dict__,
            "manifest_byte_sha256": hashlib.sha256(raw).hexdigest(),
            "manifest_self_sha256": payload["semantic_coverage_manifest_sha256"],
            "prior_successful_derivative_count": 1,
        }
    )
    calls = 0

    def forbidden_model(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("collision must fail before first model call")

    args["executor"] = forbidden_model
    with pytest.raises(Exception, match="actual derivative exclusion gate failed"):
        run_semantic_vision_n10_v12(**args)
    assert calls == 0


def test_all_53_inputs_are_materialized_before_first_vision_call(
    tmp_path: Path,
) -> None:
    population, semantics, manifest, e2, e3, images = _fixture(
        tmp_path, occurrence_count=53
    )
    output = tmp_path / "semantic-53.db"
    first_call = True

    def asserting_executor(**kwargs):
        nonlocal first_call
        if first_call:
            first_call = False
            partial = Path(str(output) + ".partial")
            connection = sqlite3.connect(partial)
            try:
                assert connection.execute(
                    "SELECT COUNT(*) FROM vision_inputs WHERE status='ready'"
                ).fetchone()[0] == 53
                assert connection.execute(
                    "SELECT COUNT(*) FROM external_call_reservations "
                    "WHERE call_kind='http'"
                ).fetchone()[0] == 53
                assert connection.execute(
                    "SELECT COUNT(*) FROM runtime_events WHERE "
                    "event_type='actual_derivative_exclusion_gate'"
                ).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT COUNT(*) FROM external_call_reservations "
                    "WHERE call_kind='vision'"
                ).fetchone()[0] == 1
            finally:
                connection.close()
        return _executor(**kwargs)

    result = run_semantic_vision_n10_v12(
        population=population,
        semantics=semantics,
        manifest_path=manifest,
        e2_path=e2,
        e3_path=e3,
        output_db=output,
        report_path=tmp_path / "semantic-53.md",
        codex_bin=Path("codex"),
        model="test-model",
        reasoning="low",
        service_tier="test",
        cli_version="test-cli",
        fetcher=_fetcher(images),
        executor=asserting_executor,
        sleeper=lambda value: None,
        clock=lambda: 0.0,
    )

    assert first_call is False
    assert result.status == "complete"
    assert result.model_call_reservations == 11


def test_source_changed_is_fixed_failure_without_retry_or_replacement(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    ordinary_fetch = args["fetcher"]

    def changed_first(source: str, url: str, **kwargs):
        payload = ordinary_fetch(source, url, **kwargs)
        if url.endswith("image-1.jpg"):
            raw = _jpeg(99)
            return FetchPayload(
                request_url=payload.request_url,
                final_url=payload.final_url,
                http_status=payload.http_status,
                content_type=payload.content_type,
                body=raw,
                raw_response_sha256=hashlib.sha256(raw).hexdigest(),
                decoded_image=payload.decoded_image,
                elapsed_seconds=payload.elapsed_seconds,
                request_count=payload.request_count,
                redirect_chain=payload.redirect_chain,
            )
        return payload

    result = run_semantic_vision_n10_v12(
        **{**args, "fetcher": changed_first}
    )
    connection = sqlite3.connect(
        f"file:{result.output_db.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    try:
        first = connection.execute(
            "SELECT status,error_kind FROM vision_inputs WHERE inference_id='semv12_000001'"
        ).fetchone()
        attempts = connection.execute(
            "SELECT COUNT(*) FROM fetch_attempts WHERE inference_id='semv12_000001'"
        ).fetchone()[0]
        occurrences = connection.execute(
            "SELECT COUNT(*) FROM selected_occurrences"
        ).fetchone()[0]
        results = connection.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0]
    finally:
        connection.close()
    assert first == ("fetch_failed", "source_changed")
    assert attempts == 1
    assert occurrences == 10
    assert results == 9
    assert result.status == "complete_with_failures"


def test_keyboard_interrupt_propagates_and_resume_consumes_open_reservation(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)

    def interrupt(**kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_semantic_vision_n10_v12(**{**args, "executor": interrupt})

    partial = Path(str(args["output_db"]) + ".partial")
    assert partial.is_file()
    connection = sqlite3.connect(partial)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_call_reservations WHERE call_kind='vision'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM external_call_completions c "
            "JOIN external_call_reservations r USING(reservation_id) "
            "WHERE r.call_kind='vision'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM vision_attempts").fetchone()[0] == 0
    finally:
        connection.close()

    def no_fetch(*args, **kwargs):
        raise AssertionError("durable ready inputs must not be fetched again")

    # The crash-open reservation is charged as an unknown-usage attempt.  The
    # observed-token policy then fail-closes before another paid call instead
    # of pretending that the unknown call was free.
    with pytest.raises(Exception, match="observed_token_stop_threshold"):
        run_semantic_vision_n10_v12(
            **{**args, "resume": True, "fetcher": no_fetch, "executor": _executor}
        )
    partial = Path(str(args["output_db"]) + ".partial")
    connection = sqlite3.connect(partial)
    try:
        attempts = connection.execute(
            "SELECT batch_no,attempt_no,error_kind,status FROM vision_attempts ORDER BY attempt_id"
        ).fetchall()
        reservations = connection.execute(
            "SELECT batch_no,attempt_no FROM external_call_reservations "
            "WHERE call_kind='vision' ORDER BY reservation_id"
        ).fetchall()
    finally:
        connection.close()
    assert attempts[0] == (1, 1, "interrupted_unknown", "failed")
    assert len(attempts) == 1
    assert reservations == [(1, 1)]


def test_observed_token_threshold_stops_before_next_paid_call(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    population = args["population"]
    args["population"] = PopulationContract(
        **{
            **population.__dict__,
            "observed_input_token_stop_threshold": 100,
            "observed_output_token_stop_threshold": 20,
            "observed_total_token_stop_threshold": 120,
        }
    )
    calls = 0

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return _executor(**kwargs)

    with pytest.raises(N10V12Paused, match="observed_token_stop_threshold"):
        run_semantic_vision_n10_v12(**{**args, "executor": counted})
    assert calls == 1
    connection = sqlite3.connect(Path(str(args["output_db"]) + ".partial"))
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_call_reservations WHERE call_kind='vision'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_model_call_reservation_cap_is_durable_and_fail_closed(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    population = args["population"]
    args["population"] = PopulationContract(
        **{**population.__dict__, "max_model_call_reservations": 2}
    )

    def known_usage_failure(**kwargs):
        value = _executor(**kwargs)
        return replace(
            value,
            status="exec_error",
            records=(),
            final_assistant_text=None,
            error_kind="fixture_failure",
            error_message="fixture failure with known usage",
        )

    with pytest.raises(N10V12Paused, match="model_call_reservation_cap"):
        run_semantic_vision_n10_v12(
            **{**args, "executor": known_usage_failure}
        )
    connection = sqlite3.connect(Path(str(args["output_db"]) + ".partial"))
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_call_reservations WHERE call_kind='vision'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM vision_attempts"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_http_circuit_breaker_preserves_unprocessed_pending_rows(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)

    def unavailable(source: str, url: str, *, callbacks, **kwargs):
        hop = callbacks.reserve(url, 1)
        callbacks.complete(
            PhysicalRequestCompletion(
                hop,
                url,
                1,
                0.01,
                503,
                "response_received",
            )
        )
        raise FetchFailure(
            "http_retryable",
            "fixture 503",
            retryable=True,
            request_url=url,
            final_url=url,
            http_status=503,
            content_type="text/html",
            response_bytes=0,
            elapsed_seconds=0.01,
            request_count=1,
        )

    with pytest.raises(N10V12Paused, match="http_circuit_open"):
        run_semantic_vision_n10_v12(**{**args, "fetcher": unavailable})
    connection = sqlite3.connect(Path(str(args["output_db"]) + ".partial"))
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM external_call_reservations WHERE call_kind='http'"
        ).fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM vision_inputs WHERE status='pending'"
        ).fetchone()[0] > 0
        assert connection.execute(
            "SELECT COUNT(*) FROM external_call_reservations WHERE call_kind='vision'"
        ).fetchone()[0] == 0
    finally:
        connection.close()
