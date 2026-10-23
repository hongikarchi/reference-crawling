"""S6-lite building_images projection: synthetic fixtures only (no big inputs, no network, no DB)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

import canonical.vnext_s6_lite_projection as s6
import canonical.vnext_s6_lite_projection_validator as val
from tools import architizer_image_host_migrate as host_migrate
from tools import vnext_building_images_apply as applier

ROOT = Path(__file__).resolve().parents[1]
DDL_V1 = ROOT / "docs/vnext/BUILDING_IMAGES_DDL.sql"
DDL_V11 = ROOT / "docs/vnext/BUILDING_IMAGES_DDL_v1_1.sql"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- constants / DDL

def test_architizer_rewrite_identical_to_migrate_tool() -> None:
    assert s6.ARCHITIZER_PATTERN == host_migrate.PATTERN
    assert s6.ARCHITIZER_REPLACEMENT == host_migrate.REPLACEMENT
    assert val.IMGIX.pattern == host_migrate.PATTERN
    assert val.IMGIX_REPL == host_migrate.REPLACEMENT
    old = "https://architizer-prod.imgix.net/media/a/é_b.jpg?w=1680&q=60&auto=format"
    assert s6.migrate_url(old) == "https://images.architizer.com/media/a/é_b.jpg?width=1680&quality=60"
    assert s6.migrate_url("https://images.divisare.com//x.jpg") == "https://images.divisare.com//x.jpg"
    assert s6.migrate_url(None) is None


def _columns(ddl: str) -> list[str]:
    body = ddl.split("CREATE TABLE IF NOT EXISTS", 1)[1]
    return re.findall(r"^\s{4}([a-z_]+)\s+(?:TEXT|INTEGER|BOOLEAN|TIMESTAMPTZ)", body, re.M)


def test_ddl_v1_1_same_columns_nullable_classification_and_grant() -> None:
    v1, v11 = DDL_V1.read_text(encoding="utf-8"), DDL_V11.read_text(encoding="utf-8")
    assert _columns(v1) == _columns(v11)
    assert list(s6.DDL_COLUMNS) == [c for c in _columns(v11) if c not in ("created_at", "updated_at")]
    for column in s6.NULL_CLASSIFICATION:
        line = re.search(rf"^\s{{4}}{column}\s+TEXT[^\n]*", v11, re.M).group(0)
        assert "NOT NULL" not in line, column
    assert re.search(r"semantic_type\s+TEXT\[\] NOT NULL DEFAULT '\{\}'::TEXT\[\]", v11)
    assert "medium IS NULL AND spatial_context IS NULL" in v11
    assert "cardinality(semantic_type) >= 1" in v11 and "engine IS NOT NULL AND engine_version IS NOT NULL" in v11
    assert "PRIMARY KEY (canonical_bld_id, image_asset_id)" in v11 and "UNIQUE (canonical_bld_id, sort_order)" in v11
    assert "REFERENCES public.canonical_v2_buildings(canonical_bld_id) ON DELETE CASCADE" in v11
    assert v11.count("CREATE UNIQUE INDEX IF NOT EXISTS uq_building_images_") == 6
    assert "GRANT SELECT ON public.building_images TO make_web;" in v11
    assert "medium             TEXT NOT NULL" in v1  # v1 kept unchanged


# --------------------------------------------------------------------------- pure rules

def _g(gid, source, bld, so, asset, pixel, url, area=100, usable=True):
    return {"group_id": gid, "source": source, "source_building_id": bld, "sort_order": so, "asset": asset,
            "pixel": pixel, "url": url, "score": {"usable_resolution": usable, "pixel_area": area, "short_edge": 10,
                                                  "long_edge": 10, "e3_low_information": False}}


def test_ordered_refs_identity_source_first() -> None:
    refs = {"archello": ["9"], "divisare": ["1", "2"], "architizer": ["7"]}
    assert s6.ordered_refs(refs, "architizer") == [("architizer", "7"), ("archello", "9"), ("divisare", "1"), ("divisare", "2")]
    assert s6.ordered_refs(refs, "metalocus") == [("archello", "9"), ("divisare", "1"), ("divisare", "2"), ("architizer", "7")]


def test_project_building_collapse_order_and_cover_bases() -> None:
    A, D = ("architizer", "atz_b"), ("divisare", "div_b")
    groups = {
        D: [_g("d0", "divisare", "div_b", 0, "div_a0", "p0", "https://d/0.jpg", area=50),
            _g("d1", "divisare", "div_b", 1, "div_a1", "p1", "https://d/1.jpg")],
        A: [_g("a0", "architizer", "atz_b", 0, "atz_a0", "p0", "https://a/0.jpg", area=500),
            _g("a1", "architizer", "atz_b", 1, "atz_a1", "p2", "https://a/1.jpg")],
    }
    legacy = {"canonical_bld_id": "bld_1", "display_cover_url": None}
    rows, stats = s6.project_building(legacy, [D, A], groups, {})
    # p0 collapses (cross-source), representative = larger architizer asset, anchored at divisare position
    assert [r["image_asset_id"] for r in rows] == ["atz_a0", "div_a1", "atz_a1"]
    assert [r["sort_order"] for r in rows] == [0, 1, 2]
    assert rows[0]["provenance"]["group_ids"] == ["a0", "d0"]
    assert stats["collapsed"] == {"cross_source": 1}
    assert all(r["medium"] is None and r["semantic_type"] == [] and r["is_cover_default"] is False for r in rows)
    assert all(r["projection_version"] == "vnext-s6-lite-v1" for r in rows)

    rows, stats = s6.project_building({**legacy, "display_cover_url": "https://a/1.jpg"}, [D, A], groups, {})
    assert rows[0]["image_asset_id"] == "atz_a1" and stats["cover_basis"] == "row_url"
    rows, stats = s6.project_building({**legacy, "display_cover_url": "https://d/0.jpg"}, [D, A], groups, {})
    assert rows[0]["image_asset_id"] == "atz_a0" and stats["cover_basis"] == "constituent_url"
    occ = {D: {"https://d/cover-form.jpg": "d1"}}
    rows, stats = s6.project_building({**legacy, "display_cover_url": "https://d/cover-form.jpg"}, [D, A], groups, occ)
    assert rows[0]["image_asset_id"] == "div_a1" and stats["cover_basis"] == "occurrence_url"
    assert rows[0]["provenance"]["cover_match"] == "occurrence_url" and rows[1]["provenance"]["cover_match"] is None
    rows, stats = s6.project_building({**legacy, "display_cover_url": "https://nowhere.jpg"}, [D, A], groups, {})
    assert stats["cover_basis"] is None and rows[0]["image_asset_id"] == "atz_a0"
    imgix = "https://architizer-prod.imgix.net/media/x.jpg?w=1"
    groups2 = {A: [_g("a0", "architizer", "atz_b", 0, "x0", "q0", "https://a/0.jpg"),
                   _g("a1", "architizer", "atz_b", 1, "x1", "q1", "https://images.architizer.com/media/x.jpg?width=1680&quality=60")]}
    rows, _ = s6.project_building({**legacy, "display_cover_url": imgix}, [A], groups2, {})
    assert rows[0]["image_asset_id"] == "x1"


def test_project_building_rejects_duplicate_urls() -> None:
    A = ("architizer", "b")
    groups = {A: [_g("a0", "architizer", "b", 0, "x0", "p0", "https://a/same.jpg"),
                  _g("a1", "architizer", "b", 1, "x1", "p1", "https://a/same.jpg")]}
    with pytest.raises(s6.S6LiteError):
        s6.project_building({"canonical_bld_id": "bld_1"}, [A], groups, {})


# --------------------------------------------------------------------------- end-to-end fixture

def _fixture(tmp: Path) -> dict[str, Path]:
    e2 = tmp / "e2.db"
    c = sqlite3.connect(e2)
    c.execute("CREATE TABLE source_project_buildings(run_id, source, source_project_id, source_building_id)")
    c.executemany("INSERT INTO source_project_buildings VALUES ('e2run',?,?,?)", [
        ("divisare", "11", "div_bld_11"), ("divisare", "12", "div_bld_12"), ("architizer", "77", "atz_b77"),
        ("architizer", "78", "atz_b78"),
    ])
    c.commit(); c.close()
    s2 = tmp / "s2.db"
    c = sqlite3.connect(s2)
    c.execute("CREATE TABLE runs(run_id, status, logical_sha256, e2_run_id, e2_sha256_before)")
    c.execute("INSERT INTO runs VALUES ('s2run','complete','L0',?,?)", ("e2run", _sha(e2)))
    c.execute("CREATE TABLE buildings(run_id, source, source_building_id)")
    c.executemany("INSERT INTO buildings VALUES ('s2run',?,?)", [("divisare", "div_bld_11"), ("divisare", "div_bld_12"), ("architizer", "atz_b77")])
    c.execute("CREATE TABLE gallery_groups(run_id, group_id, source, source_building_id, sort_order, representative_source_asset_id, "
              "representative_pixel_sha256, serving_url, representative_score_json)")
    def score(area):
        return json.dumps({"usable_resolution": True, "pixel_area": area, "short_edge": 1, "long_edge": 1, "e3_low_information": False})
    groups = [
        ("g11a", "divisare", "div_bld_11", 0, "dA", "P1", "https://images.divisare.com//images/f_auto/v1/project_images/1/100/a.jpg", score(10)),
        ("g11b", "divisare", "div_bld_11", 1, "dB", "P2", "https://images.divisare.com//images/f_auto/v1/project_images/1/101/b.jpg", score(10)),
        ("g77a", "architizer", "atz_b77", 0, "zA", "P2", "https://images.architizer.com/media/zA.jpg?width=1680&quality=60", score(99)),
        ("g77b", "architizer", "atz_b77", 1, "zB", "P3", "https://images.architizer.com/media/zB.jpg?width=1680&quality=60", score(5)),
        ("g12a", "divisare", "div_bld_12", 0, "dC", "P9", "https://images.divisare.com//images/f_auto/v1/project_images/2/200/c.jpg", score(1)),
    ]
    c.executemany("INSERT INTO gallery_groups VALUES ('s2run',?,?,?,?,?,?,?,?)", groups)
    c.execute("CREATE TABLE group_members(run_id, group_id, source, source_building_id, source_asset_id)")
    c.executemany("INSERT INTO group_members VALUES ('s2run',?,?,?,?)", [(g[0], g[1], g[2], g[4]) for g in groups])
    c.execute("CREATE TABLE source_occurrences(run_id, source, source_building_id, occurrence_id, source_asset_id, occurrence_url)")
    c.executemany("INSERT INTO source_occurrences VALUES ('s2run',?,?,?,?,?)", [
        ("divisare", "div_bld_11", "o1", "dB", "https://images.divisare.com//image/upload/c_fit,f_jpg/v1/project_images/1/101.jpg"),
        ("architizer", "atz_b77", "o2", "zB", "https://architizer-prod.imgix.net/media/zB.jpg?w=1"),
    ])
    c.commit(); c.close()
    s2m = tmp / "s2_manifest.json"
    s2m.write_text(json.dumps({"output": {"sha256": _sha(s2)}, "logical_sha256": "L0"}), encoding="utf-8")
    s2v = tmp / "s2_validation.json"
    s2v.write_text(json.dumps({"passed": True, "logical_sha256": "L0"}), encoding="utf-8")
    legacy = tmp / "legacy.jsonl"
    rows = [
        {"canonical_bld_id": "bld_000002", "source_refs": {"divisare": ["11"], "architizer": ["77"]}, "identity_source": "divisare",
         "display_cover_url": "https://images.divisare.com//image/upload/c_fit,f_jpg/v1/project_images/1/101.jpg", "is_publishable": True,
         "all_images": [{"source": "divisare", "url": "https://images.divisare.com//image/upload/c_fit,f_jpg/v1/project_images/1/101.jpg"},
                        {"source": "archello", "url": "https://archello.com/x.jpg"}]},
        {"canonical_bld_id": "bld_000001", "source_refs": {"architizer": ["77", "999"], "metalocus": ["M1"]}, "identity_source": "architizer",
         "display_cover_url": "https://architizer-prod.imgix.net/media/zB.jpg?w=1&q=2", "is_publishable": False,
         "all_images": [{"source": "architizer", "url": "https://architizer-prod.imgix.net/media/zA.jpg?w=1"}]},
        {"canonical_bld_id": "bld_000003", "source_refs": {"archello": ["5"], "architizer": ["78"]}, "identity_source": "archello",
         "display_cover_url": None, "is_publishable": True, "all_images": []},
        {"canonical_bld_id": "bld_000004", "source_refs": {"divisare": ["12", "11"]}, "identity_source": "divisare",
         "display_cover_url": None, "is_publishable": True, "all_images": []},
    ]
    legacy.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    ddl = tmp / "ddl.sql"
    shutil.copyfile(DDL_V11, ddl)
    return {"legacy": legacy, "s2": s2, "s2m": s2m, "s2v": s2v, "e2": e2, "ddl": ddl}


def _build(tmp: Path, f: dict[str, Path]) -> s6.BuildResult:
    return s6.build_projection(
        repo_root=tmp, legacy_path=f["legacy"], s2_db_path=f["s2"], s2_manifest_path=f["s2m"], s2_validation_path=f["s2v"],
        e2_path=f["e2"], ddl_path=f["ddl"], output_path=tmp / "out/rows.jsonl", manifest_path=tmp / "out/manifest.json",
        report_path=tmp / "out/report.json", pins=None, expected_legacy_rows=4,
    )


def test_end_to_end_build_validate_and_tamper(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    result = _build(tmp_path, f)
    rows = [json.loads(line) for line in (tmp_path / "out/rows.jsonl").read_text(encoding="utf-8").splitlines()]
    got = [(r["canonical_bld_id"], r["sort_order"], r["image_asset_id"]) for r in rows]
    assert got == [
        # architizer identity: 77 groups, cover (imgix->migrated) matches zB row url -> first
        ("bld_000001", 0, "zB"), ("bld_000001", 1, "zA"),
        # divisare first; P2 collapses dB+zA -> zA (bigger); cover via occurrence url of dB -> that row first
        ("bld_000002", 0, "zA"), ("bld_000002", 1, "dA"), ("bld_000002", 2, "zB"),
        # 12 then 11 (list order)
        ("bld_000004", 0, "dC"), ("bld_000004", 1, "dA"), ("bld_000004", 2, "dB"),
    ]
    assert rows[2]["provenance"]["cover_match"] == "occurrence_url" and rows[0]["provenance"]["cover_match"] == "row_url"
    manifest = json.loads((tmp_path / "out/manifest.json").read_text(encoding="utf-8"))
    assert manifest["row_counts"] == {"building_images": 8, "legacy_buildings": 4, "buildings_with_images": 3, "buildings_without_images": 1}
    assert manifest["ordered_rowset_sha256"] == result.ordered_rowset_sha256
    report = json.loads((tmp_path / "out/report.json").read_text(encoding="utf-8"))
    assert report["ref_mapping"]["unmapped_refs"] == {"building_not_in_s2:architizer": 1, "project_not_in_e2:architizer": 1,
                                                      "source_not_in_s2:archello": 1, "source_not_in_s2:metalocus": 1}
    assert report["pixel_collapse"] == {"cross_source": 1}
    assert report["display_cover"]["present_in_gallery"] == 2
    assert report["diff_vs_legacy_all_images"]["legacy_images_by_source"]["divisare"] == {"dropped": 1}  # dB collapsed into the zA row

    rep = val.validate_projection(tmp_path / "out/manifest.json", repo_root=tmp_path, production_pins=False)
    assert rep.passed, rep.failed_check_names

    with pytest.raises(s6.S6LiteError):  # no clobber
        _build(tmp_path, f)

    # tamper: swap two rows' order (bld_000004, no display cover) with consistent record/manifest SHAs
    # -> only the replay can catch it
    assert rows[5]["canonical_bld_id"] == rows[6]["canonical_bld_id"] == "bld_000004"
    rows[5]["sort_order"], rows[6]["sort_order"] = 1, 0
    rows[5], rows[6] = rows[6], rows[5]
    for r in rows:
        body = {k: v for k, v in r.items() if k != "record_sha256"}
        r["record_sha256"] = hashlib.sha256(s6.canonical_json(body).encode("utf-8")).hexdigest()
    data = "".join(s6.canonical_json(r) + "\n" for r in rows).encode("utf-8")
    (tmp_path / "out/rows.jsonl").write_bytes(data)
    manifest["output"]["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["output"]["size_bytes"] = len(data)
    manifest["ordered_rowset_sha256"] = s6.ordered_rowset_sha256(s6.serving_line(r) for r in rows)
    (tmp_path / "out/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rep = val.validate_projection(tmp_path / "out/manifest.json", repo_root=tmp_path, production_pins=False)
    assert rep.failed_check_names == ["full_replay_rows_equal"]


def test_build_refuses_pin_mismatch_and_failed_s2_validation(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    with pytest.raises(s6.S6LiteError, match="pin mismatch"):
        s6.build_projection(repo_root=tmp_path, legacy_path=f["legacy"], s2_db_path=f["s2"], s2_manifest_path=f["s2m"],
                            s2_validation_path=f["s2v"], e2_path=f["e2"], ddl_path=f["ddl"], output_path=tmp_path / "o.jsonl",
                            manifest_path=tmp_path / "m.json", report_path=tmp_path / "r.json",
                            pins={"legacy_snapshot": "0" * 64}, expected_legacy_rows=4)
    f["s2v"].write_text(json.dumps({"passed": False, "logical_sha256": "L0"}), encoding="utf-8")
    with pytest.raises(s6.S6LiteError, match="not PASS"):
        _build(tmp_path, f)


# --------------------------------------------------------------------------- apply tool (fake connection)

def _validated(tmp_path: Path) -> dict[str, Path]:
    f = _fixture(tmp_path)
    _build(tmp_path, f)
    manifest = tmp_path / "out/manifest.json"
    rep = val.validate_projection(manifest, repo_root=tmp_path, production_pins=False)
    payload = {"status": "PASS" if rep.passed else "FAIL", "passed": rep.passed, "failed_check_names": rep.failed_check_names,
               "ordered_rowset_sha256": rep.ordered_rowset_sha256, "manifest_sha256": _sha(manifest)}
    vpath = tmp_path / "out/validation.json"
    vpath.write_text(json.dumps(payload), encoding="utf-8")
    return {"manifest": manifest, "validation": vpath, "ddl": f["ddl"]}


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self.rowcount = 0
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None) -> None:
        c = self.conn
        c.sql.append(sql)
        s = " ".join(sql.split())
        self.rowcount = 0
        if s.startswith("SELECT current_database()"):
            self._row = ("archi_data", "neondb_owner")
        elif "to_regclass" in s:
            self._row = (c.table_exists,)
        elif "is_nullable" in s:
            self._row = (c.medium_nullable,)
        elif s == "SELECT count(*) FROM s6_stage":
            self._row = (c.staged,)
        elif "count(DISTINCT s.canonical_bld_id) FROM s6_stage s LEFT JOIN" in s:
            self._row = (c.missing_parents,)
        elif s == "SELECT count(*) FROM public.building_images":
            self._row = (c.rows,)
        elif "JOIN s6_stage s USING" in s:
            self._row = (0,)
        elif s.startswith("INSERT INTO public.building_images"):
            c.rows = c.staged
            self.rowcount = c.staged
        elif "count(DISTINCT canonical_bld_id) FROM public.building_images" in s:
            self._row = (c.buildings,)
        elif s.startswith("SELECT md5("):
            self._row = (c.md5,)
        elif "pg_roles" in s:
            self._row = (1,)
        elif "has_table_privilege" in s:
            self._row = (True,)
        elif s.startswith("SELECT count(") or s.startswith("SELECT count(*)"):
            self._row = (0,)
        else:
            self._row = None

    def copy_expert(self, sql: str, buf) -> None:
        self.conn.sql.append(sql)
        self.conn.staged = sum(1 for _ in buf.read().splitlines())

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, pre: dict, *, table_exists=False, medium_nullable="YES", missing_parents=0) -> None:
        self.sql: list[str] = []
        self.table_exists = table_exists
        self.medium_nullable = medium_nullable
        self.missing_parents = missing_parents
        self.staged = 0
        self.rows = 0
        self.buildings = pre["buildings"]
        self.md5 = pre["content_md5"]
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_apply_dry_run_rolls_back_and_apply_commits(tmp_path: Path) -> None:
    p = _validated(tmp_path)
    pre = applier.preflight(p["manifest"], p["validation"], p["ddl"], root=tmp_path)
    assert pre["buildings"] == 3 and len(pre["rows"]) == 8
    conn = FakeConn(pre)
    res = applier.sync(conn, pre, apply=False)
    assert res["status"] == "PASS" and res["committed"] is False and conn.rolled_back and not conn.committed
    assert any("CREATE TABLE IF NOT EXISTS public.building_images" in s for s in conn.sql)
    assert any(s.startswith("COPY s6_stage") for s in conn.sql)
    assert res["to_insert"] == 8 and res["inserted"] == 8
    conn = FakeConn(pre)
    res = applier.sync(conn, pre, apply=True)
    assert res["status"] == "PASS" and res["committed"] is True and conn.committed


def test_apply_refusals(tmp_path: Path) -> None:
    p = _validated(tmp_path)
    pre = applier.preflight(p["manifest"], p["validation"], p["ddl"], root=tmp_path)
    conn = FakeConn(pre, missing_parents=2)
    with pytest.raises(applier.ApplyRefused, match="no parent"):
        applier.sync(conn, pre, apply=True)
    assert conn.rolled_back and not conn.committed
    conn = FakeConn(pre, table_exists=True, medium_nullable="NO")
    with pytest.raises(applier.ApplyRefused, match="not DDL v1.1"):
        applier.sync(conn, pre, apply=True)
    conn = FakeConn(pre)
    conn.md5 = "x"
    res = applier.sync(conn, pre, apply=True)
    assert res["status"] == "FAIL" and not conn.committed and conn.rolled_back
    v = json.loads(p["validation"].read_text(encoding="utf-8"))
    p["validation"].write_text(json.dumps({**v, "manifest_sha256": "0" * 64}), encoding="utf-8")
    with pytest.raises(applier.ApplyRefused, match="different manifest"):
        applier.preflight(p["manifest"], p["validation"], p["ddl"], root=tmp_path)
    p["validation"].write_text(json.dumps({**v, "status": "FAIL", "passed": False}), encoding="utf-8")
    with pytest.raises(applier.ApplyRefused, match="not PASS"):
        applier.preflight(p["manifest"], p["validation"], p["ddl"], root=tmp_path)


def test_apply_cli_requires_confirm() -> None:
    with pytest.raises(SystemExit):
        applier.main(["--apply"])


def test_content_md5_matches_sql_order() -> None:
    rows = [("b", "x", "u2", 1, "v"), ("a", "y", "u1", 0, "v"), ("b", "z", "u3", 0, "v")]
    text = "a\ty\tu1\t0\nb\tz\tu3\t0\nb\tx\tu2\t1"
    assert applier.content_md5(rows) == hashlib.md5(text.encode()).hexdigest()


# --------------------------------------------------------------------------- percent-encoding (builder v2)

def test_cover_match_is_percent_encoding_insensitive() -> None:
    A = ("architizer", "atz_b")
    enc = "https://images.architizer.com/media/Soko%C5%82owski.jpg?width=1680&quality=60"
    raw_legacy = "https://architizer-prod.imgix.net/media/Sokołowski.jpg?w=1680&q=60"
    groups = {A: [_g("a0", "architizer", "atz_b", 0, "x0", "q0", "https://images.architizer.com/media/a.jpg?width=1680&quality=60"),
                  _g("a1", "architizer", "atz_b", 1, "x1", "q1", enc)]}
    rows, stats = s6.project_building({"canonical_bld_id": "bld_1", "display_cover_url": raw_legacy}, [A], groups, {})
    assert rows[0]["image_asset_id"] == "x1" and stats["cover_basis"] == "row_url"
    assert rows[0]["url"] == enc  # stored url stays the S2 serving url (encoded)
    assert s6.url_match_key(raw_legacy) == val._cover_key(enc) == s6.url_match_key(enc)


def test_end_to_end_non_ascii_architizer_cover(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    enc = "https://images.architizer.com/media/z%C5%82B.jpg?width=1680&quality=60"
    c = sqlite3.connect(f["s2"])
    c.execute("UPDATE gallery_groups SET serving_url=? WHERE group_id='g77b'", (enc,))
    c.execute("DELETE FROM source_occurrences WHERE occurrence_id='o2'")
    c.commit(); c.close()
    f["s2m"].write_text(json.dumps({"output": {"sha256": _sha(f["s2"])}, "logical_sha256": "L0"}), encoding="utf-8")
    recs = [json.loads(line) for line in f["legacy"].read_text(encoding="utf-8").splitlines()]
    for r in recs:
        if r["canonical_bld_id"] == "bld_000001":
            r["display_cover_url"] = "https://architizer-prod.imgix.net/media/złB.jpg?w=1"
            r["all_images"] = [{"source": "architizer", "url": "https://architizer-prod.imgix.net/media/złB.jpg?w=1"}]
    f["legacy"].write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    _build(tmp_path, f)
    rows = [json.loads(line) for line in (tmp_path / "out/rows.jsonl").read_text(encoding="utf-8").splitlines()]
    b1 = [r for r in rows if r["canonical_bld_id"] == "bld_000001"]
    assert [r["image_asset_id"] for r in b1] == ["zB", "zA"] and b1[0]["provenance"]["cover_match"] == "row_url"
    # url policy v3: the raw (host-migrated) legacy string is stored verbatim, S2 url kept in provenance
    raw = "https://images.architizer.com/media/złB.jpg?width=1680&quality=60"
    assert b1[0]["url"] == raw
    assert b1[0]["provenance"]["url_basis"] == "legacy_all_images" and b1[0]["provenance"]["s2_serving_url"] == enc
    assert b1[1]["provenance"]["url_basis"] == "s2_serving_url" and b1[1]["provenance"]["s2_serving_url"] is None
    report = json.loads((tmp_path / "out/report.json").read_text(encoding="utf-8"))
    assert report["diff_vs_legacy_all_images"]["legacy_images_by_source"]["architizer"] == {"same": 1}
    assert report["diff_vs_legacy_all_images"]["dropped_percent_encoding_only"] == 0
    assert report["url_policy"]["by_representative_source"]["architizer"]["legacy_string_reused_differs_from_s2"] == 1
    rep = val.validate_projection(tmp_path / "out/manifest.json", repo_root=tmp_path, production_pins=False)
    assert rep.passed, rep.failed_check_names
    # tamper: demote the decoded-equal cover row -> both the invariant and the replay fail
    b1[0]["sort_order"], b1[1]["sort_order"] = 1, 0
    rows = sorted(rows, key=lambda r: (r["canonical_bld_id"], r["sort_order"]))
    for r in rows:
        body = {k: v for k, v in r.items() if k != "record_sha256"}
        r["record_sha256"] = hashlib.sha256(s6.canonical_json(body).encode("utf-8")).hexdigest()
    data = "".join(s6.canonical_json(r) + "\n" for r in rows).encode("utf-8")
    (tmp_path / "out/rows.jsonl").write_bytes(data)
    manifest = json.loads((tmp_path / "out/manifest.json").read_text(encoding="utf-8"))
    manifest["output"]["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["output"]["size_bytes"] = len(data)
    manifest["ordered_rowset_sha256"] = s6.ordered_rowset_sha256(s6.serving_line(r) for r in rows)
    (tmp_path / "out/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rep = val.validate_projection(tmp_path / "out/manifest.json", repo_root=tmp_path, production_pins=False)
    assert rep.failed_check_names == ["cover_row_url_decoded_match_at_sort_order_0", "full_replay_rows_equal"]


# --------------------------------------------------------------------------- url policy v3 (builder v3)

def test_url_policy_v3_reuses_legacy_string_and_guards_collisions() -> None:
    A = ("architizer", "atz_b")
    raw = "https://images.architizer.com/media/złB.jpg?width=1680&quality=60"
    upper = "https://images.architizer.com/media/z%C5%82B.jpg?width=1680&quality=60"
    lower = "https://images.architizer.com/media/z%c5%82B.jpg?width=1680&quality=60"
    legacy = {"canonical_bld_id": "bld_1", "display_cover_url": None,
              "all_images": [{"source": "architizer", "url": "https://architizer-prod.imgix.net/media/złB.jpg?w=1"},
                             {"source": "architizer", "url": upper}]}  # first in all_images order wins
    # (a) two encoded S2 forms of the same decoded url: the first row takes the legacy string, the later keeps S2
    groups = {A: [_g("a0", "architizer", "atz_b", 0, "x0", "q0", upper), _g("a1", "architizer", "atz_b", 1, "x1", "q1", lower)]}
    rows, stats = s6.project_building(legacy, [A], groups, {})
    assert [r["url"] for r in rows] == [raw, lower]
    assert [r["provenance"]["url_basis"] for r in rows] == ["legacy_all_images", "s2_serving_url_collision"]
    assert stats["url_policy"] == {"architizer:legacy_string_reused": 1, "architizer:legacy_string_reused_differs_from_s2": 1,
                                   "architizer:s2_url_kept": 1, "architizer:s2_url_kept_collision": 1}
    # (b) the legacy string is literally another row's S2 url: the earlier row must not steal it
    groups = {A: [_g("a0", "architizer", "atz_b", 0, "x0", "q0", lower), _g("a1", "architizer", "atz_b", 1, "x1", "q1", raw)]}
    rows, _ = s6.project_building(legacy, [A], groups, {})
    assert [r["url"] for r in rows] == [lower, raw]
    assert [r["provenance"]["url_basis"] for r in rows] == ["s2_serving_url_collision", "legacy_all_images"]
    assert len({r["url"] for r in rows}) == 2
    # (c) no decoded-equal legacy string -> S2 url
    rows, _ = s6.project_building({**legacy, "all_images": []}, [A], groups, {})
    assert [r["provenance"]["url_basis"] for r in rows] == ["s2_serving_url", "s2_serving_url"]


def test_validator_catches_url_not_reused(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    enc = "https://images.architizer.com/media/z%C5%82A.jpg?width=1680&quality=60"
    c = sqlite3.connect(f["s2"])
    c.execute("UPDATE gallery_groups SET serving_url=? WHERE group_id='g77a'", (enc,))
    c.commit(); c.close()
    f["s2m"].write_text(json.dumps({"output": {"sha256": _sha(f["s2"])}, "logical_sha256": "L0"}), encoding="utf-8")
    recs = [json.loads(line) for line in f["legacy"].read_text(encoding="utf-8").splitlines()]
    for r in recs:
        if r["canonical_bld_id"] == "bld_000001":
            r["all_images"] = [{"source": "architizer", "url": "https://architizer-prod.imgix.net/media/złA.jpg?w=1"}]
    f["legacy"].write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    _build(tmp_path, f)
    rows = [json.loads(line) for line in (tmp_path / "out/rows.jsonl").read_text(encoding="utf-8").splitlines()]
    target = next(r for r in rows if r["canonical_bld_id"] == "bld_000001" and r["image_asset_id"] == "zA")
    assert target["url"] == "https://images.architizer.com/media/złA.jpg?width=1680&quality=60"
    other = next(r for r in rows if r["canonical_bld_id"] == "bld_000002" and r["image_asset_id"] == "zA")
    assert other["url"] == enc  # bld_000002 has no decoded-equal legacy string
    rep = val.validate_projection(tmp_path / "out/manifest.json", repo_root=tmp_path, production_pins=False)
    assert rep.passed, rep.failed_check_names
    # tamper: revert the reused legacy string to the encoded S2 url (provenance consistent, SHAs recomputed)
    target["url"], target["provenance"]["url_basis"], target["provenance"]["s2_serving_url"] = enc, "s2_serving_url", None
    for r in rows:
        body = {k: v for k, v in r.items() if k != "record_sha256"}
        r["record_sha256"] = hashlib.sha256(s6.canonical_json(body).encode("utf-8")).hexdigest()
    data = "".join(s6.canonical_json(r) + "\n" for r in rows).encode("utf-8")
    (tmp_path / "out/rows.jsonl").write_bytes(data)
    manifest = json.loads((tmp_path / "out/manifest.json").read_text(encoding="utf-8"))
    manifest["output"]["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["output"]["size_bytes"] = len(data)
    manifest["ordered_rowset_sha256"] = s6.ordered_rowset_sha256(s6.serving_line(r) for r in rows)
    (tmp_path / "out/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rep = val.validate_projection(tmp_path / "out/manifest.json", repo_root=tmp_path, production_pins=False)
    assert rep.failed_check_names == ["url_equals_decoded_equal_legacy_string", "full_replay_rows_equal"]
