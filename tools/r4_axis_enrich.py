#!/usr/bin/env python3
"""R4 full text-tagging runner: scale / structural_system / roof_type /
facade_pattern over all canonical_v2_buildings rows via codex exec.

Smoke-validated prompt/validate are reused from tools/r4_axis_smoke.py; the
worker/resume skeleton mirrors tools/d1_enrich_codex.py (ThreadPoolExecutor,
fsync'd JSONL append, failure ledger, done-cid resume). Results land in the
text sidecar consumed by tools/r4_axis_merge.py — nothing touches Neon.

Run:  python3 tools/r4_axis_enrich.py [--workers 32] [--limit N]
                                      [--publishable-only]
Resume is automatic: completed canonical_bld_ids are skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.claude_cli import CLAUDE_BIN  # noqa: E402
from tools.d1_enrich_codex import FailureLedger, JsonlWriter, extract_json  # noqa: E402
from tools.r4_axis_merge import LLM_AXES, TEXT_SIDECAR  # noqa: E402
from tools.r4_axis_smoke import (  # noqa: E402
    AXES, CLAUDE_FALLBACK_MODEL, era_from_year, run_one, validate,
)

FAILURES_PATH = ROOT / "data/canonical/r4_failures.text.json"
REPORT_PATH = ROOT / "data/reports/r4_smoke/full_text_report.json"

# Batched claude path: one `claude -p` boots a full agent (~18.7k token system
# overhead), so a per-item call wastes ~40x. Batching N items into one call
# amortizes that overhead to ~470/item — the in-session sub-agent idea, done in
# a detached, overnight-safe runner. codex stays per-item (already lean).


def _batch_prompt(rows: list[dict]) -> str:
    items = "\n".join(
        f'{i}. name="{r["name"]}" program={r.get("program")} '
        f'typology={r.get("typology_primary")} style={r.get("style")} '
        f'materials={r.get("material_visual") or []} '
        f'desc="{(r.get("visual_description") or "")[:220]}"'
        for i, r in enumerate(rows)
    )
    return (
        f"Classify these {len(rows)} architecture projects for a recommendation "
        f"database. For EACH, choose exactly one value per axis:\n"
        f"- scale {list(AXES['scale'])} (XS pavilion; S house; M mid-size; "
        f"L large complex; XL urban-scale)\n"
        f"- structural_system {list(AXES['structural_system'])}\n"
        f"- roof_type {list(AXES['roof_type'])}\n"
        f"- facade_pattern {list(AXES['facade_pattern'])}\n"
        f"Infer from text only; 'Unknown' is preferred over guessing. "
        f"Output ONLY a JSON array of {len(rows)} objects in input order, each "
        f'{{"i":<index>,"scale":..,"structural_system":..,"roof_type":..,'
        f'"facade_pattern":..}} — no markdown, no extra keys.\n\n{items}'
    )


def run_batch_claude(rows: list[dict], workers_label: str = "") -> list[dict]:
    """One claude -p call classifies the whole batch; map results back by index."""
    import subprocess
    out_by_idx: dict[int, dict] = {}
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", CLAUDE_FALLBACK_MODEL, _batch_prompt(rows)],
            capture_output=True, text=True, timeout=300, check=False,
            cwd=tempfile.gettempdir(),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[:200]}")
        text = proc.stdout.strip()
        arr = json.loads(text[text.find("["): text.rfind("]") + 1])
        for obj in arr:
            if isinstance(obj, dict) and "i" in obj:
                out_by_idx[int(obj["i"])] = obj
    except Exception as exc:  # noqa: BLE001 — whole batch fails, per-item below
        err = str(exc)[:200]
        return [{"canonical_bld_id": r["canonical_bld_id"], "name": r["name"],
                 "status": "codex_error", "error": err, "engine": "claude",
                 "era": era_from_year(r.get("project_year")), "elapsed_s": 0.0}
                for r in rows]
    results = []
    for i, r in enumerate(rows):
        base = {"canonical_bld_id": r["canonical_bld_id"], "name": r["name"],
                "engine": "claude", "era": era_from_year(r.get("project_year")),
                "elapsed_s": 0.0}
        obj = out_by_idx.get(i)
        if obj is None:
            base.update(status="parse_error", error="missing index in batch reply")
        else:
            tags = {axis: obj.get(axis) for axis in LLM_AXES}
            errs = validate(tags)
            if errs:
                base.update(status="vocab_error", error="; ".join(errs))
            else:
                base.update(status="ok", tags=tags, attempts=1)
        results.append(base)
    return results


def fetch_rows(publishable_only: bool) -> list[dict]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT canonical_bld_id, name, architect_names, location_city,
               location_country, project_year, program, typology_primary,
               style, material_visual, visual_description
        FROM canonical_v2_buildings
        {"WHERE is_publishable" if publishable_only else ""}
        ORDER BY canonical_bld_id
        """
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.rollback()
    conn.close()
    return rows


def read_done_cids(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "ok" and row.get("canonical_bld_id"):
                done.add(row["canonical_bld_id"])
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--limit", type=int, help="cap pending items (smoke/partial)")
    ap.add_argument("--publishable-only", action="store_true")
    ap.add_argument("--engine", choices=("codex", "claude"), default="codex",
                    help="claude = headless `claude -p` Haiku fallback (subscription quota)")
    ap.add_argument("--batch", type=int, default=1,
                    help="claude only: items per call (amortizes ~18.7k agent "
                         "overhead; 40 -> ~470 tok/item). codex ignores this.")
    ap.add_argument("--shard", default=None, metavar="I/N",
                    help="process only items where md5(cid) %% N == I — lets two "
                         "engine lanes run concurrently without duplicate work")
    ap.add_argument("--reverse", action="store_true",
                    help="work the pending list from the end — a second lane "
                         "started from the front won't collide until they meet")
    ap.add_argument("--abort-streak", type=int, default=30,
                    help="abort after N consecutive failures (quota-exhaustion guard)")
    ap.add_argument("--count", action="store_true",
                    help="print pending item count and exit (supervisor probe)")
    args = ap.parse_args()

    rows = fetch_rows(args.publishable_only)
    done = read_done_cids(TEXT_SIDECAR)
    pending = [r for r in rows if r["canonical_bld_id"] not in done]
    if args.shard:
        import hashlib
        i, n = (int(x) for x in args.shard.split("/"))
        pending = [
            r for r in pending
            if int(hashlib.md5(r["canonical_bld_id"].encode()).hexdigest(), 16) % n == i
        ]
    if args.reverse:
        pending.reverse()
    if args.count:
        print(len(pending))
        return 0
    if args.limit:
        pending = pending[: args.limit]
    print(f"rows={len(rows)} done={len(done)} pending={len(pending)} "
          f"workers={args.workers}", file=sys.stderr)

    TEXT_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    writer = JsonlWriter(TEXT_SIDECAR)
    ledger = FailureLedger(FAILURES_PATH)
    status_counts: Counter[str] = Counter()
    t0 = time.time()
    n_done = 0

    batched = args.engine == "claude" and args.batch > 1
    if batched:
        units = [pending[i:i + args.batch] for i in range(0, len(pending), args.batch)]
        def work(unit):  # unit = list of rows -> list of results
            return run_batch_claude(unit)
    else:
        units = [[r] for r in pending]
        def work(unit):
            return [run_one(unit[0], engine=args.engine)]

    fail_streak = 0
    aborted = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, unit): unit for unit in units}
        for future in as_completed(futures):
            unit = futures[future]
            if future.cancelled():
                continue  # cancelled after abort — not an attempt, keep sidecar clean
            try:
                results = future.result()
            except Exception as exc:  # noqa: BLE001
                results = [{"canonical_bld_id": r["canonical_bld_id"],
                            "status": "worker_error", "error": str(exc)[:300]}
                           for r in unit]
            for result in results:
                cid = result["canonical_bld_id"]
                writer.append(result)
                status_counts[result["status"]] += 1
                # ledger is best-effort bookkeeping; never let it kill the run
                try:
                    if result["status"] == "ok":
                        ledger.clear_cid(cid)
                    else:
                        ledger.append({"cid": cid, "error": result.get("error", ""),
                                       "ts": datetime.now(timezone.utc).isoformat()})
                except OSError:
                    pass
                if result["status"] == "ok":
                    fail_streak = 0
                else:
                    fail_streak += 1
                    if fail_streak >= args.abort_streak and not aborted:
                        aborted = True
                        print(f"ABORT: {fail_streak} consecutive failures — engine "
                              f"quota likely exhausted; cancelling remaining work "
                              f"(resume-safe)", file=sys.stderr)
                        for f in futures:
                            f.cancel()
                n_done += 1
            if n_done % 100 < len(results) or n_done == len(pending):
                elapsed = time.time() - t0
                rate = n_done / elapsed * 60 if elapsed else 0
                eta_min = (len(pending) - n_done) / rate if rate else 0
                print(f"  {n_done}/{len(pending)} {dict(status_counts)} "
                      f"{rate:.0f}/min eta {eta_min:.0f}min", file=sys.stderr)

    # full-run distribution report (drift check vs smoke)
    dist: dict[str, Counter] = {axis: Counter() for axis in LLM_AXES}
    n_ok = 0
    with TEXT_SIDECAR.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") != "ok":
                continue
            n_ok += 1
            for axis in LLM_AXES:
                value = (r.get("tags") or {}).get(axis)
                if value:
                    dist[axis][value] += 1
    report = {
        "mode": "r4-full-text",
        "engine": args.engine,
        "aborted_on_fail_streak": aborted,
        "generated": datetime.now(timezone.utc).isoformat(),
        "rows_total": len(rows),
        "processed_this_run": dict(status_counts),
        "ok_total_in_sidecar": n_ok,
        "ok_rate_this_run": round(status_counts["ok"] / max(1, sum(status_counts.values())), 4),
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "distributions": {axis: dict(c.most_common()) for axis, c in dist.items()},
        "unknown_rate": {
            axis: round(dist[axis].get("Unknown", 0) / n_ok, 3) if n_ok else None
            for axis in LLM_AXES
        },
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if aborted:
        return 3  # quota-exhaustion signal — switch engine or wait for reset
    return 0 if status_counts.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
