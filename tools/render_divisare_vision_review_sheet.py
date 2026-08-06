#!/usr/bin/env python3
"""Render a blinded Divisare Vision review page without persisting source images."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_image_smoke import FetchPayload, network_fetch  # noqa: E402
from canonical.divisare_vision_gold import (  # noqa: E402
    REVIEW_PROFILE,
    fixed_derivative_url,
    validate_candidate_manifest,
)
from canonical.divisare_vision_gold_finalize import HASH_EVIDENCE_FIELDS  # noqa: E402
from canonical.divisare_vision_probe import (  # noqa: E402
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_BYTES,
    DEFAULT_READ_TIMEOUT,
    validate_enriched_manifest,
)


EXPECTED_CANDIDATE_COUNT = 560
MAX_PAGE_SIZE = 25
DEFAULT_PAGE_SIZE = 25
DEFAULT_COLUMNS = 5
FETCH_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)
MAX_FETCH_WORKERS = 5

THUMB_WIDTH = 256
THUMB_HEIGHT = 192
CELL_PADDING = 8
LABEL_GAP = 6
LABEL_HEIGHT = 18
CELL_WIDTH = THUMB_WIDTH + (2 * CELL_PADDING)
CELL_HEIGHT = CELL_PADDING + THUMB_HEIGHT + LABEL_GAP + LABEL_HEIGHT + CELL_PADDING
SHEET_MARGIN = 12
CELL_GAP = 10

Fetcher = Callable[..., FetchPayload | bytes]


@dataclass(frozen=True)
class ReviewItem:
    blinded_index: int
    candidate_id: str
    review_url: str
    probe_status: str


@dataclass(frozen=True)
class RenderedSheet:
    png_bytes: bytes
    id_mapping: tuple[dict[str, Any], ...]
    unavailable_candidate_ids: tuple[str, ...]


def _require_positive_int(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("%s must be a positive integer" % field)
    return value


def _reconstruct_probe_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    probe_contract = payload.get("probe_contract")
    if not isinstance(probe_contract, Mapping):
        raise ValueError("enriched manifest is missing probe_contract")
    input_sha = probe_contract.get("input_manifest_sha256")
    input_file_sha = probe_contract.get("input_manifest_file_sha256")
    for name, value in (
        ("input_manifest_sha256", input_sha),
        ("input_manifest_file_sha256", input_file_sha),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("probe_contract.%s must be 64 lowercase hex characters" % name)

    reconstructed = copy.deepcopy(dict(payload))
    reconstructed.pop("manifest_sha256", None)
    for field in (
        "probe_contract",
        "exact_pixel_duplicate_groups",
        "phash_duplicate_pairs_le_8",
        "phash_audit_pairs_9_16",
        "probe_attempts",
    ):
        reconstructed.pop(field, None)
    candidates = reconstructed.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("enriched manifest candidates must be a list")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("every enriched candidate must be an object")
        for field in HASH_EVIDENCE_FIELDS:
            candidate.pop(field, None)
    reconstructed["manifest_sha256"] = input_sha
    validate_candidate_manifest(reconstructed)
    return reconstructed


def validate_review_manifest(payload: Mapping[str, Any]) -> None:
    """Validate selection SHA plus the complete probe/accounting evidence."""

    validate_candidate_manifest(payload)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != EXPECTED_CANDIDATE_COUNT:
        raise ValueError("review input must contain exactly 560 candidates")

    reconstructed = _reconstruct_probe_input(payload)
    probe_contract = payload["probe_contract"]
    validate_enriched_manifest(
        payload,
        input_manifest=reconstructed,
        input_manifest_file_sha256=probe_contract["input_manifest_file_sha256"],
    )

    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        source_url = candidate.get("source_url")
        review_url = candidate.get("review_url")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("every review candidate requires a string candidate_id")
        if not isinstance(source_url, str) or not isinstance(review_url, str):
            raise ValueError("candidate URLs must be strings: %s" % candidate_id)
        if review_url != fixed_derivative_url(source_url, REVIEW_PROFILE):
            raise ValueError("candidate review URL is not the frozen 1024 derivative: %s" % candidate_id)


def load_review_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("review manifest must be UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("review manifest must be a JSON object")
    validate_review_manifest(payload)
    return payload


def blinded_order(
    candidates: Sequence[Mapping[str, Any]], manifest_sha256: str
) -> list[ReviewItem]:
    """Return discovery-order-independent IDs keyed by SHA(manifest SHA + candidate ID)."""

    if (
        not isinstance(manifest_sha256, str)
        or len(manifest_sha256) != 64
        or any(char not in "0123456789abcdef" for char in manifest_sha256)
    ):
        raise ValueError("manifest_sha256 must be 64 lowercase hex characters")
    keyed: list[tuple[bytes, str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("every candidate requires a string candidate_id")
        if candidate_id in seen:
            raise ValueError("candidate IDs must be unique")
        seen.add(candidate_id)
        key = hashlib.sha256((manifest_sha256 + candidate_id).encode("utf-8")).digest()
        keyed.append((key, candidate_id, candidate))
    keyed.sort(key=lambda row: (row[0], row[1]))
    return [
        ReviewItem(
            blinded_index=index,
            candidate_id=candidate_id,
            review_url=str(candidate["review_url"]),
            probe_status=str(candidate.get("probe_status") or ""),
        )
        for index, (_, candidate_id, candidate) in enumerate(keyed, 1)
    ]


def page_items(
    items: Sequence[ReviewItem], *, page: int, page_size: int
) -> list[ReviewItem]:
    page = _require_positive_int(page, "page")
    page_size = _require_positive_int(page_size, "page_size")
    if page_size > MAX_PAGE_SIZE:
        raise ValueError("page_size cannot exceed %d" % MAX_PAGE_SIZE)
    if not items:
        raise ValueError("review input has no candidates")
    page_count = math.ceil(len(items) / page_size)
    if page > page_count:
        raise ValueError("page %d exceeds the %d-page review set" % (page, page_count))
    start = (page - 1) * page_size
    return list(items[start : start + page_size])


def _decode_rgb_white(raw: bytes):
    from PIL import Image, ImageOps

    if not isinstance(raw, bytes) or not raw:
        raise ValueError("image response is empty")
    with Image.open(io.BytesIO(raw)) as opened:
        opened.seek(0)
        oriented = ImageOps.exif_transpose(opened)
        oriented.load()
        if "A" in oriented.getbands() or "transparency" in oriented.info:
            rgba = oriented.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            white.alpha_composite(rgba)
            return white.convert("RGB")
        return oriented.convert("RGB")


def contain_no_upscale(image, size: tuple[int, int]):
    """Return an RGB thumbnail contained in size, never enlarging the input."""

    from PIL import Image

    max_width, max_height = size
    _require_positive_int(max_width, "thumbnail width")
    _require_positive_int(max_height, "thumbnail height")
    if image.mode != "RGB":
        raise ValueError("thumbnail input must be RGB")
    width, height = image.size
    if width < 1 or height < 1:
        raise ValueError("thumbnail input dimensions must be positive")
    scale = min(1.0, max_width / width, max_height / height)
    target = (max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))
    if target == image.size:
        return image.copy()
    return image.resize(target, Image.Resampling.LANCZOS)


def _fetch_bytes(url: str, fetcher: Fetcher) -> bytes:
    result = fetcher(url, timeout=FETCH_TIMEOUT, max_bytes=DEFAULT_MAX_BYTES)
    if isinstance(result, bytes):
        raw = result
    else:
        status = int(result.http_status)
        if not 200 <= status <= 299:
            raise ValueError("review fetch returned non-2xx status")
        final = urlsplit(result.final_url)
        if final.scheme.casefold() != "https" or final.hostname != "images.divisare.com":
            raise ValueError("review fetch redirected outside images.divisare.com")
        raw = result.raw
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("review fetch returned no bytes")
    if len(raw) > DEFAULT_MAX_BYTES:
        raise ValueError("review fetch exceeded the byte limit")
    return raw


def _draw_centered(draw, box: tuple[int, int, int, int], text: str, *, fill, font) -> None:
    left, top, right, bottom = box
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(
        (left + ((right - left - width) // 2), top + ((bottom - top - height) // 2)),
        text,
        fill=fill,
        font=font,
    )


def render_sheet(
    items: Sequence[ReviewItem],
    *,
    columns: int,
    fetcher: Fetcher | None = None,
) -> RenderedSheet:
    """Fetch one page in memory and return deterministic PNG/mapping bytes."""

    from PIL import Image, ImageDraw, ImageFont

    columns = _require_positive_int(columns, "columns")
    if columns > MAX_PAGE_SIZE:
        raise ValueError("columns cannot exceed %d" % MAX_PAGE_SIZE)
    if not items:
        raise ValueError("cannot render an empty review page")
    fetch = network_fetch if fetcher is None else fetcher
    rows = math.ceil(len(items) / columns)
    sheet_width = (2 * SHEET_MARGIN) + (columns * CELL_WIDTH) + ((columns - 1) * CELL_GAP)
    sheet_height = (2 * SHEET_MARGIN) + (rows * CELL_HEIGHT) + ((rows - 1) * CELL_GAP)
    sheet = Image.new("RGB", (sheet_width, sheet_height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    unavailable: list[str] = []

    def load_thumbnail(item: ReviewItem):
        if item.probe_status != "success":
            return None
        try:
            return contain_no_upscale(
                _decode_rgb_white(_fetch_bytes(item.review_url, fetch)),
                (THUMB_WIDTH, THUMB_HEIGHT),
            )
        except Exception:
            return None

    with ThreadPoolExecutor(
        max_workers=min(MAX_FETCH_WORKERS, len(items)),
        thread_name_prefix="divisare-review-sheet",
    ) as executor:
        thumbnails = list(executor.map(load_thumbnail, items))

    for offset, (item, image) in enumerate(zip(items, thumbnails)):
        row, column = divmod(offset, columns)
        cell_x = SHEET_MARGIN + (column * (CELL_WIDTH + CELL_GAP))
        cell_y = SHEET_MARGIN + (row * (CELL_HEIGHT + CELL_GAP))
        draw.rectangle(
            (cell_x, cell_y, cell_x + CELL_WIDTH - 1, cell_y + CELL_HEIGHT - 1),
            outline=(184, 184, 184),
            width=1,
        )
        thumb_x = cell_x + CELL_PADDING
        thumb_y = cell_y + CELL_PADDING
        thumb_box = (
            thumb_x,
            thumb_y,
            thumb_x + THUMB_WIDTH,
            thumb_y + THUMB_HEIGHT,
        )
        draw.rectangle(
            (thumb_box[0], thumb_box[1], thumb_box[2] - 1, thumb_box[3] - 1),
            fill=(246, 246, 246),
        )

        if image is None:
            unavailable.append(item.candidate_id)
            inset = 18
            draw.line(
                (thumb_box[0] + inset, thumb_box[1] + inset, thumb_box[2] - inset, thumb_box[3] - inset),
                fill=(145, 145, 145),
                width=2,
            )
            draw.line(
                (thumb_box[2] - inset, thumb_box[1] + inset, thumb_box[0] + inset, thumb_box[3] - inset),
                fill=(145, 145, 145),
                width=2,
            )
            _draw_centered(draw, thumb_box, "UNAVAILABLE", fill=(55, 55, 55), font=font)
        else:
            paste_x = thumb_x + ((THUMB_WIDTH - image.width) // 2)
            paste_y = thumb_y + ((THUMB_HEIGHT - image.height) // 2)
            sheet.paste(image, (paste_x, paste_y))

        label_y = thumb_y + THUMB_HEIGHT + LABEL_GAP
        draw.text(
            (thumb_x, label_y),
            "%03d  %s" % (item.blinded_index, item.candidate_id),
            fill=(20, 20, 20),
            font=font,
        )

    encoded = io.BytesIO()
    sheet.save(encoded, format="PNG", optimize=False, compress_level=9)
    mapping = tuple(
        {"blinded_index": item.blinded_index, "candidate_id": item.candidate_id}
        for item in items
    )
    return RenderedSheet(
        png_bytes=encoded.getvalue(),
        id_mapping=mapping,
        unavailable_candidate_ids=tuple(unavailable),
    )


def mapping_json_bytes(mapping: Sequence[Mapping[str, Any]]) -> bytes:
    clean: list[dict[str, Any]] = []
    for row in mapping:
        if set(row) != {"blinded_index", "candidate_id"}:
            raise ValueError("mapping rows may contain only blinded_index and candidate_id")
        clean.append(
            {
                "blinded_index": _require_positive_int(row["blinded_index"], "blinded_index"),
                "candidate_id": str(row["candidate_id"]),
            }
        )
    return (json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
    except FileExistsError as exc:
        raise FileExistsError("review output already exists: %s" % path) from exc


def write_review_sheet(
    *,
    manifest_path: Path,
    output_path: Path,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    columns: int = DEFAULT_COLUMNS,
    mapping_output_path: Path | None = None,
    fetcher: Fetcher | None = None,
) -> RenderedSheet:
    if output_path.suffix.casefold() != ".png":
        raise ValueError("output must use the .png suffix")
    if mapping_output_path is not None and mapping_output_path.suffix.casefold() != ".json":
        raise ValueError("mapping output must use the .json suffix")
    output_resolved = output_path.resolve()
    mapping_resolved = mapping_output_path.resolve() if mapping_output_path is not None else None
    if mapping_resolved == output_resolved:
        raise ValueError("PNG and mapping outputs must be different paths")
    for path in (output_path, mapping_output_path):
        if path is not None and path.exists():
            raise FileExistsError("review output already exists: %s" % path)

    payload = load_review_manifest(manifest_path)
    ordered = blinded_order(payload["candidates"], payload["manifest_sha256"])
    selected = page_items(ordered, page=page, page_size=page_size)
    rendered = render_sheet(selected, columns=columns, fetcher=fetcher)

    _write_new(output_path, rendered.png_bytes)
    if mapping_output_path is not None:
        _write_new(mapping_output_path, mapping_json_bytes(rendered.id_mapping))
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--columns", type=int, default=DEFAULT_COLUMNS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mapping-output", type=Path)
    args = parser.parse_args(argv)

    rendered = write_review_sheet(
        manifest_path=args.manifest,
        output_path=args.output,
        page=args.page,
        page_size=args.page_size,
        columns=args.columns,
        mapping_output_path=args.mapping_output,
    )
    print(
        json.dumps(
            {
                "rendered_count": len(rendered.id_mapping),
                "unavailable_count": len(rendered.unavailable_candidate_ids),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
