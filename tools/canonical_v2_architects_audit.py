#!/usr/bin/env python3
"""Read-only audit for canonical_v2 architects build output."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import canonical_v2_architects_build as builder  # noqa: E402
from tools.canonical_v2_c21_make_web_polish import _normalize_country_full  # noqa: E402
from tools.canonical_v2_c23_final import _KNOWN_COUNTRIES  # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

DEFAULT_ARCHITECTS = ROOT / "data/canonical/canonical_architects_v2.json"
DEFAULT_BUILDINGS = (
    ROOT
    / "data/canonical/country_conflict_refresh/"
    "canonical_buildings_strict_embedded.completeness_c23_final.json"
)
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_architects_audit.codex.json"
DEFAULT_MD = ROOT / "data/reports/canonical_v2_architects_audit.codex.md"
SIDECAR_PREFIX = ROOT / "data/reports/canonical_v2_architects_audit"

REQUIRED_FIELDS = {
    "canonical_arch_id",
    "canonical_name",
    "name_alts",
    "description",
    "primary_country",
    "primary_city",
    "office_locations",
    "website",
    "email",
    "phone",
    "social_links",
    "building_ids",
    "n_buildings",
    "n_buildings_publishable",
    "countries",
    "cities",
    "top_programs",
    "top_styles",
    "top_color_tones",
    "top_atmospheres",
    "top_materials",
    "top_typologies",
    "top_arch_elements",
    "feature_distribution",
    "earliest_project_year",
    "latest_project_year",
    "source_refs",
    "source_urls",
    "source_descriptions",
    "n_sources",
    "confidence_tier",
    "logo_url",
    "hero_building_id",
    "portfolio_embedding",
    "is_recommendable",
}

BRAND_SUFFIX_RE = re.compile(r"\s*(?:-|/|\|)\s*(Architizer|Archello|Divisare|Metalocus)\s*$", re.I)
DIRTY_COUNTRY_RE = re.compile(
    r"(\d{3,}|street|road|avenue|calle|district|province|county|"
    r"airport|community|private\s*-|south west|north west|city of|metropolitan)",
    re.I,
)
SOURCE_SOCIAL_RE = re.compile(
    r"(facebook\.com/(?:archello|architizer|divisare|metalocus)\b|"
    r"instagram\.com/(?:archello|architizer|divisare|metalocus)\b|"
    r"twitter\.com/(?:archello|architizer|divisare|metalocus)\b|"
    r"x\.com/(?:archello|architizer|divisare|metalocus)\b|"
    r"pinterest\.[^/]+/(?:archello|architizer|divisare|metalocus)\b|"
    r"linkedin\.com/company/(?:archello|architizer|divisare|metalocus)\b)",
    re.I,
)

CITY_STATE_COUNTRIES = {
    "Hong Kong",
    "Luxembourg",
    "Monaco",
    "San Marino",
    "Singapore",
    "Vatican City",
}

ADDITIONAL_VALID_COUNTRIES = {
    "Afghanistan",
    "Algeria",
    "Angola",
    "Armenia",
    "Azerbaijan",
    "Bolivia",
    "Burkina Faso",
    "Cabo Verde",
    "Cambodia",
    "Cameroon",
    "Colombia",
    "Costa Rica",
    "Cuba",
    "Côte d'Ivoire",
    "Dominican Republic",
    "Ecuador",
    "El Salvador",
    "Georgia",
    "Gibraltar",
    "Guatemala",
    "Haiti",
    "Jersey",
    "Kazakhstan",
    "Laos",
    "Madagascar",
    "Mali",
    "Mongolia",
    "Nepal",
    "Nicaragua",
    "Niger",
    "Palestine",
    "Panama",
    "Paraguay",
    "Puerto Rico",
    "Rwanda",
    "Senegal",
    "Somalia",
    "Tanzania",
    "Uganda",
    "Uruguay",
    "Venezuela",
}

VALID_COUNTRIES = set(_KNOWN_COUNTRIES) | ADDITIONAL_VALID_COUNTRIES


def _load_architects(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("architects") or [])


def _load_registry(path: Path) -> dict[str, dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _top_k(counter: Counter[str], k: int = 5) -> list[str]:
    return [name for name, _ in counter.most_common(k)]


def _building_reverse_index(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], dict[str, int]]:
    arch_to_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_id: dict[str, dict[str, Any]] = {}
    stats = Counter()
    for row in iter_buildings(path):
        stats["buildings_scanned"] += 1
        if row.get("is_publishable"):
            stats["buildings_publishable"] += 1
        bid = row.get("canonical_bld_id")
        if bid:
            by_id[bid] = row
        for arch_id in row.get("architect_canonical_ids") or []:
            arch_to_rows[arch_id].append(row)
    return arch_to_rows, by_id, dict(stats)


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counters = {
        "program": Counter(),
        "style": Counter(),
        "color_tone": Counter(),
        "atmosphere": Counter(),
        "material_visual": Counter(),
        "typologies": Counter(),
        "architectural_elements": Counter(),
    }
    countries = set()
    cities = set()
    years = []
    publishable = 0
    embeddings_pub = []
    embeddings_all = []
    hero_candidates = []
    building_ids = []

    for row in rows:
        bid = row.get("canonical_bld_id")
        building_ids.append(bid)
        is_pub = bool(row.get("is_publishable"))
        if is_pub:
            publishable += 1
        emb = row.get("embedding")
        if isinstance(emb, list) and len(emb) == 384:
            embeddings_all.append(emb)
            if is_pub:
                embeddings_pub.append(emb)
        for field in ("program", "style", "color_tone", "atmosphere"):
            value = row.get(field)
            if value:
                counters[field][value] += 1
        for value in row.get("material_visual") or []:
            if value:
                counters["material_visual"][value] += 1
        for value in row.get("architectural_elements") or []:
            if value:
                counters["architectural_elements"][value] += 1
        primary = row.get("typology_primary")
        if primary:
            counters["typologies"][primary] += 1
        for value in row.get("typology_tags") or []:
            if value:
                counters["typologies"][value] += 1
        if row.get("location_country"):
            countries.add(row["location_country"])
        if row.get("location_city"):
            cities.add(row["location_city"])
        if isinstance(row.get("project_year"), int):
            years.append(row["project_year"])
        if is_pub:
            tier_rank = {"T1": 0, "T2": 1, "T3": 2}.get(row.get("confidence_tier") or "T3", 3)
            hero_candidates.append((tier_rank, -(row.get("n_sources") or 0), bid))

    pool = embeddings_pub if embeddings_pub else embeddings_all
    embedding = None
    if pool:
        embedding = np.mean(np.array(pool, dtype=np.float32), axis=0)
    hero = None
    if hero_candidates:
        hero = sorted(hero_candidates)[0][2]

    return {
        "building_ids": sorted(building_ids),
        "n_buildings": len(rows),
        "n_buildings_publishable": publishable,
        "countries": sorted(countries),
        "cities": sorted(cities),
        "top_programs": _top_k(counters["program"]),
        "top_styles": _top_k(counters["style"]),
        "top_color_tones": _top_k(counters["color_tone"]),
        "top_atmospheres": _top_k(counters["atmosphere"]),
        "top_materials": _top_k(counters["material_visual"]),
        "top_typologies": _top_k(counters["typologies"]),
        "top_arch_elements": _top_k(counters["architectural_elements"]),
        "feature_distribution": {key: dict(value) for key, value in counters.items()},
        "earliest_project_year": min(years) if years else None,
        "latest_project_year": max(years) if years else None,
        "hero_building_id": hero,
        "portfolio_embedding": embedding,
    }


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_arch_id": row.get("canonical_arch_id"),
        "canonical_name": row.get("canonical_name"),
        "primary_country": row.get("primary_country"),
        "primary_city": row.get("primary_city"),
        "n_buildings": row.get("n_buildings"),
        "n_buildings_publishable": row.get("n_buildings_publishable"),
        "n_sources": row.get("n_sources"),
        "confidence_tier": row.get("confidence_tier"),
        "is_recommendable": row.get("is_recommendable"),
        "source_refs": row.get("source_refs"),
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _display_path(path: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _diff_scalar(actual: Any, expected: Any) -> bool:
    return actual != expected


def _sidecar(sidecars: dict[str, list[dict[str, Any]]], name: str, row: dict[str, Any], limit: int | None = None) -> None:
    if limit is not None and len(sidecars[name]) >= limit:
        return
    sidecars[name].append(row)


def _expected_names_and_arch_tokens(reg: dict[str, Any]) -> tuple[str | None, list[str], set[str]]:
    raw_names = reg.get("names") or []
    stripped_names = []
    for name in raw_names:
        if not isinstance(name, str):
            continue
        clean, _ = builder._strip_architect_brand(name)
        clean = " ".join(clean.split()) if clean else ""
        if clean:
            stripped_names.append(clean)

    seen = set()
    stripped_uniq = []
    for name in stripped_names:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        stripped_uniq.append(name)

    if not stripped_uniq:
        stripped_uniq = [raw_names[0]] if raw_names else []
    if not stripped_uniq:
        return None, [], set()

    canonical_name = sorted(stripped_uniq, key=lambda s: (len(s), s.casefold()))[0]
    name_alts = sorted(set(stripped_uniq) - {canonical_name})

    arch_name_set = {name.casefold() for name in stripped_uniq}
    arch_name_set.add(canonical_name.casefold())
    for name in stripped_uniq + [canonical_name]:
        for token in re.split(r"[\s,;.&|/]+", name):
            token = token.strip().casefold()
            if len(token) >= 3:
                arch_name_set.add(token)

    return canonical_name, name_alts, arch_name_set


def _apply_portfolio_location_override(
    metadata: dict[str, Any],
    buildings: list[dict[str, Any]],
    arch_name_set: set[str],
) -> dict[str, Any]:
    expected = dict(metadata)
    country_counter = Counter()
    for row in buildings:
        if not row.get("is_publishable"):
            continue
        country = builder._clean_country(row.get("location_country"), arch_name_set)
        if country:
            country_counter[country] += 1

    if country_counter:
        expected["country"] = country_counter.most_common(1)[0][0]

    final_country = expected.get("country")
    city_counter = Counter()
    if final_country:
        for row in buildings:
            if not row.get("is_publishable"):
                continue
            building_country = builder._clean_country(row.get("location_country"), arch_name_set)
            if building_country != final_country:
                continue
            city = builder._clean_city(row.get("location_city"), final_country, arch_name_set)
            if city:
                city_counter[city] += 1

    src_city = expected.get("city")
    if src_city and city_counter.get(src_city, 0) >= 1:
        return expected
    if city_counter:
        expected["city"] = city_counter.most_common(1)[0][0]
    else:
        expected["city"] = None
    return expected


def _audit(args: argparse.Namespace) -> dict[str, Any]:
    architects = _load_architects(args.input)
    registry = _load_registry(builder.REGISTRY)
    active_registry = {k: v for k, v in registry.items() if not v.get("redirected_to")}
    arch_to_rows, building_by_id, building_stats = _building_reverse_index(args.buildings)

    src_data = {
        "archello": builder._load_archello_firms(),
        "architizer": builder._load_architizer_firms(),
        "divisare": builder._load_divisare_architects(),
    }

    counters = Counter()
    warnings = Counter()
    sidecars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_ids: set[str] = set()
    active_with_buildings = {aid for aid in active_registry if arch_to_rows.get(aid)}

    for row in architects:
        counters["rows_total"] += 1
        arch_id = row.get("canonical_arch_id")
        if not arch_id or arch_id in seen_ids:
            counters["duplicate_or_missing_arch_id"] += 1
            _sidecar(sidecars, "schema_failures", {"issue": "duplicate_or_missing_arch_id", "row": row}, 50)
            continue
        seen_ids.add(arch_id)

        missing = sorted(REQUIRED_FIELDS - set(row))
        if missing:
            counters["missing_required_fields"] += 1
            _sidecar(sidecars, "schema_failures", {**_brief(row), "missing": missing})

        reg = active_registry.get(arch_id)
        if not reg:
            counters["row_not_active_registry"] += 1
            _sidecar(sidecars, "registry_mismatch", _brief(row))
            continue

        source_refs = reg.get("source_refs") or {}
        if row.get("source_refs") != source_refs:
            counters["source_refs_not_registry_exact"] += 1
            _sidecar(sidecars, "registry_mismatch", {
                **_brief(row),
                "expected_source_refs": source_refs,
            })

        buildings = arch_to_rows.get(arch_id) or []
        if not buildings:
            counters["row_has_no_reverse_index_buildings"] += 1
            _sidecar(sidecars, "building_mismatch", _brief(row))
            continue

        agg = _aggregate(buildings)
        for field in (
            "building_ids",
            "n_buildings",
            "n_buildings_publishable",
            "countries",
            "cities",
            "top_programs",
            "top_styles",
            "top_color_tones",
            "top_atmospheres",
            "top_materials",
            "top_typologies",
            "top_arch_elements",
            "feature_distribution",
            "earliest_project_year",
            "latest_project_year",
            "hero_building_id",
        ):
            if _diff_scalar(row.get(field), agg.get(field)):
                counters[f"{field}_mismatch"] += 1
                _sidecar(sidecars, "building_mismatch", {
                    **_brief(row),
                    "field": field,
                    "actual": row.get(field),
                    "expected": agg.get(field),
                }, 100)

        emb = row.get("portfolio_embedding")
        if not isinstance(emb, list) or len(emb) != 384:
            counters["embedding_bad_dim"] += 1
            _sidecar(sidecars, "embedding_mismatch", {**_brief(row), "issue": "bad_dim"})
        elif any(not isinstance(x, (int, float)) or not math.isfinite(float(x)) for x in emb):
            counters["embedding_nonfinite"] += 1
            _sidecar(sidecars, "embedding_mismatch", {**_brief(row), "issue": "nonfinite"})
        elif agg["portfolio_embedding"] is None:
            counters["embedding_expected_none"] += 1
            _sidecar(sidecars, "embedding_mismatch", {**_brief(row), "issue": "expected_none"})
        else:
            actual = np.array(emb, dtype=np.float32)
            diff = np.abs(actual - agg["portfolio_embedding"])
            max_abs = float(diff.max())
            if max_abs > 1e-5:
                counters["embedding_mean_mismatch"] += 1
                _sidecar(sidecars, "embedding_mismatch", {
                    **_brief(row),
                    "max_abs_diff": max_abs,
                }, 100)

        expected_recommendable = (
            row.get("n_buildings_publishable", 0) >= 3
            and bool(row.get("website") or row.get("description") or row.get("primary_country"))
        )
        if bool(row.get("is_recommendable")) != expected_recommendable:
            counters["is_recommendable_mismatch"] += 1
            _sidecar(sidecars, "recommendable_mismatch", {
                **_brief(row),
                "expected": expected_recommendable,
            })

        expected_n_sources = sum(1 for _, value in source_refs.items() if value)
        if row.get("n_sources") != expected_n_sources:
            counters["n_sources_mismatch"] += 1
            _sidecar(sidecars, "source_mismatch", {
                **_brief(row),
                "expected_n_sources": expected_n_sources,
            })
        expected_tier = builder._confidence_tier(expected_n_sources)
        if row.get("confidence_tier") != expected_tier:
            counters["confidence_tier_mismatch"] += 1
            _sidecar(sidecars, "source_mismatch", {
                **_brief(row),
                "expected_confidence_tier": expected_tier,
            })

        expected_name, expected_name_alts, arch_name_set = _expected_names_and_arch_tokens(reg)
        if row.get("canonical_name") != expected_name:
            counters["canonical_name_mismatch"] += 1
            _sidecar(sidecars, "name_quality", {
                **_brief(row),
                "issue": "canonical_name_mismatch",
                "expected": expected_name,
            }, 100)
        if row.get("name_alts") != expected_name_alts:
            counters["name_alts_mismatch"] += 1
            _sidecar(sidecars, "name_quality", {
                **_brief(row),
                "issue": "name_alts_mismatch",
                "expected": expected_name_alts,
            }, 100)

        sanitize_counts = Counter()
        expected_metadata = builder._merge_metadata(source_refs, src_data, arch_name_set, sanitize_counts)
        expected_metadata = _apply_portfolio_location_override(expected_metadata, buildings, arch_name_set)
        for field, meta_field in (
            ("primary_country", "country"),
            ("primary_city", "city"),
            ("website", "website"),
            ("phone", "phone"),
            ("description", "description"),
            ("office_locations", "office_locations"),
            ("logo_url", "logo_url"),
            ("source_urls", "source_urls"),
            ("source_descriptions", "source_descriptions"),
        ):
            if row.get(field) != expected_metadata.get(meta_field):
                counters[f"metadata_{field}_priority_mismatch"] += 1
                _sidecar(sidecars, "metadata_priority_mismatch", {
                    **_brief(row),
                    "field": field,
                    "actual": row.get(field),
                    "expected": expected_metadata.get(meta_field),
                }, 100)

        if BRAND_SUFFIX_RE.search(row.get("canonical_name") or ""):
            counters["canonical_name_brand_suffix"] += 1
            _sidecar(sidecars, "name_quality", {**_brief(row), "issue": "canonical_name_brand_suffix"})
        for alt in row.get("name_alts") or []:
            if BRAND_SUFFIX_RE.search(str(alt)):
                warnings["name_alt_brand_suffix"] += 1

        country = row.get("primary_country")
        if isinstance(country, str) and DIRTY_COUNTRY_RE.search(country):
            warnings["primary_country_dirty_candidate"] += 1
            _sidecar(sidecars, "location_quality", {**_brief(row), "issue": "dirty_country"})
        if isinstance(country, str) and country:
            norm_country = _normalize_country_full(country)
            if norm_country not in VALID_COUNTRIES:
                counters["primary_country_invalid_or_dirty"] += 1
                _sidecar(sidecars, "location_quality", {
                    **_brief(row),
                    "issue": "primary_country_invalid_or_dirty",
                    "normalized": norm_country,
                }, 500)
        city = row.get("primary_city")
        if isinstance(city, str) and city:
            norm_city_as_country = _normalize_country_full(city)
            if norm_city_as_country in VALID_COUNTRIES:
                norm_country = _normalize_country_full(country)
                if norm_city_as_country == norm_country and norm_city_as_country in CITY_STATE_COUNTRIES:
                    warnings["primary_city_equals_city_state_country"] += 1
                else:
                    counters["primary_city_is_country_name"] += 1
                    _sidecar(sidecars, "location_quality", {
                        **_brief(row),
                        "issue": "primary_city_is_country_name",
                        "normalized": norm_city_as_country,
                    }, 500)
        if row.get("is_recommendable") and not country:
            warnings["recommendable_missing_primary_country"] += 1
            _sidecar(sidecars, "location_quality", {**_brief(row), "issue": "recommendable_missing_country"}, 200)
        if row.get("is_recommendable") and not row.get("primary_city"):
            warnings["recommendable_missing_primary_city"] += 1

        website = row.get("website")
        if isinstance(website, str) and website and not re.match(r"^https?://", website, re.I):
            warnings["website_missing_scheme"] += 1
            _sidecar(sidecars, "url_quality", {**_brief(row), "issue": "website_missing_scheme", "website": website}, 200)

        social = row.get("social_links") or {}
        for key, value in social.items():
            if isinstance(value, str) and SOURCE_SOCIAL_RE.search(value):
                counters["social_link_source_brand_leak"] += 1
                _sidecar(sidecars, "social_link_leak", {
                    **_brief(row),
                    "platform": key,
                    "url": value,
                }, 500)

        source_urls = row.get("source_urls") or {}
        resolved_sources = set(expected_metadata.get("source_urls") or {})
        ref_sources = {src for src, ids in source_refs.items() if ids}
        missing_profile_url_sources = sorted(ref_sources - resolved_sources - {"metalocus"})
        if missing_profile_url_sources:
            warnings["source_ref_without_profile_url"] += len(missing_profile_url_sources)
            _sidecar(sidecars, "source_url_gap", {
                **_brief(row),
                "missing_sources": missing_profile_url_sources,
                "source_urls": source_urls,
            }, 200)
        for src, value in source_urls.items():
            if not isinstance(value, str):
                warnings["source_url_value_not_string"] += 1
                _sidecar(sidecars, "source_url_shape", {
                    **_brief(row),
                    "source": src,
                    "value": value,
                }, 100)

    missing_architect_rows = sorted(active_with_buildings - seen_ids)
    for arch_id in missing_architect_rows[:100]:
        _sidecar(sidecars, "registry_mismatch", {
            "canonical_arch_id": arch_id,
            "issue": "active_registry_with_buildings_missing_from_output",
            "source_refs": (active_registry.get(arch_id) or {}).get("source_refs"),
            "n_buildings": len(arch_to_rows.get(arch_id) or []),
        })
    if missing_architect_rows:
        counters["active_registry_with_buildings_missing_from_output"] = len(missing_architect_rows)

    extra_architect_rows = sorted(seen_ids - active_with_buildings)
    if extra_architect_rows:
        counters["output_architect_without_active_buildings"] = len(extra_architect_rows)

    for name, rows in sidecars.items():
        _write_jsonl(SIDECAR_PREFIX.with_name(f"{SIDECAR_PREFIX.name}_{name}.jsonl"), rows)

    hard_keys = [
        "duplicate_or_missing_arch_id",
        "missing_required_fields",
        "row_not_active_registry",
        "source_refs_not_registry_exact",
        "row_has_no_reverse_index_buildings",
        "active_registry_with_buildings_missing_from_output",
        "output_architect_without_active_buildings",
        "building_ids_mismatch",
        "n_buildings_mismatch",
        "n_buildings_publishable_mismatch",
        "countries_mismatch",
        "cities_mismatch",
        "top_programs_mismatch",
        "top_styles_mismatch",
        "top_color_tones_mismatch",
        "top_atmospheres_mismatch",
        "top_materials_mismatch",
        "top_typologies_mismatch",
        "top_arch_elements_mismatch",
        "feature_distribution_mismatch",
        "earliest_project_year_mismatch",
        "latest_project_year_mismatch",
        "hero_building_id_mismatch",
        "embedding_bad_dim",
        "embedding_nonfinite",
        "embedding_expected_none",
        "embedding_mean_mismatch",
        "is_recommendable_mismatch",
        "n_sources_mismatch",
        "confidence_tier_mismatch",
        "canonical_name_mismatch",
        "name_alts_mismatch",
        "metadata_primary_country_priority_mismatch",
        "metadata_primary_city_priority_mismatch",
        "metadata_website_priority_mismatch",
        "metadata_phone_priority_mismatch",
        "metadata_description_priority_mismatch",
        "metadata_office_locations_priority_mismatch",
        "metadata_logo_url_priority_mismatch",
        "metadata_source_urls_priority_mismatch",
        "metadata_source_descriptions_priority_mismatch",
        "canonical_name_brand_suffix",
        "social_link_source_brand_leak",
        "primary_country_invalid_or_dirty",
        "primary_city_is_country_name",
    ]
    hard_total = sum(counters.get(key, 0) for key in hard_keys)

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if hard_total == 0 else "FAIL",
        "hard_total": hard_total,
        "hard_keys": hard_keys,
        "input": _display_path(args.input),
        "buildings": _display_path(args.buildings),
        "counts": {
            "architects_rows": len(architects),
            "registry_total": len(registry),
            "registry_active": len(active_registry),
            "active_registry_with_buildings": len(active_with_buildings),
            **building_stats,
        },
        "counters": dict(sorted(counters.items())),
        "warnings": dict(sorted(warnings.items())),
        "sidecars": {
            name: {
                "count": len(rows),
                "path": _display_path(SIDECAR_PREFIX.with_name(f"{SIDECAR_PREFIX.name}_{name}.jsonl")),
            }
            for name, rows in sorted(sidecars.items())
        },
    }


def _write_md(report: dict[str, Any], path: Path) -> None:
    counters = Counter(report["counters"])
    warnings = Counter(report["warnings"])
    lines = [
        "# canonical_v2_architects Codex Audit",
        "",
        f"Generated: {report['generated']}",
        "",
        "## Verdict",
        "",
        f"- Status: `{report['status']}`",
        f"- Hard total: `{report['hard_total']}`",
        f"- Architect rows: `{report['counts']['architects_rows']}`",
        f"- Active registry with buildings: `{report['counts']['active_registry_with_buildings']}`",
        f"- Buildings scanned: `{report['counts']['buildings_scanned']}`",
        "",
        "## Hard Findings",
        "",
    ]
    found = False
    for key in report["hard_keys"]:
        val = counters.get(key, 0)
        if val:
            found = True
            lines.append(f"- `{key}`: {val}")
    if not found:
        lines.append("- None.")
    lines.extend(["", "## Warnings", ""])
    for key, val in warnings.most_common(30):
        lines.append(f"- `{key}`: {val}")
    lines.extend(["", "## Sidecars", ""])
    for name, meta in sorted((report.get("sidecars") or {}).items()):
        lines.append(f"- `{name}`: {meta['count']} -> `{meta['path']}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit canonical_v2 architects output.")
    parser.add_argument("--input", type=Path, default=DEFAULT_ARCHITECTS)
    parser.add_argument("--buildings", type=Path, default=DEFAULT_BUILDINGS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()

    report = _audit(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_md(report, args.md)
    print(json.dumps({
        "status": report["status"],
        "hard_total": report["hard_total"],
        "counts": report["counts"],
        "hard_findings": {k: report["counters"].get(k, 0) for k in report["hard_keys"] if report["counters"].get(k, 0)},
        "top_warnings": dict(Counter(report["warnings"]).most_common(15)),
        "sidecars": {k: v["count"] for k, v in report["sidecars"].items()},
        "report": _display_path(args.report),
        "md": _display_path(args.md),
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
