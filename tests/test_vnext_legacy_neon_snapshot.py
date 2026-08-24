from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.export_vnext_legacy_neon_snapshot import (
    EXPECTED_EXPORT_COLUMNS,
    _validate_live_columns,
    export_rows,
)
from tools.validate_vnext_legacy_neon_snapshot import (
    EXPECTED_COLUMNS,
    validate_snapshot,
    write_report,
)


def _row(canonical_bld_id: str, source_refs: object) -> dict[str, object]:
    row = {column: None for column in EXPECTED_EXPORT_COLUMNS}
    row.update(
        {
            "canonical_bld_id": canonical_bld_id,
            "name": f"Building {canonical_bld_id}",
            "names_alts": [],
            "architect_canonical_ids": [],
            "architect_names": [],
            "material_visual": [],
            "image_derived": {},
            "covers_by_type": {},
            "all_images": [],
            "best_image_per_cluster": {},
            "source_refs": source_refs,
            "source_urls": {},
            "publishability_reasons": [],
            "typology_tags": [],
            "architectural_elements": [],
            "source_categories": {},
            "created_at": datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 24, 4, 5, 6, tzinfo=timezone.utc),
        }
    )
    return row


def _rows() -> list[dict[str, object]]:
    return [
        _row("bld_001", {"divisare": ["d1"]}),
        _row("bld_002", {"architizer": ["a2"], "divisare": ["d2"]}),
        _row("bld_003", [{"source": "archello", "id": "x"}]),
    ]


def test_export_and_independent_validation_pass(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.jsonl"

    built = export_rows(_rows(), output, expected_rows=3)
    validated = validate_snapshot(output, expected_rows=3)

    assert tuple(EXPECTED_EXPORT_COLUMNS) == tuple(EXPECTED_COLUMNS)
    assert built["status"] == "PASS"
    assert built["column_count"] == 44
    assert built["excluded_columns"] == ["embedding"]
    assert validated["status"] == "PASS"
    assert validated["file_sha256"] == built["file_sha256"]
    assert validated["source_refs_root_types"] == {"dict": 2, "list": 1}
    first = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert first["created_at"] == "2026-08-24T01:02:03Z"
    assert first["facade_pattern"] is None
    assert "embedding" not in first


def test_export_refuses_unsorted_rows_and_retains_partial(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.jsonl"
    rows = list(reversed(_rows()))

    with pytest.raises(ValueError, match="strictly ordered"):
        export_rows(rows, output, expected_rows=3)

    assert not output.exists()
    assert output.with_name(output.name + ".partial").exists()


def test_export_refuses_to_clobber(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.jsonl"
    export_rows(_rows(), output, expected_rows=3)

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        export_rows(_rows(), output, expected_rows=3)


def test_export_refuses_thin_or_embedding_row_before_promotion(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.jsonl"
    thin = {"canonical_bld_id": "bld_001", "source_refs": {}}

    with pytest.raises(ValueError, match="schema mismatch"):
        export_rows([thin], output, expected_rows=1)

    assert not output.exists()
    assert output.with_name(output.name + ".partial").exists()

    output_2 = tmp_path / "snapshot_2.jsonl"
    row_with_embedding = _row("bld_001", {})
    row_with_embedding["embedding"] = [0.0]
    with pytest.raises(ValueError, match=r"extra=\['embedding'\]"):
        export_rows([row_with_embedding], output_2, expected_rows=1)
    assert not output_2.exists()


def test_live_schema_check_fails_closed_on_drift() -> None:
    from tools.export_vnext_legacy_neon_snapshot import EXPECTED_DB_COLUMNS

    _validate_live_columns(EXPECTED_DB_COLUMNS)
    with pytest.raises(ValueError, match="schema drift"):
        _validate_live_columns(EXPECTED_DB_COLUMNS[:-1])
    with pytest.raises(ValueError, match="schema drift"):
        _validate_live_columns((*EXPECTED_DB_COLUMNS, "future_column"))


def test_validator_detects_noncanonical_tamper(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.jsonl"
    export_rows(_rows(), output, expected_rows=3)
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    records[1]["source_refs"] = None
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    report = validate_snapshot(output, expected_rows=3)

    assert report["status"] == "FAIL"
    assert {error["code"] for error in report["errors"]} >= {
        "invalid_source_refs_root",
        "noncanonical_encoding",
    }


def test_validation_report_is_persisted_without_clobber(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.jsonl"
    report_path = tmp_path / "snapshot.validation.json"
    export_rows(_rows(), output, expected_rows=3)
    report = validate_snapshot(output, expected_rows=3)

    write_report(report, report_path)

    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "PASS"
    assert persisted["column_count"] == 44
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_report(report, report_path)
