"""Independent validator for the WP1.1 gallery drift overlay.

This module intentionally does not import the overlay producer.  It replays
the immutable v1 evidence, verifies the local cache, and checks the split and
blind-review gates from the published SQLite artifact.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


APPLICATION_ID = 0x564E5832
SCHEMA_VERSION = 1
EXPECTED_BASE_SHA256 = "d63b7a5a279f71d0b9c68b6ca66c6123e934dd67fa5c085a9f8ea16736717cc7"
EXPECTED_CONTRACT_SHA256 = "49133125c1755b050a6276aa6c5ff7a0916ad9d63d9da7793cf181c8db97b8a3"
LOGICAL_VERSION = "vnext-gallery-drift-overlay-logical-v1.1"
DEFAULT_ARTIFACT = Path("data/enrichment/vnext_gallery_draft_n10_v1_1.db")
DEFAULT_BASE = Path("data/enrichment/vnext_gallery_draft_n10_v1.db")
DEFAULT_CONTRACT = Path("docs/vnext/CONTRACT_GALLERY_SEMANTICS.md")
DEFAULT_MANIFEST = Path("data/reports/vnext_gallery_draft_n10_v1_1_manifest.json")
DEFAULT_REPORT_JSON = Path("data/reports/vnext_gallery_draft_n10_v1_1_report.json")
DEFAULT_REPORT_MD = Path("data/reports/vnext_gallery_draft_n10_v1_1_report.md")
DEFAULT_REVIEW_FORM = Path("data/reports/vnext_gallery_blind_review_n10_v1_1_review.json")

RECORD_TABLES: tuple[tuple[str, str], ...] = (
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


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str = ""


@dataclass(frozen=True)
class ValidationReport:
    artifact_path: Path
    overlay_run_id: str | None
    status: str | None
    checks: tuple[Check, ...]
    metrics: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "validator_version": "vnext-gallery-drift-overlay-independent-validator-v1",
            "artifact_path": self.artifact_path.as_posix(),
            "overlay_run_id": self.overlay_run_id, "status": self.status,
            "passed": self.passed,
            "checks": [
                {"name": row.name, "passed": row.passed, "expected": _jsonable(row.expected),
                 "actual": _jsonable(row.actual), "detail": row.detail}
                for row in self.checks
            ],
            "metrics": dict(self.metrics),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(_jsonable(key)): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _check(checks: list[Check], name: str, expected: Any, actual: Any, detail: str = "") -> None:
    checks.append(Check(name, expected == actual, expected, actual, detail))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


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


def _normalize(row: Mapping[str, Any]) -> str:
    if (
        str(row["status"]) in {"exact", "delivery_changed"}
        and row["expected_pixel_sha256"]
        and row["expected_pixel_sha256"] == row["actual_pixel_sha256"]
    ):
        return "exact_match"
    return "pixel_unverified"


def _url_pattern(url: str | None) -> tuple[str, str, str]:
    parsed = urlsplit(url or "")
    if parsed.path.startswith("/media/mediadata/uploads/"):
        pattern = "/media/mediadata/uploads/{file}"
    elif parsed.path.startswith("/media/"):
        pattern = "/media/{file}"
    else:
        parent = parsed.path.rsplit("/", 1)[0] if "/" in parsed.path else ""
        pattern = (parent + "/{file}") or "/{file}"
    return parsed.netloc.casefold(), pattern, ",".join(sorted(parse_qs(parsed.query).keys()))


def _schema_tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _semantic_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if key not in {"overlay_run_id", "record_sha256"}}


def _validate_records(connection: sqlite3.Connection, run_id: str) -> list[str]:
    failures: list[str] = []
    for table, order in RECORD_TABLES:
        for row in connection.execute(f"SELECT * FROM {table} WHERE overlay_run_id=? ORDER BY {order}", (run_id,)):
            actual = _canonical_sha(_semantic_payload(row))
            if actual != row["record_sha256"]:
                failures.append(f"{table}:{actual}:{row['record_sha256']}")
    return failures


def _logical_sha(connection: sqlite3.Connection, run_id: str) -> str:
    payload: dict[str, Any] = {"version": LOGICAL_VERSION, "tables": {}}
    for table, order in RECORD_TABLES:
        payload["tables"][table] = [
            str(row[0])
            for row in connection.execute(
                f"SELECT record_sha256 FROM {table} WHERE overlay_run_id=? ORDER BY {order}",
                (run_id,),
            )
        ]
    return _canonical_sha(payload)


def validate_overlay(
    artifact_path: Path = DEFAULT_ARTIFACT,
    *, repo_root: Path,
    require_reports: bool = True,
) -> ValidationReport:
    root = repo_root.resolve()
    artifact = (root / artifact_path).resolve() if not artifact_path.is_absolute() else artifact_path.resolve()
    checks: list[Check] = []
    metrics: dict[str, Any] = {}
    if not artifact.is_file():
        return ValidationReport(artifact, None, None, (Check("artifact_exists", False, True, False),), metrics)
    sidecars = [str(Path(str(artifact) + suffix)) for suffix in ("-wal", "-shm", "-journal") if Path(str(artifact) + suffix).exists()]
    _check(checks, "sqlite_sidecars_absent", [], sidecars)
    connection = _open(artifact)
    base: sqlite3.Connection | None = None
    run_id: str | None = None
    status: str | None = None
    try:
        _check(checks, "application_id", APPLICATION_ID, int(connection.execute("PRAGMA application_id").fetchone()[0]))
        _check(checks, "schema_version", SCHEMA_VERSION, int(connection.execute("PRAGMA user_version").fetchone()[0]))
        _check(checks, "sqlite_integrity", "ok", str(connection.execute("PRAGMA integrity_check").fetchone()[0]))
        required = {"overlay_runs", *(table for table, _ in RECORD_TABLES)}
        _check(checks, "required_tables", set(), required - _schema_tables(connection))
        runs = connection.execute("SELECT * FROM overlay_runs").fetchall()
        _check(checks, "single_run", 1, len(runs))
        if len(runs) != 1:
            return ValidationReport(artifact, None, None, tuple(checks), metrics)
        run = runs[0]
        run_id, status = str(run["overlay_run_id"]), str(run["status"])
        _check(checks, "run_status", "pass", status)
        _check(checks, "blind_review_status", "pass", str(run["blind_review_status"]))
        _check(
            checks, "zero_external_work", (0, 0, 0, 0, 0),
            tuple(int(run[key]) for key in ("http_requests", "vision_requests", "llm_requests", "neon_writes", "r2_writes")),
        )

        base_path = root / str(run["base_path"])
        contract_path = root / str(run["contract_path"])
        _check(checks, "base_path", DEFAULT_BASE.as_posix(), str(run["base_path"]))
        _check(checks, "contract_path", DEFAULT_CONTRACT.as_posix(), str(run["contract_path"]))
        current_base_sha = _sha_file(base_path) if base_path.is_file() else None
        current_contract_sha = _sha_file(contract_path) if contract_path.is_file() else None
        _check(
            checks, "immutable_v1_sha_pre_post_current",
            (EXPECTED_BASE_SHA256, EXPECTED_BASE_SHA256, EXPECTED_BASE_SHA256),
            (run["base_sha256_before"], run["base_sha256_after"], current_base_sha),
        )
        _check(checks, "contract_sha", EXPECTED_CONTRACT_SHA256, run["contract_sha256"])
        _check(checks, "contract_current_sha", EXPECTED_CONTRACT_SHA256, current_contract_sha)
        _check(checks, "base_size", base_path.stat().st_size if base_path.is_file() else None, int(run["base_size_bytes"]))

        base = _open(base_path)
        base_run = base.execute("SELECT * FROM runs").fetchone()
        _check(checks, "base_run_id", base_run["run_id"], run["base_run_id"])
        _check(checks, "base_logical_sha", base_run["stage_a_logical_sha256"], run["base_logical_sha256"])
        base_members = [
            dict(row)
            for row in base.execute(
                "SELECT m.*,p.status,p.expected_pixel_sha256,p.actual_pixel_sha256,p.actual_raw_sha256,"
                "p.http_status,p.fetch_url,p.record_sha256 AS verification_record_sha256 "
                "FROM group_members m JOIN pixel_verifications p "
                "USING(run_id,source,source_building_id,source_asset_id) WHERE m.run_id=? "
                "ORDER BY m.source,m.source_building_id,m.source_asset_id",
                (base_run["run_id"],),
            )
        ]
        base_by_key = {
            (str(row["source"]), str(row["source_building_id"]), str(row["source_asset_id"])): row
            for row in base_members
        }
        overlay_assets = [dict(row) for row in connection.execute("SELECT * FROM asset_overlay WHERE overlay_run_id=? ORDER BY source,source_building_id,source_asset_id", (run_id,))]
        overlay_by_key = {
            (str(row["source"]), str(row["source_building_id"]), str(row["source_asset_id"])): row
            for row in overlay_assets
        }
        _check(checks, "asset_key_replay", set(base_by_key), set(overlay_by_key))
        lineage_failures: list[str] = []
        for key, source in base_by_key.items():
            overlay = overlay_by_key.get(key)
            if overlay is None:
                continue
            expected_status = _normalize(source)
            expected = (
                source["group_id"], source["record_sha256"], source["normalized_pixel_sha256"],
                source["status"], expected_status, int(expected_status != "exact_match"),
            )
            actual = (
                overlay["base_group_id"], overlay["base_group_member_sha256"], overlay["frozen_pixel_sha256"],
                overlay["v1_pixel_status"], overlay["overlay_pixel_status"], overlay["pixel_unverified"],
            )
            if expected != actual:
                lineage_failures.append("/".join(key))
            if (int(overlay["representative_eligible"]), int(overlay["serving_eligible"])) != (
                int(expected_status == "exact_match"), int(expected_status == "exact_match")
            ):
                lineage_failures.append("eligibility:" + "/".join(key))
        _check(checks, "asset_lineage_and_status_replay", [], lineage_failures)
        status_counts = Counter(str(row["overlay_pixel_status"]) for row in overlay_assets)
        _check(checks, "pixel_status_counts", {"exact_match": 152, "pixel_unverified": 17}, dict(status_counts))
        _check(checks, "run_asset_counts", (169, 152, 17), (run["source_asset_count"], run["exact_match_count"], run["pixel_unverified_count"]))

        base_edges = {str(row["pair_id"]): dict(row) for row in base.execute("SELECT * FROM direct_edge_evidence WHERE run_id=?", (base_run["run_id"],))}
        overlay_edges = {str(row["pair_id"]): dict(row) for row in connection.execute("SELECT * FROM edge_overlay WHERE overlay_run_id=?", (run_id,))}
        _check(checks, "edge_key_replay", set(base_edges), set(overlay_edges))
        base_asset_by_id = {str(row["source_asset_id"]): row for row in base_members}
        local_by_pair = {
            str(row["direct_pair_id"]): dict(row)
            for row in base.execute("SELECT * FROM local_pair_evidence WHERE run_id=? AND pair_kind='e2_direct'", (base_run["run_id"],))
        }
        edge_failures: list[str] = []
        for pair, frozen in base_edges.items():
            overlay = overlay_edges.get(pair)
            if overlay is None:
                continue
            center_status = _normalize(base_asset_by_id[str(frozen["representative_source_asset_id"])])
            member_status = _normalize(base_asset_by_id[str(frozen["member_class_representative_asset_id"])])
            admissible = center_status == member_status == "exact_match"
            expected = (
                frozen["group_id"], frozen["representative_source_asset_id"], frozen["member_class_representative_asset_id"],
                center_status, member_status, "admissible" if admissible else "split",
                "both_endpoints_exact_match" if admissible else "unverifiable_edge_source_changed",
                frozen["edge_id"], frozen["hamming_distance"], frozen["edge_scope"],
                frozen["edge_record_sha256"], frozen["record_sha256"], frozen["transitive_closure_allowed"], 1,
                local_by_pair[pair]["outcome"], local_by_pair[pair]["record_sha256"],
            )
            actual = (
                overlay["base_group_id"], overlay["center_source_asset_id"], overlay["member_source_asset_id"],
                overlay["center_pixel_status"], overlay["member_pixel_status"], overlay["disposition"],
                overlay["reason_code"], overlay["edge_id"], overlay["hamming_distance"], overlay["edge_scope"],
                overlay["edge_record_sha256"], overlay["base_edge_row_sha256"], overlay["transitive_closure_allowed"],
                overlay["evidence_preserved"], overlay["local_outcome"], overlay["local_evidence_sha256"],
            )
            if expected != actual:
                edge_failures.append(pair)
            same_group = overlay["center_effective_group_id"] == overlay["member_effective_group_id"]
            if same_group != admissible:
                edge_failures.append("group:" + pair)
        _check(checks, "direct_edge_rule_and_evidence_replay", [], edge_failures)
        edge_counts = Counter(str(row["disposition"]) for row in overlay_edges.values())
        _check(checks, "edge_counts", {"admissible": 2, "split": 4}, dict(edge_counts))
        _check(checks, "run_edge_counts", (6, 2, 4), (run["direct_edge_count"], run["admissible_edge_count"], run["split_edge_count"]))

        effective_members = [dict(row) for row in connection.execute("SELECT * FROM effective_group_members WHERE overlay_run_id=?", (run_id,))]
        effective_groups = [dict(row) for row in connection.execute("SELECT * FROM effective_groups WHERE overlay_run_id=?", (run_id,))]
        effective_member_keys = {(row["source"], row["source_building_id"], row["source_asset_id"]) for row in effective_members}
        _check(checks, "effective_member_accounting", set(base_by_key), effective_member_keys)
        member_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in effective_members:
            member_by_group[str(row["effective_group_id"])].append(row)
        exact_class_groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for row in effective_members:
            exact_class_groups[(str(row["source"]), str(row["source_building_id"]), str(row["frozen_pixel_sha256"]))].add(str(row["effective_group_id"]))
        _check(checks, "frozen_exact_class_never_split", 0, sum(len(groups) != 1 for groups in exact_class_groups.values()))
        group_failures: list[str] = []
        ranks: dict[tuple[str, str], list[int]] = defaultdict(list)
        for group in effective_groups:
            gid = str(group["effective_group_id"])
            rows = member_by_group.get(gid, [])
            ranks[(str(group["source"]), str(group["source_building_id"]))].append(int(group["group_rank"]))
            pixels = {str(row["frozen_pixel_sha256"]) for row in rows}
            unverified = sum(row["overlay_pixel_status"] != "exact_match" for row in rows)
            eligible = [row for row in rows if row["overlay_pixel_status"] == "exact_match"]
            rep = group["representative_source_asset_id"]
            expected_status = "serveable" if eligible else "unresolved"
            if (
                len(rows) != int(group["member_count"])
                or len(pixels) != int(group["exact_class_count"])
                or unverified != int(group["pixel_unverified_count"])
                or str(group["group_status"]) != expected_status
                or ((rep is None) != (not eligible))
                or (rep is not None and rep not in {row["source_asset_id"] for row in eligible})
            ):
                group_failures.append(gid)
            admissible_into = sum(
                edge["disposition"] == "admissible" and edge["center_effective_group_id"] == gid
                for edge in overlay_edges.values()
            )
            if int(group["direct_class_count"]) != admissible_into:
                group_failures.append("direct_count:" + gid)
            if len(pixels) > 1:
                rep_pixel = group["representative_pixel_sha256"]
                member_edge_pixels = {
                    str(base_asset_by_id[str(edge["member_source_asset_id"])]["normalized_pixel_sha256"])
                    for edge in overlay_edges.values()
                    if edge["disposition"] == "admissible" and edge["center_effective_group_id"] == gid
                }
                if pixels - {str(rep_pixel)} != member_edge_pixels:
                    group_failures.append("non_star:" + gid)
        for key, values in ranks.items():
            if sorted(values) != list(range(1, len(values) + 1)):
                group_failures.append("ranks:" + "/".join(key))
        _check(checks, "effective_group_invariants", [], group_failures)
        group_status_counts = Counter(str(row["group_status"]) for row in effective_groups)
        _check(checks, "effective_group_counts", {"serveable": 149, "unresolved": 17}, dict(group_status_counts))
        _check(checks, "split_group_delta", (162, 166, 4), (run["base_group_count"], run["effective_group_count"], run["group_delta"]))
        _check(checks, "group_formula", int(run["base_group_count"]) + int(run["split_edge_count"]), len(effective_groups))

        expected_drift = {key: row for key, row in base_by_key.items() if _normalize(row) != "exact_match"}
        drift_rows = [dict(row) for row in connection.execute("SELECT * FROM drift_assets WHERE overlay_run_id=?", (run_id,))]
        drift_by_key = {(row["source"], row["source_building_id"], row["source_asset_id"]): row for row in drift_rows}
        _check(checks, "drift_key_replay", set(expected_drift), set(drift_by_key))
        drift_failures: list[str] = []
        expected_patterns: Counter[tuple[str, str, str, str]] = Counter()
        for key, frozen in expected_drift.items():
            host, pattern, query = _url_pattern(frozen["fetch_url"])
            expected_patterns[(str(frozen["source"]), host, pattern, query)] += 1
            overlay = drift_by_key.get(key)
            if overlay is None:
                continue
            expected = (
                host, pattern, query, frozen["expected_pixel_sha256"], frozen["actual_pixel_sha256"],
                frozen["actual_raw_sha256"], frozen["http_status"], frozen["verification_record_sha256"],
            )
            actual = (
                overlay["fetch_host"], overlay["path_pattern"], overlay["query_keys"],
                overlay["expected_pixel_sha256"], overlay["actual_pixel_sha256"], overlay["actual_raw_sha256"],
                overlay["http_status"], overlay["v1_verification_record_sha256"],
            )
            if expected != actual:
                drift_failures.append("/".join(key))
        _check(checks, "drift_lineage_and_url_replay", [], drift_failures)
        actual_patterns = Counter({
            (str(row["source"]), str(row["fetch_host"]), str(row["path_pattern"]), str(row["query_keys"])): int(row["drift_count"])
            for row in connection.execute("SELECT * FROM drift_url_patterns WHERE overlay_run_id=?", (run_id,))
        })
        _check(checks, "drift_url_pattern_summary", expected_patterns, actual_patterns)
        source_summary = {str(row["source"]): dict(row) for row in connection.execute("SELECT * FROM drift_source_summary WHERE overlay_run_id=?", (run_id,))}
        source_failures: list[str] = []
        for source in sorted({key[0] for key in base_by_key}):
            total = sum(key[0] == source for key in base_by_key)
            drift = sum(key[0] == source for key in expected_drift)
            row = source_summary.get(source)
            if row is None or (row["asset_count"], row["exact_match_count"], row["pixel_unverified_count"], row["drift_rate"]) != (total, total - drift, drift, round(drift / total, 8)):
                source_failures.append(source)
        _check(checks, "drift_source_summary", [], source_failures)

        base_cache = {
            (str(row["normalized_pixel_sha256"]), str(row["transform_version"])): dict(row)
            for row in base.execute("SELECT * FROM derivative_cache WHERE run_id=?", (base_run["run_id"],))
        }
        pins = {
            (str(row["normalized_pixel_sha256"]), str(row["transform_version"])): dict(row)
            for row in connection.execute("SELECT * FROM cache_pins WHERE overlay_run_id=?", (run_id,))
        }
        _check(checks, "cache_key_replay", set(base_cache), set(pins))
        cache_failures: list[str] = []
        for key, frozen in base_cache.items():
            pin = pins.get(key)
            if pin is None:
                continue
            expected = (frozen["relative_path"], frozen["encoded_sha256"], frozen["encoded_bytes"], frozen["record_sha256"])
            actual = (pin["relative_path"], pin["encoded_sha256"], pin["encoded_bytes"], pin["base_cache_record_sha256"])
            cache_path = root / str(pin["relative_path"])
            if expected != actual or not cache_path.is_file() or cache_path.stat().st_size != int(pin["encoded_bytes"]) or _sha_file(cache_path) != pin["encoded_sha256"]:
                cache_failures.append("/".join(key))
        _check(checks, "cache_integrity", [], cache_failures)
        inventory = _canonical_sha([pins[key]["record_sha256"] for key in sorted(pins)])
        _check(checks, "cache_run_accounting", (151, 26909641, inventory), (run["cache_file_count"], run["cache_bytes"], run["cache_inventory_sha256"]))

        review_rows = [dict(row) for row in connection.execute("SELECT * FROM blind_review_items WHERE overlay_run_id=? ORDER BY sheet_order", (run_id,))]
        admissible_ids = {pair for pair, row in overlay_edges.items() if row["disposition"] == "admissible"}
        _check(checks, "blind_review_admissible_coverage", admissible_ids, {str(row["direct_pair_id"]) for row in review_rows})
        review_failures: list[str] = []
        for index, row in enumerate(review_rows, 1):
            page = root / str(row["sheet_path"])
            if (
                row["opaque_pair_id"] != f"B{index:03d}" or row["sheet_order"] != index
                or not page.is_file() or _sha_file(page) != row["sheet_sha256"]
                or row["left_pixel_sha256"] not in {key[0] for key in pins}
                or row["right_pixel_sha256"] not in {key[0] for key in pins}
            ):
                review_failures.append(str(row["opaque_pair_id"]))
        decisions = Counter(str(row["decision"]) for row in review_rows)
        _check(checks, "blind_review_artifact_integrity", [], review_failures)
        _check(checks, "blind_review_complete", (2, 2), (len(review_rows), sum(row["reviewer"] is not None and row["decision"] is not None for row in review_rows)))
        _check(checks, "false_collapse_zero", 0, decisions["distinct_false_collapse"])
        _check(checks, "unreviewable_zero", 0, decisions["unreviewable"])
        review_form = root / DEFAULT_REVIEW_FORM
        _check(checks, "review_form_sha", run["review_form_sha256"], _sha_file(review_form) if review_form.is_file() else None)

        record_failures = _validate_records(connection, run_id)
        _check(checks, "record_sha_replay", [], record_failures[:20], f"failures={len(record_failures)}")
        logical = _logical_sha(connection, run_id)
        _check(checks, "logical_sha_replay", run["logical_sha256"], logical)

        artifact_sha = _sha_file(artifact)
        if require_reports:
            manifest_path, report_path, markdown_path = root / DEFAULT_MANIFEST, root / DEFAULT_REPORT_JSON, root / DEFAULT_REPORT_MD
            _check(checks, "final_reports_exist", True, all(path.is_file() for path in (manifest_path, report_path, markdown_path)))
            if all(path.is_file() for path in (manifest_path, report_path, markdown_path)):
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                report = json.loads(report_path.read_text(encoding="utf-8"))
                _check(checks, "manifest_artifact_sha", artifact_sha, manifest.get("artifact", {}).get("sha256"))
                _check(checks, "manifest_status", "pass", manifest.get("status"))
                _check(checks, "manifest_base_sha", EXPECTED_BASE_SHA256, manifest.get("immutable_base", {}).get("sha256_after"))
                _check(checks, "manifest_contract_sha", EXPECTED_CONTRACT_SHA256, manifest.get("contract", {}).get("sha256"))
                _check(checks, "manifest_logical_sha", logical, manifest.get("logical_sha256"))
                _check(checks, "manifest_report_json_sha", _sha_file(report_path), manifest.get("reports", {}).get("json_sha256"))
                _check(checks, "manifest_report_md_sha", _sha_file(markdown_path), manifest.get("reports", {}).get("markdown_sha256"))
                _check(checks, "report_artifact_sha", artifact_sha, report.get("artifact", {}).get("sha256"))
                _check(checks, "report_zero_external_work", {"http": 0, "vision": 0, "llm": 0, "neon": 0, "r2": 0}, report.get("requests_and_writes"))

        metrics.update(
            {
                "asset_count": len(overlay_assets), "exact_match_count": status_counts["exact_match"],
                "pixel_unverified_count": status_counts["pixel_unverified"],
                "base_group_count": int(run["base_group_count"]), "effective_group_count": len(effective_groups),
                "serveable_group_count": group_status_counts["serveable"],
                "unresolved_group_count": group_status_counts["unresolved"],
                "admissible_pair_count": edge_counts["admissible"], "split_pair_count": edge_counts["split"],
                "blind_review_count": len(review_rows), "false_collapse_count": decisions["distinct_false_collapse"],
                "cache_file_count": len(pins), "artifact_sha256": artifact_sha, "logical_sha256": logical,
            }
        )
        return ValidationReport(artifact, run_id, status, tuple(checks), metrics)
    finally:
        if base is not None:
            base.close()
        connection.close()


__all__ = ["Check", "ValidationReport", "validate_overlay"]
