"""Configuration constants for the Metalocus crawler."""

import os

BASE_URL = "https://www.metalocus.es"
LANGUAGE = "en"

CATEGORIES = [
    "architecture",
]

# Rate limiting
REQUEST_DELAY_SECONDS = 2.0
IMAGE_DELAY_SECONDS = 0.5
RETRY_MAX_ATTEMPTS = 3
RETRY_BACKOFF_FACTOR = 2.0
REQUEST_TIMEOUT = 30

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "data", "metalocus.db")
IMAGE_BASE_DIR = os.path.join(BASE_DIR, "images")

# Image settings
IMAGE_STYLE_PREFERENCE = "max_2600x2600"

# Crawl scope (set to integers for testing, None for full crawl)
MAX_PAGES_PER_CATEGORY = None
MAX_ARTICLES = None

# HTTP
USER_AGENT = "MetalocusArchCrawler/1.0 (academic research; respectful crawling)"
