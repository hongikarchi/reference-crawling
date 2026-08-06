"""Deterministic, metadata-only candidates for a fresh Vision-axis holdout.

The builder deliberately excludes every asset, article, and provisional
building represented in the earlier 560-image Vision candidate pool.  Proxy
labels and weak metadata hints are audit evidence only; reviewer-facing tools
must expose only ``review_id`` and the image itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from canonical import divisare_vision_gold as old_gold
from canonical.divisare_image_smoke import (
    _basic_lane,
    canonical_json,
    file_sha256,
    fixed_derivative_url,
    open_source_readonly,
)


MANIFEST_VERSION = "divisare-vision-axes-holdout-candidates-v1.0.0"
SELECTION_VERSION = "divisare-vision-axes-holdout-selection-v1.0.0"
BLIND_ID_VERSION = "divisare-vision-axes-holdout-review-v1"
REVIEW_ORDER_VERSION = "divisare-vision-axes-holdout-order-v1"
EXPECTED_EXCLUSION_COUNT = 560

SOURCE_PROFILE = old_gold.SOURCE_PROFILE
REVIEW_PROFILE = old_gold.REVIEW_PROFILE
PROXY_CLASSES = (
    "exterior",
    "interior",
    "drawing",
    "detail",
    "aerial",
    "out_of_scope",
)
GENERATION_GROUPS = old_gold.GENERATION_GROUPS
ROLES = ("cover", "gallery")
SCARCITY_ORDER = (
    "out_of_scope",
    "drawing",
    "aerial",
    "detail",
    "interior",
    "exterior",
)

# Total: 100.  Legacy URLs and gallery images are intentionally oversampled
# relative to their easiest modern-cover counterparts.
CELL_TARGETS: dict[tuple[str, str, str], int] = {
    ("exterior", "modern", "cover"): 6,
    ("exterior", "modern", "gallery"): 8,
    ("exterior", "legacy", "cover"): 3,
    ("exterior", "legacy", "gallery"): 3,
    ("interior", "modern", "cover"): 6,
    ("interior", "modern", "gallery"): 8,
    ("interior", "legacy", "cover"): 3,
    ("interior", "legacy", "gallery"): 3,
    ("drawing", "modern", "cover"): 5,
    ("drawing", "modern", "gallery"): 9,
    ("drawing", "legacy", "cover"): 2,
    ("drawing", "legacy", "gallery"): 4,
    ("detail", "modern", "cover"): 4,
    ("detail", "modern", "gallery"): 7,
    ("detail", "legacy", "cover"): 2,
    ("detail", "legacy", "gallery"): 3,
    ("aerial", "modern", "cover"): 4,
    ("aerial", "modern", "gallery"): 4,
    ("aerial", "legacy", "cover"): 2,
    ("aerial", "legacy", "gallery"): 2,
    ("out_of_scope", "modern", "cover"): 4,
    ("out_of_scope", "modern", "gallery"): 4,
    ("out_of_scope", "legacy", "cover"): 2,
    ("out_of_scope", "legacy", "gallery"): 2,
}

COUNTRY_CAP_PER_CELL = 2
OOS_SUBTYPE_CAP_PER_CELL = 1
SHA256_RE = re.compile(r"[0-9a-f]{64}")

_OOS_FILENAME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("people_or_portrait", ("portrait", "interview", "people", "person")),
    ("people_or_event", ("event", "lecture", "ceremony", "workshop")),
    ("text_or_graphic", ("logo", "poster", "cover-page", "title-card")),
    ("object_or_artwork", ("artwork", "sculpture", "installation", "object")),
    ("multi_panel", ("collage", "contact-sheet", "moodboard")),
)


@dataclass(frozen=True)
class ExclusionEvidence:
    file_sha256: str
    manifest_sha256: str
    source_db_sha256: str
    asset_keys: frozenset[str]
    article_ids: frozenset[int]
    building_ids: frozenset[str]
    identity_set_sha256: str


@dataclass(frozen=True)
class HoldoutEvidence:
    asset_key: str
    article_id: int
    building_id: str
    source_url: str
    url_generation: str
    generation_group: str
    original_filename: str | None
    role: str
    position: int
    article_kind: str
    kind_status: str
    country: str
    proxy_class: str
    proxy_subtype: str
    proxy_score: int
    weak_hints: tuple[str, ...]
    stable_order: str


def manifest_sha256(payload: Mapping[str, Any]) -> str:
    value = dict(payload)
    value.pop("manifest_sha256", None)
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def identity_set_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    identities = sorted(
        (
            {
                "article_id": int(row["article_id"]),
                "asset_key": str(row["asset_key"]),
                "building_id": str(row["building_id"]),
            }
            for row in rows
        ),
        key=lambda row: (row["asset_key"], row["article_id"], row["building_id"]),
    )
    return hashlib.sha256(canonical_json(identities).encode("utf-8")).hexdigest()


def _stable_hex(proxy_class: str, generation: str, role: str, asset_key: str) -> str:
    raw = "\0".join(
        (SELECTION_VERSION, proxy_class, generation, role, asset_key)
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def opaque_review_id(asset_key: str) -> str:
    digest = hashlib.sha256(
        (BLIND_ID_VERSION + "|" + asset_key).encode("utf-8")
    ).hexdigest()
    return "axis-holdout-" + digest[:12]


def _review_order_key(review_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(
        (REVIEW_ORDER_VERSION + "|" + review_id).encode("ascii")
    ).hexdigest()
    return digest, review_id


def _require_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError("%s must be 64 lowercase hexadecimal characters" % name)
    return value


def _load_set_map(conn: Any, query: str) -> dict[Any, frozenset[str]]:
    values: dict[Any, set[str]] = {}
    for key, value in conn.execute(query):
        if value is not None:
            values.setdefault(key, set()).add(str(value).strip().casefold())
    return {key: frozenset(sorted(items)) for key, items in values.items()}


def _generation_group(url_generation: str) -> str:
    return old_gold._generation_group(url_generation)


def _filename_tokens(filename: str | None) -> str:
    return unquote(filename or "").casefold().replace("_", "-")


def _oos_proxy(
    *,
    filename: str,
    article_kind: str,
    article_hints: frozenset[str],
    albums: frozenset[str],
) -> tuple[int, str, tuple[str, ...]] | None:
    """Return a weak scope-challenge proxy, never a semantic label."""
    offers: list[tuple[int, str, tuple[str, ...]]] = []
    for subtype, tokens in _OOS_FILENAME_RULES:
        matched = tuple(sorted(token for token in tokens if token in filename))
        if matched:
            offers.append(
                (96, subtype, tuple("filename_token:" + token for token in matched))
            )
    if "portrait" in article_hints:
        offers.append((100, "people_or_portrait", ("article_hint:portrait",)))
    if "reportage" in article_hints:
        offers.append((82, "people_or_event", ("article_hint:reportage",)))
    if article_kind == "concept_editorial":
        offers.append((76, "object_or_artwork", ("article_kind:concept_editorial",)))
    if article_kind == "mixed_feature":
        offers.append((72, "multi_panel", ("article_kind:mixed_feature",)))
    if article_kind == "model_feature":
        offers.append((64, "model_or_object_boundary", ("article_kind:model_feature",)))
    if "ideas" in albums:
        offers.append((58, "object_or_artwork", ("article_album:ideas",)))
    if not offers:
        return None
    return min(offers, key=lambda row: (-row[0], row[1], row[2]))


def load_exclusion_manifest(path: Path) -> tuple[dict[str, Any], ExclusionEvidence]:
    path = path.resolve()
    raw = path.read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("exclusion manifest must be a JSON object")
    old_gold.validate_candidate_manifest(payload)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != EXPECTED_EXCLUSION_COUNT:
        raise ValueError(
            "exclusion manifest must contain exactly %d candidates"
            % EXPECTED_EXCLUSION_COUNT
        )
    assets = frozenset(str(row["asset_key"]) for row in candidates)
    articles = frozenset(int(row["article_id"]) for row in candidates)
    buildings = frozenset(str(row["building_id"]) for row in candidates)
    if not (
        len(assets) == len(articles) == len(buildings) == EXPECTED_EXCLUSION_COUNT
    ):
        raise ValueError("exclusion manifest identities must each be unique")
    evidence = ExclusionEvidence(
        file_sha256=file_sha,
        manifest_sha256=_require_sha(payload.get("manifest_sha256"), "exclusion SHA"),
        source_db_sha256=_require_sha(
            payload.get("source_db_sha256"), "exclusion source DB SHA"
        ),
        asset_keys=assets,
        article_ids=articles,
        building_ids=buildings,
        identity_set_sha256=identity_set_sha256(candidates),
    )
    return payload, evidence


def _candidate_reservoirs(
    source_db: Path, exclusion: ExclusionEvidence
) -> dict[tuple[str, str, str], list[HoldoutEvidence]]:
    reservoirs: dict[tuple[str, str, str], list[HoldoutEvidence]] = {}
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
        for row in conn.execute(old_gold.BASE_QUERY):
            asset_key = str(row[0])
            if asset_key == previous_asset_key:
                raise RuntimeError(
                    "candidate source asset maps to multiple buildings: %s" % asset_key
                )
            previous_asset_key = asset_key
            article_id = int(row[1])
            building_id = str(row[2])
            if (
                asset_key in exclusion.asset_keys
                or article_id in exclusion.article_ids
                or building_id in exclusion.building_ids
            ):
                continue
            original_filename = str(row[4]) if row[4] is not None else None
            source_url = str(row[5])
            if _basic_lane(original_filename, source_url) != "raster":
                continue
            filename = _filename_tokens(original_filename)
            url_generation = str(row[3])
            generation = _generation_group(url_generation)
            role = str(row[6])
            if role not in ROLES:
                continue
            position = int(row[7])
            article_kind = str(row[8])
            kind_status = str(row[9])
            country = str(row[10] or "__missing__")
            row_image_hints = image_hints.get(asset_key, frozenset())
            row_article_hints = article_hints.get(article_id, frozenset())
            row_albums = albums.get(article_id, frozenset())
            scored = old_gold._score_classes(
                filename=filename,
                role=role,
                position=position,
                article_kind=article_kind,
                image_hints=row_image_hints,
                article_hints=row_article_hints,
                albums=row_albums,
                tags=tags.get(article_id, frozenset()),
            )
            row_tags = tags.get(article_id, frozenset())
            exterior_gallery_fallback = (
                role == "gallery"
                and position <= 3
                and article_kind in old_gold._EXTERIOR_KINDS
                and bool(row_tags & old_gold.EXTERIOR_TAGS)
                and not row_image_hints
                and not (row_article_hints & old_gold._SPECIAL_EXTERIOR_HINTS)
                and not (row_albums & old_gold._EXTERIOR_EXCLUDED_ALBUMS)
                and not any(
                    value in filename
                    for value in (*old_gold._FILENAME_DRAWING, "interior", "detail")
                )
                and "wip-work-in-progress" not in row_tags
            )
            if "exterior" not in scored and exterior_gallery_fallback:
                matched_tags = sorted(row_tags & old_gold.EXTERIOR_TAGS)
                scored["exterior"] = (
                    40,
                    tuple("early_gallery_article_tag:" + value for value in matched_tags),
                )
            proxies: list[tuple[str, int, str, tuple[str, ...]]] = [
                (label, score, label, hints)
                for label, (score, hints) in scored.items()
                if label in PROXY_CLASSES
            ]
            oos = _oos_proxy(
                filename=filename,
                article_kind=article_kind,
                article_hints=row_article_hints,
                albums=row_albums,
            )
            if oos is not None:
                score, subtype, hints = oos
                proxies.append(("out_of_scope", score, subtype, hints))
            for proxy_class, score, subtype, weak_hints in proxies:
                stable = _stable_hex(proxy_class, generation, role, asset_key)
                evidence = HoldoutEvidence(
                    asset_key=asset_key,
                    article_id=article_id,
                    building_id=building_id,
                    source_url=source_url,
                    url_generation=url_generation,
                    generation_group=generation,
                    original_filename=original_filename,
                    role=role,
                    position=position,
                    article_kind=article_kind,
                    kind_status=kind_status,
                    country=country,
                    proxy_class=proxy_class,
                    proxy_subtype=subtype,
                    proxy_score=score,
                    weak_hints=tuple(weak_hints),
                    stable_order=stable,
                )
                reservoirs.setdefault((proxy_class, generation, role), []).append(
                    evidence
                )
    for values in reservoirs.values():
        values.sort(key=lambda item: (-item.proxy_score, item.stable_order))
    return reservoirs


def _cell_order() -> list[tuple[str, str, str]]:
    return [
        (proxy_class, generation, role)
        for proxy_class in SCARCITY_ORDER
        for generation in ("legacy", "modern")
        for role in ("gallery", "cover")
        if CELL_TARGETS.get((proxy_class, generation, role), 0)
    ]


def _select_candidates(
    reservoirs: Mapping[tuple[str, str, str], Sequence[HoldoutEvidence]],
) -> list[HoldoutEvidence]:
    selected: list[HoldoutEvidence] = []
    used_assets: set[str] = set()
    used_articles: set[int] = set()
    used_buildings: set[str] = set()

    for cell in _cell_order():
        target = CELL_TARGETS[cell]
        values = reservoirs.get(cell, ())
        cell_selected: list[HoldoutEvidence] = []
        country_counts: Counter[str] = Counter()
        subtype_counts: Counter[str] = Counter()

        def eligible(item: HoldoutEvidence) -> bool:
            return (
                item.asset_key not in used_assets
                and item.article_id not in used_articles
                and item.building_id not in used_buildings
            )

        for policy in ("strict", "country_relaxed", "all_relaxed"):
            for item in values:
                if len(cell_selected) >= target:
                    break
                if not eligible(item):
                    continue
                if policy == "strict" and country_counts[item.country] >= COUNTRY_CAP_PER_CELL:
                    continue
                if (
                    cell[0] == "out_of_scope"
                    and policy != "all_relaxed"
                    and subtype_counts[item.proxy_subtype] >= OOS_SUBTYPE_CAP_PER_CELL
                ):
                    continue
                cell_selected.append(item)
                country_counts[item.country] += 1
                subtype_counts[item.proxy_subtype] += 1
                used_assets.add(item.asset_key)
                used_articles.add(item.article_id)
                used_buildings.add(item.building_id)
            if len(cell_selected) >= target:
                break
        if len(cell_selected) != target:
            raise RuntimeError(
                "holdout quota shortfall for %s/%s/%s: expected %d, got %d"
                % (*cell, target, len(cell_selected))
            )
        selected.extend(cell_selected)
    return selected


def _contract(source_db: Path, source_sha: str, exclusion: ExclusionEvidence) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "selection_version": SELECTION_VERSION,
        "source_db_filename": source_db.name,
        "source_db_sha256": source_sha,
        "source_profile": SOURCE_PROFILE,
        "review_profile": REVIEW_PROFILE,
        "proxy_classes": list(PROXY_CLASSES),
        "generation_groups": list(GENERATION_GROUPS),
        "roles": list(ROLES),
        "scarcity_order": list(SCARCITY_ORDER),
        "cell_targets": {
            "/".join(key): value for key, value in sorted(CELL_TARGETS.items())
        },
        "candidate_count": sum(CELL_TARGETS.values()),
        "selection_uses_model_output": False,
        "selection_uses_human_labels": False,
        "network_io": False,
        "reviewer_projection_fields": ["review_id", "review_url"],
        "proxy_evidence_hidden_from_reviewer": True,
        "exclusion_policy": ["asset_key", "article_id", "building_id"],
        "exclusion_manifest_sha256": exclusion.manifest_sha256,
        "exclusion_identity_set_sha256": exclusion.identity_set_sha256,
    }


def _payload_from_evidence(
    *,
    source_db: Path,
    source_sha: str,
    exclusion_path: Path,
    exclusion: ExclusionEvidence,
    selected: Sequence[HoldoutEvidence],
) -> dict[str, Any]:
    blind_order = sorted(
        selected,
        key=lambda item: _review_order_key(opaque_review_id(item.asset_key)),
    )
    cell_ranks: Counter[tuple[str, str, str]] = Counter()
    candidates: list[dict[str, Any]] = []
    for candidate_rank, item in enumerate(blind_order, 1):
        cell = (item.proxy_class, item.generation_group, item.role)
        cell_ranks[cell] += 1
        candidates.append(
            {
                "candidate_id": "holdout-candidate-%04d" % candidate_rank,
                "candidate_rank": candidate_rank,
                "review_id": opaque_review_id(item.asset_key),
                "cell_rank": cell_ranks[cell],
                "asset_key": item.asset_key,
                "article_id": item.article_id,
                "building_id": item.building_id,
                "source_url": item.source_url,
                "request_url": fixed_derivative_url(item.source_url, SOURCE_PROFILE),
                "review_url": fixed_derivative_url(item.source_url, REVIEW_PROFILE),
                "url_generation": item.url_generation,
                "generation_group": item.generation_group,
                "original_filename": item.original_filename,
                "role": item.role,
                "position": item.position,
                "article_kind": item.article_kind,
                "kind_status": item.kind_status,
                "country": item.country,
                "proxy_class": item.proxy_class,
                "proxy_subtype": item.proxy_subtype,
                "proxy_score": item.proxy_score,
                "weak_hints": list(item.weak_hints),
                "stable_order": item.stable_order,
            }
        )
    metrics = {
        "candidate_count": len(candidates),
        "unique_asset_count": len({row["asset_key"] for row in candidates}),
        "unique_article_count": len({row["article_id"] for row in candidates}),
        "unique_building_count": len({row["building_id"] for row in candidates}),
        "proxy_counts": dict(sorted(Counter(row["proxy_class"] for row in candidates).items())),
        "generation_counts": dict(
            sorted(Counter(row["generation_group"] for row in candidates).items())
        ),
        "role_counts": dict(sorted(Counter(row["role"] for row in candidates).items())),
        "oos_subtype_counts": dict(
            sorted(
                Counter(
                    row["proxy_subtype"]
                    for row in candidates
                    if row["proxy_class"] == "out_of_scope"
                ).items()
            )
        ),
        "selected_identity_set_sha256": identity_set_sha256(candidates),
    }
    payload: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "source_db_filename": source_db.name,
        "source_db_sha256": source_sha,
        "contract": _contract(source_db, source_sha, exclusion),
        "provenance": {
            "exclusion_manifest_filename": exclusion_path.name,
            "exclusion_manifest_file_sha256": exclusion.file_sha256,
            "exclusion_manifest_sha256": exclusion.manifest_sha256,
            "exclusion_candidate_count": EXPECTED_EXCLUSION_COUNT,
            "exclusion_identity_set_sha256": exclusion.identity_set_sha256,
        },
        "selection_metrics": metrics,
        "candidates": candidates,
    }
    payload["manifest_sha256"] = manifest_sha256(payload)
    return payload


def candidate_manifest_payload(
    source_db: Path, exclusion_manifest_path: Path
) -> dict[str, Any]:
    source_db = source_db.resolve()
    exclusion_manifest_path = exclusion_manifest_path.resolve()
    if source_db == exclusion_manifest_path:
        raise ValueError("source DB and exclusion manifest paths must differ")
    source_sha_before = file_sha256(source_db)
    exclusion_file_sha_before = file_sha256(exclusion_manifest_path)
    _excluded_payload, exclusion = load_exclusion_manifest(exclusion_manifest_path)
    if exclusion.file_sha256 != exclusion_file_sha_before:
        raise RuntimeError("exclusion manifest changed while being loaded")
    if source_sha_before != exclusion.source_db_sha256:
        raise ValueError("source DB SHA does not match the exclusion manifest")

    reservoirs = _candidate_reservoirs(source_db, exclusion)
    selected = _select_candidates(reservoirs)
    payload = _payload_from_evidence(
        source_db=source_db,
        source_sha=source_sha_before,
        exclusion_path=exclusion_manifest_path,
        exclusion=exclusion,
        selected=selected,
    )
    validate_candidate_manifest(payload, exclusion=exclusion)

    if file_sha256(source_db) != source_sha_before:
        raise RuntimeError("source DB changed while selecting holdout candidates")
    if file_sha256(exclusion_manifest_path) != exclusion_file_sha_before:
        raise RuntimeError("exclusion manifest changed while selecting candidates")
    return payload


def validate_candidate_manifest(
    payload: Mapping[str, Any], *, exclusion: ExclusionEvidence | None = None
) -> list[dict[str, Any]]:
    if payload.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("holdout candidate manifest version mismatch")
    if payload.get("manifest_sha256") != manifest_sha256(payload):
        raise ValueError("holdout candidate manifest SHA mismatch")
    source_sha = _require_sha(payload.get("source_db_sha256"), "source DB SHA")
    contract = payload.get("contract")
    if not isinstance(contract, Mapping):
        raise ValueError("holdout candidate contract is required")
    expected_contract = {
        "manifest_version": MANIFEST_VERSION,
        "selection_version": SELECTION_VERSION,
        "source_db_filename": payload.get("source_db_filename"),
        "source_db_sha256": source_sha,
        "source_profile": SOURCE_PROFILE,
        "review_profile": REVIEW_PROFILE,
        "proxy_classes": list(PROXY_CLASSES),
        "generation_groups": list(GENERATION_GROUPS),
        "roles": list(ROLES),
        "scarcity_order": list(SCARCITY_ORDER),
        "cell_targets": {
            "/".join(key): value for key, value in sorted(CELL_TARGETS.items())
        },
        "candidate_count": sum(CELL_TARGETS.values()),
        "selection_uses_model_output": False,
        "selection_uses_human_labels": False,
        "network_io": False,
        "reviewer_projection_fields": ["review_id", "review_url"],
        "proxy_evidence_hidden_from_reviewer": True,
        "exclusion_policy": ["asset_key", "article_id", "building_id"],
    }
    for field, expected in expected_contract.items():
        if contract.get(field) != expected:
            raise ValueError("holdout candidate contract mismatch: %s" % field)
    _require_sha(contract.get("exclusion_manifest_sha256"), "contract exclusion SHA")
    _require_sha(
        contract.get("exclusion_identity_set_sha256"),
        "contract exclusion identity SHA",
    )

    candidates_raw = payload.get("candidates")
    if not isinstance(candidates_raw, list) or len(candidates_raw) != sum(
        CELL_TARGETS.values()
    ):
        raise ValueError("holdout candidate count mismatch")
    candidates = [dict(row) for row in candidates_raw]
    unique_fields = (
        "candidate_id",
        "review_id",
        "asset_key",
        "article_id",
        "building_id",
        "request_url",
        "review_url",
    )
    for field in unique_fields:
        values = [row.get(field) for row in candidates]
        if any(value is None for value in values) or len(values) != len(set(values)):
            raise ValueError("holdout field must be non-null and unique: %s" % field)

    expected_order = sorted(
        (str(row["review_id"]) for row in candidates), key=_review_order_key
    )
    if [row["review_id"] for row in candidates] != expected_order:
        raise ValueError("holdout candidates are not in blinded review order")
    counts: Counter[tuple[str, str, str]] = Counter()
    cell_ranks: Counter[tuple[str, str, str]] = Counter()
    for rank, row in enumerate(candidates, 1):
        if row.get("candidate_id") != "holdout-candidate-%04d" % rank:
            raise ValueError("holdout candidate ID mismatch at rank %d" % rank)
        if row.get("candidate_rank") != rank:
            raise ValueError("holdout candidate rank mismatch at rank %d" % rank)
        asset_key = str(row["asset_key"])
        if row.get("review_id") != opaque_review_id(asset_key):
            raise ValueError("holdout opaque review ID mismatch at rank %d" % rank)
        proxy_class = str(row.get("proxy_class"))
        generation = str(row.get("generation_group"))
        role = str(row.get("role"))
        cell = (proxy_class, generation, role)
        if cell not in CELL_TARGETS:
            raise ValueError("unsupported holdout cell at rank %d" % rank)
        counts[cell] += 1
        cell_ranks[cell] += 1
        if row.get("cell_rank") != cell_ranks[cell]:
            raise ValueError("holdout cell rank mismatch at rank %d" % rank)
        if _generation_group(str(row.get("url_generation"))) != generation:
            raise ValueError("holdout URL generation mismatch at rank %d" % rank)
        if row.get("stable_order") != _stable_hex(
            proxy_class, generation, role, asset_key
        ):
            raise ValueError("holdout stable order mismatch at rank %d" % rank)
        if not isinstance(row.get("proxy_score"), int) or row["proxy_score"] <= 0:
            raise ValueError("holdout proxy score is invalid at rank %d" % rank)
        if not isinstance(row.get("weak_hints"), list):
            raise ValueError("holdout weak hints must be a list at rank %d" % rank)
        source_url = str(row.get("source_url") or "")
        for field, profile in (
            ("request_url", SOURCE_PROFILE),
            ("review_url", REVIEW_PROFILE),
        ):
            value = str(row.get(field) or "")
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "images.divisare.com"
                or value != fixed_derivative_url(source_url, profile)
            ):
                raise ValueError("holdout %s mismatch at rank %d" % (field, rank))
    if dict(counts) != CELL_TARGETS:
        raise ValueError("holdout cell quotas do not match the contract")

    metrics = payload.get("selection_metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("holdout selection metrics are required")
    selected_sha = identity_set_sha256(candidates)
    if metrics.get("selected_identity_set_sha256") != selected_sha:
        raise ValueError("selected holdout identity SHA mismatch")
    if any(metrics.get(field) != len(candidates) for field in (
        "candidate_count",
        "unique_asset_count",
        "unique_article_count",
        "unique_building_count",
    )):
        raise ValueError("holdout identity accounting mismatch")

    if exclusion is not None:
        if source_sha != exclusion.source_db_sha256:
            raise ValueError("holdout and exclusion source DB SHA mismatch")
        if contract.get("exclusion_manifest_sha256") != exclusion.manifest_sha256:
            raise ValueError("holdout exclusion manifest SHA mismatch")
        if contract.get("exclusion_identity_set_sha256") != exclusion.identity_set_sha256:
            raise ValueError("holdout exclusion identity SHA mismatch")
        overlap_assets = exclusion.asset_keys & {str(row["asset_key"]) for row in candidates}
        overlap_articles = exclusion.article_ids & {int(row["article_id"]) for row in candidates}
        overlap_buildings = exclusion.building_ids & {
            str(row["building_id"]) for row in candidates
        }
        if overlap_assets or overlap_articles or overlap_buildings:
            raise ValueError("holdout candidates overlap excluded identities")
    return candidates


def write_candidate_manifest(
    source_db: Path, exclusion_manifest_path: Path, output_path: Path
) -> dict[str, Any]:
    source_db = source_db.resolve()
    exclusion_manifest_path = exclusion_manifest_path.resolve()
    output_path = output_path.resolve()
    if output_path in {source_db, exclusion_manifest_path}:
        raise ValueError("output path must differ from both immutable inputs")
    if output_path.exists():
        raise FileExistsError("immutable holdout manifest already exists: %s" % output_path)
    payload = candidate_manifest_payload(source_db, exclusion_manifest_path)
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
                "immutable holdout manifest already exists: %s" % output_path
            ) from exc
    finally:
        temp.unlink(missing_ok=True)
    return payload
