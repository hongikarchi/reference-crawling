from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from PIL import Image

import canonical.divisare_vision_axes_benchmark as axes_benchmark
from canonical.divisare_image_smoke import FetchPayload, canonical_json
from canonical.divisare_vision_axes import (
    AXIS_CONTRACT_VERSION,
    AXIS_OUTPUT_SCHEMA,
    AXIS_PROMPT_VERSION,
    derive_classification,
)
from canonical.divisare_vision_axes_benchmark import (
    AXIS_GOLD_MANIFEST_VERSION,
    DEVELOPMENT_PURPOSE,
    FRESH_HOLDOUT_PURPOSE,
    HOLDOUT_AXIS_GOLD_MANIFEST_VERSION,
    HOLDOUT_GOLD_FINALIZER_VERSION,
    HOLDOUT_PROMPT_FREEZE_POLICY,
    HOLDOUT_SELECTION_POLICY_VERSION,
    HOLDOUT_SELECTION_SALT,
    SELECTION_POLICY_VERSION,
    SOURCE_PROFILE,
    _gold_applicability_options,
    _gold_derived_options,
    _gold_uncertain_axes,
    _is_applicable,
    _load_axis_gold_samples,
    _nested_prefix_metrics,
    axis_gold_logical_sha256,
    axis_gold_manifest_sha256,
    initialize_sidecar,
    load_axis_gold_manifest,
    run_axes_benchmark,
)
from canonical.divisare_vision_runtime import run_codex_vision_batch
from tools.run_divisare_vision_axes_benchmark import main


ROOT = Path(__file__).resolve().parents[1]
FROZEN_HOLDOUT_GOLD = (
    ROOT / "data" / "review" / "divisare_vision_axes_holdout_gold_n50_v1.json"
)
FROZEN_HOLDOUT_SOURCE = ROOT / "data" / "curated" / "divisare_metadata_v2_4.db"


def _jpeg_bytes(index: int) -> bytes:
    image = Image.new(
        "RGB",
        (1600, 900),
        ((index * 41) % 256, (index * 79) % 256, (index * 113) % 256),
    )
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=94)
    return output.getvalue()


def _decision(primary, *, acceptable=None, clarity="clear") -> dict:
    if clarity == "not_judgeable":
        return {"primary": None, "acceptable_labels": [], "clarity": clarity}
    return {
        "primary": primary,
        "acceptable_labels": list(acceptable if acceptable is not None else [primary]),
        "clarity": clarity,
    }


def _review_and_model(index: int) -> tuple[dict, dict]:
    kind = index % 3
    if kind == 0:
        model = {
            "in_scope": False,
            "reject_reason": "non_architectural_subject",
            "medium": "photograph",
            "spatial_context": "not_applicable",
            "framing_scale": "not_applicable",
            "camera_angle": "not_applicable",
            "drawing_kind": "not_applicable",
            "project_state": "not_applicable",
        }
        review = {
            "in_scope": _decision(False),
            "reject_reason": _decision("non_architectural_subject"),
            "axes": {
                "medium": _decision("photograph"),
                "spatial_context": _decision(None, clarity="not_judgeable"),
                "framing_scale": _decision(None, clarity="not_judgeable"),
                "camera_angle": _decision(None, clarity="not_judgeable"),
                "drawing_kind": _decision(None, clarity="not_judgeable"),
                "project_state": _decision(None, clarity="not_judgeable"),
            },
        }
    elif kind == 1:
        model = {
            "in_scope": True,
            "reject_reason": "none",
            "medium": "photograph",
            "spatial_context": "exterior",
            "framing_scale": "overall",
            "camera_angle": "eye_level",
            "drawing_kind": "not_applicable",
            "project_state": "visibly_finished",
        }
        review = {
            "in_scope": _decision(True),
            "reject_reason": _decision("none"),
            "axes": {
                "medium": _decision("photograph"),
                "spatial_context": _decision("exterior"),
                "framing_scale": _decision("overall"),
                "camera_angle": _decision("eye_level"),
                "drawing_kind": _decision(None, clarity="not_judgeable"),
                "project_state": _decision("visibly_finished"),
            },
        }
    else:
        model = {
            "in_scope": True,
            "reject_reason": "none",
            "medium": "drawing",
            "spatial_context": "not_applicable",
            "framing_scale": "not_applicable",
            "camera_angle": "not_applicable",
            "drawing_kind": "plan",
            "project_state": "not_applicable",
        }
        review = {
            "in_scope": _decision(True),
            "reject_reason": _decision("none"),
            "axes": {
                "medium": _decision("drawing"),
                "spatial_context": _decision(None, clarity="not_judgeable"),
                "framing_scale": _decision(None, clarity="not_judgeable"),
                "camera_angle": _decision(None, clarity="not_judgeable"),
                "drawing_kind": _decision("plan"),
                "project_state": _decision(None, clarity="not_judgeable"),
            },
        }
    normalized_for_derive = {
        **model,
        "uncertain_axes": (),
        "resolution_insufficient": False,
    }
    review["derived_classification"] = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in derive_classification(normalized_for_derive).items()
    }
    return review, model


def _rehash(payload: dict) -> None:
    payload.pop("logical_sha256", None)
    payload.pop("gold_manifest_sha256", None)
    payload["logical_sha256"] = axis_gold_logical_sha256(payload)
    payload["gold_manifest_sha256"] = axis_gold_manifest_sha256(payload)


def _make_manifest(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, bytes], dict[str, dict]]:
    source = tmp_path / "source.db"
    source.write_bytes(b"frozen-divisare-source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    schema_sha = hashlib.sha256(canonical_json(AXIS_OUTPUT_SCHEMA).encode()).hexdigest()
    images: dict[str, bytes] = {}
    model_rows: dict[str, dict] = {}
    samples: list[dict] = []
    for rank in range(1, 51):
        review_id = "blind-%s" % hashlib.sha256(("review-%d" % rank).encode()).hexdigest()[:16]
        raw = _jpeg_bytes(rank)
        request_url = "https://images.divisare.com/images/%s/v1/axis-%04d.jpg" % (
            SOURCE_PROFILE,
            rank,
        )
        review, model = _review_and_model(rank)
        model_rows[review_id] = {"asset_id": review_id, **model}
        images[request_url] = raw
        samples.append(
            {
                "sample_id": "axis-sample-%04d" % rank,
                "sample_rank": rank,
                "review_id": review_id,
                "subset_membership": [
                    name
                    for name, threshold in (("N10", 10), ("N20", 20), ("N50", 50))
                    if rank <= threshold
                ],
                "selection_audit": {"stratum": "test"},
                "source_identity": {
                    "candidate_id": "candidate-%04d" % rank,
                    "asset_key": "divisare|asset-%04d|v1" % rank,
                    "article_id": rank,
                    "building_id": "building-%04d" % rank,
                    "generation_group": "modern" if rank % 2 else "legacy",
                    "url_generation": "cloudinary_public_id" if rank % 2 else "project_images",
                    "request_url": request_url,
                },
                "image_evidence": {
                    "content_sha256": hashlib.sha256(raw).hexdigest(),
                    "pixel_sha256": hashlib.sha256(("pixel-%04d" % rank).encode()).hexdigest(),
                    "phash_256": hashlib.sha256(("phash-%04d" % rank).encode()).hexdigest(),
                },
                "human_review": review,
            }
        )
    provenance = {
        key: hashlib.sha256(key.encode()).hexdigest()
        for key in (
            "candidate_dev_manifest_sha256",
            "candidate_dev_manifest_file_sha256",
            "candidate_dev_manifest_logical_sha256",
            "parent_candidate_manifest_sha256",
            "parent_candidate_manifest_file_sha256",
            "parent_reviewed_pool_sha256",
            "parent_reviewed_pool_file_sha256",
            "old_gold_manifest_sha256",
            "old_gold_manifest_file_sha256",
            "old_n100_db_file_sha256",
            "old_n100_db_logical_sha256",
            "codebook_sha256",
            "adjudication_sha256",
        )
    }
    provenance.update(
        {
            "source_db_filename": source.name,
            "source_db_sha256": source_sha,
            "axis_output_schema_sha256": schema_sha,
            "axis_contract_version": AXIS_CONTRACT_VERSION,
            "axis_prompt_version": AXIS_PROMPT_VERSION,
            "reviewer_annotation_sha256s": ["1" * 64, "2" * 64],
            "reviewer": "codex-test-panel",
            "review_exported_at": "2026-08-05T00:00:00Z",
            "independent_human": False,
        }
    )
    payload = {
        "manifest_version": AXIS_GOLD_MANIFEST_VERSION,
        "purpose": DEVELOPMENT_PURPOSE,
        "development_only": True,
        "provenance": provenance,
        "selection_policy": {
            "policy_version": SELECTION_POLICY_VERSION,
            "prefix_limits": [10, 20, 50],
            "selection_salt": "test-frozen-prefix",
        },
        "samples": samples,
    }
    _rehash(payload)
    manifest = tmp_path / "axis-gold.json"
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    return source, manifest, images, model_rows


def _convert_to_holdout(manifest: Path) -> dict:
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    def sha(label: str) -> str:
        return hashlib.sha256(label.encode()).hexdigest()

    reviewer_shas = [sha("holdout-review-a"), sha("holdout-review-b")]
    adjudication_sha = sha("holdout-adjudication")
    codebook_sha = sha("holdout-codebook")
    schema_sha = hashlib.sha256(canonical_json(AXIS_OUTPUT_SCHEMA).encode()).hexdigest()
    payload.update(
        {
            "manifest_version": HOLDOUT_AXIS_GOLD_MANIFEST_VERSION,
            "finalizer_version": HOLDOUT_GOLD_FINALIZER_VERSION,
            "purpose": FRESH_HOLDOUT_PURPOSE,
            "development_only": False,
            "provenance": {
                "source_db_sha256": payload["provenance"]["source_db_sha256"],
                "parent_probed_n100": {
                    "filename": "fresh-n100-probed.json",
                    "file_sha256": sha("fresh-n100-probed-file"),
                    "manifest_sha256": sha("fresh-n100-probed-manifest"),
                    "base_probe_logical_sha256": sha("fresh-n100-base-probe"),
                    "cross_logical_sha256": sha("fresh-n100-cross-probe"),
                },
                "parent_candidate_n100": {
                    "filename": "fresh-n100.json",
                    "file_sha256": sha("fresh-n100-file"),
                    "manifest_sha256": sha("fresh-n100-manifest"),
                },
                "prior_probed_n560": {
                    "filename": "prior-n560-probed.json",
                    "file_sha256": sha("prior-n560-file"),
                    "manifest_sha256": sha("prior-n560-manifest"),
                },
                "prompt_freeze": {
                    "axis_contract_version": AXIS_CONTRACT_VERSION,
                    "axis_prompt_version": AXIS_PROMPT_VERSION,
                    "codebook_sha256": codebook_sha,
                    "axis_output_schema_sha256": schema_sha,
                    "policy": HOLDOUT_PROMPT_FREEZE_POLICY,
                },
                "candidate_holdout_manifest_sha256": sha("holdout-candidate"),
                "candidate_holdout_manifest_file_sha256": sha(
                    "holdout-candidate-file"
                ),
                "candidate_holdout_manifest_logical_sha256": sha(
                    "holdout-candidate-logical"
                ),
                "codebook_sha256": codebook_sha,
                "axis_output_schema_sha256": schema_sha,
                "adjudication_sha256": adjudication_sha,
                "reviewer_annotation_sha256s": reviewer_shas,
                "reviewer": "holdout-reviewer-a+holdout-reviewer-b",
                "independent_human": False,
                "axis_contract_version": AXIS_CONTRACT_VERSION,
                "axis_prompt_version": AXIS_PROMPT_VERSION,
            },
            "selection_policy": {
                "policy_version": HOLDOUT_SELECTION_POLICY_VERSION,
                "prefix_limits": [10, 20, 50],
                "selection_salt": HOLDOUT_SELECTION_SALT,
            },
            "review_process": {
                "source_visibility": "pixels_and_opaque_id_only",
                "image_long_edge": 1024,
                "independent_human": False,
                "reviewers": [
                    {
                        "reviewer_id": "holdout-reviewer-a",
                        "review_context_id": "holdout-context-a",
                        "file_sha256": reviewer_shas[0],
                        "logical_sha256": sha("holdout-review-a-logical"),
                    },
                    {
                        "reviewer_id": "holdout-reviewer-b",
                        "review_context_id": "holdout-context-b",
                        "file_sha256": reviewer_shas[1],
                        "logical_sha256": sha("holdout-review-b-logical"),
                    },
                ],
                "adjudication": {
                    "provided": True,
                    "filename": "holdout-adjudication.json",
                    "file_sha256": adjudication_sha,
                    "logical_sha256": sha("holdout-adjudication-logical"),
                    "adjudicator_id": "holdout-adjudicator",
                    "rows": [],
                },
            },
        }
    )
    _rehash(payload)
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    return payload


def _event_stream(records: list[dict]) -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps({"results": records}),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 100,
                        "output_tokens": 50,
                    },
                }
            ),
        ]
    )


def _fake_executor(
    rows: dict[str, dict],
    calls: list[list[str]],
    transient_paths: list[Path],
    *,
    fail_once_at: int | None = None,
):
    failed = False

    def execute(**kwargs):
        nonlocal failed
        ids = list(kwargs["expected_asset_ids"])
        calls.append(ids)
        for path in kwargs["image_paths"]:
            transient_paths.append(Path(path))
            with Image.open(path) as image:
                assert max(image.size) <= 1024
        call_no = len(calls)

        def runner(command: list[str], **_run_kwargs):
            nonlocal failed
            if fail_once_at == call_no and not failed:
                failed = True
                return subprocess.CompletedProcess(command, 7, stdout="", stderr="forced")
            output = []
            for review_id in ids:
                output.append(
                    {
                        **rows[review_id],
                        "uncertain_axes": rows[review_id].get("uncertain_axes", []),
                        "resolution_insufficient": rows[review_id].get(
                            "resolution_insufficient", False
                        ),
                        "evidence": "Visible pixels support this axis classification.",
                    }
                )
            return subprocess.CompletedProcess(
                command, 0, stdout=_event_stream(output), stderr=""
            )

        return run_codex_vision_batch(**kwargs, runner=runner)

    return execute


def test_manifest_contract_freezes_nested_prefix_and_oos_medium(tmp_path: Path) -> None:
    source, manifest, _images, _rows = _make_manifest(tmp_path)
    payload, n10, _file_sha, _source_sha = load_axis_gold_manifest(manifest, source, 10)
    _, n20, _, _ = load_axis_gold_manifest(manifest, source, 20)
    _, n50, _, _ = load_axis_gold_manifest(manifest, source, 50)
    assert [row.review_id for row in n10] == [row.review_id for row in n20[:10]]
    assert [row.review_id for row in n20] == [row.review_id for row in n50[:20]]
    assert payload["development_only"] is True
    oos = n10[2]
    assert oos.gold["in_scope"].primary is False
    assert oos.gold["medium"].primary == "photograph"
    assert oos.gold["spatial_context"].clarity == "not_judgeable"
    assert _is_applicable("spatial_context", "unknown") is True
    assert _is_applicable("spatial_context", "not_applicable") is False
    with pytest.raises(ValueError, match="limit must be one of"):
        load_axis_gold_manifest(manifest, source, 11)


def test_manifest_accepts_only_frozen_n50_holdout() -> None:
    loaded, samples, _file_sha, _source_sha = load_axis_gold_manifest(
        FROZEN_HOLDOUT_GOLD, FROZEN_HOLDOUT_SOURCE, 50
    )
    assert loaded["manifest_version"] == HOLDOUT_AXIS_GOLD_MANIFEST_VERSION
    assert loaded["purpose"] == FRESH_HOLDOUT_PURPOSE
    assert loaded["development_only"] is False
    assert len(samples) == 50
    for disallowed_limit in (10, 20):
        with pytest.raises(ValueError, match="only one N50 run"):
            load_axis_gold_manifest(
                FROZEN_HOLDOUT_GOLD,
                FROZEN_HOLDOUT_SOURCE,
                disallowed_limit,
            )


def test_self_consistent_fake_holdout_cannot_pass_frozen_lineage(tmp_path: Path) -> None:
    source, manifest, _images, _rows = _make_manifest(tmp_path)
    _convert_to_holdout(manifest)
    with pytest.raises(ValueError, match="not frozen|frozen lineage"):
        load_axis_gold_manifest(manifest, source, 50)


def test_holdout_reviewer_lineage_tamper_is_rejected_without_touching_file() -> None:
    payload = json.loads(FROZEN_HOLDOUT_GOLD.read_text(encoding="utf-8"))
    payload["review_process"]["reviewers"][0]["file_sha256"] = "f" * 64
    _rehash(payload)
    with pytest.raises(ValueError, match="does not bind reviewer file SHAs"):
        _load_axis_gold_samples(payload)


def test_holdout_rejects_runtime_prompt_body_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        axes_benchmark,
        "compose_axes_prompt",
        lambda _review_ids: "tampered runtime prompt",
    )
    with pytest.raises(ValueError, match="runtime prompt body differs"):
        load_axis_gold_manifest(
            FROZEN_HOLDOUT_GOLD,
            FROZEN_HOLDOUT_SOURCE,
            50,
        )


def test_gold_prompt_version_is_provenance_not_runtime_compatibility(
    tmp_path: Path,
) -> None:
    source, manifest, _images, _rows = _make_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["provenance"]["axis_prompt_version"] = "older-gold-prompt-v1"
    _rehash(payload)
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    loaded, samples, _file_sha, _source_sha = load_axis_gold_manifest(
        manifest, source, 10
    )
    assert loaded["provenance"]["axis_prompt_version"] == "older-gold-prompt-v1"
    assert len(samples) == 10


def test_manifest_rejects_judgeable_semantic_axis_for_oos(tmp_path: Path) -> None:
    source, manifest, _images, _rows = _make_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    sample = payload["samples"][2]
    sample["human_review"]["axes"]["spatial_context"] = _decision("exterior")
    # Keep the declared classification internally valid so this exercises OOS applicability.
    _rehash(payload)
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="out-of-scope axes must be not_judgeable"):
        load_axis_gold_manifest(manifest, source, 10)


@pytest.mark.parametrize("field", ["reject_reason", "medium"])
def test_manifest_requires_scope_medium_and_reject_reason_to_be_judgeable(
    tmp_path: Path, field: str
) -> None:
    source, manifest, _images, _rows = _make_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    sample = payload["samples"][0]
    if field == "medium":
        sample["human_review"]["axes"][field] = _decision(
            None, clarity="not_judgeable"
        )
    else:
        sample["human_review"][field] = _decision(None, clarity="not_judgeable")
    _rehash(payload)
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"{field} must be judgeable"):
        load_axis_gold_manifest(manifest, source, 10)


def test_conditional_applicability_and_uncertainty_are_not_forced_wrong() -> None:
    review, _model = _review_and_model(1)
    gold = {
        "in_scope": _decision(True, acceptable=[True, False], clarity="boundary"),
        "reject_reason": _decision(
            "none",
            acceptable=["none", "people_or_event"],
            clarity="boundary",
        ),
        **review["axes"],
    }
    assert _gold_applicability_options(gold, "spatial_context") == {False, True}
    assert _gold_applicability_options(gold, "project_state") == {False, True}
    assert _gold_applicability_options(gold, "drawing_kind") == {False}
    assert _gold_uncertain_axes(gold) == {"scope"}

    gold["in_scope"] = _decision(True)
    gold["reject_reason"] = _decision("none")
    gold["medium"] = _decision(
        "rendering", acceptable=["rendering", "mixed"], clarity="boundary"
    )
    gold["spatial_context"] = _decision("exterior")
    gold["framing_scale"] = _decision("overall")
    gold["camera_angle"] = _decision("eye_level")
    gold["drawing_kind"] = _decision("perspective")
    gold["project_state"] = _decision(None, clarity="not_judgeable")
    assert _gold_applicability_options(gold, "camera_angle") == {False, True}
    assert _gold_applicability_options(gold, "project_state") == {False}
    assert _gold_uncertain_axes(gold) == {"medium"}


def test_clear_unknown_gold_value_still_requires_an_uncertainty_flag() -> None:
    gold = {
        "in_scope": _decision(True),
        "reject_reason": _decision("none"),
        "medium": _decision("unknown"),
        "spatial_context": _decision(None, clarity="not_judgeable"),
        "framing_scale": _decision(None, clarity="not_judgeable"),
        "camera_angle": _decision(None, clarity="not_judgeable"),
        "drawing_kind": _decision(None, clarity="not_judgeable"),
        "project_state": _decision(None, clarity="not_judgeable"),
    }
    assert _gold_uncertain_axes(gold) == {"medium"}


def test_gold_derived_options_include_each_coherent_accepted_branch() -> None:
    review, _model = _review_and_model(1)
    review["axes"]["framing_scale"] = _decision(
        "overall", acceptable=["overall", "element_detail"], clarity="boundary"
    )
    gold = {
        "in_scope": review["in_scope"],
        "reject_reason": review["reject_reason"],
        **review["axes"],
    }
    options = _gold_derived_options(gold)
    assert {
        (
            option["primary_class"],
            tuple(option["secondary_classes"]),
            option["usage_status"],
        )
        for option in options
    } == {
        ("exterior", (), "review_required"),
        ("detail", ("exterior",), "review_required"),
    }


@pytest.mark.parametrize("index", [1, 2, 3])
def test_gold_derived_options_leave_clear_gold_unchanged(index: int) -> None:
    review, _model = _review_and_model(index)
    gold = {
        "in_scope": review["in_scope"],
        "reject_reason": review["reject_reason"],
        **review["axes"],
    }
    assert _gold_derived_options(gold) == (review["derived_classification"],)


def test_metrics_accept_derived_result_from_non_primary_gold_branch(
    tmp_path: Path,
) -> None:
    source, manifest, images, rows = _make_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    sample = payload["samples"][0]
    review = sample["human_review"]
    review["axes"]["framing_scale"] = _decision(
        "overall", acceptable=["overall", "element_detail"], clarity="boundary"
    )
    review["derived_classification"]["usage_status"] = "review_required"
    model = rows[sample["review_id"]]
    model["framing_scale"] = "element_detail"
    model["uncertain_axes"] = ["framing_scale"]
    _rehash(payload)
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")

    result = run_axes_benchmark(
        source_db=source,
        gold_manifest_path=manifest,
        output_db=tmp_path / "derived-boundary.db",
        report_path=tmp_path / "derived-boundary.md",
        limit=10,
        codex_bin=Path("fake.exe"),
        fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
        executor=_fake_executor(rows, [], []),
    )
    derived = result["metrics"]["derived_classification"]
    assert derived["ambiguous_derived_sample_count"] == 1
    assert derived["accepted_branch_option_count"] == 11
    assert derived["classification_tuple_correct"] == 10
    assert derived["primary_class_correct"] == 10
    assert derived["secondary_classes_exact"] == 10
    assert derived["usage_status_correct"] == 10
    assert derived["clear_sample_count"] == 9
    assert derived["clear_primary_class_correct"] == 9
    assert derived["clear_secondary_classes_exact"] == 9
    assert derived["clear_usage_status_correct"] == 9


def test_metrics_exclude_conditional_applicability_and_count_missed_uncertainty(
    tmp_path: Path,
) -> None:
    source, manifest, images, rows = _make_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    review = payload["samples"][0]["human_review"]
    review["in_scope"] = _decision(
        True, acceptable=[True, False], clarity="boundary"
    )
    review["reject_reason"] = _decision(
        "none", acceptable=["none", "people_or_event"], clarity="boundary"
    )
    review["derived_classification"]["usage_status"] = "review_required"
    rows[payload["samples"][0]["review_id"]]["uncertain_axes"] = [
        "spatial_context"
    ]
    _rehash(payload)
    manifest.write_text(canonical_json(payload) + "\n", encoding="utf-8")

    output = tmp_path / "boundary.db"
    result = run_axes_benchmark(
        source_db=source,
        gold_manifest_path=manifest,
        output_db=output,
        report_path=tmp_path / "boundary.md",
        limit=10,
        codex_bin=Path("fake.exe"),
        fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
        executor=_fake_executor(rows, [], []),
    )
    metrics = result["metrics"]
    assert metrics["axes"]["spatial_context"]["all"]["applicability_judged"] == 9
    assert metrics["axes"]["drawing_kind"]["all"]["applicability_judged"] == 10
    assert metrics["uncertainty"] == {
        "gold_boundary_images": 1,
        "gold_boundary_axis_occurrences": 1,
        "boundary_images_flagged_uncertain": 0,
        "predicted_uncertain_axis_occurrences": 1,
        "uncertain_axis_scored_predicted_occurrences": 0,
        "uncertain_axis_skipped_conditional_applicability": 1,
        "uncertain_axis_true_positive": 0,
        "uncertain_axis_false_positive": 0,
        "uncertain_axis_false_negative": 1,
        "uncertain_axis_recall": 0.0,
        "uncertain_axis_precision": None,
        "resolution_insufficient_count": 0,
    }
    assert (
        "Model uncertainty flags scored: `0`; skipped because accepted "
        "scope/medium branches disagree on axis applicability: `1`"
        in (tmp_path / "boundary.md").read_text(encoding="utf-8")
    )
    with sqlite3.connect(output) as conn:
        assert conn.execute(
            "SELECT applicability_judged FROM axis_metrics "
            "WHERE field_name='spatial_context' AND scope='all'"
        ).fetchone()[0] == 9


def test_axes_n10_end_to_end_is_1024_transient_and_plain_language(tmp_path: Path) -> None:
    source, manifest, images, rows = _make_manifest(tmp_path)
    output = tmp_path / "axes-n10.db"
    report = tmp_path / "axes-n10.md"
    calls: list[list[str]] = []
    transient_paths: list[Path] = []
    result = run_axes_benchmark(
        source_db=source,
        gold_manifest_path=manifest,
        output_db=output,
        report_path=report,
        limit=10,
        codex_bin=Path("fake-codex.exe"),
        model="test-model",
        cli_version="codex-cli 0.146.0",
        fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
        executor=_fake_executor(rows, calls, transient_paths),
    )
    assert result["technical_gate_passed"] is True
    assert result["development_only"] is True
    assert len(calls) == 2 and all(len(call) == 5 for call in calls)
    assert transient_paths and all(not path.exists() for path in transient_paths)
    aggregate = result["metrics"]["aggregate"]
    assert aggregate["all_judged_fields_acceptable"] == 10
    assert aggregate["applicable_field_acceptable_correct"] == aggregate[
        "applicable_field_acceptable_total"
    ]
    assert result["metrics"]["derived_classification"]["secondary_classes_micro_f1"] == 1.0
    assert result["metrics"]["derived_classification"]["clear_sample_count"] == 10
    assert result["metrics"]["uncertainty"]["gold_boundary_images"] == 0
    assert result["metrics"]["usage"]["input_tokens"] == 2000
    text = report.read_text(encoding="utf-8")
    assert "Transport/schema result: **PASS**" in text
    assert "not a final or production accuracy claim" in text
    assert "every judgeable field was acceptable: `10/10`" in text
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gold_samples").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM derived_inputs").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM vision_attempts").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM axis_metrics").fetchone()[0] == 32
        assert "applicability_judged" in {
            row[1] for row in conn.execute("PRAGMA table_info(axis_metrics)")
        }
        assert conn.execute("SELECT COUNT(*) FROM validations WHERE passed=0").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(FileExistsError, match="immutable output"):
        run_axes_benchmark(
            source_db=source,
            gold_manifest_path=manifest,
            output_db=output,
            report_path=tmp_path / "other.md",
            limit=10,
            codex_bin=Path("fake.exe"),
            fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
            executor=_fake_executor(rows, [], []),
        )


def test_fresh_holdout_receipt_allows_only_same_output_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root = tmp_path / "receipts"
    monkeypatch.setattr(
        axes_benchmark, "HOLDOUT_ONE_SHOT_RECEIPT_ROOT", receipt_root
    )
    output = tmp_path / "holdout-n50.db"
    report = tmp_path / "holdout-n50.md"
    fetch_calls: list[str] = []
    executor_calls: list[bool] = []

    def failed_fetch(url: str) -> FetchPayload:
        fetch_calls.append(url)
        raise OSError("forced pre-model stop")

    def forbidden_executor(**_kwargs):
        executor_calls.append(True)
        raise AssertionError("model executor must not be reached")

    arguments = {
        "source_db": FROZEN_HOLDOUT_SOURCE,
        "gold_manifest_path": FROZEN_HOLDOUT_GOLD,
        "output_db": output,
        "report_path": report,
        "limit": 50,
        "codex_bin": Path("fake-codex.exe"),
        "model": "test-model",
        "cli_version": "codex-cli 0.146.0",
        "fetcher": failed_fetch,
        "executor": forbidden_executor,
    }
    with pytest.raises(RuntimeError, match="fetch failed"):
        run_axes_benchmark(**arguments)
    receipts = list(receipt_root.glob("*.json"))
    assert len(receipts) == 1
    assert fetch_calls and not executor_calls
    partial = output.with_name(output.name + ".partial")
    with sqlite3.connect(partial) as conn:
        receipt_path, receipt_sha, sample_order_sha = conn.execute(
            "SELECT one_shot_receipt_path,one_shot_receipt_sha256,"
            "gold_sample_order_sha256 FROM benchmark_run WHERE run_id=1"
        ).fetchone()
    assert Path(receipt_path) == receipts[0].resolve()
    assert len(receipt_sha) == len(sample_order_sha) == 64

    with pytest.raises(RuntimeError, match="fetch failed"):
        run_axes_benchmark(**arguments, resume=True)
    with pytest.raises(RuntimeError, match="different run contract"):
        run_axes_benchmark(
            **{
                **arguments,
                "output_db": tmp_path / "alternate.db",
                "report_path": tmp_path / "alternate.md",
            }
        )
    with pytest.raises(RuntimeError, match="already claimed"):
        run_axes_benchmark(**arguments)


def test_nested_prefix_metrics_are_slices_of_one_n50_result(tmp_path: Path) -> None:
    source, manifest, images, rows = _make_manifest(tmp_path)
    output = tmp_path / "single-n50.db"
    result = run_axes_benchmark(
        source_db=source,
        gold_manifest_path=manifest,
        output_db=output,
        report_path=tmp_path / "single-n50.md",
        limit=50,
        codex_bin=Path("fake.exe"),
        fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
        executor=_fake_executor(rows, [], []),
    )
    with sqlite3.connect(output) as conn:
        prefixes = _nested_prefix_metrics(conn)
    assert [prefixes[name]["aggregate"]["sample_count"] for name in ("N10", "N20", "N50")] == [10, 20, 50]
    assert prefixes["N50"]["aggregate"] == result["metrics"]["aggregate"]


def test_axes_resume_retains_failed_attempt_and_finishes_exact_prefix(tmp_path: Path) -> None:
    source, manifest, images, rows = _make_manifest(tmp_path)
    output = tmp_path / "resume.db"
    report = tmp_path / "resume.md"
    first_calls: list[list[str]] = []
    with pytest.raises(RuntimeError, match="batch 2 failed"):
        run_axes_benchmark(
            source_db=source,
            gold_manifest_path=manifest,
            output_db=output,
            report_path=report,
            limit=10,
            codex_bin=Path("fake.exe"),
            model="test-model",
            cli_version="codex-cli 0.146.0",
            fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
            executor=_fake_executor(rows, first_calls, [], fail_once_at=2),
        )
    assert not output.exists() and output.with_name(output.name + ".partial").exists()
    resume_calls: list[list[str]] = []
    result = run_axes_benchmark(
        source_db=source,
        gold_manifest_path=manifest,
        output_db=output,
        report_path=report,
        limit=10,
        codex_bin=Path("fake.exe"),
        model="test-model",
        cli_version="codex-cli 0.146.0",
        resume=True,
        fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
        executor=_fake_executor(rows, resume_calls, []),
    )
    assert result["technical_gate_passed"] is True
    assert len(first_calls) == 2 and len(resume_calls) == 1
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vision_attempts WHERE status='failed'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM vision_attempts WHERE status='success'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 15


def _initialized_dev_partial(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, bytes], dict[str, dict]]:
    source, manifest, images, rows = _make_manifest(tmp_path)
    payload, samples, manifest_sha, source_sha = load_axis_gold_manifest(
        manifest, source, 10
    )
    output = tmp_path / "tampered-resume.db"
    partial = output.with_name(output.name + ".partial")
    with sqlite3.connect(partial) as conn:
        initialize_sidecar(
            conn,
            samples=samples,
            manifest_path=manifest.resolve(),
            manifest_payload=payload,
            manifest_file_sha256=manifest_sha,
            source_db=source.resolve(),
            source_sha256=source_sha,
            model="test-model",
            reasoning="low",
            service_tier="fast",
            cli_version="codex-cli 0.146.0",
        )
    return source, manifest, output, images, rows


def test_resume_rejects_tampered_frozen_header_fields(tmp_path: Path) -> None:
    source, manifest, output, images, rows = _initialized_dev_partial(tmp_path)
    partial = output.with_name(output.name + ".partial")
    with sqlite3.connect(partial) as conn:
        conn.execute(
            "UPDATE benchmark_run SET reviewer_identifier='changed',"
            "independent_human=1,source_derivative_version='changed',"
            "local_derivative_version='changed' WHERE run_id=1"
        )
    with pytest.raises(RuntimeError, match="resume contract mismatch"):
        run_axes_benchmark(
            source_db=source,
            gold_manifest_path=manifest,
            output_db=output,
            report_path=tmp_path / "tampered-header.md",
            limit=10,
            codex_bin=Path("fake.exe"),
            model="test-model",
            reasoning="low",
            service_tier="fast",
            cli_version="codex-cli 0.146.0",
            resume=True,
            fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
            executor=_fake_executor(rows, [], []),
        )


def test_resume_rejects_tampered_gold_samples(tmp_path: Path) -> None:
    source, manifest, output, images, rows = _initialized_dev_partial(tmp_path)
    partial = output.with_name(output.name + ".partial")
    with sqlite3.connect(partial) as conn:
        conn.execute(
            "UPDATE gold_samples SET gold_review_json='{}' WHERE sample_rank=1"
        )
    with pytest.raises(RuntimeError, match="resume gold_samples mismatch"):
        run_axes_benchmark(
            source_db=source,
            gold_manifest_path=manifest,
            output_db=output,
            report_path=tmp_path / "tampered-gold.md",
            limit=10,
            codex_bin=Path("fake.exe"),
            model="test-model",
            reasoning="low",
            service_tier="fast",
            cli_version="codex-cli 0.146.0",
            resume=True,
            fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
            executor=_fake_executor(rows, [], []),
        )


def test_development_accuracy_does_not_change_technical_gate(tmp_path: Path) -> None:
    source, manifest, images, rows = _make_manifest(tmp_path)
    first_id = json.loads(manifest.read_text(encoding="utf-8"))["samples"][0]["review_id"]
    rows[first_id] = {**rows[first_id], "spatial_context": "interior"}
    result = run_axes_benchmark(
        source_db=source,
        gold_manifest_path=manifest,
        output_db=tmp_path / "wrong-but-valid.db",
        report_path=tmp_path / "wrong-but-valid.md",
        limit=10,
        codex_bin=Path("fake.exe"),
        fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
        executor=_fake_executor(rows, [], []),
    )
    assert result["technical_gate_passed"] is True
    aggregate = result["metrics"]["aggregate"]
    assert aggregate["all_judged_fields_acceptable"] == 9
    assert aggregate["applicable_field_acceptable_correct"] < aggregate[
        "applicable_field_acceptable_total"
    ]


def test_cli_rejects_non_prefix_limit_before_runtime_discovery(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--gold-manifest",
                str(tmp_path / "gold.json"),
                "--output-db",
                str(tmp_path / "out.db"),
                "--report",
                str(tmp_path / "out.md"),
                "--limit",
                "11",
            ]
        )
    assert exc.value.code == 2
