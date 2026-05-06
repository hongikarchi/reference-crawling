"""Top-up: revisit the N most prolific architects in our DB and enqueue any
project IDs we don't already have. Then phase_projects can deep-fetch them.

Usage:  python3 -m tools.divisare_topup [--top 100] [--dry-run]
"""
import argparse
import re
import sqlite3
import time

from crawl.divisare import auth


PROJECT_HREF_RE = re.compile(r"/projects/(\d+)-([a-z0-9-]+)")


def walk_built(session, slug: str, max_pages: int = 60) -> dict[int, str]:
    """Return {project_id: slug_part} discovered on /authors/{slug}/projects/built."""
    found: dict[int, str] = {}
    page = 1
    while page <= max_pages:
        url = f"https://divisare.com/authors/{slug}/projects/built"
        if page > 1:
            url += f"?page={page}"
        r = session.get(url, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            break
        page_ids = {int(m.group(1)): m.group(2)
                    for m in PROJECT_HREF_RE.finditer(r.text)}
        new_ids = set(page_ids) - set(found)
        if not new_ids:
            break
        found.update(page_ids)
        time.sleep(1.0)
        page += 1
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100,
                    help="Revisit the top-N most prolific architects (by our count)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Discover only; do not insert into pending_projects")
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

    architects = conn.execute("""
        SELECT a.id, a.slug, a.name, COUNT(p.id) AS our_count
        FROM divisare_architects a
        JOIN divisare_projects p
          ON json_extract(p.architect_ids, '$[0]') = a.id
        GROUP BY a.id
        ORDER BY our_count DESC
        LIMIT ?
    """, (args.top,)).fetchall()

    print(f"Walking {len(architects)} top architects ...")
    print(f"{'#':>4} {'architect':<32} {'our':>6} {'live':>6} {'new':>6}")
    print("-" * 60)

    total_new = 0
    new_to_enqueue: list[tuple[str, str]] = []
    for i, arch in enumerate(architects, 1):
        slug = f"{arch['id']}-{arch['slug']}"
        live = walk_built(session, slug)
        # IDs we already have:
        if live:
            placeholders = ",".join("?" * len(live))
            have = {row[0] for row in conn.execute(
                f"SELECT id FROM divisare_projects WHERE id IN ({placeholders})",
                list(live.keys()),
            )}
        else:
            have = set()
        new_ids = set(live) - have
        for pid in new_ids:
            url = f"/projects/{pid}-{live[pid]}"
            src = f"/authors/{slug}/projects/built"
            new_to_enqueue.append((url, src))
        if new_ids or i <= 5:
            print(f"{i:>4} {arch['name'][:32]:<32} {arch['our_count']:>6} "
                  f"{len(live):>6} {len(new_ids):>6}")
        total_new += len(new_ids)

    print("-" * 60)
    print(f"TOTAL new projects discovered: {total_new}")

    if args.dry_run:
        print("--dry-run: skipping enqueue")
        return

    inserted = 0
    for url, src in new_to_enqueue:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pending_projects (url, source_url, status) "
            "VALUES (?, ?, 'pending')",
            (url, src),
        )
        inserted += cur.rowcount
    conn.commit()
    print(f"Enqueued into pending_projects (deduped via PK): {inserted}")
    print("Next step:")
    print("  python3 run.py crawl-divisare --phase projects --limit 5000")


if __name__ == "__main__":
    main()
