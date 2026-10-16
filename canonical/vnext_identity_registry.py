"""WP2.9 local S1 registry. No credentials, remote databases, or model clients.

The user-issued WP2.9 order authorizes rule-tier merges. Human sample verdicts
remain absent; this authorization is stored separately from human review.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "data/reports/vnext_identity_registry_v1"
DB = "data/enrichment/vnext_identity_registry_v1.db"
V4 = "data/enrichment/vnext_identity_candidates_v4.db"
V4M = "data/reports/vnext_identity_candidates_v4_manifest.json"
FREEZE = "data/reports/vnext_workspace_freeze_v1.json"
E2 = "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"
LEGACY = "data/canonical/legacy_neon_snapshot_2026q3.jsonl"
ORDER = ".claude/ops/jobs/20260827_vnext_wp29_s1_registry_order.md"
APP = "tools/vnext_merge_audit_app.html"
JS = "tools/vnext_merge_audit_pairs.js"
PRE = PREFIX + "_predeclared.json"
SEED = "vnext-wp29-merge-audit-20260827-v1"
SAMPLE_VERSION = "wp29-merge-audit-stratified-v1"
COPY_TABLES = ("source_buildings", "candidate_pairs", "candidate_origins",
               "evidence_families", "hard_conflicts", "direct_image_evidence",
               "candidate_policy_flags", "human_review_queue")


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def write_new(path, value):
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("x", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def sha(path):
    with (ROOT / path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def ro(path):
    p = (ROOT / path).resolve()
    if not p.is_file():
        raise ValueError(f"Missing read-only input: {path}")
    for suffix in ("-wal", "-journal"):
        if Path(str(p) + suffix).exists():
            raise ValueError(f"Unfrozen SQLite sidecar: {p}{suffix}")
    c = sqlite3.connect("file:" + p.as_posix() + "?mode=ro&immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def pin(path, expected=None):
    actual = sha(path)
    if expected and actual != expected:
        raise ValueError(f"SHA drift: {path}: expected {expected}, actual {actual}")
    return {"path": path, "sha256": actual, "size_bytes": (ROOT / path).stat().st_size}


def check_pins(pins):
    for p in pins:
        if (ROOT / p["path"]).stat().st_size != p["size_bytes"]:
            raise ValueError("Input size drift: " + p["path"])
        pin(p["path"], p["sha256"])


def keys(row):
    return ((row["left_source"], row["left_source_building_id"]),
            (row["right_source"], row["right_source_building_id"]))


def rule_failures(row, flags):
    reasons = []
    if row["proposal"] != "merge_candidate":
        reasons.append("proposal_not_merge_candidate")
    for col in ("name_strict_score", "firm_strict_score"):
        if row[col] < 95:
            reasons.append(col + "_lt_95")
    if row["year_gap"] is None or row["year_gap"] > 1:
        reasons.append("year_gap_missing_or_gt_1")
    if row["exact_asset_pair_count"] + row["identical_phash_pair_count"] < 1:
        reasons.append("no_exact_or_identical_phash")
    if flags:
        reasons.append("policy_flag_present")
    return reasons


def analyze_components(rows, flags):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tier = [r for r in rows if not rule_failures(r, flags.get(r["candidate_id"], []))]
    for r in tier:
        a, b = map(find, keys(r))
        parent[max(a, b)] = min(a, b)
    groups = defaultdict(list)
    for k in sorted(parent):
        groups[find(k)].append(k)
    components, membership = {}, {}
    for root, members in sorted(groups.items()):
        cid = "cmp_" + hashlib.sha256(dump(members).encode()).hexdigest()[:24]
        components[cid] = {"component_id": cid, "members": members, "size": len(members),
                           "tier_candidate_ids": [], "internal_candidate_ids": [],
                           "reasons": [], "trigger_evidence": []}
        if len(members) > 4:
            components[cid]["reasons"].append("size_gt_4")
        for k in members:
            if k in membership and membership[k] != cid:
                for bad in (cid, membership[k]):
                    components[bad]["reasons"].append("source_membership_contradiction")
            membership[k] = cid
    for r in tier:
        components[membership[keys(r)[0]]]["tier_candidate_ids"].append(r["candidate_id"])
    for r in rows:
        a, b = keys(r)
        if a not in membership or membership.get(a) != membership.get(b):
            continue
        comp = components[membership[a]]
        comp["internal_candidate_ids"].append(r["candidate_id"])
        reasons = []
        if r["proposal"] in ("reject", "defer", "related_but_separate"):
            reasons.append("internal_" + r["proposal"])
        if flags.get(r["candidate_id"]):
            reasons.append("internal_policy_flag")
        comp["reasons"].extend(reasons)
        if reasons:
            comp["trigger_evidence"].append({"candidate_id": r["candidate_id"],
                "proposal": r["proposal"], "policy_flags": flags.get(r["candidate_id"], []),
                "reasons": reasons, "v4_record_sha256": r["record_sha256"]})
    for comp in components.values():
        comp["reasons"] = sorted(set(comp["reasons"]))
        comp["status"] = "excluded_individual_review" if comp["reasons"] else "safe_tier"
        comp["tier_candidate_ids"].sort()
        comp["internal_candidate_ids"].sort()
    return components, membership


def inputs():
    m = read(V4M)
    pins = [pin(V4M), pin(V4, m["stage_a_artifact_sha256"]),
            pin(FREEZE, m["input_pins"]["workspace_freeze_manifest"]["sha256_before"]),
            pin(LEGACY, "17e8da1d74f1508b80580b40a2992aa536621214992747ed39978eba1f04527f"),
            pin(ORDER), pin(APP), pin(PRE),
            pin("docs/vnext/CONTRACT_IDENTITY.md", m["input_pins"]["identity_contract"]["sha256_before"]),
            pin("canonical/vnext_identity_registry.py"),
            pin("tools/validate_vnext_identity_registry.py")]
    for p in read(FREEZE)["frozen_inputs"]:
        pins.append(pin(p["path"], p["sha256"]))
    return pins


def stage_a():
    pins = inputs()
    with closing(ro(V4)) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM candidate_pairs ORDER BY candidate_id")]
        buildings = list(c.execute("SELECT source,source_building_id FROM source_buildings ORDER BY source,source_building_id"))
        flags = defaultdict(list)
        for r in c.execute("SELECT * FROM candidate_policy_flags ORDER BY candidate_id,flag_type"):
            flags[r["candidate_id"]].append(dict(r))
    if len(buildings) != 91803 or len(rows) != 68049 or Counter(r["proposal"] for r in rows)["merge_candidate"] != 5100:
        raise ValueError("Unexpected frozen universe cardinality")
    components, _ = analyze_components(rows, flags)
    excluded = [v for v in components.values() if v["reasons"]]
    safe = [v for v in components.values() if not v["reasons"]]
    result = {"status": "PASS", "stage": "A", "created_at": now(), "input_pins": pins,
        "rule": read(PRE)["rule"], "candidate_count": len(rows), "queue_count": 5100,
        "initial_tier_pairs": sum(len(v["tier_candidate_ids"]) for v in components.values()),
        "final_tier_pairs": sum(len(v["tier_candidate_ids"]) for v in safe),
        "safe_components": len(safe), "excluded_components": len(excluded),
        "excluded_buildings": sum(v["size"] for v in excluded),
        "excluded_reason_counts": dict(Counter(reason for v in excluded for reason in v["reasons"])),
        "component_size_counts": dict(Counter(v["size"] for v in safe)),
        "components": list(components.values())}
    result["run_id"] = "wp29-" + hashlib.sha256(dump({"pins": pins, "tier": [v["tier_candidate_ids"] for v in safe]}).encode()).hexdigest()[:24]
    check_pins(pins)
    write_new(PREFIX + "_stage_a.json", result)
    print(dump({k: v for k, v in result.items() if k not in ("components", "input_pins")}), flush=True)


def build_crosswalk(source_map, project_rows, legacy_rows):
    projects = defaultdict(set)
    for r in project_rows:
        projects[(r["source"], r["source_project_id"])].add((r["source"], r["source_building_id"]))
    legacy_targets, legacy_members, refs = {}, {}, []
    direct_bad = defaultdict(set)
    claims = defaultdict(set)
    for row in legacy_rows:
        lid = row["canonical_bld_id"]
        targets, members = set(), set()
        for source, values in sorted(row["source_refs"].items()):
            for raw in values:
                k = (source, str(raw))
                found = projects.get(k, set()) if source in ("divisare", "architizer") else set()
                status = "out_of_scope" if source not in ("divisare", "architizer") else "resolved" if len(found) == 1 else "unresolved" if not found else "ambiguous"
                if status in ("unresolved", "ambiguous"):
                    direct_bad[lid].add(status + "_in_scope_reference")
                refs.append((lid, source, str(raw), status, dump(sorted(found))))
                for sk in found:
                    if sk not in source_map:
                        raise ValueError("Project membership points outside frozen source universe")
                    targets.add(source_map[sk]); members.add(sk); claims[sk].add(lid)
        legacy_targets[lid], legacy_members[lid] = targets, members
    reverse = defaultdict(set)
    for lid, targets in legacy_targets.items():
        for target in targets:
            reverse[target].add(lid)
    for sk, lids in claims.items():
        if len(lids) > 1:
            for lid in lids:
                direct_bad[lid].add("source_building_claimed_by_multiple_legacy_ids")
    legacy, edges = [], []
    for lid, targets in sorted(legacy_targets.items()):
        reasons = set(direct_bad[lid])
        for target in targets:
            for other in reverse[target]:
                if direct_bad[other]:
                    reasons.add("adjacent_membership_conflict")
                if len(targets) > 1 and len(reverse[target]) > 1 or len(reverse[target]) > 1 and len(legacy_targets[other]) > 1:
                    reasons.add("many_to_many_cardinality")
        if not targets:
            status = "unmatched"
            reasons.add("no_in_scope_counterpart")
        elif reasons:
            status = "conflict"
        elif len(targets) > 1:
            status = "one_to_many"
        elif len(reverse[next(iter(targets))]) > 1:
            status = "many_to_one"
        else:
            status = "one_to_one"
        legacy.append((lid, status, len(targets), dump(sorted(reasons)), dump(sorted(targets)), dump(sorted(legacy_members[lid]))))
        for target in sorted(targets):
            edges.append((lid, target, status, len(targets), len(reverse[target]), dump(sorted(reasons))))
    by_target = defaultdict(list)
    for e in edges:
        by_target[e[1]].append(e)
    vnext = []
    for target in sorted(set(source_map.values())):
        links = by_target[target]
        statuses = {e[2] for e in links}
        status = "unmatched" if not links else "conflict" if "conflict" in statuses else next(iter(statuses)) if len(statuses) == 1 else "conflict"
        inherited = links[0][0] if status == "one_to_one" else None
        vnext.append((target, status, len(links), inherited, inherited or target, dump(sorted(e[0] for e in links))))
    return legacy, vnext, edges, refs


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE runs(run_id TEXT PRIMARY KEY, status TEXT NOT NULL, order_path TEXT NOT NULL,
  order_sha256 TEXT NOT NULL, audit_status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE registry(vnx_id TEXT PRIMARY KEY, issuance_ordinal INTEGER UNIQUE NOT NULL,
  source TEXT NOT NULL, source_building_id TEXT NOT NULL, survivor_id TEXT NOT NULL REFERENCES registry(vnx_id),
  state TEXT NOT NULL CHECK(state IN ('active','alias')), issued_run_id TEXT NOT NULL REFERENCES runs,
  UNIQUE(source,source_building_id));
CREATE TABLE source_membership(source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  issued_id TEXT NOT NULL UNIQUE REFERENCES registry(vnx_id), active_id TEXT NOT NULL REFERENCES registry(vnx_id),
  source_record_sha256 TEXT NOT NULL, PRIMARY KEY(source,source_building_id));
CREATE TABLE components(component_id TEXT PRIMARY KEY, status TEXT NOT NULL, size INTEGER NOT NULL,
  survivor_id TEXT REFERENCES registry(vnx_id), members_json TEXT NOT NULL, reasons_json TEXT NOT NULL,
  trigger_evidence_json TEXT NOT NULL, tier_candidate_ids_json TEXT NOT NULL);
CREATE TABLE candidate_edges(candidate_id TEXT PRIMARY KEY, left_issued_id TEXT NOT NULL REFERENCES registry(vnx_id),
  right_issued_id TEXT NOT NULL REFERENCES registry(vnx_id), v4_proposal TEXT NOT NULL,
  disposition TEXT NOT NULL, initial_rule_match INTEGER NOT NULL, final_tier INTEGER NOT NULL,
  component_id TEXT REFERENCES components, reasons_json TEXT NOT NULL, evidence_json TEXT NOT NULL);
CREATE TABLE decisions(decision_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidate_edges,
  version TEXT NOT NULL, status TEXT NOT NULL, authority TEXT NOT NULL, reasons_json TEXT NOT NULL,
  supersedes TEXT REFERENCES decisions(decision_id), run_id TEXT NOT NULL REFERENCES runs);
CREATE TABLE aliases(alias_id TEXT PRIMARY KEY REFERENCES registry(vnx_id), survivor_id TEXT NOT NULL REFERENCES registry(vnx_id),
  component_id TEXT NOT NULL REFERENCES components, run_id TEXT NOT NULL REFERENCES runs, reason TEXT NOT NULL);
CREATE TABLE human_reviews(candidate_id TEXT NOT NULL REFERENCES candidate_edges, review_version TEXT NOT NULL,
  reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL, verdict TEXT NOT NULL,
  PRIMARY KEY(candidate_id,review_version,reviewer));
CREATE TABLE legacy_crosswalk(canonical_bld_id TEXT PRIMARY KEY, status TEXT NOT NULL, vnext_degree INTEGER NOT NULL,
  reasons_json TEXT NOT NULL, targets_json TEXT NOT NULL, source_members_json TEXT NOT NULL);
CREATE TABLE vnext_crosswalk(vnx_id TEXT PRIMARY KEY REFERENCES registry(vnx_id), status TEXT NOT NULL,
  legacy_degree INTEGER NOT NULL, inherited_legacy_pk TEXT UNIQUE, serving_id TEXT UNIQUE NOT NULL, legacy_ids_json TEXT NOT NULL);
CREATE TABLE crosswalk_edges(canonical_bld_id TEXT NOT NULL REFERENCES legacy_crosswalk,
  vnx_id TEXT NOT NULL REFERENCES registry, status TEXT NOT NULL, legacy_to_vnext_degree INTEGER NOT NULL,
  vnext_to_legacy_degree INTEGER NOT NULL, reasons_json TEXT NOT NULL, PRIMARY KEY(canonical_bld_id,vnx_id));
CREATE TABLE legacy_source_refs(canonical_bld_id TEXT NOT NULL REFERENCES legacy_crosswalk, source TEXT NOT NULL,
  source_ref TEXT NOT NULL, resolution TEXT NOT NULL, source_members_json TEXT NOT NULL);
CREATE INDEX candidate_edge_disposition ON candidate_edges(disposition);
"""


def stage_b():
    a = read(PREFIX + "_stage_a.json")
    if a["status"] != "PASS":
        raise ValueError("Stage A not PASS")
    check_pins(a["input_pins"])
    if (ROOT / DB).exists():
        raise ValueError("Registry exists: never overwrite or reissue IDs")
    with closing(ro(V4)) as source:
        buildings = [dict(r) for r in source.execute("SELECT * FROM source_buildings ORDER BY source,source_building_id")]
        rows = [dict(r) for r in source.execute("SELECT * FROM candidate_pairs ORDER BY candidate_id")]
        flags = defaultdict(list)
        for r in source.execute("SELECT * FROM candidate_policy_flags ORDER BY candidate_id,flag_type"):
            flags[r["candidate_id"]].append(dict(r))
    issued = {(r["source"], r["source_building_id"]): f"vnx_bld_{i:06d}" for i, r in enumerate(buildings, 1)}
    active = dict(issued)
    member_component, tier_component = {}, {}
    for comp in a["components"]:
        members = [tuple(k) for k in comp["members"]]
        for k in members:
            member_component[k] = comp
        for cid in comp["tier_candidate_ids"]:
            tier_component[cid] = comp
        if not comp["reasons"]:
            survivor = min(issued[k] for k in members)
            for k in members:
                active[k] = survivor
    with closing(ro(E2)) as e2:
        project_rows = [dict(r) for r in e2.execute("SELECT * FROM source_project_buildings ORDER BY source,source_project_id,source_building_id")]
        e2keys = {(r[0], r[1]) for r in e2.execute("SELECT source,source_building_id FROM source_buildings")}
    if e2keys != set(issued):
        raise ValueError("Frozen E2 and v4 membership differ")
    with (ROOT / LEGACY).open(encoding="utf-8") as f:
        legacy_rows = [{k: r[k] for k in ("canonical_bld_id", "source_refs")} for r in map(json.loads, f)]
    if len(legacy_rows) != 39478:
        raise ValueError("WP0b row count mismatch")
    legacy, vnext, cross_edges, refs = build_crosswalk(active, project_rows, legacy_rows)
    # Reserve the final path exclusively; failures leave the file and a FAIL report.
    with (ROOT / DB).open("xb"):
        pass
    with closing(sqlite3.connect(ROOT / DB)) as out:
        out.executescript(SCHEMA)
        out.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (a["run_id"], "local_merges_pending_user_audit", ORDER, sha(ORDER), "awaiting_user", now()))
        out.executemany("INSERT INTO registry VALUES (?,?,?,?,?,?,?)", [(issued[k], i, *k, active[k], "active" if issued[k] == active[k] else "alias", a["run_id"]) for i, k in enumerate(issued, 1)])
        out.executemany("INSERT INTO source_membership VALUES (?,?,?,?,?)", [(r["source"], r["source_building_id"], issued[(r["source"], r["source_building_id"])], active[(r["source"], r["source_building_id"])], r["record_sha256"]) for r in buildings])
        for comp in a["components"]:
            survivor = active[tuple(comp["members"][0])] if not comp["reasons"] else None
            out.execute("INSERT INTO components VALUES (?,?,?,?,?,?,?,?)", (comp["component_id"], comp["status"], comp["size"], survivor, dump(comp["members"]), dump(comp["reasons"]), dump(comp["trigger_evidence"]), dump(comp["tier_candidate_ids"])))
            if survivor:
                out.executemany("INSERT INTO aliases VALUES (?,?,?,?,?)", [(issued[tuple(k)], survivor, comp["component_id"], a["run_id"], "earliest_issued_survivor; WP2.9 user-authorized rule tier") for k in comp["members"] if issued[tuple(k)] != survivor])
        dispositions = Counter()
        for r in rows:
            cid = r["candidate_id"]
            comp = tier_component.get(cid)
            reasons = rule_failures(r, flags.get(cid, []))
            final = bool(comp and not comp["reasons"])
            if final:
                disposition = "merged_rule_tier"
                reasons = ["fixed_rule_satisfied", "component_safety_passed", "user_order_authorized_local_merge"]
            elif comp:
                disposition = "carry_excluded_component"
                reasons = comp["reasons"]
            else:
                disposition = {"merge_candidate": "carry_merge_candidate", "defer": "carry_defer", "related_but_separate": "carry_related_but_separate", "reject": "preserved_reject"}[r["proposal"]]
            dispositions[disposition] += 1
            out.execute("INSERT INTO candidate_edges VALUES (?,?,?,?,?,?,?,?,?,?)", (cid, *(issued[k] for k in keys(r)), r["proposal"], disposition, int(not rule_failures(r, flags.get(cid, []))), int(final), comp["component_id"] if comp else None, dump(reasons), dump({"v4_candidate": r, "policy_flags": flags.get(cid, []), "evidence_tables": list(COPY_TABLES[2:])})))
            old = "v4:" + cid
            out.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?)", (old, cid, "v4", r["proposal"], "pinned_v4", dump(["preserved_v4_proposal"]), None, a["run_id"]))
            state = "merge" if final else "defer" if r["proposal"] == "merge_candidate" else r["proposal"]
            out.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?)", ("wp29:" + cid, cid, "wp29-v1", state, "user_order_rule_tier" if final else "carry_forward_no_merge", dump(reasons), old, a["run_id"]))
        out.executemany("INSERT INTO legacy_crosswalk VALUES (?,?,?,?,?,?)", legacy)
        out.executemany("INSERT INTO vnext_crosswalk VALUES (?,?,?,?,?,?)", vnext)
        out.executemany("INSERT INTO crosswalk_edges VALUES (?,?,?,?,?,?)", cross_edges)
        out.executemany("INSERT INTO legacy_source_refs VALUES (?,?,?,?,?)", refs)
        out.commit()
        out.execute("ATTACH DATABASE ? AS frozen", ("file:" + (ROOT / V4).resolve().as_posix() + "?mode=ro&immutable=1",))
        for table in COPY_TABLES:
            out.execute(f"CREATE TABLE v4_{table} AS SELECT * FROM frozen.{table}")
        out.execute("CREATE INDEX v4_pair_id ON v4_candidate_pairs(candidate_id)")
        out.commit()
        if out.execute("PRAGMA foreign_key_check").fetchall() or out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("S1 SQLite integrity check failed")
    check_pins(a["input_pins"])
    result = {"status": "PASS", "stage": "B", "run_id": a["run_id"], "created_at": now(),
        "registry": pin(DB), "input_pins": a["input_pins"], "stage_a": pin(PREFIX + "_stage_a.json"),
        "issued_ids": len(issued), "active_ids": len(set(active.values())),
        "aliases_building_reduction": len(issued) - len(set(active.values())),
        "merged_pairs": a["final_tier_pairs"], "disposition_counts": dict(dispositions),
        "legacy_crosswalk_counts": dict(Counter(r[1] for r in legacy)),
        "vnext_crosswalk_counts": dict(Counter(r[1] for r in vnext)),
        "crosswalk_edge_counts": dict(Counter(r[2] for r in cross_edges)),
        "legacy_pk_inheritable": sum(r[3] is not None for r in vnext),
        "user_audit_status": "awaiting_user", "user_verdicts_created": 0,
        "human_audit_gate": read(PRE)["human_audit_gate"],
        "cost": {"paid_api_requests": 0, "llm_requests": 0, "estimated_usd": 0},
        "forbidden_actions": {"neon_writes": 0, "r2_writes": 0, "canonical_v2_table_access": 0}}
    write_new(PREFIX + "_manifest.json", result)
    print(dump({k: v for k, v in result.items() if k not in ("input_pins", "stage_a")}), flush=True)


def stratified_sample(rows, sizes, n=100):
    strata = defaultdict(list)
    for r in rows:
        exact, phash = r["exact_asset_pair_count"] > 0, r["identical_phash_pair_count"] > 0
        profile = "exact_and_identical_phash" if exact and phash else "exact_only" if exact else "identical_phash_only"
        strata[f"size_{sizes[r['candidate_id']]}|{profile}"].append(r["candidate_id"])
    if len(rows) < n or len(strata) > n:
        raise ValueError("Cannot satisfy declared sample size/stratum coverage")
    quotas = {s: 1 for s in strata}
    seats, population = n - len(strata), len(rows) - len(strata)
    fractions = {}
    for s, members in strata.items():
        numerator = seats * (len(members) - 1)
        q, remainder = divmod(numerator, population) if population else (0, 0)
        quotas[s] += q
        fractions[s] = remainder
    for s in sorted(strata, key=lambda k: (-fractions[k], k))[:n - sum(quotas.values())]:
        quotas[s] += 1
    rank = lambda phase, cid: hashlib.sha256((SEED + "|" + phase + "|" + cid).encode()).hexdigest()
    selected = []
    manifest = []
    for s in sorted(strata):
        ids = sorted(strata[s], key=lambda cid: (rank("select", cid), cid))[:quotas[s]]
        selected.extend(ids)
        manifest.append({"stratum": s, "population": len(strata[s]), "quota": quotas[s], "candidate_ids": ids})
    selected.sort(key=lambda cid: (rank("display", cid), cid))
    return selected, manifest


CDN_HOSTS = {"images.divisare.com", "architizer-prod.imgix.net"}


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme not in ("http", "https") or parts.hostname not in CDN_HOSTS:
            raise ValueError("Redirect outside original CDN allowlist")
        return Request(newurl, headers=dict(req.headers), method="HEAD")


def check_url(url):
    result = {"url": url, "method": "HEAD", "checked_at": now(), "accessible": False,
              "body_bytes_read": 0, "url_rewritten": False}
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname not in CDN_HOSTS or parts.username or parts.password:
        result["reason"] = "not_an_allowed_original_cdn_url"
        return result
    try:
        opener = build_opener(SafeRedirect())
        with opener.open(Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as response:
            result.update(http_status=response.status, content_type=response.headers.get("Content-Type", ""), final_url=response.url)
            result["accessible"] = 200 <= response.status < 300 and result["content_type"].lower().startswith("image/")
            result["reason"] = "accessible_original_cdn" if result["accessible"] else "non_image_or_http_failure"
    except HTTPError as exc:
        result.update(http_status=exc.code, reason="http_error", error=str(exc))
    except Exception as exc:
        result.update(reason="access_unverified", error=type(exc).__name__ + ": " + str(exc))
    return result


def stage_c():
    m = read(PREFIX + "_manifest.json")
    validation = read(PREFIX + "_stage_b_validation.json")
    if validation["status"] != "PASS" or validation["registry_sha256"] != m["registry"]["sha256"]:
        raise ValueError("Independent Stage B validation missing or unbound")
    check_pins(m["input_pins"] + [m["registry"]])
    with closing(ro(DB)) as db:
        rows = [dict(r) for r in db.execute("SELECT p.* FROM v4_candidate_pairs p JOIN candidate_edges e USING(candidate_id) WHERE e.final_tier=1 ORDER BY p.candidate_id")]
        sizes = {r[0]: r[1] for r in db.execute("SELECT e.candidate_id,c.size FROM candidate_edges e JOIN components c USING(component_id) WHERE e.final_tier=1")}
        selected, strata = stratified_sample(rows, sizes)
        buildings = {(r["source"], r["source_building_id"]): dict(r) for r in db.execute("SELECT * FROM v4_source_buildings")}
    by_id = {r["candidate_id"]: r for r in rows}
    image_info = {}
    with closing(ro(E2)) as e2:
        for cid in selected:
            for k in keys(by_id[cid]):
                if k in image_info:
                    continue
                # Exact original occurrence URLs, ordered by source cover/gallery position.
                values = e2.execute("""SELECT o.occurrence_url,o.source_asset_id,o.source_project_id,o.role,o.ordinal,
                    o.source_record_sha256,a.fingerprint_status
                    FROM source_project_buildings m JOIN project_asset_occurrences o
                    ON o.source=m.source AND o.source_project_id=m.source_project_id
                    JOIN assets a ON a.source=o.source AND a.source_asset_id=o.source_asset_id
                    WHERE m.source=? AND m.source_building_id=? AND o.resolution_status='linked'
                    ORDER BY CASE o.role WHEN 'cover' THEN 0 ELSE 1 END,o.ordinal,o.source_project_id,o.occurrence_id""", k).fetchall()
                candidates, seen = [], set()
                for r in values:
                    if r["source_asset_id"] in seen or not r["occurrence_url"]:
                        continue
                    seen.add(r["source_asset_id"])
                    candidates.append(dict(r))
                    if len(candidates) == 2:
                        break
                image_info[k] = candidates
    write_new(PREFIX + "_sample_selection.json", {"run_id": m["run_id"], "sample_version": SAMPLE_VERSION,
        "seed": SEED, "candidate_ids": selected, "strata": strata, "population": len(rows),
        "stage_b_validation": pin(PREFIX + "_stage_b_validation.json")})
    urls = sorted({r["occurrence_url"] for values in image_info.values() for r in values})
    # HEAD only: no downloaded image bodies, no credentials, no models.
    with ThreadPoolExecutor(max_workers=4) as pool:
        checked = list(pool.map(check_url, urls))
    health = {r["url"]: r for r in checked}
    pairs, side_records = [], []
    for cid in selected:
        r = by_id[cid]
        pair = {"id": cid}
        for side, k in zip(("left", "right"), keys(r)):
            b = buildings[k]
            original = image_info[k]
            ok = bool(original) and all(health[v["occurrence_url"]]["accessible"] for v in original)
            urls_side = [v["occurrence_url"] for v in original] if ok else []
            source_urls = json.loads(b["source_urls_json"])
            pair[side] = {"source": k[0], "name": b["name"], "architect": "; ".join(json.loads(b["firms_json"])),
                "year": b["completion_year_min"], "city": b["locality"] or "", "country": b["country"] or "",
                "url": source_urls[0] if source_urls else "", "images": urls_side}
            side_records.append({"candidate_id": cid, "side": side, "source_key": k, "selected_original_assets": original,
                "emitted_urls": urls_side, "reason": "original_urls_accessible" if ok else "side_empty_original_url_inaccessible_or_unverified"})
        pair["evidence"] = {"name_score": r["name_strict_score"], "firm_score": r["firm_strict_score"],
            "year_gap": r["year_gap"], "exact": r["exact_asset_pair_count"],
            "identical_phash": r["identical_phash_pair_count"], "component_size": sizes[cid]}
        pairs.append(pair)
    data = {"run_id": m["run_id"], "sample_version": SAMPLE_VERSION, "pairs": pairs}
    with (ROOT / JS).open("x", encoding="utf-8", newline="\n") as f:
        f.write("window.AUDIT_DATA = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n")
    write_new(PREFIX + "_sample_manifest.json", {"run_id": m["run_id"], "sample_version": SAMPLE_VERSION,
        "seed": SEED, "candidate_ids": selected, "strata": strata, "population": len(rows),
        "selection_manifest": pin(PREFIX + "_sample_selection.json"), "audit_data": pin(JS),
        "url_checks": checked, "sides": side_records, "empty_sides": sum(not s["emitted_urls"] for s in side_records),
        "human_labels": 0, "head_urls_checked": len(urls), "body_bytes_downloaded": 0,
        "human_audit_gate": read(PRE)["human_audit_gate"]})
    check_pins(m["input_pins"] + [m["registry"]])
    print(dump({"stage": "C", "status": "PACKET_CREATED_PENDING_VALIDATION", "pairs": len(pairs),
                "empty_sides": sum(not s["emitted_urls"] for s in side_records), "url_checks": len(urls)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("a", "b", "c"), required=True)
    args = parser.parse_args()
    if (ROOT / (PREFIX + "_FAIL.json")).exists():
        raise SystemExit("FAIL report exists; stopped. User decision required before another run.")
    try:
        {"a": stage_a, "b": stage_b, "c": stage_c}[args.stage]()
    except Exception as exc:
        failure = {"status": "FAIL", "stage": args.stage, "at": now(), "error": type(exc).__name__ + ": " + str(exc),
                   "action": "STOP; preserve artifacts; no automatic rollback; no next stage"}
        write_new(PREFIX + "_FAIL.json", failure)
        print(dump(failure), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
