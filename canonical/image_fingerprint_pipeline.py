"""Bounded, resumable E1 image fingerprint runner.

The curated source database is always read-only.  Downloaded response bytes
live only long enough to normalize and hash one asset; the published artifact
is a provenance-rich SQLite sidecar, not an image cache.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import urljoin, urlsplit

from canonical.image_fingerprint import (
    FINGERPRINT_CONTRACT_VERSION,
    FingerprintError,
    ImageFingerprint,
    dependency_versions,
    fingerprint_bytes,
)
from canonical.image_fingerprint_adapters import (
    InventoryDecision,
    SourceAsset,
    SourceAssetExclusion,
    canonical_source_record_json,
    inventory_decision_manifest_record,
    iter_architizer_source_assets,
    iter_architizer_source_inventory,
    iter_divisare_source_assets,
    iter_divisare_source_inventory,
    source_asset_record_json,
    source_record_sha256,
)
from canonical.image_fingerprint_sidecar import (
    REQUIRED_VALIDATIONS,
    initialize_sidecar,
    open_sidecar,
    recover_sidecar,
    validate_sidecar,
)
from canonical.image_fingerprint_validator import validate_image_fingerprint_sidecar


RUNNER_VERSION = "archibe-e1-pipeline-v2"
RETRY_POLICY_VERSION = "archibe-e1-retry-v2"
SELECTION_VERSION = "archibe-e1-coverage-augmented-hash-v2"
DEFAULT_SAMPLE_SEED = "archibe-e1-smoke-v1"
DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DEFAULT_REQUESTS_PER_SECOND = 2.0
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 8
DEFAULT_COOLDOWN_SECONDS = 30.0
DEFAULT_PENDING_BATCH_SIZE = 128
INITIALIZATION_COMMIT_SIZE = 5_000
DEFAULT_BACKOFF_MAX_SECONDS = 60.0
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
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.http_status = http_status
        self.final_url = final_url
        self.response_mime = response_mime
        self.response_bytes = response_bytes
        self.raw_response_sha256 = raw_response_sha256
        self.retry_after_seconds = retry_after_seconds


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
    source_total_count: int = 0
    excluded_inventory_count: int = 0
    inventory_manifest_sha256: str = ""
    exclusion_manifest_sha256: str = ""


@dataclass(frozen=True)
class _InventoryPlan:
    source_total_count: int
    eligible_count: int
    excluded_count: int
    inventory_manifest_sha256: str
    exclusion_manifest_sha256: str


Fetcher = Callable[[SourceAsset, int], FetchResponse]
FetcherFactory = Callable[[], Fetcher]
AssetFactory = Callable[[], Iterable[SourceAsset]]
InventoryFactory = Callable[[], Iterable[InventoryDecision]]


@dataclass(frozen=True)
class _AttemptTask:
    asset: SourceAsset
    attempt_no: int
    delay_seconds: float = 0.0


@dataclass(frozen=True)
class _AttemptResult:
    task: _AttemptTask
    started_at: str
    completed_at: str
    elapsed_ms: int
    response: FetchResponse | None
    failure: FetchFailure | None
    fatal: BaseException | None
    fingerprint: ImageFingerprint | None
    fingerprint_failure: FingerprintError | None
    network_requests: int
    worker_no: int
    not_started: bool = False


class _GlobalRateLimiter:
    """Allocate site-wide request slots without retaining per-request state."""

    def __init__(
        self,
        requests_per_second: float,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        stop_event: threading.Event | None = None,
    ) -> None:
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._stop_event = stop_event

    def acquire(self) -> bool:
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                return False
            with self._lock:
                now = self._clock()
                delay = max(0.0, self._next_allowed - now)
                if delay <= 0:
                    self._next_allowed = now + self._interval
                    return True
            # Sleeping outside the mutex lets a concurrent overload response
            # extend the global not-before time.  Recheck after every wake so
            # a worker that was already waiting cannot leak through cooldown.
            if not _sleep_unless_stopped(
                delay, stop_event=self._stop_event, sleep=self._sleep
            ):
                return False

    def defer(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._next_allowed = max(
                self._next_allowed, self._clock() + seconds
            )


class _WorkerFetchers:
    """Create one fetcher per executor thread and close them after shutdown."""

    def __init__(self, factory: FetcherFactory) -> None:
        self._factory = factory
        self._local = threading.local()
        self._created: list[Fetcher] = []
        self._lock = threading.Lock()

    def current(self) -> tuple[Fetcher, int]:
        fetcher = getattr(self._local, "fetcher", None)
        if fetcher is None:
            fetcher = self._factory()
            with self._lock:
                self._created.append(fetcher)
                worker_no = len(self._created)
            self._local.fetcher = fetcher
            self._local.worker_no = worker_no
        return fetcher, int(self._local.worker_no)

    def close(self) -> None:
        for fetcher in self._created:
            closer = getattr(fetcher, "close", None)
            if callable(closer):
                closer()


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


def _iter_checked(
    factory: AssetFactory,
    source: str,
    *,
    ordered: bool = False,
) -> Iterator[SourceAsset]:
    seen: set[str] | None = None if ordered else set()
    previous_id: str | None = None
    for asset in factory():
        if asset.source != source:
            raise PipelineError(
                f"adapter yielded source {asset.source!r}, expected {source!r}"
            )
        if not asset.source_asset_id:
            raise PipelineError(f"duplicate or empty source asset id: {asset.source_asset_id!r}")
        if ordered:
            if previous_id is not None and asset.source_asset_id <= previous_id:
                raise PipelineError(
                    "default adapter source asset IDs must be strictly increasing: "
                    f"{previous_id!r}, {asset.source_asset_id!r}"
                )
            previous_id = asset.source_asset_id
        else:
            assert seen is not None
            if asset.source_asset_id in seen:
                raise PipelineError(
                    f"duplicate or empty source asset id: {asset.source_asset_id!r}"
                )
            seen.add(asset.source_asset_id)
        yield asset


def _smoke_selection(
    factory: AssetFactory,
    source: str,
    size: int,
    seed: str,
    *,
    ordered: bool = False,
) -> tuple[tuple[_SelectedAsset, ...], int]:
    # Keep the globally best N hashes plus one best candidate for every required
    # coverage label.  Memory remains O(N + labels) while the whole inventory is
    # eligible for selection.
    heap: list[tuple[int, int, str, SourceAsset]] = []
    best_for_label: dict[str, tuple[str, str, SourceAsset]] = {}
    serial = 0
    for asset in _iter_checked(factory, source, ordered=ordered):
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
    *,
    ordered: bool = False,
) -> _SelectionPlan:
    if sample_size is not None:
        if sample_size not in {10, 100, 1000}:
            raise ValueError("sample_size must be 10, 100, 1000, or None for full")
        selected, inventory_count = _smoke_selection(
            factory, source, sample_size, sample_seed, ordered=ordered
        )
        count, manifest = _manifest_digest(selected)
        return _SelectionPlan(count, inventory_count, manifest, selected)

    def full_items() -> Iterator[_SelectedAsset]:
        for asset in _iter_checked(factory, source, ordered=ordered):
            yield _SelectedAsset(asset, "full_inventory", _stratum(asset), None, None)

    count, manifest = _manifest_digest(full_items())
    return _SelectionPlan(count, count, manifest, None)


def _iter_plan(
    plan: _SelectionPlan,
    factory: AssetFactory,
    source: str,
    *,
    ordered: bool = False,
) -> Iterator[_SelectedAsset]:
    if plan.selected is not None:
        yield from plan.selected
        return
    for asset in _iter_checked(factory, source, ordered=ordered):
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


def _default_inventory_factory(source: str, source_db: Path) -> InventoryFactory:
    if source == "divisare":
        return lambda: iter_divisare_source_inventory(source_db)
    if source == "architizer":
        return lambda: iter_architizer_source_inventory(source_db)
    raise ValueError("source must be 'divisare' or 'architizer'")


def _inventory_from_assets(factory: AssetFactory) -> InventoryFactory:
    return lambda: iter(factory())


def _iter_inventory_checked(
    factory: InventoryFactory,
    source: str,
    *,
    ordered: bool,
) -> Iterator[InventoryDecision]:
    seen: set[str] | None = None if ordered else set()
    previous_id: str | None = None
    for decision in factory():
        if not isinstance(decision, (SourceAsset, SourceAssetExclusion)):
            raise PipelineError(
                f"inventory adapter yielded unsupported decision: {type(decision)!r}"
            )
        if decision.source != source:
            raise PipelineError(
                f"adapter yielded source {decision.source!r}, expected {source!r}"
            )
        asset_id = decision.source_asset_id
        if not asset_id:
            raise PipelineError("inventory adapter yielded an empty source asset id")
        if ordered:
            if previous_id is not None and asset_id <= previous_id:
                raise PipelineError(
                    "default inventory IDs must be strictly increasing: "
                    f"{previous_id!r}, {asset_id!r}"
                )
            previous_id = asset_id
        else:
            assert seen is not None
            if asset_id in seen:
                raise PipelineError(f"duplicate inventory source asset id: {asset_id!r}")
            seen.add(asset_id)
        yield decision


def _inventory_plan(
    factory: InventoryFactory,
    source: str,
    *,
    ordered: bool,
) -> _InventoryPlan:
    inventory_digest = hashlib.sha256()
    exclusion_digest = hashlib.sha256()
    total = 0
    eligible = 0
    excluded = 0
    for decision in _iter_inventory_checked(factory, source, ordered=ordered):
        record_bytes = _json_bytes(inventory_decision_manifest_record(decision))
        inventory_digest.update(record_bytes)
        inventory_digest.update(b"\n")
        total += 1
        if isinstance(decision, SourceAssetExclusion):
            exclusion_digest.update(record_bytes)
            exclusion_digest.update(b"\n")
            excluded += 1
        else:
            eligible += 1
    if total != eligible + excluded:
        raise PipelineError("source inventory accounting is inconsistent")
    return _InventoryPlan(
        source_total_count=total,
        eligible_count=eligible,
        excluded_count=excluded,
        inventory_manifest_sha256=inventory_digest.hexdigest(),
        exclusion_manifest_sha256=exclusion_digest.hexdigest(),
    )


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


def _parse_retry_after(
    value: str | None,
    *,
    wall_time: Callable[[], float] = time.time,
) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        seconds = float(stripped)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            seconds = parsed.timestamp() - wall_time()
        except (OSError, OverflowError, ValueError):
            return None
    if not math.isfinite(seconds):
        return None
    if seconds < 0:
        return 0.0
    return seconds


class RequestsFetcher:
    """A single-attempt Requests fetcher with manual redirect validation."""

    def __init__(
        self,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        request_gate: Callable[[], bool] | None = None,
        gate_first_request: bool = True,
        wall_time: Callable[[], float] = time.time,
    ):
        if (
            max_response_bytes < 1
            or not math.isfinite(connect_timeout)
            or connect_timeout <= 0
            or not math.isfinite(read_timeout)
            or read_timeout <= 0
        ):
            raise ValueError("fetch bounds must be positive")
        self.max_response_bytes = max_response_bytes
        self.timeout = (connect_timeout, read_timeout)
        self.max_redirects = max_redirects
        self.request_gate = request_gate
        self.gate_first_request = gate_first_request
        self.wall_time = wall_time
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
                if (
                    self.request_gate is not None
                    and (self.gate_first_request or redirect_no > 0)
                    and not self.request_gate()
                ):
                    raise FetchFailure(
                        "cancelled",
                        "request cancelled before start",
                        retryable=True,
                        final_url=url,
                    )
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
                        retry_after_seconds=_parse_retry_after(
                            response.headers.get("Retry-After"),
                            wall_time=self.wall_time,
                        ),
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
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PipelineError(f"exclusive runner lock is held: {path}") from exc
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(
            descriptor,
            f"pid={os.getpid()} started={_utc_now()}\n".encode("ascii"),
        )
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release_lock(path: Path, descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _sleep_unless_stopped(
    seconds: float,
    *,
    stop_event: threading.Event | None,
    sleep: Callable[[float], None],
) -> bool:
    if seconds <= 0:
        return stop_event is None or not stop_event.is_set()
    if stop_event is not None and sleep is time.sleep:
        return not stop_event.wait(seconds)
    sleep(seconds)
    return stop_event is None or not stop_event.is_set()


def _recover_partial(path: Path) -> None:
    """Let SQLite roll back a hot journal before any immutable read."""
    recover_sidecar(path)


def _initialize_run(
    partial: Path,
    source: str,
    source_db: Path,
    source_sha: str,
    dependency_json: str,
    dependency_sha: str,
    runner_version: str,
    max_attempts: int,
    plan: _SelectionPlan,
    sample_seed: str,
    inventory_factory: InventoryFactory,
    ordered_inventory: bool,
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
              retry_policy_version,max_attempts,
              dependency_manifest_json,dependency_manifest_sha256,
              selection_manifest_sha256,selection_mode,selection_count,
              sample_seed,selection_version,source_inventory_manifest_sha256,
              exclusion_manifest_sha256,source_total_count,eligible_count,
              excluded_count,status,started_at,initialization_updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'initializing',?,?)
            """,
            (
                run_id,
                source,
                str(source_db.resolve()),
                source_sha,
                FINGERPRINT_CONTRACT_VERSION,
                runner_version,
                RETRY_POLICY_VERSION,
                max_attempts,
                dependency_json,
                dependency_sha,
                plan.manifest_sha256,
                "sample" if plan.selected is not None else "full",
                plan.count,
                sample_seed if plan.selected is not None else None,
                SELECTION_VERSION,
                plan.inventory_manifest_sha256,
                plan.exclusion_manifest_sha256,
                plan.source_total_count,
                plan.eligible_inventory_count,
                plan.excluded_inventory_count,
                _utc_now(),
                _utc_now(),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _resume_initialization(
        partial,
        run_id,
        source,
        plan,
        inventory_factory,
        ordered_inventory=ordered_inventory,
    )
    return run_id


def _commit_initialization_batch(
    connection: sqlite3.Connection,
    run_id: str,
    inventory_count: int,
    selected_count: int,
    excluded_count: int,
) -> None:
    connection.execute(
        """UPDATE fingerprint_runs SET
             initialized_inventory_count=?,initialized_selected_count=?,
             initialized_excluded_count=?,initialization_updated_at=?
           WHERE run_id=? AND status='initializing'""",
        (
            inventory_count,
            selected_count,
            excluded_count,
            _utc_now(),
            run_id,
        ),
    )
    connection.commit()


def _resume_initialization(
    partial: Path,
    run_id: str,
    source: str,
    plan: _SelectionPlan,
    inventory_factory: InventoryFactory,
    *,
    ordered_inventory: bool,
) -> None:
    connection = open_sidecar(partial, readonly=False)
    selected_by_id: dict[str, tuple[int, _SelectedAsset]] | None
    if plan.selected is None:
        selected_by_id = None
    else:
        selected_by_id = {
            item.asset.source_asset_id: (rank, item)
            for rank, item in enumerate(plan.selected, 1)
        }
    try:
        run = connection.execute(
            "SELECT * FROM fingerprint_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None or str(run["status"]) != "initializing":
            raise PipelineError("initialization resume requires one initializing run")
        initialized_inventory = int(run["initialized_inventory_count"])
        initialized_selected = int(run["initialized_selected_count"])
        initialized_excluded = int(run["initialized_excluded_count"])
        inventory_count = 0
        eligible_count = 0
        selected_count = 0
        excluded_count = 0
        transaction_open = False

        for inventory_rank, decision in enumerate(
            _iter_inventory_checked(
                inventory_factory, source, ordered=ordered_inventory
            ),
            1,
        ):
            inventory_count = inventory_rank
            selected: _SelectedAsset | None = None
            selection_rank: int | None = None
            if isinstance(decision, SourceAssetExclusion):
                excluded_count += 1
            else:
                eligible_count += 1
                if selected_by_id is None:
                    selection_rank = eligible_count
                    selected = _SelectedAsset(
                        decision,
                        "full_inventory",
                        _stratum(decision),
                        None,
                        None,
                    )
                else:
                    selected_pair = selected_by_id.get(decision.source_asset_id)
                    if selected_pair is not None:
                        selection_rank, selected = selected_pair
                        if selected.asset != decision:
                            raise PipelineError(
                                "selected asset changed between planning and initialization"
                            )
                if selected is not None:
                    selected_count += 1

            if inventory_rank <= initialized_inventory:
                if isinstance(decision, SourceAssetExclusion):
                    existing = connection.execute(
                        """SELECT * FROM source_asset_exclusions
                           WHERE run_id=? AND inventory_rank=?""",
                        (run_id, inventory_rank),
                    ).fetchone()
                    if existing is None or any(
                        (
                            str(existing["source_asset_id"]) != decision.source_asset_id,
                            str(existing["source_asset_key"]) != decision.source_asset_key,
                            str(existing["reason_code"]) != decision.reason_code,
                            str(existing["source_record_sha256"])
                            != decision.source_record_sha256,
                            str(existing["provenance_json"])
                            != decision.source_record_json,
                            str(existing["detail_json"]) != decision.detail_json,
                        )
                    ):
                        raise PipelineError("stored exclusion prefix does not match source")
                elif selected is not None and selection_rank is not None:
                    existing = connection.execute(
                        """SELECT s.*,f.status AS fingerprint_status
                           FROM source_assets AS s
                           JOIN fingerprints AS f USING(run_id,source_asset_id)
                           WHERE s.run_id=? AND s.source_asset_id=?""",
                        (run_id, decision.source_asset_id),
                    ).fetchone()
                    record_json = source_asset_record_json(decision)
                    if existing is None or any(
                        (
                            int(existing["selection_rank"]) != selection_rank,
                            str(existing["source_record_sha256"])
                            != source_record_sha256(record_json),
                            str(existing["provenance_json"])
                            != _json_bytes(_selected_record(selected)).decode("ascii"),
                            str(existing["fingerprint_status"]) != "pending",
                        )
                    ):
                        raise PipelineError("stored selection prefix does not match source")
                if inventory_rank == initialized_inventory and (
                    selected_count != initialized_selected
                    or excluded_count != initialized_excluded
                ):
                    raise PipelineError("stored initialization progress is inconsistent")
                continue

            if not transaction_open:
                connection.execute("BEGIN IMMEDIATE")
                transaction_open = True
            if isinstance(decision, SourceAssetExclusion):
                connection.execute(
                    """INSERT INTO source_asset_exclusions(
                         run_id,source_asset_id,source_asset_key,inventory_rank,
                         reason_code,source_record_sha256,provenance_json,detail_json
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        decision.source_asset_id,
                        decision.source_asset_key,
                        inventory_rank,
                        decision.reason_code,
                        decision.source_record_sha256,
                        decision.source_record_json,
                        decision.detail_json,
                    ),
                )
            elif selected is not None and selection_rank is not None:
                record_json = source_asset_record_json(decision)
                connection.execute(
                    """INSERT INTO source_assets(
                         run_id,source_asset_id,selection_rank,canonical_url,fetch_url,
                         source_record_sha256,provenance_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        decision.source_asset_id,
                        selection_rank,
                        decision.normalized_url,
                        decision.effective_fetch_url,
                        source_record_sha256(record_json),
                        _json_bytes(_selected_record(selected)).decode("ascii"),
                    ),
                )
                connection.execute(
                    """INSERT INTO fingerprints(run_id,source_asset_id,status)
                       VALUES(?,?,'pending')""",
                    (run_id, decision.source_asset_id),
                )

            if inventory_rank % INITIALIZATION_COMMIT_SIZE == 0:
                _commit_initialization_batch(
                    connection,
                    run_id,
                    inventory_count,
                    selected_count,
                    excluded_count,
                )
                transaction_open = False

        stored_selection_digest = hashlib.sha256()
        stored_selection_count = 0
        for stored in connection.execute(
            """SELECT provenance_json FROM source_assets
               WHERE run_id=? ORDER BY selection_rank""",
            (run_id,),
        ):
            stored_selection_digest.update(
                _json_bytes(json.loads(stored["provenance_json"]))
            )
            stored_selection_digest.update(b"\n")
            stored_selection_count += 1
        if (
            inventory_count != plan.source_total_count
            or eligible_count != plan.eligible_inventory_count
            or excluded_count != plan.excluded_inventory_count
            or selected_count != plan.count
            or stored_selection_count != plan.count
            or stored_selection_digest.hexdigest() != plan.manifest_sha256
        ):
            raise PipelineError("inventory changed during sidecar initialization")
        if not transaction_open:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE fingerprint_runs SET
                 initialized_inventory_count=?,initialized_selected_count=?,
                 initialized_excluded_count=?,initialization_updated_at=?,
                 initialization_completed_at=?,status='running',error=NULL
               WHERE run_id=? AND status='initializing'""",
            (
                inventory_count,
                selected_count,
                excluded_count,
                _utc_now(),
                _utc_now(),
                run_id,
            ),
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _check_resume(
    path: Path,
    source: str,
    source_db: Path,
    source_sha: str,
    dependency_json: str,
    dependency_sha: str,
    runner_version: str,
    max_attempts: int,
    plan: _SelectionPlan,
    sample_seed: str,
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
            "retry_policy_version": RETRY_POLICY_VERSION,
            "max_attempts": max_attempts,
            "dependency_manifest_json": dependency_json,
            "dependency_manifest_sha256": dependency_sha,
            "selection_manifest_sha256": plan.manifest_sha256,
            "selection_mode": "sample" if plan.selected is not None else "full",
            "selection_count": plan.count,
            "sample_seed": sample_seed if plan.selected is not None else None,
            "selection_version": SELECTION_VERSION,
            "source_inventory_manifest_sha256": plan.inventory_manifest_sha256,
            "exclusion_manifest_sha256": plan.exclusion_manifest_sha256,
            "source_total_count": plan.source_total_count,
            "eligible_count": plan.eligible_inventory_count,
            "excluded_count": plan.excluded_inventory_count,
        }
        mismatches = [
            key for key, value in expected.items() if row[key] != value
        ]
        if mismatches:
            raise PipelineError("resume provenance mismatch: " + ", ".join(mismatches))

        status = str(row["status"])
        if status == "initializing":
            return str(row["run_id"]), status

        digest = hashlib.sha256()
        count = 0
        for asset_row in connection.execute(
            "SELECT source_record_sha256,provenance_json FROM source_assets "
            "WHERE run_id=? ORDER BY selection_rank",
            (row["run_id"],),
        ):
            record = json.loads(asset_row["provenance_json"])
            base_sha = source_record_sha256(
                source_asset_record_json(_asset_from_record(record))
            )
            if base_sha != asset_row["source_record_sha256"]:
                raise PipelineError("stored source asset provenance hash mismatch")
            digest.update(_json_bytes(record))
            digest.update(b"\n")
            count += 1
        if count != plan.count or digest.hexdigest() != plan.manifest_sha256:
            raise PipelineError("stored selection does not match the requested manifest")
        return str(row["run_id"]), status
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
    scheduled_delay_seconds: float | None = None,
    worker_no: int | None = None,
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
        failure.retry_after_seconds if failure is not None else None,
        scheduled_delay_seconds,
        worker_no,
    )


_INSERT_ATTEMPT = """
INSERT INTO fetch_attempts(
  run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
  elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
  raw_response_sha256,error_kind,error_message,retry_after_seconds,
  scheduled_delay_seconds,worker_no
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _persist_attempt(connection: sqlite3.Connection, row: tuple[object, ...]) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_INSERT_ATTEMPT, row)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _fetch_attempt_worker(
    task: _AttemptTask,
    fetchers: _WorkerFetchers,
    limiter: _GlobalRateLimiter,
    *,
    stop_event: threading.Event,
    external_request_gate: bool,
    max_response_bytes: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> _AttemptResult:
    if not _sleep_unless_stopped(
        task.delay_seconds, stop_event=stop_event, sleep=sleep
    ):
        return _AttemptResult(
            task=task, started_at=_utc_now(), completed_at=_utc_now(),
            elapsed_ms=0, response=None, failure=None, fatal=None,
            fingerprint=None, fingerprint_failure=None, network_requests=0,
            worker_no=0, not_started=True,
        )
    if external_request_gate and not limiter.acquire():
        return _AttemptResult(
            task=task, started_at=_utc_now(), completed_at=_utc_now(),
            elapsed_ms=0, response=None, failure=None, fatal=None,
            fingerprint=None, fingerprint_failure=None, network_requests=0,
            worker_no=0, not_started=True,
        )
    if stop_event.is_set():
        return _AttemptResult(
            task=task, started_at=_utc_now(), completed_at=_utc_now(),
            elapsed_ms=0, response=None, failure=None, fatal=None,
            fingerprint=None, fingerprint_failure=None, network_requests=0,
            worker_no=0, not_started=True,
        )
    fetcher, worker_no = fetchers.current()
    before_requests = getattr(fetcher, "network_requests", None)
    started_at = _utc_now()
    started = clock()
    response: FetchResponse | None = None
    failure: FetchFailure | None = None
    fatal: BaseException | None = None
    fingerprint: ImageFingerprint | None = None
    fingerprint_failure: FingerprintError | None = None
    completed_at: str | None = None
    elapsed_ms: int | None = None
    try:
        candidate = fetcher(task.asset, task.attempt_no)
        _validate_response(task.asset, candidate, max_response_bytes)
        response = candidate
        completed_at = _utc_now()
        elapsed_ms = max(0, round((clock() - started) * 1000))
    except FetchFailure as exc:
        failure = exc
    except BaseException as exc:
        fatal = exc
        failure = FetchFailure(
            "interrupted",
            f"{type(exc).__name__}: fetch attempt interrupted",
            retryable=True,
            final_url=task.asset.effective_fetch_url,
        )
    if completed_at is None:
        completed_at = _utc_now()
    if elapsed_ms is None:
        elapsed_ms = max(0, round((clock() - started) * 1000))
    after_requests = getattr(fetcher, "network_requests", None)
    if before_requests is not None and after_requests is not None:
        request_count = max(0, int(after_requests) - int(before_requests))
    else:
        request_count = 1
    return _AttemptResult(
        task=task,
        started_at=started_at,
        completed_at=completed_at,
        elapsed_ms=elapsed_ms,
        response=response,
        failure=failure,
        fatal=fatal,
        fingerprint=fingerprint,
        fingerprint_failure=fingerprint_failure,
        network_requests=request_count,
        worker_no=worker_no,
    )


def _set_invalid_asset_skipped(
    connection: sqlite3.Connection,
    run_id: str,
    asset: SourceAsset,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """UPDATE fingerprints SET status='skipped',completed_at=?,
               error_kind='invalid_fetch_url',error_message=?
               WHERE run_id=? AND source_asset_id=? AND status='pending'""",
            (
                _utc_now(),
                "fetch URL is outside the HTTPS source allowlist",
                run_id,
                asset.source_asset_id,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _set_attempt_budget_exhausted(
    connection: sqlite3.Connection,
    run_id: str,
    asset: SourceAsset,
) -> None:
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


def _finish_asset(
    connection: sqlite3.Connection,
    run_id: str,
    result: _AttemptResult,
    max_response_bytes: int,
) -> None:
    response = result.response
    failure = result.failure
    fingerprint = None
    fingerprint_failure = None
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
                    result.task.attempt_no,
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
                    result.task.asset.source_asset_id,
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
                    result.task.attempt_no,
                    hashlib.sha256(response.body).hexdigest(),
                    completed_at,
                    f"decode:{fingerprint_failure.kind}",
                    str(fingerprint_failure),
                    run_id,
                    result.task.asset.source_asset_id,
                ),
            )
        else:
            terminal = failure or FetchFailure("fetch", "fetch failed")
            cursor = connection.execute(
                """
                UPDATE fingerprints SET status='failed',completed_at=?,error_kind=?,error_message=?
                WHERE run_id=? AND source_asset_id=? AND status='pending'
                """,
                (
                    completed_at,
                    terminal.kind,
                    str(terminal),
                    run_id,
                    result.task.asset.source_asset_id,
                ),
            )
        if cursor.rowcount != 1:
            raise PipelineError("pending fingerprint row disappeared during asset transaction")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _retry_delay(failure: FetchFailure, attempt_no: int) -> float:
    exponential = min(
        DEFAULT_BACKOFF_MAX_SECONDS,
        float(2 ** max(0, attempt_no - 1)),
    )
    return max(exponential, failure.retry_after_seconds or 0.0)


def _is_overload(failure: FetchFailure | None) -> bool:
    status = failure.http_status if failure is not None else None
    return status == 429 or (status is not None and 500 <= status <= 599)


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


_PENDING_BATCH_SQL = """
SELECT s.selection_rank,s.provenance_json,
       coalesce((
         SELECT max(a.attempt_no)
         FROM fetch_attempts AS a
         WHERE a.run_id=s.run_id
           AND a.source_asset_id=s.source_asset_id
       ),0) AS previous_attempts
       ,(
         SELECT a.completed_at
         FROM fetch_attempts AS a
         WHERE a.run_id=s.run_id
           AND a.source_asset_id=s.source_asset_id
         ORDER BY a.attempt_no DESC LIMIT 1
       ) AS last_attempt_completed_at
       ,(
         SELECT a.scheduled_delay_seconds
         FROM fetch_attempts AS a
         WHERE a.run_id=s.run_id
           AND a.source_asset_id=s.source_asset_id
         ORDER BY a.attempt_no DESC LIMIT 1
       ) AS last_scheduled_delay_seconds
FROM source_assets AS s INDEXED BY idx_source_assets_run_rank
CROSS JOIN fingerprints AS f
  ON f.run_id=s.run_id AND f.source_asset_id=s.source_asset_id
WHERE s.run_id=? AND s.selection_rank>? AND f.status='pending'
ORDER BY s.selection_rank
LIMIT ?
"""


def _load_pending_batch(
    connection: sqlite3.Connection,
    run_id: str,
    batch_size: int,
    *,
    after_selection_rank: int = 0,
    wall_time: Callable[[], float] = time.time,
) -> list[tuple[SourceAsset, int, int, float]]:
    rows = connection.execute(
        _PENDING_BATCH_SQL,
        (run_id, after_selection_rank, batch_size),
    ).fetchall()
    return [
        (
            _asset_from_record(json.loads(row["provenance_json"])),
            int(row["previous_attempts"]),
            int(row["selection_rank"]),
            _remaining_scheduled_delay(
                row["last_attempt_completed_at"],
                row["last_scheduled_delay_seconds"],
                wall_time=wall_time,
            ),
        )
        for row in rows
    ]


def _remaining_scheduled_delay(
    completed_at: object,
    scheduled_delay_seconds: object,
    *,
    wall_time: Callable[[], float] = time.time,
) -> float:
    if completed_at is None or scheduled_delay_seconds is None:
        return 0.0
    delay = float(scheduled_delay_seconds)
    if delay <= 0:
        return 0.0
    try:
        completed = datetime.fromisoformat(
            str(completed_at).replace("Z", "+00:00")
        )
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        deadline = completed.timestamp() + delay
    except (OSError, OverflowError, ValueError) as exc:
        raise PipelineError("stored retry schedule timestamp is invalid") from exc
    return max(0.0, deadline - wall_time())


def _record_running_error(
    connection: sqlite3.Connection,
    run_id: str,
    message: str | None,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE fingerprint_runs SET error=? WHERE run_id=? AND status='running'",
            (message, run_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _run_pending_parallel(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    fetcher_factory: FetcherFactory,
    stop_event: threading.Event,
    workers: int,
    limiter: _GlobalRateLimiter,
    external_request_gate: bool,
    max_attempts: int,
    max_response_bytes: int,
    circuit_breaker_threshold: int,
    cooldown_seconds: float,
    batch_size: int,
    clock: Callable[[], float],
    wall_time: Callable[[], float],
    sleep: Callable[[float], None],
) -> int:
    fetchers = _WorkerFetchers(fetcher_factory)
    requests_made = 0
    consecutive_overload = 0
    circuit_open = False
    fatal_exception: BaseException | None = None
    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="archibe-e1-fetch",
    )
    last_selection_rank = 0
    try:
        while not circuit_open and fatal_exception is None:
            loaded = _load_pending_batch(
                connection,
                run_id,
                batch_size,
                after_selection_rank=last_selection_rank,
                wall_time=wall_time,
            )
            if not loaded:
                break
            last_selection_rank = loaded[-1][2]
            waiting: deque[_AttemptTask] = deque()
            for asset, previous_attempts, _selection_rank, remaining_delay in loaded:
                if not _host_allowed(asset.source, asset.effective_fetch_url):
                    _set_invalid_asset_skipped(connection, run_id, asset)
                elif previous_attempts >= max_attempts:
                    _set_attempt_budget_exhausted(connection, run_id, asset)
                else:
                    waiting.append(
                        _AttemptTask(asset, previous_attempts + 1, remaining_delay)
                    )

            futures: dict[Future[_AttemptResult], _AttemptTask] = {}
            while waiting or futures:
                # Submit one bounded wave.  Do not refill worker slots until
                # every in-flight result in this wave has crossed the durable
                # attempt-ledger boundary.
                while (
                    waiting
                    and len(futures) < workers
                    and not circuit_open
                    and fatal_exception is None
                ):
                    task = waiting.popleft()
                    future = executor.submit(
                        _fetch_attempt_worker,
                        task,
                        fetchers,
                        limiter,
                        stop_event=stop_event,
                        external_request_gate=external_request_gate,
                        max_response_bytes=max_response_bytes,
                        clock=clock,
                        sleep=sleep,
                    )
                    futures[future] = task

                if not futures:
                    break
                durable_results: list[tuple[_AttemptResult, float | None]] = []
                # Phase 1: repeatedly harvest this wave with FIRST_COMPLETED.
                # A result is committed immediately, while decode waits until
                # every started sibling has been drained.  stop_event may turn
                # waiting workers into not_started results; those have no HTTP
                # attempt to record.
                while futures:
                    done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                    results: list[_AttemptResult] = []
                    for future in done:
                        futures.pop(future)
                        results.append(future.result())
                    results.sort(
                        key=lambda item: (
                            item.completed_at,
                            item.task.asset.source_asset_id,
                            item.task.attempt_no,
                        )
                    )
                    for result in results:
                        if result.not_started:
                            continue
                        requests_made += result.network_requests
                        failure = result.failure
                        overload = _is_overload(failure)
                        if not circuit_open:
                            if overload:
                                consecutive_overload += 1
                                limiter.defer(
                                    max(
                                        cooldown_seconds,
                                        failure.retry_after_seconds or 0.0,
                                    )
                                )
                                if consecutive_overload >= circuit_breaker_threshold:
                                    circuit_open = True
                                    stop_event.set()
                            else:
                                consecutive_overload = 0
                        if result.fatal is not None and fatal_exception is None:
                            fatal_exception = result.fatal
                            stop_event.set()

                        retry_delay = (
                            _retry_delay(failure, result.task.attempt_no)
                            if failure is not None
                            and failure.retryable
                            and result.task.attempt_no < max_attempts
                            else None
                        )
                        if retry_delay is not None and overload:
                            retry_delay = max(retry_delay, cooldown_seconds)
                        attempt_row = _attempt_row(
                            run_id=run_id,
                            asset=result.task.asset,
                            attempt_no=result.task.attempt_no,
                            started_at=result.started_at,
                            completed_at=result.completed_at,
                            elapsed_ms=result.elapsed_ms,
                            outcome=(
                                "success" if result.response is not None else "failed"
                            ),
                            response=result.response,
                            failure=failure,
                            scheduled_delay_seconds=retry_delay,
                            worker_no=result.worker_no,
                        )
                        # The HTTP attempt is a separate durability boundary.
                        # A decoder crash must still consume its attempt number
                        # and retry budget on an exact resume.
                        _persist_attempt(connection, attempt_row)
                        durable_results.append((result, retry_delay))

                # Phase 2 begins only after all in-flight started attempts are
                # durable.  Retry decisions use the final wave-level circuit /
                # fatal state so interrupted work remains pending for resume.
                for result, retry_delay in durable_results:
                    failure = result.failure
                    can_retry = bool(
                        failure is not None
                        and failure.retryable
                        and result.task.attempt_no < max_attempts
                        and not circuit_open
                        and fatal_exception is None
                    )
                    if result.response is not None:
                        _finish_asset(
                            connection,
                            run_id,
                            result,
                            max_response_bytes,
                        )
                    elif can_retry and retry_delay is not None:
                        waiting.append(
                            _AttemptTask(
                                result.task.asset,
                                result.task.attempt_no + 1,
                                retry_delay,
                            )
                        )
                    elif (
                        failure is not None
                        and failure.retryable
                        and result.task.attempt_no < max_attempts
                    ):
                        # A circuit break or process-level interruption leaves
                        # retriable work pending for an exact later resume.
                        pass
                    else:
                        _finish_asset(
                            connection,
                            run_id,
                            result,
                            max_response_bytes,
                        )

            if circuit_open or fatal_exception is not None:
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=False)
        fetchers.close()

    if circuit_open:
        message = (
            "circuit breaker opened after "
            f"{consecutive_overload} consecutive HTTP 429/5xx results"
        )
        _record_running_error(connection, run_id, message)
        raise PipelineError(message)
    if fatal_exception is not None:
        _record_running_error(
            connection,
            run_id,
            f"{type(fatal_exception).__name__}: fetch worker interrupted",
        )
        raise fatal_exception
    return requests_made


def _validation_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def _finish_run(
    partial: Path,
    run_id: str,
    source_db: Path,
    source_sha: str,
    *,
    inventory_factory: InventoryFactory | None,
) -> None:
    _assert_source_snapshot(source_db)
    ending_sha = _sha256_file(source_db)
    independent = validate_image_fingerprint_sidecar(
        partial,
        source_db,
        inventory_factory=inventory_factory,
    )
    check_map = {check.name: check for check in independent.checks}
    required = []
    for name in REQUIRED_VALIDATIONS:
        check = check_map.get(name)
        if check is None:
            required.append((name, False, "present", "missing", None))
        else:
            required.append(
                (name, check.passed, check.expected, check.actual, check.detail)
            )

    connection = open_sidecar(partial, readonly=False)
    try:
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status,count(*) FROM fingerprints WHERE run_id=? GROUP BY status",
                (run_id,),
            )
        }
        pending_count = counts.get("pending", 0)
        success_count = counts.get("success", 0)
        failure_count = counts.get("failed", 0) + counts.get("skipped", 0)

        adjusted: list[tuple[str, bool, object, object, object | None]] = []
        optional_failures = {
            check.name: check.to_dict()
            for check in independent.checks
            if check.name not in {"required_validations", "terminal_status"}
            and not check.passed
        }
        for name, passed, expected, actual, detail in required:
            if name == "source_sha_unchanged":
                passed = bool(passed and ending_sha == source_sha)
            elif name == "fingerprint_accounting":
                passed = bool(passed and pending_count == 0)
            elif name == "successful_attempt_linkage":
                passed = bool(passed and success_count > 0)
                if success_count == 0:
                    detail = {"reason": "no successful fingerprints", "counts": counts}
            adjusted.append((name, passed, expected, actual, detail))

        # Optional independent checks contain stricter manifest and source-path
        # evidence.  Fold any failure into the closest required gate so a bad
        # run becomes failed_validation before terminal immutability applies.
        if optional_failures:
            target = "source_inventory_accounting"
            if any(name.startswith("source_db") for name in optional_failures):
                target = "source_sha_unchanged"
            elif any("exclusion" in name for name in optional_failures):
                target = "exclusion_ledger_accounting"
            elif any("selection" in name for name in optional_failures):
                target = "ordered_selection_manifest"
            adjusted = [
                (
                    name,
                    False if name == target else passed,
                    expected,
                    actual,
                    {
                        "independent_optional_failures": optional_failures,
                        "check_detail": detail,
                    }
                    if name == target
                    else detail,
                )
                for name, passed, expected, actual, detail in adjusted
            ]

        passed = all(item[1] for item in adjusted)
        terminal_status = (
            "complete"
            if passed and failure_count == 0
            else "complete_with_failures"
            if passed and success_count > 0
            else "failed_validation"
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            """INSERT INTO validations(
                 run_id,validation_name,severity,passed,expected,actual,detail
               ) VALUES(?,?,'error',?,?,?,?)
               ON CONFLICT(run_id,validation_name) DO UPDATE SET
                 severity=excluded.severity,passed=excluded.passed,
                 expected=excluded.expected,actual=excluded.actual,
                 detail=excluded.detail""",
            [
                (
                    run_id,
                    name,
                    int(check_passed),
                    _validation_text(expected),
                    _validation_text(actual),
                    None if detail is None else _validation_text(detail),
                )
                for name, check_passed, expected, actual, detail in adjusted
            ],
        )
        run_counts = connection.execute(
            """SELECT source_total_count,eligible_count,excluded_count,
                      selection_count,selection_manifest_sha256
               FROM fingerprint_runs WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        connection.executemany(
            """INSERT INTO validations(
                 run_id,validation_name,severity,passed,expected,actual,detail
               ) VALUES(?,?,'info',1,?,?,?)
               ON CONFLICT(run_id,validation_name) DO UPDATE SET
                 severity=excluded.severity,passed=excluded.passed,
                 expected=excluded.expected,actual=excluded.actual,
                 detail=excluded.detail""",
            (
                (
                    run_id,
                    "eligible_inventory_accounting",
                    str(run_counts["eligible_count"]),
                    str(run_counts["eligible_count"]),
                    _validation_text(
                        {
                            "eligible": int(run_counts["eligible_count"]),
                            "excluded": int(run_counts["excluded_count"]),
                            "source_assets": int(run_counts["source_total_count"]),
                        }
                    ),
                ),
                (
                    run_id,
                    "selection_manifest",
                    str(run_counts["selection_count"]),
                    str(run_counts["selection_count"]),
                    _validation_text(
                        {
                            "manifest_sha256": str(
                                run_counts["selection_manifest_sha256"]
                            ),
                            "selection_version": SELECTION_VERSION,
                        }
                    ),
                ),
            ),
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

    final_validation = validate_image_fingerprint_sidecar(
        partial,
        source_db,
        inventory_factory=inventory_factory,
    )
    if terminal_status not in {"complete", "complete_with_failures"}:
        if validate_sidecar(partial).passed:
            raise PipelineError("fingerprint sidecar failed final validation")
        raise PipelineError("fingerprint sidecar failed final validation")
    if not final_validation.passed:
        raise PipelineError("fingerprint sidecar failed independent final validation")


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
    inventory_factory: InventoryFactory | None = None,
    fetcher: Fetcher | None = None,
    fetcher_factory: FetcherFactory | None = None,
    workers: int = DEFAULT_WORKERS,
    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    batch_size: int = DEFAULT_PENDING_BATCH_SIZE,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], float] = time.time,
) -> PipelineResult:
    """Run or exactly resume one source-neutral E1 sidecar build."""

    if source not in {"divisare", "architizer"}:
        raise ValueError("source must be 'divisare' or 'architizer'")
    if max_response_bytes < 1 or max_attempts < 1:
        raise ValueError("max_response_bytes and max_attempts must be positive")
    if isinstance(workers, bool) or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
    if not math.isfinite(requests_per_second) or requests_per_second <= 0:
        raise ValueError("requests_per_second must be positive")
    if circuit_breaker_threshold < 1:
        raise ValueError("circuit_breaker_threshold must be positive")
    if not math.isfinite(cooldown_seconds) or cooldown_seconds < 0:
        raise ValueError("cooldown_seconds cannot be negative")
    if (
        not math.isfinite(connect_timeout)
        or connect_timeout <= 0
        or not math.isfinite(read_timeout)
        or read_timeout <= 0
    ):
        raise ValueError("connect_timeout and read_timeout must be positive finite values")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if fetcher is not None and fetcher_factory is not None:
        raise ValueError("fetcher and fetcher_factory are mutually exclusive")
    source_path = Path(source_db).resolve()
    output_path = Path(output).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(output_path) + ".partial")
    lock_path = Path(str(output_path) + ".lock")
    descriptor = _acquire_lock(lock_path)
    try:
        _assert_source_snapshot(source_path)
        source_sha = _sha256_file(source_path)
        dependency_json, dependency_sha = _dependency_manifest()
        runner_version = _effective_runner_version(dependency_sha)
        default_inventory = asset_factory is None and inventory_factory is None
        active_inventory_factory = (
            inventory_factory
            or (
                _default_inventory_factory(source, source_path)
                if asset_factory is None
                else _inventory_from_assets(asset_factory)
            )
        )
        factory = asset_factory or (
            lambda: (
                decision
                for decision in active_inventory_factory()
                if isinstance(decision, SourceAsset)
            )
        )
        plan = _selection_plan(
            factory,
            source,
            sample_size,
            sample_seed,
            ordered=default_inventory,
        )
        inventory_plan = _inventory_plan(
            active_inventory_factory,
            source,
            ordered=default_inventory,
        )
        if plan.eligible_inventory_count != inventory_plan.eligible_count:
            raise PipelineError(
                "eligible selection inventory does not match source inventory decisions"
            )
        if sample_size is not None and plan.count == 0:
            raise PipelineError("sample run has no eligible source assets")
        plan = replace(
            plan,
            source_total_count=inventory_plan.source_total_count,
            excluded_inventory_count=inventory_plan.excluded_count,
            inventory_manifest_sha256=inventory_plan.inventory_manifest_sha256,
            exclusion_manifest_sha256=inventory_plan.exclusion_manifest_sha256,
        )

        if output_path.exists():
            if not resume:
                raise FileExistsError(f"refusing to clobber output: {output_path}")
            _, status = _check_resume(
                output_path, source, source_path, source_sha, dependency_json,
                dependency_sha, runner_version, max_attempts, plan, sample_seed
            )
            independent = validate_image_fingerprint_sidecar(
                output_path,
                source_path,
                inventory_factory=(
                    None if default_inventory else active_inventory_factory
                ),
            )
            if status not in {"complete", "complete_with_failures"} or not independent.passed:
                raise PipelineError("published sidecar is not a valid complete run")
            return _result(output_path, source, 0, True, True)

        if partial.exists() and not resume:
            raise FileExistsError(f"partial exists; use --resume or choose another output: {partial}")
        if not partial.exists() and resume:
            raise FileNotFoundError(f"resume partial does not exist: {partial}")

        if partial.exists():
            _recover_partial(partial)
            run_id, status = _check_resume(
                partial, source, source_path, source_sha, dependency_json,
                dependency_sha, runner_version, max_attempts, plan, sample_seed
            )
            if status in {"complete", "complete_with_failures"}:
                independent = validate_image_fingerprint_sidecar(
                    partial,
                    source_path,
                    inventory_factory=(
                        None if default_inventory else active_inventory_factory
                    ),
                )
                if not independent.passed:
                    raise PipelineError("complete partial sidecar failed validation")
                _publish_hardlink(partial, output_path)
                return _result(output_path, source, 0, True, True)
            if status == "initializing":
                _resume_initialization(
                    partial,
                    run_id,
                    source,
                    plan,
                    active_inventory_factory,
                    ordered_inventory=default_inventory,
                )
                status = "running"
            if status != "running":
                raise PipelineError(f"terminal {status!r} sidecar cannot be resumed")
            resumed = True
        else:
            run_id = _initialize_run(
                partial, source, source_path, source_sha, dependency_json,
                dependency_sha, runner_version, max_attempts, plan, sample_seed,
                active_inventory_factory, default_inventory
            )
            resumed = False

        stop_event = threading.Event()
        limiter = _GlobalRateLimiter(
            requests_per_second,
            clock=clock,
            sleep=sleep,
            stop_event=stop_event,
        )
        effective_workers = workers
        external_request_gate = True
        if fetcher_factory is not None:
            active_fetcher_factory = fetcher_factory
        elif fetcher is not None:
            # Compatibility for the original injectable single fetcher: do not
            # share a caller-owned session across worker threads.
            effective_workers = 1
            active_fetcher_factory = lambda: fetcher
        else:
            def active_fetcher_factory() -> Fetcher:
                return RequestsFetcher(
                    max_response_bytes=max_response_bytes,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    request_gate=limiter.acquire,
                    gate_first_request=False,
                    wall_time=wall_time,
                )

        connection = open_sidecar(partial, readonly=False)
        try:
            _record_running_error(connection, run_id, None)
            requests_made = _run_pending_parallel(
                connection,
                run_id,
                fetcher_factory=active_fetcher_factory,
                stop_event=stop_event,
                workers=effective_workers,
                limiter=limiter,
                external_request_gate=external_request_gate,
                max_attempts=max_attempts,
                max_response_bytes=max_response_bytes,
                circuit_breaker_threshold=circuit_breaker_threshold,
                cooldown_seconds=cooldown_seconds,
                batch_size=batch_size,
                clock=clock,
                wall_time=wall_time,
                sleep=sleep,
            )
        finally:
            connection.close()

        _finish_run(
            partial,
            run_id,
            source_path,
            source_sha,
            inventory_factory=(None if default_inventory else active_inventory_factory),
        )
        _publish_hardlink(partial, output_path)
        return _result(output_path, source, requests_made, resumed, False)
    finally:
        _release_lock(lock_path, descriptor)
