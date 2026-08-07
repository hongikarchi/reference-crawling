"""Bounded, resumable E1 image fingerprint runner.

The curated source database is always read-only.  Downloaded response bytes
live only long enough to normalize and hash one asset; the published artifact
is a provenance-rich SQLite sidecar, not an image cache.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import urljoin, urlsplit

from canonical.image_fingerprint import (
    FINGERPRINT_CONTRACT_VERSION,
    FingerprintError,
    dependency_versions,
    fingerprint_bytes,
)
from canonical.image_fingerprint_adapters import (
    SourceAsset,
    iter_architizer_source_assets,
    iter_divisare_source_assets,
)
from canonical.image_fingerprint_sidecar import (
    initialize_sidecar,
    open_sidecar,
    validate_sidecar,
)


RUNNER_VERSION = "archibe-e1-pipeline-v2"
SELECTION_VERSION = "archibe-e1-coverage-augmented-hash-v2"
DEFAULT_SAMPLE_SEED = "archibe-e1-smoke-v1"
DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_MAX_REDIRECTS = 5
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class PipelineError(RuntimeError):
    """A provenance, resume, validation, or publication failure."""


class FetchFailure(RuntimeError):
    """One bounded fetch attempt failed."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        final_url: str | None = None,
        response_mime: str | None = None,
        response_bytes: int | None = None,
        raw_response_sha256: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.http_status = http_status
        self.final_url = final_url
        self.response_mime = response_mime
        self.response_bytes = response_bytes
        self.raw_response_sha256 = raw_response_sha256


@dataclass(frozen=True)
class FetchResponse:
    status_code: int
    final_url: str
    mime_type: str | None
    body: bytes


@dataclass(frozen=True)
class PipelineResult:
    output_path: Path
    source: str
    source_sha256: str
    selection_manifest_sha256: str
    selected_assets: int
    run_status: str
    status_counts: dict[str, int]
    network_requests: int
    resumed: bool
    already_complete: bool


@dataclass(frozen=True)
class _SelectedAsset:
    asset: SourceAsset
    selection_reason: str
    selection_stratum: str
    sample_score_sha256: str | None
    sample_seed: str | None


@dataclass(frozen=True)
class _SelectionPlan:
    count: int
    eligible_inventory_count: int
    manifest_sha256: str
    selected: tuple[_SelectedAsset, ...] | None


Fetcher = Callable[[SourceAsset, int], FetchResponse]
AssetFactory = Callable[[], Iterable[SourceAsset]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_source_snapshot(path: Path) -> None:
    active = [
        Path(str(path) + suffix)
        for suffix in ("-wal", "-shm", "-journal")
        if Path(str(path) + suffix).exists()
    ]
    if active:
        raise PipelineError(
            "source SQLite has active sidecar files: "
            + ", ".join(item.name for item in active)
        )


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _dependency_manifest() -> tuple[str, str]:
    payload = json.dumps(
        dependency_versions(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return payload, hashlib.sha256(payload.encode("ascii")).hexdigest()


def _effective_runner_version(dependency_sha256: str) -> str:
    return f"{RUNNER_VERSION}+deps-{dependency_sha256[:16]}"


def _asset_base_dict(asset: SourceAsset) -> dict[str, object]:
    return {
        "effective_fetch_url": asset.effective_fetch_url,
        "fetch_profile_version": asset.fetch_profile_version,
        "format_lane": asset.format_lane,
        "normalized_url": asset.normalized_url,
        "occurrence_count": asset.occurrence_count,
        "parent_count": asset.parent_count,
        "roles": list(asset.roles),
        "selected_raw_url": asset.selected_raw_url,
        "source": asset.source,
        "source_asset_id": asset.source_asset_id,
        "source_asset_key": asset.source_asset_key,
        "source_urls": list(asset.source_urls),
    }


def _stratum(asset: SourceAsset) -> str:
    roles = set(asset.roles)
    return "|".join(
        (
            f"lane={asset.format_lane}",
            f"cover={int('cover' in roles)}",
            f"gallery={int('gallery' in roles)}",
            f"multi_parent={int(asset.parent_count > 1)}",
        )
    )


def _coverage_labels(asset: SourceAsset) -> tuple[str, ...]:
    labels = [f"lane:{asset.format_lane}"]
    if "cover" in asset.roles:
        labels.append("role:cover")
    if "gallery" in asset.roles:
        labels.append("role:gallery")
    if asset.parent_count > 1:
        labels.append("scope:multi_parent")
    return tuple(labels)


def _sample_score(asset: SourceAsset, seed: str) -> str:
    base_sha = hashlib.sha256(_json_bytes(_asset_base_dict(asset))).hexdigest()
    framed = "\0".join((SELECTION_VERSION, seed, asset.source, asset.source_asset_id, base_sha))
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def _selected_record(selected: _SelectedAsset) -> dict[str, object]:
    value = _asset_base_dict(selected.asset)
    value.update(
        {
            "selection_reason": selected.selection_reason,
            "selection_stratum": selected.selection_stratum,
            "sample_score_sha256": selected.sample_score_sha256,
            "sample_seed": selected.sample_seed,
            "selection_version": SELECTION_VERSION,
        }
    )
    return value


def _manifest_digest(selected: Iterable[_SelectedAsset]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for item in selected:
        digest.update(_json_bytes(_selected_record(item)))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _iter_checked(factory: AssetFactory, source: str) -> Iterator[SourceAsset]:
    seen: set[str] = set()
    for asset in factory():
        if asset.source != source:
            raise PipelineError(
                f"adapter yielded source {asset.source!r}, expected {source!r}"
            )
        if not asset.source_asset_id or asset.source_asset_id in seen:
            raise PipelineError(f"duplicate or empty source asset id: {asset.source_asset_id!r}")
        seen.add(asset.source_asset_id)
        yield asset


def _smoke_selection(
    factory: AssetFactory, source: str, size: int, seed: str
) -> tuple[tuple[_SelectedAsset, ...], int]:
    # Keep the globally best N hashes plus one best candidate for every required
    # coverage label.  Memory remains O(N + labels) while the whole inventory is
    # eligible for selection.
    heap: list[tuple[int, int, str, SourceAsset]] = []
    best_for_label: dict[str, tuple[str, str, SourceAsset]] = {}
    serial = 0
    for asset in _iter_checked(factory, source):
        score = _sample_score(asset, seed)
        score_number = int(score, 16)
        serial += 1
        entry = (-score_number, serial, score, asset)
        if len(heap) < size:
            heapq.heappush(heap, entry)
        elif score_number < -heap[0][0]:
            heapq.heapreplace(heap, entry)
        for label in _coverage_labels(asset):
            candidate = (score, asset.source_asset_id, asset)
            if label not in best_for_label or candidate[:2] < best_for_label[label][:2]:
                best_for_label[label] = candidate

    global_best = sorted(
        ((score, asset.source_asset_id, asset) for _, _, score, asset in heap),
        key=lambda item: item[:2],
    )
    if not global_best:
        return (), serial

    candidate_labels: dict[str, set[str]] = {}
    candidate_assets: dict[str, tuple[str, SourceAsset]] = {}
    for label, (score, asset_id, asset) in best_for_label.items():
        candidate_labels.setdefault(asset_id, set()).add(label)
        candidate_assets[asset_id] = (score, asset)

    mandatory_ids = set(candidate_assets)
    if len(mandatory_ids) > size:
        # This is only relevant for a future adapter with many format lanes.
        # Greedily retain the assets covering most still-uncovered labels.
        remaining = set(best_for_label)
        kept: set[str] = set()
        while remaining and len(kept) < size:
            chosen = min(
                (asset_id for asset_id in mandatory_ids if asset_id not in kept),
                key=lambda asset_id: (
                    -len(candidate_labels[asset_id] & remaining),
                    candidate_assets[asset_id][0],
                    asset_id,
                ),
            )
            kept.add(chosen)
            remaining.difference_update(candidate_labels[chosen])
        mandatory_ids = kept

    chosen: dict[str, tuple[str, SourceAsset]] = {
        asset_id: candidate_assets[asset_id] for asset_id in mandatory_ids
    }
    for score, asset_id, asset in global_best:
        if len(chosen) >= size:
            break
        chosen.setdefault(asset_id, (score, asset))

    selected: list[_SelectedAsset] = []
    for asset_id, (score, asset) in chosen.items():
        labels = sorted(candidate_labels.get(asset_id, set()))
        reason = "coverage:" + ",".join(labels) if labels else "hash_sample"
        selected.append(
            _SelectedAsset(asset, reason, _stratum(asset), score, seed)
        )
    return (
        tuple(
            sorted(
                selected,
                key=lambda item: (
                    item.sample_score_sha256 or "",
                    item.asset.source_asset_id,
                ),
            )
        ),
        serial,
    )


def _selection_plan(
    factory: AssetFactory,
    source: str,
    sample_size: int | None,
    sample_seed: str,
) -> _SelectionPlan:
    if sample_size is not None:
        if sample_size not in {10, 100, 1000}:
            raise ValueError("sample_size must be 10, 100, 1000, or None for full")
        selected, inventory_count = _smoke_selection(
            factory, source, sample_size, sample_seed
        )
        count, manifest = _manifest_digest(selected)
        return _SelectionPlan(count, inventory_count, manifest, selected)

    def full_items() -> Iterator[_SelectedAsset]:
        for asset in _iter_checked(factory, source):
            yield _SelectedAsset(asset, "full_inventory", _stratum(asset), None, None)

    count, manifest = _manifest_digest(full_items())
    return _SelectionPlan(count, count, manifest, None)


def _iter_plan(
    plan: _SelectionPlan, factory: AssetFactory, source: str
) -> Iterator[_SelectedAsset]:
    if plan.selected is not None:
        yield from plan.selected
        return
    for asset in _iter_checked(factory, source):
        yield _SelectedAsset(asset, "full_inventory", _stratum(asset), None, None)


def _asset_from_record(record: dict[str, object]) -> SourceAsset:
    return SourceAsset(
        source=str(record["source"]),
        source_asset_id=str(record["source_asset_id"]),
        source_asset_key=str(record["source_asset_key"]),
        normalized_url=str(record["normalized_url"]),
        selected_raw_url=str(record["selected_raw_url"]),
        effective_fetch_url=str(record["effective_fetch_url"]),
        source_urls=tuple(str(value) for value in record["source_urls"]),  # type: ignore[arg-type]
        occurrence_count=int(record["occurrence_count"]),
        parent_count=int(record["parent_count"]),
        roles=tuple(str(value) for value in record["roles"]),  # type: ignore[arg-type]
        format_lane=str(record["format_lane"]),
        fetch_profile_version=str(record["fetch_profile_version"]),
    )


def _default_asset_factory(source: str, source_db: Path) -> AssetFactory:
    if source == "divisare":
        return lambda: iter_divisare_source_assets(source_db, limit=None)
    if source == "architizer":
        return lambda: iter_architizer_source_assets(source_db, limit=None)
    raise ValueError("source must be 'divisare' or 'architizer'")


def _inventory_accounting(
    source: str,
    source_db: Path,
    eligible_count: int,
    *,
    use_default_adapter: bool,
) -> dict[str, object]:
    accounting: dict[str, object] = {
        "eligible": eligible_count,
        "adapter": source,
    }
    if not use_default_adapter:
        accounting["excluded"] = {"not_observed_by_injected_adapter": 0}
        return accounting
    uri = source_db.as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        total = int(connection.execute("SELECT count(*) FROM image_assets").fetchone()[0])
        if source == "architizer":
            placeholders = int(
                connection.execute(
                    "SELECT count(*) FROM image_assets WHERE is_placeholder_candidate=1"
                ).fetchone()[0]
            )
            excluded = {
                "placeholder_candidate": placeholders,
                "other_adapter_ineligible": total - eligible_count - placeholders,
            }
        else:
            excluded = {"adapter_ineligible_or_non_image_endpoint": total - eligible_count}
        if any(value < 0 for value in excluded.values()):
            raise PipelineError("eligible adapter inventory exceeds source asset inventory")
        accounting.update({"source_assets": total, "excluded": excluded})
        return accounting
    finally:
        connection.close()


def _host_allowed(source: str, url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return False
    if source == "divisare":
        return host == "images.divisare.com"
    if source == "architizer":
        return host == "architizer-prod.imgix.net"
    return False


class RequestsFetcher:
    """A single-attempt Requests fetcher with manual redirect validation."""

    def __init__(
        self,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ):
        if max_response_bytes < 1 or connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("fetch bounds must be positive")
        self.max_response_bytes = max_response_bytes
        self.timeout = (connect_timeout, read_timeout)
        self.max_redirects = max_redirects
        self._session = None
        self.network_requests = 0

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
        del attempt_no
        import requests

        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(
                {"User-Agent": "Archibe-E1-Fingerprint/1.0", "Accept": "image/*,*/*;q=0.1"}
            )
        url = asset.effective_fetch_url
        for redirect_no in range(self.max_redirects + 1):
            if not _host_allowed(asset.source, url):
                raise FetchFailure("redirect_host", "fetch URL is outside the source allowlist")
            try:
                self.network_requests += 1
                response = self._session.get(
                    url, timeout=self.timeout, stream=True, allow_redirects=False
                )
            except requests.RequestException as exc:
                raise FetchFailure(
                    "network", str(exc), retryable=True, final_url=url
                ) from exc
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchFailure(
                            "redirect_missing_location",
                            "redirect response has no Location",
                            http_status=response.status_code,
                            final_url=url,
                        )
                    if redirect_no >= self.max_redirects:
                        raise FetchFailure(
                            "redirect_limit",
                            "redirect limit exceeded",
                            http_status=response.status_code,
                            final_url=url,
                        )
                    next_url = urljoin(url, location)
                    if not _host_allowed(asset.source, next_url):
                        raise FetchFailure(
                            "redirect_host",
                            "redirect target is outside the source allowlist",
                            http_status=response.status_code,
                            final_url=next_url,
                        )
                    url = next_url
                    continue

                mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip() or None
                if not 200 <= response.status_code < 300:
                    raise FetchFailure(
                        f"http_{response.status_code}",
                        f"HTTP {response.status_code}",
                        retryable=response.status_code in RETRYABLE_HTTP_STATUSES,
                        http_status=response.status_code,
                        final_url=url,
                        response_mime=mime,
                    )
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > self.max_response_bytes:
                    raise FetchFailure(
                        "response_too_large",
                        "Content-Length exceeds the response byte cap",
                        http_status=response.status_code,
                        final_url=url,
                        response_mime=mime,
                        response_bytes=int(length),
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise FetchFailure(
                            "response_too_large",
                            "stream exceeds the response byte cap",
                            http_status=response.status_code,
                            final_url=url,
                            response_mime=mime,
                            response_bytes=total,
                        )
                    chunks.append(chunk)
                return FetchResponse(response.status_code, url, mime, b"".join(chunks))
            except requests.RequestException as exc:
                raise FetchFailure(
                    "network", str(exc), retryable=True, final_url=url
                ) from exc
            finally:
                response.close()
        raise AssertionError("redirect loop should terminate")


def _validate_response(asset: SourceAsset, response: FetchResponse, max_bytes: int) -> None:
    if not _host_allowed(asset.source, response.final_url):
        raise FetchFailure(
            "redirect_host",
            "final URL is outside the source allowlist",
            http_status=response.status_code,
            final_url=response.final_url,
            response_mime=response.mime_type,
            response_bytes=len(response.body),
        )
    if len(response.body) > max_bytes:
        raise FetchFailure(
            "response_too_large",
            "response exceeds the byte cap",
            http_status=response.status_code,
            final_url=response.final_url,
            response_mime=response.mime_type,
            response_bytes=len(response.body),
        )
    if not 200 <= response.status_code < 300:
        raise FetchFailure(
            f"http_{response.status_code}",
            f"HTTP {response.status_code}",
            retryable=response.status_code in RETRYABLE_HTTP_STATUSES,
            http_status=response.status_code,
            final_url=response.final_url,
            response_mime=response.mime_type,
            response_bytes=len(response.body),
        )
    if not response.body:
        raise FetchFailure(
            "empty_response",
            "successful HTTP response is empty",
            http_status=response.status_code,
            final_url=response.final_url,
            response_mime=response.mime_type,
            response_bytes=0,
        )


def _acquire_lock(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PipelineError(f"exclusive runner lock already exists: {path}") from exc
    os.write(descriptor, f"pid={os.getpid()} started={_utc_now()}\n".encode("ascii"))
    return descriptor


def _release_lock(path: Path, descriptor: int) -> None:
    os.close(descriptor)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _initialize_run(
    partial: Path,
    source: str,
    source_db: Path,
    source_sha: str,
    dependency_json: str,
    dependency_sha: str,
    runner_version: str,
    plan: _SelectionPlan,
    inventory_accounting: dict[str, object],
    factory: AssetFactory,
) -> str:
    connection = initialize_sidecar(partial)
    run_id = f"e1-{source}-{source_sha[:12]}-{plan.manifest_sha256[:16]}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO fingerprint_runs(
              run_id,source_name,source_db_path,source_db_sha256_before,
              fingerprint_contract_version,runner_version,
              dependency_manifest_json,dependency_manifest_sha256,
              selection_manifest_sha256,status,started_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'running',?)
            """,
            (
                run_id,
                source,
                str(source_db.resolve()),
                source_sha,
                FINGERPRINT_CONTRACT_VERSION,
                runner_version,
                dependency_json,
                dependency_sha,
                plan.manifest_sha256,
                _utc_now(),
            ),
        )
        count = 0
        digest = hashlib.sha256()
        for rank, selected in enumerate(_iter_plan(plan, factory, source), 1):
            record = _selected_record(selected)
            record_bytes = _json_bytes(record)
            digest.update(record_bytes)
            digest.update(b"\n")
            base_sha = hashlib.sha256(_json_bytes(_asset_base_dict(selected.asset))).hexdigest()
            connection.execute(
                """
                INSERT INTO source_assets(
                  run_id,source_asset_id,selection_rank,canonical_url,fetch_url,
                  source_record_sha256,provenance_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    selected.asset.source_asset_id,
                    rank,
                    selected.asset.normalized_url,
                    selected.asset.effective_fetch_url,
                    base_sha,
                    record_bytes.decode("ascii"),
                ),
            )
            connection.execute(
                "INSERT INTO fingerprints(run_id,source_asset_id,status) VALUES(?,?,'pending')",
                (run_id, selected.asset.source_asset_id),
            )
            count += 1
        if count != plan.count or digest.hexdigest() != plan.manifest_sha256:
            raise PipelineError("selection changed between manifest and sidecar insertion")
        connection.executemany(
            """INSERT INTO validations(
              run_id,validation_name,severity,passed,expected,actual,detail
            ) VALUES(?,?,'info',1,?,?,?)""",
            (
                (
                    run_id,
                    "eligible_inventory_accounting",
                    str(plan.eligible_inventory_count),
                    str(plan.eligible_inventory_count),
                    json.dumps(inventory_accounting, sort_keys=True, separators=(",", ":")),
                ),
                (
                    run_id,
                    "selection_manifest",
                    str(plan.count),
                    str(count),
                    json.dumps(
                        {
                            "manifest_sha256": plan.manifest_sha256,
                            "selection_version": SELECTION_VERSION,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            ),
        )
        connection.commit()
        return run_id
    except Exception:
        connection.rollback()
        connection.close()
        raise
    finally:
        if connection:
            try:
                connection.close()
            except Exception:
                pass


def _check_resume(
    path: Path,
    source: str,
    source_db: Path,
    source_sha: str,
    dependency_json: str,
    dependency_sha: str,
    runner_version: str,
    plan: _SelectionPlan,
) -> tuple[str, str]:
    connection = open_sidecar(path, readonly=True)
    try:
        rows = connection.execute("SELECT * FROM fingerprint_runs").fetchall()
        if len(rows) != 1:
            raise PipelineError("sidecar must contain exactly one fingerprint run")
        row = rows[0]
        expected = {
            "source_name": source,
            "source_db_path": str(source_db.resolve()),
            "source_db_sha256_before": source_sha,
            "fingerprint_contract_version": FINGERPRINT_CONTRACT_VERSION,
            "runner_version": runner_version,
            "dependency_manifest_json": dependency_json,
            "dependency_manifest_sha256": dependency_sha,
            "selection_manifest_sha256": plan.manifest_sha256,
        }
        mismatches = [
            key for key, value in expected.items() if str(row[key]) != str(value)
        ]
        if mismatches:
            raise PipelineError("resume provenance mismatch: " + ", ".join(mismatches))

        digest = hashlib.sha256()
        count = 0
        for asset_row in connection.execute(
            "SELECT source_record_sha256,provenance_json FROM source_assets "
            "WHERE run_id=? ORDER BY selection_rank",
            (row["run_id"],),
        ):
            record = json.loads(asset_row["provenance_json"])
            base_sha = hashlib.sha256(
                _json_bytes({key: record[key] for key in _asset_base_dict(_asset_from_record(record))})
            ).hexdigest()
            if base_sha != asset_row["source_record_sha256"]:
                raise PipelineError("stored source asset provenance hash mismatch")
            digest.update(_json_bytes(record))
            digest.update(b"\n")
            count += 1
        if count != plan.count or digest.hexdigest() != plan.manifest_sha256:
            raise PipelineError("stored selection does not match the requested manifest")
        return str(row["run_id"]), str(row["status"])
    finally:
        connection.close()


def _attempt_row(
    *,
    run_id: str,
    asset: SourceAsset,
    attempt_no: int,
    started_at: str,
    completed_at: str,
    elapsed_ms: int,
    outcome: str,
    response: FetchResponse | None = None,
    failure: FetchFailure | None = None,
) -> tuple[object, ...]:
    body_sha = hashlib.sha256(response.body).hexdigest() if response is not None else None
    return (
        run_id,
        asset.source_asset_id,
        attempt_no,
        asset.effective_fetch_url,
        started_at,
        completed_at,
        elapsed_ms,
        outcome,
        response.status_code if response else failure.http_status if failure else None,
        response.mime_type if response else failure.response_mime if failure else None,
        len(response.body) if response else failure.response_bytes if failure else None,
        response.final_url if response else failure.final_url if failure else None,
        body_sha if response else failure.raw_response_sha256 if failure else None,
        None if outcome == "success" else failure.kind if failure else "fetch",
        None if outcome == "success" else str(failure) if failure else "fetch failed",
    )


_INSERT_ATTEMPT = """
INSERT INTO fetch_attempts(
  run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
  elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
  raw_response_sha256,error_kind,error_message
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _persist_attempt(connection: sqlite3.Connection, row: tuple[object, ...]) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_INSERT_ATTEMPT, row)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _process_asset(
    connection: sqlite3.Connection,
    run_id: str,
    asset: SourceAsset,
    fetcher: Fetcher,
    max_attempts: int,
    max_response_bytes: int,
    sleep: Callable[[float], None],
) -> int:
    if not _host_allowed(asset.source, asset.effective_fetch_url):
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE fingerprints SET status='skipped',completed_at=?,
               error_kind='invalid_fetch_url',error_message=?
               WHERE run_id=? AND source_asset_id=? AND status='pending'""",
            (_utc_now(), "fetch URL is outside the HTTPS source allowlist", run_id, asset.source_asset_id),
        )
        connection.commit()
        return 0

    previous_attempts = int(
        connection.execute(
            """SELECT coalesce(max(attempt_no),0)
               FROM fetch_attempts WHERE run_id=? AND source_asset_id=?""",
            (run_id, asset.source_asset_id),
        ).fetchone()[0]
    )
    if previous_attempts >= max_attempts:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """UPDATE fingerprints SET status='failed',completed_at=?,
                   error_kind='attempt_budget_exhausted',error_message=?
                   WHERE run_id=? AND source_asset_id=? AND status='pending'""",
                (
                    _utc_now(),
                    "all fetch attempts were already consumed before resume",
                    run_id,
                    asset.source_asset_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return 0

    request_count = 0
    response: FetchResponse | None = None
    terminal_failure: FetchFailure | None = None
    selected_attempt_no: int | None = None
    for attempt_no in range(previous_attempts + 1, max_attempts + 1):
        started_at = _utc_now()
        started = time.monotonic()
        request_count += 1
        try:
            candidate = fetcher(asset, attempt_no)
            _validate_response(asset, candidate, max_response_bytes)
            response = candidate
            completed_at = _utc_now()
            elapsed = max(0, round((time.monotonic() - started) * 1000))
            _persist_attempt(
                connection,
                _attempt_row(
                    run_id=run_id,
                    asset=asset,
                    attempt_no=attempt_no,
                    started_at=started_at,
                    completed_at=completed_at,
                    elapsed_ms=elapsed,
                    outcome="success",
                    response=response,
                ),
            )
            selected_attempt_no = attempt_no
            break
        except FetchFailure as exc:
            terminal_failure = exc
            completed_at = _utc_now()
            elapsed = max(0, round((time.monotonic() - started) * 1000))
            _persist_attempt(
                connection,
                _attempt_row(
                    run_id=run_id,
                    asset=asset,
                    attempt_no=attempt_no,
                    started_at=started_at,
                    completed_at=completed_at,
                    elapsed_ms=elapsed,
                    outcome="failed",
                    failure=exc,
                ),
            )
            if not exc.retryable or attempt_no == max_attempts:
                break
            sleep(min(4.0, float(2 ** (attempt_no - 1))))
        except BaseException as exc:
            completed_at = _utc_now()
            elapsed = max(0, round((time.monotonic() - started) * 1000))
            interruption = FetchFailure(
                "interrupted",
                f"{type(exc).__name__}: fetch attempt interrupted",
                retryable=True,
                final_url=asset.effective_fetch_url,
            )
            _persist_attempt(
                connection,
                _attempt_row(
                    run_id=run_id,
                    asset=asset,
                    attempt_no=attempt_no,
                    started_at=started_at,
                    completed_at=completed_at,
                    elapsed_ms=elapsed,
                    outcome="failed",
                    failure=interruption,
                ),
            )
            raise

    fingerprint = None
    fingerprint_failure: FingerprintError | None = None
    if response is not None:
        try:
            fingerprint = fingerprint_bytes(
                response.body, max_input_bytes=max_response_bytes
            )
        except FingerprintError as exc:
            fingerprint_failure = exc

    connection.execute("BEGIN IMMEDIATE")
    try:
        completed_at = _utc_now()
        if fingerprint is not None and response is not None:
            metadata = fingerprint.as_dict()
            cursor = connection.execute(
                """
                UPDATE fingerprints SET
                  status='success',selected_attempt_no=?,raw_response_sha256=?,
                  normalized_pixel_sha256=?,phash_hex=?,decoded_format=?,
                  original_width=?,original_height=?,normalized_width=?,normalized_height=?,
                  metadata_json=?,completed_at=?,error_kind=NULL,error_message=NULL
                WHERE run_id=? AND source_asset_id=? AND status='pending'
                """,
                (
                    selected_attempt_no,
                    fingerprint.response_sha256,
                    fingerprint.pixel_sha256,
                    fingerprint.phash256,
                    fingerprint.decoded_format,
                    fingerprint.source_width,
                    fingerprint.source_height,
                    fingerprint.normalized_width,
                    fingerprint.normalized_height,
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    completed_at,
                    run_id,
                    asset.source_asset_id,
                ),
            )
        elif response is not None and fingerprint_failure is not None:
            cursor = connection.execute(
                """
                UPDATE fingerprints SET status='failed',selected_attempt_no=?,
                  raw_response_sha256=?,completed_at=?,error_kind=?,error_message=?
                WHERE run_id=? AND source_asset_id=? AND status='pending'
                """,
                (
                    selected_attempt_no,
                    hashlib.sha256(response.body).hexdigest(),
                    completed_at,
                    f"decode:{fingerprint_failure.kind}",
                    str(fingerprint_failure),
                    run_id,
                    asset.source_asset_id,
                ),
            )
        else:
            failure = terminal_failure or FetchFailure("fetch", "fetch failed")
            cursor = connection.execute(
                """
                UPDATE fingerprints SET status='failed',completed_at=?,error_kind=?,error_message=?
                WHERE run_id=? AND source_asset_id=? AND status='pending'
                """,
                (completed_at, failure.kind, str(failure), run_id, asset.source_asset_id),
            )
        if cursor.rowcount != 1:
            raise PipelineError("pending fingerprint row disappeared during asset transaction")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return request_count


def _finish_run(partial: Path, run_id: str, source_db: Path, source_sha: str) -> None:
    _assert_source_snapshot(source_db)
    ending_sha = _sha256_file(source_db)
    if ending_sha != source_sha:
        connection = open_sidecar(partial, readonly=False)
        try:
            connection.execute(
                """INSERT OR REPLACE INTO validations(
                  run_id,validation_name,severity,passed,expected,actual,detail
                ) VALUES(?, 'source_sha_unchanged','error',0,?,?,?)""",
                (run_id, source_sha, ending_sha, "source database changed during the run"),
            )
            connection.commit()
        finally:
            connection.close()
        raise PipelineError("source database SHA changed during the run")

    validation = validate_sidecar(partial)
    connection = open_sidecar(partial, readonly=False)
    try:
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status,count(*) FROM fingerprints WHERE run_id=? GROUP BY status",
                (run_id,),
            )
        }
        asset_count = int(
            connection.execute(
                "SELECT count(*) FROM source_assets WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        fingerprint_count = sum(counts.values())
        success_count = counts.get("success", 0)
        failure_count = counts.get("failed", 0) + counts.get("skipped", 0)
        checks = (
            ("source_sha_unchanged", source_sha == ending_sha, source_sha, ending_sha, None),
            ("quick_check", validation.quick_check == "ok", "ok", validation.quick_check, None),
            ("integrity_check", validation.integrity_check == "ok", "ok", validation.integrity_check, None),
            ("foreign_key_check", validation.foreign_key_violations == 0, "0", str(validation.foreign_key_violations), None),
            ("fingerprint_accounting", asset_count == fingerprint_count, str(asset_count), str(fingerprint_count), json.dumps(counts, sort_keys=True)),
            ("no_pending", counts.get("pending", 0) == 0, "0", str(counts.get("pending", 0)), None),
            ("successful_fingerprints", success_count > 0, ">0", str(success_count), json.dumps(counts, sort_keys=True)),
            ("sidecar_semantics", all(value == 0 for _, value in validation.semantic_violations), "0", str(sum(value for _, value in validation.semantic_violations)), json.dumps(dict(validation.semantic_violations), sort_keys=True)),
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            """INSERT INTO validations(
              run_id,validation_name,severity,passed,expected,actual,detail
            ) VALUES(?,?,'error',?,?,?,?)
            ON CONFLICT(run_id,validation_name) DO UPDATE SET
              severity=excluded.severity,passed=excluded.passed,
              expected=excluded.expected,actual=excluded.actual,detail=excluded.detail""",
            [
                (run_id, name, int(passed), expected, actual, detail)
                for name, passed, expected, actual, detail in checks
            ],
        )
        passed = all(check[1] for check in checks)
        terminal_status = (
            "complete"
            if passed and failure_count == 0
            else "complete_with_failures"
            if passed
            else "failed_validation"
        )
        connection.execute(
            """UPDATE fingerprint_runs SET status=?,source_db_sha256_after=?,
               completed_at=?,error=? WHERE run_id=? AND status='running'""",
            (
                terminal_status,
                ending_sha,
                _utc_now(),
                None if passed else "one or more final validations failed",
                run_id,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    final_validation = validate_sidecar(partial)
    if not passed or not final_validation.passed:
        raise PipelineError("fingerprint sidecar failed final validation")


def _publish_hardlink(partial: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"refusing to clobber output: {output}")
    try:
        os.link(partial, output)
    except FileExistsError:
        raise
    except OSError as exc:
        raise PipelineError(
            "atomic hard-link publication failed; partial was preserved"
        ) from exc
    partial.unlink()


def _result(path: Path, source: str, requests: int, resumed: bool, already: bool) -> PipelineResult:
    connection = open_sidecar(path, readonly=True)
    try:
        run = connection.execute("SELECT * FROM fingerprint_runs").fetchone()
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status,count(*) FROM fingerprints GROUP BY status"
            )
        }
        return PipelineResult(
            output_path=path,
            source=source,
            source_sha256=str(run["source_db_sha256_before"]),
            selection_manifest_sha256=str(run["selection_manifest_sha256"]),
            selected_assets=sum(counts.values()),
            run_status=str(run["status"]),
            status_counts=counts,
            network_requests=requests,
            resumed=resumed,
            already_complete=already,
        )
    finally:
        connection.close()


def run_image_fingerprint_pipeline(
    *,
    source: str,
    source_db: Path | str,
    output: Path | str,
    sample_size: int | None,
    resume: bool = False,
    sample_seed: str = DEFAULT_SAMPLE_SEED,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    asset_factory: AssetFactory | None = None,
    fetcher: Fetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PipelineResult:
    """Run or exactly resume one source-neutral E1 sidecar build."""

    if source not in {"divisare", "architizer"}:
        raise ValueError("source must be 'divisare' or 'architizer'")
    if max_response_bytes < 1 or max_attempts < 1:
        raise ValueError("max_response_bytes and max_attempts must be positive")
    source_path = Path(source_db).resolve()
    output_path = Path(output).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(output_path) + ".partial")
    lock_path = Path(str(output_path) + ".lock")
    descriptor = _acquire_lock(lock_path)
    owned_fetcher = None
    try:
        _assert_source_snapshot(source_path)
        source_sha = _sha256_file(source_path)
        dependency_json, dependency_sha = _dependency_manifest()
        runner_version = _effective_runner_version(dependency_sha)
        factory = asset_factory or _default_asset_factory(source, source_path)
        plan = _selection_plan(factory, source, sample_size, sample_seed)
        inventory_accounting = _inventory_accounting(
            source,
            source_path,
            plan.eligible_inventory_count,
            use_default_adapter=asset_factory is None,
        )

        if output_path.exists():
            if not resume:
                raise FileExistsError(f"refusing to clobber output: {output_path}")
            _, status = _check_resume(
                output_path, source, source_path, source_sha, dependency_json,
                dependency_sha, runner_version, plan
            )
            if status not in {"complete", "complete_with_failures"} or not validate_sidecar(output_path).passed:
                raise PipelineError("published sidecar is not a valid complete run")
            return _result(output_path, source, 0, True, True)

        if partial.exists() and not resume:
            raise FileExistsError(f"partial exists; use --resume or choose another output: {partial}")
        if not partial.exists() and resume:
            raise FileNotFoundError(f"resume partial does not exist: {partial}")

        if partial.exists():
            run_id, status = _check_resume(
                partial, source, source_path, source_sha, dependency_json,
                dependency_sha, runner_version, plan
            )
            if status in {"complete", "complete_with_failures"}:
                if not validate_sidecar(partial).passed:
                    raise PipelineError("complete partial sidecar failed validation")
                _publish_hardlink(partial, output_path)
                return _result(output_path, source, 0, True, True)
            if status != "running":
                raise PipelineError(f"terminal {status!r} sidecar cannot be resumed")
            resumed = True
        else:
            run_id = _initialize_run(
                partial, source, source_path, source_sha, dependency_json,
                dependency_sha, runner_version, plan, inventory_accounting, factory
            )
            resumed = False

        if fetcher is None:
            owned_fetcher = RequestsFetcher(
                max_response_bytes=max_response_bytes,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )
            active_fetcher: Fetcher = owned_fetcher
        else:
            active_fetcher = fetcher

        connection = open_sidecar(partial, readonly=False)
        requests_made = 0
        try:
            last_rank = 0
            while True:
                rows = connection.execute(
                    """SELECT s.selection_rank,s.provenance_json
                       FROM source_assets s JOIN fingerprints f USING(run_id,source_asset_id)
                       WHERE s.run_id=? AND f.status='pending'
                         AND s.selection_rank>?
                       ORDER BY s.selection_rank LIMIT 128""",
                    (run_id, last_rank),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    asset = _asset_from_record(json.loads(row["provenance_json"]))
                    requests_made += _process_asset(
                        connection, run_id, asset, active_fetcher, max_attempts,
                        max_response_bytes, sleep
                    )
                    last_rank = int(row["selection_rank"])
        finally:
            connection.close()

        if owned_fetcher is not None:
            requests_made = owned_fetcher.network_requests
        _finish_run(partial, run_id, source_path, source_sha)
        _publish_hardlink(partial, output_path)
        return _result(output_path, source, requests_made, resumed, False)
    finally:
        if owned_fetcher is not None:
            owned_fetcher.close()
        _release_lock(lock_path, descriptor)
