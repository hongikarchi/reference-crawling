"""Architizer crawler (Phase 7) — sitemap-driven, public read.

Five phases:
  1. sitemap-projects   walk sitemap-projects.xml{?p=1..N} → pending_projects
  2. sitemap-firms      walk sitemap-firms.xml{?p=1..N}    → pending_firms
  3. projects           deep-fetch each pending project    → architizer_projects
  4. firms              deep-fetch each pending firm       → architizer_firms
  5. awards             walk winners.architizer.com        → architizer_awards

All phases resume-friendly via the pending_* status fields.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from core import config
from core.utils import logger, RateLimiter, create_session, fetch_page
from crawl.architizer import db as az_db
from crawl.architizer import parsers as az_parsers


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

_SESSION = None
_LIMITER = None


def _init() -> tuple:
    global _SESSION, _LIMITER
    if _SESSION is None:
        _SESSION = create_session()
        # Override the metalocus default UA with our browser-UA
        _SESSION.headers["User-Agent"] = config.ARCHITIZER_USER_AGENT
        _LIMITER = RateLimiter(config.ARCHITIZER_REQUEST_DELAY_SECONDS)
    return _SESSION, _LIMITER


def _fetch(path_or_url: str) -> Optional[str]:
    session, limiter = _init()
    if path_or_url.startswith("http"):
        url = path_or_url
    else:
        url = config.ARCHITIZER_BASE_URL + path_or_url
    return fetch_page(url, session, limiter)


# ---------------------------------------------------------------------------
# Phase 1 — sitemap projects
# ---------------------------------------------------------------------------

def phase_sitemap_projects(max_pages: int = 20) -> int:
    """Walk sitemap-projects.xml?p=1..N until empty or max_pages.
    INSERT OR IGNORE into pending_projects. Returns number of new rows."""
    base = f"{config.ARCHITIZER_BASE_URL}/sitemap-projects.xml"
    total_added = 0
    for p in range(1, max_pages + 1):
        url = f"{base}?p={p}"
        xml = _fetch(url)
        if not xml or "<urlset" not in xml:
            logger.info(f"  sitemap-projects p={p}: empty, stopping")
            break
        urls = az_parsers.parse_sitemap_urls(xml)
        if not urls:
            logger.info(f"  sitemap-projects p={p}: 0 urls, stopping")
            break
        rows = [(u["loc"], url, u.get("lastmod")) for u in urls]
        added = az_db.bulk_enqueue_projects(rows)
        total_added += added
        logger.info(f"  sitemap-projects p={p}: {len(urls)} urls, +{added} new")
    return total_added


# ---------------------------------------------------------------------------
# Phase 2 — sitemap firms
# ---------------------------------------------------------------------------

def phase_sitemap_firms(max_pages: int = 10) -> int:
    base = f"{config.ARCHITIZER_BASE_URL}/sitemap-firms.xml"
    total_added = 0
    for p in range(1, max_pages + 1):
        url = f"{base}?p={p}"
        xml = _fetch(url)
        if not xml or "<urlset" not in xml:
            logger.info(f"  sitemap-firms p={p}: empty, stopping")
            break
        urls = az_parsers.parse_sitemap_urls(xml)
        if not urls:
            logger.info(f"  sitemap-firms p={p}: 0 urls, stopping")
            break
        rows = [(u["loc"], url, u.get("lastmod")) for u in urls]
        added = az_db.bulk_enqueue_firms(rows)
        total_added += added
        logger.info(f"  sitemap-firms p={p}: {len(urls)} urls, +{added} new")
    return total_added


# ---------------------------------------------------------------------------
# Phase 3 — deep-fetch projects
# ---------------------------------------------------------------------------

def phase_projects(limit: Optional[int] = None) -> int:
    pending = az_db.get_pending("pending_projects", limit=limit)
    processed = 0
    for row in pending:
        url = row["url"]
        html = _fetch(url)
        if not html:
            az_db.mark_failed("pending_projects", "url", url, "fetch_failed")
            continue
        try:
            data = az_parsers.parse_project_page(html, url)
            if data.get("id") is None:
                az_db.mark_failed("pending_projects", "url", url, "no_pk_in_data_data")
                continue
            az_db.upsert_project(data)
            az_db.mark_done("pending_projects", "url", url)
            processed += 1
            logger.info(
                f"  project {data.get('name')!r} (pk={data['id']}) "
                f"firm={data.get('firm_slug')}"
            )
        except Exception as e:
            az_db.mark_failed("pending_projects", "url", url,
                              f"{type(e).__name__}: {e}")
            logger.error(f"  project parse error {url}: {e}")
    return processed


# ---------------------------------------------------------------------------
# Phase 4 — deep-fetch firms
# ---------------------------------------------------------------------------

def phase_firms(limit: Optional[int] = None) -> int:
    pending = az_db.get_pending("pending_firms", limit=limit)
    processed = 0
    for row in pending:
        url = row["url"]
        html = _fetch(url)
        if not html:
            az_db.mark_failed("pending_firms", "url", url, "fetch_failed")
            continue
        try:
            data = az_parsers.parse_firm_page(html, url)
            if not data.get("name"):
                az_db.mark_failed("pending_firms", "url", url, "no_name")
                continue
            az_db.upsert_firm(data)
            az_db.mark_done("pending_firms", "url", url)
            processed += 1
            logger.info(
                f"  firm {data['name']!r} (slug={data['slug']}) "
                f"projects_seen={data.get('project_count_seen')}"
            )
        except Exception as e:
            az_db.mark_failed("pending_firms", "url", url,
                              f"{type(e).__name__}: {e}")
            logger.error(f"  firm parse error {url}: {e}")
    return processed


# ---------------------------------------------------------------------------
# Phase 5 — A+Awards
# ---------------------------------------------------------------------------

AWARDS_TRACKS = ("Typology", "Firms", "Products", "Plus")
AWARDS_YEAR_RANGE = range(2013, 2026)  # 2013-2025 inclusive


def phase_awards(years: Optional[range] = None,
                 tracks: Optional[tuple] = None) -> int:
    """Walk winners.architizer.com/{year}/{track}/ for each (year, track)
    pair and insert award entries. Returns total entries inserted."""
    years = years or AWARDS_YEAR_RANGE
    tracks = tracks or AWARDS_TRACKS
    total = 0
    for y in years:
        for tr in tracks:
            url = f"{config.ARCHITIZER_AWARDS_BASE_URL}/{y}/{tr}/"
            html = _fetch(url)
            if not html:
                logger.info(f"  awards {y}/{tr}: fetch_failed (skip)")
                continue
            entries = az_parsers.parse_awards_track_page(
                html, url, award_year=y, award_track=tr,
            )
            for e in entries:
                az_db.insert_award(e)
            total += len(entries)
            logger.info(f"  awards {y}/{tr}: {len(entries)} entries")
    return total


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all(*, project_limit: int = 100000, firm_limit: int = 100000) -> None:
    """Pilot or full run depending on limits."""
    az_db.init_db()

    logger.info("=== Architizer crawl: phase 1 — sitemap-projects ===")
    n = phase_sitemap_projects()
    logger.info(f"  pending_projects newly added: {n}")

    logger.info("\n=== Architizer crawl: phase 2 — sitemap-firms ===")
    n = phase_sitemap_firms()
    logger.info(f"  pending_firms newly added: {n}")

    logger.info("\n=== Architizer crawl: phase 3 — projects ===")
    n = phase_projects(limit=project_limit)
    logger.info(f"  projects processed: {n}")

    logger.info("\n=== Architizer crawl: phase 4 — firms ===")
    n = phase_firms(limit=firm_limit)
    logger.info(f"  firms processed: {n}")

    logger.info("\n=== Architizer crawl: phase 5 — A+Awards ===")
    n = phase_awards()
    logger.info(f"  awards inserted: {n}")

    logger.info("\n=== Final stats ===")
    for k, v in az_db.stats().items():
        logger.info(f"  {k}: {v}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Architizer crawler (Phase 7)")
    parser.add_argument("--phase",
                        choices=["sitemap-projects", "sitemap-firms",
                                 "projects", "firms", "awards", "all"],
                        default="all")
    parser.add_argument("--limit", type=int, default=100,
                        help="Project / firm fetch limit per run (default 100)")
    args = parser.parse_args(argv)

    az_db.init_db()
    try:
        if args.phase == "sitemap-projects":
            n = phase_sitemap_projects()
            logger.info(f"new pending_projects: {n}")
        elif args.phase == "sitemap-firms":
            n = phase_sitemap_firms()
            logger.info(f"new pending_firms: {n}")
        elif args.phase == "projects":
            n = phase_projects(limit=args.limit)
            logger.info(f"projects processed: {n}")
        elif args.phase == "firms":
            n = phase_firms(limit=args.limit)
            logger.info(f"firms processed: {n}")
        elif args.phase == "awards":
            n = phase_awards()
            logger.info(f"awards inserted: {n}")
        else:  # all
            run_all(project_limit=args.limit, firm_limit=args.limit)
    except KeyboardInterrupt:
        logger.warning("interrupted by user")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
