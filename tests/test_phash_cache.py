import json
import sqlite3
import time

from canonical import phash_cache


def _make_divisare_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE divisare_projects ("
        "id INTEGER PRIMARY KEY, "
        "cover_image_url TEXT, "
        "gallery_urls TEXT)"
    )
    conn.execute(
        "INSERT INTO divisare_projects VALUES (?, ?, ?)",
        (1, "https://fixture/cover.jpg", json.dumps([
            "https://fixture/gallery-1.jpg",
            "https://fixture/gallery-2.jpg",
            "https://fixture/gallery-3.jpg",
            "https://fixture/gallery-4.jpg",
        ])),
    )
    conn.execute(
        "INSERT INTO divisare_projects VALUES (?, ?, ?)",
        (2, None, None),
    )
    conn.execute(
        "INSERT INTO divisare_projects VALUES (?, ?, ?)",
        (3, "https://fixture/bad.jpg", json.dumps(["https://fixture/good.jpg"])),
    )
    conn.commit()
    conn.close()


def _make_many_divisare_rows(path, n_rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE divisare_projects ("
        "id INTEGER PRIMARY KEY, "
        "cover_image_url TEXT, "
        "gallery_urls TEXT)"
    )
    for i in range(n_rows):
        conn.execute(
            "INSERT INTO divisare_projects VALUES (?, ?, ?)",
            (
                i,
                f"https://fixture/{i}/cover.jpg",
                json.dumps([
                    f"https://fixture/{i}/gallery-1.jpg",
                    f"https://fixture/{i}/gallery-2.jpg",
                    f"https://fixture/{i}/gallery-3.jpg",
                ]),
            ),
        )
    conn.commit()
    conn.close()


def _write_canonical(path, source_ids):
    path.write_text(
        json.dumps({
            "buildings": [
                {
                    "canonical_bld_id": "bld_fixture",
                    "source_refs": {"divisare": [str(source_id) for source_id in source_ids]},
                }
            ]
        }),
        encoding="utf-8",
    )


def test_build_cache_from_fixture_db_and_resume(tmp_path):
    db_path = tmp_path / "divisare.db"
    cache_path = tmp_path / "phash_cache.json"
    progress_path = tmp_path / "phash_cache_progress.json"
    _make_divisare_db(db_path)

    phashes = {
        "https://fixture/cover.jpg": "0" * 64,
        "https://fixture/gallery-1.jpg": "1" * 64,
        "https://fixture/gallery-2.jpg": "2" * 64,
        "https://fixture/gallery-3.jpg": "3" * 64,
        "https://fixture/good.jpg": "4" * 64,
    }
    calls = []

    def fake_fetch(url, *, timeout):
        calls.append((url, timeout))
        value = phashes.get(url)
        if value is None:
            return None
        return {"phash": value, "w": 100, "h": 100, "bytes": 1000}

    spec = phash_cache.SourceSpec(
        name="divisare",
        db_path=db_path,
        table="divisare_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_urls",
    )

    summary = phash_cache.build_cache(
        source="divisare",
        workers=2,
        cache_path=cache_path,
        progress_path=progress_path,
        source_specs={"divisare": spec},
        fetcher=fake_fetch,
        write_every=1,
        canonical_only=False,
    )

    assert summary["rows_processed"] == 2
    assert all(timeout == 20 for _, timeout in calls)

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache == {
        "divisare:1": [
            phashes["https://fixture/cover.jpg"],
            phashes["https://fixture/gallery-1.jpg"],
            phashes["https://fixture/gallery-2.jpg"],
            phashes["https://fixture/gallery-3.jpg"],
        ],
        "divisare:3": [phashes["https://fixture/good.jpg"]],
    }

    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress == {"done": ["divisare:1", "divisare:3"]}

    calls.clear()
    resumed = phash_cache.build_cache(
        source="divisare",
        workers=2,
        cache_path=cache_path,
        progress_path=progress_path,
        source_specs={"divisare": spec},
        fetcher=fake_fetch,
        write_every=1,
        canonical_only=False,
    )

    assert resumed["rows_skipped_done"] == 2
    assert resumed["rows_processed"] == 0
    assert calls == []


def test_build_cache_uses_inter_row_parallelism(tmp_path):
    db_path = tmp_path / "divisare.db"
    cache_path = tmp_path / "phash_cache.json"
    progress_path = tmp_path / "phash_cache_progress.json"
    _make_many_divisare_rows(db_path, 50)

    def fake_fetch(url, *, timeout):
        time.sleep(0.05)
        return {"phash": "a" * 64, "w": 100, "h": 100, "bytes": 1000}

    spec = phash_cache.SourceSpec(
        name="divisare",
        db_path=db_path,
        table="divisare_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_urls",
    )

    start = time.monotonic()
    summary = phash_cache.build_cache(
        source="divisare",
        workers=32,
        cache_path=cache_path,
        progress_path=progress_path,
        source_specs={"divisare": spec},
        fetcher=fake_fetch,
        write_every=100,
        canonical_only=False,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 5
    assert summary["rows_processed"] == 50
    assert summary["urls_attempted"] == 200
    assert summary["phashes_written"] == 200
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(cache) == 50
    assert all(len(phashes) == 4 for phashes in cache.values())


def test_build_cache_canonical_only_filters_source_rows(tmp_path):
    db_path = tmp_path / "divisare.db"
    cache_path = tmp_path / "phash_cache.json"
    progress_path = tmp_path / "phash_cache_progress.json"
    canonical_path = tmp_path / "canonical_buildings_4source.json"
    _make_many_divisare_rows(db_path, 10)
    _write_canonical(canonical_path, [1, 4, 9])
    calls = []

    def fake_fetch(url, *, timeout):
        calls.append(url)
        return {"phash": "b" * 64, "w": 100, "h": 100, "bytes": 1000}

    spec = phash_cache.SourceSpec(
        name="divisare",
        db_path=db_path,
        table="divisare_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_urls",
    )

    summary = phash_cache.build_cache(
        source="divisare",
        workers=8,
        cache_path=cache_path,
        progress_path=progress_path,
        source_specs={"divisare": spec},
        fetcher=fake_fetch,
        canonical_only=True,
        canonical_path=canonical_path,
    )

    assert summary["rows_processed"] == 3
    assert summary["rows_skipped_not_in_canonical"] == 7
    assert len(calls) == 12

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert set(cache) == {"divisare:1", "divisare:4", "divisare:9"}
    assert set(progress["done"]) == {"divisare:1", "divisare:4", "divisare:9"}
