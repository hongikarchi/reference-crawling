#!/usr/bin/env python3
"""Local manual review app for cover/image duplicate audit cases.

This tool is intentionally read-mostly:

- Snapshot mode reads Neon `canonical_v2_buildings` with SELECT only and writes
  a local JSON snapshot for review.
- Server mode serves that snapshot and writes local decision JSON only.
- It never updates Neon, R2, or canonical artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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

from tools.canonical_v2_c16_url_canon import _canonical_asset_key  # noqa: E402
from tools.canonical_v2_neon_loader import _connect  # noqa: E402

REPORT_DIR = ROOT / "data/reports/audit_2026-05-27"
SNAPSHOT_PATH = REPORT_DIR / "cover_review_snapshot.json"
DECISIONS_PATH = REPORT_DIR / "cover_review_decisions.json"

TABLE = "canonical_v2_buildings"
ALLOWED_DECISIONS = {"keep", "set_cover_to_image", "unpublish", "merge", "unsure"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> str:
    return str(value)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=json_default)
        f.write("\n")
        tmp = Path(f.name)
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_jsonish(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def image_asset(url: str | None) -> str:
    if not url:
        return ""
    try:
        return _canonical_asset_key(url) or ""
    except Exception:
        return ""


def stable_id(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return h


def fetch_publishable_rows() -> dict[str, dict[str, Any]]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT canonical_bld_id, name, architects_text, architect_names,
               project_year, year_kind, location_city, location_country,
               program, display_cover_url, cover_image_url_default,
               all_images, source_refs, is_publishable, publishability_reasons
        FROM {TABLE}
        WHERE is_publishable = true
        ORDER BY canonical_bld_id
        """
    )
    cols = [d[0] for d in cur.description]
    rows: dict[str, dict[str, Any]] = {}
    for raw in cur.fetchall():
        row = dict(zip(cols, raw))
        cid = str(row["canonical_bld_id"])
        row["architect_names"] = parse_jsonish(row.get("architect_names"), [])
        row["all_images"] = parse_jsonish(row.get("all_images"), [])
        row["source_refs"] = parse_jsonish(row.get("source_refs"), {})
        row["publishability_reasons"] = parse_jsonish(row.get("publishability_reasons"), [])
        rows[cid] = row
    cur.close()
    conn.close()
    return rows


def normalize_image(cid: str, image: dict[str, Any], index: int, cover_asset: str) -> dict[str, Any]:
    url = str(image.get("url") or "")
    asset = image_asset(url)
    order = image.get("image_order", image.get("rank", index))
    try:
        order_int = int(order)
    except Exception:
        order_int = index
    return {
        "image_id": f"{cid}:{index}:{stable_id(url)}",
        "url": url,
        "asset_key": asset,
        "kind": image.get("kind"),
        "image_order": order_int,
        "rank": image.get("rank"),
        "type": image.get("type"),
        "source": image.get("source"),
        "source_id": image.get("source_id"),
        "phash": image.get("phash"),
        "w": image.get("w"),
        "h": image.get("h"),
        "is_current_cover": bool(asset and cover_asset and asset == cover_asset),
    }


def row_images(row: dict[str, Any]) -> list[dict[str, Any]]:
    cid = str(row["canonical_bld_id"])
    cover_asset = image_asset(row.get("display_cover_url"))
    images: list[dict[str, Any]] = []
    for index, image in enumerate(row.get("all_images") or []):
        if isinstance(image, dict) and image.get("url"):
            images.append(normalize_image(cid, image, index, cover_asset))
    images.sort(key=lambda im: (not im["is_current_cover"], im.get("image_order") or 0, im["image_id"]))
    return images


def current_cover_image(row: dict[str, Any], images: list[dict[str, Any]]) -> dict[str, Any] | None:
    cover_asset = image_asset(row.get("display_cover_url"))
    if cover_asset:
        for image in images:
            if image.get("asset_key") == cover_asset:
                return image
    cover_url = row.get("display_cover_url")
    if cover_url:
        return {
            "image_id": f"{row['canonical_bld_id']}:display_cover:{stable_id(str(cover_url))}",
            "url": cover_url,
            "asset_key": cover_asset,
            "kind": "display_cover",
            "image_order": -1,
            "rank": None,
            "type": None,
            "source": None,
            "source_id": None,
            "phash": None,
            "w": None,
            "h": None,
            "is_current_cover": True,
        }
    return None


def row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_bld_id": row.get("canonical_bld_id"),
        "name": row.get("name"),
        "architects_text": row.get("architects_text"),
        "architect_names": row.get("architect_names") or [],
        "project_year": row.get("project_year"),
        "year_kind": row.get("year_kind"),
        "location_city": row.get("location_city"),
        "location_country": row.get("location_country"),
        "program": row.get("program"),
        "display_cover_url": row.get("display_cover_url"),
        "cover_image_url_default": row.get("cover_image_url_default"),
        "source_refs": row.get("source_refs") or {},
        "is_publishable": row.get("is_publishable"),
        "publishability_reasons": row.get("publishability_reasons") or [],
    }


def group_refs_by_cid(refs: list[dict[str, Any]], rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_cid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ref in refs:
        by_cid[ref["cid"]].append(ref["image"])
    out: list[dict[str, Any]] = []
    for cid in sorted(by_cid):
        seen = set()
        images = []
        for image in by_cid[cid]:
            key = image.get("image_id")
            if key in seen:
                continue
            seen.add(key)
            images.append(image)
        out.append({"building": row_summary(rows[cid]), "matching_images": images})
    return out


def case_target(row: dict[str, Any], images: list[dict[str, Any]]) -> dict[str, Any]:
    return {**row_summary(row), "images": images, "current_cover_image": current_cover_image(row, images)}


def build_snapshot() -> dict[str, Any]:
    rows = fetch_publishable_rows()
    images_by_cid = {cid: row_images(row) for cid, row in rows.items()}

    refs_by_phash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    refs_by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cid, images in images_by_cid.items():
        for image in images:
            ref = {"cid": cid, "image": image}
            if image.get("phash"):
                refs_by_phash[str(image["phash"])].append(ref)
            if image.get("url"):
                refs_by_url[str(image["url"])].append(ref)

    cases: list[dict[str, Any]] = []

    for cid in sorted(rows):
        row = rows[cid]
        images = images_by_cid[cid]
        cover = current_cover_image(row, images)
        phash = str((cover or {}).get("phash") or "")
        if not phash:
            continue
        evidence_refs = [r for r in refs_by_phash.get(phash, []) if r["cid"] != cid]
        if not evidence_refs:
            continue
        case_id = f"cover_phash:{cid}:{phash[:16]}"
        other_cids = sorted({r["cid"] for r in evidence_refs})
        cases.append(
            {
                "case_id": case_id,
                "issue_code": "COVER_PHASH_SHARED_ACROSS_BUILDINGS",
                "target_canonical_bld_id": cid,
                "evidence_key": phash,
                "evidence_label": f"target display cover phash shared with {', '.join(other_cids)}",
                "target": case_target(row, images),
                "evidence_buildings": group_refs_by_cid(evidence_refs, rows),
            }
        )

    # Exact shared URL review: only target rows where a non-cover gallery image
    # URL is also attached to another publishable row.
    for url in sorted(refs_by_url):
        refs = refs_by_url[url]
        cids = sorted({r["cid"] for r in refs})
        if len(cids) < 2:
            continue
        for cid in cids:
            target_refs = [r for r in refs if r["cid"] == cid]
            row = rows[cid]
            cover_asset = image_asset(row.get("display_cover_url"))
            has_gallery_ref = any(
                r["image"].get("kind") != "cover"
                and r["image"].get("asset_key") != cover_asset
                for r in target_refs
            )
            if not has_gallery_ref:
                continue
            evidence_refs = [r for r in refs if r["cid"] != cid]
            case_id = f"gallery_exact_url:{cid}:{stable_id(url)}"
            cases.append(
                {
                    "case_id": case_id,
                    "issue_code": "GALLERY_IMAGE_SHARED_ACROSS_BUILDINGS",
                    "target_canonical_bld_id": cid,
                    "evidence_key": url,
                    "evidence_label": f"target gallery URL also appears on {', '.join(sorted({r['cid'] for r in evidence_refs}))}",
                    "target": case_target(row, images_by_cid[cid]),
                    "evidence_buildings": group_refs_by_cid(evidence_refs, rows),
                }
            )

    cases.sort(key=lambda c: (c["issue_code"], c["target_canonical_bld_id"], c["case_id"]))
    counts = Counter(c["issue_code"] for c in cases)
    snapshot = {
        "version": 1,
        "generated_at": now_iso(),
        "source": "read-only scan of Neon canonical_v2_buildings publishable rows",
        "db_writes": "none",
        "rows_scanned": len(rows),
        "counts": {
            "total_cases": len(cases),
            "by_issue": dict(sorted(counts.items())),
        },
        "cases": cases,
    }
    atomic_write_json(SNAPSHOT_PATH, snapshot)
    return snapshot


def blank_decision(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "issue_code": case["issue_code"],
        "target_canonical_bld_id": case["target_canonical_bld_id"],
        "decision": None,
        "selected_image_id": None,
        "selected_image_url": None,
        "notes": "",
        "updated_at": None,
    }


def summarize_decisions(snapshot: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    by_action = Counter()
    decided = 0
    unsure = 0
    for case in snapshot.get("cases", []):
        d = (decisions.get("decisions") or {}).get(case["case_id"], {})
        action = d.get("decision")
        if action:
            decided += 1
            by_action[action] += 1
            if action == "unsure":
                unsure += 1
    total = len(snapshot.get("cases", []))
    return {
        "total_cases": total,
        "decided": decided,
        "undecided": total - decided,
        "unsure": unsure,
        "by_action": dict(sorted(by_action.items())),
    }


def ensure_decisions(snapshot: dict[str, Any]) -> dict[str, Any]:
    existing: dict[str, Any] = {}
    if DECISIONS_PATH.exists():
        existing = read_json(DECISIONS_PATH)
    existing_decisions = existing.get("decisions") or {}
    merged = {
        "version": 1,
        "snapshot_path": str(SNAPSHOT_PATH.relative_to(ROOT)),
        "updated_at": existing.get("updated_at") or now_iso(),
        "db_writes": "none",
        "summary": {},
        "decisions": {},
    }
    for case in snapshot.get("cases", []):
        current = blank_decision(case)
        previous = existing_decisions.get(case["case_id"])
        if isinstance(previous, dict):
            current.update({k: previous.get(k, current.get(k)) for k in current})
        merged["decisions"][case["case_id"]] = current
    merged["summary"] = summarize_decisions(snapshot, merged)
    if merged != existing:
        atomic_write_json(DECISIONS_PATH, merged)
    return merged


def validate_decision(snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    case_id = str(payload.get("case_id") or "")
    cases = {c["case_id"]: c for c in snapshot.get("cases", [])}
    if case_id not in cases:
        raise ValueError("unknown case_id")
    decision = payload.get("decision")
    if decision is not None:
        decision = str(decision)
    if decision not in ALLOWED_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(ALLOWED_DECISIONS)}")

    case = cases[case_id]
    images = case.get("target", {}).get("images") or []
    valid_by_id = {im.get("image_id"): im for im in images}
    valid_urls = {im.get("url") for im in images if im.get("url")}
    selected_image_id = payload.get("selected_image_id")
    selected_image_url = payload.get("selected_image_url")

    if decision == "set_cover_to_image":
        if selected_image_id and selected_image_id in valid_by_id:
            selected_image_url = valid_by_id[selected_image_id].get("url")
        if not selected_image_url or selected_image_url not in valid_urls:
            raise ValueError("set_cover_to_image requires a target all_images URL")
        if not selected_image_id:
            for im in images:
                if im.get("url") == selected_image_url:
                    selected_image_id = im.get("image_id")
                    break
    else:
        selected_image_id = None
        selected_image_url = None

    notes = payload.get("notes")
    if notes is None:
        notes = ""
    notes = str(notes)[:2000]

    return {
        "case_id": case_id,
        "issue_code": case["issue_code"],
        "target_canonical_bld_id": case["target_canonical_bld_id"],
        "decision": decision,
        "selected_image_id": selected_image_id,
        "selected_image_url": selected_image_url,
        "notes": notes,
        "updated_at": now_iso(),
    }


def save_decision(snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    decisions = ensure_decisions(snapshot)
    item = validate_decision(snapshot, payload)
    decisions["decisions"][item["case_id"]] = item
    decisions["updated_at"] = now_iso()
    decisions["summary"] = summarize_decisions(snapshot, decisions)
    atomic_write_json(DECISIONS_PATH, decisions)
    return decisions


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cover Review</title>
<style>
:root {
  --bg: #f5f5f2;
  --panel: #ffffff;
  --line: #d7d7cf;
  --text: #1f2933;
  --muted: #5e6a75;
  --accent: #0b7285;
  --accent-2: #e6f7f9;
  --danger: #b42318;
  --warn: #a15c07;
}
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--text); background: var(--bg); }
button, textarea { font: inherit; }
.app { display: grid; grid-template-columns: 330px minmax(0, 1fr); min-height: 100vh; }
.sidebar { border-right: 1px solid var(--line); background: #fbfbf8; padding: 14px; position: sticky; top: 0; height: 100vh; overflow: auto; }
.main { padding: 18px 22px 40px; }
.topline { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
.title { font-size: 18px; font-weight: 700; margin: 0; }
.pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 8px; border: 1px solid var(--line); border-radius: 999px; background: white; color: var(--muted); font-size: 12px; }
.statgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 10px 0; }
.stat { background: white; border: 1px solid var(--line); border-radius: 7px; padding: 8px; }
.stat b { display: block; font-size: 17px; }
.filters { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin: 10px 0; }
.filters button, .actions button, .image button { border: 1px solid var(--line); background: white; border-radius: 6px; padding: 7px 8px; cursor: pointer; }
.filters button.active { border-color: var(--accent); background: var(--accent-2); color: #07525f; }
.case-list { display: grid; gap: 7px; margin-top: 10px; }
.case-button { text-align: left; border: 1px solid var(--line); background: white; border-radius: 7px; padding: 9px; cursor: pointer; }
.case-button.active { border-color: var(--accent); background: var(--accent-2); }
.case-button .name { font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.case-button .meta { color: var(--muted); font-size: 12px; }
.case-button .decision { margin-top: 5px; font-size: 12px; color: var(--accent); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; margin-bottom: 14px; }
.case-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; }
.case-head h2 { margin: 0 0 5px; font-size: 22px; }
.muted { color: var(--muted); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.section-title { margin: 0 0 10px; font-size: 15px; font-weight: 700; }
.comparison { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 14px; }
.image-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
.image-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
.image { border: 1px solid var(--line); border-radius: 7px; overflow: hidden; background: #fafafa; }
.image.match { border-color: var(--warn); }
.image.current { border-color: var(--accent); }
.image img { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; background: #e9ecef; }
.image .cap { padding: 7px; font-size: 12px; color: var(--muted); min-height: 52px; }
.image .cap b { color: var(--text); }
.image button { width: calc(100% - 14px); margin: 0 7px 7px; background: white; }
.image button.primary { border-color: var(--accent); background: var(--accent-2); color: #07525f; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.actions button.primary { background: var(--accent); color: white; border-color: var(--accent); }
.actions button.danger { color: var(--danger); border-color: #f0b7b2; }
.actions button.warn { color: var(--warn); border-color: #edc98f; }
textarea { width: 100%; min-height: 74px; resize: vertical; border: 1px solid var(--line); border-radius: 7px; padding: 8px; }
.toast { position: fixed; right: 18px; bottom: 18px; background: #17212b; color: white; padding: 9px 12px; border-radius: 7px; opacity: 0; transform: translateY(8px); transition: .18s ease; pointer-events: none; }
.toast.show { opacity: 1; transform: translateY(0); }
.empty { padding: 40px; text-align: center; color: var(--muted); }
@media (max-width: 900px) {
  .app { grid-template-columns: 1fr; }
  .sidebar { position: relative; height: auto; }
  .comparison { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <h1 class="title">Cover Review</h1>
    <div id="snapshotMeta" class="muted mono"></div>
    <div class="statgrid">
      <div class="stat"><b id="statTotal">0</b><span>Total</span></div>
      <div class="stat"><b id="statDone">0</b><span>Done</span></div>
      <div class="stat"><b id="statTodo">0</b><span>Todo</span></div>
    </div>
    <div class="filters">
      <button data-filter="all" class="active">All</button>
      <button data-filter="todo">Todo</button>
      <button data-filter="cover">Cover</button>
      <button data-filter="gallery">Gallery</button>
      <button data-filter="unsure">Unsure</button>
      <button data-filter="done">Done</button>
    </div>
    <div id="caseList" class="case-list"></div>
  </aside>
  <main class="main">
    <div id="caseView" class="empty">Loading...</div>
  </main>
</div>
<div id="toast" class="toast"></div>
<script>
let snapshot = null;
let decisions = null;
let activeCaseId = null;
let filter = 'all';

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function loadJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

async function postDecision(payload) {
  const res = await fetch('/api/decision', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error(await res.text());
  decisions = await res.json();
  toast('Saved');
  render();
}

function toast(text) {
  const t = $('#toast');
  t.textContent = text;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1200);
}

function decisionFor(caseId) {
  return decisions?.decisions?.[caseId] || {};
}

function filteredCases() {
  const cases = snapshot?.cases || [];
  return cases.filter(c => {
    const d = decisionFor(c.case_id).decision;
    if (filter === 'todo') return !d;
    if (filter === 'done') return !!d;
    if (filter === 'unsure') return d === 'unsure';
    if (filter === 'cover') return c.issue_code === 'COVER_PHASH_SHARED_ACROSS_BUILDINGS';
    if (filter === 'gallery') return c.issue_code === 'GALLERY_IMAGE_SHARED_ACROSS_BUILDINGS';
    return true;
  });
}

function renderStats() {
  const s = decisions?.summary || {};
  $('#statTotal').textContent = s.total_cases ?? 0;
  $('#statDone').textContent = s.decided ?? 0;
  $('#statTodo').textContent = s.undecided ?? 0;
  $('#snapshotMeta').textContent = snapshot ? `${snapshot.rows_scanned} publishable rows scanned` : '';
}

function renderList() {
  const list = $('#caseList');
  const cases = filteredCases();
  if (!activeCaseId && cases[0]) activeCaseId = cases[0].case_id;
  list.innerHTML = cases.map(c => {
    const d = decisionFor(c.case_id);
    const issue = c.issue_code === 'COVER_PHASH_SHARED_ACROSS_BUILDINGS' ? 'cover phash' : 'gallery URL';
    return `<button class="case-button ${c.case_id === activeCaseId ? 'active' : ''}" data-case="${esc(c.case_id)}">
      <div class="name">${esc(c.target.name)}</div>
      <div class="meta">${esc(c.target_canonical_bld_id)} · ${issue}</div>
      <div class="decision">${esc(d.decision || 'undecided')}</div>
    </button>`;
  }).join('') || '<div class="empty">No cases</div>';
  list.querySelectorAll('[data-case]').forEach(btn => {
    btn.addEventListener('click', () => {
      activeCaseId = btn.dataset.case;
      render();
    });
  });
}

function metaBlock(b) {
  return `<div class="muted">
    <div><span class="mono">${esc(b.canonical_bld_id)}</span></div>
    <div>${esc(b.architects_text || (b.architect_names || []).join(', '))}</div>
    <div>${esc(b.project_year || '?')} (${esc(b.year_kind || '?')}) · ${esc(b.location_city || '')}, ${esc(b.location_country || '')}</div>
    <div>program: ${esc(b.program || '')}</div>
    <div class="mono">sources: ${esc(JSON.stringify(b.source_refs || {}))}</div>
  </div>`;
}

function imgCard(image, opts = {}) {
  const classes = ['image'];
  if (image?.is_current_cover) classes.push('current');
  if (opts.match) classes.push('match');
  const label = [
    image?.is_current_cover ? 'current cover' : null,
    opts.match ? 'matching evidence' : null,
    image?.kind || null,
    image?.source ? `${image.source}:${image.source_id || ''}` : null,
    image?.phash ? `phash ${String(image.phash).slice(0, 16)}` : null
  ].filter(Boolean).join(' · ');
  const button = opts.canUse ? `<button class="primary" data-use-image="${esc(image.image_id)}">Use as cover</button>` : '';
  return `<div class="${classes.join(' ')}">
    <img loading="lazy" src="${esc(image?.url || '')}" alt="">
    <div class="cap"><b>${esc(opts.title || '')}</b><br>${esc(label)}</div>
    ${button}
  </div>`;
}

function renderCase() {
  const c = (snapshot?.cases || []).find(x => x.case_id === activeCaseId);
  const view = $('#caseView');
  if (!c) {
    view.className = 'empty';
    view.textContent = 'No case selected';
    return;
  }
  view.className = '';
  const d = decisionFor(c.case_id);
  const cover = c.target.current_cover_image;
  const evidence = (c.evidence_buildings || []).flatMap(b => (b.matching_images || []).map(im => ({building: b.building, image: im})));
  const selectedUrl = d.selected_image_url;
  view.innerHTML = `
    <div class="panel case-head">
      <div>
        <div class="pill">${esc(c.issue_code)}</div>
        <h2>${esc(c.target.name)}</h2>
        ${metaBlock(c.target)}
        <p class="muted">${esc(c.evidence_label)}</p>
      </div>
      <div class="pill">${esc(d.decision || 'undecided')}</div>
    </div>

    <div class="panel">
      <h3 class="section-title">Decision</h3>
      <div class="actions">
        <button class="primary" data-action="keep">Keep current cover</button>
        <button class="warn" data-action="unsure">Unsure</button>
        <button class="danger" data-action="unpublish">Unpublish target</button>
        <button data-action="merge">Merge flag</button>
      </div>
      <p class="muted">For cover replacement, click "Use as cover" under a target image below.</p>
      <textarea id="notes" placeholder="Notes">${esc(d.notes || '')}</textarea>
      <div class="actions"><button id="saveNotes">Save notes</button></div>
    </div>

    <div class="panel">
      <h3 class="section-title">Current Cover vs Evidence</h3>
      <div class="comparison">
        <div>
          <p class="muted">Target current cover</p>
          <div class="image-row">${cover ? imgCard(cover, {title: 'Target cover'}) : '<div class="empty">No current cover image</div>'}</div>
        </div>
        <div>
          <p class="muted">Exact matching evidence from other building rows</p>
          <div class="image-row">${evidence.map(e => imgCard(e.image, {match: true, title: e.building.canonical_bld_id + ' · ' + e.building.name})).join('') || '<div class="empty">No evidence image</div>'}</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h3 class="section-title">Target Images</h3>
      <div class="image-grid">
        ${(c.target.images || []).map(im => imgCard(im, {canUse: true, title: selectedUrl === im.url ? 'selected' : ''})).join('')}
      </div>
    </div>
  `;

  view.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => postDecision({
      case_id: c.case_id,
      decision: btn.dataset.action,
      notes: $('#notes')?.value || ''
    }).catch(e => toast(e.message)));
  });
  view.querySelectorAll('[data-use-image]').forEach(btn => {
    btn.addEventListener('click', () => postDecision({
      case_id: c.case_id,
      decision: 'set_cover_to_image',
      selected_image_id: btn.dataset.useImage,
      notes: $('#notes')?.value || ''
    }).catch(e => toast(e.message)));
  });
  $('#saveNotes')?.addEventListener('click', () => {
    const current = decisionFor(c.case_id);
    if (!current.decision) {
      toast('Choose a decision first');
      return;
    }
    postDecision({
      case_id: c.case_id,
      decision: current.decision,
      selected_image_id: current.selected_image_id,
      selected_image_url: current.selected_image_url,
      notes: $('#notes')?.value || ''
    }).catch(e => toast(e.message));
  });
}

function render() {
  renderStats();
  renderList();
  renderCase();
}

async function init() {
  document.querySelectorAll('[data-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-filter]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      filter = btn.dataset.filter;
      activeCaseId = null;
      render();
    });
  });
  snapshot = await loadJson('/api/snapshot');
  decisions = await loadJson('/api/decisions');
  activeCaseId = snapshot.cases?.[0]?.case_id || null;
  render();
}

init().catch(err => {
  $('#caseView').className = 'empty';
  $('#caseView').textContent = err.message;
});
</script>
</body>
</html>
"""


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "CoverReview/1.0"

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=json_default).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def snapshot(self) -> dict[str, Any]:
        if not SNAPSHOT_PATH.exists():
            raise FileNotFoundError(f"missing {SNAPSHOT_PATH}; run --build-snapshot")
        return read_json(SNAPSHOT_PATH)

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            if path == "/":
                self._text(APP_HTML, content_type="text/html")
            elif path == "/api/snapshot":
                self._json(self.snapshot())
            elif path == "/api/decisions":
                self._json(ensure_decisions(self.snapshot()))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if path == "/api/decision":
                self._json(save_decision(self.snapshot(), payload))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def check_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    for case in snapshot.get("cases", []):
        target = case.get("target") or {}
        if not target.get("current_cover_image"):
            failures.append(f"{case['case_id']}: missing current cover image")
        if not target.get("images"):
            failures.append(f"{case['case_id']}: missing target images")
        if not case.get("evidence_buildings"):
            failures.append(f"{case['case_id']}: missing evidence buildings")
        if case.get("issue_code") == "COVER_PHASH_SHARED_ACROSS_BUILDINGS":
            cover = target.get("current_cover_image") or {}
            if not cover.get("phash"):
                failures.append(f"{case['case_id']}: cover phash missing")
        if case.get("issue_code") == "GALLERY_IMAGE_SHARED_ACROSS_BUILDINGS":
            if not case.get("evidence_key", "").startswith("http"):
                failures.append(f"{case['case_id']}: exact URL evidence missing")
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures[:20],
        "failure_count": len(failures),
        "counts": snapshot.get("counts") or {},
    }


def serve(host: str, port: int) -> None:
    snapshot = read_json(SNAPSHOT_PATH)
    ensure_decisions(snapshot)
    httpd = ThreadingHTTPServer((host, port), ReviewHandler)
    print(f"Cover review app: http://{host}:{port}/")
    print(f"Snapshot: {SNAPSHOT_PATH}")
    print(f"Decisions: {DECISIONS_PATH}")
    httpd.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description="Local cover duplicate review app")
    ap.add_argument("--build-snapshot", action="store_true", help="read Neon and write local snapshot JSON")
    ap.add_argument("--serve", action="store_true", help="serve localhost review app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--check", action="store_true", help="validate existing snapshot")
    args = ap.parse_args()

    if args.build_snapshot:
        snapshot = build_snapshot()
        decisions = ensure_decisions(snapshot)
        print(json.dumps({"snapshot": snapshot["counts"], "decisions": decisions["summary"]}, indent=2))
    if args.check:
        snapshot = read_json(SNAPSHOT_PATH)
        result = check_snapshot(snapshot)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result["status"] != "PASS":
            return 1
    if args.serve:
        if not SNAPSHOT_PATH.exists():
            print(f"missing {SNAPSHOT_PATH}; run --build-snapshot", file=sys.stderr)
            return 2
        serve(args.host, args.port)
    if not (args.build_snapshot or args.check or args.serve):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
