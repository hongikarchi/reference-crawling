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
    executable = re.sub(r"(?m)^\s*--.*$", "", text)
    for statement in ("DROP", "ALTER", "TRUNCATE", "INSERT", "UPDATE", "DELETE"):
        if re.search(rf"(?im)^\s*{statement}\b", executable):
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
            "사람 승인", "직접", "one_to_one", "false merge",
        ),
        "CONTRACT_GALLERY_SEMANTICS.md": (
            "source occurrence", "exclusion ledger", "compact semantic", "in_scope",
            "medium별 applicability", "perspective", "disagreement", "P6",
        ),
        "CONTRACT_COVERS.md": (
            "default", "exterior", "interior", "drawing", "aerial", "detail",
            "comparative_vision", "review_required", "acceptable top-3", "analysis bundle",
        ),
        "PROJECTION_SPEC.md": (
            "S1", "S2", "S3", "S4", "S5", "canonical_v2_buildings",
            "building_images", "exact-set", "사용자 명시 승인", "롤백",
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
