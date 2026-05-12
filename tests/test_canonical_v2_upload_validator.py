import json

from tools import canonical_v2_upload_validator as validator


def _sample_row(**overrides):
    row = {
        "canonical_bld_id": "bld_000001",
        "name": "House K",
        "names_alts": ["House K"],
        "architect_canonical_ids": ["arch_000001"],
        "architect_names": ["Studio Test"],
        "architects_text": "Studio Test",
        "location_city": "Seoul",
        "location_country": "South Korea",
        "project_year": 2024,
        "n_sources": 2,
        "source_refs": {"divisare": ["1"], "archello": ["2"]},
        "identity_source": "divisare",
        "confidence_tier": "T2",
        "program": "Housing",
        "style": "Contemporary",
        "color_tone": "Neutral",
        "atmosphere": "Serene",
        "material_visual": ["concrete", "glass"],
        "visual_description": "A compact concrete house with large windows.",
        "all_images": [{"url": "https://example.test/1.jpg", "source": "divisare"}],
        "best_image_per_cluster": {"0": {"url": "https://example.test/1.jpg"}},
        "covers_by_type": {
            "exterior": "https://example.test/1.jpg",
            "interior": None,
            "drawing": None,
            "aerial": None,
            "detail": None,
        },
        "image_derived": {"style": "Contemporary"},
        "cover_image_url_default": "https://example.test/1.jpg",
        "embedding": [0.01] * 384,
    }
    row.update(overrides)
    return row


def test_map_row_preserves_v2_identity_and_json_fields():
    mapped = validator.map_row(_sample_row())

    assert mapped["canonical_bld_id"] == "bld_000001"
    assert mapped["name"] == "House K"
    assert mapped["architect_canonical_ids"] == ["arch_000001"]
    assert mapped["source_refs"] == {"divisare": ["1"], "archello": ["2"]}
    assert mapped["covers_by_type"]["exterior"] == "https://example.test/1.jpg"
    assert mapped["embedding"] == [0.01] * 384
    json.dumps(mapped)


def test_validate_rows_rejects_duplicate_primary_key_and_bad_embedding():
    report = validator.validate_rows(
        [
            _sample_row(),
            _sample_row(name="Second copy"),
            _sample_row(canonical_bld_id="bld_000002", embedding=[0.01] * 383),
        ]
    )

    assert report["status"] == "FAIL"
    assert report["total_rows"] == 3
    assert report["failures"]["duplicate_pk"] == 1
    assert report["failures"]["bad_embedding"] == 1


def test_validate_rows_tracks_duplicate_names_without_failing():
    report = validator.validate_rows(
        [
            _sample_row(canonical_bld_id="bld_000001", location_country="Sweden"),
            _sample_row(canonical_bld_id="bld_000002", location_country="Thailand"),
        ]
    )

    assert report["status"] == "PASS"
    assert report["warnings"]["duplicate_name_groups"] == 1
    assert report["duplicate_name_samples"][0]["name"] == "house k"
