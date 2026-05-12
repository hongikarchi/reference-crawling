import json
import sqlite3

from tools import build_strict_canonical


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _make_source_dbs(tmp_path):
    divisare = tmp_path / "divisare.db"
    architizer = tmp_path / "architizer.db"
    archello = tmp_path / "archello.db"

    conn = sqlite3.connect(divisare)
    conn.execute(
        "CREATE TABLE divisare_projects ("
        "id INTEGER PRIMARY KEY, name TEXT, location_city TEXT, location_country TEXT, "
        "project_year INTEGER, architect_names TEXT, cover_image_url TEXT, architect_ids TEXT)"
    )
    conn.execute(
        "INSERT INTO divisare_projects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "House A", "Seoul", "South Korea", 2020, "[\"Studio A\"]", "https://img.test/a.jpg", "[101]"),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(architizer)
    conn.execute(
        "CREATE TABLE architizer_projects ("
        "id INTEGER PRIMARY KEY, name TEXT, location_city TEXT, location_country TEXT, "
        "completion_year INTEGER, firm_name TEXT, cover_image_url TEXT, firm_slug TEXT)"
    )
    conn.execute(
        "INSERT INTO architizer_projects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (2, "House B", "London", "United Kingdom", 2021, "Studio B", "https://img.test/b.jpg", "studio-b"),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(archello)
    conn.execute(
        "CREATE TABLE archello_projects ("
        "id INTEGER PRIMARY KEY, name TEXT, location_city TEXT, location_country TEXT, "
        "project_year INTEGER, architect_name TEXT, cover_image_url TEXT, architect_brand_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO archello_projects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (3, "House C", "Tokyo", "Japan", 2022, "Studio C", "https://img.test/c.jpg", 303),
    )
    conn.commit()
    conn.close()

    return divisare, architizer, archello


def test_build_resolves_architect_canonical_ids_from_source_refs(tmp_path, monkeypatch):
    divisare, architizer, archello = _make_source_dbs(tmp_path)
    monkeypatch.setattr(
        build_strict_canonical,
        "SOURCE_DBS",
        {
            "divisare": str(divisare),
            "architizer": str(architizer),
            "archello": str(archello),
            "metalocus": str(tmp_path / "missing.db"),
        },
    )

    canonical = tmp_path / "canonical.json"
    architects = tmp_path / "architects.json"
    d1 = tmp_path / "d1.jsonl"
    e1 = tmp_path / "e1.jsonl"
    e2 = tmp_path / "e2.jsonl"
    d2 = tmp_path / "d2.jsonl"
    output = tmp_path / "strict.json"

    _write_json(
        canonical,
        {
            "clusters": [
                {
                    "canonical_bld_id": "bld_1",
                    "canonical_name": "House A",
                    "n_sources": 3,
                    "source_refs": {
                        "divisare": ["1"],
                        "architizer": ["2"],
                        "archello": ["3"],
                    },
                }
            ]
        },
    )
    _write_json(
        architects,
        {
            "clusters": [
                {"canonical_arch_id": "arch_a", "canonical_name": "Studio A", "source_refs": {"divisare": ["101"]}},
                {"canonical_arch_id": "arch_b", "canonical_name": "Studio B", "source_refs": {"architizer": ["studio-b"]}},
                {"canonical_arch_id": "arch_c", "canonical_name": "Studio C", "source_refs": {"archello": ["303"]}},
            ]
        },
    )
    _write_jsonl(d1, [{"cid": "bld_1", "program": "Housing", "style": "Contemporary", "color_tone": "Neutral", "atmosphere": "Serene", "material_visual": ["glass"], "visual_description": "A calm house with glass and simple volumes."}])
    _write_jsonl(e1, [{"cid": "bld_1", "all_images": [], "best_image_per_cluster": {}}])
    _write_jsonl(e2, [{"cid": "bld_1", "covers_by_type": {"exterior": None, "interior": None, "drawing": None, "aerial": None, "detail": None}}])
    _write_jsonl(d2, [])

    build_strict_canonical.build(
        canonical_path=str(canonical),
        output_path=str(output),
        architects_path=str(architects),
        d1_path=str(d1),
        e1_path=str(e1),
        e2_path=str(e2),
        d2_path=str(d2),
    )

    row = json.loads(output.read_text(encoding="utf-8"))["buildings"][0]
    assert row["architect_canonical_ids"] == ["arch_a", "arch_b", "arch_c"]
    assert row["architect_names"] == ["Studio A", "Studio B", "Studio C"]
