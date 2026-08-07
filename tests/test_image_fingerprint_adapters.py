from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from canonical.image_fingerprint_adapters import (
    ARCHITIZER_FETCH_PROFILE_VERSION,
    DIVISARE_FETCH_PROFILE_VERSION,
    SourceAsset,
    architizer_effective_fetch_url,
    divisare_effective_fetch_url,
    iter_architizer_source_assets,
    iter_divisare_source_assets,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_no_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(path) + suffix).exists()


def _make_divisare(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE image_assets(
              asset_key TEXT PRIMARY KEY,
              original_filename TEXT,
              url_generation TEXT NOT NULL
            );
            CREATE TABLE image_urls(
              url_id INTEGER PRIMARY KEY,
              asset_key TEXT NOT NULL,
              url TEXT NOT NULL,
              transform_signature TEXT,
              url_generation TEXT NOT NULL
            );
            CREATE TABLE source_image_occurrences(
              article_id INTEGER NOT NULL,
              role TEXT NOT NULL,
              position INTEGER NOT NULL,
              raw_url TEXT NOT NULL,
              parse_status TEXT NOT NULL,
              asset_key TEXT
            );
            """
        )
        modern_gallery = (
            "https://images.divisare.com/images/f_auto,q_auto,w_auto/"
            "v100/modern-hash/project.jpg"
        )
        modern_cover = (
            "https://images.divisare.com/image/upload/c_fit,f_jpg/"
            "v100/modern-hash.jpg"
        )
        pdf_url = (
            "https://images.divisare.com/images/f_auto/v1/"
            "project_images/42/plan.pdf/project.jpg"
        )
        video_endpoint = (
            "https://images.divisare.com/videos/upload/v1/movie.mp4"
        )
        disguised_video = (
            "https://images.divisare.com/images/f_auto/v1/"
            "project_images/43/movie.mp4/project.jpg"
        )
        conn.executemany(
            "INSERT INTO image_assets VALUES (?,?,?)",
            [
                ("divisare|modern-hash|v100", None, "cloudinary_public_id"),
                ("divisare|42|plan.pdf", "plan.pdf", "project_images"),
                ("divisare|video-endpoint", "movie.mp4", "cloudinary_public_id"),
                ("divisare|43|movie.mp4", "movie.mp4", "project_images"),
            ],
        )
        conn.executemany(
            "INSERT INTO image_urls VALUES (?,?,?,?,?)",
            [
                (1, "divisare|modern-hash|v100", modern_gallery, "f_auto", "cloudinary_public_id"),
                (2, "divisare|modern-hash|v100", modern_cover, "c_fit,f_jpg", "cloudinary_public_id"),
                (3, "divisare|42|plan.pdf", pdf_url, "f_auto", "project_images"),
                (4, "divisare|video-endpoint", video_endpoint, None, "cloudinary_public_id"),
                (5, "divisare|43|movie.mp4", disguised_video, "f_auto", "project_images"),
            ],
        )
        conn.executemany(
            "INSERT INTO source_image_occurrences VALUES (?,?,?,?,?,?)",
            [
                (10, "cover", 0, modern_cover, "parsed", "divisare|modern-hash|v100"),
                (10, "gallery", 0, modern_gallery, "parsed", "divisare|modern-hash|v100"),
                (11, "gallery", 0, modern_gallery, "parsed", "divisare|modern-hash|v100"),
                (12, "gallery", 0, pdf_url, "parsed", "divisare|42|plan.pdf"),
                (13, "gallery", 0, video_endpoint, "parsed", "divisare|video-endpoint"),
                (14, "gallery", 0, disguised_video, "parsed", "divisare|43|movie.mp4"),
                (15, "gallery", 0, "not a URL", "malformed", None),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _make_architizer(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE image_assets(
              asset_id TEXT PRIMARY KEY,
              asset_key TEXT NOT NULL,
              normalized_url TEXT NOT NULL,
              host TEXT NOT NULL,
              path TEXT NOT NULL,
              is_placeholder_candidate INTEGER NOT NULL,
              asset_key_version TEXT NOT NULL
            ) STRICT;
            CREATE TABLE image_urls(
              image_url_id TEXT PRIMARY KEY,
              asset_id TEXT NOT NULL,
              raw_url TEXT NOT NULL,
              normalized_url TEXT NOT NULL,
              source_host TEXT NOT NULL
            ) STRICT;
            CREATE TABLE source_image_occurrences(
              occurrence_id TEXT PRIMARY KEY,
              source_project_id INTEGER NOT NULL,
              role TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              raw_url TEXT NOT NULL,
              image_url_id TEXT,
              asset_id TEXT,
              parse_status TEXT NOT NULL,
              parse_error TEXT,
              source_field TEXT NOT NULL,
              image_type TEXT
            ) STRICT;
            CREATE TABLE image_work_queue(
              asset_id TEXT PRIMARY KEY,
              phash_status TEXT NOT NULL,
              classification_status TEXT NOT NULL,
              queue_reason TEXT NOT NULL,
              network_calls_made INTEGER NOT NULL
            );
            CREATE TABLE project_image_global_id_occurrences(
              global_id_occurrence_id TEXT PRIMARY KEY,
              source_project_id INTEGER NOT NULL,
              ordinal INTEGER NOT NULL,
              raw_global_id TEXT NOT NULL
            );
            """
        )
        normalized = "http://architizer-prod.imgix.net/media/a.jpg"
        raw_large = (
            "http://architizer-prod.imgix.net/media/a.jpg?"
            "w=1680&q=60&auto=format%2Ccompress&s=identity-token"
        )
        raw_small = (
            "http://architizer-prod.imgix.net/media/a.jpg?"
            "w=800&s=identity-token"
        )
        placeholder = "https://facebook.com/static/placeholder.jpg"
        not_queued = "https://architizer-prod.imgix.net/media/not-queued.jpg"
        video = "https://architizer-prod.imgix.net/media/movie.mp4"
        conn.executemany(
            "INSERT INTO image_assets VALUES (?,?,?,?,?,?,?)",
            [
                ("atz_asset_a", "atz-key-a", normalized, "architizer-prod.imgix.net", "/media/a.jpg", 0, "architizer-host-path-asset-v1"),
                ("atz_asset_placeholder", "atz-key-placeholder", placeholder, "facebook.com", "/static/placeholder.jpg", 1, "architizer-host-path-asset-v1"),
                ("atz_asset_not_queued", "atz-key-not-queued", not_queued, "architizer-prod.imgix.net", "/media/not-queued.jpg", 0, "architizer-host-path-asset-v1"),
                ("atz_asset_video", "atz-key-video", video, "architizer-prod.imgix.net", "/media/movie.mp4", 0, "architizer-host-path-asset-v1"),
            ],
        )
        conn.executemany(
            "INSERT INTO image_urls VALUES (?,?,?,?,?)",
            [
                ("u-a-large", "atz_asset_a", raw_large, normalized, "architizer-prod.imgix.net"),
                ("u-a-small", "atz_asset_a", raw_small, normalized, "architizer-prod.imgix.net"),
                ("u-placeholder", "atz_asset_placeholder", placeholder, placeholder, "facebook.com"),
                ("u-not-queued", "atz_asset_not_queued", not_queued, not_queued, "architizer-prod.imgix.net"),
                ("u-video", "atz_asset_video", video, video, "architizer-prod.imgix.net"),
            ],
        )
        conn.executemany(
            "INSERT INTO source_image_occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("o1", 100, "cover", 0, raw_large, "u-a-large", "atz_asset_a", "parsed", None, "og:image:cover", None),
                ("o2", 100, "gallery", 0, raw_small, "u-a-small", "atz_asset_a", "parsed", None, "og:image:gallery", None),
                ("o3", 101, "gallery", 0, raw_large, "u-a-large", "atz_asset_a", "parsed", None, "og:image:gallery", None),
                ("op", 100, "gallery", 1, placeholder, "u-placeholder", "atz_asset_placeholder", "placeholder_candidate", None, "og:image:gallery", None),
                ("on", 102, "cover", 0, not_queued, "u-not-queued", "atz_asset_not_queued", "parsed", None, "og:image:cover", None),
                ("ov", 103, "gallery", 0, video, "u-video", "atz_asset_video", "parsed", None, "og:image:gallery", None),
            ],
        )
        conn.executemany(
            "INSERT INTO image_work_queue VALUES (?,?,?,?,?)",
            [
                ("atz_asset_a", "pending", "pending", "fixture", 0),
                ("atz_asset_placeholder", "pending", "pending", "fixture", 0),
                ("atz_asset_video", "pending", "pending", "fixture", 0),
            ],
        )
        # Deliberately unrelated to assets: adapters must not infer identity from it.
        conn.executemany(
            "INSERT INTO project_image_global_id_occurrences VALUES (?,?,?,?)",
            [
                ("g1", 100, 0, "images.image.shared"),
                ("g2", 102, 0, "images.image.shared"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_divisare_adapter_is_read_only_and_preserves_provenance(tmp_path: Path) -> None:
    path = tmp_path / "divisare.db"
    _make_divisare(path)
    before = _sha256(path)

    assets = list(iter_divisare_source_assets(path))

    assert [asset.source_asset_id for asset in assets] == [
        "divisare|42|plan.pdf",
        "divisare|modern-hash|v100",
    ]
    pdf, modern = assets
    assert pdf.source == "divisare"
    assert pdf.source_asset_key == pdf.source_asset_id
    assert pdf.format_lane == "convertible"
    assert pdf.fetch_profile_version == DIVISARE_FETCH_PROFILE_VERSION
    assert "/pg_1,c_limit,f_jpg,h_1024,q_85,w_1024/v1/" in pdf.effective_fetch_url
    assert pdf.roles == ("gallery",)
    assert pdf.occurrence_count == 1
    assert pdf.parent_count == 1

    assert modern.format_lane == "raster"
    assert modern.selected_raw_url.startswith(
        "https://images.divisare.com/image/upload/"
    )
    assert len(modern.source_urls) == 2
    assert modern.occurrence_count == 3
    assert modern.parent_count == 2
    assert modern.roles == ("cover", "gallery")
    assert "/c_limit,f_jpg,h_1024,q_85,w_1024/v100/" in modern.effective_fetch_url
    assert "w_auto" not in modern.effective_fetch_url

    assert _sha256(path) == before
    _assert_no_sqlite_sidecars(path)


def test_architizer_adapter_dedupes_asset_and_never_mutates_queue(tmp_path: Path) -> None:
    path = tmp_path / "architizer.db"
    _make_architizer(path)
    before = _sha256(path)

    assets = list(iter_architizer_source_assets(path))

    assert len(assets) == 2
    asset = next(item for item in assets if item.source_asset_id == "atz_asset_a")
    assert asset.source == "architizer"
    assert asset.source_asset_id == "atz_asset_a"
    assert asset.source_asset_key == "atz-key-a"
    assert asset.normalized_url.startswith("http://")
    assert len(asset.source_urls) == 2
    assert asset.occurrence_count == 3
    assert asset.parent_count == 2
    assert asset.roles == ("cover", "gallery")
    assert asset.format_lane == "raster"
    assert asset.fetch_profile_version == ARCHITIZER_FETCH_PROFILE_VERSION

    effective = urlsplit(asset.effective_fetch_url)
    params = parse_qs(effective.query)
    assert effective.scheme == "https"
    assert effective.hostname == "architizer-prod.imgix.net"
    assert params == {
        "auto": ["compress"],
        "fit": ["max"],
        "fm": ["jpg"],
        "h": ["1024"],
        "q": ["85"],
        "w": ["1024"],
    }
    assert "s" not in params  # Imgix signatures are invalid after transform changes.
    assert "crop" not in params

    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT asset_id,phash_status,network_calls_made "
            "FROM image_work_queue ORDER BY asset_id"
        ).fetchall() == [
            ("atz_asset_a", "pending", 0),
            ("atz_asset_placeholder", "pending", 0),
            ("atz_asset_video", "pending", 0),
        ]
    finally:
        conn.close()
    assert _sha256(path) == before
    _assert_no_sqlite_sidecars(path)


def test_fetch_url_helpers_reject_wrong_hosts_and_crop_is_absent() -> None:
    source = (
        "https://images.divisare.com/images/f_auto,q_auto,w_auto/"
        "v7/hash/name.jpg?source=kept#ignored"
    )
    effective = divisare_effective_fetch_url(source)
    assert effective == (
        "https://images.divisare.com/images/"
        "c_limit,f_jpg,h_1024,q_85,w_1024/v7/hash/name.jpg?source=kept"
    )
    with pytest.raises(ValueError):
        divisare_effective_fetch_url("https://example.com/image/upload/v1/a.jpg")
    with pytest.raises(ValueError):
        divisare_effective_fetch_url(
            "https://images.divisare.com/foo/images/f_auto/v1/a.jpg"
        )
    with pytest.raises(ValueError):
        architizer_effective_fetch_url("https://example.com/a.jpg")
    with pytest.raises(ValueError):
        architizer_effective_fetch_url("https://other.imgix.net/a.jpg")
    with pytest.raises(ValueError):
        architizer_effective_fetch_url(
            "https://architizer-prod.imgix.net:80/a.jpg"
        )
    with pytest.raises(ValueError):
        architizer_effective_fetch_url(
            "https://architizer-prod.imgix.net/a.jpg?s=stale-signature"
        )


def test_limit_validation_and_source_asset_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "divisare.db"
    _make_divisare(path)
    with pytest.raises(ValueError, match="positive"):
        list(iter_divisare_source_assets(path, limit=0))
    first = list(iter_divisare_source_assets(path, limit=1))[0]
    assert isinstance(first, SourceAsset)
    with pytest.raises(FrozenInstanceError):
        first.source = "changed"  # type: ignore[misc]
