"""Read-only source adapters for cross-source image evidence (E2).

The adapters in this module deliberately stop at source facts.  They expose
projects, source-local buildings, memberships, image occurrences, and the E1
fingerprint ledger without selecting representative images or deciding that
two buildings are identical.

Both the curated source database and its E1 sidecar are opened with SQLite's
``mode=ro&immutable=1`` contract.  Every iterator is deterministically ordered
and uses ``fetchmany`` so memory is bounded by ``batch_size``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


TERMINAL_E1_STATUSES = frozenset({"complete", "complete_with_failures"})
DEFAULT_BATCH_SIZE = 1_000


class SourceMappingError(RuntimeError):
    """Raised when a source DB and its E1 ledger cannot be joined exactly."""


@dataclass(frozen=True)
class E1Run:
    run_id: str
    source: str
    status: str
    source_db_sha256_before: str
    source_db_sha256_after: str | None
    fingerprint_contract_version: str
    selection_count: int
    excluded_count: int
    source_total_count: int


@dataclass(frozen=True)
class SourceProject:
    source: str
    source_project_id: str
    global_id: str | None
    slug: str
    source_url: str
    name: str
    normalized_name: str
    country: str | None
    city: str | None
    year: int | None
    firm_slug: str | None
    firm_name: str | None
    source_status: str
    source_record_sha256: str | None


@dataclass(frozen=True)
class SourceBuilding:
    source: str
    source_building_id: str
    primary_source_project_id: str
    name: str
    normalized_name: str
    country: str | None
    city: str | None
    year: int | None
    firm_slug: str | None
    firm_name: str | None
    identity_status: str


@dataclass(frozen=True)
class SourceMembership:
    source: str
    source_building_id: str
    source_project_id: str
    is_primary: bool
    membership_status: str
    membership_role: str | None
    confidence: float | None
    rule_id: str | None


@dataclass(frozen=True)
class SourceAssetFingerprint:
    source: str
    source_asset_id: str
    source_asset_key: str
    e1_run_id: str
    ledger_status: str
    fingerprint_status: str
    source_record_sha256: str
    canonical_url: str | None
    fetch_url: str | None
    raw_response_sha256: str | None
    normalized_pixel_sha256: str | None
    phash_hex: str | None
    original_width: int | None
    original_height: int | None
    normalized_width: int | None
    normalized_height: int | None
    metadata_json: str
    quality_flags: tuple[str, ...]
    error_kind: str | None
    error_message: str | None
    exclusion_reason: str | None
    exclusion_detail_json: str | None

    @property
    def has_fingerprint(self) -> bool:
        return bool(self.normalized_pixel_sha256 and self.phash_hex)


@dataclass(frozen=True)
class SourceOccurrence:
    source: str
    occurrence_id: str
    source_project_id: str
    source_asset_id: str | None
    role: str
    ordinal: int
    raw_url: str
    parse_status: str
    parse_error: str | None
    source_field: str | None
    image_type: str | None


def _open_immutable(path: Path | str) -> sqlite3.Connection:
    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(target)
    connection = sqlite3.connect(
        f"file:{target.as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _iter_rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[object] = (),
    *,
    batch_size: int,
) -> Iterator[sqlite3.Row]:
    cursor = connection.execute(query, tuple(parameters))
    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield from rows
    finally:
        cursor.close()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _quality_flags(metadata_json: str) -> tuple[str, ...]:
    try:
        payload = json.loads(metadata_json)
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    flags = payload.get("quality_flags", ())
    if not isinstance(flags, list):
        return ()
    return tuple(str(flag) for flag in flags if isinstance(flag, str))


class _SourceEvidenceAdapter:
    source: str

    def __init__(
        self,
        source_db_path: Path | str,
        e1_sidecar_path: Path | str,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self.source_connection = _open_immutable(source_db_path)
        try:
            self.e1_connection = _open_immutable(e1_sidecar_path)
            self.e1_run = self._bind_e1_run()
        except Exception:
            self.source_connection.close()
            raise

    def _bind_e1_run(self) -> E1Run:
        rows = self.e1_connection.execute(
            """
            SELECT run_id,source_name,status,source_db_sha256_before,
                   source_db_sha256_after,fingerprint_contract_version,
                   selection_count,excluded_count,source_total_count
            FROM fingerprint_runs
            ORDER BY run_id
            """
        ).fetchmany(2)
        if len(rows) != 1:
            raise SourceMappingError(
                f"{self.source} E1 sidecar must contain exactly one run; "
                f"observed at least {len(rows)}"
            )
        row = rows[0]
        if str(row["source_name"]) != self.source:
            raise SourceMappingError(
                f"E1 source mismatch: expected {self.source}, "
                f"got {row['source_name']}"
            )
        if str(row["status"]) not in TERMINAL_E1_STATUSES:
            raise SourceMappingError(
                f"E1 run is not complete: {row['run_id']}={row['status']}"
            )
        run_id = str(row["run_id"])
        selected = int(
            self.e1_connection.execute(
                "SELECT count(*) FROM source_assets WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        excluded = int(
            self.e1_connection.execute(
                "SELECT count(*) FROM source_asset_exclusions WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        fingerprints = int(
            self.e1_connection.execute(
                "SELECT count(*) FROM fingerprints WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        expected_selected = int(row["selection_count"])
        expected_excluded = int(row["excluded_count"])
        expected_total = int(row["source_total_count"])
        if (
            selected != expected_selected
            or excluded != expected_excluded
            or selected + excluded != expected_total
            or fingerprints != selected
        ):
            raise SourceMappingError(
                "E1 terminal accounting mismatch: "
                f"selected={selected}/{expected_selected}, "
                f"excluded={excluded}/{expected_excluded}, "
                f"fingerprints={fingerprints}, total={expected_total}"
            )
        return E1Run(
            run_id=run_id,
            source=self.source,
            status=str(row["status"]),
            source_db_sha256_before=str(row["source_db_sha256_before"]),
            source_db_sha256_after=_optional_text(row["source_db_sha256_after"]),
            fingerprint_contract_version=str(row["fingerprint_contract_version"]),
            selection_count=expected_selected,
            excluded_count=expected_excluded,
            source_total_count=expected_total,
        )

    def close(self) -> None:
        self.e1_connection.close()
        self.source_connection.close()

    def __enter__(self) -> _SourceEvidenceAdapter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _source_asset_rows(self) -> Iterator[sqlite3.Row]:
        raise NotImplementedError

    def _ledger_rows(self) -> Iterator[sqlite3.Row]:
        query = """
        SELECT s.source_asset_id,NULL AS source_asset_key,
               'eligible' AS ledger_status,s.source_record_sha256,
               s.canonical_url,s.fetch_url,f.status AS fingerprint_status,
               f.raw_response_sha256,f.normalized_pixel_sha256,f.phash_hex,
               f.original_width,f.original_height,
               f.normalized_width,f.normalized_height,
               f.metadata_json,f.error_kind,f.error_message,
               NULL AS exclusion_reason,NULL AS exclusion_detail_json
        FROM source_assets AS s
        LEFT JOIN fingerprints AS f
          ON f.run_id=s.run_id AND f.source_asset_id=s.source_asset_id
        WHERE s.run_id=?
        UNION ALL
        SELECT e.source_asset_id,e.source_asset_key,
               'excluded' AS ledger_status,e.source_record_sha256,
               NULL AS canonical_url,NULL AS fetch_url,
               'excluded' AS fingerprint_status,
               NULL AS raw_response_sha256,
               NULL AS normalized_pixel_sha256,NULL AS phash_hex,
               NULL AS original_width,NULL AS original_height,
               NULL AS normalized_width,NULL AS normalized_height,
               '{}' AS metadata_json,NULL AS error_kind,NULL AS error_message,
               e.reason_code AS exclusion_reason,
               e.detail_json AS exclusion_detail_json
        FROM source_asset_exclusions AS e
        WHERE e.run_id=?
        ORDER BY source_asset_id
        """
        return _iter_rows(
            self.e1_connection,
            query,
            (self.e1_run.run_id, self.e1_run.run_id),
            batch_size=self.batch_size,
        )

    def iter_assets(self) -> Iterator[SourceAssetFingerprint]:
        """Yield a lossless, ordered join of source assets and the E1 ledger."""

        source_rows = iter(self._source_asset_rows())
        ledger_rows = iter(self._ledger_rows())
        source_row = next(source_rows, None)
        ledger_row = next(ledger_rows, None)
        yielded = 0
        while source_row is not None or ledger_row is not None:
            if source_row is None:
                raise SourceMappingError(
                    f"E1 asset missing from {self.source} source DB: "
                    f"{ledger_row['source_asset_id']}"
                )
            if ledger_row is None:
                raise SourceMappingError(
                    f"{self.source} source asset missing from E1 ledger: "
                    f"{source_row['source_asset_id']}"
                )
            source_id = str(source_row["source_asset_id"])
            ledger_id = str(ledger_row["source_asset_id"])
            if source_id != ledger_id:
                if source_id < ledger_id:
                    raise SourceMappingError(
                        f"{self.source} source asset missing from E1 ledger: {source_id}"
                    )
                raise SourceMappingError(
                    f"E1 asset missing from {self.source} source DB: {ledger_id}"
                )
            source_key = str(source_row["source_asset_key"])
            ledger_key = _optional_text(ledger_row["source_asset_key"])
            if ledger_key is not None and ledger_key != source_key:
                raise SourceMappingError(
                    f"source asset key mismatch for {self.source}:{source_id}: "
                    f"{source_key} != {ledger_key}"
                )
            status = str(ledger_row["fingerprint_status"])
            if ledger_row["ledger_status"] == "eligible" and status not in {
                "success",
                "failed",
                "skipped",
            }:
                raise SourceMappingError(
                    f"terminal E1 asset has invalid status: {source_id}={status}"
                )
            metadata_json = str(ledger_row["metadata_json"] or "{}")
            asset = SourceAssetFingerprint(
                source=self.source,
                source_asset_id=source_id,
                source_asset_key=source_key,
                e1_run_id=self.e1_run.run_id,
                ledger_status=str(ledger_row["ledger_status"]),
                fingerprint_status=status,
                source_record_sha256=str(ledger_row["source_record_sha256"]),
                canonical_url=_optional_text(ledger_row["canonical_url"]),
                fetch_url=_optional_text(ledger_row["fetch_url"]),
                raw_response_sha256=_optional_text(
                    ledger_row["raw_response_sha256"]
                ),
                normalized_pixel_sha256=_optional_text(
                    ledger_row["normalized_pixel_sha256"]
                ),
                phash_hex=_optional_text(ledger_row["phash_hex"]),
                original_width=_optional_int(ledger_row["original_width"]),
                original_height=_optional_int(ledger_row["original_height"]),
                normalized_width=_optional_int(ledger_row["normalized_width"]),
                normalized_height=_optional_int(ledger_row["normalized_height"]),
                metadata_json=metadata_json,
                quality_flags=_quality_flags(metadata_json),
                error_kind=_optional_text(ledger_row["error_kind"]),
                error_message=_optional_text(ledger_row["error_message"]),
                exclusion_reason=_optional_text(ledger_row["exclusion_reason"]),
                exclusion_detail_json=_optional_text(
                    ledger_row["exclusion_detail_json"]
                ),
            )
            if status == "success" and not asset.has_fingerprint:
                raise SourceMappingError(
                    f"successful E1 asset lacks hashes: {self.source}:{source_id}"
                )
            if status != "success" and asset.has_fingerprint:
                raise SourceMappingError(
                    f"non-success E1 asset unexpectedly has hashes: "
                    f"{self.source}:{source_id}"
                )
            yield asset
            yielded += 1
            source_row = next(source_rows, None)
            ledger_row = next(ledger_rows, None)
        if yielded != self.e1_run.source_total_count:
            raise SourceMappingError(
                f"source/E1 total mismatch: yielded={yielded}, "
                f"expected={self.e1_run.source_total_count}"
            )


class DivisareEvidenceSources(_SourceEvidenceAdapter):
    source = "divisare"

    def iter_projects(self) -> Iterator[SourceProject]:
        query = """
        SELECT a.article_id,a.slug,a.source_url,
               coalesce(r.resolved_name,a.name_raw) AS name,
               coalesce(r.resolved_name_normalized,a.name_normalized)
                   AS normalized_name,
               coalesce(r.location_country,a.location_country) AS country,
               coalesce(r.location_city,a.location_city) AS city,
               coalesce(r.project_year,a.project_year) AS project_year,
               r.availability_status,a.article_kind,a.source_row_hash,
               (SELECT sa.slug
                  FROM article_architects aa
                  JOIN source_architects sa ON sa.architect_id=aa.architect_id
                 WHERE aa.article_id=a.article_id
                 ORDER BY aa.position,aa.architect_id LIMIT 1) AS firm_slug,
               (SELECT aa.architect_name
                  FROM article_architects aa
                 WHERE aa.article_id=a.article_id
                 ORDER BY aa.position,aa.architect_id LIMIT 1) AS firm_name
        FROM source_articles a
        LEFT JOIN article_metadata_resolution_v2_3 r
          ON r.article_id=a.article_id
        ORDER BY a.article_id
        """
        for row in _iter_rows(
            self.source_connection, query, batch_size=self.batch_size
        ):
            yield SourceProject(
                source=self.source,
                source_project_id=str(row["article_id"]),
                global_id=None,
                slug=str(row["slug"]),
                source_url=str(row["source_url"]),
                name=str(row["name"]),
                normalized_name=str(row["normalized_name"]),
                country=_optional_text(row["country"]),
                city=_optional_text(row["city"]),
                year=_optional_int(row["project_year"]),
                firm_slug=_optional_text(row["firm_slug"]),
                firm_name=_optional_text(row["firm_name"]),
                source_status=str(
                    row["availability_status"] or row["article_kind"]
                ),
                source_record_sha256=_optional_text(row["source_row_hash"]),
            )

    def iter_buildings(self) -> Iterator[SourceBuilding]:
        query = """
        SELECT b.building_id,b.primary_article_id,b.name,b.name_normalized,
               b.location_country,b.location_city,b.project_year,b.cluster_status,
               (SELECT sa.slug
                  FROM article_architects aa
                  JOIN source_architects sa ON sa.architect_id=aa.architect_id
                 WHERE aa.article_id=b.primary_article_id
                 ORDER BY aa.position,aa.architect_id LIMIT 1) AS firm_slug,
               (SELECT aa.architect_name
                  FROM article_architects aa
                 WHERE aa.article_id=b.primary_article_id
                 ORDER BY aa.position,aa.architect_id LIMIT 1) AS firm_name
        FROM buildings b
        ORDER BY b.building_id
        """
        for row in _iter_rows(
            self.source_connection, query, batch_size=self.batch_size
        ):
            yield SourceBuilding(
                source=self.source,
                source_building_id=str(row["building_id"]),
                primary_source_project_id=str(row["primary_article_id"]),
                name=str(row["name"]),
                normalized_name=str(row["name_normalized"]),
                country=_optional_text(row["location_country"]),
                city=_optional_text(row["location_city"]),
                year=_optional_int(row["project_year"]),
                firm_slug=_optional_text(row["firm_slug"]),
                firm_name=_optional_text(row["firm_name"]),
                identity_status=str(row["cluster_status"]),
            )

    def iter_memberships(self) -> Iterator[SourceMembership]:
        query = """
        SELECT m.article_id,m.building_id,m.source_article_role,
               m.membership_confidence,m.decision_method,b.primary_article_id
        FROM active_building_membership_v2_3 m
        JOIN buildings b ON b.building_id=m.building_id
        ORDER BY m.building_id,m.article_id
        """
        for row in _iter_rows(
            self.source_connection, query, batch_size=self.batch_size
        ):
            yield SourceMembership(
                source=self.source,
                source_building_id=str(row["building_id"]),
                source_project_id=str(row["article_id"]),
                is_primary=int(row["article_id"]) == int(row["primary_article_id"]),
                membership_status="active",
                membership_role=str(row["source_article_role"]),
                confidence=float(row["membership_confidence"]),
                rule_id=str(row["decision_method"]),
            )

    def _source_asset_rows(self) -> Iterator[sqlite3.Row]:
        return _iter_rows(
            self.source_connection,
            """
            SELECT asset_key AS source_asset_id,asset_key AS source_asset_key
            FROM image_assets
            ORDER BY asset_key
            """,
            batch_size=self.batch_size,
        )

    def iter_occurrences(self) -> Iterator[SourceOccurrence]:
        query = """
        SELECT article_id,role,position,raw_url,parse_status,parse_error,asset_key
        FROM source_image_occurrences
        ORDER BY article_id,role,position
        """
        for row in _iter_rows(
            self.source_connection, query, batch_size=self.batch_size
        ):
            article_id = str(row["article_id"])
            role = str(row["role"])
            ordinal = int(row["position"])
            yield SourceOccurrence(
                source=self.source,
                occurrence_id=f"{article_id}:{role}:{ordinal}",
                source_project_id=article_id,
                source_asset_id=_optional_text(row["asset_key"]),
                role=role,
                ordinal=ordinal,
                raw_url=str(row["raw_url"]),
                parse_status=str(row["parse_status"]),
                parse_error=_optional_text(row["parse_error"]),
                source_field=None,
                image_type=None,
            )


class ArchitizerEvidenceSources(_SourceEvidenceAdapter):
    source = "architizer"

    def iter_projects(self) -> Iterator[SourceProject]:
        query = """
        SELECT source_project_id,global_id,slug,source_url,name,normalized_name,
               location_country_raw,location_city_raw,completion_year_raw,
               source_firm_slug,source_firm_name,acceptance_status,exclusion_reason
        FROM source_projects
        ORDER BY source_project_id
        """
        for row in _iter_rows(
            self.source_connection, query, batch_size=self.batch_size
        ):
            status = str(row["acceptance_status"])
            reason = _optional_text(row["exclusion_reason"])
            if reason:
                status = f"{status}:{reason}"
            yield SourceProject(
                source=self.source,
                source_project_id=str(row["source_project_id"]),
                global_id=_optional_text(row["global_id"]),
                slug=str(row["slug"]),
                source_url=str(row["source_url"]),
                name=str(row["name"]),
                normalized_name=str(row["normalized_name"]),
                country=_optional_text(row["location_country_raw"]),
                city=_optional_text(row["location_city_raw"]),
                year=_optional_int(row["completion_year_raw"]),
                firm_slug=_optional_text(row["source_firm_slug"]),
                firm_name=_optional_text(row["source_firm_name"]),
                source_status=status,
                source_record_sha256=None,
            )

    def iter_buildings(self) -> Iterator[SourceBuilding]:
        query = """
        SELECT b.building_id,b.primary_project_id,b.preferred_name,b.normalized_name,
               b.location_country,b.location_city,b.completion_year,
               b.source_firm_slug,b.identity_status,p.source_firm_name
        FROM buildings b
        LEFT JOIN source_projects p
          ON p.source_project_id=b.primary_project_id
        ORDER BY b.building_id
        """
        for row in _iter_rows(
            self.source_connection, query, batch_size=self.batch_size
        ):
            yield SourceBuilding(
                source=self.source,
                source_building_id=str(row["building_id"]),
                primary_source_project_id=str(row["primary_project_id"]),
                name=str(row["preferred_name"]),
                normalized_name=str(row["normalized_name"]),
                country=_optional_text(row["location_country"]),
                city=_optional_text(row["location_city"]),
                year=_optional_int(row["completion_year"]),
                firm_slug=_optional_text(row["source_firm_slug"]),
                firm_name=_optional_text(row["source_firm_name"]),
                identity_status=str(row["identity_status"]),
            )

    def iter_memberships(self) -> Iterator[SourceMembership]:
        query = """
        SELECT bp.building_id,bp.source_project_id,bp.is_primary,
               bp.membership_status,bp.rule_id
        FROM building_projects bp
        ORDER BY bp.building_id,bp.source_project_id
        """
        for row in _iter_rows(
            self.source_connection, query, batch_size=self.batch_size
        ):
            yield SourceMembership(
                source=self.source,
                source_building_id=str(row["building_id"]),
                source_project_id=str(row["source_project_id"]),
                is_primary=bool(row["is_primary"]),
                membership_status=str(row["membership_status"]),
                membership_role="primary" if row["is_primary"] else "member",
                confidence=None,
                rule_id=str(row["rule_id"]),
            )

    def _source_asset_rows(self) -> Iterator[sqlite3.Row]:
        return _iter_rows(
            self.source_connection,
            """
            SELECT asset_id AS source_asset_id,asset_key AS source_asset_key
            FROM image_assets
            ORDER BY asset_id
            """,
            batch_size=self.batch_size,
        )

    def iter_occurrences(self) -> Iterator[SourceOccurrence]:
        query = """
        SELECT occurrence_id,source_project_id,role,ordinal,raw_url,asset_id,
               parse_status,parse_error,source_field,image_type
        FROM source_image_occurrences
        ORDER BY source_project_id,role,ordinal,occurrence_id
        """
        for row in _iter_rows(
            self.source_connection, query, batch_size=self.batch_size
        ):
            yield SourceOccurrence(
                source=self.source,
                occurrence_id=str(row["occurrence_id"]),
                source_project_id=str(row["source_project_id"]),
                source_asset_id=_optional_text(row["asset_id"]),
                role=str(row["role"]),
                ordinal=int(row["ordinal"]),
                raw_url=str(row["raw_url"]),
                parse_status=str(row["parse_status"]),
                parse_error=_optional_text(row["parse_error"]),
                source_field=_optional_text(row["source_field"]),
                image_type=_optional_text(row["image_type"]),
            )


def open_divisare_sources(
    source_db_path: Path | str,
    e1_sidecar_path: Path | str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> DivisareEvidenceSources:
    return DivisareEvidenceSources(
        source_db_path, e1_sidecar_path, batch_size=batch_size
    )


def open_architizer_sources(
    source_db_path: Path | str,
    e1_sidecar_path: Path | str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> ArchitizerEvidenceSources:
    return ArchitizerEvidenceSources(
        source_db_path, e1_sidecar_path, batch_size=batch_size
    )


__all__ = [
    "ArchitizerEvidenceSources",
    "DivisareEvidenceSources",
    "E1Run",
    "SourceAssetFingerprint",
    "SourceBuilding",
    "SourceMappingError",
    "SourceMembership",
    "SourceOccurrence",
    "SourceProject",
    "open_architizer_sources",
    "open_divisare_sources",
]
