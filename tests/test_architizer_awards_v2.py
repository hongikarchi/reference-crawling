"""Offline tests for the source-specific Architizer awards-v2 parser."""

from __future__ import annotations

import unittest
from pathlib import Path

from crawl.architizer import awards_v2


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "architizer_awards_2026_sample.html"
)


class ArchitizerAwardsV2Tests(unittest.TestCase):
    def parse_fixture(self) -> dict:
        return awards_v2.parse_awards_track_snapshot(
            FIXTURE.read_text(encoding="utf-8"),
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )

    def test_card_boundary_preserves_project_and_multi_firm_relation(self) -> None:
        page = self.parse_fixture()
        record = page["records"][0]

        self.assertEqual(page["parse_status"], "complete")
        self.assertEqual(record["award_attribution_id"], 101)
        self.assertEqual(record["award_category"], "Concepts > Adaptive Reuse")
        self.assertEqual(
            record["award_category_path"], ["Concepts", "Adaptive Reuse"]
        )
        self.assertEqual(record["award_tiers"], ["Jury"])
        self.assertEqual(record["source_group_ordinal"], 0)
        self.assertEqual(record["source_card_ordinal"], 0)
        self.assertEqual(record["subject"]["kind"], "project")
        self.assertEqual(record["subject"]["slug"], "sample-project")
        self.assertEqual(
            [company["slug"] for company in record["companies"]],
            ["studio-one", "studio-two"],
        )
        self.assertEqual(record["parse_status"], "complete")

    def test_product_and_dual_tier_are_not_discarded(self) -> None:
        record = self.parse_fixture()["records"][1]

        self.assertEqual(record["award_attribution_id"], 102)
        self.assertEqual(record["award_tiers"], ["Jury", "Popular"])
        self.assertEqual(record["subject"]["kind"], "product")
        self.assertEqual(record["companies"][0]["kind"], "brand")
        self.assertEqual(record["companies"][0]["slug"], "sample-brand")

    def test_attribute_dom_identity_conflict_is_not_resolved(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            "https://architizer.com/projects/sample-project/",
            "https://architizer.com/projects/different-project/",
            1,
        )
        page = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )
        record = page["records"][0]

        self.assertEqual(page["parse_status"], "conflict")
        self.assertEqual(record["parse_status"], "conflict")
        self.assertIsNone(record["subject"])
        self.assertTrue(
            any(conflict["field"] == "subject" for conflict in record["conflicts"])
        )
        self.assertEqual(
            record["attribute_values"]["data-slug"], "sample-project"
        )
        self.assertEqual(
            record["dom_values"]["subject"]["slug"], "different-project"
        )

    def test_data_slug_conflict_is_not_resolved(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            'data-slug="sample-project"',
            'data-slug="different-project"',
            1,
        )
        record = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )["records"][0]

        self.assertEqual(record["parse_status"], "conflict")
        self.assertIsNone(record["subject"])
        self.assertTrue(
            any(conflict["field"] == "subject_slug" for conflict in record["conflicts"])
        )

    def test_multiple_dom_subject_anchors_are_not_resolved(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            '<a class="text-dark" '
            'href="https://architizer.com/projects/sample-project/">'
            "Sample Project</a>",
            '<a class="text-dark" '
            'href="https://architizer.com/projects/sample-project/">'
            "Sample Project</a>\n"
            "            <a class=\"text-dark\" "
            "href=\"https://architizer.com/projects/second-project/\">"
            "Second Project</a>",
            1,
        )
        record = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )["records"][0]

        self.assertEqual(record["parse_status"], "conflict")
        self.assertIsNone(record["subject"])
        self.assertTrue(
            any(
                conflict.get("reason") == "multiple_subject_anchors"
                for conflict in record["conflicts"]
            )
        )
        self.assertEqual(record["dom_values"]["subject"]["slug"], "sample-project")

    def test_noncanonical_entity_urls_never_resolve(self) -> None:
        invalid_urls = (
            "http://architizer.com/projects/sample-project/",
            "https://user@architizer.com/projects/sample-project/",
            "https://architizer.com:443/projects/sample-project/",
            "https://architizer.com/projects/sample-project/?notfound=1",
            "https://architizer.com/projects/sample-project/#fragment",
            "https://www.architizer.com/projects/sample-project/",
            "/projects/sample-project/?notfound=1",
            "/architects/sample-project/",
            "/projects/./",
            "/projects/%2E%2E/",
            "/projects/sample%2Fproject/",
            "/projects/sample%5Cproject/",
            "/projects/sample%252Fproject/",
            "/projects/sample%00project/",
            "/projects/sample%FFproject/",
            "/projects/sample%09project/",
            "/projects/sample%ZZproject/",
            "/projects/sample%25ZZproject/",
            "/projects/sample%E2%80%AEproject/",
            "/projects/sample project/",
        )
        fixture = FIXTURE.read_text(encoding="utf-8")
        for invalid_url in invalid_urls:
            with self.subTest(url=invalid_url):
                html = fixture.replace(
                    'data-url="/projects/sample-project/"',
                    f'data-url="{invalid_url}"',
                    1,
                )
                record = awards_v2.parse_awards_track_snapshot(
                    html,
                    source_url="https://winners.architizer.com/2026/Plus/",
                    award_year=2026,
                    award_track="Plus",
                )["records"][0]

                self.assertIsNone(record["subject"])
                self.assertIn("attribute_subject", record["missing"])

    def test_unknown_tier_conflict_is_not_partially_resolved(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            'data-types="Jury Winner"',
            'data-types="Jury Winner,Unknown New Tier"',
            1,
        )
        record = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )["records"][0]

        self.assertEqual(record["parse_status"], "conflict")
        self.assertIsNone(record["award_tiers"])
        self.assertTrue(
            any(
                conflict.get("reason") == "unknown_tier_label"
                for conflict in record["conflicts"]
            )
        )

    def test_empty_page_is_explicit_no_content(self) -> None:
        page = awards_v2.parse_awards_track_snapshot(
            "<html><title>No winners</title><body></body></html>",
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )

        self.assertEqual(page["parse_status"], "no_content")
        self.assertEqual(page["record_count"], 0)
        self.assertEqual(page["records"], [])

    def test_malformed_company_attribute_is_preserved_as_conflict(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            "data-company-urls=\"['/firms/studio-one/', '/firms/studio-two/']\"",
            "data-company-urls=\"not-a-list\"",
            1,
        )
        page = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )
        record = page["records"][0]

        self.assertEqual(record["parse_status"], "conflict")
        self.assertIsNone(record["companies"])
        self.assertEqual(
            record["attribute_values"]["data-company-urls"], "not-a-list"
        )
        self.assertEqual(len(record["dom_values"]["companies"]), 2)

    def test_image_conflict_has_no_resolved_image(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            "https://images.example/sample.jpg?w=388",
            "https://images.example/different.jpg?w=388",
            1,
        )
        page = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )
        record = page["records"][0]

        self.assertEqual(record["parse_status"], "conflict")
        self.assertEqual(record["image_resolution_status"], "conflict")
        self.assertIsNone(record["image_url"])
        self.assertTrue(
            any(conflict["field"] == "image_url" for conflict in record["conflicts"])
        )

    def test_same_image_path_on_different_host_is_a_conflict(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            "https://images.example/sample.jpg?w=388",
            "https://untrusted.example/sample.jpg?w=388",
            1,
        )
        record = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )["records"][0]

        self.assertEqual(record["image_resolution_status"], "conflict")
        self.assertIsNone(record["image_url"])

    def test_malformed_image_url_is_preserved_as_conflict_not_parser_error(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            "https://images.example/sample.jpg?w=1680",
            "https://[invalid/sample.jpg?w=1680",
            1,
        )
        record = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )["records"][0]

        self.assertEqual(record["image_resolution_status"], "conflict")
        self.assertIsNone(record["image_url"])

    def test_one_sided_image_evidence_is_explicit_and_unresolved(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            '<img data-src="https://images.example/sample.jpg?w=388" />',
            "",
            1,
        )
        record = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )["records"][0]

        self.assertEqual(record["image_resolution_status"], "attribute_only")
        self.assertIsNone(record["image_url"])
        self.assertIn("image_url_attribute_only", record["warnings"])

    def test_duplicate_attribution_id_marks_each_page_record_conflict(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8").replace(
            "projects.awardattribution.102",
            "projects.awardattribution.101",
            1,
        )
        page = awards_v2.parse_awards_track_snapshot(
            html,
            source_url="https://winners.architizer.com/2026/Plus/",
            award_year=2026,
            award_track="Plus",
        )

        self.assertEqual(page["duplicate_award_attribution_ids"], [101])
        self.assertEqual(page["status_counts"], {"conflict": 2})
        self.assertTrue(all(row["parse_status"] == "conflict" for row in page["records"]))
        self.assertTrue(all(row["award_attribution_id"] is None for row in page["records"]))
        self.assertTrue(
            all(row["award_attribution_global_id"] is None for row in page["records"])
        )
        self.assertTrue(
            all(
                row["attribute_values"]["data-id"]
                == "projects.awardattribution.101"
                for row in page["records"]
            )
        )
        self.assertTrue(
            all(
                any(
                    conflict.get("reason") == "duplicate_on_page"
                    for conflict in row["conflicts"]
                )
                for row in page["records"]
            )
        )


if __name__ == "__main__":
    unittest.main()
