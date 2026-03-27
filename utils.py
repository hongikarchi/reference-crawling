"""Utilities: rate limiter, HTTP helpers, logging setup."""

import logging
import os
import time

import requests

import config


def setup_logging():
    logger = logging.getLogger("metalocus")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.addHandler(console)

        log_path = os.path.join(config.BASE_DIR, "crawl.log")
        fh = logging.FileHandler(log_path)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)

    return logger


logger = setup_logging()


class RateLimiter:
    def __init__(self, delay):
        self.delay = delay
        self._last_request = 0.0

    def wait(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()


def create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return session


def fetch_page(url, session, rate_limiter):
    """Fetch a URL with rate limiting and retries. Returns response text or None."""
    rate_limiter.wait()

    for attempt in range(1, config.RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", config.RETRY_BACKOFF_FACTOR ** attempt))
                logger.warning(f"Rate limited on {url}, waiting {retry_after}s")
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                wait = config.RETRY_BACKOFF_FACTOR ** attempt
                logger.warning(f"Server error {resp.status_code} on {url}, retry {attempt}/{config.RETRY_MAX_ATTEMPTS} in {wait}s")
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                logger.warning(f"404 Not Found: {url}")
                return None

            resp.raise_for_status()
            return resp.text

        except requests.RequestException as e:
            if attempt < config.RETRY_MAX_ATTEMPTS:
                wait = config.RETRY_BACKOFF_FACTOR ** attempt
                logger.warning(f"Request error on {url}: {e}, retry {attempt}/{config.RETRY_MAX_ATTEMPTS} in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"Failed after {config.RETRY_MAX_ATTEMPTS} attempts: {url} - {e}")
                raise

    return None


def slug_from_url(url):
    """Extract slug from article URL like /en/news/some-article-slug."""
    return url.rstrip("/").split("/")[-1]
