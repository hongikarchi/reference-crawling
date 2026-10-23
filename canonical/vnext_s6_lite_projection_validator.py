"""Independent validator for the vNext S6-lite ``building_images`` projection artifact.

Deliberately does NOT import :mod:`canonical.vnext_s6_lite_projection`. Rules are restated from the
job card / module docstring (``vnext-s6-lite-v1``) and re-derived from the frozen inputs:

input pins (re-hash), S2 validation PASS + manifest/logical consistency, output SHA/size, per-row
record SHA, DDL v1.1 unclassified-row shape, FK subset of the legacy snapshot ids, PK / sort_order /
url uniqueness and 0-based contiguity, file order, ordered rowset SHA, manifest row counts, and a FULL
replay (legacy source_refs -> E2 project->building -> S2 groups, exact-pixel collapse, ordering and
display-cover promotion) compared row by row including provenance.

Cover matching (builder v2 rule, restated): legacy ``display_cover_url`` and every candidate url (row,
constituent group, member occurrence) are host-migrated and then percent-DECODED before comparison, so
a raw non-ASCII legacy Architizer url equals the %-encoded S2 serving url. The stored ``url`` is the S2
serving url unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

VALIDATOR_VERSION = "vnext-s6-lite-projection-independent-validator-v2"
PROJECTION_VERSION = "vnext-s6-lite-v1"
BUILDER_VERSION = "vnext-s6-lite-projection-builder-v2"
MANIFEST_KIND = "vnext_s6_lite_building_images_v1"
ROLES = ("legacy_snapshot", "s2_db", "s2_manifest", "s2_validation", "e2_db", "ddl_v1_1")
PRODUCTION_PINS = {
    "legacy_snapshot": "17e8da1d74f1508b80580b40a2992aa536621214992747ed39978eba1f04527f",
    "s2_db": "7d7c4a239efd5d945c341b5b61a8fad4dce233486dcf616fa1904cfd2862722d",
    "s2_manifest": "e578fed5521ae0734614dc4e7109b6e2bab83136cb3c8117f752e66ce03b0d29",
    "s2_validation": "bd6b50b6501cdc97df3b0e4d53023f7e593fde49252cf72d5e785be6a2811105",
    "e2_db": "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19",
}
COLUMNS = [
    "canonical_bld_id", "image_asset_id", "url", "sort_order", "semantic_type", "medium",
    "spatial_context", "framing_scale", "camera_angle", "drawing_kind", "project_state",
    "project_legibility", "is_cover_default", "cover_slot", "engine", "engine_version",
    "projection_version",
]
NULLABLE_CLASS = ["medium", "spatial_context", "framing_scale", "camera_angle", "drawing_kind",
                  "project_state", "project_legibility", "engine", "engine_version"]
# Live-Neon Architizer rewrite (tools/architizer_image_host_migrate.py, restated).
IMGIX = re.compile(r"https?://architizer-prod\.imgix\.net(/[^\"?\s]*)(\?[^\"\s]*)?")
IMGIX_REPL = "https://images.architizer.com" + r"\1" + "?width=1680&quality=60"


def _cj(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 24), b""):
            h.update(block)
    return h.hexdigest()


def _host_migrate(url: Any) -> Any:
    return IMGIX.sub(IMGIX_REPL, url) if isinstance(url, str) and url else url


def _cover_key(url: Any) -> Any:
    m = _host_migrate(url)
    return unquote(m) if isinstance(m, str) and m else m


@dataclass
class Check:
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None


@dataclass
class ValidationReport:
    artifact_path: str
    manifest_path: str
    run_id: str | None
    ordered_rowset_sha256: str | None
    row_count: int
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failed_check_names(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


def _expected_rows(root: Path, inputs: Mapping[str, Path]) -> tuple[list[dict[str, Any]], set[str]]:
    """Independent replay of the S6-lite rules from the frozen inputs."""
    s2 = sqlite3.connect("file:" + inputs["s2_db"].resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    (run_id, e2_run), = s2.execute("SELECT run_id, e2_run_id FROM runs").fetchall()
    e2 = sqlite3.connect("file:" + inputs["e2_db"].resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    proj_to_bld = {}
    for src, pid, bid in e2.execute("SELECT source, source_project_id, source_building_id FROM source_project_buildings WHERE run_id=?", (e2_run,)):
        proj_to_bld[src + "\x1f" + pid] = bid
    e2.close()
    in_s2 = set(src + "\x1f" + bid for src, bid in s2.execute("SELECT source, source_building_id FROM buildings WHERE run_id=?", (run_id,)))

    legacy: dict[str, dict[str, Any]] = {}
    with inputs["legacy_snapshot"].open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            sr = rec.get("source_refs") or {}
            first = rec.get("identity_source")
            order = ([first] if first in sr else []) + [k for k in sr if k != first]
            blds: list[str] = []
            for src in order:
                if src not in ("divisare", "architizer"):
                    continue
                for pid in sr[src] or []:
                    bid = proj_to_bld.get(src + "\x1f" + str(pid))
                    if bid is None:
                        continue
                    k = src + "\x1f" + bid
                    if k in in_s2 and k not in blds:
                        blds.append(k)
            legacy[str(rec["canonical_bld_id"])] = {"blds": blds, "cover": _cover_key(rec.get("display_cover_url"))}
    need = set(k for v in legacy.values() for k in v["blds"])

    groups: dict[str, list[tuple]] = defaultdict(list)
    for gid, src, bid, so, asset, px, url, score in s2.execute(
        "SELECT group_id, source, source_building_id, sort_order, representative_source_asset_id, representative_pixel_sha256, "
        "serving_url, representative_score_json FROM gallery_groups WHERE run_id=? ORDER BY source, source_building_id, sort_order", (run_id,)):
        k = src + "\x1f" + bid
        if k in need:
            sc = json.loads(score)
            rank = (not sc.get("usable_resolution"), -(sc.get("pixel_area") or 0), -(sc.get("short_edge") or 0),
                    -(sc.get("long_edge") or 0), bool(sc.get("e3_low_information")), src, asset)
            groups[k].append((so, gid, src, bid, asset, px, url, rank))
    member_group: dict[tuple, str] = {}
    for src, bid, asset, gid in s2.execute("SELECT source, source_building_id, source_asset_id, group_id FROM group_members WHERE run_id=?", (run_id,)):
        if src + "\x1f" + bid in need:
            member_group[(src, bid, asset)] = gid
    group_so = {g[1]: g[0] for lst in groups.values() for g in lst}
    occ: dict[str, dict[str, str]] = defaultdict(dict)
    for src, bid, asset, url, oid in s2.execute(
        "SELECT source, source_building_id, source_asset_id, occurrence_url, occurrence_id FROM source_occurrences WHERE run_id=?", (run_id,)):
        k = src + "\x1f" + bid
        if k not in need or url is None or asset is None:
            continue
        gid = member_group.get((src, bid, asset))
        if gid is None:
            continue
        mu = _cover_key(url)
        prev = occ[k].get(mu)
        if prev is None or group_so[gid] < group_so[prev]:
            occ[k][mu] = gid
    s2.close()

    out: list[dict[str, Any]] = []
    for cid in sorted(legacy):
        info = legacy[cid]
        classes: dict[str, list] = {}
        for pos, k in enumerate(info["blds"]):
            for g in groups.get(k, []):
                classes.setdefault(g[5], []).append(((pos, g[0]), g))
        items = []
        for px, members in classes.items():
            best = min(members, key=lambda m: m[1][7])[1]
            anchor = min(m[0] for m in members)
            gs = sorted((m[1] for m in members), key=lambda g: (g[2], g[3], g[0]))
            items.append((anchor, best, gs))
        items.sort(key=lambda it: (it[0], it[1][2], it[1][4]))
        cover = info["cover"]
        pick, basis = None, None
        if cover:
            pick = next((i for i, it in enumerate(items) if _cover_key(it[1][6]) == cover), None)
            basis = "row_url" if pick is not None else None
            if pick is None:
                pick = next((i for i, it in enumerate(items) if any(_cover_key(g[6]) == cover for g in it[2])), None)
                basis = "constituent_url" if pick is not None else None
            if pick is None:
                where = {g[1]: i for i, it in enumerate(items) for g in it[2]}
                cand = [where[occ[k][cover]] for k in info["blds"] if cover in occ.get(k, {}) and occ[k][cover] in where]
                if cand:
                    pick, basis = min(cand), "occurrence_url"
        if pick:
            items.insert(0, items.pop(pick))
        for i, (anchor, best, gs) in enumerate(items):
            row = {c: None for c in COLUMNS}
            row.update({"canonical_bld_id": cid, "image_asset_id": best[4], "url": best[6], "sort_order": i,
                        "semantic_type": [], "cover_slot": [], "is_cover_default": False,
                        "projection_version": PROJECTION_VERSION})
            row["provenance"] = {
                "representative_source": best[2], "representative_source_building_id": best[3],
                "representative_group_id": best[1], "representative_pixel_sha256": best[5],
                "group_ids": [g[1] for g in gs],
                "cover_match": basis if (i == 0 and pick is not None) else None,
            }
            out.append(row)
    return out, set(legacy)


def _cover_keys(path: Path) -> dict[str, Any]:
    out = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rec = json.loads(line)
                out[str(rec["canonical_bld_id"])] = _cover_key(rec.get("display_cover_url"))
    return out


def validate_projection(manifest_path: Path, *, repo_root: Path, production_pins: bool = True) -> ValidationReport:
    checks: list[Check] = []

    def add(name: str, ok: bool, expected: Any = None, actual: Any = None) -> None:
        checks.append(Check(name, bool(ok), expected, actual))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = repo_root / manifest["output"]["path"]
    report = ValidationReport(str(artifact), str(manifest_path), manifest.get("run_id"), manifest.get("ordered_rowset_sha256"), 0)
    add("manifest_kind_versions", manifest.get("manifest_kind") == MANIFEST_KIND and manifest.get("builder_version") == BUILDER_VERSION
        and manifest.get("projection_version") == PROJECTION_VERSION,
        [MANIFEST_KIND, BUILDER_VERSION, PROJECTION_VERSION],
        [manifest.get("manifest_kind"), manifest.get("builder_version"), manifest.get("projection_version")])
    pins = manifest.get("input_pins", {})
    add("manifest_input_roles", sorted(pins) == sorted(ROLES), sorted(ROLES), sorted(pins))
    inputs = {role: repo_root / pins[role]["path"] for role in ROLES if role in pins}
    actual_sha = {role: _sha_file(path) for role, path in inputs.items()}
    add("input_sha_matches_manifest", all(actual_sha[r] == pins[r]["sha256"] for r in inputs),
        {r: pins[r]["sha256"] for r in inputs}, actual_sha)
    if production_pins:
        add("input_sha_matches_production_pins", all(actual_sha.get(r) == v for r, v in PRODUCTION_PINS.items()),
            PRODUCTION_PINS, {r: actual_sha.get(r) for r in PRODUCTION_PINS})
    s2m = json.loads(inputs["s2_manifest"].read_text(encoding="utf-8"))
    s2v = json.loads(inputs["s2_validation"].read_text(encoding="utf-8"))
    add("s2_validation_pass", s2v.get("passed") is True, True, s2v.get("passed"))
    add("s2_manifest_matches_db_and_validation",
        s2m.get("output", {}).get("sha256") == actual_sha["s2_db"] and s2v.get("logical_sha256") == s2m.get("logical_sha256")
        and manifest.get("s2", {}).get("logical_sha256") == s2m.get("logical_sha256"),
        s2m.get("logical_sha256"), [s2v.get("logical_sha256"), manifest.get("s2", {}).get("logical_sha256")])

    raw = artifact.read_bytes()
    add("output_sha_size", hashlib.sha256(raw).hexdigest() == manifest["output"]["sha256"] and len(raw) == manifest["output"]["size_bytes"],
        [manifest["output"]["sha256"], manifest["output"]["size_bytes"]], [hashlib.sha256(raw).hexdigest(), len(raw)])
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
    report.row_count = len(rows)

    bad_keys = bad_sha = bad_shape = 0
    for r in rows:
        if set(r) != set(COLUMNS) | {"provenance", "record_sha256"}:
            bad_keys += 1
        body = {k: v for k, v in r.items() if k != "record_sha256"}
        if hashlib.sha256(_cj(body).encode("utf-8")).hexdigest() != r.get("record_sha256"):
            bad_sha += 1
        if (any(r.get(c) is not None for c in NULLABLE_CLASS) or r.get("semantic_type") != [] or r.get("cover_slot") != []
                or r.get("is_cover_default") is not False or r.get("projection_version") != PROJECTION_VERSION
                or not isinstance(r.get("url"), str) or not r["url"].strip() or not isinstance(r.get("image_asset_id"), str)
                or not r["image_asset_id"].strip() or not isinstance(r.get("sort_order"), int) or r["sort_order"] < 0):
            bad_shape += 1
    add("row_keys_exact", bad_keys == 0, 0, bad_keys)
    add("row_record_sha256", bad_sha == 0, 0, bad_sha)
    add("ddl_v1_1_unclassified_shape", bad_shape == 0, 0, bad_shape)

    per: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        per[r["canonical_bld_id"]].append(r)
    pk_dup = so_bad = url_dup = 0
    for lst in per.values():
        if len({r["image_asset_id"] for r in lst}) != len(lst):
            pk_dup += 1
        if sorted(r["sort_order"] for r in lst) != list(range(len(lst))):
            so_bad += 1
        if len({r["url"] for r in lst}) != len(lst):
            url_dup += 1
    add("pk_unique_per_building", pk_dup == 0, 0, pk_dup)
    add("sort_order_contiguous_0_based", so_bad == 0, 0, so_bad)
    add("url_unique_per_building", url_dup == 0, 0, url_dup)
    keys = [(r["canonical_bld_id"], r["sort_order"]) for r in rows]
    add("file_order_bld_sort", keys == sorted(keys), True, keys == sorted(keys))
    add("no_legacy_imgix_host", not any("architizer-prod.imgix.net" in r["url"] for r in rows), 0,
        sum("architizer-prod.imgix.net" in r["url"] for r in rows))
    add("architizer_neon_query_form", all(r["url"].endswith("?width=1680&quality=60") for r in rows if r["url"].startswith("https://images.architizer.com/")),
        True, sum(1 for r in rows if r["url"].startswith("https://images.architizer.com/") and not r["url"].endswith("?width=1680&quality=60")))

    h = hashlib.sha256()
    for r in rows:
        h.update(_cj({c: r[c] for c in COLUMNS}).encode("utf-8") + b"\n")
    add("ordered_rowset_sha256", h.hexdigest() == manifest.get("ordered_rowset_sha256"), manifest.get("ordered_rowset_sha256"), h.hexdigest())

    expected, legacy_ids = _expected_rows(repo_root, inputs)
    add("fk_subset_of_legacy_ids", set(per) <= legacy_ids, 0, len(set(per) - legacy_ids))
    rc = manifest.get("row_counts", {})
    actual_counts = {"building_images": len(rows), "legacy_buildings": len(legacy_ids),
                     "buildings_with_images": len(per), "buildings_without_images": len(legacy_ids) - len(per)}
    add("manifest_row_counts", rc == actual_counts, rc, actual_counts)
    mismatches = 0
    first = None
    if len(expected) != len(rows):
        mismatches = abs(len(expected) - len(rows))
    for e, a in zip(expected, rows):
        a_cmp = {k: v for k, v in a.items() if k != "record_sha256"}
        if e != a_cmp:
            mismatches += 1
            if first is None:
                first = {"expected": e, "actual": a_cmp}
    cover_by_bld = _cover_keys(inputs["legacy_snapshot"])
    misplaced = 0
    for cid, lst in per.items():
        ck = cover_by_bld.get(cid)
        if not ck:
            continue
        hit = sorted(r["sort_order"] for r in lst if _cover_key(r["url"]) == ck)
        if hit and hit[0] != 0:
            misplaced += 1
    add("cover_row_url_decoded_match_at_sort_order_0", misplaced == 0, 0, misplaced)
    add("full_replay_rows_equal", mismatches == 0 and len(expected) == len(rows), {"rows": len(expected), "mismatches": 0},
        {"rows": len(rows), "mismatches": mismatches, "first": first})
    after = {role: _sha_file(path) for role, path in inputs.items()}
    add("inputs_unchanged_during_validation", after == actual_sha, actual_sha, after)
    report.checks = checks
    return report
