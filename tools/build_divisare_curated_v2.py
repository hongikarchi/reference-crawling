"""Build an immutable Divisare metadata-v2 overlay from a curated v1.5 DB.

The parent database is opened read-only and copied with SQLite's backup API.
All v1 tables remain byte-for-byte logical inputs; v2 truth lives only in
tables and views whose names end in ``_v2``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.divisare_curated_v2 import (  # noqa: E402
    ARTICLE_KIND_POLICY_VERSION,
    EVIDENCE_POLICY_VERSION,
    FACET_POLICY_VERSION,
    METADATA_VERSION,
    PRIMARY_VALUE_POLICY_VERSION,
    SCHEMA_VERSION,
    evidence_family_for_claim,
    facet_status_v2,
    independence_key_for_claim,
    infer_article_kind_evidence,
    resolve_article_kind,
)


BUILDER_VERSION = "divisare-metadata-v2-builder-v2.1"
EXPECTED_PARENT_BUILDER = "divisare-curated-builder-v1.5"
EXPECTED_PARENT_SCHEMA = 2
DECISION_SCHEMA_VERSION = 1

SCALAR_AXES = (
    "style",
    "structural_system",
    "roof_type",
    "facade_pattern",
    "facade_system",
)
EXCLUDED_FACET_AXES = (
    "country",
    "city",
    "project_year",
    "area_sqm",
    "country_candidate",
    "city_candidate",
)
SEARCH_TIER_RANK = {"hidden": 0, "secondary": 1, "primary": 2}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def required_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def validate_parent(conn: sqlite3.Connection) -> Dict[str, Any]:
    required = {
        "build_runs",
        "source_articles",
        "article_tags",
        "source_tags",
        "attribute_claims",
        "article_match_candidates",
        "buildings",
        "building_articles",
        "building_facets",
        "building_facet_claims",
        "article_image_occurrences",
        "image_urls",
    }
    present = required_tables(conn)
    missing = sorted(required - present)
    if missing:
        raise RuntimeError("parent DB is missing required tables: %s" % missing)
    if "artifact_lineage_v2" in present:
        raise RuntimeError("parent DB already contains a metadata-v2 overlay")

    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        raise RuntimeError("parent DB quick_check failed: %s" % quick_check)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    run = conn.execute(
        """
        SELECT *
        FROM build_runs
        WHERE status='complete'
        ORDER BY run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if run is None:
        raise RuntimeError("parent DB has no completed build run")
    if run["builder_version"] != EXPECTED_PARENT_BUILDER:
        raise RuntimeError(
            "expected parent builder %s, found %s"
            % (EXPECTED_PARENT_BUILDER, run["builder_version"])
        )
    if user_version != EXPECTED_PARENT_SCHEMA:
        raise RuntimeError(
            "expected parent user_version %d, found %d"
            % (EXPECTED_PARENT_SCHEMA, user_version)
        )

    counts = {
        "articles": conn.execute(
            "SELECT COUNT(*) FROM source_articles"
        ).fetchone()[0],
        "buildings": conn.execute(
            "SELECT COUNT(*) FROM buildings"
        ).fetchone()[0],
        "facets": conn.execute(
            "SELECT COUNT(*) FROM building_facets"
        ).fetchone()[0],
        "claims": conn.execute(
            "SELECT COUNT(*) FROM attribute_claims"
        ).fetchone()[0],
        "match_candidates": conn.execute(
            "SELECT COUNT(*) FROM article_match_candidates"
        ).fetchone()[0],
    }
    return {
        "quick_check": quick_check,
        "user_version": user_version,
        "run": dict(run),
        "counts": counts,
    }


def validate_paths(
    parent_path: Path,
    output_path: Path,
    report_path: Path,
    temp_path: Path,
    report_temp_path: Path,
    lock_path: Path,
) -> None:
    resolved = [
        parent_path.resolve(),
        output_path.resolve(),
        report_path.resolve(),
        temp_path.resolve(),
        report_temp_path.resolve(),
        lock_path.resolve(),
    ]
    if len(set(resolved)) != len(resolved):
        raise ValueError("parent, output, report, temp, and lock paths must differ")


@contextmanager
def exclusive_build_lock(lock_path: Path, output_path: Path):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
    except FileExistsError as exc:
        raise RuntimeError(
            "another v2 build may be running; lock exists: %s" % lock_path
        ) from exc
    try:
        payload = json_dumps(
            {
                "pid": os.getpid(),
                "output": str(output_path),
                "created_at": utc_now(),
            }
        )
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        fd = -1
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


SCHEMA_SQL = """
CREATE TABLE artifact_lineage_v2 (
    lineage_id                 INTEGER PRIMARY KEY CHECK(lineage_id=1),
    parent_db_path             TEXT NOT NULL,
    parent_sha256              TEXT NOT NULL CHECK(length(parent_sha256)=64),
    parent_byte_size           INTEGER NOT NULL,
    parent_user_version        INTEGER NOT NULL,
    parent_builder_version     TEXT NOT NULL,
    parent_taxonomy_version    TEXT NOT NULL,
    parent_cluster_version     TEXT NOT NULL,
    parent_resolver_version    TEXT NOT NULL,
    v2_builder_version         TEXT NOT NULL,
    v2_schema_version          INTEGER NOT NULL,
    metadata_version           TEXT NOT NULL,
    evidence_policy_version    TEXT NOT NULL,
    facet_policy_version       TEXT NOT NULL,
    article_kind_policy_version TEXT NOT NULL,
    primary_value_policy_version TEXT NOT NULL,
    decision_schema_version    INTEGER,
    decision_file_path         TEXT,
    decision_file_sha256       TEXT,
    created_at                 TEXT NOT NULL
);

CREATE TABLE claim_evidence_v2 (
    claim_id                   INTEGER PRIMARY KEY
        REFERENCES attribute_claims(claim_id),
    evidence_family            TEXT NOT NULL,
    independence_key           TEXT NOT NULL,
    mapping_kind               TEXT NOT NULL
        CHECK(mapping_kind IN ('direct','supporting','editorial','exclusion')),
    policy_version             TEXT NOT NULL,
    details_json               TEXT NOT NULL CHECK(json_valid(details_json))
);

CREATE TABLE article_kind_evidence_v2 (
    evidence_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id                 INTEGER NOT NULL
        REFERENCES source_articles(article_id),
    proposed_kind              TEXT NOT NULL CHECK(proposed_kind IN (
      'project','drawing_feature','photo_feature','model_feature',
      'concept_editorial','mixed_feature'
    )),
    confidence                 REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    evidence_kind              TEXT NOT NULL,
    evidence_family            TEXT NOT NULL,
    independence_key           TEXT NOT NULL,
    source_ref                 TEXT NOT NULL,
    status                     TEXT NOT NULL CHECK(status IN ('candidate','strong')),
    reason                     TEXT NOT NULL,
    policy_version             TEXT NOT NULL,
    UNIQUE(article_id,proposed_kind,evidence_family,source_ref,policy_version)
);

CREATE TABLE article_kind_resolution_v2 (
    article_id                 INTEGER PRIMARY KEY
        REFERENCES source_articles(article_id),
    article_kind               TEXT NOT NULL CHECK(article_kind IN (
      'project','drawing_feature','photo_feature','model_feature',
      'concept_editorial','mixed_feature','unresolved'
    )),
    status                     TEXT NOT NULL
        CHECK(status IN ('confirmed','candidate','ambiguous','unresolved')),
    confidence                 REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    method                     TEXT NOT NULL,
    evidence_count             INTEGER NOT NULL,
    evidence_families_json     TEXT NOT NULL CHECK(json_valid(evidence_families_json)),
    ranked_kinds_json          TEXT NOT NULL CHECK(json_valid(ranked_kinds_json)),
    policy_version             TEXT NOT NULL,
    resolved_at                TEXT NOT NULL
);

CREATE TABLE article_match_reviews_v2 (
    article_id_a               INTEGER NOT NULL
        REFERENCES source_articles(article_id),
    article_id_b               INTEGER NOT NULL
        REFERENCES source_articles(article_id),
    source_candidate_kind      TEXT NOT NULL,
    source_score               REAL NOT NULL CHECK(source_score BETWEEN 0 AND 1),
    source_status              TEXT NOT NULL,
    source_signals_json        TEXT NOT NULL CHECK(json_valid(source_signals_json)),
    building_id_a              TEXT NOT NULL REFERENCES buildings(building_id),
    building_id_b              TEXT NOT NULL REFERENCES buildings(building_id),
    decision_status            TEXT NOT NULL
        CHECK(decision_status IN ('confirmed','pending','rejected','deferred')),
    decision_id                TEXT NOT NULL UNIQUE,
    recommendation             TEXT NOT NULL,
    decision_source            TEXT NOT NULL,
    decision_reason_json       TEXT NOT NULL CHECK(json_valid(decision_reason_json)),
    article_kind_context_json  TEXT NOT NULL CHECK(json_valid(article_kind_context_json)),
    decision_version           TEXT NOT NULL,
    decided_at                 TEXT,
    PRIMARY KEY(article_id_a,article_id_b),
    CHECK(article_id_a < article_id_b)
);

CREATE TABLE building_redirects_v2 (
    source_building_id         TEXT PRIMARY KEY REFERENCES buildings(building_id),
    target_building_id         TEXT NOT NULL REFERENCES buildings(building_id),
    decision_version           TEXT NOT NULL,
    decision_ids_json          TEXT NOT NULL CHECK(json_valid(decision_ids_json)),
    reason_json                TEXT NOT NULL CHECK(json_valid(reason_json)),
    created_at                 TEXT NOT NULL,
    CHECK(source_building_id <> target_building_id)
);

CREATE TABLE active_building_membership_v2 (
    article_id                 INTEGER PRIMARY KEY
        REFERENCES source_articles(article_id),
    building_id                TEXT NOT NULL REFERENCES buildings(building_id),
    source_building_id         TEXT NOT NULL REFERENCES buildings(building_id),
    source_article_role        TEXT NOT NULL,
    membership_confidence      REAL NOT NULL CHECK(membership_confidence BETWEEN 0 AND 1),
    decision_method            TEXT NOT NULL
);

CREATE TABLE building_images_materialized_v2 (
    building_id                TEXT NOT NULL REFERENCES buildings(building_id),
    asset_key                  TEXT NOT NULL REFERENCES image_assets(asset_key),
    representative_url         TEXT NOT NULL,
    role_rank                  INTEGER NOT NULL,
    first_position             INTEGER NOT NULL,
    PRIMARY KEY(building_id,asset_key)
);

CREATE TABLE building_article_roles_v2 (
    article_id                 INTEGER PRIMARY KEY
        REFERENCES source_articles(article_id),
    building_id                TEXT NOT NULL REFERENCES buildings(building_id),
    source_building_id         TEXT NOT NULL REFERENCES buildings(building_id),
    article_role               TEXT NOT NULL,
    article_kind               TEXT NOT NULL,
    article_kind_status        TEXT NOT NULL,
    role_confidence            REAL NOT NULL CHECK(role_confidence BETWEEN 0 AND 1),
    decision_method            TEXT NOT NULL,
    policy_version             TEXT NOT NULL
);

CREATE TABLE building_facets_v2 (
    facet_v2_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_facet_id            INTEGER REFERENCES building_facets(facet_id),
    building_id                TEXT NOT NULL REFERENCES buildings(building_id),
    axis                       TEXT NOT NULL,
    value                      TEXT NOT NULL,
    status                     TEXT NOT NULL
        CHECK(status IN ('candidate','confirmed','rejected')),
    role                       TEXT NOT NULL
        CHECK(role IN ('primary','secondary','facet')),
    confidence                 REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    claim_count                INTEGER NOT NULL,
    article_count              INTEGER NOT NULL,
    direct_claim_count         INTEGER NOT NULL,
    supporting_claim_count     INTEGER NOT NULL,
    source_count               INTEGER NOT NULL,
    evidence_family_count      INTEGER NOT NULL,
    independence_group_count   INTEGER NOT NULL,
    max_priority               INTEGER NOT NULL,
    search_tier                TEXT NOT NULL
        CHECK(search_tier IN ('primary','secondary','hidden')),
    resolver_version           TEXT NOT NULL,
    previous_status            TEXT,
    status_changed             INTEGER NOT NULL CHECK(status_changed IN (0,1)),
    UNIQUE(building_id,axis,value)
);

CREATE TABLE building_facet_claims_v2 (
    facet_v2_id                INTEGER NOT NULL
        REFERENCES building_facets_v2(facet_v2_id),
    claim_id                   INTEGER NOT NULL REFERENCES attribute_claims(claim_id),
    weight                     REAL NOT NULL,
    evidence_family            TEXT NOT NULL,
    independence_key           TEXT NOT NULL,
    PRIMARY KEY(facet_v2_id,claim_id)
);

CREATE TABLE building_attributes_v2 (
    building_id                TEXT PRIMARY KEY REFERENCES buildings(building_id),
    is_active                  INTEGER NOT NULL CHECK(is_active IN (0,1)),
    redirect_to                TEXT REFERENCES buildings(building_id),
    article_count              INTEGER NOT NULL,
    primary_article_id         INTEGER REFERENCES source_articles(article_id),
    name                       TEXT NOT NULL,
    name_normalized            TEXT NOT NULL,
    location_country           TEXT,
    location_city              TEXT,
    location_resolution_method TEXT NOT NULL,
    location_confidence        REAL NOT NULL CHECK(location_confidence BETWEEN 0 AND 1),
    project_year               INTEGER,
    year_kind                  TEXT NOT NULL,
    area_sqm                   REAL,
    description_text_id        INTEGER REFERENCES article_text_versions(text_id),
    core_conflicts_json        TEXT NOT NULL CHECK(json_valid(core_conflicts_json)),
    programs_json              TEXT NOT NULL CHECK(json_valid(programs_json)),
    program_primary            TEXT,
    program_confidence         REAL,
    mixed_use                  INTEGER NOT NULL CHECK(mixed_use IN (0,1)),
    typologies_json            TEXT NOT NULL CHECK(json_valid(typologies_json)),
    typology_primary           TEXT,
    typology_confidence        REAL,
    multi_typology             INTEGER NOT NULL CHECK(multi_typology IN (0,1)),
    style                      TEXT,
    structural_system          TEXT,
    roof_type                  TEXT,
    facade_pattern             TEXT,
    facade_system              TEXT,
    article_kind_counts_json   TEXT NOT NULL CHECK(json_valid(article_kind_counts_json)),
    facet_conflicts_json       TEXT NOT NULL CHECK(json_valid(facet_conflicts_json)),
    metadata_needs_review      INTEGER NOT NULL CHECK(metadata_needs_review IN (0,1)),
    resolution_version         TEXT NOT NULL,
    resolved_at                TEXT NOT NULL
);

CREATE TABLE article_recrawl_queue_v2 (
    article_id                 INTEGER PRIMARY KEY
        REFERENCES source_articles(article_id),
    source_url                 TEXT NOT NULL,
    priority                   INTEGER NOT NULL,
    reasons_json               TEXT NOT NULL CHECK(json_valid(reasons_json)),
    initial_fetch_status       TEXT NOT NULL DEFAULT 'pending'
        CHECK(initial_fetch_status='pending'),
    initial_parse_status       TEXT NOT NULL DEFAULT 'pending'
        CHECK(initial_parse_status='pending'),
    queued_at                  TEXT NOT NULL
);

CREATE TABLE metadata_build_metrics_v2 (
    metric                     TEXT PRIMARY KEY,
    value                      REAL NOT NULL,
    details_json               TEXT CHECK(details_json IS NULL OR json_valid(details_json))
);

CREATE TABLE metadata_validation_v2 (
    check_name                 TEXT PRIMARY KEY,
    passed                     INTEGER NOT NULL CHECK(passed IN (0,1)),
    actual_json                TEXT NOT NULL CHECK(json_valid(actual_json)),
    expected_json              TEXT NOT NULL CHECK(json_valid(expected_json)),
    checked_at                 TEXT NOT NULL
);

CREATE INDEX idx_claim_evidence_family_v2
ON claim_evidence_v2(evidence_family,independence_key);
CREATE INDEX idx_article_kind_status_v2
ON article_kind_resolution_v2(status,article_kind);
CREATE INDEX idx_match_review_status_v2
ON article_match_reviews_v2(decision_status,source_score DESC);
CREATE INDEX idx_redirect_target_v2
ON building_redirects_v2(target_building_id);
CREATE INDEX idx_active_membership_building_v2
ON active_building_membership_v2(building_id,article_id);
CREATE INDEX idx_building_images_order_v2
ON building_images_materialized_v2(
  building_id,role_rank,first_position,asset_key
);
CREATE INDEX idx_building_facets_search_v2
ON building_facets_v2(axis,value,status,search_tier);
CREATE INDEX idx_building_facets_building_v2
ON building_facets_v2(building_id,axis,status);
CREATE INDEX idx_recrawl_priority_v2
ON article_recrawl_queue_v2(priority DESC,article_id);
"""


ACTIVE_MEMBERSHIP_VIEW_SQL = """
CREATE VIEW v_active_building_articles_v2 AS
SELECT
    article_id,
    building_id,
    source_building_id,
    source_article_role,
    membership_confidence,
    decision_method
FROM active_building_membership_v2;
"""


class UnionFind:
    def __init__(self, values: Iterable[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
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


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def populate_claim_evidence(conn: sqlite3.Connection) -> int:
    rows: List[Tuple[Any, ...]] = []
    for claim in conn.execute(
        """
        SELECT claim_id,article_id,evidence_kind,details_json
        FROM attribute_claims
        ORDER BY claim_id
        """
    ):
        try:
            details = (
                json.loads(claim["details_json"])
                if claim["details_json"]
                else {}
            )
        except (TypeError, ValueError):
            details = {}
        mapping_kind = str(details.get("mapping_kind") or "direct")
        if mapping_kind not in {"direct", "supporting", "editorial", "exclusion"}:
            mapping_kind = "direct"
        family = evidence_family_for_claim(claim["evidence_kind"], details)
        independence_key = independence_key_for_claim(
            claim["article_id"],
            claim["evidence_kind"],
        )
        rows.append(
            (
                claim["claim_id"],
                family,
                independence_key,
                mapping_kind,
                EVIDENCE_POLICY_VERSION,
                json_dumps(
                    {
                        "source_evidence_kind": claim["evidence_kind"],
                        "source_details": details,
                    }
                ),
            )
        )
    conn.executemany(
        """
        INSERT INTO claim_evidence_v2(
            claim_id,evidence_family,independence_key,mapping_kind,
            policy_version,details_json
        ) VALUES (?,?,?,?,?,?)
        """,
        rows,
    )
    return len(rows)


def populate_article_kinds(conn: sqlite3.Connection) -> Dict[str, int]:
    tags: Dict[int, List[Tuple[str, str]]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT at.article_id,st.album_slug,at.tag_slug
        FROM article_tags at
        JOIN source_tags st ON st.tag_slug=at.tag_slug
        ORDER BY at.article_id,st.album_slug,at.ordinal
        """
    ):
        tags[int(row["article_id"])].append(
            (row["album_slug"], row["tag_slug"])
        )

    content_hints: Dict[int, List[str]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT article_id,value_normalized
        FROM attribute_claims
        WHERE scope='article'
          AND axis='content_hint'
          AND polarity='positive'
        ORDER BY article_id,claim_id
        """
    ):
        content_hints[int(row["article_id"])].append(row["value_normalized"])

    evidence_rows: List[Tuple[Any, ...]] = []
    resolution_rows: List[Tuple[Any, ...]] = []
    resolved_at = utc_now()
    counts: Dict[str, int] = defaultdict(int)
    for article in conn.execute(
        """
        SELECT article_id,name_raw,slug
        FROM source_articles
        ORDER BY article_id
        """
    ):
        article_id = int(article["article_id"])
        evidence = infer_article_kind_evidence(
            article["name_raw"],
            article["slug"],
            tags.get(article_id, ()),
            content_hints.get(article_id, ()),
        )
        for item in evidence:
            if item.evidence_family.startswith("divisare.taxonomy"):
                evidence_kind = "source_tag"
            elif item.evidence_family == "divisare.content_hint":
                evidence_kind = "content_hint"
            elif item.evidence_family == "divisare.title_lexical":
                evidence_kind = "title_lexical"
            else:
                evidence_kind = "metadata_rule"
            evidence_rows.append(
                (
                    article_id,
                    item.kind,
                    item.confidence,
                    evidence_kind,
                    item.evidence_family,
                    independence_key_for_claim(article_id, evidence_kind),
                    item.source_ref,
                    "strong" if item.is_strong else "candidate",
                    item.reason,
                    ARTICLE_KIND_POLICY_VERSION,
                )
            )

        resolution = resolve_article_kind(evidence)
        if resolution.status == "unresolved":
            article_kind = "unresolved"
            confidence = 0.0
            method = "no_article_kind_evidence"
        elif resolution.status == "ambiguous":
            article_kind = "mixed_feature"
            confidence = resolution.confidence
            method = "conflicting_metadata_signals"
        else:
            article_kind = resolution.kind or "project"
            confidence = resolution.confidence
            method = (
                "html_explicit_or_manual"
                if resolution.status == "confirmed"
                else "metadata_candidate"
            )
        resolution_rows.append(
            (
                article_id,
                article_kind,
                resolution.status,
                confidence,
                method,
                resolution.evidence_count,
                json_dumps(list(resolution.evidence_families)),
                json_dumps(
                    [
                        {"kind": kind, "confidence": score}
                        for kind, score in resolution.ranked_kinds
                    ]
                ),
                ARTICLE_KIND_POLICY_VERSION,
                resolved_at,
            )
        )
        counts[resolution.status] += 1
        counts["kind:%s" % article_kind] += 1

    conn.executemany(
        """
        INSERT INTO article_kind_evidence_v2(
            article_id,proposed_kind,confidence,evidence_kind,evidence_family,
            independence_key,source_ref,status,reason,policy_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        evidence_rows,
    )
    conn.executemany(
        """
        INSERT INTO article_kind_resolution_v2(
            article_id,article_kind,status,confidence,method,evidence_count,
            evidence_families_json,ranked_kinds_json,policy_version,resolved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        resolution_rows,
    )
    counts["evidence_rows"] = len(evidence_rows)
    counts["resolution_rows"] = len(resolution_rows)
    return dict(counts)


def load_review_decisions(
    path: Optional[Path],
) -> Tuple[str, Dict[Tuple[int, int], Dict[str, Any]], Optional[str]]:
    if path is None:
        return "no-manual-decisions", {}, None
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    raw_payload = resolved.read_bytes()
    payload = json.loads(raw_payload.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("decision file must contain a JSON object")
    schema_version = int(payload.get("schema_version", DECISION_SCHEMA_VERSION))
    if schema_version != DECISION_SCHEMA_VERSION:
        raise ValueError(
            "unsupported decision schema_version: %s" % schema_version
        )
    version = str(payload.get("version") or "").strip()
    if not version:
        raise ValueError("decision file requires a non-empty version")
    items = payload.get("decisions")
    if not isinstance(items, list):
        raise ValueError("decision file requires a decisions array")

    decisions: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError("decision %d must be an object" % index)
        left = int(item["article_id_a"])
        right = int(item["article_id_b"])
        if left == right:
            raise ValueError("decision pair cannot contain the same article")
        pair = (min(left, right), max(left, right))
        action = str(item.get("decision") or "").strip().casefold()
        if action not in {"merge", "reject", "defer"}:
            raise ValueError(
                "decision %s has unsupported action %r" % (pair, action)
            )
        if pair in decisions:
            raise ValueError("duplicate decision pair: %s" % (pair,))
        approved = item.get("approved") is True
        reviewer = str(item.get("reviewer") or "").strip()
        reviewed_at = str(item.get("reviewed_at") or "").strip()
        if action == "merge" and not approved:
            raise ValueError(
                "merge decision %s requires approved=true" % (pair,)
            )
        if action == "merge" and (not reviewer or not reviewed_at):
            raise ValueError(
                "merge decision %s requires reviewer and reviewed_at" % (pair,)
            )
        reason = item.get("reason", {})
        if not isinstance(reason, (Mapping, list)):
            reason = {"note": str(reason)}
        if action == "merge" and not reason:
            raise ValueError(
                "merge decision %s requires a non-empty reason" % (pair,)
            )
        decision_id = str(
            item.get("decision_id")
            or "%s:%s:%s" % (version, pair[0], pair[1])
        ).strip()
        if not decision_id:
            raise ValueError("decision %s requires a decision_id" % (pair,))
        decisions[pair] = {
            "decision_id": decision_id,
            "decision": action,
            "reason": reason,
            "approved": approved,
            "reviewer": reviewer or None,
            "reviewed_at": reviewed_at or None,
        }
    return version, decisions, hashlib.sha256(raw_payload).hexdigest()


def populate_match_reviews_and_redirects(
    conn: sqlite3.Connection,
    *,
    decision_version: str,
    decisions: Mapping[Tuple[int, int], Mapping[str, Any]],
) -> Dict[str, int]:
    article_to_building = {
        int(row["article_id"]): row["building_id"]
        for row in conn.execute(
            "SELECT article_id,building_id FROM building_articles"
        )
    }
    article_kinds = {
        int(row["article_id"]): {
            "kind": row["article_kind"],
            "status": row["status"],
            "confidence": row["confidence"],
        }
        for row in conn.execute(
            """
            SELECT article_id,article_kind,status,confidence
            FROM article_kind_resolution_v2
            """
        )
    }
    candidate_rows = list(
        conn.execute(
            """
            SELECT *
            FROM article_match_candidates
            ORDER BY article_id_a,article_id_b
            """
        )
    )
    candidate_pairs = {
        (int(row["article_id_a"]), int(row["article_id_b"]))
        for row in candidate_rows
    }
    unknown = sorted(set(decisions) - candidate_pairs)
    if unknown:
        raise ValueError(
            "decision file contains pairs outside the D2 candidate set: %s"
            % unknown[:10]
        )

    review_rows: List[Tuple[Any, ...]] = []
    merge_building_pairs: List[Tuple[str, str, Tuple[int, int]]] = []
    rejected_pairs: List[Tuple[str, str, Tuple[int, int]]] = []
    counts: Dict[str, int] = defaultdict(int)
    now = utc_now()
    for row in candidate_rows:
        left = int(row["article_id_a"])
        right = int(row["article_id_b"])
        pair = (left, right)
        building_left = article_to_building[left]
        building_right = article_to_building[right]
        manual = decisions.get(pair)

        if manual is not None:
            action = str(manual["decision"])
            if action == "merge":
                status = "confirmed"
                recommendation = "merge"
                if building_left != building_right:
                    merge_building_pairs.append(
                        (building_left, building_right, pair)
                    )
            elif action == "reject":
                status = "rejected"
                recommendation = "keep_separate"
                rejected_pairs.append((building_left, building_right, pair))
            else:
                status = "deferred"
                recommendation = "review_later"
            source = "versioned_manual_decision"
            reason = {
                "manual": manual,
                "source_candidate_kind": row["candidate_kind"],
            }
            decided_at = manual.get("reviewed_at") or now
            decision_id = str(manual["decision_id"])
        elif row["status"] == "auto_clustered":
            status = "confirmed"
            recommendation = "keep_merged"
            source = "v1_strict_metadata_cluster"
            reason = {
                "policy": "exact normalized name, architect, location, and year"
            }
            decided_at = now
            decision_id = "v1-strict:%s:%s" % pair
        else:
            status = "pending"
            recommendation = "manual_metadata_review"
            source = "unreviewed_v1_candidate"
            reason = {"policy": "no automatic merge beyond v1 strict rules"}
            decided_at = None
            decision_id = "v1-open:%s:%s" % pair

        review_rows.append(
            (
                left,
                right,
                row["candidate_kind"],
                row["score"],
                row["status"],
                row["signals_json"],
                building_left,
                building_right,
                status,
                decision_id,
                recommendation,
                source,
                json_dumps(reason),
                json_dumps(
                    {
                        str(left): article_kinds[left],
                        str(right): article_kinds[right],
                    }
                ),
                decision_version,
                decided_at,
            )
        )
        counts["review:%s" % status] += 1

    conn.executemany(
        """
        INSERT INTO article_match_reviews_v2(
            article_id_a,article_id_b,source_candidate_kind,source_score,
            source_status,source_signals_json,building_id_a,building_id_b,
            decision_status,decision_id,recommendation,decision_source,
            decision_reason_json,article_kind_context_json,decision_version,
            decided_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        review_rows,
    )

    all_buildings = [
        row["building_id"]
        for row in conn.execute("SELECT building_id FROM buildings")
    ]
    union_find = UnionFind(all_buildings)
    for building_left, building_right, _pair in merge_building_pairs:
        union_find.union(building_left, building_right)
    for building_left, building_right, pair in rejected_pairs:
        if union_find.find(building_left) == union_find.find(building_right):
            raise ValueError(
                "manual decisions both merge and reject building component: %s"
                % (pair,)
            )

    component_members: Dict[str, List[str]] = defaultdict(list)
    for building_id in all_buildings:
        component_members[union_find.find(building_id)].append(building_id)
    redirect_rows: List[Tuple[Any, ...]] = []
    for members in component_members.values():
        if len(members) < 2:
            continue
        target = min(members)
        component_pairs = [
            pair
            for left, right, pair in merge_building_pairs
            if left in members and right in members
        ]
        component_decisions = [
            decisions[pair]
            for pair in component_pairs
        ]
        for source in sorted(members):
            if source == target:
                continue
            redirect_rows.append(
                (
                    source,
                    target,
                    decision_version,
                    json_dumps(
                        [
                            decision["decision_id"]
                            for decision in component_decisions
                        ]
                    ),
                    json_dumps(
                        {
                            "decision_pairs": [
                                [pair[0], pair[1]] for pair in component_pairs
                            ],
                            "survivor_policy": "minimum_stable_building_id",
                            "approvals": component_decisions,
                        }
                    ),
                    now,
                )
            )
    conn.executemany(
        """
        INSERT INTO building_redirects_v2(
            source_building_id,target_building_id,decision_version,
            decision_ids_json,reason_json,created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        redirect_rows,
    )
    counts["review_rows"] = len(review_rows)
    counts["manual_decisions"] = len(decisions)
    counts["redirects"] = len(redirect_rows)
    return dict(counts)


def materialize_active_membership(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        INSERT INTO active_building_membership_v2(
            article_id,building_id,source_building_id,source_article_role,
            membership_confidence,decision_method
        )
        SELECT
            ba.article_id,
            COALESCE(r.target_building_id,ba.building_id),
            ba.building_id,
            ba.article_role,
            ba.membership_confidence,
            ba.decision_method
        FROM building_articles ba
        LEFT JOIN building_redirects_v2 r
          ON r.source_building_id=ba.building_id
        ORDER BY ba.article_id
        """
    )
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM active_building_membership_v2"
        ).fetchone()[0]
    )


def materialize_building_images(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        INSERT INTO building_images_materialized_v2(
            building_id,asset_key,representative_url,role_rank,first_position
        )
        WITH ranked AS (
          SELECT
              va.building_id,
              aio.asset_key,
              iu.url AS representative_url,
              CASE aio.role WHEN 'cover' THEN 0 ELSE 1 END AS role_rank,
              aio.position AS first_position,
              ROW_NUMBER() OVER (
                PARTITION BY va.building_id,aio.asset_key
                ORDER BY
                  CASE aio.role WHEN 'cover' THEN 0 ELSE 1 END,
                  aio.position,
                  iu.url_id
              ) AS rn
          FROM active_building_membership_v2 va
          JOIN article_image_occurrences aio ON aio.article_id=va.article_id
          JOIN image_urls iu ON iu.url_id=aio.url_id
        )
        SELECT
            building_id,asset_key,representative_url,role_rank,first_position
        FROM ranked
        WHERE rn=1
        """
    )
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM building_images_materialized_v2"
        ).fetchone()[0]
    )


def populate_facets_v2(conn: sqlite3.Connection) -> Dict[str, int]:
    previous_facets = {
        (row["building_id"], row["axis"], row["value"]): (
            int(row["facet_id"]),
            row["status"],
        )
        for row in conn.execute(
            """
            SELECT facet_id,building_id,axis,value,status
            FROM building_facets
            """
        )
    }
    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in EXCLUDED_FACET_AXES)
    query = """
        SELECT
            va.building_id,
            c.claim_id,
            c.article_id,
            c.axis,
            c.value_normalized AS value,
            c.confidence,
            c.source_ref,
            c.search_tier,
            COALESCE(json_extract(c.details_json,'$.priority'),0) AS priority,
            ce.mapping_kind,
            ce.evidence_family,
            ce.independence_key
        FROM v_active_building_articles_v2 va
        JOIN attribute_claims c ON c.article_id=va.article_id
        JOIN claim_evidence_v2 ce ON ce.claim_id=c.claim_id
        WHERE c.scope='building'
          AND c.polarity='positive'
          AND c.axis NOT IN (%s)
          AND ce.mapping_kind IN ('direct','supporting')
        ORDER BY va.building_id,c.axis,c.value_normalized,c.claim_id
    """ % placeholders
    for row in conn.execute(query, EXCLUDED_FACET_AXES):
        key = (row["building_id"], row["axis"], row["value"])
        group = groups.get(key)
        if group is None:
            group = {
                "claims": [],
                "articles": set(),
                "direct_confidences": [],
                "supporting_keys": set(),
                "supporting_articles": set(),
                "all_keys": set(),
                "families": set(),
                "sources": set(),
                "confidence": 0.0,
                "max_priority": 0,
                "search_tier": "hidden",
                "direct_count": 0,
                "supporting_count": 0,
            }
            groups[key] = group
        mapping_kind = row["mapping_kind"]
        confidence = float(row["confidence"])
        group["claims"].append(
            (
                int(row["claim_id"]),
                confidence,
                row["evidence_family"],
                row["independence_key"],
            )
        )
        group["articles"].add(int(row["article_id"]))
        group["all_keys"].add(row["independence_key"])
        group["families"].add(row["evidence_family"])
        group["sources"].add(row["source_ref"] or "claim:%s" % row["claim_id"])
        group["confidence"] = max(group["confidence"], confidence)
        group["max_priority"] = max(
            group["max_priority"],
            int(row["priority"] or 0),
        )
        tier = row["search_tier"]
        if SEARCH_TIER_RANK[tier] > SEARCH_TIER_RANK[group["search_tier"]]:
            group["search_tier"] = tier
        if mapping_kind == "direct":
            group["direct_count"] += 1
            group["direct_confidences"].append(confidence)
        else:
            group["supporting_count"] += 1
            group["supporting_keys"].add(row["independence_key"])
            group["supporting_articles"].add(int(row["article_id"]))

    facet_rows: List[Tuple[Any, ...]] = []
    statuses: Dict[Tuple[str, str, str], str] = {}
    downgraded = 0
    upgraded = 0
    for key in sorted(groups):
        building_id, axis, value = key
        group = groups[key]
        status = facet_status_v2(
            group["direct_confidences"],
            group["supporting_keys"],
            group["confidence"],
            supporting_article_count=len(group["supporting_articles"]),
        )
        previous = previous_facets.get(key)
        source_facet_id = previous[0] if previous else None
        previous_status = previous[1] if previous else None
        changed = int(previous_status is not None and previous_status != status)
        if previous_status == "confirmed" and status == "candidate":
            downgraded += 1
        elif previous_status == "candidate" and status == "confirmed":
            upgraded += 1
        statuses[key] = status
        facet_rows.append(
            (
                source_facet_id,
                building_id,
                axis,
                value,
                status,
                "facet",
                group["confidence"],
                len(group["claims"]),
                len(group["articles"]),
                group["direct_count"],
                group["supporting_count"],
                len(group["sources"]),
                len(group["families"]),
                len(group["all_keys"]),
                group["max_priority"],
                group["search_tier"],
                FACET_POLICY_VERSION,
                previous_status,
                changed,
            )
        )
    conn.executemany(
        """
        INSERT INTO building_facets_v2(
            source_facet_id,building_id,axis,value,status,role,confidence,
            claim_count,article_count,direct_claim_count,
            supporting_claim_count,source_count,evidence_family_count,
            independence_group_count,max_priority,search_tier,
            resolver_version,previous_status,status_changed
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        facet_rows,
    )

    facet_ids = {
        (row["building_id"], row["axis"], row["value"]): int(row["facet_v2_id"])
        for row in conn.execute(
            "SELECT facet_v2_id,building_id,axis,value FROM building_facets_v2"
        )
    }
    link_rows: List[Tuple[Any, ...]] = []
    for key, group in groups.items():
        facet_id = facet_ids[key]
        for claim_id, weight, family, independence_key in group["claims"]:
            link_rows.append(
                (facet_id, claim_id, weight, family, independence_key)
            )
    conn.executemany(
        """
        INSERT INTO building_facet_claims_v2(
            facet_v2_id,claim_id,weight,evidence_family,independence_key
        ) VALUES (?,?,?,?,?)
        """,
        link_rows,
    )

    # Program and typology are canonical multi-value axes in v2. A scalar
    # compatibility primary exists only when exactly one value is confirmed.
    multi_axes = ("program", "typology")
    conn.execute(
        """
        UPDATE building_facets_v2
        SET role='secondary'
        WHERE status='confirmed' AND axis IN ('program','typology')
        """
    )
    for row in conn.execute(
        """
        SELECT building_id,axis,MIN(facet_v2_id) AS facet_v2_id,COUNT(*) AS n
        FROM building_facets_v2
        WHERE status='confirmed' AND axis IN ('program','typology')
        GROUP BY building_id,axis
        HAVING COUNT(*)=1
        """
    ):
        conn.execute(
            "UPDATE building_facets_v2 SET role='primary' WHERE facet_v2_id=?",
            (row["facet_v2_id"],),
        )

    scalar_conflicts = 0
    scalar_placeholders = ",".join("?" for _ in SCALAR_AXES)
    conn.execute(
        """
        UPDATE building_facets_v2
        SET role='secondary'
        WHERE status='confirmed' AND axis IN (%s)
        """
        % scalar_placeholders,
        SCALAR_AXES,
    )
    scalar_groups: Dict[Tuple[str, str], List[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT *
        FROM building_facets_v2
        WHERE status='confirmed' AND axis IN (%s)
        ORDER BY
          building_id,axis,direct_claim_count DESC,confidence DESC,
          independence_group_count DESC,max_priority DESC,claim_count DESC,value
        """
        % scalar_placeholders,
        SCALAR_AXES,
    ):
        scalar_groups[(row["building_id"], row["axis"])].append(row)
    primary_ids: List[Tuple[int]] = []
    for candidates in scalar_groups.values():
        direct = [
            row for row in candidates if int(row["direct_claim_count"]) > 0
        ]
        if len(direct) == 1:
            selected = direct[0]
        elif len(direct) > 1:
            scalar_conflicts += 1
            continue
        elif len(candidates) == 1:
            selected = candidates[0]
        else:
            scalar_conflicts += 1
            continue
        primary_ids.append((int(selected["facet_v2_id"]),))
    conn.executemany(
        "UPDATE building_facets_v2 SET role='primary' WHERE facet_v2_id=?",
        primary_ids,
    )

    return {
        "facet_rows": len(facet_rows),
        "facet_claim_links": len(link_rows),
        "confirmed_facets": sum(
            1 for status in statuses.values() if status == "confirmed"
        ),
        "candidate_facets": sum(
            1 for status in statuses.values() if status == "candidate"
        ),
        "facets_downgraded": downgraded,
        "facets_upgraded": upgraded,
        "scalar_conflicts": scalar_conflicts,
    }


def populate_building_article_roles(conn: sqlite3.Connection) -> int:
    primary_by_building = {
        row["building_id"]: int(row["primary_article_id"])
        for row in conn.execute(
            """
            SELECT building_id,primary_article_id
            FROM building_attributes_v2
            WHERE is_active=1
              AND primary_article_id IS NOT NULL
            """
        )
    }
    rows: List[Tuple[Any, ...]] = []
    for row in conn.execute(
        """
        SELECT
            va.article_id,va.building_id,va.source_building_id,
            ak.article_kind,ak.status,ak.confidence,ak.method
        FROM v_active_building_articles_v2 va
        JOIN article_kind_resolution_v2 ak ON ak.article_id=va.article_id
        ORDER BY va.building_id,va.article_id
        """
    ):
        article_id = int(row["article_id"])
        if article_id == primary_by_building[row["building_id"]]:
            role = "primary"
            method = "stable_survivor_primary"
        elif (
            row["article_kind"] != "project"
            and row["status"] == "confirmed"
        ):
            role = row["article_kind"]
            method = "confirmed_article_kind"
        else:
            role = "supporting_project"
            method = (
                "unconfirmed_article_kind_not_promoted"
                if row["article_kind"] != "project"
                else "cluster_membership"
            )
        rows.append(
            (
                article_id,
                row["building_id"],
                row["source_building_id"],
                role,
                row["article_kind"],
                row["status"],
                row["confidence"],
                method,
                ARTICLE_KIND_POLICY_VERSION,
            )
        )
    conn.executemany(
        """
        INSERT INTO building_article_roles_v2(
            article_id,building_id,source_building_id,article_role,
            article_kind,article_kind_status,role_confidence,
            decision_method,policy_version
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    return len(rows)


def populate_building_attributes(conn: sqlite3.Connection) -> Dict[str, int]:
    redirects = {
        row["source_building_id"]: row["target_building_id"]
        for row in conn.execute(
            "SELECT source_building_id,target_building_id FROM building_redirects_v2"
        )
    }
    article_counts = {
        row["building_id"]: int(row["n"])
        for row in conn.execute(
            """
            SELECT building_id,COUNT(*) AS n
            FROM v_active_building_articles_v2
            GROUP BY building_id
            """
        )
    }
    active_members: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT
            va.building_id,va.source_building_id,
            a.article_id,a.name_raw,a.name_normalized,
            a.location_country,a.location_city,a.project_year,a.area_sqm,
            a.description_quality,a.content_score,a.image_count,a.tag_count
        FROM active_building_membership_v2 va
        JOIN source_articles a ON a.article_id=va.article_id
        ORDER BY va.building_id,a.article_id
        """
    ):
        active_members[row["building_id"]].append(row)
    clean_text_ids = {
        int(row["article_id"]): int(row["text_id"])
        for row in conn.execute(
            """
            SELECT article_id,text_id
            FROM article_text_versions
            WHERE text_kind='clean_description' AND is_current=1
            """
        )
    }
    facets: Dict[Tuple[str, str], List[sqlite3.Row]] = defaultdict(list)
    primary: Dict[Tuple[str, str], sqlite3.Row] = {}
    for row in conn.execute(
        """
        SELECT *
        FROM building_facets_v2
        WHERE status='confirmed'
        ORDER BY building_id,axis,value
        """
    ):
        key = (row["building_id"], row["axis"])
        facets[key].append(row)
        if row["role"] == "primary":
            primary[key] = row

    kind_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in conn.execute(
        """
        SELECT
            va.building_id,ak.article_kind,ak.status,COUNT(*) AS n
        FROM v_active_building_articles_v2 va
        JOIN article_kind_resolution_v2 ak ON ak.article_id=va.article_id
        GROUP BY va.building_id,ak.article_kind,ak.status
        """
    ):
        kind_counts[row["building_id"]][
            "%s:%s" % (row["article_kind"], row["status"])
        ] = int(row["n"])

    review_buildings = {
        row["building_id"]
        for row in conn.execute(
            """
            SELECT DISTINCT va.building_id
            FROM article_match_reviews_v2 mr
            JOIN v_active_building_articles_v2 va
              ON va.article_id=mr.article_id_a OR va.article_id=mr.article_id_b
            WHERE mr.decision_status IN ('pending','deferred')
            UNION
            SELECT DISTINCT va.building_id
            FROM article_kind_resolution_v2 ak
            JOIN v_active_building_articles_v2 va ON va.article_id=ak.article_id
            WHERE ak.status='ambiguous'
            """
        )
    }
    scalar_conflicts_by_building: Dict[str, Dict[str, List[str]]] = defaultdict(
        dict
    )
    for (facet_building, axis), axis_rows in facets.items():
        if (
            axis in SCALAR_AXES
            and len(axis_rows) > 1
            and (facet_building, axis) not in primary
        ):
            scalar_conflicts_by_building[facet_building][axis] = [
                row["value"] for row in axis_rows
            ]

    rows: List[Tuple[Any, ...]] = []
    multi_program = 0
    multi_typology = 0
    active_buildings = 0
    core_conflict_buildings = 0
    now = utc_now()
    for building in conn.execute(
        "SELECT * FROM buildings ORDER BY building_id"
    ):
        building_id = building["building_id"]
        redirect_to = redirects.get(building_id)
        is_active = int(redirect_to is None)
        if is_active:
            active_buildings += 1
        programs = [
            row["value"] for row in facets.get((building_id, "program"), ())
        ]
        typologies = [
            row["value"] for row in facets.get((building_id, "typology"), ())
        ]
        program_primary = primary.get((building_id, "program"))
        typology_primary = primary.get((building_id, "typology"))
        if len(programs) > 1:
            multi_program += 1
        if len(typologies) > 1:
            multi_typology += 1

        scalar_values = {
            axis: (
                primary[(building_id, axis)]["value"]
                if (building_id, axis) in primary
                else None
            )
            for axis in SCALAR_AXES
        }
        facet_conflicts = scalar_conflicts_by_building.get(building_id, {})
        members = active_members.get(building_id, [])
        source_building_ids = {
            row["source_building_id"] for row in members
        }
        is_manual_merge_target = is_active and len(source_building_ids) > 1
        core_conflicts: Dict[str, Any] = {}
        if is_manual_merge_target:
            primary_member = sorted(
                members,
                key=lambda row: (
                    -(row["description_quality"] != "missing"),
                    -float(row["content_score"] or 0.0),
                    -int(row["image_count"] or 0),
                    -int(row["tag_count"] or 0),
                    int(row["article_id"]),
                ),
            )[0]
            primary_article_id = int(primary_member["article_id"])
            name = primary_member["name_raw"]
            name_normalized = primary_member["name_normalized"]
            names_by_normalized: Dict[str, str] = {}
            for member in members:
                names_by_normalized.setdefault(
                    member["name_normalized"],
                    member["name_raw"],
                )
            if len(names_by_normalized) > 1:
                core_conflicts["name"] = sorted(names_by_normalized.values())

            def display_consensus(column: str) -> Optional[str]:
                displays: Dict[str, str] = {}
                for member in members:
                    value = member[column]
                    if value is not None and str(value).strip():
                        displays.setdefault(str(value).casefold(), str(value))
                if len(displays) > 1:
                    core_conflicts[column] = sorted(displays.values())
                    return None
                return next(iter(displays.values()), None)

            location_country = display_consensus("location_country")
            location_city = display_consensus("location_city")
            if "location_country" in core_conflicts or "location_city" in core_conflicts:
                location_method = "manual_merge_conflict_abstained"
                location_confidence = 0.0
            elif location_country is None and location_city is None:
                location_method = "unresolved"
                location_confidence = 0.0
            elif location_country is None or location_city is None:
                location_method = "manual_merge_partial_consensus"
                location_confidence = 0.8
            else:
                location_method = "manual_merge_member_consensus"
                location_confidence = 0.95

            years = sorted(
                {
                    int(member["project_year"])
                    for member in members
                    if member["project_year"] is not None
                }
            )
            if len(years) == 1:
                project_year = years[0]
                year_kind = "manual_merge_consensus"
            elif not years:
                project_year = None
                year_kind = "unknown"
            else:
                project_year = None
                year_kind = "conflict_abstained"
                core_conflicts["project_year"] = years

            areas = sorted(
                {
                    float(member["area_sqm"])
                    for member in members
                    if member["area_sqm"] is not None
                }
            )
            if len(areas) == 1:
                area_sqm = areas[0]
            elif not areas:
                area_sqm = None
            else:
                area_sqm = None
                core_conflicts["area_sqm"] = areas
            description_text_id = clean_text_ids.get(primary_article_id)
        else:
            primary_article_id = int(building["primary_article_id"])
            name = building["name"]
            name_normalized = building["name_normalized"]
            location_country = building["location_country"]
            location_city = building["location_city"]
            location_method = building["location_resolution_method"]
            location_confidence = float(building["location_confidence"])
            project_year = building["project_year"]
            year_kind = building["year_kind"]
            area_sqm = building["area_sqm"]
            description_text_id = building["description_text_id"]

        if core_conflicts:
            core_conflict_buildings += 1
        needs_review = int(
            bool(building["needs_review"])
            or building_id in review_buildings
            or bool(facet_conflicts)
            or bool(core_conflicts)
        )
        rows.append(
            (
                building_id,
                is_active,
                redirect_to,
                article_counts.get(building_id, 0),
                primary_article_id,
                name,
                name_normalized,
                location_country,
                location_city,
                location_method,
                location_confidence,
                project_year,
                year_kind,
                area_sqm,
                description_text_id,
                json_dumps(core_conflicts),
                json_dumps(programs),
                program_primary["value"] if program_primary else None,
                (
                    float(program_primary["confidence"])
                    if program_primary
                    else None
                ),
                int(len(programs) > 1),
                json_dumps(typologies),
                typology_primary["value"] if typology_primary else None,
                (
                    float(typology_primary["confidence"])
                    if typology_primary
                    else None
                ),
                int(len(typologies) > 1),
                scalar_values["style"],
                scalar_values["structural_system"],
                scalar_values["roof_type"],
                scalar_values["facade_pattern"],
                scalar_values["facade_system"],
                json_dumps(dict(kind_counts.get(building_id, {}))),
                json_dumps(facet_conflicts),
                needs_review,
                FACET_POLICY_VERSION,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO building_attributes_v2(
            building_id,is_active,redirect_to,article_count,
            primary_article_id,name,name_normalized,location_country,
            location_city,location_resolution_method,location_confidence,
            project_year,year_kind,area_sqm,description_text_id,
            core_conflicts_json,programs_json,
            program_primary,program_confidence,mixed_use,typologies_json,
            typology_primary,typology_confidence,multi_typology,style,
            structural_system,roof_type,facade_pattern,facade_system,
            article_kind_counts_json,facet_conflicts_json,
            metadata_needs_review,resolution_version,resolved_at
        ) VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        rows,
    )
    return {
        "attribute_rows": len(rows),
        "active_buildings": active_buildings,
        "multi_program_buildings": multi_program,
        "multi_typology_buildings": multi_typology,
        "core_conflict_buildings": core_conflict_buildings,
    }


def populate_recrawl_queue(conn: sqlite3.Connection) -> int:
    rows: List[Tuple[Any, ...]] = []
    now = utc_now()
    for row in conn.execute(
        """
        SELECT
            a.article_id,a.source_url,a.area_sqm,a.description_quality,
            ak.status AS article_kind_status
        FROM source_articles a
        JOIN article_kind_resolution_v2 ak ON ak.article_id=a.article_id
        ORDER BY a.article_id
        """
    ):
        reasons: List[str] = []
        priority = 10
        if row["area_sqm"] is None:
            reasons.append("area_missing")
            priority += 20
        if row["description_quality"] == "missing":
            reasons.append("description_missing")
            priority += 80
        elif row["description_quality"] != "clean":
            reasons.append("description_dom_reparse")
            priority += 40
        if row["article_kind_status"] in {"candidate", "ambiguous"}:
            reasons.append("article_kind_confirmation")
            priority += 30
        rows.append(
            (
                row["article_id"],
                row["source_url"],
                priority,
                json_dumps(reasons),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO article_recrawl_queue_v2(
            article_id,source_url,priority,reasons_json,queued_at
        ) VALUES (?,?,?,?,?)
        """,
        rows,
    )
    return len(rows)


VIEWS_SQL = """
CREATE VIEW v_article_kind_review_queue_v2 AS
SELECT
    ak.article_id,
    a.source_url,
    a.name_raw,
    a.slug,
    ak.article_kind,
    ak.status,
    ak.confidence,
    ak.method,
    ak.evidence_count,
    ak.evidence_families_json,
    ak.ranked_kinds_json
FROM article_kind_resolution_v2 ak
JOIN source_articles a ON a.article_id=ak.article_id
WHERE ak.status IN ('candidate','ambiguous')
ORDER BY
  CASE ak.status WHEN 'ambiguous' THEN 0 ELSE 1 END,
  ak.confidence DESC,
  ak.article_id;

CREATE VIEW v_metadata_d2_review_queue_v2 AS
SELECT
    mr.article_id_a,
    a.source_url AS source_url_a,
    a.name_raw AS name_a,
    ka.article_kind AS article_kind_a,
    ka.status AS article_kind_status_a,
    mr.article_id_b,
    b.source_url AS source_url_b,
    b.name_raw AS name_b,
    kb.article_kind AS article_kind_b,
    kb.status AS article_kind_status_b,
    mr.source_candidate_kind,
    mr.source_score,
    mr.source_signals_json,
    mr.building_id_a,
    mr.building_id_b,
    mr.decision_status,
    mr.recommendation
FROM article_match_reviews_v2 mr
JOIN source_articles a ON a.article_id=mr.article_id_a
JOIN source_articles b ON b.article_id=mr.article_id_b
JOIN article_kind_resolution_v2 ka ON ka.article_id=mr.article_id_a
JOIN article_kind_resolution_v2 kb ON kb.article_id=mr.article_id_b
WHERE mr.decision_status IN ('pending','deferred')
ORDER BY mr.source_score DESC,mr.article_id_a,mr.article_id_b;

CREATE VIEW v_search_facets_v2 AS
SELECT
    building_id,axis,value,status,role,confidence,search_tier,
    evidence_family_count,independence_group_count
FROM building_facets_v2
WHERE search_tier <> 'hidden'
  AND status IN ('confirmed','candidate');

CREATE VIEW v_building_images_v2 AS
SELECT
    building_id,asset_key,representative_url,role_rank,first_position
FROM building_images_materialized_v2;

CREATE VIEW v_metadata_recrawl_queue_v2 AS
SELECT
    q.article_id,
    q.source_url,
    a.name_raw,
    q.priority,
    q.reasons_json,
    ak.article_kind,
    ak.status AS article_kind_status,
    a.description_quality,
    a.area_sqm,
    q.initial_fetch_status,
    q.initial_parse_status
FROM article_recrawl_queue_v2 q
JOIN source_articles a ON a.article_id=q.article_id
JOIN article_kind_resolution_v2 ak ON ak.article_id=q.article_id
ORDER BY q.priority DESC,q.article_id;

CREATE VIEW v_divisare_buildings_export_v2 AS
SELECT
    b.building_id AS canonical_bld_id,
    attrs.primary_article_id AS primary_divisare_id,
    json_object(
      'divisare',
      json((
        SELECT json_group_array(article_id)
        FROM (
          SELECT va2.article_id
          FROM v_active_building_articles_v2 va2
          WHERE va2.building_id=b.building_id
          ORDER BY va2.article_id
        )
      ))
    ) AS source_refs,
    attrs.name,
    attrs.location_city,
    attrs.location_country,
    attrs.location_resolution_method,
    attrs.location_confidence,
    attrs.project_year,
    attrs.year_kind,
    attrs.area_sqm,
    COALESCE((
      SELECT json_group_array(architect_id)
      FROM (
        SELECT DISTINCT aa.architect_id
        FROM v_active_building_articles_v2 va3
        JOIN article_architects aa ON aa.article_id=va3.article_id
        WHERE va3.building_id=b.building_id
          AND aa.architect_id IS NOT NULL
        ORDER BY aa.architect_id
      )
    ), '[]') AS architect_canonical_ids,
    COALESCE((
      SELECT json_group_array(architect_name)
      FROM (
        SELECT DISTINCT aa.architect_name
        FROM v_active_building_articles_v2 va4
        JOIN article_architects aa ON aa.article_id=va4.article_id
        WHERE va4.building_id=b.building_id
          AND aa.architect_name IS NOT NULL
        ORDER BY aa.architect_name
      )
    ), '[]') AS architect_names,
    attrs.program_primary AS program,
    json(attrs.programs_json) AS programs,
    attrs.mixed_use,
    attrs.typology_primary,
    json(attrs.typologies_json) AS typology_tags,
    attrs.multi_typology,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets_v2 f
        WHERE f.building_id=b.building_id
          AND f.axis='material'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC,f.value
      )
    ), '[]') AS material_visual,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets_v2 f
        WHERE f.building_id=b.building_id
          AND f.axis='color'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC,f.value
      )
    ), '[]') AS colors,
    attrs.style,
    attrs.structural_system,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets_v2 f
        WHERE f.building_id=b.building_id
          AND f.axis='facade_material'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC,f.value
      )
    ), '[]') AS facade_materials,
    attrs.facade_pattern,
    attrs.facade_system,
    attrs.roof_type,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets_v2 f
        WHERE f.building_id=b.building_id
          AND f.axis='architectural_element'
          AND f.status='confirmed'
          AND f.search_tier <> 'hidden'
        ORDER BY f.confidence DESC,f.value
      )
    ), '[]') AS architectural_elements,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets_v2 f
        WHERE f.building_id=b.building_id
          AND f.axis='site_context'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC,f.value
      )
    ), '[]') AS site_contexts,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets_v2 f
        WHERE f.building_id=b.building_id
          AND f.axis='intervention_type'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC,f.value
      )
    ), '[]') AS intervention_types,
    COALESCE((
      SELECT json_group_array(tag_slug)
      FROM (
        SELECT DISTINCT at.tag_slug
        FROM v_active_building_articles_v2 va5
        JOIN article_tags at ON at.article_id=va5.article_id
        WHERE va5.building_id=b.building_id
        ORDER BY at.tag_slug
      )
    ), '[]') AS source_categories,
    (
      SELECT iu.url
      FROM article_image_occurrences aio
      JOIN image_urls iu ON iu.url_id=aio.url_id
      WHERE aio.article_id=attrs.primary_article_id AND aio.role='cover'
      ORDER BY aio.position
      LIMIT 1
    ) AS cover_image_url,
    COALESCE((
      SELECT json_group_array(representative_url)
      FROM (
        SELECT bi.representative_url
        FROM v_building_images_v2 bi
        WHERE bi.building_id=b.building_id
        ORDER BY bi.role_rank,bi.first_position,bi.asset_key
      )
    ), '[]') AS gallery_image_urls,
    tv.text AS description,
    pa.description_quality,
    pa.description_ui_markers,
    primary_kind.article_kind AS primary_article_kind,
    primary_kind.status AS primary_article_kind_status,
    attrs.article_kind_counts_json,
    b.cluster_confidence,
    attrs.metadata_needs_review AS needs_review,
    attrs.core_conflicts_json,
    attrs.facet_conflicts_json,
    'divisare-metadata-v2.1' AS metadata_version
FROM buildings b
JOIN building_attributes_v2 attrs ON attrs.building_id=b.building_id
JOIN source_articles pa ON pa.article_id=attrs.primary_article_id
JOIN article_kind_resolution_v2 primary_kind
  ON primary_kind.article_id=attrs.primary_article_id
LEFT JOIN article_text_versions tv ON tv.text_id=attrs.description_text_id
WHERE attrs.is_active=1;
"""


def collect_metrics(
    conn: sqlite3.Connection,
    extra: Mapping[str, Any],
) -> Dict[str, Any]:
    metrics: Dict[str, Any] = dict(extra)
    count_queries = {
        "articles": "SELECT COUNT(*) FROM source_articles",
        "buildings_parent": "SELECT COUNT(*) FROM buildings",
        "buildings_active": (
            "SELECT COUNT(*) FROM building_attributes_v2 WHERE is_active=1"
        ),
        "claims": "SELECT COUNT(*) FROM attribute_claims",
        "claim_evidence_rows": "SELECT COUNT(*) FROM claim_evidence_v2",
        "article_kind_evidence": (
            "SELECT COUNT(*) FROM article_kind_evidence_v2"
        ),
        "article_kind_review_queue": (
            "SELECT COUNT(*) FROM v_article_kind_review_queue_v2"
        ),
        "facets_v2": "SELECT COUNT(*) FROM building_facets_v2",
        "confirmed_facets_v2": (
            "SELECT COUNT(*) FROM building_facets_v2 WHERE status='confirmed'"
        ),
        "candidate_facets_v2": (
            "SELECT COUNT(*) FROM building_facets_v2 WHERE status='candidate'"
        ),
        "program_primary_v2": (
            "SELECT COUNT(*) FROM building_attributes_v2 "
            "WHERE is_active=1 AND program_primary IS NOT NULL"
        ),
        "typology_primary_v2": (
            "SELECT COUNT(*) FROM building_attributes_v2 "
            "WHERE is_active=1 AND typology_primary IS NOT NULL"
        ),
        "multi_program_buildings": (
            "SELECT COUNT(*) FROM building_attributes_v2 "
            "WHERE is_active=1 AND mixed_use=1"
        ),
        "multi_typology_buildings": (
            "SELECT COUNT(*) FROM building_attributes_v2 "
            "WHERE is_active=1 AND multi_typology=1"
        ),
        "d2_review_pending": (
            "SELECT COUNT(*) FROM article_match_reviews_v2 "
            "WHERE decision_status='pending'"
        ),
        "d2_confirmed": (
            "SELECT COUNT(*) FROM article_match_reviews_v2 "
            "WHERE decision_status='confirmed'"
        ),
        "redirects": "SELECT COUNT(*) FROM building_redirects_v2",
        "recrawl_queue": "SELECT COUNT(*) FROM article_recrawl_queue_v2",
        "image_assets_preserved": "SELECT COUNT(*) FROM image_assets",
        "phash_pending_preserved": (
            "SELECT COUNT(*) FROM image_hashes WHERE status='pending'"
        ),
    }
    for name, query in count_queries.items():
        metrics[name] = int(conn.execute(query).fetchone()[0])
    metrics["article_kind_status"] = {
        row["status"]: int(row["n"])
        for row in conn.execute(
            """
            SELECT status,COUNT(*) AS n
            FROM article_kind_resolution_v2
            GROUP BY status
            ORDER BY status
            """
        )
    }
    metrics["article_kind_values"] = {
        row["article_kind"]: int(row["n"])
        for row in conn.execute(
            """
            SELECT article_kind,COUNT(*) AS n
            FROM article_kind_resolution_v2
            GROUP BY article_kind
            ORDER BY article_kind
            """
        )
    }
    metrics["d2_source_status"] = {
        "%s:%s" % (row["source_status"], row["source_candidate_kind"]): int(
            row["n"]
        )
        for row in conn.execute(
            """
            SELECT source_status,source_candidate_kind,COUNT(*) AS n
            FROM article_match_reviews_v2
            GROUP BY source_status,source_candidate_kind
            ORDER BY source_status,source_candidate_kind
            """
        )
    }
    return metrics


def store_metrics(conn: sqlite3.Connection, metrics: Mapping[str, Any]) -> None:
    rows: List[Tuple[str, float, Optional[str]]] = []
    for name, value in sorted(metrics.items()):
        if isinstance(value, bool):
            rows.append((name, float(int(value)), None))
        elif isinstance(value, (int, float)):
            rows.append((name, float(value), None))
        elif isinstance(value, Mapping):
            numeric = [
                float(item)
                for item in value.values()
                if isinstance(item, (int, float)) and not isinstance(item, bool)
            ]
            rows.append((name, sum(numeric), json_dumps(value)))
    conn.executemany(
        """
        INSERT INTO metadata_build_metrics_v2(metric,value,details_json)
        VALUES (?,?,?)
        """,
        rows,
    )


def validate_output(
    conn: sqlite3.Connection,
    *,
    parent_counts: Mapping[str, int],
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def add(
        name: str,
        actual: Any,
        expected: Any,
        passed: Optional[bool] = None,
    ) -> None:
        if passed is None:
            passed = actual == expected
        checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": expected,
                "passed": bool(passed),
            }
        )

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    add("sqlite_integrity", integrity, "ok")
    foreign_key_errors = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    add("foreign_keys", foreign_key_errors, 0)

    for metric, table in (
        ("articles", "source_articles"),
        ("buildings", "buildings"),
        ("claims", "attribute_claims"),
        ("match_candidates", "article_match_candidates"),
    ):
        actual = int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
        add("parent_count_%s" % metric, actual, int(parent_counts[metric]))

    article_count = int(parent_counts["articles"])
    claim_count = int(parent_counts["claims"])
    add(
        "claim_evidence_complete",
        conn.execute("SELECT COUNT(*) FROM claim_evidence_v2").fetchone()[0],
        claim_count,
    )
    add(
        "article_kind_resolution_complete",
        conn.execute(
            "SELECT COUNT(*) FROM article_kind_resolution_v2"
        ).fetchone()[0],
        article_count,
    )
    add(
        "article_roles_complete",
        conn.execute(
            "SELECT COUNT(*) FROM building_article_roles_v2"
        ).fetchone()[0],
        article_count,
    )
    add(
        "unresolved_article_kind_not_coerced",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM article_kind_resolution_v2
            WHERE (status='unresolved' AND article_kind<>'unresolved')
               OR (article_kind='unresolved' AND status<>'unresolved')
            """
        ).fetchone()[0],
        0,
    )
    add(
        "semantic_article_roles_require_confirmation",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_article_roles_v2
            WHERE article_role IN (
              'drawing_feature','photo_feature','model_feature',
              'concept_editorial','mixed_feature'
            )
              AND article_kind_status<>'confirmed'
            """
        ).fetchone()[0],
        0,
    )
    add(
        "recrawl_queue_complete",
        conn.execute(
            "SELECT COUNT(*) FROM article_recrawl_queue_v2"
        ).fetchone()[0],
        article_count,
    )
    add(
        "active_membership_unique",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT article_id
              FROM v_active_building_articles_v2
              GROUP BY article_id
              HAVING COUNT(*)<>1
            )
            """
        ).fetchone()[0],
        0,
    )
    add(
        "active_membership_complete",
        conn.execute(
            "SELECT COUNT(*) FROM active_building_membership_v2"
        ).fetchone()[0],
        article_count,
    )
    materialized_image_count = conn.execute(
        "SELECT COUNT(*) FROM building_images_materialized_v2"
    ).fetchone()[0]
    expected_image_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM (
          SELECT DISTINCT va.building_id,aio.asset_key
          FROM active_building_membership_v2 va
          JOIN article_image_occurrences aio ON aio.article_id=va.article_id
        )
        """
    ).fetchone()[0]
    add(
        "building_images_materialized_complete",
        materialized_image_count,
        expected_image_count,
    )
    add(
        "redirects_are_terminal",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_redirects_v2 r
            JOIN building_redirects_v2 next
              ON next.source_building_id=r.target_building_id
            """
        ).fetchone()[0],
        0,
    )
    add(
        "redirects_have_approved_decisions",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_redirects_v2 r,json_each(r.decision_ids_json) d
            WHERE NOT EXISTS (
              SELECT 1
              FROM article_match_reviews_v2 mr
              WHERE mr.decision_id=d.value
                AND mr.decision_source='versioned_manual_decision'
                AND mr.decision_status='confirmed'
                AND json_extract(
                      mr.decision_reason_json,'$.manual.approved'
                    )=1
                AND COALESCE(
                      json_extract(
                        mr.decision_reason_json,'$.manual.reviewer'
                      ),''
                    )<>''
                AND COALESCE(
                      json_extract(
                        mr.decision_reason_json,'$.manual.reviewed_at'
                      ),''
                    )<>''
            )
            """
        ).fetchone()[0],
        0,
    )
    add(
        "supporting_confirmation_independent",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_facets_v2
            WHERE status='confirmed'
              AND direct_claim_count=0
              AND (independence_group_count<2 OR article_count<2)
            """
        ).fetchone()[0],
        0,
    )
    add(
        "direct_confirmation_threshold",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_facets_v2 f
            WHERE f.status='confirmed'
              AND f.direct_claim_count>0
              AND NOT EXISTS (
                SELECT 1
                FROM building_facet_claims_v2 fc
                JOIN claim_evidence_v2 ce ON ce.claim_id=fc.claim_id
                JOIN attribute_claims c ON c.claim_id=fc.claim_id
                WHERE fc.facet_v2_id=f.facet_v2_id
                  AND ce.mapping_kind='direct'
                  AND c.confidence>=0.85
              )
            """
        ).fetchone()[0],
        0,
    )
    add(
        "article_kind_confirmation_requires_authoritative_evidence",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM article_kind_resolution_v2 ak
            WHERE ak.status='confirmed'
              AND NOT EXISTS (
                SELECT 1
                FROM article_kind_evidence_v2 e
                WHERE e.article_id=ak.article_id
                  AND e.evidence_kind IN ('html_explicit','manual')
                  AND e.status='strong'
              )
            """
        ).fetchone()[0],
        0,
    )
    add(
        "multi_program_primary_abstention",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_attributes_v2
            WHERE is_active=1
              AND json_array_length(programs_json)>1
              AND program_primary IS NOT NULL
            """
        ).fetchone()[0],
        0,
    )
    add(
        "multi_typology_primary_abstention",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_attributes_v2
            WHERE is_active=1
              AND json_array_length(typologies_json)>1
              AND typology_primary IS NOT NULL
            """
        ).fetchone()[0],
        0,
    )
    add(
        "active_primary_is_active_member",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_attributes_v2 a
            WHERE a.is_active=1
              AND NOT EXISTS (
                SELECT 1
                FROM active_building_membership_v2 m
                WHERE m.building_id=a.building_id
                  AND m.article_id=a.primary_article_id
              )
            """
        ).fetchone()[0],
        0,
    )
    add(
        "core_conflicts_abstain",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_attributes_v2
            WHERE is_active=1
              AND (
                (
                  json_type(core_conflicts_json,'$.project_year') IS NOT NULL
                  AND project_year IS NOT NULL
                )
                OR (
                  json_type(core_conflicts_json,'$.area_sqm') IS NOT NULL
                  AND area_sqm IS NOT NULL
                )
                OR (
                  json_type(core_conflicts_json,'$.location_country') IS NOT NULL
                  AND location_country IS NOT NULL
                )
                OR (
                  json_type(core_conflicts_json,'$.location_city') IS NOT NULL
                  AND location_city IS NOT NULL
                )
              )
            """
        ).fetchone()[0],
        0,
    )
    add(
        "unredirected_core_metadata_preserved",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM building_attributes_v2 a
            JOIN buildings b ON b.building_id=a.building_id
            WHERE a.is_active=1
              AND NOT EXISTS (
                SELECT 1
                FROM active_building_membership_v2 m
                WHERE m.building_id=a.building_id
                  AND m.source_building_id<>a.building_id
              )
              AND (
                a.primary_article_id IS NOT b.primary_article_id
                OR a.name IS NOT b.name
                OR a.name_normalized IS NOT b.name_normalized
                OR a.location_country IS NOT b.location_country
                OR a.location_city IS NOT b.location_city
                OR a.project_year IS NOT b.project_year
                OR a.area_sqm IS NOT b.area_sqm
                OR a.description_text_id IS NOT b.description_text_id
              )
            """
        ).fetchone()[0],
        0,
    )
    for axis, json_column in (
        ("program", "programs_json"),
        ("typology", "typologies_json"),
    ):
        add(
            "%s_array_has_only_confirmed_facets" % axis,
            conn.execute(
                """
                SELECT COUNT(*)
                FROM building_attributes_v2 a,json_each(a.%s) item
                WHERE a.is_active=1
                  AND NOT EXISTS (
                    SELECT 1
                    FROM building_facets_v2 f
                    WHERE f.building_id=a.building_id
                      AND f.axis=?
                      AND f.value=item.value
                      AND f.status='confirmed'
                  )
                """
                % json_column,
                (axis,),
            ).fetchone()[0],
            0,
        )
        add(
            "%s_confirmed_facets_all_exported" % axis,
            conn.execute(
                """
                SELECT COUNT(*)
                FROM building_facets_v2 f
                JOIN building_attributes_v2 a ON a.building_id=f.building_id
                WHERE a.is_active=1
                  AND f.axis=?
                  AND f.status='confirmed'
                  AND NOT EXISTS (
                    SELECT 1 FROM json_each(a.%s) item
                    WHERE item.value=f.value
                  )
                """
                % json_column,
                (axis,),
            ).fetchone()[0],
            0,
        )
    add(
        "scalar_primary_unique",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT building_id,axis
              FROM building_facets_v2
              WHERE role='primary'
                AND axis IN (
                  'style','structural_system','roof_type',
                  'facade_pattern','facade_system'
                )
              GROUP BY building_id,axis
              HAVING COUNT(*)>1
            )
            """
        ).fetchone()[0],
        0,
    )
    add(
        "auto_cluster_pairs_share_active_building",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM article_match_reviews_v2 mr
            JOIN v_active_building_articles_v2 a
              ON a.article_id=mr.article_id_a
            JOIN v_active_building_articles_v2 b
              ON b.article_id=mr.article_id_b
            WHERE mr.source_status='auto_clustered'
              AND a.building_id<>b.building_id
            """
        ).fetchone()[0],
        0,
    )
    add(
        "pending_pairs_remain_separate",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM article_match_reviews_v2 mr
            JOIN v_active_building_articles_v2 a
              ON a.article_id=mr.article_id_a
            JOIN v_active_building_articles_v2 b
              ON b.article_id=mr.article_id_b
            WHERE mr.decision_status IN ('pending','deferred')
              AND a.building_id=b.building_id
            """
        ).fetchone()[0],
        0,
    )
    active_buildings = conn.execute(
        "SELECT COUNT(*) FROM building_attributes_v2 WHERE is_active=1"
    ).fetchone()[0]
    add(
        "export_contains_active_buildings_only",
        conn.execute(
            "SELECT COUNT(*) FROM v_divisare_buildings_export_v2"
        ).fetchone()[0],
        active_buildings,
    )
    add(
        "redirected_buildings_not_exported",
        conn.execute(
            """
            SELECT COUNT(*)
            FROM v_divisare_buildings_export_v2 e
            JOIN building_redirects_v2 r
              ON r.source_building_id=e.canonical_bld_id
            """
        ).fetchone()[0],
        0,
    )
    add(
        "all_source_match_candidates_reviewed",
        conn.execute(
            "SELECT COUNT(*) FROM article_match_reviews_v2"
        ).fetchone()[0],
        int(parent_counts["match_candidates"]),
    )

    checked_at = utc_now()
    conn.executemany(
        """
        INSERT INTO metadata_validation_v2(
            check_name,passed,actual_json,expected_json,checked_at
        ) VALUES (?,?,?,?,?)
        """,
        [
            (
                check["name"],
                int(check["passed"]),
                json_dumps(check["actual"]),
                json_dumps(check["expected"]),
                checked_at,
            )
            for check in checks
        ],
    )
    failed = [check for check in checks if not check["passed"]]
    if failed:
        raise RuntimeError(
            "metadata-v2 validation failed: %s"
            % ", ".join(check["name"] for check in failed)
        )
    return {
        "passed": len(checks),
        "failed": 0,
        "checks": checks,
    }


def write_report(
    path: Path,
    *,
    parent_path: Path,
    output_path: Path,
    parent_sha256: str,
    output_sha256: str,
    decision_path: Optional[Path],
    decision_sha256: Optional[str],
    metrics: Mapping[str, Any],
    validation: Mapping[str, Any],
    elapsed_seconds: float,
) -> None:
    article_status = metrics.get("article_kind_status", {})
    article_values = metrics.get("article_kind_values", {})
    d2_status = metrics.get("d2_source_status", {})
    lines = [
        "# Divisare metadata v2 build report",
        "",
        "## Artifact",
        "",
        "- Builder: `%s`" % BUILDER_VERSION,
        "- Schema user version: `%s`" % SCHEMA_VERSION,
        "- Parent: `%s`" % parent_path,
        "- Parent SHA-256: `%s`" % parent_sha256,
        "- Output: `%s`" % output_path,
        "- Output SHA-256: `%s`" % output_sha256,
        "- Decision file: `%s`"
        % (decision_path if decision_path is not None else "none"),
        "- Decision SHA-256: `%s`" % (decision_sha256 or "none"),
        "- Elapsed: `%.2f seconds`" % elapsed_seconds,
        "- API/LLM cost: `$0`",
        "",
        "## Result",
        "",
        "- Articles / active buildings: `%s / %s`"
        % (metrics.get("articles"), metrics.get("buildings_active")),
        "- Facets confirmed / candidate: `%s / %s`"
        % (
            metrics.get("confirmed_facets_v2"),
            metrics.get("candidate_facets_v2"),
        ),
        "- v1 confirmed facets downgraded: `%s`"
        % metrics.get("facets_downgraded"),
        "- Program / typology compatibility primaries: `%s / %s`"
        % (
            metrics.get("program_primary_v2"),
            metrics.get("typology_primary_v2"),
        ),
        "- Multi-program / multi-typology buildings: `%s / %s`"
        % (
            metrics.get("multi_program_buildings"),
            metrics.get("multi_typology_buildings"),
        ),
        "- D2 confirmed / pending / redirects: `%s / %s / %s`"
        % (
            metrics.get("d2_confirmed"),
            metrics.get("d2_review_pending"),
            metrics.get("redirects"),
        ),
        "- Metadata recrawl queue: `%s`" % metrics.get("recrawl_queue"),
        "- Validation checks passed: `%s`" % validation.get("passed"),
        "",
        "## Article Kind",
        "",
        "Resolution status:",
        "",
        "```json",
        json.dumps(article_status, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "Resolved values:",
        "",
        "```json",
        json.dumps(article_values, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "Divisare album tags, content hints, and title/slug lexical rules remain",
        "candidates. Only explicit HTML DOM evidence or an approved manual",
        "decision can confirm an article kind.",
        "",
        "## Metadata D2",
        "",
        "The v1 strict auto-clusters are retained. Open pairs remain separate unless",
        "a versioned decision file explicitly merges them.",
        "",
        "```json",
        json.dumps(d2_status, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Scope Boundary",
        "",
        "- Included: evidence independence, multi-value program/typology, article",
        "  kind candidates, metadata duplicate review state, immutable redirect",
        "  overlay, and a full metadata recrawl queue.",
        "- Preserved: raw tags, source articles, text history, image URLs/assets,",
        "  pHash work state, and all v1 provenance tables.",
        "- Not performed: image semantic classification, image downloading, pHash",
        "  computation, vector/embedding generation, cross-site matching, Neon/R2.",
        "- Historical description and area values are not asserted as fixed by this",
        "  build. They require the separate HTML recrawl sidecar.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def publish_no_clobber(
    *,
    temp_path: Path,
    output_path: Path,
    report_temp_path: Path,
    report_path: Path,
) -> None:
    published: List[Path] = []
    try:
        os.link(report_temp_path, report_path)
        published.append(report_path)
        # The DB is the final commit marker. Its presence means both completed
        # artifacts were ready for publication.
        os.link(temp_path, output_path)
        published.append(output_path)
    except FileExistsError as exc:
        for path in reversed(published):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise RuntimeError(
            "an output appeared during the build; refusing to overwrite it"
        ) from exc
    except Exception:
        for path in reversed(published):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    report_temp_path.unlink()
    temp_path.unlink()


def _build_locked(
    *,
    parent_path: Path,
    output_path: Path,
    report_path: Path,
    decisions_path: Optional[Path],
) -> Dict[str, Any]:
    started = time.monotonic()
    parent_path = parent_path.resolve()
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    decisions_path = decisions_path.resolve() if decisions_path else None
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    report_temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    lock_path = output_path.with_suffix(output_path.suffix + ".build.lock")
    validate_paths(
        parent_path,
        output_path,
        report_path,
        temp_path,
        report_temp_path,
        lock_path,
    )
    if not parent_path.exists():
        raise FileNotFoundError(parent_path)
    if output_path.exists():
        raise FileExistsError(
            "%s exists; metadata DB artifacts are immutable" % output_path
        )
    if report_path.exists():
        raise FileExistsError(
            "%s exists; choose a new versioned report path" % report_path
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    for stale_temp in (temp_path, report_temp_path):
        if stale_temp.exists():
            stale_temp.unlink()

    parent_stat = parent_path.stat()
    parent_sha_before = file_sha256(parent_path)
    source = open_readonly(parent_path)
    try:
        parent = validate_parent(source)
        decision_version, decisions, decision_sha = load_review_decisions(
            decisions_path
        )
    except Exception:
        source.close()
        raise

    target: Optional[sqlite3.Connection] = None
    try:
        target = sqlite3.connect(temp_path)
        source.backup(target, pages=8192)
        source.close()
        target.row_factory = sqlite3.Row
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA synchronous=NORMAL")
        target.execute("PRAGMA temp_store=FILE")
        create_schema(target)
        target.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

        parent_run = parent["run"]
        target.execute(
            """
            INSERT INTO artifact_lineage_v2(
                lineage_id,parent_db_path,parent_sha256,parent_byte_size,
                parent_user_version,parent_builder_version,
                parent_taxonomy_version,parent_cluster_version,
                parent_resolver_version,v2_builder_version,v2_schema_version,
                metadata_version,evidence_policy_version,facet_policy_version,
                article_kind_policy_version,primary_value_policy_version,
                decision_schema_version,decision_file_path,
                decision_file_sha256,created_at
            ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(parent_path),
                parent_sha_before,
                parent_stat.st_size,
                parent["user_version"],
                parent_run["builder_version"],
                parent_run["taxonomy_version"],
                parent_run["cluster_version"],
                parent_run["resolver_version"],
                BUILDER_VERSION,
                SCHEMA_VERSION,
                METADATA_VERSION,
                EVIDENCE_POLICY_VERSION,
                FACET_POLICY_VERSION,
                ARTICLE_KIND_POLICY_VERSION,
                PRIMARY_VALUE_POLICY_VERSION,
                DECISION_SCHEMA_VERSION if decisions_path else None,
                str(decisions_path) if decisions_path else None,
                decision_sha,
                utc_now(),
            ),
        )

        claim_evidence_count = populate_claim_evidence(target)
        article_kind_metrics = populate_article_kinds(target)
        match_metrics = populate_match_reviews_and_redirects(
            target,
            decision_version=decision_version,
            decisions=decisions,
        )
        active_membership_count = materialize_active_membership(target)
        target.executescript(ACTIVE_MEMBERSHIP_VIEW_SQL)
        building_image_count = materialize_building_images(target)
        facet_metrics = populate_facets_v2(target)
        attribute_metrics = populate_building_attributes(target)
        role_count = populate_building_article_roles(target)
        recrawl_count = populate_recrawl_queue(target)
        target.executescript(VIEWS_SQL)

        extra = {
            **article_kind_metrics,
            **match_metrics,
            **facet_metrics,
            **attribute_metrics,
            "claim_evidence_generated": claim_evidence_count,
            "active_membership_rows": active_membership_count,
            "building_images_materialized": building_image_count,
            "article_roles_generated": role_count,
            "recrawl_rows_generated": recrawl_count,
            "manual_decisions_loaded": len(decisions),
        }
        metrics = collect_metrics(target, extra)
        store_metrics(target, metrics)
        validation = validate_output(
            target,
            parent_counts=parent["counts"],
        )
        target.commit()
        target.execute("ANALYZE")
        target.execute("PRAGMA optimize")
        target.commit()
        target.close()
        target = None

        parent_sha_after = file_sha256(parent_path)
        if parent_sha_after != parent_sha_before:
            raise RuntimeError("parent DB changed while v2 was being built")
        output_sha = file_sha256(temp_path)
        elapsed = time.monotonic() - started
        write_report(
            report_temp_path,
            parent_path=parent_path,
            output_path=output_path,
            parent_sha256=parent_sha_before,
            output_sha256=output_sha,
            decision_path=decisions_path,
            decision_sha256=decision_sha,
            metrics=metrics,
            validation=validation,
            elapsed_seconds=elapsed,
        )
        publish_no_clobber(
            temp_path=temp_path,
            output_path=output_path,
            report_temp_path=report_temp_path,
            report_path=report_path,
        )
        return {
            "output_db": str(output_path),
            "output_sha256": output_sha,
            "report": str(report_path),
            "elapsed_seconds": round(elapsed, 2),
            "metrics": metrics,
            "validation": {
                "passed": validation["passed"],
                "failed": validation["failed"],
            },
        }
    except Exception:
        if target is not None:
            target.close()
        try:
            source.close()
        except Exception:
            pass
        for partial in (temp_path, report_temp_path):
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
        raise


def build(
    *,
    parent_path: Path,
    output_path: Path,
    report_path: Path,
    decisions_path: Optional[Path] = None,
) -> Dict[str, Any]:
    parent_path = parent_path.resolve()
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    lock_path = output_path.with_suffix(output_path.suffix + ".build.lock")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_build_lock(lock_path, output_path):
        return _build_locked(
            parent_path=parent_path,
            output_path=output_path,
            report_path=report_path,
            decisions_path=decisions_path,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-db", type=Path, required=True)
    parser.add_argument(
        "--output-db",
        type=Path,
        default=ROOT / "data" / "curated" / "divisare_metadata_v2_1.db",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "data" / "reports" / "divisare_metadata_v2_1.md",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=None,
        help=(
            "Optional versioned JSON decisions for existing D2 candidate pairs. "
            "No decision file means no new redirects."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build(
            parent_path=args.parent_db,
            output_path=args.output_db,
            report_path=args.report,
            decisions_path=args.decisions,
        )
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
