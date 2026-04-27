#!/usr/bin/env python3
"""Quality measurement and auto-fixing for 4_buildings_final.json.

Four operations, all reading the same final JSON:

  review    — validate fields, vocab, embeddings → reports/review_report.json
  fix       — auto-normalize + clean + audit log → updates 4_buildings_final.json
                                                  + reports/fix_report.json
  rate      — score 6 dimensions 0-100         → reports/rating_report.json
  diagnose  — print distributions to stdout    → no file output

Usage:
    python3 quality.py review
    python3 quality.py fix
    python3 quality.py rate
    python3 quality.py diagnose

Or from the unified CLI:
    python3 run.py quality {review,fix,rate,diagnose}

This file consolidates what used to be review.py, fix.py, rate.py, diagnose.py
(Phase 8A consolidation — see ~/.claude/plans/db-fuzzy-lerdorf.md).
"""

import argparse
import collections
import json
import os
import re
import sys
from collections import Counter

from core import config
from core import vocab


# ===========================================================================
# Shared helpers
# ===========================================================================

def _load_final() -> list:
    """Load 4_buildings_final.json or exit with error message."""
    if not os.path.exists(config.FINAL_JSON):
        print(f"ERROR: {config.FINAL_JSON} not found. Run stage3_embed.py first.")
        sys.exit(1)
    with open(config.FINAL_JSON, encoding="utf-8") as f:
        return json.load(f)


def _filled(b: dict, field: str) -> bool:
    """True if field is non-None, non-empty-string, non-empty-list."""
    v = b.get(field)
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return len(v) > 0
    return True


def _pct_filled(buildings: list, field: str) -> float:
    n = len(buildings)
    if not n:
        return 0.0
    return sum(1 for b in buildings if _filled(b, field)) / n


def _bar(pct: float, width: int = 20) -> str:
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)


def _write_report(name: str, payload: dict) -> str:
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    path = os.path.join(config.REPORTS_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


# ===========================================================================
# REVIEW — validate fields, vocab, embeddings
# ===========================================================================

def run_review() -> dict:
    """Validate 4_buildings_final.json. Writes reports/review_report.json."""
    buildings = _load_final()
    n = len(buildings)
    print(f"Reviewing {n} buildings...")

    issues = []
    field_coverage: dict = {}
    recommendations: list = []

    # Required fields
    required = ["building_id", "name_en", "program", "embedding"]
    for field in required:
        missing = [b.get("slug", "?") for b in buildings if not b.get(field)]
        field_coverage[field] = {
            "filled": n - len(missing),
            "pct": round((n - len(missing)) / n, 4) if n else 0,
        }
        if missing:
            issues.append({
                "type": "missing_required",
                "field": field,
                "count": len(missing),
                "examples": missing[:5],
            })

    # Optional field coverage
    optional = [
        "architect", "description", "atmosphere", "style",
        "visual_description", "material_visual", "color_tone",
        "material", "year", "location_country",
    ]
    for field in optional:
        filled = sum(1 for b in buildings if _filled(b, field))
        field_coverage[field] = {
            "filled": filled,
            "pct": round(filled / n, 4) if n else 0,
        }
        if filled < n:
            issues.append({
                "type": "null_field",
                "field": field,
                "count": n - filled,
            })

    # Vocabulary compliance (vocab.py is canonical)
    for field in ("program", "style", "color_tone", "atmosphere"):
        bad = [
            b.get("slug", "?")
            for b in buildings
            if b.get(field) and not vocab.is_valid(field, b.get(field))
        ]
        if bad:
            issues.append({
                "type": "invalid_vocabulary",
                "field": field,
                "count": len(bad),
                "examples": bad[:5],
            })

    # Vocab version drift
    older = [
        b.get("slug", "?")
        for b in buildings
        if b.get("vocab_version") and b["vocab_version"] != vocab.VOCAB_VERSION
    ]
    if older:
        issues.append({
            "type": "stale_vocab_version",
            "current": vocab.VOCAB_VERSION,
            "count": len(older),
            "examples": older[:5],
        })
    unstamped = [b for b in buildings if not b.get("vocab_version")]
    if unstamped:
        issues.append({
            "type": "missing_vocab_version",
            "count": len(unstamped),
            "examples": [b.get("slug", "?") for b in unstamped[:5]],
        })

    # Duplicate IDs
    id_counts = Counter(b.get("building_id") for b in buildings)
    dupe_ids = [k for k, v in id_counts.items() if v > 1]
    if dupe_ids:
        issues.append({
            "type": "duplicate_building_id",
            "count": len(dupe_ids),
            "examples": dupe_ids[:5],
        })

    slug_counts = Counter(b.get("slug") for b in buildings)
    dupe_slugs = [k for k, v in slug_counts.items() if v > 1]
    if dupe_slugs:
        issues.append({
            "type": "duplicate_slug",
            "count": len(dupe_slugs),
            "examples": dupe_slugs[:5],
        })

    # Embedding dimensions
    wrong_dim = [
        b.get("slug", "?")
        for b in buildings
        if b.get("embedding") and len(b["embedding"]) != 384
    ]
    if wrong_dim:
        issues.append({
            "type": "wrong_embedding_dim",
            "count": len(wrong_dim),
            "expected": 384,
            "examples": wrong_dim[:5],
        })

    # Recommendations
    vd_pct  = field_coverage.get("visual_description", {}).get("pct", 0)
    style_pct = field_coverage.get("style", {}).get("pct", 0)
    mv_pct  = field_coverage.get("material_visual", {}).get("pct", 0)
    ct_pct  = field_coverage.get("color_tone", {}).get("pct", 0)
    atm_pct = field_coverage.get("atmosphere", {}).get("pct", 0)

    if vd_pct < 0.8:
        miss = n - field_coverage["visual_description"]["filled"]
        recommendations.append(f"Run image analysis on {miss} buildings missing visual_description")
    if style_pct < 0.8:
        miss = n - field_coverage["style"]["filled"]
        recommendations.append(f"Run image analysis on {miss} buildings missing style")
    if ct_pct < 0.8:
        recommendations.append("Run image analysis to fill missing color_tone values")
    if mv_pct < 0.8:
        recommendations.append("Run image analysis to fill missing material_visual values")
    if atm_pct < 0.9:
        miss = n - field_coverage["atmosphere"]["filled"]
        recommendations.append(f"Fill atmosphere for {miss} buildings (can be inferred from description)")
    if any(i["type"] == "invalid_vocabulary" for i in issues):
        recommendations.append("Run `quality.py fix` to normalize invalid vocabulary values")
    if n < 500:
        recommendations.append(f"Only {n} buildings — target is 500. Run crawl to add more.")

    # Status
    critical = [i for i in issues if i["type"] in (
        "missing_required", "duplicate_building_id", "wrong_embedding_dim"
    )]
    if critical:
        status = "fail"
    elif issues:
        status = "warning"
    else:
        status = "pass"

    report = {
        "status": status,
        "total_buildings": n,
        "field_coverage": field_coverage,
        "issues": issues,
        "recommendations": recommendations,
    }
    path = _write_report("review_report.json", report)

    print(f"\nReview complete: status={status}")
    print(f"  Issues found:  {len(issues)}")
    print(f"  Field coverage:")
    for field, cov in field_coverage.items():
        bar = "█" * int(cov["pct"] * 20) + "░" * (20 - int(cov["pct"] * 20))
        print(f"    {field:20s} {bar} {cov['pct']:.0%}  ({cov['filled']}/{n})")
    if recommendations:
        print(f"\n  Recommendations:")
        for r in recommendations:
            print(f"    - {r}")
    print(f"\n  Output: {path}")
    return report


# ===========================================================================
# FIX — auto-normalize fields, clean text, audit-log every rewrite
# ===========================================================================

def _clean_architect(value):
    """Extract primary firm name; strip role annotations and secondary partners.

    Input:  "KAAN Architecten. Lead architects.- Kees Kaan, Vincent Panhuysen."
    Output: "KAAN Architecten"
    """
    if not value:
        return value
    primary = re.split(
        r'\.\s*(?:Lead|Co-author|Local partner|Associate|Collaborat|Consultant|'
        r'Landscape|Interior|Executive|Project|Structural|Civil|Mechanical|'
        r'Technical|Design team)',
        value, maxsplit=1, flags=re.I
    )[0]
    primary = re.split(r'\.\-\s*', primary, maxsplit=1)[0]
    primary = primary.rstrip('. ').strip()
    return primary if primary else value


def _clean_name(name_en, project_name):
    """Strip editorial hook prefix from name_en.

    Only strips when: exactly one '. ' separator, prefix <= 50 chars, no digits,
    confirmed by project_name starting with same prefix.
    """
    if not name_en:
        return name_en
    dot_pos = name_en.find('. ')
    if dot_pos <= 0:
        return name_en
    prefix = name_en[:dot_pos]
    remainder = name_en[dot_pos + 2:]
    if len(prefix) > 50 or len(remainder) < 5:
        return name_en
    if re.search(r'\d', prefix):
        return name_en
    if name_en.count('. ') > 1:
        return name_en
    if project_name and not project_name.startswith(prefix + '.'):
        return name_en
    return remainder


def _clean_country(value):
    """Strip date suffixes appended to country names. 'Spain 27.02 > 10.05.2026' → 'Spain'."""
    if not value:
        return value
    cleaned = re.sub(r'\s+\d{1,2}[./]\d{2}.*$', '', value).strip()
    return cleaned if cleaned else value


def run_fix() -> dict:
    """Auto-fix vocab + cleanup. Updates 4_buildings_final.json in place."""
    buildings = _load_final()
    n = len(buildings)
    print(f"Fixing {n} buildings...")

    fixes = {
        "program": 0, "style": 0, "color_tone": 0,
        "atmosphere_from_mood": 0,
        "architect": 0, "name_en": 0, "country": 0,
    }
    needs_claude = []
    audit = []
    vocab.set_audit_sink(audit)

    for b in buildings:
        slug = b.get("slug", "?")

        # Normalize program — invalid values fall back to "Other"
        orig = b.get("program")
        fixed, reason = vocab.normalize_loose("program", orig)
        if reason:
            b["program"] = fixed if fixed is not None else "Other"
            fixes["program"] += 1
        elif orig and not vocab.is_valid("program", orig):
            b["program"] = "Other"
            fixes["program"] += 1

        # Normalize style — invalid values zeroed and flagged for re-analysis
        orig = b.get("style")
        fixed, reason = vocab.normalize_loose("style", orig)
        if reason:
            b["style"] = fixed
            fixes["style"] += 1
            if fixed is None:
                needs_claude.append({"slug": slug,
                                     "issue": f"style dropped (legacy {orig!r}), needs re-analysis"})
        elif orig and not vocab.is_valid("style", orig):
            b["style"] = None
            fixes["style"] += 1
            needs_claude.append({"slug": slug, "issue": f"invalid style: {orig!r}"})

        # Normalize color_tone
        orig = b.get("color_tone")
        fixed, reason = vocab.normalize_loose("color_tone", orig)
        if reason:
            b["color_tone"] = fixed
            fixes["color_tone"] += 1
            if fixed is None:
                needs_claude.append({"slug": slug,
                                     "issue": f"color_tone dropped (legacy {orig!r}), needs re-analysis"})
        elif orig and not vocab.is_valid("color_tone", orig):
            b["color_tone"] = None
            fixes["color_tone"] += 1
            needs_claude.append({"slug": slug, "issue": f"invalid color_tone: {orig!r}"})

        # Fill atmosphere from legacy mood field
        if not b.get("atmosphere") and b.get("mood"):
            b["atmosphere"] = b["mood"]
            fixes["atmosphere_from_mood"] += 1

        # Clean text fields
        orig = b.get("architect")
        fixed = _clean_architect(orig)
        if fixed and fixed != orig:
            b["architect"] = fixed
            fixes["architect"] += 1

        orig = b.get("name_en")
        fixed = _clean_name(orig, b.get("project_name"))
        if fixed and fixed != orig:
            b["name_en"] = fixed
            fixes["name_en"] += 1

        orig = b.get("location_country")
        fixed = _clean_country(orig)
        if fixed and fixed != orig:
            b["location_country"] = fixed
            fixes["country"] += 1

        if not b.get("visual_description"):
            needs_claude.append({"slug": slug, "issue": "missing visual_description"})
        if not b.get("name_en"):
            needs_claude.append({"slug": slug, "issue": "missing name_en"})

    # Persist
    with open(config.FINAL_JSON, "w", encoding="utf-8") as f:
        json.dump(buildings, f, ensure_ascii=False, indent=2)
    vocab.set_audit_sink(None)

    report = {
        "auto_fixed": fixes,
        "needs_claude": needs_claude[:50],
        "needs_claude_total": len(needs_claude),
        "vocab_rewrites": [
            {"field": e.field, "from": e.original, "to": e.rewritten,
             "reason": e.reason, "kind": e.kind}
            for e in audit
        ],
        "vocab_rewrites_total": len(audit),
    }
    path = _write_report("fix_report.json", report)

    print(f"\nFix complete:")
    print(f"  program normalized:       {fixes['program']}")
    print(f"  style normalized:         {fixes['style']}")
    print(f"  color_tone normalized:    {fixes['color_tone']}")
    print(f"  atmosphere filled (mood): {fixes['atmosphere_from_mood']}")
    print(f"  architect cleaned:        {fixes['architect']}")
    print(f"  name_en cleaned:          {fixes['name_en']}")
    print(f"  country cleaned:          {fixes['country']}")
    print(f"  Needs Claude attention:   {len(needs_claude)}")
    print(f"\n  Output: {config.FINAL_JSON}")
    print(f"  Report: {path}")
    return report


# ===========================================================================
# RATE — score quality across 6 dimensions
# ===========================================================================

def _score_completeness(buildings):
    weights = {
        "building_id": 1.0, "name_en": 1.0, "program": 1.0, "embedding": 1.0,
        "architect": 0.8, "description": 0.8, "atmosphere": 0.7,
        "style": 0.7, "location_country": 0.6, "year": 0.4,
    }
    total_weight = sum(weights.values())
    weighted = sum(_pct_filled(buildings, f) * w for f, w in weights.items())
    score = int(weighted / total_weight * 100)

    coverage = {f: round(_pct_filled(buildings, f), 3) for f in weights}
    weak = [f for f, pct in coverage.items() if pct < 0.9 and weights[f] >= 0.7]
    note = (f"name_en {coverage['name_en']:.0%}, "
            f"style {coverage['style']:.0%}, "
            f"atmosphere {coverage['atmosphere']:.0%}")
    if weak:
        note += f" — low: {', '.join(weak)}"
    return score, note


def _score_visual_depth(buildings):
    weights = {"style": 1.0, "atmosphere": 0.8, "color_tone": 0.8,
               "material_visual": 0.7, "visual_description": 1.0}
    total_weight = sum(weights.values())
    weighted = sum(_pct_filled(buildings, f) * w for f, w in weights.items())
    score = int(weighted / total_weight * 100)
    vd = _pct_filled(buildings, "visual_description")
    style = _pct_filled(buildings, "style")
    note = f"visual_description {vd:.0%}, style {style:.0%}"
    if vd < 0.7:
        note += " — visual_description is critical, run image analysis"
    return score, note


def _score_description(buildings):
    descriptions = [b.get("description") or "" for b in buildings]
    filled = sum(1 for d in descriptions if d.strip())
    filled_pct = filled / len(buildings) if buildings else 0
    avg_len = (sum(len(d) for d in descriptions if d.strip()) / filled
               if filled else 0)
    length_score = min(avg_len / 200, 1.0)
    score = int((filled_pct * 0.6 + length_score * 0.4) * 100)
    note = f"avg {int(avg_len)} chars, {filled_pct:.0%} filled (target: 200 chars)"
    return score, note


def _score_image_coverage(buildings):
    total_photos = total_drawings = has_any_drawing = 0
    for b in buildings:
        images = b.get("images", [])
        photos = sum(1 for i in images if i.get("type") == "photo" and i.get("upload"))
        drawings = sum(1 for i in images if i.get("type") == "drawing" and i.get("upload"))
        total_photos += photos
        total_drawings += drawings
        if drawings > 0:
            has_any_drawing += 1

    n = len(buildings)
    avg_photos = total_photos / n if n else 0
    drawing_pct = has_any_drawing / n if n else 0
    photo_score = min(avg_photos / 1.0, 1.0)
    drawing_score = min(drawing_pct / 0.4, 1.0)
    score = int((photo_score * 0.6 + drawing_score * 0.4) * 100)
    note = f"avg {avg_photos:.1f} photos, {drawing_pct:.0%} have ≥1 drawing"
    return score, note


def _score_diversity(buildings):
    programs = Counter(b.get("program", "Other") for b in buildings)
    n = len(buildings)
    if not n:
        return 0, "no buildings"
    top_program, top_count = programs.most_common(1)[0]
    top_pct = top_count / n
    if top_pct > 0.35:
        score = max(0, int(100 - (top_pct - 0.35) * 300))
    else:
        score = min(100, 60 + len(programs) * 3)
    note = f"{top_program} {top_pct:.0%} (target <35%), {len(programs)} distinct programs"
    return score, note


def _score_scale(buildings):
    n = len(buildings)
    target = 500
    score = min(100, int(n / target * 100))
    return score, f"{n} buildings, target {target}"


def run_rate() -> dict:
    """Score 6 quality dimensions. Writes reports/rating_report.json."""
    buildings = _load_final()
    n = len(buildings)
    print(f"Rating {n} buildings...")

    scorers = [
        ("completeness",   _score_completeness),
        ("visual_depth",   _score_visual_depth),
        ("description",    _score_description),
        ("image_coverage", _score_image_coverage),
        ("diversity",      _score_diversity),
        ("scale",          _score_scale),
    ]
    dim_scores = {}
    for name, fn in scorers:
        score, note = fn(buildings)
        dim_scores[name] = {"score": score, "note": note}

    overall = int(sum(d["score"] for d in dim_scores.values()) / len(dim_scores))

    weaknesses = []
    for name, d in sorted(dim_scores.items(), key=lambda x: x[1]["score"]):
        if d["score"] < 70:
            weaknesses.append(f"{name} is low ({d['score']}) — {d['note']}")

    if overall >= 80 and n >= 500:
        judgment = "Ready. Visual fields sufficient, count on target, good diversity."
    elif overall >= 65:
        judgment = f"Borderline. Overall {overall}/100 — address weaknesses before upload."
    else:
        judgment = f"Not ready. Overall {overall}/100 — significant gaps remain."

    report = {
        "overall": overall,
        "dimensions": dim_scores,
        "weaknesses": weaknesses,
        "judgment": judgment,
    }
    path = _write_report("rating_report.json", report)

    print(f"\nRating complete: overall={overall}/100")
    for name, d in dim_scores.items():
        bar = "█" * (d["score"] // 5) + "░" * (20 - d["score"] // 5)
        print(f"  {name:16s} {bar} {d['score']:3d}  {d['note']}")
    if weaknesses:
        print(f"\n  Weaknesses:")
        for w in weaknesses:
            print(f"    - {w}")
    print(f"\n  Judgment: {judgment}")
    print(f"\n  Output: {path}")
    return report


# ===========================================================================
# DIAGNOSE — distribution analysis (stdout only, no file output)
# ===========================================================================

def _print_distribution(title: str, counter: Counter, total: int,
                        valid_set=None, limit_pct: int = 35) -> None:
    print(f"\n{title}")
    print("-" * 60)
    warnings: list = []
    oov: list = []
    for value, count in counter.most_common():
        pct = count / total
        flag = ""
        if valid_set and value and value not in valid_set:
            flag = "  ← out of vocabulary"
            oov.append((value, count))
        label = str(value) if value is not None else "(missing)"
        bar = _bar(pct)
        print(f"  {label:25s} {count:5d}  {pct*100:5.1f}%  {bar}{flag}")
        if pct > limit_pct / 100:
            warnings.append(f"  ⚠  {label} at {pct*100:.1f}% exceeds {limit_pct}% guideline")

    if warnings:
        print()
        for w in warnings:
            print(w)
    if oov:
        print()
        total_oov = sum(c for _, c in oov)
        names = ", ".join(f"{v} ({c})" for v, c in oov)
        print(f"  ⚠  Out-of-vocabulary ({total_oov} buildings): {names}")
        print(f"     Run: python3 quality.py fix  (remaps known values, clears unknown)")


def run_diagnose() -> None:
    """Print distribution report to stdout. No file output."""
    buildings = _load_final()
    total = len(buildings)
    print(f"\n=== Distribution Report — {total} buildings ===")

    _print_distribution("PROGRAM",     collections.Counter(b.get("program") for b in buildings),     total, vocab.PROGRAM)
    _print_distribution("STYLE",       collections.Counter(b.get("style") for b in buildings),       total, vocab.STYLE)
    _print_distribution("COLOR TONE",  collections.Counter(b.get("color_tone") for b in buildings),  total, vocab.COLOR_TONE)
    _print_distribution("ATMOSPHERE",  collections.Counter(b.get("atmosphere") for b in buildings),  total, vocab.ATMOSPHERE)

    raw_countries = collections.Counter(b.get("location_country") for b in buildings)
    dirty = [(v, c) for v, c in raw_countries.items()
             if v and re.search(r'\d{1,2}[./]\d{2}', v)]
    print(f"\nCOUNTRY (top 20 of {len(raw_countries)})")
    print("-" * 60)
    for value, count in raw_countries.most_common(20):
        pct = count / total
        label = str(value) if value is not None else "(missing)"
        bar = _bar(pct)
        print(f"  {label:30s} {count:5d}  {pct*100:5.1f}%  {bar}")
    if dirty:
        print()
        print(f"  ⚠  Dirty country values ({sum(c for _, c in dirty)} buildings):")
        for v, c in dirty:
            print(f"     {v!r} ({c})")
        print(f"     Run: python3 quality.py fix  (strips date suffixes)")

    print(f"\nNULL COVERAGE")
    print("-" * 60)
    for field in ("name_en", "style", "color_tone", "atmosphere",
                  "material", "visual_description", "location_country"):
        missing = sum(1 for b in buildings if not b.get(field))
        pct = missing / total * 100 if total else 0
        flag = "  ← needs Claude" if missing > 0 else ""
        print(f"  {field:25s}  {missing:5d} missing  ({pct:.1f}%){flag}")
    print()


# ===========================================================================
# CLI
# ===========================================================================

_DISPATCH = {
    "review":   run_review,
    "fix":      run_fix,
    "rate":     run_rate,
    "diagnose": run_diagnose,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Quality measurement and auto-fix for 4_buildings_final.json"
    )
    parser.add_argument("cmd", choices=list(_DISPATCH.keys()),
                        help="Which quality operation to run.")
    args = parser.parse_args()
    _DISPATCH[args.cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
