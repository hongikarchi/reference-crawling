#!/usr/bin/env python3
"""Local human-review UI for a frozen Divisare Vision candidate manifest.

The server never downloads or proxies images.  The browser renders each
manifest-provided HTTPS derivative URL directly.  Review state is a mutable,
atomic JSON draft; the final review export is manifest-bound and no-clobber.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


MANIFEST_SCHEMA = "divisare-vision-gold-candidates-v1.0.0"
DRAFT_SCHEMA = "divisare-vision-human-review-draft-v1"
EXPORT_SCHEMA = "divisare-vision-reviewed-pool-v1.0.0"
GOLD_LABELS = ("exterior", "interior", "drawing", "aerial", "detail")
CLARITIES = ("clear", "boundary")
MAX_NOTES_LENGTH = 4_000
MAX_REQUEST_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANDIDATE_ID_RE = re.compile(r"^candidate-[0-9]{4,}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _without_declared_sha(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = dict(payload)
    value.pop(field, None)
    return value


def manifest_sha256(payload: Mapping[str, Any]) -> str:
    """Hash the complete manifest except its self-referential SHA field."""
    return hashlib.sha256(
        canonical_json_bytes(_without_declared_sha(payload, "manifest_sha256"))
    ).hexdigest()


def reviewed_pool_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(_without_declared_sha(payload, "reviewed_pool_sha256"))
    ).hexdigest()


def _manifest_version(payload: Mapping[str, Any]) -> str:
    version = payload.get("manifest_version")
    if version != MANIFEST_SCHEMA:
        raise ValueError(f"manifest_version must be {MANIFEST_SCHEMA}")
    return version


def _item_id(item: Mapping[str, Any]) -> str:
    candidate_id = item.get("candidate_id")
    if not isinstance(candidate_id, str) or not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ValueError("every item requires an opaque candidate_id such as candidate-0001")
    return candidate_id


def _https_url(item: Mapping[str, Any], field: str, *, fallback: str | None = None) -> str:
    value = item.get(field)
    if value is None and fallback is not None:
        value = item.get(fallback)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{_item_id(item)} requires {field}")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        raise ValueError(f"{_item_id(item)} {field} must be an HTTPS URL")
    return value


def _analysis_url(item: Mapping[str, Any]) -> str:
    return _https_url(item, "request_url")


def _review_url(item: Mapping[str, Any]) -> str:
    return _https_url(item, "review_url", fallback="request_url")


def validate_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    _manifest_version(payload)
    declared = payload.get("manifest_sha256")
    if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
        raise ValueError("manifest_sha256 must be 64 lowercase hex characters")
    computed = manifest_sha256(payload)
    if declared != computed:
        raise ValueError(
            f"manifest SHA mismatch: declared {declared}, computed {computed}"
        )
    source_sha = payload.get("source_db_sha256")
    if not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha):
        raise ValueError("source_db_sha256 must be 64 lowercase hex characters")
    contract = payload.get("contract")
    if not isinstance(contract, dict) or not contract:
        raise ValueError("manifest contract must be a non-empty object")
    items = payload.get("candidates", payload.get("items"))
    if not isinstance(items, list) or not items:
        raise ValueError("manifest items must be a non-empty list")
    seen_ids: set[str] = set()
    seen_assets: set[str] = set()
    seen_articles: set[str] = set()
    seen_buildings: set[str] = set()
    for index, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"manifest item {index} must be an object")
        candidate_id = _item_id(raw)
        if candidate_id in seen_ids:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        seen_ids.add(candidate_id)
        asset_key = raw.get("asset_key")
        if not isinstance(asset_key, str) or not asset_key.strip():
            raise ValueError(f"{candidate_id} requires asset_key")
        if asset_key in seen_assets:
            raise ValueError(f"duplicate asset_key: {asset_key}")
        seen_assets.add(asset_key)
        _analysis_url(raw)
        _review_url(raw)
        for field in ("article_id", "building_id"):
            if raw.get(field) is None or isinstance(raw.get(field), (dict, list)):
                raise ValueError(f"{candidate_id} requires scalar {field}")
        article_key = str(raw["article_id"])
        building_key = str(raw["building_id"])
        if article_key in seen_articles:
            raise ValueError(f"duplicate article_id: {raw['article_id']}")
        if building_key in seen_buildings:
            raise ValueError(f"duplicate building_id: {raw['building_id']}")
        seen_articles.add(article_key)
        seen_buildings.add(building_key)
    return dict(payload)


def load_manifest(path: Path) -> dict[str, Any]:
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def manifest_items(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(manifest.get("candidates", manifest.get("items", [])))


def _empty_draft(manifest: Mapping[str, Any], reviewer: str) -> dict[str, Any]:
    return {
        "schema_version": DRAFT_SCHEMA,
        "manifest_sha256": manifest["manifest_sha256"],
        "reviewer": reviewer,
        "updated_at": None,
        "decisions": {},
    }


def validate_decision(
    payload: Any,
    *,
    valid_ids: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("decision must be an object")
    candidate_id = payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate_id is required")
    if valid_ids is not None and candidate_id not in valid_ids:
        raise ValueError(f"candidate_id is not in the manifest: {candidate_id}")
    disposition = payload.get("disposition")
    if disposition not in ("include", "exclude"):
        raise ValueError("disposition must be include or exclude")
    notes = payload.get("notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES_LENGTH:
        raise ValueError(f"notes must be a string of at most {MAX_NOTES_LENGTH} characters")
    high_res_viewed = payload.get("high_res_viewed", False)
    if not isinstance(high_res_viewed, bool):
        raise ValueError("high_res_viewed must be a boolean")

    gold_label = payload.get("gold_label")
    clarity = payload.get("clarity")
    acceptable = payload.get("acceptable_labels", [])
    if not isinstance(acceptable, list) or any(label not in GOLD_LABELS for label in acceptable):
        raise ValueError(f"acceptable_labels must contain only {GOLD_LABELS}")
    if len(set(acceptable)) != len(acceptable):
        raise ValueError("acceptable_labels must not contain duplicates")
    acceptable = sorted(acceptable, key=GOLD_LABELS.index)

    if disposition == "exclude":
        if gold_label is not None or clarity is not None or acceptable:
            raise ValueError("excluded items cannot carry gold labels, clarity, or acceptable labels")
    else:
        if gold_label not in GOLD_LABELS:
            raise ValueError(f"gold_label must be one of {GOLD_LABELS}")
        if clarity not in CLARITIES:
            raise ValueError(f"clarity must be one of {CLARITIES}")
        if gold_label not in acceptable:
            raise ValueError("acceptable_labels must include gold_label")
        if clarity == "clear" and acceptable != [gold_label]:
            raise ValueError("clear items must accept only their gold_label")
        if clarity == "boundary" and len(acceptable) < 2:
            raise ValueError("boundary items require at least two acceptable labels")

    reviewed_at = payload.get("reviewed_at")
    if reviewed_at is None:
        reviewed_at = utc_now()
    if (
        not isinstance(reviewed_at, str)
        or not reviewed_at.strip()
        or reviewed_at != reviewed_at.strip()
    ):
        raise ValueError("reviewed_at must be a non-empty trimmed string")

    return {
        "candidate_id": candidate_id,
        "disposition": disposition,
        "gold_label": gold_label,
        "clarity": clarity,
        "acceptable_labels": acceptable,
        "high_res_viewed": high_res_viewed,
        "notes": notes.strip(),
        "reviewed_at": reviewed_at,
    }


def validate_draft(
    draft: Any,
    manifest: Mapping[str, Any],
    *,
    fallback_reviewer: str,
) -> dict[str, Any]:
    if not isinstance(draft, dict) or draft.get("schema_version") != DRAFT_SCHEMA:
        raise ValueError(f"draft schema_version must be {DRAFT_SCHEMA}")
    if draft.get("manifest_sha256") != manifest["manifest_sha256"]:
        raise ValueError("draft belongs to a different manifest SHA")
    reviewer = draft.get("reviewer", fallback_reviewer)
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer must be a non-empty string")
    raw_decisions = draft.get("decisions")
    if not isinstance(raw_decisions, dict):
        raise ValueError("draft decisions must be an object")
    valid_ids = {_item_id(item) for item in manifest_items(manifest)}
    decisions: dict[str, dict[str, Any]] = {}
    for key, raw in raw_decisions.items():
        normalized = validate_decision(raw, valid_ids=valid_ids)
        if key != normalized["candidate_id"]:
            raise ValueError(f"draft key does not match candidate_id: {key}")
        decisions[key] = normalized
    return {
        "schema_version": DRAFT_SCHEMA,
        "manifest_sha256": manifest["manifest_sha256"],
        "reviewer": reviewer.strip(),
        "updated_at": draft.get("updated_at"),
        "decisions": decisions,
    }


def load_draft(
    path: Path,
    manifest: Mapping[str, Any],
    reviewer: str,
) -> dict[str, Any]:
    if not path.exists():
        return _empty_draft(manifest, reviewer)
    return validate_draft(
        json.loads(path.read_text(encoding="utf-8")),
        manifest,
        fallback_reviewer=reviewer,
    )


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json_no_clobber(path: Path, payload: Any) -> None:
    """Publish complete JSON atomically without replacing an existing path."""
    if path.exists():
        raise FileExistsError(f"immutable output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError as exc:
            raise FileExistsError(f"immutable output already exists: {path}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def save_decision(
    *,
    draft_path: Path,
    manifest: Mapping[str, Any],
    reviewer: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    draft = load_draft(draft_path, manifest, reviewer)
    candidate_id = payload.get("candidate_id")
    if payload.get("undo") is True:
        if not isinstance(candidate_id, str):
            raise ValueError("candidate_id is required for undo")
        valid_ids = {_item_id(item) for item in manifest_items(manifest)}
        if candidate_id not in valid_ids:
            raise ValueError(f"candidate_id is not in the manifest: {candidate_id}")
        draft["decisions"].pop(candidate_id, None)
    else:
        valid_ids = {_item_id(item) for item in manifest_items(manifest)}
        decision = validate_decision(payload, valid_ids=valid_ids)
        draft["decisions"][decision["candidate_id"]] = decision
    draft["updated_at"] = utc_now()
    atomic_write_json(draft_path, draft)
    return draft


def build_export(
    manifest: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> dict[str, Any]:
    valid = validate_draft(draft, manifest, fallback_reviewer=str(draft.get("reviewer") or "human"))
    ordered = []
    for item in manifest_items(manifest):
        candidate_id = _item_id(item)
        if candidate_id in valid["decisions"]:
            identity = {
                "candidate_id": candidate_id,
                "asset_key": item["asset_key"],
                "article_id": item["article_id"],
                "building_id": item["building_id"],
                "request_url": _analysis_url(item),
                "review_url": _review_url(item),
            }
            for field in (
                "delivery_lane",
                "generation_group",
                "url_generation",
                "content_sha256",
                "pixel_sha256",
                "phash_256",
                "duplicate_of",
            ):
                if item.get(field) is not None:
                    identity[field] = item[field]
            ordered.append({**identity, **valid["decisions"][candidate_id]})
    included = sum(row["disposition"] == "include" for row in ordered)
    excluded = sum(row["disposition"] == "exclude" for row in ordered)
    payload: dict[str, Any] = {
        "manifest_version": EXPORT_SCHEMA,
        "candidate_manifest_version": _manifest_version(manifest),
        "candidate_manifest_sha256": manifest["manifest_sha256"],
        "source_db_sha256": manifest["source_db_sha256"],
        "contract": manifest["contract"],
        "reviewer": valid["reviewer"],
        "exported_at": utc_now(),
        "total_candidates": len(manifest_items(manifest)),
        "decided_count": len(ordered),
        "included_count": included,
        "excluded_count": excluded,
        "complete": len(ordered) == len(manifest_items(manifest)),
        "decisions": ordered,
    }
    payload["reviewed_pool_sha256"] = reviewed_pool_sha256(payload)
    return payload


def validate_import(payload: Any, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("import must be a JSON object")
    if payload.get("candidate_manifest_sha256") != manifest["manifest_sha256"]:
        raise ValueError("import belongs to a different manifest SHA")
    if payload.get("manifest_version") != EXPORT_SCHEMA:
        raise ValueError(f"import manifest_version must be {EXPORT_SCHEMA}")
    if payload.get("candidate_manifest_version") != _manifest_version(manifest):
        raise ValueError("import candidate manifest version mismatch")
    if payload.get("source_db_sha256") != manifest["source_db_sha256"]:
        raise ValueError("import source DB SHA mismatch")
    if payload.get("contract") != manifest["contract"]:
        raise ValueError("import derivative contract mismatch")
    declared = payload.get("reviewed_pool_sha256")
    if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
        raise ValueError("reviewed_pool_sha256 must be 64 lowercase hex characters")
    if reviewed_pool_sha256(payload) != declared:
        raise ValueError("reviewed pool SHA mismatch")
    raw = payload.get("decisions")
    if not isinstance(raw, list):
        raise ValueError("import decisions must be a list")
    expected_total = len(manifest_items(manifest))
    if payload.get("total_candidates") != expected_total:
        raise ValueError("import total_candidates does not match the manifest")
    if payload.get("decided_count") != len(raw):
        raise ValueError("import decided_count does not match decisions")
    valid_ids = {_item_id(item) for item in manifest_items(manifest)}
    items_by_id = {_item_id(item): item for item in manifest_items(manifest)}
    seen: set[str] = set()
    normalized = []
    forbidden = {
        "weak_hints",
        "discovery_class",
        "discovery_score",
        "discovery_reasons",
        "filename_hints",
        "article_hints",
        "album_priors",
        "source_url",
        "project_name",
        "article_title",
    }
    for decision in raw:
        if not isinstance(decision, dict):
            raise ValueError("every imported decision must be an object")
        leaked = forbidden.intersection(decision)
        if leaked:
            raise ValueError("reviewed pool contains forbidden hint fields: " + ", ".join(sorted(leaked)))
        row = validate_decision(decision, valid_ids=valid_ids)
        if row["candidate_id"] in seen:
            raise ValueError(f"duplicate imported candidate_id: {row['candidate_id']}")
        source_item = items_by_id[row["candidate_id"]]
        expected_identity = {
            "asset_key": source_item["asset_key"],
            "article_id": source_item["article_id"],
            "building_id": source_item["building_id"],
            "request_url": _analysis_url(source_item),
            "review_url": _review_url(source_item),
        }
        for field, expected in expected_identity.items():
            if decision.get(field) != expected:
                raise ValueError(f"import {field} mismatch for {row['candidate_id']}")
        seen.add(row["candidate_id"])
        normalized.append(row)
    included = sum(row["disposition"] == "include" for row in normalized)
    excluded = sum(row["disposition"] == "exclude" for row in normalized)
    if payload.get("included_count") != included or payload.get("excluded_count") != excluded:
        raise ValueError("import inclusion counts do not match decisions")
    if payload.get("complete") is not (len(normalized) == expected_total):
        raise ValueError("import complete flag does not match decisions")
    return normalized


def merge_import(
    *,
    draft_path: Path,
    manifest: Mapping[str, Any],
    reviewer: str,
    payload: Any,
) -> dict[str, Any]:
    imported = validate_import(payload, manifest)
    draft = load_draft(draft_path, manifest, reviewer)
    imported_reviewer = payload.get("reviewer")
    if not isinstance(imported_reviewer, str) or not imported_reviewer.strip():
        raise ValueError("import reviewer must be a non-empty string")
    if draft["decisions"] and draft["reviewer"] != imported_reviewer.strip():
        raise ValueError("import reviewer conflicts with the existing draft reviewer")
    conflicts = [
        row["candidate_id"]
        for row in imported
        if row["candidate_id"] in draft["decisions"]
        and draft["decisions"][row["candidate_id"]] != row
    ]
    if conflicts:
        raise ValueError("import conflicts with existing decisions: " + ", ".join(conflicts[:10]))
    for row in imported:
        draft["decisions"][row["candidate_id"]] = row
    if len(imported) == len(draft["decisions"]):
        draft["reviewer"] = imported_reviewer.strip()
    draft["updated_at"] = utc_now()
    atomic_write_json(draft_path, draft)
    return draft


def public_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return UI data with hints isolated so they stay collapsed by default."""
    rows = []
    for index, item in enumerate(manifest_items(manifest), 1):
        weak_hints = item.get("weak_hints")
        if weak_hints is None:
            weak_hints = {
                field: item.get(field)
                for field in (
                    "discovery_class",
                    "discovery_score",
                    "discovery_reasons",
                    "filename_hints",
                    "article_hints",
                    "album_priors",
                )
                if item.get(field) not in (None, [], {}, "")
            }
        rows.append(
            {
                "candidate_id": _item_id(item),
                "rank": item.get("candidate_rank", item.get("sample_rank", index)),
                "asset_key": item["asset_key"],
                "article_id": item["article_id"],
                "building_id": item["building_id"],
                "review_url": _review_url(item),
                "high_resolution_url": _analysis_url(item),
                "generation_group": item.get("generation_group", item.get("delivery_lane")),
                "role": item.get("role"),
                "format_lane": item.get("format_lane"),
                "weak_hints": weak_hints,
            }
        )
    return {
        "manifest_version": _manifest_version(manifest),
        "manifest_sha256": manifest["manifest_sha256"],
        "source_db_sha256": manifest["source_db_sha256"],
        "items": rows,
    }


APP_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Divisare N100 Gold Review</title>
<style>
:root{--bg:#f3f4f1;--panel:#fff;--line:#d6d9d4;--ink:#202421;--muted:#66706a;--accent:#006d77;--soft:#e8f5f4;--warn:#a13d2d;--boundary:#8a5d00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}.shell{max-width:1180px;margin:auto;padding:12px}
header,.workspace{background:var(--panel);border:1px solid var(--line);border-radius:8px}header{position:sticky;top:0;z-index:2;padding:10px 12px;margin-bottom:10px}.top,.nav,.labels,.status,.accepts,.footer{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.top{justify-content:space-between}.progress{height:5px;background:#e5e8e3;margin:8px 0 4px}.progress i{display:block;height:100%;background:var(--accent)}button,label.btn{border:1px solid var(--line);background:#fff;border-radius:6px;padding:8px 11px;cursor:pointer;font:inherit}button:hover,label.btn:hover{border-color:#8a9690}button.active{border-color:var(--accent);background:var(--soft);color:#004f56;font-weight:700}button.danger.active{border-color:var(--warn);background:#fff0ed;color:var(--warn)}button.boundary.active{border-color:var(--boundary);background:#fff7df;color:#6d4900}.workspace{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(340px,.8fr);min-height:690px;overflow:hidden}.image-pane{position:relative;background:#e4e6e2;display:grid;place-items:center;min-height:560px}.image-pane img{display:block;max-width:100%;max-height:78vh;object-fit:contain}.image-pane .resolution{position:absolute;right:10px;top:10px;z-index:1}.review{padding:16px}.meta{color:var(--muted);font-size:13px;overflow-wrap:anywhere}.section{border-top:1px solid var(--line);padding-top:13px;margin-top:13px}.labels button{min-width:96px}.accepts label{border:1px solid var(--line);border-radius:5px;padding:5px 8px}.accepts label.checked{border-color:var(--accent);background:var(--soft)}textarea{width:100%;min-height:92px;border:1px solid var(--line);border-radius:6px;padding:8px;resize:vertical;font:inherit}.footer{justify-content:space-between;margin-top:12px}.hint{background:#f5f6f3;border:1px solid var(--line);border-radius:6px;padding:9px;white-space:pre-wrap;overflow-wrap:anywhere}.hidden{display:none}.toast{position:fixed;right:18px;bottom:18px;background:#17211c;color:#fff;padding:10px 14px;border-radius:6px;opacity:0;pointer-events:none;transition:.15s}.toast.show{opacity:1}small{color:var(--muted)}kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:1px 5px;background:#fff}@media(max-width:800px){.workspace{grid-template-columns:1fr}.image-pane{min-height:360px}.image-pane img{max-height:55vh}}
</style></head><body><div class="shell">
<header><div class="top"><b>Divisare N100 human gold review</b><div class="nav"><button id="prev" title="Previous [Left]">&#8592;</button><span id="counter"></span><button id="next" title="Next [Right]">&#8594;</button><button id="nextPending">다음 미검토</button><button id="export">JSON 내보내기</button><label class="btn" for="importFile">JSON 가져오기</label><input class="hidden" id="importFile" type="file" accept="application/json"></div></div><div class="progress"><i id="progress"></i></div><small id="manifestMeta"></small></header>
<main class="workspace"><div class="image-pane"><button id="highRes" class="resolution" title="고해상도 전환">2048px</button><img id="image" referrerpolicy="no-referrer" alt="Review candidate"></div><section class="review">
<div class="meta" id="itemMeta"></div>
<div class="section"><b>Gold label</b><div class="labels" id="labels"></div><details><summary>판정 기준</summary><small>우선순위: drawing(도면·다이어그램) → aerial(전체 건물·대지의 고각/하향 시점) → detail(부재·재료·접합부의 밀착 화면) → interior(둘러싸인 공간) → exterior(일반 외부 전경). 렌더링·모형·합성·인물·공사·사물·비건축 이미지는 Exclude합니다.</small></details></div>
<div class="section"><b>판정 상태</b><div class="status"><button id="clear">Clear</button><button id="boundary" class="boundary">Boundary</button><button id="exclude" class="danger">Exclude</button></div></div>
<div class="section" id="acceptableSection"><b>허용 가능한 라벨</b><div class="accepts" id="accepts"></div><small>Boundary에서 정답으로 인정할 복수 라벨을 선택합니다. Gold label은 반드시 포함됩니다.</small></div>
<div class="section"><label for="notes"><b>메모</b></label><textarea id="notes" maxlength="4000"></textarea></div>
<div class="section"><button id="toggleHints">발견용 힌트 보기</button><div id="hints" class="hint hidden"></div><small>힌트는 reviewer anchoring을 줄이기 위해 기본적으로 숨깁니다.</small></div>
<div class="footer"><button id="undo">판정 취소</button><button id="save" class="active">저장 후 다음</button></div>
</section></main></div><div id="toast" class="toast"></div>
<script>
const LABELS=['exterior','interior','drawing','aerial','detail'];let manifest,draft,index=0,state={},dirty=false;
const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(t){let e=$('#toast');e.textContent=t;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),1800)}
function item(){return manifest.items[index]}function saved(){return draft.decisions[item().candidate_id]||null}
function loadState(){let d=saved();state=d?JSON.parse(JSON.stringify(d)):{candidate_id:item().candidate_id,disposition:'include',gold_label:null,clarity:'clear',acceptable_labels:[],high_res_viewed:false,notes:''};dirty=false;render()}
function setLabel(label){state.disposition='include';state.gold_label=label;if(state.clarity!=='boundary')state.clarity='clear';if(!state.acceptable_labels.includes(label))state.acceptable_labels.push(label);if(state.clarity==='clear')state.acceptable_labels=[label];dirty=true;renderControls()}
function setClarity(c){state.disposition='include';state.clarity=c;if(c==='clear'&&state.gold_label)state.acceptable_labels=[state.gold_label];dirty=true;renderControls()}
function setExclude(){state={candidate_id:item().candidate_id,disposition:'exclude',gold_label:null,clarity:null,acceptable_labels:[],high_res_viewed:state.high_res_viewed||false,notes:$('#notes').value};dirty=true;renderControls()}
function toggleAccept(label){if(state.clarity!=='boundary'||label===state.gold_label)return;state.acceptable_labels=state.acceptable_labels.includes(label)?state.acceptable_labels.filter(x=>x!==label):[...state.acceptable_labels,label];dirty=true;renderControls()}
function renderControls(){document.querySelectorAll('#labels button').forEach(b=>b.classList.toggle('active',state.disposition==='include'&&state.gold_label===b.dataset.label));$('#clear').classList.toggle('active',state.disposition==='include'&&state.clarity==='clear');$('#boundary').classList.toggle('active',state.disposition==='include'&&state.clarity==='boundary');$('#exclude').classList.toggle('active',state.disposition==='exclude');$('#acceptableSection').classList.toggle('hidden',state.disposition==='exclude');document.querySelectorAll('#accepts label').forEach(l=>l.classList.toggle('checked',state.acceptable_labels.includes(l.dataset.label)))}
function render(){let it=item(),done=Object.keys(draft.decisions).length;$('#counter').textContent=`${index+1}/${manifest.items.length} · ${done} 완료`;$('#progress').style.width=(100*done/manifest.items.length)+'%';$('#manifestMeta').textContent=`reviewer ${draft.reviewer} · manifest ${manifest.manifest_sha256.slice(0,12)}… · source ${manifest.source_db_sha256.slice(0,12)}…`;$('#image').src=it.review_url;$('#highRes').textContent='2048px';$('#highRes').classList.remove('active');$('#itemMeta').innerHTML=`<b>${esc(it.candidate_id)}</b> · ${esc(it.rank)}`;$('#labels').innerHTML=LABELS.map((x,i)=>`<button data-label="${x}">${i+1} ${x}</button>`).join('');$('#accepts').innerHTML=LABELS.map(x=>`<label data-label="${x}"><input type="checkbox" ${state.acceptable_labels.includes(x)?'checked':''}> ${x}</label>`).join('');$('#notes').value=state.notes||'';$('#notes').oninput=()=>{dirty=true};$('#hints').textContent=JSON.stringify({asset_key:it.asset_key,article_id:it.article_id,building_id:it.building_id,generation_group:it.generation_group,role:it.role,format_lane:it.format_lane,weak_hints:it.weak_hints||{}},null,2);$('#hints').classList.add('hidden');$('#toggleHints').textContent='감사 정보·발견용 힌트 보기';document.querySelectorAll('#labels button').forEach(b=>b.onclick=()=>setLabel(b.dataset.label));document.querySelectorAll('#accepts label').forEach(l=>l.onclick=e=>{e.preventDefault();toggleAccept(l.dataset.label)});renderControls()}
function toggleHighRes(){let high=$('#image').src===item().high_resolution_url;$('#image').src=high?item().review_url:item().high_resolution_url;$('#highRes').textContent=high?'2048px':'1024px';$('#highRes').classList.toggle('active',!high);if(!high){state.high_res_viewed=true;dirty=true}}
async function api(path,payload){let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!r.ok)throw Error(await r.text());return r.json()}
async function save(){state.notes=$('#notes').value;try{draft=await api('/api/decision',state);dirty=false;toast('저장됨');go(1)}catch(e){toast(e.message)}}
async function undo(){try{draft=await api('/api/decision',{candidate_id:item().candidate_id,undo:true});toast('판정 취소됨');loadState()}catch(e){toast(e.message)}}
function mayLeave(){return !dirty||confirm('저장하지 않은 변경을 버리고 이동할까요?')}
function go(delta){if(!mayLeave())return;index=Math.max(0,Math.min(manifest.items.length-1,index+delta));loadState()}
function nextPending(){if(!mayLeave())return;let n=manifest.items.findIndex((x,i)=>i>index&&!draft.decisions[x.candidate_id]);if(n<0)n=manifest.items.findIndex(x=>!draft.decisions[x.candidate_id]);if(n<0){toast('모든 항목을 검토했습니다');return}index=n;loadState()}
async function exportJson(){let p=await (await fetch('/api/export')).json(),blob=new Blob([JSON.stringify(p,null,2)+'\n'],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`divisare-reviewed-pool-${p.candidate_manifest_sha256.slice(0,12)}.json`;a.click();URL.revokeObjectURL(a.href)}
$('#prev').onclick=()=>go(-1);$('#next').onclick=()=>go(1);$('#nextPending').onclick=nextPending;$('#clear').onclick=()=>setClarity('clear');$('#boundary').onclick=()=>setClarity('boundary');$('#exclude').onclick=setExclude;$('#save').onclick=save;$('#undo').onclick=undo;$('#export').onclick=exportJson;$('#highRes').onclick=toggleHighRes;$('#toggleHints').onclick=()=>{let h=$('#hints'),hidden=h.classList.toggle('hidden');$('#toggleHints').textContent=hidden?'감사 정보·발견용 힌트 보기':'감사 정보·발견용 힌트 숨기기'};
$('#importFile').onchange=async e=>{try{let p=JSON.parse(await e.target.files[0].text());draft=await api('/api/import',p);toast('가져오기 완료');loadState()}catch(err){toast(err.message)}finally{e.target.value=''}};
document.addEventListener('keydown',e=>{if(e.target.matches('textarea,input'))return;if(e.ctrlKey&&e.key==='Enter'){e.preventDefault();save()}else if(e.key==='ArrowLeft')go(-1);else if(e.key==='ArrowRight')go(1);else if('12345'.includes(e.key))setLabel(LABELS[Number(e.key)-1]);else if(e.key.toLowerCase()==='b')setClarity('boundary');else if(e.key.toLowerCase()==='x')setExclude()});
(async()=>{manifest=await (await fetch('/api/manifest')).json();draft=await (await fetch('/api/draft')).json();index=Math.max(0,manifest.items.findIndex(x=>!draft.decisions[x.candidate_id]));loadState()})();
</script></body></html>"""


def make_handler(
    *,
    manifest: Mapping[str, Any],
    draft_path: Path,
    reviewer: str,
) -> type[BaseHTTPRequestHandler]:
    manifest_payload = public_manifest(manifest)

    class ReviewHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src https:; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length < 1 or length > MAX_REQUEST_BYTES:
                raise ValueError("request body size is invalid")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                body = APP_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK.value)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src https:; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                    "frame-ancestors 'none'",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/manifest":
                self._send_json(manifest_payload)
            elif self.path == "/api/draft":
                self._send_json(load_draft(draft_path, manifest, reviewer))
            elif self.path == "/api/export":
                self._send_json(build_export(manifest, load_draft(draft_path, manifest, reviewer)))
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._read_json()
                if self.path == "/api/decision":
                    result = save_decision(
                        draft_path=draft_path,
                        manifest=manifest,
                        reviewer=reviewer,
                        payload=payload,
                    )
                elif self.path == "/api/import":
                    result = merge_import(
                        draft_path=draft_path,
                        manifest=manifest,
                        reviewer=reviewer,
                        payload=payload,
                    )
                else:
                    self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json(result)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("divisare_n100_review: " + (fmt % args) + "\n")

    return ReviewHandler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--reviewer", default="local-human")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8768)
    export = sub.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    sub.add_parser("status")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    draft_path = args.draft.resolve()
    if manifest_path == draft_path:
        raise SystemExit("manifest and mutable draft paths must be different")
    manifest = load_manifest(manifest_path)
    draft = load_draft(draft_path, manifest, args.reviewer)
    if args.command == "serve":
        handler = make_handler(
            manifest=manifest,
            draft_path=draft_path,
            reviewer=args.reviewer,
        )
        server = ThreadingHTTPServer((args.host, args.port), handler)
        print(f"Divisare N100 review: http://{args.host}:{args.port}")
        print(f"manifest_sha256={manifest['manifest_sha256']}")
        server.serve_forever()
        return 0
    result = build_export(manifest, draft)
    if args.command == "export":
        if not result["complete"]:
            raise SystemExit(
                "refusing immutable export: review is incomplete "
                f"({result['decided_count']}/{result['total_candidates']})"
            )
        output_path = args.output.resolve()
        if output_path in (manifest_path, draft_path):
            raise SystemExit("export output must differ from manifest and draft paths")
        write_json_no_clobber(output_path, result)
        print(json.dumps({"output": str(args.output), **{k: result[k] for k in ("candidate_manifest_sha256", "reviewed_pool_sha256", "decided_count", "complete")}}, indent=2))
        return 0
    print(json.dumps({k: result[k] for k in ("candidate_manifest_sha256", "reviewed_pool_sha256", "total_candidates", "decided_count", "included_count", "excluded_count", "complete")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
