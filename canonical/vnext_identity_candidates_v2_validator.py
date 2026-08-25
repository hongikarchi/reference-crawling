"""Independent validator for the WP2.1 country-normalized v2 artifact.

The validator reuses the independently implemented WP2 replay primitives, not
the v2 producer.  It overlays the known/unknown/ambiguous country policy,
replays every transition against the SHA-pinned v1 artifact, and verifies the
fresh disjoint N10 plus the existing Stage-B blind-review protocol.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from canonical import vnext_identity_candidates_validator as core
from canonical import vnext_country_norm as country
from tools.canonical_v2_c21_make_web_polish import _normalize_country_full as legacy_normalize_country
from tools.canonical_v2_c23_final import _KNOWN_COUNTRIES as LEGACY_KNOWN_COUNTRIES


VALIDATOR_VERSION = "vnext-identity-country-independent-validator-v2"
APPLICATION_ID = 0x564E5832
SCHEMA_VERSION = 2
BUILDER_VERSION = "vnext-identity-candidates-builder-v2"
NORMALIZER_VERSION = "vnext-identity-country-known-neutral-v2"
POLICY_VERSION = "vnext-identity-six-family-country-known-v2"
SAMPLE_VERSION = "vnext-identity-country-regression-fresh-n10-v2"
SAMPLE_SEED = "vnext-wp21-country-norm-fresh-n10-20260825"
FULL_LOGICAL_VERSION = "vnext-identity-stage-a-country-transition-logical-v2"

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_candidates_v2.db")
DEFAULT_V1 = Path("data/enrichment/vnext_identity_candidates_v1.db")
DEFAULT_REPORT = Path("data/reports/vnext_identity_candidates_v2_validation.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_candidates_v2_manifest.json")
DEFAULT_TRANSITION_REPORT = Path("data/reports/vnext_identity_candidates_v2_transition_report.json")
DEFAULT_GATE_REPORT = Path("data/reports/vnext_identity_candidates_v2_n10_gate.json")
DEFAULT_REVIEW_FORM = Path("data/reports/vnext_identity_country_n10_v2_retry1_review.json")

V1_SHA256 = "0541b4c86bb2def88bbadaf53eec33c62d8f14e62800f1ceb8d96761f3aea5c4"
TURKEY_TURKIYE_IDS = frozenset({
    "idc_da663eb7b113b017cd2024d2c23374aa",
    "idc_abc7bc46b59f16ee341a1cdd167e3f4a",
})
REPRESENTATION_REASONS = frozenset({
    "divisare_country_normalization_changed",
    "architizer_country_normalization_changed",
})
AMBIGUOUS_COUNTRY_KEYS = frozenset({"georgia"})
EXPECTED_STRATA = {
    "state_transition":2,
    "strong_merge":2,
    "metadata_only":2,
    "image_evidence_only":2,
    "conflict_boundary":2,
}
EXTRA_TABLES = frozenset({"country_normalizations","v1_review_exclusions","candidate_transitions"})
FULL_LOGICAL_TABLES = (
    ("country_normalizations","source,source_building_id"),
    ("v1_review_exclusions","candidate_id"),
    ("candidate_transitions","candidate_id"),
)

INPUT_SPECS: Mapping[str,tuple[Path,int,str]] = {
    "divisare_curated_metadata":core.INPUT_SPECS["divisare_curated_metadata"],
    "architizer_curated_metadata":core.INPUT_SPECS["architizer_curated_metadata"],
    "cross_source_e2_full_v5":core.INPUT_SPECS["cross_source_e2_full_v5"],
    "identity_contract":(
        Path("docs/vnext/CONTRACT_IDENTITY.md"),6_855,
        "119317831186368c078c121d810dda6f866ae5ab32df971093ad61b50aa788d2",
    ),
    "building_matcher":core.INPUT_SPECS["building_matcher"],
    "phash_matcher":core.INPUT_SPECS["phash_matcher"],
    "workspace_freeze_manifest":(
        Path("data/reports/vnext_workspace_freeze_v1.json"),246_838,
        "cd787a0088161fd0e8dddfe23e1943c0a41992f1e1a214b67fcce5d3ae17dcc3",
    ),
    "v1_candidate_artifact":(DEFAULT_V1,511_156_224,V1_SHA256),
    "v1_builder_code":(
        Path("canonical/vnext_identity_candidates.py"),75_798,
        "3997998b029fc1306ab5a632102af96594ef3034fa6ef44ef88543eb40179447",
    ),
    "country_normalizer_module":(
        Path("canonical/vnext_country_norm.py"),9_042,
        "003744a3caba4132f5a168fa4ae7459420662d7b8592434bbf3040dee2c80871",
    ),
    **{
        role:(path,size,digest)
        for role,(path,size,digest) in country.SOURCE_PROVENANCE.items()
    },
}
INPUT_USAGES = {
    "divisare_curated_metadata":"candidate_and_evidence",
    "architizer_curated_metadata":"candidate_and_evidence",
    "cross_source_e2_full_v5":"candidate_and_evidence",
    "identity_contract":"policy_contract",
    "building_matcher":"candidate_blocking",
    "phash_matcher":"image_policy_provenance",
    "workspace_freeze_manifest":"pin_manifest_only",
    "v1_candidate_artifact":"comparison_only_not_candidate_evidence",
    "v1_builder_code":"shared_stage_a_engine_provenance",
    "country_normalizer_module":"normalizer_implementation",
    **{role:"normalizer_legacy_provenance" for role in country.SOURCE_PROVENANCE},
}

NORMALIZER_SPEC = {
    **core.NORMALIZER_SPEC,
    "version":NORMALIZER_VERSION,
    "country_normalizer_version":country.NORMALIZER_VERSION,
    "country":country.COUNTRY_NORMALIZER_SPEC,
}
POLICY_SPEC = {
    **core.POLICY_SPEC,
    "version":POLICY_VERSION,
    "country_positive_rule":"both known canonical equal and exact locality",
    "country_hard_conflict_rule":"both known canonical and unequal",
    "country_unknown_ambiguous_rule":"neutral_no_positive_no_conflict",
    "ambiguous_country_keys":sorted(country.AMBIGUOUS_COUNTRY_KEYS),
    "v1_artifact_usage":"transition_comparison_only_not_candidate_evidence",
    "fresh_n10":{"exclude_v1_review_pairs":True,"state_transition_min":2,"seed":SAMPLE_SEED},
}


def _classify_country(value: Any) -> tuple[str,str|None,str]:
    raw="" if value is None else str(value).strip()
    if not raw:return "unknown",None,"empty"
    key=" ".join(unicodedata.normalize("NFC",raw).split()).casefold()
    if key in AMBIGUOUS_COUNTRY_KEYS:return "ambiguous",None,"fixed_ambiguous_key"
    canonical=legacy_normalize_country(raw)
    if canonical in LEGACY_KNOWN_COUNTRIES:return "known",str(canonical),"legacy_known_country"
    return "unknown",None,"not_in_pinned_known_country_set"


def _known_country(value: Any) -> str:
    return _classify_country(value)[1] or ""


def _transition_maps(connection: sqlite3.Connection, run_id: str) -> dict[str,Any]:
    candidates={str(row["candidate_id"]):dict(row) for row in connection.execute(
        "SELECT * FROM candidate_pairs WHERE run_id=?",(run_id,)
    )}
    origins:dict[str,set[str]]=defaultdict(set)
    for row in connection.execute("SELECT candidate_id,origin FROM candidate_origins WHERE run_id=?",(run_id,)):
        origins[str(row[0])].add(str(row[1]))
    locations={str(row[0]):str(row[1]) for row in connection.execute(
        "SELECT candidate_id,status FROM evidence_families WHERE run_id=? AND family='location_site_identity'",(run_id,)
    )}
    country_conflicts={str(row[0]) for row in connection.execute(
        "SELECT candidate_id FROM hard_conflicts WHERE run_id=? AND conflict_type='country_site_incompatible'",(run_id,)
    )}
    source_norm={(str(row[0]),str(row[1])):str(row[2] or "") for row in connection.execute(
        "SELECT source,source_building_id,country_normalized FROM source_buildings WHERE run_id=?",(run_id,)
    )}
    return {
        "candidates":candidates,"origins":origins,"locations":locations,
        "country_conflicts":country_conflicts,"source_norm":source_norm,
    }


def _expected_transitions(
    current: sqlite3.Connection,current_run_id: str,v1: sqlite3.Connection,v1_run_id: str,
) -> tuple[dict[str,dict[str,Any]],set[str]]:
    old_maps=_transition_maps(v1,v1_run_id);new_maps=_transition_maps(current,current_run_id)
    excluded={str(row[0]) for row in v1.execute(
        "SELECT candidate_id FROM sample_neighbourhoods WHERE run_id=?",(v1_run_id,)
    )}
    transitions:dict[str,dict[str,Any]]={}
    for candidate_id in sorted(set(old_maps["candidates"])|set(new_maps["candidates"])):
        old=old_maps["candidates"].get(candidate_id);new=new_maps["candidates"].get(candidate_id);source=old or new
        left_id=str(source["left_source_building_id"]);right_id=str(source["right_source_building_id"])
        old_origins=sorted(old_maps["origins"].get(candidate_id,set()))
        new_origins=sorted(new_maps["origins"].get(candidate_id,set()))
        old_location=old_maps["locations"].get(candidate_id);new_location=new_maps["locations"].get(candidate_id)
        old_conflict=int(candidate_id in old_maps["country_conflicts"])
        new_conflict=int(candidate_id in new_maps["country_conflicts"])
        old_proposal=None if old is None else str(old["proposal"])
        new_proposal=None if new is None else str(new["proposal"])
        old_auto=None if old is None else int(old["auto_merge_eligible"])
        new_auto=None if new is None else int(new["auto_merge_eligible"])
        reasons=[]
        if old is None:reasons.append("candidate_added")
        if new is None:reasons.append("candidate_removed")
        if old_origins!=new_origins:reasons.append("origins_changed")
        if old_location!=new_location:reasons.append("location_family_status_changed")
        if old_conflict!=new_conflict:reasons.append("country_hard_conflict_changed")
        if old_proposal!=new_proposal:reasons.append("proposal_changed")
        if old_auto!=new_auto:reasons.append("auto_merge_eligibility_changed")
        for endpoint in (("divisare",left_id),("architizer",right_id)):
            if old_maps["source_norm"].get(endpoint,"")!=new_maps["source_norm"].get(endpoint,""):
                reasons.append(endpoint[0]+"_country_normalization_changed")
        transitions[candidate_id]={
            "candidate_id":candidate_id,"left_source_building_id":left_id,"right_source_building_id":right_id,
            "v1_present":int(old is not None),"v2_present":int(new is not None),
            "v1_origins":old_origins,"v2_origins":new_origins,
            "v1_location_status":old_location,"v2_location_status":new_location,
            "v1_country_conflict":old_conflict,"v2_country_conflict":new_conflict,
            "v1_proposal":old_proposal,"v2_proposal":new_proposal,
            "v1_auto_merge_eligible":old_auto,"v2_auto_merge_eligible":new_auto,
            "changed":int(bool(reasons)),"reasons":reasons,
        }
    return transitions,excluded


def _select_fresh_n10(
    evaluated: Mapping[str,Mapping[str,Any]],transitions: Mapping[str,Mapping[str,Any]],excluded: set[str],
) -> list[tuple[str,str]]:
    selected:list[tuple[str,Mapping[str,Any]]]=[];used_left:set[str]=set();used_right:set[str]=set()
    def add(stratum: str,count: int,predicate: Any,preference: Any) -> None:
        pool=[row for row in evaluated.values() if row["candidate_id"] not in excluded and predicate(row)]
        pool.sort(key=lambda row:(preference(row),core._stable(SAMPLE_SEED,stratum,str(row["candidate_id"]))))
        for row in pool:
            if row["left_id"] in used_left or row["right_id"] in used_right:continue
            selected.append((stratum,row));used_left.add(str(row["left_id"]));used_right.add(str(row["right_id"]))
            if sum(item[0]==stratum for item in selected)==count:return
        raise ValueError("fresh N10 stratum is insufficient: "+stratum)
    def transition(row: Mapping[str,Any]) -> Mapping[str,Any]:
        return transitions.get(str(row["candidate_id"]),{})
    add("state_transition",2,lambda row:transition(row).get("v1_proposal")!=transition(row).get("v2_proposal"),
        lambda row:(0 if transition(row).get("v1_proposal")=="reject" and transition(row).get("v2_proposal")=="merge_candidate" else 1,-int(row["positive"])))
    add("strong_merge",2,lambda row:row["proposal"]=="merge_candidate" and row["strong"]>0 and row["name_strict"]>=90,
        lambda row:(-int(row["positive"]),-int(row["strong"])))
    add("metadata_only",2,lambda row:"e2_metadata" in row["origins"] and row["strong"]==0,
        lambda row:(-int(row["positive"]),len(row["conflicts"])))
    add("image_evidence_only",2,lambda row:"e2_candidate" in row["origins"] and "e2_metadata" not in row["origins"] and row["strong"]>0,
        lambda row:(int("matcher_architect_year" in row["origins"] or "matcher_pass2" in row["origins"]),-int(row["strong"])))
    add("conflict_boundary",2,lambda row:bool(row["conflicts"]) or (row["proposal"]=="defer" and row["positive"]==1),
        lambda row:(0 if row["conflicts"] else 1,len(row["conflicts"]),-int(row["positive"])))
    return [(stratum,str(row["candidate_id"])) for stratum,row in selected]


def _full_logical(connection: sqlite3.Connection,run_id: str,stage_a_logical: str) -> str:
    digest=hashlib.sha256();digest.update((FULL_LOGICAL_VERSION+"\n"+stage_a_logical+"\n").encode())
    for table,order in FULL_LOGICAL_TABLES:
        digest.update((table+"\n").encode())
        for row in connection.execute(f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}",(run_id,)):
            digest.update((str(row[0])+"\n").encode("ascii"))
    return digest.hexdigest()


@contextmanager
def _v2_core_profile(
    artifact: Path,v1: Path,
) -> Iterator[None]:
    names=(
        "SCHEMA_VERSION","BUILDER_VERSION","NORMALIZER_VERSION","POLICY_VERSION","SAMPLE_VERSION","SAMPLE_SEED",
        "INPUT_SPECS","REQUIRED_TABLES","NORMALIZER_SPEC","POLICY_SPEC","DEFAULT_GATE_REPORT","_country","_expected_sample",
    )
    previous={name:getattr(core,name) for name in names}
    def expected_sample(evaluated: Mapping[str,Mapping[str,Any]]) -> list[tuple[str,str]]:
        current=core._open(artifact);old=core._open(v1)
        try:
            current_run=str(current.execute("SELECT run_id FROM runs").fetchone()[0])
            old_run=str(old.execute("SELECT run_id FROM runs").fetchone()[0])
            transitions,excluded=_expected_transitions(current,current_run,old,old_run)
            return _select_fresh_n10(evaluated,transitions,excluded)
        finally:
            current.close();old.close()
    replacements={
        "SCHEMA_VERSION":SCHEMA_VERSION,"BUILDER_VERSION":BUILDER_VERSION,
        "NORMALIZER_VERSION":NORMALIZER_VERSION,"POLICY_VERSION":POLICY_VERSION,
        "SAMPLE_VERSION":SAMPLE_VERSION,"SAMPLE_SEED":SAMPLE_SEED,"INPUT_SPECS":INPUT_SPECS,
        "REQUIRED_TABLES":core.REQUIRED_TABLES|EXTRA_TABLES,"NORMALIZER_SPEC":NORMALIZER_SPEC,
        "POLICY_SPEC":POLICY_SPEC,"DEFAULT_GATE_REPORT":DEFAULT_GATE_REPORT,
        "_country":_known_country,"_expected_sample":expected_sample,
    }
    for name,value in replacements.items():setattr(core,name,value)
    try:
        yield
    finally:
        for name,value in previous.items():setattr(core,name,value)


def _write_report(result: core.ValidationResult,report: Path) -> None:
    if report.exists():raise FileExistsError(f"refusing to clobber validation report: {report}")
    payload={
        "status":"PASS" if result.passed else "FAIL","validator_version":VALIDATOR_VERSION,
        "artifact_path":str(result.artifact_path),"run_id":result.run_id,
        "passed_checks":sum(row.passed for row in result.checks),"total_checks":len(result.checks),
        "failed_checks":list(result.failed_check_names),
        "checks":[{
            "name":row.name,"passed":row.passed,"expected":core._json_safe(row.expected),
            "actual":core._json_safe(row.actual),"detail":row.detail,
        } for row in result.checks],
        "metrics":core._json_safe(result.metrics),
        "validated_at":datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z"),
    }
    report.parent.mkdir(parents=True,exist_ok=True);temporary=report.with_name(report.name+".building")
    temporary.write_text(json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    temporary.replace(report)


def validate_identity_candidates_v2(
    *,repo_root: Path,artifact_path: Path=DEFAULT_ARTIFACT,report_path: Path|None=DEFAULT_REPORT,
    verify_full_hashes: bool=True,
) -> core.ValidationResult:
    root=repo_root.resolve();artifact=artifact_path if artifact_path.is_absolute() else root/artifact_path
    report=None if report_path is None else (report_path if report_path.is_absolute() else root/report_path)
    v1=root/DEFAULT_V1
    with _v2_core_profile(artifact,v1):
        base_result=core.validate_identity_candidates(
            repo_root=root,artifact_path=artifact,report_path=None,verify_full_hashes=verify_full_hashes,
        )
    checks=[row for row in base_result.checks if row.name!="n10_strata"]
    metrics=dict(base_result.metrics)
    if not artifact.is_file():
        result=core.ValidationResult(artifact,report,None,tuple(checks),metrics)
        if report is not None:_write_report(result,report)
        return result
    connection=core._open(artifact);old=core._open(v1);divisare=core._open(root/core.DIVISARE);architizer=core._open(root/core.ARCHITIZER);e2=core._open(root/core.E2)
    try:
        run=connection.execute("SELECT * FROM runs").fetchone();run_id=str(run["run_id"])
        old_run=str(old.execute("SELECT run_id FROM runs").fetchone()[0])
        core._check(checks,"contract_version","vnext-identity-contract-wp21-v2",str(run["contract_version"]))
        core._check(checks,"foreign_key_check",[],[tuple(row) for row in connection.execute("PRAGMA foreign_key_check")])
        sidecars=[]
        for relative,_size,_digest in INPUT_SPECS.values():
            if relative.suffix!=".db":continue
            for suffix in ("-wal","-shm","-journal"):
                candidate=Path(str(root/relative)+suffix)
                if candidate.exists():sidecars.append(str(candidate.relative_to(root)))
        core._check(checks,"stage_a_inputs_no_sqlite_sidecars",[],sidecars)
        stored_pins={str(row["role"]):row for row in connection.execute("SELECT * FROM input_pins WHERE run_id=?",(run_id,))}
        pin_record_failures=[]
        if set(stored_pins)!=set(INPUT_SPECS):pin_record_failures.append("role_set")
        for role,(relative,size,digest) in INPUT_SPECS.items():
            row=stored_pins.get(role);usage=INPUT_USAGES[role]
            expected=(relative.as_posix(),size,digest,digest,usage)
            actual=None if row is None else tuple(row[column] for column in ("path","size_bytes","sha256_before","sha256_after","usage"))
            payload={"role":role,"path":relative.as_posix(),"size_bytes":size,"sha256_before":digest,"sha256_after":digest,"usage":usage}
            if actual!=expected or (row is not None and str(row["record_sha256"])!=core._sha(payload)):
                pin_record_failures.append(role)
        core._check(checks,"input_pin_roles_usage_and_records",[],pin_record_failures)
        buildings=core._load_buildings(divisare,architizer,e2)
        normalizer_equivalence_failures=[];classification_failures=[]
        for building in buildings.values():
            status,canonical,reason=_classify_country(building["country"])
            if country.normalize_country_full(building["country"])!=legacy_normalize_country(building["country"]):
                if len(normalizer_equivalence_failures)<10:normalizer_equivalence_failures.append(str(building["country"]))
            production=country.classify_country(building["country"])
            if (production.status,production.canonical,production.reason)!=(status,canonical,reason):
                if len(classification_failures)<10:classification_failures.append(str(building["country"]))
            building["country_norm"]=canonical or ""
            building["country_normalization_status"]=status
            building["country_normalization_reason"]=reason
        core._check(checks,"c21_normalizer_distinct_value_equivalence",[],normalizer_equivalence_failures)
        core._check(checks,"country_classifier_independent_equivalence",[],classification_failures)
        core._check(checks,"c23_known_country_set_equivalence",set(LEGACY_KNOWN_COUNTRIES),set(country.KNOWN_COUNTRIES))
        core._check(checks,"ambiguous_country_overlay",AMBIGUOUS_COUNTRY_KEYS,set(country.AMBIGUOUS_COUNTRY_KEYS))

        stored_country={(str(row["source"]),str(row["source_building_id"])):row for row in connection.execute(
            "SELECT * FROM country_normalizations WHERE run_id=?",(run_id,)
        )}
        country_failures=[];source_country_failures=[]
        stored_sources={(str(row["source"]),str(row["source_building_id"])):row for row in connection.execute(
            "SELECT source,source_building_id,country_normalized FROM source_buildings WHERE run_id=?",(run_id,)
        )}
        for key,building in buildings.items():
            row=stored_country.get(key)
            payload={
                "source":key[0],"source_building_id":key[1],"raw_country":str(building["country"] or ""),
                "normalization_status":building["country_normalization_status"],
                "canonical_country":building["country_norm"] or None,"reason":building["country_normalization_reason"],
            }
            actual=None if row is None else (
                str(row["raw_country"]),str(row["normalization_status"]),row["canonical_country"],
                str(row["reason"]),str(row["record_sha256"]),
            )
            expected=(payload["raw_country"],payload["normalization_status"],payload["canonical_country"],payload["reason"],core._sha(payload))
            if actual!=expected and len(country_failures)<10:country_failures.append(f"{key[0]}/{key[1]}")
            source_row=stored_sources.get(key)
            if source_row is None or str(source_row["country_normalized"] or "")!=str(building["country_norm"]):
                if len(source_country_failures)<10:source_country_failures.append(f"{key[0]}/{key[1]}")
        core._check(checks,"country_normalization_rows_replay",[],country_failures)
        core._check(checks,"source_country_known_only_replay",[],source_country_failures)
        status_counts=dict(sorted(Counter(str(row["normalization_status"]) for row in stored_country.values()).items()))
        core._check(checks,"country_status_partition",len(buildings),sum(status_counts.values()))

        stored_location={str(row[0]):str(row[1]) for row in connection.execute(
            "SELECT candidate_id,status FROM evidence_families WHERE run_id=? AND family='location_site_identity'",(run_id,)
        )}
        stored_country_conflicts={str(row[0]) for row in connection.execute(
            "SELECT candidate_id FROM hard_conflicts WHERE run_id=? AND conflict_type='country_site_incompatible'",(run_id,)
        )}
        country_rule_failures=[];location_rule_failures=[]
        for row in connection.execute(
            "SELECT candidate_id,left_source_building_id,right_source_building_id FROM candidate_pairs WHERE run_id=?",(run_id,)
        ):
            candidate_id=str(row["candidate_id"]);left=buildings[("divisare",str(row["left_source_building_id"]))]
            right=buildings[("architizer",str(row["right_source_building_id"]))]
            both_known=left["country_normalization_status"]==right["country_normalization_status"]=="known"
            conflict_expected=bool(both_known and left["country_norm"]!=right["country_norm"])
            positive_expected=bool(
                both_known and left["country_norm"]==right["country_norm"] and left["locality_norm"]
                and left["locality_norm"]==right["locality_norm"]
            )
            conflict_actual=candidate_id in stored_country_conflicts
            location_actual=stored_location.get(candidate_id)
            if conflict_actual!=conflict_expected and len(country_rule_failures)<10:country_rule_failures.append(candidate_id)
            expected_status="negative" if conflict_expected else ("positive" if positive_expected else "unknown")
            if location_actual!=expected_status and len(location_rule_failures)<10:location_rule_failures.append(candidate_id)
        core._check(checks,"country_conflict_both_known_different_only",[],country_rule_failures)
        core._check(checks,"location_family_known_equal_locality_only",[],location_rule_failures)

        transitions,excluded=_expected_transitions(connection,run_id,old,old_run)
        stored_transitions={str(row["candidate_id"]):row for row in connection.execute(
            "SELECT * FROM candidate_transitions WHERE run_id=?",(run_id,)
        )}
        transition_failures=[]
        for candidate_id,payload in transitions.items():
            row=stored_transitions.get(candidate_id)
            expected=(
                payload["left_source_building_id"],payload["right_source_building_id"],payload["v1_present"],payload["v2_present"],
                core._json(payload["v1_origins"]),core._json(payload["v2_origins"]),payload["v1_location_status"],payload["v2_location_status"],
                payload["v1_country_conflict"],payload["v2_country_conflict"],payload["v1_proposal"],payload["v2_proposal"],
                payload["v1_auto_merge_eligible"],payload["v2_auto_merge_eligible"],payload["changed"],core._json(payload["reasons"]),core._sha(payload),
            )
            actual=None if row is None else tuple(row[column] for column in (
                "left_source_building_id","right_source_building_id","v1_present","v2_present","v1_origins_json","v2_origins_json",
                "v1_location_status","v2_location_status","v1_country_conflict","v2_country_conflict","v1_proposal","v2_proposal",
                "v1_auto_merge_eligible","v2_auto_merge_eligible","changed","reasons_json","record_sha256",
            ))
            if actual!=expected and len(transition_failures)<10:transition_failures.append(candidate_id)
        core._check(checks,"candidate_transition_full_replay",set(transitions),set(stored_transitions))
        core._check(checks,"candidate_transition_rows_replay",[],transition_failures)

        expected_exclusions={str(row[0]):str(row[1]) for row in old.execute(
            "SELECT candidate_id,opaque_review_id FROM sample_neighbourhoods WHERE run_id=?",(old_run,)
        )}
        actual_exclusions={str(row[0]):str(row[1]) for row in connection.execute(
            "SELECT candidate_id,v1_opaque_review_id FROM v1_review_exclusions WHERE run_id=?",(run_id,)
        )}
        core._check(checks,"v1_review_exclusions_exact",expected_exclusions,actual_exclusions)
        exclusion_record_failures=[]
        for row in connection.execute("SELECT * FROM v1_review_exclusions WHERE run_id=?",(run_id,)):
            payload={"candidate_id":row["candidate_id"],"v1_opaque_review_id":row["v1_opaque_review_id"]}
            if str(row["record_sha256"])!=core._sha(payload):exclusion_record_failures.append(str(row["candidate_id"]))
        core._check(checks,"v1_review_exclusion_records",[],exclusion_record_failures)

        proposal_rows=[row for row in transitions.values() if row["v1_proposal"]!=row["v2_proposal"]]
        transition_counts=dict(sorted(Counter(
            f"{row['v1_proposal'] or 'absent'}->{row['v2_proposal'] or 'absent'}" for row in transitions.values()
        ).items()))
        reject_merge={row["candidate_id"] for row in proposal_rows if row["v1_proposal"]=="reject" and row["v2_proposal"]=="merge_candidate"}
        reject_defer={row["candidate_id"] for row in proposal_rows if row["v1_proposal"]=="reject" and row["v2_proposal"]=="defer"}
        decision_lineage=sum(bool(set(row["reasons"])-REPRESENTATION_REASONS) for row in transitions.values())
        representation_only=sum(bool(row["reasons"]) and set(row["reasons"])<=REPRESENTATION_REASONS for row in transitions.values())
        core._check(checks,"proposal_state_transition_count",10,len(proposal_rows))
        core._check(checks,"reject_to_merge_candidate_count",8,len(reject_merge))
        core._check(checks,"reject_to_defer_count",2,len(reject_defer))
        turkey_actual={candidate_id for candidate_id in TURKEY_TURKIYE_IDS if candidate_id in reject_merge and transitions[candidate_id]["v1_country_conflict"]==1 and transitions[candidate_id]["v2_country_conflict"]==0}
        core._check(checks,"turkey_turkiye_two_pair_regression",TURKEY_TURKIYE_IDS,turkey_actual)

        actual_sample=[(str(row["stratum"]),str(row["candidate_id"])) for row in connection.execute(
            "SELECT stratum,candidate_id FROM sample_neighbourhoods WHERE run_id=? ORDER BY sample_rank",(run_id,)
        )]
        strata=dict(sorted(Counter(row[0] for row in actual_sample).items()))
        core._check(checks,"fresh_n10_strata",EXPECTED_STRATA,strata)
        core._check(checks,"fresh_n10_disjoint_from_v1",set(),{candidate_id for _stratum,candidate_id in actual_sample}&excluded)
        core._check(checks,"fresh_n10_state_transition_min",True,sum(
            transitions[candidate_id]["v1_proposal"]!=transitions[candidate_id]["v2_proposal"] for _stratum,candidate_id in actual_sample
        )>=2)

        stage_a_logical=core._logical(connection,run_id)
        full_logical=_full_logical(connection,run_id,stage_a_logical)
        manifest_path=root/DEFAULT_MANIFEST;manifest_failures=[]
        try:manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError):manifest=None
        if not isinstance(manifest,dict):manifest_failures.append("missing_or_invalid")
        else:
            if str(manifest.get("run_id"))!=run_id:manifest_failures.append("run_id")
            if str(manifest.get("stage_a_logical_sha256"))!=stage_a_logical:manifest_failures.append("stage_a_logical")
            if str(manifest.get("full_logical_sha256"))!=full_logical:manifest_failures.append("full_logical")
        core._check(checks,"v2_manifest_logical_lineage",[],manifest_failures)

        transition_report_path=root/DEFAULT_TRANSITION_REPORT;transition_report_failures=[]
        try:transition_report=json.loads(transition_report_path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError):transition_report=None
        if not isinstance(transition_report,dict):transition_report_failures.append("missing_or_invalid")
        else:
            report_tuple=(
                str(transition_report.get("run_id")),int(transition_report.get("candidate_count",-1)),
                int(transition_report.get("proposal_state_transition_count",-1)),
                int(transition_report.get("decision_lineage_change_count",-1)),
                int(transition_report.get("normalization_representation_only_count",-1)),
                transition_report.get("transition_counts"),
                {str(row["candidate_id"]) for row in transition_report.get("reject_to_merge_candidate",[])},
                {str(row["candidate_id"]) for row in transition_report.get("reject_to_defer",[])},
            )
            expected_tuple=(run_id,len(transitions),len(proposal_rows),decision_lineage,representation_only,transition_counts,reject_merge,reject_defer)
            if report_tuple!=expected_tuple:transition_report_failures.append("content")
            stage_b_manifest=connection.execute("SELECT stage_a_artifact_sha256_before FROM stage_b_manifest WHERE run_id=?",(run_id,)).fetchone()
            if stage_b_manifest is not None and str(transition_report.get("artifact_sha256"))!=str(stage_b_manifest[0]):
                transition_report_failures.append("stage_a_artifact_sha")
        core._check(checks,"compact_transition_report_replay",[],transition_report_failures)

        retry_attempts=connection.execute(
            "SELECT * FROM stage_b_retry_attempts WHERE run_id=? ORDER BY attempt_no",(run_id,)
        ).fetchall();retry_failures=[]
        for attempt in retry_attempts:
            statuses=json.loads(str(attempt["asset_status_counts_json"]));error_kinds=json.loads(str(attempt["error_kind_counts_json"]))
            payload={"attempt_no":attempt["attempt_no"],"retry_reason":attempt["retry_reason"],
                "asset_status_counts":statuses,"error_kind_counts":error_kinds,"http_request_count":attempt["http_request_count"],
                "review_sheet_count":attempt["review_sheet_count"],"archived_at":attempt["archived_at"]}
            if str(attempt["record_sha256"])!=core._sha(payload):retry_failures.append(f"attempt:{attempt['attempt_no']}")
        asset_snapshots=connection.execute(
            "SELECT * FROM stage_b_retry_asset_snapshots WHERE run_id=? ORDER BY attempt_no,opaque_review_id,side,asset_order",(run_id,)
        ).fetchall()
        for row in asset_snapshots:
            snapshot=json.loads(str(row["snapshot_json"]));payload={"attempt_no":row["attempt_no"],
                "opaque_review_id":row["opaque_review_id"],"side":row["side"],"asset_order":row["asset_order"],"snapshot":snapshot}
            if str(row["record_sha256"])!=core._sha(payload):retry_failures.append(f"asset:{row['attempt_no']}/{row['opaque_review_id']}")
        sheet_snapshots=connection.execute(
            "SELECT * FROM stage_b_retry_sheet_snapshots WHERE run_id=? ORDER BY attempt_no,opaque_review_id",(run_id,)
        ).fetchall()
        for row in sheet_snapshots:
            snapshot=json.loads(str(row["snapshot_json"]));payload={"attempt_no":row["attempt_no"],
                "opaque_review_id":row["opaque_review_id"],"snapshot":snapshot}
            sheet_path=root/str(snapshot["sheet_path"])
            if str(row["record_sha256"])!=core._sha(payload) or not sheet_path.is_file() or core._sha_file(sheet_path)!=str(snapshot["sheet_sha256"]):
                retry_failures.append(f"sheet:{row['attempt_no']}/{row['opaque_review_id']}")
        retry_actual=(len(retry_attempts),len(asset_snapshots),len(sheet_snapshots),
            dict(json.loads(str(retry_attempts[0]["asset_status_counts_json"]))) if retry_attempts else {},
            dict(json.loads(str(retry_attempts[0]["error_kind_counts_json"]))) if retry_attempts else {})
        core._check(checks,"stage_b_connection_retry_accounting",(1,36,10,{"failed":36},{"connection":36}),retry_actual)
        core._check(checks,"stage_b_retry_ledger_records",[],retry_failures)

        review_form_failures=[]
        try:review_form=json.loads((root/DEFAULT_REVIEW_FORM).read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError):review_form=None
        forbidden_fields={"candidate_id","sample_rank","stratum","origin","scores","proposal","auto_merge_eligible"}
        if not isinstance(review_form,dict):review_form_failures.append("missing_or_invalid")
        else:
            if forbidden_fields&set(review_form):review_form_failures.append("top_level_forbidden")
            if any(forbidden_fields&set(item) for item in review_form.get("items",[])):review_form_failures.append("item_forbidden")
            if len(review_form.get("items",[]))!=10:review_form_failures.append("item_count")
        core._check(checks,"blind_review_form_hides_candidate_state",[],review_form_failures)

        metrics.update(
            country_normalization_status_counts=status_counts,proposal_state_transitions=len(proposal_rows),
            decision_lineage_changes=decision_lineage,normalization_representation_only=representation_only,
            transition_counts=transition_counts,reject_to_merge_candidate=len(reject_merge),reject_to_defer=len(reject_defer),
            fresh_n10_strata=strata,n100_status="not_run_waiting_for_user_issued_design",
        )
        n100_metrics=int(connection.execute(
            "SELECT coalesce(sum(metric_value),0) FROM metrics WHERE run_id=? AND metric_name='n100_runs'",(run_id,)
        ).fetchone()[0])
        core._check(checks,"n100_not_run",0,n100_metrics)
        core._check(checks,"v1_artifact_sha256",V1_SHA256,core._sha_file(v1) if verify_full_hashes else V1_SHA256)
    finally:
        connection.close();old.close();divisare.close();architizer.close();e2.close()
    result=core.ValidationResult(artifact,report,base_result.run_id,tuple(checks),metrics)
    if report is not None:_write_report(result,report)
    return result


__all__=["DEFAULT_ARTIFACT","DEFAULT_REPORT","VALIDATOR_VERSION","validate_identity_candidates_v2"]
