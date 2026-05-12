#!/usr/bin/env python3
"""Prepare downstream refresh artifacts after approved code-name splits.

This tool does not call LLMs and does not mutate the primary D/E result files.
It removes stale rows for touched split CIDs in patched JSONL outputs, recomputes
E-1 deterministically for the affected CIDs, and marks D-1/E-2/D-2 as pending.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.image_dedup import fetch_image_metadata  # noqa: E402
from tools.e1_phash_dedup import process_cluster  # noqa: E402
from tools.image_dedup_5type import (  # noqa: E402
    DEFAULT_PHASH_CACHE_PATH,
    SourceImageSpec,
    _load_phash_cache,
    load_source_image_index,
)

DEFAULT_CANONICAL = ROOT / "data/canonical/canonical_buildings_4source.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_code_name_split_apply_report.json"
DEFAULT_OUTPUT_DIR = ROOT / "data/canonical/code_name_split_refresh"

DEFAULT_STAGE_PATHS = {
    "d1": ROOT / "data/canonical/d1_results.jsonl",
    "e1": ROOT / "data/canonical/e1_clusters.jsonl",
    "e2": ROOT / "data/canonical/e2_image_types.jsonl",
    "d2": ROOT / "data/canonical/d2_results.jsonl",
}


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=isinstance(data, dict))
        f.write("\n")


def _row_cid(row: dict[str, Any]) -> str:
    return str(row.get("cid") or row.get("canonical_bld_id") or "")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def affected_cids_from_report(report: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    touched = sorted({str(cid) for cid in report.get("touched_existing") or [] if str(cid)})
    created = sorted({str(cid) for cid in report.get("created") or [] if str(cid)})
    affected = sorted(set(touched) | set(created))
    if not affected:
        raise ValueError("split apply report has no touched_existing/created cids")
    return affected, touched, created


def canonical_subset(canonical: dict[str, Any], cids: set[str]) -> list[dict[str, Any]]:
    rows = canonical.get("clusters") or canonical.get("buildings") or []
    subset = [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("canonical_bld_id") or "") in cids
    ]
    found = {str(row.get("canonical_bld_id")) for row in subset}
    missing = sorted(cids - found)
    if missing:
        raise ValueError(f"affected cids missing from canonical: {missing[:10]}")
    return sorted(subset, key=lambda row: str(row.get("canonical_bld_id")))


def recompute_e1_rows(
    clusters: list[dict[str, Any]],
    *,
    phash_cache_path: Path = DEFAULT_PHASH_CACHE_PATH,
    source_specs: Optional[dict[str, SourceImageSpec]] = None,
    fetcher: Callable[..., Optional[dict]] = fetch_image_metadata,
) -> list[dict[str, Any]]:
    source_index = load_source_image_index(
        source_specs=source_specs,
        phash_cache=_load_phash_cache(phash_cache_path),
    )
    rows = [
        process_cluster(cluster, source_index=source_index, fetcher=fetcher)
        for cluster in clusters
    ]
    return sorted(rows, key=lambda row: str(row.get("cid")))


def patch_stage_jsonl(
    input_path: Path,
    output_path: Path,
    *,
    affected_cids: set[str],
    replacement_rows: Optional[list[dict[str, Any]]] = None,
) -> dict[str, int]:
    original = read_jsonl(input_path)
    kept = [row for row in original if _row_cid(row) not in affected_cids]
    replacements = replacement_rows or []
    merged = kept + replacements
    write_jsonl(output_path, merged)
    return {
        "input_rows": len(original),
        "dropped_stale_rows": len(original) - len(kept),
        "replacement_rows": len(replacements),
        "output_rows": len(merged),
    }


def prepare_refresh(
    *,
    split_report_path: Path = DEFAULT_REPORT,
    canonical_path: Path = DEFAULT_CANONICAL,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    stage_paths: Optional[dict[str, Path]] = None,
    phash_cache_path: Path = DEFAULT_PHASH_CACHE_PATH,
    source_specs: Optional[dict[str, SourceImageSpec]] = None,
    fetcher: Callable[..., Optional[dict]] = fetch_image_metadata,
) -> dict[str, Any]:
    split_report = _read_json(split_report_path)
    affected, touched, created = affected_cids_from_report(split_report)
    affected_set = set(affected)
    canonical = _read_json(canonical_path)
    clusters = canonical_subset(canonical, affected_set)

    output_dir.mkdir(parents=True, exist_ok=True)
    affected_payload = {
        "affected_cids": affected,
        "touched_existing": touched,
        "created": created,
    }
    _write_json(output_dir / "affected_cids.json", affected_payload)
    _write_json(
        output_dir / "canonical_subset.json",
        {
            "summary": {
                "n_affected": len(affected),
                "touched_existing": len(touched),
                "created": len(created),
            },
            "clusters": clusters,
        },
    )

    e1_rows = recompute_e1_rows(
        clusters,
        phash_cache_path=phash_cache_path,
        source_specs=source_specs,
        fetcher=fetcher,
    )
    write_jsonl(output_dir / "e1_clusters.affected.jsonl", e1_rows)

    paths = dict(DEFAULT_STAGE_PATHS)
    if stage_paths:
        paths.update(stage_paths)
    patched = {
        "d1": output_dir / "d1_results.patched.jsonl",
        "e1": output_dir / "e1_clusters.patched.jsonl",
        "e2": output_dir / "e2_image_types.patched.jsonl",
        "d2": output_dir / "d2_results.patched.jsonl",
    }

    patch_reports = {
        "d1": patch_stage_jsonl(paths["d1"], patched["d1"], affected_cids=affected_set),
        "e1": patch_stage_jsonl(paths["e1"], patched["e1"], affected_cids=affected_set, replacement_rows=e1_rows),
        "e2": patch_stage_jsonl(paths["e2"], patched["e2"], affected_cids=affected_set),
        "d2": patch_stage_jsonl(paths["d2"], patched["d2"], affected_cids=affected_set),
    }

    report = {
        "status": "READY_FOR_D1_E2_D2_REFRESH",
        "split_report": str(split_report_path),
        "canonical": str(canonical_path),
        "output_dir": str(output_dir),
        "affected_count": len(affected),
        "touched_existing_count": len(touched),
        "created_count": len(created),
        "e1_recomputed_rows": len(e1_rows),
        "e1_recomputed_images": sum(len(row.get("all_images") or []) for row in e1_rows),
        "patched_outputs": {stage: str(path) for stage, path in patched.items()},
        "patch_reports": patch_reports,
        "pending_llm_or_vision": {
            "d1": affected,
            "e2": affected,
            "d2": affected,
        },
        "strict_build_command": (
            "python3 tools/build_strict_canonical.py "
            f"--canonical {canonical_path} "
            f"--d1 {patched['d1']} --e1 {patched['e1']} "
            f"--e2 {patched['e2']} --d2 {patched['d2']} "
            f"--output {output_dir / 'canonical_buildings_strict.patched.json'}"
        ),
    }
    _write_json(output_dir / "refresh_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare code-name split downstream refresh artifacts.")
    parser.add_argument("--split-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--phash-cache", type=Path, default=DEFAULT_PHASH_CACHE_PATH)
    parser.add_argument("--d1", type=Path, default=DEFAULT_STAGE_PATHS["d1"])
    parser.add_argument("--e1", type=Path, default=DEFAULT_STAGE_PATHS["e1"])
    parser.add_argument("--e2", type=Path, default=DEFAULT_STAGE_PATHS["e2"])
    parser.add_argument("--d2", type=Path, default=DEFAULT_STAGE_PATHS["d2"])
    args = parser.parse_args()

    report = prepare_refresh(
        split_report_path=args.split_report,
        canonical_path=args.canonical,
        output_dir=args.output_dir,
        stage_paths={"d1": args.d1, "e1": args.e1, "e2": args.e2, "d2": args.d2},
        phash_cache_path=args.phash_cache,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "affected_count": report["affected_count"],
                "e1_recomputed_rows": report["e1_recomputed_rows"],
                "e1_recomputed_images": report["e1_recomputed_images"],
                "output_dir": report["output_dir"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
