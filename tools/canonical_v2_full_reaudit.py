#!/usr/bin/env python3
"""Full post-upsert canonical v2 audit.

This is intentionally stricter and broader than the upload validator.  It
does not mutate data.  It checks whether the C22 canonical dataset is clean
enough for a make_web card/feed product and whether the canonical rows still
match their raw source records.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from canonical_v2_c16_url_canon import (
    _canonical_asset_key,
    _is_gif,
    _is_lowres_url,
    _is_raster_url,
)
from canonical_v2_c21_make_web_polish import _normalize_country_full
from canonical_v2_upload_validator import iter_buildings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    ROOT
    / "data/canonical/country_conflict_refresh/"
    "canonical_buildings_strict_embedded.completeness_c22_make_web_polish.json"
)
DEFAULT_STRICT = (
    ROOT
    / "data/canonical/country_conflict_refresh/"
    "canonical_buildings_strict.completeness_c22_make_web_polish.json"
)
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_post_neon_full_reaudit.codex.json"
DEFAULT_MD = ROOT / "data/reports/canonical_v2_post_neon_full_reaudit.codex.md"

DB_DIR = ROOT / "data/crawl"
SIDECAR_PREFIX = ROOT / "data/reports/canonical_v2_post_neon_full_reaudit"

SOURCE_NAMES = ("archello", "architizer", "divisare", "metalocus")

COUNTRY_DIRTY_RE = re.compile(
    r"(\d{3,}|street|road|avenue|calle|district|province|county|"
    r"archipelago|island|brooklyn|dubai -|zhejiang|stockholm archipelago)",
    re.I,
)
CITY_DIRTY_RE = re.compile(
    r"(\d{3,}|street|road|avenue|ave\.?|calle|via|district|province|"
    r"county|municipality|region|archipelago|island|near|,.*,) ",
    re.I,
)
GENERIC_NAME_RE = re.compile(
    r"\b(project|house|villa|residence|private house|housing|office|"
    r"building|school|museum|center|centre|hotel|restaurant|apartment|"
    r"interior|extension|renovation|competition|masterplan|installation|"
    r"gallery|pavilion|store|shop)\b",
    re.I,
)
SPAM_NAME_RE = re.compile(
    r"\b(best|top|ideas?|trends?|guide|list|collection|roundup|"
    r"inspiration|interview|news|article|magazine|how to|what is)\b",
    re.I,
)
SOURCE_BRAND_RE = re.compile(r"\s*(?:-|/|\|)\s*(architizer|archello|divisare|metalocus)\s*$", re.I)
URLISH_RE = re.compile(r"(https?://|www\.|\.com\b|\.html\b|utm_|%[0-9a-f]{2})", re.I)
ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff]")


@dataclass
class SourceRecord:
    source: str
    id: str
    slug: str | None = None
    name: str | None = None
    architect_text: str | None = None
    country: str | None = None
    city: str | None = None
    year: int | None = None
    cover_url: str | None = None
    image_urls: list[str] | None = None
    source_url: str | None = None


def _norm_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = ZERO_WIDTH_RE.sub("", value)
    value = value.casefold()
    value = re.sub(r"['’`]", "", value)
    value = re.sub(r"[^a-z0-9가-힣ぁ-んァ-ン一-龥]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _norm_loose(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _tokens(value: Any) -> set[str]:
    return {t for t in _norm_text(value).split() if len(t) > 1}


def _similarity(a: Any, b: Any) -> float:
    aa = _norm_text(a)
    bb = _norm_text(b)
    if not aa or not bb:
        return 0.0
    if aa in bb or bb in aa:
        return 1.0
    return SequenceMatcher(None, aa, bb).ratio()


def _token_jaccard(a: Any, b: Any) -> float:
    ta = _tokens(a)
    tb = _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _parse_json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(parsed, list):
        return [str(x) for x in parsed if x]
    return []


def _first_year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1500 <= value <= 2100 else None
    text = str(value)
    years = [int(x) for x in re.findall(r"\b(1[5-9]\d{2}|20\d{2}|2100)\b", text)]
    return years[-1] if years else None


def _source_url(source: str, rec_id: str, slug: str | None) -> str | None:
    if source == "archello" and slug:
        return f"https://archello.com/project/{slug}"
    if source == "architizer" and slug:
        return f"https://architizer.com/projects/{slug}/"
    if source == "divisare" and rec_id and slug:
        return f"https://divisare.com/projects/{rec_id}-{slug}"
    return None


def _metalocus_slug_from_url(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    path = urlparse(url).path.strip("/")
    return path.split("/")[-1].strip().casefold()


def _metalocus_slug_key(slug: Any) -> str:
    text = str(slug or "").strip().casefold().strip("/")
    text = re.sub(r"^(?:a|an|the)-", "", text)
    return text


def _metalocus_slug_fingerprint(slug: Any) -> str:
    key = _metalocus_slug_key(slug)
    stop = {"a", "an", "the", "by", "and"}
    toks = [t for t in key.split("-") if t and t not in stop]
    return "-".join(toks)


def _clone_source_record(rec: SourceRecord, sid: str, source_url: str | None) -> SourceRecord:
    return SourceRecord(
        source=rec.source,
        id=sid,
        slug=rec.slug,
        name=rec.name,
        architect_text=rec.architect_text,
        country=rec.country,
        city=rec.city,
        year=rec.year,
        cover_url=rec.cover_url,
        image_urls=rec.image_urls,
        source_url=source_url or rec.source_url,
    )


def _connect(db_name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_DIR / db_name))
    conn.row_factory = sqlite3.Row
    return conn


def _load_source_maps() -> dict[str, dict[str, SourceRecord]]:
    maps: dict[str, dict[str, SourceRecord]] = {s: {} for s in SOURCE_NAMES}

    with _connect("archello.db") as conn:
        for r in conn.execute(
            """
            SELECT id, slug, name, architect_name, location_country,
                   location_city, project_year, cover_image_url,
                   gallery_image_urls
            FROM archello_projects
            """
        ):
            sid = str(r["id"])
            urls = [r["cover_image_url"]] + _parse_json_list(r["gallery_image_urls"])
            maps["archello"][sid] = SourceRecord(
                "archello",
                sid,
                r["slug"],
                r["name"],
                r["architect_name"],
                r["location_country"],
                r["location_city"],
                _first_year(r["project_year"]),
                r["cover_image_url"],
                [u for u in urls if u],
                _source_url("archello", sid, r["slug"]),
            )

    with _connect("architizer.db") as conn:
        for r in conn.execute(
            """
            SELECT id, global_id, slug, name, firm_name, location_country,
                   location_city, completion_year, cover_image_url,
                   gallery_image_urls
            FROM architizer_projects
            """
        ):
            sid = str(r["id"])
            urls = [r["cover_image_url"]] + _parse_json_list(r["gallery_image_urls"])
            rec = SourceRecord(
                "architizer",
                sid,
                r["slug"],
                r["name"],
                r["firm_name"],
                r["location_country"],
                r["location_city"],
                _first_year(r["completion_year"]),
                r["cover_image_url"],
                [u for u in urls if u],
                _source_url("architizer", sid, r["slug"]),
            )
            maps["architizer"][sid] = rec
            if r["global_id"]:
                maps["architizer"][str(r["global_id"])] = rec

    with _connect("divisare.db") as conn:
        for r in conn.execute(
            """
            SELECT id, slug, name, architect_names, location_country,
                   location_city, project_year, cover_image_url, gallery_urls
            FROM divisare_projects
            """
        ):
            sid = str(r["id"])
            arch = ", ".join(_parse_json_list(r["architect_names"]))
            urls = [r["cover_image_url"]] + _parse_json_list(r["gallery_urls"])
            maps["divisare"][sid] = SourceRecord(
                "divisare",
                sid,
                r["slug"],
                r["name"],
                arch,
                r["location_country"],
                r["location_city"],
                _first_year(r["project_year"]),
                r["cover_image_url"],
                [u for u in urls if u],
                _source_url("divisare", sid, r["slug"]),
            )

    metalocus_article_urls: dict[str, str] = {}
    metalocus_by_slug: dict[str, SourceRecord] = {}
    metalocus_by_fingerprint: dict[str, list[SourceRecord]] = defaultdict(list)
    with _connect("metalocus.db") as conn:
        for r in conn.execute("SELECT id, url, slug FROM articles"):
            metalocus_article_urls[str(r["id"])] = str(r["url"])
        for r in conn.execute(
            """
            SELECT id, article_id, title, architects, country, city, year,
                   cover_image_url, gallery_image_urls, drawing_image_urls
            FROM buildings
            """
        ):
            sid = f"B{int(r['id']):05d}"
            urls = (
                [r["cover_image_url"]]
                + _parse_json_list(r["gallery_image_urls"])
                + _parse_json_list(r["drawing_image_urls"])
            )
            source_url = metalocus_article_urls.get(str(r["article_id"]))
            rec = SourceRecord(
                "metalocus",
                sid,
                _metalocus_slug_from_url(source_url),
                r["title"],
                r["architects"],
                r["country"],
                r["city"],
                _first_year(r["year"]),
                r["cover_image_url"],
                [u for u in urls if u],
                source_url,
            )
            maps["metalocus"][sid] = rec
            slug_key = _metalocus_slug_key(rec.slug)
            if slug_key:
                metalocus_by_slug[slug_key] = rec
            fingerprint = _metalocus_slug_fingerprint(rec.slug)
            if fingerprint:
                metalocus_by_fingerprint[fingerprint].append(rec)

    # Metalocus canonical refs use stable B-ids from the enrich file, not
    # necessarily raw metalocus.db buildings.id. Resolve B-id -> URL slug ->
    # raw article/building record, and override the raw B-id mapping when they
    # differ.
    enrich = ROOT / "data/enrich/4_buildings_final.json"
    if enrich.exists():
        try:
            data = json.loads(enrich.read_text(encoding="utf-8"))
        except Exception:
            data = []
        if isinstance(data, list):
            for r in data:
                bid = r.get("building_id")
                if not bid:
                    continue
                sid = str(bid)
                rec = maps["metalocus"].get(sid)
                slug = r.get("slug")
                source_url = r.get("source_url") or r.get("url")
                if not source_url and slug:
                    source_url = f"https://www.metalocus.es/en/news/{slug}"
                slug_key = _metalocus_slug_key(slug or _metalocus_slug_from_url(source_url))
                raw_by_url = metalocus_by_slug.get(slug_key)
                if not raw_by_url:
                    fingerprint = _metalocus_slug_fingerprint(slug or _metalocus_slug_from_url(source_url))
                    matches = metalocus_by_fingerprint.get(fingerprint) or []
                    if len(matches) == 1:
                        raw_by_url = matches[0]
                if raw_by_url:
                    maps["metalocus"][sid] = _clone_source_record(raw_by_url, sid, source_url)
                elif rec:
                    rec.source_url = source_url or rec.source_url
                else:
                    maps["metalocus"][sid] = SourceRecord(
                        "metalocus",
                        sid,
                        slug,
                        r.get("title") or r.get("name"),
                        r.get("architects"),
                        r.get("country"),
                        r.get("city"),
                        _first_year(r.get("year")),
                        r.get("cover_image_url"),
                        [],
                        source_url,
                    )

    return maps


def _sample(samples: dict[str, list[Any]], key: str, value: Any, limit: int = 20) -> None:
    if len(samples.setdefault(key, [])) < limit:
        samples[key].append(value)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _row_brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "cid": row.get("canonical_bld_id"),
        "name": row.get("name"),
        "country": row.get("location_country"),
        "city": row.get("location_city"),
        "year": row.get("project_year"),
        "architect_names": row.get("architect_names") or [],
        "is_publishable": row.get("is_publishable"),
        "reasons": row.get("publishability_reasons") or [],
        "source_refs": row.get("source_refs") or {},
    }


def _clean_country(value: Any) -> str:
    text = str(value or "").strip()
    if not text or COUNTRY_DIRTY_RE.search(text):
        return ""
    return _normalize_country_full(text) or _norm_text(text)


def _clean_city(value: Any) -> str:
    text = str(value or "").strip()
    if not text or CITY_DIRTY_RE.search(text):
        return ""
    return _norm_text(text)


def _image_asset(url: str | None) -> str:
    if not url:
        return ""
    try:
        return _canonical_asset_key(url) or ""
    except Exception:
        return ""


def _source_records_for(row: dict[str, Any], maps: dict[str, dict[str, SourceRecord]]) -> list[SourceRecord]:
    out: list[SourceRecord] = []
    refs = row.get("source_refs") or {}
    for source, ids in refs.items():
        if source not in maps:
            continue
        for sid in ids or []:
            rec = maps[source].get(str(sid))
            if rec:
                out.append(rec)
    return out


def _ref_count(row: dict[str, Any]) -> int:
    refs = row.get("source_refs") or {}
    return sum(len(v or []) for v in refs.values())


def _architect_overlap(row: dict[str, Any], records: list[SourceRecord]) -> float:
    row_tokens = _tokens(" ".join(row.get("architect_names") or []) or row.get("architects_text"))
    source_tokens = _tokens(" ".join(r.architect_text or "" for r in records))
    if not row_tokens or not source_tokens:
        return 0.0
    return len(row_tokens & source_tokens) / len(row_tokens | source_tokens)


def _same_or_near_year(a: Any, b: Any, spread: int = 2) -> bool:
    if not isinstance(a, int) or not isinstance(b, int):
        return False
    return abs(a - b) <= spread


def _audit(args: argparse.Namespace) -> dict[str, Any]:
    source_maps = _load_source_maps()
    source_counts = {k: len(v) for k, v in source_maps.items()}

    counters: Counter[str] = Counter()
    warning: Counter[str] = Counter()
    samples: dict[str, list[Any]] = {}
    sidecars: dict[str, list[dict[str, Any]]] = defaultdict(list)

    cids: set[str] = set()
    source_ref_owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    source_url_owners: dict[str, list[str]] = defaultdict(list)
    exact_name_groups: dict[tuple[str, str, str, Any], list[dict[str, Any]]] = defaultdict(list)
    name_arch_groups: dict[tuple[str, str, Any], list[dict[str, Any]]] = defaultdict(list)
    cover_exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cover_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cover_phash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    gallery_phash_owners: dict[str, list[dict[str, Any]]] = defaultdict(list)
    gallery_asset_owners: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_rows_brief: dict[str, dict[str, Any]] = {}

    for row in iter_buildings(args.input, limit=args.limit):
        cid = row.get("canonical_bld_id")
        if not cid:
            counters["missing_canonical_bld_id"] += 1
            continue
        if cid in cids:
            counters["duplicate_canonical_bld_id"] += 1
            _sample(samples, "duplicate_canonical_bld_id", cid)
        cids.add(cid)
        brief = _row_brief(row)
        all_rows_brief[cid] = brief
        is_pub = bool(row.get("is_publishable"))
        counters["rows_total"] += 1
        counters["rows_publishable" if is_pub else "rows_nonpublishable"] += 1

        refs = row.get("source_refs") or {}
        urls = row.get("source_urls") or {}
        if row.get("n_sources") != len([s for s, ids in refs.items() if ids]):
            warning["n_sources_key_count_mismatch"] += 1
            _sample(samples, "n_sources_key_count_mismatch", brief)
        if not refs:
            counters["rows_missing_source_refs"] += 1
            _sample(samples, "rows_missing_source_refs", brief)
        for source, ids in refs.items():
            if source not in SOURCE_NAMES:
                warning["unknown_source_ref_key"] += 1
                _sample(samples, "unknown_source_ref_key", {**brief, "source": source})
            for sid in ids or []:
                source_ref_owners[(source, str(sid))].append(cid)
                if str(sid) not in source_maps.get(source, {}):
                    counters["source_ref_missing_raw_record"] += 1
                    sidecars["source_ref_missing_raw"].append({**brief, "source": source, "source_id": sid})
        for source, source_urls in urls.items():
            for u in source_urls or []:
                source_url_owners[str(u)].append(cid)
                domain = urlparse(str(u)).netloc.casefold()
                if source and source not in domain:
                    warning["source_url_domain_mismatch"] += 1
                    _sample(samples, "source_url_domain_mismatch", {**brief, "source": source, "url": u})
        if is_pub:
            for source, ids in refs.items():
                if ids and not (urls.get(source) or []):
                    counters["publishable_source_url_gap"] += 1
                    sidecars["source_url_gap"].append({**brief, "source": source, "source_ids": ids})

        records = _source_records_for(row, source_maps)
        source_names = [r.name for r in records if r.name]
        source_countries = [_clean_country(r.country) for r in records if _clean_country(r.country)]
        source_cities = [_clean_city(r.city) for r in records if _clean_city(r.city)]
        source_years = [r.year for r in records if isinstance(r.year, int)]
        source_images: set[str] = set()
        for rec in records:
            for u in rec.image_urls or []:
                key = _image_asset(u)
                if key:
                    source_images.add(key)

        if records:
            max_name_sim = max((_similarity(row.get("name"), name) for name in source_names), default=0.0)
            max_name_jac = max((_token_jaccard(row.get("name"), name) for name in source_names), default=0.0)
            if source_names and max_name_sim < 0.55 and max_name_jac < 0.35:
                counters["canonical_name_low_similarity_to_all_sources"] += 1
                sidecars["source_name_mismatch"].append({
                    **brief,
                    "source_names": source_names[:10],
                    "max_similarity": round(max_name_sim, 3),
                    "max_token_jaccard": round(max_name_jac, 3),
                })
            row_country = _clean_country(row.get("location_country"))
            source_country_set = set(source_countries)
            if is_pub and row_country and source_country_set and row_country not in source_country_set:
                counters["publishable_country_not_in_any_source"] += 1
                sidecars["country_mismatch"].append({
                    **brief,
                    "row_country_norm": row_country,
                    "source_country_norm": sorted(source_country_set),
                    "raw_source_countries": [r.country for r in records if r.country],
                })
            if is_pub and len(source_country_set) > 1:
                warning["publishable_multi_source_country_conflict"] += 1
            row_city = _clean_city(row.get("location_city"))
            source_city_set = set(source_cities)
            if is_pub and row_city and source_city_set:
                best_city = max((_similarity(row_city, c) for c in source_city_set), default=0.0)
                if best_city < 0.72 and row_city not in source_city_set:
                    warning["publishable_city_not_close_to_any_source"] += 1
                    sidecars["city_mismatch"].append({
                        **brief,
                        "row_city_norm": row_city,
                        "source_city_norm": sorted(source_city_set),
                        "raw_source_cities": [r.city for r in records if r.city],
                        "best_similarity": round(best_city, 3),
                    })
            row_year = row.get("project_year")
            if is_pub and isinstance(row_year, int) and source_years:
                if not any(_same_or_near_year(row_year, y) for y in source_years):
                    warning["publishable_year_not_near_any_source"] += 1
                    sidecars["year_mismatch"].append({
                        **brief,
                        "source_years": sorted(set(source_years)),
                    })
                if max(source_years + [row_year]) - min(source_years + [row_year]) > 2:
                    warning["publishable_multi_source_year_conflict"] += 1

            # Suspected false merge: low name agreement plus no strong
            # location/year/architect evidence for multi-source rows.
            if is_pub and _ref_count(row) >= 2 and len(records) >= 2:
                country_conflict = len(source_country_set) > 1
                year_conflict = source_years and max(source_years) - min(source_years) > 2
                arch_overlap = _architect_overlap(row, records)
                cross_source_name_low = False
                if len(source_names) >= 2:
                    sims = [_similarity(a, b) for a, b in combinations(source_names[:8], 2)]
                    cross_source_name_low = bool(sims and min(sims) < 0.45)
                if (
                    cross_source_name_low
                    and (country_conflict or year_conflict or arch_overlap < 0.08)
                    and max_name_sim < 0.80
                ):
                    counters["suspected_false_merge_rows"] += 1
                    sidecars["suspected_false_merge"].append({
                        **brief,
                        "source_names": source_names[:10],
                        "source_countries": [r.country for r in records if r.country],
                        "source_years": source_years,
                        "architect_overlap": round(arch_overlap, 3),
                    })

        name = row.get("name") or ""
        if name != name.strip():
            warning["name_leading_trailing_whitespace"] += 1
            _sample(samples, "name_leading_trailing_whitespace", brief)
        if unicodedata.normalize("NFC", name) != name:
            warning["name_not_nfc"] += 1
        if ZERO_WIDTH_RE.search(name):
            counters["name_zero_width_or_bom"] += 1
            sidecars["string_corruption"].append({**brief, "field": "name", "value": name})
        if SOURCE_BRAND_RE.search(name):
            counters["name_source_brand_suffix"] += 1
            sidecars["name_quality"].append({**brief, "issue": "source_brand_suffix"})
        if URLISH_RE.search(name):
            if source_names and any(_norm_text(name) == _norm_text(src_name) for src_name in source_names):
                warning["name_urlish_but_source_confirmed"] += 1
                sidecars["name_quality"].append({**brief, "issue": "urlish_source_confirmed"})
            else:
                counters["name_urlish"] += 1
                sidecars["name_quality"].append({**brief, "issue": "urlish"})
        if is_pub and SPAM_NAME_RE.search(name):
            warning["publishable_seo_or_listicle_name_candidate"] += 1
            sidecars["seo_name_candidate"].append(brief)
        if is_pub and GENERIC_NAME_RE.search(name):
            warning["publishable_genericish_name"] += 1
        if is_pub and len(name) > 120:
            warning["publishable_very_long_name"] += 1
            sidecars["long_name"].append(brief)
        if is_pub and row.get("project_year") and row.get("project_year") > 2026:
            warning["publishable_future_year"] += 1
        if is_pub and not row.get("location_city"):
            warning["publishable_missing_city"] += 1
        if is_pub and not row.get("project_year"):
            warning["publishable_missing_year"] += 1
        if is_pub and row.get("location_city") and CITY_DIRTY_RE.search(str(row.get("location_city"))):
            warning["publishable_suspicious_city"] += 1
            sidecars["suspicious_city"].append(brief)

        if is_pub:
            country_key = _clean_country(row.get("location_country"))
            city_key = _clean_city(row.get("location_city"))
            name_key = _norm_text(name)
            year_key = row.get("project_year")
            arch_key = " ".join(sorted(_tokens(" ".join(row.get("architect_names") or []))))[:160]
            if name_key:
                exact_name_groups[(name_key, country_key, city_key, year_key)].append(brief)
            if name_key and arch_key:
                name_arch_groups[(name_key, arch_key, year_key)].append(brief)

        images = row.get("all_images") or []
        if is_pub and not images:
            counters["publishable_empty_all_images"] += 1
            sidecars["image_blocker"].append({**brief, "issue": "empty_all_images"})
        if is_pub and len(images) < 3:
            warning["publishable_lt3_images"] += 1
        if is_pub and len(images) > 60:
            warning["publishable_gt60_images"] += 1

        seen_url: Counter[str] = Counter()
        seen_asset: Counter[str] = Counter()
        seen_phash: Counter[str] = Counter()
        image_by_asset: dict[str, dict[str, Any]] = {}
        for im in images:
            if not isinstance(im, dict):
                continue
            url = im.get("url")
            if not url:
                continue
            seen_url[str(url)] += 1
            asset = _image_asset(url)
            if asset:
                seen_asset[asset] += 1
                image_by_asset.setdefault(asset, im)
                if is_pub:
                    gallery_asset_owners[asset].append(brief)
            phash = im.get("phash")
            if phash:
                seen_phash[str(phash)] += 1
                if is_pub:
                    gallery_phash_owners[str(phash)].append(brief)
            elif is_pub:
                warning["publishable_gallery_images_missing_phash"] += 1
            if is_pub and not str(url).startswith("https://"):
                counters["publishable_image_not_https"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "image_not_https", "url": url})
            if is_pub and not _is_raster_url(str(url)):
                counters["publishable_image_non_raster"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "image_non_raster", "url": url})
        if is_pub and any(v > 1 for v in seen_url.values()):
            counters["publishable_gallery_exact_dup_rows"] += 1
            sidecars["gallery_internal_dup"].append({**brief, "issue": "exact_url", "dups": [k for k, v in seen_url.items() if v > 1][:10]})
        if is_pub and any(v > 1 for v in seen_asset.values()):
            counters["publishable_gallery_asset_dup_rows"] += 1
            sidecars["gallery_internal_dup"].append({**brief, "issue": "asset_key", "dups": [k for k, v in seen_asset.items() if v > 1][:10]})
        if is_pub and any(v > 1 for v in seen_phash.values()):
            counters["publishable_gallery_phash_dup_rows"] += 1
            sidecars["gallery_internal_dup"].append({**brief, "issue": "phash", "dups": [k for k, v in seen_phash.items() if v > 1][:10]})

        cover = row.get("display_cover_url")
        cover_asset_key = _image_asset(cover)
        cover_im = image_by_asset.get(cover_asset_key)
        if is_pub:
            if not cover:
                counters["publishable_missing_display_cover"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "missing_display_cover"})
            elif not str(cover).startswith("https://"):
                counters["publishable_cover_not_https"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "cover_not_https", "url": cover})
            elif not _is_raster_url(str(cover)):
                counters["publishable_cover_non_raster"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "cover_non_raster", "url": cover})
            elif _is_gif(str(cover)):
                counters["publishable_cover_gif"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "cover_gif", "url": cover})
            elif _is_lowres_url(str(cover)):
                counters["publishable_cover_lowres"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "cover_lowres", "url": cover})
            if cover and cover_asset_key not in image_by_asset:
                counters["publishable_cover_asset_not_in_all_images"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "cover_asset_not_in_all_images", "url": cover})
            if cover_im and not cover_im.get("phash"):
                counters["publishable_cover_missing_matched_phash"] += 1
                sidecars["image_blocker"].append({**brief, "issue": "cover_missing_matched_phash", "url": cover})
            if cover_asset_key and source_images and cover_asset_key not in source_images:
                warning["publishable_cover_not_found_in_raw_source_images"] += 1
                sidecars["cover_source_image_mismatch"].append({**brief, "cover": cover})
        if is_pub and cover:
            cover_exact[str(cover)].append(brief)
            if cover_asset_key:
                cover_asset[cover_asset_key].append(brief)
            if cover_im and cover_im.get("phash"):
                cover_phash[str(cover_im.get("phash"))].append(brief)

    # Cross-row duplicate source references are always suspicious.
    for (source, sid), owners in source_ref_owners.items():
        uniq = sorted(set(owners))
        if len(uniq) > 1:
            counters["source_ref_used_by_multiple_canonicals"] += 1
            sidecars["source_ref_duplicate_owner"].append({
                "source": source,
                "source_id": sid,
                "cids": uniq,
                "rows": [all_rows_brief.get(cid) for cid in uniq[:10]],
            })

    for url, owners in source_url_owners.items():
        uniq = sorted(set(owners))
        if len(uniq) > 1:
            counters["source_url_used_by_multiple_canonicals"] += 1
            sidecars["source_url_duplicate_owner"].append({
                "url": url,
                "cids": uniq,
                "rows": [all_rows_brief.get(cid) for cid in uniq[:10]],
            })

    def _dup_groups(groups: dict[Any, list[dict[str, Any]]], sidecar: str, counter: str) -> None:
        for key, rows in groups.items():
            cids2 = sorted({r["cid"] for r in rows if r})
            if len(cids2) > 1:
                counters[counter] += 1
                sidecars[sidecar].append({"key": key, "rows": rows[:20]})

    _dup_groups(cover_exact, "cover_cross_row_dup", "cross_row_cover_exact_dup_groups")
    _dup_groups(cover_phash, "cover_cross_row_dup", "cross_row_cover_phash_dup_groups")

    # Asset keys for Architizer can be noisy ("001", "002"). Keep as warning
    # unless exact/phash duplicate also hits.
    for key, rows in cover_asset.items():
        cids2 = sorted({r["cid"] for r in rows if r})
        if len(cids2) > 1:
            warning["cross_row_cover_asset_dup_groups"] += 1
            if len(sidecars["cover_cross_row_asset_warning"]) < 200:
                sidecars["cover_cross_row_asset_warning"].append({"key": key, "rows": rows[:20]})

    for key, rows in exact_name_groups.items():
        cids2 = sorted({r["cid"] for r in rows if r})
        if len(cids2) > 1:
            warning["exact_name_country_city_year_duplicate_groups"] += 1
            sidecars["duplicate_name_location_year"].append({"key": key, "rows": rows[:20]})
    for key, rows in name_arch_groups.items():
        cids2 = sorted({r["cid"] for r in rows if r})
        if len(cids2) > 1:
            warning["exact_name_arch_year_duplicate_groups"] += 1

    # Cross-row gallery duplicates are expected in shared publications, but
    # phash clusters with related metadata are strong split/series evidence.
    for phash, rows in gallery_phash_owners.items():
        by_cid = {r["cid"]: r for r in rows if r}
        if len(by_cid) < 2:
            continue
        vals = list(by_cid.values())
        pairs = []
        for a, b in combinations(vals[:40], 2):
            name_sim = _similarity(a.get("name"), b.get("name"))
            same_loc = (
                _clean_country(a.get("country")) == _clean_country(b.get("country"))
                and _clean_city(a.get("city")) == _clean_city(b.get("city"))
            )
            same_year = _same_or_near_year(a.get("year"), b.get("year"), 1)
            arch_sim = _token_jaccard(" ".join(a.get("architect_names") or []), " ".join(b.get("architect_names") or []))
            if name_sim >= 0.70 or (same_loc and same_year and arch_sim >= 0.12):
                pairs.append({"a": a, "b": b, "name_similarity": round(name_sim, 3), "architect_jaccard": round(arch_sim, 3)})
        if pairs:
            warning["cross_row_gallery_phash_split_or_series_groups"] += 1
            if len(sidecars["cross_row_gallery_phash_review"]) < 500:
                sidecars["cross_row_gallery_phash_review"].append({"phash": phash, "pairs": pairs[:20]})

    # Existing registry checks.
    registry_path = ROOT / "data/id_registry_buildings.json"
    registry_report: dict[str, Any] = {"exists": registry_path.exists()}
    if registry_path.exists() and not args.limit:
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception as exc:
            registry_report["parse_error"] = str(exc)
        else:
            items = registry.items() if isinstance(registry, dict) else []
            redirects = {}
            missing_redirect_targets = []
            active_missing_from_canonical = []
            for key, value in items:
                if not isinstance(value, dict):
                    continue
                target = value.get("redirected_to")
                if target:
                    redirects[key] = target
                    if target not in cids:
                        missing_redirect_targets.append({"id": key, "redirected_to": target})
                elif key.startswith("bld_") and key not in cids:
                    active_missing_from_canonical.append(key)
            registry_report.update({
                "entries": len(registry) if isinstance(registry, dict) else None,
                "redirects": len(redirects),
                "missing_redirect_targets": missing_redirect_targets[:50],
                "missing_redirect_targets_count": len(missing_redirect_targets),
                "active_missing_from_canonical_count": len(active_missing_from_canonical),
                "active_missing_from_canonical_sample": active_missing_from_canonical[:50],
            })
            if missing_redirect_targets:
                counters["registry_redirect_target_missing"] += len(missing_redirect_targets)

    for name, rows in sidecars.items():
        _write_jsonl(SIDECAR_PREFIX.with_name(f"{SIDECAR_PREFIX.name}_{name}.jsonl"), rows)

    hard_keys = [
        "duplicate_canonical_bld_id",
        "rows_missing_source_refs",
        "source_ref_missing_raw_record",
        "publishable_source_url_gap",
        "source_ref_used_by_multiple_canonicals",
        "source_url_used_by_multiple_canonicals",
        "suspected_false_merge_rows",
        "publishable_empty_all_images",
        "publishable_image_not_https",
        "publishable_image_non_raster",
        "publishable_gallery_exact_dup_rows",
        "publishable_gallery_asset_dup_rows",
        "publishable_gallery_phash_dup_rows",
        "publishable_missing_display_cover",
        "publishable_cover_not_https",
        "publishable_cover_non_raster",
        "publishable_cover_gif",
        "publishable_cover_lowres",
        "publishable_cover_asset_not_in_all_images",
        "publishable_cover_missing_matched_phash",
        "cross_row_cover_exact_dup_groups",
        "cross_row_cover_phash_dup_groups",
        "name_zero_width_or_bom",
        "name_source_brand_suffix",
        "name_urlish",
        "registry_redirect_target_missing",
    ]
    hard_total = sum(counters.get(key, 0) for key in hard_keys)
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "input": str(args.input.relative_to(ROOT) if args.input.is_relative_to(ROOT) else args.input),
        "strict_input": str(args.strict.relative_to(ROOT) if args.strict and args.strict.is_relative_to(ROOT) else args.strict),
        "limit": args.limit,
        "status": "PASS" if hard_total == 0 else "FAIL",
        "hard_total": hard_total,
        "hard_keys": hard_keys,
        "counters": dict(sorted(counters.items())),
        "warnings": dict(sorted(warning.items())),
        "source_counts": source_counts,
        "registry": registry_report,
        "sidecars": {
            name: {
                "count": len(rows),
                "path": str(SIDECAR_PREFIX.with_name(f"{SIDECAR_PREFIX.name}_{name}.jsonl").relative_to(ROOT)),
            }
            for name, rows in sorted(sidecars.items())
        },
        "samples": samples,
    }
    return report


def _write_md(report: dict[str, Any], path: Path) -> None:
    counters = Counter(report.get("counters") or {})
    warnings = Counter(report.get("warnings") or {})
    sidecars = report.get("sidecars") or {}
    lines = [
        "# Post-Neon Full Canonical Reaudit",
        "",
        f"Generated: {report.get('generated')}",
        "",
        "## Verdict",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Hard total: `{report.get('hard_total')}`",
        f"- Rows: `{counters.get('rows_total', 0)}`",
        f"- Publishable: `{counters.get('rows_publishable', 0)}`",
        f"- Nonpublishable: `{counters.get('rows_nonpublishable', 0)}`",
        "",
        "## Hard Findings",
        "",
    ]
    for key in report.get("hard_keys") or []:
        val = counters.get(key, 0)
        if val:
            lines.append(f"- `{key}`: {val}")
    if not any(counters.get(k, 0) for k in report.get("hard_keys") or []):
        lines.append("- None.")
    lines.extend(["", "## High-Signal Warnings", ""])
    for key, val in warnings.most_common(30):
        lines.append(f"- `{key}`: {val}")
    lines.extend(["", "## Sidecars", ""])
    for name, meta in sorted(sidecars.items()):
        lines.append(f"- `{name}`: {meta.get('count')} rows -> `{meta.get('path')}`")
    lines.extend([
        "",
        "## Notes",
        "",
        "- This audit is read-only.",
        "- Exact URL/phash image duplicates are treated as hard findings.",
        "- Canonical asset-key duplicates are warnings when exact/phash duplicates do not confirm same-photo reuse.",
        "- Country/city/year source mismatches are sidecar review items because raw source fields can be dirty.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _display_path(path: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full read-only canonical v2 reaudit.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--strict", type=Path, default=DEFAULT_STRICT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    report = _audit(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_md(report, args.md)
    print(json.dumps({
        "status": report["status"],
        "hard_total": report["hard_total"],
        "rows": report["counters"].get("rows_total"),
        "publishable": report["counters"].get("rows_publishable"),
        "nonpublishable": report["counters"].get("rows_nonpublishable"),
        "hard_findings": {k: report["counters"].get(k, 0) for k in report["hard_keys"] if report["counters"].get(k, 0)},
        "top_warnings": dict(Counter(report["warnings"]).most_common(15)),
        "sidecars": {k: v["count"] for k, v in report["sidecars"].items()},
        "report": _display_path(args.report),
        "md": _display_path(args.md),
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
