#!/usr/bin/env python3
"""make_db cleanup executor — deletes superseded/dead artifacts per the audit.

Safety design:
  * DELETE targets are explicit paths + narrow globs (never a directory wipe
    that could catch a live artifact).
  * PROTECTED paths are asserted to exist BEFORE and AFTER the run.
  * No DELETE target may be, or live under, a PROTECTED path — refuses if so.
  * Default mode is dry-run; --confirm is required to actually delete.

Authoritative list: data/reports/audit/cleanup_final.md.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --- PROTECTED: must exist before and after; nothing here may be deleted -----
PROTECTED = [
    "data/id_registry_buildings.json",
    "data/id_registry_architects.json",
    "data/id_registry.json",
    "data/crawl/divisare.db",
    "data/crawl/architizer.db",
    "data/crawl/archello.db",
    "data/crawl/metalocus.db",
    "data/canonical/phash_cache.json",
    "data/canonical/architects_canonical.json",
    "data/canonical/canonical_buildings_4source.json",
    "data/canonical/buildings_canonical.json",
    "data/canonical/metalocus_architect_clusters.json",
    "data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c8.json",
    "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8.json",
    "data/enrich/4_buildings_final.json",          # metalocus input to canonical builder — KEEP
    "data/reports/db_quality_audit.md",
    "core/vocab.py",
    "run.py",
]

# --- DELETE: explicit paths (dirs + files) and narrow globs ------------------
DELETE_DIRS = [
    "data/backups",
    "data/canonical/code_name_split_refresh",
    "data/canonical/d1_batches",
    "data/canonical/d1_batches_t1_v4",
    "data/canonical/d1_batches_t2",
    "data/canonical/d1_results",
    "data/canonical/d1_results_t1_v4",
    "data/canonical/d1_results_t2",
    "data/canonical/tiebreak_batches",
    "data/canonical/tiebreak_batches_buildings",
    "data/canonical/tiebreak_batches_buildings_llm",
    "data/canonical/tiebreak_batches_r2",
    "data/canonical/tiebreak_results",
    "data/canonical/tiebreak_results_buildings",
    "data/canonical/tiebreak_results_r2",
    "data/reports/audit/l3_images",
    "data/reports/audit/l3_shards",
]
DELETE_FILES = [
    "data/canonical/canonical_buildings_strict.json",
    "data/canonical/canonical_buildings_strict_embedded.json",
    "data/canonical/canonical_buildings_strict.unbackfilled",
    "data/canonical/canonical_buildings_4source_v1_baseline.json",
    "data/canonical/buildings_canonical_v2.json",
    "data/canonical/d1_failures.json",
    "data/canonical/e1_clusters.jsonl",
    "data/canonical/e1_clusters.jsonl.bak_pre_fix",
    "data/canonical/e_image_results.jsonl",
]
DELETE_GLOBS = [
    "data/canonical/canonical_buildings_4source_v[2-9].json",
    "data/canonical/architects_canonical.plan_b_v*.json",
    "data/canonical/architects_canonical.iter*.json",
    "data/canonical/architects_canonical.broken_*.json",
    "data/canonical/architect_tiebreak_pairs.plan_b_v*.json",
    "data/canonical/architect_tiebreak_pairs.iter*.json",
    "data/canonical/architect_tiebreak_pairs.broken_*.json",
    "data/canonical/tiebreak_pairs_v[2-9].json",
    "data/canonical/building_tiebreak_pairs_v[2-9].json",
    "data/canonical/country_conflict_refresh/d2_image_backfill_*",
    "data/canonical/country_conflict_refresh/canonical_buildings_strict*.completeness_c[3467]*.json",
    "data/canonical/country_conflict_refresh/completeness_c[3467]_affected_cids.json",
    "data/reports/*.completeness_c[3467].*",
    "data/reports/*.resume10*",
]


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for dp, _, fns in os.walk(p):
        for fn in fns:
            fp = Path(dp) / fn
            if fp.is_file():
                total += fp.stat().st_size
    return total


def _resolve_targets() -> list[Path]:
    targets: set[Path] = set()
    for d in DELETE_DIRS:
        p = ROOT / d
        if p.exists():
            targets.add(p)
    for f in DELETE_FILES:
        p = ROOT / f
        if p.exists():
            targets.add(p)
    for g in DELETE_GLOBS:
        for p in ROOT.glob(g):
            if p.exists():
                targets.add(p)
    return sorted(targets)


def main() -> int:
    confirm = "--confirm" in sys.argv
    protected = [ROOT / p for p in PROTECTED]

    missing = [p for p in protected if not p.exists()]
    if missing:
        print("ABORT — protected path(s) missing before run:", file=sys.stderr)
        for p in missing:
            print(f"  {p}", file=sys.stderr)
        return 2

    targets = _resolve_targets()

    # safety: no target may be, contain, or sit under a protected path
    prot_resolved = [p.resolve() for p in protected]
    for t in targets:
        tr = t.resolve()
        for pr in prot_resolved:
            if tr == pr or pr.is_relative_to(tr) or tr.is_relative_to(pr):
                print(f"ABORT — delete target collides with protected path:\n"
                      f"  target={t}\n  protected={pr}", file=sys.stderr)
                return 2

    total = sum(_size(t) for t in targets)
    print(f"{'DELETE' if confirm else 'DRY-RUN'} — {len(targets)} targets, "
          f"{total / 1024 / 1024 / 1024:.2f} GiB")
    for t in targets:
        rel = t.relative_to(ROOT)
        kind = "dir " if t.is_dir() else "file"
        print(f"  {kind} {_size(t)/1024/1024:9.1f} MiB  {rel}")

    if not confirm:
        print("\n(dry-run — re-run with --confirm to delete)")
        return 0

    deleted = 0
    for t in targets:
        try:
            if t.is_dir():
                shutil.rmtree(t)
            else:
                t.unlink()
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED {t}: {exc}", file=sys.stderr)

    still_missing = [p for p in protected if not p.exists()]
    if still_missing:
        print("WARNING — protected path(s) missing AFTER run:", file=sys.stderr)
        for p in still_missing:
            print(f"  {p}", file=sys.stderr)
        return 1

    print(f"\ndeleted {deleted}/{len(targets)} targets, "
          f"~{total/1024/1024/1024:.2f} GiB reclaimed; all protected paths intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
