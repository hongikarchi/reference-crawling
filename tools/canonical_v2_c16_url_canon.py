#!/usr/bin/env python3
"""URL canonicalization helpers for C16 image polish.

`_canonical_asset_key(url)` reduces a CDN URL (thumb / sized variant / different
query params) to the underlying source asset identity so two URLs that point
at the same original image map to the same key.

`_is_lowres_url` / `_is_gif` classify URLs by display suitability.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_DIVISARE_HASH_RE = re.compile(r"/v\d+/([0-9a-f]{20,40})(?:[/.]|$)")
# Cloudinary public_id (~16-40 alphanumeric lowercase; sometimes hyphens).
_DIVISARE_CLOUDINARY_RE = re.compile(r"/v\d+/([a-z0-9][a-z0-9-]{14,40})(?:[/.]|$)")
# UUID: 8-4-4-4-12 hex.
_DIVISARE_UUID_RE = re.compile(
    r"/v\d+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/.]|$)"
)
# Old format: /project_images/<id>/<token>(.ext or /SEO-filename)
_DIVISARE_OLD_RE = re.compile(r"/project_images/(\d+)/([^/?.]+)")
# Architizer: /uploads/<13-digit timestamp><basename>.<ext> ; same <basename>
# uploaded twice gets different timestamps → identity = basename without ts/ext.
_ARCH_UPLOADS_RE = re.compile(
    r"/uploads/\d{13}(.+?)\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)",
    re.I,
)
_ARCH_UPLOADS_FALLBACK_RE = re.compile(r"/uploads/(.+?)$")
_ARCHELLO_IMG_RE = re.compile(r"(?:/thumbs)?/images/(\d{4}/\d{2}/\d{2}/[^/?]+)")
_W_RE = re.compile(r"[?&](?:w|width)=(\d+)")
_H_RE = re.compile(r"[?&](?:h|height)=(\d+)")


def _canonical_asset_key(url) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    host = (p.netloc or "").lower()
    path = p.path or ""
    if "divisare" in host:
        # UUID first (more specific than generic cloudinary)
        m_uuid = _DIVISARE_UUID_RE.search(path)
        if m_uuid:
            return f"divisare|{m_uuid.group(1)}"
        # hex hash (matches Cloudinary hex public_ids too)
        m = _DIVISARE_HASH_RE.search(path)
        if m:
            return f"divisare|{m.group(1)}"
        # alphanumeric Cloudinary public_id (non-hex)
        m_cl = _DIVISARE_CLOUDINARY_RE.search(path)
        if m_cl:
            return f"divisare|{m_cl.group(1)}"
        # old format: /project_images/<id>/<token> — token only (extension &
        # SEO-trailing-filename variants are the same asset).
        m2 = _DIVISARE_OLD_RE.search(path)
        if m2:
            return f"divisare|{m2.group(1)}|{m2.group(2)}"
        # final fallback: full relative path
        return f"divisare|{path.strip('/')}" if path else None
    if "architizer" in host or "imgix" in host:
        # Architizer: strip leading 13-digit timestamp and extension
        m = _ARCH_UPLOADS_RE.search(path + "?")
        if m:
            return f"architizer|{m.group(1)}"
        m_fb = _ARCH_UPLOADS_FALLBACK_RE.search(path)
        if m_fb:
            # fallback: strip query, strip extension, strip leading 12-16 digits
            base = m_fb.group(1).split("?")[0]
            base = re.sub(r"\.[a-z0-9]{2,5}$", "", base, flags=re.I)
            base = re.sub(r"^\d{13}", "", base)
            return f"architizer|{base}"
        last = path.rstrip("/").split("/")[-1]
        return f"architizer|{last}" if last else None
    if "archello" in host:
        m = _ARCHELLO_IMG_RE.search(path)
        if m:
            return f"archello|{m.group(1)}"
        last = path.rstrip("/").split("/")[-1]
        return f"archello|{last}" if last else None
    last = path.rstrip("/").split("/")[-1]
    return f"{host}|{last}" if last else None


def _is_lowres_url(url) -> bool:
    if not isinstance(url, str) or not url:
        return False
    if "/thumbs/" in url:
        return True
    if "fit=crop" in url:
        return True
    for m in _W_RE.finditer(url):
        try:
            if int(m.group(1)) < 800:
                return True
        except ValueError:
            pass
    for m in _H_RE.finditer(url):
        try:
            if int(m.group(1)) < 600:
                return True
        except ValueError:
            pass
    return False


def _is_gif(url) -> bool:
    if not isinstance(url, str) or not url:
        return False
    base = url.split("?", 1)[0].lower()
    return base.endswith(".gif")


_RASTER_EXTS = {"jpg", "jpeg", "jpe", "png", "webp", "avif", "heic", "heif",
                "jfif", "gif"}


def _is_raster_url(url) -> bool:
    """True if URL is a web-renderable raster image.
    Whitelist approach: only known raster extensions are accepted.
    No extension or pure-digit "extension" (Architizer numeric filenames) is
    accepted as raster. Anything else (`.tiff`, `.pdf`, `.ai`, `.psd`, `.svg`,
    `.mp4`, `.db`, `.url`, `.docx`, `.jp2`, `.pg`, ...) is non-raster."""
    if not isinstance(url, str) or not url:
        return False
    base = url.split("?", 1)[0].rstrip("/").split("/")[-1].lower()
    if "." not in base:
        return True
    ext = base.rsplit(".", 1)[1]
    if not ext.isascii() or not ext.isalnum():
        return True
    if ext.isdigit():
        return True
    return ext in _RASTER_EXTS
