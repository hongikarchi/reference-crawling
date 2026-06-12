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
        "display_cover_url": "https://example.test/1.jpg",
        "is_publishable": True,
        "publishability_reasons": [],
        "embedding": [0.01] * 384,
        "source_urls": {
            "divisare": ["https://divisare.com/projects/1-house-k"],
            "archello": ["https://archello.com/project/house-k"],
        },
    }
    row.update(overrides)
    return row


def test_map_row_preserves_v2_identity_and_json_fields():
    mapped = validator.map_row(_sample_row())

    assert mapped["canonical_bld_id"] == "bld_000001"
    assert mapped["name"] == "House K"
    assert mapped["architect_canonical_ids"] == ["arch_000001"]
    assert mapped["source_refs"] == {"divisare": ["1"], "archello": ["2"]}
    assert mapped["source_urls"] == {
        "divisare": ["https://divisare.com/projects/1-house-k"],
        "archello": ["https://archello.com/project/house-k"],
    }
    assert mapped["covers_by_type"]["exterior"] == "https://example.test/1.jpg"
    assert mapped["display_cover_url"] == "https://example.test/1.jpg"
    assert mapped["is_publishable"] is True
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
            _sample_row(
                canonical_bld_id="bld_000002",
                location_country="Thailand",
                # distinct cover so this test isolates duplicate-NAME behavior
                # and does not trip the reused-cover check
                display_cover_url="https://example.test/2.jpg",
                cover_image_url_default="https://example.test/2.jpg",
            ),
        ]
    )

    assert report["status"] == "PASS"
    assert report["warnings"]["duplicate_name_groups"] == 1
    assert report["duplicate_name_samples"][0]["name"] == "house k"


def test_validate_rows_requires_source_urls():
    report = validator.validate_rows([_sample_row(source_urls={})])

    assert report["status"] == "FAIL"
    assert report["failures"]["bad_source_urls"] == 1


def test_validate_rows_blocks_publishable_rows_without_display_image():
    report = validator.validate_rows(
        [
            _sample_row(
                all_images=[],
                display_cover_url=None,
                cover_image_url_default=None,
                is_publishable=True,
            )
        ]
    )

    assert report["status"] == "FAIL"
    assert report["failures"]["publishable_missing_image"] == 1


def test_validate_rows_allows_nonpublishable_rows_but_counts_them():
    report = validator.validate_rows(
        [
            _sample_row(
                all_images=[],
                display_cover_url=None,
                cover_image_url_default=None,
                is_publishable=False,
                publishability_reasons=["missing_all_images", "missing_display_cover_url"],
            )
        ]
    )

    assert report["status"] == "PASS"
    assert report["publishable_rows"] == 0
    assert report["nonpublishable_rows"] == 1
    assert report["warnings"]["nonpublishable_rows"] == 1
