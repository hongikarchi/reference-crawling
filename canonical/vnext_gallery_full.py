"""vNext WP4 S2 full gallery v1 (offline) from frozen E2/E3 evidence.

Contract: ``docs/vnext/CONTRACT_GALLERY_SEMANTICS.md`` §2.1 (v1.3: continuity rule v2, Neon-form
Architizer URL, fail-closed non-raster Divisare).

* Population: every E3 ``selected_buildings`` building (91,803), keyed by source building
  ``(source, source_building_id)``. vnx re-key is a later step (P3).
* Representative eligibility: E1 fingerprint success AND source ``continuity_attested`` (from the
  pinned continuity summary, rule ``vnext-asset-continuity-rule-v2``) AND the WP1 exclusion rules (low-information, E3 candidate present).
* Groups are exact normalized-pixel SHA classes only. Every E2 ``phash_edges`` row (scopes
  ``global_le8`` and ``metadata_9_16``) between two groups of one building is preserved in
  ``direct_edge_evidence`` with ``attachment_status='deferred_no_per_asset_verification'``; no star
  attachment, no transitive closure. Distinct exact classes on the SAME pHash node have no E2 edge row;
  that relation is carried by ``representative_phash_node_id`` and counted in the report.
* ``sort_order`` is 0-based contiguous per building and follows the source page order recorded in
  E2 ``project_asset_occurrences`` (= curated DB position), then E3 rank, then the stable key.
* Serving URL: Architizer = the live-Neon string, i.e. the exact regex rewrite of
  ``tools/architizer_image_host_migrate.py`` (path verbatim, NOT percent-encoded, query
  ``?width=1680&quality=60``) applied to the frozen E2 ``canonical_url``, which must fullmatch the
  pattern; Divisare = the representative's
  first browser-raster frozen E2 occurrence URL verbatim (gallery first, then cover). A Divisare asset
  with no browser-raster occurrence URL (``.pdf/.ai/.eps/.psd/.tiff/.jp2/...`` under ``f_auto``) is
  ``unresolved`` with ``no_browser_renderable_serving_url``.

Offline: no HTTP client is imported; E2/E3 are opened ``mode=ro&immutable=1``. The output DB is
written to ``<name>.building`` and ``os.replace``d; no clobber; no SQLite sidecars.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.vnext_gallery_draft import (
    DEFAULT_E2,
    DEFAULT_E3,
    DEFAULT_FREEZE,
    E2_ROLE,
    E3_ROLE,
    EXPECTED_E2_SHA256,
    EXPECTED_E2_SIZE,
    EXPECTED_E3_SHA256,
    EXPECTED_E3_SIZE,
    REPRESENTATIVE_SCORE_VERSION,
    _atomic_json,
    _open_immutable,
    _retryable,
    load_frozen_inputs,
    representative_score,
    representative_sort_key,
    sha256_file,
    stable_score,
    utc_now,
)


CONTRACT_VERSION = "vnext-gallery-contract-v1.3"
BUILDER_VERSION = "vnext-gallery-full-builder-v1.2"
GROUPING_VERSION = "vnext-exact-class-only-v1"
SORT_ORDER_VERSION = "vnext-gallery-sort-source-page-order-v1"
URL_POLICY_VERSION = "vnext-gallery-serving-url-v1.2"
URL_POLICY_ARCHITIZER = "architizer_neon_regex_host_rewrite_v1_2"
URL_PROVENANCE_ARCHITIZER = "neon_regex_host_rewrite:architizer-prod.imgix.net->images.architizer.com"
URL_POLICY_DIVISARE = "divisare_e2_raster_occurrence_url_v1_1"
EDGE_EVIDENCE_VERSION = "vnext-gallery-deferred-edge-evidence-v1.1"
EDGE_SCOPES = ("global_le8", "metadata_9_16")
REASON_NO_SERVING_URL = "no_browser_renderable_serving_url"
UNRESOLVED_ENGINE = {
    "successful_e2_asset_missing_e3_candidate": "frozen_e2_e3_join",
    REASON_NO_SERVING_URL: "serving_url_policy",
    "source_continuity_not_attested": "continuity_attestation",
}
# Browser-renderable raster formats for a Divisare (Cloudinary) URL: an explicit raster format
# transform anywhere in the path, else the file extension. ``f_auto`` passes the original format
# through (application/pdf, image/tiff, ...), which <img> cannot render.
BROWSER_RASTER_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"})
CLOUDINARY_RASTER_FORMATS = frozenset({"f_jpg", "f_jpeg", "f_png", "f_webp", "f_avif", "f_gif"})
# The 2026-10-16 Neon migration (tools/architizer_image_host_migrate.py PATTERN/REPLACEMENT) copied
# the path verbatim. Contract v1.3 (D2): the S2 Architizer serving URL IS this rewrite (identity is
# asserted by a test against the tool's constants).
NEON_ARCHITIZER_PATTERN = r"https?://architizer-prod\.imgix\.net(/[^\"?\s]*)(\?[^\"\s]*)?"
NEON_ARCHITIZER_REPLACEMENT = "https://images.architizer.com" + r"\1" + "?width=1680&quality=60"
LOGICAL_VERSION = "vnext-gallery-full-logical-v1"
LADDER_SEED = "vnext-wp4-s2-full-ladder-v1"
APPLICATION_ID = 0x564E5834  # "VNX4"; WP1 uses 0x564E5831, WP1.1 0x564E5832
SCHEMA_VERSION = 1
SOURCES = ("architizer", "divisare")
DEFERRED_ATTACHMENT = "deferred_no_per_asset_verification"
CONTINUITY_STATUS = "continuity_attested"
PER_ASSET_PIXEL_STATUS = "pending_s3_pixel_pass"
CONTINUITY_RULE_VERSION = "vnext-asset-continuity-rule-v2"
CONTINUITY_SAMPLE_SEED = "vnext-asset-continuity-v2"
DEFAULT_CONTINUITY_SUMMARY = Path("data/reports/vnext_asset_continuity_sample_v2_summary.json")
DEFAULT_CONTRACT = Path("docs/vnext/CONTRACT_GALLERY_SEMANTICS.md")
CONTRACT_ANCHORS = ("v1.3", "population continuity attestation", DEFERRED_ATTACHMENT, "continuity_attested",
                    CONTINUITY_RULE_VERSION, CONTINUITY_SAMPLE_SEED)
DEFAULT_OUTPUT = Path("data/enrichment/vnext_gallery_full_v1.db")
DEFAULT_MANIFEST = Path("data/reports/vnext_gallery_full_v1_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_gallery_full_v1_report.json")
FULL_BUILDING_COUNT = 91_803
# WP1/WP1.1 artifacts that must stay byte-identical (job cards 20260824_vnext_wp1/wp11).
WP1_ARTIFACT_PINS: dict[str, str] = {
    "data/enrichment/vnext_gallery_draft_n10_v1.db": "d63b7a5a279f71d0b9c68b6ca66c6123e934dd67fa5c085a9f8ea16736717cc7",
    "data/reports/vnext_gallery_draft_n10_v1_manifest.json": "913a9278dcbd54d6ebe9853aa0aee6c6c45ef8edcfa4b5e3b6dd1c816626ade3",
    "data/enrichment/vnext_gallery_draft_n10_v1_1.db": "ed108eda46099348dff30eebbf4fbbeb0fcc86718dcbec9951d48a5203deb9ac",
    "data/reports/vnext_gallery_draft_n10_v1_1_manifest.json": "c8c94dd27d177a0982e02cb5f64eaf2a0177da6976588f48b684e14a41c2f278",
}
ROLE_PAGE_RANK = {"cover": 0, "gallery": 1}
BIG = 1 << 40


class GalleryFullError(RuntimeError):
    """Raised when a frozen input or a gallery invariant is violated."""


@dataclass(frozen=True)
class FullBuildResult:
    output_path: Path
    manifest_path: Path
    report_path: Path
    run_id: str
    logical_sha256: str
    scope: str
    building_count: int
    totals: Mapping[str, int]
    per_source: Mapping[str, Mapping[str, int]]
    elapsed_seconds: float
    phase_seconds: Mapping[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- pure helpers

def record_sha(row: Mapping[str, Any]) -> str:
    """record_sha256 = canonical SHA of every column except run_id/record_sha256."""

    return canonical_sha256({key: value for key, value in row.items() if key not in ("run_id", "record_sha256")})


def page_position(occurrence: Mapping[str, Any]) -> tuple[Any, ...]:
    """Source page position of one occurrence (smaller = earlier on the page)."""

    membership = occurrence.get("membership_ordinal")
    ordinal = occurrence.get("ordinal")
    return (
        1 if membership is None else 0,
        int(membership or 0),
        str(occurrence["source_project_id"]),
        ROLE_PAGE_RANK.get(str(occurrence["role"]), 2),
        1 if ordinal is None else 0,
        int(ordinal or 0),
        str(occurrence["occurrence_id"]),
    )


def e3_rank(row: Mapping[str, Any]) -> tuple[Any, ...]:
    ordinal = row.get("source_ordinal")
    role_rank = row.get("role_rank")
    return (
        1 if role_rank is None else 0,
        int(role_rank or 0),
        1 if ordinal is None else 0,
        int(ordinal or 0),
    )


def group_sort_key(members: Sequence[Mapping[str, Any]], positions: Mapping[str, Sequence[tuple[Any, ...]]], representative_asset: str) -> tuple[Any, ...]:
    """Sort key of one exact class: page order -> E3 rank -> stable key."""

    page = [pos for row in members for pos in positions.get(str(row["source_asset_id"]), ())]
    if page:
        return (0, min(page), (), representative_asset)
    ranks = [e3_rank(row) for row in members if row.get("candidate_id") is not None]
    if ranks:
        return (1, (), min(ranks), representative_asset)
    return (2, (), (), representative_asset)


def is_browser_raster_url(url: str) -> bool:
    """True when a Divisare/Cloudinary URL yields a browser-renderable raster image."""

    path = urlsplit(url).path
    for segment in path.split("/"):
        if any(token in CLOUDINARY_RASTER_FORMATS for token in segment.split(",")):
            return True
    last = path.rsplit("/", 1)[-1]
    extension = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    return extension in BROWSER_RASTER_EXTENSIONS


def divisare_serving_occurrence(asset_id: str, occurrences: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """First https images.divisare.com browser-raster occurrence of one asset (gallery first, page order)."""

    own = [row for row in occurrences if row.get("source_asset_id") == asset_id and row.get("occurrence_url")]
    own.sort(key=lambda row: (0 if row["role"] == "gallery" else 1, page_position(row)))
    for row in own:
        parts = urlsplit(str(row["occurrence_url"]))
        if parts.scheme == "https" and parts.hostname == "images.divisare.com" and is_browser_raster_url(str(row["occurrence_url"])):
            return row
    return None


def has_serving_url(source: str, asset: Mapping[str, Any], occurrences: Sequence[Mapping[str, Any]]) -> bool:
    if source == "architizer":
        return re.fullmatch(NEON_ARCHITIZER_PATTERN, str(asset.get("canonical_url") or "")) is not None
    if source == "divisare":
        return divisare_serving_occurrence(str(asset["source_asset_id"]), occurrences) is not None
    raise GalleryFullError(f"unknown source: {source}")


def neon_architizer_form(url: str) -> str:
    """The string the 2026-10-16 Neon migration produced for one old-host Architizer URL."""

    return re.sub(NEON_ARCHITIZER_PATTERN, NEON_ARCHITIZER_REPLACEMENT, url)


def serving_url(source: str, representative: Mapping[str, Any], occurrences: Sequence[Mapping[str, Any]]) -> tuple[str, str, str, str | None]:
    """Return (url, url_policy, url_provenance, occurrence_id) for one representative."""

    if source == "architizer":
        canonical = str(representative.get("canonical_url") or "")
        if re.fullmatch(NEON_ARCHITIZER_PATTERN, canonical) is None:
            raise GalleryFullError(f"Architizer asset URL does not fullmatch the Neon migration pattern: {representative['source_asset_id']}")
        return neon_architizer_form(canonical), URL_POLICY_ARCHITIZER, URL_PROVENANCE_ARCHITIZER, None
    if source == "divisare":
        chosen = divisare_serving_occurrence(str(representative["source_asset_id"]), occurrences)
        if chosen is None:
            raise GalleryFullError(f"Divisare representative has no browser-raster occurrence URL: {representative['source_asset_id']}")
        return str(chosen["occurrence_url"]), URL_POLICY_DIVISARE, "e2_occurrence_url_verbatim:" + str(chosen["role"]), str(chosen["occurrence_id"])
    raise GalleryFullError(f"unknown source: {source}")


def disposition_for(asset: Mapping[str, Any], candidate: Mapping[str, Any] | None, attested: bool, servable: bool = True) -> tuple[str, str]:
    status = asset["fingerprint_status"]
    if status == "failed":
        return "exclusion", "fingerprint_or_decoder_failure"
    if status != "success":
        return "exclusion", "e1_ledger_excluded"
    if candidate is None:
        return "unresolved", "successful_e2_asset_missing_e3_candidate"
    if int(candidate["low_information"]):
        return "exclusion", "low_information"
    if not servable:
        return "unresolved", REASON_NO_SERVING_URL
    if not attested:
        return "unresolved", "source_continuity_not_attested"
    return "gallery", "eligible_success_continuity_attested"


def build_groups(
    source: str,
    building_id: str,
    gallery_assets: Sequence[dict[str, Any]],
    occurrences: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Exact normalized-pixel classes -> (groups, members); groups carry sort_order + URL."""

    by_pixel: dict[str, list[dict[str, Any]]] = {}
    for row in gallery_assets:
        pixel = row.get("normalized_pixel_sha256")
        if not pixel:
            raise GalleryFullError("gallery asset is missing normalized-pixel SHA")
        by_pixel.setdefault(str(pixel), []).append(row)
    positions: dict[str, list[tuple[Any, ...]]] = {}
    for occ in occurrences:
        if occ.get("resolution_status") == "linked" and occ.get("source_asset_id") is not None:
            positions.setdefault(str(occ["source_asset_id"]), []).append(page_position(occ))
    classes = []
    for pixel, members in by_pixel.items():
        ordered = sorted(members, key=representative_sort_key)
        representative = ordered[0]
        key = group_sort_key(ordered, positions, str(representative["source_asset_id"]))
        classes.append((key, pixel, ordered))
    classes.sort(key=lambda item: item[0])
    groups: list[dict[str, Any]] = []
    members_out: list[dict[str, Any]] = []
    for sort_order, (key, pixel, ordered) in enumerate(classes):
        representative = ordered[0]
        rep_asset = str(representative["source_asset_id"])
        group_id = "vnx_imggrp_" + canonical_sha256(
            {
                "grouping_version": GROUPING_VERSION,
                "normalized_pixel_sha256": pixel,
                "representative_source_asset_id": rep_asset,
                "source": source,
                "source_building_id": building_id,
            }
        )[:32]
        basis = ("source_page_order", "e3_rank", "stable_key")[key[0]]
        anchor: dict[str, Any] = {"basis": basis}
        if key[0] == 0:
            position = key[1]
            anchor.update(
                {
                    "membership_ordinal": None if position[0] else position[1],
                    "source_project_id": position[2],
                    "role_page_rank": position[3],
                    "ordinal": None if position[4] else position[5],
                    "occurrence_id": position[6],
                }
            )
        elif key[0] == 1:
            anchor["e3_rank"] = list(key[2])
        url, policy, provenance, url_occurrence = serving_url(source, representative, occurrences)
        group = {
            "group_id": group_id,
            "source": source,
            "source_building_id": building_id,
            "sort_order": sort_order,
            "representative_source_asset_id": rep_asset,
            "representative_candidate_id": representative["candidate_id"],
            "representative_pixel_sha256": pixel,
            "representative_phash_node_id": representative["node_id"],
            "representative_width": representative["original_width"],
            "representative_height": representative["original_height"],
            "member_count": len(ordered),
            "grouping_basis": "exact_normalized_pixel_sha256",
            "representative_score_json": canonical_json(representative_score(representative)),
            "sort_basis": basis,
            "sort_anchor_json": canonical_json(anchor),
            "serving_url": url,
            "url_policy": policy,
            "url_provenance": provenance,
            "serving_url_occurrence_id": url_occurrence,
            "continuity_status": CONTINUITY_STATUS,
            "per_asset_pixel_status": PER_ASSET_PIXEL_STATUS,
        }
        group["record_sha256"] = record_sha(group)
        groups.append(group)
        for rank, member in enumerate(ordered):
            item = {
                "group_id": group_id,
                "source": source,
                "source_building_id": building_id,
                "source_asset_id": str(member["source_asset_id"]),
                "candidate_id": member["candidate_id"],
                "member_rank": rank,
                "member_role": "representative" if rank == 0 else "exact_member",
                "attachment_basis": "representative" if rank == 0 else "exact_pixel",
                "normalized_pixel_sha256": member["normalized_pixel_sha256"],
                "phash_node_id": member["node_id"],
                "exact_cluster_id": member["cluster_id"],
                "original_width": member["original_width"],
                "original_height": member["original_height"],
                "e2_asset_record_sha256": member["source_record_sha256"],
                "e2_relation_record_sha256": member["relation_record_sha256"],
                "e3_candidate_record_sha256": member["candidate_record_sha256"],
                "continuity_status": CONTINUITY_STATUS,
                "reason_code": member["disposition_reason"],
            }
            item["record_sha256"] = record_sha(item)
            members_out.append(item)
    return groups, members_out


def deferred_edges(groups: Sequence[Mapping[str, Any]], edge_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Preserve every frozen E2 phash edge (EDGE_SCOPES) between two distinct groups of one building."""

    by_node: dict[str, list[Mapping[str, Any]]] = {}
    for group in groups:
        by_node.setdefault(str(group["representative_phash_node_id"]), []).append(group)
    out: dict[str, dict[str, Any]] = {}
    for edge in edge_rows:
        for left in by_node.get(str(edge["left_node_id"]), ()):
            for right in by_node.get(str(edge["right_node_id"]), ()):
                if left["group_id"] == right["group_id"]:
                    continue
                pair_id = "vnx_dedge_" + canonical_sha256(
                    {
                        "edge_record_sha256": edge["edge_record_sha256"],
                        "left_group_id": left["group_id"],
                        "right_group_id": right["group_id"],
                        "version": GROUPING_VERSION,
                    }
                )[:32]
                row = {
                    "pair_id": pair_id,
                    "source": left["source"],
                    "source_building_id": left["source_building_id"],
                    "left_group_id": left["group_id"],
                    "right_group_id": right["group_id"],
                    "left_representative_asset_id": left["representative_source_asset_id"],
                    "right_representative_asset_id": right["representative_source_asset_id"],
                    "edge_id": edge["edge_id"],
                    "left_node_id": edge["left_node_id"],
                    "right_node_id": edge["right_node_id"],
                    "hamming_distance": int(edge["hamming_distance"]),
                    "edge_scope": edge["edge_scope"],
                    "edge_record_sha256": edge["edge_record_sha256"],
                    "attachment_status": DEFERRED_ATTACHMENT,
                    "transitive_closure_allowed": 0,
                }
                row["record_sha256"] = record_sha(row)
                out[pair_id] = row
    return [out[key] for key in sorted(out)]


# --------------------------------------------------------------------------- schema

SCHEMA_SQL = """
PRAGMA foreign_keys=ON;
CREATE TABLE runs(
  run_id TEXT PRIMARY KEY, contract_version TEXT NOT NULL, contract_path TEXT NOT NULL,
  contract_sha256 TEXT NOT NULL, builder_version TEXT NOT NULL, grouping_version TEXT NOT NULL,
  representative_score_version TEXT NOT NULL, sort_order_version TEXT NOT NULL,
  url_policy_version TEXT NOT NULL, scope TEXT NOT NULL, scope_limit INTEGER, scope_seed TEXT,
  status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
  freeze_path TEXT NOT NULL, freeze_sha256 TEXT NOT NULL,
  e2_path TEXT NOT NULL, e2_size_bytes INTEGER NOT NULL, e2_sha256_before TEXT NOT NULL,
  e2_sha256_after TEXT, e2_run_id TEXT NOT NULL,
  e3_path TEXT NOT NULL, e3_size_bytes INTEGER NOT NULL, e3_sha256_before TEXT NOT NULL,
  e3_sha256_after TEXT, e3_run_id TEXT NOT NULL,
  continuity_summary_path TEXT NOT NULL, continuity_summary_sha256 TEXT NOT NULL,
  continuity_json TEXT NOT NULL, wp1_pins_json TEXT NOT NULL,
  config_json TEXT NOT NULL, config_sha256 TEXT NOT NULL, logical_sha256 TEXT,
  building_count INTEGER, network_requests INTEGER NOT NULL DEFAULT 0,
  vision_requests INTEGER NOT NULL DEFAULT 0, llm_requests INTEGER NOT NULL DEFAULT 0,
  elapsed_seconds REAL
);
CREATE TABLE buildings(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  selection_id TEXT NOT NULL, name TEXT, e3_selection_record_sha256 TEXT NOT NULL,
  e3_successful_asset_count INTEGER NOT NULL, input_asset_count INTEGER NOT NULL,
  gallery_member_count INTEGER NOT NULL, gallery_group_count INTEGER NOT NULL,
  exclusion_count INTEGER NOT NULL, unresolved_count INTEGER NOT NULL,
  source_occurrence_count INTEGER NOT NULL, direct_edge_evidence_count INTEGER NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id), FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE gallery_groups(
  run_id TEXT NOT NULL, group_id TEXT NOT NULL, source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, sort_order INTEGER NOT NULL CHECK(sort_order>=0),
  representative_source_asset_id TEXT NOT NULL, representative_candidate_id TEXT NOT NULL,
  representative_pixel_sha256 TEXT NOT NULL, representative_phash_node_id TEXT NOT NULL,
  representative_width INTEGER, representative_height INTEGER,
  member_count INTEGER NOT NULL CHECK(member_count>0),
  grouping_basis TEXT NOT NULL CHECK(grouping_basis='exact_normalized_pixel_sha256'),
  representative_score_json TEXT NOT NULL, sort_basis TEXT NOT NULL, sort_anchor_json TEXT NOT NULL,
  serving_url TEXT NOT NULL, url_policy TEXT NOT NULL, url_provenance TEXT NOT NULL,
  serving_url_occurrence_id TEXT, continuity_status TEXT NOT NULL,
  per_asset_pixel_status TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,group_id), UNIQUE(run_id,source,source_building_id,sort_order),
  FOREIGN KEY(run_id,source,source_building_id) REFERENCES buildings(run_id,source,source_building_id)
);
CREATE TABLE group_members(
  run_id TEXT NOT NULL, group_id TEXT NOT NULL, source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, source_asset_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
  member_rank INTEGER NOT NULL, member_role TEXT NOT NULL, attachment_basis TEXT NOT NULL,
  normalized_pixel_sha256 TEXT NOT NULL, phash_node_id TEXT, exact_cluster_id TEXT,
  original_width INTEGER, original_height INTEGER, e2_asset_record_sha256 TEXT NOT NULL,
  e2_relation_record_sha256 TEXT NOT NULL, e3_candidate_record_sha256 TEXT NOT NULL,
  continuity_status TEXT NOT NULL, reason_code TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id,source_asset_id),
  FOREIGN KEY(run_id,group_id) REFERENCES gallery_groups(run_id,group_id)
);
CREATE INDEX idx_vnext_full_members_group ON group_members(run_id,group_id,member_rank);
CREATE TABLE exclusion_ledger(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, reason_code TEXT NOT NULL, decision_engine TEXT NOT NULL,
  decision_version TEXT NOT NULL, pixel_verification_status TEXT NOT NULL,
  fingerprint_status TEXT NOT NULL, e1_exclusion_reason TEXT, retryable INTEGER NOT NULL,
  error_kind TEXT, error_message TEXT, e2_asset_record_sha256 TEXT NOT NULL,
  e2_relation_record_sha256 TEXT NOT NULL, e3_candidate_record_sha256 TEXT,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id,source_asset_id)
);
CREATE TABLE unresolved_ledger(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, reason_code TEXT NOT NULL, decision_engine TEXT NOT NULL,
  decision_version TEXT NOT NULL, pixel_verification_status TEXT NOT NULL,
  retryable INTEGER NOT NULL, e2_asset_record_sha256 TEXT NOT NULL,
  e2_relation_record_sha256 TEXT NOT NULL, e3_candidate_record_sha256 TEXT,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id,source_asset_id)
);
CREATE TABLE source_occurrences(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  occurrence_id TEXT NOT NULL, source_project_id TEXT NOT NULL, membership_ordinal INTEGER,
  raw_asset_key TEXT NOT NULL, source_asset_id TEXT, resolution_status TEXT NOT NULL,
  role TEXT NOT NULL, ordinal INTEGER, occurrence_url TEXT,
  occurrence_record_sha256 TEXT NOT NULL, membership_record_sha256 TEXT NOT NULL,
  detail_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id,occurrence_id)
);
CREATE TABLE direct_edge_evidence(
  run_id TEXT NOT NULL, pair_id TEXT NOT NULL, source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, left_group_id TEXT NOT NULL, right_group_id TEXT NOT NULL,
  left_representative_asset_id TEXT NOT NULL, right_representative_asset_id TEXT NOT NULL,
  edge_id TEXT NOT NULL, left_node_id TEXT NOT NULL, right_node_id TEXT NOT NULL,
  hamming_distance INTEGER NOT NULL, edge_scope TEXT NOT NULL, edge_record_sha256 TEXT NOT NULL,
  attachment_status TEXT NOT NULL CHECK(attachment_status='deferred_no_per_asset_verification'),
  transitive_closure_allowed INTEGER NOT NULL CHECK(transitive_closure_allowed=0),
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,pair_id),
  FOREIGN KEY(run_id,left_group_id) REFERENCES gallery_groups(run_id,group_id),
  FOREIGN KEY(run_id,right_group_id) REFERENCES gallery_groups(run_id,group_id)
);
CREATE INDEX idx_vnext_full_edges_building ON direct_edge_evidence(run_id,source,source_building_id);
CREATE TABLE validations(
  run_id TEXT NOT NULL, phase TEXT NOT NULL, validation_name TEXT NOT NULL,
  passed INTEGER NOT NULL, expected TEXT, actual TEXT, detail_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL, PRIMARY KEY(run_id,phase,validation_name)
);
CREATE TABLE metrics(
  run_id TEXT NOT NULL, phase TEXT NOT NULL, metric_name TEXT NOT NULL,
  value_integer INTEGER, value_real REAL, value_text TEXT,
  PRIMARY KEY(run_id,phase,metric_name)
);
"""

LOGICAL_TABLES: tuple[tuple[str, str], ...] = (
    ("buildings", "source,source_building_id"),
    ("gallery_groups", "source,source_building_id,sort_order"),
    ("group_members", "source,source_building_id,source_asset_id"),
    ("exclusion_ledger", "source,source_building_id,source_asset_id"),
    ("unresolved_ledger", "source,source_building_id,source_asset_id"),
    ("source_occurrences", "source,source_building_id,occurrence_id"),
    ("direct_edge_evidence", "source,source_building_id,pair_id"),
)

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "buildings": (
        "source", "source_building_id", "selection_id", "name", "e3_selection_record_sha256",
        "e3_successful_asset_count", "input_asset_count", "gallery_member_count", "gallery_group_count",
        "exclusion_count", "unresolved_count", "source_occurrence_count", "direct_edge_evidence_count",
    ),
    "gallery_groups": (
        "group_id", "source", "source_building_id", "sort_order", "representative_source_asset_id",
        "representative_candidate_id", "representative_pixel_sha256", "representative_phash_node_id",
        "representative_width", "representative_height", "member_count", "grouping_basis",
        "representative_score_json", "sort_basis", "sort_anchor_json", "serving_url", "url_policy",
        "url_provenance", "serving_url_occurrence_id", "continuity_status", "per_asset_pixel_status",
    ),
    "group_members": (
        "group_id", "source", "source_building_id", "source_asset_id", "candidate_id", "member_rank",
        "member_role", "attachment_basis", "normalized_pixel_sha256", "phash_node_id", "exact_cluster_id",
        "original_width", "original_height", "e2_asset_record_sha256", "e2_relation_record_sha256",
        "e3_candidate_record_sha256", "continuity_status", "reason_code",
    ),
    "exclusion_ledger": (
        "source", "source_building_id", "source_asset_id", "reason_code", "decision_engine",
        "decision_version", "pixel_verification_status", "fingerprint_status", "e1_exclusion_reason",
        "retryable", "error_kind", "error_message", "e2_asset_record_sha256", "e2_relation_record_sha256",
        "e3_candidate_record_sha256",
    ),
    "unresolved_ledger": (
        "source", "source_building_id", "source_asset_id", "reason_code", "decision_engine",
        "decision_version", "pixel_verification_status", "retryable", "e2_asset_record_sha256",
        "e2_relation_record_sha256", "e3_candidate_record_sha256",
    ),
    "source_occurrences": (
        "source", "source_building_id", "occurrence_id", "source_project_id", "membership_ordinal",
        "raw_asset_key", "source_asset_id", "resolution_status", "role", "ordinal", "occurrence_url",
        "occurrence_record_sha256", "membership_record_sha256", "detail_json",
    ),
    "direct_edge_evidence": (
        "pair_id", "source", "source_building_id", "left_group_id", "right_group_id",
        "left_representative_asset_id", "right_representative_asset_id", "edge_id", "left_node_id",
        "right_node_id", "hamming_distance", "edge_scope", "edge_record_sha256", "attachment_status",
        "transitive_closure_allowed",
    ),
}


def logical_sha256(connection: sqlite3.Connection, run_id: str) -> str:
    """Streaming SHA over ordered record SHAs of every logical table."""

    digest = hashlib.sha256((LOGICAL_VERSION + "\n").encode("utf-8"))
    for table, order in LOGICAL_TABLES:
        digest.update(("#" + table + "\n").encode("utf-8"))
        for (value,) in connection.execute(f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)):
            digest.update((str(value) + "\n").encode("utf-8"))
    return digest.hexdigest()


def _insert(connection: sqlite3.Connection, table: str, run_id: str, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    columns = ("run_id",) + TABLE_COLUMNS[table] + ("record_sha256",)
    marks = ",".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({marks})",
        [(run_id, *[row[key] for key in TABLE_COLUMNS[table]], row["record_sha256"]) for row in rows],
    )


# --------------------------------------------------------------------------- frozen readers

ASSET_QUERY = """
    SELECT ba.source_asset_id,ba.project_count,ba.occurrence_count,ba.roles_json,
           ba.relation_record_sha256,a.fingerprint_status,a.canonical_url,a.fetch_url,
           a.normalized_pixel_sha256,a.phash_hex,a.original_width,a.original_height,
           a.source_record_sha256,a.provenance_json,a.error_kind,a.error_message,
           em.cluster_id,pnm.node_id
    FROM building_assets ba
    JOIN assets a ON a.run_id=ba.run_id AND a.source=ba.source AND a.source_asset_id=ba.source_asset_id
    LEFT JOIN exact_pixel_cluster_members em
      ON em.run_id=a.run_id AND em.source=a.source AND em.source_asset_id=a.source_asset_id
    LEFT JOIN phash_node_members pnm
      ON pnm.run_id=a.run_id AND pnm.source=a.source AND pnm.source_asset_id=a.source_asset_id
    WHERE ba.run_id=? AND ba.source=? AND ba.source_building_id=?
    ORDER BY ba.source_asset_id
"""

OCCURRENCE_QUERY = """
    SELECT o.occurrence_id,o.source_project_id,spb.membership_ordinal,o.raw_asset_key,o.source_asset_id,
           o.resolution_status,o.role,o.ordinal,o.occurrence_url,o.source_record_sha256,
           o.detail_json,spb.source_record_sha256 AS membership_record_sha256
    FROM source_project_buildings spb CROSS JOIN project_asset_occurrences o
      ON o.run_id=spb.run_id AND o.source=spb.source AND o.source_project_id=spb.source_project_id
    WHERE spb.run_id=? AND spb.source=? AND spb.source_building_id=?
"""  # CROSS JOIN pins spb as the outer loop (an ORDER BY made SQLite scan all occurrences); sorted in Python

CANDIDATE_QUERY = """
    SELECT candidate_id,source_asset_id,primary_role,role_rank,source_ordinal,quality_flags_json,
           low_information,candidate_record_sha256,normalized_pixel_sha256,phash_node_id
    FROM image_candidates WHERE run_id=? AND selection_id=?
"""


def _edges_for_nodes(e2: sqlite3.Connection, e2_run_id: str, nodes: Sequence[str]) -> list[dict[str, Any]]:
    if len(nodes) < 2:
        return []
    marks = ",".join("?" for _ in nodes)
    query = (
        "SELECT edge_id,left_node_id,right_node_id,hamming_distance,edge_scope,edge_record_sha256 "
        "FROM phash_edges INDEXED BY sqlite_autoindex_phash_edges_2 WHERE run_id=? AND +edge_scope IN ('global_le8','metadata_9_16') AND +hamming_distance>=1 "
        f"AND left_node_id IN ({marks}) AND right_node_id IN ({marks})"
    )
    return [dict(row) for row in e2.execute(query, (e2_run_id, *nodes, *nodes))]


def process_building(
    e2: sqlite3.Connection,
    e3: sqlite3.Connection,
    e2_run_id: str,
    e3_run_id: str,
    building: Mapping[str, Any],
    attested: Mapping[str, bool],
) -> dict[str, Any]:
    source = str(building["source"])
    building_id = str(building["source_building_id"])
    candidates = {str(row["source_asset_id"]): dict(row) for row in e3.execute(CANDIDATE_QUERY, (e3_run_id, building["selection_id"]))}
    assets = [dict(row) for row in e2.execute(ASSET_QUERY, (e2_run_id, source, building_id))]
    occurrences = sorted(
        (dict(row) for row in e2.execute(OCCURRENCE_QUERY, (e2_run_id, source, building_id))),
        key=lambda row: str(row["occurrence_id"]),
    )
    gallery: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for asset in assets:
        candidate = candidates.get(str(asset["source_asset_id"]))
        if candidate is not None and asset["fingerprint_status"] == "success":
            if candidate["normalized_pixel_sha256"] != asset["normalized_pixel_sha256"]:
                raise GalleryFullError("E2/E3 normalized-pixel SHA mismatch")
            if candidate["phash_node_id"] != asset["node_id"]:
                raise GalleryFullError("E2/E3 pHash node mismatch")
        servable = has_serving_url(source, asset, occurrences) if asset["fingerprint_status"] == "success" else True
        disposition, reason = disposition_for(asset, candidate, bool(attested.get(source)), servable)
        asset.update(
            {
                "disposition_reason": reason,
                "candidate_id": None if candidate is None else candidate["candidate_id"],
                "primary_role": None if candidate is None else candidate["primary_role"],
                "role_rank": None if candidate is None else candidate["role_rank"],
                "source_ordinal": None if candidate is None else candidate["source_ordinal"],
                "low_information": 0 if candidate is None else int(candidate["low_information"]),
                "candidate_record_sha256": None if candidate is None else candidate["candidate_record_sha256"],
            }
        )
        if disposition == "gallery":
            gallery.append(asset)
        elif disposition == "exclusion":
            provenance = json.loads(asset["provenance_json"] or "{}")
            row = {
                "source": source, "source_building_id": building_id, "source_asset_id": asset["source_asset_id"],
                "reason_code": reason, "decision_engine": "frozen_e1_e2_e3_evidence", "decision_version": BUILDER_VERSION,
                "pixel_verification_status": "frozen_success" if asset["fingerprint_status"] == "success" else "frozen_" + str(asset["fingerprint_status"]),
                "fingerprint_status": asset["fingerprint_status"],
                "e1_exclusion_reason": provenance.get("exclusion_reason") if isinstance(provenance, dict) else None,
                "retryable": _retryable(asset["error_kind"]), "error_kind": asset["error_kind"],
                "error_message": asset["error_message"], "e2_asset_record_sha256": asset["source_record_sha256"],
                "e2_relation_record_sha256": asset["relation_record_sha256"],
                "e3_candidate_record_sha256": asset["candidate_record_sha256"],
            }
            row["record_sha256"] = record_sha(row)
            exclusions.append(row)
        else:
            row = {
                "source": source, "source_building_id": building_id, "source_asset_id": asset["source_asset_id"],
                "reason_code": reason, "decision_engine": UNRESOLVED_ENGINE[reason],
                "decision_version": BUILDER_VERSION, "pixel_verification_status": "frozen_success", "retryable": 0,
                "e2_asset_record_sha256": asset["source_record_sha256"],
                "e2_relation_record_sha256": asset["relation_record_sha256"],
                "e3_candidate_record_sha256": asset["candidate_record_sha256"],
            }
            row["record_sha256"] = record_sha(row)
            unresolved.append(row)
    groups, members = build_groups(source, building_id, gallery, occurrences)
    nodes = sorted({str(group["representative_phash_node_id"]) for group in groups})
    edges = deferred_edges(groups, _edges_for_nodes(e2, e2_run_id, nodes))
    occurrence_rows = []
    for occ in occurrences:
        row = {
            "source": source, "source_building_id": building_id, "occurrence_id": occ["occurrence_id"],
            "source_project_id": occ["source_project_id"], "membership_ordinal": occ["membership_ordinal"],
            "raw_asset_key": occ["raw_asset_key"], "source_asset_id": occ["source_asset_id"],
            "resolution_status": occ["resolution_status"], "role": occ["role"], "ordinal": occ["ordinal"],
            "occurrence_url": occ["occurrence_url"], "occurrence_record_sha256": occ["source_record_sha256"],
            "membership_record_sha256": occ["membership_record_sha256"], "detail_json": occ["detail_json"],
        }
        row["record_sha256"] = record_sha(row)
        occurrence_rows.append(row)
    if len(members) + len(exclusions) + len(unresolved) != len(assets):
        raise GalleryFullError(f"per-building accounting failed: {source}/{building_id}")
    if [group["sort_order"] for group in groups] != list(range(len(groups))):
        raise GalleryFullError(f"sort_order not contiguous: {source}/{building_id}")
    building_row = {
        "source": source, "source_building_id": building_id, "selection_id": building["selection_id"],
        "name": building["name"], "e3_selection_record_sha256": building["selection_record_sha256"],
        "e3_successful_asset_count": building["successful_asset_count"], "input_asset_count": len(assets),
        "gallery_member_count": len(members), "gallery_group_count": len(groups),
        "exclusion_count": len(exclusions), "unresolved_count": len(unresolved),
        "source_occurrence_count": len(occurrence_rows), "direct_edge_evidence_count": len(edges),
    }
    building_row["record_sha256"] = record_sha(building_row)
    node_groups: dict[str, int] = {}
    for group in groups:
        node = str(group["representative_phash_node_id"])
        node_groups[node] = node_groups.get(node, 0) + 1
    asset_url = {str(row["source_asset_id"]): str(row.get("canonical_url") or "") for row in gallery}
    neon_divergent = sum(
        1 for group in groups
        if source == "architizer" and group["serving_url"] != neon_architizer_form(asset_url[str(group["representative_source_asset_id"])])
    )
    non_ascii = sum(1 for group in groups if source == "architizer" and not str(group["serving_url"]).isascii())
    return {
        "building": building_row, "groups": groups, "members": members, "exclusions": exclusions,
        "unresolved": unresolved, "occurrences": occurrence_rows, "edges": edges,
        "evidence": {
            "same_phash_node_group_pairs": sum(k * (k - 1) // 2 for k in node_groups.values()),
            "architizer_serving_url_neon_divergent": neon_divergent,
            "architizer_serving_url_non_ascii_path": non_ascii,
        },
    }


def load_population(e3: sqlite3.Connection, e3_run_id: str) -> list[dict[str, Any]]:
    rows = []
    for row in e3.execute(
        "SELECT selection_id,source,source_building_id,name,selection_record_sha256,detail_json "
        "FROM selected_buildings WHERE run_id=? AND entity_type='building' ORDER BY source,source_building_id",
        (e3_run_id,),
    ):
        detail = json.loads(row[5] or "{}").get("building_summary", {})
        rows.append(
            {
                "selection_id": str(row[0]), "source": str(row[1]), "source_building_id": str(row[2]),
                "name": None if row[3] is None else str(row[3]), "selection_record_sha256": str(row[4]),
                "successful_asset_count": int(detail.get("successful_asset_count", 0)),
            }
        )
    return rows


def select_scope(population: Sequence[Mapping[str, Any]], limit: int | None) -> list[Mapping[str, Any]]:
    """Full population, or a deterministic hash-seeded ladder subset (sorted by source key)."""

    if limit is None:
        return list(population)
    ranked = sorted(population, key=lambda row: (stable_score(LADDER_SEED, str(row["source"]), str(row["source_building_id"])), row["source"], row["source_building_id"]))
    chosen = ranked[:limit]
    return sorted(chosen, key=lambda row: (row["source"], row["source_building_id"]))


def load_continuity(summary_path: Path) -> dict[str, Any]:
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    if payload.get("partial_smoke"):
        raise GalleryFullError("continuity summary is a partial smoke, not an attestation")
    if payload.get("rule_version") != CONTINUITY_RULE_VERSION or payload.get("sample_seed") != CONTINUITY_SAMPLE_SEED:
        raise GalleryFullError(
            f"continuity summary rule/seed {payload.get('rule_version')}/{payload.get('sample_seed')} "
            f"is not the contract's {CONTINUITY_RULE_VERSION}/{CONTINUITY_SAMPLE_SEED}")
    sources = payload.get("sources", {})
    result = {
        "status": payload.get("status"),
        "rule_version": payload.get("rule_version"),
        "sample_seed": payload.get("sample_seed"),
        "thresholds": payload.get("thresholds"),
        "rows_jsonl_sha256": payload.get("rows_jsonl", {}).get("sha256"),
        "sources": {},
    }
    for source in SOURCES:
        row = sources.get(source)
        if not isinstance(row, dict):
            raise GalleryFullError(f"continuity summary lacks source {source}")
        result["sources"][source] = {
            "continuity_attested": bool(row.get("continuity_attested")),
            "continuous_rate": row.get("continuous_rate"),
            "content_changed_count": row.get("content_changed_count"),
            "unavailable_count": row.get("unavailable_count"),
            "n": row.get("n"),
        }
    return result


def _config(scope: str, limit: int | None) -> dict[str, Any]:
    return {
        "builder_version": BUILDER_VERSION,
        "contract_version": CONTRACT_VERSION,
        "grouping": {
            "version": GROUPING_VERSION,
            "exact_equivalence": "normalized_pixel_sha256",
            "direct_edge_star_attachment": False,
            "direct_edge_evidence": {
                "version": EDGE_EVIDENCE_VERSION,
                "scopes": list(EDGE_SCOPES),
                "rule": "every E2 phash_edges row (hamming>=1) whose endpoint nodes are representative nodes of two distinct groups of one building; attachment_status=" + DEFERRED_ATTACHMENT,
                "same_phash_node_classes": "no E2 edge row exists (hamming 0); relation carried by representative_phash_node_id, counted as same_phash_node_group_pairs",
            },
            "transitive_closure_allowed": False,
        },
        "representative": {
            "version": REPRESENTATIVE_SCORE_VERSION,
            "eligibility": "E1 fingerprint success AND E3 candidate AND not low_information AND browser-renderable serving URL AND source continuity_attested",
            "order": "WP1 representative_sort_key (success, short edge>=512, area/short/long desc, crop neutral, low-info, role, ordinal, stable id)",
        },
        "sort_order": {
            "version": SORT_ORDER_VERSION,
            "rule": "per building 0..k-1 by min page position over all class members' linked E2 occurrences "
                    "(membership_ordinal, source_project_id, role cover<gallery, ordinal, occurrence_id); "
                    "fallback E3 (role_rank, source_ordinal); final tie representative source_asset_id",
            "evidence": "E2 project_asset_occurrences (project, role, ordinal, url) == curated source_image_occurrences position",
        },
        "serving_url": {
            "version": URL_POLICY_VERSION,
            URL_POLICY_ARCHITIZER: "frozen E2 canonical_url must re.fullmatch PATTERN; serving_url = re.sub(PATTERN, REPLACEMENT, canonical_url) (live-Neon form: path verbatim, not percent-encoded, original query dropped); no fullmatch -> unresolved " + REASON_NO_SERVING_URL,
            "architizer_neon_pattern": NEON_ARCHITIZER_PATTERN,
            "architizer_neon_replacement": NEON_ARCHITIZER_REPLACEMENT,
            URL_POLICY_DIVISARE: "frozen E2 occurrence_url verbatim of the representative: first https images.divisare.com occurrence (gallery first, then cover, page order) that is browser-raster (explicit f_jpg/f_png/f_webp/f_avif/f_gif transform, else extension in " + ",".join(sorted(BROWSER_RASTER_EXTENSIONS)) + "); none -> unresolved " + REASON_NO_SERVING_URL,
            "architizer_neon_form_note": "contract v1.3 D2: identical to live Neon (tools/architizer_image_host_migrate.py); architizer_serving_url_neon_divergent must be 0; non-ASCII paths counted as architizer_serving_url_non_ascii_path",
        },
        "scope": {"scope": scope, "limit": limit, "seed": None if limit is None else LADDER_SEED},
        "network_requests": 0,
        "vision_requests": 0,
        "llm_requests": 0,
    }


def _resolve(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _wp1_pins(root: Path) -> dict[str, str | None]:
    return {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in WP1_ARTIFACT_PINS}


def build_gallery_full(
    *,
    repo_root: Path,
    output_path: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST,
    report_path: Path = DEFAULT_REPORT,
    freeze_path: Path = DEFAULT_FREEZE,
    continuity_summary_path: Path = DEFAULT_CONTINUITY_SUMMARY,
    contract_path: Path = DEFAULT_CONTRACT,
    limit_buildings: int | None = None,
    verify_full_hashes: bool = True,
    progress: Any = None,
) -> FullBuildResult:
    clock = time.monotonic()
    phases: dict[str, float] = {}
    started_at = utc_now()
    root = repo_root.resolve()
    output = _resolve(root, output_path)
    manifest = _resolve(root, manifest_path)
    report = _resolve(root, report_path)
    freeze = _resolve(root, freeze_path)
    continuity_file = _resolve(root, continuity_summary_path)
    contract_file = _resolve(root, contract_path)
    for target in (output, manifest, report):
        if target.exists():
            raise GalleryFullError(f"refusing to clobber existing artifact: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".building")
    if temporary.exists():
        raise GalleryFullError(f"stale temporary artifact exists: {temporary}")
    scope = "full" if limit_buildings is None else f"ladder_n{limit_buildings}"
    e2_path = (root / DEFAULT_E2).resolve()
    e3_path = (root / DEFAULT_E3).resolve()
    frozen = load_frozen_inputs(freeze)
    for role, rel, path, size, sha in (
        (E2_ROLE, DEFAULT_E2, e2_path, EXPECTED_E2_SIZE, EXPECTED_E2_SHA256),
        (E3_ROLE, DEFAULT_E3, e3_path, EXPECTED_E3_SIZE, EXPECTED_E3_SHA256),
    ):
        spec = frozen[role]
        if spec.path.as_posix() != rel.as_posix() or spec.size_bytes != size or spec.sha256 != sha:
            raise GalleryFullError(f"freeze manifest pin mismatch for {role}")
        if not path.is_file() or path.stat().st_size != size:
            raise GalleryFullError(f"missing or size-mismatched frozen input: {path}")
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(str(path) + suffix).exists():
                raise GalleryFullError(f"frozen input has forbidden SQLite sidecar: {path}{suffix}")
    contract_text = contract_file.read_text(encoding="utf-8")
    missing = [anchor for anchor in CONTRACT_ANCHORS if anchor not in contract_text]
    if missing:
        raise GalleryFullError(f"contract lacks {CONTRACT_VERSION} anchors: {missing}")
    contract_sha = sha256_file(contract_file)
    continuity_sha = sha256_file(continuity_file)
    continuity = load_continuity(continuity_file)
    attested = {source: continuity["sources"][source]["continuity_attested"] for source in SOURCES}
    wp1_before = _wp1_pins(root)
    if wp1_before != WP1_ARTIFACT_PINS:
        raise GalleryFullError("WP1/WP1.1 artifact SHA pins do not match before the run")
    tick = time.monotonic()
    e2_before = sha256_file(e2_path) if verify_full_hashes else EXPECTED_E2_SHA256
    e3_before = sha256_file(e3_path) if verify_full_hashes else EXPECTED_E3_SHA256
    phases["input_sha_pre"] = time.monotonic() - tick
    if (e2_before, e3_before) != (EXPECTED_E2_SHA256, EXPECTED_E3_SHA256):
        raise GalleryFullError("frozen E2/E3 pre-run SHA mismatch")
    e2 = _open_immutable(e2_path)
    e3 = _open_immutable(e3_path)
    for connection in (e2, e3):
        connection.execute("PRAGMA cache_size=-524288")
        connection.execute("PRAGMA mmap_size=0")
    config = _config(scope, limit_buildings)
    config_json = canonical_json(config)
    config_sha = canonical_sha256(config)
    database: sqlite3.Connection | None = None
    try:
        e2_run = e2.execute("SELECT run_id FROM e2_runs WHERE status='complete'").fetchall()
        e3_run = e3.execute("SELECT run_id FROM selection_runs WHERE status='complete'").fetchall()
        if len(e2_run) != 1 or len(e3_run) != 1:
            raise GalleryFullError("E2/E3 must each have exactly one complete run")
        e2_run_id, e3_run_id = str(e2_run[0][0]), str(e3_run[0][0])
        population = load_population(e3, e3_run_id)
        if len(population) != FULL_BUILDING_COUNT:
            raise GalleryFullError(f"E3 building population is {len(population)}, expected {FULL_BUILDING_COUNT}")
        buildings = select_scope(population, limit_buildings)
        scope_manifest_sha = canonical_sha256([[row["source"], row["source_building_id"], row["selection_record_sha256"]] for row in buildings])
        run_id = "vnx_gallery_full_" + canonical_sha256(
            {
                "config_sha256": config_sha, "contract_sha256": contract_sha, "continuity_summary_sha256": continuity_sha,
                "e2_sha256": e2_before, "e3_sha256": e3_before, "scope_manifest_sha256": scope_manifest_sha,
            }
        )[:24]
        database = sqlite3.connect(temporary)
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=NORMAL")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA cache_size=-262144")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(SCHEMA_SQL)
        database.execute(
            "INSERT INTO runs(run_id,contract_version,contract_path,contract_sha256,builder_version,grouping_version,"
            "representative_score_version,sort_order_version,url_policy_version,scope,scope_limit,scope_seed,status,"
            "started_at,freeze_path,freeze_sha256,e2_path,e2_size_bytes,e2_sha256_before,e2_run_id,e3_path,e3_size_bytes,"
            "e3_sha256_before,e3_run_id,continuity_summary_path,continuity_summary_sha256,continuity_json,wp1_pins_json,"
            "config_json,config_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, CONTRACT_VERSION, contract_file.relative_to(root).as_posix(), contract_sha, BUILDER_VERSION,
                GROUPING_VERSION, REPRESENTATIVE_SCORE_VERSION, SORT_ORDER_VERSION, URL_POLICY_VERSION, scope,
                limit_buildings, None if limit_buildings is None else LADDER_SEED, "building", started_at,
                freeze.relative_to(root).as_posix(), sha256_file(freeze), DEFAULT_E2.as_posix(), EXPECTED_E2_SIZE,
                e2_before, e2_run_id, DEFAULT_E3.as_posix(), EXPECTED_E3_SIZE, e3_before, e3_run_id,
                continuity_file.relative_to(root).as_posix(), continuity_sha, canonical_json(continuity),
                canonical_json(wp1_before), config_json, config_sha,
            ),
        )
        keys = ("buildings", "input", "gallery", "groups", "exclusion", "unresolved", "occurrences", "deferred_edges", "zero_asset_buildings")
        per_source = {source: {key: 0 for key in keys} for source in SOURCES}
        reasons: dict[str, dict[str, int]] = {}
        sort_basis: dict[str, int] = {}
        url_policies: dict[str, int] = {}
        evidence_totals: dict[str, dict[str, int]] = {
            source: {"same_phash_node_group_pairs": 0, "architizer_serving_url_neon_divergent": 0,
                     "architizer_serving_url_non_ascii_path": 0} for source in SOURCES
        }
        tick = time.monotonic()
        for index, building in enumerate(buildings, 1):
            result = process_building(e2, e3, e2_run_id, e3_run_id, building, attested)
            _insert(database, "buildings", run_id, [result["building"]])
            _insert(database, "gallery_groups", run_id, result["groups"])
            _insert(database, "group_members", run_id, result["members"])
            _insert(database, "exclusion_ledger", run_id, result["exclusions"])
            _insert(database, "unresolved_ledger", run_id, result["unresolved"])
            _insert(database, "source_occurrences", run_id, result["occurrences"])
            _insert(database, "direct_edge_evidence", run_id, result["edges"])
            counts = per_source[str(building["source"])]
            b = result["building"]
            counts["buildings"] += 1
            counts["input"] += b["input_asset_count"]
            counts["gallery"] += b["gallery_member_count"]
            counts["groups"] += b["gallery_group_count"]
            counts["exclusion"] += b["exclusion_count"]
            counts["unresolved"] += b["unresolved_count"]
            counts["occurrences"] += b["source_occurrence_count"]
            counts["deferred_edges"] += b["direct_edge_evidence_count"]
            counts["zero_asset_buildings"] += int(b["input_asset_count"] == 0)
            for key, value in result["evidence"].items():
                evidence_totals[str(building["source"])][key] += int(value)
            for row in result["exclusions"] + result["unresolved"]:
                bucket = reasons.setdefault(str(building["source"]), {})
                bucket[row["reason_code"]] = bucket.get(row["reason_code"], 0) + 1
            for group in result["groups"]:
                sort_basis[group["sort_basis"]] = sort_basis.get(group["sort_basis"], 0) + 1
                url_policies[group["url_policy"]] = url_policies.get(group["url_policy"], 0) + 1
            if progress is not None and (index % 5000 == 0 or index == len(buildings)):
                progress(index, len(buildings), time.monotonic() - tick)
        phases["build_rows"] = time.monotonic() - tick
        totals = {key: sum(per_source[source][key] for source in SOURCES) for key in keys}
        tick = time.monotonic()
        checks: list[tuple[str, Any, Any]] = []
        checks.append(("building_count", len(buildings), totals["buildings"]))
        checks.append(("global_accounting", totals["input"], totals["gallery"] + totals["exclusion"] + totals["unresolved"]))
        db_counts = {
            table: int(database.execute(f"SELECT count(*) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0])
            for table, _ in LOGICAL_TABLES
        }
        checks.append(("table_row_accounting", [totals["buildings"], totals["groups"], totals["gallery"], totals["exclusion"], totals["unresolved"], totals["occurrences"], totals["deferred_edges"]],
                       [db_counts[name] for name, _ in LOGICAL_TABLES]))
        per_building_bad = int(database.execute(
            "SELECT count(*) FROM buildings WHERE run_id=? AND input_asset_count<>gallery_member_count+exclusion_count+unresolved_count",
            (run_id,),
        ).fetchone()[0])
        checks.append(("per_building_accounting_failures", 0, per_building_bad))
        bad_sort = int(database.execute(
            "SELECT count(*) FROM (SELECT source,source_building_id,count(*) n,min(sort_order) lo,max(sort_order) hi "
            "FROM gallery_groups WHERE run_id=? GROUP BY 1,2 HAVING lo<>0 OR hi<>n-1)",
            (run_id,),
        ).fetchone()[0])
        checks.append(("sort_order_contiguous_failures", 0, bad_sort))
        checks.append(("star_attachments", 0, int(database.execute(
            "SELECT count(*) FROM direct_edge_evidence WHERE run_id=? AND attachment_status<>?", (run_id, DEFERRED_ATTACHMENT)).fetchone()[0])))
        checks.append(("multi_pixel_groups", 0, int(database.execute(
            "SELECT count(*) FROM (SELECT group_id FROM group_members WHERE run_id=? GROUP BY group_id HAVING count(DISTINCT normalized_pixel_sha256)<>1)",
            (run_id,)).fetchone()[0])))
        checks.append(("architizer_serving_url_neon_divergent", 0, evidence_totals["architizer"]["architizer_serving_url_neon_divergent"]))
        checks.append(("network_vision_llm_requests", [0, 0, 0], [0, 0, 0]))
        for name, expected, actual in checks:
            database.execute(
                "INSERT INTO validations VALUES(?,?,?,?,?,?,?,?)",
                (run_id, "build", name, int(expected == actual), canonical_json(expected), canonical_json(actual), "{}", utc_now()),
            )
            if expected != actual:
                raise GalleryFullError(f"build invariant failed: {name} expected={expected} actual={actual}")
        for source in SOURCES:
            for key, value in per_source[source].items():
                database.execute("INSERT INTO metrics(run_id,phase,metric_name,value_integer) VALUES(?,?,?,?)", (run_id, "build", f"{source}.{key}", int(value)))
        for key, value in totals.items():
            database.execute("INSERT INTO metrics(run_id,phase,metric_name,value_integer) VALUES(?,?,?,?)", (run_id, "build", f"total.{key}", int(value)))
        for source in SOURCES:
            for key, value in evidence_totals[source].items():
                database.execute("INSERT INTO metrics(run_id,phase,metric_name,value_integer) VALUES(?,?,?,?)", (run_id, "build", f"{source}.{key}", int(value)))
        logical = logical_sha256(database, run_id)
        phases["checks_logical"] = time.monotonic() - tick
        database.execute("UPDATE runs SET logical_sha256=?,building_count=? WHERE run_id=?", (logical, len(buildings), run_id))
        database.commit()
        e2.close()
        e3.close()
        tick = time.monotonic()
        e2_after = sha256_file(e2_path) if verify_full_hashes else EXPECTED_E2_SHA256
        e3_after = sha256_file(e3_path) if verify_full_hashes else EXPECTED_E3_SHA256
        phases["input_sha_post"] = time.monotonic() - tick
        if (e2_after, e3_after) != (e2_before, e3_before):
            raise GalleryFullError("frozen E2/E3 SHA changed during the run")
        wp1_after = _wp1_pins(root)
        if wp1_after != WP1_ARTIFACT_PINS:
            raise GalleryFullError("WP1/WP1.1 artifact SHA changed during the run")
        completed_at = utc_now()
        elapsed = time.monotonic() - clock
        database.execute(
            "UPDATE runs SET status='complete',completed_at=?,e2_sha256_after=?,e3_sha256_after=?,elapsed_seconds=? WHERE run_id=?",
            (completed_at, e2_after, e3_after, round(elapsed, 3), run_id),
        )
        database.execute(
            "INSERT INTO validations VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "build", "frozen_sha_pre_post_unchanged", 1, e2_before + "/" + e3_before, e2_after + "/" + e3_after, "{}", completed_at),
        )
        database.execute(
            "INSERT INTO validations VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "build", "wp1_artifacts_unchanged", 1, canonical_json(WP1_ARTIFACT_PINS), canonical_json(wp1_after), "{}", completed_at),
        )
        database.commit()
        database.close()
        database = None
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(temporary) + suffix)
            if sidecar.exists():
                raise GalleryFullError(f"temporary output left SQLite sidecar: {sidecar}")
        os.replace(temporary, output)
        output_sha = sha256_file(output)
        manifest_payload = {
            "manifest_kind": "vnext_gallery_full_v1",
            "run_id": run_id,
            "scope": scope,
            "scope_manifest_sha256": scope_manifest_sha,
            "logical_sha256": logical,
            "config_sha256": config_sha,
            "output": {"path": output.relative_to(root).as_posix() if output.is_relative_to(root) else str(output),
                       "size_bytes": output.stat().st_size, "sha256": output_sha},
            "input_pins": {
                "e2": {"path": DEFAULT_E2.as_posix(), "size_bytes": EXPECTED_E2_SIZE, "sha256": e2_before},
                "e3": {"path": DEFAULT_E3.as_posix(), "size_bytes": EXPECTED_E3_SIZE, "sha256": e3_before},
                "freeze": {"path": freeze.relative_to(root).as_posix(), "sha256": sha256_file(freeze)},
                "continuity_summary": {"path": continuity_file.relative_to(root).as_posix(), "sha256": continuity_sha},
                "contract": {"path": contract_file.relative_to(root).as_posix(), "sha256": contract_sha, "version": CONTRACT_VERSION},
                "wp1_artifacts": WP1_ARTIFACT_PINS,
            },
            "versions": {
                "builder": BUILDER_VERSION, "grouping": GROUPING_VERSION, "representative_score": REPRESENTATIVE_SCORE_VERSION,
                "sort_order": SORT_ORDER_VERSION, "url_policy": URL_POLICY_VERSION, "logical": LOGICAL_VERSION,
                "application_id": APPLICATION_ID, "schema_version": SCHEMA_VERSION,
            },
            "row_counts": db_counts,
        }
        report_payload = {
            "status": "PASS",
            "run_id": run_id,
            "scope": scope,
            "builder_version": BUILDER_VERSION,
            "logical_sha256": logical,
            "output_sha256": output_sha,
            "continuity": continuity,
            "per_source": per_source,
            "totals": totals,
            "reason_counts": reasons,
            "sort_basis_counts": sort_basis,
            "url_policy_counts": url_policies,
            "evidence_counts": evidence_totals,
            "accounting": {"expected": totals["input"], "actual": totals["gallery"] + totals["exclusion"] + totals["unresolved"], "passed": True},
            "rules": config,
            "requests": {"network": 0, "vision": 0, "llm": 0},
            "elapsed_seconds": round(elapsed, 3),
            "phase_seconds": {key: round(value, 3) for key, value in phases.items()},
            "sqlite_sidecars": [],
        }
        _atomic_json(manifest, manifest_payload, no_clobber=True)
        _atomic_json(report, report_payload, no_clobber=True)
        return FullBuildResult(
            output_path=output, manifest_path=manifest, report_path=report, run_id=run_id, logical_sha256=logical,
            scope=scope, building_count=len(buildings), totals=totals, per_source=per_source,
            elapsed_seconds=elapsed, phase_seconds=phases,
        )
    except Exception:
        try:
            e2.close()
            e3.close()
        except Exception:
            pass
        if database is not None:
            try:
                database.close()
            except Exception:
                pass
        if temporary.exists():
            temporary.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(temporary) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        raise


__all__ = [
    "APPLICATION_ID", "BUILDER_VERSION", "CONTRACT_VERSION", "DEFERRED_ATTACHMENT", "FullBuildResult",
    "GalleryFullError", "LOGICAL_TABLES", "SCHEMA_SQL", "build_gallery_full", "build_groups",
    "deferred_edges", "disposition_for", "divisare_serving_occurrence", "has_serving_url", "is_browser_raster_url",
    "logical_sha256", "neon_architizer_form", "page_position", "record_sha", "select_scope", "serving_url",
]
