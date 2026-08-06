from __future__ import annotations

import copy

import pytest

from tools.combine_divisare_pixel_reviews import build_reviewed_pool
from tools.divisare_n100_review import manifest_sha256


def _manifest() -> dict:
    item = {
        "candidate_id": "candidate-0001",
        "asset_key": "asset-1",
        "article_id": 1,
        "building_id": "building-1",
        "request_url": "https://images.divisare.com/image/upload/c_limit,f_jpg,h_2048,q_92,w_2048/x.jpg",
        "review_url": "https://images.divisare.com/image/upload/c_limit,f_jpg,h_1024,q_85,w_1024/x.jpg",
        "generation_group": "modern",
        "url_generation": "modern",
        "probe_status": "success",
    }
    payload = {
        "manifest_version": "divisare-vision-gold-candidates-v1.0.0",
        "source_db_sha256": "a" * 64,
        "contract": {"source_profile": "x"},
        "candidates": [item],
    }
    payload["manifest_sha256"] = manifest_sha256(payload)
    return payload


def _annotation() -> dict:
    return {
        "reviewer": "codex-5.6-sol-pixel-review-test",
        "page": 1,
        "blinded_index": 1,
        "candidate_id": "candidate-0001",
        "disposition": "include",
        "gold_label": "exterior",
        "clarity": "clear",
        "acceptable_labels": ["exterior"],
        "high_res_viewed": False,
        "notes": "Outdoor building view.",
    }


def test_build_reviewed_pool_is_complete_and_discloses_agent_review() -> None:
    result = build_reviewed_pool(
        manifest=_manifest(),
        annotations=[_annotation()],
        reviewer="codex-panel",
    )
    assert result["complete"] is True
    assert result["decided_count"] == 1
    assert result["review_provenance"]["independent_human_review"] is False


def test_build_reviewed_pool_rejects_duplicate_candidate() -> None:
    with pytest.raises(ValueError, match="duplicate candidate"):
        build_reviewed_pool(
            manifest=_manifest(),
            annotations=[_annotation(), _annotation()],
            reviewer="codex-panel",
        )


def test_build_reviewed_pool_rejects_wrong_blinded_slot() -> None:
    annotation = _annotation()
    annotation["blinded_index"] = 2
    with pytest.raises(ValueError, match="blinded slot mismatch"):
        build_reviewed_pool(
            manifest=_manifest(),
            annotations=[annotation],
            reviewer="codex-panel",
        )


def test_build_reviewed_pool_requires_failed_probe_exclusion() -> None:
    manifest = _manifest()
    manifest["candidates"][0]["probe_status"] = "failed"
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    with pytest.raises(ValueError, match="failed probe"):
        build_reviewed_pool(
            manifest=manifest,
            annotations=[_annotation()],
            reviewer="codex-panel",
        )


def test_build_reviewed_pool_applies_explicit_adjudication() -> None:
    adjudication = _annotation()
    adjudication.update(
        reviewer="codex-5.6-sol-pixel-review-root-adjudication",
        clarity="boundary",
        acceptable_labels=["exterior", "detail"],
        high_res_viewed=True,
    )
    result = build_reviewed_pool(
        manifest=_manifest(),
        annotations=[_annotation()],
        adjudications=[adjudication],
        reviewer="codex-panel",
    )
    assert result["decisions"][0]["clarity"] == "boundary"
    assert result["decisions"][0]["high_res_viewed"] is True
    assert result["review_provenance"]["adjudicated_candidate_ids"] == [
        "candidate-0001"
    ]


def test_build_reviewed_pool_rejects_missing_coverage() -> None:
    manifest = _manifest()
    second = copy.deepcopy(manifest["candidates"][0])
    second.update(
        candidate_id="candidate-0002",
        asset_key="asset-2",
        article_id=2,
        building_id="building-2",
        request_url=second["request_url"].replace("x.jpg", "y.jpg"),
        review_url=second["review_url"].replace("x.jpg", "y.jpg"),
    )
    manifest["candidates"].append(second)
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    with pytest.raises(ValueError, match="coverage mismatch"):
        build_reviewed_pool(
            manifest=manifest,
            annotations=[_annotation()],
            reviewer="codex-panel",
        )


def test_build_reviewed_pool_rejects_human_like_reviewer_claim() -> None:
    with pytest.raises(ValueError, match="explicit codex"):
        build_reviewed_pool(
            manifest=_manifest(),
            annotations=[_annotation()],
            reviewer="Alice Human",
        )
