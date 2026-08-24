from __future__ import annotations

from pathlib import Path

from tools.validate_vnext_wp0c_contracts import validate_contracts, validate_ddl_text


ROOT = Path(__file__).resolve().parents[1]


def test_production_wp0c_contract_set_passes() -> None:
    report = validate_contracts(ROOT)

    assert report["status"] == "PASS", report["errors"]
    assert report["file_count"] == 5


def test_ddl_validator_rejects_second_table_and_write_statement() -> None:
    ddl = (ROOT / "docs/vnext/BUILDING_IMAGES_DDL.sql").read_text(encoding="utf-8")
    errors = validate_ddl_text(ddl + "\nCREATE TABLE leaked(id int);\nDELETE FROM leaked;\n")

    assert any("exactly one table" in error for error in errors)
    assert any("forbidden WP0c statement: DELETE" in error for error in errors)


def test_ddl_validator_rejects_semicolon_prefixed_banned_statement() -> None:
    ddl = (ROOT / "docs/vnext/BUILDING_IMAGES_DDL.sql").read_text(encoding="utf-8")
    errors = validate_ddl_text(ddl + "\nSELECT 1; DROP TABLE leaked;\n")

    assert any("forbidden WP0c statement: DROP" in error for error in errors)


def test_ddl_validator_ignores_banned_words_in_comments_and_literals() -> None:
    ddl = (ROOT / "docs/vnext/BUILDING_IMAGES_DDL.sql").read_text(encoding="utf-8")
    harmless = ddl + "\n-- ; DROP TABLE harmless\nSELECT '; DELETE FROM harmless';\n"

    errors = validate_ddl_text(harmless)

    assert not any("forbidden WP0c statement" in error for error in errors)
