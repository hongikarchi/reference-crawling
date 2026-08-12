from __future__ import annotations

from pathlib import Path

import pytest

from canonical.cross_source_semantic_coverage import BuildingInventoryItem
from canonical.cross_source_semantic_coverage_n10_v12 import (
    DEFAULT_SAMPLE_SEED,
    EXPECTED_BUILDINGS,
    EXPECTED_OCCURRENCES,
    QUOTAS,
    deterministic_score,
    select_fixed_sample,
    write_manifest,
)


def _item(source: str, stratum: str, number: int) -> BuildingInventoryItem:
    return BuildingInventoryItem(
        selection_id=f"{source}:building:{stratum}:{number:03d}",
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
    assert first == select_fixed_sample(reversed(inventory), excluded_selection_ids=())
    assert len(first) == EXPECTED_BUILDINGS == 10
    assert sum(value[0] == "population_core" for value in first) == 8
    assert sum(value[0] == "risk_sentinel" for value in first) == 2
    assert sum(value[1] == "architizer" for value in first) == 7
    assert sum(value[1] == "divisare" for value in first) == 3
    assert EXPECTED_OCCURRENCES == 50


def test_excluded_winner_is_replaced_deterministically() -> None:
    inventory = _inventory()
    first = select_fixed_sample(inventory, excluded_selection_ids=())
    excluded = first[0][6].selection_id
    second = select_fixed_sample(inventory, excluded_selection_ids=(excluded,))
    assert excluded not in {value[6].selection_id for value in second}


def test_score_binds_complete_v12_selection_identity() -> None:
    args = (DEFAULT_SAMPLE_SEED, "population_core", "architizer", "ordinary", "atz:1")
    base = deterministic_score(*args)
    assert len(base) == 64 and base == deterministic_score(*args)
    assert base != deterministic_score("other", *args[1:])
    assert base != deterministic_score(DEFAULT_SAMPLE_SEED, "risk_sentinel", *args[2:])


def test_manifest_write_is_canonical_no_clobber(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    write_manifest(path, {"z": 1, "a": 2})
    assert path.read_bytes() == b'{"a":2,"z":1}\n'
    with pytest.raises(FileExistsError):
        write_manifest(path, {"a": 2, "z": 1})


def test_planner_does_not_query_semantic_result_labels() -> None:
    source = Path("canonical/cross_source_semantic_coverage_n10_v12.py").read_text("utf-8")
    assert "FROM semantic_results" not in source
    assert "known inference IDs, or result errors participate in selection" in source
