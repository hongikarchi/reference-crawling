#!/usr/bin/env python3
"""Full audit + manual-review dashboard workflow.

This tool is intentionally write-safe:

- local audit/report/snapshot/decision JSON writes only
- dashboard writes local decision JSON only
- applier writes a C24 artifact/patch report only when asked
- Neon access is SELECT-only; no R2 access
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import vocab  # noqa: E402
from tools.canonical_v2_upload_validator import (  # noqa: E402
    MATERIAL_TAXONOMY_NOISE,
    iter_buildings,
)

TODAY = datetime.now().strftime("%Y%m%d")
REPORT_DIR = ROOT / "data/reports" / f"manual_review_{TODAY}"
C23_EMBEDDED = (
    ROOT
    / "data/canonical/country_conflict_refresh/"
    "canonical_buildings_strict_embedded.completeness_c23_final.json"
)
C24_EMBEDDED = (
    ROOT
    / "data/canonical/country_conflict_refresh/"
    "canonical_buildings_strict_embedded.completeness_c24_manual_review.json"
)
ARCHITECTS_ARTIFACT = ROOT / "data/canonical/canonical_architects_v2.json"
SNAPSHOT_PATH = REPORT_DIR / "manual_review_snapshot.json"
DECISIONS_PATH = REPORT_DIR / "review_decisions.json"
PATCH_PATH = REPORT_DIR / "manual_review_c24_patch_report.json"
DB_AUDIT_JSON = REPORT_DIR / "db_audit.json"
DB_AUDIT_MD = REPORT_DIR / "db_audit.md"
CODE_AUDIT_JSON = REPORT_DIR / "code_audit.json"
CODE_AUDIT_MD = REPORT_DIR / "code_audit.md"

DEFAULT_ACTIONS = [
    "keep",
    "update_field",
    "set_cover_to_image",
    "unpublish",
    "merge",
    "split",
    "needs_fix",
    "unsure",
]
FAST_TERM_ACTIONS = [
    "material",
    "architectural_element",
    "typology",
    "search_keyword",
    "delete",
    "unsure",
]
FAST_VOCAB_ACTIONS = [
    "approve",
    "edit",
    "unsure",
]
VOCAB_LABELS_PATH = ROOT / "data/canonical/tag_vocabulary_labels.json"
VOCAB_EXAMPLES_PATH = ROOT / "data/reports/vocab_label_examples.json"
VOCAB_AXES = (
    "program", "style", "color_tone", "atmosphere",
    "material_visual", "architectural_elements",
    "era", "scale", "structural_system", "roof_type", "facade_pattern",
)
FAST_CASE_PRIORITY = {
    "country": 0,
    "year": 1,
    "cover": 2,
    "seo": 3,
    "architect": 4,
    "series": 5,
    "crawl": 6,
}
FAST_TERM_EXAMPLE_LIMIT = 5
SAFE_UPDATE_FIELDS = {
    "name",
    "location_country",
    "location_city",
    "project_year",
    "year_kind",
    "architects_text",
    "architect_names",
    "architect_canonical_ids",
    "program",
    "style",
    "color_tone",
    "atmosphere",
    "material_visual",
    "typology_primary",
    "typology_tags",
    "architectural_elements",
    "display_cover_url",
    "cover_image_url_default",
}
ISSUE_TABS = {
    "country_conflict": "country",
    "year_conflict": "year",
    "series_pavilion": "series",
    "split_suspect": "series",
    "gallery_phash": "cover",
    "cover_phash": "cover",
    "gallery_image": "cover",
    "seo_name": "seo",
    "name_quality": "seo",
    "architect_unknown": "architect",
    "material_noise": "material",
    "material_unmapped": "material",
    "d1_uncertainty": "d1",
    "d2_oov": "d2",
    "source_url_gap": "crawl",
    "source_ref_gap": "crawl",
    "audit_summary": "crawl",
}
SIDECARS = [
    ("country_conflict", ROOT / "data/reports/canonical_v2_c23_country_conflict_sidecar.jsonl"),
    ("year_conflict", ROOT / "data/reports/canonical_v2_c23_year_conflict_sidecar.jsonl"),
    ("series_pavilion", ROOT / "data/reports/canonical_v2_c23_series_pavilion_sidecar.jsonl"),
    ("gallery_phash", ROOT / "data/reports/canonical_v2_c23_gallery_phash_sidecar.jsonl"),
    ("seo_name", ROOT / "data/reports/canonical_v2_c23_seo_candidate_sidecar.jsonl"),
]
IMAGE_DERIVED_VOCAB = {
    "style": set(vocab.STYLE),
    "color_tone": set(vocab.COLOR_TONE),
    "atmosphere": set(vocab.ATMOSPHERE),
    "program": set(vocab.PROGRAM),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> str:
    return str(value)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=json_default)
        f.write("\n")
        tmp = Path(f.name)
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def parse_extra_issue_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected ISSUE_CODE=PATH")
    issue_code, raw_path = value.split("=", 1)
    issue_code = issue_code.strip()
    if not issue_code:
        raise argparse.ArgumentTypeError("issue code is required")
    return issue_code, Path(raw_path)


def normalize_review_term(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=json_default)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _rel(path: Path | str) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(ROOT))
    except Exception:
        return str(path)


def _run(cmd: list[str], *, timeout: int = 120) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as exc:  # noqa: BLE001
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}


def _first_present(data: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []):
            return value
    return None


def _brief_target(item: dict[str, Any], cid: str | None) -> dict[str, Any]:
    rows = item.get("rows")
    if isinstance(rows, list) and rows:
        first = rows[0] if isinstance(rows[0], dict) else {}
    else:
        first = item
    return {
        "canonical_bld_id": cid,
        "name": _first_present(first, ["name", "canonical_name", "title"]),
        "architect_names": first.get("architect_names") or first.get("architects") or [],
        "location_country": first.get("country") or first.get("location_country") or item.get("row_country"),
        "location_city": first.get("city") or first.get("location_city") or item.get("row_city"),
        "project_year": first.get("year") or first.get("project_year") or item.get("row_year"),
        "source_refs": first.get("source_refs") or item.get("source_refs") or {},
        "source_urls": first.get("source_urls") or item.get("source_urls") or {},
        "display_cover_url": first.get("display_cover_url") or item.get("display_cover_url"),
        "images": first.get("images") or item.get("images") or [],
    }


def _target_from_artifact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_bld_id": row.get("canonical_bld_id"),
        "name": row.get("name"),
        "architect_names": row.get("architect_names") or [],
        "location_country": row.get("location_country"),
        "location_city": row.get("location_city"),
        "project_year": row.get("project_year"),
        "year_kind": row.get("year_kind"),
        "program": row.get("program"),
        "typology_primary": row.get("typology_primary"),
        "source_refs": row.get("source_refs") or {},
        "source_urls": row.get("source_urls") or {},
        "display_cover_url": row.get("display_cover_url"),
        "images": row.get("all_images") or [],
    }


def _merge_target_context(existing: dict[str, Any], artifact_target: dict[str, Any]) -> dict[str, Any]:
    merged = dict(artifact_target)
    for key, value in (existing or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    for key in ("source_urls", "images", "display_cover_url"):
        if artifact_target.get(key) not in (None, "", [], {}):
            merged[key] = artifact_target[key]
    return merged


def enrich_cases_from_artifact(cases: list[dict[str, Any]], artifact_path: Path) -> None:
    needed = {
        str(case.get("target_canonical_bld_id") or "")
        for case in cases
        if case.get("target_canonical_bld_id")
    }
    if not needed or not artifact_path.exists():
        return
    rows: dict[str, dict[str, Any]] = {}
    for row in iter_buildings(artifact_path):
        cid = str(row.get("canonical_bld_id") or "")
        if cid in needed:
            rows[cid] = _target_from_artifact_row(row)
            if len(rows) == len(needed):
                break
    for case in cases:
        cid = str(case.get("target_canonical_bld_id") or "")
        artifact_target = rows.get(cid)
        if not artifact_target:
            continue
        case["target"] = _merge_target_context(case.get("target") or {}, artifact_target)


def normalize_ambiguous_item(
    *,
    source_path: str,
    issue_code: str,
    item: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    cid = _first_present(
        item,
        [
            "target_canonical_bld_id",
            "canonical_bld_id",
            "cid",
            "bld_id",
            "building_id",
            "survivor",
            "winner_cid",
        ],
    )
    if not cid and isinstance(item.get("rows"), list) and item["rows"]:
        first = item["rows"][0]
        if isinstance(first, dict):
            cid = _first_present(first, ["cid", "canonical_bld_id", "target_canonical_bld_id"])
    cid_text = str(cid) if cid else f"group_{index}"
    case_id = f"{issue_code}:{cid_text}:{_stable_hash([source_path, index, item])}"
    tab = ISSUE_TABS.get(issue_code, "crawl")
    target = _brief_target(item, str(cid) if cid else None)
    title = target.get("name") or item.get("key") or case_id
    return {
        "case_id": case_id,
        "tab": tab,
        "issue_code": issue_code,
        "target_canonical_bld_id": str(cid) if cid else None,
        "title": title,
        "subtitle": item.get("evidence_label") or item.get("key") or _rel(source_path),
        "severity": "manual_review",
        "allowed_actions": list(DEFAULT_ACTIONS),
        "source_path": source_path,
        "source_index": index,
        "target": target,
        "evidence": item,
    }


def _case_from_row(
    row: dict[str, Any],
    issue_code: str,
    evidence: dict[str, Any],
    *,
    source_artifact: Path = C23_EMBEDDED,
) -> dict[str, Any]:
    item = {
        "cid": row.get("canonical_bld_id"),
        "name": row.get("name"),
        "country": row.get("location_country"),
        "city": row.get("location_city"),
        "year": row.get("project_year"),
        "architect_names": row.get("architect_names") or [],
        "source_refs": row.get("source_refs") or {},
        "display_cover_url": row.get("display_cover_url"),
        "images": row.get("all_images") or [],
        **evidence,
    }
    return normalize_ambiguous_item(
        source_path=_rel(source_artifact),
        issue_code=issue_code,
        item=item,
        index=int(hashlib.sha1(str(row.get("canonical_bld_id")).encode()).hexdigest()[:8], 16),
    )


def _parse_jsonish(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return value


def _sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _sqlite_scalar(conn: sqlite3.Connection, sql: str) -> Any:
    try:
        return conn.execute(sql).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def audit_sqlite_databases() -> dict[str, Any]:
    db_paths = sorted((ROOT / "data/crawl").glob("*.db")) + [ROOT / "data/enrich/tasks.db"]
    databases = []
    totals = Counter()
    for path in db_paths:
        entry: dict[str, Any] = {"path": _rel(path), "exists": path.exists(), "tables": []}
        if not path.exists():
            entry["status"] = "missing"
            databases.append(entry)
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
            conn.row_factory = sqlite3.Row
            tables = [
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                if not str(r[0]).startswith("sqlite_")
            ]
            for table in tables:
                cols = [dict(r) for r in conn.execute(f'PRAGMA table_info("{table}")')]
                col_names = [c["name"] for c in cols]
                row_count = _sqlite_count(conn, table)
                totals["tables"] += 1
                totals["rows"] += row_count
                t: dict[str, Any] = {
                    "name": table,
                    "row_count": row_count,
                    "columns": col_names,
                    "status_counts": {},
                    "null_or_empty_counts": {},
                    "duplicate_counts": {},
                }
                if "status" in col_names:
                    try:
                        t["status_counts"] = {
                            str(r[0]): int(r[1])
                            for r in conn.execute(
                                f'SELECT status, COUNT(*) FROM "{table}" GROUP BY status ORDER BY status'
                            )
                        }
                    except Exception as exc:  # noqa: BLE001
                        t["status_counts"] = {"error": str(exc)}
                for col in ("name", "title", "url", "source_url", "cover_image_url", "location_country", "location_city"):
                    if col in col_names:
                        t["null_or_empty_counts"][col] = _sqlite_scalar(
                            conn,
                            f'SELECT COUNT(*) FROM "{table}" '
                            f'WHERE "{col}" IS NULL OR TRIM(CAST("{col}" AS TEXT)) = ""',
                        )
                for col in ("id", "global_id", "slug", "url", "source_url", "cover_image_url"):
                    if col in col_names:
                        t["duplicate_counts"][col] = _sqlite_scalar(
                            conn,
                            f'SELECT COUNT(*) FROM (SELECT "{col}" FROM "{table}" '
                            f'WHERE "{col}" IS NOT NULL AND TRIM(CAST("{col}" AS TEXT)) <> "" '
                            f'GROUP BY "{col}" HAVING COUNT(*) > 1)',
                        )
                entry["tables"].append(t)
            conn.close()
            entry["status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            entry.update(status="error", error=str(exc))
        databases.append(entry)
    return {"status": "PASS", "databases": databases, "totals": dict(totals)}


def _row_image_urls(row: dict[str, Any]) -> set[str]:
    urls = set()
    for image in row.get("all_images") or []:
        if isinstance(image, dict) and image.get("url"):
            urls.add(str(image["url"]))
    return urls


def _image_derived_oov(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for field, allowed in IMAGE_DERIVED_VOCAB.items():
        raw = value.get(field)
        vals = raw if isinstance(raw, list) else [raw]
        bad = sorted({str(v) for v in vals if v and str(v) not in allowed})
        if bad:
            out[field] = bad
    return out


def audit_buildings_artifact(path: Path = C23_EMBEDDED) -> tuple[dict[str, Any], dict[str, Any]]:
    counters = Counter()
    warnings = Counter()
    field_nulls = Counter()
    oov = Counter()
    samples: dict[str, list[Any]] = defaultdict(list)
    display_cover_owners: dict[str, list[str]] = defaultdict(list)
    name_groups: dict[tuple[str, str, Any], list[str]] = defaultdict(list)
    building_ids: set[str] = set()
    arch_refs: dict[str, set[str]] = defaultdict(set)
    generated_cases: list[dict[str, Any]] = []

    for row in iter_buildings(path):
        counters["rows_total"] += 1
        cid = str(row.get("canonical_bld_id") or "")
        if cid:
            building_ids.add(cid)
        is_pub = bool(row.get("is_publishable"))
        counters["rows_publishable" if is_pub else "rows_nonpublishable"] += 1
        for field in ("location_country", "location_city", "project_year", "display_cover_url"):
            if row.get(field) in (None, "", []):
                field_nulls[field] += 1
        for field, allowed in (
            ("program", set(vocab.PROGRAM)),
            ("style", set(vocab.STYLE)),
            ("color_tone", set(vocab.COLOR_TONE)),
            ("atmosphere", set(vocab.ATMOSPHERE)),
        ):
            value = row.get(field)
            if value not in allowed:
                oov[field] += 1
                if len(samples[f"oov_{field}"]) < 20:
                    samples[f"oov_{field}"].append({"cid": cid, "value": value})
        emb = row.get("embedding")
        if not isinstance(emb, list) or len(emb) != 384:
            counters["bad_embedding_dim"] += 1
            if len(samples["bad_embedding_dim"]) < 20:
                samples["bad_embedding_dim"].append(cid)
        refs = row.get("source_refs") or {}
        urls = row.get("source_urls") or {}
        if not refs:
            counters["missing_source_refs"] += 1
        for source, ids in refs.items():
            if ids and not urls.get(source):
                counters["source_url_gap"] += 1
                generated_cases.append(
                    _case_from_row(
                        row,
                        "source_url_gap",
                        {"source": source, "source_ids": ids},
                        source_artifact=path,
                    )
                )
        arch_ids = row.get("architect_canonical_ids") or []
        arch_names = row.get("architect_names") or []
        for arch_id in arch_ids:
            arch_refs[str(arch_id)].add(cid)
        arch_text = " ".join(str(x) for x in arch_names) + " " + str(row.get("architects_text") or "")
        if is_pub and (not arch_ids or re.search(r"\b(n/?a|unknown|anonymous)\b", arch_text, re.I)):
            counters["architect_unknown_publishable"] += 1
            generated_cases.append(
                _case_from_row(row, "architect_unknown", {"architect_text": arch_text.strip()}, source_artifact=path)
            )
        raw_material = row.get("material_visual") or []
        bad_material = sorted(
            {
                str(v)
                for v in raw_material
                if isinstance(v, str) and v.strip().lower() in MATERIAL_TAXONOMY_NOISE
            }
        )
        if bad_material:
            counters["material_noise_rows"] += 1
            generated_cases.append(
                _case_from_row(row, "material_noise", {"material_noise": bad_material}, source_artifact=path)
            )
        d2_oov = _image_derived_oov(row.get("image_derived"))
        if d2_oov:
            counters["d2_oov_rows"] += 1
            generated_cases.append(
                _case_from_row(row, "d2_oov", {"image_derived_oov": d2_oov}, source_artifact=path)
            )
        cover = row.get("display_cover_url")
        if is_pub and cover:
            display_cover_owners[str(cover)].append(cid)
            if cover not in _row_image_urls(row):
                counters["publishable_cover_not_in_all_images"] += 1
                if len(samples["publishable_cover_not_in_all_images"]) < 20:
                    samples["publishable_cover_not_in_all_images"].append({"cid": cid, "cover": cover})
        if is_pub and (not row.get("all_images") or not cover):
            counters["publishable_missing_image"] += 1
        name_key = (
            re.sub(r"\s+", " ", str(row.get("name") or "").casefold()).strip(),
            str(row.get("location_country") or ""),
            row.get("project_year"),
        )
        if name_key[0]:
            name_groups[name_key].append(cid)

    reused_covers = {url: cids for url, cids in display_cover_owners.items() if len(cids) > 1}
    duplicate_name_groups = {k: v for k, v in name_groups.items() if len(v) > 1}
    warnings["display_cover_url_reused_groups"] = len(reused_covers)
    warnings["duplicate_name_country_year_groups"] = len(duplicate_name_groups)
    sidecar_counts = {
        issue_code: {"path": _rel(path), "rows": len(load_jsonl(path)), "exists": path.exists()}
        for issue_code, path in SIDECARS
    }
    artifact_report = ROOT / "data/reports/canonical_v2_c23_final_report.json"
    c23_report = read_json(artifact_report) if artifact_report.exists() else None
    audit = {
        "path": _rel(path),
        "status": "PASS" if not oov and counters.get("bad_embedding_dim", 0) == 0 else "WARN",
        "counts": dict(counters),
        "field_nulls": dict(field_nulls),
        "oov": dict(oov),
        "warnings": dict(warnings),
        "sidecar_counts": sidecar_counts,
        "c23_report": c23_report,
        "samples": dict(samples),
    }
    index = {
        "building_ids": building_ids,
        "arch_refs": arch_refs,
        "generated_cases": generated_cases,
    }
    return audit, index


def audit_architects_artifact(
    *,
    path: Path = ARCHITECTS_ARTIFACT,
    building_ids: set[str],
    arch_refs: dict[str, set[str]],
) -> dict[str, Any]:
    if not path.exists():
        return {"path": _rel(path), "status": "missing"}
    data = read_json(path)
    rows = data.get("architects") or []
    counters = Counter()
    samples: dict[str, list[Any]] = defaultdict(list)
    seen: set[str] = set()
    arch_building_ids: dict[str, set[str]] = {}
    for row in rows:
        counters["rows_total"] += 1
        aid = str(row.get("canonical_arch_id") or "")
        if not aid or aid in seen:
            counters["duplicate_or_missing_arch_id"] += 1
        seen.add(aid)
        if row.get("is_recommendable"):
            counters["recommendable"] += 1
        emb = row.get("portfolio_embedding")
        if not isinstance(emb, list) or len(emb) != 384:
            counters["bad_portfolio_embedding_dim"] += 1
            if len(samples["bad_portfolio_embedding_dim"]) < 20:
                samples["bad_portfolio_embedding_dim"].append(aid)
        bids = {str(x) for x in row.get("building_ids") or []}
        arch_building_ids[aid] = bids
        missing = sorted(bids - building_ids)
        if missing:
            counters["building_ids_missing_in_buildings"] += len(missing)
            if len(samples["building_ids_missing_in_buildings"]) < 20:
                samples["building_ids_missing_in_buildings"].append({"arch_id": aid, "building_ids": missing[:10]})
        if row.get("n_buildings") != len(bids):
            counters["n_buildings_mismatch"] += 1
        expected_recommendable = (
            int(row.get("n_buildings_publishable") or 0) >= 3
            and bool(row.get("website") or row.get("description") or row.get("primary_country"))
        )
        if bool(row.get("is_recommendable")) != expected_recommendable:
            counters["is_recommendable_mismatch"] += 1
    for aid, expected_bids in arch_refs.items():
        if aid not in seen:
            counters["building_arch_id_missing_in_architects"] += 1
            if len(samples["building_arch_id_missing_in_architects"]) < 20:
                samples["building_arch_id_missing_in_architects"].append(aid)
            continue
        missing_reverse = sorted(expected_bids - arch_building_ids.get(aid, set()))
        if missing_reverse:
            counters["reciprocal_building_missing_from_architect"] += len(missing_reverse)
            if len(samples["reciprocal_building_missing_from_architect"]) < 20:
                samples["reciprocal_building_missing_from_architect"].append(
                    {"arch_id": aid, "building_ids": missing_reverse[:10]}
                )
    return {
        "path": _rel(path),
        "status": "PASS" if not any(k.endswith("mismatch") for k in counters) else "WARN",
        "counts": dict(counters),
        "samples": dict(samples),
    }


def audit_neon(building_index_path: Path = C23_EMBEDDED) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "not_run", "writes": "none; SELECT-only"}
    try:
        import psycopg2.extras  # noqa: F401
        from tools.canonical_v2_neon_loader import _connect
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"import failed: {exc}", "writes": "none"}

    try:
        conn = _connect()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for table in ("canonical_v2_buildings", "canonical_v2_architects"):
            cur.execute(
                """
                SELECT column_name, data_type, udt_name, is_nullable
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
                """,
                (table,),
            )
            report[f"{table}_schema"] = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT COUNT(*) total,
                   COUNT(*) FILTER (WHERE is_publishable) publishable,
                   COUNT(*) FILTER (WHERE embedding IS NULL) missing_embedding,
                   COUNT(*) FILTER (WHERE display_cover_url IS NULL) missing_display_cover_url,
                   COUNT(*) FILTER (WHERE needs_image_derived_backfill) needs_image_derived_backfill
            FROM canonical_v2_buildings
            """
        )
        report["canonical_v2_buildings_counts"] = dict(cur.fetchone())
        cur.execute("SELECT confidence_tier, COUNT(*) n FROM canonical_v2_buildings GROUP BY confidence_tier ORDER BY confidence_tier")
        report["building_confidence_tier"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT year_kind, COUNT(*) n FROM canonical_v2_buildings GROUP BY year_kind ORDER BY year_kind")
        report["building_year_kind"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT COUNT(*) duplicate_cover_rows
            FROM canonical_v2_buildings
            WHERE is_publishable AND display_cover_url IN (
                SELECT display_cover_url
                FROM canonical_v2_buildings
                WHERE is_publishable AND display_cover_url IS NOT NULL
                GROUP BY display_cover_url HAVING COUNT(*) > 1
            )
            """
        )
        report["building_cover_duplication"] = dict(cur.fetchone())
        cur.execute(
            """
            SELECT COUNT(*) rows_with_material_noise
            FROM canonical_v2_buildings b
            WHERE EXISTS (
              SELECT 1 FROM unnest(b.material_visual) m
              WHERE lower(trim(m)) = ANY(%s)
            )
            """,
            (list(MATERIAL_TAXONOMY_NOISE),),
        )
        report["building_material_noise"] = dict(cur.fetchone())
        cur.execute(
            """
            SELECT COUNT(*) total,
                   COUNT(*) FILTER (WHERE is_recommendable) recommendable,
                   COUNT(*) FILTER (WHERE portfolio_embedding IS NULL) missing_embedding,
                   COUNT(*) FILTER (WHERE n_buildings_publishable >= 3) n_buildings_publishable_gte_3
            FROM canonical_v2_architects
            """
        )
        report["canonical_v2_architects_counts"] = dict(cur.fetchone())
        cur.execute(
            """
            SELECT COUNT(*) missing_building_refs
            FROM canonical_v2_architects a
            CROSS JOIN LATERAL unnest(a.building_ids) bid
            LEFT JOIN canonical_v2_buildings b ON b.canonical_bld_id = bid
            WHERE b.canonical_bld_id IS NULL
            """
        )
        report["architect_missing_building_refs"] = dict(cur.fetchone())
        cur.execute(
            """
            SELECT COUNT(*) missing_architect_refs
            FROM canonical_v2_buildings b
            CROSS JOIN LATERAL unnest(b.architect_canonical_ids) aid
            LEFT JOIN canonical_v2_architects a ON a.canonical_arch_id = aid
            WHERE a.canonical_arch_id IS NULL
            """
        )
        report["building_missing_architect_refs"] = dict(cur.fetchone())
        cur.execute(
            """
            SELECT canonical_bld_id, name, is_publishable, location_country,
                   project_year, year_kind, display_cover_url
            FROM canonical_v2_buildings
            ORDER BY canonical_bld_id
            """
        )
        neon_rows = {str(r["canonical_bld_id"]): dict(r) for r in cur.fetchall()}
        mismatches = []
        artifact_count = 0
        for row in iter_buildings(building_index_path):
            artifact_count += 1
            cid = str(row.get("canonical_bld_id"))
            nrow = neon_rows.get(cid)
            if not nrow:
                if len(mismatches) < 100:
                    mismatches.append({"cid": cid, "field": "missing_in_neon"})
                continue
            for field in ("name", "is_publishable", "location_country", "project_year", "year_kind", "display_cover_url"):
                if nrow.get(field) != row.get(field):
                    if len(mismatches) < 100:
                        mismatches.append(
                            {"cid": cid, "field": field, "artifact": row.get(field), "neon": nrow.get(field)}
                        )
                    break
        report["artifact_vs_neon"] = {
            "artifact_rows": artifact_count,
            "neon_rows_selected": len(neon_rows),
            "missing_in_artifact": len(set(neon_rows) - {str(r.get("canonical_bld_id")) for r in iter_buildings(building_index_path)}),
            "mismatch_sample": mismatches,
            "sample_limit": 100,
        }
        cur.close()
        conn.close()
        report["status"] = "PASS" if not mismatches else "WARN"
    except Exception as exc:  # noqa: BLE001
        report.update(status="error", error=str(exc))
    return report


def audit_codebase() -> dict[str, Any]:
    files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    py_files = [p for p in files if p.suffix == ".py"]
    components = {
        "crawl": sorted(_rel(p) for p in (ROOT / "crawl").rglob("*.py")),
        "enrich": sorted(_rel(p) for p in (ROOT / "enrich").rglob("*.py")),
        "canonical": sorted(_rel(p) for p in (ROOT / "canonical").rglob("*.py")),
        "tools": sorted(_rel(p) for p in (ROOT / "tools").glob("*.py")),
        "tests": sorted(_rel(p) for p in (ROOT / "tests").glob("test_*.py")),
    }
    stale_refs = []
    patterns = [
        ("completeness_c8", "C8 artifact pin; C23 final is production truth"),
        ("resume10_complete", "resume10 default; C23 final is production truth"),
        ("39,776", "stale C8 row count; C23 final has 39,478 rows"),
    ]
    scan_paths = [ROOT / "README.md", ROOT / "docs/REFERENCE.md", ROOT / "docs/dashboard.html", ROOT / "tools/build_dashboard.py"]
    for path in scan_paths:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, 1):
            for needle, reason in patterns:
                if needle in line:
                    stale_refs.append({"path": _rel(path), "line": lineno, "needle": needle, "reason": reason})
    gates = {}
    for rel in (
        "tools/canonical_v2_neon_loader.py",
        "tools/canonical_v2_architects_neon_loader.py",
        "tools/d1_enrich_codex.py",
        "tools/d2_cover_vision.py",
        "tools/cover_review_app.py",
    ):
        path = ROOT / rel
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        gates[rel] = {
            "exists": path.exists(),
            "confirm_db_write_gate": "--confirm-db-write" in text,
            "dry_run_mode": "dry-run" in text or "dry_run" in text,
            "limit_arg": "--limit" in text,
            "local_decision_only": "decision" in text and "Neon" in text and "never" in text.lower(),
        }
    git_status = _run(["git", "status", "--short", "--branch"])
    deleted = []
    modified = []
    untracked = []
    for line in git_status["stdout"].splitlines():
        if not line or line.startswith("##"):
            continue
        status = line[:2]
        path = line[3:]
        if "D" in status:
            deleted.append(path)
        elif status == "??":
            untracked.append(path)
        else:
            modified.append(path)
    return {
        "status": "PASS",
        "python_files": len(py_files),
        "components": {k: {"count": len(v), "files": v[:80]} for k, v in components.items()},
        "pipeline_map": {
            "crawl": ["crawl/divisare", "crawl/architizer", "crawl/archello", "crawl/metalocus"],
            "enrich": ["tools/d1_enrich_codex.py", "tools/d2_cover_vision.py", "tools/e1_phash_dedup.py", "tools/e2_vision_5type.py"],
            "canonical": ["tools/build_strict_canonical.py", "tools/canonical_v2_c23_final.py"],
            "architects": ["tools/canonical_v2_architects_build.py", "tools/canonical_v2_architects_audit.py"],
            "validation_upload": ["tools/canonical_v2_upload_validator.py", "tools/canonical_v2_neon_loader.py", "tools/canonical_v2_architects_neon_loader.py"],
            "dashboard_review": ["tools/build_dashboard.py", "tools/cover_review_app.py", "tools/manual_review_workflow.py"],
        },
        "stale_references": stale_refs,
        "write_and_cost_gates": gates,
        "dirty_tree": {
            "branch_line": git_status["stdout"].splitlines()[0] if git_status["stdout"].splitlines() else "",
            "modified": modified,
            "deleted": deleted,
            "untracked": untracked,
            "note": "Pre-existing dirty state recorded; workflow did not revert it.",
        },
    }


def _md_table(rows: list[list[Any]]) -> list[str]:
    if not rows:
        return []
    out = ["| " + " | ".join(str(x) for x in rows[0]) + " |"]
    out.append("|" + "|".join("---" for _ in rows[0]) + "|")
    for row in rows[1:]:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return out


def write_db_audit_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Manual Review DB Audit",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- writes: {report['writes']}",
        f"- scope: local SQLite crawl/enrich DBs, C23 artifact, architects artifact, Neon archi_data when reachable",
        "",
        "## Summary",
        "",
    ]
    artifact = report.get("artifact", {})
    arch = report.get("architects_artifact", {})
    neon = report.get("neon", {})
    lines += _md_table(
        [
            ["Area", "Status", "Key Counts"],
            ["SQLite", report.get("sqlite", {}).get("status"), report.get("sqlite", {}).get("totals")],
            ["C23 buildings artifact", artifact.get("status"), artifact.get("counts")],
            ["Architects artifact", arch.get("status"), arch.get("counts")],
            ["Neon archi_data", neon.get("status"), neon.get("canonical_v2_buildings_counts")],
        ]
    )
    lines += [
        "",
        "## C23 Sidecars",
        "",
    ]
    sidecars = artifact.get("sidecar_counts") or {}
    lines += _md_table([["Issue", "Rows", "Path"]] + [[k, v.get("rows"), v.get("path")] for k, v in sidecars.items()])
    lines += [
        "",
        "## Ambiguity Signals",
        "",
        f"- material_noise_rows: {artifact.get('counts', {}).get('material_noise_rows', 0)}",
        f"- architect_unknown_publishable: {artifact.get('counts', {}).get('architect_unknown_publishable', 0)}",
        f"- d2_oov_rows: {artifact.get('counts', {}).get('d2_oov_rows', 0)}",
        f"- source_url_gap: {artifact.get('counts', {}).get('source_url_gap', 0)}",
        "",
    ]
    if neon.get("error"):
        lines += ["## Neon Read Error", "", neon["error"], ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_code_audit_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Manual Review Code Audit",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- writes: {report['writes']}",
        "",
        "## Pipeline Map",
        "",
    ]
    for name, items in report["pipeline_map"].items():
        lines.append(f"- {name}: {', '.join(items)}")
    lines += ["", "## Stale References", ""]
    stale = report.get("stale_references") or []
    if stale:
        lines += _md_table([["Path", "Line", "Needle", "Reason"]] + [[s["path"], s["line"], s["needle"], s["reason"]] for s in stale])
    else:
        lines.append("- none detected")
    lines += [
        "",
        "## Write Gates",
        "",
    ]
    lines += _md_table(
        [["Tool", "Confirm Gate", "Dry Run", "Limit"]]
        + [[k, v["confirm_db_write_gate"], v["dry_run_mode"], v["limit_arg"]] for k, v in report["write_and_cost_gates"].items()]
    )
    dirty = report.get("dirty_tree") or {}
    lines += [
        "",
        "## Dirty Tree At Preflight",
        "",
        f"- branch: {dirty.get('branch_line')}",
        f"- modified: {len(dirty.get('modified') or [])}",
        f"- deleted: {len(dirty.get('deleted') or [])}",
        f"- untracked: {len(dirty.get('untracked') or [])}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(*, include_neon: bool = True, output_dir: Path = REPORT_DIR) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    sqlite_report = audit_sqlite_databases()
    artifact_report, artifact_index = audit_buildings_artifact(C23_EMBEDDED)
    architects_report = audit_architects_artifact(
        building_ids=artifact_index["building_ids"],
        arch_refs=artifact_index["arch_refs"],
    )
    neon_report = audit_neon(C23_EMBEDDED) if include_neon else {"status": "skipped", "writes": "none"}
    db_report = {
        "generated_at": now_iso(),
        "writes": "local report writes only; Neon SELECT-only when included",
        "sqlite": sqlite_report,
        "artifact": artifact_report,
        "architects_artifact": architects_report,
        "neon": neon_report,
    }
    code_report = {
        "generated_at": now_iso(),
        "writes": "local report writes only",
        **audit_codebase(),
    }
    atomic_write_json(output_dir / "db_audit.json", db_report)
    atomic_write_json(output_dir / "code_audit.json", code_report)
    write_db_audit_markdown(db_report, output_dir / "db_audit.md")
    write_code_audit_markdown(code_report, output_dir / "code_audit.md")
    return {
        "db_audit": db_report,
        "code_audit": code_report,
        "generated_cases": artifact_index["generated_cases"],
        "paths": {
            "db_audit_json": _rel(output_dir / "db_audit.json"),
            "db_audit_md": _rel(output_dir / "db_audit.md"),
            "code_audit_json": _rel(output_dir / "code_audit.json"),
            "code_audit_md": _rel(output_dir / "code_audit.md"),
        },
    }


def _load_cover_review_cases() -> list[dict[str, Any]]:
    path = ROOT / "data/reports/audit_2026-05-27/cover_review_snapshot.json"
    if not path.exists():
        return []
    data = read_json(path)
    cases = []
    for idx, case in enumerate(data.get("cases") or []):
        issue = "cover_phash" if "COVER" in str(case.get("issue_code")) else "gallery_image"
        cases.append(
            normalize_ambiguous_item(
                source_path=_rel(path),
                issue_code=issue,
                item=case,
                index=idx,
            )
        )
    return cases


def build_snapshot(
    *,
    output_path: Path = SNAPSHOT_PATH,
    decisions_path: Path = DECISIONS_PATH,
    source_artifact: Path = C23_EMBEDDED,
    extra_issue_paths: list[tuple[str, Path]] | None = None,
    audit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for issue_code, path in SIDECARS:
        for idx, item in enumerate(load_jsonl(path)):
            cases.append(
                normalize_ambiguous_item(
                    source_path=_rel(path),
                    issue_code=issue_code,
                    item=item,
                    index=idx,
                )
            )
    for issue_code, path in extra_issue_paths or []:
        for idx, item in enumerate(load_jsonl(path)):
            cases.append(
                normalize_ambiguous_item(
                    source_path=_rel(path),
                    issue_code=issue_code,
                    item=item,
                    index=idx,
                )
            )
    cases.extend(_load_cover_review_cases())
    if audit_result is None:
        _, artifact_index = audit_buildings_artifact(source_artifact)
        cases.extend(artifact_index["generated_cases"])
    else:
        cases.extend(audit_result.get("generated_cases") or [])

    deduped: dict[str, dict[str, Any]] = {}
    for case in cases:
        deduped[case["case_id"]] = case
    ordered = sorted(deduped.values(), key=lambda c: (c["tab"], c["issue_code"], c.get("target_canonical_bld_id") or "", c["case_id"]))
    enrich_cases_from_artifact(ordered, source_artifact)
    counts = Counter(c["tab"] for c in ordered)
    by_issue = Counter(c["issue_code"] for c in ordered)
    snapshot = {
        "version": 1,
        "generated_at": now_iso(),
        "snapshot_path": _rel(output_path),
        "source_artifact": _rel(source_artifact),
        "db_writes": "none",
        "counts": {
            "total_cases": len(ordered),
            "by_tab": dict(sorted(counts.items())),
            "by_issue": dict(sorted(by_issue.items())),
        },
        "decision_path": _rel(decisions_path),
        "cases": ordered,
    }
    atomic_write_json(output_path, snapshot)
    ensure_decisions(snapshot, decisions_path)
    return snapshot


def blank_decision(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "issue_code": case["issue_code"],
        "target_canonical_bld_id": case.get("target_canonical_bld_id"),
        "decision": None,
        "payload": {},
        "notes": "",
        "updated_at": None,
    }


def summarize_decisions(snapshot: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    by_action = Counter()
    decided = 0
    for case in snapshot.get("cases") or []:
        action = (decisions.get("decisions") or {}).get(case["case_id"], {}).get("decision")
        if action:
            decided += 1
            by_action[action] += 1
    total = len(snapshot.get("cases") or [])
    return {
        "total_cases": total,
        "decided": decided,
        "undecided": total - decided,
        "by_action": dict(sorted(by_action.items())),
    }


def ensure_decisions(snapshot: dict[str, Any], path: Path = DECISIONS_PATH) -> dict[str, Any]:
    existing = read_json(path) if path.exists() else {}
    old = existing.get("decisions") or {}
    merged = {
        "version": 1,
        "snapshot_path": snapshot.get("snapshot_path") or _rel(SNAPSHOT_PATH),
        "updated_at": existing.get("updated_at") or now_iso(),
        "db_writes": "none",
        "summary": {},
        "decisions": {},
        "term_decisions": existing.get("term_decisions") or {},
        "vocab_decisions": existing.get("vocab_decisions") or {},
    }
    for case in snapshot.get("cases") or []:
        item = blank_decision(case)
        previous = old.get(case["case_id"])
        if isinstance(previous, dict):
            for key in item:
                if key in previous:
                    item[key] = previous[key]
        merged["decisions"][case["case_id"]] = item
    merged["summary"] = summarize_decisions(snapshot, merged)
    if merged != existing:
        atomic_write_json(path, merged)
    return merged


def _case_target(case: dict[str, Any]) -> dict[str, Any]:
    evidence = case.get("evidence") or {}
    nested = evidence.get("target")
    if isinstance(nested, dict):
        return nested
    return case.get("target") or {}


def _case_title(case: dict[str, Any]) -> str:
    target = _case_target(case)
    return str(target.get("name") or case.get("title") or case.get("case_id"))


def _case_images(case: dict[str, Any]) -> list[dict[str, Any]]:
    target = _case_target(case)
    images = target.get("images") or case.get("target", {}).get("images") or []
    return [im for im in images if isinstance(im, dict) and im.get("url")]


def _candidate_values(case: dict[str, Any]) -> list[Any]:
    evidence = case.get("evidence") or {}
    issue = case.get("issue_code")
    if issue == "country_conflict":
        return list(dict.fromkeys(evidence.get("normalized") or evidence.get("source_countries_extracted") or []))
    if issue == "year_conflict":
        return list(dict.fromkeys(evidence.get("source_years") or []))
    return []


def _fast_case_question(case: dict[str, Any]) -> str:
    title = _case_title(case)
    issue = case.get("issue_code")
    if issue == "country_conflict":
        return f"이 건물의 국가를 골라주세요: {title}"
    if issue == "year_conflict":
        return f"이 건물의 연도를 골라주세요: {title}"
    if issue in {"cover_phash", "gallery_image", "gallery_phash"}:
        return f"대표 이미지가 맞는지 골라주세요: {title}"
    if issue == "seo_name":
        return f"검색에 방해되는 이름인지 판단해주세요: {title}"
    if issue == "architect_unknown":
        return f"건축가 정보가 충분한지 판단해주세요: {title}"
    if issue == "series_pavilion":
        return f"같은 건물로 합칠지, 별도 건물로 둘지 판단해주세요: {title}"
    if issue == "source_url_gap":
        return f"소스 URL 누락을 수정 대상으로 표시할지 판단해주세요: {title}"
    return f"이 항목을 어떻게 처리할지 판단해주세요: {title}"


def _fast_case_actions(case: dict[str, Any]) -> list[dict[str, Any]]:
    issue = case.get("issue_code")
    actions: list[dict[str, Any]] = []
    if issue == "country_conflict":
        for value in _candidate_values(case):
            actions.append(
                {
                    "label_ko": str(value),
                    "decision": "update_field",
                    "payload": {"field": "location_country", "value": value},
                }
            )
    elif issue == "year_conflict":
        for value in _candidate_values(case):
            actions.append(
                {
                    "label_ko": str(value),
                    "decision": "update_field",
                    "payload": {"field": "project_year", "value": value},
                }
            )
    elif issue in {"cover_phash", "gallery_image", "gallery_phash"}:
        for image in _case_images(case)[:12]:
            actions.append(
                {
                    "label_ko": "이 이미지",
                    "decision": "set_cover_to_image",
                    "payload": {"image_url": image.get("url"), "image_id": image.get("image_id")},
                    "image_url": image.get("url"),
                    "image_id": image.get("image_id"),
                }
            )
    elif issue == "seo_name":
        actions.extend(
            [
                {"label_ko": "유지", "decision": "keep", "payload": {}},
                {"label_ko": "이름 수정", "decision": "update_field", "payload": {"field": "name", "value": _case_title(case)}},
                {"label_ko": "비공개", "decision": "unpublish", "payload": {"reason": "manual_review_seo_name"}},
            ]
        )
    elif issue == "architect_unknown":
        actions.extend(
            [
                {"label_ko": "유지", "decision": "keep", "payload": {}},
                {
                    "label_ko": "건축가 수정",
                    "decision": "update_field",
                    "payload": {"field": "architects_text", "value": ""},
                    "requires_text": True,
                },
                {"label_ko": "비공개", "decision": "unpublish", "payload": {"reason": "architect_unknown"}},
            ]
        )
    elif issue == "series_pavilion":
        evidence = case.get("evidence") or {}
        rows = [r for r in evidence.get("rows") or [] if isinstance(r, dict)]
        survivor = case.get("target_canonical_bld_id")
        losers = [str(r.get("cid")) for r in rows if r.get("cid") and str(r.get("cid")) != str(survivor)]
        actions.extend(
            [
                {"label_ko": "별도 유지", "decision": "keep", "payload": {}},
                {"label_ko": "합치기 후보", "decision": "merge", "payload": {"survivor_cid": survivor, "loser_cids": losers}},
            ]
        )
    elif issue == "source_url_gap":
        actions.extend(
            [
                {"label_ko": "유지", "decision": "keep", "payload": {}},
                {"label_ko": "수정 필요", "decision": "needs_fix", "payload": {"reason": "source_url_gap"}},
            ]
        )
    else:
        actions.append({"label_ko": "유지", "decision": "keep", "payload": {}})
    actions.append({"label_ko": "보류", "decision": "unsure", "payload": {}})
    return actions


def _fast_case_card(case: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    item = (decisions.get("decisions") or {}).get(case["case_id"]) or {}
    image_actions_enabled = case.get("issue_code") in {"cover_phash", "gallery_image", "gallery_phash"}
    return {
        "kind": "case",
        "card_id": f"case:{case['case_id']}",
        "case_id": case["case_id"],
        "tab": case.get("tab"),
        "issue_code": case.get("issue_code"),
        "title": _case_title(case),
        "question_ko": _fast_case_question(case),
        "target_canonical_bld_id": case.get("target_canonical_bld_id"),
        "target": _case_target(case),
        "evidence": case.get("evidence") or {},
        "images": _case_images(case),
        "image_actions_enabled": image_actions_enabled,
        "actions": _fast_case_actions(case),
        "decision": item.get("decision"),
        "payload": item.get("payload") or {},
        "notes": item.get("notes") or "",
    }


def _material_terms_from_case(case: dict[str, Any]) -> list[str]:
    evidence = case.get("evidence") or {}
    terms = evidence.get("unmapped_material_terms") or evidence.get("material_noise") or []
    return [str(term) for term in terms if normalize_review_term(term)]


def _term_suggestions(term_key: str) -> dict[str, Any]:
    element_patterns = [
        ("Stair", r"stair|step"),
        ("Facade", r"facade|cladding|envelope|skin|frontage|opening"),
        ("Roof", r"roof|eave"),
        ("Courtyard", r"courtyard|court"),
        ("Entrance", r"entrance|entry"),
        ("Corridor", r"corridor|hallway"),
        ("Atrium", r"atrium|void"),
        ("Terrace", r"terrace|deck"),
        ("Balcony", r"balcon"),
        ("Garden", r"garden|landscape|plant|tree|lawn"),
        ("Column", r"column"),
        ("Canopy", r"canopy"),
        ("Skylight", r"skylight"),
    ]
    typology_patterns = [
        ("Apartment", r"apartment|residential block"),
        ("Housing", r"housing|residential"),
        ("Office", r"office"),
        ("Retail", r"retail|shop|store"),
        ("Restaurant", r"restaurant|cafe"),
        ("Hotel", r"hotel|resort"),
        ("Gallery", r"gallery|exhibition"),
        ("Museum", r"museum"),
        ("University", r"university|campus"),
        ("School", r"school"),
        ("Theatre", r"theatre|theater|auditorium"),
        ("Car Park", r"parking"),
        ("Bridge", r"bridge"),
        ("Park", r"park|public square|plaza"),
        ("Memorial", r"memorial|cemetery|monument"),
    ]
    element = next((value for value, pattern in element_patterns if re.search(pattern, term_key)), None)
    typology = next((value for value, pattern in typology_patterns if re.search(pattern, term_key)), None)
    return {"architectural_element": element, "typology": typology}


def _fast_term_card(term_key: str, group: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    item = (decisions.get("term_decisions") or {}).get(term_key) or {}
    display_term = group["display_term"]
    suggestions = _term_suggestions(term_key)
    return {
        "kind": "term",
        "card_id": f"term:{term_key}",
        "term_key": term_key,
        "display_term": display_term,
        "tab": "material",
        "issue_code": "material_unmapped",
        "title": display_term,
        "question_ko": f"이 표현은 재료인가요? '{display_term}'",
        "occurrence_count": group["occurrence_count"],
        "building_count": len(group["building_ids"]),
        "examples": group["examples"],
        "suggestions": suggestions,
        "allowed_actions": list(FAST_TERM_ACTIONS),
        "decision": item.get("decision"),
        "payload": item.get("payload") or {},
        "notes": item.get("notes") or "",
    }


def build_vocab_label_examples(
    labels_path: Path = VOCAB_LABELS_PATH,
    output_path: Path = VOCAB_EXAMPLES_PATH,
    per_tag: int = 3,
) -> dict[str, Any]:
    """Sample example buildings (+ source links, cover) per labeled tag.

    One read-only Neon pass; sidecar JSON consumed by the vocab cards so the
    reviewer sees where each tag actually comes from.
    """
    from tools.canonical_v2_neon_loader import _connect

    axes = (read_json(labels_path) or {}).get("axes") or {}
    conn = _connect()
    cur = conn.cursor()
    out: dict[str, Any] = {}
    for axis, tags in axes.items():
        if axis not in VOCAB_AXES:
            raise ValueError(f"unknown axis in labels file: {axis}")
        tag_list = sorted(tags)
        if axis in ("material_visual", "architectural_elements"):
            cur.execute(
                f"""
                SELECT tag, name, source_urls, display_cover_url, doc_freq FROM (
                    SELECT t.tag, b.name, b.source_urls, b.display_cover_url,
                           COUNT(*) OVER (PARTITION BY t.tag) AS doc_freq,
                           ROW_NUMBER() OVER (PARTITION BY t.tag
                                              ORDER BY md5(b.canonical_bld_id)) AS rn
                    FROM canonical_v2_buildings b
                    CROSS JOIN LATERAL (SELECT DISTINCT u AS tag FROM unnest(b.{axis}) u) t
                    WHERE b.is_publishable AND t.tag = ANY(%s)
                ) s WHERE rn <= %s
                """,
                (tag_list, per_tag),
            )
        else:
            cur.execute(
                f"""
                SELECT tag, name, source_urls, display_cover_url, doc_freq FROM (
                    SELECT {axis} AS tag, name, source_urls, display_cover_url,
                           COUNT(*) OVER (PARTITION BY {axis}) AS doc_freq,
                           ROW_NUMBER() OVER (PARTITION BY {axis}
                                              ORDER BY md5(canonical_bld_id)) AS rn
                    FROM canonical_v2_buildings
                    WHERE is_publishable AND {axis} = ANY(%s)
                ) s WHERE rn <= %s
                """,
                (tag_list, per_tag),
            )
        for tag, name, source_urls, cover, doc_freq in cur.fetchall():
            links = []
            for source, urls in (source_urls or {}).items():
                if isinstance(urls, list) and urls:
                    links.append({"source": source, "url": urls[0]})
                elif isinstance(urls, str) and urls:
                    links.append({"source": source, "url": urls})
            entry = out.setdefault(axis, {}).setdefault(
                tag, {"doc_freq": int(doc_freq), "examples": []})
            entry["examples"].append({"name": name, "cover": cover, "links": links[:3]})
    conn.rollback()
    conn.close()
    payload = {"generated_at": now_iso(), "per_tag": per_tag, "axes": out}
    atomic_write_json(output_path, payload)
    return payload


def _fast_vocab_card(
    axis: str,
    tag: str,
    entry: dict[str, Any],
    decisions: dict[str, Any],
    example_axes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = f"{axis}|{tag}"
    item = (decisions.get("vocab_decisions") or {}).get(key) or {}
    ex = ((example_axes or {}).get(axis) or {}).get(tag) or {}
    return {
        "kind": "vocab",
        "card_id": f"vocab:{key}",
        "vocab_key": key,
        "axis": axis,
        "tag": tag,
        "tab": "vocab",
        "issue_code": "vocab_label",
        "title": f"{axis} · {tag}",
        "question_ko": f"라벨과 generic 여부를 확인해주세요: [{axis}] {tag}",
        "draft": {
            "ko": entry.get("ko") or "",
            "en": entry.get("en") or tag,
            "is_generic": bool(entry.get("is_generic")),
        },
        "doc_freq": ex.get("doc_freq"),
        "examples": ex.get("examples") or [],
        "allowed_actions": list(FAST_VOCAB_ACTIONS),
        "decision": item.get("decision"),
        "payload": item.get("payload") or {},
        "notes": item.get("notes") or "",
    }


def build_vocab_cards(decisions: dict[str, Any], labels_path: Path = VOCAB_LABELS_PATH) -> list[dict[str, Any]]:
    if not labels_path.exists():
        return []
    axes = (read_json(labels_path) or {}).get("axes") or {}
    example_axes = {}
    if VOCAB_EXAMPLES_PATH.exists():
        example_axes = (read_json(VOCAB_EXAMPLES_PATH) or {}).get("axes") or {}
    cards = []
    for axis in sorted(axes):
        for tag in sorted(axes[axis]):
            cards.append(_fast_vocab_card(axis, tag, axes[axis][tag] or {}, decisions, example_axes))
    return cards


def build_fast_snapshot(snapshot: dict[str, Any], decisions: dict[str, Any] | None = None) -> dict[str, Any]:
    decisions = decisions or {"decisions": {}, "term_decisions": {}, "vocab_decisions": {}}
    case_cards = []
    term_groups: dict[str, dict[str, Any]] = {}
    for case in snapshot.get("cases") or []:
        if case.get("issue_code") == "material_unmapped":
            evidence = case.get("evidence") or {}
            for term in _material_terms_from_case(case):
                term_key = normalize_review_term(term)
                group = term_groups.setdefault(
                    term_key,
                    {
                        "display_term": str(term),
                        "occurrence_count": 0,
                        "building_ids": set(),
                        "examples": [],
                    },
                )
                group["occurrence_count"] += 1
                cid = case.get("target_canonical_bld_id") or evidence.get("canonical_bld_id")
                if cid:
                    group["building_ids"].add(str(cid))
                if len(group["examples"]) < FAST_TERM_EXAMPLE_LIMIT:
                    group["examples"].append(
                        {
                            "canonical_bld_id": cid,
                            "name": evidence.get("name") or _case_title(case),
                            "program": evidence.get("program"),
                            "typology_primary": evidence.get("typology_primary"),
                            "raw_material_visual": evidence.get("raw_material_visual") or [],
                            "source_refs": evidence.get("source_refs") or {},
                        }
                    )
            continue
        case_cards.append(_fast_case_card(case, decisions))

    case_cards.sort(
        key=lambda c: (
            FAST_CASE_PRIORITY.get(str(c.get("tab")), 99),
            str(c.get("issue_code") or ""),
            str(c.get("target_canonical_bld_id") or ""),
            str(c.get("case_id") or ""),
        )
    )
    term_cards = [_fast_term_card(term_key, term_groups[term_key], decisions) for term_key in sorted(term_groups)]
    vocab_cards = build_vocab_cards(decisions)
    queue = case_cards + term_cards + vocab_cards
    done = sum(1 for card in queue if card.get("decision"))
    counts = Counter(str(card.get("tab") or "unknown") for card in queue)
    return {
        "version": 1,
        "source_snapshot": snapshot.get("snapshot_path") or snapshot.get("source_artifact"),
        "generated_at": now_iso(),
        "counts": {
            "total_cards": len(queue),
            "case_cards": len(case_cards),
            "term_cards": len(term_cards),
            "vocab_cards": len(vocab_cards),
            "decided": done,
            "undecided": len(queue) - done,
            "by_tab": dict(sorted(counts.items())),
        },
        "actions": {
            "term": list(FAST_TERM_ACTIONS),
            "vocab": list(FAST_VOCAB_ACTIONS),
            "architectural_elements": sorted(vocab.ARCHITECTURAL_ELEMENT),
            "typology": sorted(vocab.TYPOLOGY),
            "program": sorted(vocab.PROGRAM),
        },
        "queue": queue,
    }


def _validate_decision_payload(
    *,
    decision: str,
    payload: dict[str, Any],
    case: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if decision == "update_field":
        field = payload.get("field")
        if field not in SAFE_UPDATE_FIELDS:
            raise ValueError(f"payload.field must be one of {sorted(SAFE_UPDATE_FIELDS)}")
        if "value" not in payload:
            raise ValueError("payload.value is required for update_field")
        return {"field": field, "value": payload.get("value")}
    if decision == "set_cover_to_image":
        image_url = payload.get("image_url") or payload.get("selected_image_url")
        image_id = payload.get("image_id") or payload.get("selected_image_id")
        target_images = ((case or {}).get("target") or {}).get("images") or []
        valid_by_id = {im.get("image_id"): im for im in target_images if isinstance(im, dict)}
        valid_urls = {im.get("url") for im in target_images if isinstance(im, dict) and im.get("url")}
        if image_id and image_id in valid_by_id:
            image_url = valid_by_id[image_id].get("url")
        if not image_url:
            raise ValueError("payload.image_url is required for set_cover_to_image")
        if valid_urls and image_url not in valid_urls:
            raise ValueError("payload.image_url must exist in target images")
        return {"image_url": image_url, "image_id": image_id}
    if decision == "unpublish":
        reason = str(payload.get("reason") or "manual_review_unpublish")[:120]
        return {"reason": reason}
    if decision == "merge":
        survivor = payload.get("survivor_cid")
        losers = payload.get("loser_cids")
        if not survivor or not isinstance(losers, list) or not losers:
            raise ValueError("merge requires payload.survivor_cid and non-empty payload.loser_cids")
        return {"survivor_cid": str(survivor), "loser_cids": [str(x) for x in losers]}
    if decision == "split":
        split_plan = payload.get("split_plan")
        if not isinstance(split_plan, (dict, list)) or not split_plan:
            raise ValueError("split requires non-empty payload.split_plan")
        return {"split_plan": split_plan}
    return dict(payload or {})


def validate_decision(snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    case_id = str(payload.get("case_id") or "")
    cases = {c["case_id"]: c for c in snapshot.get("cases") or []}
    if case_id not in cases:
        raise ValueError("unknown case_id")
    decision = payload.get("decision")
    if decision is not None:
        decision = str(decision)
    if decision not in DEFAULT_ACTIONS:
        raise ValueError(f"decision must be one of {DEFAULT_ACTIONS}")
    case = cases[case_id]
    raw_payload = payload.get("payload") or {}
    if not isinstance(raw_payload, dict):
        raise ValueError("payload must be an object")
    clean_payload = _validate_decision_payload(decision=decision, payload=raw_payload, case=case)
    notes = str(payload.get("notes") or "")[:2000]
    return {
        "case_id": case_id,
        "issue_code": case["issue_code"],
        "target_canonical_bld_id": case.get("target_canonical_bld_id"),
        "decision": decision,
        "payload": clean_payload,
        "notes": notes,
        "updated_at": now_iso(),
    }


def validate_term_decision(payload: dict[str, Any]) -> dict[str, Any]:
    card_id = str(payload.get("card_id") or "")
    if not card_id.startswith("term:"):
        raise ValueError("term decision card_id must start with term:")
    term_key = normalize_review_term(card_id.removeprefix("term:"))
    if not term_key:
        raise ValueError("term decision requires a term key")
    decision = str(payload.get("decision") or "")
    if decision not in FAST_TERM_ACTIONS:
        raise ValueError(f"term decision must be one of {FAST_TERM_ACTIONS}")
    raw_payload = payload.get("payload") or {}
    if not isinstance(raw_payload, dict):
        raise ValueError("payload must be an object")
    clean_payload = dict(raw_payload)
    clean_payload["term"] = clean_payload.get("term") or term_key
    if decision == "architectural_element":
        value = clean_payload.get("canonical_value")
        if value not in vocab.ARCHITECTURAL_ELEMENT:
            raise ValueError("payload.canonical_value must be a valid architectural element")
    if decision == "typology":
        field = clean_payload.get("field") or "typology_tags"
        value = clean_payload.get("canonical_value")
        if field == "program":
            if value not in vocab.PROGRAM:
                raise ValueError("payload.canonical_value must be a valid program")
        elif field == "typology_tags":
            if value not in vocab.TYPOLOGY:
                raise ValueError("payload.canonical_value must be a valid typology")
        else:
            raise ValueError("payload.field must be program or typology_tags")
        clean_payload["field"] = field
    return {
        "term_key": term_key,
        "decision": decision,
        "payload": clean_payload,
        "notes": str(payload.get("notes") or "")[:2000],
        "updated_at": now_iso(),
    }


def validate_vocab_decision(payload: dict[str, Any]) -> dict[str, Any]:
    card_id = str(payload.get("card_id") or "")
    if not card_id.startswith("vocab:"):
        raise ValueError("vocab decision card_id must start with vocab:")
    key = card_id.removeprefix("vocab:")
    axis, sep, tag = key.partition("|")
    if not sep or not axis or not tag:
        raise ValueError("vocab card_id must be vocab:<axis>|<tag>")
    decision = str(payload.get("decision") or "")
    if decision not in FAST_VOCAB_ACTIONS:
        raise ValueError(f"vocab decision must be one of {FAST_VOCAB_ACTIONS}")
    raw_payload = payload.get("payload") or {}
    if not isinstance(raw_payload, dict):
        raise ValueError("payload must be an object")
    clean_payload: dict[str, Any] = {}
    if decision == "edit":
        ko = str(raw_payload.get("ko") or "").strip()
        en = str(raw_payload.get("en") or "").strip()
        if not ko and not en and "is_generic" not in raw_payload:
            raise ValueError("edit requires at least one of payload.ko/en/is_generic")
        clean_payload = {"ko": ko or None, "en": en or None,
                         "is_generic": bool(raw_payload.get("is_generic"))}
    return {
        "vocab_key": key,
        "axis": axis,
        "tag": tag,
        "decision": decision,
        "payload": clean_payload,
        "notes": str(payload.get("notes") or "")[:2000],
        "updated_at": now_iso(),
    }


def save_decision(snapshot: dict[str, Any], payload: dict[str, Any], path: Path = DECISIONS_PATH) -> dict[str, Any]:
    decisions = ensure_decisions(snapshot, path)
    item = validate_decision(snapshot, payload)
    decisions["decisions"][item["case_id"]] = item
    decisions["updated_at"] = now_iso()
    decisions["summary"] = summarize_decisions(snapshot, decisions)
    atomic_write_json(path, decisions)
    return decisions


def save_fast_decision(snapshot: dict[str, Any], payload: dict[str, Any], path: Path = DECISIONS_PATH) -> dict[str, Any]:
    decisions = ensure_decisions(snapshot, path)
    card_id = str(payload.get("card_id") or "")
    if payload.get("undo"):
        if card_id.startswith("term:"):
            decisions.setdefault("term_decisions", {}).pop(normalize_review_term(card_id.removeprefix("term:")), None)
        elif card_id.startswith("vocab:"):
            decisions.setdefault("vocab_decisions", {}).pop(card_id.removeprefix("vocab:"), None)
        elif card_id.startswith("case:"):
            case_id = card_id.removeprefix("case:")
            cases = {c["case_id"]: c for c in snapshot.get("cases") or []}
            if case_id in cases:
                decisions.setdefault("decisions", {})[case_id] = blank_decision(cases[case_id])
        decisions["updated_at"] = now_iso()
        decisions["summary"] = summarize_decisions(snapshot, decisions)
        atomic_write_json(path, decisions)
        return decisions
    if card_id.startswith("term:"):
        item = validate_term_decision(payload)
        decisions.setdefault("term_decisions", {})[item["term_key"]] = item
    elif card_id.startswith("vocab:"):
        item = validate_vocab_decision(payload)
        decisions.setdefault("vocab_decisions", {})[item["vocab_key"]] = item
    elif card_id.startswith("case:"):
        case_payload = dict(payload)
        case_payload["case_id"] = card_id.removeprefix("case:")
        item = validate_decision(snapshot, case_payload)
        decisions.setdefault("decisions", {})[item["case_id"]] = item
    else:
        raise ValueError("card_id must start with term:, vocab: or case:")
    decisions["updated_at"] = now_iso()
    decisions["summary"] = summarize_decisions(snapshot, decisions)
    atomic_write_json(path, decisions)
    return decisions


def _validated_decision_items(decisions: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid = []
    invalid = []
    for case_id, item in (decisions.get("decisions") or {}).items():
        if not isinstance(item, dict) or not item.get("decision"):
            continue
        decision = str(item.get("decision"))
        if decision in {"keep", "unsure"}:
            clean_payload = dict(item.get("payload") or {})
        else:
            try:
                clean_payload = _validate_decision_payload(
                    decision=decision,
                    payload=item.get("payload") or {},
                    case=None,
                )
            except Exception as exc:  # noqa: BLE001
                invalid.append({"case_id": case_id, "reason": str(exc)})
                continue
        if decision not in DEFAULT_ACTIONS:
            invalid.append({"case_id": case_id, "reason": f"unknown decision {decision}"})
            continue
        valid.append({**item, "case_id": case_id, "decision": decision, "payload": clean_payload})
    return valid, invalid


def _validated_term_decision_items(decisions: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    valid = {}
    invalid = []
    for term_key, item in (decisions.get("term_decisions") or {}).items():
        if not isinstance(item, dict) or not item.get("decision"):
            continue
        payload = {
            "card_id": f"term:{term_key}",
            "decision": item.get("decision"),
            "payload": item.get("payload") or {"term": term_key},
            "notes": item.get("notes") or "",
        }
        try:
            clean = validate_term_decision(payload)
        except Exception as exc:  # noqa: BLE001
            invalid.append({"term_key": term_key, "reason": str(exc)})
            continue
        valid[clean["term_key"]] = clean
    return valid, invalid


def _append_unique(values: list[str], value: str) -> list[str]:
    if value in values:
        return values
    return values + [value]


def _apply_to_row(row: dict[str, Any], decisions: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    changed = dict(row)
    changes = []
    for item in decisions:
        action = item["decision"]
        payload = item.get("payload") or {}
        if action == "update_field":
            field = payload["field"]
            old = changed.get(field)
            new = payload.get("value")
            if old != new:
                changed[field] = new
                changes.append({"action": action, "field": field, "old": old, "new": new, "case_id": item["case_id"]})
        elif action == "set_cover_to_image":
            new = payload["image_url"]
            for field in ("display_cover_url", "cover_image_url_default"):
                old = changed.get(field)
                if old != new:
                    changed[field] = new
                    changes.append({"action": action, "field": field, "old": old, "new": new, "case_id": item["case_id"]})
        elif action == "unpublish":
            old_pub = bool(changed.get("is_publishable"))
            reasons = list(changed.get("publishability_reasons") or [])
            reason = payload.get("reason") or "manual_review_unpublish"
            if reason not in reasons:
                reasons.append(reason)
            changed["is_publishable"] = False
            changed["publishability_reasons"] = reasons
            changes.append(
                {
                    "action": action,
                    "field": "is_publishable",
                    "old": old_pub,
                    "new": False,
                    "reason": reason,
                    "case_id": item["case_id"],
                }
            )
    return changed, changes


def _apply_term_decisions_to_row(
    row: dict[str, Any],
    term_decisions: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str]]:
    if not term_decisions:
        return row, [], [], []
    changed = dict(row)
    material = [v for v in (changed.get("material_visual") or []) if isinstance(v, str)]
    old_material = list(material)
    elements = [v for v in (changed.get("architectural_elements") or []) if isinstance(v, str)]
    typology_tags = [v for v in (changed.get("typology_tags") or []) if isinstance(v, str)]
    keyword_additions: list[str] = []
    keyword_removals: list[str] = []
    changes: list[dict[str, Any]] = []
    kept_material: list[str] = []

    for term in material:
        key = normalize_review_term(term)
        item = term_decisions.get(key)
        if not item:
            kept_material.append(term)
            continue
        action = item["decision"]
        payload = item.get("payload") or {}
        if action == "material" or action == "unsure":
            kept_material.append(term)
            continue
        if action == "search_keyword":
            keyword_additions = _append_unique(keyword_additions, payload.get("term") or key)
        elif action == "delete":
            keyword_removals = _append_unique(keyword_removals, payload.get("term") or key)
        elif action == "architectural_element":
            elements = _append_unique(elements, str(payload["canonical_value"]))
        elif action == "typology":
            if payload.get("field") == "program":
                old = changed.get("program")
                new = payload["canonical_value"]
                if old != new:
                    changed["program"] = new
                    changes.append({"action": action, "field": "program", "old": old, "new": new, "term_key": key})
            else:
                typology_tags = _append_unique(typology_tags, str(payload["canonical_value"]))

    if kept_material != old_material:
        if changed.get("is_publishable") and not kept_material and not elements:
            kept_material = ["unspecified"]
        changed["material_visual"] = kept_material
        changes.append({"action": "term_material_cleanup", "field": "material_visual", "old": old_material, "new": kept_material})
    if elements != (row.get("architectural_elements") or []):
        changed["architectural_elements"] = elements
        changes.append(
            {
                "action": "term_material_cleanup",
                "field": "architectural_elements",
                "old": row.get("architectural_elements") or [],
                "new": elements,
            }
        )
    if typology_tags != (row.get("typology_tags") or []):
        changed["typology_tags"] = typology_tags
        changes.append(
            {
                "action": "term_material_cleanup",
                "field": "typology_tags",
                "old": row.get("typology_tags") or [],
                "new": typology_tags,
            }
        )
    return changed, changes, keyword_additions, keyword_removals


def apply_decisions(
    *,
    input_path: Path = C23_EMBEDDED,
    decisions_path: Path = DECISIONS_PATH,
    output_path: Path = C24_EMBEDDED,
    patch_path: Path = PATCH_PATH,
    write_artifact: bool = False,
) -> dict[str, Any]:
    decisions = read_json(decisions_path)
    valid, invalid = _validated_decision_items(decisions)
    term_decisions, term_invalid = _validated_term_decision_items(decisions)
    invalid.extend(term_invalid)
    structural = [d for d in valid if d["decision"] in {"merge", "split"}]
    row_actions = [d for d in valid if d["decision"] in {"update_field", "set_cover_to_image", "unpublish"}]
    by_cid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in row_actions:
        cid = item.get("target_canonical_bld_id")
        if not cid:
            invalid.append({"case_id": item["case_id"], "reason": "target_canonical_bld_id is required"})
            continue
        by_cid[str(cid)].append(item)
    if structural:
        for item in structural:
            invalid.append(
                {
                    "case_id": item["case_id"],
                    "reason": f"{item['decision']} is captured but not auto-applied by this artifact applier",
                }
            )

    action_counts = Counter(d["decision"] for d in valid)
    term_action_counts = Counter(d["decision"] for d in term_decisions.values())
    changed_rows = []
    rows_in = 0
    rows_out = 0
    publishable_before = 0
    publishable_after = 0
    keyword_additions_by_cid: dict[str, list[str]] = {}
    keyword_removals_by_cid: dict[str, list[str]] = {}

    if write_artifact and invalid:
        write_artifact = False

    writer = None
    if write_artifact:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        writer = output_path.open("w", encoding="utf-8")
        writer.write('{"buildings":[')
    try:
        for row in iter_buildings(input_path):
            rows_in += 1
            if row.get("is_publishable"):
                publishable_before += 1
            cid = str(row.get("canonical_bld_id") or "")
            changed = row
            row_changes: list[dict[str, Any]] = []
            if cid in by_cid and not invalid:
                changed, row_changes = _apply_to_row(row, by_cid[cid])
            if term_decisions and not invalid:
                changed, term_changes, keyword_additions, keyword_removals = _apply_term_decisions_to_row(
                    changed,
                    term_decisions,
                )
                if term_changes:
                    row_changes.extend(term_changes)
                if keyword_additions:
                    keyword_additions_by_cid[cid] = keyword_additions
                if keyword_removals:
                    keyword_removals_by_cid[cid] = keyword_removals
            if row_changes:
                changed_rows.append({"canonical_bld_id": cid, "changes": row_changes})
            if changed.get("is_publishable"):
                publishable_after += 1
            if writer:
                writer.write(("," if rows_out else "") + json.dumps(changed, ensure_ascii=False, default=json_default))
                rows_out += 1
    finally:
        if writer:
            writer.write("]}")
            writer.close()

    report = {
        "generated_at": now_iso(),
        "status": "FAIL" if invalid else "PASS",
        "input": _rel(input_path),
        "output": _rel(output_path) if write_artifact else None,
        "patch_path": _rel(patch_path),
        "write_artifact": write_artifact,
        "db_writes": "none",
        "rows_in": rows_in,
        "rows_out": rows_out if write_artifact else rows_in,
        "changed_rows": len(changed_rows),
        "changed_canonical_ids": [c["canonical_bld_id"] for c in changed_rows],
        "publishable_before": publishable_before,
        "publishable_after": publishable_after if not invalid else publishable_before,
        "publishable_delta": (publishable_after - publishable_before) if not invalid else 0,
        "action_counts": dict(action_counts),
        "term_action_counts": dict(term_action_counts),
        "search_keyword_patch": {
            "additions": keyword_additions_by_cid if not invalid else {},
            "removals": keyword_removals_by_cid if not invalid else {},
        },
        "invalid_decisions": invalid,
        "structural_actions": structural,
        "changes": changed_rows,
        "dry_run_commands": [
            f"python3 tools/canonical_v2_upload_validator.py --input {_rel(output_path)} --report {_rel(REPORT_DIR / 'canonical_v2_upload_validation.c24_manual_review.json')}",
            f"python3 tools/canonical_v2_neon_loader.py --dry-run-upsert --input {_rel(output_path)} --report {_rel(REPORT_DIR / 'canonical_v2_neon_dry_run.c24_manual_review.json')}",
        ],
    }
    atomic_write_json(patch_path, report)
    return report


FAST_APP_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fast Review</title>
<style>
:root { --bg:#f5f6f2; --panel:#fff; --line:#d7d9d2; --text:#1f2428; --muted:#66737f; --accent:#0b7285; --accent2:#e6f6f8; --danger:#b42318; --ok:#177245; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
button, select, input { font:inherit; }
.app { min-height:100vh; display:grid; grid-template-columns:280px minmax(0,1fr); }
.side { border-right:1px solid var(--line); background:#fbfbf8; padding:16px; position:sticky; top:0; height:100vh; overflow:auto; }
.main { padding:22px; max-width:1120px; width:100%; margin:0 auto; }
h1 { margin:0 0 6px; font-size:19px; }
h2 { margin:0; font-size:28px; line-height:1.25; }
h3 { margin:0 0 8px; font-size:16px; }
.muted { color:var(--muted); }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; }
.stats { display:grid; grid-template-columns:repeat(2,1fr); gap:8px; margin:14px 0; }
.stat { border:1px solid var(--line); background:white; border-radius:8px; padding:9px; }
.stat b { display:block; font-size:22px; }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin:12px 0; }
button { border:1px solid var(--line); background:white; border-radius:7px; padding:8px 10px; cursor:pointer; }
button.active, button.primary { border-color:var(--accent); background:var(--accent2); color:#07525f; }
button.danger { color:var(--danger); border-color:#efb6b0; }
button:disabled { opacity:.5; cursor:not-allowed; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; margin-bottom:14px; }
.hero { display:grid; gap:10px; }
.badge { display:inline-flex; width:max-content; gap:6px; align-items:center; border:1px solid var(--line); border-radius:999px; padding:4px 9px; background:#f9faf7; color:var(--muted); font-size:13px; }
.actions { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:16px; }
.action { min-height:58px; font-weight:700; font-size:16px; }
.action small { display:block; font-weight:500; color:var(--muted); margin-top:2px; }
.grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(280px,.7fr); gap:14px; }
.examples { display:grid; gap:8px; }
.example { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfbf8; }
.images { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:10px; }
.image { border:1px solid var(--line); border-radius:8px; overflow:hidden; background:#fafafa; }
.image img { width:100%; aspect-ratio:4/3; object-fit:cover; display:block; background:#eceee8; }
.image button { width:100%; border:0; border-top:1px solid var(--line); border-radius:0; }
.image .cap { padding:7px; color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
.links { display:grid; gap:7px; }
.links a { color:#07525f; overflow-wrap:anywhere; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:#f7f7f5; border:1px solid var(--line); border-radius:8px; padding:10px; max-height:250px; overflow:auto; }
select, input { width:100%; border:1px solid var(--line); border-radius:7px; padding:9px; background:white; }
.controls { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; margin-top:12px; }
.toast { position:fixed; right:18px; bottom:18px; background:#17212b; color:white; padding:10px 13px; border-radius:7px; opacity:0; transform:translateY(8px); transition:.18s ease; pointer-events:none; }
.toast.show { opacity:1; transform:translateY(0); }
.empty { padding:50px; text-align:center; color:var(--muted); }
@media (max-width:900px) { .app{grid-template-columns:1fr;} .side{position:relative;height:auto;} .grid{grid-template-columns:1fr;} }
</style>
</head>
<body>
<div class="app">
<aside class="side">
  <h1>Fast Review</h1>
  <div class="muted">판단 버튼을 누르면 다음 항목으로 넘어갑니다.</div>
  <div class="stats">
    <div class="stat"><b id="done">0</b><span>Done</span></div>
    <div class="stat"><b id="todo">0</b><span>Todo</span></div>
  </div>
  <div id="tabs" class="tabs"></div>
  <button id="allQueue" class="active">전체 자동순서</button>
  <button id="undo">이전 결정 취소</button>
  <p class="muted mono" id="meta"></p>
  <p class="muted mono">단축키: 1-6</p>
  <p><a href="/debug">debug list</a></p>
</aside>
<main class="main">
  <div id="view" class="empty">Loading</div>
</main>
</div>
<div id="toast" class="toast"></div>
<script>
let state=null, activeTab='all', activeCardId=null, lastCardId=null;
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function loadFast(){const r=await fetch('/api/fast-snapshot'); if(!r.ok) throw new Error(await r.text()); state=await r.json();}
function toast(t){const el=$('#toast'); el.textContent=t; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),1400);}
function filtered(){const q=state?.queue||[]; return q.filter(c=>activeTab==='all'||c.tab===activeTab);}
function firstTodo(cards){return cards.find(c=>!c.decision)||cards[0]||null;}
function current(){const cards=filtered(); return cards.find(c=>c.card_id===activeCardId)||firstTodo(cards);}
function renderStats(){const c=state?.counts||{}; $('#done').textContent=c.decided??0; $('#todo').textContent=c.undecided??0; $('#meta').textContent=`${c.total_cards??0} cards · cases ${c.case_cards??0} · terms ${c.term_cards??0} · vocab ${c.vocab_cards??0}`;}
function renderTabs(){const counts=state?.counts?.by_tab||{}; const names=['all',...Object.keys(counts).sort()]; $('#tabs').innerHTML=names.map(n=>`<button data-tab="${esc(n)}" class="${n===activeTab?'active':''}">${esc(n)} ${n==='all'?state.counts.total_cards:counts[n]}</button>`).join(''); document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{activeTab=b.dataset.tab; activeCardId=null; render();});}
function targetMeta(c){const t=c.target||{}; return [t.name||c.title,t.location_country,t.location_city,t.project_year,(t.architect_names||[]).join(', ')].filter(Boolean).join(' · ');}
function sourceLinks(c){const urls=c.target?.source_urls||{}; const refs=c.target?.source_refs||{}; const links=[]; Object.entries(urls).forEach(([source,items])=>(items||[]).forEach((url,i)=>links.push({source,url,ref:(refs[source]||[])[i]}))); if(!links.length)return '<div class="card"><h3>원본 링크</h3><div class="muted">원본 URL 없음. source_refs만 확인하세요.</div><pre>'+esc(JSON.stringify(refs,null,2))+'</pre></div>'; return `<div class="card"><h3>원본 링크</h3><div class="links">${links.map(l=>`<a href="${esc(l.url)}" target="_blank" rel="noreferrer">${esc(l.source)} ${esc(l.ref||'')} · ${esc(l.url)}</a>`).join('')}</div></div>`;}
function renderExamples(c){return `<div class="card"><h3>예시 건물</h3><div class="examples">${(c.examples||[]).map(e=>`<div class="example"><b>${esc(e.name||e.canonical_bld_id)}</b><div class="muted">${esc([e.program,e.typology_primary].filter(Boolean).join(' · '))}</div><div class="mono">${esc((e.raw_material_visual||[]).join(', '))}</div></div>`).join('')}</div></div>`;}
function selectOptions(values, selected){return `<option value="">선택</option>${values.map(v=>`<option value="${esc(v)}" ${v===selected?'selected':''}>${esc(v)}</option>`).join('')}`;}
function renderTerm(c){const s=c.suggestions||{}; return `<div class="hero card"><span class="badge">material term · ${esc(c.occurrence_count)}회</span><h2>${esc(c.question_ko)}</h2><div class="muted">이 단어를 material facet에 남길지, 다른 검색 신호로 옮길지 결정하세요.</div></div><div class="grid"><div>${renderExamples(c)}<div class="card"><h3>분류 선택</h3><div class="controls"><label>건축요소<select id="elementValue">${selectOptions(state.actions.architectural_elements,s.architectural_element)}</select></label><label>용도/타입 필드<select id="typeField"><option value="typology_tags">typology_tags</option><option value="program">program</option></select></label><label>용도/타입 값<select id="typeValue">${selectOptions(state.actions.typology,s.typology)}</select></label></div></div></div><div><div class="card"><h3>결정</h3><div class="actions"><button class="action primary" data-term-action="material">1 재료<small>material_visual 유지</small></button><button class="action" data-term-action="architectural_element">2 건축요소<small>선택한 element로 이동</small></button><button class="action" data-term-action="typology">3 용도/타입<small>typology/program으로 이동</small></button><button class="action" data-term-action="search_keyword">4 검색어만<small>material에서 제거, 검색 보존</small></button><button class="action danger" data-term-action="delete">5 삭제<small>검색에서도 제거</small></button><button class="action" data-term-action="unsure">6 보류<small>나중에 다시 보기</small></button></div></div></div></div>`;}
function renderVocab(c){const d=c.draft||{}; const ex=(c.examples||[]).map(e=>`<div class="example" style="display:flex;gap:10px;align-items:center">${e.cover?`<img src="${esc(e.cover)}" loading="lazy" style="width:120px;height:90px;object-fit:cover;border-radius:6px;background:#eceee8;flex:none">`:''}<div><b>${esc(e.name)}</b><div class="links" style="display:flex;gap:10px">${(e.links||[]).map(l=>`<a href="${esc(l.url)}" target="_blank" rel="noreferrer">${esc(l.source)} ↗</a>`).join('')}</div></div></div>`).join(''); return `<div class="hero card"><span class="badge">vocab · ${esc(c.axis)}${c.doc_freq!=null?` · ${esc(c.doc_freq)}개 건물`:''}</span><h2>${esc(c.tag)}</h2><div class="muted">라벨 확인 후 승인/수정. generic = 변별력 없는 범용 태그 → 질문 키워드에서 제외.</div><div class="controls"><label>한글 라벨<input id="vocabKo" value="${esc(d.ko)}"></label><label>영문 라벨<input id="vocabEn" value="${esc(d.en)}"></label><label style="display:flex;align-items:center;gap:8px;margin-top:22px"><input type="checkbox" id="vocabGeneric" style="width:auto" ${d.is_generic?'checked':''}> generic (질문 제외)</label></div><div class="actions"><button class="action primary" data-vocab-action="approve">1 승인<small>초안 그대로</small></button><button class="action" data-vocab-action="edit">2 수정 저장<small>입력값으로 갱신</small></button><button class="action" data-vocab-action="unsure">3 보류<small>나중에 다시</small></button></div></div>${ex?`<div class="card"><h3>예시 건물 — 원본 출처</h3><div class="examples">${ex}</div></div>`:'<div class="card muted">예시 없음 — vocab-examples 미생성 또는 미관측 태그</div>'}`;}
function imageCards(c){const title=c.image_actions_enabled?'대표 이미지 선택':'판단 참고 사진'; if(!(c.images||[]).length)return `<div class="card"><h3>${title}</h3><div class="muted">사진 없음</div></div>`; return `<div class="card"><h3>${title}</h3><div class="images">${c.images.map((im,i)=>`<div class="image"><img src="${esc(im.url)}" loading="lazy"><div class="cap">${esc(im.kind||im.type||'image')} · ${esc(im.source||'')}</div>${c.image_actions_enabled?`<button data-case-action="${i}">이 이미지 선택</button>`:''}</div>`).join('')}</div></div>`;}
function renderCase(c){return `<div class="hero card"><span class="badge">${esc(c.tab)} · ${esc(c.issue_code)}</span><h2>${esc(c.question_ko)}</h2><div class="muted">${esc(targetMeta(c))}</div></div><div class="grid"><div>${sourceLinks(c)}${imageCards(c)}<div class="card"><h3>Evidence</h3><pre>${esc(JSON.stringify(c.evidence,null,2))}</pre></div></div><div><div class="card"><h3>결정</h3><input id="textValue" value="${esc(c.title||'')}" placeholder="수정 값"><div class="actions">${(c.actions||[]).map((a,i)=>`<button class="action ${a.decision==='unpublish'?'danger':''}" data-case-action="${i}">${i+1} ${esc(a.label_ko)}</button>`).join('')}</div></div></div></div>`;}
function bindCard(c){document.querySelectorAll('[data-term-action]').forEach(b=>b.onclick=()=>saveTerm(c,b.dataset.termAction)); document.querySelectorAll('[data-vocab-action]').forEach(b=>b.onclick=()=>saveVocab(c,b.dataset.vocabAction)); document.querySelectorAll('[data-case-action]').forEach(b=>b.onclick=()=>saveCase(c,Number(b.dataset.caseAction))); const tf=$('#typeField'); const tv=$('#typeValue'); if(tf&&tv){tf.onchange=()=>{tv.innerHTML=selectOptions(tf.value==='program'?state.actions.program:state.actions.typology,'');};}}
function render(){renderStats(); renderTabs(); const c=current(); if(!c){$('#view').innerHTML='<div class="empty">모든 검토 완료</div>'; return;} activeCardId=c.card_id; $('#view').innerHTML=c.kind==='term'?renderTerm(c):(c.kind==='vocab'?renderVocab(c):renderCase(c)); bindCard(c);}
async function postDecision(payload){const r=await fetch('/api/fast-decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); if(!r.ok){toast(await r.text()); return false;} state=await r.json(); lastCardId=payload.card_id; const cards=filtered(); const oldIndex=cards.findIndex(c=>c.card_id===payload.card_id); activeCardId=(cards.slice(oldIndex+1).find(c=>!c.decision)||cards.find(c=>!c.decision)||cards[oldIndex+1]||cards[0]||{}).card_id; toast('저장됨'); render(); return true;}
function saveTerm(c, action){const payload={term:c.display_term}; if(action==='architectural_element'){const v=$('#elementValue').value; if(!v){toast('건축요소를 선택하세요'); return;} payload.canonical_value=v;} if(action==='typology'){const field=$('#typeField').value; const v=$('#typeValue').value; if(!v){toast('용도/타입 값을 선택하세요'); return;} payload.field=field; payload.canonical_value=v;} postDecision({card_id:c.card_id,decision:action,payload});}
function saveVocab(c, action){let payload={}; if(action==='edit'){payload={ko:$('#vocabKo').value,en:$('#vocabEn').value,is_generic:$('#vocabGeneric').checked};} postDecision({card_id:c.card_id,decision:action,payload});}
function saveCase(c, idx){const action=(c.actions||[])[idx]; if(!action)return; const payload=JSON.parse(JSON.stringify(action.payload||{})); if(action.requires_text||payload.field==='name'||payload.field==='architects_text'){payload.value=$('#textValue').value;} postDecision({card_id:c.card_id,decision:action.decision,payload});}
$('#undo').onclick=async()=>{if(!lastCardId){toast('취소할 결정 없음');return;} const r=await fetch('/api/fast-decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({card_id:lastCardId,undo:true})}); if(!r.ok){toast(await r.text());return;} state=await r.json(); activeCardId=lastCardId; lastCardId=null; toast('취소됨'); render();};
document.addEventListener('keydown',e=>{if(!/^[1-6]$/.test(e.key))return; const n=Number(e.key)-1; const c=current(); if(!c)return; const buttons=[...document.querySelectorAll(c.kind==='term'?'[data-term-action]':(c.kind==='vocab'?'[data-vocab-action]':'[data-case-action]'))]; if(buttons[n])buttons[n].click();});
(async()=>{await loadFast(); render();})().catch(e=>{$('#view').textContent=e.message;});
</script>
</body></html>
"""


DEBUG_APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Manual Review</title>
<style>
:root { --bg:#f4f5f2; --panel:#fff; --line:#d8d9d2; --text:#20242a; --muted:#65707c; --accent:#0b7285; --accent2:#e6f6f8; --bad:#b42318; --warn:#985a06; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
button, textarea, select, input { font:inherit; }
.app { display:grid; grid-template-columns:340px minmax(0,1fr); min-height:100vh; }
.sidebar { background:#fbfbf8; border-right:1px solid var(--line); padding:14px; position:sticky; top:0; height:100vh; overflow:auto; }
.main { padding:18px 22px 40px; }
h1 { margin:0 0 4px; font-size:18px; }
h2 { margin:0 0 6px; font-size:22px; }
.muted { color:var(--muted); }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; }
.stats { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin:12px 0; }
.stat { background:white; border:1px solid var(--line); border-radius:7px; padding:8px; }
.stat b { display:block; font-size:18px; }
.tabs, .filters, .actions { display:flex; flex-wrap:wrap; gap:6px; margin:10px 0; }
button { border:1px solid var(--line); background:white; border-radius:6px; padding:7px 9px; cursor:pointer; }
button.active, button.primary { border-color:var(--accent); background:var(--accent2); color:#07525f; }
button.danger { color:var(--bad); border-color:#efb6b0; }
.case-list { display:grid; gap:7px; margin-top:10px; }
.case-btn { text-align:left; padding:9px; border:1px solid var(--line); background:white; border-radius:7px; cursor:pointer; }
.case-btn.active { border-color:var(--accent); background:var(--accent2); }
.case-btn .name { font-weight:650; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.case-btn .meta { color:var(--muted); font-size:12px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:14px; }
.grid { display:grid; grid-template-columns:minmax(0,1.1fr) minmax(320px,.9fr); gap:14px; }
.images { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:10px; }
.image { border:1px solid var(--line); border-radius:7px; overflow:hidden; background:#fafafa; }
.image img { display:block; width:100%; aspect-ratio:4/3; object-fit:cover; background:#e9ecef; }
.image .cap { padding:7px; color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:#f7f7f5; border:1px solid var(--line); border-radius:7px; padding:10px; max-height:360px; overflow:auto; }
textarea { width:100%; min-height:92px; border:1px solid var(--line); border-radius:7px; padding:8px; resize:vertical; }
select, input { border:1px solid var(--line); border-radius:6px; padding:7px 8px; background:white; max-width:100%; }
.formrow { display:grid; grid-template-columns:150px minmax(0,1fr); gap:8px; align-items:start; margin:8px 0; }
.toast { position:fixed; right:18px; bottom:18px; background:#17212b; color:white; padding:9px 12px; border-radius:7px; opacity:0; transform:translateY(8px); transition:.18s ease; pointer-events:none; }
.toast.show { opacity:1; transform:translateY(0); }
.empty { padding:40px; text-align:center; color:var(--muted); }
@media (max-width:900px) { .app{grid-template-columns:1fr;} .sidebar{position:relative;height:auto;} .grid{grid-template-columns:1fr;} }
</style>
</head>
<body>
<div class="app">
<aside class="sidebar">
  <h1>Manual Review</h1>
  <div id="meta" class="muted mono"></div>
  <div class="stats">
    <div class="stat"><b id="total">0</b><span>Total</span></div>
    <div class="stat"><b id="done">0</b><span>Done</span></div>
    <div class="stat"><b id="todo">0</b><span>Todo</span></div>
  </div>
  <div id="tabs" class="tabs"></div>
  <div class="filters">
    <button data-filter="all" class="active">All</button>
    <button data-filter="todo">Todo</button>
    <button data-filter="done">Done</button>
    <button data-filter="unsure">Unsure</button>
  </div>
  <div id="list" class="case-list"></div>
</aside>
<main class="main"><div id="view" class="empty">Loading</div></main>
</div>
<div id="toast" class="toast"></div>
<script>
let snapshot=null, decisions=null, active=null, tab='all', filter='all';
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(url){const r=await fetch(url); if(!r.ok) throw new Error(await r.text()); return await r.json();}
function dec(id){return decisions?.decisions?.[id]||{};}
function toast(t){const el=$('#toast'); el.textContent=t; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),1200);}
function cases(){return (snapshot?.cases||[]).filter(c=>{const d=dec(c.case_id).decision; if(tab!=='all'&&c.tab!==tab)return false; if(filter==='todo')return !d; if(filter==='done')return !!d; if(filter==='unsure')return d==='unsure'; return true;});}
function renderStats(){const s=decisions?.summary||{}; $('#total').textContent=s.total_cases??0; $('#done').textContent=s.decided??0; $('#todo').textContent=s.undecided??0; $('#meta').textContent=snapshot?`${snapshot.counts.total_cases} cases · ${snapshot.generated_at}`:'';}
function renderTabs(){const counts=snapshot?.counts?.by_tab||{}; const names=['all',...Object.keys(counts).sort()]; $('#tabs').innerHTML=names.map(n=>`<button class="${n===tab?'active':''}" data-tab="${esc(n)}">${esc(n)} ${n==='all'?snapshot.counts.total_cases:counts[n]}</button>`).join(''); document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab; active=null; render();});}
function renderList(){const cs=cases(); if(!active&&cs[0])active=cs[0].case_id; $('#list').innerHTML=cs.map(c=>{const d=dec(c.case_id).decision||'undecided';return `<button class="case-btn ${c.case_id===active?'active':''}" data-case="${esc(c.case_id)}"><div class="name">${esc(c.title)}</div><div class="meta">${esc(c.issue_code)} · ${esc(c.target_canonical_bld_id||'group')}</div><div class="meta">${esc(d)}</div></button>`}).join('')||'<div class="empty">No cases</div>'; document.querySelectorAll('[data-case]').forEach(b=>b.onclick=()=>{active=b.dataset.case; render();});}
function imageCards(c){const imgs=c?.target?.images||[]; if(!imgs.length)return ''; return `<div class="panel"><h3>Images</h3><div class="images">${imgs.map(im=>`<div class="image"><img src="${esc(im.url)}" loading="lazy"><div class="cap">${esc(im.kind||im.type||'image')}<br>${esc(im.url||'')}</div><button data-cover="${esc(im.url||'')}">Set cover</button></div>`).join('')}</div></div>`;}
function actionPanel(c){const d=dec(c.case_id); const payload=JSON.stringify(d.payload||{},null,2); return `<div class="panel"><h3>Decision</h3><div class="formrow"><label>Action</label><select id="action">${c.allowed_actions.map(a=>`<option ${d.decision===a?'selected':''}>${a}</option>`).join('')}</select></div><div class="formrow"><label>Payload JSON</label><textarea id="payload">${esc(payload)}</textarea></div><div class="formrow"><label>Notes</label><textarea id="notes">${esc(d.notes||'')}</textarea></div><div class="actions">${c.allowed_actions.map(a=>`<button data-action="${a}" class="${a==='unpublish'?'danger':''}">${a}</button>`).join('')}<button id="save" class="primary">Save</button></div></div>`;}
async function save(c, actionOverride=null, payloadOverride=null){let action=actionOverride||$('#action').value; let payload=payloadOverride; if(payload===null){try{payload=JSON.parse($('#payload').value||'{}');}catch(e){toast('Bad payload JSON');return;}} const notes=$('#notes')?.value||''; const r=await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({case_id:c.case_id,decision:action,payload,notes})}); if(!r.ok){toast(await r.text()); return;} decisions=await r.json(); toast('Saved'); render();}
function renderView(){const c=(snapshot?.cases||[]).find(x=>x.case_id===active); if(!c){$('#view').innerHTML='<div class="empty">No case selected</div>';return;} $('#view').innerHTML=`<div class="panel"><h2>${esc(c.title)}</h2><div class="muted mono">${esc(c.case_id)}</div><div class="muted">${esc(c.issue_code)} · ${esc(c.tab)} · ${esc(c.source_path)}</div></div><div class="grid"><div><div class="panel"><h3>Target</h3><pre>${esc(JSON.stringify(c.target,null,2))}</pre></div>${imageCards(c)}<div class="panel"><h3>Evidence</h3><pre>${esc(JSON.stringify(c.evidence,null,2))}</pre></div></div><div>${actionPanel(c)}</div></div>`; $('#save').onclick=()=>save(c); document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>save(c,b.dataset.action,{})); document.querySelectorAll('[data-cover]').forEach(b=>b.onclick=()=>save(c,'set_cover_to_image',{image_url:b.dataset.cover}));}
function render(){renderStats(); renderTabs(); document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter===filter)); renderList(); renderView();}
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter; active=null; render();});
(async()=>{snapshot=await load('/api/snapshot'); decisions=await load('/api/decisions'); render();})().catch(e=>{$('#view').textContent=e.message;});
</script>
</body></html>
"""


class ManualReviewHandler(BaseHTTPRequestHandler):
    snapshot_path = SNAPSHOT_PATH
    decisions_path = DECISIONS_PATH
    vocab_only = False

    def _snapshot(self) -> dict[str, Any]:
        if self.vocab_only:
            return {"cases": [], "snapshot_path": "(vocab-only)"}
        return read_json(self.snapshot_path)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=json_default).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send_text(FAST_APP_HTML, content_type="text/html")
            return
        if self.path in ("/debug", "/debug.html"):
            self._send_text(DEBUG_APP_HTML, content_type="text/html")
            return
        if self.path == "/api/snapshot":
            self._send_json(self._snapshot())
            return
        if self.path == "/api/decisions":
            snapshot = self._snapshot()
            self._send_json(ensure_decisions(snapshot, self.decisions_path))
            return
        if self.path == "/api/fast-snapshot":
            snapshot = self._snapshot()
            decisions = ensure_decisions(snapshot, self.decisions_path)
            self._send_json(build_fast_snapshot(snapshot, decisions))
            return
        self._send_text("not found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/api/decision", "/api/fast-decision"):
            self._send_text("not found", HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            snapshot = self._snapshot()
            if self.path == "/api/fast-decision":
                decisions = save_fast_decision(snapshot, payload, self.decisions_path)
                self._send_json(build_fast_snapshot(snapshot, decisions))
            else:
                decisions = save_decision(snapshot, payload, self.decisions_path)
                self._send_json(decisions)
        except Exception as exc:  # noqa: BLE001
            self._send_text(str(exc), HTTPStatus.BAD_REQUEST)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("manual_review_app: " + (fmt % args) + "\n")


def apply_vocab_labels(decisions_path: Path = DECISIONS_PATH, labels_path: Path = VOCAB_LABELS_PATH) -> dict[str, Any]:
    """Patch tag_vocabulary_labels.json with reviewed vocab decisions (local only)."""
    decisions = read_json(decisions_path) if decisions_path.exists() else {}
    vocab_decisions = decisions.get("vocab_decisions") or {}
    labels = read_json(labels_path)
    axes = labels.get("axes") or {}
    counts = Counter()
    unsure = []
    for key, item in sorted(vocab_decisions.items()):
        axis, _, tag = key.partition("|")
        decision = item.get("decision")
        counts[decision] += 1
        if decision == "unsure":
            unsure.append(key)
            continue
        entry = axes.setdefault(axis, {}).setdefault(tag, {})
        entry["reviewed"] = True
        if decision == "edit":
            payload = item.get("payload") or {}
            if payload.get("ko"):
                entry["ko"] = payload["ko"]
            if payload.get("en"):
                entry["en"] = payload["en"]
            entry["is_generic"] = bool(payload.get("is_generic"))
    atomic_write_json(labels_path, labels)
    report = {
        "status": "PASS",
        "labels_path": _rel(labels_path),
        "decision_counts": dict(counts),
        "unsure": unsure,
        "total_decided": sum(counts.values()),
    }
    return report


def serve(
    snapshot_path: Path = SNAPSHOT_PATH,
    decisions_path: Path = DECISIONS_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    vocab_only: bool = False,
) -> None:
    if not vocab_only and not snapshot_path.exists():
        build_snapshot(output_path=snapshot_path, decisions_path=decisions_path)
    handler = type(
        "ConfiguredManualReviewHandler",
        (ManualReviewHandler,),
        {"snapshot_path": snapshot_path, "decisions_path": decisions_path, "vocab_only": vocab_only},
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Manual review dashboard: http://{host}:{port}" + (" (vocab-only)" if vocab_only else ""))
    print(f"Snapshot: {'(none — vocab-only)' if vocab_only else _rel(snapshot_path)}")
    print(f"Decisions: {_rel(decisions_path)}")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual review audit/dashboard/applier workflow")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_audit = sub.add_parser("audit")
    p_audit.add_argument("--no-neon", action="store_true")
    p_audit.add_argument("--output-dir", type=Path, default=REPORT_DIR)

    p_snapshot = sub.add_parser("snapshot")
    p_snapshot.add_argument("--output", type=Path, default=SNAPSHOT_PATH)
    p_snapshot.add_argument("--decisions", type=Path, default=DECISIONS_PATH)
    p_snapshot.add_argument("--source-artifact", type=Path, default=C23_EMBEDDED)
    p_snapshot.add_argument("--extra-jsonl", type=parse_extra_issue_path, action="append", default=[])
    p_snapshot.add_argument("--with-audit", action="store_true")
    p_snapshot.add_argument("--no-neon", action="store_true")

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    p_serve.add_argument("--decisions", type=Path, default=None)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--vocab-only", action="store_true",
                         help="vocab label cards only — no audit snapshot, no case cards")

    p_validate = sub.add_parser("validate-decisions")
    p_validate.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    p_validate.add_argument("--decisions", type=Path, default=DECISIONS_PATH)

    p_apply = sub.add_parser("apply-decisions")
    p_apply.add_argument("--input", type=Path, default=C23_EMBEDDED)
    p_apply.add_argument("--decisions", type=Path, default=DECISIONS_PATH)
    p_apply.add_argument("--output", type=Path, default=C24_EMBEDDED)
    p_apply.add_argument("--patch", type=Path, default=PATCH_PATH)
    p_apply.add_argument("--write-artifact", action="store_true")

    p_vocab = sub.add_parser("apply-vocab-labels")
    p_vocab.add_argument("--decisions", type=Path, default=REPORT_DIR / "vocab_label_decisions.json")
    p_vocab.add_argument("--labels", type=Path, default=VOCAB_LABELS_PATH)

    p_vocab_ex = sub.add_parser("vocab-examples")
    p_vocab_ex.add_argument("--labels", type=Path, default=VOCAB_LABELS_PATH)
    p_vocab_ex.add_argument("--output", type=Path, default=VOCAB_EXAMPLES_PATH)
    p_vocab_ex.add_argument("--per-tag", type=int, default=3)

    args = parser.parse_args()
    if args.cmd == "audit":
        result = run_audit(include_neon=not args.no_neon, output_dir=args.output_dir)
        print(json.dumps(result["paths"], ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "snapshot":
        audit_result = run_audit(include_neon=not args.no_neon, output_dir=args.output.parent) if args.with_audit else None
        snapshot = build_snapshot(
            output_path=args.output,
            decisions_path=args.decisions,
            source_artifact=args.source_artifact,
            extra_issue_paths=args.extra_jsonl,
            audit_result=audit_result,
        )
        print(json.dumps({"snapshot": _rel(args.output), "decisions": _rel(args.decisions), "counts": snapshot["counts"]}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "serve":
        # vocab-only uses its own decisions file: ensure_decisions rebuilds the
        # case map from the (empty) snapshot, which would drop prior case
        # decisions if pointed at a shared file.
        decisions = args.decisions or (
            REPORT_DIR / "vocab_label_decisions.json" if args.vocab_only else DECISIONS_PATH)
        serve(args.snapshot, decisions, args.host, args.port, vocab_only=args.vocab_only)
        return 0
    if args.cmd == "validate-decisions":
        snapshot = read_json(args.snapshot)
        decisions = ensure_decisions(snapshot, args.decisions)
        invalid = []
        for item in (decisions.get("decisions") or {}).values():
            if item.get("decision"):
                try:
                    validate_decision(snapshot, item)
                except Exception as exc:  # noqa: BLE001
                    invalid.append({"case_id": item.get("case_id"), "reason": str(exc)})
        report = {"status": "PASS" if not invalid else "FAIL", "invalid": invalid, "summary": decisions.get("summary")}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not invalid else 1
    if args.cmd == "apply-decisions":
        report = apply_decisions(
            input_path=args.input,
            decisions_path=args.decisions,
            output_path=args.output,
            patch_path=args.patch,
            write_artifact=args.write_artifact,
        )
        print(json.dumps({k: report[k] for k in ("status", "changed_rows", "publishable_delta", "invalid_decisions", "patch_path")}, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS" else 1
    if args.cmd == "apply-vocab-labels":
        report = apply_vocab_labels(decisions_path=args.decisions, labels_path=args.labels)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "vocab-examples":
        payload = build_vocab_label_examples(
            labels_path=args.labels, output_path=args.output, per_tag=args.per_tag)
        n_tags = sum(len(v) for v in payload["axes"].values())
        print(json.dumps({"output": _rel(args.output), "tags_with_examples": n_tags,
                          "per_tag": args.per_tag}, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
