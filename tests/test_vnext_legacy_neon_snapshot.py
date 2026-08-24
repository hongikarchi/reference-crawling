from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.export_vnext_legacy_neon_snapshot import export_rows
from tools.validate_vnext_legacy_neon_snapshot import validate_snapshot


def _rows() -> list[dict[str, object]]:
    return [
        {"canonical_bld_id": "bld_001", "source_refs": {"divisare": ["d1"]}},
        {
            "canonical_bld_id": "bld_002",
            "source_refs": {"architizer": ["a2"], "divisare": ["d2"]},
        },
        {"canonical_bld_id": "bld_003", "source_refs": [{"source": "archello", "id": "x"}]},
    ]


def test_export_and_independent_validation_pass(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.jsonl"

    built = export_rows(_rows(), output, expected_rows=3)
    validated = validate_snapshot(output, expected_rows=3)

    assert built["status"] == "PASS"
    assert validated["status"] == "PASS"
    assert validated["file_sha256"] == built["file_sha256"]
    assert validated["source_refs_root_types"] == {"dict": 2, "list": 1}


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
