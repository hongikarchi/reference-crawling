#!/usr/bin/env python3
"""Eval harness: score the current prompts against a hand-labeled golden set.

Loads `data/golden/buildings.json`, runs the live enrich/analyze prompts on each
entry, and compares output to the golden labels. Writes a numeric report so
prompt changes become measurable rather than vibes.

Usage:
    python3 eval.py                          # full eval on the golden set
    python3 eval.py --limit 5                # try the first 5 only
    python3 eval.py --only enrich            # skip analyze
    python3 eval.py --only analyze           # skip enrich
    python3 eval.py --label my-test-run      # tag the report with a custom label

Input:  data/golden/buildings.json
        Each entry: {
            "building_id", "raw": {...}, "enriched": {...},
            "golden_enrich": {name_en, program, material, atmosphere},
            "golden_analyze": {style, color_tone, material_visual,
                               visual_description, atmosphere}
        }

Output: data/reports/eval_report.json
        {
            "prompt_versions": {...},
            "vocab_version": "v2",
            "enrich": {program_accuracy, atmosphere_accuracy,
                       name_en_similarity, material_overlap, n},
            "analyze": {style_accuracy, color_tone_accuracy,
                        atmosphere_accuracy, visual_description_similarity,
                        material_visual_jaccard, n},
            "per_building": [{building_id, enrich_score, analyze_score, ...}]
        }

Scoring:
  - enum fields (program, style, color_tone, atmosphere): exact match → 0/1
  - free-form strings (name_en, visual_description): cosine similarity via
    the repo's sentence-transformers model (same one used for embeddings)
  - list fields (material_visual): Jaccard index over lowercased items
  - free-form `material`: substring match either way → 1, else 0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from core import config
from core import vocab

GOLDEN_PATH = os.path.join(config.DATA_DIR, "golden", "buildings.json")
EVAL_REPORT_PATH = os.path.join(config.REPORTS_DIR, "eval_report.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cosine_similarity_model():
    """Lazy-load the sentence-transformers model used for free-form scoring.

    Same model as stage3_embed.py → scores are comparable to embedding space.
    """
    from sentence_transformers import SentenceTransformer, util
    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    return model, util


def _jaccard(pred: list, gold: list) -> float:
    pset = {str(x).lower().strip() for x in (pred or []) if x}
    gset = {str(x).lower().strip() for x in (gold or []) if x}
    if not pset and not gset:
        return 1.0
    if not pset or not gset:
        return 0.0
    return len(pset & gset) / len(pset | gset)


def _material_overlap(pred: str, gold: str) -> float:
    """Substring-based match for free-form material field."""
    if not pred and not gold:
        return 1.0
    if not pred or not gold:
        return 0.0
    p = pred.lower().strip()
    g = gold.lower().strip()
    if p == g:
        return 1.0
    # Partial credit if either contains the other (short terms like "concrete")
    if p in g or g in p:
        return 0.5
    return 0.0


def _score_enrich(pred: dict, gold: dict, text_sim) -> dict:
    return {
        "name_en_similarity":   text_sim(pred.get("name_en"), gold.get("name_en")),
        "program_exact":        int(pred.get("program") == gold.get("program")),
        "atmosphere_exact":     int(pred.get("atmosphere") == gold.get("atmosphere")),
        "material_overlap":     _material_overlap(pred.get("material") or "",
                                                  gold.get("material") or ""),
    }


def _score_analyze(pred: dict, gold: dict, text_sim) -> dict:
    return {
        "style_exact":          int(pred.get("style") == gold.get("style")),
        "color_tone_exact":     int(pred.get("color_tone") == gold.get("color_tone")),
        "atmosphere_exact":     int((pred.get("atmosphere") or None) == (gold.get("atmosphere") or None)),
        "vd_similarity":        text_sim(pred.get("visual_description"),
                                         gold.get("visual_description")),
        "mv_jaccard":           _jaccard(pred.get("material_visual"),
                                         gold.get("material_visual")),
    }


def _avg(values: list) -> float:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else 0.0


def run_eval(limit: int = None, skip_enrich: bool = False,
             skip_analyze: bool = False, label: str = None) -> dict:
    if not os.path.exists(GOLDEN_PATH):
        print(f"ERROR: golden set not found at {GOLDEN_PATH}")
        print("  Run `python3 label_golden.py --sample 30` first, or hand-populate.")
        return {"status": "no_golden"}

    with open(GOLDEN_PATH, encoding="utf-8") as f:
        golden = json.load(f)

    if not golden:
        print(f"WARNING: golden set at {GOLDEN_PATH} is empty. Nothing to score.")
        return {"status": "empty_golden"}

    if limit:
        golden = golden[:limit]

    print(f"Eval: scoring {len(golden)} golden-labeled buildings...")

    # Warn if the golden set has atmosphere=None entries — common when
    # label_golden.py was run with --source current and production had
    # freeform (pre-V2-enum) atmosphere values. Exact-match scoring on None
    # golden → 0 / N = 0% atmosphere accuracy, which is a measurement
    # artifact, not a prompt quality signal.
    null_atm = sum(
        1 for e in golden
        if (e.get("golden_enrich") or {}).get("atmosphere") is None
        or (e.get("golden_analyze") or {}).get("atmosphere") is None
    )
    if null_atm:
        pct = null_atm / len(golden) * 100
        print(f"  ⚠  {null_atm}/{len(golden)} ({pct:.0f}%) golden entries have")
        print(f"     atmosphere=None. atmosphere_accuracy will underreport.")
        print(f"     Re-label with `label_golden.py --source opus` for a valid score.")

    # Lazy-import agent modules (anthropic) only if we actually plan to call them.
    enrich_fn = analyze_fn = None
    enrich_pv = analyze_pv_with = analyze_pv_no = None
    if not skip_enrich:
        from enrich.llm_parser import enrich_building as enrich_fn, prompt_version
        enrich_pv = prompt_version()
    if not skip_analyze:
        from enrich.image_analysis import analyze_building as analyze_fn, prompt_version as pv_a
        analyze_pv_with = pv_a(True)
        analyze_pv_no = pv_a(False)

    # Semantic similarity via repo embedding model.
    model, util = _cosine_similarity_model()

    def text_sim(a: str, b: str) -> float:
        a = (a or "").strip()
        b = (b or "").strip()
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        emb = model.encode([a, b])
        return float(util.cos_sim(emb[0], emb[1]).item())

    per_building = []
    enrich_scores = {"name_en_similarity": [], "program_exact": [],
                     "atmosphere_exact": [], "material_overlap": []}
    analyze_scores = {"style_exact": [], "color_tone_exact": [],
                      "atmosphere_exact": [], "vd_similarity": [], "mv_jaccard": []}

    for entry in golden:
        bid = entry["building_id"]
        record = {"building_id": bid}

        if enrich_fn and entry.get("raw") and entry.get("golden_enrich"):
            try:
                pred = enrich_fn(entry["raw"])
                pred_dict = {
                    "name_en":    pred.name_en,
                    "program":    pred.program,
                    "material":   pred.material,
                    "atmosphere": pred.atmosphere,
                }
                scores = _score_enrich(pred_dict, entry["golden_enrich"], text_sim)
                record["enrich"] = {"pred": pred_dict, "scores": scores}
                for k, v in scores.items():
                    enrich_scores[k].append(v)
            except Exception as e:
                record["enrich"] = {"error": f"{type(e).__name__}: {e}"}

        if analyze_fn and entry.get("enriched") and entry.get("golden_analyze"):
            try:
                pred = analyze_fn(entry["enriched"])
                pred_dict = {
                    "style":              pred.style,
                    "color_tone":         pred.color_tone,
                    "material_visual":    pred.material_visual,
                    "visual_description": pred.visual_description,
                    "atmosphere":         pred.atmosphere,
                }
                scores = _score_analyze(pred_dict, entry["golden_analyze"], text_sim)
                record["analyze"] = {"pred": pred_dict, "scores": scores}
                for k, v in scores.items():
                    analyze_scores[k].append(v)
            except Exception as e:
                record["analyze"] = {"error": f"{type(e).__name__}: {e}"}

        per_building.append(record)
        print(f"  {bid}: enrich={'ok' if 'enrich' in record and 'scores' in record['enrich'] else 'skip'}  "
              f"analyze={'ok' if 'analyze' in record and 'scores' in record['analyze'] else 'skip'}")

    report = {
        "run_id": _now_iso(),
        "label": label or "default",
        "vocab_version": vocab.VOCAB_VERSION,
        "prompt_versions": {
            "enrich": enrich_pv,
            "analyze_with_atm": analyze_pv_with,
            "analyze_no_atm": analyze_pv_no,
        },
        "golden_size": len(golden),
        "enrich": {
            "n": len(enrich_scores["program_exact"]),
            "program_accuracy":   _avg(enrich_scores["program_exact"]),
            "atmosphere_accuracy": _avg(enrich_scores["atmosphere_exact"]),
            "name_en_similarity": _avg(enrich_scores["name_en_similarity"]),
            "material_overlap":   _avg(enrich_scores["material_overlap"]),
        },
        "analyze": {
            "n": len(analyze_scores["style_exact"]),
            "style_accuracy":     _avg(analyze_scores["style_exact"]),
            "color_tone_accuracy": _avg(analyze_scores["color_tone_exact"]),
            "atmosphere_accuracy": _avg(analyze_scores["atmosphere_exact"]),
            "visual_description_similarity": _avg(analyze_scores["vd_similarity"]),
            "material_visual_jaccard": _avg(analyze_scores["mv_jaccard"]),
        },
        "per_building": per_building,
    }

    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    with open(EVAL_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nEval complete → {EVAL_REPORT_PATH}")
    print(f"  Enrich (n={report['enrich']['n']}):")
    print(f"    program:    {report['enrich']['program_accuracy']:.3f}")
    print(f"    atmosphere: {report['enrich']['atmosphere_accuracy']:.3f}")
    print(f"    name_en:    {report['enrich']['name_en_similarity']:.3f}")
    print(f"    material:   {report['enrich']['material_overlap']:.3f}")
    print(f"  Analyze (n={report['analyze']['n']}):")
    print(f"    style:      {report['analyze']['style_accuracy']:.3f}")
    print(f"    color_tone: {report['analyze']['color_tone_accuracy']:.3f}")
    print(f"    atmosphere: {report['analyze']['atmosphere_accuracy']:.3f}")
    print(f"    vd_sim:     {report['analyze']['visual_description_similarity']:.3f}")
    print(f"    mv_jaccard: {report['analyze']['material_visual_jaccard']:.3f}")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Score current prompts against a golden set.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", choices=["enrich", "analyze"], default=None)
    parser.add_argument("--label", type=str, default=None,
                        help="Label for this run (appears in the report).")
    args = parser.parse_args()

    skip_enrich = args.only == "analyze"
    skip_analyze = args.only == "enrich"

    result = run_eval(
        limit=args.limit,
        skip_enrich=skip_enrich,
        skip_analyze=skip_analyze,
        label=args.label,
    )
    return 0 if result.get("status") not in ("no_golden", "empty_golden") else 1


if __name__ == "__main__":
    sys.exit(main())
