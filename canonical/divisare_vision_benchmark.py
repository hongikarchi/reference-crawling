"""Deterministic Divisare Vision resolution benchmark primitives.

The v2.4 metadata database remains immutable.  This module selects an
image-semantic sample, prepares comparable 1024/2048 derivatives from the
same decoded source, validates model output, and stores benchmark evidence in
a separate SQLite sidecar.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
import struct
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from canonical.divisare_image_smoke import (
    FetchFailure,
    FetchPayload,
    SampleAsset,
    canonical_json,
    file_sha256,
    fixed_derivative_url,
    network_fetch,
    open_source_readonly,
    select_stratified_sample,
)
from canonical.divisare_vision_runtime import (
    CLI_IMAGE_DETAIL,
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
    RUNTIME_VERSION,
    VisionRuntimeResult,
    run_codex_vision_batch,
)


SCHEMA_VERSION = 2
BENCHMARK_VERSION = "divisare-vision-resolution-benchmark-v1.1.0"
SELECTION_VERSION = "divisare-vision-semantic-strata-v1.0.0"
SOURCE_DERIVATIVE_VERSION = "divisare-cloudinary-max2048-jpeg-q92-v1.0.0"
LOCAL_DERIVATIVE_VERSION = "pillow-exif-rgb-lanczos-jpeg-q92-subsampling0-v1.0.0"
PROMPT_VERSION = "divisare-image-semantics-v1.1.0"
SOURCE_PROFILE = "c_limit,f_jpg,h_2048,q_92,w_2048"
PDF_SOURCE_PROFILE = "pg_1,c_limit,f_jpg,h_2048,q_92,w_2048"
LANES: tuple[tuple[str, int], ...] = (("long1024", 1024), ("long2048", 2048))
MAX_CANDIDATE_POOL = 10_000

MEDIA_VALUES = frozenset(
    {"photograph", "drawing", "rendering", "physical_model", "mixed", "other", "unknown"}
)
VIEW_VALUES = frozenset(
    {
        "exterior",
        "interior",
        "aerial",
        "detail",
        "plan",
        "section",
        "elevation",
        "axonometric",
        "site_plan",
        "diagram",
        "construction",
        "portrait",
        "object",
        "mixed",
        "other",
        "unknown",
    }
)
MATERIAL_VALUES = frozenset(
    {
        "concrete",
        "timber",
        "brick",
        "stone",
        "glass",
        "steel",
        "aluminum",
        "copper",
        "ceramic",
        "plaster",
        "earth",
        "textile",
        "plastic",
        "other",
    }
)
ELEMENT_VALUES = frozenset(
    {
        "courtyard",
        "patio",
        "balcony",
        "terrace",
        "ramp",
        "outdoor_stair",
        "colonnade",
        "arch",
        "vault",
        "dome",
        "canopy",
        "bridge",
        "louver",
        "brise_soleil",
        "perforated_screen",
        "skylight",
        "atrium",
        "green_roof",
        "sawtooth_roof",
    }
)

_COHORT_CYCLE = (
    "convertible_document",
    "filename_media_hint",
    "article_drawing_prior",
    "special_media_prior",
    "interior_prior",
    "material_element_prior",
    "multi_url_edge",
    "cover_plain",
    "legacy_plain",
    "gallery_plain",
)
_DRAWING_HINTS = frozenset({"drawing", "section", "plan", "construction detail"})
_SPECIAL_HINTS = frozenset(
    {"model", "reportage", "portrait", "night photography", "notebook/sketch"}
)


@dataclass(frozen=True)
class VisionSample:
    sample_rank: int
    asset_key: str
    cohort: str
    selection_reason: str
    url_generation: str
    original_filename: Optional[str]
    format_lane: str
    roles: tuple[str, ...]
    filename_hints: tuple[str, ...]
    article_hints: tuple[str, ...]
    album_priors: tuple[str, ...]
    source_url_count: int
    source_url: str


@dataclass(frozen=True)
class DecodedSource:
    image: Any
    decoded_format: str
    width: int
    height: int


@dataclass(frozen=True)
class PreparedDerivative:
    lane: str
    max_long_edge: int
    width: int
    height: int
    raw_patch_count: int
    encoded_bytes: bytes
    encoded_sha256: str
    pixel_sha256: str


def _stable_order(asset_key: str) -> bytes:
    return hashlib.sha256((SELECTION_VERSION + "\0" + asset_key).encode("utf-8")).digest()


def _object_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')", (name,)
    ).fetchone() is not None


def _chunks(values: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _load_semantic_context(
    source_db: Path, candidates: Sequence[SampleAsset]
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    article_hints: dict[str, set[str]] = defaultdict(set)
    album_priors: dict[str, set[str]] = defaultdict(set)
    keys = [item.asset_key for item in candidates]
    with open_source_readonly(source_db) as conn:
        has_content = _object_exists(conn, "v_article_content_hints")
        has_tags = all(
            _object_exists(conn, name)
            for name in ("article_tags", "source_tags", "article_image_occurrences")
        )
        for batch in _chunks(keys):
            marks = ",".join("?" for _ in batch)
            if has_content:
                for row in conn.execute(
                    """
                    SELECT DISTINCT aio.asset_key, LOWER(ach.content_hint) AS content_hint
                    FROM article_image_occurrences aio
                    JOIN v_article_content_hints ach ON ach.article_id=aio.article_id
                    WHERE aio.asset_key IN (%s)
                    """
                    % marks,
                    batch,
                ):
                    if row[1]:
                        article_hints[str(row[0])].add(str(row[1]))
            if has_tags:
                for row in conn.execute(
                    """
                    SELECT DISTINCT aio.asset_key, LOWER(st.album_slug) AS album_slug
                    FROM article_image_occurrences aio
                    JOIN article_tags atg ON atg.article_id=aio.article_id
                    JOIN source_tags st ON st.tag_slug=atg.tag_slug
                    WHERE aio.asset_key IN (%s) AND st.album_slug IS NOT NULL
                    """
                    % marks,
                    batch,
                ):
                    if row[1]:
                        album_priors[str(row[0])].add(str(row[1]))
    return article_hints, album_priors


def _semantic_cohort(
    item: SampleAsset, article_hints: set[str], album_priors: set[str]
) -> str:
    filename_hints = {value.casefold() for value in item.hints}
    if item.format_lane == "convertible":
        return "convertible_document"
    if filename_hints & (_DRAWING_HINTS | _SPECIAL_HINTS | {"detail", "interior", "aerial"}):
        return "filename_media_hint"
    if article_hints & _DRAWING_HINTS:
        return "article_drawing_prior"
    if article_hints & _SPECIAL_HINTS:
        return "special_media_prior"
    if album_priors & {"private-interiors", "public-interiors"}:
        return "interior_prior"
    if album_priors & {"materiality", "elements"}:
        return "material_element_prior"
    if item.source_url_count > 1:
        return "multi_url_edge"
    if "cover" in item.roles:
        return "cover_plain"
    if item.url_generation != "cloudinary_public_id":
        return "legacy_plain"
    if item.roles == ("gallery",):
        return "gallery_plain"
    return "modern_plain"


def select_vision_sample(source_db: Path, limit: int) -> list[VisionSample]:
    """Select stable semantic strata; every smaller limit prefixes a larger one."""
    if limit < 1:
        raise ValueError("limit must be positive")
    with open_source_readonly(source_db) as conn:
        inventory_count = int(conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0])
    pool_limit = min(inventory_count, MAX_CANDIDATE_POOL)
    candidates = select_stratified_sample(source_db, pool_limit)
    supported = [item for item in candidates if item.format_lane != "hard_skip"]
    if len(supported) < limit:
        raise RuntimeError("source contains fewer supported image assets than requested")

    article_by_key, albums_by_key = _load_semantic_context(source_db, supported)
    buckets: dict[str, list[SampleAsset]] = defaultdict(list)
    for item in supported:
        cohort = _semantic_cohort(
            item,
            article_by_key.get(item.asset_key, set()),
            albums_by_key.get(item.asset_key, set()),
        )
        buckets[cohort].append(item)
    for values in buckets.values():
        values.sort(key=lambda item: _stable_order(item.asset_key))

    selected: list[tuple[str, SampleAsset]] = []
    offsets: dict[str, int] = defaultdict(int)
    while len(selected) < limit:
        progressed = False
        for cohort in _COHORT_CYCLE:
            offset = offsets[cohort]
            values = buckets.get(cohort, [])
            if offset >= len(values):
                continue
            selected.append((cohort, values[offset]))
            offsets[cohort] += 1
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break

    if len(selected) < limit:
        chosen = {item.asset_key for _, item in selected}
        fill = sorted(
            (item for item in supported if item.asset_key not in chosen),
            key=lambda item: _stable_order(item.asset_key),
        )
        for item in fill[: limit - len(selected)]:
            selected.append(("deterministic_fill", item))
    if len(selected) != limit:
        raise RuntimeError("could not construct requested semantic sample")

    result: list[VisionSample] = []
    for rank, (cohort, item) in enumerate(selected, 1):
        result.append(
            VisionSample(
                sample_rank=rank,
                asset_key=item.asset_key,
                cohort=cohort,
                selection_reason="cycle_" + cohort,
                url_generation=item.url_generation,
                original_filename=item.original_filename,
                format_lane=item.format_lane,
                roles=item.roles,
                filename_hints=item.hints,
                article_hints=tuple(sorted(article_by_key.get(item.asset_key, set()))),
                album_priors=tuple(sorted(albums_by_key.get(item.asset_key, set()))),
                source_url_count=item.source_url_count,
                source_url=item.primary_source_url,
            )
        )
    return result


def sample_manifest_sha256(sample: Sequence[VisionSample]) -> str:
    return hashlib.sha256(
        canonical_json(
            [
                {
                    "sample_rank": item.sample_rank,
                    "asset_key": item.asset_key,
                    "cohort": item.cohort,
                    "selection_reason": item.selection_reason,
                    "url_generation": item.url_generation,
                    "original_filename": item.original_filename,
                    "format_lane": item.format_lane,
                    "roles": item.roles,
                    "filename_hints": item.filename_hints,
                    "article_hints": item.article_hints,
                    "album_priors": item.album_priors,
                    "source_url_count": item.source_url_count,
                    "source_url": item.source_url,
                }
                for item in sample
            ]
        ).encode("utf-8")
    ).hexdigest()


def inference_asset_id(sample_rank: int) -> str:
    """Return an opaque model-facing ID that cannot leak filename hints."""
    if sample_rank < 1:
        raise ValueError("sample_rank must be positive")
    return "sample-%04d" % sample_rank


def source_profile(sample: VisionSample) -> str:
    name = (sample.original_filename or sample.source_url).casefold().split("?", 1)[0]
    return PDF_SOURCE_PROFILE if name.endswith(".pdf") else SOURCE_PROFILE


def source_request_url(sample: VisionSample) -> str:
    return fixed_derivative_url(sample.source_url, source_profile(sample))


def decode_source(raw: bytes) -> DecodedSource:
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            decoded_format = str(opened.format or "").upper()
            image = ImageOps.exif_transpose(opened)
            if getattr(image, "is_animated", False):
                image.seek(0)
            image.load()
            if "A" in image.getbands():
                rgba = image.convert("RGBA")
                white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                white.alpha_composite(rgba)
                rgb = white.convert("RGB")
            else:
                rgb = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise FetchFailure("decode", str(exc)) from exc
    if rgb.width <= 0 or rgb.height <= 0:
        raise FetchFailure("invalid_dimensions", "decoded image has non-positive dimensions")
    if rgb.width > 2048 or rgb.height > 2048:
        raise FetchFailure(
            "transform_not_applied",
            "decoded dimensions exceed max2048 source derivative: %dx%d" % rgb.size,
        )
    return DecodedSource(rgb, decoded_format, rgb.width, rgb.height)


def _pixel_sha256(image: Any) -> str:
    return hashlib.sha256(
        b"RGB\0" + struct.pack(">II", image.width, image.height) + image.tobytes()
    ).hexdigest()


def prepare_derivative(decoded: DecodedSource, lane: str, max_long_edge: int) -> PreparedDerivative:
    from PIL import Image

    if max_long_edge < 1:
        raise ValueError("max_long_edge must be positive")
    image = decoded.image.copy()
    if max(image.size) > max_long_edge:
        image.thumbnail((max_long_edge, max_long_edge), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=92,
        subsampling=0,
        optimize=False,
        progressive=False,
    )
    encoded = output.getvalue()
    with Image.open(io.BytesIO(encoded)) as delivered:
        delivered.load()
        delivered_rgb = delivered.convert("RGB")
    return PreparedDerivative(
        lane=lane,
        max_long_edge=max_long_edge,
        width=image.width,
        height=image.height,
        raw_patch_count=math.ceil(image.width / 32) * math.ceil(image.height / 32),
        encoded_bytes=encoded,
        encoded_sha256=hashlib.sha256(encoded).hexdigest(),
        pixel_sha256=_pixel_sha256(delivered_rgb),
    )


def prepare_lanes(decoded: DecodedSource) -> list[PreparedDerivative]:
    return [prepare_derivative(decoded, lane, edge) for lane, edge in LANES]


def derive_legacy_type(medium: str, view: str) -> str:
    if medium == "drawing" or view in {
        "plan",
        "section",
        "elevation",
        "axonometric",
        "site_plan",
        "diagram",
    }:
        return "drawing"
    if view in {"exterior", "interior", "aerial", "detail"}:
        return view
    return "unknown"


def _controlled_list(value: Any, allowed: frozenset[str], field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("%s must be a list" % field)
    normalized: list[str] = []
    for item in value:
        token = str(item).strip().casefold()
        if token not in allowed:
            raise ValueError("%s contains unsupported value: %s" % (field, token))
        if token not in normalized:
            normalized.append(token)
    return tuple(normalized)


def normalize_vision_result(row: Mapping[str, Any], expected_asset_key: str) -> dict[str, Any]:
    asset_key = str(row.get("asset_id") or "")
    if asset_key != expected_asset_key:
        raise ValueError("vision result asset_id does not match attachment order")
    medium = str(row.get("medium") or "").strip().casefold()
    view = str(row.get("view") or "").strip().casefold()
    if medium not in MEDIA_VALUES:
        raise ValueError("unsupported medium: %s" % medium)
    if view not in VIEW_VALUES:
        raise ValueError("unsupported view: %s" % view)
    confidence = row.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be numeric")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between zero and one")
    needs_detail = row.get("needs_detail_review")
    if not isinstance(needs_detail, bool):
        raise ValueError("needs_detail_review must be boolean")
    evidence = str(row.get("evidence") or "").strip()
    if not evidence or len(evidence) > 500:
        raise ValueError("evidence must contain 1-500 characters")
    return {
        "inference_asset_id": asset_key,
        "medium": medium,
        "view": view,
        "legacy_type": derive_legacy_type(medium, view),
        "visible_materials": _controlled_list(
            row.get("visible_materials"), MATERIAL_VALUES, "visible_materials"
        ),
        "visible_elements": _controlled_list(
            row.get("visible_elements"), ELEMENT_VALUES, "visible_elements"
        ),
        "needs_detail_review": needs_detail,
        "confidence": confidence,
        "evidence": evidence,
    }


def normalize_vision_batch(
    payload: Any, expected_asset_keys: Sequence[str]
) -> list[dict[str, Any]]:
    if not isinstance(payload, (list, tuple)):
        raise ValueError("vision response must be a JSON array")
    if len(payload) != len(expected_asset_keys):
        raise ValueError("vision response count does not match attachment count")
    normalized: list[dict[str, Any]] = []
    for row, asset_key in zip(payload, expected_asset_keys):
        if not isinstance(row, Mapping):
            raise ValueError("every vision result must be a JSON object")
        normalized.append(normalize_vision_result(row, asset_key))
    return normalized


def compose_prompt(asset_keys: Sequence[str]) -> str:
    ordered = "\n".join(
        "%d. %s" % (index, asset_key) for index, asset_key in enumerate(asset_keys, 1)
    )
    return f"""Classify each attached architecture image using only visible evidence.

The attachments are in this exact order:
{ordered}

Return one JSON object per attachment, in the same order, inside
{{"results": [...]}}.
Required fields:
- asset_id: exact identifier shown above
- medium: one of {sorted(MEDIA_VALUES)}
- view: one of {sorted(VIEW_VALUES)}
- visible_materials: zero or more of {sorted(MATERIAL_VALUES)}
- visible_elements: zero or more of {sorted(ELEMENT_VALUES)}
- needs_detail_review: true only when higher-resolution crops could materially change the answer
- confidence: 0.0 to 1.0 based only on visible evidence
- evidence: one short sentence describing the visible basis

Do not infer facts from filenames, project knowledge, or likely building type.
Use unknown and empty lists instead of guessing. Output JSON only."""


_VISION_RESULT_SCHEMA: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "asset_id",
            "medium",
            "view",
            "visible_materials",
            "visible_elements",
            "needs_detail_review",
            "confidence",
            "evidence",
        ],
        "properties": {
            "asset_id": {"type": "string"},
            "medium": {"type": "string", "enum": sorted(MEDIA_VALUES)},
            "view": {"type": "string", "enum": sorted(VIEW_VALUES)},
            "visible_materials": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(MATERIAL_VALUES)},
            },
            "visible_elements": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(ELEMENT_VALUES)},
            },
            "needs_detail_review": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "string", "minLength": 1, "maxLength": 500},
        },
}

VISION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": _VISION_RESULT_SCHEMA,
        }
    },
}


SIDECAR_SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE benchmark_run(
  run_id INTEGER PRIMARY KEY CHECK(run_id=1),
  status TEXT NOT NULL CHECK(status IN ('running','complete','failed_validation','failed')),
  benchmark_version TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  selection_version TEXT NOT NULL,
  source_derivative_version TEXT NOT NULL,
  local_derivative_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  source_db_path TEXT NOT NULL,
  source_sha256_before TEXT NOT NULL,
  source_sha256_after TEXT,
  sample_limit INTEGER NOT NULL,
  batch_size INTEGER NOT NULL,
  sample_manifest_sha256 TEXT NOT NULL,
  lanes_json TEXT NOT NULL CHECK(json_valid(lanes_json)),
  model TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  service_tier TEXT NOT NULL,
  runtime_version TEXT NOT NULL,
  cli_version TEXT,
  image_detail TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  metrics_json TEXT CHECK(metrics_json IS NULL OR json_valid(metrics_json)),
  logical_sha256 TEXT,
  error TEXT
);

CREATE TABLE sample_assets(
  sample_rank INTEGER PRIMARY KEY,
  asset_key TEXT NOT NULL UNIQUE,
  cohort TEXT NOT NULL,
  selection_reason TEXT NOT NULL,
  url_generation TEXT NOT NULL,
  original_filename TEXT,
  format_lane TEXT NOT NULL,
  roles_json TEXT NOT NULL CHECK(json_valid(roles_json)),
  filename_hints_json TEXT NOT NULL CHECK(json_valid(filename_hints_json)),
  article_hints_json TEXT NOT NULL CHECK(json_valid(article_hints_json)),
  album_priors_json TEXT NOT NULL CHECK(json_valid(album_priors_json)),
  source_url_count INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  request_url TEXT NOT NULL
);

CREATE TABLE fetch_results(
  asset_key TEXT PRIMARY KEY REFERENCES sample_assets(asset_key),
  status TEXT NOT NULL CHECK(status IN ('success','failed')),
  response_sha256 TEXT,
  response_bytes INTEGER,
  response_mime TEXT,
  decoded_format TEXT,
  width INTEGER,
  height INTEGER,
  elapsed_ms INTEGER NOT NULL,
  error_kind TEXT,
  error_message TEXT,
  CHECK((status='success' AND length(response_sha256)=64 AND response_bytes>0
         AND decoded_format IS NOT NULL AND width>0 AND height>0)
        OR (status='failed' AND error_kind IS NOT NULL))
);

CREATE TABLE derived_inputs(
  asset_key TEXT NOT NULL REFERENCES sample_assets(asset_key),
  lane TEXT NOT NULL,
  max_long_edge INTEGER NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  raw_patch_count INTEGER NOT NULL,
  encoded_bytes INTEGER NOT NULL,
  encoded_sha256 TEXT NOT NULL CHECK(length(encoded_sha256)=64),
  pixel_sha256 TEXT NOT NULL CHECK(length(pixel_sha256)=64),
  PRIMARY KEY(asset_key,lane)
);

CREATE TABLE vision_attempts(
  attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  lane TEXT NOT NULL,
  batch_no INTEGER NOT NULL,
  asset_keys_json TEXT NOT NULL CHECK(json_valid(asset_keys_json)),
  status TEXT NOT NULL CHECK(status IN ('success','failed')),
  model TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  service_tier TEXT NOT NULL,
  runtime_version TEXT NOT NULL,
  cli_version TEXT,
  codex_bin TEXT NOT NULL,
  image_detail TEXT NOT NULL,
  sandbox TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256)=64),
  output_schema_sha256 TEXT NOT NULL CHECK(length(output_schema_sha256)=64),
  elapsed_ms INTEGER NOT NULL,
  input_tokens INTEGER,
  cached_input_tokens INTEGER,
  output_tokens INTEGER,
  raw_events_sha256 TEXT,
  stdout_excerpt TEXT,
  stderr_excerpt TEXT,
  non_json_lines_json TEXT NOT NULL CHECK(json_valid(non_json_lines_json)),
  error_kind TEXT,
  error_message TEXT
);

CREATE TABLE vision_results(
  asset_key TEXT NOT NULL REFERENCES sample_assets(asset_key),
  lane TEXT NOT NULL,
  medium TEXT NOT NULL,
  view TEXT NOT NULL,
  legacy_type TEXT NOT NULL,
  visible_materials_json TEXT NOT NULL CHECK(json_valid(visible_materials_json)),
  visible_elements_json TEXT NOT NULL CHECK(json_valid(visible_elements_json)),
  needs_detail_review INTEGER NOT NULL CHECK(needs_detail_review IN (0,1)),
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  evidence TEXT NOT NULL,
  response_json TEXT NOT NULL CHECK(json_valid(response_json)),
  PRIMARY KEY(asset_key,lane)
);

CREATE TABLE gold_labels(
  asset_key TEXT PRIMARY KEY REFERENCES sample_assets(asset_key),
  medium TEXT NOT NULL,
  view TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK(review_status IN ('confirmed','uncertain','excluded')),
  reviewer TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE validations(
  validation_name TEXT PRIMARY KEY,
  severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
  passed INTEGER NOT NULL CHECK(passed IN (0,1)),
  expected TEXT,
  actual TEXT NOT NULL,
  detail TEXT
);

CREATE INDEX idx_sample_cohort ON sample_assets(cohort,sample_rank);
CREATE INDEX idx_vision_results_lane ON vision_results(lane,medium,view);
"""


def initialize_sidecar(
    conn: sqlite3.Connection,
    *,
    sample: Sequence[VisionSample],
    source_db_path: Path,
    source_sha256: str,
    batch_size: int,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: Optional[str],
    started_at: str,
) -> None:
    conn.executescript(SIDECAR_SCHEMA)
    conn.execute(
        """
        INSERT INTO benchmark_run(
          run_id,status,benchmark_version,schema_version,selection_version,
          source_derivative_version,local_derivative_version,prompt_version,
          source_db_path,source_sha256_before,sample_limit,batch_size,sample_manifest_sha256,
          lanes_json,model,reasoning,service_tier,runtime_version,cli_version,
          image_detail,started_at
        ) VALUES(1,'running',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            BENCHMARK_VERSION,
            SCHEMA_VERSION,
            SELECTION_VERSION,
            SOURCE_DERIVATIVE_VERSION,
            LOCAL_DERIVATIVE_VERSION,
            PROMPT_VERSION,
            str(source_db_path),
            source_sha256,
            len(sample),
            batch_size,
            sample_manifest_sha256(sample),
            canonical_json(dict(LANES)),
            model,
            reasoning,
            service_tier,
            RUNTIME_VERSION,
            cli_version,
            CLI_IMAGE_DETAIL,
            started_at,
        ),
    )
    conn.executemany(
        """
        INSERT INTO sample_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                item.sample_rank,
                item.asset_key,
                item.cohort,
                item.selection_reason,
                item.url_generation,
                item.original_filename,
                item.format_lane,
                canonical_json(item.roles),
                canonical_json(item.filename_hints),
                canonical_json(item.article_hints),
                canonical_json(item.album_priors),
                item.source_url_count,
                item.source_url,
                source_request_url(item),
            )
            for item in sample
        ],
    )
    conn.commit()


def logical_sha256(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    run = conn.execute(
        """
        SELECT benchmark_version,schema_version,selection_version,
               source_derivative_version,local_derivative_version,prompt_version,
               source_sha256_before,sample_limit,batch_size,sample_manifest_sha256,lanes_json,
               model,reasoning,service_tier,runtime_version,
               COALESCE(cli_version,''),image_detail
        FROM benchmark_run WHERE run_id=1
        """
    ).fetchone()
    digest.update(b"benchmark_run\0")
    digest.update(canonical_json(list(run)).encode("utf-8"))
    digest.update(b"\n")
    for table, order_by in (
        ("sample_assets", "sample_rank"),
        ("fetch_results", "asset_key"),
        ("derived_inputs", "asset_key,lane"),
        ("vision_results", "asset_key,lane"),
        ("vision_attempts", "attempt_id"),
        ("gold_labels", "asset_key"),
        ("validations", "validation_name"),
    ):
        digest.update((table + "\0").encode("utf-8"))
        for row in conn.execute("SELECT * FROM %s ORDER BY %s" % (table, order_by)):
            digest.update(canonical_json(list(row)).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _existing_lanes(conn: sqlite3.Connection, asset_key: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT lane FROM vision_results WHERE asset_key=?", (asset_key,)
        )
    }


def _write_fetch_success(
    conn: sqlite3.Connection,
    *,
    asset_key: str,
    payload: FetchPayload,
    decoded: DecodedSource,
    elapsed_ms: int,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO fetch_results(
          asset_key,status,response_sha256,response_bytes,response_mime,
          decoded_format,width,height,elapsed_ms,error_kind,error_message
        ) VALUES(?,'success',?,?,?,?,?,?,?,NULL,NULL)
        """,
        (
            asset_key,
            hashlib.sha256(payload.raw).hexdigest(),
            len(payload.raw),
            payload.mime_type,
            decoded.decoded_format,
            decoded.width,
            decoded.height,
            elapsed_ms,
        ),
    )


def _write_fetch_failure(
    conn: sqlite3.Connection,
    *,
    asset_key: str,
    elapsed_ms: int,
    error_kind: str,
    error_message: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO fetch_results(
          asset_key,status,response_sha256,response_bytes,response_mime,
          decoded_format,width,height,elapsed_ms,error_kind,error_message
        ) VALUES(?,'failed',NULL,NULL,NULL,NULL,NULL,NULL,?,?,?)
        """,
        (asset_key, elapsed_ms, error_kind, error_message[:1000]),
    )


def _write_derivative(
    conn: sqlite3.Connection, asset_key: str, derivative: PreparedDerivative
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO derived_inputs(
          asset_key,lane,max_long_edge,width,height,raw_patch_count,encoded_bytes,
          encoded_sha256,pixel_sha256
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            asset_key,
            derivative.lane,
            derivative.max_long_edge,
            derivative.width,
            derivative.height,
            derivative.raw_patch_count,
            len(derivative.encoded_bytes),
            derivative.encoded_sha256,
            derivative.pixel_sha256,
        ),
    )


def _assert_resume_input_unchanged(
    conn: sqlite3.Connection,
    *,
    asset_key: str,
    payload: FetchPayload,
    prepared: Mapping[str, PreparedDerivative],
) -> bool:
    """Validate a refetch against retained evidence before resuming a missing lane."""
    fetch_row = conn.execute(
        "SELECT status,response_sha256 FROM fetch_results WHERE asset_key=?",
        (asset_key,),
    ).fetchone()
    if fetch_row is None or fetch_row[0] != "success":
        return False

    response_sha = hashlib.sha256(payload.raw).hexdigest()
    if fetch_row[1] != response_sha:
        raise RuntimeError(
            "resume source response changed for %s: expected %s, got %s"
            % (asset_key, fetch_row[1], response_sha)
        )

    prior = {
        str(row[0]): row[1:]
        for row in conn.execute(
            """
            SELECT lane,max_long_edge,width,height,encoded_bytes,encoded_sha256,pixel_sha256
            FROM derived_inputs WHERE asset_key=?
            """,
            (asset_key,),
        )
    }
    for lane, derivative in prepared.items():
        actual = (
            derivative.max_long_edge,
            derivative.width,
            derivative.height,
            len(derivative.encoded_bytes),
            derivative.encoded_sha256,
            derivative.pixel_sha256,
        )
        if prior.get(lane) != actual:
            raise RuntimeError(
                "resume derivative changed for %s/%s" % (asset_key, lane)
            )
    return True


def _write_attempt(
    conn: sqlite3.Connection,
    *,
    lane: str,
    batch_no: int,
    asset_keys: Sequence[str],
    result: VisionRuntimeResult,
    status: str,
    error_kind: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    usage = result.usage
    cli_version = conn.execute(
        "SELECT cli_version FROM benchmark_run WHERE run_id=1"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO vision_attempts(
          lane,batch_no,asset_keys_json,status,model,reasoning,service_tier,
          runtime_version,cli_version,codex_bin,image_detail,sandbox,prompt_sha256,
          output_schema_sha256,elapsed_ms,input_tokens,cached_input_tokens,output_tokens,
          raw_events_sha256,stdout_excerpt,stderr_excerpt,non_json_lines_json,
          error_kind,error_message
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            lane,
            batch_no,
            canonical_json(list(asset_keys)),
            status,
            result.provenance.model,
            result.provenance.reasoning,
            result.provenance.service_tier,
            result.provenance.runtime_version,
            cli_version,
            result.provenance.codex_bin,
            result.provenance.cli_image_detail,
            result.provenance.sandbox,
            result.provenance.prompt_sha256,
            result.provenance.output_schema_sha256,
            round(result.elapsed_seconds * 1000),
            usage.input_tokens if usage else None,
            usage.cached_input_tokens if usage else None,
            usage.output_tokens if usage else None,
            hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
            if result.stdout
            else None,
            result.stdout[-8000:] if result.stdout else None,
            result.stderr[-8000:] if result.stderr else None,
            canonical_json(list(result.non_json_stdout_lines)),
            error_kind or result.error_kind,
            (error_message or result.error_message or "")[:1000] or None,
        ),
    )


def _write_vision_results(
    conn: sqlite3.Connection, lane: str, rows: Sequence[Mapping[str, Any]]
) -> None:
    conn.executemany(
        """
        INSERT OR REPLACE INTO vision_results(
          asset_key,lane,medium,view,legacy_type,visible_materials_json,
          visible_elements_json,needs_detail_review,confidence,evidence,response_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                row["asset_key"],
                lane,
                row["medium"],
                row["view"],
                row["legacy_type"],
                canonical_json(row["visible_materials"]),
                canonical_json(row["visible_elements"]),
                int(row["needs_detail_review"]),
                row["confidence"],
                row["evidence"],
                canonical_json(row),
            )
            for row in rows
        ],
    )


def _validate_resume(
    conn: sqlite3.Connection,
    *,
    source_db: Path,
    source_sha256: str,
    sample: Sequence[VisionSample],
    batch_size: int,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: Optional[str],
) -> None:
    row = conn.execute("SELECT * FROM benchmark_run WHERE run_id=1").fetchone()
    if row is None:
        raise RuntimeError("partial sidecar has no benchmark_run row")
    columns = [item[1] for item in conn.execute("PRAGMA table_info(benchmark_run)")]
    run = dict(zip(columns, row))
    expected = {
        "status": "running",
        "benchmark_version": BENCHMARK_VERSION,
        "schema_version": SCHEMA_VERSION,
        "selection_version": SELECTION_VERSION,
        "source_derivative_version": SOURCE_DERIVATIVE_VERSION,
        "local_derivative_version": LOCAL_DERIVATIVE_VERSION,
        "prompt_version": PROMPT_VERSION,
        "source_db_path": str(source_db),
        "source_sha256_before": source_sha256,
        "sample_limit": len(sample),
        "batch_size": batch_size,
        "sample_manifest_sha256": sample_manifest_sha256(sample),
        "lanes_json": canonical_json(dict(LANES)),
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "runtime_version": RUNTIME_VERSION,
        "cli_version": cli_version,
        "image_detail": CLI_IMAGE_DETAIL,
    }
    mismatches = {
        key: {"actual": run.get(key), "expected": value}
        for key, value in expected.items()
        if run.get(key) != value
    }
    if mismatches:
        raise RuntimeError("resume contract mismatch: %s" % canonical_json(mismatches))


def _metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    lane_rows = {
        str(row[0]): {
            "results": int(row[1]),
            "avg_confidence": round(float(row[2] or 0), 4),
            "detail_review": int(row[3] or 0),
        }
        for row in conn.execute(
            """
            SELECT lane,COUNT(*),AVG(confidence),SUM(needs_detail_review)
            FROM vision_results GROUP BY lane ORDER BY lane
            """
        )
    }
    usage = conn.execute(
        """
        SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(cached_input_tokens),0),
               COALESCE(SUM(output_tokens),0),COALESCE(SUM(elapsed_ms),0),COUNT(*)
        FROM vision_attempts
        """
    ).fetchone()
    usage_by_lane = {
        str(row[0]): {
            "attempts": int(row[1]),
            "successful_attempts": int(row[2] or 0),
            "input_tokens": int(row[3] or 0),
            "cached_input_tokens": int(row[4] or 0),
            "output_tokens": int(row[5] or 0),
            "elapsed_ms": int(row[6] or 0),
        }
        for row in conn.execute(
            """
            SELECT lane,COUNT(*),SUM(status='success'),
                   COALESCE(SUM(input_tokens),0),
                   COALESCE(SUM(cached_input_tokens),0),
                   COALESCE(SUM(output_tokens),0),
                   COALESCE(SUM(elapsed_ms),0)
            FROM vision_attempts GROUP BY lane ORDER BY lane
            """
        )
    }
    agreement = conn.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN a.legacy_type=b.legacy_type THEN 1 ELSE 0 END),
               SUM(CASE WHEN a.medium=b.medium AND a.view=b.view THEN 1 ELSE 0 END)
        FROM vision_results a
        JOIN vision_results b ON b.asset_key=a.asset_key
        WHERE a.lane='long1024' AND b.lane='long2048'
        """
    ).fetchone()
    agreement_by_input = {
        ("identical" if int(row[0]) else "distinct"): {
            "paired_assets": int(row[1]),
            "legacy_type_agreement": int(row[2] or 0),
            "medium_view_agreement": int(row[3] or 0),
        }
        for row in conn.execute(
            """
            SELECT CASE WHEN d1.pixel_sha256=d2.pixel_sha256
                              AND d1.width=d2.width AND d1.height=d2.height
                        THEN 1 ELSE 0 END AS identical_input,
                   COUNT(*),
                   SUM(a.legacy_type=b.legacy_type),
                   SUM(a.medium=b.medium AND a.view=b.view)
            FROM vision_results a
            JOIN vision_results b ON b.asset_key=a.asset_key
            JOIN derived_inputs d1 ON d1.asset_key=a.asset_key AND d1.lane=a.lane
            JOIN derived_inputs d2 ON d2.asset_key=b.asset_key AND d2.lane=b.lane
            WHERE a.lane='long1024' AND b.lane='long2048'
            GROUP BY identical_input ORDER BY identical_input
            """
        )
    }
    return {
        "sample_assets": int(conn.execute("SELECT COUNT(*) FROM sample_assets").fetchone()[0]),
        "fetch_success": int(
            conn.execute("SELECT COUNT(*) FROM fetch_results WHERE status='success'").fetchone()[0]
        ),
        "fetch_failed": int(
            conn.execute("SELECT COUNT(*) FROM fetch_results WHERE status='failed'").fetchone()[0]
        ),
        "derived_inputs": int(conn.execute("SELECT COUNT(*) FROM derived_inputs").fetchone()[0]),
        "vision_results": int(conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0]),
        "vision_attempts": int(usage[4]),
        "input_tokens": int(usage[0]),
        "cached_input_tokens": int(usage[1]),
        "output_tokens": int(usage[2]),
        "model_elapsed_ms": int(usage[3]),
        "usage_by_lane": usage_by_lane,
        "lanes": lane_rows,
        "paired_assets": int(agreement[0] or 0),
        "legacy_type_agreement": int(agreement[1] or 0),
        "medium_view_agreement": int(agreement[2] or 0),
        "agreement_by_input": agreement_by_input,
        "gold_labels": int(conn.execute("SELECT COUNT(*) FROM gold_labels").fetchone()[0]),
    }


def validate_sidecar(
    conn: sqlite3.Connection, *, source_sha256_after: str
) -> list[dict[str, Any]]:
    run = conn.execute("SELECT * FROM benchmark_run WHERE run_id=1").fetchone()
    if run is None:
        raise RuntimeError("sidecar has no benchmark run")
    sample_count = int(conn.execute("SELECT COUNT(*) FROM sample_assets").fetchone()[0])
    expected_sample = int(conn.execute("SELECT sample_limit FROM benchmark_run").fetchone()[0])
    fetch_count = int(conn.execute("SELECT COUNT(*) FROM fetch_results").fetchone()[0])
    success_count = int(
        conn.execute("SELECT COUNT(*) FROM fetch_results WHERE status='success'").fetchone()[0]
    )
    expected_lane_rows = success_count * len(LANES)
    derived_count = int(conn.execute("SELECT COUNT(*) FROM derived_inputs").fetchone()[0])
    result_count = int(conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0])
    source_before = str(
        conn.execute("SELECT source_sha256_before FROM benchmark_run").fetchone()[0]
    )
    integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    fk_count = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    rows = [
        {
            "validation_name": "sample_count",
            "severity": "error",
            "passed": sample_count == expected_sample,
            "expected": str(expected_sample),
            "actual": str(sample_count),
            "detail": None,
        },
        {
            "validation_name": "fetch_accounting",
            "severity": "error",
            "passed": fetch_count == expected_sample,
            "expected": str(expected_sample),
            "actual": str(fetch_count),
            "detail": None,
        },
        {
            "validation_name": "fetch_success",
            "severity": "error",
            "passed": success_count == expected_sample,
            "expected": str(expected_sample),
            "actual": str(success_count),
            "detail": "A frozen benchmark manifest cannot silently replace failed assets.",
        },
        {
            "validation_name": "derived_lane_accounting",
            "severity": "error",
            "passed": derived_count == expected_lane_rows,
            "expected": str(expected_lane_rows),
            "actual": str(derived_count),
            "detail": None,
        },
        {
            "validation_name": "vision_result_accounting",
            "severity": "error",
            "passed": result_count == expected_lane_rows,
            "expected": str(expected_lane_rows),
            "actual": str(result_count),
            "detail": None,
        },
        {
            "validation_name": "source_immutable",
            "severity": "error",
            "passed": source_before == source_sha256_after,
            "expected": source_before,
            "actual": source_sha256_after,
            "detail": None,
        },
        {
            "validation_name": "sqlite_quick_check",
            "severity": "error",
            "passed": integrity == "ok",
            "expected": "ok",
            "actual": integrity,
            "detail": None,
        },
        {
            "validation_name": "foreign_keys",
            "severity": "error",
            "passed": fk_count == 0,
            "expected": "0",
            "actual": str(fk_count),
            "detail": None,
        },
        {
            "validation_name": "gold_quality_gate_deferred",
            "severity": "warning",
            "passed": conn.execute("SELECT COUNT(*) FROM gold_labels").fetchone()[0]
            == expected_sample,
            "expected": str(expected_sample),
            "actual": str(conn.execute("SELECT COUNT(*) FROM gold_labels").fetchone()[0]),
            "detail": "Resolution accuracy cannot be selected until human gold labels are frozen.",
        },
    ]
    return rows


def _insert_validations(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> None:
    conn.execute("DELETE FROM validations")
    conn.executemany(
        "INSERT INTO validations VALUES(?,?,?,?,?,?)",
        [
            (
                row["validation_name"],
                row["severity"],
                int(bool(row["passed"])),
                row.get("expected"),
                row["actual"],
                row.get("detail"),
            )
            for row in rows
        ],
    )


def render_report(conn: sqlite3.Connection, *, artifact_path: Path) -> str:
    metrics = _metrics(conn)
    run = conn.execute(
        "SELECT * FROM benchmark_run WHERE run_id=1"
    ).fetchone()
    columns = [item[1] for item in conn.execute("PRAGMA table_info(benchmark_run)")]
    run_row = dict(zip(columns, run))
    paired = metrics["paired_assets"]
    legacy_pct = (
        100.0 * metrics["legacy_type_agreement"] / paired if paired else 0.0
    )
    exact_pct = 100.0 * metrics["medium_view_agreement"] / paired if paired else 0.0
    total_tokens = metrics["input_tokens"] + metrics["output_tokens"]
    per_asset = total_tokens / metrics["sample_assets"] if metrics["sample_assets"] else 0
    projected_n100 = round(per_asset * 100)
    lines = [
        "# Divisare Vision resolution benchmark",
        "",
        "## Contract",
        "",
        "- Artifact: `%s`" % artifact_path,
        "- Benchmark: `%s`" % run_row["benchmark_version"],
        "- Source SHA before: `%s`" % run_row["source_sha256_before"],
        "- Source SHA after: `%s`" % run_row["source_sha256_after"],
        "- Model: `%s`" % run_row["model"],
        "- CLI image detail: `%s`" % run_row["image_detail"],
        "- Sample manifest SHA: `%s`" % run_row["sample_manifest_sha256"],
        "- Logical SHA: `%s`" % run_row["logical_sha256"],
        "",
        "## Result",
        "",
        "- Assets: `%d`" % metrics["sample_assets"],
        "- Fetch: `%d` success / `%d` failed"
        % (metrics["fetch_success"], metrics["fetch_failed"]),
        "- Derived inputs: `%d`" % metrics["derived_inputs"],
        "- Vision results: `%d`" % metrics["vision_results"],
        "- Vision attempts: `%d`" % metrics["vision_attempts"],
        "- Tokens: input `%d`, cached input `%d`, output `%d`, total `%d`"
        % (
            metrics["input_tokens"],
            metrics["cached_input_tokens"],
            metrics["output_tokens"],
            total_tokens,
        ),
        "- Model wall time: `%.1fs`" % (metrics["model_elapsed_ms"] / 1000),
        "- N100 token projection from this run: `%d`" % projected_n100,
        "",
        "### Lane usage",
        "",
    ]
    for lane in ("long1024", "long2048"):
        lane_usage = metrics["usage_by_lane"].get(lane, {})
        lines.append(
            "- `%s`: attempts `%d`, input `%d`, cached input `%d`, output `%d`, wall `%.1fs`"
            % (
                lane,
                lane_usage.get("attempts", 0),
                lane_usage.get("input_tokens", 0),
                lane_usage.get("cached_input_tokens", 0),
                lane_usage.get("output_tokens", 0),
                lane_usage.get("elapsed_ms", 0) / 1000,
            )
        )
    lines.extend(
        [
        "",
        "## Resolution agreement",
        "",
        "- Paired assets: `%d`" % paired,
        "- Legacy 5-type agreement: `%d/%d (%.2f%%)`"
        % (metrics["legacy_type_agreement"], paired, legacy_pct),
        "- Exact medium+view agreement: `%d/%d (%.2f%%)`"
        % (metrics["medium_view_agreement"], paired, exact_pct),
        "- Distinct derivative inputs: `%d`"
        % metrics["agreement_by_input"].get("distinct", {}).get("paired_assets", 0),
        "- Identical derivative inputs: `%d`"
        % metrics["agreement_by_input"].get("identical", {}).get("paired_assets", 0),
        "",
        "Agreement is not accuracy. Human gold labels must be frozen before selecting a resolution.",
        "",
        "## Validations",
        "",
        ]
    )
    for row in conn.execute(
        "SELECT validation_name,severity,passed,expected,actual,detail "
        "FROM validations ORDER BY validation_name"
    ):
        lines.append(
            "- `%s` [%s]: **%s** (expected `%s`, actual `%s`)%s"
            % (
                row[0],
                row[1],
                "PASS" if row[2] else "FAIL",
                row[3] or "",
                row[4],
                " - " + row[5] if row[5] else "",
            )
        )
    return "\n".join(lines) + "\n"


def _publish_pair(
    partial_db: Path, output_db: Path, partial_report: Path, report_path: Path
) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    published_db = False
    try:
        os.link(partial_db, output_db)
        published_db = True
        os.link(partial_report, report_path)
    except FileExistsError as exc:
        if published_db:
            output_db.unlink()
        raise FileExistsError("immutable output or report already exists") from exc
    partial_db.unlink()
    partial_report.unlink()


def run_benchmark(
    *,
    source_db: Path,
    output_db: Path,
    report_path: Path,
    limit: int,
    batch_size: int,
    codex_bin: Path,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cli_version: Optional[str] = None,
    resume: bool = False,
    fetcher: Callable[[str], FetchPayload] = network_fetch,
    executor: Callable[..., VisionRuntimeResult] = run_codex_vision_batch,
) -> dict[str, Any]:
    """Run the paired benchmark while keeping all image bytes batch-local."""
    if limit < 1 or batch_size < 1:
        raise ValueError("limit and batch_size must be positive")
    if limit > 10:
        raise ValueError(
            "N>10 requires a frozen, human-adjudicated gold manifest; "
            "the weak-prior selector is only authorized for N10 runtime calibration"
        )
    source_db = source_db.resolve()
    output_db = output_db.resolve()
    report_path = report_path.resolve()
    if output_db.exists():
        raise FileExistsError("immutable output already exists: %s" % output_db)
    if report_path.exists():
        raise FileExistsError("immutable report already exists: %s" % report_path)
    partial_db = output_db.with_name(output_db.name + ".partial")
    partial_report = report_path.with_name(report_path.name + ".partial")
    if partial_report.exists():
        raise FileExistsError("stale partial report exists: %s" % partial_report)

    source_sha = file_sha256(source_db)
    sample = select_vision_sample(source_db, limit)
    partial_db.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        if partial_db.exists():
            if not resume:
                raise FileExistsError(
                    "partial sidecar exists; pass resume=True: %s" % partial_db
                )
            conn = sqlite3.connect(partial_db)
            conn.execute("PRAGMA foreign_keys=ON")
            _validate_resume(
                conn,
                source_db=source_db,
                source_sha256=source_sha,
                sample=sample,
                batch_size=batch_size,
                model=model,
                reasoning=reasoning,
                service_tier=service_tier,
                cli_version=cli_version,
            )
        else:
            conn = sqlite3.connect(partial_db)
            conn.execute("PRAGMA foreign_keys=ON")
            initialize_sidecar(
                conn,
                sample=sample,
                source_db_path=source_db,
                source_sha256=source_sha,
                batch_size=batch_size,
                model=model,
                reasoning=reasoning,
                service_tier=service_tier,
                cli_version=cli_version,
                started_at=utc_now(),
            )
    except Exception:
        if conn is not None:
            conn.close()
        raise
    assert conn is not None

    schema_text = canonical_json(VISION_OUTPUT_SCHEMA)
    try:
        for batch_start in range(0, len(sample), batch_size):
            batch = sample[batch_start : batch_start + batch_size]
            needed = {
                item.asset_key: {
                    lane for lane, _edge in LANES if lane not in _existing_lanes(conn, item.asset_key)
                }
                for item in batch
            }
            if not any(needed.values()):
                continue

            derivatives: dict[str, dict[str, PreparedDerivative]] = {}
            for item in batch:
                if not needed[item.asset_key]:
                    continue
                started = time.perf_counter()
                prior_fetch_success = conn.execute(
                    "SELECT 1 FROM fetch_results WHERE asset_key=? AND status='success'",
                    (item.asset_key,),
                ).fetchone() is not None
                try:
                    payload = fetcher(source_request_url(item))
                    decoded = decode_source(payload.raw)
                    prepared = {value.lane: value for value in prepare_lanes(decoded)}
                    elapsed_ms = round((time.perf_counter() - started) * 1000)
                    retained = _assert_resume_input_unchanged(
                        conn,
                        asset_key=item.asset_key,
                        payload=payload,
                        prepared=prepared,
                    )
                    if not retained:
                        _write_fetch_success(
                            conn,
                            asset_key=item.asset_key,
                            payload=payload,
                            decoded=decoded,
                            elapsed_ms=elapsed_ms,
                        )
                        for derivative in prepared.values():
                            _write_derivative(conn, item.asset_key, derivative)
                    derivatives[item.asset_key] = prepared
                except Exception as exc:  # retained as row-level benchmark evidence
                    if prior_fetch_success:
                        raise RuntimeError(
                            "resume refetch could not reproduce retained input for %s"
                            % item.asset_key
                        ) from exc
                    elapsed_ms = round((time.perf_counter() - started) * 1000)
                    _write_fetch_failure(
                        conn,
                        asset_key=item.asset_key,
                        elapsed_ms=elapsed_ms,
                        error_kind=getattr(exc, "kind", exc.__class__.__name__),
                        error_message=str(exc),
                    )
                conn.commit()

            with tempfile.TemporaryDirectory(prefix="divisare-vision-benchmark-") as tmp:
                tmp_dir = Path(tmp)
                schema_path = tmp_dir / "output.schema.json"
                schema_path.write_text(schema_text, encoding="utf-8", newline="\n")
                for lane, _edge in LANES:
                    lane_items = [
                        item
                        for item in batch
                        if lane in needed[item.asset_key] and item.asset_key in derivatives
                    ]
                    if not lane_items:
                        continue
                    image_paths: list[Path] = []
                    inference_ids = [inference_asset_id(item.sample_rank) for item in lane_items]
                    for item in lane_items:
                        path = tmp_dir / ("%04d-%s.jpg" % (item.sample_rank, lane))
                        path.write_bytes(derivatives[item.asset_key][lane].encoded_bytes)
                        image_paths.append(path)
                    result = executor(
                        prompt=compose_prompt(inference_ids),
                        image_paths=image_paths,
                        output_schema_path=schema_path,
                        expected_asset_ids=inference_ids,
                        codex_bin=codex_bin,
                        model=model,
                        reasoning=reasoning,
                        service_tier=service_tier,
                        working_directory=tmp_dir,
                        timeout_seconds=600,
                    )
                    if not result.ok:
                        _write_attempt(
                            conn,
                            lane=lane,
                            batch_no=batch_start // batch_size + 1,
                            asset_keys=[item.asset_key for item in lane_items],
                            result=result,
                            status="failed",
                        )
                        conn.commit()
                        raise RuntimeError(
                            "Vision batch failed for %s: %s" % (lane, result.error_message)
                        )
                    try:
                        normalized = normalize_vision_batch(result.records, inference_ids)
                        normalized = [
                            {"asset_key": item.asset_key, **row}
                            for item, row in zip(lane_items, normalized)
                        ]
                    except Exception as exc:
                        _write_attempt(
                            conn,
                            lane=lane,
                            batch_no=batch_start // batch_size + 1,
                            asset_keys=[item.asset_key for item in lane_items],
                            result=result,
                            status="failed",
                            error_kind="semantic_schema",
                            error_message=str(exc),
                        )
                        conn.commit()
                        raise
                    _write_attempt(
                        conn,
                        lane=lane,
                        batch_no=batch_start // batch_size + 1,
                        asset_keys=[item.asset_key for item in lane_items],
                        result=result,
                        status="success",
                    )
                    _write_vision_results(conn, lane, normalized)
                    conn.commit()

        incomplete = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM sample_assets sa
                WHERE (SELECT COUNT(*) FROM vision_results vr
                       WHERE vr.asset_key=sa.asset_key) < ?
                """,
                (len(LANES),),
            ).fetchone()[0]
        )
        if incomplete:
            raise RuntimeError(
                "benchmark remains incomplete for %d assets; resume the same partial sidecar"
                % incomplete
            )

        source_after = file_sha256(source_db)
        validations = validate_sidecar(conn, source_sha256_after=source_after)
        _insert_validations(conn, validations)
        hard_failures = [
            row for row in validations if row["severity"] == "error" and not row["passed"]
        ]
        metrics = _metrics(conn)
        status = "failed_validation" if hard_failures else "complete"
        conn.execute(
            """
            UPDATE benchmark_run SET status=?,source_sha256_after=?,completed_at=?,
              metrics_json=?,error=? WHERE run_id=1
            """,
            (
                status,
                source_after,
                utc_now(),
                canonical_json(metrics),
                canonical_json(hard_failures) if hard_failures else None,
            ),
        )
        conn.commit()
        logical = logical_sha256(conn)
        conn.execute("UPDATE benchmark_run SET logical_sha256=? WHERE run_id=1", (logical,))
        conn.commit()
        if hard_failures:
            raise RuntimeError("benchmark validation failed: %s" % canonical_json(hard_failures))
        partial_report.parent.mkdir(parents=True, exist_ok=True)
        partial_report.write_text(
            render_report(conn, artifact_path=output_db), encoding="utf-8", newline="\n"
        )
    finally:
        conn.close()

    _publish_pair(partial_db, output_db, partial_report, report_path)
    return {
        "output_db": str(output_db),
        "report_path": str(report_path),
        "source_sha256": source_sha,
        "sample_manifest_sha256": sample_manifest_sha256(sample),
        "logical_sha256": logical,
        "metrics": metrics,
    }
