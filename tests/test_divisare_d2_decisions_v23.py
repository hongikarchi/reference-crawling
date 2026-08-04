from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unittest
from collections import Counter, defaultdict
from pathlib import Path

from canonical.divisare_review_v23 import (
    EXPECTED_PARENT_SHA256,
    PRODUCTION_D2_COUNTS,
    load_d2_manifest,
    resolve_identity_components,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "canonical" / "divisare_d2_decisions_v1.json"
PARENT_DB = ROOT / "data" / "curated" / "divisare_metadata_v2_2.db"
MANIFEST_SHA256 = (
    "dcc33813a31d8e0e1a3d452798cee15139180519f35b130346848ee2550f86a0"
)
EXPECTED_MERGES = frozenset(
    {
        (96467, 343892),
        (112411, 343271),
        (237243, 339073),
        (317455, 328691),
        (339186, 380335),
        (346253, 449455),
        (348479, 348989),
        (478764, 536572),
    }
)
EXPECTED_REJECT_RELATIONS = {
    "distinct_event_entry": 86,
    "distinct_phase_or_intervention": 9,
    "distinct_same_name": 11,
    "distinct_sibling_building": 22,
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _load() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _pair_index(payload: dict) -> dict[tuple[int, int], dict]:
    return {
        (int(item["article_id_a"]), int(item["article_id_b"])): item
        for item in payload["decisions"]
    }


def _readonly_connection() -> sqlite3.Connection:
    return sqlite3.connect(PARENT_DB.as_uri() + "?mode=ro", uri=True)


class DivisareV23D2DecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _load()
        cls.decisions = cls.payload["decisions"]
        cls.by_pair = _pair_index(cls.payload)

    def test_manifest_is_frozen_and_has_parent_lineage(self) -> None:
        self.assertEqual(
            MANIFEST_SHA256, hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
        )
        self.assertEqual(1, self.payload["schema_version"])
        self.assertEqual("divisare-d2-review-v1.0", self.payload["version"])
        self.assertEqual(
            "same_architectural_project_intervention",
            self.payload["identity_scope"],
        )
        self.assertEqual(EXPECTED_PARENT_SHA256, self.payload["parent_sha256"])
        parent = self.payload["lineage"]["parent_artifact"]
        self.assertEqual(
            "data/curated/divisare_metadata_v2_2.db", parent["path"]
        )
        self.assertEqual(EXPECTED_PARENT_SHA256, parent["sha256"])
        self.assertFalse(
            self.payload["lineage"]["independent_merge_audit"][
                "image_content_inspected"
            ]
        )

    def test_exact_parent_pending_pair_snapshot_and_counts(self) -> None:
        self.assertTrue(PARENT_DB.exists(), "immutable v2.2 parent DB is required")
        with _readonly_connection() as conn:
            expected_pairs = {
                (int(left), int(right))
                for left, right in conn.execute(
                    """
                    SELECT article_id_a, article_id_b
                    FROM article_match_reviews_v2
                    WHERE decision_status IN ('pending', 'deferred')
                    """
                )
            }

        self.assertEqual(220, len(expected_pairs))
        self.assertEqual(expected_pairs, set(self.by_pair))
        self.assertEqual(220, len(self.decisions))
        self.assertEqual(220, len({item["decision_id"] for item in self.decisions}))
        self.assertEqual(
            {"merge": 8, "reject": 128, "defer": 84},
            dict(Counter(item["decision"] for item in self.decisions)),
        )
        for key, expected in PRODUCTION_D2_COUNTS.items():
            declared_key = "total_pairs" if key == "total" else key
            self.assertEqual(expected, self.payload["counts"][declared_key])

        loaded = load_d2_manifest(
            MANIFEST_PATH,
            expected_parent_sha256=EXPECTED_PARENT_SHA256,
            expected_pairs=expected_pairs,
        )
        self.assertEqual(PRODUCTION_D2_COUNTS, loaded.counts)

    def test_merge_gate_requires_two_independent_families(self) -> None:
        merge_pairs = {
            pair for pair, item in self.by_pair.items() if item["decision"] == "merge"
        }
        self.assertEqual(EXPECTED_MERGES, merge_pairs)

        for pair in sorted(merge_pairs):
            with self.subTest(pair=pair):
                item = self.by_pair[pair]
                families = {
                    entry["evidence_family"]
                    for entry in item["evidence"]
                    if entry.get("supports") == "same_identity"
                    and entry.get("independent_for_merge") is True
                }
                self.assertGreaterEqual(len(families), 2)
                self.assertEqual(len(families), item["evidence_family_count"])
                self.assertEqual([], item["hard_conflicts"])
                self.assertEqual("same_project_duplicate", item["relation_type"])

    def test_reject_and_defer_semantics_are_conservative(self) -> None:
        reject_counts = Counter(
            item["relation_type"]
            for item in self.decisions
            if item["decision"] == "reject"
        )
        self.assertEqual(EXPECTED_REJECT_RELATIONS, dict(reject_counts))
        self.assertEqual(EXPECTED_REJECT_RELATIONS, self.payload["reject_relation_counts"])

        for item in self.decisions:
            with self.subTest(decision_id=item["decision_id"]):
                self.assertTrue(item["approved"])
                self.assertLess(item["article_id_a"], item["article_id_b"])
                self.assertTrue(item["reason_code"])
                self.assertTrue(item["note"])
                if item["decision"] == "reject":
                    self.assertTrue(item["hard_conflicts"])
                elif item["decision"] == "defer":
                    self.assertEqual("unresolved_identity", item["relation_type"])
                    self.assertLess(item["evidence_family_count"], 2)

    def test_known_sibling_phase_and_same_name_failures_stay_separate(self) -> None:
        expected = {
            (260144, 260145): ("reject", "distinct_sibling_building", "same_complex"),
            (235013, 235152): (
                "reject",
                "distinct_phase_or_intervention",
                "successive_intervention",
            ),
            (110876, 110882): ("reject", "distinct_sibling_building", "same_complex"),
            (430452, 437795): ("reject", "distinct_same_name", None),
        }
        for pair, (decision, relation_type, related_relation) in expected.items():
            with self.subTest(pair=pair):
                item = self.by_pair[pair]
                self.assertEqual(decision, item["decision"])
                self.assertEqual(relation_type, item["relation_type"])
                self.assertEqual(related_relation, item["related_relation"])

    def test_sparse_identity_candidates_are_explicit_abstentions(self) -> None:
        for pair in ((381279, 381465), (268679, 383882), (346047, 396651)):
            with self.subTest(pair=pair):
                item = self.by_pair[pair]
                self.assertEqual("defer", item["decision"])
                self.assertEqual("unresolved_identity", item["relation_type"])
                self.assertFalse(item["related_project"])

        for pair in ((268679, 383882), (346047, 396651)):
            facts = " ".join(
                str(entry.get("fact", "")) for entry in self.by_pair[pair]["evidence"]
            )
            self.assertIn("Exact shared asset_key=0", facts)
            self.assertIn("album membership=0", facts)

    def test_guards_are_complete_and_paths_are_portable(self) -> None:
        for item in self.decisions:
            for side, article_id in (
                ("article_a", item["article_id_a"]),
                ("article_b", item["article_id_b"]),
            ):
                with self.subTest(decision_id=item["decision_id"], side=side):
                    guard = item["guards"][side]
                    self.assertEqual(article_id, guard["article_id"])
                    self.assertEqual(
                        "divisare-html-metadata-v2.3", guard["parser_version"]
                    )
                    self.assertTrue(guard["source_url"].startswith("https://divisare.com/"))
                    self.assertTrue(
                        guard["snapshot_path"].startswith(
                            "data/enrichment/divisare_html_snapshots_v2_4/"
                        )
                    )
                    self.assertNotIn("\\", guard["snapshot_path"])
                    for field in ("html_sha256", "source_row_hash"):
                        self.assertRegex(guard[field], SHA256_RE)
                    for field in ("description_prose_sha256", "abstract_sha256"):
                        if guard[field] is not None:
                            self.assertRegex(guard[field], SHA256_RE)

    def test_duplicate_building_pair_groups_do_not_conflict(self) -> None:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for item in self.decisions:
            grouped[item["building_pair_id"]].append(item)
        repeated = [items for items in grouped.values() if len(items) > 1]
        self.assertEqual(6, len(repeated))
        for items in repeated:
            with self.subTest(building_pair_id=items[0]["building_pair_id"]):
                self.assertEqual(1, len({item["decision"] for item in items}))
                self.assertEqual(1, len({item["relation_type"] for item in items}))

    def test_component_union_cannot_collapse_a_reject_or_defer(self) -> None:
        with _readonly_connection() as conn:
            active_buildings = {
                row[0]
                for row in conn.execute(
                    "SELECT building_id FROM building_core_reconciled_v2_2 WHERE is_active=1"
                )
            }
            article_to_building = {
                int(article_id): building_id
                for article_id, building_id in conn.execute(
                    "SELECT article_id, building_id FROM active_building_membership_v2"
                )
            }

        mapping = resolve_identity_components(
            active_buildings, article_to_building, self.by_pair
        )
        self.assertEqual(len(active_buildings) - 8, len(set(mapping.values())))
        for pair, item in self.by_pair.items():
            same_component = (
                mapping[article_to_building[pair[0]]]
                == mapping[article_to_building[pair[1]]]
            )
            self.assertEqual(item["decision"] == "merge", same_component)


if __name__ == "__main__":
    unittest.main()
