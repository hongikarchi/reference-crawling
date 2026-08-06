from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from canonical import divisare_vision_axes_devset as devset
from canonical import divisare_vision_axes_review_inputs as review_inputs
from canonical.divisare_image_smoke import FetchPayload
from canonical.divisare_vision_benchmark import decode_source, prepare_derivative
from canonical.divisare_vision_gold import SOURCE_PROFILE
from canonical.divisare_vision_gold_finalize import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
FROZEN_MANIFEST = (
    ROOT / "data" / "review" / review_inputs.EXPECTED_MANIFEST_FILENAME
)
FRESH_HOLDOUT_MANIFEST = (
    ROOT
    / "data"
    / "review"
    / review_inputs.EXPECTED_HOLDOUT_MANIFEST_FILENAME
)


def _jpeg_bytes(index: int) -> bytes:
    image = Image.new(
        "RGB",
        (1600, 900),
        ((index * 37) % 256, (index * 73) % 256, (index * 109) % 256),
    )
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


def _test_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, bytes], dict]:
    payload = copy.deepcopy(json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8")))
    images: dict[str, bytes] = {}
    for index, sample in enumerate(payload["audit_samples"], 1):
        raw = _jpeg_bytes(index)
        review_id = sample["review_id"]
        url = (
            "https://images.divisare.com/images/"
            f"{SOURCE_PROFILE}/v1/{review_id}.jpg"
        )
        sample["source_identity"]["request_url"] = url
        sample["image_evidence"]["content_sha256"] = hashlib.sha256(raw).hexdigest()
        images[url] = raw
    payload.pop("logical_sha256", None)
    payload.pop("manifest_sha256", None)
    payload["logical_sha256"] = devset.logical_sha256(payload)
    payload["manifest_sha256"] = devset.manifest_sha256(payload)
    manifest = tmp_path / review_inputs.EXPECTED_MANIFEST_FILENAME
    manifest.write_bytes(canonical_json_bytes(payload) + b"\n")
    monkeypatch.setattr(
        review_inputs,
        "EXPECTED_MANIFEST_FILE_SHA256",
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        review_inputs, "EXPECTED_MANIFEST_LOGICAL_SHA256", payload["logical_sha256"]
    )
    monkeypatch.setattr(
        review_inputs, "EXPECTED_MANIFEST_SHA256", payload["manifest_sha256"]
    )
    return manifest, images, payload


def _fetcher(images: dict[str, bytes], seen: list[str] | None = None):
    def fetch(url: str) -> FetchPayload:
        if seen is not None:
            seen.append(url)
        return FetchPayload(images[url], 200, "image/jpeg", url)

    return fetch


def test_frozen_manifest_and_all_supported_prefixes_are_bound() -> None:
    payload, file_sha, logical_sha = review_inputs._load_frozen_manifest(
        FROZEN_MANIFEST
    )
    assert file_sha == review_inputs.EXPECTED_MANIFEST_FILE_SHA256
    assert logical_sha == review_inputs.EXPECTED_MANIFEST_LOGICAL_SHA256
    for subset, limit in review_inputs.SUBSET_LIMITS.items():
        selected = review_inputs._selected_rows(payload, subset)
        assert len(selected) == limit
        prefix_ids = {
            row["review_id"] for row in payload["audit_samples"][:limit]
        }
        assert [row[0] for row in selected] == [
            row for row in payload["review_rows"] if row["review_id"] in prefix_ids
        ]


def test_fresh_holdout_manifest_is_bound_and_reviewer_rows_are_opaque() -> None:
    payload, file_sha, logical_sha = review_inputs._load_frozen_manifest(
        FRESH_HOLDOUT_MANIFEST
    )
    assert file_sha == review_inputs.EXPECTED_HOLDOUT_MANIFEST_FILE_SHA256
    assert logical_sha == review_inputs.EXPECTED_HOLDOUT_MANIFEST_LOGICAL_SHA256
    assert len(review_inputs._selected_rows(payload, "all")) == 50
    public = json.dumps(payload["review_rows"], sort_keys=True)
    assert "https://" not in public
    assert "proxy_class" not in public
    assert "candidate_id" not in public
    assert "asset_key" not in public


def test_n10_stages_exact_prefix_in_blinded_order_with_benchmark_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, images, source_payload = _test_manifest(tmp_path, monkeypatch)
    output = tmp_path / "review-n10"
    seen: list[str] = []
    result = review_inputs.stage_review_inputs(
        manifest_path=manifest,
        output_dir=output,
        subset="n10",
        fetcher=_fetcher(images, seen),
    )

    prefix = source_payload["audit_samples"][:10]
    prefix_by_id = {row["review_id"]: row for row in prefix}
    expected_public = [
        row
        for row in source_payload["review_rows"]
        if row["review_id"] in prefix_by_id
    ]
    assert seen == [
        prefix_by_id[row["review_id"]]["source_identity"]["request_url"]
        for row in expected_public
    ]
    public = json.loads((output / "review_inputs.json").read_text(encoding="utf-8"))
    assert set(public) == {
        "manifest_file_sha256",
        "manifest_logical_sha256",
        "review_rows",
    }
    assert [
        {"review_rank": row["review_rank"], "review_id": row["review_id"]}
        for row in public["review_rows"]
    ] == expected_public
    assert result["image_count"] == 10

    forbidden = (
        "https://",
        "candidate",
        "divisare",
        "source",
        "asset_key",
        "request_url",
        "hint",
    )
    public_text = (output / "review_inputs.json").read_text(encoding="utf-8")
    assert not any(value in public_text for value in forbidden)
    assert {path.name for path in output.iterdir()} == {"review_inputs.json"} | {
        f"{row['review_id']}.jpg" for row in expected_public
    }
    for row in public["review_rows"]:
        sample = prefix_by_id[row["review_id"]]
        raw = images[sample["source_identity"]["request_url"]]
        expected = prepare_derivative(decode_source(raw), "long1024", 1024)
        assert (output / row["file_name"]).read_bytes() == expected.encoded_bytes
        assert row["encoded_sha256"] == expected.encoded_sha256
        assert row["pixel_sha256"] == expected.pixel_sha256
        with Image.open(output / row["file_name"]) as image:
            assert image.format == "JPEG"
            assert image.size == (1024, 576)


def test_rejects_tampered_frozen_manifest_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, images, _payload = _test_manifest(tmp_path, monkeypatch)
    expected_sha = review_inputs.EXPECTED_MANIFEST_FILE_SHA256
    manifest.write_bytes(manifest.read_bytes() + b" ")
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() != expected_sha
    fetched = False

    def fetch(_url: str) -> FetchPayload:
        nonlocal fetched
        fetched = True
        raise AssertionError("fetch must not run")

    output = tmp_path / "tampered-output"
    with pytest.raises(ValueError, match="file SHA mismatch"):
        review_inputs.stage_review_inputs(
            manifest_path=manifest,
            output_dir=output,
            subset="n10",
            fetcher=fetch,
        )
    assert fetched is False
    assert not output.exists()
    assert not list(tmp_path.glob(".tampered-output.partial-*"))


def test_content_mismatch_leaves_no_final_or_partial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, images, _payload = _test_manifest(tmp_path, monkeypatch)
    output = tmp_path / "mismatch-output"

    def wrong_fetch(url: str) -> FetchPayload:
        return FetchPayload(b"not-the-frozen-response", 200, "image/jpeg", url)

    with pytest.raises(ValueError, match="frozen response SHA mismatch"):
        review_inputs.stage_review_inputs(
            manifest_path=manifest,
            output_dir=output,
            subset="n10",
            fetcher=wrong_fetch,
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".mismatch-output.partial-*"))
    assert images


def test_rejects_redirect_off_divisare_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, images, _payload = _test_manifest(tmp_path, monkeypatch)
    output = tmp_path / "redirect-output"

    def redirected(url: str) -> FetchPayload:
        return FetchPayload(images[url], 200, "image/jpeg", "https://example.com/x.jpg")

    with pytest.raises(ValueError, match="final_url must use the Divisare"):
        review_inputs.stage_review_inputs(
            manifest_path=manifest,
            output_dir=output,
            subset="n10",
            fetcher=redirected,
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".redirect-output.partial-*"))


def test_no_clobber_preserves_existing_directory_without_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, images, _payload = _test_manifest(tmp_path, monkeypatch)
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="ascii")
    fetched = False

    def fetch(_url: str) -> FetchPayload:
        nonlocal fetched
        fetched = True
        raise AssertionError("fetch must not run")

    with pytest.raises(FileExistsError, match="already exists"):
        review_inputs.stage_review_inputs(
            manifest_path=manifest,
            output_dir=output,
            subset="all",
            fetcher=fetch,
        )
    assert fetched is False
    assert sentinel.read_text(encoding="ascii") == "keep"
    assert not list(tmp_path.glob(".existing.partial-*"))
    assert images
