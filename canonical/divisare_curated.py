"""Deterministic normalization policy for the Divisare curated database.

This module deliberately does not import ``core.vocab``.  The existing
four-source vocabulary is production state and must not be changed while the
Divisare source is being audited independently.  Instead, source tags are
projected into evidence claims with an explicit version, scope, confidence,
and search tier.  Raw tags always remain available in the curated database.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit


TAXONOMY_VERSION = "divisare-taxonomy-v1.2"
TEXT_PROCESSOR_VERSION = "divisare-text-clean-v1.0"
ASSET_KEY_VERSION = "divisare-asset-key-v1.1"
URL_HINT_VERSION = "divisare-url-hint-v1.0"
CLUSTER_VERSION = "divisare-cluster-v1.1"
RESOLVER_VERSION = "divisare-resolver-v1.2"


@dataclass(frozen=True)
class TagMapping:
    axis: str
    value: str
    target_scope: str = "building"
    mapping_kind: str = "direct"
    confidence: float = 0.9
    priority: int = 50
    search_tier: str = "primary"
    notes: Optional[str] = None


@dataclass(frozen=True)
class AssetIdentity:
    asset_key: str
    public_id: Optional[str]
    delivery_version: Optional[str]
    original_filename: Optional[str]
    url_generation: str
    transform_signature: Optional[str]


@dataclass(frozen=True)
class CleanText:
    text: Optional[str]
    quality_status: str
    removed_ui_markers: int


def parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [text]
    if isinstance(parsed, list):
        return parsed
    if parsed is None:
        return []
    return [parsed]


def parse_json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    text = str(value).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def clean_scalar(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).replace("\u00a0", " ").split())
    return text or None


_MOJIBAKE_TRANSLATION = str.maketrans(
    {
        "\u0091": "'",
        "\u0092": "'",
        "\u0093": '"',
        "\u0094": '"',
        "\u0096": "-",
        "\u0097": "-",
    }
)
_DIVISARE_UI_RE = re.compile(
    r"\bAdd to collection\s+Choose collection\.\.\.\s+New collection\.\.\.",
    re.IGNORECASE,
)


def clean_description(value: Any) -> CleanText:
    """Remove the known Divisare collection UI string conservatively.

    The historical parser flattened the complete ``.description`` DOM, which
    also included image captions and photographer labels.  Those boundaries
    cannot be reconstructed reliably from the flattened string, so this
    cleaner removes only proven UI boilerplate and marks the remaining text as
    potentially containing caption residue.  It never pretends the result is
    ground truth prose.
    """

    text = clean_scalar(value)
    if not text:
        return CleanText(None, "missing", 0)
    text = text.translate(_MOJIBAKE_TRANSLATION)
    marker_count = len(_DIVISARE_UI_RE.findall(text))
    text = _DIVISARE_UI_RE.sub(" ", text)
    text = " ".join(text.split()).strip()
    if not text:
        return CleanText(None, "rejected_ui_only", marker_count)
    status = "ui_removed_caption_residue_possible" if marker_count else "clean"
    return CleanText(text, status, marker_count)


COUNTRY_ALIASES = {
    "korea, republic of": "South Korea",
    "republic of korea": "South Korea",
    "south korea": "South Korea",
    "u.s.a.": "United States",
    "usa": "United States",
    "us": "United States",
    "united states of america": "United States",
    "united states": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "united kingdom": "United Kingdom",
    "viet nam": "Vietnam",
    "russian federation": "Russia",
    "czech republic": "Czechia",
}
_MISSING_LOCATION_VALUES = {"-", "--", "\u2013", "\u2014", "n/a", "unknown"}


def clean_location(value: Any) -> Optional[str]:
    text = clean_scalar(value)
    if not text or text.casefold() in _MISSING_LOCATION_VALUES:
        return None
    return text


def normalize_country(value: Any) -> Optional[str]:
    text = clean_location(value)
    if not text or text.startswith("- "):
        return None
    return COUNTRY_ALIASES.get(text.casefold(), text)


def normalize_identity_text(value: Any) -> str:
    """Normalize a name for blocking, not for display."""

    text = clean_scalar(value) or ""
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


GENERIC_BUILDING_NAMES = {
    "apartment",
    "apartments",
    "building",
    "casa",
    "house",
    "office",
    "pavilion",
    "school",
    "villa",
}
_GENERIC_BUILDING_RE = re.compile(
    r"^(?:apartment|building|casa|house|office|pavilion|school|villa)"
    r"(?:\s+(?:[a-z]|\d{1,3}|project|extension|renovation))?$",
    re.IGNORECASE,
)


def is_generic_building_name(value: str) -> bool:
    normalized = normalize_identity_text(value)
    return (
        not normalized
        or normalized in GENERIC_BUILDING_NAMES
        or bool(_GENERIC_BUILDING_RE.fullmatch(normalized))
        or len(normalized) < 5
    )


_DIVISARE_UUID_RE = re.compile(
    r"/(v\d+)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/.]|$)"
)
_DIVISARE_HEX_RE = re.compile(r"/(v\d+)/([0-9a-f]{20,40})(?:[/.]|$)")
_DIVISARE_PUBLIC_ID_RE = re.compile(
    r"/(v\d+)/([a-z0-9][a-z0-9-]{14,40})(?:[/.]|$)"
)
_DIVISARE_PROJECT_IMAGE_RE = re.compile(r"/project_images/(\d+)/([^/?.]+)")
_TRANSFORM_RE = re.compile(r"/(?:image/upload|images)/([^/]+)/v\d+/")


def divisare_asset_identity(url: Any) -> Optional[AssetIdentity]:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if "divisare" not in (parsed.netloc or "").casefold():
        return None
    path = parsed.path or ""
    transform_match = _TRANSFORM_RE.search(path)
    transform = transform_match.group(1) if transform_match else None

    old = _DIVISARE_PROJECT_IMAGE_RE.search(path)
    if old:
        image_id, filename = old.groups()
        return AssetIdentity(
            asset_key=f"divisare|{image_id}|{filename}",
            public_id=image_id,
            delivery_version=None,
            original_filename=filename,
            url_generation="project_images",
            transform_signature=transform,
        )

    for pattern in (_DIVISARE_UUID_RE, _DIVISARE_HEX_RE, _DIVISARE_PUBLIC_ID_RE):
        match = pattern.search(path)
        if match:
            delivery_version, public_id = match.groups()
            return AssetIdentity(
                asset_key=f"divisare|{public_id}|{delivery_version}",
                public_id=public_id,
                delivery_version=delivery_version,
                original_filename=None,
                url_generation="cloudinary_public_id",
                transform_signature=transform,
            )

    fallback = path.strip("/")
    if not fallback:
        return None
    return AssetIdentity(
        asset_key=f"divisare|path|{fallback}",
        public_id=None,
        delivery_version=None,
        original_filename=None,
        url_generation="path_fallback",
        transform_signature=transform,
    )


_URL_HINT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "drawing",
        re.compile(
            r"(?:^|[-_.\s])(?:plan|plans|planta|pianta|drawing|drawings|"
            r"elevation|elevacion|prospetto|axon|axonometric|diagram|sketch)"
            r"(?:$|[-_.\s])",
            re.IGNORECASE,
        ),
    ),
    (
        "section",
        re.compile(
            r"(?:^|[-_.\s])(?:section|sections|sezione|secciones?|corte)"
            r"(?:$|[-_.\s])",
            re.IGNORECASE,
        ),
    ),
    (
        "detail",
        re.compile(
            r"(?:^|[-_.\s])(?:detail|details|detalle|dettaglio|construction)"
            r"(?:$|[-_.\s])",
            re.IGNORECASE,
        ),
    ),
    (
        "interior",
        re.compile(r"(?:^|[-_.\s])(?:interior|inside|interno)(?:$|[-_.\s])", re.IGNORECASE),
    ),
    (
        "exterior",
        re.compile(r"(?:^|[-_.\s])(?:exterior|outside|facade)(?:$|[-_.\s])", re.IGNORECASE),
    ),
    (
        "aerial",
        re.compile(r"(?:^|[-_.\s])(?:aerial|drone|birdseye)(?:$|[-_.\s])", re.IGNORECASE),
    ),
)


def filename_media_hints(identity: AssetIdentity) -> list[str]:
    """Return low-confidence hints from a descriptive legacy filename."""

    if not identity.original_filename:
        return []
    candidate = identity.original_filename
    return [value for value, pattern in _URL_HINT_PATTERNS if pattern.search(candidate)]


def _invert_groups(groups: dict[str, Iterable[str]]) -> dict[str, str]:
    return {slug: value for value, slugs in groups.items() for slug in slugs}


TYPE_PROGRAM = _invert_groups(
    {
        "Housing": {
            "apartment-blocks",
            "public-and-social-housing",
            "residential-complexes",
            "small-apartment-blocks",
            "deck-access-blocks",
            "student-houses",
            "row-houses",
            "student-halls",
        },
        "Office": {"office-blocks", "headquarters"},
        "Museum": {"museums"},
        "Education": {
            "colleges-and-universities",
            "primary-schools",
            "secondary-schools",
            "kindergartens-and-pre-schools",
            "research-centers",
            "music-schools-and-art-academies",
            "libraries-and-mediatheques",
            "training-centers",
        },
        "Religion": {
            "churches",
            "chapels",
            "italian-churches",
            "funerary-chapels",
            "convents-monasteries-parishes",
            "temples",
            "mosques",
            "synagogues",
        },
        "Sports": {
            "sport-halls",
            "outdoor-sports-fields",
            "swimming-pools",
            "skateparks",
            "sport-clubs",
            "stadiums",
            "arenas",
        },
        "Transport": {
            "car-parks",
            "transportation-hubs",
            "train-stations",
            "metro-stations",
            "airports",
            "bike-stations",
            "bus-stops",
            "maritime-facilities",
            "gas-stations-rest-areas-and-toll-gates",
            "boathouses-and-marinas",
        },
        "Hospitality": {
            "restaurants",
            "hotels",
            "hostels-and-guesthouses",
            "bars",
            "tea-houses",
            "camping",
            "beach-facilities",
        },
        "Healthcare": {"hospitals", "nursing-homes"},
        "Public": {
            "cultural-centers",
            "civic-centers",
            "visitor-centers",
            "theaters",
            "administrative-centers",
            "memorials",
            "concert-halls",
            "city-and-town-halls",
            "convention-centers",
            "courthouses",
            "fire-police-stations",
            "prisons-detention-centres",
            "outdoor-performing-arts-venues",
        },
        "Landscape": {
            "squares-and-streets",
            "urban-parks",
            "landscape-design",
            "archaeological-parks",
            "small-urban-gardens",
            "green-and-scenic-walkways",
            "waterfronts-and-coastal-redevelopments",
            "covered-squares",
            "tourist-routes",
        },
        "Infrastructure": {
            "footbridges",
            "industrial-buildings",
            "power-plants",
            "traffic-bridges",
            "floating-architecture",
            "warehouses",
        },
    }
)

TYPE_PROGRAM_SUPPORTING = {
    "research-centers",
    "archaeological-parks",
    "beach-facilities",
    "boathouses-and-marinas",
    "floating-architecture",
}

TYPE_WORK_TYPE = {
    "installations": "Installation",
    "exhibit-design": "Exhibition Design",
    "tower-blocks-and-skyscrapers": "High-Rise Building",
    "funerary": "Funerary Architecture",
    "scenographies": "Scenography",
    "fair-stands": "Exhibition Stand",
    "expo-pavilions": "Expo Pavilion",
    "garden-studios": "Garden Studio",
    "mountain-huts": "Mountain Hut",
    "zoos-and-animal-shelters": "Zoo/Animal Shelter",
    "greenhouses": "Greenhouse",
    "trade-fair-center": "Exhibition Centre",
    "catwalks": "Catwalk",
}


TYPE_TYPOLOGY = {
    "apartment-blocks": "Apartment",
    "small-apartment-blocks": "Apartment",
    "public-and-social-housing": "Housing",
    "residential-complexes": "Housing",
    "deck-access-blocks": "Housing",
    "student-houses": "Student Housing",
    "student-halls": "Student Housing",
    "row-houses": "Housing",
    "office-blocks": "Office",
    "headquarters": "Office",
    "museums": "Museum",
    "colleges-and-universities": "University",
    "primary-schools": "School",
    "secondary-schools": "School",
    "kindergartens-and-pre-schools": "Kindergarten",
    "libraries-and-mediatheques": "Library",
    "churches": "Religious Building",
    "chapels": "Religious Building",
    "italian-churches": "Religious Building",
    "funerary-chapels": "Religious Building",
    "temples": "Religious Building",
    "mosques": "Religious Building",
    "synagogues": "Religious Building",
    "stadiums": "Stadium",
    "sport-halls": "Sports Centre",
    "arenas": "Sports Centre",
    "airports": "Airport",
    "train-stations": "Train Station",
    "car-parks": "Car Park",
    "restaurants": "Restaurant",
    "hotels": "Hotel",
    "hostels-and-guesthouses": "Hotel",
    "hospitals": "Hospital",
    "nursing-homes": "Care Home",
    "civic-centers": "Civic Building",
    "city-and-town-halls": "Civic Building",
    "courthouses": "Civic Building",
    "theaters": "Theatre",
    "concert-halls": "Concert Hall",
    "cinemas": "Theatre",
    "shopping-centers": "Shopping Centre",
    "retail-markets": "Retail",
    "pavilions": "Pavilion",
    "industrial-buildings": "Industrial",
    "warehouses": "Warehouse",
    "wineries-and-distilleries": "Winery",
    "urban-parks": "Park",
    "landscape-design": "Park",
    "footbridges": "Bridge",
    "traffic-bridges": "Bridge",
    "memorials": "Memorial",
}


HOUSE_EXCLUSIONS = {"dollhouses", "pet-houses"}
HOUSE_MATERIAL = {
    "wooden-houses": "timber",
    "concrete-houses": "concrete",
    "brick-houses": "brick",
}
HOUSE_COUNTRY = {
    "american-houses": "United States",
    "argentinian-houses": "Argentina",
    "australian-houses": "Australia",
    "austrian-houses": "Austria",
    "belgian-houses": "Belgium",
    "brazilian-houses": "Brazil",
    "british-houses": "United Kingdom",
    "canadian-houses": "Canada",
    "chilean-houses": "Chile",
    "chinese-houses": "China",
    "croatian-houses": "Croatia",
    "czech-houses": "Czechia",
    "danish-houses": "Denmark",
    "dutch-houses": "Netherlands",
    "french-houses": "France",
    "german-houses": "Germany",
    "greek-houses": "Greece",
    "hungarian-houses": "Hungary",
    "icelandic-houses": "Iceland",
    "indian-houses": "India",
    "irish-houses": "Ireland",
    "israeli-houses": "Israel",
    "italian-houses": "Italy",
    "italian-rural-houses": "Italy",
    "japanese-urban-houses": "Japan",
    "japanese-non-urban-houses": "Japan",
    "korean-houses": "South Korea",
    "mexican-houses": "Mexico",
    "new-zealand-houses": "New Zealand",
    "paraguayan-houses": "Paraguay",
    "peruvian-houses": "Peru",
    "polish-houses": "Poland",
    "portuguese-houses": "Portugal",
    "romanian-houses": "Romania",
    "spanish-houses": "Spain",
    "swiss-houses": "Switzerland",
    "turkish-houses": "Turkey",
    "uruguayan-houses": "Uruguay",
}
HOUSE_REGION = {
    "african-houses": "Africa",
    "balkan-houses": "Balkans",
    "baltic-houses": "Baltic",
    "east-european-houses": "Eastern Europe",
    "latin-american-houses": "Latin America",
    "mediterranean-houses": "Mediterranean",
    "middle-east-houses": "Middle East",
    "scandinavian-houses": "Nordic/Scandinavian",
    "south-east-asian-houses": "Southeast Asia",
}
HOUSE_CONTEXT = {
    "woodland-houses": "Woodland",
    "narrow-urban-houses": "Narrow Urban Site",
    "country-houses": "Rural",
    "italian-rural-houses": "Rural",
    "beach-houses": "Coastal",
    "mountain-houses": "Mountain",
    "japanese-urban-houses": "Urban",
    "japanese-non-urban-houses": "Non-Urban",
}


MATERIALITY_MATERIAL = {
    "tiles": "tile",
    "bricks": "brick",
    "timber": "timber",
    "polycarbonate": "polycarbonate",
    "marble": "marble",
    "cor-ten": "corten",
    "concrete": "concrete",
    "metals": "metal",
    "membranes": "membrane",
    "plaster": "plaster",
    "coloured-concrete": "colored concrete",
    "stone": "stone",
    "terracotta": "terracotta",
    "bamboo": "bamboo",
    "fabric": "fabric",
    "rammed-earth": "rammed earth",
    "glass": "glass",
    "cork": "cork",
    "wallpapers": "wallpaper",
    "ice": "ice",
    "cardboard": "cardboard",
    "carpets": "carpet",
}
MATERIALITY_COLOR = {
    "white": "white",
    "red": "red",
    "pink": "pink",
    "yellow": "yellow",
    "blue": "blue",
    "green": "green",
    "black": "black",
    "gold": "gold",
    "orange": "orange",
    "grey": "gray",
    "cyan": "cyan",
    "purple": "purple",
}
MATERIALITY_EDITORIAL = {
    "the-importance-of-being-material",
    "diaphanous-and-translucent",
    "god-is-in-the-details",
    "colours",
    "exploring-patterns",
    "perforated",
}


ELEMENT_STRUCTURAL_MATERIAL = {
    "wooden-structures": "timber",
    "steel-structures": "steel",
    "concrete-structures": "concrete",
}
ELEMENT_STRUCTURAL_SYSTEM_CANDIDATE = {
    "wooden-structures": "Timber Frame",
    "steel-structures": "Steel Frame",
    "concrete-structures": "Reinforced Concrete",
}
ELEMENT_STRUCTURAL_STRATEGY = {
    "bearing-walls": "Load-Bearing Walls",
    "massive-walls": "Mass Walls",
}
ELEMENT_FACADE_MATERIAL = {
    "bricks-facades": "brick",
    "glass-facades": "glass",
    "wooden-facades": "timber",
    "metal-claddings": "metal",
    "stone-facades": "stone",
    "terracotta-facades": "terracotta",
}
ELEMENT_FACADE_SYSTEM = {
    "facade-cladding-systems": "Cladding System",
    "slat-facades": "Slatted Screen",
    "solar-shading-systems": "Solar Shading",
}
ELEMENT_ROOF_TYPE = {
    "green-roofs": "Green Roof",
    "curved-roofs": "Curved",
}
ELEMENT_ROOF_FORM = {
    "sloping-roofs": "Sloping",
}
ELEMENT_ROOF_MATERIAL = {
    "glass-roofs": "glass",
}
ELEMENT_PRIMARY_FEATURE = {
    "private-pools": "Private Pool",
    "fireplaces": "Fireplace",
    "courtyards": "Courtyard",
    "patios": "Patio",
    "balconies": "Balcony",
    "terraces": "Terrace",
    "fountains": "Fountain",
    "canopies": "Canopy",
    "arches": "Arch",
    "porches": "Porch",
    "outdoor-stairs": "Outdoor Stair",
    "spiral-stairs": "Spiral Stair",
    "ramps": "Ramp",
    "hanging-walkways": "Hanging Walkway",
    "vertical-gardens": "Vertical Garden",
    "engawa": "Engawa",
    "round-windows": "Round Window",
    "skybridges": "Skybridge",
    "roof-gardens": "Roof Garden",
    "gates": "Gate",
    "shop-windows": "Shop Window",
    "glass-partitions": "Glass Partition",
    "wooden-partitions": "Wooden Partition",
    "stone-walls": "Stone Wall",
    "vaults": "Vault",
}
ELEMENT_SECONDARY_FEATURE = {
    "private-indoor-stairs": "Private Indoor Stair",
    "public-indoor-stairs": "Public Indoor Stair",
    "indoor-stairs": "Indoor Stair",
    "entrances": "Entrance",
    "doors": "Door",
    "windows": "Window",
    "window-frames": "Window Frame",
    "railings": "Railing",
    "columns": "Column",
    "ceilings": "Ceiling",
    "external-pavings": "External Paving",
    "furniture": "Furniture",
    "urban-furniture": "Urban Furniture",
    "bookcases": "Bookcase",
    "curtains": "Curtain",
    "handles": "Handle",
    "chairs-sofas": "Chair/Sofa",
    "tables": "Table",
    "lamps": "Lamp",
    "interior-lighting": "Interior Lighting",
    "sliding-elements": "Sliding Element",
}
ELEMENT_FINISH_MATERIAL = {
    "ceramic-floors": "ceramic floor",
    "stone-floors": "stone floor",
    "wooden-floors": "timber floor",
}
ELEMENT_EDITORIAL = {
    "points-of-view",
    "the-architecture-of-the-corner",
    "skins",
    "design-objects",
}


PRIVATE_ROOM = {
    "kitchens": "Kitchen",
    "dining-rooms": "Dining Room",
    "bathrooms": "Bathroom",
    "home-offices": "Home Office",
    "bedrooms": "Bedroom",
    "living-rooms": "Living Room",
    "wardrobe": "Wardrobe",
    "saunas": "Sauna",
}
PRIVATE_KITCHEN_CONTEXT = {
    "italian-kitchens": "Italian",
    "la-cocina-espanola": "Spanish",
    "japanese-kitchens": "Japanese",
}
PRIVATE_INTERIOR_MATERIAL = {
    "wooden-interiors": "timber",
    "concrete-interiors": "concrete",
    "brick-interiors": "brick",
    "stone-interiors": "stone",
}


PUBLIC_PROGRAM = {
    "offices-and-studios": ("Office", "Office"),
    "gyms": ("Sports", "Sports Centre"),
    "auditoriums": ("Public", "Civic Building"),
    "space-for-art": ("Museum", "Gallery"),
    "libraries": ("Education", "Library"),
    "sacred-spaces": ("Religion", "Religious Building"),
    "classrooms": ("Education", "School"),
    "coffee-shops": ("Hospitality", "Restaurant"),
    "art-galleries": ("Museum", "Gallery"),
    "canteens": ("Hospitality", "Restaurant"),
    "italian-bar": ("Hospitality", "Restaurant"),
    "delis": ("Hospitality", "Restaurant"),
    "clinics": ("Healthcare", "Hospital"),
    "co-working": ("Office", "Office"),
    "wellness-facilities-and-spas": ("Hospitality", "Hotel"),
    "archives": ("Public", "Civic Building"),
    "clubs-discos": ("Hospitality", None),
}
PUBLIC_TYPOLOGY_ONLY = {
    "showrooms-and-shops": "Retail",
    "offices-of-architecture": "Office",
}
PUBLIC_ROOM = {
    "corridors": "Corridor",
    "atriums": "Atrium",
    "receptions": "Reception",
    "interior-voids": "Interior Void",
    "toilets": "Toilet",
    "entrance-halls": "Entrance Hall",
    "lobbies": "Lobby",
    "locker-rooms": "Locker Room",
    "art-studios-and-workshops": "Studio/Workshop",
}
PUBLIC_REUSE = {
    "reused-for-culture": "Adaptive Reuse for Culture",
    "reused-for-working": "Adaptive Reuse for Work",
    "reused-for-hospitality": "Adaptive Reuse for Hospitality",
    "reused-for-learning": "Adaptive Reuse for Education",
    "reused-for-recreation-and-training": "Adaptive Reuse for Recreation",
}


PLAN_PROGRAM = {
    "plans-of-single-family-houses": ("Housing", "House"),
    "plans-of-apartment-blocks": ("Housing", "Apartment"),
    "plans-of-apartments": ("Housing", "Apartment"),
    "plans-of-schools": ("Education", "School"),
    "plans-of-bars-and-restaurants": ("Hospitality", "Restaurant"),
    "plans-of-office-blocks": ("Office", "Office"),
    "plans-of-offices": ("Office", "Office"),
    "plans-of-sport-facilities": ("Sports", "Sports Centre"),
    "plans-of-museums": ("Museum", "Museum"),
    "plans-of-civic-buildings": ("Public", "Civic Building"),
    "plans-of-hotels": ("Hospitality", "Hotel"),
    "plans-of-religious-buildings": ("Religion", "Religious Building"),
    "plans-of-performing-arts-centers": ("Public", "Theatre"),
    "plans-of-kindergartens": ("Education", "Kindergarten"),
    "plans-of-libraries": ("Education", "Library"),
    "plans-of-cultural-centers": ("Public", "Civic Building"),
    "plans-of-transportation-facilities": ("Transport", None),
    "plans-of-public-spaces": ("Landscape", None),
    "plans-of-health-facilities": ("Healthcare", "Hospital"),
    "plans-of-shops": (None, "Retail"),
}
PLAN_CONTENT_SUBJECT = {
    "construction-details-of-facades": "Facade",
    "construction-details-of-interiors": "Interior",
    "construction-details-of-frames": "Frame",
    "construction-details-of-roofs": "Roof",
    "construction-details-of-structures": "Structure",
    "construction-details-of-outdoor-spaces": "Outdoor Space",
    "construction-details-of-stairs": "Stair",
}


TOPIC_MEDIA = {
    "architectural-models": "Model",
    "architectural-drawings": "Drawing",
    "architects-notebooks": "Notebook/Sketch",
    "italian-drawings": "Drawing",
    "reportages": "Reportage",
    "portraits": "Portrait",
    "by-night": "Night Photography",
}
TOPIC_INTERVENTION = {
    "additions": "Addition",
    "critical-conservation-transformative-reuse": "Adaptive Reuse",
    "restored-and-reused": "Restoration/Reuse",
    "repurposed-recycled-reused": "Adaptive Reuse",
    "italian-restorations": "Restoration",
    "turning-from-brown-to-green": "Brownfield Regeneration",
}
TOPIC_STATUS = {
    "wip-work-in-progress": "Work in Progress",
    "ephemeral": "Temporary",
    "forgotten-interrupted": "Interrupted/Abandoned",
}
TOPIC_CONTEXT = {
    "building-in-landscape": "Landscape",
    "building-in-urban-context": "Urban",
    "building-in-historical-context": "Historic",
    "architecture-and-water": "Waterfront/Water",
    "into-the-wild": "Wild/Natural",
    "mountains-architecture": "Mountain",
    "seashore-architecture": "Coastal",
    "below-ground-zero": "Underground",
    "building-in-between": "Infill/Between Buildings",
    "rural-modernity": "Rural",
}
TOPIC_STYLE = {
    "vernacular": "Vernacular",
    "brutalist": "Brutalist",
}
TOPIC_ADDITIONAL = {
    "modern-heritage": ("heritage_context", "Modern Heritage"),
    "urban-facades": ("facade_context", "Urban Facade"),
    "extra-small": ("scale", "XS"),
    "cantilevers-dialogues-with-gravity": ("form_strategy", "Cantilever"),
    "enfilades": ("spatial_strategy", "Enfilade"),
    "building-high": ("form_strategy", "High-Rise"),
    "light-shadows": ("design_strategy", "Light and Shadow"),
    "the-fifth-facade": ("design_strategy", "Fifth Facade/Roof"),
    "radical-sustainability": ("design_strategy", "Sustainability"),
    "post-industrial-architecture": ("previous_use_context", "Industrial"),
}


def _mapping(
    axis: str,
    value: str,
    *,
    scope: str = "building",
    kind: str = "direct",
    confidence: float = 0.9,
    priority: int = 50,
    tier: str = "primary",
    notes: Optional[str] = None,
) -> TagMapping:
    return TagMapping(
        axis=axis,
        value=value,
        target_scope=scope,
        mapping_kind=kind,
        confidence=confidence,
        priority=priority,
        search_tier=tier,
        notes=notes,
    )


def mappings_for_tag(album_slug: str, tag_slug: str, label: str) -> list[TagMapping]:
    """Project one Divisare source tag into zero or more reviewed claims."""

    album = (album_slug or "").strip().casefold()
    slug = (tag_slug or "").strip().casefold()
    display = clean_scalar(label) or slug.replace("-", " ").title()
    out: list[TagMapping] = []

    if album == "types":
        out.append(_mapping("source_typology", display, confidence=1.0, priority=100))
        if slug == "private-gardens":
            out.append(
                _mapping(
                    "architectural_element",
                    "Private Garden",
                    confidence=0.9,
                    priority=75,
                )
            )
        if slug in TYPE_TYPOLOGY:
            out.append(_mapping("typology", TYPE_TYPOLOGY[slug], confidence=0.95, priority=95))
        if slug in TYPE_WORK_TYPE:
            out.append(
                _mapping(
                    "work_type",
                    TYPE_WORK_TYPE[slug],
                    confidence=0.92,
                    priority=85,
                )
            )
        if slug in TYPE_PROGRAM:
            if slug in TYPE_PROGRAM_SUPPORTING:
                out.append(
                    _mapping(
                        "program",
                        TYPE_PROGRAM[slug],
                        kind="supporting",
                        confidence=0.75,
                        priority=65,
                        notes="Type label is compatible with several building programs.",
                    )
                )
            else:
                confidence = 0.98 if slug in TYPE_TYPOLOGY else 0.92
                out.append(
                    _mapping(
                        "program",
                        TYPE_PROGRAM[slug],
                        confidence=confidence,
                        priority=95,
                    )
                )
        return out

    if album == "houses":
        out.append(_mapping("source_typology", display, confidence=1.0, priority=90))
        if slug in HOUSE_EXCLUSIONS:
            out.append(
                _mapping(
                    "source_topic",
                    display,
                    kind="exclusion",
                    confidence=1.0,
                    priority=0,
                    tier="hidden",
                    notes="Not automatically treated as a real residential building.",
                )
            )
            return out
        house_confidence = 0.78 if slug == "tree-houses" else 0.92
        out.extend(
            [
                _mapping("typology", "House", confidence=house_confidence, priority=88),
                _mapping("program", "Housing", confidence=house_confidence, priority=88),
            ]
        )
        if slug in HOUSE_MATERIAL:
            out.append(
                _mapping(
                    "material",
                    HOUSE_MATERIAL[slug],
                    kind="supporting",
                    confidence=0.82,
                    priority=70,
                    notes="Whole-house editorial material cue; component binding is unknown.",
                )
            )
        if slug in HOUSE_COUNTRY:
            out.append(
                _mapping(
                    "country_candidate",
                    HOUSE_COUNTRY[slug],
                    kind="supporting",
                    confidence=0.9,
                    priority=75,
                    tier="secondary",
                    notes="Corroboration/fallback only; never override structured location.",
                )
            )
        if slug in HOUSE_REGION:
            out.append(
                _mapping(
                    "region_context",
                    HOUSE_REGION[slug],
                    confidence=0.85,
                    priority=60,
                    tier="secondary",
                )
            )
        if slug in HOUSE_CONTEXT:
            out.append(_mapping("site_context", HOUSE_CONTEXT[slug], confidence=0.85, priority=65))
        if slug == "windowless-houses":
            out.append(
                _mapping(
                    "opening_strategy",
                    "Windowless",
                    confidence=0.9,
                    priority=75,
                )
            )
        if slug == "restored-houses":
            out.append(_mapping("intervention_type", "Restoration", confidence=0.9, priority=80))
        if slug == "microhouses":
            out.append(_mapping("scale", "XS", confidence=0.85, priority=70))
        return out

    if album == "materiality":
        if slug in MATERIALITY_MATERIAL:
            out.append(_mapping("material", MATERIALITY_MATERIAL[slug], confidence=0.92, priority=85))
        elif slug in MATERIALITY_COLOR:
            out.append(_mapping("color", MATERIALITY_COLOR[slug], confidence=0.9, priority=80))
        elif slug == "perforated":
            out.append(
                _mapping(
                    "facade_pattern",
                    "Perforated",
                    kind="supporting",
                    confidence=0.72,
                    priority=55,
                    notes="Editorial materiality cue; confirm against imagery or text.",
                )
            )
        elif slug == "diaphanous-and-translucent":
            out.append(
                _mapping(
                    "optical_quality",
                    "Translucent",
                    kind="supporting",
                    confidence=0.7,
                    priority=50,
                    tier="secondary",
                )
            )
        else:
            out.append(
                _mapping(
                    "source_topic",
                    display,
                    scope="article",
                    kind="editorial",
                    confidence=1.0,
                    priority=10,
                    tier="hidden",
                )
            )
        return out

    if album == "elements":
        handled = False
        if slug in ELEMENT_STRUCTURAL_MATERIAL:
            handled = True
            out.append(
                _mapping(
                    "structural_material",
                    ELEMENT_STRUCTURAL_MATERIAL[slug],
                    confidence=0.92,
                    priority=90,
                )
            )
            out.append(
                _mapping(
                    "material",
                    ELEMENT_STRUCTURAL_MATERIAL[slug],
                    kind="supporting",
                    confidence=0.82,
                    priority=70,
                    notes="Structural material also supports whole-building material search.",
                )
            )
        if slug in ELEMENT_STRUCTURAL_SYSTEM_CANDIDATE:
            handled = True
            out.append(
                _mapping(
                    "structural_system",
                    ELEMENT_STRUCTURAL_SYSTEM_CANDIDATE[slug],
                    kind="supporting",
                    confidence=0.68,
                    priority=55,
                    notes="Material alone does not establish the structural system.",
                )
            )
        if slug in ELEMENT_STRUCTURAL_STRATEGY:
            handled = True
            out.append(
                _mapping(
                    "structural_strategy",
                    ELEMENT_STRUCTURAL_STRATEGY[slug],
                    confidence=0.9,
                    priority=85,
                )
            )
        if slug in ELEMENT_FACADE_MATERIAL:
            handled = True
            out.append(_mapping("facade_material", ELEMENT_FACADE_MATERIAL[slug], confidence=0.92, priority=88))
        if slug in ELEMENT_FACADE_SYSTEM:
            handled = True
            tier = "secondary" if slug == "facade-cladding-systems" else "primary"
            out.append(
                _mapping(
                    "facade_system",
                    ELEMENT_FACADE_SYSTEM[slug],
                    confidence=0.88,
                    priority=80,
                    tier=tier,
                )
            )
            if slug == "slat-facades":
                out.append(
                    _mapping(
                        "facade_pattern",
                        "Louvered",
                        kind="supporting",
                        confidence=0.78,
                        priority=65,
                    )
                )
        if slug in ELEMENT_ROOF_TYPE:
            handled = True
            out.append(_mapping("roof_type", ELEMENT_ROOF_TYPE[slug], confidence=0.9, priority=85))
        if slug in ELEMENT_ROOF_FORM:
            handled = True
            out.append(_mapping("roof_form", ELEMENT_ROOF_FORM[slug], confidence=0.9, priority=82))
        if slug in ELEMENT_ROOF_MATERIAL:
            handled = True
            out.append(
                _mapping(
                    "roof_material",
                    ELEMENT_ROOF_MATERIAL[slug],
                    confidence=0.9,
                    priority=82,
                )
            )
        if slug in ELEMENT_PRIMARY_FEATURE:
            handled = True
            out.append(
                _mapping(
                    "architectural_element",
                    ELEMENT_PRIMARY_FEATURE[slug],
                    confidence=0.88,
                    priority=75,
                )
            )
        if slug in ELEMENT_SECONDARY_FEATURE:
            handled = True
            out.append(
                _mapping(
                    "architectural_element",
                    ELEMENT_SECONDARY_FEATURE[slug],
                    confidence=0.8,
                    priority=45,
                    tier="secondary",
                    notes="Source-selected feature, not a complete element inventory.",
                )
            )
        if slug in ELEMENT_FINISH_MATERIAL:
            handled = True
            out.append(_mapping("finish_material", ELEMENT_FINISH_MATERIAL[slug], confidence=0.88, priority=70))
        if slug == "daylighting":
            handled = True
            out.append(_mapping("design_strategy", "Daylighting", confidence=0.82, priority=60, tier="secondary"))
        if not handled:
            out.append(
                _mapping(
                    "source_topic",
                    display,
                    scope="article",
                    kind="editorial",
                    confidence=1.0,
                    priority=10,
                    tier="hidden",
                )
            )
        return out

    if album == "private-interiors":
        if slug in PRIVATE_ROOM:
            out.append(
                _mapping(
                    "room_type",
                    PRIVATE_ROOM[slug],
                    scope="article",
                    confidence=0.9,
                    priority=70,
                    tier="secondary",
                )
            )
        elif slug in PRIVATE_KITCHEN_CONTEXT:
            out.extend(
                [
                    _mapping(
                        "room_type",
                        "Kitchen",
                        scope="article",
                        confidence=0.9,
                        priority=70,
                        tier="secondary",
                    ),
                    _mapping(
                        "interior_context",
                        PRIVATE_KITCHEN_CONTEXT[slug],
                        scope="article",
                        kind="supporting",
                        confidence=0.78,
                        priority=55,
                        tier="secondary",
                    ),
                ]
            )
        elif slug in PRIVATE_INTERIOR_MATERIAL:
            out.append(
                _mapping(
                    "interior_material",
                    PRIVATE_INTERIOR_MATERIAL[slug],
                    scope="article",
                    confidence=0.85,
                    priority=65,
                    tier="secondary",
                )
            )
        elif slug == "apartment-renovations":
            out.extend(
                [
                    _mapping("intervention_type", "Renovation", confidence=0.92, priority=85),
                    _mapping("typology", "Apartment", kind="supporting", confidence=0.78, priority=60),
                    _mapping("program", "Housing", kind="supporting", confidence=0.72, priority=55),
                ]
            )
        elif slug == "reused-for-living":
            out.extend(
                [
                    _mapping("intervention_type", "Adaptive Reuse for Housing", confidence=0.9, priority=80),
                    _mapping("program", "Housing", kind="supporting", confidence=0.75, priority=55),
                ]
            )
        elif slug in {"duplex", "lofts-and-penthouses"}:
            out.extend(
                [
                    _mapping("typology", "Apartment", kind="supporting", confidence=0.78, priority=60),
                    _mapping("program", "Housing", kind="supporting", confidence=0.72, priority=55),
                ]
            )
        else:
            out.append(
                _mapping(
                    "interior_context",
                    display,
                    scope="article",
                    kind="editorial",
                    confidence=1.0,
                    priority=20,
                    tier="hidden",
                )
            )
        return out

    if album == "public-interiors":
        if slug in PUBLIC_PROGRAM:
            program, typology = PUBLIC_PROGRAM[slug]
            out.append(
                _mapping("program", program, kind="supporting", confidence=0.76, priority=60)
            )
            if typology:
                out.append(
                    _mapping(
                        "typology",
                        typology,
                        kind="supporting",
                        confidence=0.74,
                        priority=58,
                    )
                )
        elif slug in PUBLIC_TYPOLOGY_ONLY:
            out.append(
                _mapping(
                    "typology",
                    PUBLIC_TYPOLOGY_ONLY[slug],
                    kind="supporting",
                    confidence=0.72,
                    priority=55,
                )
            )
        elif slug in PUBLIC_ROOM:
            out.append(
                _mapping(
                    "room_type",
                    PUBLIC_ROOM[slug],
                    scope="article",
                    confidence=0.88,
                    priority=65,
                    tier="secondary",
                )
            )
        elif slug in PUBLIC_REUSE:
            out.append(_mapping("intervention_type", PUBLIC_REUSE[slug], confidence=0.9, priority=80))
        else:
            out.append(
                _mapping(
                    "interior_context",
                    display,
                    scope="article",
                    kind="editorial",
                    confidence=1.0,
                    priority=20,
                    tier="hidden",
                )
            )
        return out

    if album == "plans-details":
        if slug.startswith("plans-of-"):
            out.append(
                _mapping(
                    "content_hint",
                    "Plan",
                    scope="article",
                    kind="supporting",
                    confidence=0.8,
                    priority=70,
                    tier="secondary",
                    notes="Gallery-level prior only; never propagate to every image.",
                )
            )
        elif slug.startswith("construction-details-of-"):
            out.append(
                _mapping(
                    "content_hint",
                    "Construction Detail",
                    scope="article",
                    kind="supporting",
                    confidence=0.82,
                    priority=70,
                    tier="secondary",
                )
            )
            if slug in PLAN_CONTENT_SUBJECT:
                out.append(
                    _mapping(
                        "content_subject",
                        PLAN_CONTENT_SUBJECT[slug],
                        scope="article",
                        kind="supporting",
                        confidence=0.82,
                        priority=68,
                        tier="secondary",
                    )
                )
        elif slug == "sections":
            out.append(
                _mapping(
                    "content_hint",
                    "Section",
                    scope="article",
                    kind="supporting",
                    confidence=0.82,
                    priority=70,
                    tier="secondary",
                )
            )
        if slug in PLAN_PROGRAM:
            program, typology = PLAN_PROGRAM[slug]
            if program:
                out.append(
                    _mapping(
                        "program",
                        program,
                        kind="supporting",
                        confidence=0.78,
                        priority=65,
                    )
                )
            if typology:
                out.append(
                    _mapping(
                        "typology",
                        typology,
                        kind="supporting",
                        confidence=0.8,
                        priority=68,
                    )
                )
        return out

    if album == "topics":
        if slug in TOPIC_MEDIA:
            out.append(
                _mapping(
                    "content_hint",
                    TOPIC_MEDIA[slug],
                    scope="article",
                    kind="supporting",
                    confidence=0.78,
                    priority=60,
                    tier="secondary",
                    notes="Gallery-level prior only.",
                )
            )
        elif slug in TOPIC_INTERVENTION:
            out.append(_mapping("intervention_type", TOPIC_INTERVENTION[slug], confidence=0.88, priority=78))
        elif slug in TOPIC_STATUS:
            out.append(
                _mapping(
                    "project_status",
                    TOPIC_STATUS[slug],
                    kind="supporting",
                    confidence=0.82,
                    priority=65,
                    tier="secondary",
                    notes="Status is relative to the Divisare publication snapshot.",
                )
            )
        elif slug in TOPIC_CONTEXT:
            out.append(
                _mapping(
                    "site_context",
                    TOPIC_CONTEXT[slug],
                    kind="supporting",
                    confidence=0.74,
                    priority=58,
                )
            )
        elif slug in TOPIC_STYLE:
            out.append(
                _mapping(
                    "style",
                    TOPIC_STYLE[slug],
                    confidence=0.86,
                    priority=72,
                )
            )
        elif slug in TOPIC_ADDITIONAL:
            axis, value = TOPIC_ADDITIONAL[slug]
            out.append(
                _mapping(
                    axis,
                    value,
                    kind="supporting",
                    confidence=0.7,
                    priority=50,
                    tier="secondary",
                )
            )
        else:
            out.append(
                _mapping(
                    "source_topic",
                    display,
                    scope="article",
                    kind="editorial",
                    confidence=1.0,
                    priority=10,
                    tier="hidden",
                )
            )
        return out

    if album == "ideas":
        return [
            _mapping(
                "source_topic",
                display,
                scope="article",
                kind="editorial",
                confidence=1.0,
                priority=5,
                tier="hidden",
                notes="Concept/editorial collection; not a built-program assertion.",
            )
        ]

    if album == "cities":
        return [
            _mapping(
                "city_candidate",
                display,
                kind="supporting",
                confidence=0.95,
                priority=85,
                tier="secondary",
                notes="Use for location corroboration; resolve through a gazetteer.",
            )
        ]

    return out


def search_tier_rank(value: str) -> int:
    return {"hidden": 0, "secondary": 1, "primary": 2}.get(value, 0)


def confidence_class(value: float) -> str:
    if value >= 0.9:
        return "high"
    if value >= 0.75:
        return "medium"
    return "low"
