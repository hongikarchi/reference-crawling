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
IMAGE_DOWNLOAD_WORKERS = 4      # concurrent image download threads
RETRY_MAX_ATTEMPTS = 3
RETRY_BACKOFF_FACTOR = 2.0
REQUEST_TIMEOUT = 30

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DATABASE_PATH = os.path.join(DATA_DIR, "metalocus.db")
IMAGE_BASE_DIR = os.path.join(BASE_DIR, "images")

# Data files (in pipeline order)
RAW_JSON      = os.path.join(DATA_DIR, "1_buildings_raw.json")
ENRICHED_JSON = os.path.join(DATA_DIR, "2_buildings_enriched.json")
ANALYZED_JSON = os.path.join(DATA_DIR, "3_buildings_analyzed.json")
FINAL_JSON    = os.path.join(DATA_DIR, "4_buildings_final.json")
REGISTRY_JSON = os.path.join(DATA_DIR, "id_registry.json")
REPORTS_DIR   = os.path.join(DATA_DIR, "reports")

# Crawl scope (set at runtime by run.py)
MAX_PAGES_PER_CATEGORY = None
MAX_ARTICLES = None

# HTTP
USER_AGENT = "MetalocusArchCrawler/1.0 (academic research; respectful crawling)"

# Harness (pipeline_harness.py + agents)
# State for the queue-driven worker lives in data/tasks.db (see tasks_db.py).
# Hard-failure quarantines are appended to FAILED_LOG_JSON.
FAILED_LOG_JSON     = os.path.join(DATA_DIR, "failed_log.json")
HARNESS_MAX_RETRIES = 3
HARNESS_MODEL       = "claude-sonnet-4-6"
HARNESS_MAX_IMAGES  = 3   # cover + up to N-1 additional upload photos

# Divisare (Phase 0+) — see ~/.claude/plans/db-fuzzy-lerdorf.md
DIVISARE_BASE_URL              = "https://divisare.com"
DIVISARE_LOGIN_URL             = "https://account.divisare.com/login"  # verify in Phase 0
DIVISARE_REQUEST_DELAY_SECONDS = 3.0  # respectful default for paid-account access
DIVISARE_USER_AGENT            = "ArchiTinderResearch/1.0 (paid-member; respectful crawling)"
DIVISARE_SESSION_PATH          = os.path.join(DATA_DIR, ".divisare_session.json")
DIVISARE_DB_PATH               = os.path.join(DATA_DIR, "divisare.db")
# Sample project for --verify; replace with a known-good URL once Phase 0 confirms.
DIVISARE_TEST_PROJECT_URL      = "https://divisare.com/projects/556458-s-ar-oratory-chapel"
