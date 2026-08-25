"""WP2.2 ISO-country, anchor, and fan-out guarded identity candidate replay."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from canonical import vnext_identity_candidates as base
from canonical.vnext_country_norm import (
    AMBIGUOUS_COUNTRY_KEYS,
    COUNTRY_NORMALIZER_SPEC,
    ISO_COUNTRY_ADDITIONS,
    NORMALIZER_VERSION as COUNTRY_NORMALIZER_VERSION,
    SOURCE_PROVENANCE,
    classify_country,
)


CONTRACT_VERSION = "vnext-identity-contract-wp22-v3"
BUILDER_VERSION = "vnext-identity-candidates-builder-v3.1"
NORMALIZER_VERSION = "vnext-identity-country-iso3166-v3"
POLICY_VERSION = "vnext-identity-anchor-fanout-policy-v3"
SAMPLE_VERSION = "vnext-identity-wp22-fresh-n10-v3"
SAMPLE_SEED = "vnext-wp22-country-anchor-fanout-fresh-n10-20260825"
FULL_LOGICAL_VERSION = "vnext-identity-stage-a-wp22-transition-logical-v3.1"
APPLICATION_ID = base.APPLICATION_ID
SCHEMA_VERSION = 3

DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_candidates_v3.db")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_candidates_v3_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_candidates_v3_stage_a_report.json")
DEFAULT_V2 = Path("data/enrichment/vnext_identity_candidates_v2.db")

WP22_REGRESSION_IDS = (
    "idc_016b59163550fcd3bfe2dcfde694745a",
    "idc_31831329e77427910c173d3142b4dabe",
    "idc_55b2f5fc5c91de7d9239c392bc3f08b3",
    "idc_4142468fdbf954126c43359abad4e3b3",
)

INPUT_PINS: Mapping[str, tuple[Path, int, str, str]] = {
    "divisare_curated_metadata": (*base.INPUT_SPECS["divisare_curated_metadata"], "candidate_and_evidence"),
    "architizer_curated_metadata": (*base.INPUT_SPECS["architizer_curated_metadata"], "candidate_and_evidence"),
    "cross_source_e2_full_v5": (*base.INPUT_SPECS["cross_source_e2_full_v5"], "candidate_and_evidence"),
    "identity_contract": (
        base.DEFAULT_CONTRACT, 8_165,
        "0f31e6586f9ab862ab7f00ac22d21fb457f7d49e1dae24df29da79d737a978b3",
        "policy_contract",
    ),
    "building_matcher": (*base.FILE_PINS["building_matcher"], "candidate_blocking"),
    "phash_matcher": (*base.FILE_PINS["phash_matcher"], "image_policy_provenance"),
    "workspace_freeze_manifest": (
        base.DEFAULT_FREEZE, 246_838,
        "cd787a0088161fd0e8dddfe23e1943c0a41992f1e1a214b67fcce5d3ae17dcc3",
        "pin_manifest_only",
    ),
    "v2_candidate_artifact": (
        DEFAULT_V2, 581_292_032,
        "08e28e4e5a1ab5aba9ea5ddd53ca615f417458a6693cdfb1aefc822d57b5b0c4",
        "comparison_and_prior_review_exclusion_only",
    ),
    "v1_builder_code": (
        Path("canonical/vnext_identity_candidates.py"), 75_798,
        "3997998b029fc1306ab5a632102af96594ef3034fa6ef44ef88543eb40179447",
        "shared_stage_a_engine_provenance",
    ),
    "country_normalizer_module": (
        Path("canonical/vnext_country_norm.py"), 22_896,
        "98e3cf971525f17393d772f45bf52ac3dfe37dbb5d4bc593019483eafab36015",
        "normalizer_implementation",
    ),
    "v2_builder_code": (
        Path("canonical/vnext_identity_candidates_v2.py"), 38_264,
        "a967763ae8dc947d7e9d91d949d64645a9a18ae967a28e1928f036f6d4975f35",
        "comparison_artifact_provenance",
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
    "anchor_rule": "positive name_alias_identity strict>=90 or verified_pixel_image_overlap required for auto_merge_eligible",
    "anchor_missing_rule": "proposal may remain merge_candidate but auto_merge_eligible=0",
    "fanout_snapshot_rule": "freeze post-anchor eligible graph; apply all demotions simultaneously",
    "fanout_threshold": 2,
    "fanout_counterpart_relation": "all direct same-source pairs require name strict>=90 plus firm or known-country-locality or year<=1; hard conflicts=0",
    "fanout_exception": "all opponent pairs direct-related; no transitive closure",
    "v2_artifact_usage": "comparison_and_prior_review_exclusion_only_not_candidate_evidence",
    "fresh_n10": {
        "exclude_v1_and_v2_review_pairs":True,
        "new_known_country_conflict_min":2,
        "fanout_demotion_min":1,
        "anchor_ineligible_min":1,
        "seed":SAMPLE_SEED,
    },
}

V3_SCHEMA_SQL = """
CREATE TABLE country_normalizations(
  run_id TEXT NOT NULL REFERENCES runs(run_id), source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, raw_country TEXT NOT NULL,
  normalization_status TEXT NOT NULL CHECK(normalization_status IN ('known','unknown','ambiguous')),
  canonical_country TEXT, reason TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id),
  FOREIGN KEY(run_id,source,source_building_id) REFERENCES source_buildings(run_id,source,source_building_id)
);
CREATE TABLE prior_review_exclusions(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  prior_version TEXT NOT NULL CHECK(prior_version IN ('v1','v2')),
  prior_opaque_review_id TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id,prior_version)
);
CREATE TABLE candidate_policy_flags(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  flag_type TEXT NOT NULL CHECK(flag_type IN ('anchor_missing','fanout_conflict')),
  detail_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id,flag_type),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id)
);
CREATE TABLE fanout_groups(
  run_id TEXT NOT NULL REFERENCES runs(run_id), hub_source TEXT NOT NULL,
  hub_source_building_id TEXT NOT NULL, pre_guard_degree INTEGER NOT NULL CHECK(pre_guard_degree>=2),
  status TEXT NOT NULL CHECK(status IN ('exempt','conflict')),
  incident_candidate_ids_json TEXT NOT NULL, opponent_source_building_ids_json TEXT NOT NULL,
  unrelated_opponent_pairs_json TEXT NOT NULL, detail_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,hub_source,hub_source_building_id)
);
CREATE TABLE candidate_transitions(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  left_source_building_id TEXT NOT NULL, right_source_building_id TEXT NOT NULL,
  v2_origins_json TEXT NOT NULL, v3_origins_json TEXT NOT NULL,
  v2_location_status TEXT, v3_location_status TEXT,
  v2_hard_conflicts_json TEXT NOT NULL, v3_hard_conflicts_json TEXT NOT NULL,
  v2_proposal TEXT NOT NULL, v3_proposal TEXT NOT NULL,
  v2_auto_merge_eligible INTEGER NOT NULL CHECK(v2_auto_merge_eligible IN (0,1)),
  v3_auto_merge_eligible INTEGER NOT NULL CHECK(v3_auto_merge_eligible IN (0,1)),
  country_set_changed INTEGER NOT NULL CHECK(country_set_changed IN (0,1)),
  country_conflict_added INTEGER NOT NULL CHECK(country_conflict_added IN (0,1)),
  anchor_ineligible INTEGER NOT NULL CHECK(anchor_ineligible IN (0,1)),
  fanout_demoted INTEGER NOT NULL CHECK(fanout_demoted IN (0,1)),
  origins_changed INTEGER NOT NULL CHECK(origins_changed IN (0,1)),
  proposal_changed INTEGER NOT NULL CHECK(proposal_changed IN (0,1)),
  decision_changed INTEGER NOT NULL CHECK(decision_changed IN (0,1)),
  reasons_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id)
);
"""

_V1_STRATUM_CHECK = "CHECK(stratum IN ('strong_merge','metadata_only','image_evidence_only','conflict_boundary'))"
_V3_STRATUM_CHECK = "CHECK(stratum IN ('new_known_country_conflict','fanout_demotion','anchor_ineligible','strong_merge','metadata_only','image_evidence_only','conflict_boundary'))"
_V1_AUTO_CHECK = "CHECK(auto_merge_eligible=(positive_family_count>=2 AND hard_conflict_count=0)),\n  CHECK((proposal='merge_candidate')=auto_merge_eligible)"
_V3_AUTO_CHECK = "CHECK(auto_merge_eligible=0 OR (positive_family_count>=2 AND hard_conflict_count=0 AND proposal='merge_candidate')),\n  CHECK(proposal!='merge_candidate' OR (positive_family_count>=2 AND hard_conflict_count=0))"
V3_BASE_SCHEMA_SQL = base.SCHEMA_SQL.replace(_V1_STRATUM_CHECK,_V3_STRATUM_CHECK).replace(_V1_AUTO_CHECK,_V3_AUTO_CHECK)
if _V3_STRATUM_CHECK not in V3_BASE_SCHEMA_SQL or _V3_AUTO_CHECK not in V3_BASE_SCHEMA_SQL:
    raise RuntimeError("v1 schema lineage changed before WP2.2 overlay")

FULL_LOGICAL_TABLES = (
    ("country_normalizations","source,source_building_id"),
    ("prior_review_exclusions","candidate_id,prior_version"),
    ("candidate_policy_flags","candidate_id,flag_type"),
    ("fanout_groups","hub_source,hub_source_building_id"),
    ("candidate_transitions","candidate_id"),
)


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


class IdentityCandidateV3Error(RuntimeError):
    pass


def _verify_pins(root:Path,full_hashes:bool)->dict[str,dict[str,Any]]:
    pins={}
    for role,(relative,size,expected,usage) in INPUT_PINS.items():
        path=root/relative
        if not path.is_file() or path.stat().st_size!=size:
            raise IdentityCandidateV3Error(f"input size/missing mismatch: {role}")
        if path.suffix==".db":base._assert_no_sidecars(path)
        digest=base.sha256_file(path) if full_hashes else expected
        if digest!=expected:raise IdentityCandidateV3Error(f"input SHA mismatch: {role}")
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
    anchor_families=[]
    if result["name_strict"]>=base.NAME_POSITIVE_THRESHOLD:
        anchor_families.append("name_alias_identity")
    if result["strong_image_count"]>0:
        anchor_families.append("verified_pixel_image_overlap")
    result["anchor_families"]=anchor_families
    result["pre_anchor_eligible"]=int(result["auto_merge_eligible"])
    result["policy_flags"]=[]
    if result["auto_merge_eligible"] and not anchor_families:
        result["auto_merge_eligible"]=0
        result["policy_flags"].append({
            "flag_type":"anchor_missing",
            "detail":{
                "positive_family_count":result["positive_family_count"],
                "required_anchor_families":["name_alias_identity","verified_pixel_image_overlap"],
                "name_strict_score":round(result["name_strict"],6),
                "strong_image_count":result["strong_image_count"],
                "proposal_preserved":result["proposal"],
            },
        })
    return result


def _same_source_counterpart_relation(
    left:Mapping[str,Any],right:Mapping[str,Any],
)->tuple[bool,dict[str,Any]]:
    """Direct-only conservative relation used solely by the fan-out exception."""
    name_strict, _name_loose, _name_set, name_left, name_right = base.best_similarity(
        left["alias_norms"],right["alias_norms"]
    )
    firm_strict, _firm_loose, _firm_set, firm_left, firm_right = base.best_similarity(
        left["firm_keys"],right["firm_keys"]
    )
    exact_firms=sorted(set(left["firm_keys"])&set(right["firm_keys"]))
    firm_positive=bool(exact_firms) or firm_strict>=base.FIRM_POSITIVE_THRESHOLD
    gap=base._year_gap(left,right)
    country_conflict=bool(left["country_norm"] and right["country_norm"] and left["country_norm"]!=right["country_norm"])
    year_gate_conflict=bool(firm_positive and gap is not None and gap>base.MATCHER_YEAR_TOLERANCE)
    phase_conflict=bool(gap is not None and gap>base.PHASE_HARD_GAP)
    site_positive=bool(
        left["country_norm"] and left["country_norm"]==right["country_norm"]
        and left["locality_norm"] and left["locality_norm"]==right["locality_norm"]
    )
    year_positive=bool(gap is not None and gap<=base.YEAR_POSITIVE_TOLERANCE)
    additional=[]
    if firm_positive:additional.append("architect_firm_identity")
    if site_positive:additional.append("location_site_identity")
    if year_positive:additional.append("year_phase_compatibility")
    hard_conflicts=[]
    if country_conflict:hard_conflicts.append("country_site_incompatible")
    if year_gate_conflict:hard_conflicts.append("matcher_architect_year_gate_block")
    if phase_conflict:hard_conflicts.append("project_phase_incompatible")
    related=bool(name_strict>=base.NAME_POSITIVE_THRESHOLD and additional and not hard_conflicts)
    return related,{
        "related":related,"name_strict_score":round(name_strict,6),
        "name_left_alias":name_left,"name_right_alias":name_right,
        "name_threshold":base.NAME_POSITIVE_THRESHOLD,"additional_positive_families":additional,
        "firm_strict_score":round(firm_strict,6),"firm_left_key":firm_left,"firm_right_key":firm_right,
        "exact_firm_keys":exact_firms,"year_gap":gap,"hard_conflicts":hard_conflicts,
    }


def apply_fanout_guard(
    evaluated:Mapping[str,dict[str,Any]],buildings:Mapping[tuple[str,str],Mapping[str,Any]],
)->list[dict[str,Any]]:
    """Apply all fan-out demotions from one frozen post-anchor graph snapshot."""
    eligible=[row for row in evaluated.values() if row["auto_merge_eligible"]]
    grouped:dict[tuple[str,str],list[dict[str,Any]]]=defaultdict(list)
    for row in eligible:
        grouped[("divisare",str(row["left_id"]))].append(row)
        grouped[("architizer",str(row["right_id"]))].append(row)
    triggers:dict[str,list[dict[str,Any]]]=defaultdict(list)
    groups=[]
    for (hub_source,hub_id),incident in sorted(grouped.items()):
        if len(incident)<2:continue
        opponent_source="architizer" if hub_source=="divisare" else "divisare"
        opponent_ids=sorted({str(row["right_id"] if hub_source=="divisare" else row["left_id"]) for row in incident})
        pair_checks=[];unrelated=[]
        for first_id,second_id in combinations(opponent_ids,2):
            related,detail=_same_source_counterpart_relation(
                buildings[(opponent_source,first_id)],buildings[(opponent_source,second_id)]
            )
            pair={"first_source_building_id":first_id,"second_source_building_id":second_id,**detail}
            pair_checks.append(pair)
            if not related:unrelated.append([first_id,second_id])
        status="conflict" if unrelated else "exempt"
        incident_ids=sorted(str(row["candidate_id"]) for row in incident)
        detail={
            "snapshot":"post_country_post_anchor_pre_fanout","hub_source":hub_source,
            "hub_source_building_id":hub_id,"pre_guard_degree":len(incident),
            "opponent_source":opponent_source,"incident_candidate_ids":incident_ids,
            "opponent_source_building_ids":opponent_ids,"pair_checks":pair_checks,
            "all_opponent_pairs_direct_related":not unrelated,"transitive_closure_used":False,
        }
        group={
            "hub_source":hub_source,"hub_source_building_id":hub_id,"pre_guard_degree":len(incident),
            "status":status,"incident_candidate_ids":incident_ids,
            "opponent_source_building_ids":opponent_ids,"unrelated_opponent_pairs":unrelated,"detail":detail,
        }
        groups.append(group)
        if status=="conflict":
            trigger={
                "hub_source":hub_source,"hub_source_building_id":hub_id,
                "pre_guard_degree":len(incident),"unrelated_opponent_pairs":unrelated,
            }
            for row in incident:triggers[str(row["candidate_id"])].append(trigger)
    for candidate_id,candidate_triggers in sorted(triggers.items()):
        row=evaluated[candidate_id]
        detail={"snapshot":"post_country_post_anchor_pre_fanout","triggers":candidate_triggers}
        row["conflicts"].append({"conflict_type":"fanout_conflict","detail":detail})
        row["hard_conflict_count"]=len(row["conflicts"])
        row["proposal"]="defer"
        row["auto_merge_eligible"]=0
        row["policy_flags"].append({"flag_type":"fanout_conflict","detail":detail})
    return groups


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
)->tuple[dict[str,dict[str,Any]],list[dict[str,Any]]]:
    evaluated:dict[str,dict[str,Any]]={}
    for left_id,right_id in sorted(universe):
        pair=(left_id,right_id);cid=base.candidate_id(left_id,right_id)
        left=buildings[("divisare",left_id)];right=buildings[("architizer",right_id)]
        e2_row=e2_summaries.get(pair,{})
        result=evaluate_candidate(
            left,right,set(universe[pair]),e2_row,image_stats.get(("divisare",left_id),{}),
            image_stats.get(("architizer",right_id),{}),
        )
        evaluated[cid]={**result,"left_id":left_id,"right_id":right_id,"candidate_id":cid,"e2_row":e2_row}
    fanout_groups=apply_fanout_guard(evaluated,buildings)

    for cid in sorted(evaluated):
        result=evaluated[cid];left_id=str(result["left_id"]);right_id=str(result["right_id"])
        e2_row=result["e2_row"];origins=set(result["origins"])
        counts={
            "exact":int(e2_row.get("exact_asset_pair_count",0) or 0),
            "identical":int(e2_row.get("identical_phash_pair_count",0) or 0),
            "le8":int(e2_row.get("phash_le8_pair_count",0) or 0),
            "weak":int(e2_row.get("phash_9_16_pair_count",0) or 0),
        }
        payload={
            "candidate_id":cid,"left_source_building_id":left_id,"right_source_building_id":right_id,
            "origins":sorted(origins),"e2_metadata_pair_id":e2_row.get("metadata_pair_id"),
            "e2_building_candidate_id":e2_row.get("e2_building_candidate_id"),"image_counts":counts,
            "min_phash_distance":e2_row.get("min_phash_distance"),
            "name_strict_score":round(result["name_strict"],6),"name_loose_score":round(result["name_loose"],6),
            "name_token_set_score":round(result["name_set"],6),"firm_strict_score":round(result["firm_strict"],6),
            "year_gap":result["year_gap"],"positive_family_count":result["positive_family_count"],
            "negative_family_count":result["negative_family_count"],"hard_conflict_count":result["hard_conflict_count"],
            "proposal":result["proposal"],"auto_merge_eligible":result["auto_merge_eligible"],
            "anchor_families":list(result["anchor_families"]),
            "policy_flags":sorted(flag["flag_type"] for flag in result["policy_flags"]),
        }
        database.execute("INSERT INTO candidate_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            run_id,cid,"divisare",left_id,"architizer",right_id,int("e2_metadata" in origins),
            int("e2_candidate" in origins),int("matcher_architect_year" in origins),
            int("matcher_pass2" in origins),len(origins),e2_row.get("metadata_pair_id"),
            e2_row.get("e2_building_candidate_id"),counts["exact"],counts["identical"],counts["le8"],counts["weak"],
            e2_row.get("min_phash_distance"),round(result["name_strict"],6),round(result["name_loose"],6),
            round(result["name_set"],6),round(result["firm_strict"],6),result["year_gap"],
            result["positive_family_count"],result["negative_family_count"],result["hard_conflict_count"],
            result["proposal"],result["auto_merge_eligible"],base._record_sha(payload),
        ))
        for origin in sorted(origins):
            detail={
                "origin":origin,
                "pinned_matcher_sha256":base.FILE_PINS["building_matcher"][2] if origin.startswith("matcher_") else None,
                "e2_metadata_record_sha256":e2_row.get("e2_metadata_record_sha256") if origin=="e2_metadata" else None,
                "e2_candidate_record_sha256":e2_row.get("e2_candidate_record_sha256") if origin=="e2_candidate" else None,
            }
            database.execute("INSERT INTO candidate_origins VALUES(?,?,?,?,?)",(
                run_id,cid,origin,base.canonical_json(detail),
                base._record_sha({"candidate_id":cid,"origin":origin,"detail":detail}),
            ))
        for family in result["families"]:
            family_payload={
                "candidate_id":cid,"family":family["family"],"status":family["status"],
                "score":family["score"],"detail":family["detail"],
            }
            database.execute("INSERT INTO evidence_families VALUES(?,?,?,?,?,?,?)",(
                run_id,cid,family["family"],family["status"],family["score"],
                base.canonical_json(family["detail"]),base._record_sha(family_payload),
            ))
        for conflict in result["conflicts"]:
            conflict_payload={"candidate_id":cid,"conflict_type":conflict["conflict_type"],"detail":conflict["detail"]}
            database.execute("INSERT INTO hard_conflicts VALUES(?,?,?,?,?)",(
                run_id,cid,conflict["conflict_type"],base.canonical_json(conflict["detail"]),
                base._record_sha(conflict_payload),
            ))
        for flag in sorted(result["policy_flags"],key=lambda row:str(row["flag_type"])):
            flag_payload={"candidate_id":cid,"flag_type":flag["flag_type"],"detail":flag["detail"]}
            database.execute("INSERT INTO candidate_policy_flags VALUES(?,?,?,?,?)",(
                run_id,cid,flag["flag_type"],base.canonical_json(flag["detail"]),base._record_sha(flag_payload),
            ))
    for group in fanout_groups:
        payload={
            "hub_source":group["hub_source"],"hub_source_building_id":group["hub_source_building_id"],
            "pre_guard_degree":group["pre_guard_degree"],"status":group["status"],
            "incident_candidate_ids":group["incident_candidate_ids"],
            "opponent_source_building_ids":group["opponent_source_building_ids"],
            "unrelated_opponent_pairs":group["unrelated_opponent_pairs"],"detail":group["detail"],
        }
        database.execute("INSERT INTO fanout_groups VALUES(?,?,?,?,?,?,?,?,?,?)",(
            run_id,group["hub_source"],group["hub_source_building_id"],group["pre_guard_degree"],
            group["status"],base.canonical_json(group["incident_candidate_ids"]),
            base.canonical_json(group["opponent_source_building_ids"]),
            base.canonical_json(group["unrelated_opponent_pairs"]),base.canonical_json(group["detail"]),
            base._record_sha(payload),
        ))
    return evaluated,fanout_groups

def _row_maps(connection:sqlite3.Connection,run_id:str)->dict[str,Any]:
    candidates={str(row["candidate_id"]):dict(row) for row in connection.execute(
        "SELECT * FROM candidate_pairs WHERE run_id=?",(run_id,)
    )}
    locations={str(row[0]):str(row[1]) for row in connection.execute(
        "SELECT candidate_id,status FROM evidence_families WHERE run_id=? AND family='location_site_identity'",(run_id,)
    )}
    origins:dict[str,set[str]]=defaultdict(set)
    for row in connection.execute(
        "SELECT candidate_id,origin FROM candidate_origins WHERE run_id=?",(run_id,)
    ):
        origins[str(row[0])].add(str(row[1]))
    conflicts:dict[str,set[str]]=defaultdict(set)
    for row in connection.execute("SELECT candidate_id,conflict_type FROM hard_conflicts WHERE run_id=?",(run_id,)):
        conflicts[str(row[0])].add(str(row[1]))
    country_norm={
        (str(row["source"]),str(row["source_building_id"])):(
            str(row["normalization_status"]),str(row["canonical_country"] or "")
        )
        for row in connection.execute("SELECT * FROM country_normalizations WHERE run_id=?",(run_id,))
    }
    return {
        "candidates":candidates,"locations":locations,"origins":origins,
        "conflicts":conflicts,"country_norm":country_norm,
    }


def _insert_transitions(
    database:sqlite3.Connection,run_id:str,v2:sqlite3.Connection,v2_run_id:str,
    evaluated:Mapping[str,Mapping[str,Any]],
)->tuple[dict[str,dict[str,Any]],set[str]]:
    old_maps=_row_maps(v2,v2_run_id);new_maps=_row_maps(database,run_id)
    if set(old_maps["candidates"])!=set(new_maps["candidates"]):
        raise IdentityCandidateV3Error("v2/v3 candidate universe drift")
    exclusions:set[str]=set()
    for row in v2.execute(
        "SELECT candidate_id,v1_opaque_review_id FROM v1_review_exclusions WHERE run_id=? ORDER BY candidate_id",
        (v2_run_id,),
    ):
        candidate_id,opaque=str(row[0]),str(row[1]);exclusions.add(candidate_id)
        payload={"candidate_id":candidate_id,"prior_version":"v1","prior_opaque_review_id":opaque}
        database.execute("INSERT INTO prior_review_exclusions VALUES(?,?,?,?,?)",(
            run_id,candidate_id,"v1",opaque,base._record_sha(payload),
        ))
    for row in v2.execute(
        "SELECT candidate_id,opaque_review_id FROM sample_neighbourhoods WHERE run_id=? ORDER BY sample_rank",
        (v2_run_id,),
    ):
        candidate_id,opaque=str(row[0]),str(row[1]);exclusions.add(candidate_id)
        payload={"candidate_id":candidate_id,"prior_version":"v2","prior_opaque_review_id":opaque}
        database.execute("INSERT INTO prior_review_exclusions VALUES(?,?,?,?,?)",(
            run_id,candidate_id,"v2",opaque,base._record_sha(payload),
        ))

    transitions:dict[str,dict[str,Any]]={}
    for candidate_id in sorted(new_maps["candidates"]):
        old=old_maps["candidates"][candidate_id];new=new_maps["candidates"][candidate_id]
        left_id=str(new["left_source_building_id"]);right_id=str(new["right_source_building_id"])
        old_origins=sorted(old_maps["origins"].get(candidate_id,set()))
        new_origins=sorted(new_maps["origins"].get(candidate_id,set()))
        old_conflicts=sorted(old_maps["conflicts"].get(candidate_id,set()))
        new_conflicts=sorted(new_maps["conflicts"].get(candidate_id,set()))
        country_set_changed=any(
            old_maps["country_norm"].get(endpoint)!=new_maps["country_norm"].get(endpoint)
            for endpoint in (("divisare",left_id),("architizer",right_id))
        )
        flag_types={str(flag["flag_type"]) for flag in evaluated[candidate_id]["policy_flags"]}
        country_conflict_added=(
            "country_site_incompatible" not in old_conflicts
            and "country_site_incompatible" in new_conflicts
        )
        anchor_ineligible="anchor_missing" in flag_types
        fanout_demoted="fanout_conflict" in flag_types
        origins_changed=old_origins!=new_origins
        proposal_changed=str(old["proposal"])!=str(new["proposal"])
        decision_changed=bool(
            proposal_changed or int(old["auto_merge_eligible"])!=int(new["auto_merge_eligible"])
            or old_conflicts!=new_conflicts
            or old_maps["locations"].get(candidate_id)!=new_maps["locations"].get(candidate_id)
        )
        reasons=[]
        if country_set_changed:reasons.append("country_set_changed")
        if country_conflict_added:reasons.append("country_conflict_added")
        if anchor_ineligible:reasons.append("anchor_ineligible")
        if fanout_demoted:reasons.append("fanout_demoted")
        if origins_changed:reasons.append("origins_changed")
        if proposal_changed:reasons.append("proposal_changed")
        if int(old["auto_merge_eligible"])!=int(new["auto_merge_eligible"]):
            reasons.append("auto_merge_eligibility_changed")
        if old_maps["locations"].get(candidate_id)!=new_maps["locations"].get(candidate_id):
            reasons.append("location_family_status_changed")
        payload={
            "candidate_id":candidate_id,"left_source_building_id":left_id,
            "right_source_building_id":right_id,
            "v2_origins":old_origins,"v3_origins":new_origins,
            "v2_location_status":old_maps["locations"].get(candidate_id),
            "v3_location_status":new_maps["locations"].get(candidate_id),
            "v2_hard_conflicts":old_conflicts,"v3_hard_conflicts":new_conflicts,
            "v2_proposal":str(old["proposal"]),"v3_proposal":str(new["proposal"]),
            "v2_auto_merge_eligible":int(old["auto_merge_eligible"]),
            "v3_auto_merge_eligible":int(new["auto_merge_eligible"]),
            "country_set_changed":int(country_set_changed),
            "country_conflict_added":int(country_conflict_added),
            "anchor_ineligible":int(anchor_ineligible),"fanout_demoted":int(fanout_demoted),
            "origins_changed":int(origins_changed),
            "proposal_changed":int(proposal_changed),"decision_changed":int(decision_changed),
            "reasons":reasons,
        }
        database.execute("INSERT INTO candidate_transitions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            run_id,candidate_id,left_id,right_id,base.canonical_json(old_origins),base.canonical_json(new_origins),
            payload["v2_location_status"],payload["v3_location_status"],
            base.canonical_json(old_conflicts),base.canonical_json(new_conflicts),payload["v2_proposal"],
            payload["v3_proposal"],payload["v2_auto_merge_eligible"],payload["v3_auto_merge_eligible"],
            payload["country_set_changed"],payload["country_conflict_added"],payload["anchor_ineligible"],
            payload["fanout_demoted"],payload["origins_changed"],payload["proposal_changed"],payload["decision_changed"],
            base.canonical_json(reasons),base._record_sha(payload),
        ))
        transitions[candidate_id]=payload
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
            selected.append({"stratum":stratum,**row})
            used_left.add(row["left_id"]);used_right.add(row["right_id"])
            if sum(item["stratum"]==stratum for item in selected)==count:return
        raise IdentityCandidateV3Error("insufficient distinct candidates for fresh N10 stratum: "+stratum)
    def transition(row:Mapping[str,Any])->Mapping[str,Any]:
        return transitions[str(row["candidate_id"])]
    regression=set(WP22_REGRESSION_IDS)
    add("new_known_country_conflict",2,
        lambda row:bool(transition(row)["country_conflict_added"]),
        lambda row:(0 if row["candidate_id"] in regression else 1,0 if transition(row)["v2_auto_merge_eligible"] else 1,-int(row["positive_family_count"])))
    add("fanout_demotion",1,
        lambda row:bool(transition(row)["fanout_demoted"]),
        lambda row:(-int(transition(row)["v2_auto_merge_eligible"]),-int(row["positive_family_count"])))
    add("anchor_ineligible",1,
        lambda row:bool(transition(row)["anchor_ineligible"]) and not transition(row)["fanout_demoted"],
        lambda row:(0 if row["proposal"]=="merge_candidate" else 1,-int(row["positive_family_count"])))
    add("strong_merge",2,
        lambda row:bool(row["auto_merge_eligible"]) and row["strong_image_count"]>0 and row["name_strict"]>=90,
        lambda row:(-int(row["positive_family_count"]),-int(row["strong_image_count"])))
    add("metadata_only",1,
        lambda row:"e2_metadata" in row["origins"] and row["strong_image_count"]==0,
        lambda row:(-int(row["positive_family_count"]),int(row["hard_conflict_count"])))
    add("image_evidence_only",1,
        lambda row:"e2_candidate" in row["origins"] and "e2_metadata" not in row["origins"] and row["strong_image_count"]>0,
        lambda row:(int("matcher_architect_year" in row["origins"] or "matcher_pass2" in row["origins"]),-int(row["strong_image_count"])))
    add("conflict_boundary",2,
        lambda row:row["hard_conflict_count"]>0 or (row["proposal"]=="defer" and row["positive_family_count"]==1),
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


def _transition_spec(
    row:Mapping[str,Any],buildings:Mapping[tuple[str,str],Mapping[str,Any]],
)->dict[str,Any]:
    left=buildings[("divisare",str(row["left_source_building_id"]))]
    right=buildings[("architizer",str(row["right_source_building_id"]))]
    return {
        "candidate_id":row["candidate_id"],
        "left_source_building_id":row["left_source_building_id"],
        "right_source_building_id":row["right_source_building_id"],
        "left_name":left["name"],"right_name":right["name"],
        "left_country_raw":left["country"],"right_country_raw":right["country"],
        "left_country_status":left["country_normalization_status"],
        "right_country_status":right["country_normalization_status"],
        "left_country_canonical":left["country_norm"] or None,
        "right_country_canonical":right["country_norm"] or None,
        "v2_origins":row["v2_origins"],"v3_origins":row["v3_origins"],
        "v2_location_status":row["v2_location_status"],"v3_location_status":row["v3_location_status"],
        "v2_hard_conflicts":row["v2_hard_conflicts"],"v3_hard_conflicts":row["v3_hard_conflicts"],
        "v2_proposal":row["v2_proposal"],"v3_proposal":row["v3_proposal"],
        "v2_auto_merge_eligible":row["v2_auto_merge_eligible"],
        "v3_auto_merge_eligible":row["v3_auto_merge_eligible"],
        "country_set_changed":row["country_set_changed"],
        "country_conflict_added":row["country_conflict_added"],
        "anchor_ineligible":row["anchor_ineligible"],"fanout_demoted":row["fanout_demoted"],
        "origins_changed":row["origins_changed"],
        "proposal_changed":row["proposal_changed"],"decision_changed":row["decision_changed"],
        "reasons":row["reasons"],
    }


def build_identity_candidates_v3(
    *,repo_root:Path,output_path:Path=DEFAULT_OUTPUT,manifest_path:Path=DEFAULT_MANIFEST,
    report_path:Path=DEFAULT_REPORT,verify_full_hashes:bool=True,
)->BuildResult:
    started=time.monotonic();root=repo_root.resolve()
    output=output_path if output_path.is_absolute() else root/output_path
    manifest=manifest_path if manifest_path.is_absolute() else root/manifest_path
    report=report_path if report_path.is_absolute() else root/report_path
    for path in (output,manifest,report):
        if path.exists():raise IdentityCandidateV3Error(f"refusing to clobber existing artifact: {path}")
    output.parent.mkdir(parents=True,exist_ok=True)
    manifest.parent.mkdir(parents=True,exist_ok=True)
    report.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_name(output.name+".building")
    if temporary.exists():raise IdentityCandidateV3Error(f"stale temporary artifact: {temporary}")
    pins=_verify_pins(root,verify_full_hashes)
    normalizer_sha=base.canonical_sha256(NORMALIZER_SPEC)
    policy_sha=base.canonical_sha256(POLICY_SPEC)
    run_material={
        "builder_version":BUILDER_VERSION,"normalizer_sha256":normalizer_sha,
        "policy_sha256":policy_sha,"sample_seed":SAMPLE_SEED,
        "inputs":{role:row["sha256_before"] for role,row in sorted(pins.items())},
    }
    run_id="wp22-"+base.canonical_sha256(run_material)[:24]
    database=None;connections=[]
    try:
        divisare=base._open_immutable(root/base.DEFAULT_DIVISARE)
        architizer=base._open_immutable(root/base.DEFAULT_ARCHITIZER)
        e2=base._open_immutable(root/base.DEFAULT_E2)
        v2=base._open_immutable(root/DEFAULT_V2)
        connections=[divisare,architizer,e2,v2]
        v2_run_rows=v2.execute("SELECT run_id FROM runs").fetchall()
        if len(v2_run_rows)!=1:raise IdentityCandidateV3Error("v2 comparison artifact must contain one run")
        v2_run_id=str(v2_run_rows[0][0])
        v2_candidate_count=int(v2.execute(
            "SELECT count(*) FROM candidate_pairs WHERE run_id=?",(v2_run_id,)
        ).fetchone()[0])
        v2_proposals={str(row[0]):int(row[1]) for row in v2.execute(
            "SELECT proposal,count(*) FROM candidate_pairs WHERE run_id=? GROUP BY proposal",(v2_run_id,)
        )}
        buildings=base.load_source_buildings(divisare,architizer,e2)
        _apply_country_normalization(buildings)
        image_stats=base.load_image_stats(e2)
        universe,e2_summaries=base.build_candidate_universe(buildings,e2)

        database=sqlite3.connect(temporary);database.row_factory=sqlite3.Row
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(V3_BASE_SCHEMA_SQL)
        database.executescript(V3_SCHEMA_SQL)
        database.execute(
            "INSERT INTO runs(run_id,contract_version,builder_version,blocker_version,policy_version,normalizer_version,"
            "sample_version,sample_seed,status,started_at,normalizer_sha256,policy_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id,CONTRACT_VERSION,BUILDER_VERSION,base.BLOCKER_VERSION,POLICY_VERSION,NORMALIZER_VERSION,
             SAMPLE_VERSION,SAMPLE_SEED,"building",base.utc_now(),normalizer_sha,policy_sha),
        )
        for role,row in sorted(pins.items()):
            payload={
                "role":role,"path":row["path"],"size_bytes":row["size_bytes"],
                "sha256_before":row["sha256_before"],"usage":row["usage"],
            }
            database.execute("INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)",(
                run_id,role,row["path"],row["size_bytes"],row["sha256_before"],None,
                row["usage"],base._record_sha(payload),
            ))
        base._insert_source_rows(database,run_id,buildings,image_stats)
        _insert_country_rows(database,run_id,buildings)
        evaluated,fanout_groups=_insert_candidates(
            database,run_id,buildings,image_stats,universe,e2_summaries
        )
        direct_count=base._insert_direct_image_evidence(database,run_id,e2,e2_summaries)
        transitions,exclusions=_insert_transitions(database,run_id,v2,v2_run_id,evaluated)
        selected=choose_fresh_n10(evaluated,transitions,exclusions)
        _insert_sample(database,run_id,selected)
        database.commit()

        expected_strata={
            "new_known_country_conflict":2,"fanout_demotion":1,"anchor_ineligible":1,
            "strong_merge":2,"metadata_only":1,"image_evidence_only":1,"conflict_boundary":2,
        }
        actual_strata=dict(sorted(Counter(str(row["stratum"]) for row in selected).items()))
        if len(buildings)!=base.EXPECTED_SOURCE_TOTAL or len(evaluated)!=v2_candidate_count:
            raise IdentityCandidateV3Error("Stage A population accounting mismatch")
        if actual_strata!=expected_strata:
            raise IdentityCandidateV3Error(f"fresh N10 strata mismatch: {actual_strata}")
        if any(str(row["candidate_id"]) in exclusions for row in selected):
            raise IdentityCandidateV3Error("fresh N10 overlaps prior reviewed pairs")
        if database.execute(
            "SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1 "
            "AND (hard_conflict_count<>0 OR proposal<>'merge_candidate')",(run_id,)
        ).fetchone()[0]:
            raise IdentityCandidateV3Error("auto-merge invariant violation")
        if database.execute(
            "SELECT count(*) FROM candidate_pairs c WHERE c.run_id=? AND c.auto_merge_eligible=1 "
            "AND c.name_strict_score<90 AND "
            "(c.exact_asset_pair_count+c.identical_phash_pair_count+c.phash_le8_pair_count)=0",(run_id,)
        ).fetchone()[0]:
            raise IdentityCandidateV3Error("auto-merge anchor invariant violation")
        for regression_id in WP22_REGRESSION_IDS:
            transition=transitions.get(regression_id,{})
            actual=(
                transition.get("v2_proposal"),transition.get("v3_proposal"),
                transition.get("v2_auto_merge_eligible"),transition.get("v3_auto_merge_eligible"),
                transition.get("country_conflict_added"),
            )
            if actual!=("merge_candidate","reject",1,0,1):
                raise IdentityCandidateV3Error("WP2.2 regression pair did not return to reject: "+regression_id)
        for value in ("Senegal","Republic of Senegal","Ecuador","Republic of Ecuador"):
            if classify_country(value).status!="known":
                raise IdentityCandidateV3Error("ISO known-country regression failed: "+value)
        if len(ISO_COUNTRY_ADDITIONS["known_countries"])!=159 or len(ISO_COUNTRY_ADDITIONS["aliases"])!=398:
            raise IdentityCandidateV3Error("ISO additions accounting mismatch")

        old_maps=_row_maps(v2,v2_run_id);new_maps=_row_maps(database,run_id)
        changed_source_keys=sorted(
            key for key in new_maps["country_norm"]
            if old_maps["country_norm"].get(key)!=new_maps["country_norm"].get(key)
        )
        for connection in connections:connection.close()
        connections=[]
        for role,(relative,_size,expected,_usage) in INPUT_PINS.items():
            actual=base.sha256_file(root/relative) if verify_full_hashes else expected
            if actual!=pins[role]["sha256_before"]:
                raise IdentityCandidateV3Error("input changed during build: "+role)
            pins[role]["sha256_after"]=actual;row=pins[role]
            payload={
                "role":role,"path":row["path"],"size_bytes":row["size_bytes"],
                "sha256_before":row["sha256_before"],"sha256_after":actual,"usage":row["usage"],
            }
            database.execute(
                "UPDATE input_pins SET sha256_after=?,record_sha256=? WHERE run_id=? AND role=?",
                (actual,base._record_sha(payload),run_id,role),
            )

        logical=base.stage_a_logical_sha256(database,run_id)
        full_logical=full_logical_sha256(database,run_id,logical)
        auto_count=int(database.execute(
            "SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1",(run_id,)
        ).fetchone()[0])
        changed_count=sum(int(row["decision_changed"]) for row in transitions.values())
        country_changed=[row for row in transitions.values() if row["country_set_changed"]]
        country_conflict_added=[row for row in transitions.values() if row["country_conflict_added"]]
        anchor_ineligible=[row for row in transitions.values() if row["anchor_ineligible"]]
        fanout_demoted=[row for row in transitions.values() if row["fanout_demoted"]]
        origin_changed=[row for row in transitions.values() if row["origins_changed"]]
        proposal_changed=[row for row in transitions.values() if row["proposal_changed"]]
        transition_counts=Counter(
            f"{row['v2_proposal']}->{row['v3_proposal']}" for row in transitions.values()
        )
        reason_counts=Counter(reason for row in transitions.values() for reason in row["reasons"])
        v3_proposals={str(row[0]):int(row[1]) for row in database.execute(
            "SELECT proposal,count(*) FROM candidate_pairs WHERE run_id=? GROUP BY proposal",(run_id,)
        )}
        normalizations={str(row[0]):int(row[1]) for row in database.execute(
            "SELECT normalization_status,count(*) FROM country_normalizations WHERE run_id=? GROUP BY normalization_status",
            (run_id,),
        )}
        fanout_status_counts=dict(sorted(Counter(group["status"] for group in fanout_groups).items()))
        database.execute(
            "UPDATE runs SET status='stage_a_complete',completed_at=?,source_building_count=?,"
            "candidate_count=?,stage_a_logical_sha256=? WHERE run_id=?",
            (base.utc_now(),len(buildings),len(evaluated),logical,run_id),
        )
        metric_rows=(
            ("source_buildings",len(buildings)),("candidate_pairs",len(evaluated)),
            ("direct_image_evidence",direct_count),("auto_merge_eligible",auto_count),
            ("candidate_transitions",len(transitions)),("decision_changed_pairs",changed_count),
            ("country_set_changed_source_buildings",len(changed_source_keys)),
            ("country_set_changed_pairs",len(country_changed)),
            ("country_conflict_added_pairs",len(country_conflict_added)),
            ("anchor_ineligible_pairs",len(anchor_ineligible)),
            ("fanout_demoted_pairs",len(fanout_demoted)),
            ("origin_changed_pairs",len(origin_changed)),
            ("fanout_conflict_groups",fanout_status_counts.get("conflict",0)),
            ("fanout_exempt_groups",fanout_status_counts.get("exempt",0)),
            ("n10_sample",10),("network_requests",0),("vision_requests",0),("llm_requests",0),
            ("legacy_snapshot_reads",0),("merge_executions",0),("vnx_id_issuances",0),("n100_runs",0),
        )
        for name,value in metric_rows:
            database.execute("INSERT INTO metrics VALUES(?,?,?,?)",(run_id,"stage_a",name,value))
        database.commit()
        integrity=str(database.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity!="ok":raise IdentityCandidateV3Error("output integrity_check failed: "+integrity)

        transition_specs=[_transition_spec(row,buildings) for row in transitions.values()]
        transition_specs_by_id={row["candidate_id"]:row for row in transition_specs}
        n10=[
            {
                "sample_rank":rank+1,"stratum":row["stratum"],"candidate_id":row["candidate_id"],
                "v2_proposal":transitions[row["candidate_id"]]["v2_proposal"],
                "v3_proposal":transitions[row["candidate_id"]]["v3_proposal"],
                "v2_auto_merge_eligible":transitions[row["candidate_id"]]["v2_auto_merge_eligible"],
                "v3_auto_merge_eligible":transitions[row["candidate_id"]]["v3_auto_merge_eligible"],
            }
            for rank,row in enumerate(selected)
        ]
        database.close();database=None
        os.replace(temporary,output)
        stage_a_artifact_size=output.stat().st_size
        stage_a_artifact_sha=base.sha256_file(output)

        manifest_payload={
            "status":"PASS","run_id":run_id,"contract_version":CONTRACT_VERSION,
            "builder_version":BUILDER_VERSION,"blocker_version":base.BLOCKER_VERSION,
            "policy_version":POLICY_VERSION,"policy_sha256":policy_sha,
            "normalizer_version":NORMALIZER_VERSION,"normalizer_sha256":normalizer_sha,
            "normalizer_spec":NORMALIZER_SPEC,"additions":ISO_COUNTRY_ADDITIONS,
            "policy_spec":POLICY_SPEC,"sample_version":SAMPLE_VERSION,"sample_seed":SAMPLE_SEED,
            "stage_a_logical_sha256":logical,"full_logical_sha256":full_logical,
            "stage_a_artifact_size_bytes":stage_a_artifact_size,
            "stage_a_artifact_sha256":stage_a_artifact_sha,"input_pins":pins,
            "v2_usage":"comparison_and_prior_review_exclusion_only; never candidate evidence",
            "forbidden_actions":{
                "network_requests":0,"vision_requests":0,"llm_requests":0,
                "legacy_snapshot_reads":0,"merge_executions":0,"vnx_id_issuances":0,"n100_runs":0,
            },
        }
        base._atomic_json(manifest,manifest_payload)

        def specs(rows:Sequence[Mapping[str,Any]])->list[dict[str,Any]]:
            return [transition_specs_by_id[str(row["candidate_id"])] for row in rows]
        report_payload={
            "status":"PASS","report_version":"vnext-identity-wp22-transition-full-v3",
            "run_id":run_id,"stage":"WP2.2 Stage A v3 replay",
            "candidate_count":len(evaluated),"candidate_count_delta_vs_v2":len(evaluated)-v2_candidate_count,
            "proposal_counts_v2":dict(sorted(v2_proposals.items())),
            "proposal_counts_v3":dict(sorted(v3_proposals.items())),
            "proposal_count_delta":{
                key:v3_proposals.get(key,0)-v2_proposals.get(key,0)
                for key in sorted(set(v2_proposals)|set(v3_proposals))
            },
            "normalization_status_counts":normalizations,
            "country_set_changed_source_building_count":len(changed_source_keys),
            "country_set_changed_source_buildings":[
                {"source":source,"source_building_id":building_id} for source,building_id in changed_source_keys
            ],
            "transition_counts":dict(sorted(transition_counts.items())),
            "decision_changed_count":changed_count,
            "proposal_changed_count":len(proposal_changed),
            "country_set_changed_pair_count":len(country_changed),
            "country_conflict_added_pair_count":len(country_conflict_added),
            "anchor_ineligible_count":len(anchor_ineligible),
            "fanout_demoted_count":len(fanout_demoted),
            "origin_changed_count":len(origin_changed),
            "fanout_group_status_counts":fanout_status_counts,
            "changed_reason_counts":dict(sorted(reason_counts.items())),
            "all_country_set_changed_pairs":specs(country_changed),
            "all_country_conflict_added_pairs":specs(country_conflict_added),
            "all_anchor_ineligible_pairs":specs(anchor_ineligible),
            "all_fanout_demoted_pairs":specs(fanout_demoted),
            "all_origin_changed_pairs":specs(origin_changed),
            "all_proposal_changed_pairs":specs(proposal_changed),
            "fanout_groups":fanout_groups,
            "wp22_four_pair_regression":[transition_specs_by_id[cid] for cid in WP22_REGRESSION_IDS],
            "fresh_disjoint_n10":n10,"prior_review_pair_overlap_count":0,
            "stage_a_logical_sha256":logical,"full_logical_sha256":full_logical,
            "stage_a_artifact_size_bytes":stage_a_artifact_size,
            "stage_a_artifact_sha256":stage_a_artifact_sha,
            "accounting":{
                "source_buildings":len(buildings),"six_family_rows":len(evaluated)*6,
                "direct_image_evidence_rows":direct_count,"transition_rows":len(transitions),
                "prior_review_exclusions":len(exclusions),"registries_created":0,"new_ids_issued":0,
            },
            "cost":{
                "network_requests":0,"paid_api_requests":0,"vision_requests":0,
                "llm_requests":0,"estimated_usd":0.0,
            },
            "n100_status":"not_run_waiting_for_separate_order",
        }
        base._atomic_json(report,report_payload)
        return BuildResult(
            output,manifest,report,run_id,logical,full_logical,len(evaluated),
            auto_count,changed_count,10,time.monotonic()-started,
        )
    except Exception:
        if database is not None:database.close()
        for connection in connections:connection.close()
        if temporary.exists():
            failed=temporary.with_name(temporary.name+".failed")
            if not failed.exists():os.replace(temporary,failed)
        raise

__all__ = [
    "BUILDER_VERSION","BuildResult","CONTRACT_VERSION","DEFAULT_MANIFEST","DEFAULT_OUTPUT","DEFAULT_REPORT",
    "FULL_LOGICAL_VERSION","IdentityCandidateV3Error","NORMALIZER_SPEC","NORMALIZER_VERSION","POLICY_SPEC",
    "POLICY_VERSION","SAMPLE_SEED","SAMPLE_VERSION","SCHEMA_VERSION","V3_BASE_SCHEMA_SQL",
    "WP22_REGRESSION_IDS","apply_fanout_guard","build_identity_candidates_v3","choose_fresh_n10",
    "evaluate_candidate","full_logical_sha256",
]
