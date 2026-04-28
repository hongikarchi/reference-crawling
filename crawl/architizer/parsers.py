"""HTML parsers for Architizer pages (Phase 7).

Selectors / JSON shape verified per `.claude/research/architizer-schema.md`.
All parsers tolerate missing fields — return None / empty rather than raise.

Public functions:
  parse_sitemap_index(xml)         -> list[dict]   {loc, lastmod}
  parse_sitemap_urls(xml)          -> list[dict]   {loc, lastmod}
  parse_project_page(html, url)    -> dict
  parse_firm_page(html, url)       -> dict
  parse_awards_track_page(html, url) -> list[dict] award entries

Project parsing leans on the `data-data='{...}'` JSON blob embedded on
project pages (see schema doc TL;DR). Per-selector fallbacks fill in the
fields that JSON doesn't carry (location, OG meta, image gallery).
"""

from __future__ import annotations

import html as html_module
import json
import re
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup


_PROJECT_SLUG_RE = re.compile(r"/projects/([^/?#]+)/?")
_FIRM_SLUG_RE    = re.compile(r"/firms/([^/?#]+)/?")
# data-data='{...}' — the pk/name/description/etc JSON blob on every project page
_DATA_DATA_RE    = re.compile(r"data-data='(\{[^']{200,30000})'", re.DOTALL)
# YYYY from ISO date '2024-01-01T00:00:00'
_ISO_YEAR_RE     = re.compile(r"^(\d{4})")


# ---------------------------------------------------------------------------
# Sitemap parsers (XML, not HTML)
# ---------------------------------------------------------------------------

def parse_sitemap_index(xml: str) -> list[dict]:
    """Parse a sitemap-index XML (e.g. /sitemap.xml). Returns
    [{'loc': url, 'lastmod': iso_str_or_None}, ...] for each child sitemap."""
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
    """Parse a leaf sitemap (e.g. sitemap-projects.xml?p=1). Returns
    [{'loc': url, 'lastmod': iso_str_or_None}, ...] for each <url>."""
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


def _meta_all(soup: BeautifulSoup, prop: str) -> list[str]:
    return [t.get("content") for t in soup.find_all("meta", attrs={"property": prop})
            if t.get("content")]


def _slug_from_project_url(url: str) -> Optional[str]:
    m = _PROJECT_SLUG_RE.search(url)
    return m.group(1) if m else None


def _slug_from_firm_url(url: str) -> Optional[str]:
    m = _FIRM_SLUG_RE.search(url)
    return m.group(1) if m else None


def _split_location(s: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'New York, NY, United States' → ('United States', 'New York').
    Falls back to (None, None) on unfamiliar shapes."""
    if not s:
        return None, None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return None, None
    country = parts[-1] if len(parts) >= 1 else None
    city = parts[0] if len(parts) >= 2 else None
    return country, city


def _year_from_iso(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = _ISO_YEAR_RE.match(s.strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Project page
# ---------------------------------------------------------------------------

def parse_project_page(html: str, url: str) -> dict:
    """Parse one /projects/{slug}/ page. The big win: the `data-data`
    JSON blob carries pk/name/description/completion_date/etc. Per-selector
    fallbacks fill in OG meta + location header.

    Returns a dict ready for db.upsert_project() (excluding `fetched_at`).
    """
    out: dict = {"slug": _slug_from_project_url(url)}
    if not out["slug"]:
        # Try url path even if regex missed
        out["slug"] = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

    soup = BeautifulSoup(html, "html.parser")

    # ---- data-data JSON blob ---------------------------------------------
    m = _DATA_DATA_RE.search(html)
    if m:
        try:
            payload = json.loads(html_module.unescape(m.group(1)))
            out["id"]                    = payload.get("pk")
            out["global_id"]             = payload.get("global_id")
            # name from JSON; OG fallback below
            out["name"]                  = payload.get("name") or out.get("name")
            out["description"]           = payload.get("description")
            out["completion_year"]       = _year_from_iso(payload.get("completion_date"))
            out["building_size_slug"]    = payload.get("building_size")
            out["building_size_display"] = payload.get("size")
            out["constr_status"]         = payload.get("constr_status")
            budget = payload.get("budget")
            out["budget"] = float(budget) if budget not in (None, "") else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # ---- OG / meta fallbacks ----------------------------------------------
    if not out.get("name"):
        out["name"] = _meta(soup, "og:title")
    out["description_short"] = _meta(soup, "og:description")
    out["cover_image_url"]   = _meta(soup, "og:image")
    out["gallery_image_urls"] = _meta_all(soup, "og:image")  # list (often >=cover)
    out["categories"]        = _meta_all(soup, "article:tag")
    out["published_time"]    = _meta(soup, "article:published_time")
    out["modified_time"]     = _meta(soup, "article:modified_time")

    # Firm — '/firms/{slug}/' from article:author meta
    firm_url = _meta(soup, "article:author")
    if firm_url:
        out["firm_slug"] = _slug_from_firm_url(firm_url)
    # Firm display name from <title>: "Project Name by Firm Name | Architizer"
    title = soup.title.get_text(strip=True) if soup.title else ""
    if " by " in title:
        firm_name_part = title.split(" by ", 1)[1]
        firm_name_part = firm_name_part.split(" | ", 1)[0]
        out["firm_name"] = firm_name_part.strip() or None

    # Location — first <h2> in project header, e.g. "New York, NY, United States"
    h2 = soup.select_one("h2")
    if h2:
        loc = h2.get_text(strip=True)
        # Avoid grabbing unrelated h2s — only treat as location if it has commas
        if "," in loc:
            out["location_full"] = loc
            country, city = _split_location(loc)
            out["location_country"] = country
            out["location_city"] = city

    # Image global ids from data-globalid attrs on media items
    out["image_global_ids"] = [el.get("data-globalid")
                               for el in soup.select("[data-globalid^='media.mediaitemattribution']")
                               if el.get("data-globalid")]

    return out


# ---------------------------------------------------------------------------
# Firm page
# ---------------------------------------------------------------------------

def parse_firm_page(html: str, url: str) -> dict:
    """Parse one /firms/{slug}/ page.

    Returns a dict ready for db.upsert_firm() (excluding `fetched_at`).
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {
        "slug": _slug_from_firm_url(url) or
                urlparse(url).path.rstrip("/").rsplit("/", 1)[-1],
        "name": (soup.find("h1") or {}).get_text(strip=True) if soup.find("h1") else None,
    }

    # OG fallback for name
    if not out["name"]:
        out["name"] = _meta(soup, "og:title")

    # Description: og:description or About section
    out["description"] = _meta(soup, "og:description")

    # Office locations (sidebar) — collect any "City, ST" / "City, Country" looking spans
    # The recon doc didn't pin a stable selector; we grab the contact-block items.
    office_locations: list[str] = []
    for tag in soup.select(".firm-locations li, .firm-contact .location, .firm-info .location"):
        txt = tag.get_text(strip=True)
        if txt and "," in txt and len(txt) < 80:
            office_locations.append(txt)
    out["office_locations"] = office_locations

    # Awards summary badge — heuristic: span/div containing "Winner (" / "Finalist (" /
    # "Special Mention ("
    awards_text = None
    for tag in soup.find_all(["span", "div", "p"]):
        t = tag.get_text(" ", strip=True)
        if t and ("Winner (" in t or "Finalist (" in t or "Special Mention (" in t):
            if len(t) < 300:  # avoid pulling whole pages
                awards_text = t
                break
    out["awards_summary"] = awards_text

    # Project thumbnails — count anchors to /projects/{slug}/
    out["project_count_seen"] = len({a.get("href") for a in soup.find_all("a", href=True)
                                     if a.get("href", "").startswith("/projects/")})

    # Social links — collect outbound href attrs on social icon anchors
    social: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(domain in href for domain in
               ("facebook.com", "twitter.com", "instagram.com", "linkedin.com",
                "youtube.com", "vimeo.com")):
            host = urlparse(href).netloc.replace("www.", "").split(".")[0]
            social.setdefault(host, href)
    out["social_links"] = social

    return out


# ---------------------------------------------------------------------------
# A+Awards parsers
# ---------------------------------------------------------------------------

# A+Awards landing pages (winners.architizer.com/{year}/{Track}/) carry
# winner cards. Each card has: project link OR firm link, tier label,
# category breadcrumb. Selectors are recon-derived heuristics — verify on
# first crawl.

_AWARDS_TIER_RE = re.compile(
    r"\b(Jury Winner|Popular Choice Winner|Popular Winner|Finalist|Special Mention)\b",
    re.IGNORECASE,
)

_AWARDS_NORM_TIER = {
    "jury winner":            "Jury",
    "popular choice winner":  "Popular",
    "popular winner":         "Popular",
    "finalist":               "Finalist",
    "special mention":        "Special Mention",
}


def _normalize_tier(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    return _AWARDS_NORM_TIER.get(label.strip().lower())


def parse_awards_track_page(html: str, url: str,
                            award_year: int, award_track: str) -> list[dict]:
    """Extract award entries from a winners.architizer.com page.

    Returns a list of dicts ready for db.insert_award():
      {award_year, award_track, award_category, award_tier,
       project_slug, firm_slug, source_url}
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []

    # Strategy: each "winner" card is an anchor wrapping or near a tier label.
    # Walk every /projects/ and /firms/ anchor; lift the surrounding card's
    # text to detect tier + category.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        proj_slug = firm_slug = None
        if "/projects/" in href:
            proj_slug = _slug_from_project_url(href)
        elif "/firms/" in href or "architizer.com/firms/" in href:
            firm_slug = _slug_from_firm_url(href)
        if not proj_slug and not firm_slug:
            continue

        # Look up to find a card-like ancestor with tier text
        tier = None
        category = None
        ancestor = a
        for _ in range(5):
            ancestor = ancestor.parent
            if ancestor is None:
                break
            txt = ancestor.get_text(" ", strip=True)
            if not tier:
                m = _AWARDS_TIER_RE.search(txt)
                if m:
                    tier = _normalize_tier(m.group(1))
            # Category: look for breadcrumb-style ' > ' inside same ancestor
            if not category and " > " in txt and len(txt) < 200:
                category = txt
            if tier and category:
                break

        if not tier:
            continue  # not a winner card

        out.append({
            "award_year":    award_year,
            "award_track":   award_track,
            "award_category": category,
            "award_tier":    tier,
            "project_slug":  proj_slug,
            "firm_slug":     firm_slug,
            "source_url":    url,
        })

    # Dedupe by (project_slug, firm_slug, tier, category) — same card may be
    # touched by multiple anchors
    seen = set()
    deduped = []
    for item in out:
        key = (item["project_slug"], item["firm_slug"],
               item["award_tier"], item["award_category"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
