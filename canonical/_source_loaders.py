"""Per-source architect loaders for 4-source canonical match.

Each loader reads its source's SQLite (or JSON) and returns a list of
uniform dicts:

    {
      "name":          str,        # display name
      "source":        str,        # 'metalocus' | 'divisare' | 'architizer' | 'archello'
      "source_id":     str,        # source-native stable ID (str-cast)
      "country":       str | None, # HQ country
      "project_count": int | None, # how many projects this architect has on this source
    }

Output is intentionally minimal — match_architects.py does the cross-source
fuzzy comparison + Haiku tiebreaker on top of these.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Iterator, Optional


METALOCUS_CLUSTERS = "data/canonical/metalocus_architect_clusters.json"
DIVISARE_DB        = "data/crawl/divisare.db"
ARCHITIZER_DB      = "data/crawl/architizer.db"
ARCHELLO_DB        = "data/crawl/archello.db"


# Country-name strings that look like a person's name (Divisare parser bug).
# If a country field matches these signatures, treat as None.
_PERSON_NAME_RE = re.compile(r"^[A-ZÄÖÜÉÈÀÂ][a-zäöüéèàâ]+(\s[A-ZÄÖÜÉÈÀÂ][a-zäöüéèàâ]+)+$")
_KNOWN_COUNTRIES = {
    "united kingdom", "united states", "germany", "france", "italy",
    "spain", "japan", "china", "korea", "south korea", "netherlands",
    "switzerland", "austria", "belgium", "denmark", "sweden", "norway",
    "poland", "portugal", "russia", "russian federation", "brazil",
    "mexico", "australia", "canada", "india", "turkey", "greece",
    "ireland", "finland", "czech republic", "czechia", "hungary",
    "argentina", "chile", "colombia", "uruguay", "ecuador", "peru",
    "vietnam", "thailand", "indonesia", "philippines", "malaysia",
    "singapore", "hong kong", "taiwan", "uae", "united arab emirates",
    "saudi arabia", "qatar", "egypt", "south africa", "morocco",
    "new zealand", "iran", "iraq", "lebanon", "israel",
}


def _clean_country(s: Optional[str]) -> Optional[str]:
    """Drop person-name-shaped values; return canonicalized country or None."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    low = s.lower()
    if low in _KNOWN_COUNTRIES:
        return s
    # If it looks like "First Last" but isn't a known country, drop it
    if _PERSON_NAME_RE.match(s) and low not in _KNOWN_COUNTRIES:
        return None
    return s


def load_metalocus() -> Iterator[dict]:
    with open(METALOCUS_CLUSTERS, encoding="utf-8") as f:
        data = json.load(f)
    for c in data.get("clusters", []):
        yield {
            "name":          c["canonical_name"],
            "source":        "metalocus",
            "source_id":     c["canonical_id"],
            "country":       None,  # metalocus has no architect-level country
            "project_count": len(c.get("building_ids") or []),
        }


def load_divisare() -> Iterator[dict]:
    conn = sqlite3.connect(DIVISARE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, country, project_count_seen "
        "FROM divisare_architects WHERE name IS NOT NULL AND name != ''"
    ).fetchall()
    for r in rows:
        yield {
            "name":          r["name"],
            "source":        "divisare",
            "source_id":     str(r["id"]),
            "country":       _clean_country(r["country"]),
            "project_count": r["project_count_seen"],
        }
    conn.close()


def load_architizer() -> Iterator[dict]:
    """Yields architects from architizer_firms (sitemap master) PLUS any
    firm_slugs found in architizer_projects that are missing from the firms
    table (post-sitemap project crawl picks up new firms not on the original
    /firms/ index)."""
    conn = sqlite3.connect(ARCHITIZER_DB)
    conn.row_factory = sqlite3.Row
    # Backfill: firm-level country = mode of project countries
    backfill: dict[str, str] = {}
    for row in conn.execute(
        "SELECT firm_slug, location_country FROM ("
        "  SELECT firm_slug, location_country, COUNT(*) AS n,"
        "    ROW_NUMBER() OVER (PARTITION BY firm_slug ORDER BY COUNT(*) DESC) AS rk"
        "  FROM architizer_projects"
        "  WHERE firm_slug IS NOT NULL AND firm_slug != ''"
        "    AND location_country IS NOT NULL AND location_country != ''"
        "  GROUP BY firm_slug, location_country"
        ") WHERE rk = 1"
    ):
        backfill[row[0]] = row[1]

    seen_slugs: set[str] = set()
    rows = conn.execute(
        "SELECT slug, name, office_locations, project_count_seen "
        "FROM architizer_firms WHERE name IS NOT NULL AND name != ''"
    ).fetchall()
    for r in rows:
        country = None
        try:
            offices = json.loads(r["office_locations"] or "[]")
            for o in offices:
                if isinstance(o, dict) and o.get("country"):
                    country = _clean_country(o["country"])
                    if country:
                        break
        except (json.JSONDecodeError, TypeError):
            pass
        if not country:
            country = _clean_country(backfill.get(r["slug"]))
        seen_slugs.add(r["slug"])
        yield {
            "name":          r["name"],
            "source":        "architizer",
            "source_id":     r["slug"],
            "country":       country,
            "project_count": r["project_count_seen"],
        }

    # Firms discovered through projects but absent from the firms table.
    # firm_name + most-common project country + project count provide the
    # same shape; tiebreak handles dup detection against existing canonicals.
    extra = conn.execute(
        "SELECT firm_slug, MAX(firm_name) AS firm_name, "
        "       COUNT(*) AS pcount, MAX(location_country) AS country "
        "FROM architizer_projects "
        "WHERE firm_slug IS NOT NULL AND firm_slug != '' "
        "  AND firm_name IS NOT NULL AND firm_name != '' "
        "GROUP BY firm_slug"
    ).fetchall()
    for r in extra:
        if r["firm_slug"] in seen_slugs:
            continue
        yield {
            "name":          r["firm_name"],
            "source":        "architizer",
            "source_id":     r["firm_slug"],
            "country":       _clean_country(backfill.get(r["firm_slug"]) or r["country"]),
            "project_count": r["pcount"],
        }
    conn.close()


def load_archello() -> Iterator[dict]:
    """Yields architects from archello_firms (sitemap master) PLUS any
    architect_brand_ids found in archello_projects that are missing from
    the firms table (post-sitemap project crawl picks up many small firms
    not on the original /brands/architects/ index)."""
    conn = sqlite3.connect(ARCHELLO_DB)
    conn.row_factory = sqlite3.Row

    seen_ids: set[str] = set()
    rows = conn.execute(
        "SELECT slug, brand_id, name, location_country, project_count_archello "
        "FROM archello_firms WHERE name IS NOT NULL AND name != ''"
    ).fetchall()
    for r in rows:
        sid = str(r["brand_id"]) if r["brand_id"] else r["slug"]
        seen_ids.add(sid)
        yield {
            "name":          r["name"],
            "source":        "archello",
            "source_id":     sid,
            "country":       _clean_country(r["location_country"]),
            "project_count": r["project_count_archello"],
        }

    # Brand-IDs discovered through projects but absent from the firms table.
    extra = conn.execute(
        "SELECT architect_brand_id, MAX(architect_name) AS name, "
        "       COUNT(*) AS pcount, MAX(location_country) AS country "
        "FROM archello_projects "
        "WHERE architect_brand_id IS NOT NULL "
        "  AND architect_name IS NOT NULL AND architect_name != '' "
        "GROUP BY architect_brand_id"
    ).fetchall()
    for r in extra:
        sid = str(r["architect_brand_id"])
        if sid in seen_ids:
            continue
        yield {
            "name":          r["name"],
            "source":        "archello",
            "source_id":     sid,
            "country":       _clean_country(r["country"]),
            "project_count": r["pcount"],
        }
    conn.close()


def load_all() -> list[dict]:
    """Concatenate all 4 sources into a single pool."""
    pool: list[dict] = []
    pool.extend(load_metalocus())
    pool.extend(load_divisare())
    pool.extend(load_architizer())
    pool.extend(load_archello())
    return pool


if __name__ == "__main__":
    # Smoke test
    by_source: dict[str, int] = {}
    countries: dict[str, int] = {}
    pool = load_all()
    for a in pool:
        by_source[a["source"]] = by_source.get(a["source"], 0) + 1
        if a["country"]:
            countries[a["source"]] = countries.get(a["source"], 0) + 1
    print(f"TOTAL architects loaded: {len(pool)}")
    for src in ("metalocus", "divisare", "architizer", "archello"):
        n = by_source.get(src, 0)
        with_country = countries.get(src, 0)
        print(f"  {src:<12} {n:>6}  (with country: {with_country})")
    # Print 2 samples from each source
    for src in ("metalocus", "divisare", "architizer", "archello"):
        samples = [a for a in pool if a["source"] == src][:2]
        print(f"\n  {src} samples:")
        for s in samples:
            print(f"    {s}")
