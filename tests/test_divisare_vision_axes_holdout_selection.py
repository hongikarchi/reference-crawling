from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from canonical import divisare_vision_axes_holdout_selection as selection


ROOT = Path(__file__).resolve().parents[1]
PROBED = ROOT / "data" / "review" / "divisare_vision_axes_holdout_candidates_n100_v1_probed.json"
CANDIDATES = ROOT / "data" / "review" / "divisare_vision_axes_holdout_candidates_n100_v1.json"
PRIOR = ROOT / "data" / "review" / "divisare_vision_gold_candidates_v1_2_probed.json"


def _payload() -> tuple[dict[str, object], dict[str, object]]:
    probed, probed_sha, candidate_sha, prior_sha = selection.load_selection_inputs(
        probed_path=PROBED,
        candidate_path=CANDIDATES,
        prior_path=PRIOR,
    )
    payload = selection.build_selection_payload(
        probed,
        probed_file_sha256=probed_sha,
        candidate_file_sha256=candidate_sha,
        prior_file_sha256=prior_sha,
    )
    return probed, payload


def test_fresh_n50_is_balanced_blinded_and_deterministic() -> None:
    probed, payload = _payload()
    selection.validate_selection_manifest(payload, parent_probed=probed)
    assert payload == selection.build_selection_payload(
        probed,
        probed_file_sha256=selection.EXPECTED_PROBED_FILE_SHA256,
        candidate_file_sha256=selection.EXPECTED_CANDIDATE_FILE_SHA256,
        prior_file_sha256=selection.EXPECTED_PRIOR_FILE_SHA256,
    )
    assert payload["selection_metrics"]["proxy_counts"] == selection.EXPECTED_PROXY_COUNTS
    assert payload["selection_metrics"]["generation_counts"] == {"legacy": 15, "modern": 35}
    assert payload["selection_metrics"]["role_counts"] == {"cover": 22, "gallery": 28}
    assert all(set(row) == {"review_rank", "review_id"} for row in payload["review_rows"])
    public_text = json.dumps(payload["review_rows"], sort_keys=True)
    for forbidden in ("proxy_class", "request_url", "asset_key", "weak_hints"):
        assert forbidden not in public_text


def test_fresh_n50_rejects_tampering_and_quota_shortfall() -> None:
    probed, payload = _payload()
    changed = copy.deepcopy(payload)
    changed["audit_samples"][0]["selection_audit"]["role"] = "gallery"
    changed["logical_sha256"] = selection.logical_sha256(changed)
    changed["manifest_sha256"] = selection.manifest_sha256(changed)
    with pytest.raises(ValueError, match="sample cell"):
        selection.validate_selection_manifest(changed, parent_probed=probed)

    changed = copy.deepcopy(payload)
    changed["provenance"]["parent_probed_n100"]["file_sha256"] = "0" * 64
    changed["logical_sha256"] = selection.logical_sha256(changed)
    changed["manifest_sha256"] = selection.manifest_sha256(changed)
    with pytest.raises(ValueError, match="parent probed file SHA"):
        selection.validate_selection_manifest(changed)

    changed = copy.deepcopy(payload)
    changed["audit_samples"][0]["source_identity"]["asset_key"] = "fabricated"
    changed["logical_sha256"] = selection.logical_sha256(changed)
    changed["manifest_sha256"] = selection.manifest_sha256(changed)
    with pytest.raises(ValueError, match="frozen selected audit SHA"):
        selection.validate_selection_manifest(changed)

    changed = copy.deepcopy(payload)
    changed["audit_samples"][0]["selection_audit"]["extra"] = True
    changed["logical_sha256"] = selection.logical_sha256(changed)
    changed["manifest_sha256"] = selection.manifest_sha256(changed)
    with pytest.raises(ValueError, match="selection audit fields"):
        selection.validate_selection_manifest(changed)

    broken_parent = copy.deepcopy(probed)
    for row in broken_parent["candidates"]:
        if (
            row["proxy_class"],
            row["generation_group"],
            row["role"],
        ) == ("drawing", "legacy", "cover"):
            row["probe_status"] = "failed"
    with pytest.raises(ValueError, match="quota shortfall"):
        selection._select_candidates(broken_parent)


def test_writer_is_no_clobber(tmp_path: Path) -> None:
    output = tmp_path / "selection.json"
    first = selection.write_selection_manifest(
        probed_path=PROBED,
        candidate_path=CANDIDATES,
        prior_path=PRIOR,
        output_path=output,
    )
    assert json.loads(output.read_text(encoding="utf-8")) == first
    with pytest.raises(FileExistsError, match="already exists"):
        selection.write_selection_manifest(
            probed_path=PROBED,
            candidate_path=CANDIDATES,
            prior_path=PRIOR,
            output_path=output,
        )
