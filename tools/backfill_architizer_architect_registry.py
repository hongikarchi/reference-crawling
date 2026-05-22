#!/usr/bin/env python3
"""Append missing Architizer firm refs to the architect registry.

This repairs the case where architizer_projects has a firm_slug/firm_name, but
data/id_registry_architects.json and architects_canonical.json were generated
before those firms existed in the source DB.

The script is deterministic:
  - if firm_name exactly matches an existing active registry name, append the
    Architizer source ref to that canonical architect
  - otherwise create one new singleton architect canonical for that firm_slug
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.match_architects_sequential import export_clusters
from canonical.registry import ArchitectRegistry, _normalize_name

DEFAULT_STRICT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.resume10_complete.json"
DEFAULT_ARCHITECTS = ROOT / "data/canonical/architects_canonical.json"
DEFAULT_REGISTRY = ROOT / "data/id_registry_architects.json"
DEFAULT_ARCHITIZER_DB = ROOT / "data/crawl/architizer.db"
DEFAULT_REPORT = ROOT / "data/reports/architizer_architect_registry_backfill.json"


def _load_arch_source_index(path: Path) -> set[tuple[str, str]]:
    data = json.load(path.open(encoding="utf-8"))
    out: set[tuple[str, str]] = set()
    for cluster in data.get("clusters") or []:
        for source, ids in (cluster.get("source_refs") or {}).items():
            for sid in ids or []:
                out.add((str(source), str(sid)))
    return out


def _missing_architizer_firms(
    *,
    strict_path: Path,
    architects_path: Path,
    architizer_db: Path,
) -> list[dict[str, Any]]:
    source_index = _load_arch_source_index(architects_path)
    strict = json.load(strict_path.open(encoding="utf-8"))
    rows = strict.get("buildings") or []

    conn = sqlite3.connect(architizer_db)
    conn.row_factory = sqlite3.Row
    try:
        by_slug: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.get("architect_canonical_ids"):
                continue
            for project_id in (row.get("source_refs") or {}).get("architizer", []) or []:
                project = conn.execute(
                    "SELECT firm_slug, firm_name FROM architizer_projects WHERE id=?",
                    (str(project_id),),
                ).fetchone()
                if not project or not project["firm_slug"] or not project["firm_name"]:
                    continue
                slug = str(project["firm_slug"])
                if ("architizer", slug) in source_index:
                    continue
                item = by_slug.setdefault(
                    slug,
                    {
                        "firm_slug": slug,
                        "firm_name": str(project["firm_name"]),
                        "building_refs": [],
                    },
                )
                item["building_refs"].append(
                    {
                        "canonical_bld_id": row.get("canonical_bld_id"),
                        "building_name": row.get("name"),
                        "architizer_project_id": str(project_id),
                    }
                )
        return sorted(by_slug.values(), key=lambda item: item["firm_slug"])
    finally:
        conn.close()


def _backup(paths: list[Path]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "data/backups" / f"architizer_architect_registry_backfill_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)
    for path in paths:
        shutil.copy2(path, out_dir / path.name)
    return out_dir


def run(
    *,
    strict_path: Path,
    architects_path: Path,
    registry_path: Path,
    architizer_db: Path,
    report_path: Path,
    apply: bool,
) -> dict[str, Any]:
    candidates = _missing_architizer_firms(
        strict_path=strict_path,
        architects_path=architects_path,
        architizer_db=architizer_db,
    )
    registry = ArchitectRegistry(path=str(registry_path))
    name_index = {
        _normalize_name(name): cid
        for cid, entry in registry.data.items()
        if not entry.get("redirected_to")
        for name in entry.get("names", [])
    }

    planned: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for item in candidates:
        name = item["firm_name"]
        slug = item["firm_slug"]
        exact_match = name_index.get(_normalize_name(name))
        action = "append_to_existing_name_match" if exact_match else "create_singleton"
        counts[action] += 1
        planned.append({**item, "action": action, "target_canonical_arch_id": exact_match})

    backup_dir = None
    if apply and planned:
        backup_dir = _backup([registry_path, architects_path])
        for item in planned:
            names = {item["firm_name"]}
            source_refs = {"architizer": [item["firm_slug"]]}
            cid, _status = registry.match_or_create(names=names, source_refs=source_refs)
            registry.append(cid, names=names, source_refs=source_refs)
            item["target_canonical_arch_id"] = cid
        registry.save()
        export_clusters(registry, str(architects_path))

    report = {
        "applied": apply,
        "backup_dir": str(backup_dir) if backup_dir else None,
        "candidate_firms": len(candidates),
        "affected_buildings": sum(len(item["building_refs"]) for item in candidates),
        "actions": dict(counts),
        "candidates": planned,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill missing Architizer architect registry refs.")
    parser.add_argument("--strict", type=Path, default=DEFAULT_STRICT)
    parser.add_argument("--architects", type=Path, default=DEFAULT_ARCHITECTS)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--architizer-db", type=Path, default=DEFAULT_ARCHITIZER_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    report = run(
        strict_path=args.strict,
        architects_path=args.architects,
        registry_path=args.registry,
        architizer_db=args.architizer_db,
        report_path=args.report,
        apply=args.apply,
    )
    print(json.dumps({k: v for k, v in report.items() if k != "candidates"}, ensure_ascii=False, indent=2))
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
