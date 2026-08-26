from __future__ import annotations

import hashlib
import inspect
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from canonical import vnext_identity_band_precision as subject
from tools import run_vnext_identity_band_precision as cli


def test_protocol_constants_and_hard_caps() -> None:
    assert subject.STRATUM_QUOTAS == {"S1": 60, "S2": 60, "S3": 40, "S4": 25, "S5": 36}
    assert subject.TOTAL_COUNT == 221
    assert subject.MAX_ASSET_OCCURRENCES == 884
    assert subject.MAX_LOGICAL_HTTP_FETCHES == 884
    assert subject.MAX_WIRE_HTTP_REQUESTS == 884
    assert subject.MAX_WORKERS == 4
    assert subject.EXPECTED_FRAMES["S3"]["eligible"] == 464
    assert subject.EXPECTED_FRAMES["S4"] == {
        "raw": 119, "flagged": 21, "ex_related": 0, "prior": 1, "eligible": 97
    }


def test_seed_rank_is_the_literal_predeclared_formula() -> None:
    candidate_id = "idc_example"
    expected = hashlib.sha256(
        f"{subject.SAMPLE_VERSION}\0{subject.SEED}\0S3\0{candidate_id}".encode("utf-8")
    ).hexdigest()
    assert subject._seed_rank("S3", candidate_id) == expected


@pytest.mark.parametrize(
    ("errors", "expected"),
    [(0, "bulk_candidate"), (1, "bulk_candidate"), (2, "agent_first_pass"), (4, "agent_first_pass"), (5, "direct_review")],
)
def test_s1_grade_boundaries_are_not_rounded(errors: int, expected: str) -> None:
    labels = Counter({"same": 60 - errors, "related": errors})
    assert subject._grade("S1", labels)["candidate_grade"] == expected


def test_proportional_grade_and_different_veto() -> None:
    # S3: 1 error is 1.5 equivalent errors / 60, therefore agent-first-pass.
    metric = subject._grade("S3", Counter({"same": 39, "different": 1}))
    assert metric["equivalent_errors_60"] == 1.5
    assert metric["candidate_grade"] == "agent_first_pass"
    assert metric["bulk_excluded"] is True


def test_s5_same_is_rule_failure() -> None:
    metric = subject._grade("S5", Counter({"same": 1, "different": 35}))
    assert metric["status"] == "FAIL"
    assert metric["candidate_grade"] == "r1_rule_guard"


def test_clopper_pearson_edge_values() -> None:
    low, high = subject.clopper_pearson(60, 60)
    assert low == pytest.approx(0.94037, abs=1e-5)
    assert high == 1.0
    low, high = subject.clopper_pearson(0, 36)
    assert low == 0.0
    assert high == pytest.approx(0.0973938, abs=1e-6)


def _minimal_sidecar(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(subject.SCHEMA_SQL)
    connection.execute(
        "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
        "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
        "VALUES('run',1,'r','s','v','a','g','seed','sample_frozen','now','hash')"
    )
    observed_at = subject.utc_now()
    signals = [{"source": "test", "account_index": 0, "observed_at": observed_at,
                "remaining_percent": 90.0}]
    payload = {"observation_no": 1, "phase": "pre_freeze", "account_index": 0,
               "observed_at": observed_at, "signals": signals,
               "gate_basis_remaining_percent": 90.0, "start_allowed": 1,
               "stop_required": 0, "limitation": "test fixture"}
    connection.execute(
        "INSERT INTO quota_observations VALUES('run',1,'pre_freeze',0,?,?,?,?,?,?,?)",
        (observed_at, subject.canonical_json(signals), 90.0, 1, 0, "test fixture",
         subject._record(payload)),
    )
    connection.commit()
    connection.close()


def test_quota_gate_appends_denial_before_failing(tmp_path: Path) -> None:
    artifact = tmp_path / "quota.db"
    _minimal_sidecar(artifact)
    with pytest.raises(subject.IdentityBandPrecisionError, match="30%"):
        subject.record_quota_observation(
            repo_root=tmp_path,
            artifact_path=artifact,
            phase="pre_prepare",
            remaining_percent=30,
            source="test",
            observed_at=subject.utc_now(),
        )
    connection = sqlite3.connect(artifact)
    assert connection.execute(
        "SELECT start_allowed,stop_required FROM quota_observations WHERE phase='pre_prepare'"
        ).fetchone() == (0, 0)
    connection.close()


def test_quota_twenty_percent_requires_full_stop(tmp_path: Path) -> None:
    artifact = tmp_path / "quota.db"
    _minimal_sidecar(artifact)
    with pytest.raises(subject.IdentityBandPrecisionError, match="20%"):
        subject.record_quota_observation(
            repo_root=tmp_path,
            artifact_path=artifact,
            phase="pre_prepare",
            remaining_percent=20,
            source="test",
            observed_at=subject.utc_now(),
        )
    connection = sqlite3.connect(artifact)
    assert connection.execute(
        "SELECT start_allowed,stop_required FROM quota_observations WHERE phase='pre_prepare'"
        ).fetchone() == (0, 1)
    connection.close()


def test_blind_form_contains_no_hidden_sample_fields() -> None:
    items = [{
        "display_order": 1,
        "opaque_review_id": "r1_opaque",
        "sheet_path": "sheet.png",
        "sheet_sha256": "a" * 64,
        "label": "",
        "rationale": "",
        "hard_conflict_observed": "",
    }]
    form = subject._form_payload("run", "r1", items)
    serialized = subject.canonical_json(form)
    for forbidden in ("candidate_id", "proposal", "score", "policy_flags", "stratum", "seed_rank"):
        assert forbidden not in serialized


def test_frozen_sample_unique_and_no_update_guards() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(subject.SCHEMA_SQL)
    connection.execute(
        "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
        "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
        "VALUES('run',1,'r','s','v','a','g','seed','building','now','hash')"
    )
    connection.execute(
        "INSERT INTO sample_pairs VALUES('run',1,'sample','S1','candidate','rank','hash')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO sample_pairs VALUES('run',2,'sample2','S2','candidate','rank2','hash2')"
        )
    connection.execute("UPDATE runs SET status='sample_frozen' WHERE run_id='run'")
    with pytest.raises(sqlite3.IntegrityError, match="sample is frozen"):
        connection.execute("UPDATE sample_pairs SET stratum='S2'")
    with pytest.raises(sqlite3.IntegrityError, match="sample is frozen"):
        connection.execute(
            "INSERT INTO sample_pairs VALUES('run',2,'sample2','S2','candidate2','rank2','hash2')"
        )
    connection.close()


def test_pristine_fixture_rule_is_explicit() -> None:
    # A frozen artifact passed to a validator must never be used for prepare.
    # Phase tests create/copy a separate fixture because prepare is intentionally mutating.
    assert "sample_frozen" != "fetching"


def test_build_quota_evidence_has_no_default() -> None:
    signature = inspect.signature(subject.build_sample)
    for name in ("quota_remaining_percent", "quota_observed_at", "quota_source",
                 "quota_account_index", "quota_limitation"):
        assert signature.parameters[name].default is inspect.Parameter.empty


def test_quota_phase_is_ordered_and_caller_cannot_override_wave_gate(tmp_path: Path) -> None:
    artifact = tmp_path / "quota-order.db"
    _minimal_sidecar(artifact)
    signature = inspect.signature(subject.record_quota_observation)
    assert "starting_wave" not in signature.parameters
    with pytest.raises(subject.IdentityBandPrecisionError, match="runtime phase"):
        subject.record_quota_observation(
            repo_root=tmp_path, artifact_path=artifact, phase="anything",
            remaining_percent=90, source="test", observed_at=subject.utc_now(),
        )
    with pytest.raises(subject.IdentityBandPrecisionError, match="status|order"):
        subject.record_quota_observation(
            repo_root=tmp_path, artifact_path=artifact, phase="pre_r1_review",
            remaining_percent=90, source="test", observed_at=subject.utc_now(),
        )
    with pytest.raises(TypeError, match="starting_wave"):
        subject.record_quota_observation(
            repo_root=tmp_path, artifact_path=artifact, phase="pre_prepare",
            remaining_percent=25, source="test", observed_at=subject.utc_now(),
            starting_wave=False,  # type: ignore[call-arg]
        )
    quota_parser = cli.parser()._subparsers._group_actions[0].choices["quota-check"]
    assert "not_starting_wave" not in {action.dest for action in quota_parser._actions}


def test_quota_timestamp_requires_timezone_freshness_and_monotonicity(tmp_path: Path) -> None:
    artifact = tmp_path / "quota-time.db"
    _minimal_sidecar(artifact)
    now = datetime.now(timezone.utc)
    for observed_at, message in (
        (now.replace(tzinfo=None).isoformat(), "timezone"),
        ((now - subject.MAX_QUOTA_OBSERVATION_AGE - timedelta(seconds=1)).isoformat(), "stale"),
        ((now + subject.MAX_QUOTA_FUTURE_SKEW + timedelta(seconds=1)).isoformat(), "future"),
    ):
        with pytest.raises(subject.IdentityBandPrecisionError, match=message):
            subject.record_quota_observation(
                repo_root=tmp_path, artifact_path=artifact, phase="pre_prepare",
                remaining_percent=90, source="test", observed_at=observed_at,
            )
    accepted = subject.utc_now()
    subject.record_quota_observation(
        repo_root=tmp_path, artifact_path=artifact, phase="pre_prepare",
        remaining_percent=90, source="test", observed_at=accepted,
    )
    with pytest.raises(subject.IdentityBandPrecisionError, match="monotonic"):
        subject.record_quota_observation(
            repo_root=tmp_path, artifact_path=artifact, phase="pre_prepare",
            remaining_percent=90, source="test",
            observed_at=(datetime.fromisoformat(accepted.replace("Z", "+00:00"))
                         - timedelta(milliseconds=1)).isoformat(),
        )


def test_db_first_recovery_payload_ledgers_are_immutable() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(subject.SCHEMA_SQL)
    connection.execute(
        "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
        "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
        "VALUES('run',1,'r','s','v','a','g','seed','review_ready','now','hash')"
    )
    connection.execute(
        "INSERT INTO review_lock_payloads VALUES('run','r1','{}','hash')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="review lock payload is frozen"):
        connection.execute("UPDATE review_lock_payloads SET payload_json='changed'")
    connection.execute("UPDATE runs SET status='reviews_locked' WHERE run_id='run'")
    connection.execute(
        "INSERT INTO unblind_payloads VALUES('run','{}','{}','hash')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="unblind payload is frozen"):
        connection.execute("DELETE FROM unblind_payloads")
    connection.execute("UPDATE runs SET status='unblinded' WHERE run_id='run'")
    connection.execute(
        "INSERT INTO final_payloads VALUES('run','PASS','{}','{}','hash')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="final payload is frozen"):
        connection.execute("UPDATE final_payloads SET report_json='changed'")
    connection.close()


def test_input_and_cache_pins_freeze_after_their_write_phase() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(subject.SCHEMA_SQL)
    connection.execute(
        "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
        "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
        "VALUES('run',1,'r','s','v','a','g','seed','building','now','hash')"
    )
    connection.execute(
        "INSERT INTO input_pins VALUES('run','input','input.db',1,'before',NULL,'test','hash')"
    )
    connection.execute("UPDATE input_pins SET sha256_after='before'")
    connection.execute("UPDATE runs SET status='fetching' WHERE run_id='run'")
    with pytest.raises(sqlite3.IntegrityError, match="input pins are frozen"):
        connection.execute("UPDATE input_pins SET usage='changed'")
    with pytest.raises(sqlite3.IntegrityError, match="input pins are frozen"):
        connection.execute("DELETE FROM input_pins")
    connection.execute(
        "INSERT INTO cache_pins VALUES('run','cache','cache','v','write_only',0,0,'empty',NULL,NULL,NULL,'hash')"
    )
    connection.execute(
        "UPDATE cache_pins SET file_count_after=0,total_bytes_after=0,tree_sha256_after='empty'"
    )
    connection.execute("UPDATE runs SET status='review_ready' WHERE run_id='run'")
    with pytest.raises(sqlite3.IntegrityError, match="cache pins are frozen"):
        connection.execute("UPDATE cache_pins SET tree_sha256_after='changed'")
    with pytest.raises(sqlite3.IntegrityError, match="cache pins are frozen"):
        connection.execute("DELETE FROM cache_pins")
    connection.close()


def test_durable_http_reservation_is_closed_without_retry_after_restart(tmp_path: Path) -> None:
    artifact = tmp_path / "reserved.db"
    connection = sqlite3.connect(artifact)
    connection.row_factory = sqlite3.Row
    connection.executescript(subject.SCHEMA_SQL)
    connection.execute(
        "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
        "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
        "VALUES('run',1,'r','s','v','a','g','seed','fetching','now','hash')"
    )
    pending_payload = {
        "asset_key": "asset", "source": "architizer", "source_asset_id": "source-asset",
        "expected_url": "https://architizer-prod.imgix.net/example.jpg",
        "expected_raw_sha256": "a" * 64, "expected_pixel_sha256": "b" * 64,
        "status": "pending", "cache_role": None, "cache_path": None,
        "error_kind": None, "error_message": None,
    }
    connection.execute(
        "INSERT INTO fetch_assets VALUES('run','asset','architizer','source-asset',?,?,?,'pending',"
        "NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL,NULL,?)",
        (pending_payload["expected_url"], pending_payload["expected_raw_sha256"],
         pending_payload["expected_pixel_sha256"], subject._record(pending_payload)),
    )
    connection.commit()
    pending = [dict(connection.execute("SELECT * FROM fetch_assets").fetchone())]
    subject._reserve_fetch_attempts(connection, "run", pending)
    connection.close()

    resumed = sqlite3.connect(artifact)
    resumed.row_factory = sqlite3.Row
    assert resumed.execute("SELECT status FROM fetch_assets").fetchone()[0] == "reserved"
    assert tuple(resumed.execute(
        "SELECT status,request_count FROM fetch_attempts"
    ).fetchone()) == ("reserved", 1)
    assert subject._terminalize_reserved_fetches(
        resumed, "run", reason="simulated process interruption"
    ) == 1
    fetch = resumed.execute(
        "SELECT status,request_count,error_kind FROM fetch_assets"
    ).fetchone()
    attempt = resumed.execute(
        "SELECT status,request_count,error_kind FROM fetch_attempts"
    ).fetchone()
    assert tuple(fetch) == ("failed", 1, "interrupted_unknown")
    assert tuple(attempt) == ("failed", 1, "interrupted_unknown")
    assert resumed.execute("SELECT count(*) FROM fetch_assets WHERE status='pending'").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="transition is invalid"):
        resumed.execute("UPDATE fetch_attempts SET status='exact'")

    second_payload = {**pending_payload, "asset_key": "asset-2",
                      "source_asset_id": "source-asset-2"}
    resumed.execute(
        "INSERT INTO fetch_assets VALUES('run','asset-2','architizer','source-asset-2',?,?,?,'pending',"
        "NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL,NULL,?)",
        (second_payload["expected_url"], second_payload["expected_raw_sha256"],
         second_payload["expected_pixel_sha256"], subject._record(second_payload)),
    )
    resumed.commit()
    second = [dict(resumed.execute(
        "SELECT * FROM fetch_assets WHERE asset_key='asset-2'"
    ).fetchone())]
    subject._reserve_fetch_attempts(resumed, "run", second)
    now = subject.utc_now()
    outcome = subject._FetchOutcome(
        "asset-2", "failed", None, None, None, None, None, 1, (),
        "connection", "simulated failure", None, now, now,
    )
    subject._persist_fetch_outcome(resumed, tmp_path, "run", tmp_path / "cache", outcome)
    assert tuple(resumed.execute(
        "SELECT status,request_count,error_kind FROM fetch_attempts WHERE asset_key='asset-2'"
    ).fetchone()) == ("failed", 1, "connection")
    resumed.close()


def test_terminal_artifact_provenance_rejects_late_inserts() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(subject.SCHEMA_SQL)
    connection.execute(
        "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
        "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
        "VALUES('run',1,'r','s','v','a','g','seed','pass','now','hash')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="artifact paths are terminal"):
        connection.execute(
            "INSERT INTO artifact_paths VALUES('run','late','late.json','finalize','hash')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="artifact bindings are terminal"):
        connection.execute(
            "INSERT INTO artifact_bindings VALUES('run','late','late.json',1,'sha','finalize','hash')"
        )
    connection.close()


def test_atomic_json_allows_only_byte_identical_recovery(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    subject._atomic_json(path, {"value": 1})
    subject._atomic_json(path, {"value": 1}, allow_identical=True)
    with pytest.raises(subject.IdentityBandPrecisionError, match="clobber"):
        subject._atomic_json(path, {"value": 2}, allow_identical=True)


def test_sample_logical_sha_ignores_output_artifact_paths() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(subject.SCHEMA_SQL)
    for run_id, path in (("run-a", "one/sample.json"), ("run-b", "two/sample.json")):
        connection.execute(
            "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
            "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
            "VALUES(?,1,'r','s','v','a','g','seed','building','now','hash')",
            (run_id,),
        )
        connection.execute(
            "INSERT INTO artifact_paths VALUES(?, 'sample_manifest', ?, 'freeze', 'path-hash')",
            (run_id, path),
        )
    assert subject._logical_sha(connection, "run-a") == subject._logical_sha(connection, "run-b")
    connection.close()
