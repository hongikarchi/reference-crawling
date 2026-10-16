#!/usr/bin/env python3
"""Render the byte-pinned, source-blind semantic Vision N10 v1.1 review set.

The renderer sees runtime provenance and pixels, but never reads semantic
result values.  Its manifest exposes opaque ``semv11_`` IDs only.  Production
pins are literal so a different selection, prompt, schema, runner, or run ID
cannot silently become the acceptance sample.
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


RENDERER_VERSION = "cross-source-semantic-blind-review-sheets-n10-v1.1.0"
MANIFEST_DOMAIN = "cross-source-semantic-blind-review-n10-v1.1-manifest-v1"
ORDER_DOMAIN = "cross-source-semantic-blind-review-n10-v1.1-order-v1"
SELECTION_MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-n10-v1.1-manifest-v1"
REVIEW_SEED = "archibe-semantic-vision-n10-v1.1-blind-review-v1"
OPAQUE_ID = re.compile(r"^semv11_[0-9]{6}$")
TERMINAL_STATUSES = frozenset({"complete", "complete_with_failures"})


@dataclass(frozen=True)
class ReviewPins:
    selection_manifest_byte_sha256: str
    selection_manifest_self_sha256: str
    ordered_building_manifest_sha256: str
    ordered_occurrence_manifest_sha256: str
    ordered_fetch_url_manifest_sha256: str
    ordered_e1_pixel_manifest_sha256: str
    run_id: str
    runner_version: str
    contract_version: str
    prompt_version: str
    output_schema_sha256: str
    schema_sql_sha256: str
    schema_objects_sha256: str
    transform_version: str
    model: str
    reasoning: str
    service_tier: str
    building_count: int = 10
    occurrence_count: int = 53
    review_seed: str = REVIEW_SEED


PRODUCTION_PINS = ReviewPins(
    selection_manifest_byte_sha256="3315a48188aec4c9461867235999ac7e526b8b011a869cafdd3be9d3cd1cc5b6",
    selection_manifest_self_sha256="91957a822915ae70f56fd1a5f82244098a4508274cc5dbbdd91e1d6bf03ad8c9",
    ordered_building_manifest_sha256="7b4c9558682f231e4fce29d0e915666fe1968707e1fea177d58ac0ad514af55d",
    ordered_occurrence_manifest_sha256="0be0449356388c117e59856caacdf14900b694b0c1ee811f807b956c9449fcb6",
    ordered_fetch_url_manifest_sha256="9de60a38b4ef5e05744c0761c13e84d0a33fe88df6e09ea4b8c9ea24ac527c43",
    ordered_e1_pixel_manifest_sha256="0ec3a756a3a55480b077e6eb114f4b2df9d1497f0b4dd015e53008e1783324a8",
    run_id="semn10v11-b1c3c595b0810a2c91e02a61",
    runner_version="cross-source-semantic-vision-n10-v1.1-runner-v2.0.0",
    contract_version="cross-source-image-semantics-v1.1.0",
    prompt_version="cross-source-image-semantics-prompt-v1.1.0",
    output_schema_sha256="e72058e372e7883fc8ca80d690728fb16b9a7fdd469037f13311a8e04069eff4",
    schema_sql_sha256="85b12261ce7c720c21bbad74669c7bc89b0c7e185ba259ff9a552e15608df31e",
    schema_objects_sha256="959f95fe3a3ae9f6daa6ee9978eedc9abc00a5bad6c7fe6ef577ac03c02c3fe1",
    transform_version="cross-source-vision-input-1024-jpeg-v1.0.0",
    model="gpt-5.6-sol",
    reasoning="low",
    service_tier="fast",
)

DEFAULT_DB = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_1.db")
DEFAULT_SELECTION_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n10_v1_1.json")
DEFAULT_REVIEW_CACHE = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_1_review_cache")
DEFAULT_OUTPUT_DIR = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_1_review_sheets")


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
    if hashlib.sha256(raw).hexdigest() != pins.selection_manifest_byte_sha256:
        raise ValueError("selection manifest byte SHA mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or raw != (canonical_json(payload) + "\n").encode():
        raise ValueError("selection manifest is not canonical JSON plus LF")
    body = dict(payload)
    stored = body.pop("semantic_coverage_manifest_sha256", None)
    replayed = canonical_sha256(
        {"domain": SELECTION_MANIFEST_DOMAIN, "manifest": body}
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
    selection_before = (
        selection_manifest.stat().st_size,
        _file_sha(selection_manifest),
    )
    connection = _open_immutable(semantic_db)
    try:
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1:
            raise ValueError("semantic N10 v1.1 must contain exactly one run")
        run = dict(runs[0])
        expected_run = {
            "run_id": pins.run_id,
            "runner_version": pins.runner_version,
            "contract_version": pins.contract_version,
            "prompt_version": pins.prompt_version,
            "output_schema_sha256": pins.output_schema_sha256,
            "schema_sql_sha256": pins.schema_sql_sha256,
            "schema_objects_sha256": pins.schema_objects_sha256,
            "transform_version": pins.transform_version,
            "model": pins.model,
            "reasoning": pins.reasoning,
            "service_tier": pins.service_tier,
            "manifest_byte_sha256": pins.selection_manifest_byte_sha256,
            "manifest_self_sha256": pins.selection_manifest_self_sha256,
            "ordered_building_manifest_sha256": pins.ordered_building_manifest_sha256,
            "ordered_occurrence_manifest_sha256": pins.ordered_occurrence_manifest_sha256,
            "ordered_fetch_url_manifest_sha256": pins.ordered_fetch_url_manifest_sha256,
            "ordered_e1_pixel_manifest_sha256": pins.ordered_e1_pixel_manifest_sha256,
            "building_count": pins.building_count,
            "occurrence_count": pins.occurrence_count,
        }
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
        }
        expected_counts = {
            "buildings": pins.building_count,
            "occurrences": pins.occurrence_count,
            "results": pins.occurrence_count,
            "success": pins.occurrence_count,
        }
        if counts != expected_counts:
            raise ValueError(f"exact N10 v1.1 accounting mismatch: {counts!r}")
        rows = connection.execute(
            """
            SELECT o.input_rank,o.inference_id,v.status,
                   v.derivative_encoded_sha256,v.derivative_pixel_sha256,
                   v.derivative_width,v.derivative_height,v.derivative_bytes
            FROM selected_occurrences o JOIN vision_inputs v USING(run_id,inference_id)
            WHERE o.run_id=? ORDER BY o.input_rank
            """,
            (pins.run_id,),
        ).fetchall()
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or integrity != "ok" or foreign:
            raise ValueError("semantic DB SQLite validation failed")
    finally:
        connection.close()

    if len(rows) != pins.occurrence_count:
        raise ValueError("exact 53 review inputs are required")
    expected_names = {f"{row['inference_id']}.jpg" for row in rows}
    actual_names = {path.name for path in review_cache_dir.glob("*.jpg") if path.is_file()}
    if actual_names != expected_names:
        raise ValueError("review-cache JPEG accounting mismatch")
    scored: list[tuple[str, ReviewInput]] = []
    for row in rows:
        inference_id = str(row["inference_id"])
        if not OPAQUE_ID.fullmatch(inference_id):
            raise ValueError(f"non-v1.1 opaque inference ID: {inference_id!r}")
        path = review_cache_dir / f"{inference_id}.jpg"
        pixel_sha, width, height, image_format = _pixel_identity(path)
        actual = (
            _file_sha(path), pixel_sha, width, height, path.stat().st_size, image_format
        )
        expected = (
            str(row["derivative_encoded_sha256"]),
            str(row["derivative_pixel_sha256"]),
            int(row["derivative_width"]),
            int(row["derivative_height"]),
            int(row["derivative_bytes"]),
            "JPEG",
        )
        if actual != expected or str(row["status"]) != "success":
            raise ValueError(f"review derivative mismatch: {inference_id}")
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
            vision_status="success",
        )
        scored.append((_blind_score(pins.review_seed, inference_id, pixel_sha), item))
    scored.sort(key=lambda pair: (pair[0], pair[1].inference_id))
    inputs = [
        dataclasses.replace(item, ordinal=index)
        for index, (_, item) in enumerate(scored, 1)
    ]
    db_after = (semantic_db.stat().st_size, _file_sha(semantic_db))
    selection_after = (
        selection_manifest.stat().st_size,
        _file_sha(selection_manifest),
    )
    if db_before != db_after or selection_before != selection_after or _sidecars(semantic_db):
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
        "sqlite_sidecars_after": [],
        "sqlite_sidecars_before": [],
    }
    return db_record, inputs, selection_record, {"run": run_record, "validation": validation}


def render_n10_v11_review_sheets(
    *,
    semantic_db: Path,
    selection_manifest: Path,
    review_cache_dir: Path,
    output_dir: Path,
    pins: ReviewPins = PRODUCTION_PINS,
) -> dict[str, Any]:
    """Verify and render the exact 53-image blind review, without clobbering."""

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
        "inputs": [item.manifest_record() for item in inputs],
        "llm_requests": 0,
        "network_requests": 0,
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
    parser = argparse.ArgumentParser(description="Render exact blind semantic N10 v1.1 sheets.")
    parser.add_argument("--semantic-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--selection-manifest", type=Path, default=DEFAULT_SELECTION_MANIFEST)
    parser.add_argument("--review-cache-dir", type=Path, default=DEFAULT_REVIEW_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = render_n10_v11_review_sheets(
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
