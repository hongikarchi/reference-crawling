#!/usr/bin/env python3
"""R4 quality-gate click dashboard (G3): human-verify a sample of LLM axis tags.

Standalone, cover_review_app.py pattern — snapshot (read-only Neon JOIN) →
serve (click UI, decisions to a local JSON, atomic) → report (per-axis approve
rate). Never writes Neon. Gate: ≥90% approve per shipping axis; a failing axis
is prompt-iterated or dropped from the deploy (tag build runs without it).

  python3 tools/r4_review_app.py build-snapshot   # N=200: 150 random + 50 rare-tag
  python3 tools/r4_review_app.py serve            # http://127.0.0.1:8766
  python3 tools/r4_review_app.py report

Card UX (per user dashboard rules): decision buttons at the top, cover image +
evidence below, keyboard 1/2/3.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.r4_axis_merge import (  # noqa: E402
    LLM_AXES, MERGED_SIDECAR, R4_AXES, TEXT_SIDECAR, VISION_AXES,
    VISION_SIDECAR, VISION_WINS, load_merged, load_sidecar,
)

REPORT_DIR = ROOT / "data/reports/r4_review"
SNAPSHOT_PATH = REPORT_DIR / "snapshot.json"
DECISIONS_PATH = REPORT_DIR / "decisions.json"
ACCURACY_PATH = REPORT_DIR / "accuracy.json"

N_RANDOM = 150
N_RARE = 50
RARE_TAGS = {
    "scale": {"XL"},
    "roof_type": {"Sawtooth", "Vaulted/Domed", "Green Roof"},
    "structural_system": {"Shell/Membrane", "Earth"},
    "facade_pattern": {"Louvered", "Organic"},
}
DECISIONS = ("all_correct", "save_marked", "unsure")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_snapshot() -> dict:
    merged = load_merged(MERGED_SIDECAR)
    if not merged:
        raise SystemExit(f"merged sidecar missing/empty: {MERGED_SIDECAR}")
    # raw sidecars let us surface the *losing* value where text and vision
    # disagree on a vision axis — so one human review settles the merge policy
    # (text-wins vs vision-wins) instead of needing a second round.
    text_raw = load_sidecar(TEXT_SIDECAR)
    vision_raw = load_sidecar(VISION_SIDECAR)
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT canonical_bld_id, name, project_year, typology_primary,
               display_cover_url, location_country
        FROM canonical_v2_buildings
        WHERE is_publishable
        ORDER BY md5(canonical_bld_id || 'r4review')
        """
    )
    rows = cur.fetchall()
    conn.rollback()
    conn.close()

    def is_rare(entry: dict) -> bool:
        return any(entry.get(axis) in tags for axis, tags in RARE_TAGS.items())

    cases, rare_cases = [], []
    for cid, name, year, typ, cover, country in rows:
        entry = merged.get(cid)
        if not entry:
            continue
        sources = entry.get("sources") or {}
        t, v = text_raw.get(cid) or {}, vision_raw.get(cid) or {}
        dual = {
            axis: {"text": t[axis], "vision": v[axis],
                   "merged_source": sources.get(axis)}
            for axis in VISION_AXES
            if t.get(axis) is not None and v.get(axis) is not None
            and t[axis] != v[axis]
        }
        case = {
            "canonical_bld_id": cid,
            "name": name,
            "project_year": year,
            "typology_primary": typ,
            "display_cover_url": cover,
            "location_country": country,
            "tags": {axis: entry.get(axis) for axis in LLM_AXES},
            "sources": sources,
            "dual": dual,
            "stratum": None,
        }
        if is_rare(entry) and len(rare_cases) < N_RARE:
            case["stratum"] = "rare"
            rare_cases.append(case)
        elif len(cases) < N_RANDOM:
            case["stratum"] = "random"
            cases.append(case)
        if len(cases) >= N_RANDOM and len(rare_cases) >= N_RARE:
            break
    snapshot = {
        "generated_at": _now(),
        "n_random": len(cases),
        "n_rare": len(rare_cases),
        "axes": list(LLM_AXES),
        "era_axis_note": "era is deterministic from project_year — not reviewed here",
        "cases": cases + rare_cases,
    }
    atomic_write_json(SNAPSHOT_PATH, snapshot)
    return snapshot


def load_decisions() -> dict:
    if DECISIONS_PATH.exists():
        return json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    return {"decisions": {}, "updated_at": None}


def save_decision(payload: dict) -> dict:
    cid = str(payload.get("canonical_bld_id") or "")
    decision = str(payload.get("decision") or "")
    if not cid:
        raise ValueError("canonical_bld_id required")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    wrong = payload.get("wrong_axes") or []
    # a wrong marker is either a plain axis (single-source/agreement row) or a
    # source-qualified "axis::text" / "axis::vision" (disagreement row)
    valid_markers = set(LLM_AXES) | {
        f"{a}::{src}" for a in VISION_AXES for src in ("text", "vision")
    }
    if not isinstance(wrong, list) or any(a not in valid_markers for a in wrong):
        raise ValueError(f"wrong_axes must be a subset of {sorted(valid_markers)}")
    decisions = load_decisions()
    if payload.get("undo"):
        decisions["decisions"].pop(cid, None)
    else:
        decisions["decisions"][cid] = {
            "decision": decision,
            "wrong_axes": sorted(wrong),
            "updated_at": _now(),
        }
    decisions["updated_at"] = _now()
    atomic_write_json(DECISIONS_PATH, decisions)
    return decisions


def _axis_wrong(case: dict, axis: str, wrong_axes: list) -> bool:
    """Did the human flag the *deployed* (merged) value for this axis wrong?

    On a disagreement row the axis is judged per source ("axis::text" /
    "axis::vision"); the gate cares about whichever source the merge actually
    shipped. Elsewhere it's a plain "axis" marker.
    """
    dual = (case.get("dual") or {}).get(axis)
    if dual:
        return f"{axis}::{dual['merged_source']}" in wrong_axes
    return axis in wrong_axes


def report() -> dict:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    decisions = load_decisions()["decisions"]
    out: dict = {"generated_at": _now(), "axes": {}, "gate_threshold": 0.9}
    for stratum in ("random", "rare"):
        cases = [c for c in snapshot["cases"] if c["stratum"] == stratum]
        per_axis: dict[str, dict] = {}
        for axis in LLM_AXES:
            n = wrong = unsure = 0
            for case in cases:
                if case["tags"].get(axis) is None:
                    continue  # axis not assigned on this row — nothing to judge
                d = decisions.get(case["canonical_bld_id"])
                if not d:
                    continue
                n += 1
                if d["decision"] == "unsure":
                    unsure += 1
                elif _axis_wrong(case, axis, d["wrong_axes"]):
                    wrong += 1
            judged = n - unsure
            per_axis[axis] = {
                "judged": judged,
                "wrong": wrong,
                "unsure": unsure,
                "approve_rate": round((judged - wrong) / judged, 3) if judged else None,
            }
        out["axes"][stratum] = per_axis

    # policy comparison: on rows where text and vision DISAGREE, which source is
    # more accurate per axis. Settles roof=vision-wins / facade,structural=text-
    # wins — flip the policy + re-merge (free) if the loser scores higher.
    policy: dict[str, dict] = {}
    for axis in VISION_AXES:
        t_judged = t_wrong = v_judged = v_wrong = 0
        for case in snapshot["cases"]:
            dual = (case.get("dual") or {}).get(axis)
            if not dual:
                continue
            d = decisions.get(case["canonical_bld_id"])
            if not d or d["decision"] == "unsure":
                continue
            t_judged += 1
            v_judged += 1
            if f"{axis}::text" in d["wrong_axes"]:
                t_wrong += 1
            if f"{axis}::vision" in d["wrong_axes"]:
                v_wrong += 1
        text_acc = round((t_judged - t_wrong) / t_judged, 3) if t_judged else None
        vision_acc = round((v_judged - v_wrong) / v_judged, 3) if v_judged else None
        winner = None
        if text_acc is not None and vision_acc is not None:
            winner = "text" if text_acc >= vision_acc else "vision"
        policy[axis] = {
            "disagreements_judged": t_judged,
            "current_merge_winner": "vision" if axis in VISION_WINS else "text",
            "text_accuracy": text_acc,
            "vision_accuracy": vision_acc,
            "review_winner": winner,
            "flip_recommended": bool(
                winner and (winner != ("vision" if axis in VISION_WINS else "text"))),
        }
    out["policy_comparison"] = policy

    rnd = out["axes"]["random"]
    out["gate_pass_axes"] = sorted(
        a for a, v in rnd.items()
        if v["approve_rate"] is not None and v["approve_rate"] >= 0.9)
    out["gate_fail_axes"] = sorted(
        a for a, v in rnd.items()
        if v["approve_rate"] is not None and v["approve_rate"] < 0.9)
    out["decided"] = len(decisions)
    out["total_cases"] = len(snapshot["cases"])
    atomic_write_json(ACCURACY_PATH, out)
    return out


APP_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>R4 Quality Review</title>
<style>
:root { --line:#d8d9d2; --muted:#65707c; --accent:#0b7285; --accent2:#e6f6f8; --danger:#b42318; }
* { box-sizing:border-box; } body { margin:0; background:#f4f5f2; color:#20242a; font:15px/1.5 system-ui,sans-serif; }
.wrap { max-width:880px; margin:0 auto; padding:16px; }
.card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:18px; margin-bottom:14px; }
h2 { margin:4px 0 8px; } .muted { color:var(--muted); }
.badge { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:3px 10px; background:#f9faf7; color:var(--muted); font-size:13px; margin-right:6px; }
.actions { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:12px 0; }
button { border:1px solid var(--line); background:#fff; border-radius:8px; padding:12px; cursor:pointer; font:inherit; font-weight:700; }
button.primary { border-color:var(--accent); background:var(--accent2); color:#07525f; }
button small { display:block; font-weight:500; color:var(--muted); }
.tags { display:grid; gap:8px; margin:10px 0; }
.tag { display:flex; align-items:center; gap:10px; border:1px solid var(--line); border-radius:8px; padding:9px 12px; }
.tag.wrong { border-color:var(--danger); background:#fdf1f0; }
.tag.dual { display:block; }
.tag b { min-width:170px; } .tag .val { flex:1; }
.tag button { padding:6px 10px; font-weight:600; }
.choice { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }
.choice .opt { flex:1; min-width:120px; padding:9px 10px; font-weight:600; }
.choice .opt.sel { border-color:var(--accent); background:var(--accent2); color:#07525f; }
.choice .opt.bad.sel { border-color:var(--danger); background:#fdf1f0; color:var(--danger); }
img.cover { width:100%; max-height:480px; object-fit:contain; background:#eceee8; border-radius:8px; }
.toast { position:fixed; right:18px; bottom:18px; background:#17212b; color:#fff; padding:10px 14px; border-radius:8px; opacity:0; transition:.2s; }
.toast.show { opacity:1; }
.bar { display:flex; justify-content:space-between; align-items:center; }
</style></head><body><div class="wrap">
<div class="card bar"><div><b>R4 태그 품질 검수</b> <span class="muted">축별 wrong 토글 → 저장. 단축키 1/2/3</span></div><div id="meta" class="muted"></div></div>
<div id="view"></div></div>
<div id="toast" class="toast"></div>
<script>
let snapshot=null, decisions=null, wrongAxes=new Set(), choices={};
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const AXIS_KO={scale:'규모 (scale)',structural_system:'구조 (structural_system)',roof_type:'지붕 (roof_type)',facade_pattern:'입면 (facade_pattern)'};
function toast(t){const el=$('#toast');el.textContent=t;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1600);}
function pending(){return snapshot.cases.filter(c=>!decisions.decisions[c.canonical_bld_id]);}
function current(){const p=pending();return p[0]||null;}
function render(){
  const done=Object.keys(decisions.decisions).length;
  $('#meta').textContent=`${done}/${snapshot.cases.length} 완료`;
  const c=current();
  if(!c){$('#view').innerHTML='<div class="card"><h2>검수 완료</h2><div class="muted">python3 tools/r4_review_app.py report 로 결과 확인</div></div>';return;}
  wrongAxes=new Set(); choices={};
  const dual=c.dual||{};
  const dualAxes=Object.keys(dual);
  const rows=Object.entries(c.tags).filter(([a,v])=>v!==null&&v!==undefined).map(([a,v])=>{
    if(dual[a]){
      const d=dual[a], star=s=>d.merged_source===s?' ★':'';
      return `<div class="tag dual" id="tag-${a}"><b>${esc(AXIS_KO[a]||a)} <span class="muted">불일치 — 맞는 쪽 선택</span></b>
        <div class="choice" data-caxis="${a}">
          <button class="opt" data-pick="text">text: ${esc(d.text)}${star('text')}</button>
          <button class="opt" data-pick="vision">vision: ${esc(d.vision)}${star('vision')}</button>
          <button class="opt bad" data-pick="both">둘다 틀림</button>
        </div></div>`;
    }
    return `<div class="tag" id="tag-${a}"><b>${esc(AXIS_KO[a]||a)}</b><span class="val">${esc(v)} <span class="muted">(${esc(c.sources[a]||'')})</span></span><button data-marker="${a}">잘못됨</button></div>`;
  }).join('');
  const hasDual=dualAxes.length>0;
  $('#view').innerHTML=`<div class="card">
    <span class="badge">${esc(c.stratum)}</span><span class="badge">${esc(c.typology_primary||'')}</span><span class="badge">${esc(c.project_year||'')}</span><span class="badge">${esc(c.location_country||'')}</span>
    <h2>${esc(c.name)}</h2>
    <div class="actions">
      ${hasDual
        ? '<button id="ok" disabled title="불일치 축이 있어 \'전부 맞음\' 불가 — 맞는 쪽 선택 후 저장">1 전부 맞음</button>'
        : '<button class="primary" id="ok">1 전부 맞음</button>'}
      <button id="save">2 저장<small>${hasDual?'불일치 선택 후':'잘못됨 토글 후'}</small></button>
      <button id="unsure">3 보류</button>
    </div>
    <div class="tags">${rows}</div>
    <img class="cover" src="${esc(c.display_cover_url)}" loading="lazy">
  </div>`;
  document.querySelectorAll('button[data-marker]').forEach(b=>b.onclick=()=>{const a=b.dataset.marker;const el=$('#tag-'+CSS.escape(a));if(wrongAxes.has(a)){wrongAxes.delete(a);el.classList.remove('wrong');b.textContent='잘못됨';}else{wrongAxes.add(a);el.classList.add('wrong');b.textContent='✓ 잘못됨';}});
  document.querySelectorAll('.choice').forEach(div=>{const a=div.dataset.caxis;div.querySelectorAll('.opt').forEach(b=>b.onclick=()=>{choices[a]=b.dataset.pick;div.querySelectorAll('.opt').forEach(x=>x.classList.remove('sel'));b.classList.add('sel');});});
  if(!hasDual)$('#ok').onclick=()=>save(c,'all_correct',[]);
  $('#save').onclick=()=>{
    const missing=dualAxes.filter(a=>!choices[a]);
    if(missing.length){toast('불일치 축 선택 필요: '+missing.join(', '));return;}
    save(c,'save_marked',buildWrong());
  };
  $('#unsure').onclick=()=>save(c,'unsure',[]);
}
function buildWrong(){
  const w=[...wrongAxes];
  for(const [a,pick] of Object.entries(choices)){
    if(pick==='text')w.push(a+'::vision');        // text correct -> vision wrong
    else if(pick==='vision')w.push(a+'::text');    // vision correct -> text wrong
    else if(pick==='both'){w.push(a+'::text');w.push(a+'::vision');}
  }
  return w;
}
async function save(c,decision,wrong){
  const r=await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({canonical_bld_id:c.canonical_bld_id,decision,wrong_axes:wrong})});
  if(!r.ok){toast(await r.text());return;}
  decisions=await r.json();toast('저장됨');render();
}
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT')return;if(e.key==='1')$('#ok')&&!$('#ok').disabled&&$('#ok').click();if(e.key==='2')$('#save')?.click();if(e.key==='3')$('#unsure')?.click();});
(async()=>{snapshot=await (await fetch('/api/snapshot')).json();decisions=await (await fetch('/api/decisions')).json();render();})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = APP_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/snapshot":
            self._json(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))
        elif self.path == "/api/decisions":
            self._json(load_decisions())
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):  # noqa: N802
        if self.path != "/api/decision":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            self._json(save_decision(payload))
        except Exception as exc:  # noqa: BLE001
            body = str(exc).encode("utf-8")
            self.send_response(HTTPStatus.BAD_REQUEST.value)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("r4_review_app: " + (fmt % args) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-snapshot")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8766)
    sub.add_parser("report")
    args = ap.parse_args()

    if args.cmd == "build-snapshot":
        snap = build_snapshot()
        print(json.dumps({"cases": len(snap["cases"]), "random": snap["n_random"],
                          "rare": snap["n_rare"], "path": str(SNAPSHOT_PATH.relative_to(ROOT))},
                         indent=2))
        return 0
    if args.cmd == "serve":
        if not SNAPSHOT_PATH.exists():
            build_snapshot()
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"R4 quality review: http://{args.host}:{args.port}")
        server.serve_forever()
        return 0
    if args.cmd == "report":
        print(json.dumps(report(), indent=2, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
