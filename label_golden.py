#!/usr/bin/env python3
"""Bootstrap a golden set for eval.py.

Samples buildings from the current production data and writes golden-format
entries to `data/golden/buildings.json`. Eval (`eval.py`) then scores the live
Sonnet prompts against these golden labels.

Two sourcing modes:

  --source current   (fast, no API)
    Uses each building's current values as its own "golden." This produces a
    regression-guard baseline — good for catching silent prompt drift, bad
    for detecting that the current prompts have room to improve.

  --source opus      (labels with a stronger model)
    Calls claude-opus-4-7 on the same raw / enriched inputs to generate
    higher-quality labels. The user should spot-check ~10 of these before
    trusting the eval delta — Opus can still be wrong.

Usage:
    python3 label_golden.py --sample 30                          # default: current
    python3 label_golden.py --sample 30 --source opus            # Opus labeling
    python3 label_golden.py --sample 30 --source opus --only enrich

Output:
    data/golden/buildings.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

import config
import vocab

GOLDEN_PATH = os.path.join(config.DATA_DIR, "golden", "buildings.json")


def _normalize_field(field: str, value):
    """Pass through vocab.migrate_strict so `--source current` doesn't import
    V1 freeform values (e.g., 'urban and transformative' atmospheres) into
    the golden set. Returns None if the value can't be resolved to V2."""
    if value is None:
        return None
    fixed, _ = vocab.migrate_strict(field, value)
    return fixed


def _stratified_sample(buildings: list, n: int, stratify_by: str = "program") -> list:
    """Sample n buildings spread across values of stratify_by (program by default)."""
    groups: dict = {}
    for b in buildings:
        k = b.get(stratify_by) or "Unknown"
        groups.setdefault(k, []).append(b)

    if not groups:
        return random.sample(buildings, min(n, len(buildings)))

    per_group = max(1, n // len(groups))
    sample: list = []
    for group in groups.values():
        sample.extend(random.sample(group, min(per_group, len(group))))
    random.shuffle(sample)
    # Top up from remaining if underfilled (small groups capped at per_group).
    if len(sample) < n:
        sampled_ids = {b["building_id"] for b in sample}
        pool = [b for b in buildings if b["building_id"] not in sampled_ids]
        random.shuffle(pool)
        sample.extend(pool[: n - len(sample)])
    return sample[:n]


def _entry_from_current(building_id: str, raw_by_id: dict,
                        enriched_by_id: dict, final_by_id: dict) -> dict | None:
    raw = raw_by_id.get(building_id)
    enriched = enriched_by_id.get(building_id)
    final = final_by_id.get(building_id)
    if not raw or not enriched or not final:
        return None
    return {
        "building_id": building_id,
        "raw": raw,
        "enriched": enriched,
        "golden_enrich": {
            "name_en":    final.get("name_en"),
            "program":    _normalize_field("program", final.get("program")),
            "material":   final.get("material"),
            "atmosphere": _normalize_field("atmosphere", final.get("atmosphere")),
        },
        "golden_analyze": {
            "style":              _normalize_field("style", final.get("style")),
            "color_tone":         _normalize_field("color_tone", final.get("color_tone")),
            "material_visual":    final.get("material_visual") or [],
            "visual_description": final.get("visual_description"),
            "atmosphere":         _normalize_field("atmosphere", final.get("atmosphere")),
        },
        "_provenance": {"source": "current"},
    }


def _entry_from_opus(building_id: str, raw_by_id: dict, enriched_by_id: dict,
                     only_enrich: bool = False, only_analyze: bool = False) -> dict | None:
    """Label with Opus by monkey-patching HARNESS_MODEL for the call duration.

    agent_{llm_parser,image_analysis} read config.HARNESS_MODEL at call time,
    so swapping it in place is enough — no module surgery needed.
    """
    raw = raw_by_id.get(building_id)
    enriched = enriched_by_id.get(building_id)
    if not raw or not enriched:
        return None

    from agent_llm_parser import enrich_building
    from agent_image_analysis import analyze_building

    original_model = config.HARNESS_MODEL
    config.HARNESS_MODEL = "claude-opus-4-7"
    try:
        golden_enrich = None
        golden_analyze = None
        if not only_analyze:
            er = enrich_building(raw)
            golden_enrich = {
                "name_en":    er.name_en,
                "program":    er.program,
                "material":   er.material,
                "atmosphere": er.atmosphere,
            }
        if not only_enrich:
            ar = analyze_building(enriched)
            golden_analyze = {
                "style":              ar.style,
                "color_tone":         ar.color_tone,
                "material_visual":    ar.material_visual,
                "visual_description": ar.visual_description,
                "atmosphere":         ar.atmosphere,
            }
    finally:
        config.HARNESS_MODEL = original_model

    entry = {
        "building_id": building_id,
        "raw": raw,
        "enriched": enriched,
        "_provenance": {"source": "opus", "model": "claude-opus-4-7"},
    }
    if golden_enrich is not None:
        entry["golden_enrich"] = golden_enrich
    if golden_analyze is not None:
        entry["golden_analyze"] = golden_analyze
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the eval golden set.")
    parser.add_argument("--sample", type=int, default=30,
                        help="Number of buildings to sample (default 30).")
    parser.add_argument("--source", choices=["current", "opus"], default="current",
                        help="Use current values as golden (fast) or label with Opus (ceiling signal).")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducible sampling.")
    parser.add_argument("--only", choices=["enrich", "analyze"], default=None,
                        help="Only label one of the two tasks (opus source only).")
    parser.add_argument("--append", action="store_true",
                        help="Extend the existing golden set instead of overwriting.")
    args = parser.parse_args()

    random.seed(args.seed)

    if not os.path.exists(config.FINAL_JSON):
        print(f"ERROR: {config.FINAL_JSON} missing — run the pipeline first.")
        return 1

    with open(config.FINAL_JSON, encoding="utf-8") as f:
        final = json.load(f)
    with open(config.RAW_JSON, encoding="utf-8") as f:
        raw_by_id = {b["building_id"]: b for b in json.load(f)}
    with open(config.ENRICHED_JSON, encoding="utf-8") as f:
        enriched_by_id = {b["building_id"]: b for b in json.load(f)}
    final_by_id = {b["building_id"]: b for b in final}

    sample = _stratified_sample(final, args.sample)
    print(f"Sampled {len(sample)} buildings (stratified by program).")

    existing: list = []
    if args.append and os.path.exists(GOLDEN_PATH):
        with open(GOLDEN_PATH, encoding="utf-8") as f:
            existing = json.load(f)
        existing_ids = {e["building_id"] for e in existing}
        sample = [b for b in sample if b["building_id"] not in existing_ids]
        print(f"  --append: skipping {len(existing)} already-labeled buildings.")

    entries: list = []
    if args.source == "current":
        for b in sample:
            entry = _entry_from_current(b["building_id"], raw_by_id, enriched_by_id, final_by_id)
            if entry:
                entries.append(entry)
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ERROR: opus source requires ANTHROPIC_API_KEY in .env")
            return 1
        only_enrich = args.only == "enrich"
        only_analyze = args.only == "analyze"
        for i, b in enumerate(sample, 1):
            bid = b["building_id"]
            print(f"  [{i}/{len(sample)}] labeling {bid} with Opus...")
            try:
                entry = _entry_from_opus(bid, raw_by_id, enriched_by_id,
                                         only_enrich=only_enrich, only_analyze=only_analyze)
                if entry:
                    entries.append(entry)
            except Exception as e:
                print(f"    [warn] {type(e).__name__}: {e}")

    os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
    final_list = existing + entries if args.append else entries
    with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)

    print(f"\nWrote {len(final_list)} entries → {GOLDEN_PATH}")
    print(f"  New: {len(entries)}")
    print(f"  Source: {args.source}")

    if args.source == "current":
        atm_none = sum(
            1 for e in final_list
            if e.get("golden_enrich", {}).get("atmosphere") is None
            or e.get("golden_analyze", {}).get("atmosphere") is None
        )
        if atm_none:
            print(f"\n  ⚠  {atm_none}/{len(final_list)} entries have atmosphere=None because")
            print(f"     the current production value wasn't in V2 vocab. `eval.py` will")
            print(f"     therefore underreport atmosphere accuracy against this baseline.")
            print(f"     For atmosphere quality, re-run with `--source opus` or hand-label.")

    print(f"\nNext: spot-check 10–20 entries, correct anything the labeler got wrong,")
    print(f"      then run `python3 eval.py` to score the current prompts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
