#!/usr/bin/env python
"""Read-only acceptance aggregates for the completed semantic Vision N10."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_image_selection import canonical_json, canonical_sha256  # noqa: E402
from canonical.cross_source_semantic_coverage import (  # noqa: E402
    SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
)


INSPECTOR_VERSION = "cross-source-semantic-vision-n10-inspector-v1.0.0"
DEFAULT_DB = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n10_v1.db"
)
DEFAULT_MANIFEST = Path(
    "data/reports/cross_source_semantic_coverage_n10_v1.json"
)
FIELD_NAMES = (
    "in_scope",
    "reject_reason",
    "medium",
    "spatial_context",
    "framing_scale",
    "camera_angle",
    "drawing_kind",
    "project_state",
    "project_legibility",
    "resolution_insufficient",
)


def _file_sha256(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_sidecars(path: Path) -> tuple[str, ...]:
    return tuple(
        str(candidate)
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    )


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _load_manifest(path: Path) -> tuple[dict[str, Any], bytes, str, str]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("semantic manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("semantic manifest must be an object")
    canonical = (canonical_json(payload) + "\n").encode("utf-8")
    if raw != canonical:
        raise ValueError("semantic manifest is not canonical JSON followed by LF")
    self_sha = payload.get("semantic_coverage_manifest_sha256")
    without_self = dict(payload)
    without_self.pop("semantic_coverage_manifest_sha256", None)
    replayed = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN, "manifest": without_self}
    )
    if self_sha != replayed:
        raise ValueError("semantic manifest self SHA mismatch")
    return payload, raw, hashlib.sha256(raw).hexdigest(), replayed


def _distribution(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _nearest_percentile(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return int(ordered[index])


def _latency(values: Sequence[int]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "sum_ms": 0,
            "mean_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
        }
    return {
        "count": len(values),
        "sum_ms": int(sum(values)),
        "mean_ms": round(sum(values) / len(values), 3),
        "p50_ms": _nearest_percentile(values, 0.50),
        "p95_ms": _nearest_percentile(values, 0.95),
        "max_ms": max(values),
    }


def _elapsed_seconds(started_at: str, completed_at: str) -> float:
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    return max(0.0, (completed - start).total_seconds())


def _manifest_origins(manifest_json: str) -> tuple[str, ...]:
    payload = json.loads(manifest_json)
    origins = payload.get("occurrence", {}).get("origins", [])
    if not isinstance(origins, list) or any(not isinstance(value, str) for value in origins):
        raise ValueError("selected occurrence origins are malformed")
    return tuple(origins)


def _building_name(manifest_json: str) -> str | None:
    payload = json.loads(manifest_json)
    value = (
        payload.get("selected_building", {})
        .get("building", {})
        .get("name")
    )
    return str(value) if value is not None else None


def _coverage_aggregates(
    connection: sqlite3.Connection,
    buildings: Sequence[sqlite3.Row],
    occurrences: Sequence[sqlite3.Row],
) -> dict[str, Any]:
    origins = {
        row["inference_id"]: _manifest_origins(row["manifest_json"])
        for row in occurrences
    }
    occurrence_slots: dict[str, set[str]] = defaultdict(set)
    slot_occurrences: Counter[str] = Counter()
    slot_buildings: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        """
        SELECT selection_id,slot,inference_id
        FROM coverage_slot_assignments
        WHERE state='observed'
        ORDER BY selection_id,slot,assignment_rank
        """
    ):
        occurrence_slots[str(row["inference_id"])].add(str(row["slot"]))
        slot_occurrences[str(row["slot"])] += 1
        slot_buildings[str(row["slot"])].add(str(row["selection_id"]))

    by_selection: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in occurrences:
        by_selection[str(row["selection_id"])].append(row)
    building_rows: list[dict[str, Any]] = []
    new_slot_distribution: Counter[str] = Counter()
    buildings_with_gain = 0
    for building in buildings:
        selection_id = str(building["selection_id"])
        members = by_selection[selection_id]
        p2_members = [
            row
            for row in members
            if any(
                origin.startswith("representative_p2_rank_")
                for origin in origins[row["inference_id"]]
            )
        ]
        p2_slots = set().union(
            *(occurrence_slots[row["inference_id"]] for row in p2_members)
        ) if p2_members else set()
        expanded_slots = set().union(
            *(occurrence_slots[row["inference_id"]] for row in members)
        ) if members else set()
        new_slots = expanded_slots - p2_slots
        if new_slots:
            buildings_with_gain += 1
            new_slot_distribution.update(new_slots)
        building_rows.append(
            {
                "building_rank": int(building["building_rank"]),
                "expanded_image_count": len(members),
                "expanded_slot_count": len(expanded_slots),
                "expanded_slots": sorted(expanded_slots),
                "name": _building_name(building["manifest_json"]),
                "new_slot_count": len(new_slots),
                "new_slots": sorted(new_slots),
                "p2_top3_image_count": len(p2_members),
                "p2_top3_slot_count": len(p2_slots),
                "p2_top3_slots": sorted(p2_slots),
                "population_stratum": str(building["population_stratum"]),
                "selection_id": selection_id,
                "source": str(building["source"]),
            }
        )
    return {
        "observed_slot_building_counts": {
            slot: len(values) for slot, values in sorted(slot_buildings.items())
        },
        "observed_slot_occurrence_counts": dict(sorted(slot_occurrences.items())),
        "p2_top3_vs_expanded": {
            "building_rows": building_rows,
            "buildings_with_new_slots": buildings_with_gain,
            "new_slot_building_distribution": dict(sorted(new_slot_distribution.items())),
            "total_buildings": len(building_rows),
            "total_new_building_slot_pairs": sum(
                row["new_slot_count"] for row in building_rows
            ),
        },
    }


def _anchor_aggregates(
    buildings: Sequence[sqlite3.Row],
    occurrences: Sequence[sqlite3.Row],
    results: Mapping[str, sqlite3.Row],
    hero_tiers: Mapping[str, str],
) -> dict[str, Any]:
    building_by_id = {str(row["selection_id"]): row for row in buildings}
    rows: list[dict[str, Any]] = []
    for occurrence in occurrences:
        origins = _manifest_origins(str(occurrence["manifest_json"]))
        building = building_by_id[str(occurrence["selection_id"])]
        if "coverage_anchor_p1_rank_1" not in origins or int(building["qa_fallback"]):
            continue
        inference_id = str(occurrence["inference_id"])
        result = results.get(inference_id)
        rows.append(
            {
                "evidence": str(result["evidence"]) if result is not None else None,
                "hero_tier": hero_tiers.get(inference_id, "missing"),
                "in_scope": bool(result["in_scope"]) if result is not None else None,
                "inference_id": inference_id,
                "project_legibility": (
                    str(result["project_legibility"])
                    if result is not None
                    else None
                ),
                "result_status": "present" if result is not None else "missing",
                "selection_id": str(occurrence["selection_id"]),
                "source": str(occurrence["source"]),
            }
        )
    return {
        "criterion": "coverage_anchor_p1_rank_1 on selected_buildings.qa_fallback=0",
        "count": len(rows),
        "in_scope_count": sum(row["in_scope"] is True for row in rows),
        "legibility_distribution": _distribution(
            (
                row["project_legibility"]
                if row["project_legibility"] is not None
                else "missing"
            )
            for row in rows
        ),
        "missing_hero_decision_count": sum(
            row["hero_tier"] == "missing" for row in rows
        ),
        "missing_result_count": sum(
            row["result_status"] == "missing" for row in rows
        ),
        "rows": rows,
    }


def inspect_n10(db_path: Path | str, manifest_path: Path | str) -> dict[str, Any]:
    db = Path(db_path).resolve()
    manifest = Path(manifest_path).resolve()
    if db == manifest:
        raise ValueError("DB and manifest paths must be distinct")
    if not db.is_file() or not manifest.is_file():
        raise FileNotFoundError(db if not db.is_file() else manifest)
    db_size_before = db.stat().st_size
    db_sha_before = _file_sha256(db)
    db_sidecars_before = _sqlite_sidecars(db)
    manifest_size_before = manifest.stat().st_size
    manifest_payload, manifest_raw, manifest_sha, manifest_self_sha = _load_manifest(
        manifest
    )
    connection = _open_immutable(db)
    try:
        run = connection.execute("SELECT * FROM semantic_runs").fetchone()
        if run is None:
            raise ValueError("semantic DB contains no run")
        if run["status"] not in {"complete", "complete_with_failures"}:
            raise ValueError(f"semantic DB is not terminal: {run['status']}")
        buildings = list(
            connection.execute("SELECT * FROM selected_buildings ORDER BY building_rank")
        )
        occurrences = list(
            connection.execute("SELECT * FROM selected_occurrences ORDER BY input_rank")
        )
        result_rows = list(
            connection.execute("SELECT * FROM semantic_results ORDER BY inference_id")
        )
        results = {str(row["inference_id"]): row for row in result_rows}
        hero_rows = list(
            connection.execute(
                "SELECT inference_id,tier FROM hero_candidate_decisions ORDER BY inference_id"
            )
        )
        hero_tiers = {str(row["inference_id"]): str(row["tier"]) for row in hero_rows}
        occurrence_ids = [str(row["inference_id"]) for row in occurrences]
        occurrence_id_set = set(occurrence_ids)
        missing_result_ids = sorted(occurrence_id_set - results.keys())
        missing_hero_ids = sorted(occurrence_id_set - hero_tiers.keys())
        orphan_result_ids = sorted(results.keys() - occurrence_id_set)
        orphan_hero_ids = sorted(hero_tiers.keys() - occurrence_id_set)
        field_distributions = {
            field: _distribution(
                results[inference_id][field]
                if inference_id in results
                else "missing"
                for inference_id in occurrence_ids
            )
            for field in FIELD_NAMES
        }
        uncertain_rows: list[dict[str, Any]] = []
        uncertain_axes: Counter[str] = Counter()
        for row in result_rows:
            axes = json.loads(row["uncertain_axes_json"])
            uncertain_axes.update(axes)
            if axes or int(row["resolution_insufficient"]):
                occurrence = next(
                    item
                    for item in occurrences
                    if item["inference_id"] == row["inference_id"]
                )
                uncertain_rows.append(
                    {
                        "evidence": str(row["evidence"]),
                        "inference_id": str(row["inference_id"]),
                        "medium": str(row["medium"]),
                        "project_legibility": str(row["project_legibility"]),
                        "resolution_insufficient": bool(row["resolution_insufficient"]),
                        "selection_id": str(occurrence["selection_id"]),
                        "source": str(occurrence["source"]),
                        "uncertain_axes": axes,
                    }
                )
        source_stratum = [
            {
                "building_count": int(row[2]),
                "occurrence_count": int(row[3]),
                "population_stratum": str(row[1]),
                "source": str(row[0]),
            }
            for row in connection.execute(
                """
                SELECT b.source,b.population_stratum,COUNT(DISTINCT b.selection_id),COUNT(o.inference_id)
                FROM selected_buildings b JOIN selected_occurrences o USING(run_id,selection_id)
                GROUP BY b.source,b.population_stratum
                ORDER BY b.source,b.population_stratum
                """
            )
        ]
        fetch_elapsed = [
            int(row[0])
            for row in connection.execute(
                "SELECT elapsed_ms FROM fetch_attempts ORDER BY inference_id,attempt_no"
            )
        ]
        vision_attempt_rows = list(
            connection.execute(
                """
                SELECT status,elapsed_ms,input_tokens,cached_input_tokens,output_tokens,
                       inference_ids_json
                FROM vision_attempts ORDER BY attempt_id
                """
            )
        )
        vision_elapsed = [int(row["elapsed_ms"]) for row in vision_attempt_rows]
        input_tokens = sum(int(row["input_tokens"] or 0) for row in vision_attempt_rows)
        cached_tokens = sum(
            int(row["cached_input_tokens"] or 0) for row in vision_attempt_rows
        )
        output_tokens = sum(int(row["output_tokens"] or 0) for row in vision_attempt_rows)
        downloaded_bytes = int(
            connection.execute(
                "SELECT COALESCE(SUM(response_bytes),0) FROM fetch_attempts"
            ).fetchone()[0]
        )
        wall_seconds = _elapsed_seconds(str(run["started_at"]), str(run["completed_at"]))
        runtime = {
            "batch_image_counts": [
                len(json.loads(row["inference_ids_json"])) for row in vision_attempt_rows
            ],
            "cached_input_tokens": cached_tokens,
            "downloaded_bytes": downloaded_bytes,
            "fetch_attempts": int(
                connection.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0]
            ),
            "fetch_latency": _latency(fetch_elapsed),
            "input_tokens": input_tokens,
            "model_calls": len(vision_attempt_rows),
            "output_tokens": output_tokens,
            "successful_model_calls": sum(
                row["status"] == "success" for row in vision_attempt_rows
            ),
            "total_tokens_excluding_cached_double_count": input_tokens + output_tokens,
            "vision_latency": _latency(vision_elapsed),
            "wall_elapsed_seconds": round(wall_seconds, 3),
        }
        factor = 10
        n100_projection = {
            "factor": factor,
            "method": "simple 10x empirical scaling from this fixed 10-building N10; no N100 selection has been made",
            "projected_buildings": 100,
            "projected_cached_input_tokens": cached_tokens * factor,
            "projected_downloaded_bytes": downloaded_bytes * factor,
            "projected_images": len(occurrences) * factor,
            "projected_input_tokens": input_tokens * factor,
            "projected_model_calls": len(vision_attempt_rows) * factor,
            "projected_output_tokens": output_tokens * factor,
            "projected_total_tokens_excluding_cached_double_count": (
                input_tokens + output_tokens
            )
            * factor,
            "projected_vision_elapsed_ms_serial": sum(vision_elapsed) * factor,
            "projected_wall_elapsed_seconds": round(wall_seconds * factor, 3),
        }
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        validation_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT validation_name,severity,passed,expected,actual,detail FROM validations ORDER BY validation_name"
            )
        ]
        manifest_checks = {
            "building_count": len(buildings) == int(run["building_count"]),
            "manifest_byte_sha256": manifest_sha == run["manifest_byte_sha256"],
            "manifest_self_sha256": manifest_self_sha == run["manifest_self_sha256"],
            "occurrence_count": len(occurrences) == int(run["occurrence_count"]),
            "ordered_building_manifest_sha256": manifest_payload.get(
                "ordered_building_manifest_sha256"
            )
            == run["ordered_building_manifest_sha256"],
            "ordered_occurrence_manifest_sha256": manifest_payload.get(
                "ordered_occurrence_manifest_sha256"
            )
            == run["ordered_occurrence_manifest_sha256"],
        }
        if not all(manifest_checks.values()):
            raise ValueError(f"DB/manifest lineage mismatch: {manifest_checks}")
        body: dict[str, Any] = {
            "accounting": {
                "buildings": len(buildings),
                "missing_hero_decisions": len(missing_hero_ids),
                "missing_semantic_results": len(missing_result_ids),
                "occurrences": len(occurrences),
                "orphan_hero_decisions": len(orphan_hero_ids),
                "orphan_semantic_results": len(orphan_result_ids),
                "results": len(result_rows),
                "successful_inputs": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM vision_inputs WHERE status='success'"
                    ).fetchone()[0]
                ),
            },
            "authoritative": False,
            "coverage": _coverage_aggregates(connection, buildings, occurrences),
            "field_distributions": field_distributions,
            "hero": {
                "tier_distribution": _distribution(
                    hero_tiers.get(inference_id, "missing")
                    for inference_id in occurrence_ids
                ),
                "tiers_by_source": {
                    source: _distribution(
                        hero_tiers.get(str(row["inference_id"]), "missing")
                        for row in occurrences
                        if row["source"] == source
                    )
                    for source in sorted({str(row["source"]) for row in occurrences})
                },
            },
            "inputs": {
                "db": {
                    "logical_sha256": str(run["logical_sha256"]),
                    "path": str(db),
                    "sha256_before": db_sha_before,
                    "sidecars_before": list(db_sidecars_before),
                    "size_bytes_before": db_size_before,
                },
                "manifest": {
                    "path": str(manifest),
                    "self_sha256": manifest_self_sha,
                    "sha256": manifest_sha,
                    "size_bytes": manifest_size_before,
                },
            },
            "inspector_version": INSPECTOR_VERSION,
            "n100_projection": n100_projection,
            "missing": {
                "hero_decision_inference_ids": missing_hero_ids,
                "orphan_hero_decision_inference_ids": orphan_hero_ids,
                "orphan_semantic_result_inference_ids": orphan_result_ids,
                "semantic_result_inference_ids": missing_result_ids,
            },
            "non_qa_p1_anchor": _anchor_aggregates(
                buildings, occurrences, results, hero_tiers
            ),
            "operations": {
                "db_writes": 0,
                "llm_requests": 0,
                "network_requests": 0,
                "vision_requests": 0,
            },
            "run": {
                "completed_at": str(run["completed_at"]),
                "contract_version": str(run["contract_version"]),
                "model": str(run["model"]),
                "prompt_version": str(run["prompt_version"]),
                "run_id": str(run["run_id"]),
                "status": str(run["status"]),
            },
            "runtime": runtime,
            "source_stratum": source_stratum,
            "uncertainty": {
                "axis_distribution": dict(sorted(uncertain_axes.items())),
                "resolution_insufficient_count": sum(
                    bool(row["resolution_insufficient"]) for row in result_rows
                ),
                "rows": uncertain_rows,
                "rows_with_uncertainty_or_resolution_issue": len(uncertain_rows),
            },
            "validation": {
                "db_manifest_checks": manifest_checks,
                "foreign_key_violations": foreign_keys,
                "integrity_check": integrity,
                "quick_check": quick,
                "stored_rows": validation_rows,
            },
        }
    finally:
        connection.close()
    db_size_after = db.stat().st_size
    db_sha_after = _file_sha256(db)
    db_sidecars_after = _sqlite_sidecars(db)
    manifest_size_after = manifest.stat().st_size
    manifest_sha_after = _file_sha256(manifest)
    body["inputs"]["db"].update(
        {
            "sha256_after": db_sha_after,
            "sidecars_after": list(db_sidecars_after),
            "size_bytes_after": db_size_after,
        }
    )
    body["inputs"]["manifest"].update(
        {"sha256_after": manifest_sha_after, "size_bytes_after": manifest_size_after}
    )
    unchanged = (
        db_size_before == db_size_after
        and db_sha_before == db_sha_after
        and db_sidecars_before == db_sidecars_after
        and not db_sidecars_after
        and manifest_size_before == manifest_size_after
        and manifest_sha == manifest_sha_after
    )
    body["validation"]["inputs_unchanged_and_no_db_sidecars"] = unchanged
    if not unchanged:
        raise ValueError("DB or manifest changed during read-only inspection")
    body["inspection_sha256"] = canonical_sha256(
        {"domain": INSPECTOR_VERSION, "report": body}
    )
    return body


def _write_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    output = path.resolve()
    if not output.parent.is_dir():
        raise FileNotFoundError(f"output parent does not exist: {output.parent}")
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only aggregate inspection of the completed semantic Vision N10."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional exclusive-create JSON output; an existing file is never overwritten.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = inspect_n10(args.db, args.manifest)
        if args.output_json is not None:
            output = args.output_json.resolve()
            if output in {args.db.resolve(), args.manifest.resolve()}:
                raise ValueError("output JSON must be distinct from DB and manifest")
            _write_no_clobber(output, payload)
    except Exception as exc:
        print(
            json.dumps(
                {"error": str(exc), "error_type": type(exc).__name__, "status": "error"},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
