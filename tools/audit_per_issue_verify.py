#!/usr/bin/env python3
"""Per-issue actual-data verification for the 2026-05-27 Make DB audit.

Reads the audit CSV at docs/make-db-audit-2026-05-27/publishable_issues.csv,
queries Neon canonical_v2_buildings for sample rows per issue code, and writes
a verification report so we can decide per-issue whether it is a real problem.

Read-only. No DB writes.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect  # noqa: E402

CSV_PATH = Path("/private/tmp/make_web_db_audit/docs/make-db-audit-2026-05-27/publishable_issues.csv")
OUT_PATH = ROOT / "data/reports/audit_2026-05-27/per_issue_verification.md"

SAMPLE_N = 5

# Per-issue extra columns to pull from DB (besides the common base columns).
ISSUE_FIELDS: dict[str, list[str]] = {
    "MATERIAL_TAXONOMY_NOISE": ["material_visual"],
    "MATERIAL_KEYWORD_MISSING": ["material_visual", "visual_description"],
    "PROGRAM_OTHER": ["program", "name"],
    "PROGRAM_KEYWORD_MISMATCH": ["program", "name", "source_categories"],
    "FUTURE_PROJECT": ["project_year", "year_kind"],
    "TOO_FEW_GALLERY_IMAGES": ["all_images"],
    "GALLERY_PHASH_SHARED_ACROSS_BUILDINGS": ["all_images"],
    "MISSING_YEAR": ["project_year", "year_kind"],
    "UNKNOWN_YEAR_KIND": ["project_year", "year_kind"],
    "MULTIPLE_IMAGE_SOURCE_IDS_SAME_SOURCE": ["all_images", "source_refs"],
    "SUSPICIOUS_COVER_URL_WORD": ["display_cover_url", "cover_image_url_default"],
    "COVER_PHASH_SHARED_ACROSS_BUILDINGS": ["display_cover_url", "all_images"],
    "ALL_FOCUS_COVERS_NULL": ["covers_by_type"],
    "SUSPICIOUS_YEAR": ["project_year", "year_kind"],
    "GALLERY_IMAGE_SHARED_ACROSS_BUILDINGS": ["all_images"],
    "GENERIC_TITLE": ["name"],
    "SHORT_VISUAL_DESCRIPTION": ["visual_description"],
}

BASE_COLS = ["canonical_bld_id", "name", "is_publishable"]


def load_issues_by_code() -> dict[str, list[dict[str, str]]]:
    by_code: dict[str, list[dict[str, str]]] = defaultdict(list)
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            by_code[row["code"]].append(row)
    return by_code


def fetch_sample(conn, code: str, ids: list[str]) -> list[dict[str, Any]]:
    extras = ISSUE_FIELDS.get(code, [])
    cols = BASE_COLS + [c for c in extras if c not in BASE_COLS]
    sql = f"SELECT {', '.join(cols)} FROM canonical_v2_buildings WHERE canonical_bld_id = ANY(%s)"
    cur = conn.cursor()
    cur.execute(sql, (ids,))
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        out.append(dict(zip(cols, r)))
    return out


def fmt_value(v: Any, max_len: int = 200) -> str:
    if v is None:
        return "null"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, default=str)
    else:
        s = str(v)
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def main() -> int:
    by_code = load_issues_by_code()
    counts = {code: len(rows) for code, rows in by_code.items()}
    order = sorted(counts, key=lambda c: counts[c], reverse=True)

    conn = _connect()
    lines: list[str] = []
    lines.append("# Per-Issue Verification — Publishable rows (36,864)")
    lines.append("")
    lines.append("Source CSV: `docs/make-db-audit-2026-05-27/publishable_issues.csv`")
    lines.append(f"Sample size per code: {SAMPLE_N}")
    lines.append("")

    for code in order:
        issues = by_code[code]
        sample_issues = issues[:SAMPLE_N]
        sample_ids = [i["canonical_bld_id"] for i in sample_issues]
        sev = sample_issues[0]["severity"]
        msg = sample_issues[0]["message"]

        lines.append(f"## `{code}`")
        lines.append("")
        lines.append(f"- count: **{len(issues)}**")
        lines.append(f"- severity: `{sev}`")
        lines.append(f"- audit message: {msg}")
        lines.append("")
        try:
            db_rows = fetch_sample(conn, code, sample_ids)
        except Exception as exc:
            lines.append(f"  query error: {exc}")
            continue
        db_by_id = {r["canonical_bld_id"]: r for r in db_rows}

        for iss in sample_issues:
            bid = iss["canonical_bld_id"]
            row = db_by_id.get(bid)
            lines.append(f"### {bid} — {iss['name']}")
            lines.append("")
            lines.append(f"- audit evidence: `{iss['evidence']}`")
            if row:
                for col, val in row.items():
                    if col in ("canonical_bld_id", "name"):
                        continue
                    lines.append(f"- `{col}`: {fmt_value(val)}")
            lines.append("")

    conn.close()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_PATH} ({len(order)} issue codes, {sum(counts.values())} total issues)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
