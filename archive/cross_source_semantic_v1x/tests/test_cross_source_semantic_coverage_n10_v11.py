from __future__ import annotations

import json
from pathlib import Path

import pytest

from canonical.cross_source_semantic_coverage import BuildingInventoryItem
from canonical.cross_source_semantic_coverage_n10_v11 import (
    DEFAULT_SAMPLE_SEED,
    EXPECTED_BUILDINGS,
    QUOTAS,
    PriorIdentityLedger,
    deterministic_score,
    select_fixed_sample,
    write_manifest,
)


def _item(source: str, stratum: str, number: int) -> BuildingInventoryItem:
    selection_id = f"{source}:building:{stratum}:{number:03d}"
    return BuildingInventoryItem(
        selection_id=selection_id,
        source=source,
        source_building_id=f"b{number:03d}",
        name=None,
        population_stratum=stratum,
        successful_asset_count=1,
        successful_gallery_count=1,
        source_record_sha256=f"{number:064x}",
        selection_record_sha256=f"{number + 1:064x}",
        cross_source_candidate=stratum == "cross_source_candidate",
    )


def _inventory() -> list[BuildingInventoryItem]:
    values = []
    number = 1
    for _, source, stratum, quota in QUOTAS:
        for _ in range(quota + 3):
            values.append(_item(source, stratum, number))
            number += 1
    return values


def test_fixed_sample_is_deterministic_balanced_and_result_blind() -> None:
    inventory = _inventory()
    first = select_fixed_sample(inventory, excluded_selection_ids=())
    second = select_fixed_sample(reversed(inventory), excluded_selection_ids=())
    assert first == second
    assert len(first) == EXPECTED_BUILDINGS
    assert sum(value[0] == "population_core" for value in first) == 8
    assert sum(value[0] == "risk_sentinel" for value in first) == 2
    assert sum(value[1] == "architizer" for value in first) == 7
    assert sum(value[1] == "divisare" for value in first) == 3


def test_excluded_winner_is_replaced_deterministically() -> None:
    inventory = _inventory()
    first = select_fixed_sample(inventory, excluded_selection_ids=())
    excluded = first[0][6].selection_id
    second = select_fixed_sample(inventory, excluded_selection_ids=(excluded,))
    assert excluded not in {value[6].selection_id for value in second}
    assert len(second) == EXPECTED_BUILDINGS


def test_score_binds_lane_source_stratum_seed_and_id() -> None:
    args = (DEFAULT_SAMPLE_SEED, "population_core", "architizer", "ordinary", "atz:1")
    base = deterministic_score(*args)
    assert len(base) == 64
    assert base == deterministic_score(*args)
    assert base != deterministic_score(DEFAULT_SAMPLE_SEED, "risk_sentinel", *args[2:])
    assert base != deterministic_score("another-seed", *args[1:])


def test_prior_ledger_separates_failed_expected_only_rows() -> None:
    ledger = PriorIdentityLedger(
        (),
        ({"selection_id": "a"},),
        (
            {
                "expected_e1_pixel_sha256": "1" * 64,
                "derivative_pixel_sha256": "2" * 64,
                "canonical_url": "https://a/c",
                "fetch_url": "https://a/f",
                "planned_final_url": None,
                "actual_final_url": "https://a/final",
            },
            {
                "expected_e1_pixel_sha256": "3" * 64,
                "derivative_pixel_sha256": None,
                "canonical_url": "https://b/c",
                "fetch_url": "https://b/f",
                "planned_final_url": None,
                "actual_final_url": None,
            },
        ),
    )
    assert ledger.expected_e1_pixels == {"1" * 64, "3" * 64}
    assert ledger.successful_derivative_pixels == {"2" * 64}
    assert len(ledger.urls) == 5


def test_manifest_write_is_canonical_and_no_clobber(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    write_manifest(path, {"z": 1, "a": 2})
    assert path.read_bytes() == b'{"a":2,"z":1}\n'
    with pytest.raises(FileExistsError):
        write_manifest(path, {"a": 2, "z": 1})


def test_planner_source_does_not_read_semantic_result_labels() -> None:
    path = Path("canonical/cross_source_semantic_coverage_n10_v11.py")
    source = path.read_text(encoding="utf-8")
    assert "FROM semantic_results" not in source
    assert "known inference IDs, or result errors participate in selection" in source
