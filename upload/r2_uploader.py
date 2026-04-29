"""Phase 9 — Cover-only R2 upload + BlurHash precompute.

Path C image hosting strategy (per make_web research/infra/02-image-
hosting-strategy.md §15): for each canonical building we own the cover
image (download → R2) but keep gallery + drawings as hot-link URLs
from the source CDN. ~80% storage drop, swipe UX preserved via
cover-CDN + BlurHash placeholder.

This module exposes pure helpers; the actual upload run is invoked
from upload/neon_strict.py with --enable-cover-r2-upload.

Three operations per row:

  1. download cover_image_url to a local temp file (in-memory bytes
     are also returned to avoid disk write when not needed)
  2. compute BlurHash hash from the in-memory PIL image (~30 ms)
  3. upload to R2 at key {building_id}/cover.<ext>; return the public URL

The set of three is wrapped in `process_one_cover()` so the caller
just gets back (cover_image_cdn_url, cover_blurhash) per row.
"""

from __future__ import annotations

import io
import os
from typing import Optional
from urllib.parse import urlparse

import requests


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# imgix / Cloudflare CDN URLs sometimes include resize params we want
# stripped before storing as the source URL. Not used here, just noted.
_R2_KEY_TEMPLATE = "{building_id}/cover{ext}"
_DEFAULT_TIMEOUT = 30
_MAX_BYTES = 8 * 1024 * 1024   # 8 MB hard cap — anything larger is suspect

_VALID_IMAGE_TYPES = {
    "image/jpeg":     ".jpg",
    "image/jpg":      ".jpg",
    "image/png":      ".png",
    "image/webp":     ".webp",
    "image/gif":      ".gif",
}

# BlurHash size — 4×3 components is the documented sweet spot for
# placeholder UX (small enough to be ~30 bytes, big enough to capture
# horizontal architecture banners).
_BLURHASH_X_COMPONENTS = 4
_BLURHASH_Y_COMPONENTS = 3


# ---------------------------------------------------------------------------
# 1. Download
# ---------------------------------------------------------------------------

def fetch_image_bytes(url: str, *, timeout: int = _DEFAULT_TIMEOUT,
                      max_bytes: int = _MAX_BYTES) -> tuple[bytes, str]:
    """Download an image URL into memory. Returns (bytes, ext) where ext
    is one of '.jpg' / '.png' / '.webp' / '.gif'. Raises on:
      - HTTP non-2xx
      - Content-Length > max_bytes
      - Unsupported Content-Type
      - Body size exceeds max_bytes during streaming
    """
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*,*/*;q=0.8",
    }
    resp = requests.get(url, headers=headers, stream=True, timeout=timeout)
    resp.raise_for_status()

    content_type = (resp.headers.get("Content-Type", "")
                    .split(";")[0].strip().lower())
    if content_type not in _VALID_IMAGE_TYPES:
        # Some CDNs serve images without proper Content-Type; fall back to
        # URL extension (e.g. ".../foo.jpg")
        ext_from_url = os.path.splitext(urlparse(url).path)[1].lower()
        if ext_from_url in _VALID_IMAGE_TYPES.values():
            ext = ext_from_url
        else:
            raise ValueError(f"unsupported content-type {content_type!r} "
                             f"for url {url!r}")
    else:
        ext = _VALID_IMAGE_TYPES[content_type]

    # Stream up to max_bytes (don't trust Content-Length blindly)
    buf = io.BytesIO()
    total = 0
    for chunk in resp.iter_content(chunk_size=16384):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"image exceeds max_bytes={max_bytes} for url {url!r}")
        buf.write(chunk)
    return buf.getvalue(), ext


# ---------------------------------------------------------------------------
# 2. BlurHash
# ---------------------------------------------------------------------------

def compute_blurhash(image_bytes: bytes,
                     x_components: int = _BLURHASH_X_COMPONENTS,
                     y_components: int = _BLURHASH_Y_COMPONENTS) -> Optional[str]:
    """Compute BlurHash hash from in-memory image bytes. Returns the hash
    string (~30 chars) or None on failure (corrupt image, unsupported)."""
    try:
        import blurhash
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(f"missing dep: {e}. pip install blurhash Pillow") from e
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
        # blurhash lib expects RGB
        if img.mode != "RGB":
            img = img.convert("RGB")
        return blurhash.encode(img, x_components=x_components,
                               y_components=y_components)
    except Exception as e:
        print(f"  [r2_uploader.compute_blurhash] failed: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# 3. R2 upload
# ---------------------------------------------------------------------------

def upload_to_r2(client, bucket: str, key: str, image_bytes: bytes,
                 content_type: str = "image/jpeg",
                 public_url_base: Optional[str] = None) -> str:
    """Upload image bytes to R2 under `key`. Returns the public URL if
    `public_url_base` is set, otherwise the s3://bucket/key URI.
    public_url_base example: 'https://images.archi-tinder.com'
    """
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=image_bytes,
        ContentType=content_type,
    )
    if public_url_base:
        return f"{public_url_base.rstrip('/')}/{key}"
    return f"s3://{bucket}/{key}"


# ---------------------------------------------------------------------------
# Combined: process one cover end-to-end
# ---------------------------------------------------------------------------

def process_one_cover(
    *,
    building_id: str,
    cover_image_url: str,
    r2_client,
    r2_bucket: str,
    public_url_base: Optional[str] = None,
    skip_if_exists: bool = True,
    existing_keys: Optional[set] = None,
    compute_hash: bool = True,
) -> dict:
    """Full pipeline for one canonical row's cover image.

    Returns a dict:
      {
        "cover_image_cdn_url": str | None,   # R2 public URL (None on failure)
        "cover_blurhash":      str | None,   # ~30 chars (None on failure or skip)
        "skipped":             bool,         # True if existing_keys had it
        "error":               str | None,   # set on hard failure (caller logs)
      }
    """
    out = {"cover_image_cdn_url": None, "cover_blurhash": None,
           "skipped": False, "error": None}

    if not cover_image_url:
        out["error"] = "no_cover_image_url"
        return out

    # Probe key first to potentially skip
    # Determine extension from URL early (override if Content-Type disagrees)
    ext_from_url = os.path.splitext(urlparse(cover_image_url).path)[1].lower()
    if ext_from_url not in _VALID_IMAGE_TYPES.values():
        ext_from_url = ".jpg"
    candidate_key = _R2_KEY_TEMPLATE.format(building_id=building_id, ext=ext_from_url)

    if skip_if_exists and existing_keys is not None and candidate_key in existing_keys:
        out["cover_image_cdn_url"] = (f"{public_url_base.rstrip('/')}/{candidate_key}"
                                       if public_url_base
                                       else f"s3://{r2_bucket}/{candidate_key}")
        out["skipped"] = True
        return out

    try:
        image_bytes, ext = fetch_image_bytes(cover_image_url)
    except Exception as e:
        out["error"] = f"fetch_failed: {type(e).__name__}: {e}"
        return out

    # Re-derive key with the actual ext (in case Content-Type differed from URL)
    final_key = _R2_KEY_TEMPLATE.format(building_id=building_id, ext=ext)

    if compute_hash:
        try:
            out["cover_blurhash"] = compute_blurhash(image_bytes)
        except RuntimeError as e:
            print(f"  [process_one_cover] blurhash dep missing: {e}")

    try:
        content_type = next((ct for ct, e in _VALID_IMAGE_TYPES.items() if e == ext),
                            "image/jpeg")
        out["cover_image_cdn_url"] = upload_to_r2(
            r2_client, r2_bucket, final_key, image_bytes,
            content_type=content_type, public_url_base=public_url_base,
        )
    except Exception as e:
        out["error"] = f"r2_upload_failed: {type(e).__name__}: {e}"
        return out

    return out


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """Smoke test: download + blurhash one URL. Doesn't upload to R2 (no key
    needed). Use to verify the deps + image fetching work."""
    import argparse
    p = argparse.ArgumentParser(description="r2_uploader smoke test (download + blurhash, no R2 upload)")
    p.add_argument("--url", required=True, help="image URL to test")
    args = p.parse_args(argv)

    print(f"Fetching {args.url} ...")
    image_bytes, ext = fetch_image_bytes(args.url)
    print(f"  {len(image_bytes):,} bytes, ext={ext}")
    print("Computing BlurHash ...")
    h = compute_blurhash(image_bytes)
    print(f"  blurhash: {h!r} ({len(h or '')} chars)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
