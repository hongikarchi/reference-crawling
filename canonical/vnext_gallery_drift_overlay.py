"""WP1.1 offline drift overlay for the immutable WP1 v1 gallery artifact.

The overlay never fetches an image and never mutates the v1 database.  It
replays v1 verified-pixel evidence, splits direct edges whose endpoints are not
both exact pixel matches, pins the existing derivative cache, and renders a
blind sheet containing only admissible pairs.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from canonical.cross_source_image_selection import canonical_json, canonical_sha256


OVERLAY_VERSION = "vnext-gallery-drift-overlay-n10-v1.1"
GROUPING_VERSION = "vnext-exact-class-verified-direct-star-v1.1"
BLIND_REVIEW_VERSION = "vnext-gallery-admissible-blind-review-v1.1"
LOGICAL_VERSION = "vnext-gallery-drift-overlay-logical-v1.1"
APPLICATION_ID = 0x564E5832
SCHEMA_VERSION = 1

DEFAULT_BASE = Path("data/enrichment/vnext_gallery_draft_n10_v1.db")
DEFAULT_OUTPUT = Path("data/enrichment/vnext_gallery_draft_n10_v1_1.db")
DEFAULT_CONTRACT = Path("docs/vnext/CONTRACT_GALLERY_SEMANTICS.md")
DEFAULT_REVIEW_PAGE = Path("data/reports/vnext_gallery_blind_review_n10_v1_1_page_01.png")
DEFAULT_REVIEW_FORM = Path("data/reports/vnext_gallery_blind_review_n10_v1_1_review.json")
DEFAULT_REPORT_JSON = Path("data/reports/vnext_gallery_draft_n10_v1_1_report.json")
DEFAULT_REPORT_MD = Path("data/reports/vnext_gallery_draft_n10_v1_1_report.md")
DEFAULT_MANIFEST = Path("data/reports/vnext_gallery_draft_n10_v1_1_manifest.json")

EXPECTED_BASE_SHA256 = "d63b7a5a279f71d0b9c68b6ca66c6123e934dd67fa5c085a9f8ea16736717cc7"
EXPECTED_CONTRACT_SHA256 = "49133125c1755b050a6276aa6c5ff7a0916ad9d63d9da7793cf181c8db97b8a3"


class DriftOverlayError(RuntimeError):
    """Raised when a WP1.1 input, accounting rule, or no-clobber gate fails."""


@dataclass(frozen=True)
class OverlayResult:
    artifact_path: Path
    run_id: str
    status: str
    base_group_count: int
    effective_group_count: int
    exact_match_count: int
    pixel_unverified_count: int
    admissible_pair_count: int
    split_pair_count: int
    review_page_path: Path
    review_form_path: Path


SCHEMA_SQL = """
PRAGMA foreign_keys=ON;
CREATE TABLE overlay_runs(
  overlay_run_id TEXT PRIMARY KEY, overlay_version TEXT NOT NULL,
  grouping_version TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, completed_at TEXT,
  base_path TEXT NOT NULL, base_size_bytes INTEGER NOT NULL,
  base_sha256_before TEXT NOT NULL, base_sha256_after TEXT NOT NULL,
  base_run_id TEXT NOT NULL, base_logical_sha256 TEXT NOT NULL,
  contract_path TEXT NOT NULL, contract_sha256 TEXT NOT NULL,
  cache_file_count INTEGER NOT NULL, cache_bytes INTEGER NOT NULL,
  cache_inventory_sha256 TEXT NOT NULL,
  source_asset_count INTEGER NOT NULL, base_group_count INTEGER NOT NULL,
  effective_group_count INTEGER NOT NULL, group_delta INTEGER NOT NULL,
  exact_match_count INTEGER NOT NULL, pixel_unverified_count INTEGER NOT NULL,
  serveable_group_count INTEGER NOT NULL, unresolved_group_count INTEGER NOT NULL,
  direct_edge_count INTEGER NOT NULL, admissible_edge_count INTEGER NOT NULL,
  split_edge_count INTEGER NOT NULL,
  http_requests INTEGER NOT NULL, vision_requests INTEGER NOT NULL,
  llm_requests INTEGER NOT NULL, neon_writes INTEGER NOT NULL, r2_writes INTEGER NOT NULL,
  blind_review_status TEXT NOT NULL, review_form_sha256 TEXT,
  logical_sha256 TEXT NOT NULL
);
CREATE TABLE asset_overlay(
  overlay_run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
  base_group_id TEXT NOT NULL, base_group_member_sha256 TEXT NOT NULL,
  frozen_pixel_sha256 TEXT NOT NULL, v1_pixel_status TEXT NOT NULL,
  overlay_pixel_status TEXT NOT NULL, pixel_unverified INTEGER NOT NULL,
  representative_eligible INTEGER NOT NULL, serving_eligible INTEGER NOT NULL,
  effective_group_id TEXT NOT NULL, effective_group_status TEXT NOT NULL,
  reason_code TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(overlay_run_id,source,source_building_id,source_asset_id),
  FOREIGN KEY(overlay_run_id) REFERENCES overlay_runs(overlay_run_id)
);
CREATE TABLE effective_groups(
  overlay_run_id TEXT NOT NULL, effective_group_id TEXT NOT NULL,
  base_group_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  base_group_rank INTEGER NOT NULL, group_rank INTEGER NOT NULL,
  group_status TEXT NOT NULL, representative_source_asset_id TEXT,
  representative_candidate_id TEXT, representative_pixel_sha256 TEXT,
  member_count INTEGER NOT NULL, exact_class_count INTEGER NOT NULL,
  direct_class_count INTEGER NOT NULL, pixel_unverified_count INTEGER NOT NULL,
  change_kind TEXT NOT NULL, split_pair_id TEXT, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(overlay_run_id,effective_group_id),
  UNIQUE(overlay_run_id,source,source_building_id,group_rank),
  FOREIGN KEY(overlay_run_id) REFERENCES overlay_runs(overlay_run_id)
);
CREATE TABLE effective_group_members(
  overlay_run_id TEXT NOT NULL, effective_group_id TEXT NOT NULL,
  source TEXT NOT NULL, source_building_id TEXT NOT NULL, source_asset_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL, member_rank INTEGER NOT NULL,
  frozen_pixel_sha256 TEXT NOT NULL, class_representative_source_asset_id TEXT NOT NULL,
  member_role TEXT NOT NULL, attachment_basis TEXT NOT NULL, direct_pair_id TEXT,
  overlay_pixel_status TEXT NOT NULL, pixel_unverified INTEGER NOT NULL,
  base_member_record_sha256 TEXT NOT NULL, reason_code TEXT NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(overlay_run_id,source,source_building_id,source_asset_id),
  FOREIGN KEY(overlay_run_id,effective_group_id)
    REFERENCES effective_groups(overlay_run_id,effective_group_id)
);
CREATE TABLE edge_overlay(
  overlay_run_id TEXT NOT NULL, pair_id TEXT NOT NULL, base_group_id TEXT NOT NULL,
  center_source_asset_id TEXT NOT NULL, member_source_asset_id TEXT NOT NULL,
  center_pixel_status TEXT NOT NULL, member_pixel_status TEXT NOT NULL,
  disposition TEXT NOT NULL, reason_code TEXT NOT NULL,
  center_effective_group_id TEXT NOT NULL, member_effective_group_id TEXT NOT NULL,
  edge_id TEXT NOT NULL, hamming_distance INTEGER NOT NULL, edge_scope TEXT NOT NULL,
  edge_record_sha256 TEXT NOT NULL, base_edge_row_sha256 TEXT NOT NULL,
  local_outcome TEXT NOT NULL, local_evidence_sha256 TEXT NOT NULL,
  transitive_closure_allowed INTEGER NOT NULL, evidence_preserved INTEGER NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(overlay_run_id,pair_id),
  FOREIGN KEY(overlay_run_id) REFERENCES overlay_runs(overlay_run_id)
);
CREATE TABLE drift_assets(
  overlay_run_id TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, fetch_host TEXT NOT NULL, path_pattern TEXT NOT NULL,
  query_keys TEXT NOT NULL, expected_pixel_sha256 TEXT NOT NULL,
  actual_pixel_sha256 TEXT, actual_raw_sha256 TEXT, http_status INTEGER,
  v1_verification_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(overlay_run_id,source,source_building_id,source_asset_id),
  FOREIGN KEY(overlay_run_id) REFERENCES overlay_runs(overlay_run_id)
);
CREATE TABLE drift_source_summary(
  overlay_run_id TEXT NOT NULL, source TEXT NOT NULL, asset_count INTEGER NOT NULL,
  exact_match_count INTEGER NOT NULL, pixel_unverified_count INTEGER NOT NULL,
  drift_rate REAL NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(overlay_run_id,source),
  FOREIGN KEY(overlay_run_id) REFERENCES overlay_runs(overlay_run_id)
);
CREATE TABLE drift_url_patterns(
  overlay_run_id TEXT NOT NULL, source TEXT NOT NULL, fetch_host TEXT NOT NULL,
  path_pattern TEXT NOT NULL, query_keys TEXT NOT NULL, drift_count INTEGER NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(overlay_run_id,source,fetch_host,path_pattern,query_keys),
  FOREIGN KEY(overlay_run_id) REFERENCES overlay_runs(overlay_run_id)
);
CREATE TABLE cache_pins(
  overlay_run_id TEXT NOT NULL, normalized_pixel_sha256 TEXT NOT NULL,
  transform_version TEXT NOT NULL, relative_path TEXT NOT NULL,
  encoded_sha256 TEXT NOT NULL, encoded_bytes INTEGER NOT NULL,
  base_cache_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(overlay_run_id,normalized_pixel_sha256,transform_version),
  FOREIGN KEY(overlay_run_id) REFERENCES overlay_runs(overlay_run_id)
);
CREATE TABLE blind_review_items(
  overlay_run_id TEXT NOT NULL, opaque_pair_id TEXT NOT NULL, direct_pair_id TEXT NOT NULL,
  sheet_path TEXT NOT NULL, sheet_sha256 TEXT NOT NULL, sheet_order INTEGER NOT NULL,
  left_pixel_sha256 TEXT NOT NULL, right_pixel_sha256 TEXT NOT NULL,
  left_cache_sha256 TEXT NOT NULL, right_cache_sha256 TEXT NOT NULL,
  decision TEXT, reviewer TEXT, reviewed_at TEXT, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(overlay_run_id,opaque_pair_id), UNIQUE(overlay_run_id,direct_pair_id),
  FOREIGN KEY(overlay_run_id,direct_pair_id) REFERENCES edge_overlay(overlay_run_id,pair_id)
);
"""


LOGICAL_TABLES: tuple[tuple[str, str], ...] = (
    ("asset_overlay", "source,source_building_id,source_asset_id"),
    ("effective_groups", "source,source_building_id,group_rank"),
    ("effective_group_members", "source,source_building_id,source_asset_id"),
    ("edge_overlay", "pair_id"),
    ("drift_assets", "source,source_building_id,source_asset_id"),
    ("drift_source_summary", "source"),
    ("drift_url_patterns", "source,fetch_host,path_pattern,query_keys"),
    ("cache_pins", "normalized_pixel_sha256,transform_version"),
    ("blind_review_items", "opaque_pair_id"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _assert_no_sidecars(path: Path) -> None:
    sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    present = [str(item) for item in sidecars if item.exists()]
    if present:
        raise DriftOverlayError("forbidden SQLite sidecar(s): " + ", ".join(present))


def _atomic_bytes(path: Path, raw: bytes, *, no_clobber: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if no_clobber and path.exists():
        raise DriftOverlayError(f"refusing to clobber artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise DriftOverlayError(f"stale temporary artifact: {temporary}")
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


def _atomic_json(path: Path, payload: Mapping[str, Any], *, no_clobber: bool = True) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        no_clobber=no_clobber,
    )


def _record(row: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(row))


def normalize_pixel_status(row: Mapping[str, Any]) -> str:
    """Map v1 transport statuses to the WP1.1 semantic pixel status."""

    status = str(row.get("status", row.get("v1_pixel_status")))
    expected = row.get("expected_pixel_sha256")
    actual = row.get("actual_pixel_sha256")
    if status in {"exact", "delivery_changed"} and expected and expected == actual:
        return "exact_match"
    return "pixel_unverified"


def _url_pattern(url: str | None) -> tuple[str, str, str]:
    parsed = urlsplit(url or "")
    path = parsed.path
    if path.startswith("/media/mediadata/uploads/"):
        pattern = "/media/mediadata/uploads/{file}"
    elif path.startswith("/media/"):
        pattern = "/media/{file}"
    else:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        pattern = (parent + "/{file}") or "/{file}"
    return parsed.netloc.casefold(), pattern, ",".join(sorted(parse_qs(parsed.query).keys()))


def _insert_dicts(connection: sqlite3.Connection, table: str, rows: Iterable[Mapping[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    columns = list(rows[0])
    marks = ",".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({marks})",
        ([row[column] for column in columns] for row in rows),
    )


def _logical_sha(connection: sqlite3.Connection, run_id: str) -> str:
    payload: dict[str, Any] = {"version": LOGICAL_VERSION, "tables": {}}
    for table, order in LOGICAL_TABLES:
        payload["tables"][table] = [
            str(row[0])
            for row in connection.execute(
                f"SELECT record_sha256 FROM {table} WHERE overlay_run_id=? ORDER BY {order}",
                (run_id,),
            )
        ]
    return canonical_sha256(payload)


def _fit_image(raw: bytes, width: int, height: int):
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    return canvas


def _render_review_page(
    root: Path,
    path: Path,
    ordered_edges: Sequence[Mapping[str, Any]],
    cache_by_pixel: Mapping[str, Mapping[str, Any]],
) -> str:
    from PIL import Image, ImageDraw

    canvas = Image.new("RGB", (1500, 80 + 350 * len(ordered_edges)), (245, 245, 245))
    draw = ImageDraw.Draw(canvas)
    draw.text((30, 24), "Blind duplicate review — admissible verified pixels only", fill="black")
    for offset, edge in enumerate(ordered_edges):
        opaque = f"B{offset + 1:03d}"
        y = 70 + offset * 350
        draw.text((30, y + 8), opaque, fill="black")
        for side, pixel, x in (
            ("L", str(edge["center_pixel_sha256"]), 90),
            ("R", str(edge["member_pixel_sha256"]), 795),
        ):
            cache = cache_by_pixel[pixel]
            fitted = _fit_image((root / str(cache["relative_path"])).read_bytes(), 650, 300)
            canvas.paste(fitted, (x, y + 35))
            draw.text((x + 5, y + 40), side, fill="black")
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=False)
    _atomic_bytes(root / path, output.getvalue())
    return hashlib.sha256(output.getvalue()).hexdigest()


def _load_base(base: sqlite3.Connection) -> tuple[sqlite3.Row, list[dict[str, Any]], list[dict[str, Any]]]:
    runs = base.execute("SELECT * FROM runs").fetchall()
    if len(runs) != 1 or runs[0]["stage_b_status"] != "complete":
        raise DriftOverlayError("v1 must contain one terminal Stage B run")
    run = runs[0]
    members = [
        dict(row)
        for row in base.execute(
            "SELECT m.*,g.group_rank,g.representative_source_asset_id AS base_representative_asset_id,"
            "p.status AS v1_pixel_status,p.expected_pixel_sha256,p.actual_pixel_sha256,"
            "p.actual_raw_sha256,p.http_status,p.fetch_url,p.record_sha256 AS verification_record_sha256 "
            "FROM group_members m JOIN gallery_groups g USING(run_id,group_id) "
            "JOIN pixel_verifications p USING(run_id,source,source_building_id,source_asset_id) "
            "WHERE m.run_id=? ORDER BY m.source,m.source_building_id,g.group_rank,m.gallery_rank,m.source_asset_id",
            (run["run_id"],),
        )
    ]
    edges = [dict(row) for row in base.execute("SELECT * FROM direct_edge_evidence WHERE run_id=? ORDER BY pair_id", (run["run_id"],))]
    return run, members, edges


def _recompute(
    members: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_asset = {str(row["source_asset_id"]): row for row in members}
    pixel_status = {asset: normalize_pixel_status(row) for asset, row in by_asset.items()}
    edge_states: dict[str, dict[str, Any]] = {}
    for edge in edges:
        center = str(edge["representative_source_asset_id"])
        member = str(edge["member_class_representative_asset_id"])
        admissible = pixel_status[center] == pixel_status[member] == "exact_match"
        edge_states[str(edge["pair_id"])] = {
            **dict(edge),
            "center_status": pixel_status[center],
            "member_status": pixel_status[member],
            "disposition": "admissible" if admissible else "split",
            "reason_code": "both_endpoints_exact_match" if admissible else "unverifiable_edge_source_changed",
        }

    groups_out: list[dict[str, Any]] = []
    members_out: list[dict[str, Any]] = []
    asset_out: list[dict[str, Any]] = []
    edge_group_map: dict[str, tuple[str, str]] = {}
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in members:
        grouped[(str(row["source"]), str(row["source_building_id"]), str(row["group_id"]))].append(row)

    partitions_by_building: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (source, building, base_group_id), group_members in grouped.items():
        ordered_members = sorted(group_members, key=lambda row: (int(row["gallery_rank"]), str(row["source_asset_id"])))
        base_rank = int(ordered_members[0]["group_rank"])
        base_rep = str(ordered_members[0]["base_representative_asset_id"])
        center_pixel = str(by_asset[base_rep]["normalized_pixel_sha256"])
        classes: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in ordered_members:
            classes[str(row["normalized_pixel_sha256"])].append(row)
        attached: dict[str, Mapping[str, Any]] = {}
        for row in ordered_members:
            pair = row.get("direct_pair_id")
            if pair:
                attached[str(row["normalized_pixel_sha256"])] = edge_states[str(pair)]
        unknown = set(classes) - {center_pixel} - set(attached)
        if unknown:
            raise DriftOverlayError(f"v1 group is not a direct-edge star: {base_group_id}")
        main_pixels = [center_pixel] + sorted(
            (pixel for pixel, edge in attached.items() if edge["disposition"] == "admissible"),
            key=lambda pixel: str(attached[pixel]["pair_id"]),
        )
        partitions = [{"pixels": main_pixels, "split_pair_id": None, "group_id": base_group_id}]
        for pixel, edge in sorted(attached.items(), key=lambda item: str(item[1]["pair_id"])):
            if edge["disposition"] != "split":
                continue
            new_id = "vnx_imggrp_v11_" + canonical_sha256(
                {"base_group_id": base_group_id, "pair_id": edge["pair_id"], "pixel": pixel, "version": GROUPING_VERSION}
            )[:32]
            partitions.append({"pixels": [pixel], "split_pair_id": str(edge["pair_id"]), "group_id": new_id})
        for index, partition in enumerate(partitions):
            partition.update(
                {
                    "source": source,
                    "source_building_id": building,
                    "base_group_id": base_group_id,
                    "base_group_rank": base_rank,
                    "partition_order": index,
                    "classes": classes,
                    "attached": attached,
                }
            )
            partitions_by_building[(source, building)].append(partition)

    for building_key, partitions in sorted(partitions_by_building.items()):
        partitions.sort(key=lambda row: (row["base_group_rank"], row["partition_order"], row["group_id"]))
        for group_rank, partition in enumerate(partitions, 1):
            selected_members = [row for pixel in partition["pixels"] for row in partition["classes"][pixel]]
            selected_members.sort(key=lambda row: (int(row["gallery_rank"]), str(row["source_asset_id"])))
            eligible = [row for row in selected_members if pixel_status[str(row["source_asset_id"])] == "exact_match"]
            representative = eligible[0] if eligible else None
            status = "serveable" if representative else "unresolved"
            split_pair = partition["split_pair_id"]
            split_count = sum(
                edge["disposition"] == "split" and str(edge["group_id"]) == partition["base_group_id"]
                for edge in edge_states.values()
            )
            admissible_pixels = [
                pixel for pixel in partition["pixels"]
                if pixel != partition["pixels"][0]
                and pixel in partition["attached"]
                and partition["attached"][pixel]["disposition"] == "admissible"
            ]
            if split_pair:
                change_kind = "split_unverifiable_edge_source_changed"
            elif split_count:
                change_kind = "direct_edges_split"
            elif admissible_pixels:
                change_kind = "admissible_direct_edges_retained"
            else:
                change_kind = "unchanged_exact_class"
            group_semantic = {
                "effective_group_id": partition["group_id"],
                "base_group_id": partition["base_group_id"],
                "source": building_key[0],
                "source_building_id": building_key[1],
                "base_group_rank": partition["base_group_rank"],
                "group_rank": group_rank,
                "group_status": status,
                "representative_source_asset_id": None if representative is None else representative["source_asset_id"],
                "representative_candidate_id": None if representative is None else representative["candidate_id"],
                "representative_pixel_sha256": None if representative is None else representative["normalized_pixel_sha256"],
                "member_count": len(selected_members),
                "exact_class_count": len(partition["pixels"]),
                "direct_class_count": len(admissible_pixels),
                "pixel_unverified_count": sum(pixel_status[str(row["source_asset_id"])] != "exact_match" for row in selected_members),
                "change_kind": change_kind,
                "split_pair_id": split_pair,
            }
            groups_out.append({**group_semantic, "record_sha256": _record(group_semantic)})
            for member_rank, row in enumerate(selected_members, 1):
                asset = str(row["source_asset_id"])
                member_pixel = str(row["normalized_pixel_sha256"])
                direct_pair = row.get("direct_pair_id")
                unverified = int(pixel_status[asset] != "exact_match")
                if representative is None:
                    member_role, basis, reason = "unresolved_member", "frozen_exact_pixel", "no_exact_match_member"
                elif asset == str(representative["source_asset_id"]):
                    member_role, basis, reason = "representative", "verified_exact_pixel", "best_exact_match_representative"
                elif member_pixel == str(representative["normalized_pixel_sha256"]):
                    member_role, basis, reason = "exact_member", "frozen_exact_pixel", "frozen_exact_pixel"
                else:
                    member_role, basis, reason = "direct_class_member", "verified_direct_edge", "both_endpoints_exact_match"
                member_semantic = {
                    "effective_group_id": partition["group_id"],
                    "source": row["source"], "source_building_id": row["source_building_id"],
                    "source_asset_id": asset, "candidate_id": row["candidate_id"],
                    "member_rank": member_rank, "frozen_pixel_sha256": member_pixel,
                    "class_representative_source_asset_id": row["class_representative_source_asset_id"],
                    "member_role": member_role, "attachment_basis": basis,
                    "direct_pair_id": direct_pair, "overlay_pixel_status": pixel_status[asset],
                    "pixel_unverified": unverified, "base_member_record_sha256": row["record_sha256"],
                    "reason_code": reason,
                }
                members_out.append({**member_semantic, "record_sha256": _record(member_semantic)})
                asset_semantic = {
                    "source": row["source"], "source_building_id": row["source_building_id"],
                    "source_asset_id": asset, "candidate_id": row["candidate_id"],
                    "base_group_id": row["group_id"], "base_group_member_sha256": row["record_sha256"],
                    "frozen_pixel_sha256": member_pixel, "v1_pixel_status": row["v1_pixel_status"],
                    "overlay_pixel_status": pixel_status[asset], "pixel_unverified": unverified,
                    "representative_eligible": 1 - unverified, "serving_eligible": 1 - unverified,
                    "effective_group_id": partition["group_id"], "effective_group_status": status,
                    "reason_code": "pixel_unverified" if unverified else "exact_match",
                }
                asset_out.append({**asset_semantic, "record_sha256": _record(asset_semantic)})
            for pixel in partition["pixels"]:
                edge = partition["attached"].get(pixel)
                if edge:
                    center_group = partition["base_group_id"]
                    member_group = partition["group_id"] if edge["disposition"] == "split" else center_group
                    edge_group_map[str(edge["pair_id"])] = (center_group, member_group)

    edge_out: list[dict[str, Any]] = []
    for pair_id, edge in sorted(edge_states.items()):
        center = str(edge["representative_source_asset_id"])
        member = str(edge["member_class_representative_asset_id"])
        center_group, member_group = edge_group_map[pair_id]
        semantic = {
            "pair_id": pair_id, "base_group_id": edge["group_id"],
            "center_source_asset_id": center, "member_source_asset_id": member,
            "center_pixel_status": edge["center_status"], "member_pixel_status": edge["member_status"],
            "disposition": edge["disposition"], "reason_code": edge["reason_code"],
            "center_effective_group_id": center_group, "member_effective_group_id": member_group,
            "edge_id": edge["edge_id"], "hamming_distance": edge["hamming_distance"],
            "edge_scope": edge["edge_scope"], "edge_record_sha256": edge["edge_record_sha256"],
            "base_edge_row_sha256": edge["record_sha256"],
            "transitive_closure_allowed": edge["transitive_closure_allowed"],
            "evidence_preserved": 1,
        }
        edge_out.append(semantic)
    return groups_out, members_out, asset_out, edge_out


def _base_local_evidence(base: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["direct_pair_id"]): dict(row)
        for row in base.execute(
            "SELECT * FROM local_pair_evidence WHERE run_id=? AND pair_kind='e2_direct' ORDER BY direct_pair_id",
            (run_id,),
        )
    }


def build_overlay(
    *,
    repo_root: Path,
    base_path: Path = DEFAULT_BASE,
    output_path: Path = DEFAULT_OUTPUT,
    contract_path: Path = DEFAULT_CONTRACT,
) -> OverlayResult:
    root = repo_root.resolve()
    base_file = (root / base_path).resolve()
    output_file = (root / output_path).resolve()
    contract_file = (root / contract_path).resolve()
    for required in (base_file, contract_file):
        if not required.is_file():
            raise DriftOverlayError(f"required input is missing: {required}")
    for target in (output_file, root / DEFAULT_REVIEW_PAGE, root / DEFAULT_REVIEW_FORM, root / DEFAULT_REPORT_JSON, root / DEFAULT_REPORT_MD, root / DEFAULT_MANIFEST):
        if target.exists():
            raise DriftOverlayError(f"refusing to clobber existing WP1.1 artifact: {target}")
    _assert_no_sidecars(base_file)
    base_before = sha256_file(base_file)
    if base_before != EXPECTED_BASE_SHA256:
        raise DriftOverlayError("v1 SHA does not match the accepted fail-closed artifact")
    contract_sha = sha256_file(contract_file)
    if contract_sha != EXPECTED_CONTRACT_SHA256:
        raise DriftOverlayError("gallery contract SHA does not match the WP1.1 revision")

    base = _open_immutable(base_file)
    try:
        base_run, members, base_edges = _load_base(base)
        base_run_id = str(base_run["run_id"])
        groups, effective_members, assets, edge_semantics = _recompute(members, base_edges)
        local = _base_local_evidence(base, base_run_id)
        if set(local) != {str(row["pair_id"]) for row in base_edges}:
            raise DriftOverlayError("v1 local evidence does not cover every direct edge")
        for edge in edge_semantics:
            evidence = local[str(edge["pair_id"])]
            edge["local_outcome"] = evidence["outcome"]
            edge["local_evidence_sha256"] = evidence["record_sha256"]
            edge["record_sha256"] = _record(edge)

        cache_rows = [dict(row) for row in base.execute("SELECT * FROM derivative_cache WHERE run_id=? ORDER BY normalized_pixel_sha256,transform_version", (base_run_id,))]
        cache_pins: list[dict[str, Any]] = []
        cache_by_pixel: dict[str, dict[str, Any]] = {}
        for row in cache_rows:
            cache_path = root / str(row["relative_path"])
            if not cache_path.is_file() or cache_path.stat().st_size != int(row["encoded_bytes"]):
                raise DriftOverlayError(f"v1 derivative cache is missing or size-mismatched: {cache_path}")
            if sha256_file(cache_path) != str(row["encoded_sha256"]):
                raise DriftOverlayError(f"v1 derivative cache SHA mismatch: {cache_path}")
            semantic = {
                "normalized_pixel_sha256": row["normalized_pixel_sha256"],
                "transform_version": row["transform_version"], "relative_path": row["relative_path"],
                "encoded_sha256": row["encoded_sha256"], "encoded_bytes": row["encoded_bytes"],
                "base_cache_record_sha256": row["record_sha256"],
            }
            pin = {**semantic, "record_sha256": _record(semantic)}
            cache_pins.append(pin)
            cache_by_pixel[str(row["normalized_pixel_sha256"])] = pin

        member_by_asset = {str(row["source_asset_id"]): row for row in members}
        drift_rows: list[dict[str, Any]] = []
        pattern_counts: Counter[tuple[str, str, str, str]] = Counter()
        for row in members:
            if normalize_pixel_status(row) == "exact_match":
                continue
            host, pattern, query_keys = _url_pattern(None if row["fetch_url"] is None else str(row["fetch_url"]))
            semantic = {
                "source": row["source"], "source_building_id": row["source_building_id"],
                "source_asset_id": row["source_asset_id"], "fetch_host": host,
                "path_pattern": pattern, "query_keys": query_keys,
                "expected_pixel_sha256": row["expected_pixel_sha256"],
                "actual_pixel_sha256": row["actual_pixel_sha256"], "actual_raw_sha256": row["actual_raw_sha256"],
                "http_status": row["http_status"], "v1_verification_record_sha256": row["verification_record_sha256"],
            }
            drift_rows.append({**semantic, "record_sha256": _record(semantic)})
            pattern_counts[(str(row["source"]), host, pattern, query_keys)] += 1
        source_summary: list[dict[str, Any]] = []
        for source in sorted({str(row["source"]) for row in members}):
            total = sum(str(row["source"]) == source for row in members)
            drift = sum(str(row["source"]) == source for row in drift_rows)
            semantic = {
                "source": source, "asset_count": total, "exact_match_count": total - drift,
                "pixel_unverified_count": drift, "drift_rate": round(drift / total, 8),
            }
            source_summary.append({**semantic, "record_sha256": _record(semantic)})
        patterns: list[dict[str, Any]] = []
        for (source, host, pattern, query_keys), count in sorted(pattern_counts.items()):
            semantic = {
                "source": source, "fetch_host": host, "path_pattern": pattern,
                "query_keys": query_keys, "drift_count": count,
            }
            patterns.append({**semantic, "record_sha256": _record(semantic)})

        admissible = [row for row in edge_semantics if row["disposition"] == "admissible"]
        ordered_review = sorted(
            admissible,
            key=lambda row: hashlib.sha256((OVERLAY_VERSION + "\0blind\0" + str(row["pair_id"])).encode()).hexdigest(),
        )
        for edge in ordered_review:
            center = member_by_asset[str(edge["center_source_asset_id"])]
            member = member_by_asset[str(edge["member_source_asset_id"])]
            edge["center_pixel_sha256"] = center["actual_pixel_sha256"]
            edge["member_pixel_sha256"] = member["actual_pixel_sha256"]
            if edge["center_pixel_sha256"] not in cache_by_pixel or edge["member_pixel_sha256"] not in cache_by_pixel:
                raise DriftOverlayError("admissible blind pair lacks a pinned local derivative")

        run_id = "vnx_gallery_overlay_" + canonical_sha256(
            {"base_sha256": base_before, "contract_sha256": contract_sha, "version": OVERLAY_VERSION}
        )[:24]
        sheet_sha = _render_review_page(root, DEFAULT_REVIEW_PAGE, ordered_review, cache_by_pixel)
        form = {
            "review_version": BLIND_REVIEW_VERSION, "overlay_run_id": run_id,
            "blind_to_source_project_url_filename": True, "reviewer": None,
            "items": [{"opaque_pair_id": f"B{index:03d}", "decision": None} for index in range(1, len(ordered_review) + 1)],
            "allowed_decisions": ["same_or_variant", "distinct_false_collapse", "unreviewable"],
        }
        _atomic_json(root / DEFAULT_REVIEW_FORM, form)
        review_rows: list[dict[str, Any]] = []
        for index, edge in enumerate(ordered_review, 1):
            left = cache_by_pixel[str(edge["center_pixel_sha256"])]
            right = cache_by_pixel[str(edge["member_pixel_sha256"])]
            semantic = {
                "opaque_pair_id": f"B{index:03d}", "direct_pair_id": edge["pair_id"],
                "sheet_path": DEFAULT_REVIEW_PAGE.as_posix(), "sheet_sha256": sheet_sha,
                "sheet_order": index, "left_pixel_sha256": edge["center_pixel_sha256"],
                "right_pixel_sha256": edge["member_pixel_sha256"],
                "left_cache_sha256": left["encoded_sha256"], "right_cache_sha256": right["encoded_sha256"],
                "decision": None, "reviewer": None, "reviewed_at": None,
            }
            review_rows.append({**semantic, "record_sha256": _record(semantic)})
    finally:
        base.close()

    base_after = sha256_file(base_file)
    if base_after != base_before:
        raise DriftOverlayError("immutable v1 SHA changed while building overlay")
    base_group_count = len({str(row["group_id"]) for row in members})
    exact_count = sum(row["overlay_pixel_status"] == "exact_match" for row in assets)
    drift_count = len(assets) - exact_count
    serveable_groups = sum(row["group_status"] == "serveable" for row in groups)
    unresolved_groups = len(groups) - serveable_groups
    split_count = sum(row["disposition"] == "split" for row in edge_semantics)
    cache_inventory_sha = canonical_sha256([row["record_sha256"] for row in cache_pins])
    if (len(assets), base_group_count, len(groups), exact_count, drift_count, len(admissible), split_count) != (169, 162, 166, 152, 17, 2, 4):
        raise DriftOverlayError("WP1.1 expected N10 accounting does not match v1 evidence")
    if unresolved_groups != 17 or serveable_groups != 149:
        raise DriftOverlayError("representative eligibility accounting mismatch")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_file.with_name(output_file.name + ".building")
    if temporary.exists():
        raise DriftOverlayError(f"stale SQLite temporary artifact: {temporary}")
    database = sqlite3.connect(temporary)
    try:
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(SCHEMA_SQL)
        run_row = {
            "overlay_run_id": run_id, "overlay_version": OVERLAY_VERSION,
            "grouping_version": GROUPING_VERSION, "status": "review_pending",
            "started_at": utc_now(), "completed_at": None,
            "base_path": DEFAULT_BASE.as_posix(), "base_size_bytes": base_file.stat().st_size,
            "base_sha256_before": base_before, "base_sha256_after": base_after,
            "base_run_id": base_run_id, "base_logical_sha256": base_run["stage_a_logical_sha256"],
            "contract_path": DEFAULT_CONTRACT.as_posix(), "contract_sha256": contract_sha,
            "cache_file_count": len(cache_pins), "cache_bytes": sum(int(row["encoded_bytes"]) for row in cache_pins),
            "cache_inventory_sha256": cache_inventory_sha, "source_asset_count": len(assets),
            "base_group_count": base_group_count, "effective_group_count": len(groups),
            "group_delta": len(groups) - base_group_count, "exact_match_count": exact_count,
            "pixel_unverified_count": drift_count, "serveable_group_count": serveable_groups,
            "unresolved_group_count": unresolved_groups, "direct_edge_count": len(edge_semantics),
            "admissible_edge_count": len(admissible), "split_edge_count": split_count,
            "http_requests": 0, "vision_requests": 0, "llm_requests": 0,
            "neon_writes": 0, "r2_writes": 0, "blind_review_status": "pending",
            "review_form_sha256": None, "logical_sha256": "pending",
        }
        _insert_dicts(database, "overlay_runs", [run_row])
        for table, rows in (
            ("asset_overlay", assets), ("effective_groups", groups),
            ("effective_group_members", effective_members), ("edge_overlay", edge_semantics),
            ("drift_assets", drift_rows), ("drift_source_summary", source_summary),
            ("drift_url_patterns", patterns), ("cache_pins", cache_pins),
            ("blind_review_items", review_rows),
        ):
            _insert_dicts(database, table, ({"overlay_run_id": run_id, **row} for row in rows))
        logical = _logical_sha(database, run_id)
        database.execute("UPDATE overlay_runs SET logical_sha256=? WHERE overlay_run_id=?", (logical, run_id))
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DriftOverlayError("overlay SQLite integrity check failed")
        database.commit()
    except Exception:
        database.close()
        if temporary.exists():
            temporary.unlink()
        raise
    else:
        database.close()
        os.replace(temporary, output_file)
    _assert_no_sidecars(output_file)
    if sha256_file(base_file) != base_before:
        raise DriftOverlayError("v1 SHA changed after overlay publication")
    return OverlayResult(
        output_file, run_id, "review_pending", base_group_count, len(groups), exact_count,
        drift_count, len(admissible), split_count, root / DEFAULT_REVIEW_PAGE, root / DEFAULT_REVIEW_FORM,
    )


def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if key not in {"overlay_run_id", "record_sha256"}}


def _report_payload(database: sqlite3.Connection, run: sqlite3.Row, artifact_sha: str) -> dict[str, Any]:
    source_summary = [dict(row) for row in database.execute("SELECT source,asset_count,exact_match_count,pixel_unverified_count,drift_rate FROM drift_source_summary ORDER BY source")]
    url_patterns = [dict(row) for row in database.execute("SELECT source,fetch_host,path_pattern,query_keys,drift_count FROM drift_url_patterns ORDER BY source,fetch_host,path_pattern,query_keys")]
    splits = [dict(row) for row in database.execute("SELECT pair_id,base_group_id,center_source_asset_id,member_source_asset_id,center_pixel_status,member_pixel_status,member_effective_group_id,reason_code FROM edge_overlay WHERE disposition='split' ORDER BY pair_id")]
    review = [dict(row) for row in database.execute("SELECT opaque_pair_id,decision,reviewer,direct_pair_id FROM blind_review_items ORDER BY sheet_order")]
    return {
        "report_version": OVERLAY_VERSION,
        "status": run["status"], "overlay_run_id": run["overlay_run_id"],
        "artifact": {"path": DEFAULT_OUTPUT.as_posix(), "sha256": artifact_sha},
        "immutable_base": {
            "path": run["base_path"], "size_bytes": run["base_size_bytes"],
            "sha256_before": run["base_sha256_before"], "sha256_after": run["base_sha256_after"],
            "run_id": run["base_run_id"],
        },
        "contract": {"path": run["contract_path"], "sha256": run["contract_sha256"]},
        "group_accounting": {
            "base_groups": run["base_group_count"], "effective_groups": run["effective_group_count"],
            "delta": run["group_delta"], "serveable_groups": run["serveable_group_count"],
            "unresolved_groups": run["unresolved_group_count"], "gallery_rows_retained": run["source_asset_count"],
            "exact_match": run["exact_match_count"], "pixel_unverified": run["pixel_unverified_count"],
        },
        "edge_accounting": {
            "total": run["direct_edge_count"], "admissible": run["admissible_edge_count"],
            "split": run["split_edge_count"], "split_changes": splits,
        },
        "drift": {"source_breakdown": source_summary, "url_patterns": url_patterns},
        "cache": {
            "files": run["cache_file_count"], "bytes": run["cache_bytes"],
            "inventory_sha256": run["cache_inventory_sha256"], "source": "v1 verified-pixel cache only",
        },
        "requests_and_writes": {
            "http": run["http_requests"], "vision": run["vision_requests"],
            "llm": run["llm_requests"], "neon": run["neon_writes"], "r2": run["r2_writes"],
        },
        "blind_review": {
            "status": run["blind_review_status"], "coverage": len(review),
            "false_collapses": sum(row["decision"] == "distinct_false_collapse" for row in review),
            "unreviewable": sum(row["decision"] == "unreviewable" for row in review),
            "items": review,
        },
        "logical_sha256": run["logical_sha256"],
    }


def _report_markdown(report: Mapping[str, Any]) -> str:
    groups = report["group_accounting"]
    edges = report["edge_accounting"]
    drift = report["drift"]
    calls = report["requests_and_writes"]
    review = report["blind_review"]
    lines = [
        "# WP1.1 drift overlay N10 — PASS", "",
        f"- overlay run: `{report['overlay_run_id']}`",
        f"- v1 SHA before/after: `{report['immutable_base']['sha256_before']}` / `{report['immutable_base']['sha256_after']}`",
        f"- contract SHA: `{report['contract']['sha256']}`", "",
        "## 결과", "",
        f"- gallery 행: {groups['gallery_rows_retained']}개 유지; exact_match {groups['exact_match']}개, pixel_unverified {groups['pixel_unverified']}개",
        f"- group: {groups['base_groups']} → {groups['effective_groups']} (delta +{groups['delta']}); serveable {groups['serveable_groups']}, unresolved {groups['unresolved_groups']}",
        f"- direct edge: 전체 {edges['total']}, admissible {edges['admissible']}, split {edges['split']}",
        f"- blind review: {review['coverage']}/{edges['admissible']} 전수, false collapse {review['false_collapses']}, unreviewable {review['unreviewable']}",
        f"- 외부 작업: HTTP {calls['http']}, Vision {calls['vision']}, LLM {calls['llm']}, Neon {calls['neon']}, R2 {calls['r2']}", "",
        "## 드리프트 소스 분해", "",
    ]
    for row in drift["source_breakdown"]:
        lines.append(f"- {row['source']}: {row['pixel_unverified_count']}/{row['asset_count']} ({row['drift_rate'] * 100:.2f}%)")
    lines.extend(["", "## URL 패턴", ""])
    for row in drift["url_patterns"]:
        lines.append(f"- {row['source']} `{row['fetch_host']}{row['path_pattern']}` query=`{row['query_keys']}`: {row['drift_count']}건")
    lines.extend(["", "## split 변화", ""])
    for row in edges["split_changes"]:
        lines.append(f"- `{row['pair_id']}`: {row['center_pixel_status']} / {row['member_pixel_status']} → `{row['reason_code']}` → `{row['member_effective_group_id']}`")
    lines.extend(["", f"logical SHA: `{report['logical_sha256']}`", f"artifact SHA: `{report['artifact']['sha256']}`", ""])
    return "\n".join(lines)


def apply_blind_review(
    *, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT, review_path: Path = DEFAULT_REVIEW_FORM
) -> dict[str, Any]:
    root = repo_root.resolve()
    artifact = (root / artifact_path).resolve()
    review_file = (root / review_path).resolve()
    if not artifact.is_file() or not review_file.is_file():
        raise DriftOverlayError("overlay or blind review form is missing")
    for target in (root / DEFAULT_REPORT_JSON, root / DEFAULT_REPORT_MD, root / DEFAULT_MANIFEST):
        if target.exists():
            raise DriftOverlayError(f"refusing to clobber final WP1.1 report: {target}")
    _assert_no_sidecars(artifact)
    payload = json.loads(review_file.read_text(encoding="utf-8"))
    if payload.get("review_version") != BLIND_REVIEW_VERSION or payload.get("blind_to_source_project_url_filename") is not True:
        raise DriftOverlayError("blind review version or blinding contract mismatch")
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise DriftOverlayError("blind review requires a reviewer")
    allowed = {"same_or_variant", "distinct_false_collapse", "unreviewable"}
    parsed: dict[str, str] = {}
    for item in payload.get("items", []):
        opaque = str(item.get("opaque_pair_id", ""))
        decision = item.get("decision")
        if not opaque or opaque in parsed or decision not in allowed:
            raise DriftOverlayError("blind review contains a duplicate or invalid decision")
        parsed[opaque] = str(decision)

    base_file = root / DEFAULT_BASE
    base_before = sha256_file(base_file)
    if base_before != EXPECTED_BASE_SHA256 or sha256_file(root / DEFAULT_CONTRACT) != EXPECTED_CONTRACT_SHA256:
        raise DriftOverlayError("base or contract pin changed before review application")
    database = sqlite3.connect(artifact)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("PRAGMA synchronous=FULL")
    try:
        run = database.execute("SELECT * FROM overlay_runs").fetchone()
        if run is None or run["status"] != "review_pending" or payload.get("overlay_run_id") != run["overlay_run_id"]:
            raise DriftOverlayError("overlay is not the matching review-pending run")
        expected = {str(row[0]) for row in database.execute("SELECT opaque_pair_id FROM blind_review_items WHERE overlay_run_id=?", (run["overlay_run_id"],))}
        if set(parsed) != expected or len(expected) != int(run["admissible_edge_count"]):
            raise DriftOverlayError("blind review does not cover all admissible pairs")
        reviewed_at = utc_now()
        for opaque, decision in sorted(parsed.items()):
            row = database.execute("SELECT * FROM blind_review_items WHERE overlay_run_id=? AND opaque_pair_id=?", (run["overlay_run_id"], opaque)).fetchone()
            semantic = _row_payload(row)
            semantic.update({"decision": decision, "reviewer": reviewer.strip(), "reviewed_at": reviewed_at})
            database.execute(
                "UPDATE blind_review_items SET decision=?,reviewer=?,reviewed_at=?,record_sha256=? WHERE overlay_run_id=? AND opaque_pair_id=?",
                (decision, reviewer.strip(), reviewed_at, _record(semantic), run["overlay_run_id"], opaque),
            )
        false_collapses = sum(value == "distinct_false_collapse" for value in parsed.values())
        unreviewable = sum(value == "unreviewable" for value in parsed.values())
        passed = false_collapses == 0 and unreviewable == 0
        logical = _logical_sha(database, str(run["overlay_run_id"]))
        base_after = sha256_file(base_file)
        if base_after != base_before:
            raise DriftOverlayError("immutable v1 changed during review application")
        database.execute(
            "UPDATE overlay_runs SET status=?,completed_at=?,base_sha256_after=?,blind_review_status=?,review_form_sha256=?,logical_sha256=? WHERE overlay_run_id=?",
            (
                "pass" if passed else "fail", reviewed_at, base_after, "pass" if passed else "fail",
                sha256_file(review_file), logical, run["overlay_run_id"],
            ),
        )
        database.commit()
    finally:
        database.close()
    _assert_no_sidecars(artifact)
    artifact_sha = sha256_file(artifact)
    read_only = _open_immutable(artifact)
    try:
        final_run = read_only.execute("SELECT * FROM overlay_runs").fetchone()
        report = _report_payload(read_only, final_run, artifact_sha)
    finally:
        read_only.close()
    _atomic_json(root / DEFAULT_REPORT_JSON, report)
    _atomic_bytes(root / DEFAULT_REPORT_MD, _report_markdown(report).encode("utf-8"))
    manifest = {
        "manifest_version": OVERLAY_VERSION, "status": report["status"],
        "overlay_run_id": report["overlay_run_id"], "artifact": report["artifact"],
        "immutable_base": report["immutable_base"], "contract": report["contract"],
        "logical_sha256": report["logical_sha256"],
        "review": {
            "page_path": DEFAULT_REVIEW_PAGE.as_posix(), "page_sha256": sha256_file(root / DEFAULT_REVIEW_PAGE),
            "form_path": DEFAULT_REVIEW_FORM.as_posix(), "form_sha256": sha256_file(review_file),
        },
        "reports": {
            "json_path": DEFAULT_REPORT_JSON.as_posix(), "json_sha256": sha256_file(root / DEFAULT_REPORT_JSON),
            "markdown_path": DEFAULT_REPORT_MD.as_posix(), "markdown_sha256": sha256_file(root / DEFAULT_REPORT_MD),
        },
        "request_counts": report["requests_and_writes"],
    }
    _atomic_json(root / DEFAULT_MANIFEST, manifest)
    return {
        "overlay_run_id": report["overlay_run_id"], "passed": report["status"] == "pass",
        "reviewed_pairs": len(parsed), "false_collapse_count": false_collapses,
        "unreviewable_count": unreviewable, "artifact_sha256": artifact_sha,
        "logical_sha256": report["logical_sha256"],
    }


__all__ = [
    "APPLICATION_ID", "BLIND_REVIEW_VERSION", "DEFAULT_BASE", "DEFAULT_CONTRACT",
    "DEFAULT_MANIFEST", "DEFAULT_OUTPUT", "DEFAULT_REPORT_JSON", "DEFAULT_REPORT_MD",
    "DEFAULT_REVIEW_FORM", "DEFAULT_REVIEW_PAGE", "DriftOverlayError", "GROUPING_VERSION",
    "LOGICAL_TABLES", "LOGICAL_VERSION", "OVERLAY_VERSION", "OverlayResult", "SCHEMA_SQL",
    "SCHEMA_VERSION", "apply_blind_review", "build_overlay", "normalize_pixel_status", "sha256_file",
]
