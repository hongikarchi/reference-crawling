import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from canonical.divisare_curated import (
    clean_description,
    clean_location,
    divisare_asset_identity,
    is_generic_building_name,
    mappings_for_tag,
    normalize_country,
)
from tools.build_divisare_curated import (
    build,
    file_sha256,
    nonregenerable_state,
    promote_temp_output,
    years_compatible,
)


RAW_SCHEMA = """
CREATE TABLE divisare_architects (
    id INTEGER PRIMARY KEY,
    slug TEXT,
    name TEXT,
    description TEXT,
    country TEXT,
    city TEXT,
    website TEXT,
    phone TEXT,
    project_count_seen INTEGER,
    fetched_at TEXT
);
CREATE TABLE divisare_projects (
    id INTEGER PRIMARY KEY,
    slug TEXT,
    name TEXT,
    architect_ids TEXT,
    architect_names TEXT,
    location_country TEXT,
    location_city TEXT,
    project_year INTEGER,
    area_sqm REAL,
    abstract TEXT,
    description TEXT,
    tag_slugs TEXT,
    cover_image_url TEXT,
    gallery_urls TEXT,
    credits TEXT,
    fetched_at TEXT
);
CREATE TABLE divisare_tags (
    slug TEXT PRIMARY KEY,
    name TEXT,
    curated INTEGER,
    project_count_seen INTEGER,
    fetched_at TEXT
);
CREATE TABLE divisare_albums (
    slug TEXT PRIMARY KEY,
    name TEXT,
    kind TEXT,
    child_count INTEGER,
    fetched_at TEXT
);
CREATE TABLE divisare_album_membership (
    album_slug TEXT,
    child_slug TEXT,
    child_name TEXT,
    child_url TEXT,
    PRIMARY KEY(album_slug,child_slug)
);
CREATE TABLE pending_tags (
    slug TEXT PRIMARY KEY,
    status TEXT,
    discovered_at TEXT,
    fetched_at TEXT,
    error TEXT
);
"""


class DivisarePolicyTests(unittest.TestCase):
    def test_description_cleaner_removes_only_known_ui(self):
        raw = (
            "Photographer Add to collection Choose collection... New collection... "
            "A real project description."
        )
        result = clean_description(raw)
        self.assertEqual(result.removed_ui_markers, 1)
        self.assertNotIn("Add to collection", result.text)
        self.assertIn("A real project description.", result.text)
        self.assertEqual(result.quality_status, "ui_removed_caption_residue_possible")

    def test_cover_and_gallery_urls_share_asset_key(self):
        cover = (
            "https://images.divisare.com//image/upload/"
            "c_fit,f_jpg,q_80,w_1200/v1/project_images/1743878/COLL-p03.jpg"
        )
        gallery = (
            "https://images.divisare.com//images/f_auto,q_auto,w_auto/"
            "v1/project_images/1743878/COLL-p03/project-name.jpg"
        )
        cover_identity = divisare_asset_identity(cover)
        gallery_identity = divisare_asset_identity(gallery)
        self.assertIsNotNone(cover_identity)
        self.assertEqual(cover_identity.asset_key, gallery_identity.asset_key)
        self.assertEqual(cover_identity.asset_key, "divisare|1743878|COLL-p03")
        self.assertIsNone(cover_identity.delivery_version)

    def test_cloudinary_asset_key_keeps_delivery_version(self):
        public_id = "7f2fedf69ca074197bf77b221731ff5cca8a0812"
        cover = (
            "https://images.divisare.com/image/upload/c_fit,w_1200/"
            f"v1678438203/{public_id}.jpg"
        )
        gallery_same_version = (
            "https://images.divisare.com/images/f_auto,q_auto,w_auto/"
            f"v1678438203/{public_id}/the-gyaan-center.jpg"
        )
        gallery_next_version = (
            "https://images.divisare.com/images/f_auto,q_auto,w_auto/"
            f"v1678438207/{public_id}/the-gyaan-center.jpg"
        )

        cover_identity = divisare_asset_identity(cover)
        same_identity = divisare_asset_identity(gallery_same_version)
        next_identity = divisare_asset_identity(gallery_next_version)

        self.assertEqual(cover_identity.asset_key, same_identity.asset_key)
        self.assertEqual(
            cover_identity.asset_key,
            f"divisare|{public_id}|v1678438203",
        )
        self.assertEqual(cover_identity.delivery_version, "v1678438203")
        self.assertNotEqual(cover_identity.asset_key, next_identity.asset_key)
        self.assertEqual(next_identity.delivery_version, "v1678438207")

    def test_tag_policy_keeps_scope_and_specificity(self):
        plans = mappings_for_tag(
            "plans-details", "plans-of-schools", "Plans of Schools"
        )
        self.assertTrue(
            any(
                m.axis == "content_hint"
                and m.target_scope == "article"
                and m.value == "Plan"
                for m in plans
            )
        )
        self.assertTrue(
            any(
                m.axis == "program"
                and m.target_scope == "building"
                and m.value == "Education"
                for m in plans
            )
        )

        outdoor = mappings_for_tag("elements", "outdoor-stairs", "Outdoor Stairs")
        column = mappings_for_tag("elements", "columns", "Columns")
        self.assertEqual(outdoor[0].value, "Outdoor Stair")
        self.assertEqual(outdoor[0].search_tier, "primary")
        self.assertEqual(column[0].value, "Column")
        self.assertEqual(column[0].search_tier, "secondary")

        geo = mappings_for_tag("houses", "spanish-houses", "Spanish Houses")
        self.assertTrue(
            any(m.axis == "country_candidate" and m.value == "Spain" for m in geo)
        )
        self.assertFalse(any(m.axis == "city" for m in geo))

        private_room = mappings_for_tag(
            "private-interiors", "kitchens", "Kitchens"
        )
        self.assertTrue(
            any(
                m.axis == "room_type" and m.target_scope == "article"
                for m in private_room
            )
        )
        public_program = mappings_for_tag(
            "public-interiors", "libraries", "Libraries"
        )
        self.assertTrue(
            any(
                m.axis == "program"
                and m.target_scope == "building"
                and m.mapping_kind == "supporting"
                for m in public_program
            )
        )
        private_garden = mappings_for_tag(
            "types", "private-gardens", "Private Gardens"
        )
        self.assertFalse(any(m.axis == "program" for m in private_garden))
        self.assertTrue(
            any(
                m.axis == "architectural_element"
                and m.value == "Private Garden"
                for m in private_garden
            )
        )
        installation = mappings_for_tag(
            "types", "installations", "Installations"
        )
        self.assertFalse(any(m.axis == "program" for m in installation))
        self.assertTrue(
            any(
                m.axis == "work_type" and m.value == "Installation"
                for m in installation
            )
        )

        structure = mappings_for_tag(
            "elements", "wooden-structures", "Wooden Structures"
        )
        self.assertTrue(
            any(
                m.axis == "structural_material"
                and m.mapping_kind == "direct"
                for m in structure
            )
        )
        self.assertTrue(
            any(
                m.axis == "structural_system"
                and m.mapping_kind == "supporting"
                for m in structure
            )
        )

    def test_generic_names_and_year_policy_are_strict(self):
        self.assertTrue(is_generic_building_name("House A"))
        self.assertTrue(is_generic_building_name("Villa 12"))
        self.assertFalse(is_generic_building_name("Museum of Modern Art"))
        self.assertTrue(years_compatible(2020, 2020))
        self.assertFalse(years_compatible(2020, None))
        self.assertFalse(years_compatible(None, None))

    def test_location_sentinels_are_not_treated_as_real_values(self):
        self.assertIsNone(clean_location("-"))
        self.assertIsNone(clean_location("Unknown"))
        self.assertIsNone(normalize_country("- Nis"))
        self.assertEqual(clean_location("Nis"), "Nis")


class DivisareBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "divisare.db"
        self.output = self.root / "divisare_curated.db"
        self.report = self.root / "report.md"
        self._make_source_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _add_tag(self, album, slug, name):
        conn = sqlite3.connect(self.source)
        try:
            conn.execute(
                """
                INSERT INTO divisare_album_membership(
                    album_slug,child_slug,child_name,child_url
                ) VALUES (?,?,?,'/' || ?)
                """,
                (album, slug, name, slug),
            )
            conn.execute(
                """
                INSERT INTO pending_tags(slug,status,discovered_at)
                VALUES (?,'done','2026-01-01')
                """,
                (slug,),
            )
            conn.execute(
                """
                INSERT INTO divisare_tags(
                    slug,name,curated,project_count_seen,fetched_at
                ) VALUES (?,?,1,1,'2026-01-01')
                """,
                (slug, name),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_project(self, project_id, name, tags, year=2022, cover=None):
        conn = sqlite3.connect(self.source)
        try:
            slug = name.lower().replace(" ", "-")
            cover = cover or (
                "https://images.divisare.com//image/upload/"
                f"c_fit,f_jpg,q_80,w_1200/v1/project_images/{project_id}/main.jpg"
            )
            conn.execute(
                """
                INSERT INTO divisare_projects(
                    id,slug,name,architect_ids,architect_names,
                    location_country,location_city,project_year,area_sqm,
                    abstract,description,tag_slugs,cover_image_url,gallery_urls,
                    credits,fetched_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    project_id,
                    slug,
                    name,
                    json.dumps([10]),
                    json.dumps(["Studio A"]),
                    "South Korea",
                    "Seoul",
                    year,
                    None,
                    f"Abstract {project_id}",
                    f"Description {project_id}",
                    json.dumps(tags),
                    cover,
                    json.dumps([]),
                    json.dumps({}),
                    "2026-01-01",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _insert_downstream_model_run(conn):
        return conn.execute(
            """
            INSERT INTO build_runs(
                started_at,completed_at,status,builder_version,schema_version,
                taxonomy_version,text_processor_version,asset_key_version,
                cluster_version,resolver_version,source_db_path,output_db_path
            ) VALUES (
                '2026-01-02','2026-01-02','complete','model-builder-v1',2,
                'divisare-taxonomy-v1.2','model-v1','divisare-asset-key-v1.0',
                'divisare-cluster-v1.1','divisare-resolver-v1.2',
                'fixture-source','fixture-output'
            )
            """
        ).lastrowid

    def _make_source_db(self):
        conn = sqlite3.connect(self.source)
        conn.executescript(RAW_SCHEMA)
        albums = [
            ("types", "Types", "tag_album"),
            ("houses", "Houses", "tag_album"),
            ("elements", "Elements", "tag_album"),
            ("materiality", "Materiality", "tag_album"),
            ("plans-details", "Plans & Details", "tag_album"),
            ("topics", "Topics", "tag_album"),
            ("cities", "Cities", "tag_album"),
        ]
        conn.executemany(
            """
            INSERT INTO divisare_albums(slug,name,kind,child_count,fetched_at)
            VALUES (?,?,?,0,'2026-01-01')
            """,
            albums,
        )
        members = [
            ("types", "museums", "Museums"),
            ("houses", "spanish-houses", "Spanish Houses"),
            ("elements", "outdoor-stairs", "Outdoor Stairs"),
            ("elements", "columns", "Columns"),
            ("materiality", "timber", "Timber"),
            ("materiality", "white", "White"),
            ("materiality", "exploring-patterns", "Exploring Patterns"),
            ("plans-details", "plans-of-schools", "Plans of Schools"),
            ("topics", "architectural-drawings", "Architectural Drawings"),
            ("topics", "restored-and-reused", "Restored and Reused"),
            ("cities", "madrid", "Madrid"),
        ]
        conn.executemany(
            """
            INSERT INTO divisare_album_membership(
                album_slug,child_slug,child_name,child_url
            ) VALUES (?,?,?,'/' || ?)
            """,
            [(album, slug, name, slug) for album, slug, name in members],
        )
        conn.executemany(
            """
            INSERT INTO pending_tags(slug,status,discovered_at)
            VALUES (?,'done','2026-01-01')
            """,
            [(slug,) for _, slug, _ in members],
        )
        conn.executemany(
            """
            INSERT INTO divisare_tags(
                slug,name,curated,project_count_seen,fetched_at
            ) VALUES (?,?,1,20,'2026-01-01')
            """,
            [(slug, name) for _, slug, name in members],
        )
        conn.execute(
            """
            INSERT INTO divisare_architects(
                id,slug,name,country,city,project_count_seen,fetched_at
            ) VALUES (10,'studio-a','Studio A','South Korea','Seoul',3,'2026-01-01')
            """
        )
        marker = "Add to collection Choose collection... New collection..."
        cover_1 = (
            "https://images.divisare.com//image/upload/"
            "c_fit,f_jpg,q_80,w_1200/v1/project_images/100/plan-main.jpg"
        )
        gallery_1 = (
            "https://images.divisare.com//images/f_auto,q_auto,w_auto/"
            "v1/project_images/100/plan-main/museum-a.jpg"
        )
        projects = [
            (
                1,
                "studio-a-museum-a",
                "Museum A",
                ["museums"],
                "South Korea",
                "Seoul",
                2020,
                cover_1,
                [gallery_1],
                f"Photo Name {marker} Museum description one.",
            ),
            (
                2,
                "studio-a-museum-a-second-feature",
                "Museum A",
                ["museums", "plans-of-schools"],
                "South Korea",
                "Seoul",
                2020,
                (
                    "https://images.divisare.com//image/upload/"
                    "c_fit,f_jpg,q_80,w_1200/v1453977576/vcxepqgqykps62qss3to.jpg"
                ),
                [
                    "https://images.divisare.com//images/f_auto,q_auto,w_auto/"
                    "v1453977576/vcxepqgqykps62qss3to/museum-a.jpg"
                ],
                f"Second Photographer {marker} Museum description two.",
            ),
            (
                3,
                "studio-a-spanish-house",
                "Spanish House",
                [
                    "spanish-houses",
                    "timber",
                    "white",
                    "exploring-patterns",
                    "outdoor-stairs",
                    "columns",
                    "architectural-drawings",
                    "restored-and-reused",
                    "madrid",
                ],
                "Spain",
                "Madrid",
                2021,
                (
                    "https://images.divisare.com//image/upload/"
                    "c_fit,f_jpg,q_80,w_1200/v1/project_images/300/house-photo.jpg"
                ),
                [
                    "https://images.divisare.com//images/f_auto,q_auto,w_auto/"
                    "v1/project_images/300/house-photo/spanish-house.jpg"
                ],
                f"House Photographer {marker} House description.",
            ),
        ]
        for (
            project_id,
            slug,
            name,
            tags,
            country,
            city,
            year,
            cover,
            gallery,
            description,
        ) in projects:
            conn.execute(
                """
                INSERT INTO divisare_projects(
                    id,slug,name,architect_ids,architect_names,
                    location_country,location_city,project_year,area_sqm,
                    abstract,description,tag_slugs,cover_image_url,gallery_urls,
                    credits,fetched_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    project_id,
                    slug,
                    name,
                    json.dumps([10]),
                    json.dumps(["Studio A"]),
                    country,
                    city,
                    year,
                    None,
                    f"Abstract {project_id}",
                    description,
                    json.dumps(tags),
                    cover,
                    json.dumps(gallery),
                    json.dumps({"photo": [f"Photographer {project_id}"]}),
                    "2026-01-01",
                ),
            )
        conn.commit()
        conn.close()

    def test_full_fixture_build(self):
        result = build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=False,
        )
        self.assertEqual(result["validation"]["integrity_check"], "ok")
        self.assertTrue(self.report.exists())

        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM buildings").fetchone()[0], 2
            )

            merged = conn.execute(
                "SELECT * FROM buildings WHERE article_count=2"
            ).fetchone()
            self.assertIsNotNone(merged)
            self.assertEqual(merged["program"], "Museum")

            house = conn.execute(
                "SELECT * FROM buildings WHERE name='Spanish House'"
            ).fetchone()
            self.assertEqual(house["program"], "Housing")

            facets = {
                (r["axis"], r["value"]): r["search_tier"]
                for r in conn.execute(
                    """
                    SELECT axis,value,search_tier
                    FROM building_facets
                    WHERE building_id=?
                    """,
                    (house["building_id"],),
                )
            }
            self.assertEqual(facets[("material", "timber")], "primary")
            self.assertEqual(facets[("color", "white")], "primary")
            self.assertEqual(
                facets[("architectural_element", "Outdoor Stair")], "primary"
            )
            self.assertEqual(
                facets[("architectural_element", "Column")], "secondary"
            )

            plan_claim = conn.execute(
                """
                SELECT * FROM attribute_claims
                WHERE article_id=2
                  AND axis='content_hint'
                  AND value_normalized='Plan'
                """
            ).fetchone()
            self.assertEqual(plan_claim["scope"], "article")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM image_classifications"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM image_assets
                    WHERE asset_key='divisare|100|plan-main'
                    """
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM v_unmapped_tags").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM image_hashes WHERE status='pending'"
                ).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM source_image_occurrences"
                ).fetchone()[0],
                conn.execute(
                    "SELECT SUM(image_count) FROM source_articles"
                ).fetchone()[0],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM article_attributions"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_single_supporting_tag_does_not_fill_program(self):
        self._insert_project(4, "Plan Feature", ["plans-of-schools"])
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            building = conn.execute(
                "SELECT * FROM buildings WHERE primary_article_id=4"
            ).fetchone()
            self.assertIsNone(building["program"])
            facet = conn.execute(
                """
                SELECT status,direct_claim_count,supporting_claim_count,source_count
                FROM building_facets
                WHERE building_id=? AND axis='program' AND value='Education'
                """,
                (building["building_id"],),
            ).fetchone()
            self.assertEqual(facet["status"], "candidate")
            self.assertEqual(facet["direct_claim_count"], 0)
            self.assertEqual(facet["supporting_claim_count"], 1)
            self.assertEqual(facet["source_count"], 1)
        finally:
            conn.close()

    def test_conflicting_direct_programs_abstain(self):
        self._add_tag("types", "primary-schools", "Primary Schools")
        self._insert_project(
            4,
            "Conflicted Project",
            ["museums", "primary-schools"],
        )
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            building = conn.execute(
                "SELECT * FROM buildings WHERE primary_article_id=4"
            ).fetchone()
            self.assertIsNone(building["program"])
            self.assertEqual(building["needs_review"], 1)
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM building_facets
                    WHERE building_id=?
                      AND axis='program'
                      AND status='confirmed'
                      AND role='primary'
                    """,
                    (building["building_id"],),
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_missing_year_and_generic_names_are_review_only(self):
        self._insert_project(4, "Yearless Project", ["museums"], year=None)
        self._insert_project(5, "Yearless Project", ["museums"], year=None)
        self._insert_project(6, "House A", ["spanish-houses"], year=2022)
        self._insert_project(7, "House A", ["spanish-houses"], year=2022)
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        conn = sqlite3.connect(self.output)
        try:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT building_id)
                    FROM building_articles
                    WHERE article_id IN (4,5)
                    """
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT building_id)
                    FROM building_articles
                    WHERE article_id IN (6,7)
                    """
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM article_match_candidates
                    WHERE article_id_a IN (4,6)
                      AND article_id_b IN (5,7)
                      AND status='open'
                    """
                ).fetchone()[0],
                2,
            )
        finally:
            conn.close()

    def test_malformed_image_url_is_preserved(self):
        self._insert_project(
            4,
            "Malformed Image",
            ["museums"],
            cover="https://example.com/not-a-divisare-image.jpg",
        )
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            occurrence = conn.execute(
                """
                SELECT * FROM source_image_occurrences
                WHERE article_id=4 AND role='cover'
                """
            ).fetchone()
            self.assertEqual(occurrence["parse_status"], "malformed")
            self.assertEqual(
                occurrence["raw_url"],
                "https://example.com/not-a-divisare-image.jpg",
            )
            self.assertIsNotNone(occurrence["parse_error"])
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM article_image_occurrences WHERE article_id=4"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_path_collision_is_rejected_without_touching_source(self):
        with self.assertRaises(ValueError):
            build(
                source_path=self.source,
                output_path=self.source,
                report_path=self.report,
                limit_rows=None,
                replace=True,
                skip_source_hash=True,
            )
        conn = sqlite3.connect(self.source)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM divisare_projects").fetchone()[0],
                3,
            )
        finally:
            conn.close()

    def test_pristine_output_is_immutable(self):
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        self.assertEqual(nonregenerable_state(self.output), {})
        with self.assertRaisesRegex(RuntimeError, "immutable"):
            build(
                source_path=self.source,
                output_path=self.output,
                report_path=self.report,
                limit_rows=None,
                replace=True,
                skip_source_hash=True,
            )
        conn = sqlite3.connect(self.output)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0],
                3,
            )
        finally:
            conn.close()

    def test_promotion_does_not_clobber_output_created_during_build(self):
        temp_output = self.output.with_suffix(".db.tmp")
        temp_output.write_bytes(b"completed build")
        self.output.write_bytes(b"concurrent output")
        with self.assertRaises(RuntimeError):
            promote_temp_output(
                temp_path=temp_output,
                output_path=self.output,
                output_existed_at_start=False,
                initial_output_sha256=None,
            )
        self.assertEqual(self.output.read_bytes(), b"concurrent output")
        self.assertTrue(temp_output.exists())

    def test_promotion_rechecks_enrichment_added_during_build(self):
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        initial_sha256 = file_sha256(self.output)
        temp_output = self.output.with_suffix(".db.tmp")
        shutil.copyfile(self.output, temp_output)
        conn = sqlite3.connect(self.output)
        try:
            run_id = self._insert_downstream_model_run(conn)
            conn.execute(
                """
                INSERT INTO article_text_versions(
                    article_id,text_kind,text,quality_status,processor_version,
                    is_current,checksum,run_id
                ) VALUES (
                    1,'model_summary','Concurrent model text.','clean','model-v1',
                    1,?,?
                )
                """,
                ("2" * 64, run_id),
            )
            conn.commit()
        finally:
            conn.close()
        state = nonregenerable_state(self.output)
        self.assertEqual(state["model_text_enrichment"], 1)
        self.assertGreater(state["downstream_or_unknown_build_runs"], 0)
        with self.assertRaises(RuntimeError):
            promote_temp_output(
                temp_path=temp_output,
                output_path=self.output,
                output_existed_at_start=True,
                initial_output_sha256=initial_sha256,
            )
        conn = sqlite3.connect(self.output)
        try:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM article_text_versions
                    WHERE processor_version='model-v1'
                    """
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()
        self.assertTrue(temp_output.exists())

    def test_replace_refuses_to_discard_phash_work(self):
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        conn = sqlite3.connect(self.output)
        try:
            asset_key = conn.execute(
                "SELECT asset_key FROM image_hashes LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE image_hashes
                SET status='success',hash_bits=256,hash_hex=?,
                    attempt_count=1,computed_at='2026-01-01'
                WHERE asset_key=?
                """,
                ("0" * 64, asset_key),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(RuntimeError):
            build(
                source_path=self.source,
                output_path=self.output,
                report_path=self.report,
                limit_rows=None,
                replace=True,
                skip_source_hash=True,
            )
        conn = sqlite3.connect(self.output)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM image_hashes WHERE status='success'"
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_replace_refuses_to_discard_model_text(self):
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        conn = sqlite3.connect(self.output)
        try:
            run_id = self._insert_downstream_model_run(conn)
            conn.execute(
                """
                INSERT INTO article_text_versions(
                    article_id,text_kind,text,quality_status,processor_version,
                    is_current,checksum,run_id
                ) VALUES (
                    1,'model_summary','Model-enriched text.','clean','model-v1',
                    1,?,?
                )
                """,
                ("1" * 64, run_id),
            )
            conn.commit()
        finally:
            conn.close()
        state = nonregenerable_state(self.output)
        self.assertEqual(state["model_text_enrichment"], 1)
        self.assertGreater(state["downstream_or_unknown_build_runs"], 0)
        with self.assertRaises(RuntimeError):
            build(
                source_path=self.source,
                output_path=self.output,
                report_path=self.report,
                limit_rows=None,
                replace=True,
                skip_source_hash=True,
            )
        conn = sqlite3.connect(self.output)
        try:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM article_text_versions
                    WHERE processor_version='model-v1'
                    """
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_missing_architect_index_record_is_preserved(self):
        conn = sqlite3.connect(self.source)
        try:
            conn.execute(
                """
                UPDATE divisare_projects
                SET architect_ids=?,architect_names=?
                WHERE id=3
                """,
                (json.dumps([999]), json.dumps(["Missing Studio"])),
            )
            conn.commit()
        finally:
            conn.close()
        build(
            source_path=self.source,
            output_path=self.output,
            report_path=self.report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            architect = conn.execute(
                "SELECT * FROM source_architects WHERE architect_id=999"
            ).fetchone()
            self.assertEqual(
                architect["record_source"], "project_reference_aligned"
            )
            self.assertEqual(architect["name"], "Missing Studio")
            article_architect = conn.execute(
                "SELECT * FROM article_architects WHERE article_id=3"
            ).fetchone()
            self.assertEqual(article_architect["architect_id"], 999)
            self.assertEqual(article_architect["architect_name"], "Missing Studio")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
