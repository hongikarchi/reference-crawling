from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.divisare_n100_review import (
    DRAFT_SCHEMA,
    build_export,
    load_draft,
    manifest_sha256,
    merge_import,
    public_manifest,
    reviewed_pool_sha256,
    save_decision,
    validate_decision,
    validate_import,
    validate_manifest,
    write_json_no_clobber,
)


def _manifest() -> dict:
    payload = {
        "manifest_version": "divisare-vision-gold-candidates-v1.0.0",
        "kind": "review_candidate_pool",
        "source_db_path": "data/curated/divisare_metadata_v2_4.db",
        "source_db_sha256": "a" * 64,
        "selection_version": "test-v1",
        "contract": {"profile": "max2048-jpeg-q92"},
        "items": [
            {
                "candidate_id": "candidate-0001",
                "asset_key": "divisare|asset-1",
                "article_id": 101,
                "building_id": "divisare-building-1",
                "request_url": "https://images.divisare.com/asset-1.jpg",
                "review_url": "https://images.divisare.com/asset-1-1024.jpg",
                "source_url": "https://example.invalid/source-1.jpg",
                "delivery_lane": "modern",
                "url_generation": "cloudinary_public_id",
                "role": "gallery",
                "weak_hints": {"article": ["interiors"]},
                "discovery_class": "interior",
                "pixel_sha256": "1" * 64,
            },
            {
                "candidate_id": "candidate-0002",
                "asset_key": "divisare|asset-2",
                "article_id": 102,
                "building_id": "divisare-building-2",
                "request_url": "https://images.divisare.com/asset-2.jpg",
                "delivery_lane": "legacy",
                "filename_hints": ["plan"],
            },
        ],
    }
    payload["manifest_sha256"] = manifest_sha256(payload)
    return payload


def _include(candidate_id: str = "candidate-0001") -> dict:
    return {
        "candidate_id": candidate_id,
        "disposition": "include",
        "gold_label": "interior",
        "clarity": "clear",
        "acceptable_labels": ["interior"],
        "notes": "visible enclosed room",
        "reviewed_at": "2026-08-05T00:00:00+00:00",
    }


def test_manifest_sha_binds_all_content_and_aliases_url() -> None:
    manifest = _manifest()
    assert validate_manifest(manifest)["manifest_sha256"] == manifest["manifest_sha256"]
    public = public_manifest(manifest)
    assert public["items"][0]["review_url"].endswith("asset-1-1024.jpg")
    assert public["items"][0]["high_resolution_url"].endswith("asset-1.jpg")
    assert public["items"][1]["review_url"].endswith("asset-2.jpg")

    changed = copy.deepcopy(manifest)
    changed["items"][0]["asset_key"] = "divisare|tampered"
    with pytest.raises(ValueError, match="manifest SHA mismatch"):
        validate_manifest(changed)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m["items"].__setitem__(1, copy.deepcopy(m["items"][0])), "duplicate candidate_id"),
        (lambda m: m["items"][0].__setitem__("request_url", "http://images.divisare.com/a.jpg"), "HTTPS"),
        (lambda m: m["items"][0].__setitem__("building_id", None), "building_id"),
        (lambda m: m["items"][1].__setitem__("article_id", 101), "duplicate article_id"),
        (lambda m: m["items"][1].__setitem__("building_id", "divisare-building-1"), "duplicate building_id"),
    ],
)
def test_manifest_rejects_invalid_identity_or_url(mutate, message: str) -> None:
    manifest = _manifest()
    mutate(manifest)
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    with pytest.raises(ValueError, match=message):
        validate_manifest(manifest)


def test_decision_contract_handles_clear_boundary_and_exclude() -> None:
    clear = validate_decision(_include())
    assert clear["acceptable_labels"] == ["interior"]
    assert clear["high_res_viewed"] is False

    high_res = _include()
    high_res["high_res_viewed"] = True
    assert validate_decision(high_res)["high_res_viewed"] is True
    invalid_high_res = _include()
    invalid_high_res["high_res_viewed"] = "yes"
    with pytest.raises(ValueError, match="boolean"):
        validate_decision(invalid_high_res)

    boundary = _include()
    boundary.update(
        clarity="boundary",
        gold_label="exterior",
        acceptable_labels=["interior", "exterior"],
    )
    assert validate_decision(boundary)["acceptable_labels"] == ["exterior", "interior"]

    one_label_boundary = copy.deepcopy(boundary)
    one_label_boundary["acceptable_labels"] = ["exterior"]
    with pytest.raises(ValueError, match="at least two"):
        validate_decision(one_label_boundary)

    invalid_clear = _include()
    invalid_clear["acceptable_labels"] = ["interior", "detail"]
    with pytest.raises(ValueError, match="clear items"):
        validate_decision(invalid_clear)

    excluded = {
        "candidate_id": "candidate-0001",
        "disposition": "exclude",
        "gold_label": None,
        "clarity": None,
        "acceptable_labels": [],
        "notes": "broken image",
    }
    assert validate_decision(excluded)["disposition"] == "exclude"
    excluded["gold_label"] = "detail"
    with pytest.raises(ValueError, match="excluded items"):
        validate_decision(excluded)


def test_decision_rejects_invalid_reviewed_at() -> None:
    invalid = _include()
    invalid["reviewed_at"] = 123
    with pytest.raises(ValueError, match="reviewed_at"):
        validate_decision(invalid)

    blank = _include()
    blank["reviewed_at"] = "  "
    with pytest.raises(ValueError, match="reviewed_at"):
        validate_decision(blank)


def test_draft_is_manifest_bound_and_atomic(tmp_path: Path) -> None:
    manifest = _manifest()
    draft_path = tmp_path / "draft.json"
    draft = save_decision(
        draft_path=draft_path,
        manifest=manifest,
        reviewer="reviewer-a",
        payload=_include(),
    )
    assert draft["schema_version"] == DRAFT_SCHEMA
    assert load_draft(draft_path, manifest, "other")["reviewer"] == "reviewer-a"

    other = copy.deepcopy(manifest)
    other["selection_version"] = "test-v2"
    other["manifest_sha256"] = manifest_sha256(other)
    with pytest.raises(ValueError, match="different manifest SHA"):
        load_draft(draft_path, other, "reviewer-a")


def test_export_is_hint_free_and_hash_verified(tmp_path: Path) -> None:
    manifest = _manifest()
    draft_path = tmp_path / "draft.json"
    draft = save_decision(
        draft_path=draft_path,
        manifest=manifest,
        reviewer="reviewer-a",
        payload=_include(),
    )
    exported = build_export(manifest, draft)
    assert exported["candidate_manifest_sha256"] == manifest["manifest_sha256"]
    assert exported["reviewed_pool_sha256"] == reviewed_pool_sha256(exported)
    assert exported["reviewer"] == "reviewer-a"
    assert exported["decided_count"] == 1
    assert exported["decisions"][0]["request_url"].endswith("asset-1.jpg")
    assert exported["decisions"][0]["review_url"].endswith("asset-1-1024.jpg")
    serialized = json.dumps(exported)
    assert "weak_hints" not in serialized
    assert "discovery_class" not in serialized

    tampered = copy.deepcopy(exported)
    tampered["decisions"][0]["gold_label"] = "detail"
    with pytest.raises(ValueError, match="reviewed pool SHA mismatch"):
        validate_import(tampered, manifest)


def test_import_merges_only_nonconflicting_manifest_bound_rows(tmp_path: Path) -> None:
    manifest = _manifest()
    draft_path = tmp_path / "draft.json"
    source_draft = save_decision(
        draft_path=tmp_path / "source-draft.json",
        manifest=manifest,
        reviewer="reviewer-a",
        payload=_include(),
    )
    export = build_export(manifest, source_draft)
    merged = merge_import(
        draft_path=draft_path,
        manifest=manifest,
        reviewer="reviewer-a",
        payload=export,
    )
    assert list(merged["decisions"]) == ["candidate-0001"]

    conflict = copy.deepcopy(export)
    conflict["decisions"][0]["notes"] = "different"
    conflict["reviewed_pool_sha256"] = reviewed_pool_sha256(conflict)
    with pytest.raises(ValueError, match="conflicts"):
        merge_import(
            draft_path=draft_path,
            manifest=manifest,
            reviewer="reviewer-a",
            payload=conflict,
        )

    wrong_manifest = copy.deepcopy(export)
    wrong_manifest["candidate_manifest_sha256"] = "f" * 64
    wrong_manifest["reviewed_pool_sha256"] = reviewed_pool_sha256(wrong_manifest)
    with pytest.raises(ValueError, match="different manifest SHA"):
        validate_import(wrong_manifest, manifest)

    wrong_count = copy.deepcopy(export)
    wrong_count["decided_count"] = 2
    wrong_count["reviewed_pool_sha256"] = reviewed_pool_sha256(wrong_count)
    with pytest.raises(ValueError, match="decided_count"):
        validate_import(wrong_count, manifest)

    leaked = copy.deepcopy(export)
    leaked["decisions"][0]["weak_hints"] = {"filename": "interior"}
    leaked["reviewed_pool_sha256"] = reviewed_pool_sha256(leaked)
    with pytest.raises(ValueError, match="forbidden hint"):
        validate_import(leaked, manifest)


def test_no_clobber_export(tmp_path: Path) -> None:
    output = tmp_path / "review.json"
    write_json_no_clobber(output, {"value": 1})
    assert json.loads(output.read_text(encoding="utf-8")) == {"value": 1}
    before = output.read_bytes()
    with pytest.raises(FileExistsError, match="immutable output"):
        write_json_no_clobber(output, {"value": 2})
    assert output.read_bytes() == before
