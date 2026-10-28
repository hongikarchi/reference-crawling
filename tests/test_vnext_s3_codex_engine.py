from tools import vnext_s3_codex_engine as ce

NA = "not_applicable"


def _photo(**kw):
    r = {"idx": 1, "in_scope": True, "reject_reason": "none", "medium": "photograph",
         "spatial_context": "exterior", "framing_scale": "overall", "camera_angle": "eye_level",
         "drawing_kind": NA, "project_state": "visibly_finished", "project_legibility": "high"}
    r.update(kw)
    return r


def test_valid_rows():
    assert ce.validate_row(_photo()) == []
    assert ce.validate_row(_photo(camera_angle="unknown")) == []
    assert ce.validate_row(_photo(medium="drawing", spatial_context=NA, framing_scale=NA, camera_angle=NA,
                                  drawing_kind="plan", project_state=NA)) == []
    assert ce.validate_row(_photo(medium="rendering", drawing_kind="perspective", project_state=NA)) == []
    assert ce.validate_row(_photo(in_scope=False, reject_reason="people_or_event", spatial_context=NA,
                                  framing_scale=NA, camera_angle=NA, project_state=NA,
                                  project_legibility="none")) == []


def test_invalid_rows():
    assert ce.validate_row(_photo(project_legibility="medium"))  # v1.4: medium not emitted
    assert ce.validate_row(_photo(drawing_kind="plan"))
    assert ce.validate_row(_photo(camera_angle=NA))
    assert ce.validate_row(_photo(spatial_context="interior", camera_angle="aerial_oblique"))
    assert ce.validate_row(_photo(in_scope=False, reject_reason="none"))
    assert ce.validate_row(_photo(in_scope=True, reject_reason="other"))
    assert ce.validate_row(_photo(medium="physical_model", drawing_kind=NA, project_state=NA))  # view axes set
    assert ce.validate_row({"idx": 1})


def test_cover_validation():
    assert ce.validate_cover({"ranked": [2, 1], "acceptable": [1, 2, 3]}, 3) == []
    assert ce.validate_cover({"ranked": [], "acceptable": []}, 3) == []
    assert ce.validate_cover({"ranked": [4], "acceptable": [4]}, 3)
    assert ce.validate_cover({"ranked": [1], "acceptable": [2]}, 3)


def test_derived_types():
    assert ce.derived_types(_photo()) == ["exterior_overall"]
    assert "aerial" in ce.derived_types(_photo(camera_angle="aerial_oblique"))
