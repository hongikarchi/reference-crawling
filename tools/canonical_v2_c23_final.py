#!/usr/bin/env python3
"""C23 final cleanup — auto-merge 6 arch-overlap groups + country dirty
extraction + suspicious city + sidecar refresh.

Addresses Codex C22 full reaudit residual warnings:
  A. 6 arch-overlap name+country+city+year groups auto-merge (Mercedes-Benz
     Museum etc.); 16 series/exhibition groups → sidecar.
  B. 4 hard country mismatch — dirty source ("<city> <country>" format) →
     last-country-token extraction → all resolved.
  C. 135 suspicious city → broader regex (street/postal/desc-only).
  D. Sidecar refresh — 16 series + 60 SEO (sidecar only, no unpublish) + 165
     gallery + cleaned country + year.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import _confidence_tier  # noqa: E402
from tools.canonical_v2_c19_make_web_polish import (  # noqa: E402
    _load_slug_country_year_map, _row_source_country_year,
)
from tools.canonical_v2_c21_make_web_polish import (  # noqa: E402
    _NATIVE_COUNTRY_ALIASES, _normalize_country_full,
)
from tools.canonical_v2_recover_dropped_twins import (  # noqa: E402
    _UF, _union, _is_empty, _ABSORB_IF_EMPTY, _ABSORB_UNION,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C22 = CCR / "canonical_buildings_strict.completeness_c22_make_web_polish.json"
NAME_DUP_SIDECAR = ROOT / "data/reports/canonical_v2_post_neon_full_reaudit_duplicate_name_location_year.jsonl"
SEO_SIDECAR = ROOT / "data/reports/canonical_v2_post_neon_full_reaudit_seo_name_candidate.jsonl"
GALLERY_SIDECAR_IN = ROOT / "data/reports/canonical_v2_post_neon_full_reaudit_cross_row_gallery_phash_review.jsonl"

OUT = CCR / "canonical_buildings_strict.completeness_c23_final.json"
REPORT = ROOT / "data/reports/canonical_v2_c23_final_report.json"
COUNTRY_SIDECAR = ROOT / "data/reports/canonical_v2_c23_country_conflict_sidecar.jsonl"
YEAR_SIDECAR = ROOT / "data/reports/canonical_v2_c23_year_conflict_sidecar.jsonl"
SERIES_SIDECAR = ROOT / "data/reports/canonical_v2_c23_series_pavilion_sidecar.jsonl"
SEO_SIDECAR_OUT = ROOT / "data/reports/canonical_v2_c23_seo_candidate_sidecar.jsonl"
GALLERY_SIDECAR_OUT = ROOT / "data/reports/canonical_v2_c23_gallery_phash_sidecar.jsonl"

# Phase C — broader suspicious city
_SUSPICIOUS_CITY_C23 = re.compile(
    r"^\s*("
    r"\d+[a-z]?"
    r"|[A-Za-z]"
    r"|(19|20)\d{2}\s*[-–]"
    r"|.*,\s*(via|rua|street|st\.?|avenue|ave|road|rd\.?|calle|strasse|str\.?|blvd|boulevard|hauptstr|平|通|大街)\s+\S+"
    r"|[-–]+"
    r"|\d{5}(-\d{4})?"
    r"|tbd|address|location|n/a|none|unknown"
    r"|.*\b(centro direzionale|industrial park|downtown|district)\b.*"
    r")\s*$",
    re.I,
)


def _is_suspicious_city(city):
    return isinstance(city, str) and bool(_SUSPICIOUS_CITY_C23.match(city))


# Phase B — country dirty token extraction
# Build known-country set from alias map + common names
_KNOWN_COUNTRIES = set(_NATIVE_COUNTRY_ALIASES.values())
_KNOWN_COUNTRIES.update({
    "Spain", "Italy", "France", "Germany", "Netherlands", "Belgium", "United Kingdom",
    "United States", "Canada", "Mexico", "Brazil", "Argentina", "Chile", "Peru",
    "Japan", "China", "South Korea", "North Korea", "Taiwan", "Hong Kong",
    "Vietnam", "Thailand", "Indonesia", "Malaysia", "Philippines", "Singapore",
    "India", "Pakistan", "Bangladesh", "Sri Lanka",
    "Australia", "New Zealand",
    "Russia", "Ukraine", "Poland", "Czechia", "Slovakia", "Hungary", "Romania",
    "Bulgaria", "Croatia", "Serbia", "Slovenia",
    "Sweden", "Norway", "Denmark", "Finland", "Iceland", "Ireland",
    "Austria", "Switzerland", "Portugal", "Greece", "Turkey", "Cyprus",
    "Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Oman", "Bahrain",
    "Iran", "Iraq", "Israel", "Jordan", "Lebanon", "Egypt", "Morocco", "Tunisia",
    "South Africa", "Nigeria", "Kenya", "Ethiopia", "Ghana",
    "Estonia", "Latvia", "Lithuania", "Belarus", "Moldova",
    "Albania", "North Macedonia", "Montenegro", "Bosnia and Herzegovina",
    "Luxembourg", "Liechtenstein", "Monaco", "Malta", "Andorra", "San Marino",
})


def _extract_country_from_dirty(raw):
    """If raw looks like '<city> <country>' or comma-separated, extract the
    country name. Return None if no known country found."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # try comma split first
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    for p in reversed(parts):
        norm = _normalize_country_full(p)
        if norm in _KNOWN_COUNTRIES:
            return norm
    # try ending tokens (1, 2, 3 trailing words)
    tokens = text.split()
    for n in (1, 2, 3, 4):
        if n > len(tokens):
            break
        candidate = " ".join(tokens[-n:])
        norm = _normalize_country_full(candidate)
        if norm in _KNOWN_COUNTRIES:
            return norm
    return None


# Phase A — load 22 name-dup groups, classify by arch overlap
def _load_name_dup_merge_groups():
    """Return list of {survivor, losers, key} for arch-overlap groups,
    plus list of series-only groups for sidecar."""
    merge = []
    series = []
    if not NAME_DUP_SIDECAR.exists():
        return merge, series
    with NAME_DUP_SIDECAR.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows = d.get("rows") or []
            if len(rows) < 2:
                continue
            arch_sets = [frozenset(r.get("architect_names") or []) for r in rows]
            overlap = frozenset.intersection(*arch_sets) if arch_sets else frozenset()
            if overlap:
                cids = sorted(r["cid"] for r in rows)
                merge.append({
                    "survivor": cids[0], "losers": cids[1:], "key": d.get("key"),
                    "row_cids": cids,
                })
            else:
                series.append({
                    "key": d.get("key"),
                    "rows": [{"cid": r["cid"], "name": r.get("name"),
                              "architect_names": r.get("architect_names"),
                              "source_refs": r.get("source_refs")}
                             for r in rows],
                })
    return merge, series


def _build_merge_plan(merge_groups, meta):
    uf = _UF()
    for g in merge_groups:
        cids = g["row_cids"]
        for c in cids[1:]:
            uf.union(cids[0], c)
    comp = defaultdict(list)
    for cid in uf.p:
        comp[uf.find(cid)].append(cid)
    losers = set(uf.p) - set(comp)
    survivor_patch = {}
    for surv, members in comp.items():
        if surv not in meta:
            continue
        merged = dict(meta[surv])
        for lc in members:
            if lc == surv:
                continue
            loser = meta.get(lc)
            if loser is None:
                continue
            sr = dict(merged.get("source_refs") or {})
            for s, ids in (loser.get("source_refs") or {}).items():
                sr[s] = _union(sr.get(s), ids)
            merged["source_refs"] = sr
            su = dict(loser.get("source_urls") or {})
            su.update(merged.get("source_urls") or {})
            merged["source_urls"] = su
            for f in _ABSORB_UNION:
                merged[f] = _union(merged.get(f), loser.get(f))
            for f in _ABSORB_IF_EMPTY:
                if _is_empty(merged.get(f)) and not _is_empty(loser.get(f)):
                    merged[f] = loser.get(f)
        n = len(merged.get("source_refs") or {})
        merged["n_sources"] = n
        merged["confidence_tier"] = _confidence_tier(n)
        survivor_patch[surv] = merged
    return survivor_patch, losers


# --- main ---
def main() -> int:
    if not C22.exists():
        print(f"FATAL: missing {C22}", file=sys.stderr)
        return 2

    # Phase A — collect merge groups
    merge_groups, series_groups = _load_name_dup_merge_groups()
    print(f"name-dup merge groups (arch overlap): {len(merge_groups)}; "
          f"series groups (no overlap): {len(series_groups)}", file=sys.stderr)

    target_cids = set()
    for g in merge_groups:
        target_cids.update(g["row_cids"])

    print("loading meta for merge target cids...", file=sys.stderr)
    meta = {}
    for r in iter_buildings(C22):
        cid = r.get("canonical_bld_id")
        if cid in target_cids:
            meta[cid] = r

    survivor_patch, losers = _build_merge_plan(merge_groups, meta)
    print(f"merge losers: {len(losers)}", file=sys.stderr)

    print("loading source DB country/year maps...", file=sys.stderr)
    src_map = _load_slug_country_year_map()

    # Phase D — write series + SEO + gallery sidecars
    SERIES_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    with SERIES_SIDECAR.open("w") as f:
        for s in series_groups:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    # SEO sidecar passthrough
    if SEO_SIDECAR.exists():
        with SEO_SIDECAR_OUT.open("w") as f:
            for line in SEO_SIDECAR.read_text().splitlines():
                if line.strip():
                    f.write(line + "\n")
    # Gallery sidecar passthrough
    if GALLERY_SIDECAR_IN.exists():
        with GALLERY_SIDECAR_OUT.open("w") as f:
            for line in GALLERY_SIDECAR_IN.read_text().splitlines():
                if line.strip():
                    f.write(line + "\n")

    counts: Counter = Counter()
    country_sidecar_rows = []
    year_sidecar_rows = []
    counter_classes = Counter()

    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C22):
            n_in += 1
            cid = row.get("canonical_bld_id")
            if cid in losers:
                counts["merge_loser_removed"] += 1
                continue
            if cid in survivor_patch:
                row = survivor_patch[cid]
                counts["merge_survivor"] += 1

            # C — suspicious city extension
            city = row.get("location_city")
            if _is_suspicious_city(city):
                row["location_city"] = None
                counts["suspicious_city_nulled_c23"] += 1

            # B + sidecar — country re-classify with dirty extraction
            if row.get("is_publishable"):
                pairs = _row_source_country_year(row, src_map)
                # Extract country from each dirty source
                cleaned_countries = []
                for _src, raw, _yr in pairs:
                    if not raw:
                        continue
                    norm_direct = _normalize_country_full(raw)
                    if norm_direct in _KNOWN_COUNTRIES:
                        cleaned_countries.append(norm_direct)
                    else:
                        extracted = _extract_country_from_dirty(raw)
                        if extracted:
                            cleaned_countries.append(extracted)
                            counts["country_dirty_extracted"] += 1
                # Compare
                norm = set(cleaned_countries)
                rc = _normalize_country_full(row.get("location_country"))
                if rc:
                    norm.add(rc)
                if len(norm) > 1:
                    counter_classes["real"] += 1
                    country_sidecar_rows.append({
                        "cid": cid, "name": row.get("name"),
                        "row_country": row.get("location_country"),
                        "source_countries_extracted": cleaned_countries,
                        "normalized": sorted(norm),
                        "source_refs": row.get("source_refs") or {},
                    })
                    reasons = list(row.get("publishability_reasons") or [])
                    if "country_disputed" not in reasons:
                        reasons.append("country_disputed")
                    row["publishability_reasons"] = reasons
                else:
                    counter_classes["resolved"] += 1
                    reasons = [r for r in (row.get("publishability_reasons") or [])
                               if r != "country_disputed"]
                    row["publishability_reasons"] = reasons

                years = [y for _, _, y in pairs if isinstance(y, int)]
                if years:
                    yy = years[:]
                    if row.get("project_year") is not None:
                        yy.append(row.get("project_year"))
                    if max(yy) - min(yy) > 2:
                        year_sidecar_rows.append({
                            "cid": cid, "name": row.get("name"),
                            "row_year": row.get("project_year"),
                            "source_years": years,
                            "source_refs": row.get("source_refs") or {},
                        })

            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    with COUNTRY_SIDECAR.open("w") as f:
        for r in country_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with YEAR_SIDECAR.open("w") as f:
        for r in year_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C22.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_out == n_in - len(losers),
        "counts": dict(counts),
        "name_dup_merge_groups": len(merge_groups),
        "name_dup_series_sidecar": len(series_groups),
        "country_sidecar_entries": len(country_sidecar_rows),
        "year_sidecar_entries": len(year_sidecar_rows),
        "country_classes": dict(counter_classes),
        "removed_canonical_ids_this_pass": sorted(losers),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C23 final [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps({k: v for k, v in report.items()
                      if k != "removed_canonical_ids_this_pass"},
                     ensure_ascii=False, indent=2))
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
