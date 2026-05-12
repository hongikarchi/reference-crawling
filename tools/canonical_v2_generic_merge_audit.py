#!/usr/bin/env python3
"""Audit generic-name multi-source canonicals for likely false merges.

This is a read-only QC layer for names like "House K", "Private House", and
"Gallery House". It checks source-level metadata inside each canonical row
instead of treating duplicate display names as errors.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_upload_validator import (
    DEFAULT_INPUT,
    is_genericish_name,
    iter_buildings,
    normalize_name,
)


DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_generic_merge_audit.json"
SOURCE_DBS = {
    "divisare": ROOT / "data/crawl/divisare.db",
    "architizer": ROOT / "data/crawl/architizer.db",
    "archello": ROOT / "data/crawl/archello.db",
    "metalocus": ROOT / "data/crawl/metalocus.db",
}
CODE_TOKEN_RE = re.compile(r"^(?:[a-z]{1,3}|\d+|[a-z]\d+|\d+[a-z])$", re.I)
CODE_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "de",
    "do",
    "for",
    "in",
    "la",
    "no",
    "of",
    "on",
    "os",
    "the",
    "to",
}
GENERIC_BUILDING_TOKENS = {"apartment", "apartments", "casa", "house", "villa"}
COUNTRY_ALIASES = {
    "brasil": "brazil",
    "czech republic": "czechia",
    "dubai - uae": "united arab emirates",
    "dubai - united arab emirates": "united arab emirates",
    "españa": "spain",
    "iran, islamic republic of": "iran",
    "italia": "italy",
    "korea, republic of": "south korea",
    "korea selatan": "south korea",
    "lao people's democratic republic": "laos",
    "méxico": "mexico",
    "republic of serbia": "serbia",
    "russian federation": "russia",
    "singapor": "singapore",
    "the netherlands": "netherlands",
    "turkey": "türkiye",
    "uk": "united kingdom",
    "usa": "united states",
}


class SourceLookup(Protocol):
    def get(self, key: tuple[str, str]) -> dict[str, Any] | None:
        ...


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return normalize_name(str(value))


def _norm_country(value: Any) -> str:
    country = _norm(value)
    return COUNTRY_ALIASES.get(country, country)


def _year(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s\-_,./()]+", name.casefold()) if t]


def _is_code_token(token: str) -> bool:
    return bool(CODE_TOKEN_RE.match(token))


def _is_code_column(tokens: list[str], common_tokens: set[str]) -> bool:
    if not all(_is_code_token(token) for token in tokens):
        return False
    lowered = {token.casefold() for token in tokens}
    if lowered <= CODE_STOPWORDS:
        return False
    if lowered - CODE_STOPWORDS:
        return True
    return bool(common_tokens & GENERIC_BUILDING_TOKENS)


def names_differ_only_by_code(names: Iterable[str]) -> bool:
    unique = [n for n in dict.fromkeys(normalize_name(n) for n in names if n)]
    if len(unique) < 2:
        return False
    token_lists = [_tokens(name) for name in unique]
    if len({len(tokens) for tokens in token_lists}) != 1:
        return False

    differing_cols: list[list[str]] = []
    common_non_code = 0
    common_tokens: set[str] = set()
    for idx in range(len(token_lists[0])):
        col = [tokens[idx] for tokens in token_lists]
        if len(set(col)) == 1:
            common_tokens.add(col[0])
            if not _is_code_token(col[0]):
                common_non_code += 1
            continue
        differing_cols.append(col)

    if not differing_cols or common_non_code < 1:
        return False
    return all(_is_code_column(col, common_tokens) for col in differing_cols)


class SqliteSourceLookup:
    def __init__(self) -> None:
        self._conns: dict[str, sqlite3.Connection] = {}
        self._cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        for source, path in SOURCE_DBS.items():
            if path.exists():
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                self._conns[source] = conn

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()

    def get(self, key: tuple[str, str]) -> dict[str, Any] | None:
        if key in self._cache:
            return self._cache[key]
        source, source_id = key
        conn = self._conns.get(source)
        if conn is None:
            self._cache[key] = None
            return None
        queries = {
            "divisare": (
                "SELECT name, location_city AS city, location_country AS country, "
                "project_year AS year, architect_names AS architects "
                "FROM divisare_projects WHERE id=?"
            ),
            "architizer": (
                "SELECT name, location_city AS city, location_country AS country, "
                "completion_year AS year, firm_name AS architects "
                "FROM architizer_projects WHERE id=?"
            ),
            "archello": (
                "SELECT name, location_city AS city, location_country AS country, "
                "project_year AS year, architect_name AS architects "
                "FROM archello_projects WHERE id=?"
            ),
            "metalocus": (
                "SELECT title AS name, city, country, year, architects "
                "FROM buildings WHERE id=?"
            ),
        }
        try:
            row = conn.execute(queries[source], (str(source_id),)).fetchone()
        except (KeyError, sqlite3.Error):
            row = None
        meta = dict(row) if row else None
        self._cache[key] = meta
        return meta


def _iter_source_members(row: dict[str, Any], source_lookup: SourceLookup) -> tuple[list[dict[str, Any]], int]:
    members: list[dict[str, Any]] = []
    missing = 0
    for source, ids in (row.get("source_refs") or {}).items():
        for source_id in ids or []:
            sid = str(source_id)
            meta = source_lookup.get((source, sid))
            if not meta:
                missing += 1
                continue
            members.append({"source": source, "source_id": sid, **meta})
    return members, missing


def _audit_row(row: dict[str, Any], source_lookup: SourceLookup) -> tuple[dict[str, Any] | None, int]:
    if (row.get("n_sources") or 1) < 2:
        return None, 0

    canonical_name = str(row.get("name") or "")
    members, missing = _iter_source_members(row, source_lookup)
    if len(members) < 2:
        return None, missing

    names = [str(member.get("name") or "") for member in members]
    genericish = is_genericish_name(canonical_name) or any(is_genericish_name(name) for name in names)
    code_conflict = names_differ_only_by_code(names)
    if not genericish and not code_conflict:
        return None, missing

    countries = sorted({_norm(member.get("country")) for member in members if _norm(member.get("country"))})
    normalized_countries = sorted(
        {_norm_country(member.get("country")) for member in members if _norm_country(member.get("country"))}
    )
    cities = sorted({_norm(member.get("city")) for member in members if _norm(member.get("city"))})
    years = sorted({year for member in members if (year := _year(member.get("year"))) is not None})
    architects = sorted({_norm(member.get("architects")) for member in members if _norm(member.get("architects"))})

    flags: list[str] = []
    if len(normalized_countries) > 1:
        flags.append("country_conflict")
    if years and max(years) - min(years) > 2:
        flags.append("year_span_conflict")
    if code_conflict:
        flags.append("code_name_conflict")
    if len(architects) > 1:
        flags.append("architect_text_conflict")

    hard_flags = {"country_conflict", "year_span_conflict", "code_name_conflict"}
    review_required = bool(set(flags) & hard_flags)
    if not review_required and not flags:
        return None, missing

    return {
        "canonical_bld_id": row.get("canonical_bld_id"),
        "canonical_name": canonical_name,
        "flags": flags,
        "review_required": review_required,
        "countries": countries,
        "normalized_countries": normalized_countries,
        "cities": cities,
        "years": years,
        "architects": architects[:8],
        "members": [
            {
                "source": member.get("source"),
                "source_id": member.get("source_id"),
                "name": member.get("name"),
                "city": member.get("city"),
                "country": member.get("country"),
                "year": member.get("year"),
                "architects": member.get("architects"),
            }
            for member in members[:16]
        ],
    }, missing


def audit_rows(
    rows: Iterable[dict[str, Any]],
    source_lookup: SourceLookup,
    *,
    max_findings: int = 100,
) -> dict[str, Any]:
    flag_counts: Counter[str] = Counter()
    findings: list[dict[str, Any]] = []
    rows_examined = 0
    multi_source_rows = 0
    source_meta_missing = 0

    for row in rows:
        rows_examined += 1
        if (row.get("n_sources") or 1) >= 2:
            multi_source_rows += 1
        finding, missing = _audit_row(row, source_lookup)
        source_meta_missing += missing
        if not finding:
            continue
        for flag in finding["flags"]:
            flag_counts[flag] += 1
        findings.append(finding)

    review_required = sum(1 for finding in findings if finding["review_required"])
    return {
        "status": "BLOCK" if review_required else "PASS",
        "rows_examined": rows_examined,
        "multi_source_rows": multi_source_rows,
        "findings_total": len(findings),
        "review_required": review_required,
        "source_meta_missing": source_meta_missing,
        "flag_counts": dict(flag_counts),
        "findings": findings[:max_findings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit generic-name canonical merge risks")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--max-findings", type=int, default=500)
    args = parser.parse_args()

    lookup = SqliteSourceLookup()
    try:
        report = audit_rows(
            iter_buildings(args.input, limit=args.limit),
            lookup,
            max_findings=args.max_findings,
        )
    finally:
        lookup.close()

    report.update({"input": str(args.input), "limit": args.limit, "writes": "none; read-only audit"})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps({k: report[k] for k in ("status", "rows_examined", "multi_source_rows", "findings_total", "review_required", "source_meta_missing", "flag_counts")}, indent=2, ensure_ascii=False))
    print(f"report: {args.report}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
