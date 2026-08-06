from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from canonical import divisare_vision_axes_devset as subject


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _inputs(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict, dict, subject.N100Audit, dict]:
    source_sha = _sha("source")
    values = {
        "EXPECTED_SOURCE_DB_SHA256": source_sha,
        "EXPECTED_PARENT_CANDIDATE_MANIFEST_SHA256": _sha("candidate-manifest"),
        "EXPECTED_PARENT_CANDIDATE_FILE_SHA256": _sha("candidate-file"),
        "EXPECTED_REVIEWED_POOL_SHA256": _sha("reviewed-pool"),
        "EXPECTED_REVIEWED_POOL_FILE_SHA256": _sha("reviewed-file"),
        "EXPECTED_OLD_GOLD_MANIFEST_SHA256": _sha("old-gold-manifest"),
        "EXPECTED_OLD_GOLD_FILE_SHA256": _sha("old-gold-file"),
        "EXPECTED_OLD_GOLD_LOGICAL_SHA256": _sha("old-gold-logical"),
        "EXPECTED_OLD_N100_DB_FILE_SHA256": _sha("old-n100-file"),
        "EXPECTED_OLD_N100_LOGICAL_SHA256": _sha("old-n100-logical"),
    }
    for name, value in values.items():
        monkeypatch.setattr(subject, name, value)

    candidates = []
    ordered_ids = sorted(subject.ALL_SELECTED_IDS)
    for index, candidate_id in enumerate(ordered_ids, 1):
        generation = "modern" if index <= 37 else "legacy"
        url_generation = (
            "cloudinary_public_id" if generation == "modern" else "project_images"
        )
        candidates.append(
            {
                "candidate_id": candidate_id,
                "candidate_rank": int(candidate_id.split("-")[1]),
                "asset_key": "asset-%03d" % index,
                "article_id": 10_000 + index,
                "building_id": "building-%03d" % index,
                "generation_group": generation,
                "url_generation": url_generation,
                "request_url": "https://example.test/max2048/%s.jpg" % candidate_id,
                "review_url": "https://example.test/max1024/%s.jpg" % candidate_id,
                "probe_status": "success",
                "probe_final_url": "https://example.test/max2048/%s.jpg" % candidate_id,
                "probe_completed_at": "2026-08-05T00:00:00Z",
                "probe_elapsed_ms": index,
                "http_status": 200,
                "response_mime": "image/jpeg",
                "response_bytes": 1000 + index,
                "content_sha256": _sha("content|" + candidate_id),
                "pixel_sha256": _sha("pixel|" + candidate_id),
                "phash_256": _sha("phash|" + candidate_id),
                "original_format": "JPEG",
                "original_mode": "RGB",
                "original_width": 1600,
                "original_height": 1200,
                "frame_count": 1,
                "exif_orientation": 1,
                "orientation_applied": False,
                "oriented_width": 1600,
                "oriented_height": 1200,
                "alpha_composited": False,
                "icc_profile_present": False,
                "color_normalization": "rgb",
                "normalized_width": 512,
                "normalized_height": 384,
                "is_exact_pixel_duplicate": False,
                "exact_duplicate_group": None,
                "duplicate_of": None,
                "auto_exclude_exact_duplicate": False,
                "has_phash_le8_candidate": False,
                "phash_le8_matches": [],
            }
        )

    candidate_manifest = {
        "manifest_sha256": values["EXPECTED_PARENT_CANDIDATE_MANIFEST_SHA256"],
        "source_db_sha256": source_sha,
        "contract": {
            "source_profile": "c_limit,f_jpg,h_2048,q_92,w_2048",
            "review_profile": "c_limit,f_jpg,h_1024,q_85,w_1024",
            "identity_profile": "identity-v1",
            "pixel_hash_version": "pixel-v1",
            "phash_version": "phash-v1",
        },
        "probe_contract": {"logical_sha256": _sha("probe-logical")},
        "candidates": candidates,
    }
    decisions = [
        {
            "candidate_id": candidate["candidate_id"],
            "disposition": subject.EXPECTED_PRIOR_DISPOSITIONS[candidate["candidate_id"]],
        }
        for candidate in candidates
    ]
    reviewed_pool = {
        "reviewed_pool_sha256": values["EXPECTED_REVIEWED_POOL_SHA256"],
        "candidate_manifest_sha256": values[
            "EXPECTED_PARENT_CANDIDATE_MANIFEST_SHA256"
        ],
        "source_db_sha256": source_sha,
        "reviewer": "agent-reviewer",
        "exported_at": "2026-08-05T00:00:00Z",
        "decisions": decisions,
    }
    old_gold = {
        "gold_manifest_sha256": values["EXPECTED_OLD_GOLD_MANIFEST_SHA256"],
        "logical_sha256": values["EXPECTED_OLD_GOLD_LOGICAL_SHA256"],
        "provenance": {
            "source_db_sha256": source_sha,
            "candidate_manifest_sha256": values[
                "EXPECTED_PARENT_CANDIDATE_MANIFEST_SHA256"
            ],
            "reviewed_pool_sha256": values["EXPECTED_REVIEWED_POOL_SHA256"],
        },
    }
    prior_rows: dict[str, dict] = {}
    for candidate_id in subject.STRATUM_IDS["prior_1024_error"]:
        prior_rows[candidate_id] = {
            "candidate_id": candidate_id,
            "clarity": "clear",
            "primary_correct": 0,
        }
    for candidate_id in subject.STRATUM_IDS["axis_boundary"]:
        prior_rows[candidate_id] = {
            "candidate_id": candidate_id,
            "clarity": "boundary",
            "primary_correct": 1,
        }
    for candidate_id in subject.STRATUM_IDS["clear_control"]:
        prior_rows[candidate_id] = {
            "candidate_id": candidate_id,
            "clarity": "clear",
            "primary_correct": 1,
        }
    n100_audit = subject.N100Audit(
        file_sha256=values["EXPECTED_OLD_N100_DB_FILE_SHA256"],
        logical_sha256=values["EXPECTED_OLD_N100_LOGICAL_SHA256"],
        status="failed_quality_gate",
        benchmark_version="old-benchmark-v1",
        gold_manifest_file_sha256=values["EXPECTED_OLD_GOLD_FILE_SHA256"],
        gold_manifest_sha256=values["EXPECTED_OLD_GOLD_MANIFEST_SHA256"],
        gold_logical_sha256=values["EXPECTED_OLD_GOLD_LOGICAL_SHA256"],
        source_db_sha256=source_sha,
        long1024_by_candidate=prior_rows,
    )

    monkeypatch.setattr(
        subject.gold_contract,
        "validate_enriched_candidate_manifest",
        lambda payload: payload["candidates"],
    )
    monkeypatch.setattr(
        subject.gold_contract,
        "validate_reviewed_pool",
        lambda payload, _manifest: payload["decisions"],
    )
    monkeypatch.setattr(subject.gold_contract, "validate_gold_manifest", lambda _payload: None)
    return candidate_manifest, reviewed_pool, old_gold, n100_audit, values


def _build(monkeypatch: pytest.MonkeyPatch) -> dict:
    candidate, review, gold, audit, values = _inputs(monkeypatch)
    return subject.build_devset_payload(
        candidate_manifest=candidate,
        reviewed_pool=review,
        old_gold=gold,
        n100_audit=audit,
        candidate_file_sha256=values["EXPECTED_PARENT_CANDIDATE_FILE_SHA256"],
        reviewed_pool_file_sha256=values["EXPECTED_REVIEWED_POOL_FILE_SHA256"],
        old_gold_file_sha256=values["EXPECTED_OLD_GOLD_FILE_SHA256"],
    )


def _rehash(payload: dict) -> None:
    payload["logical_sha256"] = subject.logical_sha256(payload)
    payload["manifest_sha256"] = subject.manifest_sha256(payload)


def test_frozen_id_contract_and_nested_prefixes() -> None:
    assert len(subject.N10_IDS) == 10
    assert len(subject.N20_IDS) == 20
    assert len(subject.ORDERED_N50_IDS) == 50
    assert subject.ORDERED_N50_IDS[:10] == subject.N10_IDS
    assert subject.ORDERED_N50_IDS[:20] == subject.N20_IDS
    assert "candidate-0492" in subject.ORDERED_N50_IDS
    assert "candidate-0432" not in subject.ORDERED_N50_IDS
    assert (
        subject._selected_id_set_sha256(subject.ORDERED_N50_IDS)
        == subject.EXPECTED_SELECTED_ID_SET_SHA256
    )


def test_build_is_deterministic_blinded_and_self_validating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _build(monkeypatch)
    second = _build(monkeypatch)
    assert first == second
    subject.validate_devset_manifest(first)
    assert first["development_only"] is True
    assert first["manifest_sha256"] == subject.manifest_sha256(first)
    assert first["logical_sha256"] == subject.logical_sha256(first)
    assert [row["source_identity"]["candidate_id"] for row in first["audit_samples"][:10]] == list(subject.N10_IDS)
    assert [row["source_identity"]["candidate_id"] for row in first["audit_samples"][:20]] == list(subject.N20_IDS)
    assert all(set(row) == subject.PUBLIC_REVIEW_FIELDS for row in first["review_rows"])
    public_text = repr(first["review_rows"])
    assert "candidate-" not in public_text
    assert "discovery" not in public_text
    assert "gold_label" not in public_text
    assert "predicted" not in public_text


def test_blind_id_uses_frozen_exact_formula() -> None:
    candidate_id = "candidate-0065"
    expected = hashlib.sha256(
        ("axis-review-v1|" + candidate_id).encode("ascii")
    ).hexdigest()[:12]
    assert subject.opaque_review_id(candidate_id) == "axis-" + expected


def test_rejects_hard_case_that_was_not_a_clear_1024_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, review, gold, audit, values = _inputs(monkeypatch)
    changed_rows = copy.deepcopy(dict(audit.long1024_by_candidate))
    changed_rows[subject.STRATUM_IDS["prior_1024_error"][0]]["primary_correct"] = 1
    changed_audit = subject.N100Audit(
        **{**audit.__dict__, "long1024_by_candidate": changed_rows}
    )
    with pytest.raises(ValueError, match="hard-case N100 audit mismatch"):
        subject.build_devset_payload(
            candidate_manifest=candidate,
            reviewed_pool=review,
            old_gold=gold,
            n100_audit=changed_audit,
            candidate_file_sha256=values["EXPECTED_PARENT_CANDIDATE_FILE_SHA256"],
            reviewed_pool_file_sha256=values["EXPECTED_REVIEWED_POOL_FILE_SHA256"],
            old_gold_file_sha256=values["EXPECTED_OLD_GOLD_FILE_SHA256"],
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("probe_status", "failed", "probe did not succeed"),
        ("is_exact_pixel_duplicate", True, "exact-duplicate flag"),
        ("has_phash_le8_candidate", True, "pHash <=8 evidence"),
    ),
)
def test_rejects_unsafe_selected_candidate_evidence(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object, message: str
) -> None:
    candidate, review, gold, audit, values = _inputs(monkeypatch)
    target = next(
        row for row in candidate["candidates"] if row["candidate_id"] == subject.N10_IDS[0]
    )
    target[field] = value
    with pytest.raises(ValueError, match=message):
        subject.build_devset_payload(
            candidate_manifest=candidate,
            reviewed_pool=review,
            old_gold=gold,
            n100_audit=audit,
            candidate_file_sha256=values["EXPECTED_PARENT_CANDIDATE_FILE_SHA256"],
            reviewed_pool_file_sha256=values["EXPECTED_REVIEWED_POOL_FILE_SHA256"],
            old_gold_file_sha256=values["EXPECTED_OLD_GOLD_FILE_SHA256"],
        )


def test_validator_rejects_public_metadata_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _build(monkeypatch)
    changed = copy.deepcopy(payload)
    changed["review_rows"][0]["candidate_id"] = "candidate-0001"
    _rehash(changed)
    with pytest.raises(ValueError, match="public review row fields mismatch"):
        subject.validate_devset_manifest(changed)


def test_validator_rejects_tampering(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _build(monkeypatch)
    changed = copy.deepcopy(payload)
    changed["audit_samples"][0]["source_identity"]["article_id"] = -1
    with pytest.raises(ValueError, match="(?:logical|manifest) SHA mismatch"):
        subject.validate_devset_manifest(changed)


def test_no_clobber_writer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = _build(monkeypatch)
    output = tmp_path / "axes-dev.json"
    subject.write_json_no_clobber(output, payload)
    before = output.read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        subject.write_json_no_clobber(output, payload)
    assert output.read_bytes() == before
    assert not list(tmp_path.glob("*.partial"))
