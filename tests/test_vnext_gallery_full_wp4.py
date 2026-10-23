"""WP4 S2 full gallery v1: tiny synthetic fixtures only (no big DBs, no network)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import canonical.vnext_gallery_full as full
import canonical.vnext_gallery_full_validator as validator
from tools import vnext_asset_continuity_sample as cont


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- unit: rules

def _gallery_asset(asset_id: str, pixel: str, node: str, *, width: int = 1024, height: int = 768, ordinal: int = 1) -> dict:
    return {
        "source_asset_id": asset_id, "candidate_id": "c-" + asset_id, "fingerprint_status": "success",
        "normalized_pixel_sha256": pixel, "node_id": node, "cluster_id": "cl-" + pixel[:4],
        "original_width": width, "original_height": height, "low_information": 0, "primary_role": "gallery",
        "role_rank": 1, "source_ordinal": ordinal, "canonical_url": f"http://architizer-prod.imgix.net/media/{asset_id}_\u00e9.jpg?w=1680&q=60",
        "source_record_sha256": _h("a" + asset_id), "relation_record_sha256": _h("r" + asset_id),
        "candidate_record_sha256": _h("c" + asset_id), "disposition_reason": "eligible_success_continuity_attested",
    }


def _occ(occ_id: str, asset_id: str, role: str, ordinal: int, *, project: str = "p1", url: str | None = None) -> dict:
    return {
        "occurrence_id": occ_id, "source_project_id": project, "membership_ordinal": 0, "source_asset_id": asset_id,
        "resolution_status": "linked", "role": role, "ordinal": ordinal,
        "occurrence_url": url or f"https://images.divisare.com//images/f_auto,q_auto,w_auto/v1/project_images/{asset_id}/{role}.jpg",
    }


def test_exact_classes_only_and_page_order() -> None:
    assets = [
        _gallery_asset("late-dup", "1" * 64, "n1", width=800, height=600, ordinal=5),
        _gallery_asset("hero", "1" * 64, "n1", width=1200, height=900, ordinal=0),
        _gallery_asset("near", "2" * 64, "n2", ordinal=1),
        _gallery_asset("same-node", "3" * 64, "n1", ordinal=2),
    ]
    occurrences = [
        _occ("o-near", "near", "gallery", 0),
        _occ("o-hero-g", "hero", "gallery", 3),
        _occ("o-hero-c", "hero", "cover", 0),
        _occ("o-dup", "late-dup", "gallery", 4),
        _occ("o-same", "same-node", "gallery", 1),
    ]
    groups, members = full.build_groups("architizer", "b1", assets, occurrences)
    # 3 exact pixel classes; identical pHash node with different pixels never groups.
    assert len(groups) == 3
    assert [g["representative_source_asset_id"] for g in groups] == ["hero", "near", "same-node"]
    assert [g["sort_order"] for g in groups] == [0, 1, 2]  # cover first, then gallery ordinal
    by_asset = {m["source_asset_id"]: m for m in members}
    assert by_asset["late-dup"]["group_id"] == by_asset["hero"]["group_id"]
    assert by_asset["hero"]["member_role"] == "representative"
    assert by_asset["late-dup"]["member_role"] == "exact_member"
    assert all(g["grouping_basis"] == "exact_normalized_pixel_sha256" for g in groups)
    # contract v1.3 D2: live-Neon form, path verbatim (not percent-encoded), original query dropped
    assert groups[0]["serving_url"] == "https://images.architizer.com/media/hero_\u00e9.jpg?width=1680&quality=60"
    assert groups[0]["url_policy"] == full.URL_POLICY_ARCHITIZER


def test_divisare_url_prefers_gallery_occurrence_verbatim() -> None:
    asset = _gallery_asset("d1", "4" * 64, "n4")
    cover_url = "https://images.divisare.com//image/upload/c_fit,f_jpg,q_80,w_1200/v1/project_images/9/d1.jpg"
    gallery_url = "https://images.divisare.com//images/f_auto,q_auto,w_auto/v1/project_images/9/d1/slug.jpg"
    occurrences = [_occ("10:cover:0", "d1", "cover", 0, url=cover_url), _occ("10:gallery:2", "d1", "gallery", 2, url=gallery_url)]
    url, policy, _, occ_id = full.serving_url("divisare", asset, occurrences)
    assert (url, policy, occ_id) == (gallery_url, full.URL_POLICY_DIVISARE, "10:gallery:2")
    url, _, _, _ = full.serving_url("divisare", asset, occurrences[:1])
    assert url == cover_url


def test_direct_edges_are_deferred_not_attached() -> None:
    assets = [_gallery_asset("a", "1" * 64, "n1", ordinal=0), _gallery_asset("b", "2" * 64, "n2", ordinal=1)]
    occurrences = [_occ("o1", "a", "gallery", 0), _occ("o2", "b", "gallery", 1)]
    groups, members = full.build_groups("architizer", "b1", assets, occurrences)
    edges = full.deferred_edges(groups, [{
        "edge_id": "e1", "left_node_id": "n1", "right_node_id": "n2", "hamming_distance": 3,
        "edge_scope": "global_le8", "edge_record_sha256": "e" * 64,
    }])
    assert len(groups) == 2 and len({m["group_id"] for m in members}) == 2
    assert len(edges) == 1
    assert edges[0]["attachment_status"] == "deferred_no_per_asset_verification"
    assert edges[0]["transitive_closure_allowed"] == 0


def test_disposition_rules() -> None:
    ok = {"fingerprint_status": "success"}
    cand = {"low_information": 0}
    assert full.disposition_for({"fingerprint_status": "failed"}, None, True) == ("exclusion", "fingerprint_or_decoder_failure")
    assert full.disposition_for({"fingerprint_status": "excluded"}, None, True) == ("exclusion", "e1_ledger_excluded")
    assert full.disposition_for(ok, None, True) == ("unresolved", "successful_e2_asset_missing_e3_candidate")
    assert full.disposition_for(ok, {"low_information": 1}, True) == ("exclusion", "low_information")
    assert full.disposition_for(ok, cand, False) == ("unresolved", "source_continuity_not_attested")
    assert full.disposition_for(ok, cand, True) == ("gallery", "eligible_success_continuity_attested")


def test_continuity_rule_v1_still_reproducible() -> None:
    base = {"status": 200, "e1_wh": [1024, 683], "wh": [1024, 681], "hamming": 32}
    assert cont.classify(base, "v1") == "continuous"
    assert cont.classify({**base, "wh": [1024, 680]}, "v1") == "content_changed"
    assert cont.classify({**base, "hamming": 33}, "v1") == "content_changed"
    assert cont.classify({**base, "status": 410}, "v1") == "unavailable"
    assert cont.classify({**base, "status": 503}, "v1") == "fetch_error"
    assert cont.classify({**base, "wh": None, "hamming": None}, "v1") == "decode_failed"
    assert cont.RULES["v1"]["seed"] == "vnext-asset-continuity-v1"
    assert cont.RULES["v1"]["summary"].name == "vnext_asset_continuity_sample_v1_summary.json"
    # the r1 false positive: same picture, panorama 1024x263 -> 1021x263
    pano = {"status": 200, "e1_wh": [1024, 263], "wh": [1021, 263], "hamming": 4}
    assert cont.classify(pano, "v1") == "content_changed" and cont.classify(pano, "v2") == "continuous"


def test_continuity_classification_v2_and_verdict() -> None:
    assert cont.DEFAULT_RULE == "v2" and cont.SAMPLE_SEED == "vnext-asset-continuity-v2"
    base = {"status": 200, "e1_wh": [489, 643], "wh": [779, 1024], "hamming": 32}  # re-render, same aspect
    assert cont.classify(base) == "continuous"
    assert cont.classify({**base, "hamming": 33}) == "content_changed"
    assert cont.classify({**base, "e1_wh": [1000, 1000], "wh": [1019, 1000]}) == "continuous"  # 1.9%
    assert cont.classify({**base, "e1_wh": [1000, 1000], "wh": [1021, 1000]}) == "content_changed"
    assert cont.classify({**base, "e1_wh": [1000, 1000], "wh": [1000, 1021]}) == "content_changed"
    assert cont.classify({**base, "status": 410}) == "unavailable"
    assert cont.classify({**base, "status": 503}) == "fetch_error"
    assert cont.classify({**base, "wh": None, "hamming": None}) == "decode_failed"
    with pytest.raises(ValueError):
        cont.classify(base, "v9")
    for row in (base, {**base, "hamming": 40}, {**base, "status": 404}, {**base, "e1_wh": [1000, 1000], "wh": [1021, 1000]},
                {**base, "e1_wh": [1000, 1000], "wh": [1019, 1000]}, {**base, "wh": None, "hamming": None}):
        assert validator.classify_continuity(row) == cont.classify(row)
    assert validator.CONT_RULE_VERSION == cont.RULES["v2"]["rule_id"] == full.CONTINUITY_RULE_VERSION
    assert validator.CONT_SAMPLE_SEED == cont.RULES["v2"]["seed"] == full.CONTINUITY_SAMPLE_SEED
    rows = [{"class": "continuous", "hamming": 2, "wh": [1, 1], "e1_wh": [1, 1], "status": 200}] * 990
    rows += [{"class": "unavailable", "status": 404}] * 10
    assert cont.source_verdict(rows)["continuity_attested"] is True
    changed = rows[:-1] + [{"class": "content_changed", "hamming": 40, "wh": [1, 1], "e1_wh": [1, 1], "status": 200}]
    assert cont.source_verdict(changed)["continuity_attested"] is False
    assert cont.request_url("architizer", "https://architizer-prod.imgix.net/media/a b.jpg?w=1") == (
        "https://images.architizer.com/media/a%20b.jpg?width=1024&height=1024&fit=max&quality=85&format=jpg")


def test_schema_and_application_id_distinct_from_wp1() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(full.SCHEMA_SQL)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert validator.REQUIRED_TABLES <= tables
    assert full.APPLICATION_ID == validator.APPLICATION_ID
    assert full.APPLICATION_ID not in (0x564E5831, 0x564E5832)


def test_validator_does_not_import_builder() -> None:
    text = Path(validator.__file__).read_text(encoding="utf-8")
    for module in ("vnext_gallery_full", "vnext_gallery_draft", "vnext_identity_registry"):
        assert f"from canonical.{module} import" not in text
        assert f"import canonical.{module}" not in text
    # Runtime proof: importing ONLY the validator in a fresh interpreter loads none of them.
    root = Path(validator.__file__).resolve().parents[1]
    code = (
        "import sys; import canonical.vnext_gallery_full_validator; "
        "bad=[m for m in ('canonical.vnext_gallery_full','canonical.vnext_gallery_draft','canonical.vnext_identity_registry',"
        "'canonical.cross_source_image_selection') if m in sys.modules]; print(','.join(bad))"
    )
    done = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); " % str(root) + code],
                          capture_output=True, text=True, timeout=60, cwd=str(root))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""


def test_divisare_serving_url_must_be_browser_raster() -> None:
    base = "https://images.divisare.com//images/f_auto,q_auto,w_auto/v1/project_images/9/"
    assert full.is_browser_raster_url(base + "x.jpg") and full.is_browser_raster_url(base + "x.PNG")
    for ext in ("pdf", "ai", "eps", "psd", "tiff", "jp2", "octet-stream"):
        assert not full.is_browser_raster_url(base + "x." + ext)
        assert not validator.browser_raster(base + "x." + ext)
    cover = "https://images.divisare.com//image/upload/c_fit,f_jpg,q_80,w_1200/v1/project_images/9/x.pdf"
    assert full.is_browser_raster_url(cover) and validator.browser_raster(cover)
    asset = _gallery_asset("d9", "9" * 64, "n9")
    pdf_gallery = _occ("10:gallery:0", "d9", "gallery", 0, url=base + "d9.pdf")
    pdf_cover = _occ("10:cover:0", "d9", "cover", 0, url=cover.replace("x.pdf", "d9.pdf"))
    # gallery-first, but only among browser-raster occurrences -> falls back to the f_jpg cover
    url, _, provenance, occ_id = full.serving_url("divisare", asset, [pdf_gallery, pdf_cover])
    assert (url, provenance, occ_id) == (pdf_cover["occurrence_url"], "e2_occurrence_url_verbatim:cover", "10:cover:0")
    assert not full.has_serving_url("divisare", asset, [pdf_gallery])
    ok, cand = {"fingerprint_status": "success"}, {"low_information": 0}
    assert full.disposition_for(ok, cand, True, False) == ("unresolved", "no_browser_renderable_serving_url")
    assert validator.expected_disposition(ok, cand, True, False) == full.disposition_for(ok, cand, True, False)


def test_architizer_serving_url_equals_live_neon_form() -> None:
    from tools import architizer_image_host_migrate as neon_tool

    assert full.NEON_ARCHITIZER_PATTERN == neon_tool.PATTERN == validator.NEON_ARCHITIZER_PATTERN
    assert full.NEON_ARCHITIZER_REPLACEMENT == neon_tool.REPLACEMENT == validator.NEON_ARCHITIZER_REPLACEMENT
    ascii_url = "https://architizer-prod.imgix.net/media/mediadata/uploads/1/a_b.jpg?w=1680&q=60"
    assert full.serving_url("architizer", {"canonical_url": ascii_url, "source_asset_id": "x"}, [])[0] == full.neon_architizer_form(ascii_url)
    cyr = "https://architizer-prod.imgix.net/media/1756145560964\u0412\u0438\u0434_5.jpg"
    s2 = full.serving_url("architizer", {"canonical_url": cyr, "source_asset_id": "x"}, [])[0]
    assert s2 == full.neon_architizer_form(cyr) == neon_tool_rewrite(cyr)
    assert s2 == "https://images.architizer.com/media/1756145560964\u0412\u0438\u0434_5.jpg?width=1680&quality=60"
    assert validator.expected_url("architizer", {"canonical_url": cyr, "source_asset_id": "x"}, [])[0] == s2
    # not a fullmatch of the Neon pattern (whitespace in path, other host) -> not servable, fail-closed
    for bad in ("https://architizer-prod.imgix.net/media/a b.jpg", "https://images.architizer.com/media/a.jpg"):
        assert not full.has_serving_url("architizer", {"canonical_url": bad, "source_asset_id": "x"}, [])
        assert validator.expected_url("architizer", {"canonical_url": bad, "source_asset_id": "x"}, []) is None
        with pytest.raises(full.GalleryFullError):
            full.serving_url("architizer", {"canonical_url": bad, "source_asset_id": "x"}, [])


def neon_tool_rewrite(url: str) -> str:
    import re

    from tools import architizer_image_host_migrate as neon_tool

    return re.sub(neon_tool.PATTERN, neon_tool.REPLACEMENT, url)


# --------------------------------------------------------------------------- end-to-end synthetic

E2_SQL = """
CREATE TABLE e2_runs(run_id TEXT, status TEXT);
CREATE TABLE assets(run_id TEXT, source TEXT, source_asset_id TEXT, fingerprint_status TEXT, canonical_url TEXT,
  fetch_url TEXT, normalized_pixel_sha256 TEXT, phash_hex TEXT, original_width INTEGER, original_height INTEGER,
  source_record_sha256 TEXT, provenance_json TEXT, error_kind TEXT, error_message TEXT);
CREATE TABLE building_assets(run_id TEXT, source TEXT, source_building_id TEXT, source_asset_id TEXT,
  project_count INTEGER, occurrence_count INTEGER, roles_json TEXT, relation_record_sha256 TEXT);
CREATE TABLE exact_pixel_cluster_members(run_id TEXT, cluster_id TEXT, source TEXT, source_asset_id TEXT);
CREATE TABLE phash_node_members(run_id TEXT, node_id TEXT, source TEXT, source_asset_id TEXT);
CREATE TABLE source_project_buildings(run_id TEXT, source TEXT, source_project_id TEXT, source_building_id TEXT,
  membership_ordinal INTEGER, source_record_sha256 TEXT);
CREATE TABLE project_asset_occurrences(run_id TEXT, source TEXT, occurrence_id TEXT, source_project_id TEXT,
  raw_asset_key TEXT, source_asset_id TEXT, resolution_status TEXT, role TEXT, ordinal INTEGER, occurrence_url TEXT,
  source_record_sha256 TEXT, detail_json TEXT);
CREATE TABLE phash_edges(run_id TEXT, edge_id TEXT, left_node_id TEXT, right_node_id TEXT, hamming_distance INTEGER,
  edge_scope TEXT, edge_record_sha256 TEXT, PRIMARY KEY(run_id, edge_id),
  UNIQUE(run_id, left_node_id, right_node_id, edge_scope));
"""
E3_SQL = """
CREATE TABLE selection_runs(run_id TEXT, status TEXT);
CREATE TABLE selected_buildings(run_id TEXT, selection_id TEXT, source TEXT, entity_type TEXT, source_entity_id TEXT,
  source_building_id TEXT, name TEXT, selection_record_sha256 TEXT, detail_json TEXT);
CREATE TABLE image_candidates(run_id TEXT, candidate_id TEXT, selection_id TEXT, source TEXT, source_asset_id TEXT,
  primary_role TEXT, role_rank INTEGER, source_ordinal INTEGER, quality_flags_json TEXT, low_information INTEGER,
  candidate_record_sha256 TEXT, normalized_pixel_sha256 TEXT, phash_node_id TEXT);
"""
E2_RUN, E3_RUN = "e2-test", "e3-test"


def _make_fixture(root: Path, *, attested: bool = True, seed: str = cont.SAMPLE_SEED,
                  rule_version: str = cont.RULES["v2"]["rule_id"]) -> None:
    (root / "data/enrichment").mkdir(parents=True)
    (root / "data/reports").mkdir(parents=True)
    (root / "docs/vnext").mkdir(parents=True)
    e2 = sqlite3.connect(root / full.DEFAULT_E2)
    e3 = sqlite3.connect(root / full.DEFAULT_E3)
    e2.executescript(E2_SQL)
    e3.executescript(E3_SQL)
    e2.execute("INSERT INTO e2_runs VALUES(?,?)", (E2_RUN, "complete"))
    e3.execute("INSERT INTO selection_runs VALUES(?,?)", (E3_RUN, "complete"))
    # (source, building, project, asset, status, pixel, node, w, h, roles/ordinals, candidate?, low_info)
    spec = [
        ("architizer", "atz_b1", "p1", "a1", "success", "1" * 64, "n1", 1200, 900, [("cover", 0), ("gallery", 0)], True, 0),
        ("architizer", "atz_b1", "p1", "a2", "success", "1" * 64, "n1", 800, 600, [("gallery", 1)], True, 0),
        ("architizer", "atz_b1", "p1", "a3", "success", "2" * 64, "n2", 1000, 800, [("gallery", 2)], True, 0),
        ("architizer", "atz_b1", "p1", "a4", "success", "3" * 64, "n3", 1000, 800, [("gallery", 3)], True, 0),
        ("architizer", "atz_b1", "p1", "a5", "failed", None, None, None, None, [("gallery", 4)], False, 0),
        ("architizer", "atz_b1", "p1", "a6", "success", "4" * 64, "n4", 900, 700, [("gallery", 5)], False, 0),
        ("architizer", "atz_b1", "p1", "a7", "success", "5" * 64, "n5", 900, 700, [("gallery", 6)], True, 1),
        ("divisare", "div_b1", "10", "d1", "success", "6" * 64, "n6", 980, 600, [("cover", 0), ("gallery", 0)], True, 0),
        ("divisare", "div_b1", "10", "d2", "success", "7" * 64, "n7", 980, 600, [("gallery", 1)], True, 0),
        # .pdf under f_auto with no raster occurrence -> unresolved no_browser_renderable_serving_url
        ("divisare", "div_b1", "10", "d3", "success", "8" * 64, "n8", 980, 600, [("gallery", 2)], True, 0),
    ]
    buildings = {("architizer", "atz_b1", "p1"), ("divisare", "div_b1", "10"), ("divisare", "div_b0", "11")}
    for source, bid, project in sorted(buildings):
        selection = f"{source}:building:{bid}"
        count = sum(1 for row in spec if row[1] == bid and row[4] == "success")
        e3.execute("INSERT INTO selected_buildings VALUES(?,?,?,?,?,?,?,?,?)", (
            E3_RUN, selection, source, "building", bid, bid, "Name " + bid, _h("sel" + bid),
            json.dumps({"building_summary": {"successful_asset_count": count}})))
        e2.execute("INSERT INTO source_project_buildings VALUES(?,?,?,?,?,?)", (E2_RUN, source, project, bid, 0, _h("spb" + bid)))
    for source, bid, project, aid, status, pixel, node, w, h, occs, has_cand, low in spec:
        path = f"/media/{aid}.jpg"
        canonical = ("http://architizer-prod.imgix.net" + path) if source == "architizer" else (
            f"https://images.divisare.com//images/f_auto,q_auto,w_auto/v1/project_images/{aid}/slug.{'pdf' if aid == 'd3' else 'jpg'}")
        e2.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            E2_RUN, source, aid, status, canonical, canonical, pixel, pixel and _h("ph" + pixel), w, h, _h("asset" + aid),
            "{}", None if status == "success" else "http_404", None if status == "success" else "404"))
        e2.execute("INSERT INTO building_assets VALUES(?,?,?,?,?,?,?,?)", (E2_RUN, source, bid, aid, 1, len(occs), "[]", _h("rel" + aid)))
        if pixel:
            e2.execute("INSERT INTO exact_pixel_cluster_members VALUES(?,?,?,?)", (E2_RUN, "cl" + pixel[:3], source, aid))
            e2.execute("INSERT INTO phash_node_members VALUES(?,?,?,?)", (E2_RUN, node, source, aid))
        for role, ordinal in occs:
            occ_id = f"{project}:{role}:{ordinal}"
            url = canonical if source == "architizer" else (
                f"https://images.divisare.com//image/upload/c_fit,f_jpg,q_80,w_1200/v1/project_images/{aid}.jpg" if role == "cover" else canonical)
            e2.execute("INSERT INTO project_asset_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                E2_RUN, source, occ_id, project, aid, aid, "linked", role, ordinal, url, _h("occ" + occ_id), "{}"))
        if has_cand:
            e3.execute("INSERT INTO image_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                E3_RUN, "cand-" + aid, f"{source}:building:{bid}", source, aid, occs[0][0], 0 if occs[0][0] == "cover" else 1,
                occs[0][1], "[]", low, _h("cand" + aid), pixel, node))
    e2.execute("INSERT INTO phash_edges VALUES(?,?,?,?,?,?,?)", (E2_RUN, "edge-23", "n2", "n3", 4, "global_le8", _h("edge23")))
    e2.execute("INSERT INTO phash_edges VALUES(?,?,?,?,?,?,?)", (E2_RUN, "edge-far", "n2", "n3", 12, "metadata_9_16", _h("edgefar")))
    for connection in (e2, e3):
        connection.commit()
        connection.close()
    (root / "docs/vnext/CONTRACT_GALLERY_SEMANTICS.md").write_text(
        "contract v1.3 population continuity attestation continuity_attested deferred_no_per_asset_verification "
        "vnext-asset-continuity-rule-v2 vnext-asset-continuity-v2\n", encoding="utf-8")
    (root / "data/wp1.db").write_bytes(b"wp1 artifact")
    rows = []
    for source in ("architizer", "divisare"):
        for rank in range(1, 1001):
            row = {"source": source, "sample_rank": rank, "source_asset_id": f"{source}-{rank}", "e1_wh": [100, 100],
                   "status": 404 if rank <= 3 else 200, "wh": None if rank <= 3 else [100, 101], "hamming": None if rank <= 3 else 5}
            if not attested and rank == 4:
                row["wh"] = [100, 140]  # same picture, size change -> content_changed -> not attested
            row["class"] = cont.classify(row)
            rows.append(row)
    jsonl = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    (root / "data/reports/cont.jsonl").write_bytes(jsonl)
    summary = {
        "partial_smoke": False, "status": "PASS", "sample_seed": seed, "rule_version": rule_version, "thresholds": {},
        "rows_jsonl": {"path": "data/reports/cont.jsonl", "sha256": hashlib.sha256(jsonl).hexdigest()},
        "sources": {s: cont.source_verdict([r for r in rows if r["source"] == s]) for s in ("architizer", "divisare")},
    }
    (root / "data/reports/cont_summary.json").write_text(json.dumps(summary), encoding="utf-8")


def _pin(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    e2_sha, e3_sha = _sha_file(root / full.DEFAULT_E2), _sha_file(root / full.DEFAULT_E3)
    e2_size, e3_size = (root / full.DEFAULT_E2).stat().st_size, (root / full.DEFAULT_E3).stat().st_size
    freeze = {"frozen_inputs": [
        {"role": "cross_source_e2_full_v5", "path": full.DEFAULT_E2.as_posix(), "size_bytes": e2_size, "sha256": e2_sha},
        {"role": "cross_source_e3_full_v1", "path": full.DEFAULT_E3.as_posix(), "size_bytes": e3_size, "sha256": e3_sha},
    ]}
    (root / "data/reports/vnext_workspace_freeze_v1.json").write_text(json.dumps(freeze), encoding="utf-8")
    pins = {"data/wp1.db": _sha_file(root / "data/wp1.db")}
    for module in (full, validator):
        monkeypatch.setattr(module, "EXPECTED_E2_SHA256", e2_sha)
        monkeypatch.setattr(module, "EXPECTED_E3_SHA256", e3_sha)
        monkeypatch.setattr(module, "EXPECTED_E2_SIZE", e2_size)
        monkeypatch.setattr(module, "EXPECTED_E3_SIZE", e3_size)
        monkeypatch.setattr(module, "WP1_ARTIFACT_PINS", pins)
        monkeypatch.setattr(module, "FULL_BUILDING_COUNT", 3)


def _build(root: Path, name: str = "out") -> full.FullBuildResult:
    return full.build_gallery_full(
        repo_root=root, output_path=Path(f"data/enrichment/{name}.db"), manifest_path=Path(f"data/reports/{name}_manifest.json"),
        report_path=Path(f"data/reports/{name}_report.json"), freeze_path=Path("data/reports/vnext_workspace_freeze_v1.json"),
        continuity_summary_path=Path("data/reports/cont_summary.json"), verify_full_hashes=True,
    )


def test_end_to_end_build_and_independent_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fixture(tmp_path)
    _pin(monkeypatch, tmp_path)
    result = _build(tmp_path)
    assert result.totals["buildings"] == 3
    assert result.totals["input"] == 10
    assert result.totals["gallery"] == 6 and result.totals["groups"] == 5
    assert result.totals["exclusion"] == 2 and result.totals["unresolved"] == 2
    # both E2 scopes are preserved (global_le8 hamming 4 + metadata_9_16 hamming 12)
    assert result.totals["deferred_edges"] == 2 and result.totals["zero_asset_buildings"] == 1
    for suffix in ("-wal", "-shm", "-journal", ".building"):
        assert not Path(str(result.output_path) + suffix).exists()
    connection = sqlite3.connect(result.output_path)
    rows = connection.execute(
        "SELECT source_building_id,sort_order,representative_source_asset_id,serving_url FROM gallery_groups ORDER BY 1,2").fetchall()
    connection.close()
    assert rows[0][:3] == ("atz_b1", 0, "a1")
    assert rows[0][3] == "https://images.architizer.com/media/a1.jpg?width=1680&quality=60"
    assert [r[2] for r in rows if r[0] == "atz_b1"] == ["a1", "a3", "a4"]
    assert rows[3][3].startswith("https://images.divisare.com//images/f_auto,q_auto,w_auto/")
    connection = sqlite3.connect(result.output_path)
    unresolved = dict(connection.execute("SELECT source_asset_id,reason_code FROM unresolved_ledger").fetchall())
    scopes = sorted(r[0] for r in connection.execute("SELECT edge_scope FROM direct_edge_evidence"))
    connection.close()
    assert unresolved == {"a6": "successful_e2_asset_missing_e3_candidate", "d3": "no_browser_renderable_serving_url"}
    assert scopes == ["global_le8", "metadata_9_16"]
    report = validator.validate_gallery_full(result.output_path, repo_root=tmp_path, verify_input_hashes=True,
                                             manifest_path=result.manifest_path)
    assert report.passed, report.failed_check_names
    assert report.logical_sha256 == result.logical_sha256
    assert "manifest_output_sha" in {c.name for c in report.checks}
    with pytest.raises(full.GalleryFullError):
        _build(tmp_path)  # no clobber


def test_end_to_end_not_attested_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fixture(tmp_path, attested=False)
    _pin(monkeypatch, tmp_path)
    result = _build(tmp_path)
    assert result.totals["input"] == 10
    assert result.totals["gallery"] == 0 and result.totals["groups"] == 0 and result.totals["deferred_edges"] == 0
    assert result.totals["exclusion"] == 2 and result.totals["unresolved"] == 8
    connection = sqlite3.connect(result.output_path)
    reasons = dict(connection.execute("SELECT reason_code,count(*) FROM unresolved_ledger GROUP BY 1").fetchall())
    connection.close()
    assert reasons == {"source_continuity_not_attested": 6, "successful_e2_asset_missing_e3_candidate": 1,
                       "no_browser_renderable_serving_url": 1}
    report = validator.validate_gallery_full(result.output_path, repo_root=tmp_path, manifest_path=result.manifest_path)
    assert report.passed, report.failed_check_names
    assert report.metrics["continuity.architizer"]["attested"] is False


def test_builder_rejects_v1_rule_continuity_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fixture(tmp_path, seed=cont.RULES["v1"]["seed"], rule_version=None)
    _pin(monkeypatch, tmp_path)
    with pytest.raises(full.GalleryFullError, match="rule/seed"):
        _build(tmp_path)
    assert not (tmp_path / "data/enrichment/out.db").exists()


def test_validator_missing_artifact_fails(tmp_path: Path) -> None:
    report = validator.validate_gallery_full(tmp_path / "absent.db", repo_root=tmp_path)
    assert report.passed is False
    assert report.failed_check_names == ("artifact_exists",)


def _forge(path: Path, table: str, where: str, changes: dict) -> None:
    """Rewrite non-key columns AND recompute record_sha256 + runs.logical_sha256 consistently."""

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    run_id = connection.execute("SELECT run_id FROM runs").fetchone()[0]
    row = dict(connection.execute(f"SELECT * FROM {table} WHERE {where}").fetchone())
    row.update(changes)
    row["record_sha256"] = validator._row_sha(row)
    sets = ",".join(f"{key}=?" for key in [*changes, "record_sha256"])
    connection.execute(f"UPDATE {table} SET {sets} WHERE {where}", [*changes.values(), row["record_sha256"]])
    connection.commit()
    connection.execute("UPDATE runs SET logical_sha256=?", (validator._logical(connection, run_id),))
    connection.commit()
    connection.close()


@pytest.mark.parametrize("table,where,changes", [
    ("source_occurrences", "occurrence_id='10:gallery:1'", {"resolution_status": "FORGED", "detail_json": '{"f":1}'}),
    ("exclusion_ledger", "source_asset_id='a5'", {"error_kind": "FORGED", "retryable": 1, "pixel_verification_status": "FORGED"}),
    ("unresolved_ledger", "source_asset_id='a6'", {"decision_engine": "FORGED"}),
    ("buildings", "source_building_id='atz_b1'", {"name": "FORGED", "e3_successful_asset_count": 999}),
    ("gallery_groups", "representative_source_asset_id='a3'", {"representative_width": 1, "url_provenance": "FORGED"}),
    ("group_members", "source_asset_id='a2'", {"phash_node_id": "FORGED", "original_height": 1}),
    ("direct_edge_evidence", "edge_id='edge-23'", {"hamming_distance": 1, "edge_record_sha256": "f" * 64}),
])
def test_validator_rejects_consistently_forged_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str, where: str, changes: dict) -> None:
    _make_fixture(tmp_path)
    _pin(monkeypatch, tmp_path)
    result = _build(tmp_path)
    forged = tmp_path / "data/enrichment/forged.db"
    shutil.copyfile(result.output_path, forged)
    _forge(forged, table, where, changes)
    report = validator.validate_gallery_full(forged, repo_root=tmp_path)
    assert not report.passed
    assert "per_building_replay" in report.failed_check_names
    assert "logical_sha" not in report.failed_check_names  # SHAs are internally consistent; only the replay catches it


def test_validator_checks_manifest_and_run_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fixture(tmp_path)
    _pin(monkeypatch, tmp_path)
    result = _build(tmp_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["row_counts"]["gallery_groups"] += 1
    bad_manifest = tmp_path / "data/reports/bad_manifest.json"
    bad_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    report = validator.validate_gallery_full(result.output_path, repo_root=tmp_path, manifest_path=bad_manifest)
    assert report.failed_check_names == ("manifest_row_counts",)
    connection = sqlite3.connect(result.output_path)
    connection.execute("UPDATE runs SET config_sha256=?", ("0" * 64,))
    connection.commit()
    connection.close()
    report = validator.validate_gallery_full(result.output_path, repo_root=tmp_path)
    assert {"config_sha", "run_id_recomputed"} <= set(report.failed_check_names)


def test_validator_detects_tampering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fixture(tmp_path)
    _pin(monkeypatch, tmp_path)
    result = _build(tmp_path)
    connection = sqlite3.connect(result.output_path)
    connection.execute("UPDATE gallery_groups SET sort_order=sort_order+10 WHERE source_building_id='div_b1'")
    connection.commit()
    connection.close()
    report = validator.validate_gallery_full(result.output_path, repo_root=tmp_path)
    assert not report.passed
    assert "per_building_replay" in report.failed_check_names
    connection = sqlite3.connect(result.output_path)
    connection.execute("UPDATE gallery_groups SET sort_order=sort_order-10 WHERE source_building_id='div_b1'")
    connection.execute("UPDATE group_members SET record_sha256=? WHERE source_asset_id='d2'", ("0" * 64,))
    connection.commit()
    connection.close()
    report = validator.validate_gallery_full(result.output_path, repo_root=tmp_path)
    assert {"per_building_replay", "logical_sha"} <= set(report.failed_check_names)


def test_ladder_scope_is_deterministic_subset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fixture(tmp_path)
    _pin(monkeypatch, tmp_path)
    result = full.build_gallery_full(
        repo_root=tmp_path, output_path=Path("data/smoke/n2.db"), manifest_path=Path("data/smoke/n2_m.json"),
        report_path=Path("data/smoke/n2_r.json"), freeze_path=Path("data/reports/vnext_workspace_freeze_v1.json"),
        continuity_summary_path=Path("data/reports/cont_summary.json"), limit_buildings=2,
    )
    assert result.scope == "ladder_n2" and result.building_count == 2
    report = validator.validate_gallery_full(result.output_path, repo_root=tmp_path)
    assert report.passed, report.failed_check_names
