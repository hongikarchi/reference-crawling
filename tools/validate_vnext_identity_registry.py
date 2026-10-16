"""Independent WP2.9 validator; deliberately does not import the producer."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "data/reports/vnext_identity_registry_v1"
DB = "data/enrichment/vnext_identity_registry_v1.db"
V4 = "data/enrichment/vnext_identity_candidates_v4.db"
E2 = "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"
LEGACY = "data/canonical/legacy_neon_snapshot_2026q3.jsonl"
SEED = "vnext-wp29-merge-audit-20260827-v1"
RUN_ID = "wp29r_20261016"
SIZE3_AUDIT = PREFIX + "r_size3_audit.json"
STAGE_A = PREFIX + "_stage_a.json"
STAGE_A_SHA = "2241f2979f32ad7a253556c4a017b37a067673310b9ab2fc64fe157fe07376ac"
SIZE3_KEEP = "within_source_duplicate"
SIZE3_DROP = ("distinct_buildings", "unclear")
MANDATORY_STRATUM = "mandatory_wp29r_size3_components"
RUN_FAIL = PREFIX + "r_FAIL.json"
LEGACY_FAIL = PREFIX + "_FAIL.json"
LEGACY_FAIL_SHA = "8aa98a37d7cbd6372748c1cc8707a5636e9c6de5b4ca693db789bca1dc4e0278"
CHECKS = []
POLICIES = ("predeclared_live_only", "live_then_local", "predeclared_live_host_migrated")
CODE_PATHS = ("canonical/vnext_identity_registry.py", "tools/validate_vnext_identity_registry.py")
OLD_ATZ_HOST, NEW_ATZ_HOST, NEW_ATZ_QUERY = "architizer-prod.imgix.net", "images.architizer.com", "width=1680&quality=60"


def migrated_url(url):
    """Independent copy of the producer's host migration (deliberately not imported)."""
    u = urlsplit(url)
    if u.hostname != OLD_ATZ_HOST:
        return url, "original"
    return (urlunsplit(("https", NEW_ATZ_HOST, quote(u.path, safe="/%"), NEW_ATZ_QUERY, "")),
            "host_migrated:%s->%s" % (OLD_ATZ_HOST, NEW_ATZ_HOST))


def check(name, ok, detail=None):
    CHECKS.append({"check": name, "passed": bool(ok), "detail": detail})
    if not ok:
        raise ValueError(name + ": " + str(detail))


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def hash_file(path):
    with (ROOT / path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, data):
    with (ROOT / path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def connect(path):
    file = (ROOT / path).resolve()
    for ending in ("-wal", "-journal"):
        check("no_sqlite_sidecar:" + path + ending, not Path(str(file) + ending).exists())
    db = sqlite3.connect("file:" + file.as_posix() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def endpoints(pair):
    return ((pair["left_source"], pair["left_source_building_id"]),
            (pair["right_source"], pair["right_source_building_id"]))


def independent_components(pairs, flagged):
    # BFS connected components, independent of the producer's union-find.
    qualifying = {r["candidate_id"] for r in pairs
        if r["proposal"] == "merge_candidate" and r["name_strict_score"] >= 95
        and r["firm_strict_score"] >= 95 and r["year_gap"] is not None and r["year_gap"] <= 1
        and r["exact_asset_pair_count"] + r["identical_phash_pair_count"] >= 1
        and r["candidate_id"] not in flagged}
    graph = defaultdict(set)
    for r in pairs:
        if r["candidate_id"] in qualifying:
            left, right = endpoints(r)
            graph[left].add(right); graph[right].add(left)
    components, owner = [], {}
    for start in sorted(graph):
        if start in owner:
            continue
        idx, found, queue = len(components), set(), deque([start])
        while queue:
            k = queue.popleft()
            if k in found:
                continue
            found.add(k)
            owner[k] = idx
            queue.extend(graph[k] - found)
        components.append({"members": found, "reasons": set(), "tier": set(), "internal": set()})
    for comp in components:
        if len(comp["members"]) > 4:
            comp["reasons"].add("size_gt_4")
    for r in pairs:
        left, right = endpoints(r)
        if left not in owner or owner.get(left) != owner.get(right):
            continue
        comp = components[owner[left]]
        comp["internal"].add(r["candidate_id"])
        if r["candidate_id"] in qualifying:
            comp["tier"].add(r["candidate_id"])
        if r["proposal"] in ("reject", "defer", "related_but_separate"):
            comp["reasons"].add("internal_" + r["proposal"])
        if r["candidate_id"] in flagged:
            comp["reasons"].add("internal_policy_flag")
    final = set().union(*(c["tier"] for c in components if not c["reasons"]))
    return qualifying, final, components


def apply_size3_exclusions(components, audit):
    """WP2.9-R: replay the size-3 audit on independently built components (matched by member set)."""
    drop = {}
    for c in audit["components"]:
        if c["classification"] not in (SIZE3_KEEP,) + SIZE3_DROP:
            raise ValueError("unknown size-3 classification " + str(c["classification"]))
        if c["classification"] in SIZE3_DROP:
            drop[frozenset(tuple(k) for k in c["members"])] = c["classification"]
    audited = {frozenset(tuple(k) for k in c["members"]) for c in audit["components"]}
    size3 = {frozenset(c["members"]) for c in components if len(c["members"]) == 3}
    if audited != size3:
        raise ValueError("size-3 audit does not cover exactly the size-3 components")
    for comp in components:
        cls = drop.get(frozenset(comp["members"]))
        if cls:
            comp["reasons"].add("wp29r_size3_audit:" + cls)
    return set().union(*(c["tier"] for c in components if not c["reasons"])), len(drop)


def replay_sample(rows, sizes, mandatory, n=100):
    """Independent replay of the fixed-seed stratified sample with a mandatory census stratum."""
    h = lambda kind, cid: hashlib.sha256((SEED + "|" + kind + "|" + cid).encode()).hexdigest()
    mandatory = sorted(set(mandatory), key=lambda cid: (h("select", cid), cid))
    groups = defaultdict(list)
    for cid, row in rows.items():
        if cid in mandatory:
            continue
        profile = "exact_and_identical_phash" if row["exact_asset_pair_count"] and row["identical_phash_pair_count"] else "exact_only" if row["exact_asset_pair_count"] else "identical_phash_only"
        groups[f"size_{sizes[cid]}|{profile}"].append(cid)
    seats = n - len(mandatory)
    weights = {s: len(cids) - 1 for s, cids in groups.items()}
    remaining = seats - len(groups)
    denominator = sum(weights.values())
    q = {s: 1 + (remaining * weights[s] // denominator if denominator else 0) for s in groups}
    extras = sorted(groups, key=lambda s: (-(remaining * weights[s] % denominator if denominator else 0), s))[:seats - sum(q.values())]
    for s in extras:
        q[s] += 1
    strata = ([{"stratum": MANDATORY_STRATUM, "population": len(mandatory), "quota": len(mandatory), "candidate_ids": mandatory}] if mandatory else [])
    strata += [{"stratum": s, "population": len(groups[s]), "quota": q[s], "candidate_ids": sorted(groups[s], key=lambda cid: (h("select", cid), cid))[:q[s]]} for s in sorted(groups)]
    ids = sorted([cid for s in strata for cid in s["candidate_ids"]], key=lambda cid: (h("display", cid), cid))
    return strata, ids


def validate_crosswalk(db, active, e2):
    source_projects = defaultdict(set)
    for row in e2.execute("SELECT source,source_project_id,source_building_id FROM source_project_buildings"):
        source_projects[(row[0], row[1])].add((row[0], row[2]))
    targets, members, issues, expected_refs = {}, {}, defaultdict(set), Counter()
    with (ROOT / LEGACY).open(encoding="utf-8") as f:
        for row in map(json.loads, f):
            lid = row["canonical_bld_id"]
            found = set()
            for source, refs in row["source_refs"].items():
                for ref in refs:
                    in_scope = source in {"divisare", "architizer"}
                    keys = source_projects.get((source, str(ref)), set()) if in_scope else set()
                    state = "out_of_scope" if not in_scope else "resolved" if len(keys) == 1 else "ambiguous" if keys else "unresolved"
                    if state in {"unresolved", "ambiguous"}:
                        issues[lid].add(state + "_in_scope_reference")
                    expected_refs[(lid, source, str(ref), state, tuple(sorted(keys)))] += 1
                    found.update(keys)
            members[lid] = found
            targets[lid] = {active[k] for k in found}
    claims, incoming = defaultdict(set), defaultdict(set)
    for lid in targets:
        for k in members[lid]:
            claims[k].add(lid)
        for v in targets[lid]:
            incoming[v].add(lid)
    for k, owners in claims.items():
        if len(owners) > 1:
            for lid in owners:
                issues[lid].add("source_building_claimed_by_multiple_legacy_ids")
    actual_l = {r["canonical_bld_id"]: r for r in db.execute("SELECT * FROM legacy_crosswalk")}
    check("crosswalk_all_legacy_ids", set(actual_l) == set(targets) and len(targets) == 39478)
    expected_edges = set()
    for lid, dest in targets.items():
        bad = set(issues[lid])
        for v in dest:
            if any(issues[other] for other in incoming[v]):
                bad.add("adjacent_membership_conflict")
            if len(incoming[v]) > 1 and any(len(targets[other]) > 1 for other in incoming[v]):
                bad.add("many_to_many_cardinality")
        if not dest:
            status = "unmatched"; bad.add("no_in_scope_counterpart")
        elif bad:
            status = "conflict"
        elif len(dest) > 1:
            status = "one_to_many"
        elif len(incoming[next(iter(dest))]) > 1:
            status = "many_to_one"
        else:
            status = "one_to_one"
        r = actual_l[lid]
        if (r["status"] != status or r["vnext_degree"] != len(dest)
            or set(json.loads(r["reasons_json"])) != bad or set(json.loads(r["targets_json"])) != dest
            or {tuple(k) for k in json.loads(r["source_members_json"])} != members[lid]):
            check("crosswalk_legacy_replay", False, lid)
        for v in dest:
            expected_edges.add((lid, v, status, len(dest), len(incoming[v]), tuple(sorted(bad))))
    actual_edges = {(r[0], r[1], r[2], r[3], r[4], tuple(sorted(json.loads(r[5])))) for r in db.execute("SELECT * FROM crosswalk_edges")}
    check("crosswalk_bidirectional_edges_and_reasons", expected_edges == actual_edges, len(expected_edges))
    actual_refs = Counter((r[0], r[1], r[2], r[3], tuple(tuple(k) for k in json.loads(r[4]))) for r in db.execute("SELECT * FROM legacy_source_refs"))
    check("crosswalk_all_source_refs_preserved", actual_refs == expected_refs, sum(expected_refs.values()))
    actual_v = {r["vnx_id"]: r for r in db.execute("SELECT * FROM vnext_crosswalk")}
    check("crosswalk_all_active_vnext_ids", set(actual_v) == set(active.values()))
    expected_vcounts = Counter()
    inherited = 0
    for v, r in actual_v.items():
        lids = incoming[v]
        labels = {actual_l[lid]["status"] for lid in lids}
        status = "unmatched" if not lids else "conflict" if "conflict" in labels or len(labels) != 1 else next(iter(labels))
        legacy_id = next(iter(lids)) if status == "one_to_one" else None
        if (r["status"], r["legacy_degree"], r["inherited_legacy_pk"], r["serving_id"], set(json.loads(r["legacy_ids_json"]))) != (status, len(lids), legacy_id, legacy_id or v, lids):
            check("crosswalk_vnext_replay", False, v)
        expected_vcounts[status] += 1
        inherited += int(legacy_id is not None)
    check("crosswalk_legacy_and_vnext_replay", True, {"legacy_counts": dict(Counter(r["status"] for r in actual_l.values())), "vnext_counts": dict(expected_vcounts), "inheritable": inherited})


def validate_b(manifest):
    for p in manifest["input_pins"] + [manifest["registry"], manifest["stage_a"]]:
        check("sha_pin:" + p["path"], hash_file(p["path"]) == p["sha256"] and (ROOT / p["path"]).stat().st_size == p["size_bytes"])
    with closing(connect(V4)) as frozen, closing(connect(DB)) as db, closing(connect(E2)) as e2:
        check("sqlite_integrity", db.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        check("sqlite_foreign_keys", not db.execute("PRAGMA foreign_key_check").fetchall())
        pairs = [dict(r) for r in frozen.execute("SELECT * FROM candidate_pairs")]
        flags = {r[0] for r in frozen.execute("SELECT DISTINCT candidate_id FROM candidate_policy_flags")}
        initial, _, comps = independent_components(pairs, flags)
        check("run_provenance_fixed", manifest["run_id"] == RUN_ID)
        check("size3_audit_pinned", any(p["path"] == SIZE3_AUDIT for p in manifest["input_pins"]))
        # Stage A reuse: the independent pre-exclusion replay must equal the SHA-pinned original artifact.
        check("stage_a_original_sha_pinned", hash_file(STAGE_A) == STAGE_A_SHA)
        original_a = read(STAGE_A)
        replayed = {(frozenset(c["members"]), frozenset(c["tier"]), frozenset(c["reasons"])) for c in comps}
        stored = {(frozenset(tuple(k) for k in c["members"]), frozenset(c["tier_candidate_ids"]), frozenset(c["reasons"])) for c in original_a["components"]}
        check("stage_a_original_equals_independent_replay", replayed == stored and len(initial) == original_a["initial_tier_pairs"],
              {"replayed_components": len(replayed), "stored_components": len(stored)})
        final, dropped = apply_size3_exclusions(comps, read(SIZE3_AUDIT))
        check("size3_exclusions_replayed", True, {"excluded_components": dropped, "final_pairs": len(final)})
        expected_components = {frozenset(c["members"]): c for c in comps}
        actual_comps = list(db.execute("SELECT * FROM components"))
        check("component_count", len(actual_comps) == len(comps))
        for c in actual_comps:
            members = frozenset(tuple(k) for k in json.loads(c["members_json"]))
            check_comp = expected_components.get(members)
            if not check_comp or set(json.loads(c["reasons_json"])) != check_comp["reasons"] or set(json.loads(c["tier_candidate_ids_json"])) != check_comp["tier"] or c["size"] != len(members):
                check("component_full_exclusion_replay", False, c["component_id"])
            if c["status"] != ("excluded_individual_review" if check_comp["reasons"] else "safe_tier"):
                check("component_status", False, c["component_id"])
        check("component_full_exclusion_replay", True, {"initial_pairs": len(initial), "final_pairs": len(final)})
        ordered_keys = [tuple(r) for r in frozen.execute("SELECT source,source_building_id FROM source_buildings ORDER BY source COLLATE BINARY,source_building_id COLLATE BINARY")]
        e2_keys = {tuple(r) for r in e2.execute("SELECT source,source_building_id FROM source_buildings")}
        memberships = {(r["source"], r["source_building_id"]): r for r in db.execute("SELECT * FROM source_membership")}
        check("full_91803_membership", len(ordered_keys) == 91803 and set(ordered_keys) == e2_keys == set(memberships))
        issued = {k: "vnx_bld_" + str(i).zfill(6) for i, k in enumerate(ordered_keys, 1)}
        expected_active = dict(issued)
        for c in comps:
            if not c["reasons"]:
                survivor = min(issued[k] for k in c["members"])
                for k in c["members"]:
                    expected_active[k] = survivor
        registry = {r["vnx_id"]: r for r in db.execute("SELECT * FROM registry")}
        check("registry_all_ids_retained", set(registry) == set(issued.values()))
        v4_sources = {(r["source"], r["source_building_id"]): r["record_sha256"] for r in frozen.execute("SELECT * FROM source_buildings")}
        for i, k in enumerate(ordered_keys, 1):
            r, member = registry[issued[k]], memberships[k]
            expected = (i, k[0], k[1], expected_active[k], "active" if issued[k] == expected_active[k] else "alias", manifest["run_id"])
            actual = tuple(r[v] for v in ("issuance_ordinal", "source", "source_building_id", "survivor_id", "state", "issued_run_id"))
            if actual != expected or (member["issued_id"], member["active_id"], member["source_record_sha256"]) != (issued[k], expected_active[k], v4_sources[k]):
                check("registry_deterministic_stable_issuance", False, k)
        check("registry_deterministic_stable_issuance", True, len(issued))
        aliases = {(r[0], r[1]) for r in db.execute("SELECT alias_id,survivor_id FROM aliases")}
        check("aliases_earliest_survivor", aliases == {(issued[k], v) for k, v in expected_active.items() if issued[k] != v})
        edges = {r["candidate_id"]: r for r in db.execute("SELECT * FROM candidate_edges")}
        check("all_68049_edges_retained", len(edges) == 68049 and set(edges) == {r["candidate_id"] for r in pairs})
        check("exact_tier_reapplication", {k for k, r in edges.items() if r["final_tier"]} == final and {k for k, r in edges.items() if r["initial_rule_match"]} == initial)
        decisions = {(r["candidate_id"], r["version"]): r for r in db.execute("SELECT * FROM decisions")}
        check("decision_history_preserved", len(decisions) == len(pairs) * 2)
        for r in pairs:
            cid, (left, right) = r["candidate_id"], endpoints(r)
            edge = edges[cid]
            if (edge["left_issued_id"], edge["right_issued_id"], edge["v4_proposal"]) != (issued[left], issued[right], r["proposal"]):
                check("edge_endpoints_and_original_state", False, cid)
            if json.loads(edge["evidence_json"])["v4_candidate"] != r or not json.loads(edge["reasons_json"]):
                check("all_edge_evidence_and_reasons", False, cid)
            disposition = "merged_rule_tier" if cid in final else "carry_excluded_component" if cid in initial else {"merge_candidate": "carry_merge_candidate", "defer": "carry_defer", "related_but_separate": "carry_related_but_separate", "reject": "preserved_reject"}[r["proposal"]]
            expected_state = "merge" if cid in final else "defer" if r["proposal"] == "merge_candidate" else r["proposal"]
            new, old = decisions[(cid, "wp29-v1")], decisions[(cid, "v4")]
            if edge["disposition"] != disposition or new["status"] != expected_state or old["status"] != r["proposal"] or new["supersedes"] != old["decision_id"]:
                check("carry_forward_and_decision_supersession", False, cid)
            if expected_active[left] == expected_active[right] and (r["proposal"] != "merge_candidate" or cid in flags):
                check("active_component_forbidden_internal_edge", False, cid)
        check("all_edge_decisions_evidence_carry_forward", True, dict(Counter(r["disposition"] for r in edges.values())))
        # Byte-equivalent row values compared in original primary-key order.
        for table in ("source_buildings", "candidate_pairs", "candidate_origins", "evidence_families", "hard_conflicts", "direct_image_evidence", "candidate_policy_flags", "human_review_queue"):
            columns = list(frozen.execute("PRAGMA table_info(" + table + ")"))
            order = ",".join(r[1] for r in sorted(columns, key=lambda x: x[5]) if r[5])
            original = frozen.execute("SELECT * FROM " + table + " ORDER BY " + order)
            copied = db.execute("SELECT * FROM v4_" + table + " ORDER BY " + order)
            count = 0
            while True:
                x, y = original.fetchmany(500), copied.fetchmany(500)
                if [tuple(v) for v in x] != [tuple(v) for v in y]:
                    check("immutable_evidence_copy:" + table, False, count)
                count += len(x)
                if not x:
                    break
            check("immutable_evidence_copy:" + table, True, count)
        check("no_human_verdicts_fabricated", db.execute("SELECT COUNT(*) FROM human_reviews").fetchone()[0] == 0)
        check("manifest_registry_counts", manifest["issued_ids"] == len(issued) and manifest["active_ids"] == len(set(expected_active.values())) and manifest["aliases_building_reduction"] == len(aliases) and manifest["merged_pairs"] == len(final))
        validate_crosswalk(db, expected_active, e2)


def expected_side_images(selected, record, health, policy, e2, e2_run, source, label):
    """Replay the side's image emission from manifest provenance, verifying any local copy independently."""
    prov = record["image_provenance"]
    check("image_provenance_matches_selection:" + label, [a["source_asset_id"] for a in prov] == [a["source_asset_id"] for a in selected]
          and [a["occurrence_url"] for a in prov] == [a["occurrence_url"] for a in selected])
    if policy in ("predeclared_live_only", "predeclared_live_host_migrated"):
        mapped = [migrated_url(a["occurrence_url"]) if policy == "predeclared_live_host_migrated"
                  else (a["occurrence_url"], "original") for a in selected]
        if policy == "predeclared_live_host_migrated":
            check("host_migration_replayed:" + label, [(a["checked_url"], a["url_provenance"]) for a in prov] == mapped)
            provs = sorted({x for _, x in mapped if x != "original"})
            expected_prov = "original" if not provs else provs[0] if len(provs) == 1 else "mixed:" + ",".join(provs)
            check("side_image_provenance:" + label, record["side_image_provenance"] == expected_prov)
        ok = bool(selected) and all(health[u]["accessible"] for u, _ in mapped)
        check("predeclared_no_local_copies:" + label, all(a["local_copy"] is None for a in prov))
        return [u for u, _ in mapped] if ok else []
    out = []
    for a in prov:
        if health[a["occurrence_url"]]["accessible"]:
            check("live_preferred:" + label, a["local_copy"] is None)
            out.append(a["occurrence_url"])
            continue
        loc = a["local_copy"]
        if not loc:
            continue
        f = (ROOT / loc["file"]).resolve()
        db_path = ROOT / loc["availability_db"]
        cache = Path(str(db_path)[:-3] + "_cache").resolve()
        ok = cache in f.parents and f.is_file() and hash_file(loc["file"]) == loc["sha256"]
        ok = ok and Path(os.path.relpath(f, ROOT / "tools")).as_posix() == loc["app_src"]
        if ok:
            with closing(connect(loc["availability_db"])) as av:
                row = av.execute("""SELECT a.derivative_relative_path,a.derivative_snapshot_sha256,a.actual_e1_pixel_sha256
                    FROM availability_results a JOIN pool_occurrences p USING(run_id,census_id)
                    WHERE p.source=? AND p.source_asset_id=? AND a.outcome='exact_match' AND a.derivative_snapshot_sha256=?""",
                    (source, a["source_asset_id"], loc["sha256"])).fetchone()
            pixel = e2.execute("SELECT normalized_pixel_sha256 FROM assets WHERE run_id=? AND source=? AND source_asset_id=?",
                               (e2_run, source, a["source_asset_id"])).fetchone()
            ok = bool(row and pixel) and (cache / row[0]).resolve() == f and row[2] == pixel[0] == loc["e1_pixel_sha256"]
        check("local_copy_provenance:" + label + a["source_asset_id"], ok)
        out.append(loc["app_src"])
    return out


def validate_c(manifest):
    b = read(PREFIX + "_stage_b_validation.json")
    check("stage_b_pass_bound_to_registry", b["status"] == "PASS" and b["registry_sha256"] == hash_file(DB) == manifest["registry"]["sha256"])
    sample = read(PREFIX + "_sample_manifest.json")
    selection = read(PREFIX + "_sample_selection.json")
    check("sample_selection_pin", hash_file(sample["selection_manifest"]["path"]) == sample["selection_manifest"]["sha256"])
    check("audit_js_pin", hash_file(sample["audit_data"]["path"]) == sample["audit_data"]["sha256"])
    raw = (ROOT / "tools/vnext_merge_audit_pairs.js").read_text(encoding="utf-8")
    check("audit_js_assignment", raw.startswith("window.AUDIT_DATA = ") and raw.endswith(";\n"))
    data = json.loads(raw[len("window.AUDIT_DATA = "):-2])
    check("exact_audit_top_level_schema", set(data) == {"run_id", "sample_version", "pairs"})
    check("audit_run_version_binding", data["run_id"] == sample["run_id"] == manifest["run_id"] and data["sample_version"] == sample["sample_version"] == "wp29-merge-audit-stratified-v1")
    ids = [p["id"] for p in data["pairs"]]
    check("sample_100_unique_pairs", len(ids) == len(set(ids)) == 100 and ids == sample["candidate_ids"] == selection["candidate_ids"])
    with closing(connect(DB)) as db, closing(connect(E2)) as e2:
        rows = {r["candidate_id"]: dict(r) for r in db.execute("SELECT p.* FROM v4_candidate_pairs p JOIN candidate_edges e USING(candidate_id) WHERE e.final_tier=1")}
        sizes = {r[0]: r[1] for r in db.execute("SELECT e.candidate_id,c.size FROM candidate_edges e JOIN components c USING(component_id) WHERE e.final_tier=1")}
        check("sample_merged_population", set(ids) <= set(rows) and len(rows) == sample["population"])
        audit_pin = next((x for x in manifest["input_pins"] if x["path"] == SIZE3_AUDIT), None)
        check("size3_audit_pin_unchanged", audit_pin is not None and hash_file(SIZE3_AUDIT) == audit_pin["sha256"])
        audit = read(SIZE3_AUDIT)
        requested = sorted(audit["mandatory_audit_candidate_ids"])
        size3_tier = sorted(cid for r in db.execute("SELECT tier_candidate_ids_json FROM components WHERE size=3") for cid in json.loads(r[0]))
        check("mandatory_list_equals_all_size3_tier_pairs", requested == size3_tier, {"requested": len(requested), "size3_tier_pairs": len(size3_tier)})
        included = sorted(cid for cid in requested if cid in rows)
        not_merged = sorted(set(requested) - set(included))
        states = {r[0]: r[1] for r in db.execute("SELECT candidate_id,final_tier FROM candidate_edges WHERE candidate_id IN (%s)" % ",".join("?" * len(requested)), requested)} if requested else {}
        mi = sample["mandatory_inclusion"]
        check("mandatory_size3_pairs_included", set(included) <= set(ids) and sorted(mi["requested"]) == requested
              and sorted(mi["included"]) == included and sorted(x["candidate_id"] for x in mi["excluded_not_merged"]) == not_merged
              and set(states) == set(requested) and not any(states[c] for c in not_merged),
              {"requested": len(requested), "included": len(included), "excluded_not_merged": len(not_merged)})
        expected_strata, expected_ids = replay_sample(rows, sizes, included)
        check("fixed_seed_stratification_replayed", sample["seed"] == SEED and sample["strata"] == expected_strata and ids == expected_ids)
        policy = sample.get("image_policy")
        check("image_policy_known", policy in POLICIES, policy)
        if "code_pins" in sample:
            cp = sample["code_pins"]
            check("code_pins_stage_b_match_manifest", cp["stage_b"] == [x for x in manifest["input_pins"] if x["path"] in CODE_PATHS])
            check("code_pins_stage_c_current", [x["path"] for x in cp["stage_c"]] == list(CODE_PATHS) and all(hash_file(x["path"]) == x["sha256"] for x in cp["stage_c"]))
        e2_run = [r[0] for r in e2.execute("SELECT run_id FROM e2_runs")]
        check("e2_single_run", len(e2_run) == 1)
        e2_run = e2_run[0]
        side_records = {(s["candidate_id"], s["side"]): s for s in sample["sides"]}
        health = {u["url"]: u for u in sample["url_checks"]}
        check("all_200_sides_accounted", len(side_records) == 200)
        buildings = {(r["source"], r["source_building_id"]): r for r in db.execute("SELECT * FROM v4_source_buildings")}
        for p in data["pairs"]:
            check("pair_schema_no_labels:" + p["id"], set(p) == {"id", "left", "right", "evidence"})
            r = rows[p["id"]]
            expected_evidence = dict(name_score=r["name_strict_score"], firm_score=r["firm_strict_score"], year_gap=r["year_gap"], exact=r["exact_asset_pair_count"], identical_phash=r["identical_phash_pair_count"], component_size=sizes[p["id"]])
            check("evidence_exact_schema:" + p["id"], p["evidence"] == expected_evidence)
            for side, sk in zip(("left", "right"), endpoints(r)):
                s = p[side]
                check("side_schema_no_labels:" + p["id"] + side, set(s) == {"source", "name", "architect", "year", "city", "country", "url", "images"} and isinstance(s["images"], list) and len(s["images"]) <= 2)
                record = side_records[(p["id"], side)]
                selected = record["selected_original_assets"]
                for asset in selected:
                    exists = e2.execute("""SELECT 1 FROM source_project_buildings b CROSS JOIN project_asset_occurrences o
                        ON o.run_id=b.run_id AND o.source=b.source AND o.source_project_id=b.source_project_id
                        WHERE b.run_id=? AND b.source=? AND b.source_building_id=? AND o.source_asset_id=? AND o.occurrence_url=? AND o.source_record_sha256=?""",
                        (e2_run, *sk, asset["source_asset_id"], asset["occurrence_url"], asset["source_record_sha256"])).fetchone()
                    check("original_cdn_provenance:" + p["id"] + side + asset["source_asset_id"], bool(exists))
                expected_urls = expected_side_images(selected, record, health, policy, e2, e2_run, sk[0], p["id"] + side)
                check("inaccessible_side_empty:" + p["id"] + side, s["images"] == record["emitted_urls"] == expected_urls)
                bld = buildings[sk]
                source_urls = json.loads(bld["source_urls_json"])
                expected_side = dict(source=sk[0], name=bld["name"], architect="; ".join(json.loads(bld["firms_json"])), year=bld["completion_year_min"], city=bld["locality"] or "", country=bld["country"] or "", url=source_urls[0] if source_urls else "", images=expected_urls)
                check("source_metadata_exact:" + p["id"] + side, s == expected_side)
    app_pin = next(p for p in manifest["input_pins"] if p["path"] == "tools/vnext_merge_audit_app.html")
    check("review_app_unchanged", hash_file(app_pin["path"]) == app_pin["sha256"])
    pre_pin = next(p for p in manifest["input_pins"] if p["path"].endswith("_predeclared.json"))
    check("predeclared_rules_and_gate_unchanged", hash_file(pre_pin["path"]) == pre_pin["sha256"] and sample["human_audit_gate"] == read(pre_pin["path"])["human_audit_gate"])
    check("human_audit_pending_unlabeled", sample["human_labels"] == 0 and manifest["user_audit_status"] == "awaiting_user")
    check("no_image_body_download", sample["body_bytes_downloaded"] == 0 and all(u["body_bytes_read"] == 0 and u["method"] == "HEAD" and not u["url_rewritten"] for u in sample["url_checks"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("b", "c"), required=True)
    args = parser.parse_args()
    if (ROOT / RUN_FAIL).exists():
        raise SystemExit("WP2.9-R is stopped by FAIL report")
    if (ROOT / LEGACY_FAIL).exists() and hash_file(LEGACY_FAIL) != LEGACY_FAIL_SHA:
        raise SystemExit("WP2.9 FAIL report changed since the resume order")
    manifest = read(PREFIX + "_manifest.json")
    result = {"stage": args.stage, "run_id": manifest["run_id"], "registry_sha256": manifest["registry"]["sha256"], "at": datetime.now(timezone.utc).isoformat()}
    try:
        (validate_b if args.stage == "b" else validate_c)(manifest)
        result["status"] = "PASS"
    except Exception as exc:
        result.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc), action="STOP; no next stage; no automatic rollback")
    result["checks"] = CHECKS
    result["checks_count"] = len(CHECKS)
    write(PREFIX + "_stage_" + args.stage + "_validation.json", result)
    if result["status"] == "FAIL":
        write(RUN_FAIL, result)
    print(json.dumps({k: v for k, v in result.items() if k != "checks"}, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
