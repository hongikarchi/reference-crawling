"""WP1 Stage B: HTTP-only verified pixels, shared derivatives, local features.

No Vision or LLM client is imported here.  Frozen E1 URLs are fetched exactly
once per pending N10 gallery occurrence.  A changed normalized pixel is
recorded as ``source_changed`` and is never admitted to the active cache.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_fetch import FetchFailure, FetchPayload, fetch_once
from canonical.divisare_vision_benchmark import decode_source, prepare_derivative
from canonical.image_fingerprint import FingerprintError, fingerprint_bytes


STAGE_B_VERSION = "vnext-gallery-verified-pixels-n10-v1"
TRANSFORM_VERSION = "vnext-p4b-shared-1024-jpeg-q92-s0-v1"
FEATURE_VERSION = "vnext-local-gradient-patch-8x8-grid6-v1"
PAIR_VERSION = "vnext-local-direct-pair-evidence-v1"
BLIND_REVIEW_VERSION = "vnext-gallery-false-collapse-blind-review-v1"
DEFAULT_ARTIFACT = Path("data/enrichment/vnext_gallery_draft_n10_v1.db")
DEFAULT_CACHE_ROOT = Path("data/enrichment/vnext_p4b_pixel_cache_v1")
DEFAULT_REVIEW_PREFIX = Path("data/reports/vnext_gallery_blind_review_n10_v1")
DEFAULT_REVIEW_FORM = Path("data/reports/vnext_gallery_blind_review_n10_v1_review.json")
DIVISARE_E1 = Path("data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db")
ARCHITIZER_E1 = Path("data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db")
E1_SPECS = {
    "divisare": (DIVISARE_E1, 2_646_114_304, "869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819"),
    "architizer": (ARCHITIZER_E1, 4_373_962_752, "58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937"),
}
SUCCESS_STATUSES = frozenset({"exact", "delivery_changed"})
TERMINAL_STATUSES = frozenset({"exact", "delivery_changed", "source_changed", "failed"})


class VerifiedPixelError(RuntimeError):
    """Raised when Stage B lineage/cache/review invariants fail."""


@dataclass(frozen=True)
class StageBResult:
    artifact_path: Path
    run_id: str
    status: str
    http_requests: int
    resume_http_requests: int | None
    pixel_status_counts: Mapping[str, int]
    derivative_cache_count: int
    local_pair_count: int
    review_sheet_paths: tuple[Path, ...]
    review_form_path: Path
    elapsed_seconds: float


@dataclass(frozen=True)
class _FetchResult:
    key: tuple[str, str, str]
    status: str
    request_count: int
    actual_raw_sha256: str | None
    actual_pixel_sha256: str | None
    final_url: str | None
    http_status: int | None
    response_bytes: int | None
    retryable: int
    error_kind: str | None
    error_message: str | None
    body: bytes | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _record_sha(value: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(value))


def _assert_no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            raise VerifiedPixelError(f"forbidden SQLite sidecar exists: {sidecar}")


def _atomic_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise VerifiedPixelError(f"shared cache collision: {path}")
        return
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise VerifiedPixelError(f"stale cache temporary exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _atomic_json(path: Path, payload: Mapping[str, Any], *, no_clobber: bool) -> None:
    if no_clobber and path.exists():
        raise VerifiedPixelError(f"refusing to clobber existing review artifact: {path}")
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise VerifiedPixelError(f"stale review temporary exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _cache_relative_path(pixel_sha256: str) -> Path:
    transform_key = hashlib.sha256(TRANSFORM_VERSION.encode("utf-8")).hexdigest()[:12]
    return DEFAULT_CACHE_ROOT / pixel_sha256[:2] / f"{pixel_sha256}.{transform_key}.jpg"


def _e1_row(connection: sqlite3.Connection, source_asset_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT sa.run_id,sa.fetch_url,sa.source_record_sha256,f.status,f.raw_response_sha256,"
        "f.normalized_pixel_sha256,f.selected_attempt_no "
        "FROM source_assets sa JOIN fingerprints f USING(run_id,source_asset_id) "
        "WHERE sa.source_asset_id=?",
        (source_asset_id,),
    ).fetchone()


def _initialize_manifest(
    database: sqlite3.Connection,
    run_id: str,
    e1_connections: Mapping[str, sqlite3.Connection],
) -> None:
    existing = int(database.execute("SELECT count(*) FROM pixel_verifications WHERE run_id=?", (run_id,)).fetchone()[0])
    gallery = database.execute(
        "SELECT i.source,i.source_building_id,i.source_asset_id,i.e1_run_id,i.fetch_url,"
        "i.raw_response_sha256,i.normalized_pixel_sha256,i.e2_asset_record_sha256 "
        "FROM input_assets i JOIN group_members g USING(run_id,source,source_building_id,source_asset_id) "
        "WHERE i.run_id=? ORDER BY i.source,i.source_building_id,i.source_asset_id",
        (run_id,),
    ).fetchall()
    if existing not in (0, len(gallery)):
        raise VerifiedPixelError("partial pixel manifest has unexpected row count")
    if existing == len(gallery):
        return
    failures: list[str] = []
    for row in gallery:
        source = str(row[0])
        e1 = _e1_row(e1_connections[source], str(row[2]))
        if e1 is None:
            failures.append(f"missing E1 row:{source}/{row[2]}")
            fetch_url = None
            expected_raw = row[5]
            expected_pixel = row[6]
        else:
            fetch_url = e1[1]
            expected_raw = e1[4]
            expected_pixel = e1[5]
            if (e1[0], e1[1], e1[3], e1[4], e1[5]) != (row[3], row[4], "success", row[5], row[6]):
                failures.append(f"E1/E2 lineage mismatch:{source}/{row[2]}")
        database.execute(
            "INSERT INTO pixel_verifications(run_id,source,source_building_id,source_asset_id,status,fetch_url,"
            "expected_raw_sha256,expected_pixel_sha256,error_kind,error_message) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, source, row[1], row[2], "pending" if e1 is not None else "failed",
                fetch_url, expected_raw, expected_pixel,
                None if e1 is not None else "missing_e1_lineage",
                None if e1 is not None else "frozen E1 source asset row is missing",
            ),
        )
    if failures:
        raise VerifiedPixelError("frozen E1/E2 manifest lineage failed: " + "; ".join(failures[:5]))
    database.execute(
        "INSERT OR REPLACE INTO validations VALUES(?,?,?,?,?,?,?,?)",
        (run_id, "stage_b", "e1_manifest_lineage", 1, "all match", "all match", "{}", utc_now()),
    )
    database.commit()


def _fetch_one(row: Mapping[str, Any], fetcher: Callable[..., FetchPayload]) -> _FetchResult:
    key = (str(row["source"]), str(row["source_building_id"]), str(row["source_asset_id"]))
    started = time.monotonic()
    try:
        payload = fetcher(str(row["source"]), str(row["fetch_url"]))
        fingerprint = fingerprint_bytes(payload.body)
        if fingerprint.response_sha256 != payload.raw_response_sha256:
            raise VerifiedPixelError("fetch payload raw SHA and fingerprint raw SHA disagree")
        if fingerprint.pixel_sha256 != row["expected_pixel_sha256"]:
            status = "source_changed"
            body = None
        elif fingerprint.response_sha256 == row["expected_raw_sha256"]:
            status = "exact"
            body = payload.body
        else:
            status = "delivery_changed"
            body = payload.body
        return _FetchResult(
            key=key, status=status, request_count=int(payload.request_count),
            actual_raw_sha256=fingerprint.response_sha256,
            actual_pixel_sha256=fingerprint.pixel_sha256, final_url=payload.final_url,
            http_status=int(payload.http_status), response_bytes=len(payload.body), retryable=0,
            error_kind=None, error_message=None, body=body,
        )
    except FetchFailure as exc:
        return _FetchResult(
            key=key, status="failed", request_count=int(exc.request_count),
            actual_raw_sha256=exc.raw_response_sha256, actual_pixel_sha256=None,
            final_url=exc.final_url, http_status=exc.http_status,
            response_bytes=exc.response_bytes, retryable=int(exc.retryable),
            error_kind=exc.kind, error_message=str(exc)[:2000], body=None,
        )
    except (FingerprintError, ValueError, OSError, VerifiedPixelError) as exc:
        return _FetchResult(
            key=key, status="failed", request_count=1,
            actual_raw_sha256=None, actual_pixel_sha256=None, final_url=None,
            http_status=None, response_bytes=None, retryable=0,
            error_kind="local_fingerprint_failed", error_message=str(exc)[:2000], body=None,
        )
    finally:
        _ = started


def _materialize_derivative(root: Path, pixel_sha256: str, body: bytes) -> dict[str, Any]:
    decoded = decode_source(body)
    derivative = prepare_derivative(decoded, "vnext_p4b_shared", 1024)
    relative = _cache_relative_path(pixel_sha256)
    target = root / relative
    _atomic_bytes(target, derivative.encoded_bytes)
    payload = {
        "normalized_pixel_sha256": pixel_sha256,
        "transform_version": TRANSFORM_VERSION,
        "cache_key": canonical_sha256({"pixel_sha256": pixel_sha256, "transform_version": TRANSFORM_VERSION}),
        "relative_path": relative.as_posix(),
        "encoded_sha256": derivative.encoded_sha256,
        "derivative_pixel_sha256": derivative.pixel_sha256,
        "width": derivative.width,
        "height": derivative.height,
        "encoded_bytes": len(derivative.encoded_bytes),
    }
    payload["record_sha256"] = _record_sha(payload)
    return payload


def extract_local_features(raw: bytes) -> dict[str, Any]:
    """Extract deterministic gradient-selected 8x8 local patch descriptors."""

    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as opened:
        image = ImageOps.exif_transpose(opened).convert("L")
        image.thumbnail((384, 384), Image.Resampling.LANCZOS)
        array = np.asarray(image, dtype=np.float32)
    if array.shape[0] < 32 or array.shape[1] < 32:
        return {"width": int(array.shape[1]), "height": int(array.shape[0]), "descriptors": []}
    gx = np.zeros_like(array)
    gy = np.zeros_like(array)
    gx[:, 1:-1] = array[:, 2:] - array[:, :-2]
    gy[1:-1, :] = array[2:, :] - array[:-2, :]
    magnitude = gx * gx + gy * gy
    border = 14
    height, width = array.shape
    descriptors: list[dict[str, Any]] = []
    for grid_y in range(6):
        y0 = max(border, round(grid_y * height / 6))
        y1 = min(height - border, round((grid_y + 1) * height / 6))
        for grid_x in range(6):
            x0 = max(border, round(grid_x * width / 6))
            x1 = min(width - border, round((grid_x + 1) * width / 6))
            if y1 <= y0 or x1 <= x0:
                continue
            region = magnitude[y0:y1, x0:x1]
            index = int(np.argmax(region))
            yy, xx = np.unravel_index(index, region.shape)
            y, x = y0 + int(yy), x0 + int(xx)
            if float(magnitude[y, x]) < 64.0:
                continue
            patch = array[y - 12 : y + 13, x - 12 : x + 13]
            patch_image = Image.fromarray(np.clip(patch, 0, 255).astype(np.uint8), mode="L")
            small = np.asarray(patch_image.resize((8, 8), Image.Resampling.BILINEAR), dtype=np.float32)
            mean = float(small.mean())
            std = float(small.std())
            if std < 2.0:
                continue
            vector = np.clip(np.rint((small - mean) / std * 24.0), -127, 127).astype(np.int8)
            descriptors.append(
                {
                    "x": round(x / max(1, width - 1), 6),
                    "y": round(y / max(1, height - 1), 6),
                    "v": [int(value) for value in vector.reshape(-1)],
                }
            )
    return {"width": width, "height": height, "descriptors": descriptors}


def compare_local_features(left: Mapping[str, Any], right: Mapping[str, Any], *, same_pixel: bool = False) -> dict[str, Any]:
    import numpy as np

    left_rows = list(left.get("descriptors", []))
    right_rows = list(right.get("descriptors", []))
    if not left_rows or not right_rows:
        return {"matched_keypoints": 0, "left_keypoints": len(left_rows), "right_keypoints": len(right_rows), "median_similarity": None, "max_similarity": None, "outcome": "unavailable"}
    left_array = np.asarray([row["v"] for row in left_rows], dtype=np.float32)
    right_array = np.asarray([row["v"] for row in right_rows], dtype=np.float32)
    left_array /= np.maximum(np.linalg.norm(left_array, axis=1, keepdims=True), 1e-9)
    right_array /= np.maximum(np.linalg.norm(right_array, axis=1, keepdims=True), 1e-9)
    similarities = left_array @ right_array.T
    left_best = similarities.argmax(axis=1)
    right_best = similarities.argmax(axis=0)
    matched: list[float] = []
    for left_index, right_index in enumerate(left_best):
        score = float(similarities[left_index, right_index])
        if int(right_best[right_index]) == left_index and score >= 0.88:
            matched.append(score)
    median = float(np.median(matched)) if matched else None
    maximum = float(similarities.max())
    minimum_match = max(3, math.ceil(min(len(left_rows), len(right_rows)) * 0.10))
    if same_pixel and len(matched) >= minimum_match:
        outcome = "same_pixel_match"
    elif len(matched) >= minimum_match and (median or 0.0) >= 0.90:
        outcome = "local_feature_match"
    else:
        outcome = "weak_or_distinct"
    return {
        "matched_keypoints": len(matched), "left_keypoints": len(left_rows),
        "right_keypoints": len(right_rows),
        "median_similarity": None if median is None else round(median, 8),
        "max_similarity": round(maximum, 8), "outcome": outcome,
    }


def _load_feature(database: sqlite3.Connection, run_id: str, pixel: str) -> dict[str, Any] | None:
    row = database.execute(
        "SELECT descriptor_json FROM local_features WHERE run_id=? AND normalized_pixel_sha256=? AND feature_version=?",
        (run_id, pixel, FEATURE_VERSION),
    ).fetchone()
    return None if row is None else json.loads(str(row[0]))


def _build_features_and_pairs(database: sqlite3.Connection, root: Path, run_id: str) -> int:
    caches = [dict(row) for row in database.execute("SELECT * FROM derivative_cache WHERE run_id=? ORDER BY normalized_pixel_sha256", (run_id,))]
    for row in caches:
        pixel = str(row["normalized_pixel_sha256"])
        if database.execute("SELECT 1 FROM local_features WHERE run_id=? AND normalized_pixel_sha256=? AND feature_version=?", (run_id, pixel, FEATURE_VERSION)).fetchone():
            continue
        raw = (root / str(row["relative_path"])).read_bytes()
        features = extract_local_features(raw)
        descriptor_json = canonical_json(features)
        payload = {
            "normalized_pixel_sha256": pixel, "feature_version": FEATURE_VERSION,
            "keypoint_count": len(features["descriptors"]),
            "descriptor_sha256": hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest(),
            "descriptor_json": descriptor_json,
        }
        database.execute(
            "INSERT INTO local_features VALUES(?,?,?,?,?,?,?)",
            (run_id, pixel, FEATURE_VERSION, payload["keypoint_count"], payload["descriptor_sha256"], descriptor_json, _record_sha(payload)),
        )
    database.execute("DELETE FROM local_pair_evidence WHERE run_id=?", (run_id,))
    verifications = {
        str(row["source_asset_id"]): dict(row)
        for row in database.execute("SELECT * FROM pixel_verifications WHERE run_id=?", (run_id,))
    }
    direct_rows = [dict(row) for row in database.execute("SELECT * FROM direct_edge_evidence WHERE run_id=? ORDER BY pair_id", (run_id,))]
    pair_rows: list[dict[str, Any]] = []
    for direct in direct_rows:
        left_asset = str(direct["representative_source_asset_id"])
        right_asset = str(direct["member_class_representative_asset_id"])
        left_v, right_v = verifications[left_asset], verifications[right_asset]
        left_pixel = str(left_v["actual_pixel_sha256"] or left_v["expected_pixel_sha256"])
        right_pixel = str(right_v["actual_pixel_sha256"] or right_v["expected_pixel_sha256"])
        left_feature = _load_feature(database, run_id, left_pixel) if left_v["status"] in SUCCESS_STATUSES else None
        right_feature = _load_feature(database, run_id, right_pixel) if right_v["status"] in SUCCESS_STATUSES else None
        comparison = compare_local_features(left_feature, right_feature) if left_feature and right_feature else {
            "matched_keypoints": 0, "left_keypoints": 0, "right_keypoints": 0,
            "median_similarity": None, "max_similarity": None, "outcome": "unavailable",
        }
        pair_rows.append(
            {
                "local_pair_id": "vnx_local_" + canonical_sha256({"direct_pair_id": direct["pair_id"], "version": PAIR_VERSION})[:32],
                "pair_kind": "e2_direct", "direct_pair_id": direct["pair_id"],
                "left_source_asset_id": left_asset, "right_source_asset_id": right_asset,
                "left_pixel_sha256": left_pixel, "right_pixel_sha256": right_pixel,
                **comparison,
            }
        )
    first_success = next((row for row in verifications.values() if row["status"] in SUCCESS_STATUSES), None)
    if first_success is None:
        raise VerifiedPixelError("no verified pixel is available for exact-pixel feature smoke")
    asset = str(first_success["source_asset_id"])
    pixel = str(first_success["actual_pixel_sha256"])
    feature = _load_feature(database, run_id, pixel)
    if feature is None:
        raise VerifiedPixelError("exact-pixel smoke feature is missing")
    pair_rows.append(
        {
            "local_pair_id": "vnx_local_" + canonical_sha256({"pixel": pixel, "version": PAIR_VERSION, "kind": "exact_smoke"})[:32],
            "pair_kind": "exact_pixel_smoke", "direct_pair_id": None,
            "left_source_asset_id": asset, "right_source_asset_id": asset,
            "left_pixel_sha256": pixel, "right_pixel_sha256": pixel,
            **compare_local_features(feature, feature, same_pixel=True),
        }
    )
    for row in pair_rows:
        payload = dict(row)
        payload.update({"feature_version": FEATURE_VERSION, "identity_authorization": 0})
        database.execute(
            "INSERT INTO local_pair_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,row["local_pair_id"],row["pair_kind"],row["direct_pair_id"],
                row["left_source_asset_id"],row["right_source_asset_id"],row["left_pixel_sha256"],
                row["right_pixel_sha256"],FEATURE_VERSION,row["matched_keypoints"],row["left_keypoints"],
                row["right_keypoints"],row["median_similarity"],row["max_similarity"],row["outcome"],
                0,_record_sha(payload),
            ),
        )
    return len(pair_rows)


def _fit_image(raw: bytes, width: int, height: int):
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    return canvas


def _render_blind_sheets(
    database: sqlite3.Connection,
    root: Path,
    run_id: str,
    prefix: Path,
    review_form: Path,
) -> tuple[tuple[Path, ...], Path]:
    from PIL import Image, ImageDraw

    direct_rows = [dict(row) for row in database.execute("SELECT * FROM direct_edge_evidence WHERE run_id=? ORDER BY pair_id", (run_id,))]
    if not direct_rows:
        raise VerifiedPixelError("blind review requires at least one direct attachment")
    verification = {str(row["source_asset_id"]): dict(row) for row in database.execute("SELECT * FROM pixel_verifications WHERE run_id=?", (run_id,))}
    cache = {str(row["normalized_pixel_sha256"]): dict(row) for row in database.execute("SELECT * FROM derivative_cache WHERE run_id=?", (run_id,))}
    ordered = sorted(direct_rows, key=lambda row: hashlib.sha256((run_id + "\0blind\0" + str(row["pair_id"])).encode()).hexdigest())
    page_paths: list[Path] = []
    database.execute("DELETE FROM blind_review_items WHERE run_id=?", (run_id,))
    per_page = 4
    for page_index in range(math.ceil(len(ordered) / per_page)):
        page_items = ordered[page_index * per_page : (page_index + 1) * per_page]
        canvas = Image.new("RGB", (1500, 80 + 350 * len(page_items)), (245, 245, 245))
        draw = ImageDraw.Draw(canvas)
        draw.text((30, 24), "Blind duplicate review — pixels only", fill="black")
        for offset, row in enumerate(page_items):
            global_index = page_index * per_page + offset + 1
            opaque = f"B{global_index:03d}"
            y = 70 + offset * 350
            draw.text((30, y + 8), opaque, fill="black")
            left_asset = str(row["representative_source_asset_id"])
            right_asset = str(row["member_class_representative_asset_id"])
            for side, asset, x in (("L", left_asset, 90), ("R", right_asset, 795)):
                state = verification[asset]
                if state["status"] in SUCCESS_STATUSES and str(state["actual_pixel_sha256"]) in cache:
                    cache_row = cache[str(state["actual_pixel_sha256"])]
                    fitted = _fit_image((root / str(cache_row["relative_path"])).read_bytes(), 650, 300)
                    canvas.paste(fitted, (x, y + 35))
                else:
                    draw.rectangle((x, y + 35, x + 650, y + 335), fill=(210, 210, 210), outline=(80, 80, 80))
                    draw.text((x + 260, y + 175), "UNAVAILABLE", fill=(80, 80, 80))
                draw.text((x + 5, y + 40), side, fill="black")
            page_path = prefix.with_name(prefix.name + f"_page_{page_index + 1:02d}.png")
            relative_page = page_path.as_posix()
            database.execute(
                "INSERT INTO blind_review_items(run_id,opaque_pair_id,direct_pair_id,sheet_path,sheet_page,sheet_order) VALUES(?,?,?,?,?,?)",
                (run_id, opaque, row["pair_id"], relative_page, page_index + 1, global_index),
            )
        page_path = root / prefix.with_name(prefix.name + f"_page_{page_index + 1:02d}.png")
        if page_path.exists():
            raise VerifiedPixelError(f"refusing to clobber blind sheet: {page_path}")
        page_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = page_path.with_name(page_path.name + ".building")
        canvas.save(temporary, format="PNG", optimize=False)
        os.replace(temporary, page_path)
        page_paths.append(page_path)
    form_path = root / review_form
    form_payload = {
        "review_version": BLIND_REVIEW_VERSION,
        "run_id": run_id,
        "blind_to_source_project_url_filename": True,
        "reviewer": None,
        "items": [
            {"opaque_pair_id": f"B{index:03d}", "decision": None}
            for index in range(1, len(ordered) + 1)
        ],
        "allowed_decisions": ["same_or_variant", "distinct_false_collapse", "unreviewable"],
    }
    _atomic_json(form_path, form_payload, no_clobber=True)
    return tuple(page_paths), form_path


def _result(database: sqlite3.Connection, artifact: Path, run_id: str, elapsed: float, review_form: Path) -> StageBResult:
    run = database.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    counts = {str(row[0]): int(row[1]) for row in database.execute("SELECT status,count(*) FROM pixel_verifications WHERE run_id=? GROUP BY status", (run_id,))}
    sheets = tuple(
        sorted(
            {artifact.parent.parent.parent / str(row[0]) for row in database.execute("SELECT sheet_path FROM blind_review_items WHERE run_id=?", (run_id,))},
            key=str,
        )
    )
    return StageBResult(
        artifact_path=artifact, run_id=run_id, status=str(run["stage_b_status"]),
        http_requests=int(run["stage_b_http_requests"]),
        resume_http_requests=None if run["stage_b_resume_http_requests"] is None else int(run["stage_b_resume_http_requests"]),
        pixel_status_counts=counts,
        derivative_cache_count=int(database.execute("SELECT count(*) FROM derivative_cache WHERE run_id=?", (run_id,)).fetchone()[0]),
        local_pair_count=int(database.execute("SELECT count(*) FROM local_pair_evidence WHERE run_id=?", (run_id,)).fetchone()[0]),
        review_sheet_paths=sheets, review_form_path=review_form,
        elapsed_seconds=elapsed,
    )


def run_verified_pixels(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_ARTIFACT,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    review_prefix: Path = DEFAULT_REVIEW_PREFIX,
    review_form: Path = DEFAULT_REVIEW_FORM,
    workers: int = 4,
    verify_full_hashes: bool = True,
    fetcher: Callable[..., FetchPayload] = fetch_once,
) -> StageBResult:
    del cache_root  # The cache root is contract-fixed; the argument documents the boundary.
    started = time.monotonic()
    root = repo_root.resolve()
    artifact = (root / artifact_path).resolve() if not artifact_path.is_absolute() else artifact_path.resolve()
    if workers < 1 or workers > 8:
        raise ValueError("workers must be between 1 and 8")
    if not artifact.is_file():
        raise VerifiedPixelError(f"Stage A artifact is missing: {artifact}")
    _assert_no_sidecars(artifact)
    e1_before: dict[str, str] = {}
    for source, (relative, size, expected_sha) in E1_SPECS.items():
        path = root / relative
        if not path.is_file() or path.stat().st_size != size:
            raise VerifiedPixelError(f"frozen {source} E1 size mismatch")
        digest = sha256_file(path) if verify_full_hashes else expected_sha
        if digest != expected_sha:
            raise VerifiedPixelError(f"frozen {source} E1 pre-run SHA mismatch")
        e1_before[source] = digest
    e1_connections = {source: _open_immutable(root / spec[0]) for source, spec in E1_SPECS.items()}
    database = sqlite3.connect(artifact)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("PRAGMA synchronous=FULL")
    database.execute("PRAGMA foreign_keys=ON")
    try:
        runs = database.execute("SELECT * FROM runs").fetchall()
        if len(runs) != 1 or runs[0]["status"] not in {"stage_a_complete", "stage_b_complete", "complete"}:
            raise VerifiedPixelError("artifact is not a terminal Stage A run")
        run_id = str(runs[0]["run_id"])
        _initialize_manifest(database, run_id, e1_connections)
        states = [str(row[0]) for row in database.execute("SELECT status FROM pixel_verifications WHERE run_id=?", (run_id,))]
        if states and all(state in TERMINAL_STATUSES for state in states) and runs[0]["stage_b_status"] == "complete":
            database.execute("UPDATE runs SET stage_b_resume_http_requests=0 WHERE run_id=?", (run_id,))
            database.commit()
            return _result(database, artifact, run_id, time.monotonic() - started, root / review_form)
        pending = [dict(row) for row in database.execute("SELECT * FROM pixel_verifications WHERE run_id=? AND status='pending' ORDER BY source,source_building_id,source_asset_id", (run_id,))]
        if not pending:
            raise VerifiedPixelError("Stage B has no pending rows but is not terminal complete")
        database.execute("UPDATE runs SET stage_b_status='running',stage_b_http_requests=0,stage_b_resume_http_requests=NULL WHERE run_id=?", (run_id,))
        database.commit()
        results: list[_FetchResult] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vnext-pixel") as executor:
            futures = {executor.submit(_fetch_one, row, fetcher): row for row in pending}
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda row: row.key)
        total_requests = 0
        for result in results:
            total_requests += result.request_count
            source, building, asset = result.key
            expected = database.execute(
                "SELECT expected_raw_sha256,expected_pixel_sha256 FROM pixel_verifications "
                "WHERE run_id=? AND source=? AND source_building_id=? AND source_asset_id=?",
                (run_id, source, building, asset),
            ).fetchone()
            if expected is None:
                raise VerifiedPixelError("pending pixel row disappeared")
            if result.status in SUCCESS_STATUSES:
                if result.body is None or result.actual_pixel_sha256 != expected[1]:
                    raise VerifiedPixelError("successful verification lacks expected pixel body")
                cache = _materialize_derivative(root, str(result.actual_pixel_sha256), result.body)
                database.execute(
                    "INSERT OR IGNORE INTO derivative_cache VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,cache["normalized_pixel_sha256"],cache["transform_version"],cache["cache_key"],
                        cache["relative_path"],cache["encoded_sha256"],cache["derivative_pixel_sha256"],
                        cache["width"],cache["height"],cache["encoded_bytes"],cache["record_sha256"],
                    ),
                )
            record = {
                "source": source, "source_building_id": building, "source_asset_id": asset,
                "status": result.status, "expected_raw_sha256": expected[0], "expected_pixel_sha256": expected[1],
                "actual_raw_sha256": result.actual_raw_sha256, "actual_pixel_sha256": result.actual_pixel_sha256,
                "actual_final_url": result.final_url, "http_status": result.http_status,
                "response_bytes": result.response_bytes, "request_count": result.request_count,
                "retryable": result.retryable, "error_kind": result.error_kind,
            }
            database.execute(
                "UPDATE pixel_verifications SET status=?,actual_raw_sha256=?,actual_pixel_sha256=?,actual_final_url=?,"
                "http_status=?,response_bytes=?,request_count=?,retryable=?,error_kind=?,error_message=?,started_at=?,"
                "completed_at=?,record_sha256=? WHERE run_id=? AND source=? AND source_building_id=? AND source_asset_id=?",
                (
                    result.status,result.actual_raw_sha256,result.actual_pixel_sha256,result.final_url,result.http_status,
                    result.response_bytes,result.request_count,result.retryable,result.error_kind,result.error_message,
                    utc_now(),utc_now(),_record_sha(record),run_id,source,building,asset,
                ),
            )
            database.commit()
        pair_count = _build_features_and_pairs(database, root, run_id)
        sheets, form_path = _render_blind_sheets(database, root, run_id, review_prefix, review_form)
        for source, (relative, _size, expected_sha) in E1_SPECS.items():
            after = sha256_file(root / relative) if verify_full_hashes else expected_sha
            if after != e1_before[source]:
                raise VerifiedPixelError(f"frozen {source} E1 SHA changed during Stage B")
            database.execute(
                "INSERT OR REPLACE INTO validations VALUES(?,?,?,?,?,?,?,?)",
                (run_id, "stage_b", f"{source}_e1_sha_pre_post_unchanged", 1, e1_before[source], after, "{}", utc_now()),
            )
        statuses = {str(row[0]): int(row[1]) for row in database.execute("SELECT status,count(*) FROM pixel_verifications WHERE run_id=? GROUP BY status", (run_id,))}
        if not set(statuses).issubset(TERMINAL_STATUSES):
            raise VerifiedPixelError("non-terminal Stage B pixel rows remain")
        database.execute(
            "UPDATE runs SET stage_b_status='complete',stage_b_http_requests=?,stage_b_vision_requests=0,"
            "stage_b_llm_requests=0,blind_review_status='pending' WHERE run_id=?",
            (total_requests, run_id),
        )
        database.execute("INSERT OR REPLACE INTO metrics(run_id,phase,metric_name,value_integer) VALUES(?,?,?,?)", (run_id, "stage_b", "http_requests", total_requests))
        database.execute("INSERT OR REPLACE INTO metrics(run_id,phase,metric_name,value_integer) VALUES(?,?,?,?)", (run_id, "stage_b", "local_pair_count", pair_count))
        database.commit()
        result = StageBResult(
            artifact_path=artifact, run_id=run_id, status="complete", http_requests=total_requests,
            resume_http_requests=None, pixel_status_counts=statuses,
            derivative_cache_count=int(database.execute("SELECT count(*) FROM derivative_cache WHERE run_id=?", (run_id,)).fetchone()[0]),
            local_pair_count=pair_count, review_sheet_paths=sheets, review_form_path=form_path,
            elapsed_seconds=time.monotonic() - started,
        )
        return result
    finally:
        for connection in e1_connections.values():
            connection.close()
        database.close()
        _assert_no_sidecars(artifact)


def apply_blind_review(*, repo_root: Path, artifact_path: Path, review_path: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    artifact = (root / artifact_path).resolve() if not artifact_path.is_absolute() else artifact_path.resolve()
    review_file = (root / review_path).resolve() if not review_path.is_absolute() else review_path.resolve()
    payload = json.loads(review_file.read_text(encoding="utf-8"))
    if payload.get("review_version") != BLIND_REVIEW_VERSION or payload.get("blind_to_source_project_url_filename") is not True:
        raise VerifiedPixelError("blind review contract/version mismatch")
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise VerifiedPixelError("blind review requires a reviewer")
    items = payload.get("items")
    if not isinstance(items, list):
        raise VerifiedPixelError("blind review items must be a list")
    decisions = {"same_or_variant", "distinct_false_collapse", "unreviewable"}
    parsed: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("decision") not in decisions:
            raise VerifiedPixelError("blind review contains an invalid decision")
        opaque = str(item.get("opaque_pair_id", ""))
        if not opaque or opaque in parsed:
            raise VerifiedPixelError("blind review opaque IDs must be unique")
        parsed[opaque] = str(item["decision"])
    _assert_no_sidecars(artifact)
    database = sqlite3.connect(artifact)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("PRAGMA synchronous=FULL")
    try:
        run = database.execute("SELECT * FROM runs").fetchone()
        if run is None or run["stage_b_status"] != "complete":
            raise VerifiedPixelError("Stage B is not complete")
        run_id = str(run["run_id"])
        expected = {str(row[0]) for row in database.execute("SELECT opaque_pair_id FROM blind_review_items WHERE run_id=?", (run_id,))}
        if set(parsed) != expected:
            raise VerifiedPixelError("blind review opaque ID set mismatch")
        reviewed_at = utc_now()
        for opaque, decision in sorted(parsed.items()):
            record = {
                "opaque_pair_id": opaque, "decision": decision, "reviewer": reviewer.strip(),
                "review_version": BLIND_REVIEW_VERSION, "reviewed_at": reviewed_at,
            }
            database.execute(
                "UPDATE blind_review_items SET decision=?,reviewer=?,reviewed_at=?,record_sha256=? "
                "WHERE run_id=? AND opaque_pair_id=?",
                (decision, reviewer.strip(), reviewed_at, _record_sha(record), run_id, opaque),
            )
        false_collapses = sum(value == "distinct_false_collapse" for value in parsed.values())
        unreviewable = sum(value == "unreviewable" for value in parsed.values())
        passed = false_collapses == 0 and unreviewable == 0
        database.execute(
            "UPDATE runs SET blind_review_status=?,status=? WHERE run_id=?",
            ("pass" if passed else "fail", "complete" if passed else "stage_b_complete", run_id),
        )
        database.execute(
            "INSERT OR REPLACE INTO validations VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "blind_review", "false_collapse_zero", int(false_collapses == 0), "0", str(false_collapses), canonical_json({"unreviewable": unreviewable}), reviewed_at),
        )
        database.commit()
        return {
            "run_id": run_id, "passed": passed, "reviewer": reviewer.strip(),
            "reviewed_pairs": len(parsed), "false_collapse_count": false_collapses,
            "unreviewable_count": unreviewable,
        }
    finally:
        database.close()
        _assert_no_sidecars(artifact)


__all__ = [
    "BLIND_REVIEW_VERSION", "DEFAULT_ARTIFACT", "DEFAULT_CACHE_ROOT", "DEFAULT_REVIEW_FORM",
    "DEFAULT_REVIEW_PREFIX", "FEATURE_VERSION", "PAIR_VERSION", "STAGE_B_VERSION",
    "TRANSFORM_VERSION", "StageBResult", "VerifiedPixelError", "apply_blind_review",
    "compare_local_features", "extract_local_features", "run_verified_pixels",
]
