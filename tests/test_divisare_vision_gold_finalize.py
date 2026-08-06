from __future__ import annotations

import copy
import hashlib
from collections import Counter
from pathlib import Path

import pytest

import canonical.divisare_vision_gold as candidate_contract
import canonical.divisare_vision_gold_finalize as finalizer
import canonical.divisare_vision_probe as probe_contract


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate_contract(source_sha: str) -> dict:
    return {
        "manifest_version": candidate_contract.CANDIDATE_MANIFEST_VERSION,
        "selection_version": candidate_contract.SELECTION_VERSION,
        "source_db_filename": "source.db",
        "source_db_sha256": source_sha,
        "source_profile": candidate_contract.SOURCE_PROFILE,
        "review_profile": candidate_contract.REVIEW_PROFILE,
        "identity_profile": candidate_contract.IDENTITY_PROFILE,
        "pixel_hash_version": candidate_contract.PIXEL_HASH_VERSION,
        "phash_version": candidate_contract.PHASH_VERSION,
        "class_order": list(candidate_contract.CLASSES),
        "scarcity_order": list(candidate_contract.SCARCITY_ORDER),
        "pool_targets": candidate_contract.POOL_TARGETS,
        "final_cell_quotas": {
            "%s_%s" % key: value
            for key, value in candidate_contract.FINAL_CELL_QUOTAS.items()
        },
        "review_policy": {
            "hints_hidden_by_default": True,
            "exact_pixel_duplicates": "auto_exclude_after_probe",
        },
    }


def _enriched_manifest() -> dict:
    source_sha = "a" * 64
    contract = _candidate_contract(source_sha)
    candidates = []
    attempts = []
    class_ranks = {label: 0 for label in candidate_contract.CLASSES}
    for label in candidate_contract.CLASSES:
        for generation in candidate_contract.GENERATION_GROUPS:
            for _ in range(candidate_contract.POOL_TARGETS[label][generation]):
                rank = len(candidates) + 1
                class_ranks[label] += 1
                candidate_id = "candidate-%04d" % rank
                asset_key = "asset-%04d" % rank
                source_url = (
                    "https://images.divisare.com/image/upload/v1/%s.jpg" % asset_key
                )
                request_url = candidate_contract.fixed_derivative_url(
                    source_url, candidate_contract.SOURCE_PROFILE
                )
                review_url = candidate_contract.fixed_derivative_url(
                    source_url, candidate_contract.REVIEW_PROFILE
                )
                content_sha = _digest("content-%04d" % rank)
                pixel_sha = _digest("pixels-%04d" % rank)
                phash = _digest("phash-%04d" % rank)
                candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "candidate_rank": rank,
                        "class_rank": class_ranks[label],
                        "discovery_class": label,
                        "discovery_score": 55,
                        "generation_group": generation,
                        "asset_key": asset_key,
                        "article_id": rank,
                        "building_id": "building-%04d" % rank,
                        "source_url": source_url,
                        "request_url": request_url,
                        "review_url": review_url,
                        "url_generation": (
                            "cloudinary_public_id"
                            if generation == "modern"
                            else "project_images"
                        ),
                        "original_filename": "%s.jpg" % asset_key,
                        "role": "gallery",
                        "position": 0,
                        "article_kind": "photo_feature",
                        "kind_status": "confirmed",
                        "country": "Testland",
                        "weak_hints": ["test-only"],
                        "country_cap_fallback": False,
                        "stable_order": candidate_contract._stable_hex(label, asset_key),
                        "probe_status": "success",
                        "probe_final_url": request_url,
                        "http_status": 200,
                        "response_mime": "image/jpeg",
                        "response_bytes": 1000,
                        "content_sha256": content_sha,
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
                        "color_normalization": "mode_to_srgb",
                        "normalized_width": 512,
                        "normalized_height": 384,
                        "pixel_sha256": pixel_sha,
                        "phash_256": phash,
                        "exact_duplicate_group": None,
                        "is_exact_pixel_duplicate": False,
                        "duplicate_of": None,
                        "auto_exclude_exact_duplicate": False,
                        "phash_le8_matches": [],
                        "has_phash_le8_candidate": False,
                        "probe_attempt_count": 1,
                        "probe_elapsed_ms": 10,
                        "probe_completed_at": "2026-08-05T00:00:00Z",
                        "probe_error_kind": None,
                        "probe_error_message": None,
                    }
                )
                attempts.append(
                    {
                        "candidate_id": candidate_id,
                        "attempt_no": 1,
                        "started_at": "2026-08-05T00:00:00Z",
                        "elapsed_ms": 10,
                        "outcome": "success",
                        "final_url": request_url,
                        "http_status": 200,
                        "response_mime": "image/jpeg",
                        "response_bytes": 1000,
                        "content_sha256": content_sha,
                        "error_kind": None,
                        "error_message": None,
                    }
                )
    exact, pairs, audit, _, _ = finalizer._expected_duplicate_evidence(candidates)
    assert not exact and not pairs and not audit
    input_candidates = copy.deepcopy(candidates)
    for candidate in input_candidates:
        for field in finalizer.HASH_EVIDENCE_FIELDS:
            candidate.pop(field, None)
    input_manifest = {
        "manifest_version": candidate_contract.CANDIDATE_MANIFEST_VERSION,
        "source_db_filename": "source.db",
        "source_db_sha256": source_sha,
        "contract": copy.deepcopy(contract),
        "candidates": input_candidates,
    }
    input_manifest["manifest_sha256"] = candidate_contract.manifest_sha256(input_manifest)
    runtime_versions = {
        "python": "3.12.12",
        "pillow": "11.3.0",
        "imagehash": "4.3.2",
        "numpy": "2.3.2",
    }
    probe_config = probe_contract.ProbeConfig(workers=4, max_attempts=1)
    results = [
        {
            "candidate_rank": row["candidate_rank"],
            "candidate_id": row["candidate_id"],
            "asset_key": row["asset_key"],
            "request_url": row["request_url"],
            "status": row["probe_status"],
            "attempt_count": row["probe_attempt_count"],
            "elapsed_ms": row["probe_elapsed_ms"],
            "final_url": row["probe_final_url"],
            "http_status": row["http_status"],
            "response_mime": row["response_mime"],
            "response_bytes": row["response_bytes"],
            "content_sha256": row["content_sha256"],
            "original_format": row["original_format"],
            "original_mode": row["original_mode"],
            "original_width": row["original_width"],
            "original_height": row["original_height"],
            "frame_count": row["frame_count"],
            "exif_orientation": row["exif_orientation"],
            "orientation_applied": row["orientation_applied"],
            "oriented_width": row["oriented_width"],
            "oriented_height": row["oriented_height"],
            "alpha_composited": row["alpha_composited"],
            "icc_profile_present": row["icc_profile_present"],
            "color_normalization": row["color_normalization"],
            "normalized_width": row["normalized_width"],
            "normalized_height": row["normalized_height"],
            "pixel_sha256": row["pixel_sha256"],
            "phash_256": row["phash_256"],
            "error_kind": row["probe_error_kind"],
        }
        for row in candidates
    ]
    probe_logical = probe_contract._logical_sha256(
        manifest=input_manifest,
        manifest_file_sha="c" * 64,
        config=probe_config,
        runtime_versions=runtime_versions,
        results=results,
        exact_groups=exact,
        duplicate_pairs=pairs,
        audit_pairs=audit,
    )
    payload = {
        "manifest_version": candidate_contract.CANDIDATE_MANIFEST_VERSION,
        "source_db_filename": "source.db",
        "source_db_sha256": source_sha,
        "contract": contract,
        "candidates": candidates,
        "probe_contract": {
            "probe_version": probe_contract.PROBE_VERSION,
            "identity_profile": candidate_contract.IDENTITY_PROFILE,
            "pixel_hash_version": candidate_contract.PIXEL_HASH_VERSION,
            "phash_version": candidate_contract.PHASH_VERSION,
            "runtime_versions": runtime_versions,
            "input_manifest_sha256": input_manifest["manifest_sha256"],
            "input_manifest_file_sha256": "c" * 64,
            "input_manifest_filename": "candidates.json",
            "source_request_profile": candidate_contract.SOURCE_PROFILE,
            "normalized_long_edge": probe_contract.NORMALIZED_LONG_EDGE,
            "max_bytes": 10 * 1024 * 1024,
            "connect_timeout": 10.0,
            "read_timeout": 30.0,
            "max_attempts": 1,
            "workers": 4,
            "images_persisted": False,
            "started_at": "2026-08-05T00:00:00Z",
            "completed_at": "2026-08-05T00:01:00Z",
            "metrics": {
                "candidate_count": 560,
                "success_count": 560,
                "failure_count": 0,
                "pending_count": 0,
                "attempt_count": 560,
                "successful_probe_attempts": 560,
                "http_2xx_attempts": 560,
                "failed_attempts": 0,
                "downloaded_bytes": 560000,
                "errors_by_kind": {},
            },
            "logical_sha256": probe_logical,
        },
        "exact_pixel_duplicate_groups": exact,
        "phash_duplicate_pairs_le_8": pairs,
        "phash_audit_pairs_9_16": audit,
        "probe_attempts": attempts,
    }
    payload["manifest_sha256"] = candidate_contract.manifest_sha256(payload)
    return payload


def _boundary_labels(label: str) -> list[str]:
    other = candidate_contract.CLASSES[
        (candidate_contract.CLASSES.index(label) + 1) % len(candidate_contract.CLASSES)
    ]
    return sorted([label, other], key=candidate_contract.CLASSES.index)


def _reviewed_pool(manifest: dict) -> dict:
    positions: Counter[tuple[str, str]] = Counter()
    decisions = []
    for candidate in manifest["candidates"]:
        key = (candidate["discovery_class"], candidate["generation_group"])
        positions[key] += 1
        position = positions[key]
        generation = candidate["generation_group"]
        clear_quota = candidate_contract.FINAL_CELL_QUOTAS[(generation, "clear")]
        boundary_quota = candidate_contract.FINAL_CELL_QUOTAS[(generation, "boundary")]
        if position <= clear_quota:
            disposition = "include"
            label = candidate["discovery_class"]
            clarity = "clear"
            acceptable = [label]
        elif position <= clear_quota + boundary_quota:
            disposition = "include"
            label = candidate["discovery_class"]
            clarity = "boundary"
            acceptable = _boundary_labels(label)
        else:
            disposition = "exclude"
            label = None
            clarity = None
            acceptable = []
        decision = {
            "candidate_id": candidate["candidate_id"],
            "asset_key": candidate["asset_key"],
            "article_id": candidate["article_id"],
            "building_id": candidate["building_id"],
            "request_url": candidate["request_url"],
            "review_url": candidate["review_url"],
            "generation_group": candidate["generation_group"],
            "url_generation": candidate["url_generation"],
            "content_sha256": candidate["content_sha256"],
            "pixel_sha256": candidate["pixel_sha256"],
            "phash_256": candidate["phash_256"],
            "disposition": disposition,
            "gold_label": label,
            "clarity": clarity,
            "acceptable_labels": acceptable,
            "high_res_viewed": False,
            "notes": "review note" if disposition == "include" else "",
            "reviewed_at": "2026-08-05T01:00:00+00:00",
        }
        if candidate["duplicate_of"] is not None:
            decision["duplicate_of"] = candidate["duplicate_of"]
        decisions.append(decision)
    included = sum(row["disposition"] == "include" for row in decisions)
    payload = {
        "manifest_version": candidate_contract.REVIEWED_POOL_VERSION,
        "candidate_manifest_version": manifest["manifest_version"],
        "candidate_manifest_sha256": manifest["manifest_sha256"],
        "source_db_sha256": manifest["source_db_sha256"],
        "contract": copy.deepcopy(manifest["contract"]),
        "reviewer": "reviewer-a",
        "exported_at": "2026-08-05T01:10:00+00:00",
        "total_candidates": 560,
        "decided_count": 560,
        "included_count": included,
        "excluded_count": 560 - included,
        "complete": True,
        "decisions": decisions,
    }
    payload["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(payload)
    return payload


def _refresh_duplicate_evidence(manifest: dict, review: dict) -> None:
    successful = [
        candidate
        for candidate in manifest["candidates"]
        if candidate["probe_status"] == "success"
    ]
    exact, pairs, audit, duplicate_status, matches = finalizer._expected_duplicate_evidence(
        successful
    )
    manifest["exact_pixel_duplicate_groups"] = exact
    manifest["phash_duplicate_pairs_le_8"] = pairs
    manifest["phash_audit_pairs_9_16"] = audit
    for candidate, decision in zip(manifest["candidates"], review["decisions"]):
        group, duplicate_of = duplicate_status.get(candidate["candidate_id"], (None, None))
        candidate["exact_duplicate_group"] = group
        candidate["is_exact_pixel_duplicate"] = group is not None
        candidate["duplicate_of"] = duplicate_of
        candidate["auto_exclude_exact_duplicate"] = duplicate_of is not None
        candidate["phash_le8_matches"] = matches.get(candidate["candidate_id"], [])
        candidate["has_phash_le8_candidate"] = bool(
            matches.get(candidate["candidate_id"], [])
        )
        for field in ("content_sha256", "pixel_sha256", "phash_256"):
            decision[field] = candidate[field]
        if duplicate_of is None:
            decision.pop("duplicate_of", None)
        else:
            decision["duplicate_of"] = duplicate_of
    reconstructed_input = copy.deepcopy(manifest)
    reconstructed_input.pop("manifest_sha256", None)
    for field in (
        "probe_contract",
        "exact_pixel_duplicate_groups",
        "phash_duplicate_pairs_le_8",
        "phash_audit_pairs_9_16",
        "probe_attempts",
    ):
        reconstructed_input.pop(field, None)
    for candidate in reconstructed_input["candidates"]:
        for field in finalizer.HASH_EVIDENCE_FIELDS:
            candidate.pop(field, None)
    reconstructed_input["manifest_sha256"] = manifest["probe_contract"][
        "input_manifest_sha256"
    ]
    config = probe_contract.ProbeConfig(
        workers=manifest["probe_contract"]["workers"],
        max_bytes=manifest["probe_contract"]["max_bytes"],
        connect_timeout=manifest["probe_contract"]["connect_timeout"],
        read_timeout=manifest["probe_contract"]["read_timeout"],
        max_attempts=manifest["probe_contract"]["max_attempts"],
    )
    manifest["probe_contract"]["logical_sha256"] = probe_contract._logical_sha256(
        manifest=reconstructed_input,
        manifest_file_sha=manifest["probe_contract"]["input_manifest_file_sha256"],
        config=config,
        runtime_versions=manifest["probe_contract"]["runtime_versions"],
        results=finalizer._reconstructed_probe_results(manifest["candidates"]),
        exact_groups=exact,
        duplicate_pairs=pairs,
        audit_pairs=audit,
    )
    manifest["manifest_sha256"] = candidate_contract.manifest_sha256(manifest)
    review["candidate_manifest_sha256"] = manifest["manifest_sha256"]
    review["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(review)


def _mark_failed_candidate(manifest: dict, review: dict, index: int = 72) -> None:
    candidate = manifest["candidates"][index]
    candidate.update(
        {
            "probe_status": "failed",
            "probe_final_url": None,
            "http_status": 404,
            "response_mime": None,
            "response_bytes": None,
            "content_sha256": None,
            "original_format": None,
            "original_mode": None,
            "original_width": None,
            "original_height": None,
            "frame_count": None,
            "exif_orientation": None,
            "orientation_applied": None,
            "oriented_width": None,
            "oriented_height": None,
            "alpha_composited": None,
            "icc_profile_present": None,
            "color_normalization": None,
            "normalized_width": None,
            "normalized_height": None,
            "pixel_sha256": None,
            "phash_256": None,
            "probe_elapsed_ms": 12,
            "probe_error_kind": "http_404",
            "probe_error_message": "HTTP 404",
        }
    )
    attempt = manifest["probe_attempts"][index]
    attempt.update(
        {
            "elapsed_ms": 12,
            "outcome": "failed",
            "final_url": None,
            "http_status": 404,
            "response_mime": None,
            "response_bytes": None,
            "content_sha256": None,
            "error_kind": "http_404",
            "error_message": "HTTP 404",
        }
    )
    manifest["probe_contract"]["metrics"].update(
        {
            "success_count": 559,
            "failure_count": 1,
            "successful_probe_attempts": 559,
            "http_2xx_attempts": 559,
            "failed_attempts": 1,
            "downloaded_bytes": 559000,
            "errors_by_kind": {"http_404": 1},
        }
    )
    decision = review["decisions"][index]
    if decision["disposition"] == "include":
        decision.update(
            {
                "disposition": "exclude",
                "gold_label": None,
                "clarity": None,
                "acceptable_labels": [],
                "notes": "",
            }
        )
        review["included_count"] -= 1
        review["excluded_count"] += 1
    _refresh_duplicate_evidence(manifest, review)
    for field in ("content_sha256", "pixel_sha256", "phash_256"):
        decision.pop(field, None)
    review["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(review)


def _build(manifest: dict, review: dict) -> dict:
    return finalizer.build_gold_manifest(
        candidate_manifest=manifest,
        reviewed_pool=review,
        candidate_manifest_file_sha256="e" * 64,
        reviewed_pool_file_sha256="f" * 64,
    )


def test_builds_deterministic_balanced_gold_without_discovery_hints() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    first = _build(manifest, review)
    second = _build(manifest, review)

    assert first == second
    assert len(first["samples"]) == 100
    assert first["gold_manifest_sha256"] == finalizer.gold_manifest_sha256(first)
    assert first["logical_sha256"] == finalizer.gold_logical_sha256(first)
    finalizer.validate_gold_manifest(first)
    counts = Counter(
        (
            row["human_review"]["gold_label"],
            row["source_identity"]["generation_group"],
            row["human_review"]["clarity"],
        )
        for row in first["samples"]
    )
    assert counts == Counter(finalizer.CELL_QUOTAS)
    assert not any(
        finalizer.DISCOVERY_HINT_FIELDS.intersection(row["source_identity"])
        for row in first["samples"]
    )
    assert all(row["human_review"]["notes"] == "review note" for row in first["samples"])


def test_logical_sha_ignores_only_input_file_serialization_hashes() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    first = _build(manifest, review)
    second = finalizer.build_gold_manifest(
        candidate_manifest=manifest,
        reviewed_pool=review,
        candidate_manifest_file_sha256="1" * 64,
        reviewed_pool_file_sha256="2" * 64,
    )
    assert first["logical_sha256"] == second["logical_sha256"]
    assert first["gold_manifest_sha256"] != second["gold_manifest_sha256"]


def test_rejects_review_sha_identity_and_completeness_tampering() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)

    changed = copy.deepcopy(review)
    changed["decisions"][0]["notes"] = "tampered"
    with pytest.raises(ValueError, match="reviewed pool SHA mismatch"):
        _build(manifest, changed)

    changed = copy.deepcopy(review)
    changed["decisions"][0]["asset_key"] = "other"
    changed["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(changed)
    with pytest.raises(ValueError, match="review identity mismatch"):
        _build(manifest, changed)

    changed = copy.deepcopy(review)
    changed["decisions"].pop()
    changed["decided_count"] = 559
    changed["complete"] = False
    changed["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(changed)
    with pytest.raises(ValueError, match="all 560 decisions"):
        _build(manifest, changed)


def test_rejects_inconsistent_failed_candidate_evidence() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    manifest["candidates"][0]["probe_status"] = "failed"
    manifest["manifest_sha256"] = candidate_contract.manifest_sha256(manifest)
    review["candidate_manifest_sha256"] = manifest["manifest_sha256"]
    review["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(review)
    with pytest.raises(ValueError, match="probe_error_kind"):
        _build(manifest, review)


def test_builds_with_complete_review_and_one_terminal_probe_failure() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    _mark_failed_candidate(manifest, review)

    candidates = finalizer.validate_enriched_candidate_manifest(manifest)
    output = _build(manifest, review)

    assert Counter(row["probe_status"] for row in candidates) == {
        "success": 559,
        "failed": 1,
    }
    assert len(review["decisions"]) == 560
    assert len(output["samples"]) == 100
    assert all(
        row["source_identity"]["candidate_id"] != "candidate-0073"
        for row in output["samples"]
    )
    assert all(row["image_evidence"]["probe_status"] == "success" for row in output["samples"])


def test_failed_probe_candidate_must_be_reviewed_as_excluded() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    _mark_failed_candidate(manifest, review)
    decision = review["decisions"][72]
    decision.update(
        {
            "disposition": "include",
            "gold_label": "exterior",
            "clarity": "clear",
            "acceptable_labels": ["exterior"],
            "notes": "incorrect include",
        }
    )
    review["included_count"] += 1
    review["excluded_count"] -= 1
    review["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(review)

    with pytest.raises(ValueError, match="failed probe candidate must be excluded"):
        _build(manifest, review)


def test_failed_probe_candidate_rejects_hash_or_successful_terminal_attempt() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    _mark_failed_candidate(manifest, review)

    changed = copy.deepcopy(manifest)
    changed["candidates"][72]["content_sha256"] = "0" * 64
    changed["manifest_sha256"] = candidate_contract.manifest_sha256(changed)
    with pytest.raises(ValueError, match="failed candidate carries image evidence"):
        finalizer.validate_enriched_candidate_manifest(changed)

    changed = copy.deepcopy(manifest)
    changed["probe_attempts"][72].update(
        {"outcome": "success", "error_kind": None, "error_message": None}
    )
    changed["manifest_sha256"] = candidate_contract.manifest_sha256(changed)
    with pytest.raises(ValueError, match="failed candidate has invalid retry history"):
        finalizer.validate_enriched_candidate_manifest(changed)


def test_quota_shortfall_names_the_actionable_cell() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    first = review["decisions"][0]
    first.update(
        {
            "disposition": "exclude",
            "gold_label": None,
            "clarity": None,
            "acceptable_labels": [],
            "notes": "",
        }
    )
    review["included_count"] -= 1
    review["excluded_count"] += 1
    review["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(review)
    with pytest.raises(finalizer.GoldQuotaError) as raised:
        _build(manifest, review)
    assert raised.value.shortfalls["exterior/modern/clear"] == {
        "required": 13,
        "eligible": 12,
        "selected": 0,
    }


def test_exact_duplicate_uses_representative_and_deterministic_spare() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    representative = manifest["candidates"][0]
    duplicate = manifest["candidates"][1]
    spare = manifest["candidates"][16]
    duplicate["pixel_sha256"] = representative["pixel_sha256"]
    duplicate["phash_256"] = representative["phash_256"]
    spare_decision = review["decisions"][16]
    spare_decision.update(
        {
            "disposition": "include",
            "gold_label": "exterior",
            "clarity": "clear",
            "acceptable_labels": ["exterior"],
            "notes": "review note",
        }
    )
    review["included_count"] += 1
    review["excluded_count"] -= 1
    _refresh_duplicate_evidence(manifest, review)

    output = _build(manifest, review)
    selected = {
        row["source_identity"]["candidate_id"] for row in output["samples"]
    }
    assert representative["candidate_id"] in selected
    assert duplicate["candidate_id"] not in selected
    assert spare["candidate_id"] in selected


def test_gold_validator_recomputes_selected_phash_safety() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    output = _build(manifest, review)
    changed = copy.deepcopy(output)
    changed["samples"][1]["image_evidence"]["phash_256"] = changed["samples"][0][
        "image_evidence"
    ]["phash_256"]
    changed["logical_sha256"] = finalizer.gold_logical_sha256(changed)
    changed["gold_manifest_sha256"] = finalizer.gold_manifest_sha256(changed)
    with pytest.raises(ValueError, match="pHash duplicate"):
        finalizer.validate_gold_manifest(changed)


def test_file_finalizer_is_strict_no_clobber(tmp_path: Path) -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    candidate_path = tmp_path / "candidates.json"
    review_path = tmp_path / "review.json"
    output = tmp_path / "gold.json"
    candidate_path.write_bytes(finalizer.canonical_json_bytes(manifest) + b"\n")
    review_path.write_bytes(finalizer.canonical_json_bytes(review) + b"\n")
    payload = finalizer.finalize_gold_files(
        candidate_manifest_path=candidate_path,
        reviewed_pool_path=review_path,
        output_path=output,
    )
    finalizer.validate_gold_manifest(payload)
    before = output.read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        finalizer.finalize_gold_files(
            candidate_manifest_path=candidate_path,
            reviewed_pool_path=review_path,
            output_path=output,
        )
    assert output.read_bytes() == before


def test_rejects_probe_logical_sha_tampering_even_with_valid_manifest_sha() -> None:
    manifest = _enriched_manifest()
    review = _reviewed_pool(manifest)
    manifest["probe_contract"]["logical_sha256"] = "0" * 64
    manifest["manifest_sha256"] = candidate_contract.manifest_sha256(manifest)
    review["candidate_manifest_sha256"] = manifest["manifest_sha256"]
    review["reviewed_pool_sha256"] = finalizer.reviewed_pool_sha256(review)
    with pytest.raises(ValueError, match="probe logical SHA mismatch"):
        _build(manifest, review)
