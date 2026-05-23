#!/usr/bin/env python3
"""C17 make_web pre-upsert polish (round 3) + registry merge lineage.

Addresses Codex's C16 re-audit residual blockers + registry lineage:
  A. URL canonicalizer rewritten (in canonical_v2_c16_url_canon.py) to
     normalize Architizer timestamp prefix + Divisare non-hex / UUID /
     Cloudinary / extension variants.
  B. internal canonical asset re-dedup using the strengthened key.
  C. cover residuals:
     C-1 cover URL canonical-asset not in all_images (17 rows) -> re-pick
         display_cover from first non-lowres all_images entry.
     C-2/3 lowres/thumb/GIF cover residual (17) -> unpublish + reason
         cover_lowres_or_gif.
     C-4 publishable all-images-missing-phash (6) -> unpublish + reason
         images_missing_phash.
  D. source country conflict (216) -> extended COUNTRY_ALIASES normalize,
     drop false positives; real conflicts get sidecar + reason
     country_disputed (publishable kept).
  E. source year conflict (42) -> sidecar only (legit renovation/conservation
     multi-year is too common to auto-flag).
  F. registry merge lineage -> 285 cumulative removed_canonical_ids resolved
     to survivor via source_refs index over C17 output; backup + write
     id_registry_buildings.json.

Streaming, strict artifact. Read-only w.r.t. Neon.
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import COUNTRY_ALIASES, _display_cover_url  # noqa: E402
from tools.canonical_v2_c16_make_web_polish import _dedup_by_asset  # noqa: E402
from tools.canonical_v2_c16_url_canon import (  # noqa: E402
    _canonical_asset_key, _is_gif, _is_lowres_url,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C16 = CCR / "canonical_buildings_strict.completeness_c16_make_web_polish.json"
ACTIONABLE = ROOT / "data/reports/canonical_v2_c16_make_web_actionable_issues.codex_audit.json"
REGISTRY = ROOT / "data/id_registry_buildings.json"
REGISTRY_BACKUP = ROOT / "data/id_registry_buildings.backup_pre_c17.json"

OUT = CCR / "canonical_buildings_strict.completeness_c17_make_web_polish.json"
REPORT = ROOT / "data/reports/canonical_v2_c17_make_web_polish_report.json"
COUNTRY_SIDECAR = ROOT / "data/reports/canonical_v2_c17_country_conflict_sidecar.jsonl"
YEAR_SIDECAR = ROOT / "data/reports/canonical_v2_c17_year_conflict_sidecar.jsonl"

# extended aliases for Phase D — covers Codex-flagged variants
_COUNTRY_ALIASES_EXT = {
    **{k: v for k, v in COUNTRY_ALIASES.items()},
    "korea, democratic people's republic of": "North Korea",
    "korea, dpr": "North Korea",
    "democratic people's republic of korea": "North Korea",
    "north korea": "North Korea",
    "dprk": "North Korea",
    "people's republic of china": "China",
    "china, people's republic of": "China",
    "iran, islamic republic of": "Iran",
    "iran (islamic republic of)": "Iran",
    "macedonia, the former yugoslav republic of": "North Macedonia",
    "republic of north macedonia": "North Macedonia",
    "moldova, republic of": "Moldova",
    "syrian arab republic": "Syria",
    "tanzania, united republic of": "Tanzania",
    "lao people's democratic republic": "Laos",
    "hong kong, china": "Hong Kong",
    "hong kong sar": "Hong Kong",
    "taiwan, province of china": "Taiwan",
    "republic of china": "Taiwan",
    "palestine, state of": "Palestine",
    "vietnam, socialist republic of": "Vietnam",
    "bolivia, plurinational state of": "Bolivia",
    "venezuela, bolivarian republic of": "Venezuela",
    "korea, republic of": "South Korea",
    "republic of korea": "South Korea",
}


def _normalize_country_ext(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = " ".join(text.replace(" ", " ").split()).casefold()
    return _COUNTRY_ALIASES_EXT.get(key, text)


# --- Phase F — registry lineage ---
def _load_removed_cids_with_cycle():
    """Return dict cid -> cycle_label for cumulative removed ids."""
    cycles = []
    for label, fname in [
        ("C10", "canonical_v2_c10_recovery_report.json"),
        ("C13", "canonical_v2_c13_imageqa_report.json"),
        ("C14", "canonical_v2_c14_finalize_report.json"),
        ("C16", "canonical_v2_c16_make_web_polish_report.json"),
    ]:
        p = ROOT / "data/reports" / fname
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        ids = (d.get("removed_canonical_ids_this_pass")
               or d.get("removed_this_pass") or [])
        if not ids and label == "C10":
            ids = d.get("removed_canonical_ids") or []
        cycles.append((label, ids))
    out = {}
    for label, ids in cycles:
        for cid in ids:
            out.setdefault(cid, label)
    return out


def _update_registry(c17_output: Path, removed_with_cycle: dict[str, str]) -> dict:
    """Backup + update registry: for each removed cid, set redirected_to via
    source_refs index over C17 output."""
    if not REGISTRY.exists():
        return {"updated": 0, "no_survivor": 0, "missing_in_registry": 0}
    shutil.copyfile(REGISTRY, REGISTRY_BACKUP)
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))

    src_key_to_cid = {}
    for row in iter_buildings(c17_output):
        cid = row.get("canonical_bld_id")
        for s, ids in (row.get("source_refs") or {}).items():
            for sid in ids or []:
                src_key_to_cid[(s, str(sid))] = cid

    stats = Counter()
    for removed_cid, cycle in removed_with_cycle.items():
        entry = registry.get(removed_cid)
        if not isinstance(entry, dict):
            stats["missing_in_registry"] += 1
            continue
        refs = entry.get("source_refs") or {}
        survivors = []
        for s, ids in refs.items():
            for sid in ids or []:
                surv = src_key_to_cid.get((s, str(sid)))
                if surv and surv != removed_cid:
                    survivors.append(surv)
        if survivors:
            winner = Counter(survivors).most_common(1)[0][0]
            entry["redirected_to"] = winner
            stats["updated"] += 1
        else:
            entry["redirected_to"] = None
            stats["no_survivor"] += 1
        entry["removed_at"] = cycle
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return dict(stats)


# --- Phase C — cover residual helpers ---
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


def _force_repick_cover(row):
    images = row.get("all_images") or []
    full = next(
        (im.get("url") for im in images
         if isinstance(im, dict) and im.get("url")
         and not (_is_lowres_url(im.get("url")) or _is_gif(im.get("url")))),
        None,
    )
    if full:
        row["display_cover_url"] = full
        return "repicked"
    any_url = next(
        (im.get("url") for im in images
         if isinstance(im, dict) and im.get("url")),
        None,
    )
    if any_url:
        row["display_cover_url"] = any_url
        return "repicked_lowres_only"
    return "no_alternative"


def _set_unpublish(row, reason):
    reasons = list(row.get("publishability_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    row["publishability_reasons"] = reasons
    row["is_publishable"] = False


# --- Phase D — country conflict classification ---
def _is_real_country_conflict(issue_countries, row_country):
    norm = {_normalize_country_ext(c) or "" for c in (issue_countries or [])}
    norm.discard("")
    rc = _normalize_country_ext(row_country) or ""
    if rc:
        norm.add(rc)
    return len(norm) > 1


# --- main ---
def main() -> int:
    for p in (C16, ACTIONABLE, REGISTRY):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    iss = actionable["issues"]
    cover_lowres_cids = {r["cid"] for r in iss.get("display_cover_lowres_or_thumb", [])}
    cover_gif_cids = {r["cid"] for r in iss.get("display_cover_gif", [])}
    cover_not_in_imgs_cids = {r["cid"]
                              for r in iss.get("publishable_display_cover_asset_not_in_all_images", [])}
    missing_phash_cids = {r["cid"]
                          for r in iss.get("publishable_all_images_missing_phash", [])}
    country_conflict_rows = iss.get("publishable_source_country_conflict", [])
    year_conflict_rows = iss.get("publishable_source_year_conflict_gt2", [])

    # Pre-compute country conflict classification
    country_real_conflict = {}
    for r in country_conflict_rows:
        cid = r.get("cid")
        is_real = _is_real_country_conflict(r.get("issue") or [], r.get("country"))
        country_real_conflict[cid] = (is_real, r)

    # Year sidecar (no decision impact, write upfront)
    YEAR_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    with YEAR_SIDECAR.open("w", encoding="utf-8") as f:
        for r in year_conflict_rows:
            f.write(json.dumps({
                "cid": r.get("cid"), "name": r.get("name"),
                "country": r.get("country"), "city": r.get("city"),
                "year": r.get("year"), "source_years": r.get("issue"),
                "source_refs": r.get("source_refs") or {},
            }, ensure_ascii=False) + "\n")

    # Country sidecar (real conflicts only)
    country_real_cids = {cid for cid, (real, _) in country_real_conflict.items() if real}
    country_false_cids = set(country_real_conflict) - country_real_cids
    with COUNTRY_SIDECAR.open("w", encoding="utf-8") as f:
        for cid, (real, r) in country_real_conflict.items():
            if real:
                f.write(json.dumps({
                    "cid": cid, "name": r.get("name"),
                    "row_country": r.get("country"),
                    "source_countries_raw": r.get("issue") or [],
                    "normalized": sorted({_normalize_country_ext(c) or ""
                                          for c in (r.get("issue") or [])}),
                    "source_refs": r.get("source_refs") or {},
                }, ensure_ascii=False) + "\n")

    counts: Counter = Counter()
    counts["country_false_positive"] = len(country_false_cids)
    counts["country_real_conflict"] = len(country_real_cids)

    # Stream C16 → C17
    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C16):
            n_in += 1
            cid = row.get("canonical_bld_id")

            # B — canonical asset re-dedup (strengthened keys)
            new_imgs, excess = _dedup_by_asset(row.get("all_images") or [])
            if excess:
                row["all_images"] = new_imgs
                counts["asset_redup_rows"] += 1
                counts["asset_redup_excess_removed"] += excess
            bipc = row.get("best_image_per_cluster")
            if isinstance(bipc, dict):
                seen = set()
                new_bipc = {}
                ex = 0
                for k, im in bipc.items():
                    if isinstance(im, dict) and im.get("url"):
                        akey = _canonical_asset_key(im.get("url"))
                        if akey and akey in seen:
                            ex += 1
                            continue
                        if akey:
                            seen.add(akey)
                    new_bipc[k] = im
                if ex:
                    row["best_image_per_cluster"] = new_bipc
                    counts["bipc_redup_rows"] += 1

            # C-1 cover not in all_images → re-pick
            if cid in cover_not_in_imgs_cids and not _row_cover_in_images(row):
                outcome = _force_repick_cover(row)
                if outcome == "no_alternative":
                    _set_unpublish(row, "cover_invalid_no_alternative")
                    counts["cover_no_alt_unpublished"] += 1
                else:
                    counts[f"cover_{outcome}"] += 1
                # re-derive via _display_cover_url for consistency with cbt
                ndcu = _display_cover_url(
                    covers_by_type=row.get("covers_by_type") or {},
                    cover_image_url_default=row.get("cover_image_url_default"),
                    all_images=row.get("all_images") or [],
                )
                if ndcu:
                    row["display_cover_url"] = ndcu

            # C-2/3 lowres/GIF cover residual → unpublish
            if row.get("is_publishable") and (cid in cover_lowres_cids
                                              or cid in cover_gif_cids):
                _set_unpublish(row, "cover_lowres_or_gif")
                counts["unpublish_cover_lowres_or_gif"] += 1

            # C-4 missing-phash → unpublish if publishable
            if row.get("is_publishable") and cid in missing_phash_cids and not _row_has_phash(row):
                _set_unpublish(row, "images_missing_phash")
                counts["unpublish_missing_phash"] += 1

            # D — country conflict reason flag for real conflicts (publishable kept)
            if cid in country_real_cids:
                reasons = list(row.get("publishability_reasons") or [])
                if "country_disputed" not in reasons:
                    reasons.append("country_disputed")
                row["publishability_reasons"] = reasons
                counts["country_disputed_flagged"] += 1

            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    # F — registry lineage
    removed_with_cycle = _load_removed_cids_with_cycle()
    registry_stats = _update_registry(OUT, removed_with_cycle)

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C16.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_in == n_out,
        "counts": dict(counts),
        "registry": {
            "cumulative_removed_ids": len(removed_with_cycle),
            **registry_stats,
            "backup": str(REGISTRY_BACKUP.relative_to(ROOT)),
        },
        "country_sidecar_entries": len(country_real_cids),
        "year_sidecar_entries": len(year_conflict_rows),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C17 polish [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"  country sidecar -> {COUNTRY_SIDECAR.relative_to(ROOT)} ({len(country_real_cids)})")
    print(f"  year sidecar    -> {YEAR_SIDECAR.relative_to(ROOT)} ({len(year_conflict_rows)})")
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
