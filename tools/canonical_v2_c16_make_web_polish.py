#!/usr/bin/env python3
"""C16 make_web pre-upsert polish (round 2) — on top of C15.

Addresses Codex's C15 re-audit residual blockers:
  A. cover dup residual (2 groups) — force-null cover URLs whose canonical
     asset matches the dup group; re-derive display_cover.
  B. internal canonical asset dedupe (~11k rows) — group all_images by
     `_canonical_asset_key` and keep one (full-res preferred) per asset.
  C. display cover lowres / GIF (~352) — swap to a full-res variant of the
     same asset; failing that, any full-res non-GIF image.
  D. exact name+country+arch auto-merge (96 groups) — matcher recall
     leftovers. Survivor = min(cid); losers absorbed + removed.
  E. sidecars — 59 exact name+country+city+year groups (BUS:STOP series
     suspects) + 309 cross-card any-image phash groups (classified
     split-suspect / series-share / cross-arch). No row change.
  F. source_url gap backfill (~2,038 rows) — slug lookup in source DBs ->
     build canonical URL via the same `_build_source_url` the canonical
     pipeline already uses.
  G. suspicious city + string hygiene extension — regex expansions for
     street-address cities and BOM / zero-width / bidi control chars.
  H. minor — sidecar arch list-serialization, `year_kind` docs entry.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import (  # noqa: E402
    _build_source_url, _confidence_tier, _display_cover_url,
)
from tools.canonical_v2_c15_make_web_polish import _strip_str as _c15_strip  # noqa: E402
from tools.canonical_v2_c16_url_canon import (  # noqa: E402
    _canonical_asset_key, _is_gif, _is_lowres_url,
)
from tools.canonical_v2_recover_dropped_twins import (  # noqa: E402
    _UF, _union, _is_empty, _ABSORB_IF_EMPTY, _ABSORB_UNION,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C15 = CCR / "canonical_buildings_strict.completeness_c15_make_web_polish.json"
ACTIONABLE = ROOT / "data/reports/canonical_v2_c15_make_web_actionable_issues.codex_audit.json"
GATE = ROOT / "data/reports/canonical_v2_c15_make_web_quality_gate.codex_audit.json"
C15_REPORT = ROOT / "data/reports/canonical_v2_c15_make_web_polish_report.json"
CRAWL = ROOT / "data/crawl"

OUT = CCR / "canonical_buildings_strict.completeness_c16_make_web_polish.json"
REPORT = ROOT / "data/reports/canonical_v2_c16_make_web_polish_report.json"
NAME_SIDECAR = ROOT / "data/reports/canonical_v2_c16_name_year_sidecar.jsonl"
GALLERY_SIDECAR = ROOT / "data/reports/canonical_v2_c16_gallery_dup_sidecar.jsonl"
HYGIENE_DIFF = ROOT / "data/reports/canonical_v2_c16_hygiene_diff.json"

# Extended control char regex (BOM, zero-width, bidi)
_HYGIENE_EXTRA_RE = re.compile(
    r"[﻿​-‏‪-‮⁠-⁯]"
)
# Extended suspicious city patterns (street address with "via", year prefix, dash-only)
_SUSPICIOUS_CITY_EXT = re.compile(
    r"^\s*("
    r"\d+[a-z]?"
    r"|[A-Za-z]"
    r"|(19|20)\d{2}\s*[-–]"
    r"|.*,\s*(via|rua|street|st\.?|avenue|ave|road|rd\.?)\s+\S+"
    r"|[-–]+"
    r")\s*$",
    re.I,
)


def _strip_str_ext(s):
    """C15 _strip_str + extended control set (BOM, zero-width, bidi)."""
    if not isinstance(s, str):
        return s, False
    orig = s
    s = _HYGIENE_EXTRA_RE.sub("", s)
    s, _ = _c15_strip(s)
    return s, s != orig


def _is_suspicious_city_ext(city):
    return isinstance(city, str) and bool(_SUSPICIOUS_CITY_EXT.match(city))


def _name_key(s):
    return " ".join(str(s or "").casefold().split())


def _name_sim(a, b):
    return fuzz.token_set_ratio(_name_key(a), _name_key(b))


# --- Phase A — cover dup avoid sets ---
def _load_cover_force_avoid():
    """For each loser cid in the 2 residual cover-dup groups, the asset key
    to avoid + the dup phash."""
    avoid = defaultdict(lambda: {"asset_keys": set(), "phashes": set()})
    gate = json.loads(GATE.read_text(encoding="utf-8"))
    samples = gate.get("samples", {})
    for g in samples.get("cross_card_display_phash_duplicate_groups", []):
        rows = g.get("rows") or []
        if len(rows) < 2:
            continue
        ph = g.get("phash")
        # rank: more sources first; first wins, others swap
        ranked = sorted(rows, key=lambda r: -len(r.get("source_refs") or {}))
        winner_key = _canonical_asset_key(ranked[0].get("display_cover_url"))
        for r in ranked[1:]:
            if winner_key:
                avoid[r["cid"]]["asset_keys"].add(winner_key)
            if ph:
                avoid[r["cid"]]["phashes"].add(ph)
    return avoid


# --- Phase B — internal canonical asset dedup ---
def _dedup_by_asset(images):
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
    for key, grp in groups.items():
        if len(grp) == 1:
            picked.append(grp[0])
            continue
        excess += len(grp) - 1
        # prefer non-lowres, then longest URL, then first by image_order
        best = sorted(grp, key=lambda im: (
            _is_lowres_url(im.get("url")) or _is_gif(im.get("url")),
            -len(im.get("url") or ""),
            im.get("image_order") if isinstance(im.get("image_order"), int) else 9999,
        ))[0]
        picked.append(best)
    # preserve a stable order
    picked.sort(key=lambda im: im.get("image_order") if isinstance(im.get("image_order"), int) else 9999)
    return out_unkeyed + picked, excess


# --- Phase C — display cover lowres/GIF swap ---
def _swap_lowres_cover(row, counts):
    dcu = row.get("display_cover_url")
    if not dcu or not (_is_lowres_url(dcu) or _is_gif(dcu)):
        return False
    images = row.get("all_images") or []
    cur_key = _canonical_asset_key(dcu)
    # 1: same-asset full-res variant
    same_asset_full = next(
        (im.get("url") for im in images
         if isinstance(im, dict) and im.get("url")
         and _canonical_asset_key(im.get("url")) == cur_key
         and not (_is_lowres_url(im.get("url")) or _is_gif(im.get("url")))),
        None,
    )
    if same_asset_full and same_asset_full != dcu:
        row["display_cover_url"] = same_asset_full
        counts["lowres_cover_same_asset_swap"] += 1
        return True
    # 2: any non-lowres non-GIF image
    other_full = next(
        (im.get("url") for im in images
         if isinstance(im, dict) and im.get("url")
         and not (_is_lowres_url(im.get("url")) or _is_gif(im.get("url")))),
        None,
    )
    if other_full:
        row["display_cover_url"] = other_full
        counts["lowres_cover_other_asset_swap"] += 1
        return True
    counts["lowres_cover_unswappable"] += 1
    return False


# --- Phase D — auto-merge 96 groups ---
def _load_merge_groups():
    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    return actionable["issues"].get("exact_name_country_same_arch_groups", [])


def _build_merge_plan(groups, meta):
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


# --- Phase F — source_url backfill via slug lookup ---
_SRC_DB_TABLE = {
    "archello": ("archello.db", "archello_projects"),
    "architizer": ("architizer.db", "architizer_projects"),
    "divisare": ("divisare.db", "divisare_projects"),
    "metalocus": ("metalocus.db", "buildings"),
}


def _load_slug_map(source: str, ids: list[str]) -> dict[str, str]:
    """Return sid (str) -> slug for a source. Empty for ids we can't find."""
    if source not in _SRC_DB_TABLE or not ids:
        return {}
    db_file, table = _SRC_DB_TABLE[source]
    out = {}
    int_ids = []
    for sid in ids:
        try:
            int_ids.append(int(sid))
        except (TypeError, ValueError):
            continue
    if not int_ids:
        return {}
    conn = sqlite3.connect(str(CRAWL / db_file))
    try:
        placeholders = ",".join("?" * len(int_ids))
        for sid, slug in conn.execute(
            f"SELECT id, slug FROM {table} WHERE id IN ({placeholders})",
            int_ids,
        ):
            if slug:
                out[str(sid)] = str(slug)
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return out


def _load_source_url_gaps():
    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    gaps = actionable["issues"].get("publishable_rows_with_source_url_gap", [])
    out = defaultdict(list)
    for row in gaps:
        cid = row.get("cid")
        for src, sid in row.get("missing_keys") or []:
            out[cid].append((src, str(sid)))
    return out


def _build_slug_index(gaps):
    """Pre-load slug maps for every (source, id) pair across all gaps."""
    by_src = defaultdict(set)
    for missing in gaps.values():
        for src, sid in missing:
            by_src[src].add(sid)
    return {src: _load_slug_map(src, sorted(ids)) for src, ids in by_src.items()}


# --- Phase E — sidecars ---
def _classify_gallery_group(cids, meta):
    arch_sets = [frozenset(meta[c].get("architect_canonical_ids") or [])
                 for c in cids if c in meta]
    if not arch_sets:
        return "cross-arch"
    arch_overlap = frozenset.intersection(*arch_sets) if len(arch_sets) > 1 else arch_sets[0]
    countries = {meta[c].get("location_country") for c in cids if c in meta}
    same_country = len(countries) == 1 and None not in countries
    if arch_overlap and same_country:
        names = [meta[c].get("name") for c in cids if c in meta]
        if len(names) >= 2 and all(
            _name_sim(names[0], n) >= 80 for n in names[1:]
        ):
            return "split-suspect"
        return "series-share"
    if arch_overlap:
        return "series-share"
    return "cross-arch"


# --- main ---
def main() -> int:
    for p in (C15, ACTIONABLE, GATE):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    cover_avoid = _load_cover_force_avoid()
    merge_groups = _load_merge_groups()
    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    name_year_groups = actionable["issues"].get(
        "exact_name_country_city_year_groups", [])
    gallery_groups = actionable["issues"].get(
        "cross_card_any_image_phash_duplicate_groups", [])
    gaps = _load_source_url_gaps()
    slug_index = _build_slug_index(gaps)

    # Collect cids for which we need full meta (merge survivors + losers + sidecar)
    merge_cids = {r["cid"] for g in merge_groups for r in (g.get("rows") or [])}
    sidecar_cids = {r["cid"] for g in name_year_groups for r in (g.get("rows") or [])}
    sidecar_cids |= {r["cid"] for g in gallery_groups for r in (g.get("rows") or [])}
    target_cids = merge_cids | sidecar_cids | set(cover_avoid)

    # Pass 1 — meta
    meta = {}
    arch_country_meta = {}
    for row in iter_buildings(C15):
        cid = row.get("canonical_bld_id")
        if cid in target_cids:
            meta[cid] = row
        if cid in sidecar_cids:
            arch_country_meta[cid] = {
                "name": row.get("name"),
                "location_country": row.get("location_country"),
                "architect_canonical_ids": row.get("architect_canonical_ids") or [],
            }

    survivor_patch, losers = _build_merge_plan(merge_groups, meta)

    # Sidecars
    NAME_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    with NAME_SIDECAR.open("w", encoding="utf-8") as f:
        for g in name_year_groups:
            entry = {
                "key": g.get("key"),
                "count": g.get("count"),
                "rows": [
                    {
                        "cid": r.get("cid"),
                        "name": r.get("name"),
                        "country": r.get("country"),
                        "city": r.get("city"),
                        "year": r.get("year"),
                        "arch": sorted(list(r.get("architect_ids") or [])),
                        "image_count": r.get("image_count"),
                        "display_cover_url": r.get("display_cover_url"),
                        "source_refs": r.get("source_refs") or {},
                    }
                    for r in (g.get("rows") or [])
                ],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    with GALLERY_SIDECAR.open("w", encoding="utf-8") as f:
        for g in gallery_groups:
            cids = [r.get("cid") for r in (g.get("rows") or [])]
            cls = _classify_gallery_group(cids, arch_country_meta)
            entry = {
                "phash": g.get("phash"),
                "count": g.get("count"),
                "classification": cls,
                "cids": cids,
                "rows": [
                    {
                        "cid": r.get("cid"),
                        "name": r.get("name"),
                        "country": r.get("country"),
                        "arch": sorted(list(r.get("architect_ids") or [])),
                    }
                    for r in (g.get("rows") or [])
                ],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Pass 2 — stream, apply all phases, write C16
    counts: Counter = Counter()
    hygiene_diffs: list = []
    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C15):
            n_in += 1
            cid = row.get("canonical_bld_id")
            if cid in losers:
                counts["merge_loser_removed"] += 1
                continue
            if cid in survivor_patch:
                row = survivor_patch[cid]
                counts["merge_survivor"] += 1

            # B — canonical asset dedup
            new_imgs, excess = _dedup_by_asset(row.get("all_images") or [])
            if excess:
                row["all_images"] = new_imgs
                counts["asset_dedupe_rows"] += 1
                counts["asset_dedupe_excess_removed"] += excess
            bipc = row.get("best_image_per_cluster")
            if isinstance(bipc, dict):
                bipc_seen_keys = set()
                new_bipc = {}
                bipc_excess = 0
                for k, im in bipc.items():
                    if isinstance(im, dict) and im.get("url"):
                        akey = _canonical_asset_key(im.get("url"))
                        if akey and akey in bipc_seen_keys:
                            bipc_excess += 1
                            continue
                        if akey:
                            bipc_seen_keys.add(akey)
                    new_bipc[k] = im
                if bipc_excess:
                    row["best_image_per_cluster"] = new_bipc
                    counts["bipc_asset_dedupe_rows"] += 1

            # A — cover dup residual fix
            if cid in cover_avoid:
                spec = cover_avoid[cid]
                bad_keys = spec["asset_keys"]
                bad_phashes = spec["phashes"]
                dcu = row.get("display_cover_url")
                cidu = row.get("cover_image_url_default")
                cbt = dict(row.get("covers_by_type") or {})
                if dcu and _canonical_asset_key(dcu) in bad_keys:
                    row["display_cover_url"] = None
                    dcu = None
                if cidu and _canonical_asset_key(cidu) in bad_keys:
                    row["cover_image_url_default"] = None
                    cidu = None
                for k, v in list(cbt.items()):
                    if v and _canonical_asset_key(v) in bad_keys:
                        cbt[k] = None
                row["covers_by_type"] = cbt
                # also filter all_images by bad asset_keys / phashes
                imgs = row.get("all_images") or []
                filtered = [im for im in imgs
                            if not (isinstance(im, dict)
                                    and (_canonical_asset_key(im.get("url")) in bad_keys
                                         or im.get("phash") in bad_phashes))]
                new_dcu = _display_cover_url(
                    covers_by_type=cbt,
                    cover_image_url_default=cidu,
                    all_images=filtered,
                )
                if new_dcu and new_dcu != row.get("display_cover_url"):
                    row["display_cover_url"] = new_dcu
                    counts["cover_force_swapped"] += 1
                elif not new_dcu:
                    counts["cover_force_swap_no_alt"] += 1
                    row["display_cover_url"] = None

            # C — lowres / GIF cover swap
            _swap_lowres_cover(row, counts)

            # F — source_urls gap backfill
            if cid in gaps:
                su = dict(row.get("source_urls") or {})
                for src, sid in gaps[cid]:
                    slug = slug_index.get(src, {}).get(sid)
                    meta_for_build = {"slug": slug}
                    built = _build_source_url(src, sid, meta_for_build)
                    if built:
                        lst = list(su.get(src) or [])
                        if built not in lst:
                            lst.append(built)
                            su[src] = lst
                            counts["source_url_backfilled"] += 1
                row["source_urls"] = su

            # G — string hygiene extended
            diff = {}
            for fld in ("name", "architects_text", "location_city"):
                v = row.get(fld)
                nv, changed = _strip_str_ext(v)
                if changed:
                    diff[fld] = {"before": v, "after": nv}
                    row[fld] = nv
                    counts[f"hygiene_{fld}_c16"] += 1
            if diff:
                hygiene_diffs.append({"cid": cid, **diff})

            # G — suspicious city extended null-out
            city = row.get("location_city")
            if _is_suspicious_city_ext(city):
                row["location_city"] = None
                counts["suspicious_city_nulled_c16"] += 1

            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    HYGIENE_DIFF.write_text(
        json.dumps({"rows_changed": len(hygiene_diffs),
                    "samples": hygiene_diffs[:50]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # cumulative removed_canonical_ids
    prev = []
    c15_report = ROOT / "data/reports/canonical_v2_c14_finalize_report.json"
    if c15_report.exists():
        prev = json.loads(c15_report.read_text(encoding="utf-8")).get(
            "removed_canonical_ids", [])
    cumulative = sorted(set(prev) | set(losers))

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C15.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_out == n_in - len(losers),
        "counts": dict(counts),
        "merge_groups": len(merge_groups),
        "merge_losers": len(losers),
        "name_year_sidecar_groups": len(name_year_groups),
        "gallery_sidecar_groups": len(gallery_groups),
        "removed_canonical_ids_cumulative": cumulative,
        "removed_canonical_ids_this_pass": sorted(losers),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C16 polish [{status}] -> {OUT.relative_to(ROOT)}")
    summary = {k: v for k, v in report.items()
               if k not in ("removed_canonical_ids_cumulative",
                            "removed_canonical_ids_this_pass")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"  name-year sidecar -> {NAME_SIDECAR.relative_to(ROOT)} ({len(name_year_groups)})")
    print(f"  gallery sidecar   -> {GALLERY_SIDECAR.relative_to(ROOT)} ({len(gallery_groups)})")
    print(f"  hygiene diff      -> {HYGIENE_DIFF.relative_to(ROOT)}")
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
