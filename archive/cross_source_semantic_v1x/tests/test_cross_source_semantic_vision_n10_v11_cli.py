from __future__ import annotations

from pathlib import Path

import pytest

from tools.run_cross_source_semantic_vision_n10_v11 import (
    _assert_literal_contract,
    _resolve_verified_codex,
    _review_cache_preflight,
)
import tools.run_cross_source_semantic_vision_n10_v11 as run_cli


def test_literal_prompt_runtime_contract_is_exact() -> None:
    _assert_literal_contract()


def test_fresh_run_rejects_existing_review_cache(tmp_path: Path) -> None:
    cache = tmp_path / "review-cache"
    cache.mkdir()

    with pytest.raises(FileExistsError, match="refuses existing review cache"):
        _review_cache_preflight(cache, resume=False)


def test_resume_requires_and_accepts_existing_review_cache(tmp_path: Path) -> None:
    cache = tmp_path / "review-cache"
    with pytest.raises(FileNotFoundError, match="requires its existing review cache"):
        _review_cache_preflight(cache, resume=True)

    cache.mkdir()
    _review_cache_preflight(cache, resume=True)


def test_run_cli_wires_mandatory_preflight_and_terminal_validator() -> None:
    source = Path("tools/run_cross_source_semantic_vision_n10_v11.py").read_text(
        encoding="utf-8"
    )
    assert "preflight_validator=mandatory_preflight" in source
    assert "validate_semantic_n10_v11(" in source
    assert "review_cache_dir=args.review_cache_dir" in source
    assert "cli_version=actual_cli_version" in source


def test_codex_discovery_resolves_and_verifies_actual_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"test executable placeholder")
    monkeypatch.setattr(run_cli, "discover_codex_bin", lambda: binary)
    monkeypatch.setattr(run_cli, "codex_version", lambda path: "codex-cli 0.146.0")

    path, version = _resolve_verified_codex(None)

    assert path == binary.resolve()
    assert version == "codex-cli 0.146.0"


def test_explicit_codex_is_verified_and_old_version_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "explicit-codex.exe"
    binary.write_bytes(b"test executable placeholder")
    monkeypatch.setattr(
        run_cli,
        "discover_codex_bin",
        lambda: (_ for _ in ()).throw(AssertionError("explicit path must bypass discovery")),
    )
    monkeypatch.setattr(run_cli, "codex_version", lambda path: "codex-cli 0.145.9")

    with pytest.raises(RuntimeError, match="too old"):
        _resolve_verified_codex(binary)


def test_explicit_codex_must_exist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        _resolve_verified_codex(tmp_path / "missing-codex.exe")
