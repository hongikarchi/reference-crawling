"""Offline tests for tools/vnext_preview_seed.py (no network, no DB)."""
from __future__ import annotations

import copy
import re
from collections import Counter

import pytest

from tools import vnext_preview_seed as seed

DDL = seed.DDL_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- DDL <-> checker mirror

def _array_vocab(body: str) -> tuple[str, ...]:
    m = re.search(r"ARRAY\[(.*?)\]", body, re.S)
    return tuple(re.findall(r"'([^']+)'", m.group(1)))


def _in_vocab(col: str) -> tuple[str, ...]:
    m = re.search(rf"\b{col}\s+TEXT[^,]*?CHECK\s*\(\s*{col}\s+IN\s*\((.*?)\)\)", DDL, re.S)
    assert m, col
    return tuple(re.findall(r"'([^']+)'", m.group(1)))


@pytest.mark.parametrize("col,expected", [
    ("medium", seed.MEDIUMS),
    ("spatial_context", seed.SPATIAL_CONTEXTS),
    ("framing_scale", seed.FRAMING_SCALES),
    ("camera_angle", seed.CAMERA_ANGLES),
    ("drawing_kind", seed.DRAWING_KINDS),
    ("project_state", seed.PROJECT_STATES),
    ("project_legibility", seed.LEGIBILITIES),
])
def test_scalar_vocab_mirrors_ddl(col, expected):
    assert _in_vocab(col) == expected


def test_array_vocab_mirrors_ddl():
    checks = seed.extract_checks(DDL)
    sem = next(c for c in checks if "semantic_type <@" in c)
    slot = next(c for c in checks if "cover_slot <@" in c)
    assert _array_vocab(sem) == seed.SEMANTIC_TYPES
    assert _array_vocab(slot) == seed.COVER_SLOTS


def test_ddl_constraint_inventory_is_what_checker_mirrors():
    # If the DDL grows/shrinks a constraint, this fails and check_row must be updated.
    checks = seed.extract_checks(DDL)
    assert len(checks) == 20  # 15 column-level + 5 table-level
    assert len(re.findall(r"CREATE UNIQUE INDEX", DDL)) == 6
    assert "PRIMARY KEY (canonical_bld_id, image_asset_id)" in DDL
    assert "UNIQUE (canonical_bld_id, sort_order)" in DDL
    slots = re.findall(r"cover_slot @> ARRAY\['(\w+)'\]::TEXT\[\];", DDL)
    assert sorted(slots) == sorted(seed.COVER_SLOTS)


# --------------------------------------------------------------------------- DDL rewrite

def test_rewrite_ddl():
    out = seed.rewrite_ddl(DDL)
    assert "CREATE TABLE IF NOT EXISTS vnext_preview.building_images" in out
    assert "REFERENCES public.canonical_v2_buildings(canonical_bld_id) ON DELETE CASCADE" in out
    idx = re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)\s+ON (\S+)", out)
    assert len(idx) == 8
    assert all(name.startswith("vnext_preview_") and on == "vnext_preview.building_images" for name, on in idx)
    assert seed.extract_checks(out) == seed.extract_checks(DDL)  # CHECKs byte-identical
    body = seed._strip_sql_comments(out)
    assert not re.findall(r"(?<![.\w])building_images\b(?!_)", body)


# --------------------------------------------------------------------------- production guard

@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@ep-broad-hat-a1jaomn7.ap-southeast-1.aws.neon.tech/archi_data?sslmode=require",
    "postgres://u:p@ep-broad-hat-a1jaomn7-pooler.ap-southeast-1.aws.neon.tech/archi_data",
    "host=ep-broad-hat-a1jaomn7.aws.neon.tech dbname=archi_data user=u",
    "postgresql://u:p@EP-BROAD-HAT-A1JAOMN7.aws.neon.tech/db",
])
def test_refuses_production(dsn):
    assert seed.is_production_dsn(dsn)
    with pytest.raises(SystemExit):
        seed.refuse_if_production(dsn)


def test_allows_dev_branch():
    dsn = "postgresql://u:p@ep-quiet-dev-123456.ap-southeast-1.aws.neon.tech/archi_data"
    assert seed.dsn_host(dsn) == "ep-quiet-dev-123456.ap-southeast-1.aws.neon.tech"
    assert not seed.is_production_dsn(dsn)
    seed.refuse_if_production(dsn)


def test_apply_without_dsn_env_refused():
    with pytest.raises(SystemExit):
        seed.main(["--apply"])


# --------------------------------------------------------------------------- derivation

def _bld(**kw):
    base = {
        "canonical_bld_id": "bld_000001",
        "all_images": [{"url": f"https://img/{i}.jpg"} for i in range(6)],
        "covers_by_type": {"exterior": "https://img/0.jpg", "interior": "https://img/2.jpg",
                           "aerial": "https://img/0.jpg", "detail": None, "drawing": "https://img/5.jpg"},
        "display_cover_url": "https://img/0.jpg",
    }
    base.update(kw)
    return base


def test_derive_rows_basic_and_pass_checker():
    stats = Counter()
    rows = seed.derive_rows(_bld(), stats)
    assert [r["sort_order"] for r in rows] == list(range(6))
    assert all(r["image_asset_id"] == seed.asset_id(r["url"]) and len(r["image_asset_id"]) == 16 for r in rows)
    r0, r2, r5 = rows[0], rows[2], rows[5]
    assert r0["medium"] == "photograph" and r0["is_cover_default"]
    assert r0["cover_slot"] == ["exterior", "aerial"]
    assert r0["semantic_type"] == ["exterior_overall", "aerial"]
    assert r2["semantic_type"] == ["interior"] and r2["cover_slot"] == ["interior"]
    assert r5["medium"] == "drawing" and r5["drawing_kind"] == "unknown" and r5["semantic_type"] == ["drawing"]
    assert r5["cover_slot"] == ["drawing"] and r5["spatial_context"] is None and r5["project_state"] is None
    other = rows[1]
    assert other["semantic_type"] == ["exterior_overall"] and other["cover_slot"] == []
    assert (other["spatial_context"], other["framing_scale"], other["camera_angle"], other["project_state"]) == ("unknown",) * 4
    assert all(r["project_legibility"] == "unknown" and r["engine"] == "mock_preview"
               and r["engine_version"] == "mock-0" and r["projection_version"] == "mock-0" for r in rows)
    assert sum(r["is_cover_default"] for r in rows) == 1
    assert not seed.check_rows(rows, {"bld_000001"})


def test_derive_edge_cases_pass_checker():
    b = _bld(
        all_images=[{"url": "https://img/a.jpg"}, {"url": "https://img/a.jpg"}, {"url": " "}, {"url": None},
                    {"url": "https://img/d.jpg"}],
        covers_by_type={"exterior": "https://img/ext-alt.jpg", "drawing": "https://img/d.jpg",
                        "interior": "https://img/d.jpg"},  # drawing + photo slot conflict
        display_cover_url="https://img/d.jpg",  # display is the drawing -> no default
    )
    stats = Counter()
    rows = seed.derive_rows(b, stats)
    assert stats["skipped_duplicate_url"] == 1 and stats["skipped_blank_url"] == 2
    assert stats["appended_cover_url_not_in_all_images"] == 1
    assert stats["dropped_photo_slots_on_drawing"] == 1
    assert stats["display_cover_is_drawing_no_default"] == 1
    assert stats["buildings_without_default_cover"] == 1
    d = next(r for r in rows if r["url"] == "https://img/d.jpg")
    assert d["medium"] == "drawing" and d["cover_slot"] == ["drawing"] and not d["is_cover_default"]
    ext = next(r for r in rows if r["url"] == "https://img/ext-alt.jpg")
    assert ext["cover_slot"] == ["exterior"] and ext["sort_order"] == len(rows) - 1
    assert not seed.check_rows(rows, {b["canonical_bld_id"]})


def test_display_cover_not_in_images_is_appended_as_default():
    b = _bld(covers_by_type={}, display_cover_url="https://img/cdn.jpg")
    rows = seed.derive_rows(b)
    assert rows[-1]["url"] == "https://img/cdn.jpg" and rows[-1]["is_cover_default"]
    assert not seed.check_rows(rows)


def test_json_string_inputs_and_empty():
    import json
    b = _bld()
    b["all_images"] = json.dumps(b["all_images"])
    b["covers_by_type"] = json.dumps(b["covers_by_type"])
    assert not seed.check_rows(seed.derive_rows(b))
    assert seed.derive_rows(_bld(all_images=[], covers_by_type=None, display_cover_url=None)) == []


# --------------------------------------------------------------------------- checker negatives (one per constraint)

def _good():
    return copy.deepcopy(seed.derive_rows(_bld())[1])


def _drawing():
    return copy.deepcopy(seed.derive_rows(_bld())[5])


@pytest.mark.parametrize("mut,code", [
    (lambda r: r.update(url=None), "not_null:url"),
    (lambda r: r.update(image_asset_id="  "), "check_nonblank:image_asset_id"),
    (lambda r: r.update(url=" "), "check_nonblank:url"),
    (lambda r: r.update(engine=""), "check_nonblank:engine"),
    (lambda r: r.update(engine_version=" "), "check_nonblank:engine_version"),
    (lambda r: r.update(projection_version=""), "check_nonblank:projection_version"),
    (lambda r: r.update(sort_order=-1), "check_sort_order"),
    (lambda r: r.update(semantic_type=[]), "check_semantic_type"),
    (lambda r: r.update(semantic_type=["facade"]), "check_semantic_type"),
    (lambda r: r.update(medium="video"), "check_medium"),
    (lambda r: r.update(spatial_context="roof"), "check_spatial_context"),
    (lambda r: r.update(framing_scale="wide"), "check_framing_scale"),
    (lambda r: r.update(camera_angle="drone"), "check_camera_angle"),
    (lambda r: r.update(project_state="new"), "check_project_state"),
    (lambda r: r.update(project_legibility="great"), "check_project_legibility"),
    (lambda r: r.update(cover_slot=["hero"]), "check_cover_slot"),
    (lambda r: r.update(drawing_kind="plan"), "check_medium_field_shape"),
    (lambda r: r.update(project_state=None), "check_medium_field_shape"),
    (lambda r: r.update(camera_angle="aerial_oblique", spatial_context="interior"), "check_aerial_camera_spatial"),
    (lambda r: r.update(medium="unknown"), "check_medium_field_shape"),
    (lambda r: r.update(semantic_type=None), "not_null:semantic_type"),
    (lambda r: r.update(is_cover_default=None), "not_null:is_cover_default"),
])
def test_checker_rejects_photo_mutations(mut, code):
    r = _good()
    assert seed.check_row(r) == []
    mut(r)
    assert code in seed.check_row(r)


@pytest.mark.parametrize("mut,code", [
    (lambda r: r.update(is_cover_default=True), "check_cover_default_photograph"),
    (lambda r: r.update(cover_slot=["drawing", "exterior"]), "check_photo_slot_photograph"),
    (lambda r: r.update(drawing_kind=None), "check_medium_field_shape"),
    (lambda r: r.update(drawing_kind="napkin"), "check_drawing_kind"),
])
def test_checker_rejects_drawing_mutations(mut, code):
    r = _drawing()
    assert seed.check_row(r) == []
    mut(r)
    assert code in seed.check_row(r)


def test_checker_drawing_slot_on_photo():
    r = _good()
    r["cover_slot"] = ["drawing"]
    assert "check_drawing_slot_drawing" in seed.check_row(r)


def test_checker_three_valued_logic():
    # rendering may carry camera aerial with spatial 'exterior'; NULL camera never fails the aerial check.
    r = _good()
    r.update(medium="rendering", drawing_kind="perspective", project_state=None, camera_angle="aerial_top_down",
             spatial_context="exterior")
    assert seed.check_row(r) == []
    m = _good()
    m.update(medium="physical_model", spatial_context=None, framing_scale=None, camera_angle=None, project_state=None)
    assert seed.check_row(m) == []


def test_check_rows_uniques_and_fk():
    rows = seed.derive_rows(_bld())
    dup = copy.deepcopy(rows)
    dup[1]["image_asset_id"] = dup[0]["image_asset_id"]
    dup[2]["sort_order"] = 0
    dup[3]["is_cover_default"] = True
    dup[3]["cover_slot"] = ["exterior", "interior", "aerial", "detail"]
    dup[4]["cover_slot"] = ["detail"]
    v = seed.check_rows(dup, {"other"})
    assert v["pk_duplicate"] == 1 and v["unique_sort_order"] == 1 and v["uq_default_cover"] == 1
    assert v["uq_exterior_cover"] == 1 and v["uq_interior_cover"] == 1 and v["uq_aerial_cover"] == 1
    assert v["uq_detail_cover"] == 1 and v["fk_canonical_bld_id"] == len(dup)
    drw = copy.deepcopy(rows[5])
    drw["image_asset_id"], drw["sort_order"] = "x" * 16, 99
    assert seed.check_rows(rows + [drw])["uq_drawing_cover"] == 1


def test_print_ddl(capsys):
    assert seed.main(["--print-ddl"]) == 0
    assert "vnext_preview.building_images" in capsys.readouterr().out
