"""Phase 13 — Reality filter: 4-layer defense against junk rows.

Decides KEEP / DROP for each candidate canonical building, with a
`confidence_tier` ∈ {T1, T2, T3} on KEEPs.

Layers (first decisive layer wins):

  L1  Cross-source confirmation       — ≥2 sources via Stage-2 match
                                         verdict ∈ {accept_high, accept_medium}
                                       → KEEP, tier=T1
  L2  Refined regex + positive override — drop on negative patterns
                                         (action verbs, event keywords,
                                         interview/monograph patterns,
                                         length > MAX), but KEEP if a
                                         building noun is present
  L3  Structural metadata signal      — KEEP if any of: area_sqm,
                                         year, country, source-specific
                                         "this is a project" tag
                                       → tier=T2
  L4  Haiku final classifier (caller-invoked) — for rows that survived
                                         L2 but failed L3 AND have a
                                         verified architect link
                                       → tier=T3 if yes, DROP if no

This module implements L1-L3 inline. L4 is signalled via the
"DEFER_L4" decision; the caller is responsible for invoking
canonical/match_tiebreaker.py:haiku_classify() (TODO Phase 14a).
Until L4 is wired, callers can treat DEFER_L4 as DROP (conservative).

Decisions are made on a building DICT with these keys (any subset):
    name OR name_en OR project_name
    area_sqm OR building_area_m2
    year OR project_year OR completion_year
    location_country OR country
    building_type                         (metalocus)
    tag_slugs                             (Divisare; list[str])
    constr_status, categories             (Architizer)
    category                              (Archello)
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional, Literal


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# L2a — negative patterns
_ARTICLE_VERB_RE = re.compile(
    r"\b(?:wins?|won|announces?|announced|reveals?|revealed|applies|applied|"
    r"uses?|used|opens?|opened|presents?|presented|launches?|launched|"
    r"unveil(?:s|ed)?|debuts?|debuted|nominates?|nominated|"
    r"publishes?|published|exhibits?|exhibited|showcases?|showcased)\b",
    re.IGNORECASE,
)

# Event / award / competition keywords — drop unless positive override
_NON_BUILDING_KEYWORDS_RE = re.compile(
    r"\b(?:biennale|symposium|conference|lecture|award|nominat|finalist|"
    r"winner|competition\s+for|short\s*list|long\s*list|prize)\b",
    re.IGNORECASE,
)

# Interview-style article keywords — added per Phase 13 plan
_INTERVIEW_KEYWORDS_RE = re.compile(
    r"\b(?:interview\s+with|in\s+conversation\s+with|monograph|retrospective|"
    r"tribute\s+to|obituary|all\s+entries|all\s+participants|"
    r"official\s+list)\b",
    re.IGNORECASE,
)

# L2b — positive override (KEEP even if L2a negative pattern hits).
# 40+ building nouns: explicit building / landscape / residential / F&B /
# studio / cultural typology terms. If the name contains any of these as a
# whole word, the row is almost certainly about a real architectural
# project, not an article about an event.
_BUILDING_NOUN_RE = re.compile(
    # Explicit building types
    r"\b(?:Pavilion|Pavillon|House|Tower|Museum|Library|School|Hospital|"
    r"Stadium|Arena|Cathedral|Chapel|Bridge|Embassy|Mosque|Temple|Synagogue|"
    r"Office|Offices|Headquarters|Hall|Theater|Theatre|Gallery|Cinema|"
    r"Church|Residence|Center|Centre|Hotel|Hostel|Resort|"
    # Landscape architecture
    r"Plaza|Park|Garden|Memorial|Cemetery|Promenade|"
    # Residential
    r"Apartment|Apartments|Loft|Condominium|Villa|Cabin|Cottage|Cottages|"
    # Hospitality / F&B / wellness
    r"Restaurant|Cafe|Café|Bar|Spa|Sauna|"
    # Studio / workshop / industrial
    r"Studio|Workshop|Atelier|Factory|Warehouse|"
    # Educational extras
    r"University|Campus|Kindergarten|Nursery|"
    # Civic / cultural
    r"Town\s*Hall|City\s*Hall|Library|Pavilion"
    r")\b",
    re.IGNORECASE,
)

# L2 — name length cap (raised from 70 → 100; positive override
# compensates for legitimate-but-long names like "Reconfiguration of
# the Reina Sofía Museum public areas and access points by …Architects")
MAX_BUILDING_NAME_LEN = 100

# Bad metalocus building_type values (these signal non-building rows)
_NON_BUILDING_TYPES = {
    "exhibition", "event", "installation", "award", "competition",
    "lecture", "interview", "obituary",
}


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ConfidenceTier = Literal["T1", "T2", "T3"]
Decision = Literal["KEEP", "DROP", "DEFER_L4"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _name_of(building: dict) -> str:
    """Pick the best name field. Try canonical 'name' first, then
    'name_en' (LLM-cleaned), then 'project_name' (raw)."""
    for k in ("name", "name_en", "project_name", "title"):
        v = building.get(k)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _has_area(b: dict) -> bool:
    for k in ("area_sqm", "building_area_m2"):
        v = b.get(k)
        if v is not None and v != "":
            try:
                if float(v) > 0:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def _has_year(b: dict) -> bool:
    for k in ("year", "project_year", "completion_year"):
        v = b.get(k)
        if v in (None, ""):
            continue
        try:
            n = int(v)
            if 1800 < n <= 2100:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _has_country(b: dict) -> bool:
    for k in ("location_country", "country"):
        v = b.get(k)
        if v and isinstance(v, str) and v.strip():
            return True
    return False


_BUILDING_TAGS_CACHE: Optional[set] = None


def _building_tag_set() -> set:
    """Lazy-load from canonical/_known_building_tags.json. Empty if missing
    (file is curated; can grow over time)."""
    global _BUILDING_TAGS_CACHE
    if _BUILDING_TAGS_CACHE is None:
        path = os.path.join(os.path.dirname(__file__),
                            "_known_building_tags.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    _BUILDING_TAGS_CACHE = set(json.load(f))
            except (json.JSONDecodeError, OSError):
                _BUILDING_TAGS_CACHE = set()
        else:
            _BUILDING_TAGS_CACHE = set()
    return _BUILDING_TAGS_CACHE


def _has_source_type_signal(b: dict) -> bool:
    """Source-specific 'this is a building project' signal. Returns True
    if any one source's positive type field is present and meaningful."""
    # metalocus
    bt = b.get("building_type")
    if bt and str(bt).strip().lower() not in _NON_BUILDING_TYPES:
        return True

    # Architizer
    constr_status = b.get("constr_status")
    categories = b.get("categories")
    if constr_status in ("built", "concept", "on_hold"):
        if categories:  # firm uploaded with a category
            return True

    # Archello
    cat = b.get("category")
    if cat and str(cat).strip():
        return True

    # Divisare
    tag_slugs = b.get("tag_slugs") or []
    if isinstance(tag_slugs, str):
        try:
            tag_slugs = json.loads(tag_slugs)
        except json.JSONDecodeError:
            tag_slugs = []
    building_tags = _building_tag_set()
    if building_tags and any(t in building_tags for t in tag_slugs):
        return True

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reality_filter(
    building: dict,
    *,
    multi_source_confirmed: bool = False,
    architect_verified: bool = False,
) -> tuple[Decision, Optional[ConfidenceTier], str]:
    """4-layer defense. Returns (decision, tier, reason).

    Decisions:
      - ('KEEP', 'T1', 'L1_cross_source')      — multi-source confirmed
      - ('KEEP', 'T2', 'L3_struct_signal:...')  — single-source w/ struct meta
      - ('KEEP', 'T3', 'L4_haiku_yes')          — caller fills after Haiku call
      - ('DROP', None, 'L2_<pattern>')          — bad name pattern, no override
      - ('DROP', None, 'L3_no_arch_link')       — no architect cluster match
      - ('DEFER_L4', None, 'L3_no_struct_defer')— caller should invoke Haiku
                                                  (treat as DROP if Haiku off)
    """
    name = _name_of(building)

    # ----- L1: cross-source confirmation -----
    if multi_source_confirmed:
        return "KEEP", "T1", "L1_cross_source"

    # ----- L2: refined regex with positive override -----
    if not name:
        return "DROP", None, "L2_empty_name"

    has_building_noun = bool(_BUILDING_NOUN_RE.search(name))

    # Detect each negative pattern (for explainable reason strings)
    too_long = len(name) > MAX_BUILDING_NAME_LEN
    has_action_verb = bool(_ARTICLE_VERB_RE.search(name))
    has_event_kw = bool(_NON_BUILDING_KEYWORDS_RE.search(name))
    has_interview_kw = bool(_INTERVIEW_KEYWORDS_RE.search(name))
    has_negative = too_long or has_action_verb or has_event_kw or has_interview_kw

    if has_negative and not has_building_noun:
        if too_long:
            reason = f"L2_name_too_long({len(name)})"
        elif has_action_verb:
            reason = "L2_action_verb"
        elif has_event_kw:
            reason = "L2_non_building_keyword"
        else:
            reason = "L2_interview_keyword"
        return "DROP", None, reason

    # ----- L3: requires architect link first -----
    if not architect_verified:
        return "DROP", None, "L3_no_architect_link"

    # Structural metadata signals
    sig_area = _has_area(building)
    sig_year = _has_year(building)
    sig_country = _has_country(building)
    sig_type = _has_source_type_signal(building)

    if sig_area or sig_year or sig_country or sig_type:
        # Build an informative reason string
        sigs = []
        if sig_area:    sigs.append("area")
        if sig_year:    sigs.append("year")
        if sig_country: sigs.append("country")
        if sig_type:    sigs.append("type")
        return "KEEP", "T2", f"L3_struct_signal:{'+'.join(sigs)}"

    # ----- L4: defer to Haiku (caller-invoked) -----
    return "DEFER_L4", None, "L3_no_struct_defer_to_L4"


def reality_filter_with_default_l4(
    building: dict,
    *,
    multi_source_confirmed: bool = False,
    architect_verified: bool = False,
    l4_default: Decision = "DROP",
) -> tuple[Decision, Optional[ConfidenceTier], str]:
    """Convenience wrapper: invoke reality_filter() and resolve any DEFER_L4
    to the given default. Use l4_default='DROP' for conservative pre-Haiku
    runs; 'KEEP' (with tier='T3') for permissive pre-Haiku runs."""
    decision, tier, reason = reality_filter(
        building,
        multi_source_confirmed=multi_source_confirmed,
        architect_verified=architect_verified,
    )
    if decision == "DEFER_L4":
        if l4_default == "KEEP":
            return "KEEP", "T3", "L4_default_keep_haiku_disabled"
        return "DROP", None, "L4_default_drop_haiku_disabled"
    return decision, tier, reason


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """Run a labeled-fixture test of reality_filter on a few hand-crafted cases.
    Useful for parser regression checking. Pass `--canonical PATH` to score
    an existing canonical_buildings_strict.json file."""
    import argparse
    p = argparse.ArgumentParser(description="Phase 13 reality filter smoke test")
    p.add_argument("--canonical", help="Score an existing canonical JSON file")
    p.add_argument("--l4-default", choices=["DROP", "KEEP"], default="DROP",
                   help="How to resolve DEFER_L4 cases when Haiku is off")
    args = p.parse_args(argv)

    if args.canonical:
        return _score_canonical(args.canonical, args.l4_default)
    return _run_fixture()


# Each fixture: (building_dict, multi_source, arch_verified, expected_decision, why)
# `building_dict` includes optional struct meta (year/country) so L3 can pass.
_FIXTURE_CASES = [
    # ---- L2 positive-override cases (noun present, negative pattern in name) ----
    ({"name": "LG Corporation Headquarters", "year": 2024, "country": "South Korea"},
     False, True, "KEEP", "Headquarters noun + L3 struct (year+country)"),
    ({"name": "China Pavilion at the Biennale di Venezia 2025 by Ma Yansong",
      "year": 2025, "country": "Italy"},
     False, True, "KEEP", "Pavilion noun overrides Biennale keyword"),
    ({"name": "Foster + Partners wins competition for new Library",
      "year": 2024, "country": "UK"},
     False, True, "KEEP", "Library noun overrides 'wins' verb"),
    ({"name": "Foster + Partners' new headquarters in London",
      "year": 2024, "country": "UK"},
     False, True, "KEEP", "headquarters noun, sentence-style ok"),

    # ---- L2 negative cases (no positive override) ----
    ({"name": "Architecture Biennale 2024 announces participants",
      "year": 2024, "country": "Italy"},
     False, True, "DROP", "no building noun + announces + Biennale"),
    ({"name": "Norman Foster: A Retrospective", "year": 2024},
     False, True, "DROP", "Retrospective interview-style, no noun"),
    ({"name": "Interview with David Chipperfield", "year": 2023},
     False, True, "DROP", "Interview keyword, no noun"),
    ({"name": "Pritzker Prize 2025 awarded to Riken Yamamoto", "year": 2025},
     False, True, "DROP", "no noun, awarded verb + Prize keyword"),

    # ---- L3 deferral (no struct meta, no negative pattern, has arch) ----
    ({"name": "Casa Lucernas"},  # no struct meta, deferred to L4
     False, True, "DROP", "no L2 hit but no struct signal → DEFER_L4 → DROP default"),
    ({"name": "Crystal Palace by Foster"},
     False, True, "DROP", "DEFER_L4 → DROP default (real building, missing meta)"),

    # ---- L1 cross-source ----
    ({"name": "MVRDV Office Tower"},
     True, True, "KEEP", "multi-source confirmed → T1"),

    # ---- L3 source-specific signal (Architizer) ----
    ({"name": "Some Project", "constr_status": "built", "categories": ["Office"]},
     False, True, "KEEP", "Architizer constr_status + categories → T2"),

    # ---- L3 source-specific signal (Archello) ----
    ({"name": "Some Project", "category": "Private Houses"},
     False, True, "KEEP", "Archello category populated → T2"),

    # ---- No architect link ----
    ({"name": "House", "year": 2024},
     False, False, "DROP", "no architect_verified → L3 fails → DROP"),
]


def _run_fixture() -> int:
    print("=== Reality filter fixture test ===\n")
    passed = 0
    failed = 0
    for (building, multi, arch, expected, why) in _FIXTURE_CASES:
        decision, tier, reason = reality_filter_with_default_l4(
            building,
            multi_source_confirmed=multi,
            architect_verified=arch,
            l4_default="DROP",
        )
        ok = decision == expected
        marker = "✓" if ok else "✗"
        name = building.get("name", "?")
        print(f"  {marker} {name!r:75s}")
        print(f"      expected={expected:5s}  got={decision:5s} ({tier}) reason={reason}")
        print(f"      why: {why}")
        if not ok:
            failed += 1
        else:
            passed += 1
        print()
    print(f"Passed {passed}/{len(_FIXTURE_CASES)}")
    return 0 if failed == 0 else 1


def _score_canonical(path: str, l4_default: str) -> int:
    from collections import Counter
    print(f"=== Scoring {path} (l4_default={l4_default}) ===\n")
    with open(path) as f:
        rows = json.load(f)
    decisions = Counter()
    tiers = Counter()
    for r in rows:
        # Multi-source proxy: row has divisare_id (matched into Divisare)
        # Arch verified proxy: row has architect_canonical_ids
        decision, tier, reason = reality_filter_with_default_l4(
            r,
            multi_source_confirmed=bool(r.get("divisare_id")),
            architect_verified=bool(r.get("architect_canonical_ids")),
            l4_default=l4_default,
        )
        decisions[decision] += 1
        if tier:
            tiers[tier] += 1
    print(f"Total rows: {len(rows)}")
    print("\nDecisions:")
    for d, n in decisions.most_common():
        print(f"  {d:6s}: {n:5d}")
    print("\nTiers (KEEPs only):")
    for t, n in sorted(tiers.items()):
        print(f"  {t}: {n:5d}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
