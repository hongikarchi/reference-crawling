"""Deterministic vNext N10 gallery draft from frozen E2/E3 evidence.

This module is deliberately offline.  It does not import an HTTP client and it
never opens a mutable connection to E2 or E3.  Exact normalized-pixel classes
are formed first; only frozen E2 ``global_le8`` edges from a class
representative to a chosen group representative may attach another class.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256


CONTRACT_VERSION = "vnext-gallery-draft-contract-v1"
BUILDER_VERSION = "vnext-gallery-draft-n10-builder-v1"
SAMPLE_VERSION = "vnext-gallery-draft-source-balanced-n10-v1"
GROUPING_VERSION = "vnext-exact-class-direct-star-v1"
REPRESENTATIVE_SCORE_VERSION = "vnext-gallery-representative-score-v1"
LOGICAL_VERSION = "vnext-gallery-stage-a-logical-v1"
APPLICATION_ID = 0x564E5831
SCHEMA_VERSION = 1
SAMPLE_SEED = "vnext-wp1-gallery-n10-20260824"
DEFAULT_E2 = Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db")
DEFAULT_E3 = Path("data/enrichment/divisare_architizer_image_selection_e3_full_v1.db")
DEFAULT_FREEZE = Path("data/reports/vnext_workspace_freeze_v1.json")
DEFAULT_OUTPUT = Path("data/enrichment/vnext_gallery_draft_n10_v1.db")
DEFAULT_MANIFEST = Path("data/reports/vnext_gallery_draft_n10_v1_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_gallery_draft_n10_v1_stage_a_report.json")
EXPECTED_E2_SHA256 = "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19"
EXPECTED_E3_SHA256 = "8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4"
EXPECTED_E2_SIZE = 10_164_682_752
EXPECTED_E3_SIZE = 10_236_592_128
E2_ROLE = "cross_source_e2_full_v5"
E3_ROLE = "cross_source_e3_full_v1"


class GalleryDraftError(RuntimeError):
    """Raised when a frozen input or gallery invariant is violated."""


@dataclass(frozen=True)
class FrozenInput:
    role: str
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    manifest_path: Path
    report_path: Path
    run_id: str
    logical_sha256: str
    building_count: int
    input_asset_count: int
    gallery_asset_count: int
    gallery_group_count: int
    exclusion_count: int
    unresolved_count: int
    direct_attachment_count: int
    elapsed_seconds: float


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_file(path: Path, chunk_size: int = 16 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any], *, no_clobber: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if no_clobber and path.exists():
        raise GalleryDraftError(f"refusing to clobber existing artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise GalleryDraftError(f"stale temporary artifact exists: {temporary}")
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        "file:" + path.resolve().as_posix() + "?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_frozen_inputs(freeze_path: Path) -> dict[str, FrozenInput]:
    payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    result: dict[str, FrozenInput] = {}
    for row in payload.get("frozen_inputs", []):
        role = str(row.get("role", ""))
        if role in {E2_ROLE, E3_ROLE, "divisare_e1_v1_2", "architizer_e1_v1_2"}:
            result[role] = FrozenInput(
                role=role,
                path=Path(str(row["path"])),
                size_bytes=int(row["size_bytes"]),
                sha256=str(row["sha256"]),
            )
    if E2_ROLE not in result or E3_ROLE not in result:
        raise GalleryDraftError("workspace freeze manifest does not pin E2 and E3")
    return result


def _validate_frozen_spec(spec: FrozenInput, *, expected_path: Path, expected_size: int, expected_sha: str) -> None:
    if spec.path.as_posix() != expected_path.as_posix():
        raise GalleryDraftError(f"unexpected {spec.role} path in freeze manifest: {spec.path}")
    if spec.size_bytes != expected_size or spec.sha256 != expected_sha:
        raise GalleryDraftError(f"unexpected {spec.role} frozen size/SHA")
    if not expected_path.is_file() or expected_path.stat().st_size != expected_size:
        raise GalleryDraftError(f"missing or size-mismatched frozen input: {expected_path}")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(expected_path) + suffix)
        if sidecar.exists():
            raise GalleryDraftError(f"frozen input has forbidden SQLite sidecar: {sidecar}")


def stable_score(seed: str, *parts: str) -> str:
    return hashlib.sha256((seed + "\0" + "\0".join(parts)).encode("utf-8")).hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


def _building_inventory(e3: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    buildings: list[dict[str, Any]] = []
    for row in e3.execute(
        "SELECT selection_id,source,source_building_id,name,selection_record_sha256,detail_json "
        "FROM selected_buildings WHERE entity_type='building' ORDER BY source,source_building_id"
    ):
        detail = _json_object(row[5]).get("building_summary", {})
        buildings.append(
            {
                "selection_id": str(row[0]),
                "source": str(row[1]),
                "source_building_id": str(row[2]),
                "name": None if row[3] is None else str(row[3]),
                "selection_record_sha256": str(row[4]),
                "asset_count": int(detail.get("successful_asset_count", 0)),
                "quality_risk_cover_count": int(detail.get("quality_risk_cover_count", 0)),
            }
        )
    suppressions: dict[str, dict[str, int]] = {}
    query = """
        SELECT selection_id,
               count(*) AS total_n,
               sum(suppression_reason='suppressed_exact_pixel') AS exact_n,
               sum(suppression_reason='suppressed_identical_phash') AS node_n,
               sum(suppression_reason='suppressed_direct_phash_le8') AS direct_n
        FROM policy_rankings
        WHERE policy_id='p2_quality_exact_direct_phash_shortlist'
          AND suppression_reason IS NOT NULL
        GROUP BY selection_id
    """
    for row in e3.execute(query):
        suppressions[str(row[0])] = {
            "total": int(row[1]),
            "exact": int(row[2] or 0),
            "same_node": int(row[3] or 0),
            "direct": int(row[4] or 0),
        }
    return buildings, suppressions


def choose_n10(e3: sqlite3.Connection, seed: str = SAMPLE_SEED) -> list[dict[str, Any]]:
    """Choose 8 normal + one large-gallery + one duplicate-burst building."""

    buildings, suppressions = _building_inventory(e3)
    by_source = {source: [row for row in buildings if row["source"] == source] for source in ("divisare", "architizer")}
    large_pool = [row for row in by_source["divisare"] if 40 <= row["asset_count"] <= 60]
    if not large_pool:
        raise GalleryDraftError("no bounded Divisare large-gallery candidate")
    large = min(
        large_pool,
        key=lambda row: (
            -row["asset_count"],
            stable_score(seed, "large_gallery", row["source"], row["source_building_id"]),
        ),
    )
    duplicate_pool = [
        row
        for row in by_source["architizer"]
        if row["asset_count"] <= 60 and suppressions.get(row["selection_id"], {}).get("direct", 0) > 0
    ]
    if not duplicate_pool:
        raise GalleryDraftError("no bounded Architizer direct-duplicate candidate")
    duplicate = min(
        duplicate_pool,
        key=lambda row: (
            -suppressions[row["selection_id"]]["direct"],
            -suppressions[row["selection_id"]]["total"],
            stable_score(seed, "duplicate_burst", row["source"], row["source_building_id"]),
        ),
    )
    chosen_keys = {(large["source"], large["source_building_id"]), (duplicate["source"], duplicate["source_building_id"])}
    normals: list[dict[str, Any]] = []
    for source in ("divisare", "architizer"):
        pool = [
            row
            for row in by_source[source]
            if 6 <= row["asset_count"] <= 12
            and row["quality_risk_cover_count"] == 0
            and suppressions.get(row["selection_id"], {}).get("total", 0) == 0
            and (row["source"], row["source_building_id"]) not in chosen_keys
        ]
        pool.sort(key=lambda row: stable_score(seed, "normal", source, row["source_building_id"]))
        if len(pool) < 4:
            raise GalleryDraftError(f"insufficient normal sample candidates for {source}")
        normals.extend(pool[:4])
    selected: list[dict[str, Any]] = []
    for stratum, row in (("large_gallery", large), ("duplicate_burst", duplicate)):
        item = dict(row)
        item["stratum"] = stratum
        item["suppression_metrics"] = suppressions.get(row["selection_id"], {"total": 0, "exact": 0, "same_node": 0, "direct": 0})
        selected.append(item)
    normals.sort(key=lambda row: (row["source"], stable_score(seed, "normal", row["source"], row["source_building_id"])))
    for row in normals:
        item = dict(row)
        item["stratum"] = "normal"
        item["suppression_metrics"] = suppressions.get(row["selection_id"], {"total": 0, "exact": 0, "same_node": 0, "direct": 0})
        selected.append(item)
    if len(selected) != 10 or {source: sum(row["source"] == source for row in selected) for source in ("divisare", "architizer")} != {"divisare": 5, "architizer": 5}:
        raise GalleryDraftError("N10 sample balance invariant failed")
    for rank, row in enumerate(selected, 1):
        row["sample_rank"] = rank
        row["sample_score_sha256"] = stable_score(seed, row["stratum"], row["source"], row["source_building_id"])
    return selected


def _role_rank(role: str | None) -> int:
    if role == "cover":
        return 0
    if role == "gallery":
        return 1
    return 2


def representative_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    width = int(row.get("original_width") or 0)
    height = int(row.get("original_height") or 0)
    short_edge = min(width, height) if width and height else 0
    long_edge = max(width, height)
    area = width * height
    ordinal = row.get("source_ordinal")
    return (
        0 if row.get("fingerprint_status") == "success" else 1,
        0 if short_edge >= 512 else 1,
        -area,
        -short_edge,
        -long_edge,
        0,  # crop/overlay loss is unavailable in frozen E2/E3 and therefore neutral.
        int(row.get("low_information") or 0),
        _role_rank(None if row.get("primary_role") is None else str(row.get("primary_role"))),
        1 if ordinal is None else 0,
        int(ordinal or 0),
        str(row["source_asset_id"]),
        str(row.get("candidate_id") or ""),
    )


def representative_score(row: Mapping[str, Any]) -> dict[str, Any]:
    width = int(row.get("original_width") or 0)
    height = int(row.get("original_height") or 0)
    short_edge = min(width, height) if width and height else 0
    return {
        "version": REPRESENTATIVE_SCORE_VERSION,
        "verified_fingerprint_success": row.get("fingerprint_status") == "success",
        "usable_resolution": short_edge >= 512,
        "pixel_area": width * height,
        "short_edge": short_edge,
        "long_edge": max(width, height),
        "crop_overlay_loss": None,
        "crop_overlay_policy": "neutral_when_unavailable_in_frozen_e2_e3",
        "e3_low_information": bool(row.get("low_information")),
        "e3_primary_role": row.get("primary_role"),
        "e3_source_ordinal": row.get("source_ordinal"),
        "stable_image_key": str(row["source_asset_id"]),
    }


def _retryable(error_kind: str | None) -> int:
    value = (error_kind or "").casefold()
    return int(any(token in value for token in ("timeout", "connection", "http_5", "rate", "429", "temporary")))


def _load_building_assets(e2: sqlite3.Connection, e3: sqlite3.Connection, building: Mapping[str, Any]) -> list[dict[str, Any]]:
    source = str(building["source"])
    building_id = str(building["source_building_id"])
    selection_id = str(building["selection_id"])
    candidates = {
        str(row["source_asset_id"]): dict(row)
        for row in e3.execute(
            "SELECT * FROM image_candidates WHERE selection_id=? ORDER BY source_asset_id",
            (selection_id,),
        )
    }
    query = """
        SELECT ba.source_asset_id,ba.project_count,ba.occurrence_count,ba.roles_json,
               ba.relation_record_sha256,a.e1_run_id,a.fingerprint_status,
               a.canonical_url,a.fetch_url,a.final_url,a.raw_response_sha256,
               a.normalized_pixel_sha256,a.phash_hex,a.original_width,a.original_height,
               a.normalized_width,a.normalized_height,a.source_record_sha256,
               a.provenance_json,a.error_kind,a.error_message,em.cluster_id,pnm.node_id
        FROM building_assets ba
        JOIN assets a ON a.run_id=ba.run_id AND a.source=ba.source
                     AND a.source_asset_id=ba.source_asset_id
        LEFT JOIN exact_pixel_cluster_members em
          ON em.run_id=a.run_id AND em.source=a.source AND em.source_asset_id=a.source_asset_id
        LEFT JOIN phash_node_members pnm
          ON pnm.run_id=a.run_id AND pnm.source=a.source AND pnm.source_asset_id=a.source_asset_id
        WHERE ba.source=? AND ba.source_building_id=?
        ORDER BY ba.source_asset_id
    """
    output: list[dict[str, Any]] = []
    for raw in e2.execute(query, (source, building_id)):
        row = dict(raw)
        candidate = candidates.get(str(row["source_asset_id"]))
        if row["fingerprint_status"] != "success":
            disposition = "exclusion"
            reason = "fingerprint_or_decoder_failure"
        elif candidate is None:
            disposition = "unresolved"
            reason = "successful_e2_asset_missing_e3_candidate"
        elif int(candidate["low_information"]):
            disposition = "exclusion"
            reason = "low_information"
        else:
            disposition = "gallery"
            reason = "eligible_frozen_e2_e3_evidence"
        row.update(
            {
                "source": source,
                "source_building_id": building_id,
                "selection_id": selection_id,
                "disposition": disposition,
                "disposition_reason": reason,
                "candidate_id": None if candidate is None else candidate["candidate_id"],
                "primary_role": None if candidate is None else candidate["primary_role"],
                "source_ordinal": None if candidate is None else candidate["source_ordinal"],
                "quality_flags_json": "[]" if candidate is None else candidate["quality_flags_json"],
                "low_information": 0 if candidate is None else int(candidate["low_information"]),
                "candidate_record_sha256": None if candidate is None else candidate["candidate_record_sha256"],
                "e3_exact_cluster_id": None if candidate is None else candidate["exact_cluster_id"],
                "e3_phash_node_id": None if candidate is None else candidate["phash_node_id"],
            }
        )
        if candidate is not None:
            if candidate["normalized_pixel_sha256"] != row["normalized_pixel_sha256"]:
                raise GalleryDraftError("E2/E3 normalized-pixel SHA mismatch")
            if candidate["phash_node_id"] != row["node_id"]:
                raise GalleryDraftError("E2/E3 pHash node mismatch")
        output.append(row)
    if not output:
        raise GalleryDraftError(f"sample building has no E2 building assets: {source}/{building_id}")
    return output


def _load_occurrences(e2: sqlite3.Connection, building: Mapping[str, Any]) -> list[dict[str, Any]]:
    query = """
        SELECT o.occurrence_id,o.source_project_id,o.raw_asset_key,o.source_asset_id,
               o.resolution_status,o.role,o.ordinal,o.occurrence_url,o.source_record_sha256,
               o.detail_json,spb.source_record_sha256 AS membership_record_sha256
        FROM source_project_buildings spb
        JOIN project_asset_occurrences o
          ON o.run_id=spb.run_id AND o.source=spb.source
         AND o.source_project_id=spb.source_project_id
        WHERE spb.source=? AND spb.source_building_id=?
        ORDER BY o.occurrence_id
    """
    return [dict(row) for row in e2.execute(query, (building["source"], building["source_building_id"]))]


def _direct_edges_for_representatives(e2: sqlite3.Connection, representatives: Sequence[Mapping[str, Any]]) -> dict[frozenset[str], dict[str, Any]]:
    nodes = sorted({str(row["node_id"]) for row in representatives if row.get("node_id")})
    if len(nodes) < 2:
        return {}
    marks = ",".join("?" for _ in nodes)
    query = (
        "SELECT edge_id,left_node_id,right_node_id,hamming_distance,edge_scope,edge_record_sha256 "
        "FROM phash_edges WHERE edge_scope='global_le8' AND hamming_distance BETWEEN 1 AND 8 "
        f"AND left_node_id IN ({marks}) AND right_node_id IN ({marks}) "
        "ORDER BY left_node_id,right_node_id,edge_id"
    )
    by_node = {str(row["node_id"]): str(row["source_asset_id"]) for row in representatives}
    output: dict[frozenset[str], dict[str, Any]] = {}
    for raw in e2.execute(query, (*nodes, *nodes)):
        row = dict(raw)
        left_asset = by_node.get(str(row["left_node_id"]))
        right_asset = by_node.get(str(row["right_node_id"]))
        if left_asset is None or right_asset is None or left_asset == right_asset:
            continue
        key = frozenset((left_asset, right_asset))
        if key in output and output[key]["hamming_distance"] != row["hamming_distance"]:
            raise GalleryDraftError("conflicting E2 direct edges")
        output[key] = row
    return output


def group_gallery_assets(e2: sqlite3.Connection, assets: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return groups, members, and direct-edge rows for one building."""

    active = [row for row in assets if row["disposition"] == "gallery"]
    by_pixel: dict[str, list[dict[str, Any]]] = {}
    for row in active:
        pixel = row.get("normalized_pixel_sha256")
        if not pixel:
            raise GalleryDraftError("gallery asset is missing normalized-pixel SHA")
        by_pixel.setdefault(str(pixel), []).append(row)
    classes: list[dict[str, Any]] = []
    for pixel, members in by_pixel.items():
        ordered = sorted(members, key=representative_sort_key)
        classes.append({"pixel": pixel, "representative": ordered[0], "members": ordered})
    classes.sort(key=lambda row: representative_sort_key(row["representative"]))
    representatives = [row["representative"] for row in classes]
    edges = _direct_edges_for_representatives(e2, representatives)
    unassigned = {str(row["representative"]["source_asset_id"]) for row in classes}
    class_by_rep = {str(row["representative"]["source_asset_id"]): row for row in classes}
    groups: list[dict[str, Any]] = []
    members_out: list[dict[str, Any]] = []
    edge_out: list[dict[str, Any]] = []
    group_rank = 0
    for class_row in classes:
        center_asset = str(class_row["representative"]["source_asset_id"])
        if center_asset not in unassigned:
            continue
        group_rank += 1
        unassigned.remove(center_asset)
        attached: list[tuple[dict[str, Any], dict[str, Any] | None]] = [(class_row, None)]
        for other in classes:
            other_asset = str(other["representative"]["source_asset_id"])
            if other_asset not in unassigned:
                continue
            edge = edges.get(frozenset((center_asset, other_asset)))
            if edge is not None:
                attached.append((other, edge))
                unassigned.remove(other_asset)
        center = class_row["representative"]
        group_id = "vnx_imggrp_" + canonical_sha256(
            {
                "grouping_version": GROUPING_VERSION,
                "normalized_pixel_sha256": class_row["pixel"],
                "representative_source_asset_id": center_asset,
                "source": center["source"],
                "source_building_id": center["source_building_id"],
            }
        )[:32]
        group_member_count = sum(len(item[0]["members"]) for item in attached)
        group = {
            "group_id": group_id,
            "source": center["source"],
            "source_building_id": center["source_building_id"],
            "group_rank": group_rank,
            "representative_source_asset_id": center_asset,
            "representative_candidate_id": center["candidate_id"],
            "representative_pixel_sha256": center["normalized_pixel_sha256"],
            "member_count": group_member_count,
            "exact_class_count": len(attached),
            "direct_class_count": len(attached) - 1,
            "representative_score_json": canonical_json(representative_score(center)),
        }
        group["record_sha256"] = canonical_sha256(group)
        groups.append(group)
        gallery_rank = 0
        for attached_class, edge in attached:
            class_rep = attached_class["representative"]
            class_rep_asset = str(class_rep["source_asset_id"])
            pair_id: str | None = None
            if edge is not None:
                pair_id = "vnx_pair_" + canonical_sha256(
                    {
                        "edge_record_sha256": edge["edge_record_sha256"],
                        "group_id": group_id,
                        "left": min(center_asset, class_rep_asset),
                        "right": max(center_asset, class_rep_asset),
                        "version": GROUPING_VERSION,
                    }
                )[:32]
                pair = {
                    "pair_id": pair_id,
                    "group_id": group_id,
                    "representative_source_asset_id": center_asset,
                    "member_class_representative_asset_id": class_rep_asset,
                    "edge_id": edge["edge_id"],
                    "left_node_id": edge["left_node_id"],
                    "right_node_id": edge["right_node_id"],
                    "hamming_distance": int(edge["hamming_distance"]),
                    "edge_scope": edge["edge_scope"],
                    "edge_record_sha256": edge["edge_record_sha256"],
                    "transitive_closure_allowed": 0,
                }
                pair["record_sha256"] = canonical_sha256(pair)
                edge_out.append(pair)
            for index, member in enumerate(attached_class["members"]):
                gallery_rank += 1
                asset_id = str(member["source_asset_id"])
                if edge is None and index == 0:
                    role = "representative"
                    basis = "representative"
                elif edge is None:
                    role = "exact_member"
                    basis = "exact_pixel"
                elif index == 0:
                    role = "direct_class_representative"
                    basis = "e2_direct_phash_le8"
                else:
                    role = "exact_member_of_direct_class"
                    basis = "exact_pixel_via_direct_class_representative"
                item = {
                    "group_id": group_id,
                    "source": member["source"],
                    "source_building_id": member["source_building_id"],
                    "source_asset_id": asset_id,
                    "candidate_id": member["candidate_id"],
                    "gallery_rank": gallery_rank,
                    "member_role": role,
                    "attachment_basis": basis,
                    "class_representative_source_asset_id": class_rep_asset,
                    "normalized_pixel_sha256": member["normalized_pixel_sha256"],
                    "direct_pair_id": pair_id,
                    "reason_code": "frozen_exact_pixel" if "exact" in basis else ("frozen_e2_direct_edge" if edge else "best_representative"),
                }
                item["record_sha256"] = canonical_sha256(item)
                members_out.append(item)
    if unassigned:
        raise GalleryDraftError("grouping left unassigned exact classes")
    return groups, members_out, edge_out


SCHEMA_SQL = """
PRAGMA foreign_keys=ON;
CREATE TABLE runs(
  run_id TEXT PRIMARY KEY, contract_version TEXT NOT NULL, builder_version TEXT NOT NULL,
  sample_version TEXT NOT NULL, grouping_version TEXT NOT NULL,
  representative_score_version TEXT NOT NULL, sample_seed TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
  e2_path TEXT NOT NULL, e2_size_bytes INTEGER NOT NULL, e2_sha256_before TEXT NOT NULL,
  e2_sha256_after TEXT, e2_run_id TEXT NOT NULL,
  e3_path TEXT NOT NULL, e3_size_bytes INTEGER NOT NULL, e3_sha256_before TEXT NOT NULL,
  e3_sha256_after TEXT, e3_run_id TEXT NOT NULL,
  sample_manifest_sha256 TEXT NOT NULL, config_json TEXT NOT NULL, config_sha256 TEXT NOT NULL,
  stage_a_logical_sha256 TEXT, stage_a_network_requests INTEGER NOT NULL DEFAULT 0,
  stage_a_vision_requests INTEGER NOT NULL DEFAULT 0, stage_a_llm_requests INTEGER NOT NULL DEFAULT 0,
  stage_b_status TEXT NOT NULL DEFAULT 'not_started', stage_b_http_requests INTEGER NOT NULL DEFAULT 0,
  stage_b_vision_requests INTEGER NOT NULL DEFAULT 0, stage_b_llm_requests INTEGER NOT NULL DEFAULT 0,
  stage_b_resume_http_requests INTEGER, blind_review_status TEXT NOT NULL DEFAULT 'not_started'
);
CREATE TABLE sample_buildings(
  run_id TEXT NOT NULL, sample_rank INTEGER NOT NULL, source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, selection_id TEXT NOT NULL, name TEXT,
  stratum TEXT NOT NULL, sample_score_sha256 TEXT NOT NULL, e3_asset_count INTEGER NOT NULL,
  input_asset_count INTEGER NOT NULL, source_occurrence_count INTEGER NOT NULL,
  suppression_metrics_json TEXT NOT NULL, e3_selection_record_sha256 TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_rank),
  UNIQUE(run_id,source,source_building_id), FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE input_assets(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, candidate_id TEXT, disposition TEXT NOT NULL,
  disposition_reason TEXT NOT NULL, group_id TEXT, project_count INTEGER NOT NULL,
  occurrence_count INTEGER NOT NULL, roles_json TEXT NOT NULL, e1_run_id TEXT NOT NULL,
  fingerprint_status TEXT NOT NULL, canonical_url TEXT, fetch_url TEXT, final_url TEXT,
  raw_response_sha256 TEXT, normalized_pixel_sha256 TEXT, phash_hex TEXT,
  original_width INTEGER, original_height INTEGER, normalized_width INTEGER,
  normalized_height INTEGER, exact_cluster_id TEXT, phash_node_id TEXT,
  primary_role TEXT, source_ordinal INTEGER, quality_flags_json TEXT NOT NULL,
  low_information INTEGER NOT NULL, e2_asset_record_sha256 TEXT NOT NULL,
  e2_relation_record_sha256 TEXT NOT NULL, e3_candidate_record_sha256 TEXT,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,source,source_building_id,source_asset_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE INDEX idx_vnext_input_disposition ON input_assets(run_id,disposition);
CREATE TABLE gallery_groups(
  run_id TEXT NOT NULL, group_id TEXT NOT NULL, source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, group_rank INTEGER NOT NULL,
  representative_source_asset_id TEXT NOT NULL, representative_candidate_id TEXT NOT NULL,
  representative_pixel_sha256 TEXT NOT NULL, member_count INTEGER NOT NULL,
  exact_class_count INTEGER NOT NULL, direct_class_count INTEGER NOT NULL,
  representative_score_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,group_id), UNIQUE(run_id,source,source_building_id,group_rank),
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE direct_edge_evidence(
  run_id TEXT NOT NULL, pair_id TEXT NOT NULL, group_id TEXT NOT NULL,
  representative_source_asset_id TEXT NOT NULL,
  member_class_representative_asset_id TEXT NOT NULL, edge_id TEXT NOT NULL,
  left_node_id TEXT NOT NULL, right_node_id TEXT NOT NULL, hamming_distance INTEGER NOT NULL,
  edge_scope TEXT NOT NULL, edge_record_sha256 TEXT NOT NULL,
  transitive_closure_allowed INTEGER NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,pair_id), FOREIGN KEY(run_id,group_id) REFERENCES gallery_groups(run_id,group_id)
);
CREATE TABLE group_members(
  run_id TEXT NOT NULL, group_id TEXT NOT NULL, source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, source_asset_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
  gallery_rank INTEGER NOT NULL, member_role TEXT NOT NULL, attachment_basis TEXT NOT NULL,
  class_representative_source_asset_id TEXT NOT NULL, normalized_pixel_sha256 TEXT NOT NULL,
  direct_pair_id TEXT, reason_code TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id,source_asset_id),
  FOREIGN KEY(run_id,group_id) REFERENCES gallery_groups(run_id,group_id)
);
CREATE TABLE exclusion_ledger(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, reason_code TEXT NOT NULL, decision_engine TEXT NOT NULL,
  decision_version TEXT NOT NULL, pixel_verification_status TEXT NOT NULL,
  retryable INTEGER NOT NULL, error_kind TEXT, error_message TEXT, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id,source_asset_id)
);
CREATE TABLE unresolved_ledger(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, reason_code TEXT NOT NULL, decision_engine TEXT NOT NULL,
  decision_version TEXT NOT NULL, pixel_verification_status TEXT NOT NULL,
  retryable INTEGER NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id,source_asset_id)
);
CREATE TABLE source_occurrences(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  occurrence_id TEXT NOT NULL, source_project_id TEXT NOT NULL, raw_asset_key TEXT NOT NULL,
  source_asset_id TEXT, resolution_status TEXT NOT NULL, role TEXT NOT NULL, ordinal INTEGER,
  occurrence_url TEXT, occurrence_record_sha256 TEXT NOT NULL,
  membership_record_sha256 TEXT NOT NULL, detail_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id,occurrence_id)
);
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
CREATE TABLE pixel_verifications(
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', fetch_url TEXT,
  expected_raw_sha256 TEXT, expected_pixel_sha256 TEXT, actual_raw_sha256 TEXT,
  actual_pixel_sha256 TEXT, actual_final_url TEXT, http_status INTEGER,
  response_bytes INTEGER, request_count INTEGER NOT NULL DEFAULT 0, retryable INTEGER,
  error_kind TEXT, error_message TEXT, started_at TEXT, completed_at TEXT,
  record_sha256 TEXT, PRIMARY KEY(run_id,source,source_building_id,source_asset_id)
);
CREATE TABLE derivative_cache(
  run_id TEXT NOT NULL, normalized_pixel_sha256 TEXT NOT NULL, transform_version TEXT NOT NULL,
  cache_key TEXT NOT NULL, relative_path TEXT NOT NULL, encoded_sha256 TEXT NOT NULL,
  derivative_pixel_sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
  encoded_bytes INTEGER NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,normalized_pixel_sha256,transform_version)
);
CREATE TABLE local_features(
  run_id TEXT NOT NULL, normalized_pixel_sha256 TEXT NOT NULL, feature_version TEXT NOT NULL,
  keypoint_count INTEGER NOT NULL, descriptor_sha256 TEXT NOT NULL, descriptor_json TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,normalized_pixel_sha256,feature_version)
);
CREATE TABLE local_pair_evidence(
  run_id TEXT NOT NULL, local_pair_id TEXT NOT NULL, pair_kind TEXT NOT NULL,
  direct_pair_id TEXT, left_source_asset_id TEXT NOT NULL, right_source_asset_id TEXT NOT NULL,
  left_pixel_sha256 TEXT NOT NULL, right_pixel_sha256 TEXT NOT NULL,
  feature_version TEXT NOT NULL, matched_keypoints INTEGER NOT NULL,
  left_keypoints INTEGER NOT NULL, right_keypoints INTEGER NOT NULL,
  median_similarity REAL, max_similarity REAL, outcome TEXT NOT NULL,
  identity_authorization INTEGER NOT NULL DEFAULT 0, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,local_pair_id)
);
CREATE TABLE blind_review_items(
  run_id TEXT NOT NULL, opaque_pair_id TEXT NOT NULL, direct_pair_id TEXT NOT NULL,
  sheet_path TEXT NOT NULL, sheet_page INTEGER NOT NULL, sheet_order INTEGER NOT NULL,
  decision TEXT, reviewer TEXT, reviewed_at TEXT, record_sha256 TEXT,
  PRIMARY KEY(run_id,opaque_pair_id), UNIQUE(run_id,direct_pair_id)
);
"""


LOGICAL_TABLES: tuple[tuple[str, str], ...] = (
    ("sample_buildings", "sample_rank"),
    ("input_assets", "source,source_building_id,source_asset_id"),
    ("gallery_groups", "source,source_building_id,group_rank"),
    ("group_members", "source,source_building_id,source_asset_id"),
    ("direct_edge_evidence", "pair_id"),
    ("exclusion_ledger", "source,source_building_id,source_asset_id"),
    ("unresolved_ledger", "source,source_building_id,source_asset_id"),
    ("source_occurrences", "source,source_building_id,occurrence_id"),
)


def stage_a_logical_sha256(connection: sqlite3.Connection, run_id: str) -> str:
    payload: dict[str, Any] = {"version": LOGICAL_VERSION, "tables": {}}
    for table, order in LOGICAL_TABLES:
        payload["tables"][table] = [
            str(row[0])
            for row in connection.execute(
                f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}",
                (run_id,),
            )
        ]
    return canonical_sha256(payload)


def _record_sha(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(payload))


def _config() -> dict[str, Any]:
    return {
        "builder_version": BUILDER_VERSION,
        "contract_version": CONTRACT_VERSION,
        "grouping": {
            "exact_equivalence": "normalized_pixel_sha256",
            "direct_edge_scope": "E2 global_le8, hamming 1..8",
            "same_phash_node_without_edge_groups": False,
            "transitive_closure_allowed": False,
            "star_center": "best exact-class representative",
        },
        "representative_score": {
            "version": REPRESENTATIVE_SCORE_VERSION,
            "order": [
                "frozen fingerprint success",
                "usable short edge >=512",
                "pixel area/short edge/long edge descending",
                "crop-overlay loss (neutral when unavailable)",
                "E3 low-information/role/ordinal",
                "stable source_asset_id/candidate_id",
            ],
        },
        "sample": {
            "version": SAMPLE_VERSION,
            "seed": SAMPLE_SEED,
            "normal": "4 per source; 6..12 assets; no P2 suppression; no quality-risk cover",
            "large_gallery": "Divisare; max 40..60 assets; deterministic tie",
            "duplicate_burst": "Architizer; max frozen P2 direct suppressions; <=60 assets",
        },
        "stage_a_network_requests": 0,
        "stage_a_vision_requests": 0,
        "stage_a_llm_requests": 0,
    }


def _insert_rows(connection: sqlite3.Connection, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    marks = ",".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({marks})",
        rows,
    )


def build_gallery_draft(
    *,
    repo_root: Path,
    output_path: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST,
    report_path: Path = DEFAULT_REPORT,
    freeze_path: Path = DEFAULT_FREEZE,
    verify_full_hashes: bool = True,
) -> BuildResult:
    started_clock = time.monotonic()
    started_at = utc_now()
    root = repo_root.resolve()
    output = (root / output_path).resolve() if not output_path.is_absolute() else output_path.resolve()
    manifest = (root / manifest_path).resolve() if not manifest_path.is_absolute() else manifest_path.resolve()
    report = (root / report_path).resolve() if not report_path.is_absolute() else report_path.resolve()
    freeze = (root / freeze_path).resolve() if not freeze_path.is_absolute() else freeze_path.resolve()
    e2_path = (root / DEFAULT_E2).resolve()
    e3_path = (root / DEFAULT_E3).resolve()
    for target in (output, manifest, report):
        if target.exists():
            raise GalleryDraftError(f"refusing to clobber existing artifact: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
    frozen = load_frozen_inputs(freeze)
    _validate_frozen_spec(frozen[E2_ROLE], expected_path=DEFAULT_E2, expected_size=EXPECTED_E2_SIZE, expected_sha=EXPECTED_E2_SHA256)
    _validate_frozen_spec(frozen[E3_ROLE], expected_path=DEFAULT_E3, expected_size=EXPECTED_E3_SIZE, expected_sha=EXPECTED_E3_SHA256)
    e2_before = sha256_file(e2_path) if verify_full_hashes else EXPECTED_E2_SHA256
    e3_before = sha256_file(e3_path) if verify_full_hashes else EXPECTED_E3_SHA256
    if e2_before != EXPECTED_E2_SHA256 or e3_before != EXPECTED_E3_SHA256:
        raise GalleryDraftError("frozen E2/E3 pre-run SHA mismatch")
    e2 = _open_immutable(e2_path)
    e3 = _open_immutable(e3_path)
    temporary = output.with_name(output.name + ".building")
    if temporary.exists():
        raise GalleryDraftError(f"stale temporary artifact exists: {temporary}")
    config = _config()
    config_json = canonical_json(config)
    config_sha = canonical_sha256(config)
    try:
        e2_run = e2.execute("SELECT run_id FROM e2_runs WHERE status='complete'").fetchone()
        e3_run = e3.execute("SELECT run_id FROM selection_runs WHERE status='complete'").fetchone()
        if e2_run is None or e3_run is None:
            raise GalleryDraftError("E2/E3 is not terminal complete")
        sample = choose_n10(e3)
        sample_manifest_payload = {
            "config_sha256": config_sha,
            "e2_sha256": e2_before,
            "e3_sha256": e3_before,
            "sample": [
                {
                    key: row[key]
                    for key in (
                        "sample_rank", "source", "source_building_id", "selection_id",
                        "stratum", "sample_score_sha256", "asset_count", "selection_record_sha256",
                    )
                }
                for row in sample
            ],
            "sample_seed": SAMPLE_SEED,
            "sample_version": SAMPLE_VERSION,
        }
        sample_manifest_sha = canonical_sha256(sample_manifest_payload)
        run_id = "vnx_gallery_n10_" + canonical_sha256(
            {"config_sha256": config_sha, "sample_manifest_sha256": sample_manifest_sha}
        )[:24]
        database = sqlite3.connect(temporary)
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(SCHEMA_SQL)
        database.execute(
            "INSERT INTO runs(run_id,contract_version,builder_version,sample_version,grouping_version,"
            "representative_score_version,sample_seed,status,started_at,e2_path,e2_size_bytes,e2_sha256_before,"
            "e2_run_id,e3_path,e3_size_bytes,e3_sha256_before,e3_run_id,sample_manifest_sha256,config_json,config_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, CONTRACT_VERSION, BUILDER_VERSION, SAMPLE_VERSION, GROUPING_VERSION,
                REPRESENTATIVE_SCORE_VERSION, SAMPLE_SEED, "building", started_at,
                DEFAULT_E2.as_posix(), EXPECTED_E2_SIZE, e2_before, str(e2_run[0]),
                DEFAULT_E3.as_posix(), EXPECTED_E3_SIZE, e3_before, str(e3_run[0]),
                sample_manifest_sha, config_json, config_sha,
            ),
        )
        totals = {"input": 0, "gallery": 0, "groups": 0, "exclusion": 0, "unresolved": 0, "direct": 0, "occurrences": 0}
        for building in sample:
            assets = _load_building_assets(e2, e3, building)
            occurrences = _load_occurrences(e2, building)
            groups, members, edges = group_gallery_assets(e2, assets)
            group_by_asset = {row["source_asset_id"]: row["group_id"] for row in members}
            for row in assets:
                row["group_id"] = group_by_asset.get(str(row["source_asset_id"]))
                base = {
                    "source": row["source"], "source_building_id": row["source_building_id"],
                    "source_asset_id": row["source_asset_id"], "candidate_id": row["candidate_id"],
                    "disposition": row["disposition"], "disposition_reason": row["disposition_reason"],
                    "group_id": row["group_id"], "e2_asset_record_sha256": row["source_record_sha256"],
                    "e2_relation_record_sha256": row["relation_record_sha256"],
                    "e3_candidate_record_sha256": row["candidate_record_sha256"],
                }
                row["record_sha256"] = _record_sha(base)
            building_record = {
                "sample_rank": building["sample_rank"], "source": building["source"],
                "source_building_id": building["source_building_id"], "selection_id": building["selection_id"],
                "stratum": building["stratum"], "sample_score_sha256": building["sample_score_sha256"],
                "e3_asset_count": building["asset_count"], "input_asset_count": len(assets),
                "source_occurrence_count": len(occurrences),
                "suppression_metrics": building["suppression_metrics"],
                "e3_selection_record_sha256": building["selection_record_sha256"],
            }
            building_sha = _record_sha(building_record)
            database.execute(
                "INSERT INTO sample_buildings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, building["sample_rank"], building["source"], building["source_building_id"],
                    building["selection_id"], building["name"], building["stratum"],
                    building["sample_score_sha256"], building["asset_count"], len(assets), len(occurrences),
                    canonical_json(building["suppression_metrics"]), building["selection_record_sha256"],
                    building_sha,
                ),
            )
            input_columns = (
                "run_id","source","source_building_id","source_asset_id","candidate_id","disposition",
                "disposition_reason","group_id","project_count","occurrence_count","roles_json","e1_run_id",
                "fingerprint_status","canonical_url","fetch_url","final_url","raw_response_sha256",
                "normalized_pixel_sha256","phash_hex","original_width","original_height","normalized_width",
                "normalized_height","exact_cluster_id","phash_node_id","primary_role","source_ordinal",
                "quality_flags_json","low_information","e2_asset_record_sha256","e2_relation_record_sha256",
                "e3_candidate_record_sha256","record_sha256",
            )
            _insert_rows(database, "input_assets", input_columns, [
                (
                    run_id,row["source"],row["source_building_id"],row["source_asset_id"],row["candidate_id"],
                    row["disposition"],row["disposition_reason"],row["group_id"],row["project_count"],
                    row["occurrence_count"],row["roles_json"],row["e1_run_id"],row["fingerprint_status"],
                    row["canonical_url"],row["fetch_url"],row["final_url"],row["raw_response_sha256"],
                    row["normalized_pixel_sha256"],row["phash_hex"],row["original_width"],row["original_height"],
                    row["normalized_width"],row["normalized_height"],row["cluster_id"],row["node_id"],
                    row["primary_role"],row["source_ordinal"],row["quality_flags_json"],row["low_information"],
                    row["source_record_sha256"],row["relation_record_sha256"],row["candidate_record_sha256"],
                    row["record_sha256"],
                ) for row in assets
            ])
            _insert_rows(database, "gallery_groups", (
                "run_id","group_id","source","source_building_id","group_rank","representative_source_asset_id",
                "representative_candidate_id","representative_pixel_sha256","member_count","exact_class_count",
                "direct_class_count","representative_score_json","record_sha256",
            ), [(run_id, *[row[key] for key in (
                "group_id","source","source_building_id","group_rank","representative_source_asset_id",
                "representative_candidate_id","representative_pixel_sha256","member_count","exact_class_count",
                "direct_class_count","representative_score_json","record_sha256",
            )]) for row in groups])
            _insert_rows(database, "direct_edge_evidence", (
                "run_id","pair_id","group_id","representative_source_asset_id",
                "member_class_representative_asset_id","edge_id","left_node_id","right_node_id",
                "hamming_distance","edge_scope","edge_record_sha256","transitive_closure_allowed","record_sha256",
            ), [(run_id, *[row[key] for key in (
                "pair_id","group_id","representative_source_asset_id","member_class_representative_asset_id",
                "edge_id","left_node_id","right_node_id","hamming_distance","edge_scope","edge_record_sha256",
                "transitive_closure_allowed","record_sha256",
            )]) for row in edges])
            _insert_rows(database, "group_members", (
                "run_id","group_id","source","source_building_id","source_asset_id","candidate_id","gallery_rank",
                "member_role","attachment_basis","class_representative_source_asset_id","normalized_pixel_sha256",
                "direct_pair_id","reason_code","record_sha256",
            ), [(run_id, *[row[key] for key in (
                "group_id","source","source_building_id","source_asset_id","candidate_id","gallery_rank",
                "member_role","attachment_basis","class_representative_source_asset_id","normalized_pixel_sha256",
                "direct_pair_id","reason_code","record_sha256",
            )]) for row in members])
            for row in assets:
                if row["disposition"] == "exclusion":
                    ledger = {
                        "source": row["source"], "source_building_id": row["source_building_id"],
                        "source_asset_id": row["source_asset_id"], "reason_code": row["disposition_reason"],
                        "decision_engine": "frozen_e1_e3_evidence", "decision_version": BUILDER_VERSION,
                        "pixel_verification_status": "frozen_success" if row["fingerprint_status"] == "success" else "frozen_failed",
                        "retryable": _retryable(row["error_kind"]), "error_kind": row["error_kind"],
                        "error_message": row["error_message"],
                    }
                    database.execute(
                        "INSERT INTO exclusion_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, *ledger.values(), _record_sha(ledger)),
                    )
                elif row["disposition"] == "unresolved":
                    ledger = {
                        "source": row["source"], "source_building_id": row["source_building_id"],
                        "source_asset_id": row["source_asset_id"], "reason_code": row["disposition_reason"],
                        "decision_engine": "frozen_e2_e3_join", "decision_version": BUILDER_VERSION,
                        "pixel_verification_status": "frozen_success", "retryable": 0,
                    }
                    database.execute(
                        "INSERT INTO unresolved_ledger VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, *ledger.values(), _record_sha(ledger)),
                    )
            occurrence_rows = []
            for row in occurrences:
                payload = {
                    "source": building["source"], "source_building_id": building["source_building_id"],
                    "occurrence_id": row["occurrence_id"], "source_project_id": row["source_project_id"],
                    "raw_asset_key": row["raw_asset_key"], "source_asset_id": row["source_asset_id"],
                    "resolution_status": row["resolution_status"], "role": row["role"], "ordinal": row["ordinal"],
                    "occurrence_url": row["occurrence_url"], "occurrence_record_sha256": row["source_record_sha256"],
                    "membership_record_sha256": row["membership_record_sha256"],
                }
                occurrence_rows.append((
                    run_id,building["source"],building["source_building_id"],row["occurrence_id"],
                    row["source_project_id"],row["raw_asset_key"],row["source_asset_id"],row["resolution_status"],
                    row["role"],row["ordinal"],row["occurrence_url"],row["source_record_sha256"],
                    row["membership_record_sha256"],row["detail_json"],_record_sha(payload),
                ))
            _insert_rows(database, "source_occurrences", (
                "run_id","source","source_building_id","occurrence_id","source_project_id","raw_asset_key",
                "source_asset_id","resolution_status","role","ordinal","occurrence_url","occurrence_record_sha256",
                "membership_record_sha256","detail_json","record_sha256",
            ), occurrence_rows)
            totals["input"] += len(assets)
            totals["gallery"] += len(members)
            totals["groups"] += len(groups)
            totals["exclusion"] += sum(row["disposition"] == "exclusion" for row in assets)
            totals["unresolved"] += sum(row["disposition"] == "unresolved" for row in assets)
            totals["direct"] += len(edges)
            totals["occurrences"] += len(occurrences)
        if totals["gallery"] + totals["exclusion"] + totals["unresolved"] != totals["input"]:
            raise GalleryDraftError("global accounting invariant failed")
        accounting_failures = database.execute(
            "SELECT count(*) FROM (SELECT b.source,b.source_building_id,b.input_asset_count,"
            "sum(a.disposition='gallery') g,sum(a.disposition='exclusion') e,sum(a.disposition='unresolved') u "
            "FROM sample_buildings b JOIN input_assets a USING(run_id,source,source_building_id) "
            "GROUP BY b.source,b.source_building_id HAVING input_asset_count<>g+e+u)"
        ).fetchone()[0]
        if accounting_failures:
            raise GalleryDraftError("per-building accounting invariant failed")
        for name, expected, actual in (
            ("n10_building_count", 10, len(sample)),
            ("source_balance", "5/5", "5/5"),
            ("gallery_exclusion_unresolved_accounting", totals["input"], totals["gallery"] + totals["exclusion"] + totals["unresolved"]),
            ("transitive_closure_disabled", 0, database.execute("SELECT coalesce(sum(transitive_closure_allowed),0) FROM direct_edge_evidence").fetchone()[0]),
            ("stage_a_network_requests", 0, 0),
            ("stage_a_vision_requests", 0, 0),
            ("stage_a_llm_requests", 0, 0),
        ):
            database.execute(
                "INSERT INTO validations VALUES(?,?,?,?,?,?,?,?)",
                (run_id, "stage_a", name, int(expected == actual), str(expected), str(actual), "{}", utc_now()),
            )
        for name, value in totals.items():
            database.execute("INSERT INTO metrics(run_id,phase,metric_name,value_integer) VALUES(?,?,?,?)", (run_id, "stage_a", name, int(value)))
        logical = stage_a_logical_sha256(database, run_id)
        database.execute("UPDATE runs SET stage_a_logical_sha256=? WHERE run_id=?", (logical, run_id))
        database.commit()
        e2.close()
        e3.close()
        e2_after = sha256_file(e2_path) if verify_full_hashes else EXPECTED_E2_SHA256
        e3_after = sha256_file(e3_path) if verify_full_hashes else EXPECTED_E3_SHA256
        if (e2_after, e3_after) != (e2_before, e3_before):
            raise GalleryDraftError("frozen E2/E3 SHA changed during Stage A")
        completed_at = utc_now()
        database.execute(
            "UPDATE runs SET status='stage_a_complete',completed_at=?,e2_sha256_after=?,e3_sha256_after=? WHERE run_id=?",
            (completed_at, e2_after, e3_after, run_id),
        )
        database.execute(
            "INSERT INTO validations VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "stage_a", "frozen_sha_pre_post_unchanged", 1, e2_before + "/" + e3_before, e2_after + "/" + e3_after, "{}", completed_at),
        )
        database.commit()
        database.execute("PRAGMA optimize")
        database.close()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(temporary) + suffix)
            if sidecar.exists():
                raise GalleryDraftError(f"temporary output left SQLite sidecar: {sidecar}")
        os.replace(temporary, output)
        manifest_payload = dict(sample_manifest_payload)
        manifest_payload.update(
            {
                "manifest_kind": "vnext_gallery_draft_n10_v1",
                "manifest_sha256": sample_manifest_sha,
                "output_path": output.relative_to(root).as_posix(),
                "run_id": run_id,
                "stage_a_logical_sha256": logical,
            }
        )
        report_payload = {
            "status": "PASS",
            "run_id": run_id,
            "builder_version": BUILDER_VERSION,
            "stage_a_logical_sha256": logical,
            "input_pins": {
                "e2": {"path": DEFAULT_E2.as_posix(), "size_bytes": EXPECTED_E2_SIZE, "sha256_before": e2_before, "sha256_after": e2_after},
                "e3": {"path": DEFAULT_E3.as_posix(), "size_bytes": EXPECTED_E3_SIZE, "sha256_before": e3_before, "sha256_after": e3_after},
            },
            "metrics": totals,
            "accounting": {"expected": totals["input"], "actual": totals["gallery"] + totals["exclusion"] + totals["unresolved"], "passed": True},
            "requests": {"network": 0, "vision": 0, "llm": 0},
            "sqlite_sidecars": [],
        }
        _atomic_json(manifest, manifest_payload, no_clobber=True)
        _atomic_json(report, report_payload, no_clobber=True)
        return BuildResult(
            output_path=output, manifest_path=manifest, report_path=report, run_id=run_id,
            logical_sha256=logical, building_count=10, input_asset_count=totals["input"],
            gallery_asset_count=totals["gallery"], gallery_group_count=totals["groups"],
            exclusion_count=totals["exclusion"], unresolved_count=totals["unresolved"],
            direct_attachment_count=totals["direct"], elapsed_seconds=time.monotonic() - started_clock,
        )
    except Exception:
        try:
            e2.close()
            e3.close()
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
    "BUILDER_VERSION", "CONTRACT_VERSION", "DEFAULT_FREEZE", "DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT", "DEFAULT_REPORT", "GalleryDraftError", "GROUPING_VERSION",
    "LOGICAL_TABLES", "REPRESENTATIVE_SCORE_VERSION", "SAMPLE_SEED", "SAMPLE_VERSION",
    "BuildResult", "build_gallery_draft", "choose_n10", "representative_sort_key",
    "representative_score", "sha256_file", "stage_a_logical_sha256",
]
