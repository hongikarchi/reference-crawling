from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, features

from canonical.image_fingerprint import fingerprint_bytes
from canonical.image_fingerprint_benchmark import (
    BenchmarkError,
    generate_controlled_variants,
    run_benchmark,
    select_cached_images,
    write_json_report,
)


def _pattern(seed: int, *, size: tuple[int, int] = (128, 96)) -> bytes:
    image = Image.new("RGB", size, (18 + seed * 17, 31 + seed * 11, 47))
    draw = ImageDraw.Draw(image)
    for index in range(7):
        x0 = (seed * 13 + index * 19) % size[0]
        y0 = (seed * 23 + index * 11) % size[1]
        draw.rectangle(
            (x0, y0, min(size[0] - 1, x0 + 8 + index), min(size[1] - 1, y0 + 13)),
            fill=((seed * 41 + index * 29) % 256, 220 - index * 17, 61 + index * 19),
        )
    draw.line((0, seed * 7 % size[1], size[0] - 1, size[1] - 1), fill="white", width=3)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92, subsampling=0)
    return output.getvalue()


def _cache(tmp_path: Path, count: int = 4) -> Path:
    root = tmp_path / "cache"
    for index in range(count):
        raw = _pattern(index + 1)
        path = root / f"group-{index % 2}" / f"image-{index}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return root


def test_cache_selection_is_sha_sorted_and_content_deduplicated(tmp_path: Path) -> None:
    root = _cache(tmp_path, 3)
    duplicate = root / "duplicate" / "copy.bin"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes((root / "group-0" / "image-0.jpg").read_bytes())

    selection = select_cached_images(root, limit=3, minimum_side=32)

    digests = [sample.raw_sha256 for sample in selection.samples]
    assert digests == sorted(digests)
    assert len(set(digests)) == 3
    assert selection.candidate_files == 4
    assert selection.unique_contents == 3
    assert selection.eligible_contents == 3
    assert selection.skipped["duplicate_content"] == 1
    for sample in selection.samples:
        assert sample.raw_sha256 == hashlib.sha256(sample.raw).hexdigest()


def test_controlled_variants_cover_required_transform_families() -> None:
    raw = _pattern(3)
    baseline = fingerprint_bytes(raw)

    variants = generate_controlled_variants(raw)

    expected = [
        "png_lossless",
        "jpeg_q85",
        "jpeg_q60",
        "jpeg_q35",
    ]
    if features.check("webp"):
        expected.append("webp_q75")
    expected.extend(
        [
            "resize_75pct",
            "resize_50pct",
            "resize_200pct",
            "brightness_90pct",
            "center_crop_1pct",
            "center_crop_3pct",
            "center_crop_5pct",
        ]
    )
    assert tuple(variants) == tuple(expected)
    assert fingerprint_bytes(variants["png_lossless"]).pixel_sha256 == baseline.pixel_sha256
    assert all(payload != raw for payload in variants.values())


def test_benchmark_is_deterministic_and_reports_threshold_metrics(tmp_path: Path) -> None:
    root = _cache(tmp_path, 5)

    first = run_benchmark(
        root,
        sample_size=4,
        minimum_side=32,
        thresholds=(16, 8, 16),
    )
    second = run_benchmark(
        root,
        sample_size=4,
        minimum_side=32,
        thresholds=(8, 16),
    )

    assert first == second
    assert first["selection"]["selected"] == 4
    assert first["selection"]["ordered_content_manifest_bytes"] == 4 * 65
    assert len(first["selection"]["ordered_content_manifest_sha256"]) == 64
    transform_count = 12 if features.check("webp") else 11
    assert first["summary"]["positive_pair_count"] == 4 * transform_count
    assert first["summary"]["assumed_unrelated_pair_count"] == 4
    assert set(first["summary"]["thresholds"]) == {"8", "16"}
    for metrics in first["summary"]["thresholds"].values():
        assert 0.0 <= metrics["recall"] <= 1.0
        assert 0.0 <= metrics["false_positive_rate"] <= 1.0
        assert (
            metrics["true_positive"] + metrics["false_negative"]
            == 4 * transform_count
        )
        assert metrics["false_positive"] + metrics["true_negative"] == 4
    assert set(
        first["summary"]["by_transform"]["jpeg_q85"]["threshold_recall"]
    ) == {"8", "16"}
    assert set(first["summary"]["by_family"]) == {
        "codec_resize",
        "photometric",
        "crop",
    }
    assert sum(
        family["pair_count"]
        for family in first["summary"]["by_family"].values()
    ) == 4 * transform_count
    assert all(
        row["label"] == "assumed_unrelated"
        for row in first["pairs"]["assumed_unrelated"]
    )
    json.dumps(first, sort_keys=True)


def test_summary_only_and_stable_json_writer(tmp_path: Path) -> None:
    root = _cache(tmp_path, 3)
    result = run_benchmark(
        root,
        sample_size=3,
        minimum_side=32,
        include_pairs=False,
    )
    output = tmp_path / "result" / "benchmark.json"

    write_json_report(result, output)

    assert "pairs" not in result
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert output.read_bytes().endswith(b"\n")

    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_json_report({"unexpected": "replacement"}, output)
    assert output.read_bytes() == before


def test_selection_requires_enough_eligible_unique_images(tmp_path: Path) -> None:
    root = _cache(tmp_path, 1)

    with pytest.raises(BenchmarkError, match="only 1 eligible"):
        select_cached_images(root, limit=2, minimum_side=32)


def test_selection_enforces_decoded_source_pixel_limit(tmp_path: Path) -> None:
    root = _cache(tmp_path, 2)

    with pytest.raises(BenchmarkError, match="only 0 eligible"):
        select_cached_images(
            root,
            limit=2,
            minimum_side=32,
            max_source_pixels=100,
        )
