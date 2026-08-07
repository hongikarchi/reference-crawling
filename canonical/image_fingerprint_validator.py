"""Independent, read-only validation for shared E1 fingerprint sidecars.

The runner's stored validation rows are evidence, not authority.  This module
re-opens the immutable source snapshot, streams the source adapter inventory,
and independently recomputes inventory, exclusion, source-record, and ordered
selection manifests with memory bounded by the requested smoke sample.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from canonical.image_fingerprint_adapters import (
    InventoryDecision,
    SourceAsset,
    SourceAssetExclusion,
    inventory_decision_manifest_json,
    iter_architizer_source_inventory,
    iter_divisare_source_inventory,
    source_asset_record_json,
    source_record_sha256,
)
from canonical.image_fingerprint_sidecar import (
    REQUIRED_VALIDATIONS,
    open_sidecar,
    recover_sidecar,
    validate_sidecar,
)


InventoryFactory = Callable[[], Iterable[InventoryDecision]]
_TERMINAL_SUCCESS = frozenset({"complete", "complete_with_failures"})


@dataclass(frozen=True)
class IndependentCheck:
    name: str
    passed: bool
    expected: object
    actual: object
    detail: object | None = None

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "actual": self.actual,
            "expected": self.expected,
            "passed": self.passed,
        }
        if self.detail is not None:
            value["detail"] = self.detail
        return value


@dataclass(frozen=True)
class IndependentValidation:
    sidecar_path: Path
    source_db_path: Path
    source_name: str | None
    run_id: str | None
    checks: tuple[IndependentCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": {check.name: check.to_dict() for check in self.checks},
            "passed": self.passed,
            "run_id": self.run_id,
            "sidecar_path": str(self.sidecar_path),
            "source_db_path": str(self.source_db_path),
            "source_name": self.source_name,
        }


@dataclass(frozen=True)
class _SelectedAsset:
    asset: SourceAsset
    selection_reason: str
    selection_stratum: str
    sample_score_sha256: str | None
    sample_seed: str | None


class _SampleSelector:
    """Coverage-augmented hash selection using O(sample size) memory."""

    def __init__(self, size: int, seed: str, selection_version: str):
        self.size = size
        self.seed = seed
        self.selection_version = selection_version
        self.heap: list[tuple[int, int, str, SourceAsset]] = []
        self.best_for_label: dict[str, tuple[str, str, SourceAsset]] = {}
        self.serial = 0

    def add(self, asset: SourceAsset) -> None:
        score = _sample_score(asset, self.seed, self.selection_version)
        score_number = int(score, 16)
        self.serial += 1
        entry = (-score_number, self.serial, score, asset)
        if len(self.heap) < self.size:
            heapq.heappush(self.heap, entry)
        elif score_number < -self.heap[0][0]:
            heapq.heapreplace(self.heap, entry)
        for label in _coverage_labels(asset):
            candidate = (score, asset.source_asset_id, asset)
            if label not in self.best_for_label or candidate[:2] < self.best_for_label[label][:2]:
                self.best_for_label[label] = candidate

    def finish(self) -> tuple[_SelectedAsset, ...]:
        global_best = sorted(
            (
                (score, asset.source_asset_id, asset)
                for _, _, score, asset in self.heap
            ),
            key=lambda item: item[:2],
        )
        if not global_best:
            return ()

        candidate_labels: dict[str, set[str]] = {}
        candidate_assets: dict[str, tuple[str, SourceAsset]] = {}
        for label, (score, asset_id, asset) in self.best_for_label.items():
            candidate_labels.setdefault(asset_id, set()).add(label)
            candidate_assets[asset_id] = (score, asset)

        mandatory_ids = set(candidate_assets)
        if len(mandatory_ids) > self.size:
            remaining = set(self.best_for_label)
            kept: set[str] = set()
            while remaining and len(kept) < self.size:
                chosen = min(
                    (
                        asset_id
                        for asset_id in mandatory_ids
                        if asset_id not in kept
                    ),
                    key=lambda asset_id: (
                        -len(candidate_labels[asset_id] & remaining),
                        candidate_assets[asset_id][0],
                        asset_id,
                    ),
                )
                kept.add(chosen)
                remaining.difference_update(candidate_labels[chosen])
            mandatory_ids = kept

        chosen = {
            asset_id: candidate_assets[asset_id] for asset_id in mandatory_ids
        }
        for score, asset_id, asset in global_best:
            if len(chosen) >= self.size:
                break
            chosen.setdefault(asset_id, (score, asset))

        selected = []
        for asset_id, (score, asset) in chosen.items():
            labels = sorted(candidate_labels.get(asset_id, set()))
            reason = "coverage:" + ",".join(labels) if labels else "hash_sample"
            selected.append(
                _SelectedAsset(asset, reason, _stratum(asset), score, self.seed)
            )
        return tuple(
            sorted(
                selected,
                key=lambda item: (
                    item.sample_score_sha256 or "",
                    item.asset.source_asset_id,
                ),
            )
        )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


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


def _sample_score(asset: SourceAsset, seed: str, selection_version: str) -> str:
    base_sha = hashlib.sha256(_canonical_json_bytes(_asset_base_dict(asset))).hexdigest()
    framed = "\0".join(
        (selection_version, seed, asset.source, asset.source_asset_id, base_sha)
    )
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def _selected_record(selected: _SelectedAsset, selection_version: str) -> dict[str, object]:
    value = _asset_base_dict(selected.asset)
    value.update(
        {
            "selection_reason": selected.selection_reason,
            "selection_stratum": selected.selection_stratum,
            "sample_score_sha256": selected.sample_score_sha256,
            "sample_seed": selected.sample_seed,
            "selection_version": selection_version,
        }
    )
    return value


def _default_inventory_factory(source: str, source_db: Path) -> InventoryFactory:
    if source == "divisare":
        return lambda: iter_divisare_source_inventory(source_db)
    if source == "architizer":
        return lambda: iter_architizer_source_inventory(source_db)
    raise ValueError(f"unsupported E1 source: {source!r}")


def _source_table_count(source_db: Path) -> int:
    uri = source_db.as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        return int(connection.execute("SELECT count(*) FROM image_assets").fetchone()[0])
    finally:
        connection.close()


def _next_or_none(iterator: Iterator[sqlite3.Row]) -> sqlite3.Row | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _selected_row_mismatches(
    stored: sqlite3.Row | None,
    selected: _SelectedAsset,
    rank: int,
    selection_version: str,
) -> tuple[int, int]:
    """Return (row/provenance mismatches, source-record SHA mismatches)."""

    if stored is None:
        return 1, 1
    asset = selected.asset
    expected_provenance = _canonical_json_bytes(
        _selected_record(selected, selection_version)
    ).decode("ascii")
    expected_record_sha = source_record_sha256(source_asset_record_json(asset))
    row_mismatch = int(
        int(stored["selection_rank"]) != rank
        or str(stored["source_asset_id"]) != asset.source_asset_id
        or str(stored["canonical_url"]) != asset.normalized_url
        or str(stored["fetch_url"]) != asset.effective_fetch_url
        or str(stored["provenance_json"]) != expected_provenance
    )
    sha_mismatch = int(str(stored["source_record_sha256"]) != expected_record_sha)
    return row_mismatch, sha_mismatch


def validate_image_fingerprint_sidecar(
    sidecar_path: Path | str,
    source_db_path: Path | str,
    *,
    inventory_factory: InventoryFactory | None = None,
    recover: bool = False,
) -> IndependentValidation:
    """Independently validate one sidecar against its immutable source DB."""

    sidecar = Path(sidecar_path).resolve()
    source_db = Path(source_db_path).resolve()
    if recover:
        recover_sidecar(sidecar)
    local_validation = validate_sidecar(sidecar)
    connection = open_sidecar(sidecar, readonly=True)
    checks: list[IndependentCheck] = []

    def add(
        name: str,
        passed: bool,
        expected: object,
        actual: object,
        detail: object | None = None,
    ) -> None:
        checks.append(IndependentCheck(name, bool(passed), expected, actual, detail))

    try:
        rows = connection.execute("SELECT * FROM fingerprint_runs").fetchall()
        add("single_run", len(rows) == 1, 1, len(rows))
        add("quick_check", local_validation.quick_check == "ok", "ok", local_validation.quick_check)
        add(
            "integrity_check",
            local_validation.integrity_check == "ok",
            "ok",
            local_validation.integrity_check,
        )
        add(
            "foreign_key_check",
            local_validation.foreign_key_violations == 0,
            0,
            local_validation.foreign_key_violations,
        )
        semantic_total = sum(value for _, value in local_validation.semantic_violations)
        add(
            "sidecar_semantics",
            semantic_total == 0,
            0,
            semantic_total,
            dict(local_validation.semantic_violations),
        )
        if len(rows) != 1:
            return IndependentValidation(sidecar, source_db, None, None, tuple(checks))

        run = rows[0]
        source = str(run["source_name"])
        run_id = str(run["run_id"])
        stored_source_path = Path(str(run["source_db_path"])).resolve()
        add(
            "source_db_path",
            stored_source_path == source_db,
            str(stored_source_path),
            str(source_db),
        )
        sidecars = [
            str(Path(str(source_db) + suffix))
            for suffix in ("-wal", "-shm", "-journal")
            if Path(str(source_db) + suffix).exists()
        ]
        add("source_db_sidecars_absent", not sidecars, [], sidecars)
        current_source_sha = _sha256_file(source_db)
        expected_before = str(run["source_db_sha256_before"])
        expected_after = run["source_db_sha256_after"]
        sha_ok = current_source_sha == expected_before and (
            expected_after is None or current_source_sha == str(expected_after)
        )
        add(
            "source_sha_unchanged",
            sha_ok,
            {"before": expected_before, "after": expected_after},
            current_source_sha,
        )

        factory = inventory_factory or _default_inventory_factory(source, source_db)
        selection_mode = str(run["selection_mode"])
        selection_count = int(run["selection_count"])
        sample_seed = run["sample_seed"]
        selection_version = str(run["selection_version"])
        selector = (
            _SampleSelector(selection_count, str(sample_seed), selection_version)
            if selection_mode == "sample" and 1 <= selection_count <= 1000
            else None
        )
        selection_config_ok = (
            (selection_mode == "sample" and selector is not None and sample_seed is not None)
            or (selection_mode == "full" and sample_seed is None)
        )
        add(
            "selection_config",
            selection_config_ok,
            "sample size 1..1000 with seed, or full without seed",
            {
                "mode": selection_mode,
                "sample_seed": sample_seed,
                "selection_count": selection_count,
                "selection_version": selection_version,
            },
        )

        inventory_digest = hashlib.sha256()
        exclusion_digest = hashlib.sha256()
        selection_digest = hashlib.sha256()
        total = eligible = excluded = 0
        order_violations = source_name_violations = 0
        exclusion_row_mismatches = source_record_mismatches = 0
        selected_row_mismatches = 0
        previous_id: str | None = None
        require_sorted_ids = inventory_factory is None
        seen_ids: set[str] | None = None if require_sorted_ids else set()

        exclusions = iter(
            connection.execute(
                """SELECT * FROM source_asset_exclusions
                   WHERE run_id=? ORDER BY inventory_rank""",
                (run_id,),
            )
        )
        stored_exclusion = _next_or_none(exclusions)
        selected_rows = iter(
            connection.execute(
                "SELECT * FROM source_assets WHERE run_id=? ORDER BY selection_rank",
                (run_id,),
            )
        )
        stored_selected = _next_or_none(selected_rows) if selection_mode == "full" else None

        for inventory_rank, decision in enumerate(factory(), 1):
            total += 1
            current_id = decision.source_asset_id
            if require_sorted_ids:
                if previous_id is not None and current_id <= previous_id:
                    order_violations += 1
                previous_id = current_id
            else:
                assert seen_ids is not None
                if current_id in seen_ids:
                    order_violations += 1
                seen_ids.add(current_id)
            if decision.source != source:
                source_name_violations += 1

            manifest_json = inventory_decision_manifest_json(decision)
            manifest_bytes = manifest_json.encode("ascii") + b"\n"
            inventory_digest.update(manifest_bytes)

            if isinstance(decision, SourceAssetExclusion):
                excluded += 1
                exclusion_digest.update(manifest_bytes)
                expected_sha = decision.source_record_sha256
                if stored_exclusion is None:
                    exclusion_row_mismatches += 1
                    source_record_mismatches += 1
                else:
                    stored_provenance = str(stored_exclusion["provenance_json"])
                    try:
                        stored_provenance_sha = source_record_sha256(
                            stored_provenance
                        )
                    except (UnicodeEncodeError, ValueError):
                        stored_provenance_sha = "invalid"
                    exclusion_row_mismatches += int(
                        str(stored_exclusion["source_asset_id"]) != current_id
                        or str(stored_exclusion["source_asset_key"])
                        != decision.source_asset_key
                        or int(stored_exclusion["inventory_rank"]) != inventory_rank
                        or str(stored_exclusion["reason_code"]) != decision.reason_code
                        or stored_provenance != decision.source_record_json
                        or str(stored_exclusion["detail_json"]) != decision.detail_json
                    )
                    source_record_mismatches += int(
                        str(stored_exclusion["source_record_sha256"]) != expected_sha
                        or stored_provenance_sha
                        != str(stored_exclusion["source_record_sha256"])
                    )
                    stored_exclusion = _next_or_none(exclusions)
                continue

            eligible += 1
            if selector is not None:
                selector.add(decision)
            elif selection_mode == "full":
                selected = _SelectedAsset(
                    decision,
                    "full_inventory",
                    _stratum(decision),
                    None,
                    None,
                )
                selected_bytes = _canonical_json_bytes(
                    _selected_record(selected, selection_version)
                )
                selection_digest.update(selected_bytes)
                selection_digest.update(b"\n")
                row_mismatch, sha_mismatch = _selected_row_mismatches(
                    stored_selected, selected, eligible, selection_version
                )
                selected_row_mismatches += row_mismatch
                source_record_mismatches += sha_mismatch
                stored_selected = _next_or_none(selected_rows)

        if stored_exclusion is not None:
            exclusion_row_mismatches += 1 + sum(1 for _ in exclusions)

        if selector is not None:
            selected_rows = iter(
                connection.execute(
                    "SELECT * FROM source_assets WHERE run_id=? ORDER BY selection_rank",
                    (run_id,),
                )
            )
            stored_selected = _next_or_none(selected_rows)
            selected_assets = selector.finish()
            for rank, selected in enumerate(selected_assets, 1):
                selected_bytes = _canonical_json_bytes(
                    _selected_record(selected, selection_version)
                )
                selection_digest.update(selected_bytes)
                selection_digest.update(b"\n")
                row_mismatch, sha_mismatch = _selected_row_mismatches(
                    stored_selected, selected, rank, selection_version
                )
                selected_row_mismatches += row_mismatch
                source_record_mismatches += sha_mismatch
                stored_selected = _next_or_none(selected_rows)
            selected_actual = len(selected_assets)
        elif selection_mode == "full":
            selected_actual = eligible
        else:
            selected_actual = 0
        if stored_selected is not None:
            selected_row_mismatches += 1 + sum(1 for _ in selected_rows)

        add(
            "inventory_sorted_unique" if require_sorted_ids else "inventory_unique",
            order_violations == 0,
            0,
            order_violations,
        )
        add("inventory_source_name", source_name_violations == 0, 0, source_name_violations)
        source_table_count = _source_table_count(source_db) if inventory_factory is None else total
        add("source_table_inventory_count", total == source_table_count, source_table_count, total)
        expected_counts = {
            "eligible": int(run["eligible_count"]),
            "excluded": int(run["excluded_count"]),
            "source_total": int(run["source_total_count"]),
        }
        actual_counts = {
            "eligible": eligible,
            "excluded": excluded,
            "source_total": total,
        }
        add(
            "source_inventory_accounting",
            total == eligible + excluded and actual_counts == expected_counts,
            expected_counts,
            actual_counts,
        )
        add(
            "exclusion_ledger_accounting",
            exclusion_row_mismatches == 0,
            0,
            exclusion_row_mismatches,
        )
        add(
            "source_inventory_manifest",
            inventory_digest.hexdigest() == str(run["source_inventory_manifest_sha256"]),
            str(run["source_inventory_manifest_sha256"]),
            inventory_digest.hexdigest(),
        )
        add(
            "exclusion_manifest",
            exclusion_digest.hexdigest() == str(run["exclusion_manifest_sha256"]),
            str(run["exclusion_manifest_sha256"]),
            exclusion_digest.hexdigest(),
        )
        add("source_record_sha256", source_record_mismatches == 0, 0, source_record_mismatches)
        selection_actual_sha = selection_digest.hexdigest()
        selection_ok = (
            selection_config_ok
            and selected_actual == selection_count
            and selected_row_mismatches == 0
            and selection_actual_sha == str(run["selection_manifest_sha256"])
        )
        add(
            "ordered_selection_manifest",
            selection_ok,
            {
                "count": selection_count,
                "sha256": str(run["selection_manifest_sha256"]),
            },
            {
                "count": selected_actual,
                "row_mismatches": selected_row_mismatches,
                "sha256": selection_actual_sha,
            },
        )

        fingerprint_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """SELECT status,count(*) FROM fingerprints
                   WHERE run_id=? GROUP BY status""",
                (run_id,),
            )
        }
        fingerprint_total = sum(fingerprint_counts.values())
        terminal_no_pending = (
            str(run["status"]) not in _TERMINAL_SUCCESS
            or fingerprint_counts.get("pending", 0) == 0
        )
        add(
            "fingerprint_accounting",
            fingerprint_total == selection_count and terminal_no_pending,
            {"selected": selection_count, "terminal_pending": 0},
            {
                "counts": fingerprint_counts,
                "selected": fingerprint_total,
                "terminal_pending": fingerprint_counts.get("pending", 0),
            },
        )
        attempt_link_mismatches = int(
            connection.execute(
                """
                SELECT count(*)
                FROM fingerprints f
                LEFT JOIN fetch_attempts a
                  ON a.run_id=f.run_id
                 AND a.source_asset_id=f.source_asset_id
                 AND a.attempt_no=f.selected_attempt_no
                WHERE f.run_id=? AND f.status='success'
                  AND (a.attempt_no IS NULL OR a.outcome<>'success'
                       OR a.raw_response_sha256<>f.raw_response_sha256)
                """,
                (run_id,),
            ).fetchone()[0]
        )
        add(
            "successful_attempt_linkage",
            attempt_link_mismatches == 0,
            0,
            attempt_link_mismatches,
        )

        initialized_actual = {
            "excluded": int(run["initialized_excluded_count"]),
            "inventory": int(run["initialized_inventory_count"]),
            "selected": int(run["initialized_selected_count"]),
        }
        initialized_expected = {
            "excluded": excluded,
            "inventory": total,
            "selected": selection_count,
        }
        add(
            "initialization_accounting",
            initialized_actual == initialized_expected
            and run["initialization_completed_at"] is not None,
            initialized_expected,
            {
                **initialized_actual,
                "completed_at": run["initialization_completed_at"],
            },
        )

        validation_rows = {
            str(row[0]): {"severity": str(row[1]), "passed": int(row[2])}
            for row in connection.execute(
                "SELECT validation_name,severity,passed FROM validations WHERE run_id=?",
                (run_id,),
            )
        }
        required_actual = {
            name: validation_rows.get(name) for name in REQUIRED_VALIDATIONS
        }
        required_ok = all(
            required_actual[name] == {"severity": "error", "passed": 1}
            for name in REQUIRED_VALIDATIONS
        )
        add(
            "required_validations",
            required_ok,
            {name: {"severity": "error", "passed": 1} for name in REQUIRED_VALIDATIONS},
            required_actual,
        )
        add(
            "terminal_status",
            str(run["status"]) in _TERMINAL_SUCCESS,
            sorted(_TERMINAL_SUCCESS),
            str(run["status"]),
        )
        return IndependentValidation(sidecar, source_db, source, run_id, tuple(checks))
    finally:
        connection.close()


def validate_sidecar_independently(
    sidecar_path: Path | str,
    source_db_path: Path | str,
    *,
    inventory_factory: InventoryFactory | None = None,
    recover: bool = False,
) -> IndependentValidation:
    """Compatibility alias with an explicit independence-oriented name."""

    return validate_image_fingerprint_sidecar(
        sidecar_path,
        source_db_path,
        inventory_factory=inventory_factory,
        recover=recover,
    )
