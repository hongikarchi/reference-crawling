"""Pure policy for Architizer fixed-snapshot/recrawl reconciliation.

This module is intentionally source-specific.  It does not import the common
canonical schema or vocabulary, perform network requests, or mutate any input
database.  The companion tool materializes an *intermediate reconciliation
plan*.  A later curated-v2 materializer can consume its effective-field views
only after the source crawl has converged and the plan is publication-eligible.

The central policy is conservative but permits genuine source updates:

* only ``targets.last_good_version_id`` with a valid entity identity is read;
* an unambiguous, non-empty recrawl value may replace an older baseline value;
* missing or parser-conflicting recrawl values never erase the baseline;
* project/firm identity fields never change for an existing entity; and
* every candidate, decision, and conflict remains queryable as lineage.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping, Optional


RECONCILIATION_SCHEMA_VERSION = "architizer-reconciliation-schema-v0.2"
RECONCILIATION_POLICY_VERSION = "architizer-reconciliation-policy-v0.2"
RECONCILIATION_TOOL_VERSION = "architizer-reconciliation-tool-v0.2"
RECONCILIATION_READY_VERSION = "architizer-reconciliation-ready-v2"
FINAL_TARGET_SCHEMA_VERSION = "architizer-curated-schema-v2.0"
RULE_VERSION = "architizer-field-reconciliation-v1"
_MALFORMED_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")

ELIGIBLE_RECRAWL_STATUSES = frozenset({"confirmed", "single_source"})
EMPTY_VALUES = (None, "", [], {})
ARCHITIZER_ENTITY_COLLECTIONS = {
    "project": "projects",
    "firm": "firms",
}


@dataclass(frozen=True)
class FieldSpec:
    """Mapping of one effective field across the three immutable inputs."""

    entity_type: str
    name: str
    raw_column: Optional[str]
    curated_column: Optional[str]
    sidecar_field: Optional[str]
    value_kind: str = "text"
    identity: bool = False


@dataclass(frozen=True)
class Candidate:
    """One normalized field candidate and its exact input locator."""

    source_role: str
    value: Any
    status: str
    quality: str
    locator: Mapping[str, Any]


@dataclass(frozen=True)
class FieldDecision:
    """Deterministic effective value plus an explicit conflict disposition."""

    value: Any
    decision_kind: str
    status: str
    selected_role: Optional[str]
    conflict_kind: Optional[str]
    rule_id: str


PROJECT_FIELD_SPECS = (
    FieldSpec("project", "project_id", "id", "source_project_id", "project_id", "int", True),
    FieldSpec("project", "global_id", "global_id", "global_id", "global_id", identity=True),
    FieldSpec("project", "slug", "slug", "slug", "slug", identity=True),
    FieldSpec("project", "name", "name", "name", "name"),
    FieldSpec("project", "firm_slug", "firm_slug", "source_firm_slug", "firm_slug"),
    FieldSpec("project", "firm_name", "firm_name", "source_firm_name", "firm_name"),
    FieldSpec("project", "description", "description", "description", "description"),
    FieldSpec("project", "description_short", "description_short", "description_short", "description_short"),
    FieldSpec("project", "completion_year", "completion_year", "completion_year_raw", "completion_year", "int"),
    FieldSpec("project", "building_size_slug", "building_size_slug", "building_size_slug", "size_bucket"),
    FieldSpec("project", "building_size_display", "building_size_display", "building_size_display", None),
    FieldSpec("project", "construction_status", "constr_status", "constr_status_raw", "construction_status"),
    FieldSpec("project", "budget", "budget", "budget_raw", None, "real"),
    FieldSpec("project", "location_full", "location_full", "location_full", "location"),
    FieldSpec("project", "location_country", "location_country", "location_country_raw", None),
    FieldSpec("project", "location_city", "location_city", "location_city_raw", None),
    FieldSpec("project", "categories", "categories", "categories_raw_json", "categories", "json_list"),
    FieldSpec("project", "cover_image_url", "cover_image_url", None, "cover_image_url"),
    FieldSpec(
        "project",
        "gallery_image_urls",
        "gallery_image_urls",
        "gallery_image_urls_raw_json",
        "gallery_image_urls",
        "json_list",
    ),
    FieldSpec(
        "project",
        "image_global_ids",
        "image_global_ids",
        "image_global_ids_raw_json",
        "image_global_ids",
        "json_list",
    ),
    FieldSpec("project", "published_time", "published_time", "published_time", "published_time"),
    FieldSpec("project", "modified_time", "modified_time", "modified_time", "modified_time"),
    FieldSpec("project", "fetched_at", "fetched_at", "fetched_at", None),
)

FIRM_FIELD_SPECS = (
    FieldSpec("firm", "slug", "slug", "source_firm_slug", "slug", identity=True),
    FieldSpec("firm", "name", "name", "source_name", "name"),
    FieldSpec("firm", "office_locations", "office_locations", "office_locations_raw_json", "office_locations", "json_list"),
    FieldSpec("firm", "description", "description", "description", "description"),
    FieldSpec("firm", "awards_summary", "awards_summary", "awards_summary", None),
    FieldSpec("firm", "project_count_seen", "project_count_seen", "project_count_seen", None, "int"),
    FieldSpec("firm", "project_urls", None, None, "project_urls", "json_list"),
    FieldSpec("firm", "social_links", "social_links", "social_links_raw_json", "social_links", "json_object"),
    FieldSpec("firm", "fetched_at", "fetched_at", "fetched_at", None),
)

FIELD_SPECS = {
    "project": PROJECT_FIELD_SPECS,
    "firm": FIRM_FIELD_SPECS,
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = "\x1f".join("" if item is None else str(item) for item in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}{digest}"


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value))
    text = text.replace("\u00a0", " ").replace("\ufeff", "")
    return " ".join(text.split()) or None


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    text = clean_text(value)
    if text is None:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [text] if isinstance(default, list) else default


def normalize_value(value: Any, value_kind: str) -> Any:
    """Normalize only representation; never infer a missing source claim."""

    if value_kind == "json_list":
        parsed = _json_value(value, [])
        if not isinstance(parsed, list):
            parsed = [parsed]
        result = []
        seen: set[str] = set()
        for item in parsed:
            normalized = clean_text(item) if not isinstance(item, (dict, list)) else item
            if normalized in EMPTY_VALUES:
                continue
            key = canonical_json(normalized)
            if key not in seen:
                seen.add(key)
                result.append(normalized)
        return result
    if value_kind == "json_object":
        parsed = _json_value(value, {})
        return parsed if isinstance(parsed, dict) else {}
    if value_kind == "int":
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return int(parsed) if parsed.is_integer() else None
    if value_kind == "real":
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return clean_text(value)


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return clean_text(left).casefold() == clean_text(right).casefold()
    return canonical_json(left) == canonical_json(right)


def reconcile_field(
    *,
    baseline: Optional[Candidate],
    recrawl: Optional[Candidate],
    identity_field: bool,
    entity_is_new: bool,
) -> FieldDecision:
    """Choose an effective field without erasing evidence or sparse values."""

    baseline_present = baseline is not None and not is_empty(baseline.value)
    recrawl_eligible = (
        recrawl is not None
        and recrawl.status in ELIGIBLE_RECRAWL_STATUSES
        and not is_empty(recrawl.value)
    )
    recrawl_conflict = recrawl is not None and recrawl.status == "conflict"

    if baseline_present:
        if not recrawl_eligible:
            return FieldDecision(
                baseline.value,
                "baseline_retained",
                "conflict_preserved" if recrawl_conflict else "baseline_only",
                baseline.source_role,
                "parser_conflict" if recrawl_conflict else None,
                f"{RULE_VERSION}.no-clobber",
            )
        if values_equal(baseline.value, recrawl.value):
            return FieldDecision(
                baseline.value,
                "confirmed_same",
                "confirmed",
                baseline.source_role,
                None,
                f"{RULE_VERSION}.same-value",
            )
        if identity_field:
            return FieldDecision(
                baseline.value,
                "baseline_identity_retained",
                "conflict_preserved",
                baseline.source_role,
                "identity_change",
                f"{RULE_VERSION}.identity-immutable",
            )
        return FieldDecision(
            recrawl.value,
            "recrawl_updated",
            "confirmed",
            recrawl.source_role,
            "baseline_recrawl_difference",
            f"{RULE_VERSION}.latest-valid-unambiguous",
        )

    if recrawl_eligible:
        return FieldDecision(
            recrawl.value,
            "new_from_recrawl" if entity_is_new else "recrawl_filled",
            "confirmed",
            recrawl.source_role,
            None,
            f"{RULE_VERSION}.fill-missing",
        )
    return FieldDecision(
        None,
        "unresolved_missing",
        "conflict_preserved" if recrawl_conflict else "missing",
        None,
        "parser_conflict" if recrawl_conflict else None,
        f"{RULE_VERSION}.abstain",
    )


def is_valid_entity_slug(slug: Any) -> bool:
    """Return whether *slug* is a safe, single Architizer path segment.

    Architizer slugs are source identities, so path-like values must never be
    normalized into a different identity.  Repeated decoding also prevents a
    doubly encoded slash, backslash, dot segment, or control character from
    crossing a later URL-decoding boundary.
    """

    if not isinstance(slug, str) or not slug:
        return False
    candidate = slug
    for _ in range(5):
        if (
            candidate in {".", ".."}
            or "/" in candidate
            or "\\" in candidate
            or _MALFORMED_PERCENT_ESCAPE_RE.search(candidate) is not None
            or any(
                character.isspace()
                or unicodedata.category(character).startswith("C")
                for character in candidate
            )
        ):
            return False
        try:
            decoded = urllib.parse.unquote(candidate, errors="strict")
        except UnicodeDecodeError:
            return False
        if decoded == candidate:
            return True
        candidate = decoded
    # More than four decoding layers is not a plausible source slug and is
    # safer to reject than to let another consumer decode it further.
    return False


def entity_slug_from_url(url: str, entity_type: str) -> Optional[str]:
    plural = ARCHITIZER_ENTITY_COLLECTIONS.get(entity_type)
    if plural is None or not isinstance(url, str) or url != url.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    if (parsed.hostname or "").casefold() not in {
        "architizer.com",
        "www.architizer.com",
    }:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password or port not in {None, 443}:
        return None
    if parsed.query or parsed.fragment:
        return None
    match = re.fullmatch(rf"/{plural}/([^/]+)/?", parsed.path)
    if not match:
        return None
    try:
        slug = urllib.parse.unquote(match.group(1), errors="strict")
    except UnicodeDecodeError:
        return None
    return slug if is_valid_entity_slug(slug) else None


def canonical_entity_url(entity_type: str, slug: str) -> str:
    plural = ARCHITIZER_ENTITY_COLLECTIONS.get(entity_type)
    if plural is None:
        raise ValueError(f"unsupported Architizer entity_type: {entity_type!r}")
    if not is_valid_entity_slug(slug):
        raise ValueError(f"invalid Architizer {entity_type} slug: {slug!r}")
    quoted = urllib.parse.quote(slug, safe="-._~")
    return f"https://architizer.com/{plural}/{quoted}/"


def validate_last_good_identity(
    *,
    entity_type: str,
    target_url: str,
    identity_status: str,
    identity_payload: Mapping[str, Any],
    resolved_values: Mapping[str, Any],
    relationship_slugs: set[str],
    baseline_identity: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Return explicit reasons a last-good version cannot enter the plan."""

    issues: list[str] = []
    if entity_type not in ARCHITIZER_ENTITY_COLLECTIONS:
        return ["unsupported_entity_type"]
    slug = entity_slug_from_url(target_url, entity_type)
    if identity_status != "valid":
        issues.append("last_good_identity_not_valid")
    if not slug:
        issues.append("target_url_not_canonical_entity")
        return issues
    if identity_payload.get("status") != "valid":
        issues.append("identity_payload_not_valid")
    expected_slug = clean_text(identity_payload.get("expected_slug"))
    if expected_slug != slug:
        issues.append("identity_expected_slug_mismatch")
    resolved_slug = clean_text(resolved_values.get("slug"))
    if resolved_slug != slug:
        issues.append("resolved_slug_mismatch")

    if entity_type == "project":
        project_id = normalize_value(resolved_values.get("project_id"), "int")
        global_id = clean_text(resolved_values.get("global_id"))
        name = clean_text(resolved_values.get("name"))
        firm_slug = clean_text(resolved_values.get("firm_slug"))
        if project_id is None:
            issues.append("project_id_missing")
        expected_global = (
            f"projects.project.{project_id}" if project_id is not None else None
        )
        if global_id != expected_global:
            issues.append("project_global_id_mismatch")
        if not name:
            issues.append("project_name_missing")
        if not firm_slug:
            issues.append("project_firm_slug_missing")
        elif relationship_slugs != {firm_slug}:
            issues.append("project_firm_relationship_mismatch")
        payload_id = normalize_value(identity_payload.get("project_id"), "int")
        if payload_id != project_id:
            issues.append("identity_project_id_mismatch")
        if clean_text(identity_payload.get("global_id")) != global_id:
            issues.append("identity_global_id_mismatch")
        if baseline_identity:
            if normalize_value(baseline_identity.get("project_id"), "int") != project_id:
                issues.append("baseline_project_id_mismatch")
            if clean_text(baseline_identity.get("global_id")) != global_id:
                issues.append("baseline_global_id_mismatch")
            if clean_text(baseline_identity.get("slug")) != slug:
                issues.append("baseline_slug_mismatch")
    else:
        if not clean_text(resolved_values.get("name")):
            issues.append("firm_name_missing")
        if baseline_identity and clean_text(baseline_identity.get("slug")) != slug:
            issues.append("baseline_slug_mismatch")
    return sorted(set(issues))


__all__ = [
    "RECONCILIATION_SCHEMA_VERSION",
    "RECONCILIATION_POLICY_VERSION",
    "RECONCILIATION_TOOL_VERSION",
    "RECONCILIATION_READY_VERSION",
    "FINAL_TARGET_SCHEMA_VERSION",
    "RULE_VERSION",
    "ELIGIBLE_RECRAWL_STATUSES",
    "ARCHITIZER_ENTITY_COLLECTIONS",
    "FieldSpec",
    "Candidate",
    "FieldDecision",
    "PROJECT_FIELD_SPECS",
    "FIRM_FIELD_SPECS",
    "FIELD_SPECS",
    "canonical_json",
    "stable_id",
    "clean_text",
    "normalize_value",
    "is_empty",
    "values_equal",
    "reconcile_field",
    "is_valid_entity_slug",
    "entity_slug_from_url",
    "canonical_entity_url",
    "validate_last_good_identity",
]
