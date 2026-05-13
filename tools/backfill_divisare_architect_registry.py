#!/usr/bin/env python3
"""Append missing Divisare author refs from project pages to architect registry."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.match_architects_sequential import export_clusters
from canonical.registry import ArchitectRegistry, _normalize_name


DEFAULT_ARCHITECTS = ROOT / "data/canonical/architects_canonical.json"
DEFAULT_REGISTRY = ROOT / "data/id_registry_architects.json"
DEFAULT_DIVISARE_DB = ROOT / "data/crawl/divisare.db"
DEFAULT_REPORT = ROOT / "data/reports/divisare_architect_registry_backfill.json"


def _parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _load_arch_source_index(path: Path) -> set[tuple[str, str]]:
    data = json.load(path.open(encoding="utf-8"))
    out: set[tuple[str, str]] = set()
    for cluster in data.get("clusters") or []:
        for source, ids in (cluster.get("source_refs") or {}).items():
            for sid in ids or []:
                out.add((str(source), str(sid)))
    return out


def _candidate_architects(divisare_db: Path, architects_path: Path) -> list[dict[str, Any]]:
    source_index = _load_arch_source_index(architects_path)
    conn = sqlite3.connect(divisare_db)
    conn.row_factory = sqlite3.Row
    try:
        by_id: dict[str, dict[str, Any]] = {}
        project_refs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in conn.execute(
            "SELECT id, slug, name, architect_ids, architect_names "
            "FROM divisare_projects "
            "WHERE architect_ids IS NOT NULL AND architect_ids != '' AND architect_ids != '[]'"
        ):
            ids = [str(v) for v in _parse_json_list(row["architect_ids"]) if str(v)]
            names = [str(v).strip() for v in _parse_json_list(row["architect_names"]) if str(v).strip()]
            for index, aid in enumerate(ids):
                if ("divisare", aid) in source_index:
                    continue
                name = names[index] if index < len(names) else None
                if not name:
                    continue
                by_id.setdefault(aid, {"source_id": aid, "name": name})
                project_refs[aid].append(
                    {
                        "project_id": str(row["id"]),
                        "project_name": row["name"],
                        "project_slug": row["slug"],
                    }
                )
        return [
            {**item, "project_refs": project_refs[item["source_id"]]}
            for item in sorted(by_id.values(), key=lambda item: item["source_id"])
        ]
    finally:
        conn.close()


def _backup(paths: list[Path]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "data/backups" / f"divisare_architect_registry_backfill_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)
    for path in paths:
        shutil.copy2(path, out_dir / path.name)
    return out_dir


def run(
    *,
    divisare_db: Path,
    architects_path: Path,
    registry_path: Path,
    report_path: Path,
    apply: bool,
) -> dict[str, Any]:
    candidates = _candidate_architects(divisare_db, architects_path)
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
        exact_match = name_index.get(_normalize_name(item["name"]))
        action = "append_to_existing_name_match" if exact_match else "create_singleton"
        counts[action] += 1
        planned.append({**item, "action": action, "target_canonical_arch_id": exact_match})

    backup_dir = None
    if apply and planned:
        backup_dir = _backup([registry_path, architects_path])
        for item in planned:
            names = {item["name"]}
            source_refs = {"divisare": [item["source_id"]]}
            cid, _status = registry.match_or_create(names=names, source_refs=source_refs)
            registry.append(cid, names=names, source_refs=source_refs)
            item["target_canonical_arch_id"] = cid
        registry.save()
        export_clusters(registry, str(architects_path))

    report = {
        "applied": apply,
        "backup_dir": str(backup_dir) if backup_dir else None,
        "candidate_architects": len(candidates),
        "affected_project_refs": sum(len(item["project_refs"]) for item in candidates),
        "actions": dict(counts),
        "candidates": planned,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill missing Divisare architect registry refs.")
    parser.add_argument("--divisare-db", type=Path, default=DEFAULT_DIVISARE_DB)
    parser.add_argument("--architects", type=Path, default=DEFAULT_ARCHITECTS)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    report = run(
        divisare_db=args.divisare_db,
        architects_path=args.architects,
        registry_path=args.registry,
        report_path=args.report,
        apply=args.apply,
    )
    print(json.dumps({k: v for k, v in report.items() if k != "candidates"}, ensure_ascii=False, indent=2))
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
