from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V1_PATH = ROOT / "canonical" / "divisare_partial_text_decisions_v1.json"
V2_PATH = ROOT / "canonical" / "divisare_partial_text_decisions_v2.json"
V1_SHA256 = "f31ee5b94afec2c5cde59f2479ea2e06a1e27925b24336f973b571e40838b5df"
REJECTED_EXHIBITION_IDS = frozenset({261731, 261740})
EXPECTED_IDS = frozenset(
    {
        152511,
        189203,
        203666,
        203673,
        203677,
        203680,
        203683,
        203700,
        226836,
        250887,
        261731,
        261740,
        294814,
        302107,
        340154,
        346848,
        347217,
        348500,
        425341,
        431814,
        545147,
    }
)
EXHIBITION_PROSE_SHA256 = (
    "01cce0c2282871f1f6c4bf52794b3f4c166733f64667946b21cd0c30f5e868dd"
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _index(payload: dict) -> dict[int, dict]:
    return {int(item["article_id"]): item for item in payload["decisions"]}


class DivisareV23PartialTextDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.v1 = _load(V1_PATH)
        cls.v2 = _load(V2_PATH)
        cls.v1_by_id = _index(cls.v1)
        cls.v2_by_id = _index(cls.v2)

    def test_v1_manifest_remains_byte_immutable(self) -> None:
        self.assertEqual(V1_SHA256, hashlib.sha256(V1_PATH.read_bytes()).hexdigest())

    def test_v2_is_a_complete_exact_21_article_snapshot(self) -> None:
        decisions = self.v2["decisions"]
        ids = [int(item["article_id"]) for item in decisions]

        self.assertEqual(1, self.v2["schema_version"])
        self.assertEqual("divisare-partial-text-review-v2.0", self.v2["version"])
        self.assertEqual(21, len(decisions))
        self.assertEqual(21, len(set(ids)))
        self.assertEqual(EXPECTED_IDS, frozenset(ids))
        self.assertEqual(set(self.v1_by_id), set(self.v2_by_id))

    def test_v2_supersedes_the_exact_v1_snapshot(self) -> None:
        self.assertEqual(
            {
                "version": "divisare-partial-text-review-v1.0",
                "sha256": V1_SHA256,
            },
            self.v2["supersedes"],
        )

    def test_exhibition_fallbacks_are_hash_guarded_rejects(self) -> None:
        for article_id in REJECTED_EXHIBITION_IDS:
            with self.subTest(article_id=article_id):
                item = self.v2_by_id[article_id]
                self.assertEqual("divisare-html-metadata-v2.3", item["parser_version"])
                self.assertEqual(EXHIBITION_PROSE_SHA256, item["prose_sha256"])
                self.assertEqual("reject", item["decision"])
                self.assertEqual(
                    "shared_exhibition_boilerplate", item["reason_code"]
                )
                self.assertEqual(
                    "Shared exhibition-section boilerplate, not project prose.",
                    item["note"],
                )

    def test_all_other_article_decisions_are_identical_to_v1(self) -> None:
        for article_id in sorted(EXPECTED_IDS - REJECTED_EXHIBITION_IDS):
            with self.subTest(article_id=article_id):
                self.assertEqual(self.v1_by_id[article_id], self.v2_by_id[article_id])

    def test_exhibition_override_changes_only_the_review_decision_fields(self) -> None:
        changed_fields = {"decision", "reason_code", "note"}
        for article_id in REJECTED_EXHIBITION_IDS:
            with self.subTest(article_id=article_id):
                v1_item = self.v1_by_id[article_id]
                v2_item = self.v2_by_id[article_id]
                self.assertEqual(
                    {key: value for key, value in v1_item.items() if key not in changed_fields},
                    {key: value for key, value in v2_item.items() if key not in changed_fields},
                )


if __name__ == "__main__":
    unittest.main()
