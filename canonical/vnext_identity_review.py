"""WP2 Stage B: bounded HTTP pixels, blind N10 review, and adjudication.

No Vision or LLM client is imported.  The module freezes at most two assets per
side for each of the ten Stage-A neighbourhoods, reuses the WP1 pixel cache
before HTTP, renders score/proposal-blind sheets, then records reviewer and
adjudicator decisions without ever executing a merge.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_fetch import FetchFailure, FetchPayload, fetch_once
from canonical.divisare_vision_benchmark import decode_source, prepare_derivative
from canonical.image_fingerprint import FingerprintError, fingerprint_bytes


STAGE_B_VERSION = "vnext-identity-http-blind-review-n10-v1"
ASSET_SELECTION_VERSION = "vnext-identity-two-assets-per-side-v1"
TRANSFORM_VERSION = "vnext-identity-review-1024-jpeg-q92-s0-v1"
WP1_TRANSFORM_VERSION = "vnext-p4b-shared-1024-jpeg-q92-s0-v1"
BLIND_REVIEW_VERSION = "vnext-identity-blind-review-n10-v1"
ADJUDICATION_VERSION = "vnext-identity-adjudication-n10-v1"
GATE_VERSION = "vnext-identity-lane-c-gate-v1"
MAX_ASSETS_PER_SIDE = 2
MAX_LOGICAL_FETCHES = 40
MAX_HTTP_REQUESTS_WITH_REDIRECTS = 160
TERMINAL_ASSET_STATUSES = frozenset({"cache_hit", "exact", "delivery_changed", "source_changed", "failed"})

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_candidates_v1.db")
DEFAULT_E2 = Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db")
DEFAULT_WP1_CACHE = Path("data/enrichment/vnext_p4b_pixel_cache_v1")
DEFAULT_CACHE = Path("data/enrichment/vnext_identity_review_cache_v1")
DEFAULT_REVIEW_PREFIX = Path("data/reports/vnext_identity_blind_review_n10_v1")
DEFAULT_REVIEW_FORM = Path("data/reports/vnext_identity_blind_review_n10_v1_review.json")
DEFAULT_ADJUDICATION_PACKET = Path("data/reports/vnext_identity_n10_v1_adjudication_packet.json")
DEFAULT_ADJUDICATION_FORM = Path("data/reports/vnext_identity_n10_v1_adjudication.json")
DEFAULT_STAGE_B_MANIFEST = Path("data/reports/vnext_identity_review_n10_v1_manifest.json")
DEFAULT_STAGE_B_REPORT = Path("data/reports/vnext_identity_review_n10_v1_stage_b_report.json")
DEFAULT_GATE_REPORT = Path("data/reports/vnext_identity_candidates_v1_n10_gate.json")

E2_SPEC = (DEFAULT_E2, 10_164_682_752, "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19")
E1_SPECS = {
    "divisare": (
        Path("data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db"),
        2_646_114_304,
        "869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819",
    ),
    "architizer": (
        Path("data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db"),
        4_373_962_752,
        "58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937",
    ),
}

STAGE_B_SCHEMA = """
CREATE TABLE IF NOT EXISTS stage_b_input_pins(
  run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, sha256_before TEXT NOT NULL, sha256_after TEXT,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,role)
);
CREATE TABLE IF NOT EXISTS stage_b_manifest(
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id), stage_b_version TEXT NOT NULL,
  asset_selection_version TEXT NOT NULL, transform_version TEXT NOT NULL,
  blind_review_version TEXT NOT NULL, adjudication_version TEXT NOT NULL,
  max_assets_per_side INTEGER NOT NULL, max_logical_fetches INTEGER NOT NULL,
  max_http_requests_with_redirects INTEGER NOT NULL,
  stage_a_artifact_sha256_before TEXT NOT NULL, stage_a_logical_sha256 TEXT NOT NULL,
  initialized_at TEXT NOT NULL, completed_at TEXT, record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_sheets(
  run_id TEXT NOT NULL, opaque_review_id TEXT NOT NULL, sheet_path TEXT NOT NULL,
  sheet_sha256 TEXT NOT NULL, sheet_bytes INTEGER NOT NULL,
  review_version TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,opaque_review_id),
  FOREIGN KEY(run_id,opaque_review_id) REFERENCES sample_neighbourhoods(run_id,opaque_review_id)
);
"""


class IdentityReviewError(RuntimeError):
    """Raised when Stage B lineage, cache, review, or gate invariants fail."""


@dataclass(frozen=True)
class FetchResult:
    key: tuple[str, str, int]
    status: str
    actual_final_url: str | None
    actual_raw_sha256: str | None
    actual_pixel_sha256: str | None
    http_status: int | None
    response_bytes: int | None
    request_count: int
    error_kind: str | None
    error_message: str | None
    body: bytes | None


@dataclass(frozen=True)
class StageBResult:
    artifact_path: Path
    run_id: str
    status: str
    selected_asset_count: int
    cache_hit_count: int
    logical_fetch_count: int
    http_request_count: int
    pixel_status_counts: Mapping[str, int]
    review_sheet_paths: tuple[Path, ...]
    review_form_path: Path
    elapsed_seconds: float


@dataclass(frozen=True)
class GateResult:
    artifact_path: Path
    run_id: str
    status: str
    false_merge_count: int
    hard_conflict_violation_count: int
    disagreement_count: int
    report_path: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _record(value: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(value))


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _assert_no_sidecars(path: Path) -> None:
    present = [str(Path(str(path) + suffix)) for suffix in ("-wal", "-shm", "-journal") if Path(str(path) + suffix).exists()]
    if present:
        raise IdentityReviewError("forbidden SQLite sidecar(s): " + ", ".join(present))


def _atomic_bytes(path: Path, raw: bytes, *, allow_identical: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if allow_identical and path.read_bytes() == raw:
            return
        raise IdentityReviewError(f"refusing to clobber artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise IdentityReviewError(f"stale temporary artifact: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _cache_relative(root: Path, cache_root: Path, pixel_sha: str, transform_version: str) -> Path:
    transform_key = hashlib.sha256(transform_version.encode("utf-8")).hexdigest()[:12]
    return cache_root / pixel_sha[:2] / f"{pixel_sha}.{transform_key}.jpg"


def _stage_a_logical(connection: sqlite3.Connection, run_id: str) -> str:
    tables = (
        ("input_pins", "role"), ("source_buildings", "source,source_building_id"),
        ("building_image_stats", "source,source_building_id"),
        ("candidate_pairs", "left_source_building_id,right_source_building_id"),
        ("candidate_origins", "candidate_id,origin"), ("evidence_families", "candidate_id,family"),
        ("hard_conflicts", "candidate_id,conflict_type"), ("direct_image_evidence", "e2_evidence_id"),
        ("sample_neighbourhoods", "sample_rank"), ("sample_neighbourhood_pairs", "opaque_review_id,display_order"),
    )
    digest = hashlib.sha256()
    digest.update(b"vnext-identity-stage-a-logical-v1\n")
    for table, order in tables:
        digest.update((table + "\n").encode("utf-8"))
        for row in connection.execute(f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _e1_row(connection: sqlite3.Connection, source_asset_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT sa.run_id,sa.fetch_url,sa.source_record_sha256,f.status,f.raw_response_sha256,"
        "f.normalized_pixel_sha256,f.selected_attempt_no FROM source_assets sa "
        "JOIN fingerprints f USING(run_id,source_asset_id) "
        "WHERE sa.run_id=(SELECT run_id FROM fingerprint_runs LIMIT 1) AND sa.source_asset_id=?",
        (source_asset_id,),
    ).fetchone()


def _asset_row(e2: sqlite3.Connection, source: str, asset_id: str) -> sqlite3.Row | None:
    return e2.execute(
        "SELECT source,source_asset_id,fingerprint_status,canonical_url,fetch_url,final_url,"
        "raw_response_sha256,normalized_pixel_sha256,phash_hex,original_width,original_height,source_record_sha256 "
        "FROM assets WHERE run_id=(SELECT run_id FROM e2_runs LIMIT 1) AND source=? AND source_asset_id=?",
        (source, asset_id),
    ).fetchone()


def _top_building_assets(e2: sqlite3.Connection, source: str, building_id: str) -> list[str]:
    rows = e2.execute(
        "SELECT ba.source_asset_id,a.original_width,a.original_height FROM building_assets ba JOIN assets a "
        "ON a.run_id=ba.run_id AND a.source=ba.source AND a.source_asset_id=ba.source_asset_id "
        "WHERE ba.run_id=(SELECT run_id FROM e2_runs LIMIT 1) AND ba.source=? AND ba.source_building_id=? AND a.fingerprint_status='success' "
        "ORDER BY (coalesce(a.original_width,0)*coalesce(a.original_height,0)) DESC,"
        "min(coalesce(a.original_width,0),coalesce(a.original_height,0)) DESC,ba.source_asset_id",
        (source, building_id),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _choose_assets_for_sample(
    database: sqlite3.Connection,
    e2: sqlite3.Connection,
    run_id: str,
    candidate_id: str,
) -> dict[str, list[tuple[str, str]]]:
    pair = database.execute(
        "SELECT left_source_building_id,right_source_building_id FROM candidate_pairs WHERE run_id=? AND candidate_id=?",
        (run_id, candidate_id),
    ).fetchone()
    if pair is None:
        raise IdentityReviewError("sample candidate is missing")
    selected: dict[str, list[tuple[str, str]]] = {"left": [], "right": []}
    priorities = {"exact_pixel": 0, "identical_phash": 1, "phash_le8": 2, "phash_9_16": 3}
    direct_rows = [dict(row) for row in database.execute(
        "SELECT * FROM direct_image_evidence WHERE run_id=? AND candidate_id=?",
        (run_id, candidate_id),
    )]
    direct_rows.sort(key=lambda row: (priorities[str(row["evidence_kind"])], int(row["phash_distance"] or 0), str(row["e2_evidence_id"])))
    for index, row in enumerate(direct_rows, 1):
        left_asset, right_asset = str(row["left_source_asset_id"]), str(row["right_source_asset_id"])
        if len(selected["left"]) < MAX_ASSETS_PER_SIDE and left_asset not in {asset for asset, _reason in selected["left"]}:
            selected["left"].append((left_asset, f"direct_evidence_{index}"))
        if len(selected["right"]) < MAX_ASSETS_PER_SIDE and right_asset not in {asset for asset, _reason in selected["right"]}:
            selected["right"].append((right_asset, f"direct_evidence_{index}"))
        if all(len(values) >= MAX_ASSETS_PER_SIDE for values in selected.values()):
            break
    for side, source, building_id in (
        ("left", "divisare", str(pair[0])), ("right", "architizer", str(pair[1]))
    ):
        if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
            continue
        existing = {asset for asset, _reason in selected[side]}
        for asset_id in _top_building_assets(e2, source, building_id):
            if asset_id in existing:
                continue
            selected[side].append((asset_id, "building_representative"))
            existing.add(asset_id)
            if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
                break
    return selected


def _initialize_stage_b(
    database: sqlite3.Connection,
    e2: sqlite3.Connection,
    e1: Mapping[str, sqlite3.Connection],
    root: Path,
    artifact: Path,
    run_id: str,
    stage_a_artifact_sha: str,
) -> None:
    existing = int(database.execute("SELECT count(*) FROM review_assets WHERE run_id=?", (run_id,)).fetchone()[0])
    if existing:
        if existing > MAX_LOGICAL_FETCHES:
            raise IdentityReviewError("existing review asset manifest exceeds bounded cap")
        return
    samples = database.execute(
        "SELECT sample_rank,candidate_id,opaque_review_id FROM sample_neighbourhoods WHERE run_id=? ORDER BY sample_rank",
        (run_id,),
    ).fetchall()
    if len(samples) != 10:
        raise IdentityReviewError("Stage B requires exactly ten frozen neighbourhoods")
    stage_a_logical = _stage_a_logical(database, run_id)
    expected_logical = str(database.execute("SELECT stage_a_logical_sha256 FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
    if stage_a_logical != expected_logical:
        raise IdentityReviewError("Stage A logical SHA changed before Stage B")
    initialized_at = utc_now()
    manifest_payload = {
        "stage_b_version": STAGE_B_VERSION,
        "asset_selection_version": ASSET_SELECTION_VERSION,
        "transform_version": TRANSFORM_VERSION,
        "blind_review_version": BLIND_REVIEW_VERSION,
        "adjudication_version": ADJUDICATION_VERSION,
        "max_assets_per_side": MAX_ASSETS_PER_SIDE,
        "max_logical_fetches": MAX_LOGICAL_FETCHES,
        "max_http_requests_with_redirects": MAX_HTTP_REQUESTS_WITH_REDIRECTS,
        "stage_a_artifact_sha256_before": stage_a_artifact_sha,
        "stage_a_logical_sha256": stage_a_logical,
        "initialized_at": initialized_at,
    }
    database.execute(
        "INSERT INTO stage_b_manifest VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,STAGE_B_VERSION,ASSET_SELECTION_VERSION,TRANSFORM_VERSION,BLIND_REVIEW_VERSION,
            ADJUDICATION_VERSION,MAX_ASSETS_PER_SIDE,MAX_LOGICAL_FETCHES,MAX_HTTP_REQUESTS_WITH_REDIRECTS,
            stage_a_artifact_sha,stage_a_logical,initialized_at,None,_record(manifest_payload),
        ),
    )
    for sample in samples:
        candidate_id, opaque = str(sample["candidate_id"]), str(sample["opaque_review_id"])
        pair = database.execute(
            "SELECT left_source_building_id,right_source_building_id FROM candidate_pairs WHERE run_id=? AND candidate_id=?",
            (run_id,candidate_id),
        ).fetchone()
        chosen = _choose_assets_for_sample(database,e2,run_id,candidate_id)
        for side, source, building_id in (
            ("left","divisare",str(pair[0])),("right","architizer",str(pair[1]))
        ):
            for asset_order,(asset_id,reason) in enumerate(chosen[side],1):
                e2_asset=_asset_row(e2,source,asset_id);e1_asset=_e1_row(e1[source],asset_id)
                if e2_asset is None or e1_asset is None:
                    status="failed";error_kind="missing_frozen_lineage";error_message="E1/E2 asset lineage is unavailable"
                    fetch_url=e2_asset["fetch_url"] if e2_asset else None
                    expected_raw=e2_asset["raw_response_sha256"] if e2_asset else None
                    expected_pixel=e2_asset["normalized_pixel_sha256"] if e2_asset else None
                else:
                    if (e1_asset[3],e1_asset[4],e1_asset[5]) != ("success",e2_asset["raw_response_sha256"],e2_asset["normalized_pixel_sha256"]):
                        raise IdentityReviewError(f"E1/E2 lineage mismatch: {source}/{asset_id}")
                    status="pending";error_kind=None;error_message=None
                    fetch_url=e1_asset[1];expected_raw=e1_asset[4];expected_pixel=e1_asset[5]
                database.execute(
                    "INSERT INTO review_assets(run_id,opaque_review_id,side,asset_order,source,source_building_id,source_asset_id,"
                    "selection_reason,expected_url,expected_raw_sha256,expected_pixel_sha256,status,error_kind,error_message) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id,opaque,side,asset_order,source,building_id,asset_id,reason,fetch_url,expected_raw,expected_pixel,status,error_kind,error_message),
                )
    count=int(database.execute("SELECT count(*) FROM review_assets WHERE run_id=?",(run_id,)).fetchone()[0])
    if count<20 or count>MAX_LOGICAL_FETCHES:
        raise IdentityReviewError(f"review asset count outside bounded range: {count}")
    database.commit()


def _materialize_current(root: Path, cache_root: Path, pixel_sha: str, body: bytes) -> str:
    decoded=decode_source(body)
    derivative=prepare_derivative(decoded,"vnext_identity_review",1024)
    relative=_cache_relative(root,cache_root,pixel_sha,TRANSFORM_VERSION)
    _atomic_bytes(root/relative,derivative.encoded_bytes,allow_identical=True)
    return relative.as_posix()


def _fetch_asset(row: Mapping[str, Any], fetcher: Callable[..., FetchPayload]) -> FetchResult:
    key=(str(row["opaque_review_id"]),str(row["side"]),int(row["asset_order"]))
    try:
        payload=fetcher(str(row["source"]),str(row["expected_url"]))
        fingerprint=fingerprint_bytes(payload.body)
        if fingerprint.response_sha256!=payload.raw_response_sha256:
            raise IdentityReviewError("fetch/fingerprint raw SHA mismatch")
        if fingerprint.pixel_sha256==row["expected_pixel_sha256"]:
            status="exact" if fingerprint.response_sha256==row["expected_raw_sha256"] else "delivery_changed"
        else:
            status="source_changed"
        return FetchResult(key,status,payload.final_url,fingerprint.response_sha256,fingerprint.pixel_sha256,
            int(payload.http_status),len(payload.body),int(payload.request_count),None,None,payload.body)
    except FetchFailure as exc:
        return FetchResult(key,"failed",exc.final_url,exc.raw_response_sha256,None,exc.http_status,exc.response_bytes,
            int(exc.request_count),exc.kind,str(exc)[:2000],None)
    except (FingerprintError,ValueError,OSError,IdentityReviewError) as exc:
        return FetchResult(key,"failed",None,None,None,None,None,1,"local_fingerprint_failed",str(exc)[:2000],None)


def _font(size: int):
    from PIL import ImageFont
    for path in ("C:/Windows/Fonts/arial.ttf","C:/Windows/Fonts/malgun.ttf","DejaVuSans.ttf"):
        try:return ImageFont.truetype(path,size)
        except OSError:continue
    return ImageFont.load_default()


def _fit_image(raw: bytes, width: int, height: int):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw)) as opened:
        image=ImageOps.exif_transpose(opened).convert("RGB")
    image.thumbnail((width,height),Image.Resampling.LANCZOS)
    canvas=Image.new("RGB",(width,height),"white")
    canvas.paste(image,((width-image.width)//2,(height-image.height)//2))
    return canvas


def _metadata(database: sqlite3.Connection, run_id: str, source: str, building_id: str) -> dict[str, Any]:
    row=database.execute("SELECT * FROM source_buildings WHERE run_id=? AND source=? AND source_building_id=?",(run_id,source,building_id)).fetchone()
    return {
        "source":source,"name":str(row["name"]),"aliases":json.loads(str(row["aliases_json"])),
        "firms":json.loads(str(row["firms_json"])),"country":row["country"],"locality":row["locality"],
        "year_min":row["completion_year_min"],"year_max":row["completion_year_max"],
    }


def _draw_wrapped(draw: Any, text: str, xy: tuple[int,int], width_chars: int, font: Any, fill: Any, spacing: int=5) -> int:
    lines=[]
    for paragraph in str(text).splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph,width=width_chars,break_long_words=False,break_on_hyphens=False) or [""])
    x,y=xy
    for line in lines:
        draw.text((x,y),line,font=font,fill=fill);y+=font.size+spacing
    return y


def _render_sheet(database: sqlite3.Connection, root: Path, run_id: str, opaque: str, path: Path) -> str:
    from PIL import Image, ImageDraw
    sample=database.execute(
        "SELECT s.candidate_id,c.left_source_building_id,c.right_source_building_id FROM sample_neighbourhoods s "
        "JOIN candidate_pairs c USING(run_id,candidate_id) WHERE s.run_id=? AND s.opaque_review_id=?",
        (run_id,opaque),
    ).fetchone()
    left=_metadata(database,run_id,"divisare",str(sample["left_source_building_id"]))
    right=_metadata(database,run_id,"architizer",str(sample["right_source_building_id"]))
    canvas=Image.new("RGB",(1800,1220),(244,246,248));draw=ImageDraw.Draw(canvas)
    title_font=_font(34);heading_font=_font(25);body_font=_font(20);small_font=_font(16)
    draw.text((40,28),f"Blind building identity review · {opaque}",font=title_font,fill=(20,25,30))
    draw.text((40,72),"Scores, proposal, rank, and stratum are hidden. Judge the focal pair only.",font=small_font,fill=(75,80,88))
    for side,meta,x in (("LEFT",left,35),("RIGHT",right,915)):
        draw.rounded_rectangle((x,110,x+850,1040),radius=12,fill="white",outline=(195,200,207),width=2)
        draw.text((x+24,132),f"{side} · {meta['source']}",font=heading_font,fill=(25,45,70))
        year=meta["year_min"] if meta["year_min"]==meta["year_max"] else f"{meta['year_min']}–{meta['year_max']}"
        info=f"Name: {meta['name']}\nFirm: {'; '.join(meta['firms'][:4]) or 'unknown'}\nLocation: {meta['locality'] or 'unknown'}, {meta['country'] or 'unknown'}\nYear: {year if year is not None else 'unknown'}"
        _draw_wrapped(draw,info,(x+24,178),72,body_font,(35,38,43),4)
        assets=database.execute(
            "SELECT * FROM review_assets WHERE run_id=? AND opaque_review_id=? AND side=? ORDER BY asset_order",
            (run_id,opaque,side.casefold()),
        ).fetchall()
        for index,row in enumerate(assets):
            image_x=x+24+index*405;image_y=385
            draw.rectangle((image_x,image_y,image_x+380,image_y+430),fill=(222,225,229),outline=(150,155,163),width=2)
            cache_path=row["cache_relative_path"]
            if cache_path and (root/str(cache_path)).is_file():
                fitted=_fit_image((root/str(cache_path)).read_bytes(),376,390)
                canvas.paste(fitted,(image_x+2,image_y+38))
            else:
                draw.text((image_x+118,image_y+210),"UNAVAILABLE",font=body_font,fill=(95,95,95))
            status=str(row["status"])
            status_text={"cache_hit":"cached verified pixel","exact":"current pixel · exact","delivery_changed":"current pixel · delivery changed","source_changed":"current pixel · SOURCE CHANGED","failed":"fetch unavailable"}.get(status,status)
            color=(155,45,35) if status in {"source_changed","failed"} else (30,95,65)
            draw.text((image_x+8,image_y+8),status_text,font=small_font,fill=color)
        draw.text((x+24,840),"Aliases:",font=small_font,fill=(70,75,80))
        _draw_wrapped(draw,"; ".join(meta["aliases"][:5]) or "none",(x+24,865),90,small_font,(70,75,80),3)
    neighbours=[]
    for row in database.execute(
        "SELECT n.relationship,c.left_source_building_id,c.right_source_building_id FROM sample_neighbourhood_pairs n "
        "JOIN candidate_pairs c USING(run_id,candidate_id) WHERE n.run_id=? AND n.opaque_review_id=? AND n.relationship<>'focal' ORDER BY n.display_order",
        (run_id,opaque),
    ):
        other_source="architizer" if row["relationship"]=="shares_left" else "divisare"
        other_id=str(row["right_source_building_id"] if row["relationship"]=="shares_left" else row["left_source_building_id"])
        meta=_metadata(database,run_id,other_source,other_id)
        neighbours.append(f"{row['relationship'].replace('_',' ')}: {meta['name']} · {meta['locality'] or '?'} / {meta['country'] or '?'} · {meta['year_min'] or '?'}")
    draw.text((40,1070),"Unranked neighbourhood context (metadata only)",font=heading_font,fill=(25,45,70))
    _draw_wrapped(draw,"\n".join(neighbours) if neighbours else "No alternate candidate in the bounded neighbourhood.",(40,1110),150,small_font,(55,60,68),3)
    output=io.BytesIO();canvas.save(output,format="PNG",optimize=False)
    raw=output.getvalue();_atomic_bytes(path,raw)
    return hashlib.sha256(raw).hexdigest()


def _render_review_artifacts(
    database: sqlite3.Connection,
    root: Path,
    run_id: str,
    review_prefix: Path,
    review_form: Path,
) -> tuple[tuple[Path,...],Path]:
    # Opaque-ID order is independent of sample rank/stratum, so page numbering
    # cannot leak the stratification to the reviewer.
    samples=database.execute("SELECT opaque_review_id FROM sample_neighbourhoods WHERE run_id=? ORDER BY opaque_review_id",(run_id,)).fetchall()
    paths=[]
    for index,row in enumerate(samples,1):
        opaque=str(row[0]);relative=review_prefix.with_name(review_prefix.name+f"_{index:02d}.png");path=root/relative
        digest=_render_sheet(database,root,run_id,opaque,path);size=path.stat().st_size
        payload={"opaque_review_id":opaque,"sheet_path":relative.as_posix(),"sheet_sha256":digest,"sheet_bytes":size,"review_version":BLIND_REVIEW_VERSION}
        database.execute("INSERT INTO review_sheets VALUES(?,?,?,?,?,?,?)",(run_id,opaque,relative.as_posix(),digest,size,BLIND_REVIEW_VERSION,_record(payload)))
        paths.append(path)
    form_path=root/review_form
    form={
        "review_version":BLIND_REVIEW_VERSION,"run_id":run_id,
        "blind_fields_hidden":["candidate_id","sample_rank","stratum","origin","scores","proposal","auto_merge_eligible"],
        "reviewer":None,
        "instructions":"Use only the rendered source metadata, direct/current image evidence, and unranked neighbourhood context.",
        "allowed_labels":["same","related","different","insufficient"],
        "items":[{"opaque_review_id":str(row[0]),"label":None,"rationale":None,"hard_conflict_observed":None} for row in samples],
    }
    _atomic_json(form_path,form)
    return tuple(paths),form_path


def run_stage_b(
    *,
    repo_root: Path,
    artifact_path: Path=DEFAULT_ARTIFACT,
    review_prefix: Path=DEFAULT_REVIEW_PREFIX,
    review_form: Path=DEFAULT_REVIEW_FORM,
    manifest_path: Path=DEFAULT_STAGE_B_MANIFEST,
    report_path: Path=DEFAULT_STAGE_B_REPORT,
    workers: int=4,
    verify_full_hashes: bool=True,
    fetcher: Callable[...,FetchPayload]=fetch_once,
    wp1_cache_root: Path=DEFAULT_WP1_CACHE,
    cache_root: Path=DEFAULT_CACHE,
) -> StageBResult:
    started=time.monotonic();root=repo_root.resolve();artifact=artifact_path if artifact_path.is_absolute() else root/artifact_path
    if not artifact.is_file():raise IdentityReviewError("Stage A artifact is missing")
    _assert_no_sidecars(artifact)
    stage_a_artifact_sha=sha256_file(artifact)
    e2_path=root/E2_SPEC[0]
    input_paths={"cross_source_e2_full_v5":E2_SPEC,**{f"{source}_e1_v1_2":spec for source,spec in E1_SPECS.items()}}
    pins={}
    for role,(relative,size,expected) in input_paths.items():
        path=root/relative
        if not path.is_file() or path.stat().st_size!=size:raise IdentityReviewError(f"Stage B input size mismatch: {role}")
        _assert_no_sidecars(path)
        digest=sha256_file(path) if verify_full_hashes else expected
        if digest!=expected:raise IdentityReviewError(f"Stage B input SHA mismatch: {role}")
        pins[role]={"path":relative.as_posix(),"size_bytes":size,"sha256_before":digest,"sha256_after":None}
    e2=_open_immutable(e2_path);e1={source:_open_immutable(root/spec[0]) for source,spec in E1_SPECS.items()}
    database=sqlite3.connect(artifact);database.row_factory=sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE");database.execute("PRAGMA synchronous=FULL");database.execute("PRAGMA foreign_keys=ON")
    try:
        database.executescript(STAGE_B_SCHEMA)
        run_rows=database.execute("SELECT * FROM runs").fetchall()
        if len(run_rows)!=1 or run_rows[0]["status"] not in {"stage_a_complete","stage_b_complete"}:raise IdentityReviewError("artifact is not at a valid Stage B boundary")
        run_id=str(run_rows[0]["run_id"])
        if database.execute("SELECT count(*) FROM review_sheets WHERE run_id=?",(run_id,)).fetchone()[0]:
            raise IdentityReviewError("Stage B review sheets already exist")
        _initialize_stage_b(database,e2,e1,root,artifact,run_id,stage_a_artifact_sha)
        for role,row in sorted(pins.items()):
            payload={"role":role,**row}
            database.execute("INSERT OR REPLACE INTO stage_b_input_pins VALUES(?,?,?,?,?,?,?)",(run_id,role,row["path"],row["size_bytes"],row["sha256_before"],None,_record(payload)))
        pending=[dict(row) for row in database.execute("SELECT * FROM review_assets WHERE run_id=? AND status='pending' ORDER BY opaque_review_id,side,asset_order",(run_id,))]
        cache_hits=0;to_fetch=[]
        for row in pending:
            expected=str(row["expected_pixel_sha256"] or "")
            cached=root/_cache_relative(root,wp1_cache_root,expected,WP1_TRANSFORM_VERSION)
            if expected and cached.is_file():
                relative=(cached.relative_to(root) if cached.is_relative_to(root) else cached).as_posix()
                payload={"opaque_review_id":row["opaque_review_id"],"side":row["side"],"asset_order":row["asset_order"],"source_asset_id":row["source_asset_id"],"status":"cache_hit","cache_relative_path":relative,"actual_pixel_sha256":expected,"request_count":0}
                database.execute(
                    "UPDATE review_assets SET status='cache_hit',cache_relative_path=?,actual_final_url=expected_url,"
                    "actual_raw_sha256=expected_raw_sha256,actual_pixel_sha256=expected_pixel_sha256,request_count=0,record_sha256=? "
                    "WHERE run_id=? AND opaque_review_id=? AND side=? AND asset_order=?",
                    (relative,_record(payload),run_id,row["opaque_review_id"],row["side"],row["asset_order"]),
                );cache_hits+=1
            else:to_fetch.append(row)
        database.execute("UPDATE runs SET stage_b_status='running' WHERE run_id=?",(run_id,));database.commit()
        if len(to_fetch)>MAX_LOGICAL_FETCHES:raise IdentityReviewError("logical fetch cap exceeded")
        results=[]
        with ThreadPoolExecutor(max_workers=max(1,min(workers,4)),thread_name_prefix="vnext-identity-http") as executor:
            futures={executor.submit(_fetch_asset,row,fetcher):row for row in to_fetch}
            for future in as_completed(futures):results.append(future.result())
        results.sort(key=lambda row:row.key);http_requests=0
        for result in results:
            http_requests+=result.request_count;opaque,side,asset_order=result.key
            cache_path=None
            if result.body is not None and result.actual_pixel_sha256:
                cache_path=_materialize_current(root,cache_root,result.actual_pixel_sha256,result.body)
            payload={"opaque_review_id":opaque,"side":side,"asset_order":asset_order,"status":result.status,"cache_relative_path":cache_path,
                "actual_final_url":result.actual_final_url,"actual_raw_sha256":result.actual_raw_sha256,"actual_pixel_sha256":result.actual_pixel_sha256,
                "http_status":result.http_status,"response_bytes":result.response_bytes,"request_count":result.request_count,"error_kind":result.error_kind}
            database.execute(
                "UPDATE review_assets SET status=?,cache_relative_path=?,actual_final_url=?,actual_raw_sha256=?,actual_pixel_sha256=?,"
                "http_status=?,response_bytes=?,request_count=?,error_kind=?,error_message=?,record_sha256=? "
                "WHERE run_id=? AND opaque_review_id=? AND side=? AND asset_order=?",
                (result.status,cache_path,result.actual_final_url,result.actual_raw_sha256,result.actual_pixel_sha256,result.http_status,
                 result.response_bytes,result.request_count,result.error_kind,result.error_message,_record(payload),run_id,opaque,side,asset_order),
            );database.commit()
        if http_requests>MAX_HTTP_REQUESTS_WITH_REDIRECTS:raise IdentityReviewError("HTTP request cap exceeded")
        statuses={str(row[0]):int(row[1]) for row in database.execute("SELECT status,count(*) FROM review_assets WHERE run_id=? GROUP BY status",(run_id,))}
        if not set(statuses)<=TERMINAL_ASSET_STATUSES:raise IdentityReviewError("non-terminal review assets remain")
        sheets,form=_render_review_artifacts(database,root,run_id,review_prefix,review_form)
        for role,(relative,_size,expected) in input_paths.items():
            after=sha256_file(root/relative) if verify_full_hashes else expected
            if after!=pins[role]["sha256_before"]:raise IdentityReviewError(f"Stage B input changed: {role}")
            pins[role]["sha256_after"]=after;payload={"role":role,**pins[role]}
            database.execute("UPDATE stage_b_input_pins SET sha256_after=?,record_sha256=? WHERE run_id=? AND role=?",(after,_record(payload),run_id,role))
        completed=utc_now();manifest_row=database.execute("SELECT * FROM stage_b_manifest WHERE run_id=?",(run_id,)).fetchone()
        manifest_payload={key:manifest_row[key] for key in manifest_row.keys() if key not in {"completed_at","record_sha256"}};manifest_payload["completed_at"]=completed
        database.execute("UPDATE stage_b_manifest SET completed_at=?,record_sha256=? WHERE run_id=?",(completed,_record(manifest_payload),run_id))
        database.execute("UPDATE runs SET status='stage_b_complete',stage_b_status='complete',stage_b_http_requests=?,blind_review_status='pending' WHERE run_id=?",(http_requests,run_id))
        for metric,value in (("selected_assets",sum(statuses.values())),("cache_hits",cache_hits),("logical_fetches",len(to_fetch)),("http_requests",http_requests)):
            database.execute("INSERT OR REPLACE INTO metrics VALUES(?,?,?,?)",(run_id,"stage_b",metric,value))
        database.commit()
        stage_b_manifest={"status":"PASS","run_id":run_id,"stage_b_version":STAGE_B_VERSION,"input_pins":pins,"asset_selection_version":ASSET_SELECTION_VERSION,
            "transform_version":TRANSFORM_VERSION,"blind_review_version":BLIND_REVIEW_VERSION,"adjudication_version":ADJUDICATION_VERSION,
            "bounds":{"max_assets_per_side":MAX_ASSETS_PER_SIDE,"max_logical_fetches":MAX_LOGICAL_FETCHES,"max_http_requests_with_redirects":MAX_HTTP_REQUESTS_WITH_REDIRECTS},
            "cost":{"vision_requests":0,"llm_requests":0,"paid_api_requests":0,"estimated_usd":0.0}}
        report_payload={"status":"PASS","run_id":run_id,"stage":"WP2 Stage B pre-review","selected_asset_count":sum(statuses.values()),"cache_hit_count":cache_hits,
            "logical_fetch_count":len(to_fetch),"http_request_count":http_requests,"pixel_status_counts":statuses,"review_sheet_count":len(sheets),
            "blind_fields_hidden":["candidate_id","sample_rank","stratum","origin","scores","proposal","auto_merge_eligible"],"vision_requests":0,"llm_requests":0}
        _atomic_json(root/manifest_path,stage_b_manifest);_atomic_json(root/report_path,report_payload)
        return StageBResult(artifact,run_id,"complete",sum(statuses.values()),cache_hits,len(to_fetch),http_requests,statuses,sheets,form,time.monotonic()-started)
    finally:
        database.close();e2.close()
        for connection in e1.values():connection.close()


def apply_blind_review(
    *,repo_root:Path,artifact_path:Path=DEFAULT_ARTIFACT,review_form_path:Path=DEFAULT_REVIEW_FORM,
    adjudication_packet_path:Path=DEFAULT_ADJUDICATION_PACKET,adjudication_form_path:Path=DEFAULT_ADJUDICATION_FORM,
) -> tuple[Path,Path]:
    root=repo_root.resolve();artifact=artifact_path if artifact_path.is_absolute() else root/artifact_path
    form_path=review_form_path if review_form_path.is_absolute() else root/review_form_path
    payload=json.loads(form_path.read_text(encoding="utf-8"))
    if payload.get("review_version")!=BLIND_REVIEW_VERSION:raise IdentityReviewError("blind review version mismatch")
    reviewer=str(payload.get("reviewer") or "").strip()
    if not reviewer:raise IdentityReviewError("blind review requires reviewer")
    items=payload.get("items");labels={"same","related","different","insufficient"}
    if not isinstance(items,list) or len(items)!=10:raise IdentityReviewError("blind review requires exactly ten items")
    parsed={}
    for item in items:
        opaque=str(item.get("opaque_review_id") or "");label=str(item.get("label") or "")
        rationale=str(item.get("rationale") or "").strip();conflict=str(item.get("hard_conflict_observed") or "").strip()
        if not opaque or opaque in parsed or label not in labels or not rationale or not conflict:raise IdentityReviewError("blind review item is incomplete")
        parsed[opaque]=(label,rationale,conflict)
    _assert_no_sidecars(artifact);database=sqlite3.connect(artifact);database.row_factory=sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE");database.execute("PRAGMA synchronous=FULL");database.execute("PRAGMA foreign_keys=ON")
    try:
        run=database.execute("SELECT * FROM runs").fetchone();run_id=str(run["run_id"])
        if run["stage_b_status"]!="complete" or run["blind_review_status"]!="pending":raise IdentityReviewError("artifact is not awaiting blind review")
        expected={str(row[0]) for row in database.execute("SELECT opaque_review_id FROM sample_neighbourhoods WHERE run_id=?",(run_id,))}
        if set(parsed)!=expected:raise IdentityReviewError("blind review opaque ID set mismatch")
        reviewed_at=utc_now()
        for opaque,(label,rationale,conflict) in sorted(parsed.items()):
            row_payload={"opaque_review_id":opaque,"label":label,"rationale":rationale,"hard_conflict_observed":conflict,
                "reviewer":reviewer,"review_version":BLIND_REVIEW_VERSION,"reviewed_at":reviewed_at}
            database.execute("INSERT INTO blind_reviews VALUES(?,?,?,?,?,?,?,?,?)",(run_id,opaque,label,rationale,conflict,reviewer,BLIND_REVIEW_VERSION,reviewed_at,_record(row_payload)))
        database.execute("UPDATE runs SET blind_review_status='complete',adjudication_status='pending' WHERE run_id=?",(run_id,));database.commit()
        packet_items=[];form_items=[]
        for sample in database.execute("SELECT * FROM sample_neighbourhoods WHERE run_id=? ORDER BY sample_rank",(run_id,)):
            opaque=str(sample["opaque_review_id"]);candidate=database.execute("SELECT * FROM candidate_pairs WHERE run_id=? AND candidate_id=?",(run_id,sample["candidate_id"])).fetchone()
            review=database.execute("SELECT * FROM blind_reviews WHERE run_id=? AND opaque_review_id=?",(run_id,opaque)).fetchone()
            families=[{"family":row["family"],"status":row["status"],"score":row["score"],"evidence":json.loads(row["evidence_json"])} for row in database.execute("SELECT * FROM evidence_families WHERE run_id=? AND candidate_id=? ORDER BY family",(run_id,sample["candidate_id"]))]
            conflicts=[{"conflict_type":row["conflict_type"],"detail":json.loads(row["detail_json"])} for row in database.execute("SELECT * FROM hard_conflicts WHERE run_id=? AND candidate_id=? ORDER BY conflict_type",(run_id,sample["candidate_id"]))]
            packet_items.append({"opaque_review_id":opaque,"candidate_id":sample["candidate_id"],"stratum":sample["stratum"],
                "left":_metadata(database,run_id,"divisare",candidate["left_source_building_id"]),"right":_metadata(database,run_id,"architizer",candidate["right_source_building_id"]),
                "reviewer":{"label":review["label"],"rationale":review["rationale"],"hard_conflict_observed":review["hard_conflict_observed"]},
                "candidate_assessment":{"origins":[row[0] for row in database.execute("SELECT origin FROM candidate_origins WHERE run_id=? AND candidate_id=? ORDER BY origin",(run_id,sample["candidate_id"]))],
                    "name_strict_score":candidate["name_strict_score"],"firm_strict_score":candidate["firm_strict_score"],"year_gap":candidate["year_gap"],
                    "positive_family_count":candidate["positive_family_count"],"hard_conflict_count":candidate["hard_conflict_count"],"proposal":candidate["proposal"],"auto_merge_eligible":candidate["auto_merge_eligible"],
                    "families":families,"hard_conflicts":conflicts}})
            form_items.append({"opaque_review_id":opaque,"candidate_id":sample["candidate_id"],"reviewer_label":review["label"],"adjudicator_label":None,"rationale":None})
        packet={"adjudication_version":ADJUDICATION_VERSION,"run_id":run_id,"review_locked":True,"items":packet_items}
        form={"adjudication_version":ADJUDICATION_VERSION,"run_id":run_id,"adjudicator":None,"allowed_labels":["same","related","different","insufficient"],
            "disagreement_rule":"reviewer/adjudicator disagreement maps final gold to insufficient and decision to defer","items":form_items}
        packet_path=adjudication_packet_path if adjudication_packet_path.is_absolute() else root/adjudication_packet_path
        adjudication_path=adjudication_form_path if adjudication_form_path.is_absolute() else root/adjudication_form_path
        _atomic_json(packet_path,packet);_atomic_json(adjudication_path,form)
        return packet_path,adjudication_path
    finally:database.close()


def apply_adjudication(
    *,repo_root:Path,artifact_path:Path=DEFAULT_ARTIFACT,adjudication_form_path:Path=DEFAULT_ADJUDICATION_FORM,
    gate_report_path:Path=DEFAULT_GATE_REPORT,
) -> GateResult:
    root=repo_root.resolve();artifact=artifact_path if artifact_path.is_absolute() else root/artifact_path
    form_path=adjudication_form_path if adjudication_form_path.is_absolute() else root/adjudication_form_path
    payload=json.loads(form_path.read_text(encoding="utf-8"))
    if payload.get("adjudication_version")!=ADJUDICATION_VERSION:raise IdentityReviewError("adjudication version mismatch")
    adjudicator=str(payload.get("adjudicator") or "").strip();items=payload.get("items");allowed={"same","related","different","insufficient"}
    if not adjudicator or not isinstance(items,list) or len(items)!=10:raise IdentityReviewError("adjudication form is incomplete")
    parsed={}
    for item in items:
        opaque=str(item.get("opaque_review_id") or "");label=str(item.get("adjudicator_label") or "");rationale=str(item.get("rationale") or "").strip()
        if not opaque or opaque in parsed or label not in allowed or not rationale:raise IdentityReviewError("adjudication item is incomplete")
        parsed[opaque]=(label,rationale)
    database=sqlite3.connect(artifact);database.row_factory=sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE");database.execute("PRAGMA synchronous=FULL");database.execute("PRAGMA foreign_keys=ON")
    try:
        run=database.execute("SELECT * FROM runs").fetchone();run_id=str(run["run_id"])
        if run["blind_review_status"]!="complete" or run["adjudication_status"]!="pending":raise IdentityReviewError("artifact is not awaiting adjudication")
        expected={str(row[0]) for row in database.execute("SELECT opaque_review_id FROM blind_reviews WHERE run_id=?",(run_id,))}
        if set(parsed)!=expected:raise IdentityReviewError("adjudication opaque ID set mismatch")
        mapped={"same":"merge","related":"related_but_separate","different":"reject","insufficient":"defer"};at=utc_now();disagreements=0
        for opaque,(adjudicator_label,rationale) in sorted(parsed.items()):
            reviewer_label=str(database.execute("SELECT label FROM blind_reviews WHERE run_id=? AND opaque_review_id=?",(run_id,opaque)).fetchone()[0])
            agrees=int(reviewer_label==adjudicator_label);final=reviewer_label if agrees else "insufficient";disagreements+=1-agrees
            row_payload={"opaque_review_id":opaque,"reviewer_label":reviewer_label,"adjudicator_label":adjudicator_label,"agrees":agrees,
                "final_gold_label":final,"mapped_decision":mapped[final],"rationale":rationale,"adjudicator":adjudicator,
                "adjudication_version":ADJUDICATION_VERSION,"adjudicated_at":at}
            database.execute("INSERT INTO adjudications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(run_id,opaque,reviewer_label,adjudicator_label,agrees,final,mapped[final],rationale,adjudicator,ADJUDICATION_VERSION,at,_record(row_payload)))
        false_merge=int(database.execute(
            "SELECT count(*) FROM adjudications a JOIN sample_neighbourhoods s USING(run_id,opaque_review_id) "
            "JOIN candidate_pairs c USING(run_id,candidate_id) WHERE a.run_id=? AND a.reviewer_label='different' AND c.auto_merge_eligible=1",
            (run_id,),
        ).fetchone()[0])
        hard_violation=int(database.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1 AND hard_conflict_count>0",(run_id,)).fetchone()[0])
        status="PASS" if false_merge==0 and hard_violation==0 else "FAIL"
        database.execute("UPDATE runs SET status=?,adjudication_status='complete',gate_status=? WHERE run_id=?",("pass" if status=="PASS" else "fail",status.casefold(),run_id))
        for metric,value in (("false_merge",false_merge),("hard_conflict_violation",hard_violation),("review_adjudication_disagreement",disagreements)):
            database.execute("INSERT OR REPLACE INTO metrics VALUES(?,?,?,?)",(run_id,"gate",metric,value))
        database.commit()
        label_counts={str(row[0]):int(row[1]) for row in database.execute("SELECT final_gold_label,count(*) FROM adjudications WHERE run_id=? GROUP BY final_gold_label",(run_id,))}
        report_path=gate_report_path if gate_report_path.is_absolute() else root/gate_report_path
        report={"status":status,"gate_version":GATE_VERSION,"run_id":run_id,"false_merge_count":false_merge,"hard_conflict_violation_count":hard_violation,
            "review_adjudication_disagreement_count":disagreements,"final_gold_label_counts":label_counts,"merge_executions":0,"vnx_id_issuances":0,
            "cost":{"vision_requests":0,"llm_requests":0,"paid_api_requests":0,"estimated_usd":0.0},"completed_at":utc_now()}
        _atomic_json(report_path,report)
        return GateResult(artifact,run_id,status,false_merge,hard_violation,disagreements,report_path)
    finally:database.close()


__all__=[
    "ADJUDICATION_VERSION","BLIND_REVIEW_VERSION","DEFAULT_ADJUDICATION_FORM","DEFAULT_ADJUDICATION_PACKET",
    "DEFAULT_ARTIFACT","DEFAULT_GATE_REPORT","DEFAULT_REVIEW_FORM","DEFAULT_REVIEW_PREFIX","DEFAULT_STAGE_B_MANIFEST",
    "DEFAULT_STAGE_B_REPORT","GATE_VERSION","IdentityReviewError","StageBResult","GateResult","apply_adjudication",
    "apply_blind_review","run_stage_b",
]
