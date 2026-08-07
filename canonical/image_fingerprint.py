"""Source-neutral image normalization and fingerprinting for E1.

Source adapters own URL selection and downloads. This module only accepts
response bytes so Divisare, Architizer, and later sources use the same local
raster contract before exact-pixel and perceptual hashes are compared.
"""

from __future__ import annotations

import hashlib
import io
import struct
import warnings
from dataclasses import asdict, dataclass
from typing import Any


FINGERPRINT_CONTRACT_VERSION = "archibe-e1-fingerprint-v1"
NORMALIZER_VERSION = "archibe-image-raster-v1"
PIXEL_HASH_VERSION = "archibe-e1-rgb-v1"
PHASH_VERSION = "imagehash-phash-256-hf4-archibe-raster-v1"
TARGET_LONG_EDGE = 512
DEFAULT_MAX_INPUT_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_SOURCE_PIXELS = 80_000_000
_PIXEL_PREFIX = b"archibe-e1-rgb-v1\0"
_HEX = frozenset("0123456789abcdef")


class FingerprintError(ValueError):
    """A terminal decode or normalization failure with a stable error kind."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ImageFingerprint:
    response_sha256: str
    decoded_format: str
    source_width: int
    source_height: int
    source_mode: str
    exif_orientation: int | None
    icc_profile_sha256: str | None
    color_status: str
    had_alpha: bool
    frame_count: int
    is_animated: bool
    normalized_width: int
    normalized_height: int
    pixel_sha256: str
    phash256: str
    quality_flags: tuple[str, ...]
    normalizer_version: str = NORMALIZER_VERSION
    pixel_hash_version: str = PIXEL_HASH_VERSION
    phash_version: str = PHASH_VERSION

    @property
    def auto_exact_eligible(self) -> bool:
        """Whether equal pixel SHA values may dedupe image occurrences.

        This never authorizes merging buildings. Animated/multipage, invalid
        color metadata, and near-blank images require review even when frame-0
        pixels happen to match.
        """

        blocked = {"animated_or_multipage", "icc_invalid_assumed_srgb", "low_information"}
        return not blocked.intersection(self.quality_flags)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def dependency_versions() -> dict[str, str | None]:
    """Return dependencies that can affect normalized pixels or pHash bits."""

    import imagehash
    import numpy
    import scipy
    from PIL import Image, features

    return {
        "pillow": getattr(Image, "__version__", None),
        "imagehash": getattr(imagehash, "__version__", None),
        "numpy": getattr(numpy, "__version__", None),
        "scipy": getattr(scipy, "__version__", None),
        "littlecms2": features.version("littlecms2"),
    }


def phash_distance(left: str, right: str) -> int:
    """Return the Hamming distance between two lowercase 256-bit pHashes."""

    for value in (left, right):
        if len(value) != 64 or any(char not in _HEX for char in value):
            raise ValueError("pHash must be a lowercase 64-character hexadecimal value")
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _half_up_scaled(value: int, numerator: int, denominator: int) -> int:
    return max(1, (value * numerator * 2 + denominator) // (2 * denominator))


def _normalized_size(width: int, height: int) -> tuple[int, int]:
    if width >= height:
        return TARGET_LONG_EDGE, _half_up_scaled(height, TARGET_LONG_EDGE, width)
    return _half_up_scaled(width, TARGET_LONG_EDGE, height), TARGET_LONG_EDGE


def _has_alpha(image: Any, info: dict[str, Any]) -> bool:
    return "A" in image.getbands() or "transparency" in info


def _convert_to_srgb(image: Any, icc_profile: bytes | None, had_alpha: bool):
    """Return (image, color_status, flags) before white alpha compositing."""

    from PIL import ImageCms

    if not icc_profile:
        return image, "assumed_srgb", []

    try:
        source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
        target_profile = ImageCms.createProfile("sRGB")
        if had_alpha:
            rgba = image.convert("RGBA")
            alpha = rgba.getchannel("A")
            converted = ImageCms.profileToProfile(
                rgba.convert("RGB"),
                source_profile,
                target_profile,
                outputMode="RGB",
            )
            converted.putalpha(alpha)
        else:
            converted = ImageCms.profileToProfile(
                image,
                source_profile,
                target_profile,
                outputMode="RGB",
            )
        return converted, "icc_to_srgb", []
    except (ImageCms.PyCMSError, OSError, TypeError, ValueError) as exc:
        return image, "icc_invalid_assumed_srgb", [
            "icc_invalid_assumed_srgb",
            "icc_error:%s" % type(exc).__name__,
        ]


def _white_matte_rgb(image: Any, had_alpha: bool):
    from PIL import Image

    if not had_alpha:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, rgba).convert("RGB")


def fingerprint_bytes(
    raw: bytes,
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
) -> ImageFingerprint:
    """Decode response bytes and compute the frozen E1 local fingerprints."""

    import imagehash
    from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    if not raw:
        raise FingerprintError("empty_response", "image response is empty")
    if len(raw) > max_input_bytes:
        raise FingerprintError(
            "input_too_large",
            "image response exceeds %d bytes" % max_input_bytes,
        )
    if max_source_pixels <= 0:
        raise ValueError("max_source_pixels must be positive")

    response_sha = hashlib.sha256(raw).hexdigest()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as opened:
                source_format = str(opened.format or "").upper()
                source_width, source_height = opened.size
                source_mode = str(opened.mode)
                if source_width <= 0 or source_height <= 0:
                    raise FingerprintError(
                        "invalid_dimensions", "decoded image has non-positive dimensions"
                    )
                if source_width * source_height > max_source_pixels:
                    raise FingerprintError(
                        "source_pixels_exceeded",
                        "decoded image exceeds %d source pixels" % max_source_pixels,
                    )

                info = dict(opened.info)
                icc_value = info.get("icc_profile")
                icc_profile = bytes(icc_value) if isinstance(icc_value, (bytes, bytearray)) else None
                icc_sha = hashlib.sha256(icc_profile).hexdigest() if icc_profile else None
                try:
                    orientation_value = opened.getexif().get(274)
                    exif_orientation = int(orientation_value) if orientation_value else None
                except (AttributeError, OSError, TypeError, ValueError):
                    exif_orientation = None

                try:
                    frame_count = int(getattr(opened, "n_frames", 1) or 1)
                except (EOFError, OSError, ValueError):
                    frame_count = 1
                is_animated = bool(getattr(opened, "is_animated", False) or frame_count > 1)
                opened.seek(0)
                opened.load()
                had_alpha = _has_alpha(opened, info)
                frame = opened.copy()

        oriented = ImageOps.exif_transpose(frame)
        color_image, color_status, color_flags = _convert_to_srgb(
            oriented, icc_profile, had_alpha
        )
        rgb = _white_matte_rgb(color_image, had_alpha)
        normalized_size = _normalized_size(rgb.width, rgb.height)
        normalized = rgb.resize(normalized_size, Image.Resampling.LANCZOS)
    except FingerprintError:
        raise
    except Image.DecompressionBombWarning as exc:
        raise FingerprintError("decompression_bomb", str(exc)) from exc
    except Image.DecompressionBombError as exc:
        raise FingerprintError("decompression_bomb", str(exc)) from exc
    except (UnidentifiedImageError, EOFError, OSError, SyntaxError, ValueError) as exc:
        raise FingerprintError("decode", str(exc)) from exc

    flags = list(color_flags)
    if is_animated or frame_count > 1:
        flags.append("animated_or_multipage")
    luminance_stddev = float(ImageStat.Stat(normalized.convert("L")).stddev[0])
    if luminance_stddev < 2.0:
        flags.append("low_information")

    pixel_bytes = normalized.tobytes()
    pixel_sha = hashlib.sha256(
        _PIXEL_PREFIX
        + struct.pack(">II", normalized.width, normalized.height)
        + pixel_bytes
    ).hexdigest()
    phash256 = str(
        imagehash.phash(normalized, hash_size=16, highfreq_factor=4)
    ).casefold()
    if len(phash256) != 64 or any(char not in _HEX for char in phash256):
        raise FingerprintError("invalid_phash", "pHash is not lowercase 256-bit hex")

    return ImageFingerprint(
        response_sha256=response_sha,
        decoded_format=source_format,
        source_width=source_width,
        source_height=source_height,
        source_mode=source_mode,
        exif_orientation=exif_orientation,
        icc_profile_sha256=icc_sha,
        color_status=color_status,
        had_alpha=had_alpha,
        frame_count=frame_count,
        is_animated=is_animated,
        normalized_width=normalized.width,
        normalized_height=normalized.height,
        pixel_sha256=pixel_sha,
        phash256=phash256,
        quality_flags=tuple(sorted(set(flags))),
    )
