#!/usr/bin/env python3
"""vNext WP3 (P4B engine comparison) N10 fixed sample manifest builder.

Local, deterministic, no network. Reads the frozen S6-lite building_images
projection + the legacy Neon snapshot (read-only) and writes
data/smoke_vnext/wp3_n10_v1/sample_manifest.json.

Selection rule (rule_version vnext-wp3-n10-sample-rule-v1), fixed before the run:
  1. Group S6-lite rows by canonical_bld_id. A building is *eligible* iff
     8 <= image_count <= 30 and every image URL is on ONE host:
     images.divisare.com (stratum divisare) or images.architizer.com
     (stratum architizer); the asset-id prefix must agree with the host
     ("divisare|" / "atz_asset_").
  2. Hash key = sha256("vnext-wp3-n10-v1|" + canonical_bld_id) hex; ascending.
  3. drawing_hint(image) = the original-asset filename tokens intersect
     DRAWING_HINT_TOKENS (Divisare: the asset path segment right before the
     SEO slug; Architizer: URL basename without extension; URL-decoded,
     lowercased, split on non-alphanumerics, leading/trailing digits stripped).
     drawing_hint(building) = any image has drawing_hint.
  4. Per stratum: take the first RESERVED_HINT_SLOTS (2) hinted buildings in
     hash order, then fill to 5 with the remaining eligible buildings in hash
     order (hinted or not). Final set must contain >= 3 hinted buildings
     (asserted; if the population cannot satisfy it, it is reported, not
     patched).
  5. ALL images of each chosen building are included, ordered by sort_order.
  No post-hoc replacement: the manifest is written once; re-run refuses to
  overwrite a manifest with a different selection.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
S6 = ROOT / "data/canonical/vnext_s6_lite_building_images_v1.jsonl"
LEGACY = ROOT / "data/canonical/legacy_neon_snapshot_2026q3.jsonl"
OUT_DIR = ROOT / "data/smoke_vnext/wp3_n10_v1"
OUT = OUT_DIR / "sample_manifest.json"

SEED = "vnext-wp3-n10-v1"
RULE_VERSION = "vnext-wp3-n10-sample-rule-v1"
MIN_IMAGES, MAX_IMAGES = 8, 30
PER_STRATUM = 5
RESERVED_HINT_SLOTS = 2
MIN_HINTED_TOTAL = 3
HOSTS = {"images.divisare.com": "divisare", "images.architizer.com": "architizer"}
ASSET_PREFIX = {"divisare": "divisare|", "architizer": "atz_asset_"}
DRAWING_HINT_TOKENS = sorted({
    "plan", "plans", "floorplan", "floorplans", "siteplan", "masterplan", "lageplan",
    "planimetria", "pianta", "piante", "planta", "plantas", "grundriss",
    "section", "sections", "sezione", "sezioni", "seccion", "corte", "cortes", "schnitt",
    "elevation", "elevations", "prospetto", "prospetti", "alzado", "alzados", "ansicht",
    "drawing", "drawings", "dwg", "diagram", "diagrams", "diagramma",
    "axo", "axon", "axonometric", "axonometry", "assonometria", "isometric", "sketch", "schizzo",
})


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def asset_filename(stratum: str, url: str) -> str:
    path = unquote(urlsplit(url).path)
    parts = [p for p in path.split("/") if p]
    if stratum == "divisare":
        # /images/<transform>/v1/project_images/<id>/<assetname>/<slug>.jpg
        # older form: /image/upload/<t>/v1/project_images/<id>/<file>.jpg
        if "project_images" in parts:
            i = parts.index("project_images")
            rest = parts[i + 2:]
            if len(rest) >= 2:
                return rest[0]
            if rest:
                return rest[0].rsplit(".", 1)[0]
        return parts[-1].rsplit(".", 1)[0] if parts else ""
    return parts[-1].rsplit(".", 1)[0] if parts else ""


def tokens(name: str) -> set[str]:
    out = set()
    for t in re.split(r"[^a-z0-9]+", name.lower()):
        t = re.sub(r"^\d+|\d+$", "", t)
        if t:
            out.add(t)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    by_bld: dict[str, list[dict]] = defaultdict(list)
    n_rows = 0
    with open(S6, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            n_rows += 1
            by_bld[r["canonical_bld_id"]].append({
                "image_asset_id": r["image_asset_id"], "url": r["url"],
                "sort_order": r["sort_order"],
                "representative_pixel_sha256": (r.get("provenance") or {}).get("representative_pixel_sha256"),
                "record_sha256": r.get("record_sha256"),
            })

    eligible: dict[str, list[tuple[str, str, bool]]] = {"divisare": [], "architizer": []}
    stats = {"buildings_total": len(by_bld), "rows_total": n_rows,
             "eligible": {"divisare": 0, "architizer": 0},
             "eligible_hinted": {"divisare": 0, "architizer": 0}}
    for bid, imgs in by_bld.items():
        if not (MIN_IMAGES <= len(imgs) <= MAX_IMAGES):
            continue
        strata = {HOSTS.get(urlsplit(i["url"]).hostname or "") for i in imgs}
        if len(strata) != 1 or None in strata:
            continue
        s = strata.pop()
        if not all(i["image_asset_id"].startswith(ASSET_PREFIX[s]) for i in imgs):
            continue
        for i in imgs:
            i["asset_filename"] = asset_filename(s, i["url"])
            i["drawing_hint_tokens"] = sorted(tokens(i["asset_filename"]) & set(DRAWING_HINT_TOKENS))
        hinted = any(i["drawing_hint_tokens"] for i in imgs)
        key = hashlib.sha256(f"{SEED}|{bid}".encode()).hexdigest()
        eligible[s].append((key, bid, hinted))
        stats["eligible"][s] += 1
        stats["eligible_hinted"][s] += int(hinted)

    chosen: list[tuple[str, str, str, bool, str]] = []  # (stratum, key, bid, hinted, reason)
    for s in ("divisare", "architizer"):
        order = sorted(eligible[s])
        picked = [x for x in order if x[2]][:RESERVED_HINT_SLOTS]
        sel = [(s, k, b, h, "reserved_drawing_hint_slot") for k, b, h in picked]
        taken = {b for _, b, _ in picked}
        for k, b, h in order:
            if len(sel) >= PER_STRATUM:
                break
            if b not in taken:
                sel.append((s, k, b, h, "hash_order_fill"))
                taken.add(b)
        sel.sort(key=lambda x: x[1])
        chosen.extend(sel)
    n_hinted = sum(1 for c in chosen if c[3])
    if n_hinted < MIN_HINTED_TOTAL:
        print(f"WARNING: only {n_hinted} hinted buildings available", file=sys.stderr)

    want = {c[2] for c in chosen}
    legacy: dict[str, dict] = {}
    with open(LEGACY, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            # cheap prefilter before full parse
            m = re.search(r'"canonical_bld_id":\s*"(bld_\d+)"', line)
            if m and m.group(1) in want:
                d = json.loads(line)
                legacy[d["canonical_bld_id"]] = {
                    k: d.get(k) for k in ("name", "location_country", "location_city", "program",
                                          "typology_primary", "project_year", "identity_source",
                                          "is_publishable", "display_cover_url")}

    buildings, idx = [], 0
    for s, key, bid, hinted, reason in chosen:
        imgs = sorted(by_bld[bid], key=lambda i: i["sort_order"])
        rows = []
        for i in imgs:
            rows.append({"idx": idx, **i})
            idx += 1
        lg = legacy.get(bid, {})
        buildings.append({
            "canonical_bld_id": bid, "stratum": s, "hash_key": key,
            "selection_reason": reason, "drawing_hint": hinted,
            "name": lg.get("name"), "location_country": lg.get("location_country"),
            "location_city": lg.get("location_city"), "program": lg.get("program"),
            "typology_primary": lg.get("typology_primary"), "project_year": lg.get("project_year"),
            "legacy_display_cover_url": lg.get("display_cover_url"),
            "legacy_found": bid in legacy,
            "image_count": len(rows), "images": rows,
        })

    manifest = {
        "manifest_version": "vnext-wp3-n10-sample-v1", "rule_version": RULE_VERSION,
        "seed": SEED, "builder": "tools/vnext_wp3_n10_sample.py",
        "builder_sha256": sha256_file(Path(__file__)),
        "rule": {
            "eligible": f"{MIN_IMAGES}<=image_count<={MAX_IMAGES}; all image URLs on one host in {sorted(HOSTS)}; asset-id prefix agrees",
            "hash_key": f"sha256('{SEED}|'+canonical_bld_id) hex ascending",
            "per_stratum": PER_STRATUM, "reserved_drawing_hint_slots_per_stratum": RESERVED_HINT_SLOTS,
            "min_hinted_total": MIN_HINTED_TOTAL,
            "drawing_hint": "asset filename tokens (Divisare: asset path segment before SEO slug; Architizer: basename w/o ext; url-decoded, lowercased, split on non-alnum, leading/trailing digits stripped) intersect drawing_hint_tokens",
            "drawing_hint_tokens": DRAWING_HINT_TOKENS,
            "fill": "after reserved hinted slots, remaining eligible buildings in hash order; final per-stratum list sorted by hash key",
            "no_post_hoc_replacement": True,
        },
        "inputs": {
            str(S6.relative_to(ROOT)).replace("\\", "/"): sha256_file(S6),
            str(LEGACY.relative_to(ROOT)).replace("\\", "/"): sha256_file(LEGACY),
        },
        "population_stats": stats,
        "summary": {"buildings": len(buildings), "images": idx,
                    "per_stratum_buildings": {s: sum(b["stratum"] == s for b in buildings) for s in HOSTS.values()},
                    "per_stratum_images": {s: sum(b["image_count"] for b in buildings if b["stratum"] == s) for s in HOSTS.values()},
                    "hinted_buildings": n_hinted,
                    "hinted_images": sum(1 for b in buildings for i in b["images"] if i["drawing_hint_tokens"]),
                    "min_hinted_satisfied": n_hinted >= MIN_HINTED_TOTAL},
        "buildings": buildings,
    }
    if "--dry-run" in sys.argv:
        print(json.dumps(manifest["summary"], ensure_ascii=False))
        print(json.dumps(stats))
        for b in buildings:
            print(b["stratum"], b["canonical_bld_id"], b["image_count"], b["drawing_hint"],
                  b["selection_reason"], b["name"], b["location_country"], b["program"],
                  [i["drawing_hint_tokens"] for i in b["images"] if i["drawing_hint_tokens"]])
        return 0
    if OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8"))
        if [b["canonical_bld_id"] for b in old["buildings"]] != [b["canonical_bld_id"] for b in buildings]:
            print("REFUSE: existing manifest has a different selection (no post-hoc replacement)", file=sys.stderr)
            return 2
        print("manifest exists with identical selection; not rewritten")
        return 0
    OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(manifest["summary"], ensure_ascii=False))
    print(json.dumps(stats))
    for b in buildings:
        print(b["stratum"], b["canonical_bld_id"], b["image_count"], b["drawing_hint"], b["selection_reason"], b["name"], b["location_country"], b["program"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
