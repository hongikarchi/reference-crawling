"""Canonical building entity (Phase 2).

A CanonicalBuilding is the unified record that the upload step writes to
Postgres. It merges:
  - Divisare metadata (the canonical spine: stable IDs, curated taxonomy)
  - metalocus content (images, free-text descriptions, embeddings) — when
    a match exists per `data/canonical/match/metalocus_to_divisare.json`

Each scalar field tracks its provenance: which source actually populated it.
This lets downstream code make policy decisions (e.g., trust Divisare's year
over metalocus's; use metalocus's visual_description but never its
inconsistent architect names).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional


# Source labels for the provenance dict. Add as more sources land.
SOURCE_DIVISARE = "divisare"
SOURCE_METALOCUS = "metalocus"
SOURCE_LLM      = "llm"           # for fields LLM-rewrote (e.g. canonical name_en)
SOURCE_DERIVED  = "derived"       # for vocab.migrate_strict / typology mapping outputs


@dataclass
class CanonicalBuilding:
    # ------------------------------------------------------------------
    # Identity (Divisare-canonical when matched; else metalocus-only orphan)
    # ------------------------------------------------------------------
    divisare_id:               Optional[int]   = None
    divisare_slug:             Optional[str]   = None
    metalocus_building_id:     Optional[str]   = None    # B00042 etc.

    # ------------------------------------------------------------------
    # Core fields (Divisare authoritative when both sources present)
    # ------------------------------------------------------------------
    name:                      Optional[str]   = None
    abstract:                  Optional[str]   = None
    description:               Optional[str]   = None
    location_country:          Optional[str]   = None
    location_city:             Optional[str]   = None
    project_year:              Optional[int]   = None

    # ------------------------------------------------------------------
    # Architects (canonical IDs from Divisare; names from Divisare display)
    # ------------------------------------------------------------------
    architect_canonical_ids:   list[int]       = field(default_factory=list)
    architect_names:           list[str]       = field(default_factory=list)

    # ------------------------------------------------------------------
    # Vocab-mapped to make_db's controlled vocabularies (vocab.py)
    # ------------------------------------------------------------------
    program:                   Optional[str]   = None    # one of vocab.PROGRAM
    style:                     Optional[str]   = None    # one of vocab.STYLE — image-derived (metalocus side)
    color_tone:                Optional[str]   = None    # vocab.COLOR_TONE — image-derived
    atmosphere:                Optional[str]   = None    # vocab.ATMOSPHERE — image-derived
    material_visual:           list[str]       = field(default_factory=list)
    visual_description:        Optional[str]   = None    # metalocus's image-prose

    # ------------------------------------------------------------------
    # Tags (Divisare's curated taxonomy — keep raw, no normalization)
    # ------------------------------------------------------------------
    divisare_tags:             list[str]       = field(default_factory=list)

    # ------------------------------------------------------------------
    # Credits (Divisare sidebar — sub-firms by role)
    # JSON-shaped: {"structures": [...], "lighting": [...], "photo": [...], ...}
    # Design/Designer keys excluded (already in architect_canonical_ids).
    # ------------------------------------------------------------------
    divisare_credits:          Optional[dict]  = None

    # ------------------------------------------------------------------
    # Quantitative
    # ------------------------------------------------------------------
    area_sqm:                  Optional[float] = None    # parsed from sidebar "Area" if present

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------
    image_paths:               list[str]       = field(default_factory=list)   # local metalocus paths
    cover_image_url_divisare:  Optional[str]   = None    # remote, single
    divisare_gallery_urls:     list[str]       = field(default_factory=list)   # remote, ~10-19 per project

    # ------------------------------------------------------------------
    # Embedding (set in stage3_embed)
    # ------------------------------------------------------------------
    embedding:                 Optional[list]  = None    # 384-dim

    # ------------------------------------------------------------------
    # Versioning + provenance
    # ------------------------------------------------------------------
    vocab_version:             Optional[str]   = None
    prompt_version:            Optional[str]   = None    # for any LLM-set field
    provenance:                dict            = field(default_factory=dict)
    # Phase 14a — confidence tier set by canonical/reality_filter:
    #   T1 = cross-source confirmed
    #   T2 = single-source w/ structural metadata signal
    #   T3 = Haiku-validated borderline
    #   None = pre-Phase-14a row OR non-strict build
    confidence_tier:           Optional[str]   = None

    # Phase 14b — per-source description map for enrich concat.
    # Keys: "metalocus" | "divisare" | "architizer" | "archello".
    # Empty dict for pre-14b rows. Populated by canonical/build.py from
    # whichever source DBs have a description field for the matched ID.
    # The single-string `description` field above is still set (chosen
    # source per source-priority) for backward compat.
    description_per_source:    dict            = field(default_factory=dict)
    # Provenance shape:
    #   {"name": "divisare", "description": "metalocus", "program": "derived",
    #    "atmosphere": "metalocus", ...}
    # If a field isn't in provenance, it's None.

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CanonicalBuilding":
        # Drop unknown keys defensively (forward-compat with older saves)
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "CanonicalBuilding":
        return cls.from_dict(json.loads(s))

    # Provenance helper — set field value AND record its source.
    def set_field(self, key: str, value, source: str) -> None:
        if not hasattr(self, key):
            raise AttributeError(f"unknown field: {key}")
        setattr(self, key, value)
        self.provenance[key] = source

    @property
    def is_orphan(self) -> bool:
        """True if no Divisare match — metalocus-only record."""
        return self.divisare_id is None and self.metalocus_building_id is not None
