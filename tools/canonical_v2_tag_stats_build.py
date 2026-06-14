#!/usr/bin/env python3
"""Build the make_web precompute tables (algo-support request R1-R3).

Creates/refreshes three sibling tables in Neon `archi_data`, all derived from
`is_publishable = true` rows of canonical_v2_buildings:

  canonical_v2_tag_stats       (axis, tag) -> doc_freq, total_n, idf
  canonical_v2_tag_centroids   (axis, tag) -> L2-normalized mean embedding
  canonical_v2_tag_vocabulary  (axis, tag) -> display_ko/en, is_generic, sort_rank

Axes: program / style / color_tone / atmosphere (TEXT columns) +
material_visual / architectural_elements (TEXT[] columns, per-row DISTINCT).
doc_freq and the centroid come from the same GROUP BY query, so R1/R2 key
parity holds by construction. Labels and is_generic come from the checked-in
`data/canonical/tag_vocabulary_labels.json` (user-reviewed).

The whole refresh is one transaction: optional reclassify migration
(`--with-reclassify`, see strip_material_noise_neon.py) -> aggregation ->
DELETE+INSERT of the three tables -> in-transaction QC -> COMMIT. Any QC FAIL
rolls everything back, so partial application is impossible.

Modes:
  --discover                read-only probes (pgvector, material tag space, grants)
  --dry-run                 full build + QC in a transaction, then ROLLBACK
  --build --confirm-db-write  full build + QC, COMMIT (user-gated)
  --inspect-tables          read-only row counts of the three tables
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psycopg2.extras import execute_values  # noqa: E402

from core import vocab  # noqa: E402  (read-only import; vocab is user-owned)
from tools.canonical_v2_neon_loader import _connect, _vec_literal  # noqa: E402
from tools.r4_axis_merge import ERA_VALUES  # noqa: E402
from tools import strip_material_noise_neon  # noqa: E402

TEXT_AXES = ("program", "style", "color_tone", "atmosphere")
# R4 axes: TEXT columns that may be NULL (unresolved). Aggregated over
# non-NULL rows only; sum(doc_freq) == count of non-NULL rows, not total_n.
NULLABLE_TEXT_AXES = ("era", "scale", "structural_system", "roof_type", "facade_pattern")
ARRAY_AXES = ("material_visual", "architectural_elements")
ALL_AXES = TEXT_AXES + NULLABLE_TEXT_AXES + ARRAY_AXES

CONTROLLED = {
    "program": frozenset(vocab.PROGRAM),
    "style": frozenset(vocab.STYLE),
    "color_tone": frozenset(vocab.COLOR_TONE),
    "atmosphere": frozenset(vocab.ATMOSPHERE),
    "architectural_elements": frozenset(vocab.ARCHITECTURAL_ELEMENT),
    "era": frozenset(ERA_VALUES),  # derived axis — not in vocab.py by design
    "scale": frozenset(vocab.SCALE),
    "structural_system": frozenset(vocab.STRUCTURAL_SYSTEM),
    "roof_type": frozenset(vocab.ROOF_TYPE),
    "facade_pattern": frozenset(vocab.FACADE_PATTERN),
}

# Minimum expected coverage (non-NULL share of publishable rows) per R4 axis,
# from the N=100 smoke; WARN below floor (vision merge should only raise them).
COVERAGE_FLOORS = {
    "era": 0.95, "scale": 0.95, "facade_pattern": 0.70,
    "structural_system": 0.45, "roof_type": 0.28,
}

EMBED_DIM = 384
LABELS_PATH = ROOT / "data/canonical/tag_vocabulary_labels.json"
REPORT_PATH = ROOT / "data/reports/tag_stats_build_report.json"
GENERIC_DF_RATIO = 0.25  # tags above this corpus share are surfaced as generic candidates
MATERIAL_LABEL_MIN_DF = 100  # material terms at/above this doc_freq should carry labels

AXIS_CHECK = ", ".join(f"'{a}'" for a in ALL_AXES)

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS canonical_v2_tag_stats (
    axis            TEXT NOT NULL CHECK (axis IN ({AXIS_CHECK})),
    tag             TEXT NOT NULL,
    doc_freq        INTEGER NOT NULL CHECK (doc_freq >= 1),
    total_n         INTEGER NOT NULL,
    idf             DOUBLE PRECISION NOT NULL,
    corpus_version  TEXT NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (axis, tag)
);
CREATE TABLE IF NOT EXISTS canonical_v2_tag_centroids (
    axis            TEXT NOT NULL CHECK (axis IN ({AXIS_CHECK})),
    tag             TEXT NOT NULL,
    centroid        VECTOR({EMBED_DIM}) NOT NULL,
    n_buildings     INTEGER NOT NULL CHECK (n_buildings >= 1),
    corpus_version  TEXT NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (axis, tag)
);
CREATE TABLE IF NOT EXISTS canonical_v2_tag_vocabulary (
    axis            TEXT NOT NULL CHECK (axis IN ({AXIS_CHECK})),
    tag             TEXT NOT NULL,
    display_ko      TEXT,
    display_en      TEXT,
    is_generic      BOOLEAN NOT NULL DEFAULT FALSE,
    sort_rank       INTEGER,
    PRIMARY KEY (axis, tag)
);
"""

TABLES = (
    "canonical_v2_tag_stats",
    "canonical_v2_tag_centroids",
    "canonical_v2_tag_vocabulary",
)


def _parse_vec(text: str) -> np.ndarray:
    vec = np.array(text.strip()[1:-1].split(","), dtype=np.float64)
    if vec.shape != (EMBED_DIM,):
        raise ValueError(f"expected {EMBED_DIM}-dim vector, got {vec.shape}")
    return vec


def _l2norm(vec: np.ndarray, key: str) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        raise ValueError(f"zero-norm centroid for {key}")
    return vec / norm


def _axes(skip_elements: bool, with_r4_axes: bool = True) -> tuple[tuple[str, ...], tuple[str, ...]]:
    arr = tuple(a for a in ARRAY_AXES if not (skip_elements and a == "architectural_elements"))
    text = TEXT_AXES + (NULLABLE_TEXT_AXES if with_r4_axes else ())
    return text, arr


def probe_avg_vector(conn) -> bool:
    """Check server-side AVG(vector) support without disturbing the main txn."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT AVG(embedding) FROM canonical_v2_buildings WHERE false")
        cur.fetchone()
        ok = True
    except Exception:
        ok = False
    conn.rollback()
    cur.close()
    return ok


def fetch_total_n(cur) -> int:
    cur.execute("SELECT count(*) FROM canonical_v2_buildings WHERE is_publishable")
    return int(cur.fetchone()[0])


def fetch_axis_stats(cur, axis: str) -> list[tuple[str, int, np.ndarray]]:
    """One server-side pass: (tag, doc_freq, mean embedding) for one axis."""
    if axis in TEXT_AXES or axis in NULLABLE_TEXT_AXES:
        cur.execute(
            f"""
            SELECT {axis} AS tag, COUNT(*)::int AS doc_freq, AVG(embedding)::text AS mean_vec
            FROM canonical_v2_buildings
            WHERE is_publishable AND {axis} IS NOT NULL
            GROUP BY 1
            """
        )
    else:
        cur.execute(
            f"""
            SELECT t.tag, COUNT(*)::int AS doc_freq, AVG(b.embedding)::text AS mean_vec
            FROM canonical_v2_buildings b
            CROSS JOIN LATERAL (
                SELECT DISTINCT u AS tag FROM unnest(b.{axis}) u
            ) t
            WHERE b.is_publishable
            GROUP BY t.tag
            """
        )
    return [(tag, int(df), _parse_vec(vec)) for tag, df, vec in cur.fetchall()]


def fetch_stats_clientside(cur, axes: tuple[str, ...]) -> dict[str, list[tuple[str, int, np.ndarray]]]:
    """Fallback when AVG(vector) is unavailable: one streaming pass, numpy sums."""
    cols = ", ".join(axes)
    cur.execute(
        f"SELECT {cols}, embedding::text FROM canonical_v2_buildings WHERE is_publishable"
    )
    sums: dict[tuple[str, str], np.ndarray] = {}
    counts: dict[tuple[str, str], int] = {}
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for row in rows:
            emb = _parse_vec(row[-1])
            for i, axis in enumerate(axes):
                value = row[i]
                tags = set(value or []) if axis in ARRAY_AXES else ({value} if value else set())
                for tag in tags:
                    key = (axis, tag)
                    if key in sums:
                        sums[key] += emb
                        counts[key] += 1
                    else:
                        sums[key] = emb.copy()
                        counts[key] = 1
    out: dict[str, list[tuple[str, int, np.ndarray]]] = {axis: [] for axis in axes}
    for (axis, tag), total in sums.items():
        n = counts[(axis, tag)]
        out[axis].append((tag, n, total / n))
    return out


def load_labels(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"labels file missing: {path}\n"
            "Draft it first (data/canonical/tag_vocabulary_labels.json) — R3 needs labels."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("axes") or {}


def build_rows(
    stats: dict[str, list[tuple[str, int, np.ndarray]]],
    labels: dict,
    total_n: int,
    corpus_version: str,
):
    r1, r2, r3 = [], [], []
    label_warnings: list[str] = []
    generic_candidates: list[str] = []
    for axis, tag_stats in stats.items():
        axis_labels = labels.get(axis) or {}
        ranked = sorted(tag_stats, key=lambda t: (-t[1], t[0]))
        observed = set()
        for rank, (tag, doc_freq, mean_vec) in enumerate(ranked, start=1):
            observed.add(tag)
            idf = math.log(total_n / (doc_freq + 1))
            r1.append((axis, tag, doc_freq, total_n, idf, corpus_version))
            centroid = _l2norm(mean_vec, f"{axis}/{tag}")
            r2.append((axis, tag, _vec_literal(centroid.tolist()), doc_freq, corpus_version))
            entry = axis_labels.get(tag) or {}
            if not entry:
                if axis in CONTROLLED:
                    raise SystemExit(f"labels JSON missing controlled term: {axis}/{tag}")
                if doc_freq >= MATERIAL_LABEL_MIN_DF:
                    label_warnings.append(f"{axis}/{tag} (doc_freq {doc_freq})")
            is_generic = bool(entry.get("is_generic"))
            if doc_freq / total_n > GENERIC_DF_RATIO and not is_generic:
                generic_candidates.append(f"{axis}/{tag} ({doc_freq}/{total_n})")
            r3.append((axis, tag, entry.get("ko"), entry.get("en") or tag, is_generic, rank))
        # controlled vocab terms with zero publishable occurrences still get an
        # R3 row (make_web LEFT JOINs R3 -> R1), ranked after observed ones
        extra_rank = len(ranked)
        for tag in sorted(CONTROLLED.get(axis, frozenset()) - observed):
            extra_rank += 1
            entry = (labels.get(axis) or {}).get(tag)
            if entry is None:
                raise SystemExit(f"labels JSON missing controlled term: {axis}/{tag}")
            r3.append((axis, tag, entry.get("ko"), entry.get("en") or tag,
                       bool(entry.get("is_generic")), extra_rank))
    return r1, r2, r3, label_warnings, generic_candidates


def write_tables(cur, r1, r2, r3) -> None:
    cur.execute(SCHEMA_SQL)
    for table in TABLES:
        cur.execute(f"DELETE FROM {table}")
    execute_values(
        cur,
        """
        INSERT INTO canonical_v2_tag_stats
            (axis, tag, doc_freq, total_n, idf, corpus_version)
        VALUES %s
        """,
        r1,
        page_size=500,
    )
    execute_values(
        cur,
        """
        INSERT INTO canonical_v2_tag_centroids
            (axis, tag, centroid, n_buildings, corpus_version)
        VALUES %s
        """,
        r2,
        template=f"(%s, %s, %s::vector({EMBED_DIM}), %s, %s)",
        page_size=500,
    )
    execute_values(
        cur,
        """
        INSERT INTO canonical_v2_tag_vocabulary
            (axis, tag, display_ko, display_en, is_generic, sort_rank)
        VALUES %s
        """,
        r3,
        page_size=500,
    )


def qc(cur, r1, r2, r3, total_n: int, label_warnings, generic_candidates) -> list[dict]:
    checks: list[dict] = []

    def add(check: str, ok: bool | None, detail: str, warn: bool = False) -> None:
        status = "INFO" if ok is None else ("PASS" if ok else ("WARN" if warn else "FAIL"))
        checks.append({"check": check, "status": status, "detail": detail})

    pub = fetch_total_n(cur)
    add("total_n_matches_publishable", pub == total_n, f"total_n={total_n}, live={pub}")

    r1_by_axis: dict[str, dict[str, int]] = {}
    for axis, tag, doc_freq, _tn, _idf, _cv in r1:
        r1_by_axis.setdefault(axis, {})[tag] = doc_freq
    for axis in TEXT_AXES:
        s = sum(r1_by_axis.get(axis, {}).values())
        add(f"sum_doc_freq[{axis}]", s == total_n, f"sum={s}, total_n={total_n}")
    # nullable (R4) axes: doc_freq sums to the non-NULL row count, and
    # coverage must not regress below the smoke-measured floor (WARN).
    for axis in NULLABLE_TEXT_AXES:
        if axis not in r1_by_axis:
            continue
        cur.execute(
            f"SELECT count(*) FROM canonical_v2_buildings "
            f"WHERE is_publishable AND {axis} IS NOT NULL"
        )
        non_null = int(cur.fetchone()[0])
        s = sum(r1_by_axis[axis].values())
        add(f"sum_doc_freq[{axis}]", s == non_null, f"sum={s}, non_null={non_null}")
        coverage = non_null / total_n if total_n else 0.0
        floor = COVERAGE_FLOORS.get(axis, 0.0)
        add(f"coverage[{axis}]", coverage >= floor,
            f"{coverage:.1%} of publishable (floor {floor:.0%})", warn=True)
    for axis, controlled in CONTROLLED.items():
        oov = sorted(set(r1_by_axis.get(axis, {})) - controlled)
        add(f"oov[{axis}]", not oov, f"out-of-vocab tags: {oov or 'none'}")

    bad_idf = [
        (axis, tag) for axis, tag, doc_freq, tn, idf, _cv in r1
        if abs(idf - math.log(tn / (doc_freq + 1))) > 1e-9
    ]
    add("idf_formula", not bad_idf, f"mismatches: {bad_idf[:5] or 'none'}")

    r1_keys = {(row[0], row[1]) for row in r1}
    r2_keys = {(row[0], row[1]) for row in r2}
    r3_keys = {(row[0], row[1]) for row in r3}
    add("r1_keys_eq_r2_keys", r1_keys == r2_keys,
        f"r1-only={len(r1_keys - r2_keys)}, r2-only={len(r2_keys - r1_keys)}")
    add("r1_subset_r3", r1_keys <= r3_keys, f"missing from r3: {len(r1_keys - r3_keys)}")

    n_mismatch = [
        (axis, tag) for (axis, tag, _c, n, _cv) in r2
        if n != r1_by_axis.get(axis, {}).get(tag)
    ]
    add("n_buildings_eq_doc_freq", not n_mismatch, f"mismatches: {n_mismatch[:5] or 'none'}")

    cur.execute(
        "SELECT axis, tag, abs(1.0 - (centroid <#> centroid) * -1.0) "
        "FROM canonical_v2_tag_centroids ORDER BY random() LIMIT 5"
    )
    norm_rows = cur.fetchall()
    bad_norms = [(a, t, float(d)) for a, t, d in norm_rows if float(d) > 1e-5]
    add("centroid_norms_db_spotcheck", not bad_norms, f"off-norm: {bad_norms or 'none'}")

    add("labels_missing_high_freq", not label_warnings,
        f"unlabeled doc_freq>={MATERIAL_LABEL_MIN_DF}: {label_warnings[:10] or 'none'}",
        warn=True)
    add("generic_candidates", None,
        f"corpus share >{GENERIC_DF_RATIO:.0%} and not is_generic (user decision): "
        f"{generic_candidates or 'none'}")

    if ("style", "Brutalist") in r2_keys:
        cur.execute(
            """
            SELECT style, count(*) FROM (
                SELECT style FROM canonical_v2_buildings
                WHERE is_publishable
                ORDER BY embedding <=> (
                    SELECT centroid FROM canonical_v2_tag_centroids
                    WHERE axis = 'style' AND tag = 'Brutalist')
                LIMIT 20
            ) t GROUP BY 1 ORDER BY 2 DESC
            """
        )
        dist = dict(cur.fetchall())
        add("cosine_sanity_brutalist", None, f"top-20 nearest styles: {dist}")

    grant_failures = []
    for table in TABLES:
        try:
            cur.execute("SELECT has_table_privilege('make_web', %s, 'SELECT')", (table,))
            if not cur.fetchone()[0]:
                grant_failures.append(table)
        except Exception:
            grant_failures.append(f"{table} (probe error)")
            cur.connection.rollback()
            raise
    add("make_web_select_grant", not grant_failures, f"missing grant: {grant_failures or 'none'}")
    return checks


def run_build(args) -> int:
    labels = load_labels(Path(args.labels))
    conn = _connect()
    server_side = not args.client_side and probe_avg_vector(conn)
    if not args.client_side and not server_side:
        print("NOTE: AVG(vector) unavailable -> falling back to --client-side aggregation")
    cur = conn.cursor()

    reclassify_stats = None
    if args.with_reclassify:
        reclassify_stats = strip_material_noise_neon.execute(cur)

    r4_backfill_stats = None
    if args.with_r4:
        # DDL (columns) must already exist — run r4_deploy_neon.py Txn A first.
        from tools.r4_deploy_neon import backfill_execute
        r4_backfill_stats = backfill_execute(cur)

    total_n = fetch_total_n(cur)
    cur.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'canonical_v2_buildings' AND column_name = ANY(%s)",
        (list(NULLABLE_TEXT_AXES),),
    )
    with_r4_axes = int(cur.fetchone()[0]) == len(NULLABLE_TEXT_AXES)
    if not with_r4_axes:
        print("NOTE: R4 columns absent -> building 6-axis tables (pre-R4 mode)")
    text_axes, array_axes = _axes(args.skip_elements_axis, with_r4_axes)
    axes = text_axes + array_axes
    if server_side:
        stats = {axis: fetch_axis_stats(cur, axis) for axis in axes}
    else:
        stats = fetch_stats_clientside(cur, axes)

    r1, r2, r3, label_warnings, generic_candidates = build_rows(
        stats, labels, total_n, args.corpus_version
    )
    write_tables(cur, r1, r2, r3)
    checks = qc(cur, r1, r2, r3, total_n, label_warnings, generic_candidates)

    failed = [c for c in checks if c["status"] == "FAIL"]
    # missing grant is fixable in-transaction (default privileges should cover
    # new tables since 2026-05-24; this is the explicit fallback)
    if args.build and any(c["check"] == "make_web_select_grant" for c in failed):
        cur.execute(f"GRANT SELECT ON {', '.join(TABLES)} TO make_web")
        checks = [c for c in checks if c["check"] != "make_web_select_grant"]
        cur.execute("SELECT bool_and(has_table_privilege('make_web', t, 'SELECT')) "
                    "FROM unnest(%s::text[]) t", (list(TABLES),))
        ok = bool(cur.fetchone()[0])
        checks.append({"check": "make_web_select_grant", "status": "PASS" if ok else "FAIL",
                       "detail": "explicit GRANT applied" if ok else "GRANT did not take"})
        failed = [c for c in checks if c["status"] == "FAIL"]

    report = {
        "mode": "build" if args.build else "dry-run",
        "corpus_version": args.corpus_version,
        "with_reclassify": bool(args.with_reclassify),
        "aggregation": "server-side AVG(vector)" if server_side else "client-side numpy",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "total_n": total_n,
        "rows": {
            "tag_stats": len(r1),
            "tag_centroids": len(r2),
            "tag_vocabulary": len(r3),
        },
        "rows_per_axis": {
            axis: len(tag_stats) for axis, tag_stats in stats.items()
        },
        "reclassify": reclassify_stats,
        "r4_backfill": r4_backfill_stats,
        "qc": checks,
        "result": "FAIL" if failed else "PASS",
    }

    if failed:
        conn.rollback()
        print("ROLLBACK (QC FAIL)")
    elif args.build:
        conn.commit()
        print("COMMIT")
    else:
        conn.rollback()
        print("ROLLBACK (dry-run)")
    cur.close()
    conn.close()

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "qc"}, indent=2, ensure_ascii=False))
    for c in checks:
        print(f"  [{c['status']:4}] {c['check']}: {c['detail']}")
    print(f"report -> {args.report}")
    return 1 if failed else 0


def run_discover(args) -> int:
    conn = _connect()
    cur = conn.cursor()
    out: dict = {"mode": "discover"}
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    out["pgvector_version"] = row[0] if row else None
    out["avg_vector_supported"] = probe_avg_vector(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT count(DISTINCT u), count(*) FROM (
            SELECT unnest(material_visual) u FROM canonical_v2_buildings WHERE is_publishable
        ) t
        """
    )
    distinct_terms, occurrences = cur.fetchone()
    out["material_distinct_tags"] = int(distinct_terms)
    out["material_occurrences"] = int(occurrences)
    cur.execute(
        """
        SELECT t.tag, COUNT(*)::int AS doc_freq
        FROM canonical_v2_buildings b
        CROSS JOIN LATERAL (SELECT DISTINCT u AS tag FROM unnest(b.material_visual) u) t
        WHERE b.is_publishable
        GROUP BY 1 ORDER BY 2 DESC LIMIT 50
        """
    )
    out["material_top50"] = [{"tag": t, "doc_freq": d} for t, d in cur.fetchall()]
    cur.execute(
        """
        SELECT lower(btrim(t.tag)), array_agg(DISTINCT t.tag)
        FROM canonical_v2_buildings b
        CROSS JOIN LATERAL (SELECT DISTINCT u AS tag FROM unnest(b.material_visual) u) t
        WHERE b.is_publishable
        GROUP BY 1 HAVING count(DISTINCT t.tag) > 1
        """
    )
    out["material_case_trim_variants"] = {k: v for k, v in cur.fetchall()}
    cur.execute(
        """
        SELECT count(*) FILTER (WHERE doc_freq = 1),
               count(*) FILTER (WHERE doc_freq >= %s)
        FROM (
            SELECT COUNT(*)::int AS doc_freq
            FROM canonical_v2_buildings b
            CROSS JOIN LATERAL (SELECT DISTINCT u AS tag FROM unnest(b.material_visual) u) t
            WHERE b.is_publishable GROUP BY t.tag
        ) s
        """,
        (MATERIAL_LABEL_MIN_DF,),
    )
    singletons, labelable = cur.fetchone()
    out["material_singleton_tags"] = int(singletons)
    out[f"material_tags_df_ge_{MATERIAL_LABEL_MIN_DF}"] = int(labelable)
    grants = {}
    for table in TABLES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        exists = bool(cur.fetchone()[0])
        if exists:
            cur.execute("SELECT has_table_privilege('make_web', %s, 'SELECT')", (table,))
            grants[table] = bool(cur.fetchone()[0])
        else:
            grants[table] = "table absent"
    out["make_web_grants"] = grants
    conn.rollback()
    conn.close()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def run_inspect(args) -> int:
    conn = _connect()
    cur = conn.cursor()
    out: dict = {"mode": "inspect-tables"}
    for table in TABLES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        if not cur.fetchone()[0]:
            out[table] = "absent"
            continue
        cur.execute(f"SELECT axis, count(*) FROM {table} GROUP BY 1 ORDER BY 1")
        out[table] = {axis: int(n) for axis, n in cur.fetchall()}
    conn.rollback()
    conn.close()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true", help="read-only probes")
    mode.add_argument("--dry-run", action="store_true", help="build + QC in txn, ROLLBACK")
    mode.add_argument("--build", action="store_true", help="build + QC, COMMIT (requires --confirm-db-write)")
    mode.add_argument("--inspect-tables", action="store_true", help="read-only table counts")
    ap.add_argument("--confirm-db-write", action="store_true", help="required with --build")
    ap.add_argument("--corpus-version", help="e.g. c23_final+matstrip (required for dry-run/build)")
    ap.add_argument("--labels", default=str(LABELS_PATH))
    ap.add_argument("--report", default=str(REPORT_PATH))
    ap.add_argument("--with-reclassify", action="store_true",
                    help="run the material reclassify migration first, same transaction")
    ap.add_argument("--with-r4", action="store_true",
                    help="run the R4 axis backfill first, same transaction "
                         "(requires r4_deploy_neon.py DDL applied)")
    ap.add_argument("--client-side", action="store_true",
                    help="force client-side numpy aggregation (no AVG(vector))")
    ap.add_argument("--skip-elements-axis", action="store_true",
                    help="omit the architectural_elements axis")
    args = ap.parse_args()

    if args.discover:
        return run_discover(args)
    if args.inspect_tables:
        return run_inspect(args)
    if args.build and not args.confirm_db_write:
        print("--build requires --confirm-db-write", file=sys.stderr)
        return 2
    if not args.corpus_version:
        print("--corpus-version is required for --dry-run/--build", file=sys.stderr)
        return 2
    return run_build(args)


if __name__ == "__main__":
    sys.exit(main())
