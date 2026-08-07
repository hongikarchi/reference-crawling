from __future__ import annotations

import io

import pytest
from PIL import Image, ImageCms, ImageDraw

from canonical.image_fingerprint import (
    FINGERPRINT_CONTRACT_VERSION,
    NORMALIZER_VERSION,
    PHASH_VERSION,
    FingerprintError,
    dependency_versions,
    fingerprint_bytes,
    phash_distance,
)


def _bytes(image: Image.Image, format_name: str, **kwargs) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format_name, **kwargs)
    return output.getvalue()


def _test_image(size: tuple[int, int] = (80, 50)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((3, 4, size[0] // 2, size[1] - 5), fill=(20, 70, 180))
    draw.ellipse((size[0] // 2, 7, size[0] - 4, size[1] - 3), fill=(220, 60, 30))
    return image


def test_lossless_formats_share_normalized_pixel_hash() -> None:
    image = _test_image()
    png = fingerprint_bytes(_bytes(image, "PNG"))
    bmp = fingerprint_bytes(_bytes(image, "BMP"))

    assert png.response_sha256 != bmp.response_sha256
    assert png.pixel_sha256 == bmp.pixel_sha256
    assert png.phash256 == bmp.phash256
    assert (png.normalized_width, png.normalized_height) == (512, 320)
    assert png.normalizer_version == NORMALIZER_VERSION
    assert png.phash_version == PHASH_VERSION


def test_exif_orientation_is_applied_before_hashing() -> None:
    upright = _test_image((70, 40))
    stored = upright.transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[274] = 6

    upright_result = fingerprint_bytes(_bytes(upright, "PNG"))
    oriented_result = fingerprint_bytes(_bytes(stored, "PNG", exif=exif))

    assert oriented_result.exif_orientation == 6
    assert oriented_result.source_width == 40
    assert oriented_result.source_height == 70
    assert oriented_result.pixel_sha256 == upright_result.pixel_sha256


def test_transparent_hidden_rgb_is_composited_on_white() -> None:
    left = Image.new("RGBA", (32, 16), (255, 0, 0, 0))
    right = Image.new("RGBA", (32, 16), (0, 0, 255, 0))
    for image in (left, right):
        ImageDraw.Draw(image).rectangle((8, 2, 24, 13), fill=(10, 100, 40, 255))

    left_result = fingerprint_bytes(_bytes(left, "PNG"))
    right_result = fingerprint_bytes(_bytes(right, "PNG"))

    assert left_result.had_alpha is True
    assert left_result.pixel_sha256 == right_result.pixel_sha256


def test_valid_and_invalid_icc_profiles_are_explicit() -> None:
    image = _test_image()
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    converted = fingerprint_bytes(_bytes(image, "PNG", icc_profile=profile))
    invalid = fingerprint_bytes(_bytes(image, "PNG", icc_profile=b"not-an-icc-profile"))

    assert converted.color_status == "icc_to_srgb"
    assert converted.icc_profile_sha256 is not None
    assert invalid.color_status == "icc_invalid_assumed_srgb"
    assert "icc_invalid_assumed_srgb" in invalid.quality_flags
    assert invalid.auto_exact_eligible is False


def test_animation_uses_first_frame_and_blocks_auto_exact() -> None:
    first = _test_image((40, 30))
    second = Image.new("RGB", first.size, "black")
    raw = _bytes(first, "GIF", save_all=True, append_images=[second], duration=100, loop=0)

    result = fingerprint_bytes(raw)

    assert result.frame_count == 2
    assert result.is_animated is True
    assert "animated_or_multipage" in result.quality_flags
    assert result.auto_exact_eligible is False


def test_jpeg_is_near_candidate_but_not_exact_pixel_identity() -> None:
    image = _test_image((160, 100))
    lossless = fingerprint_bytes(_bytes(image, "PNG"))
    jpeg = fingerprint_bytes(_bytes(image, "JPEG", quality=60))

    assert lossless.pixel_sha256 != jpeg.pixel_sha256
    assert phash_distance(lossless.phash256, jpeg.phash256) <= 16


def test_low_information_and_error_contracts() -> None:
    blank = fingerprint_bytes(_bytes(Image.new("RGB", (10, 10), "white"), "PNG"))
    assert "low_information" in blank.quality_flags
    assert blank.auto_exact_eligible is False

    with pytest.raises(FingerprintError, match="empty") as empty:
        fingerprint_bytes(b"")
    assert empty.value.kind == "empty_response"

    with pytest.raises(FingerprintError) as invalid:
        fingerprint_bytes(b"not an image")
    assert invalid.value.kind == "decode"

    raw = _bytes(Image.new("RGB", (11, 10), "white"), "PNG")
    with pytest.raises(FingerprintError) as oversized:
        fingerprint_bytes(raw, max_source_pixels=100)
    assert oversized.value.kind == "source_pixels_exceeded"


def test_phash_validation_and_dependency_provenance() -> None:
    assert phash_distance("0" * 64, "f" * 64) == 256
    with pytest.raises(ValueError, match="lowercase"):
        phash_distance("A" * 64, "0" * 64)

    versions = dependency_versions()
    assert set(versions) == {"pillow", "imagehash", "numpy", "scipy", "littlecms2"}
    assert all(versions.values())


def test_cross_machine_golden_raster_and_hashes() -> None:
    raw = b"P6\n3 2\n255\n" + bytes(
        [
            255, 0, 0,
            0, 255, 0,
            0, 0, 255,
            255, 255, 255,
            0, 0, 0,
            127, 127, 127,
        ]
    )

    result = fingerprint_bytes(raw)

    assert FINGERPRINT_CONTRACT_VERSION == "archibe-e1-fingerprint-v1"
    assert result.response_sha256 == "dfad052c8d342cf69b5a6a7615ceb4716139f40aa066246c39dee72f1cc3cb40"
    assert result.pixel_sha256 == "ecb32d8f4c8d365272ed11e5b72ffeb6f86ac6fdb19d65cd3f012b7eb4d12374"
    assert result.phash256 == "e2321e67ab32f67fab26f679ae4c01d854cd09988193fe26a932f636ab2401f8"
    assert (result.normalized_width, result.normalized_height) == (512, 341)


def test_cross_machine_icc_alpha_golden_pixels() -> None:
    image = Image.new("RGBA", (4, 3), (0, 0, 255, 0))
    pixels = image.load()
    for y in range(3):
        for x in range(4):
            pixels[x, y] = (
                x * 60,
                y * 90,
                40 + x * 20,
                255 if (x + y) % 2 else 80,
            )
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    result = fingerprint_bytes(_bytes(image, "PNG", icc_profile=profile))

    assert result.color_status == "icc_to_srgb"
    assert result.had_alpha is True
    assert result.pixel_sha256 == "bb3be3720d7ba0f742cdc2ae865d101a30a1872948e34be56fc26494d49c17a5"
    assert result.phash256 == "bbb17ab1f335ae708e106e70f211af70801aff62503e5b0651de530e79da1a2d"
