#!/usr/bin/env python3
"""CLI entry point for the Metalocus architecture crawler."""

import argparse
import sys

import config
import database as db
from crawler import phase_discover, phase_listings, phase_articles, phase_images, run_all
from utils import logger


def cmd_discover(args):
    categories = args.categories.split(",") if args.categories else None
    phase_discover(categories)


def cmd_listings(args):
    phase_listings()


def cmd_articles(args):
    phase_articles()


def cmd_images(args):
    phase_images()


def cmd_all(args):
    categories = args.categories.split(",") if args.categories else None
    run_all(categories)


def cmd_retry(args):
    table = args.only if args.only else None
    db.reset_failed(table)
    logger.info(f"Reset failed items to pending" + (f" (table: {table})" if table else " (all tables)"))


def cmd_stats(args):
    stats = db.get_stats()
    print("\n=== Metalocus Crawler Stats ===\n")
    for table_name in ["crawl_pages", "articles", "images"]:
        s = stats[table_name]
        print(f"  {table_name}:")
        print(f"    Total: {s['total']}  |  Pending: {s['pending']}  |  Completed: {s['completed']}  |  Failed: {s['failed']}")
    print(f"\n  buildings: {stats['buildings']['total']}")
    print(f"  tags: {stats['tags']['total']}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Metalocus.es architecture crawler")
    parser.add_argument("--delay", type=float, help="Request delay in seconds")
    parser.add_argument("--max-pages", type=int, help="Max listing pages per category")
    parser.add_argument("--max-articles", type=int, help="Max articles to crawl")
    parser.add_argument("--db", help="Database file path")
    parser.add_argument("--image-dir", help="Image download directory")

    sub = parser.add_subparsers(dest="command", help="Crawler phase to run")

    p_discover = sub.add_parser("discover", help="Phase 1: Discover listing page URLs")
    p_discover.add_argument("--categories", help="Comma-separated category list")
    p_discover.set_defaults(func=cmd_discover)

    p_listings = sub.add_parser("listings", help="Phase 2: Crawl listing pages for article URLs")
    p_listings.set_defaults(func=cmd_listings)

    p_articles = sub.add_parser("articles", help="Phase 3: Crawl article pages for building data")
    p_articles.set_defaults(func=cmd_articles)

    p_images = sub.add_parser("images", help="Phase 4: Download images")
    p_images.set_defaults(func=cmd_images)

    p_all = sub.add_parser("all", help="Run all phases sequentially")
    p_all.add_argument("--categories", help="Comma-separated category list")
    p_all.set_defaults(func=cmd_all)

    p_retry = sub.add_parser("retry", help="Reset failed items to pending")
    p_retry.add_argument("--only", choices=["crawl_pages", "articles", "images"], help="Only reset specific table")
    p_retry.set_defaults(func=cmd_retry)

    p_stats = sub.add_parser("stats", help="Show crawl progress statistics")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()

    # Apply CLI overrides to config
    if args.delay:
        config.REQUEST_DELAY_SECONDS = args.delay
    if args.max_pages is not None:
        config.MAX_PAGES_PER_CATEGORY = args.max_pages
    if args.max_articles is not None:
        config.MAX_ARTICLES = args.max_articles
    if args.db:
        config.DATABASE_PATH = args.db
    if args.image_dir:
        config.IMAGE_BASE_DIR = args.image_dir

    # Initialize database
    db.init_db()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
