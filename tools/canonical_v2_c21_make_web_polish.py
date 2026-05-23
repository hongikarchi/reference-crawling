#!/usr/bin/env python3
"""C21 short cleanup — metalocus source_url + phash-based cover dup swap +
native-name country alias + split sidecar full re-derive.

Addresses Codex C20 deep audit residuals:
  A. metalocus source_url backfill (50) via 4_buildings_final.json B-id → url.
  B. 4 cover-not-in-all_images re-pick + 9 phash-missing rows unpublish.
  C. 3 cross-card cover phash dup — PHASH-based swap (C20 asset-key swap
     failed because winner/loser cover URLs had different Cloudinary IDs but
     same phash).
  D. Split sidecar 35 full re-derive (C20 dumped only 8 audit samples).
  E. Country alias native-name expansion (España/Italia/The Netherlands/
     Türkiye/Deutschland/etc.) + sidecar reclassify.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import _display_cover_url  # noqa: E402
from tools.canonical_v2_c16_url_canon import (  # noqa: E402
    _canonical_asset_key, _is_gif, _is_lowres_url, _is_raster_url,
)
from tools.canonical_v2_c17_make_web_polish import (  # noqa: E402
    _normalize_country_ext,
)
from tools.canonical_v2_c18_make_web_polish import (  # noqa: E402
    _set_unpublish, _row_has_phash,
)
from tools.canonical_v2_c19_make_web_polish import (  # noqa: E402
    _load_slug_country_year_map, _row_source_country_year,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C20 = CCR / "canonical_buildings_strict.completeness_c20_make_web_polish.json"
DEEP_AUDIT = ROOT / "data/reports/canonical_v2_c20_make_web_deep_audit.codex.json"
B_ID_MAP = ROOT / "data/enrich/4_buildings_final.json"

OUT = CCR / "canonical_buildings_strict.completeness_c21_make_web_polish.json"
REPORT = ROOT / "data/reports/canonical_v2_c21_make_web_polish_report.json"
COUNTRY_SIDECAR = ROOT / "data/reports/canonical_v2_c21_country_conflict_sidecar.jsonl"
YEAR_SIDECAR = ROOT / "data/reports/canonical_v2_c21_year_conflict_sidecar.jsonl"
SPLIT_SUSPECT_SIDECAR = ROOT / "data/reports/canonical_v2_c21_split_suspect_sidecar.jsonl"

# Phase E — native-name country alias
_NATIVE_COUNTRY_ALIASES = {
    "españa": "Spain",
    "italia": "Italy",
    "the netherlands": "Netherlands",
    "nederland": "Netherlands",
    "türkiye": "Turkey",
    "turkiye": "Turkey",
    "deutschland": "Germany",
    "österreich": "Austria",
    "osterreich": "Austria",
    "schweiz": "Switzerland",
    "suisse": "Switzerland",
    "svizzera": "Switzerland",
    "svizra": "Switzerland",
    "sverige": "Sweden",
    "norge": "Norway",
    "danmark": "Denmark",
    "suomi": "Finland",
    "magyarország": "Hungary",
    "magyarorszag": "Hungary",
    "polska": "Poland",
    "česko": "Czechia",
    "cesko": "Czechia",
    "česká republika": "Czechia",
    "hrvatska": "Croatia",
    "slovensko": "Slovakia",
    "slovenija": "Slovenia",
    "ελλάδα": "Greece",
    "ellada": "Greece",
    "belgique": "Belgium",
    "belgïe": "Belgium",
    "belgië": "Belgium",
    "belgie": "Belgium",
    "luxemburg": "Luxembourg",
    "lëtzebuerg": "Luxembourg",
    "ísland": "Iceland",
    "island": "Iceland",
    "ireland": "Ireland",
    "éire": "Ireland",
    "eire": "Ireland",
    "portugal": "Portugal",
    "中国": "China",
    "中華人民共和國": "China",
    "中華民國": "Taiwan",
    "台灣": "Taiwan",
    "日本": "Japan",
    "한국": "South Korea",
    "대한민국": "South Korea",
    "조선": "North Korea",
    "조선민주주의인민공화국": "North Korea",
    "ประเทศไทย": "Thailand",
    "việt nam": "Vietnam",
    "viet nam": "Vietnam",
    "الإمارات": "United Arab Emirates",
    "الإمارات العربية المتحدة": "United Arab Emirates",
    "السعودية": "Saudi Arabia",
    "المملكة العربية السعودية": "Saudi Arabia",
    "مصر": "Egypt",
    "brasil": "Brazil",
    "méxico": "Mexico",
    "mexico": "Mexico",
    "россия": "Russia",
    "российская федерация": "Russia",
    "україна": "Ukraine",
    "ukraina": "Ukraine",
    "rumänien": "Romania",
    "românia": "Romania",
}


def _normalize_country_full(value):
    """Extended ext normalizer including native-names."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text_nfc = unicodedata.normalize("NFC", text)
    key = " ".join(text_nfc.split()).casefold()
    if key in _NATIVE_COUNTRY_ALIASES:
        return _NATIVE_COUNTRY_ALIASES[key]
    return _normalize_country_ext(text)


# Phase A — metalocus source_url backfill
def _load_b_id_url_map():
    if not B_ID_MAP.exists():
        return {}
    d = json.loads(B_ID_MAP.read_text(encoding="utf-8"))
    out = {}
    if isinstance(d, list):
        for r in d:
            bid = r.get("building_id")
            url = r.get("source_url") or r.get("url")
            if bid and url:
                out[str(bid)] = str(url)
    return out


def _backfill_metalocus(row, b_url_map, counts):
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
            counts["metalocus_url_backfilled"] += 1
    if changed:
        su["metalocus"] = existing
        row["source_urls"] = su


# Phase C — phash-based cover dup avoid
def _load_cover_dup_phash_groups():
    d = json.loads(DEEP_AUDIT.read_text(encoding="utf-8"))
    samples = d.get("samples", {}).get("cross_card_duplicate_cover_phash", [])
    avoid_by_cid = defaultdict(set)
    sidecar = []
    for g in samples:
        ph = g.get("phash")
        cids = g.get("cids") or []
        if not ph or len(cids) < 2:
            continue
        # Could use full row data to pick winner but loser/winner symmetry
        # doesn't matter for our purpose — every cid loses an asset.
        # Convention: lowest cid is winner; rest swap.
        ordered = sorted(cids)
        sidecar.append({"phash": ph, "winner_cid": ordered[0],
                        "swap_cids": ordered[1:]})
        for cid in ordered[1:]:
            avoid_by_cid[cid].add(ph)
    return avoid_by_cid, sidecar


def _swap_cover_by_phash(row, avoid_phashes, counts):
    dcu = row.get("display_cover_url")
    if not dcu:
        return
    # Check if current cover's image has phash in avoid set
    images = row.get("all_images") or []
    cur_image = next((im for im in images
                      if isinstance(im, dict) and im.get("url") == dcu), None)
    if not cur_image:
        return
    cur_phash = cur_image.get("phash")
    if cur_phash not in avoid_phashes:
        return
    # Find non-avoid raster non-lowres non-gif image
    alt = next((im.get("url") for im in images
                if isinstance(im, dict) and im.get("url")
                and im.get("phash") not in avoid_phashes
                and _is_raster_url(im.get("url"))
                and not _is_lowres_url(im.get("url"))
                and not _is_gif(im.get("url"))), None)
    if not alt:
        # any raster non-gif
        alt = next((im.get("url") for im in images
                    if isinstance(im, dict) and im.get("url")
                    and im.get("phash") not in avoid_phashes
                    and _is_raster_url(im.get("url"))
                    and not _is_gif(im.get("url"))), None)
    if alt:
        row["display_cover_url"] = alt
        counts["cover_phash_dup_swapped"] += 1
    elif row.get("is_publishable"):
        _set_unpublish(row, "cover_duplicate_no_alt")
        counts["unpublish_cover_dup_no_alt_c21"] += 1


# Phase B — cover-not-in-images + missing-phash
def _load_cover_blocker_cids():
    d = json.loads(DEEP_AUDIT.read_text(encoding="utf-8"))
    sam = d.get("samples", {})
    cover_not_in = {r["cid"]
                    for r in sam.get("pub_cover_asset_not_in_all_images", [])}
    cover_missing_phash = {r["cid"]
                           for r in sam.get("pub_cover_missing_matched_phash", [])}
    return cover_not_in, cover_missing_phash


def _row_cover_in_images_strict(row):
    dcu = row.get("display_cover_url")
    if not dcu:
        return False
    cur_key = _canonical_asset_key(dcu)
    for im in row.get("all_images") or []:
        if isinstance(im, dict) and _canonical_asset_key(im.get("url")) == cur_key:
            return True
    return False


def _force_repick_raster(row):
    images = row.get("all_images") or []
    full = next(
        (im.get("url") for im in images
         if isinstance(im, dict) and im.get("url")
         and _is_raster_url(im.get("url"))
         and not _is_lowres_url(im.get("url"))
         and not _is_gif(im.get("url"))),
        None,
    )
    if full:
        row["display_cover_url"] = full
        return "repicked"
    any_raster = next(
        (im.get("url") for im in images
         if isinstance(im, dict) and im.get("url")
         and _is_raster_url(im.get("url"))
         and not _is_gif(im.get("url"))),
        None,
    )
    if any_raster:
        row["display_cover_url"] = any_raster
        return "repicked_lowres"
    return "no_alt"


# Phase D — split sidecar full re-derive (35 expected)
def _norm_name(s):
    return " ".join(unicodedata.normalize("NFKD", str(s or ""))
                    .encode("ascii", "ignore").decode().casefold().split())


def _split_sidecar_full(rows_index):
    """Re-derive cross-card phash dup pairs from full dataset (display cover
    phash + cross-card image phash dup); classify per arch + name similarity."""
    # Build display_cover phash → cid map
    cover_phash_to_cids = defaultdict(list)
    any_phash_to_cids = defaultdict(set)
    for cid, row in rows_index.items():
        if not row.get("is_publishable"):
            continue
        dcu = row.get("display_cover_url")
        for im in row.get("all_images") or []:
            if not isinstance(im, dict):
                continue
            ph = im.get("phash")
            if not ph:
                continue
            any_phash_to_cids[ph].add(cid)
            if im.get("url") == dcu:
                cover_phash_to_cids[ph].append(cid)
    # Cross-card pairs with cover phash shared (already detected) PLUS any-image
    # phash shared if it's a split-suspect (same arch + similar name).
    pairs = set()
    for ph, cids in any_phash_to_cids.items():
        if len(cids) < 2:
            continue
        cids_list = sorted(cids)
        for a, b in combinations(cids_list, 2):
            ra = rows_index.get(a)
            rb = rows_index.get(b)
            if not ra or not rb:
                continue
            arch_a = frozenset(ra.get("architect_canonical_ids") or [])
            arch_b = frozenset(rb.get("architect_canonical_ids") or [])
            overlap = arch_a & arch_b
            if not overlap:
                continue
            country_a = ra.get("location_country")
            country_b = rb.get("location_country")
            if not country_a or country_a != country_b:
                continue
            name_sim = fuzz.token_set_ratio(_norm_name(ra.get("name")),
                                            _norm_name(rb.get("name")))
            if name_sim < 80:
                continue
            pairs.add((a, b, ph, name_sim))
    return sorted(pairs)


def main() -> int:
    for p in (C20, DEEP_AUDIT):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    b_url_map = _load_b_id_url_map()
    print(f"loaded {len(b_url_map)} B-id → url mappings", file=sys.stderr)

    cover_avoid_phash, cover_dup_sidecar = _load_cover_dup_phash_groups()
    cover_not_in, cover_missing_phash = _load_cover_blocker_cids()

    print("loading source DB country/year maps...", file=sys.stderr)
    src_map = _load_slug_country_year_map()

    # Load all rows for split sidecar pass-1
    print("loading C20 rows for split sidecar derivation...", file=sys.stderr)
    rows_index = {}
    for r in iter_buildings(C20):
        rows_index[r.get("canonical_bld_id")] = r

    print("deriving split sidecar...", file=sys.stderr)
    split_pairs = _split_sidecar_full(rows_index)
    print(f"  split pairs: {len(split_pairs)}", file=sys.stderr)

    counts: Counter = Counter()
    country_sidecar_rows = []
    year_sidecar_rows = []
    counter_classes = Counter()

    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C20):
            n_in += 1
            cid = row.get("canonical_bld_id")

            # A — metalocus source_url backfill
            _backfill_metalocus(row, b_url_map, counts)

            # C — phash-based cover dup swap
            if cid in cover_avoid_phash:
                _swap_cover_by_phash(row, cover_avoid_phash[cid], counts)

            # B — cover-not-in-images force re-pick
            if cid in cover_not_in and not _row_cover_in_images_strict(row):
                action = _force_repick_raster(row)
                if action == "no_alt" and row.get("is_publishable"):
                    _set_unpublish(row, "cover_invalid_no_alternative")
                    counts["unpublish_cover_no_alt_c21"] += 1
                else:
                    counts[f"cover_repick_{action}_c21"] += 1

            # B — missing-phash residual unpublish (rows whose images are all
            # phash-less even after C20 backfill).
            if (cid in cover_missing_phash and row.get("is_publishable")
                    and not _row_has_phash(row)):
                _set_unpublish(row, "images_missing_phash")
                counts["unpublish_missing_phash_c21"] += 1

            # E — country sidecar reclassify (publishable only)
            if row.get("is_publishable"):
                pairs = _row_source_country_year(row, src_map)
                countries = [c for _, c, _ in pairs if c]
                years = [y for _, _, y in pairs if isinstance(y, int)]
                # Apply full normalization including native names
                norm = {_normalize_country_full(c) or "" for c in countries}
                norm.discard("")
                rc = _normalize_country_full(row.get("location_country")) or ""
                if rc:
                    norm.add(rc)
                if len(norm) > 1:
                    counter_classes["real"] += 1
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
                    counter_classes["resolved"] += 1
                    # remove stale country_disputed flag
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

    # Sidecars
    COUNTRY_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    with COUNTRY_SIDECAR.open("w", encoding="utf-8") as f:
        for r in country_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with YEAR_SIDECAR.open("w", encoding="utf-8") as f:
        for r in year_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with SPLIT_SUSPECT_SIDECAR.open("w", encoding="utf-8") as f:
        for a, b, ph, sim in split_pairs:
            ra = rows_index.get(a) or {}
            rb = rows_index.get(b) or {}
            f.write(json.dumps({
                "phash": ph, "name_sim": sim,
                "rows": [
                    {"cid": a, "name": ra.get("name"),
                     "country": ra.get("location_country"),
                     "city": ra.get("location_city"),
                     "year": ra.get("project_year"),
                     "arch": ra.get("architect_canonical_ids") or []},
                    {"cid": b, "name": rb.get("name"),
                     "country": rb.get("location_country"),
                     "city": rb.get("location_city"),
                     "year": rb.get("project_year"),
                     "arch": rb.get("architect_canonical_ids") or []},
                ],
            }, ensure_ascii=False) + "\n")

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C20.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_in == n_out,
        "counts": dict(counts),
        "country_sidecar_entries": len(country_sidecar_rows),
        "year_sidecar_entries": len(year_sidecar_rows),
        "split_sidecar_entries": len(split_pairs),
        "country_classes": dict(counter_classes),
        "cover_dup_groups": len(cover_dup_sidecar),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C21 polish [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
