from tools import canonical_v2_generic_merge_audit as audit


def _row(**overrides):
    row = {
        "canonical_bld_id": "bld_1",
        "name": "House K",
        "n_sources": 2,
        "source_refs": {"divisare": ["1"], "archello": ["2"]},
    }
    row.update(overrides)
    return row


def test_audit_passes_generic_name_when_source_metadata_agrees():
    source_lookup = {
        ("divisare", "1"): {
            "name": "House K",
            "city": "Stockholm",
            "country": "Sweden",
            "year": 2004,
            "architects": "Studio A",
        },
        ("archello", "2"): {
            "name": "HOUSE K",
            "city": "Stockholm",
            "country": "Sweden",
            "year": 2004,
            "architects": "Studio A",
        },
    }

    report = audit.audit_rows([_row()], source_lookup)

    assert report["status"] == "PASS"
    assert report["review_required"] == 0


def test_audit_blocks_generic_name_with_country_and_code_conflicts():
    source_lookup = {
        ("divisare", "1"): {
            "name": "House K",
            "city": "Stockholm",
            "country": "Sweden",
            "year": 2004,
            "architects": "Studio A",
        },
        ("archello", "2"): {
            "name": "House M",
            "city": "Bangkok",
            "country": "Thailand",
            "year": 2023,
            "architects": "Studio B",
        },
    }

    report = audit.audit_rows([_row()], source_lookup)

    assert report["status"] == "BLOCK"
    assert report["review_required"] == 1
    assert report["flag_counts"]["country_conflict"] == 1
    assert report["flag_counts"]["year_span_conflict"] == 1
    assert report["flag_counts"]["code_name_conflict"] == 1


def test_audit_ignores_single_source_duplicate_display_names():
    source_lookup = {
        ("divisare", "1"): {
            "name": "House K",
            "city": "Stockholm",
            "country": "Sweden",
            "year": 2004,
            "architects": "Studio A",
        }
    }

    report = audit.audit_rows([
        _row(canonical_bld_id="bld_1", n_sources=1, source_refs={"divisare": ["1"]})
    ], source_lookup)

    assert report["status"] == "PASS"
    assert report["review_required"] == 0


def test_audit_normalizes_country_aliases_before_blocking():
    source_lookup = {
        ("divisare", "1"): {
            "name": "Osprey House",
            "city": "Shelter Island",
            "country": "United States",
            "year": 2024,
            "architects": "Studio A",
        },
        ("archello", "2"): {
            "name": "Osprey House",
            "city": "Shelter Island",
            "country": "USA",
            "year": 2024,
            "architects": "Studio A",
        },
    }

    report = audit.audit_rows([_row(name="Osprey House")], source_lookup)

    assert report["status"] == "PASS"
    assert report["review_required"] == 0
