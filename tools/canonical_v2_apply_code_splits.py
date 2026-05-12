#!/usr/bin/env python3
"""Apply approved code-name split preview to registry + Stage B canonical.

Default mode is dry-run. Use `--apply` to write files. The tool always validates
source_ref preservation before writing and writes timestamped backups.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "data/id_registry_buildings.json"
DEFAULT_CANONICAL = ROOT / "data/canonical/canonical_buildings_4source.json"
DEFAULT_PREVIEW = ROOT / "data/reports/canonical_v2_code_name_split_preview.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_code_name_split_apply_report.json"


def _flat_refs(source_refs: dict[str, list[str]]) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for source, ids in (source_refs or {}).items():
        for source_id in ids or []:
            refs.add((str(source), str(source_id)))
    return refs


def _normalize_refs(source_refs: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for source, ids in (source_refs or {}).items():
        clean = sorted({str(source_id) for source_id in ids or [] if str(source_id)})
        if clean:
            out[str(source)] = clean
    return dict(sorted(out.items()))


def _member_count(source_refs: dict[str, list[str]]) -> int:
    return sum(len(ids) for ids in (source_refs or {}).values())


def _cluster_from_payload(payload: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    source_refs = _normalize_refs(payload.get("source_refs") or {})
    names = sorted({str(name) for name in payload.get("names") or [] if str(name)})
    first_name = names[0] if names else payload.get("group_name")
    return {
        "canonical_bld_id": payload["canonical_bld_id"],
        "canonical_name": first_name,
        "names": names,
        "source_refs": source_refs,
        "first_seen": template.get("first_seen"),
        "last_seen": template.get("last_seen"),
        "n_members": _member_count(source_refs),
        "n_sources": len(source_refs),
    }


def _registry_entry_from_payload(payload: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    return {
        "names": sorted({str(name) for name in payload.get("names") or [] if str(name)}),
        "source_refs": _normalize_refs(payload.get("source_refs") or {}),
        "first_seen": template.get("first_seen"),
        "last_seen": template.get("last_seen"),
        "redirected_to": None,
    }


def _summary(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    active = [c for c in clusters if c.get("canonical_bld_id")]
    by_n_sources = Counter(str(c.get("n_sources") or len(c.get("source_refs") or {})) for c in active)
    return {
        "n_canonicals": len(active),
        "multi_source": sum(1 for c in active if int(c.get("n_sources") or 0) > 1),
        "by_n_sources": dict(sorted(by_n_sources.items(), key=lambda item: int(item[0]))),
    }


def _source_ref_counts(registry: dict[str, dict]) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for entry in registry.values():
        if entry.get("redirected_to"):
            continue
        for ref in _flat_refs(entry.get("source_refs") or {}):
            counts[ref] += 1
    return counts


def apply_preview(
    registry: dict[str, dict],
    canonical: dict[str, Any],
    preview: dict[str, Any],
) -> tuple[dict[str, dict], dict[str, Any], dict[str, Any]]:
    out_registry = copy.deepcopy(registry)
    out_canonical = copy.deepcopy(canonical)
    clusters = out_canonical.get("clusters")
    if not isinstance(clusters, list):
        raise ValueError("canonical must contain clusters list")

    cluster_by_id = {cluster.get("canonical_bld_id"): idx for idx, cluster in enumerate(clusters)}
    inserted: list[dict[str, Any]] = []
    touched_existing: list[str] = []
    created: list[str] = []

    for split in preview.get("splits") or []:
        cid = split.get("canonical_bld_id")
        if cid not in out_registry:
            raise ValueError(f"{cid}: registry entry missing")
        if cid not in cluster_by_id:
            raise ValueError(f"{cid}: canonical cluster missing")

        keep = split.get("keep")
        if not keep or keep.get("canonical_bld_id") != cid:
            raise ValueError(f"{cid}: invalid keep payload")

        original_refs = _flat_refs(out_registry[cid].get("source_refs") or {})
        produced_refs = _flat_refs(keep.get("source_refs") or {})
        for payload in split.get("create") or []:
            new_id = payload.get("canonical_bld_id")
            if not new_id:
                raise ValueError(f"{cid}: create payload missing canonical_bld_id")
            if new_id in out_registry:
                raise ValueError(f"{new_id}: already exists in registry")
            produced_refs |= _flat_refs(payload.get("source_refs") or {})

        if original_refs != produced_refs:
            raise ValueError(f"{cid}: source_refs mismatch during apply")

        reg_template = out_registry[cid]
        cluster_template = clusters[cluster_by_id[cid]]
        out_registry[cid] = _registry_entry_from_payload(keep, reg_template)
        clusters[cluster_by_id[cid]] = _cluster_from_payload(keep, cluster_template)
        touched_existing.append(cid)

        for payload in split.get("create") or []:
            new_id = payload["canonical_bld_id"]
            out_registry[new_id] = _registry_entry_from_payload(payload, reg_template)
            new_cluster = _cluster_from_payload(payload, cluster_template)
            inserted.append(new_cluster)
            created.append(new_id)

    clusters.extend(inserted)
    clusters.sort(key=lambda cluster: cluster.get("canonical_bld_id") or "")
    out_canonical["summary"] = _summary(clusters)

    duplicate_refs = [ref for ref, count in _source_ref_counts(out_registry).items() if count > 1]
    if duplicate_refs:
        raise ValueError(f"duplicate source_refs after apply: {duplicate_refs[:10]}")

    report = {
        "status": "APPLIED",
        "touched_existing": touched_existing,
        "created": created,
        "created_count": len(created),
        "canonical_summary": out_canonical["summary"],
        "source_refs_lost": 0,
        "source_refs_duplicated": 0,
    }
    return out_registry, out_canonical, report


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=isinstance(data, dict))


def _backup(paths: list[Path]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "data/backups" / f"code_name_split_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in paths:
        shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply code-name split preview")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--preview", type=Path, default=DEFAULT_PREVIEW)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    registry = json.load(args.registry.open())
    canonical = json.load(args.canonical.open())
    preview = json.load(args.preview.open())
    out_registry, out_canonical, report = apply_preview(registry, canonical, preview)
    report.update(
        {
            "registry": str(args.registry),
            "canonical": str(args.canonical),
            "preview": str(args.preview),
            "mode": "apply" if args.apply else "dry-run",
        }
    )

    if args.apply:
        backup_dir = _backup([args.registry, args.canonical])
        _write_json(args.registry, out_registry)
        _write_json(args.canonical, out_canonical)
        report["backup_dir"] = str(backup_dir)

    _write_json(args.report, report)
    print(json.dumps({k: report[k] for k in ("status", "mode", "created_count", "canonical_summary")}, indent=2, ensure_ascii=False))
    print(f"report: {args.report}")
    if args.apply:
        print(f"backup: {report['backup_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
