"""Conservative metadata policies for the Divisare v2 materialization.

This module is intentionally independent from the v1 implementation.  It
contains pure functions only, so a migration can recompute derived state
without mutating or importing v1 policy globals.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


V2_METADATA_VERSION = "divisare-metadata-v2.1"
V2_SCHEMA_VERSION = 4
V2_ARTICLE_KIND_POLICY_VERSION = "divisare-article-kind-v2.1"
V2_EVIDENCE_POLICY_VERSION = "divisare-evidence-v2.1"
V2_FACET_POLICY_VERSION = "divisare-facet-resolver-v2.1"
V2_PRIMARY_VALUE_POLICY_VERSION = "divisare-primary-value-v2.1"

# Short aliases are useful in build metadata while the V2-prefixed names make
# accidental use of v1 constants less likely.
METADATA_VERSION = V2_METADATA_VERSION
SCHEMA_VERSION = V2_SCHEMA_VERSION
ARTICLE_KIND_POLICY_VERSION = V2_ARTICLE_KIND_POLICY_VERSION
EVIDENCE_POLICY_VERSION = V2_EVIDENCE_POLICY_VERSION
FACET_POLICY_VERSION = V2_FACET_POLICY_VERSION
PRIMARY_VALUE_POLICY_VERSION = V2_PRIMARY_VALUE_POLICY_VERSION

DIRECT_CONFIRM_THRESHOLD = 0.85
SUPPORTING_CONFIRM_THRESHOLD = 0.75
SUPPORTING_MIN_INDEPENDENT_GROUPS = 2
SUPPORTING_MIN_ARTICLES = 2
ARTICLE_KIND_AMBIGUITY_MARGIN = 0.05

ARTICLE_KINDS = (
    "project",
    "drawing_feature",
    "photo_feature",
    "model_feature",
    "concept_editorial",
    "mixed_feature",
)
ARTICLE_KIND_STATUSES = ("confirmed", "candidate", "ambiguous", "unresolved")

EVIDENCE_FAMILY_STRUCTURED = "divisare.structured"
EVIDENCE_FAMILY_TAXONOMY = "divisare.taxonomy"
EVIDENCE_FAMILY_CONTENT_HINT = "divisare.content_hint"
EVIDENCE_FAMILY_TITLE_LEXICAL = "divisare.title_lexical"
EVIDENCE_FAMILY_HTML_EXPLICIT = "divisare.html_explicit"
EVIDENCE_FAMILY_TEXT = "divisare.text"
EVIDENCE_FAMILY_IMAGE_MODEL = "divisare.image_model"
EVIDENCE_FAMILY_IMAGE_FILENAME = "divisare.image_filename"
EVIDENCE_FAMILY_EXACT_IMAGE = "divisare.exact_image"
EVIDENCE_FAMILY_PHASH = "divisare.phash"
EVIDENCE_FAMILY_MANUAL = "manual"
EVIDENCE_FAMILY_UNKNOWN = "unknown"

_ARTICLE_KIND_PRIORITY = {
    "drawing_feature": 0,
    "model_feature": 1,
    "photo_feature": 2,
    "concept_editorial": 3,
    "mixed_feature": 4,
    "project": 5,
}

_KNOWN_ALBUMS = {
    "plans-details",
    "ideas",
    "topics",
    "types",
    "houses",
    "elements",
    "materiality",
    "cities",
    "private-interiors",
    "public-interiors",
}

_TOPIC_KIND_BY_TAG = {
    "architectural-drawings": "drawing_feature",
    "italian-drawings": "drawing_feature",
    "architects-notebooks": "drawing_feature",
    "architectural-models": "model_feature",
    "reportages": "photo_feature",
    "portraits": "photo_feature",
    "by-night": "photo_feature",
}

_HINT_KIND = {
    "plan": "drawing_feature",
    "construction detail": "drawing_feature",
    "section": "drawing_feature",
    "drawing": "drawing_feature",
    "notebook sketch": "drawing_feature",
    "model": "model_feature",
    "reportage": "photo_feature",
    "portrait": "photo_feature",
    "night photography": "photo_feature",
}

_LEXICAL_PATTERNS = {
    "drawing_feature": (
        re.compile(r"\bplans?\b"),
        re.compile(r"\bsections?\b"),
        re.compile(r"\bdrawings?\b"),
        re.compile(r"\b(?:architectural|working)\s+drawings?\b"),
        re.compile(r"\bconstruction\s+details?\b"),
        re.compile(r"\bfloor\s+plans?\b"),
        re.compile(r"\bplans?\s+and\s+sections?\b"),
        re.compile(r"\bsections?\s+and\s+plans?\b"),
        re.compile(r"\bsketchbooks?\b"),
    ),
    "model_feature": (
        re.compile(r"\bmodels?\b"),
        re.compile(r"\barchitectural\s+models?\b"),
        re.compile(r"\bscale\s+models?\b"),
        re.compile(r"\bmodel\s+stud(?:y|ies)\b"),
    ),
    "photo_feature": (
        re.compile(r"\bphotograph(?:s|y|ic)?\b"),
        re.compile(r"\bportraits?\b"),
        re.compile(r"\bby\s+night\b"),
        re.compile(r"\bphoto(?:graphic)?\s+essays?\b"),
        re.compile(r"\bphoto\s+series\b"),
        re.compile(r"\bphotographs?\s+by\b"),
        re.compile(r"\breportages?\b"),
    ),
    "concept_editorial": (
        re.compile(r"\bconcept(?:ual)?\b"),
        re.compile(r"\bproposals?\b"),
        re.compile(r"\bvisions?\b"),
        re.compile(r"\bmanifestos?\b"),
        re.compile(r"\bunbuilt\s+(?:project|proposal|architecture)\b"),
        re.compile(r"\bcompetition\s+(?:entry|proposal)\b"),
        re.compile(r"\bconceptual\s+(?:project|study|proposal)\b"),
        re.compile(r"\bideas?\s+for\b"),
        re.compile(r"\barchitectural\s+manifesto\b"),
    ),
}


@dataclass(frozen=True)
class ArticleKindEvidence:
    """One atomic assertion about the editorial kind of an article."""

    kind: str
    confidence: float
    evidence_family: str
    source_ref: str
    is_strong: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ARTICLE_KINDS:
            raise ValueError("unsupported article kind: %s" % self.kind)
        _validate_confidence(self.confidence, "confidence")
        if not self.evidence_family:
            raise ValueError("evidence_family must not be empty")
        if not self.source_ref:
            raise ValueError("source_ref must not be empty")

    @property
    def article_kind(self) -> str:
        return self.kind


@dataclass(frozen=True)
class Resolution:
    """Resolved article kind plus the evidence summary used to derive it."""

    kind: Optional[str]
    status: str
    confidence: float
    evidence_count: int
    evidence_families: Tuple[str, ...]
    reasons: Tuple[str, ...]
    ranked_kinds: Tuple[Tuple[str, float], ...]

    def __post_init__(self) -> None:
        if self.kind is not None and self.kind not in ARTICLE_KINDS:
            raise ValueError("unsupported resolved article kind: %s" % self.kind)
        if self.status not in ARTICLE_KIND_STATUSES:
            raise ValueError("unsupported resolution status: %s" % self.status)
        _validate_confidence(self.confidence, "confidence")

    @property
    def article_kind(self) -> Optional[str]:
        return self.kind

    @property
    def resolved_kind(self) -> Optional[str]:
        return self.kind


def _validate_confidence(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError("%s must be a real number" % label)
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValueError("%s must be between 0 and 1" % label)
    return number


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _normalize_token(value: Any) -> str:
    return _normalize_text(value).replace(" ", "-")


def _as_values(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return list(value.values())
    try:
        return list(value)
    except TypeError:
        return [value]


def _iter_album_tags(album_tags: Any) -> List[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()
    if album_tags is None:
        return []

    if isinstance(album_tags, Mapping):
        items = album_tags.items()
    else:
        items = []
        for item in _as_values(album_tags):
            if isinstance(item, Mapping):
                album = item.get("album_slug") or item.get("album")
                tag = item.get("tag_slug") or item.get("tag") or "*"
                items.append((album, tag))
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                items.append((item[0], item[1]))
            else:
                token = _normalize_token(item)
                if token in _KNOWN_ALBUMS:
                    items.append((token, "*"))
                elif (
                    token.startswith("plans-of-")
                    or token.startswith("construction-details-of-")
                    or token == "sections"
                ):
                    items.append(("plans-details", token))
                elif token in _TOPIC_KIND_BY_TAG:
                    items.append(("topics", token))

    for album, tags in items:
        album_token = _normalize_token(album)
        if not album_token:
            continue
        for tag in _as_values(tags):
            tag_token = _normalize_token(tag) or "*"
            pairs.add((album_token, tag_token))
    return sorted(pairs)


def _iter_content_hints(content_hints: Any) -> List[str]:
    hints: Set[str] = set()
    for item in _as_values(content_hints):
        if isinstance(item, Mapping):
            value = (
                item.get("value")
                or item.get("content_hint")
                or item.get("value_normalized")
            )
        elif isinstance(item, (tuple, list)) and item:
            value = item[-1]
        else:
            value = item
        normalized = _normalize_text(value)
        if normalized:
            hints.add(normalized)
    return sorted(hints)


def infer_article_kind_evidence(
    name: Any,
    slug: Any,
    album_tags: Any,
    content_hints: Any,
) -> List[ArticleKindEvidence]:
    """Infer conservative article-kind evidence.

    Album tags and content hints are candidate evidence.  Lexical evidence is
    marked strong, but the resolver still requires a matching taxonomy or
    content-hint assertion before it confirms a kind.
    """

    evidence: Dict[Tuple[str, str, str], ArticleKindEvidence] = {}

    def add(item: ArticleKindEvidence) -> None:
        key = (item.kind, item.evidence_family, item.source_ref)
        current = evidence.get(key)
        if current is None or item.confidence > current.confidence:
            evidence[key] = item

    for album, tag in _iter_album_tags(album_tags):
        family = "%s.%s" % (EVIDENCE_FAMILY_TAXONOMY, album)
        source_ref = "tag:%s:%s" % (album, tag)
        if album == "plans-details":
            add(
                ArticleKindEvidence(
                    "drawing_feature",
                    0.82,
                    family,
                    source_ref,
                    reason="Plans/details album membership is a gallery-level prior.",
                )
            )
        elif album == "ideas":
            add(
                ArticleKindEvidence(
                    "concept_editorial",
                    0.80,
                    family,
                    source_ref,
                    reason="Ideas album membership is an editorial prior.",
                )
            )
        elif album == "topics" and tag in _TOPIC_KIND_BY_TAG:
            add(
                ArticleKindEvidence(
                    _TOPIC_KIND_BY_TAG[tag],
                    0.76,
                    family,
                    source_ref,
                    reason="Media topic membership is a gallery-level prior.",
                )
            )

    for hint in _iter_content_hints(content_hints):
        kind = _HINT_KIND.get(hint)
        if kind is None:
            continue
        add(
            ArticleKindEvidence(
                kind,
                0.78,
                EVIDENCE_FAMILY_CONTENT_HINT,
                "content_hint:%s" % hint.replace(" ", "-"),
                reason="Content hints remain candidate evidence.",
            )
        )

    lexical_text = " ".join(
        part for part in (_normalize_text(name), _normalize_text(slug)) if part
    )
    for kind, patterns in _LEXICAL_PATTERNS.items():
        if any(pattern.search(lexical_text) for pattern in patterns):
            add(
                ArticleKindEvidence(
                    kind,
                    0.94,
                    EVIDENCE_FAMILY_TITLE_LEXICAL,
                    "title_or_slug",
                    is_strong=True,
                    reason="A conservative title or slug phrase matched.",
                )
            )

    return sorted(
        evidence.values(),
        key=lambda item: (
            _ARTICLE_KIND_PRIORITY[item.kind],
            item.evidence_family,
            item.source_ref,
        ),
    )


def resolve_article_kind(evidence: Iterable[ArticleKindEvidence]) -> Resolution:
    """Resolve evidence without confirming metadata-only signals."""

    items = list(evidence or ())
    if not items:
        return Resolution(None, "unresolved", 0.0, 0, (), (), ())
    if not all(isinstance(item, ArticleKindEvidence) for item in items):
        raise TypeError("evidence must contain ArticleKindEvidence instances")

    by_kind: Dict[str, List[ArticleKindEvidence]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)

    scores: Dict[str, float] = {}
    confirmed: List[str] = []
    for kind, kind_items in by_kind.items():
        candidate_items = [
            item
            for item in kind_items
            if item.evidence_family.startswith(EVIDENCE_FAMILY_TAXONOMY + ".")
            or item.evidence_family == EVIDENCE_FAMILY_CONTENT_HINT
        ]
        strong_items = [
            item
            for item in kind_items
            if item.is_strong
            and item.evidence_family == EVIDENCE_FAMILY_TITLE_LEXICAL
        ]
        authoritative_items = [
            item
            for item in kind_items
            if item.is_strong
            and item.evidence_family
            in {EVIDENCE_FAMILY_HTML_EXPLICIT, EVIDENCE_FAMILY_MANUAL}
        ]
        scores[kind] = max(item.confidence for item in kind_items)
        if candidate_items and strong_items:
            tag_confidence = max(item.confidence for item in candidate_items)
            lexical_confidence = max(item.confidence for item in strong_items)
            scores[kind] = min(
                0.99, ((tag_confidence + lexical_confidence) / 2.0) + 0.05
            )
        if authoritative_items:
            scores[kind] = max(
                scores[kind],
                max(item.confidence for item in authoritative_items),
            )
            confirmed.append(kind)

    ranked = tuple(
        sorted(
            scores.items(),
            key=lambda item: (
                -item[1],
                _ARTICLE_KIND_PRIORITY[item[0]],
                item[0],
            ),
        )
    )
    families = tuple(sorted({item.evidence_family for item in items}))
    reasons = tuple(sorted({item.reason for item in items if item.reason}))

    if len(confirmed) == 1:
        kind = confirmed[0]
        return Resolution(
            kind,
            "confirmed",
            scores[kind],
            len(items),
            families,
            reasons,
            ranked,
        )
    if len(confirmed) > 1:
        return Resolution(
            None,
            "ambiguous",
            max(scores[kind] for kind in confirmed),
            len(items),
            families,
            reasons,
            ranked,
        )

    top_kind, top_confidence = ranked[0]
    if (
        len(ranked) > 1
        and top_confidence - ranked[1][1] < ARTICLE_KIND_AMBIGUITY_MARGIN
    ):
        return Resolution(
            None,
            "ambiguous",
            top_confidence,
            len(items),
            families,
            reasons,
            ranked,
        )
    return Resolution(
        top_kind,
        "candidate",
        top_confidence,
        len(items),
        families,
        reasons,
        ranked,
    )


def _details_dict(details: Any) -> Dict[str, Any]:
    if details is None:
        return {}
    if isinstance(details, Mapping):
        return dict(details)
    if isinstance(details, str):
        try:
            parsed = json.loads(details)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def evidence_family_for_claim(evidence_kind: Any, details: Any = None) -> str:
    """Map a claim source to a stable evidence family."""

    detail = _details_dict(details)
    explicit = detail.get("evidence_family")
    if explicit:
        return str(explicit).strip()

    kind = _normalize_token(evidence_kind)
    album = _normalize_token(
        detail.get("album_slug")
        or detail.get("album")
        or detail.get("source_album")
    )

    if kind in {"content-hint", "divisare-content-hint"}:
        return EVIDENCE_FAMILY_CONTENT_HINT
    if kind in {
        "source-tag",
        "tag",
        "tag-crosswalk",
        "taxonomy",
    } or "tag" in kind:
        if album:
            return "%s.%s" % (EVIDENCE_FAMILY_TAXONOMY, album)
        return EVIDENCE_FAMILY_TAXONOMY
    if kind in {"structured", "structured-field", "source-field", "field"}:
        return EVIDENCE_FAMILY_STRUCTURED
    if kind in {
        "text",
        "text-extraction",
        "description",
        "recrawl-text",
        "title-lexical",
    }:
        return EVIDENCE_FAMILY_TEXT
    if kind in {"image-model", "image-classification", "vision-model"}:
        return EVIDENCE_FAMILY_IMAGE_MODEL
    if kind in {"filename", "filename-hint", "image-filename"}:
        return EVIDENCE_FAMILY_IMAGE_FILENAME
    if kind in {"sha256", "exact-image", "exact-hash", "asset-key"}:
        return EVIDENCE_FAMILY_EXACT_IMAGE
    if kind in {"phash", "perceptual-hash", "near-image"}:
        return EVIDENCE_FAMILY_PHASH
    if kind in {"manual", "manual-review", "human"}:
        return EVIDENCE_FAMILY_MANUAL
    if kind.startswith("external-"):
        return "external.%s" % kind[len("external-") :]
    source_system = _normalize_token(detail.get("source_system"))
    if source_system and source_system != "divisare":
        return "external.%s" % source_system
    return EVIDENCE_FAMILY_UNKNOWN


def independence_key_for_claim(article_id: Any, evidence_kind: Any) -> str:
    """Return the conservative correlation group for one article claim."""

    article_key = str(article_id).strip()
    if not article_key:
        raise ValueError("article_id must not be empty")
    kind = _normalize_token(evidence_kind)

    if (
        kind.startswith("divisare-taxonomy")
        or kind in {
            "source-tag",
            "tag",
            "tag-crosswalk",
            "taxonomy",
            "content-hint",
            "divisare-content-hint",
        }
        or "tag" in kind
    ):
        channel = "taxonomy"
    elif "structured" in kind or kind in {"source-field", "field"}:
        channel = "structured"
    elif (
        "text" in kind
        or "title-lexical" in kind
        or kind == "description"
    ):
        channel = "text"
    elif (
        "image" in kind
        or "phash" in kind
        or "perceptual-hash" in kind
        or kind in {"sha256", "asset-key"}
    ):
        channel = "image"
    elif "manual" in kind or kind == "human":
        return "manual:article:%s" % article_key
    elif kind.startswith("external-"):
        return "%s:article:%s" % (kind, article_key)
    else:
        channel = "unknown"
    return "divisare:article:%s:%s" % (article_key, channel)


def _supporting_group_count(groups: Any) -> int:
    if groups is None:
        return 0
    if isinstance(groups, bool):
        raise TypeError("supporting evidence group count must not be boolean")
    if isinstance(groups, int):
        if groups < 0:
            raise ValueError("supporting evidence group count must be non-negative")
        return groups

    if isinstance(groups, Mapping):
        if any(key in groups for key in ("independence_key", "group", "key")):
            key = (
                groups.get("independence_key")
                or groups.get("group")
                or groups.get("key")
            )
            return 1 if key is not None and str(key).strip() else 0
        return len({str(key) for key in groups})

    unique: Set[str] = set()
    for group in _as_values(groups):
        if isinstance(group, Mapping):
            key = (
                group.get("independence_key")
                or group.get("group")
                or group.get("key")
            )
        elif isinstance(group, (tuple, list)) and group:
            key = group[0]
        else:
            key = getattr(group, "independence_key", group)
        if key is not None and str(key).strip():
            unique.add(str(key).strip())
    return len(unique)


def facet_status_v2(
    direct_confidences: Any,
    supporting_evidence_groups: Any,
    confidence: float,
    supporting_article_count: Optional[int] = None,
) -> str:
    """Resolve one facet value under the v2 independence policy."""

    aggregate_confidence = _validate_confidence(confidence, "confidence")
    if direct_confidences is None:
        direct_values: List[Any] = []
    elif isinstance(direct_confidences, Real) and not isinstance(
        direct_confidences, bool
    ):
        direct_values = [direct_confidences]
    elif isinstance(direct_confidences, Mapping):
        direct_values = list(direct_confidences.values())
    else:
        direct_values = list(direct_confidences)

    validated_direct = [
        _validate_confidence(value, "direct confidence")
        for value in direct_values
        if value is not None
    ]
    if validated_direct:
        return (
            "confirmed"
            if max(validated_direct) >= DIRECT_CONFIRM_THRESHOLD
            else "candidate"
        )

    independent_groups = _supporting_group_count(supporting_evidence_groups)
    if supporting_article_count is not None:
        if (
            isinstance(supporting_article_count, bool)
            or not isinstance(supporting_article_count, int)
            or supporting_article_count < 0
        ):
            raise ValueError(
                "supporting_article_count must be a non-negative integer"
            )
    if (
        independent_groups >= SUPPORTING_MIN_INDEPENDENT_GROUPS
        and (
            supporting_article_count is None
            or supporting_article_count >= SUPPORTING_MIN_ARTICLES
        )
        and aggregate_confidence >= SUPPORTING_CONFIRM_THRESHOLD
    ):
        return "confirmed"
    return "candidate"


@dataclass(frozen=True)
class _PrimaryCandidate:
    value: str
    confidence: float
    priority: int

    @property
    def normalized_value(self) -> str:
        return _normalize_text(self.value)


def _primary_candidate(record: Any, fallback_value: Any = None) -> _PrimaryCandidate:
    if isinstance(record, Mapping):
        value = record.get("value", fallback_value)
        confidence = record.get("confidence", 0.0)
        priority = record.get("priority", 0)
    elif isinstance(record, (tuple, list)):
        if not record:
            raise ValueError("primary value tuple must not be empty")
        if fallback_value is None:
            value = record[0]
            confidence = record[1] if len(record) > 1 else 0.0
            priority = record[2] if len(record) > 2 else 0
        else:
            value = fallback_value
            confidence = record[0]
            priority = record[1] if len(record) > 1 else 0
    elif hasattr(record, "value"):
        value = getattr(record, "value")
        confidence = getattr(record, "confidence", 0.0)
        priority = getattr(record, "priority", 0)
    else:
        value = record if fallback_value is None else fallback_value
        confidence = 0.0 if fallback_value is None else record
        priority = 0

    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        raise ValueError("primary value must not be empty")
    conf = _validate_confidence(confidence, "primary value confidence")
    if isinstance(priority, bool) or not isinstance(priority, Real):
        raise TypeError("primary value priority must be numeric")
    return _PrimaryCandidate(text, conf, int(priority))


def choose_primary_value(values: Any, allow_multi: bool = True) -> Optional[str]:
    """Choose a primary value without forcing a tied multi-value result.

    Input records may be mappings with value/confidence/priority keys, objects
    with those attributes, or ``(value, confidence, priority)`` tuples.  When
    ``allow_multi`` is true, an exact top-rank tie returns ``None``.  Setting it
    false resolves that tie by normalized lexical order.
    """

    candidates: List[_PrimaryCandidate] = []
    if values is None:
        return None
    if isinstance(values, Mapping):
        if "value" in values:
            candidates.append(_primary_candidate(values))
        else:
            for value, payload in values.items():
                candidates.append(_primary_candidate(payload, fallback_value=value))
    elif (
        isinstance(values, (tuple, list))
        and len(values) in (2, 3)
        and isinstance(values[0], str)
        and isinstance(values[1], Real)
        and not isinstance(values[1], bool)
    ):
        candidates.append(_primary_candidate(values))
    else:
        for record in _as_values(values):
            candidates.append(_primary_candidate(record))
    if not candidates:
        return None

    best_by_value: Dict[str, _PrimaryCandidate] = {}
    for candidate in candidates:
        key = candidate.normalized_value
        current = best_by_value.get(key)
        rank = (candidate.confidence, candidate.priority)
        current_rank = (
            (current.confidence, current.priority) if current is not None else None
        )
        if (
            current is None
            or rank > current_rank
            or (rank == current_rank and candidate.value < current.value)
        ):
            best_by_value[key] = candidate

    ranked = sorted(
        best_by_value.values(),
        key=lambda item: (
            -item.confidence,
            -item.priority,
            item.normalized_value,
            item.value,
        ),
    )
    top = ranked[0]
    tied = [
        item
        for item in ranked
        if item.confidence == top.confidence and item.priority == top.priority
    ]
    if allow_multi and len(tied) > 1:
        return None
    return top.value


__all__ = [
    "V2_METADATA_VERSION",
    "V2_SCHEMA_VERSION",
    "V2_ARTICLE_KIND_POLICY_VERSION",
    "V2_EVIDENCE_POLICY_VERSION",
    "V2_FACET_POLICY_VERSION",
    "V2_PRIMARY_VALUE_POLICY_VERSION",
    "METADATA_VERSION",
    "SCHEMA_VERSION",
    "ARTICLE_KIND_POLICY_VERSION",
    "EVIDENCE_POLICY_VERSION",
    "FACET_POLICY_VERSION",
    "PRIMARY_VALUE_POLICY_VERSION",
    "DIRECT_CONFIRM_THRESHOLD",
    "SUPPORTING_CONFIRM_THRESHOLD",
    "SUPPORTING_MIN_INDEPENDENT_GROUPS",
    "SUPPORTING_MIN_ARTICLES",
    "ARTICLE_KIND_AMBIGUITY_MARGIN",
    "ARTICLE_KINDS",
    "ARTICLE_KIND_STATUSES",
    "EVIDENCE_FAMILY_HTML_EXPLICIT",
    "EVIDENCE_FAMILY_MANUAL",
    "EVIDENCE_FAMILY_STRUCTURED",
    "EVIDENCE_FAMILY_TAXONOMY",
    "EVIDENCE_FAMILY_CONTENT_HINT",
    "EVIDENCE_FAMILY_TITLE_LEXICAL",
    "EVIDENCE_FAMILY_TEXT",
    "EVIDENCE_FAMILY_IMAGE_MODEL",
    "EVIDENCE_FAMILY_IMAGE_FILENAME",
    "EVIDENCE_FAMILY_EXACT_IMAGE",
    "EVIDENCE_FAMILY_PHASH",
    "EVIDENCE_FAMILY_MANUAL",
    "EVIDENCE_FAMILY_UNKNOWN",
    "ArticleKindEvidence",
    "Resolution",
    "infer_article_kind_evidence",
    "resolve_article_kind",
    "evidence_family_for_claim",
    "independence_key_for_claim",
    "facet_status_v2",
    "choose_primary_value",
]
