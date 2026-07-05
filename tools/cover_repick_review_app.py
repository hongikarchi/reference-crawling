#!/usr/bin/env python3
"""Local review app for the Fable re-review of repick_chunk1k cover swaps.

Read-mostly, same contract as cover_review_app.py:
- --build composes review_cases.json from confirmed.jsonl + fable_recheck.jsonl
  + building_meta.json (bucket assignment is deterministic Python, never LLM).
- --serve hosts a localhost review UI (default port 8766; 8765 belongs to
  cover_review_app). Decisions land in cover_repick_decisions.json only.
- --check re-derives buckets and validates the cases file.
- --report emits fable_recheck_2026q3.json (agreement matrix, bucket counts).
- It never writes Neon, R2, or canonical artifacts. The decisions file is the
  direct input for the future gated apply tool (apply_cover_repick_neon.py).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_DIR = ROOT / "data/reports/cover_audit/repick_chunk1k"
CONFIRMED_PATH = REPORT_DIR / "confirmed.jsonl"
RECHECK_PATH = REPORT_DIR / "fable_recheck.jsonl"
META_PATH = REPORT_DIR / "building_meta.json"
CASES_PATH = REPORT_DIR / "review_cases.json"
DECISIONS_PATH = REPORT_DIR / "cover_repick_decisions.json"
REPORT_JSON_PATH = REPORT_DIR / "fable_recheck_2026q3.json"

ALLOWED_DECISIONS = {"approve_swap", "reject", "defer"}
BUCKETS = ["auto_approve", "rescued", "demoted", "user_review", "both_bad"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        tmp = Path(f.name)
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ------------------------------------------------------------------ buckets
def assign_bucket(haiku_better: bool, final: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Return (bucket, sub_tag, recommended_decision). First rule wins."""
    verdict, action, agree = final["verdict"], final["action"], final["judges_agree"]
    if verdict == "unjudgeable":
        return "user_review", "download_failed", None
    if agree is False:
        return "user_review", "judges_disagree", None
    if verdict == "both_bad":
        return "both_bad", None, "reject"
    if not haiku_better and action == "swap":
        return "rescued", None, "approve_swap"
    if haiku_better and action == "no_swap":
        return "demoted", "real_demotion", "reject"
    if haiku_better and action == "swap":
        return "auto_approve", None, "approve_swap"
    return "demoted", "confirmed_reject", "reject"


def compose_case(row: dict[str, Any], rc: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    bid = row["canonical_bld_id"]
    final = rc["final"]
    bucket, sub_tag, rec = assign_bucket(bool(row["better"]), final)
    flags = []
    if final["verdict"] == "interior_exception":
        flags.append("interior_exception")
    if final["verdict"] == "both_bad":
        flags.append("needs_full_repick")
    if rc["image_status"]["current"] != "ok":
        flags.append("download_failed_current")
    if rc["image_status"]["proposed"] != "ok":
        flags.append("download_failed_proposed")
    return {
        "case_id": f"repick:{bid}",
        "canonical_bld_id": bid,
        "bucket": bucket,
        "sub_tag": sub_tag,
        "recommended_decision": rec,
        "flags": flags,
        "current_cover_url": row["current_cover"],
        "proposed_cover_url": row["proposed_cover"],
        "current_ext_score": row["current_ext_score"],
        "proposed_ext_score": row["proposed_ext_score"],
        "haiku": {"better": row["better"], "why": row["why"]},
        "fable": {
            "verdict": final["verdict"],
            "confidence": final["confidence"],
            "reason_ko": final["reason_ko"],
            "judges_agree": final["judges_agree"],
            "pass2_verdict": (rc.get("pass2") or {}).get("verdict"),
            "second_pass_trigger": rc.get("second_pass_trigger"),
        },
        "building": meta.get(bid) or {},
    }


def case_sort_key(c: dict[str, Any]) -> tuple:
    # flagged/real cases first inside each bucket; confirmed rejects
    # (zero-action rows) last — they are bulk-confirmable noise.
    return (
        BUCKETS.index(c["bucket"]),
        1 if c["sub_tag"] == "confirmed_reject" else 0,
        0 if "interior_exception" in c["flags"] else 1,
        c["canonical_bld_id"],
    )


def compose_cases() -> dict[str, Any]:
    confirmed = {r["canonical_bld_id"]: r for r in read_jsonl(CONFIRMED_PATH)}
    recheck = {r["canonical_bld_id"]: r for r in read_jsonl(RECHECK_PATH)}
    meta = read_json(META_PATH)["rows"]

    if set(confirmed) != set(recheck):
        missing = sorted(set(confirmed) ^ set(recheck))
        raise SystemExit(f"confirmed/recheck id mismatch ({len(missing)}): {missing[:5]} ...")

    cases = [compose_case(row, recheck[bid], meta) for bid, row in confirmed.items()]
    cases.sort(key=case_sort_key)

    counts = {
        "total": len(cases),
        "by_bucket": dict(Counter(c["bucket"] for c in cases)),
        "interior_exception": sum(1 for c in cases if "interior_exception" in c["flags"]),
        "download_failed": sum(1 for c in cases if c["sub_tag"] == "download_failed"),
    }
    return {
        "version": 1,
        "generated_at": now_iso(),
        "source_files": {
            "confirmed": str(CONFIRMED_PATH),
            "recheck": str(RECHECK_PATH),
            "meta": str(META_PATH),
        },
        "counts": counts,
        "cases": cases,
    }


# ---------------------------------------------------------------- decisions
def blank_decision(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "canonical_bld_id": case["canonical_bld_id"],
        "bucket": case["bucket"],
        "decision": None,
        "old_display_cover_url": case["current_cover_url"],
        "new_display_cover_url": None,
        "recommended_decision": case["recommended_decision"],
        "followed_recommendation": None,
        "notes": "",
        "updated_at": None,
    }


def summarize(cases: list[dict[str, Any]], decisions: dict[str, Any]) -> dict[str, Any]:
    by_bucket: dict[str, dict[str, int]] = {
        b: {"total": 0, "decided": 0, "approve_swap": 0, "reject": 0, "defer": 0}
        for b in BUCKETS
    }
    totals = Counter()
    overrides = 0
    for case in cases:
        d = decisions.get(case["case_id"]) or {}
        bucket = by_bucket[case["bucket"]]
        bucket["total"] += 1
        dec = d.get("decision")
        if dec:
            bucket["decided"] += 1
            bucket[dec] += 1
            totals[dec] += 1
            if case["recommended_decision"] and dec != case["recommended_decision"]:
                overrides += 1
    decided = sum(totals.values())
    return {
        "total": len(cases),
        "decided": decided,
        "undecided": len(cases) - decided,
        "approve_swap": totals["approve_swap"],
        "reject": totals["reject"],
        "defer": totals["defer"],
        "by_bucket": by_bucket,
        "overrides_of_recommendation": overrides,
    }


def ensure_decisions(snapshot: dict[str, Any]) -> dict[str, Any]:
    cases = snapshot["cases"]
    existing = read_json(DECISIONS_PATH) if DECISIONS_PATH.exists() else {}
    old = existing.get("decisions") or {}
    decisions = {}
    for case in cases:
        rec = old.get(case["case_id"]) or blank_decision(case)
        # server-authoritative fields refreshed from the case on every load
        rec["bucket"] = case["bucket"]
        rec["old_display_cover_url"] = case["current_cover_url"]
        rec["recommended_decision"] = case["recommended_decision"]
        decisions[case["case_id"]] = rec
    payload = {
        "version": 1,
        "cases_path": str(CASES_PATH),
        "updated_at": existing.get("updated_at") or now_iso(),
        "db_writes": "none",
        "summary": summarize(cases, decisions),
        "decisions": decisions,
    }
    atomic_write_json(DECISIONS_PATH, payload)
    return payload


def apply_decision(case: dict[str, Any], rec: dict[str, Any],
                   decision: str | None, notes: str) -> None:
    rec["decision"] = decision
    # server derives the URLs from the case — the client never sends them
    rec["new_display_cover_url"] = case["proposed_cover_url"] if decision == "approve_swap" else None
    rec["followed_recommendation"] = (
        None if decision is None or not case["recommended_decision"]
        else decision == case["recommended_decision"]
    )
    rec["notes"] = notes
    rec["updated_at"] = now_iso()


def save_decision(snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    case_id = payload.get("case_id")
    cases = {c["case_id"]: c for c in snapshot["cases"]}
    if case_id not in cases:
        raise ValueError(f"unknown case_id {case_id!r}")
    decision = payload.get("decision")
    if decision is not None and decision not in ALLOWED_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(ALLOWED_DECISIONS)} or null")
    notes = str(payload.get("notes") or "")

    store = ensure_decisions(snapshot)
    rec = store["decisions"][case_id]
    apply_decision(cases[case_id], rec, decision, notes)
    store["summary"] = summarize(snapshot["cases"], store["decisions"])
    store["updated_at"] = now_iso()
    atomic_write_json(DECISIONS_PATH, store)
    return {"decision": rec, "summary": store["summary"]}


def save_bulk(snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    bucket = payload.get("bucket")
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {BUCKETS}")
    store = ensure_decisions(snapshot)
    applied = 0
    for case in snapshot["cases"]:
        if case["bucket"] != bucket or not case["recommended_decision"]:
            continue
        rec = store["decisions"][case["case_id"]]
        if rec["decision"] is not None:
            continue  # bulk never overwrites an explicit human decision
        apply_decision(case, rec, case["recommended_decision"], rec.get("notes") or "")
        applied += 1
    store["summary"] = summarize(snapshot["cases"], store["decisions"])
    store["updated_at"] = now_iso()
    atomic_write_json(DECISIONS_PATH, store)
    return {"applied": applied, "summary": store["summary"]}


# -------------------------------------------------------------------- check
def check_cases(snapshot: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    confirmed = {r["canonical_bld_id"]: r for r in read_jsonl(CONFIRMED_PATH)}
    recheck = {r["canonical_bld_id"]: r for r in read_jsonl(RECHECK_PATH)}
    cases = snapshot.get("cases", [])

    ids = [c["canonical_bld_id"] for c in cases]
    if len(ids) != len(set(ids)):
        failures.append("duplicate case ids")
    if set(ids) != set(confirmed):
        failures.append(f"case ids != confirmed ids ({len(set(ids) ^ set(confirmed))} diff)")

    for c in cases:
        bid = c["canonical_bld_id"]
        if not c["current_cover_url"] or not c["proposed_cover_url"]:
            failures.append(f"{bid}: empty cover url")
        if bid in confirmed and bid in recheck:
            bucket, sub_tag, rec = assign_bucket(
                bool(confirmed[bid]["better"]), recheck[bid]["final"])
            if (c["bucket"], c["sub_tag"], c["recommended_decision"]) != (bucket, sub_tag, rec):
                failures.append(
                    f"{bid}: stored ({c['bucket']},{c['sub_tag']},{c['recommended_decision']}) "
                    f"!= derived ({bucket},{sub_tag},{rec})")

    by_bucket = dict(Counter(c["bucket"] for c in cases))
    if snapshot.get("counts", {}).get("by_bucket") != by_bucket:
        failures.append("counts.by_bucket stale")
    return {
        "status": "PASS" if not failures else "FAIL",
        "failure_count": len(failures),
        "failures": failures[:20],
        "counts": snapshot.get("counts") or {},
    }


# ------------------------------------------------------------------- report
def build_report() -> dict[str, Any]:
    confirmed = {r["canonical_bld_id"]: r for r in read_jsonl(CONFIRMED_PATH)}
    recheck = read_jsonl(RECHECK_PATH)
    snapshot = read_json(CASES_PATH) if CASES_PATH.exists() else None
    decisions = read_json(DECISIONS_PATH) if DECISIONS_PATH.exists() else None

    matrix: dict[str, Counter] = {"haiku_true": Counter(), "haiku_false": Counter()}
    pass1_verdicts, pass1_conf, triggers = Counter(), Counter(), Counter()
    pass2_agree = pass2_disagree = 0
    for r in recheck:
        haiku = "haiku_true" if confirmed[r["canonical_bld_id"]]["better"] else "haiku_false"
        matrix[haiku][r["final"]["verdict"]] += 1
        if r["pass1"]:
            pass1_verdicts[r["pass1"]["verdict"]] += 1
            pass1_conf[r["pass1"]["confidence"]] += 1
        if r["second_pass_trigger"]:
            triggers[r["second_pass_trigger"]] += 1
        if r["final"]["judges_agree"] is True and r["pass2"]:
            pass2_agree += 1
        elif r["final"]["judges_agree"] is False:
            pass2_disagree += 1

    report = {
        "generated_at": now_iso(),
        "inputs": {
            "confirmed_rows": len(confirmed),
            "haiku_better_true": sum(1 for r in confirmed.values() if r["better"]),
        },
        "pass1": {"verdicts": dict(pass1_verdicts), "confidence": dict(pass1_conf)},
        "second_pass": {
            "triggered": sum(triggers.values()),
            "by_trigger": dict(triggers),
            "agree": pass2_agree,
            "disagree": pass2_disagree,
        },
        "agreement_matrix": {k: dict(v) for k, v in matrix.items()},
        "bucket_counts": (snapshot or {}).get("counts", {}).get("by_bucket"),
        "interior_exception_ids": sorted(
            r["canonical_bld_id"] for r in recheck
            if r["final"]["verdict"] == "interior_exception"),
        "rescued_ids": sorted(
            c["canonical_bld_id"] for c in (snapshot or {}).get("cases", [])
            if c["bucket"] == "rescued"),
        "decisions_summary": (decisions or {}).get("summary"),
    }
    atomic_write_json(REPORT_JSON_PATH, report)
    return report


# ------------------------------------------------------------------- server
APP_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cover Re-pick Review</title>
<style>
  :root {
    --bg:#f5f5f2; --panel:#ffffff; --ink:#1f2329; --muted:#6b7280;
    --line:#e2e4e8; --accent:#0b7285; --accent-soft:#e6f4f6;
    --danger:#b42318; --danger-soft:#fdecea; --warn:#a15c07; --warn-soft:#fdf3e0;
    --ok:#1f9d57;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }
  .mono { font-family:"SF Mono","JetBrains Mono",Consolas,monospace; font-size:12px; }

  header { position:sticky; top:0; z-index:10; background:var(--panel);
           border-bottom:1px solid var(--line); padding:10px 16px; }
  .hrow { display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; }
  .tabs { display:flex; gap:6px; flex-wrap:wrap; }
  .tab { border:1px solid var(--line); background:var(--bg); border-radius:6px;
         padding:5px 10px; cursor:pointer; font-size:13px; }
  .tab.active { background:var(--accent); border-color:var(--accent); color:#fff; }
  .tab .cnt { opacity:.75; margin-left:4px; }
  .progress { margin-left:auto; text-align:right; min-width:160px; }
  .pbar { height:4px; background:var(--line); border-radius:2px; overflow:hidden; margin-top:4px; }
  .pbar > div { height:100%; background:var(--accent); }

  .layout { display:grid; grid-template-columns:300px 1fr; gap:14px;
            max-width:1400px; margin:14px auto; padding:0 16px; }
  .sidebar { position:sticky; top:66px; align-self:start; max-height:calc(100vh - 80px);
             display:flex; flex-direction:column; }
  .bucket-desc { background:var(--warn-soft); border:1px solid var(--line);
                 border-radius:8px; padding:8px 10px; font-size:12.5px; margin-bottom:8px; }
  .filters { display:flex; gap:4px; margin-bottom:8px; flex-wrap:wrap; }
  .filters button { border:1px solid var(--line); background:var(--panel);
                    border-radius:5px; padding:3px 8px; font-size:12px; cursor:pointer; }
  .filters button.active { background:var(--ink); color:#fff; border-color:var(--ink); }
  .caselist { overflow-y:auto; border:1px solid var(--line); border-radius:8px;
              background:var(--panel); flex:1; }
  .caseitem { padding:7px 10px; border-bottom:1px solid var(--line); cursor:pointer;
              display:flex; align-items:center; gap:8px; }
  .caseitem:hover { background:var(--bg); }
  .caseitem.active { background:var(--accent-soft); }
  .dot { width:9px; height:9px; border-radius:50%; background:#cbd2d9; flex:none; }
  .dot.approve_swap { background:var(--accent); }
  .dot.reject { background:var(--danger); }
  .dot.defer { background:var(--warn); }
  .caseitem .nm { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bulkbtn { margin-top:8px; padding:8px; border-radius:8px; border:1px solid var(--accent);
             background:var(--accent-soft); color:var(--accent); cursor:pointer; font-size:13px; }

  .main { min-width:0; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:14px 16px; }
  .badges { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin:4px 0 8px; }
  .badge { font-size:11.5px; padding:2px 8px; border-radius:999px; border:1px solid var(--line);
           background:var(--bg); }
  .badge.swap { background:var(--accent-soft); color:var(--accent); border-color:var(--accent); }
  .badge.keep { background:#eef0f2; color:var(--muted); }
  .badge.interior_exception { background:var(--warn-soft); color:var(--warn); border-color:var(--warn); }
  .badge.both_bad { background:var(--danger-soft); color:var(--danger); border-color:var(--danger); }
  .badge.unjudgeable { background:#eef0f2; color:var(--muted); }
  .badge.warnb { background:var(--warn-soft); color:var(--warn); border-color:var(--warn); }
  h2 { margin:0; font-size:18px; }
  .caption { color:var(--muted); font-size:13px; margin-top:2px; }

  .judge { background:var(--bg); border:1px solid var(--line); border-radius:8px;
           padding:8px 12px; margin:10px 0; font-size:13px; }
  .judge .fable { font-size:14px; margin-top:2px; }
  .judge .fable b { color:var(--accent); }

  .comparison { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .imgcard { border:2px solid var(--line); border-radius:10px; overflow:hidden;
             background:var(--panel); }
  .imgcard.recommended { border-color:var(--accent); }
  .imgcard .label { display:flex; justify-content:space-between; padding:6px 10px;
                    font-size:12.5px; border-bottom:1px solid var(--line); }
  .imgcard .tag { color:var(--accent); font-weight:600; }
  .imgbox { aspect-ratio:4/3; background:#e9ecef; display:flex; align-items:center;
            justify-content:center; cursor:zoom-in; }
  .imgbox img { width:100%; height:100%; object-fit:contain; }

  .actions { display:flex; gap:10px; margin-top:12px; align-items:center; flex-wrap:wrap; }
  .abtn { padding:9px 16px; border-radius:8px; border:1px solid var(--line);
          background:var(--panel); cursor:pointer; font-size:14px; }
  .abtn.keepb { border-color:var(--danger); color:var(--danger); }
  .abtn.keepb.selected { background:var(--danger); color:#fff; }
  .abtn.swapb { border-color:var(--accent); color:var(--accent); }
  .abtn.swapb.selected { background:var(--accent); color:#fff; }
  .abtn.deferb { border-color:var(--warn); color:var(--warn); }
  .abtn.deferb.selected { background:var(--warn); color:#fff; }
  .abtn.rec { box-shadow:0 0 0 3px var(--accent-soft); }
  .keys { color:var(--muted); font-size:12px; margin-left:auto; }
  textarea { width:100%; margin-top:10px; border:1px solid var(--line); border-radius:8px;
             padding:8px; font:13px/1.4 inherit; resize:vertical; min-height:36px; }

  .sumgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
             gap:10px; margin:10px 0; }
  .stat { background:var(--bg); border:1px solid var(--line); border-radius:8px;
          padding:10px; text-align:center; }
  .stat .v { font-size:22px; font-weight:700; }
  .stat.teal .v { color:var(--accent); } .stat.red .v { color:var(--danger); }
  .stat.amber .v { color:var(--warn); }
  .sumlist { margin-top:8px; }
  .sumrow { display:flex; gap:10px; align-items:center; padding:6px 0;
            border-bottom:1px solid var(--line); }
  .thumb { width:72px; height:54px; object-fit:cover; border-radius:4px; background:#e9ecef; }
  .arrow { color:var(--accent); font-weight:700; }
  .warnbox { background:var(--warn-soft); border:1px solid var(--warn); color:var(--warn);
             border-radius:8px; padding:8px 12px; margin:8px 0; }
  .cmd { background:#1f2329; color:#e8eaed; border-radius:8px; padding:10px 12px;
         margin-top:10px; overflow-x:auto; }
  .toast { position:fixed; bottom:18px; right:18px; background:var(--ink); color:#fff;
           padding:8px 14px; border-radius:8px; opacity:0; transition:opacity .2s; }
  .toast.show { opacity:.92; }
</style>
</head>
<body>
<header>
  <div class="hrow">
    <h1>Cover Re-pick Review</h1>
    <div class="tabs" id="tabs"></div>
    <div class="progress">
      <div>결정 <b id="pdone">0</b> / <span id="ptotal">0</span></div>
      <div class="pbar"><div id="pfill" style="width:0%"></div></div>
    </div>
  </div>
</header>
<div class="layout">
  <div class="sidebar" id="sidebar"></div>
  <div class="main" id="main"></div>
</div>
<div class="toast" id="toast"></div>
<script>
const BUCKETS = ["auto_approve","rescued","demoted","user_review","both_bad"];
const TAB_LABELS = {auto_approve:"자동승인", rescued:"구제", demoted:"강등",
                    user_review:"판정필요", both_bad:"둘다부적합", summary:"요약"};
const BUCKET_DESC = {
  auto_approve:"Haiku·Fable 모두 교체 찬성. Enter 연타로 통과하며 이상한 것만 걸러내세요.",
  rescued:"Haiku는 기각했지만 Fable이 교체 찬성한 구제 후보. 한 건씩 확인 권장.",
  demoted:"Haiku는 승인했지만 Fable이 반대(강등) — 기본값 '유지'. 인테리어 예외 뱃지 주목. 아래쪽 회색 항목은 둘 다 기각(벌크 처리 가능).",
  user_review:"두 판정 불일치 또는 판정불가 — 직접 보고 결정하세요.",
  both_bad:"둘 다 부적합 판정 — 기본 '유지'(교체 안 함). 추후 후보 재탐색 대상."};
const VERDICT_KO = {swap:"교체 찬성", keep:"현재 유지", interior_exception:"인테리어 예외",
                    both_bad:"둘다 부적합", unjudgeable:"판정불가"};
const DEC_LABEL = {approve_swap:"B로 교체", reject:"A 유지", defer:"보류"};

let CASES = [], DECS = {}, SUMMARY = {};
let tab = "auto_approve", activeId = null, filter = "all";
const undoStack = [];

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function load() {
  const cs = await (await fetch("/api/cases")).json();
  const ds = await (await fetch("/api/decisions")).json();
  CASES = cs.cases; DECS = ds.decisions; SUMMARY = ds.summary;
  const firstUndecided = CASES.find(c => c.bucket === tab && !DECS[c.case_id]?.decision);
  activeId = (firstUndecided || CASES.find(c => c.bucket === tab) || CASES[0])?.case_id;
  render();
}

function bucketCases(b) { return CASES.filter(c => c.bucket === b); }
function visibleCases() {
  let list = bucketCases(tab);
  if (filter === "undecided") list = list.filter(c => !DECS[c.case_id]?.decision);
  if (filter === "decided") list = list.filter(c => DECS[c.case_id]?.decision);
  if (filter === "defer") list = list.filter(c => DECS[c.case_id]?.decision === "defer");
  if (filter === "override") list = list.filter(c => {
    const d = DECS[c.case_id];
    return d?.decision && c.recommended_decision && d.decision !== c.recommended_decision;
  });
  return list;
}

function render() { renderTabs(); tab === "summary" ? renderSummary() : (renderSidebar(), renderMain()); }

function renderTabs() {
  $("tabs").innerHTML = BUCKETS.map(b => {
    const cs = bucketCases(b);
    const done = cs.filter(c => DECS[c.case_id]?.decision).length;
    return `<div class="tab ${tab===b?"active":""}" onclick="switchTab('${b}')">` +
      `${TAB_LABELS[b]}<span class="cnt">${done}/${cs.length}</span></div>`;
  }).join("") +
  `<div class="tab ${tab==="summary"?"active":""}" onclick="switchTab('summary')">${TAB_LABELS.summary}</div>`;
  $("pdone").textContent = SUMMARY.decided ?? 0;
  $("ptotal").textContent = SUMMARY.total ?? CASES.length;
  $("pfill").style.width = (100 * (SUMMARY.decided ?? 0) / Math.max(1, SUMMARY.total ?? 1)) + "%";
}

function switchTab(b) {
  tab = b; filter = "all";
  if (b !== "summary") {
    const list = bucketCases(b);
    activeId = (list.find(c => !DECS[c.case_id]?.decision) || list[0])?.case_id ?? null;
  }
  render();
}

function renderSidebar() {
  const list = visibleCases();
  const recPending = bucketCases(tab).filter(c =>
    c.recommended_decision && !DECS[c.case_id]?.decision).length;
  $("sidebar").innerHTML = `
    <div class="bucket-desc">${BUCKET_DESC[tab] || ""}</div>
    <div class="filters">
      ${["all","undecided","decided","defer","override"].map(f =>
        `<button class="${filter===f?"active":""}" onclick="setFilter('${f}')">` +
        `${{all:"전체",undecided:"미결정",decided:"완료",defer:"보류",override:"추천과 다름"}[f]}</button>`).join("")}
    </div>
    <div class="caselist">
      ${list.map(c => {
        const d = DECS[c.case_id] || {};
        return `<div class="caseitem ${c.case_id===activeId?"active":""}" onclick="openCase('${c.case_id}')">
          <span class="dot ${d.decision || ""}"></span>
          <span class="nm">${esc(c.building.name || c.canonical_bld_id)}</span>
          <span class="badge ${c.fable.verdict}">${VERDICT_KO[c.fable.verdict] || ""}</span>
        </div>`;
      }).join("") || `<div style="padding:12px;color:var(--muted)">항목 없음</div>`}
    </div>
    ${recPending > 0 ? `<button class="bulkbtn" onclick="bulkApply()">남은 추천 전체 적용 (${recPending}건)</button>` : ""}
  `;
}

function renderMain() {
  const c = CASES.find(x => x.case_id === activeId);
  if (!c) { $("main").innerHTML = `<div class="card">케이스를 선택하세요.</div>`; return; }
  const d = DECS[c.case_id] || {};
  const b = c.building || {};
  const cap = [c.canonical_bld_id, b.typology_primary, b.program,
               [b.location_city, b.location_country].filter(Boolean).join(", "),
               b.project_year].filter(Boolean).join(" · ");
  const recSide = c.recommended_decision === "approve_swap" ? "B" :
                  c.recommended_decision === "reject" ? "A" : null;
  const agreeChip = c.fable.judges_agree === true ? `<span class="badge">2차 판정 일치</span>` :
                    c.fable.judges_agree === false ? `<span class="badge warnb">2차 판정 불일치${c.fable.pass2_verdict ? " · 2차: " + (VERDICT_KO[c.fable.pass2_verdict]||c.fable.pass2_verdict) : ""}</span>` : "";
  $("main").innerHTML = `
    <div class="card">
      <h2>${esc(b.name || c.canonical_bld_id)}</h2>
      <div class="caption mono">${esc(cap)}</div>
      <div class="badges">
        <span class="badge">${TAB_LABELS[c.bucket]}${c.sub_tag ? " · " + c.sub_tag : ""}</span>
        <span class="badge ${c.fable.verdict}">Fable: ${VERDICT_KO[c.fable.verdict] || c.fable.verdict}</span>
        ${c.fable.confidence ? `<span class="badge">확신 ${c.fable.confidence}</span>` : ""}
        ${agreeChip}
        ${c.flags.includes("interior_exception") ? `<span class="badge interior_exception">⛪ 인테리어가 본질인 건물</span>` : ""}
      </div>
      <div class="judge">
        <div class="mono">SigLIP ext A ${c.current_ext_score} → B ${c.proposed_ext_score}
          &nbsp;·&nbsp; Haiku: ${c.haiku.better ? "교체 찬성" : "기각"} "${esc(c.haiku.why)}"</div>
        <div class="fable"><b>Fable:</b> ${esc(c.fable.reason_ko || "")}</div>
      </div>
      <div class="comparison">
        <div class="imgcard ${recSide==="A"?"recommended":""}">
          <div class="label"><span>현재 커버 (A) · ext ${c.current_ext_score}</span>
            ${recSide==="A"?`<span class="tag">추천</span>`:""}</div>
          <div class="imgbox" onclick="window.open('${c.current_cover_url}')">
            <img loading="lazy" src="${c.current_cover_url}"></div>
        </div>
        <div class="imgcard ${recSide==="B"?"recommended":""}">
          <div class="label"><span>제안 커버 (B) · ext ${c.proposed_ext_score}</span>
            ${recSide==="B"?`<span class="tag">추천</span>`:""}</div>
          <div class="imgbox" onclick="window.open('${c.proposed_cover_url}')">
            <img loading="lazy" src="${c.proposed_cover_url}"></div>
        </div>
      </div>
      <div class="actions">
        <button class="abtn keepb ${d.decision==="reject"?"selected":""} ${c.recommended_decision==="reject"?"rec":""}"
          onclick="decide('reject')">✕ A 유지</button>
        <button class="abtn swapb ${d.decision==="approve_swap"?"selected":""} ${c.recommended_decision==="approve_swap"?"rec":""}"
          onclick="decide('approve_swap')">✓ B로 교체</button>
        <button class="abtn deferb ${d.decision==="defer"?"selected":""}" onclick="decide('defer')">? 보류</button>
        <span class="keys">←/A 유지 · →/B 교체 · D 보류 · Enter 추천수락 · J/K 이동 · U 되돌리기</span>
      </div>
      <textarea id="notes" placeholder="메모 (선택)">${esc(d.notes || "")}</textarea>
    </div>`;
}

function renderSummary() {
  const s = SUMMARY, bb = s.by_bucket || {};
  const approved = CASES.filter(c => DECS[c.case_id]?.decision === "approve_swap");
  const deferred = CASES.filter(c => DECS[c.case_id]?.decision === "defer");
  const overrides = CASES.filter(c => {
    const d = DECS[c.case_id];
    return d?.decision && c.recommended_decision && d.decision !== c.recommended_decision;
  });
  $("sidebar").innerHTML = `<div class="bucket-desc">검토 완료 후 이 요약이 곧 적용 목록입니다.
    아래 명령으로 dry-run부터 시작하세요 (Neon 쓰기는 별도 승인).</div>`;
  $("main").innerHTML = `
    <div class="card">
      <h2>요약 — 적용 프리뷰</h2>
      <div class="sumgrid">
        <div class="stat"><div class="v">${s.decided}/${s.total}</div><div>결정 완료</div></div>
        <div class="stat teal"><div class="v">${s.approve_swap}</div><div>교체 승인</div></div>
        <div class="stat red"><div class="v">${s.reject}</div><div>유지</div></div>
        <div class="stat amber"><div class="v">${s.defer}</div><div>보류</div></div>
        <div class="stat"><div class="v">${s.overrides_of_recommendation}</div><div>추천과 다른 결정</div></div>
      </div>
      ${s.undecided > 0 ? `<div class="warnbox">아직 ${s.undecided}건이 미결정입니다.</div>` : ""}
      <table class="mono" style="border-collapse:collapse">
        ${BUCKETS.map(b => { const x = bb[b] || {}; return `<tr><td style="padding:2px 14px 2px 0">${TAB_LABELS[b]}</td>
          <td>${x.decided||0}/${x.total||0} 결정 · 교체 ${x.approve_swap||0} · 유지 ${x.reject||0} · 보류 ${x.defer||0}</td></tr>`; }).join("")}
      </table>
      <h3>교체 승인 목록 (${approved.length})</h3>
      <div class="sumlist">${approved.map(c => `
        <div class="sumrow">
          <img class="thumb" loading="lazy" src="${c.current_cover_url}">
          <span class="arrow">→</span>
          <img class="thumb" loading="lazy" src="${c.proposed_cover_url}">
          <span class="mono">${c.canonical_bld_id}</span>
          <span>${esc(c.building.name || "")}</span>
        </div>`).join("") || `<div style="color:var(--muted)">없음</div>`}</div>
      ${deferred.length ? `<h3>보류 (${deferred.length})</h3><div class="mono">${deferred.map(c => c.canonical_bld_id).join(", ")}</div>` : ""}
      ${overrides.length ? `<h3>추천과 다르게 결정 (${overrides.length})</h3><div class="mono">${overrides.map(c => `${c.canonical_bld_id} (추천 ${DEC_LABEL[c.recommended_decision]} → 결정 ${DEC_LABEL[DECS[c.case_id].decision]})`).join("<br>")}</div>` : ""}
      <div class="cmd mono">python tools/apply_cover_repick_neon.py --decisions data/reports/cover_audit/repick_chunk1k/cover_repick_decisions.json&nbsp;&nbsp;# dry-run (도구는 후속 단계에서 제작)</div>
    </div>`;
}

function setFilter(f) { filter = f; renderSidebar(); }
function openCase(id) { activeId = id; renderSidebar(); renderMain(); }

async function postDecision(caseId, decision, notes) {
  const res = await fetch("/api/decision", { method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({case_id: caseId, decision, notes}) });
  if (!res.ok) { toast("저장 실패"); return null; }
  const out = await res.json();
  DECS[caseId] = out.decision; SUMMARY = out.summary;
  return out;
}

async function decide(decision) {
  const c = CASES.find(x => x.case_id === activeId);
  if (!c) return;
  const prev = DECS[c.case_id] || {};
  undoStack.push({case_id: c.case_id, decision: prev.decision ?? null, notes: prev.notes || ""});
  const notes = $("notes") ? $("notes").value : (prev.notes || "");
  const out = await postDecision(c.case_id, decision, notes);
  if (!out) return;
  toast(`${DEC_LABEL[decision] || decision} 저장됨`);
  advance();
}

function advance() {
  const list = visibleCases();
  const next = list.find(c => !DECS[c.case_id]?.decision && c.case_id !== activeId)
    || list[Math.min(list.findIndex(c => c.case_id === activeId) + 1, list.length - 1)];
  if (next) activeId = next.case_id;
  render();
}

function move(delta) {
  const list = visibleCases();
  const i = list.findIndex(c => c.case_id === activeId);
  const next = list[Math.max(0, Math.min(list.length - 1, i + delta))];
  if (next) { activeId = next.case_id; renderSidebar(); renderMain(); }
}

async function undo() {
  const item = undoStack.pop();
  if (!item) { toast("되돌릴 항목 없음"); return; }
  await postDecision(item.case_id, item.decision, item.notes);
  activeId = item.case_id;
  const c = CASES.find(x => x.case_id === item.case_id);
  if (c && c.bucket !== tab) tab = c.bucket;
  toast("되돌렸습니다");
  render();
}

async function bulkApply() {
  const n = bucketCases(tab).filter(c => c.recommended_decision && !DECS[c.case_id]?.decision).length;
  if (!confirm(`${TAB_LABELS[tab]} 버킷의 미결정 ${n}건에 추천 결정을 일괄 적용합니다. 계속할까요?`)) return;
  const res = await fetch("/api/bulk-decisions", { method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify({bucket: tab}) });
  const out = await res.json();
  if (out.applied != null) {
    const ds = await (await fetch("/api/decisions")).json();
    DECS = ds.decisions; SUMMARY = ds.summary;
    toast(`${out.applied}건 적용됨`); render();
  } else toast("실패: " + (out.error || ""));
}

let toastTimer;
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 1500);
}

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (tab === "summary") { if (/^[1-5]$/.test(e.key)) switchTab(BUCKETS[+e.key-1]); return; }
  const c = CASES.find(x => x.case_id === activeId);
  switch (e.key) {
    case "ArrowLeft": case "a": case "A": e.preventDefault(); decide("reject"); break;
    case "ArrowRight": case "b": case "B": e.preventDefault(); decide("approve_swap"); break;
    case "d": case "D": decide("defer"); break;
    case "Enter": case " ":
      e.preventDefault();
      if (c?.recommended_decision) decide(c.recommended_decision); else advance();
      break;
    case "j": case "J": move(1); break;
    case "k": case "K": move(-1); break;
    case "u": case "U": undo(); break;
    case "1": case "2": case "3": case "4": case "5": switchTab(BUCKETS[+e.key-1]); break;
    case "6": switchTab("summary"); break;
  }
});

load();
</script>
</body>
</html>
"""


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "CoverRepickReview/1.0"

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: HTTPStatus = HTTPStatus.OK,
              content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def snapshot(self) -> dict[str, Any]:
        if not CASES_PATH.exists():
            raise FileNotFoundError(f"missing {CASES_PATH}; run --build")
        return read_json(CASES_PATH)

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            if path == "/":
                self._text(APP_HTML, content_type="text/html")
            elif path == "/api/cases":
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
            elif path == "/api/bulk-decisions":
                self._json(save_bulk(self.snapshot(), payload))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def serve(host: str, port: int, open_browser: bool) -> None:
    snapshot = read_json(CASES_PATH)
    ensure_decisions(snapshot)
    httpd = ThreadingHTTPServer((host, port), ReviewHandler)
    url = f"http://{host}:{port}/"
    print(f"Cover re-pick review app: {url}")
    print(f"Cases: {CASES_PATH}")
    print(f"Decisions: {DECISIONS_PATH}")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    httpd.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description="Local review app for Fable cover re-pick recheck")
    ap.add_argument("--build", action="store_true", help="compose review_cases.json")
    ap.add_argument("--check", action="store_true", help="validate review_cases.json")
    ap.add_argument("--serve", action="store_true", help="serve localhost review app")
    ap.add_argument("--report", action="store_true", help="emit fable_recheck_2026q3.json")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-open", action="store_true", help="do not auto-open the browser")
    args = ap.parse_args()

    if args.build:
        snapshot = compose_cases()
        atomic_write_json(CASES_PATH, snapshot)
        decisions = ensure_decisions(snapshot)
        print(json.dumps({"cases": snapshot["counts"],
                          "decisions": decisions["summary"]}, indent=2, ensure_ascii=False))
    if args.check:
        result = check_cases(read_json(CASES_PATH))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result["status"] != "PASS":
            return 1
    if args.report:
        report = build_report()
        print(json.dumps({k: report[k] for k in
                          ("pass1", "second_pass", "agreement_matrix", "bucket_counts")},
                         indent=2, ensure_ascii=False))
        print(f"-> {REPORT_JSON_PATH}")
    if args.serve:
        if not CASES_PATH.exists():
            print(f"missing {CASES_PATH}; run --build", file=sys.stderr)
            return 2
        serve(args.host, args.port, not args.no_open)
    if not (args.build or args.check or args.serve or args.report):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
