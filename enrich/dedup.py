#!/usr/bin/env python3
"""Stage 2: Deduplicate buildings and assign stable IDs.

Usage:
    python3 stage2_dedup.py

Input:  data/1_buildings_raw.json
Output: data/1_buildings_raw.json  (updated with building_id + source_slugs)
        data/id_registry.json      (stable slug → building_id map)
        images/{building_id}/      (reorganized from images/{slug}/)

Runs:
  - Preliminary embedding (throwaway, for dedup only)
  - Fuzzy name match + cosine confirm → merge duplicates
  - Assign stable building_id (B00001 format)
  - Move images/{slug}/ → images/{building_id}/
"""

import json
import os
import shutil
import sys

from core import config


CONFIRMED_THRESHOLD = 0.92
REVIEW_THRESHOLD    = 0.85


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Preliminary embeddings (throwaway — used only for dedup confirmation)
# ---------------------------------------------------------------------------

def _make_raw_text(b):
    parts = [
        b.get("project_name", ""),
        b.get("architect", "") or "",
        b.get("location_country", "") or "",
        b.get("building_type", "") or "",
        (b.get("description", "") or "")[:300],
    ]
    return " ".join(p for p in parts if p)


def _generate_preliminary_embeddings(buildings, model):
    print(f"  Generating preliminary embeddings for {len(buildings)} buildings...")
    texts = [_make_raw_text(b) for b in buildings]
    embeddings = model.encode(texts, batch_size=64, show_progress_bar=True)
    for b, emb in zip(buildings, embeddings):
        b["_prelim_embedding"] = emb
    return buildings


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _cosine_similarity(a, b):
    import numpy as np
    a = a / (np.linalg.norm(a) + 1e-10)
    b = b / (np.linalg.norm(b) + 1e-10)
    return float(np.dot(a, b))


def _name_similarity(a, b):
    from rapidfuzz import fuzz
    return fuzz.token_set_ratio(a.lower(), b.lower()) / 100.0


def _find_candidates(buildings):
    candidates = []
    for i, b1 in enumerate(buildings):
        for b2 in buildings[i + 1:]:
            name_sim = _name_similarity(b1["project_name"], b2["project_name"])
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


def _confirm_duplicates(candidates, by_slug):
    confirmed, review = [], []
    for slug_a, slug_b, name_sim in candidates:
        cos = _cosine_similarity(
            by_slug[slug_a]["_prelim_embedding"],
            by_slug[slug_b]["_prelim_embedding"],
        )
        if cos >= CONFIRMED_THRESHOLD:
            confirmed.append((slug_a, slug_b, name_sim, cos))
        elif cos >= REVIEW_THRESHOLD:
            review.append({
                "confidence": round(cos, 4),
                "name_similarity": round(name_sim, 4),
                "building_a": {"slug": slug_a, "project_name": by_slug[slug_a]["project_name"]},
                "building_b": {"slug": slug_b, "project_name": by_slug[slug_b]["project_name"]},
                "action": "keep",
            })
    return confirmed, review


def _merge(winner, dup):
    merged = dict(winner)
    for field in ["architect", "location_country", "city", "year", "area_sqm",
                  "building_type", "material", "description", "url"]:
        if merged.get(field) is None and dup.get(field) is not None:
            merged[field] = dup[field]
    merged["tags"] = list(set((winner.get("tags") or []) + (dup.get("tags") or [])))
    winner_files = {i["filename"] for i in winner.get("images", [])}
    merged["images"] = list(winner.get("images", [])) + [
        i for i in dup.get("images", []) if i["filename"] not in winner_files
    ]
    for idx, img in enumerate(merged["images"]):
        img["order"] = idx
    merged["source_slugs"] = list(set(
        winner.get("source_slugs", [winner["slug"]]) +
        dup.get("source_slugs", [dup["slug"]])
    ))
    return merged


def _richness(b):
    count = 0
    for v in b.values():
        if v is None:
            continue
        if isinstance(v, str) and v == "":
            continue
        if isinstance(v, list) and len(v) == 0:
            continue
        count += 1
    return count


def run_deduplication(buildings, existing_review=None):
    by_slug = {b["slug"]: b for b in buildings}

    manual_merges = []
    if existing_review:
        for entry in existing_review:
            if entry.get("action") == "merge":
                manual_merges.append((entry["building_a"]["slug"], entry["building_b"]["slug"]))

    print("  Pass 1: fuzzy name candidates...")
    candidates = _find_candidates(buildings)
    print(f"  Found {len(candidates)} candidate pairs")

    print("  Pass 2: cosine confirmation...")
    confirmed, review_pairs = _confirm_duplicates(candidates, by_slug)

    for slug_a, slug_b in manual_merges:
        if slug_a in by_slug and slug_b in by_slug:
            if not any((a == slug_a and b == slug_b) or (a == slug_b and b == slug_a)
                       for a, b, *_ in confirmed):
                confirmed.append((slug_a, slug_b, 0.0, 1.0))

    print(f"  Confirmed: {len(confirmed)}, Review needed: {len(review_pairs)}")

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

    groups = {}
    for b in buildings:
        groups.setdefault(find(b["slug"]), []).append(b)

    result = []
    for group in groups.values():
        if len(group) == 1:
            b = dict(group[0])
            b.setdefault("source_slugs", [b["slug"]])
            result.append(b)
        else:
            group_sorted = sorted(group, key=_richness, reverse=True)
            merged = dict(group_sorted[0])
            merged.setdefault("source_slugs", [merged["slug"]])
            for dup in group_sorted[1:]:
                merged = _merge(merged, dup)
            result.append(merged)

    return result, review_pairs


# ---------------------------------------------------------------------------
# ID assignment
# ---------------------------------------------------------------------------

def assign_building_ids(buildings):
    registry = load_json(config.REGISTRY_JSON) if os.path.exists(config.REGISTRY_JSON) else {}
    next_id = max((int(v[1:]) for v in registry.values()), default=0) + 1

    for b in buildings:
        all_slugs = b.get("source_slugs", [b["slug"]])
        existing = next((registry[s] for s in all_slugs if s in registry), None)
        if existing is None:
            existing = f"B{next_id:05d}"
            next_id += 1
        for s in all_slugs:
            registry[s] = existing
        b["building_id"] = existing

    save_json(config.REGISTRY_JSON, registry)
    return buildings


# ---------------------------------------------------------------------------
# Image reorganization: images/{slug}/ → images/{building_id}/
# ---------------------------------------------------------------------------

def reorganize_images(buildings):
    for b in buildings:
        building_id = b["building_id"]
        dest = os.path.join(config.IMAGE_BASE_DIR, building_id)
        if os.path.exists(dest):
            continue
        os.makedirs(dest, exist_ok=True)
        for slug in b.get("source_slugs", [b["slug"]]):
            src = os.path.join(config.IMAGE_BASE_DIR, slug)
            if not os.path.exists(src):
                continue
            for fname in os.listdir(src):
                dst_file = os.path.join(dest, fname)
                if not os.path.exists(dst_file):
                    shutil.move(os.path.join(src, fname), dst_file)
            try:
                os.rmdir(src)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(config.RAW_JSON):
        print(f"ERROR: {config.RAW_JSON} not found. Run stage1_export.py first.")
        sys.exit(1)

    print("Loading 1_buildings_raw.json...")
    buildings = load_json(config.RAW_JSON)
    print(f"  {len(buildings)} buildings")

    review_path = os.path.join(config.DATA_DIR, "duplicates_review.json")
    existing_review = load_json(review_path) if os.path.exists(review_path) else None

    print("\nGenerating preliminary embeddings...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    buildings = _generate_preliminary_embeddings(buildings, model)

    print("\nDeduplicating...")
    buildings, review_pairs = run_deduplication(buildings, existing_review)
    print(f"  After dedup: {len(buildings)} unique buildings")

    for b in buildings:
        b.pop("_prelim_embedding", None)

    if review_pairs:
        save_json(review_path, review_pairs)
        print(f"\n  {len(review_pairs)} uncertain pairs → {review_path}")
        print("  Set 'action': 'merge' for any matches, then re-run.")

    print("\nAssigning building IDs...")
    buildings = assign_building_ids(buildings)

    print("Reorganizing images to building_id directories...")
    reorganize_images(buildings)

    save_json(config.RAW_JSON, buildings)

    print(f"\nDedup complete:")
    print(f"  Buildings: {len(buildings)}")
    print(f"  Output: {config.RAW_JSON}  (updated with building_id)")
    print(f"  Registry: {config.REGISTRY_JSON}")
    print()
    print("Next: run Claude text enrichment session.")
    print("  Read 1_buildings_raw.json → fill name_en, program, material, atmosphere")
    print("  → write 2_buildings_enriched.json")


if __name__ == "__main__":
    main()
