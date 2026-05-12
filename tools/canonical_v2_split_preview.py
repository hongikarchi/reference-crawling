#!/usr/bin/env python3
"""Build a non-mutating split preview for code-name overmerge candidates."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "data/id_registry_buildings.json"
DEFAULT_CANDIDATES = ROOT / "data/reports/canonical_v2_code_name_split_candidates.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_code_name_split_preview.json"


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _next_id(registry: dict[str, dict], used: set[str]) -> str:
    max_n = -1
    for cid in list(registry) + list(used):
        try:
            max_n = max(max_n, int(str(cid).split("_")[-1]))
        except (TypeError, ValueError):
            continue
    return f"bld_{max_n + 1:06d}"


def _source_refs_from_members(members: list[dict[str, Any]]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for member in members:
        source = str(member.get("source") or "")
        source_id = str(member.get("source_id") or "")
        if not source or not source_id:
            continue
        refs.setdefault(source, [])
        if source_id not in refs[source]:
            refs[source].append(source_id)
    return {source: sorted(ids) for source, ids in sorted(refs.items())}


def _names_from_members(members: list[dict[str, Any]]) -> list[str]:
    names = []
    for member in members:
        name = str(member.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return sorted(names)


def _flat_refs(source_refs: dict[str, list[str]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for source, ids in (source_refs or {}).items():
        for source_id in ids or []:
            out.add((str(source), str(source_id)))
    return out


def _choose_keep_group(candidate: dict[str, Any]) -> int:
    groups = candidate.get("split_groups") or []
    current_name = _norm(candidate.get("current_name"))
    for idx, group in enumerate(groups):
        if _norm(group.get("group_name")) == current_name:
            return idx
    sizes = [len(group.get("members") or []) for group in groups]
    return max(range(len(groups)), key=lambda idx: sizes[idx]) if groups else -1


def build_preview(registry: dict[str, dict], candidates: dict[str, Any]) -> dict[str, Any]:
    used_new_ids: set[str] = set()
    splits: list[dict[str, Any]] = []
    total_lost = 0
    total_duplicated = 0
    problems: list[str] = []

    for candidate in candidates.get("candidates") or []:
        cid = str(candidate.get("canonical_bld_id") or "")
        original = registry.get(cid)
        if not cid or not original:
            problems.append(f"{cid or '<missing>'}: original registry entry not found")
            continue
        groups = candidate.get("split_groups") or []
        if len(groups) < 2:
            problems.append(f"{cid}: fewer than two split groups")
            continue

        original_refs = _flat_refs(original.get("source_refs") or {})
        produced_refs: list[tuple[str, str]] = []
        keep_idx = _choose_keep_group(candidate)
        keep_payload: dict[str, Any] | None = None
        create_payloads: list[dict[str, Any]] = []

        for idx, group in enumerate(groups):
            members = group.get("members") or []
            source_refs = _source_refs_from_members(members)
            refs_flat = _flat_refs(source_refs)
            produced_refs.extend(sorted(refs_flat))
            payload = {
                "group_name": group.get("group_name"),
                "names": _names_from_members(members),
                "source_refs": source_refs,
                "n_source_refs": len(refs_flat),
            }
            if idx == keep_idx:
                keep_payload = {"canonical_bld_id": cid, **payload}
            else:
                new_id = _next_id(registry, used_new_ids)
                used_new_ids.add(new_id)
                create_payloads.append({"canonical_bld_id": new_id, **payload})

        produced_counts = Counter(produced_refs)
        produced_set = set(produced_refs)
        lost = sorted(original_refs - produced_set)
        extra = sorted(produced_set - original_refs)
        duplicated = sorted(ref for ref, count in produced_counts.items() if count > 1)
        total_lost += len(lost)
        total_duplicated += len(duplicated)
        if extra:
            problems.append(f"{cid}: preview includes refs not in original: {extra[:5]}")

        splits.append(
            {
                "canonical_bld_id": cid,
                "current_name": candidate.get("current_name"),
                "original_names": original.get("names") or [],
                "original_source_refs": original.get("source_refs") or {},
                "keep": keep_payload,
                "create": create_payloads,
                "validation": {
                    "source_refs_original": len(original_refs),
                    "source_refs_produced": len(produced_set),
                    "source_refs_lost": lost,
                    "source_refs_duplicated": duplicated,
                    "source_refs_extra": extra,
                },
            }
        )

    status = "READY"
    if problems or total_lost or total_duplicated:
        status = "NEEDS_REVIEW"

    return {
        "status": status,
        "summary": {
            "candidates": len(candidates.get("candidates") or []),
            "splits_planned": len(splits),
            "new_ids_planned": sum(len(split["create"]) for split in splits),
            "source_refs_lost": total_lost,
            "source_refs_duplicated": total_duplicated,
            "problems": problems,
        },
        "policy": {
            "keep_existing_id": "group whose normalized name equals current_name; otherwise largest group",
            "mutation": "none; preview only",
        },
        "splits": splits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build code-name split preview")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    registry = json.load(args.registry.open())
    candidates = json.load(args.candidates.open())
    report = build_preview(registry, candidates)
    report.update(
        {
            "registry": str(args.registry),
            "candidates_path": str(args.candidates),
            "writes": "none; read-only preview",
        }
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps({"status": report["status"], **report["summary"]}, indent=2, ensure_ascii=False))
    print(f"report: {args.report}")
    return 0 if report["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
