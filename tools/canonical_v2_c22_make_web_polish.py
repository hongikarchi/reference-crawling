#!/usr/bin/env python3
"""C22 short cleanup — final 5 blockers from Codex C21 deep audit.

  A. 1 metalocus URL gap (Sornells 21 / bld_029682) — build URL from slug
     when 4_buildings_final's source_url is null.
  B. 5 cover phash missing rows unpublish + reason `images_missing_phash`.
  C. 2 cover dup force-swap — bug fix: swap regardless of stored phash (None
     in JSON but Codex audit confirms dup phash on download).
  D. country dirty filter expanded — comma-2+ tokens count as dirty
     (`São Paulo, SP, Brasil` etc.) after alias normalize fails.
  E. split sidecar dedup 40 → 7 unique (cid_a, cid_b) pairs.
"""
from __future__ import annotations

import json
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_c16_url_canon import (  # noqa: E402
    _canonical_asset_key, _is_gif, _is_lowres_url, _is_raster_url,
)
from tools.canonical_v2_c18_make_web_polish import _set_unpublish  # noqa: E402
from tools.canonical_v2_c19_make_web_polish import (  # noqa: E402
    _load_slug_country_year_map, _row_source_country_year,
)
from tools.canonical_v2_c21_make_web_polish import (  # noqa: E402
    _NATIVE_COUNTRY_ALIASES, _normalize_country_full,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C21 = CCR / "canonical_buildings_strict.completeness_c21_make_web_polish.json"
DEEP_AUDIT = ROOT / "data/reports/canonical_v2_c21_make_web_deep_audit.codex.json"
B_ID_MAP = ROOT / "data/enrich/4_buildings_final.json"
C21_SPLIT_SIDECAR = ROOT / "data/reports/canonical_v2_c21_split_suspect_sidecar.jsonl"

OUT = CCR / "canonical_buildings_strict.completeness_c22_make_web_polish.json"
REPORT = ROOT / "data/reports/canonical_v2_c22_make_web_polish_report.json"
COUNTRY_SIDECAR = ROOT / "data/reports/canonical_v2_c22_country_conflict_sidecar.jsonl"
YEAR_SIDECAR = ROOT / "data/reports/canonical_v2_c22_year_conflict_sidecar.jsonl"
SPLIT_SUSPECT_SIDECAR = ROOT / "data/reports/canonical_v2_c22_split_suspect_sidecar.jsonl"

# Phase B — 5 missing-phash cids
PHASH_MISSING_UNPUBLISH = {
    "bld_002151", "bld_002330", "bld_004279", "bld_031682", "bld_032237",
}

# Phase C — cover dup groups (from Codex C21 audit)
COVER_DUP_GROUPS = [
    {"phash": "99c2ea1c76bcd9c2963eb11a6710e309c7b2973a3c6b2603641948b539bc7bf3",
     "winner": "bld_008883", "losers": ["bld_016520"]},
    {"phash": "b146c037eebabbdf934180298cd736de5b2c4823249b7adcfb70d511868d8a4d",
     "winner": "bld_011561", "losers": ["bld_011563"]},
]
COVER_AVOID = defaultdict(set)
for g in COVER_DUP_GROUPS:
    for l in g["losers"]:
        COVER_AVOID[l].add(g["phash"])


def _force_swap_cover(row, avoid_phashes, counts):
    """Always swap if cid in avoid set; pick any raster non-lowres non-gif
    alternative, preferring images with phash."""
    images = row.get("all_images") or []
    cur_url = row.get("display_cover_url")
    candidates = [im for im in images
                  if isinstance(im, dict) and im.get("url")
                  and im.get("url") != cur_url
                  and im.get("phash") not in avoid_phashes
                  and _is_raster_url(im.get("url"))
                  and not _is_lowres_url(im.get("url"))
                  and not _is_gif(im.get("url"))]
    # prefer with phash
    candidates.sort(key=lambda im: (not bool(im.get("phash")),
                                    im.get("image_order") if isinstance(im.get("image_order"), int) else 9999))
    if candidates:
        row["display_cover_url"] = candidates[0]["url"]
        counts["cover_dup_force_swapped"] += 1
        return
    # fallback: any raster (allow lowres)
    fallback = next((im.get("url") for im in images
                     if isinstance(im, dict) and im.get("url")
                     and im.get("url") != cur_url
                     and im.get("phash") not in avoid_phashes
                     and _is_raster_url(im.get("url"))
                     and not _is_gif(im.get("url"))), None)
    if fallback:
        row["display_cover_url"] = fallback
        counts["cover_dup_swapped_lowres"] += 1
    elif row.get("is_publishable"):
        _set_unpublish(row, "cover_duplicate_no_alt")
        counts["unpublish_cover_dup_no_alt_c22"] += 1


# Phase A — B-id → url map with slug fallback
def _load_b_id_url_map_with_slug():
    if not B_ID_MAP.exists():
        return {}
    d = json.loads(B_ID_MAP.read_text(encoding="utf-8"))
    out = {}
    if isinstance(d, list):
        for r in d:
            bid = r.get("building_id")
            url = r.get("source_url") or r.get("url")
            if not url:
                slug = r.get("slug")
                if slug:
                    url = f"https://www.metalocus.es/en/news/{slug}"
            if bid and url:
                out[str(bid)] = str(url)
    return out


def _backfill_metalocus_with_slug(row, b_url_map, counts):
    refs = (row.get("source_refs") or {}).get("metalocus") or []
    if not refs:
        return
    su = dict(row.get("source_urls") or {})
    existing = list(su.get("metalocus") or [])
    existing_set = set(existing)
    changed = False
    for bid in refs:
        url = b_url_map.get(str(bid))
        if url and url not in existing_set:
            existing.append(url)
            existing_set.add(url)
            changed = True
            counts["metalocus_url_backfilled_c22"] += 1
    if changed:
        su["metalocus"] = existing
        row["source_urls"] = su


# Phase D — country dirty filter expanded
_VALID_COMMA_PREFIXES = {
    "korea, democratic people's republic of",
    "korea, republic of",
    "iran, islamic republic of",
    "iran, (islamic republic of)",
    "macedonia, the former yugoslav republic of",
    "moldova, republic of",
    "tanzania, united republic of",
    "venezuela, bolivarian republic of",
    "bolivia, plurinational state of",
    "palestine, state of",
    "taiwan, province of china",
    "viet nam, socialist republic of",
    "vietnam, socialist republic of",
    "china, people's republic of",
}


def _is_dirty_country_expanded(value):
    if not isinstance(value, str):
        return True
    s = value.strip()
    if not s:
        return True
    sl = s.casefold()
    if sl in _VALID_COMMA_PREFIXES:
        return False
    if sl in _NATIVE_COUNTRY_ALIASES:
        return False
    # multi-token comma-separated → likely city/region/country mix
    if s.count(",") >= 1:
        return True
    # pure numeric / single char (legacy)
    if s.isdigit() or len(s) <= 2:
        return True
    return False


def _country_class_with_dirty_strip(countries, row_country):
    cleaned = [c for c in countries if not _is_dirty_country_expanded(c)]
    if not cleaned:
        return "resolved", set()
    norm = {_normalize_country_full(c) or "" for c in cleaned}
    norm.discard("")
    rc = _normalize_country_full(row_country) or ""
    if rc:
        norm.add(rc)
    if len(norm) > 1:
        return "real", norm
    return "resolved", norm


# Phase E — split sidecar dedup
def _dedup_split_sidecar():
    if not C21_SPLIT_SIDECAR.exists():
        return []
    seen_pairs = {}
    for line in C21_SPLIT_SIDECAR.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        rows = entry.get("rows") or []
        if len(rows) < 2:
            continue
        cids = tuple(sorted(r.get("cid") for r in rows[:2]))
        ph = entry.get("phash") or ""
        if cids not in seen_pairs or ph < seen_pairs[cids]["phash"]:
            seen_pairs[cids] = entry
    return list(seen_pairs.values())


# --- main ---
def main() -> int:
    for p in (C21, DEEP_AUDIT):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    b_url_map = _load_b_id_url_map_with_slug()
    print(f"loaded {len(b_url_map)} B-id → url mappings (with slug fallback)",
          file=sys.stderr)

    print("loading source DB country/year maps...", file=sys.stderr)
    src_map = _load_slug_country_year_map()

    split_pairs_deduped = _dedup_split_sidecar()
    print(f"split sidecar deduped: {len(split_pairs_deduped)}", file=sys.stderr)

    country_sidecar_rows = []
    year_sidecar_rows = []
    counter_classes = Counter()
    counts: Counter = Counter()
    n_in = n_out = 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C21):
            n_in += 1
            cid = row.get("canonical_bld_id")

            # A — metalocus URL backfill with slug fallback
            _backfill_metalocus_with_slug(row, b_url_map, counts)

            # B — 5 missing-phash unpublish
            if cid in PHASH_MISSING_UNPUBLISH and row.get("is_publishable"):
                _set_unpublish(row, "images_missing_phash")
                counts["unpublish_missing_phash_c22"] += 1

            # C — cover dup force-swap
            if cid in COVER_AVOID:
                _force_swap_cover(row, COVER_AVOID[cid], counts)

            # D — country reclassify with expanded dirty filter
            if row.get("is_publishable"):
                pairs = _row_source_country_year(row, src_map)
                countries = [c for _, c, _ in pairs if c]
                years = [y for _, _, y in pairs if isinstance(y, int)]
                cls, norm = _country_class_with_dirty_strip(
                    countries, row.get("location_country"))
                counter_classes[cls] += 1
                if cls == "real":
                    country_sidecar_rows.append({
                        "cid": cid, "name": row.get("name"),
                        "row_country": row.get("location_country"),
                        "source_countries_raw": countries,
                        "normalized": sorted(norm),
                        "source_refs": row.get("source_refs") or {},
                    })
                    reasons = list(row.get("publishability_reasons") or [])
                    if "country_disputed" not in reasons:
                        reasons.append("country_disputed")
                    row["publishability_reasons"] = reasons
                else:
                    reasons = [r for r in (row.get("publishability_reasons") or [])
                               if r != "country_disputed"]
                    row["publishability_reasons"] = reasons

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

    COUNTRY_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    with COUNTRY_SIDECAR.open("w", encoding="utf-8") as f:
        for r in country_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with YEAR_SIDECAR.open("w", encoding="utf-8") as f:
        for r in year_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with SPLIT_SUSPECT_SIDECAR.open("w", encoding="utf-8") as f:
        for entry in split_pairs_deduped:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C21.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_in == n_out,
        "counts": dict(counts),
        "country_sidecar_entries": len(country_sidecar_rows),
        "year_sidecar_entries": len(year_sidecar_rows),
        "split_sidecar_entries": len(split_pairs_deduped),
        "country_classes": dict(counter_classes),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C22 polish [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
