"""Pure reconciliation policy for Divisare metadata recrawl evidence.

The v2.1 metadata database remains immutable.  These functions choose a
resolved value (or abstain) without mutating either the parent value or the
recrawl evidence.  Callers are expected to persist the returned status and
conflict/evidence details alongside the chosen value.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from canonical.divisare_curated import (
    clean_location,
    clean_scalar,
    normalize_country,
    normalize_identity_text,
)


SCHEMA_VERSION = 5
METADATA_VERSION = "divisare-metadata-v2.2"
POLICY_VERSION = "divisare-metadata-reconciliation-v1.1"

MIN_PROJECT_YEAR = 1000
MAX_PROJECT_YEAR = 2100
MAX_IMPLICIT_AREA_AUTO_SQM = 250_000.0
MAX_EXPLICIT_AREA_AUTO_SQM = 500_000.0
MAX_AREA_HARD_SQM = 100_000_000.0
SQFT_TO_SQM = 0.09290304
AREA_SCOPE_CANDIDATE_CONFIDENCE = 0.70
AREA_UNMATCHED_UNIT_CANDIDATE_CONFIDENCE = 0.55


@dataclass(frozen=True)
class ConflictEvidence:
    field: str
    parent_value: Any
    recrawl_value: Any
    normalized_parent: Any
    normalized_recrawl: Any
    reason: str


@dataclass(frozen=True)
class ScalarResolution:
    value: Any
    source: str
    status: str
    conflict: Optional[ConflictEvidence]
    needs_review: bool


@dataclass(frozen=True)
class DescriptionResolution:
    """Description decision.

    ``value`` is populated only for recrawl text.  When ``source`` is
    ``parent``, the builder should read the immutable parent text itself.
    ``recrawl_candidate`` is evidence, not permission to export the value.
    """

    value: Optional[str]
    source: str
    status: str
    needs_review: bool


@dataclass(frozen=True)
class AreaEvidence:
    value_sqm: Optional[float]
    status: str
    unit_kind: str
    confidence: float
    needs_review: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _PreparedScalar:
    raw: Any
    value: Any
    comparison_key: Any
    invalid_reason: Optional[str] = None


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _prepare_text(value: Any, *, identity: bool) -> _PreparedScalar:
    cleaned = clean_scalar(value)
    key = normalize_identity_text(cleaned) if cleaned else None
    if cleaned and identity and not key:
        return _PreparedScalar(value, None, None, "empty_identity")
    return _PreparedScalar(value, cleaned, key if identity else cleaned)


def _strip_trailing_hyphen_delimiter(value: str) -> str:
    return re.sub(r"\s+-\s*$", "", value).strip()


def _prepare_country(value: Any) -> _PreparedScalar:
    cleaned = clean_location(value)
    if not cleaned:
        invalid = "location_delimiter_only" if not _missing(value) else None
        return _PreparedScalar(value, None, None, invalid)
    if re.match(r"^-\s+", cleaned):
        return _PreparedScalar(value, None, None, "leading_location_delimiter")
    cleaned = _strip_trailing_hyphen_delimiter(cleaned)
    if not cleaned:
        return _PreparedScalar(value, None, None, "location_delimiter_only")
    canonical = normalize_country(cleaned)
    if canonical is None:
        return _PreparedScalar(value, None, None, "invalid_country")
    return _PreparedScalar(value, canonical, normalize_identity_text(canonical))


def _prepare_city(value: Any) -> _PreparedScalar:
    cleaned = clean_location(value)
    if not cleaned:
        invalid = "location_delimiter_only" if not _missing(value) else None
        return _PreparedScalar(value, None, None, invalid)
    if re.match(r"^-\s+", cleaned):
        cleaned = re.sub(r"^-\s+", "", cleaned).strip()
    cleaned = _strip_trailing_hyphen_delimiter(cleaned)
    if not cleaned:
        return _PreparedScalar(value, None, None, "location_delimiter_only")
    key = normalize_identity_text(cleaned)
    if not key:
        return _PreparedScalar(value, None, None, "empty_identity")
    return _PreparedScalar(value, cleaned, key)


def _prepare_year(value: Any) -> _PreparedScalar:
    if _missing(value):
        return _PreparedScalar(value, None, None)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return _PreparedScalar(value, None, None, "not_an_integer_year")
    if not math.isfinite(numeric) or not numeric.is_integer():
        return _PreparedScalar(value, None, None, "not_an_integer_year")
    year = int(numeric)
    if not MIN_PROJECT_YEAR <= year <= MAX_PROJECT_YEAR:
        return _PreparedScalar(value, None, None, "year_out_of_range")
    return _PreparedScalar(value, year, year)


def _prepare_area(value: Any) -> _PreparedScalar:
    if _missing(value):
        return _PreparedScalar(value, None, None)
    if isinstance(value, bool):
        return _PreparedScalar(value, None, None, "invalid_area")
    try:
        area = float(value)
    except (TypeError, ValueError):
        return _PreparedScalar(value, None, None, "invalid_area")
    if not math.isfinite(area) or not 0 < area <= MAX_AREA_HARD_SQM:
        return _PreparedScalar(value, None, None, "area_out_of_range")
    return _PreparedScalar(value, area, area)


def _conflict(
    field_name: str,
    parent: _PreparedScalar,
    recrawl: _PreparedScalar,
    reason: str,
) -> ConflictEvidence:
    return ConflictEvidence(
        field=field_name,
        parent_value=parent.raw,
        recrawl_value=recrawl.raw,
        normalized_parent=parent.comparison_key,
        normalized_recrawl=recrawl.comparison_key,
        reason=reason,
    )


def _resolve_scalar(
    field_name: str,
    parent: _PreparedScalar,
    recrawl: _PreparedScalar,
    *,
    equivalent: Callable[[Any, Any], bool] = lambda left, right: left == right,
) -> ScalarResolution:
    invalid = parent.invalid_reason or recrawl.invalid_reason
    if invalid:
        evidence = _conflict(field_name, parent, recrawl, invalid)
        if parent.value is not None and recrawl.value is not None:
            if equivalent(parent.comparison_key, recrawl.comparison_key):
                return ScalarResolution(
                    parent.value, "parent", "confirmed_with_invalid_evidence", evidence, True
                )
            return ScalarResolution(parent.value, "parent", "conflict", evidence, True)
        if parent.value is not None:
            return ScalarResolution(
                parent.value, "parent", "parent_with_invalid_recrawl", evidence, True
            )
        if recrawl.value is not None:
            return ScalarResolution(
                recrawl.value, "recrawl", "filled_with_invalid_parent", evidence, True
            )
        return ScalarResolution(None, "none", "invalid", evidence, True)

    if parent.value is None and recrawl.value is None:
        return ScalarResolution(None, "none", "unresolved", None, False)
    if parent.value is None:
        return ScalarResolution(recrawl.value, "recrawl", "filled", None, False)
    if recrawl.value is None:
        return ScalarResolution(parent.value, "parent", "parent_only", None, False)
    if equivalent(parent.comparison_key, recrawl.comparison_key):
        return ScalarResolution(parent.value, "parent", "confirmed", None, False)
    return ScalarResolution(
        parent.value,
        "parent",
        "conflict",
        _conflict(field_name, parent, recrawl, "value_mismatch"),
        True,
    )


def resolve_name(parent_value: Any, recrawl_value: Any) -> ScalarResolution:
    return _resolve_scalar(
        "name",
        _prepare_text(parent_value, identity=True),
        _prepare_text(recrawl_value, identity=True),
    )


def resolve_country(parent_value: Any, recrawl_value: Any) -> ScalarResolution:
    return _resolve_scalar(
        "country", _prepare_country(parent_value), _prepare_country(recrawl_value)
    )


def resolve_city(parent_value: Any, recrawl_value: Any) -> ScalarResolution:
    return _resolve_scalar("city", _prepare_city(parent_value), _prepare_city(recrawl_value))


def resolve_year(parent_value: Any, recrawl_value: Any) -> ScalarResolution:
    return _resolve_scalar("year", _prepare_year(parent_value), _prepare_year(recrawl_value))


def resolve_area(parent_value: Any, recrawl_value: Any) -> ScalarResolution:
    """Resolve already validated square-metre values.

    Recrawl callers must pass ``parse_area_evidence(...).value_sqm`` rather
    than the legacy parser's ``area_sqm`` column directly.
    """

    return _resolve_scalar(
        "area_sqm",
        _prepare_area(parent_value),
        _prepare_area(recrawl_value),
        equivalent=lambda left, right: math.isclose(
            left, right, rel_tol=1e-6, abs_tol=0.01
        ),
    )


def resolve_description(
    parent_present: bool,
    recrawl_text: Any,
    fetch_status: Any,
    parse_status: Any,
    description_quality: Any,
) -> DescriptionResolution:
    """Gate recrawled prose without reviving flattened caption residue."""

    text = clean_scalar(recrawl_text)
    fetch = (clean_scalar(fetch_status) or "").casefold()
    parse = (clean_scalar(parse_status) or "").casefold()
    quality = (clean_scalar(description_quality) or "").casefold()

    if fetch == "not_found" or parse == "skipped":
        if parent_present:
            return DescriptionResolution(
                None, "parent", "parent_fallback_tombstone", True
            )
        return DescriptionResolution(None, "none", "unresolved_tombstone", True)

    valid_snapshot = fetch in {"success", "not_modified"}
    if valid_snapshot and (
        parse == "no_content"
        or quality in {"no_description_dom", "no_prose_content"}
    ):
        return DescriptionResolution(None, "none", "source_has_no_prose", False)

    if valid_snapshot and (
        parse == "partial" or quality == "dom_text_fallback_review"
    ):
        if text:
            return DescriptionResolution(
                text, "recrawl_candidate", "candidate_partial", True
            )
        return DescriptionResolution(None, "none", "unresolved_partial", True)

    if (
        valid_snapshot
        and parse == "success"
        and quality == "dom_prose_paragraphs"
        and text
    ):
        return DescriptionResolution(text, "recrawl", "accepted", False)

    if valid_snapshot and parse == "success" and quality == "dom_prose_paragraphs":
        return DescriptionResolution(None, "none", "unresolved_missing_prose", True)

    return DescriptionResolution(None, "none", "unresolved_recrawl_failure", True)


_NUMBER = r"[+-]?\d(?:[\d\s\u00a0'\u2019.,]*\d)?"
_METRIC_UNIT = (
    r"(?:m\s*(?:2|\u00b2)|\u33a1|sq\.?\s*m\.?|sq\.?\s*met(?:er|re)s?|sqm|mq|"
    r"m\.?\s*q\.?|qm|sm|msq|smq|square\s+met(?:er|re)s?)"
)
_IMPERIAL_UNIT = (
    r"(?:ft\s*(?:2|\u00b2)|sq\.?\s*ft\.?|sft|sf|pi\s*(?:2|\u00b2)|"
    r"square\s+feet)"
)
_HECTARE_UNIT = r"(?:ha|hectares?)"
_AREA_UNIT = rf"(?:{_METRIC_UNIT}|{_IMPERIAL_UNIT}|{_HECTARE_UNIT})"

_AREA_AFTER_RE = re.compile(
    rf"(?P<number>{_NUMBER})\s*(?:gross\s+)?(?P<unit>{_AREA_UNIT})(?![\w\u00b2])",
    re.IGNORECASE,
)
_AREA_BEFORE_RE = re.compile(
    rf"(?<!\w)(?P<unit>{_AREA_UNIT})\s*(?P<number>{_NUMBER})",
    re.IGNORECASE,
)
_MULTIPLICATIVE_AREA_RE = re.compile(
    rf"(?:{_NUMBER}\s*[x\u00d7*]\s*{_NUMBER}\s*{_AREA_UNIT}|"
    rf"{_AREA_UNIT}\s*{_NUMBER}\s*[x\u00d7*]\s*{_NUMBER})",
    re.IGNORECASE,
)
_MULTIPLIER_AFTER_RE = re.compile(
    rf"^\s*(?P<factor_a>{_NUMBER})\s*[x\u00d7*]\s*"
    rf"(?P<factor_b>{_NUMBER})\s*(?P<unit>{_AREA_UNIT})\s*$",
    re.IGNORECASE,
)
_MULTIPLIER_BEFORE_RE = re.compile(
    rf"^\s*(?P<unit>{_AREA_UNIT})\s*(?P<factor_a>{_NUMBER})\s*"
    rf"[x\u00d7*]\s*(?P<factor_b>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_LINEAR_OR_VOLUME_RE = re.compile(
    rf"{_NUMBER}\s*(?:mm|cm|mt|ml|lm|m\s*(?:3|\u00b3)|mc|m)(?![\w2\u00b2])",
    re.IGNORECASE,
)
_AMBIGUOUS_UNIT_RE = re.compile(
    rf"{_NUMBER}\s*sq(?!\w)", re.IGNORECASE
)
_PURE_NUMBER_RE = re.compile(rf"^\s*(?P<number>{_NUMBER})\s*$")


def _parse_number_token(token: str) -> Optional[float]:
    number = token.strip().replace("\u00a0", "").replace(" ", "")
    number = number.replace("'", "").replace("\u2019", "")
    if not number:
        return None
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        tail = number.rsplit(",", 1)[1]
        number = number.replace(",", "") if len(tail) == 3 else number.replace(",", ".")
    elif number.count(".") > 1:
        number = number.replace(".", "")
    elif "." in number:
        tail = number.rsplit(".", 1)[1]
        if len(tail) == 3:
            number = number.replace(".", "")
    try:
        value = float(number)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _area_unit_kind(unit: str) -> str:
    normalized = re.sub(r"[\s.]", "", unit.casefold())
    if normalized in {"ha", "hectare", "hectares"}:
        return "hectare"
    if (
        "ft" in normalized
        or normalized in {"sf", "sft", "pi2", "pi\u00b2", "squarefeet"}
    ):
        return "sqft"
    return "sqm"


def _quarantined(
    raw: Any,
    parsed_area_sqm: Any,
    reason: str,
    *,
    unit_kind: str = "unknown",
    candidates: Optional[list[dict[str, Any]]] = None,
    residue: Optional[str] = None,
    qualifier: Optional[str] = None,
    scope: Optional[str] = None,
    candidate_value_sqm: Optional[float] = None,
    candidate_confidence: float = 0.0,
) -> AreaEvidence:
    details: dict[str, Any] = {
        "raw": clean_scalar(raw),
        "parsed_area_sqm": parsed_area_sqm,
        "reason": reason,
    }
    if candidates is not None:
        details["candidates"] = candidates
    if residue is not None:
        details["residue"] = residue
    if qualifier is not None:
        details["qualifier"] = qualifier
    if scope is not None:
        details["scope"] = scope
    if candidate_value_sqm is not None:
        details["candidate_value_sqm"] = candidate_value_sqm
        details["candidate_confidence"] = candidate_confidence
    return AreaEvidence(
        candidate_value_sqm,
        "quarantined",
        unit_kind,
        candidate_confidence,
        True,
        details,
    )


def _valid_area(value: float) -> bool:
    return math.isfinite(value) and 0 < value <= MAX_AREA_HARD_SQM


def _accepted_area(
    *,
    raw: str,
    parsed_area_sqm: Any,
    value: float,
    status: str,
    unit_kind: str,
    confidence: float,
    candidates: list[dict[str, Any]],
    residue: Optional[str] = None,
    qualifier: Optional[str] = None,
) -> AreaEvidence:
    parsed = (
        _parse_number_token(str(parsed_area_sqm))
        if not _missing(parsed_area_sqm)
        else None
    )
    parsed_matches = (
        parsed is not None
        and _valid_area(parsed)
        and math.isclose(value, parsed, rel_tol=1e-6, abs_tol=0.01)
    )
    if parsed is not None and not parsed_matches and status == "accepted":
        status = "accepted_reparsed"

    needs_review = False
    reason = None
    if unit_kind == "hectare":
        status = "converted_hectare_review"
        confidence = min(confidence, 0.90)
        needs_review = True
        reason = "hectare_semantic_review"
    else:
        auto_limit = (
            MAX_IMPLICIT_AREA_AUTO_SQM
            if unit_kind == "implicit_sqm"
            else MAX_EXPLICIT_AREA_AUTO_SQM
        )
        if value > auto_limit:
            status = "qa_outlier_review"
            needs_review = True
            reason = "area_above_auto_threshold"

    details = {
        "raw": raw,
        "parsed_area_sqm": parsed_area_sqm,
        "parsed_matches": parsed_matches,
        "reason": reason,
        "candidates": candidates,
    }
    if residue:
        details["residue"] = residue
    if qualifier:
        details["qualifier"] = qualifier
    return AreaEvidence(
        round(value, 6),
        status,
        unit_kind,
        confidence,
        needs_review,
        details,
    )


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: (item["start"], item["end"])):
        if any(
            candidate["start"] == existing["start"]
            and candidate["end"] == existing["end"]
            for existing in unique
        ):
            continue
        unique.append(candidate)
    return unique


_RISKY_RESIDUE_RE = re.compile(
    r"\b(?:existing|addition|additional|added|extension|plus|range|from|to|"
    r"between|through|versus|vs)\b",
    re.IGNORECASE,
)
_SAFE_RESIDUE_LABEL_RE = re.compile(
    r"\b(?:area|built|surface|gross|gfa|house|approx|approximately|app|circa)\b",
    re.IGNORECASE,
)
_NON_AREA_HOUSING_UNITS_RE = re.compile(
    rf"\(\s*{_NUMBER}\s+housing\s+units?\s*\)", re.IGNORECASE
)
_NON_AREA_VOLUME_DIMENSIONS_RE = re.compile(
    rf",?\s*{_NUMBER}\s*m\s*(?:3|\u00b3)\s*"
    rf"\(\s*{_NUMBER}\s*[x\u00d7*]\s*{_NUMBER}\s*[x\u00d7*]\s*"
    rf"{_NUMBER}\s*m\.?\s*\)",
    re.IGNORECASE,
)
_UNSIGNED_NUMBER = r"\d(?:[\d\s\u00a0'\u2019.,]*\d)?"
_UNLABELED_ADDITIVE_RE = re.compile(
    rf"(?<!\w){_UNSIGNED_NUMBER}\s*\+\s*{_UNSIGNED_NUMBER}"
)
_UNLABELED_RANGE_RE = re.compile(
    rf"(?<!\w){_UNSIGNED_NUMBER}\s*(?:-|\u2013|\u2014|/|\bto\b)\s*"
    rf"{_UNSIGNED_NUMBER}",
    re.IGNORECASE,
)
_REVIEW_SCOPE_PATTERNS = (
    (re.compile(r"\bmain\s+building\b", re.IGNORECASE), "main_building"),
    (re.compile(r"\bouvert\b", re.IGNORECASE), "open_area"),
    (re.compile(r"\bsdo\b", re.IGNORECASE), "sdo"),
    (re.compile(r"\buseful\s+area\b", re.IGNORECASE), "useful_area"),
    (re.compile(r"\baboveground\b", re.IGNORECASE), "aboveground"),
    (re.compile(r"\bslp\b", re.IGNORECASE), "slp"),
    (re.compile(r"\bnew\s+construction\b", re.IGNORECASE), "new_construction"),
    (re.compile(r"\broof\s+area\b", re.IGNORECASE), "roof_area"),
    (re.compile(r"\bfootprint\b", re.IGNORECASE), "footprint"),
)


def _candidate_residue(
    raw: str,
    candidates: list[dict[str, Any]],
    *,
    allow_unit_separator: bool,
) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    masked = list(raw)
    for candidate in candidates:
        for index in range(candidate["start"], candidate["end"]):
            masked[index] = " "
    residue = " ".join("".join(masked).split())
    remainder = residue
    safe_qualifiers: list[str] = []
    for pattern, label in (
        (_NON_AREA_HOUSING_UNITS_RE, "housing_units_annotation"),
        (_NON_AREA_VOLUME_DIMENSIONS_RE, "volume_dimensions_annotation"),
    ):
        if pattern.search(remainder):
            safe_qualifiers.append(label)
            remainder = pattern.sub(" ", remainder)

    if "+" in remainder or _RISKY_RESIDUE_RE.search(remainder):
        return (
            residue,
            "residual_additive_or_range_scope",
            remainder,
            "additive_or_range",
        )
    if re.search(r"\d", remainder):
        return residue, "residual_numeric_scope", remainder, "unmatched_numeric"
    if "/" in remainder and not allow_unit_separator:
        return (
            residue,
            "residual_additive_or_range_scope",
            remainder,
            "unmatched_slash_or_unit",
        )
    for pattern, scope in _REVIEW_SCOPE_PATTERNS:
        match = pattern.search(remainder)
        if match:
            return residue, "residual_scope_label", match.group(0), scope

    safe_labels = [match.group(0).casefold() for match in _SAFE_RESIDUE_LABEL_RE.finditer(remainder)]
    safe_qualifiers.extend(safe_labels)
    remainder = _SAFE_RESIDUE_LABEL_RE.sub(" ", remainder)
    if re.search(r"\w", remainder, re.UNICODE):
        qualifier = " ".join(re.findall(r"\w+", remainder, re.UNICODE))
        return residue, "residual_scope_label", qualifier, "unrecognized_scope"
    return residue, None, ",".join(safe_qualifiers) or None, None


def _unique_metric_candidate_value(
    candidates: list[dict[str, Any]],
) -> Optional[float]:
    values = {
        round(float(candidate["value_sqm"]), 6)
        for candidate in candidates
        if candidate["unit_kind"] != "sqft"
    }
    return next(iter(values)) if len(values) == 1 else None


def parse_area_evidence(area_raw: Any, parsed_area_sqm: Any) -> AreaEvidence:
    """Reparse a Divisare ``Built Surface`` value with explicit unit safety.

    Explicit metric values are preferred.  Imperial-only values are converted,
    verified dual-unit values use the metric side, and formulas or ambiguous
    multi-value strings are quarantined.  The legacy parsed value is retained
    only as comparison evidence; it is never the source of the result.
    """

    raw = clean_scalar(area_raw)
    if raw is None:
        if _missing(parsed_area_sqm):
            return AreaEvidence(
                None,
                "missing",
                "none",
                0.0,
                False,
                {"raw": None, "parsed_area_sqm": parsed_area_sqm},
            )
        return _quarantined(area_raw, parsed_area_sqm, "missing_raw")

    multiplier_match = _MULTIPLIER_AFTER_RE.fullmatch(raw)
    if multiplier_match is None:
        multiplier_match = _MULTIPLIER_BEFORE_RE.fullmatch(raw)
    if multiplier_match is not None:
        factor_a = _parse_number_token(multiplier_match.group("factor_a"))
        factor_b = _parse_number_token(multiplier_match.group("factor_b"))
        unit_kind = _area_unit_kind(multiplier_match.group("unit"))
        unit_multiplier = (
            SQFT_TO_SQM
            if unit_kind == "sqft"
            else 10_000.0
            if unit_kind == "hectare"
            else 1.0
        )
        if factor_a is None or factor_b is None:
            return _quarantined(
                raw,
                parsed_area_sqm,
                "invalid_multiplicative_expression",
                unit_kind="ambiguous",
            )
        value = factor_a * factor_b * unit_multiplier
        multiplier_candidates = [
            {
                "factor_a": factor_a,
                "factor_b": factor_b,
                "unit_kind": unit_kind,
                "value_sqm": value,
            }
        ]
        if not _valid_area(value):
            return _quarantined(
                raw,
                parsed_area_sqm,
                "area_out_of_range",
                unit_kind=unit_kind,
                candidates=multiplier_candidates,
            )
        return _accepted_area(
            raw=raw,
            parsed_area_sqm=parsed_area_sqm,
            value=value,
            status="computed_multiplier",
            unit_kind=unit_kind,
            confidence=0.90,
            candidates=multiplier_candidates,
        )

    if _MULTIPLICATIVE_AREA_RE.search(raw):
        return _quarantined(
            raw, parsed_area_sqm, "multiplicative_expression", unit_kind="ambiguous"
        )

    candidates: list[dict[str, Any]] = []
    for pattern in (_AREA_AFTER_RE, _AREA_BEFORE_RE):
        for match in pattern.finditer(raw):
            number = _parse_number_token(match.group("number"))
            if number is None:
                continue
            unit_kind = _area_unit_kind(match.group("unit"))
            multiplier = (
                SQFT_TO_SQM
                if unit_kind == "sqft"
                else 10_000.0
                if unit_kind == "hectare"
                else 1.0
            )
            candidates.append(
                {
                    "start": match.start(),
                    "end": match.end(),
                    "raw_value": number,
                    "unit_kind": unit_kind,
                    "value_sqm": number * multiplier,
                }
            )
    candidates = _dedupe_candidates(candidates)
    public_candidates = [
        {
            "raw_value": item["raw_value"],
            "unit_kind": item["unit_kind"],
            "value_sqm": item["value_sqm"],
        }
        for item in candidates
    ]

    additive_match = _UNLABELED_ADDITIVE_RE.search(raw)
    if additive_match:
        return _quarantined(
            raw,
            parsed_area_sqm,
            "residual_additive_or_range_scope",
            unit_kind="ambiguous",
            candidates=public_candidates,
            residue=additive_match.group(0),
            qualifier=additive_match.group(0),
            scope="unmatched_additive",
        )
    range_match = _UNLABELED_RANGE_RE.search(raw)
    if range_match:
        return _quarantined(
            raw,
            parsed_area_sqm,
            "residual_additive_or_range_scope",
            unit_kind="ambiguous",
            candidates=public_candidates,
            residue=range_match.group(0),
            qualifier=range_match.group(0),
            scope="unlabeled_range",
        )

    residue = None
    safe_qualifier = None
    if not candidates:
        pure_number = _PURE_NUMBER_RE.fullmatch(raw)
        if pure_number:
            value = _parse_number_token(pure_number.group("number"))
            if value is None or not _valid_area(value):
                return _quarantined(raw, parsed_area_sqm, "area_out_of_range")
            chosen_status = "accepted_implicit_sqm"
            unit_kind = "implicit_sqm"
            confidence = 0.75
        elif _LINEAR_OR_VOLUME_RE.search(raw):
            return _quarantined(
                raw, parsed_area_sqm, "linear_or_volume_unit", unit_kind="non_area"
            )
        elif _AMBIGUOUS_UNIT_RE.search(raw):
            return _quarantined(
                raw, parsed_area_sqm, "ambiguous_area_unit", unit_kind="ambiguous"
            )
        else:
            return _quarantined(
                raw, parsed_area_sqm, "unrecognized_or_ambiguous_value"
            )
    else:
        if any(not _valid_area(item["value_sqm"]) for item in candidates):
            return _quarantined(
                raw,
                parsed_area_sqm,
                "area_out_of_range",
                candidates=public_candidates,
            )

        metric = [item for item in candidates if item["unit_kind"] != "sqft"]
        imperial = [item for item in candidates if item["unit_kind"] == "sqft"]
        metric_values = {round(item["value_sqm"], 6) for item in metric}
        imperial_values = {round(item["value_sqm"], 6) for item in imperial}
        if len(metric_values) > 1 or len(imperial_values) > 1:
            return _quarantined(
                raw,
                parsed_area_sqm,
                "multiple_area_values",
                unit_kind="ambiguous",
                candidates=public_candidates,
            )
        verified_dual = False
        if metric and imperial:
            metric_value = metric[0]["value_sqm"]
            imperial_value = imperial[0]["value_sqm"]
            if not math.isclose(metric_value, imperial_value, rel_tol=0.03, abs_tol=1.0):
                return _quarantined(
                    raw,
                    parsed_area_sqm,
                    "dual_unit_mismatch",
                    unit_kind="dual",
                    candidates=public_candidates,
                )
            verified_dual = True

        residue, residue_reason, qualifier, scope = _candidate_residue(
            raw, candidates, allow_unit_separator=verified_dual
        )
        if residue_reason:
            candidate_value = None
            candidate_confidence = 0.0
            if residue_reason == "residual_scope_label":
                candidate_value = _unique_metric_candidate_value(candidates)
                candidate_confidence = (
                    AREA_SCOPE_CANDIDATE_CONFIDENCE
                    if candidate_value is not None
                    else 0.0
                )
            elif scope == "unmatched_slash_or_unit":
                candidate_value = _unique_metric_candidate_value(candidates)
                candidate_confidence = (
                    AREA_UNMATCHED_UNIT_CANDIDATE_CONFIDENCE
                    if candidate_value is not None
                    else 0.0
                )
            return _quarantined(
                raw,
                parsed_area_sqm,
                residue_reason,
                unit_kind="candidate_scope",
                candidates=public_candidates,
                residue=residue,
                qualifier=qualifier,
                scope=scope,
                candidate_value_sqm=candidate_value,
                candidate_confidence=candidate_confidence,
            )
        safe_qualifier = qualifier

        if metric and imperial:
            metric_value = metric[0]["value_sqm"]
            value = metric_value
            chosen_status = "accepted_dual_verified"
            unit_kind = "sqm_dual"
            confidence = 0.99
        elif metric:
            value = metric[0]["value_sqm"]
            source_kind = metric[0]["unit_kind"]
            chosen_status = "accepted_converted" if source_kind == "hectare" else "accepted"
            unit_kind = source_kind
            confidence = 0.99 if source_kind == "sqm" else 0.97
        else:
            value = imperial[0]["value_sqm"]
            chosen_status = "accepted_converted"
            unit_kind = "sqft"
            confidence = 0.97

    return _accepted_area(
        raw=raw,
        parsed_area_sqm=parsed_area_sqm,
        value=value,
        status=chosen_status,
        unit_kind=unit_kind,
        confidence=confidence,
        candidates=public_candidates,
        residue=residue,
        qualifier=safe_qualifier,
    )


__all__ = [
    "AREA_SCOPE_CANDIDATE_CONFIDENCE",
    "AREA_UNMATCHED_UNIT_CANDIDATE_CONFIDENCE",
    "AreaEvidence",
    "ConflictEvidence",
    "DescriptionResolution",
    "MAX_AREA_HARD_SQM",
    "MAX_EXPLICIT_AREA_AUTO_SQM",
    "MAX_IMPLICIT_AREA_AUTO_SQM",
    "MAX_PROJECT_YEAR",
    "METADATA_VERSION",
    "MIN_PROJECT_YEAR",
    "POLICY_VERSION",
    "SCHEMA_VERSION",
    "SQFT_TO_SQM",
    "ScalarResolution",
    "parse_area_evidence",
    "resolve_area",
    "resolve_city",
    "resolve_country",
    "resolve_description",
    "resolve_name",
    "resolve_year",
]
