#!/usr/bin/env python3
"""Render the exact source-blind semantic Vision N10 v1.2 review set.

The renderer authenticates the production selection, semantic sidecar, and
retained 1024px derivatives before exposing only opaque ``semv12_`` IDs and
pixels.  Production use is deliberately disabled until literal release pins
are installed; tests pass an explicit :class:`ReviewPins` value.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.cross_source_image_selection import canonical_json, canonical_sha256  # noqa: E402
from tools.render_cross_source_semantic_review_sheets import (  # noqa: E402
    DEFAULT_IMAGES_PER_SHEET,
    ReviewInput,
    _pixel_identity,
    _render_sheet,
)


RENDERER_VERSION = "cross-source-semantic-blind-review-sheets-n10-v1.2.0"
MANIFEST_DOMAIN = "cross-source-semantic-blind-review-n10-v1.2-manifest-v1"
ORDER_DOMAIN = "cross-source-semantic-blind-review-n10-v1.2-order-v1"
REVIEW_SEED = "archibe-semantic-vision-n10-v1.2-blind-review-v1"
OPAQUE_ID = re.compile(r"^semv12_[0-9]{6}$")
TERMINAL_STATUSES = frozenset({"complete", "complete_with_failures"})


@dataclass(frozen=True)
class ReviewPins:
    selection_manifest_domain: str
    selection_manifest_size_bytes: int
    selection_manifest_byte_sha256: str
    selection_manifest_self_sha256: str
    ordered_building_manifest_sha256: str
    ordered_occurrence_manifest_sha256: str
    ordered_fetch_url_manifest_sha256: str
    ordered_e1_pixel_manifest_sha256: str
    run_id: str
    runner_version: str
    schema_version: int
    contract_version: str
    prompt_version: str
    output_schema_sha256: str
    schema_sql_sha256: str
    schema_objects_sha256: str
    transform_version: str
    e1_contract_version: str
    dependency_manifest_sha256: str
    runtime_version: str
    retry_policy_version: str
    model: str
    reasoning: str
    service_tier: str
    image_detail: str
    cli_version: str
    publication_contract_version: str
    prior_run_names_json: str
    prior_successful_derivative_count: int
    max_model_call_reservations: int
    observed_input_token_stop_threshold: int
    observed_output_token_stop_threshold: int
    observed_total_token_stop_threshold: int
    batch_size: int = 5
    max_fetch_attempts: int = 3
    max_vision_attempts: int = 2
    inference_id_prefix: str = "semv12_"
    building_count: int = 10
    occurrence_count: int = 50
    review_seed: str = REVIEW_SEED

    def __post_init__(self) -> None:
        sha_fields = (
            "selection_manifest_byte_sha256",
            "selection_manifest_self_sha256",
            "ordered_building_manifest_sha256",
            "ordered_occurrence_manifest_sha256",
            "ordered_fetch_url_manifest_sha256",
            "ordered_e1_pixel_manifest_sha256",
            "output_schema_sha256",
            "schema_sql_sha256",
            "schema_objects_sha256",
            "dependency_manifest_sha256",
        )
        for name in sha_fields:
            if re.fullmatch(r"[0-9a-f]{64}", str(getattr(self, name))) is None:
                raise ValueError(f"{name} must be lowercase SHA-256 hex")
        if self.building_count != 10 or self.occurrence_count <= 0:
            raise ValueError("v1.2 blind QA requires 10 buildings and a positive pinned occurrence count")
        if self.selection_manifest_size_bytes <= 0:
            raise ValueError("selection manifest size pin must be positive")
        if self.inference_id_prefix != "semv12_":
            raise ValueError("v1.2 blind QA requires the semv12_ namespace")
        expected = {
            "batch_size": 5,
            "max_fetch_attempts": 3,
            "max_vision_attempts": 2,
            "max_model_call_reservations": 12,
            "observed_input_token_stop_threshold": 250_000,
            "observed_output_token_stop_threshold": 10_000,
            "observed_total_token_stop_threshold": 260_000,
            "prior_successful_derivative_count": 665,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"v1.2 production envelope requires {name}={value}")


# Reviewed literal production envelope.  These values are never discovered
# from whichever artifacts happen to exist at execution time.
PRODUCTION_PINS: ReviewPins | None = ReviewPins(
    selection_manifest_domain="archibe-cross-source-semantic-coverage-n10-v1.2-manifest-v1",
    selection_manifest_size_bytes=983_229,
    selection_manifest_byte_sha256="00d494c58590a9a0ca3d78de858f857f6a1e226390018f5426d799a846ead8fe",
    selection_manifest_self_sha256="44f9c5ca47adbacd8fc0f87be11e59b6468d57205831e2c5d78b0f96438a495a",
    ordered_building_manifest_sha256="8008b9972ce779488e280a0709013a3a13916d9936ce1aaafb1fa1a13c3363fb",
    ordered_occurrence_manifest_sha256="939cea9f514a0caeb4fa4ec7bc9688f062ae212318f4d458ae551b04574cc314",
    ordered_fetch_url_manifest_sha256="1cf542b62edd93defd375d1e070f06975bae92ce4b5d80a2834a63d77e990d3e",
    ordered_e1_pixel_manifest_sha256="613bce3f755497e9944c7368c6d720bd913a7ee58d1523afef2bef09d3e05d5c",
    run_id="semn10v12-82ddad3e9cb37d3f640209c7",
    runner_version="cross-source-semantic-vision-n10-v1.2-runner-v2.0.0",
    schema_version=4,
    contract_version="cross-source-image-semantics-v1.2.0",
    prompt_version="cross-source-image-semantics-prompt-v1.2.0",
    output_schema_sha256="e72058e372e7883fc8ca80d690728fb16b9a7fdd469037f13311a8e04069eff4",
    schema_sql_sha256="7eebb5135000125d3e2a453bf48526d28518fe78fe2d861672b18b30bd382640",
    schema_objects_sha256="e77a4be8896aa474720155e217162a7592d0f5a6018c3b8c2c5ea1f09efc2401",
    transform_version="cross-source-vision-input-1024-jpeg-v1.0.0",
    e1_contract_version="archibe-e1-fingerprint-v1",
    dependency_manifest_sha256="31e93728ab3c2743d251cfa1e2392d36664bcb4322199fa21ce78ed032af0aba",
    runtime_version="divisare-codex-vision-runtime-v1.1.0",
    retry_policy_version="cross-source-semantic-vision-n10-v1.2-retry-v2.0.0",
    model="gpt-5.6-sol",
    reasoning="low",
    service_tier="fast",
    image_detail="high",
    cli_version="codex-cli 0.147.0",
    publication_contract_version="semantic-two-file-recoverable-publication-v2",
    prior_run_names_json=canonical_json(
        ["semantic_n100_v1", "semantic_n10_v1", "semantic_n10_v1_1"]
    ),
    prior_successful_derivative_count=665,
    max_model_call_reservations=12,
    observed_input_token_stop_threshold=250_000,
    observed_output_token_stop_threshold=10_000,
    observed_total_token_stop_threshold=260_000,
    building_count=10,
    occurrence_count=50,
)

DEFAULT_DB = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_2.db")
DEFAULT_SELECTION_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n10_v1_2.json")
DEFAULT_REVIEW_CACHE = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_2_review_cache")
DEFAULT_OUTPUT_DIR = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_2_review_sheets")


def _require_pins(pins: ReviewPins | None) -> ReviewPins:
    if pins is None:
        raise RuntimeError(
            "v1.2 production review pins are not installed; refusing artifact discovery"
        )
    return pins


def _file_sha(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _sidecars(path: Path) -> tuple[str, ...]:
    return tuple(
        str(candidate)
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    )


def _open_immutable(path: Path) -> sqlite3.Connection:
    if _sidecars(path):
        raise ValueError(f"semantic DB has SQLite sidecars: {_sidecars(path)}")
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _selection_manifest(path: Path, pins: ReviewPins) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) != pins.selection_manifest_size_bytes:
        raise ValueError("selection manifest byte size mismatch")
    if hashlib.sha256(raw).hexdigest() != pins.selection_manifest_byte_sha256:
        raise ValueError("selection manifest byte SHA mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or raw != (canonical_json(payload) + "\n").encode():
        raise ValueError("selection manifest is not canonical JSON plus LF")
    body = dict(payload)
    stored = body.pop("semantic_coverage_manifest_sha256", None)
    replayed = canonical_sha256(
        {"domain": pins.selection_manifest_domain, "manifest": body}
    )
    expected = {
        "self": pins.selection_manifest_self_sha256,
        "ordered_buildings": pins.ordered_building_manifest_sha256,
        "ordered_occurrences": pins.ordered_occurrence_manifest_sha256,
        "ordered_urls": pins.ordered_fetch_url_manifest_sha256,
        "ordered_pixels": pins.ordered_e1_pixel_manifest_sha256,
        "buildings": pins.building_count,
        "occurrences": pins.occurrence_count,
    }
    actual = {
        "self": stored,
        "ordered_buildings": payload.get("ordered_building_manifest_sha256"),
        "ordered_occurrences": payload.get("ordered_occurrence_manifest_sha256"),
        "ordered_urls": payload.get("ordered_fetch_url_manifest_sha256"),
        "ordered_pixels": payload.get("ordered_e1_pixel_manifest_sha256"),
        "buildings": payload.get("sample_size_buildings"),
        "occurrences": payload.get("planned_occurrence_count"),
    }
    if replayed != pins.selection_manifest_self_sha256 or actual != expected:
        raise ValueError(f"selection manifest identity mismatch: {actual!r}")
    return payload


def _expected_run_pins(pins: ReviewPins) -> dict[str, Any]:
    return {
        "building_count": pins.building_count,
        "contract_version": pins.contract_version,
        "batch_size": pins.batch_size,
        "cli_version": pins.cli_version,
        "dependency_manifest_sha256": pins.dependency_manifest_sha256,
        "e1_contract_version": pins.e1_contract_version,
        "image_detail": pins.image_detail,
        "inference_id_prefix": pins.inference_id_prefix,
        "manifest_byte_sha256": pins.selection_manifest_byte_sha256,
        "manifest_domain": pins.selection_manifest_domain,
        "manifest_self_sha256": pins.selection_manifest_self_sha256,
        "model": pins.model,
        "max_fetch_attempts": pins.max_fetch_attempts,
        "max_model_call_reservations": pins.max_model_call_reservations,
        "max_vision_attempts": pins.max_vision_attempts,
        "occurrence_count": pins.occurrence_count,
        "ordered_building_manifest_sha256": pins.ordered_building_manifest_sha256,
        "ordered_e1_pixel_manifest_sha256": pins.ordered_e1_pixel_manifest_sha256,
        "ordered_fetch_url_manifest_sha256": pins.ordered_fetch_url_manifest_sha256,
        "ordered_occurrence_manifest_sha256": pins.ordered_occurrence_manifest_sha256,
        "output_schema_sha256": pins.output_schema_sha256,
        "observed_input_token_stop_threshold": pins.observed_input_token_stop_threshold,
        "observed_output_token_stop_threshold": pins.observed_output_token_stop_threshold,
        "observed_total_token_stop_threshold": pins.observed_total_token_stop_threshold,
        "prior_run_names_json": pins.prior_run_names_json,
        "prior_successful_derivative_count": pins.prior_successful_derivative_count,
        "prompt_version": pins.prompt_version,
        "publication_contract_version": pins.publication_contract_version,
        "reasoning": pins.reasoning,
        "run_id": pins.run_id,
        "runner_version": pins.runner_version,
        "runtime_version": pins.runtime_version,
        "schema_objects_sha256": pins.schema_objects_sha256,
        "schema_sql_sha256": pins.schema_sql_sha256,
        "schema_version": pins.schema_version,
        "service_tier": pins.service_tier,
        "transform_version": pins.transform_version,
        "retry_policy_version": pins.retry_policy_version,
    }


def _blind_score(seed: str, inference_id: str, pixel_sha256: str) -> str:
    return canonical_sha256(
        {
            "domain": ORDER_DOMAIN,
            "inference_id": inference_id,
            "pixel_sha256": pixel_sha256,
            "seed": seed,
        }
    )


def _verified_inputs(
    semantic_db: Path,
    selection_manifest: Path,
    review_cache_dir: Path,
    pins: ReviewPins,
) -> tuple[dict[str, Any], list[ReviewInput], dict[str, Any], dict[str, Any]]:
    for path in (semantic_db, selection_manifest):
        if not path.is_file():
            raise FileNotFoundError(path)
    if not review_cache_dir.is_dir():
        raise FileNotFoundError(review_cache_dir)
    _selection_manifest(selection_manifest, pins)
    db_before = (semantic_db.stat().st_size, _file_sha(semantic_db))
    selection_before = (selection_manifest.stat().st_size, _file_sha(selection_manifest))
    sidecars_before = list(_sidecars(semantic_db))
    connection = _open_immutable(semantic_db)
    try:
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1:
            raise ValueError("semantic N10 v1.2 must contain exactly one run")
        run = dict(runs[0])
        expected_run = _expected_run_pins(pins)
        mismatches = {
            key: {"expected": value, "actual": run.get(key)}
            for key, value in expected_run.items()
            if run.get(key) != value
        }
        if str(run.get("status")) not in TERMINAL_STATUSES:
            mismatches["status"] = {
                "expected": sorted(TERMINAL_STATUSES),
                "actual": run.get("status"),
            }
        if mismatches:
            raise ValueError("semantic run pin mismatch: " + canonical_json(mismatches))
        metrics = json.loads(str(run["metrics_json"]))
        cache_provenance = metrics.get("materialization_cache")
        expected_cache = {
            "path": str(review_cache_dir.resolve()),
            "retained_for_review": True,
            "write_contract": "atomic-exclusive-sha256-v1",
        }
        if cache_provenance != expected_cache:
            raise ValueError("retained review-cache provenance mismatch")
        counts = {
            "buildings": int(connection.execute("SELECT COUNT(*) FROM selected_buildings WHERE run_id=?", (pins.run_id,)).fetchone()[0]),
            "occurrences": int(connection.execute("SELECT COUNT(*) FROM selected_occurrences WHERE run_id=?", (pins.run_id,)).fetchone()[0]),
            "results": int(connection.execute("SELECT COUNT(*) FROM semantic_results WHERE run_id=?", (pins.run_id,)).fetchone()[0]),
            "success": int(connection.execute("SELECT COUNT(*) FROM vision_inputs WHERE run_id=? AND status='success'", (pins.run_id,)).fetchone()[0]),
            "links": int(connection.execute("SELECT COUNT(*) FROM occurrence_result_links WHERE run_id=?", (pins.run_id,)).fetchone()[0]),
        }
        if (
            counts["buildings"] != pins.building_count
            or counts["occurrences"] != pins.occurrence_count
            or not 0 <= counts["results"] <= counts["links"] <= pins.occurrence_count
            or counts["success"] != counts["links"]
        ):
            raise ValueError(f"exact N10 v1.2 accounting mismatch: {counts!r}")
        rows = connection.execute(
            """
            SELECT o.input_rank,o.inference_id,v.status,
                   v.derivative_encoded_sha256,v.derivative_pixel_sha256,
                   v.derivative_width,v.derivative_height,v.derivative_bytes,
                   CASE WHEN l.result_inference_id IS NOT NULL
                              AND r.inference_id IS NOT NULL THEN 1 ELSE 0 END
                     AS model_result_available
            FROM selected_occurrences o JOIN vision_inputs v USING(run_id,inference_id)
            LEFT JOIN occurrence_result_links l
              ON l.run_id=o.run_id AND l.inference_id=o.inference_id
            LEFT JOIN semantic_results r
              ON r.run_id=l.run_id AND r.inference_id=l.result_inference_id
            WHERE o.run_id=? ORDER BY o.input_rank
            """,
            (pins.run_id,),
        ).fetchall()
        status_counts = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM vision_inputs "
                "WHERE run_id=? GROUP BY status ORDER BY status",
                (pins.run_id,),
            )
        }
        if sum(status_counts.values()) != pins.occurrence_count or any(
            status in {"pending", "ready"} for status in status_counts
        ):
            raise ValueError("review population contains non-terminal Vision inputs")
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or integrity != "ok" or foreign:
            raise ValueError("semantic DB SQLite validation failed")
    finally:
        connection.close()

    if len(rows) != pins.occurrence_count:
        raise ValueError(
            f"exactly {pins.occurrence_count} pinned review inputs are required"
        )
    expected_names = {f"{row['inference_id']}.jpg" for row in rows}
    actual_names = {path.name for path in review_cache_dir.glob("*.jpg") if path.is_file()}
    if actual_names != expected_names:
        raise ValueError("review-cache JPEG accounting mismatch")
    scored: list[tuple[str, ReviewInput]] = []
    result_availability: dict[str, bool] = {}
    for row in rows:
        inference_id = str(row["inference_id"])
        if not OPAQUE_ID.fullmatch(inference_id):
            raise ValueError(f"non-v1.2 opaque inference ID: {inference_id!r}")
        path = review_cache_dir / f"{inference_id}.jpg"
        pixel_sha, width, height, image_format = _pixel_identity(path)
        actual = (_file_sha(path), pixel_sha, width, height, path.stat().st_size, image_format)
        expected = (
            str(row["derivative_encoded_sha256"]),
            str(row["derivative_pixel_sha256"]),
            int(row["derivative_width"]),
            int(row["derivative_height"]),
            int(row["derivative_bytes"]),
            "JPEG",
        )
        if actual != expected:
            raise ValueError(f"review derivative mismatch: {inference_id}")
        available = bool(row["model_result_available"])
        if available != (str(row["status"]) == "success"):
            raise ValueError(f"Vision status/result availability mismatch: {inference_id}")
        result_availability[inference_id] = available
        item = ReviewInput(
            ordinal=0,
            input_rank=int(row["input_rank"]),
            inference_id=inference_id,
            cache_path=path,
            encoded_bytes=path.stat().st_size,
            encoded_sha256=actual[0],
            pixel_sha256=pixel_sha,
            width=width,
            height=height,
            vision_status=str(row["status"]),
        )
        scored.append((_blind_score(pins.review_seed, inference_id, pixel_sha), item))
    scored.sort(key=lambda pair: (pair[0], pair[1].inference_id))
    inputs = [dataclasses.replace(item, ordinal=index) for index, (_, item) in enumerate(scored, 1)]
    db_after = (semantic_db.stat().st_size, _file_sha(semantic_db))
    selection_after = (selection_manifest.stat().st_size, _file_sha(selection_manifest))
    sidecars_after = list(_sidecars(semantic_db))
    if db_before != db_after or selection_before != selection_after or sidecars_after:
        raise RuntimeError("renderer inputs changed or gained SQLite sidecars")
    db_record = {
        "byte_sha256_after": db_after[1],
        "byte_sha256_before": db_before[1],
        "filename": semantic_db.name,
        "size_bytes_after": db_after[0],
        "size_bytes_before": db_before[0],
    }
    selection_record = {
        "byte_sha256": selection_after[1],
        "manifest_domain": pins.selection_manifest_domain,
        "manifest_sha256": pins.selection_manifest_self_sha256,
        "ordered_building_manifest_sha256": pins.ordered_building_manifest_sha256,
        "ordered_e1_pixel_manifest_sha256": pins.ordered_e1_pixel_manifest_sha256,
        "ordered_fetch_url_manifest_sha256": pins.ordered_fetch_url_manifest_sha256,
        "ordered_occurrence_manifest_sha256": pins.ordered_occurrence_manifest_sha256,
        "size_bytes": selection_after[0],
    }
    run_record = {key: run[key] for key in expected_run}
    run_record["status"] = run["status"]
    run_record["logical_sha256"] = run["logical_sha256"]
    run_record["review_cache_provenance"] = {
        "path_sha256": hashlib.sha256(str(review_cache_dir.resolve()).encode()).hexdigest(),
        "retained_for_review": True,
        "write_contract": "atomic-exclusive-sha256-v1",
    }
    validation = {
        "foreign_key_violations": 0,
        "integrity_check": integrity,
        "quick_check": quick,
        "sqlite_sidecars_after": sidecars_after,
        "sqlite_sidecars_before": sidecars_before,
    }
    ordered_available = [
        item.inference_id for item in inputs if result_availability[item.inference_id]
    ]
    ordered_missing = [
        item.inference_id for item in inputs if not result_availability[item.inference_id]
    ]
    result_accounting = {
        "available_count": len(ordered_available),
        "available_inference_ids": ordered_available,
        "missing_count": len(ordered_missing),
        "missing_inference_ids": ordered_missing,
        "semantic_result_row_count": counts["results"],
        "successful_input_count": counts["success"],
        "terminal_input_status_counts": status_counts,
    }
    return db_record, inputs, selection_record, {
        "model_result_accounting": result_accounting,
        "result_availability": result_availability,
        "run": run_record,
        "validation": validation,
    }


def render_n10_v12_review_sheets(
    *,
    semantic_db: Path,
    selection_manifest: Path,
    review_cache_dir: Path,
    output_dir: Path,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> dict[str, Any]:
    """Verify and render the exact pinned blind population, without clobbering."""

    pins = _require_pins(pins)
    semantic_db = semantic_db.resolve()
    selection_manifest = selection_manifest.resolve()
    review_cache_dir = review_cache_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(output_dir)
    db_record, inputs, selection_record, details = _verified_inputs(
        semantic_db, selection_manifest, review_cache_dir, pins
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=False)
    sheets: list[dict[str, Any]] = []
    for offset in range(0, len(inputs), DEFAULT_IMAGES_PER_SHEET):
        batch = inputs[offset : offset + DEFAULT_IMAGES_PER_SHEET]
        sheet_no = len(sheets) + 1
        filename = f"sheet_{sheet_no:03d}.jpg"
        path = output_dir / filename
        _render_sheet(path, batch)
        sheets.append(
            {
                "filename": filename,
                "inference_ids": [item.inference_id for item in batch],
                "sha256": _file_sha(path),
                "sheet_no": sheet_no,
                "size_bytes": path.stat().st_size,
            }
        )
    ids = [item.inference_id for item in inputs]
    body: dict[str, Any] = {
        "blind_labels_only": True,
        "images_per_sheet": DEFAULT_IMAGES_PER_SHEET,
        "input_count": len(inputs),
        "inputs": [
            {
                **item.manifest_record(),
                "model_result_available": details["result_availability"][item.inference_id],
            }
            for item in inputs
        ],
        "llm_requests": 0,
        "network_requests": 0,
        "model_result_accounting": details["model_result_accounting"],
        "ordered_inference_ids": ids,
        "ordered_inference_ids_sha256": canonical_sha256(
            {"domain": ORDER_DOMAIN, "ordered_inference_ids": ids}
        ),
        "renderer_version": RENDERER_VERSION,
        "review_seed": pins.review_seed,
        "selection_manifest": selection_record,
        "semantic_db": db_record,
        "semantic_labels_exposed": False,
        "semantic_run": details["run"],
        "sheet_count": len(sheets),
        "sheets": sheets,
        "validation": details["validation"],
        "vision_requests": 0,
    }
    body["manifest_sha256"] = canonical_sha256(
        {"domain": MANIFEST_DOMAIN, "manifest": body}
    )
    (output_dir / "manifest.json").write_text(
        canonical_json(body) + "\n", encoding="utf-8", newline="\n"
    )
    return body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render exact blind semantic N10 v1.2 sheets.")
    parser.add_argument("--semantic-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--selection-manifest", type=Path, default=DEFAULT_SELECTION_MANIFEST)
    parser.add_argument("--review-cache-dir", type=Path, default=DEFAULT_REVIEW_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = render_n10_v12_review_sheets(
        semantic_db=args.semantic_db,
        selection_manifest=args.selection_manifest,
        review_cache_dir=args.review_cache_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps({
        "input_count": result["input_count"],
        "manifest_sha256": result["manifest_sha256"],
        "network_requests": 0,
        "ordered_inference_ids_sha256": result["ordered_inference_ids_sha256"],
        "sheet_count": result["sheet_count"],
        "vision_requests": 0,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
