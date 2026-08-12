from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import struct
from pathlib import Path

import pytest
from PIL import Image

from tools.render_cross_source_semantic_review_sheets_n100 import (
    DEFAULT_REVIEW_SEED,
    render_n100_review_sheets,
)


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


def _fixture(
    tmp_path: Path, *, count: int = 7, building_count: int = 100
) -> tuple[Path, Path, list[str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "semantic.db"
    cache = tmp_path / "review-cache"
    cache.mkdir()
    connection = sqlite3.connect(database)
    ids: list[str] = []
    try:
        connection.executescript(
            """
            CREATE TABLE semantic_runs(
              run_id TEXT, status TEXT, runner_version TEXT,
              contract_version TEXT, prompt_version TEXT,
              output_schema_sha256 TEXT, transform_version TEXT,
              model TEXT, reasoning TEXT, service_tier TEXT,
              logical_sha256 TEXT, building_count INTEGER,
              occurrence_count INTEGER, metrics_json TEXT
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
            "INSERT INTO semantic_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "semn100-fixture",
                "complete",
                "runner-v1",
                "contract-v1",
                "prompt-v1",
                "a" * 64,
                "transform-v1",
                "model-v1",
                "low",
                "fast",
                "b" * 64,
                building_count,
                count,
                json.dumps(
                    {
                        "materialization_cache": {
                            "path": str(cache.resolve()),
                            "retained_for_review": True,
                            "write_contract": "atomic-exclusive-sha256-v1",
                        }
                    }
                ),
            ),
        )
        for rank in range(1, count + 1):
            inference_id = f"semv100_{rank:06d}"
            ids.append(inference_id)
            encoded, pixel = _jpeg(
                ((rank * 31) % 255, (rank * 47) % 255, (rank * 73) % 255),
                (120 + rank, 80 + rank),
            )
            path = cache / f"{inference_id}.jpg"
            path.write_bytes(encoded)
            connection.execute(
                "INSERT INTO selected_occurrences VALUES(?,?,?,?,?)",
                ("semn100-fixture", rank, inference_id, "SECRET", "SECRET-BUILDING"),
            )
            connection.execute(
                "INSERT INTO vision_inputs VALUES(?,?,?,?,?,?,?,?)",
                (
                    "semn100-fixture",
                    inference_id,
                    "success",
                    hashlib.sha256(encoded).hexdigest(),
                    pixel,
                    120 + rank,
                    80 + rank,
                    len(encoded),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return database, cache, ids


def test_n100_renderer_is_blind_seeded_deterministic_and_six_per_sheet(
    tmp_path: Path,
) -> None:
    database, cache, input_ids = _fixture(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    first = render_n100_review_sheets(
        semantic_db=database,
        review_cache_dir=cache,
        output_dir=tmp_path / "first",
        review_seed=DEFAULT_REVIEW_SEED,
    )
    second = render_n100_review_sheets(
        semantic_db=database,
        review_cache_dir=cache,
        output_dir=tmp_path / "second",
        review_seed=DEFAULT_REVIEW_SEED,
    )
    third = render_n100_review_sheets(
        semantic_db=database,
        review_cache_dir=cache,
        output_dir=tmp_path / "third",
        review_seed="different-blind-seed",
    )

    assert first["ordered_inference_ids"] == second["ordered_inference_ids"]
    assert set(first["ordered_inference_ids"]) == set(input_ids)
    assert first["ordered_inference_ids"] != third["ordered_inference_ids"]
    assert [len(sheet["inference_ids"]) for sheet in first["sheets"]] == [6, 1]
    assert first["images_per_sheet"] == 6
    assert first["semantic_labels_exposed"] is False
    assert first["network_requests"] == first["vision_requests"] == 0
    raw = (tmp_path / "first" / "manifest.json").read_text(encoding="utf-8")
    assert "SECRET" not in raw
    assert "http://" not in raw and "https://" not in raw
    assert json.loads(raw) == first
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()


def test_n100_renderer_rejects_tamper_non_n100_and_no_clobber(tmp_path: Path) -> None:
    database, cache, _ = _fixture(tmp_path, count=1)
    path = cache / "semv100_000001.jpg"
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="identity mismatch"):
        render_n100_review_sheets(
            semantic_db=database,
            review_cache_dir=cache,
            output_dir=tmp_path / "tampered-output",
        )
    assert not (tmp_path / "tampered-output").exists()

    other_db, other_cache, _ = _fixture(
        tmp_path / "other", count=1, building_count=10
    )
    with pytest.raises(ValueError, match="100 buildings"):
        render_n100_review_sheets(
            semantic_db=other_db,
            review_cache_dir=other_cache,
            output_dir=tmp_path / "non-n100-output",
        )

    clean_db, clean_cache, _ = _fixture(tmp_path / "clean", count=1)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        render_n100_review_sheets(
            semantic_db=clean_db,
            review_cache_dir=clean_cache,
            output_dir=existing,
        )


def test_n100_renderer_rejects_copied_cache_outside_runtime_provenance(
    tmp_path: Path,
) -> None:
    database, cache, _ = _fixture(tmp_path, count=1)
    copied = tmp_path / "copied-cache"
    shutil.copytree(cache, copied)
    with pytest.raises(ValueError, match="runtime provenance"):
        render_n100_review_sheets(
            semantic_db=database,
            review_cache_dir=copied,
            output_dir=tmp_path / "should-not-render",
        )
    assert not (tmp_path / "should-not-render").exists()
