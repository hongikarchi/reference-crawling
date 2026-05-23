#!/usr/bin/env python3
"""C20 final polish — phash backfill + architect branding strip + URL canon
fix + sidecar refine.

Addresses Codex C19 deep audit residuals:
  A. Architizer URL canon base-length ≥ 3 (fix in canonical_v2_c16_url_canon).
  B. Architect name source-branding strip (3,130) — " - Architizer" etc.
  C. Source_url gap backfill (51 publishable).
  D. Cover phash backfill via download + imagehash.phash (113 rows).
  E. Cross-card cover phash dup residual (3 groups).
  F. Country sidecar 3-tier reclassify (alias-fixable / dirty / real).
  G. Year + split sidecar refresh + suspicious_city extended.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import httpx
import imagehash
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import _build_source_url, _display_cover_url  # noqa: E402
from tools.canonical_v2_c16_url_canon import (  # noqa: E402
    _canonical_asset_key, _is_gif, _is_lowres_url, _is_raster_url,
)
from tools.canonical_v2_c17_make_web_polish import (  # noqa: E402
    _normalize_country_ext,
)
from tools.canonical_v2_c18_make_web_polish import (  # noqa: E402
    _dedup_with_phash_priority, _set_unpublish,
)
from tools.canonical_v2_c19_make_web_polish import (  # noqa: E402
    _load_slug_country_year_map, _row_source_country_year,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C19 = CCR / "canonical_buildings_strict.completeness_c19_make_web_polish.json"
DEEP_AUDIT = ROOT / "data/reports/canonical_v2_c19_make_web_deep_audit.codex.json"
CRAWL = ROOT / "data/crawl"

OUT = CCR / "canonical_buildings_strict.completeness_c20_make_web_polish.json"
REPORT = ROOT / "data/reports/canonical_v2_c20_make_web_polish_report.json"
COUNTRY_SIDECAR = ROOT / "data/reports/canonical_v2_c20_country_conflict_sidecar.jsonl"
YEAR_SIDECAR = ROOT / "data/reports/canonical_v2_c20_year_conflict_sidecar.jsonl"
SPLIT_SUSPECT_SIDECAR = ROOT / "data/reports/canonical_v2_c20_split_suspect_sidecar.jsonl"
PHASH_LOG = ROOT / "data/reports/canonical_v2_c20_phash_backfill_log.json"

# Phase B — architect branding strip
_ARCH_BRAND_RE = re.compile(
    r"\s*[\-|/]\s*(architizer|archello|divisare|metalocus)\s*$",
    re.I,
)

# Phase G — dirty source country (postcode/numeric/address)
_DIRTY_COUNTRY_RE = re.compile(
    r"^[\d\s.,\-/]+$"
    r"|^.{1,2}$",
)

# Phase G — extended suspicious city (preserve `Tokyo (Shibuya)` etc.)
_SUSPICIOUS_CITY_C20 = re.compile(
    r"^\s*("
    r"\d+[a-z]?"
    r"|[A-Za-z]"
    r"|(19|20)\d{2}\s*[-–]"
    r"|.*,\s*(via|rua|street|st\.?|avenue|ave|road|rd\.?|calle|strasse|str\.?)\s+\S+"
    r"|[-–]+"
    r"|\d{5}(-\d{4})?"
    r"|tbd|address|location|n/a|none|unknown"
    r")\s*$",
    re.I,
)


def _is_suspicious_city(city):
    return isinstance(city, str) and bool(_SUSPICIOUS_CITY_C20.match(city))


def _strip_architect_brand(name):
    if not isinstance(name, str):
        return name, False
    new = _ARCH_BRAND_RE.sub("", name).strip()
    return new, new != name


def _strip_architect_brand_text(text):
    if not isinstance(text, str):
        return text, False
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    new_parts = []
    changed = False
    for p in parts:
        np_, ch = _strip_architect_brand(p)
        if np_:
            new_parts.append(np_)
        if ch:
            changed = True
    new_text = ", ".join(new_parts)
    return new_text, changed


# Phase D — cover phash backfill
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ArchiTinder/1.0)",
    "Accept": "image/jpeg,image/png,image/webp,*/*;q=0.8",
}


def _download_phash(url, client):
    try:
        r = client.get(url, headers=_HEADERS, timeout=10.0, follow_redirects=True)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content))
        ph = imagehash.phash(img, hash_size=16)
        return str(ph), None
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__


def _load_cover_phash_targets():
    """Load all 113 cids needing cover phash from deep audit + scan C19 for full
    list (audit samples are limited to 12)."""
    cids = set()
    if DEEP_AUDIT.exists():
        d = json.loads(DEEP_AUDIT.read_text(encoding="utf-8"))
        for r in d.get("samples", {}).get("pub_cover_missing_matched_phash", []):
            cids.add(r["cid"])
    # also scan C19 to find full set: publishable + display_cover_url in
    # all_images but image's phash is None.
    for row in iter_buildings(C19):
        if not row.get("is_publishable"):
            continue
        dcu = row.get("display_cover_url")
        if not dcu:
            continue
        imgs = row.get("all_images") or []
        matched = next((im for im in imgs
                        if isinstance(im, dict) and im.get("url") == dcu), None)
        if matched and not matched.get("phash"):
            cids.add(row["canonical_bld_id"])
    return cids


def _phash_backfill(c19_rows_index, target_cids):
    """Returns dict cid -> {url, phash, error}."""
    results = {}
    with httpx.Client(http2=False, verify=True) as client:
        for i, cid in enumerate(sorted(target_cids), 1):
            row = c19_rows_index.get(cid)
            if not row:
                continue
            url = row.get("display_cover_url")
            if not url:
                continue
            ph, err = _download_phash(url, client)
            results[cid] = {"url": url, "phash": ph, "error": err}
            if i % 10 == 0:
                print(f"  phash backfill: {i}/{len(target_cids)}",
                      file=sys.stderr)
    return results


# Phase C — source_url gap
def _load_slug_only(source, ids):
    if source == "metalocus" or not ids:
        return {}
    table = {"archello": "archello_projects", "architizer": "architizer_projects",
             "divisare": "divisare_projects"}.get(source)
    if not table:
        return {}
    out = {}
    int_ids = []
    for sid in ids:
        try:
            int_ids.append(int(sid))
        except (TypeError, ValueError):
            continue
    if not int_ids:
        return {}
    db_file = {"archello": "archello.db", "architizer": "architizer.db",
               "divisare": "divisare.db"}[source]
    conn = sqlite3.connect(str(CRAWL / db_file))
    try:
        ph = ",".join("?" * len(int_ids))
        for sid, slug in conn.execute(
            f"SELECT id, slug FROM {table} WHERE id IN ({ph})", int_ids
        ):
            if slug:
                out[str(sid)] = str(slug)
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


# Phase E — cover dup avoid (uses cids only; need to look up display_cover from
# the live row to derive asset key).
def _load_cover_dup_groups(c19_rows_index):
    d = json.loads(DEEP_AUDIT.read_text(encoding="utf-8"))
    avoid = defaultdict(lambda: {"asset_keys": set(), "phashes": set()})
    sidecar = []
    samples = d.get("samples", {}).get("cross_card_duplicate_cover_phash", [])
    for g in samples:
        ph = g.get("phash")
        cids = g.get("cids") or []
        if len(cids) < 2:
            continue
        # rank by source count (winner first)
        ranked = sorted(cids, key=lambda c: -len(
            (c19_rows_index.get(c, {}).get("source_refs") or {})
        ))
        winner_cid = ranked[0]
        winner_row = c19_rows_index.get(winner_cid, {})
        winner_dcu = winner_row.get("display_cover_url")
        winner_key = _canonical_asset_key(winner_dcu)
        for cid in ranked[1:]:
            if winner_key:
                avoid[cid]["asset_keys"].add(winner_key)
            if ph:
                avoid[cid]["phashes"].add(ph)
        sidecar.append({
            "phash": ph,
            "winner_cid": winner_cid,
            "swap_cids": list(ranked[1:]),
            "rows": [{"cid": cid,
                      "name": c19_rows_index.get(cid, {}).get("name"),
                      "country": c19_rows_index.get(cid, {}).get("location_country"),
                      "display_cover_url": c19_rows_index.get(cid, {}).get("display_cover_url")}
                     for cid in cids],
        })
    return avoid, sidecar


# Phase C — source_url gap (collect from audit sample + scan)
def _load_source_url_gap_targets(c19_rows_index):
    """Returns dict cid -> {source: [missing_ids]}."""
    out = defaultdict(lambda: defaultdict(list))
    d = json.loads(DEEP_AUDIT.read_text(encoding="utf-8"))
    for r in d.get("samples", {}).get("source_url_gap_by_ref_key", []):
        cid = r.get("cid")
        src = r.get("source")
        for sid in r.get("ids") or []:
            out[cid][src].append(str(sid))
    # full scan: for each publishable row, source_refs vs source_urls
    for cid, row in c19_rows_index.items():
        if not row.get("is_publishable"):
            continue
        refs = row.get("source_refs") or {}
        urls = row.get("source_urls") or {}
        for src, ids in refs.items():
            existing = urls.get(src) or []
            if len(existing) >= len(ids or []):
                continue
            # find which ids are missing — heuristic: any ids beyond what's in
            # source_urls is missing. We can't match url-to-id without slug
            # lookup, so flag if count differs.
            for sid in ids or []:
                if sid not in out[cid][src]:
                    out[cid][src].append(str(sid))
    return out


def _backfill_source_urls(row, gap_map, slug_index, counts):
    if not gap_map:
        return
    cid = row.get("canonical_bld_id")
    missing = gap_map.get(cid)
    if not missing:
        return
    su = dict(row.get("source_urls") or {})
    changed = False
    for src, ids in missing.items():
        if src == "metalocus":
            # metalocus URLs typically already in source_urls; skip
            continue
        for sid in ids:
            slug = slug_index.get(src, {}).get(str(sid))
            meta = {"slug": slug}
            built = _build_source_url(src, str(sid), meta)
            if built:
                lst = list(su.get(src) or [])
                if built not in lst:
                    lst.append(built)
                    su[src] = lst
                    changed = True
                    counts["source_url_backfilled_c20"] += 1
    if changed:
        row["source_urls"] = su


# Phase F — country sidecar reclassify
def _is_dirty_country(value):
    if not isinstance(value, str):
        return True
    s = value.strip()
    if not s:
        return True
    return bool(_DIRTY_COUNTRY_RE.match(s))


def _country_reclassify(countries, row_country):
    """Returns ('alias_fixable' | 'real' | 'resolved', normalized_set_after_clean)."""
    cleaned = []
    for c in countries:
        if _is_dirty_country(c):
            continue
        cleaned.append(c)
    if not cleaned:
        return "resolved", set()
    norm = {_normalize_country_ext(c) or "" for c in cleaned}
    norm.discard("")
    rc = _normalize_country_ext(row_country) or ""
    if rc:
        norm.add(rc)
    if len(norm) <= 1:
        # alias-fixable (originals differed but normalize matched)
        return ("alias_fixable" if len(set(cleaned)) > 1 else "resolved"), norm
    return "real", norm


# --- main ---
def main() -> int:
    for p in (C19, DEEP_AUDIT):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    deep = json.loads(DEEP_AUDIT.read_text(encoding="utf-8"))
    samples = deep.get("samples", {})

    print("loading source DB country/year maps...", file=sys.stderr)
    src_map = _load_slug_country_year_map()

    print("identifying cover phash backfill targets...", file=sys.stderr)
    c19_rows_index = {}
    for r in iter_buildings(C19):
        cid = r.get("canonical_bld_id")
        if cid:
            c19_rows_index[cid] = r
    phash_targets = _load_cover_phash_targets()
    print(f"  cover phash targets: {len(phash_targets)}", file=sys.stderr)

    if PHASH_LOG.exists():
        print(f"reusing existing phash log {PHASH_LOG.relative_to(ROOT)}",
              file=sys.stderr)
        phash_results = json.loads(PHASH_LOG.read_text(encoding="utf-8"))
    else:
        print("downloading + computing phash for covers...", file=sys.stderr)
        phash_results = _phash_backfill(c19_rows_index, phash_targets)
        PHASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        PHASH_LOG.write_text(json.dumps(phash_results, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    backfilled = sum(1 for v in phash_results.values() if v.get("phash"))
    print(f"  phash backfilled: {backfilled}/{len(phash_results)}",
          file=sys.stderr)

    cover_avoid, cover_dup_sidecar = _load_cover_dup_groups(c19_rows_index)
    # Build source_url gap targets + slug index
    gap_map = _load_source_url_gap_targets(c19_rows_index)
    by_src_ids = defaultdict(set)
    for cid, m in gap_map.items():
        for src, ids in m.items():
            for sid in ids:
                by_src_ids[src].add(sid)
    slug_index = {src: _load_slug_only(src, sorted(ids))
                  for src, ids in by_src_ids.items() if src != "metalocus"}

    # Sidecar source row sets for country/year (re-derived via full scan)
    print("computing country / year sidecars...", file=sys.stderr)
    country_sidecar_rows = []
    year_sidecar_rows = []
    counter_classes = Counter()
    counts: Counter = Counter()

    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C19):
            n_in += 1
            cid = row.get("canonical_bld_id")

            # B — architect brand strip
            names = row.get("architect_names") or []
            new_names = []
            for n in names:
                nn, ch = _strip_architect_brand(n)
                if nn:
                    new_names.append(nn)
                if ch:
                    counts["architect_name_brand_stripped"] += 1
            # dedupe preserving order
            seen = set()
            deduped = []
            for n in new_names:
                if n not in seen:
                    seen.add(n)
                    deduped.append(n)
            if deduped != names:
                row["architect_names"] = deduped
            text = row.get("architects_text")
            nt, ch = _strip_architect_brand_text(text)
            if ch:
                row["architects_text"] = nt
                counts["architects_text_brand_stripped"] += 1

            # D — apply phash backfill if applicable
            if cid in phash_results:
                ph = phash_results[cid].get("phash")
                if ph:
                    target_url = phash_results[cid].get("url")
                    for im in row.get("all_images") or []:
                        if isinstance(im, dict) and im.get("url") == target_url:
                            im["phash"] = ph
                            counts["phash_backfilled_in_all_images"] += 1
                            break
                    bipc = row.get("best_image_per_cluster") or {}
                    if isinstance(bipc, dict):
                        for k, im in bipc.items():
                            if isinstance(im, dict) and im.get("url") == target_url:
                                im["phash"] = ph
                                counts["phash_backfilled_in_bipc"] += 1
                                break

            # E — cover phash dup swap
            if cid in cover_avoid:
                spec = cover_avoid[cid]
                bad_keys = spec["asset_keys"]
                bad_ph = spec["phashes"]
                dcu = row.get("display_cover_url")
                if dcu and (_canonical_asset_key(dcu) in bad_keys):
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
                        counts["cover_dup_swapped_c20"] += 1
                    elif row.get("is_publishable"):
                        _set_unpublish(row, "cover_duplicate_no_alt")
                        counts["unpublish_cover_dup_no_alt_c20"] += 1

            # C — source_url backfill
            _backfill_source_urls(row, gap_map, slug_index, counts)

            # G — re-dedup with phash priority (catches Architizer base-fix
            # induced collapses from short bases)
            new_imgs, excess = _dedup_with_phash_priority(row.get("all_images") or [])
            if excess:
                row["all_images"] = new_imgs
                counts["redup_c20_rows"] += 1
                counts["redup_c20_excess"] += excess

            # G — suspicious city extended
            city = row.get("location_city")
            if _is_suspicious_city(city):
                row["location_city"] = None
                counts["suspicious_city_nulled_c20"] += 1

            # F — country sidecar (publishable only, with raw source query)
            if row.get("is_publishable"):
                pairs = _row_source_country_year(row, src_map)
                countries = [c for _, c, _ in pairs if c]
                years = [y for _, _, y in pairs if isinstance(y, int)]
                cls, norm = _country_reclassify(countries, row.get("location_country"))
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

                # year sidecar
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

    with COUNTRY_SIDECAR.open("w", encoding="utf-8") as f:
        for r in country_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with YEAR_SIDECAR.open("w", encoding="utf-8") as f:
        for r in year_sidecar_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with SPLIT_SUSPECT_SIDECAR.open("w", encoding="utf-8") as f:
        for p in samples.get("split_suspect_sidecar", []):
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    counts["country_class_real"] = counter_classes["real"]
    counts["country_class_alias_fixable"] = counter_classes["alias_fixable"]
    counts["country_class_resolved"] = counter_classes["resolved"]
    counts["cover_dup_groups"] = len(cover_dup_sidecar)

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C19.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_in == n_out,
        "counts": dict(counts),
        "country_sidecar_entries": len(country_sidecar_rows),
        "year_sidecar_entries": len(year_sidecar_rows),
        "phash_targets": len(phash_targets),
        "phash_backfilled": backfilled,
        "phash_failed": len(phash_results) - backfilled,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C20 polish [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
