"""Independent validator for the vNext WP4 S2 full gallery v1 artifact.

Deliberately does NOT import :mod:`canonical.vnext_gallery_full`, the WP1 builder
(:mod:`canonical.vnext_gallery_draft`) or the S1 registry. It re-derives every rule from the
contract text (§2.1 v1.3) and the frozen E2/E3 rows:

input/contract/continuity/WP1 pins, run row (single complete E2/E3 run, config SHA, run_id),
optional manifest, continuity re-classification from the JSONL rows, scope, and a FULL per-building
replay: every stored column of every row (buildings, groups, members, ledgers, occurrences, deferred
edges) is re-derived from E2/E3 and compared, including record_sha256; then logical SHA and accounting.
A row whose non-key column was rewritten with consistently recomputed SHAs therefore still fails.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


VALIDATOR_VERSION = "vnext-gallery-full-independent-validator-v1.2"
# Rule/version strings restated from the contract §2.1 + order (not imported from the builder).
BUILDER_VERSION = "vnext-gallery-full-builder-v1.2"
GROUPING_VERSION = "vnext-exact-class-only-v1"
REPRESENTATIVE_SCORE_VERSION = "vnext-gallery-representative-score-v1"
SORT_ORDER_VERSION = "vnext-gallery-sort-source-page-order-v1"
URL_POLICY_VERSION = "vnext-gallery-serving-url-v1.2"
URL_POLICY_ARCHITIZER = "architizer_neon_regex_host_rewrite_v1_2"
URL_PROVENANCE_ARCHITIZER = "neon_regex_host_rewrite:architizer-prod.imgix.net->images.architizer.com"
URL_POLICY_DIVISARE = "divisare_e2_raster_occurrence_url_v1_1"
PER_ASSET_PIXEL_STATUS = "pending_s3_pixel_pass"
EDGE_SCOPES = ("global_le8", "metadata_9_16")
REASON_NO_SERVING_URL = "no_browser_renderable_serving_url"
UNRESOLVED_ENGINE = {
    "successful_e2_asset_missing_e3_candidate": "frozen_e2_e3_join",
    REASON_NO_SERVING_URL: "serving_url_policy",
    "source_continuity_not_attested": "continuity_attestation",
}
RASTER_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"})
RASTER_FORMAT_TRANSFORMS = frozenset({"f_jpg", "f_jpeg", "f_png", "f_webp", "f_avif", "f_gif"})
# Live-Neon form of an Architizer URL (tools/architizer_image_host_migrate.py, 2026-10-16). Contract v1.3
# (D2): the Architizer serving URL is exactly this rewrite of a fullmatching frozen canonical_url.
NEON_ARCHITIZER_PATTERN = r"https?://architizer-prod\.imgix\.net(/[^\"?\s]*)(\?[^\"\s]*)?"
NEON_ARCHITIZER_REPLACEMENT = "https://images.architizer.com" + r"\1" + "?width=1680&quality=60"
RUN_ID_PREFIX = "vnx_gallery_full_"
APPLICATION_ID = 0x564E5834
SCHEMA_VERSION = 1
LOGICAL_VERSION = "vnext-gallery-full-logical-v1"
LADDER_SEED = "vnext-wp4-s2-full-ladder-v1"
FULL_BUILDING_COUNT = 91_803
EXPECTED_E2_SHA256 = "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19"
EXPECTED_E3_SHA256 = "8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4"
EXPECTED_E2_SIZE = 10_164_682_752
EXPECTED_E3_SIZE = 10_236_592_128
E2_PATH = "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"
E3_PATH = "data/enrichment/divisare_architizer_image_selection_e3_full_v1.db"
DEFERRED = "deferred_no_per_asset_verification"
CONTRACT_VERSION = "vnext-gallery-contract-v1.3"
CONT_RULE_VERSION = "vnext-asset-continuity-rule-v2"
CONT_SAMPLE_SEED = "vnext-asset-continuity-v2"
CONTRACT_ANCHORS = ("v1.3", "population continuity attestation", DEFERRED, "continuity_attested",
                    CONT_RULE_VERSION, CONT_SAMPLE_SEED)
WP1_ARTIFACT_PINS = {
    "data/enrichment/vnext_gallery_draft_n10_v1.db": "d63b7a5a279f71d0b9c68b6ca66c6123e934dd67fa5c085a9f8ea16736717cc7",
    "data/reports/vnext_gallery_draft_n10_v1_manifest.json": "913a9278dcbd54d6ebe9853aa0aee6c6c45ef8edcfa4b5e3b6dd1c816626ade3",
    "data/enrichment/vnext_gallery_draft_n10_v1_1.db": "ed108eda46099348dff30eebbf4fbbeb0fcc86718dcbec9951d48a5203deb9ac",
    "data/reports/vnext_gallery_draft_n10_v1_1_manifest.json": "c8c94dd27d177a0982e02cb5f64eaf2a0177da6976588f48b684e14a41c2f278",
}
# Contract §2.1 v1.3 (continuity rule v2) thresholds, restated independently of the sampling tool.
CONT_SAMPLE_SIZE = 1000
CONT_ASPECT_REL_MAX = 0.02
CONT_HAMMING_MAX = 32
CONT_RATE_MIN = 0.99
LOGICAL_TABLES: tuple[tuple[str, str], ...] = (
    ("buildings", "source,source_building_id"),
    ("gallery_groups", "source,source_building_id,sort_order"),
    ("group_members", "source,source_building_id,source_asset_id"),
    ("exclusion_ledger", "source,source_building_id,source_asset_id"),
    ("unresolved_ledger", "source,source_building_id,source_asset_id"),
    ("source_occurrences", "source,source_building_id,occurrence_id"),
    ("direct_edge_evidence", "source,source_building_id,pair_id"),
)
REQUIRED_TABLES = {name for name, _ in LOGICAL_TABLES} | {"runs", "validations", "metrics"}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str | None = None


@dataclass(frozen=True)
class ValidationReport:
    artifact_path: Path
    run_id: str | None
    scope: str | None
    logical_sha256: str | None
    checks: tuple[Check, ...]
    metrics: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(row.passed for row in self.checks)

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(row.name for row in self.checks if not row.passed)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _check(checks: list[Check], name: str, expected: Any, actual: Any, detail: str | None = None) -> None:
    checks.append(Check(name, expected == actual, expected, actual, detail))


def _row_sha(row: Mapping[str, Any]) -> str:
    return _sha({key: row[key] for key in row.keys() if key not in ("run_id", "record_sha256")})


# --------------------------------------------------------------------------- independent rules

def classify_continuity(row: Mapping[str, Any]) -> str:
    """Continuity rule v2: 200 and Hamming <= 32 and |(w*h_e1)/(h*w_e1) - 1| <= 2%."""

    status = row.get("status")
    if status is None:
        return "fetch_error"
    if int(status) in (404, 410):
        return "unavailable"
    if int(status) != 200:
        return "fetch_error"
    if row.get("wh") is None or row.get("hamming") is None:
        return "decode_failed"
    w, h = int(row["wh"][0]), int(row["wh"][1])
    w1, h1 = int(row["e1_wh"][0]), int(row["e1_wh"][1])
    aspect = abs((w * h1) / (h * w1) - 1.0) if min(w, h, w1, h1) > 0 else float("inf")
    if int(row["hamming"]) <= CONT_HAMMING_MAX and aspect <= CONT_ASPECT_REL_MAX:
        return "continuous"
    return "content_changed"


def rep_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    width = int(row.get("original_width") or 0)
    height = int(row.get("original_height") or 0)
    short_edge = min(width, height) if width and height else 0
    ordinal = row.get("source_ordinal")
    role = row.get("primary_role")
    return (
        0 if row.get("fingerprint_status") == "success" else 1,
        0 if short_edge >= 512 else 1,
        -(width * height), -short_edge, -max(width, height), 0,
        int(row.get("low_information") or 0), 0 if role == "cover" else (1 if role == "gallery" else 2),
        1 if ordinal is None else 0, int(ordinal or 0),
        str(row["source_asset_id"]), str(row.get("candidate_id") or ""),
    )


def rep_score(row: Mapping[str, Any]) -> dict[str, Any]:
    width = int(row.get("original_width") or 0)
    height = int(row.get("original_height") or 0)
    short_edge = min(width, height) if width and height else 0
    return {
        "version": REPRESENTATIVE_SCORE_VERSION, "verified_fingerprint_success": row.get("fingerprint_status") == "success",
        "usable_resolution": short_edge >= 512, "pixel_area": width * height, "short_edge": short_edge,
        "long_edge": max(width, height), "crop_overlay_loss": None,
        "crop_overlay_policy": "neutral_when_unavailable_in_frozen_e2_e3",
        "e3_low_information": bool(row.get("low_information")), "e3_primary_role": row.get("primary_role"),
        "e3_source_ordinal": row.get("source_ordinal"), "stable_image_key": str(row["source_asset_id"]),
    }


def retryable(error_kind: str | None) -> int:
    value = (error_kind or "").casefold()
    return int(any(token in value for token in ("timeout", "connection", "http_5", "rate", "429", "temporary")))


def page_pos(occ: Mapping[str, Any]) -> tuple[Any, ...]:
    membership, ordinal = occ.get("membership_ordinal"), occ.get("ordinal")
    return (
        1 if membership is None else 0, int(membership or 0), str(occ["source_project_id"]),
        {"cover": 0, "gallery": 1}.get(str(occ["role"]), 2), 1 if ordinal is None else 0, int(ordinal or 0),
        str(occ["occurrence_id"]),
    )


def browser_raster(url: str) -> bool:
    path = urlsplit(url).path
    if any(token in RASTER_FORMAT_TRANSFORMS for segment in path.split("/") for token in segment.split(",")):
        return True
    last = path.rsplit("/", 1)[-1]
    return ("." in last) and last.rsplit(".", 1)[-1].lower() in RASTER_EXTENSIONS


def divisare_occurrence(asset_id: str, occurrences: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    own = [occ for occ in occurrences if occ["source_asset_id"] == asset_id and occ["occurrence_url"]]
    own.sort(key=lambda occ: (0 if occ["role"] == "gallery" else 1, page_pos(occ)))
    for occ in own:
        parts = urlsplit(str(occ["occurrence_url"]))
        if parts.scheme == "https" and parts.hostname == "images.divisare.com" and browser_raster(str(occ["occurrence_url"])):
            return occ
    return None


def expected_url(source: str, rep: Mapping[str, Any], occurrences: Sequence[Mapping[str, Any]]) -> tuple[str, str, str, str | None] | None:
    """(serving_url, url_policy, url_provenance, serving_url_occurrence_id) or None when not servable."""

    if source == "architizer":
        canonical = str(rep.get("canonical_url") or "")
        if re.fullmatch(NEON_ARCHITIZER_PATTERN, canonical) is None:
            return None
        return (neon_form(canonical), URL_POLICY_ARCHITIZER, URL_PROVENANCE_ARCHITIZER, None)
    if source == "divisare":
        occ = divisare_occurrence(str(rep["source_asset_id"]), occurrences)
        if occ is None:
            return None
        return (str(occ["occurrence_url"]), URL_POLICY_DIVISARE, "e2_occurrence_url_verbatim:" + str(occ["role"]),
                str(occ["occurrence_id"]))
    return None


def neon_form(url: str) -> str:
    return re.sub(NEON_ARCHITIZER_PATTERN, NEON_ARCHITIZER_REPLACEMENT, url)


def expected_disposition(asset: Mapping[str, Any], cand: Mapping[str, Any] | None, attested: bool, servable: bool = True) -> tuple[str, str]:
    if asset["fingerprint_status"] == "failed":
        return "exclusion", "fingerprint_or_decoder_failure"
    if asset["fingerprint_status"] != "success":
        return "exclusion", "e1_ledger_excluded"
    if cand is None:
        return "unresolved", "successful_e2_asset_missing_e3_candidate"
    if int(cand["low_information"]):
        return "exclusion", "low_information"
    if not servable:
        return "unresolved", REASON_NO_SERVING_URL
    if not attested:
        return "unresolved", "source_continuity_not_attested"
    return "gallery", "eligible_success_continuity_attested"


# --------------------------------------------------------------------------- replay

def _with_sha(row: dict[str, Any]) -> dict[str, Any]:
    row["record_sha256"] = _row_sha(row)
    return row


def _compare(failures: list[str], table: str, tag: str, stored: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]) -> None:
    """Every stored column (except run_id) must equal the value re-derived from E2/E3."""

    if len(stored) != len(expected):
        failures.append(f"row replay:{table}:{tag}:row_count {len(stored)}!={len(expected)}")
        return
    for got, want in zip(stored, expected):
        got_cols = {key: got[key] for key in got.keys() if key != "run_id"}
        if got_cols != dict(want):
            bad = sorted(key for key in set(got_cols) | set(want) if got_cols.get(key, object()) != want.get(key, object()))
            failures.append(f"row replay:{table}:{tag}:{','.join(bad[:6])}")
            return


def _replay_building(
    out: sqlite3.Connection, e2: sqlite3.Connection, e3: sqlite3.Connection, run_id: str,
    e2_run: str, e3_run: str, building: Mapping[str, Any], attested: Mapping[str, bool],
    failures: list[str], counters: dict[str, int],
) -> None:
    source, bid = str(building["source"]), str(building["source_building_id"])
    tag = f"{source}/{bid}"
    counters["buildings"] += 1
    stored = {
        name: [dict(r) for r in out.execute(
            f"SELECT * FROM {name} WHERE run_id=? AND source=? AND source_building_id=? ORDER BY {order}", (run_id, source, bid))]
        for name, order in (("group_members", "source_asset_id"), ("gallery_groups", "sort_order"),
                            ("exclusion_ledger", "source_asset_id"), ("unresolved_ledger", "source_asset_id"),
                            ("source_occurrences", "occurrence_id"), ("direct_edge_evidence", "pair_id"))
    }
    for name, rows in stored.items():
        counters[name] += len(rows)
    sel = e3.execute(
        "SELECT selection_id,selection_record_sha256,name,detail_json FROM selected_buildings WHERE run_id=? AND source=? "
        "AND entity_type='building' AND source_entity_id=?", (e3_run, source, bid)).fetchall()
    if len(sel) != 1:
        failures.append(f"E3 selection lineage:{tag}")
        return
    sel_row = sel[0]
    assets = {str(r["source_asset_id"]): dict(r) for r in e2.execute(
        "SELECT ba.source_asset_id,ba.relation_record_sha256,a.fingerprint_status,a.canonical_url,a.normalized_pixel_sha256,"
        "a.original_width,a.original_height,a.source_record_sha256,a.provenance_json,a.error_kind,a.error_message,"
        "em.cluster_id,pnm.node_id FROM building_assets ba JOIN assets a ON a.run_id=ba.run_id AND a.source=ba.source "
        "AND a.source_asset_id=ba.source_asset_id LEFT JOIN exact_pixel_cluster_members em ON em.run_id=a.run_id "
        "AND em.source=a.source AND em.source_asset_id=a.source_asset_id LEFT JOIN phash_node_members pnm "
        "ON pnm.run_id=a.run_id AND pnm.source=a.source AND pnm.source_asset_id=a.source_asset_id "
        "WHERE ba.run_id=? AND ba.source=? AND ba.source_building_id=?", (e2_run, source, bid))}
    cands = {str(r["source_asset_id"]): dict(r) for r in e3.execute(
        "SELECT candidate_id,source_asset_id,primary_role,source_ordinal,role_rank,low_information,"
        "candidate_record_sha256,normalized_pixel_sha256,phash_node_id FROM image_candidates WHERE run_id=? AND selection_id=?",
        (e3_run, sel_row["selection_id"]))}
    occs = sorted((dict(r) for r in e2.execute(
        "SELECT o.occurrence_id,o.source_project_id,spb.membership_ordinal,o.raw_asset_key,o.source_asset_id,o.resolution_status,"
        "o.role,o.ordinal,o.occurrence_url,o.source_record_sha256,o.detail_json,spb.source_record_sha256 AS m_sha "
        "FROM source_project_buildings spb CROSS JOIN project_asset_occurrences o ON o.run_id=spb.run_id "
        "AND o.source=spb.source AND o.source_project_id=spb.source_project_id "
        "WHERE spb.run_id=? AND spb.source=? AND spb.source_building_id=?", (e2_run, source, bid))),
        key=lambda r: str(r["occurrence_id"]))
    # dispositions + ledgers
    exp_excl: list[dict[str, Any]] = []
    exp_unres: list[dict[str, Any]] = []
    gal: list[dict[str, Any]] = []
    for aid in sorted(assets):
        asset, cand = assets[aid], cands.get(aid)
        if cand is not None and asset["fingerprint_status"] == "success" and (
                cand["normalized_pixel_sha256"] != asset["normalized_pixel_sha256"] or cand["phash_node_id"] != asset["node_id"]):
            failures.append(f"E2/E3 pixel/node mismatch:{tag}:{aid}")
        servable = expected_url(source, {**asset, "source_asset_id": aid}, occs) is not None if asset["fingerprint_status"] == "success" else True
        disp, reason = expected_disposition(asset, cand, bool(attested.get(source)), servable)
        lineage = {"e2_asset_record_sha256": asset["source_record_sha256"], "e2_relation_record_sha256": asset["relation_record_sha256"],
                   "e3_candidate_record_sha256": None if cand is None else cand["candidate_record_sha256"]}
        if disp == "exclusion":
            provenance = json.loads(asset["provenance_json"] or "{}")
            exp_excl.append(_with_sha({
                "source": source, "source_building_id": bid, "source_asset_id": aid, "reason_code": reason,
                "decision_engine": "frozen_e1_e2_e3_evidence", "decision_version": BUILDER_VERSION,
                "pixel_verification_status": "frozen_success" if asset["fingerprint_status"] == "success" else "frozen_" + str(asset["fingerprint_status"]),
                "fingerprint_status": asset["fingerprint_status"],
                "e1_exclusion_reason": provenance.get("exclusion_reason") if isinstance(provenance, dict) else None,
                "retryable": retryable(asset["error_kind"]), "error_kind": asset["error_kind"], "error_message": asset["error_message"],
                **lineage}))
        elif disp == "unresolved":
            exp_unres.append(_with_sha({
                "source": source, "source_building_id": bid, "source_asset_id": aid, "reason_code": reason,
                "decision_engine": UNRESOLVED_ENGINE[reason], "decision_version": BUILDER_VERSION,
                "pixel_verification_status": "frozen_success", "retryable": 0, **lineage}))
        else:
            item = dict(asset)
            item.update(source_asset_id=aid, candidate_id=cand["candidate_id"], primary_role=cand["primary_role"],
                        source_ordinal=cand["source_ordinal"], role_rank=cand["role_rank"],
                        low_information=int(cand["low_information"]), candidate_record_sha256=cand["candidate_record_sha256"],
                        reason=reason)
            gal.append(item)
    # exact classes -> groups/members in source page order
    classes: dict[str, list[dict[str, Any]]] = {}
    for item in gal:
        classes.setdefault(str(item["normalized_pixel_sha256"]), []).append(item)
    positions: dict[str, list[tuple[Any, ...]]] = {}
    for occ in occs:
        if occ["resolution_status"] == "linked" and occ["source_asset_id"] is not None:
            positions.setdefault(str(occ["source_asset_id"]), []).append(page_pos(occ))
    keyed = []
    for pixel, items in classes.items():
        ordered = sorted(items, key=rep_key)
        rep = ordered[0]
        page = [p for it in ordered for p in positions.get(str(it["source_asset_id"]), ())]
        if page:
            key = (0, min(page), (), str(rep["source_asset_id"]))
        else:
            ranks = [(1 if it["role_rank"] is None else 0, int(it["role_rank"] or 0), 1 if it["source_ordinal"] is None else 0,
                      int(it["source_ordinal"] or 0)) for it in ordered]
            key = (1, (), min(ranks), str(rep["source_asset_id"]))
        keyed.append((key, pixel, ordered))
    keyed.sort(key=lambda item: item[0])
    exp_groups: list[dict[str, Any]] = []
    exp_members: list[dict[str, Any]] = []
    for sort_order, (key, pixel, ordered) in enumerate(keyed):
        rep = ordered[0]
        rep_id = str(rep["source_asset_id"])
        counters["sort_basis_" + ("source_page_order" if key[0] == 0 else "e3_rank")] += 1
        group_id = "vnx_imggrp_" + _sha({"grouping_version": GROUPING_VERSION, "normalized_pixel_sha256": pixel,
                                         "representative_source_asset_id": rep_id, "source": source, "source_building_id": bid})[:32]
        anchor: dict[str, Any] = {"basis": "source_page_order" if key[0] == 0 else "e3_rank"}
        if key[0] == 0:
            pos = key[1]
            anchor.update(membership_ordinal=None if pos[0] else pos[1], source_project_id=pos[2], role_page_rank=pos[3],
                          ordinal=None if pos[4] else pos[5], occurrence_id=pos[6])
        else:
            anchor["e3_rank"] = list(key[2])
        url = expected_url(source, rep, occs)
        if url is None:
            failures.append(f"url policy (representative not servable):{tag}:{rep_id}")
            return
        counters["architizer_neon_divergent"] += int(source == "architizer" and url[0] != neon_form(str(rep["canonical_url"])))
        counters["architizer_non_ascii"] += int(source == "architizer" and not url[0].isascii())
        exp_groups.append(_with_sha({
            "group_id": group_id, "source": source, "source_building_id": bid, "sort_order": sort_order,
            "representative_source_asset_id": rep_id, "representative_candidate_id": rep["candidate_id"],
            "representative_pixel_sha256": pixel, "representative_phash_node_id": rep["node_id"],
            "representative_width": rep["original_width"], "representative_height": rep["original_height"],
            "member_count": len(ordered), "grouping_basis": "exact_normalized_pixel_sha256",
            "representative_score_json": _json(rep_score(rep)), "sort_basis": anchor["basis"], "sort_anchor_json": _json(anchor),
            "serving_url": url[0], "url_policy": url[1], "url_provenance": url[2], "serving_url_occurrence_id": url[3],
            "continuity_status": "continuity_attested", "per_asset_pixel_status": PER_ASSET_PIXEL_STATUS}))
        for rank, member in enumerate(ordered):
            exp_members.append(_with_sha({
                "group_id": group_id, "source": source, "source_building_id": bid, "source_asset_id": str(member["source_asset_id"]),
                "candidate_id": member["candidate_id"], "member_rank": rank,
                "member_role": "representative" if rank == 0 else "exact_member",
                "attachment_basis": "representative" if rank == 0 else "exact_pixel",
                "normalized_pixel_sha256": member["normalized_pixel_sha256"], "phash_node_id": member["node_id"],
                "exact_cluster_id": member["cluster_id"], "original_width": member["original_width"],
                "original_height": member["original_height"], "e2_asset_record_sha256": member["source_record_sha256"],
                "e2_relation_record_sha256": member["relation_record_sha256"],
                "e3_candidate_record_sha256": member["candidate_record_sha256"], "continuity_status": "continuity_attested",
                "reason_code": member["reason"]}))
    exp_members.sort(key=lambda row: row["source_asset_id"])
    # deferred direct edges (no star), every E2 scope, hamming >= 1
    by_node: dict[str, list[dict[str, Any]]] = {}
    for group in exp_groups:
        by_node.setdefault(str(group["representative_phash_node_id"]), []).append(group)
    counters["same_node_group_pairs"] += sum(len(v) * (len(v) - 1) // 2 for v in by_node.values())
    nodes = sorted(by_node)
    exp_edges: dict[str, dict[str, Any]] = {}
    if len(nodes) >= 2:
        marks = ",".join("?" for _ in nodes)
        scopes = ",".join("'" + scope + "'" for scope in EDGE_SCOPES)
        for edge in e2.execute(
            "SELECT edge_id,left_node_id,right_node_id,hamming_distance,edge_scope,edge_record_sha256 FROM phash_edges "
            f"INDEXED BY sqlite_autoindex_phash_edges_2 WHERE run_id=? AND +edge_scope IN ({scopes}) AND +hamming_distance>=1 "
            f"AND left_node_id IN ({marks}) AND right_node_id IN ({marks})", (e2_run, *nodes, *nodes)):
            for left in by_node.get(str(edge["left_node_id"]), ()):
                for right in by_node.get(str(edge["right_node_id"]), ()):
                    if left["group_id"] == right["group_id"]:
                        continue
                    pair_id = "vnx_dedge_" + _sha({"edge_record_sha256": edge["edge_record_sha256"], "left_group_id": left["group_id"],
                                                   "right_group_id": right["group_id"], "version": GROUPING_VERSION})[:32]
                    exp_edges[pair_id] = _with_sha({
                        "pair_id": pair_id, "source": source, "source_building_id": bid, "left_group_id": left["group_id"],
                        "right_group_id": right["group_id"], "left_representative_asset_id": left["representative_source_asset_id"],
                        "right_representative_asset_id": right["representative_source_asset_id"], "edge_id": edge["edge_id"],
                        "left_node_id": edge["left_node_id"], "right_node_id": edge["right_node_id"],
                        "hamming_distance": int(edge["hamming_distance"]), "edge_scope": edge["edge_scope"],
                        "edge_record_sha256": edge["edge_record_sha256"], "attachment_status": DEFERRED, "transitive_closure_allowed": 0})
    exp_occ = [_with_sha({
        "source": source, "source_building_id": bid, "occurrence_id": o["occurrence_id"], "source_project_id": o["source_project_id"],
        "membership_ordinal": o["membership_ordinal"], "raw_asset_key": o["raw_asset_key"], "source_asset_id": o["source_asset_id"],
        "resolution_status": o["resolution_status"], "role": o["role"], "ordinal": o["ordinal"], "occurrence_url": o["occurrence_url"],
        "occurrence_record_sha256": o["source_record_sha256"], "membership_record_sha256": o["m_sha"], "detail_json": o["detail_json"],
    }) for o in occs]
    detail = json.loads(sel_row["detail_json"] or "{}").get("building_summary", {})
    exp_building = _with_sha({
        "source": source, "source_building_id": bid, "selection_id": sel_row["selection_id"],
        "name": None if sel_row["name"] is None else str(sel_row["name"]),
        "e3_selection_record_sha256": sel_row["selection_record_sha256"],
        "e3_successful_asset_count": int(detail.get("successful_asset_count", 0)), "input_asset_count": len(assets),
        "gallery_member_count": len(exp_members), "gallery_group_count": len(exp_groups), "exclusion_count": len(exp_excl),
        "unresolved_count": len(exp_unres), "source_occurrence_count": len(exp_occ), "direct_edge_evidence_count": len(exp_edges)})
    if len(exp_members) + len(exp_excl) + len(exp_unres) != len(assets):
        failures.append(f"per-building accounting:{tag}")
    _compare(failures, "buildings", tag, [building], [exp_building])
    _compare(failures, "gallery_groups", tag, stored["gallery_groups"], exp_groups)
    _compare(failures, "group_members", tag, stored["group_members"], exp_members)
    _compare(failures, "exclusion_ledger", tag, stored["exclusion_ledger"], exp_excl)
    _compare(failures, "unresolved_ledger", tag, stored["unresolved_ledger"], exp_unres)
    _compare(failures, "source_occurrences", tag, stored["source_occurrences"], exp_occ)
    _compare(failures, "direct_edge_evidence", tag, stored["direct_edge_evidence"], [exp_edges[k] for k in sorted(exp_edges)])


def _logical(connection: sqlite3.Connection, run_id: str) -> str:
    digest = hashlib.sha256((LOGICAL_VERSION + "\n").encode("utf-8"))
    for table, order in LOGICAL_TABLES:
        digest.update(("#" + table + "\n").encode("utf-8"))
        for (value,) in connection.execute(f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)):
            digest.update((str(value) + "\n").encode("utf-8"))
    return digest.hexdigest()


def _validate_continuity(root: Path, run: Mapping[str, Any], checks: list[Check], metrics: dict[str, Any]) -> dict[str, bool]:
    summary_path = root / str(run["continuity_summary_path"])
    current = _sha_file(summary_path) if summary_path.is_file() else None
    _check(checks, "continuity_summary_sha", run["continuity_summary_sha256"], current)
    if current is None:
        return {}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    _check(checks, "continuity_not_partial", False, bool(summary.get("partial_smoke")))
    _check(checks, "continuity_rule_and_seed", (CONT_RULE_VERSION, CONT_SAMPLE_SEED),
           (summary.get("rule_version"), summary.get("sample_seed")))
    jsonl_path = root / str(summary.get("rows_jsonl", {}).get("path", ""))
    jsonl_sha = _sha_file(jsonl_path) if jsonl_path.is_file() else None
    _check(checks, "continuity_jsonl_sha", summary.get("rows_jsonl", {}).get("sha256"), jsonl_sha)
    rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()] if jsonl_sha else []
    mismatched = [f"{r['source']}:{r['source_asset_id']}" for r in rows if classify_continuity(r) != r.get("class")]
    _check(checks, "continuity_row_reclassification", [], mismatched[:20], f"rows={len(rows)}")
    recomputed: dict[str, bool] = {}
    for source in ("architizer", "divisare"):
        part = [r for r in rows if r["source"] == source]
        classes = [classify_continuity(r) for r in part]
        n = len(part)
        rate = classes.count("continuous") / n if n else 0.0
        changed = classes.count("content_changed")
        ok = n == CONT_SAMPLE_SIZE and len({r["source_asset_id"] for r in part}) == n and rate >= CONT_RATE_MIN and changed == 0
        recomputed[source] = ok
        metrics[f"continuity.{source}"] = {"n": n, "continuous_rate": round(rate, 6), "content_changed": changed,
                                           "unavailable": classes.count("unavailable"), "attested": ok}
        _check(checks, f"continuity_summary_verdict_{source}", ok, bool(summary.get("sources", {}).get(source, {}).get("continuity_attested")))
    run_cont = json.loads(run["continuity_json"])
    _check(checks, "run_continuity_pin", recomputed, {s: bool(v["continuity_attested"]) for s, v in run_cont["sources"].items()})
    return recomputed


def _validate_manifest(
    manifest_path: Path, artifact: Path, root: Path, run: Mapping[str, Any], logical: str,
    table_counts: Mapping[str, int], checks: list[Check],
) -> None:
    path = manifest_path if manifest_path.is_absolute() else root / manifest_path
    if not path.is_file():
        checks.append(Check("manifest_exists", False, True, False, str(path)))
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    output = manifest.get("output", {})
    pins = manifest.get("input_pins", {})
    _check(checks, "manifest_output_sha", _sha_file(artifact), output.get("sha256"))
    _check(checks, "manifest_output_size", artifact.stat().st_size, output.get("size_bytes"))
    _check(checks, "manifest_run_logical_config",
           (run["run_id"], run["scope"], logical, run["config_sha256"]),
           (manifest.get("run_id"), manifest.get("scope"), manifest.get("logical_sha256"), manifest.get("config_sha256")))
    _check(checks, "manifest_row_counts", dict(table_counts), manifest.get("row_counts"))
    _check(checks, "manifest_input_pins",
           (run["e2_sha256_before"], run["e3_sha256_before"], run["contract_sha256"], run["continuity_summary_sha256"], run["freeze_sha256"]),
           (pins.get("e2", {}).get("sha256"), pins.get("e3", {}).get("sha256"), pins.get("contract", {}).get("sha256"),
            pins.get("continuity_summary", {}).get("sha256"), pins.get("freeze", {}).get("sha256")))


def validate_gallery_full(
    artifact_path: Path,
    *,
    repo_root: Path,
    verify_input_hashes: bool = False,
    integrity_check: bool = True,
    manifest_path: Path | None = None,
) -> ValidationReport:
    artifact = artifact_path.resolve()
    root = repo_root.resolve()
    checks: list[Check] = []
    metrics: dict[str, Any] = {}
    if not artifact.is_file():
        return ValidationReport(artifact, None, None, None, (Check("artifact_exists", False, True, False),), metrics)
    sidecars = [str(Path(str(artifact) + s)) for s in ("-wal", "-shm", "-journal", ".building") if Path(str(artifact) + s).exists()]
    _check(checks, "sqlite_sidecars_absent", [], sidecars)
    out = _open(artifact)
    try:
        _check(checks, "application_id", APPLICATION_ID, int(out.execute("PRAGMA application_id").fetchone()[0]))
        _check(checks, "schema_version", SCHEMA_VERSION, int(out.execute("PRAGMA user_version").fetchone()[0]))
        if integrity_check:
            _check(checks, "sqlite_integrity", "ok", str(out.execute("PRAGMA integrity_check").fetchone()[0]))
        tables = {str(r[0]) for r in out.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _check(checks, "required_tables", set(), REQUIRED_TABLES - tables)
        runs = out.execute("SELECT * FROM runs").fetchall()
        _check(checks, "single_run", 1, len(runs))
        if len(runs) != 1:
            return ValidationReport(artifact, None, None, None, tuple(checks), metrics)
        run = runs[0]
        run_id = str(run["run_id"])
        _check(checks, "run_status", "complete", run["status"])
        _check(checks, "request_counts", (0, 0, 0), (run["network_requests"], run["vision_requests"], run["llm_requests"]))
        _check(checks, "contract_version", CONTRACT_VERSION, run["contract_version"])
        _check(checks, "rule_versions",
               (BUILDER_VERSION, GROUPING_VERSION, REPRESENTATIVE_SCORE_VERSION, SORT_ORDER_VERSION, URL_POLICY_VERSION),
               (run["builder_version"], run["grouping_version"], run["representative_score_version"],
                run["sort_order_version"], run["url_policy_version"]))
        try:
            config = json.loads(run["config_json"])
        except (TypeError, ValueError):
            config = None
        _check(checks, "config_sha", run["config_sha256"], None if config is None else _sha(config))
        _check(checks, "config_canonical_json", run["config_json"], None if config is None else _json(config))
        cfg = config if isinstance(config, dict) else {}
        _check(checks, "config_scope",
               {"scope": run["scope"], "limit": run["scope_limit"], "seed": run["scope_seed"]}, cfg.get("scope"))
        _check(checks, "config_versions",
               (BUILDER_VERSION, URL_POLICY_VERSION, list(EDGE_SCOPES), 0, 0, 0),
               (cfg.get("builder_version"), cfg.get("serving_url", {}).get("version"),
                cfg.get("grouping", {}).get("direct_edge_evidence", {}).get("scopes") if isinstance(cfg.get("grouping", {}).get("direct_edge_evidence"), dict) else None,
                cfg.get("network_requests"), cfg.get("vision_requests"), cfg.get("llm_requests")))
        # input pins
        e2_path, e3_path = root / E2_PATH, root / E3_PATH
        _check(checks, "e2_path", E2_PATH, run["e2_path"])
        _check(checks, "e3_path", E3_PATH, run["e3_path"])
        _check(checks, "e2_size", EXPECTED_E2_SIZE, e2_path.stat().st_size if e2_path.is_file() else None)
        _check(checks, "e3_size", EXPECTED_E3_SIZE, e3_path.stat().st_size if e3_path.is_file() else None)
        _check(checks, "e2_pre_post_pin", (EXPECTED_E2_SHA256, EXPECTED_E2_SHA256), (run["e2_sha256_before"], run["e2_sha256_after"]))
        _check(checks, "e3_pre_post_pin", (EXPECTED_E3_SHA256, EXPECTED_E3_SHA256), (run["e3_sha256_before"], run["e3_sha256_after"]))
        if verify_input_hashes:
            _check(checks, "e2_current_sha", EXPECTED_E2_SHA256, _sha_file(e2_path))
            _check(checks, "e3_current_sha", EXPECTED_E3_SHA256, _sha_file(e3_path))
        freeze_path = root / str(run["freeze_path"])
        _check(checks, "freeze_sha", run["freeze_sha256"], _sha_file(freeze_path) if freeze_path.is_file() else None)
        if freeze_path.is_file():
            frozen = {row["role"]: (row["path"], row["size_bytes"], row["sha256"]) for row in json.loads(freeze_path.read_text(encoding="utf-8"))["frozen_inputs"]}
            _check(checks, "freeze_pins_e2_e3",
                   [(E2_PATH, EXPECTED_E2_SIZE, EXPECTED_E2_SHA256), (E3_PATH, EXPECTED_E3_SIZE, EXPECTED_E3_SHA256)],
                   [tuple(frozen.get("cross_source_e2_full_v5", ())), tuple(frozen.get("cross_source_e3_full_v1", ()))])
        contract = root / str(run["contract_path"])
        contract_text = contract.read_text(encoding="utf-8") if contract.is_file() else ""
        _check(checks, "contract_sha", run["contract_sha256"], _sha_file(contract) if contract.is_file() else None)
        _check(checks, "contract_v13_anchors", [], [a for a in CONTRACT_ANCHORS if a not in contract_text])
        wp1_now = {rel: (_sha_file(root / rel) if (root / rel).is_file() else None) for rel in WP1_ARTIFACT_PINS}
        _check(checks, "wp1_artifacts_unchanged", WP1_ARTIFACT_PINS, wp1_now)
        _check(checks, "wp1_pins_recorded", WP1_ARTIFACT_PINS, json.loads(run["wp1_pins_json"]))
        attested = _validate_continuity(root, run, checks, metrics)
        e2, e3 = _open(e2_path), _open(e3_path)
        try:
            for connection in (e2, e3):
                connection.execute("PRAGMA cache_size=-524288")
            e2_run, e3_run = str(run["e2_run_id"]), str(run["e3_run_id"])
            # The builder requires exactly ONE complete run in each input; it must be the pinned one.
            _check(checks, "e2_single_complete_run", [e2_run], [str(r[0]) for r in e2.execute("SELECT run_id FROM e2_runs WHERE status='complete'")])
            _check(checks, "e3_single_complete_run", [e3_run], [str(r[0]) for r in e3.execute("SELECT run_id FROM selection_runs WHERE status='complete'")])
            population = [(str(r[0]), str(r[1])) for r in e3.execute(
                "SELECT source,source_building_id FROM selected_buildings WHERE run_id=? AND entity_type='building'", (e3_run,))]
            _check(checks, "e3_population", FULL_BUILDING_COUNT, len(population))
            if run["scope"] == "full":
                expected_scope = sorted(population)
            else:
                limit = int(run["scope_limit"])
                _check(checks, "ladder_seed", LADDER_SEED, run["scope_seed"])
                ranked = sorted(population, key=lambda p: (hashlib.sha256((LADDER_SEED + "\0" + p[0] + "\0" + p[1]).encode("utf-8")).hexdigest(), p[0], p[1]))
                expected_scope = sorted(ranked[:limit])
            buildings = [dict(r) for r in out.execute("SELECT * FROM buildings WHERE run_id=? ORDER BY source,source_building_id", (run_id,))]
            _check(checks, "scope_building_set", expected_scope, [(b["source"], b["source_building_id"]) for b in buildings], f"n={len(buildings)}")
            _check(checks, "run_building_count", len(buildings), run["building_count"])
            scope_manifest = _sha([[b["source"], b["source_building_id"], b["e3_selection_record_sha256"]] for b in buildings])
            _check(checks, "run_id_recomputed", RUN_ID_PREFIX + _sha({
                "config_sha256": run["config_sha256"], "contract_sha256": run["contract_sha256"],
                "continuity_summary_sha256": run["continuity_summary_sha256"], "e2_sha256": run["e2_sha256_before"],
                "e3_sha256": run["e3_sha256_before"], "scope_manifest_sha256": scope_manifest})[:24], run_id)
            failures: list[str] = []
            counters = {name: 0 for name, _ in LOGICAL_TABLES}
            counters.update(sort_basis_source_page_order=0, sort_basis_e3_rank=0, same_node_group_pairs=0, architizer_neon_divergent=0,
                            architizer_non_ascii=0)
            for building in buildings:
                _replay_building(out, e2, e3, run_id, e2_run, e3_run, building, attested, failures, counters)
            _check(checks, "per_building_replay", [], failures[:25], f"failures={len(failures)}")
        finally:
            e2.close()
            e3.close()
        table_counts = {name: int(out.execute(f"SELECT count(*) FROM {name} WHERE run_id=?", (run_id,)).fetchone()[0]) for name, _ in LOGICAL_TABLES}
        _check(checks, "no_orphan_rows", {name: counters[name] for name, _ in LOGICAL_TABLES}, table_counts)
        totals = out.execute(
            "SELECT sum(input_asset_count),sum(gallery_member_count),sum(exclusion_count),sum(unresolved_count) FROM buildings WHERE run_id=?",
            (run_id,)).fetchone()
        _check(checks, "global_accounting", int(totals[0] or 0), int(totals[1] or 0) + int(totals[2] or 0) + int(totals[3] or 0))
        _check(checks, "global_accounting_rows", int(totals[0] or 0),
               table_counts["group_members"] + table_counts["exclusion_ledger"] + table_counts["unresolved_ledger"])
        _check(checks, "architizer_url_neon_form_identical", 0, counters["architizer_neon_divergent"])
        _check(checks, "star_attachment_absent", 0, int(out.execute(
            "SELECT count(*) FROM direct_edge_evidence WHERE run_id=? AND attachment_status<>?", (run_id, DEFERRED)).fetchone()[0]))
        logical = _logical(out, run_id)
        _check(checks, "logical_sha", run["logical_sha256"], logical)
        per_source: dict[str, dict[str, int]] = {}
        for row in out.execute(
            "SELECT source,count(*),sum(input_asset_count),sum(gallery_member_count),sum(gallery_group_count),sum(exclusion_count),"
            "sum(unresolved_count),sum(source_occurrence_count),sum(direct_edge_evidence_count),sum(input_asset_count=0) "
            "FROM buildings WHERE run_id=? GROUP BY source ORDER BY source", (run_id,)):
            per_source[str(row[0])] = dict(zip(
                ("buildings", "input", "gallery", "groups", "exclusion", "unresolved", "occurrences", "deferred_edges", "zero_asset_buildings"),
                [int(v or 0) for v in row[1:]]))
        metrics.update({"per_source": per_source, "table_counts": table_counts,
                        "sort_basis": {k[len("sort_basis_"):]: v for k, v in counters.items() if k.startswith("sort_basis_")},
                        "same_phash_node_group_pairs": counters["same_node_group_pairs"],
                        "architizer_serving_url_neon_divergent": counters["architizer_neon_divergent"],
                        "architizer_serving_url_non_ascii_path": counters["architizer_non_ascii"]})
        if manifest_path is not None:
            _validate_manifest(manifest_path, artifact, root, run, logical, table_counts, checks)
        return ValidationReport(artifact, run_id, str(run["scope"]), logical, tuple(checks), metrics)
    finally:
        out.close()


__all__ = ["Check", "VALIDATOR_VERSION", "ValidationReport", "classify_continuity", "validate_gallery_full"]
