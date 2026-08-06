from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from canonical.divisare_image_smoke import FetchFailure, FetchPayload
from canonical import divisare_vision_axes_holdout as holdout
from canonical import divisare_vision_axes_holdout_probe as probe
from canonical.divisare_vision_probe import ProbeConfig


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = (
    ROOT / "data" / "review" / "divisare_vision_axes_holdout_candidates_n100_v1.json"
)
PRIOR = (
    ROOT / "data" / "review" / "divisare_vision_gold_candidates_v1_2_probed.json"
)


def _row(
    candidate_id: str,
    rank: int,
    *,
    content: str,
    pixel: str,
    phash: str,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate_rank": rank,
        "content_sha256": content,
        "pixel_sha256": pixel,
        "phash_256": phash,
    }


def _jpeg_for_url(url: str) -> bytes:
    digest = hashlib.sha256(url.encode("utf-8")).digest()
    image = Image.new("RGB", (96, 72), tuple(digest[:3]))
    draw = ImageDraw.Draw(image)
    for index in range(4):
        offset = index * 4
        x0 = digest[offset] % 70
        y0 = digest[offset + 1] % 50
        x1 = min(95, x0 + 8 + digest[offset + 2] % 24)
        y1 = min(71, y0 + 8 + digest[offset + 3] % 20)
        color = tuple(digest[(offset + 4 + value) % len(digest)] for value in range(3))
        draw.rectangle((x0, y0, x1, y1), fill=color)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=93)
    return output.getvalue()


def _fetch(url: str, **_kwargs: object) -> FetchPayload:
    return FetchPayload(_jpeg_for_url(url), 200, "image/jpeg", url)


def test_real_holdout_and_prior_inputs_are_cross_bound() -> None:
    (
        candidate,
        rows,
        candidate_file_sha,
        prior,
        prior_rows,
        prior_file_sha,
        exclusion,
    ) = probe.load_probe_inputs(CANDIDATES, PRIOR)
    assert len(rows) == 100
    assert len(prior_rows) == 560
    assert candidate["provenance"]["exclusion_manifest_file_sha256"] == prior_file_sha
    assert candidate["provenance"]["exclusion_manifest_sha256"] == prior["manifest_sha256"]
    assert candidate_file_sha == hashlib.sha256(CANDIDATES.read_bytes()).hexdigest()
    assert exclusion.asset_keys.isdisjoint({row["asset_key"] for row in rows})


def test_prior_probe_contract_must_match_current_identity_runtime() -> None:
    prior = json.loads(PRIOR.read_text(encoding="utf-8"))
    changed = copy.deepcopy(prior)
    changed["probe_contract"]["normalized_long_edge"] = 256
    with pytest.raises(ValueError, match="normalized_long_edge"):
        probe._validate_prior_probe_contract(changed)


def test_cross_duplicate_evidence_separates_exact_and_phash_bands() -> None:
    zero = "0" * 64
    distance_four = "%064x" % 0xF
    distance_twelve = "%064x" % 0xFFF
    distance_seventeen = "%064x" % 0x1FFFF
    fresh = [_row("fresh", 1, content="a" * 64, pixel="b" * 64, phash=zero)]
    prior = [
        _row("exact", 1, content="a" * 64, pixel="b" * 64, phash=distance_four),
        _row("audit", 2, content="c" * 64, pixel="d" * 64, phash=distance_twelve),
        _row("far", 3, content="e" * 64, pixel="f" * 64, phash=distance_seventeen),
    ]
    content, pixel, le8, audit = probe.build_cross_duplicate_evidence(fresh, prior)
    assert [row["prior_candidate_id"] for row in content] == ["exact"]
    assert [row["prior_candidate_id"] for row in pixel] == ["exact"]
    assert [(row["prior_candidate_id"], row["phash_distance"]) for row in le8] == [
        ("exact", 4)
    ]
    assert [(row["prior_candidate_id"], row["phash_distance"]) for row in audit] == [
        ("audit", 12)
    ]


def test_probe_resume_publishes_hash_only_manifest_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    output = tmp_path / "holdout-probed.json"
    staging = tmp_path / "holdout-probed.staging.sqlite"
    config = ProbeConfig(workers=2, max_attempts=1)
    partial = probe.run_holdout_probe(
        candidate_manifest_path=CANDIDATES,
        prior_probed_manifest_path=PRIOR,
        output_path=output,
        staging_path=staging,
        config=config,
        stop_after=3,
        fetcher=_fetch,
        sleep=lambda _seconds: None,
    )
    assert partial["status"] == "running"
    assert partial["pending_count"] == 97
    assert staging.exists() and not output.exists()

    result = probe.run_holdout_probe(
        candidate_manifest_path=CANDIDATES,
        prior_probed_manifest_path=PRIOR,
        output_path=output,
        staging_path=staging,
        config=config,
        resume=True,
        fetcher=_fetch,
        sleep=lambda _seconds: None,
    )
    assert result["candidate_count"] == 100
    assert result["success_count"] == 100
    assert result["failure_count"] == 0
    assert output.exists() and not staging.exists()

    payload = json.loads(output.read_text(encoding="utf-8"))
    candidate, _rows, candidate_file_sha, prior, _prior_rows, prior_file_sha, exclusion = (
        probe.load_probe_inputs(CANDIDATES, PRIOR)
    )
    probe.validate_probed_holdout_manifest(
        payload,
        input_manifest=candidate,
        input_manifest_file_sha256=candidate_file_sha,
        prior_manifest=prior,
        prior_manifest_file_sha256=prior_file_sha,
        exclusion=exclusion,
    )
    assert payload["probe_contract"]["images_persisted"] is False
    assert all("image_bytes" not in row for row in payload["candidates"])

    changed = copy.deepcopy(payload)
    changed["holdout_cross_duplicate_contract"]["cross_pixel_match_count"] += 1
    changed["manifest_sha256"] = holdout.manifest_sha256(changed)
    with pytest.raises(ValueError, match="cross-duplicate evidence"):
        probe.validate_probed_holdout_manifest(
            changed,
            input_manifest=candidate,
            input_manifest_file_sha256=candidate_file_sha,
            prior_manifest=prior,
            prior_manifest_file_sha256=prior_file_sha,
            exclusion=exclusion,
        )

    with pytest.raises(FileExistsError, match="already exists"):
        probe.run_holdout_probe(
            candidate_manifest_path=CANDIDATES,
            prior_probed_manifest_path=PRIOR,
            output_path=output,
            staging_path=staging,
            config=config,
            fetcher=_fetch,
        )


def test_probe_preserves_fetch_failure_and_excludes_it_from_cross_matching(
    tmp_path: Path,
) -> None:
    output = tmp_path / "holdout-probed-with-failure.json"
    staging = tmp_path / "holdout-probed-with-failure.staging.sqlite"
    manifest = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    failed_id = manifest["candidates"][0]["candidate_id"]
    failed_url = manifest["candidates"][0]["request_url"]

    def fetch_with_failure(url: str, **kwargs: object) -> FetchPayload:
        if url == failed_url:
            raise FetchFailure(
                "http_404", "HTTP 404", http_status=404, retryable=False
            )
        return _fetch(url, **kwargs)

    result = probe.run_holdout_probe(
        candidate_manifest_path=CANDIDATES,
        prior_probed_manifest_path=PRIOR,
        output_path=output,
        staging_path=staging,
        config=ProbeConfig(workers=2, max_attempts=1),
        fetcher=fetch_with_failure,
        sleep=lambda _seconds: None,
    )
    assert result["success_count"] == 99
    assert result["failure_count"] == 1
    assert result["errors_by_kind"] == {"http_404": 1}

    payload = json.loads(output.read_text(encoding="utf-8"))
    failed = next(
        row for row in payload["candidates"] if row["candidate_id"] == failed_id
    )
    assert failed["probe_status"] == "failed"
    assert failed["probe_error_kind"] == "http_404"
    assert failed["prior_content_sha_matches"] == []
    assert failed["prior_pixel_sha_matches"] == []
    assert failed["prior_phash_le8_matches"] == []
    assert failed["prior_phash_9_16_matches"] == []
