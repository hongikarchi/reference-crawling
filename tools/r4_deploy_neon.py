#!/usr/bin/env python3
"""R4 Neon deploy — Txn A (DDL) + the composable axis backfill (Txn B body).

Two-transaction design (single mega-txn rejected: ALTER TABLE holds ACCESS
EXCLUSIVE until commit, which would stall every make_web read for the whole
backfill + tag rebuild):

  Txn A (--ddl --apply --confirm-db-write)  catalog-only, sub-second locks:
      - buildings: ADD COLUMN IF NOT EXISTS era/scale/structural_system/
        roof_type/facade_pattern + CHECKs + indexes — executes the loader's
        SCHEMA_EVOLUTION_SQL (single source of truth).
      - tag tables ×3: widen the axis CHECK to the 11-axis list.
      - lock_timeout 5s + 3 retries so a long reader can't wedge the deploy.

  Txn B  run via: canonical_v2_tag_stats_build.py --build --with-r4
      --confirm-db-write --corpus-version <v>
      (calls backfill_execute(cur) below first, then rebuilds the tag tables,
      QC, single COMMIT — DML only, MVCC, readers never blocked).

  --dry-run: A+B simulated in ONE transaction then ROLLBACK. WARNING: holds
  the DDL locks for the whole rehearsal — prefer a Neon branch for a full
  rehearsal, or run at a quiet hour.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psycopg2.extras import execute_values  # noqa: E402

from tools.canonical_v2_neon_loader import SCHEMA_EVOLUTION_SQL, _connect  # noqa: E402
from tools.r4_axis_merge import (  # noqa: E402
    ERA_BUCKETS,
    LLM_AXES,
    MERGED_SIDECAR,
    load_merged,
)

TAG_TABLES = (
    "canonical_v2_tag_stats",
    "canonical_v2_tag_centroids",
    "canonical_v2_tag_vocabulary",
)


def _axis_check_sql() -> str:
    from tools.canonical_v2_tag_stats_build import ALL_AXES
    axis_list = ", ".join(f"'{a}'" for a in ALL_AXES)
    return f"axis IN ({axis_list})"


def _era_case_sql() -> str:
    parts = ["CASE WHEN project_year IS NULL THEN NULL"]
    for upper, label in ERA_BUCKETS[:-1]:
        parts.append(f"WHEN project_year < {upper} THEN '{label}'")
    parts.append(f"ELSE '{ERA_BUCKETS[-1][1]}' END")
    return " ".join(parts)


def ddl_execute(cur) -> dict:
    """Txn A body: buildings columns + tag-table axis CHECK widening."""
    cur.execute(SCHEMA_EVOLUTION_SQL)
    widened = []
    for table in TAG_TABLES:
        cur.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = %s::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%%axis%%'
            """,
            (table,),
        )
        for (conname,) in cur.fetchall():
            cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {conname}")
        cur.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_axis_check "
            f"CHECK ({_axis_check_sql()})"
        )
        widened.append(table)
    return {"buildings_columns": "applied (SCHEMA_EVOLUTION_SQL)",
            "tag_axis_check_widened": widened}


def backfill_execute(cur, sidecar: Path = MERGED_SIDECAR) -> dict:
    """Txn B body (composable): era for all rows + LLM axes from the sidecar.

    Idempotent — era recomputes deterministically; sidecar values overwrite.
    """
    era_case = _era_case_sql()
    cur.execute(
        f"UPDATE canonical_v2_buildings SET era = {era_case} "
        f"WHERE era IS DISTINCT FROM ({era_case})"
    )
    era_updated = cur.rowcount

    merged = load_merged(sidecar)
    updates = [
        (cid, *(entry.get(axis) for axis in LLM_AXES))
        for cid, entry in merged.items()
    ]
    if updates:
        execute_values(
            cur,
            """
            UPDATE canonical_v2_buildings AS t
            SET scale = v.scale,
                structural_system = v.structural_system,
                roof_type = v.roof_type,
                facade_pattern = v.facade_pattern
            FROM (VALUES %s) AS v(bid, scale, structural_system, roof_type, facade_pattern)
            WHERE t.canonical_bld_id = v.bid
            """,
            updates,
            page_size=500,
        )
    coverage = {}
    for axis in ("era",) + LLM_AXES:
        cur.execute(
            f"SELECT count(*) FILTER (WHERE {axis} IS NOT NULL), count(*) "
            f"FROM canonical_v2_buildings WHERE is_publishable"
        )
        non_null, total = cur.fetchone()
        coverage[axis] = f"{int(non_null)}/{int(total)} ({non_null / total:.1%})"
    return {
        "era_rows_updated": era_updated,
        "sidecar_cids": len(merged),
        "sidecar_rows_updated": len(updates),
        "publishable_coverage": coverage,
    }


def _run_txn_a(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '5s'")
    last_error = None
    for attempt in range(3):
        try:
            result = ddl_execute(cur)
            conn.commit()
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            conn.rollback()
            cur = conn.cursor()
            cur.execute("SET lock_timeout = '5s'")
            time.sleep(3 * (attempt + 1))
    raise SystemExit(f"Txn A failed after 3 attempts: {last_error}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ddl", action="store_true", help="Txn A: DDL only, COMMIT")
    mode.add_argument("--dry-run", action="store_true",
                      help="DDL + backfill in one txn, ROLLBACK (holds DDL locks!)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--confirm-db-write", action="store_true")
    ap.add_argument("--sidecar", type=Path, default=MERGED_SIDECAR)
    args = ap.parse_args()

    if args.ddl and not (args.apply and args.confirm_db_write):
        print("--ddl requires --apply --confirm-db-write", file=sys.stderr)
        return 2

    conn = _connect()
    if args.ddl:
        result = {"mode": "ddl(TxnA)", **_run_txn_a(conn)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("Txn A COMMIT. Next (Txn B): python3 tools/canonical_v2_tag_stats_build.py "
              "--build --with-r4 --confirm-db-write --corpus-version <v>")
    else:
        print("WARNING: dry-run holds DDL locks until rollback — keep it short / quiet hour",
              file=sys.stderr)
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '5s'")
        ddl = ddl_execute(cur)
        backfill = backfill_execute(cur, args.sidecar)
        conn.rollback()
        print(json.dumps({"mode": "dry-run(A+B, ROLLBACK)", "ddl": ddl,
                          "backfill": backfill}, indent=2, ensure_ascii=False))
        print("ROLLBACK (dry-run)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
