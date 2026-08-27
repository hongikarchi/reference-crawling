from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from canonical import vnext_identity_instrument_v21_probe_validator as validator


REPO_ROOT = Path(__file__).resolve().parents[1]


def _observation(**changes: str) -> dict[str, str]:
    value = {
        "image": "same_object", "name": "agree", "firm": "agree",
        "place": "unclear", "year": "compatible", "scope": "same",
        "basis": "fixture evidence",
    }
    value.update(changes)
    return value


def test_exact_n100_parser_allows_semicolons_only_in_basis() -> None:
    parsed = validator.parse_n100_rationale(
        "image=same_object; name=alias; firm=overlap; place=unclear; "
        "year=compatible; scope=same; basis=first clause; second clause"
    )
    assert parsed["basis"] == "first clause; second clause"
    with pytest.raises(ValueError):
        validator.parse_n100_rationale(
            "image=same_object; name=alias; firm=unknown; place=unclear; "
            "year=compatible; scope=same; basis=x"
        )


def test_consensus_priority_and_derivation_are_independent_and_exact() -> None:
    left = _observation(image="same_object", name="agree", firm="agree", scope="same")
    right = _observation(image="inconclusive", name="alias", firm="conflict", scope="unrelated")
    result = validator.consensus_axes(left, right)
    assert result == {
        "image": "inconclusive", "name": "alias", "firm": "conflict",
        "place": "unclear", "year": "compatible", "scope": "unrelated",
    }
    assert validator.derive_label(result) == ("insufficient", 1)
    assert validator.derive_label(_observation(image="different_objects")) == ("different", 2)
    assert validator.derive_label(_observation(scope="related_separate")) == ("related", 3)
    assert validator.derive_label(_observation()) == ("same", 4)
    assert validator.derive_label(_observation(scope="unclear")) == ("insufficient", 5)


def test_real_n100_replay_is_102_of_105_with_three_frozen_mismatches() -> None:
    snapshot = validator._n100_snapshot(REPO_ROOT, verify_full_hashes=True)
    assert snapshot["matches"] == 102
    assert snapshot["rule_counts"] == validator.EXPECTED_RULE_COUNTS
    assert tuple((row["n100_sample_id"], row["predicted_label"], row["gold_label"])
                 for row in snapshot["mismatches"]) == validator.EXPECTED_MISMATCHES
    assert snapshot["observed"] == validator.EXPECTED_VOCAB_COUNTS


def _gate_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    rows.extend({"group_code": "D", "image": "same_object", "scope": "same",
                 "derived_label": "same", "n100_uncertain": False} for _ in range(15))
    rows.extend({"group_code": "P1", "image": "same_object", "scope": "same",
                 "derived_label": "different" if index < 2 else "same",
                 "n100_uncertain": index == 0} for index in range(3))
    rows.extend({"group_code": "P2", "image": "inconclusive" if index < 2 else "same_object",
                 "scope": "same", "derived_label": "insufficient",
                 "n100_uncertain": index < 2} for index in range(4))
    rows.extend({"group_code": "P3", "image": "unavailable", "scope": "same",
                 "derived_label": "insufficient", "n100_uncertain": True} for _ in range(2))
    return rows


def test_four_gate_boundaries_and_and_combiner() -> None:
    gate = validator.gate_from_rows(_gate_rows())
    assert gate["status"] == "pass"
    assert gate["counts"] == {
        "d_image_same_object": 15, "d_scope_same": 15,
        "n100_image_non_same_object": 4, "p1_non_same_label": 2,
    }
    rows = _gate_rows()
    rows[0]["image"] = "inconclusive"
    rows[1]["image"] = "inconclusive"
    assert validator.gate_from_rows(rows)["status"] == "fail"


def test_blind_leak_scan_catches_nested_label_gold_group_candidate_and_ids() -> None:
    payload = {"items": [{"candidate_id": "secret", "nested": {"gold_label": "same"}}],
               "group": "P1"}
    failures = validator.blind_json_leaks(payload, hidden_identifiers=("secret",))
    assert failures
    clean = {"items": [{"opaque_review_id": "r1_x", "image": "same_object"}]}
    assert validator.blind_json_leaks(clean) == []


def test_quota_order_and_sticky_stop_fail_closed() -> None:
    rows = []
    for number, phase in enumerate(validator.QUOTA_PHASES, 1):
        rows.append({"observation_no": number, "phase": phase,
                     "observed_at": f"2026-08-27T00:00:0{number}Z",
                     "remaining_percent": 79, "start_allowed": 1, "stop_required": 0})
    assert validator.quota_ledger_failures(rows) == []
    rows[2]["remaining_percent"] = 20
    rows[2]["start_allowed"] = 0
    rows[2]["stop_required"] = 1
    assert any("activity_after_stop" in item for item in validator.quota_ledger_failures(rows))


def test_corrupt_db_fails_closed_and_writes_json(tmp_path: Path) -> None:
    artifact = tmp_path / "corrupt.db"
    artifact.write_bytes(b"not sqlite")
    output = tmp_path / "validation.json"
    result = validator.validate_identity_instrument_v21_probe(
        repo_root=REPO_ROOT, artifact_path=artifact,
        validation_report_path=output, verify_full_hashes=False,
    )
    assert not result.passed
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert any(row["name"] == "validator_execution" for row in payload["checks"])


def test_validator_does_not_import_or_execute_identity_producer() -> None:
    source = Path(validator.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import vnext_identity_instrument_v21_probe",
        "from canonical.vnext_identity_instrument_v21_probe import",
        "from canonical import vnext_identity_instrument_v21_probe",
    )
    assert all(token not in source for token in forbidden)


def test_cli_is_directly_executable_outside_repo(tmp_path: Path) -> None:
    output = tmp_path / "validation.json"
    command = [sys.executable, str(REPO_ROOT / "tools" /
               "validate_vnext_identity_instrument_v21_probe.py"),
               "--repo-root", str(tmp_path), "--artifact", "missing.db",
               "--validation-report", str(output), "--skip-full-hashes"]
    completed = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert completed.returncode == 1
    assert "WP2.8 independent validation: FAIL" in completed.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "FAIL"


def test_record_hash_replay_rejects_tamper() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE item(value TEXT,record_sha256 TEXT)")
    connection.execute("INSERT INTO item VALUES(?,?)", ("a", validator._record_sha({"value": "a"})))
    assert validator._record_hash_failures(connection) == []
    connection.execute("UPDATE item SET value='b'")
    assert validator._record_hash_failures(connection) == ["item[0]"]
