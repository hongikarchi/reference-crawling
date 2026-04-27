"""Main crawl orchestrator — 4-phase workflow."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from core import config
from crawl.metalocus import database as db
from crawl.metalocus.parsers import parse_listing_page, parse_last_page_number, parse_article_page
from crawl.metalocus.downloader import download_image
from core.utils import logger, RateLimiter, create_session, fetch_page, slug_from_url


# ---------------------------------------------------------------------------
# Content filter — rejects non-building articles before saving
# ---------------------------------------------------------------------------

_JUNK_TAGS = {
    "metalocus music project",
    "metalocus recommends",
}

_JUNK_TITLE_KEYWORDS = [
    "music video", "video clip", "videoclip",
    "pritzker", "riba gold medal",
    "happy holidays", "merry christmas", "best for 20",
    "obituary",
]


def is_building_project(data):
    """Return True only if the article describes an actual building project.

    Rejects:
    - Pages tagged with known non-building tags (music, editorial)
    - Pages whose title contains award/event keywords
    - Pages with no architect, no area, and no building-type tag
    """
    tags_lower = {t.lower() for t in data.tags}
    if tags_lower & _JUNK_TAGS:
        return False

    title_lower = (data.title or "").lower()
    if any(kw in title_lower for kw in _JUNK_TITLE_KEYWORDS):
        return False

    has_architect = bool(data.architects and data.architects.strip())
    has_area = bool(data.area_sqm and str(data.area_sqm).strip())
    has_building_type = bool(data.building_type)
    if not has_architect and not has_area and not has_building_type:
        return False

    return True


def phase_discover(categories=None):
    """Phase 1: Discover listing page URLs and seed the crawl_pages table."""
    categories = categories or config.CATEGORIES
    session = create_session()
    rate_limiter = RateLimiter(config.REQUEST_DELAY_SECONDS)

    for category in categories:
        logger.info(f"Discovering pages for category: {category}")

        first_url = f"{config.BASE_URL}/{config.LANGUAGE}/category/{category}"
        html = fetch_page(first_url, session, rate_limiter)
        if not html:
            logger.error(f"Could not fetch first page for {category}")
            continue

        last_page = parse_last_page_number(html)
        if last_page is None:
            logger.warning(f"Could not determine last page for {category}, using page 0 only")
            last_page = 0

        max_pages = config.MAX_PAGES_PER_CATEGORY
        if max_pages is not None:
            last_page = min(last_page, max_pages - 1)

        logger.info(f"Category '{category}': {last_page + 1} pages to crawl")

        # Page 0 has no ?page= parameter, pages 1+ use ?page=N
        db.add_crawl_page(first_url, category, 0)
        for page_num in range(1, last_page + 1):
            page_url = f"{first_url}?page={page_num}"
            db.add_crawl_page(page_url, category, page_num)

    stats = db.get_stats()
    logger.info(f"Discovery complete. Total listing pages: {stats['crawl_pages']['total']}")


def phase_listings():
    """Phase 2: Crawl listing pages to discover article URLs."""
    session = create_session()
    rate_limiter = RateLimiter(config.REQUEST_DELAY_SECONDS)

    pending = db.get_pending_crawl_pages()
    total = len(pending)
    logger.info(f"Phase 2: {total} listing pages to crawl")

    for i, page in enumerate(pending, 1):
        url = page["url"]
        logger.info(f"[{i}/{total}] Crawling listing: {url}")

        try:
            html = fetch_page(url, session, rate_limiter)
            if not html:
                db.mark_crawl_page_failed(url, "No content returned")
                continue

            article_urls = parse_listing_page(html)
            logger.info(f"  Found {len(article_urls)} articles")

            for article_url in article_urls:
                # Ensure full URL path
                if not article_url.startswith("http"):
                    full_url = f"{config.BASE_URL}{article_url}"
                else:
                    full_url = article_url
                slug = slug_from_url(article_url)
                db.add_article(full_url, slug, url)

            db.mark_crawl_page_done(url)

        except Exception as e:
            logger.error(f"Error crawling listing {url}: {e}")
            db.mark_crawl_page_failed(url, str(e))

    stats = db.get_stats()
    logger.info(f"Listings phase complete. Articles discovered: {stats['articles']['total']}")


def phase_articles():
    """Phase 3: Crawl individual article pages and extract building data."""
    session = create_session()
    rate_limiter = RateLimiter(config.REQUEST_DELAY_SECONDS)

    limit = config.MAX_ARTICLES
    pending = db.get_pending_articles(limit=limit)
    total = len(pending)
    logger.info(f"Phase 3: {total} articles to crawl")

    for i, article in enumerate(pending, 1):
        url = article["url"]
        article_id = article["id"]
        logger.info(f"[{i}/{total}] Crawling article: {article['slug']}")

        try:
            html = fetch_page(url, session, rate_limiter)
            if not html:
                db.mark_article_failed(article_id, "No content returned")
                continue

            data = parse_article_page(html, url)

            if not is_building_project(data):
                db.mark_article_skipped(article_id, "Not a building project")
                logger.info(f"  SKIP: {(data.title or 'no title')[:70]}")
                continue

            db.save_building(article_id, data)
            db.save_tags(article_id, data.tags)

            for img in data.images:
                db.add_image(article_id, img.url, img.filename, img.alt_text, img.image_order)

            db.mark_article_done(article_id)
            logger.info(f"  OK: {data.title[:70]}  |  images: {len(data.images)}")

        except Exception as e:
            logger.error(f"Error crawling article {url}: {e}")
            db.mark_article_failed(article_id, str(e))

    stats = db.get_stats()
    logger.info(
        f"Articles phase complete. Buildings: {stats['buildings']['total']}, "
        f"Images queued: {stats['images']['total']}"
    )


def phase_images():
    """Phase 4: Download pending images using concurrent workers.

    Workers share one thread-safe RateLimiter so the request rate to
    metalocus.es stays at 1 / IMAGE_DELAY_SECONDS.  While one worker
    waits for a slow image to stream, others can begin their next
    request, cutting wall-clock time by ~50 % vs single-threaded.
    """
    # Per-thread sessions — requests.Session is not thread-safe
    _thread_local = threading.local()

    def _get_session():
        if not hasattr(_thread_local, "session"):
            _thread_local.session = create_session()
        return _thread_local.session

    rate_limiter = RateLimiter(config.IMAGE_DELAY_SECONDS)  # shared, now thread-safe

    pending = db.get_pending_images()
    total = len(pending)
    logger.info(f"Phase 4: {total} images to download ({config.IMAGE_DOWNLOAD_WORKERS} workers)")

    counter_lock = threading.Lock()
    done_count = [0]

    def _download_one(img):
        local_dir = os.path.join(config.IMAGE_BASE_DIR, img["slug"])
        local_path = os.path.join(local_dir, img["filename"])
        try:
            file_size = download_image(img["url"], local_path, _get_session(), rate_limiter)
            if file_size:
                db.mark_image_done(img["id"], local_path, file_size)
            else:
                db.mark_image_failed(img["id"], "Download returned None")
        except Exception as e:
            logger.error(f"Error downloading {img['url']}: {e}")
            db.mark_image_failed(img["id"], str(e))

        with counter_lock:
            done_count[0] += 1
            n = done_count[0]
            if n % 200 == 0 or n == total:
                logger.info(f"[{n}/{total}] Downloading images...")

    with ThreadPoolExecutor(max_workers=config.IMAGE_DOWNLOAD_WORKERS) as executor:
        executor.map(_download_one, pending)

    stats = db.get_stats()
    logger.info(
        f"Images phase complete. Downloaded: {stats['images']['completed']}, "
        f"Failed: {stats['images']['failed']}"
    )


def run_all(categories=None):
    """Run all 4 phases sequentially."""
    phase_discover(categories)
    phase_listings()
    phase_articles()
    phase_images()
