#!/usr/bin/env python3
"""Full-census deterministic audit of live Neon `archi_data` (read-only).

Backbone of the 2026-Q2 census audit (plan: full_census_audit_2026Q2). Runs the
FREE deterministic layers against the live production tables — never the stale
local artifacts. Strictly SELECT-only (sets the session read-only).

Layers (all 100% coverage, no LLM):
  diag  quick sanity probes (R4 populated, updated_at, display_cover, mat noise)
  l1    structural/schema: PK, CHECK domains, NULL census, embedding health
  l2    vocab conformance vs core.vocab (11 controlled axes)
  l3    referential integrity (architect<->building, source_refs, tag tables)
  l4    make_web contract (recompute is_publishable / is_recommendable)
  l6    cross-field coherence (era<->year, year_kind, tier<->n_sources)
  l7a   label triangulation (D1<->D2, typology<->program)
  l7b   vagueness / over-generality scoring

Paid layers (L5, L7c vision, L7d web) run via a separate tiered Workflow.

Usage:
  python3 tools/audit_full_census.py --section diag
  python3 tools/audit_full_census.py --section all --out data/reports/full_census_audit_2026Q2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2.extras  # noqa: E402

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from core import vocab  # noqa: E402

BLD = "canonical_v2_buildings"
ARCH = "canonical_v2_architects"
SOURCES = ("divisare", "architizer", "archello", "metalocus")

# era CASE replicating tools/r4_axis_merge.era_from_year (year < upper -> bucket)
ERA_CASE = """CASE
  WHEN project_year IS NULL THEN NULL
  WHEN project_year < 1900 THEN 'Pre-1900'
  WHEN project_year < 1945 THEN '1900-1945'
  WHEN project_year < 1980 THEN '1945-1980'
  WHEN project_year < 2000 THEN '1980-2000'
  WHEN project_year < 2015 THEN '2000-2015'
  ELSE '2015+' END"""

# typology_primary -> ACCEPTABLE programs (sets, not single value). program is a
# coarse 14-value axis; typology is finer (35). A mismatch is a HARD contradiction
# only when the stored program is outside this acceptable set (so legit coarsening
# like Apartment->Mixed Use or Library->Public is NOT flagged).
TYP_PROGRAM_OK = {
    "House": ["Housing", "Mixed Use"], "Apartment": ["Housing", "Mixed Use"],
    "Housing": ["Housing", "Mixed Use"], "Student Housing": ["Housing", "Mixed Use"],
    "Care Home": ["Housing", "Healthcare", "Mixed Use"],
    "Office": ["Office", "Mixed Use"],
    "Museum": ["Museum", "Public", "Mixed Use"], "Gallery": ["Museum", "Public", "Mixed Use"],
    "Library": ["Education", "Public", "Mixed Use"],
    "School": ["Education", "Public", "Mixed Use"],
    "University": ["Education", "Public", "Mixed Use"],
    "Kindergarten": ["Education", "Public", "Mixed Use"],
    "Hospital": ["Healthcare", "Public"],
    "Religious Building": ["Religion", "Public"],
    "Sports Centre": ["Sports", "Public", "Mixed Use"],
    "Stadium": ["Sports", "Public", "Mixed Use"],
    "Airport": ["Transport", "Infrastructure", "Public"],
    "Train Station": ["Transport", "Infrastructure", "Public"],
    "Car Park": ["Transport", "Infrastructure", "Public"],
    "Hotel": ["Hospitality", "Mixed Use"], "Restaurant": ["Hospitality", "Mixed Use"],
    "Civic Building": ["Public", "Office", "Mixed Use"],
    "Bank": ["Public", "Office", "Mixed Use"],
    "Industrial": ["Infrastructure", "Office", "Mixed Use", "Other"],
    "Warehouse": ["Infrastructure", "Office", "Mixed Use", "Other"],
    "Bridge": ["Infrastructure", "Transport", "Landscape"],
    "Park": ["Landscape", "Public"],
    "Mixed Use": ["Mixed Use"],
}


def _q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return [dict(r) for r in cur.fetchall()]


def _one(cur, sql, args=None):
    rows = _q(cur, sql, args)
    return rows[0] if rows else {}


def _safe(cur, fn):
    try:
        return fn(cur)
    except Exception as e:  # noqa: BLE001
        cur.connection.rollback()
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
def section_diag(cur) -> dict:
    out = {}
    out["counts"] = _one(cur, f"""
        SELECT count(*) total,
               count(*) FILTER (WHERE is_publishable) publishable,
               count(*) FILTER (WHERE NOT is_publishable) nonpublishable
        FROM {BLD}""")
    for scope, where in (("all", ""), ("publishable", "WHERE is_publishable")):
        out[f"r4_population_{scope}"] = _one(cur, f"""
            SELECT count(*) FILTER (WHERE era IS NOT NULL) era,
                   count(*) FILTER (WHERE scale IS NOT NULL) scale,
                   count(*) FILTER (WHERE structural_system IS NOT NULL) structural_system,
                   count(*) FILTER (WHERE roof_type IS NOT NULL) roof_type,
                   count(*) FILTER (WHERE facade_pattern IS NOT NULL) facade_pattern,
                   count(*) total FROM {BLD} {where}""")
    out["updated_at"] = _one(cur, f"""
        SELECT min(updated_at) min, max(updated_at) max,
               count(DISTINCT updated_at) distinct_ts FROM {BLD}""")
    return out


# --------------------------------------------------------------------------- #
def section_l1(cur) -> dict:
    out = {}
    out["pk"] = _one(cur, f"""
        SELECT count(*) total, count(DISTINCT canonical_bld_id) distinct_pk,
               count(*) - count(DISTINCT canonical_bld_id) dup_pk FROM {BLD}""")

    # CHECK-domain values actually present (should never exceed declared domains)
    domains = {
        "confidence_tier": ["T1", "T2", "T3"],
        "year_kind": ["completed", "future", "unknown"],
        "era": list(vocab_era()),
        "scale": sorted(vocab.SCALE),
        "structural_system": sorted(vocab.STRUCTURAL_SYSTEM),
        "roof_type": sorted(vocab.ROOF_TYPE),
        "facade_pattern": sorted(vocab.FACADE_PATTERN),
    }
    dom = {}
    for col, allowed in domains.items():
        bad = _q(cur, f"""
            SELECT {col} val, count(*) n FROM {BLD}
            WHERE {col} IS NOT NULL AND NOT ({col} = ANY(%s))
            GROUP BY {col} ORDER BY n DESC LIMIT 20""", (allowed,))
        dom[col] = {"out_of_domain_values": bad}
    out["check_domains"] = dom

    # NULL + empty census per column
    cols = _q(cur, """
        SELECT column_name, data_type, udt_name FROM information_schema.columns
        WHERE table_name = %s ORDER BY ordinal_position""", (BLD,))
    parts, names = [], []
    for c in cols:
        name, dt, udt = c["column_name"], c["data_type"], c["udt_name"]
        parts.append(f'count(*) FILTER (WHERE "{name}" IS NULL) AS "{name}__null"')
        names.append(name)
        if dt == "text":
            parts.append(f"count(*) FILTER (WHERE \"{name}\" = '') AS \"{name}__empty\"")
        elif dt == "ARRAY":
            parts.append(f'count(*) FILTER (WHERE cardinality("{name}") = 0) AS "{name}__empty"')
        elif dt == "jsonb":
            parts.append(f"count(*) FILTER (WHERE \"{name}\" IN ('{{}}'::jsonb,'[]'::jsonb,'null'::jsonb)) AS \"{name}__empty\"")
    row = _one(cur, f"SELECT count(*) AS __total, {', '.join(parts)} FROM {BLD}")
    total = row["__total"]
    census = {}
    for name in names:
        entry = {"null": row.get(f"{name}__null", 0)}
        if f"{name}__empty" in row:
            entry["empty"] = row[f"{name}__empty"]
        entry["null_pct"] = round(100 * entry["null"] / total, 2)
        census[name] = entry
    out["null_census"] = census

    # embedding health. NOTE: building embeddings are intentionally RAW (not
    # L2-normalized) per the make_web contract (MAKEDB_ALGO_SUPPORT_RESPONSE.md);
    # make_web uses cosine <=>. So we only assert dim + non-degenerate + sane norm
    # magnitude consistency. norm = sqrt(-(v <#> v)).
    out["embedding"] = _one(cur, f"""
        SELECT count(*) total,
          count(*) FILTER (WHERE vector_dims(embedding) <> 384) bad_dim,
          count(*) FILTER (WHERE embedding <#> embedding = 0) zero_vec,
          round(min(sqrt(-(embedding <#> embedding)))::numeric, 4) norm_min,
          round(max(sqrt(-(embedding <#> embedding)))::numeric, 4) norm_max,
          round(avg(sqrt(-(embedding <#> embedding)))::numeric, 4) norm_avg,
          count(*) FILTER (WHERE abs(1 - sqrt(-(embedding <#> embedding))) <= 1e-3) unit_norm
        FROM {BLD}""")
    return out


def vocab_era():
    from tools.r4_axis_merge import ERA_VALUES
    return ERA_VALUES


# --------------------------------------------------------------------------- #
def section_l2(cur) -> dict:
    out = {}
    scalar = {
        "program": sorted(vocab.PROGRAM), "style": sorted(vocab.STYLE),
        "color_tone": sorted(vocab.COLOR_TONE), "atmosphere": sorted(vocab.ATMOSPHERE),
    }
    for col, allowed in scalar.items():
        out[col] = {"oov": _q(cur, f"""
            SELECT {col} val, count(*) n FROM {BLD}
            WHERE {col} IS NOT NULL AND NOT ({col} = ANY(%s))
            GROUP BY {col} ORDER BY n DESC LIMIT 20""", (allowed,))}
    # nullable scalar R4 + typology_primary
    nullable = {
        "typology_primary": sorted(vocab.TYPOLOGY), "scale": sorted(vocab.SCALE),
        "structural_system": sorted(vocab.STRUCTURAL_SYSTEM),
        "roof_type": sorted(vocab.ROOF_TYPE), "facade_pattern": sorted(vocab.FACADE_PATTERN),
        "era": list(vocab_era()),
    }
    for col, allowed in nullable.items():
        out[col] = {"oov": _q(cur, f"""
            SELECT {col} val, count(*) n FROM {BLD}
            WHERE {col} IS NOT NULL AND NOT ({col} = ANY(%s))
            GROUP BY {col} ORDER BY n DESC LIMIT 20""", (allowed,))}
    # array axes
    out["typology_tags"] = {"oov": _q(cur, f"""
        SELECT t val, count(*) n FROM {BLD}, unnest(typology_tags) t
        WHERE NOT (t = ANY(%s)) GROUP BY t ORDER BY n DESC LIMIT 20""",
        (sorted(vocab.TYPOLOGY),))}
    out["architectural_elements"] = {"oov": _q(cur, f"""
        SELECT t val, count(*) n FROM {BLD}, unnest(architectural_elements) t
        WHERE NOT (t = ANY(%s)) GROUP BY t ORDER BY n DESC LIMIT 20""",
        (sorted(vocab.ARCHITECTURAL_ELEMENT),))}
    # material_visual advisory (not controlled)
    out["material_visual_advisory"] = _one(cur, f"""
        SELECT count(DISTINCT m) distinct_terms,
               count(*) FILTER (WHERE m='unspecified') unspecified_uses
        FROM {BLD}, unnest(material_visual) m""")
    # image_derived.style advisory OOV
    out["image_derived_style_advisory"] = _one(cur, f"""
        SELECT count(*) FILTER (WHERE image_derived->>'style' IS NOT NULL) have_style,
               count(*) FILTER (WHERE image_derived->>'style' IS NOT NULL
                    AND NOT (image_derived->>'style' = ANY(%s))) oov_style
        FROM {BLD}""", (sorted(vocab.STYLE),))
    return out


# --------------------------------------------------------------------------- #
def section_l3(cur) -> dict:
    out = {}
    # architect_canonical_ids -> architects (dangling)
    out["building_to_architect"] = _one(cur, f"""
        WITH ids AS (SELECT DISTINCT unnest(architect_canonical_ids) aid FROM {BLD})
        SELECT count(*) distinct_arch_ids,
               count(*) FILTER (WHERE aid NOT IN (SELECT canonical_arch_id FROM {ARCH})) dangling
        FROM ids""")
    out["building_rows_with_dangling_arch"] = _one(cur, f"""
        SELECT count(*) n FROM {BLD} b
        WHERE EXISTS (SELECT 1 FROM unnest(b.architect_canonical_ids) aid
                      WHERE aid NOT IN (SELECT canonical_arch_id FROM {ARCH}))""")
    # architects.building_ids -> buildings (reverse dangling)
    out["architect_to_building"] = _one(cur, f"""
        WITH bids AS (SELECT DISTINCT unnest(building_ids) bid FROM {ARCH})
        SELECT count(*) distinct_bld_ids,
               count(*) FILTER (WHERE bid NOT IN (SELECT canonical_bld_id FROM {BLD})) dangling
        FROM bids""")
    # source_refs keys valid
    out["source_ref_keys"] = {"unexpected": _q(cur, f"""
        SELECT k val, count(*) n FROM {BLD}, jsonb_object_keys(source_refs) k
        WHERE NOT (k = ANY(%s)) GROUP BY k ORDER BY n DESC LIMIT 20""", (list(SOURCES),))}

    # tag tables
    out["tag_tables"] = _safe(cur, _l3_tag_tables)
    return out


def _l3_tag_tables(cur) -> dict:
    o = {}
    pub = _one(cur, f"SELECT count(*) n FROM {BLD} WHERE is_publishable")["n"]
    o["publishable_n"] = pub
    o["row_counts"] = _one(cur, """
        SELECT (SELECT count(*) FROM canonical_v2_tag_stats) stats,
               (SELECT count(*) FROM canonical_v2_tag_centroids) centroids,
               (SELECT count(*) FROM canonical_v2_tag_vocabulary) vocabulary""")
    # (axis,tag) parity across the 3 tables
    o["key_parity"] = _one(cur, """
        SELECT
         (SELECT count(*) FROM (SELECT axis,tag FROM canonical_v2_tag_stats
            EXCEPT SELECT axis,tag FROM canonical_v2_tag_centroids) x) stats_minus_centroids,
         (SELECT count(*) FROM (SELECT axis,tag FROM canonical_v2_tag_centroids
            EXCEPT SELECT axis,tag FROM canonical_v2_tag_stats) x) centroids_minus_stats,
         (SELECT count(*) FROM (SELECT axis,tag FROM canonical_v2_tag_stats
            EXCEPT SELECT axis,tag FROM canonical_v2_tag_vocabulary) x) stats_minus_vocab,
         (SELECT count(*) FROM (SELECT axis,tag FROM canonical_v2_tag_vocabulary
            EXCEPT SELECT axis,tag FROM canonical_v2_tag_stats) x) vocab_minus_stats""")
    # total_n consistency + IDF formula
    o["total_n"] = _one(cur, """
        SELECT min(total_n) min, max(total_n) max, count(DISTINCT total_n) distinct_vals
        FROM canonical_v2_tag_stats""")
    o["idf_formula_violations"] = _one(cur, """
        SELECT count(*) n FROM canonical_v2_tag_stats
        WHERE abs(idf - ln(total_n::float/(doc_freq+1))) > 1e-6""")
    # doc_freq sum per scalar controlled axis must equal total_n
    o["docfreq_sum_scalar_axes"] = _q(cur, f"""
        SELECT axis, sum(doc_freq) docfreq_sum, max(total_n) total_n
        FROM canonical_v2_tag_stats
        WHERE axis IN ('program','style','color_tone','atmosphere')
        GROUP BY axis ORDER BY axis""")
    # centroid L2-norm health
    o["centroid_norm"] = _one(cur, """
        SELECT count(*) total,
          count(*) FILTER (WHERE abs(1 + (centroid <#> centroid)) > 1e-3) not_normalized,
          count(*) FILTER (WHERE centroid <#> centroid = 0) zero_vec
        FROM canonical_v2_tag_centroids""")
    return o


# --------------------------------------------------------------------------- #
def section_l4(cur) -> dict:
    out = {}
    # recompute is_publishable predicate vs stored flag
    pred = """(name IS NOT NULL AND name <> ''
        AND source_refs <> '{}'::jsonb
        AND source_urls IS NOT NULL AND source_urls NOT IN ('{}'::jsonb,'[]'::jsonb,'null'::jsonb)
        AND all_images IS NOT NULL AND all_images NOT IN ('{}'::jsonb,'[]'::jsonb,'null'::jsonb)
        AND display_cover_url IS NOT NULL AND display_cover_url <> '')"""
    out["publishable_recompute"] = _one(cur, f"""
        SELECT count(*) FILTER (WHERE {pred} AND NOT is_publishable) pred_true_flag_false,
               count(*) FILTER (WHERE NOT ({pred}) AND is_publishable) pred_false_flag_true,
               count(*) total FROM {BLD}""")
    # placeholder cover urls among publishable (%% escapes psycopg2 placeholder)
    out["placeholder_covers_publishable"] = _one(cur, f"""
        SELECT count(*) n FROM {BLD} WHERE is_publishable AND (
            display_cover_url ILIKE '%%placeholder%%' OR display_cover_url ILIKE '%%default-thumb%%'
            OR display_cover_url ILIKE '%%facebook%%' OR display_cover_url ILIKE '%%img-placeholder%%')""")
    # invariant: is_publishable == (publishability_reasons is empty) ?
    out["publishable_reasons_invariant"] = _one(cur, f"""
        SELECT count(*) FILTER (WHERE is_publishable AND cardinality(publishability_reasons) > 0) pub_but_has_reasons,
               count(*) FILTER (WHERE NOT is_publishable AND cardinality(publishability_reasons) = 0) nonpub_but_no_reasons
        FROM {BLD}""")
    out["nonpublishable_reason_breakdown"] = _q(cur, f"""
        SELECT r reason, count(*) n FROM {BLD}, unnest(publishability_reasons) r
        GROUP BY r ORDER BY n DESC LIMIT 20""")
    out["publishable_with_reasons_sample"] = _q(cur, f"""
        SELECT canonical_bld_id, name, publishability_reasons FROM {BLD}
        WHERE is_publishable AND cardinality(publishability_reasons) > 0 LIMIT 10""")
    # architects recommendable recompute
    out["recommendable_recompute"] = _safe(cur, lambda c: _one(c, f"""
        SELECT count(*) FILTER (WHERE rec_pred AND NOT is_recommendable) pred_true_flag_false,
               count(*) FILTER (WHERE NOT rec_pred AND is_recommendable) pred_false_flag_true,
               count(*) total FROM (
          SELECT is_recommendable,
            (n_buildings_publishable >= 3 AND (
                (website IS NOT NULL AND website <> '') OR
                (description IS NOT NULL AND description <> '') OR
                (primary_country IS NOT NULL AND primary_country <> ''))) rec_pred
          FROM {ARCH}) s"""))
    return out


# --------------------------------------------------------------------------- #
def section_l6(cur) -> dict:
    out = {}
    out["era_vs_year"] = _one(cur, f"""
        SELECT count(*) FILTER (WHERE era IS DISTINCT FROM ({ERA_CASE})) mismatch,
               count(*) total FROM {BLD}""")
    out["year_kind_vs_year"] = _one(cur, f"""
        SELECT count(*) FILTER (WHERE year_kind IS DISTINCT FROM (CASE
            WHEN project_year IS NULL THEN 'unknown'
            WHEN project_year >= 2026 THEN 'future'
            ELSE 'completed' END)) mismatch,
               count(*) total FROM {BLD}""")
    out["year_kind_mismatch_breakdown"] = _q(cur, f"""
        SELECT year_kind stored, (CASE
            WHEN project_year IS NULL THEN 'unknown'
            WHEN project_year >= 2026 THEN 'future'
            ELSE 'completed' END) expected,
            min(project_year) min_year, max(project_year) max_year, count(*) n
        FROM {BLD}
        WHERE year_kind IS DISTINCT FROM (CASE
            WHEN project_year IS NULL THEN 'unknown'
            WHEN project_year >= 2026 THEN 'future'
            ELSE 'completed' END)
        GROUP BY year_kind, expected ORDER BY n DESC""")
    out["tier_vs_nsources"] = _one(cur, f"""
        SELECT count(*) FILTER (WHERE confidence_tier IS DISTINCT FROM (CASE
            WHEN n_sources >= 3 THEN 'T1' WHEN n_sources = 2 THEN 'T2'
            ELSE 'T3' END)) mismatch,
               count(*) total FROM {BLD}""")
    out["identity_source"] = {"unexpected": _q(cur, f"""
        SELECT identity_source val, count(*) n FROM {BLD}
        WHERE identity_source IS NOT NULL AND NOT (identity_source = ANY(%s))
        GROUP BY identity_source ORDER BY n DESC LIMIT 20""", (list(SOURCES),))}
    return out


# --------------------------------------------------------------------------- #
def section_l7a(cur) -> dict:
    out = {}
    # D1 style vs D2 image_derived.style agreement (publishable, both present)
    out["d1_d2_style_agreement"] = _one(cur, f"""
        SELECT count(*) compared,
          count(*) FILTER (WHERE lower(style) = lower(image_derived->>'style')) agree
        FROM {BLD}
        WHERE is_publishable AND image_derived->>'style' IS NOT NULL
          AND image_derived->>'style' <> ''""")
    # typology_primary vs program HARD contradiction (program outside acceptable set)
    items = list(TYP_PROGRAM_OK.items())
    cases = " ".join("WHEN typology_primary = %s THEN %s::text[]" for _ in items)
    flat = []
    for t, ok in items:
        flat += [t, ok]
    base = f"""SELECT canonical_bld_id, name, program, typology_primary,
        (CASE {cases} ELSE NULL END) ok_programs FROM {BLD}"""
    out["typology_program_contradiction"] = _one(cur, f"""
        WITH s AS ({base})
        SELECT count(*) FILTER (WHERE ok_programs IS NOT NULL
                    AND NOT (program = ANY(ok_programs))) hard_contradictions,
               count(*) FILTER (WHERE ok_programs IS NOT NULL) checkable
        FROM s""", tuple(flat))
    out["typology_program_examples"] = _q(cur, f"""
        WITH s AS ({base})
        SELECT typology_primary, program, count(*) n
        FROM s WHERE ok_programs IS NOT NULL AND NOT (program = ANY(ok_programs))
        GROUP BY typology_primary, program ORDER BY n DESC LIMIT 25""", tuple(flat))
    return out


# --------------------------------------------------------------------------- #
def section_l7b(cur) -> dict:
    out = {}
    pub = "WHERE is_publishable"
    # catch-all rates (publishable)
    out["catch_all_rates"] = _one(cur, f"""
        SELECT count(*) total,
          count(*) FILTER (WHERE program='Other') program_other,
          count(*) FILTER (WHERE style='Contemporary') style_contemporary,
          count(*) FILTER (WHERE typology_primary='Mixed Use') typ_mixed,
          count(*) FILTER (WHERE typology_primary IS NULL) typ_null,
          count(*) FILTER (WHERE cardinality(material_visual)=0
                OR material_visual = ARRAY['unspecified']) material_empty_or_unspec,
          count(*) FILTER (WHERE cardinality(architectural_elements)=0) elements_empty
        FROM {BLD} {pub}""")
    # per-axis distribution skew (top value share, publishable)
    skew = {}
    for col in ("program", "style", "color_tone", "atmosphere"):
        top = _q(cur, f"""
            SELECT {col} val, count(*) n FROM {BLD} {pub}
            GROUP BY {col} ORDER BY n DESC LIMIT 3""")
        skew[col] = top
    out["distribution_top3"] = skew
    # per-row information score: # of generic/NULL discriminative axes
    out["info_score"] = _one(cur, f"""
        SELECT
          round(avg(g),3) avg_generic_axes,
          count(*) FILTER (WHERE g >= 5) rows_5plus_generic,
          count(*) FILTER (WHERE g >= 6) rows_6plus_generic,
          count(*) total
        FROM (
          SELECT (
            (program='Other')::int + (style='Contemporary')::int +
            (typology_primary IS NULL OR typology_primary='Mixed Use')::int +
            (era IS NULL)::int + (scale IS NULL)::int +
            (structural_system IS NULL)::int + (roof_type IS NULL)::int +
            (facade_pattern IS NULL)::int +
            (cardinality(material_visual)=0 OR material_visual=ARRAY['unspecified'])::int
          ) g FROM {BLD} {pub}) s""")
    # visual_description length
    out["visual_description_len"] = _one(cur, f"""
        SELECT min(length(visual_description)) min,
               round(avg(length(visual_description))) avg,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY length(visual_description)) p50,
               count(*) FILTER (WHERE length(visual_description) < 50) under_50_chars,
               count(*) total FROM {BLD} {pub}""")
    return out


def section_prior(cur) -> dict:
    """Re-query the 14 named failures from the 2026-05 audit (db_quality_audit.md)."""
    out = {}
    # year hallucinations: 1847/1800/1812 are <1850 -> C9 floor should have NULLed
    out["year_below_1850"] = _one(cur, f"""
        SELECT count(*) n, min(project_year) min, max(project_year) max
        FROM {BLD} WHERE project_year IS NOT NULL AND project_year < 1850""")
    out["year_specific_halluc"] = _q(cur, f"""
        SELECT project_year, count(*) n FROM {BLD}
        WHERE project_year IN (1800, 1812, 1847) GROUP BY project_year""")
    # garbage names (case-insensitive), with publishability status
    out["name_literal_test"] = _q(cur, f"""
        SELECT canonical_bld_id, name, is_publishable, publishability_reasons
        FROM {BLD} WHERE lower(trim(name)) = 'test' LIMIT 10""")
    out["name_social_dwellings"] = _q(cur, f"""
        SELECT canonical_bld_id, name, is_publishable, publishability_reasons
        FROM {BLD} WHERE lower(trim(name)) = 'social dwellings' LIMIT 10""")
    # leaked "City - Country" style names: short, contains ' - ', no letters beyond places
    out["name_leaked_location_like"] = _one(cur, f"""
        SELECT count(*) n FROM {BLD}
        WHERE name ~ '^[A-Z][a-z]+( [A-Z][a-z]+)? - [A-Z]'
          AND length(name) < 30 AND is_publishable""")
    # garbage-name gate effectiveness (these became nonpublishable)
    out["garbage_name_gates"] = _q(cur, f"""
        SELECT r reason, count(*) n FROM {BLD}, unnest(publishability_reasons) r
        WHERE r IN ('seo_or_generic_title','name_needs_review','spam_candidate')
        GROUP BY r ORDER BY n DESC""")
    # publishable rows that still look generic/garbage by name length
    out["publishable_very_short_names"] = _q(cur, f"""
        SELECT name, count(*) n FROM {BLD}
        WHERE is_publishable AND length(trim(name)) <= 3
        GROUP BY name ORDER BY n DESC LIMIT 20""")
    return out


SECTIONS = {
    "diag": section_diag, "l1": section_l1, "l2": section_l2, "l3": section_l3,
    "l4": section_l4, "l6": section_l6, "l7a": section_l7a, "l7b": section_l7b,
    "prior": section_prior,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", default="diag",
                    help="comma list or 'all' (" + ",".join(SECTIONS) + ")")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    want = list(SECTIONS) if args.section == "all" else args.section.split(",")
    conn = _connect()
    conn.set_session(readonly=True, autocommit=False)
    result = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for name in want:
            fn = SECTIONS.get(name.strip())
            result[name] = _safe(cur, fn) if fn else {"error": "unknown section"}
    conn.close()

    text = json.dumps(result, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
