#!/usr/bin/env python3
"""Cross-building image duplicate audit — read-only.

The existing phash logic (canonical/match_phash_check.py, image_dedup) is a
within-building deduper and a building-merge false-positive gate. Neither
prevents the SAME image phash appearing on several DIFFERENT canonical
buildings — which is what makes the make_web feed feel repetitive.

This tool maps every image phash (carried on the `all_images` /
`best_image_per_cluster` image objects) to the set of canonical buildings that
contain it, then reports:

  exact-dup  — one phash on >= 2 canonical buildings
  near-dup   — two phashes within Hamming 8 on different buildings
               (9-band LSH so 171k phashes are not compared pairwise)

Each exact group is auto-classified:
  mass_duplicate            — phash on > 20 buildings (stock / default image)
  split_canonical_candidate — buildings look like one building split into rows
                              (same country + shared architect or name >= 90)
  review                    — everything else (wrong attachment vs shared
                              context needs image-content judgement)

Output: a JSON summary + a JSONL of every cross-building cluster for the
remediation step. Read-only — no artifact / Neon writes.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C12 = CCR / "canonical_buildings_strict.completeness_c12_taxonomy.json"
REPORT = ROOT / "data/reports/canonical_v2_image_dup_audit.json"
CLUSTERS = ROOT / "data/reports/canonical_v2_image_dup_clusters.jsonl"

HAMMING_MAX = 8
MASS_DUP = 20            # phash on more than this many buildings = stock image
_BANDS = [(0, 7), (7, 14), (14, 21), (21, 28), (28, 35),
          (35, 42), (42, 49), (49, 56), (56, 64)]  # 9 bands -> Hamming<=8 pigeonhole


def _img_url(image):
    return image.get("url") if isinstance(image, dict) else image


def _norm(name) -> str:
    return " ".join(str(name or "").casefold().split())


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _row_images(row):
    """Yield image objects carrying a phash from all_images + best_image_per_cluster."""
    for im in row.get("all_images") or []:
        if isinstance(im, dict) and im.get("phash"):
            yield im
    bipc = row.get("best_image_per_cluster")
    if isinstance(bipc, dict):
        for im in bipc.values():
            if isinstance(im, dict) and im.get("phash"):
                yield im


def _classify(cids: list[str], meta: dict) -> str:
    if len(cids) > MASS_DUP:
        return "mass_duplicate"
    countries = {meta[c]["country"] for c in cids}
    if len(countries) != 1 or None in countries:
        return "review"
    arch_sets = [meta[c]["arch"] for c in cids]
    common_arch = frozenset.intersection(*arch_sets) if arch_sets else frozenset()
    names = [meta[c]["name"] for c in cids]
    name_ok = all(fuzz.token_set_ratio(a, b) >= 90
                  for a, b in combinations(names, 2)) if len(names) > 1 else True
    if common_arch or name_ok:
        return "split_canonical_candidate"
    return "review"


def main() -> int:
    if not C12.exists():
        print(f"FATAL: missing {C12}", file=sys.stderr)
        return 2

    phash_to_cids: dict = defaultdict(set)
    cover_phash_to_cids: dict = defaultdict(set)
    meta: dict = {}
    rows = 0
    for row in iter_buildings(C12):
        cid = row.get("canonical_bld_id")
        if not cid:
            continue
        rows += 1
        meta[cid] = {
            "name": _norm(row.get("name")),
            "country": row.get("location_country"),
            "arch": frozenset(row.get("architect_canonical_ids") or []),
            "publishable": bool(row.get("is_publishable")),
        }
        url_phash: dict = {}
        for im in _row_images(row):
            ph = str(im["phash"])
            phash_to_cids[ph].add(cid)
            if _img_url(im):
                url_phash[_img_url(im)] = ph
        cover = row.get("display_cover_url")
        if cover and cover in url_phash:
            cover_phash_to_cids[url_phash[cover]].add(cid)

    # exact duplicates — one phash on >= 2 buildings
    exact = {ph: sorted(cids) for ph, cids in phash_to_cids.items() if len(cids) >= 2}
    cover_exact = {ph: sorted(cids) for ph, cids in cover_phash_to_cids.items()
                   if len({c for c in cids if meta[c]["publishable"]}) >= 2}

    # near duplicates — 9-band LSH, then verified Hamming
    band_index: dict = defaultdict(set)
    for ph in phash_to_cids:
        if len(ph) == 64:
            for bi, (a, b) in enumerate(_BANDS):
                band_index[(bi, ph[a:b])].add(ph)
    near_pairs: set = set()
    for bucket in band_index.values():
        if len(bucket) < 2:
            continue
        for p1, p2 in combinations(sorted(bucket), 2):
            if _hamming(p1, p2) <= HAMMING_MAX:
                if phash_to_cids[p1] | phash_to_cids[p2] != phash_to_cids[p1] \
                        or phash_to_cids[p1] != phash_to_cids[p2]:
                    near_pairs.add((p1, p2))
    near_cross = [(p1, p2) for p1, p2 in near_pairs
                  if len(phash_to_cids[p1] | phash_to_cids[p2]) >= 2]

    by_class: Counter = Counter()
    clusters: list = []
    exact_bld: set = set()
    for ph, cids in exact.items():
        cls = _classify(cids, meta)
        by_class[cls] += 1
        exact_bld.update(cids)
        clusters.append({"kind": "exact", "phash": ph, "class": cls,
                         "n_buildings": len(cids), "cids": cids,
                         "is_cover_dup": ph in cover_exact})
    near_bld: set = set()
    for p1, p2 in near_cross:
        cids = sorted(phash_to_cids[p1] | phash_to_cids[p2])
        near_bld.update(cids)
        clusters.append({"kind": "near", "phash": [p1, p2],
                         "hamming": _hamming(p1, p2),
                         "n_buildings": len(cids), "cids": cids})

    CLUSTERS.parent.mkdir(parents=True, exist_ok=True)
    with CLUSTERS.open("w", encoding="utf-8") as f:
        for c in clusters:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    examples = sorted((c for c in clusters if c["kind"] == "exact"),
                      key=lambda c: -c["n_buildings"])[:25]
    for ex in examples:
        ex["names"] = [meta[c]["name"] for c in ex["cids"][:6]]

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C12.relative_to(ROOT)),
        "rows": rows,
        "phashes_total": len(phash_to_cids),
        "exact_dup": {
            "groups": len(exact),
            "buildings_touched": len(exact_bld),
            "by_class": dict(by_class),
            "cover_dup_groups": len(cover_exact),
        },
        "near_dup": {
            "cross_building_pairs": len(near_cross),
            "buildings_touched": len(near_bld),
        },
        "exact_examples": examples,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"image dup audit -> {REPORT.relative_to(ROOT)}")
    print(f"  rows {rows:,} | unique phashes {len(phash_to_cids):,}")
    print(f"  exact-dup groups: {len(exact):,} | buildings: {len(exact_bld):,}")
    print(f"    by class: {dict(by_class)}")
    print(f"    groups where the dup is a display cover: {len(cover_exact):,}")
    print(f"  near-dup cross-building pairs: {len(near_cross):,} | "
          f"buildings: {len(near_bld):,}")
    print(f"  all clusters -> {CLUSTERS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
