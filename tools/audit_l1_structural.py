#!/usr/bin/env python3
"""L1 audit — structural integrity of Neon canonical_v2_buildings + Neon<->JSON parity.

Strictly read-only. Part of the make_db database quality audit.

Baseline artifact: completeness_c8 (Neon = c8 as of 2026-05-22; the pipeline
applied C8 — a 27-row web/LLM location/year backfill — during planning).

Checks:
  * aggregate SELECTs: row count, PK uniqueness, CHECK violations, embedding
    dimension, zero vectors, null rates, JSONB shape, vocab conformance,
    key distributions.
  * full-population row-equivalence: every one of the 39,776 rows is diffed
    Neon vs the c8 embedded JSON on all non-embedding columns (deep, float
    tolerant). Embedding parity is checked on a deterministic 2000-row sample
    by cosine (tolerant of the float32 / 8-decimal round-trip).

Writes data/reports/audit/L1_structural.json.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect, COLUMNS, TABLE  # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings, map_row  # noqa: E402
from core import vocab  # noqa: E402

C8 = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8.json"
REPORT = ROOT / "data/reports/audit/L1_structural.json"
SAMPLE_N = 2000
SEED = "make_db-audit-2026-05"
EMB_DIM = 384
EMB_COS_THRESHOLD = 0.999999
COLS_NOEMB = [c for c in COLUMNS if c != "embedding"]


def _vocab_list(name: str) -> list[str]:
    return sorted(str(v) for v in getattr(vocab, name))


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _canon(x: Any) -> Any:
    """Float-tolerant canonical form (jsonb numbers may reformat on round-trip)."""
    if isinstance(x, bool):
        return x
    if isinstance(x, float):
        return round(x, 6)
    if isinstance(x, dict):
        return {k: _canon(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_canon(v) for v in x]
    return x


def _hash_nonemb(d: dict[str, Any]) -> str:
    payload = {k: _canon(d.get(k)) for k in COLS_NOEMB}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def run_aggregates(cur) -> dict[str, Any]:
    out: dict[str, Any] = {}

    cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT canonical_bld_id) FROM {TABLE}")
    total, distinct_pk = cur.fetchone()
    out["count"] = {"total_rows": total, "distinct_pk": distinct_pk,
                    "ok": total == distinct_pk == 39776}

    cur.execute(f"""
        SELECT
          COUNT(*) FILTER (WHERE confidence_tier NOT IN ('T1','T2','T3')),
          COUNT(*) FILTER (WHERE n_sources < 1),
          COUNT(*) FILTER (WHERE name IS NULL OR name = ''),
          COUNT(*) FILTER (WHERE program IS NULL OR style IS NULL
                              OR color_tone IS NULL OR atmosphere IS NULL
                              OR visual_description IS NULL)
        FROM {TABLE}
    """)
    bad_tier, bad_nsrc, bad_name, null_notnull = cur.fetchone()
    out["check_violations"] = {
        "bad_confidence_tier": bad_tier, "n_sources_lt_1": bad_nsrc,
        "empty_name": bad_name, "null_in_notnull_text_cols": null_notnull,
        "ok": all(v == 0 for v in (bad_tier, bad_nsrc, bad_name, null_notnull)),
    }

    zero_vec = "[" + ",".join(["0"] * EMB_DIM) + "]"
    cur.execute(f"""
        SELECT COUNT(*),
          COUNT(*) FILTER (WHERE vector_dims(embedding) = {EMB_DIM}),
          MIN(vector_dims(embedding)), MAX(vector_dims(embedding)),
          COUNT(*) FILTER (WHERE embedding = %s::vector)
        FROM {TABLE}
    """, (zero_vec,))
    n, dim_ok, dmin, dmax, zeros = cur.fetchone()
    out["embedding"] = {"rows": n, "dim_384": dim_ok, "min_dim": dmin,
                        "max_dim": dmax, "zero_vectors": zeros,
                        "ok": dim_ok == n and zeros == 0}

    cur.execute(f"""
        SELECT
          COUNT(*) FILTER (WHERE location_country IS NULL),
          COUNT(*) FILTER (WHERE location_country = ''),
          COUNT(*) FILTER (WHERE location_city IS NULL),
          COUNT(*) FILTER (WHERE location_city = ''),
          COUNT(*) FILTER (WHERE project_year IS NULL),
          COUNT(*) FILTER (WHERE display_cover_url IS NULL),
          COUNT(*) FILTER (WHERE architects_text IS NULL OR architects_text = ''),
          COUNT(*) FILTER (WHERE cardinality(architect_canonical_ids) = 0),
          COUNT(*) FILTER (WHERE cardinality(architect_names) = 0)
        FROM {TABLE}
    """)
    (nc, ec, nci, eci, ny, ncov, nat, nac, nan) = cur.fetchone()
    out["null_rates"] = {
        "location_country_null": nc, "location_country_empty": ec,
        "location_city_null": nci, "location_city_empty": eci,
        "project_year_null": ny, "display_cover_url_null": ncov,
        "architects_text_missing": nat,
        "empty_architect_canonical_ids": nac, "empty_architect_names": nan,
    }

    cur.execute(f"""
        SELECT
          COUNT(*) FILTER (WHERE jsonb_typeof(source_refs) <> 'object'),
          COUNT(*) FILTER (WHERE jsonb_typeof(covers_by_type) <> 'object'),
          COUNT(*) FILTER (WHERE jsonb_typeof(all_images) <> 'array'),
          COUNT(*) FILTER (WHERE jsonb_typeof(image_derived) <> 'object'),
          COUNT(*) FILTER (WHERE source_refs = '{{}}'::jsonb),
          COUNT(*) FILTER (WHERE NOT (covers_by_type ?& ARRAY['exterior','interior','drawing','aerial','detail']))
        FROM {TABLE}
    """)
    bsr, bcv, bai, bid, esr, cov5 = cur.fetchone()
    out["jsonb_shape"] = {
        "source_refs_not_object": bsr, "covers_not_object": bcv,
        "all_images_not_array": bai, "image_derived_not_object": bid,
        "empty_source_refs": esr, "covers_missing_5_keys": cov5,
        "ok": all(v == 0 for v in (bsr, bcv, bai, bid, esr)),
    }

    progs, styles, tones, atmos = (_vocab_list("PROGRAM"), _vocab_list("STYLE"),
                                   _vocab_list("COLOR_TONE"), _vocab_list("ATMOSPHERE"))
    cur.execute(f"""
        SELECT
          COUNT(*) FILTER (WHERE program    <> ALL(%s)),
          COUNT(*) FILTER (WHERE style      <> ALL(%s)),
          COUNT(*) FILTER (WHERE color_tone <> ALL(%s)),
          COUNT(*) FILTER (WHERE atmosphere <> ALL(%s))
        FROM {TABLE}
    """, (progs, styles, tones, atmos))
    bp, bs, bc, ba = cur.fetchone()
    out["vocab_conformance"] = {
        "bad_program": bp, "bad_style": bs, "bad_color_tone": bc,
        "bad_atmosphere": ba, "vocab_version": getattr(vocab, "VOCAB_VERSION", "?"),
        "ok": all(v == 0 for v in (bp, bs, bc, ba)),
    }

    dist: dict[str, Any] = {}
    for col in ("confidence_tier", "program", "style", "color_tone",
                "atmosphere", "is_publishable", "n_sources", "identity_source"):
        cur.execute(f"SELECT {col}, COUNT(*) FROM {TABLE} GROUP BY 1 ORDER BY 2 DESC")
        dist[col] = {str(k): v for k, v in cur.fetchall()}
    out["distributions"] = dist
    return out


def run_equivalence(conn) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute(f"SELECT canonical_bld_id FROM {TABLE} "
                f"ORDER BY md5(canonical_bld_id || %s) LIMIT %s", (SEED, SAMPLE_N))
    sample_pks = {r[0] for r in cur.fetchall()}

    # Pass A — stream the c8 embedded JSON.
    c8_hash: dict[str, str] = {}
    c8_emb: dict[str, list] = {}
    for obj in iter_buildings(C8):
        m = map_row(obj)
        cid = m["canonical_bld_id"]
        if cid in sample_pks:
            c8_emb[cid] = m.get("embedding")
        c8_hash[cid] = _hash_nonemb(m)

    # Pass B — full scan of Neon (server-side cursor), non-embedding columns.
    select_cols = ", ".join(COLS_NOEMB)
    scan = conn.cursor(name="audit_l1_fullscan")
    scan.itersize = 2000
    scan.execute(f"SELECT {select_cols} FROM {TABLE}")
    neon_seen: set[str] = set()
    mismatches: list[dict[str, Any]] = []
    in_neon_not_c8: list[str] = []
    for rec in scan:
        row = dict(zip(COLS_NOEMB, rec))
        cid = row["canonical_bld_id"]
        neon_seen.add(cid)
        if cid not in c8_hash:
            in_neon_not_c8.append(cid)
            continue
        if _hash_nonemb(row) != c8_hash[cid]:
            mismatches.append({"canonical_bld_id": cid, "issue": "nonembedding_field_mismatch"})
    scan.close()
    in_c8_not_neon = [cid for cid in c8_hash if cid not in neon_seen]

    # Embedding parity on the 2000-row sample (cosine; float32 round-trip tolerant).
    cur.execute(f"SELECT canonical_bld_id, embedding::text FROM {TABLE} "
                f"WHERE canonical_bld_id = ANY(%s)", (list(sample_pks),))
    emb_min_cos = 1.0
    emb_below = 0
    emb_checked = 0
    for cid, txt in cur.fetchall():
        nv = json.loads(txt) if txt else None
        jv = c8_emb.get(cid)
        if not isinstance(nv, list) or not isinstance(jv, list):
            emb_below += 1
            continue
        emb_checked += 1
        cos = _cosine(nv, jv)
        emb_min_cos = min(emb_min_cos, cos)
        if cos < EMB_COS_THRESHOLD:
            emb_below += 1

    return {
        "c8_rows": len(c8_hash),
        "neon_rows": len(neon_seen),
        "nonembedding_full_population": True,
        "nonembedding_mismatches": len(mismatches),
        "mismatch_samples": mismatches[:40],
        "in_neon_not_c8": in_neon_not_c8[:40],
        "in_c8_not_neon": in_c8_not_neon[:40],
        "in_neon_not_c8_count": len(in_neon_not_c8),
        "in_c8_not_neon_count": len(in_c8_not_neon),
        "embedding_sample": SAMPLE_N,
        "embedding_checked": emb_checked,
        "embedding_min_cosine": round(emb_min_cos, 10),
        "embedding_below_threshold": emb_below,
        "ok": (len(mismatches) == 0 and not in_neon_not_c8 and not in_c8_not_neon
               and emb_below == 0 and len(c8_hash) == len(neon_seen) == 39776),
    }


def main() -> int:
    if not C8.exists():
        print(f"FATAL: c8 artifact not found: {C8}", file=sys.stderr)
        return 2

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(updated_at) FROM {TABLE}")
            max_updated = cur.fetchone()[0]
            aggregates = run_aggregates(cur)
        equivalence = run_equivalence(conn)
    finally:
        conn.rollback()
        conn.close()

    findings: list[str] = []
    if not aggregates["count"]["ok"]:
        findings.append("FAIL: row count / PK uniqueness off expected 39,776")
    if not aggregates["check_violations"]["ok"]:
        findings.append(f"FAIL: CHECK-constraint violations {aggregates['check_violations']}")
    if not aggregates["embedding"]["ok"]:
        findings.append(f"FAIL: embedding dimension/zero issue {aggregates['embedding']}")
    if not aggregates["jsonb_shape"]["ok"]:
        findings.append(f"FAIL: JSONB shape issue {aggregates['jsonb_shape']}")
    if not aggregates["vocab_conformance"]["ok"]:
        findings.append(f"WARN: top-level vocab non-conformance {aggregates['vocab_conformance']}")
    nr = aggregates["null_rates"]
    if nr["location_country_empty"] or nr["location_city_empty"]:
        findings.append(f"WARN: empty-string used for missing location "
                        f"(country={nr['location_country_empty']}, city={nr['location_city_empty']}) "
                        f"— inconsistent with NULL representation")
    if not equivalence["ok"]:
        findings.append(f"FAIL: Neon<->c8 parity — nonembedding mismatches={equivalence['nonembedding_mismatches']}, "
                        f"in_neon_not_c8={equivalence['in_neon_not_c8_count']}, "
                        f"in_c8_not_neon={equivalence['in_c8_not_neon_count']}, "
                        f"embedding_below_threshold={equivalence['embedding_below_threshold']}")

    hard_fail = any(f.startswith("FAIL") for f in findings)
    verdict = "FAIL" if hard_fail else ("WARN" if findings else "PASS")

    report = {
        "layer": "L1",
        "table": TABLE,
        "baseline_artifact": str(C8),
        "neon_max_updated_at": str(max_updated),
        "verdict": verdict,
        "findings": findings,
        "aggregates": aggregates,
        "row_equivalence": equivalence,
        "writes": "none; read-only",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(json.dumps({
        "verdict": verdict, "findings": findings,
        "neon_max_updated_at": str(max_updated),
        "count": aggregates["count"],
        "check_violations": aggregates["check_violations"],
        "embedding": aggregates["embedding"],
        "jsonb_shape": aggregates["jsonb_shape"],
        "vocab_conformance": aggregates["vocab_conformance"],
        "null_rates": aggregates["null_rates"],
        "row_equivalence": equivalence,
    }, ensure_ascii=False, indent=2, default=str))
    print(f"report: {REPORT}")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
