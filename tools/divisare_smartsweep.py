"""Smart sweep — for each architect in our DB, walk /projects/built page 1.
If all ids are already in our DB AND page 1 isn't full (<11), the architect
is fully covered. Otherwise walk more pages until no new ids appear.

Newly discovered project ids → enqueue into pending_projects (lite is also
upserted via parse_author_built_projects_rich), so a subsequent
phase_projects deep-fetch will pick them up.

Usage:  python3 -m tools.divisare_smartsweep [--start 0] [--limit 99999]
                                              [--rate 1.0]
Resumes from --start (offset into divisare_architects ordered by id ASC).
"""
import argparse
import re
import sqlite3
import time
import traceback

from crawl.divisare import auth, db as divisare_db, parsers as divisare_parsers


PROJECT_HREF_RE = re.compile(r"/projects/(\d+)-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=99999)
    ap.add_argument("--rate", type=float, default=1.0,
                    help="Seconds between requests (default 1.0)")
    ap.add_argument("--max-pages", type=int, default=50,
                    help="Cap pages per architect (default 50)")
    args = ap.parse_args()

    session = auth.get_authenticated_session()
    session.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36",
    )
    conn = sqlite3.connect("data/crawl/divisare.db")
    conn.row_factory = sqlite3.Row

    architects = conn.execute(
        "SELECT id, slug, name FROM divisare_architects "
        "ORDER BY id ASC LIMIT ? OFFSET ?",
        (args.limit, args.start),
    ).fetchall()
    print(f"Sweeping {len(architects)} architects (offset {args.start}) ...")

    total_new_ids = 0
    architects_with_new = 0
    pages_fetched = 0
    t0 = time.time()

    for i, arch in enumerate(architects, 1):
        arch_id = arch["id"]
        slug = f"{arch_id}-{arch['slug']}"
        author_path = f"/authors/{slug}"
        new_for_this = 0
        page = 1
        prev_ids: set[int] = set()

        while page <= args.max_pages:
            base = f"https://divisare.com{author_path}/projects/built"
            url = base if page == 1 else f"{base}?page={page}"
            try:
                r = session.get(url, timeout=20, allow_redirects=True)
                pages_fetched += 1
            except Exception as e:
                print(f"[{i}/{len(architects)}] {arch['name'][:30]} page={page} "
                      f"ERROR {type(e).__name__}: {e}")
                break
            if r.status_code != 200:
                break
            page_ids = {int(m) for m in PROJECT_HREF_RE.findall(r.text)}
            new_ids = page_ids - prev_ids
            if not new_ids:
                break

            # Filter to ids we don't have
            placeholders = ",".join("?" * len(page_ids))
            existing = {row[0] for row in conn.execute(
                f"SELECT id FROM divisare_projects WHERE id IN ({placeholders})",
                list(page_ids),
            )}
            unknown_ids = page_ids - existing

            if unknown_ids:
                # Use rich parser to upsert lite projects + enqueue
                rich = divisare_parsers.parse_author_built_projects_rich(r.text)
                for proj in rich:
                    if proj.get("id") in unknown_ids:
                        try:
                            divisare_db.upsert_project_lite(proj, arch_id)
                            divisare_db.enqueue_project(
                                f"/projects/{proj['id']}-{proj['slug']}",
                                source_url=author_path,
                            )
                            new_for_this += 1
                        except Exception as e:
                            print(f"  upsert/enqueue failed for {proj.get('id')}: {e}")

            prev_ids |= page_ids
            time.sleep(args.rate)

            # Pagination: stop if page wasn't full + all already known
            if not unknown_ids and len(page_ids) < 11:
                break
            page += 1

        if new_for_this:
            architects_with_new += 1
            total_new_ids += new_for_this
            print(f"[{i}/{len(architects)}] {arch['name'][:35]:<35} "
                  f"+{new_for_this} new (pages_walked={page})")

        # Periodic progress
        if i % 200 == 0:
            elapsed = time.time() - t0
            rate = pages_fetched / elapsed if elapsed > 0 else 0
            eta_min = (len(architects) - i) / (i / max(elapsed, 1)) / 60 if i else 0
            print(f"--- progress: {i}/{len(architects)} architects, "
                  f"{pages_fetched} pages, {rate:.2f} req/s, "
                  f"new_ids={total_new_ids}, eta={eta_min:.0f}min ---")

    elapsed = time.time() - t0
    print(f"\nDONE: {len(architects)} architects, {pages_fetched} page fetches, "
          f"{elapsed/60:.1f} min")
    print(f"  architects with new projects: {architects_with_new}")
    print(f"  new project ids enqueued:     {total_new_ids}")
    print("Next: python3 run.py crawl-divisare --phase projects --limit 10000")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
