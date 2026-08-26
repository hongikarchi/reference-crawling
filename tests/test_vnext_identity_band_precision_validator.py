from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from canonical import vnext_identity_band_precision_validator as validator


def _frame_fixture() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE human_review_queue(
          candidate_id TEXT PRIMARY KEY, evidence_strength_band TEXT NOT NULL
        );
        CREATE TABLE candidate_policy_flags(candidate_id TEXT, flag_type TEXT);
        CREATE TABLE candidate_transitions(
          candidate_id TEXT PRIMARY KEY, v3_proposal TEXT NOT NULL,
          v4_proposal TEXT NOT NULL, v3_hard_conflicts_json TEXT NOT NULL,
          r1_changed INTEGER NOT NULL
        );
        CREATE TABLE hard_conflicts(candidate_id TEXT, conflict_type TEXT);
        """
    )

    queue_rows = [
        ("s1_good", "name_firm_image_3plus"),
        ("s1_flag", "name_firm_image_3plus"),
        ("s1_related", "name_firm_image_3plus"),
        ("s1_prior", "name_firm_image_3plus"),
        ("s2_good", "other_anchor_3plus"),
        ("s3_anchor", "anchor_2"),
        ("s3_no_anchor", "no_anchor_2plus"),
    ]
    connection.executemany("INSERT INTO human_review_queue VALUES(?,?)", queue_rows)
    transition_rows = [
        (candidate_id, "merge_candidate", "merge_candidate", "[]", 0)
        for candidate_id, _ in queue_rows
    ]
    transition_rows.extend(
        [
            ("s4_good", "related_but_separate", "merge_candidate", "[]", 0),
            ("s5_good", "merge_candidate", "reject", "[]", 1),
        ]
    )
    # S1/S2 ex-related is based on the v3 proposal, independent of v4.
    transition_rows[2] = (
        "s1_related",
        "related_but_separate",
        "merge_candidate",
        "[]",
        0,
    )
    connection.executemany(
        "INSERT INTO candidate_transitions VALUES(?,?,?,?,?)", transition_rows
    )
    connection.execute(
        "INSERT INTO candidate_policy_flags VALUES(?,?)",
        ("s1_flag", "fanout_review_required"),
    )
    connection.execute(
        "INSERT INTO candidate_policy_flags VALUES(?,?)",
        ("s3_no_anchor", "anchor_missing"),
    )
    connection.execute(
        "INSERT INTO hard_conflicts VALUES(?,?)",
        ("s5_good", "project_phase_incompatible"),
    )
    return connection


def test_stable_rank_is_exact_predeclared_formula() -> None:
    candidate_id = "idc_0123456789abcdef0123456789abcdef"
    expected = hashlib.sha256(
        (
            validator.SAMPLE_VERSION
            + "\0"
            + validator.SAMPLE_SEED
            + "\0S3\0"
            + candidate_id
        ).encode("utf-8")
    ).hexdigest()
    assert validator.stable_rank("S3", candidate_id) == expected


def test_frame_replay_applies_flags_related_and_prior_in_order() -> None:
    connection = _frame_fixture()
    replay = validator._frame_replay(connection, {"s1_prior"})
    assert replay["raw"]["S1"] == {
        "s1_good",
        "s1_flag",
        "s1_related",
        "s1_prior",
    }
    assert replay["frames"]["S1"] == {"s1_good"}
    assert replay["exclusions"]["S1"] == {
        "flagged": {"s1_flag"},
        "ex_related": {"s1_related"},
        "prior_reviewed": {"s1_prior"},
    }
    assert replay["frames"]["S2"] == {"s2_good"}
    assert replay["frames"]["S3"] == {"s3_anchor"}
    # The raw S4 transition universe may overlap S1's ex-related universe.
    assert replay["frames"]["S4"] == {"s1_related", "s4_good"}
    assert replay["frames"]["S5"] == {"s5_good"}


@pytest.mark.parametrize(
    ("errors", "n", "equivalent", "grade"),
    [
        (0, 60, 0.0, "bulk_candidate"),
        (1, 60, 1.0, "bulk_candidate"),
        (2, 60, 2.0, "agent_first_pass"),
        (4, 60, 4.0, "agent_first_pass"),
        (5, 60, 5.0, "direct_review"),
        (0, 40, 0.0, "bulk_candidate"),
        (1, 40, 1.5, "agent_first_pass"),
        (3, 40, 4.5, "agent_first_pass"),
        (4, 40, 6.0, "direct_review"),
        (0, 25, 0.0, "bulk_candidate"),
        (1, 25, 2.4, "agent_first_pass"),
        (2, 25, 4.8, "agent_first_pass"),
        (3, 25, 7.2, "direct_review"),
    ],
)
def test_candidate_grade_uses_exact_unrounded_ratio(
    errors: int, n: int, equivalent: float, grade: str
) -> None:
    assert validator.candidate_grade(errors, n) == (equivalent, grade)


def test_clopper_pearson_endpoints_and_reference_value() -> None:
    assert validator.clopper_pearson_95(0, 60)[0] == 0.0
    assert validator.clopper_pearson_95(60, 60)[1] == 1.0
    low, high = validator.clopper_pearson_95(60, 60)
    assert low == pytest.approx(0.9403705077, abs=1e-10)
    assert high == 1.0
    low, high = validator.clopper_pearson_95(30, 60)
    assert low == pytest.approx(0.3680620319, abs=1e-10)
    assert high == pytest.approx(0.6319379681, abs=1e-10)


def test_clopper_pearson_rejects_invalid_counts() -> None:
    with pytest.raises(ValueError):
        validator.clopper_pearson_95(0, 0)
    with pytest.raises(ValueError):
        validator.clopper_pearson_95(2, 1)


def test_blind_leak_detector_accepts_only_opaque_review_material(tmp_path: Path) -> None:
    safe = tmp_path / "safe.json"
    safe.write_text(
        json.dumps(
            {
                "lane": "r1",
                "items": [
                    {
                        "display_order": 1,
                        "opaque_review_id": "r1_deadbeef",
                        "sheet_path": "blind/r1/001_r1_deadbeef.png",
                        "label": "",
                        "rationale": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert validator._blind_json_leaks(safe, set()) == []


def test_blind_leak_detector_rejects_hidden_policy_fields_and_values(
    tmp_path: Path,
) -> None:
    candidate_id = "idc_0123456789abcdef0123456789abcdef"
    leaky = tmp_path / "leaky.json"
    leaky.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "opaque_review_id": "r1_deadbeef",
                        "candidate_id": candidate_id,
                        "proposal": "merge_candidate",
                        "stratum": "S1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    leaks = validator._blind_json_leaks(leaky, {candidate_id})
    assert "$.items[0].candidate_id" in leaks
    assert "$.items[0].proposal" in leaks
    assert "$.items[0].stratum" in leaks
    assert "candidate_id_value" in leaks
    assert "selected_candidate_value" in leaks
    assert "forbidden_value:merge_candidate" in leaks


def test_validator_fails_closed_when_artifact_is_missing(tmp_path: Path) -> None:
    result = validator.validate_identity_band_precision(
        repo_root=tmp_path,
        artifact_path=Path("missing.db"),
        report_path=None,
        verify_full_hashes=False,
        phase="sample",
    )
    assert not result.passed
    assert result.failed_check_names == ("artifact_exists",)


def test_validator_does_not_import_producer_or_n100() -> None:
    source = Path(validator.__file__).read_text(encoding="utf-8")
    assert "import vnext_identity_band_precision " not in source
    assert "import vnext_identity_n100" not in source
    assert "from canonical.vnext_identity_band_precision" not in source
    assert "from canonical.vnext_identity_n100" not in source


def _quota_row(
    number: int,
    phase: str,
    remaining: float,
    *,
    account_index: int = 2,
    signals: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    observed_at = f"2026-08-26T{number + 9:02d}:00:00+09:00"
    if signals is None:
        signals = [
            {
                "source": "runtime_status",
                "account_index": account_index,
                "observed_at": observed_at,
                "remaining_percent": remaining,
            }
        ]
    return {
        "observation_no": number,
        "phase": phase,
        "account_index": account_index,
        "observed_at": observed_at,
        "signals_json": json.dumps(signals),
        "gate_basis_remaining_percent": remaining,
        "start_allowed": int(remaining > 30),
        "stop_required": int(remaining <= 20),
        "limitation": "runtime observation; independently replayed",
    }


def test_dynamic_quota_accepts_runtime_account_timestamp_and_percentage() -> None:
    row = _quota_row(1, "pre_freeze", 77.5, account_index=4)
    assert validator._quota_ledger_failures([row], phase="sample") == []
    row["phase"] = ""
    assert "1:phase" in validator._quota_ledger_failures([row], phase="sample")


def test_dynamic_quota_uses_conservative_minimum_across_signals() -> None:
    signals = [
        {
            "source": "fresh_header",
            "account_index": 0,
            "observed_at": "2026-08-26T09:23:00+09:00",
            "remaining_percent": 94,
        },
        {
            "source": "stale_cache",
            "account_index": 0,
            "observed_at": "2026-08-24",
            "remaining_percent": 63,
        },
    ]
    row = _quota_row(1, "pre_freeze", 63, account_index=0, signals=signals)
    assert validator._quota_ledger_failures([row], phase="sample") == []
    row["gate_basis_remaining_percent"] = 94
    failures = validator._quota_ledger_failures([row], phase="sample")
    assert "1:conservative_gate" in failures


def test_final_quota_requires_all_wave_boundaries_in_order() -> None:
    phases = (
        "pre_freeze",
        "pre_prepare",
        "pre_r1_review",
        "pre_r2_review",
        "pre_adjudication_review",
        "pre_finalize",
    )
    rows = [_quota_row(index, phase, 63) for index, phase in enumerate(phases, 1)]
    assert validator._quota_ledger_failures(rows, phase="final") == []
    assert "missing_final_wave_phases:pre_finalize" in validator._quota_ledger_failures(
        rows[:-1], phase="final"
    )


def test_full_artifact_provenance_requires_all_fourteen_byte_bindings(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE artifact_paths(
          run_id TEXT, role TEXT, path TEXT, stage TEXT, record_sha256 TEXT
        );
        CREATE TABLE artifact_bindings(
          run_id TEXT, role TEXT, path TEXT, size_bytes INTEGER, sha256 TEXT,
          stage TEXT, record_sha256 TEXT
        );
        CREATE TABLE review_lock_payloads(run_id TEXT);
        CREATE TABLE unblind_payloads(run_id TEXT);
        CREATE TABLE final_payloads(run_id TEXT);
        """
    )
    run_id = "run"
    for role, stage in validator.FINAL_PATH_STAGES.items():
        relative = Path(role)
        path = tmp_path / relative
        if role == "sheets_root":
            path.mkdir()
        else:
            path.write_text(role, encoding="utf-8")
        connection.execute(
            "INSERT INTO artifact_paths VALUES(?,?,?,?,?)",
            (run_id, role, relative.as_posix(), stage, "unused"),
        )
        if role in validator.FINAL_ARTIFACT_STAGES:
            raw = path.read_bytes()
            connection.execute(
                "INSERT INTO artifact_bindings VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    role,
                    relative.as_posix(),
                    len(raw),
                    hashlib.sha256(raw).hexdigest(),
                    stage,
                    "unused",
                ),
            )
    connection.executemany(
        "INSERT INTO review_lock_payloads VALUES(?)", [(run_id,), (run_id,)]
    )
    connection.execute("INSERT INTO unblind_payloads VALUES(?)", (run_id,))
    connection.execute("INSERT INTO final_payloads VALUES(?)", (run_id,))
    checks: list[validator.Check] = []
    result = validator._validate_artifact_provenance(
        tmp_path.resolve(),
        connection,
        run_id,
        checks,
        phase="final",
        verify_full_hashes=True,
        explicit_paths={
            "sample_manifest": Path("sample_manifest"),
            "fetch_manifest": Path("fetch_manifest"),
            "fetch_report": Path("fetch_report"),
            "locked_adjudication": Path("locked_adjudication"),
            "final_report": Path("final_report"),
            "final_manifest": Path("final_manifest"),
        },
    )
    assert checks[-1].passed
    assert set(result["bindings"]) == set(validator.FINAL_ARTIFACT_STAGES)


def _fetch_fixture(
    tmp_path: Path, status: str = "exact"
) -> tuple[sqlite3.Connection, dict[str, object]]:
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
    run_id = "run"
    asset_key = "asset"
    transform = "fixture-transform-v1"
    output_root = tmp_path / "output-cache"
    read_root = tmp_path / "read-cache"
    output_root.mkdir()
    read_root.mkdir()
    connection.executemany(
        "INSERT INTO cache_pins VALUES(?,?,?,?,?)",
        [
            (run_id, "wp25_output", "output-cache", transform, "write_only"),
            (
                run_id,
                "wp2_v1_v2_review",
                "read-cache",
                transform,
                "read_only",
            ),
        ],
    )
    connection.execute(
        "INSERT INTO sample_assets VALUES(?,?,?,?)",
        (run_id, "sample", "left", asset_key),
    )

    expected_raw = "a" * 64
    expected_pixel = "b" * 64
    actual_raw: str | None = expected_raw
    actual_pixel: str | None = expected_pixel
    cache_role: str | None = "wp25_output"
    request_count = 1
    actual_final_url: str | None = "https://images.example/final.jpg"
    http_status: int | None = 200
    response_bytes: int | None = 123
    error_kind: str | None = None
    error_message: str | None = None
    if status == "delivery_changed":
        actual_raw = "c" * 64
    elif status == "source_changed":
        actual_raw = "c" * 64
        actual_pixel = "d" * 64
    elif status == "cache_hit":
        actual_raw = actual_pixel = actual_final_url = None
        http_status = response_bytes = None
        cache_role = "wp2_v1_v2_review"
        request_count = 0
    elif status in {"failed", "interrupted"}:
        actual_raw = actual_pixel = None
        cache_role = None
        actual_final_url = None
        http_status = 503
        response_bytes = 0
        error_kind = "http_503" if status == "failed" else "interrupted"
        error_message = "fixture failure"

    cache_path: str | None = None
    if cache_role is not None:
        pixel = expected_pixel if status == "cache_hit" else str(actual_pixel)
        cache_root = read_root if status == "cache_hit" else output_root
        materialized = cache_root / validator._cache_relative_path(pixel, transform)
        materialized.parent.mkdir(parents=True)
        materialized.write_bytes(b"fixture derivative")
        cache_path = materialized.relative_to(tmp_path).as_posix()

    connection.execute(
        "INSERT INTO fetch_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            asset_key,
            "https://images.example/source.jpg",
            expected_raw,
            expected_pixel,
            status,
            cache_role,
            cache_path,
            actual_final_url,
            actual_raw,
            actual_pixel,
            http_status,
            response_bytes,
            request_count,
            error_kind,
            error_message,
        ),
    )
    if status != "cache_hit":
        connection.execute(
            "INSERT INTO fetch_attempts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                asset_key,
                1,
                status,
                request_count,
                "[]",
                "2026-08-26T01:00:00Z",
                "2026-08-26T01:00:01Z",
                error_kind,
            ),
        )
    run = {
        "run_id": run_id,
        "asset_occurrence_count": 1,
        "unique_asset_count": 1,
        "logical_http_fetches": int(status != "cache_hit"),
        "wire_http_requests": int(status != "cache_hit"),
    }
    return connection, run


def _fetch_checks(
    tmp_path: Path,
    connection: sqlite3.Connection,
    run: dict[str, object],
) -> dict[str, validator.Check]:
    checks: list[validator.Check] = []
    validator._validate_fetch(tmp_path.resolve(), connection, run, checks, {})
    return {check.name: check for check in checks}


@pytest.mark.parametrize(
    "status", ["exact", "delivery_changed", "source_changed", "cache_hit", "failed"]
)
def test_fetch_validator_accepts_status_and_cache_contracts(
    tmp_path: Path, status: str
) -> None:
    connection, run = _fetch_fixture(tmp_path, status)
    checks = _fetch_checks(tmp_path, connection, run)
    assert all(check.passed for check in checks.values())


def test_fetch_validator_requires_attempt_fetch_bijection(tmp_path: Path) -> None:
    connection, run = _fetch_fixture(tmp_path)
    connection.execute("DELETE FROM fetch_attempts")
    connection.execute(
        "INSERT INTO fetch_attempts VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "run",
            "orphan",
            1,
            "exact",
            1,
            "[]",
            "2026-08-26T01:00:00Z",
            "2026-08-26T01:00:01Z",
            None,
        ),
    )
    checks = _fetch_checks(tmp_path, connection, run)
    assert not checks["fetch_attempt_bijection"].passed
    assert "asset:0!=1" in str(checks["fetch_attempt_bijection"].actual)
    assert "orphan:attempt_without_fetch" in str(
        checks["fetch_attempt_bijection"].actual
    )


@pytest.mark.parametrize(
    ("status", "column", "value", "detail"),
    [
        ("exact", "actual_raw_sha256", "c" * 64, "exact_hash_semantics"),
        ("delivery_changed", "actual_raw_sha256", "a" * 64, "delivery_changed_hash_semantics"),
        ("source_changed", "actual_pixel_sha256", "b" * 64, "source_changed_hash_semantics"),
        ("failed", "actual_pixel_sha256", "e" * 64, "failed_pixel_without_raw"),
    ],
)
def test_fetch_validator_rejects_false_status_hash_claims(
    tmp_path: Path, status: str, column: str, value: str, detail: str
) -> None:
    connection, run = _fetch_fixture(tmp_path, status)
    connection.execute(f"UPDATE fetch_assets SET {column}=?", (value,))
    checks = _fetch_checks(tmp_path, connection, run)
    assert not checks["fetch_status_hash_semantics"].passed
    assert detail in str(checks["fetch_status_hash_semantics"].actual)


def test_fetch_validator_rejects_interrupted_terminal_row(tmp_path: Path) -> None:
    connection, run = _fetch_fixture(tmp_path, "interrupted")
    checks = _fetch_checks(tmp_path, connection, run)
    assert not checks["fetch_status_hash_semantics"].passed
    assert "non_terminal_or_unknown:interrupted" in str(
        checks["fetch_status_hash_semantics"].actual
    )


def test_fetch_validator_binds_network_derivative_to_output_tree(tmp_path: Path) -> None:
    connection, run = _fetch_fixture(tmp_path)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not the pinned derivative")
    connection.execute(
        "UPDATE fetch_assets SET cache_path=?", (outside.relative_to(tmp_path).as_posix(),)
    )
    checks = _fetch_checks(tmp_path, connection, run)
    assert not checks["fetch_cache_role_path_tree_binding"].passed
    assert "cache_tree_path" in str(
        checks["fetch_cache_role_path_tree_binding"].actual
    )


def test_fetch_validator_rejects_network_derivative_in_read_only_role(
    tmp_path: Path,
) -> None:
    connection, run = _fetch_fixture(tmp_path)
    connection.execute(
        "UPDATE fetch_assets SET cache_role='wp2_v1_v2_review'"
    )
    checks = _fetch_checks(tmp_path, connection, run)
    assert not checks["fetch_cache_role_path_tree_binding"].passed
    assert "cache_role" in str(checks["fetch_cache_role_path_tree_binding"].actual)


def test_fetch_validator_recomputes_no_redirect_retry_and_logical_wire(
    tmp_path: Path,
) -> None:
    connection, run = _fetch_fixture(tmp_path)
    connection.execute(
        "UPDATE fetch_attempts SET attempt_no=2,request_count=0,redirect_chain_json=?",
        ('[{"http_status":302}]',),
    )
    connection.execute("UPDATE fetch_assets SET request_count=0")
    run["wire_http_requests"] = 0
    checks = _fetch_checks(tmp_path, connection, run)
    assert not checks["fetch_redirects_retries_disabled"].passed
    assert "retry" in str(checks["fetch_redirects_retries_disabled"].actual)
    assert "redirect" in str(checks["fetch_redirects_retries_disabled"].actual)
    assert "logical_wire_mismatch" in str(
        checks["fetch_redirects_retries_disabled"].actual
    )
