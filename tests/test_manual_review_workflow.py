import json

import pytest

from tools import manual_review_workflow as workflow


def _sample_row(**overrides):
    row = {
        "canonical_bld_id": "bld_000001",
        "name": "House K",
        "location_country": "South Korea",
        "location_city": "Seoul",
        "project_year": 2024,
        "architect_names": ["Studio Test"],
        "architect_canonical_ids": ["arch_000001"],
        "source_refs": {"divisare": ["1"]},
        "source_urls": {"divisare": ["https://divisare.com/projects/1-house-k"]},
        "display_cover_url": "https://img.test/current.jpg",
        "cover_image_url_default": "https://img.test/current.jpg",
        "all_images": [
            {"url": "https://img.test/current.jpg", "kind": "cover"},
            {"url": "https://img.test/better.jpg", "kind": "gallery"},
        ],
        "is_publishable": True,
        "publishability_reasons": [],
        "embedding": [0.01] * 384,
    }
    row.update(overrides)
    return row


def test_normalize_sidecar_item_creates_dashboard_case():
    item = {
        "cid": "bld_000001",
        "name": "House K",
        "row_country": "Korea",
        "normalized": ["South Korea", "North Korea"],
    }

    case = workflow.normalize_ambiguous_item(
        source_path="data/reports/canonical_v2_c23_country_conflict_sidecar.jsonl",
        issue_code="country_conflict",
        item=item,
        index=0,
    )

    assert case["case_id"].startswith("country_conflict:")
    assert case["tab"] == "country"
    assert case["target_canonical_bld_id"] == "bld_000001"
    assert case["allowed_actions"] == workflow.DEFAULT_ACTIONS
    assert case["evidence"]["row_country"] == "Korea"


def test_validate_decision_requires_payload_for_field_update():
    snapshot = {
        "version": 1,
        "cases": [
            workflow.normalize_ambiguous_item(
                source_path="sidecar.jsonl",
                issue_code="year_conflict",
                item={"cid": "bld_000001", "name": "House K"},
                index=0,
            )
        ],
    }

    with pytest.raises(ValueError, match="payload.field"):
        workflow.validate_decision(snapshot, {"case_id": snapshot["cases"][0]["case_id"], "decision": "update_field"})

    decision = workflow.validate_decision(
        snapshot,
        {
            "case_id": snapshot["cases"][0]["case_id"],
            "decision": "update_field",
            "payload": {"field": "project_year", "value": 2025},
        },
    )

    assert decision["payload"] == {"field": "project_year", "value": 2025}
    assert decision["target_canonical_bld_id"] == "bld_000001"


def test_apply_decisions_writes_patch_and_changed_artifact(tmp_path):
    input_path = tmp_path / "c23.json"
    output_path = tmp_path / "c24.json"
    patch_path = tmp_path / "patch.json"
    decisions_path = tmp_path / "decisions.json"

    input_path.write_text(json.dumps({"buildings": [_sample_row()]}), encoding="utf-8")
    decisions_path.write_text(
        json.dumps(
            {
                "version": 1,
                "decisions": {
                    "cover:bld_000001": {
                        "case_id": "cover:bld_000001",
                        "target_canonical_bld_id": "bld_000001",
                        "decision": "set_cover_to_image",
                        "payload": {"image_url": "https://img.test/better.jpg"},
                    },
                    "country:bld_000001": {
                        "case_id": "country:bld_000001",
                        "target_canonical_bld_id": "bld_000001",
                        "decision": "update_field",
                        "payload": {"field": "location_country", "value": "South Korea"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    report = workflow.apply_decisions(
        input_path=input_path,
        decisions_path=decisions_path,
        output_path=output_path,
        patch_path=patch_path,
        write_artifact=True,
    )

    assert report["status"] == "PASS"
    assert report["changed_rows"] == 1
    changed = json.loads(output_path.read_text(encoding="utf-8"))["buildings"][0]
    assert changed["display_cover_url"] == "https://img.test/better.jpg"
    assert changed["cover_image_url_default"] == "https://img.test/better.jpg"
    assert changed["location_country"] == "South Korea"
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    assert patch["changes"][0]["canonical_bld_id"] == "bld_000001"


def test_apply_decisions_rejects_incomplete_merge(tmp_path):
    input_path = tmp_path / "c23.json"
    decisions_path = tmp_path / "decisions.json"
    input_path.write_text(json.dumps({"buildings": [_sample_row()]}), encoding="utf-8")
    decisions_path.write_text(
        json.dumps(
            {
                "version": 1,
                "decisions": {
                    "merge:bld_000001": {
                        "case_id": "merge:bld_000001",
                        "target_canonical_bld_id": "bld_000001",
                        "decision": "merge",
                        "payload": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = workflow.apply_decisions(
        input_path=input_path,
        decisions_path=decisions_path,
        output_path=tmp_path / "c24.json",
        patch_path=tmp_path / "patch.json",
        write_artifact=False,
    )

    assert report["status"] == "FAIL"
    assert report["invalid_decisions"][0]["reason"].startswith("merge requires")


def test_build_fast_snapshot_groups_material_terms_uniquely_and_orders_small_cases_first():
    snapshot = {
        "version": 1,
        "cases": [
            workflow.normalize_ambiguous_item(
                source_path="material.jsonl",
                issue_code="material_unmapped",
                item={
                    "canonical_bld_id": "bld_000001",
                    "name": "House A",
                    "unmapped_material_terms": ["Public Square", "media equipment"],
                    "raw_material_visual": ["Public Square", "media equipment"],
                },
                index=0,
            ),
            workflow.normalize_ambiguous_item(
                source_path="material.jsonl",
                issue_code="material_unmapped",
                item={
                    "canonical_bld_id": "bld_000002",
                    "name": "House B",
                    "unmapped_material_terms": ["public   square"],
                    "raw_material_visual": ["public   square"],
                },
                index=1,
            ),
            workflow.normalize_ambiguous_item(
                source_path="year.jsonl",
                issue_code="year_conflict",
                item={"cid": "bld_000003", "name": "House C", "row_year": 2020, "source_years": [2020, 2021]},
                index=0,
            ),
        ],
    }

    fast = workflow.build_fast_snapshot(snapshot, {"decisions": {}, "term_decisions": {}})

    assert fast["counts"]["case_cards"] == 1
    assert fast["counts"]["term_cards"] == 2
    assert fast["queue"][0]["kind"] == "case"
    assert fast["queue"][0]["issue_code"] == "year_conflict"
    term_cards = [card for card in fast["queue"] if card["kind"] == "term"]
    public_square = next(card for card in term_cards if card["term_key"] == "public square")
    assert public_square["occurrence_count"] == 2
    assert [example["canonical_bld_id"] for example in public_square["examples"]] == ["bld_000001", "bld_000002"]


def test_save_fast_term_decision_preserves_existing_case_decisions(tmp_path):
    old_case = workflow.normalize_ambiguous_item(
        source_path="country.jsonl",
        issue_code="country_conflict",
        item={"cid": "bld_000002", "name": "House B", "row_country": "Korea", "normalized": ["South Korea"]},
        index=0,
    )
    snapshot = {
        "version": 1,
        "snapshot_path": "snapshot.json",
        "cases": [
            old_case,
            workflow.normalize_ambiguous_item(
                source_path="material.jsonl",
                issue_code="material_unmapped",
                item={
                    "canonical_bld_id": "bld_000001",
                    "name": "House A",
                    "unmapped_material_terms": ["media equipment"],
                    "raw_material_visual": ["media equipment"],
                },
                index=0,
            )
        ],
    }
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps(
            {
                "version": 1,
                "snapshot_path": "snapshot.json",
                "decisions": {
                    old_case["case_id"]: {
                        "case_id": old_case["case_id"],
                        "decision": "keep",
                        "payload": {},
                        "notes": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    saved = workflow.save_fast_decision(
        snapshot,
        {
            "card_id": "term:media equipment",
            "decision": "search_keyword",
            "payload": {"term": "media equipment"},
            "notes": "keep recall",
        },
        decisions_path,
    )

    assert saved["decisions"][old_case["case_id"]]["decision"] == "keep"
    assert saved["term_decisions"]["media equipment"]["decision"] == "search_keyword"
    assert saved["term_decisions"]["media equipment"]["payload"]["term"] == "media equipment"


def test_save_fast_term_decision_rejects_invalid_action(tmp_path):
    snapshot = {"version": 1, "cases": []}

    with pytest.raises(ValueError, match="term decision"):
        workflow.save_fast_decision(
            snapshot,
            {"card_id": "term:media equipment", "decision": "bad_action", "payload": {}},
            tmp_path / "decisions.json",
        )


def test_apply_decisions_handles_material_term_keyword_actions(tmp_path):
    input_path = tmp_path / "c25.json"
    output_path = tmp_path / "c26.json"
    patch_path = tmp_path / "patch.json"
    decisions_path = tmp_path / "decisions.json"

    input_path.write_text(
        json.dumps({"buildings": [_sample_row(material_visual=["concrete", "public square", "media equipment"])]}),
        encoding="utf-8",
    )
    decisions_path.write_text(
        json.dumps(
            {
                "version": 1,
                "decisions": {},
                "term_decisions": {
                    "public square": {
                        "term_key": "public square",
                        "decision": "search_keyword",
                        "payload": {"term": "public square"},
                    },
                    "media equipment": {
                        "term_key": "media equipment",
                        "decision": "delete",
                        "payload": {"term": "media equipment"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    report = workflow.apply_decisions(
        input_path=input_path,
        decisions_path=decisions_path,
        output_path=output_path,
        patch_path=patch_path,
        write_artifact=True,
    )

    changed = json.loads(output_path.read_text(encoding="utf-8"))["buildings"][0]
    assert report["status"] == "PASS"
    assert changed["material_visual"] == ["concrete"]
    assert report["term_action_counts"] == {"delete": 1, "search_keyword": 1}
    assert report["search_keyword_patch"]["additions"]["bld_000001"] == ["public square"]
    assert report["search_keyword_patch"]["removals"]["bld_000001"] == ["media equipment"]
