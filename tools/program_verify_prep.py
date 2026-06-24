#!/usr/bin/env python3
"""Prepare blind A/B verify batches for the program_error subset (independent Opus check).

Reads the Sonnet classify output (program_diag_classify.json: list of {id, category,
suggested_program,...}) and the diagnosis queue (program_diag_queue.jsonl: id, evidence,
stored_program). For each program_error row builds a BLIND pair {option_A, option_B} =
deterministically-shuffled (stored_program, suggested_program) so the Opus verifier never
knows which is the incumbent. Writes:
  - /tmp/prog_diag_batches/verify_NNN.json  (arrays of {id, evidence, option_A, option_B})
  - data/canonical/program_verify_map.json  ({id: {A,B,stored,suggested}})  reverse map

Verdict resolution (later, in program_corrections.py): better==(the option that is the
SUGGESTED program) => CONFIRMED program_error; better==stored or equal => NOT confirmed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CLASSIFY = ROOT / "data/canonical/program_diag_classify.json"
QUEUE = ROOT / "data/canonical/program_diag_queue.jsonl"
MAPOUT = ROOT / "data/canonical/program_verify_map.json"
BATCHDIR = Path("/tmp/prog_diag_batches")


def _flip(bid: str) -> int:
    return sum(ord(c) for c in bid) % 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--classify", default=str(CLASSIFY))
    ap.add_argument("--batch-size", type=int, default=20)
    args = ap.parse_args()

    classify = json.loads(Path(args.classify).read_text())
    # accept either {results:[...]} or [...]
    rows = classify.get("results", classify) if isinstance(classify, dict) else classify
    ev = {}
    with QUEUE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                ev[r["id"]] = r

    errs = [r for r in rows if r.get("category") == "program_error" and r.get("suggested_program")]
    items, mapping = [], {}
    for r in errs:
        bid = r["id"]
        q = ev.get(bid)
        if not q:
            continue
        stored = q.get("stored_program")
        sugg = r["suggested_program"]
        if sugg == stored:
            continue  # no-op
        if _flip(bid) == 0:
            a, b = stored, sugg
        else:
            a, b = sugg, stored
        items.append({"id": bid, "evidence": q.get("evidence"), "option_A": a, "option_B": b})
        mapping[bid] = {"A": a, "B": b, "stored": stored, "suggested": sugg}

    MAPOUT.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
    # clear old verify batches
    for f in BATCHDIR.glob("verify_*.json"):
        f.unlink()
    nb = 0
    for i in range(0, len(items), args.batch_size):
        chunk = items[i:i + args.batch_size]
        (BATCHDIR / f"verify_{i // args.batch_size:03d}.json").write_text(
            json.dumps(chunk, ensure_ascii=False))
        nb += 1
    print(json.dumps({"program_errors": len(errs), "verify_items": len(items),
                      "verify_batches": nb, "map": str(MAPOUT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
