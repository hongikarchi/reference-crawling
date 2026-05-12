from tools import canonical_v2_apply_code_splits as apply_splits


def test_apply_preview_updates_registry_and_canonical_summary():
    registry = {
        "bld_000001": {
            "names": ["House O", "House T"],
            "source_refs": {"archello": ["2"], "divisare": ["1"]},
            "first_seen": "2026-05-01",
            "last_seen": "2026-05-12",
            "redirected_to": None,
        }
    }
    canonical = {
        "summary": {},
        "clusters": [
            {
                "canonical_bld_id": "bld_000001",
                "canonical_name": None,
                "names": ["House O", "House T"],
                "source_refs": {"archello": ["2"], "divisare": ["1"]},
                "first_seen": "2026-05-01",
                "last_seen": "2026-05-12",
                "n_members": 2,
                "n_sources": 2,
            }
        ],
    }
    preview = {
        "splits": [
            {
                "canonical_bld_id": "bld_000001",
                "keep": {
                    "canonical_bld_id": "bld_000001",
                    "names": ["House O"],
                    "source_refs": {"archello": ["2"]},
                },
                "create": [
                    {
                        "canonical_bld_id": "bld_000002",
                        "names": ["House T"],
                        "source_refs": {"divisare": ["1"]},
                    }
                ],
            }
        ]
    }

    out_registry, out_canonical, report = apply_splits.apply_preview(
        registry, canonical, preview
    )

    assert report["status"] == "APPLIED"
    assert out_registry["bld_000001"]["names"] == ["House O"]
    assert out_registry["bld_000001"]["source_refs"] == {"archello": ["2"]}
    assert out_registry["bld_000002"]["names"] == ["House T"]
    assert out_registry["bld_000002"]["source_refs"] == {"divisare": ["1"]}
    assert [c["canonical_bld_id"] for c in out_canonical["clusters"]] == [
        "bld_000001",
        "bld_000002",
    ]
    assert out_canonical["summary"] == {
        "n_canonicals": 2,
        "multi_source": 0,
        "by_n_sources": {"1": 2},
    }


def test_apply_preview_rejects_duplicate_new_id():
    registry = {
        "bld_000001": {"names": ["A"], "source_refs": {"a": ["1"]}},
        "bld_000002": {"names": ["B"], "source_refs": {"b": ["2"]}},
    }
    canonical = {
        "summary": {},
        "clusters": [
            {"canonical_bld_id": "bld_000001", "names": ["A"], "source_refs": {"a": ["1"]}}
        ],
    }
    preview = {
        "splits": [
            {
                "canonical_bld_id": "bld_000001",
                "keep": {"canonical_bld_id": "bld_000001", "names": ["A"], "source_refs": {"a": ["1"]}},
                "create": [{"canonical_bld_id": "bld_000002", "names": ["C"], "source_refs": {"c": ["3"]}}],
            }
        ]
    }

    try:
        apply_splits.apply_preview(registry, canonical, preview)
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected duplicate id rejection")
