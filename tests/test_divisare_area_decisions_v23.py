from __future__ import annotations

import hashlib
import json
import math
import re
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "canonical" / "divisare_area_decisions_v1.json"
SOURCE_REVIEW_SHA256 = (
    "6051a74d6de87e69a70e480deb0ea81ad092759587a58b090723da7ba9fcd5a2"
)
PARENT_DB_SHA256 = (
    "ee7bcd55fedf38fe8cb9a49f51e8f12f69493aef68ff1d201d2fa1e5be8ec95c"
)
GUARD_TUPLE_SHA256 = (
    "1c6d20ff2014cb499ea1ed558021487b059fd5b33eb4125eddcc5c87b60e1486"
)
EXPECTED_IDS = frozenset(
    {
        3356,
        3363,
        12685,
        14079,
        16857,
        17072,
        17231,
        68768,
        70665,
        74510,
        85625,
        87929,
        99758,
        99768,
        100673,
        103166,
        110613,
        112867,
        116579,
        123425,
        124509,
        125050,
        125094,
        137557,
        137558,
        137561,
        137566,
        150864,
        158032,
        161150,
        170460,
        182354,
        189319,
        191552,
        194970,
        195689,
        196760,
        197306,
        199771,
        199805,
        211124,
        212779,
        220148,
        221886,
        223104,
        225540,
        227942,
        228355,
        228395,
        229021,
        234597,
        241310,
        241700,
        242438,
        244432,
        248751,
        251528,
        255831,
        261284,
        261510,
        263706,
        265703,
        266480,
        267355,
        273268,
        275358,
        277558,
        277581,
        279446,
        279498,
        279532,
        279540,
        279553,
        279646,
        280185,
        280217,
        280355,
        280377,
        280918,
        281920,
        282736,
        282785,
        283423,
        285124,
        285142,
        286241,
        286442,
        287588,
        289519,
        290236,
        292435,
        292742,
        295348,
        312933,
        313559,
        313835,
        316548,
        322209,
        324373,
        324471,
        325574,
        327274,
        328092,
        331581,
        332886,
        333852,
        338920,
        341227,
        344383,
        364492,
        365195,
        365231,
        371498,
        381244,
        384288,
        385436,
        388862,
        388870,
        391491,
        395133,
        465382,
        469482,
        479403,
        497947,
        521929,
    }
)
EXPECTED_COUNTS = {
    "accept_area": 10,
    "final": 123,
    "keep_null_multi_or_conflict": 21,
    "keep_scoped_candidate": 15,
    "open_external_text_review": 2,
    "reject_non_area": 79,
    "total": 125,
}
DECISION_TYPES = frozenset(
    {
        "accept_area",
        "keep_null_multi_or_conflict",
        "keep_scoped_candidate",
        "reject_non_area",
    }
)
CLOSURE_STATUSES = frozenset({"final", "open_external_text_review"})
AREA_SCOPES = frozenset(
    {
        "abovegrade_and_basement",
        "abovegrade_and_parking",
        "aboveground_area",
        "building_area",
        "building_components",
        "built_surface",
        "built_surface_inferred_sqm",
        "conflicting_project_area",
        "conflicting_project_phase_area",
        "conflicting_site_area",
        "covered_area",
        "cctv_development_program_area",
        "floor_components",
        "footprint_area",
        "gross_floor_area",
        "linear_measure",
        "main_and_parking",
        "main_building_area",
        "mixed_area_standards",
        "mixed_built_and_track",
        "multi_building_components",
        "new_construction_area",
        "open_configuration_area",
        "program_components",
        "roof_area",
        "sdo_floor_area",
        "slp_gross_floor_area",
        "state_variant_footprint",
        "terminal_area",
        "total_built_surface",
        "total_project_area_including_exterior",
        "unitless_program_components",
        "unlabeled_additive",
        "useful_area",
        "volume",
    }
)
PARSER_STATUSES = frozenset(
    {"converted_hectare_review", "qa_outlier_review", "quarantined"}
)
PARSER_UNIT_KINDS = frozenset(
    {
        "ambiguous",
        "candidate_scope",
        "hectare",
        "implicit_sqm",
        "non_area",
        "sqm",
        "unknown",
    }
)
OPEN_IDS = frozenset({14079, 465382})
EXTERNAL_EVIDENCE_IDS = frozenset(
    {14079, 16857, 277581, 282785, 465382, 479403}
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _load() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _is_positive_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


class DivisareV23AreaDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _load()
        cls.decisions = cls.payload["decisions"]
        cls.by_id = {int(item["article_id"]): item for item in cls.decisions}

    def test_exact_125_article_snapshot_and_revised_counts(self) -> None:
        ids = [int(item["article_id"]) for item in self.decisions]

        self.assertEqual(125, len(ids))
        self.assertEqual(125, len(set(ids)))
        self.assertEqual(EXPECTED_IDS, frozenset(ids))
        self.assertEqual(EXPECTED_COUNTS, self.payload["counts"])
        self.assertEqual(
            {
                "accept_area": 10,
                "keep_null_multi_or_conflict": 21,
                "keep_scoped_candidate": 15,
                "reject_non_area": 79,
            },
            dict(Counter(item["decision_type"] for item in self.decisions)),
        )
        self.assertEqual(
            {"final": 123, "open_external_text_review": 2},
            dict(Counter(item["closure_status"] for item in self.decisions)),
        )

    def test_manifest_identity_is_deterministic(self) -> None:
        self.assertEqual(1, self.payload["schema_version"])
        self.assertEqual("divisare-area-review-v1.0", self.payload["version"])
        self.assertEqual(
            "codex-5.6-sol-area-review-with-independent-audit",
            self.payload["decided_by"],
        )
        self.assertEqual(
            "2026-08-04T00:00:00+09:00", self.payload["decided_at"]
        )
        self.assertEqual(
            "2026-08-04T00:00:00+09:00", self.payload["frozen_at"]
        )
        self.assertEqual("divisare-area-human-review-v1", self.payload["policy"])

    def test_allowed_enums_are_closed(self) -> None:
        self.assertEqual(
            DECISION_TYPES, {item["decision_type"] for item in self.decisions}
        )
        self.assertEqual(
            CLOSURE_STATUSES, {item["closure_status"] for item in self.decisions}
        )
        self.assertEqual(AREA_SCOPES, {item["area_scope"] for item in self.decisions})
        self.assertEqual(
            PARSER_STATUSES,
            {
                item["evidence"]["parser_evidence"]["status"]
                for item in self.decisions
            },
        )
        self.assertEqual(
            PARSER_UNIT_KINDS,
            {
                item["evidence"]["parser_evidence"]["unit_kind"]
                for item in self.decisions
            },
        )
        for item in self.decisions:
            with self.subTest(article_id=item["article_id"]):
                self.assertTrue(0 <= item["confidence"] <= 1)
                parser_confidence = item["evidence"]["parser_evidence"]["confidence"]
                self.assertTrue(0 <= parser_confidence <= 1)
                self.assertIs(item["evidence"]["parser_evidence"]["needs_review"], True)

    def test_guard_fields_are_complete_and_frozen(self) -> None:
        guard_lines = []
        prose_null_ids = set()

        for item in sorted(self.decisions, key=lambda value: value["article_id"]):
            article_id = int(item["article_id"])
            evidence = item["evidence"]
            with self.subTest(article_id=article_id):
                self.assertEqual(
                    "divisare-html-metadata-v2.3", evidence["parser_version"]
                )
                self.assertIsInstance(evidence["area_raw"], str)
                self.assertNotEqual("", evidence["area_raw"])
                self.assertRegex(evidence["area_raw_sha256"], SHA256_RE)
                self.assertRegex(evidence["html_sha256"], SHA256_RE)
                prose_sha256 = evidence["description_prose_sha256"]
                if prose_sha256 is None:
                    prose_null_ids.add(article_id)
                    prose_guard = "<null>"
                else:
                    self.assertRegex(prose_sha256, SHA256_RE)
                    prose_guard = prose_sha256
                self.assertTrue(evidence["source_url"].startswith("https://divisare.com/"))
                self.assertIsInstance(evidence["snapshot_path"], str)
                self.assertNotEqual("", evidence["snapshot_path"])

            guard_lines.append(
                "|".join(
                    (
                        str(article_id),
                        evidence["parser_version"],
                        evidence["area_raw_sha256"],
                        prose_guard,
                        evidence["html_sha256"],
                    )
                )
            )

        self.assertEqual({465382}, prose_null_ids)
        self.assertEqual(
            GUARD_TUPLE_SHA256,
            hashlib.sha256("\n".join(guard_lines).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            {
                "apply_only_if": (
                    "article_id, parser_version, all non-null SHA guards match"
                ),
                "area_raw_sha256": (
                    "sha256 of exact UTF-8 recrawl_area_raw; no normalization"
                ),
                "description_prose_sha256": (
                    "sha256 of exact UTF-8 description_prose; no normalization"
                ),
                "html_sha256": "copied from recrawl snapshot evidence",
            },
            self.payload["guard_semantics"],
        )

    def test_accept_scoped_and_null_invariants(self) -> None:
        for item in self.decisions:
            article_id = int(item["article_id"])
            decision_type = item["decision_type"]
            resolved = item["resolved_area_sqm"]
            candidate = item["candidate_area_sqm"]
            with self.subTest(article_id=article_id, decision_type=decision_type):
                if decision_type == "accept_area":
                    self.assertTrue(_is_positive_number(resolved))
                    self.assertIsNone(candidate)
                elif decision_type == "keep_scoped_candidate":
                    self.assertIsNone(resolved)
                    self.assertTrue(_is_positive_number(candidate))
                else:
                    self.assertIsNone(resolved)
                    self.assertIsNone(candidate)

        open_ids = {
            int(item["article_id"])
            for item in self.decisions
            if item["closure_status"] == "open_external_text_review"
        }
        self.assertEqual(OPEN_IDS, open_ids)
        self.assertTrue(
            all(
                self.by_id[article_id]["decision_type"]
                == "keep_null_multi_or_conflict"
                for article_id in OPEN_IDS
            )
        )

        cctv = self.by_id[16857]
        self.assertEqual("keep_scoped_candidate", cctv["decision_type"])
        self.assertEqual("cctv_development_program_area", cctv["area_scope"])
        self.assertIsNone(cctv["resolved_area_sqm"])
        self.assertEqual(575000.0, cctv["candidate_area_sqm"])

    def test_numeric_area_is_never_derived_from_images(self) -> None:
        self.assertEqual(
            "never infer numeric area from photographs or drawings",
            self.payload["image_policy"],
        )

        def keys(value: object):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield str(key)
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        for item in self.decisions:
            with self.subTest(article_id=item["article_id"]):
                self.assertFalse(
                    any("image" in key.lower() for key in keys(item)),
                    "Area decisions must not contain image-derived evidence fields.",
                )

    def test_external_evidence_is_explicit_and_source_scoped(self) -> None:
        evidence_ids = {
            int(item["article_id"])
            for item in self.decisions
            if "external_evidence" in item
        }
        self.assertEqual(EXTERNAL_EVIDENCE_IDS, evidence_ids)

        required_fields = {
            "url",
            "publisher",
            "observed_value_or_claim",
            "scope",
            "retrieved_at",
        }
        for article_id in sorted(EXTERNAL_EVIDENCE_IDS):
            entries = self.by_id[article_id]["external_evidence"]
            self.assertGreater(len(entries), 0)
            for entry in entries:
                with self.subTest(article_id=article_id, url=entry.get("url")):
                    self.assertEqual(required_fields, set(entry))
                    self.assertTrue(entry["url"].startswith("https://"))
                    self.assertNotEqual("", entry["publisher"].strip())
                    self.assertNotEqual("", entry["observed_value_or_claim"].strip())
                    self.assertNotEqual("", entry["scope"].strip())
                    self.assertEqual("2026-08-04", entry["retrieved_at"])

    def test_frozen_source_and_parent_lineage_are_provenance_only(self) -> None:
        self.assertEqual(
            {
                "path": r"C:\tmp\divisare_area_decisions_audit_v1.json",
                "sha256": SOURCE_REVIEW_SHA256,
                "required_at_runtime": False,
            },
            self.payload["lineage"]["review_source"],
        )
        self.assertEqual(
            {
                "path": "data/curated/divisare_metadata_v2_2.db",
                "sha256": PARENT_DB_SHA256,
            },
            self.payload["lineage"]["parent_artifact"],
        )
        self.assertEqual(
            {
                "description": (
                    "Independent-review correction for article 16857 plus "
                    "external-source provenance additions."
                ),
                "reclassified_article_ids": [16857],
                "external_evidence_article_ids": [
                    14079,
                    16857,
                    277581,
                    282785,
                    465382,
                    479403,
                ],
            },
            self.payload["canonical_transform"],
        )


if __name__ == "__main__":
    unittest.main()
