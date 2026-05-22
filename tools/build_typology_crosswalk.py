#!/usr/bin/env python3
"""Build canonical/typology_crosswalk.json — source tag -> v2 vocab mapping.

Reads the taxonomy tag inventory and maps every source-native taxonomy tag
(divisare tag_slugs, architizer/archello categories) onto core.vocab TYPOLOGY
and ARCHITECTURAL_ELEMENT terms with a deterministic keyword ruleset. A tag may
map to several terms (archello concatenates categories, e.g. "Bars
Restaurants"); unmatched tags map to [] (drop).

Output: canonical/typology_crosswalk.json = {source: {raw_tag: [vocab_term...]}}.
The crosswalk is a reviewable, hand-editable artifact — the C11 taxonomy build
consumes the JSON, not this ruleset. Read-only w.r.t. crawl DBs / Neon.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import vocab  # noqa: E402

INVENTORY = ROOT / "data/reports/taxonomy_tag_inventory.json"
OUT = ROOT / "canonical/typology_crosswalk.json"
REPORT = ROOT / "data/reports/typology_crosswalk_coverage.json"

# (vocab term, [keyword, ...]). A keyword matches a tag when every space-
# separated word in it appears as a WHOLE token of the normalized tag (regular
# plurals handled) — exact, not prefix, so "hospital" matches "hospital" /
# "hospitals" but never "hospitality". Keywords are full words, not stems.
RULES: list[tuple[str, list[str]]] = [
    # --- architectural elements ---
    ("Stair", ["stair"]),
    ("Facade", ["facade", "cladding", "curtain wall"]),
    ("Roof", ["roof", "rooftop"]),
    ("Courtyard", ["courtyard", "patio"]),
    ("Entrance", ["entrance", "lobby", "foyer", "vestibule"]),
    ("Corridor", ["corridor", "hallway", "passage"]),
    ("Atrium", ["atrium", "atria"]),
    ("Terrace", ["terrace"]),
    ("Balcony", ["balcony", "loggia"]),
    ("Garden", ["garden"]),
    ("Fireplace", ["fireplace", "hearth"]),
    ("Column", ["column", "colonnade"]),
    ("Canopy", ["canopy", "awning"]),
    ("Skylight", ["skylight"]),
    # --- typology ---
    ("Kindergarten", ["kindergarten", "preschool", "nursery", "daycare", "creche"]),
    ("School", ["school", "classroom"]),
    ("University", ["university", "college", "campus"]),
    ("Library", ["library", "mediatheque"]),
    ("Museum", ["museum"]),
    ("Gallery", ["gallery", "exhibition"]),
    ("Concert Hall", ["concert", "philharmonic"]),
    ("Theatre", ["theatre", "theater", "cinema", "auditorium", "playhouse",
                 "opera", "performing arts"]),
    ("Hospital", ["hospital", "clinic", "healthcare", "infirmary", "polyclinic"]),
    ("Care Home", ["care home", "nursing home", "hospice", "retirement", "elderly"]),
    ("Religious Building", ["church", "chapel", "cathedral", "mosque", "synagogue",
                            "temple", "shrine", "monastery", "basilica", "religious",
                            "convent"]),
    ("Hotel", ["hotel", "hostel", "resort", "motel", "hospitality"]),
    ("Restaurant", ["restaurant", "cafe", "bistro", "canteen", "brasserie", "bar",
                    "dining"]),
    ("Retail", ["shop", "store", "retail", "showroom", "boutique", "kiosk", "market"]),
    ("Shopping Centre", ["shopping", "mall"]),
    ("Office", ["office", "headquarter", "coworking", "co working", "workspace",
                "research center"]),
    ("Bank", ["bank"]),
    ("Civic Building", ["civic", "town hall", "city hall", "courthouse",
                        "court house", "administrative", "government", "embassy",
                        "parliament", "police", "fire station", "post office",
                        "cultural center", "visitor center", "social center",
                        "community center"]),
    ("Sports Centre", ["sport", "gym", "swimming", "fitness", "athletic"]),
    ("Stadium", ["stadium", "arena"]),
    ("Pavilion", ["pavilion"]),
    ("Airport", ["airport"]),
    ("Train Station", ["train station", "railway", "metro station", "bus station",
                       "bus stop", "transit"]),
    ("Car Park", ["car park", "parking", "garage"]),
    ("Industrial", ["industrial", "factory", "manufacturing", "workshop"]),
    ("Warehouse", ["warehouse", "storage", "depot", "logistic"]),
    ("Winery", ["winery", "wine cellar", "brewery", "distillery", "vineyard"]),
    ("House", ["house", "home", "villa", "dwelling", "cottage", "cabin",
               "bungalow", "chalet"]),
    ("Apartment", ["apartment", "flat", "condo", "multifamily", "multi family",
                   "multi unit"]),
    ("Housing", ["housing", "residential", "tenement"]),
    ("Student Housing", ["student housing", "dormitory", "halls of residence"]),
    ("Park", ["park", "landscape", "plaza", "playground", "promenade",
              "waterfront", "public space", "square"]),
    ("Memorial", ["memorial", "monument", "cemetery", "crematorium", "mausoleum"]),
    ("Bridge", ["bridge", "viaduct", "footbridge"]),
    ("Mixed Use", ["mixed use", "multi use"]),
]

# when the key term matches, drop the listed (less specific) terms
SUPPRESS: dict[str, list[str]] = {
    "Kindergarten": ["School"],
    "Car Park": ["Park"],
    "Shopping Centre": ["Retail"],
    "Student Housing": ["Housing", "House"],
    "Care Home": ["House", "Housing"],
    "Winery": ["Industrial"],
    "Memorial": ["Park"],
    "Park": ["Garden"],
}


def _tokens(slug: str) -> list[str]:
    text = re.sub(r"[-_/]+", " ", str(slug).strip().lower())
    return text.replace("centre", "center").split()


def _word_forms(word: str) -> set[str]:
    """A keyword word plus its regular English plural forms."""
    forms = {word, word + "s", word + "es"}
    if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
        forms.add(word[:-1] + "ies")
    return forms


def _kw_match(token_set: set[str], keyword: str) -> bool:
    """True when every word of the keyword (or a regular plural of it) is a
    whole token — exact, not prefix, so 'hospital' never matches 'hospitality'."""
    return all(_word_forms(w) & token_set for w in keyword.split())


def map_tag(slug: str) -> list[str]:
    token_set = set(_tokens(slug))
    if not token_set:
        return []
    matched = {term for term, kws in RULES
               if any(_kw_match(token_set, kw) for kw in kws)}
    for trigger, drop in SUPPRESS.items():
        if trigger in matched:
            matched -= set(drop)
    return sorted(matched)


def main() -> int:
    if not INVENTORY.exists():
        print(f"FATAL: inventory missing: {INVENTORY} — run taxonomy_tag_inventory first",
              file=sys.stderr)
        return 2
    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))

    crosswalk: dict = {}
    report: dict = {"generated": datetime.now(timezone.utc).isoformat(), "sources": {}}
    for source, sdata in inv["sources"].items():
        mapping: dict = {}
        tags_total = tags_mapped = 0
        assign_total = assign_mapped = 0
        term_counts: Counter = Counter()
        for t in sdata["tags"]:
            slug, count = t["slug"], t["count"]
            terms = map_tag(slug)
            mapping[slug] = terms
            tags_total += 1
            assign_total += count
            if terms:
                tags_mapped += 1
                assign_mapped += count
                for term in terms:
                    term_counts[term] += count
        crosswalk[source] = mapping
        report["sources"][source] = {
            "tags_total": tags_total,
            "tags_mapped": tags_mapped,
            "tags_mapped_pct": round(tags_mapped / tags_total, 4) if tags_total else 0,
            "assignments_total": assign_total,
            "assignments_mapped": assign_mapped,
            "assignments_mapped_pct": round(assign_mapped / assign_total, 4) if assign_total else 0,
            "top_terms": term_counts.most_common(20),
        }

    # sanity: every mapped term must be a real vocab value
    bad = sorted({term for m in crosswalk.values() for terms in m.values()
                  for term in terms
                  if term not in vocab.TYPOLOGY and term not in vocab.ARCHITECTURAL_ELEMENT})
    if bad:
        print(f"FATAL: crosswalk produced non-vocab terms: {bad}", file=sys.stderr)
        return 1

    OUT.write_text(json.dumps(crosswalk, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"typology crosswalk -> {OUT.relative_to(ROOT)}")
    for source, r in report["sources"].items():
        print(f"\n=== {source} ===")
        print(f"  tags {r['tags_mapped']:,}/{r['tags_total']:,} mapped "
              f"({r['tags_mapped_pct']:.0%}) | "
              f"assignments {r['assignments_mapped']:,}/{r['assignments_total']:,} "
              f"({r['assignments_mapped_pct']:.0%})")
        print(f"  top terms: {', '.join(f'{t}:{c:,}' for t, c in r['top_terms'][:10])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
