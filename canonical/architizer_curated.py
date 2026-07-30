"""Deterministic, source-specific policy for the Architizer curated database.

The raw crawler database is the source of truth.  This module only turns
explicit Architizer fields into conservative claims; it does not call a
network service, an LLM, or the production canonical vocabulary.  In
particular, an Architizer project id is a source-record id, not a real-world
building id, and an ``article:tag`` is evidence rather than a silent default.

The category table below is intentionally closed over the 78 tags observed in
the input database identified by the 2026-07-31 handoff manifest.  Unknown
tags and ``Other`` return no mapping, so the builder can preserve them as
unmapped raw occurrences.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = "architizer-curated-schema-v1.3"
POLICY_VERSION = "architizer-curation-policy-v1.4"
TAXONOMY_VERSION = "architizer-article-tag-taxonomy-v1.1"
ASSET_KEY_VERSION = "architizer-host-path-asset-v1"
CLUSTER_VERSION = "architizer-strict-internal-cluster-v2"


@dataclass(frozen=True)
class CategoryMapping:
    """One normalized claim supported by a raw Architizer category."""

    axis: str
    value: str
    mapping_kind: str
    confidence: float
    status: str
    rule_id: str
    evidence: str
    target_scope: str = "building"


@dataclass(frozen=True)
class ImageIdentity:
    """Stable identity for one source image, independent of known transforms."""

    asset_id: str
    asset_key: str
    normalized_url: str
    host: str
    path: str
    is_placeholder_candidate: bool


def clean_scalar(value: Any) -> Optional[str]:
    """Collapse presentation whitespace while retaining the source text."""

    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("utf-8", errors="replace")
    text = str(value)
    text = text.replace("\u00a0", " ").replace("\ufeff", "")
    text = unicodedata.normalize("NFC", text)
    text = " ".join(text.split())
    return text or None


def normalize_identity_text(value: Any) -> str:
    """Normalize a project or firm name for blocking, never for display."""

    text = clean_scalar(value) or ""
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("_", " ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


_GENERIC_PROJECT_NAMES = {
    "apartment",
    "apartments",
    "building",
    "casa",
    "community center",
    "cultural center",
    "factory",
    "gallery",
    "hotel",
    "house",
    "library",
    "museum",
    "office",
    "offices",
    "park",
    "pavilion",
    "project",
    "restaurant",
    "school",
    "shop",
    "store",
    "tower",
    "villa",
    "warehouse",
}
_GENERIC_BASE = (
    r"(?:apartment|building|casa|community center|cultural center|factory|"
    r"gallery|hotel|house|library|museum|office|park|pavilion|project|"
    r"restaurant|school|shop|store|tower|villa|warehouse)"
)
_GENERIC_PROJECT_RE = re.compile(
    rf"^(?:the\s+)?{_GENERIC_BASE}"
    r"(?:\s+(?:no|number|project|extension|renovation|new|prototype))?"
    r"(?:\s+(?:[a-z](?:\s+\d{1,4})?|\d{1,4}|[ivxlcdm]{1,8}))?$",
    re.IGNORECASE,
)
_GENERIC_LOCATION_RE = re.compile(
    rf"^(?:the\s+)?{_GENERIC_BASE}\s+(?:in|at)\s+[\w-]+(?:\s+[\w-]+)?$",
    re.IGNORECASE,
)


def is_generic_project_name(value: Any) -> bool:
    """Reject names too generic to support automatic duplicate clustering.

    Short numbered labels such as ``House 01`` and location-only labels such
    as ``Office in Seoul`` remain useful display text, but are not identities.
    """

    normalized = normalize_identity_text(value)
    if not normalized or len(normalized) < 5:
        return True
    return (
        normalized in _GENERIC_PROJECT_NAMES
        or bool(_GENERIC_PROJECT_RE.fullmatch(normalized))
        or bool(_GENERIC_LOCATION_RE.fullmatch(normalized))
    )


def parse_json_list(value: Any) -> list[Any]:
    """Decode a crawler JSON-list field without discarding malformed input.

    A non-JSON scalar is returned as a one-item list.  The builder can then
    preserve the occurrence and attach a malformed-JSON QA issue instead of
    silently losing source data.
    """

    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    text = clean_scalar(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [text]
    if parsed is None:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def parse_json_dict(value: Any) -> dict[str, Any]:
    """Decode a crawler JSON-object field; non-objects yield an empty dict."""

    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    text = clean_scalar(value)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# The broad parent tags support search/review candidates but are insufficient
# for a confirmed scalar facet.  Composite parents intentionally emit multiple
# candidates so the resolver must abstain rather than select an arbitrary side.
_BROAD_CATEGORY_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "Commercial": (("program", "Commercial"),),
    "Cultural": (("program", "Cultural"),),
    "Educational": (("program", "Education"),),
    "Government + Health": (
        ("program", "Government"),
        ("program", "Healthcare"),
    ),
    "Hospitality + Sport": (
        ("program", "Hospitality"),
        ("program", "Sports"),
    ),
    "Industrial": (("program", "Industrial"),),
    "Landscape + Planning": (
        ("work_type", "Landscape Architecture"),
        ("work_type", "Urban Planning"),
    ),
    "Residential": (("program", "Housing"),),
    "Transport + Infrastructure": (
        ("program", "Transport"),
        ("work_type", "Infrastructure"),
    ),
}

BROAD_CATEGORIES = frozenset(_BROAD_CATEGORY_RULES)

# Architizer's 69 leaf labels have one source-taxonomy parent.  The input can
# contain several broad/leaf pairs on the same project, so this dictionary is
# the hierarchy definition, not a claim that a project has only one program.
# ``Other`` is included for hierarchy auditing but remains explicitly unmapped.
CATEGORY_PARENT: dict[str, str] = {
    "Aging Facility": "Government + Health",
    "Airport": "Transport + Infrastructure",
    "Amusement Park": "Hospitality + Sport",
    "Apartment": "Residential",
    "Auditorium": "Educational",
    "Bank": "Commercial",
    "Bar/Nightclub": "Hospitality + Sport",
    "Bicycles": "Transport + Infrastructure",
    "Bridge": "Transport + Infrastructure",
    "Bus": "Transport + Infrastructure",
    "Cemetery": "Landscape + Planning",
    "City Hall": "Government + Health",
    "Community Center": "Government + Health",
    "Consulate/Embassy": "Government + Health",
    "Court/Post Office": "Government + Health",
    "Cultural Center": "Cultural",
    "Elementary School": "Educational",
    "Exhibition Center": "Commercial",
    "Factory": "Industrial",
    "Farm": "Industrial",
    "Fire/Police/Military": "Government + Health",
    "Gallery": "Cultural",
    "Hall/Theater": "Cultural",
    "High School": "Educational",
    "Highway": "Transport + Infrastructure",
    "Hospital": "Government + Health",
    "Hotel": "Hospitality + Sport",
    "Information Center": "Hospitality + Sport",
    "Laboratory": "Industrial",
    "Library": "Educational",
    "Marina and Ports": "Transport + Infrastructure",
    "Masterplan": "Landscape + Planning",
    "Medical Facility": "Government + Health",
    "Memorial": "Cultural",
    "Movie Theater": "Hospitality + Sport",
    "Multi Unit Housing": "Residential",
    "Museum": "Cultural",
    "Nursery": "Educational",
    "Observation Tower": "Cultural",
    "Office": "Commercial",
    "Other": "Educational",
    "Parking": "Transport + Infrastructure",
    "Pavilion": "Cultural",
    "Playground": "Landscape + Planning",
    "Pop-Up": "Commercial",
    "Power Plant": "Industrial",
    "Private Garden": "Landscape + Planning",
    "Private House": "Residential",
    "Public Park": "Landscape + Planning",
    "Religious": "Cultural",
    "Research Facility": "Industrial",
    "Restaurant": "Hospitality + Sport",
    "Retail": "Commercial",
    "Sculpture": "Cultural",
    "Shopping Mall": "Commercial",
    "Showroom": "Commercial",
    "Sports Center": "Hospitality + Sport",
    "Stadium": "Hospitality + Sport",
    "Student Housing": "Residential",
    "Supermarket": "Commercial",
    "Train/Subway": "Transport + Infrastructure",
    "University": "Educational",
    "Urban Green Space": "Landscape + Planning",
    "Warehouse": "Industrial",
    "Water Facility": "Industrial",
    "Waterway/Wetland": "Landscape + Planning",
    "Wellness/Spa": "Hospitality + Sport",
    "Winery": "Industrial",
    "Zoo/Aquarium": "Hospitality + Sport",
}


# Leaf tags are direct source taxonomy evidence.  Values deliberately stay
# source-specific and do not import or mutate core.vocab.
_LEAF_CATEGORY_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "Aging Facility": (
        ("program", "Healthcare"),
        ("typology", "Aging Facility"),
    ),
    "Airport": (
        ("program", "Transport"),
        ("typology", "Airport"),
        ("work_type", "Infrastructure"),
    ),
    "Amusement Park": (("typology", "Amusement Park"),),
    "Apartment": (
        ("program", "Housing"),
        ("typology", "Apartment"),
    ),
    "Auditorium": (("typology", "Auditorium"),),
    "Bank": (
        ("program", "Commercial"),
        ("typology", "Bank"),
    ),
    "Bar/Nightclub": (
        ("program", "Hospitality"),
        ("typology", "Bar/Nightclub"),
    ),
    "Bridge": (
        ("program", "Transport"),
        ("typology", "Bridge"),
        ("work_type", "Infrastructure"),
    ),
    "Cemetery": (("typology", "Cemetery"),),
    "City Hall": (
        ("program", "Government"),
        ("typology", "City Hall"),
    ),
    "Community Center": (
        ("program", "Civic"),
        ("typology", "Community Center"),
    ),
    "Consulate/Embassy": (
        ("program", "Government"),
        ("typology", "Consulate/Embassy"),
    ),
    "Court/Post Office": (("program", "Government"),),
    "Cultural Center": (
        ("program", "Cultural"),
        ("typology", "Cultural Center"),
    ),
    "Elementary School": (
        ("program", "Education"),
        ("typology", "Elementary School"),
    ),
    "Exhibition Center": (("typology", "Exhibition Center"),),
    "Factory": (
        ("program", "Industrial"),
        ("typology", "Factory"),
    ),
    "Farm": (
        ("program", "Agriculture"),
        ("typology", "Farm"),
    ),
    "Fire/Police/Military": (
        ("program", "Government"),
        ("typology", "Fire/Police/Military Facility"),
    ),
    "Gallery": (
        ("program", "Cultural"),
        ("typology", "Gallery"),
    ),
    "Hall/Theater": (
        ("program", "Cultural"),
        ("typology", "Hall/Theater"),
    ),
    "High School": (
        ("program", "Education"),
        ("typology", "High School"),
    ),
    "Highway": (
        ("program", "Transport"),
        ("work_type", "Infrastructure"),
    ),
    "Hospital": (
        ("program", "Healthcare"),
        ("typology", "Hospital"),
    ),
    "Hotel": (
        ("program", "Hospitality"),
        ("typology", "Hotel"),
    ),
    "Information Center": (("typology", "Information Center"),),
    "Laboratory": (
        ("program", "Research"),
        ("typology", "Laboratory"),
    ),
    "Library": (("typology", "Library"),),
    "Marina and Ports": (
        ("program", "Transport"),
        ("typology", "Marina/Port"),
        ("work_type", "Infrastructure"),
    ),
    "Masterplan": (("work_type", "Urban Planning"),),
    "Medical Facility": (
        ("program", "Healthcare"),
        ("typology", "Medical Facility"),
    ),
    "Memorial": (("typology", "Memorial"),),
    "Movie Theater": (("typology", "Movie Theater"),),
    "Multi Unit Housing": (
        ("program", "Housing"),
        ("typology", "Multi-unit Housing"),
    ),
    "Museum": (
        ("program", "Cultural"),
        ("typology", "Museum"),
    ),
    "Observation Tower": (("typology", "Observation Tower"),),
    "Office": (
        ("program", "Commercial"),
        ("typology", "Office"),
    ),
    "Parking": (
        ("program", "Transport"),
        ("typology", "Parking Facility"),
        ("work_type", "Infrastructure"),
    ),
    "Pavilion": (("typology", "Pavilion"),),
    "Playground": (
        ("typology", "Playground"),
        ("work_type", "Landscape Architecture"),
    ),
    "Pop-Up": (("work_type", "Temporary Installation"),),
    "Power Plant": (
        ("program", "Industrial"),
        ("typology", "Power Plant"),
        ("work_type", "Infrastructure"),
    ),
    "Private Garden": (
        ("typology", "Private Garden"),
        ("work_type", "Landscape Architecture"),
    ),
    "Private House": (
        ("program", "Housing"),
        ("typology", "House"),
    ),
    "Public Park": (
        ("typology", "Public Park"),
        ("work_type", "Landscape Architecture"),
    ),
    "Religious": (
        ("program", "Religious"),
        ("typology", "Religious Building"),
    ),
    "Research Facility": (
        ("program", "Research"),
        ("typology", "Research Facility"),
    ),
    "Restaurant": (
        ("program", "Hospitality"),
        ("typology", "Restaurant"),
    ),
    "Retail": (
        ("program", "Commercial"),
        ("typology", "Retail"),
    ),
    "Sculpture": (("work_type", "Art Installation"),),
    "Shopping Mall": (
        ("program", "Commercial"),
        ("typology", "Shopping Mall"),
    ),
    "Showroom": (
        ("program", "Commercial"),
        ("typology", "Showroom"),
    ),
    "Sports Center": (
        ("program", "Sports"),
        ("typology", "Sports Center"),
    ),
    "Stadium": (
        ("program", "Sports"),
        ("typology", "Stadium"),
    ),
    "Student Housing": (
        ("program", "Housing"),
        ("typology", "Student Housing"),
    ),
    "Supermarket": (
        ("program", "Commercial"),
        ("typology", "Supermarket"),
    ),
    "Train/Subway": (
        ("program", "Transport"),
        ("typology", "Rail/Metro Station"),
        ("work_type", "Infrastructure"),
    ),
    "University": (
        ("program", "Education"),
        ("typology", "University"),
    ),
    "Urban Green Space": (
        ("typology", "Urban Green Space"),
        ("work_type", "Landscape Architecture"),
    ),
    "Warehouse": (
        ("program", "Industrial"),
        ("typology", "Warehouse"),
    ),
    "Water Facility": (
        ("program", "Infrastructure"),
        ("typology", "Water Facility"),
        ("work_type", "Infrastructure"),
    ),
    "Waterway/Wetland": (
        ("typology", "Waterway/Wetland"),
        ("work_type", "Landscape Architecture"),
    ),
    "Wellness/Spa": (("typology", "Wellness/Spa"),),
    "Winery": (
        ("program", "Industrial"),
        ("typology", "Winery"),
    ),
    "Zoo/Aquarium": (("typology", "Zoo/Aquarium"),),
}


# These leaf-looking tags do not identify a stable building typology on their
# own.  They remain candidates even though they are below a broad parent in the
# Architizer taxonomy.
_AMBIGUOUS_LEAF_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "Bicycles": (
        ("program", "Transport"),
        ("work_type", "Infrastructure"),
    ),
    "Bus": (
        ("program", "Transport"),
        ("typology", "Bus Facility"),
        ("work_type", "Infrastructure"),
    ),
    "Nursery": (
        ("program", "Education"),
        ("typology", "Nursery School"),
    ),
}


ARCHITIZER_ARTICLE_TAGS = frozenset(
    {
        "Aging Facility",
        "Airport",
        "Amusement Park",
        "Apartment",
        "Auditorium",
        "Bank",
        "Bar/Nightclub",
        "Bicycles",
        "Bridge",
        "Bus",
        "Cemetery",
        "City Hall",
        "Commercial",
        "Community Center",
        "Consulate/Embassy",
        "Court/Post Office",
        "Cultural",
        "Cultural Center",
        "Educational",
        "Elementary School",
        "Exhibition Center",
        "Factory",
        "Farm",
        "Fire/Police/Military",
        "Gallery",
        "Government + Health",
        "Hall/Theater",
        "High School",
        "Highway",
        "Hospital",
        "Hospitality + Sport",
        "Hotel",
        "Industrial",
        "Information Center",
        "Laboratory",
        "Landscape + Planning",
        "Library",
        "Marina and Ports",
        "Masterplan",
        "Medical Facility",
        "Memorial",
        "Movie Theater",
        "Multi Unit Housing",
        "Museum",
        "Nursery",
        "Observation Tower",
        "Office",
        "Other",
        "Parking",
        "Pavilion",
        "Playground",
        "Pop-Up",
        "Power Plant",
        "Private Garden",
        "Private House",
        "Public Park",
        "Religious",
        "Research Facility",
        "Residential",
        "Restaurant",
        "Retail",
        "Sculpture",
        "Shopping Mall",
        "Showroom",
        "Sports Center",
        "Stadium",
        "Student Housing",
        "Supermarket",
        "Train/Subway",
        "Transport + Infrastructure",
        "University",
        "Urban Green Space",
        "Warehouse",
        "Water Facility",
        "Waterway/Wetland",
        "Wellness/Spa",
        "Winery",
        "Zoo/Aquarium",
    }
)

_RULED_CATEGORIES = (
    set(_BROAD_CATEGORY_RULES)
    | set(_LEAF_CATEGORY_RULES)
    | set(_AMBIGUOUS_LEAF_RULES)
    | {"Other"}
)
if _RULED_CATEGORIES != set(ARCHITIZER_ARTICLE_TAGS):
    raise RuntimeError("Architizer category policy must account for all 78 raw tags")
if set(CATEGORY_PARENT) != set(ARCHITIZER_ARTICLE_TAGS) - set(BROAD_CATEGORIES):
    raise RuntimeError("Architizer parent policy must account for all 69 leaf tags")
if not set(CATEGORY_PARENT.values()).issubset(BROAD_CATEGORIES):
    raise RuntimeError("Architizer leaf parents must be one of the nine broad tags")

_CATEGORY_LOOKUP = {
    normalize_identity_text(category): category
    for category in ARCHITIZER_ARTICLE_TAGS
}


def _rule_slug(value: str) -> str:
    return normalize_identity_text(value).replace(" ", "-")


def mappings_for_category(raw: Any) -> list[CategoryMapping]:
    """Map one raw ``article:tag`` into reviewed source-specific claims.

    Broad parents and ambiguous leaves produce ``candidate`` claims.  Direct
    leaves produce ``confirmed`` claims.  ``Other``, blank values, and unknown
    future tags return ``[]`` so the builder records an unmapped occurrence.
    No category creates a material claim.
    """

    category = _CATEGORY_LOOKUP.get(normalize_identity_text(raw))
    if not category or category == "Other":
        return []

    if category in _BROAD_CATEGORY_RULES:
        rules = _BROAD_CATEGORY_RULES[category]
        kind = "supporting"
        status = "candidate"
        confidence = 0.68
        evidence_note = "broad parent category; supporting evidence only"
    elif category in _AMBIGUOUS_LEAF_RULES:
        rules = _AMBIGUOUS_LEAF_RULES[category]
        kind = "supporting"
        status = "candidate"
        confidence = 0.72
        evidence_note = "leaf label is source-explicit but semantically ambiguous"
    else:
        rules = _LEAF_CATEGORY_RULES[category]
        kind = "direct"
        status = "confirmed"
        confidence = 0.95
        evidence_note = "source-explicit leaf category"

    category_slug = _rule_slug(category)
    out: list[CategoryMapping] = []
    for axis, value in rules:
        rule_id = (
            f"{TAXONOMY_VERSION}.{category_slug}.{axis}.{_rule_slug(value)}"
        )
        out.append(
            CategoryMapping(
                axis=axis,
                value=value,
                mapping_kind=kind,
                confidence=confidence,
                status=status,
                rule_id=rule_id,
                evidence=(
                    f'Architizer article:tag "{category}" ({evidence_note}).'
                ),
            )
        )
    return out


_ALLOWED_IMAGE_HOSTS = {
    "architizer-prod.imgix.net",
    "static-web-prod.arc.ht",
}
# The 2026-07-31 input uses w/q/auto/cs.  The remaining names are documented
# Imgix rendering operations; none identifies the underlying media object.
_KNOWN_IMGIX_TRANSFORMS = {
    "ar",
    "auto",
    "bg",
    "blur",
    "border",
    "bri",
    "ch",
    "chromasub",
    "con",
    "crop",
    "cs",
    "dpr",
    "exp",
    "faceindex",
    "facepad",
    "faces",
    "fill",
    "fill-color",
    "fit",
    "flip",
    "fm",
    "fp-debug",
    "fp-x",
    "fp-y",
    "fp-z",
    "gam",
    "h",
    "high",
    "invert",
    "lossless",
    "mark",
    "mark-align",
    "mark-alpha",
    "mark-base",
    "mark-fit",
    "mark-h",
    "mark-pad",
    "mark-rot",
    "mark-scale",
    "mark-w",
    "mask",
    "mask-bg",
    "monochrome",
    "nr",
    "orient",
    "pad",
    "palette",
    "q",
    "rect",
    "rot",
    "sat",
    "sepia",
    "shad",
    "sharp",
    "trim",
    "trim-color",
    "trim-md",
    "trim-pad",
    "trim-tol",
    "usm",
    "usmrad",
    "vib",
    "w",
}
_PLACEHOLDER_PATH_RE = re.compile(
    r"(?:^|[/_.-])(?:default|fallback|missing|no[-_]?image|placeholder|"
    r"facebook[-_]?default|social)(?:[/_.-]|$)",
    re.IGNORECASE,
)
_CONTROL_OR_SPACE_RE = re.compile(r"[\x00-\x20\x7f]")


def image_identity(raw_url: Any) -> Optional[ImageIdentity]:
    """Return a conservative host/path identity for an Architizer image.

    Known Imgix transformation parameters are removed.  Unknown query
    parameters are retained in canonical order because they may carry
    identity rather than rendering information.  The caller must separately
    retain ``raw_url`` as provenance.
    """

    if not isinstance(raw_url, str):
        return None
    raw = raw_url.strip()
    if not raw or _CONTROL_OR_SPACE_RE.search(raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or host not in _ALLOWED_IMAGE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    path = parsed.path or ""
    if not path.startswith("/") or path == "/" or "\\" in path:
        return None

    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    try:
        query_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=200,
        )
    except (ValueError, TypeError):
        return None
    identity_pairs = sorted(
        (key, value)
        for key, value in query_pairs
        if key.casefold() not in _KNOWN_IMGIX_TRANSFORMS
    )
    identity_query = urlencode(identity_pairs, doseq=True)
    normalized_url = urlunsplit(
        (scheme, netloc, path, identity_query, "")
    )
    host_path_key = f"{host}{path}"
    if identity_query:
        host_path_key += f"?{identity_query}"
    asset_key = f"{ASSET_KEY_VERSION}:{host_path_key}"
    digest = hashlib.sha256(asset_key.encode("utf-8")).hexdigest()
    placeholder = bool(_PLACEHOLDER_PATH_RE.search(path))
    return ImageIdentity(
        asset_id=f"atz_asset_{digest[:24]}",
        asset_key=asset_key,
        normalized_url=normalized_url,
        host=host,
        path=path,
        is_placeholder_candidate=placeholder,
    )


_SIZE_BUCKETS: dict[str, tuple[int, Optional[int]]] = {
    "sqft_0_1": (0, 1_000),
    "sqft_1_3": (1_000, 3_000),
    "sqft_3_5": (3_000, 5_000),
    "sqft_5_10": (5_000, 10_000),
    "sqft_10_25": (10_000, 25_000),
    "sqft_25_100": (25_000, 100_000),
    "sqft_100_300": (100_000, 300_000),
    "sqft_300_500": (300_000, 500_000),
    "sqft_500_1000": (500_000, 1_000_000),
    "sqft_1000": (1_000_000, None),
}
_SIZE_NUMBER_RE = re.compile(r"\d[\d,]*")


def _display_size_bounds(display: Optional[str]) -> Optional[tuple[int, Optional[int]]]:
    if not display:
        return None
    numbers = [
        int(token.replace(",", ""))
        for token in _SIZE_NUMBER_RE.findall(display)
    ]
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    if len(numbers) == 1 and "+" in display:
        return numbers[0], None
    return None


def parse_size_bucket(slug: Any, display: Any) -> Optional[dict[str, Any]]:
    """Parse Architizer's square-foot range without inventing exact area.

    Returned keys are ``slug``, ``display``, ``min_sqft``, ``max_sqft``,
    ``is_open_ended``, and ``status``.  A known structured slug is confirmed;
    display-only bounds are candidates; disagreement is review.
    """

    slug_text = clean_scalar(slug)
    display_text = clean_scalar(display)
    if not slug_text and not display_text:
        return None
    slug_key = (slug_text or "").casefold()
    structured = _SIZE_BUCKETS.get(slug_key)
    displayed = _display_size_bounds(display_text)

    if structured is not None:
        status = "review" if displayed is not None and displayed != structured else "confirmed"
        lower, upper = structured
        canonical_slug: Optional[str] = slug_key
    elif displayed is not None:
        status = "candidate"
        lower, upper = displayed
        canonical_slug = slug_text
    else:
        status = "review"
        lower = upper = None
        canonical_slug = slug_text

    return {
        "slug": canonical_slug,
        "display": display_text,
        "min_sqft": lower,
        "max_sqft": upper,
        "is_open_ended": upper is None and lower is not None,
        "status": status,
    }


def valid_or_candidate_year(
    year: Any,
    status: Any,
    current_year: int = 2026,
) -> tuple[Optional[int], str]:
    """Validate a year against Architizer's construction-status semantics.

    Built projects can confirm a non-future year.  Concept and
    under-construction dates are candidates up to ten years ahead.  Implausible
    values are retained by the builder as raw evidence but returned for review.
    """

    if year is None or clean_scalar(year) is None:
        return None, "missing"
    if isinstance(year, bool):
        return None, "review"
    if isinstance(year, int):
        parsed_year = year
    elif isinstance(year, float) and year.is_integer():
        parsed_year = int(year)
    else:
        text = clean_scalar(year) or ""
        if not re.fullmatch(r"\d{4}", text):
            return None, "review"
        parsed_year = int(text)

    if parsed_year < 1800 or parsed_year > current_year + 10:
        return None, "review"

    construction_status = normalize_identity_text(status).replace(" ", "-")
    if construction_status == "built":
        if parsed_year <= current_year:
            return parsed_year, "confirmed"
        return None, "review"
    if construction_status in {"concept", "under-construction"}:
        return parsed_year, "candidate"
    if parsed_year <= current_year:
        return parsed_year, "candidate"
    return None, "review"


_MOJIBAKE_MARKERS = (
    "\ufffd",
    "Ã",
    "Â",
    "â€",
    "â€™",
    "â€œ",
    "â€\x9d",
    "â€“",
    "â€”",
    "ðŸ",
    "ï»¿",
)


def text_has_mojibake(value: Any) -> bool:
    """Flag common UTF-8/Windows-1252 decoding residue conservatively."""

    if value is None:
        return False
    text = str(value)
    if any(marker in text for marker in _MOJIBAKE_MARKERS):
        return True
    return any(0x80 <= ord(ch) <= 0x9F for ch in text)


def name_similarity(left: Any, right: Any) -> float:
    """Return a deterministic 0..1 fuzzy score for duplicate review.

    Exact normalized names score 1.0.  Token-order and whole-string similarity
    are combined only for candidate ranking; this score never authorizes an
    automatic merge by itself.
    """

    a = normalize_identity_text(left)
    b = normalize_identity_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    direct = SequenceMatcher(None, a, b, autojunk=False).ratio()
    token_ordered_a = " ".join(sorted(a.split()))
    token_ordered_b = " ".join(sorted(b.split()))
    token_ordered = SequenceMatcher(
        None,
        token_ordered_a,
        token_ordered_b,
        autojunk=False,
    ).ratio()
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    union = tokens_a | tokens_b
    jaccard = len(tokens_a & tokens_b) / len(union) if union else 0.0
    return round(max(direct, token_ordered, jaccard), 6)


__all__ = [
    "SCHEMA_VERSION",
    "POLICY_VERSION",
    "TAXONOMY_VERSION",
    "ASSET_KEY_VERSION",
    "CLUSTER_VERSION",
    "CategoryMapping",
    "ImageIdentity",
    "ARCHITIZER_ARTICLE_TAGS",
    "BROAD_CATEGORIES",
    "CATEGORY_PARENT",
    "clean_scalar",
    "normalize_identity_text",
    "is_generic_project_name",
    "parse_json_list",
    "parse_json_dict",
    "mappings_for_category",
    "image_identity",
    "parse_size_bucket",
    "valid_or_candidate_year",
    "text_has_mojibake",
    "name_similarity",
]
