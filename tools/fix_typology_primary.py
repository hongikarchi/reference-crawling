#!/usr/bin/env python3
"""F1 remediation: re-derive typology_primary with a fixed crosswalk + tie-break.

Root cause (audit 2026-06-14): the C11 crosswalk maps incidental room/feature tags
and umbrella parent-categories to building typologies (dining-rooms->Restaurant,
home-offices->[House,Office], "Hospitality + Sport"->[Hotel,Sports Centre],
post-industrial-*->Industrial), and `_pick_primary`'s priority list buries
residential so a tie between an incidental type and the real (residential) type
resolves to the incidental one.

Fix = two parts, both validated against the 200 ground-truth LLM verdicts
(audit_s1_verdicts.json):
  1. DEMOTE a set of incidental/umbrella/former-use tags -> contribute no typology.
  2. Tie-break: when typology counts tie, prefer a "primary-use" type over an
     ancillary one (ANCILLARY set), instead of the old informativeness ordering.

Read-only: re-derives from live Neon `source_categories` (which stores the raw
per-source tags), writes a proposed-corrections artifact + validation report.
NO Neon write. NO core/vocab.py edit. The crosswalk file itself is NOT mutated here
— the demotion is applied in-memory so the change is reviewable before committing.

Usage:
  python3 tools/fix_typology_primary.py --validate         # score vs 200 verdicts
  python3 tools/fix_typology_primary.py --emit-corrections data/reports/typology_corrections.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2.extras  # noqa: E402
from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.build_completeness_c11_taxonomy import _name_fallback, _program_fallback  # noqa: E402
from core import vocab  # noqa: E402

# residential family treated as one class for lenient validation (House vs Housing)
_RESID = {"House", "Apartment", "Housing", "Student Housing"}


def _eq(a, b):
    if a == b:
        return True
    return a in _RESID and b in _RESID

CROSSWALK = ROOT / "canonical/typology_crosswalk.json"
VERDICTS = ROOT / "data/reports/audit_s1_verdicts.json"

# (1) tags that must NOT yield a building typology — rooms/features, umbrella
# parent-categories, and "former-use" (reuse) tags. Derived from the audit's
# culprit analysis + the principle: interior/feature/umbrella != building use.
DEMOTE = {
    "divisare": {
        "dining-rooms", "canteens", "kitchens", "bedrooms", "living-rooms",
        "bathrooms", "home-offices", "offices-and-studios", "car-parks",
        "pet-houses", "art-studios-and-workshops",
        "post-industrial-architecture", "post-industrial-interiors",
    },
    "architizer": {"Hospitality + Sport", "Government + Health"},
    "archello": set(),
    "metalocus": set(),
}

# (2) ancillary typologies: lose a count-tie to any genuine primary-use type.
ANCILLARY = {"Park", "Pavilion", "Mixed Use", "Memorial", "Car Park",
             "Civic Building", "Office", "Restaurant"}

# old priority (informativeness) kept only as a final deterministic tie-break
_PRIO = ["Library", "Museum", "Gallery", "Theatre", "Concert Hall", "School",
         "University", "Kindergarten", "Hospital", "Care Home", "Religious Building",
         "Stadium", "Sports Centre", "Pavilion", "Airport", "Train Station", "Bridge",
         "Winery", "Industrial", "Warehouse", "Hotel", "Restaurant", "Shopping Centre",
         "Retail", "Office", "Bank", "Civic Building", "Memorial", "Park", "Car Park",
         "Student Housing", "House", "Apartment", "Housing", "Mixed Use"]
_PRIO_IDX = {t: i for i, t in enumerate(_PRIO)}
_CONTEXT = {"Park", "Pavilion", "Mixed Use", "Memorial"}


def pick_primary(counter: Counter):
    """Fixed pick: drop context types if a specific exists; among the rest prefer
    higher count, then a primary-use type over an ancillary one, then _PRIO."""
    if not counter:
        return None
    specifics = {t: c for t, c in counter.items() if t not in _CONTEXT}
    pool = specifics or counter
    return max(pool, key=lambda t: (pool[t], t not in ANCILLARY, -_PRIO_IDX.get(t, 999)))


def rederive(crosswalk, source_categories, demote):
    typ = Counter()
    for source, tags in (source_categories or {}).items():
        cw = crosswalk.get(source) or {}
        dem = demote.get(source, set())
        for t in tags or []:
            if t in dem:
                continue
            for term in cw.get(t) or []:
                if term in vocab.TYPOLOGY:
                    typ[term] += 1
    return pick_primary(typ), typ


def load_rows(cur):
    cur.execute("""SELECT canonical_bld_id id, typology_primary cur_typ,
        typology_primary_source src, source_categories sc, name, program
        FROM canonical_v2_buildings""")
    return [dict(r) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--emit-corrections", default=None)
    args = ap.parse_args()

    crosswalk = json.loads(CROSSWALK.read_text())
    conn = _connect(); conn.set_session(readonly=True)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        rows = load_rows(cur)
    conn.close()

    new = {}
    for r in rows:
        # only re-derive where the current value came from source_tags
        if r["src"] == "source_tags":
            nt, cnt = rederive(crosswalk, r["sc"], DEMOTE)
            if nt is None:  # demotion emptied the tags -> name/program fallback
                nt = _name_fallback(r["name"]) or _program_fallback(r["program"])
            new[r["id"]] = nt
        else:
            new[r["id"]] = r["cur_typ"]
    cur_map = {r["id"]: r["cur_typ"] for r in rows}
    changed = {i: (cur_map[i], new[i]) for i in new if new[i] != cur_map[i]}

    out = {"rows": len(rows), "source_tag_rows": sum(1 for r in rows if r["src"] == "source_tags"),
           "changed": len(changed)}

    if args.validate and VERDICTS.exists():
        verds = json.load(open(VERDICTS))["result"]["verdicts"]
        # the 200 sample rows used compact args; suggested_typ is the target for
        # typology_wrong; for both_ok the old value should be preserved.
        agree_typwrong = total_typwrong = 0
        broke_ok = total_ok = 0
        for v in verds:
            i = v["id"]
            if i not in new:
                continue
            if v["verdict"] in ("typology_wrong",) and v.get("suggested_typ"):
                total_typwrong += 1
                if _eq(new[i], v["suggested_typ"]):
                    agree_typwrong += 1
            if v["verdict"] == "both_ok":
                total_ok += 1
                if new[i] != cur_map[i]:
                    broke_ok += 1
        out["validation"] = {
            "typology_wrong_with_suggestion": total_typwrong,
            "now_match_suggestion": agree_typwrong,
            "agreement_pct": round(100 * agree_typwrong / total_typwrong, 1) if total_typwrong else None,
            "both_ok_n": total_ok, "both_ok_broken": broke_ok,
        }
        # how many of the 200 contradictions are resolved (typ no longer the wrong value)
        resolved = sum(1 for v in verds if v["id"] in changed)
        out["validation"]["sample_rows_changed"] = resolved

    if args.emit_corrections:
        corr = [{"id": i, "old": o, "new": n} for i, (o, n) in changed.items()]
        Path(args.emit_corrections).write_text(json.dumps(corr, indent=2, default=str))
        out["corrections_written"] = args.emit_corrections

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
