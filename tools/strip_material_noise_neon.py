#!/usr/bin/env python3
"""Strip MATERIAL_TAXONOMY_NOISE terms from canonical_v2_buildings.material_visual.

Mirrors the audit's noise set. For rows where the entire material_visual array
collapses to empty after the filter, also flips is_publishable to FALSE and
appends 'material_noise_only' to publishability_reasons.

Safe modes:
  --dry-run  open transaction, run UPDATEs, report counts, then ROLLBACK
Write modes (require --confirm-db-write):
  --apply    run UPDATEs and COMMIT
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.canonical_v2_upload_validator import MATERIAL_TAXONOMY_NOISE, EMPTY_MATERIAL_REASON  # noqa: E402

NOISE = sorted(MATERIAL_TAXONOMY_NOISE)


def run(apply: bool) -> int:
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COUNT(*) FROM canonical_v2_buildings
        WHERE material_visual && %s::text[]
        """,
        (NOISE,),
    )
    rows_with_noise = cur.fetchone()[0]
    print(f"rows with at least one noise term:        {rows_with_noise}")

    cur.execute(
        """
        SELECT COUNT(*) FROM canonical_v2_buildings
        WHERE material_visual && %s::text[]
          AND NOT EXISTS (
            SELECT 1 FROM unnest(material_visual) m
            WHERE LOWER(m) <> ALL(%s::text[])
          )
        """,
        (NOISE, NOISE),
    )
    rows_becoming_empty = cur.fetchone()[0]
    print(f"  of which would become EMPTY (unpublish): {rows_becoming_empty}")

    cur.execute(
        "SELECT COUNT(*) FROM canonical_v2_buildings WHERE is_publishable=true"
    )
    pub_before = cur.fetchone()[0]
    print(f"is_publishable=true before: {pub_before}")

    # UPDATE A: strip noise where result remains non-empty
    cur.execute(
        """
        UPDATE canonical_v2_buildings
        SET material_visual = (
            SELECT array_agg(m ORDER BY ord)
            FROM unnest(material_visual) WITH ORDINALITY t(m, ord)
            WHERE LOWER(m) <> ALL(%s::text[])
        )
        WHERE material_visual && %s::text[]
          AND EXISTS (
            SELECT 1 FROM unnest(material_visual) m
            WHERE LOWER(m) <> ALL(%s::text[])
          )
        """,
        (NOISE, NOISE, NOISE),
    )
    a_count = cur.rowcount
    print(f"UPDATE A (strip noise, non-empty result):  {a_count} rows")

    # UPDATE B: strip noise + unpublish + append reason where result would be empty
    cur.execute(
        """
        UPDATE canonical_v2_buildings
        SET material_visual = ARRAY[]::text[],
            is_publishable = false,
            publishability_reasons = (
                SELECT array_agg(DISTINCT r) FROM unnest(
                    publishability_reasons || ARRAY[%s]::text[]
                ) r
            )
        WHERE material_visual && %s::text[]
          AND NOT EXISTS (
            SELECT 1 FROM unnest(material_visual) m
            WHERE LOWER(m) <> ALL(%s::text[])
          )
        """,
        (EMPTY_MATERIAL_REASON, NOISE, NOISE),
    )
    b_count = cur.rowcount
    print(f"UPDATE B (strip noise + unpublish empty):  {b_count} rows")

    cur.execute(
        "SELECT COUNT(*) FROM canonical_v2_buildings WHERE is_publishable=true"
    )
    pub_after = cur.fetchone()[0]
    print(f"is_publishable=true after:  {pub_after} (delta {pub_after - pub_before:+d})")

    cur.execute(
        """
        SELECT COUNT(*) FROM canonical_v2_buildings
        WHERE material_visual && %s::text[]
        """,
        (NOISE,),
    )
    remaining = cur.fetchone()[0]
    print(f"rows still containing noise (should be 0): {remaining}")

    # UPDATE C: clean architects.top_materials (aggregate downstream)
    cur.execute(
        "SELECT COUNT(*) FROM canonical_v2_architects WHERE top_materials && %s::text[]",
        (NOISE,),
    )
    arch_with_noise = cur.fetchone()[0]
    print(f"architects with noise in top_materials before: {arch_with_noise}")
    cur.execute(
        """
        UPDATE canonical_v2_architects
        SET top_materials = COALESCE((
            SELECT array_agg(m ORDER BY ord)
            FROM unnest(top_materials) WITH ORDINALITY t(m, ord)
            WHERE LOWER(m) <> ALL(%s::text[])
        ), ARRAY[]::text[])
        WHERE top_materials && %s::text[]
        """,
        (NOISE, NOISE),
    )
    c_count = cur.rowcount
    print(f"UPDATE C (architects.top_materials):        {c_count} rows")
    cur.execute(
        "SELECT COUNT(*) FROM canonical_v2_architects WHERE top_materials && %s::text[]",
        (NOISE,),
    )
    arch_remaining = cur.fetchone()[0]
    print(f"architects still containing noise (should be 0): {arch_remaining}")

    if apply:
        conn.commit()
        print("COMMIT")
    else:
        conn.rollback()
        print("ROLLBACK (dry-run)")
    cur.close()
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="run in transaction, then ROLLBACK")
    ap.add_argument("--apply", action="store_true", help="run UPDATEs and COMMIT (requires --confirm-db-write)")
    ap.add_argument("--confirm-db-write", action="store_true", help="required with --apply")
    args = ap.parse_args()
    if args.apply and not args.confirm_db_write:
        print("--apply requires --confirm-db-write", file=sys.stderr)
        return 2
    if not args.apply and not args.dry_run:
        print("must pass --dry-run or --apply", file=sys.stderr)
        return 2
    return run(apply=bool(args.apply))


if __name__ == "__main__":
    sys.exit(main())
