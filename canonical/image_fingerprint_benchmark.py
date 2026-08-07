"""Offline calibration benchmark for the shared image fingerprint contract.

The benchmark deliberately has no URL or network support.  It selects unique
cached responses by content SHA-256, creates controlled renditions in memory,
and compares those renditions with deterministically paired unrelated images.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import statistics
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageEnhance, ImageOps

from canonical.image_fingerprint import (
    DEFAULT_MAX_SOURCE_PIXELS,
    FINGERPRINT_CONTRACT_VERSION,
    NORMALIZER_VERSION,
    PHASH_VERSION,
    dependency_versions,
    fingerprint_bytes,
    phash_distance,
)


BENCHMARK_SCHEMA_VERSION = 1
BENCHMARK_VERSION = "image-fingerprint-offline-benchmark-v1.2.0"
DEFAULT_THRESHOLDS = (8, 16)
DEFAULT_MINIMUM_SIDE = 64
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024

TRANSFORM_SPECS: tuple[tuple[str, str, Mapping[str, Any]], ...] = (
    ("png_lossless", "codec_resize", {"format": "PNG", "compress_level": 6}),
    (
        "jpeg_q85",
        "codec_resize",
        {"format": "JPEG", "quality": 85, "subsampling": 0},
    ),
    (
        "jpeg_q60",
        "codec_resize",
        {"format": "JPEG", "quality": 60, "subsampling": 2},
    ),
    (
        "jpeg_q35",
        "codec_resize",
        {"format": "JPEG", "quality": 35, "subsampling": 2},
    ),
    (
        "webp_q75",
        "codec_resize",
        {"format": "WEBP", "quality": 75, "method": 6, "requires": "webp"},
    ),
    ("resize_75pct", "codec_resize", {"scale": 0.75, "format": "PNG"}),
    ("resize_50pct", "codec_resize", {"scale": 0.50, "format": "PNG"}),
    ("resize_200pct", "codec_resize", {"scale": 2.00, "format": "PNG"}),
    ("brightness_90pct", "photometric", {"factor": 0.90, "format": "PNG"}),
    ("center_crop_1pct", "crop", {"crop_each_edge": 0.01, "format": "PNG"}),
    ("center_crop_3pct", "crop", {"crop_each_edge": 0.03, "format": "PNG"}),
    ("center_crop_5pct", "crop", {"crop_each_edge": 0.05, "format": "PNG"}),
)


class BenchmarkError(ValueError):
    """Raised when an offline benchmark cannot produce a valid sample."""


@dataclass(frozen=True)
class CachedImage:
    relative_path: str
    raw_sha256: str
    raw: bytes
    width: int
    height: int


@dataclass(frozen=True)
class CacheSelection:
    samples: tuple[CachedImage, ...]
    candidate_files: int
    unique_contents: int
    eligible_contents: int
    skipped: Mapping[str, int]


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_rgb(
    raw: bytes,
    *,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
) -> Image.Image:
    """Decode frame zero, apply EXIF orientation, and composite alpha on white."""

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > max_source_pixels:
                raise BenchmarkError("decoded image exceeds the source-pixel limit")
            if getattr(opened, "n_frames", 1) > 1:
                opened.seek(0)
            icc_value = opened.info.get("icc_profile")
            icc_profile = (
                bytes(icc_value)
                if isinstance(icc_value, (bytes, bytearray))
                else None
            )
            had_alpha = "A" in opened.getbands() or "transparency" in opened.info
            opened.load()
            oriented = ImageOps.exif_transpose(opened.copy())
        if icc_profile:
            try:
                from PIL import ImageCms

                source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                target_profile = ImageCms.createProfile("sRGB")
                if had_alpha:
                    rgba = oriented.convert("RGBA")
                    alpha = rgba.getchannel("A")
                    oriented = ImageCms.profileToProfile(
                        rgba.convert("RGB"),
                        source_profile,
                        target_profile,
                        outputMode="RGB",
                    )
                    oriented.putalpha(alpha)
                else:
                    oriented = ImageCms.profileToProfile(
                        oriented,
                        source_profile,
                        target_profile,
                        outputMode="RGB",
                    )
            except (ImageCms.PyCMSError, OSError, TypeError, ValueError):
                pass
        if had_alpha:
            rgba = oriented.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            return Image.alpha_composite(white, rgba).convert("RGB")
        return oriented.convert("RGB")


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def select_cached_images(
    cache_dir: Path | str,
    *,
    limit: int,
    minimum_side: int = DEFAULT_MINIMUM_SIDE,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
) -> CacheSelection:
    """Select a deterministic, content-deduplicated cache sample.

    Selection is the first ``limit`` eligible contents after sorting by the
    SHA-256 of the exact cached response.  Paths and filename extensions do not
    influence membership.
    """

    root = Path(cache_dir)
    if limit < 2:
        raise BenchmarkError("limit must be at least 2 so negative pairs exist")
    if minimum_side < 16:
        raise BenchmarkError("minimum_side must be at least 16 pixels")
    if max_file_bytes < 1:
        raise BenchmarkError("max_file_bytes must be positive")
    if max_source_pixels < 1:
        raise BenchmarkError("max_source_pixels must be positive")
    if not root.is_dir():
        raise BenchmarkError(f"cache directory does not exist: {root}")

    skipped: dict[str, int] = {
        "symlink": 0,
        "too_large": 0,
        "read_error": 0,
        "duplicate_content": 0,
        "decode_error": 0,
        "too_small": 0,
    }
    candidates = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: _relative_path(root, path),
    )
    unique: dict[str, tuple[str, Path]] = {}
    for path in candidates:
        if path.is_symlink():
            skipped["symlink"] += 1
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                skipped["too_large"] += 1
                continue
            raw = path.read_bytes()
            if len(raw) > max_file_bytes:
                skipped["too_large"] += 1
                continue
        except OSError:
            skipped["read_error"] += 1
            continue
        digest = _sha256(raw)
        relative = _relative_path(root, path)
        if digest in unique:
            skipped["duplicate_content"] += 1
            if relative < unique[digest][0]:
                unique[digest] = (relative, path)
            continue
        unique[digest] = (relative, path)

    selected: list[CachedImage] = []
    eligible_count = 0
    for digest in sorted(unique):
        relative, path = unique[digest]
        try:
            raw = path.read_bytes()
            if len(raw) > max_file_bytes:
                skipped["too_large"] += 1
                continue
            image = _load_rgb(raw, max_source_pixels=max_source_pixels)
        except OSError:
            skipped["read_error"] += 1
            continue
        except Exception:  # Pillow exposes several format-specific exceptions.
            skipped["decode_error"] += 1
            continue
        width, height = image.size
        image.close()
        if min(width, height) < minimum_side:
            skipped["too_small"] += 1
            continue
        eligible_count += 1
        if len(selected) < limit:
            selected.append(
                CachedImage(
                    relative_path=relative,
                    raw_sha256=digest,
                    raw=raw,
                    width=width,
                    height=height,
                )
            )

    if eligible_count < limit:
        raise BenchmarkError(
            f"requested {limit} images but only {eligible_count} eligible unique "
            "contents were found"
        )
    return CacheSelection(
        samples=tuple(selected),
        candidate_files=len(candidates),
        unique_contents=len(unique),
        eligible_contents=eligible_count,
        skipped=skipped,
    )


def _encode(image: Image.Image, *, format_name: str, **kwargs: Any) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format_name, **kwargs)
    return output.getvalue()


def available_transform_specs() -> tuple[tuple[str, str, Mapping[str, Any]], ...]:
    """Return the stable transform set supported by the active Pillow build."""

    from PIL import features

    webp_supported = bool(features.check("webp"))
    return tuple(
        (name, family, parameters)
        for name, family, parameters in TRANSFORM_SPECS
        if parameters.get("requires") != "webp" or webp_supported
    )


def generate_controlled_variants(raw: bytes) -> dict[str, bytes]:
    """Generate deterministic, in-memory variants of one decoded image."""

    image = _load_rgb(raw)
    width, height = image.size
    if min(width, height) < 16:
        image.close()
        raise BenchmarkError("image is too small for controlled crop/resize variants")
    try:
        variants: dict[str, bytes] = {}
        variants["png_lossless"] = _encode(
            image, format_name="PNG", compress_level=6
        )
        variants["jpeg_q85"] = _encode(
            image, format_name="JPEG", quality=85, subsampling=0
        )
        variants["jpeg_q60"] = _encode(
            image, format_name="JPEG", quality=60, subsampling=2
        )
        variants["jpeg_q35"] = _encode(
            image, format_name="JPEG", quality=35, subsampling=2
        )
        if any(name == "webp_q75" for name, _, _ in available_transform_specs()):
            variants["webp_q75"] = _encode(
                image, format_name="WEBP", quality=75, method=6
            )

        for name, scale in (
            ("resize_75pct", 0.75),
            ("resize_50pct", 0.50),
            ("resize_200pct", 2.00),
        ):
            resized = image.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.Resampling.LANCZOS,
            )
            variants[name] = _encode(resized, format_name="PNG")
            resized.close()

        darker = ImageEnhance.Brightness(image).enhance(0.90)
        variants["brightness_90pct"] = _encode(darker, format_name="PNG")
        darker.close()

        for name, fraction in (
            ("center_crop_1pct", 0.01),
            ("center_crop_3pct", 0.03),
            ("center_crop_5pct", 0.05),
        ):
            crop_x = max(1, round(width * fraction))
            crop_y = max(1, round(height * fraction))
            cropped = image.crop(
                (crop_x, crop_y, width - crop_x, height - crop_y)
            )
            cropped = cropped.resize((width, height), Image.Resampling.LANCZOS)
            variants[name] = _encode(cropped, format_name="PNG")
            cropped.close()
        return variants
    finally:
        image.close()


def _quantile(sorted_values: Sequence[int], fraction: float) -> float:
    if not sorted_values:
        raise BenchmarkError("cannot summarize an empty distance set")
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def summarize_distances(values: Iterable[int]) -> dict[str, int | float]:
    ordered = sorted(int(value) for value in values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p05": round(_quantile(ordered, 0.05), 3),
        "p25": round(_quantile(ordered, 0.25), 3),
        "median": round(_quantile(ordered, 0.50), 3),
        "p75": round(_quantile(ordered, 0.75), 3),
        "p95": round(_quantile(ordered, 0.95), 3),
        "max": ordered[-1],
        "mean": round(statistics.fmean(ordered), 3),
    }


def _validate_thresholds(thresholds: Iterable[int]) -> tuple[int, ...]:
    values = tuple(sorted({int(value) for value in thresholds}))
    if not values:
        raise BenchmarkError("at least one pHash threshold is required")
    if values[0] < 0 or values[-1] > 256:
        raise BenchmarkError("pHash thresholds must be between 0 and 256")
    return values


def _assumed_unrelated_pairs(
    sample_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair entries by a half-list rotation, avoiding exact normalized pixels."""

    count = len(sample_rows)
    shift = max(1, count // 2)
    while count > 2 and math.gcd(shift, count) != 1:
        shift += 1
        if shift >= count:
            shift = 1
    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(sample_rows):
        right: dict[str, Any] | None = None
        for offset in range(count):
            candidate = sample_rows[(left_index + shift + offset) % count]
            if candidate["raw_sha256"] == left["raw_sha256"]:
                continue
            if candidate["pixel_sha256"] == left["pixel_sha256"]:
                continue
            right = candidate
            break
        if right is None:
            continue
        pairs.append(
            {
                "left_raw_sha256": left["raw_sha256"],
                "right_raw_sha256": right["raw_sha256"],
                "distance": phash_distance(left["phash256"], right["phash256"]),
                "label": "assumed_unrelated",
            }
        )
    if not pairs:
        raise BenchmarkError("sample has fewer than two distinct normalized images")
    return pairs


def _threshold_metrics(
    positive_rows: Sequence[dict[str, Any]],
    negative_rows: Sequence[dict[str, Any]],
    thresholds: Sequence[int],
) -> dict[str, dict[str, int | float]]:
    output: dict[str, dict[str, int | float]] = {}
    for threshold in thresholds:
        true_positive = sum(row["distance"] <= threshold for row in positive_rows)
        false_positive = sum(row["distance"] <= threshold for row in negative_rows)
        false_negative = len(positive_rows) - true_positive
        true_negative = len(negative_rows) - false_positive
        predicted_positive = true_positive + false_positive
        negative_count = len(negative_rows)
        rate = false_positive / negative_count
        z = 1.959963984540054
        denominator = 1.0 + z * z / negative_count
        center = (rate + z * z / (2 * negative_count)) / denominator
        half_width = (
            z
            * math.sqrt(
                rate * (1 - rate) / negative_count
                + z * z / (4 * negative_count * negative_count)
            )
            / denominator
        )
        output[str(threshold)] = {
            "true_positive": true_positive,
            "false_negative": false_negative,
            "false_positive": false_positive,
            "true_negative": true_negative,
            "recall": round(true_positive / len(positive_rows), 6),
            "false_positive_rate": round(
                rate, 6
            ),
            "false_positive_rate_wilson95": [
                round(max(0.0, center - half_width), 6),
                round(min(1.0, center + half_width), 6),
            ],
            "sample_precision": round(
                true_positive / predicted_positive, 6
            )
            if predicted_positive
            else 0.0,
        }
    return output


def run_benchmark(
    cache_dir: Path | str,
    *,
    sample_size: int = 100,
    thresholds: Iterable[int] = DEFAULT_THRESHOLDS,
    minimum_side: int = DEFAULT_MINIMUM_SIDE,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
    include_pairs: bool = True,
) -> dict[str, Any]:
    """Run the deterministic offline benchmark and return JSON-ready data."""

    threshold_values = _validate_thresholds(thresholds)
    selection = select_cached_images(
        cache_dir,
        limit=sample_size,
        minimum_side=minimum_side,
        max_file_bytes=max_file_bytes,
        max_source_pixels=max_source_pixels,
    )

    sample_rows: list[dict[str, Any]] = []
    positive_rows: list[dict[str, Any]] = []
    active_specs = available_transform_specs()
    transform_family = {name: family for name, family, _ in active_specs}
    transform_distances: dict[str, list[int]] = {
        name: [] for name, _, _ in active_specs
    }
    transform_pixel_equal: dict[str, int] = {
        name: 0 for name, _, _ in active_specs
    }

    for sample in selection.samples:
        baseline = fingerprint_bytes(sample.raw)
        sample_rows.append(
            {
                "raw_sha256": sample.raw_sha256,
                "relative_path": sample.relative_path,
                "source_width": sample.width,
                "source_height": sample.height,
                "pixel_sha256": baseline.pixel_sha256,
                "phash256": baseline.phash256,
            }
        )
        variants = generate_controlled_variants(sample.raw)
        if tuple(variants) != tuple(name for name, _, _ in active_specs):
            raise BenchmarkError("controlled transform set changed unexpectedly")
        for transform_name, variant_raw in variants.items():
            fingerprint = fingerprint_bytes(variant_raw)
            distance = phash_distance(baseline.phash256, fingerprint.phash256)
            pixel_equal = baseline.pixel_sha256 == fingerprint.pixel_sha256
            transform_distances[transform_name].append(distance)
            transform_pixel_equal[transform_name] += int(pixel_equal)
            positive_rows.append(
                {
                    "base_raw_sha256": sample.raw_sha256,
                    "transform": transform_name,
                    "family": transform_family[transform_name],
                    "distance": distance,
                    "pixel_sha256_equal": pixel_equal,
                    "label": "same_source_controlled_transform",
                }
            )

    negative_rows = _assumed_unrelated_pairs(sample_rows)
    ordered_manifest = "".join(
        f"{row['raw_sha256']}\n" for row in sample_rows
    ).encode("ascii")
    summary_by_transform = {
        name: {
            "distance": summarize_distances(transform_distances[name]),
            "pixel_sha256_equal": transform_pixel_equal[name],
            "pixel_sha256_equal_rate": round(
                transform_pixel_equal[name] / sample_size, 6
            ),
            "threshold_recall": {
                str(threshold): round(
                    sum(
                        distance <= threshold
                        for distance in transform_distances[name]
                    )
                    / sample_size,
                    6,
                )
                for threshold in threshold_values
            },
        }
        for name, _, _ in active_specs
    }
    family_names = tuple(dict.fromkeys(family for _, family, _ in active_specs))
    summary_by_family = {}
    for family in family_names:
        distances = [
            row["distance"] for row in positive_rows if row["family"] == family
        ]
        summary_by_family[family] = {
            "pair_count": len(distances),
            "distance": summarize_distances(distances),
            "threshold_recall": {
                str(threshold): round(
                    sum(distance <= threshold for distance in distances)
                    / len(distances),
                    6,
                )
                for threshold in threshold_values
            },
        }
    result: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "fingerprint_contract": {
            "fingerprint_contract_version": FINGERPRINT_CONTRACT_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "phash_version": PHASH_VERSION,
            "dependencies": dict(dependency_versions()),
        },
        "selection": {
            "method": "unique_response_sha256_ascending",
            "requested": sample_size,
            "selected": len(selection.samples),
            "candidate_files": selection.candidate_files,
            "unique_contents": selection.unique_contents,
            "eligible_contents": selection.eligible_contents,
            "minimum_side": minimum_side,
            "max_file_bytes": max_file_bytes,
            "max_source_pixels": max_source_pixels,
            "skipped": dict(selection.skipped),
            "ordered_content_manifest_framing": "lowercase_sha256_plus_lf",
            "ordered_content_manifest_bytes": len(ordered_manifest),
            "ordered_content_manifest_sha256": _sha256(ordered_manifest),
        },
        "samples": sample_rows,
        "transform_specs": [
            {
                "name": name,
                "family": family,
                **{
                    key: value
                    for key, value in parameters.items()
                    if key != "requires"
                },
            }
            for name, family, parameters in active_specs
        ],
        "summary": {
            "positive_pair_count": len(positive_rows),
            "assumed_unrelated_pair_count": len(negative_rows),
            "positive_distance": summarize_distances(
                row["distance"] for row in positive_rows
            ),
            "assumed_unrelated_distance": summarize_distances(
                row["distance"] for row in negative_rows
            ),
            "by_transform": summary_by_transform,
            "by_family": summary_by_family,
            "thresholds": _threshold_metrics(
                positive_rows, negative_rows, threshold_values
            ),
            "overall_recall_caveat": (
                "Overall recall weights every synthetic transform equally; "
                "use by_family and by_transform when choosing candidate "
                "thresholds."
            ),
            "negative_label_caveat": (
                "Negative pairs are deterministic cross-image pairs without "
                "human duplicate adjudication; their error rate is an "
                "estimated false-positive rate, not a labeled-corpus claim."
            ),
            "sample_precision_caveat": (
                "sample_precision reflects this benchmark's synthetic class "
                "balance and is not an estimate of production precision."
            ),
        },
    }
    if include_pairs:
        result["pairs"] = {
            "positive": positive_rows,
            "assumed_unrelated": negative_rows,
        }
    return result


def write_json_report(result: Mapping[str, Any], output_path: Path | str) -> None:
    """Create a stable UTF-8 JSON report without replacing an existing file."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        result,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload + "\n")
