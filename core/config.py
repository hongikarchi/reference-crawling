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

# Paths — config.py lives in `core/`, so BASE_DIR is its grandparent (project root).
# data/ is sub-divided by pipeline stage (mirrors the code package layout):
#   data/crawl/      — Stage 1 source-of-truth DBs (metalocus.db, divisare.db)
#   data/enrich/     — Stages 2-3 outputs + harness queue + eval datasets
#   data/canonical/  — Stage 4 matches + canonical artefact + clusters
#   data/reports/    — QC + audit JSON (cross-stage)
#   data/id_registry.json — stable building_id assignments (NEVER delete)
BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR        = os.path.join(BASE_DIR, "data")
CRAWL_DIR       = os.path.join(DATA_DIR, "crawl")
ENRICH_DIR      = os.path.join(DATA_DIR, "enrich")
CANONICAL_DIR   = os.path.join(DATA_DIR, "canonical")
REPORTS_DIR     = os.path.join(DATA_DIR, "reports")
IMAGE_BASE_DIR  = os.path.join(BASE_DIR, "images")

# Stage-1 raw DBs
DATABASE_PATH    = os.path.join(CRAWL_DIR, "metalocus.db")

# Stage-2/3 outputs (in pipeline order)
RAW_JSON      = os.path.join(ENRICH_DIR, "1_buildings_raw.json")
ENRICHED_JSON = os.path.join(ENRICH_DIR, "2_buildings_enriched.json")
ANALYZED_JSON = os.path.join(ENRICH_DIR, "3_buildings_analyzed.json")
FINAL_JSON    = os.path.join(ENRICH_DIR, "4_buildings_final.json")

# Stable global registry — NEVER delete
REGISTRY_JSON = os.path.join(DATA_DIR, "id_registry.json")

# Crawl scope (set at runtime by run.py)
MAX_PAGES_PER_CATEGORY = None
MAX_ARTICLES = None

# HTTP
USER_AGENT = "MetalocusArchCrawler/1.0 (academic research; respectful crawling)"

# Phase 11 / 5-stage compliance: metalocus crawler stores image URLs only;
# no downloads at stage 1. Cover selection + R2 upload move to stage 4
# (canonical/image_dedup) + stage 5 (upload). Existing 3,465 production
# rows keep their on-disk images.
METALOCUS_DOWNLOAD_IMAGES = False

# Harness (enrich/harness.py + agents)
# State for the queue-driven worker lives in data/enrich/tasks.db (see enrich/tasks_db.py).
# Hard-failure quarantines are appended to FAILED_LOG_JSON.
TASKS_DB_PATH       = os.path.join(ENRICH_DIR, "tasks.db")
FAILED_LOG_JSON     = os.path.join(ENRICH_DIR, "failed_log.json")
HARNESS_MAX_RETRIES = 3
HARNESS_MODEL       = "claude-sonnet-4-6"
HARNESS_MAX_IMAGES  = 3   # cover + up to N-1 additional upload photos

# Divisare (Phase 0+) — see ~/.claude/plans/db-fuzzy-lerdorf.md
DIVISARE_BASE_URL              = "https://divisare.com"
DIVISARE_LOGIN_URL             = "https://divisare.com/login"          # form GET (CSRF)
DIVISARE_LOGIN_POST_URL        = "https://divisare.com/people/login"   # form POST target
DIVISARE_REQUEST_DELAY_SECONDS = 3.0  # respectful default for paid-account access
# Browser UA — Divisare returns Cloudflare challenges to non-browser UAs
DIVISARE_USER_AGENT            = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36")
DIVISARE_SESSION_PATH          = os.path.join(DATA_DIR, ".divisare_session.json")
DIVISARE_DB_PATH               = os.path.join(CRAWL_DIR, "divisare.db")
# Sample project for `verify`; replace if it 404s.
DIVISARE_TEST_PROJECT_URL      = "https://divisare.com/projects/556458-s-ar-oratory-chapel"

# Architizer (Phase 7) — public read, no auth, sitemap-driven discovery.
# Recon: .claude/research/architizer-schema.md
ARCHITIZER_BASE_URL              = "https://architizer.com"
ARCHITIZER_AWARDS_BASE_URL       = "https://winners.architizer.com"
ARCHITIZER_REQUEST_DELAY_SECONDS = 2.0
ARCHITIZER_USER_AGENT            = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/120.0.0.0 Safari/537.36")
ARCHITIZER_DB_PATH               = os.path.join(CRAWL_DIR, "architizer.db")

# Archello (Phase 8) — public read, no auth, sitemap-driven, browser-UA only.
# Recon: .claude/research/archello-schema.md (note: Content-Signal: ai-train=no
# is acknowledged; our use is metadata mirroring, not model training).
ARCHELLO_BASE_URL              = "https://archello.com"
ARCHELLO_REQUEST_DELAY_SECONDS = 1.5  # above published crawl-delay: 1; recon burst test (5 sequential @ 0s) all 200, no challenge
ARCHELLO_USER_AGENT            = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36")
ARCHELLO_DB_PATH               = os.path.join(CRAWL_DIR, "archello.db")
