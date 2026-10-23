#!/usr/bin/env python
"""vNext WP4 population continuity attestation sample (contract §2.1; rule v1 = v1.2, rule v2 = v1.3).

Promoted from the 2026-10-19 scratch probe ``probe/cont2000.py``.

* Fixed hash-seeded sample: 1,000 E2 ``assets`` with ``fingerprint_status='success'`` per source.
* Fetch profile per source:
  - Architizer: ``https://images.architizer.com`` + ``quote(path, safe="/%")`` +
    ``?width=1024&height=1024&fit=max&quality=85&format=jpg`` (frozen ``source_asset_id`` kept,
    host mapped; the frozen E1 imgix host returns 410).
  - Divisare: the frozen E1 ``fetch_url`` as-is.
* 4 workers, global pacing <= 4 requests/second. Response bytes are fingerprinted in memory and
  never stored.
* Versioned rules (``--rule-version``; each has its own seed and output paths, v1 stays reproducible):
  - v1 (contract v1.2, seed ``vnext-asset-continuity-v1``): ``continuous`` = HTTP 200 and decoded dims
    within +-2px per axis of E1 and pHash-256 Hamming <= 32.
  - v2 (contract v1.3, decision D1 2026-10-23, seed ``vnext-asset-continuity-v2``): ``continuous`` =
    HTTP 200 and Hamming <= 32 and aspect-ratio relative difference <= 2%
    (``|(w*h_e1)/(h*w_e1) - 1|``); ``content_changed`` = HTTP 200 and (Hamming > 32 or aspect > 2%).
  Pixel-SHA equality is reported but not required (Architizer re-encodes on the new host).
* A source is ``continuity_attested`` when continuous/N >= 0.99 and ``content_changed`` == 0.
  404/410 are ``unavailable``: counted in the denominator, reported, not a failure reason.

Inputs are opened read-only/immutable. Outputs are written no-clobber via ``.building`` +
``os.replace``. No Neon, R2, Vision or LLM calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.image_fingerprint import (  # noqa: E402
    FingerprintError,
    PHASH_VERSION,
    PIXEL_HASH_VERSION,
    dependency_versions,
    fingerprint_bytes,
    phash_distance,
)

RULES: dict[str, dict[str, Any]] = {
    "v1": {
        "tool_version": "vnext-asset-continuity-sample-v1",
        "rule_id": "vnext-asset-continuity-rule-v1",
        "seed": "vnext-asset-continuity-v1",
        "contract": "docs/vnext/CONTRACT_GALLERY_SEMANTICS.md §2 v1.2 population continuity attestation",
        "jsonl": Path("data/reports/vnext_asset_continuity_sample_v1.jsonl"),
        "summary": Path("data/reports/vnext_asset_continuity_sample_v1_summary.json"),
    },
    "v2": {
        "tool_version": "vnext-asset-continuity-sample-v2",
        "rule_id": "vnext-asset-continuity-rule-v2",
        "seed": "vnext-asset-continuity-v2",
        "contract": "docs/vnext/CONTRACT_GALLERY_SEMANTICS.md §2.1 v1.3 population continuity attestation",
        "jsonl": Path("data/reports/vnext_asset_continuity_sample_v2.jsonl"),
        "summary": Path("data/reports/vnext_asset_continuity_sample_v2_summary.json"),
    },
}
DEFAULT_RULE = "v2"
TOOL_VERSION = RULES[DEFAULT_RULE]["tool_version"]
SAMPLE_SEED = RULES[DEFAULT_RULE]["seed"]
SAMPLE_SIZE = 1000
SOURCES = ("architizer", "divisare")
WORKERS = 4
MAX_RPS = 4.0
TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
USER_AGENT = "Archibe-Continuity-Sample/1.0"
DIM_TOLERANCE_PX = 2  # rule v1 only
ASPECT_REL_DIFF_MAX = 0.02  # rule v2 only
HAMMING_MAX = 32
CONTINUOUS_RATE_MIN = 0.99
CONTENT_CHANGED_MAX = 0
ARCHITIZER_HOST = "https://images.architizer.com"
ARCHITIZER_PROFILE_QUERY = "width=1024&height=1024&fit=max&quality=85&format=jpg"
FETCH_PROFILES = {
    "architizer": ARCHITIZER_HOST + "{quote(urlsplit(e1_fetch_url).path, safe='/%')}?" + ARCHITIZER_PROFILE_QUERY,
    "divisare": "frozen_e1_fetch_url",
}
UNAVAILABLE_STATUSES = frozenset({404, 410})
E2_PATH = Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db")
EXPECTED_E2_SHA256 = "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19"
EXPECTED_E2_SIZE = 10_164_682_752
DEFAULT_JSONL = RULES[DEFAULT_RULE]["jsonl"]
DEFAULT_SUMMARY = RULES[DEFAULT_RULE]["summary"]
CLASSES = ("continuous", "content_changed", "unavailable", "decode_failed", "fetch_error")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sample_score(source: str, source_asset_id: str, seed: str = SAMPLE_SEED) -> str:
    return hashlib.sha256((seed + "\0" + source + "\0" + source_asset_id).encode("utf-8")).hexdigest()


def request_url(source: str, fetch_url: str) -> str:
    """Return the fetch-profile URL for one frozen asset (no key recomputation)."""

    if source == "architizer":
        return ARCHITIZER_HOST + quote(urlsplit(fetch_url).path, safe="/%") + "?" + ARCHITIZER_PROFILE_QUERY
    if source == "divisare":
        return fetch_url
    raise ValueError(f"unknown source: {source}")


def aspect_rel_diff(wh: Any, e1_wh: Any) -> float:
    """Relative aspect-ratio difference ``|(w/h)/(w_e1/h_e1) - 1|`` (exact, unrounded)."""

    width, height = int(wh[0]), int(wh[1])
    e1_width, e1_height = int(e1_wh[0]), int(e1_wh[1])
    if min(width, height, e1_width, e1_height) <= 0:
        return float("inf")
    return abs((width * e1_height) / (height * e1_width) - 1.0)


def classify(row: Mapping[str, Any], rule_version: str = DEFAULT_RULE) -> str:
    """Pure continuity classification of one fetched row under rule ``v1`` or ``v2``."""

    if rule_version not in RULES:
        raise ValueError(f"unknown continuity rule version: {rule_version}")

    status = row.get("status")
    if status is None:
        return "fetch_error"
    if int(status) in UNAVAILABLE_STATUSES:
        return "unavailable"
    if int(status) != 200:
        return "fetch_error"
    if row.get("wh") is None or row.get("hamming") is None:
        return "decode_failed"
    if rule_version == "v2":
        if int(row["hamming"]) <= HAMMING_MAX and aspect_rel_diff(row["wh"], row["e1_wh"]) <= ASPECT_REL_DIFF_MAX:
            return "continuous"
        return "content_changed"
    width, height = row["wh"]
    e1_width, e1_height = row["e1_wh"]
    dims_ok = abs(int(width) - int(e1_width)) <= DIM_TOLERANCE_PX and abs(int(height) - int(e1_height)) <= DIM_TOLERANCE_PX
    if dims_ok and int(row["hamming"]) <= HAMMING_MAX:
        return "continuous"
    return "content_changed"


def source_verdict(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {name: sum(row["class"] == name for row in rows) for name in CLASSES}
    n = len(rows)
    rate = counts["continuous"] / n if n else 0.0
    hams = sorted(int(row["hamming"]) for row in rows if row.get("hamming") is not None)
    status_counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("status"))
        status_counts[key] = status_counts.get(key, 0) + 1
    attested = n == SAMPLE_SIZE and rate >= CONTINUOUS_RATE_MIN and counts["content_changed"] <= CONTENT_CHANGED_MAX
    return {
        "n": n,
        "class_counts": counts,
        "http_status_counts": dict(sorted(status_counts.items())),
        "continuous_rate": round(rate, 6),
        "unavailable_count": counts["unavailable"],
        "content_changed_count": counts["content_changed"],
        "raw_eq_count": sum(bool(row.get("raw_eq")) for row in rows),
        "pix_eq_count": sum(bool(row.get("pix_eq")) for row in rows),
        "hamming_max": hams[-1] if hams else None,
        "hamming_p50": hams[len(hams) // 2] if hams else None,
        "hamming_p99": hams[min(len(hams) - 1, int(len(hams) * 0.99))] if hams else None,
        "hamming_le8_count": sum(value <= 8 for value in hams),
        "max_abs_dim_delta": max(
            (max(abs(row["wh"][0] - row["e1_wh"][0]), abs(row["wh"][1] - row["e1_wh"][1])) for row in rows if row.get("wh")),
            default=None,
        ),
        "max_aspect_rel_diff": max(
            (round(aspect_rel_diff(row["wh"], row["e1_wh"]), 6) for row in rows if row.get("wh")), default=None,
        ),
        "attempts_total": sum(int(row.get("attempts") or 0) for row in rows),
        "bytes_total": sum(int(row.get("bytes") or 0) for row in rows),
        "continuity_attested": attested,
        "verdict": "continuity_attested" if attested else "not_attested",
    }


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SystemExit(f"refusing to clobber existing artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise SystemExit(f"stale temporary artifact exists: {temporary}")
    with temporary.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_sample(e2_path: Path, seed: str = SAMPLE_SEED) -> list[dict[str, Any]]:
    connection = sqlite3.connect("file:" + e2_path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        run = connection.execute("SELECT run_id FROM e2_runs WHERE status='complete'").fetchall()
        if len(run) != 1:
            raise SystemExit("E2 must have exactly one complete run")
        items: list[dict[str, Any]] = []
        for source in SOURCES:
            rows = connection.execute(
                "SELECT source_asset_id,fetch_url,phash_hex,normalized_pixel_sha256,raw_response_sha256,"
                "original_width,original_height FROM assets WHERE run_id=? AND source=? AND fingerprint_status='success'",
                (run[0][0], source),
            ).fetchall()
            ranked = sorted(rows, key=lambda row: (sample_score(source, str(row[0]), seed), str(row[0])))[:SAMPLE_SIZE]
            if len(ranked) != SAMPLE_SIZE:
                raise SystemExit(f"insufficient E2 success assets for {source}")
            for rank, row in enumerate(ranked, 1):
                items.append(
                    {
                        "source": source,
                        "sample_rank": rank,
                        "source_asset_id": str(row[0]),
                        "sample_score_sha256": sample_score(source, str(row[0]), seed),
                        "e1_fetch_url": row[1],
                        "e1_phash": row[2],
                        "e1_pixel_sha256": row[3],
                        "e1_raw_sha256": row[4],
                        "e1_wh": [int(row[5]), int(row[6])],
                    }
                )
        return items
    finally:
        connection.close()


class Pacer:
    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / rps
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            if delay > 0:
                time.sleep(delay)
            self.next_at = max(now, self.next_at) + self.interval


def fetch_one(item: Mapping[str, Any], pacer: Pacer, local: threading.local, rule_version: str = DEFAULT_RULE) -> dict[str, Any]:
    import requests

    if not hasattr(local, "session"):
        local.session = requests.Session()
        local.session.headers["User-Agent"] = USER_AGENT
    url = request_url(str(item["source"]), str(item["e1_fetch_url"]))
    row: dict[str, Any] = {
        "source": item["source"],
        "sample_rank": item["sample_rank"],
        "source_asset_id": item["source_asset_id"],
        "request_url": url,
        "e1_wh": item["e1_wh"],
        "status": None,
        "bytes": None,
        "wh": None,
        "raw_eq": None,
        "pix_eq": None,
        "hamming": None,
        "attempts": 0,
        "error": None,
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        pacer.wait()
        row["attempts"] = attempt
        started = time.monotonic()
        try:
            response = local.session.get(url, timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # network error: retry once, then fetch_error
            row.update(status=None, error=repr(exc)[:200], seconds=round(time.monotonic() - started, 3))
            continue
        body = response.content
        row.update(
            status=int(response.status_code),
            bytes=len(body),
            seconds=round(time.monotonic() - started, 3),
            content_type=response.headers.get("Content-Type"),
            error=None,
        )
        if response.status_code == 429 or response.status_code >= 500:
            row["error"] = f"http_{response.status_code}"
            continue
        if response.status_code == 200:
            row["raw_eq"] = hashlib.sha256(body).hexdigest() == item["e1_raw_sha256"]
            try:
                fingerprint = fingerprint_bytes(body)
            except FingerprintError as exc:
                row["error"] = "decode:" + exc.kind
            except Exception as exc:  # decoder bug or unsupported image
                row["error"] = "decode:" + type(exc).__name__
            else:
                row["wh"] = [fingerprint.source_width, fingerprint.source_height]
                row["pix_eq"] = fingerprint.pixel_sha256 == item["e1_pixel_sha256"]
                row["hamming"] = phash_distance(fingerprint.phash256, str(item["e1_phash"]))
                if rule_version == "v2":
                    row["aspect_rel_diff"] = round(aspect_rel_diff(row["wh"], row["e1_wh"]), 6)
        break
    row["class"] = classify(row, rule_version)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vNext WP4 continuity attestation sample (HTTP GET only).")
    parser.add_argument("--rule-version", choices=sorted(RULES), default=DEFAULT_RULE,
                        help="continuity rule (own seed + default output paths); v1 reproduces the 2026-10-20 sample")
    parser.add_argument("--jsonl", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--limit-per-source", type=int, default=None, help="smoke only; summary marks partial")
    args = parser.parse_args(argv)
    rule = RULES[args.rule_version]
    args.jsonl = args.jsonl or rule["jsonl"]
    args.summary = args.summary or rule["summary"]
    jsonl = args.jsonl if args.jsonl.is_absolute() else ROOT / args.jsonl
    summary = args.summary if args.summary.is_absolute() else ROOT / args.summary
    for target in (jsonl, summary):
        if target.exists() or target.with_name(target.name + ".building").exists():
            parser.error(f"refusing to clobber existing artifact: {target}")
    e2_path = ROOT / E2_PATH
    started_at = utc_now()
    clock = time.monotonic()
    if e2_path.stat().st_size != EXPECTED_E2_SIZE:
        raise SystemExit("E2 size mismatch")
    e2_before = sha256_file(e2_path)
    if e2_before != EXPECTED_E2_SHA256:
        raise SystemExit("E2 SHA mismatch")
    items = load_sample(e2_path, rule["seed"])
    sample_manifest_sha = canonical_sha256(
        [[row["source"], row["sample_rank"], row["source_asset_id"]] for row in items]
    )
    if args.limit_per_source:
        items = [row for row in items if row["sample_rank"] <= args.limit_per_source]
    pacer = Pacer(MAX_RPS)
    local = threading.local()
    with ThreadPoolExecutor(WORKERS) as pool:
        rows = list(pool.map(lambda item: fetch_one(item, pacer, local, args.rule_version), items))
    elapsed = time.monotonic() - clock
    e2_after = sha256_file(e2_path)
    lines = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    _write_new(jsonl, lines)
    per_source = {source: source_verdict([row for row in rows if row["source"] == source]) for source in SOURCES}
    if args.rule_version == "v1":
        thresholds: dict[str, Any] = {"dim_tolerance_px_per_axis": DIM_TOLERANCE_PX}
    else:
        thresholds = {
            "aspect_ratio_rel_diff_max": ASPECT_REL_DIFF_MAX,
            "aspect_ratio_rel_diff_formula": "abs((w*h_e1)/(h*w_e1) - 1)",
            "content_changed": "HTTP 200 and (hamming > phash256_hamming_max or aspect_ratio_rel_diff > aspect_ratio_rel_diff_max)",
        }
    thresholds.update({
        "phash256_hamming_max": HAMMING_MAX,
        "continuous_rate_min": CONTINUOUS_RATE_MIN,
        "content_changed_max": CONTENT_CHANGED_MAX,
        "unavailable_http_statuses": sorted(UNAVAILABLE_STATUSES),
        "unavailable_policy": "counted in denominator and reported; not an attestation failure reason",
        "pixel_sha_equality_required": False,
    })
    payload = {
        "tool_version": rule["tool_version"],
        "contract": rule["contract"],
        "sample_seed": rule["seed"],
        "sample_size_per_source": SAMPLE_SIZE,
        "sample_scope": "E2 full_v5 assets fingerprint_status='success', ordered by sha256(seed\\0source\\0source_asset_id)",
        "sample_manifest_sha256": sample_manifest_sha,
        "partial_smoke": bool(args.limit_per_source),
        "fetch_profiles": FETCH_PROFILES,
        "thresholds": thresholds,
        "http": {"workers": WORKERS, "max_rps": MAX_RPS, "timeout_seconds": TIMEOUT_SECONDS,
                 "max_attempts": MAX_ATTEMPTS, "user_agent": USER_AGENT, "bytes_stored": False},
        "fingerprint": {"pixel_hash_version": PIXEL_HASH_VERSION, "phash_version": PHASH_VERSION,
                        "dependencies": dependency_versions()},
        "inputs": {"e2": {"path": E2_PATH.as_posix(), "size_bytes": EXPECTED_E2_SIZE,
                          "sha256_before": e2_before, "sha256_after": e2_after}},
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "rows_jsonl": {"path": jsonl.relative_to(ROOT).as_posix() if jsonl.is_relative_to(ROOT) else str(jsonl),
                       "size_bytes": len(lines), "sha256": hashlib.sha256(lines).hexdigest(), "rows": len(rows)},
        "sources": per_source,
        "started_at": started_at,
        "completed_at": utc_now(),
        "elapsed_seconds": round(elapsed, 1),
        "requests_total": sum(int(row.get("attempts") or 0) for row in rows),
        "requests": {"neon": 0, "r2": 0, "vision": 0, "llm": 0},
    }
    if args.rule_version != "v1":
        payload["rule_version"] = rule["rule_id"]
    if e2_after != e2_before:
        payload["status"] = "FAIL_E2_CHANGED"
    else:
        payload["status"] = "PASS" if all(row["continuity_attested"] for row in per_source.values()) else "NOT_ATTESTED"
    _write_new(summary, (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"status": payload["status"], "elapsed_seconds": payload["elapsed_seconds"],
                      "sources": {s: {k: v[k] for k in ("continuous_rate", "class_counts", "verdict")} for s, v in per_source.items()}},
                     ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
