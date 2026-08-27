from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from canonical import vnext_identity_instrument_v21_probe as probe


REPO_ROOT = Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _stage_ab(tmp_path: Path) -> Path:
    artifact = tmp_path / "probe.db"
    result = probe.extract_vocabulary(
        repo_root=REPO_ROOT, quota_remaining_percent=80.0,
        quota_observed_at=_now(), quota_source="unit-test",
        output_path=artifact,
    )
    assert result.status == "vocabulary_frozen"
    verified = probe.verify_derivation(repo_root=REPO_ROOT, artifact_path=artifact)
    assert verified.status == "derivation_verified"
    return artifact


def test_exact_n100_parser_and_controlled_vocabulary() -> None:
    value = (
        "image=same_object; name=alias; firm=overlap; place=unclear; "
        "year=compatible; scope=related_separate; basis=clear visible evidence"
    )
    parsed = probe.parse_n100_rationale(value)
    assert parsed == {
        "image": "same_object", "name": "alias", "firm": "overlap",
        "place": "unclear", "year": "compatible", "scope": "related_separate",
        "basis": "clear visible evidence",
    }
    with pytest.raises(probe.IdentityInstrumentV21Error):
        probe.parse_n100_rationale(value.replace("image=", "image ="))
    assert set(probe.CONTROLLED_VALUES["image"]) == {
        "same_object", "different_objects", "inconclusive", "unavailable"
    }
    assert set(probe.CONTROLLED_VALUES["scope"]) == {
        "same", "related_separate", "unclear", "unrelated"
    }
    assert "component_vs_umbrella" not in str(probe.CONTROLLED_VALUES)
    assert "phase_or_extension" not in str(probe.CONTROLLED_VALUES)
    assert "unknown" not in probe.CONTROLLED_VALUES["firm"]


def test_consensus_priority_and_derivation_first_match() -> None:
    r1 = {"image": "different_objects", "name": "agree", "firm": "agree",
          "place": "unclear", "year": "compatible", "scope": "unrelated"}
    r2 = {"image": "inconclusive", "name": "alias", "firm": "overlap",
          "place": "conflict", "year": "conflict", "scope": "unclear"}
    assert probe.consensus_axes(r1, r2) == {
        "image": "inconclusive", "name": "alias", "firm": "overlap",
        "place": "conflict", "year": "conflict", "scope": "unrelated",
    }
    assert probe.derive_label({**r1, "image": "unavailable"}) == ("insufficient", 1)
    assert probe.derive_label(r1) == ("different", 2)
    assert probe.derive_label({**r1, "image": "same_object",
                               "scope": "related_separate"}) == ("related", 3)
    assert probe.derive_label({**r1, "image": "same_object", "scope": "same"}) == (
        "same", 4
    )
    assert probe.derive_label({**r1, "image": "same_object", "scope": "unclear"}) == (
        "insufficient", 5
    )


def test_stage_a_b_exact_210_105_102_replay_and_immutable_rows(tmp_path: Path) -> None:
    artifact = _stage_ab(tmp_path)
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    try:
        run = connection.execute("SELECT * FROM runs").fetchone()
        assert run["status"] == "derivation_verified"
        assert connection.execute("SELECT count(*) FROM n100_review_axes").fetchone()[0] == 210
        assert connection.execute("SELECT count(*) FROM vocabulary_counts").fetchone()[0] == 22
        assert connection.execute("SELECT count(*) FROM n100_consensus_axes").fetchone()[0] == 105
        derivation = connection.execute("SELECT * FROM derivation_contract").fetchone()
        assert (derivation["numerator"], derivation["denominator"],
                derivation["minimum_matches"], derivation["passed"]) == (102, 105, 100, 1)
        assert json.loads(derivation["rule_counts_json"]) == {
            "1": 11, "2": 10, "3": 1, "4": 83, "5": 0
        }
        mismatches = json.loads(derivation["mismatches_json"])
        assert [(row["n100_sample_id"], row["predicted_label"], row["gold_label"])
                for row in mismatches] == list(probe.EXPECTED_MISMATCHES)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE vocabulary_counts SET frequency=0")
    finally:
        connection.close()


def test_full_temp_workflow_blindness_state_gate_and_recovery(tmp_path: Path) -> None:
    artifact = _stage_ab(tmp_path)
    protocol = tmp_path / "protocol.md"
    protocol.write_text("# test-only v2.1 protocol\n", encoding="utf-8")
    frozen = probe.freeze_instrument(
        repo_root=REPO_ROOT, artifact_path=artifact, protocol_path=protocol,
    )
    assert frozen.status == "instrument_frozen"
    sample_manifest = tmp_path / "sample.json"
    sample = probe.freeze_sample(
        repo_root=REPO_ROOT, artifact_path=artifact,
        sample_manifest_path=sample_manifest,
    )
    assert (sample.sample_count, sample.sheet_count, sample.asset_occurrence_count) == (24, 48, 93)
    gate = probe.declare_gate(repo_root=REPO_ROOT, artifact_path=artifact)
    assert [(row["code"], row["denominator"], row["threshold"])
            for row in gate["predicates"]] == [
                ("d_image_same_object", 15, 14),
                ("d_scope_same", 15, 14),
                ("n100_image_non_same_object", 5, 3),
                ("p1_non_same_label", 3, 2),
            ]
    assert len(gate["eligible_sample_ids"]) == 5

    probe.record_quota_observation(
        repo_root=REPO_ROOT, artifact_path=artifact, phase="pre_r1_review",
        remaining_percent=79.0, source="unit-test", observed_at=_now(),
    )
    r1_form, r2_form = tmp_path / "r1.json", tmp_path / "r2.json"
    fetch_manifest, fetch_report = tmp_path / "fetch_manifest.json", tmp_path / "fetch_report.json"
    probe.prepare_review(
        repo_root=REPO_ROOT, artifact_path=artifact, r1_form_path=r1_form,
        r2_form_path=r2_form, fetch_manifest_path=fetch_manifest,
        fetch_report_path=fetch_report,
    )
    for path in (r1_form, r2_form):
        form = json.loads(path.read_text(encoding="utf-8"))
        assert form["instructions"]["controlled_values"] == {
            key: list(value) for key, value in probe.CONTROLLED_VALUES.items()
        }
        assert not any(key in form for key in probe.FORBIDDEN_INPUT_FIELDS)
        form["reviewer"] = "Reviewer One" if form["lane"] == "r1" else "Reviewer Two"
        form["reviewed_at"] = _now()
        for item in form["items"]:
            item.update({"image": "same_object", "name": "agree", "firm": "agree",
                         "place": "unclear", "year": "compatible", "scope": "same",
                         "basis": "same physical object is identifiable"})
        completed = tmp_path / f"{form['lane']}_completed.json"
        _write_json(completed, form)
        if form["lane"] == "r1":
            probe.lock_review(
                repo_root=REPO_ROOT, artifact_path=artifact, lane="r1",
                completed_form_path=completed, locked_output_path=tmp_path / "r1_locked.json",
            )
            probe.record_quota_observation(
                repo_root=REPO_ROOT, artifact_path=artifact, phase="pre_r2_review",
                remaining_percent=78.0, source="unit-test", observed_at=_now(),
            )
        else:
            probe.lock_review(
                repo_root=REPO_ROOT, artifact_path=artifact, lane="r2",
                completed_form_path=completed, locked_output_path=tmp_path / "r2_locked.json",
            )

    probe.record_quota_observation(
        repo_root=REPO_ROOT, artifact_path=artifact, phase="pre_adjudication_review",
        remaining_percent=77.0, source="unit-test", observed_at=_now(),
    )
    packet, adjudication_form = tmp_path / "packet.json", tmp_path / "adjudication.json"
    probe.prepare_adjudication(
        repo_root=REPO_ROOT, artifact_path=artifact, packet_path=packet,
        adjudication_form_path=adjudication_form,
    )
    packet_payload = json.loads(packet.read_text(encoding="utf-8"))
    assert not any(key in packet_payload for key in probe.FORBIDDEN_INPUT_FIELDS)
    form = json.loads(adjudication_form.read_text(encoding="utf-8"))
    form["adjudicator"] = "Adjudicator Three"; form["adjudicated_at"] = _now()
    for item in form["items"]:
        item.update({"image": "same_object", "name": "agree", "firm": "agree",
                     "place": "unclear", "year": "compatible", "scope": "same",
                     "basis": "same physical object is identifiable"})
    adjudication_completed = tmp_path / "adjudication_completed.json"
    _write_json(adjudication_completed, form)
    probe.lock_adjudication(
        repo_root=REPO_ROOT, artifact_path=artifact,
        completed_form_path=adjudication_completed,
        locked_output_path=tmp_path / "adjudication_locked.json",
    )
    probe.record_quota_observation(
        repo_root=REPO_ROOT, artifact_path=artifact, phase="pre_reveal_score",
        remaining_percent=76.0, source="unit-test", observed_at=_now(),
    )
    report, manifest = tmp_path / "report.json", tmp_path / "manifest.json"
    terminal = probe.reveal_score(
        repo_root=REPO_ROOT, artifact_path=artifact,
        report_path=report, manifest_path=manifest,
    )
    assert terminal.status == "FAIL"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["stage_b_derivation"]["numerator"] == 102
    assert len(payload["stage_b_derivation"]["mismatches"]) == 3
    assert len(payload["four_way_comparison"]) == 24
    assert payload["gate"]["predicates"]["d_image_same_object"]["actual"] == 15
    assert payload["gate"]["predicates"]["d_scope_same"]["actual"] == 15
    assert payload["gate"]["predicates"]["n100_image_non_same_object"]["actual"] == 0
    assert payload["gate"]["predicates"]["p1_non_same_label"]["actual"] == 0
    report.unlink(); manifest.unlink()
    recovered = probe.reveal_score(
        repo_root=REPO_ROOT, artifact_path=artifact,
        report_path=report, manifest_path=manifest,
    )
    assert recovered.status == "FAIL" and report.is_file() and manifest.is_file()


def test_quota_at_or_below_twenty_is_sticky(tmp_path: Path) -> None:
    artifact = _stage_ab(tmp_path)
    protocol = tmp_path / "protocol.md"; protocol.write_text("v2.1\n", encoding="utf-8")
    probe.freeze_instrument(repo_root=REPO_ROOT, artifact_path=artifact, protocol_path=protocol)
    probe.freeze_sample(repo_root=REPO_ROOT, artifact_path=artifact,
                        sample_manifest_path=tmp_path / "sample.json")
    probe.declare_gate(repo_root=REPO_ROOT, artifact_path=artifact)
    with pytest.raises(probe.IdentityInstrumentV21Error, match="sticky stop set"):
        probe.record_quota_observation(
            repo_root=REPO_ROOT, artifact_path=artifact, phase="pre_r1_review",
            remaining_percent=20.0, source="unit-test", observed_at=_now(),
        )
    connection = sqlite3.connect(artifact)
    try:
        assert connection.execute("SELECT sticky_stop FROM runs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT stop_required FROM quota_observations ORDER BY observation_no DESC LIMIT 1"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    with pytest.raises(probe.IdentityInstrumentV21Error):
        probe.record_quota_observation(
            repo_root=REPO_ROOT, artifact_path=artifact, phase="pre_r1_review",
            remaining_percent=90.0, source="unit-test", observed_at=_now(),
        )
