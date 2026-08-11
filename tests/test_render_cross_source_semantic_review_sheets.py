from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import struct
from pathlib import Path

import pytest
from PIL import Image

from tools.render_cross_source_semantic_review_sheets import render_review_sheets


def _jpeg(color: tuple[int, int, int], size: tuple[int, int]) -> tuple[bytes, str]:
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(
        output,
        "JPEG",
        quality=92,
        subsampling=0,
        optimize=False,
        progressive=False,
    )
    encoded = output.getvalue()
    with Image.open(io.BytesIO(encoded)) as delivered:
        delivered.load()
        rgb = delivered.convert("RGB")
    pixel = hashlib.sha256(
        b"RGB\0" + struct.pack(">II", rgb.width, rgb.height) + rgb.tobytes()
    ).hexdigest()
    return encoded, pixel


def _fixture(tmp_path: Path, count: int = 7) -> tuple[Path, Path, list[dict]]:
    database = tmp_path / "semantic.partial"
    cache = tmp_path / "review-cache"
    cache.mkdir()
    rows = []
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE semantic_runs(
              run_id TEXT, status TEXT, runner_version TEXT,
              contract_version TEXT, prompt_version TEXT,
              output_schema_sha256 TEXT, transform_version TEXT,
              model TEXT, reasoning TEXT, service_tier TEXT,
              logical_sha256 TEXT
            );
            CREATE TABLE selected_occurrences(
              run_id TEXT, input_rank INTEGER, inference_id TEXT,
              source TEXT, source_building_id TEXT
            );
            CREATE TABLE vision_inputs(
              run_id TEXT, inference_id TEXT, status TEXT,
              derivative_encoded_sha256 TEXT, derivative_pixel_sha256 TEXT,
              derivative_width INTEGER, derivative_height INTEGER,
              derivative_bytes INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO semantic_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-fixture",
                "running",
                "runner-v1",
                "contract-v1",
                "prompt-v1",
                "a" * 64,
                "transform-v1",
                "model-v1",
                "low",
                "fast",
                None,
            ),
        )
        for rank in range(1, count + 1):
            inference_id = f"semv_{rank:06d}"
            encoded, pixel = _jpeg(
                ((rank * 31) % 255, (rank * 47) % 255, (rank * 73) % 255),
                (120 + rank, 80 + rank),
            )
            path = cache / f"{inference_id}.jpg"
            path.write_bytes(encoded)
            encoded_sha = hashlib.sha256(encoded).hexdigest()
            connection.execute(
                "INSERT INTO selected_occurrences VALUES(?,?,?,?,?)",
                ("run-fixture", rank, inference_id, "SECRET_SOURCE", "SECRET_BUILDING"),
            )
            connection.execute(
                "INSERT INTO vision_inputs VALUES(?,?,?,?,?,?,?,?)",
                (
                    "run-fixture",
                    inference_id,
                    "success" if rank % 2 else "ready",
                    encoded_sha,
                    pixel,
                    120 + rank,
                    80 + rank,
                    len(encoded),
                ),
            )
            rows.append(
                {
                    "inference_id": inference_id,
                    "encoded_sha256": encoded_sha,
                    "pixel_sha256": pixel,
                    "size": len(encoded),
                }
            )
        connection.commit()
    finally:
        connection.close()
    return database, cache, rows


def test_renders_blind_sheets_and_identity_manifest(tmp_path: Path) -> None:
    database, cache, expected = _fixture(tmp_path)
    output = tmp_path / "blind-review"
    db_before = hashlib.sha256(database.read_bytes()).hexdigest()

    result = render_review_sheets(
        semantic_db=database,
        review_cache_dir=cache,
        output_dir=output,
    )

    assert result["input_count"] == 7
    assert result["sheet_count"] == 2
    assert result["ordered_inference_ids"] == [
        row["inference_id"] for row in expected
    ]
    assert [len(sheet["inference_ids"]) for sheet in result["sheets"]] == [6, 1]
    assert result["network_requests"] == 0
    manifest_raw = (output / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_raw)
    assert manifest == result
    assert "SECRET_SOURCE" not in manifest_raw
    assert "SECRET_BUILDING" not in manifest_raw
    assert "http://" not in manifest_raw and "https://" not in manifest_raw
    for index, sheet in enumerate(result["sheets"], 1):
        path = output / f"sheet_{index:03d}.jpg"
        assert sheet["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert sheet["size_bytes"] == path.stat().st_size
        with Image.open(path) as image:
            assert image.size == (1800, 1200)
    for actual, expected_row in zip(result["inputs"], expected):
        assert actual["derivative_encoded_sha256"] == expected_row["encoded_sha256"]
        assert actual["derivative_pixel_sha256"] == expected_row["pixel_sha256"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == db_before


def test_rejects_tampered_cache_before_creating_output(tmp_path: Path) -> None:
    database, cache, _ = _fixture(tmp_path, count=1)
    path = cache / "semv_000001.jpg"
    path.write_bytes(path.read_bytes() + b"tamper")
    output = tmp_path / "blind-review"

    with pytest.raises(ValueError, match="identity mismatch"):
        render_review_sheets(
            semantic_db=database,
            review_cache_dir=cache,
            output_dir=output,
        )
    assert not output.exists()


def test_rejects_cache_accounting_mismatch_and_existing_output(tmp_path: Path) -> None:
    database, cache, _ = _fixture(tmp_path, count=1)
    (cache / "semv_999999.jpg").write_bytes((cache / "semv_000001.jpg").read_bytes())
    with pytest.raises(ValueError, match="accounting mismatch"):
        render_review_sheets(
            semantic_db=database,
            review_cache_dir=cache,
            output_dir=tmp_path / "first-output",
        )

    (cache / "semv_999999.jpg").unlink()
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        render_review_sheets(
            semantic_db=database,
            review_cache_dir=cache,
            output_dir=existing,
        )
