import json

from canonical import match_buildings_sequential as matcher
from canonical import match_phash_check
from canonical.registry import BuildingRegistry


def _write_cache(tmp_path, monkeypatch, data):
    cache_path = tmp_path / "phash_cache.json"
    cache_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(match_phash_check, "PHASH_CACHE_PATH", cache_path)
    match_phash_check._load_cache.cache_clear()


def _seed_registry_and_pool(tmp_path):
    registry = BuildingRegistry(path=str(tmp_path / "id_registry_buildings.json"))
    cid, _ = registry.match_or_create(
        names={"Shared Civic Library"},
        source_refs={"divisare": ["d1"]},
    )
    pool = matcher.BuildingPool()
    pool.add(
        {
            "id": "d1",
            "name": "Shared Civic Library",
            "name_core": "shared civic library",
            "canonical_arch_ids": ["arch_001"],
            "country": "Spain",
            "city": "Madrid",
            "year": 2020,
            "typology": "library",
            "cover_image_url": "https://fixture/divisare.jpg",
            "source": "divisare",
        },
        cid,
    )
    return registry, pool, cid


def _incoming_item(source_id):
    return {
        "id": source_id,
        "name": "Shared Civic Library",
        "name_core": "shared civic library",
        "canonical_arch_ids": ["arch_001"],
        "country": "Spain",
        "city": "Madrid",
        "year": 2020,
        "typology": "library",
        "cover_image_url": "https://fixture/architizer.jpg",
        "source": "architizer",
    }


def test_auto_accept_merge_allowed_when_phash_overlaps(tmp_path, monkeypatch):
    monkeypatch.setattr(matcher, "PHASH_BLOCKS_OUTPUT", str(tmp_path / "blocks.json"))
    matcher._reset_phash_blocks()
    _write_cache(
        tmp_path,
        monkeypatch,
        {
            "divisare:d1": ["0000000000000000", "1111111111111111"],
            "architizer:a1": ["000000000000000f", "ffffffffffffffff"],
        },
    )
    registry, pool, cid = _seed_registry_and_pool(tmp_path)
    tiebreak_queue = []

    counts = matcher.phase_match_against_pool(
        registry,
        pool,
        [_incoming_item("a1")],
        label="architizer",
        allow_new=True,
        tiebreak_queue=tiebreak_queue,
    )

    assert counts["auto_accept"] == 1
    assert "phash_block" not in counts
    assert registry.data[cid]["source_refs"]["architizer"] == ["a1"]
    assert tiebreak_queue == []
    assert matcher.PHASH_BLOCK_LOG == []


def test_auto_accept_merge_allowed_when_zero_phash_but_text_agrees(tmp_path, monkeypatch):
    blocks_path = tmp_path / "blocks.json"
    monkeypatch.setattr(matcher, "PHASH_BLOCKS_OUTPUT", str(blocks_path))
    matcher._reset_phash_blocks()
    _write_cache(
        tmp_path,
        monkeypatch,
        {
            "divisare:d1": ["0000000000000000", "00000000000000ff"],
            "architizer:a1": ["ffffffffffffffff", "ffffffffffffff00"],
        },
    )
    registry, pool, cid = _seed_registry_and_pool(tmp_path)
    tiebreak_queue = []

    counts = matcher.phase_match_against_pool(
        registry,
        pool,
        [_incoming_item("a1")],
        label="architizer",
        allow_new=True,
        tiebreak_queue=tiebreak_queue,
    )

    assert counts["auto_accept"] == 1
    assert "phash_block" not in counts
    assert registry.data[cid]["source_refs"]["architizer"] == ["a1"]
    assert tiebreak_queue == []

    blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
    assert blocks == [
        {
            "phase": "architizer",
            "cluster_id_a": cid,
            "cluster_id_b": "a1",
            "src_a": "divisare",
            "src_b": "architizer",
            "verdict": "tiebreaker_pass",
            "overlap": 0,
            "a_n": 2,
            "b_n": 2,
        }
    ]
