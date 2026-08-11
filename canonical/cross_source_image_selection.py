"""Pure, offline helpers for E3 image-shortlist policy experiments.

E3 compares deterministic shortlist policies over the immutable E2 evidence
artifact.  It deliberately does **not** choose a final representative image,
create a Vision task, infer image meaning from pHash, or close a pHash graph
transitively.  Persistence layers should store the returned component scores,
reason codes, record hashes, and ordered manifests without adding an implicit
winner.

The three frozen policies are:

``P0``
    Editorial baseline: successful assets ordered by source role, ordinal,
    dimensions, and stable asset identity.
``P1``
    P0 plus a hard-risk quality gate.  Risky assets are used only as an
    explicit QA fallback when *all* successful assets for the building are
    risky.
``P2``
    P1 plus greedy chosen-star redundancy suppression.  A later candidate is
    suppressed only when it has exact-pixel, identical-pHash-node, or a direct
    pHash-distance <= 8 evidence relation to an already selected candidate.
    A relation through a suppressed node is never followed.

pHash suppression is only a shortlist-budget operation.  It never authorizes
semantic-result reuse or a building identity decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


E3_SELECTION_VERSION = "archibe-e3-cross-source-image-selection-v1"
E3_POLICY_VERSION = "archibe-e3-shortlist-policy-v1"
E3_SAMPLE_POLICY_VERSION = "archibe-e3-stratified-sample-v1"

DEFAULT_SHORTLIST_SIZE = 3
HARD_RISK_MIN_SHORT_EDGE = 256

P0_POLICY_ID = "p0_editorial_baseline"
P1_POLICY_ID = "p1_quality_gated_editorial"
P2_POLICY_ID = "p2_quality_exact_direct_phash_shortlist"

_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_SUCCESS_STATUS = "success"
_KNOWN_FINGERPRINT_STATUSES = frozenset(
    {"pending", "success", "failed", "skipped", "excluded"}
)
_LOW_INFORMATION_FLAG = "low_information"


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically for IDs, records, and manifests."""

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
    """Validate and return one lowercase 64-character hexadecimal digest."""

    if not isinstance(value, str) or _LOWER_HEX_64.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase 64-character hexadecimal value")
    return value


def _identity_part(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty and have no outer whitespace")
    return value


def _optional_identity_part(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    return _identity_part(value, label=label)


def _nonnegative_optional_int(value: int | None, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer or None")
    return value


def _positive_optional_int(value: int | None, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer or None")
    return value


def stable_candidate_id(
    source: str, source_building_id: str, source_asset_id: str
) -> str:
    """Return the stable building-qualified ID of one shortlist candidate."""

    payload = {
        "domain": "building-image-candidate",
        "source": _identity_part(source, label="source"),
        "source_asset_id": _identity_part(
            source_asset_id, label="source_asset_id"
        ),
        "source_building_id": _identity_part(
            source_building_id, label="source_building_id"
        ),
        "version": E3_SELECTION_VERSION,
    }
    return "e3c_" + canonical_sha256(payload)


def stable_shortlist_id(
    source: str,
    source_building_id: str,
    policy_config_sha256: str,
) -> str:
    """Return a stable policy/building shortlist identity, not a final winner."""

    payload = {
        "domain": "policy-building-shortlist",
        "policy_config_sha256": validate_sha256(
            policy_config_sha256, label="policy config SHA-256"
        ),
        "source": _identity_part(source, label="source"),
        "source_building_id": _identity_part(
            source_building_id, label="source_building_id"
        ),
        "version": E3_SELECTION_VERSION,
    }
    return "e3s_" + canonical_sha256(payload)


@dataclass(frozen=True)
class Candidate:
    """One building-qualified E2 asset and its offline ranking features.

    ``exact_cluster_id`` and ``phash_node_id`` are evidence identifiers.  They
    may reduce redundant shortlist slots under P2, but never carry semantic
    labels.  ``source_record_sha256`` binds the source/E1 record from which the
    candidate features were mapped.
    """

    source: str
    source_building_id: str
    source_asset_id: str
    fingerprint_status: str
    role: str
    ordinal: int | None
    original_width: int | None
    original_height: int | None
    quality_flags: tuple[str, ...]
    source_record_sha256: str
    exact_cluster_id: str | None = None
    phash_node_id: str | None = None
    canonical_url: str | None = None

    def __post_init__(self) -> None:
        source = _identity_part(self.source, label="source").casefold()
        building = _identity_part(
            self.source_building_id, label="source_building_id"
        )
        asset = _identity_part(self.source_asset_id, label="source_asset_id")
        status = _identity_part(
            self.fingerprint_status, label="fingerprint_status"
        ).casefold()
        if status not in _KNOWN_FINGERPRINT_STATUSES:
            raise ValueError(f"unsupported fingerprint_status: {status}")
        role = _identity_part(self.role, label="role").casefold()
        ordinal = _nonnegative_optional_int(self.ordinal, label="ordinal")
        width = _positive_optional_int(self.original_width, label="original_width")
        height = _positive_optional_int(
            self.original_height, label="original_height"
        )
        if not isinstance(self.quality_flags, tuple):
            raise TypeError("quality_flags must be a tuple")
        flags = tuple(
            sorted(
                {
                    _identity_part(flag, label="quality flag").casefold()
                    for flag in self.quality_flags
                }
            )
        )
        source_record_sha = validate_sha256(
            self.source_record_sha256, label="source record SHA-256"
        )
        exact = _optional_identity_part(
            self.exact_cluster_id, label="exact_cluster_id"
        )
        phash = _optional_identity_part(self.phash_node_id, label="phash_node_id")
        url = _optional_identity_part(self.canonical_url, label="canonical_url")

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_building_id", building)
        object.__setattr__(self, "source_asset_id", asset)
        object.__setattr__(self, "fingerprint_status", status)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "ordinal", ordinal)
        object.__setattr__(self, "original_width", width)
        object.__setattr__(self, "original_height", height)
        object.__setattr__(self, "quality_flags", flags)
        object.__setattr__(self, "source_record_sha256", source_record_sha)
        object.__setattr__(self, "exact_cluster_id", exact)
        object.__setattr__(self, "phash_node_id", phash)
        object.__setattr__(self, "canonical_url", url)

    @property
    def candidate_id(self) -> str:
        return stable_candidate_id(
            self.source, self.source_building_id, self.source_asset_id
        )

    @property
    def is_success(self) -> bool:
        return self.fingerprint_status == _SUCCESS_STATUS

    @property
    def pixel_area(self) -> int:
        return (self.original_width or 0) * (self.original_height or 0)

    @property
    def short_edge(self) -> int:
        if self.original_width is None or self.original_height is None:
            return 0
        return min(self.original_width, self.original_height)

    @property
    def long_edge(self) -> int:
        return max(self.original_width or 0, self.original_height or 0)

    @property
    def hard_risk_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if _LOW_INFORMATION_FLAG in self.quality_flags:
            reasons.append("low_information")
        if (
            self.original_width is not None
            and self.original_height is not None
            and min(self.original_width, self.original_height)
            < HARD_RISK_MIN_SHORT_EDGE
        ):
            reasons.append("short_edge_below_256")
        return tuple(reasons)

    @property
    def is_hard_risk(self) -> bool:
        return bool(self.hard_risk_reasons)

    def as_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "canonical_url": self.canonical_url,
            "exact_cluster_id": self.exact_cluster_id,
            "fingerprint_status": self.fingerprint_status,
            "hard_risk_reasons": list(self.hard_risk_reasons),
            "ordinal": self.ordinal,
            "original_height": self.original_height,
            "original_width": self.original_width,
            "phash_node_id": self.phash_node_id,
            "quality_flags": list(self.quality_flags),
            "role": self.role,
            "source": self.source,
            "source_asset_id": self.source_asset_id,
            "source_building_id": self.source_building_id,
            "source_record_sha256": self.source_record_sha256,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())


def candidate_record_sha256(candidate: Candidate) -> str:
    """Return the full feature-record SHA of ``candidate``."""

    if not isinstance(candidate, Candidate):
        raise TypeError("candidate must be a Candidate")
    return candidate.record_sha256


@dataclass(frozen=True)
class DirectPHashEdge:
    """One explicit pHash-distance <= 8 relation between two candidates.

    The edge can suppress a redundant *later* P2 shortlist candidate only.
    It cannot be traversed through another node and cannot reuse a semantic
    result.
    """

    left_candidate_id: str
    right_candidate_id: str
    distance: int

    def __post_init__(self) -> None:
        left = _identity_part(self.left_candidate_id, label="left_candidate_id")
        right = _identity_part(
            self.right_candidate_id, label="right_candidate_id"
        )
        if left == right:
            raise ValueError("a direct pHash edge requires two candidates")
        if (
            isinstance(self.distance, bool)
            or not isinstance(self.distance, int)
            or not 0 <= self.distance <= 8
        ):
            raise ValueError("direct pHash edge distance must be between 0 and 8")
        object.__setattr__(self, "left_candidate_id", left)
        object.__setattr__(self, "right_candidate_id", right)

    @property
    def pair(self) -> tuple[str, str]:
        return tuple(sorted((self.left_candidate_id, self.right_candidate_id)))  # type: ignore[return-value]

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(
            {
                "distance": self.distance,
                "left_candidate_id": self.pair[0],
                "right_candidate_id": self.pair[1],
                "semantic_reuse_allowed": False,
                "transitive_closure_allowed": False,
                "version": E3_POLICY_VERSION,
            }
        )


@dataclass(frozen=True)
class PolicyDefinition:
    """Versioned configuration for one shortlist-only policy."""

    policy_id: str
    description: str
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE
    quality_gate: bool = False
    suppress_exact_pixel: bool = False
    suppress_same_phash_node: bool = False
    suppress_direct_phash_le8: bool = False

    def __post_init__(self) -> None:
        _identity_part(self.policy_id, label="policy_id")
        _identity_part(self.description, label="description")
        if (
            isinstance(self.shortlist_size, bool)
            or not isinstance(self.shortlist_size, int)
            or self.shortlist_size <= 0
        ):
            raise ValueError("shortlist_size must be a positive integer")
        for field in (
            "quality_gate",
            "suppress_exact_pixel",
            "suppress_same_phash_node",
            "suppress_direct_phash_le8",
        ):
            if not isinstance(getattr(self, field), bool):
                raise TypeError(f"{field} must be a bool")

    def as_config(self) -> dict[str, Any]:
        return {
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "description": self.description,
            "output_kind": "policy_shortlist_only",
            "phash_semantic_reuse_allowed": False,
            "phash_transitive_closure_allowed": False,
            "policy_id": self.policy_id,
            "policy_version": E3_POLICY_VERSION,
            "quality_gate": self.quality_gate,
            "shortlist_size": self.shortlist_size,
            "suppress_direct_phash_le8": self.suppress_direct_phash_le8,
            "suppress_exact_pixel": self.suppress_exact_pixel,
            "suppress_same_phash_node": self.suppress_same_phash_node,
        }

    @property
    def config_sha256(self) -> str:
        return canonical_sha256(self.as_config())


def policy_definitions(
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE,
) -> tuple[PolicyDefinition, PolicyDefinition, PolicyDefinition]:
    """Return frozen P0/P1/P2 definitions for one shortlist budget."""

    return (
        PolicyDefinition(
            policy_id=P0_POLICY_ID,
            description="editorial role, ordinal, dimensions, stable asset ID",
            shortlist_size=shortlist_size,
        ),
        PolicyDefinition(
            policy_id=P1_POLICY_ID,
            description="P0 with hard-risk quality gate and all-risk QA fallback",
            shortlist_size=shortlist_size,
            quality_gate=True,
        ),
        PolicyDefinition(
            policy_id=P2_POLICY_ID,
            description=(
                "P1 with exact-pixel, identical-pHash-node, and direct <=8 "
                "chosen-star suppression"
            ),
            shortlist_size=shortlist_size,
            quality_gate=True,
            suppress_exact_pixel=True,
            suppress_same_phash_node=True,
            suppress_direct_phash_le8=True,
        ),
    )


def _role_rank(role: str) -> int:
    if role == "cover":
        return 0
    if role == "gallery":
        return 1
    return 2


def editorial_sort_key(candidate: Candidate) -> tuple[Any, ...]:
    """Return the frozen P0 ordering key; lower sorts first."""

    ordinal_missing = candidate.ordinal is None
    return (
        _role_rank(candidate.role),
        int(ordinal_missing),
        candidate.ordinal if candidate.ordinal is not None else 0,
        -candidate.pixel_area,
        -candidate.short_edge,
        -candidate.long_edge,
        candidate.source_asset_id,
        candidate.candidate_id,
    )


def candidate_component_scores(candidate: Candidate) -> tuple[tuple[str, Any], ...]:
    """Return named raw components persisted with every policy evaluation."""

    return (
        ("fingerprint_success", int(candidate.is_success)),
        ("hard_risk", int(candidate.is_hard_risk)),
        ("role_rank", _role_rank(candidate.role)),
        ("ordinal_missing", int(candidate.ordinal is None)),
        ("ordinal", candidate.ordinal),
        ("pixel_area", candidate.pixel_area),
        ("short_edge", candidate.short_edge),
        ("long_edge", candidate.long_edge),
        ("asset_id_tiebreak", candidate.source_asset_id),
    )


@dataclass(frozen=True)
class CandidateEvaluation:
    """One candidate's auditable outcome under one shortlist policy."""

    policy_id: str
    policy_config_sha256: str
    candidate_id: str
    candidate_record_sha256: str
    editorial_rank: int
    shortlist_rank: int | None
    selected: bool
    qa_fallback: bool
    hard_risk: bool
    component_scores: tuple[tuple[str, Any], ...]
    reasons: tuple[str, ...]
    suppressed_by_candidate_id: str | None = None

    def __post_init__(self) -> None:
        _identity_part(self.policy_id, label="policy_id")
        validate_sha256(self.policy_config_sha256, label="policy config SHA-256")
        _identity_part(self.candidate_id, label="candidate_id")
        validate_sha256(
            self.candidate_record_sha256, label="candidate record SHA-256"
        )
        if self.editorial_rank < 1:
            raise ValueError("editorial_rank must be positive")
        if self.selected != (self.shortlist_rank is not None):
            raise ValueError("selected and shortlist_rank must agree")
        if self.shortlist_rank is not None and self.shortlist_rank < 1:
            raise ValueError("shortlist_rank must be positive")
        if self.qa_fallback and not (self.selected and self.hard_risk):
            raise ValueError("QA fallback must be a selected hard-risk candidate")
        if not self.reasons:
            raise ValueError("candidate evaluation requires reason codes")

    def as_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_record_sha256": self.candidate_record_sha256,
            "component_scores": dict(self.component_scores),
            "editorial_rank": self.editorial_rank,
            "hard_risk": self.hard_risk,
            "policy_config_sha256": self.policy_config_sha256,
            "policy_id": self.policy_id,
            "qa_fallback": self.qa_fallback,
            "reasons": list(self.reasons),
            "selected": self.selected,
            "shortlist_rank": self.shortlist_rank,
            "suppressed_by_candidate_id": self.suppressed_by_candidate_id,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())


@dataclass(frozen=True)
class PolicyShortlist:
    """Auditable shortlist comparison output for one building and policy.

    ``selected_candidate_ids`` is an ordered candidate set for later review.
    It is intentionally not a representative-image decision.
    """

    source: str
    source_building_id: str
    policy: PolicyDefinition
    evaluations: tuple[CandidateEvaluation, ...]
    selected_candidate_ids: tuple[str, ...]
    qa_fallback: bool

    @property
    def shortlist_id(self) -> str:
        return stable_shortlist_id(
            self.source, self.source_building_id, self.policy.config_sha256
        )

    @property
    def ordered_manifest_sha256(self) -> str:
        by_id = {row.candidate_id: row for row in self.evaluations}
        return canonical_sha256(
            {
                "ordered_items": [
                    {
                        "candidate_id": candidate_id,
                        "candidate_record_sha256": by_id[
                            candidate_id
                        ].candidate_record_sha256,
                        "rank": rank,
                    }
                    for rank, candidate_id in enumerate(
                        self.selected_candidate_ids, 1
                    )
                ],
                "policy_config_sha256": self.policy.config_sha256,
                "sample_policy_version": E3_SAMPLE_POLICY_VERSION,
                "shortlist_id": self.shortlist_id,
            }
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "evaluation_record_sha256s": [
                row.record_sha256 for row in self.evaluations
            ],
            "ordered_manifest_sha256": self.ordered_manifest_sha256,
            "policy_config_sha256": self.policy.config_sha256,
            "policy_id": self.policy.policy_id,
            "qa_fallback": self.qa_fallback,
            "selected_candidate_ids": list(self.selected_candidate_ids),
            "shortlist_id": self.shortlist_id,
            "source": self.source,
            "source_building_id": self.source_building_id,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())


def _candidate_reason_prefix(candidate: Candidate) -> list[str]:
    reasons = ["fingerprint_success" if candidate.is_success else "fingerprint_not_success"]
    reasons.append(f"editorial_role_{candidate.role if candidate.role in {'cover', 'gallery'} else 'other'}")
    if candidate.is_hard_risk:
        reasons.extend(f"quality_hard_risk:{value}" for value in candidate.hard_risk_reasons)
    else:
        reasons.append("quality_non_risk")
    if candidate.original_width is None or candidate.original_height is None:
        reasons.append("dimensions_missing")
    return reasons


def _direct_edge_map(
    edges: Iterable[DirectPHashEdge], candidate_ids: set[str]
) -> dict[frozenset[str], DirectPHashEdge]:
    mapped: dict[frozenset[str], DirectPHashEdge] = {}
    for edge in edges:
        if not isinstance(edge, DirectPHashEdge):
            raise TypeError("direct_phash_edges must contain DirectPHashEdge values")
        missing = set(edge.pair) - candidate_ids
        if missing:
            raise ValueError(
                "direct pHash edge references unknown candidate IDs: "
                + ", ".join(sorted(missing))
            )
        key = frozenset(edge.pair)
        prior = mapped.get(key)
        if prior is not None and prior.distance != edge.distance:
            raise ValueError("conflicting direct pHash distances for one pair")
        mapped[key] = edge
    return mapped


def _p2_relation(
    candidate: Candidate,
    selected: Candidate,
    policy: PolicyDefinition,
    edges: Mapping[frozenset[str], DirectPHashEdge],
) -> str | None:
    if (
        policy.suppress_exact_pixel
        and candidate.exact_cluster_id is not None
        and candidate.exact_cluster_id == selected.exact_cluster_id
    ):
        return "suppressed_exact_pixel"
    if (
        policy.suppress_same_phash_node
        and candidate.phash_node_id is not None
        and candidate.phash_node_id == selected.phash_node_id
    ):
        return "suppressed_identical_phash"
    if policy.suppress_direct_phash_le8 and frozenset(
        (candidate.candidate_id, selected.candidate_id)
    ) in edges:
        return "suppressed_direct_phash_le8"
    return None


def evaluate_policy(
    candidates: Sequence[Candidate],
    policy: PolicyDefinition,
    *,
    direct_phash_edges: Iterable[DirectPHashEdge] = (),
) -> PolicyShortlist:
    """Evaluate one policy for exactly one source-qualified building.

    Input order does not affect the result.  P2 compares a candidate only with
    candidates already chosen for the shortlist, making A--B--C chains
    explicitly non-transitive when A--C has no direct relation.
    """

    if not isinstance(policy, PolicyDefinition):
        raise TypeError("policy must be a PolicyDefinition")
    values = tuple(candidates)
    if not values:
        raise ValueError("at least one candidate is required")
    if not all(isinstance(value, Candidate) for value in values):
        raise TypeError("candidates must contain Candidate values")
    building_keys = {(value.source, value.source_building_id) for value in values}
    if len(building_keys) != 1:
        raise ValueError("evaluate_policy accepts exactly one source-qualified building")
    by_id = {value.candidate_id: value for value in values}
    if len(by_id) != len(values):
        raise ValueError("candidate IDs must be unique within a building")
    edges = _direct_edge_map(direct_phash_edges, set(by_id))

    ordered = tuple(sorted(values, key=editorial_sort_key))
    editorial_ranks = {
        candidate.candidate_id: rank for rank, candidate in enumerate(ordered, 1)
    }
    successful = tuple(value for value in ordered if value.is_success)
    safe = tuple(value for value in successful if not value.is_hard_risk)
    qa_fallback = bool(policy.quality_gate and successful and not safe)
    if policy.quality_gate:
        pool = successful if qa_fallback else safe
    else:
        pool = successful

    selected: list[Candidate] = []
    suppressed: dict[str, tuple[str, str]] = {}
    if any(
        (
            policy.suppress_exact_pixel,
            policy.suppress_same_phash_node,
            policy.suppress_direct_phash_le8,
        )
    ):
        for candidate in pool:
            relation: tuple[str, str] | None = None
            for chosen in selected:
                reason = _p2_relation(candidate, chosen, policy, edges)
                if reason is not None:
                    relation = (reason, chosen.candidate_id)
                    break
            if relation is not None:
                suppressed[candidate.candidate_id] = relation
                continue
            if len(selected) < policy.shortlist_size:
                selected.append(candidate)
    else:
        selected.extend(pool[: policy.shortlist_size])

    selected_ranks = {
        value.candidate_id: rank for rank, value in enumerate(selected, 1)
    }
    pool_ids = {value.candidate_id for value in pool}
    evaluations: list[CandidateEvaluation] = []
    for candidate in ordered:
        reasons = _candidate_reason_prefix(candidate)
        chosen_rank = selected_ranks.get(candidate.candidate_id)
        suppressed_value = suppressed.get(candidate.candidate_id)
        suppressed_by: str | None = None
        if chosen_rank is not None:
            reasons.append(
                "selected_qa_fallback" if qa_fallback else "selected_shortlist"
            )
        elif not candidate.is_success:
            reasons.append("excluded_non_success")
        elif policy.quality_gate and candidate.candidate_id not in pool_ids:
            reasons.append("excluded_quality_hard_risk")
        elif suppressed_value is not None:
            reasons.append(suppressed_value[0])
            suppressed_by = suppressed_value[1]
        else:
            reasons.append("excluded_shortlist_limit")
        evaluations.append(
            CandidateEvaluation(
                policy_id=policy.policy_id,
                policy_config_sha256=policy.config_sha256,
                candidate_id=candidate.candidate_id,
                candidate_record_sha256=candidate.record_sha256,
                editorial_rank=editorial_ranks[candidate.candidate_id],
                shortlist_rank=chosen_rank,
                selected=chosen_rank is not None,
                qa_fallback=bool(chosen_rank is not None and qa_fallback),
                hard_risk=candidate.is_hard_risk,
                component_scores=candidate_component_scores(candidate),
                reasons=tuple(reasons),
                suppressed_by_candidate_id=suppressed_by,
            )
        )

    source, building_id = next(iter(building_keys))
    return PolicyShortlist(
        source=source,
        source_building_id=building_id,
        policy=policy,
        evaluations=tuple(evaluations),
        selected_candidate_ids=tuple(value.candidate_id for value in selected),
        qa_fallback=qa_fallback,
    )


def compare_standard_policies(
    candidates: Sequence[Candidate],
    *,
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE,
    direct_phash_edges: Iterable[DirectPHashEdge] = (),
) -> tuple[PolicyShortlist, PolicyShortlist, PolicyShortlist]:
    """Evaluate frozen P0/P1/P2 over the same building candidates."""

    edges = tuple(direct_phash_edges)
    return tuple(
        evaluate_policy(candidates, policy, direct_phash_edges=edges)
        for policy in policy_definitions(shortlist_size)
    )  # type: ignore[return-value]


def selection_stratum(candidate: Candidate) -> str:
    """Return a deterministic diagnostic stratum for a candidate."""

    if not candidate.is_success:
        return "non_success"
    if candidate.is_hard_risk:
        return "success_hard_risk"
    role = candidate.role if candidate.role in {"cover", "gallery"} else "other"
    return f"success_{role}"


@dataclass(frozen=True)
class SamplingItem:
    """One stable sample unit assigned to a source x stratum cell."""

    identity: str
    source: str
    stratum: str
    input_record_sha256: str

    def __post_init__(self) -> None:
        identity = _identity_part(self.identity, label="identity")
        source = _identity_part(self.source, label="source").casefold()
        stratum = _identity_part(self.stratum, label="stratum")
        record_sha = validate_sha256(
            self.input_record_sha256, label="input record SHA-256"
        )
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "stratum", stratum)
        object.__setattr__(self, "input_record_sha256", record_sha)

    @property
    def cell(self) -> tuple[str, str]:
        return (self.source, self.stratum)

    def as_record(self) -> dict[str, str]:
        return {
            "identity": self.identity,
            "input_record_sha256": self.input_record_sha256,
            "source": self.source,
            "stratum": self.stratum,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> "SamplingItem":
        return cls(
            identity=candidate.candidate_id,
            source=candidate.source,
            stratum=selection_stratum(candidate),
            input_record_sha256=candidate.record_sha256,
        )


def _validated_sampling_items(items: Iterable[SamplingItem]) -> tuple[SamplingItem, ...]:
    values = tuple(items)
    if not all(isinstance(value, SamplingItem) for value in values):
        raise TypeError("items must contain SamplingItem values")
    counts = Counter(value.identity for value in values)
    duplicates = sorted(identity for identity, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError("sampling identities must be unique: " + ", ".join(duplicates))
    return values


def _sample_size(value: int, total: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("sample_size must be a non-negative integer")
    if value > total:
        raise ValueError("sample_size cannot exceed the item count")
    return value


def _largest_remainder(
    capacities: Mapping[tuple[str, str], int], seats: int
) -> dict[tuple[str, str], int]:
    cells = sorted(capacities)
    total_capacity = sum(capacities.values())
    if seats == 0:
        return {cell: 0 for cell in cells}
    if seats < 0 or seats > total_capacity:
        raise ValueError("largest-remainder seats exceed capacity")
    floors: dict[tuple[str, str], int] = {}
    remainders: list[tuple[int, tuple[str, str]]] = []
    for cell in cells:
        numerator = seats * capacities[cell]
        floors[cell] = numerator // total_capacity
        remainders.append((numerator % total_capacity, cell))
    remaining = seats - sum(floors.values())
    for _remainder, cell in sorted(remainders, key=lambda value: (-value[0], value[1]))[
        :remaining
    ]:
        floors[cell] += 1
    return floors


def allocate_stratified_quotas(
    items: Iterable[SamplingItem], sample_size: int
) -> dict[tuple[str, str], int]:
    """Allocate source x stratum quotas by proportional largest remainder.

    Every non-empty cell receives one slot before proportional allocation when
    ``sample_size`` is at least the number of cells.  This is the minimum-cell
    coverage guarantee; it is skipped when covering every cell is impossible.
    Remaining slots are Hamilton/largest-remainder allocations over residual
    cell capacity with lexicographic cell tie-breaking.
    """

    values = _validated_sampling_items(items)
    sample_size = _sample_size(sample_size, len(values))
    counts = Counter(value.cell for value in values)
    if not counts:
        return {}
    cells = sorted(counts)
    baseline = 1 if sample_size >= len(cells) else 0
    quotas = {cell: baseline for cell in cells}
    remaining = sample_size - baseline * len(cells)
    residual = {cell: counts[cell] - baseline for cell in cells}
    additions = _largest_remainder(residual, remaining)
    for cell in cells:
        quotas[cell] += additions[cell]
        if quotas[cell] > counts[cell]:
            raise AssertionError("quota exceeds cell capacity")
    if sum(quotas.values()) != sample_size:
        raise AssertionError("quota accounting mismatch")
    return quotas


def deterministic_sample_score(seed: str, item: SamplingItem) -> str:
    """Return the stable within-cell sampling score for one item."""

    seed = _identity_part(seed, label="seed")
    if not isinstance(item, SamplingItem):
        raise TypeError("item must be a SamplingItem")
    return canonical_sha256(
        {
            "identity": item.identity,
            "policy_version": E3_SAMPLE_POLICY_VERSION,
            "seed": seed,
            "source": item.source,
            "stratum": item.stratum,
        }
    )


def deterministic_stratified_sample(
    items: Iterable[SamplingItem], *, sample_size: int, seed: str
) -> tuple[SamplingItem, ...]:
    """Select an ordered, input-order-independent stratified sample."""

    seed = _identity_part(seed, label="seed")
    values = _validated_sampling_items(items)
    quotas = allocate_stratified_quotas(values, sample_size)
    by_cell: dict[tuple[str, str], list[SamplingItem]] = defaultdict(list)
    for item in values:
        by_cell[item.cell].append(item)
    selected: list[SamplingItem] = []
    for cell in sorted(by_cell):
        ordered = sorted(
            by_cell[cell],
            key=lambda item: (deterministic_sample_score(seed, item), item.identity),
        )
        selected.extend(ordered[: quotas[cell]])
    return tuple(selected)


def ordered_sample_manifest_sha256(items: Iterable[SamplingItem]) -> str:
    """Hash a caller-defined sample order and every selected input record."""

    values = tuple(items)
    if not all(isinstance(value, SamplingItem) for value in values):
        raise TypeError("items must contain SamplingItem values")
    return canonical_sha256(
        {
            "ordered_items": [
                {
                    "item": value.as_record(),
                    "item_record_sha256": value.record_sha256,
                    "rank": rank,
                }
                for rank, value in enumerate(values, 1)
            ],
            "policy_version": E3_SAMPLE_POLICY_VERSION,
        }
    )


__all__ = [
    "Candidate",
    "CandidateEvaluation",
    "DEFAULT_SHORTLIST_SIZE",
    "DirectPHashEdge",
    "E3_POLICY_VERSION",
    "E3_SAMPLE_POLICY_VERSION",
    "E3_SELECTION_VERSION",
    "HARD_RISK_MIN_SHORT_EDGE",
    "P0_POLICY_ID",
    "P1_POLICY_ID",
    "P2_POLICY_ID",
    "PolicyDefinition",
    "PolicyShortlist",
    "SamplingItem",
    "allocate_stratified_quotas",
    "candidate_component_scores",
    "candidate_record_sha256",
    "canonical_json",
    "canonical_sha256",
    "compare_standard_policies",
    "deterministic_sample_score",
    "deterministic_stratified_sample",
    "editorial_sort_key",
    "evaluate_policy",
    "ordered_sample_manifest_sha256",
    "policy_definitions",
    "selection_stratum",
    "stable_candidate_id",
    "stable_shortlist_id",
    "validate_sha256",
]
