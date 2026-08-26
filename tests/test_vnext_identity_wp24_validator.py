from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from canonical import vnext_identity_candidates_v4_validator as validator


FAMILIES = (
    "name_alias_identity",
    "architect_firm_identity",
    "location_site_identity",
    "year_phase_compatibility",
    "source_url_external_stable_id",
    "verified_pixel_image_overlap",
)


def _state(
    candidate_id: str,
    *,
    left_id: str = "d",
    right_id: str = "a",
    gap: int | None = 0,
    name_positive: bool = True,
    firm_positive: bool = False,
    image_status: str = "unknown",
    phash_veto: bool = False,
) -> validator.CandidateState:
    statuses = {
        "name_alias_identity": "positive" if name_positive else "unknown",
        "architect_firm_identity": "positive" if firm_positive else "unknown",
        "location_site_identity": "unknown",
        "year_phase_compatibility": "unknown",
        "source_url_external_stable_id": "unknown",
        "verified_pixel_image_overlap": image_status,
    }
    families = {
        family: {"status": statuses[family], "score": None, "detail": {}}
        for family in FAMILIES
    }
    conflicts: dict[str, dict[str, object]] = {}
    if gap is not None and gap >= 6:
        # This legacy matcher conflict must be discarded and independently
        # reconstructed under R1's conditional phase rule.
        conflicts["matcher_architect_year_gate_block"] = {"legacy": True}
    if phash_veto:
        conflicts["verified_phash_veto"] = {"reason": "synthetic_phash_veto"}
    row: dict[str, object] = {
        "candidate_id": candidate_id,
        "left_source": "divisare",
        "left_source_building_id": left_id,
        "right_source": "architizer",
        "right_source_building_id": right_id,
        "proposal": "defer",
        "auto_merge_eligible": 0,
        "positive_family_count": 0,
        "negative_family_count": 0,
        "hard_conflict_count": len(conflicts),
        "name_strict_score": 100.0 if name_positive else 0.0,
        "exact_asset_pair_count": 0,
        "identical_phash_pair_count": 0,
        "phash_le8_pair_count": 1 if image_status == "positive" else 0,
        "year_gap": gap,
    }
    return validator.CandidateState(
        candidate_id=candidate_id,
        left_id=left_id,
        right_id=right_id,
        row=row,
        v3_row=dict(row),
        families=families,
        conflicts=conflicts,
        flags={},
        v3_conflicts=tuple(sorted(conflicts)),
        v3_flags=(),
        anchor_families=(),
        r1_before={},
        r1_after={},
        r2_before={},
        r2_after={},
        r3_before={},
        r3_after={},
        fanout_triggers=[],
    )


def _building(building_id: str, name: str) -> dict[str, object]:
    return {
        "source": "architizer",
        "id": building_id,
        "name": name,
        "aliases": (name,),
        "firm_keys": ("shared firm",),
        "country_status": "known",
        "country_norm": "KR",
        "locality_norm": "seoul",
        "year_min": 2020,
        "year_max": 2020,
    }


def test_validator_source_never_imports_v4_producer() -> None:
    source_path = Path(validator.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    assert "canonical.vnext_identity_candidates_v4" not in imported


@pytest.mark.parametrize(
    ("gap", "expected_status", "phase_conflict"),
    [
        (1, "positive", False),
        (2, "unknown", False),
        (5, "unknown", False),
        (6, "negative", True),
    ],
)
def test_independent_r1_boundaries(
    gap: int, expected_status: str, phase_conflict: bool
) -> None:
    state = _state(f"gap-{gap}", gap=gap)
    validator._apply_r1_r2({state.candidate_id: state})
    assert state.families["year_phase_compatibility"]["status"] == expected_status
    assert ("project_phase_incompatible" in state.conflicts) is phase_conflict
    assert "matcher_architect_year_gate_block" not in state.conflicts


@pytest.mark.parametrize(
    ("firm_positive", "image_status"),
    [(True, "unknown"), (False, "positive")],
)
def test_independent_r1_phase_conflict_is_conditional(
    firm_positive: bool, image_status: str
) -> None:
    state = _state(
        "phase-override",
        gap=6,
        firm_positive=firm_positive,
        image_status=image_status,
    )
    validator._apply_r1_r2({state.candidate_id: state})
    assert state.families["year_phase_compatibility"]["status"] == "negative"
    assert "project_phase_incompatible" not in state.conflicts


def test_r1_before_replays_legacy_matcher_related_baseline() -> None:
    state = _state("legacy-related", gap=6, firm_positive=True)
    validator._apply_r1_r2({state.candidate_id: state})

    assert state.r1_before["hard_conflicts"] == ["matcher_architect_year_gate_block"]
    assert state.r1_before["proposal"] == "related_but_separate"
    assert state.r1_after["hard_conflicts"] == []
    assert state.r1_after["proposal"] == "merge_candidate"


def test_independent_r2_downgrades_only_name_plus_firm_veto() -> None:
    downgraded = _state(
        "downgraded",
        name_positive=True,
        firm_positive=True,
        image_status="negative",
        phash_veto=True,
    )
    retained = _state(
        "retained",
        name_positive=True,
        firm_positive=False,
        image_status="negative",
        phash_veto=True,
    )
    validator._apply_r1_r2(
        {downgraded.candidate_id: downgraded, retained.candidate_id: retained}
    )

    assert "verified_phash_veto" not in downgraded.conflicts
    assert set(downgraded.flags) == {"phash_veto_review_required"}
    assert (downgraded.row["proposal"], downgraded.row["auto_merge_eligible"]) == (
        "merge_candidate",
        0,
    )
    assert downgraded.families["verified_pixel_image_overlap"]["status"] == "negative"

    assert set(retained.conflicts) == {"verified_phash_veto"}
    assert "phash_veto_review_required" not in retained.flags
    assert (retained.row["proposal"], retained.row["auto_merge_eligible"]) == (
        "reject",
        0,
    )


def test_independent_r3_graph_and_queue_parity() -> None:
    first = _state("c1", left_id="hub", right_id="a1", firm_positive=True)
    second = _state("c2", left_id="hub", right_id="a2", firm_positive=True)
    states = {first.candidate_id: first, second.candidate_id: second}
    validator._apply_r1_r2(states)
    assert all(state.row["auto_merge_eligible"] == 1 for state in states.values())

    buildings = {
        ("divisare", "hub"): {
            **_building("hub", "Hub Project"),
            "source": "divisare",
        },
        ("architizer", "a1"): _building("a1", "Alpha Museum"),
        ("architizer", "a2"): _building("a2", "Zeta Hospital"),
    }
    groups = validator._apply_fanout(states, buildings)

    assert len(groups) == 1
    assert groups[0]["status"] == "conflict"
    assert groups[0]["detail"]["transitive_closure_used"] is False
    for state in states.values():
        assert "fanout_conflict" not in state.conflicts
        assert set(state.flags) == {"fanout_review_required"}
        assert (state.row["proposal"], state.row["auto_merge_eligible"]) == (
            "merge_candidate",
            0,
        )
        assert validator._queue_reasons(state) == ("merge_candidate",)


def test_wp22_binding_requires_reject_and_country_conflict() -> None:
    for candidate_id in validator.WP22_REGRESSION_IDS:
        binding = validator._wp22_expected_binding(candidate_id)
        assert binding["expected"] == {
            "proposal": "reject",
            "hard_conflict": "country_site_incompatible",
        }
        assert binding["case_id"] == "rgr_" + validator.core._stable(
            validator.POLICY_VERSION,
            "wp22_country_conflict_reject",
            "WP22",
            candidate_id,
        )[:32]


def test_missing_artifact_fails_closed_without_report(tmp_path: Path) -> None:
    report = tmp_path / "reports" / "validation.json"
    with pytest.raises(validator.IdentityCandidateV4ValidationError, match="artifact is missing"):
        validator.validate_identity_candidates_v4(
            repo_root=tmp_path,
            artifact_path=Path("missing.db"),
            report_path=report,
            verify_full_hashes=False,
        )
    assert not report.exists()
    assert not report.with_name(report.name + ".tmp").exists()


def test_report_writer_serializes_atomically_and_refuses_clobber(tmp_path: Path) -> None:
    report = tmp_path / "nested" / "validation.json"
    payload = {"status": "PASS", "metrics": {"queue": 5_100}}
    validator._write_json_no_clobber(report, payload)
    first_bytes = report.read_bytes()

    assert json.loads(first_bytes) == payload
    assert first_bytes.endswith(b"\n")
    assert not report.with_name(report.name + ".tmp").exists()
    with pytest.raises(validator.IdentityCandidateV4ValidationError, match="clobber"):
        validator._write_json_no_clobber(report, {"status": "FAIL"})
    assert report.read_bytes() == first_bytes
