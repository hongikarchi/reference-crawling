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
            row["typology_tags"] = sorted(typ_counter)
            row["architectural_elements"] = sorted(elements)
            primary = _pick_primary(typ_counter)
            row["typology_primary"] = primary

            counts["rows_total"] += 1
            if src_cats:
                counts["rows_with_source_categories"] += 1
            if typ_counter:
                counts["rows_with_typology"] += 1
            if elements:
                counts["rows_with_elements"] += 1
            if primary:
                counts["rows_with_typology_primary"] += 1
                primary_dist[primary] += 1
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
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"C11 taxonomy backfill -> {OUT.relative_to(ROOT)}")
    print(json.dumps({"rows_total": n_in, "counts": dict(counts)},
                     ensure_ascii=False, indent=2))
    print("typology_primary top: " + ", ".join(
        f"{t}:{c:,}" for t, c in primary_dist.most_common(15)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
