from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import BuildingInventoryItem
from canonical.cross_source_semantic_coverage_n100 import (
    N10_MANIFEST_DOMAIN,
    PriorN10Spec,
    deterministic_n100_score,
    hierarchical_hamilton_quotas,
    load_prior_n10_exclusions,
    select_stratified_n100,
    write_semantic_coverage_n100_manifest,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _building(source: str, stratum: str, index: int) -> BuildingInventoryItem:
    key = f"{source}-{stratum}-{index:03d}"
    return BuildingInventoryItem(
        selection_id=f"{source}:building:{key}",
        source=source,
        source_building_id=key,
        name=key,
        population_stratum=stratum,
        successful_asset_count=6,
        successful_gallery_count=6,
        source_record_sha256=_sha("source:" + key),
        selection_record_sha256=_sha("selection:" + key),
        cross_source_candidate=stratum == "cross_source_candidate",
    )


def test_hierarchical_hamilton_is_source_then_stratum_and_sums_exactly() -> None:
    inventory = tuple(
        [_building("architizer", "ordinary", i) for i in range(6)]
        + [_building("architizer", "cross_source_candidate", 0)]
        + [_building("divisare", "ordinary", i) for i in range(3)]
    )
    quotas, record = hierarchical_hamilton_quotas(inventory, sample_size=5)
    assert quotas == {
        ("architizer", "cross_source_candidate"): 1,
        ("architizer", "ordinary"): 3,
        ("divisare", "ordinary"): 1,
    }
    assert sum(item["allocated_quota"] for item in record["source_level"]) == 5
    assert sum(quotas.values()) == 5


def test_n100_selection_is_order_independent_disjoint_and_does_not_force_rare() -> None:
    inventory = tuple(
        [_building("architizer", "ordinary", i) for i in range(80)]
        + [_building("architizer", "cover_quality_risk", 0)]
        + [_building("divisare", "ordinary", i) for i in range(39)]
    )
    excluded = {inventory[0].selection_id, inventory[-1].selection_id}
    first, quotas = select_stratified_n100(
        inventory,
        excluded_selection_ids=excluded,
        seed="fixed-n100",
        sample_size=20,
    )
    replay, replay_quotas = select_stratified_n100(
        reversed(inventory),
        excluded_selection_ids=reversed(sorted(excluded)),
        seed="fixed-n100",
        sample_size=20,
    )
    assert [value.building.selection_id for value in first] == [
        value.building.selection_id for value in replay
    ]
    assert quotas == replay_quotas
    assert not ({value.building.selection_id for value in first} & excluded)
    assert not any(
        value.building.population_stratum == "cover_quality_risk" for value in first
    )


def test_n100_score_is_domain_separated_and_seed_sensitive() -> None:
    first = deterministic_n100_score("seed-a", "architizer", "ordinary", "id-a")
    assert first == deterministic_n100_score(
        "seed-a", "architizer", "ordinary", "id-a"
    )
    assert first != deterministic_n100_score(
        "seed-b", "architizer", "ordinary", "id-a"
    )


def test_manifest_write_is_canonical_exclusive_no_clobber(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    payload = {"z": 1, "a": [2, 3]}
    assert write_semantic_coverage_n100_manifest(output, payload) == output.resolve()
    assert output.read_bytes() == (canonical_json(payload) + "\n").encode()
    with pytest.raises(FileExistsError):
        write_semantic_coverage_n100_manifest(output, payload)


def _prior_fixture(tmp_path: Path) -> PriorN10Spec:
    manifest_path = tmp_path / "n10.json"
    body = {
        "ordered_building_manifest_sha256": _sha("ordered-buildings"),
        "ordered_occurrence_manifest_sha256": _sha("ordered-occurrences"),
        "sample_seed": "fixture-n10-seed",
        "sample_size_buildings": 10,
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": N10_MANIFEST_DOMAIN, "manifest": body}
    )
    manifest_path.write_text(canonical_json(body) + "\n", encoding="utf-8", newline="\n")
    manifest_raw = manifest_path.read_bytes()

    sidecar_path = tmp_path / "n10.db"
    connection = sqlite3.connect(sidecar_path)
    connection.executescript(
        """
        PRAGMA application_id=1398165041;
        PRAGMA user_version=1;
        CREATE TABLE semantic_runs(
          run_id TEXT,status TEXT,logical_sha256 TEXT,manifest_size INTEGER,
          manifest_byte_sha256 TEXT,manifest_self_sha256 TEXT
        );
        CREATE TABLE selected_buildings(
          building_rank INTEGER,selection_id TEXT,source TEXT,source_building_id TEXT
        );
        CREATE TABLE selected_occurrences(
          run_id TEXT,input_rank INTEGER,inference_id TEXT,selection_id TEXT,
          candidate_id TEXT,source TEXT,source_asset_id TEXT,fetch_url TEXT,
          expected_e1_pixel_sha256 TEXT,manifest_json TEXT
        );
        CREATE TABLE vision_inputs(
          run_id TEXT,inference_id TEXT,actual_e1_pixel_sha256 TEXT,
          derivative_pixel_sha256 TEXT,selected_fetch_attempt_no INTEGER
        );
        CREATE TABLE fetch_attempts(
          run_id TEXT,inference_id TEXT,attempt_no INTEGER,final_url TEXT
        );
        """
    )
    run_id = "fixture-n10-run"
    logical = _sha("logical")
    connection.execute(
        "INSERT INTO semantic_runs VALUES(?,?,?,?,?,?)",
        (
            run_id,
            "complete",
            logical,
            len(manifest_raw),
            hashlib.sha256(manifest_raw).hexdigest(),
            body["semantic_coverage_manifest_sha256"],
        ),
    )
    for building_rank in range(1, 11):
        source = "architizer" if building_rank <= 5 else "divisare"
        selection_id = f"{source}:building:b{building_rank}"
        connection.execute(
            "INSERT INTO selected_buildings VALUES(?,?,?,?)",
            (building_rank, selection_id, source, f"b{building_rank}"),
        )
    for input_rank in range(1, 58):
        building_rank = (input_rank - 1) % 10 + 1
        source = "architizer" if building_rank <= 5 else "divisare"
        selection_id = f"{source}:building:b{building_rank}"
        inference_id = f"semv_{input_rank:06d}"
        candidate = {
            "canonical_url": f"https://canonical/{input_rank}",
            "final_url": f"https://final/{input_rank}",
        }
        wrapper = {"occurrence": {"candidate": candidate}}
        expected = _sha(f"e1:{input_rank}")
        connection.execute(
            "INSERT INTO selected_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                input_rank,
                inference_id,
                selection_id,
                f"candidate-{input_rank}",
                source,
                f"asset-{input_rank}",
                f"https://fetch/{input_rank}",
                expected,
                canonical_json(wrapper),
            ),
        )
        connection.execute(
            "INSERT INTO vision_inputs VALUES(?,?,?,?,?)",
            (run_id, inference_id, expected, _sha(f"derivative:{input_rank}"), 1),
        )
        connection.execute(
            "INSERT INTO fetch_attempts VALUES(?,?,?,?)",
            (run_id, inference_id, 1, f"https://actual/{input_rank}"),
        )
    connection.commit()
    connection.close()
    sidecar_raw = sidecar_path.read_bytes()
    return PriorN10Spec(
        manifest_path=manifest_path,
        sidecar_path=sidecar_path,
        expected_manifest_size=len(manifest_raw),
        expected_manifest_byte_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        expected_manifest_self_sha256=body["semantic_coverage_manifest_sha256"],
        expected_sidecar_size=len(sidecar_raw),
        expected_sidecar_byte_sha256=hashlib.sha256(sidecar_raw).hexdigest(),
        expected_sidecar_logical_sha256=logical,
        expected_run_id=run_id,
    )


def test_prior_n10_loader_freezes_building_pixel_url_and_derivative_identities(
    tmp_path: Path,
) -> None:
    prior = load_prior_n10_exclusions(_prior_fixture(tmp_path))
    assert len(prior.building_ids) == 10
    assert len(prior.occurrence_records) == 57
    assert len(prior.e1_pixels) == 57
    assert len(prior.derivative_pixels) == 57
    assert len({value["fetch_url"] for value in prior.occurrence_records}) == 57
    assert prior.sidecar_input["sqlite_quick_check"] == "ok"


def test_prior_n10_loader_fails_closed_on_byte_sha_mismatch(tmp_path: Path) -> None:
    spec = _prior_fixture(tmp_path)
    bad = PriorN10Spec(
        **{
            **spec.__dict__,
            "expected_sidecar_byte_sha256": "0" * 64,
        }
    )
    with pytest.raises(ValueError, match="byte SHA"):
        load_prior_n10_exclusions(bad)
