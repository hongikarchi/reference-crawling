"""Canonical controlled vocabularies and migrations.

Single source of truth for all vocabulary enforcement across the pipeline:
agent_llm_parser, agent_image_analysis, fix, stage3_embed, review, upload.

Two normalization functions preserve the existing divergent behavior:

  migrate_strict(field, value) -> (new_value | None, reason | None)
    Applies old-vocab-to-new-vocab legacy migrations only. Returns (None, reason)
    when the value cannot be resolved to a current vocab value. Used by
    stage3_embed (during embedding) and review (validation). Mirrors the
    previous stage3_embed.py _normalize() semantics.

  normalize_loose(field, value) -> (new_value, reason | None)
    Applies legacy migrations + fuzzy aliases (substring match on lowercased
    value). Falls back to the original value on pure no-match (permissive).
    Returns None for legacy-dropped values (reason="legacy_dropped") so the
    caller can flag them for human review. Used by fix.py.

Both functions return a reason string on any rewrite so callers can log
migrations to an audit channel. No rewrite happens silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

VOCAB_VERSION = "v2"

# ---------------------------------------------------------------------------
# Canonical vocabularies (v2)
# ---------------------------------------------------------------------------

PROGRAM = frozenset({
    "Housing", "Office", "Museum", "Education", "Religion", "Sports",
    "Transport", "Hospitality", "Healthcare", "Public",
    "Mixed Use", "Landscape", "Infrastructure", "Other",
})

STYLE = frozenset({
    "Minimalist", "Brutalist", "High-Tech", "Postmodern", "Vernacular",
    "Contemporary", "Deconstructivist", "Industrial", "Neo-Classical",
    "Organic", "Modernist", "Parametric",
})

COLOR_TONE = frozenset({
    "Monochrome", "Warm", "Cool", "Earth", "Vibrant", "Neutral", "Dark", "Light",
})

ATMOSPHERE = frozenset({
    "Serene", "Dynamic", "Raw", "Warm", "Urban", "Industrial", "Playful",
    "Monumental", "Intimate", "Futuristic", "Rustic", "Contemplative",
})

# material_visual is suggested, not strict. Kept here for prompt-side hints.
MATERIAL_VISUAL_HINTS = (
    "concrete", "glass", "timber", "brick", "stone", "steel", "corten",
    "aluminum", "copper", "plaster", "tile", "marble", "rammed earth",
    "bamboo", "polycarbonate", "fabric",
)

_VALID_SETS = {
    "program": PROGRAM,
    "style": STYLE,
    "color_tone": COLOR_TONE,
    "atmosphere": ATMOSPHERE,
}

# ---------------------------------------------------------------------------
# Legacy migrations (v1 → v2, strict)
# ---------------------------------------------------------------------------
#
# Keys are lowercased legacy values; values are the current canonical value
# or None to signal "drop, requires re-classification". migrate_strict()
# returns (None, "dropped_in_v2") for drops — caller decides what to do.

LEGACY_MIGRATIONS = {
    "program": {},
    "style": {
        "expressionist": "Postmodern",
        "classical":     "Neo-Classical",
        "other":         None,
    },
    "color_tone": {
        "warm white":   "Light",
        "cool white":   "Light",
        "raw gray":     "Neutral",
        "earthy":       "Earth",
        "natural wood": "Warm",
        "terracotta":   "Warm",
        "colorful":     "Vibrant",
    },
    "atmosphere": {},
}

# ---------------------------------------------------------------------------
# Divisare tag → make_db program (Phase 2)
# ---------------------------------------------------------------------------
#
# Most Divisare tags are NOT typology indicators (they're materials, sizes,
# themes, cities, building elements). Only the subset below is treated as a
# program signal. Per-project lookup: walk the project's tag_slugs in order,
# return the first matched program; fall back to "Other" if no typology tag
# is present. Add entries as new typology tags are encountered.

DIVISARE_TAG_TO_PROGRAM = {
    # Religion
    "chapels":       "Religion",
    "churches":      "Religion",
    "mosques":       "Religion",
    "synagogues":    "Religion",
    "temples":       "Religion",

    # Education
    "kindergartens":            "Education",
    "primary-schools":          "Education",
    "secondary-schools":        "Education",
    "universities":             "Education",
    "schools":                  "Education",
    "libraries-and-mediatheques": "Education",
    "libraries":                "Education",

    # Housing
    "houses":               "Housing",
    "apartment-blocks":     "Housing",
    "single-family-houses": "Housing",
    "social-housing":       "Housing",
    "student-housing":      "Housing",

    # Office
    "office-buildings":  "Office",
    "headquarters":      "Office",
    "co-working-spaces": "Office",

    # Museum
    "museums":           "Museum",
    "art-galleries":     "Museum",
    "exhibition-spaces": "Museum",

    # Sports
    "stadiums":       "Sports",
    "sport-centers":  "Sports",
    "swimming-pools": "Sports",
    "gymnasiums":     "Sports",

    # Transport
    "airports":        "Transport",
    "train-stations":  "Transport",
    "metro-stations":  "Transport",
    "bus-stations":    "Transport",
    "ferry-terminals": "Transport",

    # Hospitality
    "hotels":      "Hospitality",
    "restaurants": "Hospitality",
    "bars":        "Hospitality",
    "cafes":       "Hospitality",
    "resorts":     "Hospitality",
    "wine-cellars":"Hospitality",
    "cinemas":     "Hospitality",
    "theaters":    "Hospitality",

    # Healthcare
    "hospitals":              "Healthcare",
    "clinics":                "Healthcare",
    "rehabilitation-centers": "Healthcare",

    # Public
    "administrative-centers": "Public",
    "town-halls":             "Public",
    "courthouses":            "Public",
    "civic-centers":          "Public",
    "community-centers":      "Public",
    "archives":               "Public",
    "auditoriums":            "Public",
    "pavilions":              "Public",

    # Mixed Use
    "mixed-use":           "Mixed Use",
    "mixed-use-buildings": "Mixed Use",

    # Landscape
    "parks":         "Landscape",
    "gardens":       "Landscape",
    "public-spaces": "Landscape",
    "plazas":        "Landscape",
    "urban-parks":   "Landscape",

    # Infrastructure
    "bridges":      "Infrastructure",
    "skybridges":   "Infrastructure",
    "viaducts":     "Infrastructure",
    "tunnels":      "Infrastructure",
    "dams":         "Infrastructure",
    "power-plants": "Infrastructure",
    "factories":    "Infrastructure",
}


def map_divisare_tags_to_program(tag_slugs: list) -> Tuple[str, str]:
    """Return (program, reason) given a project's Divisare tag slugs.

    First typology-indicator tag wins. No match → "Other".
    Audit-logged via the same _log channel as vocabulary migrations.
    """
    if not tag_slugs:
        return "Other", "no_tags"
    for slug in tag_slugs:
        if slug in DIVISARE_TAG_TO_PROGRAM:
            program = DIVISARE_TAG_TO_PROGRAM[slug]
            _log(MigrationEvent("program", slug, program,
                                f"divisare_tag:{slug}", "divisare"))
            return program, f"matched:{slug}"
    _log(MigrationEvent("program", ",".join(tag_slugs[:3]), "Other",
                        "no_typology_tag_match", "divisare"))
    return "Other", "no_typology_tag_match"


# ---------------------------------------------------------------------------
# Loose aliases (fuzzy substring normalization, used only by fix.py)
# ---------------------------------------------------------------------------
#
# Substrings are matched against the lowercased stripped input. First match
# wins (dict iteration order), so put more specific keywords before generic
# ones if overlap would cause issues.

LOOSE_ALIASES = {
    "program": {
        "house": "Housing", "apartment": "Housing", "residential": "Housing",
        "villa": "Housing", "dwelling": "Housing",
        "office": "Office", "workspace": "Office", "corporate": "Office",
        "museum": "Museum", "gallery": "Museum",
        "school": "Education", "university": "Education", "library": "Education",
        "church": "Religion", "mosque": "Religion", "temple": "Religion",
        "sport": "Sports", "stadium": "Sports", "arena": "Sports",
        "airport": "Transport", "station": "Transport", "terminal": "Transport",
        "hotel": "Hospitality", "restaurant": "Hospitality",
        "hospital": "Healthcare", "clinic": "Healthcare", "medical": "Healthcare",
        "civic": "Public", "community": "Public", "plaza": "Public",
        "mixed": "Mixed Use",
        "landscape": "Landscape", "park": "Landscape", "garden": "Landscape",
        "bridge": "Infrastructure", "pavilion": "Infrastructure",
    },
    "style": {
        "minimal":       "Minimalist", "minimalism": "Minimalist",
        "brutal":        "Brutalist",  "brutalism":  "Brutalist",
        "high tech":     "High-Tech",  "hightech":   "High-Tech",
        "contemporary":  "Contemporary", "modern":   "Contemporary",
        "deconstructiv": "Deconstructivist",
        "neo-classic":   "Neo-Classical", "neoclassical": "Neo-Classical",
        "organic":       "Organic",
        "vernacular":    "Vernacular",
        "industrial":    "Industrial",
        "parametric":    "Parametric",
        "modernist":     "Modernist",
        "postmodern":    "Postmodern",
    },
    "color_tone": {
        "monochrome": "Monochrome",
        "vibrant":    "Vibrant",
        "neutral":    "Neutral",
        "earth":      "Earth",
        "light":      "Light",
        "dark":       "Dark",
        "warm":       "Warm",
        "cool":       "Cool",
    },
    "atmosphere": {},
}


# ---------------------------------------------------------------------------
# Audit log — callers opt in by setting a collector list before invoking
# ---------------------------------------------------------------------------

@dataclass
class MigrationEvent:
    field: str
    original: Optional[str]
    rewritten: Optional[str]
    reason: str
    kind: str   # 'legacy' | 'loose' | 'invalid'


_audit_sink: Optional[list] = None


def set_audit_sink(sink: Optional[list]) -> None:
    """Register a list to receive MigrationEvent records. Pass None to disable."""
    global _audit_sink
    _audit_sink = sink


def _log(event: MigrationEvent) -> None:
    if _audit_sink is not None:
        _audit_sink.append(event)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_valid(field: str, value: Optional[str]) -> bool:
    """Return True iff value is a current canonical vocab entry for field."""
    if not value:
        return False
    vocab = _VALID_SETS.get(field)
    if vocab is None:
        raise KeyError(f"unknown vocab field: {field!r}")
    return value in vocab


def _case_normalize(field_vocab: frozenset, value: str) -> Optional[str]:
    """Return the canonical-case vocab entry matching value case-insensitively, or None."""
    lower = value.lower().strip()
    for canonical in field_vocab:
        if canonical.lower() == lower:
            return canonical
    return None


def migrate_strict(field: str, value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Strict old→new migration + case normalization. No fuzzy aliases.

    Returns (new_value, reason). reason is None when no rewrite occurred.
    Returns (None, reason) when the value is unrecognized or explicitly dropped.

    Replaces stage3_embed.py _normalize() behavior.
    """
    vocab = _VALID_SETS.get(field)
    if vocab is None:
        raise KeyError(f"unknown vocab field: {field!r}")

    if value is None or value == "":
        return None, None

    if value in vocab:
        return value, None

    cased = _case_normalize(vocab, value)
    if cased is not None:
        _log(MigrationEvent(field, value, cased, "case_normalized", "case"))
        return cased, "case_normalized"

    lower = value.lower().strip()
    migrations = LEGACY_MIGRATIONS.get(field, {})
    if lower in migrations:
        rewritten = migrations[lower]
        reason = "legacy_dropped" if rewritten is None else "legacy_migrated"
        _log(MigrationEvent(field, value, rewritten, reason, "legacy"))
        return rewritten, reason

    _log(MigrationEvent(field, value, None, "unknown_value", "invalid"))
    return None, "unknown_value"


def normalize_loose(field: str, value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Legacy migrations + fuzzy substring aliases + passthrough on no-match.

    Returns (new_value, reason). reason is None when no rewrite occurred.
    Never returns None unless value was already empty — falls back to the
    original value on no-match (permissive).

    Replaces fix.py _remap() behavior.
    """
    vocab = _VALID_SETS.get(field)
    if vocab is None:
        raise KeyError(f"unknown vocab field: {field!r}")

    if not value:
        return value, None

    if value in vocab:
        return value, None

    cased = _case_normalize(vocab, value)
    if cased is not None:
        _log(MigrationEvent(field, value, cased, "case_normalized", "case"))
        return cased, "case_normalized"

    lower = value.lower().strip()

    migrations = LEGACY_MIGRATIONS.get(field, {})
    if lower in migrations:
        rewritten = migrations[lower]
        reason = "legacy_dropped" if rewritten is None else "legacy_migrated"
        _log(MigrationEvent(field, value, rewritten, reason, "legacy"))
        return rewritten, reason

    aliases = LOOSE_ALIASES.get(field, {})
    for keyword, mapped in aliases.items():
        if keyword in lower:
            _log(MigrationEvent(field, value, mapped, "loose_alias", "loose"))
            return mapped, "loose_alias"

    return value, None


# ---------------------------------------------------------------------------
# Prompt helpers — keep prompt text in sync with vocab.
# ---------------------------------------------------------------------------

def options_string(field: str) -> str:
    """Comma-separated sorted vocab list for inclusion in prompts."""
    vocab = _VALID_SETS.get(field)
    if vocab is None:
        raise KeyError(f"unknown vocab field: {field!r}")
    return ", ".join(sorted(vocab))


# ---------------------------------------------------------------------------
# Per-building QC (folded in from agent_qc.py — Phase 8A)
# ---------------------------------------------------------------------------
#
# Used by pipeline_harness.py after image analysis completes. Returns a
# QCResult indicating whether the building passes vocab + length checks; if
# not, names which stage should re-process it.

@dataclass
class QCResult:
    passed: bool
    issues: list = field(default_factory=list)
    re_queue_to: Optional[str] = None  # "enrichment" | "image_analysis" | None


def _check_enrichment_fields(building: dict) -> list:
    """Checks fields set by agent_llm_parser. Returns list of issue strings."""
    issues = []

    name_en = building.get("name_en")
    if not name_en or len(name_en.strip()) < 5:
        issues.append(f"name_en missing or too short: {name_en!r}")

    program = building.get("program")
    if not is_valid("program", program):
        issues.append(f"program invalid: {program!r}")

    atmosphere = building.get("atmosphere")
    if atmosphere and not is_valid("atmosphere", atmosphere):
        issues.append(f"atmosphere not in controlled vocab: {atmosphere!r}")

    return issues


def _check_image_fields(building: dict) -> list:
    """Checks fields set by agent_image_analysis. Returns list of issue strings."""
    issues = []

    style = building.get("style")
    if not is_valid("style", style):
        issues.append(f"style invalid or missing: {style!r}")

    color_tone = building.get("color_tone")
    if not is_valid("color_tone", color_tone):
        issues.append(f"color_tone invalid or missing: {color_tone!r}")

    visual_description = building.get("visual_description")
    if not visual_description or len(visual_description.strip()) < 20:
        issues.append("visual_description missing or too short")

    material_visual = building.get("material_visual")
    if not isinstance(material_visual, list):
        issues.append(f"material_visual must be a list, got: {type(material_visual).__name__}")

    return issues


def check_building(building: dict) -> QCResult:
    """Validate a single building that has passed through enrichment + image analysis.

    Returns QCResult with:
    - passed=True if all checks pass
    - re_queue_to="enrichment" if text fields are bad
    - re_queue_to="image_analysis" if visual fields are bad
    - When both fail, re_queue_to="enrichment" (fix root cause first)
    """
    enrichment_issues = _check_enrichment_fields(building)
    image_issues = _check_image_fields(building)
    all_issues = enrichment_issues + image_issues

    if not all_issues:
        return QCResult(passed=True)

    if enrichment_issues:
        return QCResult(passed=False, issues=all_issues, re_queue_to="enrichment")
    return QCResult(passed=False, issues=all_issues, re_queue_to="image_analysis")
