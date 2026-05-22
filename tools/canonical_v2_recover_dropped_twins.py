#!/usr/bin/env python3
"""C10 recovery — attach matcher-missed cross-source twins to canonical rows.

Consumes the matcher recall audit (canonical_v2_matcher_recall_audit.py) and
applies its missed-twin pairs to the C9 canonical artifact:

  one_or_both_dropped : a source building absent from every canonical row is
                        attached (as a source_ref) to its surviving twin row.
  different_canonical : two canonical rows that are the same building merge —
                        survivor = lowest bld_id; the survivor absorbs the
                        losers' source_refs + NULL-only enrichment, and the
                        loser rows are removed from the artifact. Connected
                        merge pairs union-find into one component, so chains
                        resolve to a single survivor.

Removed loser ids are written to the report as `removed_canonical_ids`; the
Neon upsert DELETEs them (DELETE-then-UPSERT, one transaction) so a source_ref
is never duplicated across two rows. Streaming, strict artifact only
(embeddings are regenerated downstream). Read-only w.r.t. Neon.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import _confidence_tier       # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings   # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C9 = CCR / "canonical_buildings_strict.completeness_c9.json"
PAIRS = ROOT / "data/reports/canonical_v2_matcher_recall_pairs.jsonl"
OUT = CCR / "canonical_buildings_strict.completeness_c10_recovery.json"
REPORT = ROOT / "data/reports/canonical_v2_c10_recovery_report.json"
CONFLICTS = ROOT / "data/reports/canonical_v2_c10_recovery_conflicts.jsonl"

# loser -> survivor: copy only where the survivor's value is empty.
_ABSORB_IF_EMPTY = [
    "name", "location_city", "location_country", "project_year",
    "architects_text", "program", "style", "color_tone", "atmosphere",
    "material_visual", "visual_description", "image_derived",
]
# loser -> survivor: union (dedup, order-stable). All string lists.
_ABSORB_UNION = ["names_alts", "architect_canonical_ids", "architect_names"]


def _is_empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _union(a, b) -> list:
    out = list(a or [])
    seen = set(out)
    for x in (b or []):
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


class _UF:
    """Union-find; the component root is always the lexicographically lowest id."""

    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            self.p[hi] = lo


def _build_plan():
    """Return (attach_raw, merge_uf, unrecoverable) from the pairs JSONL."""
    attach_raw: list = []          # (dropped_source, dropped_id, survivor_cid)
    merge_uf = _UF()
    unrecoverable = 0
    for line in PAIRS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        pair = json.loads(line)
        a, b, reason = pair["a"], pair["b"], pair["reason"]
        if reason == "one_or_both_dropped":
            if a["cid"] and not b["cid"]:
                surv, drop = a, b
            elif b["cid"] and not a["cid"]:
                surv, drop = b, a
            else:
                unrecoverable += 1  # both sides dropped — no row to attach to
                continue
            attach_raw.append((drop["source"], str(drop["id"]), surv["cid"]))
        elif a["cid"] and b["cid"]:   # different_canonical_*
            merge_uf.union(a["cid"], b["cid"])
    return attach_raw, merge_uf, unrecoverable


def main() -> int:
    if not C9.exists():
        print(f"FATAL: C9 artifact missing: {C9}", file=sys.stderr)
        return 2
    if not PAIRS.exists():
        print(f"FATAL: recall pairs missing: {PAIRS} — run the audit first",
              file=sys.stderr)
        return 2

    attach_raw, merge_uf, unrecoverable = _build_plan()
    merge_cids = set(merge_uf.p)

    # dropped building -> set of candidate survivor cids (redirect via merge root)
    dropped_targets: dict = defaultdict(set)
    for dsrc, did, surv_cid in attach_raw:
        target = merge_uf.find(surv_cid) if surv_cid in merge_uf.p else surv_cid
        dropped_targets[(dsrc, did)].add(target)

    attach_plan: dict = defaultdict(set)   # target_cid -> {(source, dropped_id)}
    conflicts: list = []
    for key, targets in dropped_targets.items():
        chosen = min(targets)              # deterministic: lowest cid
        attach_plan[chosen].add(key)
        if len(targets) > 1:
            conflicts.append({"dropped": {"source": key[0], "id": key[1]},
                              "chosen": chosen, "all_targets": sorted(targets)})

    # pass 1 — collect the merge-involved rows
    merge_rows: dict = {}
    for row in iter_buildings(C9):
        cid = row.get("canonical_bld_id")
        if cid in merge_cids:
            merge_rows[cid] = row

    # build survivor patches (survivor absorbs every loser in its component)
    comp_members: dict = defaultdict(list)
    for cid in merge_cids:
        comp_members[merge_uf.find(cid)].append(cid)
    survivor_patch: dict = {}
    loser_cids = merge_cids - set(comp_members)
    for surv_cid, members in comp_members.items():
        surv = merge_rows.get(surv_cid)
        if surv is None:
            continue
        merged = dict(surv)
        for lc in (m for m in members if m != surv_cid):
            loser = merge_rows.get(lc)
            if loser is None:
                continue
            sr = dict(merged.get("source_refs") or {})
            for s, ids in (loser.get("source_refs") or {}).items():
                sr[s] = _union(sr.get(s), ids)
            merged["source_refs"] = sr
            su = dict(loser.get("source_urls") or {})
            su.update(merged.get("source_urls") or {})   # survivor wins
            merged["source_urls"] = su
            for f in _ABSORB_UNION:
                merged[f] = _union(merged.get(f), loser.get(f))
            for f in _ABSORB_IF_EMPTY:
                if _is_empty(merged.get(f)) and not _is_empty(loser.get(f)):
                    merged[f] = loser.get(f)
        n = len(merged.get("source_refs") or {})
        merged["n_sources"] = n
        merged["confidence_tier"] = _confidence_tier(n)
        survivor_patch[surv_cid] = merged

    # fold attaches that target a survivor into its patch; the rest stay standalone
    attach_only: dict = {}
    for target, keys in attach_plan.items():
        if target in survivor_patch:
            merged = survivor_patch[target]
            sr = dict(merged.get("source_refs") or {})
            for s, did in keys:
                sr[s] = _union(sr.get(s), [did])
            merged["source_refs"] = sr
            merged["n_sources"] = len(sr)
            merged["confidence_tier"] = _confidence_tier(len(sr))
        else:
            attach_only[target] = keys

    # pass 2 — stream + apply + write
    counts: Counter = Counter()
    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C9):
            n_in += 1
            cid = row.get("canonical_bld_id")
            if cid in loser_cids:
                counts["merge_loser_removed"] += 1
                continue  # merged into survivor — loser row removed
            if cid in survivor_patch:
                row = survivor_patch[cid]
                counts["merge_survivor"] += 1
            if cid in attach_only:
                sr = dict(row.get("source_refs") or {})
                for s, did in attach_only[cid]:
                    sr[s] = _union(sr.get(s), [did])
                row["source_refs"] = sr
                row["n_sources"] = len(sr)
                row["confidence_tier"] = _confidence_tier(len(sr))
                counts["attach_target"] += 1
            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    with CONFLICTS.open("w", encoding="utf-8") as f:
        for c in conflicts:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    removed_ids = sorted(loser_cids)
    ok = n_out == n_in - len(removed_ids)
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C9.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "rows_removed": len(removed_ids),
        "row_count_ok": ok,
        "attach_pairs_seen": len(attach_raw),
        "dropped_buildings_attached": len(dropped_targets),
        "attach_target_rows": len(attach_plan),
        "merge_components": len(comp_members),
        "merge_losers_removed": len(removed_ids),
        "conflicts": len(conflicts),
        "unrecoverable_both_dropped": unrecoverable,
        "counts": dict(counts),
        "removed_canonical_ids": removed_ids,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "PASS" if ok else "FAIL"
    print(f"C10 recovery [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps({k: v for k, v in report.items()
                      if k != "removed_canonical_ids"}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
