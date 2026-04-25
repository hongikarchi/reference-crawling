"""HTML parsers for Divisare pages (Phase 1).

Selectors verified against saved samples in `data/divisare_samples/` per
`.claude/research/divisare-schema.md`. All parsers are tolerant of missing
fields — they return `None` rather than raising.

Public functions:
  parse_project_page(html, url)   -> dict
  parse_architect_page(html, url) -> dict
  parse_tag_page(html)            -> dict (name + curated + project_urls + has_next)
  parse_designer_index(html)      -> list[str]   author URLs
  parse_author_built_projects(html, url) -> dict (project_urls + has_next)
"""

from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTHOR_ID_RE = re.compile(r"/authors/(\d+)-([^/?#]+)")
_PROJECT_ID_RE = re.compile(r"/projects/(\d+)-([^/?#]+)")


def _id_slug_from_project_url(url: str) -> tuple[Optional[int], Optional[str]]:
    m = _PROJECT_ID_RE.search(url)
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def _id_slug_from_architect_url(url: str) -> tuple[Optional[int], Optional[str]]:
    m = _AUTHOR_ID_RE.search(url)
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def _meta_content(soup: BeautifulSoup, key: str) -> Optional[str]:
    """Return content of <meta property=key> or <meta name=key>."""
    tag = soup.find("meta", attrs={"property": key}) or \
          soup.find("meta", attrs={"name": key})
    return tag.get("content") if tag else None


def _project_h1(soup: BeautifulSoup) -> Optional[str]:
    """The project H1 — skip the two 'divisare' branding H1s in the masthead."""
    for h1 in soup.find_all("h1"):
        text = h1.get_text(strip=True)
        if text and text.lower() != "divisare":
            return text
    return None


def _section_value(sidebar, label: str) -> Optional[str]:
    """In div.sidebar, find a div.content whose .section == label, return next-sibling text."""
    if sidebar is None:
        return None
    for content in sidebar.find_all("div", class_="content"):
        section = content.find("div", class_="section")
        if section and section.get_text(strip=True).lower() == label.lower():
            value_div = section.find_next_sibling("div")
            if value_div is not None:
                return value_div.get_text(" ", strip=True)
    return None


def _section_value_links(sidebar, label: str) -> list[tuple[str, str]]:
    """Like _section_value but return (href, text) for any <a> in the value div."""
    if sidebar is None:
        return []
    for content in sidebar.find_all("div", class_="content"):
        section = content.find("div", class_="section")
        if section and section.get_text(strip=True).lower() == label.lower():
            value_div = section.find_next_sibling("div")
            if value_div:
                return [(a.get("href"), a.get_text(strip=True))
                        for a in value_div.find_all("a", href=True)]
    return []


# ---------------------------------------------------------------------------
# Project page
# ---------------------------------------------------------------------------

def parse_project_page(html: str, url: str) -> dict:
    """Parse a Divisare project page into a dict ready for divisare_db.upsert_project()."""
    soup = BeautifulSoup(html, "lxml")
    project_id, slug = _id_slug_from_project_url(url)

    project_div = soup.find("div", class_="project")
    header = project_div.find("div", class_="header") if project_div else None
    sidebar = project_div.find("div", class_="sidebar") if project_div else None

    name = _project_h1(soup)
    abstract_el = header.find("div", class_="abstract") if header else None
    abstract = abstract_el.get_text(" ", strip=True) if abstract_el else None

    desc_el = project_div.find("div", class_="description") if project_div else None
    description = desc_el.get_text(" ", strip=True) if desc_el else None

    # Designer (architect) — link in sidebar
    architect_links = _section_value_links(sidebar, "Designer")
    architect_ids: list[int] = []
    architect_names: list[str] = []
    for href, text in architect_links:
        aid, _aslug = _id_slug_from_architect_url(href or "")
        if aid is not None:
            architect_ids.append(aid)
            architect_names.append(text)

    # Location: "Mexico - Santiago" → country, city
    location_country = location_city = None
    loc_text = _section_value(sidebar, "Location")
    if loc_text:
        parts = [p.strip() for p in loc_text.split(" - ", 1)]
        location_country = parts[0] if parts else None
        location_city = parts[1] if len(parts) > 1 else None

    # Project Year
    year_text = _section_value(sidebar, "Project Year")
    project_year: Optional[int] = None
    if year_text and year_text.strip().isdigit():
        try:
            project_year = int(year_text.strip())
        except ValueError:
            pass

    # Published date — div.divider.first .text "Published on …"
    published_date = None
    if sidebar is not None:
        first_divider = sidebar.find("div", class_="divider")
        if first_divider and "first" in (first_divider.get("class") or []):
            txt = first_divider.get_text(" ", strip=True)
            m = re.match(r"Published\s+on\s+(.+)", txt, re.I)
            if m:
                published_date = m.group(1).strip()

    # Album — the .divider that immediately precedes the ul.tags block.
    # (Cleaner than text-based filtering; "Credits" et al. are also dividers
    # but they sit BEFORE the credits-content blocks, not before tags.)
    album_name = None
    if sidebar is not None:
        ul_tags = sidebar.find("ul", class_="tags")
        if ul_tags is not None:
            # Walk backward through previous DOM siblings of the .row that
            # contains ul.tags, looking for a .divider.
            parent_row = ul_tags.find_parent("div", class_="row")
            cursor = parent_row.find_previous_sibling("div", class_="row") if parent_row else None
            while cursor is not None:
                d = cursor.find("div", class_="divider")
                if d is not None and "first" not in (d.get("class") or []):
                    txt = d.get_text(" ", strip=True)
                    if txt:
                        album_name = txt
                        break
                cursor = cursor.find_previous_sibling("div", class_="row")

    # Tags — ul.tags li a
    tag_slugs: list[str] = []
    if sidebar is not None:
        for ul in sidebar.find_all("ul", class_="tags"):
            for a in ul.find_all("a", href=True):
                href = a["href"]
                # Either "https://divisare.com/{slug}" or "/{slug}"
                m = re.match(r"^(?:https?://divisare\.com)?/([^/?#]+)$", href)
                if m:
                    tag_slugs.append(m.group(1))

    cover = _meta_content(soup, "og:image")

    return {
        "id":               project_id,
        "slug":             slug,
        "name":             name,
        "architect_ids":    architect_ids,
        "architect_names":  architect_names,
        "location_country": location_country,
        "location_city":    location_city,
        "project_year":     project_year,
        "published_date":   published_date,
        "abstract":         abstract,
        "description":      description,
        "album_name":       album_name,
        "tag_slugs":        tag_slugs,
        "cover_image_url":  cover,
    }


# ---------------------------------------------------------------------------
# Architect page
# ---------------------------------------------------------------------------

_PHONE_RE   = re.compile(r"Phone:\s*([+\d()\s\-\.]+)", re.I)
_WEBSITE_RE = re.compile(r"\bwww\.[A-Za-z0-9\-\._/]+\b")


def parse_architect_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    aid, slug = _id_slug_from_architect_url(url)

    # Name = first non-branding H1
    name = None
    for h1 in soup.find_all("h1"):
        t = h1.get_text(strip=True)
        if t and t.lower() != "divisare":
            name = t
            break

    # Description / address text — search .description first, then .sidebar
    desc_el = soup.find(class_="description") or soup.find(class_="sidebar")
    desc_text = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Heuristic short description: "<Name> is an architectural practice based in <City>, <Country>."
    description = None
    m = re.match(r"(.+?\.)", desc_text)
    if m:
        description = m.group(1).strip()

    # Address lines: take the FIRST "City, Country" pattern (typically office HQ).
    # Each name capped at 1-2 capitalized words; otherwise greedy matching
    # produces "United Kingdom London" when two addresses run together
    # without a delimiter.
    country = city = None
    addr_re = re.compile(
        r"([A-Z][a-zA-Z\-]+(?:\s[A-Z][a-zA-Z\-]+)?),"
        r"\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)"
    )
    addr_match = addr_re.search(desc_text or "")
    if addr_match:
        city = addr_match.group(1).strip()
        country = addr_match.group(2).strip()

    phone = None
    pm = _PHONE_RE.search(desc_text or "")
    if pm:
        phone = pm.group(1).strip()

    website = None
    wm = _WEBSITE_RE.search(desc_text or "")
    if wm:
        website = wm.group(0)

    # Number of projects shown (proxy for project_count_seen — pagination not
    # consulted here)
    project_links = set(re.findall(r'/projects/(\d+)-', html))
    project_count_seen = len(project_links)

    return {
        "id":                  aid,
        "slug":                slug,
        "name":                name,
        "description":         description,
        "country":             country,
        "city":                city,
        "website":             website,
        "phone":               phone,
        "project_count_seen":  project_count_seen,
    }


# ---------------------------------------------------------------------------
# Tag page
# ---------------------------------------------------------------------------

def parse_tag_page(html: str, slug: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    name = None
    for h1 in soup.find_all("h1"):
        t = h1.get_text(strip=True)
        if t and t.lower() != "divisare":
            name = t
            break

    title_text = soup.title.get_text(" ", strip=True) if soup.title else ""
    curated = "curated by divisare" in title_text.lower()

    project_paths = sorted(set(re.findall(r'href="(/projects/\d+-[^"#?]+)"', html)))

    next_page_links = re.findall(r'href="([^"]*\bpage=\d+[^"]*)"', html)
    has_next = bool(next_page_links)

    return {
        "slug":              slug,
        "name":              name,
        "curated":           1 if curated else 0,
        "project_paths":     project_paths,
        "project_count_seen": len(project_paths),
        "has_next":          has_next,
        "next_page_paths":   sorted(set(next_page_links))[:5],
    }


# ---------------------------------------------------------------------------
# Designer index pages (/projects "General Index" + /designers/{region})
# ---------------------------------------------------------------------------

def parse_designer_index(html: str) -> list[str]:
    """Return list of /authors/{id}-{slug} URLs found in a /designers/{region} page.

    Note: Divisare's `/projects` page is misleadingly labeled "General Index"
    — it actually lists `/designers/{region}` URLs. Discovery walks each
    region page and calls THIS function on the result HTML.
    """
    return sorted(set(re.findall(r'href="(/authors/\d+-[^"#?]+)"', html)))


def parse_designers_region_pages(html: str) -> dict:
    """For a /designers/{region} page, return author URLs + pagination."""
    authors = sorted(set(re.findall(r'href="(/authors/\d+-[^"#?]+)"', html)))
    page_links = re.findall(r'href="([^"]*\bpage=\d+[^"]*)"', html)
    max_page = 1
    for p in page_links:
        m = re.search(r"page=(\d+)", p)
        if m:
            max_page = max(max_page, int(m.group(1)))
    return {
        "author_paths": authors,
        "max_page":     max_page,
    }


DIVISARE_TOP_REGIONS = ("europe", "asia", "americas", "africa", "oceania")


def designer_region_urls() -> list[str]:
    """The five top-level /designers/{region} URLs (stable Divisare UI)."""
    return [f"/designers/{r}" for r in DIVISARE_TOP_REGIONS]


def parse_projects_general_index(html: str) -> list[str]:
    """All designer-region paths in the /projects "General Index" page —
    includes both top-level (`/designers/europe`) and subregion
    (`/designers/europe/southern-europe/albania`) variants."""
    return sorted(set(re.findall(r'href="(/designers/[a-z\-/]+)"', html)))


def parse_author_built_projects(html: str) -> dict:
    """Return project paths + pagination hint from an architect's /projects/built page."""
    project_paths = sorted(set(re.findall(r'href="(/projects/\d+-[^"#?]+)"', html)))
    next_page_links = re.findall(r'href="([^"]*\bpage=\d+[^"]*)"', html)
    return {
        "project_paths":   project_paths,
        "has_next":        bool(next_page_links),
        "next_page_paths": sorted(set(next_page_links))[:5],
    }
