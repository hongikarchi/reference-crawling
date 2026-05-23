#!/usr/bin/env python3
"""C19 short cleanup — BIPC non-raster + extension whitelist + cover residuals
+ sidecar regen.

Addresses Codex C18 re-audit residuals (short cycle):
  A. raster whitelist (in canonical_v2_c16_url_canon.py) now uses a strict
     allow-list; previously-passed `.mp4`/`.db`/`.url`/`.docx`/`.jp2`/`.pg`/
     `.jpe` URLs get filtered.
  B. best_image_per_cluster non-raster strip (forgotten in C18) — 2k rows.
  C. cover residual 4 (3 lowres + 1 GIF) + cover phash dup 3 + missing-phash
     5 → swap or unpublish (raster-only filter).
  D. sidecars regen by full C18-dataset scan + Metalocus source_urls join.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

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
    _dedup_with_phash_priority, _row_cover_in_images, _row_has_phash,
    _set_unpublish, _strip_non_raster,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C18 = CCR / "canonical_buildings_strict.completeness_c18_make_web_polish.json"
ACTIONABLE = ROOT / "data/reports/canonical_v2_c18_make_web_actionable_issues.codex_audit.json"
GATE = ROOT / "data/reports/canonical_v2_c18_make_web_quality_gate.codex_audit.json"
CRAWL = ROOT / "data/crawl"

OUT = CCR / "canonical_buildings_strict.completeness_c19_make_web_polish.json"
REPORT = ROOT / "data/reports/canonical_v2_c19_make_web_polish_report.json"
COUNTRY_SIDECAR = ROOT / "data/reports/canonical_v2_c19_country_conflict_sidecar.jsonl"
YEAR_SIDECAR = ROOT / "data/reports/canonical_v2_c19_year_conflict_sidecar.jsonl"
SPLIT_SUSPECT_SIDECAR = ROOT / "data/reports/canonical_v2_c19_split_suspect_sidecar.jsonl"


# --- Phase B: BIPC non-raster strip ---
def _strip_bipc_non_raster(row, counts):
    bipc = row.get("best_image_per_cluster")
    if not isinstance(bipc, dict) or not bipc:
        return
    new_bipc = {}
    stripped = 0
    for k, im in bipc.items():
        if isinstance(im, dict) and im.get("url") and not _is_raster_url(im.get("url")):
            stripped += 1
            continue
        new_bipc[k] = im
    if stripped:
        row["best_image_per_cluster"] = new_bipc
        counts["bipc_non_raster_stripped"] += stripped
        counts["bipc_non_raster_rows"] += 1


# --- Phase C: cover residual swap / unpublish ---
def _repick_raster_cover_safe(row):
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
        return "repicked_full"
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
    return "no_alternative"


def _load_cover_avoid_c18():
    avoid = defaultdict(lambda: {"asset_keys": set(), "phashes": set()})
    gate = json.loads(GATE.read_text(encoding="utf-8"))
    samples = gate.get("samples", {})
    for g in samples.get("cross_card_display_phash_duplicate_groups", []):
        rows = g.get("rows") or []
        if len(rows) < 2:
            continue
        ph = g.get("phash")
        ranked = sorted(rows, key=lambda r: -len(r.get("source_refs") or {}))
        winner_key = _canonical_asset_key(ranked[0].get("display_cover_url"))
        for r in ranked[1:]:
            if winner_key:
                avoid[r["cid"]]["asset_keys"].add(winner_key)
            if ph:
                avoid[r["cid"]]["phashes"].add(ph)
    for g in samples.get("near_display_cover_phash_pairs_hamming_le8", []):
        rows = g.get("rows") or []
        phashes = g.get("phashes") or []
        if len(rows) < 2:
            continue
        ranked = sorted(rows, key=lambda r: -len(r.get("source_refs") or {}))
        for r in ranked[1:]:
            for ph in phashes:
                avoid[r["cid"]]["phashes"].add(ph)
            wk = _canonical_asset_key(ranked[0].get("display_cover_url"))
            if wk:
                avoid[r["cid"]]["asset_keys"].add(wk)
    return avoid


# --- Phase D: source country / year sidecar via full scan ---
def _load_slug_country_year_map():
    """Pre-load country + year per source DB id. Returns
    {source: {sid: (country, year)}}."""
    out = {}
    out["archello"] = {}
    out["architizer"] = {}
    out["divisare"] = {}
    out["metalocus_by_url"] = {}
    try:
        conn = sqlite3.connect(str(CRAWL / "archello.db"))
        for r in conn.execute(
            "SELECT id, location_country, project_year FROM archello_projects"
        ):
            out["archello"][str(r[0])] = (r[1], r[2])
        conn.close()
    except sqlite3.Error:
        pass
    try:
        conn = sqlite3.connect(str(CRAWL / "architizer.db"))
        for r in conn.execute(
            "SELECT id, location_country, completion_year FROM architizer_projects"
        ):
            out["architizer"][str(r[0])] = (r[1], r[2])
        conn.close()
    except sqlite3.Error:
        pass
    try:
        conn = sqlite3.connect(str(CRAWL / "divisare.db"))
        for r in conn.execute(
            "SELECT id, location_country, project_year FROM divisare_projects"
        ):
            out["divisare"][str(r[0])] = (r[1], r[2])
        conn.close()
    except sqlite3.Error:
        pass
    # metalocus: join articles.url → buildings (article_id)
    try:
        conn = sqlite3.connect(str(CRAWL / "metalocus.db"))
        for r in conn.execute(
            "SELECT a.url, b.country, b.year FROM articles a "
            "JOIN buildings b ON b.article_id = a.id"
        ):
            url, country, year = r
            try:
                y = int(str(year).strip()) if year else None
                y = y if y and 1500 < y < 2200 else None
            except (TypeError, ValueError):
                y = None
            out["metalocus_by_url"][str(url)] = (country, y)
        conn.close()
    except sqlite3.Error:
        pass
    return out


def _row_source_country_year(row, src_map):
    """Returns list of (source, raw_country, raw_year) per source-ref."""
    out = []
    refs = row.get("source_refs") or {}
    urls = row.get("source_urls") or {}
    for src, ids in refs.items():
        if src == "metalocus":
            for u in urls.get("metalocus") or []:
                pair = src_map["metalocus_by_url"].get(str(u))
                if pair:
                    out.append((src, pair[0], pair[1]))
        else:
            tbl = src_map.get(src) or {}
            for sid in ids or []:
                pair = tbl.get(str(sid))
                if pair:
                    out.append((src, pair[0], pair[1]))
    return out


def _is_real_country_conflict(country_list, row_country):
    norm = {_normalize_country_ext(c) or "" for c in country_list}
    norm.discard("")
    rc = _normalize_country_ext(row_country) or ""
    if rc:
        norm.add(rc)
    return len(norm) > 1, norm


def _is_real_year_conflict(year_list, row_year):
    years = [y for y in year_list if isinstance(y, int)]
    if not years:
        return False
    if row_year is not None:
        years.append(row_year)
    spread = max(years) - min(years)
    return spread > 2


# --- main ---
def main() -> int:
    for p in (C18, ACTIONABLE, GATE):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    iss = actionable["issues"]
    cover_lowres_cids = {r["cid"] for r in iss.get("display_cover_lowres_or_thumb", [])}
    cover_gif_cids = {r["cid"] for r in iss.get("display_cover_gif", [])}
    missing_phash_cids = {r["cid"] for r in iss.get("publishable_all_images_missing_phash", [])}
    cover_avoid = _load_cover_avoid_c18()
    split_suspect_pairs = iss.get("shared_phash_split_suspect_pairs", []) or []

    print("loading source DB country/year maps...", file=sys.stderr)
    src_map = _load_slug_country_year_map()
    print(f"  archello: {len(src_map['archello'])}, architizer: {len(src_map['architizer'])}, "
          f"divisare: {len(src_map['divisare'])}, metalocus_url: {len(src_map['metalocus_by_url'])}",
          file=sys.stderr)

    SPLIT_SUSPECT_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    with SPLIT_SUSPECT_SIDECAR.open("w", encoding="utf-8") as f:
        for p in split_suspect_pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    country_sidecar_rows = []
    year_sidecar_rows = []

    counts: Counter = Counter()
    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C18):
            n_in += 1
            cid = row.get("canonical_bld_id")

            # A — non-raster strip (new whitelist) on all_images + cover fields
            outcome = _strip_non_raster(row, counts)
            if outcome == "unpublishable_no_alt" and row.get("is_publishable"):
                _set_unpublish(row, "cover_non_raster_no_alt")
                counts["unpublish_cover_non_raster_c19"] += 1

            # B — BIPC non-raster strip
            _strip_bipc_non_raster(row, counts)

            # B-2 — re-dedup with phash priority (catch any new dups exposed)
            new_imgs, excess = _dedup_with_phash_priority(row.get("all_images") or [])
            if excess:
                row["all_images"] = new_imgs
                counts["redup_rows_c19"] += 1
                counts["redup_excess_c19"] += excess

            # C — cover residual lowres/GIF
            if row.get("is_publishable") and (cid in cover_lowres_cids
                                              or cid in cover_gif_cids):
                action = _repick_raster_cover_safe(row)
                if action == "repicked_full":
                    counts["cover_lowres_swapped_full"] += 1
                else:
                    _set_unpublish(row, "cover_lowres_or_gif")
                    counts["unpublish_cover_lowres_or_gif_c19"] += 1

            # C — cover phash dup residual
            if cid in cover_avoid:
                spec = cover_avoid[cid]
                bad_keys = spec["asset_keys"]
                bad_ph = spec["phashes"]
                dcu = row.get("display_cover_url")
                if dcu and _canonical_asset_key(dcu) in bad_keys:
                    images_filt = [im for im in row.get("all_images") or []
                                   if isinstance(im, dict)
                                   and _canonical_asset_key(im.get("url")) not in bad_keys
                                   and im.get("phash") not in bad_ph
                                   and _is_raster_url(im.get("url"))
                                   and not _is_lowres_url(im.get("url"))
                                   and not _is_gif(im.get("url"))]
                    new_dcu = _display_cover_url(
                        covers_by_type={k: v for k, v in (row.get("covers_by_type") or {}).items()
                                        if not v or _canonical_asset_key(v) not in bad_keys},
                        cover_image_url_default=(row.get("cover_image_url_default")
                                                 if row.get("cover_image_url_default")
                                                 and _canonical_asset_key(row.get("cover_image_url_default")) not in bad_keys
                                                 else None),
                        all_images=images_filt,
                    )
                    if new_dcu:
                        row["display_cover_url"] = new_dcu
                        counts["cover_dup_swapped_c19"] += 1
                    elif row.get("is_publishable"):
                        _set_unpublish(row, "cover_duplicate_no_alt")
                        counts["unpublish_cover_dup_no_alt_c19"] += 1

            # C — cover-not-in-images residual via universal check
            if row.get("is_publishable") and not _row_cover_in_images(row):
                action = _repick_raster_cover_safe(row)
                if action == "no_alternative":
                    _set_unpublish(row, "cover_invalid_no_alternative")
                    counts["unpublish_cover_no_alt_c19"] += 1
                else:
                    counts[f"cover_repick_{action}_c19"] += 1

            # C — missing-phash residual unpublish
            if (row.get("is_publishable") and cid in missing_phash_cids
                    and not _row_has_phash(row)):
                _set_unpublish(row, "images_missing_phash")
                counts["unpublish_missing_phash_c19"] += 1

            # D — sidecar collection (publishable only)
            if row.get("is_publishable"):
                pairs = _row_source_country_year(row, src_map)
                countries = [c for _, c, _ in pairs if c]
                years = [y for _, _, y in pairs if isinstance(y, int)]
                is_c_conflict, norm_set = _is_real_country_conflict(
                    countries, row.get("location_country"))
                if is_c_conflict:
                    country_sidecar_rows.append({
                        "cid": cid, "name": row.get("name"),
                        "row_country": row.get("location_country"),
                        "source_countries_raw": countries,
                        "normalized": sorted(norm_set),
                        "source_refs": row.get("source_refs") or {},
                    })
                    counts["country_conflict_real"] += 1
                if _is_real_year_conflict(years, row.get("project_year")):
                    year_sidecar_rows.append({
                        "cid": cid, "name": row.get("name"),
                        "row_year": row.get("project_year"),
                        "source_years": years,
                        "source_refs": row.get("source_refs") or {},
                    })
                    counts["year_conflict_real"] += 1

            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    with COUNTRY_SIDECAR.open("w", encoding="utf-8") as f:
        for r in country_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with YEAR_SIDECAR.open("w", encoding="utf-8") as f:
        for r in year_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C18.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_in == n_out,
        "counts": dict(counts),
        "country_sidecar_entries": len(country_sidecar_rows),
        "year_sidecar_entries": len(year_sidecar_rows),
        "split_suspect_entries": len(split_suspect_pairs),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C19 polish [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
