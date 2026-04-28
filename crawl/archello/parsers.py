"""HTML parsers for Archello pages (Phase 8).

Selectors verified per `.claude/research/archello-schema.md`. All parsers
tolerate missing fields — return None / empty rather than raising.

Public functions:
  parse_sitemap_index(xml)         -> list[dict]   {loc, lastmod}
  parse_sitemap_urls(xml)          -> list[dict]   {loc, lastmod}
  parse_project_page(html, url)    -> dict
"""

from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup


_PROJECT_SLUG_RE = re.compile(r"/project/([^/?#]+)/?")
_BRAND_SLUG_RE   = re.compile(r"/brand/([^/?#]+)/?")
_PRODUCT_SLUG_RE = re.compile(r"/product/([^/?#]+)/?")
_AREA_M2_RE      = re.compile(r"(\d+(?:[.,]\d+)?)\s*m2", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Sitemap parsers (XML)
# ---------------------------------------------------------------------------

def parse_sitemap_index(xml: str) -> list[dict]:
    """Parse /sitemaps/index.xml. Returns [{'loc', 'lastmod'}, ...]."""
    soup = BeautifulSoup(xml, "xml")
    out = []
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        lastmod = sm.find("lastmod")
        if loc and loc.text:
            out.append({"loc": loc.text.strip(),
                        "lastmod": lastmod.text.strip() if lastmod else None})
    return out


def parse_sitemap_urls(xml: str) -> list[dict]:
    """Parse a leaf sitemap shard (e.g. projects.1.xml)."""
    soup = BeautifulSoup(xml, "xml")
    out = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        lastmod = u.find("lastmod")
        if loc and loc.text:
            out.append({"loc": loc.text.strip(),
                        "lastmod": lastmod.text.strip() if lastmod else None})
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta(soup: BeautifulSoup, prop: str) -> Optional[str]:
    tag = soup.find("meta", attrs={"property": prop}) or \
          soup.find("meta", attrs={"name": prop})
    return tag.get("content") if tag else None


def _slug_from_project_url(url: str) -> Optional[str]:
    m = _PROJECT_SLUG_RE.search(url)
    return m.group(1) if m else None


def _slug_from_brand_url(url: str) -> Optional[str]:
    m = _BRAND_SLUG_RE.search(url)
    return m.group(1) if m else None


def _slug_from_product_url(url: str) -> Optional[str]:
    m = _PRODUCT_SLUG_RE.search(url)
    return m.group(1) if m else None


def _split_location(s: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'Jingdezhen, Jiangxi, China | View Map' → ('China', 'Jingdezhen')."""
    if not s:
        return None, None
    # Strip trailing '| View Map' or similar UI suffixes
    s = s.split("|", 1)[0].strip()
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return None, None
    country = parts[-1]
    city = parts[0] if len(parts) >= 2 else None
    return country, city


def _parse_area_m2(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    m = _AREA_M2_RE.search(s)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _data_key_ids(item) -> dict:
    """Extract {brand_id, project_id} from <div data-key='...'> JSON.
    Returns empty dict if missing/malformed."""
    raw = item.get("data-key")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Project page
# ---------------------------------------------------------------------------

def parse_project_page(html: str, url: str) -> dict:
    """Parse one /project/{slug} page.

    Returns dict shaped for db.upsert_project() PLUS a 'details' list of
    dicts shaped for db.replace_project_details(). The crawler invokes
    both sequentially.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {
        "slug": _slug_from_project_url(url) or
                urlparse(url).path.rstrip("/").rsplit("/", 1)[-1],
    }

    # Hero
    hero_title = soup.select_one("h2.ah-project-hero__title")
    out["name"] = hero_title.get_text(strip=True) if hero_title else _meta(soup, "og:title")
    out["cover_image_url"] = _meta(soup, "og:image")

    # Architect name from <title> middle segment: "Project | Architect | Archello"
    title = soup.title.get_text(strip=True) if soup.title else ""
    if "|" in title:
        parts = [p.strip() for p in title.split("|")]
        if len(parts) >= 3:
            out["architect_name"] = parts[1] or None

    # Sidebar — General data dt/dd pairs
    for dt in soup.select("dl#grid-product-detail-general dt"):
        key = dt.get_text(strip=True).lower().replace(" ", "_")
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        val = dd.get_text(" ", strip=True)
        if not val:
            continue
        if key == "location":
            out["location_full"] = val
            country, city = _split_location(val)
            out["location_country"] = country
            out["location_city"] = city
        elif key == "project_year":
            try:
                out["project_year"] = int(re.sub(r"\D", "", val)[:4])
            except ValueError:
                pass
        elif key == "category":
            out["category"] = val
        elif key in ("building_area", "site_area"):
            # Prefer building_area; fall back to site_area only if building_area missing
            if key == "building_area" or out.get("building_area_m2") is None:
                out["building_area_m2"] = _parse_area_m2(val)

    # Body / description
    body = soup.select_one("div.ah-project-story__body")
    if body:
        out["description"] = body.get_text("\n", strip=True)

    # Gallery — collect <img src> inside the story gallery
    gallery_imgs = []
    for img in soup.select("div.ah-project-story__gallery img"):
        src = img.get("src") or img.get("data-src")
        if src:
            gallery_imgs.append(src)
    out["gallery_image_urls"] = gallery_imgs

    # Detail items — the BIM-source-list structure.
    # Skip empty separator divs (no role title AND no text content).
    details: list[dict] = []
    for item in soup.select("div.ah-project-details__item"):
        title_tag = item.select_one(".ah-project-details__item-title")
        text_block = item.select_one(".ah-project-details__item-text")
        title_txt = title_tag.get_text(strip=True) if title_tag else ""
        text_txt  = text_block.get_text(strip=True) if text_block else ""
        if not title_txt and not text_txt:
            continue   # empty UI separator
        ids = _data_key_ids(item)
        product_links = []
        brand_links = []
        if text_block:
            for a in text_block.select("a[href]"):
                href = a.get("href", "")
                txt = a.get_text(strip=True)
                if href.startswith("/product/"):
                    product_links.append((href, txt))
                elif href.startswith("/brand/"):
                    brand_links.append((href, txt))
        # Take the first brand link as the primary brand for this detail row
        primary_brand_slug = primary_brand_name = None
        if brand_links:
            first_href, first_name = brand_links[0]
            primary_brand_slug = _slug_from_brand_url(first_href)
            primary_brand_name = first_name or None
        details.append({
            "brand_id":         ids.get("brand_id"),
            "project_id":       ids.get("project_id"),
            "role_or_category": title_tag.get_text(strip=True) if title_tag else None,
            "brand_slug":       primary_brand_slug,
            "brand_name":       primary_brand_name,
            "product_slugs":    [_slug_from_product_url(href) for href, _ in product_links],
            "product_names":    [name for _, name in product_links],
            "_extra_brands":    [(s, n) for href, n in brand_links[1:]
                                 for s in [_slug_from_brand_url(href)] if s],
        })

    out["details"] = details

    # Project ID + primary architect: lift from the first detail row that
    # has a non-NULL project_id (every row's data-key carries the same id).
    project_id = next((d["project_id"] for d in details
                       if d.get("project_id") is not None), None)
    out["id"] = project_id

    # Heuristic for primary architect: first detail row whose role contains
    # 'architect' (case-insensitive); if none, use the first detail row.
    arch_row = next((d for d in details
                     if d.get("role_or_category")
                     and "architect" in d["role_or_category"].lower()), None)
    if arch_row is None and details:
        arch_row = details[0]
    if arch_row:
        out["architect_brand_id"] = arch_row.get("brand_id")
        # Prefer the brand_name from the row over the title-tag scrape
        if arch_row.get("brand_name"):
            out["architect_name"] = arch_row["brand_name"]

    return out
