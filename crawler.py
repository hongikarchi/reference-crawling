"""Main crawl orchestrator — 4-phase workflow."""

import os

import config
import database as db
from parsers import parse_listing_page, parse_last_page_number, parse_article_page
from downloader import download_image
from utils import logger, RateLimiter, create_session, fetch_page, slug_from_url


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

            db.save_building(article_id, data)
            db.save_tags(article_id, data.tags)

            for img in data.images:
                db.add_image(article_id, img.url, img.filename, img.alt_text, img.image_order)

            db.mark_article_done(article_id)
            logger.info(f"  Title: {data.title[:80]}  |  Images: {len(data.images)}  |  Tags: {len(data.tags)}")

        except Exception as e:
            logger.error(f"Error crawling article {url}: {e}")
            db.mark_article_failed(article_id, str(e))

    stats = db.get_stats()
    logger.info(
        f"Articles phase complete. Buildings: {stats['buildings']['total']}, "
        f"Images queued: {stats['images']['total']}"
    )


def phase_images():
    """Phase 4: Download pending images."""
    session = create_session()
    rate_limiter = RateLimiter(config.IMAGE_DELAY_SECONDS)

    pending = db.get_pending_images()
    total = len(pending)
    logger.info(f"Phase 4: {total} images to download")

    for i, img in enumerate(pending, 1):
        slug = img["slug"]
        url = img["url"]
        filename = img["filename"]
        image_id = img["id"]

        local_dir = os.path.join(config.IMAGE_BASE_DIR, slug)
        local_path = os.path.join(local_dir, filename)

        if i % 50 == 0 or i == 1:
            logger.info(f"[{i}/{total}] Downloading images...")

        try:
            file_size = download_image(url, local_path, session, rate_limiter)
            if file_size:
                db.mark_image_done(image_id, local_path, file_size)
            else:
                db.mark_image_failed(image_id, "Download returned None")
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
            db.mark_image_failed(image_id, str(e))

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
