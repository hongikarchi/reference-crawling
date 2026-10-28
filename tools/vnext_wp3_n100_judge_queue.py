#!/usr/bin/env python3
"""vNext WP3 N100: Claude judge queue, exactly per preregistration.json `judge_protocol` ($0, offline).

Inputs (read-only): frozen local_pred.jsonl / local_covers.jsonl / fetch_log.jsonl (SHA-checked against
freeze_manifest.json), codex/labels.jsonl + codex/cover.jsonl (holdout codex gold source).

Image set (labels):
- `image`: every image where probe != codex on any of the 9 core fields after the v1.4 mapping
  (legibility medium->high on both sides; camera `unknown` vs eye_level/elevated counts as agreement,
  either direction). Images whose codex label failed are queued with reason `codex_label_failed`.
- `spot`: 60 images with probe == codex on all 9 fields, ordered by
  sha256('vnext-wp3-n100-spot-v1|'+str(idx)) ascending.
Per disagreeing field the two candidate values are shown as A/B, order fixed by
sha256('vnext-wp3-n100-judge-order-v1|'+str(idx)+'|'+field): int(hex,16) even -> A = probe, odd -> A = codex.
Engine identity is hidden in the chunk files; the unblinding key is only in judge_queue.json.

Building set (covers), any of:
- `confident_pick_not_in_codex_acceptable`: local-confident default not in codex acceptable_idx
- `codex_best_not_in_local_top3`: codex ranked[0] exists and is not in the local top-3
- `final_pick_critical`: final funnel pick (confident -> local default, else codex ranked[0]; empty -> NULL)
  has a critical codex image label (COVERS §3 via tools/vnext_wp3_eval.critical_cover_reasons)
- `codex_cover_failed` / `final_pick_codex_label_missing` (explicit, should be 0)
The cover judge sees all ok images of the building in an opaque order
sha256('vnext-wp3-n100-cover-opaque-v1|'+bld+'|'+str(idx)) ascending, and writes the acceptable set.

Outputs: judge_queue.json, judge_chunks/chunk_{1,2,3}.json (image+spot, contiguous by idx),
judge_chunks/covers.json. No network, no model calls, no Neon/R2.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_wp3_eval as ev  # noqa: E402  (unchanged v1 helpers)

WP = ROOT / "data" / "smoke_vnext" / "wp3_n100_v1"
PREREG = WP / "preregistration.json"
PREREG_SHA = "59eee674e96aa1582fd4a68950cc645b247c2e023a8b00df17775cfbc49b375b"
FREEZE = WP / "freeze_manifest.json"
CODEX = WP / "codex"
OUT_Q = WP / "judge_queue.json"
OUT_C = WP / "judge_chunks"
FIELDS = ["in_scope", "reject_reason", "medium", "spatial_context", "framing_scale",
          "camera_angle", "drawing_kind", "project_state", "project_legibility"]
SPOT_N = 60
SPOT_SEED = "vnext-wp3-n100-spot-v1"
ORDER_SEED = "vnext-wp3-n100-judge-order-v1"
COVER_SEED = "vnext-wp3-n100-cover-opaque-v1"
N_CHUNKS = 3


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def hx(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def rj(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def wj(p: Path, obj) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    return sha(p)


def m14(field: str, v):
    return "high" if field == "project_legibility" and v == "medium" else v


def agree(field: str, a, b) -> bool:
    a, b = m14(field, a), m14(field, b)
    if a == b:
        return True
    if field == "camera_angle" and {a, b} in ({"unknown", "eye_level"}, {"unknown", "elevated"}):
        return True
    return False


def abspath(rel: str) -> str:
    return (ROOT / rel).as_posix()


def main() -> int:
    if sha(PREREG) != PREREG_SHA:
        raise SystemExit("preregistration SHA drift")
    fz = json.loads(FREEZE.read_text(encoding="utf-8"))
    for rel, s in fz["files"].items():
        if sha(ROOT / rel) != s:
            raise SystemExit(f"frozen file drift: {rel}")
    probe = {r["idx"]: r for r in rj(WP / "local_pred.jsonl")}
    lcov = {r["canonical_bld_id"]: r for r in rj(WP / "local_covers.jsonl")}
    fetch = {r["idx"]: r for r in rj(WP / "fetch_log.jsonl") if r.get("status") == "ok"}
    cx = {r["idx"]: r for r in rj(CODEX / "labels.jsonl")}
    ccov = {}
    for r in rj(CODEX / "cover.jsonl"):
        if r.get("complete"):
            ccov[r["canonical_bld_id"]] = r

    # ---------------- image + spot
    images, agree_idx, st = [], [], Counter()
    for idx in sorted(fetch):
        p, c = probe[idx], cx.get(idx)
        base = {"idx": idx, "canonical_bld_id": p["canonical_bld_id"], "stratum": p["stratum"],
                "image_path": abspath(fetch[idx]["image_path"]), "probe_labels": p["labels"]}
        if c is None or c.get("status") != "ok":
            st["codex_label_failed"] += 1
            images.append({**base, "kind": "image", "reason": "codex_label_failed",
                           "codex_labels": None, "differing_fields": list(FIELDS)})
            continue
        diff = [f for f in FIELDS if not agree(f, p["labels"][f], c["labels"][f])]
        if diff:
            images.append({**base, "kind": "image", "reason": "probe_codex_disagree",
                           "codex_labels": c["labels"], "differing_fields": diff})
            for f in diff:
                st[f"diff:{f}"] += 1
        else:
            agree_idx.append(idx)
    spot_idx = sorted(agree_idx, key=lambda i: hx(f"{SPOT_SEED}|{i}"))[:SPOT_N]
    spots = []
    for idx in spot_idx:
        p = probe[idx]
        spots.append({"idx": idx, "canonical_bld_id": p["canonical_bld_id"], "stratum": p["stratum"],
                      "image_path": abspath(fetch[idx]["image_path"]), "kind": "spot",
                      "reason": "agreement_spot_check", "probe_labels": p["labels"],
                      "codex_labels": cx[idx]["labels"], "differing_fields": []})
    # blinding key
    for e in images:
        e["ab_key"] = {}
        for f in e["differing_fields"]:
            even = int(hx(f"{ORDER_SEED}|{e['idx']}|{f}"), 16) % 2 == 0
            e["ab_key"][f] = {"A": "probe", "B": "codex"} if even else {"A": "codex", "B": "probe"}

    # ---------------- covers
    covers, cst = [], Counter()
    by_bld = {}
    for idx in sorted(fetch):
        by_bld.setdefault(probe[idx]["canonical_bld_id"], []).append(idx)
    for bld in sorted(lcov):
        lc, cc = lcov[bld], ccov.get(bld)
        reasons = []
        conf = bool(lc["local_confident"])
        top3 = list(lc.get("top3_idx") or [])
        if cc is None or cc.get("status") != "ok":
            reasons.append("codex_cover_failed")
            acc, ranked = None, None
        else:
            acc, ranked = list(cc["acceptable_idx"]), list(cc["ranked_idx"])
        if conf and acc is not None and lc["default_idx"] not in acc:
            reasons.append("confident_pick_not_in_codex_acceptable")
        if ranked and ranked[0] not in top3:
            reasons.append("codex_best_not_in_local_top3")
        if conf:
            final, final_src = lc["default_idx"], "local_confident"
        elif ranked is not None:
            final, final_src = (ranked[0] if ranked else None), "codex_cover"
        else:
            final, final_src = None, "codex_cover_failed"
        crit = []
        if final is not None:
            cl = cx.get(final)
            if cl is None or cl.get("status") != "ok":
                reasons.append("final_pick_codex_label_missing")
            else:
                crit = ev.critical_cover_reasons(cl["labels"])
                if crit:
                    reasons.append("final_pick_critical")
        for r in reasons:
            cst[r] += 1
        if not reasons:
            continue
        opaque = sorted(by_bld[bld], key=lambda i: hx(f"{COVER_SEED}|{bld}|{i}"))
        covers.append({"kind": "cover", "canonical_bld_id": bld, "stratum": lc["stratum"], "reasons": reasons,
                       "n_images_ok": len(opaque),
                       "images": [{"idx": i, "image_path": abspath(fetch[i]["image_path"])} for i in opaque],
                       "local": {"confident": conf, "default_idx": lc["default_idx"], "top3_idx": top3,
                                 "confidence_top1": lc["confidence_top1"], "confidence_margin": lc["confidence_margin"]},
                       "codex": {"ranked_idx": ranked, "acceptable_idx": acc},
                       "final_funnel_pick": {"idx": final, "source": final_src, "critical_codex_label": crit}})

    # ---------------- chunks (blinded)
    lab_entries = sorted(images + spots, key=lambda e: e["idx"])

    def blind(e):
        out = {"kind": e["kind"], "idx": e["idx"], "canonical_bld_id": e["canonical_bld_id"],
               "image_path": e["image_path"]}
        if e["kind"] == "spot":
            out["labels"] = {f: e["codex_labels"][f] for f in FIELDS}
            out["task"] = "confirm or correct each of the 9 fields"
            return out
        p, c = e["probe_labels"], e["codex_labels"]
        ctx = {}
        for f in FIELDS:
            if f in e["differing_fields"]:
                continue
            a, b = m14(f, p[f]), m14(f, c[f])
            ctx[f] = a if a == b else sorted({a, b})
        out["agreed_fields"] = ctx
        out["differing_fields"] = []
        for f in e["differing_fields"]:
            k = e["ab_key"][f]
            val = {"probe": p[f], "codex": c[f] if c else None}
            out["differing_fields"].append({"field": f, "A": val[k["A"]], "B": val[k["B"]]})
        out["task"] = "per differing field: A | B | both | neither (+ judge value)"
        return out

    n = len(lab_entries)
    bounds = [round(i * n / N_CHUNKS) for i in range(N_CHUNKS + 1)]
    chunk_files = {}
    for i in range(N_CHUNKS):
        part = lab_entries[bounds[i]:bounds[i + 1]]
        p = OUT_C / f"chunk_{i + 1}.json"
        doc = {"version": "vnext-wp3-n100-judge-chunk-v1", "chunk": i + 1, "of": N_CHUNKS,
               "protocol": "preregistration.json judge_protocol; values blinded A/B (engine identity hidden); "
                           "v1.4 mapping applied for agreement (camera unknown ~ eye_level/elevated; legibility medium->high)",
               "vocab_ref": "docs/vnext/CONTRACT_GALLERY_SEMANTICS.md §4 (v1.4)",
               "n_entries": len(part), "n_image": sum(1 for e in part if e["kind"] == "image"),
               "n_spot": sum(1 for e in part if e["kind"] == "spot"),
               "n_field_decisions": sum(len(e["differing_fields"]) for e in part if e["kind"] == "image"),
               "entries": [blind(e) for e in part]}
        chunk_files[str(p.relative_to(ROOT)).replace("\\", "/")] = wj(p, doc)
    cp = OUT_C / "covers.json"
    cdoc = {"version": "vnext-wp3-n100-judge-covers-v1",
            "protocol": "judge sees all ok images (opaque order) and writes the acceptable default set "
                        "(CONTRACT_COVERS §1/§3; empty = NULL is correct); candidates hidden",
            "n_entries": len(covers),
            "entries": [{"kind": "cover", "canonical_bld_id": c["canonical_bld_id"], "n_images_ok": c["n_images_ok"],
                         "images": c["images"]} for c in covers]}
    chunk_files[str(cp.relative_to(ROOT)).replace("\\", "/")] = wj(cp, cdoc)

    q = {"version": "vnext-wp3-n100-judge-queue-v1", "tool": "tools/vnext_wp3_n100_judge_queue.py",
         "tool_sha256": sha(Path(__file__)), "preregistration_sha256": PREREG_SHA,
         "inputs_sha256": {**fz["files"], "data/smoke_vnext/wp3_n100_v1/codex/labels.jsonl": sha(CODEX / "labels.jsonl"),
                           "data/smoke_vnext/wp3_n100_v1/codex/cover.jsonl": sha(CODEX / "cover.jsonl")},
         "rules": {"agreement": "v1.4: legibility medium->high both sides; camera unknown ~ eye_level/elevated (symmetric)",
                   "spot": f"sha256('{SPOT_SEED}|'+str(idx)) asc, first {SPOT_N} of all-9-field agreements",
                   "ab_order": f"sha256('{ORDER_SEED}|'+str(idx)+'|'+field) int even -> A=probe else A=codex",
                   "cover_opaque_order": f"sha256('{COVER_SEED}|'+bld+'|'+str(idx)) asc",
                   "final_funnel_pick": "local_confident -> local default; else codex ranked[0]; empty -> NULL"},
         "summary": {"images_ok": len(fetch), "codex_labels_ok": sum(1 for r in cx.values() if r.get("status") == "ok"),
                     "image_entries": len(images), "spot_entries": len(spots), "agreement_pool": len(agree_idx),
                     "cover_entries": len(covers), "codex_covers_ok": sum(1 for r in ccov.values() if r.get("status") == "ok"),
                     "field_decisions": sum(len(e["differing_fields"]) for e in images),
                     "image_counts": dict(st), "cover_reason_counts": dict(cst),
                     "per_stratum_image_entries": dict(Counter(e["stratum"] for e in images)),
                     "per_stratum_cover_entries": dict(Counter(c["stratum"] for c in covers))},
         "chunk_files_sha256": chunk_files,
         "entries": {"image": images, "spot": spots, "cover": covers}}
    wj(OUT_Q, q)
    print(json.dumps({"summary": q["summary"], "chunks": {k: None for k in chunk_files},
                      "chunk_sizes": [bounds[i + 1] - bounds[i] for i in range(N_CHUNKS)]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
