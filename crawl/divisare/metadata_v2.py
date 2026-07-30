"""DOM-aware metadata extraction for Divisare project HTML."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup


PARSER_VERSION = "divisare-html-metadata-v2.3"

_PROJECT_URL_RE = re.compile(r"/projects/(\d+)-([^/?#]+)")
_UI_RE = re.compile(
    r"\bAdd to collection\s+Choose collection\.\.\.\s+New collection\.\.\.",
    re.IGNORECASE,
)
_MEDIA_SELECTORS = (
    "div.image",
    "div.images",
    "div.zoom",
    "figure",
    "figcaption",
    "picture",
    ".caption",
    ".captions",
    ".photographer",
    ".credit",
    ".credits",
    ".collection",
    ".add-to-collection",
    "script",
    "style",
    "noscript",
    "template",
)
_EXPLICIT_KIND_MAP = {
    "project": "project",
    "built project": "project",
    "drawing": "drawing_feature",
    "drawings": "drawing_feature",
    "drawing feature": "drawing_feature",
    "plans": "drawing_feature",
    "photo feature": "photo_feature",
    "photo essay": "photo_feature",
    "photography": "photo_feature",
    "model": "model_feature",
    "model feature": "model_feature",
    "concept": "concept_editorial",
    "concept editorial": "concept_editorial",
    "editorial": "concept_editorial",
}


@dataclass(frozen=True)
class ParsedMetadata:
    article_id: Optional[int]
    slug: Optional[str]
    name: Optional[str]
    abstract: Optional[str]
    location_country: Optional[str]
    location_city: Optional[str]
    project_year: Optional[int]
    area_sqm: Optional[float]
    area_raw: Optional[str]
    description_prose: Optional[str]
    description_quality: str
    explicit_article_kind: Optional[str]
    explicit_article_kind_raw: Optional[str]
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).replace("\u00a0", " ").split())
    return text or None


def _project_identity(url: str) -> Tuple[Optional[int], Optional[str]]:
    match = _PROJECT_URL_RE.search(url or "")
    if not match:
        return None, None
    return int(match.group(1)), match.group(2)


def _project_h1(soup: BeautifulSoup) -> Optional[str]:
    for h1 in soup.find_all("h1"):
        text = _clean_text(h1.get_text(" ", strip=True))
        if text and text.casefold() != "divisare":
            return text
    return None


def _section_value(sidebar: Any, labels: Tuple[str, ...]) -> Optional[str]:
    if sidebar is None:
        return None
    normalized_labels = {label.casefold() for label in labels}
    for content in sidebar.find_all("div", class_="content"):
        section = content.find("div", class_="section")
        if section is None:
            continue
        label = _clean_text(section.get_text(" ", strip=True))
        if not label or label.casefold() not in normalized_labels:
            continue
        value = section.find_next_sibling("div")
        return _clean_text(value.get_text(" ", strip=True)) if value else None
    return None


def _project_fact_value(
    project_div: Any,
    labels: Tuple[str, ...],
) -> Tuple[Optional[str], Optional[str]]:
    if project_div is None:
        return None, None
    normalized_labels = {label.casefold() for label in labels}
    for facts in project_div.find_all("div", class_="project_fact"):
        for item in facts.find_all("li"):
            label_node = item.find("span", recursive=False) or item.find("span")
            label = (
                _clean_text(label_node.get_text(" ", strip=True))
                if label_node is not None
                else None
            )
            if not label or label.casefold() not in normalized_labels:
                continue
            fragment = BeautifulSoup(str(item), "lxml")
            cloned_item = fragment.find("li")
            cloned_label = cloned_item.find("span") if cloned_item else None
            if cloned_label is not None:
                cloned_label.decompose()
            value = (
                _clean_text(cloned_item.get_text(" ", strip=True))
                if cloned_item is not None
                else None
            )
            return label, value
    return None, None


def _parse_area(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = re.search(r"[-+]?\d[\d\s.,]*", value)
    if not match:
        return None
    number = match.group(0).strip().replace(" ", "")
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
        parsed = float(number)
    except ValueError:
        return None
    if parsed <= 0 or parsed > 100_000_000:
        return None
    return parsed


def _extract_description(project_div: Any) -> Tuple[Optional[str], str, Dict[str, Any]]:
    description = (
        project_div.find("div", class_="description")
        if project_div is not None
        else None
    )
    if description is None:
        return None, "no_description_dom", {
            "removed_media_nodes": 0,
            "removed_ui_markers": 0,
            "paragraph_count": 0,
        }

    fragment = BeautifulSoup(str(description), "lxml")
    root = fragment.find("div", class_="description")
    if root is None:
        return None, "description_dom_parse_failed", {
            "removed_media_nodes": 0,
            "removed_ui_markers": 0,
            "paragraph_count": 0,
        }

    removed = 0
    for selector in _MEDIA_SELECTORS:
        for node in list(root.select(selector)):
            node.decompose()
            removed += 1

    paragraphs: List[str] = []
    removed_ui = 0
    for paragraph in root.find_all("p", recursive=False):
        text = _clean_text(paragraph.get_text(" ", strip=True))
        if not text:
            continue
        marker_count = len(_UI_RE.findall(text))
        removed_ui += marker_count
        text = _clean_text(_UI_RE.sub(" ", text))
        if text:
            paragraphs.append(text)

    if paragraphs:
        prose = "\n\n".join(dict.fromkeys(paragraphs))
        quality = "dom_prose_paragraphs"
    else:
        text = _clean_text(root.get_text(" ", strip=True))
        marker_count = len(_UI_RE.findall(text or ""))
        removed_ui += marker_count
        prose = _clean_text(_UI_RE.sub(" ", text or ""))
        quality = "dom_text_fallback_review" if prose else "no_prose_content"

    return prose, quality, {
        "removed_media_nodes": removed,
        "removed_ui_markers": removed_ui,
        "paragraph_count": len(paragraphs),
        "paragraph_selector": "description_direct_p",
        "fallback_used": quality == "dom_text_fallback_review",
        "prose_sha256": (
            hashlib.sha256(prose.encode("utf-8")).hexdigest() if prose else None
        ),
    }


def _explicit_article_kind(
    soup: BeautifulSoup,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    candidates: List[Tuple[str, str]] = []
    for node in soup.select("[data-article-kind]"):
        candidates.append(("data-article-kind", node.get("data-article-kind", "")))
    for key in ("article-kind", "article_kind", "divisare:article_kind"):
        meta = soup.find("meta", attrs={"name": key}) or soup.find(
            "meta", attrs={"property": key}
        )
        if meta is not None:
            candidates.append(("meta:%s" % key, meta.get("content", "")))
    for selector in (".article-kind", ".article_type", ".project-kind"):
        for node in soup.select(selector):
            candidates.append((selector, node.get_text(" ", strip=True)))

    for source, raw in candidates:
        normalized = _clean_text(raw)
        if not normalized:
            continue
        mapped = _EXPLICIT_KIND_MAP.get(normalized.casefold())
        if mapped:
            return mapped, normalized, source
    return None, None, None


def looks_like_login_wall(
    html: str,
    final_url: str,
    status_code: int = 200,
) -> bool:
    if status_code in {401, 403}:
        return True
    lowered_url = (final_url or "").casefold()
    if "/login" in lowered_url or "/people/login" in lowered_url:
        return True
    soup = BeautifulSoup(html or "", "lxml")
    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    has_project_dom = soup.find("div", class_="project") is not None
    if (
        title
        and not has_project_dom
        and re.search(r"\b(?:login|log in|sign in)\b", title, re.IGNORECASE)
    ):
        return True
    return bool(
        soup.find("form", action=re.compile(r"/people/login"))
        and soup.find("input", attrs={"name": "person[email]"})
        and soup.find("div", class_="project") is None
    )


def parse_project_metadata(
    html: str,
    url: str,
    *,
    expected_article_id: Optional[int] = None,
) -> ParsedMetadata:
    soup = BeautifulSoup(html or "", "lxml")
    url_article_id, slug = _project_identity(url)
    article_id = (
        int(expected_article_id)
        if expected_article_id is not None
        else url_article_id
    )
    page_url_matches_expected = (
        article_id is not None and url_article_id == article_id
    )
    project_divs = soup.find_all("div", class_="project")
    project_div = project_divs[0] if project_divs else None
    project_dom_id_raw = (
        _clean_text(project_div.get("data-project-id"))
        if project_div is not None
        else None
    )
    try:
        project_dom_id = int(project_dom_id_raw) if project_dom_id_raw else None
    except ValueError:
        project_dom_id = None
    project_dom_id_matches_url = (
        len(project_divs) == 1
        and article_id is not None
        and project_dom_id == article_id
    )
    header = project_div.find("div", class_="header") if project_div else None
    sidebar = project_div.find("div", class_="sidebar") if project_div else None

    abstract_node = header.find("div", class_="abstract") if header else None
    abstract = (
        _clean_text(abstract_node.get_text(" ", strip=True))
        if abstract_node
        else None
    )
    location_raw = _section_value(sidebar, ("Location",))
    location_country = location_city = None
    if location_raw:
        parts = [part.strip() for part in location_raw.split(" - ", 1)]
        location_country = parts[0] or None
        location_city = (parts[1] or None) if len(parts) > 1 else None

    year_raw = _section_value(sidebar, ("Project Year",))
    year_match = re.search(r"\b(18|19|20|21)\d{2}\b", year_raw or "")
    project_year = int(year_match.group(0)) if year_match else None
    area_label_raw, area_raw = _project_fact_value(
        project_div,
        ("Built Surface",),
    )
    description, description_quality, description_details = _extract_description(
        project_div
    )
    article_kind, article_kind_raw, article_kind_source = _explicit_article_kind(
        soup
    )

    return ParsedMetadata(
        article_id=article_id,
        slug=slug,
        name=_project_h1(soup),
        abstract=abstract,
        location_country=location_country,
        location_city=location_city,
        project_year=project_year,
        area_sqm=_parse_area(area_raw),
        area_raw=area_raw,
        description_prose=description,
        description_quality=description_quality,
        explicit_article_kind=article_kind,
        explicit_article_kind_raw=article_kind_raw,
        details={
            **description_details,
            "has_project_dom": project_div is not None,
            "project_dom_count": len(project_divs),
            "project_dom_id": project_dom_id,
            "project_dom_id_raw": project_dom_id_raw,
            "project_dom_id_matches_url": project_dom_id_matches_url,
            "page_url_article_id": url_article_id,
            "page_url_matches_expected": page_url_matches_expected,
            "location_raw": location_raw,
            "project_year_raw": year_raw,
            "area_label_raw": area_label_raw,
            "area_value_raw": area_raw,
            "area_source": (
                "project_fact_dimensions" if area_raw is not None else None
            ),
            "area_unit_status": (
                "implicit_square_metres_divisare"
                if area_raw is not None
                else None
            ),
            "area_confidence": 0.75 if area_raw is not None else None,
            "explicit_article_kind_source": article_kind_source,
            "parser_version": PARSER_VERSION,
        },
    )


__all__ = [
    "PARSER_VERSION",
    "ParsedMetadata",
    "looks_like_login_wall",
    "parse_project_metadata",
]
