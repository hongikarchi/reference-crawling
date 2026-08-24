"""Independent validator for the WP2 identity-candidate artifact.

This module deliberately does not import ``vnext_identity_candidates``.  It
reconstructs the curated population, matcher blocking, six evidence-family
decisions, hard conflicts, sample strata, and gates from the pinned inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rapidfuzz import fuzz


VALIDATOR_VERSION = "vnext-identity-candidates-independent-validator-v1"
APPLICATION_ID = 0x564E5832
SCHEMA_VERSION = 1
LOGICAL_VERSION = "vnext-identity-stage-a-logical-v1"
BUILDER_VERSION = "vnext-identity-candidates-builder-v1"
NORMALIZER_VERSION = "vnext-identity-unicode-firm-core-v1"
POLICY_VERSION = "vnext-identity-six-family-policy-v1"
SAMPLE_VERSION = "vnext-identity-stratified-neighbourhood-n10-v1"
SAMPLE_SEED = "vnext-wp2-identity-n10-20260824"

DIVISARE = Path("data/curated/divisare_metadata_v2_4.db")
ARCHITIZER = Path("data/curated/architizer_curated_v2_0.db")
E2 = Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db")
CONTRACT = Path("docs/vnext/CONTRACT_IDENTITY.md")
MATCHER = Path("canonical/match_buildings_sequential.py")
PHASH_MATCHER = Path("canonical/match_phash_check.py")
DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_candidates_v1.db")
DEFAULT_REPORT = Path("data/reports/vnext_identity_candidates_v1_validation.json")

INPUT_SPECS: Mapping[str, tuple[Path, int, str]] = {
    "divisare_curated_metadata": (DIVISARE, 2_225_299_456, "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f"),
    "architizer_curated_metadata": (ARCHITIZER, 8_767_438_848, "605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc"),
    "cross_source_e2_full_v5": (E2, 10_164_682_752, "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19"),
    "identity_contract": (CONTRACT, 6_598, "1ee13f1a2a7ffe7dcaf5b7c1cbaf419c5999d53bcd473311499b74070aabeca6"),
    "building_matcher": (MATCHER, 26_812, "3cf9592a9e54cd4e07512461ca08dc5ca7f8dd5e6cd97b2d6c118dd0b265d87b"),
    "phash_matcher": (PHASH_MATCHER, 5_450, "3dcc73152d822fe2b8eaccbb333625672378a3f6cfa59d3099cda0ca28e64fe7"),
}
EXPECTED_COUNTS = {"divisare": 29_891, "architizer": 61_912, "total": 91_803, "metadata": 6_754, "e2_candidates": 9_026}
EVIDENCE_FAMILIES = (
    "name_alias_identity", "architect_firm_identity", "location_site_identity",
    "year_phase_compatibility", "source_url_external_stable_id", "verified_pixel_image_overlap",
)
NAME_THRESHOLD = 90.0
FIRM_THRESHOLD = 90.0
YEAR_BLOCK_TOLERANCE = 2
PASS2_YEAR_TOLERANCE = 1
PASS2_TOKEN_OVERLAP = 2
YEAR_POSITIVE_TOLERANCE = 1
PHASE_HARD_GAP = 10
PHASH_STRONG_MIN = 3
PHASH_THIN_MIN = 2

GENERIC_FIRM = frozenset(
    {
        "architects", "architect", "architectes", "architecte", "architecten", "architekti",
        "architekt", "architekten", "arquitectos", "arquitectes", "arquiteto", "arquitetos",
        "arquitectura", "arquitetura", "architectures", "architecture", "architectural", "atelier",
        "studio", "estudio", "studios", "ateliers", "design", "designs", "designstudio", "office",
        "bureau", "associates", "associati", "associes", "asociados", "partner", "partners",
        "group", "grupo", "gruppe", "compagnia", "company", "co", "llp", "ltd", "srl", "gmbh",
        "sa", "sl", "inc", "spa", "ag", "kg", "et", "and", "the", "de", "da", "do", "di",
        "del", "des", "der", "die", "das", "consultants", "consulting", "planning", "planners",
        "urbanism", "urbanisme", "urbanistes", "urbanists", "construction", "engineering", "engineers",
        "engineer", "interiors", "lab", "projetos", "projectos", "proyectos", "workshop", "architizer",
    }
)
GENERIC_BUILDING = frozenset(
    {
        "house", "home", "office", "tower", "building", "project", "studio", "the", "a", "an",
        "and", "of", "for", "by", "with", "in", "on", "centre", "center", "store", "shop", "park",
        "garden", "plaza", "school", "library", "museum", "hotel", "restaurant", "cafe", "bar",
        "residence", "residential", "apartment", "apartments", "complex",
    }
)
COUNTRY_ALIASES = {
    "usa":"united states", "u s a":"united states", "united states of america":"united states",
    "uk":"united kingdom", "u k":"united kingdom", "great britain":"united kingdom",
    "england":"united kingdom", "scotland":"united kingdom", "wales":"united kingdom",
    "czech republic":"czechia", "russian federation":"russia", "republic of korea":"south korea",
    "korea south":"south korea", "viet nam":"vietnam",
}
ASCII_FOLD = str.maketrans({"ø":"o", "ł":"l", "đ":"d", "ð":"d", "þ":"th", "æ":"ae", "œ":"oe", "ß":"ss"})

NORMALIZER_SPEC = {
    "version":NORMALIZER_VERSION,"unicode":"NFKD+casefold+combining-mark-removal",
    "punctuation":"unicode-nonword-to-space",
    "ascii_fold":dict(sorted({chr(k):v for k,v in ASCII_FOLD.items()}.items())),
    "firm_generic_tokens":sorted(GENERIC_FIRM),"building_generic_tokens":sorted(GENERIC_BUILDING),
    "country_aliases":dict(sorted(COUNTRY_ALIASES.items())),
    "firm_key_strength":"one token length>=3 or at least two tokens",
}
POLICY_SPEC = {
    "version":POLICY_VERSION,
    "candidate_union":["e2_metadata","e2_candidate","matcher_architect_year","matcher_pass2"],
    "name_positive_threshold":90.0,"firm_positive_threshold":90.0,"matcher_year_tolerance":2,
    "pass2_year_tolerance":1,"pass2_min_distinctive_token_overlap":2,"year_positive_tolerance":1,
    "phase_hard_gap":10,"phash_threshold":8,"phash_strong_min_unique_pixels":3,
    "phash_thin_min_unique_pixels":2,"auto_merge_rule":"positive_families>=2 and hard_conflicts=0",
    "legacy_snapshot_candidate_evidence":False,"transitive_image_evidence":False,"merge_execution":False,
}

REQUIRED_TABLES = {
    "runs", "input_pins", "source_buildings", "building_image_stats", "candidate_pairs",
    "candidate_origins", "evidence_families", "hard_conflicts", "direct_image_evidence",
    "sample_neighbourhoods", "sample_neighbourhood_pairs", "review_assets", "blind_reviews",
    "adjudications", "validations", "metrics",
}
FORBIDDEN_TABLE_FRAGMENTS = ("registry", "component", "crosswalk")
LOGICAL_TABLES: tuple[tuple[str,str], ...] = (
    ("input_pins","role"),("source_buildings","source,source_building_id"),
    ("building_image_stats","source,source_building_id"),
    ("candidate_pairs","left_source_building_id,right_source_building_id"),
    ("candidate_origins","candidate_id,origin"),("evidence_families","candidate_id,family"),
    ("hard_conflicts","candidate_id,conflict_type"),("direct_image_evidence","e2_evidence_id"),
    ("sample_neighbourhoods","sample_rank"),("sample_neighbourhood_pairs","opaque_review_id,display_order"),
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    artifact_path: Path
    report_path: Path | None
    run_id: str | None
    checks: tuple[Check, ...]
    metrics: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(row.passed for row in self.checks)

    @property
    def failed_check_names(self) -> tuple[str,...]:
        return tuple(row.name for row in self.checks if not row.passed)


def _json(value: Any) -> str:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


def _json_safe(value: Any) -> Any:
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value)]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key):_json_safe(item) for key,item in value.items()}
    if isinstance(value, Path):
        return str(value)
    return value


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        while chunk:=handle.read(16*1024*1024):digest.update(chunk)
    return digest.hexdigest()


def _stable(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _norm(value: Any, *, ascii_fold: bool=False) -> str:
    text=unicodedata.normalize("NFKD",str(value or "")).casefold()
    text="".join(char for char in text if not unicodedata.combining(char))
    if ascii_fold:text=text.translate(ASCII_FOLD)
    text=re.sub(r"[^\w]+"," ",text,flags=re.UNICODE)
    return " ".join(token.strip("_") for token in text.split() if token.strip("_"))


def _country(value: Any) -> str:
    value=_norm(value,ascii_fold=True)
    return COUNTRY_ALIASES.get(value,value)


def _firm_core(value: Any, *, ascii_fold: bool=False) -> str:
    return " ".join(
        token for token in _norm(value,ascii_fold=ascii_fold).split()
        if token not in GENERIC_FIRM and (len(token)>=2 or token.isdigit())
    )


def _firm_keys(values: Iterable[Any]) -> tuple[str,...]:
    result:set[str]=set()
    for raw in values:
        if not raw:continue
        text=str(raw).removesuffix(" - Architizer").replace("-"," ")
        for fold in (False,True):
            core=_firm_core(text,ascii_fold=fold);tokens=core.split()
            if tokens and (len(tokens)>=2 or len(tokens[0])>=3):result.add(core)
    return tuple(sorted(result))


def _tokens(value: str) -> set[str]:
    return {token for token in _norm(value,ascii_fold=True).split() if len(token)>=3 and token not in GENERIC_BUILDING}


def _sim(left: str,right: str) -> tuple[float,float,float]:
    if not left or not right:return 0.0,0.0,0.0
    sort=float(fuzz.token_sort_ratio(left,right));sets=float(fuzz.token_set_ratio(left,right))
    return min(sort,sets),max(sort,sets),sets


def _best(left_values: Sequence[str],right_values: Sequence[str]) -> tuple[float,float,float]:
    best=(0.0,0.0,0.0)
    for left in left_values:
        for right in right_values:
            scores=_sim(left,right)
            if scores>best:best=scores
    return best


def _open(path: Path) -> sqlite3.Connection:
    connection=sqlite3.connect("file:"+path.resolve().as_posix()+"?mode=ro&immutable=1",uri=True)
    connection.row_factory=sqlite3.Row;connection.execute("PRAGMA query_only=ON")
    return connection


def _check(checks:list[Check],name:str,expected:Any,actual:Any,detail:str|None=None)->None:
    checks.append(Check(name,expected==actual,expected,actual,detail))


def _year_gap(left:Mapping[str,Any],right:Mapping[str,Any])->int|None:
    ly=[x for x in (left.get("year_min"),left.get("year_max")) if x is not None]
    ry=[x for x in (right.get("year_min"),right.get("year_max")) if x is not None]
    if not ly or not ry:return None
    lmin,lmax=min(ly),max(ly);rmin,rmax=min(ry),max(ry)
    if lmax<rmin:return int(rmin-lmax)
    if rmax<lmin:return int(lmin-rmax)
    return 0


def _candidate_id(left_id:str,right_id:str)->str:
    return "idc_"+_stable("vnext-identity-candidate-v1","divisare",left_id,"architizer",right_id)[:32]


def _endpoint(row:sqlite3.Row)->tuple[str,str]:
    if row["left_source"]=="divisare":return str(row["left_source_building_id"]),str(row["right_source_building_id"])
    return str(row["right_source_building_id"]),str(row["left_source_building_id"])


def _load_buildings(divisare:sqlite3.Connection,architizer:sqlite3.Connection,e2:sqlite3.Connection)->dict[tuple[str,str],dict[str,Any]]:
    buildings:dict[tuple[str,str],dict[str,Any]]={}
    for row in e2.execute(
        "SELECT source,source_building_id,name,country,locality,completion_year_min,completion_year_max,source_record_sha256,metadata_json FROM source_buildings"
    ):
        key=(str(row["source"]),str(row["source_building_id"]))
        buildings[key]={
            "source":key[0],"id":key[1],"name":str(row["name"] or ""),"aliases":{str(row["name"] or "")},
            "firms":set(),"country":str(row["country"] or ""),"locality":str(row["locality"] or ""),
            "year_min":row["completion_year_min"],"year_max":row["completion_year_max"],
            "urls":set(),"external_ids":set(),"e2_sha":str(row["source_record_sha256"]),
            "e2_metadata":json.loads(str(row["metadata_json"])),
        }
    div_ids={str(row[0]) for row in divisare.execute("SELECT building_id FROM buildings")}
    atz_ids={str(row[0]) for row in architizer.execute("SELECT building_id FROM buildings")}
    if div_ids!={key[1] for key in buildings if key[0]=="divisare"} or atz_ids!={key[1] for key in buildings if key[0]=="architizer"}:
        raise ValueError("curated/E2 membership mismatch")
    for row in divisare.execute(
        "SELECT b.building_id,b.name,b.location_country,b.location_city,b.project_year,sa.article_id,sa.name_raw,sa.source_url "
        "FROM buildings b LEFT JOIN building_articles ba ON ba.building_id=b.building_id "
        "LEFT JOIN source_articles sa ON sa.article_id=ba.article_id ORDER BY b.building_id,sa.article_id"
    ):
        b=buildings[("divisare",str(row["building_id"]))]
        b["name"]=str(row["name"] or b["name"]);b["country"]=str(row["location_country"] or b["country"])
        b["locality"]=str(row["location_city"] or b["locality"])
        if row["project_year"] is not None:b["year_min"]=b["year_max"]=row["project_year"]
        if row["name_raw"]:b["aliases"].add(str(row["name_raw"]))
        if row["source_url"]:b["urls"].add(str(row["source_url"]))
        if row["article_id"] is not None:b["external_ids"].add("divisare_article:"+str(row["article_id"]))
    for row in divisare.execute(
        "SELECT ba.building_id,aa.architect_name,sa.slug,sa.name FROM building_articles ba "
        "JOIN article_architects aa ON aa.article_id=ba.article_id LEFT JOIN source_architects sa ON sa.architect_id=aa.architect_id"
    ):
        buildings[("divisare",str(row["building_id"]))]["firms"].update(str(x) for x in (row["architect_name"],row["slug"],row["name"]) if x)
    for row in architizer.execute(
        "SELECT b.building_id,b.preferred_name,b.location_country,b.location_city,b.completion_year,"
        "sp.source_project_id,sp.name,sp.source_url,sp.global_id FROM buildings b "
        "LEFT JOIN building_projects bp ON bp.building_id=b.building_id "
        "LEFT JOIN source_projects sp ON sp.source_project_id=bp.source_project_id ORDER BY b.building_id,sp.source_project_id"
    ):
        b=buildings[("architizer",str(row["building_id"]))]
        b["name"]=str(row["preferred_name"] or b["name"]);b["country"]=str(row["location_country"] or b["country"])
        b["locality"]=str(row["location_city"] or b["locality"])
        if row["completion_year"] is not None:b["year_min"]=b["year_max"]=row["completion_year"]
        if row["name"]:b["aliases"].add(str(row["name"]))
        if row["source_url"]:b["urls"].add(str(row["source_url"]))
        if row["global_id"]:b["external_ids"].add("architizer_global:"+str(row["global_id"]))
    for row in architizer.execute(
        "SELECT bp.building_id,pf.raw_firm_name,pf.source_firm_slug,sf.source_name FROM building_projects bp "
        "JOIN project_firms pf ON pf.source_project_id=bp.source_project_id LEFT JOIN source_firms sf ON sf.source_firm_slug=pf.source_firm_slug"
    ):
        buildings[("architizer",str(row["building_id"]))]["firms"].update(str(x) for x in (row["raw_firm_name"],row["source_firm_slug"],row["source_name"]) if x)
    for b in buildings.values():
        b["firms"].update(str(x) for x in (b["e2_metadata"].get("firm_name"),b["e2_metadata"].get("firm_slug")) if x)
        b["aliases"]=tuple(sorted(x for x in b["aliases"] if x));b["alias_norms"]=tuple(sorted({_norm(x,ascii_fold=True) for x in b["aliases"] if x}))
        b["firms"]=tuple(sorted(b["firms"]));b["firm_keys"]=_firm_keys(b["firms"])
        b["country_norm"]=_country(b["country"]);b["locality_norm"]=_norm(b["locality"],ascii_fold=True)
        b["urls"]=tuple(sorted(b["urls"]));b["external_ids"]=tuple(sorted(b["external_ids"]))
    return buildings


def _image_stats(e2:sqlite3.Connection)->dict[tuple[str,str],dict[str,Any]]:
    states:dict[tuple[str,str],dict[str,Any]]=defaultdict(lambda:{"asset":0,"success":0,"pixels":set(),"rep":None})
    for row in e2.execute(
        "SELECT ba.source,ba.source_building_id,ba.source_asset_id,a.fingerprint_status,a.canonical_url,a.fetch_url,a.final_url,"
        "a.raw_response_sha256,a.normalized_pixel_sha256,a.phash_hex,a.original_width,a.original_height,a.source_record_sha256 "
        "FROM building_assets ba JOIN assets a ON a.run_id=ba.run_id AND a.source=ba.source AND a.source_asset_id=ba.source_asset_id "
        "ORDER BY ba.source,ba.source_building_id,ba.source_asset_id"
    ):
        key=(str(row["source"]),str(row["source_building_id"]));state=states[key];state["asset"]+=1
        if row["fingerprint_status"]=="success":state["success"]+=1;state["pixels"].add(str(row["normalized_pixel_sha256"]))
        width,height=int(row["original_width"] or 0),int(row["original_height"] or 0)
        score=(0 if row["fingerprint_status"]=="success" else 1,-(width*height),-min(width,height),str(row["source_asset_id"]))
        if state["rep"] is None or score<state["rep"][0]:
            state["rep"]=(score,{"source_asset_id":str(row["source_asset_id"]),"fingerprint_status":str(row["fingerprint_status"]),
                "canonical_url":row["canonical_url"],"fetch_url":row["fetch_url"],"final_url":row["final_url"],
                "raw_response_sha256":row["raw_response_sha256"],"normalized_pixel_sha256":row["normalized_pixel_sha256"],
                "phash_hex":row["phash_hex"],"original_width":row["original_width"],"original_height":row["original_height"],
                "e2_asset_record_sha256":row["source_record_sha256"]})
    return {key:{"asset_count":s["asset"],"successful_asset_count":s["success"],"unique_pixel_count":len(s["pixels"]),"representative":s["rep"][1] if s["rep"] else None} for key,s in states.items()}


def _universe(buildings:Mapping[tuple[str,str],Mapping[str,Any]],e2:sqlite3.Connection)->tuple[dict[tuple[str,str],set[str]],dict[tuple[str,str],dict[str,Any]]]:
    origins:dict[tuple[str,str],set[str]]=defaultdict(set);summaries:dict[tuple[str,str],dict[str,Any]]={}
    metadata=list(e2.execute("SELECT * FROM metadata_building_pairs ORDER BY metadata_pair_id"))
    candidates=list(e2.execute("SELECT * FROM cross_source_building_candidates ORDER BY building_candidate_id"))
    if len(metadata)!=EXPECTED_COUNTS["metadata"] or len(candidates)!=EXPECTED_COUNTS["e2_candidates"]:raise ValueError("E2 candidate input count mismatch")
    for row in metadata:
        pair=_endpoint(row);origins[pair].add("e2_metadata");summaries.setdefault(pair,{}).update(metadata_pair_id=str(row["metadata_pair_id"]))
    for row in candidates:
        pair=_endpoint(row);origins[pair].add("e2_candidate")
        summaries.setdefault(pair,{}).update(e2_building_candidate_id=str(row["building_candidate_id"]),metadata_pair_id=row["metadata_pair_id"],
            exact=int(row["exact_asset_pair_count"]),identical=int(row["identical_phash_pair_count"]),le8=int(row["phash_le8_pair_count"]),
            weak=int(row["phash_9_16_pair_count"]),min_distance=row["min_phash_distance"])
    divs=[b for key,b in buildings.items() if key[0]=="divisare"];atzs=[b for key,b in buildings.items() if key[0]=="architizer"]
    firm_index:dict[str,list[Mapping[str,Any]]]=defaultdict(list)
    for left in divs:
        for key in left["firm_keys"]:firm_index[key].append(left)
    for right in atzs:
        seen={str(left["id"]):left for key in right["firm_keys"] for left in firm_index.get(key,())}
        for left in seen.values():
            gap=_year_gap(left,right)
            if gap is None or gap<=YEAR_BLOCK_TOLERANCE:origins[(str(left["id"]),str(right["id"]))].add("matcher_architect_year")
    geo:dict[tuple[str,int,str],set[str]]=defaultdict(set);div_by_id={str(b["id"]):b for b in divs}
    for left in divs:
        if left["country_norm"] and left["year_min"] is not None:
            for token in _tokens(str(left["name"])):geo[(str(left["country_norm"]),int(left["year_min"]),token)].add(str(left["id"]))
    for right in atzs:
        if not right["country_norm"] or right["year_min"] is None:continue
        hits:Counter[str]=Counter()
        for year in range(int(right["year_min"])-1,int(right["year_min"])+2):
            for token in _tokens(str(right["name"])):
                for left_id in geo.get((str(right["country_norm"]),year,token),()):hits[left_id]+=1
        for left_id,count in hits.items():
            if count>=PASS2_TOKEN_OVERLAP and _sim(_norm(div_by_id[left_id]["name"],ascii_fold=True),_norm(right["name"],ascii_fold=True))[0]>=NAME_THRESHOLD:
                origins[(left_id,str(right["id"]))].add("matcher_pass2")
    return dict(origins),summaries


def _decision(left:Mapping[str,Any],right:Mapping[str,Any],summary:Mapping[str,Any],left_stats:Mapping[str,Any],right_stats:Mapping[str,Any])->dict[str,Any]:
    name_strict,name_loose,name_set=_best(left["alias_norms"],right["alias_norms"])
    firm_strict,_firm_loose,_firm_set=_best(left["firm_keys"],right["firm_keys"])
    firm_positive=bool(set(left["firm_keys"])&set(right["firm_keys"])) or firm_strict>=FIRM_THRESHOLD
    gap=_year_gap(left,right);strong=int(summary.get("exact",0))+int(summary.get("identical",0))+int(summary.get("le8",0))
    left_n=int(left_stats.get("unique_pixel_count",0));right_n=int(right_stats.get("unique_pixel_count",0));veto=False
    if not strong and left_n>=PHASH_STRONG_MIN and right_n>=PHASH_STRONG_MIN:veto=True
    elif not strong and left_n>=PHASH_THIN_MIN and right_n>=PHASH_THIN_MIN:
        primary_set=float(fuzz.token_set_ratio(_norm(left["name"],ascii_fold=True),_norm(right["name"],ascii_fold=True)))
        veto=primary_set<NAME_THRESHOLD and gap is not None and gap>PASS2_YEAR_TOLERANCE
    conflicts:set[str]=set()
    if left["country_norm"] and right["country_norm"] and left["country_norm"]!=right["country_norm"]:conflicts.add("country_site_incompatible")
    if firm_positive and gap is not None and gap>YEAR_BLOCK_TOLERANCE:conflicts.add("matcher_architect_year_gate_block")
    if gap is not None and gap>PHASE_HARD_GAP:conflicts.add("project_phase_incompatible")
    if veto:conflicts.add("verified_phash_veto")
    site_positive=bool(left["country_norm"] and left["country_norm"]==right["country_norm"] and left["locality_norm"] and left["locality_norm"]==right["locality_norm"])
    statuses={
        "name_alias_identity":"positive" if name_strict>=NAME_THRESHOLD else "unknown",
        "architect_firm_identity":"positive" if firm_positive else "unknown",
        "location_site_identity":"negative" if "country_site_incompatible" in conflicts else ("positive" if site_positive else "unknown"),
        "year_phase_compatibility":"negative" if gap is not None and gap>PHASE_HARD_GAP else ("positive" if gap is not None and gap<=YEAR_POSITIVE_TOLERANCE else "unknown"),
        "source_url_external_stable_id":"unknown",
        "verified_pixel_image_overlap":"negative" if veto else ("positive" if strong else "unknown"),
    }
    positive=sum(value=="positive" for value in statuses.values());negative=sum(value=="negative" for value in statuses.values())
    if not conflicts and positive>=2:proposal="merge_candidate"
    elif conflicts:
        related=name_strict>=NAME_THRESHOLD and (firm_positive or site_positive) and bool(conflicts&{"matcher_architect_year_gate_block","project_phase_incompatible"}) and "country_site_incompatible" not in conflicts
        proposal="related_but_separate" if related else "reject"
    else:proposal="defer"
    return {"name_strict":name_strict,"name_loose":name_loose,"name_set":name_set,"firm_strict":firm_strict,"gap":gap,
        "strong":strong,"statuses":statuses,"positive":positive,"negative":negative,"conflicts":conflicts,"proposal":proposal,
        "auto":int(positive>=2 and not conflicts)}


def _logical(connection:sqlite3.Connection,run_id:str)->str:
    digest=hashlib.sha256();digest.update((LOGICAL_VERSION+"\n").encode())
    for table,order in LOGICAL_TABLES:
        digest.update((table+"\n").encode())
        for row in connection.execute(f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}",(run_id,)):
            digest.update((str(row[0])+"\n").encode("ascii"))
    return digest.hexdigest()


def _expected_sample(evaluated:Mapping[str,Mapping[str,Any]])->list[tuple[str,str]]:
    selected:list[tuple[str,dict[str,Any]]]=[];used_left:set[str]=set();used_right:set[str]=set()
    def add(stratum:str,count:int,predicate:Any,preference:Any)->None:
        pool=[row for row in evaluated.values() if predicate(row)]
        pool.sort(key=lambda row:(preference(row),_stable(SAMPLE_SEED,stratum,str(row["candidate_id"]))))
        for row in pool:
            if row["left_id"] in used_left or row["right_id"] in used_right:continue
            selected.append((stratum,row));used_left.add(row["left_id"]);used_right.add(row["right_id"])
            if sum(item[0]==stratum for item in selected)==count:return
        raise ValueError("sample stratum insufficient: "+stratum)
    add("strong_merge",4,lambda r:r["proposal"]=="merge_candidate" and r["strong"]>0 and r["name_strict"]>=90,lambda r:(-r["positive"],-r["strong"]))
    add("metadata_only",2,lambda r:"e2_metadata" in r["origins"] and r["strong"]==0,lambda r:(-r["positive"],len(r["conflicts"])))
    add("image_evidence_only",2,lambda r:"e2_candidate" in r["origins"] and "e2_metadata" not in r["origins"] and r["strong"]>0,lambda r:(int("matcher_architect_year" in r["origins"] or "matcher_pass2" in r["origins"]),-r["strong"]))
    add("conflict_boundary",2,lambda r:bool(r["conflicts"]) or (r["proposal"]=="defer" and r["positive"]==1),lambda r:(0 if r["conflicts"] else 1,len(r["conflicts"]),-r["positive"]))
    return [(stratum,str(row["candidate_id"])) for stratum,row in selected]


def validate_identity_candidates(
    *,repo_root:Path,artifact_path:Path=DEFAULT_ARTIFACT,report_path:Path|None=DEFAULT_REPORT,
    verify_full_hashes:bool=True,
)->ValidationResult:
    root=repo_root.resolve();artifact=artifact_path if artifact_path.is_absolute() else root/artifact_path
    report=None if report_path is None else (report_path if report_path.is_absolute() else root/report_path)
    checks:list[Check]=[];metrics:dict[str,Any]={};run_id: str|None=None
    if not artifact.is_file():
        _check(checks,"artifact_exists",True,False)
        return ValidationResult(artifact,report,None,tuple(checks),metrics)
    connection=_open(artifact);divisare=_open(root/DIVISARE);architizer=_open(root/ARCHITIZER);e2=_open(root/E2)
    try:
        _check(checks,"sqlite_application_id",APPLICATION_ID,int(connection.execute("PRAGMA application_id").fetchone()[0]))
        _check(checks,"sqlite_schema_version",SCHEMA_VERSION,int(connection.execute("PRAGMA user_version").fetchone()[0]))
        tables={str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _check(checks,"required_tables",True,REQUIRED_TABLES<=tables)
        forbidden=sorted(table for table in tables if any(fragment in table.casefold() for fragment in FORBIDDEN_TABLE_FRAGMENTS))
        _check(checks,"no_registry_component_crosswalk_tables",[],forbidden)
        run_rows=connection.execute("SELECT * FROM runs").fetchall();_check(checks,"single_run",1,len(run_rows))
        if len(run_rows)!=1:return ValidationResult(artifact,report,None,tuple(checks),metrics)
        run=run_rows[0];run_id=str(run["run_id"])
        _check(checks,"builder_version",BUILDER_VERSION,str(run["builder_version"]))
        _check(checks,"normalizer_version",NORMALIZER_VERSION,str(run["normalizer_version"]))
        _check(checks,"policy_version",POLICY_VERSION,str(run["policy_version"]))
        _check(checks,"sample_contract",(SAMPLE_VERSION,SAMPLE_SEED),(str(run["sample_version"]),str(run["sample_seed"])))
        _check(checks,"normalizer_spec_sha",_sha(NORMALIZER_SPEC),str(run["normalizer_sha256"]))
        _check(checks,"policy_spec_sha",_sha(POLICY_SPEC),str(run["policy_sha256"]))
        forbidden_counts=(int(run["stage_a_network_requests"]),int(run["stage_a_vision_requests"]),int(run["stage_a_llm_requests"]),int(run["legacy_snapshot_reads"]),int(run["merge_executions"]),int(run["vnx_id_issuances"]))
        _check(checks,"stage_a_forbidden_actions_zero",(0,0,0,0,0,0),forbidden_counts)
        _check(checks,"no_vnx_ids_in_artifact",0,int(connection.execute("SELECT count(*) FROM source_buildings WHERE source_building_id LIKE 'vnx_bld_%'").fetchone()[0]))

        actual_pins={str(row["role"]):row for row in connection.execute("SELECT * FROM input_pins WHERE run_id=?",(run_id,))}
        pin_failures=[]
        for role,(relative,size,digest) in INPUT_SPECS.items():
            path=root/relative
            actual_digest=_sha_file(path) if verify_full_hashes else digest
            row=actual_pins.get(role)
            if row is None or path.stat().st_size!=size or actual_digest!=digest or row["sha256_before"]!=digest or row["sha256_after"]!=digest:
                pin_failures.append(role)
        _check(checks,"input_file_pins_pre_post",[],pin_failures)
        legacy_paths=[str(row[0]) for row in connection.execute("SELECT path FROM input_pins WHERE lower(path) LIKE '%legacy%'")]
        _check(checks,"legacy_snapshot_not_an_input",[],legacy_paths)

        buildings=_load_buildings(divisare,architizer,e2);stats=_image_stats(e2);universe,summaries=_universe(buildings,e2)
        _check(checks,"source_population_recomputed",EXPECTED_COUNTS["total"],len(buildings))
        artifact_source_count=int(connection.execute("SELECT count(*) FROM source_buildings WHERE run_id=?",(run_id,)).fetchone()[0])
        _check(checks,"artifact_source_population",EXPECTED_COUNTS["total"],artifact_source_count)
        stored_sources={(str(row["source"]),str(row["source_building_id"])):row for row in connection.execute("SELECT * FROM source_buildings WHERE run_id=?",(run_id,))}
        stored_stats={(str(row["source"]),str(row["source_building_id"])):row for row in connection.execute("SELECT * FROM building_image_stats WHERE run_id=?",(run_id,))}
        source_failures=[];stats_failures=[]
        for key,b in buildings.items():
            row=stored_sources.get(key)
            expected=(b["name"],_json(list(b["aliases"])),_json(list(b["firms"])),_json(list(b["firm_keys"])),b["country"],b["country_norm"],b["locality"],b["locality_norm"],b["year_min"],b["year_max"],_json(list(b["urls"])),_json(list(b["external_ids"])),b["e2_sha"])
            actual=None if row is None else tuple(row[column] for column in ("name","aliases_json","firms_json","firm_keys_json","country","country_normalized","locality","locality_normalized","completion_year_min","completion_year_max","source_urls_json","external_ids_json","e2_source_record_sha256"))
            if actual!=expected and len(source_failures)<10:source_failures.append(f"{key[0]}/{key[1]}")
            source_stat=stats.get(key,{"asset_count":0,"successful_asset_count":0,"unique_pixel_count":0,"representative":None})
            stat_row=stored_stats.get(key)
            stat_actual=None if stat_row is None else (stat_row["asset_count"],stat_row["successful_asset_count"],stat_row["unique_pixel_count"],json.loads(stat_row["representative_json"]) if stat_row["representative_json"] else None)
            stat_expected=(source_stat["asset_count"],source_stat["successful_asset_count"],source_stat["unique_pixel_count"],source_stat["representative"])
            if stat_actual!=stat_expected and len(stats_failures)<10:stats_failures.append(f"{key[0]}/{key[1]}")
        _check(checks,"source_rows_replay",[],source_failures)
        _check(checks,"image_stats_replay",[],stats_failures)

        artifact_pairs={(str(row["left_source_building_id"]),str(row["right_source_building_id"])):row for row in connection.execute("SELECT * FROM candidate_pairs WHERE run_id=?",(run_id,))}
        stored_origins_by_candidate:dict[str,set[str]]=defaultdict(set)
        for origin_row in connection.execute("SELECT candidate_id,origin FROM candidate_origins WHERE run_id=?",(run_id,)):
            stored_origins_by_candidate[str(origin_row["candidate_id"])].add(str(origin_row["origin"]))
        stored_family_by_candidate:dict[str,dict[str,str]]=defaultdict(dict)
        for family_row in connection.execute("SELECT candidate_id,family,status FROM evidence_families WHERE run_id=?",(run_id,)):
            stored_family_by_candidate[str(family_row["candidate_id"])][str(family_row["family"])]=str(family_row["status"])
        stored_conflicts_by_candidate:dict[str,set[str]]=defaultdict(set)
        for conflict_row in connection.execute("SELECT candidate_id,conflict_type FROM hard_conflicts WHERE run_id=?",(run_id,)):
            stored_conflicts_by_candidate[str(conflict_row["candidate_id"])].add(str(conflict_row["conflict_type"]))
        _check(checks,"candidate_universe_exact",set(universe),set(artifact_pairs),f"expected={len(universe)} actual={len(artifact_pairs)}")
        origin_failures=[];decision_failures=[];family_failures=[];conflict_failures=[];evaluated:dict[str,dict[str,Any]]={}
        for pair,origins in universe.items():
            row=artifact_pairs.get(pair)
            if row is None:continue
            cid=_candidate_id(*pair);summary=summaries.get(pair,{})
            stored_origins=stored_origins_by_candidate.get(cid,set())
            if str(row["candidate_id"])!=cid or stored_origins!=origins:
                if len(origin_failures)<10:origin_failures.append(cid)
            left=buildings[("divisare",pair[0])];right=buildings[("architizer",pair[1])]
            decision=_decision(left,right,summary,stats.get(("divisare",pair[0]),{}),stats.get(("architizer",pair[1]),{}))
            expected_decision=(round(decision["name_strict"],6),round(decision["name_loose"],6),round(decision["name_set"],6),round(decision["firm_strict"],6),decision["gap"],decision["positive"],decision["negative"],len(decision["conflicts"]),decision["proposal"],decision["auto"])
            actual_decision=tuple(row[column] for column in ("name_strict_score","name_loose_score","name_token_set_score","firm_strict_score","year_gap","positive_family_count","negative_family_count","hard_conflict_count","proposal","auto_merge_eligible"))
            if actual_decision!=expected_decision and len(decision_failures)<10:decision_failures.append(cid)
            stored_status=stored_family_by_candidate.get(cid,{})
            if stored_status!=decision["statuses"] and len(family_failures)<10:family_failures.append(cid)
            stored_conflicts=stored_conflicts_by_candidate.get(cid,set())
            if stored_conflicts!=decision["conflicts"] and len(conflict_failures)<10:conflict_failures.append(cid)
            evaluated[cid]={**decision,"candidate_id":cid,"left_id":pair[0],"right_id":pair[1],"origins":origins}
        _check(checks,"candidate_origins_replay",[],origin_failures)
        _check(checks,"candidate_decisions_replay",[],decision_failures)
        _check(checks,"six_family_status_replay",[],family_failures)
        _check(checks,"hard_conflicts_replay",[],conflict_failures)
        _check(checks,"six_rows_per_candidate",len(universe)*6,int(connection.execute("SELECT count(*) FROM evidence_families WHERE run_id=?",(run_id,)).fetchone()[0]))
        _check(checks,"auto_merge_has_zero_hard_conflict",0,int(connection.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1 AND hard_conflict_count<>0",(run_id,)).fetchone()[0]))

        e2_direct=int(e2.execute("SELECT count(*) FROM candidate_image_evidence").fetchone()[0]);artifact_direct=int(connection.execute("SELECT count(*) FROM direct_image_evidence WHERE run_id=?",(run_id,)).fetchone()[0])
        _check(checks,"direct_image_evidence_count",e2_direct,artifact_direct)
        e2_candidates_by_id={str(row["building_candidate_id"]):row for row in e2.execute("SELECT * FROM cross_source_building_candidates")}
        e2_evidence_by_id={str(row["evidence_id"]):row for row in e2.execute("SELECT * FROM candidate_image_evidence")}
        direct_failures=[]
        for row in connection.execute("SELECT * FROM direct_image_evidence WHERE run_id=?",(run_id,)):
            source=e2_evidence_by_id.get(str(row["e2_evidence_id"]))
            if source is None:
                if len(direct_failures)<10:direct_failures.append(str(row["e2_evidence_id"]));continue
            pair_row=e2_candidates_by_id[str(source["building_candidate_id"])];pair=_endpoint(pair_row)
            expected_assets=(source["left_source_asset_id"],source["right_source_asset_id"]) if source["left_source"]=="divisare" else (source["right_source_asset_id"],source["left_source_asset_id"])
            if (row["candidate_id"],row["evidence_kind"],row["left_source_asset_id"],row["right_source_asset_id"],row["phash_distance"],row["e2_evidence_record_sha256"])!=(_candidate_id(*pair),source["evidence_kind"],*expected_assets,source["phash_distance"],source["evidence_record_sha256"]):
                if len(direct_failures)<10:direct_failures.append(str(row["e2_evidence_id"]))
        _check(checks,"direct_image_evidence_replay",[],direct_failures)

        expected_sample=_expected_sample(evaluated)
        actual_sample=[(str(row["stratum"]),str(row["candidate_id"])) for row in connection.execute("SELECT stratum,candidate_id FROM sample_neighbourhoods WHERE run_id=? ORDER BY sample_rank",(run_id,))]
        _check(checks,"frozen_n10_exact",expected_sample,actual_sample)
        strata=Counter(x[0] for x in actual_sample);_check(checks,"n10_strata",{"strong_merge":4,"metadata_only":2,"image_evidence_only":2,"conflict_boundary":2},dict(strata))
        endpoint_count=len({evaluated[cid]["left_id"] for _s,cid in actual_sample})+len({evaluated[cid]["right_id"] for _s,cid in actual_sample})
        _check(checks,"n10_distinct_endpoints",20,endpoint_count)

        logical=_logical(connection,run_id);_check(checks,"stage_a_logical_sha",logical,str(run["stage_a_logical_sha256"]))
        integrity=str(connection.execute("PRAGMA integrity_check").fetchone()[0]);_check(checks,"sqlite_integrity","ok",integrity)
        stage_b_status=str(run["stage_b_status"]);metrics.update(source_buildings=len(buildings),candidate_pairs=len(universe),auto_merge_eligible=sum(x["auto"] for x in evaluated.values()),stage_b_status=stage_b_status)
        if stage_b_status in {"complete","reviewed","adjudicated","pass","fail"} or str(run["status"]) in {"stage_b_complete","pass","fail"}:
            review_assets=int(connection.execute("SELECT count(*) FROM review_assets WHERE run_id=?",(run_id,)).fetchone()[0])
            pending=int(connection.execute("SELECT count(*) FROM review_assets WHERE run_id=? AND status='pending'",(run_id,)).fetchone()[0])
            _check(checks,"stage_b_assets_present",True,review_assets>0);_check(checks,"stage_b_assets_terminal",0,pending)
            _check(checks,"stage_b_paid_calls_zero",(0,0),(int(run["stage_b_vision_requests"]),int(run["stage_b_llm_requests"])))
        if str(run["blind_review_status"])=="complete":
            _check(checks,"blind_review_n10",10,int(connection.execute("SELECT count(*) FROM blind_reviews WHERE run_id=?",(run_id,)).fetchone()[0]))
        if str(run["adjudication_status"])=="complete":
            _check(checks,"adjudication_n10",10,int(connection.execute("SELECT count(*) FROM adjudications WHERE run_id=?",(run_id,)).fetchone()[0]))
            false_merge=int(connection.execute("SELECT count(*) FROM adjudications a JOIN sample_neighbourhoods s USING(run_id,opaque_review_id) JOIN candidate_pairs c USING(run_id,candidate_id) WHERE a.run_id=? AND a.reviewer_label='different' AND c.auto_merge_eligible=1",(run_id,)).fetchone()[0])
            hard_violation=int(connection.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1 AND hard_conflict_count>0",(run_id,)).fetchone()[0])
            _check(checks,"gate_false_merge_zero",0,false_merge);_check(checks,"gate_hard_conflict_violation_zero",0,hard_violation)
    finally:
        connection.close();divisare.close();architizer.close();e2.close()
    result=ValidationResult(artifact,report,run_id,tuple(checks),metrics)
    if report is not None:
        report.parent.mkdir(parents=True,exist_ok=True)
        if report.exists():raise FileExistsError(f"refusing to clobber validation report: {report}")
        payload={"status":"PASS" if result.passed else "FAIL","validator_version":VALIDATOR_VERSION,"artifact_path":str(artifact),"run_id":run_id,
            "passed_checks":sum(row.passed for row in checks),"total_checks":len(checks),"failed_checks":list(result.failed_check_names),
            "checks":[{"name":row.name,"passed":row.passed,"expected":_json_safe(row.expected),"actual":_json_safe(row.actual),"detail":row.detail} for row in checks],
            "metrics":_json_safe(metrics),"validated_at":datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z")}
        temporary=report.with_name(report.name+".building")
        temporary.write_text(json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
        temporary.replace(report)
    return result


__all__=["DEFAULT_ARTIFACT","DEFAULT_REPORT","VALIDATOR_VERSION","Check","ValidationResult","validate_identity_candidates"]
