#!/usr/bin/env python3
"""Build the completeness_c11 artifact — fine-grained taxonomy backfill.

Reads the C10 recovery artifact and, for every canonical row, reverse-joins
`source_refs` to the crawl DBs to recover the source-native taxonomy that
`build_strict_canonical.py` dropped. Fills four new fields:

  source_categories      {source: [raw tag, ...]}   raw, lossless
  typology_tags          [TYPOLOGY term, ...]        crosswalk-mapped union
  typology_primary       one TYPOLOGY term           most-tagged, priority tie-break
  architectural_elements [ELEMENT term, ...]         crosswalk-mapped union

Every row gains the four fields (>= empty), so the whole artifact is the
upsert payload — there is no affected-only subset. Streaming, strict artifact
only (embeddings are regenerated downstream). Read-only w.r.t. Neon.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import vocab  # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C10 = CCR / "canonical_buildings_strict.completeness_c10_recovery.json"
CROSSWALK = ROOT / "canonical/typology_crosswalk.json"
CRAWL = ROOT / "data" / "crawl"
OUT = CCR / "canonical_buildings_strict.completeness_c11_taxonomy.json"
REPORT = ROOT / "data/reports/canonical_v2_completeness_c11_apply_report.json"

# typology_primary tie-break: most-tagged term wins; ties resolve to the most
# informative type (generic residential terms last).
_TYPOLOGY_PRIORITY = [
    "Library", "Museum", "Gallery", "Theatre", "Concert Hall",
    "School", "University", "Kindergarten",
    "Hospital", "Care Home", "Religious Building",
    "Stadium", "Sports Centre", "Pavilion",
    "Airport", "Train Station", "Bridge",
    "Winery", "Industrial", "Warehouse",
    "Hotel", "Restaurant", "Shopping Centre", "Retail", "Office", "Bank",
    "Civic Building", "Memorial", "Park", "Car Park",
    "Student Housing", "House", "Apartment", "Housing", "Mixed Use",
]
_PRIO_IDX = {t: i for i, t in enumerate(_TYPOLOGY_PRIORITY)}

# Fallback chain for rows whose source tags yield no typology: an unambiguous
# institution word in the name, then a program value that maps cleanly to one
# typology. Provenance is recorded in typology_primary_source.
_NAME_RULES = [
    (re.compile(r"kindergarten|\bnurser|pre-?school", re.I), "Kindergarten"),
    (re.compile(r"universit|\bcollege\b", re.I), "University"),
    (re.compile(r"\bschool\b", re.I), "School"),
    (re.compile(r"\blibrar|bibliote|mediathe|bibliothek", re.I), "Library"),
    (re.compile(r"\bmuseum|\bmuseo\b|mus[ée]e|muzeum", re.I), "Museum"),
    (re.compile(r"\bgaller|galer[íi]a|galerie", re.I), "Gallery"),
    (re.compile(r"cathedral|basilica|\bchurch|\bchapel|mosque|synagogue|"
                r"\btemple\b|monaster|convent", re.I), "Religious Building"),
    (re.compile(r"\bhospital|\bclinic", re.I), "Hospital"),
    (re.compile(r"\bstadium\b|\barena\b", re.I), "Stadium"),
    (re.compile(r"\bairport\b", re.I), "Airport"),
    (re.compile(r"train station|railway station", re.I), "Train Station"),
    (re.compile(r"town hall|city hall", re.I), "Civic Building"),
    (re.compile(r"\bwinery\b|vineyard|bodega", re.I), "Winery"),
]
# program -> typology, only where the program value maps to exactly one type
_PROGRAM_FALLBACK = {
    "Office": "Office", "Museum": "Museum", "Religion": "Religious Building",
    "Sports": "Sports Centre", "Healthcare": "Hospital", "Mixed Use": "Mixed Use",
    "Landscape": "Park", "Housing": "Housing",
}


def _name_fallback(name):
    text = str(name or "")
    for rx, term in _NAME_RULES:
        if rx.search(text):
            return term
    return None


def _program_fallback(program):
    return _PROGRAM_FALLBACK.get(program)


def _load_source_tags() -> dict:
    """{(source, str(id)): [raw tag, ...]} for divisare / architizer / archello.

    Metalocus is skipped — its buildings.building_type is effectively empty
    (1 of 7,378 rows) so it carries no taxonomy signal.
    """
    out: dict = {}

    def _json_tags(db_file, table, col, source):
        conn = sqlite3.connect(str(CRAWL / db_file))
        try:
            for sid, raw in conn.execute(f"SELECT id, {col} FROM {table}"):
                try:
                    tags = json.loads(raw) if raw else []
                except (json.JSONDecodeError, TypeError):
                    tags = []
                if isinstance(tags, list):
                    clean = [str(t).strip() for t in tags if t and str(t).strip()]
                    if clean:
                        out[(source, str(sid))] = clean
        finally:
            conn.close()

    _json_tags("divisare.db", "divisare_projects", "tag_slugs", "divisare")
    _json_tags("architizer.db", "architizer_projects", "categories", "architizer")

    conn = sqlite3.connect(str(CRAWL / "archello.db"))
    try:
        for sid, cat in conn.execute("SELECT id, category FROM archello_projects"):
            if cat and str(cat).strip():
                out[("archello", str(sid))] = [str(cat).strip()]
    finally:
        conn.close()
    return out


def _pick_primary(counter: Counter):
    if not counter:
        return None
    return max(counter, key=lambda t: (counter[t], -_PRIO_IDX.get(t, 999)))


def main() -> int:
    for path in (C10, CROSSWALK):
        if not path.exists():
            print(f"FATAL: missing input: {path}", file=sys.stderr)
            return 2

    crosswalk = json.loads(CROSSWALK.read_text(encoding="utf-8"))
    source_tags = _load_source_tags()

    counts = Counter()
    primary_dist: Counter = Counter()
    prov_dist: Counter = Counter()
    n_in = 0
    f = OUT.open("w", encoding="utf-8")
    f.write('{"buildings":[')
    try:
        for row in iter_buildings(C10):
            refs = row.get("source_refs") or {}
            src_cats: dict = {}
            typ_counter: Counter = Counter()
            elements: set = set()
            for source, sids in refs.items():
                raw: list = []
                for sid in sids or []:
                    raw.extend(source_tags.get((source, str(sid))) or [])
                if raw:
                    src_cats[source] = sorted(set(raw))
                cwmap = crosswalk.get(source) or {}
                for tag in raw:
                    for term in cwmap.get(tag) or []:
                        if term in vocab.TYPOLOGY:
                            typ_counter[term] += 1
                        elif term in vocab.ARCHITECTURAL_ELEMENT:
                            elements.add(term)

            row["source_categories"] = src_cats
            row["architectural_elements"] = sorted(elements)
            if typ_counter:
                typ_tags = sorted(typ_counter)
                primary = _pick_primary(typ_counter)
                prov = "source_tags"
            else:
                name_fb = _name_fallback(row.get("name"))
                prog_fb = _program_fallback(row.get("program"))
                if name_fb:
                    typ_tags, primary, prov = [name_fb], name_fb, "name"
                elif prog_fb:
                    typ_tags, primary, prov = [prog_fb], prog_fb, "program"
                else:
                    typ_tags, primary, prov = [], None, None
            row["typology_tags"] = typ_tags
            row["typology_primary"] = primary
            row["typology_primary_source"] = prov

            counts["rows_total"] += 1
            if src_cats:
                counts["rows_with_source_categories"] += 1
            if typ_tags:
                counts["rows_with_typology"] += 1
            if elements:
                counts["rows_with_elements"] += 1
            if primary:
                counts["rows_with_typology_primary"] += 1
                primary_dist[primary] += 1
                prov_dist[prov] += 1
            elif row.get("is_publishable"):
                counts["publishable_without_typology"] += 1

            f.write(("," if n_in else "") + json.dumps(row, ensure_ascii=False))
            n_in += 1
    finally:
        f.write("]}")
        f.close()

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C10.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_total": n_in,
        "counts": dict(counts),
        "typology_primary_distribution": dict(primary_dist.most_common()),
        "typology_primary_source_distribution": dict(prov_dist),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"C11 taxonomy backfill -> {OUT.relative_to(ROOT)}")
    print(json.dumps({"rows_total": n_in, "counts": dict(counts),
                      "primary_source": dict(prov_dist)},
                     ensure_ascii=False, indent=2))
    print("typology_primary top: " + ", ".join(
        f"{t}:{c:,}" for t, c in primary_dist.most_common(15)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
