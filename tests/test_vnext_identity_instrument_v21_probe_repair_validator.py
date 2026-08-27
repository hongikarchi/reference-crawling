from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from canonical import vnext_identity_instrument_v21_probe_repair_validator as validator


REPO_ROOT = Path(__file__).resolve().parents[1]


def _gate_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(15):
        rows.append(
            {
                "sample_id": f"d-{index:02d}",
                "group_code": "D",
                "image": "same_object",
                "scope": "same",
                "derived_label": "same",
                "n100_uncertain": False,
            }
        )
    rows.extend(
        [
            {
                "sample_id": "p1-a",
                "group_code": "P1",
                "image": "different_objects",
                "scope": "unrelated",
                "derived_label": "different",
                "n100_uncertain": False,
            },
            {
                "sample_id": "p1-b",
                "group_code": "P1",
                "image": "inconclusive",
                "scope": "same",
                "derived_label": "insufficient",
                "n100_uncertain": False,
            },
            {
                "sample_id": validator.EXPECTED_EXCLUDED_SAMPLE,
                "group_code": "P1",
                "image": "unavailable",
                "scope": "same",
                "derived_label": "insufficient",
                "n100_uncertain": True,
            },
        ]
    )
    for index in range(4):
        rows.append(
            {
                "sample_id": f"p2-{index}",
                "group_code": "P2",
                "image": "inconclusive" if index < 2 else "same_object",
                "scope": "same",
                "derived_label": "insufficient" if index < 2 else "same",
                "n100_uncertain": index < 2,
            }
        )
    for index in range(2):
        rows.append(
            {
                "sample_id": f"p3-{index}",
                "group_code": "P3",
                "image": "different_objects",
                "scope": "unrelated",
                "derived_label": "different",
                "n100_uncertain": True,
            }
        )
    return rows


def test_adjusted_gate_preserves_all_four_frozen_ratios() -> None:
    gate = validator.effective_gate_from_rows(
        _gate_rows(),
        excluded_sample_ids=[validator.EXPECTED_EXCLUDED_SAMPLE],
    )
    assert gate["status"] == "pass"
    assert {
        code: (row["denominator"], row["threshold"])
        for code, row in gate["predicates"].items()
    } == {
        "d_image_same_object": (15, 14),
        "d_scope_same": (15, 14),
        "n100_image_non_same_object": (4, 3),
        "p1_non_same_label": (2, 2),
    }


def test_effective_gate_terminal_fail_is_a_valid_measured_outcome() -> None:
    rows = _gate_rows()
    rows[0]["image"] = "different_objects"
    rows[1]["image"] = "different_objects"
    gate = validator.effective_gate_from_rows(
        rows,
        excluded_sample_ids=[validator.EXPECTED_EXCLUDED_SAMPLE],
    )
    assert gate["status"] == "fail"
    assert validator.terminal_consistency_failures(
        gate,
        persisted_status="fail",
        report_status="FAIL",
        manifest_status="FAIL",
    ) == []
    assert validator.terminal_consistency_failures(
        gate,
        persisted_status="pass",
        report_status="FAIL",
        manifest_status="FAIL",
    ) == ["persisted_status"]


def test_effective_gate_terminal_pass_is_a_valid_measured_outcome() -> None:
    gate = validator.effective_gate_from_rows(
        _gate_rows(),
        excluded_sample_ids=[validator.EXPECTED_EXCLUDED_SAMPLE],
    )
    assert gate["status"] == "pass"
    assert validator.terminal_consistency_failures(
        gate,
        persisted_status="pass",
        report_status="PASS",
        manifest_status="PASS",
    ) == []


def test_real_repair_source_is_exact_failed_17_only() -> None:
    original = validator._source_snapshot(
        REPO_ROOT,
        validator.DEFAULT_SOURCE_V1,
        verify_full_hashes=True,
        verify_files=True,
    )
    repaired = validator._source_snapshot(
        REPO_ROOT,
        validator.DEFAULT_SOURCE_V2,
        verify_full_hashes=True,
        verify_files=True,
    )
    assert validator._compare_source_versions(original, repaired) == []
    completeness = validator._expected_completeness(repaired)
    assert [
        (row["sample_id"], row["side"])
        for row in completeness
        if not row["complete"]
    ] == [(validator.EXPECTED_EXCLUDED_SAMPLE, "right")]


def test_source_delta_tamper_is_rejected() -> None:
    original = validator._source_snapshot(
        REPO_ROOT,
        validator.DEFAULT_SOURCE_V1,
        verify_full_hashes=False,
        verify_files=False,
    )
    repaired = validator._source_snapshot(
        REPO_ROOT,
        validator.DEFAULT_SOURCE_V2,
        verify_full_hashes=False,
        verify_files=False,
    )
    tampered = copy.deepcopy(repaired)
    requested = next(row for row in tampered["fetch_assets"] if row["request_count"] == 1)
    requested["request_count"] = 0
    assert "requested_equals_failed" in validator._compare_source_versions(original, tampered)


def test_source_delta_rejects_success_change_and_failed_identity_change() -> None:
    original = validator._source_snapshot(
        REPO_ROOT,
        validator.DEFAULT_SOURCE_V1,
        verify_full_hashes=False,
        verify_files=False,
    )
    repaired = validator._source_snapshot(
        REPO_ROOT,
        validator.DEFAULT_SOURCE_V2,
        verify_full_hashes=False,
        verify_files=False,
    )
    success_tamper = copy.deepcopy(repaired)
    success = next(row for row in success_tamper["fetch_assets"] if row["request_count"] == 0)
    success["status"] = "delivery_changed"
    assert any(
        value.startswith("successful_fetch_changed:")
        for value in validator._compare_source_versions(original, success_tamper)
    )

    failed_tamper = copy.deepcopy(repaired)
    recovered = next(row for row in failed_tamper["fetch_assets"] if row["request_count"] == 1)
    recovered["source_asset_id"] = "forbidden-lineage-change"
    assert any(
        value.startswith("failed_fetch_forbidden_delta:")
        for value in validator._compare_source_versions(original, failed_tamper)
    )


def test_blind_scanner_allows_vocab_labels_but_rejects_hidden_ids() -> None:
    guidance = {"instructions": "Choose same, related, different, or insufficient."}
    hidden = {"probe_secret", "candidate_secret", "n100_secret"}
    assert validator.blind_json_leaks(guidance, hidden_identifiers=hidden) == []
    leaked = {"instructions": "Inspect packet probe_secret."}
    assert validator.blind_json_leaks(leaked, hidden_identifiers=hidden) == [
        "hidden_identifier_value"
    ]


def test_repair_immutability_trigger_contract_rejects_missing_trigger() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    for table in {value[1] for value in validator.REPAIR_TRIGGER_TARGETS.values()}:
        connection.execute(f"CREATE TABLE {table}(value TEXT)")
    assert validator._repair_trigger_failures(connection)


def test_record_hash_replay_rejects_tamper() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE item(value TEXT,record_sha256 TEXT)")
    connection.execute(
        "INSERT INTO item VALUES(?,?)",
        ("a", validator._record_sha({"value": "a"})),
    )
    assert validator._record_hash_failures(connection) == []
    connection.execute("UPDATE item SET value='tampered'")
    assert validator._record_hash_failures(connection) == ["item[0]"]


def test_corrupt_db_fails_closed_and_writes_validation_json(tmp_path: Path) -> None:
    artifact = tmp_path / "corrupt.db"
    artifact.write_bytes(b"not sqlite")
    output = tmp_path / "validation.json"
    result = validator.validate_identity_instrument_v21_probe_repair(
        repo_root=REPO_ROOT,
        artifact_path=artifact,
        validation_report_path=output,
        verify_full_hashes=False,
    )
    assert not result.passed
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert any(row["name"] == "validator_execution" for row in payload["checks"])


def test_validator_does_not_import_any_producer_module() -> None:
    source = Path(validator.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import vnext_identity_instrument_v21_probe_repair",
        "import vnext_identity_instrument_v21_probe",
        "import vnext_identity_instrument_probe",
        "import vnext_identity_band_precision",
        "from canonical import vnext_identity",
    )
    assert all(token not in source for token in forbidden)


def test_cli_is_directly_executable_outside_repo(tmp_path: Path) -> None:
    output = tmp_path / "validation.json"
    command = [
        sys.executable,
        str(
            REPO_ROOT
            / "tools"
            / "validate_vnext_identity_instrument_v21_probe_repair.py"
        ),
        "--repo-root",
        str(tmp_path),
        "--artifact",
        "missing.db",
        "--validation-report",
        str(output),
        "--skip-full-hashes",
    ]
    completed = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert completed.returncode == 1
    assert "WP2.8-R independent validation: FAIL" in completed.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "FAIL"
