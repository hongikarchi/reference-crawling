#!/usr/bin/env python
"""Emit the compact WP2.1 v1 -> v2 transition report.

The Stage-A report from the first successful v2 replay counted every endpoint
country representation change as ``changed_transition_count``.  This report
keeps that lineage metric but separates it from actual proposal-state changes.
It is intentionally generated without exposing the fresh N10 identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_candidates_v2.db")
DEFAULT_OUTPUT = Path("data/reports/vnext_identity_candidates_v2_transition_report.json")
REPRESENTATION_REASONS = frozenset({
    "divisare_country_normalization_changed",
    "architizer_country_normalization_changed",
})
TURKEY_TURKIYE_IDS = frozenset({
    "idc_da663eb7b113b017cd2024d2c23374aa",
    "idc_abc7bc46b59f16ee341a1cdd167e3f4a",
})


def _sha256(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        while chunk:=handle.read(1024*1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pair_spec(row: sqlite3.Row, sources: dict[tuple[str,str],sqlite3.Row]) -> dict[str,Any]:
    left=sources[("divisare",str(row["left_source_building_id"]))]
    right=sources[("architizer",str(row["right_source_building_id"]))]
    return {
        "candidate_id":str(row["candidate_id"]),
        "left_source_building_id":str(row["left_source_building_id"]),
        "right_source_building_id":str(row["right_source_building_id"]),
        "left_name":str(left["name"]),
        "right_name":str(right["name"]),
        "left_country_raw":str(left["country"]),
        "right_country_raw":str(right["country"]),
        "left_country_canonical":left["canonical_country"],
        "right_country_canonical":right["canonical_country"],
        "left_country_status":str(left["normalization_status"]),
        "right_country_status":str(right["normalization_status"]),
        "v1_proposal":row["v1_proposal"],
        "v2_proposal":row["v2_proposal"],
        "v1_location_status":row["v1_location_status"],
        "v2_location_status":row["v2_location_status"],
        "v1_country_conflict":int(row["v1_country_conflict"]),
        "v2_country_conflict":int(row["v2_country_conflict"]),
        "v1_auto_merge_eligible":row["v1_auto_merge_eligible"],
        "v2_auto_merge_eligible":row["v2_auto_merge_eligible"],
        "reasons":json.loads(str(row["reasons_json"])),
    }


def summarize(artifact: Path, output: Path) -> dict[str,Any]:
    if output.exists():
        raise FileExistsError(f"refusing to clobber transition report: {output}")
    connection=sqlite3.connect(f"file:{artifact.as_posix()}?mode=ro",uri=True)
    connection.row_factory=sqlite3.Row
    try:
        run_rows=connection.execute("SELECT * FROM runs").fetchall()
        if len(run_rows)!=1:
            raise RuntimeError("expected exactly one v2 run")
        run=run_rows[0];run_id=str(run["run_id"])
        sources={
            (str(row["source"]),str(row["source_building_id"])):row
            for row in connection.execute(
                "SELECT s.source,s.source_building_id,s.name,s.country,n.normalization_status,n.canonical_country "
                "FROM source_buildings s JOIN country_normalizations n USING(run_id,source,source_building_id) "
                "WHERE s.run_id=?",(run_id,)
            )
        }
        rows=connection.execute(
            "SELECT * FROM candidate_transitions WHERE run_id=? ORDER BY candidate_id",(run_id,)
        ).fetchall()
        proposal_counts_v2={
            str(row[0]):int(row[1]) for row in connection.execute(
                "SELECT proposal,count(*) FROM candidate_pairs WHERE run_id=? GROUP BY proposal",(run_id,)
            )
        }
        transition_counts:Counter[str]=Counter();reason_counts:Counter[str]=Counter()
        proposal_rows=[];decision_lineage_count=0;representation_only_count=0;unchanged_count=0
        for row in rows:
            reasons=set(json.loads(str(row["reasons_json"])))
            reason_counts.update(reasons)
            transition_counts.update([f"{row['v1_proposal'] or 'absent'}->{row['v2_proposal'] or 'absent'}"])
            if row["v1_proposal"]!=row["v2_proposal"]:
                proposal_rows.append(row)
            if reasons-REPRESENTATION_REASONS:
                decision_lineage_count+=1
            elif reasons:
                representation_only_count+=1
            else:
                unchanged_count+=1
        proposal_specs=[_pair_spec(row,sources) for row in proposal_rows]
        turkey=[row for row in proposal_specs if row["candidate_id"] in TURKEY_TURKIYE_IDS]
        if len(turkey)!=2 or any(
            (row["v1_proposal"],row["v2_proposal"],row["v1_country_conflict"],row["v2_country_conflict"])
            !=("reject","merge_candidate",1,0) for row in turkey
        ):
            raise RuntimeError("Turkey/Turkiye regression pair invariant failed")
        v1_counts:Counter[str]=Counter()
        for row in rows:
            if row["v1_present"]:
                v1_counts.update([str(row["v1_proposal"])])
        strata={str(row[0]):int(row[1]) for row in connection.execute(
            "SELECT stratum,count(*) FROM sample_neighbourhoods WHERE run_id=? GROUP BY stratum",(run_id,)
        )}
        v1_overlap=int(connection.execute(
            "SELECT count(*) FROM sample_neighbourhoods s JOIN v1_review_exclusions e USING(run_id,candidate_id) WHERE s.run_id=?",
            (run_id,),
        ).fetchone()[0])
        payload={
            "status":"PASS","report_version":"vnext-identity-country-transition-compact-v2",
            "run_id":run_id,"artifact_path":str(artifact),"artifact_sha256":_sha256(artifact),
            "candidate_count":len(rows),
            "proposal_counts_v1":dict(sorted(v1_counts.items())),
            "proposal_counts_v2":dict(sorted(proposal_counts_v2.items())),
            "proposal_count_delta":{
                key:proposal_counts_v2.get(key,0)-v1_counts.get(key,0)
                for key in sorted(set(v1_counts)|set(proposal_counts_v2))
            },
            "transition_counts":dict(sorted(transition_counts.items())),
            "proposal_state_transition_count":len(proposal_rows),
            "all_proposal_state_transition_pairs":proposal_specs,
            "reject_to_merge_candidate":[row for row in proposal_specs if row["v1_proposal"]=="reject" and row["v2_proposal"]=="merge_candidate"],
            "reject_to_defer":[row for row in proposal_specs if row["v1_proposal"]=="reject" and row["v2_proposal"]=="defer"],
            "turkey_turkiye_regression":turkey,
            "decision_lineage_change_count":decision_lineage_count,
            "normalization_representation_only_count":representation_only_count,
            "normalization_lineage_changed_count":decision_lineage_count+representation_only_count,
            "unchanged_count":unchanged_count,
            "changed_reason_counts":dict(sorted(reason_counts.items())),
            "metric_note":"proposal_state_transition_count is the decision-state metric; normalization_lineage_changed_count also includes endpoint representation-only changes",
            "fresh_disjoint_n10":{"count":sum(strata.values()),"strata":dict(sorted(strata.items())),"v1_review_pair_overlap_count":v1_overlap},
            "cost":{"network_requests":0,"paid_api_requests":0,"vision_requests":0,"llm_requests":0,"estimated_usd":0.0},
            "n100_status":"not_run_waiting_for_user_issued_design",
            "generated_at":datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z"),
        }
    finally:
        connection.close()
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_name(output.name+".building")
    temporary.write_text(json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    temporary.replace(output)
    return payload


def main() -> int:
    parser=argparse.ArgumentParser(description="Summarize WP2.1 v1 -> v2 candidate transitions.")
    parser.add_argument("--artifact",type=Path,default=DEFAULT_ARTIFACT)
    parser.add_argument("--output",type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args()
    artifact=args.artifact if args.artifact.is_absolute() else ROOT/args.artifact
    output=args.output if args.output.is_absolute() else ROOT/args.output
    payload=summarize(artifact,output)
    print(json.dumps({
        "status":payload["status"],"proposal_state_transition_count":payload["proposal_state_transition_count"],
        "decision_lineage_change_count":payload["decision_lineage_change_count"],
        "normalization_representation_only_count":payload["normalization_representation_only_count"],
        "output":str(output),
    },ensure_ascii=False,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
