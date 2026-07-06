#!/usr/bin/env python3
"""Local review app for covers_by_type slot updates (interior fill + exterior sync).

Same contract as cover_repick_review_app.py (port 8767; 8766 belongs to the
repick app): --build composes cases deterministically, --serve captures local
decisions only, --check validates. Never writes Neon/R2/artifacts.

Sections:
  interior — pick the building's interior-slot cover from the SigLIP shortlist
             (Fable's pick prefilled; '없음' = leave slot empty)
  exterior — sync the 145 approved display covers into covers_by_type.exterior
             (140 auto-prefilled; 5 low-exterior-score cases need a human eye)

Decisions -> cbt_decisions.json, consumed by the future gated apply step.
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

from tools.cover_repick_review_app import atomic_write_json, read_json, read_jsonl  # noqa: E402

REPORT_DIR = ROOT / "data/reports/cover_audit/repick_chunk1k"
SHORTLIST = REPORT_DIR / "cbt_interior_shortlist.jsonl"
FABLE_PICKS = REPORT_DIR / "cbt_fable_picks.jsonl"
META_PATH = REPORT_DIR / "building_meta.json"
REPICK_CASES = REPORT_DIR / "review_cases.json"
REPICK_DECISIONS = REPORT_DIR / "cover_repick_decisions.json"
CASES_PATH = REPORT_DIR / "cbt_review_cases.json"
DECISIONS_PATH = REPORT_DIR / "cbt_decisions.json"

SECTIONS = ["interior", "exterior"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------- build
def compose_cases() -> dict[str, Any]:
    meta = read_json(META_PATH)["rows"]
    shortlist = {r["canonical_bld_id"]: r for r in read_jsonl(SHORTLIST)}
    picks = {r["canonical_bld_id"]: r for r in read_jsonl(FABLE_PICKS)}
    repick_cases = {c["canonical_bld_id"]: c for c in read_json(REPICK_CASES)["cases"]}
    repick_dec = read_json(REPICK_DECISIONS)["decisions"]

    cases: list[dict[str, Any]] = []

    # interior fill — one case per building that has a scored pool
    for bid, sl in sorted(shortlist.items()):
        pk = picks.get(bid) or {}
        cands = sl["shortlist"]
        pick_url = pk.get("choice_url")
        conf = pk.get("confidence")
        if not cands:
            bucket, prefill = "none_found", None
        elif pick_url and conf == "high":
            bucket, prefill = "auto_fill", pick_url
        else:
            bucket, prefill = "user_pick", pick_url  # may be None (fable said none)
        cases.append({
            "case_id": f"int:{bid}", "section": "interior",
            "canonical_bld_id": bid, "bucket": bucket,
            "candidates": [{"url": c["url"], "interior_prob": c["interior_prob"]}
                           for c in cands],
            "fable": {"pick_url": pick_url, "confidence": conf,
                      "reason_ko": pk.get("reason_ko")},
            "prefill_url": prefill,
            "building": meta.get(bid) or {},
        })

    # exterior sync — one case per approved display swap
    for rec in sorted(repick_dec.values(), key=lambda r: r["canonical_bld_id"]):
        if rec["decision"] != "approve_swap":
            continue
        bid = rec["canonical_bld_id"]
        c = repick_cases[bid]
        auto = c["fable"]["verdict"] == "swap" or (c["proposed_ext_score"] or 0) >= 0.5
        cases.append({
            "case_id": f"ext:{bid}", "section": "exterior",
            "canonical_bld_id": bid,
            "bucket": "auto_sync" if auto else "user_check",
            "current_slot_url": c["current_cover_url"],  # old slot value == old display
            "new_url": rec["new_display_cover_url"],
            "proposed_ext_score": c["proposed_ext_score"],
            "fable": {"verdict": c["fable"]["verdict"],
                      "reason_ko": c["fable"]["reason_ko"]},
            "prefill": auto,
            "building": meta.get(bid) or {},
        })

    order = {"user_pick": 0, "user_check": 0, "auto_fill": 1, "auto_sync": 1, "none_found": 2}
    cases.sort(key=lambda c: (SECTIONS.index(c["section"]),
                              order[c["bucket"]], c["canonical_bld_id"]))
    counts = {
        "total": len(cases),
        "by_bucket": dict(Counter(c["bucket"] for c in cases)),
        "interior_with_candidates": sum(1 for c in cases
                                        if c["section"] == "interior" and c["candidates"]),
    }
    return {"version": 1, "generated_at": now_iso(), "counts": counts, "cases": cases}


# ---------------------------------------------------------------- decisions
def blank_decision(case: dict[str, Any]) -> dict[str, Any]:
    if case["section"] == "interior":
        value = case["prefill_url"]  # url | None(=슬롯 비움)
        # none_found has nothing to choose from — auto-decide "leave empty"
        decided = case["bucket"] in ("auto_fill", "none_found")
    else:
        value = case["new_url"] if case["prefill"] else None
        decided = case["bucket"] == "auto_sync"
    return {
        "case_id": case["case_id"], "canonical_bld_id": case["canonical_bld_id"],
        "section": case["section"], "bucket": case["bucket"],
        "value": value if decided else None,     # chosen url (or None)
        "decided": decided,                       # prefills count as decided; user can change
        "prefilled": decided,
        "updated_at": now_iso() if decided else None,
    }


def summarize(cases: list[dict[str, Any]], decisions: dict[str, Any]) -> dict[str, Any]:
    s = {"total": len(cases), "decided": 0,
         "interior_fill": 0, "interior_skip": 0,
         "exterior_sync": 0, "exterior_skip": 0,
         "by_bucket": {}}
    for case in cases:
        d = decisions.get(case["case_id"]) or {}
        b = s["by_bucket"].setdefault(case["bucket"], {"total": 0, "decided": 0})
        b["total"] += 1
        if d.get("decided"):
            s["decided"] += 1
            b["decided"] += 1
            if case["section"] == "interior":
                s["interior_fill" if d.get("value") else "interior_skip"] += 1
            else:
                s["exterior_sync" if d.get("value") else "exterior_skip"] += 1
    return s


def ensure_decisions(snapshot: dict[str, Any]) -> dict[str, Any]:
    existing = read_json(DECISIONS_PATH) if DECISIONS_PATH.exists() else {}
    old = existing.get("decisions") or {}
    decisions = {c["case_id"]: old.get(c["case_id"]) or blank_decision(c)
                 for c in snapshot["cases"]}
    payload = {"version": 1, "db_writes": "none",
               "updated_at": existing.get("updated_at") or now_iso(),
               "summary": summarize(snapshot["cases"], decisions),
               "decisions": decisions}
    atomic_write_json(DECISIONS_PATH, payload)
    return payload


def save_decision(snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    cases = {c["case_id"]: c for c in snapshot["cases"]}
    case = cases.get(payload.get("case_id"))
    if case is None:
        raise ValueError(f"unknown case_id {payload.get('case_id')!r}")
    value = payload.get("value")  # url string | None
    if value is not None:
        allowed = ({c["url"] for c in case["candidates"]} if case["section"] == "interior"
                   else {case["new_url"]})
        if value not in allowed:
            raise ValueError("value must be one of the case's candidate urls or null")
    store = ensure_decisions(snapshot)
    rec = store["decisions"][case["case_id"]]
    rec.update(value=value, decided=bool(payload.get("decided", True)),
               prefilled=False, updated_at=now_iso())
    store["summary"] = summarize(snapshot["cases"], store["decisions"])
    store["updated_at"] = now_iso()
    atomic_write_json(DECISIONS_PATH, store)
    return {"decision": rec, "summary": store["summary"]}


# -------------------------------------------------------------------- check
def check_cases(snapshot: dict[str, Any]) -> dict[str, Any]:
    failures = []
    derived = compose_cases()
    if len(derived["cases"]) != len(snapshot.get("cases", [])):
        failures.append("case count differs from re-derivation")
    for a, b in zip(derived["cases"], snapshot.get("cases", [])):
        if (a["case_id"], a["bucket"], a.get("prefill_url"), a.get("prefill")) != \
           (b["case_id"], b["bucket"], b.get("prefill_url"), b.get("prefill")):
            failures.append(f"{b['case_id']}: stored != derived")
            if len(failures) > 5:
                break
    return {"status": "PASS" if not failures else "FAIL",
            "failures": failures[:10], "counts": snapshot.get("counts")}


# ------------------------------------------------------------------- server
APP_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>covers_by_type Review</title>
<style>
  :root { --bg:#f5f5f2; --panel:#fff; --ink:#1f2329; --muted:#6b7280; --line:#e2e4e8;
          --accent:#0b7285; --accent-soft:#e6f4f6; --danger:#b42318; --warn:#a15c07;
          --warn-soft:#fdf3e0; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif; }
  .mono { font-family:Consolas,monospace; font-size:12px; }
  header { position:sticky; top:0; z-index:10; background:var(--panel);
           border-bottom:1px solid var(--line); padding:10px 16px;
           display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; }
  .tab { border:1px solid var(--line); background:var(--bg); border-radius:6px;
         padding:5px 10px; cursor:pointer; }
  .tab.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .prog { margin-left:auto; }
  .wrap { max-width:1250px; margin:14px auto; padding:0 16px; }
  .desc { background:var(--warn-soft); border:1px solid var(--line); border-radius:8px;
          padding:8px 12px; margin-bottom:10px; font-size:13px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:12px 14px; margin-bottom:14px; }
  .card h3 { margin:0 0 2px; font-size:15px; }
  .cap { color:var(--muted); font-size:12.5px; margin-bottom:6px; }
  .badge { font-size:11px; padding:1px 8px; border-radius:999px; border:1px solid var(--line);
           background:var(--bg); margin-left:6px; }
  .badge.warnb { background:var(--warn-soft); color:var(--warn); border-color:var(--warn); }
  .reason { font-size:13px; margin:4px 0 8px; }
  .reason b { color:var(--accent); }
  .grid { display:flex; gap:10px; flex-wrap:wrap; }
  .cand { width:230px; border:3px solid var(--line); border-radius:8px; overflow:hidden;
          cursor:pointer; background:var(--panel); }
  .cand.sel { border-color:var(--accent); }
  .cand .box { aspect-ratio:4/3; background:#e9ecef; }
  .cand img { width:100%; height:100%; object-fit:contain; }
  .cand .lb { padding:4px 8px; font-size:12px; display:flex; justify-content:space-between; }
  .noneb { align-self:stretch; border:2px dashed var(--line); border-radius:8px;
           padding:0 18px; cursor:pointer; background:var(--bg); font-size:13px; }
  .noneb.sel { border-color:var(--danger); color:var(--danger); font-weight:600; }
  .state { font-size:12px; color:var(--muted); margin-top:6px; }
  .state.done { color:var(--accent); }
  .bulk { margin:8px 0 16px; padding:8px 14px; border-radius:8px; cursor:pointer;
          border:1px solid var(--accent); background:var(--accent-soft); color:var(--accent); }
  .sumgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; }
  .stat { background:var(--bg); border:1px solid var(--line); border-radius:8px;
          padding:10px; text-align:center; }
  .stat .v { font-size:20px; font-weight:700; }
  .toast { position:fixed; bottom:18px; right:18px; background:var(--ink); color:#fff;
           padding:8px 14px; border-radius:8px; opacity:0; transition:opacity .2s; }
  .toast.show { opacity:.92; }
</style>
</head>
<body>
<header>
  <h1>covers_by_type Review</h1>
  <div class="tab" id="t_int" onclick="setTab('interior')">인테리어 채우기</div>
  <div class="tab" id="t_ext" onclick="setTab('exterior')">외관 동기화</div>
  <div class="tab" id="t_sum" onclick="setTab('summary')">요약</div>
  <div class="prog">결정 <b id="pd">0</b>/<span id="pt">0</span></div>
</header>
<div class="wrap" id="wrap"></div>
<div class="toast" id="toast"></div>
<script>
const DESC = {
  interior: "각 건물의 인테리어 슬롯 후보입니다. Fable 추천이 파란 테두리로 선택돼 있습니다 — 다른 후보를 클릭해 바꾸거나, '슬롯 비움'을 누르세요. 위쪽 '판단 필요' 케이스부터 확인하세요.",
  exterior: "기본 커버로 승인된 145건을 exterior 슬롯에도 반영합니다. 140건은 자동, 아래 5건은 새 커버가 외관 사진이 아닐 수 있어 직접 판단이 필요합니다 (반영 안 하면 기존 슬롯 유지)."};
let CASES=[], DECS={}, SUM={}, tab='interior';
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function load(){
  CASES=(await (await fetch('/api/cases')).json()).cases;
  const d=await (await fetch('/api/decisions')).json();
  DECS=d.decisions; SUM=d.summary; render();
}
function setTab(t){ tab=t; render(); }
function bucketRank(c){ return (c.bucket==='user_pick'||c.bucket==='user_check')?0:(c.bucket==='none_found'?2:1); }

function render(){
  $('t_int').className='tab'+(tab==='interior'?' active':'');
  $('t_ext').className='tab'+(tab==='exterior'?' active':'');
  $('t_sum').className='tab'+(tab==='summary'?' active':'');
  $('pd').textContent=SUM.decided??0; $('pt').textContent=SUM.total??0;
  if(tab==='summary') return renderSum();
  const list=CASES.filter(c=>c.section===tab).sort((a,b)=>bucketRank(a)-bucketRank(b));
  const pending=list.filter(c=>!(DECS[c.case_id]||{}).decided).length;
  let h=`<div class="desc">${DESC[tab]}</div>`;
  if(pending>0 && tab==='interior')
    h+=`<button class="bulk" onclick="bulkOk()">미확정 ${pending}건을 현재 표시된 선택대로 확정</button>`;
  for(const c of list) h+= tab==='interior'?intCard(c):extCard(c);
  $('wrap').innerHTML=h;
}

function meta(c){const b=c.building||{};return [c.canonical_bld_id,b.typology_primary,b.program,[b.location_city,b.location_country].filter(Boolean).join(', ')].filter(Boolean).join(' · ');}

function intCard(c){
  const d=DECS[c.case_id]||{}; const cur = d.value===undefined?c.prefill_url:d.value;
  const needs=c.bucket==='user_pick';
  let g='';
  for(const k of c.candidates)
    g+=`<div class="cand ${cur===k.url?'sel':''}" onclick="pick('${c.case_id}','${k.url}')">
      <div class="box"><img loading="lazy" src="${k.url}"></div>
      <div class="lb"><span>interior ${(k.interior_prob*100).toFixed(0)}%</span>
      ${c.fable.pick_url===k.url?'<span style="color:var(--accent)">Fable 추천</span>':''}</div></div>`;
  g+=`<button class="noneb ${cur==null?'sel':''}" onclick="pick('${c.case_id}',null)">슬롯 비움<br>(적절한 인테리어 없음)</button>`;
  return `<div class="card"><h3>${esc((c.building||{}).name||c.canonical_bld_id)}
      ${needs?'<span class="badge warnb">판단 필요</span>':'<span class="badge">자동(변경 가능)</span>'}</h3>
    <div class="cap mono">${esc(meta(c))}</div>
    ${c.fable.reason_ko?`<div class="reason"><b>Fable:</b> ${esc(c.fable.reason_ko)} (확신 ${c.fable.confidence??'-'})</div>`:''}
    <div class="grid">${g}</div>
    <div class="state ${(d.decided)?'done':''}">${d.decided?'✓ 확정됨':'미확정 — 후보를 클릭하면 확정됩니다'}</div></div>`;
}

function extCard(c){
  const d=DECS[c.case_id]||{}; const on = d.value!=null;
  return `<div class="card"><h3>${esc((c.building||{}).name||c.canonical_bld_id)}
      ${c.bucket==='user_check'?'<span class="badge warnb">판단 필요 — 외관성 낮음 (ext '+(c.proposed_ext_score*100).toFixed(0)+'%)</span>':'<span class="badge">자동</span>'}</h3>
    <div class="cap mono">${esc(meta(c))}</div>
    <div class="reason"><b>Fable:</b> ${esc(c.fable.reason_ko||'')} (판정 ${c.fable.verdict})</div>
    <div class="grid">
      <div class="cand" style="cursor:default"><div class="box"><img loading="lazy" src="${c.current_slot_url}"></div><div class="lb"><span>기존 exterior 슬롯</span></div></div>
      <div class="cand ${on?'sel':''}" onclick="pick('${c.case_id}','${c.new_url}')"><div class="box"><img loading="lazy" src="${c.new_url}"></div><div class="lb"><span>새 기본 커버 → 슬롯 반영</span></div></div>
      <button class="noneb ${(!on&&d.decided)?'sel':''}" onclick="pick('${c.case_id}',null)">반영 안 함<br>(기존 슬롯 유지)</button>
    </div>
    <div class="state ${(d.decided)?'done':''}">${d.decided?'✓ 확정됨':'미확정'}</div></div>`;
}

function renderSum(){
  const s=SUM;
  $('wrap').innerHTML=`<div class="card"><h3>요약 — 적용 프리뷰</h3>
    <div class="sumgrid">
      <div class="stat"><div class="v">${s.decided}/${s.total}</div><div>확정</div></div>
      <div class="stat"><div class="v">${s.interior_fill}</div><div>인테리어 채움</div></div>
      <div class="stat"><div class="v">${s.interior_skip}</div><div>인테리어 비움</div></div>
      <div class="stat"><div class="v">${s.exterior_sync}</div><div>외관 슬롯 반영</div></div>
      <div class="stat"><div class="v">${s.exterior_skip}</div><div>외관 유지</div></div>
    </div>
    <p class="mono">확정 후 적용(별도 승인): python tools/apply_covers_by_type_neon.py --decisions .../cbt_decisions.json</p></div>`;
}

async function pick(id,val){
  const r=await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({case_id:id,value:val,decided:true})});
  if(!r.ok){toast('저장 실패');return;}
  const o=await r.json(); DECS[id]=o.decision; SUM=o.summary; render(); toast('저장됨');
}
async function bulkOk(){
  const list=CASES.filter(c=>c.section===tab&&!(DECS[c.case_id]||{}).decided);
  if(!confirm(list.length+'건을 현재 표시된 선택대로 확정합니다.')) return;
  for(const c of list){
    const cur=(DECS[c.case_id]&&DECS[c.case_id].value!==undefined)?DECS[c.case_id].value:c.prefill_url;
    await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({case_id:c.case_id,value:cur??null,decided:true})});
  }
  await load(); toast('일괄 확정 완료');
}
let tm; function toast(m){const t=$('toast');t.textContent=m;t.classList.add('show');
  clearTimeout(tm);tm=setTimeout(()=>t.classList.remove('show'),1400);}
load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CbtReview/1.0"

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            if path == "/":
                body = APP_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/cases":
                self._json(read_json(CASES_PATH))
            elif path == "/api/decisions":
                self._json(ensure_decisions(read_json(CASES_PATH)))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if urlparse(self.path).path != "/api/decision":
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            self._json(save_decision(read_json(CASES_PATH), payload))
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="covers_by_type review app")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if args.build:
        snapshot = compose_cases()
        atomic_write_json(CASES_PATH, snapshot)
        decisions = ensure_decisions(snapshot)
        print(json.dumps({"cases": snapshot["counts"],
                          "summary": decisions["summary"]}, indent=2, ensure_ascii=False))
    if args.check:
        result = check_cases(read_json(CASES_PATH))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result["status"] != "PASS":
            return 1
    if args.serve:
        if not CASES_PATH.exists():
            print(f"missing {CASES_PATH}; run --build", file=sys.stderr)
            return 2
        snapshot = read_json(CASES_PATH)
        ensure_decisions(snapshot)
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
        url = f"http://{args.host}:{args.port}/"
        print(f"covers_by_type review app: {url}")
        if not args.no_open:
            threading.Timer(0.8, webbrowser.open, args=(url,)).start()
        httpd.serve_forever()
    if not (args.build or args.check or args.serve):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
