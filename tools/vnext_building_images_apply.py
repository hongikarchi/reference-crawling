#!/usr/bin/env python
"""Apply the vNext S6-lite building_images artifact to Neon (exact-set sync). DRY-RUN BY DEFAULT.

One transaction:
  1. refuse unless the artifact manifest + independent validation are PASS and SHA-consistent
     (validation.manifest_sha256 == manifest file, artifact file SHA == manifest, DDL SHA == manifest pin);
  2. CREATE TABLE IF NOT EXISTS from docs/vnext/BUILDING_IMAGES_DDL_v1_1.sql (+ indexes, GRANT make_web);
     an existing table must already be v1.1-shaped (medium nullable), else refuse;
  3. COPY the artifact rows into a temp stage; refuse if any parent canonical_bld_id is missing;
  4. diff (unchanged / to_insert / to_delete / stale buildings), then per-building delete-then-insert
     for every artifact building and delete of stale buildings not in the artifact (exact set);
  5. verify row/building counts, FK orphans, 0-based sort contiguity, url uniqueness per building,
     unclassified shape, make_web SELECT grant, and an ordered content md5 equal to the local artifact;
  6. ROLLBACK (default) or, with --apply --confirm-db-write and all checks PASS, COMMIT.

--dsn-env NAME targets another DB (e.g. the dev-ksangjo branch); default is the make_db .env DB (prod).
Prints one JSON summary; never prints the DSN.

Operational caveats (job card 20261023_vnext_s6_lite_building_images.md §5):
  * The dry-run is NOT lock-free: it really executes the DDL (CREATE TABLE + FK to canonical_v2_buildings
    takes a SHARE ROW EXCLUSIVE lock on the parent), DELETE and ~700k-row INSERT before ROLLBACK. Run it on
    prod only when no parent loader is running. The dev dry-run is the first real-Postgres test of the SQL.
  * FK is ON DELETE CASCADE. tools/canonical_v2_neon_loader.py deletes ``removed_ids`` before its upsert;
    a reinstated id gets its parent row back but its building_images rows are gone. After ANY parent
    re-upsert that passes removed_ids, re-run this tool (dry-run, then apply) to restore the child set.

  .venv/Scripts/python.exe tools/vnext_building_images_apply.py --dsn-env DEV_DATABASE_URL          # dry-run on dev
  .venv/Scripts/python.exe tools/vnext_building_images_apply.py --dsn-env DEV_DATABASE_URL --apply --confirm-db-write
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

DEFAULT_MANIFEST = ROOT / "data/reports/vnext_s6_lite_building_images_v1_manifest.json"
DEFAULT_VALIDATION = ROOT / "data/reports/vnext_s6_lite_building_images_v1_validation.json"
DEFAULT_DDL = ROOT / "docs/vnext/BUILDING_IMAGES_DDL_v1_1.sql"
TABLE = "public.building_images"
PARENT = "public.canonical_v2_buildings"
STAGE_COLUMNS = ("canonical_bld_id", "image_asset_id", "url", "sort_order", "projection_version")
CLASS_COLUMNS = ("medium", "spatial_context", "framing_scale", "camera_angle", "drawing_kind",
                 "project_state", "project_legibility", "engine", "engine_version")


class ApplyRefused(RuntimeError):
    pass


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 24), b""):
            h.update(block)
    return h.hexdigest()


def preflight(manifest_path: Path, validation_path: Path, ddl_path: Path, root: Path = ROOT) -> dict[str, Any]:
    """Load and cross-check manifest/validation/artifact/DDL; return rows + local digests."""
    if not manifest_path.exists() or not validation_path.exists():
        raise ApplyRefused("manifest or validation report missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if validation.get("status") != "PASS" or validation.get("passed") is not True or validation.get("failed_check_names"):
        raise ApplyRefused("validation is not PASS")
    manifest_sha = _sha_file(manifest_path)
    if validation.get("manifest_sha256") != manifest_sha:
        raise ApplyRefused("validation was produced for a different manifest")
    if validation.get("ordered_rowset_sha256") != manifest.get("ordered_rowset_sha256"):
        raise ApplyRefused("validation rowset sha != manifest rowset sha")
    artifact = root / manifest["output"]["path"]
    raw = artifact.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["output"]["sha256"]:
        raise ApplyRefused("artifact sha != manifest")
    ddl_pin = manifest.get("input_pins", {}).get("ddl_v1_1", {}).get("sha256")
    if _sha_file(ddl_path) != ddl_pin:
        raise ApplyRefused("DDL v1.1 sha != manifest pin")
    rows = []
    for line in raw.decode("utf-8").splitlines():
        if not line:
            continue
        r = json.loads(line)
        if any(r.get(c) is not None for c in CLASS_COLUMNS) or r.get("semantic_type") != [] or r.get("cover_slot") != [] or r.get("is_cover_default") is not False:
            raise ApplyRefused("artifact row is not an unclassified S6-lite row")
        rows.append(tuple(r[c] for c in STAGE_COLUMNS))
    if len(rows) != manifest["row_counts"]["building_images"]:
        raise ApplyRefused("artifact row count != manifest")
    rows.sort(key=lambda t: (t[0], t[3]))
    return {
        "manifest": manifest,
        "manifest_sha256": manifest_sha,
        "rows": rows,
        "buildings": len({r[0] for r in rows}),
        "content_md5": content_md5(rows),
        "ddl_sql": ddl_path.read_text(encoding="utf-8"),
    }


def content_md5(rows: Sequence[tuple]) -> str:
    """md5 of '\\n'.join(bld\\turl-ish line) in (bld COLLATE "C", sort_order) order — same as the SQL check."""
    ordered = sorted(rows, key=lambda t: (t[0].encode("utf-8"), t[3]))
    text = "\n".join(f"{b}\t{a}\t{u}\t{s}" for b, a, u, s, _ in ordered)
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def copy_payload(rows: Sequence[tuple]) -> io.StringIO:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    buf.seek(0)
    return buf


def connect(dsn_env: str | None):
    import psycopg2

    if dsn_env:
        dsn = os.environ.get(dsn_env)
        if not dsn:
            raise SystemExit(f"env var {dsn_env} is empty (value never printed)")
        return psycopg2.connect(dsn)
    from canonical_v2_neon_loader import _connect

    return _connect()


def _one(cur, sql: str, params: Any = None) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return None if row is None else row[0]


def sync(conn, pre: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    rows = pre["rows"]
    result: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "manifest_sha256": pre["manifest_sha256"],
        "run_id": pre["manifest"].get("run_id"),
        "artifact_rows": len(rows),
        "artifact_buildings": pre["buildings"],
        "committed": False,
    }
    checks: dict[str, bool] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            result["db"] = list(cur.fetchone())
            exists = _one(cur, "SELECT to_regclass(%s) IS NOT NULL", (TABLE,))
            result["table_existed"] = bool(exists)
            if exists:
                nullable = _one(cur, "SELECT is_nullable FROM information_schema.columns WHERE table_schema='public' "
                                     "AND table_name='building_images' AND column_name='medium'")
                if nullable != "YES":
                    raise ApplyRefused("existing public.building_images is not DDL v1.1 (medium NOT NULL); migrate first")
            cur.execute(pre["ddl_sql"])
            cur.execute("CREATE TEMP TABLE s6_stage (canonical_bld_id TEXT NOT NULL, image_asset_id TEXT NOT NULL, "
                        "url TEXT NOT NULL, sort_order INTEGER NOT NULL, projection_version TEXT NOT NULL) ON COMMIT DROP")
            cur.copy_expert("COPY s6_stage (" + ",".join(STAGE_COLUMNS) + ") FROM STDIN WITH (FORMAT csv)", copy_payload(rows))
            staged = _one(cur, "SELECT count(*) FROM s6_stage")
            checks["stage_count"] = staged == len(rows)
            missing = _one(cur, f"SELECT count(DISTINCT s.canonical_bld_id) FROM s6_stage s LEFT JOIN {PARENT} b "
                                "ON b.canonical_bld_id = s.canonical_bld_id WHERE b.canonical_bld_id IS NULL")
            result["missing_parents"] = missing
            if missing:
                raise ApplyRefused(f"{missing} artifact buildings have no parent row in {PARENT}")
            result["before_rows"] = _one(cur, f"SELECT count(*) FROM {TABLE}")
            result["unchanged_rows"] = _one(cur, f"SELECT count(*) FROM {TABLE} t JOIN s6_stage s USING "
                                                 "(canonical_bld_id, image_asset_id, url, sort_order, projection_version)")
            result["to_insert"] = len(rows) - result["unchanged_rows"]
            result["to_delete"] = result["before_rows"] - result["unchanged_rows"]
            result["stale_buildings"] = _one(cur, f"SELECT count(DISTINCT t.canonical_bld_id) FROM {TABLE} t WHERE NOT EXISTS "
                                                  "(SELECT 1 FROM s6_stage s WHERE s.canonical_bld_id = t.canonical_bld_id)")
            cur.execute(f"DELETE FROM {TABLE} t USING (SELECT DISTINCT canonical_bld_id FROM s6_stage) s "
                        "WHERE t.canonical_bld_id = s.canonical_bld_id")
            result["deleted_artifact_buildings_rows"] = cur.rowcount
            cur.execute(f"DELETE FROM {TABLE} t WHERE NOT EXISTS (SELECT 1 FROM s6_stage s WHERE s.canonical_bld_id = t.canonical_bld_id)")
            result["deleted_stale_rows"] = cur.rowcount
            cur.execute(f"INSERT INTO {TABLE} (" + ",".join(STAGE_COLUMNS) + ") SELECT " + ",".join(STAGE_COLUMNS) +
                        " FROM s6_stage ORDER BY canonical_bld_id, sort_order")
            result["inserted"] = cur.rowcount
            after = _one(cur, f"SELECT count(*) FROM {TABLE}")
            result["after_rows"] = after
            checks["after_rows"] = after == len(rows) and result["inserted"] == len(rows)
            checks["after_buildings"] = _one(cur, f"SELECT count(DISTINCT canonical_bld_id) FROM {TABLE}") == pre["buildings"]
            checks["fk_orphans_zero"] = _one(cur, f"SELECT count(*) FROM {TABLE} t LEFT JOIN {PARENT} b ON "
                                                  "b.canonical_bld_id = t.canonical_bld_id WHERE b.canonical_bld_id IS NULL") == 0
            checks["sort_contiguous"] = _one(cur, f"SELECT count(*) FROM (SELECT canonical_bld_id, min(sort_order) mn, "
                                                  f"max(sort_order) mx, count(*) c FROM {TABLE} GROUP BY 1) g "
                                                  "WHERE mn <> 0 OR mx <> c - 1") == 0
            checks["url_unique_per_building"] = _one(cur, f"SELECT count(*) FROM (SELECT 1 FROM {TABLE} GROUP BY canonical_bld_id, url "
                                                          "HAVING count(*) > 1) d") == 0
            checks["unclassified_shape"] = _one(cur, f"SELECT count(*) FROM {TABLE} WHERE " +
                                                " OR ".join(f"{c} IS NOT NULL" for c in CLASS_COLUMNS) +
                                                " OR is_cover_default OR cardinality(semantic_type) > 0 OR cardinality(cover_slot) > 0") == 0
            db_md5 = _one(cur, "SELECT md5(coalesce(string_agg(canonical_bld_id || E'\\t' || image_asset_id || E'\\t' || url || "
                               f"E'\\t' || sort_order::text, E'\\n' ORDER BY canonical_bld_id COLLATE \"C\", sort_order), '')) FROM {TABLE}")
            result["content_md5"] = {"local": pre["content_md5"], "db": db_md5}
            checks["content_md5_equal"] = db_md5 == pre["content_md5"]
            role = _one(cur, "SELECT count(*) FROM pg_roles WHERE rolname = 'make_web'")
            checks["make_web_select_grant"] = bool(role) and bool(
                _one(cur, "SELECT has_table_privilege('make_web', %s, 'SELECT')", (TABLE,)))
        result["checks"] = checks
        result["status"] = "PASS" if checks and all(checks.values()) else "FAIL"
        if apply and result["status"] == "PASS":
            conn.commit()
            result["committed"] = True
        else:
            conn.rollback()
        return result
    except BaseException:
        conn.rollback()
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    ap.add_argument("--ddl", type=Path, default=DEFAULT_DDL)
    ap.add_argument("--dsn-env", help="env var holding the target postgres URL (default: make_db .env = prod)")
    ap.add_argument("--apply", action="store_true", help="COMMIT (requires --confirm-db-write)")
    ap.add_argument("--confirm-db-write", action="store_true")
    a = ap.parse_args(argv)
    if a.apply and not a.confirm_db_write:
        raise SystemExit("--apply needs --confirm-db-write")
    try:
        pre = preflight(a.manifest, a.validation, a.ddl)
    except ApplyRefused as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}, ensure_ascii=False))
        return 2
    conn = connect(a.dsn_env)
    try:
        try:
            res = sync(conn, pre, apply=a.apply)
        except ApplyRefused as exc:
            res = {"status": "REFUSED", "reason": str(exc), "committed": False}
    finally:
        conn.close()
    res["target"] = f"env:{a.dsn_env}" if a.dsn_env else "make_db .env (prod)"
    print(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    return 0 if res.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
