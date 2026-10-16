"""Adversarial checks for local merging, carry-forward, and legacy ID safety."""
import importlib.util
from pathlib import Path

import sqlite3
from contextlib import closing

import pytest

import hashlib
import json

import canonical.vnext_identity_registry as registry
from canonical.vnext_identity_registry import (MANDATORY_STRATUM, analyze_components, apply_size3_audit, attach_ro,
    build_crosswalk, compare_pins, compare_stage_a, emit_side, local_image_index, open_output, rule_failures,
    stage_a_result, stratified_sample)

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


def test_output_connection_uri_mode_and_readonly_attach_round_trip(tmp_path):
    """Connection-integration smoke: producer-style output DB + real read-only ATTACH (WP2.9-R Fix 1)."""
    frozen = tmp_path / "frozen.sqlite"
    with closing(sqlite3.connect(frozen)) as src:
        src.execute("CREATE TABLE candidate_pairs(candidate_id TEXT, proposal TEXT)")
        src.executemany("INSERT INTO candidate_pairs VALUES (?,?)", [("c1", "merge_candidate"), ("c2", "reject")])
        src.commit()
    frozen_bytes = frozen.read_bytes()
    out_path = tmp_path / "out.sqlite"
    with out_path.open("xb"):  # producer reserves the path exclusively first
        pass
    with closing(open_output(out_path)) as out:
        out.execute("CREATE TABLE runs(run_id TEXT)")
        out.execute("INSERT INTO runs VALUES ('r1')")
        out.commit()
        attach_ro(out, frozen, "frozen")
        assert out.execute("SELECT count(*) FROM frozen.candidate_pairs").fetchone()[0] == 2
        out.execute("CREATE TABLE v4_candidate_pairs AS SELECT * FROM frozen.candidate_pairs")
        out.commit()
        for stmt in ("INSERT INTO frozen.candidate_pairs VALUES ('c3','defer')",
                     "CREATE TABLE frozen.evil(x)", "DELETE FROM frozen.candidate_pairs"):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                out.execute(stmt)
        assert out.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert frozen.read_bytes() == frozen_bytes
    with closing(sqlite3.connect(out_path)) as check:
        assert check.execute("SELECT count(*) FROM v4_candidate_pairs").fetchone()[0] == 2
        assert check.execute("SELECT run_id FROM runs").fetchone()[0] == "r1"
    with pytest.raises(sqlite3.OperationalError):
        open_output(tmp_path / "missing.sqlite").execute("CREATE TABLE t(x)")


def test_plain_connection_attach_is_the_defect(tmp_path):
    """Documents the root cause: a non-URI connection treats the ATTACH URI as a literal filename."""
    frozen = tmp_path / "frozen.sqlite"
    with closing(sqlite3.connect(frozen)) as src:
        src.execute("CREATE TABLE t(x)")
        src.commit()
    with closing(sqlite3.connect(tmp_path / "plain.sqlite")) as plain:
        with pytest.raises((sqlite3.OperationalError, ValueError)):
            attach_ro(plain, frozen, "frozen")
            plain.execute("SELECT count(*) FROM frozen.t").fetchone()


# ---- WP2.9-R: size-3 audit exclusions, mandatory sample members, Stage A reuse equality ----

RULE = {"proposal": "merge_candidate"}


def size3_fixture():
    rows = [pair("c1", "d1", "a1"), pair("c2", "d1", "a2"), pair("c3", "d2", "a3"),
            pair("c4", "d3", "a4"), pair("c5", "d3", "a5")]
    comps, _ = analyze_components(rows, {})
    base = stage_a_result(rows, comps, RULE)
    return rows, base


def audit_for(base, classes, run_id="wp29-old", sha_value="s" * 64):
    size3 = [c for c in base["components"] if c["size"] == 3]
    entries = [{"component_id": c["component_id"], "members": c["members"], "classification": classes[i],
                "exclude_from_tier": classes[i] != "within_source_duplicate", "reason": "fixture"} for i, c in enumerate(size3)]
    return {"stage_a_run_id": run_id, "inputs": {"stage_a": {"sha256": sha_value}}, "size3_component_count": len(size3),
            "components": entries,
            "excluded_component_ids": sorted(e["component_id"] for e in entries if e["exclude_from_tier"]),
            "classification_counts": {k: sum(e["classification"] == k for e in entries) for k in registry.SIZE3_CLASSES},
            "mandatory_audit_candidate_ids": sorted(cid for c in size3 for cid in c["tier_candidate_ids"])}


def test_size3_exclusion_is_carried_with_reason_and_matches_independent_replay():
    rows, base = size3_fixture()
    assert sorted(c["size"] for c in base["components"]) == [2, 3, 3]
    audit = audit_for(base, ["distinct_buildings", "within_source_duplicate"])
    comps, exclusions, mandatory = apply_size3_audit(base["components"], audit, "wp29-old", "s" * 64, "a" * 64)
    assert list(exclusions.values()) == ["distinct_buildings"] and len(mandatory) == 4
    dropped = next(c for c in comps if c["component_id"] in exclusions)
    assert dropped["reasons"] == ["wp29r_size3_audit:distinct_buildings"] and dropped["status"] == "excluded_individual_review"
    assert dropped["trigger_evidence"][-1]["audit_sha256"] == "a" * 64
    assert base["components"] != comps  # input untouched, copy returned
    final = {cid for c in comps if not c["reasons"] for cid in c["tier_candidate_ids"]}
    _, _, independent_comps = independent.independent_components(rows, set())
    replay_final, n_dropped = independent.apply_size3_exclusions(independent_comps, audit)
    assert replay_final == final and n_dropped == 1 and len(final) == 3
    summary = stage_a_result(rows, comps, RULE)
    assert summary["final_tier_pairs"] == 3 and summary["excluded_reason_counts"] == {"wp29r_size3_audit:distinct_buildings": 1}


def test_zero_exclusions_keeps_tier_and_unclear_also_excludes():
    rows, base = size3_fixture()
    comps, exclusions, _ = apply_size3_audit(base["components"], audit_for(base, ["within_source_duplicate"] * 2), "wp29-old", "s" * 64, "a")
    assert exclusions == {} and comps == base["components"]
    _, exclusions, _ = apply_size3_audit(base["components"], audit_for(base, ["unclear", "within_source_duplicate"]), "wp29-old", "s" * 64, "a")
    assert list(exclusions.values()) == ["unclear"]


@pytest.mark.parametrize("mutate", [
    lambda a: a.update(stage_a_run_id="other"),
    lambda a: a["inputs"]["stage_a"].update(sha256="x" * 64),
    lambda a: a["components"].pop(),
    lambda a: a["components"][0].update(exclude_from_tier=True),
    lambda a: a["components"][0].update(classification="maybe"),
    lambda a: a["components"][0].update(members=[["divisare", "zz"]] * 3),
    lambda a: a.update(excluded_component_ids=["cmp_x"]),
    lambda a: a.update(mandatory_audit_candidate_ids=["c1"]),
])
def test_size3_audit_binding_failures_stop(mutate):
    _, base = size3_fixture()
    audit = audit_for(base, ["within_source_duplicate"] * 2)
    mutate(audit)
    with pytest.raises(ValueError):
        apply_size3_audit(base["components"], audit, "wp29-old", "s" * 64, "a")


def sample_rows():
    rows = [pair("idc_" + str(i), "d" + str(i), "a" + str(i), exact_asset_pair_count=int(i % 9 == 0)) for i in range(250)]
    sizes = {r["candidate_id"]: 3 if i < 20 else 2 for i, r in enumerate(rows)}
    return rows, sizes


def test_mandatory_members_always_sampled_and_replayed_independently():
    rows, sizes = sample_rows()
    mandatory = ["idc_" + str(i) for i in range(20)]
    ids, strata = stratified_sample(rows, sizes, mandatory=mandatory)
    assert len(ids) == len(set(ids)) == 100 and set(mandatory) <= set(ids)
    assert strata[0]["stratum"] == MANDATORY_STRATUM and strata[0]["quota"] == 20
    assert sum(s["quota"] for s in strata) == 100
    assert not any(s["stratum"].startswith("size_3") for s in strata[1:])  # rest drawn from remaining population
    assert stratified_sample(list(reversed(rows)), sizes, mandatory=list(reversed(mandatory))) == (ids, strata)
    by_id = {r["candidate_id"]: r for r in rows}
    assert independent.replay_sample(by_id, sizes, mandatory) == (strata, ids)
    # No mandatory members: identical to the predeclared allocation, and the replay agrees.
    plain = stratified_sample(rows, sizes)
    assert all(s["stratum"] != MANDATORY_STRATUM for s in plain[1])
    assert independent.replay_sample(by_id, sizes, []) == (plain[1], plain[0])


def test_mandatory_member_outside_merged_population_fails():
    rows, sizes = sample_rows()
    with pytest.raises(ValueError, match="outside the merged population"):
        stratified_sample(rows, sizes, mandatory=["idc_excluded"])


def test_stage_a_compare_detects_any_drift():
    rows, base = size3_fixture()
    same = json.loads(json.dumps(base))
    assert compare_stage_a(same, base) == []
    tampered = json.loads(json.dumps(base))
    tampered["components"][0]["tier_candidate_ids"] = []
    assert compare_stage_a(tampered, base)[0].startswith("components")
    tampered = json.loads(json.dumps(base)) | {"final_tier_pairs": 999}
    assert compare_stage_a(tampered, base) == ["final_tier_pairs"]
    old = [{"path": "data/x.db", "sha256": "1", "size_bytes": 1}, {"path": registry.CODE_PINS[0], "sha256": "old", "size_bytes": 1}]
    assert compare_pins(old, [{"path": "data/x.db", "sha256": "1", "size_bytes": 1}]) == []
    assert compare_pins(old, [{"path": "data/x.db", "sha256": "2", "size_bytes": 1}]) == ["data/x.db"]
    assert compare_pins(old, []) == ["data/x.db"]


def run_stage_a(tmp_path, monkeypatch, original):
    rows, base = size3_fixture()
    stage_a_text = json.dumps(original)
    audit = audit_for(base, ["distinct_buildings", "within_source_duplicate"], sha_value=hashlib.sha256(stage_a_text.encode()).hexdigest())
    files = {registry.STAGE_A: stage_a_text, registry.SIZE3_AUDIT: json.dumps(audit),
             registry.LEGACY_FAIL: "{}", registry.PRE: json.dumps({"rule": RULE})}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    monkeypatch.setattr(registry, "ROOT", tmp_path)
    monkeypatch.setattr(registry, "STAGE_A_SHA", registry.sha(registry.STAGE_A))
    monkeypatch.setattr(registry, "LEGACY_FAIL_SHA", registry.sha(registry.LEGACY_FAIL))
    monkeypatch.setattr(registry, "inputs", lambda: [])
    monkeypatch.setattr(registry, "load_frozen_v4", lambda: (rows, [], {}))
    registry.stage_a()
    return json.loads((tmp_path / registry.STAGE_A_REUSE).read_text(encoding="utf-8"))


def test_stage_a_reuse_recomputes_then_applies_exclusions(tmp_path, monkeypatch):
    _, base = size3_fixture()
    original = base | {"run_id": "wp29-old", "input_pins": [], "created_at": "t"}
    out = run_stage_a(tmp_path, monkeypatch, original)
    assert out["run_id"] == "wp29r_20261016" and out["recompute_equality"]["identical"]
    assert out["pre_exclusion"]["final_tier_pairs"] == 5 and out["final_tier_pairs"] == 3
    assert out["size3_audit"]["excluded_count"] == 1 and len(out["mandatory_audit_candidate_ids"]) == 4


def test_stage_a_reuse_mismatch_fails_before_exclusions(tmp_path, monkeypatch):
    _, base = size3_fixture()
    original = base | {"run_id": "wp29-old", "input_pins": [], "created_at": "t", "initial_tier_pairs": 4}
    with pytest.raises(ValueError, match="recompute mismatch"):
        run_stage_a(tmp_path, monkeypatch, original)
    assert not (tmp_path / registry.STAGE_A_REUSE).exists()


def test_image_policies_live_local_and_empty():
    sel = [{"source_asset_id": "x1", "occurrence_url": "https://cdn/1"}, {"source_asset_id": "x2", "occurrence_url": "https://cdn/2"}]
    health = {"https://cdn/1": {"accessible": True, "http_status": 200}, "https://cdn/2": {"accessible": False, "http_status": 410}}
    local = {"x2": {"app_src": "../data/enrichment/c_cache/derivatives/x2.jpg"}}
    assert emit_side(sel, health, local, "predeclared_live_only")[0] == []
    imgs, prov, reason = emit_side(sel, health, local, "live_then_local")
    assert imgs == ["https://cdn/1", "../data/enrichment/c_cache/derivatives/x2.jpg"] and reason == "mixed_live_and_local"
    assert [p["emitted"] for p in prov] == ["live", "local"]
    assert emit_side(sel, health, {}, "live_then_local")[0] == ["https://cdn/1"]
    dead = {k: {"accessible": False, "http_status": 410} for k in health}
    images, _, reason = emit_side(sel, dead, {}, "live_then_local")
    assert images == [] and reason == "side_empty_no_live_or_local_copy"
    with pytest.raises(ValueError):
        emit_side(sel, health, {}, "rewrite")


def test_local_image_index_requires_exact_match_sha_and_pixel(tmp_path):
    enrich = tmp_path / "data/enrichment"
    cache = enrich / "divisare_architizer_semantic_availability_n10_vt_cache/derivatives"
    cache.mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    good, bad = b"jpeg-good", b"jpeg-bad"
    (cache / "g.jpg").write_bytes(good)
    (cache / "b.jpg").write_bytes(bad)
    h = lambda b: hashlib.sha256(b).hexdigest()
    with closing(sqlite3.connect(enrich / "divisare_architizer_semantic_availability_n10_vt.db")) as c:
        c.execute("CREATE TABLE pool_occurrences(run_id,census_id,source,source_asset_id)")
        c.execute("CREATE TABLE availability_results(run_id,census_id,outcome,actual_e1_pixel_sha256,derivative_relative_path,derivative_snapshot_sha256)")
        c.executemany("INSERT INTO pool_occurrences VALUES ('r',?,?,?)",
                      [(1, "architizer", "ok"), (2, "architizer", "shamismatch"), (3, "architizer", "changed"), (4, "architizer", "pixel")])
        c.executemany("INSERT INTO availability_results VALUES ('r',?,?,?,?,?)", [
            (1, "exact_match", "p1", "derivatives/g.jpg", h(good)), (2, "exact_match", "p2", "derivatives/b.jpg", h(good)),
            (3, "source_changed", "p3", "derivatives/g.jpg", h(good)), (4, "exact_match", "other", "derivatives/g.jpg", h(good))])
        c.commit()
    needed = {("architizer", "ok"): "p1", ("architizer", "shamismatch"): "p2", ("architizer", "changed"): "p3", ("architizer", "pixel"): "p4"}
    index, scanned = local_image_index(needed, root=tmp_path)
    assert list(index) == [("architizer", "ok")]
    assert index[("architizer", "ok")]["app_src"] == "../data/enrichment/divisare_architizer_semantic_availability_n10_vt_cache/derivatives/g.jpg"
    assert scanned[0]["used_assets"] == 1


MIGRATED = "host_migrated:architizer-prod.imgix.net->images.architizer.com"


@pytest.mark.parametrize("url,expected", [
    ("http://architizer-prod.imgix.net/media/mediadata/uploads/a_b.jpg?w=1680&q=60&auto=format,compress&cs=strip",
     "https://images.architizer.com/media/mediadata/uploads/a_b.jpg?width=1680&quality=60"),
    ("https://architizer-prod.imgix.net/media/139576273114406.jpg",
     "https://images.architizer.com/media/139576273114406.jpg?width=1680&quality=60"),
    ("https://architizer-prod.imgix.net/media/mediadata/uploads/Café Ü.jpg?fit=crop",
     "https://images.architizer.com/media/mediadata/uploads/Caf%C3%A9%20%C3%9C.jpg?width=1680&quality=60"),
    ("https://architizer-prod.imgix.net/media/already%20encoded.jpg?w=1",
     "https://images.architizer.com/media/already%20encoded.jpg?width=1680&quality=60"),
])
def test_host_migration_mapping_matches_independent_copy(url, expected):
    assert registry.migrate_image_url(url) == (expected, MIGRATED)
    assert independent.migrated_url(url) == (expected, MIGRATED)
    assert registry.migrate_image_url(expected)[0] == expected  # idempotent on the new host


@pytest.mark.parametrize("url", ["https://images.divisare.com/images/f_auto/x/y.jpg?w=1", "https://images.architizer.com/media/x.jpg?width=1680&quality=60",
                                 "https://imgix.net/media/x.jpg?w=1", "https://other.architizer-prod.imgix.net/media/x.jpg"])
def test_host_migration_leaves_other_hosts_untouched(url):
    assert registry.migrate_image_url(url) == independent.migrated_url(url) == (url, "original")


def _replay(selected, record, health, policy):
    independent.CHECKS.clear()
    out = independent.expected_side_images(selected, record, health, policy, None, None, "architizer", "t")
    assert all(c["passed"] for c in independent.CHECKS)
    return out


def test_host_migrated_policy_producer_and_validator_agree():
    old = ["http://architizer-prod.imgix.net/media/1.jpg?w=1680&q=60", "http://architizer-prod.imgix.net/media/É2.jpg?w=1680"]
    new = [registry.migrate_image_url(u)[0] for u in old]
    assert new[1] == "https://images.architizer.com/media/%C3%892.jpg?width=1680&quality=60"
    sel = [{"source_asset_id": "a%d" % i, "occurrence_url": u} for i, u in enumerate(old)]
    for live in ([True, True], [True, False]):
        health = {u: {"accessible": ok, "http_status": 200 if ok else 404} for u, ok in zip(new, live)}
        images, assets, reason = emit_side(sel, health, {}, "predeclared_live_host_migrated")
        record = {"image_provenance": assets, "side_image_provenance": registry.side_provenance(assets)}
        assert record["side_image_provenance"] == MIGRATED
        assert _replay(sel, record, health, "predeclared_live_host_migrated") == images
        assert images == (new if all(live) else []) and all(a["occurrence_url"] in old for a in assets)
    # A tampered provenance record fails the independent replay.
    record["image_provenance"][0] = dict(record["image_provenance"][0], checked_url=old[0])
    independent.CHECKS.clear()
    with pytest.raises(ValueError, match="host_migration_replayed"):
        independent.expected_side_images(sel, record, health, "predeclared_live_host_migrated", None, None, "architizer", "t")


def test_host_migrated_policy_keeps_divisare_original_urls():
    sel = [{"source_asset_id": "d1", "occurrence_url": "https://images.divisare.com/1.jpg"},
           {"source_asset_id": "d2", "occurrence_url": "https://images.divisare.com/2.jpg"}]
    health = {a["occurrence_url"]: {"accessible": True, "http_status": 200} for a in sel}
    images, assets, _ = emit_side(sel, health, {}, "predeclared_live_host_migrated")
    assert images == [a["occurrence_url"] for a in sel] == emit_side(sel, health, {}, "predeclared_live_only")[0]
    assert registry.side_provenance(assets) == "original" and {a["url_provenance"] for a in assets} == {"original"}
    record = {"image_provenance": assets, "side_image_provenance": "original"}
    assert _replay(sel, record, health, "predeclared_live_host_migrated") == images
    assert emit_side([], {}, {}, "predeclared_live_host_migrated")[:3:2] == ([], "no_original_assets")
