"""vNext S6-lite projection: Neon ``building_images`` child rows for the Friday release (offline).

Identity = the legacy rows' CURRENT identity (WP0b snapshot ``source_refs``), NOT S1 merges
(job card ``20261020_vnext_wp4_s2_full_order.md``, final decisions). Parents are unchanged; this
module only emits the gallery child rowset (DDL ``docs/vnext/BUILDING_IMAGES_DDL_v1_1.sql``).

Rules (``vnext-s6-lite-v1``):

* Ref mapping: legacy ``source_refs[source][i]`` is a source PROJECT id. For ``divisare`` /
  ``architizer`` it maps through frozen E2 ``source_project_buildings`` (the E2 run pinned by the S2
  run) to one S2 source building ``(source, source_building_id)``. Other sources (archello,
  metalocus) and project ids absent from E2 / buildings absent from S2 are reported as unmapped.
* Ref order: ``identity_source`` refs first, then the remaining ``source_refs`` keys in snapshot
  order; list order within a source. Duplicate S2 buildings are kept once (first position).
* Child rows = union of the S2 gallery groups of the mapped buildings. Groups that share the exact
  ``representative_pixel_sha256`` (cross-source or cross-building) collapse into ONE row whose
  representative is the best by (usable_resolution, pixel_area, short_edge, long_edge,
  not low_information) and then the stable key ``(source, representative_source_asset_id)``.
  ``image_asset_id`` = representative ``source_asset_id``; ``url`` = its S2 ``serving_url``.
* Order: rows by min over collapsed constituents of (ref position, S2 ``sort_order``); then the
  row matching the legacy ``display_cover_url`` (after the 2026-10-16 Architizer host migration)
  moves to position 0. Match basis, in order: row url; any constituent group's serving url; any
  occurrence url (S2 ``source_occurrences``, host-migrated) of a member asset of the row. Every
  comparison is on the percent-DECODED form (``urllib.parse.unquote`` of both sides; builder v2):
  legacy Neon stores Architizer non-ASCII filenames raw while S2 ``serving_url`` is percent-encoded.
  ``url`` itself stays the S2 ``serving_url`` (encoded). ``sort_order`` = 0..n-1.
* Classification columns NULL, ``semantic_type``/``cover_slot`` ``[]``, ``is_cover_default``
  false, ``engine``/``engine_version`` NULL, ``projection_version`` ``vnext-s6-lite-v1``.

Offline: inputs are opened read-only/immutable and hashed before and after; outputs are written to
``<name>.building`` then ``os.replace``d; no clobber.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

PROJECTION_VERSION = "vnext-s6-lite-v1"
BUILDER_VERSION = "vnext-s6-lite-projection-builder-v2"
ROWSET_VERSION = "vnext-s6-lite-ordered-rowset-v1"
MANIFEST_KIND = "vnext_s6_lite_building_images_v1"
S2_SOURCES = ("architizer", "divisare")

# The live-Neon Architizer rewrite (tools/architizer_image_host_migrate.py PATTERN/REPLACEMENT; a test
# asserts identity with the tool's constants).
ARCHITIZER_PATTERN = r"https?://architizer-prod\.imgix\.net(/[^\"?\s]*)(\?[^\"\s]*)?"
ARCHITIZER_REPLACEMENT = "https://images.architizer.com" + r"\1" + "?width=1680&quality=60"
_ARCHITIZER_RE = re.compile(ARCHITIZER_PATTERN)

# Child row columns in DDL v1.1 order (created_at/updated_at are DB defaults).
DDL_COLUMNS = (
    "canonical_bld_id", "image_asset_id", "url", "sort_order", "semantic_type", "medium",
    "spatial_context", "framing_scale", "camera_angle", "drawing_kind", "project_state",
    "project_legibility", "is_cover_default", "cover_slot", "engine", "engine_version",
    "projection_version",
)
NULL_CLASSIFICATION = (
    "medium", "spatial_context", "framing_scale", "camera_angle", "drawing_kind",
    "project_state", "project_legibility", "engine", "engine_version",
)

DEFAULT_LEGACY = Path("data/canonical/legacy_neon_snapshot_2026q3.jsonl")
DEFAULT_S2_DB = Path("data/enrichment/vnext_gallery_full_v1.db")
DEFAULT_S2_MANIFEST = Path("data/reports/vnext_gallery_full_v1_manifest.json")
DEFAULT_S2_VALIDATION = Path("data/reports/vnext_gallery_full_v1_validation.json")
DEFAULT_E2 = Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db")
DEFAULT_DDL = Path("docs/vnext/BUILDING_IMAGES_DDL_v1_1.sql")
DEFAULT_OUTPUT = Path("data/canonical/vnext_s6_lite_building_images_v1.jsonl")
DEFAULT_MANIFEST = Path("data/reports/vnext_s6_lite_building_images_v1_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_s6_lite_building_images_v1_report.json")

# Production pins (WP0b snapshot report, WP4 S2 r3 job card, WP0a freeze).
PRODUCTION_PINS: dict[str, str] = {
    "legacy_snapshot": "17e8da1d74f1508b80580b40a2992aa536621214992747ed39978eba1f04527f",
    "s2_db": "7d7c4a239efd5d945c341b5b61a8fad4dce233486dcf616fa1904cfd2862722d",
    "s2_manifest": "e578fed5521ae0734614dc4e7109b6e2bab83136cb3c8117f752e66ce03b0d29",
    "s2_validation": "bd6b50b6501cdc97df3b0e4d53023f7e593fde49252cf72d5e785be6a2811105",
    "e2_db": "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19",
}
PRODUCTION_LEGACY_ROWS = 39_478


class S6LiteError(RuntimeError):
    """A pinned input or a projection invariant is violated."""


# --------------------------------------------------------------------------- pure helpers

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk: int = 16 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def migrate_url(url: str | None) -> str | None:
    """Legacy (imgix) Architizer URL -> live-Neon form; other URLs unchanged."""
    if not url:
        return url
    return _ARCHITIZER_RE.sub(ARCHITIZER_REPLACEMENT, url)


def url_match_key(url: str | None) -> str | None:
    """Cover/diff comparison key: host-migrated, then percent-decoded (raw vs %-encoded forms equal)."""
    migrated = migrate_url(url)
    return unquote(migrated) if migrated else migrated


def ordered_refs(source_refs: Mapping[str, Sequence[Any]], identity_source: str | None) -> list[tuple[str, str]]:
    keys = list(source_refs.keys())
    if identity_source in source_refs:
        keys = [identity_source] + [key for key in keys if key != identity_source]
    refs: list[tuple[str, str]] = []
    for key in keys:
        for value in source_refs.get(key) or []:
            refs.append((str(key), str(value)))
    return refs


def representative_key(group: Mapping[str, Any]) -> tuple[Any, ...]:
    score = group["score"]
    return (
        0 if score.get("usable_resolution") else 1,
        -int(score.get("pixel_area") or 0),
        -int(score.get("short_edge") or 0),
        -int(score.get("long_edge") or 0),
        1 if score.get("e3_low_information") else 0,
        str(group["source"]),
        str(group["asset"]),
    )


def child_row(canonical_bld_id: str, asset: str, url: str, sort_order: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "canonical_bld_id": canonical_bld_id,
        "image_asset_id": asset,
        "url": url,
        "sort_order": sort_order,
        "semantic_type": [],
        "is_cover_default": False,
        "cover_slot": [],
        "projection_version": PROJECTION_VERSION,
    }
    for column in NULL_CLASSIFICATION:
        row[column] = None
    return {column: row[column] for column in DDL_COLUMNS}


def serving_line(row: Mapping[str, Any]) -> str:
    """One canonical line of the ordered serving rowset (DDL columns only)."""
    return canonical_json({column: row[column] for column in DDL_COLUMNS})


def project_building(
    legacy: Mapping[str, Any],
    mapped: Sequence[tuple[str, str]],
    groups_by_building: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    occurrence_index: Mapping[tuple[str, str], Mapping[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project one legacy row to its ordered child rows.

    ``mapped``: S2 buildings in ref order (deduplicated). ``groups_by_building``: per S2 building the
    gallery groups (dicts with group_id, sort_order, asset, pixel, url, score, source). ``occurrence_index``:
    per S2 building {url_match_key(occurrence url) -> group_id}.
    """
    bld = str(legacy["canonical_bld_id"])
    by_pixel: dict[str, list[tuple[tuple[int, int], Mapping[str, Any]]]] = {}
    pixel_order: list[str] = []
    for position, key in enumerate(mapped):
        for group in groups_by_building.get(key, ()):
            pixel = str(group["pixel"])
            if pixel not in by_pixel:
                by_pixel[pixel] = []
                pixel_order.append(pixel)
            by_pixel[pixel].append(((position, int(group["sort_order"])), group))
    rows: list[dict[str, Any]] = []
    collapsed = Counter()
    for pixel in pixel_order:
        constituents = by_pixel[pixel]
        rep = min((group for _, group in constituents), key=representative_key)
        anchor = min(position for position, _ in constituents)
        if len(constituents) > 1:
            kind = "cross_source" if len({g["source"] for _, g in constituents}) > 1 else "same_source_cross_building"
            collapsed[kind] += len(constituents) - 1
        rows.append(
            {
                "anchor": anchor,
                "rep": rep,
                "groups": sorted((group for _, group in constituents), key=lambda g: (g["source"], g["source_building_id"], int(g["sort_order"]))),
            }
        )
    rows.sort(key=lambda item: (item["anchor"], item["rep"]["source"], item["rep"]["asset"]))
    urls = [item["rep"]["url"] for item in rows]
    if len(set(urls)) != len(urls):
        raise S6LiteError(f"{bld}: duplicate serving url after pixel collapse")
    assets = [item["rep"]["asset"] for item in rows]
    if len(set(assets)) != len(assets):
        raise S6LiteError(f"{bld}: duplicate image_asset_id after pixel collapse")

    cover_url = url_match_key(legacy.get("display_cover_url"))
    cover_index: int | None = None
    cover_basis: str | None = None
    if cover_url:
        for index, item in enumerate(rows):
            if url_match_key(item["rep"]["url"]) == cover_url:
                cover_index, cover_basis = index, "row_url"
                break
        if cover_index is None:
            for index, item in enumerate(rows):
                if any(url_match_key(group["url"]) == cover_url for group in item["groups"]):
                    cover_index, cover_basis = index, "constituent_url"
                    break
        if cover_index is None:
            group_to_row = {group["group_id"]: index for index, item in enumerate(rows) for group in item["groups"]}
            hits = sorted(
                group_to_row[group_id]
                for key in mapped
                for group_id in [occurrence_index.get(key, {}).get(cover_url)]
                if group_id is not None and group_id in group_to_row
            )
            if hits:
                cover_index, cover_basis = hits[0], "occurrence_url"
    if cover_index is not None and cover_index > 0:
        rows.insert(0, rows.pop(cover_index))

    out: list[dict[str, Any]] = []
    for sort_order, item in enumerate(rows):
        rep = item["rep"]
        row = child_row(bld, str(rep["asset"]), str(rep["url"]), sort_order)
        row["provenance"] = {
            "representative_source": rep["source"],
            "representative_source_building_id": rep["source_building_id"],
            "representative_group_id": rep["group_id"],
            "representative_pixel_sha256": rep["pixel"],
            "group_ids": [group["group_id"] for group in item["groups"]],
            "cover_match": cover_basis if (sort_order == 0 and cover_index is not None) else None,
        }
        row["record_sha256"] = sha256_text(canonical_json(row))
        out.append(row)
    stats = {
        "collapsed": dict(collapsed),
        "cover_basis": cover_basis,
        "has_display_cover": bool(cover_url),
    }
    return out, stats


def ordered_rowset_sha256(lines: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


# --------------------------------------------------------------------------- I/O

def _open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def iter_legacy(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _legacy_image_urls(row: Mapping[str, Any]) -> list[tuple[str | None, str]]:
    out = []
    for image in row.get("all_images") or []:
        if isinstance(image, Mapping) and image.get("url"):
            out.append((image.get("source"), migrate_url(str(image["url"]))))
    return out


_DIVISARE_KEY = re.compile(r"/project_images/(\d+)/([^/.]+)")


def divisare_image_key(url: str) -> str | None:
    """``project_images/<a>/<b>`` key shared by the c_fit cover form and the f_auto gallery form."""
    if "images.divisare.com" not in url:
        return None
    match = _DIVISARE_KEY.search(url)
    return None if match is None else match.group(1) + "/" + match.group(2)


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    manifest_path: Path
    report_path: Path
    run_id: str
    row_count: int
    ordered_rowset_sha256: str
    output_sha256: str
    elapsed_seconds: float


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise S6LiteError(f"refusing to clobber existing artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise S6LiteError(f"stale temporary artifact exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def config() -> dict[str, Any]:
    return {
        "builder_version": BUILDER_VERSION,
        "projection_version": PROJECTION_VERSION,
        "rowset_version": ROWSET_VERSION,
        "identity": "legacy snapshot source_refs (current Neon identity); S1 merges NOT applied (P3 later)",
        "ref_mapping": "source_refs[divisare|architizer][i] = source_project_id -> E2 source_project_buildings (S2 run e2_run_id) -> S2 buildings; other sources unmapped",
        "ref_order": "identity_source first, then remaining source_refs keys in snapshot order; list order within a source; duplicate S2 buildings kept at first position",
        "collapse": "groups sharing exact representative_pixel_sha256 -> one row; representative = min (not usable_resolution, -pixel_area, -short_edge, -long_edge, low_information, source, source_asset_id)",
        "order": "min constituent (ref position, S2 sort_order), tie (source, asset); then display_cover_url match (host-migrated) to position 0 by row_url > constituent_url > occurrence_url",
        "cover_match_key": "urllib.parse.unquote(host-migrated url) on both sides (percent-encoding-insensitive); stored url = S2 serving_url unchanged",
        "architizer_pattern": ARCHITIZER_PATTERN,
        "architizer_replacement": ARCHITIZER_REPLACEMENT,
        "classification": "all NULL, semantic_type '{}', cover_slot '{}', is_cover_default false, engine NULL (until S3)",
        "ddl": "docs/vnext/BUILDING_IMAGES_DDL_v1_1.sql",
    }


def build_projection(
    *,
    repo_root: Path,
    legacy_path: Path = DEFAULT_LEGACY,
    s2_db_path: Path = DEFAULT_S2_DB,
    s2_manifest_path: Path = DEFAULT_S2_MANIFEST,
    s2_validation_path: Path = DEFAULT_S2_VALIDATION,
    e2_path: Path = DEFAULT_E2,
    ddl_path: Path = DEFAULT_DDL,
    output_path: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST,
    report_path: Path = DEFAULT_REPORT,
    pins: Mapping[str, str] | None = PRODUCTION_PINS,
    expected_legacy_rows: int | None = PRODUCTION_LEGACY_ROWS,
) -> BuildResult:
    started = time.perf_counter()
    root = repo_root
    paths = {
        "legacy_snapshot": _resolve(root, legacy_path),
        "s2_db": _resolve(root, s2_db_path),
        "s2_manifest": _resolve(root, s2_manifest_path),
        "s2_validation": _resolve(root, s2_validation_path),
        "e2_db": _resolve(root, e2_path),
        "ddl_v1_1": _resolve(root, ddl_path),
    }
    output_path = _resolve(root, output_path)
    manifest_path = _resolve(root, manifest_path)
    report_path = _resolve(root, report_path)
    for path in (output_path, manifest_path, report_path):
        if path.exists():
            raise S6LiteError(f"refusing to clobber existing artifact: {path}")

    before = {role: sha256_file(path) for role, path in paths.items()}
    if pins is not None:
        for role, expected in pins.items():
            if before.get(role) != expected:
                raise S6LiteError(f"input pin mismatch for {role}: {before.get(role)} != {expected}")

    s2_manifest = json.loads(paths["s2_manifest"].read_text(encoding="utf-8"))
    s2_validation = json.loads(paths["s2_validation"].read_text(encoding="utf-8"))
    if s2_validation.get("passed") is not True:
        raise S6LiteError("S2 validation is not PASS")
    if s2_manifest.get("output", {}).get("sha256") != before["s2_db"]:
        raise S6LiteError("S2 manifest output sha does not match the S2 DB")
    if s2_validation.get("logical_sha256") != s2_manifest.get("logical_sha256"):
        raise S6LiteError("S2 validation logical sha does not match the S2 manifest")

    s2 = _open_ro(paths["s2_db"])
    runs = s2.execute("SELECT run_id, status, logical_sha256, e2_run_id, e2_sha256_before FROM runs").fetchall()
    if len(runs) != 1 or runs[0][1] != "complete" or runs[0][2] != s2_manifest.get("logical_sha256"):
        raise S6LiteError("S2 must contain one complete run matching the manifest logical sha")
    s2_run_id, _, s2_logical, e2_run_id, e2_sha_in_s2 = runs[0]
    if e2_sha_in_s2 != before["e2_db"]:
        raise S6LiteError("E2 DB is not the E2 the S2 run was built from")
    e2 = _open_ro(paths["e2_db"])
    project_map: dict[tuple[str, str], str] = {}
    for source, project, building in e2.execute(
        "SELECT source, source_project_id, source_building_id FROM source_project_buildings WHERE run_id=?", (e2_run_id,)
    ):
        key = (str(source), str(project))
        if key in project_map and project_map[key] != building:
            raise S6LiteError(f"E2 project maps to several buildings: {key}")
        project_map[key] = str(building)
    e2.close()
    s2_buildings = {(str(s), str(b)) for s, b in s2.execute("SELECT source, source_building_id FROM buildings WHERE run_id=?", (s2_run_id,))}

    # pass 1: legacy -> mapped S2 buildings
    legacy_rows: list[dict[str, Any]] = []
    unmapped = Counter()
    mapped_refs = Counter()
    unmapped_examples: list[dict[str, str]] = []
    needed: set[tuple[str, str]] = set()
    for record in iter_legacy(paths["legacy_snapshot"]):
        refs = ordered_refs(record.get("source_refs") or {}, record.get("identity_source"))
        mapped: list[tuple[str, str]] = []
        for source, project in refs:
            if source not in S2_SOURCES:
                unmapped["source_not_in_s2:" + source] += 1
                continue
            building = project_map.get((source, project))
            if building is None:
                unmapped["project_not_in_e2:" + source] += 1
                unmapped_examples.append({"canonical_bld_id": record["canonical_bld_id"], "source": source, "source_project_id": project, "reason": "project_not_in_e2"})
                continue
            if (source, building) not in s2_buildings:
                unmapped["building_not_in_s2:" + source] += 1
                unmapped_examples.append({"canonical_bld_id": record["canonical_bld_id"], "source": source, "source_project_id": project, "reason": "building_not_in_s2"})
                continue
            mapped_refs[source] += 1
            if (source, building) not in mapped:
                mapped.append((source, building))
        needed.update(mapped)
        legacy_rows.append(
            {
                "canonical_bld_id": str(record["canonical_bld_id"]),
                "display_cover_url": record.get("display_cover_url"),
                "is_publishable": bool(record.get("is_publishable")),
                "identity_source": record.get("identity_source"),
                "mapped": mapped,
                "n_refs": len(refs),
                "legacy_urls": _legacy_image_urls(record),
            }
        )
    legacy_rows.sort(key=lambda r: r["canonical_bld_id"])
    ids = [r["canonical_bld_id"] for r in legacy_rows]
    if len(set(ids)) != len(ids):
        raise S6LiteError("duplicate canonical_bld_id in legacy snapshot")
    if expected_legacy_rows is not None and len(ids) != expected_legacy_rows:
        raise S6LiteError(f"legacy rows {len(ids)} != {expected_legacy_rows}")

    # pass 2: S2 groups / occurrences for the needed buildings
    groups_by_building: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for gid, source, building, sort_order, asset, pixel, url, score in s2.execute(
        "SELECT group_id, source, source_building_id, sort_order, representative_source_asset_id, "
        "representative_pixel_sha256, serving_url, representative_score_json FROM gallery_groups WHERE run_id=?",
        (s2_run_id,),
    ):
        key = (str(source), str(building))
        if key in needed:
            groups_by_building[key].append(
                {"group_id": gid, "source": source, "source_building_id": building, "sort_order": int(sort_order),
                 "asset": asset, "pixel": pixel, "url": url, "score": json.loads(score)}
            )
    for value in groups_by_building.values():
        value.sort(key=lambda g: g["sort_order"])
    asset_group: dict[tuple[str, str, str], str] = {}
    for source, building, asset, gid in s2.execute(
        "SELECT source, source_building_id, source_asset_id, group_id FROM group_members WHERE run_id=?", (s2_run_id,)
    ):
        if (source, building) in needed:
            asset_group[(source, building, asset)] = gid
    occurrence_index: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    group_rank = {g["group_id"]: g["sort_order"] for v in groups_by_building.values() for g in v}
    for source, building, asset, url in s2.execute(
        "SELECT source, source_building_id, source_asset_id, occurrence_url FROM source_occurrences "
        "WHERE run_id=? AND occurrence_url IS NOT NULL AND source_asset_id IS NOT NULL ORDER BY source, source_building_id, occurrence_id",
        (s2_run_id,),
    ):
        key = (source, building)
        if key not in needed:
            continue
        gid = asset_group.get((source, building, asset))
        if gid is None:
            continue
        murl = url_match_key(url)
        current = occurrence_index[key].get(murl)
        if current is None or group_rank[gid] < group_rank[current]:
            occurrence_index[key][murl] = gid
    s2.close()

    # pass 3: project
    lines: list[str] = []
    body: list[bytes] = []
    collapsed_total = Counter()
    cover_basis = Counter()
    cover_miss_hosts = Counter()
    per_building_counts: list[int] = []
    pub = Counter()
    diff = Counter()
    diff_by_legacy_source: dict[str, Counter] = defaultdict(Counter)
    multi_building_rows = 0
    s2_users = Counter()
    for legacy in legacy_rows:
        rows, stats = project_building(legacy, legacy["mapped"], groups_by_building, occurrence_index)
        for key in legacy["mapped"]:
            s2_users[key] += 1
        if len(legacy["mapped"]) > 1:
            multi_building_rows += 1
        collapsed_total.update(stats["collapsed"])
        if stats["has_display_cover"]:
            cover_basis[stats["cover_basis"] or "not_in_gallery"] += 1
            if not stats["cover_basis"]:
                host = urlsplit(str(legacy["display_cover_url"])).netloc or "?"
                cover_miss_hosts[host + (":gallery_empty" if not rows else ":gallery_nonempty")] += 1
        else:
            cover_basis["no_display_cover_url"] += 1
        n = len(rows)
        per_building_counts.append(n)
        pub[("publishable" if legacy["is_publishable"] else "not_publishable") + ("_with_images" if n else "_without_images")] += 1
        new_urls = {row["url"] for row in rows}
        legacy_urls = legacy["legacy_urls"]
        old_set = {url for _, url in legacy_urls}
        diff["same"] += len(old_set & new_urls)
        diff["added"] += len(new_urls - old_set)
        diff["dropped"] += len(old_set - new_urls)
        new_div_keys = {k for k in (divisare_image_key(u) for u in new_urls) if k}
        new_decoded = {unquote(u) for u in new_urls}
        diff["dropped_percent_encoding_only"] += len({u for u in old_set - new_urls if unquote(u) in new_decoded})
        seen: set[str] = set()
        for source, url in legacy_urls:
            if url in seen:
                continue
            seen.add(url)
            label = str(source)
            if url in new_urls:
                diff_by_legacy_source[label]["same"] += 1
            elif unquote(url) in new_decoded:
                diff_by_legacy_source[label]["dropped_url_form_only_percent_encoding"] += 1
            elif divisare_image_key(url) and divisare_image_key(url) in new_div_keys:
                diff_by_legacy_source[label]["dropped_url_form_only_same_divisare_image"] += 1
            else:
                diff_by_legacy_source[label]["dropped"] += 1
        for row in rows:
            line = serving_line(row)
            lines.append(line)
            body.append((canonical_json(row) + "\n").encode("utf-8"))
    rowset_sha = ordered_rowset_sha256(lines)
    data = b"".join(body)
    output_sha = hashlib.sha256(data).hexdigest()

    after = {role: sha256_file(path) for role, path in paths.items()}
    if after != before:
        raise S6LiteError("an input changed during the build")

    hist = Counter()
    for n in per_building_counts:
        bucket = "0" if n == 0 else "1-4" if n < 5 else "5-9" if n < 10 else "10-19" if n < 20 else "20-49" if n < 50 else "50-99" if n < 100 else "100+"
        hist[bucket] += 1
    sorted_counts = sorted(per_building_counts)

    def pct(p: float) -> int:
        return sorted_counts[min(len(sorted_counts) - 1, int(p * (len(sorted_counts) - 1)))] if sorted_counts else 0

    has_cover = sum(v for k, v in cover_basis.items() if k != "no_display_cover_url")
    in_gallery = sum(v for k, v in cover_basis.items() if k in {"row_url", "constituent_url", "occurrence_url"})
    cfg = config()
    config_sha = sha256_text(canonical_json(cfg))
    run_id = "vnx_s6lite_" + sha256_text(canonical_json({"config": config_sha, "inputs": before}))[:24]
    row_count = len(lines)
    manifest = {
        "manifest_kind": MANIFEST_KIND,
        "run_id": run_id,
        "builder_version": BUILDER_VERSION,
        "projection_version": PROJECTION_VERSION,
        "rowset_version": ROWSET_VERSION,
        "config": cfg,
        "config_sha256": config_sha,
        "input_pins": {role: {"path": _rel(root, path), "sha256": before[role], "size_bytes": path.stat().st_size} for role, path in paths.items()},
        "s2": {"run_id": s2_run_id, "logical_sha256": s2_logical, "e2_run_id": e2_run_id},
        "output": {"path": _rel(root, output_path), "sha256": output_sha, "size_bytes": len(data)},
        "row_counts": {
            "building_images": row_count,
            "legacy_buildings": len(legacy_rows),
            "buildings_with_images": sum(1 for n in per_building_counts if n),
            "buildings_without_images": sum(1 for n in per_building_counts if not n),
        },
        "ordered_rowset_sha256": rowset_sha,
        "ordered_rowset_rule": "sha256 over canonical_json(DDL columns) + '\\n' per row, rows ordered by (canonical_bld_id, sort_order)",
        "requests": {"network": 0, "llm": 0, "vision": 0, "db_writes": 0},
    }
    report = {
        "status": "PASS",
        "run_id": run_id,
        "builder_version": BUILDER_VERSION,
        "row_count": row_count,
        "ordered_rowset_sha256": rowset_sha,
        "legacy_buildings": len(legacy_rows),
        "buildings_with_images": dict(sorted(pub.items())),
        "images_per_building": {
            "histogram": dict(sorted(hist.items(), key=lambda kv: ["0", "1-4", "5-9", "10-19", "20-49", "50-99", "100+"].index(kv[0]))),
            "min": sorted_counts[0] if sorted_counts else 0,
            "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99),
            "max": sorted_counts[-1] if sorted_counts else 0,
            "mean": round(row_count / len(per_building_counts), 3) if per_building_counts else 0,
        },
        "ref_mapping": {
            "mapped_s2_buildings_by_source": dict(sorted(Counter(src for r in legacy_rows for src, _ in r["mapped"]).items())),
            "mapped_refs": dict(sorted(mapped_refs.items())),
            "unmapped_refs": dict(sorted(unmapped.items())),
            "unmapped_examples": unmapped_examples[:200],
            "legacy_rows_with_no_mapped_s2_building": sum(1 for r in legacy_rows if not r["mapped"]),
            "legacy_rows_with_multiple_s2_buildings": multi_building_rows,
            "s2_buildings_used": len(s2_users),
            "s2_buildings_used_by_multiple_legacy_rows": sum(1 for v in s2_users.values() if v > 1),
        },
        "pixel_collapse": dict(sorted(collapsed_total.items())),
        "display_cover": {
            "basis_counts": dict(sorted(cover_basis.items())),
            "legacy_rows_with_display_cover_url": has_cover,
            "present_in_gallery": in_gallery,
            "present_in_gallery_rate": round(in_gallery / has_cover, 6) if has_cover else None,
            "not_in_gallery_by_cover_host": dict(sorted(cover_miss_hosts.items())),
        },
        "diff_vs_legacy_all_images": {
            "note": "URL-level, legacy URLs host-migrated (architizer imgix -> images.architizer.com); per-building sets. "
                    "dropped includes dropped_percent_encoding_only (same URL after percent-decoding: legacy raw non-ASCII vs S2 %-encoded); "
                    "per-source buckets: same / dropped_url_form_only_percent_encoding / dropped_url_form_only_same_divisare_image / dropped",
            "same": diff["same"], "added": diff["added"], "dropped": diff["dropped"],
            "dropped_percent_encoding_only": diff["dropped_percent_encoding_only"],
            "legacy_images_by_source": {k: dict(sorted(v.items())) for k, v in sorted(diff_by_legacy_source.items())},
        },
        "inputs_unchanged": True,
        "requests": {"network": 0, "llm": 0, "vision": 0, "db_writes": 0},
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    _atomic_write(output_path, data)
    _atomic_write(manifest_path, _json_bytes(manifest))
    _atomic_write(report_path, _json_bytes(report))
    return BuildResult(output_path, manifest_path, report_path, run_id, row_count, rowset_sha, output_sha, time.perf_counter() - started)
