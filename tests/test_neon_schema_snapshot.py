"""Offline tests for tools/neon_schema_snapshot.py (no network, no DB)."""
from __future__ import annotations

import copy
import json

from tools import neon_schema_snapshot as nss


def _raw():
    return {
        "server_version_num": "170011",
        "schemas_present": ["public"],
        "extensions": [("vector", "0.8.0"), ("plpgsql", "1.0")],
        "relations": [("public", "canonical_v2_buildings", "r", None),
                      ("public", "canonical_v2_tag_stats", "r", None)],
        "columns": [
            ("public", "canonical_v2_buildings", "name", "text", True, None, "", ""),
            ("public", "canonical_v2_buildings", "embedding", "vector(384)", False, None, "", ""),
            ("public", "canonical_v2_buildings", "canonical_bld_id", "text", True, None, "", ""),
            ("public", "canonical_v2_tag_stats", "corpus_version", "text", True, "'x'::text", "", ""),
        ],
        "indexes": [("public", "canonical_v2_buildings", "canonical_v2_buildings_pkey",
                     "CREATE UNIQUE INDEX canonical_v2_buildings_pkey ON public.canonical_v2_buildings USING btree (canonical_bld_id)")],
        "constraints": [("public", "canonical_v2_buildings", "canonical_v2_buildings_pkey", "p",
                         "PRIMARY KEY (canonical_bld_id)")],
        "row_counts": {"public.canonical_v2_buildings": 39478, "public.canonical_v2_tag_stats": 15655},
        "corpus_versions": {"public.canonical_v2_tag_stats": ["v2", "v1"]},
    }


def test_normalize_shape_sorted_and_no_host():
    snap = nss.normalize(_raw(), ["public"])
    assert snap["format"] == nss.FORMAT and snap["server_version_major"] == 17
    cols = snap["tables"]["public.canonical_v2_buildings"]["columns"]
    assert list(cols) == sorted(cols)
    assert cols["embedding"] == {"type": "vector(384)", "nullable": True, "default": None}
    assert cols["name"]["nullable"] is False
    assert snap["data"]["corpus_versions"]["public.canonical_v2_tag_stats"] == ["v1", "v2"]
    text = nss.dumps(snap)
    assert json.loads(text) == snap
    for bad in ("host", "password", "neon.tech", "sslmode", "user"):
        assert bad not in text.lower()


def test_normalize_is_order_independent():
    a = nss.normalize(_raw(), ["public"])
    r = _raw()
    for k in ("columns", "relations", "extensions"):
        r[k] = list(reversed(r[k]))
    assert nss.dumps(nss.normalize(r, ["public"])) == nss.dumps(a)


def test_identical_is_exit_0():
    s = nss.normalize(_raw(), ["public"])
    text, code = nss.report(nss.diff(s, copy.deepcopy(s)), "a", "b", strict_data=True)
    assert code == 0 and "schema: identical" in text


def test_schema_drift_exit_2():
    a = nss.normalize(_raw(), ["public"])
    b = copy.deepcopy(a)
    b["tables"]["public.canonical_v2_buildings"]["columns"]["embedding"]["type"] = "vector(768)"
    b["tables"]["public.canonical_v2_buildings"]["columns"]["new_col"] = {"type": "int", "nullable": True, "default": None}
    del b["tables"]["public.canonical_v2_buildings"]["indexes"]["canonical_v2_buildings_pkey"]
    b["extensions"]["vector"] = "0.7.0"
    d = nss.diff(a, b)
    assert len(d["schema"]) == 4 and not d["data"]
    joined = "\n".join(d["schema"])
    assert "~ tables/public.canonical_v2_buildings/columns/embedding: type: \"vector(384)\" -> \"vector(768)\"" in joined
    assert "+ tables/public.canonical_v2_buildings/columns/new_col" in joined
    assert "- tables/public.canonical_v2_buildings/indexes/canonical_v2_buildings_pkey" in joined
    text, code = nss.report(d, "a", "b", strict_data=False)
    assert code == 2 and text.count("DRIFT") == 1


def test_data_diff_is_info_unless_strict():
    a = nss.normalize(_raw(), ["public"])
    b = copy.deepcopy(a)
    b["data"]["row_counts"]["public.canonical_v2_buildings"] = 40000
    b["data"]["corpus_versions"]["public.canonical_v2_tag_stats"] = ["v3"]
    d = nss.diff(a, b)
    assert not d["schema"] and len(d["data"]) == 2
    text, code = nss.report(d, "a", "b", strict_data=False)
    assert code == 0 and "INFO" in text
    _, code = nss.report(d, "a", "b", strict_data=True)
    assert code == 3


def test_cli_file_vs_file(tmp_path, capsys):
    a = nss.normalize(_raw(), ["public"])
    b = copy.deepcopy(a)
    b["tables"]["public.canonical_v2_buildings"]["columns"]["name"]["nullable"] = True
    fa, fb = tmp_path / "a.json", tmp_path / "b.json"
    fa.write_text(nss.dumps(a), encoding="utf-8")
    fb.write_text(nss.dumps(b), encoding="utf-8")
    assert nss.main(["--snapshot-file", str(fa), "--compare", str(fa)]) == 0
    assert nss.main(["--snapshot-file", str(fa), "--compare", str(fb)]) == 2
    assert "nullable" in capsys.readouterr().out


def test_committed_prod_snapshot_is_wellformed():
    path = nss.ROOT / "docs" / "schema" / "archi_data.schema.json"
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert snap["format"] == nss.FORMAT
    assert "public.canonical_v2_buildings" in snap["tables"]
    assert nss.dumps(snap) == path.read_text(encoding="utf-8")  # already normalized
    assert "neon.tech" not in path.read_text(encoding="utf-8")
