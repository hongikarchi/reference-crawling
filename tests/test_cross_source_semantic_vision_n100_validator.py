from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import canonical.cross_source_semantic_vision_n100_validator as validator
from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage_n100 import (
    SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN,
)
from canonical.cross_source_semantic_vision_n100_sidecar import RuntimeContract
from canonical.divisare_vision_runtime import RUNTIME_VERSION
from tests.test_cross_source_semantic_vision_n100_sidecar import (
    _run_args,
    _vision_result,
    _fetcher,
)


def test_production_manifest_literal_pins_and_ordered_replay() -> None:
    payload, raw = validator._load_manifest(
        Path("data/reports/cross_source_semantic_coverage_n100_v1.json")
    )
    buildings, occurrences = validator._manifest_rows(payload)
    assert hashlib.sha256(raw).hexdigest() == validator.FROZEN_MANIFEST_BYTE_SHA256
    assert len(buildings) == 100
    assert len(occurrences) == 562
    assert buildings[0]["building_rank"] == 1
    assert occurrences[0]["inference_id"] == "semv100_000001"
    assert occurrences[-1]["inference_id"] == "semv100_000562"


def test_preflight_builds_exact_production_lineage_specs_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def replay(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return SimpleNamespace(as_dict=lambda: {"passed": True})

    monkeypatch.setattr(
        validator, "validate_semantic_coverage_n100_manifest", replay
    )
    result = validator.validate_frozen_n100_manifest_preflight(
        Path("data/reports/cross_source_semantic_coverage_n100_v1.json"),
        e2_path=Path(
            "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"
        ),
        e3_path=Path(
            "data/enrichment/divisare_architizer_image_selection_e3_full_v1.db"
        ),
        n10_manifest_path=Path(
            "data/reports/cross_source_semantic_coverage_n10_v1.json"
        ),
        n10_sidecar_path=Path(
            "data/enrichment/divisare_architizer_semantic_vision_n10_v1.db"
        ),
    )
    assert result["passed"] is True
    assert captured["expected_seed"] == validator.FROZEN_SAMPLE_SEED
    assert captured["e2_spec"].expected_size == 10_164_682_752
    assert captured["e3_spec"].expected_size == 10_236_592_128
    assert captured["n10_spec"].expected_sidecar_byte_sha256 == (
        validator.FROZEN_N10_SHA256
    )


def _source_artifact(path: Path, *, label: str) -> tuple[dict[str, object], dict[str, object]]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA application_id=17")
        connection.execute("PRAGMA user_version=1")
        if label == "e2":
            connection.executescript(
                """
                CREATE TABLE e2_runs(
                  run_id TEXT, contract_version TEXT, builder_version TEXT,
                  status TEXT, selection_mode TEXT
                );
                CREATE TABLE e2_metrics(
                  run_id TEXT, phase TEXT, metric_name TEXT,
                  stratum_json TEXT, value_text TEXT
                );
                INSERT INTO e2_runs VALUES('e2-run','e2-contract','e2-builder','complete','full');
                """
            )
            metric_table = "e2_metrics"
            prefix = "e2"
        else:
            connection.executescript(
                """
                CREATE TABLE selection_runs(
                  run_id TEXT, contract_version TEXT, builder_version TEXT,
                  status TEXT, selection_mode TEXT, network_requests INTEGER,
                  vision_requests INTEGER, llm_requests INTEGER,
                  artifact_scope TEXT
                );
                CREATE TABLE selection_metrics(
                  run_id TEXT, phase TEXT, metric_name TEXT,
                  stratum_json TEXT, value_text TEXT
                );
                INSERT INTO selection_runs VALUES(
                  'e3-run','e3-contract','e3-builder','complete','full',0,0,0,
                  'candidate_only'
                );
                """
            )
            metric_table = "selection_metrics"
            prefix = "e3"
        logical = hashlib.sha256(f"{label}-logical".encode()).hexdigest()
        connection.execute(
            f"INSERT INTO {metric_table} VALUES(?, 'validation', "
            "'output_logical_sha256', '{}', ?)",
            (f"{label}-run", logical),
        )
        connection.commit()
    finally:
        connection.close()
    raw = path.read_bytes()
    byte_sha = hashlib.sha256(raw).hexdigest()
    record = {
        "application_id": 17,
        "builder_version": f"{label}-builder",
        "byte_sha256": byte_sha,
        "contract_version": f"{label}-contract",
        "logical_sha256": logical,
        "path": str(path.resolve()),
        "run_id": f"{label}-run",
        "size_bytes": len(raw),
        "user_version": 1,
    }
    run = {
        f"{prefix}_path": str(path.resolve()),
        f"{prefix}_size": len(raw),
        f"{prefix}_sha256_before": byte_sha,
        f"{prefix}_sha256_after": byte_sha,
        f"{prefix}_logical_sha256": logical,
        f"{prefix}_run_id": f"{label}-run",
    }
    return record, run


@pytest.mark.parametrize("label", ["e2", "e3"])
def test_artifact_lineage_reads_run_and_validation_metric_tables(
    tmp_path: Path, label: str
) -> None:
    record, run = _source_artifact(tmp_path / f"{label}.db", label=label)
    checks: list[validator.ValidationCheck] = []
    actual = validator._validate_artifact(
        checks, label, tmp_path / f"{label}.db", record, run  # type: ignore[arg-type]
    )
    assert actual["logical_sha"] == record["logical_sha256"]
    assert len(checks) == 1
    assert checks[0].passed


def _add_ordered_pins(
    payload: dict[str, object],
) -> tuple[str, str, str, str]:
    buildings: list[dict[str, object]] = []
    occurrences: list[dict[str, object]] = []
    urls: list[dict[str, object]] = []
    pixels: list[dict[str, object]] = []
    input_rank = 0
    for building_rank, wrapper in enumerate(
        payload["selected_buildings"], 1  # type: ignore[arg-type,index]
    ):
        selected = wrapper["selected_building"]
        building = selected["building"]
        buildings.append(
            {
                "rank": building_rank,
                "record_sha256": wrapper["selected_building_record_sha256"],
            }
        )
        for occurrence in selected["coverage_plan"]["selected_occurrences"]:
            input_rank += 1
            candidate = occurrence["occurrence"]["candidate"]
            occurrences.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "occurrence_record_sha256": occurrence[
                        "occurrence_record_sha256"
                    ],
                    "selection_id": building["selection_id"],
                }
            )
            urls.append(
                {
                    "fetch_url": candidate["fetch_url"],
                    "input_rank": input_rank,
                    "source": candidate["source"],
                }
            )
            pixels.append(
                {
                    "input_rank": input_rank,
                    "normalized_pixel_sha256": candidate["normalized_pixel_sha256"],
                    "source": candidate["source"],
                }
            )
    url_sha = canonical_sha256(
        {"ordered_fetch_urls": urls, "version": validator.SELECTION_VERSION}
    )
    pixel_sha = canonical_sha256(
        {"ordered_e1_pixels": pixels, "version": validator.SELECTION_VERSION}
    )
    building_sha = canonical_sha256(
        {"ordered_buildings": buildings, "version": validator.SELECTION_VERSION}
    )
    occurrence_sha = canonical_sha256(
        {
            "ordered_occurrences": occurrences,
            "version": validator.SELECTION_VERSION,
        }
    )
    payload["ordered_building_manifest_sha256"] = building_sha
    payload["ordered_occurrence_manifest_sha256"] = occurrence_sha
    payload["ordered_fetch_url_manifest_sha256"] = url_sha
    payload["ordered_e1_pixel_manifest_sha256"] = pixel_sha
    return building_sha, occurrence_sha, url_sha, pixel_sha


def test_terminal_validator_accepts_durable_executor_exception_then_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, images, _ = _run_args(tmp_path)
    payload = json.loads(Path(args["manifest_path"]).read_text(encoding="utf-8"))
    payload.pop("semantic_coverage_manifest_sha256")
    building_sha, occurrence_sha, url_sha, pixel_sha = _add_ordered_pins(payload)
    payload["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN, "manifest": payload}
    )
    raw = (canonical_json(payload) + "\n").encode("utf-8")
    Path(args["manifest_path"]).write_bytes(raw)
    contract = RuntimeContract(
        hashlib.sha256(raw).hexdigest(),
        str(payload["semantic_coverage_manifest_sha256"]),
        str(payload["sample_seed"]),
        100,
        100,
        str(payload["ordered_building_manifest_sha256"]),
        str(payload["ordered_occurrence_manifest_sha256"]),
    )
    review_cache = tmp_path / "review-cache"
    args["contract"] = contract
    args["review_cache_dir"] = review_cache
    calls = 0

    def executor(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic executor crash")
        result = _vision_result(kwargs, ok=True)
        records = list(result.records)
        stdout = "\n".join(
            (
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
                            "cached_input_tokens": 10,
                            "output_tokens": 20,
                        },
                    }
                ),
            )
        ) + "\n"
        return replace(
            result,
            stdout=stdout,
            provenance=replace(result.provenance, runtime_version=RUNTIME_VERSION),
        )

    run_result = __import__(
        "canonical.cross_source_semantic_vision_n100_sidecar",
        fromlist=["run_semantic_vision_n100"],
    ).run_semantic_vision_n100(
        **args,
        fetcher=_fetcher(images),
        executor=executor,
    )
    assert run_result.status == "complete"
    assert calls == 21

    n10_manifest = tmp_path / "n10-manifest.json"
    n10_manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(validator, "FROZEN_MANIFEST_BYTE_SHA256", contract.manifest_byte_sha256)
    monkeypatch.setattr(validator, "FROZEN_MANIFEST_SELF_SHA256", contract.manifest_self_sha256)
    monkeypatch.setattr(validator, "FROZEN_OCCURRENCE_COUNT", 100)
    monkeypatch.setattr(validator, "FROZEN_ORDERED_BUILDING_SHA256", building_sha)
    monkeypatch.setattr(validator, "FROZEN_ORDERED_OCCURRENCE_SHA256", occurrence_sha)
    monkeypatch.setattr(validator, "FROZEN_ORDERED_FETCH_URL_SHA256", url_sha)
    monkeypatch.setattr(validator, "FROZEN_ORDERED_E1_PIXEL_SHA256", pixel_sha)
    monkeypatch.setattr(validator, "FROZEN_N10_SHA256", args["expected_n10_sidecar_sha256"])
    monkeypatch.setattr(validator, "PLANNED_BATCH_COUNT", 20)
    monkeypatch.setattr(validator, "MAX_TOTAL_MODEL_CALLS", 32)
    monkeypatch.setattr(
        validator,
        "_load_n10",
        lambda *_args, **_kwargs: (set(), set(), {"fixture": True}),
    )
    monkeypatch.setattr(
        validator,
        "_independent_manifest_replay",
        lambda **_kwargs: {"passed": True},
    )
    monkeypatch.setattr(
        validator,
        "_validate_artifact",
        lambda *_args, **_kwargs: {"fixture": True},
    )
    validation = validator.validate_semantic_vision_n100(
        Path(args["output_db"]),
        report_path=Path(args["report_path"]),
        manifest_path=Path(args["manifest_path"]),
        e2_path=Path(args["e2_path"]),
        e3_path=Path(args["e3_path"]),
        n10_manifest_path=n10_manifest,
        n10_sidecar_path=Path(args["n10_sidecar_path"]),
        review_cache_dir=review_cache,
    )
    failed = [check.as_dict() for check in validation.checks if not check.passed]
    assert validation.passed, failed
    assert validation.counts["vision_attempts"] == 21
    assert validation.counts["vision_retries"] == 1
    connection = sqlite3.connect(args["output_db"])
    try:
        first = connection.execute(
            "SELECT attempt_no,status,error_kind,error_message FROM vision_attempts "
            "ORDER BY attempt_id LIMIT 1"
        ).fetchone()
        assert first == (1, "failed", "RuntimeError", "synthetic executor crash")
    finally:
        connection.close()


def test_validator_does_not_import_n100_runner() -> None:
    source = Path(validator.__file__).read_text(encoding="utf-8")
    assert "cross_source_semantic_vision_n100_sidecar" not in source
