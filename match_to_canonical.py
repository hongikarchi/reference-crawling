#!/usr/bin/env python3
"""Match metalocus buildings to Divisare canonical projects (Phase 3).

Pipeline per metalocus building:
  1. Pre-filter Divisare candidates by (country, year ±2).
  2. Score each candidate: name_sim (rapidfuzz token_set_ratio), architect_match
     (cleaned name overlap), cosine_sim (sentence-transformers embedding).
  3. Decide verdict:
       accept_high     — name_sim ≥ 95 AND architect_match, OR cosine ≥ 0.93
       accept_medium   — name_sim ≥ 90 AND cosine ≥ 0.85
       needs_review    — middle band; defer to LLM tiebreaker (Phase 3.1)
       reject          — best cosine < 0.7 OR no candidates
  4. Write `data/match/metalocus_to_divisare.json` with the best match per
     metalocus building + its scores.

Reuses (no duplication):
  - stage2_dedup._name_similarity (rapidfuzz token_set_ratio / 100)
  - stage2_dedup._cosine_similarity
  - stage3_embed's SentenceTransformer model identifier
  - quality._clean_architect for cross-source architect normalization

Usage:
  python3 match_to_canonical.py            # full run on all metalocus + divisare
  python3 match_to_canonical.py --limit 100  # sample for inspection
  python3 match_to_canonical.py --building-id B00042  # inspect a single building
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
import divisare_db
import quality          # for _clean_architect
import stage2_dedup     # for _name_similarity, _cosine_similarity


MATCH_OUTPUT_PATH = os.path.join(config.DATA_DIR, "match", "metalocus_to_divisare.json")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

@dataclass
class DivisareProject:
    id: int
    slug: str
    name: str
    architect_ids: list
    architect_names: list
    location_country: Optional[str]
    location_city: Optional[str]
    project_year: Optional[int]


def _load_divisare_projects() -> list[DivisareProject]:
    out = []
    with divisare_db.get_db() as conn:
        for r in conn.execute(
            "SELECT id, slug, name, architect_ids, architect_names, "
            "location_country, location_city, project_year FROM divisare_projects"
        ).fetchall():
            d = dict(r)
            d["architect_ids"] = json.loads(d["architect_ids"]) if d["architect_ids"] else []
            d["architect_names"] = json.loads(d["architect_names"]) if d["architect_names"] else []
            out.append(DivisareProject(**d))
    return out


def _load_metalocus_buildings() -> list[dict]:
    if not os.path.exists(config.FINAL_JSON):
        print(f"ERROR: {config.FINAL_JSON} not found.")
        sys.exit(1)
    with open(config.FINAL_JSON, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _norm_country(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _build_match_text(name: str, architect: str, city: str, country: str) -> str:
    parts = [p for p in (name, architect, city, country) if p]
    return " ".join(parts)


# Generic architecture words that shouldn't drive architect-name matching
_GENERIC_ARCH_TOKENS = {
    "studio", "studios", "architect", "architects", "architecture", "architectes",
    "architettura", "design", "designs", "atelier", "office", "associates",
    "associati", "partners", "partnership", "and", "the", "co", "ltd",
}


def _strip_generic_tokens(name: str) -> str:
    tokens = re.split(r"\s+|[,&/+]", name.lower())
    kept = [t for t in tokens if t and t not in _GENERIC_ARCH_TOKENS]
    return " ".join(kept) if kept else name.lower()


def _architect_overlap(metalocus_architect: Optional[str],
                       divisare_names: list[str]) -> float:
    """0-1 score: cleaned metalocus architect ↔ any Divisare architect name.
    Generic words ("studio", "architects", …) are stripped before comparison
    so "Studio Fuksas" vs "Giuseppe Gurrieri Studio" doesn't score on the
    shared 'studio' token."""
    if not metalocus_architect or not divisare_names:
        return 0.0
    cleaned = quality._clean_architect(metalocus_architect) or metalocus_architect
    a = _strip_generic_tokens(cleaned)
    if not a:
        return 0.0
    best = 0.0
    for dn in divisare_names:
        if not dn:
            continue
        b = _strip_generic_tokens(dn)
        if not b:
            continue
        score = stage2_dedup._name_similarity(a, b)
        if score > best:
            best = score
    return best


def _decide_verdict(name_sim: float, architect_overlap: float, cosine: float,
                    has_candidates: bool) -> tuple[str, str]:
    """Returns (verdict, confidence).

    Tuned to require BOTH a strong name_sim AND a strong cosine for
    auto-accept. Cosine alone produces too many false positives in small
    corpora where same-country/year buildings cluster in embedding space.
    """
    if not has_candidates:
        return "reject", "no_candidates"
    # Strongest: name + architect both confirm
    if name_sim >= 0.90 and architect_overlap >= 0.70:
        return "accept_high", "name+architect"
    # Strong: very high cosine AND non-trivial name overlap
    if cosine >= 0.93 and name_sim >= 0.70:
        return "accept_high", "cosine+name"
    # Medium: solid all-round
    if name_sim >= 0.80 and cosine >= 0.85 and architect_overlap >= 0.50:
        return "accept_medium", "name+cosine+architect"
    # Mid-band candidates worth a closer look (LLM tiebreaker in Phase 3.1)
    if cosine >= 0.85 and name_sim >= 0.50:
        return "needs_review", "mid_band"
    if name_sim >= 0.80:
        return "needs_review", "name_high_cosine_mid"
    return "reject", "weak_signals"


# ---------------------------------------------------------------------------
# Main matcher
# ---------------------------------------------------------------------------

def match_all(metalocus_buildings: list[dict],
              divisare_projects: list[DivisareProject],
              limit: Optional[int] = None,
              filter_building_id: Optional[str] = None) -> list[dict]:
    if filter_building_id:
        metalocus_buildings = [b for b in metalocus_buildings
                               if b.get("building_id") == filter_building_id]
    if limit:
        metalocus_buildings = metalocus_buildings[:limit]

    print(f"Matching {len(metalocus_buildings)} metalocus → "
          f"{len(divisare_projects)} divisare projects...")

    # Pre-filter indexes:
    #   by_country_year[(country, year)] → [divisare indices]   (preferred — narrower)
    #   by_country[country]               → [divisare indices]   (fallback when project_year is NULL,
    #                                                              which is the lite-index case)
    by_country_year: dict = {}
    by_country: dict = {}
    for i, dp in enumerate(divisare_projects):
        if not dp.location_country:
            continue
        country = _norm_country(dp.location_country)
        by_country.setdefault(country, []).append(i)
        if dp.project_year:
            for offset in range(-2, 3):
                key = (country, dp.project_year + offset)
                by_country_year.setdefault(key, []).append(i)

    # Embedding model — reuse the same one as stage3_embed for consistency
    print("  Loading sentence-transformers model...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

    # Pre-embed all Divisare projects
    print(f"  Embedding {len(divisare_projects)} Divisare projects...")
    div_texts = [
        _build_match_text(dp.name, ", ".join(dp.architect_names),
                          dp.location_city or "", dp.location_country or "")
        for dp in divisare_projects
    ]
    div_embeds = model.encode(div_texts, batch_size=64, show_progress_bar=False)

    # Pre-embed metalocus too
    print(f"  Embedding {len(metalocus_buildings)} metalocus buildings...")
    mb_texts = [
        _build_match_text(b.get("name_en") or "",
                          b.get("architect") or "",
                          b.get("city") or "",
                          b.get("location_country") or "")
        for b in metalocus_buildings
    ]
    mb_embeds = model.encode(mb_texts, batch_size=64, show_progress_bar=False)

    results = []
    verdict_counter: Counter = Counter()

    for idx, mb in enumerate(metalocus_buildings):
        bid = mb.get("building_id")
        country = _norm_country(mb.get("location_country"))
        year = mb.get("year")

        # Candidate set: prefer (country, year ±2); fall back to all in country
        # when either side is missing year (the lite-index case).
        candidate_indices: list = []
        if country:
            if year:
                candidate_indices = by_country_year.get((country, year), [])[:]
                # Also include year-less Divisare projects in the same country
                # (lite mode — bulk of corpus has year=NULL until deep fetch)
                year_less = [i for i in by_country.get(country, [])
                             if divisare_projects[i].project_year is None]
                candidate_indices.extend(year_less)
            else:
                candidate_indices = by_country.get(country, [])[:]
        candidate_indices = list(dict.fromkeys(candidate_indices))  # dedupe preserve order

        record: dict = {
            "metalocus_building_id": bid,
            "metalocus_name_en":     mb.get("name_en"),
            "metalocus_architect":   mb.get("architect"),
            "metalocus_year":        year,
            "metalocus_country":     mb.get("location_country"),
            "candidates_evaluated":  len(candidate_indices),
        }

        if not candidate_indices:
            record.update({"verdict": "reject", "confidence": "no_candidates",
                           "best_match": None})
            verdict_counter["reject"] += 1
            results.append(record)
            continue

        # Score each candidate
        mb_embed = mb_embeds[idx]
        scored: list[dict] = []
        for ci in candidate_indices:
            dp = divisare_projects[ci]
            name_sim = stage2_dedup._name_similarity(mb.get("name_en") or "", dp.name or "")
            arch_overlap = _architect_overlap(mb.get("architect"), dp.architect_names)
            cos = float(stage2_dedup._cosine_similarity(mb_embed, div_embeds[ci]))
            scored.append({
                "divisare_id":    dp.id,
                "divisare_name":  dp.name,
                "divisare_year":  dp.project_year,
                "divisare_city":  dp.location_city,
                "divisare_architects": dp.architect_names,
                "name_sim":       round(name_sim, 4),
                "architect_overlap": round(arch_overlap, 4),
                "cosine":         round(cos, 4),
            })

        # Best candidate by cosine, tie-break by name_sim
        scored.sort(key=lambda x: (-x["cosine"], -x["name_sim"]))
        best = scored[0]
        verdict, conf = _decide_verdict(
            best["name_sim"], best["architect_overlap"], best["cosine"], True
        )

        record.update({
            "verdict":     verdict,
            "confidence":  conf,
            "best_match":  best,
            # Top-3 alternatives for inspection / future LLM tiebreak
            "alternatives": scored[1:3],
        })
        verdict_counter[verdict] += 1
        results.append(record)

        if (idx + 1) % 100 == 0:
            print(f"  ...{idx + 1}/{len(metalocus_buildings)}  verdicts so far: {dict(verdict_counter)}")

    return results, verdict_counter


# ---------------------------------------------------------------------------
# CLI + report
# ---------------------------------------------------------------------------

def _save_results(results: list[dict], verdict_counter: Counter,
                  metalocus_count: int, divisare_count: int) -> str:
    os.makedirs(os.path.dirname(MATCH_OUTPUT_PATH), exist_ok=True)
    output = {
        "run_id":            datetime.now(timezone.utc).isoformat(),
        "metalocus_count":   metalocus_count,
        "divisare_count":    divisare_count,
        "evaluated":         len(results),
        "verdict_summary":   dict(verdict_counter),
        "results":           results,
    }
    with open(MATCH_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    return MATCH_OUTPUT_PATH


def _print_summary(verdict_counter: Counter, total: int) -> None:
    print(f"\nVerdict summary (n={total}):")
    for v, c in verdict_counter.most_common():
        pct = c / total * 100 if total else 0
        print(f"  {v:18s} {c:6d}  {pct:5.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description="Match metalocus → Divisare canonical")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only score the first N metalocus buildings")
    parser.add_argument("--building-id", type=str, default=None,
                        help="Score only this metalocus building_id (debug)")
    args = parser.parse_args()

    metalocus = _load_metalocus_buildings()
    divisare  = _load_divisare_projects()

    if not divisare:
        print("ERROR: data/divisare.db has no projects. "
              "Run `python3 run.py crawl-divisare` first.")
        return 1

    results, vc = match_all(
        metalocus, divisare,
        limit=args.limit, filter_building_id=args.building_id,
    )

    path = _save_results(results, vc, len(metalocus), len(divisare))
    _print_summary(vc, len(results))
    print(f"\nFull report → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
