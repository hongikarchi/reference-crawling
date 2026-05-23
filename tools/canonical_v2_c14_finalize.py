#!/usr/bin/env python3
"""C14 finalize — near-split merges + spam unpublish + image recovery.

Addresses Codex C13 deep-audit findings before any Neon upsert:
  1. Merge near-phash (Hamming<=8) cross-building pairs that ALSO have
     name token_set_ratio>=90 and same country. Same strict gate as the
     C13 exact-image merge, applied to near-dup pairs (~34 critical).
  2. Un-publish spam / listicle rows flagged by C13's spam sidecar.
  3. Recover 5 publishable-by-source rows whose all_images was emptied
     somewhere upstream — pull gallery_image_urls from archello /
     architizer source DBs, rebuild all_images, re-derive display cover.

Streaming, strict artifact. Read-only w.r.t. Neon.
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

from tools.build_strict_canonical import _confidence_tier, _display_cover_url  # noqa: E402
from tools.canonical_v2_image_dedup_remediate import _name_sim, _is_placeholder  # noqa: E402
from tools.canonical_v2_recover_dropped_twins import (  # noqa: E402
    _UF, _union, _is_empty, _ABSORB_IF_EMPTY, _ABSORB_UNION,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C13 = CCR / "canonical_buildings_strict.completeness_c13_imageqa.json"
CLUSTERS = ROOT / "data/reports/canonical_v2_image_dup_clusters.jsonl"
SPAM_LIST = ROOT / "data/reports/canonical_v2_c13_spam_candidates.json"
C13_REPORT = ROOT / "data/reports/canonical_v2_c13_imageqa_report.json"
CRAWL = ROOT / "data/crawl"
OUT = CCR / "canonical_buildings_strict.completeness_c14_finalize.json"
REPORT = ROOT / "data/reports/canonical_v2_c14_finalize_report.json"

MERGE_NAME_MIN = 90
# From Codex deep audit — 5 publishable-by-source rows that became image-less
RECOVER_CIDS = {"bld_029979", "bld_031541", "bld_035855", "bld_036810", "bld_036881"}


def _load_gallery(db_file: str, table: str, ids: list[int]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not ids:
        return out
    conn = sqlite3.connect(str(CRAWL / db_file))
    try:
        placeholders = ",".join("?" * len(ids))
        sql = f"SELECT id, gallery_image_urls FROM {table} WHERE id IN ({placeholders})"
        for sid, raw in conn.execute(sql, ids):
            try:
                urls = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                urls = []
            out[str(sid)] = [u for u in urls if isinstance(u, str) and not _is_placeholder(u)]
    finally:
        conn.close()
    return out


def _recover_images(row: dict, archello_imgs: dict, architizer_imgs: dict) -> int:
    """Pull source-DB gallery_image_urls into all_images; re-derive cover."""
    images = list(row.get("all_images") or [])
    refs = row.get("source_refs") or {}
    pools = {"archello": archello_imgs, "architizer": architizer_imgs}
    for src_name, sids in refs.items():
        pool = pools.get(src_name)
        if pool is None:
            continue
        for sid in sids or []:
            for i, url in enumerate(pool.get(str(sid), []) or []):
                images.append({
                    "url": url, "source": src_name, "source_id": str(sid),
                    "kind": "gallery", "image_order": i, "phash": None,
                    "phash_cluster_id": None, "rank": None, "type": None,
                    "w": None, "h": None, "bytes": None,
                })
    row["all_images"] = images
    new_dcu = _display_cover_url(
        covers_by_type=row.get("covers_by_type") or {},
        cover_image_url_default=row.get("cover_image_url_default"),
        all_images=images,
    )
    row["display_cover_url"] = new_dcu
    if new_dcu:
        reasons = [r for r in (row.get("publishability_reasons") or [])
                   if r not in ("image_unavailable", "missing_display_cover_url",
                                "missing_all_images")]
        row["publishability_reasons"] = reasons
        row["is_publishable"] = not reasons
    return len(images)


def main() -> int:
    for p in (C13, CLUSTERS, SPAM_LIST):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    near_pairs: list = []
    for line in CLUSTERS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if c.get("kind") == "near" and len(c.get("cids") or []) >= 2:
            near_pairs.append(c)
    spam_cids = {x["canonical_bld_id"]
                 for x in json.loads(SPAM_LIST.read_text(encoding="utf-8"))}

    cluster_cids = {c for cl in near_pairs for c in cl["cids"]}
    target_cids = cluster_cids | RECOVER_CIDS

    meta: dict = {}
    for row in iter_buildings(C13):
        cid = row.get("canonical_bld_id")
        if cid in target_cids:
            meta[cid] = row

    uf = _UF()
    pair_seen: set = set()
    for cl in near_pairs:
        cids = cl["cids"]
        for i, a in enumerate(cids):
            for b in cids[i + 1:]:
                lo, hi = sorted((a, b))
                if (lo, hi) in pair_seen:
                    continue
                pair_seen.add((lo, hi))
                ra, rb = meta.get(lo), meta.get(hi)
                if not ra or not rb:
                    continue
                ca, cb = ra.get("location_country"), rb.get("location_country")
                if not ca or ca != cb:
                    continue
                if _name_sim(ra.get("name"), rb.get("name")) >= MERGE_NAME_MIN:
                    uf.union(lo, hi)

    comp: dict = defaultdict(list)
    for cid in uf.p:
        comp[uf.find(cid)].append(cid)
    losers = set(uf.p) - set(comp)

    survivor_patch: dict = {}
    for surv, members in comp.items():
        merged = dict(meta[surv])
        for lc in (m for m in members if m != surv):
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

    arch_ids: set = set()
    aritz_ids: set = set()
    for cid in RECOVER_CIDS:
        row = meta.get(cid)
        if not row:
            continue
        refs = row.get("source_refs") or {}
        for sid in refs.get("archello") or []:
            arch_ids.add(int(sid))
        for sid in refs.get("architizer") or []:
            aritz_ids.add(int(sid))
    archello_imgs = _load_gallery("archello.db", "archello_projects", sorted(arch_ids))
    architizer_imgs = _load_gallery("architizer.db", "architizer_projects", sorted(aritz_ids))

    counts: Counter = Counter()
    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C13):
            n_in += 1
            cid = row.get("canonical_bld_id")
            if cid in losers:
                counts["near_merge_loser_removed"] += 1
                continue
            if cid in survivor_patch:
                row = survivor_patch[cid]
                counts["near_merge_survivor"] += 1
            if cid in RECOVER_CIDS:
                added = _recover_images(row, archello_imgs, architizer_imgs)
                counts["image_recovered"] += 1
                counts["images_added"] += added
                if row.get("is_publishable"):
                    counts["recover_now_publishable"] += 1
            if cid in spam_cids:
                reasons = list(row.get("publishability_reasons") or [])
                if "spam_candidate" not in reasons:
                    reasons.append("spam_candidate")
                row["publishability_reasons"] = reasons
                row["is_publishable"] = False
                counts["spam_unpublished"] += 1
            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    removed_this = sorted(losers)
    prev: list = []
    if C13_REPORT.exists():
        prev = json.loads(C13_REPORT.read_text(encoding="utf-8")).get("removed_canonical_ids", [])
    cumulative = sorted(set(prev) | set(removed_this))

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C13.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "near_pairs_evaluated": len(pair_seen),
        "merge_components": len(comp),
        "merge_losers_removed": len(removed_this),
        "row_count_ok": n_out == n_in - len(removed_this),
        "counts": dict(counts),
        "removed_canonical_ids": cumulative,
        "removed_this_pass": removed_this,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C14 finalize [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("removed_canonical_ids", "removed_this_pass")},
                     ensure_ascii=False, indent=2))
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
