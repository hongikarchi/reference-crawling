#!/usr/bin/env python3
"""Build a C6 web/recrawl candidate queue for remaining canonical_v2 gaps.

This is a read-only planning artifact. It turns remaining metadata gaps into a
durable queue so that web search / re-crawl can be smoked, measured, and later
applied narrowly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_CANONICAL = Path(
    "data/canonical/country_conflict_refresh/"
    "canonical_buildings_strict_embedded.completeness_c4.json"
)
DEFAULT_C5_VERDICT = Path("data/reports/canonical_v2_c5_local_candidate_verdict.json")
DEFAULT_C6_SMOKE = Path("data/reports/canonical_v2_c6_web_search_smoke.json")
DEFAULT_QUEUE_JSON = Path("data/reports/canonical_v2_c6_candidate_queue.json")
DEFAULT_QUEUE_MD = Path("data/reports/canonical_v2_c6_candidate_queue.md")
DEFAULT_N100_JSON = Path("data/reports/canonical_v2_c6_n100_smoke_queue.json")
DEFAULT_N100_MD = Path("data/reports/canonical_v2_c6_n100_smoke_queue.md")

FIELDS = ("location_country", "location_city", "project_year")
DESIGN_OBJECT_TERMS = {
    "bag",
    "chair",
    "table",
    "lamp",
    "vase",
    "cutlery",
    "spoon",
    "fork",
    "knife",
    "product",
    "furniture",
    "collection",
    "object",
}
ABSTRACT_TERMS = {
    "manifesto",
    "atlas",
    "speculum",
    "collage",
    "installation",
    "exhibition",
    "biennale",
    "pavilion",
}
GENERIC_NAME_TERMS = {
    "affordable housing",
    "housing",
    "apartment",
    "villa",
    "house",
    "office",
    "tower",
    "museum",
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get("buildings"), list):
        return data["buildings"]
    if isinstance(data, list):
        return data
    raise SystemExit(f"Unsupported canonical format: {path}")


def load_json_optional(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def stable_hash(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:12], 16)


def clean_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def source_names(row: dict[str, Any]) -> list[str]:
    refs = row.get("source_refs") or {}
    if isinstance(refs, dict):
        return sorted(str(k) for k, v in refs.items() if v)
    return []


def source_profile(names: list[str]) -> str:
    if not names:
        return "no_source_ref"
    if len(names) == 1:
        return f"single_{names[0]}"
    return "multi_source"


def first_values(values: Any, limit: int = 4) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return [str(v) for v in values[:limit]]
    return [str(values)]


def architect_names(row: dict[str, Any]) -> list[str]:
    names = row.get("architect_names") or row.get("architects") or []
    return first_values(names, 3)


def missing_fields(row: dict[str, Any]) -> list[str]:
    return [field for field in FIELDS if not row.get(field)]


def missing_pattern(fields: list[str]) -> str:
    return "+".join(fields)


def has_design_object_risk(row: dict[str, Any]) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("name", "program", "style", "visual_description", "architects_text")
    )
    tokens = clean_tokens(text)
    return bool(tokens & DESIGN_OBJECT_TERMS)


def has_abstract_location_risk(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(key) or "") for key in ("name", "program", "style"))
    tokens = clean_tokens(text)
    return bool(tokens & ABSTRACT_TERMS)


def has_generic_name_risk(row: dict[str, Any]) -> bool:
    name = str(row.get("name") or "").lower()
    return any(term in name for term in GENERIC_NAME_TERMS)


def source_urls(row: dict[str, Any]) -> list[str]:
    urls = row.get("source_urls") or {}
    out: list[str] = []
    if isinstance(urls, dict):
        for value in urls.values():
            out.extend(first_values(value, 3))
    elif isinstance(urls, list):
        out.extend(first_values(urls, 6))
    return [url for url in out if url.startswith(("http://", "https://"))][:8]


def direct_source_hints(row: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    refs = row.get("source_refs") or {}
    if not isinstance(refs, dict):
        return hints
    for source, ids in refs.items():
        for source_id in first_values(ids, 4):
            if source == "divisare" and source_id.isdigit():
                hints.append(f"https://divisare.com/projects/{source_id}")
            else:
                hints.append(f"{source}:{source_id}")
    return hints[:8]


def query_templates(row: dict[str, Any], fields: list[str]) -> list[str]:
    name = str(row.get("name") or "").strip()
    architects = architect_names(row)
    architect = architects[0] if architects else ""
    field_terms = []
    if "location_country" in fields or "location_city" in fields:
        field_terms.extend(["location", "city", "country"])
    if "project_year" in fields:
        field_terms.extend(["project year", "completed", "opened"])
    term = " ".join(dict.fromkeys(field_terms))
    queries = []
    if name and architect:
        queries.append(f'"{name}" "{architect}" {term}'.strip())
        queries.append(f'"{name}" "{architect}" "Divisare"')
    elif name:
        queries.append(f'"{name}" {term}'.strip())
    for source in source_names(row)[:2]:
        if name:
            queries.append(f'"{name}" "{source}" {term}'.strip())
    return list(dict.fromkeys(queries))[:4]


def c5_apply_ready_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for row in payload.get("rows", []):
        if row.get("verdict") == "apply":
            out[row["canonical_bld_id"]] = row
    return out


def c6_smoke_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["canonical_bld_id"]: row for row in payload.get("rows", [])}


def classify_next_step(
    row: dict[str, Any],
    fields: list[str],
    c5_ready: dict[str, dict[str, Any]],
    smoke: dict[str, dict[str, Any]],
) -> tuple[str, list[str], int]:
    cid = row["canonical_bld_id"]
    risks: list[str] = []
    score = 0

    if cid in c5_ready:
        return "c5_local_apply_ready", ["local_semantic_verdict"], 100

    smoke_row = smoke.get(cid)
    if smoke_row:
        verdict = smoke_row.get("verdict")
        if verdict == "likely_safe_apply_after_source_review":
            return "c6_seed_apply_review", ["web_smoke_seed"], 90
        if verdict in {"conflict_manual", "keep_null_policy", "unresolved"}:
            risks.append(f"web_smoke_{verdict}")

    if "location_country" in fields and "location_city" in fields:
        score += 45
    elif "location_city" in fields:
        score += 25
    elif "location_country" in fields:
        score += 20

    if "project_year" in fields:
        score += 25

    names = source_names(row)
    if names:
        score += 10
    if len(names) == 1:
        score += 5
    if source_urls(row) or direct_source_hints(row):
        score += 10

    if has_design_object_risk(row):
        risks.append("design_object_candidate")
        score -= 30
    if has_abstract_location_risk(row):
        risks.append("abstract_or_exhibition_candidate")
        score -= 10
    if has_generic_name_risk(row):
        risks.append("generic_name_conflict_risk")
        score -= 10

    if "design_object_candidate" in risks:
        return "policy_null_review", risks, max(score, 5)
    if "location_country" in fields or "location_city" in fields:
        if "project_year" in fields:
            return "web_search_location_year", risks, max(score, 10)
        return "web_search_location", risks, max(score, 10)
    if "project_year" in fields:
        return "web_search_year", risks, max(score, 10)
    return "manual_review", risks, max(score, 0)


def priority_band(score: int) -> str:
    if score >= 75:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


def build_smoke_queue(items: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(candidates: list[dict[str, Any]], quota: int) -> None:
        nonlocal selected
        for item in candidates:
            cid = item["canonical_bld_id"]
            if cid in seen:
                continue
            selected.append(item)
            seen.add(cid)
            if len(selected) >= limit or quota <= 1:
                return
            quota -= 1

    by_step: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_step[item["recommended_next_step"]].append(item)

    for values in by_step.values():
        values.sort(key=lambda x: (-x["priority_score"], stable_hash(x["canonical_bld_id"])))

    add(by_step.get("c6_seed_apply_review", []), 10)
    add(by_step.get("web_search_location_year", []), 35)
    add(by_step.get("web_search_location", []), 35)
    add(by_step.get("web_search_year", []), 20)
    add(by_step.get("policy_null_review", []), 10)

    if len(selected) < limit:
        rest = sorted(
            items,
            key=lambda x: (-x["priority_score"], stable_hash(x["canonical_bld_id"])),
        )
        add(rest, limit - len(selected))

    return selected[:limit]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--c5-verdict", type=Path, default=DEFAULT_C5_VERDICT)
    parser.add_argument("--c6-smoke", type=Path, default=DEFAULT_C6_SMOKE)
    parser.add_argument("--queue-json", type=Path, default=DEFAULT_QUEUE_JSON)
    parser.add_argument("--queue-md", type=Path, default=DEFAULT_QUEUE_MD)
    parser.add_argument("--n100-json", type=Path, default=DEFAULT_N100_JSON)
    parser.add_argument("--n100-md", type=Path, default=DEFAULT_N100_MD)
    args = parser.parse_args()

    rows = load_rows(args.canonical)
    c5_ready = c5_apply_ready_map(load_json_optional(args.c5_verdict))
    smoke = c6_smoke_map(load_json_optional(args.c6_smoke))

    items: list[dict[str, Any]] = []
    field_counts: Counter[str] = Counter()
    pattern_counts: Counter[str] = Counter()
    step_counts: Counter[str] = Counter()
    priority_counts: Counter[str] = Counter()
    source_profile_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()

    for row in rows:
        fields = missing_fields(row)
        if not fields:
            continue

        for field in fields:
            field_counts[field] += 1
        pattern = missing_pattern(fields)
        pattern_counts[pattern] += 1

        next_step, risks, score = classify_next_step(row, fields, c5_ready, smoke)
        band = priority_band(score)
        step_counts[next_step] += 1
        priority_counts[band] += 1
        for risk in risks:
            risk_counts[risk] += 1

        names = source_names(row)
        profile = source_profile(names)
        source_profile_counts[profile] += 1

        item = {
            "canonical_bld_id": row["canonical_bld_id"],
            "name": row.get("name"),
            "architect_names": architect_names(row),
            "missing_fields": fields,
            "missing_pattern": pattern,
            "source_refs": row.get("source_refs") or {},
            "source_names": names,
            "source_profile": profile,
            "source_url_hints": source_urls(row),
            "direct_source_hints": direct_source_hints(row),
            "recommended_next_step": next_step,
            "risk_flags": risks,
            "priority_score": score,
            "priority_band": band,
            "query_templates": query_templates(row, fields),
        }

        c5_row = c5_ready.get(row["canonical_bld_id"])
        if c5_row:
            item["proposed_values"] = {
                c5_row["field"]: c5_row.get("proposed_value"),
            }
        smoke_row = smoke.get(row["canonical_bld_id"])
        if smoke_row:
            item["seed_smoke_verdict"] = smoke_row

        items.append(item)

    items.sort(
        key=lambda x: (
            -x["priority_score"],
            x["recommended_next_step"],
            stable_hash(x["canonical_bld_id"]),
        )
    )
    smoke100 = build_smoke_queue(items, 100)

    summary = {
        "canonical_input": str(args.canonical),
        "rows_inspected": len(rows),
        "rows_missing_any": len(items),
        "field_counts": dict(sorted(field_counts.items())),
        "missing_pattern_counts": dict(sorted(pattern_counts.items())),
        "recommended_next_step_counts": dict(sorted(step_counts.items())),
        "priority_band_counts": dict(sorted(priority_counts.items())),
        "source_profile_counts": dict(sorted(source_profile_counts.items())),
        "risk_flag_counts": dict(sorted(risk_counts.items())),
        "n100_smoke_size": len(smoke100),
    }

    payload = {
        "status": "PASS",
        "mode": "read_only_c6_candidate_queue",
        "summary": summary,
        "items": items,
    }
    smoke_payload = {
        "status": "PASS",
        "mode": "read_only_c6_n100_smoke_queue",
        "summary": {
            "source_queue": str(args.queue_json),
            "size": len(smoke100),
            "recommended_next_step_counts": dict(
                sorted(Counter(i["recommended_next_step"] for i in smoke100).items())
            ),
            "missing_pattern_counts": dict(
                sorted(Counter(i["missing_pattern"] for i in smoke100).items())
            ),
            "source_profile_counts": dict(
                sorted(Counter(i["source_profile"] for i in smoke100).items())
            ),
        },
        "items": smoke100,
    }

    for path, data in (
        (args.queue_json, payload),
        (args.n100_json, smoke_payload),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    write_queue_md(args.queue_md, summary, items)
    write_n100_md(args.n100_md, smoke_payload)

    print(
        json.dumps(
            {
                "status": "PASS",
                "rows_missing_any": len(items),
                "recommended_next_step_counts": summary["recommended_next_step_counts"],
                "n100_smoke_size": len(smoke100),
                "writes": [
                    str(args.queue_json),
                    str(args.queue_md),
                    str(args.n100_json),
                    str(args.n100_md),
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def write_queue_md(path: Path, summary: dict[str, Any], items: list[dict[str, Any]]) -> None:
    lines = [
        "# canonical_v2 C6 candidate queue",
        "",
        "Mode: read-only queue builder for remaining metadata gaps.",
        "",
        "## Summary",
        "",
        f"- rows inspected: {summary['rows_inspected']}",
        f"- rows missing any C6 field: {summary['rows_missing_any']}",
        f"- N=100 smoke queue size: {summary['n100_smoke_size']}",
        "",
        "## Field counts",
        "",
        "| field | count |",
        "| --- | ---: |",
    ]
    for key, value in summary["field_counts"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(["", "## Recommended next step counts", "", "| next_step | count |", "| --- | ---: |"])
    for key, value in summary["recommended_next_step_counts"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(["", "## Missing pattern counts", "", "| pattern | count |", "| --- | ---: |"])
    for key, value in summary["missing_pattern_counts"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(["", "## Risk flag counts", "", "| risk | count |", "| --- | ---: |"])
    for key, value in summary["risk_flag_counts"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(
        [
            "",
            "## Top queue examples",
            "",
            "| cid | name | missing | next_step | score | risks | query 1 |",
            "| --- | --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for item in items[:30]:
        name = str(item["name"]).replace("|", "/")
        risks = ", ".join(item["risk_flags"])
        query = item["query_templates"][0] if item["query_templates"] else ""
        query = query.replace("|", "/")
        lines.append(
            f"| {item['canonical_bld_id']} | {name} | "
            f"{','.join(item['missing_fields'])} | {item['recommended_next_step']} | "
            f"{item['priority_score']} | {risks} | `{query}` |"
        )

    lines.extend(
        [
            "",
            "## C6 apply rules",
            "",
            "1. Prefer exact original source page/source ID over general search results.",
            "2. Source ranking: original crawler source > architect/project official page > reputable architecture publication > repost/search snippet.",
            "3. Auto-apply only if a high-ranked source gives one non-conflicting value.",
            "4. Keep conflicts, design-object cases, and abstract/theoretical projects out of automatic apply.",
            "5. Use N=10 then N=100 smoke measurements before any larger web batch.",
            "6. Apply only affected rows and upsert only affected rows to Neon.",
            "",
            "## Apply boundary",
            "",
            "This queue does not mutate canonical artifacts or Neon.",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def write_n100_md(path: Path, payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    lines = [
        "# canonical_v2 C6 N=100 smoke queue",
        "",
        "Mode: read-only deterministic smoke queue generated from the C6 candidate queue.",
        "",
        "## Summary",
        "",
        f"- size: {summary['size']}",
        "",
        "## Recommended next step counts",
        "",
        "| next_step | count |",
        "| --- | ---: |",
    ]
    for key, value in summary["recommended_next_step_counts"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(["", "## Missing pattern counts", "", "| pattern | count |", "| --- | ---: |"])
    for key, value in summary["missing_pattern_counts"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(
        [
            "",
            "## Queue rows",
            "",
            "| idx | cid | name | missing | next_step | score | query 1 |",
            "| ---: | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for idx, item in enumerate(payload["items"], 1):
        name = str(item["name"]).replace("|", "/")
        query = item["query_templates"][0] if item["query_templates"] else ""
        query = query.replace("|", "/")
        lines.append(
            f"| {idx} | {item['canonical_bld_id']} | {name} | "
            f"{','.join(item['missing_fields'])} | {item['recommended_next_step']} | "
            f"{item['priority_score']} | `{query}` |"
        )
    lines.append("")
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
