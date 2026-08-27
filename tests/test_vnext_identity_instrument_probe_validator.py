from __future__ import annotations

import binascii
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import zlib

import pytest

from canonical import vnext_identity_instrument_probe_validator as validator


def test_stable_rank_is_exact_predeclared_formula() -> None:
    candidate_id = "idc_0123456789abcdef0123456789abcdef"
    expected = hashlib.sha256(
        (
            validator.SAMPLE_VERSION
            + "\0"
            + validator.SAMPLE_SEED
            + "\0P2\0"
            + candidate_id
        ).encode("utf-8")
    ).hexdigest()
    assert validator.stable_rank("P2", candidate_id) == expected


def _gate_rows(p1_non_same: int, p23_non_same: int, d_same: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    rows.extend(
        {"group": "P1", "probe_label": "different" if index < p1_non_same else "same"}
        for index in range(3)
    )
    rows.extend(
        {"group": "P2", "probe_label": "insufficient" if index < p23_non_same else "same"}
        for index in range(4)
    )
    rows.extend(
        {
            "group": "P3",
            "probe_label": "related" if index + 4 < p23_non_same else "same",
        }
        for index in range(2)
    )
    rows.extend(
        {"group": "D", "probe_label": "same" if index < d_same else "different"}
        for index in range(15)
    )
    return rows


def test_gate_exact_thresholds_pass() -> None:
    gate = validator.gate_from_labels(_gate_rows(2, 3, 13))
    assert gate == {
        "counts": {"P1_non_same": 2, "P2_P3_non_same": 3, "D_same": 13},
        "predicates": {
            "p1_non_same_at_least_2_of_3": True,
            "p2_p3_non_same_at_least_3_of_6": True,
            "d_same_at_least_13_of_15": True,
        },
        "status": "pass",
    }


@pytest.mark.parametrize(
    ("p1", "p23", "d"),
    [(1, 6, 15), (3, 2, 15), (3, 6, 12)],
)
def test_gate_fails_when_any_one_predicate_misses(p1: int, p23: int, d: int) -> None:
    assert validator.gate_from_labels(_gate_rows(p1, p23, d))["status"] == "fail"


def test_axis_counter_distinguishes_n100_contract_from_wp25_free_prose() -> None:
    tagged = {
        "rationale": (
            "image=same_object; name=alias; firm=agree; place=unclear; "
            "year=compatible; scope=same; basis=direct evidence"
        )
    }
    prose = {"rationale": "사진과 이름이 같아 동일 프로젝트다."}
    assert validator._axis_counts([tagged]) == {axis: 1 for axis in validator.AXES}
    assert validator._full_axis_count([tagged]) == 1
    assert validator._axis_counts([prose]) == {axis: 0 for axis in validator.AXES}
    assert validator._full_axis_count([prose]) == 0


def test_blind_json_accepts_opaque_wp25_form(tmp_path: Path) -> None:
    path = tmp_path / "safe.json"
    path.write_text(
        json.dumps(
            {
                "lane": "r1",
                "items": [
                    {
                        "display_order": 1,
                        "opaque_review_id": "r1_deadbeef",
                        "sheet_path": "blind/001.png",
                        "sheet_sha256": "a" * 64,
                        "label": "",
                        "rationale": "",
                        "hard_conflict_observed": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert validator._blind_json_leaks(path, {"idc_secret"}) == []


def test_blind_json_rejects_hidden_candidate_group_gold_proposal_score_and_flag(
    tmp_path: Path,
) -> None:
    candidate_id = "idc_0123456789abcdef0123456789abcdef"
    path = tmp_path / "leaky.json"
    path.write_text(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "group": "P1",
                "gold_label": "related",
                "proposal": "merge_candidate",
                "score": 0.99,
                "policy_flags": ["fanout_review_required"],
            }
        ),
        encoding="utf-8",
    )
    leaks = validator._blind_json_leaks(path, {candidate_id})
    assert "$.candidate_id" in leaks
    assert "$.group" in leaks
    assert "$.gold_label" in leaks
    assert "$.proposal" in leaks
    assert "$.score" in leaks
    assert "$.policy_flags" in leaks
    assert "selected_candidate_value" in leaks


def test_sample_manifest_allows_aggregate_counts_but_rejects_per_sample_group(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.json"
    payload = {
        "group_counts": {"P1": 3, "P2": 4, "P3": 2, "D": 15},
        "samples": [
            {
                "sample_id": "n100_sample",
                "seed_rank_sha256": "a" * 64,
                "gold_commitment_sha256": "b" * 64,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert validator._sample_manifest_leaks(path, real_candidate_ids=set()) == []
    payload["samples"][0]["group"] = "P1"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert "$.samples[0].group" in validator._sample_manifest_leaks(
        path, real_candidate_ids=set()
    )


def _quota_row(number: int, phase: str, remaining: float) -> dict[str, object]:
    observed_at = f"2026-08-27T{number + 8:02d}:00:00+09:00"
    return {
        "observation_no": number,
        "phase": phase,
        "account_index": 0,
        "observed_at": observed_at,
        "signals_json": json.dumps(
            [
                {
                    "source": "runtime_header",
                    "account_index": 0,
                    "observed_at": observed_at,
                    "remaining_percent": remaining,
                }
            ]
        ),
        "gate_basis_remaining_percent": remaining,
        "start_allowed": int(remaining > 30),
        "stop_required": int(remaining <= 20),
        "limitation": "fixture",
    }


def test_quota_requires_all_six_boundaries_in_order() -> None:
    phases = (
        "pre_freeze",
        "pre_prepare",
        "pre_r1_review",
        "pre_r2_review",
        "pre_adjudication_review",
        "pre_reveal_score",
    )
    rows = [_quota_row(index, phase, 79) for index, phase in enumerate(phases, 1)]
    assert validator._quota_ledger_failures(rows, phase="final") == []
    assert "missing_wave_phases:pre_reveal_score" in validator._quota_ledger_failures(
        rows[:-1], phase="final"
    )


def test_quota_applies_start_and_stop_thresholds() -> None:
    start_blocked = _quota_row(1, "pre_freeze", 30)
    failures = validator._quota_ledger_failures([start_blocked], phase="sample")
    assert "sample_started_at_or_below_30" in failures
    stopped = _quota_row(1, "pre_freeze", 20)
    next_row = _quota_row(2, "pre_prepare", 19)
    assert "2:activity_after_stop" in validator._quota_ledger_failures(
        [stopped, next_row], phase="final"
    )


def test_input_pin_roles_follow_persisted_prepare_barrier() -> None:
    assert validator._expected_input_roles("sample", "sample_frozen") == (
        validator.SAMPLE_INPUT_ROLES
    )
    assert validator._expected_input_roles("sample", "review_ready") == (
        validator.SAMPLE_INPUT_ROLES | validator.PREPARE_INPUT_ROLES
    )
    assert validator._expected_input_roles("final", "sample_frozen") == (
        validator.SAMPLE_INPUT_ROLES | validator.PREPARE_INPUT_ROLES
    )


def test_gate_status_follows_declared_then_terminal_lifecycle() -> None:
    assert validator._expected_persisted_gate_status("sample_frozen") == "declared"
    assert validator._expected_persisted_gate_status("review_ready") == "declared"
    assert validator._expected_persisted_gate_status("pass") == "pass"
    assert validator._expected_persisted_gate_status("fail") == "fail"


def test_terminal_status_is_case_insensitive_but_value_strict() -> None:
    assert validator._terminal_status_matches("pass", "PASS")
    assert validator._terminal_status_matches("fail", "FAIL")
    assert not validator._terminal_status_matches("fail", "FAILED")
    assert not validator._terminal_status_matches("declared", "DECLARED")


def test_report_binding_normalizes_path_separator_but_keeps_sha_strict(
    tmp_path: Path,
) -> None:
    digest = "a" * 64
    assert validator._resolved_report_binding_matches(
        tmp_path,
        {"path": "data\\reports\\report.json", "sha256": digest},
        expected_path="data/reports/report.json",
        expected_sha256=digest,
    )
    assert not validator._resolved_report_binding_matches(
        tmp_path,
        {"path": "data\\reports\\other.json", "sha256": digest},
        expected_path="data/reports/report.json",
        expected_sha256=digest,
    )
    assert not validator._resolved_report_binding_matches(
        tmp_path,
        {"path": "data\\reports\\report.json", "sha256": "b" * 64},
        expected_path="data/reports/report.json",
        expected_sha256=digest,
    )


def _record_hash_fetch_fixture() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE fetch_assets(
          run_id TEXT, asset_key TEXT, source TEXT, source_asset_id TEXT,
          expected_url TEXT, expected_raw_sha256 TEXT,
          expected_pixel_sha256 TEXT, status TEXT, cache_role TEXT,
          cache_path TEXT, actual_final_url TEXT, actual_raw_sha256 TEXT,
          actual_pixel_sha256 TEXT, http_status INTEGER,
          response_bytes INTEGER, request_count INTEGER, error_kind TEXT,
          error_message TEXT, record_sha256 TEXT
        );
        CREATE TABLE fetch_attempts(
          run_id TEXT, asset_key TEXT, attempt_no INTEGER, status TEXT,
          request_count INTEGER, redirect_chain_json TEXT, started_at TEXT,
          completed_at TEXT, error_kind TEXT, record_sha256 TEXT
        );
        CREATE TABLE probe_reveals(
          run_id TEXT, sample_id TEXT, candidate_id TEXT, group_code TEXT,
          n100_sample_id TEXT, n100_gold_label TEXT, final_probe_label TEXT,
          commitment_sha256 TEXT, record_sha256 TEXT
        );
        """
    )
    initial = {
        "asset_key": "asset",
        "source": "divisare",
        "source_asset_id": "source-asset",
        "expected_url": "https://example.test/image.jpg",
        "expected_raw_sha256": "a" * 64,
        "expected_pixel_sha256": "b" * 64,
        "status": "cache_hit",
        "cache_role": "wp23_n100_review",
        "cache_path": "cache/image.jpg",
        "error_kind": None,
        "error_message": None,
    }
    connection.execute(
        "INSERT INTO fetch_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run",
            initial["asset_key"],
            initial["source"],
            initial["source_asset_id"],
            initial["expected_url"],
            initial["expected_raw_sha256"],
            initial["expected_pixel_sha256"],
            initial["status"],
            initial["cache_role"],
            initial["cache_path"],
            None,
            None,
            None,
            None,
            None,
            0,
            None,
            None,
            validator._record_sha(initial),
        ),
    )
    attempt = {
        "asset_key": "network-asset",
        "attempt_no": 1,
        "status": "failed",
        "request_count": 1,
        "redirect_chain": [],
        "started_at": "2026-08-27T01:00:00Z",
        "completed_at": "2026-08-27T01:00:01Z",
        "error_kind": "connection",
    }
    connection.execute(
        "INSERT INTO fetch_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "run",
            attempt["asset_key"],
            attempt["attempt_no"],
            attempt["status"],
            attempt["request_count"],
            "[]",
            attempt["started_at"],
            attempt["completed_at"],
            attempt["error_kind"],
            validator._record_sha(attempt),
        ),
    )
    reveal = {
        "sample_id": "probe-sample",
        "candidate_id": "candidate",
        "group": "P1",
        "n100_sample_id": "n100-sample",
        "n100_gold_label": "related",
        "final_probe_label": "insufficient",
        "commitment_sha256": "c" * 64,
    }
    connection.execute(
        "INSERT INTO probe_reveals VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "run",
            reveal["sample_id"],
            reveal["candidate_id"],
            reveal["group"],
            reveal["n100_sample_id"],
            reveal["n100_gold_label"],
            reveal["final_probe_label"],
            reveal["commitment_sha256"],
            validator._record_sha(reveal),
        ),
    )
    return connection


def test_record_hash_replay_matches_wp25_cache_and_attempt_payloads() -> None:
    connection = _record_hash_fetch_fixture()
    checks: list[validator.Check] = []
    validator._validate_record_hashes(connection, checks)
    assert checks[-1].passed

    connection.execute(
        "UPDATE fetch_assets SET source_asset_id='tampered' WHERE asset_key='asset'"
    )
    checks = []
    validator._validate_record_hashes(connection, checks)
    assert not checks[-1].passed


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", binascii.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _rgb_png(*, text_chunk: bytes | None = None) -> bytes:
    ihdr = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)
    # These are RGB pixel bytes, not semantic text, despite their ASCII values.
    pixels = b"\x00P1P2P3"
    chunks = [_png_chunk(b"IHDR", ihdr)]
    if text_chunk is not None:
        chunks.append(_png_chunk(b"tEXt", text_chunk))
    chunks.extend(
        [_png_chunk(b"IDAT", zlib.compress(pixels)), _png_chunk(b"IEND", b"")]
    )
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def test_png_leak_scan_ignores_pixel_bytes_but_rejects_text_channel(
    tmp_path: Path,
) -> None:
    safe = tmp_path / "safe.png"
    safe.write_bytes(_rgb_png())
    assert validator._png_structure_leaks(safe) == []

    leaky = tmp_path / "leaky.png"
    leaky.write_bytes(_rgb_png(text_chunk=b"group\x00P1"))
    assert "unexpected_chunk:tEXt" in validator._png_structure_leaks(leaky)


def test_sheet_structured_text_scan_rejects_renderer_input_leak() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE candidate_snapshots(run_id TEXT,sample_id TEXT,"
        "left_metadata_json TEXT,right_metadata_json TEXT)"
    )
    safe = {
        "source": "divisare",
        "name": "Safe building",
        "aliases_json": "[]",
        "firms_json": "[]",
        "country": "France",
    }
    connection.execute(
        "INSERT INTO candidate_snapshots VALUES(?,?,?,?)",
        ("run", "sample", json.dumps(safe), json.dumps(safe)),
    )
    assert validator._sheet_structured_text_leaks(
        connection,
        run_id="run",
        sample_id="sample",
        opaque_review_id="r1_opaque",
        tokens={"P1", "merge_candidate"},
    ) == []
    safe["name"] = "merge_candidate"
    connection.execute(
        "UPDATE candidate_snapshots SET left_metadata_json=?",
        (json.dumps(safe),),
    )
    assert validator._sheet_structured_text_leaks(
        connection,
        run_id="run",
        sample_id="sample",
        opaque_review_id="r1_opaque",
        tokens={"P1", "merge_candidate"},
    ) == ["merge_candidate"]


def _scrub_fixture() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE sample_pairs(
          sample_id TEXT, candidate_id TEXT, stratum TEXT, record_sha256 TEXT
        );
        CREATE TABLE candidate_snapshots(
          sample_id TEXT, candidate_json TEXT, origins_json TEXT,
          families_json TEXT, conflicts_json TEXT, policy_flags_json TEXT,
          transition_json TEXT, record_sha256 TEXT
        );
        CREATE TABLE population_frames(stratum TEXT, count INTEGER);
        """
    )
    connection.execute(
        "INSERT INTO sample_pairs VALUES(?,?,?,?)",
        ("sample", "ctl_" + "a" * 24, "probe", "b" * 64),
    )
    connection.execute(
        "INSERT INTO candidate_snapshots VALUES(?,?,?,?,?,?,?,?)",
        (
            "sample",
            json.dumps(
                {
                    "left_source": "divisare",
                    "left_source_building_id": "left",
                    "right_source": "architizer",
                    "right_source_building_id": "right",
                }
            ),
            "[]",
            "[]",
            "[]",
            "[]",
            "{}",
            "c" * 64,
        ),
    )
    connection.execute("INSERT INTO population_frames VALUES('P1',3)")
    return connection


def test_pre_reveal_sidecar_scrub_accepts_opaque_minimal_snapshot() -> None:
    connection = _scrub_fixture()
    assert validator._pre_reveal_db_leaks(
        connection, real_candidate_ids={"idc_real_candidate"}
    ) == []


def test_pre_reveal_sidecar_scrub_rejects_real_candidate_and_policy_payload() -> None:
    connection = _scrub_fixture()
    connection.execute(
        "UPDATE sample_pairs SET candidate_id=?", ("idc_real_candidate",)
    )
    connection.execute(
        "UPDATE candidate_snapshots SET policy_flags_json=?",
        ('["fanout_review_required"]',),
    )
    failures = validator._pre_reveal_db_leaks(
        connection, real_candidate_ids={"idc_real_candidate"}
    )
    assert any("real_candidate_id" in failure for failure in failures)
    assert any("policy_flags_json" in failure for failure in failures)


def _fetch_fixture(tmp_path: Path) -> tuple[sqlite3.Connection, dict[str, object]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE sample_assets(
          run_id TEXT, sample_id TEXT, side TEXT, asset_key TEXT
        );
        CREATE TABLE fetch_assets(
          run_id TEXT, asset_key TEXT, expected_url TEXT,
          expected_raw_sha256 TEXT, expected_pixel_sha256 TEXT, status TEXT,
          cache_role TEXT, cache_path TEXT, actual_final_url TEXT,
          actual_raw_sha256 TEXT, actual_pixel_sha256 TEXT, http_status INTEGER,
          response_bytes INTEGER, request_count INTEGER, error_kind TEXT,
          error_message TEXT
        );
        CREATE TABLE fetch_attempts(
          run_id TEXT, asset_key TEXT, attempt_no INTEGER, status TEXT,
          request_count INTEGER, redirect_chain_json TEXT, started_at TEXT,
          completed_at TEXT, error_kind TEXT
        );
        CREATE TABLE cache_pins(
          run_id TEXT, role TEXT, path TEXT, transform_version TEXT,
          access_mode TEXT
        );
        """
    )
    transform = "fixture-transform-v1"
    pixel = "b" * 64
    cache_root = tmp_path / "cache"
    materialized = cache_root / validator._cache_relative_path(pixel, transform)
    materialized.parent.mkdir(parents=True)
    materialized.write_bytes(b"fixture")
    connection.execute(
        "INSERT INTO cache_pins VALUES(?,?,?,?,?)",
        ("run", "wp26_output", "cache", transform, "write_only"),
    )
    connection.execute(
        "INSERT INTO sample_assets VALUES(?,?,?,?)", ("run", "sample", "left", "asset")
    )
    connection.execute(
        "INSERT INTO fetch_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run",
            "asset",
            "https://example/source.jpg",
            "a" * 64,
            pixel,
            "exact",
            "wp26_output",
            materialized.relative_to(tmp_path).as_posix(),
            "https://example/final.jpg",
            "a" * 64,
            pixel,
            200,
            100,
            1,
            None,
            None,
        ),
    )
    connection.execute(
        "INSERT INTO fetch_attempts VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "run",
            "asset",
            1,
            "exact",
            1,
            "[]",
            "2026-08-27T01:00:00Z",
            "2026-08-27T01:00:01Z",
            None,
        ),
    )
    run = {
        "run_id": "run",
        "asset_occurrence_count": 1,
        "unique_asset_count": 1,
        "logical_http_fetches": 1,
        "wire_http_requests": 1,
    }
    return connection, run


def _fetch_checks(
    tmp_path: Path, connection: sqlite3.Connection, run: dict[str, object]
) -> dict[str, validator.Check]:
    checks: list[validator.Check] = []
    validator._validate_fetch(tmp_path.resolve(), connection, run, checks, {})
    return {check.name: check for check in checks}


def test_fetch_validator_accepts_exact_one_shot_binding(tmp_path: Path) -> None:
    connection, run = _fetch_fixture(tmp_path)
    assert all(check.passed for check in _fetch_checks(tmp_path, connection, run).values())


def test_fetch_validator_rejects_false_exact_hash_and_redirect_retry(tmp_path: Path) -> None:
    connection, run = _fetch_fixture(tmp_path)
    connection.execute("UPDATE fetch_assets SET actual_raw_sha256=?", ("c" * 64,))
    connection.execute(
        "UPDATE fetch_attempts SET attempt_no=2,redirect_chain_json=?",
        ('[{"http_status":302}]',),
    )
    checks = _fetch_checks(tmp_path, connection, run)
    assert not checks["fetch_status_hash_semantics"].passed
    assert not checks["fetch_redirects_retries_disabled"].passed


def _fetch_cap_correction_fixture(tmp_path: Path) -> dict[str, object]:
    root = tmp_path.resolve()
    producer = root / validator.PROBE_PRODUCER_PATH
    producer.parent.mkdir(parents=True)
    producer.write_bytes(b"current producer fixture\n")
    fetch_manifest_path = root / "reports" / "fetch.json"
    fetch_manifest_path.parent.mkdir(parents=True)
    fetch_manifest = {
        "run_id": "run",
        "asset_occurrence_count": 3,
        "logical_http_fetches": 2,
        "wire_http_requests": 2,
        "hard_caps": dict(validator.LEGACY_WP25_FETCH_CAPS),
        "payload": {"kept": "byte-for-byte"},
    }
    fetch_manifest_path.write_text(
        json.dumps(fetch_manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    artifact_path = root / "data" / "probe.db"
    stage_report_path = root / "reports" / "report.json"
    manifest_path = root / "reports" / "manifest.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"terminal db")
    stage_report_path.write_bytes(b'{"status":"FAIL"}\n')
    manifest_path.write_bytes(b'{"status":"FAIL"}\n')

    def file_binding(path: Path) -> dict[str, object]:
        return {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": validator._sha256_file(path),
        }

    actual_counts = {
        "asset_occurrences": 3,
        "logical_http_fetches": 2,
        "wire_http_requests": 2,
        "redirects": 0,
        "retries": 0,
    }
    corrected = dict(fetch_manifest)
    corrected["hard_caps"] = dict(validator.FETCH_CAPS)
    correction = {
        "status": "CORRECTION",
        "correction_version": validator.FETCH_CAP_CORRECTION_VERSION,
        "run_id": "run",
        "scope": "fetch_manifest.hard_caps",
        "cause": {
            "code": "reused_wp25_fetch_manifest_literal",
            "detail": validator.FETCH_CAP_CORRECTION_CAUSE,
        },
        "original_fetch_manifest": {
            **file_binding(fetch_manifest_path),
            "recorded_hard_caps": dict(validator.LEGACY_WP25_FETCH_CAPS),
        },
        "corrected_contract": {
            "hard_caps": dict(validator.FETCH_CAPS),
            "actual_counts": actual_counts,
            "within_hard_caps": True,
        },
        "corrected_projection": {
            "canonicalization": "canonical-json-v1",
            "operation": "replace-hard_caps-only",
            "sha256": validator._record_sha(corrected),
        },
        "terminal_bindings": {
            "database": file_binding(artifact_path),
            "report": file_binding(stage_report_path),
            "manifest": file_binding(manifest_path),
        },
        "producer_bindings": {
            "runtime": {
                "path": validator.PROBE_PRODUCER_PATH.as_posix(),
                "size_bytes": validator.PROBE_PRODUCER_RUNTIME_SIZE,
                "sha256": validator.PROBE_PRODUCER_RUNTIME_SHA256,
            },
            "current": file_binding(producer),
        },
    }
    correction_path = root / "reports" / "correction.json"
    correction_path.write_text(
        json.dumps(correction, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    terminal = {
        "run_id": "run",
        "report_path": stage_report_path.relative_to(root).as_posix(),
        "report_sha256": validator._sha256_file(stage_report_path),
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest_sha256": validator._sha256_file(manifest_path),
    }
    return {
        "root": root,
        "producer": producer,
        "fetch_manifest_path": fetch_manifest_path,
        "fetch_manifest": fetch_manifest,
        "artifact_path": artifact_path,
        "stage_report_path": stage_report_path,
        "manifest_path": manifest_path,
        "actual_counts": actual_counts,
        "correction": correction,
        "correction_path": correction_path,
        "terminal": terminal,
    }


def _fetch_cap_correction_failures(fixture: dict[str, object]) -> list[str]:
    return validator._fetch_cap_correction_failures(
        fixture["root"],
        correction_path=fixture["correction_path"],
        fetch_manifest_path=fixture["fetch_manifest_path"],
        fetch_manifest=fixture["fetch_manifest"],
        run_id="run",
        actual_counts=fixture["actual_counts"],
        artifact_path=fixture["artifact_path"],
        stage_report_path=fixture["stage_report_path"],
        manifest_path=fixture["manifest_path"],
        terminal=fixture["terminal"],
    )


def test_legacy_fetch_cap_correction_accepts_exact_bindings(tmp_path: Path) -> None:
    fixture = _fetch_cap_correction_fixture(tmp_path)
    assert _fetch_cap_correction_failures(fixture) == []


def test_legacy_fetch_cap_correction_rejects_missing_overlay(tmp_path: Path) -> None:
    fixture = _fetch_cap_correction_fixture(tmp_path)
    fixture["correction_path"].unlink()
    assert _fetch_cap_correction_failures(fixture) == ["correction:missing"]


@pytest.mark.parametrize(
    ("mutator", "expected_failure"),
    [
        (
            lambda payload: payload["corrected_projection"].__setitem__(
                "sha256", "0" * 64
            ),
            "correction:corrected_projection",
        ),
        (
            lambda payload: payload["original_fetch_manifest"].__setitem__(
                "sha256", "0" * 64
            ),
            "original_fetch_manifest:sha256",
        ),
        (
            lambda payload: payload["terminal_bindings"]["database"].__setitem__(
                "sha256", "0" * 64
            ),
            "terminal_bindings.database:sha256",
        ),
    ],
)
def test_legacy_fetch_cap_correction_rejects_tamper_and_wrong_hash(
    tmp_path: Path, mutator: object, expected_failure: str
) -> None:
    fixture = _fetch_cap_correction_fixture(tmp_path)
    payload = fixture["correction"]
    mutator(payload)
    fixture["correction_path"].write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    assert expected_failure in _fetch_cap_correction_failures(fixture)


def test_future_native_96_fetch_manifest_needs_no_correction(tmp_path: Path) -> None:
    fetch_manifest_path = tmp_path / "fetch.json"
    fetch_manifest_path.write_text(
        json.dumps(
            {
                "run_id": "run",
                "asset_occurrence_count": 3,
                "logical_http_fetches": 2,
                "wire_http_requests": 2,
                "hard_caps": validator.FETCH_CAPS,
            }
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    checks: list[validator.Check] = []
    metrics: dict[str, object] = {}
    validator._validate_fetch_manifest_cap_contract(
        tmp_path.resolve(),
        connection,
        {
            "run_id": "run",
            "asset_occurrence_count": 3,
            "logical_http_fetches": 2,
            "wire_http_requests": 2,
        },
        checks,
        metrics,
        paths={"fetch_manifest": fetch_manifest_path},
        artifact_path=tmp_path / "absent.db",
        stage_report_path=tmp_path / "absent-report.json",
        manifest_path=tmp_path / "absent-manifest.json",
    )
    assert checks[-1].passed
    assert metrics["fetch_manifest_cap_contract"]["mode"] == "native-96"
    assert not (tmp_path / validator.DEFAULT_FETCH_CAP_CORRECTION).exists()


def test_json_safe_set_and_frozenset_serialization_is_stably_sorted() -> None:
    value_a = {"set": {"z", "a", "m"}, "frozen": frozenset({"3", "1", "2"})}
    value_b = {
        "set": set(reversed(["z", "a", "m"])),
        "frozen": frozenset(reversed(["3", "1", "2"])),
    }
    expected = {"set": ["a", "m", "z"], "frozen": ["1", "2", "3"]}
    assert validator._json_safe(value_a) == expected
    assert validator._json_safe(value_b) == expected
    assert validator._canonical_json(validator._json_safe(value_a)) == validator._canonical_json(
        validator._json_safe(value_b)
    )


def test_validator_fails_closed_when_artifact_is_missing(tmp_path: Path) -> None:
    result = validator.validate_identity_instrument_probe(
        repo_root=tmp_path,
        artifact_path=Path("missing.db"),
        report_path=None,
        verify_full_hashes=False,
        phase="sample",
    )
    assert not result.passed
    assert result.failed_check_names == ("artifact_exists",)


def test_validator_does_not_import_any_producer() -> None:
    source = Path(validator.__file__).read_text(encoding="utf-8")
    assert "import vnext_identity_instrument_probe" not in source
    assert "import vnext_identity_band_precision" not in source
    assert "import vnext_identity_n100" not in source
    assert "from canonical.vnext_identity" not in source


def test_validator_cli_is_directly_executable_outside_repo_cwd(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "tools" / "validate_vnext_identity_instrument_probe.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "independent WP2.6 instrument-probe validator" in completed.stdout
