"""Portable guards for tests bound to gitignored production artifacts.

The guarded tests remain collected and run automatically whenever their exact
production inputs are present.  A fresh clone cannot reconstruct those inputs:
they are intentionally outside Git, so absence is an environment skip rather
than a product failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
V22_DB = ROOT / "data" / "curated" / "divisare_metadata_v2_2.db"
REVIEW = ROOT / "data" / "review"
AXES_DEV_N50 = REVIEW / "divisare_vision_axes_dev_n50_candidates_v1.json"
HOLDOUT_N100 = REVIEW / "divisare_vision_axes_holdout_candidates_n100_v1.json"
HOLDOUT_N100_PROBED = (
    REVIEW / "divisare_vision_axes_holdout_candidates_n100_v1_probed.json"
)
HOLDOUT_GOLD_N50 = REVIEW / "divisare_vision_axes_holdout_gold_n50_v1.json"
HOLDOUT_N50 = REVIEW / "divisare_vision_axes_holdout_n50_candidates_v1_1.json"
PRIOR_PROBED = REVIEW / "divisare_vision_gold_candidates_v1_2_probed.json"


_REQUIREMENTS: dict[tuple[str, str], tuple[Path, ...]] = {
    (
        "test_divisare_d2_decisions_v23.py",
        "test_component_union_cannot_collapse_a_reject_or_defer",
    ): (V22_DB,),
    (
        "test_divisare_d2_decisions_v23.py",
        "test_exact_parent_pending_pair_snapshot_and_counts",
    ): (V22_DB,),
    (
        "test_divisare_reviewed_v23.py",
        "test_production_validate_only_has_no_output_side_effect",
    ): (V22_DB,),
    (
        "test_divisare_vision_axes_benchmark.py",
        "test_manifest_accepts_only_frozen_n50_holdout",
    ): (HOLDOUT_GOLD_N50,),
    (
        "test_divisare_vision_axes_benchmark.py",
        "test_holdout_reviewer_lineage_tamper_is_rejected_without_touching_file",
    ): (HOLDOUT_GOLD_N50,),
    (
        "test_divisare_vision_axes_benchmark.py",
        "test_holdout_rejects_runtime_prompt_body_tamper",
    ): (HOLDOUT_GOLD_N50,),
    (
        "test_divisare_vision_axes_benchmark.py",
        "test_fresh_holdout_receipt_allows_only_same_output_resume",
    ): (HOLDOUT_GOLD_N50,),
    (
        "test_divisare_vision_axes_holdout_probe.py",
        "test_real_holdout_and_prior_inputs_are_cross_bound",
    ): (HOLDOUT_N100, PRIOR_PROBED),
    (
        "test_divisare_vision_axes_holdout_probe.py",
        "test_prior_probe_contract_must_match_current_identity_runtime",
    ): (HOLDOUT_N100, PRIOR_PROBED),
    (
        "test_divisare_vision_axes_holdout_probe.py",
        "test_probe_resume_publishes_hash_only_manifest_and_rejects_tampering",
    ): (HOLDOUT_N100, PRIOR_PROBED),
    (
        "test_divisare_vision_axes_holdout_probe.py",
        "test_probe_preserves_fetch_failure_and_excludes_it_from_cross_matching",
    ): (HOLDOUT_N100, PRIOR_PROBED),
    (
        "test_divisare_vision_axes_holdout_selection.py",
        "test_fresh_n50_is_balanced_blinded_and_deterministic",
    ): (HOLDOUT_N100_PROBED, HOLDOUT_N100, PRIOR_PROBED),
    (
        "test_divisare_vision_axes_holdout_selection.py",
        "test_fresh_n50_rejects_tampering_and_quota_shortfall",
    ): (HOLDOUT_N100_PROBED, HOLDOUT_N100, PRIOR_PROBED),
    (
        "test_divisare_vision_axes_holdout_selection.py",
        "test_writer_is_no_clobber",
    ): (HOLDOUT_N100_PROBED, HOLDOUT_N100, PRIOR_PROBED),
    (
        "test_divisare_vision_axes_review.py",
        "test_fresh_holdout_supports_blind_template_seal_adjudication_and_gold",
    ): (HOLDOUT_N50,),
    (
        "test_divisare_vision_axes_review_inputs.py",
        "test_frozen_manifest_and_all_supported_prefixes_are_bound",
    ): (AXES_DEV_N50,),
    (
        "test_divisare_vision_axes_review_inputs.py",
        "test_fresh_holdout_manifest_is_bound_and_reviewer_rows_are_opaque",
    ): (HOLDOUT_N50,),
    (
        "test_divisare_vision_axes_review_inputs.py",
        "test_n10_stages_exact_prefix_in_blinded_order_with_benchmark_bytes",
    ): (AXES_DEV_N50,),
    (
        "test_divisare_vision_axes_review_inputs.py",
        "test_rejects_tampered_frozen_manifest_before_fetch",
    ): (AXES_DEV_N50,),
    (
        "test_divisare_vision_axes_review_inputs.py",
        "test_content_mismatch_leaves_no_final_or_partial_directory",
    ): (AXES_DEV_N50,),
    (
        "test_divisare_vision_axes_review_inputs.py",
        "test_rejects_redirect_off_divisare_and_cleans_staging",
    ): (AXES_DEV_N50,),
    (
        "test_divisare_vision_axes_review_inputs.py",
        "test_no_clobber_preserves_existing_directory_without_fetch",
    ): (AXES_DEV_N50,),
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip only production-bound tests whose exact ignored inputs are absent."""

    for item in items:
        test_name = getattr(item, "originalname", None) or item.name
        required = _REQUIREMENTS.get((Path(str(item.fspath)).name, test_name))
        if required is None:
            continue
        missing = tuple(path for path in required if not path.is_file())
        if missing:
            relative = ", ".join(str(path.relative_to(ROOT)) for path in missing)
            item.add_marker(
                pytest.mark.skip(
                    reason=f"requires exact gitignored production artifact(s): {relative}"
                )
            )
