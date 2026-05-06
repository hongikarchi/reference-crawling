"""One-shot diagnostic: how many projects does Divisare actually list per
architect (built / unbuilt) vs how many we have in divisare_projects?

Reads the saved authenticated session — no credentials passed in.
Usage:  python3 -m tools.divisare_gap_check
"""
import re
import sqlite3
import time

from crawl.divisare import auth


def count_projects(session, slug, endpoint, max_pages=60):
    """Walk all pages of /authors/{slug}/projects/{endpoint} and return the
    set of unique project ids found. Stops when a page yields no new ids.
    """
    seen: set[str] = set()
    page = 1
    while page <= max_pages:
        url = f"https://divisare.com/authors/{slug}/projects/{endpoint}"
        if page > 1:
            url += f"?page={page}"
        r = session.get(url, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return seen, r.status_code
        ids = set(re.findall(r"/projects/(\d+)-", r.text))
        new_ids = ids - seen
        if not new_ids:
            return seen, 200
        seen |= ids
        time.sleep(1.0)
        page += 1
    return seen, 200


def main() -> None:
    session = auth.get_authenticated_session()
    session.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36",
    )
    conn = sqlite3.connect("data/crawl/divisare.db")

    targets = [
        (8909,        "foster-partners",            "Foster + Partners"),
        (9099,        "zaha-hadid-architects",      "Zaha Hadid Architects"),
        (8934,        "herzog-de-meuron",           "HERZOG & DE MEURON"),
        (2144621144,  "kengo-kuma-and-associates",  "KENGO KUMA"),
        (63533,       "big-bjarke-ingels-group",    "BIG"),
        (2144618346,  "oma",                        "OMA"),
    ]

    header = (f"{'architect':<28} {'our_db':>8} {'live_built':>11} "
              f"{'live_unbuilt':>13} {'gap_built':>10} {'unbuilt_only':>13}")
    print(header)
    print("-" * len(header))
    total_db = total_built = total_unbuilt_only = 0
    for arch_id, slug_part, display in targets:
        our_count = conn.execute(
            "SELECT COUNT(*) FROM divisare_projects "
            "WHERE json_extract(architect_ids, '$[0]') = ?",
            (arch_id,),
        ).fetchone()[0]
        auth_slug = f"{arch_id}-{slug_part}"
        built, b_st = count_projects(session, auth_slug, "built")
        if b_st != 200:
            print(f"{display[:28]:<28} {our_count:>8} BUILT-HTTP-{b_st}")
            continue
        unbuilt, u_st = count_projects(session, auth_slug, "unbuilt")
        only_unbuilt = unbuilt - built if u_st == 200 else set()
        gap_built = len(built) - our_count
        unbuilt_str = str(len(unbuilt)) if u_st == 200 else f"http-{u_st}"
        print(f"{display[:28]:<28} {our_count:>8} {len(built):>11} "
              f"{unbuilt_str:>13} {gap_built:>+10} {len(only_unbuilt):>13}")
        total_db += our_count
        total_built += len(built)
        total_unbuilt_only += len(only_unbuilt)

    print("-" * len(header))
    print(f"{'TOTAL':<28} {total_db:>8} {total_built:>11} "
          f"{'':>13} {total_built - total_db:>+10} {total_unbuilt_only:>13}")


if __name__ == "__main__":
    main()
