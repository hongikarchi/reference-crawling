"""Pure, deterministic helpers for cross-source E2 image evidence.

This module deliberately stops at evidence.  It does not cluster buildings,
choose representative images, create a Vision queue, or infer identity through
transitive graph paths.  Callers must persist and evaluate every direct edge on
its own evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from canonical.image_fingerprint import phash_distance


E2_EVIDENCE_VERSION = "archibe-e2-cross-source-image-evidence-v1"
E2_SCHEMA_VERSION = "archibe-e2-cross-source-image-evidence-schema-v1"
PHASH_BAND_VERSION = "archibe-e2-phash-9-interleaved-v1"
PHASH_PAIR_POLICY_VERSION = "archibe-e2-phash-pair-policy-v1"
METADATA_NORMALIZATION_VERSION = "archibe-e2-metadata-normalization-v1"
SAMPLE_POLICY_VERSION = "archibe-e2-deterministic-sample-v2"

PHASH_BIT_COUNT = 256
PHASH_BAND_COUNT = 9
PHASH_STRONG_MAX_DISTANCE = 8
PHASH_REVIEW_MAX_DISTANCE = 16

_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")

# Interleaving distributes adjacent pHash bits across bands.  The bands are
# disjoint and cover all 256 positions, so at most eight changed bits cannot
# touch all nine bands (pigeonhole principle).
PHASH_BAND_BIT_POSITIONS: tuple[tuple[int, ...], ...] = tuple(
    tuple(range(band_index, PHASH_BIT_COUNT, PHASH_BAND_COUNT))
    for band_index in range(PHASH_BAND_COUNT)
)


@dataclass(frozen=True)
class PHashPairDecision:
    """The policy result for one directly compared pHash pair."""

    distance: int
    classification: str
    reason_code: str
    metadata_blocked: bool

    @property
    def is_evidence_edge(self) -> bool:
        """Whether the direct pair is retained as strong or review evidence."""

        return self.classification in {"strong", "review"}


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically for IDs, manifests, and checksums."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 of :func:`canonical_json` encoded as UTF-8."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_sha256(value: str, *, label: str = "SHA-256") -> str:
    """Validate and return one lowercase 256-bit hexadecimal digest."""

    if not isinstance(value, str) or _LOWER_HEX_64.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase 64-character hexadecimal value")
    return value


def validate_phash256(value: str) -> str:
    """Validate and return a lowercase 256-bit pHash."""

    return validate_sha256(value, label="pHash")


def _require_identity_part(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty and have no outer whitespace")
    return value


def _domain_id(prefix: str, domain: str, parts: Iterable[str]) -> str:
    payload = {
        "domain": domain,
        "parts": list(parts),
        "version": E2_EVIDENCE_VERSION,
    }
    return prefix + canonical_sha256(payload)


def stable_node_id(source: str, source_asset_id: str) -> str:
    """Return a stable ID for one source-qualified asset record."""

    source = _require_identity_part(source, label="source")
    source_asset_id = _require_identity_part(
        source_asset_id, label="source_asset_id"
    )
    return _domain_id("e2n_", "asset-node", (source, source_asset_id))


def stable_exact_id(normalized_pixel_sha256: str) -> str:
    """Return a collision-free ID for one exact normalized-pixel value."""

    digest = validate_sha256(
        normalized_pixel_sha256, label="normalized pixel SHA-256"
    )
    return "e2x_" + digest


def stable_phash_id(phash256: str) -> str:
    """Return a collision-free ID for one distinct 256-bit pHash value."""

    return "e2p_" + validate_phash256(phash256)


def stable_edge_id(left_node_id: str, right_node_id: str, evidence_kind: str) -> str:
    """Return an order-independent ID for one direct evidence edge."""

    left_node_id = _require_identity_part(left_node_id, label="left_node_id")
    right_node_id = _require_identity_part(right_node_id, label="right_node_id")
    evidence_kind = _require_identity_part(evidence_kind, label="evidence_kind")
    if left_node_id == right_node_id:
        raise ValueError("an evidence edge requires two different nodes")
    left_node_id, right_node_id = sorted((left_node_id, right_node_id))
    return _domain_id(
        "e2e_", "direct-evidence-edge", (evidence_kind, left_node_id, right_node_id)
    )


def _phash_bits(phash256: str) -> str:
    return f"{int(validate_phash256(phash256), 16):0256b}"


def phash_band_keys(phash256: str) -> tuple[str, ...]:
    """Return nine lossless, disjoint interleaved band lookup keys.

    Equal keys are only candidate-generation evidence.  A caller must always
    recompute the full 256-bit Hamming distance before retaining an edge.
    """

    return tuple(
        phash_band_key(phash256, band_index)
        for band_index in range(PHASH_BAND_COUNT)
    )


def phash_band_key(phash256: str, band_index: int) -> str:
    """Return one deterministic interleaved band key.

    This single-band form lets a bounded-memory SQLite builder scan one band at
    a time without calculating the other eight keys on every pass.
    """

    if isinstance(band_index, bool) or not isinstance(band_index, int):
        raise TypeError("band_index must be an integer")
    if not 0 <= band_index < PHASH_BAND_COUNT:
        raise ValueError(f"band_index must be between 0 and {PHASH_BAND_COUNT - 1}")
    bits = _phash_bits(phash256)
    positions = PHASH_BAND_BIT_POSITIONS[band_index]
    band_bits = "".join(bits[position] for position in positions)
    width = len(positions)
    hex_width = (width + 3) // 4
    encoded = f"{int(band_bits, 2):0{hex_width}x}"
    return f"{band_index}:{width}:{encoded}"


def classify_phash_pair(
    left_phash256: str,
    right_phash256: str,
    *,
    metadata_blocked: bool = False,
) -> PHashPairDecision:
    """Classify one direct pair without making a building-identity claim."""

    if not isinstance(metadata_blocked, bool):
        raise TypeError("metadata_blocked must be a bool")
    left = validate_phash256(left_phash256)
    right = validate_phash256(right_phash256)
    distance = phash_distance(left, right)
    if distance <= PHASH_STRONG_MAX_DISTANCE:
        return PHashPairDecision(
            distance=distance,
            classification="strong",
            reason_code="phash_distance_0_8",
            metadata_blocked=metadata_blocked,
        )
    if distance <= PHASH_REVIEW_MAX_DISTANCE and metadata_blocked:
        return PHashPairDecision(
            distance=distance,
            classification="review",
            reason_code="phash_distance_9_16_metadata_blocked",
            metadata_blocked=True,
        )
    if distance <= PHASH_REVIEW_MAX_DISTANCE:
        reason = "phash_distance_9_16_requires_metadata_block"
    else:
        reason = "phash_distance_above_16"
    return PHashPairDecision(
        distance=distance,
        classification="rejected",
        reason_code=reason,
        metadata_blocked=metadata_blocked,
    )


def deterministic_sample_score(seed: str, identity: str) -> str:
    """Return a stable hexadecimal sample score for a unique identity."""

    seed = _require_identity_part(seed, label="seed")
    identity = _require_identity_part(identity, label="identity")
    return canonical_sha256(
        {
            "identity": identity,
            "policy_version": SAMPLE_POLICY_VERSION,
            "seed": seed,
        }
    )


def deterministic_sample_ids(
    identities: Iterable[str], *, seed: str, limit: int
) -> tuple[str, ...]:
    """Select unique IDs by stable hash order, using identity as the tie-breaker."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    seed = _require_identity_part(seed, label="seed")
    unique: set[str] = set()
    for identity in identities:
        unique.add(_require_identity_part(identity, label="identity"))
    ordered = sorted(
        unique,
        key=lambda identity: (deterministic_sample_score(seed, identity), identity),
    )
    return tuple(ordered[:limit])


def ordered_manifest_sha256(ordered_identities: Iterable[str]) -> str:
    """Hash a caller-defined order without silently sorting or deduplicating it."""

    values = [
        _require_identity_part(identity, label="identity")
        for identity in ordered_identities
    ]
    return canonical_sha256(
        {
            "ordered_identities": values,
            "policy_version": SAMPLE_POLICY_VERSION,
        }
    )


def normalize_block_text(value: str | None) -> str:
    """Conservatively normalize text while retaining Unicode distinctions.

    The function performs no translation, stop-word removal, stemming, fuzzy
    matching, or source-specific vocabulary mapping.  It is suitable for
    constructing candidate blocks, not for asserting entity identity.
    """

    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("text value must be a string or None")
    text = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        characters.append(character if category[0] in {"L", "N", "M"} else " ")
    return " ".join("".join(characters).split())


def block_tokens(value: str | None, *, min_length: int = 2) -> tuple[str, ...]:
    """Return sorted unique tokens from :func:`normalize_block_text`."""

    if isinstance(min_length, bool) or not isinstance(min_length, int) or min_length < 1:
        raise ValueError("min_length must be a positive integer")
    normalized = normalize_block_text(value)
    return tuple(
        sorted({token for token in normalized.split() if len(token) >= min_length})
    )


__all__ = [
    "E2_EVIDENCE_VERSION",
    "E2_SCHEMA_VERSION",
    "METADATA_NORMALIZATION_VERSION",
    "PHASH_BAND_BIT_POSITIONS",
    "PHASH_BAND_COUNT",
    "PHASH_BAND_VERSION",
    "PHASH_PAIR_POLICY_VERSION",
    "PHASH_REVIEW_MAX_DISTANCE",
    "PHASH_STRONG_MAX_DISTANCE",
    "PHashPairDecision",
    "SAMPLE_POLICY_VERSION",
    "block_tokens",
    "canonical_json",
    "canonical_sha256",
    "classify_phash_pair",
    "deterministic_sample_ids",
    "deterministic_sample_score",
    "normalize_block_text",
    "ordered_manifest_sha256",
    "phash_band_keys",
    "phash_band_key",
    "stable_edge_id",
    "stable_exact_id",
    "stable_node_id",
    "stable_phash_id",
    "validate_phash256",
    "validate_sha256",
]
