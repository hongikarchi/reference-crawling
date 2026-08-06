"""Freeze a pre-result, same-batch N50 stability subset from Divisare gold.

The freezer deliberately accepts only the reviewed N100 gold manifest. It
selects ten whole contiguous five-sample batches before any benchmark result
exists, so later repeatability measurements cannot be outcome-conditioned.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from canonical.divisare_vision_gold import CLASSES, GENERATION_GROUPS
from canonical.divisare_vision_gold_finalize import (
    CELL_ORDER,
    canonical_json_bytes,
    parse_json_strict,
    validate_gold_manifest,
)


MANIFEST_VERSION = "divisare-vision-stability-subset-v1.0.0"
FREEZER_VERSION = "divisare-vision-stability-subset-freezer-v1.0.0"
EXPECTED_GOLD_SAMPLE_COUNT = 100
FIXED_BATCH_SIZE = 5
EXPECTED_BATCH_COUNT = EXPECTED_GOLD_SAMPLE_COUNT // FIXED_BATCH_SIZE
SELECTED_BATCH_COUNT = 10
SELECTED_SAMPLE_COUNT = SELECTED_BATCH_COUNT * FIXED_BATCH_SIZE
CLARITIES = ("clear", "boundary")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SCORE_FIELDS = (
    "joint_cell_sum_squared_doubled_half_deviation",
    "joint_cell_max_absolute_doubled_half_deviation",
    "label_sum_squared_doubled_half_deviation",
    "generation_sum_squared_doubled_half_deviation",
    "clarity_sum_squared_doubled_half_deviation",
    "label_max_absolute_doubled_half_deviation",
    "generation_max_absolute_doubled_half_deviation",
    "clarity_max_absolute_doubled_half_deviation",
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(canonical_json_bytes(value))


def _require_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError("%s must be 64 lowercase hexadecimal characters" % name)
    return value


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("%s must be an object" % name)
    return value


def _cell_name(cell: tuple[str, str, str]) -> str:
    return "/".join(cell)


def _logical_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    provenance = dict(_require_mapping(payload.get("provenance"), "provenance"))
    provenance.pop("gold_manifest_filename", None)
    provenance.pop("gold_manifest_file_sha256", None)
    return {
        "manifest_version": payload["manifest_version"],
        "freezer_version": payload["freezer_version"],
        "provenance": provenance,
        "selection_policy": payload["selection_policy"],
        "selection_metrics": payload["selection_metrics"],
        "selected_batches": payload["selected_batches"],
        "selected_samples": payload["selected_samples"],
    }


def subset_logical_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_logical_payload(payload))


def subset_manifest_sha256(payload: Mapping[str, Any]) -> str:
    value = dict(payload)
    value.pop("subset_manifest_sha256", None)
    return _sha256_value(value)


def _sample_cell(sample: Mapping[str, Any]) -> tuple[str, str, str]:
    source = _require_mapping(sample.get("source_identity"), "sample source_identity")
    review = _require_mapping(sample.get("human_review"), "sample human_review")
    return (
        str(review.get("gold_label") or ""),
        str(source.get("generation_group") or ""),
        str(review.get("clarity") or ""),
    )


def _count_vector(samples: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    counts = Counter(_sample_cell(sample) for sample in samples)
    return tuple(counts.get(cell, 0) for cell in CELL_ORDER)


def _marginal_deltas(
    joint_deltas: Sequence[int], dimension: int
) -> tuple[int, ...]:
    if dimension == 0:
        values: Sequence[str] = CLASSES
    elif dimension == 1:
        values = GENERATION_GROUPS
    elif dimension == 2:
        values = CLARITIES
    else:
        raise ValueError("unsupported cell dimension")
    return tuple(
        sum(
            joint_deltas[index]
            for index, cell in enumerate(CELL_ORDER)
            if cell[dimension] == value
        )
        for value in values
    )


def _score(
    selected_counts: Sequence[int], total_counts: Sequence[int]
) -> tuple[int, ...]:
    joint = tuple(
        2 * selected - total
        for selected, total in zip(selected_counts, total_counts, strict=True)
    )
    labels = _marginal_deltas(joint, 0)
    generations = _marginal_deltas(joint, 1)
    clarities = _marginal_deltas(joint, 2)
    return (
        sum(value * value for value in joint),
        max(abs(value) for value in joint),
        sum(value * value for value in labels),
        sum(value * value for value in generations),
        sum(value * value for value in clarities),
        max(abs(value) for value in labels),
        max(abs(value) for value in generations),
        max(abs(value) for value in clarities),
    )


def _selection_policy() -> dict[str, Any]:
    return {
        "policy_version": FREEZER_VERSION,
        "input_scope": "validated_gold_manifest_only_pre_result",
        "result_conditioning": False,
        "gold_sample_count": EXPECTED_GOLD_SAMPLE_COUNT,
        "batch_definition": "contiguous_sample_rank_groups",
        "batch_size": FIXED_BATCH_SIZE,
        "batch_count": EXPECTED_BATCH_COUNT,
        "selected_batch_count": SELECTED_BATCH_COUNT,
        "selected_sample_count": SELECTED_SAMPLE_COUNT,
        "class_order": list(CLASSES),
        "generation_order": list(GENERATION_GROUPS),
        "clarity_order": list(CLARITIES),
        "joint_cell_order": [_cell_name(cell) for cell in CELL_ORDER],
        "target": "one_half_of_each_gold_distribution_count",
        "deviation_unit": "2*selected_count-gold_count",
        "score_order": list(SCORE_FIELDS),
        "search": "exhaustive_all_20_choose_10_whole_batch_combinations",
        "tie_break": "lexicographically_smallest_selected_batch_numbers",
    }


def _score_mapping(score: Sequence[int]) -> dict[str, int]:
    return dict(zip(SCORE_FIELDS, score, strict=True))


def _search_batches(
    batch_vectors: Sequence[Sequence[int]], total_counts: Sequence[int]
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], int, int]:
    best_score: tuple[int, ...] | None = None
    best_batches: tuple[int, ...] | None = None
    best_counts: tuple[int, ...] | None = None
    optimal_score_tie_count = 0
    combinations_evaluated = 0
    for batch_indexes in itertools.combinations(
        range(EXPECTED_BATCH_COUNT), SELECTED_BATCH_COUNT
    ):
        combinations_evaluated += 1
        selected_counts = tuple(
            sum(batch_vectors[batch_index][cell_index] for batch_index in batch_indexes)
            for cell_index in range(len(CELL_ORDER))
        )
        score = _score(selected_counts, total_counts)
        if best_score is None or score < best_score:
            best_score = score
            best_batches = batch_indexes
            best_counts = selected_counts
            optimal_score_tie_count = 1
        elif score == best_score:
            optimal_score_tie_count += 1

    expected_combinations = math.comb(EXPECTED_BATCH_COUNT, SELECTED_BATCH_COUNT)
    if combinations_evaluated != expected_combinations:
        raise RuntimeError("exhaustive stability subset search accounting mismatch")
    if best_score is None or best_batches is None or best_counts is None:
        raise RuntimeError("stability subset search produced no selection")
    return (
        best_batches,
        best_counts,
        best_score,
        optimal_score_tie_count,
        combinations_evaluated,
    )


def _distribution_metrics(
    selected_counts: Sequence[int], total_counts: Sequence[int]
) -> dict[str, Any]:
    joint_deltas = tuple(
        2 * selected - total
        for selected, total in zip(selected_counts, total_counts, strict=True)
    )
    joint = {
        _cell_name(cell): {
            "gold_count": total_counts[index],
            "selected_count": selected_counts[index],
            "doubled_half_deviation": joint_deltas[index],
        }
        for index, cell in enumerate(CELL_ORDER)
    }
    marginals: dict[str, Any] = {}
    for name, dimension, values in (
        ("label", 0, CLASSES),
        ("generation", 1, GENERATION_GROUPS),
        ("clarity", 2, CLARITIES),
    ):
        rows: dict[str, Any] = {}
        for value in values:
            indexes = [
                index for index, cell in enumerate(CELL_ORDER) if cell[dimension] == value
            ]
            gold_count = sum(total_counts[index] for index in indexes)
            selected_count = sum(selected_counts[index] for index in indexes)
            rows[value] = {
                "gold_count": gold_count,
                "selected_count": selected_count,
                "doubled_half_deviation": 2 * selected_count - gold_count,
            }
        marginals[name] = rows
    return {"joint_cells": joint, "marginals": marginals}


def _selected_sample_record(sample: Mapping[str, Any], batch_no: int) -> dict[str, Any]:
    source = _require_mapping(sample.get("source_identity"), "sample source_identity")
    evidence = _require_mapping(sample.get("image_evidence"), "sample image_evidence")
    review = _require_mapping(sample.get("human_review"), "sample human_review")
    return {
        "batch_no": batch_no,
        "sample_rank": sample["sample_rank"],
        "sample_id": sample["sample_id"],
        "candidate_id": source["candidate_id"],
        "asset_key": source["asset_key"],
        "content_sha256": evidence["content_sha256"],
        "gold_label": review["gold_label"],
        "generation_group": source["generation_group"],
        "clarity": review["clarity"],
        "acceptable_labels": list(review["acceptable_labels"]),
    }


def build_stability_subset(
    *,
    gold_manifest: Mapping[str, Any],
    gold_manifest_file_sha256: str,
    gold_manifest_filename: str,
) -> dict[str, Any]:
    """Build the deterministic subset; benchmark outputs are not an input."""
    validate_gold_manifest(gold_manifest)
    gold_file_sha = _require_sha(
        gold_manifest_file_sha256, "gold manifest file SHA"
    )
    if not isinstance(gold_manifest_filename, str) or not gold_manifest_filename:
        raise ValueError("gold manifest filename must be non-empty")
    samples_value = gold_manifest.get("samples")
    if not isinstance(samples_value, list) or len(samples_value) != EXPECTED_GOLD_SAMPLE_COUNT:
        raise ValueError("gold manifest must contain exactly 100 samples")
    samples = [
        _require_mapping(sample, "gold sample") for sample in samples_value
    ]
    batches = [
        samples[offset : offset + FIXED_BATCH_SIZE]
        for offset in range(0, EXPECTED_GOLD_SAMPLE_COUNT, FIXED_BATCH_SIZE)
    ]
    batch_vectors = [_count_vector(batch) for batch in batches]
    total_counts = tuple(
        sum(vector[index] for vector in batch_vectors)
        for index in range(len(CELL_ORDER))
    )

    (
        best_batches,
        best_counts,
        best_score,
        optimal_score_tie_count,
        combinations_evaluated,
    ) = _search_batches(batch_vectors, total_counts)

    selected_batch_numbers = tuple(index + 1 for index in best_batches)
    selected_batches = []
    selected_samples = []
    for batch_index in best_batches:
        batch_no = batch_index + 1
        batch = batches[batch_index]
        selected_batches.append(
            {
                "batch_no": batch_no,
                "sample_rank_start": batch_index * FIXED_BATCH_SIZE + 1,
                "sample_rank_end": (batch_index + 1) * FIXED_BATCH_SIZE,
                "sample_ids": [sample["sample_id"] for sample in batch],
                "joint_cell_counts": {
                    _cell_name(cell): batch_vectors[batch_index][cell_index]
                    for cell_index, cell in enumerate(CELL_ORDER)
                    if batch_vectors[batch_index][cell_index]
                },
            }
        )
        selected_samples.extend(
            _selected_sample_record(sample, batch_no) for sample in batch
        )

    payload: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "freezer_version": FREEZER_VERSION,
        "provenance": {
            "gold_manifest_filename": gold_manifest_filename,
            "gold_manifest_version": gold_manifest["manifest_version"],
            "gold_manifest_file_sha256": gold_file_sha,
            "gold_logical_sha256": gold_manifest["logical_sha256"],
            "gold_manifest_sha256": gold_manifest["gold_manifest_sha256"],
        },
        "selection_policy": _selection_policy(),
        "selection_metrics": {
            "combinations_evaluated": combinations_evaluated,
            "optimal_score_tie_count": optimal_score_tie_count,
            "best_score": _score_mapping(best_score),
            "selected_batch_numbers": list(selected_batch_numbers),
            "excluded_batch_numbers": [
                batch_no
                for batch_no in range(1, EXPECTED_BATCH_COUNT + 1)
                if batch_no not in selected_batch_numbers
            ],
            "distribution": _distribution_metrics(best_counts, total_counts),
        },
        "selected_batches": selected_batches,
        "selected_samples": selected_samples,
    }
    payload["logical_sha256"] = subset_logical_sha256(payload)
    payload["subset_manifest_sha256"] = subset_manifest_sha256(payload)
    validate_stability_subset(payload)
    return payload


def validate_stability_subset(
    payload: Mapping[str, Any],
    *,
    gold_manifest: Mapping[str, Any] | None = None,
    gold_manifest_file_sha256: str | None = None,
    gold_manifest_filename: str | None = None,
) -> None:
    canonical_json_bytes(payload)
    expected_top = {
        "manifest_version",
        "freezer_version",
        "provenance",
        "selection_policy",
        "selection_metrics",
        "selected_batches",
        "selected_samples",
        "logical_sha256",
        "subset_manifest_sha256",
    }
    if set(payload) != expected_top:
        raise ValueError("stability subset top-level schema mismatch")
    if payload.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("stability subset manifest version mismatch")
    if payload.get("freezer_version") != FREEZER_VERSION:
        raise ValueError("stability subset freezer version mismatch")
    logical = _require_sha(payload.get("logical_sha256"), "subset logical SHA")
    if logical != subset_logical_sha256(payload):
        raise ValueError("stability subset logical SHA mismatch")
    manifest_sha = _require_sha(
        payload.get("subset_manifest_sha256"), "subset manifest SHA"
    )
    if manifest_sha != subset_manifest_sha256(payload):
        raise ValueError("stability subset manifest SHA mismatch")

    provenance = _require_mapping(payload.get("provenance"), "provenance")
    expected_provenance = {
        "gold_manifest_filename",
        "gold_manifest_version",
        "gold_manifest_file_sha256",
        "gold_logical_sha256",
        "gold_manifest_sha256",
    }
    if set(provenance) != expected_provenance:
        raise ValueError("stability subset provenance schema mismatch")
    for field in (
        "gold_manifest_file_sha256",
        "gold_logical_sha256",
        "gold_manifest_sha256",
    ):
        _require_sha(provenance.get(field), "provenance.%s" % field)
    if payload.get("selection_policy") != _selection_policy():
        raise ValueError("stability subset selection policy mismatch")

    metrics = _require_mapping(payload.get("selection_metrics"), "selection_metrics")
    if set(metrics) != {
        "combinations_evaluated",
        "optimal_score_tie_count",
        "best_score",
        "selected_batch_numbers",
        "excluded_batch_numbers",
        "distribution",
    }:
        raise ValueError("stability subset metrics schema mismatch")
    if metrics.get("combinations_evaluated") != math.comb(
        EXPECTED_BATCH_COUNT, SELECTED_BATCH_COUNT
    ):
        raise ValueError("stability subset exhaustive search count mismatch")
    if not isinstance(metrics.get("optimal_score_tie_count"), int) or metrics[
        "optimal_score_tie_count"
    ] < 1:
        raise ValueError("stability subset optimal tie count is invalid")
    best_score = _require_mapping(metrics.get("best_score"), "best_score")
    if set(best_score) != set(SCORE_FIELDS) or any(
        not isinstance(best_score[field], int) or best_score[field] < 0
        for field in SCORE_FIELDS
    ):
        raise ValueError("stability subset best score is invalid")

    batch_numbers = metrics.get("selected_batch_numbers")
    excluded = metrics.get("excluded_batch_numbers")
    if (
        not isinstance(batch_numbers, list)
        or len(batch_numbers) != SELECTED_BATCH_COUNT
        or batch_numbers != sorted(set(batch_numbers))
        or any(
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 20
            for value in batch_numbers
        )
    ):
        raise ValueError("stability subset selected batches are invalid")
    expected_excluded = [
        value for value in range(1, EXPECTED_BATCH_COUNT + 1) if value not in batch_numbers
    ]
    if excluded != expected_excluded:
        raise ValueError("stability subset excluded batches are invalid")

    batches = payload.get("selected_batches")
    selected = payload.get("selected_samples")
    if not isinstance(batches, list) or len(batches) != SELECTED_BATCH_COUNT:
        raise ValueError("stability subset must contain ten selected batches")
    if not isinstance(selected, list) or len(selected) != SELECTED_SAMPLE_COUNT:
        raise ValueError("stability subset must contain fifty selected samples")
    flattened_ids: list[str] = []
    for expected_batch_no, raw_batch in zip(batch_numbers, batches, strict=True):
        batch = _require_mapping(raw_batch, "selected batch")
        if set(batch) != {
            "batch_no",
            "sample_rank_start",
            "sample_rank_end",
            "sample_ids",
            "joint_cell_counts",
        }:
            raise ValueError("stability subset batch schema mismatch")
        start = (expected_batch_no - 1) * FIXED_BATCH_SIZE + 1
        end = expected_batch_no * FIXED_BATCH_SIZE
        if (
            batch.get("batch_no") != expected_batch_no
            or batch.get("sample_rank_start") != start
            or batch.get("sample_rank_end") != end
        ):
            raise ValueError("stability subset batch range mismatch")
        ids = batch.get("sample_ids")
        if not isinstance(ids, list) or len(ids) != FIXED_BATCH_SIZE:
            raise ValueError("stability subset batch sample IDs are invalid")
        flattened_ids.extend(ids)
        cell_counts = _require_mapping(batch.get("joint_cell_counts"), "batch cells")
        if any(
            key not in {_cell_name(cell) for cell in CELL_ORDER}
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for key, value in cell_counts.items()
        ) or sum(cell_counts.values()) != FIXED_BATCH_SIZE:
            raise ValueError("stability subset batch cell counts are invalid")

    selected_ids: list[str] = []
    selected_counts: Counter[tuple[str, str, str]] = Counter()
    for raw_sample in selected:
        sample = _require_mapping(raw_sample, "selected sample")
        if set(sample) != {
            "batch_no",
            "sample_rank",
            "sample_id",
            "candidate_id",
            "asset_key",
            "content_sha256",
            "gold_label",
            "generation_group",
            "clarity",
            "acceptable_labels",
        }:
            raise ValueError("stability subset sample schema mismatch")
        rank = sample.get("sample_rank")
        batch_no = sample.get("batch_no")
        if (
            not isinstance(rank, int)
            or isinstance(rank, bool)
            or batch_no != ((rank - 1) // FIXED_BATCH_SIZE) + 1
            or batch_no not in batch_numbers
            or sample.get("sample_id") != "sample-%04d" % rank
        ):
            raise ValueError("stability subset sample rank/batch mismatch")
        cell = (
            sample.get("gold_label"),
            sample.get("generation_group"),
            sample.get("clarity"),
        )
        if cell not in CELL_ORDER:
            raise ValueError("stability subset sample cell is invalid")
        acceptable = sample.get("acceptable_labels")
        if not isinstance(acceptable, list) or sample.get("gold_label") not in acceptable:
            raise ValueError("stability subset acceptable labels are invalid")
        _require_sha(sample.get("content_sha256"), "sample content SHA")
        selected_ids.append(sample["sample_id"])
        selected_counts[cell] += 1
    if len(set(selected_ids)) != SELECTED_SAMPLE_COUNT or selected_ids != flattened_ids:
        raise ValueError("stability subset sample identities do not match batches")

    distribution = _require_mapping(metrics.get("distribution"), "distribution")
    if set(distribution) != {"joint_cells", "marginals"}:
        raise ValueError("stability subset distribution schema mismatch")
    joint = _require_mapping(distribution.get("joint_cells"), "joint distribution")
    if set(joint) != {_cell_name(cell) for cell in CELL_ORDER}:
        raise ValueError("stability subset joint distribution cells mismatch")
    total_counts: list[int] = []
    selected_vector: list[int] = []
    for cell in CELL_ORDER:
        row = _require_mapping(joint[_cell_name(cell)], "joint distribution row")
        if set(row) != {"gold_count", "selected_count", "doubled_half_deviation"}:
            raise ValueError("stability subset joint distribution row mismatch")
        total = row.get("gold_count")
        chosen = row.get("selected_count")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or not isinstance(chosen, int)
            or isinstance(chosen, bool)
            or total < 0
            or chosen < 0
            or row.get("doubled_half_deviation") != 2 * chosen - total
            or chosen != selected_counts.get(cell, 0)
        ):
            raise ValueError("stability subset joint distribution accounting mismatch")
        total_counts.append(total)
        selected_vector.append(chosen)
    actual_score = _score(selected_vector, total_counts)
    if _score_mapping(actual_score) != dict(best_score):
        raise ValueError("stability subset score accounting mismatch")
    expected_distribution = _distribution_metrics(selected_vector, total_counts)
    if distribution != expected_distribution:
        raise ValueError("stability subset distribution accounting mismatch")

    if gold_manifest is not None:
        validate_gold_manifest(gold_manifest)
        if gold_manifest_file_sha256 is None or gold_manifest_filename is None:
            raise ValueError("gold file SHA and filename are required for bound validation")
        expected_provenance_values = {
            "gold_manifest_filename": gold_manifest_filename,
            "gold_manifest_version": gold_manifest["manifest_version"],
            "gold_manifest_file_sha256": _require_sha(
                gold_manifest_file_sha256, "gold manifest file SHA"
            ),
            "gold_logical_sha256": gold_manifest["logical_sha256"],
            "gold_manifest_sha256": gold_manifest["gold_manifest_sha256"],
        }
        if dict(provenance) != expected_provenance_values:
            raise ValueError("stability subset is not bound to the supplied gold manifest")
        gold_samples = [
            _require_mapping(sample, "gold sample")
            for sample in gold_manifest["samples"]
        ]
        gold_batches = [
            gold_samples[offset : offset + FIXED_BATCH_SIZE]
            for offset in range(0, EXPECTED_GOLD_SAMPLE_COUNT, FIXED_BATCH_SIZE)
        ]
        gold_batch_vectors = [_count_vector(batch) for batch in gold_batches]
        gold_total_counts = tuple(
            sum(vector[index] for vector in gold_batch_vectors)
            for index in range(len(CELL_ORDER))
        )
        (
            expected_batch_indexes,
            expected_selected_counts,
            expected_score,
            expected_tie_count,
            expected_combinations,
        ) = _search_batches(gold_batch_vectors, gold_total_counts)
        expected_batch_numbers = [index + 1 for index in expected_batch_indexes]
        if batch_numbers != expected_batch_numbers:
            raise ValueError("stability subset is not the pre-result optimal batch selection")
        if (
            metrics["combinations_evaluated"] != expected_combinations
            or metrics["optimal_score_tie_count"] != expected_tie_count
            or dict(best_score) != _score_mapping(expected_score)
            or distribution
            != _distribution_metrics(expected_selected_counts, gold_total_counts)
        ):
            raise ValueError("stability subset optimization metrics differ from supplied gold")
        gold_by_id = {sample["sample_id"]: sample for sample in gold_manifest["samples"]}
        for record in selected:
            gold = gold_by_id.get(record["sample_id"])
            if gold is None:
                raise ValueError("stability subset sample is absent from supplied gold")
            expected_record = _selected_sample_record(gold, record["batch_no"])
            if record != expected_record:
                raise ValueError("stability subset sample differs from supplied gold")
        for raw_batch in batches:
            batch_no = raw_batch["batch_no"]
            expected_vector = gold_batch_vectors[batch_no - 1]
            expected_cells = {
                _cell_name(cell): expected_vector[index]
                for index, cell in enumerate(CELL_ORDER)
                if expected_vector[index]
            }
            if raw_batch["joint_cell_counts"] != expected_cells:
                raise ValueError("stability subset batch cells differ from supplied gold")


def write_json_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError("immutable stability subset already exists: %s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".partial", dir=path.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError as exc:
            raise FileExistsError(
                "immutable stability subset already exists: %s" % path
            ) from exc
    finally:
        temp.unlink(missing_ok=True)


def freeze_stability_subset_file(
    *, gold_manifest_path: Path, output_path: Path
) -> dict[str, Any]:
    gold_manifest_path = gold_manifest_path.resolve()
    output_path = output_path.resolve()
    if gold_manifest_path == output_path:
        raise ValueError("gold manifest and stability subset paths must differ")
    if output_path.exists():
        raise FileExistsError("immutable stability subset already exists: %s" % output_path)
    raw = gold_manifest_path.read_bytes()
    gold = parse_json_strict(raw, label="gold manifest")
    file_sha = _sha256_bytes(raw)
    payload = build_stability_subset(
        gold_manifest=gold,
        gold_manifest_file_sha256=file_sha,
        gold_manifest_filename=gold_manifest_path.name,
    )
    if gold_manifest_path.read_bytes() != raw:
        raise RuntimeError("gold manifest changed during stability subset freeze")
    write_json_no_clobber(output_path, payload)
    return payload
