#!/usr/bin/env python3
"""Apply USER-APPROVED cover re-pick swaps to Neon. USER-GATED.

Default = dry-run (transaction + ROLLBACK, prints counts + in-txn QC). A live
write requires BOTH --apply and --confirm-db-write.

Input = cover_repick_decisions.json written by tools/cover_repick_review_app.py:
only rows with decision == "approve_swap" are applied, and the review app is the
sole author of old/new URLs (server-derived from confirmed.jsonl), so this tool
never recomputes them. Per row: display_cover_url := new, ONLY where the stored
value still equals old (guards against concurrent/duplicate application) and
differs from new (idempotent re-runs are no-ops).

On a live commit a reversal sidecar applied_cover_swaps_<date>.jsonl is written
next to the decisions file. covers_by_type is deliberately NOT touched (user
decision 2026-07-06: default-cover fix only; type-slot sync deferred to the
make_web intent-serving work).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2.extras  # noqa: E402
from tools.canonical_v2_neon_loader import _connect  # noqa: E402

REPORT_DIR = ROOT / "data/reports/cover_audit/repick_chunk1k"
DECISIONS = REPORT_DIR / "cover_repick_decisions.json"


def load_swaps(path: Path, limit: int | None) -> list[tuple[str, str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str]] = []
    for rec in data["decisions"].values():
        if rec.get("decision") != "approve_swap":
            continue
        bid = rec["canonical_bld_id"]
        old, new = rec.get("old_display_cover_url"), rec.get("new_display_cover_url")
        if not (old and new) or not new.startswith("http"):
            raise SystemExit(f"{bid}: approve_swap with malformed urls (old={old!r}, new={new!r})")
        if new == old:
            raise SystemExit(f"{bid}: approve_swap but new == old")
        rows.append((bid, old, new))
    ids = [r[0] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate canonical_bld_id in approve_swap set")

    # snapshot pinning: the decisions must still describe the reviewed pairing.
    # ensure_decisions() refreshes old urls from review_cases.json on every app
    # load while decision/new stay frozen, so a rebuilt cases file could silently
    # produce fresh-old + stale-approved-new rows the human never reviewed.
    cases_path = path.parent / "review_cases.json"
    cases = {c["canonical_bld_id"]: c
             for c in json.loads(cases_path.read_text(encoding="utf-8"))["cases"]}
    for bid, old, new in rows:
        case = cases.get(bid)
        if case is None:
            raise SystemExit(f"{bid}: approved but absent from {cases_path.name}")
        if old != case["current_cover_url"] or new != case["proposed_cover_url"]:
            raise SystemExit(
                f"{bid}: decision urls diverge from the reviewed case "
                f"(cases file rebuilt after review?) — refusing to apply")

    rows.sort()
    return rows[:limit] if limit is not None else rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--decisions", default=str(DECISIONS))
    def positive_int(v: str) -> int:
        n = int(v)
        if n < 1:
            raise argparse.ArgumentTypeError("--limit must be >= 1")
        return n

    ap.add_argument("--limit", type=positive_int, help="cap rows (smoke ladder)")
    ap.add_argument("--apply", action="store_true",
                    help="attempt live write (also needs --confirm-db-write)")
    ap.add_argument("--confirm-db-write", action="store_true")
    args = ap.parse_args()
    live = args.apply and args.confirm_db_write

    rows = load_swaps(Path(args.decisions), args.limit)
    report = {"mode": "LIVE-COMMIT" if live else "dry-run(rollback)",
              "approved_swaps": len(rows)}
    if not rows:
        print(json.dumps(report | {"note": "nothing to apply"}, indent=2))
        return 0

    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            psycopg2.extras.execute_values(cur, """
                UPDATE canonical_v2_buildings b SET display_cover_url = v.newu
                FROM (VALUES %s) AS v(id, oldu, newu)
                WHERE b.canonical_bld_id = v.id
                  AND b.display_cover_url IS NOT DISTINCT FROM v.oldu
                  AND b.display_cover_url IS DISTINCT FROM v.newu
            """, rows, template="(%s, %s, %s)", page_size=len(rows) + 1)
            report["rows_affected"] = cur.rowcount

            # in-txn QC: after the UPDATE every targeted row must hold new
            ids = [r[0] for r in rows]
            want = {bid: new for bid, _old, new in rows}
            cur.execute("""SELECT canonical_bld_id, display_cover_url
                           FROM canonical_v2_buildings
                           WHERE canonical_bld_id = ANY(%s)""", (ids,))
            got = {r["canonical_bld_id"]: r["display_cover_url"] for r in cur.fetchall()}
            missing = sorted(set(ids) - set(got))
            stale = sorted(bid for bid, url in got.items() if url != want[bid])
            report["ids_missing_in_db"] = missing
            report["ids_stale_guard_blocked"] = stale
            report["null_covers_after"] = sum(1 for url in got.values() if not url)
            report["sample_after"] = [
                {"id": bid, "display_cover_url": got.get(bid)} for bid in ids[:5]]
            qc_pass = not missing and not stale and report["null_covers_after"] == 0
            report["qc_in_txn"] = "PASS" if qc_pass else "FAIL"

            if live and not qc_pass:
                conn.rollback()
                report["committed"] = False
                report["note"] = "QC FAIL — rolled back despite --apply"
                print(json.dumps(report, indent=2, default=str))
                return 1

        if live:
            conn.commit()
            report["committed"] = True
        else:
            conn.rollback()
            report["committed"] = False
    finally:
        conn.close()

    if live:
        applied_at = datetime.now(timezone.utc).isoformat()
        sidecar = Path(args.decisions).parent / f"applied_cover_swaps_{applied_at[:10]}.jsonl"
        try:
            with open(sidecar, "a", encoding="utf-8") as f:
                for bid, old, new in rows:
                    f.write(json.dumps(
                        {"canonical_bld_id": bid, "old": old, "new": new,
                         "applied_at": applied_at,
                         "run_rows_affected": report["rows_affected"]},
                        ensure_ascii=False) + "\n")
            report["reversal_sidecar"] = str(sidecar)
        except OSError as exc:
            # the commit is already durable; decisions JSON still holds old/new
            report["reversal_sidecar_error"] = str(exc)

    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("qc_in_txn") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
