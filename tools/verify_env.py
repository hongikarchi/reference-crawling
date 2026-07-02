#!/usr/bin/env python3
"""Environment self-check after moving machines (Mac -> Windows/other) — 2026-07.

Run this FIRST on a new machine. Reports what's ready and what must still be
copied/installed, so work can resume without guessing. Read-only. Cross-platform.

  python3 tools/verify_env.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEPS = ["numpy", "ijson", "requests", "PIL", "torch", "transformers",
        "sentence_transformers", "sentencepiece", "psycopg2"]
SECRETS = [".env", ".env.make-web"]
DATA_MUST_COPY = [
    "data/canonical/country_conflict_refresh/"
    "canonical_buildings_strict_embedded.completeness_c26_rem2026q2.json",  # 1GB source
    "data/reports/cover_audit/repick_chunk1k/confirmed.jsonl",              # 164 deliverable
]
DERIVABLE = [  # rebuilt by tools from the 1GB source — absence is OK
    "data/reports/neighbor_eval/embeddings.npy",
    "data/reports/cover_audit/all_images.jsonl",
]


def check(label, ok, note=""):
    mark = "OK  " if ok else "MISS"
    print(f"  [{mark}] {label}{('  — ' + note) if note else ''}")
    return ok


def main() -> int:
    print("=== deps ===")
    missing_dep = []
    for d in DEPS:
        try:
            m = importlib.import_module(d)
            check(d, True, getattr(m, "__version__", ""))
        except Exception:
            check(d, False, "pip install")
            missing_dep.append(d)

    print("=== GPU ===")
    try:
        import torch
        if torch.cuda.is_available():
            dev = f"cuda ({torch.cuda.get_device_name(0)})"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            dev = "mps (Apple)"
        else:
            dev = "cpu (works, slower — fine for the cover/eval tools)"
        print(f"  device: {dev}")
    except Exception:
        print("  torch missing — install first")

    print("=== secrets (copy manually, NEVER via git) ===")
    miss_secret = [s for s in SECRETS if not (ROOT / s).exists()]
    for s in SECRETS:
        check(s, (ROOT / s).exists(), "" if (ROOT / s).exists() else "copy from Mac via USB")

    print("=== data: must-copy (not in git) ===")
    miss_data = [f for f in DATA_MUST_COPY if not (ROOT / f).exists()]
    for f in DATA_MUST_COPY:
        p = ROOT / f
        check(Path(f).name, p.exists(),
              "" if p.exists() else "copy from Mac")

    print("=== data: derivable (OK if missing — tools rebuild) ===")
    for f in DERIVABLE:
        p = ROOT / f
        print(f"  [{'have' if p.exists() else 'rebuild'}] {Path(f).name}")

    print("=== Neon (read-only reachability) ===")
    neon = None
    if (ROOT / ".env").exists() and "psycopg2" not in missing_dep:
        try:
            sys.path.insert(0, str(ROOT))
            from tools.canonical_v2_neon_loader import _connect
            con = _connect()
            cur = con.cursor()
            cur.execute("SELECT count(*) FROM canonical_v2_buildings")
            n = cur.fetchone()[0]
            con.close()
            neon = True
            print(f"  [OK  ] connected — canonical_v2_buildings has {n} rows")
        except Exception as e:
            neon = False
            print(f"  [MISS] {str(e)[:80]}")
    else:
        print("  [skip] need .env + psycopg2 first")

    print("\n=== VERDICT ===")
    blockers = []
    if missing_dep:
        blockers.append(f"pip install: {' '.join(missing_dep)}")
    if miss_secret:
        blockers.append(f"copy secrets: {', '.join(miss_secret)}")
    if miss_data:
        blockers.append(f"copy data: {', '.join(Path(f).name for f in miss_data)}")
    if not blockers:
        print("  READY — resume work. See docs/MIGRATION_WINDOWS.md for current state + next step.")
    else:
        print("  NOT READY. Do:")
        for b in blockers:
            print(f"    - {b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
