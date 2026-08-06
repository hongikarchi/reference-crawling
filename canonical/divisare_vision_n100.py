"""Gold-backed Divisare Vision N100 resolution benchmark.

This runner deliberately does not share the weak-prior N10 selector. It accepts
only the frozen, reviewed 100-image gold manifest, revalidates every
download against the probe-time content SHA, and keeps image bytes transient.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from canonical.divisare_image_smoke import (
    FetchPayload,
    canonical_json,
    file_sha256,
    network_fetch,
    utc_now,
)
from canonical.divisare_vision_benchmark import (
    LANES,
    LOCAL_DERIVATIVE_VERSION,
    SOURCE_DERIVATIVE_VERSION,
    VISION_OUTPUT_SCHEMA,
    DecodedSource,
    PreparedDerivative,
    compose_prompt,
    decode_source,
    normalize_vision_batch,
    prepare_lanes,
)
from canonical.divisare_vision_gold import (
    CANDIDATE_MANIFEST_VERSION,
    CLASSES,
    GOLD_MANIFEST_VERSION,
    REVIEWED_POOL_VERSION,
    SOURCE_PROFILE,
)
from canonical.divisare_vision_gold_finalize import (
    FINALIZER_VERSION,
    gold_manifest_sha256,
    parse_json_strict,
    phash_distance,
    validate_gold_manifest,
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


SCHEMA_VERSION = 1
BENCHMARK_VERSION = "divisare-vision-gold-n100-v1.0.0"
EXPECTED_SAMPLE_COUNT = 100
FIXED_BATCH_SIZE = 5
EXPECTED_BATCH_COUNT = EXPECTED_SAMPLE_COUNT // FIXED_BATCH_SIZE
EXPECTED_SUCCESSFUL_CALLS = EXPECTED_BATCH_COUNT * len(LANES)
CLEAR_MACRO_F1_MIN = 0.90
CLEAR_CLASS_RECALL_MIN = 0.85
MAX_1024_MACRO_F1_DEFICIT = 0.03
MAX_1024_ADDITIONAL_CLEAR_ERRORS = 2
PREDICTION_LABELS = (*CLASSES, "unknown")


@dataclass(frozen=True)
class GoldSample:
    sample_rank: int
    sample_id: str
    candidate_id: str
    asset_key: str
    article_id: str
    building_id: str
    generation_group: str
    url_generation: str
    request_url: str
    expected_content_sha256: str
    pixel_sha256: str
    phash_256: str
    gold_label: str
    clarity: str
    acceptable_labels: tuple[str, ...]


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_sha(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError("%s must be lowercase SHA-256 hex" % name)
    return value


def _validate_request_url(value: Any, sample_id: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("%s request_url is missing" % sample_id)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "images.divisare.com":
        raise ValueError("%s request_url must use images.divisare.com HTTPS" % sample_id)
    path_parts = parsed.path.split("/")
    if SOURCE_PROFILE not in path_parts:
        raise ValueError("%s request_url is not the frozen max2048 profile" % sample_id)
    return value


def _load_gold_samples(payload: Mapping[str, Any]) -> list[GoldSample]:
    validate_gold_manifest(payload)
    if payload.get("manifest_version") != GOLD_MANIFEST_VERSION:
        raise ValueError("gold manifest version mismatch")
    if payload.get("finalizer_version") != FINALIZER_VERSION:
        raise ValueError("gold finalizer version mismatch")
    if payload.get("gold_manifest_sha256") != gold_manifest_sha256(payload):
        raise ValueError("gold manifest self SHA mismatch")
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("gold provenance is missing")
    if provenance.get("candidate_manifest_version") != CANDIDATE_MANIFEST_VERSION:
        raise ValueError("gold candidate manifest version mismatch")
    if provenance.get("reviewed_pool_version") != REVIEWED_POOL_VERSION:
        raise ValueError("gold reviewed-pool version mismatch")

    samples: list[GoldSample] = []
    article_ids: set[str] = set()
    building_ids: set[str] = set()
    assets: set[str] = set()
    quota: Counter[tuple[str, str, str]] = Counter()
    hashes: list[tuple[str, str, str]] = []
    for expected_rank, raw in enumerate(payload.get("samples", []), 1):
        if not isinstance(raw, Mapping):
            raise ValueError("gold sample must be an object")
        source = raw.get("source_identity")
        evidence = raw.get("image_evidence")
        review = raw.get("human_review")
        if not all(isinstance(value, Mapping) for value in (source, evidence, review)):
            raise ValueError("gold sample sections must be objects")
        assert isinstance(source, Mapping)
        assert isinstance(evidence, Mapping)
        assert isinstance(review, Mapping)
        sample_id = str(raw.get("sample_id") or "")
        if raw.get("sample_rank") != expected_rank or sample_id != "sample-%04d" % expected_rank:
            raise ValueError("gold sample rank/ID mismatch at %d" % expected_rank)
        candidate_id = str(source.get("candidate_id") or "")
        asset_key = str(source.get("asset_key") or "")
        article_id = str(source.get("article_id") or "")
        building_id = str(source.get("building_id") or "")
        if not all((candidate_id, asset_key, article_id, building_id)):
            raise ValueError("%s has an empty source identity" % sample_id)
        if asset_key in assets or article_id in article_ids or building_id in building_ids:
            raise ValueError("gold asset/article/building identities must be globally unique")
        assets.add(asset_key)
        article_ids.add(article_id)
        building_ids.add(building_id)
        generation = str(source.get("generation_group") or "")
        if generation not in ("modern", "legacy"):
            raise ValueError("%s generation_group is invalid" % sample_id)
        label = str(review.get("gold_label") or "")
        clarity = str(review.get("clarity") or "")
        acceptable = review.get("acceptable_labels")
        if label not in CLASSES or clarity not in ("clear", "boundary"):
            raise ValueError("%s reviewed label is invalid" % sample_id)
        if (
            not isinstance(acceptable, list)
            or not acceptable
            or any(value not in CLASSES for value in acceptable)
            or len(set(acceptable)) != len(acceptable)
            or label not in acceptable
        ):
            raise ValueError("%s acceptable_labels are invalid" % sample_id)
        if clarity == "clear" and acceptable != [label]:
            raise ValueError("%s clear sample must accept only its primary label" % sample_id)
        if clarity == "boundary" and len(acceptable) < 2:
            raise ValueError("%s boundary sample needs multiple acceptable labels" % sample_id)
        content_sha = _require_sha(evidence.get("content_sha256"), "%s content SHA" % sample_id)
        pixel_sha = _require_sha(evidence.get("pixel_sha256"), "%s pixel SHA" % sample_id)
        phash = _require_sha(evidence.get("phash_256"), "%s pHash" % sample_id)
        request_url = _validate_request_url(source.get("request_url"), sample_id)
        quota[(label, generation, clarity)] += 1
        hashes.append((sample_id, pixel_sha, phash))
        samples.append(
            GoldSample(
                sample_rank=expected_rank,
                sample_id=sample_id,
                candidate_id=candidate_id,
                asset_key=asset_key,
                article_id=article_id,
                building_id=building_id,
                generation_group=generation,
                url_generation=str(source.get("url_generation") or ""),
                request_url=request_url,
                expected_content_sha256=content_sha,
                pixel_sha256=pixel_sha,
                phash_256=phash,
                gold_label=label,
                clarity=clarity,
                acceptable_labels=tuple(str(value) for value in acceptable),
            )
        )
    if len(samples) != EXPECTED_SAMPLE_COUNT:
        raise ValueError("gold manifest must contain exactly 100 samples")
    for label in CLASSES:
        expected = {
            (label, "modern", "clear"): 13,
            (label, "modern", "boundary"): 3,
            (label, "legacy", "clear"): 3,
            (label, "legacy", "boundary"): 1,
        }
        for cell, count in expected.items():
            if quota[cell] != count:
                raise ValueError("gold quota mismatch for %s" % "/".join(cell))
    for index, (left_id, left_pixel, left_phash) in enumerate(hashes):
        for right_id, right_pixel, right_phash in hashes[index + 1 :]:
            if left_pixel == right_pixel:
                raise ValueError("gold exact pixel duplicate: %s/%s" % (left_id, right_id))
            if phash_distance(left_phash, right_phash) <= 8:
                raise ValueError("gold pHash <=8 pair: %s/%s" % (left_id, right_id))
    return samples


def load_gold_manifest(
    gold_manifest_path: Path, source_db: Path
) -> tuple[dict[str, Any], list[GoldSample], str, str]:
    raw = gold_manifest_path.read_bytes()
    payload = parse_json_strict(raw, label="gold manifest")
    samples = _load_gold_samples(payload)
    source_sha = file_sha256(source_db)
    declared_source_sha = _require_sha(
        payload["provenance"].get("source_db_sha256"), "gold source DB SHA"
    )
    if source_sha != declared_source_sha:
        raise ValueError(
            "source DB SHA does not match frozen gold: expected %s, got %s"
            % (declared_source_sha, source_sha)
        )
    return payload, samples, _sha256_bytes(raw), source_sha


SIDECAR_SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE benchmark_run(
  run_id INTEGER PRIMARY KEY CHECK(run_id=1),
  status TEXT NOT NULL CHECK(status IN ('running','complete','failed_quality_gate','failed_validation')),
  benchmark_version TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  gold_manifest_path TEXT NOT NULL,
  gold_manifest_version TEXT NOT NULL,
  gold_manifest_file_sha256 TEXT NOT NULL,
  gold_manifest_sha256 TEXT NOT NULL,
  gold_logical_sha256 TEXT NOT NULL,
  reviewer_identifier TEXT NOT NULL,
  review_exported_at TEXT NOT NULL,
  source_db_path TEXT NOT NULL,
  source_sha256_before TEXT NOT NULL,
  source_sha256_after TEXT,
  batch_size INTEGER NOT NULL,
  batch_count INTEGER NOT NULL,
  lanes_json TEXT NOT NULL CHECK(json_valid(lanes_json)),
  lane_schedule_json TEXT NOT NULL CHECK(json_valid(lane_schedule_json)),
  source_derivative_version TEXT NOT NULL,
  local_derivative_version TEXT NOT NULL,
  source_profile TEXT NOT NULL,
  model TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  service_tier TEXT NOT NULL,
  runtime_version TEXT NOT NULL,
  cli_version TEXT,
  image_detail TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  selected_lane TEXT,
  decision_reason TEXT,
  metrics_json TEXT CHECK(metrics_json IS NULL OR json_valid(metrics_json)),
  logical_sha256 TEXT,
  error TEXT
);

CREATE TABLE gold_samples(
  sample_rank INTEGER PRIMARY KEY,
  sample_id TEXT NOT NULL UNIQUE,
  candidate_id TEXT NOT NULL UNIQUE,
  asset_key TEXT NOT NULL UNIQUE,
  article_id TEXT NOT NULL UNIQUE,
  building_id TEXT NOT NULL UNIQUE,
  generation_group TEXT NOT NULL CHECK(generation_group IN ('modern','legacy')),
  url_generation TEXT NOT NULL,
  request_url TEXT NOT NULL UNIQUE,
  expected_content_sha256 TEXT NOT NULL CHECK(length(expected_content_sha256)=64),
  gold_pixel_sha256 TEXT NOT NULL CHECK(length(gold_pixel_sha256)=64),
  gold_phash_256 TEXT NOT NULL CHECK(length(gold_phash_256)=64),
  gold_label TEXT NOT NULL,
  clarity TEXT NOT NULL CHECK(clarity IN ('clear','boundary')),
  acceptable_labels_json TEXT NOT NULL CHECK(json_valid(acceptable_labels_json))
);

CREATE TABLE fetch_attempts(
  fetch_attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_key TEXT NOT NULL REFERENCES gold_samples(asset_key),
  batch_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('success','failed','content_mismatch')),
  expected_content_sha256 TEXT NOT NULL,
  actual_content_sha256 TEXT,
  response_bytes INTEGER,
  response_mime TEXT,
  http_status INTEGER,
  final_url TEXT,
  elapsed_ms INTEGER NOT NULL,
  error_kind TEXT,
  error_message TEXT
);

CREATE TABLE fetch_results(
  asset_key TEXT PRIMARY KEY REFERENCES gold_samples(asset_key),
  status TEXT NOT NULL CHECK(status IN ('success','failed','content_mismatch')),
  expected_content_sha256 TEXT NOT NULL,
  actual_content_sha256 TEXT,
  response_bytes INTEGER,
  response_mime TEXT,
  http_status INTEGER,
  final_url TEXT,
  decoded_format TEXT,
  width INTEGER,
  height INTEGER,
  elapsed_ms INTEGER NOT NULL,
  error_kind TEXT,
  error_message TEXT,
  CHECK((status='success' AND expected_content_sha256=actual_content_sha256
         AND response_bytes>0 AND decoded_format IS NOT NULL AND width>0 AND height>0)
        OR status IN ('failed','content_mismatch'))
);

CREATE TABLE derived_inputs(
  asset_key TEXT NOT NULL REFERENCES gold_samples(asset_key),
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
  scheduled_position INTEGER NOT NULL CHECK(scheduled_position IN (1,2)),
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
  asset_key TEXT NOT NULL REFERENCES gold_samples(asset_key),
  lane TEXT NOT NULL,
  medium TEXT NOT NULL,
  view TEXT NOT NULL,
  predicted_label TEXT NOT NULL,
  visible_materials_json TEXT NOT NULL CHECK(json_valid(visible_materials_json)),
  visible_elements_json TEXT NOT NULL CHECK(json_valid(visible_elements_json)),
  needs_detail_review INTEGER NOT NULL CHECK(needs_detail_review IN (0,1)),
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  evidence TEXT NOT NULL,
  primary_correct INTEGER NOT NULL CHECK(primary_correct IN (0,1)),
  acceptable_correct INTEGER NOT NULL CHECK(acceptable_correct IN (0,1)),
  response_json TEXT NOT NULL CHECK(json_valid(response_json)),
  PRIMARY KEY(asset_key,lane)
);

CREATE TABLE classification_metrics(
  lane TEXT NOT NULL,
  scope TEXT NOT NULL CHECK(scope IN ('all','clear','boundary')),
  class_label TEXT NOT NULL,
  support INTEGER NOT NULL,
  tp INTEGER,
  fp INTEGER,
  fn INTEGER,
  precision REAL,
  recall REAL,
  f1 REAL,
  primary_correct INTEGER NOT NULL,
  acceptable_correct INTEGER NOT NULL,
  total INTEGER NOT NULL,
  PRIMARY KEY(lane,scope,class_label)
);

CREATE TABLE validations(
  validation_name TEXT PRIMARY KEY,
  severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
  passed INTEGER NOT NULL CHECK(passed IN (0,1)),
  expected TEXT,
  actual TEXT NOT NULL,
  detail TEXT
);

CREATE INDEX idx_results_lane_label ON vision_results(lane,predicted_label);
CREATE INDEX idx_attempts_batch_lane ON vision_attempts(batch_no,lane,attempt_id);
"""


def lane_schedule(batch_no: int) -> tuple[str, str]:
    if batch_no < 1:
        raise ValueError("batch_no must be positive")
    return ("long1024", "long2048") if batch_no % 2 else ("long2048", "long1024")


def initialize_sidecar(
    conn: sqlite3.Connection,
    *,
    samples: Sequence[GoldSample],
    gold_manifest_path: Path,
    gold_payload: Mapping[str, Any],
    gold_file_sha256: str,
    source_db: Path,
    source_sha256: str,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: Optional[str],
) -> None:
    conn.executescript(SIDECAR_SCHEMA)
    schedule = {str(batch): list(lane_schedule(batch)) for batch in range(1, 21)}
    conn.execute(
        """
        INSERT INTO benchmark_run(
          run_id,status,benchmark_version,schema_version,gold_manifest_path,
          gold_manifest_version,gold_manifest_file_sha256,gold_manifest_sha256,
          gold_logical_sha256,reviewer_identifier,review_exported_at,
          source_db_path,source_sha256_before,batch_size,batch_count,
          lanes_json,lane_schedule_json,source_derivative_version,local_derivative_version,
          source_profile,model,reasoning,service_tier,runtime_version,cli_version,
          image_detail,started_at
        ) VALUES(1,'running',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            BENCHMARK_VERSION,
            SCHEMA_VERSION,
            str(gold_manifest_path),
            GOLD_MANIFEST_VERSION,
            gold_file_sha256,
            gold_payload["gold_manifest_sha256"],
            gold_payload["logical_sha256"],
            str(gold_payload["provenance"]["reviewer"]),
            str(gold_payload["provenance"]["review_exported_at"]),
            str(source_db),
            source_sha256,
            FIXED_BATCH_SIZE,
            EXPECTED_BATCH_COUNT,
            canonical_json(dict(LANES)),
            canonical_json(schedule),
            SOURCE_DERIVATIVE_VERSION,
            LOCAL_DERIVATIVE_VERSION,
            SOURCE_PROFILE,
            model,
            reasoning,
            service_tier,
            RUNTIME_VERSION,
            cli_version,
            CLI_IMAGE_DETAIL,
            utc_now(),
        ),
    )
    conn.executemany(
        "INSERT INTO gold_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                row.sample_rank,
                row.sample_id,
                row.candidate_id,
                row.asset_key,
                row.article_id,
                row.building_id,
                row.generation_group,
                row.url_generation,
                row.request_url,
                row.expected_content_sha256,
                row.pixel_sha256,
                row.phash_256,
                row.gold_label,
                row.clarity,
                canonical_json(list(row.acceptable_labels)),
            )
            for row in samples
        ],
    )
    conn.commit()


def _validate_resume(
    conn: sqlite3.Connection,
    *,
    gold_manifest_path: Path,
    gold_payload: Mapping[str, Any],
    gold_file_sha256: str,
    source_db: Path,
    source_sha256: str,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: Optional[str],
) -> None:
    row = conn.execute("SELECT * FROM benchmark_run WHERE run_id=1").fetchone()
    if row is None:
        raise RuntimeError("partial N100 sidecar has no benchmark_run")
    columns = [value[1] for value in conn.execute("PRAGMA table_info(benchmark_run)")]
    actual = dict(zip(columns, row))
    expected = {
        "status": "running",
        "benchmark_version": BENCHMARK_VERSION,
        "schema_version": SCHEMA_VERSION,
        "gold_manifest_path": str(gold_manifest_path),
        "gold_manifest_version": GOLD_MANIFEST_VERSION,
        "gold_manifest_file_sha256": gold_file_sha256,
        "gold_manifest_sha256": gold_payload["gold_manifest_sha256"],
        "gold_logical_sha256": gold_payload["logical_sha256"],
        "reviewer_identifier": str(gold_payload["provenance"]["reviewer"]),
        "review_exported_at": str(gold_payload["provenance"]["review_exported_at"]),
        "source_db_path": str(source_db),
        "source_sha256_before": source_sha256,
        "batch_size": FIXED_BATCH_SIZE,
        "batch_count": EXPECTED_BATCH_COUNT,
        "lanes_json": canonical_json(dict(LANES)),
        "source_profile": SOURCE_PROFILE,
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "runtime_version": RUNTIME_VERSION,
        "cli_version": cli_version,
        "image_detail": CLI_IMAGE_DETAIL,
    }
    mismatches = {
        key: {"actual": actual.get(key), "expected": value}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise RuntimeError("resume contract mismatch: %s" % canonical_json(mismatches))


def _existing_lanes(conn: sqlite3.Connection, asset_key: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT lane FROM vision_results WHERE asset_key=?", (asset_key,)
        )
    }


def _write_fetch_attempt(
    conn: sqlite3.Connection,
    *,
    sample: GoldSample,
    batch_no: int,
    status: str,
    elapsed_ms: int,
    payload: FetchPayload | None = None,
    actual_sha: str | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_attempts(
          asset_key,batch_no,status,expected_content_sha256,actual_content_sha256,
          response_bytes,response_mime,http_status,final_url,elapsed_ms,error_kind,error_message
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            sample.asset_key,
            batch_no,
            status,
            sample.expected_content_sha256,
            actual_sha,
            len(payload.raw) if payload else None,
            payload.mime_type if payload else None,
            payload.http_status if payload else None,
            payload.final_url if payload else None,
            elapsed_ms,
            error_kind,
            (error_message or "")[:1000] or None,
        ),
    )


def _write_fetch_result(
    conn: sqlite3.Connection,
    *,
    sample: GoldSample,
    status: str,
    elapsed_ms: int,
    payload: FetchPayload | None = None,
    decoded: DecodedSource | None = None,
    actual_sha: str | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO fetch_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            sample.asset_key,
            status,
            sample.expected_content_sha256,
            actual_sha,
            len(payload.raw) if payload else None,
            payload.mime_type if payload else None,
            payload.http_status if payload else None,
            payload.final_url if payload else None,
            decoded.decoded_format if decoded else None,
            decoded.width if decoded else None,
            decoded.height if decoded else None,
            elapsed_ms,
            error_kind,
            (error_message or "")[:1000] or None,
        ),
    )


def _derivative_tuple(value: PreparedDerivative) -> tuple[Any, ...]:
    return (
        value.max_long_edge,
        value.width,
        value.height,
        value.raw_patch_count,
        len(value.encoded_bytes),
        value.encoded_sha256,
        value.pixel_sha256,
    )


def _retain_or_write_derivatives(
    conn: sqlite3.Connection,
    *,
    sample: GoldSample,
    payload: FetchPayload,
    prepared: Mapping[str, PreparedDerivative],
    decoded: DecodedSource,
    elapsed_ms: int,
) -> None:
    prior = conn.execute(
        "SELECT status,actual_content_sha256 FROM fetch_results WHERE asset_key=?",
        (sample.asset_key,),
    ).fetchone()
    actual_sha = _sha256_bytes(payload.raw)
    if prior is not None and prior[0] == "success":
        if prior[1] != actual_sha:
            raise RuntimeError("resume source response changed for %s" % sample.sample_id)
        retained = {
            str(row[0]): tuple(row[1:])
            for row in conn.execute(
                """
                SELECT lane,max_long_edge,width,height,raw_patch_count,encoded_bytes,
                       encoded_sha256,pixel_sha256
                FROM derived_inputs WHERE asset_key=?
                """,
                (sample.asset_key,),
            )
        }
        for lane, value in prepared.items():
            if retained.get(lane) != _derivative_tuple(value):
                raise RuntimeError("resume derivative changed for %s/%s" % (sample.sample_id, lane))
        return
    _write_fetch_result(
        conn,
        sample=sample,
        status="success",
        elapsed_ms=elapsed_ms,
        payload=payload,
        decoded=decoded,
        actual_sha=actual_sha,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO derived_inputs VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (sample.asset_key, lane, *_derivative_tuple(value))
            for lane, value in prepared.items()
        ],
    )


def _write_vision_attempt(
    conn: sqlite3.Connection,
    *,
    lane: str,
    batch_no: int,
    scheduled_position: int,
    asset_keys: Sequence[str],
    result: VisionRuntimeResult,
    status: str,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    usage = result.usage
    cli_version = conn.execute(
        "SELECT cli_version FROM benchmark_run WHERE run_id=1"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO vision_attempts(
          lane,batch_no,scheduled_position,asset_keys_json,status,model,reasoning,
          service_tier,runtime_version,cli_version,codex_bin,image_detail,sandbox,
          prompt_sha256,output_schema_sha256,elapsed_ms,input_tokens,cached_input_tokens,
          output_tokens,raw_events_sha256,stdout_excerpt,stderr_excerpt,
          non_json_lines_json,error_kind,error_message
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            lane,
            batch_no,
            scheduled_position,
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
            _sha256_bytes(result.stdout.encode("utf-8")) if result.stdout else None,
            result.stdout[-8000:] if result.stdout else None,
            result.stderr[-8000:] if result.stderr else None,
            canonical_json(list(result.non_json_stdout_lines)),
            error_kind or result.error_kind,
            (error_message or result.error_message or "")[:1000] or None,
        ),
    )


def _write_vision_results(
    conn: sqlite3.Connection,
    lane: str,
    samples: Sequence[GoldSample],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    values = []
    for sample, row in zip(samples, rows):
        predicted = str(row["legacy_type"])
        values.append(
            (
                sample.asset_key,
                lane,
                row["medium"],
                row["view"],
                predicted,
                canonical_json(row["visible_materials"]),
                canonical_json(row["visible_elements"]),
                int(row["needs_detail_review"]),
                row["confidence"],
                row["evidence"],
                int(predicted == sample.gold_label),
                int(predicted in sample.acceptable_labels),
                canonical_json(row),
            )
        )
    conn.executemany("INSERT OR REPLACE INTO vision_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", values)


def _scope_rows(conn: sqlite3.Connection, lane: str, scope: str) -> list[dict[str, Any]]:
    where = "" if scope == "all" else "AND g.clarity=?"
    parameters: tuple[Any, ...] = (lane,) if scope == "all" else (lane, scope)
    rows = conn.execute(
        """
        SELECT g.gold_label,g.clarity,g.acceptable_labels_json,r.predicted_label,
               r.primary_correct,r.acceptable_correct
        FROM vision_results r JOIN gold_samples g ON g.asset_key=r.asset_key
        WHERE r.lane=? %s ORDER BY g.sample_rank
        """ % where,
        parameters,
    ).fetchall()
    return [
        {
            "gold": str(row[0]),
            "clarity": str(row[1]),
            "acceptable": json.loads(row[2]),
            "predicted": str(row[3]),
            "primary_correct": int(row[4]),
            "acceptable_correct": int(row[5]),
        }
        for row in rows
    ]


def _classification_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    output: dict[str, Any] = {}
    metric_rows: list[tuple[Any, ...]] = []
    for lane, _edge in LANES:
        lane_metrics: dict[str, Any] = {}
        for scope in ("all", "clear", "boundary"):
            rows = _scope_rows(conn, lane, scope)
            total = len(rows)
            primary_correct = sum(row["primary_correct"] for row in rows)
            acceptable_correct = sum(row["acceptable_correct"] for row in rows)
            confusion = {
                label: {predicted: 0 for predicted in PREDICTION_LABELS}
                for label in CLASSES
            }
            per_class: dict[str, Any] = {}
            for row in rows:
                confusion[row["gold"]][row["predicted"]] += 1
            for label in CLASSES:
                support = sum(row["gold"] == label for row in rows)
                tp = sum(row["gold"] == label and row["predicted"] == label for row in rows)
                fp = sum(row["gold"] != label and row["predicted"] == label for row in rows)
                fn = support - tp
                precision = tp / (tp + fp) if tp + fp else 0.0
                recall = tp / support if support else 0.0
                f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
                per_class[label] = {
                    "support": support,
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "precision": round(precision, 6),
                    "recall": round(recall, 6),
                    "f1": round(f1, 6),
                }
                metric_rows.append(
                    (
                        lane,
                        scope,
                        label,
                        support,
                        tp,
                        fp,
                        fn,
                        precision,
                        recall,
                        f1,
                        primary_correct,
                        acceptable_correct,
                        total,
                    )
                )
            macro_f1 = sum(per_class[label]["f1"] for label in CLASSES) / len(CLASSES)
            lane_metrics[scope] = {
                "total": total,
                "primary_correct": primary_correct,
                "primary_accuracy": round(primary_correct / total, 6) if total else 0.0,
                "acceptable_correct": acceptable_correct,
                "acceptable_accuracy": round(acceptable_correct / total, 6) if total else 0.0,
                "macro_f1": round(macro_f1, 6),
                "per_class": per_class,
                "primary_confusion": confusion,
            }
            metric_rows.append(
                (
                    lane,
                    scope,
                    "__overall__",
                    total,
                    None,
                    None,
                    None,
                    None,
                    None,
                    macro_f1,
                    primary_correct,
                    acceptable_correct,
                    total,
                )
            )
        output[lane] = lane_metrics
    conn.execute("DELETE FROM classification_metrics")
    conn.executemany(
        "INSERT INTO classification_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", metric_rows
    )
    return output


def choose_resolution(classification: Mapping[str, Any]) -> dict[str, Any]:
    gates: dict[str, Any] = {}
    for lane, _edge in LANES:
        clear = classification[lane]["clear"]
        recalls = {
            label: float(clear["per_class"][label]["recall"]) for label in CLASSES
        }
        gates[lane] = {
            "clear_macro_f1": float(clear["macro_f1"]),
            "clear_errors": int(clear["total"] - clear["primary_correct"]),
            "minimum_class_recall": min(recalls.values()),
            "class_recalls": recalls,
            "passes": (
                float(clear["macro_f1"]) >= CLEAR_MACRO_F1_MIN
                and all(value >= CLEAR_CLASS_RECALL_MIN for value in recalls.values())
            ),
        }
    low = gates["long1024"]
    high = gates["long2048"]
    macro_deficit = high["clear_macro_f1"] - low["clear_macro_f1"]
    additional_errors = low["clear_errors"] - high["clear_errors"]
    low_efficient = (
        low["passes"]
        and macro_deficit <= MAX_1024_MACRO_F1_DEFICIT + 1e-12
        and additional_errors <= MAX_1024_ADDITIONAL_CLEAR_ERRORS
    )
    if low_efficient:
        selected = "long1024"
        reason = "1024 passes quality gates and is within the allowed 2048 accuracy margin"
    elif high["passes"]:
        selected = "long2048"
        reason = "1024 misses the efficiency rule; 2048 passes all quality gates"
    else:
        selected = "fail"
        reason = "no eligible resolution satisfies the frozen clear-sample quality gates"
    return {
        "selected_lane": selected,
        "reason": reason,
        "thresholds": {
            "clear_macro_f1_min": CLEAR_MACRO_F1_MIN,
            "clear_class_recall_min": CLEAR_CLASS_RECALL_MIN,
            "max_1024_macro_f1_deficit": MAX_1024_MACRO_F1_DEFICIT,
            "max_1024_additional_clear_errors": MAX_1024_ADDITIONAL_CLEAR_ERRORS,
        },
        "observed": {
            "macro_f1_deficit_1024_vs_2048": round(macro_deficit, 6),
            "additional_clear_errors_1024_vs_2048": additional_errors,
        },
        "gates": gates,
    }


def _usage_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute(
        """
        SELECT COUNT(*),SUM(status='success'),SUM(status='failed'),
               COALESCE(SUM(input_tokens),0),COALESCE(SUM(cached_input_tokens),0),
               COALESCE(SUM(output_tokens),0),COALESCE(SUM(elapsed_ms),0)
        FROM vision_attempts
        """
    ).fetchone()
    by_lane = {
        str(row[0]): {
            "attempts": int(row[1]),
            "successful_attempts": int(row[2] or 0),
            "failed_attempts": int(row[3] or 0),
            "input_tokens": int(row[4] or 0),
            "cached_input_tokens": int(row[5] or 0),
            "output_tokens": int(row[6] or 0),
            "elapsed_ms": int(row[7] or 0),
        }
        for row in conn.execute(
            """
            SELECT lane,COUNT(*),SUM(status='success'),SUM(status='failed'),
                   COALESCE(SUM(input_tokens),0),COALESCE(SUM(cached_input_tokens),0),
                   COALESCE(SUM(output_tokens),0),COALESCE(SUM(elapsed_ms),0)
            FROM vision_attempts GROUP BY lane ORDER BY lane
            """
        )
    }
    return {
        "vision_attempts": int(total[0]),
        "successful_attempts": int(total[1] or 0),
        "failed_attempts": int(total[2] or 0),
        "input_tokens": int(total[3] or 0),
        "cached_input_tokens": int(total[4] or 0),
        "output_tokens": int(total[5] or 0),
        "model_elapsed_ms": int(total[6] or 0),
        "by_lane": by_lane,
        "fetch_attempts": int(conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0]),
        "downloaded_bytes": int(
            conn.execute("SELECT COALESCE(SUM(response_bytes),0) FROM fetch_attempts").fetchone()[0]
        ),
    }


def _validations(
    conn: sqlite3.Connection,
    *,
    source_sha_after: str,
    gold_file_sha_after: str,
) -> list[dict[str, Any]]:
    run = conn.execute(
        "SELECT source_sha256_before,gold_manifest_file_sha256 FROM benchmark_run WHERE run_id=1"
    ).fetchone()
    expected_slots = {
        (batch_no, lane)
        for batch_no in range(1, EXPECTED_BATCH_COUNT + 1)
        for lane in lane_schedule(batch_no)
    }
    successful_rows = conn.execute(
        """
        SELECT batch_no,lane,scheduled_position,asset_keys_json
        FROM vision_attempts WHERE status='success' ORDER BY attempt_id
        """
    ).fetchall()
    actual_slots = {(int(row[0]), str(row[1])) for row in successful_rows}
    expected_assets_by_batch = {
        batch_no: [
            str(row[0])
            for row in conn.execute(
                """
                SELECT asset_key FROM gold_samples
                WHERE sample_rank BETWEEN ? AND ? ORDER BY sample_rank
                """,
                ((batch_no - 1) * FIXED_BATCH_SIZE + 1, batch_no * FIXED_BATCH_SIZE),
            )
        ]
        for batch_no in range(1, EXPECTED_BATCH_COUNT + 1)
    }
    successful_by_batch: dict[int, list[tuple[str, int, list[str]]]] = {
        batch_no: [] for batch_no in range(1, EXPECTED_BATCH_COUNT + 1)
    }
    for batch_no, lane, position, asset_keys_json in successful_rows:
        successful_by_batch[int(batch_no)].append(
            (str(lane), int(position), json.loads(str(asset_keys_json)))
        )
    call_contract_ok = all(
        successful_by_batch[batch_no]
        == [
            (lane, position, expected_assets_by_batch[batch_no])
            for position, lane in enumerate(lane_schedule(batch_no), 1)
        ]
        for batch_no in range(1, EXPECTED_BATCH_COUNT + 1)
    )
    successful = int(
        conn.execute("SELECT COUNT(*) FROM vision_attempts WHERE status='success'").fetchone()[0]
    )
    sample_count = int(conn.execute("SELECT COUNT(*) FROM gold_samples").fetchone()[0])
    fetch_success = int(
        conn.execute("SELECT COUNT(*) FROM fetch_results WHERE status='success'").fetchone()[0]
    )
    content_match = int(
        conn.execute(
            "SELECT COUNT(*) FROM fetch_results WHERE status='success' AND expected_content_sha256=actual_content_sha256"
        ).fetchone()[0]
    )
    derived = int(conn.execute("SELECT COUNT(*) FROM derived_inputs").fetchone()[0])
    results = int(conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0])
    integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())

    def row(name: str, passed: bool, expected: Any, actual: Any, detail: str | None = None):
        return {
            "validation_name": name,
            "severity": "error",
            "passed": passed,
            "expected": str(expected),
            "actual": str(actual),
            "detail": detail,
        }

    return [
        row("gold_sample_count", sample_count == 100, 100, sample_count),
        row("fetch_success", fetch_success == 100, 100, fetch_success),
        row("frozen_content_sha_match", content_match == 100, 100, content_match),
        row("derived_input_accounting", derived == 200, 200, derived),
        row("vision_result_accounting", results == 200, 200, results),
        row("successful_call_accounting", successful == 40, 40, successful),
        row("batch_lane_slots", actual_slots == expected_slots, 40, len(actual_slots)),
        row(
            "counterbalanced_batch_call_contract",
            call_contract_ok,
            "20 batches x ordered 2 lanes x exact 5 assets",
            "match" if call_contract_ok else "mismatch",
        ),
        row("source_immutable", source_sha_after == run[0], run[0], source_sha_after),
        row("gold_manifest_immutable", gold_file_sha_after == run[1], run[1], gold_file_sha_after),
        row("sqlite_quick_check", integrity == "ok", "ok", integrity),
        row("foreign_keys", foreign_keys == 0, 0, foreign_keys),
    ]


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


def logical_sha256(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    run = conn.execute(
        """
        SELECT benchmark_version,schema_version,gold_manifest_version,
               gold_manifest_file_sha256,gold_manifest_sha256,gold_logical_sha256,
               reviewer_identifier,review_exported_at,
               source_sha256_before,batch_size,batch_count,lanes_json,lane_schedule_json,
               source_derivative_version,local_derivative_version,source_profile,
               model,reasoning,service_tier,runtime_version,COALESCE(cli_version,''),image_detail,
               COALESCE(selected_lane,''),COALESCE(decision_reason,''),COALESCE(metrics_json,'')
        FROM benchmark_run WHERE run_id=1
        """
    ).fetchone()
    digest.update(b"benchmark_run\0" + canonical_json(list(run)).encode("utf-8") + b"\n")
    for table, order_by in (
        ("gold_samples", "sample_rank"),
        ("fetch_results", "asset_key"),
        ("derived_inputs", "asset_key,lane"),
        ("vision_results", "asset_key,lane"),
        ("classification_metrics", "lane,scope,class_label"),
        ("validations", "validation_name"),
    ):
        columns = [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]
        digest.update((table + "\0").encode("utf-8"))
        for row in conn.execute("SELECT * FROM %s ORDER BY %s" % (table, order_by)):
            digest.update(canonical_json(dict(zip(columns, row))).encode("utf-8") + b"\n")
    return digest.hexdigest()


def reviewer_interpretation(reviewer_identifier: str) -> str:
    automated = any(
        marker in reviewer_identifier.casefold()
        for marker in ("agent", "model", "codex", "gpt", "claude", "openai")
    )
    if automated:
        return "model/agent-reviewed labels; not independent-human accuracy"
    return "accuracy against the frozen reviewed gold; reviewer independence is not inferred"


def render_report(conn: sqlite3.Connection, artifact_path: Path) -> str:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(benchmark_run)")]
    run = dict(zip(columns, conn.execute("SELECT * FROM benchmark_run WHERE run_id=1").fetchone()))
    metrics = json.loads(run["metrics_json"])
    decision = metrics["decision"]
    usage = metrics["usage"]
    reviewer_identifier = str(run["reviewer_identifier"])
    lines = [
        "# Divisare Vision N100 gold benchmark",
        "",
        "## Contract",
        "",
        "- Artifact: `%s`" % artifact_path,
        "- Benchmark: `%s`" % run["benchmark_version"],
        "- Gold manifest SHA: `%s`" % run["gold_manifest_sha256"],
        "- Gold file SHA: `%s`" % run["gold_manifest_file_sha256"],
        "- Reviewer identifier (verbatim): `%s`" % reviewer_identifier,
        "- Review exported at: `%s`" % run["review_exported_at"],
        "- Source SHA before: `%s`" % run["source_sha256_before"],
        "- Source SHA after: `%s`" % run["source_sha256_after"],
        "- Logical SHA: `%s`" % run["logical_sha256"],
        "- Model: `%s`" % run["model"],
        "- Batch contract: `20 x 5`, two counterbalanced lanes, `40` successful calls",
        "- Images persisted: `false`",
        "- Gold interpretation: `%s`"
        % reviewer_interpretation(reviewer_identifier),
        "",
        "## Decision",
        "",
        "- Status: `%s`" % run["status"],
        "- Selected lane: `%s`" % decision["selected_lane"],
        "- Reason: %s" % decision["reason"],
        "- 1024 vs 2048 clear macro-F1 deficit: `%.4f`"
        % decision["observed"]["macro_f1_deficit_1024_vs_2048"],
        "- 1024 additional clear errors: `%d`"
        % decision["observed"]["additional_clear_errors_1024_vs_2048"],
        "",
        "## Accuracy",
        "",
        "| Lane | Scope | Primary | Acceptable | Macro-F1 | Min recall |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for lane, _edge in LANES:
        for scope in ("all", "clear", "boundary"):
            value = metrics["classification"][lane][scope]
            min_recall = min(
                value["per_class"][label]["recall"] for label in CLASSES
            )
            lines.append(
                "| `%s` | %s | %.2f%% | %.2f%% | %.4f | %.4f |"
                % (
                    lane,
                    scope,
                    100 * value["primary_accuracy"],
                    100 * value["acceptable_accuracy"],
                    value["macro_f1"],
                    min_recall,
                )
            )
    lines.extend(["", "### Per-class metrics", ""])
    for lane, _edge in LANES:
        for scope in ("all", "clear", "boundary"):
            lines.append("`%s/%s`:" % (lane, scope))
            for label in CLASSES:
                value = metrics["classification"][lane][scope]["per_class"][label]
                lines.append(
                    "- `%s`: P `%.4f`, R `%.4f`, F1 `%.4f` (support `%d`)"
                    % (
                        label,
                        value["precision"],
                        value["recall"],
                        value["f1"],
                        value["support"],
                    )
                )
    lines.extend(["", "### Overall primary-label confusion", ""])
    for lane, _edge in LANES:
        confusion = metrics["classification"][lane]["all"]["primary_confusion"]
        lines.extend(
            [
                "`%s`:" % lane,
                "",
                "| Gold \\ Predicted | %s |" % " | ".join(PREDICTION_LABELS),
                "|---|%s|" % "|".join("---:" for _ in PREDICTION_LABELS),
            ]
        )
        for label in CLASSES:
            lines.append(
                "| %s | %s |"
                % (
                    label,
                    " | ".join(str(confusion[label][value]) for value in PREDICTION_LABELS),
                )
            )
    lines.extend(
        [
            "",
            "## Accounting",
            "",
            "- Fetch attempts: `%d`, downloaded bytes: `%d`"
            % (usage["fetch_attempts"], usage["downloaded_bytes"]),
            "- Vision attempts: `%d` (`%d` success, `%d` failed)"
            % (
                usage["vision_attempts"],
                usage["successful_attempts"],
                usage["failed_attempts"],
            ),
            "- Tokens: input `%d`, cached input `%d`, output `%d`"
            % (usage["input_tokens"], usage["cached_input_tokens"], usage["output_tokens"]),
            "- Model wall time: `%.1fs`" % (usage["model_elapsed_ms"] / 1000),
            "",
            "## Validations",
            "",
        ]
    )
    for row in conn.execute(
        "SELECT validation_name,severity,passed,expected,actual,detail FROM validations ORDER BY validation_name"
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


def _publish_pair(partial_db: Path, output_db: Path, partial_report: Path, report: Path) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    linked_db = False
    try:
        os.link(partial_db, output_db)
        linked_db = True
        os.link(partial_report, report)
    except FileExistsError as exc:
        if linked_db:
            output_db.unlink()
        raise FileExistsError("immutable output or report already exists") from exc
    partial_db.unlink()
    partial_report.unlink()


def run_n100(
    *,
    source_db: Path,
    gold_manifest_path: Path,
    output_db: Path,
    report_path: Path,
    codex_bin: Path,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cli_version: Optional[str] = None,
    resume: bool = False,
    fetcher: Callable[[str], FetchPayload] = network_fetch,
    executor: Callable[..., VisionRuntimeResult] = run_codex_vision_batch,
) -> dict[str, Any]:
    """Run the immutable N100 benchmark with batch-local image bytes."""
    source_db = source_db.resolve()
    gold_manifest_path = gold_manifest_path.resolve()
    output_db = output_db.resolve()
    report_path = report_path.resolve()
    paths = {source_db, gold_manifest_path, output_db, report_path}
    if len(paths) != 4:
        raise ValueError("source, gold, output, and report paths must be distinct")
    if output_db.exists():
        raise FileExistsError("immutable output already exists: %s" % output_db)
    if report_path.exists():
        raise FileExistsError("immutable report already exists: %s" % report_path)
    partial_db = output_db.with_name(output_db.name + ".partial")
    partial_report = report_path.with_name(report_path.name + ".partial")
    if partial_report.exists():
        raise FileExistsError("stale partial report exists: %s" % partial_report)

    gold_payload, samples, gold_file_sha, source_sha = load_gold_manifest(
        gold_manifest_path, source_db
    )
    partial_db.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        if partial_db.exists():
            if not resume:
                raise FileExistsError("partial sidecar exists; pass resume=True: %s" % partial_db)
            conn = sqlite3.connect(partial_db)
            conn.execute("PRAGMA foreign_keys=ON")
            _validate_resume(
                conn,
                gold_manifest_path=gold_manifest_path,
                gold_payload=gold_payload,
                gold_file_sha256=gold_file_sha,
                source_db=source_db,
                source_sha256=source_sha,
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
                samples=samples,
                gold_manifest_path=gold_manifest_path,
                gold_payload=gold_payload,
                gold_file_sha256=gold_file_sha,
                source_db=source_db,
                source_sha256=source_sha,
                model=model,
                reasoning=reasoning,
                service_tier=service_tier,
                cli_version=cli_version,
            )
    except Exception:
        if conn is not None:
            conn.close()
        raise
    assert conn is not None

    schema_text = canonical_json(VISION_OUTPUT_SCHEMA)
    logical = ""
    metrics: dict[str, Any] = {}
    try:
        for batch_index in range(EXPECTED_BATCH_COUNT):
            batch_no = batch_index + 1
            batch = samples[batch_index * FIXED_BATCH_SIZE : (batch_index + 1) * FIXED_BATCH_SIZE]
            needed = {
                sample.asset_key: {
                    lane for lane, _edge in LANES if lane not in _existing_lanes(conn, sample.asset_key)
                }
                for sample in batch
            }
            if not any(needed.values()):
                continue
            derivatives: dict[str, dict[str, PreparedDerivative]] = {}
            for sample in batch:
                if not needed[sample.asset_key]:
                    continue
                started = time.perf_counter()
                payload: FetchPayload | None = None
                actual_sha: str | None = None
                prior_fetch_success = conn.execute(
                    "SELECT 1 FROM fetch_results WHERE asset_key=? AND status='success'",
                    (sample.asset_key,),
                ).fetchone() is not None
                try:
                    payload = fetcher(sample.request_url)
                    actual_sha = _sha256_bytes(payload.raw)
                    elapsed_ms = round((time.perf_counter() - started) * 1000)
                    if actual_sha != sample.expected_content_sha256:
                        message = "frozen response SHA mismatch for %s: expected %s, got %s" % (
                            sample.sample_id,
                            sample.expected_content_sha256,
                            actual_sha,
                        )
                        _write_fetch_attempt(
                            conn,
                            sample=sample,
                            batch_no=batch_no,
                            status="content_mismatch",
                            elapsed_ms=elapsed_ms,
                            payload=payload,
                            actual_sha=actual_sha,
                            error_kind="content_sha256_mismatch",
                            error_message=message,
                        )
                        if not prior_fetch_success:
                            _write_fetch_result(
                                conn,
                                sample=sample,
                                status="content_mismatch",
                                elapsed_ms=elapsed_ms,
                                payload=payload,
                                actual_sha=actual_sha,
                                error_kind="content_sha256_mismatch",
                                error_message=message,
                            )
                        conn.commit()
                        raise RuntimeError(message)
                    decoded = decode_source(payload.raw)
                    prepared = {value.lane: value for value in prepare_lanes(decoded)}
                    _write_fetch_attempt(
                        conn,
                        sample=sample,
                        batch_no=batch_no,
                        status="success",
                        elapsed_ms=elapsed_ms,
                        payload=payload,
                        actual_sha=actual_sha,
                    )
                    _retain_or_write_derivatives(
                        conn,
                        sample=sample,
                        payload=payload,
                        prepared=prepared,
                        decoded=decoded,
                        elapsed_ms=elapsed_ms,
                    )
                    derivatives[sample.asset_key] = prepared
                except RuntimeError:
                    raise
                except Exception as exc:
                    elapsed_ms = round((time.perf_counter() - started) * 1000)
                    _write_fetch_attempt(
                        conn,
                        sample=sample,
                        batch_no=batch_no,
                        status="failed",
                        elapsed_ms=elapsed_ms,
                        payload=payload,
                        actual_sha=actual_sha,
                        error_kind=getattr(exc, "kind", exc.__class__.__name__),
                        error_message=str(exc),
                    )
                    if not prior_fetch_success:
                        _write_fetch_result(
                            conn,
                            sample=sample,
                            status="failed",
                            elapsed_ms=elapsed_ms,
                            payload=payload,
                            actual_sha=actual_sha,
                            error_kind=getattr(exc, "kind", exc.__class__.__name__),
                            error_message=str(exc),
                        )
                    conn.commit()
                    failure = (
                        "N100 resume refetch could not reproduce retained input for %s"
                        if prior_fetch_success
                        else "N100 fetch failed for %s"
                    )
                    raise RuntimeError(failure % sample.sample_id) from exc
                conn.commit()

            with tempfile.TemporaryDirectory(prefix="divisare-vision-n100-") as temp_name:
                temp_dir = Path(temp_name)
                schema_path = temp_dir / "output.schema.json"
                schema_path.write_text(schema_text, encoding="utf-8", newline="\n")
                for position, lane in enumerate(lane_schedule(batch_no), 1):
                    lane_samples = [
                        sample
                        for sample in batch
                        if lane in needed[sample.asset_key] and sample.asset_key in derivatives
                    ]
                    if not lane_samples:
                        continue
                    image_paths: list[Path] = []
                    inference_ids = [sample.sample_id for sample in lane_samples]
                    for sample in lane_samples:
                        path = temp_dir / ("%04d-%s.jpg" % (sample.sample_rank, lane))
                        path.write_bytes(derivatives[sample.asset_key][lane].encoded_bytes)
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
                        working_directory=temp_dir,
                        timeout_seconds=600,
                    )
                    if not result.ok:
                        _write_vision_attempt(
                            conn,
                            lane=lane,
                            batch_no=batch_no,
                            scheduled_position=position,
                            asset_keys=[sample.asset_key for sample in lane_samples],
                            result=result,
                            status="failed",
                        )
                        conn.commit()
                        raise RuntimeError(
                            "Vision N100 batch failed for batch %d/%s: %s"
                            % (batch_no, lane, result.error_message)
                        )
                    try:
                        normalized = normalize_vision_batch(result.records, inference_ids)
                    except Exception as exc:
                        _write_vision_attempt(
                            conn,
                            lane=lane,
                            batch_no=batch_no,
                            scheduled_position=position,
                            asset_keys=[sample.asset_key for sample in lane_samples],
                            result=result,
                            status="failed",
                            error_kind="semantic_schema",
                            error_message=str(exc),
                        )
                        conn.commit()
                        raise
                    _write_vision_attempt(
                        conn,
                        lane=lane,
                        batch_no=batch_no,
                        scheduled_position=position,
                        asset_keys=[sample.asset_key for sample in lane_samples],
                        result=result,
                        status="success",
                    )
                    _write_vision_results(conn, lane, lane_samples, normalized)
                    conn.commit()

        incomplete = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM gold_samples g
                WHERE (SELECT COUNT(*) FROM vision_results r WHERE r.asset_key=g.asset_key)<>2
                """
            ).fetchone()[0]
        )
        if incomplete:
            raise RuntimeError("N100 remains incomplete for %d samples; resume partial sidecar" % incomplete)
        source_after = file_sha256(source_db)
        gold_after = file_sha256(gold_manifest_path)
        validations = _validations(
            conn, source_sha_after=source_after, gold_file_sha_after=gold_after
        )
        _insert_validations(conn, validations)
        hard_failures = [row for row in validations if not row["passed"]]
        if hard_failures:
            conn.execute(
                "UPDATE benchmark_run SET status='failed_validation',source_sha256_after=?,error=? WHERE run_id=1",
                (source_after, canonical_json(hard_failures)),
            )
            conn.commit()
            raise RuntimeError("N100 structural validation failed: %s" % canonical_json(hard_failures))
        classification = _classification_metrics(conn)
        decision = choose_resolution(classification)
        usage = _usage_metrics(conn)
        metrics = {"classification": classification, "decision": decision, "usage": usage}
        status = "complete" if decision["selected_lane"] != "fail" else "failed_quality_gate"
        conn.execute(
            """
            UPDATE benchmark_run SET status=?,source_sha256_after=?,completed_at=?,selected_lane=?,
              decision_reason=?,metrics_json=?,error=? WHERE run_id=1
            """,
            (
                status,
                source_after,
                utc_now(),
                decision["selected_lane"],
                decision["reason"],
                canonical_json(metrics),
                decision["reason"] if decision["selected_lane"] == "fail" else None,
            ),
        )
        conn.commit()
        logical = logical_sha256(conn)
        conn.execute("UPDATE benchmark_run SET logical_sha256=? WHERE run_id=1", (logical,))
        conn.commit()
        partial_report.parent.mkdir(parents=True, exist_ok=True)
        partial_report.write_text(
            render_report(conn, output_db), encoding="utf-8", newline="\n"
        )
    finally:
        conn.close()

    _publish_pair(partial_db, output_db, partial_report, report_path)
    return {
        "output_db": str(output_db),
        "report_path": str(report_path),
        "source_sha256": source_sha,
        "gold_manifest_file_sha256": gold_file_sha,
        "gold_manifest_sha256": gold_payload["gold_manifest_sha256"],
        "logical_sha256": logical,
        "selected_lane": metrics["decision"]["selected_lane"],
        "quality_gate_passed": metrics["decision"]["selected_lane"] != "fail",
        "metrics": metrics,
    }
