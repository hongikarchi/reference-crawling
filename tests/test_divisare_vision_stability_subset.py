from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import pytest

import canonical.divisare_vision_stability_subset as freezer
from canonical.divisare_vision_gold import CLASSES, FINAL_CELL_QUOTAS


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _synthetic_gold() -> dict:
    samples = []
    rank = 0
    for label in CLASSES:
        for (generation, clarity), count in FINAL_CELL_QUOTAS.items():
            for _ in range(count):
                rank += 1
                samples.append(
                    {
                        "sample_rank": rank,
                        "sample_id": "sample-%04d" % rank,
                        "source_identity": {
                            "candidate_id": "candidate-%04d" % rank,
                            "asset_key": "asset-%04d" % rank,
                            "generation_group": generation,
                        },
                        "image_evidence": {"content_sha256": _digest("image-%d" % rank)},
                        "human_review": {
                            "gold_label": label,
                            "clarity": clarity,
                            "acceptable_labels": [label],
                        },
                    }
                )
    return {
        "manifest_version": "divisare-vision-gold-manifest-v1.0.0",
        "logical_sha256": "1" * 64,
        "gold_manifest_sha256": "2" * 64,
        "samples": samples,
    }


@pytest.fixture(autouse=True)
def _stub_upstream_gold_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freezer, "validate_gold_manifest", lambda payload: None)


def _build(gold: dict | None = None) -> dict:
    return freezer.build_stability_subset(
        gold_manifest=gold or _synthetic_gold(),
        gold_manifest_file_sha256="3" * 64,
        gold_manifest_filename="gold.json",
    )


def test_exhaustive_deterministic_whole_batch_selection() -> None:
    gold = _synthetic_gold()
    first = _build(gold)
    second = _build(copy.deepcopy(gold))

    assert first == second
    assert len(first["selected_batches"]) == 10
    assert len(first["selected_samples"]) == 50
    assert first["selection_metrics"]["combinations_evaluated"] == math.comb(20, 10)
    assert first["selection_policy"]["result_conditioning"] is False
    assert first["selection_policy"]["input_scope"].endswith("pre_result")
    batch_numbers = first["selection_metrics"]["selected_batch_numbers"]
    assert batch_numbers == sorted(batch_numbers)
    assert [row["batch_no"] for row in first["selected_batches"]] == batch_numbers
    for row in first["selected_batches"]:
        assert row["sample_rank_end"] - row["sample_rank_start"] == 4
    freezer.validate_stability_subset(
        first,
        gold_manifest=gold,
        gold_manifest_file_sha256="3" * 64,
        gold_manifest_filename="gold.json",
    )


def test_score_is_joint_cell_first_and_tie_break_is_lexicographic() -> None:
    payload = _build()
    selected = payload["selection_metrics"]["selected_batch_numbers"]
    assert selected == [1, 3, 5, 7, 9, 11, 13, 16, 17, 20]
    assert payload["selection_metrics"]["optimal_score_tie_count"] >= 1
    score = payload["selection_metrics"]["best_score"]
    assert list(score) == list(freezer.SCORE_FIELDS)


def test_selected_distribution_and_sample_snapshots_are_bound() -> None:
    gold = _synthetic_gold()
    payload = _build(gold)
    selected_counts = Counter(
        (row["gold_label"], row["generation_group"], row["clarity"])
        for row in payload["selected_samples"]
    )
    joint = payload["selection_metrics"]["distribution"]["joint_cells"]
    for cell in freezer.CELL_ORDER:
        assert joint["/".join(cell)]["selected_count"] == selected_counts[cell]
    changed = copy.deepcopy(payload)
    changed["selected_samples"][0]["asset_key"] = "outcome-conditioned"
    changed["logical_sha256"] = freezer.subset_logical_sha256(changed)
    changed["subset_manifest_sha256"] = freezer.subset_manifest_sha256(changed)
    with pytest.raises(ValueError, match="differs from supplied gold"):
        freezer.validate_stability_subset(
            changed,
            gold_manifest=gold,
            gold_manifest_file_sha256="3" * 64,
            gold_manifest_filename="gold.json",
        )


def test_binds_all_three_gold_hashes_and_detects_tampering() -> None:
    payload = _build()
    assert payload["provenance"] == {
        "gold_manifest_filename": "gold.json",
        "gold_manifest_version": "divisare-vision-gold-manifest-v1.0.0",
        "gold_manifest_file_sha256": "3" * 64,
        "gold_logical_sha256": "1" * 64,
        "gold_manifest_sha256": "2" * 64,
    }
    changed = copy.deepcopy(payload)
    changed["provenance"]["gold_manifest_sha256"] = "4" * 64
    with pytest.raises(ValueError, match="logical SHA mismatch"):
        freezer.validate_stability_subset(changed)


def test_rejects_non_sha_file_binding() -> None:
    with pytest.raises(ValueError, match="gold manifest file SHA"):
        freezer.build_stability_subset(
            gold_manifest=_synthetic_gold(),
            gold_manifest_file_sha256="bad",
            gold_manifest_filename="gold.json",
        )


def test_strict_parser_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    gold_path = tmp_path / "gold.json"
    gold_path.write_text('{"x":1,"x":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        freezer.freeze_stability_subset_file(
            gold_manifest_path=gold_path,
            output_path=tmp_path / "subset.json",
        )


def test_file_freezer_is_no_clobber_and_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gold = _synthetic_gold()
    raw = freezer.canonical_json_bytes(gold) + b"\n"
    gold_path = tmp_path / "gold.json"
    output = tmp_path / "subset.json"
    gold_path.write_bytes(raw)

    first = freezer.freeze_stability_subset_file(
        gold_manifest_path=gold_path, output_path=output
    )
    before = output.read_bytes()
    assert before == freezer.canonical_json_bytes(first) + b"\n"
    freezer.validate_stability_subset(
        json.loads(before),
        gold_manifest=gold,
        gold_manifest_file_sha256=hashlib.sha256(raw).hexdigest(),
        gold_manifest_filename="gold.json",
    )
    assert first["provenance"]["gold_manifest_file_sha256"] == hashlib.sha256(
        raw
    ).hexdigest()
    with pytest.raises(FileExistsError, match="already exists"):
        freezer.freeze_stability_subset_file(
            gold_manifest_path=gold_path, output_path=output
        )
    assert output.read_bytes() == before


def test_cli_accepts_only_gold_and_output_arguments() -> None:
    from tools.freeze_divisare_vision_stability_subset import main

    with pytest.raises(SystemExit) as exc:
        main(["--n100-results", "result.db"])
    assert exc.value.code == 2
