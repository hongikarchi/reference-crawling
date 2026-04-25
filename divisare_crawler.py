#!/usr/bin/env python3
"""4-phase Divisare crawler (Phase 1).

Mirror of metalocus's `crawler.py` pattern, but for Divisare's authenticated
API surface and entity model.

Phases:
  1. discover    — walk /designers/{region} pages, harvest /authors/{id} URLs
                   into pending_architects
  2. architects  — for each pending architect: fetch their page (save canonical
                   entity) + walk /projects/built (paginated) (queue projects)
  3. projects    — for each pending project: fetch + parse + save canonical
                   entity. Side-effect: queue every tag slug seen.
  4. tags        — for each pending tag: fetch + parse + save canonical entity.

Each phase is idempotent (UNIQUE-keyed pending tables; upsert on entity tables)
and crash-safe (per-row commit, mark_done after success).

Usage:
    python3 divisare_crawler.py --phase discover
    python3 divisare_crawler.py --phase architects --limit 20
    python3 divisare_crawler.py --phase projects --limit 100
    python3 divisare_crawler.py --phase tags --limit 200
    python3 divisare_crawler.py --phase all --limit 50

Or via the unified CLI:
    python3 run.py crawl-divisare [--limit N] [--phase ...]
"""

from __future__ import annotations

import argparse
import sys
import time

import config
import divisare_db
import divisare_parsers
from divisare_auth import get_authenticated_session
from utils import RateLimiter, logger


_SESSION = None
_RATE_LIMITER: RateLimiter | None = None


def _init() -> tuple:
    global _SESSION, _RATE_LIMITER
    if _SESSION is None:
        _SESSION = get_authenticated_session()
        _RATE_LIMITER = RateLimiter(config.DIVISARE_REQUEST_DELAY_SECONDS)
    return _SESSION, _RATE_LIMITER


def _looks_like_login_wall(response) -> bool:
    """Detect when Divisare bounced us to the login page (session expired).
    Two ways this manifests:
      1. Final URL ends at /login (allow_redirects=True followed a 302).
      2. Response is a redirect (3xx) with Location pointing at /login or
         /people/login (when allow_redirects=False — not our default).
      3. Status 200 but TooManyRedirects could already have raised.
    """
    if response is None:
        return False
    final = (getattr(response, "url", "") or "").lower()
    if "/login" in final or "/people/login" in final:
        return True
    return False


def _refresh_session() -> bool:
    """Re-login programmatically and rebuild the in-memory Session.
    Returns True on success."""
    import os as _os
    from divisare_auth import do_login
    email = _os.environ.get("DIVISARE_EMAIL")
    pw    = _os.environ.get("DIVISARE_PASSWORD")
    if not (email and pw):
        logger.error("auto-relogin: DIVISARE_EMAIL/PASSWORD not set in env")
        return False

    logger.warning("auto-relogin: refreshing Divisare session…")
    if not do_login(email, pw, verbose=False):
        logger.error("auto-relogin: do_login() returned False")
        return False

    # Reload cookies into our in-memory Session.
    global _SESSION
    _SESSION = get_authenticated_session()
    logger.warning("auto-relogin: session refreshed OK")
    return True


def _fetch(path_or_url: str, *, _relogin_attempted: bool = False) -> str | None:
    """Fetch one URL, applying rate-limit + retry + auto-relogin.
    Returns text or None."""
    session, rl = _init()
    rl.wait()
    url = path_or_url if path_or_url.startswith("http") else (config.DIVISARE_BASE_URL + path_or_url)

    for attempt in range(1, 4):
        try:
            r = session.get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
        except Exception as e:
            # TooManyRedirects on stale session is a common manifestation.
            looks_auth = "TooManyRedirects" in type(e).__name__
            if looks_auth and not _relogin_attempted:
                if _refresh_session():
                    return _fetch(path_or_url, _relogin_attempted=True)
            logger.warning(f"  fetch error {url}: {type(e).__name__}: {e} (attempt {attempt})")
            time.sleep(min(2 ** attempt, 30))
            continue

        # Auth wall detection: 200 but final URL is the login page.
        if r.status_code == 200 and _looks_like_login_wall(r):
            if not _relogin_attempted and _refresh_session():
                return _fetch(path_or_url, _relogin_attempted=True)
            logger.error(f"  auth wall on {url} after re-login attempt — giving up")
            return None

        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            logger.warning(f"  404 {url}")
            return None
        if r.status_code in (401, 403):
            if not _relogin_attempted and _refresh_session():
                return _fetch(path_or_url, _relogin_attempted=True)
            raise RuntimeError(
                f"Auth failed ({r.status_code}) on {url} after re-login attempt"
            )
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "60"))
            logger.warning(f"  rate-limited on {url}, sleeping {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            backoff = min(2 ** attempt, 30)
            logger.warning(f"  HTTP {r.status_code} on {url}, retrying in {backoff}s")
            time.sleep(backoff)
            continue
        logger.warning(f"  unexpected HTTP {r.status_code} on {url}")
        return None

    logger.error(f"  giving up on {url} after retries")
    return None


# ---------------------------------------------------------------------------
# Phase 1 — discover
# ---------------------------------------------------------------------------

def phase_discover(max_pages_per_region: int | None = None) -> int:
    """Walk /designers/{region} pages → harvest /authors/{id}-{slug} URLs.

    By default walks ALL paginated pages of every region. Use
    `max_pages_per_region` to cap (1 = pilot mode).
    """
    total_new = 0
    for region in divisare_parsers.DIVISARE_TOP_REGIONS:
        region_path = f"/designers/{region}"
        page_num = 1
        max_page = 1
        while page_num <= max_page:
            path = f"{region_path}?page={page_num}" if page_num > 1 else region_path
            html = _fetch(path)
            if not html:
                break
            parsed = divisare_parsers.parse_designers_region_pages(html)
            new_this_page = 0
            for author_path in parsed["author_paths"]:
                if divisare_db.enqueue_architect(author_path):
                    new_this_page += 1
            total_new += new_this_page
            if page_num == 1:
                max_page = parsed["max_page"]
                if max_pages_per_region:
                    max_page = min(max_page, max_pages_per_region)
            if page_num % 10 == 0 or page_num == max_page:
                logger.info(
                    f"  region={region} page={page_num}/{max_page} "
                    f"authors_seen={len(parsed['author_paths'])} new+={new_this_page} "
                    f"(running total this region: pages walked={page_num})"
                )
            page_num += 1
        logger.info(f"  region={region} done: {max_page} pages walked")
    return total_new


# ---------------------------------------------------------------------------
# Phase 2 — architects
# ---------------------------------------------------------------------------

def phase_architects(limit: int | None = None,
                     max_built_pages: int = 50,
                     also_unbuilt: bool = True) -> int:
    """For each pending architect: fetch architect page + walk /projects/built
    (and optionally /projects/unbuilt). Each project's lite metadata
    (name, architects, location, photographer) is upserted DIRECTLY into
    divisare_projects — no individual project-page fetch required.

    Use `phase_projects()` (= deferred deep fetch) only for projects whose
    full description / credits / area / gallery you specifically want.
    """
    pending = divisare_db.get_pending("pending_architects", limit=limit)
    processed = 0
    for row in pending:
        url = row["url"]
        html = _fetch(url)
        if not html:
            divisare_db.mark_failed("pending_architects", "url", url, "fetch_failed")
            continue
        try:
            data = divisare_parsers.parse_architect_page(html, url)
            if data["id"] is None:
                divisare_db.mark_failed("pending_architects", "url", url, "no_id_in_url")
                continue
            divisare_db.upsert_architect(data)
            primary_arch_id = data["id"]

            lite_total = 0
            for subpath in (("projects/built",) + (("projects/unbuilt",) if also_unbuilt else ())):
                base = f"{url}/{subpath}"
                for page_num in range(1, max_built_pages + 1):
                    paged = f"{base}?page={page_num}" if page_num > 1 else base
                    bhtml = _fetch(paged)
                    if not bhtml:
                        break
                    rich = divisare_parsers.parse_author_built_projects_rich(bhtml)
                    if not rich:
                        break
                    for proj in rich:
                        try:
                            divisare_db.upsert_project_lite(proj, primary_arch_id)
                            lite_total += 1
                        except Exception as e:
                            logger.warning(f"    upsert_project_lite failed for "
                                           f"id={proj.get('id')}: {e}")
                    # Pagination signal: if no rich projects on this page, stop
                    bparsed = divisare_parsers.parse_author_built_projects(bhtml)
                    if not bparsed["has_next"]:
                        break

            divisare_db.mark_done("pending_architects", "url", url)
            processed += 1
            logger.info(f"  architect {data['name']!r} (id={data['id']}) "
                        f"lite_projects_upserted+={lite_total}")
        except RuntimeError:
            raise  # auth errors bubble up
        except Exception as e:
            divisare_db.mark_failed("pending_architects", "url", url, f"{type(e).__name__}: {e}")
            logger.error(f"  architect parse error {url}: {e}")
    return processed


# ---------------------------------------------------------------------------
# Phase 3 — projects
# ---------------------------------------------------------------------------

def phase_projects(limit: int | None = None) -> int:
    pending = divisare_db.get_pending("pending_projects", limit=limit)
    processed = 0
    for row in pending:
        url = row["url"]
        html = _fetch(url)
        if not html:
            divisare_db.mark_failed("pending_projects", "url", url, "fetch_failed")
            continue
        try:
            full_url = url if url.startswith("http") else (config.DIVISARE_BASE_URL + url)
            data = divisare_parsers.parse_project_page(html, full_url)
            if data["id"] is None:
                divisare_db.mark_failed("pending_projects", "url", url, "no_id_in_url")
                continue
            divisare_db.upsert_project(data)
            for tag_slug in (data.get("tag_slugs") or []):
                divisare_db.enqueue_tag(tag_slug)
            divisare_db.mark_done("pending_projects", "url", url)
            processed += 1
            logger.info(
                f"  project {data['name']!r} (id={data['id']}) "
                f"tags+={len(data.get('tag_slugs') or [])}"
            )
        except RuntimeError:
            raise
        except Exception as e:
            divisare_db.mark_failed("pending_projects", "url", url, f"{type(e).__name__}: {e}")
            logger.error(f"  project parse error {url}: {e}")
    return processed


# ---------------------------------------------------------------------------
# Phase 5 — albums (homepage mega-menu taxonomy)
# ---------------------------------------------------------------------------

def phase_albums() -> int:
    """Fetch the homepage and parse the 14 top-level browsing albums
    (Cities / Houses / Ideas / Materiality / Plans & Details / Private
    Interiors / Public Interiors / Topics / Types / Elements / designers
    by Country / designers by City / photographers by Country/City).

    Saves album metadata + child membership rows. One HTTP request total.
    """
    html = _fetch("/")
    if not html:
        logger.error("phase_albums: failed to fetch homepage")
        return 0

    albums = divisare_parsers.parse_homepage_taxonomy(html)
    for album in albums:
        divisare_db.upsert_album({
            "slug":        album["album_slug"],
            "name":        album["album_name"],
            "kind":        album["kind"],
            "child_count": len(album["children"]),
        })
        divisare_db.replace_album_membership(album["album_slug"], album["children"])
        logger.info(f"  album {album['album_name']!r} ({album['kind']}) "
                    f"children={len(album['children'])}")

    return len(albums)


# ---------------------------------------------------------------------------
# Phase 4 — tags
# ---------------------------------------------------------------------------

def phase_tags(limit: int | None = None) -> int:
    pending = divisare_db.get_pending("pending_tags", limit=limit)
    processed = 0
    for row in pending:
        slug = row["slug"]
        url = f"{config.DIVISARE_BASE_URL}/{slug}"
        html = _fetch(url)
        if not html:
            divisare_db.mark_failed("pending_tags", "slug", slug, "fetch_failed")
            continue
        try:
            data = divisare_parsers.parse_tag_page(html, slug)
            divisare_db.upsert_tag(data)
            divisare_db.mark_done("pending_tags", "slug", slug)
            processed += 1
            logger.info(f"  tag /{slug} → name={data['name']!r} curated={data['curated']}")
        except RuntimeError:
            raise
        except Exception as e:
            divisare_db.mark_failed("pending_tags", "slug", slug, f"{type(e).__name__}: {e}")
    return processed


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_all(*,
            project_limit: int = 50,
            architect_limit: int = 20,
            tag_limit: int = 200,
            discover_pages_per_region: int = 1) -> None:
    """Pilot crawl: discover → architects → projects → tags → albums (taxonomy)."""
    divisare_db.init_db()

    logger.info("=== Divisare crawl: phase 1 — discover (regions → authors) ===")
    n = phase_discover(max_pages_per_region=discover_pages_per_region)
    logger.info(f"  pending_architects newly added: {n}")

    logger.info("\n=== Divisare crawl: phase 2 — architects ===")
    n = phase_architects(limit=architect_limit)
    logger.info(f"  architects processed: {n}")

    logger.info("\n=== Divisare crawl: phase 3 — projects ===")
    n = phase_projects(limit=project_limit)
    logger.info(f"  projects processed: {n}")

    logger.info("\n=== Divisare crawl: phase 4 — tags ===")
    n = phase_tags(limit=tag_limit)
    logger.info(f"  tags processed: {n}")

    logger.info("\n=== Divisare crawl: phase 5 — albums (taxonomy) ===")
    n = phase_albums()
    logger.info(f"  albums saved: {n}")

    logger.info("\n=== Final stats ===")
    for k, v in divisare_db.stats().items():
        logger.info(f"  {k}: {v}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Divisare crawler (Phase 1)")
    parser.add_argument("--phase",
                        choices=["discover", "architects", "projects", "tags", "albums", "all"],
                        default="all")
    parser.add_argument("--limit", type=int, default=50,
                        help="Project limit for --phase all/projects (default 50)")
    parser.add_argument("--architect-limit", type=int, default=20,
                        help="Architect limit for --phase all/architects (default 20)")
    parser.add_argument("--tag-limit", type=int, default=200,
                        help="Tag limit for --phase all/tags (default 200)")
    parser.add_argument("--discover-pages-per-region", type=int, default=0,
                        help="How many /designers/{region} pages to walk per region (0 = all, default)")
    args = parser.parse_args()

    divisare_db.init_db()

    try:
        if args.phase == "discover":
            cap = args.discover_pages_per_region or None
            n = phase_discover(max_pages_per_region=cap)
            logger.info(f"new pending_architects: {n}")
        elif args.phase == "architects":
            n = phase_architects(limit=args.limit)
            logger.info(f"architects processed: {n}")
        elif args.phase == "projects":
            n = phase_projects(limit=args.limit)
            logger.info(f"projects processed: {n}")
        elif args.phase == "tags":
            n = phase_tags(limit=args.limit)
            logger.info(f"tags processed: {n}")
        elif args.phase == "albums":
            n = phase_albums()
            logger.info(f"albums saved: {n}")
        else:  # all
            run_all(
                project_limit=args.limit,
                architect_limit=args.architect_limit,
                tag_limit=args.tag_limit,
                discover_pages_per_region=args.discover_pages_per_region or None,
            )
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
