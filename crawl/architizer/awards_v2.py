"""Offline parser for current Architizer A+Awards track snapshots.

The legacy awards parser treats every project/firm link as an independent
award row.  Current winners pages expose a stronger card boundary: one
``projects.awardattribution`` card contains the category, one or more award
tiers, a primary project/firm/product, and zero or more firm/brand relations.

This module deliberately does not write either the legacy source DB or the
recrawl sidecar.  It returns a JSON-serializable evidence package that keeps
HTML ``data-*`` attributes separate from DOM-derived values.  A resolved value
is emitted only when the two sources agree; disagreements remain conflicts for
later QA/reconciliation.
"""

from __future__ import annotations

import ast
import re
import unicodedata
import urllib.parse
from collections import Counter
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag


PARSER_VERSION = "architizer-awards-v2.1.0"

_ATTRIBUTION_ID_RE = re.compile(r"^projects\.awardattribution\.(\d+)$")
_MALFORMED_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PATH_KINDS = {
    "projects": "project",
    "firms": "firm",
    "products": "product",
    "brands": "brand",
}
_SUBJECT_KINDS = {"project", "firm", "product"}
_COMPANY_KINDS = {"firm", "brand"}
_TIER_NAMES = {
    "jury winner": "Jury",
    "popular choice winner": "Popular",
    "popular winner": "Popular",
    "finalist": "Finalist",
    "special mention": "Special Mention",
}


def _text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _valid_entity_slug(slug: str) -> bool:
    """Accept only a source identity that remains one safe path segment."""

    candidate = slug
    for _ in range(5):
        if (
            not candidate
            or candidate in {".", ".."}
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
    return False


def _entity_from_url(raw_url: Optional[str], source_url: str) -> Optional[dict]:
    if not raw_url:
        return None
    # Treat entity URLs as identity evidence, not as permissive browser links.
    # The live cards use either a canonical root-relative path or the canonical
    # HTTPS origin.  Query strings such as ``?notfound=1`` have appeared on
    # error-like links and must never become discovery seeds.
    if raw_url != raw_url.strip():
        return None
    try:
        raw_parsed = urllib.parse.urlsplit(raw_url)
        raw_port = raw_parsed.port
    except ValueError:
        return None
    if raw_parsed.scheme or raw_parsed.netloc:
        if (
            raw_parsed.scheme != "https"
            or raw_parsed.netloc != "architizer.com"
            or raw_parsed.username is not None
            or raw_parsed.password is not None
            or raw_port is not None
        ):
            return None
        absolute = raw_url
    else:
        if not raw_url.startswith("/") or raw_url.startswith("//"):
            return None
        absolute = urllib.parse.urljoin("https://architizer.com/", raw_url)
    try:
        parsed = urllib.parse.urlsplit(absolute)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "architizer.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    raw_parts = [part for part in parsed.path.split("/") if part]
    if len(raw_parts) != 2 or raw_parts[0] not in _PATH_KINDS:
        return None
    collection = raw_parts[0]
    try:
        slug = urllib.parse.unquote(raw_parts[1], errors="strict")
    except UnicodeDecodeError:
        return None
    if not _valid_entity_slug(slug):
        return None
    canonical_path = (
        f"/{collection}/{urllib.parse.quote(slug, safe='-._~')}/"
    )
    if parsed.path not in {canonical_path, canonical_path.rstrip("/")}:
        return None
    return {
        "kind": _PATH_KINDS[collection],
        "slug": slug,
        "url": f"https://architizer.com{canonical_path}",
        "raw_url": raw_url,
    }


def _anchor_entities(card: Tag, source_url: str) -> list[dict]:
    entities: list[dict] = []
    for anchor in card.find_all("a", href=True):
        entity = _entity_from_url(anchor.get("href"), source_url)
        if not entity:
            continue
        entity["name"] = _text(anchor.get_text(" ", strip=True))
        entity["classes"] = list(anchor.get("class") or [])
        entities.append(entity)
    return entities


def _parse_string_list(raw: Optional[str]) -> tuple[Optional[list[str]], Optional[str]]:
    if raw is None or not raw.strip():
        return [], None
    try:
        parsed = ast.literal_eval(raw)
    except (SyntaxError, ValueError) as exc:
        return None, f"invalid Python-style list: {type(exc).__name__}"
    if not isinstance(parsed, (list, tuple)) or not all(
        isinstance(value, str) for value in parsed
    ):
        return None, "expected a list of strings"
    return list(parsed), None


def _tier_values(raw_values: list[str]) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    unknown: list[str] = []
    for raw in raw_values:
        label = _text(raw)
        if not label:
            continue
        tier = _TIER_NAMES.get(label.casefold())
        if tier:
            if tier not in normalized:
                normalized.append(tier)
        else:
            unknown.append(label)
    return normalized, unknown


def _same_entity(left: Optional[dict], right: Optional[dict]) -> bool:
    if not left or not right:
        return False
    return (
        left.get("kind") == right.get("kind")
        and left.get("slug") == right.get("slug")
        and _text(left.get("name")) == _text(right.get("name"))
    )


def _company_key(entity: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    return (
        entity.get("kind"),
        entity.get("slug"),
        _text(entity.get("name")),
    )


def _image_identity(raw_url: str) -> Optional[tuple[str, str, Optional[int], str]]:
    try:
        parsed = urllib.parse.urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not hostname:
        return None
    return scheme, hostname, port, parsed.path


def _parse_card(
    card: Tag,
    *,
    source_group_ordinal: int,
    source_card_ordinal: int,
    category: Optional[str],
    award_year: int,
    award_track: str,
    source_url: str,
) -> dict[str, Any]:
    conflicts: list[dict[str, Any]] = []
    missing: list[str] = []
    warnings: list[str] = []

    winner = card.find(
        "div",
        class_=lambda classes: classes and "winner" in classes,
        recursive=False,
    )
    if winner is None:
        return {
            "parser_version": PARSER_VERSION,
            "award_year": award_year,
            "award_track": award_track,
            "source_group_ordinal": source_group_ordinal,
            "source_card_ordinal": source_card_ordinal,
            "source_url": source_url,
            "parse_status": "no_content",
            "missing": ["winner_card"],
            "conflicts": [],
            "warnings": [],
            "card_attribute_values": {
                "data-types": card.get("data-types") or "",
            },
            "attribute_values": {},
            "dom_values": {},
        }

    attributes = {
        key: value
        for key, value in sorted(winner.attrs.items())
        if key.startswith("data-")
    }
    attribution_global_id = attributes.get("data-id")
    attribution_match = _ATTRIBUTION_ID_RE.fullmatch(
        attribution_global_id or ""
    )
    attribution_id = int(attribution_match.group(1)) if attribution_match else None
    if attribution_id is None:
        missing.append("award_attribution_id")

    attribute_subject = _entity_from_url(attributes.get("data-url"), source_url)
    attribute_slug_conflict = False
    if attribute_subject:
        attribute_subject["name"] = _text(attributes.get("data-name"))
        attribute_slug = attributes.get("data-slug")
        if attribute_slug and attribute_slug != attribute_subject["slug"]:
            attribute_slug_conflict = True
            conflicts.append(
                {
                    "field": "subject_slug",
                    "attribute_url_slug": attribute_subject["slug"],
                    "attribute_data_slug": attribute_slug,
                }
            )
    else:
        missing.append("attribute_subject")

    anchors = _anchor_entities(card, source_url)
    if attribute_subject:
        subject_anchors = [
            entity
            for entity in anchors
            if entity["kind"] == attribute_subject["kind"]
        ]
    else:
        subject_anchors = [
            entity
            for entity in anchors
            if entity["kind"] in _SUBJECT_KINDS
            and "text-dark" in entity.get("classes", [])
        ]
    dom_subject = subject_anchors[0] if subject_anchors else None
    if len(subject_anchors) > 1:
        conflicts.append(
            {
                "field": "dom_subject",
                "reason": "multiple_subject_anchors",
                "values": subject_anchors,
            }
        )
    if dom_subject is None:
        missing.append("dom_subject")

    resolved_subject = None
    if (
        attribute_subject
        and dom_subject
        and len(subject_anchors) == 1
        and not attribute_slug_conflict
    ):
        if _same_entity(attribute_subject, dom_subject):
            resolved_subject = {
                key: attribute_subject[key]
                for key in ("kind", "slug", "url", "name")
            }
        else:
            conflicts.append(
                {
                    "field": "subject",
                    "attribute": attribute_subject,
                    "dom": dom_subject,
                }
            )

    company_names, names_error = _parse_string_list(
        attributes.get("data-company-names")
    )
    company_urls, urls_error = _parse_string_list(
        attributes.get("data-company-urls")
    )
    if names_error:
        conflicts.append(
            {"field": "company_names_attribute", "reason": names_error}
        )
    if urls_error:
        conflicts.append(
            {"field": "company_urls_attribute", "reason": urls_error}
        )

    attribute_companies: Optional[list[dict]] = None
    if company_names is not None and company_urls is not None:
        if len(company_names) != len(company_urls):
            conflicts.append(
                {
                    "field": "attribute_companies",
                    "reason": "name_url_length_mismatch",
                    "name_count": len(company_names),
                    "url_count": len(company_urls),
                }
            )
        else:
            attribute_companies = []
            for name, raw_url in zip(company_names, company_urls):
                entity = _entity_from_url(raw_url, source_url)
                if not entity or entity["kind"] not in _COMPANY_KINDS:
                    conflicts.append(
                        {
                            "field": "attribute_company_url",
                            "reason": "unsupported_company_url",
                            "value": raw_url,
                        }
                    )
                    attribute_companies = None
                    break
                entity["name"] = _text(name)
                attribute_companies.append(entity)

    dom_companies = [
        entity
        for entity in anchors
        if entity is not dom_subject and entity["kind"] in _COMPANY_KINDS
    ]
    resolved_companies = None
    if attribute_companies is not None:
        if [_company_key(value) for value in attribute_companies] == [
            _company_key(value) for value in dom_companies
        ]:
            resolved_companies = [
                {key: value[key] for key in ("kind", "slug", "url", "name")}
                for value in attribute_companies
            ]
        else:
            conflicts.append(
                {
                    "field": "companies",
                    "attribute": attribute_companies,
                    "dom": dom_companies,
                }
            )

    raw_attribute_tiers = [
        item.strip()
        for item in (card.get("data-types") or "").split(",")
        if item.strip()
    ]
    raw_dom_tiers = [
        value
        for value in (
            _text(badge.get_text(" ", strip=True))
            for badge in card.select(".awards .badge")
        )
        if value
    ]
    attribute_tiers, attribute_unknown = _tier_values(raw_attribute_tiers)
    dom_tiers, dom_unknown = _tier_values(raw_dom_tiers)
    if attribute_unknown or dom_unknown:
        conflicts.append(
            {
                "field": "award_tiers",
                "reason": "unknown_tier_label",
                "attribute_unknown": attribute_unknown,
                "dom_unknown": dom_unknown,
            }
        )
    resolved_tiers = None
    if (
        not attribute_unknown
        and not dom_unknown
        and attribute_tiers
        and dom_tiers
    ):
        if attribute_tiers == dom_tiers:
            resolved_tiers = attribute_tiers
        else:
            conflicts.append(
                {
                    "field": "award_tiers",
                    "attribute": attribute_tiers,
                    "dom": dom_tiers,
                }
            )
    else:
        missing.append("award_tiers")

    category_path = [
        part
        for part in (_text(value) for value in (category or "").split(" > "))
        if part
    ]
    if not category_path:
        missing.append("award_category")

    image_attribute = attributes.get("data-image") or None
    image_tag = winner.find("img")
    image_dom = None
    if image_tag:
        image_dom = image_tag.get("data-src") or image_tag.get("src")
    resolved_image = None
    image_resolution_status = "missing"
    if image_attribute and image_dom:
        attribute_image_identity = _image_identity(image_attribute)
        dom_image_identity = _image_identity(image_dom)
        if (
            attribute_image_identity is None
            or dom_image_identity is None
            or attribute_image_identity != dom_image_identity
        ):
            image_resolution_status = "conflict"
            conflicts.append(
                {
                    "field": "image_url",
                    "attribute": image_attribute,
                    "dom": image_dom,
                }
            )
        else:
            resolved_image = image_attribute
            image_resolution_status = "agreed"
    elif image_attribute:
        image_resolution_status = "attribute_only"
        warnings.append("image_url_attribute_only")
    elif image_dom:
        image_resolution_status = "dom_only"
        warnings.append("image_url_dom_only")
    elif not image_attribute and not image_dom:
        warnings.append("image_url_missing")

    status = "complete"
    if conflicts:
        status = "conflict"
    elif missing:
        status = "partial"

    return {
        "parser_version": PARSER_VERSION,
        "award_year": award_year,
        "award_track": award_track,
        "source_group_ordinal": source_group_ordinal,
        "source_card_ordinal": source_card_ordinal,
        "award_attribution_id": attribution_id,
        "award_attribution_global_id": attribution_global_id,
        "award_category": category,
        "award_category_path": category_path,
        "award_tiers": resolved_tiers,
        "raw_tier_attribute_labels": raw_attribute_tiers,
        "raw_tier_dom_labels": raw_dom_tiers,
        "subject": resolved_subject,
        "companies": resolved_companies,
        "description": attributes.get("data-description") or None,
        "image_url": resolved_image,
        "image_resolution_status": image_resolution_status,
        "parse_status": status,
        "missing": missing,
        "conflicts": conflicts,
        "warnings": warnings,
        "source_url": source_url,
        "card_attribute_values": {
            "data-types": card.get("data-types") or "",
        },
        "attribute_values": attributes,
        "dom_values": {
            "category": category,
            "tier_badges": raw_dom_tiers,
            "subject": dom_subject,
            "companies": dom_companies,
            "image_url": image_dom,
        },
    }


def parse_awards_track_snapshot(
    html: str,
    *,
    source_url: str,
    award_year: int,
    award_track: str,
) -> dict[str, Any]:
    """Parse one saved winners track page without making network requests."""

    if award_year < 2013 or award_year > 9999:
        raise ValueError(f"implausible award year: {award_year}")
    if not award_track or not award_track.strip():
        raise ValueError("award_track must be non-empty")

    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []
    for group_ordinal, group in enumerate(
        soup.select(".container-awards > .row")
    ):
        title = group.find(
            class_=lambda classes: classes and "group-title" in classes,
            recursive=False,
        )
        cards = group.find_all(
            class_=lambda classes: classes and "winner-container" in classes,
            recursive=False,
        )
        if not cards:
            continue
        category = _text(title.get_text(" ", strip=True)) if title else None
        for card_ordinal, card in enumerate(cards):
            records.append(
                _parse_card(
                    card,
                    source_group_ordinal=group_ordinal,
                    source_card_ordinal=card_ordinal,
                    category=category,
                    award_year=award_year,
                    award_track=award_track.strip(),
                    source_url=source_url,
                )
            )

    attribution_ids = [
        record["award_attribution_id"]
        for record in records
        if record.get("award_attribution_id") is not None
    ]
    duplicate_ids = sorted(
        attribution_id
        for attribution_id, count in Counter(attribution_ids).items()
        if count > 1
    )
    duplicate_id_set = set(duplicate_ids)
    if duplicate_id_set:
        duplicate_counts = Counter(attribution_ids)
        for record in records:
            attribution_id = record.get("award_attribution_id")
            if attribution_id not in duplicate_id_set:
                continue
            record["conflicts"].append(
                {
                    "field": "award_attribution_global_id",
                    "reason": "duplicate_on_page",
                    "occurrence_count": duplicate_counts[attribution_id],
                    "value": record.get("award_attribution_global_id"),
                }
            )
            # The duplicated value remains available in raw winner attributes
            # and in the conflict evidence above, but is not emitted as a
            # resolved attribution identity.
            record["award_attribution_id"] = None
            record["award_attribution_global_id"] = None
            record["parse_status"] = "conflict"

    status_counts = Counter(record["parse_status"] for record in records)
    if not records:
        page_status = "no_content"
    elif status_counts.get("conflict") or status_counts.get("no_content"):
        page_status = "conflict"
    elif status_counts.get("partial"):
        page_status = "partial"
    else:
        page_status = "complete"

    return {
        "parser_version": PARSER_VERSION,
        "award_year": award_year,
        "award_track": award_track.strip(),
        "source_url": source_url,
        "page_title": _text(soup.title.get_text(" ", strip=True))
        if soup.title
        else None,
        "parse_status": page_status,
        "record_count": len(records),
        "status_counts": dict(sorted(status_counts.items())),
        "duplicate_award_attribution_ids": duplicate_ids,
        "records": records,
    }
