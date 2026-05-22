#!/usr/bin/env python3
"""L4 (statistical + embedding quality) + L5 (cross-field coherence) audit.

Strictly read-only against Neon canonical_v2_buildings (= completeness_c8).
Part of the make_db database quality audit.

Writes data/reports/audit/L4L5.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect, TABLE  # noqa: E402
from core import vocab  # noqa: E402

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

REPORT = ROOT / "data/reports/audit/L4L5.json"
SEED = "make_db-audit-2026-05"
# 5 divisare project ids the L0-a crawler review found with location-text leaked into `name`
DIVISARE_BAD_NAME_IDS = ["315697", "325845", "331218", "335232", "335270"]
REGRESSION_FIXTURES = ["bld_026977", "bld_018178"]


def _rows(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


# --------------------------------------------------------------------------
# L4 — statistical / distributional
# --------------------------------------------------------------------------

def l4_distributions(cur) -> dict:
    out = {}

    # project_year sanity
    rows = _rows(cur, f"""
        SELECT
          COUNT(*) FILTER (WHERE project_year IS NOT NULL),
          COUNT(*) FILTER (WHERE project_year < 1850),
          COUNT(*) FILTER (WHERE project_year > 2027),
          MIN(project_year), MAX(project_year)
        FROM {TABLE}
    """)
    yc, ylo, yhi, ymin, ymax = rows[0]
    out["project_year"] = {"non_null": yc, "below_1850": ylo, "above_2027": yhi,
                           "min": ymin, "max": ymax,
                           "ok": ylo == 0 and yhi == 0}
    out["project_year_decade"] = {str(k): v for k, v in _rows(cur, f"""
        SELECT (project_year/10)*10, COUNT(*) FROM {TABLE}
        WHERE project_year IS NOT NULL GROUP BY 1 ORDER BY 1
    """)}

    # vocab skew per identity_source: dominant style share
    skew = {}
    for (src,) in _rows(cur, f"SELECT DISTINCT identity_source FROM {TABLE}"):
        rows = _rows(cur, f"""
            SELECT style, COUNT(*) FROM {TABLE}
            WHERE identity_source IS NOT DISTINCT FROM %s GROUP BY 1 ORDER BY 2 DESC
        """, (src,))
        total = sum(c for _, c in rows)
        if total:
            top_style, top_c = rows[0]
            skew[str(src)] = {"rows": total, "top_style": top_style,
                              "top_style_share": round(top_c / total, 4),
                              "skew_flag": top_c / total > 0.70}
    out["style_skew_by_source"] = skew

    # program skew overall
    prog = _rows(cur, f"SELECT program, COUNT(*) FROM {TABLE} GROUP BY 1 ORDER BY 2 DESC")
    ptot = sum(c for _, c in prog)
    out["program_distribution"] = {p: c for p, c in prog}
    out["program_top_share"] = round(prog[0][1] / ptot, 4) if prog else 0

    # architect fan-out
    out["architects_per_building"] = {str(k): v for k, v in _rows(cur, f"""
        SELECT cardinality(architect_canonical_ids), COUNT(*) FROM {TABLE}
        GROUP BY 1 ORDER BY 1
    """)}
    out["buildings_with_ge5_architects"] = _rows(cur, f"""
        SELECT COUNT(*) FROM {TABLE} WHERE cardinality(architect_canonical_ids) >= 5
    """)[0][0]
    top_arch = _rows(cur, f"""
        SELECT aid, COUNT(*) c FROM {TABLE}, unnest(architect_canonical_ids) aid
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """)
    out["top_architect_fanout"] = [{"architect_id": a, "buildings": c} for a, c in top_arch]
    return out


def l4_embedding(conn) -> dict:
    out = {"numpy_available": HAVE_NUMPY}
    cur = conn.cursor()

    if HAVE_NUMPY:
        cur.execute(f"SELECT embedding::text FROM {TABLE} "
                    f"ORDER BY md5(canonical_bld_id || %s) LIMIT 4000", (SEED,))
        vecs = [json.loads(t) for (t,) in cur.fetchall()]
        arr = np.array(vecs, dtype=np.float64)
        norms = np.linalg.norm(arr, axis=1)
        unit = arr / norms[:, None]
        sims = unit @ unit.T
        iu = np.triu_indices(len(arr), k=1)
        pair = sims[iu]
        out["collapse_check"] = {
            "sample": len(arr),
            "mean_pairwise_cosine": round(float(pair.mean()), 5),
            "p99_pairwise_cosine": round(float(np.percentile(pair, 99)), 5),
            "max_pairwise_cosine": round(float(pair.max()), 5),
            "healthy": float(pair.mean()) < 0.6,
        }
        out["norm_stats"] = {
            "min": round(float(norms.min()), 5), "max": round(float(norms.max()), 5),
            "mean": round(float(norms.mean()), 5), "std": round(float(norms.std()), 5),
        }

    # near-duplicate detection via HNSW: each row's single nearest neighbour
    cur.execute(f"""
        SELECT a_cid, b_cid, cos FROM (
          SELECT a.canonical_bld_id AS a_cid, b.canonical_bld_id AS b_cid,
                 1 - (a.embedding <=> b.embedding) AS cos
          FROM {TABLE} a
          CROSS JOIN LATERAL (
            SELECT canonical_bld_id, embedding FROM {TABLE}
            WHERE canonical_bld_id <> a.canonical_bld_id
            ORDER BY a.embedding <=> embedding LIMIT 1
          ) b
        ) s WHERE cos > 0.95
        ORDER BY cos DESC
    """)
    raw = cur.fetchall()
    seen = set()
    pairs = []
    for a, b, cos in raw:
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        pairs.append({"a": a, "b": b, "cosine": round(float(cos), 5)})
    out["near_duplicate_pairs"] = {
        "threshold": 0.95,
        "pair_count": len(pairs),
        "ge_0_98": sum(1 for p in pairs if p["cosine"] >= 0.98),
        "ge_0_99": sum(1 for p in pairs if p["cosine"] >= 0.99),
        "samples": pairs[:60],
        "all_cids": sorted({c for p in pairs for c in (p["a"], p["b"])}),
    }
    return out


# --------------------------------------------------------------------------
# L5 — cross-field coherence
# --------------------------------------------------------------------------

def l5_coherence(cur) -> dict:
    out = {}
    style_vocab = {str(v) for v in vocab.STYLE}
    tone_vocab = {str(v) for v in vocab.COLOR_TONE}

    # image_derived key introspection
    keys = sorted({k for (k,) in _rows(cur,
        f"SELECT DISTINCT jsonb_object_keys(image_derived) FROM {TABLE}")})
    out["image_derived_keys"] = keys
    style_key = "style" if "style" in keys else ("style_image" if "style_image" in keys else None)
    tone_key = "color_tone" if "color_tone" in keys else ("color_tone_image" if "color_tone_image" in keys else None)

    # image_derived vocab conformance (D-2 output)
    if style_key:
        sd = _rows(cur, f"SELECT image_derived->>%s, COUNT(*) FROM {TABLE} GROUP BY 1 ORDER BY 2 DESC", (style_key,))
        bad = {(v or "<null>"): c for v, c in sd if v is not None and v not in style_vocab}
        out["image_derived_style"] = {
            "key": style_key,
            "distinct_values": len(sd),
            "all_values": {(v or "<null>"): c for v, c in sd},
            "out_of_vocab_values": bad,
            "out_of_vocab_row_count": sum(bad.values()),
        }
    if tone_key:
        td = _rows(cur, f"SELECT image_derived->>%s, COUNT(*) FROM {TABLE} GROUP BY 1 ORDER BY 2 DESC", (tone_key,))
        bad = {(v or "<null>"): c for v, c in td if v is not None and v not in tone_vocab}
        out["image_derived_color_tone"] = {
            "key": tone_key,
            "distinct_values": len(td),
            "all_values": {(v or "<null>"): c for v, c in td},
            "out_of_vocab_values": bad,
            "out_of_vocab_row_count": sum(bad.values()),
        }

    # D-1 (top-level) vs D-2 (image_derived) disagreement
    if style_key:
        rows = _rows(cur, f"""
            SELECT
              COUNT(*) FILTER (WHERE image_derived->>%s IS NOT NULL),
              COUNT(*) FILTER (WHERE image_derived->>%s IS NOT NULL
                                 AND image_derived->>%s = style),
              COUNT(*) FILTER (WHERE image_derived->>%s IS NOT NULL
                                 AND image_derived->>%s <> style
                                 AND image_derived->>%s = ANY(%s))
            FROM {TABLE}
        """, (style_key, style_key, style_key, style_key, style_key, style_key, sorted(style_vocab)))
        have, agree, disagree_invocab = rows[0]
        out["d1_d2_style_agreement"] = {
            "rows_with_image_style": have,
            "agree_with_top_level": agree,
            "disagree_both_in_vocab": disagree_invocab,
            "agreement_rate_among_invocab": round(agree / (agree + disagree_invocab), 4)
            if (agree + disagree_invocab) else None,
        }

    # year coherence: 4-digit year in name contradicting project_year
    out["year_in_name_conflict"] = _rows(cur, f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE project_year IS NOT NULL
          AND name ~ '\\y(18|19|20)\\d\\d\\y'
          AND substring(name from '(18|19|20)\\d\\d')::int <> project_year
    """)[0][0]

    # publishability coherence
    rows = _rows(cur, f"""
        SELECT
          COUNT(*) FILTER (WHERE is_publishable AND (display_cover_url IS NULL OR all_images = '[]'::jsonb)),
          COUNT(*) FILTER (WHERE NOT is_publishable AND display_cover_url IS NOT NULL AND all_images <> '[]'::jsonb),
          COUNT(*) FILTER (WHERE needs_image_derived_backfill AND image_derived <> '{{}}'::jsonb)
        FROM {TABLE}
    """)
    pub_bad, nonpub_hasimg, backfill_incoherent = rows[0]
    out["publishability_coherence"] = {
        "publishable_but_no_cover_or_images": pub_bad,
        "nonpublishable_but_has_cover_and_images": nonpub_hasimg,
        "needs_backfill_but_image_derived_populated": backfill_incoherent,
        "ok": pub_bad == 0,
    }

    # n_sources vs source_refs cardinality; tier vs n_sources
    rows = _rows(cur, f"""
        SELECT
          COUNT(*) FILTER (WHERE n_sources <> (SELECT COUNT(*) FROM jsonb_object_keys(source_refs))),
          COUNT(*) FILTER (WHERE confidence_tier = 'T1' AND n_sources < 3),
          COUNT(*) FILTER (WHERE confidence_tier = 'T2' AND n_sources <> 2),
          COUNT(*) FILTER (WHERE confidence_tier = 'T3' AND n_sources <> 1)
        FROM {TABLE}
    """)
    nsrc_mismatch, t1_bad, t2_bad, t3_bad = rows[0]
    out["source_count_coherence"] = {
        "n_sources_ne_source_refs_keys": nsrc_mismatch,
        "T1_with_lt3_sources": t1_bad,
        "T2_with_ne2_sources": t2_bad,
        "T3_with_ne1_sources": t3_bad,
        "ok": all(v == 0 for v in (nsrc_mismatch, t1_bad, t2_bad, t3_bad)),
    }

    # material_visual empties
    out["material_visual_empty"] = _rows(cur, f"""
        SELECT COUNT(*) FROM {TABLE} WHERE cardinality(material_visual) = 0
    """)[0][0]

    # Divisare leaked-name bug: did any of the 5 bad divisare ids reach canonical?
    leaked = _rows(cur, f"""
        SELECT canonical_bld_id, name, source_refs->'divisare'
        FROM {TABLE} WHERE source_refs->'divisare' ?| %s
    """, (DIVISARE_BAD_NAME_IDS,))
    out["divisare_leaked_name_bug"] = {
        "bad_divisare_ids_checked": DIVISARE_BAD_NAME_IDS,
        "reached_canonical": [{"canonical_bld_id": c, "name": n, "divisare_ids": d}
                              for c, n, d in leaked],
        "count": len(leaked),
    }
    # name looks like a leaked "City - Country" location string
    out["name_looks_like_location"] = _rows(cur, f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE name ~ '^[A-Z][A-Za-z .]+ - [A-Z][A-Za-z ]+$'
          AND location_city IS NULL AND location_country IS NULL
    """)[0][0]

    # regression fixtures
    fix = []
    for cid in REGRESSION_FIXTURES:
        r = _rows(cur, f"""
            SELECT canonical_bld_id, name, location_country, location_city,
                   project_year, architect_names, n_sources, confidence_tier
            FROM {TABLE} WHERE canonical_bld_id = %s
        """, (cid,))
        fix.append(dict(zip(
            ("canonical_bld_id", "name", "location_country", "location_city",
             "project_year", "architect_names", "n_sources", "confidence_tier"),
            r[0])) if r else {"canonical_bld_id": cid, "status": "NOT FOUND"})
    out["regression_fixtures"] = fix

    # 36 rows flagged by qc_strict as missing image_derived style
    if style_key:
        out["image_derived_style_missing"] = _rows(cur, f"""
            SELECT COUNT(*) FROM {TABLE}
            WHERE image_derived->>%s IS NULL OR image_derived->>%s = ''
        """, (style_key, style_key))[0][0]
    return out


def main() -> int:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(updated_at) FROM {TABLE}")
            max_updated = str(cur.fetchone()[0])
            l4_dist = l4_distributions(cur)
            l5 = l5_coherence(cur)
        l4_emb = l4_embedding(conn)
    finally:
        conn.rollback()
        conn.close()

    findings: list[str] = []
    if not l4_dist["project_year"]["ok"]:
        findings.append(f"WARN: project_year out of range — below_1850={l4_dist['project_year']['below_1850']}, "
                        f"above_2027={l4_dist['project_year']['above_2027']}")
    skewed = [s for s, v in l4_dist["style_skew_by_source"].items() if v["skew_flag"]]
    if skewed:
        findings.append(f"WARN: style distribution >70% single value for source(s): {skewed}")
    if HAVE_NUMPY and not l4_emb.get("collapse_check", {}).get("healthy", True):
        findings.append(f"WARN: embedding space may be collapsed — mean pairwise cosine "
                        f"{l4_emb['collapse_check']['mean_pairwise_cosine']}")
    nd = l4_emb["near_duplicate_pairs"]
    if nd["ge_0_99"]:
        findings.append(f"WARN: {nd['ge_0_99']} embedding pairs cosine>=0.99 — possible duplicate buildings")
    iv = l5.get("image_derived_style", {})
    if iv.get("out_of_vocab_row_count"):
        findings.append(f"FAIL(image_derived): {iv['out_of_vocab_row_count']} rows have image_derived style "
                        f"OUT OF VOCAB — values {list(iv['out_of_vocab_values'])[:10]}")
    tv = l5.get("image_derived_color_tone", {})
    if tv.get("out_of_vocab_row_count"):
        findings.append(f"FAIL(image_derived): {tv['out_of_vocab_row_count']} rows have image_derived color_tone "
                        f"OUT OF VOCAB — values {list(tv['out_of_vocab_values'])[:10]}")
    if not l5["publishability_coherence"]["ok"]:
        findings.append(f"WARN: publishability incoherence {l5['publishability_coherence']}")
    if not l5["source_count_coherence"]["ok"]:
        findings.append(f"WARN: source-count/tier incoherence {l5['source_count_coherence']}")
    if l5["divisare_leaked_name_bug"]["count"]:
        findings.append(f"WARN: divisare leaked-name bug reached canonical — "
                        f"{l5['divisare_leaked_name_bug']['count']} row(s)")
    if l5["year_in_name_conflict"]:
        findings.append(f"INFO: {l5['year_in_name_conflict']} rows have a year in name differing from project_year")

    report = {
        "layer": "L4+L5",
        "table": TABLE,
        "neon_max_updated_at": max_updated,
        "findings": findings,
        "L4_distributions": l4_dist,
        "L4_embedding": l4_emb,
        "L5_coherence": l5,
        "writes": "none; read-only",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"findings": findings,
                       "L4_distributions": l4_dist,
                       "L4_embedding": {k: v for k, v in l4_emb.items() if k != "near_duplicate_pairs"},
                       "near_duplicate_summary": {k: v for k, v in l4_emb["near_duplicate_pairs"].items()
                                                  if k not in ("samples", "all_cids")},
                       "L5_coherence": {k: v for k, v in l5.items()
                                        if k not in ("image_derived_style", "image_derived_color_tone")},
                       "image_derived_style_summary": {k: v for k, v in l5.get("image_derived_style", {}).items()
                                                       if k != "all_values"},
                       "image_derived_color_tone_summary": {k: v for k, v in l5.get("image_derived_color_tone", {}).items()
                                                            if k != "all_values"}},
                      ensure_ascii=False, indent=2, default=str))
    print(f"report: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
