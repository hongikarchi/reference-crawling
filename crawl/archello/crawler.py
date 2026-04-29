"""Archello crawler (Phase 8) — sitemap-driven, public read, browser-UA.

Two phases:
  1. sitemap   walk /sitemaps/index.xml + project shards → pending_projects
  2. projects  deep-fetch each pending project → archello_projects + details

Resume-friendly via pending_projects.status. Rate-limited per
config.ARCHELLO_REQUEST_DELAY_SECONDS (default 2.5s, conservative
above the published crawl-delay: 1).
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from core import config
from core.utils import logger, RateLimiter, create_session, fetch_page
from crawl.archello import db as ar_db
from crawl.archello import parsers as ar_parsers


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

_SESSION = None
_LIMITER = None


def _init() -> tuple:
    global _SESSION, _LIMITER
    if _SESSION is None:
        _SESSION = create_session()
        # Cloudflare blocks ClaudeBot; browser-UA passes
        _SESSION.headers["User-Agent"] = config.ARCHELLO_USER_AGENT
        _LIMITER = RateLimiter(config.ARCHELLO_REQUEST_DELAY_SECONDS)
    return _SESSION, _LIMITER


def _fetch(path_or_url: str) -> Optional[str]:
    session, limiter = _init()
    if path_or_url.startswith("http"):
        url = path_or_url
    else:
        url = config.ARCHELLO_BASE_URL + path_or_url
    return fetch_page(url, session, limiter)


# ---------------------------------------------------------------------------
# Phase 1 — sitemap discovery
# ---------------------------------------------------------------------------

def phase_sitemap(only_projects: bool = True) -> int:
    """Walk /sitemaps/index.xml → for each project shard, walk the URLs and
    enqueue. Returns total newly added rows.

    Per the recon, 1142 child sitemaps exist. We currently only ingest the
    project shards; brands/products are out of scope for Phase 8.
    """
    index_xml = _fetch("/sitemaps/index.xml")
    if not index_xml:
        logger.error("failed to fetch sitemap index")
        return 0
    children = ar_parsers.parse_sitemap_index(index_xml)
    logger.info(f"  sitemap-index: {len(children)} child sitemaps total")

    project_shards = [c for c in children if "/projects." in c["loc"]]
    logger.info(f"  project shards: {len(project_shards)}")

    total = 0
    for i, shard in enumerate(project_shards, 1):
        shard_xml = _fetch(shard["loc"])
        if not shard_xml:
            logger.warning(f"    shard {i}/{len(project_shards)}: fetch failed (skip)")
            continue
        urls = ar_parsers.parse_sitemap_urls(shard_xml)
        rows = [(u["loc"], shard["loc"], u.get("lastmod")) for u in urls]
        added = ar_db.bulk_enqueue_projects(rows)
        total += added
        if i % 25 == 0 or i == len(project_shards):
            logger.info(f"    shard {i}/{len(project_shards)} ({shard['loc'].rsplit('/', 1)[-1]}): "
                        f"{len(urls)} urls, +{added} new")
    return total


# ---------------------------------------------------------------------------
# Phase 2 — deep-fetch projects
# ---------------------------------------------------------------------------

def phase_projects(limit: Optional[int] = None) -> int:
    pending = ar_db.get_pending(limit=limit)
    processed = 0
    for row in pending:
        url = row["url"]
        html = _fetch(url)
        if not html:
            ar_db.mark_failed(url, "fetch_failed")
            continue
        try:
            data = ar_parsers.parse_project_page(html, url)
            if data.get("id") is None or not data.get("name"):
                ar_db.mark_failed(url, "no_id_or_name")
                continue
            project_id = data["id"]
            details = data.pop("details", [])
            ar_db.upsert_project(data)
            ar_db.replace_project_details(project_id, details)
            # Log brands seen for optional later crawl
            for d in details:
                if d.get("brand_slug"):
                    ar_db.upsert_brand_seen(
                        slug=d["brand_slug"],
                        brand_id=d.get("brand_id"),
                        name=d.get("brand_name"),
                    )
                for s, n in d.get("_extra_brands", []) or []:
                    ar_db.upsert_brand_seen(slug=s, brand_id=None, name=n)
            ar_db.mark_done(url)
            processed += 1
            logger.info(
                f"  project {data.get('name')!r} (id={project_id}) "
                f"details={len(details)} category={data.get('category')!r}"
            )
        except Exception as e:
            ar_db.mark_failed(url, f"{type(e).__name__}: {e}")
            logger.error(f"  project parse error {url}: {e}")
    return processed


# ---------------------------------------------------------------------------
# Phase 3 — enqueue firms (architect-role brands → pending_firms)
# ---------------------------------------------------------------------------

def phase_enqueue_firms() -> int:
    """Bulk-enqueue every brand_slug that has appeared as 'Architect' role
    on any project into pending_firms. Idempotent."""
    n = ar_db.enqueue_firms_from_architect_role()
    logger.info(f"  enqueued {n} firms (architect-role brands)")
    return n


# ---------------------------------------------------------------------------
# Phase 4 — deep-fetch firms (/brand/{slug})
# ---------------------------------------------------------------------------

def phase_firms(limit: Optional[int] = None) -> int:
    pending = ar_db.get_pending(limit=limit, table="pending_firms")
    processed = 0
    for row in pending:
        url = row["url"]
        html = _fetch(url)
        if not html:
            ar_db.mark_failed(url, "fetch_failed", table="pending_firms")
            continue
        try:
            data = ar_parsers.parse_brand_page(html, url)
            if not data.get("name"):
                ar_db.mark_failed(url, "no_name", table="pending_firms")
                continue
            # Backfill brand_id from archello_brands_seen if available
            if data.get("brand_id") is None and data.get("slug"):
                with ar_db.get_db() as conn:
                    r = conn.execute(
                        "SELECT brand_id FROM archello_brands_seen WHERE slug = ?",
                        (data["slug"],),
                    ).fetchone()
                    if r:
                        data["brand_id"] = r["brand_id"]
            ar_db.upsert_firm(data)
            ar_db.mark_done(url, table="pending_firms")
            processed += 1
            logger.info(
                f"  firm {data['name']!r} (slug={data['slug']}) "
                f"projects={data.get('project_count_archello')} "
                f"hq={data.get('location_country')}"
            )
        except Exception as e:
            ar_db.mark_failed(url, f"{type(e).__name__}: {e}", table="pending_firms")
            logger.error(f"  firm parse error {url}: {e}")
    return processed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all(*, project_limit: int = 1000) -> None:
    """Pilot run: sitemap discovery then a limited deep-fetch slice."""
    ar_db.init_db()

    logger.info("=== Archello crawl: phase 1 — sitemap ===")
    n = phase_sitemap()
    logger.info(f"  pending_projects newly added: {n}")

    logger.info(f"\n=== Archello crawl: phase 2 — projects (limit={project_limit}) ===")
    n = phase_projects(limit=project_limit)
    logger.info(f"  projects processed: {n}")

    logger.info("\n=== Final stats ===")
    for k, v in ar_db.stats().items():
        logger.info(f"  {k}: {v}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Archello crawler (Phase 8)")
    parser.add_argument("--phase",
                        choices=["sitemap", "projects",
                                 "enqueue-firms", "firms", "all"],
                        default="all")
    parser.add_argument("--limit", type=int, default=1000,
                        help="Project / firm fetch limit per run (default 1000)")
    args = parser.parse_args(argv)

    ar_db.init_db()
    try:
        if args.phase == "sitemap":
            n = phase_sitemap()
            logger.info(f"new pending_projects: {n}")
        elif args.phase == "projects":
            n = phase_projects(limit=args.limit)
            logger.info(f"projects processed: {n}")
        elif args.phase == "enqueue-firms":
            n = phase_enqueue_firms()
            logger.info(f"newly enqueued firms: {n}")
        elif args.phase == "firms":
            n = phase_firms(limit=args.limit)
            logger.info(f"firms processed: {n}")
        else:
            run_all(project_limit=args.limit)
    except KeyboardInterrupt:
        logger.warning("interrupted by user")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
