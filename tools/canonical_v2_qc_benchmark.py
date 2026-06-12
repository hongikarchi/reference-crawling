#!/usr/bin/env python3
"""External-standards QC benchmark for canonical_v2 (READ-ONLY).

Runs the 11-rule QC checklist from docs/DATA_QUALITY_BENCHMARK.md against the
live Neon canonical_v2_buildings + canonical_v2_architects tables and emits a
pass/fail scorecard. No writes — SELECT only.

Each rule is scored against an engineering-default threshold:
  PASS  metric meets threshold
  WARN  within 5 percentage points of threshold
  FAIL  below threshold
  INFO  informational (process rule, not data-checkable)

Usage:
  python3 tools/canonical_v2_qc_benchmark.py            # scorecard to stdout
  python3 tools/canonical_v2_qc_benchmark.py --json OUT # also write JSON
  python3 tools/canonical_v2_qc_benchmark.py --md OUT   # also write Markdown
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect  # noqa: E402

try:
    from core.vocab import STYLE, PROGRAM  # noqa: E402
except Exception:  # pragma: no cover
    STYLE, PROGRAM = frozenset(), frozenset()

try:
    from tools.canonical_v2_upload_validator import MATERIAL_TAXONOMY_NOISE  # noqa: E402
except Exception:  # pragma: no cover
    MATERIAL_TAXONOMY_NOISE = frozenset()

NOISE = sorted(MATERIAL_TAXONOMY_NOISE)
PLACEHOLDERS = ["unknown", "n/a", "na", "-", "--", "", "tbd", "unknown architect", "?"]
CURRENT_YEAR = datetime.date.today().year
MOJIBAKE_RE = r'(Ã[\x80-\xbf]|â€|Â[\x80-\xbf]|�)'


def pct(num: int, den: int) -> float:
    num, den = int(num or 0), int(den or 0)
    return 100.0 * num / den if den else 100.0


def grade(value: float, threshold: float, *, warn_band: float = 5.0,
          higher_is_better: bool = True) -> str:
    """Grade a percentage metric vs threshold."""
    if higher_is_better:
        if value >= threshold:
            return "PASS"
        if value >= threshold - warn_band:
            return "WARN"
        return "FAIL"
    else:  # lower is better (value is a defect rate; threshold is the ceiling)
        if value <= threshold:
            return "PASS"
        if value <= threshold + warn_band:
            return "WARN"
        return "FAIL"


def rule(rid: str, title: str, status: str, headline: str,
         detail: dict[str, Any] | None = None,
         samples: list[Any] | None = None) -> dict[str, Any]:
    return {
        "id": rid, "title": title, "status": status,
        "headline": headline, "detail": detail or {}, "samples": samples or [],
    }


def run(conn) -> dict[str, Any]:
    cur = conn.cursor()
    results: list[dict[str, Any]] = []

    cur.execute("SELECT count(*) FROM canonical_v2_buildings WHERE is_publishable")
    total = cur.fetchone()[0]

    # ---- R1 presence / completeness ----------------------------------------
    cur.execute(
        """
        SELECT
          count(*) FILTER (WHERE name IS NULL OR btrim(name)='')                         AS name_missing,
          count(*) FILTER (WHERE cardinality(architect_canonical_ids)=0)                 AS arch_missing,
          count(*) FILTER (WHERE project_year IS NULL)                                   AS year_missing,
          count(*) FILTER (WHERE location_country IS NULL OR btrim(location_country)='') AS country_missing,
          count(*) FILTER (WHERE program IS NULL OR btrim(program)='')                   AS program_missing,
          count(*) FILTER (WHERE
                COALESCE(display_cover_url, cover_image_url_default) IS NULL
                AND COALESCE(jsonb_array_length(
                      CASE WHEN jsonb_typeof(all_images)='array' THEN all_images ELSE '[]'::jsonb END),0)=0
          ) AS image_missing
        FROM canonical_v2_buildings WHERE is_publishable
        """
    )
    (name_m, arch_m, year_m, country_m, program_m, image_m) = cur.fetchone()
    fields = [
        ("name", name_m, 100.0), ("architect", arch_m, 99.0),
        ("project_year", year_m, 95.0), ("location_country", country_m, 85.0),
        ("program", program_m, 90.0), (">=1 image", image_m, 100.0),
    ]
    sub, worst = {}, "PASS"
    order = {"PASS": 0, "WARN": 1, "FAIL": 2, "INFO": 0}
    for fname, missing, thr in fields:
        present = pct(total - missing, total)
        g = grade(present, thr)
        sub[fname] = {"present_pct": round(present, 2), "missing": missing, "threshold": thr, "status": g}
        if order[g] > order[worst]:
            worst = g
    results.append(rule(
        "R1", "Presence / completeness", worst,
        "; ".join(f"{k} {v['present_pct']}%/{v['threshold']}% [{v['status']}]" for k, v in sub.items()),
        detail=sub,
    ))

    # ---- R2 architect validity (referential + placeholder) ------------------
    cur.execute(
        """
        SELECT count(DISTINCT b.canonical_bld_id)
        FROM canonical_v2_buildings b, unnest(b.architect_canonical_ids) aid
        WHERE b.is_publishable
          AND NOT EXISTS (SELECT 1 FROM canonical_v2_architects a WHERE a.canonical_arch_id = aid)
        """
    )
    dangling = cur.fetchone()[0]
    cur.execute(
        """
        SELECT count(*) FROM canonical_v2_buildings
        WHERE is_publishable AND EXISTS (
            SELECT 1 FROM unnest(architect_names) n WHERE lower(btrim(n)) = ANY(%s::text[]))
        """,
        (PLACEHOLDERS,),
    )
    placeholder_rows = cur.fetchone()[0]
    cur.execute(
        """
        SELECT b.canonical_bld_id, b.name FROM canonical_v2_buildings b
        WHERE b.is_publishable AND EXISTS (
            SELECT 1 FROM unnest(b.architect_canonical_ids) aid
            WHERE NOT EXISTS (SELECT 1 FROM canonical_v2_architects a WHERE a.canonical_arch_id = aid))
        LIMIT 5
        """
    )
    samp = [dict(zip(("bid", "name"), r)) for r in cur.fetchall()]
    resolvable = pct(total - dangling, total)
    g = grade(resolvable, 99.0)
    if placeholder_rows > 0 and order[g] < order["WARN"]:
        g = "WARN"
    results.append(rule(
        "R2", "Architect validity", g,
        f"resolvable {resolvable:.2f}%/99% [{grade(resolvable,99.0)}]; dangling-ref rows={dangling}; placeholder-name rows={placeholder_rows}",
        detail={"resolvable_pct": round(resolvable, 2), "dangling_ref_rows": dangling, "placeholder_rows": placeholder_rows},
        samples=samp,
    ))

    # ---- R3 year sanity -----------------------------------------------------
    # Out-of-range: <1500 always bad; >yr+10 bad UNLESS year_kind='future'
    # (future/concept projects legitimately carry far-future target years);
    # a hard ceiling of yr+100 still flags absurd values even for future.
    cur.execute(
        f"""
        SELECT
          count(*) FILTER (WHERE project_year IS NOT NULL AND (
                project_year < 1500
                OR (year_kind <> 'future' AND project_year > {CURRENT_YEAR + 10})
                OR project_year > {CURRENT_YEAR + 100}))                                  AS oob,
          count(*) FILTER (WHERE year_kind='completed' AND project_year > {CURRENT_YEAR})  AS completed_future,
          count(*) FILTER (WHERE year_kind='completed' AND project_year IS NULL)           AS completed_no_year
        FROM canonical_v2_buildings WHERE is_publishable
        """
    )
    oob, comp_future, comp_no_year = cur.fetchone()
    cur.execute(
        "SELECT year_kind, count(*) FROM canonical_v2_buildings WHERE is_publishable GROUP BY year_kind ORDER BY 2 DESC"
    )
    yk_dist = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(
        f"""SELECT canonical_bld_id, name, project_year, year_kind FROM canonical_v2_buildings
           WHERE is_publishable AND project_year IS NOT NULL
             AND (project_year < 1500
                  OR (year_kind <> 'future' AND project_year > {CURRENT_YEAR + 10})
                  OR project_year > {CURRENT_YEAR + 100}) LIMIT 5"""
    )
    yr_samp = [dict(zip(("bid", "name", "year", "kind"), r)) for r in cur.fetchall()]
    bad_year = oob + comp_future + comp_no_year
    g = "PASS" if bad_year == 0 else ("WARN" if bad_year <= 0.005 * total else "FAIL")
    results.append(rule(
        "R3", "Year sanity", g,
        f"out-of-range={oob}; completed-but-future-year={comp_future}; completed-but-null-year={comp_no_year}",
        detail={"oob": oob, "completed_future": comp_future, "completed_no_year": comp_no_year, "year_kind_dist": yk_dist},
        samples=yr_samp,
    ))

    # ---- R4 style controlled-vocab ------------------------------------------
    cur.execute("SELECT style, count(*) FROM canonical_v2_buildings WHERE is_publishable GROUP BY style")
    style_rows = cur.fetchall()
    oov = sum(c for s, c in style_rows if s not in STYLE)
    oov_terms = sorted(((s, c) for s, c in style_rows if s not in STYLE), key=lambda x: -x[1])[:10]
    oov_rate = pct(oov, total)
    g = grade(oov_rate, 5.0, higher_is_better=False)
    results.append(rule(
        "R4", "Style controlled-vocab", g,
        f"OOV {oov_rate:.2f}% (limit 5%) [{g}]; {oov} rows; vocab={len(STYLE)} terms",
        detail={"oov_rate_pct": round(oov_rate, 2), "oov_rows": oov, "oov_terms": oov_terms},
    ))

    # ---- R5 material noise + emptiness --------------------------------------
    cur.execute(
        """
        SELECT
          count(*) FILTER (WHERE EXISTS (SELECT 1 FROM unnest(material_visual) m WHERE lower(m)=ANY(%s::text[]))) AS noise_rows,
          count(*) FILTER (WHERE cardinality(material_visual)=0) AS empty_rows
        FROM canonical_v2_buildings WHERE is_publishable
        """,
        (NOISE,),
    )
    noise_rows, empty_rows = cur.fetchone()
    empty_rate = pct(empty_rows, total)
    g = "PASS"
    if noise_rows > 0:
        g = "FAIL"
    elif empty_rate > 5.0:
        g = "WARN"
    results.append(rule(
        "R5", "Material noise filter", g,
        f"non-material pollution rows={noise_rows} (target 0); empty material {empty_rate:.2f}% ({empty_rows} rows, limit 5%)",
        detail={"noise_rows": noise_rows, "empty_rows": empty_rows, "empty_rate_pct": round(empty_rate, 2), "noise_terms": len(NOISE)},
    ))

    # ---- R6 + R8 cover presence + dedup -------------------------------------
    cur.execute(
        "SELECT count(*) FROM canonical_v2_buildings WHERE is_publishable AND COALESCE(display_cover_url, cover_image_url_default) IS NULL"
    )
    no_cover = cur.fetchone()[0]
    cur.execute(
        """
        WITH c AS (
            SELECT COALESCE(display_cover_url, cover_image_url_default) AS url
            FROM canonical_v2_buildings
            WHERE is_publishable AND COALESCE(display_cover_url, cover_image_url_default) IS NOT NULL
        ), dups AS (
            SELECT url, count(*) n FROM c GROUP BY url HAVING count(*) > 1
        )
        SELECT COALESCE(count(*),0) AS dup_url_groups, COALESCE(sum(n),0) AS dup_rows FROM dups
        """
    )
    dup_url_groups, dup_rows = cur.fetchone()
    dup_rate = pct(dup_rows, total)
    cover_present = pct(total - no_cover, total)
    g8 = grade(cover_present, 100.0)
    results.append(rule(
        "R8", "Cover selection", g8,
        f"cover present {cover_present:.2f}%/100% [{g8}]; missing cover rows={no_cover}",
        detail={"cover_present_pct": round(cover_present, 2), "missing_cover": no_cover},
    ))
    g6 = grade(dup_rate, 1.0, higher_is_better=False)
    results.append(rule(
        "R6", "Image dedup (cover URL)", g6,
        f"shared-cover-URL rows {dup_rate:.2f}% (limit 1%) [{g6}]; {dup_url_groups} dup URLs across {dup_rows} rows",
        detail={"dup_rate_pct": round(dup_rate, 2), "dup_url_groups": dup_url_groups, "dup_rows": dup_rows},
    ))

    # ---- R7 hash-method choice (informational) ------------------------------
    results.append(rule(
        "R7", "Hash-method choice", "INFO",
        "process rule: pipeline uses pHash (e1_phash_dedup); ColourHash/WaveHash not used. Not data-checkable.",
    ))

    # ---- R9 cross-source duplicate projects (heuristic) ---------------------
    cur.execute(
        """
        WITH n AS (
            SELECT canonical_bld_id, lower(btrim(name)) nm, lower(coalesce(location_country,'')) cc
            FROM canonical_v2_buildings WHERE is_publishable AND btrim(name) <> ''
        ), g AS (
            SELECT nm, cc, count(*) k FROM n GROUP BY nm, cc HAVING count(*) > 1
        )
        SELECT COALESCE(count(*),0) AS groups, COALESCE(sum(k),0) AS rows_in_groups FROM g
        """
    )
    dup_groups, dup_proj_rows = cur.fetchone()
    cur.execute(
        """
        WITH n AS (
            SELECT canonical_bld_id, name, lower(btrim(name)) nm, lower(coalesce(location_country,'')) cc
            FROM canonical_v2_buildings WHERE is_publishable AND btrim(name) <> ''
        )
        SELECT nm, cc, count(*) k, array_agg(canonical_bld_id) FROM n GROUP BY nm, cc HAVING count(*) > 1
        ORDER BY 3 DESC LIMIT 5
        """
    )
    proj_samp = [{"name": r[0], "country": r[1], "n": r[2], "bids": r[3][:6]} for r in cur.fetchall()]
    dup_proj_rate = pct(dup_proj_rows, total)
    g = grade(dup_proj_rate, 2.0, higher_is_better=False)
    results.append(rule(
        "R9", "Cross-source dup (name+country heuristic)", g,
        f"name+country collision rows {dup_proj_rate:.2f}% (limit 2%) [{g}]; {dup_groups} groups / {dup_proj_rows} rows",
        detail={"dup_rate_pct": round(dup_proj_rate, 2), "groups": dup_groups, "rows_in_groups": dup_proj_rows},
        samples=proj_samp,
    ))

    # ---- R10 encoding / mojibake --------------------------------------------
    cur.execute(
        """
        SELECT count(*) FROM canonical_v2_buildings
        WHERE is_publishable AND (
            name ~ %s OR coalesce(architects_text,'') ~ %s OR coalesce(visual_description,'') ~ %s
            OR coalesce(location_city,'') ~ %s)
        """,
        (MOJIBAKE_RE, MOJIBAKE_RE, MOJIBAKE_RE, MOJIBAKE_RE),
    )
    mojibake_rows = cur.fetchone()[0]
    cur.execute(
        """
        SELECT canonical_bld_id, name FROM canonical_v2_buildings
        WHERE is_publishable AND (name ~ %s OR coalesce(architects_text,'') ~ %s) LIMIT 5
        """,
        (MOJIBAKE_RE, MOJIBAKE_RE),
    )
    moj_samp = [dict(zip(("bid", "name"), r)) for r in cur.fetchall()]
    g = "PASS" if mojibake_rows == 0 else ("WARN" if mojibake_rows <= 0.005 * total else "FAIL")
    results.append(rule(
        "R10", "Encoding / language", g,
        f"mojibake/replacement-char rows={mojibake_rows} (target 0)",
        detail={"mojibake_rows": mojibake_rows}, samples=moj_samp,
    ))

    # ---- R11 cold-start coverage (architects) -------------------------------
    cur.execute("SELECT count(*) FROM canonical_v2_architects WHERE is_recommendable")
    rec = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM canonical_v2_architects WHERE is_recommendable AND n_buildings_publishable = 0"
    )
    rec_no_pub = cur.fetchone()[0]
    coverage = pct(rec - rec_no_pub, rec) if rec else 100.0
    g = grade(coverage, 100.0)
    results.append(rule(
        "R11", "Cold-start coverage (architects)", g,
        f"recommendable={rec}; derivable portfolio embedding {coverage:.2f}%/100% [{g}]; recommendable-but-0-publishable={rec_no_pub}",
        detail={"recommendable": rec, "rec_without_publishable_building": rec_no_pub, "coverage_pct": round(coverage, 2)},
    ))

    cur.close()
    summary = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "WARN", "FAIL", "INFO")}
    return {
        "table": "canonical_v2_buildings",
        "publishable_rows": total,
        "current_year": CURRENT_YEAR,
        "summary": summary,
        "rules": results,
    }


def to_markdown(rep: dict[str, Any]) -> str:
    icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "INFO": "ℹ️"}
    s = rep["summary"]
    out = [
        "# QC benchmark scorecard — canonical_v2",
        "",
        f"Publishable rows: **{rep['publishable_rows']:,}** · "
        f"PASS {s['PASS']} · WARN {s['WARN']} · FAIL {s['FAIL']} · INFO {s['INFO']}",
        "",
        "| # | Rule | Status | Result |",
        "|---|---|---|---|",
    ]
    for r in rep["rules"]:
        out.append(f"| {r['id']} | {r['title']} | {icon.get(r['status'],'')} {r['status']} | {r['headline']} |")
    out.append("")
    out.append("_Benchmark: docs/DATA_QUALITY_BENCHMARK.md (deep-research 2026-06-04). Read-only audit._")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="write JSON report")
    ap.add_argument("--md", metavar="PATH", help="write Markdown scorecard")
    args = ap.parse_args()

    conn = _connect()
    try:
        rep = run(conn)
    finally:
        conn.close()

    print(to_markdown(rep))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}")
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(to_markdown(rep), encoding="utf-8")
        print(f"wrote {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
