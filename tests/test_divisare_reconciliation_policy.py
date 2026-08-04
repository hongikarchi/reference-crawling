import unittest

from canonical.divisare_reconciliation import (
    AREA_SCOPE_CANDIDATE_CONFIDENCE,
    AREA_UNMATCHED_UNIT_CANDIDATE_CONFIDENCE,
    METADATA_VERSION,
    POLICY_VERSION,
    SCHEMA_VERSION,
    SQFT_TO_SQM,
    parse_area_evidence,
    resolve_area,
    resolve_city,
    resolve_country,
    resolve_description,
    resolve_name,
    resolve_year,
)


class VersionTests(unittest.TestCase):
    def test_versions_are_pinned(self):
        self.assertEqual(5, SCHEMA_VERSION)
        self.assertEqual("divisare-metadata-v2.2", METADATA_VERSION)
        self.assertEqual("divisare-metadata-reconciliation-v1.1", POLICY_VERSION)


class ScalarResolutionTests(unittest.TestCase):
    def test_equivalent_name_keeps_parent_display(self):
        result = resolve_name("Huanglong Mountain Museum", "Huanglong-Mountain Museum")

        self.assertEqual("Huanglong Mountain Museum", result.value)
        self.assertEqual("parent", result.source)
        self.assertEqual("confirmed", result.status)
        self.assertFalse(result.needs_review)

    def test_missing_parent_is_filled_from_recrawl(self):
        result = resolve_city(None, "Madrid")

        self.assertEqual("Madrid", result.value)
        self.assertEqual("recrawl", result.source)
        self.assertEqual("filled", result.status)

    def test_true_conflict_preserves_parent_and_evidence(self):
        result = resolve_city("Madrid", "Barcelona")

        self.assertEqual("Madrid", result.value)
        self.assertEqual("conflict", result.status)
        self.assertTrue(result.needs_review)
        self.assertEqual("Madrid", result.conflict.parent_value)
        self.assertEqual("Barcelona", result.conflict.recrawl_value)

    def test_country_aliases_confirm_parent_canonical_value(self):
        result = resolve_country("South Korea", "Korea, Republic of")

        self.assertEqual("South Korea", result.value)
        self.assertEqual("confirmed", result.status)

    def test_country_trailing_delimiter_is_removed(self):
        result = resolve_country("France -", "France")

        self.assertEqual("France", result.value)
        self.assertEqual("confirmed", result.status)

    def test_leading_country_delimiter_is_not_adopted(self):
        result = resolve_country(None, "- Nis")

        self.assertIsNone(result.value)
        self.assertEqual("invalid", result.status)
        self.assertTrue(result.needs_review)
        self.assertEqual("leading_location_delimiter", result.conflict.reason)

    def test_city_delimiter_only_is_not_adopted(self):
        result = resolve_city(None, "-")

        self.assertIsNone(result.value)
        self.assertEqual("invalid", result.status)
        self.assertEqual("location_delimiter_only", result.conflict.reason)

    def test_year_equivalence_uses_integer_value(self):
        result = resolve_year("2020", 2020)

        self.assertEqual(2020, result.value)
        self.assertEqual("confirmed", result.status)

    def test_invalid_parent_year_is_replaced_but_kept_as_review_evidence(self):
        result = resolve_year(9, 2020)

        self.assertEqual(2020, result.value)
        self.assertEqual("filled_with_invalid_parent", result.status)
        self.assertTrue(result.needs_review)
        self.assertEqual("year_out_of_range", result.conflict.reason)

    def test_invalid_years_are_quarantined(self):
        for value in (1, 9, 12, 999, 2101):
            with self.subTest(value=value):
                result = resolve_year(None, value)
                self.assertIsNone(result.value)
                self.assertEqual("invalid", result.status)
                self.assertTrue(result.needs_review)

    def test_area_uses_tolerance_and_keeps_parent(self):
        result = resolve_area(100.0, 100.005)

        self.assertEqual(100.0, result.value)
        self.assertEqual("confirmed", result.status)

    def test_area_conflict_preserves_parent(self):
        result = resolve_area(100.0, 120.0)

        self.assertEqual(100.0, result.value)
        self.assertEqual("conflict", result.status)
        self.assertTrue(result.needs_review)


class DescriptionResolutionTests(unittest.TestCase):
    def test_dom_prose_success_is_accepted(self):
        result = resolve_description(
            True,
            "New clean prose.",
            "success",
            "success",
            "dom_prose_paragraphs",
        )

        self.assertEqual("New clean prose.", result.value)
        self.assertEqual("recrawl", result.source)
        self.assertEqual("accepted", result.status)
        self.assertFalse(result.needs_review)

    def test_partial_is_candidate_even_when_parent_exists(self):
        result = resolve_description(
            True,
            "Fallback DOM text",
            "success",
            "partial",
            "dom_text_fallback_review",
        )

        self.assertEqual("Fallback DOM text", result.value)
        self.assertEqual("recrawl_candidate", result.source)
        self.assertEqual("candidate_partial", result.status)
        self.assertTrue(result.needs_review)

    def test_no_content_does_not_revive_flattened_parent_text(self):
        result = resolve_description(
            True,
            None,
            "success",
            "no_content",
            "no_prose_content",
        )

        self.assertIsNone(result.value)
        self.assertEqual("none", result.source)
        self.assertEqual("source_has_no_prose", result.status)
        self.assertFalse(result.needs_review)

    def test_not_found_uses_parent_only_as_tombstone_fallback(self):
        result = resolve_description(
            True, None, "not_found", "skipped", "not_parsed"
        )

        self.assertEqual("parent", result.source)
        self.assertEqual("parent_fallback_tombstone", result.status)
        self.assertTrue(result.needs_review)

    def test_not_found_without_parent_is_unresolved(self):
        result = resolve_description(
            False, None, "not_found", "skipped", "not_parsed"
        )

        self.assertEqual("none", result.source)
        self.assertEqual("unresolved_tombstone", result.status)
        self.assertTrue(result.needs_review)

    def test_failed_fetch_does_not_silently_fallback(self):
        result = resolve_description(
            True, None, "failed", "skipped_due_to_failure", "not_parsed"
        )

        self.assertEqual("none", result.source)
        self.assertEqual("unresolved_recrawl_failure", result.status)
        self.assertTrue(result.needs_review)


class AreaEvidenceTests(unittest.TestCase):
    def test_apostrophe_thousands_are_reparsed(self):
        result = parse_area_evidence("1'670 m2", 1.0)

        self.assertEqual(1670.0, result.value_sqm)
        self.assertEqual("accepted_reparsed", result.status)
        self.assertFalse(result.details["parsed_matches"])

    def test_european_decimal_and_thousands_are_parsed(self):
        result = parse_area_evidence("2'680.00 mq", 2.0)

        self.assertEqual(2680.0, result.value_sqm)
        self.assertEqual("sqm", result.unit_kind)

    def test_expanded_square_metre_abbreviation_is_parsed(self):
        result = parse_area_evidence("294 sq. meters", 294.0)

        self.assertEqual(294.0, result.value_sqm)
        self.assertEqual("accepted", result.status)

    def test_exact_two_factor_area_expression_is_computed(self):
        cases = (
            ("2 x 332 m\u00b2", 664.0),
            ("3 x 170m\u00b2", 510.0),
            ("mq 12,50 x 2", 25.0),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = parse_area_evidence(raw, 2.0)
                self.assertEqual(expected, result.value_sqm)
                self.assertEqual("computed_multiplier", result.status)
                self.assertEqual(0.90, result.confidence)
                self.assertFalse(result.needs_review)

    def test_non_exact_multiplicative_expression_is_quarantined(self):
        result = parse_area_evidence("2 x 3 x 170m\u00b2", 2.0)

        self.assertIsNone(result.value_sqm)
        self.assertEqual("quarantined", result.status)
        self.assertEqual("multiplicative_expression", result.details["reason"])

    def test_unmatched_additive_number_is_quarantined_with_candidate(self):
        result = parse_area_evidence("8.400 +120 m2", 8400.0)

        self.assertIsNone(result.value_sqm)
        self.assertEqual(0.0, result.confidence)
        self.assertEqual("residual_additive_or_range_scope", result.details["reason"])
        self.assertEqual("unmatched_additive", result.details["scope"])
        self.assertEqual(120.0, result.details["candidates"][0]["value_sqm"])

    def test_existing_addition_residue_is_quarantined_with_candidate(self):
        result = parse_area_evidence("1594 sqm + existing", 1594.0)

        self.assertIsNone(result.value_sqm)
        self.assertEqual(0.0, result.confidence)
        self.assertEqual("residual_additive_or_range_scope", result.details["reason"])
        self.assertEqual("additive_or_range", result.details["scope"])
        self.assertEqual(1594.0, result.details["candidates"][0]["value_sqm"])

    def test_unlabeled_additive_and_ranges_are_quarantined(self):
        cases = (
            "120 m2 + 30",
            "100-200 m2",
            "100 to 200 m2",
            "100/200 m2",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                result = parse_area_evidence(raw, 120.0)
                self.assertIsNone(result.value_sqm)
                self.assertEqual(
                    "residual_additive_or_range_scope", result.details["reason"]
                )

    def test_unmatched_square_unit_after_slash_is_quarantined(self):
        result = parse_area_evidence("6 700 m\u00b2/sq", 6700.0)

        self.assertEqual(6700.0, result.value_sqm)
        self.assertEqual(
            AREA_UNMATCHED_UNIT_CANDIDATE_CONFIDENCE, result.confidence
        )
        self.assertEqual("quarantined", result.status)
        self.assertTrue(result.needs_review)
        self.assertEqual("unmatched_slash_or_unit", result.details["scope"])
        self.assertEqual(6700.0, result.details["candidates"][0]["value_sqm"])
        self.assertEqual(6700.0, result.details["candidate_value_sqm"])

    def test_imperial_only_value_is_converted(self):
        result = parse_area_evidence("918 sq ft", 918.0)

        self.assertAlmostEqual(918 * SQFT_TO_SQM, result.value_sqm, places=6)
        self.assertEqual("accepted_converted", result.status)
        self.assertEqual("sqft", result.unit_kind)

    def test_hectares_are_converted(self):
        result = parse_area_evidence("1.5 ha", 1.5)

        self.assertEqual(15000.0, result.value_sqm)
        self.assertEqual("converted_hectare_review", result.status)
        self.assertEqual("hectare", result.unit_kind)
        self.assertTrue(result.needs_review)

    def test_audited_hectare_outlier_keeps_value_for_review(self):
        result = parse_area_evidence("600 ha", 600.0)

        self.assertEqual(6_000_000.0, result.value_sqm)
        self.assertEqual("converted_hectare_review", result.status)
        self.assertTrue(result.needs_review)

    def test_metric_aliases_are_supported(self):
        cases = (
            ("800 m.q.", 800.0),
            ("220 qm", 220.0),
            ("24 sm", 24.0),
            ("132msq", 132.0),
            ("3500 smq", 3500.0),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = parse_area_evidence(raw, expected)
                self.assertEqual(expected, result.value_sqm)
                self.assertEqual("sqm", result.unit_kind)
                self.assertFalse(result.needs_review)

    def test_pi_squared_is_treated_as_square_feet(self):
        result = parse_area_evidence("120m2 (\u00b11290pi\u00b2)", 120.0)

        self.assertEqual(120.0, result.value_sqm)
        self.assertEqual("accepted_dual_verified", result.status)
        self.assertEqual("sqm_dual", result.unit_kind)

    def test_safe_labels_and_non_area_annotations_are_allowed(self):
        cases = (
            ("Area: 1500 sq.m", 1500.0),
            ("342 mq (GFA)", 342.0),
            ("House: 75,00 m2", 75.0),
            ("4.150 m\u00b2 (gross)", 4150.0),
            ("app. 350 sq.m.", 350.0),
            ("500 mq circa", 500.0),
            ("2.752,01 m\u00b2 built", 2752.01),
            ("BUILT SURFACE 18.900 M2", 18900.0),
            ("13,700 m2 (250 housing units)", 13700.0),
            ("16 m\u00b2, 64 m\u00b3 (4x4x4 m.)", 16.0),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = parse_area_evidence(raw, expected)
                self.assertEqual(expected, result.value_sqm)
                self.assertFalse(result.needs_review)

    def test_scope_qualified_area_values_require_review(self):
        cases = (
            (
                "Main Building (11,100 sq.m/120,000 sq.ft.)",
                "main_building",
                11100.0,
            ),
            ("32 m\u00b2 ouvert", "open_area", 32.0),
            ("8 748 m\u00b2 SDO", "sdo", 8748.0),
            ("54.00 mq useful area", "useful_area", 54.0),
            ("23330 sqm aboveground", "aboveground", 23330.0),
            ("5950 mq (SLP)", "slp", 5950.0),
            ("6548 m2 - New Construction", "new_construction", 6548.0),
            ("roof area 5.000 m2", "roof_area", 5000.0),
            ("footprint 60,5 m2", "footprint", 60.5),
        )
        for raw, expected_scope, expected_value in cases:
            with self.subTest(raw=raw):
                result = parse_area_evidence(raw, 100.0)
                self.assertEqual(expected_value, result.value_sqm)
                self.assertTrue(result.needs_review)
                self.assertEqual("quarantined", result.status)
                self.assertEqual(AREA_SCOPE_CANDIDATE_CONFIDENCE, result.confidence)
                self.assertEqual("residual_scope_label", result.details["reason"])
                self.assertEqual(expected_scope, result.details["scope"])
                self.assertTrue(result.details["candidates"])
                self.assertEqual(
                    expected_value, result.details["candidate_value_sqm"]
                )

    def test_consistent_dual_units_prefer_metric(self):
        result = parse_area_evidence("240 m2 (2 583 ft2)", 240.0)

        self.assertEqual(240.0, result.value_sqm)
        self.assertEqual("accepted_dual_verified", result.status)
        self.assertEqual("sqm_dual", result.unit_kind)

    def test_reversed_consistent_dual_units_prefer_metric(self):
        result = parse_area_evidence(
            "258,300 gross sq ft / 23,996 gross sq m", 258300.0
        )

        self.assertEqual(23996.0, result.value_sqm)
        self.assertEqual("accepted_dual_verified", result.status)

    def test_mismatched_dual_units_are_quarantined(self):
        result = parse_area_evidence("100 m2 / 100 sq ft", 100.0)

        self.assertIsNone(result.value_sqm)
        self.assertEqual("dual_unit_mismatch", result.details["reason"])

    def test_multiple_metric_values_are_quarantined(self):
        result = parse_area_evidence(
            "25 sqm (covered) + 26 sqm (exterior)", 25.0
        )

        self.assertIsNone(result.value_sqm)
        self.assertEqual("multiple_area_values", result.details["reason"])

    def test_linear_and_volumetric_units_are_quarantined(self):
        for raw in ("larghezza 10,20 mt", "40 cm", "80m", "65mc"):
            with self.subTest(raw=raw):
                result = parse_area_evidence(raw, 10.2)
                self.assertIsNone(result.value_sqm)
                self.assertEqual("linear_or_volume_unit", result.details["reason"])

    def test_ambiguous_square_abbreviation_is_quarantined(self):
        result = parse_area_evidence("95'000 sq", 95.0)

        self.assertIsNone(result.value_sqm)
        self.assertEqual("ambiguous_area_unit", result.details["reason"])

    def test_bare_number_uses_built_surface_context(self):
        result = parse_area_evidence("78,70", 78.7)

        self.assertEqual(78.7, result.value_sqm)
        self.assertEqual("accepted_implicit_sqm", result.status)
        self.assertEqual("implicit_sqm", result.unit_kind)

    def test_implicit_qa_outlier_keeps_value_for_review(self):
        result = parse_area_evidence("300000", 300000.0)

        self.assertEqual(300000.0, result.value_sqm)
        self.assertEqual("qa_outlier_review", result.status)
        self.assertTrue(result.needs_review)

    def test_explicit_qa_outlier_keeps_value_for_review(self):
        result = parse_area_evidence("575000 mq", 575000.0)

        self.assertEqual(575000.0, result.value_sqm)
        self.assertEqual("qa_outlier_review", result.status)
        self.assertTrue(result.needs_review)

    def test_explicit_value_below_its_auto_limit_remains_automatic(self):
        result = parse_area_evidence("300,000 sqm", 300000.0)

        self.assertEqual(300000.0, result.value_sqm)
        self.assertFalse(result.needs_review)

    def test_hard_invalid_area_is_quarantined(self):
        result = parse_area_evidence("100000001 sqm", 100000001.0)

        self.assertIsNone(result.value_sqm)
        self.assertEqual("quarantined", result.status)
        self.assertEqual("area_out_of_range", result.details["reason"])

    def test_missing_raw_never_trusts_legacy_parsed_value(self):
        result = parse_area_evidence(None, 1200.0)

        self.assertIsNone(result.value_sqm)
        self.assertEqual("quarantined", result.status)
        self.assertEqual("missing_raw", result.details["reason"])


if __name__ == "__main__":
    unittest.main()
