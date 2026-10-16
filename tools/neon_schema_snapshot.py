#!/usr/bin/env python3
"""Read-only Postgres schema snapshot + drift check for the archi_data DB.

Purpose: keep dev branches and the production Neon DB from silently
diverging. The snapshot is normalized, sorted JSON with NO host / credential
information, so it can be committed (docs/schema/archi_data.schema.json).

Connection (never writes; session is forced read-only):
  --dsn-env VAR        postgres URL taken from env var VAR (after loading .env)
  (default)            make_db .env via tools/canonical_v2_neon_loader._connect

Modes:
  --out FILE                      write snapshot JSON
  --compare FILE                  diff live snapshot (left) vs FILE (right)
  --compare-dsn-env VAR           diff live snapshot vs a second live DB
  --snapshot-file FILE            use FILE as the left side instead of a live DB
                                  (offline file-vs-file compare)

Exit codes:
  0  no schema drift (data differences are INFO only)
  2  schema DRIFT (tables/columns/types/nullability/defaults/indexes/
     constraints/extensions differ)
  3  data differences (row counts / tag corpus_version) with --strict-data
  1  usage / connection error

Examples:
  .venv/Scripts/python.exe tools/neon_schema_snapshot.py --out docs/schema/archi_data.schema.json
  .venv/Scripts/python.exe tools/neon_schema_snapshot.py --dsn-env DEV_DATABASE_URL --compare docs/schema/archi_data.schema.json
  .venv/Scripts/python.exe tools/neon_schema_snapshot.py --dsn-env DEV_DATABASE_URL --compare-dsn-env DATABASE_URL --strict-data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORMAT = "neon_schema_snapshot/v1"
DATA_TABLE_PREFIX = "canonical_v2_"
SCHEMA_SECTIONS = ("server_version_major", "schemas", "extensions", "tables")
DATA_SECTIONS = ("data",)

RELKIND = {"r": "table", "p": "partitioned_table", "v": "view", "m": "materialized_view", "f": "foreign_table"}


# --------------------------------------------------------------------------- connection

def connect(dsn_env: str | None):
    """Open a connection and force the session read-only. Never prints the DSN."""
    import psycopg2  # local import keeps unit tests free of the driver

    if dsn_env:
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
        except ImportError:  # pragma: no cover
            pass
        dsn = os.environ.get(dsn_env)
        if not dsn:
            raise SystemExit(f"env var {dsn_env} is not set (value never printed)")
        conn = psycopg2.connect(dsn)
    else:
        from tools.canonical_v2_neon_loader import _connect

        conn = _connect()
    conn.set_session(readonly=True, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on")
    return conn


# --------------------------------------------------------------------------- fetch

def _fetch(cur, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
    cur.execute(sql, tuple(params))
    return list(cur.fetchall())


def fetch_raw(conn, schemas: list[str]) -> dict[str, Any]:
    """Run catalog queries (SELECT only) and return raw rows for normalize()."""
    with conn.cursor() as cur:
        raw: dict[str, Any] = {}
        raw["server_version_num"] = _fetch(cur, "SHOW server_version_num")[0][0]
        raw["schemas_present"] = [r[0] for r in _fetch(
            cur, "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)", [schemas])]
        raw["extensions"] = _fetch(cur, "SELECT extname, extversion FROM pg_extension")
        raw["relations"] = _fetch(cur, """
            SELECT n.nspname, c.relname, c.relkind,
                   CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid, true) END
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','f')""", [schemas])
        raw["columns"] = _fetch(cur, """
            SELECT n.nspname, c.relname, a.attname,
                   format_type(a.atttypid, a.atttypmod), a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated
              FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
              LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','f')
               AND a.attnum > 0 AND NOT a.attisdropped""", [schemas])
        raw["indexes"] = _fetch(cur, """
            SELECT schemaname, tablename, indexname, indexdef
              FROM pg_indexes WHERE schemaname = ANY(%s)""", [schemas])
        raw["constraints"] = _fetch(cur, """
            SELECT n.nspname, c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid, true)
              FROM pg_constraint k
              JOIN pg_class c ON c.oid = k.conrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ANY(%s)""", [schemas])

        # Data facts: exact row counts for canonical_v2_* base tables + tag corpus_version.
        row_counts: dict[str, int] = {}
        corpus: dict[str, list[str]] = {}
        col_index = {(s, t, col) for s, t, col, *_ in raw["columns"]}
        for schema, rel, kind, _ in raw["relations"]:
            if kind not in ("r", "p") or not rel.startswith(DATA_TABLE_PREFIX):
                continue
            ident = f'"{schema}"."{rel}"'
            row_counts[f"{schema}.{rel}"] = int(_fetch(cur, f"SELECT count(*) FROM {ident}")[0][0])
            if rel.startswith(DATA_TABLE_PREFIX + "tag_") and (schema, rel, "corpus_version") in col_index:
                corpus[f"{schema}.{rel}"] = [
                    r[0] for r in _fetch(cur, f"SELECT DISTINCT corpus_version::text FROM {ident}")]
        raw["row_counts"] = row_counts
        raw["corpus_versions"] = corpus
    conn.rollback()
    return raw


# --------------------------------------------------------------------------- normalize

def normalize(raw: dict[str, Any], schemas: list[str]) -> dict[str, Any]:
    """Build the canonical, sorted, credential-free snapshot dict."""
    tables: dict[str, dict[str, Any]] = {}
    for schema, rel, kind, viewdef in raw["relations"]:
        entry: dict[str, Any] = {"kind": RELKIND.get(kind, kind), "columns": {}, "indexes": {}, "constraints": {}}
        if viewdef is not None:
            entry["definition"] = " ".join(str(viewdef).split())
        tables[f"{schema}.{rel}"] = entry
    for schema, rel, col, typ, notnull, default, identity, generated in raw["columns"]:
        key = f"{schema}.{rel}"
        if key not in tables:
            continue
        c: dict[str, Any] = {"type": typ, "nullable": not bool(notnull), "default": default}
        if identity:
            c["identity"] = identity
        if generated:
            c["generated"] = generated
        tables[key]["columns"][col] = c
    for schema, rel, name, indexdef in raw["indexes"]:
        key = f"{schema}.{rel}"
        if key in tables:
            tables[key]["indexes"][name] = indexdef
    for schema, rel, name, contype, definition in raw["constraints"]:
        key = f"{schema}.{rel}"
        if key in tables:
            tables[key]["constraints"][name] = {"type": contype, "definition": definition}
    snap = {
        "format": FORMAT,
        "server_version_major": int(raw["server_version_num"]) // 10000,
        "schemas": sorted(set(raw["schemas_present"]) & set(schemas)),
        "extensions": {name: ver for name, ver in raw["extensions"]},
        "tables": tables,
        "data": {
            "row_counts": dict(raw["row_counts"]),
            "corpus_versions": {k: sorted(v) for k, v in raw["corpus_versions"].items()},
        },
    }
    return sort_obj(snap)


def sort_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: sort_obj(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [sort_obj(v) for v in obj]
    return obj


def dumps(snap: dict[str, Any]) -> str:
    return json.dumps(sort_obj(snap), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def snapshot(conn, schemas: list[str]) -> dict[str, Any]:
    return normalize(fetch_raw(conn, schemas), schemas)


# --------------------------------------------------------------------------- diff

def _entities(snap: dict[str, Any], sections: Iterable[str]) -> dict[str, Any]:
    """Flatten to one entry per comparable entity (a column, an index, ...)."""
    out: dict[str, Any] = {}
    for sec in sections:
        val = snap.get(sec)
        if sec == "tables":
            for t, entry in (val or {}).items():
                for k, v in entry.items():
                    if isinstance(v, dict):
                        for name, item in v.items():
                            out[f"tables/{t}/{k}/{name}"] = item
                    else:
                        out[f"tables/{t}/{k}"] = v
        elif isinstance(val, dict):
            for k, v in val.items():
                if isinstance(v, dict):
                    for name, item in v.items():
                        out[f"{sec}/{k}/{name}"] = item
                else:
                    out[f"{sec}/{k}"] = v
        else:
            out[sec] = val
    return out


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, list[str]]:
    """Return {'schema': [...], 'data': [...]} human-readable difference lines.

    '-' only in left, '+' only in right, '~' changed (changed attrs listed).
    """
    result: dict[str, list[str]] = {"schema": [], "data": []}
    for bucket, sections in (("schema", SCHEMA_SECTIONS), ("data", DATA_SECTIONS)):
        lf, rf = _entities(left, sections), _entities(right, sections)
        for key in sorted(set(lf) | set(rf)):
            if key not in rf:
                result[bucket].append(f"- {key} = {_j(lf[key])}")
            elif key not in lf:
                result[bucket].append(f"+ {key} = {_j(rf[key])}")
            elif lf[key] != rf[key]:
                a, b = lf[key], rf[key]
                if isinstance(a, dict) and isinstance(b, dict):
                    parts = [f"{k}: {_j(a.get(k))} -> {_j(b.get(k))}"
                             for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
                    result[bucket].append(f"~ {key}: " + "; ".join(parts))
                else:
                    result[bucket].append(f"~ {key}: {_j(a)} -> {_j(b)}")
    return result


def report(d: dict[str, list[str]], left_label: str, right_label: str, strict_data: bool) -> tuple[str, int]:
    lines = [f"left  = {left_label}", f"right = {right_label}"]
    if d["schema"]:
        lines.append(f"DRIFT: {len(d['schema'])} schema difference(s)")
        lines += ["  " + x for x in d["schema"]]
    else:
        lines.append("schema: identical")
    if d["data"]:
        tag = "DATA-DRIFT" if strict_data else "INFO"
        lines.append(f"{tag}: {len(d['data'])} data difference(s) (row counts / corpus_version)")
        lines += ["  " + x for x in d["data"]]
    else:
        lines.append("data: identical")
    code = 2 if d["schema"] else (3 if (strict_data and d["data"]) else 0)
    return "\n".join(lines), code


# --------------------------------------------------------------------------- CLI

def _load(path: str) -> dict[str, Any]:
    snap = json.loads(Path(path).read_text(encoding="utf-8"))
    if snap.get("format") != FORMAT:
        raise SystemExit(f"{path}: unexpected format {snap.get('format')!r}")
    return snap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn-env", help="env var holding the postgres URL (default: make_db .env)")
    ap.add_argument("--schemas", default="public", help="comma-separated schemas (default: public)")
    ap.add_argument("--snapshot-file", help="use this snapshot file as the left side (no DB connection)")
    ap.add_argument("--out", help="write the (left) snapshot JSON here")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--compare", help="snapshot file to compare against")
    grp.add_argument("--compare-dsn-env", help="env var of a second DB to compare against")
    ap.add_argument("--strict-data", action="store_true", help="treat row-count/corpus diffs as failures (exit 3)")
    args = ap.parse_args(argv)
    schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]

    if args.snapshot_file:
        left = _load(args.snapshot_file)
        left_label = f"file:{args.snapshot_file}"
    else:
        conn = connect(args.dsn_env)
        try:
            left = snapshot(conn, schemas)
        finally:
            conn.close()
        left_label = f"db:{args.dsn_env or 'make_db .env'}"

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(dumps(left), encoding="utf-8")
        print(f"wrote {args.out}: {len(left['tables'])} relations, "
              f"{len(left['data']['row_counts'])} counted tables")

    if args.compare or args.compare_dsn_env:
        if args.compare:
            right = _load(args.compare)
            right_label = f"file:{args.compare}"
        else:
            conn = connect(args.compare_dsn_env)
            try:
                right = snapshot(conn, schemas)
            finally:
                conn.close()
            right_label = f"db:{args.compare_dsn_env}"
        text, code = report(diff(left, right), left_label, right_label, args.strict_data)
        print(text)
        return code

    if not args.out:
        sys.stdout.write(dumps(left))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
