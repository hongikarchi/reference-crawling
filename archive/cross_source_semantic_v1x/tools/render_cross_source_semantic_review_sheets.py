#!/usr/bin/env python3
"""Render blind, local-only review sheets for semantic Vision N10 inputs.

The renderer reads a terminal or partial semantic sidecar and the explicitly
retained review-cache JPEGs.  It verifies each cached derivative against the
sidecar before drawing sheets that expose only ordinal numbers and opaque
``semv_`` inference IDs.  It contains no HTTP or model runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.cross_source_image_selection import canonical_json, canonical_sha256  # noqa: E402


RENDERER_VERSION = "cross-source-semantic-blind-review-sheets-v1.0.0"
MANIFEST_DOMAIN = "cross-source-semantic-blind-review-manifest-v1"
DEFAULT_IMAGES_PER_SHEET = 6
SHEET_WIDTH = 1800
SHEET_HEIGHT = 1200
SHEET_COLUMNS = 3
SHEET_ROWS = 2
OPAQUE_ID_PREFIX = "semv_"


@dataclass(frozen=True)
class ReviewInput:
    ordinal: int
    input_rank: int
    inference_id: str
    cache_path: Path
    encoded_bytes: int
    encoded_sha256: str
    pixel_sha256: str
    width: int
    height: int
    vision_status: str

    def manifest_record(self) -> dict[str, Any]:
        return {
            "cache_filename": self.cache_path.name,
            "derivative_encoded_bytes": self.encoded_bytes,
            "derivative_encoded_sha256": self.encoded_sha256,
            "derivative_height": self.height,
            "derivative_pixel_sha256": self.pixel_sha256,
            "derivative_width": self.width,
            "inference_id": self.inference_id,
            "input_rank": self.input_rank,
            "ordinal": self.ordinal,
            "vision_status": self.vision_status,
        }


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _pixel_identity(path: Path) -> tuple[str, int, int, str]:
    """Return the frozen Vision derivative pixel SHA, size, and format."""

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            image.load()
            decoded_format = str(image.format or "").upper()
            rgb = image.convert("RGB")
    except (UnidentifiedImageError, EOFError, OSError, SyntaxError, ValueError) as exc:
        raise ValueError(f"review-cache image cannot be decoded: {path.name}: {exc}") from exc
    if decoded_format != "JPEG":
        raise ValueError(f"review-cache input is not the frozen JPEG derivative: {path.name}")
    payload = b"RGB\0" + struct.pack(">II", rgb.width, rgb.height) + rgb.tobytes()
    return hashlib.sha256(payload).hexdigest(), rgb.width, rgb.height, decoded_format


def _load_verified_inputs(
    semantic_db: Path,
    review_cache_dir: Path,
) -> tuple[dict[str, Any], list[ReviewInput], dict[str, Any]]:
    if not semantic_db.is_file():
        raise FileNotFoundError(f"semantic DB does not exist: {semantic_db}")
    if not review_cache_dir.is_dir():
        raise FileNotFoundError(f"review-cache directory does not exist: {review_cache_dir}")

    db_size_before = semantic_db.stat().st_size
    db_sha_before = _sha256_file(semantic_db)
    connection = _open_readonly(semantic_db)
    try:
        run_rows = connection.execute(
            "SELECT run_id,status,runner_version,contract_version,prompt_version,"
            "output_schema_sha256,transform_version,model,reasoning,service_tier,"
            "COALESCE(logical_sha256,'') AS logical_sha256 FROM semantic_runs"
        ).fetchall()
        if len(run_rows) != 1:
            raise ValueError(f"semantic DB must contain exactly one run, found {len(run_rows)}")
        run = dict(run_rows[0])
        rows = connection.execute(
            """
            SELECT o.input_rank,o.inference_id,v.status,v.derivative_encoded_sha256,
                   v.derivative_pixel_sha256,v.derivative_width,v.derivative_height,
                   v.derivative_bytes
            FROM selected_occurrences AS o
            JOIN vision_inputs AS v
              ON v.run_id=o.run_id AND v.inference_id=o.inference_id
            WHERE o.run_id=? AND v.derivative_encoded_sha256 IS NOT NULL
            ORDER BY o.input_rank,o.inference_id
            """,
            (run["run_id"],),
        ).fetchall()
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick != "ok":
            raise ValueError(f"semantic DB quick_check failed: {quick}")
    finally:
        connection.close()

    if not rows:
        raise ValueError("semantic DB has no retained Vision derivatives to review")
    expected_names = {f"{row['inference_id']}.jpg" for row in rows}
    actual_jpegs = {path.name for path in review_cache_dir.glob("*.jpg") if path.is_file()}
    if actual_jpegs != expected_names:
        raise ValueError(
            "review-cache JPEG accounting mismatch: "
            f"missing={sorted(expected_names - actual_jpegs)}, "
            f"unexpected={sorted(actual_jpegs - expected_names)}"
        )

    verified: list[ReviewInput] = []
    for ordinal, row in enumerate(rows, 1):
        inference_id = str(row["inference_id"])
        suffix = inference_id.removeprefix(OPAQUE_ID_PREFIX)
        if not inference_id.startswith(OPAQUE_ID_PREFIX) or not suffix.isdigit():
            raise ValueError(f"non-opaque inference ID in semantic DB: {inference_id!r}")
        path = review_cache_dir / f"{inference_id}.jpg"
        encoded_size = path.stat().st_size
        encoded_sha = _sha256_file(path)
        pixel_sha, width, height, _ = _pixel_identity(path)
        expected = {
            "encoded_bytes": int(row["derivative_bytes"]),
            "encoded_sha256": str(row["derivative_encoded_sha256"]),
            "height": int(row["derivative_height"]),
            "pixel_sha256": str(row["derivative_pixel_sha256"]),
            "width": int(row["derivative_width"]),
        }
        actual = {
            "encoded_bytes": encoded_size,
            "encoded_sha256": encoded_sha,
            "height": height,
            "pixel_sha256": pixel_sha,
            "width": width,
        }
        if actual != expected:
            raise ValueError(
                f"review-cache derivative identity mismatch for {inference_id}: "
                f"expected={expected}, actual={actual}"
            )
        verified.append(
            ReviewInput(
                ordinal=ordinal,
                input_rank=int(row["input_rank"]),
                inference_id=inference_id,
                cache_path=path,
                encoded_bytes=encoded_size,
                encoded_sha256=encoded_sha,
                pixel_sha256=pixel_sha,
                width=width,
                height=height,
                vision_status=str(row["status"]),
            )
        )

    db_size_after = semantic_db.stat().st_size
    db_sha_after = _sha256_file(semantic_db)
    if (db_size_before, db_sha_before) != (db_size_after, db_sha_after):
        raise RuntimeError("semantic DB changed during read-only review input validation")
    db_record = {
        "byte_sha256_after": db_sha_after,
        "byte_sha256_before": db_sha_before,
        "filename": semantic_db.name,
        "size_bytes_after": db_size_after,
        "size_bytes_before": db_size_before,
    }
    run_record = {
        "contract_version": run["contract_version"],
        "logical_sha256": run["logical_sha256"] or None,
        "model": run["model"],
        "output_schema_sha256": run["output_schema_sha256"],
        "prompt_version": run["prompt_version"],
        "reasoning": run["reasoning"],
        "run_id": run["run_id"],
        "runner_version": run["runner_version"],
        "service_tier": run["service_tier"],
        "status": run["status"],
        "transform_version": run["transform_version"],
    }
    return db_record, verified, run_record


def _fit_without_upscale(image: Any, width: int, height: int) -> Any:
    from PIL import Image

    scale = min(1.0, width / image.width, height / image.height)
    size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    if size == image.size:
        return image.copy()
    return image.resize(size, Image.Resampling.LANCZOS)


def _render_sheet(path: Path, inputs: Sequence[ReviewInput]) -> None:
    from PIL import Image, ImageDraw, ImageFont

    canvas = Image.new("RGB", (SHEET_WIDTH, SHEET_HEIGHT), (242, 242, 242))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    cell_width = SHEET_WIDTH // SHEET_COLUMNS
    cell_height = SHEET_HEIGHT // SHEET_ROWS
    label_height = 42
    pad = 18
    for index, item in enumerate(inputs):
        column = index % SHEET_COLUMNS
        row = index // SHEET_COLUMNS
        left = column * cell_width
        top = row * cell_height
        draw.rectangle(
            (left + 4, top + 4, left + cell_width - 5, top + cell_height - 5),
            fill=(255, 255, 255),
            outline=(150, 150, 150),
            width=2,
        )
        label = f"#{item.ordinal:03d}  {item.inference_id}"
        draw.text((left + pad, top + 15), label, fill=(10, 10, 10), font=font)
        with Image.open(item.cache_path) as opened:
            opened.load()
            rgb = opened.convert("RGB")
        fitted = _fit_without_upscale(
            rgb,
            cell_width - 2 * pad,
            cell_height - label_height - 2 * pad,
        )
        x = left + (cell_width - fitted.width) // 2
        image_area_top = top + label_height
        y = image_area_top + (cell_height - label_height - fitted.height) // 2
        canvas.paste(fitted, (x, y))
    canvas.save(
        path,
        format="JPEG",
        quality=92,
        subsampling=0,
        optimize=False,
        progressive=False,
    )


def render_review_sheets(
    *,
    semantic_db: Path,
    review_cache_dir: Path,
    output_dir: Path,
    images_per_sheet: int = DEFAULT_IMAGES_PER_SHEET,
) -> dict[str, Any]:
    """Verify inputs and render a new no-clobber blind-review directory."""

    semantic_db = semantic_db.resolve()
    review_cache_dir = review_cache_dir.resolve()
    output_dir = output_dir.resolve()
    if images_per_sheet < 1 or images_per_sheet > SHEET_COLUMNS * SHEET_ROWS:
        raise ValueError(
            f"images_per_sheet must be between 1 and {SHEET_COLUMNS * SHEET_ROWS}"
        )
    if output_dir.exists():
        raise FileExistsError(f"blind-review output directory already exists: {output_dir}")

    db_record, inputs, run_record = _load_verified_inputs(
        semantic_db, review_cache_dir
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=False)

    sheets: list[dict[str, Any]] = []
    for offset in range(0, len(inputs), images_per_sheet):
        batch = inputs[offset : offset + images_per_sheet]
        sheet_no = len(sheets) + 1
        filename = f"sheet_{sheet_no:03d}.jpg"
        path = output_dir / filename
        _render_sheet(path, batch)
        sheets.append(
            {
                "filename": filename,
                "inference_ids": [item.inference_id for item in batch],
                "sheet_no": sheet_no,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )

    body: dict[str, Any] = {
        "blind_labels_only": True,
        "images_per_sheet": images_per_sheet,
        "input_count": len(inputs),
        "inputs": [item.manifest_record() for item in inputs],
        "network_requests": 0,
        "ordered_inference_ids": [item.inference_id for item in inputs],
        "renderer_version": RENDERER_VERSION,
        "semantic_db": db_record,
        "semantic_run": run_record,
        "sheet_count": len(sheets),
        "sheets": sheets,
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
        description="Render local-only blind contact sheets from semantic Vision cache inputs."
    )
    parser.add_argument("--semantic-db", type=Path, required=True)
    parser.add_argument("--review-cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--images-per-sheet", type=int, default=DEFAULT_IMAGES_PER_SHEET
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = render_review_sheets(
        semantic_db=args.semantic_db,
        review_cache_dir=args.review_cache_dir,
        output_dir=args.output_dir,
        images_per_sheet=args.images_per_sheet,
    )
    print(
        json.dumps(
            {
                "input_count": result["input_count"],
                "manifest_sha256": result["manifest_sha256"],
                "network_requests": 0,
                "output_dir": str(args.output_dir.resolve()),
                "sheet_count": result["sheet_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
