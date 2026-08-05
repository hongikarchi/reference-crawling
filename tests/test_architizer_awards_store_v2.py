"""Offline fixture tests for the immutable Architizer awards-v2 builder."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crawl.architizer import awards_store_v2 as store


SIDECAR_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    run_kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    source_db_path TEXT NOT NULL,
    source_db_sha256_before TEXT NOT NULL,
    source_db_sha256_after TEXT,
    source_db_size INTEGER NOT NULL,
    selected_count INTEGER DEFAULT 0,
    summary_json TEXT,
    error TEXT
);
CREATE TABLE http_attempts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    target_url TEXT,
    request_kind TEXT NOT NULL,
    requested_url TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    final_url TEXT,
    content_type TEXT,
    response_bytes INTEGER NOT NULL,
    sha256 TEXT,
    gzip_path TEXT,
    retryable INTEGER NOT NULL,
    block_signals_json TEXT NOT NULL,
    error TEXT
);
CREATE TABLE award_discoveries (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    award_year INTEGER NOT NULL,
    award_track TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    slug TEXT NOT NULL,
    source_url TEXT NOT NULL,
    discovered_url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(run_id,award_year,award_track,entity_type,slug,source_url)
);
"""


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def track_html(track: str, count: int = 6) -> str:
    cards = []
    for index in range(count):
        if track in {"Plus", "Sustainability", "Typology"}:
            subject_collection = "projects"
            prefixes = {
                "Plus": "",
                "Sustainability": "sustainability-",
                "Typology": "typology-",
            }
            prefix = prefixes[track]
            subject_slug = f"{prefix}project-{index}"
            subject_name = f"{track} Project {index}"
            company_collection = "firms"
            company_slug = f"{prefix}firm-{index}"
            company_name = f"{track} Firm {index}"
            tier_attr = "Jury Winner"
            badges = '<span class="badge">Jury Winner</span>'
            attribution_bases = {
                "Plus": 2000,
                "Sustainability": 3500,
                "Typology": 4000,
            }
            attribution_id = attribution_bases[track] + index
        elif track == "Firm":
            subject_collection = "firms"
            subject_slug = f"firm-subject-{index}"
            subject_name = f"Firm Winner {index}"
            company_collection = "brands"
            company_slug = f"firm-track-brand-{index}"
            company_name = f"Firm Track Brand {index}"
            tier_attr = "Jury Winner"
            badges = '<span class="badge">Jury Winner</span>'
            attribution_id = 1000 + index
        else:
            subject_collection = "products"
            subject_slug = f"product-{index}"
            subject_name = f"Product {index}"
            company_collection = "brands"
            company_slug = f"brand-{index}"
            company_name = f"Brand {index}"
            tier_attr = "Jury Winner,Popular Winner"
            badges = (
                '<span class="badge">Jury Winner</span>'
                '<span class="badge">Popular Choice Winner</span>'
            )
            attribution_id = 3000 + index
        cards.append(
            f"""
            <div class="winner-container col-12" data-types="{tier_attr}">
              <div class="awards">{badges}</div>
              <div class="winner card"
                   data-id="projects.awardattribution.{attribution_id}"
                   data-name="{subject_name}"
                   data-slug="{subject_slug}"
                   data-url="/{subject_collection}/{subject_slug}/"
                   data-description="Description {index}"
                   data-image="https://images.example/{subject_slug}.jpg?w=1680"
                   data-company-names='["{company_name}"]'
                   data-company-urls='["/{company_collection}/{company_slug}/"]'>
                <img data-src="https://images.example/{subject_slug}.jpg?w=388" />
                <a class="text-dark" href="https://architizer.com/{subject_collection}/{subject_slug}/">{subject_name}</a>
                <a href="https://architizer.com/{company_collection}/{company_slug}/">{company_name}</a>
              </div>
            </div>
            """
        )
    return f"""
    <!doctype html><html><head><title>2026 {track} Winners</title></head><body>
      <div class="container-fluid container-awards"><div class="row">
        <div class="col-12 group-title">Sample &gt; {track}</div>
        {''.join(cards)}
      </div></div>
    </body></html>
    """


def make_fixture(root: Path) -> tuple[Path, Path]:
    sidecar = root / "recrawl.db"
    snapshots = root / "snapshots"
    snapshots.mkdir()
    connection = sqlite3.connect(sidecar)
    try:
        connection.executescript(SIDECAR_SCHEMA)
        connection.execute(
            "INSERT INTO state_meta(key,value) VALUES ('schema_version',?)",
            (store.STATE_SCHEMA_VERSION,),
        )
        run_summary = {
            "award_year": 2026,
            "official_root": "https://winners.architizer.com/2026/",
            "tracks": ["Plus", "Products"],
            "track_urls": {
                "Plus": "https://winners.architizer.com/2026/Plus/",
                "Products": "https://winners.architizer.com/2026/Products/",
            },
        }
        source_sha = "A" * 64
        connection.execute(
            """
            INSERT INTO runs VALUES (
                7,'award_seed_census_2026','2026-08-01T00:00:00+00:00',
                '2026-08-01T00:01:00+00:00','completed','recrawl-fixture-v1',
                '{}','legacy.db',?,?,123,0,?,NULL
            )
            """,
            (source_sha, source_sha, json.dumps(run_summary)),
        )
        root_body = b"""
        <!doctype html><html><body>
          <a href="/2026/Plus/">Plus</a>
          <a href="https://winners.architizer.com/2026/Products/">Products</a>
        </body></html>
        """
        root_sha = hashlib.sha256(root_body).hexdigest().upper()
        root_relative = Path("awards") / root_sha[:2] / f"{root_sha}.html.gz"
        root_snapshot = snapshots / root_relative
        root_snapshot.parent.mkdir(parents=True, exist_ok=True)
        with gzip.GzipFile(
            filename=str(root_snapshot), mode="wb", mtime=0
        ) as handle:
            handle.write(root_body)
        root_url = "https://winners.architizer.com/2026/"
        connection.execute(
            """
            INSERT INTO http_attempts VALUES (
                10,7,NULL,'award_year_root',?,1,
                '2026-08-01T00:00:00+00:00','2026-08-01T00:00:01+00:00',
                100,'success',200,?,'text/html',?,?,?,0,'[]',NULL
            )
            """,
            (
                root_url,
                root_url,
                len(root_body),
                root_sha,
                str(root_relative).replace("\\", "/"),
            ),
        )
        for offset, track in enumerate(("Plus", "Products"), 1):
            body = track_html(track).encode("utf-8")
            content_sha = hashlib.sha256(body).hexdigest().upper()
            relative = Path("awards") / content_sha[:2] / f"{content_sha}.html.gz"
            snapshot = snapshots / relative
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            with gzip.GzipFile(filename=str(snapshot), mode="wb", mtime=0) as handle:
                handle.write(body)
            url = f"https://winners.architizer.com/2026/{track}/"
            connection.execute(
                """
                INSERT INTO http_attempts VALUES (
                    ?,7,NULL,'award_track_root',?,1,
                    '2026-08-01T00:00:00+00:00','2026-08-01T00:00:01+00:00',
                    100,'success',200,?,'text/html',?,?,?,0,'[]',NULL
                )
                """,
                (
                    10 + offset,
                    url,
                    url,
                    len(body),
                    content_sha,
                    str(relative).replace("\\", "/"),
                ),
            )
            if track == "Plus":
                for index in range(6):
                    for entity_type in ("project", "firm"):
                        slug = f"{entity_type}-{index}"
                        connection.execute(
                            """
                            INSERT INTO award_discoveries VALUES (
                                7,2026,?,?,?,?,?,
                                '2026-08-01T00:00:01+00:00'
                            )
                            """,
                            (
                                track,
                                entity_type,
                                slug,
                                url,
                                f"https://architizer.com/{entity_type}s/{slug}/",
                            ),
                        )
        connection.commit()
    finally:
        connection.close()
    return sidecar, snapshots


def rewrite_snapshot(
    sidecar: Path,
    snapshots: Path,
    attempt_id: int,
    transform,
) -> None:
    connection = sqlite3.connect(sidecar)
    connection.row_factory = sqlite3.Row
    try:
        attempt = connection.execute(
            "SELECT gzip_path FROM http_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        snapshot = snapshots / attempt["gzip_path"]
        with gzip.open(snapshot, "rb") as handle:
            body = handle.read()
        body = transform(body)
        with gzip.GzipFile(filename=str(snapshot), mode="wb", mtime=0) as handle:
            handle.write(body)
        content_sha = hashlib.sha256(body).hexdigest().upper()
        connection.execute(
            "UPDATE http_attempts SET response_bytes=?,sha256=? WHERE id=?",
            (len(body), content_sha, attempt_id),
        )
        connection.commit()
    finally:
        connection.close()


def reseal_output_snapshot_manifest(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    pages = connection.execute(
        "SELECT * FROM award_page_versions ORDER BY id"
    ).fetchall()
    manifest_bytes = store._snapshot_manifest_bytes_from_output_pages(pages)
    metadata = json.loads(
        connection.execute("SELECT metadata_json FROM input_lineage").fetchone()[0]
    )
    metadata["snapshot_manifest_size_bytes"] = len(manifest_bytes)
    metadata["distinct_physical_snapshot_count"] = len(
        {
            (page["snapshot_gzip_path"], page["snapshot_gzip_sha256"])
            for page in pages
        }
    )
    connection.execute(
        "UPDATE input_lineage SET snapshot_manifest_sha256=?,metadata_json=?",
        (
            hashlib.sha256(manifest_bytes).hexdigest().upper(),
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        ),
    )


def add_typology_exact_snapshot(
    sidecar: Path,
    snapshots: Path,
    *,
    reuse_verified_root_snapshot: bool,
) -> None:
    typology_url = "https://winners.architizer.com/2026/Typology/"
    typology_body = track_html("Typology").encode("utf-8")
    root_body = typology_body.replace(
        b"</body>",
        (
            b'<a href="/2026/Plus/">Plus</a>'
            b'<a href="/2026/Products/">Products</a>'
            b'<a href="/2026/Typology/">Typology</a></body>'
        ),
        1,
    )
    rewrite_snapshot(sidecar, snapshots, 10, lambda _: root_body)

    connection = sqlite3.connect(sidecar)
    connection.row_factory = sqlite3.Row
    try:
        root_attempt = connection.execute(
            "SELECT response_bytes,sha256,gzip_path FROM http_attempts WHERE id=10"
        ).fetchone()
        if reuse_verified_root_snapshot:
            response_bytes = root_attempt["response_bytes"]
            content_sha = root_attempt["sha256"]
            relative = root_attempt["gzip_path"]
        else:
            response_bytes = len(typology_body)
            content_sha = hashlib.sha256(typology_body).hexdigest().upper()
            relative_path = (
                Path("awards") / content_sha[:2] / f"{content_sha}.html.gz"
            )
            snapshot = snapshots / relative_path
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            with gzip.GzipFile(filename=str(snapshot), mode="wb", mtime=0) as handle:
                handle.write(typology_body)
            relative = str(relative_path).replace("\\", "/")

        run_summary = json.loads(
            connection.execute("SELECT summary_json FROM runs WHERE id=7").fetchone()[0]
        )
        run_summary["tracks"].append("Typology")
        run_summary["track_urls"]["Typology"] = typology_url
        connection.execute(
            "UPDATE runs SET summary_json=? WHERE id=7",
            (json.dumps(run_summary),),
        )
        connection.execute(
            """
            INSERT INTO http_attempts VALUES (
                13,7,NULL,'award_track_root',?,1,
                '2026-08-01T00:00:00+00:00','2026-08-01T00:00:01+00:00',
                100,'success',200,?,'text/html',?,?,?,0,'[]',NULL
            )
            """,
            (
                typology_url,
                typology_url,
                response_bytes,
                content_sha,
                relative,
            ),
        )
        for index in range(6):
            for entity_type in ("project", "firm"):
                slug = f"typology-{entity_type}-{index}"
                connection.execute(
                    """
                    INSERT INTO award_discoveries VALUES (
                        7,2026,'Typology',?,?,?,?,
                        '2026-08-01T00:00:01+00:00'
                    )
                    """,
                    (
                        entity_type,
                        slug,
                        typology_url,
                        f"https://architizer.com/{entity_type}s/{slug}/",
                    ),
                )
        connection.commit()
    finally:
        connection.close()


RUN4_CONTRACT = (
    Path(__file__).parent
    / "fixtures"
    / "architizer_awards_run4_contract.json"
)


def make_run4_five_track_fixture(root: Path) -> tuple[Path, Path, dict]:
    """Create a sanitized DB-contract equivalent of the observed run 4."""

    contract = json.loads(RUN4_CONTRACT.read_text(encoding="utf-8"))
    run_id = int(contract["run_id"])
    award_year = int(contract["award_year"])
    tracks = list(contract["tracks"])
    count = int(contract["records_per_track"])
    sidecar = root / "architizer_source_recrawl_v2.db"
    snapshots = root / "architizer_html_snapshots_v2"
    snapshots.mkdir()
    root_url = f"https://winners.architizer.com/{award_year}/"
    track_urls = {
        track: f"https://winners.architizer.com/{award_year}/{track}/"
        for track in tracks
    }
    links = "".join(
        f'<a href="/{award_year}/{track}/">{track}</a>' for track in tracks
    )
    typology_body = track_html("Typology", count).replace(
        "</body>", f"{links}</body>", 1
    ).encode("utf-8")

    def save_snapshot(body: bytes) -> tuple[int, str, str]:
        content_sha = hashlib.sha256(body).hexdigest().upper()
        relative = Path("awards") / content_sha[:2] / f"{content_sha}.html.gz"
        path = snapshots / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as handle:
                handle.write(body)
        return len(body), content_sha, relative.as_posix()

    root_snapshot = save_snapshot(typology_body)
    connection = sqlite3.connect(sidecar)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SIDECAR_SCHEMA)
        connection.execute(
            "INSERT INTO state_meta(key,value) VALUES ('schema_version',?)",
            (store.STATE_SCHEMA_VERSION,),
        )
        summary = {
            "award_year": award_year,
            "official_root": root_url,
            "tracks": tracks,
            "track_urls": track_urls,
        }
        source_sha = "4" * 64
        connection.execute(
            """
            INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                run_id,
                f"award_seed_census_{award_year}",
                "2026-08-02T11:40:00+00:00",
                contract["finished_at"],
                "completed",
                contract["parser_version"],
                "{}",
                "data/crawl/architizer.db",
                source_sha,
                source_sha,
                123456,
                0,
                json.dumps(summary, sort_keys=True),
            ),
        )
        connection.execute(
            """
            INSERT INTO http_attempts VALUES (
                40,?,NULL,'award_year_root',?,1,?,?,100,'success',200,?,
                'text/html; charset=utf-8',?,?,?,0,'[]',NULL
            )
            """,
            (
                run_id,
                root_url,
                "2026-08-02T11:40:00+00:00",
                "2026-08-02T11:40:01+00:00",
                root_url,
                *root_snapshot,
            ),
        )
        for offset, track in enumerate(tracks, 1):
            body = typology_body if track == "Typology" else track_html(track, count).encode(
                "utf-8"
            )
            snapshot = root_snapshot if track == "Typology" else save_snapshot(body)
            attempt_id = 40 + offset
            connection.execute(
                """
                INSERT INTO http_attempts VALUES (
                    ?,?,NULL,'award_track_root',?,1,?,?,100,'success',200,?,
                    'text/html; charset=utf-8',?,?,?,0,'[]',NULL
                )
                """,
                (
                    attempt_id,
                    run_id,
                    track_urls[track],
                    "2026-08-02T11:40:00+00:00",
                    "2026-08-02T11:40:01+00:00",
                    track_urls[track],
                    *snapshot,
                ),
            )
            parsed = store.parse_awards_track_snapshot(
                body.decode("utf-8"),
                source_url=track_urls[track],
                award_year=award_year,
                award_track=track,
            )
            discoveries = set()
            for record in parsed["records"]:
                values = [record.get("subject"), *(record.get("companies") or [])]
                for value in values:
                    if value and value["kind"] in {"project", "firm"}:
                        discoveries.add(
                            (value["kind"], value["slug"], value["url"])
                        )
            for entity_type, slug, discovered_url in sorted(discoveries):
                connection.execute(
                    """
                    INSERT INTO award_discoveries VALUES (
                        ?,?,?,?,?,?,?,'2026-08-02T11:40:01+00:00'
                    )
                    """,
                    (
                        run_id,
                        award_year,
                        track,
                        entity_type,
                        slug,
                        track_urls[track],
                        discovered_url,
                    ),
                )
        connection.commit()
    finally:
        connection.close()
    return sidecar, snapshots, contract


class ArchitizerAwardsStoreV2Tests(unittest.TestCase):
    def test_n10_build_preserves_lineage_and_product_brand_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards-n10.db"
            sidecar_before = file_sha(sidecar)
            snapshot_before = {
                path.relative_to(snapshots): file_sha(path)
                for path in snapshots.rglob("*.gz")
            }

            result = store.build_awards_database(
                sidecar_path=sidecar,
                snapshot_root=snapshots,
                output_path=output,
                run_id=7,
                limit=10,
            )

            self.assertEqual(result["selected_record_count"], 10)
            self.assertEqual(result["source_record_count"], 12)
            self.assertEqual(result["page_count"], 3)
            self.assertEqual(result["track_page_count"], 2)
            self.assertEqual(result["subject_counts"], {"product": 5, "project": 5})
            self.assertEqual(result["company_counts"], {"brand": 5, "firm": 5})
            self.assertEqual(result["validation"]["quick_check"], "ok")
            ready = Path(result["ready_path"])
            self.assertTrue(ready.is_file())
            receipt = json.loads(ready.read_text(encoding="utf-8"))
            self.assertEqual(receipt["database"]["sha256"], file_sha(output))
            self.assertEqual(receipt["input_sidecar"]["sha256_before"], sidecar_before)
            self.assertEqual(receipt["recrawl_run"]["id"], 7)
            self.assertEqual(
                receipt["snapshot_manifest"]["page_version_count"], 3
            )
            self.assertEqual(
                receipt["snapshot_manifest"]["distinct_physical_snapshot_count"],
                3,
            )
            self.assertEqual(file_sha(sidecar), sidecar_before)
            self.assertEqual(
                {
                    path.relative_to(snapshots): file_sha(path)
                    for path in snapshots.rglob("*.gz")
                },
                snapshot_before,
            )

            connection = sqlite3.connect(
                output.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
            connection.row_factory = sqlite3.Row
            try:
                lineage = connection.execute("SELECT * FROM input_lineage").fetchone()
                manifest = connection.execute("SELECT * FROM build_manifest").fetchone()
                page_counts = {
                    row["award_track"]: row["selected_record_count"]
                    for row in connection.execute(
                        "SELECT award_track,selected_record_count "
                        "FROM award_page_versions WHERE page_kind='track'"
                    )
                }
                root_page = connection.execute(
                    "SELECT * FROM award_page_versions WHERE page_kind='year_root'"
                ).fetchone()
                subject_counts = dict(
                    connection.execute(
                        "SELECT subject_kind,COUNT(*) FROM award_attributions "
                        "GROUP BY subject_kind"
                    ).fetchall()
                )
                company_counts = dict(
                    connection.execute(
                        "SELECT entity_kind,COUNT(*) "
                        "FROM award_attribution_companies GROUP BY entity_kind"
                    ).fetchall()
                )
                tier_count = connection.execute(
                    "SELECT COUNT(*) FROM award_attribution_tiers"
                ).fetchone()[0]
                raw_evidence = json.loads(
                    connection.execute(
                        "SELECT raw_attributes_json FROM award_attributions "
                        "ORDER BY selection_order LIMIT 1"
                    ).fetchone()[0]
                )
                policies = {
                    row["entity_kind"]: dict(row)
                    for row in connection.execute("SELECT * FROM corpus_projection_policy")
                }
            finally:
                connection.close()

            self.assertEqual(
                lineage["sqlite_open_mode"], "mode=ro&immutable=1;query_only=ON"
            )
            self.assertEqual(lineage["sidecar_sha256_before"], sidecar_before)
            self.assertEqual(lineage["sidecar_sha256_after"], sidecar_before)
            self.assertEqual(lineage["selected_snapshot_count"], 3)
            self.assertEqual(manifest["build_limit"], 10)
            self.assertEqual(manifest["is_full_snapshot_projection"], 0)
            self.assertEqual(page_counts, {"Plus": 5, "Products": 5})
            self.assertIsNone(root_page["award_track"])
            self.assertEqual(root_page["source_record_count"], 0)
            self.assertEqual(subject_counts, {"product": 5, "project": 5})
            self.assertEqual(company_counts, {"brand": 5, "firm": 5})
            self.assertEqual(tier_count, 15)
            self.assertEqual(raw_evidence["card"]["data-types"], "Jury Winner")
            self.assertIn("data-id", raw_evidence["winner"])
            connection = sqlite3.connect(
                output.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
            try:
                duplicate_ordinals = connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT page_version_id,source_group_ordinal,
                               source_card_ordinal,COUNT(*) AS n
                        FROM award_attributions
                        GROUP BY page_version_id,source_group_ordinal,
                                 source_card_ordinal
                        HAVING n > 1
                    )
                    """
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(duplicate_ordinals, 0)
            self.assertEqual(policies["product"]["preserve_in_source_corpus"], 1)
            self.assertIn(
                "excluded",
                policies["product"]["project_firm_curated_projection"],
            )
            self.assertEqual(policies["brand"]["preserve_in_source_corpus"], 1)

    def test_attribution_parent_page_parity_is_enforced_and_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards-parent-parity.db"
            store.build_awards_database(
                sidecar_path=sidecar,
                snapshot_root=snapshots,
                output_path=output,
                run_id=7,
                limit=10,
            )
            connection = sqlite3.connect(output)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                attribution_id = int(
                    connection.execute(
                        "SELECT id FROM award_attributions ORDER BY id LIMIT 1"
                    ).fetchone()[0]
                )
                page_id = int(
                    connection.execute(
                        "SELECT page_version_id FROM award_attributions WHERE id=?",
                        (attribution_id,),
                    ).fetchone()[0]
                )
                for statement in (
                    "UPDATE award_attributions SET award_year=2025 WHERE id=?",
                    "UPDATE award_attributions SET award_track='Firm' WHERE id=?",
                    "UPDATE award_attributions SET source_url='https://example.invalid/' WHERE id=?",
                ):
                    with self.subTest(statement=statement):
                        with self.assertRaisesRegex(
                            sqlite3.IntegrityError, "parent parity mismatch"
                        ):
                            connection.execute(statement, (attribution_id,))
                        connection.rollback()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "child parity mismatch"
                ):
                    connection.execute(
                        "UPDATE award_page_versions SET award_track='Firm' WHERE id=?",
                        (page_id,),
                    )
                connection.rollback()

                # Simulate a corrupt producer that removed the trigger and
                # re-sealed the DB; the immutable output validator must still
                # derive parity from stored rows and reject it.
                connection.execute(
                    "DROP TRIGGER award_attributions_parent_parity_update"
                )
                connection.execute(
                    "UPDATE award_attributions SET award_track='Firm' WHERE id=?",
                    (attribution_id,),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(
                store.AwardsBuildError, "parent parity mismatch"
            ):
                store._validate_output(output, 10)

    def test_output_validator_recomputes_snapshot_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards-manifest-parity.db"
            store.build_awards_database(
                sidecar_path=sidecar,
                snapshot_root=snapshots,
                output_path=output,
                run_id=7,
                limit=10,
            )
            connection = sqlite3.connect(output)
            try:
                connection.execute(
                    "UPDATE award_page_versions "
                    "SET snapshot_gzip_path='tampered.html.gz' "
                    "WHERE page_kind='track' AND id=("
                    "SELECT MIN(id) FROM award_page_versions WHERE page_kind='track'"
                    ")"
                )
                connection.commit()
                connection.execute("VACUUM")
            finally:
                connection.close()
            with self.assertRaisesRegex(
                store.AwardsBuildError, "snapshot manifest evidence mismatch"
            ):
                store._validate_output(output, 10)

    def test_release_validator_rejects_resealed_typology_contract_tampering(self) -> None:
        cases = (
            (
                "final-url",
                "UPDATE award_page_versions SET final_url=? WHERE award_track='Typology'",
                ("https://winners.architizer.com/2026/",),
                "track exact final-URL mismatch",
            ),
            (
                "final-url-policy",
                "UPDATE award_page_versions SET final_url_policy=? "
                "WHERE award_track='Typology'",
                ("official_year_root_alias_verified",),
                "track exact final-URL mismatch",
            ),
            (
                "dedupe-path",
                "UPDATE award_page_versions SET snapshot_gzip_path=? "
                "WHERE award_track='Typology'",
                ("awards/copied-identical-snapshot.html.gz",),
                "Typology deduplicated year-root snapshot mismatch",
            ),
            (
                "manifest-alias-claim",
                None,
                (),
                "exact final-URL policy contract mismatch",
            ),
        )
        for name, sql, params, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                sidecar, snapshots, _ = make_run4_five_track_fixture(root)
                output = root / "architizer_awards_v2.db"
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=4,
                )
                connection = sqlite3.connect(output)
                try:
                    if sql is not None:
                        connection.execute(sql, params)
                    else:
                        summary = json.loads(
                            connection.execute(
                                "SELECT summary_json FROM build_manifest"
                            ).fetchone()[0]
                        )
                        summary["root_alias_tracks"] = ["Typology"]
                        connection.execute(
                            "UPDATE build_manifest SET summary_json=?",
                            (
                                json.dumps(
                                    summary, sort_keys=True, separators=(",", ":")
                                ),
                            ),
                        )
                    reseal_output_snapshot_manifest(connection)
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaisesRegex(store.AwardsBuildError, message):
                    store._validate_output(output, 10)

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            store.build_awards_database(
                sidecar_path=sidecar,
                snapshot_root=snapshots,
                output_path=output,
                run_id=7,
                limit=10,
            )
            before = file_sha(output)

            with self.assertRaisesRegex(store.AwardsBuildError, "already exists"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

            self.assertEqual(file_sha(output), before)

    def test_snapshot_sha_mismatch_aborts_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            connection = sqlite3.connect(sidecar)
            try:
                connection.execute("UPDATE http_attempts SET sha256=? WHERE id=10", ("0" * 64,))
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(store.AwardsBuildError, "SHA mismatch"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

            self.assertFalse(output.exists())

    def test_sidecar_schema_and_foreign_key_integrity_are_hard_gates(self) -> None:
        cases = (
            (
                "schema",
                "UPDATE state_meta SET value='unsupported' "
                "WHERE key='schema_version'",
                "unsupported recrawl sidecar schema version",
            ),
            (
                "foreign_key",
                "UPDATE http_attempts SET run_id=999 WHERE id=11",
                "foreign_key_check failed",
            ),
        )
        for name, sql, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                sidecar, snapshots = make_fixture(root)
                connection = sqlite3.connect(sidecar)
                try:
                    connection.execute("PRAGMA foreign_keys=OFF")
                    connection.execute(sql)
                    connection.commit()
                finally:
                    connection.close()
                output = root / "awards.db"
                with self.assertRaisesRegex(store.AwardsBuildError, message):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=output,
                        run_id=7,
                        limit=10,
                    )
                self.assertFalse(output.exists())
                self.assertFalse(store._ready_path(output).exists())

    def test_malformed_sidecar_is_rejected_by_sqlite_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            sidecar.write_bytes(b"not-a-sqlite-database")
            with self.assertRaisesRegex(store.AwardsBuildError, "SQLite validation failed"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=root / "awards.db",
                    run_id=7,
                    limit=10,
                )
            self.assertFalse(Path(str(sidecar) + ".lock").exists())

    def test_active_sidecar_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            script = (
                "import sys\n"
                "from pathlib import Path\n"
                "from crawl.architizer.recrawl_v2 import SidecarLock\n"
                "with SidecarLock(Path(sys.argv[1])):\n"
                "    print('LOCKED', flush=True)\n"
                "    sys.stdin.readline()\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(sidecar)],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "LOCKED")
                with self.assertRaisesRegex(store.AwardsBuildError, "lock.*exists"):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=output,
                        run_id=7,
                        limit=10,
                    )
            finally:
                process.stdin.write("release\n")
                process.stdin.flush()
                _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)

            self.assertFalse(output.exists())

    def test_sidecar_lock_spans_page_load_and_is_cleaned_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            lock_path = Path(str(sidecar) + ".lock")
            original_load_pages = store._load_pages

            def assert_lock_then_load(*args, **kwargs):
                self.assertTrue(lock_path.is_file())
                return original_load_pages(*args, **kwargs)

            with mock.patch.object(
                store, "_load_pages", side_effect=assert_lock_then_load
            ):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )
            self.assertFalse(lock_path.exists())

            failure_root = root / "failure"
            failure_root.mkdir()
            sidecar_two, snapshots_two = make_fixture(failure_root)
            failed_output = root / "failed.db"
            with mock.patch.object(
                store,
                "_load_pages",
                side_effect=store.AwardsBuildError("fixture page-load failure"),
            ):
                with self.assertRaisesRegex(store.AwardsBuildError, "page-load failure"):
                    store.build_awards_database(
                        sidecar_path=sidecar_two,
                        snapshot_root=snapshots_two,
                        output_path=failed_output,
                        run_id=7,
                        limit=10,
                    )
            self.assertFalse(Path(str(sidecar_two) + ".lock").exists())
            self.assertFalse(failed_output.exists())
            self.assertFalse(store._ready_path(failed_output).exists())

    def test_sidecar_wal_appearing_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            original_load_pages = store._load_pages

            def load_pages_then_create_wal(*args, **kwargs):
                result = original_load_pages(*args, **kwargs)
                Path(str(sidecar) + "-wal").write_bytes(b"writer appeared")
                return result

            with mock.patch.object(
                store,
                "_load_pages",
                side_effect=load_pages_then_create_wal,
            ):
                with self.assertRaisesRegex(
                    store.AwardsBuildError, "SQLite sidecars"
                ):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=output,
                        run_id=7,
                        limit=10,
                    )

            self.assertFalse(output.exists())

    def test_discovery_card_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            connection = sqlite3.connect(sidecar)
            try:
                connection.execute(
                    "UPDATE award_discoveries SET award_track='Products', "
                    "source_url='https://winners.architizer.com/2026/Products/' "
                    "WHERE entity_type='project' AND slug='project-0'"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(store.AwardsBuildError, "discovery tuples differ"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

            self.assertFalse(output.exists())

    def test_discovery_rows_require_exact_year_slug_source_and_uniqueness(self) -> None:
        mutations = (
            (
                "wrong_year",
                "UPDATE award_discoveries SET award_year=2025 "
                "WHERE entity_type='project' AND slug='project-0'",
                "year differs",
            ),
            (
                "wrong_slug",
                "UPDATE award_discoveries SET slug='wrong-slug' "
                "WHERE entity_type='project' AND slug='project-0'",
                "slug differs",
            ),
            (
                "wrong_source",
                "UPDATE award_discoveries SET source_url="
                "'https://winners.architizer.com/2026/Products/' "
                "WHERE entity_type='project' AND slug='project-0'",
                "source URL differs",
            ),
            (
                "duplicate_url",
                "INSERT INTO award_discoveries VALUES ("
                "7,2026,'Plus','project','duplicate-project-0',"
                "'https://winners.architizer.com/2026/Plus/',"
                "'https://architizer.com/projects/project-0/',"
                "'2026-08-01T00:00:02+00:00')",
                "duplicate discovered URL",
            ),
        )
        for name, sql, message in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                sidecar, snapshots = make_fixture(root)
                connection = sqlite3.connect(sidecar)
                try:
                    connection.execute(sql)
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaisesRegex(store.AwardsBuildError, message):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=root / "awards.db",
                        run_id=7,
                        limit=10,
                    )

    def test_track_login_block_non_html_and_wrong_redirect_are_rejected(self) -> None:
        cases = [
            (
                "login",
                "UPDATE http_attempts SET block_signals_json='[\"login_wall\"]' WHERE id=11",
                "block/login signals",
            ),
            (
                "non_html",
                "UPDATE http_attempts SET content_type='application/json' WHERE id=11",
                "non-HTML",
            ),
            (
                "redirect",
                "UPDATE http_attempts SET final_url='https://winners.architizer.com/login/' WHERE id=11",
                "final URL mismatch",
            ),
            (
                "year_root_redirect_on_track",
                "UPDATE http_attempts SET final_url='https://winners.architizer.com/2026/' WHERE id=11",
                "final URL mismatch",
            ),
        ]
        for name, sql, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                sidecar, snapshots = make_fixture(root)
                output = root / "awards.db"
                connection = sqlite3.connect(sidecar)
                try:
                    connection.execute(sql)
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaisesRegex(store.AwardsBuildError, message):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=output,
                        run_id=7,
                        limit=10,
                    )
                self.assertFalse(output.exists())

    def test_typology_exact_url_requires_deduplicated_year_root_snapshot(self) -> None:
        for reuse_root, should_succeed in ((True, True), (False, False)):
            with self.subTest(reuse_root=reuse_root), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                sidecar, snapshots = make_fixture(root)
                output = root / "awards.db"
                add_typology_exact_snapshot(
                    sidecar,
                    snapshots,
                    reuse_verified_root_snapshot=reuse_root,
                )
                if not should_succeed:
                    with self.assertRaisesRegex(
                        store.AwardsBuildError,
                        "deduplicated year-root snapshot differs",
                    ):
                        store.build_awards_database(
                            sidecar_path=sidecar,
                            snapshot_root=snapshots,
                            output_path=output,
                            run_id=7,
                            limit=12,
                        )
                    self.assertFalse(output.exists())
                    continue

                result = store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=12,
                )
                self.assertEqual(result["root_alias_tracks"], [])
                connection = sqlite3.connect(
                    output.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
                )
                connection.row_factory = sqlite3.Row
                try:
                    typology = connection.execute(
                        "SELECT * FROM award_page_versions WHERE award_track='Typology'"
                    ).fetchone()
                    year_root = connection.execute(
                        "SELECT * FROM award_page_versions WHERE page_kind='year_root'"
                    ).fetchone()
                    summary = json.loads(
                        connection.execute(
                            "SELECT summary_json FROM build_manifest"
                        ).fetchone()[0]
                    )
                finally:
                    connection.close()
                self.assertEqual(
                    typology["final_url"],
                    "https://winners.architizer.com/2026/Typology/",
                )
                self.assertEqual(typology["final_url_policy"], "exact")
                self.assertEqual(
                    typology["snapshot_content_sha256"],
                    year_root["snapshot_content_sha256"],
                )
                self.assertEqual(
                    typology["snapshot_gzip_path"], year_root["snapshot_gzip_path"]
                )
                self.assertEqual(summary["root_alias_tracks"], [])

    def test_typology_year_root_redirect_is_rejected_despite_snapshot_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            add_typology_exact_snapshot(
                sidecar,
                snapshots,
                reuse_verified_root_snapshot=True,
            )
            connection = sqlite3.connect(sidecar)
            try:
                connection.execute(
                    "UPDATE http_attempts SET final_url=? WHERE id=13",
                    ("https://winners.architizer.com/2026/",),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(store.AwardsBuildError, "final URL mismatch"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=12,
                )
            self.assertFalse(output.exists())

    def test_sanitized_run4_five_track_exact_url_and_dedupe_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots, contract = make_run4_five_track_fixture(root)
            output = root / "architizer_awards_v2.db"

            result = store.build_awards_database(
                sidecar_path=sidecar,
                snapshot_root=snapshots,
                output_path=output,
                run_id=4,
            )

            self.assertEqual(result["recrawl_run_id"], contract["run_id"])
            self.assertEqual(result["track_page_count"], 5)
            self.assertEqual(result["page_count"], 6)
            self.assertEqual(result["source_record_count"], 10)
            self.assertEqual(result["selected_record_count"], 10)
            self.assertEqual(result["root_alias_tracks"], [])
            self.assertEqual(result["selected_page_version_count"], 6)
            self.assertEqual(result["distinct_physical_snapshot_count"], 5)
            self.assertEqual(
                result["discovery_counts"],
                {
                    "Firm": {"firm": 2, "project": 0},
                    "Plus": {"firm": 2, "project": 2},
                    "Products": {"firm": 0, "project": 0},
                    "Sustainability": {"firm": 2, "project": 2},
                    "Typology": {"firm": 2, "project": 2},
                },
            )
            receipt = json.loads(
                store._ready_path(output).read_text(encoding="utf-8")
            )
            self.assertEqual(
                receipt["snapshot_manifest"],
                {
                    "distinct_physical_snapshot_count": 5,
                    "page_version_count": 6,
                    "sha256": result["snapshot_manifest_sha256"],
                    "size_bytes": result["snapshot_manifest_size_bytes"],
                },
            )
            connection = sqlite3.connect(output)
            connection.row_factory = sqlite3.Row
            try:
                lineage = connection.execute("SELECT * FROM input_lineage").fetchone()
                metadata = json.loads(lineage["metadata_json"])
                typology = connection.execute(
                    "SELECT * FROM award_page_versions WHERE award_track='Typology'"
                ).fetchone()
                year_root = connection.execute(
                    "SELECT * FROM award_page_versions WHERE page_kind='year_root'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(lineage["selected_snapshot_count"], 6)
            self.assertEqual(
                metadata["selected_snapshot_count_semantics"], "page_versions"
            )
            self.assertEqual(metadata["distinct_physical_snapshot_count"], 5)
            self.assertEqual(
                typology["final_url"],
                "https://winners.architizer.com/2026/Typology/",
            )
            self.assertEqual(
                typology["final_url_policy"], contract["typology_final_url_policy"]
            )
            self.assertEqual(
                typology["snapshot_content_sha256"],
                year_root["snapshot_content_sha256"],
            )
            self.assertEqual(
                typology["snapshot_gzip_path"], year_root["snapshot_gzip_path"]
            )

    def test_year_root_block_non_html_and_wrong_redirect_are_rejected(self) -> None:
        cases = [
            (
                "block",
                "UPDATE http_attempts SET block_signals_json='[\"captcha\"]' WHERE id=10",
                "block/login signals",
            ),
            (
                "non_html",
                "UPDATE http_attempts SET content_type='text/xml' WHERE id=10",
                "non-HTML",
            ),
            (
                "redirect",
                "UPDATE http_attempts SET final_url='https://winners.architizer.com/2025/' WHERE id=10",
                "final URL mismatch",
            ),
        ]
        for name, sql, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                sidecar, snapshots = make_fixture(root)
                output = root / "awards.db"
                connection = sqlite3.connect(sidecar)
                try:
                    connection.execute(sql)
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaisesRegex(store.AwardsBuildError, message):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=output,
                        run_id=7,
                        limit=10,
                    )

    def test_empty_official_track_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            rewrite_snapshot(
                sidecar,
                snapshots,
                12,
                lambda _: b"<html><body>No award cards</body></html>",
            )

            with self.assertRaisesRegex(store.AwardsBuildError, "zero attribution"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

    def test_cross_page_duplicate_attribution_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            rewrite_snapshot(
                sidecar,
                snapshots,
                12,
                lambda body: body.replace(
                    b"projects.awardattribution.3000",
                    b"projects.awardattribution.2000",
                    1,
                ),
            )

            with self.assertRaisesRegex(store.AwardsBuildError, "repeat across track"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

    def test_track_attempt_not_registered_by_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            connection = sqlite3.connect(sidecar)
            try:
                connection.execute(
                    "UPDATE http_attempts SET requested_url=?,final_url=? WHERE id=12",
                    (
                        "https://winners.architizer.com/2026/Typology/",
                        "https://winners.architizer.com/2026/Typology/",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(store.AwardsBuildError, "not registered"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

    def test_year_root_track_set_must_match_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            rewrite_snapshot(
                sidecar,
                snapshots,
                10,
                lambda body: body.replace(b"/2026/Plus/", b"/2026/Firm/", 1),
            )

            with self.assertRaisesRegex(store.AwardsBuildError, "year-root tracks differ"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

    def test_run_summary_year_and_root_must_match_selected_run(self) -> None:
        cases = [
            ("award_year", 2025, "award_year differs"),
            (
                "official_root",
                "https://winners.architizer.com/2025/",
                "official_root differs",
            ),
        ]
        for field, value, message in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                sidecar, snapshots = make_fixture(root)
                output = root / "awards.db"
                connection = sqlite3.connect(sidecar)
                try:
                    summary = json.loads(
                        connection.execute(
                            "SELECT summary_json FROM runs WHERE id=7"
                        ).fetchone()[0]
                    )
                    summary[field] = value
                    connection.execute(
                        "UPDATE runs SET summary_json=? WHERE id=7",
                        (json.dumps(summary),),
                    )
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaisesRegex(store.AwardsBuildError, message):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=output,
                        run_id=7,
                        limit=10,
                    )

    def test_discovery_for_non_official_track_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            connection = sqlite3.connect(sidecar)
            try:
                connection.execute(
                    """
                    INSERT INTO award_discoveries VALUES (
                        7,2026,'Ghost','project','ghost-project',
                        'https://winners.architizer.com/2026/Ghost/',
                        'https://architizer.com/projects/ghost-project/',
                        '2026-08-01T00:00:01+00:00'
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                store.AwardsBuildError, "non-official track"
            ):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

    def test_snapshot_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            connection = sqlite3.connect(sidecar)
            try:
                connection.execute(
                    "UPDATE http_attempts SET gzip_path='../outside.html.gz' WHERE id=11"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(store.AwardsBuildError, "escapes snapshot root"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )

    def test_concurrent_publish_never_deletes_other_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"

            def concurrent_link(_source, destination):
                Path(destination).write_bytes(b"concurrent-owner")
                raise FileExistsError(destination)

            with mock.patch.object(store.os, "link", side_effect=concurrent_link):
                with self.assertRaisesRegex(store.AwardsBuildError, "during publish"):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=output,
                        run_id=7,
                        limit=10,
                    )

            self.assertEqual(output.read_bytes(), b"concurrent-owner")

    def test_ready_is_published_last_and_concurrent_owner_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            ready = store._ready_path(output)
            real_link = store.os.link
            destinations = []

            def track_links(source, destination):
                destinations.append(Path(destination))
                return real_link(source, destination)

            with mock.patch.object(store.os, "link", side_effect=track_links):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )
            self.assertEqual(destinations, [output, ready])
            self.assertTrue(output.is_file())
            self.assertTrue(ready.is_file())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            ready = store._ready_path(output)
            real_link = store.os.link

            def concurrent_ready(source, destination):
                if Path(destination) == ready:
                    ready.write_bytes(b"concurrent-ready-owner")
                    raise FileExistsError(destination)
                return real_link(source, destination)

            with mock.patch.object(store.os, "link", side_effect=concurrent_ready):
                with self.assertRaisesRegex(
                    store.AwardsBuildError, "READY receipt appeared during publish"
                ):
                    store.build_awards_database(
                        sidecar_path=sidecar,
                        snapshot_root=snapshots,
                        output_path=output,
                        run_id=7,
                        limit=10,
                    )
            self.assertFalse(output.exists())
            self.assertEqual(ready.read_bytes(), b"concurrent-ready-owner")

    def test_preexisting_ready_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            ready = store._ready_path(output)
            ready.write_bytes(b"existing-ready-owner")

            with self.assertRaisesRegex(store.AwardsBuildError, "READY receipt already"):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=output,
                    run_id=7,
                    limit=10,
                )
            self.assertFalse(output.exists())
            self.assertEqual(ready.read_bytes(), b"existing-ready-owner")

    def test_build_is_byte_deterministic_across_workspace_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            first_sidecar, first_snapshots = make_fixture(first_root)
            second_sidecar, second_snapshots = make_fixture(second_root)
            self.assertEqual(file_sha(first_sidecar), file_sha(second_sidecar))
            first_output = first_root / "artifact.db"
            second_output = second_root / "artifact.db"

            first = store.build_awards_database(
                sidecar_path=first_sidecar,
                snapshot_root=first_snapshots,
                output_path=first_output,
                run_id=7,
                limit=10,
            )
            second = store.build_awards_database(
                sidecar_path=second_sidecar,
                snapshot_root=second_snapshots,
                output_path=second_output,
                run_id=7,
                limit=10,
            )

            self.assertEqual(first["output_sha256"], second["output_sha256"])
            self.assertEqual(
                Path(first["ready_path"]).read_bytes(),
                Path(second["ready_path"]).read_bytes(),
            )
            connection = sqlite3.connect(first_output)
            connection.row_factory = sqlite3.Row
            try:
                lineage = connection.execute("SELECT * FROM input_lineage").fetchone()
                manifest = connection.execute("SELECT * FROM build_manifest").fetchone()
            finally:
                connection.close()
            self.assertEqual(lineage["sidecar_path"], "recrawl.db")
            self.assertEqual(lineage["snapshot_root"], "snapshots")
            self.assertEqual(manifest["built_at"], "2026-08-01T00:01:00+00:00")

    def test_build_mode_requires_safe_explicit_selection_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            with self.assertRaisesRegex(
                store.AwardsBuildError, "explicit --run-id or --award-year"
            ):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=root / "full.db",
                )
            with self.assertRaisesRegex(
                store.AwardsBuildError, "non-production output path"
            ):
                store.build_awards_database(
                    sidecar_path=sidecar,
                    snapshot_root=snapshots,
                    output_path=store.DEFAULT_PRODUCTION_OUTPUT,
                    run_id=7,
                    limit=10,
                )

    def test_multiple_dom_subject_conflict_is_not_a_discovery_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            original_anchor = (
                b'<a class="text-dark" '
                b'href="https://architizer.com/projects/project-0/">'
                b"Plus Project 0</a>"
            )
            extra_anchor = (
                original_anchor
                + b'<a class="text-dark" '
                + b'href="https://architizer.com/projects/second-project/">'
                + b"Second Project</a>"
            )
            rewrite_snapshot(
                sidecar,
                snapshots,
                11,
                lambda body: body.replace(original_anchor, extra_anchor, 1),
            )
            connection = sqlite3.connect(sidecar)
            try:
                connection.execute(
                    "DELETE FROM award_discoveries "
                    "WHERE entity_type='project' AND slug='project-0'"
                )
                connection.commit()
            finally:
                connection.close()

            result = store.build_awards_database(
                sidecar_path=sidecar,
                snapshot_root=snapshots,
                output_path=output,
                run_id=7,
                limit=2,
            )

            self.assertEqual(result["discovery_counts"]["Plus"]["project"], 5)
            connection = sqlite3.connect(output)
            connection.row_factory = sqlite3.Row
            try:
                record = connection.execute(
                    "SELECT * FROM award_attributions WHERE award_track='Plus' "
                    "ORDER BY source_card_ordinal LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(record["parse_status"], "conflict")
            self.assertIsNone(record["subject_slug"])
            self.assertTrue(
                any(
                    conflict.get("reason") == "multiple_subject_anchors"
                    for conflict in json.loads(record["conflicts_json"])
                )
            )

    def test_conflicting_card_evidence_is_preserved_not_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar, snapshots = make_fixture(root)
            output = root / "awards.db"
            connection = sqlite3.connect(sidecar)
            connection.row_factory = sqlite3.Row
            try:
                attempt = connection.execute(
                    "SELECT gzip_path FROM http_attempts WHERE id=11"
                ).fetchone()
                snapshot = snapshots / attempt["gzip_path"]
                with gzip.open(snapshot, "rb") as handle:
                    body = handle.read()
                body = body.replace(
                    b"https://architizer.com/projects/project-0/",
                    b"https://architizer.com/projects/different-project/",
                    1,
                )
                with gzip.GzipFile(
                    filename=str(snapshot), mode="wb", mtime=0
                ) as handle:
                    handle.write(body)
                content_sha = hashlib.sha256(body).hexdigest().upper()
                connection.execute(
                    "UPDATE http_attempts SET response_bytes=?,sha256=? WHERE id=11",
                    (len(body), content_sha),
                )
                connection.execute(
                    "DELETE FROM award_discoveries "
                    "WHERE entity_type='project' AND slug='project-0'"
                )
                connection.commit()
            finally:
                connection.close()

            result = store.build_awards_database(
                sidecar_path=sidecar,
                snapshot_root=snapshots,
                output_path=output,
                run_id=7,
                limit=2,
            )

            self.assertEqual(result["status_counts"], {"complete": 1, "conflict": 1})
            self.assertEqual(result["discovery_counts"]["Plus"]["project"], 5)
            connection = sqlite3.connect(
                output.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
            connection.row_factory = sqlite3.Row
            try:
                conflict = connection.execute(
                    "SELECT * FROM award_attributions WHERE parse_status='conflict'"
                ).fetchone()
                raw = json.loads(conflict["raw_attributes_json"])
                dom = json.loads(conflict["dom_values_json"])
            finally:
                connection.close()

            self.assertIsNone(conflict["subject_slug"])
            self.assertEqual(raw["winner"]["data-slug"], "project-0")
            self.assertEqual(dom["subject"]["slug"], "different-project")


if __name__ == "__main__":
    unittest.main()
