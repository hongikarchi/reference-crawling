#!/usr/bin/env python3
"""Stage 3: Generate 384-dim embeddings from enriched + analyzed buildings.

Usage:
    python3 stage3_embed.py

Input:  data/3_buildings_analyzed.json
Output: data/4_buildings_final.json

Validates required fields, normalizes program/style/color_tone vocabularies,
then encodes each building into a 384-dim vector using all enriched fields.
"""

import json
import os
import sys

import config
import vocab


# ---------------------------------------------------------------------------
# Embedding text
# ---------------------------------------------------------------------------

def make_embedding_text(b):
    """Build the text that gets encoded into the 384-dim vector.

    Includes all enriched fields — text AND visual — so the vector
    captures visual character, not just programmatic similarity.
    """
    material_visual = " ".join(b.get("material_visual") or [])
    parts = [
        b.get("name_en") or b.get("project_name", ""),
        b.get("architect", "") or "",
        b.get("location_country", "") or "",
        b.get("program", "") or "",
        b.get("style", "") or "",
        b.get("atmosphere", "") or "",
        b.get("color_tone", "") or "",
        material_visual,
        b.get("visual_description", "") or "",
        (b.get("description", "") or "")[:500],
    ]
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(config.ANALYZED_JSON):
        print(f"ERROR: {config.ANALYZED_JSON} not found.")
        print("Run Claude image analysis first → write 3_buildings_analyzed.json")
        sys.exit(1)

    print("Loading 3_buildings_analyzed.json...")
    with open(config.ANALYZED_JSON, encoding="utf-8") as f:
        buildings = json.load(f)
    print(f"  {len(buildings)} buildings")

    # Validate required fields
    missing = []
    for b in buildings:
        for field in ["building_id", "name_en", "program"]:
            if not b.get(field):
                missing.append(f"  {b.get('slug', '?')}: missing {field}")
    if missing:
        print(f"\nWARNING: {len(missing)} missing required fields:")
        for m in missing[:10]:
            print(m)
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")

    # Normalize vocabularies. Program uses loose matching (raw building_type
    # values may leak through) and falls back to "Other"; style/color_tone
    # are strict — only explicit legacy migrations, no fuzzy aliases.
    remapped = {"program": 0, "style": 0, "color_tone": 0}
    audit = []
    vocab.set_audit_sink(audit)
    for b in buildings:
        orig_program = b.get("program")
        fixed_program, _ = vocab.normalize_loose("program", orig_program)
        if orig_program and not vocab.is_valid("program", fixed_program):
            fixed_program = "Other"
        b["program"] = fixed_program or "Other"
        if b["program"] != orig_program:
            remapped["program"] += 1

        orig_style = b.get("style")
        fixed_style, _ = vocab.migrate_strict("style", orig_style)
        b["style"] = fixed_style
        if b["style"] != orig_style:
            remapped["style"] += 1

        orig_tone = b.get("color_tone")
        fixed_tone, _ = vocab.migrate_strict("color_tone", orig_tone)
        b["color_tone"] = fixed_tone
        if b["color_tone"] != orig_tone:
            remapped["color_tone"] += 1
    vocab.set_audit_sink(None)

    print(f"  Normalized: program={remapped['program']}, "
          f"style={remapped['style']}, color_tone={remapped['color_tone']}")
    if audit:
        print(f"  Vocab rewrites logged: {len(audit)} events")

    # Generate embeddings
    print("\nGenerating 384-dim embeddings...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    texts = [make_embedding_text(b) for b in buildings]
    embeddings = model.encode(texts, batch_size=64, show_progress_bar=True)
    for b, emb in zip(buildings, embeddings):
        b["embedding"] = emb.tolist()

    # Build final output — strict field order matching schema
    output = []
    for b in buildings:
        output.append({
            "building_id":        b["building_id"],
            "slug":               b["slug"],
            "name_en":            b.get("name_en"),
            "project_name":       b.get("project_name"),
            "source_slugs":       b.get("source_slugs", [b["slug"]]),
            "architect":          b.get("architect"),
            "location_country":   b.get("location_country"),
            "city":               b.get("city"),
            "year":               b.get("year"),
            "area_sqm":           b.get("area_sqm"),
            "program":            b.get("program", "Other"),
            "style":              b.get("style"),
            "atmosphere":         b.get("atmosphere"),
            "color_tone":         b.get("color_tone"),
            "material":           b.get("material"),
            "material_visual":    b.get("material_visual") or [],
            "description":        b.get("description"),
            "visual_description": b.get("visual_description"),
            "url":                b.get("url"),
            "images":             b.get("images", []),
            "tags":               b.get("tags", []),
            "vocab_version":      b.get("vocab_version") or vocab.VOCAB_VERSION,
            "prompt_version":     b.get("prompt_version"),
            "embedding":          b.get("embedding"),
        })

    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.FINAL_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    no_embed = sum(1 for b in output if not b.get("embedding"))
    programs = {}
    for b in output:
        programs[b["program"]] = programs.get(b["program"], 0) + 1

    print(f"\nEmbed complete:")
    print(f"  Buildings:          {len(output)}")
    print(f"  Missing embeddings: {no_embed}")
    print(f"  Program distribution: {dict(sorted(programs.items()))}")
    print(f"  Output: {config.FINAL_JSON}")
    print()
    print("Next: run review.py → fix.py → rate.py")


if __name__ == "__main__":
    main()
