from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from canonical.divisare_image_smoke import FetchPayload
from canonical.divisare_vision_gold import (
    CANDIDATE_MANIFEST_VERSION,
    CLASSES,
    GOLD_MANIFEST_VERSION,
    REVIEWED_POOL_VERSION,
    SOURCE_PROFILE,
)
from canonical.divisare_vision_gold_finalize import (
    CELL_ORDER,
    CELL_QUOTAS,
    FINALIZER_VERSION,
    SELECTION_POLICY_VERSION,
    canonical_json_bytes,
    gold_logical_sha256,
    gold_manifest_sha256,
)
from canonical.divisare_vision_n100 import (
    choose_resolution,
    load_gold_manifest,
    reviewer_interpretation,
    run_n100,
)
from canonical.divisare_vision_runtime import run_codex_vision_batch
from tools.run_divisare_vision_n100 import require_supported_cli


def _jpeg_bytes(index: int) -> bytes:
    image = Image.new(
        "RGB",
        (80, 48),
        ((index * 37) % 256, (index * 73) % 256, (index * 109) % 256),
    )
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


def _rehash(payload: dict) -> dict:
    payload.pop("logical_sha256", None)
    payload.pop("gold_manifest_sha256", None)
    payload["logical_sha256"] = gold_logical_sha256(payload)
    payload["gold_manifest_sha256"] = gold_manifest_sha256(payload)
    return payload


def _make_gold(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes], dict[str, str]]:
    source = tmp_path / "source.db"
    source.write_bytes(b"frozen-divisare-source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    images: dict[str, bytes] = {}
    labels: dict[str, str] = {}
    samples: list[dict] = []
    rank = 0
    for class_index, label in enumerate(CLASSES):
        next_label = CLASSES[(class_index + 1) % len(CLASSES)]
        for generation, clarity, count in (
            ("modern", "clear", 13),
            ("modern", "boundary", 3),
            ("legacy", "clear", 3),
            ("legacy", "boundary", 1),
        ):
            for _ in range(count):
                rank += 1
                sample_id = "sample-%04d" % rank
                raw = _jpeg_bytes(rank)
                request_url = (
                    "https://images.divisare.com/images/%s/v1/test-%04d.jpg"
                    % (SOURCE_PROFILE, rank)
                )
                images[request_url] = raw
                labels[sample_id] = label
                acceptable = [label]
                if clarity == "boundary":
                    acceptable = sorted([label, next_label], key=CLASSES.index)
                samples.append(
                    {
                        "sample_id": sample_id,
                        "sample_rank": rank,
                        "source_identity": {
                            "candidate_id": "candidate-%04d" % rank,
                            "candidate_rank": rank,
                            "asset_key": "divisare|asset-%04d|v1" % rank,
                            "article_id": rank,
                            "building_id": "building-%04d" % rank,
                            "request_url": request_url,
                            "review_url": request_url,
                            "generation_group": generation,
                            "url_generation": (
                                "cloudinary_public_id" if generation == "modern" else "project_images"
                            ),
                        },
                        "image_evidence": {
                            "probe_status": "success",
                            "probe_final_url": request_url,
                            "http_status": 200,
                            "response_mime": "image/jpeg",
                            "response_bytes": len(raw),
                            "content_sha256": hashlib.sha256(raw).hexdigest(),
                            "original_format": "JPEG",
                            "original_mode": "RGB",
                            "original_width": 80,
                            "original_height": 48,
                            "frame_count": 1,
                            "exif_orientation": None,
                            "orientation_applied": False,
                            "oriented_width": 80,
                            "oriented_height": 48,
                            "alpha_composited": False,
                            "icc_profile_present": False,
                            "color_normalization": "srgb-assumed",
                            "normalized_width": 80,
                            "normalized_height": 48,
                            "pixel_sha256": hashlib.sha256(
                                ("pixel-%04d" % rank).encode()
                            ).hexdigest(),
                            "phash_256": hashlib.sha256(
                                ("phash-%04d" % rank).encode()
                            ).hexdigest(),
                            "exact_duplicate_group": None,
                            "is_exact_pixel_duplicate": False,
                            "duplicate_of": None,
                            "auto_exclude_exact_duplicate": False,
                            "phash_le8_matches": [],
                            "has_phash_le8_candidate": False,
                            "probe_attempt_count": 1,
                            "probe_elapsed_ms": 1,
                            "probe_completed_at": "2026-08-05T00:00:00Z",
                            "probe_error_kind": None,
                            "probe_error_message": None,
                            "probe_attempts": [
                                {
                                    "candidate_id": "candidate-%04d" % rank,
                                    "attempt_no": 1,
                                    "started_at": "2026-08-05T00:00:00Z",
                                    "elapsed_ms": 1,
                                    "outcome": "success",
                                    "final_url": request_url,
                                    "http_status": 200,
                                    "response_mime": "image/jpeg",
                                    "response_bytes": len(raw),
                                    "content_sha256": hashlib.sha256(raw).hexdigest(),
                                    "error_kind": None,
                                    "error_message": None,
                                }
                            ],
                        },
                        "human_review": {
                            "disposition": "include",
                            "gold_label": label,
                            "clarity": clarity,
                            "acceptable_labels": acceptable,
                            "high_res_viewed": True,
                            "notes": "",
                            "reviewed_at": "2026-08-05T00:00:00Z",
                        },
                    }
                )
    payload = {
        "manifest_version": GOLD_MANIFEST_VERSION,
        "finalizer_version": FINALIZER_VERSION,
        "provenance": {
            "source_db_filename": source.name,
            "source_db_sha256": source_sha,
            "candidate_manifest_version": CANDIDATE_MANIFEST_VERSION,
            "candidate_manifest_sha256": "1" * 64,
            "candidate_manifest_file_sha256": "2" * 64,
            "selection_input_manifest_sha256": "3" * 64,
            "selection_contract": {},
            "probe_contract": {},
            "reviewed_pool_version": REVIEWED_POOL_VERSION,
            "reviewed_pool_sha256": "4" * 64,
            "reviewed_pool_file_sha256": "5" * 64,
            "reviewer": "test-reviewer",
            "review_exported_at": "2026-08-05T00:00:00Z",
        },
        "selection_policy": {
            "policy_version": SELECTION_POLICY_VERSION,
            "class_order": list(CLASSES),
            "cell_order": ["/".join(cell) for cell in CELL_ORDER],
            "cell_quotas": {"/".join(cell): CELL_QUOTAS[cell] for cell in CELL_ORDER},
            "candidate_tie_break": "candidate_rank_then_candidate_id",
            "exact_pixel_policy": "canonical_representative_only",
            "phash_policy": "selected_pair_hamming_distance_gt_8",
            "phash_bits": 256,
        },
        "selection_metrics": {
            "reviewed_included_count": 100,
            "canonical_exact_duplicate_nonrepresentative_count": 0,
            "eligible_count": 100,
            "selected_count": 100,
            "search_state_count": 1,
            "cells": {
                "/".join(cell): {
                    "required": CELL_QUOTAS[cell],
                    "reviewed_included": CELL_QUOTAS[cell],
                    "exact_duplicate_nonrepresentatives": 0,
                    "eligible": CELL_QUOTAS[cell],
                    "selected": CELL_QUOTAS[cell],
                }
                for cell in CELL_ORDER
            },
        },
        "samples": samples,
    }
    _rehash(payload)
    gold = tmp_path / "gold.json"
    gold.write_bytes(canonical_json_bytes(payload) + b"\n")
    return source, gold, images, labels


def _vision_record(sample_id: str, label: str) -> dict:
    medium = "drawing" if label == "drawing" else "photograph"
    view = "plan" if label == "drawing" else label
    return {
        "asset_id": sample_id,
        "medium": medium,
        "view": view,
        "visible_materials": [],
        "visible_elements": [],
        "needs_detail_review": False,
        "confidence": 0.95,
        "evidence": "Visible evidence supports the selected architecture image class.",
    }


def _event_stream(records: list[dict]) -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps({"results": records}),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 100,
                        "output_tokens": 50,
                    },
                }
            ),
        ]
    )


def _fake_executor(
    labels: dict[str, str],
    calls: list[tuple[int, str]],
    *,
    fail_once_at: int | None = None,
):
    failed = False

    def execute(**kwargs):
        nonlocal failed
        lane = "long2048" if "long2048" in kwargs["image_paths"][0].name else "long1024"
        first_rank = int(kwargs["expected_asset_ids"][0].split("-")[1])
        batch_no = (first_rank - 1) // 5 + 1
        calls.append((batch_no, lane))
        call_no = len(calls)

        def runner(command: list[str], **_run_kwargs):
            nonlocal failed
            if fail_once_at == call_no and not failed:
                failed = True
                return subprocess.CompletedProcess(command, 7, stdout="", stderr="forced")
            records = [
                _vision_record(sample_id, labels[sample_id])
                for sample_id in kwargs["expected_asset_ids"]
            ]
            return subprocess.CompletedProcess(
                command, 0, stdout=_event_stream(records), stderr=""
            )

        return run_codex_vision_batch(**kwargs, runner=runner)

    return execute


def test_n100_end_to_end_counterbalanced_and_selects_1024(tmp_path: Path) -> None:
    source, gold, images, labels = _make_gold(tmp_path)
    output = tmp_path / "n100.db"
    report = tmp_path / "n100.md"
    calls: list[tuple[int, str]] = []
    predictions = dict(labels)
    for sample in json.loads(gold.read_text(encoding="utf-8"))["samples"]:
        if sample["human_review"]["clarity"] == "boundary":
            predictions[sample["sample_id"]] = next(
                label
                for label in sample["human_review"]["acceptable_labels"]
                if label != sample["human_review"]["gold_label"]
            )

    result = run_n100(
        source_db=source,
        gold_manifest_path=gold,
        output_db=output,
        report_path=report,
        codex_bin=Path("fake-codex.exe"),
        model="test-model",
        cli_version="codex-cli 0.146.0",
        fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
        executor=_fake_executor(predictions, calls),
    )

    assert result["quality_gate_passed"] is True
    assert result["selected_lane"] == "long1024"
    assert len(calls) == 40
    assert calls[:4] == [
        (1, "long1024"),
        (1, "long2048"),
        (2, "long2048"),
        (2, "long1024"),
    ]
    assert result["metrics"]["classification"]["long1024"]["clear"]["macro_f1"] == 1.0
    assert result["metrics"]["classification"]["long1024"]["all"]["primary_accuracy"] == 0.8
    assert result["metrics"]["classification"]["long1024"]["all"]["acceptable_accuracy"] == 1.0
    assert result["metrics"]["usage"]["input_tokens"] == 40_000
    assert output.exists() and report.exists()
    report_text = report.read_text(encoding="utf-8")
    assert "Selected lane: `long1024`" in report_text
    assert "Reviewer identifier (verbatim): `test-reviewer`" in report_text
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gold_samples").fetchone()[0] == 100
        assert conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0] == 200
        assert conn.execute("SELECT COUNT(*) FROM vision_attempts").fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM validations WHERE passed=0").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(FileExistsError, match="immutable output"):
        run_n100(
            source_db=source,
            gold_manifest_path=gold,
            output_db=output,
            report_path=tmp_path / "other.md",
            codex_bin=Path("fake-codex.exe"),
            fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
            executor=_fake_executor(labels, []),
        )


def test_n100_resume_keeps_completed_lane_and_exact_accounting(tmp_path: Path) -> None:
    source, gold, images, labels = _make_gold(tmp_path)
    output = tmp_path / "resume.db"
    report = tmp_path / "resume.md"
    first_calls: list[tuple[int, str]] = []
    with pytest.raises(RuntimeError, match="Vision N100 batch failed"):
        run_n100(
            source_db=source,
            gold_manifest_path=gold,
            output_db=output,
            report_path=report,
            codex_bin=Path("fake-codex.exe"),
            model="test-model",
            cli_version="test-cli",
            fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
            executor=_fake_executor(labels, first_calls, fail_once_at=2),
        )
    assert first_calls == [(1, "long1024"), (1, "long2048")]
    partial = output.with_name(output.name + ".partial")
    assert partial.exists()
    conn = sqlite3.connect(partial)
    try:
        assert conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0] == 5
    finally:
        conn.close()

    resume_calls: list[tuple[int, str]] = []
    result = run_n100(
        source_db=source,
        gold_manifest_path=gold,
        output_db=output,
        report_path=report,
        codex_bin=Path("fake-codex.exe"),
        model="test-model",
        cli_version="test-cli",
        resume=True,
        fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
        executor=_fake_executor(labels, resume_calls),
    )
    assert resume_calls[0] == (1, "long2048")
    assert (1, "long1024") not in resume_calls
    assert result["metrics"]["usage"]["vision_attempts"] == 41
    assert result["metrics"]["usage"]["successful_attempts"] == 40
    assert result["metrics"]["usage"]["failed_attempts"] == 1


def test_n100_rejects_changed_content_before_model_input(tmp_path: Path) -> None:
    source, gold, images, labels = _make_gold(tmp_path)
    output = tmp_path / "changed.db"
    report = tmp_path / "changed.md"
    calls: list[tuple[int, str]] = []
    with pytest.raises(RuntimeError, match="frozen response SHA mismatch"):
        run_n100(
            source_db=source,
            gold_manifest_path=gold,
            output_db=output,
            report_path=report,
            codex_bin=Path("fake-codex.exe"),
            fetcher=lambda url: FetchPayload(b"changed", 200, "image/jpeg", url),
            executor=_fake_executor(labels, calls),
        )
    assert calls == []
    with sqlite3.connect(output.with_name(output.name + ".partial")) as conn:
        row = conn.execute(
            "SELECT status,error_kind FROM fetch_results"
        ).fetchone()
        assert row == ("content_mismatch", "content_sha256_mismatch")


def test_resume_changed_content_preserves_retained_success(tmp_path: Path) -> None:
    source, gold, images, labels = _make_gold(tmp_path)
    output = tmp_path / "resume-changed.db"
    report = tmp_path / "resume-changed.md"
    with pytest.raises(RuntimeError, match="Vision N100 batch failed"):
        run_n100(
            source_db=source,
            gold_manifest_path=gold,
            output_db=output,
            report_path=report,
            codex_bin=Path("fake-codex.exe"),
            model="test-model",
            cli_version="test-cli",
            fetcher=lambda url: FetchPayload(images[url], 200, "image/jpeg", url),
            executor=_fake_executor(labels, [], fail_once_at=2),
        )
    resume_calls: list[tuple[int, str]] = []
    with pytest.raises(RuntimeError, match="frozen response SHA mismatch"):
        run_n100(
            source_db=source,
            gold_manifest_path=gold,
            output_db=output,
            report_path=report,
            codex_bin=Path("fake-codex.exe"),
            model="test-model",
            cli_version="test-cli",
            resume=True,
            fetcher=lambda url: FetchPayload(b"changed", 200, "image/jpeg", url),
            executor=_fake_executor(labels, resume_calls),
        )
    assert resume_calls == []
    partial = output.with_name(output.name + ".partial")
    conn = sqlite3.connect(partial)
    try:
        result = conn.execute(
            "SELECT status,expected_content_sha256,actual_content_sha256 FROM fetch_results ORDER BY asset_key LIMIT 1"
        ).fetchone()
        assert result[0] == "success" and result[1] == result[2]
        assert conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts WHERE status='content_mismatch'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_gold_loader_rejects_reused_article_or_building(tmp_path: Path) -> None:
    source, gold, _images, _labels = _make_gold(tmp_path)
    payload = json.loads(gold.read_text(encoding="utf-8"))
    payload["samples"][1]["source_identity"]["article_id"] = payload["samples"][0][
        "source_identity"
    ]["article_id"]
    _rehash(payload)
    invalid = tmp_path / "invalid-gold.json"
    invalid.write_bytes(canonical_json_bytes(payload) + b"\n")
    with pytest.raises(ValueError, match="source identities must be unique"):
        load_gold_manifest(invalid, source)


def test_n100_cli_version_guard() -> None:
    require_supported_cli("codex-cli 0.146.0")
    with pytest.raises(RuntimeError, match="too old"):
        require_supported_cli("codex-cli 0.145.9")
    assert "not independent-human accuracy" in reviewer_interpretation("codex-agent")


def test_resolution_rule_prefers_high_lane_or_fails_when_required() -> None:
    def classification(low_macro: float, low_recall: float, low_errors: int,
                       high_macro: float, high_recall: float, high_errors: int) -> dict:
        def lane(macro: float, recall: float, errors: int) -> dict:
            return {
                "clear": {
                    "macro_f1": macro,
                    "total": 80,
                    "primary_correct": 80 - errors,
                    "per_class": {
                        label: {"recall": recall} for label in CLASSES
                    },
                }
            }

        return {
            "long1024": lane(low_macro, low_recall, low_errors),
            "long2048": lane(high_macro, high_recall, high_errors),
        }

    assert choose_resolution(classification(0.92, 0.875, 3, 0.96, 0.9375, 0))[
        "selected_lane"
    ] == "long2048"
    assert choose_resolution(classification(0.89, 0.875, 2, 0.88, 0.875, 2))[
        "selected_lane"
    ] == "fail"
