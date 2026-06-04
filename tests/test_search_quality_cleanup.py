import json

from tools import search_quality_cleanup as sq


def _row(**overrides):
    row = {
        "canonical_bld_id": "bld_000001",
        "name": "Courtyard House",
        "architect_names": ["Studio Test"],
        "architects_text": "Studio Test",
        "program": "Housing",
        "style": "Contemporary",
        "color_tone": "Warm",
        "atmosphere": "Serene",
        "material_visual": ["glass", "wood", "water", "terraces", "green roof", "reinforced concrete"],
        "visual_description": "A warm courtyard house uses concrete, glass, and timber around a quiet garden.",
        "typology_primary": "House",
        "typology_tags": ["House", "Housing"],
        "architectural_elements": ["Courtyard"],
        "source_categories": {"divisare": ["houses", "renovation"]},
        "is_publishable": True,
        "publishability_reasons": [],
        "all_images": [{"url": "https://img.test/1.jpg"}],
        "display_cover_url": "https://img.test/1.jpg",
        "cover_image_url_default": "https://img.test/1.jpg",
        "embedding": [0.01] * 384,
    }
    row.update(overrides)
    return row


def test_clean_row_strips_noise_moves_elements_and_collapses_materials():
    cleaned, change = sq.clean_building_row(_row())

    assert cleaned["material_visual"] == ["glass", "timber", "concrete"]
    assert cleaned["architectural_elements"] == ["Courtyard", "Roof", "Terrace"]
    assert change["removed_noise"] == ["water"]
    assert change["moved_to_elements"] == {"green roof": "Roof", "terraces": "Terrace"}
    assert change["material_mapped"] == {"wood": "timber", "reinforced concrete": "concrete"}


def test_search_keywords_include_structured_fields_and_description_terms():
    keywords = sq.build_search_keywords(_row())

    for expected in {
        "courtyard",
        "house",
        "studio",
        "test",
        "housing",
        "contemporary",
        "serene",
        "glass",
        "timber",
        "concrete",
        "garden",
        "renovation",
    }:
        assert expected in keywords


def test_second_pass_maps_common_unmapped_materials_and_extra_noise():
    cleaned, change = sq.clean_building_row(
        _row(
            material_visual=[
                "brass",
                "terrazzo",
                "rock",
                "gravel",
                "roof",
                "led lighting",
                "solar panels",
                "white walls",
            ],
            architectural_elements=[],
        )
    )

    assert cleaned["material_visual"] == ["metal", "terrazzo", "stone", "paving", "plaster"]
    assert cleaned["architectural_elements"] == ["Roof"]
    assert change["removed_noise"] == ["led lighting", "solar panels"]
    assert change["moved_to_elements"] == {"roof": "Roof"}


def test_apply_cleanup_writes_artifact_sidecar_and_report(tmp_path):
    input_path = tmp_path / "c23.json"
    output_path = tmp_path / "c24.json"
    keyword_path = tmp_path / "keywords.jsonl"
    review_path = tmp_path / "review.jsonl"
    report_path = tmp_path / "report.json"
    mapping_path = tmp_path / "mapping.json"
    input_path.write_text(json.dumps({"buildings": [
        _row(),
        _row(canonical_bld_id="bld_000002", is_publishable=False),
        _row(canonical_bld_id="bld_000003", material_visual=["water"], architectural_elements=[]),
    ]}))

    report = sq.apply_cleanup(
        input_path=input_path,
        output_path=output_path,
        keyword_path=keyword_path,
        review_path=review_path,
        report_path=report_path,
        mapping_path=mapping_path,
    )

    assert report["status"] == "PASS"
    assert report["rows_total"] == 3
    assert report["publishable_rows"] == 2
    assert report["material_noise_rows_after"] == 0
    assert report["material_unmapped_review_rows"] == 1
    assert report["search_keywords_publishable_coverage_pct"] == 100.0
    assert report["controlled_oov_counts"] == {}
    assert report["facet_distributions"]["program"]["distinct"] == 1
    assert report["search_keyword_stats"]["p50"] > 0
    assert report["visual_description_word_stats"]["p50"] > 0
    changed = json.loads(output_path.read_text())["buildings"][0]
    assert changed["material_visual"] == ["glass", "timber", "concrete"]
    keyword_rows = [json.loads(line) for line in keyword_path.read_text().splitlines()]
    assert keyword_rows[0] == {"canonical_bld_id": "bld_000001", "search_keywords": sq.build_search_keywords(changed)}
    review_rows = [json.loads(line) for line in review_path.read_text().splitlines()]
    assert review_rows[0]["canonical_bld_id"] == "bld_000003"
    assert review_rows[0]["raw_material_visual"] == ["water"]
    changed_rows = json.loads(output_path.read_text())["buildings"]
    assert changed_rows[2]["material_visual"] == ["unspecified"]
    mapping = json.loads(mapping_path.read_text())
    assert mapping["controlled_materials"]
    assert mapping["noise_terms"]["water"]["category"] == "landscape_context"
