#!/usr/bin/env python3
"""Unified CLI for the archi-tinder make_db pipeline.

Usage:
    python3 run.py make-db              # unlimited crawl + full pipeline
    python3 run.py make-db --limit 20   # crawl until 20 total buildings, then pipeline
    python3 run.py make-db --limit 100  # crawl until 100 total buildings, then pipeline
    python3 run.py post-enrich          # resume after Claude enrichment session
    python3 run.py stats                # show current crawler + database progress

Workflow:
    1. run.py make-db [--limit N]
       → crawls only what's needed (incremental, no duplicates)
       → exports 1_buildings_raw.json
       → runs stage2 pre-enrich (dedup, IDs)
       → PAUSES and prints Claude enrichment prompt

    2. [run Claude enrichment session in this directory]

    3. run.py post-enrich
       → runs stage2 post-enrich (embed)
       → loads into PostgreSQL
"""

import argparse
import sys

import config
import database as db
from crawler import phase_discover, phase_listings, phase_articles, phase_images
from stage1_crawl import export_buildings
from stage2_ml import cmd_pre_enrich, cmd_post_enrich
from utils import logger


def cmd_make_db(args):
    """Crawl incrementally then run pre-enrich, pausing for Claude enrichment."""
    limit = args.limit

    db.init_db()
    completed = db.get_completed_article_count()
    logger.info(f"Buildings already in DB: {completed}")

    # --- Crawl phase ---
    if limit is not None and completed >= limit:
        logger.info(f"Already have {completed} buildings (target: {limit}). Skipping crawl.")
    else:
        if limit is not None:
            remaining = limit - completed
            logger.info(f"Need {remaining} more buildings (have {completed}, target: {limit})")
            config.MAX_ARTICLES = remaining
        else:
            logger.info("Unlimited crawl mode")
            config.MAX_ARTICLES = None

        config.MAX_PAGES_PER_CATEGORY = None  # discover all pages

        phase_discover()
        phase_listings()
        phase_articles()
        phase_images()

    # --- Stage 1: export ---
    logger.info("Exporting 1_buildings_raw.json...")
    count = export_buildings()
    if count == 0:
        logger.error("No buildings to export. Aborting.")
        sys.exit(1)

    # --- Stage 2a-c: pre-enrich ---
    logger.info("Running stage 2 pre-enrich (dedup + IDs)...")
    cmd_pre_enrich()

    # --- Pause for Claude enrichment ---
    print()
    print("=" * 60)
    print("PIPELINE PAUSED — Claude enrichment needed")
    print("=" * 60)
    print()
    print("Start a Claude Code session in this directory and use")
    print("this prompt:")
    print()
    print('  "Read data/2_buildings_to_enrich.json. For each building,')
    print("   fill null fields: name_en, mood, material, program.")
    print("   program must be exactly one of: Housing, Office, Museum,")
    print("   Education, Religion, Sports, Transport, Hospitality,")
    print("   Healthcare, Public, Mixed Use, Landscape, Infrastructure,")
    print('   Other. Write output to data/3_buildings_enriched.json."')
    print()
    print("Then run:")
    print("  python3 run.py post-enrich")
    print("=" * 60)


def cmd_post_enrich(args):
    """Resume after Claude enrichment: post-enrich + PostgreSQL load."""
    import os
    import stage3_postgres

    enriched = os.path.join(config.BASE_DIR, "data", "3_buildings_enriched.json")
    if not os.path.exists(enriched):
        print(f"ERROR: {enriched} not found.")
        print("Run the Claude enrichment session first, then retry.")
        sys.exit(1)

    # --- Stage 2e: post-enrich (validate + embed) ---
    logger.info("Running stage 2 post-enrich (validate + embed)...")
    cmd_post_enrich()

    # --- Stage 3: load into PostgreSQL ---
    logger.info("Loading into PostgreSQL...")
    stage3_postgres.main_programmatic(reset=False)

    print("\nPipeline complete!")
    print("  architecture_vectors is populated and ready.")


def cmd_stats(args):
    """Show crawler and pipeline progress."""
    stats = db.get_stats()
    print("\n=== make_db Status ===\n")
    for table in ["crawl_pages", "articles", "images"]:
        s = stats[table]
        print(f"  {table}:")
        print(f"    Total: {s['total']}  |  Pending: {s['pending']}  |  "
              f"Completed: {s['completed']}  |  Failed: {s['failed']}")
    print(f"\n  buildings (SQLite): {stats['buildings']['total']}")
    print(f"  tags:               {stats['tags']['total']}")

    # Check output files
    import os
    import json
    data_dir = os.path.join(config.BASE_DIR, "data")
    for fname in ["1_buildings_raw.json", "2_buildings_to_enrich.json",
                  "3_buildings_enriched.json", "4_buildings_processed.json"]:
        path = os.path.join(data_dir, fname)
        if os.path.exists(path):
            with open(path) as f:
                n = len(json.load(f))
            print(f"  {fname}: {n} buildings")
        else:
            print(f"  {fname}: (not found)")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="archi-tinder make_db pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p_make = sub.add_parser("make-db", help="Crawl + export + pre-enrich (pauses for Claude)")
    p_make.add_argument(
        "--limit", type=int, default=None,
        help="Target total buildings (e.g. 20, 100). Default: unlimited"
    )
    p_make.set_defaults(func=cmd_make_db)

    p_post = sub.add_parser("post-enrich", help="Post-enrich + PostgreSQL load (after Claude session)")
    p_post.set_defaults(func=cmd_post_enrich)

    p_stats = sub.add_parser("stats", help="Show crawler and pipeline progress")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    db.init_db()
    args.func(args)


if __name__ == "__main__":
    main()
