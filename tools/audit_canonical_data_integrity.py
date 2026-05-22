#!/usr/bin/env python3
"""Read-only end-to-end data integrity audit for the canonical DB.

This catches gaps that normal schema validators miss:
  - source_refs pointing to missing source DB rows
  - source fields present but missing in final strict rows
  - source architect refs present but not resolved to canonical architect IDs
  - source image URLs present but missing in final image/cover fields
  - accidental JSON-array strings such as architects_text == "[]"
  - duplicate source project refs across canonical building rows
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STRICT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.resume10_complete.json"
DEFAULT_EMBEDDED = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json"
DEFAULT_ARCHITECTS = ROOT / "data/canonical/architects_canonical.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_data_integrity_audit.json"
METALOCUS_FINAL = ROOT / "data/enrich/4_buildings_final.json"

SOURCE_DBS = {
    "divisare": ROOT / "data/crawl/divisare.db",
    "architizer": ROOT / "data/crawl/architizer.db",
    "archello": ROOT / "data/crawl/archello.db",
    "metalocus": ROOT / "data/crawl/metalocus.db",
}

COUNTRY_ALIASES = {
    "korea, republic of": "south korea",
    "republic of korea": "south korea",
    "usa": "united states",
    "u.s.a.": "united states",
    "us": "united states",
    "uk": "united kingdom",
    "u.k.": "united kingdom",
    "russian federation": "russia",
    "czech republic": "czechia",
    "viet nam": "vietnam",
}


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def norm(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    return " ".join(text.replace("\u00a0", " ").casefold().split())


def norm_country(value: Any) -> str:
    return COUNTRY_ALIASES.get(norm(value), norm(value))


def parse_year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        year = value
    elif isinstance(value, float):
        year = int(value)
    else:
        match = re.search(r"\b(18|19|20|21)\d{2}\b", str(value))
        if not match:
            return None
        year = int(match.group(0))
    return year if 1800 <= year <= 2199 else None


def years_in_text(value: Any) -> list[int]:
    text = clean(value)
    if not text:
        return []
    years: list[int] = []
    for match in re.finditer(r"\b(18|19|20|21)\d{2}\b", text):
        year = int(match.group(0))
        if 1800 <= year <= 2199 and year not in years:
            years.append(year)
    return years


def slugify_text(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def source_slug_prefix_before_project(meta: dict[str, Any]) -> str | None:
    slug = clean(meta.get("slug"))
    name_slug = slugify_text(meta.get("name"))
    if not slug or not name_slug:
        return None
    if slug == name_slug:
        return None
    if slug.endswith("-" + name_slug):
        prefix = slug[: -(len(name_slug) + 1)].strip("-")
        return prefix or None
    return None


def parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [text]
    if isinstance(parsed, list):
        return [v for v in parsed if v not in (None, "")]
    if parsed in (None, ""):
        return []
    return [parsed]


def list_text(value: Any) -> str | None:
    items = [str(v).strip() for v in parse_json_list(value) if str(v).strip()]
    if items:
        return ", ".join(items)
    text = clean(value)
    return None if text in {"[]", "{}"} else text


def source_url(source: str, source_id: str, slug: Any, url: Any = None) -> str | None:
    direct = clean(url)
    if direct:
        return direct
    slug_text = clean(slug)
    if not slug_text:
        return None
    if source == "divisare":
        return f"https://divisare.com/projects/{source_id}-{slug_text}"
    if source == "architizer":
        return f"https://architizer.com/projects/{slug_text}/"
    if source == "archello":
        return f"https://archello.com/project/{slug_text}"
    if source == "metalocus":
        return f"https://www.metalocus.es/en/news/{slug_text}"
    return None


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def image_list(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, list):
            out.extend(str(v).strip() for v in value if str(v).strip())
        else:
            out.extend(str(v).strip() for v in parse_json_list(value) if str(v).strip())
    return dedupe(out)


def load_source_meta() -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Counter]]:
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    source_stats: dict[str, Counter] = defaultdict(Counter)

    if SOURCE_DBS["divisare"].exists():
        conn = sqlite3.connect(SOURCE_DBS["divisare"])
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                "SELECT id, slug, name, architect_ids, architect_names, "
                "location_country, location_city, project_year, description, abstract, "
                "cover_image_url, gallery_urls FROM divisare_projects"
            ):
                source = "divisare"
                sid = str(row["id"])
                arch_ids = [str(v) for v in parse_json_list(row["architect_ids"])]
                arch_text = list_text(row["architect_names"])
                images = image_list(row["cover_image_url"], row["gallery_urls"])
                meta[(source, sid)] = {
                    "source": source,
                    "source_id": sid,
                    "name": clean(row["name"]),
                    "slug": clean(row["slug"]),
                    "city": clean(row["location_city"]),
                    "country": clean(row["location_country"]),
                    "year": parse_year(row["project_year"]),
                    "architect_source_ids": arch_ids,
                    "architects_text": arch_text,
                    "description": clean(row["description"] or row["abstract"]),
                    "cover": clean(row["cover_image_url"]),
                    "images": images,
                    "source_url": source_url(source, sid, row["slug"]),
                }
                source_stats[source]["total"] += 1
                if not arch_ids:
                    source_stats[source]["missing_architect_source_ids"] += 1
                if not arch_text:
                    source_stats[source]["missing_architect_text"] += 1
                if not clean(row["location_country"]):
                    source_stats[source]["missing_country"] += 1
                if not clean(row["location_city"]):
                    source_stats[source]["missing_city"] += 1
                if parse_year(row["project_year"]) is None:
                    source_stats[source]["missing_year"] += 1
                if not images:
                    source_stats[source]["missing_images"] += 1
                if not clean(row["description"] or row["abstract"]):
                    source_stats[source]["missing_description"] += 1
        finally:
            conn.close()

    if SOURCE_DBS["architizer"].exists():
        conn = sqlite3.connect(SOURCE_DBS["architizer"])
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                "SELECT id, slug, name, firm_slug, firm_name, description, description_short, "
                "completion_year, location_full, location_country, location_city, "
                "cover_image_url, gallery_image_urls FROM architizer_projects"
            ):
                source = "architizer"
                sid = str(row["id"])
                arch_ids = [str(row["firm_slug"])] if clean(row["firm_slug"]) else []
                images = image_list(row["cover_image_url"], row["gallery_image_urls"])
                meta[(source, sid)] = {
                    "source": source,
                    "source_id": sid,
                    "name": clean(row["name"]),
                    "slug": clean(row["slug"]),
                    "city": clean(row["location_city"]),
                    "country": clean(row["location_country"]),
                    "year": parse_year(row["completion_year"]),
                    "architect_source_ids": arch_ids,
                    "architects_text": clean(row["firm_name"]),
                    "description": clean(row["description"] or row["description_short"]),
                    "cover": clean(row["cover_image_url"]),
                    "images": images,
                    "source_url": source_url(source, sid, row["slug"]),
                    "location_full": clean(row["location_full"]),
                }
                source_stats[source]["total"] += 1
                if not arch_ids:
                    source_stats[source]["missing_architect_source_ids"] += 1
                if not clean(row["firm_name"]):
                    source_stats[source]["missing_architect_text"] += 1
                if not clean(row["location_country"]):
                    source_stats[source]["missing_country"] += 1
                    if clean(row["location_full"]):
                        source_stats[source]["missing_country_but_location_full_present"] += 1
                if not clean(row["location_city"]):
                    source_stats[source]["missing_city"] += 1
                    if clean(row["location_full"]):
                        source_stats[source]["missing_city_but_location_full_present"] += 1
                if parse_year(row["completion_year"]) is None:
                    source_stats[source]["missing_year"] += 1
                if not images:
                    source_stats[source]["missing_images"] += 1
                if not clean(row["description"] or row["description_short"]):
                    source_stats[source]["missing_description"] += 1
        finally:
            conn.close()

    if SOURCE_DBS["archello"].exists():
        conn = sqlite3.connect(SOURCE_DBS["archello"])
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                "SELECT id, slug, name, architect_brand_id, architect_name, location_full, "
                "location_country, location_city, project_year, description, "
                "cover_image_url, gallery_image_urls FROM archello_projects"
            ):
                source = "archello"
                sid = str(row["id"])
                arch_ids = [str(row["architect_brand_id"])] if row["architect_brand_id"] else []
                images = image_list(row["cover_image_url"], row["gallery_image_urls"])
                meta[(source, sid)] = {
                    "source": source,
                    "source_id": sid,
                    "name": clean(row["name"]),
                    "slug": clean(row["slug"]),
                    "city": clean(row["location_city"]),
                    "country": clean(row["location_country"]),
                    "year": parse_year(row["project_year"]),
                    "architect_source_ids": arch_ids,
                    "architects_text": clean(row["architect_name"]),
                    "description": clean(row["description"]),
                    "cover": clean(row["cover_image_url"]),
                    "images": images,
                    "source_url": source_url(source, sid, row["slug"]),
                    "location_full": clean(row["location_full"]),
                }
                source_stats[source]["total"] += 1
                if not arch_ids:
                    source_stats[source]["missing_architect_source_ids"] += 1
                if not clean(row["architect_name"]):
                    source_stats[source]["missing_architect_text"] += 1
                if not clean(row["location_country"]):
                    source_stats[source]["missing_country"] += 1
                    if clean(row["location_full"]):
                        source_stats[source]["missing_country_but_location_full_present"] += 1
                if not clean(row["location_city"]):
                    source_stats[source]["missing_city"] += 1
                    if clean(row["location_full"]):
                        source_stats[source]["missing_city_but_location_full_present"] += 1
                if parse_year(row["project_year"]) is None:
                    source_stats[source]["missing_year"] += 1
                if not images:
                    source_stats[source]["missing_images"] += 1
                if not clean(row["description"]):
                    source_stats[source]["missing_description"] += 1
        finally:
            conn.close()

    if SOURCE_DBS["metalocus"].exists():
        conn = sqlite3.connect(SOURCE_DBS["metalocus"])
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                "SELECT b.id, b.title, b.architects, b.location, b.city, b.country, b.year, "
                "b.description, b.cover_image_url, b.gallery_image_urls, b.drawing_image_urls, "
                "a.slug, a.url FROM buildings b LEFT JOIN articles a ON a.id=b.article_id"
            ):
                source = "metalocus"
                sid = str(row["id"])
                images = image_list(row["cover_image_url"], row["gallery_image_urls"], row["drawing_image_urls"])
                meta[(source, sid)] = {
                    "source": source,
                    "source_id": sid,
                    "name": clean(row["title"]),
                    "slug": clean(row["slug"]),
                    "city": clean(row["city"]),
                    "country": clean(row["country"]),
                    "year": parse_year(row["year"]),
                    "architect_source_ids": [],
                    "architects_text": clean(row["architects"]),
                    "description": clean(row["description"]),
                    "cover": clean(row["cover_image_url"]),
                    "images": images,
                    "source_url": source_url(source, sid, row["slug"], row["url"]),
                    "location_full": clean(row["location"]),
                }
                source_stats[source]["total"] += 1
                if not clean(row["architects"]):
                    source_stats[source]["missing_architect_text"] += 1
                if not clean(row["country"]):
                    source_stats[source]["missing_country"] += 1
                    if clean(row["location"]):
                        source_stats[source]["missing_country_but_location_full_present"] += 1
                if not clean(row["city"]):
                    source_stats[source]["missing_city"] += 1
                    if clean(row["location"]):
                        source_stats[source]["missing_city_but_location_full_present"] += 1
                if parse_year(row["year"]) is None:
                    source_stats[source]["missing_year"] += 1
                if not images:
                    source_stats[source]["missing_images"] += 1
                if not clean(row["description"]):
                    source_stats[source]["missing_description"] += 1
        finally:
            conn.close()

    if METALOCUS_FINAL.exists():
        data = json.load(METALOCUS_FINAL.open(encoding="utf-8"))
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict) or not row.get("building_id"):
                    continue
                key = ("metalocus", str(row["building_id"]))
                meta.setdefault(
                    key,
                    {
                        "source": "metalocus",
                        "source_id": str(row["building_id"]),
                        "name": clean(row.get("name_en") or row.get("project_name")),
                        "slug": clean(row.get("slug")),
                        "city": clean(row.get("city")),
                        "country": clean(row.get("location_country")),
                        "year": parse_year(row.get("year")),
                        "architect_source_ids": [],
                        "architects_text": clean(row.get("architect")),
                        "description": clean(row.get("description")),
                        "cover": clean(row.get("cover_image_url")),
                        "images": image_list(row.get("cover_image_url")),
                        "source_url": clean(row.get("url") or row.get("source_url")),
                    },
                )

    return meta, source_stats


def load_arch_index(path: Path) -> tuple[dict[tuple[str, str], str], set[str]]:
    data = json.load(path.open(encoding="utf-8"))
    source_to_arch: dict[tuple[str, str], str] = {}
    canonical_ids: set[str] = set()
    for cluster in data.get("clusters") or []:
        aid = str(cluster.get("canonical_arch_id") or "")
        if not aid:
            continue
        canonical_ids.add(aid)
        for source, ids in (cluster.get("source_refs") or {}).items():
            for sid in ids or []:
                source_to_arch[(str(source), str(sid))] = aid
    return source_to_arch, canonical_ids


def add_example(bucket: dict[str, list[dict[str, Any]]], key: str, row: dict[str, Any], extra: dict[str, Any]) -> None:
    examples = bucket.setdefault(key, [])
    if len(examples) >= 20:
        return
    examples.append(
        {
            "canonical_bld_id": row.get("canonical_bld_id"),
            "name": row.get("name"),
            "source_refs": row.get("source_refs"),
            **extra,
        }
    )


def audit_strict(
    strict_path: Path,
    source_meta: dict[tuple[str, str], dict[str, Any]],
    arch_source_to_id: dict[tuple[str, str], str],
    canonical_arch_ids: set[str],
) -> dict[str, Any]:
    data = json.load(strict_path.open(encoding="utf-8"))
    rows = data.get("buildings") or []

    counts: Counter = Counter()
    by_source: dict[str, Counter] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = {}
    seen_cids: set[str] = set()
    seen_source_refs: dict[tuple[str, str], str] = {}

    identity_fields = {
        "location_country": ("country", norm_country),
        "location_city": ("city", norm),
        "project_year": ("year", lambda v: str(parse_year(v) or "")),
        "architects_text": ("architects_text", norm),
    }

    for row in rows:
        counts["total_rows"] += 1
        cid = str(row.get("canonical_bld_id") or "")
        if not cid:
            counts["missing_canonical_bld_id"] += 1
        elif cid in seen_cids:
            counts["duplicate_canonical_bld_id"] += 1
            add_example(examples, "duplicate_canonical_bld_id", row, {})
        else:
            seen_cids.add(cid)

        refs = row.get("source_refs") or {}
        members: list[dict[str, Any]] = []
        for source, ids in refs.items():
            for sid in ids or []:
                key = (str(source), str(sid))
                by_source[str(source)]["source_refs"] += 1
                if key in seen_source_refs and seen_source_refs[key] != cid:
                    counts["source_project_ref_used_by_multiple_canonicals"] += 1
                    add_example(
                        examples,
                        "source_project_ref_used_by_multiple_canonicals",
                        row,
                        {"source_ref": f"{key[0]}:{key[1]}", "first_cid": seen_source_refs[key]},
                    )
                else:
                    seen_source_refs[key] = cid
                meta = source_meta.get(key)
                if not meta:
                    counts["source_ref_missing_in_source_db"] += 1
                    by_source[str(source)]["source_ref_missing_in_source_db"] += 1
                    add_example(examples, "source_ref_missing_in_source_db", row, {"source_ref": f"{key[0]}:{key[1]}"})
                    continue
                members.append(meta)

                source_urls = row.get("source_urls") or {}
                if ids and not source_urls.get(str(source)):
                    counts["source_urls_missing_for_source_ref"] += 1
                    by_source[str(source)]["source_urls_missing_for_source_ref"] += 1
                    add_example(examples, "source_urls_missing_for_source_ref", row, {"source": source})

        for field, (meta_field, normalizer) in identity_fields.items():
            final_value = row.get(field)
            if final_value:
                continue
            source_values = [
                meta.get(meta_field)
                for meta in members
                if meta.get(meta_field) not in (None, "", [])
            ]
            if source_values:
                counts[f"{field}_missing_but_source_has"] += 1
                for meta in members:
                    if meta.get(meta_field) not in (None, "", []):
                        by_source[meta["source"]][f"{field}_missing_but_source_has"] += 1
                add_example(
                    examples,
                    f"{field}_missing_but_source_has",
                    row,
                    {"source_values": [str(v) for v in source_values[:8]]},
                )

            if field in {"location_country", "location_city"} and not source_values:
                raw_locations = [
                    meta.get("location_full")
                    for meta in members
                    if meta.get("location_full")
                ]
                if raw_locations:
                    counts[f"{field}_missing_but_source_location_full_present"] += 1
                    for meta in members:
                        if meta.get("location_full"):
                            by_source[meta["source"]][f"{field}_missing_but_source_location_full_present"] += 1
                    add_example(
                        examples,
                        f"{field}_missing_but_source_location_full_present",
                        row,
                        {"source_location_full": [str(v) for v in raw_locations[:8]]},
                    )

            if field == "project_year" and not source_values:
                year_candidates = dedupe(
                    [
                        str(year)
                        for meta in members
                        for year in years_in_text(meta.get("description"))
                    ]
                )
                if year_candidates:
                    counts["project_year_missing_but_source_description_has_year_candidate"] += 1
                    for meta in members:
                        if years_in_text(meta.get("description")):
                            by_source[meta["source"]]["project_year_missing_but_source_description_has_year_candidate"] += 1
                    add_example(
                        examples,
                        "project_year_missing_but_source_description_has_year_candidate",
                        row,
                        {"year_candidates": year_candidates[:8]},
                    )

            if field in {"location_country", "location_city", "project_year"}:
                final_norm = normalizer(final_value)
                source_norms = sorted({normalizer(v) for v in source_values if normalizer(v)})
                if final_norm and source_norms and final_norm not in source_norms:
                    counts[f"{field}_final_differs_from_all_sources"] += 1
                    add_example(
                        examples,
                        f"{field}_final_differs_from_all_sources",
                        row,
                        {"final": final_value, "source_values": [str(v) for v in source_values[:8]]},
                    )

        arch_ids = [str(v) for v in row.get("architect_canonical_ids") or [] if str(v)]
        if not arch_ids:
            counts["architect_canonical_ids_missing"] += 1
            source_arch_refs = [
                (meta["source"], arch_sid)
                for meta in members
                for arch_sid in (meta.get("architect_source_ids") or [])
            ]
            source_arch_texts = [meta.get("architects_text") for meta in members if meta.get("architects_text")]
            resolved = [
                arch_source_to_id[(source, str(arch_sid))]
                for source, arch_sid in source_arch_refs
                if (source, str(arch_sid)) in arch_source_to_id
            ]
            if resolved:
                counts["architect_ids_missing_but_source_refs_resolve"] += 1
                add_example(
                    examples,
                    "architect_ids_missing_but_source_refs_resolve",
                    row,
                    {"resolved_architect_ids": sorted(set(resolved))},
                )
            elif source_arch_refs:
                counts["architect_ids_missing_source_refs_unclustered"] += 1
                add_example(
                    examples,
                    "architect_ids_missing_source_refs_unclustered",
                    row,
                    {"source_arch_refs": [f"{s}:{sid}" for s, sid in source_arch_refs[:8]]},
                )
            elif source_arch_texts:
                counts["architect_ids_missing_source_text_only"] += 1
                add_example(
                    examples,
                    "architect_ids_missing_source_text_only",
                    row,
                    {"source_architect_texts": source_arch_texts[:8]},
                )
            else:
                counts["architect_ids_missing_no_source_arch_data"] += 1
                for meta in members:
                    by_source[meta["source"]]["architect_ids_missing_no_source_arch_data"] += 1
                slug_prefixes = [
                    prefix
                    for meta in members
                    for prefix in [source_slug_prefix_before_project(meta)]
                    if prefix
                ]
                if slug_prefixes:
                    counts["architect_ids_missing_slug_prefix_candidate"] += 1
                    for meta in members:
                        if source_slug_prefix_before_project(meta):
                            by_source[meta["source"]]["architect_ids_missing_slug_prefix_candidate"] += 1
                add_example(
                    examples,
                    "architect_ids_missing_no_source_arch_data",
                    row,
                    {
                        "slug_prefix_candidates": slug_prefixes[:8],
                        "source_members": [
                            {
                                "source": meta.get("source"),
                                "source_id": meta.get("source_id"),
                                "name": meta.get("name"),
                                "slug": meta.get("slug"),
                            }
                            for meta in members[:8]
                        ]
                    },
                )
        else:
            missing_arch_ids = [aid for aid in arch_ids if aid not in canonical_arch_ids]
            if missing_arch_ids:
                counts["architect_canonical_ids_not_in_arch_table"] += 1
                add_example(
                    examples,
                    "architect_canonical_ids_not_in_arch_table",
                    row,
                    {"bad_architect_ids": missing_arch_ids[:8]},
                )

        architects_text = row.get("architects_text")
        if (
            isinstance(architects_text, str)
            and (
                architects_text.strip() == "[]"
                or architects_text.strip().startswith('["')
                or architects_text.strip().startswith("['")
            )
        ):
            counts["architects_text_json_string_smell"] += 1
            add_example(examples, "architects_text_json_string_smell", row, {"architects_text": row.get("architects_text")})

        source_images = dedupe([url for meta in members for url in (meta.get("images") or [])])
        source_covers = dedupe([meta["cover"] for meta in members if meta.get("cover")])
        all_images = row.get("all_images") or []
        display_cover = clean(row.get("display_cover_url"))
        default_cover = clean(row.get("cover_image_url_default"))

        if source_images and not all_images:
            counts["all_images_empty_but_source_has_images"] += 1
            add_example(
                examples,
                "all_images_empty_but_source_has_images",
                row,
                {"source_image_count": len(source_images), "source_image_examples": source_images[:3]},
            )
        if source_covers and not default_cover:
            counts["cover_image_url_default_missing_but_source_has_cover"] += 1
            add_example(
                examples,
                "cover_image_url_default_missing_but_source_has_cover",
                row,
                {"source_covers": source_covers[:3]},
            )
        if source_images and not display_cover:
            counts["display_cover_url_missing_but_source_has_images"] += 1
            add_example(
                examples,
                "display_cover_url_missing_but_source_has_images",
                row,
                {"source_image_count": len(source_images), "source_image_examples": source_images[:3]},
            )

        covers_by_type = row.get("covers_by_type") or {}
        if display_cover and not any(clean(v) for v in covers_by_type.values()):
            counts["display_cover_present_but_no_typed_cover"] += 1

        if not row.get("embedding"):
            counts["embedding_missing_in_strict_input"] += 1

    return {
        "strict_path": str(strict_path),
        "counts": dict(counts),
        "by_source": {source: dict(counter) for source, counter in by_source.items()},
        "examples": examples,
    }


def audit_embedded(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"path": str(path) if path else None, "status": "SKIPPED"}
    data = json.load(path.open(encoding="utf-8"))
    rows = data.get("buildings") or []
    counts: Counter = Counter()
    examples: list[dict[str, Any]] = []
    for row in rows:
        counts["total_rows"] += 1
        emb = row.get("embedding")
        if not emb:
            counts["missing_embedding"] += 1
            if len(examples) < 20:
                examples.append({"canonical_bld_id": row.get("canonical_bld_id"), "name": row.get("name"), "reason": "missing"})
        elif len(emb) != 384:
            counts["bad_embedding_dim"] += 1
            if len(examples) < 20:
                examples.append(
                    {
                        "canonical_bld_id": row.get("canonical_bld_id"),
                        "name": row.get("name"),
                        "reason": "bad_dim",
                        "dim": len(emb),
                    }
                )
    return {"path": str(path), "counts": dict(counts), "examples": examples}


def source_slug_recovery_audit(source_meta: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """Divisare-specific check for remaining empty architect refs recoverable by slug."""
    arch_by_slug: dict[str, dict[str, Any]] = {}
    if not SOURCE_DBS["divisare"].exists():
        return {"status": "SKIPPED"}
    conn = sqlite3.connect(SOURCE_DBS["divisare"])
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT id, slug, name FROM divisare_architects"):
            if clean(row["slug"]):
                arch_by_slug[str(row["slug"])] = {"id": row["id"], "name": row["name"], "slug": row["slug"]}
    finally:
        conn.close()
    slugs = sorted(arch_by_slug, key=len, reverse=True)

    def hits(project_slug: str) -> list[dict[str, Any]]:
        rest = project_slug
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(12):
            hit = None
            for slug in slugs:
                if rest == slug or rest.startswith(slug + "-"):
                    hit = slug
                    break
            if not hit:
                break
            if hit not in seen:
                seen.add(hit)
                out.append(arch_by_slug[hit])
            rest = rest[len(hit):].lstrip("-")
        return out

    counts: Counter = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (source, sid), meta in source_meta.items():
        if source != "divisare":
            continue
        if meta.get("architect_source_ids"):
            continue
        counts["divisare_projects_without_architect_ids"] += 1
        h = hits(str(meta.get("slug") or ""))
        if h:
            counts["slug_prefix_recoverable"] += 1
            if len(examples["slug_prefix_recoverable"]) < 20:
                examples["slug_prefix_recoverable"].append(
                    {"source_id": sid, "name": meta.get("name"), "slug": meta.get("slug"), "hits": h}
                )
        else:
            counts["slug_prefix_unresolved"] += 1
            if len(examples["slug_prefix_unresolved"]) < 20:
                examples["slug_prefix_unresolved"].append(
                    {"source_id": sid, "name": meta.get("name"), "slug": meta.get("slug")}
                )
    return {"counts": dict(counts), "examples": dict(examples)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only source-to-canonical data integrity audit.")
    parser.add_argument("--strict", type=Path, default=DEFAULT_STRICT)
    parser.add_argument("--embedded", type=Path, default=DEFAULT_EMBEDDED)
    parser.add_argument("--architects", type=Path, default=DEFAULT_ARCHITECTS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    source_meta, source_stats = load_source_meta()
    arch_source_to_id, canonical_arch_ids = load_arch_index(args.architects)
    strict_report = audit_strict(args.strict, source_meta, arch_source_to_id, canonical_arch_ids)
    embedded_report = audit_embedded(args.embedded)
    divisare_recovery = source_slug_recovery_audit(source_meta)

    report = {
        "status": "COMPLETE",
        "strict": strict_report,
        "embedded": embedded_report,
        "source_stats": {source: dict(counter) for source, counter in source_stats.items()},
        "source_meta_rows_loaded": len(source_meta),
        "architect_source_index_size": len(arch_source_to_id),
        "divisare_slug_recovery": divisare_recovery,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        json.dumps(
            {
                "status": report["status"],
                "strict_counts": strict_report["counts"],
                "embedded_counts": embedded_report.get("counts"),
                "source_stats": report["source_stats"],
                "divisare_slug_recovery": divisare_recovery.get("counts"),
                "report": str(args.report),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
