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
from canonical.divisare_vision_benchmark import (
    LANES,
    VISION_OUTPUT_SCHEMA,
    decode_source,
    initialize_sidecar,
    inference_asset_id,
    logical_sha256,
    normalize_vision_batch,
    prepare_lanes,
    run_benchmark,
    sample_manifest_sha256,
    select_vision_sample,
)
from canonical.divisare_vision_runtime import run_codex_vision_batch
from tools.run_divisare_vision_benchmark import require_supported_cli


def _make_source(path: Path, count: int = 40) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE image_assets(
          asset_key TEXT PRIMARY KEY,
          url_generation TEXT NOT NULL,
          original_filename TEXT
        );
        CREATE TABLE image_urls(
          url_id INTEGER PRIMARY KEY,
          asset_key TEXT NOT NULL,
          url TEXT NOT NULL,
          transform_signature TEXT,
          url_generation TEXT NOT NULL
        );
        CREATE TABLE article_image_occurrences(
          article_id INTEGER NOT NULL,
          role TEXT NOT NULL,
          position INTEGER NOT NULL,
          asset_key TEXT NOT NULL,
          url_id INTEGER NOT NULL
        );
        CREATE TABLE image_url_hints(
          asset_key TEXT NOT NULL,
          url_id INTEGER NOT NULL,
          hint TEXT NOT NULL
        );
        CREATE TABLE v_article_content_hints(
          article_id INTEGER NOT NULL,
          content_hint TEXT NOT NULL,
          confidence REAL,
          source_tags TEXT
        );
        CREATE TABLE article_tags(
          article_id INTEGER NOT NULL,
          tag_slug TEXT NOT NULL,
          ordinal INTEGER NOT NULL
        );
        CREATE TABLE source_tags(
          tag_slug TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          album_slug TEXT
        );
        """
    )
    albums = [
        ("tag-interior", "Interior", "private-interiors"),
        ("tag-material", "Material", "materiality"),
        ("tag-topic", "Topic", "topics"),
    ]
    conn.executemany("INSERT INTO source_tags VALUES(?,?,?)", albums)
    for index in range(1, count + 1):
        key = f"divisare|asset-{index:03d}|v1"
        generation = "project_images" if index % 7 == 0 else "cloudinary_public_id"
        extension = "pdf" if index % 13 == 0 else "jpg"
        filename = f"asset-{index:03d}.{extension}"
        url = (
            "https://images.divisare.com/images/f_auto,q_auto,w_auto/"
            f"v1/asset-{index:03d}/project-{index:03d}.{extension}"
        )
        conn.execute("INSERT INTO image_assets VALUES(?,?,?)", (key, generation, filename))
        conn.execute(
            "INSERT INTO image_urls VALUES(?,?,?,?,?)",
            (index, key, url, "f_auto,q_auto,w_auto", generation),
        )
        role = "cover" if index % 5 == 0 else "gallery"
        conn.execute(
            "INSERT INTO article_image_occurrences VALUES(?,?,?,?,?)",
            (index, role, 0, key, index),
        )
        if index % 11 == 0:
            conn.execute("INSERT INTO image_url_hints VALUES(?,?,?)", (key, index, "drawing"))
        if index % 9 == 0:
            conn.execute(
                "INSERT INTO v_article_content_hints VALUES(?,?,?,?)",
                (index, "Plan", 0.8, "plans-of-houses"),
            )
        if index % 8 == 0:
            conn.execute("INSERT INTO article_tags VALUES(?,?,?)", (index, "tag-interior", 0))
        elif index % 6 == 0:
            conn.execute("INSERT INTO article_tags VALUES(?,?,?)", (index, "tag-material", 0))
        elif index % 10 == 0:
            conn.execute("INSERT INTO article_tags VALUES(?,?,?)", (index, "tag-topic", 0))
    conn.commit()
    conn.close()


def _jpeg_bytes(size: tuple[int, int] = (1600, 800)) -> bytes:
    image = Image.new("RGB", size, (90, 130, 170))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


def _record(asset_id: str, *, view: str = "exterior") -> dict:
    return {
        "asset_id": asset_id,
        "medium": "photograph",
        "view": view,
        "visible_materials": ["concrete", "glass"],
        "visible_elements": ["louver"],
        "needs_detail_review": False,
        "confidence": 0.91,
        "evidence": "A photographed facade and its concrete frame are visible.",
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


def _fake_executor(*, fail_lane: str | None = None, calls: list[str] | None = None):
    def execute(**kwargs):
        lane = "long2048" if "long2048" in kwargs["image_paths"][0].name else "long1024"
        if calls is not None:
            calls.append(lane)

        def runner(command: list[str], **_run_kwargs):
            if lane == fail_lane:
                return subprocess.CompletedProcess(command, 7, stdout="", stderr="forced")
            records = [_record(value) for value in kwargs["expected_asset_ids"]]
            return subprocess.CompletedProcess(
                command, 0, stdout=_event_stream(records), stderr=""
            )

        return run_codex_vision_batch(**kwargs, runner=runner)

    return execute


def test_semantic_sample_is_stable_prefix(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_source(source)

    n10 = select_vision_sample(source, 10)
    n20 = select_vision_sample(source, 20)

    assert [item.asset_key for item in n10] == [item.asset_key for item in n20[:10]]
    assert len({item.asset_key for item in n20}) == 20
    assert len({item.cohort for item in n10}) >= 5
    assert sample_manifest_sha256(n10) == sample_manifest_sha256(
        select_vision_sample(source, 10)
    )


def test_local_derivatives_share_source_and_do_not_upscale() -> None:
    decoded = decode_source(_jpeg_bytes((2048, 1024)))
    derivatives = {item.lane: item for item in prepare_lanes(decoded)}

    assert derivatives["long1024"].width == 1024
    assert derivatives["long1024"].height == 512
    assert derivatives["long2048"].width == 2048
    assert derivatives["long2048"].height == 1024
    assert derivatives["long1024"].raw_patch_count == 512
    assert derivatives["long2048"].raw_patch_count == 2048
    assert len(derivatives["long1024"].encoded_sha256) == 64
    assert derivatives == {item.lane: item for item in prepare_lanes(decoded)}

    small = {item.lane: item for item in prepare_lanes(decode_source(_jpeg_bytes((320, 200))))}
    assert small["long1024"].width == small["long2048"].width == 320
    assert small["long1024"].height == small["long2048"].height == 200


def test_alpha_is_composited_on_white() -> None:
    image = Image.new("RGBA", (2, 1), (255, 0, 0, 0))
    image.putpixel((1, 0), (0, 0, 0, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")

    decoded = decode_source(output.getvalue())

    assert decoded.image.getpixel((0, 0)) == (255, 255, 255)
    assert decoded.image.getpixel((1, 0)) == (0, 0, 0)


def test_output_schema_and_semantic_validation_abstain_instead_of_default() -> None:
    assert VISION_OUTPUT_SCHEMA["required"] == ["results"]
    rows = normalize_vision_batch([_record("asset-a", view="unknown")], ["asset-a"])
    assert rows[0]["legacy_type"] == "unknown"
    assert rows[0]["inference_asset_id"] == "asset-a"

    invalid = _record("asset-a")
    invalid["visible_elements"] = ["column"]
    with pytest.raises(ValueError, match="unsupported value"):
        normalize_vision_batch([invalid], ["asset-a"])
    with pytest.raises(ValueError, match="count"):
        normalize_vision_batch([], ["asset-a"])


def test_model_facing_ids_are_opaque() -> None:
    assert inference_asset_id(1) == "sample-0001"
    assert inference_asset_id(999) == "sample-0999"
    with pytest.raises(ValueError, match="positive"):
        inference_asset_id(0)


def test_cli_version_gate() -> None:
    require_supported_cli("codex-cli 0.146.0")
    require_supported_cli("codex-cli 1.0.0")
    with pytest.raises(RuntimeError, match="too old"):
        require_supported_cli("codex-cli 0.138.0-alpha.7")
    with pytest.raises(RuntimeError, match="could not parse"):
        require_supported_cli("unknown")


def test_sidecar_initialization_and_logical_sha(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_source(source)
    sample = select_vision_sample(source, 5)
    sidecar = tmp_path / "sidecar.db"
    conn = sqlite3.connect(sidecar)

    initialize_sidecar(
        conn,
        sample=sample,
        source_db_path=source.resolve(),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        batch_size=5,
        model="test-model",
        reasoning="low",
        service_tier="fast",
        cli_version="test-cli",
        started_at="2026-08-04T00:00:00Z",
    )

    assert conn.execute("SELECT COUNT(*) FROM sample_assets").fetchone()[0] == 5
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert logical_sha256(conn) == logical_sha256(conn)
    conn.close()


def test_end_to_end_fake_runtime_no_clobber_and_accounting(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "benchmark.db"
    report = tmp_path / "benchmark.md"
    _make_source(source)
    raw = _jpeg_bytes()

    def fetcher(url: str) -> FetchPayload:
        assert "h_2048" in url and "w_2048" in url
        return FetchPayload(raw=raw, http_status=200, mime_type="image/jpeg", final_url=url)

    calls: list[str] = []
    result = run_benchmark(
        source_db=source,
        output_db=output,
        report_path=report,
        limit=4,
        batch_size=2,
        codex_bin=Path("fake-codex.exe"),
        model="test-model",
        cli_version="test-cli",
        fetcher=fetcher,
        executor=_fake_executor(calls=calls),
    )

    assert output.exists() and report.exists()
    assert not output.with_name(output.name + ".partial").exists()
    assert result["metrics"]["fetch_success"] == 4
    assert result["metrics"]["derived_inputs"] == 8
    assert result["metrics"]["vision_results"] == 8
    assert result["metrics"]["input_tokens"] == 4000
    assert calls == ["long1024", "long2048", "long1024", "long2048"]
    assert "Agreement is not accuracy" in report.read_text(encoding="utf-8")
    with sqlite3.connect(output) as conn:
        rows = conn.execute(
            "SELECT asset_key,response_json FROM vision_results ORDER BY asset_key,lane"
        ).fetchall()
        attempt = conn.execute(
            "SELECT stderr_excerpt,non_json_lines_json FROM vision_attempts ORDER BY attempt_id LIMIT 1"
        ).fetchone()
    assert all(asset_key.startswith("divisare|") for asset_key, _payload in rows)
    assert all(
        json.loads(payload)["inference_asset_id"].startswith("sample-")
        for _asset_key, payload in rows
    )
    assert attempt == (None, "[]")
    with pytest.raises(FileExistsError, match="immutable output"):
        run_benchmark(
            source_db=source,
            output_db=output,
            report_path=tmp_path / "other.md",
            limit=4,
            batch_size=2,
            codex_bin=Path("fake-codex.exe"),
            fetcher=fetcher,
            executor=_fake_executor(),
        )


def test_resume_does_not_repeat_successful_lane(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "resume.db"
    report = tmp_path / "resume.md"
    _make_source(source)
    raw = _jpeg_bytes()

    def fetcher(url: str) -> FetchPayload:
        return FetchPayload(raw=raw, http_status=200, mime_type="image/jpeg", final_url=url)

    first_calls: list[str] = []
    with pytest.raises(RuntimeError, match="Vision batch failed"):
        run_benchmark(
            source_db=source,
            output_db=output,
            report_path=report,
            limit=2,
            batch_size=2,
            codex_bin=Path("fake-codex.exe"),
            model="test-model",
            fetcher=fetcher,
            executor=_fake_executor(fail_lane="long2048", calls=first_calls),
        )
    assert first_calls == ["long1024", "long2048"]
    assert output.with_name(output.name + ".partial").exists()
    conn = sqlite3.connect(output.with_name(output.name + ".partial"))
    try:
        assert conn.execute(
            "SELECT stderr_excerpt FROM vision_attempts WHERE status='failed'"
        ).fetchone()[0] == "forced"
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="resume contract mismatch"):
        run_benchmark(
            source_db=source,
            output_db=output,
            report_path=report,
            limit=2,
            batch_size=1,
            codex_bin=Path("fake-codex.exe"),
            model="test-model",
            resume=True,
            fetcher=fetcher,
            executor=_fake_executor(),
        )

    resume_calls: list[str] = []
    run_benchmark(
        source_db=source,
        output_db=output,
        report_path=report,
        limit=2,
        batch_size=2,
        codex_bin=Path("fake-codex.exe"),
        model="test-model",
        resume=True,
        fetcher=fetcher,
        executor=_fake_executor(calls=resume_calls),
    )

    assert resume_calls == ["long2048"]
    assert output.exists() and report.exists()


def test_resume_rejects_changed_source_response(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "changed.db"
    report = tmp_path / "changed.md"
    _make_source(source)
    initial_raw = _jpeg_bytes((1600, 800))

    with pytest.raises(RuntimeError, match="Vision batch failed"):
        run_benchmark(
            source_db=source,
            output_db=output,
            report_path=report,
            limit=2,
            batch_size=2,
            codex_bin=Path("fake-codex.exe"),
            model="test-model",
            cli_version="test-cli",
            fetcher=lambda url: FetchPayload(initial_raw, 200, "image/jpeg", url),
            executor=_fake_executor(fail_lane="long2048"),
        )

    partial = output.with_name(output.name + ".partial")
    with sqlite3.connect(partial) as conn:
        before_fetch = conn.execute(
            "SELECT response_sha256 FROM fetch_results ORDER BY asset_key"
        ).fetchall()
        assert conn.execute(
            "SELECT COUNT(*) FROM vision_results WHERE lane='long1024'"
        ).fetchone()[0] == 2

    changed_raw = _jpeg_bytes((1400, 700))
    resume_calls: list[str] = []
    with pytest.raises(RuntimeError, match="could not reproduce retained input"):
        run_benchmark(
            source_db=source,
            output_db=output,
            report_path=report,
            limit=2,
            batch_size=2,
            codex_bin=Path("fake-codex.exe"),
            model="test-model",
            cli_version="test-cli",
            resume=True,
            fetcher=lambda url: FetchPayload(changed_raw, 200, "image/jpeg", url),
            executor=_fake_executor(calls=resume_calls),
        )

    with sqlite3.connect(partial) as conn:
        assert conn.execute(
            "SELECT response_sha256 FROM fetch_results ORDER BY asset_key"
        ).fetchall() == before_fetch
        assert conn.execute(
            "SELECT COUNT(*) FROM vision_results WHERE lane='long1024'"
        ).fetchone()[0] == 2
    assert resume_calls == []
