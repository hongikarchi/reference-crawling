#!/usr/bin/env python3
"""Build the completeness_c12 artifact — taxonomy backfill + placeholder strip.

Supersedes the defective C11 (Codex data-QA: hospitality->Hospital crosswalk
bug, placeholder cover images, feature-tag-dominated typology_primary). Reads
the C10 recovery artifact and, per canonical row:

  1. Strips placeholder image URLs (facebook-default-thumb / img-placeholder)
     from all_images / covers_by_type / cover_image_url_default /
     display_cover_url; re-derives the display cover. A row left with no real
     image becomes non-publishable (publishability_reasons += image_unavailable).
  2. Reverse-joins source_refs to the crawl DBs, maps source tags through the
     typology crosswalk, and fills source_categories / typology_tags /
     typology_primary / typology_primary_source / architectural_elements.
     Context typologies (Park / Pavilion / Mixed Use / Memorial) never bury a
     specific typology when picking typology_primary.

Streaming, strict artifact only (embeddings regenerated downstream).
Read-only w.r.t. Neon.
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
from tools.build_strict_canonical import _display_cover_url  # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C10 = CCR / "canonical_buildings_strict.completeness_c10_recovery.json"
CROSSWALK = ROOT / "canonical/typology_crosswalk.json"
CRAWL = ROOT / "data" / "crawl"
OUT = CCR / "canonical_buildings_strict.completeness_c12_taxonomy.json"
REPORT = ROOT / "data/reports/canonical_v2_completeness_c12_apply_report.json"

# placeholder image URL fragments — extend here when a new one is found
_PLACEHOLDER_PATTERNS = ("facebook-default-thumb", "img-placeholder")

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

# context / catch-all typologies — a specific typology outranks these in
# typology_primary even with fewer tags, so landscape feature-tags cannot bury
# a building's real use (e.g. a transit terminal with a planted roof).
_CONTEXT_TYPOLOGIES = {"Park", "Pavilion", "Mixed Use", "Memorial"}

# 2026-Q2 audit fold (job 20260615_audit_remediation_F1_F3): incidental room/feature/
# umbrella/former-use source tags must NOT yield a building typology (e.g. dining-rooms
# -> Restaurant on a house). Skip them when building the typology counter.
_DEMOTE_TAGS = {
    "divisare": {"dining-rooms", "canteens", "kitchens", "bedrooms", "living-rooms",
                 "bathrooms", "home-offices", "offices-and-studios", "car-parks",
                 "pet-houses", "art-studios-and-workshops",
                 "post-industrial-architecture", "post-industrial-interiors"},
    "architizer": {"Hospitality + Sport", "Government + Health"},
    "archello": set(), "metalocus": set(),
}
# ancillary typologies lose a count-tie to any genuine primary-use type (tie-break fix).
_ANCILLARY = {"Park", "Pavilion", "Mixed Use", "Memorial", "Car Park",
              "Civic Building", "Office", "Restaurant"}
# remediation overrides keyed by stable canonical_bld_id; applied verbatim so a rebuild
# preserves the 2026-Q2 LLM typology re-derivation + material cleanup. CHANGED-ONLY
# (only the ~20.8k rows actually remediated) so future re-enrichment of OTHER rows is
# NOT frozen — new/unchanged rows still get the improved crosswalk (_DEMOTE + tie-break).
_OVERRIDES_PATH = ROOT / "data/canonical/remediation_typmat_changed_rem2026q2.jsonl"

# Fallback chain for rows whose source tags yield no typology: an unambiguous
# institution word in the name, then a program value that maps to one typology.
# Regexes are word-bounded so "hospital" never matches "hospitality".
_NAME_RULES = [
    (re.compile(r"\bkindergartens?\b|\bnurser(?:y|ies)\b|\bpre-?schools?\b", re.I),
     "Kindergarten"),
    (re.compile(r"\buniversit\w*\b|\buniversidad\b|\bcollege\b", re.I), "University"),
    (re.compile(r"\bschools?\b", re.I), "School"),
    (re.compile(r"\blibrar(?:y|ies)\b|\bbiblio\w*\b|\bm[ée]diath\w*\b", re.I),
     "Library"),
    (re.compile(r"\bmuseums?\b|\bmuseo\b|\bmus[ée]e\b|\bmuzeum\b", re.I), "Museum"),
    (re.compile(r"\bgaller(?:y|ies)\b|\bgaler[íi]a\b|\bgalerie\b|\bgalleria\b", re.I),
     "Gallery"),
    (re.compile(r"\bcathedrals?\b|\bbasilicas?\b|\bchurch(?:es)?\b|\bchapels?\b|"
                r"\bmosques?\b|\bsynagogues?\b|\btemples?\b|\bmonaster\w*\b|"
                r"\bconvents?\b", re.I), "Religious Building"),
    (re.compile(r"\bhospitals?\b|\bclinics?\b", re.I), "Hospital"),
    (re.compile(r"\bstadiums?\b|\barenas?\b", re.I), "Stadium"),
    (re.compile(r"\bairports?\b", re.I), "Airport"),
    (re.compile(r"\btrain station\b|\brailway station\b|\bterminals?\b", re.I),
     "Train Station"),
    (re.compile(r"\btown hall\b|\bcity hall\b", re.I), "Civic Building"),
    (re.compile(r"\bwiner(?:y|ies)\b|\bvineyards?\b|\bbodegas?\b", re.I), "Winery"),
]
# program -> typology, only where the program value maps to exactly one type
_PROGRAM_FALLBACK = {
    "Office": "Office", "Museum": "Museum", "Religion": "Religious Building",
    "Sports": "Sports Centre", "Healthcare": "Hospital", "Mixed Use": "Mixed Use",
    "Landscape": "Park", "Housing": "Housing",
}


def _is_placeholder(url) -> bool:
    u = str(url or "")
    return any(p in u for p in _PLACEHOLDER_PATTERNS)


def _img_url(image):
    return image.get("url") if isinstance(image, dict) else image


def _strip_placeholders(row: dict) -> tuple[bool, bool]:
    """Remove placeholder image URLs and re-derive the display cover.

    Returns (stripped, lost_last_image). `lost_last_image` is True when the
    strip left the row with no real image — it is then marked non-publishable.
    """
    imgs = row.get("all_images") or []
    cbt = row.get("covers_by_type")
    cbt = cbt if isinstance(cbt, dict) else {}
    bipc = row.get("best_image_per_cluster")
    bipc = bipc if isinstance(bipc, dict) else {}
    has_ph = (
        any(_is_placeholder(_img_url(im)) for im in imgs)
        or any(_is_placeholder(v) for v in cbt.values())
        or any(_is_placeholder(_img_url(v)) for v in bipc.values())
        or _is_placeholder(row.get("cover_image_url_default"))
        or _is_placeholder(row.get("display_cover_url"))
    )
    if not has_ph:
        return False, False

    kept = [im for im in imgs if not _is_placeholder(_img_url(im))]
    row["all_images"] = kept
    cbt = {k: (None if _is_placeholder(v) else v) for k, v in cbt.items()}
    row["covers_by_type"] = cbt
    row["best_image_per_cluster"] = {k: v for k, v in bipc.items()
                                     if not _is_placeholder(_img_url(v))}
    if _is_placeholder(row.get("cover_image_url_default")):
        row["cover_image_url_default"] = None
    new_dcu = _display_cover_url(
        covers_by_type=cbt,
        cover_image_url_default=row.get("cover_image_url_default"),
        all_images=kept,
    )
    row["display_cover_url"] = new_dcu
    if not new_dcu:
        reasons = list(row.get("publishability_reasons") or [])
        if "image_unavailable" not in reasons:
            reasons.append("image_unavailable")
        row["publishability_reasons"] = reasons
        row["is_publishable"] = False
        return True, True
    return True, False


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
    """Most-tagged typology; a specific type always outranks a context type
    (Park/Pavilion/Mixed Use/Memorial) regardless of tag count. On a count-tie,
    a genuine primary-use type beats an ancillary one (2026-Q2 tie-break fix), so a
    house tagged with one incidental ancillary tag no longer resolves to the ancillary."""
    if not counter:
        return None
    specifics = {t: c for t, c in counter.items() if t not in _CONTEXT_TYPOLOGIES}
    pool = specifics or counter
    return max(pool, key=lambda t: (pool[t], t not in _ANCILLARY, -_PRIO_IDX.get(t, 999)))


def _load_overrides() -> dict:
    """{canonical_bld_id: {typology_primary, typology_primary_source, typology_tags,
    material_visual, architectural_elements}} from the 2026-Q2 remediation sidecar."""
    if not _OVERRIDES_PATH.exists():
        return {}
    out = {}
    with _OVERRIDES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                out[r["id"]] = r
    return out


def main() -> int:
    for path in (C10, CROSSWALK):
        if not path.exists():
            print(f"FATAL: missing input: {path}", file=sys.stderr)
            return 2

    crosswalk = json.loads(CROSSWALK.read_text(encoding="utf-8"))
    source_tags = _load_source_tags()
    overrides = _load_overrides()  # 2026-Q2 remediation: authoritative per-id overrides

    counts = Counter()
    primary_dist: Counter = Counter()
    prov_dist: Counter = Counter()
    n_in = 0
    f = OUT.open("w", encoding="utf-8")
    f.write('{"buildings":[')
    try:
        for row in iter_buildings(C10):
            stripped, lost = _strip_placeholders(row)
            if stripped:
                counts["placeholder_stripped"] += 1
            if lost:
                counts["placeholder_now_unpublishable"] += 1

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
                dem = _DEMOTE_TAGS.get(source, set())
                for tag in raw:
                    if tag in dem:  # 2026-Q2: incidental/umbrella tags yield no typology
                        continue
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

            # 2026-Q2 remediation override (authoritative for existing ids): replaces the
            # crosswalk-derived typology + material with the committed LLM/cleaned values.
            ov = overrides.get(row.get("canonical_bld_id"))
            if ov:
                row["typology_primary"] = ov["typology_primary"]
                row["typology_primary_source"] = ov["typology_primary_source"]
                row["typology_tags"] = ov["typology_tags"]
                row["material_visual"] = ov["material_visual"]
                row["architectural_elements"] = ov["architectural_elements"]
                primary = ov["typology_primary"]
                typ_tags = ov["typology_tags"]
                prov = ov["typology_primary_source"]
                elements = set(ov["architectural_elements"])
                counts["override_applied"] += 1

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

    print(f"C12 taxonomy backfill -> {OUT.relative_to(ROOT)}")
    print(json.dumps({"rows_total": n_in, "counts": dict(counts),
                      "primary_source": dict(prov_dist)},
                     ensure_ascii=False, indent=2))
    print("typology_primary top: " + ", ".join(
        f"{t}:{c:,}" for t, c in primary_dist.most_common(15)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
