#!/usr/bin/env python3
"""Create independent blind references and inspect reconciled N10 v1.2 QA.

``template`` and ``freeze`` never open SQLite.  Each reviewer must freeze the
entire pixel-only population before seeing either the peer ledger or model
labels.  ``inspect`` accepts only a separately frozen, fully reconciled ledger
and authenticates it before opening the immutable semantic sidecar.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.cross_source_image_selection import canonical_json, canonical_sha256  # noqa: E402
from canonical.cross_source_semantic_vision_v12 import (  # noqa: E402
    CAMERA_ANGLES,
    DRAWING_KINDS,
    FRAMING_SCALES,
    MEDIA,
    PROJECT_LEGIBILITY,
    PROJECT_STATES,
    REJECT_REASONS,
    SPATIAL_CONTEXTS,
    UNCERTAIN_AXES,
    derive_hero_decision,
    validate_invariants,
)
from tools.render_cross_source_semantic_review_sheets_n10_v12 import (  # noqa: E402
    MANIFEST_DOMAIN as REVIEW_MANIFEST_DOMAIN,
    OPAQUE_ID,
    ORDER_DOMAIN as REVIEW_ORDER_DOMAIN,
    PRODUCTION_PINS,
    RENDERER_VERSION,
    ReviewPins,
    _expected_run_pins,
    _file_sha,
    _open_immutable,
    _require_pins,
    _sidecars,
)


INSPECTOR_VERSION = "cross-source-semantic-vision-n10-v1.2-inspector-v1.0.0"
INDEPENDENT_LEDGER_VERSION = (
    "cross-source-semantic-vision-n10-v1.2-independent-agent-reference-v1.0.0"
)
LEDGER_TEMPLATE_DOMAIN = "cross-source-semantic-vision-n10-v1.2-independent-ledger-template-v1"
INDEPENDENT_LEDGER_DOMAIN = "cross-source-semantic-vision-n10-v1.2-independent-frozen-ledger-v1"
INSPECTION_DOMAIN = "cross-source-semantic-vision-n10-v1.2-inspection-v1"

QA_FIELDS = (
    "in_scope",
    "reject_reason",
    "medium",
    "spatial_context",
    "framing_scale",
    "camera_angle",
    "drawing_kind",
    "project_state",
    "project_legibility",
)
FIELD_VALUES: dict[str, tuple[Any, ...]] = {
    "in_scope": (False, True),
    "reject_reason": tuple(REJECT_REASONS),
    "medium": tuple(MEDIA),
    "spatial_context": tuple(SPATIAL_CONTEXTS),
    "framing_scale": tuple(FRAMING_SCALES),
    "camera_angle": tuple(CAMERA_ANGLES),
    "drawing_kind": tuple(DRAWING_KINDS),
    "project_state": tuple(PROJECT_STATES),
    "project_legibility": tuple(PROJECT_LEGIBILITY),
}
USABLE_HERO_TIERS = frozenset({"preferred", "eligible"})
CRITICAL_FALSE_POSITIVE_MODEL_TIERS = USABLE_HERO_TIERS
CRITICAL_FALSE_POSITIVE_REFERENCE_TIERS = frozenset(
    {"qa_only", "archive_only", "rejected"}
)
REVIEW_ROW_KEYS = frozenset({
    "acceptable_values", "expected_uncertain_axes", "inference_id",
    "primary", "reason", "resolution_insufficient",
})
INDEPENDENT_DRAFT_KEYS = frozenset({
    "authoritative", "blind_review_completed_before_peer_exchange", "created_at",
    "field_vocabulary", "independence_contract", "ledger_version",
    "model_results_read", "peer_ledger_read", "reference_type",
    "review_manifest", "reviewer_id", "rows", "semantic_labels_read",
    "status", "template_sha256", "uncertainty_axes",
})
INDEPENDENT_FROZEN_KEYS = (
    INDEPENDENT_DRAFT_KEYS - {"template_sha256"}
) | {"completed_at", "ledger_sha256"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> tuple[dict[str, Any], bytes, str]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload, raw, hashlib.sha256(raw).hexdigest()


def _write_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(payload) + "\n")


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _validate_review_manifest(
    path: Path,
    *,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> tuple[dict[str, Any], str, str]:
    pins = _require_pins(pins)
    payload, raw, byte_sha = _load_json(path)
    stored = payload.get("manifest_sha256")
    body = copy.deepcopy(payload)
    body.pop("manifest_sha256", None)
    if not isinstance(stored, str) or stored != canonical_sha256(
        {"domain": REVIEW_MANIFEST_DOMAIN, "manifest": body}
    ):
        raise ValueError("review manifest self SHA mismatch")
    if payload.get("renderer_version") != RENDERER_VERSION:
        raise ValueError("review renderer version mismatch")
    if payload.get("review_seed") != pins.review_seed:
        raise ValueError("review seed mismatch")
    if payload.get("blind_labels_only") is not True:
        raise ValueError("review manifest is not source-blind")
    if payload.get("semantic_labels_exposed") is not False:
        raise ValueError("review manifest exposed semantic labels")
    if any(int(payload.get(name, -1)) != 0 for name in (
        "network_requests", "vision_requests", "llm_requests"
    )):
        raise ValueError("review renderer recorded an external request")

    ids = payload.get("ordered_inference_ids")
    inputs = payload.get("inputs")
    if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
        raise ValueError("ordered inference IDs are invalid")
    if len(ids) != pins.occurrence_count or len(set(ids)) != len(ids):
        raise ValueError(
            f"exactly {pins.occurrence_count} unique review IDs are required"
        )
    if any(OPAQUE_ID.fullmatch(value) is None for value in ids):
        raise ValueError("review IDs must use the semv12_ opaque namespace")
    if not isinstance(inputs, list) or len(inputs) != len(ids):
        raise ValueError("review input accounting mismatch")
    if [row.get("inference_id") for row in inputs if isinstance(row, dict)] != ids:
        raise ValueError("review input order differs from ordered IDs")
    if int(payload.get("input_count", -1)) != pins.occurrence_count:
        raise ValueError(
            f"review input_count must be exactly {pins.occurrence_count}"
        )
    order_sha = canonical_sha256(
        {"domain": REVIEW_ORDER_DOMAIN, "ordered_inference_ids": ids}
    )
    if payload.get("ordered_inference_ids_sha256") != order_sha:
        raise ValueError("review ordered-ID SHA mismatch")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("model_result_available"), bool)
        or not isinstance(row.get("vision_status"), str)
        or row.get("vision_status") in {"pending", "ready"}
        or (row.get("vision_status") == "success") != row.get("model_result_available")
        for row in inputs
    ):
        raise ValueError("review input result-availability/status contract mismatch")
    available_ids = [
        row["inference_id"] for row in inputs if row["model_result_available"]
    ]
    missing_ids = [
        row["inference_id"] for row in inputs if not row["model_result_available"]
    ]
    result_accounting = payload.get("model_result_accounting")
    status_counts = Counter(str(row["vision_status"]) for row in inputs)
    expected_accounting = {
        "available_count": len(available_ids),
        "available_inference_ids": available_ids,
        "missing_count": len(missing_ids),
        "missing_inference_ids": missing_ids,
        "semantic_result_row_count": len(available_ids),
        "successful_input_count": len(available_ids),
        "terminal_input_status_counts": dict(sorted(status_counts.items())),
    }
    if result_accounting != expected_accounting:
        raise ValueError("review manifest model-result accounting mismatch")

    selection = payload.get("selection_manifest")
    expected_selection = {
        "byte_sha256": pins.selection_manifest_byte_sha256,
        "manifest_domain": pins.selection_manifest_domain,
        "manifest_sha256": pins.selection_manifest_self_sha256,
        "ordered_building_manifest_sha256": pins.ordered_building_manifest_sha256,
        "ordered_e1_pixel_manifest_sha256": pins.ordered_e1_pixel_manifest_sha256,
        "ordered_fetch_url_manifest_sha256": pins.ordered_fetch_url_manifest_sha256,
        "ordered_occurrence_manifest_sha256": pins.ordered_occurrence_manifest_sha256,
        "size_bytes": pins.selection_manifest_size_bytes,
    }
    if not isinstance(selection, dict) or any(
        selection.get(key) != value for key, value in expected_selection.items()
    ):
        raise ValueError("review manifest is not bound to the exact production selection")
    run = payload.get("semantic_run")
    expected_run = _expected_run_pins(pins)
    if not isinstance(run, dict) or any(run.get(key) != value for key, value in expected_run.items()):
        raise ValueError("review manifest semantic-run pins differ from production")
    provenance = run.get("review_cache_provenance")
    if not isinstance(provenance, dict) or provenance.get("retained_for_review") is not True:
        raise ValueError("retained review-cache provenance is absent")
    if provenance.get("write_contract") != "atomic-exclusive-sha256-v1":
        raise ValueError("retained review-cache write contract mismatch")
    path_sha = provenance.get("path_sha256")
    if not isinstance(path_sha, str) or re_full_sha(path_sha) is False:
        raise ValueError("retained review-cache path provenance is invalid")
    semantic_db = payload.get("semantic_db")
    if not isinstance(semantic_db, dict):
        raise ValueError("review manifest lacks semantic DB identity")
    if semantic_db.get("byte_sha256_before") != semantic_db.get("byte_sha256_after"):
        raise ValueError("semantic DB changed during review rendering")
    if semantic_db.get("size_bytes_before") != semantic_db.get("size_bytes_after"):
        raise ValueError("semantic DB size changed during review rendering")
    validation = payload.get("validation")
    if not isinstance(validation, dict) or (
        validation.get("quick_check") != "ok"
        or validation.get("integrity_check") != "ok"
        or int(validation.get("foreign_key_violations", -1)) != 0
        or validation.get("sqlite_sidecars_before") != []
        or validation.get("sqlite_sidecars_after") != []
    ):
        raise ValueError("review-render validation is not clean")
    if not raw:
        raise ValueError("empty review manifest")
    return payload, byte_sha, stored


def re_full_sha(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _blank_review_row(inference_id: str) -> dict[str, Any]:
    return {
        "acceptable_values": {field: [] for field in QA_FIELDS},
        "expected_uncertain_axes": [],
        "inference_id": inference_id,
        "primary": {field: None for field in QA_FIELDS},
        "reason": "",
        "resolution_insufficient": None,
    }


def _manifest_binding(
    manifest: Mapping[str, Any], byte_sha: str, self_sha: str
) -> dict[str, Any]:
    return {
        "byte_sha256": byte_sha,
        "input_count": int(manifest["input_count"]),
        "manifest_sha256": self_sha,
        "ordered_inference_ids_sha256": manifest["ordered_inference_ids_sha256"],
        "semantic_db_byte_sha256": manifest["semantic_db"]["byte_sha256_after"],
        "semantic_run_id": manifest["semantic_run"]["run_id"],
    }


def create_independent_ledger_template(
    *,
    review_manifest_path: Path,
    output_path: Path,
    reviewer_id: str,
    created_at: str | None = None,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> dict[str, Any]:
    """Create one full-population pixel-only draft without opening SQLite."""

    if not reviewer_id.strip():
        raise ValueError("reviewer_id must be non-empty")
    manifest, byte_sha, self_sha = _validate_review_manifest(
        review_manifest_path.resolve(), pins=pins
    )
    ids = list(manifest["ordered_inference_ids"])
    payload: dict[str, Any] = {
        "authoritative": False,
        "blind_review_completed_before_peer_exchange": False,
        "created_at": created_at or _utc_now(),
        "field_vocabulary": {field: list(values) for field, values in FIELD_VALUES.items()},
        "independence_contract": "full-population-pixel-only-pre-exchange-v1",
        "ledger_version": INDEPENDENT_LEDGER_VERSION,
        "model_results_read": False,
        "peer_ledger_read": False,
        "reference_type": "independent_agent_reference",
        "review_manifest": _manifest_binding(manifest, byte_sha, self_sha),
        "reviewer_id": reviewer_id,
        "rows": [_blank_review_row(inference_id) for inference_id in ids],
        "semantic_labels_read": False,
        "status": "draft",
        "uncertainty_axes": list(UNCERTAIN_AXES),
    }
    payload["template_sha256"] = canonical_sha256(
        {"domain": LEDGER_TEMPLATE_DOMAIN, "template": payload}
    )
    _write_no_clobber(output_path, payload)
    return payload


def _ordered_unique(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if len(value) != len({json.dumps(item, sort_keys=True) for item in value}):
        raise ValueError(f"{label} contains duplicates")
    return value


def _validate_completed_row(row: Mapping[str, Any], expected_id: str) -> None:
    if set(row) != REVIEW_ROW_KEYS:
        raise ValueError(f"review row field set mismatch for {expected_id}")
    if row.get("inference_id") != expected_id:
        raise ValueError(f"review row ID mismatch for {expected_id}")
    primary = row.get("primary")
    acceptable = row.get("acceptable_values")
    if not isinstance(primary, dict) or set(primary) != set(QA_FIELDS):
        raise ValueError(f"primary field set mismatch for {expected_id}")
    if not isinstance(acceptable, dict) or set(acceptable) != set(QA_FIELDS):
        raise ValueError(f"acceptable field set mismatch for {expected_id}")
    for field in QA_FIELDS:
        value = primary[field]
        if field == "in_scope" and not isinstance(value, bool):
            raise ValueError(f"in_scope must be boolean for {expected_id}")
        if value not in FIELD_VALUES[field]:
            raise ValueError(f"unsupported {field} for {expected_id}: {value!r}")
        values = _ordered_unique(
            acceptable[field], label=f"{expected_id}.{field}.acceptable_values"
        )
        if not values or any(item not in FIELD_VALUES[field] for item in values):
            raise ValueError(f"invalid acceptable values for {expected_id}.{field}")
        if field == "in_scope" and any(not isinstance(item, bool) for item in values):
            raise ValueError(f"acceptable in_scope values must be boolean for {expected_id}")
        if value not in values:
            raise ValueError(f"primary must be acceptable for {expected_id}.{field}")
    axes = _ordered_unique(
        row.get("expected_uncertain_axes"),
        label=f"{expected_id}.expected_uncertain_axes",
    )
    if any(axis not in UNCERTAIN_AXES for axis in axes):
        raise ValueError(f"unsupported uncertainty axis for {expected_id}")
    resolution = row.get("resolution_insufficient")
    if not isinstance(resolution, bool):
        raise ValueError(f"resolution_insufficient must be boolean for {expected_id}")
    reason = row.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"pixel-grounded reason is required for {expected_id}")
    validate_invariants(
        {
            "asset_id": expected_id,
            **{field: primary[field] for field in QA_FIELDS},
            "uncertain_axes": tuple(axes),
            "resolution_insufficient": resolution,
            "evidence": reason.strip(),
        }
    )


def _canonical_review_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(row))
    result["acceptable_values"] = {
        field: [value for value in FIELD_VALUES[field] if value in row["acceptable_values"][field]]
        for field in QA_FIELDS
    }
    result["expected_uncertain_axes"] = [
        axis for axis in UNCERTAIN_AXES if axis in row["expected_uncertain_axes"]
    ]
    result["reason"] = str(row["reason"]).strip()
    return result


def _validate_binding(
    ledger: Mapping[str, Any],
    manifest: Mapping[str, Any],
    byte_sha: str,
    self_sha: str,
) -> list[str]:
    if ledger.get("review_manifest") != _manifest_binding(manifest, byte_sha, self_sha):
        raise ValueError("review ledger manifest binding mismatch")
    ids = list(manifest["ordered_inference_ids"])
    rows = ledger.get("rows")
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError("review ledger must contain the exact pinned population")
    if [row.get("inference_id") for row in rows if isinstance(row, dict)] != ids:
        raise ValueError("review ledger does not preserve blind order")
    return ids


def freeze_independent_ledger(
    *,
    review_manifest_path: Path,
    draft_path: Path,
    output_path: Path,
    completed_at: str | None = None,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> dict[str, Any]:
    """Freeze one independent reference while peer/model labels remain unread."""

    manifest, byte_sha, self_sha = _validate_review_manifest(
        review_manifest_path.resolve(), pins=pins
    )
    draft, _, _ = _load_json(draft_path.resolve())
    if set(draft) != INDEPENDENT_DRAFT_KEYS:
        raise ValueError("independent draft field set mismatch")
    if draft.get("status") != "draft":
        raise ValueError("only a draft ledger may be frozen")
    if draft.get("reference_type") != "independent_agent_reference":
        raise ValueError("v1.2 N10 requires an independent agent reference")
    if draft.get("ledger_version") != INDEPENDENT_LEDGER_VERSION:
        raise ValueError("independent ledger version mismatch")
    if draft.get("authoritative") is not False:
        raise ValueError("independent agent reference cannot be authoritative")
    if draft.get("field_vocabulary") != {
        field: list(values) for field, values in FIELD_VALUES.items()
    } or draft.get("uncertainty_axes") != list(UNCERTAIN_AXES):
        raise ValueError("independent ledger vocabulary contract mismatch")
    if draft.get("independence_contract") != "full-population-pixel-only-pre-exchange-v1":
        raise ValueError("independent ledger contract mismatch")
    if draft.get("semantic_labels_read") is not False or draft.get("model_results_read") is not False:
        raise ValueError("model/semantic labels must remain unread before freeze")
    if draft.get("peer_ledger_read") is not False:
        raise ValueError("peer reference must remain unread before independent freeze")
    if draft.get("blind_review_completed_before_peer_exchange") is not True:
        raise ValueError("pre-exchange blind completion attestation is required")
    reviewer_id = draft.get("reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise ValueError("reviewer_id must be non-empty")
    ids = _validate_binding(draft, manifest, byte_sha, self_sha)
    for expected_id, row in zip(ids, draft["rows"]):
        if not isinstance(row, dict):
            raise ValueError(f"review row must be an object: {expected_id}")
        _validate_completed_row(row, expected_id)
    frozen = copy.deepcopy(draft)
    frozen.pop("template_sha256", None)
    frozen["rows"] = [_canonical_review_row(row) for row in draft["rows"]]
    frozen["completed_at"] = completed_at or _utc_now()
    frozen["status"] = "frozen"
    frozen["ledger_sha256"] = canonical_sha256(
        {"domain": INDEPENDENT_LEDGER_DOMAIN, "ledger": frozen}
    )
    _write_no_clobber(output_path, frozen)
    return frozen


def validate_independent_frozen_ledger(
    *,
    review_manifest_path: Path,
    ledger_path: Path,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> tuple[dict[str, Any], dict[str, Any], str, str, str]:
    """Authenticate an independent frozen ledger without opening SQLite."""

    manifest, byte_sha, self_sha = _validate_review_manifest(
        review_manifest_path.resolve(), pins=pins
    )
    ledger, raw, ledger_byte_sha = _load_json(ledger_path.resolve())
    if raw != (canonical_json(ledger) + "\n").encode("utf-8"):
        raise ValueError("independent frozen ledger is not canonical JSON plus LF")
    if ledger.get("status") != "frozen":
        raise ValueError("independent reference is not frozen")
    if ledger.get("reference_type") != "independent_agent_reference":
        raise ValueError("reference is not an independent agent ledger")
    if ledger.get("ledger_version") != INDEPENDENT_LEDGER_VERSION:
        raise ValueError("independent ledger version mismatch")
    if set(ledger) != INDEPENDENT_FROZEN_KEYS:
        raise ValueError("independent frozen ledger field set mismatch")
    if ledger.get("authoritative") is not False:
        raise ValueError("independent agent reference cannot be authoritative")
    if ledger.get("field_vocabulary") != {
        field: list(values) for field, values in FIELD_VALUES.items()
    } or ledger.get("uncertainty_axes") != list(UNCERTAIN_AXES):
        raise ValueError("independent ledger vocabulary contract mismatch")
    if ledger.get("blind_review_completed_before_peer_exchange") is not True:
        raise ValueError("independent blind attestation is absent")
    if any(ledger.get(key) is not False for key in (
        "peer_ledger_read", "semantic_labels_read", "model_results_read"
    )):
        raise ValueError("independent ledger was exposed before freeze")
    stored = ledger.get("ledger_sha256")
    body = copy.deepcopy(ledger)
    body.pop("ledger_sha256", None)
    if not isinstance(stored, str) or stored != canonical_sha256(
        {"domain": INDEPENDENT_LEDGER_DOMAIN, "ledger": body}
    ):
        raise ValueError("independent frozen ledger SHA mismatch")
    ids = _validate_binding(ledger, manifest, byte_sha, self_sha)
    for expected_id, row in zip(ids, ledger["rows"]):
        _validate_completed_row(row, expected_id)
        if _canonical_review_row(row) != row:
            raise ValueError(f"independent ledger row is not canonical: {expected_id}")
    if not raw:
        raise ValueError("empty independent ledger")
    return manifest, ledger, byte_sha, self_sha, ledger_byte_sha


def _semantic_from_reference(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": str(row["inference_id"]),
        **{field: row["primary"][field] for field in QA_FIELDS},
        "uncertain_axes": tuple(row["expected_uncertain_axes"]),
        "resolution_insufficient": bool(row["resolution_insufficient"]),
        "evidence": str(row["reason"]),
    }


def _semantic_from_db(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": str(row["inference_id"]),
        "in_scope": bool(row["in_scope"]),
        **{field: str(row[field]) for field in QA_FIELDS if field != "in_scope"},
        "uncertain_axes": tuple(json.loads(str(row["uncertain_axes_json"]))),
        "resolution_insufficient": bool(row["resolution_insufficient"]),
        "evidence": str(row["evidence"]),
    }


def _confusion(counter: Counter[tuple[Any, Any]]) -> list[dict[str, Any]]:
    return [
        {"count": count, "model": model, "reference": reference}
        for (reference, model), count in sorted(
            counter.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
        )
    ]


def _binary_metric(
    *,
    tp: int,
    fp: int,
    fn: int,
    tn: int,
    fp_rows: Sequence[Any] = (),
    fn_rows: Sequence[Any] = (),
) -> dict[str, Any]:
    return {
        "f1": None if 2 * tp + fp + fn == 0 else round((2 * tp) / (2 * tp + fp + fn), 6),
        "fn": fn,
        "fn_rows": list(fn_rows),
        "fp": fp,
        "fp_rows": list(fp_rows),
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "tn": tn,
        "tp": tp,
    }


def inspect_reconciled_ledger(
    *,
    semantic_db: Path,
    review_manifest_path: Path,
    ledger_path: Path,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> dict[str, Any]:
    """Inspect only a fully reconciled frozen reference against immutable DB."""

    pins = _require_pins(pins)
    # Lazy import avoids a module cycle: reconciliation reuses the independent
    # ledger validators above.  Crucially, this completes before any DB access.
    from tools.reconcile_cross_source_semantic_review_n10_v12 import (
        validate_reconciled_frozen_ledger,
    )

    review_manifest, ledger, manifest_byte_sha, manifest_self_sha, _ = (
        validate_reconciled_frozen_ledger(
            review_manifest_path=review_manifest_path,
            ledger_path=ledger_path,
            pins=pins,
        )
    )
    semantic_db = semantic_db.resolve()
    if not semantic_db.is_file():
        raise FileNotFoundError(semantic_db)
    sidecars_before = list(_sidecars(semantic_db))
    if sidecars_before:
        raise ValueError(f"semantic DB has SQLite sidecars: {sidecars_before}")
    size_before = semantic_db.stat().st_size
    sha_before = _file_sha(semantic_db)
    if sha_before != review_manifest["semantic_db"]["byte_sha256_after"]:
        raise ValueError("semantic DB differs from the blind-review manifest")

    connection = _open_immutable(semantic_db)
    try:
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1:
            raise ValueError("semantic DB must contain exactly one run")
        run = dict(runs[0])
        expected_run = _expected_run_pins(pins)
        mismatches = {
            key: {"actual": run.get(key), "expected": value}
            for key, value in expected_run.items()
            if run.get(key) != value
        }
        if mismatches:
            raise ValueError("semantic DB production-pin mismatch: " + canonical_json(mismatches))
        if str(run.get("status")) not in {"complete", "complete_with_failures"}:
            raise ValueError("semantic run is not terminal")
        metrics = json.loads(str(run["metrics_json"]))
        cache = metrics.get("materialization_cache")
        if not isinstance(cache, dict) or cache.get("retained_for_review") is not True:
            raise ValueError("terminal DB lacks retained review-cache provenance")
        if cache.get("write_contract") != "atomic-exclusive-sha256-v1":
            raise ValueError("terminal DB review-cache contract mismatch")
        cache_path = cache.get("path")
        if not isinstance(cache_path, str) or hashlib.sha256(
            str(Path(cache_path).resolve()).encode()
        ).hexdigest() != review_manifest["semantic_run"]["review_cache_provenance"]["path_sha256"]:
            raise ValueError("terminal DB review-cache path provenance mismatch")

        building_count = int(connection.execute(
            "SELECT COUNT(*) FROM selected_buildings WHERE run_id=?", (pins.run_id,)
        ).fetchone()[0])
        occurrences = [dict(row) for row in connection.execute(
            """
            SELECT input_rank,inference_id FROM selected_occurrences
            WHERE run_id=? ORDER BY input_rank
            """,
            (pins.run_id,),
        )]
        result_rows = [dict(row) for row in connection.execute(
            """
            SELECT inference_id,in_scope,reject_reason,medium,spatial_context,
                   framing_scale,camera_angle,drawing_kind,project_state,
                   project_legibility,uncertain_axes_json,
                   resolution_insufficient,evidence
            FROM semantic_results WHERE run_id=? ORDER BY inference_id
            """,
            (pins.run_id,),
        )]
        results = {str(row["inference_id"]): _semantic_from_db(row) for row in result_rows}
        links = {
            str(row["inference_id"]): str(row["result_inference_id"])
            for row in connection.execute(
                "SELECT inference_id,result_inference_id FROM occurrence_result_links WHERE run_id=?",
                (pins.run_id,),
            )
        }
        stored_hero = {
            str(row["inference_id"]): str(row["tier"])
            for row in connection.execute(
                "SELECT inference_id,tier FROM hero_candidate_decisions WHERE run_id=?",
                (pins.run_id,),
            )
        }
        input_statuses = {
            str(row["inference_id"]): str(row["status"])
            for row in connection.execute(
                "SELECT inference_id,status FROM vision_inputs WHERE run_id=?",
                (pins.run_id,),
            )
        }
        successful_inputs = sum(status == "success" for status in input_statuses.values())
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()

    ids = list(review_manifest["ordered_inference_ids"])
    occurrence_ids = [str(row["inference_id"]) for row in occurrences]
    ledger_rows = {str(row["inference_id"]): row for row in ledger["rows"]}
    manifest_inputs = {
        str(row["inference_id"]): row for row in review_manifest["inputs"]
    }
    available_ids = [
        inference_id
        for inference_id in ids
        if bool(manifest_inputs[inference_id]["model_result_available"])
    ]
    available_set = set(available_ids)
    missing_ids = [inference_id for inference_id in ids if inference_id not in available_set]
    matched_set = {
        inference_id
        for inference_id, result_id in links.items()
        if result_id in results
    }
    if (
        building_count != pins.building_count
        or len(occurrence_ids) != pins.occurrence_count
        or set(occurrence_ids) != set(ids)
        or set(ledger_rows) != set(ids)
        or set(input_statuses) != set(ids)
        or set(manifest_inputs) != set(ids)
        or matched_set != available_set
        or set(links) != available_set
        or set(links.values()) != set(results)
        or set(stored_hero) != available_set
        or {key for key, value in input_statuses.items() if value == "success"} != available_set
        or any(
            input_statuses[inference_id]
            != manifest_inputs[inference_id]["vision_status"]
            for inference_id in ids
        )
        or review_manifest["model_result_accounting"]["semantic_result_row_count"] != len(results)
        or review_manifest["model_result_accounting"]["successful_input_count"] != successful_inputs
    ):
        raise ValueError("exact pinned building/occurrence/result/reference accounting failed")
    matched_count = len(available_ids)
    missing_result_rows = [
        {
            "inference_id": inference_id,
            "vision_status": input_statuses[inference_id],
        }
        for inference_id in missing_ids
    ]

    field_state: dict[str, dict[str, Any]] = {
        field: {"acceptable": 0, "clear_errors": [], "confusion": Counter(), "strict": 0}
        for field in QA_FIELDS
    }
    row_uncertainty = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "fp_rows": [], "fn_rows": []}
    axis_uncertainty = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "fp_rows": [], "fn_rows": []}
    axis_breakdown = {
        axis: {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "fp_rows": [], "fn_rows": []}
        for axis in UNCERTAIN_AXES
    }
    resolution = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "fp_rows": [], "fn_rows": []}
    critical_hero_fp: list[dict[str, Any]] = []
    hero_confusion: Counter[tuple[Any, Any]] = Counter()
    hero_derivation_mismatches: list[dict[str, Any]] = []
    usable_support = 0
    usable_retained = 0
    usable_false_negative_rows: list[dict[str, Any]] = []

    for inference_id in available_ids:
        reference_row = ledger_rows[inference_id]
        reference = _semantic_from_reference(reference_row)
        result_id = links[inference_id]
        model = results.get(result_id)
        if model is None:
            raise ValueError(f"result link is dangling: {inference_id}")
        validate_invariants(reference)
        validate_invariants(model)
        for field in QA_FIELDS:
            model_value = model[field]
            reference_value = reference_row["primary"][field]
            acceptable = reference_row["acceptable_values"][field]
            metric = field_state[field]
            metric["strict"] += model_value == reference_value
            metric["acceptable"] += model_value in acceptable
            metric["confusion"][(reference_value, model_value)] += 1
            if model_value not in acceptable:
                metric["clear_errors"].append({
                    "acceptable": acceptable,
                    "inference_id": inference_id,
                    "model": model_value,
                    "reference": reference_value,
                })

        model_axes = set(model["uncertain_axes"])
        reference_axes = set(reference["uncertain_axes"])
        row_key = "tp" if model_axes and reference_axes else "fp" if model_axes else "fn" if reference_axes else "tn"
        row_uncertainty[row_key] += 1
        if row_key in {"fp", "fn"}:
            row_uncertainty[f"{row_key}_rows"].append(inference_id)
        for axis in UNCERTAIN_AXES:
            predicted = axis in model_axes
            expected = axis in reference_axes
            key = "tp" if predicted and expected else "fp" if predicted else "fn" if expected else "tn"
            axis_uncertainty[key] += 1
            axis_breakdown[axis][key] += 1
            if key in {"fp", "fn"}:
                axis_uncertainty[f"{key}_rows"].append({"axis": axis, "inference_id": inference_id})
                axis_breakdown[axis][f"{key}_rows"].append(inference_id)
        model_resolution = bool(model["resolution_insufficient"])
        reference_resolution = bool(reference["resolution_insufficient"])
        resolution_key = "tp" if model_resolution and reference_resolution else "fp" if model_resolution else "fn" if reference_resolution else "tn"
        resolution[resolution_key] += 1
        if resolution_key in {"fp", "fn"}:
            resolution[f"{resolution_key}_rows"].append(inference_id)

        model_hero = derive_hero_decision(model)[0]
        reference_hero = derive_hero_decision(reference)[0]
        stored = stored_hero.get(inference_id)
        if stored != model_hero:
            hero_derivation_mismatches.append(
                {"derived": model_hero, "inference_id": inference_id, "stored": stored}
            )
        hero_confusion[(reference_hero, model_hero)] += 1
        if (
            model_hero in CRITICAL_FALSE_POSITIVE_MODEL_TIERS
            and reference_hero in CRITICAL_FALSE_POSITIVE_REFERENCE_TIERS
        ):
            critical_hero_fp.append(
                {"inference_id": inference_id, "model": model_hero, "reference": reference_hero}
            )
        if reference_hero in USABLE_HERO_TIERS:
            usable_support += 1
            if model_hero in USABLE_HERO_TIERS:
                usable_retained += 1
            else:
                usable_false_negative_rows.append(
                    {"inference_id": inference_id, "model": model_hero, "reference": reference_hero}
                )

    fields: dict[str, Any] = {}
    total_acceptable = 0
    for field, metric in field_state.items():
        total_acceptable += int(metric["acceptable"])
        fields[field] = {
            "acceptable_agreement": _ratio(metric["acceptable"], matched_count),
            "acceptable_count": metric["acceptable"],
            "clear_error_count": len(metric["clear_errors"]),
            "clear_errors": metric["clear_errors"],
            "confusion": _confusion(metric["confusion"]),
            "strict_agreement": _ratio(metric["strict"], matched_count),
            "strict_count": metric["strict"],
            "support": matched_count,
        }
    total_field_decisions = matched_count * len(QA_FIELDS)
    nine_field_agreement = _ratio(total_acceptable, total_field_decisions)
    row_metric = _binary_metric(**row_uncertainty)
    axis_metric = _binary_metric(**axis_uncertainty)
    resolution_metric = _binary_metric(**resolution)
    axis_metrics = {axis: _binary_metric(**metric) for axis, metric in axis_breakdown.items()}
    reference_uncertain_rows = row_uncertainty["tp"] + row_uncertainty["fn"]
    predicted_uncertain_rows = row_uncertainty["tp"] + row_uncertainty["fp"]
    overflow_cap = reference_uncertain_rows + max(5, math.ceil(matched_count * 0.10))
    uncertainty_gate: bool | None
    uncertainty_status: str
    if reference_uncertain_rows < 5:
        uncertainty_gate = None
        uncertainty_status = "INCONCLUSIVE_REFERENCE_SUPPORT_LT_5"
    else:
        uncertainty_gate = bool(
            row_metric["precision"] is not None and row_metric["precision"] >= 0.5
            and row_metric["recall"] is not None and row_metric["recall"] >= 0.5
            and axis_metric["recall"] is not None and axis_metric["recall"] >= 0.4
            and resolution_metric["fn"] == 0
        )
        uncertainty_status = "PASS" if uncertainty_gate else "FAIL"

    size_after = semantic_db.stat().st_size
    sha_after = _file_sha(semantic_db)
    sidecars_after = list(_sidecars(semantic_db))
    if (size_before, sha_before) != (size_after, sha_after) or sidecars_after:
        raise RuntimeError("semantic DB changed or gained sidecars during inspection")

    gates: dict[str, Any] = {
        "critical_hero_false_positives_zero": len(critical_hero_fp) == 0,
        "exact_pinned_population": building_count == pins.building_count and len(ids) == pins.occurrence_count,
        "medium_clear_errors_zero": len(fields["medium"]["clear_errors"]) == 0,
        "nine_field_acceptable_at_least_90_percent": bool(nine_field_agreement is not None and nine_field_agreement >= 0.90),
        "predicted_uncertainty_within_overflow_cap": predicted_uncertain_rows <= overflow_cap,
        "result_coverage_100_percent": matched_count == pins.occurrence_count,
        "scope_clear_errors_zero": len(fields["in_scope"]["clear_errors"]) == 0,
        "uncertainty_quality": uncertainty_gate,
        "uncertainty_quality_status": uncertainty_status,
        "usable_hero_retention_at_least_90_percent": bool(usable_support > 0 and usable_retained / usable_support >= 0.90),
    }
    hard_values = [
        gates["critical_hero_false_positives_zero"],
        gates["exact_pinned_population"],
        gates["medium_clear_errors_zero"],
        gates["nine_field_acceptable_at_least_90_percent"],
        gates["predicted_uncertainty_within_overflow_cap"],
        gates["result_coverage_100_percent"],
        gates["scope_clear_errors_zero"],
        gates["usable_hero_retention_at_least_90_percent"],
    ]
    if uncertainty_gate is False or not all(hard_values) or hero_derivation_mismatches:
        verdict = "FAIL"
    elif uncertainty_gate is None:
        verdict = "PASS_WITH_UNCERTAINTY_INCONCLUSIVE"
    else:
        verdict = "PASS"

    result: dict[str, Any] = {
        "accounting": {
            "buildings": building_count,
            "frozen_reference_rows": len(ledger_rows),
            "matched_result_occurrences": matched_count,
            "missing_result_count": len(missing_ids),
            "missing_results": missing_result_rows,
            "occurrences": len(occurrences),
            "semantic_results": len(results),
            "successful_inputs": successful_inputs,
        },
        "authoritative": False,
        "fields": fields,
        "gates": gates,
        "hero": {
            "critical_false_positive_count": len(critical_hero_fp),
            "critical_false_positive_rows": critical_hero_fp,
            "derivation_mismatches": hero_derivation_mismatches,
            "primary_confusion": _confusion(hero_confusion),
            "usable_false_negative_rows": usable_false_negative_rows,
            "usable_retained": usable_retained,
            "usable_retention": _ratio(usable_retained, usable_support),
            "usable_support": usable_support,
        },
        "inputs": {
            "frozen_reconciled_ledger": {
                "ledger_sha256": ledger["ledger_sha256"],
                "path": str(ledger_path.resolve()),
                "reference_type": "reconciled_agent_reference",
            },
            "review_manifest": {
                "byte_sha256": manifest_byte_sha,
                "manifest_sha256": manifest_self_sha,
                "path": str(review_manifest_path.resolve()),
            },
            "semantic_db": {
                "path": str(semantic_db),
                "sha256_after": sha_after,
                "sha256_before": sha_before,
                "sidecars_after": sidecars_after,
                "sidecars_before": sidecars_before,
                "size_bytes_after": size_after,
                "size_bytes_before": size_before,
            },
        },
        "inspector_version": INSPECTOR_VERSION,
        "nine_field": {
            "acceptable_agreement": nine_field_agreement,
            "acceptable_decisions": total_acceptable,
            "total_decisions": total_field_decisions,
        },
        "operations": {"db_writes": 0, "llm_requests": 0, "network_requests": 0, "vision_requests": 0},
        "reference": {
            "completed_at": ledger["completed_at"],
            "description": "two independently frozen pixel-only agent references, fully reconciled before unblinding",
            "reviewer_ids": list(ledger["reviewer_ids"]),
            "type": "reconciled_agent_reference",
            "unresolved_disagreement_count": 0,
        },
        "run": {key: run[key] for key in expected_run},
        "uncertainty": {
            "axes": axis_metrics,
            "aggregate_axis": axis_metric,
            "predicted_overflow_cap": overflow_cap,
            "predicted_row_count": predicted_uncertain_rows,
            "reference_row_count": reference_uncertain_rows,
            "resolution_insufficient": resolution_metric,
            "rows": row_metric,
        },
        "validation": {
            "foreign_key_violations": foreign_keys,
            "frozen_before_db_open": True,
            "integrity_check": integrity,
            "quick_check": quick,
            "semantic_db_unchanged": sha_before == sha_after,
            "sqlite_sidecars_absent": not sidecars_before and not sidecars_after,
            "two_independent_references_reconciled": True,
        },
        "verdict": verdict,
    }
    result["inspection_sha256"] = canonical_sha256(
        {"domain": INSPECTION_DOMAIN, "inspection": result}
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/freeze independent blind references or inspect reconciled N10 v1.2 QA."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template")
    template.add_argument("--review-manifest", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)
    template.add_argument("--reviewer-id", required=True)
    template.add_argument("--created-at")
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--review-manifest", type=Path, required=True)
    freeze.add_argument("--draft", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--completed-at")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--semantic-db", type=Path, required=True)
    inspect.add_argument("--review-manifest", type=Path, required=True)
    inspect.add_argument("--ledger", type=Path, required=True)
    inspect.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "template":
        result = create_independent_ledger_template(
            review_manifest_path=args.review_manifest,
            output_path=args.output,
            reviewer_id=args.reviewer_id,
            created_at=args.created_at,
        )
    elif args.command == "freeze":
        result = freeze_independent_ledger(
            review_manifest_path=args.review_manifest,
            draft_path=args.draft,
            output_path=args.output,
            completed_at=args.completed_at,
        )
    else:
        result = inspect_reconciled_ledger(
            semantic_db=args.semantic_db,
            review_manifest_path=args.review_manifest,
            ledger_path=args.ledger,
        )
        if args.output is not None:
            _write_no_clobber(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
