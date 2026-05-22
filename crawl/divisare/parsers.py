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


def _section_label_matches(text: str, label) -> bool:
    labels = label if isinstance(label, (tuple, list, set)) else (label,)
    normalized = text.strip().lower()
    return any(normalized == str(item).strip().lower() for item in labels)


def _section_value(sidebar, label: str) -> Optional[str]:
    """In div.sidebar, find a div.content whose .section == label, return next-sibling text."""
    if sidebar is None:
        return None
    for content in sidebar.find_all("div", class_="content"):
        section = content.find("div", class_="section")
        if section and _section_label_matches(section.get_text(strip=True), label):
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
        if section and _section_label_matches(section.get_text(strip=True), label):
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

    # Designer (architect) — link in sidebar. Divisare uses both singular
    # and plural labels depending on the page.
    architect_links = _section_value_links(sidebar, ("Designer", "Designers"))
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

    # Area (sqm). Divisare's "Area" section is sometimes present, format varies:
    # "1500", "1,500 sqm", "1500 m²", "1500 m2". Extract first number.
    area_text = _section_value(sidebar, "Area") or _section_value(sidebar, "Surface")
    area_sqm: Optional[float] = None
    if area_text:
        m = re.search(r"([\d,\.]+)", area_text)
        if m:
            try:
                area_sqm = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

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

    # Credits — `<div class="credit">` blocks under the sidebar (only on
    # projects rich enough to list collaborators). Each credit contains:
    #   <div class="role">Acoustic</div>
    #   <p>Nexus Audio Video with Ole Christensen</p>
    # Skip "Design"/"Designer" credits — already captured as architect_ids/names.
    _CREDITS_SKIP = {"design", "designer", "designers"}
    credits: dict = {}
    if sidebar is not None:
        for credit_div in sidebar.find_all("div", class_="credit"):
            role_el = credit_div.find("div", class_="role")
            if role_el is None:
                continue
            role = role_el.get_text(strip=True)
            if not role or role.lower() in _CREDITS_SKIP:
                continue
            # Firm names live in <p> tags of the second .row sibling
            firm_texts = []
            for p in credit_div.find_all("p"):
                t = p.get_text(" ", strip=True)
                if t:
                    firm_texts.append(t)
            if not firm_texts:
                continue
            role_key = re.sub(r"\s+", "_", role.lower().strip())
            credits.setdefault(role_key, []).extend(firm_texts)

    # Gallery images — lazy-loaded `<img data-src="...">` (and friends) anywhere
    # under .project. The actual gallery `.image` divs are nested inside
    # `.description > .image > .zoom > img`; rather than guess scope, we sweep
    # the whole project subtree and dedup by URL. Cover the lazy-load attribute
    # variants Divisare uses today (`data-src` is current, others future-proof).
    gallery_urls: list[str] = []
    seen_urls: set = set()
    if project_div is not None:
        for img in project_div.find_all("img"):
            for attr in ("data-src", "data-original", "data-lazy", "src"):
                u = img.get(attr)
                if u and u.startswith("http") and "divisare" in u and u not in seen_urls:
                    seen_urls.add(u)
                    gallery_urls.append(u)
                    break  # one URL per <img>; data-src wins over plain src

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
        "area_sqm":         area_sqm,
        "abstract":         abstract,
        "description":      description,
        "tag_slugs":        tag_slugs,
        "cover_image_url":  cover,
        "gallery_urls":     gallery_urls,
        "credits":          credits if credits else None,
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


# ---------------------------------------------------------------------------
# Homepage mega-menu taxonomy (Phase 1.5 — album hierarchy)
# ---------------------------------------------------------------------------

def _slugify_album_name(name: str) -> str:
    """'Plans & Details' → 'plans-details', 'designers by Country' → 'designers-by-country'."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def parse_homepage_taxonomy(html: str) -> list[dict]:
    """Parse the homepage mega-menu — extract the 14 top-level browsing
    albums (Elements / Cities / Houses / Ideas / Materiality / Plans & Details
    / Private Interiors / Public Interiors / Topics / Types / designers by
    Country / designers by City / photographers by Country / photographers by City).

    Each album is a `<li>` containing:
      <a>AlbumName</a>           ← parent label, NO href
      <ul>
        <li><div class="row"><div class="columns">
          <a href="/aarhus">Aarhus</a>
          <a href="/abu-dhabi">Abu Dhabi</a>
          ...
        </div></div></li>
      </ul>

    Returns: list of dicts:
      {"album_slug", "album_name", "kind", "children": [
          {"child_slug", "child_name", "child_url"}, ...
      ]}
    """
    soup = BeautifulSoup(html, "lxml")
    albums = []

    # We accept any <li> whose first child <a> has no href and whose first
    # nested <ul> contains href-bearing anchors. That filters out main nav
    # items (Designers / Photographers / Books / Contact Us) which DO have
    # hrefs on their own anchor.
    for li in soup.find_all("li"):
        direct_a = li.find("a", recursive=False)
        if not direct_a or direct_a.get("href"):
            continue
        label = direct_a.get_text(strip=True)
        if not label or len(label) > 40:
            continue

        child_ul = li.find("ul", recursive=False)
        if child_ul is None:
            continue

        children = []
        for ca in child_ul.find_all("a", href=True):
            href = ca["href"]
            if not href.startswith("/"):
                continue
            text = ca.get_text(strip=True)
            if not text:
                continue
            child_slug = href.rstrip("/").split("/")[-1]
            children.append({
                "child_slug": child_slug,
                "child_name": text,
                "child_url":  href,
            })

        if not children:
            continue

        # Classify album kind by sniffing the first child's URL.
        first_url = children[0]["child_url"]
        if first_url.startswith("/designers/"):
            kind = "designer_index"
        elif first_url.startswith("/photographers/"):
            kind = "photographer_index"
        else:
            kind = "tag_album"

        albums.append({
            "album_slug": _slugify_album_name(label),
            "album_name": label,
            "kind":       kind,
            "children":   children,
        })

    return albums


def parse_author_built_projects(html: str) -> dict:
    """Return project paths + pagination hint from an architect's /projects/built page.

    Used for the cheap discovery pass; richer per-project data is in
    `parse_author_built_projects_rich()`.
    """
    project_paths = sorted(set(re.findall(r'href="(/projects/\d+-[^"#?]+)"', html)))
    next_page_links = re.findall(r'href="([^"]*\bpage=\d+[^"]*)"', html)
    return {
        "project_paths":   project_paths,
        "has_next":        bool(next_page_links),
        "next_page_paths": sorted(set(next_page_links))[:5],
    }


def parse_author_built_projects_rich(html: str) -> list[dict]:
    """Extract per-project lite metadata from an architect's /projects/built page.

    Each project `<li>` exposes (without us having to fetch its individual
    project page):
        - canonical project_id + slug (URL)
        - name
        - architect_names (primary + co-architects, from img alt)
        - co_architects (separately surfaced via "With <X>")
        - location_city + location_country (note: this listing uses
          CITY - COUNTRY ordering, the OPPOSITE of project-page sidebars
          which use COUNTRY - CITY)
        - photographer (from "Photo by <X>" if present)

    `year`, `description`, `tags`, `area_sqm`, `credits`, `gallery_urls`,
    `cover_image_url` are NOT here — they require the deep project page fetch.
    """
    soup = BeautifulSoup(html, "lxml")
    projects = []
    seen_ids: set = set()

    for li in soup.find_all("li"):
        a = li.find("a", href=re.compile(r"^/projects/\d+-"))
        if not a:
            continue

        m = _PROJECT_ID_RE.search(a["href"])
        if not m:
            continue
        proj_id = int(m.group(1))
        slug = m.group(2)
        if proj_id in seen_ids:
            continue
        seen_ids.add(proj_id)

        # Pipe-separated text rendering: "NAME | CITY - COUNTRY [| With | CO] [| Photo by | PHOTOG]"
        parts = [p.strip() for p in li.get_text(" | ", strip=True).split("|")]
        parts = [p for p in parts if p]

        name = parts[0] if parts else None
        location_text = parts[1] if len(parts) > 1 else ""

        # When the name cell is empty, get_text() omits the missing node and
        # the "City - Country" location shifts into parts[0]; the real
        # location slot (parts[1]) is then absent or a non-location marker.
        # Drop the leaked location rather than store it as the building name.
        if name and " - " in name and " - " not in location_text:
            name = None

        co_architects: list[str] = []
        photographer: str | None = None
        i = 2
        while i < len(parts) - 1:
            if parts[i] == "With":
                # The next entry might be a comma-joined list (we'll keep raw)
                co_architects.append(parts[i + 1])
                i += 2
            elif parts[i] == "Photo by":
                photographer = parts[i + 1]
                i += 2
            else:
                i += 1

        # Split "City - Country" — last " - " wins so multi-word cities work
        location_city = location_country = None
        if " - " in location_text:
            city_part, country_part = location_text.rsplit(" - ", 1)
            location_city = city_part.strip() or None
            location_country = country_part.strip() or None

        # img alt: "Primary, [Co-arch], [Photographer] · Project Name"
        architect_names: list[str] = []
        img = a.find("img")
        if img and img.get("alt"):
            alt = img["alt"]
            if " · " in alt:
                names_chunk = alt.split(" · ")[0]
                names_list = [n.strip() for n in names_chunk.split(",") if n.strip()]
                # Drop the photographer (last name) if it matches the parent text
                if photographer:
                    names_list = [n for n in names_list if n != photographer]
                architect_names = names_list

        projects.append({
            "id":               proj_id,
            "slug":             slug,
            "name":             name,
            "architect_names":  architect_names,
            "co_architects":    co_architects,
            "location_city":    location_city,
            "location_country": location_country,
            "photographer":     photographer,
        })

    return projects
