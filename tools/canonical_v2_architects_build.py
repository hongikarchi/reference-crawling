#!/usr/bin/env python3
"""Build canonical_v2 architects (firms) table from existing data.

For each architect in id_registry_architects.json:
  - Merge metadata from source DBs (divisare > archello > architizer).
  - Reverse-index from canonical_v2_buildings.completeness_c23_final to
    collect this firm's buildings.
  - Aggregate top-K style/typology/material signatures.
  - Compute portfolio embedding = mean of publishable building embeddings.
  - Mark is_recommendable.

Output: data/canonical/canonical_architects_v2.json (streaming JSON array).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
BUILDINGS = CCR / "canonical_buildings_strict_embedded.completeness_c23_final.json"
REGISTRY = ROOT / "data/id_registry_architects.json"
CRAWL = ROOT / "data/crawl"

OUT = ROOT / "data/canonical/canonical_architects_v2.json"
REPORT = ROOT / "data/reports/canonical_v2_architects_build_report.json"

TOP_K = 5

# Source priority for field merging: divisare > archello > architizer
_SOURCE_PRIORITY = ("divisare", "archello", "architizer")


# --- Load source DB firm tables ---
def _load_archello_firms():
    out = {}  # brand_id (str) -> dict
    conn = sqlite3.connect(str(CRAWL / "archello.db"))
    try:
        for r in conn.execute(
            "SELECT slug, brand_id, name, description_short, about, "
            "location_country, location_city, offices, project_count_archello, "
            "website, social_links, cover_image_url FROM archello_firms"
        ):
            (slug, brand_id, name, desc_short, about, country, city,
             offices_raw, n_proj, website, social_raw, cover) = r
            if brand_id is None:
                continue
            try:
                offices = json.loads(offices_raw) if offices_raw else []
            except (json.JSONDecodeError, TypeError):
                offices = []
            try:
                social = json.loads(social_raw) if social_raw else {}
            except (json.JSONDecodeError, TypeError):
                social = {}
            out[str(brand_id)] = {
                "slug": slug, "name": name,
                "description": about or desc_short,
                "country": country, "city": city,
                "offices": offices, "website": website,
                "social_links": social, "logo_url": cover,
                "n_projects": n_proj,
            }
    finally:
        conn.close()
    return out


def _load_architizer_firms():
    out = {}  # slug -> dict
    conn = sqlite3.connect(str(CRAWL / "architizer.db"))
    try:
        for r in conn.execute(
            "SELECT slug, name, office_locations, description, awards_summary, "
            "project_count_seen, social_links FROM architizer_firms"
        ):
            (slug, name, offices_raw, desc, awards, n_proj, social_raw) = r
            if not slug:
                continue
            try:
                offices = json.loads(offices_raw) if offices_raw else []
            except (json.JSONDecodeError, TypeError):
                offices = []
            try:
                social = json.loads(social_raw) if social_raw else {}
            except (json.JSONDecodeError, TypeError):
                social = {}
            out[slug] = {
                "slug": slug, "name": name,
                "description": desc, "awards": awards,
                "offices": offices, "social_links": social,
                "n_projects": n_proj,
            }
    finally:
        conn.close()
    return out


def _load_divisare_architects():
    out = {}  # id (str) -> dict
    conn = sqlite3.connect(str(CRAWL / "divisare.db"))
    try:
        for r in conn.execute(
            "SELECT id, slug, name, description, country, city, website, "
            "phone, project_count_seen FROM divisare_architects"
        ):
            (aid, slug, name, desc, country, city, website, phone, n_proj) = r
            out[str(aid)] = {
                "slug": slug, "name": name,
                "description": desc,
                "country": country, "city": city,
                "website": website, "phone": phone,
                "n_projects": n_proj,
            }
    finally:
        conn.close()
    return out


# --- Merge metadata with source priority ---
def _first_nonempty(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def _merge_metadata(source_refs, src_data):
    """source_refs: {source: [ids]} from registry. src_data: per-source dicts."""
    # Per-source resolved entry
    resolved = {}
    for src in _SOURCE_PRIORITY + ("metalocus",):
        sids = source_refs.get(src) or []
        if not sids:
            continue
        if src == "metalocus":
            resolved[src] = {"slug": None, "name": None}
            continue
        # Use first available sid for this source
        for sid in sids:
            data = src_data.get(src, {}).get(str(sid))
            if data:
                resolved[src] = data
                break

    # Build source_urls + source_descriptions per source
    source_urls = {}
    source_descriptions = {}
    for src, data in resolved.items():
        slug = data.get("slug") if data else None
        if src == "divisare" and slug:
            sids = source_refs.get(src) or []
            if sids:
                source_urls[src] = f"https://divisare.com/authors/{sids[0]}-{slug}"
        elif src == "architizer" and slug:
            source_urls[src] = f"https://architizer.com/firms/{slug}/"
        elif src == "archello" and slug:
            source_urls[src] = f"https://archello.com/brand/{slug}"
        if data and data.get("description"):
            source_descriptions[src] = data["description"]

    # Priority field merge: divisare > archello > architizer
    div = resolved.get("divisare") or {}
    arc = resolved.get("archello") or {}
    azi = resolved.get("architizer") or {}

    website = _first_nonempty(div.get("website"), arc.get("website"))
    country = _first_nonempty(div.get("country"), arc.get("country"))
    city = _first_nonempty(div.get("city"), arc.get("city"))
    phone = div.get("phone")
    description = _first_nonempty(div.get("description"), arc.get("description"),
                                  azi.get("description"))
    # social_links: archello has 5 platforms, architizer 2-3. Merge w/ archello win.
    social = {}
    for src in ("architizer", "archello"):
        s = (resolved.get(src) or {}).get("social_links") or {}
        for k, v in s.items():
            if v and k not in social:
                social[k] = v
    # offices: union
    offices = []
    seen_office = set()
    for src in ("archello", "architizer"):
        for o in (resolved.get(src) or {}).get("offices") or []:
            if not isinstance(o, dict):
                continue
            key = (o.get("city"), o.get("country"), o.get("address"))
            if key in seen_office:
                continue
            seen_office.add(key)
            offices.append(o)
    logo = arc.get("logo_url")

    return {
        "website": website,
        "country": country,
        "city": city,
        "phone": phone,
        "description": description,
        "social_links": social,
        "office_locations": offices,
        "logo_url": logo,
        "source_urls": source_urls,
        "source_descriptions": source_descriptions,
    }


# --- Aggregate building features ---
def _aggregate_buildings(rows):
    """rows: list of full building dicts. Returns aggregate dict."""
    counters = {
        "program": Counter(),
        "style": Counter(),
        "color_tone": Counter(),
        "atmosphere": Counter(),
        "material_visual": Counter(),
        "typologies": Counter(),
        "architectural_elements": Counter(),
    }
    countries = set()
    cities = set()
    years = []
    publishable_count = 0
    embeddings_pub = []
    embeddings_all = []
    hero_candidates = []
    building_ids = []
    for r in rows:
        bid = r.get("canonical_bld_id")
        building_ids.append(bid)
        is_pub = bool(r.get("is_publishable"))
        if is_pub:
            publishable_count += 1
        emb = r.get("embedding")
        if isinstance(emb, list) and len(emb) == 384:
            embeddings_all.append(emb)
            if is_pub:
                embeddings_pub.append(emb)
        # singular fields
        for fld in ("program", "style", "color_tone", "atmosphere"):
            v = r.get(fld)
            if v:
                counters[fld][v] += 1
        # array fields
        for v in r.get("material_visual") or []:
            if v:
                counters["material_visual"][v] += 1
        for v in r.get("architectural_elements") or []:
            if v:
                counters["architectural_elements"][v] += 1
        # typology
        tp = r.get("typology_primary")
        if tp:
            counters["typologies"][tp] += 1
        for v in r.get("typology_tags") or []:
            if v:
                counters["typologies"][v] += 1
        # geo
        c = r.get("location_country")
        ci = r.get("location_city")
        if c:
            countries.add(c)
        if ci:
            cities.add(ci)
        # year
        y = r.get("project_year")
        if isinstance(y, int):
            years.append(y)
        # hero candidate
        if is_pub:
            tier = r.get("confidence_tier") or "T3"
            tier_rank = {"T1": 0, "T2": 1, "T3": 2}.get(tier, 3)
            ns = r.get("n_sources") or 0
            hero_candidates.append((tier_rank, -ns, bid, r.get("display_cover_url")))

    # Top-K
    def top_k(c):
        return [k for k, _ in c.most_common(TOP_K)]

    feature_distribution = {fld: dict(cnt) for fld, cnt in counters.items()}

    # Embedding: prefer publishable mean
    pool = embeddings_pub if embeddings_pub else embeddings_all
    if pool:
        emb = np.mean(np.array(pool, dtype=np.float32), axis=0)
        portfolio_embedding = emb.tolist()
    else:
        portfolio_embedding = None

    hero = None
    if hero_candidates:
        hero_candidates.sort()
        hero = hero_candidates[0][2]

    return {
        "building_ids": sorted(building_ids),
        "n_buildings": len(rows),
        "n_buildings_publishable": publishable_count,
        "countries": sorted(countries),
        "cities": sorted(cities),
        "top_programs": top_k(counters["program"]),
        "top_styles": top_k(counters["style"]),
        "top_color_tones": top_k(counters["color_tone"]),
        "top_atmospheres": top_k(counters["atmosphere"]),
        "top_materials": top_k(counters["material_visual"]),
        "top_typologies": top_k(counters["typologies"]),
        "top_arch_elements": top_k(counters["architectural_elements"]),
        "feature_distribution": feature_distribution,
        "earliest_project_year": min(years) if years else None,
        "latest_project_year": max(years) if years else None,
        "hero_building_id": hero,
        "portfolio_embedding": portfolio_embedding,
    }


def _confidence_tier(n_sources):
    if n_sources >= 3:
        return "T1"
    if n_sources == 2:
        return "T2"
    return "T3"


# --- main ---
def main() -> int:
    for p in (BUILDINGS, REGISTRY):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    print("loading source DB firm tables...", file=sys.stderr)
    src_data = {
        "archello": _load_archello_firms(),
        "architizer": _load_architizer_firms(),
        "divisare": _load_divisare_architects(),
    }
    print(f"  archello firms: {len(src_data['archello'])}, "
          f"architizer: {len(src_data['architizer'])}, "
          f"divisare: {len(src_data['divisare'])}", file=sys.stderr)

    print("loading architect registry...", file=sys.stderr)
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    active_archs = {k: v for k, v in registry.items()
                    if not v.get("redirected_to")}
    print(f"  registry: {len(registry)} total, {len(active_archs)} active",
          file=sys.stderr)

    print("building arch_id → buildings reverse index...", file=sys.stderr)
    arch_to_rows = defaultdict(list)
    n_rows = 0
    for row in iter_buildings(BUILDINGS):
        n_rows += 1
        for arch_id in row.get("architect_canonical_ids") or []:
            arch_to_rows[arch_id].append(row)
    print(f"  scanned {n_rows} buildings, {len(arch_to_rows)} architects "
          f"have buildings", file=sys.stderr)

    counts = Counter()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    n_out = 0
    n_skipped_no_buildings = 0
    n_recommendable = 0
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"architects":[')
        for i, (arch_id, reg) in enumerate(sorted(active_archs.items()), 1):
            buildings = arch_to_rows.get(arch_id) or []
            if not buildings:
                n_skipped_no_buildings += 1
                continue

            source_refs = reg.get("source_refs") or {}
            n_sources = sum(1 for k, v in source_refs.items() if v)
            confidence_tier = _confidence_tier(n_sources)

            metadata = _merge_metadata(source_refs, src_data)
            agg = _aggregate_buildings(buildings)

            if agg["portfolio_embedding"] is None:
                counts["skipped_no_embedding"] += 1
                continue

            names = reg.get("names") or []
            # canonical_name = first non-source-branded name
            canonical_name = None
            for n in names:
                if " - Architizer" in n or " - Archello" in n or " - Divisare" in n:
                    continue
                canonical_name = n
                break
            if not canonical_name and names:
                canonical_name = names[0]
            if not canonical_name:
                continue
            name_alts = sorted(set(names) - {canonical_name})

            is_recommendable = (
                agg["n_buildings_publishable"] >= 3
                and bool(metadata.get("website")
                         or metadata.get("description")
                         or metadata.get("country"))
            )
            if is_recommendable:
                n_recommendable += 1

            record = {
                "canonical_arch_id": arch_id,
                "canonical_name": canonical_name,
                "name_alts": name_alts,
                "description": metadata["description"],
                "primary_country": metadata["country"],
                "primary_city": metadata["city"],
                "office_locations": metadata["office_locations"],
                "website": metadata["website"],
                "email": None,  # no source has email
                "phone": metadata["phone"],
                "social_links": metadata["social_links"],
                **agg,
                "source_refs": source_refs,
                "source_urls": metadata["source_urls"],
                "source_descriptions": metadata["source_descriptions"],
                "n_sources": n_sources,
                "confidence_tier": confidence_tier,
                "logo_url": metadata["logo_url"],
                "is_recommendable": is_recommendable,
            }
            fout.write(("," if n_out else "") + json.dumps(record, ensure_ascii=False))
            n_out += 1
            if i % 5000 == 0:
                print(f"  built {i}/{len(active_archs)} ({n_out} written)",
                      file=sys.stderr)
        fout.write("]}")

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "output": str(OUT.relative_to(ROOT)),
        "registry_total": len(registry),
        "registry_active": len(active_archs),
        "buildings_scanned": n_rows,
        "architects_with_buildings": len(arch_to_rows),
        "architects_written": n_out,
        "skipped_no_buildings": n_skipped_no_buildings,
        "skipped_no_embedding": counts["skipped_no_embedding"],
        "is_recommendable": n_recommendable,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"architects build -> {OUT.relative_to(ROOT)}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
