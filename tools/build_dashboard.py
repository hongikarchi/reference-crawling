#!/usr/bin/env python3
"""Build a static, self-contained web dashboard for the make_db pipeline.

Read-only with respect to the project; the only files it writes are the two
dashboard outputs:

  docs/data/state.json   -- collected project state (one JSON object)
  docs/dashboard.html    -- self-contained single-file dashboard with the
                            state.json data embedded inline (works via file://)

Re-runnable and idempotent: `python3 tools/build_dashboard.py` regenerates
both files from the current project state.

State collected:
  pipeline  -- 5 pipeline stages (Crawl / Enrich / Canonical / Upload) + status
  db_state  -- Neon canonical_v2_buildings totals, publishable, tiers, freshness
  phases    -- reverse-chronological timeline parsed from .claude/ops/jobs/*.md
  files     -- du-style sizes of key data/ subdirectories + repo total
  git       -- current branch + last 10 commit subjects
  meta      -- generation timestamp
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_JSON = ROOT / "docs/data/state.json"
OUT_HTML = ROOT / "docs/dashboard.html"

# Stage 1: crawl databases -> (db filename, project-count table).
CRAWL_DBS: list[tuple[str, str]] = [
    ("divisare.db", "divisare_projects"),
    ("architizer.db", "architizer_projects"),
    ("archello.db", "archello_projects"),
    ("metalocus.db", "buildings"),
]

# Stage 2-4: the canonical artifact this pipeline currently pins.
CANONICAL_ARTIFACT = (
    ROOT
    / "data/canonical/country_conflict_refresh"
    / "canonical_buildings_strict_embedded.completeness_c8.json"
)

# Stage 5: Neon table.
NEON_TABLE = "canonical_v2_buildings"

# Deliverable-4 data subdirectories.
DATA_SUBDIRS = [
    "data/crawl",
    "data/canonical",
    "data/reports",
    "data/enrich",
    "data/backups",
]

# Audit pointers (deliverable 2).
AUDIT_VERDICT = "PASS with WARNINGS"
AUDIT_REPORT = "data/reports/db_quality_audit.md"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess; never raise. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:  # noqa: BLE001 - never let a tool abort the build
        return 1, "", str(exc)


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _dir_size_bytes(path: Path) -> int:
    """Sum of file sizes under `path` via os.walk; resilient to bad entries."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


# --------------------------------------------------------------------------
# collectors
# --------------------------------------------------------------------------
def collect_pipeline() -> dict[str, Any]:
    """5 pipeline stages: Crawl, Enrich, Canonical, Upload."""
    # Stage 1 -- crawl DB row counts.
    crawl_sources: list[dict[str, Any]] = []
    crawl_total = 0
    crawl_ok = True
    for db_name, table in CRAWL_DBS:
        db_path = ROOT / "data/crawl" / db_name
        entry: dict[str, Any] = {"db": db_name, "table": table}
        if not db_path.exists():
            entry.update(status="missing", count=None, error="db file not found")
            crawl_ok = False
            crawl_sources.append(entry)
            continue
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            try:
                cur = conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - table from fixed allowlist
                count = int(cur.fetchone()[0])
            finally:
                conn.close()
            entry.update(status="ok", count=count)
            crawl_total += count
        except Exception as exc:  # noqa: BLE001 - one DB must not block the rest
            entry.update(status="error", count=None, error=str(exc))
            crawl_ok = False
        crawl_sources.append(entry)

    stage_crawl = {
        "name": "Crawl",
        "stage": "Stage 1",
        "status": "ok" if crawl_ok else "warn",
        "total_projects": crawl_total,
        "sources": crawl_sources,
    }

    # Stage 2-3 Enrich + Stage 4 Canonical -- the canonical artifact.
    artifact_exists = CANONICAL_ARTIFACT.exists()
    artifact_size = (
        CANONICAL_ARTIFACT.stat().st_size if artifact_exists else None
    )
    canonical_block = {
        "name": "Canonical",
        "stage": "Stage 2-4 (Enrich + Canonical)",
        "status": "ok" if artifact_exists else "missing",
        "artifact": str(CANONICAL_ARTIFACT.relative_to(ROOT)),
        "artifact_exists": artifact_exists,
        "artifact_size_bytes": artifact_size,
        "artifact_size_human": _human_size(artifact_size)
        if artifact_size is not None
        else None,
    }

    # Stage 2-3 split out as its own "Enrich" box for the flow strip; it shares
    # the canonical artifact as its end product.
    enrich_block = {
        "name": "Enrich",
        "stage": "Stage 2-3 (LLM text + image enrich)",
        "status": "ok" if artifact_exists else "missing",
        "note": "Enriched rows roll up into the canonical artifact below.",
        "artifact": str(CANONICAL_ARTIFACT.relative_to(ROOT)),
        "artifact_exists": artifact_exists,
        "artifact_size_human": _human_size(artifact_size)
        if artifact_size is not None
        else None,
    }

    # Stage 5 -- Neon row count.
    upload_count: int | None = None
    upload_status = "warn"
    upload_error: str | None = None
    try:
        from tools.canonical_v2_neon_loader import _connect  # noqa: E402

        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {NEON_TABLE}")  # noqa: S608
                upload_count = int(cur.fetchone()[0])
            upload_status = "ok"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - Neon unreachable must not block build
        upload_error = str(exc)

    upload_block: dict[str, Any] = {
        "name": "Upload",
        "stage": "Stage 5 (Neon)",
        "status": upload_status,
        "table": NEON_TABLE,
        "row_count": upload_count,
    }
    if upload_error:
        upload_block["error"] = upload_error

    return {
        "stages": [stage_crawl, enrich_block, canonical_block, upload_block],
    }


def collect_db_state() -> dict[str, Any]:
    """Neon canonical_v2_buildings detail: totals, publishable, tiers, freshness."""
    state: dict[str, Any] = {
        "table": NEON_TABLE,
        "audit_verdict": AUDIT_VERDICT,
        "audit_report": AUDIT_REPORT,
    }
    try:
        from tools.canonical_v2_neon_loader import _connect  # noqa: E402

        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        COUNT(*)                                       AS total_rows,
                        COUNT(*) FILTER (WHERE is_publishable)          AS publishable_rows,
                        MAX(updated_at)                                 AS max_updated_at
                    FROM {NEON_TABLE}
                    """  # noqa: S608 - fixed table name
                )
                total_rows, publishable_rows, max_updated_at = cur.fetchone()

                cur.execute(
                    f"""
                    SELECT confidence_tier, COUNT(*)
                    FROM {NEON_TABLE}
                    GROUP BY confidence_tier
                    ORDER BY confidence_tier
                    """  # noqa: S608
                )
                tier_dist = {str(row[0]): int(row[1]) for row in cur.fetchall()}
        finally:
            conn.close()

        state.update(
            status="ok",
            total_rows=int(total_rows) if total_rows is not None else 0,
            publishable_rows=int(publishable_rows)
            if publishable_rows is not None
            else 0,
            confidence_tier_distribution=tier_dist,
            max_updated_at=max_updated_at,
        )
    except Exception as exc:  # noqa: BLE001 - record error, keep building
        state.update(status="error", error=str(exc))
    return state


def collect_phases() -> dict[str, Any]:
    """Reverse-chronological timeline from .claude/ops/jobs/*.md."""
    jobs_dir = ROOT / ".claude/ops/jobs"
    items: list[dict[str, Any]] = []
    if jobs_dir.is_dir():
        for md in sorted(jobs_dir.glob("*.md")):
            name = md.name
            stem = md.stem
            # Filename encodes a YYYYMMDD_ date prefix + a slug.
            date_prefix = ""
            slug = stem
            if "_" in stem:
                head, _, rest = stem.partition("_")
                if len(head) == 8 and head.isdigit():
                    date_prefix = head
                    slug = rest
            # First "- status:" line, if present.
            status: str | None = None
            try:
                for line in md.read_text(encoding="utf-8", errors="replace").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- status:"):
                        status = stripped[len("- status:"):].strip() or None
                        break
            except OSError:
                status = None
            date_display = (
                f"{date_prefix[0:4]}-{date_prefix[4:6]}-{date_prefix[6:8]}"
                if date_prefix
                else ""
            )
            items.append(
                {
                    "file": name,
                    "date_prefix": date_prefix,
                    "date_display": date_display,
                    "slug": slug,
                    "status": status,
                }
            )
    # Reverse-chronological: sort on the filename date prefix (stable, not mtime).
    items.sort(key=lambda it: (it["date_prefix"], it["file"]), reverse=True)
    return {"count": len(items), "jobs": items}


def collect_files() -> dict[str, Any]:
    """du-style sizes of key data/ subdirectories + repo total."""
    dirs: list[dict[str, Any]] = []
    for rel in DATA_SUBDIRS:
        path = ROOT / rel
        if path.is_dir():
            size = _dir_size_bytes(path)
            dirs.append(
                {
                    "path": rel,
                    "exists": True,
                    "size_bytes": size,
                    "size_human": _human_size(size),
                }
            )
        else:
            dirs.append(
                {"path": rel, "exists": False, "size_bytes": None, "size_human": None}
            )

    # Repo total: `du -sh .` over the whole project root.
    repo_total: dict[str, Any] = {"path": ".", "method": "du -sh ."}
    code, out, _err = _run(["du", "-sh", "."])
    if code == 0 and out.strip():
        repo_total["size_human"] = out.split()[0]
    else:
        # Fallback: os.walk the whole tree.
        size = _dir_size_bytes(ROOT)
        repo_total["size_bytes"] = size
        repo_total["size_human"] = _human_size(size)
    return {"data_subdirs": dirs, "repo_total": repo_total}


def collect_git() -> dict[str, Any]:
    """Current branch + last 10 commit subjects."""
    git: dict[str, Any] = {}
    code, out, _err = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    git["branch"] = out.strip() if code == 0 else None

    code, out, _err = _run(["git", "log", "-n", "10", "--pretty=format:%h %s"])
    commits: list[dict[str, str]] = []
    if code == 0:
        for line in out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            short, _, subject = line.partition(" ")
            commits.append({"hash": short, "subject": subject})
    git["recent_commits"] = commits
    return git


def collect_state() -> dict[str, Any]:
    """Assemble the full state object."""
    return {
        "pipeline": collect_pipeline(),
        "db_state": collect_db_state(),
        "phases": collect_phases(),
        "files": collect_files(),
        "git": collect_git(),
        "meta": {
            "generated_at": datetime.now(timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S %Z"),
            "generator": "tools/build_dashboard.py",
            "project_root": str(ROOT),
        },
    }


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>make_db pipeline dashboard</title>
<style>
  :root {
    --bg: #f4f5f7;
    --panel: #ffffff;
    --ink: #1f2329;
    --muted: #6b7280;
    --line: #e2e4e8;
    --accent: #2f6f4f;
    --ok: #1f9d57;
    --warn: #c98a14;
    --bad: #cc3d3d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px; line-height: 1.5;
  }
  .num, code, .mono {
    font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  }
  header {
    background: var(--panel); border-bottom: 1px solid var(--line);
    padding: 18px 28px;
  }
  header h1 { margin: 0; font-size: 18px; }
  header .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  main { max-width: 1100px; margin: 0 auto; padding: 24px 20px 48px; }
  section { margin-bottom: 28px; }
  section > h2 {
    font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); margin: 0 0 12px; font-weight: 600;
  }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 16px 18px;
  }

  /* pipeline flow strip */
  .flow { display: flex; align-items: stretch; gap: 0; flex-wrap: wrap; }
  .flow .stage {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 14px 16px; flex: 1 1 200px; min-width: 200px;
  }
  .flow .arrow {
    display: flex; align-items: center; color: var(--muted);
    font-size: 20px; padding: 0 8px;
  }
  .stage .stage-tag {
    font-size: 11px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .stage .stage-name { font-size: 15px; font-weight: 600; margin: 2px 0 8px; }
  .stage .metric { font-size: 22px; }
  .stage .metric-label { font-size: 12px; color: var(--muted); }
  .stage .detail { font-size: 12px; color: var(--muted); margin-top: 8px; }
  .stage .detail .num { color: var(--ink); }

  .badge {
    display: inline-block; font-size: 11px; font-weight: 600;
    padding: 2px 8px; border-radius: 10px; text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .badge.ok { background: #e3f4ea; color: var(--ok); }
  .badge.warn { background: #fbf0d8; color: var(--warn); }
  .badge.bad, .badge.error, .badge.missing { background: #fae0e0; color: var(--bad); }

  /* grids */
  .kv { display: grid; grid-template-columns: auto 1fr; gap: 6px 18px; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; }
  .cols2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 720px) { .cols2 { grid-template-columns: 1fr; } }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td {
    text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line);
  }
  th { color: var(--muted); font-weight: 600; font-size: 12px; }
  td.r, th.r { text-align: right; }
  tbody tr:last-child td { border-bottom: none; }

  .tiers { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 4px; }
  .tier {
    border: 1px solid var(--line); border-radius: 6px;
    padding: 8px 14px; text-align: center; min-width: 80px;
  }
  .tier .t { font-size: 11px; color: var(--muted); }
  .tier .v { font-size: 18px; }

  .timeline { border-left: 2px solid var(--line); margin-left: 6px; }
  .timeline .row {
    position: relative; padding: 8px 0 8px 18px;
  }
  .timeline .row::before {
    content: ""; position: absolute; left: -6px; top: 14px;
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--accent); border: 2px solid var(--panel);
  }
  .timeline .when { font-size: 12px; color: var(--muted); }
  .timeline .what { font-weight: 500; }
  .timeline .file { font-size: 11px; color: var(--muted); }

  .git-commits li { margin-bottom: 4px; }
  .git-commits .hash { color: var(--accent); }
  .empty { color: var(--muted); font-style: italic; }
  footer {
    text-align: center; color: var(--muted); font-size: 12px;
    padding: 12px 0 0;
  }
</style>
</head>
<body>
<header>
  <h1>make_db &mdash; pipeline dashboard</h1>
  <div class="sub">crawl &rarr; enrich + analyze &rarr; canonical &rarr; upload (Neon + R2)
    &nbsp;&middot;&nbsp; generated <span class="mono" id="gen-ts"></span></div>
</header>
<main>
  <section>
    <h2>Pipeline flow</h2>
    <div class="flow" id="flow"></div>
  </section>

  <section>
    <h2>Database state &mdash; canonical_v2_buildings</h2>
    <div class="card" id="dbstate"></div>
  </section>

  <div class="cols2">
    <section>
      <h2>Phase history</h2>
      <div class="card"><div class="timeline" id="timeline"></div></div>
    </section>
    <section>
      <h2>Storage</h2>
      <div class="card" id="files"></div>
    </section>
  </div>

  <section>
    <h2>Git</h2>
    <div class="card" id="git"></div>
  </section>

  <footer>regenerate with <code>python3 tools/build_dashboard.py</code></footer>
</main>

<script id="state-data" type="application/json">__STATE_JSON__</script>
<script>
(function () {
  "use strict";
  var STATE = JSON.parse(document.getElementById("state-data").textContent);

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined && html !== null) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function num(n) {
    if (n === null || n === undefined) return "&mdash;";
    return Number(n).toLocaleString("en-US");
  }
  function statusClass(s) {
    s = (s || "").toLowerCase();
    if (s === "ok" || s === "pass" || s === "complete") return "ok";
    if (s === "warn" || s === "in_progress") return "warn";
    return "bad";
  }

  // header timestamp
  document.getElementById("gen-ts").textContent =
    (STATE.meta && STATE.meta.generated_at) || "unknown";

  // ---- pipeline flow strip --------------------------------------------
  var flow = document.getElementById("flow");
  var stages = (STATE.pipeline && STATE.pipeline.stages) || [];
  stages.forEach(function (st, i) {
    var box = el("div", "stage");
    box.appendChild(el("div", "stage-tag", esc(st.stage || "")));
    var nameRow = el("div", "stage-name");
    nameRow.appendChild(el("span", null, esc(st.name || "")));
    nameRow.appendChild(document.createTextNode(" "));
    nameRow.appendChild(
      el("span", "badge " + statusClass(st.status), esc(st.status || "?"))
    );
    box.appendChild(nameRow);

    if (st.name === "Crawl") {
      box.appendChild(el("div", "metric num", num(st.total_projects)));
      box.appendChild(el("div", "metric-label", "projects crawled (4 sources)"));
      var d = el("div", "detail");
      (st.sources || []).forEach(function (src) {
        var line = src.db + ": ";
        line +=
          src.count != null
            ? '<span class="num">' + num(src.count) + "</span>"
            : "<span>" + esc(src.status) + "</span>";
        d.innerHTML += line + "<br>";
      });
      box.appendChild(d);
    } else if (st.name === "Upload") {
      box.appendChild(el("div", "metric num", num(st.row_count)));
      box.appendChild(el("div", "metric-label", "rows in " + esc(st.table)));
      if (st.error) {
        box.appendChild(el("div", "detail", "error: " + esc(st.error)));
      }
    } else {
      // Enrich / Canonical -- artifact-backed
      var label = st.name === "Canonical" ? "canonical artifact" : "enriched output";
      box.appendChild(
        el(
          "div",
          "metric num",
          esc(st.artifact_size_human || (st.artifact_exists ? "present" : "missing"))
        )
      );
      box.appendChild(el("div", "metric-label", label));
      var det = el("div", "detail");
      if (st.note) det.innerHTML = esc(st.note) + "<br>";
      det.innerHTML +=
        '<code>' + esc((st.artifact || "").split("/").slice(-1)[0]) + "</code>";
      box.appendChild(det);
    }
    flow.appendChild(box);
    if (i < stages.length - 1) flow.appendChild(el("div", "arrow", "&rarr;"));
  });

  // ---- db state card ---------------------------------------------------
  var db = STATE.db_state || {};
  var dbCard = document.getElementById("dbstate");
  if (db.status === "ok") {
    var kv = el("dl", "kv");
    function row(k, v) {
      kv.appendChild(el("dt", null, esc(k)));
      kv.appendChild(el("dd", "num", v));
    }
    row("Total rows", num(db.total_rows));
    row("Publishable", num(db.publishable_rows));
    var nonPub =
      db.total_rows != null && db.publishable_rows != null
        ? db.total_rows - db.publishable_rows
        : null;
    row("Non-publishable", num(nonPub));
    row("Last updated", esc(db.max_updated_at || "&mdash;"));
    row("Audit verdict", esc(db.audit_verdict || "&mdash;"));
    dbCard.appendChild(kv);

    var tiers = el("div", "tiers");
    var dist = db.confidence_tier_distribution || {};
    Object.keys(dist)
      .sort()
      .forEach(function (t) {
        var tc = el("div", "tier");
        tc.appendChild(el("div", "t", esc(t)));
        tc.appendChild(el("div", "v num", num(dist[t])));
        tiers.appendChild(tc);
      });
    if (tiers.childNodes.length) {
      dbCard.appendChild(el("div", "metric-label", "confidence tiers"));
      dbCard.appendChild(tiers);
    }
    dbCard.appendChild(
      el(
        "div",
        "detail",
        "audit report: <code>" + esc(db.audit_report || "") + "</code>"
      )
    );
  } else {
    dbCard.appendChild(
      el(
        "div",
        "empty",
        "Neon unreachable &mdash; " + esc(db.error || "no connection") +
          '<br><span class="detail">audit verdict: ' +
          esc(db.audit_verdict || "") +
          " &middot; report: <code>" +
          esc(db.audit_report || "") +
          "</code></span>"
      )
    );
  }

  // ---- phase timeline --------------------------------------------------
  var tl = document.getElementById("timeline");
  var jobs = (STATE.phases && STATE.phases.jobs) || [];
  if (!jobs.length) {
    tl.appendChild(el("div", "empty", "no job cards found"));
  }
  jobs.forEach(function (j) {
    var r = el("div", "row");
    var when = el("span", "when", esc(j.date_display || j.date_prefix || "undated"));
    if (j.status) {
      when.innerHTML +=
        ' &middot; <span class="badge ' +
        statusClass(j.status) +
        '">' +
        esc(j.status) +
        "</span>";
    } else {
      when.innerHTML += ' &middot; <span class="empty">no status</span>';
    }
    r.appendChild(when);
    r.appendChild(el("div", "what", esc(j.slug)));
    r.appendChild(el("div", "file", esc(j.file)));
    tl.appendChild(r);
  });

  // ---- files table -----------------------------------------------------
  var files = STATE.files || {};
  var fCard = document.getElementById("files");
  var tbl = el("table");
  tbl.innerHTML =
    "<thead><tr><th>Directory</th><th class='r'>Size</th></tr></thead>";
  var tb = el("tbody");
  (files.data_subdirs || []).forEach(function (d) {
    var tr = el("tr");
    tr.appendChild(el("td", "mono", esc(d.path)));
    tr.appendChild(
      el("td", "r num", d.exists ? esc(d.size_human) : "<span class='empty'>missing</span>")
    );
    tb.appendChild(tr);
  });
  if (files.repo_total) {
    var tr2 = el("tr");
    tr2.innerHTML =
      "<td class='mono'><strong>repo total</strong></td>" +
      "<td class='r num'><strong>" +
      esc(files.repo_total.size_human || "&mdash;") +
      "</strong></td>";
    tb.appendChild(tr2);
  }
  tbl.appendChild(tb);
  fCard.appendChild(tbl);

  // ---- git panel -------------------------------------------------------
  var git = STATE.git || {};
  var gCard = document.getElementById("git");
  gCard.appendChild(
    el(
      "div",
      null,
      "branch: <code>" + esc(git.branch || "unknown") + "</code>"
    )
  );
  var ul = el("ul", "git-commits mono");
  ul.style.marginTop = "8px";
  ul.style.paddingLeft = "18px";
  (git.recent_commits || []).forEach(function (c) {
    var li = el("li");
    li.innerHTML =
      '<span class="hash">' + esc(c.hash) + "</span> " + esc(c.subject);
    ul.appendChild(li);
  });
  if (!(git.recent_commits || []).length) {
    ul.appendChild(el("li", "empty", "no commits"));
  }
  gCard.appendChild(ul);
})();
</script>
</body>
</html>
"""


def render_html(state: dict[str, Any]) -> str:
    """Embed state JSON inline in the HTML template (file:// safe)."""
    payload = json.dumps(state, ensure_ascii=False, default=str)
    # Prevent the embedded JSON from prematurely closing the <script> tag.
    payload = payload.replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__STATE_JSON__", payload)


def main() -> int:
    state = collect_state()

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(render_html(state), encoding="utf-8")

    pipe = state["pipeline"]["stages"]
    db = state["db_state"]
    print(f"wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"wrote {OUT_HTML.relative_to(ROOT)}")
    print("--- summary ---")
    for st in pipe:
        if st["name"] == "Crawl":
            print(f"  Crawl    : {st['total_projects']:,} projects ({st['status']})")
        elif st["name"] == "Upload":
            rc = st.get("row_count")
            print(f"  Upload   : {rc if rc is None else format(rc, ',')} rows ({st['status']})")
        else:
            print(f"  {st['name']:<9}: {st.get('artifact_size_human') or st['status']}")
    if db.get("status") == "ok":
        print(
            f"  DB state : {db['total_rows']:,} rows, "
            f"{db['publishable_rows']:,} publishable, "
            f"tiers={db.get('confidence_tier_distribution')}"
        )
    else:
        print(f"  DB state : error -- {db.get('error')}")
    print(f"  Phases   : {state['phases']['count']} job cards")
    print(f"  Git      : branch {state['git'].get('branch')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
