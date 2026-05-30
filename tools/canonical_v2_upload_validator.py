#!/usr/bin/env python3
"""Read-only validator for the proposed canonical_v2_buildings upload path.

This tool does not import upload code and does not open Neon/R2 connections.
It maps the completed strict canonical JSON into the shape we expect to upload
later, validates the mapping, and writes a compact report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import vocab  # noqa: E402

DEFAULT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_upload_dry_run.resume10_complete.json"
EXPECTED_IMAGE_TYPES = {"exterior", "interior", "drawing", "aerial", "detail"}
VALID_TIERS = {"T1", "T2", "T3"}
PLACEHOLDER_PATTERNS = ("facebook-default-thumb", "img-placeholder")

REQUIRED_TEXT_FIELDS = (
    "canonical_bld_id",
    "name",
    "program",
    "style",
    "color_tone",
    "atmosphere",
    "visual_description",
    "confidence_tier",
)

# Mirrors tools/audit_make_db_for_make_web.py::MATERIAL_TAXONOMY_NOISE. Spatial
# elements / non-material terms that pollute `material_visual` filters. Stripped
# at load time so cleanup is idempotent across canonical → Neon reloads.
MATERIAL_TAXONOMY_NOISE = frozenset({
    "balconies", "columns", "courtyard", "courtyards", "curtains",
    "facade", "facade cladding", "furniture", "garden", "garden planting",
    "grass", "greenery", "green roof", "landscape", "landscaping",
    "light", "lighting", "planting", "plants", "skylights",
    "stairs", "terrace", "terraces", "trees", "vegetation",
    "walls", "water", "window frames", "windows",
})


def filter_material_noise(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, str) and v.strip().lower() not in MATERIAL_TAXONOMY_NOISE]

GENERIC_NAME_TOKENS = {
    "a",
    "an",
    "and",
    "apartment",
    "apartments",
    "building",
    "casa",
    "center",
    "centre",
    "gallery",
    "garden",
    "home",
    "house",
    "housing",
    "in",
    "of",
    "office",
    "pavilion",
    "private",
    "project",
    "residence",
    "school",
    "social",
    "summer",
    "the",
    "tower",
    "villa",
}


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list, dict)) and not value:
        return False
    return True


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def is_genericish_name(value: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    if not tokens:
        return False
    distinctive = [t for t in tokens if t not in GENERIC_NAME_TOKENS and len(t) > 1]
    return len(distinctive) <= 1


EMPTY_MATERIAL_REASON = "material_noise_only"


def map_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return the exact row shape proposed for canonical_v2_buildings."""
    raw_material = row.get("material_visual") or []
    material_visual = filter_material_noise(raw_material)
    publishability_reasons = list(row.get("publishability_reasons") or [])
    is_publishable = bool(row.get("is_publishable"))
    if raw_material and not material_visual:
        # Building had only noise terms for material — unpublish + flag reason.
        is_publishable = False
        if EMPTY_MATERIAL_REASON not in publishability_reasons:
            publishability_reasons.append(EMPTY_MATERIAL_REASON)
    return {
        "canonical_bld_id": row.get("canonical_bld_id"),
        "name": row.get("name"),
        "names_alts": row.get("names_alts") or [],
        "location_city": row.get("location_city"),
        "location_country": row.get("location_country"),
        "project_year": row.get("project_year"),
        "architect_canonical_ids": row.get("architect_canonical_ids") or [],
        "architect_names": row.get("architect_names") or [],
        "architects_text": row.get("architects_text"),
        "program": row.get("program"),
        "style": row.get("style"),
        "color_tone": row.get("color_tone"),
        "atmosphere": row.get("atmosphere"),
        "material_visual": material_visual,
        "visual_description": row.get("visual_description"),
        "image_derived": row.get("image_derived") or {},
        "covers_by_type": row.get("covers_by_type") or {},
        "all_images": row.get("all_images") or [],
        "best_image_per_cluster": row.get("best_image_per_cluster") or {},
        "source_refs": row.get("source_refs") or {},
        "source_urls": row.get("source_urls") or {},
        "identity_source": row.get("identity_source"),
        "confidence_tier": row.get("confidence_tier"),
        "n_sources": row.get("n_sources"),
        "cover_image_url_default": row.get("cover_image_url_default"),
        "display_cover_url": row.get("display_cover_url"),
        "is_publishable": is_publishable,
        "publishability_reasons": publishability_reasons,
        "needs_image_derived_backfill": bool(row.get("needs_image_derived_backfill")),
        "typology_primary": row.get("typology_primary"),
        "typology_primary_source": row.get("typology_primary_source"),
        "typology_tags": row.get("typology_tags") or [],
        "architectural_elements": row.get("architectural_elements") or [],
        "source_categories": row.get("source_categories") or {},
        "year_kind": row.get("year_kind") or "unknown",
        "embedding": row.get("embedding"),
    }


def _embedding_is_valid(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 384
        and all(isinstance(v, (int, float)) for v in value)
    )


def _covers_are_valid(value: Any) -> bool:
    return isinstance(value, dict) and set(value.keys()) == EXPECTED_IMAGE_TYPES


def _source_refs_are_valid(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    for source, ids in value.items():
        if not isinstance(source, str) or not source:
            return False
        if not isinstance(ids, list) or not ids:
            return False
        if any(not isinstance(source_id, (str, int)) or str(source_id) == "" for source_id in ids):
            return False
    return True


def _source_urls_are_valid(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    for source, urls in value.items():
        if not isinstance(source, str) or not source:
            return False
        if not isinstance(urls, list) or not urls:
            return False
        for url in urls:
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                return False
    return True


def _compact_sample(mapped: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_bld_id": mapped["canonical_bld_id"],
        "name": mapped["name"],
        "location_country": mapped["location_country"],
        "project_year": mapped["project_year"],
        "architect_names": mapped["architect_names"][:3],
        "n_sources": mapped["n_sources"],
        "source_refs": mapped["source_refs"],
        "source_urls": mapped["source_urls"],
        "covers_present": sorted(k for k, v in mapped["covers_by_type"].items() if v),
        "all_images_count": len(mapped["all_images"]),
        "display_cover_url": mapped["display_cover_url"],
        "is_publishable": mapped["is_publishable"],
        "publishability_reasons": mapped["publishability_reasons"],
        "embedding_dim": len(mapped["embedding"]) if isinstance(mapped["embedding"], list) else None,
    }


def _row_has_placeholder(mapped: dict[str, Any]) -> bool:
    """True if any image field of the row carries a known placeholder URL."""
    def ph(value: Any) -> bool:
        url = value.get("url", "") if isinstance(value, dict) else str(value or "")
        return any(p in url for p in PLACEHOLDER_PATTERNS)
    if ph(mapped.get("cover_image_url_default")) or ph(mapped.get("display_cover_url")):
        return True
    if any(ph(im) for im in mapped.get("all_images") or []):
        return True
    if any(ph(v) for v in (mapped.get("covers_by_type") or {}).values()):
        return True
    if any(ph(v) for v in (mapped.get("best_image_per_cluster") or {}).values()):
        return True
    return False


def validate_rows(rows: Iterable[dict[str, Any]], *, sample_limit: int = 5) -> dict[str, Any]:
    failures: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    seen: set[str] = set()
    duplicate_pks: list[str] = []
    by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    display_covers: defaultdict[str, list[str]] = defaultdict(list)
    samples: list[dict[str, Any]] = []
    total = 0
    publishable_rows = 0
    nonpublishable_rows = 0

    for raw in rows:
        total += 1
        mapped = map_row(raw)
        cid = mapped.get("canonical_bld_id")
        is_publishable = mapped.get("is_publishable")

        if len(samples) < sample_limit:
            samples.append(_compact_sample(mapped))

        for field in REQUIRED_TEXT_FIELDS:
            if not _present(mapped.get(field)):
                failures["missing_required"] += 1
                break

        if not isinstance(cid, str) or not cid:
            failures["bad_pk"] += 1
        elif cid in seen:
            failures["duplicate_pk"] += 1
            duplicate_pks.append(cid)
        else:
            seen.add(cid)

        if mapped.get("confidence_tier") not in VALID_TIERS:
            failures["invalid_confidence_tier"] += 1
        if not isinstance(mapped.get("material_visual"), list):
            failures["bad_material_visual"] += 1
        elif not mapped["material_visual"] and EMPTY_MATERIAL_REASON not in (mapped.get("publishability_reasons") or []):
            # Allow empty material_visual only when the row is flagged as
            # noise-only (auto-unpublished). All other empties remain failures.
            failures["bad_material_visual"] += 1
        if not _covers_are_valid(mapped.get("covers_by_type")):
            failures["bad_covers_by_type"] += 1
        if not _source_refs_are_valid(mapped.get("source_refs")):
            failures["bad_source_refs"] += 1
        if not _source_urls_are_valid(mapped.get("source_urls")):
            failures["bad_source_urls"] += 1
        if not _embedding_is_valid(mapped.get("embedding")):
            failures["bad_embedding"] += 1
        if any(t not in vocab.TYPOLOGY for t in mapped.get("typology_tags") or []):
            failures["bad_typology_tags"] += 1
        if any(e not in vocab.ARCHITECTURAL_ELEMENT
               for e in mapped.get("architectural_elements") or []):
            failures["bad_architectural_elements"] += 1
        typ_primary = mapped.get("typology_primary")
        if typ_primary is not None and typ_primary not in vocab.TYPOLOGY:
            failures["bad_typology_primary"] += 1
        if not isinstance(mapped.get("source_categories"), dict):
            failures["bad_source_categories"] += 1
        if not isinstance(is_publishable, bool):
            failures["bad_publishable_flag"] += 1
        elif is_publishable:
            publishable_rows += 1
            if not mapped.get("all_images") or not mapped.get("display_cover_url"):
                failures["publishable_missing_image"] += 1
        else:
            nonpublishable_rows += 1
            warnings["nonpublishable_rows"] += 1

        if is_publishable:
            if _row_has_placeholder(mapped):
                failures["placeholder_in_publishable"] += 1
            dcu = mapped.get("display_cover_url")
            if dcu:
                display_covers[str(dcu)].append(str(cid))

        if not mapped.get("all_images"):
            warnings["empty_all_images"] += 1
        if not mapped.get("cover_image_url_default"):
            warnings["missing_cover_image_url_default"] += 1
        if not mapped.get("display_cover_url"):
            warnings["missing_display_cover_url"] += 1
        if not mapped.get("location_country"):
            warnings["missing_location_country"] += 1
        if not mapped.get("location_city"):
            warnings["missing_location_city"] += 1
        if mapped.get("project_year") is None:
            warnings["missing_project_year"] += 1
        if not mapped.get("architect_canonical_ids"):
            warnings["missing_architect_canonical_ids"] += 1
        if mapped.get("needs_image_derived_backfill"):
            warnings["needs_image_derived_backfill"] += 1
        if is_genericish_name(str(mapped.get("name") or "")):
            warnings["genericish_name_rows"] += 1

        name = normalize_name(str(mapped.get("name") or ""))
        if name:
            by_name[name].append(
                {
                    "canonical_bld_id": cid,
                    "country": mapped.get("location_country"),
                    "city": mapped.get("location_city"),
                    "year": mapped.get("project_year"),
                    "architect_names": mapped.get("architect_names")[:2],
                    "n_sources": mapped.get("n_sources"),
                }
            )

        try:
            json.dumps(mapped, ensure_ascii=False)
        except (TypeError, ValueError):
            failures["bad_json_serialization"] += 1

    duplicate_name_groups = {name: group for name, group in by_name.items() if len(group) > 1}
    generic_duplicate_name_groups = {
        name: group for name, group in duplicate_name_groups.items() if is_genericish_name(name)
    }
    warnings["duplicate_name_groups"] = len(duplicate_name_groups)
    warnings["generic_duplicate_name_groups"] = len(generic_duplicate_name_groups)

    reused_covers = {u: cids for u, cids in display_covers.items() if len(cids) > 1}
    if reused_covers:
        failures["display_cover_url_reused"] = sum(len(c) for c in reused_covers.values())

    duplicate_name_samples = [
        {"name": name, "count": len(group), "rows": group[:8]}
        for name, group in sorted(
            duplicate_name_groups.items(), key=lambda item: (-len(item[1]), item[0])
        )[:25]
    ]

    return {
        "status": "FAIL" if failures else "PASS",
        "total_rows": total,
        "publishable_rows": publishable_rows,
        "nonpublishable_rows": nonpublishable_rows,
        "unique_pk": len(seen),
        "failures": dict(failures),
        "warnings": dict(warnings),
        "duplicate_pk_samples": duplicate_pks[:25],
        "duplicate_name_samples": duplicate_name_samples,
        "sample_rows": samples,
    }


def iter_buildings(path: Path, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Stream objects from the top-level `buildings` array.

    The embedded canonical file is about 1GB, so this avoids materializing the
    full file for validation.
    """
    decoder = json.JSONDecoder()
    key = '"buildings"'
    with path.open("r", encoding="utf-8") as f:
        buffer = ""
        found = False
        yielded = 0

        while not found:
            chunk = f.read(1024 * 1024)
            if not chunk:
                raise ValueError(f"{path} has no top-level buildings array")
            buffer += chunk
            key_idx = buffer.find(key)
            if key_idx == -1:
                buffer = buffer[-len(key) :]
                continue
            bracket_idx = buffer.find("[", key_idx + len(key))
            if bracket_idx == -1:
                continue
            buffer = buffer[bracket_idx + 1 :]
            found = True

        while True:
            stripped = buffer.lstrip()
            buffer = stripped
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            if limit is not None and yielded >= limit:
                return

            try:
                obj, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    raise
                buffer += chunk
                continue

            if not isinstance(obj, dict):
                raise ValueError("buildings array contains a non-object item")
            yielded += 1
            yield obj
            buffer = buffer[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate canonical_v2 upload mapping")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--sample-limit", type=int, default=5)
    args = parser.parse_args()

    rows = iter_buildings(args.input, limit=args.limit)
    report = validate_rows(rows, sample_limit=args.sample_limit)
    report.update(
        {
            "input": str(args.input),
            "limit": args.limit,
            "table": "canonical_v2_buildings",
            "writes": "none; read-only dry-run",
        }
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps({k: report[k] for k in ("status", "total_rows", "unique_pk", "failures", "warnings")}, indent=2, ensure_ascii=False))
    print(f"report: {args.report}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
