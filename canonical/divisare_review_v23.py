"""Pure decision and identity policy for the Divisare v2.3 review overlay.

The production builder treats the v2.2 database as immutable evidence.  This
module contains no SQLite writes so manifest and graph behavior can be tested
without constructing the full artifact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 6
METADATA_VERSION = "divisare-metadata-v2.3"
POLICY_VERSION = "divisare-metadata-review-v2.3.0"
BUILDER_VERSION = "divisare-metadata-review-builder-v2.3.0"

EXPECTED_PARENT_METADATA_VERSION = "divisare-metadata-v2.2"
EXPECTED_PARENT_SCHEMA = 5
EXPECTED_PARENT_SHA256 = (
    "ee7bcd55fedf38fe8cb9a49f51e8f12f69493aef68ff1d201d2fa1e5be8ec95c"
)
EXPECTED_PARTIAL_SUPERSEDES_SHA256 = (
    "f31ee5b94afec2c5cde59f2479ea2e06a1e27925b24336f973b571e40838b5df"
)
EXPECTED_PARSER_VERSION = "divisare-html-metadata-v2.3"

PARTIAL_SCHEMA_VERSION = 1
AREA_SCHEMA_VERSION = 1
D2_SCHEMA_VERSION = 1

AREA_DECISION_TYPES = frozenset(
    {
        "accept_area",
        "keep_scoped_candidate",
        "keep_null_multi_or_conflict",
        "reject_non_area",
    }
)
AREA_CLOSURE_STATUSES = frozenset({"final", "open_external_text_review"})
PRODUCTION_AREA_COUNTS = {
    "accept_area": 10,
    "keep_scoped_candidate": 15,
    "keep_null_multi_or_conflict": 21,
    "reject_non_area": 79,
    "final": 123,
    "open_external_text_review": 2,
    "total": 125,
}
PRODUCTION_D2_COUNTS = {
    "merge": 8,
    "reject": 128,
    "defer": 84,
    "total": 220,
    "unique_components": 134,
    "unique_building_pairs": 214,
    "approved": 220,
    "approved_abstentions": 84,
}

D2_DECISIONS = frozenset({"merge", "reject", "defer"})
D2_MERGE_SCOPE = "same_architectural_project_intervention"
D2_RELATION_TYPES = frozenset(
    {
        "same_project_duplicate",
        "distinct_sibling_building",
        "distinct_phase_or_intervention",
        "distinct_event_entry",
        "distinct_same_name",
        "unresolved_identity",
    }
)
D2_REJECT_RELATION_TYPES = frozenset(
    {
        "distinct_sibling_building",
        "distinct_phase_or_intervention",
        "distinct_event_entry",
        "distinct_same_name",
    }
)
D2_RELATED_RELATIONS = frozenset(
    {
        "alternative_design",
        "same_campus",
        "same_series",
        "same_event",
        "same_site",
        "same_complex",
        "same_collection",
        "successive_intervention",
    }
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return sha256_bytes(value.encode("utf-8"))


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _load_json(path: Path) -> Tuple[Dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not valid UTF-8 JSON: %s" % path) from exc
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object: %s" % path)
    return payload, sha256_bytes(raw)


@dataclass(frozen=True)
class LoadedManifest:
    path: Path
    sha256: str
    schema_version: int
    version: str
    policy: str
    frozen_at: Optional[str]
    decisions: Mapping[Any, Mapping[str, Any]]
    counts: Mapping[str, int]


def load_partial_manifest(path: Path) -> LoadedManifest:
    payload, digest = _load_json(path)
    if payload.get("schema_version") != PARTIAL_SCHEMA_VERSION:
        raise ValueError("unsupported partial manifest schema_version")
    version = _clean(payload.get("version"))
    policy = version
    decided_by = _clean(payload.get("decided_by"))
    decided_at = _clean(payload.get("decided_at"))
    supersedes = payload.get("supersedes")
    if not version or not decided_by or not decided_at:
        raise ValueError("partial manifest is missing provenance")
    if not isinstance(supersedes, Mapping):
        raise ValueError("partial manifest must pin its superseded snapshot")
    if _clean(supersedes.get("sha256")).casefold() != (
        EXPECTED_PARTIAL_SUPERSEDES_SHA256
    ):
        raise ValueError("partial manifest supersedes an unexpected v1 snapshot")

    indexed: Dict[int, Dict[str, Any]] = {}
    counts = {"accept": 0, "reject": 0, "review": 0, "total": 0}
    items = payload.get("decisions")
    if not isinstance(items, list):
        raise ValueError("partial manifest requires a decisions array")
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("partial decision must be an object")
        try:
            article_id = int(item["article_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("partial decision has an invalid article_id") from exc
        if article_id in indexed:
            raise ValueError("duplicate partial decision: %d" % article_id)
        parser_version = _clean(item.get("parser_version"))
        prose_sha = _clean(item.get("prose_sha256")).casefold()
        decision = _clean(item.get("decision")).casefold()
        reason_code = _clean(item.get("reason_code"))
        note = _clean(item.get("note"))
        if parser_version != EXPECTED_PARSER_VERSION:
            raise ValueError("partial decision parser guard is not v2.3")
        if len(prose_sha) != 64:
            raise ValueError("partial decision has an invalid prose SHA")
        if decision not in counts or decision == "total":
            raise ValueError("partial decision must be accept, reject, or review")
        if not reason_code or not note:
            raise ValueError("partial decision is missing reason provenance")
        indexed[article_id] = {
            **dict(item),
            "article_id": article_id,
            "parser_version": parser_version,
            "prose_sha256": prose_sha,
            "decision": decision,
            "reason_code": reason_code,
            "note": note,
            "decided_by": decided_by,
            "decided_at": decided_at,
            "decision_policy_version": version,
        }
        counts[decision] += 1
        counts["total"] += 1
    return LoadedManifest(
        path.resolve(), digest, PARTIAL_SCHEMA_VERSION, version, policy,
        decided_at, indexed, counts,
    )


def _manifest_parent_sha(payload: Mapping[str, Any]) -> str:
    direct = _clean(payload.get("parent_sha256"))
    if direct:
        return direct.casefold()
    lineage = payload.get("lineage")
    if isinstance(lineage, Mapping):
        artifact = lineage.get("parent_artifact")
        if isinstance(artifact, Mapping):
            return _clean(artifact.get("sha256")).casefold()
    artifact = payload.get("parent_artifact")
    if isinstance(artifact, Mapping):
        return _clean(artifact.get("sha256")).casefold()
    return ""


def load_area_manifest(
    path: Path,
    *,
    expected_parent_sha256: str,
    expected_counts: Optional[Mapping[str, int]] = PRODUCTION_AREA_COUNTS,
) -> LoadedManifest:
    payload, digest = _load_json(path)
    if payload.get("schema_version") != AREA_SCHEMA_VERSION:
        raise ValueError("unsupported area manifest schema_version")
    version = _clean(payload.get("version"))
    policy = _clean(payload.get("policy"))
    decided_by = _clean(payload.get("decided_by"))
    decided_at = _clean(payload.get("decided_at"))
    frozen_at = _clean(payload.get("frozen_at"))
    if not version or not policy or not decided_by or not decided_at or not frozen_at:
        raise ValueError("area manifest is missing versioned policy provenance")
    if _manifest_parent_sha(payload) != expected_parent_sha256.casefold():
        raise ValueError("area manifest parent SHA does not match the supplied v2.2 DB")
    image_policy = _clean(payload.get("image_policy")).casefold()
    if "never infer numeric area" not in image_policy:
        raise ValueError("area manifest must forbid numeric inference from images")

    items = payload.get("decisions")
    if not isinstance(items, list):
        raise ValueError("area manifest requires a decisions array")
    indexed: Dict[int, Dict[str, Any]] = {}
    counts = {key: 0 for key in AREA_DECISION_TYPES}
    counts.update({key: 0 for key in AREA_CLOSURE_STATUSES})
    counts["total"] = 0
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("area decision must be an object")
        try:
            article_id = int(item["article_id"])
            confidence = float(item["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("area decision has invalid scalar fields") from exc
        if article_id in indexed:
            raise ValueError("duplicate area decision: %d" % article_id)
        decision_type = _clean(item.get("decision_type"))
        closure = _clean(item.get("closure_status"))
        scope = _clean(item.get("area_scope"))
        rationale = _clean(item.get("rationale_code"))
        evidence = item.get("evidence")
        if decision_type not in AREA_DECISION_TYPES:
            raise ValueError("unsupported area decision_type: %s" % decision_type)
        if closure not in AREA_CLOSURE_STATUSES:
            raise ValueError("unsupported area closure_status: %s" % closure)
        if not 0.0 <= confidence <= 1.0 or not scope or not rationale:
            raise ValueError("area decision is missing scope/confidence/rationale")
        if not isinstance(evidence, Mapping):
            raise ValueError("area decision requires evidence")
        parser_version = _clean(evidence.get("parser_version"))
        if parser_version != EXPECTED_PARSER_VERSION:
            raise ValueError("area decision parser guard is not v2.3")
        for field in (
            "area_raw_sha256", "description_prose_sha256", "html_sha256"
        ):
            guard = evidence.get(field)
            if guard is not None and len(_clean(guard)) != 64:
                raise ValueError("area decision has invalid %s" % field)

        resolved = item.get("resolved_area_sqm")
        candidate = item.get("candidate_area_sqm")
        if resolved is not None:
            resolved = float(resolved)
        if candidate is not None:
            candidate = float(candidate)
        if decision_type == "accept_area":
            if resolved is None or resolved <= 0 or candidate is not None:
                raise ValueError("accept_area requires only a positive resolved value")
        elif decision_type == "keep_scoped_candidate":
            if resolved is not None or candidate is None or candidate <= 0:
                raise ValueError("scoped area requires only a positive candidate")
        elif resolved is not None or candidate is not None:
            raise ValueError("null/reject area decisions cannot retain numeric values")

        normalized = {
            **dict(item),
            "article_id": article_id,
            "decision_type": decision_type,
            "closure_status": closure,
            "area_scope": scope,
            "confidence": confidence,
            "rationale_code": rationale,
            "resolved_area_sqm": resolved,
            "candidate_area_sqm": candidate,
            "evidence": dict(evidence),
            "decision_policy_version": version,
        }
        indexed[article_id] = normalized
        counts[decision_type] += 1
        counts[closure] += 1
        counts["total"] += 1

    declared = payload.get("counts")
    if isinstance(declared, Mapping):
        declared_counts = {key: int(value) for key, value in declared.items()}
        for key, actual in counts.items():
            if declared_counts.get(key) != actual:
                raise ValueError("area manifest declared count mismatch: %s" % key)
    if expected_counts is not None:
        for key, expected in expected_counts.items():
            if counts.get(key) != int(expected):
                raise ValueError(
                    "area production count mismatch for %s: %s != %s"
                    % (key, counts.get(key), expected)
                )
    return LoadedManifest(
        path.resolve(), digest, AREA_SCHEMA_VERSION, version, policy,
        frozen_at, indexed, counts,
    )


def load_d2_manifest(
    path: Path,
    *,
    expected_parent_sha256: str,
    expected_pairs: Optional[Iterable[Tuple[int, int]]] = None,
    expected_counts: Optional[Mapping[str, int]] = PRODUCTION_D2_COUNTS,
) -> LoadedManifest:
    payload, digest = _load_json(path)
    if payload.get("schema_version") != D2_SCHEMA_VERSION:
        raise ValueError("unsupported D2 manifest schema_version")
    version = _clean(payload.get("version"))
    policy = _clean(payload.get("policy"))
    frozen_at = _clean(payload.get("frozen_at"))
    if not version or not policy or not frozen_at:
        raise ValueError("D2 manifest requires version, policy, and frozen_at")
    if _manifest_parent_sha(payload) != expected_parent_sha256.casefold():
        raise ValueError("D2 manifest parent SHA does not match the supplied parent")

    items = payload.get("decisions")
    if not isinstance(items, list):
        raise ValueError("D2 manifest requires a decisions array")
    indexed: Dict[Tuple[int, int], Dict[str, Any]] = {}
    decision_ids: set[str] = set()
    counts = {"merge": 0, "reject": 0, "defer": 0, "total": 0}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("D2 decision must be an object")
        try:
            left = int(item["article_id_a"])
            right = int(item["article_id_b"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("D2 decision has invalid article IDs") from exc
        if left == right:
            raise ValueError("D2 decision cannot compare an article with itself")
        if left > right:
            raise ValueError("D2 decision article IDs must be in ascending order")
        pair = (min(left, right), max(left, right))
        if pair in indexed:
            raise ValueError("duplicate D2 decision pair: %s" % (pair,))
        decision = _clean(item.get("decision")).casefold()
        decision_id = _clean(item.get("decision_id"))
        reviewer = _clean(item.get("reviewer"))
        reviewed_at = _clean(item.get("reviewed_at"))
        reason_code = _clean(item.get("reason_code"))
        note = _clean(item.get("note"))
        identity_scope = _clean(item.get("identity_scope"))
        relation_type = _clean(item.get("relation_type"))
        building_id_a_before = _clean(item.get("building_id_a_before"))
        building_id_b_before = _clean(item.get("building_id_b_before"))
        source_candidate_kind = _clean(item.get("source_candidate_kind"))
        component_id = _clean(item.get("component_id"))
        building_pair_id = _clean(item.get("building_pair_id"))
        try:
            source_score = float(item["source_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("D2 decision has invalid source_score") from exc
        approved = item.get("approved") is True
        evidence_payload = item.get("evidence", [])
        evidence_entries: Sequence[Any]
        if isinstance(evidence_payload, list):
            evidence_entries = evidence_payload
        elif isinstance(evidence_payload, Mapping):
            nested = evidence_payload.get("entries", [])
            evidence_entries = nested if isinstance(nested, list) else []
        else:
            raise ValueError("D2 evidence must be an array or an object with entries")
        derived_families = {
            _clean(entry.get("evidence_family") or entry.get("family"))
            for entry in evidence_entries
            if (
                isinstance(entry, Mapping)
                and entry.get("independent_for_merge") is True
                and _clean(entry.get("supports")) == "same_identity"
            )
        }
        derived_families.discard("")
        supplied_payload = item.get("evidence_families")
        supplied_families = {
            _clean(value) for value in (supplied_payload or []) if _clean(value)
        }
        if supplied_payload is not None and supplied_families != derived_families:
            raise ValueError("D2 supplied and derived evidence families disagree")
        evidence_families = sorted(derived_families)
        try:
            declared_family_count = int(
                item.get("evidence_family_count", len(evidence_families))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("D2 evidence_family_count must be an integer") from exc
        if declared_family_count != len(evidence_families):
            raise ValueError("D2 evidence_family_count is inconsistent")
        hard_conflicts = item.get("hard_conflicts", [])
        guards = item.get("guards")
        related_relation = (
            _clean(item.get("related_relation"))
            if item.get("related_relation") is not None
            else None
        )
        if decision not in D2_DECISIONS:
            raise ValueError("unsupported D2 decision: %s" % decision)
        if not decision_id or decision_id in decision_ids:
            raise ValueError("D2 decision_id must be non-empty and unique")
        if not reviewer or not reviewed_at or not reason_code or not note:
            raise ValueError("D2 decision is missing review provenance")
        if not all(
            (
                building_id_a_before, building_id_b_before,
                source_candidate_kind, component_id, building_pair_id,
            )
        ):
            raise ValueError("D2 decision is missing pair provenance")
        if not approved:
            raise ValueError("D2 decisions must be explicitly approved")
        if relation_type not in D2_RELATION_TYPES:
            raise ValueError("unsupported D2 relation_type: %s" % relation_type)
        if identity_scope != D2_MERGE_SCOPE:
            raise ValueError("D2 decision has the wrong identity scope")
        if related_relation is not None and related_relation not in D2_RELATED_RELATIONS:
            raise ValueError("unsupported D2 related_relation: %s" % related_relation)
        if not isinstance(hard_conflicts, list):
            raise ValueError("D2 hard_conflicts must be an array")
        if not isinstance(guards, Mapping):
            raise ValueError("D2 decision requires article guards")
        normalized_guards: Dict[str, Dict[str, Any]] = {}
        for side, article_id in (("article_a", pair[0]), ("article_b", pair[1])):
            guard = guards.get(side)
            if not isinstance(guard, Mapping):
                raise ValueError("D2 decision is missing %s guard" % side)
            try:
                guarded_article_id = int(guard["article_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("D2 %s guard has an invalid article ID" % side) from exc
            if guarded_article_id != article_id:
                raise ValueError("D2 %s guard article ID does not match its pair" % side)
            if not _clean(guard.get("source_url")):
                raise ValueError("D2 %s guard requires source_url" % side)
            if _clean(guard.get("parser_version")) != EXPECTED_PARSER_VERSION:
                raise ValueError("D2 %s guard parser version is not v2.3" % side)
            for field in (
                "description_prose_sha256", "abstract_sha256", "html_sha256",
                "source_row_hash",
            ):
                value = guard.get(field)
                if value is not None and len(_clean(value)) != 64:
                    raise ValueError("D2 %s guard has invalid %s" % (side, field))
            if not _clean(guard.get("snapshot_path")):
                raise ValueError("D2 %s guard requires snapshot_path" % side)
            normalized_guards[side] = dict(guard)
        if decision == "merge":
            if identity_scope != D2_MERGE_SCOPE:
                raise ValueError("D2 merge has the wrong identity scope")
            if relation_type != "same_project_duplicate":
                raise ValueError(
                    "D2 merge relation_type must be same_project_duplicate"
                )
            if len(evidence_families) < 2:
                raise ValueError("D2 merge requires two independent evidence families")
            if hard_conflicts:
                raise ValueError("D2 merge cannot contain a hard conflict")
        elif decision == "reject":
            if relation_type not in D2_REJECT_RELATION_TYPES:
                raise ValueError("D2 reject requires a distinct relation_type")
            if not hard_conflicts or not all(
                isinstance(conflict, Mapping) and _clean(conflict.get("fact"))
                for conflict in hard_conflicts
            ):
                raise ValueError("D2 reject requires factual hard conflicts")
        elif relation_type != "unresolved_identity":
            raise ValueError("D2 defer relation_type must be unresolved_identity")
        related_project = bool(item.get("related_project", False))
        related_group_id = _clean(item.get("related_group_id")) or None
        if related_project:
            if decision != "reject" or related_relation is None or not related_group_id:
                raise ValueError(
                    "D2 related-project edge requires reject, relation, and group"
                )
        elif related_group_id is not None:
            raise ValueError("D2 non-edge decision cannot retain a related group")

        normalized = {
            **dict(item),
            "article_id_a": pair[0],
            "article_id_b": pair[1],
            "decision": decision,
            "decision_id": decision_id,
            "building_id_a_before": building_id_a_before,
            "building_id_b_before": building_id_b_before,
            "source_candidate_kind": source_candidate_kind,
            "source_score": source_score,
            "component_id": component_id,
            "building_pair_id": building_pair_id,
            "approved": True,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "reason_code": reason_code,
            "note": note,
            "identity_scope": identity_scope,
            "relation_type": relation_type,
            "related_project": related_project,
            "related_relation": related_relation,
            "related_group_id": related_group_id,
            "evidence_families": evidence_families,
            "evidence_family_count": declared_family_count,
            "hard_conflicts": list(hard_conflicts),
            "evidence": evidence_payload,
            "guards": normalized_guards,
            "decision_policy_version": version,
        }
        indexed[pair] = normalized
        decision_ids.add(decision_id)
        counts[decision] += 1
        counts["total"] += 1

    article_components: Dict[int, str] = {}
    building_pair_outcomes: Dict[str, Tuple[Any, ...]] = {}
    for pair, item in sorted(indexed.items()):
        component_id = str(item["component_id"])
        for article_id in pair:
            previous = article_components.setdefault(article_id, component_id)
            if previous != component_id:
                raise ValueError("D2 component_id is inconsistent across graph edges")
        outcome = (
            item["decision"], item["relation_type"], item["related_project"],
            item["related_relation"], item["related_group_id"],
        )
        previous_outcome = building_pair_outcomes.setdefault(
            str(item["building_pair_id"]), outcome
        )
        if previous_outcome != outcome:
            raise ValueError("D2 duplicate building-pair decisions disagree")

    if expected_pairs is not None:
        expected = {tuple(sorted((int(a), int(b)))) for a, b in expected_pairs}
        actual = set(indexed)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            raise ValueError(
                "D2 decision pair set mismatch: missing=%s extra=%s"
                % (missing[:10], extra[:10])
            )
    derived_extra = {
        "unique_components": len(
            {_clean(item.get("component_id")) for item in indexed.values()}
        ),
        "unique_building_pairs": len(
            {_clean(item.get("building_pair_id")) for item in indexed.values()}
        ),
        "approved": counts["total"],
        "approved_abstentions": counts["defer"],
    }
    counts.update(derived_extra)
    declared = payload.get("counts")
    if isinstance(declared, Mapping):
        for key, actual in {
            key: counts[key] for key in ("merge", "reject", "defer", "total")
        }.items():
            declared_key = "total_pairs" if key == "total" and "total" not in declared else key
            if int(declared.get(declared_key, -1)) != actual:
                raise ValueError("D2 manifest declared count mismatch: %s" % key)
        for key, actual in derived_extra.items():
            if key in declared and int(declared[key]) != actual:
                raise ValueError("D2 manifest declared count mismatch: %s" % key)
    reject_relation_counts: Dict[str, int] = {}
    for item in indexed.values():
        if item["decision"] == "reject":
            relation = str(item["relation_type"])
            reject_relation_counts[relation] = reject_relation_counts.get(relation, 0) + 1
    declared_relations = payload.get("reject_relation_counts")
    if isinstance(declared_relations, Mapping):
        normalized_declared = {
            str(key): int(value) for key, value in declared_relations.items()
        }
        if normalized_declared != reject_relation_counts:
            raise ValueError("D2 reject_relation_counts mismatch")
    if expected_counts is not None:
        for key, expected in expected_counts.items():
            if counts.get(key) != int(expected):
                raise ValueError(
                    "D2 production count mismatch for %s: %s != %s"
                    % (key, counts.get(key), expected)
                )
    return LoadedManifest(
        path.resolve(), digest, D2_SCHEMA_VERSION, version, policy,
        frozen_at, indexed, counts,
    )


def validate_partial_guard(
    decision: Mapping[str, Any],
    *,
    parser_version: Optional[str],
    prose_sha256: Optional[str],
) -> None:
    if parser_version != decision["parser_version"]:
        raise RuntimeError("partial decision parser guard failed")
    if (prose_sha256 or "").casefold() != decision["prose_sha256"]:
        raise RuntimeError("partial decision prose hash guard failed")


def validate_area_guard(
    decision: Mapping[str, Any],
    *,
    parser_version: Optional[str],
    area_raw: Optional[str],
    description_prose: Optional[str],
    html_sha256: Optional[str],
) -> None:
    evidence = decision["evidence"]
    if parser_version != evidence.get("parser_version"):
        raise RuntimeError("area decision parser guard failed")
    actual = {
        "area_raw_sha256": sha256_text(area_raw),
        "description_prose_sha256": sha256_text(description_prose),
        "html_sha256": html_sha256.casefold() if html_sha256 else None,
    }
    for field, value in actual.items():
        expected = evidence.get(field)
        if expected is not None and (value or "").casefold() != str(expected).casefold():
            raise RuntimeError("area decision %s guard failed" % field)


def validate_d2_guard(
    decision: Mapping[str, Any],
    *,
    side: str,
    article_id: int,
    source_url: Optional[str],
    parser_version: Optional[str],
    description_prose: Optional[str],
    recrawl_abstract: Optional[str],
    html_sha256: Optional[str],
    source_row_hash: Optional[str],
    snapshot_path: Optional[str],
) -> None:
    if side not in ("article_a", "article_b"):
        raise ValueError("D2 guard side must be article_a or article_b")
    guard = decision["guards"][side]
    actual = {
        "article_id": int(article_id),
        "source_url": source_url,
        "parser_version": parser_version,
        "description_prose_sha256": sha256_text(description_prose),
        "abstract_sha256": sha256_text(recrawl_abstract),
        "html_sha256": html_sha256,
        "source_row_hash": source_row_hash,
        "snapshot_path": snapshot_path,
    }
    for field, value in actual.items():
        expected = guard.get(field)
        if field.endswith("sha256") and expected is not None:
            matches = (value or "").casefold() == str(expected).casefold()
        elif field == "snapshot_path":
            actual_path = str(value or "").replace("\\", "/")
            expected_path = str(expected or "").replace("\\", "/")
            matches = actual_path == expected_path or actual_path.endswith(
                "/" + expected_path.lstrip("/")
            )
        else:
            matches = value == expected
        if not matches:
            raise RuntimeError("D2 %s %s guard failed" % (side, field))


class UnionFind:
    def __init__(self, values: Iterable[str]):
        self.parent = {str(value): str(value) for value in values}

    def find(self, value: str) -> str:
        value = str(value)
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        survivor = min(left_root, right_root)
        loser = max(left_root, right_root)
        self.parent[loser] = survivor


def resolve_identity_components(
    active_buildings: Iterable[str],
    article_to_building: Mapping[int, str],
    decisions: Mapping[Tuple[int, int], Mapping[str, Any]],
) -> Dict[str, str]:
    nodes = sorted({str(value) for value in active_buildings})
    union = UnionFind(nodes)
    for pair, decision in sorted(decisions.items()):
        if decision["decision"] != "merge":
            continue
        try:
            left = article_to_building[int(pair[0])]
            right = article_to_building[int(pair[1])]
        except KeyError as exc:
            raise ValueError("D2 decision references an article without membership") from exc
        union.union(left, right)

    mapping = {node: union.find(node) for node in nodes}
    for pair, decision in sorted(decisions.items()):
        if decision["decision"] == "merge":
            continue
        left = mapping[article_to_building[int(pair[0])]]
        right = mapping[article_to_building[int(pair[1])]]
        if left == right:
            raise ValueError(
                "D2 %s pair collapses through a merge component: %s"
                % (decision["decision"], pair)
            )
    return mapping
