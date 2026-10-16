"""Rewrite dead Architizer image URLs (architizer-prod.imgix.net, HTTP 410 since 2026) to the
live host images.architizer.com in canonical_v2_buildings.

Same path, new host, and the query Architizer itself serves (`?width=1680&quality=60`); the old
imgix params (w/q/auto/cs) are ignored by the new host, which would return multi-MB originals.

Default is a dry-run: run every UPDATE inside one transaction, verify, then ROLLBACK.
--apply --confirm-db-write COMMITs. Before committing, the old values of every touched row are
written to a JSONL backup (reversible). --dsn-env targets another DB (e.g. a dev branch).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

OLD_HOST = "architizer-prod.imgix.net"
NEW_PREFIX = "https://images.architizer.com"
NEW_QUERY = "?width=1680&quality=60"
TABLE = "canonical_v2_buildings"
TEXT_COLS = ("display_cover_url", "cover_image_url_default")
JSON_COLS = ("covers_by_type", "all_images", "best_image_per_cluster")
# URL = scheme + old host + path, optional query; ends at a JSON quote / whitespace.
PATTERN = r"https?://architizer-prod\.imgix\.net(/[^\"?\s]*)(\?[^\"\s]*)?"
REPLACEMENT = NEW_PREFIX + r"\1" + NEW_QUERY


def connect(dsn_env: str | None):
    import psycopg2

    if dsn_env:
        dsn = os.environ.get(dsn_env)
        if not dsn:
            raise SystemExit(f"env var {dsn_env} is empty")
        return psycopg2.connect(dsn)
    from canonical_v2_neon_loader import _connect

    return _connect()


def rewrite_sql(col: str, is_json: bool) -> str:
    expr = f"regexp_replace({col}::text, %(pat)s, %(rep)s, 'g')"
    return f"{expr}::jsonb" if is_json else expr


def where_any() -> str:
    cols = TEXT_COLS + JSON_COLS
    return " OR ".join(f"{c}::text LIKE %(like)s" for c in cols)


def run(dsn_env: str | None, apply: bool, backup_dir: Path) -> dict:
    params = {"pat": PATTERN, "rep": REPLACEMENT, "like": f"%{OLD_HOST}%"}
    conn = connect(dsn_env)
    result: dict = {"target": f"env:{dsn_env}" if dsn_env else "make_db .env", "mode": "apply" if apply else "dry-run"}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT inet_server_addr()::text, current_database(), current_user")
            result["db"] = cur.fetchone()[1:]
            before = {}
            for c in TEXT_COLS + JSON_COLS:
                cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {c}::text LIKE %(like)s", params)
                before[c] = cur.fetchone()[0]
            result["rows_with_old_host_before"] = before
            cur.execute(f"SELECT canonical_bld_id, {', '.join(TEXT_COLS + JSON_COLS)} FROM {TABLE} WHERE {where_any()}", params)
            old_rows = cur.fetchall()
            result["rows_to_update"] = len(old_rows)
            sets = ", ".join(f"{c} = {rewrite_sql(c, c in JSON_COLS)}" for c in TEXT_COLS + JSON_COLS)
            cur.execute(f"UPDATE {TABLE} SET {sets}, updated_at = now() WHERE {where_any()}", params)
            result["updated"] = cur.rowcount
            after = {}
            for c in TEXT_COLS + JSON_COLS:
                cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {c}::text LIKE %(like)s", params)
                after[c] = cur.fetchone()[0]
            result["rows_with_old_host_after"] = after
            cur.execute(
                f"SELECT count(*) FROM {TABLE} WHERE display_cover_url LIKE %s AND display_cover_url NOT LIKE %s",
                (NEW_PREFIX + "/media/%", "%" + NEW_QUERY),
            )
            result["bad_new_cover_query"] = cur.fetchone()[0]
            cur.execute(f"SELECT count(*) FROM {TABLE} WHERE display_cover_url LIKE %s", (NEW_PREFIX + "%",))
            result["covers_on_new_host"] = cur.fetchone()[0]
            cur.execute(f"SELECT display_cover_url FROM {TABLE} WHERE display_cover_url LIKE %s ORDER BY canonical_bld_id LIMIT 3", (NEW_PREFIX + "%",))
            result["sample"] = [r[0] for r in cur.fetchall()]
            ok = (
                all(v == 0 for v in after.values())
                and result["updated"] == result["rows_to_update"]
                and result["bad_new_cover_query"] == 0
            )
            result["checks"] = "PASS" if ok else "FAIL"
            if not apply or not ok:
                conn.rollback()
                result["committed"] = False
                return result
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            tag = (dsn_env or "prod").lower()
            bpath = backup_dir / f"architizer_host_migrate_backup_{tag}_{stamp}.jsonl"
            cols = ("canonical_bld_id",) + TEXT_COLS + JSON_COLS
            with bpath.open("w", encoding="utf-8") as f:
                for r in old_rows:
                    f.write(json.dumps(dict(zip(cols, r)), ensure_ascii=False, default=str) + "\n")
            result["backup"] = str(bpath)
        conn.commit()
        result["committed"] = True
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn-env", help="env var holding the target postgres URL (default: make_db .env = prod)")
    ap.add_argument("--apply", action="store_true", help="commit (requires --confirm-db-write)")
    ap.add_argument("--confirm-db-write", action="store_true")
    ap.add_argument("--backup-dir", default=str(ROOT / "data" / "reports" / "architizer_host_migrate"))
    a = ap.parse_args()
    if a.apply and not a.confirm_db_write:
        raise SystemExit("--apply needs --confirm-db-write")
    res = run(a.dsn_env, a.apply, Path(a.backup_dir))
    print(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    return 0 if res["checks"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
