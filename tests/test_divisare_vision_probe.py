from __future__ import annotations

import copy
import hashlib
import io
import json
import random
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

import canonical.divisare_vision_probe as probe
from canonical.divisare_image_smoke import FetchFailure, FetchPayload, fixed_derivative_url
from canonical.divisare_vision_gold import (
    CANDIDATE_MANIFEST_VERSION,
    IDENTITY_PROFILE,
    PHASH_VERSION,
    PIXEL_HASH_VERSION,
    SOURCE_PROFILE,
    manifest_sha256,
)


def _image_bytes(seed: int, *, size: tuple[int, int] = (96, 64), alpha: bool = False) -> bytes:
    rng = random.Random(seed)
    mode = "RGBA" if alpha else "RGB"
    channels = 4 if alpha else 3
    values = bytearray(rng.randrange(256) for _ in range(size[0] * size[1] * channels))
    if alpha:
        for offset in range(3, len(values), 4):
            values[offset] = rng.randrange(64, 256)
    image = Image.frombytes(mode, size, bytes(values))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _manifest(count: int = 5) -> dict:
    candidates = []
    for rank in range(1, count + 1):
        source_url = "https://images.divisare.com/image/upload/v1/probe-%04d.png" % rank
        candidates.append(
            {
                "candidate_id": "candidate-%04d" % rank,
                "candidate_rank": rank,
                "class_rank": rank,
                "discovery_class": "exterior",
                "discovery_score": 100,
                "generation_group": "modern",
                "asset_key": "divisare|probe-%04d" % rank,
                "article_id": rank,
                "building_id": "building-%04d" % rank,
                "source_url": source_url,
                "request_url": fixed_derivative_url(source_url, SOURCE_PROFILE),
                "review_url": source_url,
                "url_generation": "cloudinary_public_id",
                "original_filename": None,
                "role": "gallery",
                "position": rank,
                "article_kind": "photo_feature",
                "kind_status": "confirmed",
                "country": "Test",
                "weak_hints": [],
                "country_cap_fallback": False,
                "stable_order": "%064x" % rank,
            }
        )
    payload = {
        "manifest_version": CANDIDATE_MANIFEST_VERSION,
        "source_db_sha256": "a" * 64,
        "contract": {
            "manifest_version": CANDIDATE_MANIFEST_VERSION,
            "source_db_sha256": "a" * 64,
            "source_profile": SOURCE_PROFILE,
            "identity_profile": IDENTITY_PROFILE,
            "pixel_hash_version": PIXEL_HASH_VERSION,
            "phash_version": PHASH_VERSION,
        },
        "candidates": candidates,
    }
    payload["manifest_sha256"] = manifest_sha256(payload)
    return payload


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_decode_normalizes_orientation_alpha_and_long_edge() -> None:
    rgba = Image.new("RGBA", (1024, 512), (255, 0, 0, 64))
    raw = io.BytesIO()
    rgba.save(raw, format="PNG")
    evidence = probe.decode_normalize_hash(raw.getvalue())
    assert (evidence.original_width, evidence.original_height) == (1024, 512)
    assert (evidence.normalized_width, evidence.normalized_height) == (512, 256)
    assert evidence.alpha_composited is True
    assert len(evidence.pixel_sha256) == 64
    assert len(evidence.phash_256) == 64

    image = Image.new("RGB", (100, 200), "navy")
    exif = Image.Exif()
    exif[274] = 6
    rotated = io.BytesIO()
    image.save(rotated, format="JPEG", exif=exif)
    oriented = probe.decode_normalize_hash(rotated.getvalue())
    assert oriented.orientation_applied is True
    assert (oriented.original_width, oriented.original_height) == (100, 200)
    assert (oriented.oriented_width, oriented.oriented_height) == (200, 100)


def test_decode_rejects_unbounded_dimensions() -> None:
    image = Image.new("RGB", (2049, 8), "white")
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    with pytest.raises(FetchFailure, match="exceed max2048") as caught:
        probe.decode_normalize_hash(raw.getvalue())
    assert caught.value.kind == "transform_not_applied"


def test_duplicate_evidence_covers_threshold_bands() -> None:
    base = {
        "candidate_rank": 1,
        "candidate_id": "candidate-0001",
        "pixel_sha256": "1" * 64,
        "phash_256": "0" * 64,
    }
    near = {
        "candidate_rank": 2,
        "candidate_id": "candidate-0002",
        "pixel_sha256": "2" * 64,
        "phash_256": ("%064x" % ((1 << 8) - 1)),
    }
    exact = {
        "candidate_rank": 3,
        "candidate_id": "candidate-0003",
        "pixel_sha256": "1" * 64,
        "phash_256": "0" * 64,
    }
    groups, duplicate_pairs, _audit = probe.build_duplicate_evidence([exact, near, base])
    assert groups == [
        {
            "group_id": "exact-pixel-0001",
            "pixel_sha256": "1" * 64,
            "representative_candidate_id": "candidate-0001",
            "member_candidate_ids": ["candidate-0001", "candidate-0003"],
            "member_count": 2,
        }
    ]
    assert {(row["candidate_id_a"], row["candidate_id_b"], row["phash_distance"]) for row in duplicate_pairs} >= {
        ("candidate-0001", "candidate-0002", 8),
        ("candidate-0001", "candidate-0003", 0),
    }

    audit_right = {
        "candidate_rank": 2,
        "candidate_id": "candidate-0002",
        "pixel_sha256": "2" * 64,
        "phash_256": "%064x" % ((1 << 12) - 1),
    }
    _groups, duplicates, audit = probe.build_duplicate_evidence([base, audit_right])
    assert duplicates == []
    assert audit[0]["phash_distance"] == 12


def test_probe_enriches_manifest_accounts_failures_and_never_stores_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "validate_candidate_manifest", lambda payload: None)
    manifest_path = tmp_path / "candidates.json"
    output_path = tmp_path / "probed.json"
    staging_path = tmp_path / "probe.sqlite"
    source = _manifest()
    _write_manifest(manifest_path, source)

    duplicate = _image_bytes(1, alpha=True)
    raw_by_rank = {1: duplicate, 2: duplicate, 4: _image_bytes(4), 5: _image_bytes(5)}
    seen_contracts: list[tuple[tuple[float, float], int]] = []

    def fetcher(url: str, *, timeout, max_bytes: int) -> FetchPayload:
        seen_contracts.append((timeout, max_bytes))
        rank = int(url.rsplit("probe-", 1)[1].split(".", 1)[0])
        if rank == 3:
            raise FetchFailure("http_404", "not found", http_status=404)
        raw = raw_by_rank[rank]
        return FetchPayload(raw, 200, "image/png", url)

    config = probe.ProbeConfig(
        workers=2,
        max_bytes=1024 * 1024,
        connect_timeout=1.5,
        read_timeout=2.5,
        max_attempts=1,
    )
    result = probe.run_candidate_probe(
        manifest_path=manifest_path,
        output_path=output_path,
        staging_path=staging_path,
        config=config,
        fetcher=fetcher,
        sleep=lambda _seconds: None,
    )
    assert result["success_count"] == 4
    assert result["failure_count"] == 1
    assert output_path.exists()
    assert not staging_path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert all(value == ((1.5, 2.5), 1024 * 1024) for value in seen_contracts)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["manifest_version"] == CANDIDATE_MANIFEST_VERSION
    assert payload["manifest_sha256"] == manifest_sha256(payload)
    assert payload["probe_contract"]["input_manifest_sha256"] == source["manifest_sha256"]
    assert payload["probe_contract"]["images_persisted"] is False
    assert payload["probe_contract"]["runtime_versions"] == probe.probe_runtime_versions()
    assert payload["candidates"][0]["content_sha256"] == hashlib.sha256(duplicate).hexdigest()
    assert payload["candidates"][0]["is_exact_pixel_duplicate"] is True
    assert payload["candidates"][0]["auto_exclude_exact_duplicate"] is False
    assert payload["candidates"][1]["duplicate_of"] == "candidate-0001"
    assert payload["candidates"][1]["auto_exclude_exact_duplicate"] is True
    assert payload["candidates"][2]["probe_status"] == "failed"
    assert payload["candidates"][2]["probe_error_kind"] == "http_404"
    assert payload["exact_pixel_duplicate_groups"][0]["member_candidate_ids"] == [
        "candidate-0001",
        "candidate-0002",
    ]
    assert all("raw" not in row and "image_bytes" not in row for row in payload["candidates"])

    tampered = copy.deepcopy(payload)
    tampered["phash_duplicate_pairs_le_8"] = []
    tampered["manifest_sha256"] = manifest_sha256(tampered)
    with pytest.raises(ValueError, match="pHash <=8 pairs"):
        probe.validate_enriched_manifest(
            tampered,
            input_manifest=source,
            input_manifest_file_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )

    runtime_tampered = copy.deepcopy(payload)
    runtime_tampered["probe_contract"]["runtime_versions"]["python"] += ".tampered"
    runtime_tampered["manifest_sha256"] = manifest_sha256(runtime_tampered)
    with pytest.raises(ValueError, match="logical SHA"):
        probe.validate_enriched_manifest(
            runtime_tampered,
            input_manifest=source,
            input_manifest_file_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )

    second = probe.run_candidate_probe(
        manifest_path=manifest_path,
        output_path=tmp_path / "probed-second.json",
        staging_path=tmp_path / "probe-second.sqlite",
        config=config,
        fetcher=fetcher,
        sleep=lambda _seconds: None,
    )
    assert second["logical_sha256"] == result["logical_sha256"]
    with pytest.raises(FileExistsError, match="immutable output"):
        probe.run_candidate_probe(
            manifest_path=manifest_path,
            output_path=output_path,
            staging_path=staging_path,
            config=config,
            fetcher=fetcher,
        )


def test_resume_processes_only_pending_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "validate_candidate_manifest", lambda payload: None)
    manifest_path = tmp_path / "candidates.json"
    output_path = tmp_path / "probed.json"
    staging_path = tmp_path / "probe.sqlite"
    manifest = _manifest(3)
    _write_manifest(manifest_path, manifest)
    loaded, file_sha = probe.load_probe_manifest(manifest_path)
    candidates = probe._validate_probe_manifest(loaded)
    config = probe.ProbeConfig(workers=1, max_attempts=1)
    probe._create_staging(
        staging_path,
        manifest_path=manifest_path.resolve(),
        manifest_file_sha=file_sha,
        manifest=loaded,
        candidates=candidates,
        config=config,
    )

    first_raw = _image_bytes(1)
    first = probe._probe_candidate(
        candidates[0],
        config=config,
        fetcher=lambda url, **_kwargs: FetchPayload(first_raw, 200, "image/png", url),
        sleep=lambda _seconds: None,
    )
    conn = sqlite3.connect(staging_path)
    try:
        schema = "\n".join(
            str(row[0])
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        )
        assert " BLOB" not in schema.upper()
        assert "image_bytes" not in schema and " raw" not in schema
        probe._write_probe_result(conn, first)
    finally:
        conn.close()

    fetched: list[str] = []

    def fetcher(url: str, **_kwargs) -> FetchPayload:
        fetched.append(url)
        rank = int(url.rsplit("probe-", 1)[1].split(".", 1)[0])
        return FetchPayload(_image_bytes(rank), 200, "image/png", url)

    probe.run_candidate_probe(
        manifest_path=manifest_path,
        output_path=output_path,
        staging_path=staging_path,
        config=config,
        resume=True,
        fetcher=fetcher,
        sleep=lambda _seconds: None,
    )
    assert len(fetched) == 2
    assert all("probe-0001" not in url for url in fetched)
    assert not staging_path.exists()


def test_stop_after_retains_full_staging_then_resumes_to_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "validate_candidate_manifest", lambda payload: None)
    manifest_path = tmp_path / "candidates.json"
    output_path = tmp_path / "probed.json"
    staging_path = tmp_path / "probe.sqlite"
    manifest = _manifest(20)
    for index, cell in enumerate(probe.PROBE_CELL_ORDER):
        for candidate in manifest["candidates"][index * 2 : index * 2 + 2]:
            candidate["discovery_class"], candidate["generation_group"] = cell
            candidate["url_generation"] = (
                "cloudinary_public_id" if cell[1] == "modern" else "legacy_url"
            )
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    _write_manifest(manifest_path, manifest)
    config = probe.ProbeConfig(workers=2, max_attempts=1)
    first_fetched: list[int] = []

    def first_fetcher(url: str, **_kwargs) -> FetchPayload:
        rank = int(url.rsplit("probe-", 1)[1].split(".", 1)[0])
        first_fetched.append(rank)
        return FetchPayload(_image_bytes(rank), 200, "image/png", url)

    progress = probe.run_candidate_probe(
        manifest_path=manifest_path,
        output_path=output_path,
        staging_path=staging_path,
        config=config,
        stop_after=10,
        fetcher=first_fetcher,
        sleep=lambda _seconds: None,
    )
    expected_first_ranks = list(range(1, 20, 2))
    assert sorted(first_fetched) == expected_first_ranks
    assert progress["status"] == "running"
    assert progress["final_output_written"] is False
    assert progress["processed_this_invocation"] == 10
    assert progress["selected_candidate_ids"] == [
        "candidate-%04d" % rank for rank in expected_first_ranks
    ]
    assert [
        (row["discovery_class"], row["generation_group"])
        for row in progress["selected_cells"]
    ] == list(probe.PROBE_CELL_ORDER)
    assert progress["pending_count"] == 10
    assert staging_path.exists()
    assert not output_path.exists()

    conn = sqlite3.connect(staging_path)
    try:
        assert conn.execute("SELECT status FROM probe_run").fetchone()[0] == "running"
        states = dict(conn.execute(
            "SELECT candidate_rank,status FROM candidate_results ORDER BY candidate_rank"
        ).fetchall())
        assert [rank for rank, status in states.items() if status == "success"] == expected_first_ranks
        assert [rank for rank, status in states.items() if status == "pending"] == list(
            range(2, 21, 2)
        )
    finally:
        conn.close()

    resumed_fetched: list[int] = []

    def resumed_fetcher(url: str, **_kwargs) -> FetchPayload:
        rank = int(url.rsplit("probe-", 1)[1].split(".", 1)[0])
        resumed_fetched.append(rank)
        return FetchPayload(_image_bytes(rank), 200, "image/png", url)

    completed = probe.run_candidate_probe(
        manifest_path=manifest_path,
        output_path=output_path,
        staging_path=staging_path,
        config=config,
        resume=True,
        fetcher=resumed_fetcher,
        sleep=lambda _seconds: None,
    )
    assert sorted(resumed_fetched) == list(range(2, 21, 2))
    assert completed["success_count"] == 20
    assert output_path.exists()
    assert not staging_path.exists()


def test_probe_config_and_injected_fetch_payload_are_bounded() -> None:
    for invalid in (
        probe.ProbeConfig(workers=0),
        probe.ProbeConfig(max_attempts=5),
        probe.ProbeConfig(max_bytes=1023),
        probe.ProbeConfig(connect_timeout=0),
        probe.ProbeConfig(read_timeout=121),
    ):
        with pytest.raises(ValueError):
            invalid.validate()
    with pytest.raises(ValueError, match="positive integer"):
        probe.run_candidate_probe(
            manifest_path=Path("unused"),
            output_path=Path("unused-output"),
            stop_after=0,
        )

    valid = FetchPayload(b"x", 200, "image/jpeg", "https://images.divisare.com/x")
    probe._validate_fetch_payload(valid, max_bytes=1)
    cases = (
        (FetchPayload(b"", 200, "image/jpeg", valid.final_url), "empty_response"),
        (FetchPayload(b"xx", 200, "image/jpeg", valid.final_url), "too_large"),
        (FetchPayload(b"x", 404, "image/jpeg", valid.final_url), "http_404"),
        (FetchPayload(b"x", 200, "image/jpeg", "https://example.com/x"), "redirect_host_rejected"),
    )
    for payload, kind in cases:
        with pytest.raises(FetchFailure) as caught:
            probe._validate_fetch_payload(payload, max_bytes=1)
        assert caught.value.kind == kind


def test_invalid_manifest_is_rejected_before_fetch(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["manifest_version"] = "wrong"
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    path = tmp_path / "bad.json"
    _write_manifest(path, manifest)
    called = False

    def fetcher(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError

    with pytest.raises(ValueError, match="version mismatch"):
        probe.run_candidate_probe(
            manifest_path=path,
            output_path=tmp_path / "out.json",
            config=probe.ProbeConfig(workers=1),
            fetcher=fetcher,
        )
    assert called is False
