#!/usr/bin/env python3
"""Validate the fixed WP0c contract set and serving DDL without DB access."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "CONTRACT_IDENTITY.md",
    "CONTRACT_GALLERY_SEMANTICS.md",
    "CONTRACT_COVERS.md",
    "BUILDING_IMAGES_DDL.sql",
    "PROJECTION_SPEC.md",
)


def _contains_all(text: str, values: Sequence[str]) -> list[str]:
    return [value for value in values if value not in text]


def _strip_sql_comments_and_literals(text: str) -> str:
    """Keep statement separators/tokens while blanking comments and literals."""

    output: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("--", index):
            end = text.find("\n", index + 2)
            if end == -1:
                output.extend(" " * (length - index))
                break
            output.extend(" " * (end - index))
            output.append("\n")
            index = end + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                output.extend(" " * (length - index))
                break
            output.extend("\n" if char == "\n" else " " for char in text[index : end + 2])
            index = end + 2
            continue
        quote = text[index]
        if quote in ("'", '"'):
            output.append(" ")
            index += 1
            while index < length:
                if text[index] == quote:
                    if index + 1 < length and text[index + 1] == quote:
                        output.extend((" ", " "))
                        index += 2
                        continue
                    output.append(" ")
                    index += 1
                    break
                output.append("\n" if text[index] == "\n" else " ")
                index += 1
            continue
        if text[index] == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", text[index:])
            if match:
                delimiter = match.group(0)
                end = text.find(delimiter, index + len(delimiter))
                if end == -1:
                    output.extend(" " * (length - index))
                    break
                stop = end + len(delimiter)
                output.extend("\n" if char == "\n" else " " for char in text[index:stop])
                index = stop
                continue
        output.append(text[index])
        index += 1
    return "".join(output)


def validate_ddl_text(text: str) -> list[str]:
    errors: list[str] = []
    create_tables = re.findall(r"\bCREATE\s+TABLE\b", text, flags=re.IGNORECASE)
    if len(create_tables) != 1:
        errors.append(f"DDL must create exactly one table, got {len(create_tables)}")
    missing = _contains_all(
        text,
        (
            "CREATE TABLE IF NOT EXISTS building_images",
            "REFERENCES canonical_v2_buildings(canonical_bld_id)",
            "semantic_type      TEXT[]",
            "cover_slot         TEXT[]",
            "PRIMARY KEY (canonical_bld_id, image_asset_id)",
            "UNIQUE (canonical_bld_id, sort_order)",
        ),
    )
    errors.extend(f"DDL missing: {value}" for value in missing)
    unique_indexes = re.findall(
        r"\bCREATE\s+UNIQUE\s+INDEX\b", text, flags=re.IGNORECASE
    )
    if len(unique_indexes) != 6:
        errors.append(f"DDL must define default + five unique cover indexes, got {len(unique_indexes)}")
    executable = _strip_sql_comments_and_literals(text)
    banned = ("DROP", "ALTER", "TRUNCATE", "INSERT", "UPDATE", "DELETE")
    for match in re.finditer(
        rf"(?is)(?:\A|;)\s*({'|'.join(banned)})\b", executable
    ):
        statement = match.group(1).upper()
        errors.append(f"DDL contains forbidden WP0c statement: {statement}")
    for forbidden_column in ("evidence_json", "source_occurrences", "lineage_json"):
        if forbidden_column in executable:
            errors.append(f"serving DDL leaks local-only field: {forbidden_column}")
    return errors


def validate_contracts(root: Path) -> dict[str, object]:
    directory = root.resolve() / "docs" / "vnext"
    errors: list[str] = []
    outputs: list[dict[str, object]] = []
    texts: dict[str, str] = {}
    for name in FILES:
        path = directory / name
        if not path.is_file():
            errors.append(f"missing file: {path}")
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            errors.append(f"invalid UTF-8: {name}: {exc}")
            continue
        if b"\r\n" in raw:
            errors.append(f"CRLF is not canonical: {name}")
        if not raw.endswith(b"\n"):
            errors.append(f"missing terminal LF: {name}")
        texts[name] = text
        outputs.append(
            {
                "path": path.relative_to(root.resolve()).as_posix(),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    required: dict[str, tuple[str, ...]] = {
        "CONTRACT_IDENTITY.md": (
            "91,803", "6,754", "9,026", "merge", "related", "reject", "defer",
            "사람 승인", "직접", "one_to_one", "false merge", "strict_sim >= 90",
            "3cf9592a9e54cd4e07512461ca08dc5ca7f8dd5e6cd97b2d6c118dd0b265d87b",
            "related_but_separate", "블라인드 1인 리뷰",
        ),
        "CONTRACT_GALLERY_SEMANTICS.md": (
            "source occurrence", "exclusion ledger", "compact semantic", "in_scope",
            "medium별 applicability", "perspective", "disagreement", "P6", "E2 `full_v5`",
            "transitive closure", "fail-closed", "`vnx_bld_*`",
            "local 엔진 전수 + 저신뢰(이중판정 불일치·low score)·커버 후보만 remote 승격.",
            "**1.43M 모집단 전수 원격 Vision은 WP3 결과와 무관하게 금지.**",
            "WP3 N10은 엔진 선택·임계값 캘리브레이션만 결정한다.",
        ),
        "CONTRACT_COVERS.md": (
            "default", "exterior", "interior", "drawing", "aerial", "detail",
            "comparative_vision", "review_required", "acceptable top-3", "analysis bundle",
            "5개 키를 가진다", "is_publishable=false", "`vnx_bld_id`",
        ),
        "PROJECTION_SPEC.md": (
            "S1", "S2", "S3", "S4", "S5", "canonical_v2_buildings",
            "building_images", "exact-set", "사용자 명시 승인", "롤백",
            "canonical_v2_tag_stats", "canonical_v2_tag_centroids",
            "canonical_v2_tag_vocabulary", "canonical_v2_tag_stats_build.py --with-r4",
            "scale, structural_system, roof_type, facade_pattern", "delete-then-insert",
        ),
    }
    for name, anchors in required.items():
        text = texts.get(name)
        if text is None:
            continue
        errors.extend(f"{name} missing anchor: {value}" for value in _contains_all(text, anchors))
    ddl = texts.get("BUILDING_IMAGES_DDL.sql")
    if ddl is not None:
        errors.extend(validate_ddl_text(ddl))

    return {
        "status": "PASS" if not errors else "FAIL",
        "file_count": len(outputs),
        "files": outputs,
        "error_count": len(errors),
        "errors": errors,
        "network_requests": 0,
        "llm_requests": 0,
        "db_connections": 0,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = validate_contracts(args.root)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
