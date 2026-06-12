#!/usr/bin/env python3
"""Reclassify MATERIAL_TAXONOMY_NOISE in canonical_v2_buildings (Neon migration).

For every row whose `material_visual` contains noise terms:
  - element-type noise (terrace, balcony, courtyard, skylight, column, facade,
    garden, green roof, stairs) is MOVED into `architectural_elements`
    (controlled vocab, deduped) so it stays searchable — not deleted;
  - the rest (water, vegetation, lighting, walls, windows, …) is dropped from
    `material_visual`;
  - if `material_visual` collapses to empty AND no element was salvaged, the
    row is unpublished with reason `material_noise_only`.

Also (bundled):
  - R2: unpublish rows whose architect resolves only to a placeholder
    ('N/A', 'unknown', …) with reason `architect_unknown`;
  - clean noise out of `canonical_v2_architects.top_materials`.

Logic is shared with the loader (`canonical_v2_upload_validator.reclassify_material`)
so a future canonical→Neon reload reproduces the same result (idempotent).

Safe modes:
  --dry-run                 run in a transaction, report counts, ROLLBACK
  --apply --confirm-db-write  run and COMMIT
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psycopg2.extras import execute_values  # noqa: E402

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.canonical_v2_upload_validator import (  # noqa: E402
    EMPTY_MATERIAL_REASON,
    MATERIAL_TAXONOMY_NOISE,
    reclassify_material,
)

NOISE = sorted(MATERIAL_TAXONOMY_NOISE)
PLACEHOLDERS = ["unknown", "n/a", "na", "-", "--", "", "tbd", "unknown architect", "?"]
ARCHITECT_UNKNOWN_REASON = "architect_unknown"


def run(apply: bool) -> int:
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM canonical_v2_buildings WHERE is_publishable")
    pub_before = cur.fetchone()[0]

    # ---- buildings: fetch every row carrying noise, recompute in Python -----
    cur.execute(
        """
        SELECT canonical_bld_id, material_visual, architectural_elements,
               is_publishable, publishability_reasons
        FROM canonical_v2_buildings
        WHERE material_visual && %s::text[]
        """,
        (NOISE,),
    )
    rows = cur.fetchall()
    print(f"rows carrying >=1 noise term: {len(rows)}")

    updates = []
    elements_added: Counter[str] = Counter()
    n_unpublished = 0
    n_empty_kept = 0  # material emptied but salvaged an element → stays publishable
    n_elements_moved_rows = 0
    for bid, mat, elem, pub, reasons in rows:
        mat = list(mat or [])
        elem = list(elem or [])
        reasons = list(reasons or [])
        new_mat, new_elem, gained = reclassify_material(mat, elem)
        new_pub, new_reasons = pub, list(reasons)
        if mat and not new_mat and not gained:
            new_pub = False
            if EMPTY_MATERIAL_REASON not in new_reasons:
                new_reasons.append(EMPTY_MATERIAL_REASON)
        if (new_mat, new_elem, new_pub, new_reasons) == (mat, elem, pub, reasons):
            continue
        if gained:
            n_elements_moved_rows += 1
            for g in gained:
                elements_added[g] += 1
        if new_pub is False and pub is True:
            n_unpublished += 1
        if not new_mat and new_pub and mat:
            n_empty_kept += 1
        updates.append((new_mat, new_elem, new_pub, new_reasons, bid))

    print(f"UPDATE buildings: {len(updates)} rows changed")
    print(f"  of which moved >=1 element into architectural_elements: {n_elements_moved_rows}")
    print(f"  material emptied -> UNPUBLISHED (no element salvaged):   {n_unpublished}")
    print(f"  material emptied but KEPT publishable (gained element):  {n_empty_kept}")
    print(f"  elements added (by type): {dict(elements_added.most_common())}")

    if updates:
        execute_values(
            cur,
            """
            UPDATE canonical_v2_buildings AS t
            SET material_visual = v.mat,
                architectural_elements = v.elem,
                is_publishable = v.pub,
                publishability_reasons = v.reasons
            FROM (VALUES %s) AS v(bid, mat, elem, pub, reasons)
            WHERE t.canonical_bld_id = v.bid
            """,
            [(bid, mat, elem, pub, reasons) for (mat, elem, pub, reasons, bid) in updates],
            template="(%s, %s::text[], %s::text[], %s::bool, %s::text[])",
            page_size=500,
        )

    # ---- R2: unpublish placeholder-architect rows ---------------------------
    cur.execute(
        """
        UPDATE canonical_v2_buildings
        SET is_publishable = false,
            publishability_reasons = (
                SELECT array_agg(DISTINCT r)
                FROM unnest(publishability_reasons || ARRAY[%s]::text[]) r)
        WHERE is_publishable AND EXISTS (
            SELECT 1 FROM unnest(architect_names) n WHERE lower(btrim(n)) = ANY(%s::text[]))
        """,
        (ARCHITECT_UNKNOWN_REASON, PLACEHOLDERS),
    )
    r2_count = cur.rowcount
    print(f"UPDATE R2 (unpublish placeholder-architect rows): {r2_count} rows")

    cur.execute("SELECT count(*) FROM canonical_v2_buildings WHERE is_publishable")
    pub_after = cur.fetchone()[0]
    print(f"is_publishable: {pub_before} -> {pub_after} (delta {pub_after - pub_before:+d})")

    cur.execute(
        "SELECT count(*) FROM canonical_v2_buildings WHERE material_visual && %s::text[]",
        (NOISE,),
    )
    print(f"buildings still containing material noise (want 0): {cur.fetchone()[0]}")

    # ---- architects: strip noise from top_materials -------------------------
    cur.execute(
        "SELECT count(*) FROM canonical_v2_architects WHERE top_materials && %s::text[]",
        (NOISE,),
    )
    arch_before = cur.fetchone()[0]
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
    print(f"UPDATE architects.top_materials: {cur.rowcount} rows (had noise: {arch_before})")
    cur.execute(
        "SELECT count(*) FROM canonical_v2_architects WHERE top_materials && %s::text[]",
        (NOISE,),
    )
    print(f"architects still containing noise (want 0): {cur.fetchone()[0]}")
    print("NOTE: architects.top_arch_elements is NOT re-aggregated here; rebuild "
          "via canonical_v2_architects_build.py to reflect moved elements.")

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
    ap.add_argument("--apply", action="store_true", help="run and COMMIT (requires --confirm-db-write)")
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
