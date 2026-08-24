"""Independent validator for the vNext WP1 gallery draft artifact.

The validator intentionally does not import :mod:`canonical.vnext_gallery_draft`.
It replays source rows, exact classes, direct stars, representative ordering,
accounting, and (when present) the Stage B cache/review gates.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


VALIDATOR_VERSION = "vnext-gallery-draft-independent-validator-v1"
APPLICATION_ID = 0x564E5831
SCHEMA_VERSION = 1
LOGICAL_VERSION = "vnext-gallery-stage-a-logical-v1"
EXPECTED_E2_SHA256 = "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19"
EXPECTED_E3_SHA256 = "8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4"
EXPECTED_E2_SIZE = 10_164_682_752
EXPECTED_E3_SIZE = 10_236_592_128
TERMINAL_PIXEL_STATUSES = frozenset({"exact", "delivery_changed", "source_changed", "failed"})
SUCCESS_PIXEL_STATUSES = frozenset({"exact", "delivery_changed"})
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


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str | None = None


@dataclass(frozen=True)
class ValidationReport:
    artifact_path: Path
    run_id: str | None
    status: str | None
    logical_sha256: str | None
    checks: tuple[Check, ...]
    metrics: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(row.passed for row in self.checks)

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(row.name for row in self.checks if not row.passed)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _check(checks: list[Check], name: str, expected: Any, actual: Any, detail: str | None = None) -> None:
    checks.append(Check(name, expected == actual, expected, actual, detail))


def _record_sha(payload: Mapping[str, Any]) -> str:
    return _sha(dict(payload))


def _role_rank(role: str | None) -> int:
    return 0 if role == "cover" else (1 if role == "gallery" else 2)


def _rep_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    width = int(row.get("original_width") or 0)
    height = int(row.get("original_height") or 0)
    short_edge = min(width, height) if width and height else 0
    ordinal = row.get("source_ordinal")
    return (
        0 if row.get("fingerprint_status") == "success" else 1,
        0 if short_edge >= 512 else 1,
        -(width * height), -short_edge, -max(width, height), 0,
        int(row.get("low_information") or 0), _role_rank(row.get("primary_role")),
        1 if ordinal is None else 0, int(ordinal or 0),
        str(row["source_asset_id"]), str(row.get("candidate_id") or ""),
    )


def _stage_a_logical(connection: sqlite3.Connection, run_id: str) -> str:
    payload: dict[str, Any] = {"version": LOGICAL_VERSION, "tables": {}}
    for table, order in LOGICAL_TABLES:
        payload["tables"][table] = [
            str(row[0])
            for row in connection.execute(
                f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)
            )
        ]
    return _sha(payload)


def _schema_tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _replay_input_lineage(
    connection: sqlite3.Connection,
    e2: sqlite3.Connection,
    e3: sqlite3.Connection,
    run_id: str,
    e2_run_id: str,
    e3_run_id: str,
) -> tuple[int, list[str]]:
    failures: list[str] = []
    count = 0
    query_e2 = """
        SELECT ba.project_count,ba.occurrence_count,ba.roles_json,ba.relation_record_sha256,
               a.e1_run_id,a.fingerprint_status,a.canonical_url,a.fetch_url,a.final_url,
               a.raw_response_sha256,a.normalized_pixel_sha256,a.phash_hex,a.original_width,
               a.original_height,a.normalized_width,a.normalized_height,a.source_record_sha256,
               em.cluster_id,pnm.node_id,a.error_kind
        FROM building_assets ba JOIN assets a
          ON a.run_id=ba.run_id AND a.source=ba.source AND a.source_asset_id=ba.source_asset_id
        LEFT JOIN exact_pixel_cluster_members em
          ON em.run_id=a.run_id AND em.source=a.source AND em.source_asset_id=a.source_asset_id
        LEFT JOIN phash_node_members pnm
          ON pnm.run_id=a.run_id AND pnm.source=a.source AND pnm.source_asset_id=a.source_asset_id
        WHERE ba.run_id=? AND ba.source=? AND ba.source_building_id=? AND ba.source_asset_id=?
    """
    for row in connection.execute("SELECT * FROM input_assets WHERE run_id=? ORDER BY source,source_building_id,source_asset_id", (run_id,)):
        count += 1
        source, building, asset = str(row["source"]), str(row["source_building_id"]), str(row["source_asset_id"])
        src = e2.execute(query_e2, (e2_run_id, source, building, asset)).fetchone()
        if src is None:
            failures.append(f"missing E2 row:{source}/{building}/{asset}")
            continue
        pairs = {
            "project_count": src[0], "occurrence_count": src[1], "roles_json": src[2],
            "e2_relation_record_sha256": src[3], "e1_run_id": src[4], "fingerprint_status": src[5],
            "canonical_url": src[6], "fetch_url": src[7], "final_url": src[8],
            "raw_response_sha256": src[9], "normalized_pixel_sha256": src[10], "phash_hex": src[11],
            "original_width": src[12], "original_height": src[13], "normalized_width": src[14],
            "normalized_height": src[15], "e2_asset_record_sha256": src[16],
            "exact_cluster_id": src[17], "phash_node_id": src[18],
        }
        for key, expected in pairs.items():
            if row[key] != expected:
                failures.append(f"E2 mismatch:{source}/{building}/{asset}:{key}")
                break
        candidate = e3.execute(
            "SELECT candidate_id,primary_role,source_ordinal,quality_flags_json,low_information,"
            "candidate_record_sha256,normalized_pixel_sha256,phash_node_id "
            "FROM image_candidates WHERE run_id=? AND source=? AND source_building_id=? AND source_asset_id=?",
            (e3_run_id, source, building, asset),
        ).fetchone()
        if src[5] != "success":
            expected_disposition, expected_reason = "exclusion", "fingerprint_or_decoder_failure"
        elif candidate is None:
            expected_disposition, expected_reason = "unresolved", "successful_e2_asset_missing_e3_candidate"
        elif int(candidate[4]):
            expected_disposition, expected_reason = "exclusion", "low_information"
        else:
            expected_disposition, expected_reason = "gallery", "eligible_frozen_e2_e3_evidence"
        if (row["disposition"], row["disposition_reason"]) != (expected_disposition, expected_reason):
            failures.append(f"disposition mismatch:{source}/{building}/{asset}")
        if candidate is not None:
            candidate_pairs = {
                "candidate_id": candidate[0], "primary_role": candidate[1], "source_ordinal": candidate[2],
                "quality_flags_json": candidate[3], "low_information": candidate[4],
                "e3_candidate_record_sha256": candidate[5],
            }
            if any(row[key] != value for key, value in candidate_pairs.items()):
                failures.append(f"E3 mismatch:{source}/{building}/{asset}")
            if candidate[6] != src[10] or candidate[7] != src[18]:
                failures.append(f"E2/E3 evidence mismatch:{source}/{building}/{asset}")
        payload = {
            "source": source, "source_building_id": building, "source_asset_id": asset,
            "candidate_id": row["candidate_id"], "disposition": row["disposition"],
            "disposition_reason": row["disposition_reason"], "group_id": row["group_id"],
            "e2_asset_record_sha256": row["e2_asset_record_sha256"],
            "e2_relation_record_sha256": row["e2_relation_record_sha256"],
            "e3_candidate_record_sha256": row["e3_candidate_record_sha256"],
        }
        if row["record_sha256"] != _record_sha(payload):
            failures.append(f"input record SHA mismatch:{source}/{building}/{asset}")
    return count, failures


def _replay_groups(
    connection: sqlite3.Connection,
    e2: sqlite3.Connection,
    run_id: str,
    e2_run_id: str,
) -> tuple[int, int, list[str]]:
    failures: list[str] = []
    direct_count = 0
    group_count = 0
    buildings = connection.execute(
        "SELECT source,source_building_id FROM sample_buildings WHERE run_id=? ORDER BY sample_rank", (run_id,)
    ).fetchall()
    for building_row in buildings:
        source, building = str(building_row[0]), str(building_row[1])
        assets = [dict(row) for row in connection.execute(
            "SELECT * FROM input_assets WHERE run_id=? AND source=? AND source_building_id=? AND disposition='gallery' ORDER BY source_asset_id",
            (run_id, source, building),
        )]
        members = [dict(row) for row in connection.execute(
            "SELECT * FROM group_members WHERE run_id=? AND source=? AND source_building_id=? ORDER BY source_asset_id",
            (run_id, source, building),
        )]
        if len(assets) != len(members) or {row["source_asset_id"] for row in assets} != {row["source_asset_id"] for row in members}:
            failures.append(f"gallery membership mismatch:{source}/{building}")
            continue
        by_asset = {str(row["source_asset_id"]): row for row in assets}
        by_pixel: dict[str, list[dict[str, Any]]] = {}
        for row in assets:
            by_pixel.setdefault(str(row["normalized_pixel_sha256"]), []).append(row)
        classes: list[dict[str, Any]] = []
        for pixel, values in by_pixel.items():
            ordered = sorted(values, key=_rep_key)
            classes.append({"pixel": pixel, "rep": ordered[0], "members": ordered})
        classes.sort(key=lambda item: _rep_key(item["rep"]))
        reps = [item["rep"] for item in classes]
        nodes = sorted({str(row["phash_node_id"]) for row in reps})
        edge_map: dict[frozenset[str], sqlite3.Row] = {}
        if len(nodes) >= 2:
            marks = ",".join("?" for _ in nodes)
            rows = e2.execute(
                "SELECT edge_id,left_node_id,right_node_id,hamming_distance,edge_scope,edge_record_sha256 "
                "FROM phash_edges WHERE run_id=? AND edge_scope='global_le8' AND hamming_distance BETWEEN 1 AND 8 "
                f"AND left_node_id IN ({marks}) AND right_node_id IN ({marks})",
                (e2_run_id, *nodes, *nodes),
            ).fetchall()
            node_asset = {str(row["phash_node_id"]): str(row["source_asset_id"]) for row in reps}
            for edge in rows:
                left, right = node_asset.get(str(edge[1])), node_asset.get(str(edge[2]))
                if left and right and left != right:
                    edge_map[frozenset((left, right))] = edge
        expected_groups: dict[str, str] = {}
        expected_edges: set[tuple[str, str]] = set()
        unassigned = {str(item["rep"]["source_asset_id"]) for item in classes}
        for item in classes:
            center = str(item["rep"]["source_asset_id"])
            if center not in unassigned:
                continue
            unassigned.remove(center)
            attached = [item]
            for other in classes:
                asset = str(other["rep"]["source_asset_id"])
                if asset in unassigned and frozenset((center, asset)) in edge_map:
                    attached.append(other)
                    unassigned.remove(asset)
                    expected_edges.add((center, asset))
            actual_group = next((row["group_id"] for row in members if row["source_asset_id"] == center), None)
            if actual_group is None:
                failures.append(f"missing center group:{source}/{building}/{center}")
                continue
            for attached_class in attached:
                for value in attached_class["members"]:
                    expected_groups[str(value["source_asset_id"])] = str(actual_group)
        actual_groups = {str(row["source_asset_id"]): str(row["group_id"]) for row in members}
        if expected_groups != actual_groups:
            failures.append(f"direct-star replay mismatch:{source}/{building}")
        actual_edge_rows = [dict(row) for row in connection.execute(
            "SELECT * FROM direct_edge_evidence WHERE run_id=? AND group_id IN "
            "(SELECT group_id FROM gallery_groups WHERE run_id=? AND source=? AND source_building_id=?) ORDER BY pair_id",
            (run_id, run_id, source, building),
        )]
        direct_count += len(actual_edge_rows)
        actual_pairs = {(str(row["representative_source_asset_id"]), str(row["member_class_representative_asset_id"])) for row in actual_edge_rows}
        if actual_pairs != expected_edges:
            failures.append(f"direct-edge set mismatch:{source}/{building}")
        for row in actual_edge_rows:
            left_asset, right_asset = row["representative_source_asset_id"], row["member_class_representative_asset_id"]
            left_node = by_asset[str(left_asset)]["phash_node_id"]
            right_node = by_asset[str(right_asset)]["phash_node_id"]
            edge = e2.execute(
                "SELECT edge_id,left_node_id,right_node_id,hamming_distance,edge_scope,edge_record_sha256 "
                "FROM phash_edges WHERE run_id=? AND edge_id=?", (e2_run_id, row["edge_id"])
            ).fetchone()
            if edge is None or frozenset((str(edge[1]), str(edge[2]))) != frozenset((str(left_node), str(right_node))):
                failures.append(f"invalid frozen direct edge:{row['pair_id']}")
            elif (edge[0], edge[3], edge[4], edge[5]) != (row["edge_id"], row["hamming_distance"], row["edge_scope"], row["edge_record_sha256"]):
                failures.append(f"direct edge field mismatch:{row['pair_id']}")
            if int(row["transitive_closure_allowed"]) != 0:
                failures.append(f"transitive edge authorized:{row['pair_id']}")
        for group in connection.execute(
            "SELECT * FROM gallery_groups WHERE run_id=? AND source=? AND source_building_id=? ORDER BY group_rank",
            (run_id, source, building),
        ):
            group_count += 1
            group_members = [row for row in members if row["group_id"] == group["group_id"]]
            if len(group_members) != int(group["member_count"]):
                failures.append(f"group count mismatch:{group['group_id']}")
            class_reps = {str(row["class_representative_source_asset_id"]) for row in group_members}
            representative = str(group["representative_source_asset_id"])
            if representative not in class_reps or min((by_asset[item] for item in class_reps), key=_rep_key)["source_asset_id"] != representative:
                failures.append(f"representative score mismatch:{group['group_id']}")
            for member in group_members:
                class_rep = by_asset[str(member["class_representative_source_asset_id"])]
                if member["normalized_pixel_sha256"] != class_rep["normalized_pixel_sha256"]:
                    failures.append(f"exact-class mismatch:{member['source_asset_id']}")
    return group_count, direct_count, failures


def _replay_occurrences(
    connection: sqlite3.Connection,
    e2: sqlite3.Connection,
    run_id: str,
    e2_run_id: str,
) -> tuple[int, list[str]]:
    failures: list[str] = []
    total = 0
    for building in connection.execute("SELECT source,source_building_id,source_occurrence_count FROM sample_buildings WHERE run_id=?", (run_id,)):
        source, building_id = str(building[0]), str(building[1])
        frozen = e2.execute(
            "SELECT o.occurrence_id,o.source_record_sha256,spb.source_record_sha256 "
            "FROM source_project_buildings spb JOIN project_asset_occurrences o "
            "ON o.run_id=spb.run_id AND o.source=spb.source AND o.source_project_id=spb.source_project_id "
            "WHERE spb.run_id=? AND spb.source=? AND spb.source_building_id=? ORDER BY o.occurrence_id",
            (e2_run_id, source, building_id),
        ).fetchall()
        actual = connection.execute(
            "SELECT occurrence_id,occurrence_record_sha256,membership_record_sha256 FROM source_occurrences "
            "WHERE run_id=? AND source=? AND source_building_id=? ORDER BY occurrence_id",
            (run_id, source, building_id),
        ).fetchall()
        total += len(actual)
        if [tuple(row) for row in frozen] != [tuple(row) for row in actual] or len(actual) != int(building[2]):
            failures.append(f"occurrence replay mismatch:{source}/{building_id}")
    return total, failures


def _validate_stage_b(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    root: Path,
    checks: list[Check],
    metrics: dict[str, Any],
    require_stage_b: bool,
) -> None:
    stage_b_status = str(run["stage_b_status"])
    if not require_stage_b and stage_b_status == "not_started":
        return
    _check(checks, "stage_b_complete", "complete", stage_b_status)
    rows = [dict(row) for row in connection.execute("SELECT * FROM pixel_verifications WHERE run_id=? ORDER BY source,source_building_id,source_asset_id", (run["run_id"],))]
    gallery_count = int(connection.execute("SELECT count(*) FROM group_members WHERE run_id=?", (run["run_id"],)).fetchone()[0])
    _check(checks, "stage_b_member_accounting", gallery_count, len(rows))
    statuses = {str(row["status"]) for row in rows}
    _check(checks, "stage_b_terminal_statuses", True, statuses.issubset(TERMINAL_PIXEL_STATUSES) and len(rows) == gallery_count, str(sorted(statuses)))
    request_sum = sum(int(row["request_count"] or 0) for row in rows)
    _check(checks, "stage_b_http_request_accounting", int(run["stage_b_http_requests"]), request_sum)
    _check(checks, "stage_b_vision_requests", 0, int(run["stage_b_vision_requests"]))
    _check(checks, "stage_b_llm_requests", 0, int(run["stage_b_llm_requests"]))
    if run["stage_b_resume_http_requests"] is not None:
        _check(checks, "terminal_resume_http_requests", 0, int(run["stage_b_resume_http_requests"]))
    cache_rows = [dict(row) for row in connection.execute("SELECT * FROM derivative_cache WHERE run_id=?", (run["run_id"],))]
    cache_by_pixel = {str(row["normalized_pixel_sha256"]): row for row in cache_rows}
    cache_failures: list[str] = []
    for row in rows:
        if row["status"] not in SUCCESS_PIXEL_STATUSES:
            continue
        pixel = str(row["actual_pixel_sha256"])
        cache = cache_by_pixel.get(pixel)
        if cache is None:
            cache_failures.append(f"missing:{pixel}")
            continue
        path = root / str(cache["relative_path"])
        if not path.is_file() or path.stat().st_size != int(cache["encoded_bytes"]) or _sha_file(path) != cache["encoded_sha256"]:
            cache_failures.append(f"invalid:{pixel}")
    _check(checks, "derivative_cache_integrity", [], cache_failures[:20], f"cache_rows={len(cache_rows)}")
    direct_ids = {str(row[0]) for row in connection.execute("SELECT pair_id FROM direct_edge_evidence WHERE run_id=?", (run["run_id"],))}
    local_rows = [dict(row) for row in connection.execute("SELECT * FROM local_pair_evidence WHERE run_id=?", (run["run_id"],))]
    local_direct = {str(row["direct_pair_id"]) for row in local_rows if row["pair_kind"] == "e2_direct"}
    _check(checks, "local_feature_direct_pair_coverage", direct_ids, local_direct)
    _check(checks, "local_feature_no_identity_authorization", 0, sum(int(row["identity_authorization"]) for row in local_rows))
    exact_smoke = sum(row["pair_kind"] == "exact_pixel_smoke" for row in local_rows)
    _check(checks, "local_feature_exact_pixel_smoke", 1, exact_smoke)
    review_rows = [dict(row) for row in connection.execute("SELECT * FROM blind_review_items WHERE run_id=?", (run["run_id"],))]
    _check(checks, "blind_review_direct_pair_coverage", direct_ids, {str(row["direct_pair_id"]) for row in review_rows})
    decisions = {str(row["decision"]) for row in review_rows}
    _check(checks, "blind_review_complete", True, bool(review_rows) and None not in {row["decision"] for row in review_rows})
    false_collapses = sum(row["decision"] == "distinct_false_collapse" for row in review_rows)
    unreviewable = sum(row["decision"] == "unreviewable" for row in review_rows)
    _check(checks, "false_collapse_zero", 0, false_collapses, str(sorted(decisions)))
    _check(checks, "blind_review_unreviewable_zero", 0, unreviewable)
    _check(checks, "blind_review_status", "pass", str(run["blind_review_status"]))
    metrics.update(
        {
            "pixel_status_counts": {status: sum(row["status"] == status for row in rows) for status in sorted(statuses)},
            "stage_b_http_requests": request_sum,
            "derivative_cache_rows": len(cache_rows),
            "local_pair_rows": len(local_rows),
            "blind_review_rows": len(review_rows),
            "false_collapse_count": false_collapses,
        }
    )


def validate_gallery_draft(
    artifact_path: Path,
    *,
    repo_root: Path,
    verify_input_hashes: bool = False,
    require_stage_b: bool = False,
) -> ValidationReport:
    artifact = artifact_path.resolve()
    root = repo_root.resolve()
    checks: list[Check] = []
    metrics: dict[str, Any] = {}
    if not artifact.is_file():
        return ValidationReport(artifact, None, None, None, (Check("artifact_exists", True, False),), metrics)
    sidecars = [str(Path(str(artifact) + suffix)) for suffix in ("-wal", "-shm", "-journal") if Path(str(artifact) + suffix).exists()]
    _check(checks, "sqlite_sidecars_absent", [], sidecars)
    connection = _open(artifact)
    run: sqlite3.Row | None = None
    try:
        _check(checks, "application_id", APPLICATION_ID, int(connection.execute("PRAGMA application_id").fetchone()[0]))
        _check(checks, "schema_version", SCHEMA_VERSION, int(connection.execute("PRAGMA user_version").fetchone()[0]))
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        _check(checks, "sqlite_integrity", "ok", integrity)
        required = {
            "runs", "sample_buildings", "input_assets", "gallery_groups", "group_members",
            "direct_edge_evidence", "exclusion_ledger", "unresolved_ledger", "source_occurrences",
            "validations", "metrics", "pixel_verifications", "derivative_cache", "local_features",
            "local_pair_evidence", "blind_review_items",
        }
        _check(checks, "required_tables", set(), required - _schema_tables(connection))
        runs = connection.execute("SELECT * FROM runs").fetchall()
        _check(checks, "single_run", 1, len(runs))
        if len(runs) != 1:
            return ValidationReport(artifact, None, None, None, tuple(checks), metrics)
        run = runs[0]
        run_id = str(run["run_id"])
        allowed_status = {"stage_a_complete", "stage_b_complete", "complete"}
        _check(checks, "run_status", True, str(run["status"]) in allowed_status, str(run["status"]))
        _check(checks, "stage_a_request_counts", (0, 0, 0), (int(run["stage_a_network_requests"]), int(run["stage_a_vision_requests"]), int(run["stage_a_llm_requests"])))
        samples = [dict(row) for row in connection.execute("SELECT * FROM sample_buildings WHERE run_id=? ORDER BY sample_rank", (run_id,))]
        _check(checks, "sample_count", 10, len(samples))
        _check(checks, "sample_ranks", list(range(1, 11)), [int(row["sample_rank"]) for row in samples])
        source_counts = {source: sum(row["source"] == source for row in samples) for source in ("divisare", "architizer")}
        _check(checks, "source_balance", {"divisare": 5, "architizer": 5}, source_counts)
        stratum_counts = {kind: sum(row["stratum"] == kind for row in samples) for kind in ("normal", "large_gallery", "duplicate_burst")}
        _check(checks, "sample_strata", {"normal": 8, "large_gallery": 1, "duplicate_burst": 1}, stratum_counts)
        e2_path = root / str(run["e2_path"])
        e3_path = root / str(run["e3_path"])
        _check(checks, "e2_size", EXPECTED_E2_SIZE, e2_path.stat().st_size if e2_path.is_file() else None)
        _check(checks, "e3_size", EXPECTED_E3_SIZE, e3_path.stat().st_size if e3_path.is_file() else None)
        _check(checks, "e2_pre_post_pin", (EXPECTED_E2_SHA256, EXPECTED_E2_SHA256), (run["e2_sha256_before"], run["e2_sha256_after"]))
        _check(checks, "e3_pre_post_pin", (EXPECTED_E3_SHA256, EXPECTED_E3_SHA256), (run["e3_sha256_before"], run["e3_sha256_after"]))
        if verify_input_hashes:
            _check(checks, "e2_current_sha", EXPECTED_E2_SHA256, _sha_file(e2_path))
            _check(checks, "e3_current_sha", EXPECTED_E3_SHA256, _sha_file(e3_path))
        e2, e3 = _open(e2_path), _open(e3_path)
        try:
            input_count, lineage_failures = _replay_input_lineage(
                connection, e2, e3, run_id, str(run["e2_run_id"]), str(run["e3_run_id"])
            )
            _check(checks, "input_lineage_replay", [], lineage_failures[:20], f"rows={input_count}")
            group_count, direct_count, group_failures = _replay_groups(
                connection, e2, run_id, str(run["e2_run_id"])
            )
            _check(checks, "exact_direct_star_replay", [], group_failures[:20], f"groups={group_count}, direct={direct_count}")
            occurrence_count, occurrence_failures = _replay_occurrences(
                connection, e2, run_id, str(run["e2_run_id"])
            )
            _check(checks, "source_occurrence_replay", [], occurrence_failures[:20], f"rows={occurrence_count}")
            selection_failures: list[str] = []
            for row in samples:
                source = e3.execute(
                    "SELECT source,source_building_id,selection_record_sha256 FROM selected_buildings WHERE run_id=? AND selection_id=?",
                    (run["e3_run_id"], row["selection_id"]),
                ).fetchone()
                candidate_count = int(e3.execute(
                    "SELECT count(*) FROM image_candidates WHERE run_id=? AND selection_id=?",
                    (run["e3_run_id"], row["selection_id"]),
                ).fetchone()[0])
                if source is None or (source[0], source[1], source[2], candidate_count) != (row["source"], row["source_building_id"], row["e3_selection_record_sha256"], row["e3_asset_count"]):
                    selection_failures.append(str(row["selection_id"]))
            _check(checks, "sample_e3_lineage", [], selection_failures)
        finally:
            e2.close()
            e3.close()
        dispositions = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT disposition,count(*) FROM input_assets WHERE run_id=? GROUP BY disposition", (run_id,))
        }
        input_count = sum(dispositions.values())
        gallery_count = int(connection.execute("SELECT count(*) FROM group_members WHERE run_id=?", (run_id,)).fetchone()[0])
        exclusion_count = int(connection.execute("SELECT count(*) FROM exclusion_ledger WHERE run_id=?", (run_id,)).fetchone()[0])
        unresolved_count = int(connection.execute("SELECT count(*) FROM unresolved_ledger WHERE run_id=?", (run_id,)).fetchone()[0])
        _check(checks, "global_accounting", input_count, gallery_count + exclusion_count + unresolved_count)
        per_building_failures = int(connection.execute(
            "SELECT count(*) FROM (SELECT b.source,b.source_building_id,b.input_asset_count,"
            "sum(a.disposition='gallery') g,sum(a.disposition='exclusion') e,sum(a.disposition='unresolved') u "
            "FROM sample_buildings b JOIN input_assets a USING(run_id,source,source_building_id) "
            "WHERE b.run_id=? GROUP BY b.source,b.source_building_id HAVING input_asset_count<>g+e+u)",
            (run_id,),
        ).fetchone()[0])
        _check(checks, "per_building_accounting", 0, per_building_failures)
        logical = _stage_a_logical(connection, run_id)
        _check(checks, "stage_a_logical_sha", str(run["stage_a_logical_sha256"]), logical)
        metrics.update(
            {
                "building_count": len(samples), "input_asset_count": input_count,
                "gallery_asset_count": gallery_count, "gallery_group_count": int(connection.execute("SELECT count(*) FROM gallery_groups WHERE run_id=?", (run_id,)).fetchone()[0]),
                "exclusion_count": exclusion_count, "unresolved_count": unresolved_count,
                "direct_attachment_count": int(connection.execute("SELECT count(*) FROM direct_edge_evidence WHERE run_id=?", (run_id,)).fetchone()[0]),
            }
        )
        _validate_stage_b(connection, run, root, checks, metrics, require_stage_b)
        return ValidationReport(artifact, run_id, str(run["status"]), logical, tuple(checks), metrics)
    finally:
        connection.close()


__all__ = ["Check", "VALIDATOR_VERSION", "ValidationReport", "validate_gallery_draft"]
