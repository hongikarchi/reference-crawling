import json

from canonical import match_phash_check


def _write_cache(tmp_path, monkeypatch, data):
    cache_path = tmp_path / "phash_cache.json"
    cache_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(match_phash_check, "PHASH_CACHE_PATH", cache_path)
    match_phash_check._load_cache.cache_clear()
    return cache_path


def test_blocks_bld_026977_zero_overlap_with_enough_images(tmp_path, monkeypatch):
    _write_cache(
        tmp_path,
        monkeypatch,
        {
            "metalocus:bld_026977_a": ["0000000000000000", "00000000000000ff"],
            "divisare:bld_026977_b": ["ffffffffffffffff", "ffffffffffffff00"],
        },
    )

    result = match_phash_check.has_phash_overlap(
        ["bld_026977_a"],
        ["bld_026977_b"],
        "metalocus",
        "divisare",
    )

    assert result == {"verdict": "BLOCK", "overlap": 0, "a_n": 2, "b_n": 2}


def test_passes_genuine_match_with_phash_within_threshold(tmp_path, monkeypatch):
    _write_cache(
        tmp_path,
        monkeypatch,
        {
            "metalocus:project_a": ["0000000000000000", "1111111111111111"],
            "divisare:project_b": ["000000000000000f", "ffffffffffffffff"],
        },
    )

    result = match_phash_check.has_phash_overlap(
        ["project_a"],
        ["project_b"],
        "metalocus",
        "divisare",
    )

    assert result["verdict"] == "PASS"
    assert result["overlap"] >= 1
    assert result["a_n"] == 2
    assert result["b_n"] == 2


def test_passes_when_one_side_has_only_one_image(tmp_path, monkeypatch):
    _write_cache(
        tmp_path,
        monkeypatch,
        {
            "metalocus:project_a": ["0000000000000000"],
            "divisare:project_b": ["ffffffffffffffff", "ffffffffffffff00"],
        },
    )

    result = match_phash_check.has_phash_overlap(
        ["project_a"],
        ["project_b"],
        "metalocus",
        "divisare",
    )

    assert result == {"verdict": "PASS", "overlap": 0, "a_n": 1, "b_n": 2}


def test_passes_when_cache_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(match_phash_check, "PHASH_CACHE_PATH", tmp_path / "missing.json")
    match_phash_check._load_cache.cache_clear()

    result = match_phash_check.has_phash_overlap(
        ["project_a"],
        ["project_b"],
        "metalocus",
        "divisare",
    )

    assert result == {"verdict": "PASS", "reason": "no cache"}

