"""Policy and SQLite integration tests for Architizer curated v1.

The fixture intentionally uses the crawler's five-table schema.  It is small
enough for the deterministic builder to run twice while still exercising
strict clustering, review-only candidates, source occurrence accounting, and
immutable publication.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from canonical.architizer_curated import (
    ARCHITIZER_ARTICLE_TAGS,
    BROAD_CATEGORIES,
    CATEGORY_PARENT,
    POLICY_VERSION,
    SCHEMA_VERSION,
    clean_scalar,
    image_identity,
    is_generic_project_name,
    mappings_for_category,
    name_similarity,
    normalize_identity_text,
    parse_json_dict,
    parse_json_list,
    parse_size_bucket,
    text_has_mojibake,
    valid_or_candidate_year,
)
from tools import build_architizer_curated as builder


SOURCE_SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE architizer_projects (
    id                    INTEGER PRIMARY KEY,
    global_id             TEXT UNIQUE,
    slug                  TEXT UNIQUE NOT NULL,
    name                  TEXT NOT NULL,
    firm_slug             TEXT,
    firm_name             TEXT,
    description           TEXT,
    description_short     TEXT,
    completion_year       INTEGER,
    building_size_slug    TEXT,
    building_size_display TEXT,
    constr_status         TEXT,
    budget                REAL,
    location_full         TEXT,
    location_country      TEXT,
    location_city         TEXT,
    categories            TEXT,
    cover_image_url       TEXT,
    gallery_image_urls    TEXT,
    image_global_ids      TEXT,
    published_time        TEXT,
    modified_time         TEXT,
    fetched_at            TEXT
);

CREATE TABLE architizer_firms (
    slug                TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    office_locations    TEXT,
    description         TEXT,
    awards_summary      TEXT,
    project_count_seen  INTEGER DEFAULT 0,
    social_links        TEXT,
    fetched_at          TEXT
);

CREATE TABLE architizer_awards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    award_year      INTEGER NOT NULL,
    award_track     TEXT NOT NULL,
    award_category  TEXT,
    award_tier      TEXT NOT NULL,
    project_slug    TEXT,
    firm_slug       TEXT,
    source_url      TEXT NOT NULL,
    fetched_at      TEXT
);

CREATE TABLE pending_projects (
    url            TEXT PRIMARY KEY,
    source_url     TEXT,
    lastmod        TEXT,
    status         TEXT DEFAULT 'pending',
    discovered_at  TEXT,
    fetched_at     TEXT,
    error          TEXT
);

CREATE TABLE pending_firms (
    url            TEXT PRIMARY KEY,
    source_url     TEXT,
    lastmod        TEXT,
    status         TEXT DEFAULT 'pending',
    discovered_at  TEXT,
    fetched_at     TEXT,
    error          TEXT
);
"""

PROJECT_INSERT = """
INSERT INTO architizer_projects(
    id,global_id,slug,name,firm_slug,firm_name,description,description_short,
    completion_year,building_size_slug,building_size_display,constr_status,
    budget,location_full,location_country,location_city,categories,
    cover_image_url,gallery_image_urls,image_global_ids,published_time,
    modified_time,fetched_at
) VALUES (
    :id,:global_id,:slug,:name,:firm_slug,:firm_name,:description,
    :description_short,:completion_year,:building_size_slug,
    :building_size_display,:constr_status,:budget,:location_full,
    :location_country,:location_city,:categories,:cover_image_url,
    :gallery_image_urls,:image_global_ids,:published_time,:modified_time,
    :fetched_at
)
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _project(
    project_id: int,
    *,
    slug: str,
    name: str,
    year: int,
    categories: list[Any],
    global_id: str | None = None,
    cover: str | None = None,
    gallery: list[Any] | None = None,
    global_ids: list[Any] | None = None,
) -> dict[str, Any]:
    cover = cover or (
        f"http://architizer-prod.imgix.net/media/{slug}.jpg"
        "?w=1680&q=60&auto=format,compress&cs=strip"
    )
    gallery = gallery if gallery is not None else [
        f"http://architizer-prod.imgix.net/media/{slug}-gallery.jpg?w=900&q=60"
    ]
    global_ids = global_ids if global_ids is not None else [
        f"media.mediaitemattribution.{project_id}01"
    ]
    return {
        "id": project_id,
        "global_id": global_id or f"projects.project.{project_id}",
        "slug": slug,
        "name": name,
        "firm_slug": "studio-one",
        "firm_name": "Studio One",
        "description": f"Source description for {name}.",
        "description_short": f"Short source description for {name}.",
        "completion_year": year,
        "building_size_slug": "sqft_10_25",
        "building_size_display": "10,000 sqft - 25,000 sqft",
        "constr_status": "built",
        "budget": 0.0,
        "location_full": "Seoul, South Korea",
        "location_country": "South Korea",
        "location_city": "Seoul",
        "categories": json.dumps(categories, ensure_ascii=False),
        "cover_image_url": cover,
        "gallery_image_urls": json.dumps(gallery, ensure_ascii=False),
        "image_global_ids": json.dumps(global_ids, ensure_ascii=False),
        "published_time": "2024-01-01T00:00:00Z",
        "modified_time": "2025-01-01T00:00:00Z",
        "fetched_at": "2026-07-30 12:00:00",
    }


def _create_source_fixture(path: Path) -> list[dict[str, Any]]:
    common_cover_large = (
        "http://architizer-prod.imgix.net/media/shared-river.jpg"
        "?w=1680&q=60&auto=format,compress&cs=strip"
    )
    common_cover_small = (
        "https://architizer-prod.imgix.net/media/shared-river.jpg?w=400&q=80"
    )
    placeholder_url = (
        "http://static-web-prod.arc.ht/img/social/"
        "facebook-default-thumb.3966dfd42283.jpg"
    )
    projects = [
        _project(
            1,
            slug="river-arts-center-a",
            name="River Arts Center",
            year=2020,
            categories=["Cultural", "Museum"],
            cover=common_cover_large,
            gallery=[
                common_cover_small,
                "http://architizer-prod.imgix.net/media/river-gallery.jpg?w=900&q=60",
                "not a url",
                placeholder_url,
            ],
            global_ids=[
                "media.mediaitemattribution.101",
                "media.mediaitemattribution.102",
                "media.mediaitemattribution.103",
            ],
        ),
        _project(
            2,
            slug="river-arts-center-b",
            name="River Arts Center",
            year=2020,
            categories=["Cultural", "Museum"],
            cover=common_cover_small,
        ),
        _project(
            3,
            slug="river-arts-center-later-record",
            name="River Arts Center",
            year=2021,
            categories=["Cultural", "Museum"],
        ),
        _project(
            4,
            slug="civic-health-complex",
            name="Civic Health Complex",
            year=2022,
            categories=["Government + Health", "Hospital", "City Hall"],
        ),
        _project(
            5,
            slug="wrong-entity-project",
            name="Wrong Entity Project",
            year=2020,
            categories=["Commercial", "Office"],
            global_id="firms.firm.5",
        ),
        _project(
            6,
            slug="house-01-a",
            name="House 01",
            year=2019,
            categories=["Residential", "Private House"],
        ),
        _project(
            7,
            slug="house-01-b",
            name="House 01",
            year=2019,
            categories=["Residential", "Private House"],
        ),
        _project(
            8,
            slug="civic-hub-phase-2-a",
            name="Civic Hub Phase 2",
            year=2018,
            categories=["Educational", "University"],
        ),
        _project(
            9,
            slug="civic-hub-phase-2-b",
            name="Civic Hub Phase 2",
            year=2018,
            categories=["Educational", "University"],
        ),
        _project(
            10,
            slug="civic-learning-hub",
            name="Civic Learning Hub",
            year=2017,
            categories=["Educational", "University"],
        ),
        _project(
            11,
            slug="civic-learnng-hub",
            name="Civic Learnng Hub",
            year=2017,
            categories=["Educational", "University"],
            cover=placeholder_url,
            gallery=[placeholder_url],
        ),
    ]
    # Keep these records eligible for the strict identity rule while forcing
    # two structured scalar conflicts inside the resulting building.
    projects[1]["building_size_slug"] = "sqft_25_100"
    projects[1]["building_size_display"] = "25,000 sqft - 100,000 sqft"
    projects[1]["constr_status"] = "concept"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SOURCE_SCHEMA)
        connection.execute(
            """
            INSERT INTO architizer_firms(
                slug,name,office_locations,description,awards_summary,
                project_count_seen,social_links,fetched_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "studio-one",
                "Studio One",
                json.dumps(["Seoul, South Korea"]),
                "Studio One source profile.",
                "Winner (1)",
                len(projects),
                json.dumps({"instagram": "https://instagram.com/studio-one"}),
                "2026-07-30 12:00:00",
            ),
        )
        for project in projects:
            connection.execute(PROJECT_INSERT, project)
            connection.execute(
                """
                INSERT INTO pending_projects(
                    url,source_url,lastmod,status,discovered_at,fetched_at,error
                ) VALUES (?,?,?,?,?,?,NULL)
                """,
                (
                    f"https://architizer.com/projects/{project['slug']}/",
                    "https://architizer.com/sitemap-projects.xml?p=1",
                    "2026-07-30",
                    "done",
                    "2026-07-30 10:00:00",
                    "2026-07-30 12:00:00",
                ),
            )
        connection.execute(
            """
            INSERT INTO pending_firms(
                url,source_url,lastmod,status,discovered_at,fetched_at,error
            ) VALUES (?,?,?,?,?,?,NULL)
            """,
            (
                "https://architizer.com/firms/studio-one/",
                "https://architizer.com/sitemap-firms.xml?p=1",
                "2026-07-30",
                "done",
                "2026-07-30 10:00:00",
                "2026-07-30 12:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO architizer_awards(
                award_year,award_track,award_category,award_tier,
                project_slug,firm_slug,source_url,fetched_at
            ) VALUES (2025,'Typology','Cultural > Museum','Jury',
                      'river-arts-center-a',NULL,
                      'https://winners.architizer.com/2025/Typology/',
                      '2026-07-30 12:00:00')
            """
        )
        connection.commit()
    finally:
        connection.close()
    return projects


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


class ArchitizerPolicyTests(unittest.TestCase):
    def test_category_inventory_and_hierarchy_are_complete(self) -> None:
        self.assertEqual(len(ARCHITIZER_ARTICLE_TAGS), 78)
        self.assertEqual(len(BROAD_CATEGORIES), 9)
        self.assertEqual(len(CATEGORY_PARENT), 69)
        self.assertEqual(
            set(CATEGORY_PARENT),
            set(ARCHITIZER_ARTICLE_TAGS) - set(BROAD_CATEGORIES),
        )
        self.assertLessEqual(set(CATEGORY_PARENT.values()), set(BROAD_CATEGORIES))

    def test_unknown_and_other_are_explicitly_unmapped(self) -> None:
        self.assertEqual(mappings_for_category("Other"), [])
        self.assertEqual(mappings_for_category("Future Unknown Category"), [])
        self.assertEqual(mappings_for_category(None), [])

    def test_broad_ambiguous_and_direct_leaf_evidence_are_distinct(self) -> None:
        broad = mappings_for_category("Residential")
        ambiguous = mappings_for_category("Bicycles")
        direct = mappings_for_category("Private House")
        self.assertTrue(broad)
        self.assertTrue(ambiguous)
        self.assertTrue(direct)
        self.assertEqual({mapping.status for mapping in broad}, {"candidate"})
        self.assertEqual({mapping.mapping_kind for mapping in broad}, {"supporting"})
        self.assertEqual({mapping.status for mapping in ambiguous}, {"candidate"})
        self.assertEqual(
            {mapping.mapping_kind for mapping in ambiguous},
            {"supporting"},
        )
        self.assertEqual({mapping.status for mapping in direct}, {"confirmed"})
        self.assertEqual({mapping.mapping_kind for mapping in direct}, {"direct"})

    def test_no_category_invents_material(self) -> None:
        mappings = [
            mapping
            for category in ARCHITIZER_ARTICLE_TAGS
            for mapping in mappings_for_category(category)
        ]
        self.assertTrue(mappings)
        self.assertNotIn("material", {mapping.axis for mapping in mappings})
        self.assertTrue(
            all(mapping.target_scope == "building" for mapping in mappings)
        )

    def test_json_scalar_and_identity_helpers(self) -> None:
        self.assertEqual(clean_scalar("  A\u00a0  B  "), "A B")
        self.assertEqual(normalize_identity_text("Álvaro—Siza_Office"), "alvaro siza office")
        self.assertEqual(parse_json_list('["Museum", "Cultural"]'), ["Museum", "Cultural"])
        self.assertEqual(parse_json_list("not-json"), ["not-json"])
        self.assertEqual(parse_json_dict('{"instagram":"https://example.test"}'), {
            "instagram": "https://example.test"
        })
        self.assertEqual(parse_json_dict("[]"), {})

    def test_size_and_year_policies_do_not_depend_on_location(self) -> None:
        known = parse_size_bucket(
            "sqft_100_300",
            "100,000 sqft - 300,000 sqft",
        )
        self.assertEqual(
            known,
            {
                "slug": "sqft_100_300",
                "display": "100,000 sqft - 300,000 sqft",
                "min_sqft": 100_000,
                "max_sqft": 300_000,
                "is_open_ended": False,
                "status": "confirmed",
            },
        )
        self.assertEqual(
            parse_size_bucket("sqft_1000", "1,000,000 +")["max_sqft"],
            None,
        )
        self.assertEqual(
            parse_size_bucket("", "42 sqft - 84 sqft")["status"],
            "candidate",
        )
        self.assertEqual(
            parse_size_bucket("sqft_10_25", "1 sqft - 2 sqft")["status"],
            "review",
        )
        self.assertEqual(valid_or_candidate_year(2024, "built"), (2024, "confirmed"))
        self.assertEqual(valid_or_candidate_year(2030, "concept"), (2030, "candidate"))
        self.assertEqual(
            valid_or_candidate_year(2030, "under-construction"),
            (2030, "candidate"),
        )
        self.assertEqual(
            valid_or_candidate_year(2030, "built"),
            (None, "review"),
        )
        self.assertEqual(
            valid_or_candidate_year(2588, "under-construction"),
            (None, "review"),
        )
        self.assertEqual(valid_or_candidate_year(None, "built"), (None, "missing"))
        # The parser's positional city/country values are preserved, but the
        # source header alone is not sufficient semantic confirmation.
        self.assertEqual(
            builder._location_part_policy(
                "Seoul, South Korea",
                "South Korea",
                part="country",
            ),
            (
                "South Korea",
                "candidate",
                "last_header_token_semantics_unverified",
            ),
        )
        self.assertEqual(
            builder._location_part_policy(
                "Seoul, Korea, Republic of",
                "Republic of",
                part="country",
            ),
            (None, "review", "country_token_semantics_incomplete"),
        )
        self.assertEqual(
            builder._location_part_policy(
                "Seoul, South Korea",
                "Seoul",
                part="city",
            ),
            ("Seoul", "candidate", "first_header_token_semantics_unverified"),
        )
        self.assertEqual(
            builder._location_part_policy(
                "Rijnweg, South Holland, Netherlands",
                "Rijnweg",
                part="city",
            ),
            ("Rijnweg", "candidate", "first_header_token_semantics_unverified"),
        )
        self.assertEqual(
            builder._location_part_policy(
                "CO, United States",
                "CO",
                part="city",
            ),
            (None, "review", "likely_admin_area_abbreviation"),
        )
        self.assertEqual(
            builder._location_part_policy(
                "101, South Korea",
                "101",
                part="city",
            ),
            (None, "review", "non_alphabetic_city_token"),
        )

    def test_generic_phase_mojibake_and_fuzzy_policies(self) -> None:
        self.assertTrue(is_generic_project_name("House"))
        self.assertTrue(is_generic_project_name("House 01"))
        self.assertTrue(is_generic_project_name("House A"))
        self.assertTrue(is_generic_project_name("Office B"))
        self.assertTrue(is_generic_project_name("Pavilion II"))
        self.assertTrue(is_generic_project_name("Office in Seoul"))
        self.assertFalse(is_generic_project_name("Fallingwater"))
        self.assertTrue(builder._phase_marker("Civic Hub Phase 2"))
        self.assertTrue(builder._phase_marker("Museum Extension"))
        self.assertTrue(builder._phase_marker("Civic Hub", "civic-hub-stage-ii"))
        self.assertFalse(builder._phase_marker("River Arts Center"))
        self.assertNotEqual(builder._match_name("AB C"), builder._match_name("A BC"))
        self.assertEqual(name_similarity("Álvaro Siza", "Alvaro-Siza"), 1.0)
        self.assertGreater(
            name_similarity("Civic Learning Hub", "Civic Learnng Hub"),
            0.88,
        )
        self.assertEqual(name_similarity("", "Civic Learning Hub"), 0.0)
        self.assertTrue(text_has_mojibake("Fran\u00c3\u00a7ois"))
        self.assertFalse(text_has_mojibake("Fran\u00e7ois"))

    def test_image_asset_identity_placeholder_and_malformed(self) -> None:
        large = image_identity(
            "http://architizer-prod.imgix.net/media/a.jpg"
            "?w=1680&q=60&auto=format,compress&cs=strip"
        )
        small = image_identity(
            "https://architizer-prod.imgix.net/media/a.jpg?w=400&q=80"
        )
        identity_query = image_identity(
            "https://architizer-prod.imgix.net/media/a.jpg?token=one&w=400"
        )
        self.assertIsNotNone(large)
        self.assertIsNotNone(small)
        self.assertIsNotNone(identity_query)
        self.assertEqual(large.asset_id, small.asset_id)
        self.assertEqual(large.asset_key, small.asset_key)
        self.assertNotEqual(large.asset_key, identity_query.asset_key)
        self.assertNotIn("w=", large.normalized_url)
        placeholder = image_identity(
            "http://static-web-prod.arc.ht/img/social/"
            "facebook-default-thumb.3966dfd42283.jpg"
        )
        self.assertIsNotNone(placeholder)
        self.assertTrue(placeholder.is_placeholder_candidate)
        self.assertIsNone(image_identity("not a url"))
        self.assertIsNone(image_identity("ftp://architizer-prod.imgix.net/a.jpg"))
        self.assertIsNone(image_identity("https://example.com/a.jpg"))


class ArchitizerBuilderIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(
            prefix="architizer-curated-test-"
        )
        cls.root = Path(cls._temporary.name)
        cls.source = cls.root / "architizer-source.db"
        cls.output = cls.root / "architizer-curated.db"
        cls.report = cls.root / "architizer-curated.md"
        cls.projects = _create_source_fixture(cls.source)
        cls.source_sha_before = _sha256(cls.source)
        cls.source_size = cls.source.stat().st_size
        cls.result = builder.build(
            source_path=cls.source,
            output_path=cls.output,
            report_path=cls.report,
            limit=None,
            expected_sha256=cls.source_sha_before,
            expected_size=cls.source_size,
            verify_deterministic=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_source_is_read_only_and_membership_accounting_is_exact(self) -> None:
        self.assertEqual(_sha256(self.source), self.source_sha_before)
        self.assertEqual(self.result["source_audit"]["query_only"], 1)
        self.assertTrue(self.result["validation"]["passed"])
        connection = _open_readonly(self.output)
        try:
            snapshot = connection.execute(
                """
                SELECT source_path,source_sha256_before,source_sha256_after,query_only
                FROM source_snapshots
                """
            ).fetchone()
            self.assertEqual(snapshot["source_path"], self.source.name)
            self.assertEqual(snapshot["source_sha256_before"], self.source_sha_before)
            self.assertEqual(snapshot["source_sha256_after"], self.source_sha_before)
            self.assertEqual(snapshot["query_only"], 1)
            accepted = connection.execute(
                "SELECT COUNT(*) FROM source_projects WHERE acceptance_status='accepted'"
            ).fetchone()[0]
            excluded = connection.execute(
                "SELECT COUNT(*) FROM source_projects WHERE acceptance_status='excluded'"
            ).fetchone()[0]
            self.assertEqual(accepted, 10)
            self.assertEqual(excluded, 1)
            membership_errors = connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT p.source_project_id,COUNT(bp.building_id) AS n
                    FROM source_projects p
                    LEFT JOIN building_projects bp USING (source_project_id)
                    WHERE p.acceptance_status='accepted'
                    GROUP BY p.source_project_id
                    HAVING n != 1
                )
                """
            ).fetchone()[0]
            self.assertEqual(membership_errors, 0)
            excluded_memberships = connection.execute(
                """
                SELECT COUNT(*)
                FROM source_projects p
                JOIN building_projects bp USING (source_project_id)
                WHERE p.acceptance_status='excluded'
                """
            ).fetchone()[0]
            self.assertEqual(excluded_memberships, 0)
            excluded_row = connection.execute(
                """
                SELECT acceptance_status,exclusion_reason
                FROM source_projects WHERE source_project_id=5
                """
            ).fetchone()
            self.assertEqual(excluded_row["acceptance_status"], "excluded")
            self.assertEqual(
                excluded_row["exclusion_reason"],
                "global_id_entity_type_mismatch",
            )
        finally:
            connection.close()
        source_connection = builder.open_source(self.source)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                source_connection.execute("CREATE TABLE forbidden_write(id INTEGER)")
        finally:
            source_connection.close()

    def test_category_image_and_global_id_occurrences_are_preserved(self) -> None:
        connection = _open_readonly(self.output)
        try:
            expected_categories = connection.execute(
                "SELECT SUM(category_occurrence_count) FROM source_projects"
            ).fetchone()[0]
            actual_categories = connection.execute(
                "SELECT COUNT(*) FROM project_category_occurrences"
            ).fetchone()[0]
            self.assertEqual(actual_categories, expected_categories)
            project_one_categories = [
                row["raw_value"]
                for row in connection.execute(
                    """
                    SELECT raw_value FROM project_category_occurrences
                    WHERE source_project_id=1 ORDER BY ordinal
                    """
                )
            ]
            self.assertEqual(project_one_categories, ["Cultural", "Museum"])

            expected_images = connection.execute(
                "SELECT SUM(1 + gallery_occurrence_count) FROM source_projects"
            ).fetchone()[0]
            actual_images = connection.execute(
                "SELECT COUNT(*) FROM source_image_occurrences"
            ).fetchone()[0]
            self.assertEqual(actual_images, expected_images)
            malformed = connection.execute(
                """
                SELECT raw_url,parse_status,asset_id
                FROM source_image_occurrences
                WHERE source_project_id=1 AND raw_url='not a url'
                """
            ).fetchone()
            self.assertEqual(malformed["parse_status"], "malformed")
            self.assertIsNone(malformed["asset_id"])
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM source_image_occurrences
                    WHERE image_type IS NOT NULL
                    """
                ).fetchone()[0],
                0,
            )
            shared_assets = connection.execute(
                """
                SELECT COUNT(DISTINCT asset_id)
                FROM source_image_occurrences
                WHERE source_project_id IN (1,2) AND role='cover'
                """
            ).fetchone()[0]
            self.assertEqual(shared_assets, 1)
            self.assertGreaterEqual(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM source_image_occurrences
                    WHERE parse_status='placeholder_candidate'
                    """
                ).fetchone()[0],
                1,
            )

            expected_global_ids = connection.execute(
                "SELECT SUM(image_global_id_occurrence_count) FROM source_projects"
            ).fetchone()[0]
            actual_global_ids = connection.execute(
                "SELECT COUNT(*) FROM project_image_global_id_occurrences"
            ).fetchone()[0]
            self.assertEqual(actual_global_ids, expected_global_ids)
            global_ids = [
                row["raw_global_id"]
                for row in connection.execute(
                    """
                    SELECT raw_global_id
                    FROM project_image_global_id_occurrences
                    WHERE source_project_id=1 ORDER BY ordinal
                    """
                )
            ]
            self.assertEqual(
                global_ids,
                [
                    "media.mediaitemattribution.101",
                    "media.mediaitemattribution.102",
                    "media.mediaitemattribution.103",
                ],
            )
            placeholder_only = connection.execute(
                """
                SELECT pc.has_image,e.cover_image_url,e.image_urls_json,
                       e.work_type_tags_json
                FROM project_completeness pc
                JOIN building_projects bp USING (source_project_id)
                JOIN v_architizer_buildings_export e USING (building_id)
                WHERE pc.source_project_id=11
                """
            ).fetchone()
            self.assertEqual(placeholder_only["has_image"], 0)
            self.assertIsNone(placeholder_only["cover_image_url"])
            self.assertEqual(json.loads(placeholder_only["image_urls_json"]), [])
            self.assertIsInstance(
                json.loads(placeholder_only["work_type_tags_json"]),
                list,
            )
        finally:
            connection.close()

    def test_strict_merge_and_all_review_candidates_stay_separate(self) -> None:
        connection = _open_readonly(self.output)
        try:
            strict = connection.execute(
                """
                SELECT candidate_kind,decision_status
                FROM duplicate_candidates
                WHERE left_project_id=1 AND right_project_id=2
                """
            ).fetchone()
            self.assertEqual(tuple(strict), ("strict", "auto_clustered"))
            strict_buildings = connection.execute(
                """
                SELECT COUNT(DISTINCT building_id) FROM building_projects
                WHERE source_project_id IN (1,2)
                """
            ).fetchone()[0]
            self.assertEqual(strict_buildings, 1)
            self.assertEqual(
                connection.execute(
                    """
                    SELECT project_count FROM buildings
                    WHERE building_id=(
                        SELECT building_id FROM building_projects
                        WHERE source_project_id=1
                    )
                    """
                ).fetchone()[0],
                2,
            )

            year_conflict = connection.execute(
                """
                SELECT candidate_kind,decision_status,same_nonnull_year
                FROM duplicate_candidates
                WHERE left_project_id=1 AND right_project_id=3
                """
            ).fetchone()
            self.assertEqual(
                tuple(year_conflict),
                ("exact_review", "review", 0),
            )
            generic = connection.execute(
                """
                SELECT candidate_kind,decision_status,generic_name
                FROM duplicate_candidates
                WHERE left_project_id=6 AND right_project_id=7
                """
            ).fetchone()
            self.assertEqual(tuple(generic), ("exact_review", "review", 1))
            phase = connection.execute(
                """
                SELECT candidate_kind,decision_status,phase_marker
                FROM duplicate_candidates
                WHERE left_project_id=8 AND right_project_id=9
                """
            ).fetchone()
            self.assertEqual(tuple(phase), ("exact_review", "review", 1))
            fuzzy = connection.execute(
                """
                SELECT candidate_kind,decision_status
                FROM duplicate_candidates
                WHERE left_project_id=10 AND right_project_id=11
                """
            ).fetchone()
            self.assertEqual(tuple(fuzzy), ("fuzzy_review", "review"))
            for left, right in ((1, 3), (6, 7), (8, 9), (10, 11)):
                rows = connection.execute(
                    """
                    SELECT COUNT(DISTINCT building_id)
                    FROM building_projects
                    WHERE source_project_id IN (?,?)
                    """,
                    (left, right),
                ).fetchone()[0]
                self.assertEqual(rows, 2)
        finally:
            connection.close()

    def test_scalar_conflict_abstains_and_material_is_never_created(self) -> None:
        connection = _open_readonly(self.output)
        try:
            building_id = connection.execute(
                "SELECT building_id FROM building_projects WHERE source_project_id=4"
            ).fetchone()[0]
            program_facets = [
                tuple(row)
                for row in connection.execute(
                    """
                    SELECT value,status FROM building_facets
                    WHERE building_id=? AND axis='program'
                    ORDER BY value
                    """,
                    (building_id,),
                )
            ]
            self.assertEqual(
                program_facets,
                [("Government", "conflict"), ("Healthcare", "conflict")],
            )
            export = connection.execute(
                """
                SELECT program_primary,taxonomy_status
                FROM v_architizer_buildings_export WHERE building_id=?
                """,
                (building_id,),
            ).fetchone()
            self.assertIsNone(export["program_primary"])
            self.assertEqual(export["taxonomy_status"], "conflict")
            qa = connection.execute(
                """
                SELECT COUNT(*) FROM qa_issues
                WHERE entity_type='building_facet'
                  AND entity_id=? AND issue_code='scalar_conflict_abstained'
                """,
                (f"{building_id}:program",),
            ).fetchone()[0]
            self.assertEqual(qa, 1)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM attribute_claims WHERE axis='material'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM building_facets WHERE axis='material'"
                ).fetchone()[0],
                0,
            )
            strict_building_id = connection.execute(
                "SELECT building_id FROM building_projects WHERE source_project_id=1"
            ).fetchone()[0]
            strict_export = connection.execute(
                """
                SELECT area_bucket,project_status
                FROM v_architizer_buildings_export
                WHERE building_id=?
                """,
                (strict_building_id,),
            ).fetchone()
            self.assertIsNone(strict_export["area_bucket"])
            self.assertIsNone(strict_export["project_status"])
            scalar_conflicts = {
                row["axis"]
                for row in connection.execute(
                    """
                    SELECT DISTINCT axis
                    FROM building_facets
                    WHERE building_id=? AND status='conflict'
                      AND axis IN ('area_bucket','project_status')
                    """,
                    (strict_building_id,),
                )
            }
            self.assertEqual(
                scalar_conflicts,
                {"area_bucket", "project_status"},
            )
        finally:
            connection.close()

    def test_malformed_containers_keep_source_text_and_occurrences(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-malformed-test-"
        ) as temporary:
            root = Path(temporary)
            source = root / "source.db"
            output = root / "curated.db"
            report = root / "curated.md"
            _create_source_fixture(source)
            source_connection = sqlite3.connect(source)
            try:
                source_connection.execute(
                    """
                    UPDATE architizer_projects
                    SET categories=?,
                        gallery_image_urls=?,
                        image_global_ids=?
                    WHERE id=10
                    """,
                    ("not-json", "[broken", json.dumps("single-global-id")),
                )
                source_connection.execute(
                    """
                    INSERT INTO pending_firms(
                        url,source_url,lastmod,status,discovered_at,fetched_at,error
                    ) VALUES (?,?,?,?,?,?,NULL)
                    """,
                    (
                        "https://architizer.com/firms/missing-done-firm/",
                        "https://architizer.com/sitemap-firms.xml?p=1",
                        "2026-07-30",
                        "done",
                        "2026-07-30 10:00:00",
                        "2026-07-30 12:00:00",
                    ),
                )
                source_connection.commit()
            finally:
                source_connection.close()
            source_sha = _sha256(source)
            result = builder.build(
                source_path=source,
                output_path=output,
                report_path=report,
                limit=None,
                expected_sha256=source_sha,
                expected_size=source.stat().st_size,
                verify_deterministic=False,
            )
            connection = _open_readonly(output)
            try:
                project = connection.execute(
                    """
                    SELECT categories_source_text,gallery_image_urls_source_text,
                           image_global_ids_source_text,categories_raw_json,
                           gallery_image_urls_raw_json,image_global_ids_raw_json
                    FROM source_projects WHERE source_project_id=10
                    """
                ).fetchone()
                self.assertEqual(project["categories_source_text"], "not-json")
                self.assertEqual(project["gallery_image_urls_source_text"], "[broken")
                self.assertEqual(
                    project["image_global_ids_source_text"],
                    json.dumps("single-global-id"),
                )
                self.assertEqual(json.loads(project["categories_raw_json"]), ["not-json"])
                self.assertEqual(
                    json.loads(project["gallery_image_urls_raw_json"]),
                    ["[broken"],
                )
                self.assertEqual(
                    json.loads(project["image_global_ids_raw_json"]),
                    ["single-global-id"],
                )
                category = connection.execute(
                    """
                    SELECT raw_value,parse_status
                    FROM project_category_occurrences
                    WHERE source_project_id=10
                    """
                ).fetchone()
                self.assertEqual(tuple(category), ("not-json", "malformed_container"))
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM qa_issues
                        WHERE entity_id='10' AND issue_code IN (
                            'malformed_categories_json',
                            'malformed_gallery_json',
                            'malformed_image_global_ids_json'
                        )
                        """
                    ).fetchone()[0],
                    3,
                )
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM qa_issues
                        WHERE issue_code='done_queue_without_firm'
                        """
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    result["validation"]["category_master_occurrence_count_error"],
                    0,
                )
                self.assertEqual(
                    result["validation"]["category_master_project_count_error"],
                    0,
                )
            finally:
                connection.close()
            self.assertEqual(_sha256(source), source_sha)

    def test_path_namespace_and_nonzero_source_wal_are_rejected(self) -> None:
        self.assertEqual(builder._source_label(self.source), self.source.name)
        with tempfile.TemporaryDirectory(
            prefix="architizer-path-test-"
        ) as temporary:
            root = Path(temporary)
            source = root / "source.db"
            output = root / "curated.db"
            report = root / "curated.md"
            _create_source_fixture(source)
            output_wal = Path(str(output) + "-wal")
            output_wal.write_bytes(b"")
            with self.assertRaises(builder.BuildError):
                builder.validate_build_paths(source, output, report)
            output_wal.unlink()
            source_wal = Path(str(source) + "-wal")
            source_wal.write_bytes(b"uncheckpointed")
            with self.assertRaises(builder.BuildError):
                builder._assert_source_sidecars_clean(source)

    def test_byte_determinism_and_no_clobber(self) -> None:
        self.assertTrue(self.result["deterministic_verified"])
        self.assertEqual(
            self.result["database_sha256"],
            self.result["deterministic_shadow_sha256"],
        )
        self.assertEqual(_sha256(self.output), self.result["database_sha256"])
        output_sha = _sha256(self.output)
        report_sha = _sha256(self.report)
        with self.assertRaises(builder.BuildError):
            builder.build(
                source_path=self.source,
                output_path=self.output,
                report_path=self.report,
                limit=None,
                expected_sha256=self.source_sha_before,
                expected_size=self.source_size,
                verify_deterministic=False,
            )
        self.assertEqual(_sha256(self.output), output_sha)
        self.assertEqual(_sha256(self.report), report_sha)
        self.assertEqual(_sha256(self.source), self.source_sha_before)


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SOURCE = REPO_ROOT / "data" / "crawl" / "architizer.db"
REAL_FULL = REPO_ROOT / "data" / "curated" / "architizer_curated_v1_3.db"
REAL_FULL_SHA256 = (
    "5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089"
)


class ArchitizerRealArtifactReadOnlyTests(unittest.TestCase):
    @unittest.skipUnless(REAL_SOURCE.exists(), "real Architizer crawler DB unavailable")
    def test_real_source_manifest_read_only(self) -> None:
        self.assertEqual(REAL_SOURCE.stat().st_size, builder.EXPECTED_SOURCE_SIZE)
        self.assertEqual(_sha256(REAL_SOURCE), builder.EXPECTED_SOURCE_SHA256)
        connection = builder.open_source(REAL_SOURCE)
        try:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(DISTINCT value) FROM json_each("
                    "(SELECT json_group_array(value) FROM "
                    "architizer_projects,json_each(categories)))"
                ).fetchone()[0],
                78,
            )
        finally:
            connection.close()

    @unittest.skipUnless(
        REAL_SOURCE.exists() and REAL_FULL.exists(),
        "full Architizer curated artifact unavailable",
    )
    def test_real_full_artifact_read_only(self) -> None:
        source_sha = _sha256(REAL_SOURCE)
        self.assertEqual(_sha256(REAL_FULL), REAL_FULL_SHA256)
        connection = _open_readonly(REAL_FULL)
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            snapshot = connection.execute(
                """
                SELECT source_sha256_before,source_sha256_after,query_only
                FROM source_snapshots
                """
            ).fetchone()
            self.assertEqual(snapshot["source_sha256_before"], source_sha)
            self.assertEqual(snapshot["source_sha256_after"], source_sha)
            self.assertEqual(snapshot["query_only"], 1)
            validation = json.loads(
                connection.execute(
                    "SELECT validation_json FROM build_runs"
                ).fetchone()[0]
            )
            lineage = connection.execute(
                """
                SELECT builder_version,schema_version,policy_version,
                       selected_project_count
                FROM build_runs
                """
            ).fetchone()
            self.assertEqual(lineage["builder_version"], builder.BUILDER_VERSION)
            self.assertEqual(lineage["schema_version"], SCHEMA_VERSION)
            self.assertEqual(lineage["policy_version"], POLICY_VERSION)
            self.assertEqual(lineage["selected_project_count"], 10_632)
            self.assertTrue(validation["passed"])
            self.assertEqual(
                validation["article_category_image_type_propagation_rows"],
                0,
            )
            self.assertEqual(validation["material_claims_without_source_evidence"], 0)
            self.assertEqual(validation["accepted_membership_error"], 0)
            self.assertEqual(validation["excluded_membership_error"], 0)
        finally:
            connection.close()
        self.assertEqual(_sha256(REAL_SOURCE), source_sha)


if __name__ == "__main__":
    unittest.main()
