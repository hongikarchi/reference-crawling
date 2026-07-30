import unittest

from canonical.divisare_curated_v2 import (
    ArticleKindEvidence,
    EVIDENCE_FAMILY_HTML_EXPLICIT,
    choose_primary_value,
    facet_status_v2,
    independence_key_for_claim,
    infer_article_kind_evidence,
    resolve_article_kind,
)


class ArticleKindPolicyTests(unittest.TestCase):
    def test_tag_only_evidence_stays_candidate(self):
        cases = [
            (
                {"plans-details": ["plans-of-single-family-houses"]},
                [],
                "drawing_feature",
            ),
            (
                {"ideas": ["ideas-for-houses"]},
                [],
                "concept_editorial",
            ),
            (
                {"topics": ["by-night"]},
                ["Night Photography"],
                "photo_feature",
            ),
        ]
        for album_tags, content_hints, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                evidence = infer_article_kind_evidence(
                    "Villa",
                    "villa",
                    album_tags,
                    content_hints,
                )
                resolution = resolve_article_kind(evidence)
                self.assertEqual("candidate", resolution.status)
                self.assertEqual(expected_kind, resolution.kind)

    def test_matching_tag_and_strong_lexical_evidence_stays_candidate(self):
        cases = [
            (
                "Villa Plans",
                {"plans-details": ["plans-of-single-family-houses"]},
                ["Plan"],
                "drawing_feature",
            ),
            (
                "Housing Concept",
                {"ideas": ["ideas-for-housing-blocks"]},
                [],
                "concept_editorial",
            ),
            (
                "Villa by Night",
                {"topics": ["by-night"]},
                ["Night Photography"],
                "photo_feature",
            ),
        ]
        for name, album_tags, content_hints, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                evidence = infer_article_kind_evidence(
                    name,
                    name.lower().replace(" ", "-"),
                    album_tags,
                    content_hints,
                )
                resolution = resolve_article_kind(evidence)
                self.assertEqual("candidate", resolution.status)
                self.assertEqual(expected_kind, resolution.kind)

    def test_explicit_html_evidence_can_confirm(self):
        resolution = resolve_article_kind(
            [
                ArticleKindEvidence(
                    "drawing_feature",
                    0.99,
                    EVIDENCE_FAMILY_HTML_EXPLICIT,
                    "dom:article-kind",
                    is_strong=True,
                    reason="Explicit article template marker.",
                )
            ]
        )

        self.assertEqual("confirmed", resolution.status)
        self.assertEqual("drawing_feature", resolution.kind)

    def test_conflicting_confirmed_kinds_are_ambiguous(self):
        evidence = infer_article_kind_evidence(
            "Plans and Models",
            "plans-and-models",
            {
                "plans-details": ["plans-of-single-family-houses"],
                "topics": ["architectural-models"],
            },
            ["Plan", "Model"],
        )

        resolution = resolve_article_kind(evidence)

        self.assertEqual("ambiguous", resolution.status)
        self.assertIsNone(resolution.kind)

    def test_empty_evidence_is_unresolved(self):
        resolution = resolve_article_kind([])

        self.assertEqual("unresolved", resolution.status)
        self.assertIsNone(resolution.kind)
        self.assertEqual(0.0, resolution.confidence)


class EvidenceIndependencePolicyTests(unittest.TestCase):
    def test_same_article_taxonomy_families_share_independence_key(self):
        plans_key = independence_key_for_claim(
            123,
            "divisare.taxonomy.plans-details",
        )
        ideas_key = independence_key_for_claim(
            123,
            "divisare.taxonomy.ideas",
        )

        self.assertEqual(plans_key, ideas_key)

    def test_different_articles_have_different_independence_keys(self):
        first = independence_key_for_claim(
            123,
            "divisare.taxonomy.plans-details",
        )
        second = independence_key_for_claim(
            456,
            "divisare.taxonomy.plans-details",
        )

        self.assertNotEqual(first, second)


class FacetResolutionPolicyTests(unittest.TestCase):
    def test_one_supporting_group_stays_candidate(self):
        status = facet_status_v2([], ["article:1:taxonomy"], 0.90)

        self.assertEqual("candidate", status)

    def test_two_independent_supporting_groups_confirm(self):
        status = facet_status_v2(
            [],
            ["article:1:taxonomy", "article:2:text"],
            0.75,
            supporting_article_count=2,
        )

        self.assertEqual("confirmed", status)

    def test_two_channels_from_one_article_stay_candidate(self):
        status = facet_status_v2(
            [],
            ["article:1:taxonomy", "article:1:text"],
            0.90,
            supporting_article_count=1,
        )

        self.assertEqual("candidate", status)

    def test_low_direct_claim_cannot_piggyback_on_supporting_groups(self):
        status = facet_status_v2(
            [0.84],
            ["article:1:taxonomy", "article:2:text"],
            0.95,
        )

        self.assertEqual("candidate", status)


class PrimaryValuePolicyTests(unittest.TestCase):
    def test_tied_multi_value_result_abstains(self):
        primary = choose_primary_value(
            [
                ("Housing", 0.90, 80),
                ("Public", 0.90, 80),
            ],
            allow_multi=True,
        )

        self.assertIsNone(primary)

    def test_clear_winner_is_selected_deterministically(self):
        primary = choose_primary_value(
            [
                ("Public", 0.90, 90),
                ("Housing", 0.95, 80),
            ],
            allow_multi=True,
        )

        self.assertEqual("Housing", primary)


if __name__ == "__main__":
    unittest.main()
