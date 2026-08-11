from __future__ import annotations

import math

import pytest

from canonical.cross_source_image_evidence import (
    PHASH_BAND_BIT_POSITIONS,
    PHASH_BAND_COUNT,
    block_tokens,
    canonical_json,
    canonical_sha256,
    classify_phash_pair,
    deterministic_sample_ids,
    deterministic_sample_score,
    normalize_block_text,
    ordered_manifest_sha256,
    phash_band_keys,
    phash_band_key,
    stable_edge_id,
    stable_exact_id,
    stable_node_id,
    stable_phash_id,
    validate_phash256,
)


ZERO = "0" * 64


def _with_low_bits(count: int) -> str:
    return ((1 << count) - 1).to_bytes(32, "big").hex()


def _flip_bit_positions(phash: str, positions: tuple[int, ...]) -> str:
    bits = list(f"{int(phash, 16):0256b}")
    for position in positions:
        bits[position] = "1" if bits[position] == "0" else "0"
    return f"{int(''.join(bits), 2):064x}"


def test_canonical_json_and_sha_are_stable_and_strict() -> None:
    left = {"z": ["서울", 2], "a": {"b": True}}
    right = {"a": {"b": True}, "z": ["서울", 2]}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_json(left) == '{"a":{"b":true},"z":["서울",2]}'
    assert canonical_sha256(left) == canonical_sha256(right)
    with pytest.raises(ValueError):
        canonical_json({"bad": math.nan})


@pytest.mark.parametrize("distance", [0, 8])
def test_phash_distance_zero_through_eight_is_strong(distance: int) -> None:
    decision = classify_phash_pair(ZERO, _with_low_bits(distance))
    assert decision.distance == distance
    assert decision.classification == "strong"
    assert decision.reason_code == "phash_distance_0_8"
    assert decision.is_evidence_edge


@pytest.mark.parametrize("distance", [9, 16])
def test_phash_distance_nine_through_sixteen_requires_metadata_block(
    distance: int,
) -> None:
    candidate = _with_low_bits(distance)
    rejected = classify_phash_pair(ZERO, candidate)
    assert (rejected.distance, rejected.classification) == (distance, "rejected")
    assert rejected.reason_code == "phash_distance_9_16_requires_metadata_block"
    assert not rejected.is_evidence_edge

    review = classify_phash_pair(ZERO, candidate, metadata_blocked=True)
    assert (review.distance, review.classification) == (distance, "review")
    assert review.reason_code == "phash_distance_9_16_metadata_blocked"
    assert review.is_evidence_edge


def test_phash_distance_above_sixteen_is_always_rejected() -> None:
    candidate = _with_low_bits(17)
    for metadata_blocked in (False, True):
        decision = classify_phash_pair(
            ZERO, candidate, metadata_blocked=metadata_blocked
        )
        assert decision.distance == 17
        assert decision.classification == "rejected"
        assert decision.reason_code == "phash_distance_above_16"


def test_nine_interleaved_bands_are_disjoint_and_cover_all_bits() -> None:
    assert len(PHASH_BAND_BIT_POSITIONS) == PHASH_BAND_COUNT == 9
    flattened = [position for band in PHASH_BAND_BIT_POSITIONS for position in band]
    assert sorted(flattened) == list(range(256))
    assert len(flattened) == len(set(flattened)) == 256
    assert sorted(map(len, PHASH_BAND_BIT_POSITIONS)) == [28] * 5 + [29] * 4
    assert phash_band_keys(ZERO) == tuple(
        phash_band_key(ZERO, index) for index in range(9)
    )
    with pytest.raises(ValueError, match="band_index"):
        phash_band_key(ZERO, 9)


def test_nine_band_pigeonhole_recall_for_up_to_eight_changed_bits() -> None:
    base_keys = phash_band_keys(ZERO)
    for changed_count in range(9):
        # Touch a different band with every changed bit, the hardest case for
        # preserving a common band at this distance.
        positions = tuple(
            PHASH_BAND_BIT_POSITIONS[index][0] for index in range(changed_count)
        )
        changed_keys = phash_band_keys(_flip_bit_positions(ZERO, positions))
        assert any(left == right for left, right in zip(base_keys, changed_keys))

    all_bands_touched = tuple(band[0] for band in PHASH_BAND_BIT_POSITIONS)
    changed_keys = phash_band_keys(_flip_bit_positions(ZERO, all_bands_touched))
    assert all(left != right for left, right in zip(base_keys, changed_keys))


def test_direct_edges_do_not_infer_transitive_identity() -> None:
    a = ZERO
    b = _with_low_bits(8)
    c = _with_low_bits(16)
    assert classify_phash_pair(a, b).classification == "strong"
    assert classify_phash_pair(b, c).classification == "strong"
    # A--B--C is not promoted to a strong A--C edge through graph reachability.
    direct_ac = classify_phash_pair(a, c)
    assert direct_ac.distance == 16
    assert direct_ac.classification == "rejected"


def test_stable_ids_are_deterministic_domain_separated_and_symmetric() -> None:
    div = stable_node_id("divisare", "asset-42")
    arch = stable_node_id("architizer", "asset-42")
    assert div == stable_node_id("divisare", "asset-42")
    assert div != arch

    pixel_sha = "a" * 64
    assert stable_exact_id(pixel_sha) == "e2x_" + pixel_sha
    assert stable_phash_id(pixel_sha) == "e2p_" + pixel_sha
    assert stable_edge_id(div, arch, "phash") == stable_edge_id(
        arch, div, "phash"
    )
    assert stable_edge_id(div, arch, "phash") != stable_edge_id(
        div, arch, "exact_pixel"
    )
    with pytest.raises(ValueError, match="different nodes"):
        stable_edge_id(div, div, "phash")


def test_deterministic_sampling_and_ordered_manifest() -> None:
    identities = ["asset-c", "asset-a", "asset-b", "asset-a"]
    forward = deterministic_sample_ids(identities, seed="fixed-seed", limit=3)
    reverse = deterministic_sample_ids(
        reversed(identities), seed="fixed-seed", limit=3
    )
    assert forward == reverse
    assert len(forward) == len(set(forward)) == 3
    assert list(forward) == sorted(
        forward,
        key=lambda item: (deterministic_sample_score("fixed-seed", item), item),
    )
    assert ordered_manifest_sha256(forward) == ordered_manifest_sha256(reverse)
    assert ordered_manifest_sha256(forward) != ordered_manifest_sha256(
        reversed(forward)
    )


@pytest.mark.parametrize(
    "bad",
    ["", "0" * 63, "0" * 65, "G" * 64, "A" * 64, 0, None],
)
def test_invalid_phash_is_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="pHash"):
        validate_phash256(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="pHash"):
        phash_band_keys(bad)  # type: ignore[arg-type]


def test_invalid_classification_and_sampling_inputs_are_rejected() -> None:
    with pytest.raises(TypeError, match="metadata_blocked"):
        classify_phash_pair(ZERO, ZERO, metadata_blocked=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        deterministic_sample_ids(["a"], seed="seed", limit=-1)
    with pytest.raises(ValueError, match="outer whitespace"):
        stable_node_id(" divisare", "asset")


def test_conservative_text_normalization_retains_unicode_distinctions() -> None:
    assert normalize_block_text("  270—Park_Avenue  ") == "270 park avenue"
    assert normalize_block_text("École / 서울") == "école 서울"
    assert normalize_block_text(None) == ""
    assert block_tokens("The 270 Park Park Avenue", min_length=3) == (
        "270",
        "avenue",
        "park",
        "the",
    )
    with pytest.raises(TypeError):
        normalize_block_text(270)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_length"):
        block_tokens("project", min_length=0)
