from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

import canonical.divisare_vision_axes_holdout as holdout


def _evidence(
    rank: int,
    *,
    proxy_class: str,
    generation: str,
    role: str,
    article_id: int | None = None,
    building_id: str | None = None,
    subtype: str | None = None,
) -> holdout.HoldoutEvidence:
    asset_key = "divisare|holdout-%04d|v1" % rank
    url_generation = (
        "cloudinary_public_id" if generation == "modern" else "legacy_url"
    )
    source_url = (
        "https://images.divisare.com/image/upload/v1/holdout-%04d.jpg" % rank
    )
    return holdout.HoldoutEvidence(
        asset_key=asset_key,
        article_id=article_id if article_id is not None else rank,
        building_id=building_id or "building-%04d" % rank,
        source_url=source_url,
        url_generation=url_generation,
        generation_group=generation,
        original_filename=None,
        role=role,
        position=0 if role == "cover" else rank,
        article_kind="photo_feature",
        kind_status="candidate",
        country="Country-%d" % rank,
        proxy_class=proxy_class,
        proxy_subtype=subtype or proxy_class,
        proxy_score=100 - rank,
        weak_hints=("test",),
        stable_order=holdout._stable_hex(
            proxy_class, generation, role, asset_key
        ),
    )


def _exclusion(source_sha: str, file_sha: str = "e" * 64) -> holdout.ExclusionEvidence:
    rows = [
        {
            "asset_key": "excluded-asset",
            "article_id": 9001,
            "building_id": "excluded-building",
        }
    ]
    return holdout.ExclusionEvidence(
        file_sha256=file_sha,
        manifest_sha256="f" * 64,
        source_db_sha256=source_sha,
        asset_keys=frozenset({"excluded-asset"}),
        article_ids=frozenset({9001}),
        building_ids=frozenset({"excluded-building"}),
        identity_set_sha256=holdout.identity_set_sha256(rows),
    )


def test_oos_proxy_targets_scope_challenges_without_rejecting_valid_states() -> None:
    assert holdout._oos_proxy(
        filename="portrait-architect.jpg",
        article_kind="photo_feature",
        article_hints=frozenset(),
        albums=frozenset(),
    )[:2] == (96, "people_or_portrait")
    assert holdout._oos_proxy(
        filename="plain.jpg",
        article_kind="concept_editorial",
        article_hints=frozenset(),
        albums=frozenset(),
    )[:2] == (76, "object_or_artwork")
    assert holdout._oos_proxy(
        filename="rendering-construction.jpg",
        article_kind="photo_feature",
        article_hints=frozenset(),
        albums=frozenset(),
    ) is None


def test_selector_fills_cells_without_reusing_article_or_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = {
        ("out_of_scope", "legacy", "gallery"): 2,
        ("drawing", "modern", "cover"): 1,
    }
    monkeypatch.setattr(holdout, "CELL_TARGETS", targets)
    oos_a = _evidence(
        1,
        proxy_class="out_of_scope",
        generation="legacy",
        role="gallery",
        subtype="people_or_portrait",
    )
    oos_b = _evidence(
        2,
        proxy_class="out_of_scope",
        generation="legacy",
        role="gallery",
        subtype="people_or_event",
    )
    drawing_overlap = _evidence(
        3,
        proxy_class="drawing",
        generation="modern",
        role="cover",
        article_id=oos_a.article_id,
    )
    drawing_unique = _evidence(
        4,
        proxy_class="drawing",
        generation="modern",
        role="cover",
    )
    reservoirs = {
        ("out_of_scope", "legacy", "gallery"): [oos_a, oos_b],
        ("drawing", "modern", "cover"): [drawing_overlap, drawing_unique],
    }
    selected = holdout._select_candidates(reservoirs)
    assert [item.asset_key for item in selected] == [
        oos_a.asset_key,
        oos_b.asset_key,
        drawing_unique.asset_key,
    ]
    assert len({item.article_id for item in selected}) == 3
    assert len({item.building_id for item in selected}) == 3


def test_payload_uses_blind_order_and_validates_exclusion_disjointness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = {
        ("out_of_scope", "legacy", "gallery"): 1,
        ("drawing", "modern", "cover"): 1,
    }
    monkeypatch.setattr(holdout, "CELL_TARGETS", targets)
    source_sha = "a" * 64
    exclusion = _exclusion(source_sha)
    selected = [
        _evidence(
            1,
            proxy_class="out_of_scope",
            generation="legacy",
            role="gallery",
            subtype="people_or_event",
        ),
        _evidence(
            2,
            proxy_class="drawing",
            generation="modern",
            role="cover",
        ),
    ]
    payload = holdout._payload_from_evidence(
        source_db=Path("source.db"),
        source_sha=source_sha,
        exclusion_path=Path("excluded.json"),
        exclusion=exclusion,
        selected=selected,
    )
    rows = holdout.validate_candidate_manifest(payload, exclusion=exclusion)
    assert [row["review_id"] for row in rows] == sorted(
        (row["review_id"] for row in rows), key=holdout._review_order_key
    )
    assert all(row["review_id"].startswith("axis-holdout-") for row in rows)
    assert payload["contract"]["selection_uses_model_output"] is False
    assert payload["contract"]["network_io"] is False

    changed = copy.deepcopy(payload)
    changed["candidates"][0]["asset_key"] = "excluded-asset"
    changed["candidates"][0]["review_id"] = holdout.opaque_review_id(
        "excluded-asset"
    )
    row = changed["candidates"][0]
    row["stable_order"] = holdout._stable_hex(
        row["proxy_class"], row["generation_group"], row["role"], row["asset_key"]
    )
    changed["selection_metrics"]["selected_identity_set_sha256"] = (
        holdout.identity_set_sha256(changed["candidates"])
    )
    changed["candidates"].sort(
        key=lambda value: holdout._review_order_key(value["review_id"])
    )
    for rank, candidate in enumerate(changed["candidates"], 1):
        candidate["candidate_id"] = "holdout-candidate-%04d" % rank
        candidate["candidate_rank"] = rank
    cell_counts: dict[tuple[str, str, str], int] = {}
    for candidate in changed["candidates"]:
        cell = (
            candidate["proxy_class"],
            candidate["generation_group"],
            candidate["role"],
        )
        cell_counts[cell] = cell_counts.get(cell, 0) + 1
        candidate["cell_rank"] = cell_counts[cell]
    changed["manifest_sha256"] = holdout.manifest_sha256(changed)
    with pytest.raises(ValueError, match="overlap excluded identities"):
        holdout.validate_candidate_manifest(changed, exclusion=exclusion)


def test_public_builder_binds_source_and_exclusion_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = {("drawing", "modern", "cover"): 1}
    monkeypatch.setattr(holdout, "CELL_TARGETS", targets)
    source = tmp_path / "source.db"
    source.write_bytes(b"immutable source")
    exclusion_path = tmp_path / "excluded.json"
    exclusion_path.write_bytes(b"{}\n")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    exclusion_file_sha = hashlib.sha256(exclusion_path.read_bytes()).hexdigest()
    exclusion = _exclusion(source_sha, exclusion_file_sha)
    item = _evidence(
        7,
        proxy_class="drawing",
        generation="modern",
        role="cover",
    )
    monkeypatch.setattr(
        holdout,
        "load_exclusion_manifest",
        lambda _path: ({}, exclusion),
    )
    monkeypatch.setattr(
        holdout,
        "_candidate_reservoirs",
        lambda _source, _exclusion: {
            ("drawing", "modern", "cover"): [item]
        },
    )
    payload = holdout.candidate_manifest_payload(source, exclusion_path)
    assert payload["source_db_sha256"] == source_sha
    assert payload["provenance"]["exclusion_manifest_file_sha256"] == (
        exclusion_file_sha
    )


def test_writer_is_no_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "holdout.json"
    payload = {"manifest_version": holdout.MANIFEST_VERSION, "manifest_sha256": "a" * 64}
    monkeypatch.setattr(
        holdout, "candidate_manifest_payload", lambda _source, _excluded: payload
    )
    holdout.write_candidate_manifest(
        tmp_path / "source.db", tmp_path / "excluded.json", output
    )
    before = output.read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        holdout.write_candidate_manifest(
            tmp_path / "source.db", tmp_path / "excluded.json", output
        )
    assert output.read_bytes() == before
