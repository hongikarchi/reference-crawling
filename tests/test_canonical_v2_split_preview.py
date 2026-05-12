from tools import canonical_v2_split_preview as preview


def test_build_preview_keeps_current_name_group_and_allocates_new_ids():
    registry = {
        "bld_000010": {
            "names": ["House O", "House T"],
            "source_refs": {"divisare": ["1"], "archello": ["2"]},
            "first_seen": "2026-05-01",
            "last_seen": "2026-05-12",
            "redirected_to": None,
        }
    }
    candidates = {
        "candidates": [
            {
                "canonical_bld_id": "bld_000010",
                "current_name": "House O",
                "split_groups": [
                    {
                        "group_name": "house o",
                        "members": [{"source": "archello", "source_id": "2", "name": "House O"}],
                    },
                    {
                        "group_name": "house t",
                        "members": [{"source": "divisare", "source_id": "1", "name": "House T"}],
                    },
                ],
            }
        ]
    }

    report = preview.build_preview(registry, candidates)

    assert report["status"] == "READY"
    split = report["splits"][0]
    assert split["keep"]["canonical_bld_id"] == "bld_000010"
    assert split["keep"]["group_name"] == "house o"
    assert split["create"][0]["canonical_bld_id"] == "bld_000011"
    assert split["create"][0]["source_refs"] == {"divisare": ["1"]}
    assert report["summary"]["source_refs_lost"] == 0
    assert report["summary"]["source_refs_duplicated"] == 0


def test_build_preview_marks_single_group_as_needing_review():
    registry = {
        "bld_000010": {
            "names": ["House O", "House T"],
            "source_refs": {"archello": ["2"]},
            "redirected_to": None,
        }
    }
    candidates = {
        "candidates": [
            {
                "canonical_bld_id": "bld_000010",
                "current_name": "House O",
                "split_groups": [
                    {
                        "group_name": "house o",
                        "members": [{"source": "archello", "source_id": "2", "name": "House O"}],
                    }
                ],
            }
        ]
    }

    report = preview.build_preview(registry, candidates)

    assert report["status"] == "NEEDS_REVIEW"
    assert report["summary"]["source_refs_lost"] == 0
