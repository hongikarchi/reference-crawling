from __future__ import annotations

import hashlib
import io
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from PIL import Image

from canonical.divisare_image_smoke import (
    DEFAULT_PROFILE,
    KNOWN_SPLIT_PRIMARY_ASSET,
    KNOWN_SPLIT_PUBLIC_ID,
    KNOWN_SPLIT_SECONDARY_ASSET,
    PDF_PROFILE,
    FetchFailure,
    FetchPayload,
    fixed_derivative_url,
    run_smoke,
    select_stratified_sample,
)


def _source_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE image_assets(
          asset_key TEXT PRIMARY KEY,
          url_generation TEXT NOT NULL,
          original_filename TEXT
        );
        CREATE TABLE image_urls(
          url_id INTEGER PRIMARY KEY,
          asset_key TEXT NOT NULL,
          url TEXT NOT NULL UNIQUE,
          transform_signature TEXT,
          url_generation TEXT NOT NULL
        );
        CREATE TABLE article_image_occurrences(
          article_id INTEGER NOT NULL,
          role TEXT NOT NULL,
          position INTEGER NOT NULL,
          asset_key TEXT NOT NULL,
          url_id INTEGER NOT NULL
        );
        CREATE TABLE image_url_hints(asset_key TEXT NOT NULL, hint TEXT NOT NULL);
        """
    )


def _modern_key(number: int, *, version: int = 100) -> str:
    return f"divisare|modern-{number:04d}|v{version}"


def _modern_url(
    number: int,
    *,
    version: int = 100,
    suffix: str = "jpg",
    slug: str | None = None,
) -> str:
    return (
        "https://images.divisare.com/images/f_auto,q_auto,w_auto/"
        f"v{version}/modern-{number:04d}/"
        f"{slug or f'project-{number:04d}.{suffix}'}"
    )


def _known_split_url(version: int, *, cover: bool = False) -> str:
    if cover:
        return (
            "https://images.divisare.com/image/upload/c_fit,f_jpg,q_80,w_1200/"
            f"v{version}/{KNOWN_SPLIT_PUBLIC_ID}.jpg"
        )
    return (
        "https://images.divisare.com/images/f_auto,q_auto,w_auto/"
        f"v{version}/{KNOWN_SPLIT_PUBLIC_ID}/the-gyaan-center.jpg"
    )


def _legacy_url(number: int, filename: str, *, slug: str | None = None) -> str:
    return (
        "https://images.divisare.com/images/f_auto,q_auto,w_auto/v1/"
        f"project_images/{number}/{filename}/{slug or ('project-%04d.jpg' % number)}"
    )


def _make_source(path: Path) -> None:
    conn = sqlite3.connect(path)
    _source_schema(conn)
    next_url_id = 1
    next_article = 1

    def add(
        key: str,
        generation: str,
        filename: str | None,
        urls: list[str],
        *,
        roles: tuple[str, ...] = ("gallery",),
        role_url_indexes: tuple[int, ...] | None = None,
        hints: tuple[str, ...] = (),
    ) -> None:
        nonlocal next_url_id, next_article
        conn.execute(
            "INSERT INTO image_assets VALUES(?,?,?)", (key, generation, filename)
        )
        url_ids: list[int] = []
        for url in urls:
            url_ids.append(next_url_id)
            conn.execute(
                "INSERT INTO image_urls VALUES(?,?,?,?,?)",
                (
                    next_url_id,
                    key,
                    url,
                    "f_auto,q_auto,w_auto" if "/images/" in url else None,
                    generation,
                ),
            )
            next_url_id += 1
        indexes = role_url_indexes or tuple(0 for _ in roles)
        if len(indexes) != len(roles):
            raise ValueError("role_url_indexes must align with roles")
        for position, (role, url_index) in enumerate(zip(roles, indexes)):
            conn.execute(
                "INSERT INTO article_image_occurrences VALUES(?,?,?,?,?)",
                (next_article, role, position, key, url_ids[url_index]),
            )
        next_article += 1
        for hint in hints:
            conn.execute("INSERT INTO image_url_hints VALUES(?,?)", (key, hint))

    # Stable N10 strata.
    add(
        _modern_key(1),
        "cloudinary_public_id",
        None,
        [_modern_url(1)],
        roles=("cover", "gallery"),
    )
    add(
        _modern_key(2),
        "cloudinary_public_id",
        None,
        [_modern_url(2)],
    )
    add(
        "divisare|legacy-cover-gallery",
        "project_images",
        "cover.jpg",
        [_legacy_url(3, "cover.jpg")],
        roles=("cover", "gallery"),
    )
    add(
        "divisare|legacy-gallery-only",
        "project_images",
        "gallery.jpg",
        [_legacy_url(4, "gallery.jpg")],
    )
    add(
        "divisare|legacy-gif",
        "project_images",
        "animation.gif",
        [_legacy_url(5, "animation.gif")],
    )
    add(
        "divisare|legacy-pdf",
        "project_images",
        "plan.pdf",
        [_legacy_url(6, "plan.pdf")],
        hints=("drawing",),
    )
    add(
        "divisare|legacy-svg",
        "project_images",
        "section.svg",
        [_legacy_url(7, "section.svg")],
        hints=("section",),
    )
    add(
        "divisare|path|videos/upload/v1/clip.mp4",
        "path_fallback",
        "clip.mp4",
        ["https://images.divisare.com/videos/upload/v1/clip.mp4"],
    )
    long_slug = "project%20" + ("very-long-name-" * 12) + ".jpg"
    add(
        "divisare|long-percent",
        "project_images",
        "long name.jpg",
        [_legacy_url(8, "long%20name.jpg", slug=long_slug)],
    )
    add(
        KNOWN_SPLIT_PRIMARY_ASSET,
        "cloudinary_public_id",
        None,
        [
            _known_split_url(1678438203, cover=True),
            _known_split_url(1678438203),
        ],
        roles=("cover", "gallery"),
        role_url_indexes=(0, 1),
    )
    add(
        KNOWN_SPLIT_SECONDARY_ASSET,
        "cloudinary_public_id",
        None,
        [_known_split_url(1678438207)],
    )

    # Quota population: enough mutually exclusive rows for N100.
    for number in range(100, 170):
        add(
            _modern_key(number),
            "cloudinary_public_id",
            None,
            [_modern_url(number)],
            roles=("cover",) if number % 5 == 0 else ("gallery",),
        )
    for number in range(200, 245):
        add(
            f"divisare|legacy-{number}",
            "project_images",
            f"legacy-{number}.jpg",
            [_legacy_url(number, f"legacy-{number}.jpg")],
        )
    for number in range(300, 325):
        extension = "pdf" if number % 2 == 0 else "ai"
        filename = f"document-{number}.{extension}"
        add(
            f"divisare|document-{number}",
            "project_images",
            filename,
            [_legacy_url(number, filename)],
        )
    for number in range(400, 408):
        add(
            f"divisare|path|videos/upload/v1/movie-{number}.mp4",
            "path_fallback",
            "movie.mp4",
            [f"https://images.divisare.com/videos/upload/v1/movie-{number}.mp4"],
        )
    for number in range(500, 505):
        add(
            _modern_key(number, version=1),
            "cloudinary_public_id",
            None,
            [
                _modern_url(number, version=1, slug=f"project-{number}-a.jpg"),
                _modern_url(number, version=1, slug=f"project-{number}-b.jpg"),
                _modern_url(number, version=1, slug=f"project-{number}-c.jpg"),
            ],
            hints=("drawing", "section") if number == 500 else (),
        )
    conn.commit()
    conn.close()


def _image_bytes(seed: str, image_format: str = "JPEG") -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    image = Image.new("RGB", (48, 32), tuple(digest[:3]))
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, quality=80)
    return buffer.getvalue()


def _fake_origin_seed(url: str) -> str:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    for index, part in enumerate(parts):
        if part.startswith("v") and part[1:].isdigit() and index + 1 < len(parts):
            if parts[index + 1] == "project_images":
                return "/".join(parts[index:])
            public_id = parts[index + 1].split(".", 1)[0]
            return f"{part}|{public_id}"
    return url


def _fake_fetch(url: str, **_: object) -> FetchPayload:
    image_format = "PNG" if url.endswith(".png") else "JPEG"
    raw = _image_bytes(_fake_origin_seed(url), image_format)
    mime = "image/png" if image_format == "PNG" else "image/jpeg"
    return FetchPayload(raw=raw, http_status=200, mime_type=mime, final_url=url)


def test_fixed_derivative_url_replaces_or_inserts_transform() -> None:
    modern = "https://images.divisare.com//images/f_auto,q_auto,w_auto/v9/id/slug.jpg"
    assert fixed_derivative_url(modern, DEFAULT_PROFILE) == (
        "https://images.divisare.com/images/"
        f"{DEFAULT_PROFILE}/v9/id/slug.jpg"
    )
    no_transform = "https://images.divisare.com/images/v1/id/slug.jpg?x=1"
    assert fixed_derivative_url(no_transform, PDF_PROFILE) == (
        "https://images.divisare.com/images/"
        f"{PDF_PROFILE}/v1/id/slug.jpg?x=1"
    )
    with pytest.raises(ValueError, match="outside"):
        fixed_derivative_url("https://example.com/images/v1/a.jpg", DEFAULT_PROFILE)


def test_stratified_n10_is_prefix_of_n100(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_source(source)
    n10 = select_stratified_sample(source, 10)
    n100 = select_stratified_sample(source, 100)
    assert [row.asset_key for row in n100[:10]] == [row.asset_key for row in n10]
    assert n10[-1].asset_key == KNOWN_SPLIT_PRIMARY_ASSET
    assert n100[10].asset_key == KNOWN_SPLIT_SECONDARY_ASSET
    assert n100[10].selection_reason == "known_split_distinct_version"
    assert [row.selection_reason for row in n10] == [
        "modern_cover_gallery",
        "modern_gallery_only",
        "legacy_cover_gallery",
        "legacy_gallery_only",
        "legacy_png_or_gif",
        "pdf_first_page",
        "vector_or_layered_source",
        "hard_skip_resource",
        "percent_encoded_long_url",
        "known_split_same_version_duplicate",
    ]
    assert Counter(row.cohort for row in n100) == {
        "modern_raster": 50,
        "legacy_raster": 25,
        "convertible": 15,
        "hard_skip": 5,
        "edge": 5,
    }


def test_smoke_is_asset_keyed_resumable_and_split_identity_is_consistent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "n10.db"
    report = tmp_path / "n10.md"
    _make_source(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    result = run_smoke(
        source_db=source,
        output_db=output,
        report_path=report,
        cache_dir=tmp_path / "cache",
        limit=10,
        workers=2,
        fetcher=_fake_fetch,
        sleep=lambda _: None,
        max_attempts=1,
    )
    assert result["status"] == "complete"
    assert result["by_status"] == {"skipped": 1, "success": 9}
    assert result["identity_conflicts"] == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert output.exists() and report.exists()

    conn = sqlite3.connect(output)
    try:
        split_asset = conn.execute(
            """
            SELECT identity_status,distinct_pixel_sha_count,success_variant_count
            FROM image_asset_results WHERE asset_key=?
            """,
            (KNOWN_SPLIT_PRIMARY_ASSET,),
        ).fetchone()
        assert split_asset == ("consistent", 1, 2)
        assert conn.execute(
            """
            SELECT COUNT(DISTINCT normalized_pixel_sha256)
            FROM image_variant_results WHERE asset_key=? AND status='success'
            """,
            (KNOWN_SPLIT_PRIMARY_ASSET,),
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM validations WHERE severity='error' AND passed=0"
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM validations
            WHERE validation_name IN (
              'source_asset_single_delivery_version',
              'source_asset_key_delivery_version',
              'asset_identity_consistency'
            ) AND severity='error' AND passed=1
            """
        ).fetchone()[0] == 3
        successful_asset = conn.execute(
            "SELECT asset_key FROM image_variant_results WHERE status='success' LIMIT 1"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE image_variant_results SET phash_hex=NULL WHERE asset_key=?",
                (successful_asset,),
            )
        conn.rollback()
    finally:
        conn.close()

    calls = 0

    def should_not_fetch(url: str, **kwargs: object) -> FetchPayload:
        nonlocal calls
        calls += 1
        raise AssertionError((url, kwargs))

    resumed = run_smoke(
        source_db=source,
        output_db=output,
        report_path=report,
        cache_dir=tmp_path / "cache",
        limit=10,
        workers=2,
        resume=True,
        fetcher=should_not_fetch,
    )
    assert resumed["requests_made"] == 0
    assert calls == 0
    with pytest.raises(FileExistsError, match="immutable output"):
        run_smoke(
            source_db=source,
            output_db=output,
            report_path=report,
            cache_dir=None,
            limit=10,
            workers=1,
            fetcher=_fake_fetch,
        )


def test_smoke_rejects_colliding_output_and_report_paths(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    collision = tmp_path / "same-path"
    _make_source(source)
    with pytest.raises(ValueError, match="must be distinct"):
        run_smoke(
            source_db=source,
            output_db=collision,
            report_path=collision,
            cache_dir=None,
            limit=10,
            workers=1,
            fetcher=_fake_fetch,
        )


def test_smoke_rejects_asset_key_spanning_delivery_versions(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "n10.db"
    report = tmp_path / "n10.md"
    _make_source(source)
    conn = sqlite3.connect(source)
    try:
        next_url_id = conn.execute("SELECT MAX(url_id)+1 FROM image_urls").fetchone()[0]
        conn.execute(
            "INSERT INTO image_urls VALUES(?,?,?,?,?)",
            (
                next_url_id,
                KNOWN_SPLIT_PRIMARY_ASSET,
                _known_split_url(1678438211),
                "f_auto,q_auto,w_auto",
                "cloudinary_public_id",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = run_smoke(
        source_db=source,
        output_db=output,
        report_path=report,
        cache_dir=None,
        limit=10,
        workers=2,
        fetcher=_fake_fetch,
        sleep=lambda _: None,
        max_attempts=1,
    )
    assert result["status"] == "failed_validation"
    assert result["identity_conflicts"] == 1

    conn = sqlite3.connect(output)
    try:
        failures = dict(
            conn.execute(
                """
                SELECT validation_name,passed FROM validations
                WHERE validation_name IN (
                  'source_asset_single_delivery_version',
                  'source_asset_key_delivery_version',
                  'asset_identity_consistency'
                )
                """
            )
        )
        assert failures == {
            "asset_identity_consistency": 0,
            "source_asset_key_delivery_version": 0,
            "source_asset_single_delivery_version": 0,
        }
        assert conn.execute(
            "SELECT identity_status FROM image_asset_results WHERE asset_key=?",
            (KNOWN_SPLIT_PRIMARY_ASSET,),
        ).fetchone()[0] == "conflict"
    finally:
        conn.close()


def test_failed_request_does_not_shift_following_asset_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "n10.db"
    report = tmp_path / "n10.md"
    _make_source(source)
    sample = select_stratified_sample(source, 10)
    failed_key = sample[0].asset_key

    def fetch(url: str, **kwargs: object) -> FetchPayload:
        if "modern-0001" in url:
            raise FetchFailure("fixture_failure", "intentional")
        return _fake_fetch(url, **kwargs)

    result = run_smoke(
        source_db=source,
        output_db=output,
        report_path=report,
        cache_dir=None,
        limit=10,
        workers=2,
        fetcher=fetch,
        sleep=lambda _: None,
        max_attempts=1,
    )
    assert result["status"] == "failed_validation"
    conn = sqlite3.connect(output)
    try:
        assert conn.execute(
            "SELECT status FROM image_asset_results WHERE asset_key=?", (failed_key,)
        ).fetchone()[0] == "failed"
        following = sample[1]
        row = conn.execute(
            "SELECT status,phash_hex FROM image_asset_results WHERE asset_key=?",
            (following.asset_key,),
        ).fetchone()
        assert row[0] == "success"
        assert len(row[1]) == 64
        assert conn.execute(
            "SELECT COUNT(*) FROM image_variant_results WHERE asset_key=? AND selected_source_url LIKE '%modern-0001%'",
            (following.asset_key,),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="status mismatch"):
        run_smoke(
            source_db=source,
            output_db=output,
            report_path=report,
            cache_dir=None,
            limit=10,
            workers=2,
            resume=True,
            fetcher=_fake_fetch,
        )
