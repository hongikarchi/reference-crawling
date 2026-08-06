"""Probe fresh Divisare axis-holdout candidates without persisting images.

The mature Vision probe owns fetch retries, in-memory decoding, 512-pixel
identity hashing, and crash-safe staging.  This module binds that engine to the
fresh holdout manifest and adds duplicate checks against the earlier 560-image
candidate pool.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from canonical.divisare_image_smoke import (
    FetchPayload,
    canonical_json,
    file_sha256,
    network_fetch,
    utc_now,
)
from canonical import divisare_vision_axes_holdout as holdout
from canonical import divisare_vision_probe as base_probe
from canonical.divisare_vision_gold_finalize import parse_json_strict


HOLDOUT_PROBE_VERSION = "divisare-vision-axes-holdout-probe-v1.0.0"
EXPECTED_HOLDOUT_COUNT = 100
EXPECTED_PRIOR_COUNT = holdout.EXPECTED_EXCLUSION_COUNT


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = parse_json_strict(raw, label=label)
    if not isinstance(payload, dict):
        raise ValueError("%s must be a JSON object" % label)
    return payload, _sha256_bytes(raw)


def _validate_prior_probe_contract(prior: Mapping[str, Any]) -> None:
    contract = prior.get("probe_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("prior probed manifest is missing probe_contract")
    expected = {
        "probe_version": base_probe.PROBE_VERSION,
        "identity_profile": base_probe.IDENTITY_PROFILE,
        "pixel_hash_version": base_probe.PIXEL_HASH_VERSION,
        "phash_version": base_probe.PHASH_VERSION,
        "source_request_profile": base_probe.SOURCE_PROFILE,
        "normalized_long_edge": base_probe.NORMALIZED_LONG_EDGE,
        "runtime_versions": base_probe.probe_runtime_versions(),
        "images_persisted": False,
    }
    for field, value in expected.items():
        if contract.get(field) != value:
            raise ValueError("prior probe contract is incompatible: %s" % field)


def load_probe_inputs(
    candidate_manifest_path: Path,
    prior_probed_manifest_path: Path,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    str,
    dict[str, Any],
    list[dict[str, Any]],
    str,
    holdout.ExclusionEvidence,
]:
    """Load and cross-bind the fresh N100 and prior probed N560 manifests."""

    candidate_manifest_path = candidate_manifest_path.resolve()
    prior_probed_manifest_path = prior_probed_manifest_path.resolve()
    if candidate_manifest_path == prior_probed_manifest_path:
        raise ValueError("fresh and prior manifests must be different files")

    candidate, candidate_file_sha = _load_json(
        candidate_manifest_path, "holdout candidate manifest"
    )
    prior, prior_file_sha = _load_json(
        prior_probed_manifest_path, "prior probed candidate manifest"
    )
    _validated_prior, exclusion = holdout.load_exclusion_manifest(
        prior_probed_manifest_path
    )
    if _validated_prior != prior:
        raise RuntimeError("prior manifest changed while being validated")
    candidates = holdout.validate_candidate_manifest(candidate, exclusion=exclusion)
    if len(candidates) != EXPECTED_HOLDOUT_COUNT:
        raise ValueError("holdout probe requires exactly 100 candidates")
    provenance = candidate.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("holdout candidate provenance is required")
    if provenance.get("exclusion_manifest_file_sha256") != prior_file_sha:
        raise ValueError("holdout candidates bind a different prior manifest file")
    if provenance.get("exclusion_manifest_sha256") != prior.get("manifest_sha256"):
        raise ValueError("holdout candidates bind a different prior manifest")

    _validate_prior_probe_contract(prior)

    prior_rows = prior.get("candidates")
    if not isinstance(prior_rows, list) or len(prior_rows) != EXPECTED_PRIOR_COUNT:
        raise ValueError("prior probed manifest must contain exactly 560 candidates")
    terminal = [dict(row) for row in prior_rows]
    for row in terminal:
        status = row.get("probe_status")
        if status not in {"success", "failed"}:
            raise ValueError("prior candidate probe status must be terminal")
        if status == "success":
            for field in ("content_sha256", "pixel_sha256", "phash_256"):
                value = row.get(field)
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)
                ):
                    raise ValueError("prior successful candidate has invalid %s" % field)
    return (
        candidate,
        candidates,
        candidate_file_sha,
        prior,
        terminal,
        prior_file_sha,
        exclusion,
    )


def _candidate_validator(
    exclusion: holdout.ExclusionEvidence,
) -> Callable[[Mapping[str, Any]], Any]:
    return lambda payload: holdout.validate_candidate_manifest(
        payload, exclusion=exclusion
    )


def build_cross_duplicate_evidence(
    fresh_successful: Sequence[Mapping[str, Any]],
    prior_successful: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return exact encoded/pixel and pHash-near pairs across the two pools."""

    fresh = sorted(fresh_successful, key=lambda row: int(row["candidate_rank"]))
    prior = sorted(prior_successful, key=lambda row: int(row["candidate_rank"]))
    content_index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    pixel_index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in prior:
        content_index[str(row["content_sha256"])].append(row)
        pixel_index[str(row["pixel_sha256"])].append(row)

    content_matches: list[dict[str, Any]] = []
    pixel_matches: list[dict[str, Any]] = []
    phash_le8: list[dict[str, Any]] = []
    phash_9_16: list[dict[str, Any]] = []
    for left in fresh:
        left_id = str(left["candidate_id"])
        for right in content_index.get(str(left["content_sha256"]), ()):
            content_matches.append(
                {
                    "holdout_candidate_id": left_id,
                    "prior_candidate_id": str(right["candidate_id"]),
                    "content_sha256": str(left["content_sha256"]),
                }
            )
        for right in pixel_index.get(str(left["pixel_sha256"]), ()):
            pixel_matches.append(
                {
                    "holdout_candidate_id": left_id,
                    "prior_candidate_id": str(right["candidate_id"]),
                    "pixel_sha256": str(left["pixel_sha256"]),
                }
            )
        for right in prior:
            distance = base_probe.phash_distance(
                str(left["phash_256"]), str(right["phash_256"])
            )
            if distance > 16:
                continue
            pair = {
                "holdout_candidate_id": left_id,
                "prior_candidate_id": str(right["candidate_id"]),
                "phash_distance": distance,
                "exact_pixel_duplicate": (
                    left["pixel_sha256"] == right["pixel_sha256"]
                ),
            }
            (phash_le8 if distance <= 8 else phash_9_16).append(pair)

    pair_order = lambda row: (  # noqa: E731
        row.get("phash_distance", -1),
        row["holdout_candidate_id"],
        row["prior_candidate_id"],
    )
    content_matches.sort(key=pair_order)
    pixel_matches.sort(key=pair_order)
    phash_le8.sort(key=pair_order)
    phash_9_16.sort(key=pair_order)
    return content_matches, pixel_matches, phash_le8, phash_9_16


def _attach_cross_duplicate_evidence(
    payload: Mapping[str, Any],
    *,
    prior_manifest: Mapping[str, Any],
    prior_manifest_file_sha256: str,
) -> dict[str, Any]:
    output = copy.deepcopy(dict(payload))
    output.pop("manifest_sha256", None)
    fresh_successful = [
        row for row in output["candidates"] if row.get("probe_status") == "success"
    ]
    prior_successful = [
        row
        for row in prior_manifest["candidates"]
        if row.get("probe_status") == "success"
    ]
    content, pixel, phash_le8, phash_9_16 = build_cross_duplicate_evidence(
        fresh_successful, prior_successful
    )

    by_id: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"content": [], "pixel": [], "le8": [], "audit": []}
    )
    for row in content:
        by_id[row["holdout_candidate_id"]]["content"].append(
            {
                "prior_candidate_id": row["prior_candidate_id"],
                "content_sha256": row["content_sha256"],
            }
        )
    for row in pixel:
        by_id[row["holdout_candidate_id"]]["pixel"].append(
            {
                "prior_candidate_id": row["prior_candidate_id"],
                "pixel_sha256": row["pixel_sha256"],
            }
        )
    for rows, key in ((phash_le8, "le8"), (phash_9_16, "audit")):
        for row in rows:
            by_id[row["holdout_candidate_id"]][key].append(
                {
                    "prior_candidate_id": row["prior_candidate_id"],
                    "distance": row["phash_distance"],
                }
            )
    for candidate in output["candidates"]:
        matches = by_id[str(candidate["candidate_id"])]
        candidate.update(
            {
                "prior_content_sha_matches": matches["content"],
                "prior_pixel_sha_matches": matches["pixel"],
                "prior_phash_le8_matches": matches["le8"],
                "prior_phash_9_16_matches": matches["audit"],
                "has_prior_exact_pixel_match": bool(matches["pixel"]),
                "has_prior_phash_le8_match": bool(matches["le8"]),
                "has_prior_phash_9_16_match": bool(matches["audit"]),
            }
        )

    base_logical = output["probe_contract"]["logical_sha256"]
    logical_value = {
        "holdout_probe_version": HOLDOUT_PROBE_VERSION,
        "base_probe_logical_sha256": base_logical,
        "prior_manifest_file_sha256": prior_manifest_file_sha256,
        "prior_manifest_sha256": prior_manifest["manifest_sha256"],
        "prior_success_count": len(prior_successful),
        "content_matches": content,
        "pixel_matches": pixel,
        "phash_le8": phash_le8,
        "phash_9_16": phash_9_16,
    }
    output["holdout_cross_duplicate_contract"] = {
        "holdout_probe_version": HOLDOUT_PROBE_VERSION,
        "prior_manifest_file_sha256": prior_manifest_file_sha256,
        "prior_manifest_sha256": prior_manifest["manifest_sha256"],
        "prior_candidate_count": len(prior_manifest["candidates"]),
        "prior_success_count": len(prior_successful),
        "phash_auto_duplicate_max_distance": 8,
        "phash_audit_max_distance": 16,
        "cross_content_match_count": len(content),
        "cross_pixel_match_count": len(pixel),
        "cross_phash_le8_pair_count": len(phash_le8),
        "cross_phash_9_16_pair_count": len(phash_9_16),
        "logical_sha256": hashlib.sha256(
            canonical_json(logical_value).encode("utf-8")
        ).hexdigest(),
    }
    output["prior_content_sha_matches"] = content
    output["prior_pixel_sha_matches"] = pixel
    output["prior_phash_pairs_le_8"] = phash_le8
    output["prior_phash_pairs_9_16"] = phash_9_16
    output["manifest_sha256"] = holdout.manifest_sha256(output)
    return output


def validate_probed_holdout_manifest(
    payload: Mapping[str, Any],
    *,
    input_manifest: Mapping[str, Any],
    input_manifest_file_sha256: str,
    prior_manifest: Mapping[str, Any],
    prior_manifest_file_sha256: str,
    exclusion: holdout.ExclusionEvidence,
) -> None:
    """Recompute base and cross-pool evidence for an immutable probe output."""

    base_probe.validate_enriched_manifest(
        payload,
        input_manifest=input_manifest,
        input_manifest_file_sha256=input_manifest_file_sha256,
        candidate_validator=_candidate_validator(exclusion),
    )
    # Candidate cross flags are also derived fields and must be removed before
    # recomputing them from the underlying hashes.
    derived_fields = {
        "prior_content_sha_matches",
        "prior_pixel_sha_matches",
        "prior_phash_le8_matches",
        "prior_phash_9_16_matches",
        "has_prior_exact_pixel_match",
        "has_prior_phash_le8_match",
        "has_prior_phash_9_16_match",
    }
    clean = copy.deepcopy(dict(payload))
    for candidate in clean["candidates"]:
        for field in derived_fields:
            candidate.pop(field, None)
    clean.pop("holdout_cross_duplicate_contract", None)
    clean.pop("prior_content_sha_matches", None)
    clean.pop("prior_pixel_sha_matches", None)
    clean.pop("prior_phash_pairs_le_8", None)
    clean.pop("prior_phash_pairs_9_16", None)
    clean.pop("manifest_sha256", None)
    recomputed = _attach_cross_duplicate_evidence(
        clean,
        prior_manifest=prior_manifest,
        prior_manifest_file_sha256=prior_manifest_file_sha256,
    )
    if dict(payload) != recomputed:
        raise ValueError("holdout cross-duplicate evidence is incomplete or changed")


def run_holdout_probe(
    *,
    candidate_manifest_path: Path,
    prior_probed_manifest_path: Path,
    output_path: Path,
    staging_path: Path | None = None,
    config: base_probe.ProbeConfig = base_probe.ProbeConfig(),
    resume: bool = False,
    stop_after: int | None = None,
    fetcher: Callable[..., FetchPayload] = network_fetch,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Probe fresh candidates and publish an immutable hash-only manifest."""

    config.validate()
    if stop_after is not None and (
        isinstance(stop_after, bool) or not isinstance(stop_after, int) or stop_after < 1
    ):
        raise ValueError("stop_after must be a positive integer")
    candidate_manifest_path = candidate_manifest_path.resolve()
    prior_probed_manifest_path = prior_probed_manifest_path.resolve()
    output_path = output_path.resolve()
    staging_path = (
        staging_path.resolve()
        if staging_path is not None
        else output_path.with_name(output_path.name + ".staging.sqlite")
    )
    if len(
        {
            candidate_manifest_path,
            prior_probed_manifest_path,
            output_path,
            staging_path,
        }
    ) != 4:
        raise ValueError("candidate, prior, output, and staging paths must differ")
    if output_path.exists():
        raise FileExistsError("immutable output already exists: %s" % output_path)

    (
        manifest,
        candidates,
        manifest_file_sha,
        prior_manifest,
        _prior_rows,
        prior_file_sha,
        exclusion,
    ) = load_probe_inputs(candidate_manifest_path, prior_probed_manifest_path)
    if staging_path.exists() and not resume:
        raise FileExistsError("staging artifact exists; pass --resume: %s" % staging_path)
    if not staging_path.exists() and resume:
        raise FileNotFoundError("cannot resume missing staging artifact: %s" % staging_path)
    if not staging_path.exists():
        base_probe._create_staging(
            staging_path,
            manifest_path=candidate_manifest_path,
            manifest_file_sha=manifest_file_sha,
            manifest=manifest,
            candidates=candidates,
            config=config,
        )

    conn = sqlite3.connect(staging_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    executor: ThreadPoolExecutor | None = None
    try:
        base_probe._verify_staging(
            conn,
            manifest_path=candidate_manifest_path,
            manifest_file_sha=manifest_file_sha,
            manifest=manifest,
            candidates=candidates,
            config=config,
        )
        pending_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT candidate_id FROM candidate_results WHERE status='pending'"
            )
        }
        pending = [row for row in candidates if str(row["candidate_id"]) in pending_ids]
        selected = pending if stop_after is None else pending[:stop_after]
        executor = ThreadPoolExecutor(
            max_workers=config.workers,
            thread_name_prefix="divisare-holdout-probe",
        )
        futures: dict[Future[dict[str, Any]], str] = {
            executor.submit(
                base_probe._probe_candidate,
                row,
                config=config,
                fetcher=fetcher,
                sleep=sleep,
            ): str(row["candidate_id"])
            for row in selected
        }
        for future in as_completed(futures):
            base_probe._write_probe_result(conn, future.result())
        executor.shutdown(wait=True, cancel_futures=True)
        executor = None

        if file_sha256(candidate_manifest_path) != manifest_file_sha:
            raise RuntimeError("holdout candidate manifest changed during probe")
        if file_sha256(prior_probed_manifest_path) != prior_file_sha:
            raise RuntimeError("prior probed manifest changed during probe")
        pending_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM candidate_results WHERE status='pending'"
            ).fetchone()[0]
        )
        if stop_after is not None:
            conn.execute(
                "UPDATE probe_run SET status='running',updated_at=?,error=NULL WHERE run_id=1",
                (utc_now(),),
            )
            conn.commit()
            return {
                "status": "running",
                "final_output_written": False,
                "staging_path": str(staging_path),
                "processed_this_invocation": len(selected),
                "pending_count": pending_count,
            }
        if pending_count:
            raise RuntimeError("holdout probe has %d pending candidates" % pending_count)

        failure_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM candidate_results WHERE status='failed'"
            ).fetchone()[0]
        )
        completed = utc_now()
        status = "complete_with_failures" if failure_count else "complete"
        conn.execute(
            "UPDATE probe_run SET status=?,updated_at=?,completed_at=?,error=NULL WHERE run_id=1",
            (status, completed, completed),
        )
        conn.commit()
        payload = base_probe._build_enriched_manifest(
            conn,
            manifest=manifest,
            manifest_file_sha=manifest_file_sha,
            config=config,
            candidate_validator=_candidate_validator(exclusion),
        )
        payload = _attach_cross_duplicate_evidence(
            payload,
            prior_manifest=prior_manifest,
            prior_manifest_file_sha256=prior_file_sha,
        )
        validate_probed_holdout_manifest(
            payload,
            input_manifest=manifest,
            input_manifest_file_sha256=manifest_file_sha,
            prior_manifest=prior_manifest,
            prior_manifest_file_sha256=prior_file_sha,
            exclusion=exclusion,
        )
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("holdout staging SQLite quick_check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("holdout staging SQLite foreign-key check failed")
    except BaseException as exc:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        try:
            conn.execute(
                "UPDATE probe_run SET status='interrupted',updated_at=?,error=? WHERE run_id=1",
                (utc_now(), str(exc)[:2000]),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    base_probe._write_json_no_clobber(output_path, payload)
    staging_path.unlink()
    contract = payload["holdout_cross_duplicate_contract"]
    metrics = payload["probe_contract"]["metrics"]
    return {
        "output_path": str(output_path),
        "output_file_sha256": file_sha256(output_path),
        "manifest_sha256": payload["manifest_sha256"],
        "base_probe_logical_sha256": payload["probe_contract"]["logical_sha256"],
        "cross_logical_sha256": contract["logical_sha256"],
        **metrics,
        "within_exact_group_count": len(payload["exact_pixel_duplicate_groups"]),
        "within_phash_le8_pair_count": len(payload["phash_duplicate_pairs_le_8"]),
        "within_phash_9_16_pair_count": len(payload["phash_audit_pairs_9_16"]),
        "cross_content_match_count": contract["cross_content_match_count"],
        "cross_pixel_match_count": contract["cross_pixel_match_count"],
        "cross_phash_le8_pair_count": contract["cross_phash_le8_pair_count"],
        "cross_phash_9_16_pair_count": contract["cross_phash_9_16_pair_count"],
    }
