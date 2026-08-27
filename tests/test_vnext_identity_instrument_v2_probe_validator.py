from __future__ import annotations

import binascii
import hashlib
import json
import sqlite3
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from canonical import vnext_identity_instrument_v2_probe_validator as validator


REPO_ROOT = Path(__file__).resolve().parents[1]


def _observation(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "image": "same_object",
        "name": "agree",
        "firm": "agree",
        "place": "agree",
        "year": "compatible",
        "scope": "same",
        "basis": "visible evidence supports these observations",
    }
    result.update(updates)
    return result


EXPECTED_TRUTH_TABLE = {
    ("inconclusive", "same"): ("insufficient", 1),
    ("inconclusive", "component_vs_umbrella"): ("insufficient", 1),
    ("inconclusive", "phase_or_extension"): ("insufficient", 1),
    ("inconclusive", "unclear"): ("insufficient", 1),
    ("inconclusive", "unrelated"): ("insufficient", 1),
    ("different_objects", "same"): ("different", 2),
    ("different_objects", "component_vs_umbrella"): ("different", 2),
    ("different_objects", "phase_or_extension"): ("different", 2),
    ("different_objects", "unclear"): ("different", 2),
    ("different_objects", "unrelated"): ("different", 2),
    ("same_object", "same"): ("same", 4),
    ("same_object", "component_vs_umbrella"): ("related", 3),
    ("same_object", "phase_or_extension"): ("related", 3),
    ("same_object", "unclear"): ("insufficient", 5),
    ("same_object", "unrelated"): ("different", 2),
}


def test_derivation_exhaustively_covers_all_15_image_scope_pairs() -> None:
    assert validator.derivation_truth_table() == EXPECTED_TRUTH_TABLE
    assert len(validator.derivation_truth_table()) == 15


def test_actor_instructions_are_exact_complete_and_label_free() -> None:
    instructions = validator._expected_axis_instructions()
    assert instructions["observation_keys"] == list(validator.OBSERVATION_KEYS)
    assert instructions["controlled_values"] == {
        key: list(validator.CONTROLLED_VALUE_ORDER[key])
        for key in validator.OBSERVATION_KEYS[:-1]
    }
    assert instructions["basis"] == {
        "required": True,
        "format": "trimmed_non_empty_text",
    }
    assert instructions["image_inconclusive"]["comparison_unavailable_is_normal"] is True
    assert "not equal project scope" in instructions["scope_guidance"]["matching_photographs"]
    assert "scope=component_vs_umbrella" in instructions["scope_guidance"]["umbrella"]
    assert validator.blind_json_leaks(instructions) == []


@pytest.mark.parametrize(
    ("axis", "values"),
    [
        ("name", validator.CONTROLLED_VALUES["name"]),
        ("firm", validator.CONTROLLED_VALUES["firm"]),
        ("place", validator.CONTROLLED_VALUES["place"]),
        ("year", validator.CONTROLLED_VALUES["year"]),
    ],
)
def test_non_decision_axes_are_required_but_cannot_change_label(
    axis: str, values: frozenset[str]
) -> None:
    outcomes = {validator.derive_label(_observation(**{axis: value})) for value in values}
    assert outcomes == {("same", 4)}


def test_observation_contract_accepts_exact_enum_and_nonempty_basis() -> None:
    assert validator.observation_failures(_observation()) == []


@pytest.mark.parametrize(
    ("mutation", "failure"),
    [
        (lambda row: row.pop("scope"), "missing:scope"),
        (lambda row: row.__setitem__("extra", "x"), "extra:extra"),
        (lambda row: row.__setitem__("image", "Same_Object"), "image:controlled_value"),
        (lambda row: row.__setitem__("scope", "component"), "scope:controlled_value"),
        (lambda row: row.__setitem__("basis", "  "), "basis:non_empty"),
        (lambda row: row.__setitem__("basis", " untrimmed"), "basis:trimmed"),
        (lambda row: row.__setitem__("basis", None), "basis:non_empty"),
    ],
)
def test_observation_contract_rejects_missing_extra_synonym_case_and_blank(
    mutation: object, failure: str
) -> None:
    row = _observation()
    mutation(row)
    assert failure in validator.observation_failures(row)


@pytest.mark.parametrize("forbidden", sorted(validator.FORBIDDEN_INPUT_KEYS))
def test_form_contract_rejects_every_label_or_legacy_input_key(forbidden: str) -> None:
    item = {
        "display_order": 1,
        "opaque_review_id": "r1_opaque",
        "sheet_path": "sheet.png",
        "sheet_sha256": "a" * 64,
        **_observation(),
        forbidden: "same",
    }
    failures = validator.form_item_failures(item)
    assert f"forbidden:{forbidden}" in failures or f"extra:{forbidden}" in failures


def _gate_fixture() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    # P1: 2 insufficient + 1 related gold.  Two non-same outputs.
    rows.extend(
        [
            {"id": "p1-i1", "group": "P1", "gold": "insufficient", "v2_label": "insufficient"},
            {"id": "p1-i2", "group": "P1", "gold": "insufficient", "v2_label": "different"},
            {"id": "p1-r1", "group": "P1", "gold": "related", "v2_label": "same"},
        ]
    )
    # P2: 3 insufficient + 1 related.  Two more exact insufficient and the
    # sole required exact related.
    rows.extend(
        [
            {"id": "p2-i1", "group": "P2", "gold": "insufficient", "v2_label": "insufficient"},
            {"id": "p2-i2", "group": "P2", "gold": "insufficient", "v2_label": "insufficient"},
            {"id": "p2-i3", "group": "P2", "gold": "insufficient", "v2_label": "different"},
            {"id": "p2-r1", "group": "P2", "gold": "related", "v2_label": "related"},
        ]
    )
    # P3 contributes the fourth exact insufficient.
    rows.extend(
        [
            {"id": "p3-i1", "group": "P3", "gold": "insufficient", "v2_label": "insufficient"},
            {"id": "p3-i2", "group": "P3", "gold": "insufficient", "v2_label": "different"},
        ]
    )
    rows.extend(
        {
            "id": f"d-{index:02d}",
            "group": "D",
            "gold": "same",
            "v2_label": "same" if index <= 13 else "different",
        }
        for index in range(1, 16)
    )
    return rows


def test_four_gate_exact_boundaries_pass() -> None:
    gate = validator.gate_from_rows(_gate_fixture())
    assert gate["status"] == "pass"
    assert gate["counts"] == {
        "d_same": 13,
        "p1_non_same": 2,
        "gold_insufficient_to_insufficient": 4,
        "gold_related_to_related": 1,
    }
    assert all(item["passed"] for item in gate["predicates"].values())


@pytest.mark.parametrize(
    ("target_id", "new_label", "failed_predicate"),
    [
        ("d-13", "different", "d_same"),
        ("p1-i2", "same", "p1_non_same"),
        ("p2-i2", "different", "gold_insufficient_to_insufficient"),
        ("p2-r1", "same", "gold_related_to_related"),
    ],
)
def test_each_single_gate_miss_forces_fail(
    target_id: str, new_label: str, failed_predicate: str
) -> None:
    rows = _gate_fixture()
    next(row for row in rows if row["id"] == target_id)["v2_label"] = new_label
    gate = validator.gate_from_rows(rows)
    assert gate["status"] == "fail"
    assert gate["predicates"][failed_predicate]["passed"] is False
    assert sum(not value["passed"] for value in gate["predicates"].values()) == 1


def test_blind_leak_scan_rejects_nested_n100_v1_candidate_group_and_score() -> None:
    payload = {
        "items": [
            {
                "nested": {
                    "v1_probe_label": "same",
                    "group_code": "P1",
                    "score": 1.0,
                    "innocent": "idc_secret",
                }
            }
        ]
    }
    failures = validator.blind_json_leaks(
        payload,
        candidate_ids={"idc_secret"},
        n100_sample_ids={"n100_secret"},
    )
    assert "$.items[0].nested.v1_probe_label" in failures
    assert "$.items[0].nested.group_code" in failures
    assert "$.items[0].nested.score" in failures
    assert "hidden_identifier_value" in failures


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )


def _rgb_png(*, text_chunk: bytes | None = None) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\x10\x20\x30"
    chunks = [_png_chunk(b"IHDR", ihdr)]
    if text_chunk is not None:
        chunks.append(_png_chunk(b"tEXt", text_chunk))
    chunks.extend([_png_chunk(b"IDAT", zlib.compress(raw)), _png_chunk(b"IEND", b"")])
    return signature + b"".join(chunks)


def test_png_contract_accepts_pixels_but_rejects_metadata_channel(tmp_path: Path) -> None:
    safe = tmp_path / "safe.png"
    safe.write_bytes(_rgb_png())
    assert validator._png_structure_failures(safe) == []
    leaky = tmp_path / "leaky.png"
    leaky.write_bytes(_rgb_png(text_chunk=b"v1_probe_label\x00same"))
    assert "unexpected_chunk:tEXt" in validator._png_structure_failures(leaky)


def _quota_rows(remaining: float = 79.0) -> list[dict[str, object]]:
    rows = []
    for index, phase in enumerate(validator.QUOTA_PHASES, 1):
        rows.append(
            {
                "observation_no": index,
                "phase": phase,
                "account_index": 0,
                "observed_at": f"2026-08-27T0{index}:00:00+09:00",
                "source": "test header",
                "remaining_percent": remaining,
                "start_allowed": int(remaining > 30),
                "stop_required": int(remaining <= 20),
                "limitation": "synthetic fixture",
            }
        )
    return rows


def test_quota_contract_accepts_all_ordered_boundaries() -> None:
    assert validator.quota_ledger_failures(_quota_rows()) == []


def test_quota_contract_rejects_30_percent_wave_and_activity_after_sticky_stop() -> None:
    denied = _quota_rows(30.0)
    assert any("wave_not_authorized" in item for item in validator.quota_ledger_failures(denied))
    sticky = _quota_rows()
    sticky[2]["remaining_percent"] = 20.0
    sticky[2]["start_allowed"] = 0
    sticky[2]["stop_required"] = 1
    assert any("activity_after_stop" in item for item in validator.quota_ledger_failures(sticky))


def test_quota_contract_accepts_same_phase_denial_then_fresh_authorization() -> None:
    rows = _quota_rows()
    denied = dict(rows[1])
    denied.update({"remaining_percent": 30.0, "start_allowed": 0, "stop_required": 0})
    rows.insert(1, denied)
    for index, row in enumerate(rows, 1):
        row["observation_no"] = index
        row["observed_at"] = f"2026-08-27T{index:02d}:00:00+09:00"
    assert validator.quota_ledger_failures(rows) == []


def test_quota_contract_rejects_unresolved_same_phase_denial() -> None:
    rows = _quota_rows()
    rows[-1].update({"remaining_percent": 30.0, "start_allowed": 0, "stop_required": 0})
    assert "pre_reveal_score:wave_not_authorized" in validator.quota_ledger_failures(rows)


def test_wp26_authoritative_48_row_packet_binding_sha() -> None:
    source = validator._source_snapshot(REPO_ROOT, verify_full_hashes=False)
    rows = [
        {
            key: row[key]
            for key in (
                "lane",
                "display_order",
                "opaque_review_id",
                "sample_id",
                "sheet_path",
                "sheet_sha256",
                "sheet_bytes",
            )
        }
        for row in source["sheets"]
    ]
    assert validator._record_sha(rows) == (
        "b1f0491852d68648405c032e1a8486ed41a939b18eb655cea1aab92ab07b39e5"
    )


def test_wp26_visual_inventory_sha_and_total_bytes() -> None:
    source = validator._source_snapshot(REPO_ROOT, verify_full_hashes=False)
    assert validator._record_sha(source["inventory_rows"]) == (
        "0cdf2b6b42b9a4866a2e9ee0cb37e5efb9c045915ce83e67d350569749bbe99e"
    )
    assert sum(row["bytes"] for row in source["inventory_rows"]) == 24_517_369


def test_record_hash_replay_accepts_one_canonical_payload_and_rejects_tamper() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE fixture(run_id TEXT,value TEXT,record_sha256 TEXT)")
    payload = {"run_id": "run", "value": "frozen"}
    connection.execute(
        "INSERT INTO fixture VALUES(?,?,?)",
        (payload["run_id"], payload["value"], validator._record_sha(payload)),
    )
    assert validator._record_hash_failures(connection) == []
    connection.execute("UPDATE fixture SET value='tampered'")
    assert validator._record_hash_failures(connection) == ["fixture[0]"]


def test_db_first_artifact_recovery_binding_rejects_payload_hex_tamper(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE artifacts(run_id TEXT,role TEXT,path TEXT,stage TEXT,size_bytes INTEGER,"
        "sha256 TEXT,payload_hex TEXT,created_at TEXT,record_sha256 TEXT)"
    )
    connection.execute(
        "CREATE TABLE artifact_paths(run_id TEXT,role TEXT,path TEXT,stage TEXT,record_sha256 TEXT)"
    )
    connection.execute(
        "CREATE TABLE artifact_bindings(run_id TEXT,role TEXT,path TEXT,size_bytes INTEGER,"
        "sha256 TEXT,stage TEXT,record_sha256 TEXT)"
    )
    connection.execute(
        "CREATE TABLE json_payloads(run_id TEXT,role TEXT,payload_json TEXT,record_sha256 TEXT)"
    )
    payload = {"status": "PASS"}
    raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    path = tmp_path / "sample.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    connection.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
        ("run", "sample_manifest", str(path), "sample_freeze", len(raw), digest,
         raw.hex(), "2026-08-27T00:00:00+00:00", "x"),
    )
    connection.execute(
        "INSERT INTO artifact_paths VALUES(?,?,?,?,?)",
        ("run", "sample_manifest", str(path), "sample_freeze", "x"),
    )
    connection.execute(
        "INSERT INTO artifact_bindings VALUES(?,?,?,?,?,?,?)",
        ("run", "sample_manifest", str(path), len(raw), digest, "sample_freeze", "x"),
    )
    connection.execute(
        "INSERT INTO json_payloads VALUES(?,?,?,?)",
        ("run", "sample_manifest", validator._canonical_json(payload), "x"),
    )
    _, failures = validator._artifact_map(
        tmp_path, connection, verify_full_hashes=True, phase="sample"
    )
    assert failures == []
    connection.execute("UPDATE artifacts SET payload_hex='00'")
    _, failures = validator._artifact_map(
        tmp_path, connection, verify_full_hashes=True, phase="sample"
    )
    assert "sample_manifest:payload_sha256" in failures
    assert "sample_manifest:db_first_recovery_bytes" in failures


def test_validator_fails_closed_when_artifact_is_missing(tmp_path: Path) -> None:
    output = tmp_path / "validation.json"
    result = validator.validate_identity_instrument_v2_probe(
        repo_root=tmp_path,
        artifact_path=Path("missing.db"),
        validation_report_path=output,
        verify_full_hashes=False,
    )
    assert not result.passed
    assert result.failed_check_names == ("artifact_exists",)
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "FAIL"


def test_validator_fails_closed_and_writes_report_for_corrupt_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "corrupt.db"
    artifact.write_bytes(b"not a sqlite database")
    output = tmp_path / "validation.json"
    monkeypatch.setattr(
        validator,
        "_source_snapshot",
        lambda root, verify_full_hashes: {},
    )
    result = validator.validate_identity_instrument_v2_probe(
        repo_root=tmp_path,
        artifact_path=artifact,
        validation_report_path=output,
        verify_full_hashes=False,
    )
    assert not result.passed
    assert "validator_execution" in result.failed_check_names
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert any(check["name"] == "validator_execution" for check in report["checks"])


def test_validator_does_not_import_any_identity_producer() -> None:
    source = Path(validator.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import vnext_identity_instrument_v2_probe",
        "import vnext_identity_instrument_probe",
        "import vnext_identity_band_precision",
        "import vnext_identity_n100",
        "from canonical.vnext_identity",
    )
    assert all(token not in source for token in forbidden)


def test_sample_phase_accepts_exact_stage_a_b_c_boundary() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE state_events(
          event_no INTEGER, from_status TEXT, to_status TEXT,
          occurred_at TEXT, detail_json TEXT
        );
        CREATE TABLE runs(status TEXT);
        CREATE TABLE gate_contract(gate_sha256 TEXT);
        CREATE TABLE quota_observations(
          observation_no INTEGER, phase TEXT, observed_at TEXT,
          remaining_percent REAL, start_allowed INTEGER, stop_required INTEGER,
          source TEXT, limitation TEXT
        );
        """
    )
    gate_sha = "a" * 64
    details = (
        {"stage": "A"},
        {"stage": "B"},
        {
            "stage": "C",
            "gate_sha256": gate_sha,
            "pre_form_count": 0,
            "pre_review_count": 0,
            "pre_adjudication_count": 0,
        },
    )
    states = validator.STATE_SEQUENCE[:3]
    for index, (state, detail) in enumerate(zip(states, details), 1):
        connection.execute(
            "INSERT INTO state_events VALUES(?,?,?,?,?)",
            (
                index,
                "none" if index == 1 else states[index - 2],
                state,
                f"2026-08-27T00:00:0{index}Z",
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
            ),
        )
    connection.execute("INSERT INTO runs VALUES('criteria_frozen')")
    connection.execute("INSERT INTO gate_contract VALUES(?)", (gate_sha,))
    connection.execute(
        "INSERT INTO quota_observations VALUES(1,'pre_freeze',"
        "'2026-08-27T00:00:00Z',79,1,0,'fixture','')"
    )
    checks: list[validator.Check] = []
    validator._validate_state_and_quota(connection, checks, phase="sample")
    assert checks and all(check.passed for check in checks)
    connection.close()


def test_validator_cli_is_directly_executable_outside_repo_cwd(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "validate_vnext_identity_instrument_v2_probe.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "independent WP2.7 identity-instrument v2" in completed.stdout


def test_canonical_json_hash_is_stable() -> None:
    left = {"z": [3, 2, 1], "a": {"b": "c"}}
    right = {"a": {"b": "c"}, "z": [3, 2, 1]}
    assert validator._record_sha(left) == validator._record_sha(right)
    assert validator._record_sha(left) == hashlib.sha256(
        json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_terminal_manifest_inherited_fetch_is_provenance_only() -> None:
    inherited = validator._expected_inherited_fetch_manifest()
    assert inherited["asset_occurrences"] == 93
    assert inherited["logical_http_fetches"] == 17
    assert inherited["wire_http_requests"] == 17
    assert inherited["source_correction"]["native_hard_caps"] == {
        "asset_occurrences": 96,
        "logical_http_fetches": 96,
        "wire_http_requests": 96,
        "redirects": 0,
        "retries": 0,
    }
    serialized = json.dumps(inherited, sort_keys=True)
    assert all(
        token not in serialized
        for token in ("three_way_comparison", "n100_gold_label", "v1_probe_label")
    )
