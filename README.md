# Reference Crawling

Architecture reference crawler for [metalocus.es](https://www.metalocus.es) — extracts building data, metadata, and images into a local SQLite database.

## Features

- **4-phase pipeline**: discover → listings → articles → images
- **Resumable**: tracks pending/completed/failed status per item, so you can stop and restart anytime
- **Respectful**: rate limiting, retry with exponential backoff, polite user-agent
- **Structured data**: extracts title, architects, location, year, area, materials, credits, tags, and more
- **Image downloading**: streams high-res images with deduplication

## Setup

```bash
pip install -r requirements.txt
```

## Usage

Run all phases at once:

```bash
python run.py all
```

Or run each phase individually:

```bash
python run.py discover          # Phase 1: Find listing page URLs
python run.py listings          # Phase 2: Crawl listings for article URLs
python run.py articles          # Phase 3: Extract building data from articles
python run.py images            # Phase 4: Download images
```

Check progress:

```bash
python run.py stats
```

Retry failed items:

```bash
python run.py retry             # Reset all failed items
python run.py retry --only images   # Reset only failed images
```

### Options

```
--delay SECONDS       Request delay (default: 2.0)
--max-pages N         Max listing pages per category
--max-articles N      Max articles to crawl
--db PATH             Custom database path
--image-dir PATH      Custom image directory
--categories LIST     Comma-separated categories (e.g. architecture,design)
```

## Project Structure

```
├── run.py            # CLI entry point
├── crawler.py        # 4-phase crawl orchestrator
├── parsers.py        # HTML parsing (listings, articles, images)
├── database.py       # SQLite database layer
├── downloader.py     # Image downloader with retry
├── models.py         # Data classes (BuildingData, ImageData)
├── utils.py          # Rate limiter, HTTP session, logging
├── config.py         # Configuration constants
└── requirements.txt  # Python dependencies
```

## Data Output

- **SQLite database** in `data/metalocus.db` with tables: `crawl_pages`, `articles`, `buildings`, `tags`, `article_tags`, `images`
- **Images** saved to `images/<article-slug>/` directories
