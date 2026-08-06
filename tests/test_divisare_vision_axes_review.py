from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from canonical.divisare_vision_axes import AXIS_CONTRACT_VERSION, AXIS_PROMPT_VERSION
from canonical.divisare_vision_axes_benchmark import (
    AXIS_GOLD_MANIFEST_VERSION,
    DEVELOPMENT_PURPOSE,
    _load_axis_gold_samples,
)
from canonical.divisare_vision_axes_review import (
    ADJUDICATION_VERSION,
    FRESH_HOLDOUT_PURPOSE,
    HOLDOUT_AXIS_GOLD_MANIFEST_VERSION,
    HOLDOUT_GOLD_FINALIZER_VERSION,
    REVIEW_ANNOTATION_VERSION,
    SOURCE_VISIBILITY,
    adjudication_logical_sha256,
    annotation_logical_sha256,
    axes_review_codebook_sha256,
    build_reviewer_annotation_template,
    devset_contract,
    finalize_axes_gold_files,
    seal_reviewer_annotation_file,
    write_reviewer_annotation_template,
)
from canonical import divisare_vision_axes_holdout_selection as holdout_selection


ROOT = Path(__file__).resolve().parents[1]
FRESH_HOLDOUT = (
    ROOT
    / "data"
    / "review"
    / "divisare_vision_axes_holdout_n50_candidates_v1_1.json"
)


@pytest.fixture(autouse=True)
def _isolate_devset_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    # These unit fixtures exercise the review layer. The devset module has its
    # own exact, frozen-manifest validation tests.
    monkeypatch.setattr(devset_contract, "validate_devset_manifest", lambda _payload: None)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_review_codebook_binds_the_frozen_prompt_definitions() -> None:
    assert axes_review_codebook_sha256() == (
        "6bd6642cad29ef109c1d24f16a6c444535ad88fb644c2d8fa394d4b75d2bbc07"
    )


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _candidate_payload() -> dict:
    shas = {
        "source_db_sha256": _digest("source-db"),
        "parent_candidate_manifest": {
            "manifest_sha256": _digest("parent-candidate-logical"),
            "file_sha256": _digest("parent-candidate-file"),
        },
        "parent_reviewed_pool": {
            "reviewed_pool_sha256": _digest("parent-reviewed-logical"),
            "file_sha256": _digest("parent-reviewed-file"),
        },
        "old_gold_manifest": {
            "gold_manifest_sha256": _digest("old-gold-logical"),
            "file_sha256": _digest("old-gold-file"),
        },
        "old_n100_benchmark": {
            "file_sha256": _digest("old-n100-file"),
            "logical_sha256": _digest("old-n100-logical"),
        },
    }
    samples = []
    for rank in range(1, 51):
        review_id = f"axis-{_digest(f'review-{rank}')[:12]}"
        samples.append(
            {
                "sample_rank": rank,
                "review_id": review_id,
                "subset_membership": ["n50"],
                "selection_audit": {},
                "source_identity": {
                    "candidate_id": f"candidate-{rank:04d}",
                    "asset_key": f"asset-{rank:04d}",
                    "article_id": rank,
                    "building_id": f"building-{rank:04d}",
                    "generation_group": "legacy" if rank % 5 == 0 else "modern",
                    "url_generation": "project_images" if rank % 5 == 0 else "cloudinary_public_id",
                    "request_url": (
                        "https://images.divisare.com/image/upload/"
                        f"c_limit,f_jpg,h_2048,q_92,w_2048/asset-{rank:04d}.jpg"
                    ),
                },
                "image_evidence": {
                    "content_sha256": _digest(f"content-{rank}"),
                    "pixel_sha256": _digest(f"pixel-{rank}"),
                    "phash_256": _digest(f"phash-{rank}"),
                },
            }
        )
    review_rows = [
        {"review_rank": rank, "review_id": sample["review_id"]}
        for rank, sample in enumerate(reversed(samples), 1)
    ]
    return {
        "manifest_version": "test-axis-dev-candidates-v1",
        "purpose": DEVELOPMENT_PURPOSE,
        "development_only": True,
        "manifest_sha256": _digest("candidate-self"),
        "logical_sha256": _digest("candidate-logical"),
        "provenance": shas,
        "selection_contract": {"blind_id_version": "axis-review-v1"},
        "review_rows": review_rows,
        "audit_samples": samples,
    }


def _photo_annotation(review_id: str, *, out_of_scope: bool = False) -> dict:
    row = {
        "review_id": review_id,
        "in_scope": not out_of_scope,
        "reject_reason": "text_or_logo_only" if out_of_scope else "none",
        "medium": "photograph",
        "spatial_context": "not_applicable" if out_of_scope else "exterior",
        "framing_scale": "not_applicable" if out_of_scope else "overall",
        "camera_angle": "not_applicable" if out_of_scope else "eye_level",
        "drawing_kind": "not_applicable",
        "project_state": "not_applicable" if out_of_scope else "visibly_finished",
        "uncertain_axes": [],
        "resolution_insufficient": False,
        "evidence": "Visible pixels support this development-set decision.",
        "clarity": {
            "in_scope": "clear",
            "reject_reason": "clear",
            "medium": "clear",
            "spatial_context": "not_judgeable" if out_of_scope else "clear",
            "framing_scale": "not_judgeable" if out_of_scope else "clear",
            "camera_angle": "not_judgeable" if out_of_scope else "clear",
            "drawing_kind": "not_judgeable",
            "project_state": "not_judgeable" if out_of_scope else "clear",
        },
    }
    return row


def _review_payload(
    candidate: dict,
    candidate_path: Path,
    reviewer_id: str,
    context_id: str,
) -> dict:
    by_id = {
        sample["review_id"]: _photo_annotation(
            sample["review_id"], out_of_scope=sample["sample_rank"] == 50
        )
        for sample in candidate["audit_samples"]
    }
    rows = [by_id[row["review_id"]] for row in candidate["review_rows"]]
    payload = {
        "manifest_version": REVIEW_ANNOTATION_VERSION,
        "purpose": DEVELOPMENT_PURPOSE,
        "development_only": True,
        "independent_human": False,
        "reviewer_id": reviewer_id,
        "review_context_id": context_id,
        "source_visibility": SOURCE_VISIBILITY,
        "image_long_edge": 1024,
        "candidate_dev_manifest_file_sha256": hashlib.sha256(
            candidate_path.read_bytes()
        ).hexdigest(),
        "candidate_dev_manifest_logical_sha256": candidate["logical_sha256"],
        "codebook_sha256": axes_review_codebook_sha256(),
        "axis_contract_version": AXIS_CONTRACT_VERSION,
        "axis_prompt_version": AXIS_PROMPT_VERSION,
        "annotations": rows,
    }
    payload["logical_sha256"] = annotation_logical_sha256(payload)
    return payload


def _fixture_files(tmp_path: Path) -> tuple[dict, Path, dict, Path, dict, Path]:
    candidate = _candidate_payload()
    candidate_path = tmp_path / "candidate.json"
    _write(candidate_path, candidate)
    review_a = _review_payload(candidate, candidate_path, "agent-review-a", "context-a")
    review_b = _review_payload(candidate, candidate_path, "agent-review-b", "context-b")
    review_a_path = tmp_path / "review-a.json"
    review_b_path = tmp_path / "review-b.json"
    _write(review_a_path, review_a)
    _write(review_b_path, review_b)
    return candidate, candidate_path, review_a, review_a_path, review_b, review_b_path


def test_identical_double_review_finalizes_runner_compatible_gold(tmp_path: Path) -> None:
    candidate, candidate_path, _, review_a_path, _, review_b_path = _fixture_files(tmp_path)
    output_path = tmp_path / "gold.json"
    payload = finalize_axes_gold_files(
        candidate_dev_manifest_path=candidate_path,
        reviewer_annotation_paths=[review_a_path, review_b_path],
        output_path=output_path,
    )
    assert payload["manifest_version"] == AXIS_GOLD_MANIFEST_VERSION
    assert payload["development_only"] is True
    assert payload["provenance"]["independent_human"] is False
    assert len(payload["samples"]) == 50
    assert payload["samples"][0]["subset_membership"] == ["N10", "N20", "N50"]
    assert payload["samples"][10]["subset_membership"] == ["N20", "N50"]
    assert payload["samples"][20]["subset_membership"] == ["N50"]
    assert len(payload["samples"][0]["review_provenance"]["source_reviews"]) == 2
    assert payload["samples"][-1]["human_review"]["axes"]["medium"]["primary"] == "photograph"
    assert payload["samples"][-1]["human_review"]["axes"]["spatial_context"] == {
        "primary": None,
        "acceptable_labels": [],
        "clarity": "not_judgeable",
    }
    loaded = _load_axis_gold_samples(payload)
    assert len(loaded) == 50
    assert loaded[0].review_id == candidate["audit_samples"][0]["review_id"]
    before = output_path.read_bytes()
    with pytest.raises(FileExistsError, match="overwrite"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=candidate_path,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=output_path,
        )
    assert output_path.read_bytes() == before


def test_template_is_opaque_ordered_no_clobber_and_sealable(tmp_path: Path) -> None:
    candidate, candidate_path, review_a, _, _, _ = _fixture_files(tmp_path)
    template = build_reviewer_annotation_template(
        candidate_dev_manifest_path=candidate_path,
        reviewer_id="template-reviewer",
        review_context_id="template-context",
    )
    expected_ids = [row["review_id"] for row in candidate["review_rows"]]
    assert [row["review_id"] for row in template["annotations"]] == expected_ids
    serialized = json.dumps(template)
    assert "candidate_id" not in serialized
    assert "request_url" not in serialized
    assert all(row["in_scope"] is None for row in template["annotations"])

    template_path = tmp_path / "template.json"
    write_reviewer_annotation_template(
        candidate_dev_manifest_path=candidate_path,
        reviewer_id="template-reviewer",
        review_context_id="template-context",
        output_path=template_path,
    )
    with pytest.raises(FileExistsError, match="overwrite"):
        write_reviewer_annotation_template(
            candidate_dev_manifest_path=candidate_path,
            reviewer_id="template-reviewer",
            review_context_id="template-context",
            output_path=template_path,
        )

    filled = deepcopy(template)
    filled["annotations"] = review_a["annotations"]
    draft_path = tmp_path / "filled-draft.json"
    _write(draft_path, filled)
    sealed_path = tmp_path / "sealed.json"
    sealed = seal_reviewer_annotation_file(
        candidate_dev_manifest_path=candidate_path,
        draft_path=draft_path,
        output_path=sealed_path,
    )
    assert sealed["logical_sha256"] == annotation_logical_sha256(sealed)
    assert sealed_path.exists()
    assert not list(tmp_path.glob("*.validation-only"))


def test_disagreement_requires_sha_bound_adjudication(tmp_path: Path) -> None:
    candidate, candidate_path, _, review_a_path, review_b, review_b_path = _fixture_files(tmp_path)
    review_id = candidate["audit_samples"][0]["review_id"]
    target = next(row for row in review_b["annotations"] if row["review_id"] == review_id)
    target["spatial_context"] = "interior"
    review_b["logical_sha256"] = annotation_logical_sha256(review_b)
    _write(review_b_path, review_b)
    output_path = tmp_path / "gold.json"
    with pytest.raises(ValueError, match="requires adjudication"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=candidate_path,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=output_path,
        )

    reviewer_shas = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (review_a_path, review_b_path)
    ]
    overlay = {
        "manifest_version": ADJUDICATION_VERSION,
        "purpose": DEVELOPMENT_PURPOSE,
        "development_only": True,
        "independent_human": False,
        "adjudicator_id": "agent-adjudicator",
        "candidate_dev_manifest_file_sha256": hashlib.sha256(
            candidate_path.read_bytes()
        ).hexdigest(),
        "candidate_dev_manifest_logical_sha256": candidate["logical_sha256"],
        "reviewer_annotation_file_sha256s": reviewer_shas,
        "adjudications": [
            {
                "review_id": review_id,
                "field": "spatial_context",
                "primary": "exterior",
                "acceptable_labels": ["exterior", "interior"],
                "clarity": "boundary",
                "reason": "reviewer_disagreement",
                "evidence": "The glazing makes the camera side ambiguous.",
            }
        ],
    }
    overlay["logical_sha256"] = adjudication_logical_sha256(overlay)
    overlay_path = tmp_path / "adjudication.json"
    _write(overlay_path, overlay)
    payload = finalize_axes_gold_files(
        candidate_dev_manifest_path=candidate_path,
        reviewer_annotation_paths=[review_a_path, review_b_path],
        adjudication_path=overlay_path,
        output_path=output_path,
    )
    decision = payload["samples"][0]["human_review"]["axes"]["spatial_context"]
    assert decision["clarity"] == "boundary"
    assert decision["acceptable_labels"] == ["exterior", "interior"]
    assert payload["samples"][0]["review_provenance"]["adjudications"] == overlay[
        "adjudications"
    ]
    assert payload["provenance"]["adjudication_sha256"] == hashlib.sha256(
        overlay_path.read_bytes()
    ).hexdigest()
    assert len(_load_axis_gold_samples(payload)) == 50


def test_illegal_axis_combination_is_rejected_before_gold(tmp_path: Path) -> None:
    _, candidate_path, review_a, review_a_path, _, review_b_path = _fixture_files(tmp_path)
    review_a["annotations"][-1]["drawing_kind"] = "elevation"
    review_a["annotations"][-1]["clarity"]["drawing_kind"] = "clear"
    review_a["logical_sha256"] = annotation_logical_sha256(review_a)
    _write(review_a_path, review_a)
    with pytest.raises(ValueError, match="photograph requires drawing_kind"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=candidate_path,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=tmp_path / "gold.json",
        )


def test_annotation_binding_order_and_strict_json_are_enforced(tmp_path: Path) -> None:
    _, candidate_path, review_a, review_a_path, _, review_b_path = _fixture_files(tmp_path)
    review_a["candidate_dev_manifest_file_sha256"] = "f" * 64
    review_a["logical_sha256"] = annotation_logical_sha256(review_a)
    _write(review_a_path, review_a)
    with pytest.raises(ValueError, match="wrong candidate file SHA"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=candidate_path,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=tmp_path / "gold.json",
        )

    review_a_path.write_text(
        '{"manifest_version":"a","manifest_version":"b"}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=candidate_path,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=tmp_path / "strict-gold.json",
        )


def test_boundary_agreement_still_requires_explicit_acceptable_set(tmp_path: Path) -> None:
    _, candidate_path, review_a, review_a_path, review_b, review_b_path = _fixture_files(tmp_path)
    for review in (review_a, review_b):
        review["annotations"][-1]["clarity"]["framing_scale"] = "boundary"
        review["annotations"][-1]["uncertain_axes"] = ["framing_scale"]
        review["logical_sha256"] = annotation_logical_sha256(review)
    _write(review_a_path, review_a)
    _write(review_b_path, review_b)
    with pytest.raises(ValueError, match="requires adjudication"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=candidate_path,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=tmp_path / "gold.json",
        )


def test_boundary_clarity_and_uncertain_axes_are_bidirectionally_bound(
    tmp_path: Path,
) -> None:
    _, candidate_path, review_a, review_a_path, _, review_b_path = _fixture_files(tmp_path)
    review_a["annotations"][-1]["clarity"]["framing_scale"] = "boundary"
    review_a["logical_sha256"] = annotation_logical_sha256(review_a)
    _write(review_a_path, review_a)
    with pytest.raises(ValueError, match="boundary clarity and uncertain_axes"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=candidate_path,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=tmp_path / "missing-uncertain.json",
        )

    review_a["annotations"][-1]["clarity"]["framing_scale"] = "clear"
    review_a["annotations"][-1]["uncertain_axes"] = ["framing_scale"]
    review_a["logical_sha256"] = annotation_logical_sha256(review_a)
    _write(review_a_path, review_a)
    with pytest.raises(ValueError, match="boundary clarity and uncertain_axes"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=candidate_path,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=tmp_path / "extra-uncertain.json",
        )


def test_fresh_holdout_supports_blind_template_seal_adjudication_and_gold(
    tmp_path: Path,
) -> None:
    candidate = json.loads(FRESH_HOLDOUT.read_text(encoding="utf-8"))
    template_a = build_reviewer_annotation_template(
        candidate_dev_manifest_path=FRESH_HOLDOUT,
        reviewer_id="holdout-agent-a",
        review_context_id="holdout-context-a",
    )
    template_b = build_reviewer_annotation_template(
        candidate_dev_manifest_path=FRESH_HOLDOUT,
        reviewer_id="holdout-agent-b",
        review_context_id="holdout-context-b",
    )
    expected_review_ids = [row["review_id"] for row in candidate["review_rows"]]
    assert template_a["purpose"] == FRESH_HOLDOUT_PURPOSE
    assert template_a["development_only"] is False
    assert template_a["independent_human"] is False
    assert [row["review_id"] for row in template_a["annotations"]] == expected_review_ids
    public_text = json.dumps(template_a, sort_keys=True)
    for hidden in (
        "proxy_class",
        "generation_group",
        "request_url",
        "review_url",
        "asset_key",
        "candidate_id",
    ):
        assert hidden not in public_text

    for template in (template_a, template_b):
        template["annotations"] = [
            _photo_annotation(review_id) for review_id in expected_review_ids
        ]
    disputed_id = expected_review_ids[0]
    template_b["annotations"][0]["spatial_context"] = "interior"
    draft_a = tmp_path / "holdout-a.draft.json"
    draft_b = tmp_path / "holdout-b.draft.json"
    _write(draft_a, template_a)
    _write(draft_b, template_b)
    review_a_path = tmp_path / "holdout-a.sealed.json"
    review_b_path = tmp_path / "holdout-b.sealed.json"
    sealed_a = seal_reviewer_annotation_file(
        candidate_dev_manifest_path=FRESH_HOLDOUT,
        draft_path=draft_a,
        output_path=review_a_path,
    )
    sealed_b = seal_reviewer_annotation_file(
        candidate_dev_manifest_path=FRESH_HOLDOUT,
        draft_path=draft_b,
        output_path=review_b_path,
    )
    assert sealed_a["purpose"] == sealed_b["purpose"] == FRESH_HOLDOUT_PURPOSE
    with pytest.raises(ValueError, match="requires adjudication"):
        finalize_axes_gold_files(
            candidate_dev_manifest_path=FRESH_HOLDOUT,
            reviewer_annotation_paths=[review_a_path, review_b_path],
            output_path=tmp_path / "holdout-without-adjudication.json",
        )

    reviewer_shas = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (review_a_path, review_b_path)
    ]
    overlay = {
        "manifest_version": ADJUDICATION_VERSION,
        "purpose": FRESH_HOLDOUT_PURPOSE,
        "development_only": False,
        "independent_human": False,
        "adjudicator_id": "holdout-agent-adjudicator",
        "candidate_dev_manifest_file_sha256": hashlib.sha256(
            FRESH_HOLDOUT.read_bytes()
        ).hexdigest(),
        "candidate_dev_manifest_logical_sha256": candidate["logical_sha256"],
        "reviewer_annotation_file_sha256s": reviewer_shas,
        "adjudications": [
            {
                "review_id": disputed_id,
                "field": "spatial_context",
                "primary": "exterior",
                "acceptable_labels": ["exterior", "interior"],
                "clarity": "boundary",
                "reason": "reviewer_disagreement",
                "evidence": "The camera side of the threshold is visually ambiguous.",
            }
        ],
    }
    overlay["logical_sha256"] = adjudication_logical_sha256(overlay)
    overlay_path = tmp_path / "holdout-adjudication.json"
    _write(overlay_path, overlay)
    output_path = tmp_path / "holdout-gold.json"
    gold = finalize_axes_gold_files(
        candidate_dev_manifest_path=FRESH_HOLDOUT,
        reviewer_annotation_paths=[review_a_path, review_b_path],
        adjudication_path=overlay_path,
        output_path=output_path,
    )

    assert gold["manifest_version"] == HOLDOUT_AXIS_GOLD_MANIFEST_VERSION
    assert gold["finalizer_version"] == HOLDOUT_GOLD_FINALIZER_VERSION
    assert gold["purpose"] == FRESH_HOLDOUT_PURPOSE
    assert gold["development_only"] is False
    assert gold["review_process"]["independent_human"] is False
    assert gold["selection_policy"] == {
        "policy_version": holdout_selection.SELECTOR_VERSION,
        "prefix_limits": [10, 20, 50],
        "selection_salt": holdout_selection.BLIND_ID_VERSION,
    }
    for key, value in candidate["provenance"].items():
        assert gold["provenance"][key] == value
    assert gold["provenance"]["candidate_holdout_manifest_sha256"] == candidate[
        "manifest_sha256"
    ]
    assert gold["provenance"]["candidate_holdout_manifest_file_sha256"] == hashlib.sha256(
        FRESH_HOLDOUT.read_bytes()
    ).hexdigest()
    assert [row["review_id"] for row in gold["samples"]] == [
        row["review_id"] for row in candidate["audit_samples"]
    ]
    for gold_sample, candidate_sample in zip(gold["samples"], candidate["audit_samples"]):
        assert gold_sample["source_identity"]["generation_group"] == candidate_sample[
            "selection_audit"
        ]["generation_group"]
    decision = next(
        row for row in gold["samples"] if row["review_id"] == disputed_id
    )["human_review"]["axes"]["spatial_context"]
    assert decision == {
        "primary": "exterior",
        "acceptable_labels": ["exterior", "interior"],
        "clarity": "boundary",
    }
