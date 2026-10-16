"""Adversarial checks for local merging, carry-forward, and legacy ID safety."""
import importlib.util
from pathlib import Path

from canonical.vnext_identity_registry import analyze_components, build_crosswalk, rule_failures, stratified_sample

spec = importlib.util.spec_from_file_location("wp29_independent", Path(__file__).resolve().parents[1] / "tools/validate_vnext_identity_registry.py")
independent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(independent)


def pair(cid, left, right, **changes):
    row = dict(candidate_id=cid, left_source="divisare", left_source_building_id=left,
               right_source="architizer", right_source_building_id=right, proposal="merge_candidate",
               name_strict_score=100, firm_strict_score=100, year_gap=0,
               exact_asset_pair_count=0, identical_phash_pair_count=1,
               phash_le8_pair_count=99, record_sha256="f" * 64)
    return row | changes


def check_both(rows, flags=None):
    flags = flags or {}
    comps, _ = analyze_components(rows, flags)
    _, final, replay = independent.independent_components(rows, set(flags))
    assert final == {cid for c in comps.values() if not c["reasons"] for cid in c["tier_candidate_ids"]}
    assert {(frozenset(c["members"]), tuple(c["reasons"])) for c in comps.values()} == {(frozenset(c["members"]), tuple(sorted(c["reasons"]))) for c in replay}
    return list(comps.values()), final


def test_phash_le8_alone_and_unknown_year_do_not_qualify():
    assert "no_exact_or_identical_phash" in rule_failures(pair("x", "a", "b", identical_phash_pair_count=0), [])
    assert rule_failures(pair("x", "a", "b", year_gap=None), [])
    assert not rule_failures(pair("x", "a", "b", name_strict_score=95, firm_strict_score=95, year_gap=1), [])


def test_any_internal_prohibited_edge_excludes_entire_component():
    safe = [pair("1", "d1", "a1"), pair("2", "d2", "a1"), pair("3", "d2", "a2")]
    for proposal in ("reject", "defer", "related_but_separate"):
        comps, final = check_both(safe + [pair("4", "d1", "a2", proposal=proposal)])
        assert final == set() and len(comps) == 1
        assert comps[0]["reasons"] == ["internal_" + proposal]
        assert set(comps[0]["tier_candidate_ids"]) == {"1", "2", "3"}


def test_non_tier_internal_policy_flag_excludes_component():
    rows = [pair("1", "d1", "a1"), pair("2", "d2", "a1"), pair("3", "d2", "a2"), pair("4", "d1", "a2", name_strict_score=80)]
    comps, final = check_both(rows, {"4": [{"flag_type": "fanout_review_required"}]})
    assert not final and comps[0]["reasons"] == ["internal_policy_flag"]


def test_large_component_exclusion_does_not_remove_other_component():
    rows = [pair("1", "d1", "a1"), pair("2", "d2", "a1"), pair("3", "d2", "a2"), pair("4", "d3", "a2"), pair("5", "d9", "a9")]
    comps, final = check_both(rows)
    assert final == {"5"}
    assert [c["size"] for c in comps if c["reasons"]] == [5]


def test_crosswalk_cardinality_and_membership_conflicts():
    projects = [dict(source="divisare", source_project_id=str(i), source_building_id="d" + str(i)) for i in range(1, 9)]
    active = {("divisare", "d" + str(i)): "v" + str(i) for i in range(1, 9)}
    active[("divisare", "d3")] = "v2"
    legacy = [dict(canonical_bld_id="l1", source_refs={"divisare": ["1"], "archello": ["other"]}),
              dict(canonical_bld_id="l2", source_refs={"divisare": ["2"]}),
              dict(canonical_bld_id="l3", source_refs={"divisare": ["3"]}),
              dict(canonical_bld_id="l4", source_refs={"divisare": ["4", "5"]}),
              dict(canonical_bld_id="l5", source_refs={"divisare": ["6"]}),
              dict(canonical_bld_id="l6", source_refs={"divisare": ["6"]}),
              dict(canonical_bld_id="l7", source_refs={"divisare": ["7", "missing"]}),
              dict(canonical_bld_id="l8", source_refs={"metalocus": ["x"]})]
    left, right, edges, refs = build_crosswalk(active, projects, legacy)
    assert {r[0]: r[1] for r in left} == dict(l1="one_to_one", l2="many_to_one", l3="many_to_one", l4="one_to_many", l5="conflict", l6="conflict", l7="conflict", l8="unmatched")
    assert {r[0]: r[3] for r in right} == dict(v1="l1", v2=None, v4=None, v5=None, v6=None, v7=None, v8=None)
    assert len(refs) == 11


def test_many_to_many_is_conflict_in_both_directions():
    active = {("divisare", "d1"): "v1", ("divisare", "d2"): "v2", ("architizer", "a1"): "v1"}
    projects = [dict(source=s, source_project_id=k, source_building_id=k) for s, k in active]
    legacy = [dict(canonical_bld_id="l1", source_refs={"divisare": ["d1", "d2"]}), dict(canonical_bld_id="l2", source_refs={"architizer": ["a1"]})]
    left, right, *_ = build_crosswalk(active, projects, legacy)
    assert all(r[1] == "conflict" for r in left + right)


def test_sample_is_fixed_and_covers_rare_strata():
    rows = [pair("idc_" + str(i), "d" + str(i), "a" + str(i), exact_asset_pair_count=int(i % 9 == 0)) for i in range(250)]
    sizes = {r["candidate_id"]: 4 if i == 249 else 2 for i, r in enumerate(rows)}
    ids, strata = stratified_sample(rows, sizes)
    assert len(ids) == len(set(ids)) == 100
    assert "idc_249" in ids
    assert stratified_sample(list(reversed(rows)), sizes) == (ids, strata)
    assert sum(s["quota"] for s in strata) == 100
