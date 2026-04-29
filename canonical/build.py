#!/usr/bin/env python3
"""Stage B-3: assemble canonical_buildings.json from all matching outputs.

Inputs:
  data/enrich/4_buildings_final.json                       — metalocus content
  data/canonical/metalocus_architect_clusters.json             — Stage A output
  data/canonical/match/metalocus_architect_to_divisare.json    — Stage B-1 output
  data/canonical/match/metalocus_to_divisare_buildings.json    — Stage B-2 output
  data/crawl/divisare.db                                   — Phase 1 crawl

Output:
  data/canonical/canonical_buildings.json — list of CanonicalBuilding-shaped dicts

Field-merge policy (per `~/.claude/plans/db-fuzzy-lerdorf.md`):
  • Structure (name, location, year, area_sqm, divisare_credits, gallery URLs)
    → Divisare authoritative when matched; metalocus fallback when Divisare NULL.
  • Content (description, visual_description, image_paths, embedding,
    program/style/atmosphere/color_tone) → metalocus authoritative (Divisare
    lite has no enrichment).
  • Each set field is recorded in `provenance` — see CanonicalBuilding doc.

Orphan handling: metalocus buildings without a Divisare PROJECT match still
become canonical rows (divisare_id=None). If their architect cluster matched
a Divisare architect (Stage B-1), architect_canonical_ids is populated even
without a project link — preserves the consolidated identity.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Optional

from core import vocab
from canonical.schema import (
    CanonicalBuilding, SOURCE_DIVISARE, SOURCE_METALOCUS, SOURCE_DERIVED,
)

METALOC_PATH      = "data/enrich/4_buildings_final.json"
CLUSTERS_PATH     = "data/canonical/metalocus_architect_clusters.json"
ARCH_MATCH_PATH   = "data/canonical/match/metalocus_architect_to_divisare.json"
BLDG_MATCH_PATH   = "data/canonical/match/metalocus_to_divisare_buildings.json"
DIVISARE_DB       = "data/crawl/divisare.db"
OUTPUT_PATH       = "data/canonical/canonical_buildings.json"
STRICT_OUTPUT_PATH = "data/canonical/canonical_buildings_strict.json"

# --------------------------------------------------------------------------
# Strict-mode filters: drop arch-only records that don't look like buildings.
# Pure orphans (no Divisare attribution at all) are always dropped in strict
# mode — they live in the metalocus source database and we don't want them
# in the canonical artefact.
# --------------------------------------------------------------------------

import re

# Action verbs that imply the row is an article-event entry, not a building
_ARTICLE_VERB_RE = re.compile(
    r"\b(?:wins?|won|announces?|announced|reveals?|revealed|applies|applied|"
    r"uses?|used|opens?|opened|presents?|presented|launches?|launched|"
    r"unveil(?:s|ed)?|debuts?|debuted|nominates?|nominated)\b",
    re.IGNORECASE,
)

# Words that signal exhibitions / events / awards rather than buildings
_NON_BUILDING_KEYWORDS_RE = re.compile(
    r"\b(?:biennale|symposium|conference|lecture|award|nominat|finalist|"
    r"winner|competition\s+for|short\s*list|long\s*list|prize)\b",
    re.IGNORECASE,
)

MAX_BUILDING_NAME_LEN = 70

# Strips a trailing " by SOMEONE" credit. We drop everything from the last
# bare " by " onward to recover real names like:
#   "Cafayate Convention Center by Ignacio Carón, Fabio Estremera, …"
#       → "Cafayate Convention Center"
# Refusing to strip when the substring before " by " is too short prevents
# trimming entries like "Renovation by X" → "Renovation".
_BY_SUFFIX_RE = re.compile(r"\s+by\s+", re.IGNORECASE)

# Strips an editorial-hook prefix that ends in a period followed by the
# real name. Heuristic: prefix is a sentence (>=20 chars, ends with .),
# remainder is shorter, has at least one alpha + spaces.
_HOOK_PREFIX_RE = re.compile(r"^([A-Z][^\.]{19,}\.)\s+(.{4,})$")


def _clean_building_name(name: str) -> str:
    """Recover the canonical building name from an article-style metalocus entry.
    Idempotent: safe to call on already-clean names.
    """
    if not name:
        return name
    cleaned = name.strip()

    # Editorial hook: 'Some sentence. Real Name'
    m = _HOOK_PREFIX_RE.match(cleaned)
    if m:
        candidate = m.group(2).strip()
        if len(candidate) >= 4:
            cleaned = candidate

    # Strip ", … by ARCHITECT" credit suffix (LAST occurrence of " by ")
    by_matches = list(_BY_SUFFIX_RE.finditer(cleaned))
    if by_matches:
        cut = by_matches[-1].start()
        candidate = cleaned[:cut].strip().rstrip(".,;:")
        if len(candidate) >= 8:
            cleaned = candidate

    return cleaned


def _is_article_title(name: str) -> tuple[bool, str]:
    """Returns (drop, reason) on the (already-cleaned) name.

    Note: leading-quote names (e.g. '"Lighthouse"') are KEPT — they're often
    real building names with the architect's stylistic quoting. The exhibition
    cases like '"Tea for Two" Exhibition' get caught by the keyword rule.
    """
    if not name:
        return True, "empty_name"
    if len(name) > MAX_BUILDING_NAME_LEN:
        return True, "name_too_long"
    if _ARTICLE_VERB_RE.search(name):
        return True, "action_verb"
    if _NON_BUILDING_KEYWORDS_RE.search(name):
        return True, "non_building_keyword"
    return False, ""


def _load_json(path: str):
    with open(path) as f:
        return json.load(f)


def _load_arch_map() -> dict[str, dict]:
    """{cluster_id: {divisare_id, divisare_name, divisare_country}}"""
    data = _load_json(ARCH_MATCH_PATH)
    out = {}
    for m in data["matches"]:
        if m["verdict"] in ("auto_accept", "accept_with_country") and m["divisare_id"]:
            out[m["metaloc_id"]] = {
                "divisare_id": m["divisare_id"],
                "divisare_name": m["divisare_name"],
                "divisare_country": m["divisare_country"],
            }
    return out


def _load_building_match_map() -> tuple[dict[str, dict], dict[int, str]]:
    """({bid: match_dict for confident matches},
       {divisare_id: bid that primarily owns this divisare_id})

    When several metalocus buildings claim the same Divisare project (real
    metalocus duplicates that stage2_dedup missed), the first encountered
    bid keeps the divisare_id; later bids fall back to orphan + a note in
    the canonical record's provenance.
    """
    data = _load_json(BLDG_MATCH_PATH)
    out_match = {}
    primary_owner: dict[int, str] = {}
    duplicates: dict[str, int] = {}  # bid → div_id it would have claimed
    for m in data["matches"]:
        if m["verdict"] not in ("accept_high", "accept_medium"):
            continue
        bid = m["metalocus_building_id"]
        did = m.get("divisare_id")
        if did is None:
            continue
        if did in primary_owner:
            duplicates[bid] = did
            continue
        primary_owner[did] = bid
        out_match[bid] = m
    if duplicates:
        print(f"  ⚠ {len(duplicates)} metalocus duplicates detected "
              f"(multiple bids → same divisare_id); later bids will be orphaned")
    return out_match, duplicates


def _load_divisare_projects(ids_needed: set[int]) -> dict[int, dict]:
    """Bulk load Divisare project rows for given IDs."""
    if not ids_needed:
        return {}
    out = {}
    with sqlite3.connect(DIVISARE_DB) as conn:
        conn.row_factory = sqlite3.Row
        # SQLite has a parameter-count limit; chunk to be safe
        ids_list = list(ids_needed)
        chunk = 500
        for start in range(0, len(ids_list), chunk):
            sub = ids_list[start:start + chunk]
            placeholders = ",".join("?" * len(sub))
            for r in conn.execute(
                f"SELECT id, slug, name, architect_ids, architect_names, "
                f"location_country, location_city, project_year, area_sqm, "
                f"abstract, description, tag_slugs, cover_image_url, "
                f"gallery_urls, credits "
                f"FROM divisare_projects WHERE id IN ({placeholders})",
                sub,
            ):
                d = dict(r)
                # Decode JSON fields
                for jk in ("architect_ids", "architect_names", "tag_slugs",
                           "gallery_urls", "credits"):
                    raw = d.get(jk)
                    if raw:
                        try:
                            d[jk] = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            d[jk] = None
                out[d["id"]] = d
    return out


def _building_image_paths(b: dict) -> list[str]:
    """Return relative image paths. metalocus stores entries as
    {"filename": "...jpg", "order": N, ...} with the convention that the
    file lives at `images/{building_id}/{filename}` (post stage2_dedup
    reorganization).
    """
    out: list[str] = []
    bid = b.get("building_id")
    for img in b.get("images") or []:
        if isinstance(img, str):
            out.append(img)
            continue
        if not isinstance(img, dict):
            continue
        # Some saves include an explicit path/local_path; honour it
        p = img.get("path") or img.get("local_path")
        if p:
            out.append(p)
            continue
        fn = img.get("filename")
        if fn and bid:
            out.append(f"images/{bid}/{fn}")
    return out


def _build_one(b: dict, *, arch_map: dict, building_match: Optional[dict],
               div_projects: dict[int, dict],
               building_to_clusters: dict[str, list[str]]) -> CanonicalBuilding:
    cb = CanonicalBuilding()
    bid = b["building_id"]

    # ---- Identity --------------------------------------------------------
    cb.set_field("metalocus_building_id", bid, SOURCE_METALOCUS)

    div_proj = None
    if building_match and building_match.get("divisare_id"):
        div_proj = div_projects.get(building_match["divisare_id"])
        if div_proj:
            cb.set_field("divisare_id", div_proj["id"], SOURCE_DIVISARE)
            cb.set_field("divisare_slug", div_proj["slug"], SOURCE_DIVISARE)

    # ---- Architect canonical IDs (from Stage A + B-1) -------------------
    cluster_ids = building_to_clusters.get(bid) or []
    matched_arch_ids: list[int] = []
    matched_arch_names: list[str] = []
    for cid in cluster_ids:
        am = arch_map.get(cid)
        if am:
            matched_arch_ids.append(am["divisare_id"])
            if am.get("divisare_name"):
                matched_arch_names.append(am["divisare_name"])
    if matched_arch_ids:
        cb.set_field("architect_canonical_ids", matched_arch_ids, SOURCE_DERIVED)
    if matched_arch_names:
        cb.set_field("architect_names", matched_arch_names, SOURCE_DIVISARE)
    elif b.get("architect"):
        # Fallback to raw metalocus architect string when no Divisare match
        cb.set_field("architect_names", [b["architect"]], SOURCE_METALOCUS)

    # ---- Name / location / year (Divisare authoritative when matched) ---
    div_name = div_proj.get("name") if div_proj else None
    metaloc_name = b.get("name_en") or b.get("project_name")
    if div_name:
        cb.set_field("name", div_name, SOURCE_DIVISARE)
    elif metaloc_name:
        cb.set_field("name", metaloc_name, SOURCE_METALOCUS)

    div_country = div_proj.get("location_country") if div_proj else None
    if div_country:
        cb.set_field("location_country", div_country, SOURCE_DIVISARE)
    elif b.get("location_country"):
        cb.set_field("location_country", b["location_country"], SOURCE_METALOCUS)

    div_city = div_proj.get("location_city") if div_proj else None
    if div_city:
        cb.set_field("location_city", div_city, SOURCE_DIVISARE)
    elif b.get("city"):
        cb.set_field("location_city", b["city"], SOURCE_METALOCUS)

    div_year = div_proj.get("project_year") if div_proj else None
    if div_year:
        cb.set_field("project_year", div_year, SOURCE_DIVISARE)
    elif b.get("year"):
        try:
            cb.set_field("project_year", int(b["year"]), SOURCE_METALOCUS)
        except (TypeError, ValueError):
            pass

    # ---- Divisare-only structural fields (only when matched) ------------
    if div_proj:
        if div_proj.get("area_sqm") is not None:
            cb.set_field("area_sqm", float(div_proj["area_sqm"]), SOURCE_DIVISARE)
        if div_proj.get("credits"):
            cb.set_field("divisare_credits", div_proj["credits"], SOURCE_DIVISARE)
        if div_proj.get("tag_slugs"):
            cb.set_field("divisare_tags", div_proj["tag_slugs"], SOURCE_DIVISARE)
        if div_proj.get("cover_image_url"):
            cb.set_field("cover_image_url_divisare",
                         div_proj["cover_image_url"], SOURCE_DIVISARE)
        if div_proj.get("gallery_urls"):
            cb.set_field("divisare_gallery_urls",
                         div_proj["gallery_urls"], SOURCE_DIVISARE)
        if div_proj.get("abstract"):
            cb.set_field("abstract", div_proj["abstract"], SOURCE_DIVISARE)

    # ---- metalocus content (description, vocab fields, images, embed) ---
    # Divisare lite typically has no description; metalocus's enriched
    # description is the user-facing text.
    if b.get("description"):
        cb.set_field("description", b["description"], SOURCE_METALOCUS)
    elif div_proj and div_proj.get("description"):
        # Divisare deep-fetch description (rare in lite mode but possible)
        cb.set_field("description", div_proj["description"], SOURCE_DIVISARE)

    # ---- Phase 14b: description_per_source map (for concat enrich) ----
    # Populated for every row where ≥1 source has a description; harness
    # later reads this and passes the concatenation to text enrich.
    desc_map: dict = {}
    if b.get("description"):
        desc_map["metalocus"] = b["description"]
    if div_proj:
        # Divisare: prefer the long description; fall back to abstract
        if div_proj.get("description"):
            desc_map["divisare"] = div_proj["description"]
        elif div_proj.get("abstract"):
            desc_map["divisare"] = div_proj["abstract"]
    # Architizer / Archello will be added by Phase 9.5 when those DBs
    # are wired into _build_one. For now, only the matched 2-source pair.
    if desc_map:
        cb.description_per_source = desc_map

    if b.get("visual_description"):
        cb.set_field("visual_description", b["visual_description"], SOURCE_METALOCUS)

    for fname in ("program", "style", "color_tone", "atmosphere"):
        v = b.get(fname)
        if v and vocab.is_valid(fname, v):
            cb.set_field(fname, v, SOURCE_METALOCUS)

    if b.get("material_visual"):
        cb.set_field("material_visual", b["material_visual"], SOURCE_METALOCUS)

    img_paths = _building_image_paths(b)
    if img_paths:
        cb.set_field("image_paths", img_paths, SOURCE_METALOCUS)

    if b.get("embedding"):
        # Embedding doesn't carry provenance (not user-meaningful), but we
        # still need to store it.
        cb.embedding = b["embedding"]

    cb.vocab_version = vocab.VOCAB_VERSION

    return cb


def build_all(*, strict: bool = False, output_path: Optional[str] = None,
              enable_l4_haiku: bool = False,
              haiku_budget: Optional[int] = None) -> dict:
    """Build canonical_buildings.json (or _strict.json under --strict).

    strict=True:
      • drop pure orphans (no Divisare ID and no architect link)
      • drop arch_only rows whose name fails _is_article_title()
      • keep all full matches as-is
    """
    out_path = output_path or (STRICT_OUTPUT_PATH if strict else OUTPUT_PATH)

    print(f"loading metalocus buildings from {METALOC_PATH}...")
    metaloc = _load_json(METALOC_PATH)
    print(f"  {len(metaloc)} buildings")

    print(f"loading architect map from {ARCH_MATCH_PATH}...")
    arch_map = _load_arch_map()
    print(f"  {len(arch_map)} confident architect matches")

    print(f"loading building match map from {BLDG_MATCH_PATH}...")
    bldg_match, duplicate_bids = _load_building_match_map()
    print(f"  {len(bldg_match)} confident project matches "
          f"(after deduping {len(duplicate_bids)} metalocus dupes)")

    print(f"loading building→cluster index from {CLUSTERS_PATH}...")
    building_to_clusters = _load_json(CLUSTERS_PATH).get("building_to_canonical") or {}

    needed_div_ids = {m["divisare_id"] for m in bldg_match.values() if m.get("divisare_id")}
    print(f"loading {len(needed_div_ids)} Divisare project rows from {DIVISARE_DB}...")
    div_projects = _load_divisare_projects(needed_div_ids)

    print(f"\nbuilding canonical records (strict={strict})...")
    canonical_records: list[dict] = []
    matched_count = 0
    arch_only_kept = 0
    arch_only_dropped = 0
    pure_orphan_dropped = 0
    drop_reasons: dict[str, int] = {}
    dropped_examples: list[dict] = []

    for i, b in enumerate(metaloc, 1):
        bm = bldg_match.get(b["building_id"])
        cb = _build_one(
            b,
            arch_map=arch_map,
            building_match=bm,
            div_projects=div_projects,
            building_to_clusters=building_to_clusters,
        )

        if cb.divisare_id is not None:
            # Full match: cross-source confirmed → KEEP T1 always
            matched_count += 1
            d = cb.to_dict()
            if strict:
                d["confidence_tier"] = "T1"
            canonical_records.append(d)
        elif cb.architect_canonical_ids:
            # Arch-only: in strict mode, clean the name then run reality_filter
            if strict:
                from canonical.reality_filter import reality_filter
                original = cb.name or ""
                cleaned = _clean_building_name(original)
                if cleaned != original and cleaned:
                    cb.set_field("name", cleaned, SOURCE_DERIVED)
                # Build a transient dict mirroring what reality_filter expects
                rf_input = cb.to_dict()
                rf_input["name"] = cleaned
                # Feed metalocus building_type / metalocus tags from the source
                # 'b' (raw metalocus row) so L3 source-type signal can fire.
                if b.get("building_type"):
                    rf_input["building_type"] = b["building_type"]
                if b.get("tags"):
                    rf_input["tags"] = b["tags"]
                if b.get("year"):
                    rf_input["year"] = b["year"]
                if b.get("location_country"):
                    rf_input["country"] = b["location_country"]
                decision, tier, reason = reality_filter(
                    rf_input,
                    multi_source_confirmed=False,        # arch_only path
                    architect_verified=True,             # has architect_canonical_ids
                )
                # Resolve DEFER_L4: either via Haiku (if --enable-l4-haiku)
                # or default to DROP (conservative).
                if decision == "DEFER_L4":
                    if enable_l4_haiku:
                        from canonical.match_tiebreaker import (
                            classify_reality, HaikuCostCapHit,
                        )
                        try:
                            answer = classify_reality(
                                rf_input, default="no",
                                new_call_budget=haiku_budget,
                            )
                            if answer == "yes":
                                decision, tier, reason = "KEEP", "T3", "L4_haiku_yes"
                            else:
                                decision, tier, reason = "DROP", None, "L4_haiku_no"
                        except HaikuCostCapHit as e:
                            print(f"  ⚠ Haiku cost cap hit: {e}")
                            decision, tier, reason = "DROP", None, "L4_cost_cap"
                    else:
                        decision, tier, reason = "DROP", None, "L4_disabled_drop"
                if decision == "DROP":
                    arch_only_dropped += 1
                    drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
                    if len(dropped_examples) < 25:
                        dropped_examples.append({
                            "metalocus_building_id": cb.metalocus_building_id,
                            "name_original": original,
                            "name_after_cleanup": cleaned,
                            "reason": reason,
                            "program": cb.program,
                        })
                    continue
                arch_only_kept += 1
                d = cb.to_dict()
                d["confidence_tier"] = tier
                canonical_records.append(d)
            else:
                arch_only_kept += 1
                canonical_records.append(cb.to_dict())
        else:
            # Pure orphan: drop in strict mode, keep otherwise
            if strict:
                pure_orphan_dropped += 1
                continue
            canonical_records.append(cb.to_dict())

        if i % 500 == 0:
            print(f"  scanned {i}/{len(metaloc)}  "
                  f"(matched={matched_count}, arch_kept={arch_only_kept}, "
                  f"arch_dropped={arch_only_dropped}, orphan_dropped={pure_orphan_dropped})")

    print(f"\n=== summary (strict={strict}) ===")
    print(f"  scanned metalocus rows:                {len(metaloc)}")
    print(f"  → kept full match:                     {matched_count}")
    print(f"  → kept arch_only:                      {arch_only_kept}")
    if strict:
        print(f"  → dropped arch_only (article-style):   {arch_only_dropped}")
        for r, n in sorted(drop_reasons.items(), key=lambda t: -t[1]):
            print(f"        ・ {r}: {n}")
        print(f"  → dropped pure orphans (no Divisare):  {pure_orphan_dropped}")
    print(f"  TOTAL records in canonical:            {len(canonical_records)}")

    print(f"\nwriting → {out_path} ...")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(canonical_records, f, ensure_ascii=False)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"  wrote {len(canonical_records)} records ({size_mb:.1f} MB)")

    if strict and dropped_examples:
        print(f"\n=== sample of dropped arch_only (first {len(dropped_examples)}) ===")
        for ex in dropped_examples[:25]:
            shown = ex.get("name_after_cleanup") or ex.get("name_original") or ex.get("name") or ""
            print(f"  [{ex['reason']}] {shown!r}")

    return {"records": len(canonical_records), "matched": matched_count,
            "arch_only_kept": arch_only_kept,
            "arch_only_dropped": arch_only_dropped,
            "pure_orphan_dropped": pure_orphan_dropped,
            "drop_reasons": drop_reasons,
            "size_mb": round(size_mb, 1)}


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--strict", action="store_true",
                   help="drop pure orphans + arch_only rows whose name looks like "
                        "an article title (writes data/canonical/canonical_buildings_strict.json)")
    p.add_argument("--output", default=None,
                   help="override output path")
    p.add_argument("--enable-l4-haiku", action="store_true",
                   help="resolve reality_filter DEFER_L4 cases via Haiku classify "
                        "(requires ANTHROPIC_API_KEY; cost ~$0.0005/call, "
                        "cached at data/canonical/_haiku_cache.json)")
    p.add_argument("--haiku-budget", type=int, default=None,
                   help="cap on net-new Haiku calls per run (default: no cap)")
    args = p.parse_args(argv)
    build_all(strict=args.strict, output_path=args.output,
              enable_l4_haiku=args.enable_l4_haiku,
              haiku_budget=args.haiku_budget)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
