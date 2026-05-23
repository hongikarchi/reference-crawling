#!/usr/bin/env python3
"""C18 make_web pre-upsert polish (round 4).

Addresses Codex C17 re-audit residuals:
  A. Non-raster URL strip — display_cover .tiff/.pdf/.ai/.psd (44) +
     all_images non-raster (~10k URLs). Strip + re-pick cover from raster.
  B. Asset dedup phash priority — prefer images WITH phash in tie-breaking,
     fixing the C17 regression that produced 18 publishable rows with all
     images phash-less.
  C. Cover-not-in-all_images (11) — force re-pick from raster only.
  D. Cover phash dup residual (3 exact + 10 near = 13) — swap or unpublish.
  E. Low-dimensions cover (8) — unpublish.
  F. Name+arch auto-merge (7 residual from C16) — same gate.
  G. Pixel House single country mismatch — source-majority override.
  H. Sidecar refresh (country 251, year 131, name-year 24, split-suspect 36).
  I. Suspicious city + string hygiene leftover extension (regex + NFC).
  J. Registry lineage update for any new C18 merge losers.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import _confidence_tier, _display_cover_url  # noqa: E402
from tools.canonical_v2_c16_url_canon import (  # noqa: E402
    _canonical_asset_key, _is_gif, _is_lowres_url, _is_raster_url,
)
from tools.canonical_v2_c17_make_web_polish import (  # noqa: E402
    _load_removed_cids_with_cycle, _normalize_country_ext,
)
from tools.canonical_v2_recover_dropped_twins import (  # noqa: E402
    _UF, _union, _is_empty, _ABSORB_IF_EMPTY, _ABSORB_UNION,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C17 = CCR / "canonical_buildings_strict.completeness_c17_make_web_polish.json"
ACTIONABLE = ROOT / "data/reports/canonical_v2_c17_make_web_actionable_issues.codex_audit.json"
GATE = ROOT / "data/reports/canonical_v2_c17_make_web_quality_gate.codex_audit.json"
REGISTRY = ROOT / "data/id_registry_buildings.json"
REGISTRY_BACKUP = ROOT / "data/id_registry_buildings.backup_pre_c18.json"

OUT = CCR / "canonical_buildings_strict.completeness_c18_make_web_polish.json"
REPORT = ROOT / "data/reports/canonical_v2_c18_make_web_polish_report.json"
COUNTRY_SIDECAR = ROOT / "data/reports/canonical_v2_c18_country_conflict_sidecar.jsonl"
YEAR_SIDECAR = ROOT / "data/reports/canonical_v2_c18_year_conflict_sidecar.jsonl"
NAME_YEAR_SIDECAR = ROOT / "data/reports/canonical_v2_c18_name_year_sidecar.jsonl"
SPLIT_SUSPECT_SIDECAR = ROOT / "data/reports/canonical_v2_c18_split_suspect_sidecar.jsonl"

_NAME_KEY = lambda s: " ".join(unicodedata.normalize("NFKD", str(s or "")).encode(
    "ascii", "ignore").decode().casefold().split())

# Extended suspicious-city regex (adds US ZIP-only, parenthesized address, etc.)
_SUSPICIOUS_CITY_EXT = re.compile(
    r"^\s*("
    r"\d+[a-z]?"
    r"|[A-Za-z]"
    r"|(19|20)\d{2}\s*[-–]"
    r"|.*,\s*(via|rua|street|st\.?|avenue|ave|road|rd\.?|calle|strasse|str\.?)\s+\S+"
    r"|[-–]+"
    r"|\d{5}(-\d{4})?"
    r"|tbd|address|location|n/a|none|unknown"
    r"|\(.+\)"
    r")\s*$",
    re.I,
)

# Extended control + BOM + zero-width + bidi + invisible chars
_HYGIENE_CHARS_RE = re.compile(
    r"["
    r"\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
    r"​-‏‪-‮⁠-⁯﻿"
    r"]"
)
_MULTI_SPACE_RE = re.compile(r"\s{2,}")


def _strip_str_full(s):
    if not isinstance(s, str):
        return s, False
    orig = s
    s = unicodedata.normalize("NFC", s)
    s = _HYGIENE_CHARS_RE.sub("", s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    s = s.strip()
    return s, s != orig


def _is_suspicious_city(city):
    return isinstance(city, str) and bool(_SUSPICIOUS_CITY_EXT.match(city))


# --- Phase A — non-raster strip helpers ---
def _strip_non_raster(row, counts):
    """Filter all_images / covers to raster only. Returns display_cover
    decision: 'ok', 'repicked', 'unpublishable_no_alt'."""
    imgs = row.get("all_images") or []
    if not isinstance(imgs, list):
        imgs = []
    raster = [im for im in imgs
              if not isinstance(im, dict) or _is_raster_url(im.get("url"))]
    stripped = len(imgs) - len(raster)
    if stripped:
        row["all_images"] = raster
        counts["all_images_non_raster_stripped"] += stripped

    cbt = row.get("covers_by_type")
    if isinstance(cbt, dict):
        new_cbt = {k: (None if v and not _is_raster_url(v) else v)
                   for k, v in cbt.items()}
        if new_cbt != cbt:
            row["covers_by_type"] = new_cbt
            counts["covers_by_type_non_raster_nulled"] += 1
        cbt = new_cbt
    else:
        cbt = {}

    cidu = row.get("cover_image_url_default")
    if cidu and not _is_raster_url(cidu):
        row["cover_image_url_default"] = None
        cidu = None

    dcu = row.get("display_cover_url")
    if dcu and not _is_raster_url(dcu):
        row["display_cover_url"] = None
        dcu = None
        new_dcu = _display_cover_url(
            covers_by_type=cbt, cover_image_url_default=cidu, all_images=raster
        )
        if new_dcu:
            row["display_cover_url"] = new_dcu
            counts["cover_non_raster_repicked"] += 1
            return "repicked"
        return "unpublishable_no_alt"
    return "ok"


# --- Phase B — phash-aware dedup ---
def _dedup_with_phash_priority(images):
    if not isinstance(images, list):
        return images, 0
    groups = defaultdict(list)
    out_unkeyed = []
    seen_url = set()
    for im in images:
        if not isinstance(im, dict):
            out_unkeyed.append(im)
            continue
        url = im.get("url")
        if not url:
            out_unkeyed.append(im)
            continue
        if url in seen_url:
            continue
        seen_url.add(url)
        key = _canonical_asset_key(url)
        if key is None:
            out_unkeyed.append(im)
            continue
        groups[key].append(im)
    excess = 0
    picked = []
    for grp in groups.values():
        if len(grp) == 1:
            picked.append(grp[0])
            continue
        excess += len(grp) - 1
        best = sorted(grp, key=lambda im: (
            _is_lowres_url(im.get("url")) or _is_gif(im.get("url")),
            not bool(im.get("phash")),
            -len(im.get("url") or ""),
            im.get("image_order") if isinstance(im.get("image_order"), int) else 9999,
        ))[0]
        picked.append(best)
    picked.sort(key=lambda im: im.get("image_order")
                if isinstance(im.get("image_order"), int) else 9999)
    return out_unkeyed + picked, excess


def _row_has_phash(row):
    for im in row.get("all_images") or []:
        if isinstance(im, dict) and im.get("phash"):
            return True
    return False


def _row_cover_in_images(row):
    dcu = row.get("display_cover_url")
    if not dcu:
        return False
    cur_key = _canonical_asset_key(dcu)
    if not cur_key:
        return False
    for im in row.get("all_images") or []:
        if isinstance(im, dict) and _canonical_asset_key(im.get("url")) == cur_key:
            return True
    return False


def _repick_raster_cover(row):
    images = row.get("all_images") or []
    full = next(
        (im.get("url") for im in images
         if isinstance(im, dict) and im.get("url") and _is_raster_url(im.get("url"))
         and not (_is_lowres_url(im.get("url")) or _is_gif(im.get("url")))),
        None,
    )
    if full:
        row["display_cover_url"] = full
        return "repicked_full"
    any_raster = next(
        (im.get("url") for im in images
         if isinstance(im, dict) and im.get("url") and _is_raster_url(im.get("url"))),
        None,
    )
    if any_raster:
        row["display_cover_url"] = any_raster
        return "repicked_lowres"
    return "no_alternative"


def _set_unpublish(row, reason):
    reasons = list(row.get("publishability_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    row["publishability_reasons"] = reasons
    row["is_publishable"] = False


# --- Phase D — cover dup avoid map (exact + near, raster fallback) ---
def _load_cover_dup_avoid():
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


# --- Phase F — name+arch auto-merge ---
def _name_arch_meta_pass(input_path, target_cids):
    meta = {}
    for row in iter_buildings(input_path):
        cid = row.get("canonical_bld_id")
        if cid in target_cids:
            meta[cid] = row
    return meta


def _build_name_arch_merge(groups, meta):
    uf = _UF()
    for g in groups:
        cids = sorted(r["cid"] for r in (g.get("rows") or []))
        if len(cids) < 2:
            continue
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


# --- Phase G — Pixel House / source-majority country fix ---
def _load_country_fix_map():
    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    out = {}
    for r in actionable["issues"].get("publishable_final_country_not_in_sources", []):
        cid = r.get("cid")
        sources = r.get("source_countries") or []
        if not sources:
            continue
        norm = [_normalize_country_ext(c) for c in sources if c]
        norm = [c for c in norm if c]
        if not norm:
            continue
        majority = Counter(norm).most_common(1)[0][0]
        out[cid] = majority
    return out


# --- Phase J — registry update (reuse C17 logic) ---
def _update_registry(c18_output, removed_with_cycle):
    if not REGISTRY.exists():
        return {}
    import shutil
    shutil.copyfile(REGISTRY, REGISTRY_BACKUP)
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    src_key_to_cid = {}
    for row in iter_buildings(c18_output):
        cid = row.get("canonical_bld_id")
        for s, ids in (row.get("source_refs") or {}).items():
            for sid in ids or []:
                src_key_to_cid[(s, str(sid))] = cid
    stats = Counter()
    for removed_cid, cycle in removed_with_cycle.items():
        entry = registry.get(removed_cid)
        if not isinstance(entry, dict):
            stats["missing"] += 1
            continue
        refs = entry.get("source_refs") or {}
        survivors = []
        for s, ids in refs.items():
            for sid in ids or []:
                surv = src_key_to_cid.get((s, str(sid)))
                if surv and surv != removed_cid:
                    survivors.append(surv)
        if survivors:
            entry["redirected_to"] = Counter(survivors).most_common(1)[0][0]
            stats["updated"] += 1
        else:
            entry["redirected_to"] = None
            stats["no_survivor"] += 1
        entry["removed_at"] = cycle
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return dict(stats)


# --- main ---
def main() -> int:
    for p in (C17, ACTIONABLE, GATE):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    iss = actionable["issues"]
    cover_non_raster_cids = {r["cid"] for r in iss.get("display_cover_non_raster_ext", [])}
    missing_phash_cids = {r["cid"] for r in iss.get("publishable_all_images_missing_phash", [])}
    cover_not_in_imgs_cids = {r["cid"]
                              for r in iss.get("publishable_display_cover_asset_not_in_all_images", [])}
    cover_low_dim_cids = {r["cid"]
                          for r in iss.get("display_cover_low_dimensions", []) or []}
    name_arch_groups = iss.get("exact_name_country_same_arch_groups", [])
    name_year_groups = iss.get("exact_name_country_city_year_groups", [])
    split_suspect_pairs = iss.get("shared_phash_split_suspect_pairs", []) \
        or iss.get("cross_card_any_image_phash_duplicate_groups", [])
    country_conflict = iss.get("publishable_source_country_conflict", [])
    year_conflict = iss.get("publishable_source_year_conflict_gt2", [])
    country_fix = _load_country_fix_map()
    cover_dup_avoid = _load_cover_dup_avoid()

    # name+arch merge pre-pass
    target_meta_cids = {r["cid"] for g in name_arch_groups for r in (g.get("rows") or [])}
    name_arch_meta = _name_arch_meta_pass(C17, target_meta_cids) if name_arch_groups else {}
    survivor_patch, losers = _build_name_arch_merge(name_arch_groups, name_arch_meta) \
        if name_arch_groups else ({}, set())

    # sidecars (write upfront, no row dependency)
    COUNTRY_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    with COUNTRY_SIDECAR.open("w", encoding="utf-8") as f:
        for r in country_conflict:
            f.write(json.dumps({
                "cid": r.get("cid"), "name": r.get("name"),
                "row_country": r.get("country"),
                "source_countries_raw": r.get("issue") or r.get("source_countries") or [],
                "source_refs": r.get("source_refs") or {},
            }, ensure_ascii=False) + "\n")
    with YEAR_SIDECAR.open("w", encoding="utf-8") as f:
        for r in year_conflict:
            f.write(json.dumps({
                "cid": r.get("cid"), "name": r.get("name"), "year": r.get("year"),
                "source_years": r.get("issue") or r.get("source_years") or [],
                "source_refs": r.get("source_refs") or {},
            }, ensure_ascii=False) + "\n")
    with NAME_YEAR_SIDECAR.open("w", encoding="utf-8") as f:
        for g in name_year_groups:
            f.write(json.dumps({
                "key": g.get("key"), "count": g.get("count"),
                "cids": [r.get("cid") for r in (g.get("rows") or [])],
                "rows": [{"cid": r.get("cid"), "name": r.get("name"),
                          "country": r.get("country"), "city": r.get("city"),
                          "year": r.get("year"),
                          "arch": sorted(list(r.get("architect_ids") or []))}
                         for r in (g.get("rows") or [])],
            }, ensure_ascii=False) + "\n")
    with SPLIT_SUSPECT_SIDECAR.open("w", encoding="utf-8") as f:
        for g in split_suspect_pairs[:500]:  # cap
            f.write(json.dumps(g, ensure_ascii=False) + "\n")

    real_country_cids = set()
    for r in country_conflict:
        cid = r.get("cid")
        issue = r.get("issue") or r.get("source_countries") or []
        norm = {_normalize_country_ext(c) or "" for c in issue}
        norm.discard("")
        rc = _normalize_country_ext(r.get("country")) or ""
        if rc:
            norm.add(rc)
        if len(norm) > 1:
            real_country_cids.add(cid)

    counts: Counter = Counter()
    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C17):
            n_in += 1
            cid = row.get("canonical_bld_id")
            if cid in losers:
                counts["merge_loser_removed"] += 1
                continue
            if cid in survivor_patch:
                row = survivor_patch[cid]
                counts["merge_survivor"] += 1

            # A — non-raster strip + cover re-pick if needed
            outcome = _strip_non_raster(row, counts)
            if outcome == "unpublishable_no_alt" and row.get("is_publishable"):
                _set_unpublish(row, "cover_non_raster_no_alt")
                counts["unpublish_cover_non_raster"] += 1

            # B — asset re-dedup with phash priority
            new_imgs, excess = _dedup_with_phash_priority(row.get("all_images") or [])
            if excess:
                row["all_images"] = new_imgs
                counts["redup_phash_priority_rows"] += 1
                counts["redup_phash_priority_excess"] += excess

            # B-2: if cid was in missing-phash list AND still no phash → unpublish
            if cid in missing_phash_cids and row.get("is_publishable") and not _row_has_phash(row):
                _set_unpublish(row, "images_missing_phash")
                counts["unpublish_missing_phash"] += 1

            # C — cover-not-in-all_images residual
            if cid in cover_not_in_imgs_cids and not _row_cover_in_images(row):
                action = _repick_raster_cover(row)
                if action == "no_alternative" and row.get("is_publishable"):
                    _set_unpublish(row, "cover_invalid_no_alternative")
                    counts["unpublish_cover_no_alt"] += 1
                else:
                    counts[f"cover_{action}"] += 1

            # D — cover phash dup residual swap
            if cid in cover_dup_avoid:
                spec = cover_dup_avoid[cid]
                bad_keys = spec["asset_keys"]
                bad_ph = spec["phashes"]
                dcu = row.get("display_cover_url")
                if dcu and (_canonical_asset_key(dcu) in bad_keys):
                    row["display_cover_url"] = None
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
                        counts["cover_dup_swapped"] += 1
                    elif row.get("is_publishable"):
                        _set_unpublish(row, "cover_duplicate_no_alt")
                        counts["unpublish_cover_dup_no_alt"] += 1

            # E — low-dimensions cover
            if cid in cover_low_dim_cids and row.get("is_publishable"):
                _set_unpublish(row, "cover_low_dim")
                counts["unpublish_cover_low_dim"] += 1

            # G — Pixel House / source-majority country override
            if cid in country_fix:
                row["location_country"] = country_fix[cid]
                counts["country_majority_fix"] += 1

            # D-flag — country_disputed for real conflicts
            if cid in real_country_cids:
                reasons = list(row.get("publishability_reasons") or [])
                if "country_disputed" not in reasons:
                    reasons.append("country_disputed")
                row["publishability_reasons"] = reasons
                counts["country_disputed_flagged"] += 1

            # I — string hygiene (NFC + extended chars)
            for fld in ("name", "architects_text", "location_city"):
                v = row.get(fld)
                nv, changed = _strip_str_full(v)
                if changed:
                    row[fld] = nv
                    counts[f"hygiene_{fld}_c18"] += 1

            # I — suspicious city (extended) null-out
            city = row.get("location_city")
            if _is_suspicious_city(city):
                row["location_city"] = None
                counts["suspicious_city_nulled_c18"] += 1

            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    # J — registry update (include any new C18 losers)
    removed_with_cycle = _load_removed_cids_with_cycle()
    for c in losers:
        removed_with_cycle.setdefault(c, "C18")
    registry_stats = _update_registry(OUT, removed_with_cycle)

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C17.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_out == n_in - len(losers),
        "counts": dict(counts),
        "name_arch_merge_groups": len(name_arch_groups),
        "name_arch_merge_losers": len(losers),
        "country_conflict_sidecar": len(country_conflict),
        "year_conflict_sidecar": len(year_conflict),
        "name_year_sidecar": len(name_year_groups),
        "split_suspect_sidecar": len(split_suspect_pairs[:500]),
        "registry": {"cumulative_removed_ids": len(removed_with_cycle), **registry_stats,
                     "backup": str(REGISTRY_BACKUP.relative_to(ROOT))},
        "removed_canonical_ids_this_pass": sorted(losers),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C18 polish [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps({k: v for k, v in report.items()
                      if k != "removed_canonical_ids_this_pass"},
                     ensure_ascii=False, indent=2))
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
