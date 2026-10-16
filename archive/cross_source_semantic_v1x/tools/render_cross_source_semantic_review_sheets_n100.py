#!/usr/bin/env python3
"""Render deterministic, source-blind review sheets for semantic Vision N100.

This tool is deliberately local-only.  It verifies every retained JPEG against
the immutable semantic sidecar, replaces input ordering with a separately
seeded blind order, and renders only opaque inference IDs.  It never reads or
emits semantic labels, source names, project names, or URLs.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.cross_source_image_selection import canonical_json, canonical_sha256  # noqa: E402
from tools.render_cross_source_semantic_review_sheets import (  # noqa: E402
    DEFAULT_IMAGES_PER_SHEET,
    ReviewInput,
    _pixel_identity,
    _render_sheet,
)


RENDERER_VERSION = "cross-source-semantic-blind-review-sheets-n100-v1.0.0"
MANIFEST_DOMAIN = "cross-source-semantic-blind-review-n100-manifest-v1"
ORDER_DOMAIN = "cross-source-semantic-blind-review-n100-order-v1"
DEFAULT_REVIEW_SEED = "archibe-semantic-vision-n100-blind-review-v1"
OPAQUE_ID = re.compile(r"^semv100_[0-9]{6}$")
TERMINAL_STATUSES = frozenset({"complete", "complete_with_failures"})


def _file_sha256(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _blind_score(seed: str, inference_id: str, pixel_sha256: str) -> str:
    return canonical_sha256(
        {
            "domain": ORDER_DOMAIN,
            "inference_id": inference_id,
            "pixel_sha256": pixel_sha256,
            "seed": seed,
        }
    )


def _load_verified_inputs(
    semantic_db: Path,
    review_cache_dir: Path,
    *,
    review_seed: str,
) -> tuple[dict[str, Any], list[ReviewInput], dict[str, Any]]:
    if not semantic_db.is_file():
        raise FileNotFoundError(f"semantic DB does not exist: {semantic_db}")
    if not review_cache_dir.is_dir():
        raise FileNotFoundError(f"review cache does not exist: {review_cache_dir}")
    if not review_seed.strip():
        raise ValueError("review seed must be non-empty")

    size_before = semantic_db.stat().st_size
    sha_before = _file_sha256(semantic_db)
    connection = _open_immutable(semantic_db)
    try:
        runs = connection.execute(
            """
            SELECT run_id,status,runner_version,contract_version,prompt_version,
                   output_schema_sha256,transform_version,model,reasoning,
                   service_tier,COALESCE(logical_sha256,'') AS logical_sha256,
                   building_count,occurrence_count,metrics_json
            FROM semantic_runs
            """
        ).fetchall()
        if len(runs) != 1:
            raise ValueError(f"semantic DB must contain exactly one run, found {len(runs)}")
        run = dict(runs[0])
        if int(run["building_count"]) != 100:
            raise ValueError(
                f"N100 sidecar must declare 100 buildings, found {run['building_count']}"
            )
        if str(run["status"]) not in TERMINAL_STATUSES:
            raise ValueError(f"N100 sidecar is not terminal: {run['status']}")
        try:
            metrics = json.loads(str(run["metrics_json"]))
            cache_provenance = metrics["materialization_cache"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("N100 sidecar lacks review-cache provenance") from exc
        if cache_provenance.get("retained_for_review") is not True:
            raise ValueError("N100 sidecar did not retain its review cache")
        stored_cache_path = cache_provenance.get("path")
        if not isinstance(stored_cache_path, str) or (
            Path(stored_cache_path).resolve() != review_cache_dir.resolve()
        ):
            raise ValueError(
                "requested review-cache path differs from N100 runtime provenance"
            )
        rows = connection.execute(
            """
            SELECT o.input_rank,o.inference_id,v.status,
                   v.derivative_encoded_sha256,v.derivative_pixel_sha256,
                   v.derivative_width,v.derivative_height,v.derivative_bytes
            FROM selected_occurrences AS o
            JOIN vision_inputs AS v
              ON v.run_id=o.run_id AND v.inference_id=o.inference_id
            WHERE o.run_id=? AND v.derivative_encoded_sha256 IS NOT NULL
            ORDER BY o.input_rank,o.inference_id
            """,
            (run["run_id"],),
        ).fetchall()
        selected_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM selected_occurrences WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0]
        )
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick != "ok":
            raise ValueError(f"semantic DB quick_check failed: {quick}")
    finally:
        connection.close()

    if not rows:
        raise ValueError("N100 sidecar has no retained Vision derivatives")
    expected_names = {f"{row['inference_id']}.jpg" for row in rows}
    actual_names = {
        path.name for path in review_cache_dir.glob("*.jpg") if path.is_file()
    }
    if expected_names != actual_names:
        raise ValueError(
            "review-cache JPEG accounting mismatch: "
            f"missing={sorted(expected_names - actual_names)}, "
            f"unexpected={sorted(actual_names - expected_names)}"
        )

    verified: list[ReviewInput] = []
    scored: list[tuple[str, ReviewInput]] = []
    for row in rows:
        inference_id = str(row["inference_id"])
        if not OPAQUE_ID.fullmatch(inference_id):
            raise ValueError(f"non-opaque inference ID in semantic DB: {inference_id!r}")
        path = review_cache_dir / f"{inference_id}.jpg"
        actual = {
            "encoded_bytes": path.stat().st_size,
            "encoded_sha256": _file_sha256(path),
        }
        pixel_sha, width, height, decoded_format = _pixel_identity(path)
        actual.update(
            {
                "height": height,
                "pixel_sha256": pixel_sha,
                "width": width,
            }
        )
        if decoded_format != "JPEG":
            raise ValueError(f"review-cache input is not JPEG: {inference_id}")
        expected = {
            "encoded_bytes": int(row["derivative_bytes"]),
            "encoded_sha256": str(row["derivative_encoded_sha256"]),
            "height": int(row["derivative_height"]),
            "pixel_sha256": str(row["derivative_pixel_sha256"]),
            "width": int(row["derivative_width"]),
        }
        if actual != expected:
            raise ValueError(
                f"review-cache derivative identity mismatch for {inference_id}: "
                f"expected={expected}, actual={actual}"
            )
        item = ReviewInput(
            ordinal=0,
            input_rank=int(row["input_rank"]),
            inference_id=inference_id,
            cache_path=path,
            encoded_bytes=actual["encoded_bytes"],
            encoded_sha256=actual["encoded_sha256"],
            pixel_sha256=pixel_sha,
            width=width,
            height=height,
            vision_status=str(row["status"]),
        )
        scored.append((_blind_score(review_seed, inference_id, pixel_sha), item))

    scored.sort(key=lambda pair: (pair[0], pair[1].inference_id))
    for ordinal, (_, item) in enumerate(scored, 1):
        verified.append(dataclasses.replace(item, ordinal=ordinal))

    size_after = semantic_db.stat().st_size
    sha_after = _file_sha256(semantic_db)
    if (size_before, sha_before) != (size_after, sha_after):
        raise RuntimeError("semantic DB changed during immutable review input validation")
    db_record = {
        "byte_sha256_after": sha_after,
        "byte_sha256_before": sha_before,
        "filename": semantic_db.name,
        "size_bytes_after": size_after,
        "size_bytes_before": size_before,
    }
    run_record = {
        key: run[key]
        for key in (
            "building_count",
            "contract_version",
            "logical_sha256",
            "model",
            "occurrence_count",
            "output_schema_sha256",
            "prompt_version",
            "reasoning",
            "run_id",
            "runner_version",
            "service_tier",
            "status",
            "transform_version",
        )
    }
    run_record["selected_occurrence_rows"] = selected_count
    run_record["review_cache_provenance"] = {
        "path_sha256": hashlib.sha256(
            str(review_cache_dir.resolve()).encode("utf-8")
        ).hexdigest(),
        "retained_for_review": True,
        "write_contract": cache_provenance.get("write_contract"),
    }
    return db_record, verified, run_record


def render_n100_review_sheets(
    *,
    semantic_db: Path,
    review_cache_dir: Path,
    output_dir: Path,
    review_seed: str = DEFAULT_REVIEW_SEED,
) -> dict[str, Any]:
    """Verify N100 inputs and create a new six-image-per-sheet directory."""

    semantic_db = semantic_db.resolve()
    review_cache_dir = review_cache_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"blind-review output already exists: {output_dir}")
    db_record, inputs, run_record = _load_verified_inputs(
        semantic_db, review_cache_dir, review_seed=review_seed
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
                "sheet_no": sheet_no,
                "sha256": _file_sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )

    ordered_ids = [item.inference_id for item in inputs]
    body: dict[str, Any] = {
        "blind_labels_only": True,
        "images_per_sheet": DEFAULT_IMAGES_PER_SHEET,
        "input_count": len(inputs),
        "inputs": [item.manifest_record() for item in inputs],
        "llm_requests": 0,
        "network_requests": 0,
        "ordered_inference_ids": ordered_ids,
        "ordered_inference_ids_sha256": canonical_sha256(
            {"domain": ORDER_DOMAIN, "ordered_inference_ids": ordered_ids}
        ),
        "renderer_version": RENDERER_VERSION,
        "review_seed": review_seed,
        "semantic_db": db_record,
        "semantic_labels_exposed": False,
        "semantic_run": run_record,
        "sheet_count": len(sheets),
        "sheets": sheets,
        "vision_requests": 0,
    }
    body["manifest_sha256"] = canonical_sha256(
        {"domain": MANIFEST_DOMAIN, "manifest": body}
    )
    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(body) + "\n")
    return body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render deterministic local-only blind N100 review sheets."
    )
    parser.add_argument("--semantic-db", type=Path, required=True)
    parser.add_argument("--review-cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--review-seed", default=DEFAULT_REVIEW_SEED)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = render_n100_review_sheets(
        semantic_db=args.semantic_db,
        review_cache_dir=args.review_cache_dir,
        output_dir=args.output_dir,
        review_seed=args.review_seed,
    )
    print(
        json.dumps(
            {
                "input_count": result["input_count"],
                "manifest_sha256": result["manifest_sha256"],
                "network_requests": 0,
                "ordered_inference_ids_sha256": result[
                    "ordered_inference_ids_sha256"
                ],
                "output_dir": str(args.output_dir.resolve()),
                "sheet_count": result["sheet_count"],
                "vision_requests": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
