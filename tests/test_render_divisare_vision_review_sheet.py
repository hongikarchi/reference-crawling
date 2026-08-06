from __future__ import annotations

import copy
import hashlib
import io
import json

import pytest
from PIL import Image

from canonical.divisare_image_smoke import FetchPayload
from canonical.divisare_vision_gold import REVIEW_PROFILE, manifest_sha256
from tests.test_divisare_vision_gold_finalize import _enriched_manifest
from tools.render_divisare_vision_review_sheet import (
    CELL_HEIGHT,
    CELL_WIDTH,
    CELL_GAP,
    SHEET_MARGIN,
    ReviewItem,
    _decode_rgb_white,
    blinded_order,
    contain_no_upscale,
    load_review_manifest,
    mapping_json_bytes,
    page_items,
    render_sheet,
    validate_review_manifest,
    write_review_sheet,
)


def _png_bytes(
    size: tuple[int, int] = (80, 40),
    color=(20, 100, 180, 255),
    mode: str = "RGBA",
) -> bytes:
    encoded = io.BytesIO()
    Image.new(mode, size, color).save(encoded, format="PNG")
    return encoded.getvalue()


def _success_payload(url: str, raw: bytes) -> FetchPayload:
    return FetchPayload(
        raw=raw,
        http_status=200,
        mime_type="image/png",
        final_url=url,
    )


@pytest.fixture(scope="module")
def enriched_manifest() -> dict:
    return _enriched_manifest()


def test_manifest_validation_checks_self_sha_and_probe_evidence(enriched_manifest) -> None:
    validate_review_manifest(enriched_manifest)

    bad_sha = copy.deepcopy(enriched_manifest)
    bad_sha["candidates"][0]["country"] = "Changed"
    with pytest.raises(ValueError, match="SHA"):
        validate_review_manifest(bad_sha)

    bad_probe = copy.deepcopy(enriched_manifest)
    bad_probe["probe_attempts"][0]["response_bytes"] += 1
    bad_probe["manifest_sha256"] = manifest_sha256(bad_probe)
    with pytest.raises(ValueError, match="attempt|logical|metrics"):
        validate_review_manifest(bad_probe)


def test_blinded_order_uses_manifest_sha_and_not_discovery_order() -> None:
    manifest_sha = "a" * 64
    candidates = [
        {
            "candidate_id": "candidate-%04d" % index,
            "review_url": "https://images.divisare.com/%d" % index,
            "probe_status": "success",
        }
        for index in range(1, 9)
    ]
    forward = blinded_order(candidates, manifest_sha)
    reversed_order = blinded_order(list(reversed(candidates)), manifest_sha)
    expected_ids = sorted(
        (row["candidate_id"] for row in candidates),
        key=lambda candidate_id: hashlib.sha256(
            (manifest_sha + candidate_id).encode("utf-8")
        ).digest(),
    )
    assert [row.candidate_id for row in forward] == expected_ids
    assert forward == reversed_order
    assert [row.blinded_index for row in forward] == list(range(1, 9))


def test_page_contract_caps_at_25() -> None:
    items = [ReviewItem(index, str(index), "https://images.divisare.com/x", "success") for index in range(1, 31)]
    assert [row.blinded_index for row in page_items(items, page=2, page_size=25)] == list(
        range(26, 31)
    )
    with pytest.raises(ValueError, match="cannot exceed 25"):
        page_items(items, page=1, page_size=26)
    with pytest.raises(ValueError, match="exceeds"):
        page_items(items, page=3, page_size=25)


def test_decode_applies_exif_and_composites_alpha_on_white() -> None:
    rgba = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
    rgba.putpixel((1, 0), (255, 0, 0, 255))
    encoded = io.BytesIO()
    rgba.save(encoded, format="PNG")
    decoded = _decode_rgb_white(encoded.getvalue())
    assert decoded.mode == "RGB"
    assert decoded.getpixel((0, 0)) == (255, 255, 255)
    assert decoded.getpixel((1, 0)) == (255, 0, 0)

    oriented = Image.new("RGB", (3, 2), (10, 20, 30))
    exif = oriented.getexif()
    exif[274] = 6
    encoded = io.BytesIO()
    oriented.save(encoded, format="JPEG", exif=exif)
    assert _decode_rgb_white(encoded.getvalue()).size == (2, 3)


def test_contain_thumbnail_never_upscales() -> None:
    small = Image.new("RGB", (40, 20), "red")
    assert contain_no_upscale(small, (256, 192)).size == (40, 20)
    large = Image.new("RGB", (400, 100), "red")
    assert contain_no_upscale(large, (200, 200)).size == (200, 50)


def test_render_is_deterministic_and_marks_failures_without_fetching_probe_failure() -> None:
    items = [
        ReviewItem(9, "candidate-0009", "https://images.divisare.com/good", "success"),
        ReviewItem(10, "candidate-0010", "https://images.divisare.com/probe-failed", "failed"),
        ReviewItem(11, "candidate-0011", "https://images.divisare.com/fetch-failed", "success"),
    ]
    raw = _png_bytes()

    def run_once():
        calls: list[str] = []

        def fetch(url: str, *, timeout, max_bytes):
            calls.append(url)
            if url.endswith("fetch-failed"):
                raise RuntimeError("test fetch failure")
            return _success_payload(url, raw)

        return render_sheet(items, columns=2, fetcher=fetch), calls

    first, first_calls = run_once()
    second, second_calls = run_once()
    assert first.png_bytes == second.png_bytes
    assert first_calls == second_calls == [items[0].review_url, items[2].review_url]
    assert first.unavailable_candidate_ids == ("candidate-0010", "candidate-0011")
    assert first.id_mapping == (
        {"blinded_index": 9, "candidate_id": "candidate-0009"},
        {"blinded_index": 10, "candidate_id": "candidate-0010"},
        {"blinded_index": 11, "candidate_id": "candidate-0011"},
    )
    with Image.open(io.BytesIO(first.png_bytes)) as sheet:
        assert sheet.mode == "RGB"
        assert sheet.size == (
            (2 * SHEET_MARGIN) + (2 * CELL_WIDTH) + CELL_GAP,
            (2 * SHEET_MARGIN) + (2 * CELL_HEIGHT) + CELL_GAP,
        )


def test_write_sheet_fetches_only_page_review_urls_and_mapping_has_ids_only(
    tmp_path, enriched_manifest
) -> None:
    manifest_path = tmp_path / "enriched.json"
    manifest_path.write_text(json.dumps(enriched_manifest), encoding="utf-8")
    output_path = tmp_path / "page.png"
    mapping_path = tmp_path / "page.json"
    raw = _png_bytes()
    calls: list[str] = []

    def fetch(url: str, *, timeout, max_bytes):
        calls.append(url)
        assert "/" + REVIEW_PROFILE + "/" in url
        return _success_payload(url, raw)

    rendered = write_review_sheet(
        manifest_path=manifest_path,
        output_path=output_path,
        mapping_output_path=mapping_path,
        page=2,
        page_size=3,
        columns=2,
        fetcher=fetch,
    )
    expected = page_items(
        blinded_order(enriched_manifest["candidates"], enriched_manifest["manifest_sha256"]),
        page=2,
        page_size=3,
    )
    assert calls == [row.review_url for row in expected]
    assert output_path.read_bytes() == rendered.png_bytes
    mapping = json.loads(mapping_path.read_text(encoding="ascii"))
    assert mapping == list(rendered.id_mapping)
    assert all(set(row) == {"blinded_index", "candidate_id"} for row in mapping)


def test_no_clobber_fails_before_manifest_read_or_fetch(tmp_path) -> None:
    output = tmp_path / "existing.png"
    output.write_bytes(b"keep")
    called = False

    def fetch(url: str, *, timeout, max_bytes):
        nonlocal called
        called = True
        raise AssertionError("must not fetch")

    with pytest.raises(FileExistsError, match="already exists"):
        write_review_sheet(
            manifest_path=tmp_path / "missing.json",
            output_path=output,
            fetcher=fetch,
        )
    assert output.read_bytes() == b"keep"
    assert called is False


def test_mapping_serializer_rejects_discovery_hints() -> None:
    raw = mapping_json_bytes([{"blinded_index": 1, "candidate_id": "candidate-0001"}])
    assert json.loads(raw) == [{"blinded_index": 1, "candidate_id": "candidate-0001"}]
    with pytest.raises(ValueError, match="only"):
        mapping_json_bytes(
            [
                {
                    "blinded_index": 1,
                    "candidate_id": "candidate-0001",
                    "discovery_class": "drawing",
                }
            ]
        )


def test_load_manifest_is_offline(tmp_path, enriched_manifest, monkeypatch) -> None:
    manifest_path = tmp_path / "enriched.json"
    manifest_path.write_text(json.dumps(enriched_manifest), encoding="utf-8")

    def forbidden_network(*args, **kwargs):
        raise AssertionError("manifest validation must not use the network")

    monkeypatch.setattr(
        "tools.render_divisare_vision_review_sheet.network_fetch", forbidden_network
    )
    assert len(load_review_manifest(manifest_path)["candidates"]) == 560
