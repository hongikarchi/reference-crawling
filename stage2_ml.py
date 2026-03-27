#!/usr/bin/env python3
"""Stage 2: Deduplicate, assign IDs, and embed buildings.

Usage:
    python stage2_ml.py --pre-enrich
        Runs sub-steps 2a (preliminary embed) + 2b (dedup) + 2c (assign IDs).
        Input:  data/1_buildings_raw.json
        Output: data/2_buildings_to_enrich.json  ← hand off to Claude Code session
                data/id_registry.json            ← stable building IDs

    python stage2_ml.py --post-enrich
        Runs sub-step 2e (final 384-dim embed) after Claude enrichment session.
        Input:  data/3_buildings_enriched.json   ← written by Claude Code session
        Output: data/4_buildings_processed.json  ← final output for Stage 3
"""

import argparse
import json
import os
import shutil
import sys

import config

DATA_DIR = os.path.join(config.BASE_DIR, "data")
RAW_JSON = os.path.join(DATA_DIR, "1_buildings_raw.json")
TO_ENRICH_JSON = os.path.join(DATA_DIR, "2_buildings_to_enrich.json")
ENRICHED_JSON = os.path.join(DATA_DIR, "3_buildings_enriched.json")
PROCESSED_JSON = os.path.join(DATA_DIR, "4_buildings_processed.json")
REVIEW_JSON = os.path.join(DATA_DIR, "duplicates_review.json")
REGISTRY_JSON = os.path.join(DATA_DIR, "id_registry.json")

CONFIRMED_THRESHOLD = 0.92
REVIEW_THRESHOLD = 0.85

VALID_PROGRAMS = {
    "Housing", "Office", "Museum", "Education", "Religion", "Sports",
    "Transport", "Hospitality", "Healthcare", "Public",
    "Mixed Use", "Landscape", "Infrastructure", "Other",
}

# Substring remap for common near-matches from Claude enrichment
PROGRAM_REMAP = {
    "house": "Housing", "apartment": "Housing", "residential": "Housing",
    "villa": "Housing", "dwelling": "Housing",
    "office": "Office", "workspace": "Office", "corporate": "Office",
    "museum": "Museum", "gallery": "Museum", "exhibition": "Museum",
    "school": "Education", "university": "Education", "library": "Education",
    "church": "Religion", "mosque": "Religion", "temple": "Religion",
    "sport": "Sports", "stadium": "Sports", "arena": "Sports",
    "airport": "Transport", "station": "Transport", "terminal": "Transport",
    "hotel": "Hospitality", "restaurant": "Hospitality", "hospitality": "Hospitality",
    "hospital": "Healthcare", "clinic": "Healthcare", "medical": "Healthcare",
    "civic": "Public", "community": "Public", "public": "Public",
    "mixed": "Mixed Use",
    "landscape": "Landscape", "park": "Landscape", "garden": "Landscape",
    "bridge": "Infrastructure", "infrastructure": "Infrastructure", "pavilion": "Infrastructure",
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Sub-step 2a — Preliminary embeddings
# ---------------------------------------------------------------------------

def make_raw_embedding_text(b):
    parts = [
        b.get("project_name", ""),
        b.get("architect", "") or "",
        b.get("location_country", "") or "",
        b.get("building_type", "") or "",
        (b.get("description", "") or "")[:300],
    ]
    return " ".join(p for p in parts if p)


def generate_preliminary_embeddings(buildings, model):
    print(f"  Generating preliminary embeddings for {len(buildings)} buildings...")
    texts = [make_raw_embedding_text(b) for b in buildings]
    embeddings = model.encode(texts, batch_size=64, show_progress_bar=True)
    for b, emb in zip(buildings, embeddings):
        b["_prelim_embedding"] = emb
    return buildings


# ---------------------------------------------------------------------------
# Sub-step 2b — Deduplication
# ---------------------------------------------------------------------------

def cosine_similarity(a, b):
    import numpy as np
    a = a / (np.linalg.norm(a) + 1e-10)
    b = b / (np.linalg.norm(b) + 1e-10)
    return float(np.dot(a, b))


def name_similarity(a, b):
    from rapidfuzz import fuzz
    return fuzz.token_set_ratio(a.lower(), b.lower()) / 100.0


def find_duplicate_candidates(buildings):
    """Pass 1: fast fuzzy name match to build candidate pairs."""
    candidates = []
    for i, b1 in enumerate(buildings):
        for b2 in buildings[i + 1:]:
            name_sim = name_similarity(b1["project_name"], b2["project_name"])
            if name_sim <= 0.75:
                continue
            same_architect = (
                b1.get("architect") and b2.get("architect") and
                b1["architect"].strip().lower() == b2["architect"].strip().lower()
            )
            same_country = (
                b1.get("location_country") and b2.get("location_country") and
                b1["location_country"].strip().lower() == b2["location_country"].strip().lower()
            )
            if same_architect or same_country:
                candidates.append((b1["slug"], b2["slug"], name_sim))
    return candidates


def confirm_duplicates(candidates, buildings_by_slug):
    """Pass 2: confirm candidates using embedding cosine similarity."""
    confirmed = []
    review = []

    for slug_a, slug_b, name_sim in candidates:
        b1 = buildings_by_slug[slug_a]
        b2 = buildings_by_slug[slug_b]
        cos_sim = cosine_similarity(b1["_prelim_embedding"], b2["_prelim_embedding"])

        if cos_sim >= CONFIRMED_THRESHOLD:
            confirmed.append((slug_a, slug_b, name_sim, cos_sim))
        elif cos_sim >= REVIEW_THRESHOLD:
            review.append({
                "confidence": round(cos_sim, 4),
                "name_similarity": round(name_sim, 4),
                "building_a": {"slug": slug_a, "project_name": b1["project_name"]},
                "building_b": {"slug": slug_b, "project_name": b2["project_name"]},
                "action": "keep",  # edit to "merge" and re-run to apply
            })

    return confirmed, review


def merge_buildings(winner, duplicate):
    """Merge duplicate into winner, keeping richer data."""
    merged = dict(winner)
    for field in ["architect", "location_country", "city", "year", "area_sqm",
                  "building_type", "mood", "material", "description", "url"]:
        if merged.get(field) is None and duplicate.get(field) is not None:
            merged[field] = duplicate[field]

    merged["tags"] = list(set((winner.get("tags") or []) + (duplicate.get("tags") or [])))

    winner_filenames = {img["filename"] for img in winner.get("images", [])}
    extra_images = [
        img for img in duplicate.get("images", [])
        if img["filename"] not in winner_filenames
    ]
    merged["images"] = list(winner.get("images", [])) + extra_images
    for i, img in enumerate(merged["images"]):
        img["order"] = i

    merged["source_slugs"] = list(set(
        winner.get("source_slugs", [winner["slug"]]) +
        duplicate.get("source_slugs", [duplicate["slug"]])
    ))
    return merged


def apply_deduplication(buildings, existing_review=None):
    """Run deduplication. Returns (deduplicated_buildings, review_pairs)."""
    buildings_by_slug = {b["slug"]: b for b in buildings}

    # Apply manual merge decisions from a previous review file
    manual_merges = []
    if existing_review:
        for entry in existing_review:
            if entry.get("action") == "merge":
                manual_merges.append((entry["building_a"]["slug"], entry["building_b"]["slug"]))

    print(f"  Pass 1: finding fuzzy name candidates...")
    candidates = find_duplicate_candidates(buildings)
    print(f"  Found {len(candidates)} candidate pairs")

    print(f"  Pass 2: confirming with cosine similarity...")
    confirmed, review_pairs = confirm_duplicates(candidates, buildings_by_slug)

    # Add manual merges (from review file) to confirmed list
    for slug_a, slug_b in manual_merges:
        if slug_a in buildings_by_slug and slug_b in buildings_by_slug:
            already = any(
                (a == slug_a and b == slug_b) or (a == slug_b and b == slug_a)
                for a, b, *_ in confirmed
            )
            if not already:
                confirmed.append((slug_a, slug_b, 0.0, 1.0))

    print(f"  Confirmed duplicates: {len(confirmed)}, Review pairs: {len(review_pairs)}")

    # Build merge groups (union-find)
    parent = {b["slug"]: b["slug"] for b in buildings}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(y)] = find(x)

    for slug_a, slug_b, *_ in confirmed:
        if slug_a in parent and slug_b in parent:
            union(slug_a, slug_b)

    # Group buildings by their root
    groups = {}
    for b in buildings:
        root = find(b["slug"])
        groups.setdefault(root, []).append(b)

    # Merge each group
    result = []
    for root, group in groups.items():
        if len(group) == 1:
            b = dict(group[0])
            if "source_slugs" not in b:
                b["source_slugs"] = [b["slug"]]
            result.append(b)
        else:
            # Pick winner = record with most non-null fields
            def richness(b):
                return sum(1 for v in b.values() if v is not None and v != "" and v != [])
            group_sorted = sorted(group, key=richness, reverse=True)
            merged = group_sorted[0]
            if "source_slugs" not in merged:
                merged["source_slugs"] = [merged["slug"]]
            for dup in group_sorted[1:]:
                merged = merge_buildings(merged, dup)
            result.append(merged)

    return result, review_pairs


# ---------------------------------------------------------------------------
# Sub-step 2c — Assign stable building IDs
# ---------------------------------------------------------------------------

def assign_building_ids(buildings):
    registry = load_json(REGISTRY_JSON) if os.path.exists(REGISTRY_JSON) else {}
    next_id = max((int(v[1:]) for v in registry.values()), default=0) + 1

    for building in buildings:
        all_slugs = building.get("source_slugs", [building["slug"]])
        existing_id = next(
            (registry[s] for s in all_slugs if s in registry), None
        )
        if existing_id is None:
            existing_id = f"B{next_id:05d}"
            next_id += 1
        for s in all_slugs:
            registry[s] = existing_id
        building["building_id"] = existing_id

    save_json(REGISTRY_JSON, registry)
    return buildings


# ---------------------------------------------------------------------------
# Image reorganization: images/{slug}/ → images/{building_id}/
# ---------------------------------------------------------------------------

def reorganize_images(buildings):
    """Move image directories from slug-based to building_id-based layout."""
    for building in buildings:
        building_id = building["building_id"]
        dest_dir = os.path.join(config.IMAGE_BASE_DIR, building_id)

        # Collect all source slug dirs
        source_slugs = building.get("source_slugs", [building["slug"]])
        if os.path.exists(dest_dir):
            continue  # already reorganized

        os.makedirs(dest_dir, exist_ok=True)

        for slug in source_slugs:
            src_dir = os.path.join(config.IMAGE_BASE_DIR, slug)
            if not os.path.exists(src_dir):
                continue
            for fname in os.listdir(src_dir):
                src_file = os.path.join(src_dir, fname)
                dst_file = os.path.join(dest_dir, fname)
                if not os.path.exists(dst_file):
                    shutil.move(src_file, dst_file)
            # Remove empty slug dir
            try:
                os.rmdir(src_dir)
            except OSError:
                pass  # not empty — skip


# ---------------------------------------------------------------------------
# Sub-step 2e — Final 384-dim embeddings
# ---------------------------------------------------------------------------

def make_embedding_text(b):
    parts = [
        b.get("name_en", b.get("project_name", "")),
        b.get("architect", "") or "",
        b.get("location_country", "") or "",
        b.get("program", "") or "",
        b.get("mood", "") or "",
        b.get("material", "") or "",
        (b.get("description", "") or "")[:500],
    ]
    return " ".join(p for p in parts if p)


def generate_final_embeddings(buildings, model):
    print(f"  Generating final embeddings for {len(buildings)} buildings...")
    texts = [make_embedding_text(b) for b in buildings]
    embeddings = model.encode(texts, batch_size=64, show_progress_bar=True)
    for building, emb in zip(buildings, embeddings):
        building["embedding"] = emb.tolist()
    return buildings


# ---------------------------------------------------------------------------
# Program normalization
# ---------------------------------------------------------------------------

def normalize_program(value):
    if value in VALID_PROGRAMS:
        return value
    if value is None:
        return "Other"
    lower = value.lower().strip()
    for keyword, mapped in PROGRAM_REMAP.items():
        if keyword in lower:
            return mapped
    return "Other"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_pre_enrich():
    if not os.path.exists(RAW_JSON):
        print(f"ERROR: {RAW_JSON} not found. Run stage1_crawl.py --export first.")
        sys.exit(1)

    print("Loading buildings_raw.json...")
    buildings = load_json(RAW_JSON)
    print(f"  {len(buildings)} buildings loaded")

    # Load existing review file for manual merge decisions
    existing_review = load_json(REVIEW_JSON) if os.path.exists(REVIEW_JSON) else None

    # Sub-step 2a: preliminary embeddings
    print("\nSub-step 2a: preliminary embeddings...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    buildings = generate_preliminary_embeddings(buildings, model)

    # Sub-step 2b: deduplication
    print("\nSub-step 2b: deduplication...")
    buildings, review_pairs = apply_deduplication(buildings, existing_review)
    print(f"  After dedup: {len(buildings)} unique buildings")

    # Clean up temp embeddings before writing
    for b in buildings:
        b.pop("_prelim_embedding", None)

    if review_pairs:
        save_json(REVIEW_JSON, review_pairs)
        print(f"\n  {len(review_pairs)} uncertain pairs written to {REVIEW_JSON}")
        print("  Review that file, set 'action': 'merge' where appropriate, then re-run --pre-enrich.")

    # Sub-step 2c: assign stable building IDs
    print("\nSub-step 2c: assigning building IDs...")
    buildings = assign_building_ids(buildings)

    # Reorganize images to building_id directories
    print("  Reorganizing images to building_id directories...")
    reorganize_images(buildings)

    # Prepare output: add name_en and program as null for Claude to fill
    for b in buildings:
        b.setdefault("name_en", None)
        b.setdefault("program", None)

    save_json(TO_ENRICH_JSON, buildings)

    print(f"\nPre-enrich complete:")
    print(f"  Buildings: {len(buildings)}")
    print(f"  Output: {TO_ENRICH_JSON}")
    print(f"  Registry: {REGISTRY_JSON}")
    if review_pairs:
        print(f"  Review: {REVIEW_JSON}  ← edit and re-run if needed")
    print()
    print("Next step: run the Claude Code enrichment session.")
    print("  Prompt: Read data/2_buildings_to_enrich.json. Fill null fields")
    print("  (name_en, mood, material, program) and write data/3_buildings_enriched.json.")


def cmd_post_enrich():
    if not os.path.exists(ENRICHED_JSON):
        print(f"ERROR: {ENRICHED_JSON} not found.")
        print("Run the Claude Code enrichment session first, then re-run --post-enrich.")
        sys.exit(1)

    print("Loading 3_buildings_enriched.json...")
    buildings = load_json(ENRICHED_JSON)
    print(f"  {len(buildings)} buildings loaded")

    # Validate required fields
    missing = []
    for b in buildings:
        for field in ["building_id", "name_en", "mood", "material", "program"]:
            if not b.get(field):
                missing.append(f"  {b.get('slug', '?')}: missing {field}")
    if missing:
        print(f"\nWARNING: {len(missing)} missing required fields:")
        for m in missing[:10]:
            print(m)
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")

    # Normalize program values
    print("\nNormalizing program values...")
    remapped = 0
    for b in buildings:
        original = b.get("program")
        normalized = normalize_program(original)
        if normalized != original:
            remapped += 1
        b["program"] = normalized
    print(f"  Remapped {remapped} program values")

    # Sub-step 2e: final embeddings
    print("\nSub-step 2e: final 384-dim embeddings...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    buildings = generate_final_embeddings(buildings, model)

    # Ensure output matches Section 4.2 schema (drop internal fields)
    output = []
    for b in buildings:
        record = {
            "building_id": b["building_id"],
            "slug": b["slug"],
            "name_en": b.get("name_en"),
            "project_name": b.get("project_name"),
            "source_slugs": b.get("source_slugs", [b["slug"]]),
            "architect": b.get("architect"),
            "location_country": b.get("location_country"),
            "city": b.get("city"),
            "year": b.get("year"),
            "area_sqm": b.get("area_sqm"),
            "program": b.get("program", "Other"),
            "mood": b.get("mood"),
            "material": b.get("material"),
            "description": b.get("description"),
            "url": b.get("url"),
            "images": b.get("images", []),
            "tags": b.get("tags", []),
            "embedding": b.get("embedding"),
        }
        output.append(record)

    save_json(PROCESSED_JSON, output)

    print(f"\nPost-enrich complete:")
    print(f"  Buildings: {len(output)}")
    print(f"  Output: {PROCESSED_JSON}")
    programs = {}
    for b in output:
        programs[b["program"]] = programs.get(b["program"], 0) + 1
    print(f"  Program distribution: {dict(sorted(programs.items()))}")
    no_embed = sum(1 for b in output if not b.get("embedding"))
    print(f"  Missing embeddings: {no_embed}")


def main():
    parser = argparse.ArgumentParser(description="Stage 2: ML dedup, enrich, and embed")
    parser.add_argument("--pre-enrich", action="store_true",
                        help="Run 2a+2b+2c: dedup + assign IDs → 2_buildings_to_enrich.json")
    parser.add_argument("--post-enrich", action="store_true",
                        help="Run 2e: validate + embed → 4_buildings_processed.json")
    args = parser.parse_args()

    if not args.pre_enrich and not args.post_enrich:
        parser.print_help()
        sys.exit(1)

    if args.pre_enrich:
        cmd_pre_enrich()

    if args.post_enrich:
        cmd_post_enrich()


if __name__ == "__main__":
    main()
