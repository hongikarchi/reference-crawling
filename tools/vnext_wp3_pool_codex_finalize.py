#!/usr/bin/env python3
"""WP3 teach: finalize pool codex labels -> data/smoke_vnext/wp3_pool_v1/codex_labels.jsonl (offline, $0).

Copies every row of `codex_label_run/labels.jsonl` (engine output, unchanged) and adds one explicit
`status: not_run` row per pool image whose fetch failed (no pixels -> never sent), so the file covers
every pool_manifest image. Also prints teacher-vs-prefilter crosstabs per selection_reason.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / "data/smoke_vnext/wp3_pool_v1"


def main() -> int:
    man = json.loads((POOL / "pool_manifest.json").read_text(encoding="utf-8"))
    rows = {json.loads(x)["idx"]: json.loads(x) for x in
            (POOL / "codex_label_run/labels.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}
    out = []
    for im in sorted(man["images"], key=lambda x: x["idx"]):
        r = rows.get(im["idx"])
        if r is None:
            assert im["fetch_status"] != "ok", im["idx"]
            r = {"idx": im["idx"], "canonical_bld_id": im["canonical_bld_id"], "stratum": im["stratum"],
                 "image_asset_id": im["image_asset_id"], "sort_order": im["sort_order"], "status": "not_run",
                 "errors": [f"fetch_failed:{im.get('fetch_error_code')}"], "labels": None}
        r = dict(r, selection_reason=im["selection_reason"])
        out.append(r)
    p = POOL / "codex_labels.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    adv = {im["idx"]: im.get("zs_prefilter_advisory") or {} for im in man["images"]}
    st = Counter(r["status"] for r in out)
    by_reason = defaultdict(Counter)
    for r in out:
        if r["status"] != "ok":
            continue
        L = r["labels"]
        key = "out_of_scope:" + L["reject_reason"] if not L["in_scope"] else L["medium"]
        by_reason[r["selection_reason"]][key] += 1
    agree = Counter()
    for r in out:
        if r["status"] != "ok":
            continue
        a = adv[r["idx"]]
        L = r["labels"]
        agree["scope_" + ("agree" if bool(a.get("in_scope")) == L["in_scope"] else "disagree")] += 1
        if a.get("in_scope") and L["in_scope"]:
            agree["medium_" + ("agree" if a.get("medium_argmax") == L["medium"] else "disagree")] += 1
    print(json.dumps({"rows": len(out), "status": dict(st), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                      "teacher_by_selection_reason": {k: dict(v.most_common()) for k, v in by_reason.items()},
                      "prefilter_vs_teacher": dict(agree)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
