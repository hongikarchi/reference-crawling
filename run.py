#!/usr/bin/env python3
"""Unified CLI for the archi-tinder make_db pipeline.

Common workflows:

  Build / extend the dataset
    python3 run.py make-db [--limit N]   # crawl + export + dedup (metalocus)
    python3 run.py crawl --articles 500  # metalocus-only (resume image downloads)
    python3 run.py export-dedup          # SQLite → 1_buildings_raw.json + dedup
    python3 run.py harness               # enrich + analyze + QC (Anthropic API)
    python3 run.py embed                 # final embeddings → 4_buildings_final.json
    python3 run.py embed-rate            # embed + quality review/fix/rate
    python3 run.py crawl-divisare        # Divisare canonical crawl (auth'd)

  Quality + auditing
    python3 run.py quality review        # validate fields, vocab, embeddings
    python3 run.py quality fix           # auto-normalize + clean
    python3 run.py quality rate          # 6-dim quality score
    python3 run.py quality diagnose      # distribution analysis
    python3 run.py stats                 # crawler + pipeline + rating summary

  Vocab / eval / re-processing
    python3 run.py migrate-vocab [--apply]
    python3 run.py label-golden --sample 30 [--source opus]
    python3 run.py eval [--limit N] [--only enrich|analyze]
    python3 run.py reprocess --from-vocab-migration [--apply]

  Upload (manual gate)
    python3 upload.py --dry-run
    python3 upload.py            # only after explicit user approval
"""

import argparse
import json
import os
import sys

import config
import database as db
from utils import logger


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_export_dedup() -> int:
    """Stage 1 export + Stage 2 dedup. Returns number of unique buildings.

    Shared between `make-db` and the standalone `export-dedup` subcommand.
    """
    from stage1_export import export_buildings

    logger.info("Exporting 1_buildings_raw.json...")
    count = export_buildings()
    if count == 0:
        logger.error("No buildings to export.")
        return 0

    logger.info("Running stage2_dedup (dedup + IDs)...")
    import stage2_dedup
    from sentence_transformers import SentenceTransformer

    buildings = stage2_dedup.load_json(config.RAW_JSON)
    review_path = os.path.join(config.DATA_DIR, "duplicates_review.json")
    existing_review = (stage2_dedup.load_json(review_path)
                       if os.path.exists(review_path) else None)

    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    buildings = stage2_dedup._generate_preliminary_embeddings(buildings, model)
    buildings, review_pairs = stage2_dedup.run_deduplication(buildings, existing_review)
    for b in buildings:
        b.pop("_prelim_embedding", None)
    if review_pairs:
        stage2_dedup.save_json(review_path, review_pairs)
        logger.info(f"{len(review_pairs)} uncertain pairs → {review_path}")
    buildings = stage2_dedup.assign_building_ids(buildings)
    stage2_dedup.reorganize_images(buildings)
    stage2_dedup.save_json(config.RAW_JSON, buildings)
    logger.info(f"Dedup complete: {len(buildings)} unique buildings")
    return len(buildings)


def _print_pipeline_status() -> None:
    """Pipeline-side counts (raw/enriched/analyzed/final + needs counts).
    Folded in from pipeline.print_status (Phase 8A consolidation).
    """
    print("\n=== Pipeline Status ===\n")
    counts = {}
    for path, label in [
        (config.RAW_JSON,      "1_buildings_raw"),
        (config.ENRICHED_JSON, "2_buildings_enriched"),
        (config.ANALYZED_JSON, "3_buildings_analyzed"),
        (config.FINAL_JSON,    "4_buildings_final"),
    ]:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                n = len(json.load(f))
            counts[label] = n
            print(f"  {label}: {n} buildings")
        else:
            counts[label] = 0
            print(f"  {label}: (not found)")

    needs_text = max(0, counts["1_buildings_raw"] - counts["2_buildings_enriched"])
    needs_img  = max(0, counts["2_buildings_enriched"] - counts["3_buildings_analyzed"])
    final_n    = counts["4_buildings_final"]
    r2_gb      = final_n * 2.1 / 1024
    print(f"\n  Needs text enrichment:  {needs_text}")
    print(f"  Needs image analysis:   {needs_img}")
    print(f"  R2 estimate:            {r2_gb:.2f} GB ({final_n} buildings × 2.1 MB)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_make_db(args):
    """Crawl incrementally then dedup. Pauses with instructions for the harness."""
    from crawler import phase_discover, phase_listings, phase_articles, phase_images

    limit = args.limit
    completed = db.get_completed_article_count()
    logger.info(f"Buildings already in DB: {completed}")

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
        config.MAX_PAGES_PER_CATEGORY = None

        phase_discover()
        phase_listings()
        phase_articles()
        phase_images()

    if _run_export_dedup() == 0:
        sys.exit(1)

    print()
    print("=" * 60)
    print("CRAWL + EXPORT + DEDUP COMPLETE")
    print("=" * 60)
    print()
    print("Next: run the AI pipeline harness (queue-driven, crash-safe):")
    print("  python3 run.py harness")
    print()
    print("Then embed and review:")
    print("  python3 run.py embed-rate")
    print()
    print("When quality is acceptable, manually upload:")
    print("  python3 upload.py --dry-run")
    print("  python3 upload.py")
    print("=" * 60)


def cmd_crawl(args):
    """Crawl next batch only (no export/dedup). Resumes image downloads."""
    config.MAX_ARTICLES = args.articles
    config.MAX_PAGES_PER_CATEGORY = None
    from crawler import phase_articles, phase_images
    logger.info(f"=== crawl batch (max_articles={args.articles}) ===")
    phase_articles()
    phase_images()


def cmd_export_dedup(args):
    """Run stage1_export + stage2_dedup standalone."""
    n = _run_export_dedup()
    if n == 0:
        sys.exit(1)


def cmd_embed(args):
    """Run stage3_embed.py."""
    if not os.path.exists(config.ANALYZED_JSON):
        print(f"ERROR: {config.ANALYZED_JSON} not found.")
        print("Run image analysis first: python3 run.py harness")
        sys.exit(1)
    import stage3_embed
    stage3_embed.main()


def cmd_embed_rate(args):
    """stage3_embed + quality review + quality fix + quality rate. Returns score."""
    cmd_embed(args)
    import quality
    quality.run_review()
    quality.run_fix()
    quality.run_rate()


def cmd_quality(args):
    """quality {review,fix,rate,diagnose}."""
    import quality
    dispatch = {
        "review":   quality.run_review,
        "fix":      quality.run_fix,
        "rate":     quality.run_rate,
        "diagnose": quality.run_diagnose,
    }
    dispatch[args.subcmd]()


def cmd_migrate_vocab(args):
    """Apply vocab.py legacy migrations to existing data records."""
    import migrate_vocab
    sys.argv = ["migrate_vocab.py"] + (["--apply"] if args.apply else [])
    sys.exit(migrate_vocab.main())


def cmd_label_golden(args):
    """Bootstrap data/golden/buildings.json (regression baseline or Opus labeling)."""
    import label_golden
    forwarded = ["label_golden.py",
                 "--sample", str(args.sample),
                 "--source", args.source,
                 "--seed", str(args.seed)]
    if args.only:
        forwarded += ["--only", args.only]
    if args.append:
        forwarded += ["--append"]
    sys.argv = forwarded
    sys.exit(label_golden.main())


def cmd_eval(args):
    """Score current prompts against the golden set."""
    import eval as _eval
    forwarded = ["eval.py"]
    if args.limit:
        forwarded += ["--limit", str(args.limit)]
    if args.only:
        forwarded += ["--only", args.only]
    if args.label:
        forwarded += ["--label", args.label]
    sys.argv = forwarded
    sys.exit(_eval.main())


def cmd_reprocess(args):
    """Targeted re-processing — reads vocab_migration.json, eval_report.json, or an ids file."""
    import reprocess
    forwarded = ["reprocess.py"]
    if args.from_vocab_migration:
        forwarded += ["--from-vocab-migration"]
    if args.from_eval_report:
        forwarded += ["--from-eval-report", "--eval-threshold", str(args.eval_threshold)]
    if args.from_ids_file:
        forwarded += ["--from-ids-file", args.from_ids_file]
    forwarded += ["--task", args.task]
    if args.limit:
        forwarded += ["--limit", str(args.limit)]
    if args.dry_run:
        forwarded += ["--dry-run"]
    sys.argv = forwarded
    sys.exit(reprocess.main())


def cmd_stats(args):
    """Crawler + pipeline + rating summary."""
    stats = db.get_stats()
    print("\n=== make_db Status ===\n")
    for table in ["crawl_pages", "articles", "images"]:
        s = stats[table]
        skipped = f"  Skipped: {s.get('skipped', 0)}" if "skipped" in s else ""
        print(f"  {table}:")
        print(f"    Total: {s['total']}  |  Pending: {s['pending']}  |  "
              f"Completed: {s['completed']}  |  Failed: {s['failed']}{skipped}")
    print(f"\n  buildings (SQLite): {stats['buildings']['total']}")
    print(f"  tags:               {stats['tags']['total']}")

    _print_pipeline_status()

    rating_path = os.path.join(config.REPORTS_DIR, "rating_report.json")
    if os.path.exists(rating_path):
        with open(rating_path, encoding="utf-8") as f:
            rating = json.load(f)
        print(f"\n  Last rating: {rating['overall']}/100 — {rating['judgment']}")
    print()


def cmd_match_canonical(args):
    """Match metalocus buildings → Divisare canonical projects."""
    import match_to_canonical
    forwarded = ["match_to_canonical.py"]
    if args.limit:
        forwarded += ["--limit", str(args.limit)]
    if args.building_id:
        forwarded += ["--building-id", args.building_id]
    sys.argv = forwarded
    sys.exit(match_to_canonical.main())


def cmd_crawl_divisare(args):
    """Run Divisare 4-phase crawler. Requires authenticated session
    (`python3 divisare_auth.py login` first)."""
    import divisare_crawler
    sys.argv = ["divisare_crawler.py", "--phase", args.phase,
                "--limit", str(args.limit),
                "--architect-limit", str(args.architect_limit),
                "--tag-limit", str(args.tag_limit),
                "--discover-pages-per-region", str(args.discover_pages_per_region)]
    sys.exit(divisare_crawler.main())


def cmd_harness(args):
    """Auto-enrich + analyze new buildings via Anthropic API (queue-driven)."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(config.BASE_DIR, ".env"))

    import pipeline_harness

    if args.status:
        pipeline_harness.print_harness_status()
        return

    if args.enqueue_only:
        pipeline_harness.run_harness(limit=args.limit, run_embed=False, enqueue_only=True)
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        print("Add: ANTHROPIC_API_KEY=sk-ant-... to your .env file")
        sys.exit(1)

    pipeline_harness.run_harness(
        limit=args.limit,
        run_embed=not args.no_embed,
        enqueue_only=False,
    )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="archi-tinder make_db pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    # make-db
    p = sub.add_parser("make-db", help="Crawl + export + dedup")
    p.add_argument("--limit", type=int, default=None,
                   help="Target total buildings. Default: unlimited")
    p.set_defaults(func=cmd_make_db)

    # crawl (formerly pipeline.py crawl)
    p = sub.add_parser("crawl", help="Crawl next batch of articles + images")
    p.add_argument("--articles", type=int, default=500,
                   help="Max articles to crawl (default 500)")
    p.set_defaults(func=cmd_crawl)

    # export-dedup (formerly pipeline.py export-dedup)
    p = sub.add_parser("export-dedup", help="stage1_export + stage2_dedup")
    p.set_defaults(func=cmd_export_dedup)

    # embed
    p = sub.add_parser("embed", help="Run stage3_embed.py")
    p.set_defaults(func=cmd_embed)

    # embed-rate (formerly pipeline.py embed-rate)
    p = sub.add_parser("embed-rate", help="embed + quality review + fix + rate")
    p.set_defaults(func=cmd_embed_rate)

    # crawl-divisare
    p = sub.add_parser("crawl-divisare",
                       help="Authenticated Divisare crawler (4 phases → data/divisare.db)")
    p.add_argument("--phase", choices=["discover", "architects", "projects", "tags", "albums", "all"],
                   default="all")
    p.add_argument("--limit", type=int, default=50,
                   help="Project limit for --phase all/projects (default 50)")
    p.add_argument("--architect-limit", type=int, default=20)
    p.add_argument("--tag-limit", type=int, default=200)
    p.add_argument("--discover-pages-per-region", type=int, default=1)
    p.set_defaults(func=cmd_crawl_divisare)

    # match-canonical
    p = sub.add_parser("match-canonical",
                       help="Match metalocus buildings to Divisare canonical projects")
    p.add_argument("--limit", type=int, default=None,
                   help="Score only the first N metalocus buildings")
    p.add_argument("--building-id", type=str, default=None,
                   help="Score only this metalocus building_id (debug)")
    p.set_defaults(func=cmd_match_canonical)

    # harness
    p = sub.add_parser("harness", help="Queue-driven AI pipeline (enrich + analyze + QC)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--status", action="store_true")
    p.add_argument("--no-embed", action="store_true")
    p.add_argument("--enqueue-only", action="store_true")
    p.set_defaults(func=cmd_harness)

    # quality {review,fix,rate,diagnose}
    p = sub.add_parser("quality", help="Quality review/fix/rate/diagnose on 4_buildings_final.json")
    p.add_argument("subcmd", choices=["review", "fix", "rate", "diagnose"])
    p.set_defaults(func=cmd_quality)

    # migrate-vocab
    p = sub.add_parser("migrate-vocab", help="Apply vocab.py migrations to existing records")
    p.add_argument("--apply", action="store_true",
                   help="Write changes (default: dry-run audit only)")
    p.set_defaults(func=cmd_migrate_vocab)

    # label-golden
    p = sub.add_parser("label-golden", help="Seed data/golden/buildings.json")
    p.add_argument("--sample", type=int, default=30)
    p.add_argument("--source", choices=["current", "opus"], default="current")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--only", choices=["enrich", "analyze"], default=None)
    p.add_argument("--append", action="store_true")
    p.set_defaults(func=cmd_label_golden)

    # eval
    p = sub.add_parser("eval", help="Score current prompts vs golden set")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--only", choices=["enrich", "analyze"], default=None)
    p.add_argument("--label", type=str, default=None)
    p.set_defaults(func=cmd_eval)

    # reprocess
    p = sub.add_parser("reprocess", help="Enqueue targeted re-processing")
    p.add_argument("--from-vocab-migration", action="store_true")
    p.add_argument("--from-eval-report", action="store_true")
    p.add_argument("--eval-threshold", type=float, default=0.7)
    p.add_argument("--from-ids-file", type=str, default=None)
    p.add_argument("--task", choices=["enrich", "analyze", "both"], default="analyze")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_reprocess)

    # stats
    p = sub.add_parser("stats", help="Crawler + pipeline + rating summary")
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    db.init_db()
    args.func(args)


if __name__ == "__main__":
    main()
