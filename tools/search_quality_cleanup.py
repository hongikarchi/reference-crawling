#!/usr/bin/env python3
"""Deterministic search/facet cleanup for make_web.

Artifact-first, local writes only:

- writes a cleaned canonical artifact
- writes a search_keywords JSONL sidecar
- writes material mapping + before/after report
- never connects to Neon/R2 unless another explicit verification command does
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import vocab  # noqa: E402
from tools.canonical_v2_upload_validator import (  # noqa: E402
    ELEMENT_MAP,
    MATERIAL_TAXONOMY_NOISE,
    iter_buildings,
)

RUN_DATE = datetime.now().strftime("%Y%m%d")
CCR = ROOT / "data/canonical/country_conflict_refresh"
REPORT_DIR = ROOT / "data/reports" / f"search_quality_{RUN_DATE}"
DEFAULT_INPUT = CCR / "canonical_buildings_strict_embedded.completeness_c23_final.json"
C24_REVIEW_QUEUE = REPORT_DIR / "material_unmapped_review.c24.jsonl"
DEFAULT_OUTPUT = CCR / "canonical_buildings_strict_embedded.completeness_c25_search_quality.json"
DEFAULT_KEYWORDS = REPORT_DIR / "canonical_v2_search_keywords.c25.jsonl"
DEFAULT_MATERIAL_REVIEW = REPORT_DIR / "material_unmapped_review.c25.jsonl"
DEFAULT_REPORT = REPORT_DIR / "search_quality_c25_report.json"
DEFAULT_MD = REPORT_DIR / "search_quality_c25_report.md"
DEFAULT_MAPPING = REPORT_DIR / "material_mapping.c25.json"

CONTROLLED_MATERIALS = [
    "concrete",
    "glass",
    "timber",
    "brick",
    "stone",
    "steel",
    "metal",
    "aluminum",
    "copper",
    "corten",
    "plaster",
    "tile",
    "ceramic",
    "terracotta",
    "marble",
    "rammed earth",
    "earth",
    "bamboo",
    "polycarbonate",
    "fabric",
    "masonry",
    "paving",
    "paint",
    "mirror",
    "acrylic",
    "plastic",
    "thatch",
    "clay",
    "zinc",
    "terrazzo",
    "composite",
    "leather",
    "paper",
    "resin",
    "cork",
    "lime",
    "linoleum",
    "rubber",
    "membrane",
    "unspecified",
]

EXACT_MATERIAL_MAP = {
    "wood": "timber",
    "wooden": "timber",
    "timber": "timber",
    "oak": "timber",
    "pine": "timber",
    "plywood": "timber",
    "laminated wood": "timber",
    "glulam": "timber",
    "reinforced concrete": "concrete",
    "exposed concrete": "concrete",
    "precast concrete": "concrete",
    "cast concrete": "concrete",
    "fair-faced concrete": "concrete",
    "raw concrete": "concrete",
    "concrete block": "concrete",
    "bricks": "brick",
    "brickwork": "brick",
    "stonework": "stone",
    "limestone": "stone",
    "granite": "stone",
    "slate": "stone",
    "travertine": "stone",
    "stainless steel": "steel",
    "galvanized steel": "steel",
    "weathering steel": "corten",
    "corten steel": "corten",
    "iron": "metal",
    "metal cladding": "metal",
    "aluminium": "aluminum",
    "aluminum": "aluminum",
    "copper": "copper",
    "zinc": "zinc",
    "ceramics": "ceramic",
    "ceramic tile": "tile",
    "tiles": "tile",
    "terracotta": "terracotta",
    "terra cotta": "terracotta",
    "rammed earth": "rammed earth",
    "earth": "earth",
    "bamboo": "bamboo",
    "polycarbonate": "polycarbonate",
    "fabric": "fabric",
    "textile": "fabric",
    "masonry": "masonry",
    "paving": "paving",
    "painted surfaces": "paint",
    "paint": "paint",
    "mirror": "mirror",
    "mirrors": "mirror",
    "acrylic": "acrylic",
    "plastic": "plastic",
    "thatch": "thatch",
    "clay": "clay",
    "interior finishes": "plaster",
    "brass": "metal",
    "bronze": "metal",
    "terrazzo": "terrazzo",
    "rock": "stone",
    "sandstone": "stone",
    "gravel": "paving",
    "white walls": "plaster",
    "white surfaces": "plaster",
    "composite": "composite",
    "carpet": "fabric",
    "leather": "leather",
    "paper": "paper",
    "resin": "resin",
    "cedar": "timber",
    "walnut": "timber",
    "textiles": "fabric",
    "wallpaper": "paper",
    "pavement": "paving",
    "cast iron": "metal",
    "parquet": "timber",
    "asphalt": "paving",
    "larch": "timber",
    "plasterboard": "plaster",
    "velvet": "fabric",
    "microcement": "concrete",
    "mosaic": "tile",
    "upholstery": "fabric",
    "teak": "timber",
    "mortar": "masonry",
    "clt": "timber",
    "decking": "timber",
    "cork": "cork",
    "membrane": "membrane",
    "shipping containers": "metal",
    "douglas fir": "timber",
    "basalt": "stone",
    "linoleum": "linoleum",
    "cor-ten": "corten",
    "corian": "composite",
    "linen": "fabric",
    "rubber": "rubber",
    "rope": "fabric",
    "adobe": "earth",
    "fiberglass": "composite",
    "lime": "lime",
    "joinery": "timber",
    "built-in cabinetry": "timber",
    "cardboard": "paper",
    "osb": "timber",
    "hardwood": "timber",
    "straw": "thatch",
    "wrought iron": "metal",
    "lacquer": "paint",
    "porcelain": "ceramic",
    "millwork": "timber",
    "carpentry": "timber",
    "gold": "metal",
    "fresco": "paint",
    "mdf": "timber",
    "drywall": "plaster",
    "spruce": "timber",
    "cobblestone": "paving",
    "reeds": "thatch",
    "custom cabinetry": "timber",
}

PATTERN_MATERIAL_MAP = [
    (re.compile(r"\b(glass|glazing|glazed)\b"), "glass"),
    (re.compile(r"\b(concrete|cement)\b"), "concrete"),
    (re.compile(r"\b(timber|wood|wooden|plywood|oak|pine|glulam)\b"), "timber"),
    (re.compile(r"\b(brick|brickwork)\b"), "brick"),
    (re.compile(r"\b(marble|granite|limestone|slate|travertine|stone)\b"), "stone"),
    (re.compile(r"\bcorten\b"), "corten"),
    (re.compile(r"\b(stainless steel|galvanized steel|steel)\b"), "steel"),
    (re.compile(r"\b(aluminium|aluminum)\b"), "aluminum"),
    (re.compile(r"\bcopper\b"), "copper"),
    (re.compile(r"\bzinc\b"), "zinc"),
    (re.compile(r"\bmetal\b"), "metal"),
    (re.compile(r"\b(plaster|stucco|render)\b"), "plaster"),
    (re.compile(r"\b(ceramic|tile|tiles)\b"), "tile"),
    (re.compile(r"\b(terracotta|terra cotta)\b"), "terracotta"),
    (re.compile(r"\brammed earth\b"), "rammed earth"),
    (re.compile(r"\bbamboo\b"), "bamboo"),
    (re.compile(r"\bpolycarbonate\b"), "polycarbonate"),
    (re.compile(r"\b(fabric|textile|canvas)\b"), "fabric"),
    (re.compile(r"\bmasonry\b"), "masonry"),
    (re.compile(r"\bpaving\b"), "paving"),
    (re.compile(r"\bpaint"), "paint"),
    (re.compile(r"\bmirror"), "mirror"),
    (re.compile(r"\bacrylic\b"), "acrylic"),
    (re.compile(r"\bplastic\b"), "plastic"),
    (re.compile(r"\bthatch"), "thatch"),
    (re.compile(r"\bclay\b"), "clay"),
    (re.compile(r"\b(brass|bronze)\b"), "metal"),
    (re.compile(r"\bterrazzo\b"), "terrazzo"),
    (re.compile(r"\b(rock|sandstone)\b"), "stone"),
    (re.compile(r"\bgravel\b"), "paving"),
    (re.compile(r"\bwhite (walls|surfaces)\b"), "plaster"),
    (re.compile(r"\bcomposite\b"), "composite"),
    (re.compile(r"\bcarpet\b"), "fabric"),
    (re.compile(r"\bleather\b"), "leather"),
    (re.compile(r"\bpaper\b"), "paper"),
    (re.compile(r"\bresin\b"), "resin"),
    (re.compile(r"\b(cedar|walnut)\b"), "timber"),
    (re.compile(r"\btextiles?\b"), "fabric"),
    (re.compile(r"\bwallpaper\b"), "paper"),
    (re.compile(r"\bpavement\b"), "paving"),
    (re.compile(r"\bcast iron\b"), "metal"),
    (re.compile(r"\b(parquet|larch|teak|clt)\b"), "timber"),
    (re.compile(r"\basphalt\b"), "paving"),
    (re.compile(r"\bplasterboard\b"), "plaster"),
    (re.compile(r"\b(velvet|upholstery)\b"), "fabric"),
    (re.compile(r"\bmicrocement\b"), "concrete"),
    (re.compile(r"\bmosaic\b"), "tile"),
    (re.compile(r"\bmortar\b"), "masonry"),
    (re.compile(r"\bdecking\b"), "timber"),
    (re.compile(r"\bcork\b"), "cork"),
    (re.compile(r"\bmembrane\b"), "membrane"),
    (re.compile(r"\bshipping containers?\b"), "metal"),
    (re.compile(r"\bdouglas fir\b"), "timber"),
    (re.compile(r"\bbasalt\b"), "stone"),
    (re.compile(r"\blinoleum\b"), "linoleum"),
    (re.compile(r"\bcor-?ten\b"), "corten"),
    (re.compile(r"\bcorian\b"), "composite"),
    (re.compile(r"\blinen\b"), "fabric"),
    (re.compile(r"\brubber\b"), "rubber"),
    (re.compile(r"\brope\b"), "fabric"),
    (re.compile(r"\badobe\b"), "earth"),
    (re.compile(r"\bfiberglass\b"), "composite"),
    (re.compile(r"\blime\b"), "lime"),
    (re.compile(r"\b(joinery|built-in cabinetry)\b"), "timber"),
    (re.compile(r"\bcardboard\b"), "paper"),
    (re.compile(r"\bosb\b"), "timber"),
    (re.compile(r"\bhardwood\b"), "timber"),
    (re.compile(r"\bstraw\b"), "thatch"),
    (re.compile(r"\bwrought iron\b"), "metal"),
    (re.compile(r"\blacquer\b"), "paint"),
    (re.compile(r"\bporcelain\b"), "ceramic"),
    (re.compile(r"\b(millwork|carpentry|mdf|spruce|custom cabinetry)\b"), "timber"),
    (re.compile(r"\bgold\b"), "metal"),
    (re.compile(r"\bfresco\b"), "paint"),
    (re.compile(r"\bdrywall\b"), "plaster"),
    (re.compile(r"\bcobblestone\b"), "paving"),
    (re.compile(r"\breeds\b"), "thatch"),
]

LANDSCAPE_CONTEXT = {
    "grass",
    "greenery",
    "garden planting",
    "landscape",
    "landscaping",
    "planting",
    "plants",
    "trees",
    "vegetation",
    "water",
}
LIGHTING_ATMOSPHERE = {"light", "lighting"}
JUNK_CONTEXT = {"curtains", "furniture", "walls", "window frames", "windows"}
EXTRA_ELEMENT_MAP = {
    "roof": "Roof",
    "roofs": "Roof",
    "patio": "Terrace",
    "patios": "Terrace",
    "fireplace": "Fireplace",
    "fireplaces": "Fireplace",
    "gardens": "Garden",
    "facade panels": "Facade",
    "doors": "Entrance",
    "staircase": "Stair",
    "staircases": "Stair",
    "indoor stairs": "Stair",
    "interior stairs": "Stair",
    "outdoor stairs": "Stair",
    "facades": "Facade",
    "white facade": "Facade",
    "urban facade": "Facade",
    "historic facade": "Facade",
    "slatted facade": "Facade",
    "slat facade": "Facade",
    "curtain wall": "Facade",
    "unspecified facade": "Facade",
    "atrium": "Atrium",
    "atria": "Atrium",
    "canopy": "Canopy",
    "canopies": "Canopy",
    "skylight": "Skylight",
    "corridors": "Corridor",
    "porch": "Entrance",
    "porches": "Entrance",
    "portico": "Entrance",
    "loggias": "Balcony",
    "pitched roof": "Roof",
    "sloping roof": "Roof",
    "roofing": "Roof",
    "green roofs": "Roof",
    "pergola": "Canopy",
    "sliding doors": "Entrance",
    "roof terrace": "Terrace",
    "perforated facade": "Facade",
    "cladding": "Facade",
    "planters": "Garden",
    "pillars": "Column",
    "roof garden": "Garden",
    "pitched roofs": "Roof",
    "sloping roofs": "Roof",
    "roof structure": "Roof",
    "pergolas": "Canopy",
    "black cladding": "Facade",
    "translucent cladding": "Facade",
    "translucent facade": "Facade",
    "flat roof": "Roof",
    "roof cladding": "Roof",
    "deck": "Terrace",
    "veranda": "Terrace",
    "balcony": "Balcony",
    "green wall": "Garden",
    "steps": "Stair",
    "louvers": "Facade",
    "planted roof": "Roof",
    "historic facades": "Facade",
    "white cladding": "Facade",
}
EXTRA_CONTEXT_DROP = {
    "led lighting",
    "solar panels",
    "solar shading",
    "natural materials",
    "unspecified materials",
    "soil",
    "sand",
    "urban furniture",
    "existing structure",
    "railings",
    "interior partitions",
    "partition walls",
    "openings",
    "natural light",
    "daylight",
    "pool",
    "pool water",
    "exhibition displays",
    "exhibition installation",
    "exhibition panels",
    "photographs",
    "architectural models",
    "models",
    "drawings",
    "bookcases",
    "books",
    "built-in furniture",
    "custom furniture",
    "tables",
    "ramps",
    "pavilion structure",
    "pavilion",
    "pavilions",
    "temporary structure",
    "voids",
    "solid volumes",
    "open space",
    "paths",
    "walkways",
    "garden landscape",
    "park landscape",
    "green space",
    "green spaces",
    "landscape planting",
    "industrial shell",
    "platform",
    "tower",
    "frames",
    "wall",
    "gallery walls",
    "ceilings",
    "ceiling",
    "mezzanine",
    "reflective surfaces",
    "textured surfaces",
    "interior surfaces",
    "colored surfaces",
    "translucent panels",
    "acoustic panels",
    "photovoltaic panels",
    "local materials",
    "mixed materials",
    "recycled materials",
    "unspecified material",
    "unspecified finishes",
    "unspecified exterior materials",
    "unspecified interior finishes",
    "not specified",
    "arches",
    "vaults",
    "cabinetry",
    "flooring",
    "shelving",
    "benches",
    "lawn",
    "bookshelves",
    "prefabricated panels",
    "insulation",
    "partitions",
    "signage",
    "beams",
    "platforms",
    "curtain",
    "native planting",
    "shutters",
    "screens",
    "white finishes",
    "structural frame",
    "scaffolding",
    "reused materials",
    "retaining walls",
    "colored interiors",
    "exposed structure",
    "sliding panels",
    "load-bearing walls",
    "solid walls",
    "sculpture",
    "raw materials",
    "plinth",
    "integrated lighting",
    "artwork",
    "furnishings",
    "seating",
    "display panels",
    "existing walls",
    "lattice",
    "perforated panels",
    "olive trees",
    "translucent surfaces",
    "sliding elements",
    "warehouse structure",
    "industrial structure",
    "courtyard planting",
    "colored finishes",
    "exposed services",
    "graphics",
    "flowers",
    "terrain",
    "typography",
    "climbing plants",
    "built-in storage",
    "shrubs",
    "snow",
    "interior lighting",
    "prefabricated elements",
    "black surfaces",
    "bridges",
    "archaeological remains",
    "shop windows",
    "slabs",
    "interior walls",
    "footbridges",
    "garden walls",
    "cables",
}

TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
STOPWORDS = {
    "with",
    "from",
    "that",
    "this",
    "into",
    "uses",
    "used",
    "around",
    "through",
    "while",
    "where",
    "their",
    "there",
    "building",
    "architecture",
    "architectural",
    "project",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
        tmp = Path(f.name)
    tmp.replace(path)


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def classify_noise(term: str) -> dict[str, Any] | None:
    low = _norm_text(term)
    if low in EXTRA_ELEMENT_MAP:
        return {"category": "architectural_element", "target": EXTRA_ELEMENT_MAP[low]}
    if low in EXTRA_CONTEXT_DROP:
        return {"category": "non_material_context", "target": None}
    if low not in MATERIAL_TAXONOMY_NOISE:
        return None
    element = ELEMENT_MAP.get(low)
    if element:
        return {"category": "architectural_element", "target": element}
    if low in LANDSCAPE_CONTEXT:
        return {"category": "landscape_context", "target": None}
    if low in LIGHTING_ATMOSPHERE:
        return {"category": "lighting_atmosphere", "target": None}
    if low in JUNK_CONTEXT:
        return {"category": "junk", "target": None}
    return {"category": "non_material_context", "target": None}


def normalize_material(term: Any) -> str | None:
    low = _norm_text(term)
    if not low:
        return None
    if classify_noise(low):
        return None
    if low in CONTROLLED_MATERIALS:
        return low
    if low in EXACT_MATERIAL_MAP:
        return EXACT_MATERIAL_MAP[low]
    for pattern, target in PATTERN_MATERIAL_MAP:
        if pattern.search(low):
            return target
    return None


def clean_building_row(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cleaned = dict(row)
    original_materials = row.get("material_visual") or []
    materials: list[str] = []
    removed_noise: list[str] = []
    moved_to_elements: dict[str, str] = {}
    material_mapped: dict[str, str] = {}
    unmapped: list[str] = []

    existing_elements = [str(e) for e in (row.get("architectural_elements") or []) if e]
    elements = list(existing_elements)

    for raw in original_materials:
        raw_text = str(raw).strip()
        if not raw_text:
            continue
        low = _norm_text(raw_text)
        noise = classify_noise(low)
        if noise:
            target = noise.get("target")
            if target:
                moved_to_elements[low] = str(target)
                elements.append(str(target))
            else:
                removed_noise.append(low)
            continue
        mapped = normalize_material(low)
        if mapped:
            materials.append(mapped)
            if mapped != low:
                material_mapped[low] = mapped
        else:
            unmapped.append(low)

    cleaned["material_visual"] = _dedupe(materials)
    cleaned["architectural_elements"] = sorted(_dedupe(elements))

    change = {
        "canonical_bld_id": row.get("canonical_bld_id"),
        "removed_noise": sorted(_dedupe(removed_noise)),
        "moved_to_elements": dict(sorted(moved_to_elements.items())),
        "material_mapped": dict(sorted(material_mapped.items())),
        "unmapped_material_terms": sorted(_dedupe(unmapped)),
        "material_before": [str(x) for x in original_materials if x],
        "material_after": cleaned["material_visual"],
        "architectural_elements_after": cleaned["architectural_elements"],
    }
    return cleaned, change


def _add_tokens(out: set[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _add_tokens(out, key)
            _add_tokens(out, item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _add_tokens(out, item)
        return
    for token in TOKEN_RE.findall(str(value).casefold()):
        if len(token) < 3 or token in STOPWORDS:
            continue
        out.add(token)


def build_search_keywords(row: dict[str, Any]) -> list[str]:
    cleaned, _ = clean_building_row(row)
    tokens: set[str] = set()
    for field in (
        "canonical_bld_id",
        "name",
        "names_alts",
        "architect_names",
        "architects_text",
        "program",
        "style",
        "color_tone",
        "atmosphere",
        "material_visual",
        "typology_primary",
        "typology_tags",
        "architectural_elements",
        "source_categories",
        "visual_description",
    ):
        _add_tokens(tokens, cleaned.get(field))
    return sorted(tokens)


def _material_noise_present(values: Any) -> bool:
    return any(
        isinstance(v, str) and _norm_text(v) in MATERIAL_TAXONOMY_NOISE
        for v in (values or [])
    )


def _collect_stats(rows_seen: int, publishable: int, material_counter: Counter[str], counters: Counter[str]) -> dict[str, Any]:
    return {
        "rows_total": rows_seen,
        "publishable_rows": publishable,
        "material_noise_rows": counters["material_noise_rows"],
        "material_empty_publishable_rows": counters["material_empty_publishable_rows"],
        "material_distinct": len(material_counter),
        "top_materials": material_counter.most_common(50),
        "search_keywords_publishable_rows": counters["search_keywords_publishable_rows"],
        "search_keywords_missing_publishable_rows": counters["search_keywords_missing_publishable_rows"],
    }


def _summarize_numbers(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "avg": 0, "p10": 0, "p50": 0, "p90": 0}
    ordered = sorted(values)

    def percentile(pct: float) -> int:
        index = round((len(ordered) - 1) * pct)
        return ordered[index]

    return {
        "count": len(ordered),
        "avg": round(sum(ordered) / len(ordered), 2),
        "p10": percentile(0.10),
        "p50": percentile(0.50),
        "p90": percentile(0.90),
    }


def _increment_scalar(counter: Counter[str], value: Any) -> None:
    if isinstance(value, str) and value.strip():
        counter[value.strip()] += 1


def _facet_distribution_payload(counters: dict[str, Counter[str]]) -> dict[str, Any]:
    return {
        field: {
            "distinct": len(counter),
            "top_values": counter.most_common(50),
        }
        for field, counter in sorted(counters.items())
    }


def material_mapping_payload(raw_material_counter: Counter[str] | None = None) -> dict[str, Any]:
    raw_material_counter = raw_material_counter or Counter()
    noise_terms = {}
    for term in sorted(MATERIAL_TAXONOMY_NOISE):
        noise_terms[term] = classify_noise(term) or {"category": "non_material_context", "target": None}
    for term in sorted(set(EXTRA_ELEMENT_MAP) | EXTRA_CONTEXT_DROP):
        noise_terms[term] = classify_noise(term) or {"category": "non_material_context", "target": None}
    exact = {
        term: target
        for term, target in sorted(EXACT_MATERIAL_MAP.items())
        if term != target
    }
    return {
        "generated_at": now_iso(),
        "controlled_materials": CONTROLLED_MATERIALS,
        "exact_material_map": exact,
        "noise_terms": noise_terms,
        "review_queue_input": str(C24_REVIEW_QUEUE),
        "top_raw_materials": raw_material_counter.most_common(500),
        "notes": [
            "High-confidence deterministic mappings only.",
            "core/vocab.py was not edited.",
            "Unknown material terms are reported for later LLM/manual review.",
            "Classified non-material terms are not re-queued; affected rows keep explicit material_visual=['unspecified'] for loader/facet stability.",
        ],
    }


def apply_cleanup(
    *,
    input_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    keyword_path: Path = DEFAULT_KEYWORDS,
    review_path: Path = DEFAULT_MATERIAL_REVIEW,
    report_path: Path = DEFAULT_REPORT,
    mapping_path: Path = DEFAULT_MAPPING,
    md_path: Path = DEFAULT_MD,
    limit: int | None = None,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    keyword_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    rows_total = 0
    rows_out = 0
    publishable = 0
    before_counter: Counter[str] = Counter()
    after_counter: Counter[str] = Counter()
    before_counts: Counter[str] = Counter()
    after_counts: Counter[str] = Counter()
    actions = Counter()
    changes: list[dict[str, Any]] = []
    unmapped_terms: Counter[str] = Counter()
    facet_counters = {
        "program": Counter(),
        "style": Counter(),
        "color_tone": Counter(),
        "atmosphere": Counter(),
        "typology_primary": Counter(),
        "material_visual": Counter(),
        "architectural_elements": Counter(),
    }
    controlled_oov = Counter()
    keyword_lengths: list[int] = []
    description_word_counts: list[int] = []

    with (
        output_path.open("w", encoding="utf-8") as out,
        keyword_path.open("w", encoding="utf-8") as kw,
        review_path.open("w", encoding="utf-8") as review,
    ):
        out.write('{"buildings":[')
        for row in iter_buildings(input_path, limit=limit):
            rows_total += 1
            if _material_noise_present(row.get("material_visual")):
                before_counts["material_noise_rows"] += 1
            for material in row.get("material_visual") or []:
                if isinstance(material, str) and material.strip():
                    before_counter[_norm_text(material)] += 1
            if row.get("is_publishable"):
                publishable += 1
                if not row.get("material_visual"):
                    before_counts["material_empty_publishable_rows"] += 1

            cleaned, change = clean_building_row(row)
            if change["removed_noise"]:
                actions["rows_with_removed_noise"] += 1
            if change["moved_to_elements"]:
                actions["rows_with_elements_moved"] += 1
            if change["material_mapped"]:
                actions["rows_with_material_mapped"] += 1
            for term in change["unmapped_material_terms"]:
                unmapped_terms[term] += 1
            if (
                change["removed_noise"]
                or change["moved_to_elements"]
                or change["material_mapped"]
                or change["unmapped_material_terms"]
            ):
                if len(changes) < 1000:
                    changes.append(change)

            needs_material_fallback = not cleaned.get("material_visual") and (
                cleaned.get("is_publishable") or not (cleaned.get("architectural_elements") or [])
            )
            if needs_material_fallback:
                after_counts["material_unspecified_rows"] += 1
                if cleaned.get("is_publishable"):
                    after_counts["material_unspecified_publishable_rows"] += 1
                if change["unmapped_material_terms"]:
                    after_counts["material_unmapped_review_rows"] += 1
                    if cleaned.get("is_publishable"):
                        after_counts["material_unmapped_publishable_review_rows"] += 1
                    review.write(
                        json.dumps(
                            {
                                "canonical_bld_id": cleaned.get("canonical_bld_id"),
                                "name": cleaned.get("name"),
                                "program": cleaned.get("program"),
                                "typology_primary": cleaned.get("typology_primary"),
                                "architectural_elements": cleaned.get("architectural_elements") or [],
                                "raw_material_visual": [str(x) for x in (row.get("material_visual") or [])],
                                "unmapped_material_terms": change["unmapped_material_terms"],
                                "removed_noise": change["removed_noise"],
                                "moved_to_elements": change["moved_to_elements"],
                                "source_refs": cleaned.get("source_refs") or {},
                                "is_publishable": bool(cleaned.get("is_publishable")),
                                "review_reason": "unclassified material_visual terms remain after deterministic cleanup",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                cleaned["material_visual"] = ["unspecified"]

            keywords = []
            if cleaned.get("is_publishable"):
                keywords = build_search_keywords(cleaned)
                if keywords:
                    after_counts["search_keywords_publishable_rows"] += 1
                    kw.write(json.dumps({"canonical_bld_id": cleaned["canonical_bld_id"], "search_keywords": keywords}, ensure_ascii=False) + "\n")
                    keyword_lengths.append(len(keywords))
                else:
                    after_counts["search_keywords_missing_publishable_rows"] += 1
                    keyword_lengths.append(0)

                for field in ("program", "style", "color_tone", "atmosphere", "typology_primary"):
                    _increment_scalar(facet_counters[field], cleaned.get(field))
                for material in cleaned.get("material_visual") or []:
                    _increment_scalar(facet_counters["material_visual"], material)
                for element in cleaned.get("architectural_elements") or []:
                    _increment_scalar(facet_counters["architectural_elements"], element)

                for field in ("program", "style", "color_tone", "atmosphere"):
                    value = cleaned.get(field)
                    if value and not vocab.is_valid(field, value):
                        controlled_oov[field] += 1
                typology = cleaned.get("typology_primary")
                if typology and not vocab.is_valid("typology", typology):
                    controlled_oov["typology_primary"] += 1
                for element in cleaned.get("architectural_elements") or []:
                    if element and not vocab.is_valid("architectural_element", element):
                        controlled_oov["architectural_elements"] += 1

                description_word_counts.append(len(TOKEN_RE.findall(str(cleaned.get("visual_description") or ""))))

            if _material_noise_present(cleaned.get("material_visual")):
                after_counts["material_noise_rows"] += 1
            if cleaned.get("is_publishable") and not cleaned.get("material_visual"):
                after_counts["material_empty_publishable_rows"] += 1
            for material in cleaned.get("material_visual") or []:
                after_counter[_norm_text(material)] += 1

            out.write(("," if rows_out else "") + json.dumps(cleaned, ensure_ascii=False))
            rows_out += 1
        out.write("]}")

    before = _collect_stats(rows_total, publishable, before_counter, before_counts)
    after = _collect_stats(rows_total, publishable, after_counter, after_counts)
    coverage = (
        round(after["search_keywords_publishable_rows"] * 100 / publishable, 4)
        if publishable
        else 100.0
    )
    report = {
        "generated_at": now_iso(),
        "status": (
            "PASS"
            if rows_total == rows_out
            and after["material_noise_rows"] == 0
            and coverage == 100.0
            and not controlled_oov
            else "WARN"
        ),
        "input": str(input_path),
        "output": str(output_path),
        "keyword_output": str(keyword_path),
        "material_unmapped_review": str(review_path),
        "mapping": str(mapping_path),
        "db_writes": "none",
        "rows_total": rows_total,
        "rows_out": rows_out,
        "publishable_rows": publishable,
        "material_noise_rows_before": before["material_noise_rows"],
        "material_noise_rows_after": after["material_noise_rows"],
        "material_distinct_before": before["material_distinct"],
        "material_distinct_after": after["material_distinct"],
        "material_empty_publishable_rows_before": before["material_empty_publishable_rows"],
        "material_empty_publishable_rows_after": after["material_empty_publishable_rows"],
        "material_unspecified_rows": after_counts["material_unspecified_rows"],
        "material_unspecified_publishable_rows": after_counts["material_unspecified_publishable_rows"],
        "material_unmapped_review_rows": after_counts["material_unmapped_review_rows"],
        "material_unmapped_publishable_review_rows": after_counts["material_unmapped_publishable_review_rows"],
        "search_keywords_publishable_coverage_pct": coverage,
        "action_counts": dict(actions),
        "before": before,
        "after": after,
        "facet_distributions": _facet_distribution_payload(facet_counters),
        "controlled_oov_counts": dict(controlled_oov),
        "program_other_publishable_rows": facet_counters["program"].get("Other", 0),
        "search_keyword_stats": _summarize_numbers(keyword_lengths),
        "visual_description_word_stats": _summarize_numbers(description_word_counts),
        "top_unmapped_material_terms": unmapped_terms.most_common(500),
        "change_samples": changes,
        "validation_commands": [
            f"python3 tools/canonical_v2_upload_validator.py --input {output_path} --report {REPORT_DIR / 'c25_upload_validator.json'}",
            f"python3 tools/canonical_v2_full_reaudit.py --input {output_path} --strict data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c23_final.json --report {REPORT_DIR / 'c25_full_reaudit.json'} --md {REPORT_DIR / 'c25_full_reaudit.md'}",
            f"python3 tools/canonical_v2_neon_loader.py --dry-run-upsert --input {output_path} --report {REPORT_DIR / 'c25_neon_dry_run_upsert.json'}",
        ],
    }
    atomic_write_json(mapping_path, material_mapping_payload(before_counter))
    atomic_write_json(report_path, report)
    write_markdown(report, md_path)
    return report


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Search Quality C25 Report",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- status: {report['status']}",
        f"- db_writes: {report['db_writes']}",
        "",
        "## Material Cleanup",
        "",
        f"- noise rows before: {report['material_noise_rows_before']}",
        f"- noise rows after: {report['material_noise_rows_after']}",
        f"- distinct material labels before: {report['material_distinct_before']}",
        f"- distinct material labels after: {report['material_distinct_after']}",
        f"- empty material publishable rows before: {report['material_empty_publishable_rows_before']}",
        f"- empty material publishable rows after: {report['material_empty_publishable_rows_after']}",
        f"- unspecified material rows: {report['material_unspecified_rows']}",
        f"- unspecified material publishable rows: {report['material_unspecified_publishable_rows']}",
        f"- unresolved material review rows: {report['material_unmapped_review_rows']}",
        f"- unresolved material publishable review rows: {report['material_unmapped_publishable_review_rows']}",
        "",
        "## Search Keywords",
        "",
        f"- publishable rows: {report['publishable_rows']}",
        f"- coverage: {report['search_keywords_publishable_coverage_pct']}%",
        f"- keyword count avg/p10/p50/p90: {report['search_keyword_stats']['avg']} / {report['search_keyword_stats']['p10']} / {report['search_keyword_stats']['p50']} / {report['search_keyword_stats']['p90']}",
        f"- visual description words avg/p10/p50/p90: {report['visual_description_word_stats']['avg']} / {report['visual_description_word_stats']['p10']} / {report['visual_description_word_stats']['p50']} / {report['visual_description_word_stats']['p90']}",
        "",
        "## Facets",
        "",
        f"- controlled OOV counts: {report['controlled_oov_counts']}",
        f"- program `Other` rows: {report['program_other_publishable_rows']}",
        "",
    ]
    for field in ("program", "style", "color_tone", "atmosphere", "typology_primary", "architectural_elements"):
        dist = report["facet_distributions"][field]
        lines.append(f"- {field} distinct: {dist['distinct']}")
        for value, count in dist["top_values"][:12]:
            lines.append(f"  - {value}: {count}")
    lines += [
        "",
        "## Top Materials After",
        "",
    ]
    for material, count in report["after"]["top_materials"][:30]:
        lines.append(f"- {material}: {count}")
    lines += ["", "## Top Unmapped Terms", ""]
    for term, count in report["top_unmapped_material_terms"][:30]:
        lines.append(f"- {term}: {count}")
    lines += ["", "## Validation Commands", ""]
    for cmd in report["validation_commands"]:
        lines.append(f"- `{cmd}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply deterministic C25 search-quality cleanup")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--keyword-output", type=Path, default=DEFAULT_KEYWORDS)
    parser.add_argument("--material-review-output", type=Path, default=DEFAULT_MATERIAL_REVIEW)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    report = apply_cleanup(
        input_path=args.input,
        output_path=args.output,
        keyword_path=args.keyword_output,
        review_path=args.material_review_output,
        report_path=args.report,
        mapping_path=args.mapping,
        md_path=args.md,
        limit=args.limit,
    )
    print(json.dumps({
        "status": report["status"],
        "rows_total": report["rows_total"],
        "publishable_rows": report["publishable_rows"],
        "material_noise_rows_before": report["material_noise_rows_before"],
        "material_noise_rows_after": report["material_noise_rows_after"],
        "material_distinct_before": report["material_distinct_before"],
        "material_distinct_after": report["material_distinct_after"],
        "search_keywords_publishable_coverage_pct": report["search_keywords_publishable_coverage_pct"],
        "report": str(args.report),
        "mapping": str(args.mapping),
        "keyword_output": str(args.keyword_output),
        "material_review_output": str(args.material_review_output),
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
