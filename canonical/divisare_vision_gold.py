"""Deterministic Divisare Vision N100 candidate and gold-manifest contracts."""

from __future__ import annotations

import hashlib
import heapq
import os
import re
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from canonical.divisare_image_smoke import (
    _basic_lane,
    canonical_json,
    file_sha256,
    fixed_derivative_url,
    open_source_readonly,
)


CANDIDATE_MANIFEST_VERSION = "divisare-vision-gold-candidates-v1.0.0"
SELECTION_VERSION = "divisare-vision-gold-candidate-selection-v1.1.0"
REVIEWED_POOL_VERSION = "divisare-vision-reviewed-pool-v1.0.0"
GOLD_MANIFEST_VERSION = "divisare-vision-gold-manifest-v1.0.0"

SOURCE_PROFILE = "c_limit,f_jpg,h_2048,q_92,w_2048"
REVIEW_PROFILE = "c_limit,f_jpg,h_1024,q_85,w_1024"
IDENTITY_PROFILE = "pillow-exif-rgb-white-c_limit512-no-upscale-v1.0.0"
PIXEL_HASH_VERSION = "sha256-rgb0-u32be-width-height-rgb-bytes-v1.0.0"
PHASH_VERSION = "imagehash-phash-hash_size16-highfreq4-v1.0.0"

CLASSES = ("exterior", "interior", "drawing", "aerial", "detail")
SCARCITY_ORDER = ("drawing", "aerial", "detail", "interior", "exterior")
GENERATION_GROUPS = ("modern", "legacy")

POOL_TARGETS: dict[str, dict[str, int]] = {
    "exterior": {"modern": 64, "legacy": 16},
    "interior": {"modern": 64, "legacy": 16},
    "drawing": {"modern": 64, "legacy": 16},
    "detail": {"modern": 64, "legacy": 16},
    "aerial": {"modern": 200, "legacy": 40},
}

FINAL_CELL_QUOTAS: dict[tuple[str, str], int] = {
    ("modern", "clear"): 13,
    ("modern", "boundary"): 3,
    ("legacy", "clear"): 3,
    ("legacy", "boundary"): 1,
}

AERIAL_TAGS = frozenset(
    {
        "building-in-landscape",
        "squares-and-streets",
        "urban-parks",
        "landscape-design",
        "outdoor-sports-fields",
        "archaeological-parks",
        "waterfronts-and-coastal-redevelopments",
        "stadiums",
        "airports",
        "tower-blocks-and-skyscrapers",
        "footbridges",
        "traffic-bridges",
    }
)

EXTERIOR_TAGS = frozenset(
    {
        "building-in-landscape",
        "building-in-urban-context",
        "urban-facades",
        "bricks-facades",
        "glass-facades",
        "wooden-facades",
        "stone-facades",
        "metal-claddings",
        "facade-cladding-systems",
        "slat-facades",
        "entrances",
        "sloping-roofs",
        "courtyards",
        "patios",
        "outdoor-stairs",
        "waterfronts-and-coastal-redevelopments",
        "squares-and-streets",
    }
)

_DRAWING_IMAGE_HINTS = frozenset({"drawing", "section"})
_DRAWING_ARTICLE_HINTS = frozenset({"plan", "section", "drawing"})
_SPECIAL_EXTERIOR_HINTS = frozenset(
    {
        "plan",
        "section",
        "drawing",
        "construction detail",
        "model",
        "notebook/sketch",
        "portrait",
    }
)
_EXTERIOR_KINDS = frozenset({"photo_feature", "unresolved", "mixed_feature"})
_AERIAL_FALLBACK_KINDS = frozenset({"photo_feature", "unresolved"})
_EXTERIOR_EXCLUDED_ALBUMS = frozenset(
    {"private-interiors", "public-interiors", "plans-details", "ideas"}
)
_FILENAME_DRAWING = ("plan", "section", "elevation", "axon", "diagram")
_FILENAME_AERIAL_RE = re.compile(
    r"(?<![a-z0-9])(?:aerial|drone|bird(?:s|'s)?[- ]?(?:eye|view))(?![a-z0-9])"
)

BASE_QUERY = """
SELECT ia.asset_key,ia.first_seen_article_id AS article_id,bi.building_id,
       ia.url_generation,ia.original_filename,bi.representative_url AS source_url,
       CASE bi.role_rank WHEN 0 THEN 'cover' ELSE 'gallery' END AS role,
       bi.first_position AS position,ak.article_kind,ak.status,sa.location_country
FROM building_images_materialized_v2_3 bi
JOIN image_assets ia USING(asset_key)
JOIN source_articles sa ON sa.article_id=ia.first_seen_article_id
JOIN article_kind_resolution_v2 ak ON ak.article_id=ia.first_seen_article_id
ORDER BY ia.asset_key
"""


@dataclass(frozen=True)
class CandidateEvidence:
    asset_key: str
    article_id: int
    building_id: str
    url_generation: str
    generation_group: str
    original_filename: str | None
    source_url: str
    role: str
    position: int
    article_kind: str
    kind_status: str
    country: str
    discovery_class: str
    discovery_score: int
    weak_hints: tuple[str, ...]
    stable_order: str


def _stable_hex(*values: str) -> str:
    payload = "\0".join((SELECTION_VERSION, *values)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_set_map(conn: Any, query: str) -> dict[Any, frozenset[str]]:
    values: dict[Any, set[str]] = {}
    for key, value in conn.execute(query):
        if value is not None:
            values.setdefault(key, set()).add(str(value).strip().casefold())
    return {key: frozenset(sorted(items)) for key, items in values.items()}


def _generation_group(url_generation: str) -> str:
    return "modern" if url_generation == "cloudinary_public_id" else "legacy"


def _filename_tokens(filename: str | None) -> str:
    return unquote(filename or "").casefold().replace("_", "-")


def _score_classes(
    *,
    filename: str,
    role: str,
    position: int,
    article_kind: str,
    image_hints: frozenset[str],
    article_hints: frozenset[str],
    albums: frozenset[str],
    tags: frozenset[str],
) -> dict[str, tuple[int, tuple[str, ...]]]:
    scored: dict[str, tuple[int, tuple[str, ...]]] = {}

    def offer(label: str, score: int, evidence: Iterable[str]) -> None:
        hints = tuple(sorted(set(evidence)))
        prior = scored.get(label)
        if prior is None or score > prior[0] or (score == prior[0] and hints < prior[1]):
            scored[label] = (score, hints)

    if "exterior" in image_hints:
        offer("exterior", 100, ("image_hint:exterior",))
    elif "exterior" in filename:
        offer("exterior", 95, ("filename_token:exterior",))

    if "interior" in image_hints:
        offer("interior", 100, ("image_hint:interior",))
    elif "interior" in filename:
        offer("interior", 95, ("filename_token:interior",))
    elif albums & {"private-interiors", "public-interiors"}:
        offer(
            "interior",
            65,
            ("article_album:" + value for value in albums & {"private-interiors", "public-interiors"}),
        )
    elif any("interior" in tag for tag in tags):
        offer("interior", 55, ("article_tag:interior",))

    if image_hints & _DRAWING_IMAGE_HINTS:
        offer(
            "drawing",
            100,
            ("image_hint:" + value for value in image_hints & _DRAWING_IMAGE_HINTS),
        )
    elif any(value in filename for value in _FILENAME_DRAWING):
        offer("drawing", 95, ("filename_token:drawing",))
    elif article_hints & _DRAWING_ARTICLE_HINTS:
        offer(
            "drawing",
            70,
            ("article_hint:" + value for value in article_hints & _DRAWING_ARTICLE_HINTS),
        )
    elif article_kind == "drawing_feature":
        offer("drawing", 55, ("article_kind:drawing_feature",))

    if "detail" in image_hints:
        offer("detail", 100, ("image_hint:detail",))
    elif "detail" in filename:
        offer("detail", 95, ("filename_token:detail",))
    elif "construction detail" in article_hints:
        offer("detail", 70, ("article_hint:construction detail",))
    elif "god-is-in-the-details" in tags or any(
        tag.startswith("construction-details-") for tag in tags
    ):
        offer("detail", 55, ("article_tag:construction-detail",))

    if "aerial" in image_hints:
        offer("aerial", 100, ("image_hint:aerial",))
    elif _FILENAME_AERIAL_RE.search(filename):
        offer("aerial", 95, ("filename_token:aerial",))
    aerial_fallback = (
        article_kind in _AERIAL_FALLBACK_KINDS
        and not image_hints
        and not (article_hints & _SPECIAL_EXTERIOR_HINTS)
        and not (albums & _EXTERIOR_EXCLUDED_ALBUMS)
        and not any(
            value in filename
            for value in (*_FILENAME_DRAWING, "interior", "exterior", "detail")
        )
    )
    if tags & AERIAL_TAGS and role == "cover" and aerial_fallback:
        offer(
            "aerial",
            55,
            ("article_tag:" + value for value in tags & AERIAL_TAGS),
        )
    elif (
        tags & AERIAL_TAGS
        and role == "gallery"
        and position <= 3
        and aerial_fallback
    ):
        offer(
            "aerial",
            45,
            ("early_gallery_tag:" + value for value in tags & AERIAL_TAGS),
        )

    if tags & EXTERIOR_TAGS and role == "cover":
        offer(
            "exterior",
            65,
            ("cover_article_tag:" + value for value in tags & EXTERIOR_TAGS),
        )
    exterior_fallback = (
        role == "cover"
        and article_kind in _EXTERIOR_KINDS
        and not (article_hints & _SPECIAL_EXTERIOR_HINTS)
        and not (albums & _EXTERIOR_EXCLUDED_ALBUMS)
        and "wip-work-in-progress" not in tags
    )
    if exterior_fallback:
        offer("exterior", 45, ("filtered_cover",))
    return scored


def candidate_manifest_payload(source_db: Path) -> dict[str, Any]:
    """Build a deterministic, over-sampled candidate manifest without image I/O."""
    source_db = source_db.resolve()
    source_sha_before = file_sha256(source_db)
    per_country_keep = 8
    heaps: dict[tuple[str, str, str], list[tuple[int, int, str, CandidateEvidence]]] = {}

    with closing(open_source_readonly(source_db)) as conn:
        conn.execute("BEGIN")
        image_hints = _load_set_map(
            conn, "SELECT asset_key,LOWER(hint) FROM image_url_hints"
        )
        article_hints = _load_set_map(
            conn, "SELECT article_id,LOWER(content_hint) FROM v_article_content_hints"
        )
        tags = _load_set_map(
            conn, "SELECT article_id,LOWER(tag_slug) FROM article_tags"
        )
        albums = _load_set_map(
            conn,
            """
            SELECT at.article_id,LOWER(st.album_slug)
            FROM article_tags at JOIN source_tags st USING(tag_slug)
            WHERE st.album_slug IS NOT NULL
            """,
        )

        previous_asset_key: str | None = None
        for row in conn.execute(BASE_QUERY):
            asset_key = str(row[0])
            if asset_key == previous_asset_key:
                raise RuntimeError(
                    "candidate source asset maps to multiple materialized buildings: %s"
                    % asset_key
                )
            previous_asset_key = asset_key
            original_filename = str(row[4]) if row[4] is not None else None
            source_url = str(row[5])
            if _basic_lane(original_filename, source_url) != "raster":
                continue
            article_id = int(row[1])
            generation = _generation_group(str(row[3]))
            country = str(row[10] or "__missing__")
            scored = _score_classes(
                filename=_filename_tokens(original_filename),
                role=str(row[6]),
                position=int(row[7]),
                article_kind=str(row[8]),
                image_hints=image_hints.get(asset_key, frozenset()),
                article_hints=article_hints.get(article_id, frozenset()),
                albums=albums.get(article_id, frozenset()),
                tags=tags.get(article_id, frozenset()),
            )
            for discovery_class, (score, weak_hints) in scored.items():
                stable = _stable_hex(discovery_class, asset_key)
                candidate = CandidateEvidence(
                    asset_key=asset_key,
                    article_id=article_id,
                    building_id=str(row[2]),
                    url_generation=str(row[3]),
                    generation_group=generation,
                    original_filename=original_filename,
                    source_url=source_url,
                    role=str(row[6]),
                    position=int(row[7]),
                    article_kind=str(row[8]),
                    kind_status=str(row[9]),
                    country=country,
                    discovery_class=discovery_class,
                    discovery_score=score,
                    weak_hints=weak_hints,
                    stable_order=stable,
                )
                bucket_key = (discovery_class, generation, country)
                heap = heaps.setdefault(bucket_key, [])
                quality = (score, -int(stable, 16), asset_key, candidate)
                if len(heap) < per_country_keep:
                    heapq.heappush(heap, quality)
                elif quality[:3] > heap[0][:3]:
                    heapq.heapreplace(heap, quality)

    reservoirs: dict[tuple[str, str], list[CandidateEvidence]] = {}
    for (label, generation, _country), heap in heaps.items():
        reservoirs.setdefault((label, generation), []).extend(item[3] for item in heap)
    for values in reservoirs.values():
        values.sort(key=lambda item: (-item.discovery_score, item.stable_order))

    chosen: dict[tuple[str, str], list[tuple[CandidateEvidence, bool]]] = {}
    used_assets: set[str] = set()
    used_articles: set[int] = set()
    used_buildings: set[str] = set()
    for label in SCARCITY_ORDER:
        for generation in GENERATION_GROUPS:
            target = POOL_TARGETS[label][generation]
            values = reservoirs.get((label, generation), [])
            country_counts: dict[str, int] = {}
            selected: list[tuple[CandidateEvidence, bool]] = []

            def eligible(item: CandidateEvidence) -> bool:
                return (
                    item.asset_key not in used_assets
                    and item.article_id not in used_articles
                    and item.building_id not in used_buildings
                )

            for item in values:
                if len(selected) >= target:
                    break
                if not eligible(item) or country_counts.get(item.country, 0) >= 4:
                    continue
                selected.append((item, False))
                country_counts[item.country] = country_counts.get(item.country, 0) + 1
                used_assets.add(item.asset_key)
                used_articles.add(item.article_id)
                used_buildings.add(item.building_id)
            if len(selected) < target:
                for item in values:
                    if len(selected) >= target:
                        break
                    if not eligible(item):
                        continue
                    selected.append((item, True))
                    country_counts[item.country] = country_counts.get(item.country, 0) + 1
                    used_assets.add(item.asset_key)
                    used_articles.add(item.article_id)
                    used_buildings.add(item.building_id)
            if len(selected) != target:
                raise RuntimeError(
                    "candidate quota shortfall for %s/%s: expected %d, got %d"
                    % (label, generation, target, len(selected))
                )
            chosen[(label, generation)] = selected

    candidates: list[dict[str, Any]] = []
    class_ranks = {label: 0 for label in CLASSES}
    for label in CLASSES:
        for generation in GENERATION_GROUPS:
            for item, cap_fallback in chosen[(label, generation)]:
                class_ranks[label] += 1
                candidates.append(
                    {
                        "candidate_id": "candidate-%04d" % (len(candidates) + 1),
                        "candidate_rank": len(candidates) + 1,
                        "class_rank": class_ranks[label],
                        "discovery_class": label,
                        "discovery_score": item.discovery_score,
                        "generation_group": item.generation_group,
                        "asset_key": item.asset_key,
                        "article_id": item.article_id,
                        "building_id": item.building_id,
                        "source_url": item.source_url,
                        "request_url": fixed_derivative_url(item.source_url, SOURCE_PROFILE),
                        "review_url": fixed_derivative_url(item.source_url, REVIEW_PROFILE),
                        "url_generation": item.url_generation,
                        "original_filename": item.original_filename,
                        "role": item.role,
                        "position": item.position,
                        "article_kind": item.article_kind,
                        "kind_status": item.kind_status,
                        "country": item.country,
                        "weak_hints": list(item.weak_hints),
                        "country_cap_fallback": cap_fallback,
                        "stable_order": item.stable_order,
                    }
                )

    source_db_sha256 = file_sha256(source_db)
    if source_db_sha256 != source_sha_before:
        raise RuntimeError("source DB changed while selecting Vision candidates")
    contract = {
        "manifest_version": CANDIDATE_MANIFEST_VERSION,
        "selection_version": SELECTION_VERSION,
        "source_db_filename": source_db.name,
        "source_db_sha256": source_db_sha256,
        "source_profile": SOURCE_PROFILE,
        "review_profile": REVIEW_PROFILE,
        "identity_profile": IDENTITY_PROFILE,
        "pixel_hash_version": PIXEL_HASH_VERSION,
        "phash_version": PHASH_VERSION,
        "class_order": list(CLASSES),
        "scarcity_order": list(SCARCITY_ORDER),
        "pool_targets": POOL_TARGETS,
        "final_cell_quotas": {
            "%s_%s" % key: value for key, value in FINAL_CELL_QUOTAS.items()
        },
        "review_policy": {
            "hints_hidden_by_default": True,
            "exact_pixel_duplicates": "auto_exclude_after_probe",
            "phash_distance_le_8": "human_duplicate_adjudication_required",
            "phash_distance_le_16": "audit_only",
            "out_of_scope": [
                "rendering",
                "physical_model",
                "mixed",
                "portrait",
                "construction",
                "object_only",
            ],
        },
    }
    payload: dict[str, Any] = {
        "manifest_version": CANDIDATE_MANIFEST_VERSION,
        "source_db_filename": source_db.name,
        "source_db_sha256": source_db_sha256,
        "contract": contract,
        "candidates": candidates,
    }
    payload["manifest_sha256"] = manifest_sha256(payload)
    return payload


def manifest_sha256(payload: Mapping[str, Any]) -> str:
    value = dict(payload)
    value.pop("manifest_sha256", None)
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_candidate_manifest(payload: Mapping[str, Any]) -> None:
    if payload.get("manifest_version") != CANDIDATE_MANIFEST_VERSION:
        raise ValueError("candidate manifest version mismatch")
    if payload.get("manifest_sha256") != manifest_sha256(payload):
        raise ValueError("candidate manifest SHA mismatch")
    source_db_sha256 = payload.get("source_db_sha256")
    if (
        not isinstance(source_db_sha256, str)
        or len(source_db_sha256) != 64
        or any(value not in "0123456789abcdef" for value in source_db_sha256)
    ):
        raise ValueError("source_db_sha256 must be 64 lowercase hex characters")
    contract = payload.get("contract")
    if not isinstance(contract, Mapping) or contract.get("source_db_sha256") != source_db_sha256:
        raise ValueError("contract source DB SHA mismatch")
    expected_contract = {
        "manifest_version": CANDIDATE_MANIFEST_VERSION,
        "selection_version": SELECTION_VERSION,
        "source_db_filename": payload.get("source_db_filename"),
        "source_db_sha256": source_db_sha256,
        "source_profile": SOURCE_PROFILE,
        "review_profile": REVIEW_PROFILE,
        "identity_profile": IDENTITY_PROFILE,
        "pixel_hash_version": PIXEL_HASH_VERSION,
        "phash_version": PHASH_VERSION,
        "class_order": list(CLASSES),
        "scarcity_order": list(SCARCITY_ORDER),
        "pool_targets": POOL_TARGETS,
        "final_cell_quotas": {
            "%s_%s" % key: value for key, value in FINAL_CELL_QUOTAS.items()
        },
    }
    for field, expected in expected_contract.items():
        if contract.get(field) != expected:
            raise ValueError("candidate contract mismatch: %s" % field)
    if not isinstance(payload.get("source_db_filename"), str) or not payload.get(
        "source_db_filename"
    ):
        raise ValueError("source_db_filename is required")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != sum(
        sum(values.values()) for values in POOL_TARGETS.values()
    ):
        raise ValueError("candidate count mismatch")
    for field in (
        "candidate_id",
        "asset_key",
        "article_id",
        "building_id",
        "request_url",
        "review_url",
    ):
        values = [row.get(field) for row in candidates]
        if any(value is None for value in values) or len(values) != len(set(values)):
            raise ValueError("candidate field must be non-null and unique: %s" % field)
    counts: dict[tuple[str, str], int] = {}
    class_ranks = {label: 0 for label in CLASSES}
    for rank, row in enumerate(candidates, 1):
        key = (str(row.get("discovery_class")), str(row.get("generation_group")))
        label, generation = key
        if label not in CLASSES or generation not in GENERATION_GROUPS:
            raise ValueError("unsupported candidate class/generation at rank %d" % rank)
        class_ranks[label] += 1
        if row.get("candidate_id") != "candidate-%04d" % rank:
            raise ValueError("candidate_id sequence mismatch at rank %d" % rank)
        if row.get("candidate_rank") != rank or row.get("class_rank") != class_ranks[label]:
            raise ValueError("candidate rank mismatch at rank %d" % rank)
        if _generation_group(str(row.get("url_generation"))) != generation:
            raise ValueError("candidate generation mismatch at rank %d" % rank)
        if row.get("stable_order") != _stable_hex(label, str(row.get("asset_key"))):
            raise ValueError("candidate stable order mismatch at rank %d" % rank)
        if not isinstance(row.get("discovery_score"), int) or row["discovery_score"] <= 0:
            raise ValueError("candidate discovery score is invalid at rank %d" % rank)
        if not isinstance(row.get("country_cap_fallback"), bool):
            raise ValueError("candidate country fallback flag is invalid at rank %d" % rank)
        for field, profile in (("request_url", SOURCE_PROFILE), ("review_url", REVIEW_PROFILE)):
            parts = urlsplit(str(row.get(field)))
            if (
                parts.scheme != "https"
                or parts.hostname != "images.divisare.com"
                or ("/" + profile + "/") not in parts.path
            ):
                raise ValueError("candidate %s contract mismatch at rank %d" % (field, rank))
        counts[key] = counts.get(key, 0) + 1
    for label in CLASSES:
        for generation in GENERATION_GROUPS:
            if counts.get((label, generation), 0) != POOL_TARGETS[label][generation]:
                raise ValueError("candidate quota mismatch for %s/%s" % (label, generation))


def write_candidate_manifest(source_db: Path, output_path: Path) -> dict[str, Any]:
    if output_path.exists():
        raise FileExistsError("immutable candidate manifest already exists: %s" % output_path)
    payload = candidate_manifest_payload(source_db)
    validate_candidate_manifest(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".partial", dir=output_path.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, output_path)
        except FileExistsError as exc:
            raise FileExistsError(
                "immutable candidate manifest already exists: %s" % output_path
            ) from exc
    finally:
        temp.unlink(missing_ok=True)
    return payload
