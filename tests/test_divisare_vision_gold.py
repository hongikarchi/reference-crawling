from __future__ import annotations

import copy
from pathlib import Path

import pytest

import canonical.divisare_vision_gold as gold


def _score(*, filename: str, article_kind: str = "photo_feature", **overrides):
    values = {
        "filename": filename,
        "role": "cover",
        "position": 0,
        "article_kind": article_kind,
        "image_hints": frozenset(),
        "article_hints": frozenset(),
        "albums": frozenset(),
        "tags": frozenset(),
    }
    values.update(overrides)
    return gold._score_classes(**values)


def test_aerial_filename_uses_semantic_token_boundaries() -> None:
    for false_positive in (
        "androne-pianoterra.jpg",
        "ladybird-house.jpg",
        "birdhouse.jpg",
        "bird-hide.jpg",
        "bird-factory.jpg",
    ):
        assert "aerial" not in _score(filename=false_positive)

    for aerial in ("aerial-view.jpg", "drone-01.jpg", "birds-eye-view.jpg"):
        assert _score(filename=aerial)["aerial"][0] == 95


def test_weak_aerial_tag_fallback_excludes_non_photo_evidence() -> None:
    tags = frozenset({"building-in-landscape"})
    assert _score(filename="plain.jpg", tags=tags)["aerial"][0] == 55
    assert "aerial" not in _score(
        filename="plain.jpg", article_kind="drawing_feature", tags=tags
    )
    assert "aerial" not in _score(
        filename="plain.jpg", tags=tags, article_hints=frozenset({"plan"})
    )
    assert "aerial" not in _score(
        filename="plain.jpg", tags=tags, image_hints=frozenset({"interior"})
    )


def _manifest() -> dict:
    source_sha = "a" * 64
    contract = {
        "manifest_version": gold.CANDIDATE_MANIFEST_VERSION,
        "selection_version": gold.SELECTION_VERSION,
        "source_db_filename": "source.db",
        "source_db_sha256": source_sha,
        "source_profile": gold.SOURCE_PROFILE,
        "review_profile": gold.REVIEW_PROFILE,
        "identity_profile": gold.IDENTITY_PROFILE,
        "pixel_hash_version": gold.PIXEL_HASH_VERSION,
        "phash_version": gold.PHASH_VERSION,
        "class_order": list(gold.CLASSES),
        "scarcity_order": list(gold.SCARCITY_ORDER),
        "pool_targets": gold.POOL_TARGETS,
        "final_cell_quotas": {
            "%s_%s" % key: value for key, value in gold.FINAL_CELL_QUOTAS.items()
        },
        "review_policy": {"hints_hidden_by_default": True},
    }
    candidates = []
    class_ranks = {label: 0 for label in gold.CLASSES}
    for label in gold.CLASSES:
        for generation in gold.GENERATION_GROUPS:
            for _ in range(gold.POOL_TARGETS[label][generation]):
                rank = len(candidates) + 1
                class_ranks[label] += 1
                asset_key = "asset-%04d" % rank
                source_url = "https://images.divisare.com/image/upload/v1/%s.jpg" % asset_key
                candidates.append(
                    {
                        "candidate_id": "candidate-%04d" % rank,
                        "candidate_rank": rank,
                        "class_rank": class_ranks[label],
                        "discovery_class": label,
                        "discovery_score": 55,
                        "generation_group": generation,
                        "asset_key": asset_key,
                        "article_id": rank,
                        "building_id": "building-%04d" % rank,
                        "source_url": source_url,
                        "request_url": gold.fixed_derivative_url(
                            source_url, gold.SOURCE_PROFILE
                        ),
                        "review_url": gold.fixed_derivative_url(
                            source_url, gold.REVIEW_PROFILE
                        ),
                        "url_generation": (
                            "cloudinary_public_id" if generation == "modern" else "legacy_url"
                        ),
                        "country_cap_fallback": False,
                        "stable_order": gold._stable_hex(label, asset_key),
                    }
                )
    payload = {
        "manifest_version": gold.CANDIDATE_MANIFEST_VERSION,
        "source_db_filename": "source.db",
        "source_db_sha256": source_sha,
        "contract": contract,
        "candidates": candidates,
    }
    payload["manifest_sha256"] = gold.manifest_sha256(payload)
    return payload


def test_candidate_manifest_validator_binds_contract_and_identity() -> None:
    payload = _manifest()
    gold.validate_candidate_manifest(payload)

    changed = copy.deepcopy(payload)
    changed["contract"]["selection_version"] = "wrong"
    changed["manifest_sha256"] = gold.manifest_sha256(changed)
    with pytest.raises(ValueError, match="selection_version"):
        gold.validate_candidate_manifest(changed)

    changed = copy.deepcopy(payload)
    changed["candidates"][0]["request_url"] = "https://example.com/image.jpg"
    changed["manifest_sha256"] = gold.manifest_sha256(changed)
    with pytest.raises(ValueError, match="request_url"):
        gold.validate_candidate_manifest(changed)


def test_candidate_manifest_writer_is_no_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _manifest()
    monkeypatch.setattr(gold, "candidate_manifest_payload", lambda _source: payload)
    output = tmp_path / "manifest.json"
    gold.write_candidate_manifest(tmp_path / "source.db", output)
    before = output.read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        gold.write_candidate_manifest(tmp_path / "source.db", output)
    assert output.read_bytes() == before
