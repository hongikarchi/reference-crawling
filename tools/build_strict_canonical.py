"""Stage F — assemble final canonical_buildings_strict.json from:

- canonical_buildings_4source.json (v5, 38,295 clusters from Stage B)
- d1_results.jsonl                  (Stage D-1 text enrichment, partial OK)
- e1_clusters.jsonl                 (Stage E-1 phash dedup + cluster id)
- e2_image_types.jsonl              (Stage E-2 5-type classification, optional)
- d2_results.jsonl                  (Stage D-2 cover Vision enrichment, optional)
- architects_canonical.json         (Stage A architect canonical clusters)
- 4 source DBs                      (identity fallback: name/city/country/year/architect/cover)

Output: data/canonical/canonical_buildings_strict.json — dict
        {summary, buildings: [<row>]} where each row is a CanonicalBuilding-shaped
        dict with provenance per field.

Tolerant of missing inputs — missing jsonl files just leave their fields None
so the same script works at every stage of pipeline completion (D-1 partial,
E-1 partial, E-2/D-2 not started). Re-run after each stage finishes.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional


CANONICAL_V5     = "data/canonical/canonical_buildings_4source.json"
ARCHITECTS_PATH  = "data/canonical/architects_canonical.json"
METALOCUS_FINAL  = "data/enrich/4_buildings_final.json"
D1_RESULTS       = "data/canonical/d1_results.jsonl"
E1_CLUSTERS      = "data/canonical/e1_clusters.jsonl"
E2_IMAGE_TYPES   = "data/canonical/e2_image_types.jsonl"
D2_RESULTS       = "data/canonical/d2_results.jsonl"
OUTPUT_PATH      = "data/canonical/canonical_buildings_strict.json"
IMAGE_UNAVAILABLE_PATH = ""

SOURCE_DBS = {
    "divisare":   "data/crawl/divisare.db",
    "architizer": "data/crawl/architizer.db",
    "archello":   "data/crawl/archello.db",
    "metalocus":  "data/crawl/metalocus.db",
}

SOURCE_PRIORITY = ("divisare", "architizer", "archello", "metalocus")
_METALOCUS_FINAL_CACHE_PATH: Optional[str] = None
_METALOCUS_FINAL_CACHE: dict[str, dict[str, Any]] | None = None

COUNTRY_ALIASES = {
    "korea, republic of": "South Korea",
    "republic of korea": "South Korea",
    "south korea": "South Korea",
    "u.s.a.": "United States",
    "usa": "United States",
    "us": "United States",
    "united states of america": "United States",
    "united states": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "united kingdom": "United Kingdom",
    "viet nam": "Vietnam",
    "russian federation": "Russia",
    "czech republic": "Czechia",
}


def _load_jsonl_by_cid(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    p = Path(path)
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = d.get("cid") or d.get("canonical_bld_id")
            if cid:
                out[cid] = d
    return out


def _load_cid_manifest(path: str | None) -> set[str]:
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.load(p.open(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(data, dict):
        values = data.get("affected_cids") or data.get("cids") or []
    elif isinstance(data, list):
        values = data
    else:
        values = []
    return {str(cid) for cid in values if str(cid)}


def _parse_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    if isinstance(value, (int, float)):
        return [str(int(value))]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        return [str(v) for v in parsed if str(v)]
    if isinstance(parsed, (str, int, float)):
        return [str(parsed)]
    return [part.strip() for part in text.split(",") if part.strip()]


def _parse_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        return [str(v).strip() for v in parsed if str(v).strip()]
    return [text]


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_country(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    key = " ".join(text.replace("\u00a0", " ").split()).casefold()
    return COUNTRY_ALIASES.get(key, text)


# A value is accepted as a project year only when the whole string IS a year
# (optionally circa-prefixed), a 4-digit year range, or a date. A 4-digit
# number embedded in prose ("1800 homes", "1812 sqm") is rejected -> None: a
# NULL year is honest, a prose-grabbed wrong year is a silent error (audit
# 2026-05). Metalocus feeds free-text here; the other three sources already
# pass structured year values.
_YEAR_TOKEN = r"(?:18|19|20|21)\d{2}"
_YEAR_BARE  = re.compile(rf"^\s*(?:(?:c|ca|circa)\.?\s*)?({_YEAR_TOKEN})\s*$", re.IGNORECASE)
_YEAR_RANGE = re.compile(rf"^\s*({_YEAR_TOKEN})\s*[-–/]\s*({_YEAR_TOKEN})\s*$")
_YEAR_ISO   = re.compile(rf"^\s*({_YEAR_TOKEN})[-/.]\d{{1,2}}(?:[-/.]\d{{1,2}})?\s*$")
_YEAR_DMY   = re.compile(rf"^\s*\d{{1,2}}[-/.]\d{{1,2}}[-/.]({_YEAR_TOKEN})\s*$")


def _parse_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        year = int(value)
        return year if 1800 <= year <= 2199 else None
    text = str(value)
    m = _YEAR_BARE.match(text)
    if m:
        return int(m.group(1))
    m = _YEAR_RANGE.match(text)
    if m:
        return max(int(m.group(1)), int(m.group(2)))
    m = _YEAR_ISO.match(text)
    if m:
        return int(m.group(1))
    m = _YEAR_DMY.match(text)
    if m:
        return int(m.group(1))
    return None


def _build_source_url(src: str, sid: str, meta: dict[str, Any]) -> Optional[str]:
    if meta.get("source_url"):
        return str(meta["source_url"])
    slug = _clean_text(meta.get("slug"))
    if src == "divisare" and slug:
        return f"https://divisare.com/projects/{sid}-{slug}"
    if src == "architizer" and slug:
        return f"https://architizer.com/projects/{slug}/"
    if src == "archello" and slug:
        return f"https://archello.com/project/{slug}"
    if src == "metalocus" and slug:
        return f"https://www.metalocus.es/en/news/{slug}"
    return None


def _metalocus_final_index() -> dict[str, dict[str, Any]]:
    global _METALOCUS_FINAL_CACHE_PATH, _METALOCUS_FINAL_CACHE
    if _METALOCUS_FINAL_CACHE is not None and _METALOCUS_FINAL_CACHE_PATH == METALOCUS_FINAL:
        return _METALOCUS_FINAL_CACHE
    path = Path(METALOCUS_FINAL)
    if not path.exists():
        _METALOCUS_FINAL_CACHE_PATH = METALOCUS_FINAL
        _METALOCUS_FINAL_CACHE = {}
        return _METALOCUS_FINAL_CACHE
    data = json.load(path.open(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("building_id"):
                out[str(row["building_id"])] = row
    _METALOCUS_FINAL_CACHE_PATH = METALOCUS_FINAL
    _METALOCUS_FINAL_CACHE = out
    return out


def _fetch_metalocus_final_meta(sid: str) -> Optional[dict[str, Any]]:
    raw = _metalocus_final_index().get(str(sid))
    if not raw:
        return None
    meta = {
        "name": _clean_text(raw.get("name_en") or raw.get("project_name")),
        "city": _clean_text(raw.get("city")),
        "country": _normalize_country(raw.get("location_country")),
        "year": _parse_year(raw.get("year")),
        "architects": _clean_text(raw.get("architect")),
        "cover": None,
        "architect_source_ids": [],
        "slug": _clean_text(raw.get("slug")),
        "source_url": _clean_text(raw.get("url") or raw.get("source_url")),
    }
    meta["source_url"] = _build_source_url("metalocus", str(sid), meta)
    return meta


def _arch_source_index(arch_data: dict) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for cluster in arch_data.get("clusters", []):
        arch_id = cluster.get("canonical_arch_id")
        if not arch_id:
            continue
        for source, ids in (cluster.get("source_refs") or {}).items():
            for source_id in ids or []:
                out[(str(source), str(source_id))] = str(arch_id)
    return out


def _open_dbs() -> dict[str, sqlite3.Connection]:
    conns: dict[str, sqlite3.Connection] = {}
    for src, path in SOURCE_DBS.items():
        if Path(path).exists():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conns[src] = conn
    return conns


def _fetch_meta(src: str, sid: str, conn: sqlite3.Connection) -> Optional[dict]:
    """Identity fallback per source."""
    queries = {
        "divisare":
            "SELECT name, location_city AS city, location_country AS country, "
            "project_year AS year, architect_names AS architects, cover_image_url AS cover "
            ", architect_ids AS architect_source_ids, slug, NULL AS source_url "
            "FROM divisare_projects WHERE id=?",
        "architizer":
            "SELECT name, location_city AS city, location_country AS country, "
            "completion_year AS year, firm_name AS architects, cover_image_url AS cover "
            ", firm_slug AS architect_source_ids, slug, NULL AS source_url "
            "FROM architizer_projects WHERE id=?",
        "archello":
            "SELECT name, location_city AS city, location_country AS country, "
            "project_year AS year, architect_name AS architects, cover_image_url AS cover "
            ", architect_brand_id AS architect_source_ids, slug, NULL AS source_url "
            "FROM archello_projects WHERE id=?",
        "metalocus":
            "SELECT b.title AS name, b.city AS city, b.country AS country, "
            "b.year AS year, b.architects AS architects, b.cover_image_url AS cover, "
            "NULL AS architect_source_ids, a.slug AS slug, a.url AS source_url "
            "FROM buildings b LEFT JOIN articles a ON a.id = b.article_id WHERE b.id=?",
    }
    q = queries.get(src)
    if not q:
        return None
    try:
        row = conn.execute(q, (str(sid),)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        if src == "metalocus":
            return _fetch_metalocus_final_meta(str(sid))
        return None
    meta = dict(row)
    meta["architect_source_ids"] = _parse_id_list(meta.get("architect_source_ids"))
    meta["city"] = _clean_text(meta.get("city"))
    meta["country"] = _normalize_country(meta.get("country"))
    meta["year"] = _parse_year(meta.get("year"))
    if src == "divisare":
        names = _parse_text_list(meta.get("architects"))
        meta["architects"] = ", ".join(names) if names else None
    else:
        meta["architects"] = _clean_text(meta.get("architects"))
    meta["cover"] = _clean_text(meta.get("cover"))
    meta["source_url"] = _build_source_url(src, str(sid), meta)
    return meta


def _confidence_tier(n_sources: Optional[int]) -> str:
    if n_sources is None:
        return "T3"
    if n_sources >= 3:
        return "T1"
    if n_sources == 2:
        return "T2"
    return "T3"


def _identity_fields(cluster: dict, conns: dict[str, sqlite3.Connection]) -> dict:
    """Pick name/city/country/year/architects/cover from highest-priority source."""
    refs = cluster.get("source_refs") or {}
    out: dict[str, Any] = {
        "name": cluster.get("canonical_name"),
        "location_city": None,
        "location_country": None,
        "project_year": None,
        "architects_text": None,
        "cover_image_url_default": None,
        "identity_source": None,
    }
    for src in SOURCE_PRIORITY:
        ids = refs.get(src) or []
        if not ids or src not in conns:
            continue
        for source_id in ids:
            meta = _fetch_meta(src, str(source_id), conns[src])
            if not meta:
                continue
            out["name"] = out["name"] or meta.get("name")
            out["location_city"] = out["location_city"] or meta.get("city")
            out["location_country"] = out["location_country"] or meta.get("country")
            out["project_year"] = out["project_year"] or meta.get("year")
            out["architects_text"] = out["architects_text"] or meta.get("architects")
            out["cover_image_url_default"] = (
                out["cover_image_url_default"] or meta.get("cover")
            )
            if out["identity_source"] is None:
                out["identity_source"] = src
            if all(v is not None for v in (
                out["name"], out["location_city"], out["location_country"],
                out["project_year"], out["architects_text"],
                out["cover_image_url_default"],
            )):
                break
        if all(v is not None for v in (
            out["name"], out["location_city"], out["location_country"],
            out["project_year"], out["architects_text"],
            out["cover_image_url_default"],
        )):
            break
    return out


def _source_urls_for_cluster(
    cluster: dict,
    conns: dict[str, sqlite3.Connection],
) -> dict[str, list[str]]:
    refs = cluster.get("source_refs") or {}
    out: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for src in SOURCE_PRIORITY:
        ids = refs.get(src) or []
        if not ids or src not in conns:
            continue
        for source_id in ids:
            meta = _fetch_meta(src, str(source_id), conns[src])
            if not meta or not meta.get("source_url"):
                continue
            key = (src, str(meta["source_url"]))
            if key in seen:
                continue
            seen.add(key)
            out.setdefault(src, []).append(str(meta["source_url"]))
    return out


def _display_cover_url(
    *,
    covers_by_type: dict[str, Any],
    cover_image_url_default: Any,
    all_images: list[dict[str, Any]],
) -> Optional[str]:
    if isinstance(covers_by_type, dict):
        exterior = _clean_text(covers_by_type.get("exterior"))
        if exterior:
            return exterior
    default = _clean_text(cover_image_url_default)
    if default:
        return default
    for image in all_images:
        if isinstance(image, dict):
            url = _clean_text(image.get("url"))
            if url:
                return url
    if isinstance(covers_by_type, dict):
        for value in covers_by_type.values():
            url = _clean_text(value)
            if url:
                return url
    return None


def _publishability_reasons(
    *,
    name: Any,
    source_refs: dict[str, Any],
    source_urls: dict[str, Any],
    all_images: list[dict[str, Any]],
    display_cover_url: Optional[str],
) -> list[str]:
    reasons: list[str] = []
    if not _clean_text(name):
        reasons.append("missing_name")
    if not source_refs:
        reasons.append("missing_source_refs")
    if not source_urls:
        reasons.append("missing_source_urls")
    if not all_images:
        reasons.append("missing_all_images")
    if not display_cover_url:
        reasons.append("missing_display_cover_url")
    return reasons


def _architect_ids_for_cluster(
    cluster: dict,
    conns: dict[str, sqlite3.Connection],
    arch_source_to_id: dict[tuple[str, str], str],
) -> list[str]:
    refs = cluster.get("source_refs") or {}
    out: list[str] = []
    seen: set[str] = set()
    for src in SOURCE_PRIORITY:
        ids = refs.get(src) or []
        if not ids or src not in conns:
            continue
        for source_id in ids:
            meta = _fetch_meta(src, str(source_id), conns[src])
            if not meta:
                continue
            for arch_source_id in meta.get("architect_source_ids") or []:
                arch_id = arch_source_to_id.get((src, str(arch_source_id)))
                if arch_id and arch_id not in seen:
                    seen.add(arch_id)
                    out.append(arch_id)
    return out


def build(
    canonical_path: str = CANONICAL_V5,
    output_path: str = OUTPUT_PATH,
    architects_path: str = ARCHITECTS_PATH,
    d1_path: str = D1_RESULTS,
    e1_path: str = E1_CLUSTERS,
    e2_path: str = E2_IMAGE_TYPES,
    d2_path: str = D2_RESULTS,
    image_unavailable_path: str | None = IMAGE_UNAVAILABLE_PATH,
) -> dict:
    canonical = json.load(open(canonical_path))
    clusters = canonical.get("clusters") or canonical.get("buildings") or []

    arch_data = json.load(open(architects_path)) if Path(architects_path).exists() else {"clusters": []}
    arch_by_id = {a["canonical_arch_id"]: a for a in arch_data.get("clusters", [])}
    arch_source_to_id = _arch_source_index(arch_data)

    d1 = _load_jsonl_by_cid(d1_path)
    e1 = _load_jsonl_by_cid(e1_path)
    e2 = _load_jsonl_by_cid(e2_path)
    d2 = _load_jsonl_by_cid(d2_path)
    image_unavailable_cids = _load_cid_manifest(image_unavailable_path)

    conns = _open_dbs()
    out_rows: list[dict] = []
    coverage = {
        "total":   len(clusters),
        "with_d1": 0, "with_e1": 0, "with_e2": 0, "with_d2": 0,
        "with_identity_name": 0,
        "with_source_urls": 0,
        "with_display_cover_url": 0,
        "publishable": 0,
        "nonpublishable": 0,
        "needs_image_derived_backfill": 0,
        "by_tier": {"T1": 0, "T2": 0, "T3": 0},
    }
    try:
        for cluster in clusters:
            cid = cluster.get("canonical_bld_id")
            if not cid:
                continue

            ident = _identity_fields(cluster, conns)
            if ident.get("name"):
                coverage["with_identity_name"] += 1

            arch_ids = _architect_ids_for_cluster(cluster, conns, arch_source_to_id)
            arch_names = [
                (arch_by_id.get(a) or {}).get("canonical_name")
                for a in arch_ids
            ]
            arch_names = [n for n in arch_names if n]
            source_urls = _source_urls_for_cluster(cluster, conns)
            if source_urls:
                coverage["with_source_urls"] += 1

            d1_row = d1.get(cid) or {}
            e1_row = e1.get(cid) or {}
            e2_row = e2.get(cid) or {}
            d2_row = d2.get(cid) or {}
            if d1_row: coverage["with_d1"] += 1
            if e1_row: coverage["with_e1"] += 1
            if e2_row: coverage["with_e2"] += 1
            if d2_row: coverage["with_d2"] += 1

            tier = _confidence_tier(cluster.get("n_sources"))
            coverage["by_tier"][tier] += 1
            all_images = e1_row.get("all_images") or []
            covers_by_type = e2_row.get("covers_by_type") or {}
            image_derived = {
                "style":              d2_row.get("style_image"),
                "color_tone":         d2_row.get("color_tone_image"),
                "material_visual":    d2_row.get("material_visual_image"),
                "visual_description": d2_row.get("visual_description_image"),
            } if d2_row else {}
            display_cover_url = _display_cover_url(
                covers_by_type=covers_by_type,
                cover_image_url_default=ident.get("cover_image_url_default"),
                all_images=all_images,
            )
            publishability_reasons = _publishability_reasons(
                name=ident.get("name"),
                source_refs=cluster.get("source_refs") or {},
                source_urls=source_urls,
                all_images=all_images,
                display_cover_url=display_cover_url,
            )
            if cid in image_unavailable_cids and "image_unavailable" not in publishability_reasons:
                publishability_reasons.append("image_unavailable")
            is_publishable = not publishability_reasons
            needs_image_derived_backfill = bool(
                is_publishable and display_cover_url and not (image_derived or {}).get("style")
            )
            if display_cover_url:
                coverage["with_display_cover_url"] += 1
            if publishability_reasons:
                coverage["nonpublishable"] += 1
            else:
                coverage["publishable"] += 1
            if needs_image_derived_backfill:
                coverage["needs_image_derived_backfill"] += 1

            row = {
                "canonical_bld_id":       cid,
                "name":                   ident.get("name"),
                "names_alts":             cluster.get("names") or [],
                "architect_canonical_ids": arch_ids,
                "architect_names":        arch_names,
                "architects_text":        ident.get("architects_text"),
                "location_city":          ident.get("location_city"),
                "location_country":       ident.get("location_country"),
                "project_year":           ident.get("project_year"),
                "n_sources":              cluster.get("n_sources"),
                "source_refs":            cluster.get("source_refs") or {},
                "source_urls":            source_urls,
                "identity_source":        ident.get("identity_source"),
                "confidence_tier":        tier,
                # text enrichment (D-1)
                "program":                d1_row.get("program"),
                "style":                  d1_row.get("style"),
                "color_tone":             d1_row.get("color_tone"),
                "atmosphere":             d1_row.get("atmosphere"),
                "material_visual":        d1_row.get("material_visual"),
                "visual_description":     d1_row.get("visual_description"),
                # image cluster (E-1)
                "all_images":             all_images,
                "best_image_per_cluster": e1_row.get("best_image_per_cluster") or {},
                # image type covers (E-2)
                "covers_by_type":         covers_by_type,
                # vision enrichment (D-2)
                "image_derived":          image_derived,
                "cover_image_url_default": ident.get("cover_image_url_default"),
                "display_cover_url":      display_cover_url,
                "is_publishable":         is_publishable,
                "publishability_reasons": publishability_reasons,
                "needs_image_derived_backfill": needs_image_derived_backfill,
            }
            out_rows.append(row)
    finally:
        for c in conns.values():
            c.close()

    summary = {
        "n_buildings":    len(out_rows),
        "source_summary": canonical.get("summary"),
        "coverage":       coverage,
    }
    payload = {"summary": summary, "buildings": out_rows}
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--canonical", default=CANONICAL_V5)
    ap.add_argument("--output", default=OUTPUT_PATH)
    ap.add_argument("--architects", default=ARCHITECTS_PATH)
    ap.add_argument("--d1", default=D1_RESULTS)
    ap.add_argument("--e1", default=E1_CLUSTERS)
    ap.add_argument("--e2", default=E2_IMAGE_TYPES)
    ap.add_argument("--d2", default=D2_RESULTS)
    ap.add_argument("--image-unavailable", default=IMAGE_UNAVAILABLE_PATH)
    args = ap.parse_args()

    summary = build(
        canonical_path=args.canonical,
        output_path=args.output,
        architects_path=args.architects,
        d1_path=args.d1,
        e1_path=args.e1,
        e2_path=args.e2,
        d2_path=args.d2,
        image_unavailable_path=args.image_unavailable,
    )
    print(json.dumps(summary, indent=2))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
