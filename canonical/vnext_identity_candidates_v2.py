"""WP2.1 country-normalized identity candidate replay and fresh N10 freeze."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from canonical import vnext_identity_candidates as base
from canonical.vnext_country_norm import (
    AMBIGUOUS_COUNTRY_KEYS,
    COUNTRY_NORMALIZER_SPEC,
    NORMALIZER_VERSION as COUNTRY_NORMALIZER_VERSION,
    SOURCE_PROVENANCE,
    classify_country,
)


CONTRACT_VERSION = "vnext-identity-contract-wp21-v2"
BUILDER_VERSION = "vnext-identity-candidates-builder-v2"
NORMALIZER_VERSION = "vnext-identity-country-known-neutral-v2"
POLICY_VERSION = "vnext-identity-six-family-country-known-v2"
SAMPLE_VERSION = "vnext-identity-country-regression-fresh-n10-v2"
SAMPLE_SEED = "vnext-wp21-country-norm-fresh-n10-20260825"
FULL_LOGICAL_VERSION = "vnext-identity-stage-a-country-transition-logical-v2"
APPLICATION_ID = base.APPLICATION_ID
SCHEMA_VERSION = 2

DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_candidates_v2.db")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_candidates_v2_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_candidates_v2_stage_a_report.json")
DEFAULT_V1 = Path("data/enrichment/vnext_identity_candidates_v1.db")

V1_TURKEY_REGRESSION_IDS = (
    "idc_da663eb7b113b017cd2024d2c23374aa",
    "idc_abc7bc46b59f16ee341a1cdd167e3f4a",
)

INPUT_PINS: Mapping[str, tuple[Path, int, str, str]] = {
    "divisare_curated_metadata": (*base.INPUT_SPECS["divisare_curated_metadata"], "candidate_and_evidence"),
    "architizer_curated_metadata": (*base.INPUT_SPECS["architizer_curated_metadata"], "candidate_and_evidence"),
    "cross_source_e2_full_v5": (*base.INPUT_SPECS["cross_source_e2_full_v5"], "candidate_and_evidence"),
    "identity_contract": (
        base.DEFAULT_CONTRACT, 6_855,
        "119317831186368c078c121d810dda6f866ae5ab32df971093ad61b50aa788d2",
        "policy_contract",
    ),
    "building_matcher": (*base.FILE_PINS["building_matcher"], "candidate_blocking"),
    "phash_matcher": (*base.FILE_PINS["phash_matcher"], "image_policy_provenance"),
    "workspace_freeze_manifest": (
        base.DEFAULT_FREEZE, 246_838,
        "cd787a0088161fd0e8dddfe23e1943c0a41992f1e1a214b67fcce5d3ae17dcc3",
        "pin_manifest_only",
    ),
    "v1_candidate_artifact": (
        DEFAULT_V1, 511_156_224,
        "0541b4c86bb2def88bbadaf53eec33c62d8f14e62800f1ceb8d96761f3aea5c4",
        "comparison_only_not_candidate_evidence",
    ),
    "v1_builder_code": (
        Path("canonical/vnext_identity_candidates.py"), 75_798,
        "3997998b029fc1306ab5a632102af96594ef3034fa6ef44ef88543eb40179447",
        "shared_stage_a_engine_provenance",
    ),
    "country_normalizer_module": (
        Path("canonical/vnext_country_norm.py"), 9_042,
        "003744a3caba4132f5a168fa4ae7459420662d7b8592434bbf3040dee2c80871",
        "normalizer_implementation",
    ),
    **{
        role: (path,size,digest,"normalizer_legacy_provenance")
        for role,(path,size,digest) in SOURCE_PROVENANCE.items()
    },
}

NORMALIZER_SPEC = {
    **base.NORMALIZER_SPEC,
    "version": NORMALIZER_VERSION,
    "country_normalizer_version": COUNTRY_NORMALIZER_VERSION,
    "country": COUNTRY_NORMALIZER_SPEC,
}
POLICY_SPEC = {
    **base.POLICY_SPEC,
    "version": POLICY_VERSION,
    "country_positive_rule": "both known canonical equal and exact locality",
    "country_hard_conflict_rule": "both known canonical and unequal",
    "country_unknown_ambiguous_rule": "neutral_no_positive_no_conflict",
    "ambiguous_country_keys": sorted(AMBIGUOUS_COUNTRY_KEYS),
    "v1_artifact_usage": "transition_comparison_only_not_candidate_evidence",
    "fresh_n10": {"exclude_v1_review_pairs":True,"state_transition_min":2,"seed":SAMPLE_SEED},
}

V2_SCHEMA_SQL = """
CREATE TABLE country_normalizations(
  run_id TEXT NOT NULL REFERENCES runs(run_id), source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, raw_country TEXT NOT NULL,
  normalization_status TEXT NOT NULL CHECK(normalization_status IN ('known','unknown','ambiguous')),
  canonical_country TEXT, reason TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id),
  FOREIGN KEY(run_id,source,source_building_id) REFERENCES source_buildings(run_id,source,source_building_id)
);
CREATE TABLE v1_review_exclusions(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  v1_opaque_review_id TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id)
);
CREATE TABLE candidate_transitions(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  left_source_building_id TEXT NOT NULL, right_source_building_id TEXT NOT NULL,
  v1_present INTEGER NOT NULL CHECK(v1_present IN (0,1)),
  v2_present INTEGER NOT NULL CHECK(v2_present IN (0,1)),
  v1_origins_json TEXT NOT NULL, v2_origins_json TEXT NOT NULL,
  v1_location_status TEXT, v2_location_status TEXT,
  v1_country_conflict INTEGER NOT NULL CHECK(v1_country_conflict IN (0,1)),
  v2_country_conflict INTEGER NOT NULL CHECK(v2_country_conflict IN (0,1)),
  v1_proposal TEXT, v2_proposal TEXT,
  v1_auto_merge_eligible INTEGER, v2_auto_merge_eligible INTEGER,
  changed INTEGER NOT NULL CHECK(changed IN (0,1)),
  reasons_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id)
);
"""

_V1_STRATUM_CHECK = "CHECK(stratum IN ('strong_merge','metadata_only','image_evidence_only','conflict_boundary'))"
_V2_STRATUM_CHECK = "CHECK(stratum IN ('state_transition','strong_merge','metadata_only','image_evidence_only','conflict_boundary'))"
V2_BASE_SCHEMA_SQL = base.SCHEMA_SQL.replace(_V1_STRATUM_CHECK,_V2_STRATUM_CHECK)
if V2_BASE_SCHEMA_SQL == base.SCHEMA_SQL:
    raise RuntimeError("v1 sample stratum CHECK lineage changed")

FULL_LOGICAL_TABLES = (
    ("country_normalizations","source,source_building_id"),
    ("v1_review_exclusions","candidate_id"),
    ("candidate_transitions","candidate_id"),
)

COUNTRY_REPRESENTATION_REASONS = frozenset({
    "divisare_country_normalization_changed",
    "architizer_country_normalization_changed",
})


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    manifest_path: Path
    report_path: Path
    run_id: str
    logical_sha256: str
    full_logical_sha256: str
    candidate_count: int
    auto_merge_eligible_count: int
    changed_transition_count: int
    sample_count: int
    elapsed_seconds: float


class IdentityCandidateV2Error(RuntimeError):
    pass


def _verify_pins(root:Path,full_hashes:bool)->dict[str,dict[str,Any]]:
    pins={}
    for role,(relative,size,expected,usage) in INPUT_PINS.items():
        path=root/relative
        if not path.is_file() or path.stat().st_size!=size:
            raise IdentityCandidateV2Error(f"input size/missing mismatch: {role}")
        if path.suffix==".db":base._assert_no_sidecars(path)
        digest=base.sha256_file(path) if full_hashes else expected
        if digest!=expected:raise IdentityCandidateV2Error(f"input SHA mismatch: {role}")
        pins[role]={"path":relative.as_posix(),"size_bytes":size,"sha256_before":digest,"sha256_after":None,"usage":usage}
    return pins


def _apply_country_normalization(buildings:Mapping[tuple[str,str],dict[str,Any]])->None:
    for building in buildings.values():
        result=classify_country(building["country"])
        building["country_norm"]=result.canonical or ""
        building["country_normalization_status"]=result.status
        building["country_normalization_reason"]=result.reason


def evaluate_candidate(
    left:Mapping[str,Any],right:Mapping[str,Any],origins:set[str],e2_row:Mapping[str,Any],
    left_stats:Mapping[str,Any],right_stats:Mapping[str,Any],
)->dict[str,Any]:
    result=base.evaluate_candidate(left,right,origins,e2_row,left_stats,right_stats)
    location=next(row for row in result["families"] if row["family"]=="location_site_identity")
    location["detail"].update({
        "left_country_status":left["country_normalization_status"],
        "right_country_status":right["country_normalization_status"],
        "left_country_canonical":left["country_norm"] or None,
        "right_country_canonical":right["country_norm"] or None,
        "rule":"both-known-canonical; equal+exact-locality positive; unequal conflict; otherwise neutral",
    })
    for conflict in result["conflicts"]:
        if conflict["conflict_type"]=="country_site_incompatible":
            conflict["detail"].update({
                "left_country_canonical":left["country_norm"],
                "right_country_canonical":right["country_norm"],
                "known_on_both_sides":True,
            })
    return result


def _insert_country_rows(database:sqlite3.Connection,run_id:str,buildings:Mapping[tuple[str,str],Mapping[str,Any]])->None:
    for key in sorted(buildings):
        row=buildings[key]
        payload={
            "source":key[0],"source_building_id":key[1],"raw_country":str(row["country"] or ""),
            "normalization_status":row["country_normalization_status"],
            "canonical_country":row["country_norm"] or None,"reason":row["country_normalization_reason"],
        }
        database.execute("INSERT INTO country_normalizations VALUES(?,?,?,?,?,?,?,?)",(
            run_id,key[0],key[1],payload["raw_country"],payload["normalization_status"],
            payload["canonical_country"],payload["reason"],base._record_sha(payload),
        ))


def _insert_candidates(
    database:sqlite3.Connection,run_id:str,buildings:Mapping[tuple[str,str],Mapping[str,Any]],
    image_stats:Mapping[tuple[str,str],Mapping[str,Any]],universe:Mapping[tuple[str,str],set[str]],
    e2_summaries:Mapping[tuple[str,str],Mapping[str,Any]],
)->dict[str,dict[str,Any]]:
    evaluated={}
    for left_id,right_id in sorted(universe):
        pair=(left_id,right_id);cid=base.candidate_id(left_id,right_id)
        left=buildings[("divisare",left_id)];right=buildings[("architizer",right_id)];e2_row=e2_summaries.get(pair,{})
        result=evaluate_candidate(left,right,set(universe[pair]),e2_row,image_stats.get(("divisare",left_id),{}),image_stats.get(("architizer",right_id),{}))
        origins=set(result["origins"]);counts={
            "exact":int(e2_row.get("exact_asset_pair_count",0) or 0),"identical":int(e2_row.get("identical_phash_pair_count",0) or 0),
            "le8":int(e2_row.get("phash_le8_pair_count",0) or 0),"weak":int(e2_row.get("phash_9_16_pair_count",0) or 0),
        }
        payload={
            "candidate_id":cid,"left_source_building_id":left_id,"right_source_building_id":right_id,"origins":sorted(origins),
            "e2_metadata_pair_id":e2_row.get("metadata_pair_id"),"e2_building_candidate_id":e2_row.get("e2_building_candidate_id"),
            "image_counts":counts,"min_phash_distance":e2_row.get("min_phash_distance"),
            "name_strict_score":round(result["name_strict"],6),"name_loose_score":round(result["name_loose"],6),
            "name_token_set_score":round(result["name_set"],6),"firm_strict_score":round(result["firm_strict"],6),
            "year_gap":result["year_gap"],"positive_family_count":result["positive_family_count"],
            "negative_family_count":result["negative_family_count"],"hard_conflict_count":result["hard_conflict_count"],
            "proposal":result["proposal"],"auto_merge_eligible":result["auto_merge_eligible"],
        }
        database.execute("INSERT INTO candidate_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            run_id,cid,"divisare",left_id,"architizer",right_id,int("e2_metadata" in origins),int("e2_candidate" in origins),
            int("matcher_architect_year" in origins),int("matcher_pass2" in origins),len(origins),e2_row.get("metadata_pair_id"),
            e2_row.get("e2_building_candidate_id"),counts["exact"],counts["identical"],counts["le8"],counts["weak"],
            e2_row.get("min_phash_distance"),round(result["name_strict"],6),round(result["name_loose"],6),
            round(result["name_set"],6),round(result["firm_strict"],6),result["year_gap"],result["positive_family_count"],
            result["negative_family_count"],result["hard_conflict_count"],result["proposal"],result["auto_merge_eligible"],
            base._record_sha(payload),
        ))
        for origin in sorted(origins):
            detail={
                "origin":origin,"pinned_matcher_sha256":base.FILE_PINS["building_matcher"][2] if origin.startswith("matcher_") else None,
                "e2_metadata_record_sha256":e2_row.get("e2_metadata_record_sha256") if origin=="e2_metadata" else None,
                "e2_candidate_record_sha256":e2_row.get("e2_candidate_record_sha256") if origin=="e2_candidate" else None,
            }
            database.execute("INSERT INTO candidate_origins VALUES(?,?,?,?,?)",(
                run_id,cid,origin,base.canonical_json(detail),base._record_sha({"candidate_id":cid,"origin":origin,"detail":detail}),
            ))
        for family in result["families"]:
            family_payload={"candidate_id":cid,"family":family["family"],"status":family["status"],"score":family["score"],"detail":family["detail"]}
            database.execute("INSERT INTO evidence_families VALUES(?,?,?,?,?,?,?)",(
                run_id,cid,family["family"],family["status"],family["score"],base.canonical_json(family["detail"]),base._record_sha(family_payload),
            ))
        for conflict in result["conflicts"]:
            conflict_payload={"candidate_id":cid,"conflict_type":conflict["conflict_type"],"detail":conflict["detail"]}
            database.execute("INSERT INTO hard_conflicts VALUES(?,?,?,?,?)",(
                run_id,cid,conflict["conflict_type"],base.canonical_json(conflict["detail"]),base._record_sha(conflict_payload),
            ))
        evaluated[cid]={**result,"left_id":left_id,"right_id":right_id,"candidate_id":cid}
    return evaluated


def _row_maps(connection:sqlite3.Connection,run_id:str)->dict[str,Any]:
    candidates={str(row["candidate_id"]):dict(row) for row in connection.execute("SELECT * FROM candidate_pairs WHERE run_id=?",(run_id,))}
    origins:dict[str,set[str]]=defaultdict(set)
    for row in connection.execute("SELECT candidate_id,origin FROM candidate_origins WHERE run_id=?",(run_id,)):origins[str(row[0])].add(str(row[1]))
    locations={str(row[0]):str(row[1]) for row in connection.execute("SELECT candidate_id,status FROM evidence_families WHERE run_id=? AND family='location_site_identity'",(run_id,))}
    country_conflicts={str(row[0]) for row in connection.execute("SELECT candidate_id FROM hard_conflicts WHERE run_id=? AND conflict_type='country_site_incompatible'",(run_id,))}
    source_norm={(str(row["source"]),str(row["source_building_id"])):str(row["country_normalized"] or "") for row in connection.execute("SELECT source,source_building_id,country_normalized FROM source_buildings WHERE run_id=?",(run_id,))}
    return {"candidates":candidates,"origins":origins,"locations":locations,"country_conflicts":country_conflicts,"source_norm":source_norm}


def _insert_transitions(
    database:sqlite3.Connection,run_id:str,v1:sqlite3.Connection,v1_run_id:str,
)->tuple[dict[str,dict[str,Any]],set[str]]:
    v1_maps=_row_maps(v1,v1_run_id);v2_maps=_row_maps(database,run_id);transitions={}
    exclusions=set()
    for row in v1.execute("SELECT candidate_id,opaque_review_id FROM sample_neighbourhoods WHERE run_id=? ORDER BY sample_rank",(v1_run_id,)):
        cid,opaque=str(row[0]),str(row[1]);exclusions.add(cid);payload={"candidate_id":cid,"v1_opaque_review_id":opaque}
        database.execute("INSERT INTO v1_review_exclusions VALUES(?,?,?,?)",(run_id,cid,opaque,base._record_sha(payload)))
    all_ids=sorted(set(v1_maps["candidates"])|set(v2_maps["candidates"]))
    for cid in all_ids:
        old=v1_maps["candidates"].get(cid);new=v2_maps["candidates"].get(cid);source=old or new
        left_id,right_id=str(source["left_source_building_id"]),str(source["right_source_building_id"])
        old_origins=sorted(v1_maps["origins"].get(cid,set()));new_origins=sorted(v2_maps["origins"].get(cid,set()))
        old_location=v1_maps["locations"].get(cid);new_location=v2_maps["locations"].get(cid)
        old_conflict=int(cid in v1_maps["country_conflicts"]);new_conflict=int(cid in v2_maps["country_conflicts"])
        old_proposal=None if old is None else str(old["proposal"]);new_proposal=None if new is None else str(new["proposal"])
        old_auto=None if old is None else int(old["auto_merge_eligible"]);new_auto=None if new is None else int(new["auto_merge_eligible"])
        reasons=[]
        if old is None:reasons.append("candidate_added")
        if new is None:reasons.append("candidate_removed")
        if old_origins!=new_origins:reasons.append("origins_changed")
        if old_location!=new_location:reasons.append("location_family_status_changed")
        if old_conflict!=new_conflict:reasons.append("country_hard_conflict_changed")
        if old_proposal!=new_proposal:reasons.append("proposal_changed")
        if old_auto!=new_auto:reasons.append("auto_merge_eligibility_changed")
        for endpoint in (("divisare",left_id),("architizer",right_id)):
            if v1_maps["source_norm"].get(endpoint,"")!=v2_maps["source_norm"].get(endpoint,""):
                reasons.append(endpoint[0]+"_country_normalization_changed")
        payload={
            "candidate_id":cid,"left_source_building_id":left_id,"right_source_building_id":right_id,
            "v1_present":int(old is not None),"v2_present":int(new is not None),"v1_origins":old_origins,"v2_origins":new_origins,
            "v1_location_status":old_location,"v2_location_status":new_location,"v1_country_conflict":old_conflict,
            "v2_country_conflict":new_conflict,"v1_proposal":old_proposal,"v2_proposal":new_proposal,
            "v1_auto_merge_eligible":old_auto,"v2_auto_merge_eligible":new_auto,"changed":int(bool(reasons)),"reasons":reasons,
        }
        database.execute("INSERT INTO candidate_transitions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            run_id,cid,left_id,right_id,payload["v1_present"],payload["v2_present"],base.canonical_json(old_origins),
            base.canonical_json(new_origins),old_location,new_location,old_conflict,new_conflict,old_proposal,new_proposal,
            old_auto,new_auto,payload["changed"],base.canonical_json(reasons),base._record_sha(payload),
        ))
        transitions[cid]=payload
    return transitions,exclusions


def choose_fresh_n10(
    evaluated:Mapping[str,Mapping[str,Any]],transitions:Mapping[str,Mapping[str,Any]],excluded:set[str],
)->list[dict[str,Any]]:
    selected=[];used_left=set();used_right=set()
    def add(stratum:str,count:int,predicate:Any,preference:Any)->None:
        pool=[row for row in evaluated.values() if row["candidate_id"] not in excluded and predicate(row)]
        pool.sort(key=lambda row:(preference(row),base.stable_hash(SAMPLE_SEED,stratum,str(row["candidate_id"]))))
        for row in pool:
            if row["left_id"] in used_left or row["right_id"] in used_right:continue
            selected.append({"stratum":stratum,**row});used_left.add(row["left_id"]);used_right.add(row["right_id"])
            if sum(item["stratum"]==stratum for item in selected)==count:return
        raise IdentityCandidateV2Error("insufficient distinct candidates for fresh N10 stratum: "+stratum)
    def transition(row:Mapping[str,Any])->Mapping[str,Any]:return transitions.get(str(row["candidate_id"]),{})
    add("state_transition",2,lambda row:transition(row).get("v1_proposal")!=transition(row).get("v2_proposal"),
        lambda row:(0 if transition(row).get("v1_proposal")=="reject" and transition(row).get("v2_proposal")=="merge_candidate" else 1,-int(row["positive_family_count"])))
    add("strong_merge",2,lambda row:row["proposal"]=="merge_candidate" and row["strong_image_count"]>0 and row["name_strict"]>=90,
        lambda row:(-int(row["positive_family_count"]),-int(row["strong_image_count"])))
    add("metadata_only",2,lambda row:"e2_metadata" in row["origins"] and row["strong_image_count"]==0,
        lambda row:(-int(row["positive_family_count"]),int(row["hard_conflict_count"])))
    add("image_evidence_only",2,lambda row:"e2_candidate" in row["origins"] and "e2_metadata" not in row["origins"] and row["strong_image_count"]>0,
        lambda row:(int("matcher_architect_year" in row["origins"] or "matcher_pass2" in row["origins"]),-int(row["strong_image_count"])))
    add("conflict_boundary",2,lambda row:row["hard_conflict_count"]>0 or (row["proposal"]=="defer" and row["positive_family_count"]==1),
        lambda row:(0 if row["hard_conflict_count"]>0 else 1,int(row["hard_conflict_count"]),-int(row["positive_family_count"])))
    if len(selected)!=10:raise AssertionError(len(selected))
    return selected


def _insert_sample(database:sqlite3.Connection,run_id:str,selected:Sequence[Mapping[str,Any]])->None:
    frozen_at=base.utc_now()
    for rank,row in enumerate(selected,1):
        cid=str(row["candidate_id"]);opaque="blind_"+base.stable_hash(SAMPLE_SEED,"opaque",cid)[:24]
        seed_rank=base.stable_hash(SAMPLE_SEED,str(row["stratum"]),cid)
        payload={"sample_rank":rank,"stratum":row["stratum"],"candidate_id":cid,"opaque_review_id":opaque,"seed_rank_sha256":seed_rank}
        database.execute("INSERT INTO sample_neighbourhoods VALUES(?,?,?,?,?,?,?,?)",(
            run_id,rank,row["stratum"],cid,opaque,frozen_at,seed_rank,base._record_sha(payload),
        ))
        focal=database.execute("SELECT left_source_building_id,right_source_building_id FROM candidate_pairs WHERE run_id=? AND candidate_id=?",(run_id,cid)).fetchone()
        neighbours=[(cid,"focal")];contextual=[]
        for candidate in database.execute(
            "SELECT candidate_id,left_source_building_id,right_source_building_id FROM candidate_pairs WHERE run_id=? "
            "AND candidate_id<>? AND (left_source_building_id=? OR right_source_building_id=?)",(run_id,cid,focal[0],focal[1]),
        ):
            relationship="shares_left" if candidate[1]==focal[0] else "shares_right"
            contextual.append((base.stable_hash(SAMPLE_SEED,opaque,str(candidate[0])),str(candidate[0]),relationship))
        neighbours.extend((neighbour,relationship) for _score,neighbour,relationship in sorted(contextual)[:4])
        for order,(neighbour,relationship) in enumerate(neighbours,1):
            pair_payload={"opaque_review_id":opaque,"display_order":order,"candidate_id":neighbour,"relationship":relationship}
            database.execute("INSERT INTO sample_neighbourhood_pairs VALUES(?,?,?,?,?,?)",(
                run_id,opaque,order,neighbour,relationship,base._record_sha(pair_payload),
            ))


def full_logical_sha256(database:sqlite3.Connection,run_id:str,stage_a_logical:str)->str:
    digest=hashlib.sha256();digest.update((FULL_LOGICAL_VERSION+"\n"+stage_a_logical+"\n").encode())
    for table,order in FULL_LOGICAL_TABLES:
        digest.update((table+"\n").encode())
        for row in database.execute(f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}",(run_id,)):
            digest.update((str(row[0])+"\n").encode("ascii"))
    return digest.hexdigest()


def _transition_spec(row:Mapping[str,Any],buildings:Mapping[tuple[str,str],Mapping[str,Any]])->dict[str,Any]:
    left=buildings.get(("divisare",str(row["left_source_building_id"])),{});right=buildings.get(("architizer",str(row["right_source_building_id"])),{})
    return {
        "candidate_id":row["candidate_id"],"left_source_building_id":row["left_source_building_id"],
        "right_source_building_id":row["right_source_building_id"],"left_name":left.get("name"),"right_name":right.get("name"),
        "left_country_raw":left.get("country"),"right_country_raw":right.get("country"),
        "left_country_status":left.get("country_normalization_status"),"right_country_status":right.get("country_normalization_status"),
        "left_country_canonical":left.get("country_norm") or None,"right_country_canonical":right.get("country_norm") or None,
        "v1_proposal":row["v1_proposal"],"v2_proposal":row["v2_proposal"],
        "v1_location_status":row["v1_location_status"],"v2_location_status":row["v2_location_status"],
        "v1_country_conflict":row["v1_country_conflict"],"v2_country_conflict":row["v2_country_conflict"],
        "v1_auto_merge_eligible":row["v1_auto_merge_eligible"],"v2_auto_merge_eligible":row["v2_auto_merge_eligible"],
        "reasons":row["reasons"],
    }


def build_identity_candidates_v2(
    *,repo_root:Path,output_path:Path=DEFAULT_OUTPUT,manifest_path:Path=DEFAULT_MANIFEST,
    report_path:Path=DEFAULT_REPORT,verify_full_hashes:bool=True,
)->BuildResult:
    started=time.monotonic();root=repo_root.resolve();output=output_path if output_path.is_absolute() else root/output_path
    manifest=manifest_path if manifest_path.is_absolute() else root/manifest_path;report=report_path if report_path.is_absolute() else root/report_path
    for path in (output,manifest,report):
        if path.exists():raise IdentityCandidateV2Error(f"refusing to clobber existing artifact: {path}")
    output.parent.mkdir(parents=True,exist_ok=True);manifest.parent.mkdir(parents=True,exist_ok=True);report.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_name(output.name+".building")
    if temporary.exists():raise IdentityCandidateV2Error(f"stale temporary artifact: {temporary}")
    pins=_verify_pins(root,verify_full_hashes);normalizer_sha=base.canonical_sha256(NORMALIZER_SPEC);policy_sha=base.canonical_sha256(POLICY_SPEC)
    run_material={"builder_version":BUILDER_VERSION,"normalizer_sha256":normalizer_sha,"policy_sha256":policy_sha,
        "sample_seed":SAMPLE_SEED,"inputs":{role:row["sha256_before"] for role,row in sorted(pins.items())}}
    run_id="wp21-"+base.canonical_sha256(run_material)[:24];database=None;connections=[]
    try:
        divisare=base._open_immutable(root/base.DEFAULT_DIVISARE);architizer=base._open_immutable(root/base.DEFAULT_ARCHITIZER)
        e2=base._open_immutable(root/base.DEFAULT_E2);v1=base._open_immutable(root/DEFAULT_V1);connections=[divisare,architizer,e2,v1]
        v1_run_rows=v1.execute("SELECT run_id FROM runs").fetchall()
        if len(v1_run_rows)!=1:raise IdentityCandidateV2Error("v1 comparison artifact must contain one run")
        v1_run_id=str(v1_run_rows[0][0]);v1_candidate_count=int(v1.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=?",(v1_run_id,)).fetchone()[0])
        buildings=base.load_source_buildings(divisare,architizer,e2);_apply_country_normalization(buildings)
        image_stats=base.load_image_stats(e2);universe,e2_summaries=base.build_candidate_universe(buildings,e2)
        database=sqlite3.connect(temporary);database.row_factory=sqlite3.Row;database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL");database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}");database.executescript(V2_BASE_SCHEMA_SQL);database.executescript(V2_SCHEMA_SQL)
        database.execute(
            "INSERT INTO runs(run_id,contract_version,builder_version,blocker_version,policy_version,normalizer_version,"
            "sample_version,sample_seed,status,started_at,normalizer_sha256,policy_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id,CONTRACT_VERSION,BUILDER_VERSION,base.BLOCKER_VERSION,POLICY_VERSION,NORMALIZER_VERSION,SAMPLE_VERSION,SAMPLE_SEED,"building",base.utc_now(),normalizer_sha,policy_sha),
        )
        for role,row in sorted(pins.items()):
            payload={"role":role,"path":row["path"],"size_bytes":row["size_bytes"],"sha256_before":row["sha256_before"],"usage":row["usage"]}
            database.execute("INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)",(
                run_id,role,row["path"],row["size_bytes"],row["sha256_before"],None,row["usage"],base._record_sha(payload),
            ))
        base._insert_source_rows(database,run_id,buildings,image_stats);_insert_country_rows(database,run_id,buildings)
        evaluated=_insert_candidates(database,run_id,buildings,image_stats,universe,e2_summaries)
        direct_count=base._insert_direct_image_evidence(database,run_id,e2,e2_summaries)
        transitions,exclusions=_insert_transitions(database,run_id,v1,v1_run_id)
        selected=choose_fresh_n10(evaluated,transitions,exclusions);_insert_sample(database,run_id,selected);database.commit()
        if len(buildings)!=base.EXPECTED_SOURCE_TOTAL or len(selected)!=10:raise IdentityCandidateV2Error("Stage A accounting mismatch")
        if any(row["candidate_id"] in exclusions for row in selected):raise IdentityCandidateV2Error("fresh N10 overlaps v1 reviewed pairs")
        if sum(row["stratum"]=="state_transition" for row in selected)<2:raise IdentityCandidateV2Error("fresh N10 lacks transition pairs")
        if database.execute("SELECT count(*) FROM candidate_pairs WHERE auto_merge_eligible=1 AND hard_conflict_count<>0").fetchone()[0]:
            raise IdentityCandidateV2Error("auto-merge hard-conflict violation")
        for regression_id in V1_TURKEY_REGRESSION_IDS:
            transition=transitions.get(regression_id,{})
            if (transition.get("v1_proposal"),transition.get("v2_proposal"),transition.get("v1_country_conflict"),transition.get("v2_country_conflict"))!=("reject","merge_candidate",1,0):
                raise IdentityCandidateV2Error("Turkey/Türkiye regression pair did not reclassify: "+regression_id)
        for connection in connections:connection.close()
        connections=[]
        for role,(relative,_size,expected,_usage) in INPUT_PINS.items():
            actual=base.sha256_file(root/relative) if verify_full_hashes else expected
            if actual!=pins[role]["sha256_before"]:raise IdentityCandidateV2Error("input changed during build: "+role)
            pins[role]["sha256_after"]=actual;row=pins[role]
            payload={"role":role,"path":row["path"],"size_bytes":row["size_bytes"],"sha256_before":row["sha256_before"],"sha256_after":actual,"usage":row["usage"]}
            database.execute("UPDATE input_pins SET sha256_after=?,record_sha256=? WHERE run_id=? AND role=?",(actual,base._record_sha(payload),run_id,role))
        logical=base.stage_a_logical_sha256(database,run_id);full_logical=full_logical_sha256(database,run_id,logical)
        auto_count=int(database.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1",(run_id,)).fetchone()[0])
        changed_count=int(database.execute("SELECT count(*) FROM candidate_transitions WHERE run_id=? AND changed=1",(run_id,)).fetchone()[0])
        database.execute("UPDATE runs SET status='stage_a_complete',completed_at=?,source_building_count=?,candidate_count=?,stage_a_logical_sha256=? WHERE run_id=?",
            (base.utc_now(),len(buildings),len(evaluated),logical,run_id))
        for name,value in (("source_buildings",len(buildings)),("candidate_pairs",len(evaluated)),("direct_image_evidence",direct_count),
            ("auto_merge_eligible",auto_count),("candidate_transitions",len(transitions)),("changed_transitions",changed_count),
            ("n10_sample",10),("network_requests",0),("vision_requests",0),("llm_requests",0),("legacy_snapshot_reads",0),
            ("merge_executions",0),("vnx_id_issuances",0),("n100_runs",0)):
            database.execute("INSERT INTO metrics VALUES(?,?,?,?)",(run_id,"stage_a",name,value))
        database.commit();integrity=str(database.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity!="ok":raise IdentityCandidateV2Error("output integrity_check failed: "+integrity)
        changed_rows=[transitions[cid] for cid in sorted(transitions) if transitions[cid]["changed"]]
        proposal_transition_rows=[row for row in changed_rows if row["v1_proposal"]!=row["v2_proposal"]]
        decision_lineage_rows=[
            row for row in changed_rows
            if set(row["reasons"])-COUNTRY_REPRESENTATION_REASONS
        ]
        representation_only_rows=[
            row for row in changed_rows
            if set(row["reasons"]) and set(row["reasons"])<=COUNTRY_REPRESENTATION_REASONS
        ]
        changed_specs=[_transition_spec(row,buildings) for row in changed_rows]
        proposal_transition_specs=[_transition_spec(row,buildings) for row in proposal_transition_rows]
        transition_counts=Counter((row["v1_proposal"] or "absent")+"->"+(row["v2_proposal"] or "absent") for row in transitions.values())
        reason_counts=Counter(reason for row in changed_rows for reason in row["reasons"])
        v2_proposals={str(row[0]):int(row[1]) for row in database.execute("SELECT proposal,count(*) FROM candidate_pairs GROUP BY proposal")}
        v1_proposals=Counter(row["v1_proposal"] for row in transitions.values() if row["v1_present"])
        normalizations={str(row[0]):int(row[1]) for row in database.execute("SELECT normalization_status,count(*) FROM country_normalizations GROUP BY normalization_status")}
        n10=[{"sample_rank":rank+1,"stratum":row["stratum"],"candidate_id":row["candidate_id"],
            "v1_proposal":transitions[row["candidate_id"]]["v1_proposal"],"v2_proposal":transitions[row["candidate_id"]]["v2_proposal"]} for rank,row in enumerate(selected)]
        database.close();database=None;os.replace(temporary,output)
        manifest_payload={
            "status":"PASS","run_id":run_id,"contract_version":CONTRACT_VERSION,"builder_version":BUILDER_VERSION,
            "blocker_version":base.BLOCKER_VERSION,"policy_version":POLICY_VERSION,"policy_sha256":policy_sha,
            "normalizer_version":NORMALIZER_VERSION,"normalizer_sha256":normalizer_sha,"normalizer_spec":NORMALIZER_SPEC,
            "policy_spec":POLICY_SPEC,"sample_version":SAMPLE_VERSION,"sample_seed":SAMPLE_SEED,
            "stage_a_logical_sha256":logical,"full_logical_sha256":full_logical,"input_pins":pins,
            "v1_usage":"comparison_and_disjoint_sample_exclusion_only; never candidate evidence",
            "forbidden_actions":{"network_requests":0,"vision_requests":0,"llm_requests":0,"legacy_snapshot_reads":0,
                "merge_executions":0,"vnx_id_issuances":0,"n100_runs":0},
        }
        base._atomic_json(manifest,manifest_payload)
        report_payload={
            "status":"PASS","run_id":run_id,"stage":"WP2.1 Stage A v2 replay","candidate_count":len(evaluated),
            "candidate_count_delta_vs_v1":len(evaluated)-v1_candidate_count,
            "proposal_counts_v1":dict(sorted(v1_proposals.items())),"proposal_counts_v2":dict(sorted(v2_proposals.items())),
            "proposal_count_delta":{key:v2_proposals.get(key,0)-v1_proposals.get(key,0) for key in sorted(set(v1_proposals)|set(v2_proposals))},
            "normalization_status_counts":normalizations,"transition_counts":dict(sorted(transition_counts.items())),
            "normalization_lineage_changed_count":changed_count,
            "normalization_lineage_changed_count_semantics":"any transition row whose evidence, decision, or endpoint country representation changed",
            "proposal_state_transition_count":len(proposal_transition_rows),
            "decision_lineage_change_count":len(decision_lineage_rows),
            "normalization_representation_only_count":len(representation_only_rows),
            "changed_transition_count":changed_count,
            "changed_transition_count_deprecated_semantics":"alias of normalization_lineage_changed_count; not the proposal-state transition count",
            "changed_reason_counts":dict(sorted(reason_counts.items())),
            "reject_to_merge_candidate":[row for row in changed_specs if row["v1_proposal"]=="reject" and row["v2_proposal"]=="merge_candidate"],
            "reject_to_defer":[row for row in changed_specs if row["v1_proposal"]=="reject" and row["v2_proposal"]=="defer"],
            "all_proposal_state_transition_pairs":proposal_transition_specs,
            "all_normalization_lineage_changed_pairs":changed_specs,
            "turkey_turkiye_regression":[row for row in changed_specs if row["candidate_id"] in V1_TURKEY_REGRESSION_IDS],
            "fresh_disjoint_n10":n10,"v1_review_pair_overlap_count":0,
            "stage_a_logical_sha256":logical,"full_logical_sha256":full_logical,
            "accounting":{"source_buildings":len(buildings),"six_family_rows":len(evaluated)*6,"direct_image_evidence_rows":direct_count,
                "transition_rows":len(transitions),"normalization_lineage_changed_rows":changed_count,
                "proposal_state_transition_rows":len(proposal_transition_rows),
                "decision_lineage_change_rows":len(decision_lineage_rows),
                "normalization_representation_only_rows":len(representation_only_rows),
                "registries_created":0,"new_ids_issued":0},
            "cost":{"network_requests":0,"paid_api_requests":0,"vision_requests":0,"llm_requests":0,"estimated_usd":0.0},
            "n100_status":"not_run_waiting_for_user_issued_design",
        }
        base._atomic_json(report,report_payload)
        return BuildResult(output,manifest,report,run_id,logical,full_logical,len(evaluated),auto_count,changed_count,10,time.monotonic()-started)
    except Exception:
        if database is not None:database.close()
        for connection in connections:connection.close()
        if temporary.exists():
            failed=temporary.with_name(temporary.name+".failed")
            if not failed.exists():os.replace(temporary,failed)
        raise


__all__ = [
    "BUILDER_VERSION","BuildResult","CONTRACT_VERSION","DEFAULT_MANIFEST","DEFAULT_OUTPUT","DEFAULT_REPORT",
    "FULL_LOGICAL_VERSION","IdentityCandidateV2Error","NORMALIZER_SPEC","NORMALIZER_VERSION","POLICY_SPEC",
    "POLICY_VERSION","SAMPLE_SEED","SAMPLE_VERSION","SCHEMA_VERSION","V1_TURKEY_REGRESSION_IDS",
    "build_identity_candidates_v2","choose_fresh_n10","evaluate_candidate","full_logical_sha256",
]
