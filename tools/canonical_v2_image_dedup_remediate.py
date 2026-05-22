#!/usr/bin/env python3
"""Build completeness_c13 — image-dup remediation on top of C12.

Consumes the cross-building image dup audit (canonical_v2_image_dup_audit.py)
and applies, per canonical row:

  1. degenerate-image strip — images whose phash is a blank / solid / uniform
     hash (extreme bit-density) are dropped from all_images /
     best_image_per_cluster; the display cover is re-derived; a row left with
     no real image becomes non-publishable.
  2. split-canonical merge — when an exact image phash is shared by two
     canonical rows that ALSO have the same country and name token_set_ratio
     >= 90 (diacritics stripped), they are the same building split into rows.
     Survivor = lowest bld_id; losers absorbed (source_refs + NULL-only
     enrichment) and removed. Connected clusters union-find to one survivor.
     Arch-overlap WITHOUT a name match is NOT merged (same architect, different
     building, shared series photo) — those go to an arch-review sidecar.

Also emits a spam-candidate sidecar (SEO / non-building rows) for a separate
user decision. Streaming, strict artifact. Read-only w.r.t. Neon.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import _confidence_tier, _display_cover_url  # noqa: E402
from tools.canonical_v2_recover_dropped_twins import (  # noqa: E402
    _UF, _union, _is_empty, _ABSORB_IF_EMPTY, _ABSORB_UNION,
)
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C12 = CCR / "canonical_buildings_strict.completeness_c12_taxonomy.json"
OUT = CCR / "canonical_buildings_strict.completeness_c13_imageqa.json"
CLUSTERS = ROOT / "data/reports/canonical_v2_image_dup_clusters.jsonl"
C10_REPORT = ROOT / "data/reports/canonical_v2_c10_recovery_report.json"
REPORT = ROOT / "data/reports/canonical_v2_c13_imageqa_report.json"
SPAM = ROOT / "data/reports/canonical_v2_c13_spam_candidates.json"
ARCH_REVIEW = ROOT / "data/reports/canonical_v2_c13_arch_review.jsonl"

MERGE_NAME_MIN = 90
_PLACEHOLDER_PATTERNS = ("facebook-default-thumb", "img-placeholder")
_SPAM_RE = re.compile(r"\d{2,}\s*\+|\bmẫu\b|interior designer|home remodel|"
                      r"interior decorator", re.I)


def _ascii(s) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()


def _name_key(s) -> str:
    return " ".join(_ascii(s).casefold().split())


def _name_sim(a, b) -> float:
    return fuzz.token_set_ratio(_name_key(a), _name_key(b))


def _img_url(image):
    return image.get("url") if isinstance(image, dict) else image


def _is_placeholder(url) -> bool:
    return any(p in str(url or "") for p in _PLACEHOLDER_PATTERNS)


def _is_degenerate_phash(ph) -> bool:
    """A blank / solid / uniform image — phash with extreme bit-density."""
    s = str(ph or "")
    if not s:
        return False
    try:
        bits = bin(int(s, 16)).count("1")
    except ValueError:
        return False
    total = len(s) * 4
    return bits <= 4 or bits >= total - 4


def _bad_image(image) -> bool:
    return (_is_placeholder(_img_url(image))
            or (isinstance(image, dict) and _is_degenerate_phash(image.get("phash"))))


def _strip_bad_images(row: dict) -> tuple[bool, bool]:
    """Drop degenerate/placeholder images; re-derive cover. Returns
    (stripped, lost_last_image)."""
    imgs = row.get("all_images") or []
    bipc = row.get("best_image_per_cluster")
    bipc = bipc if isinstance(bipc, dict) else {}
    cbt = row.get("covers_by_type")
    cbt = cbt if isinstance(cbt, dict) else {}
    if not (any(_bad_image(im) for im in imgs)
            or any(_bad_image(v) for v in bipc.values())
            or any(_is_placeholder(v) for v in cbt.values())
            or _is_placeholder(row.get("cover_image_url_default"))
            or _is_placeholder(row.get("display_cover_url"))):
        return False, False
    kept = [im for im in imgs if not _bad_image(im)]
    row["all_images"] = kept
    cbt = {k: (None if _is_placeholder(v) else v) for k, v in cbt.items()}
    row["covers_by_type"] = cbt
    row["best_image_per_cluster"] = {k: v for k, v in bipc.items()
                                     if not _bad_image(v)}
    if _is_placeholder(row.get("cover_image_url_default")):
        row["cover_image_url_default"] = None
    new_dcu = _display_cover_url(covers_by_type=cbt,
                                 cover_image_url_default=row.get("cover_image_url_default"),
                                 all_images=kept)
    row["display_cover_url"] = new_dcu
    if not new_dcu:
        reasons = list(row.get("publishability_reasons") or [])
        if "image_unavailable" not in reasons:
            reasons.append("image_unavailable")
        row["publishability_reasons"] = reasons
        row["is_publishable"] = False
        return True, True
    return True, False


def main() -> int:
    for path in (C12, CLUSTERS):
        if not path.exists():
            print(f"FATAL: missing input: {path}", file=sys.stderr)
            return 2

    exact = [json.loads(l) for l in CLUSTERS.read_text(encoding="utf-8").splitlines()
             if l.strip() and json.loads(l).get("kind") == "exact"]
    involved = {c for cl in exact for c in cl["cids"]}

    # pass 1 — collect full rows for the cids in exact clusters
    meta: dict = {}
    for row in iter_buildings(C12):
        cid = row.get("canonical_bld_id")
        if cid in involved:
            meta[cid] = row

    # merge plan — union pairs that are exact-image + same country + name>=90;
    # record arch-overlap-without-name pairs for the review sidecar.
    uf = _UF()
    arch_review: list = []
    seen_pairs: set = set()
    for cl in exact:
        for a, b in combinations(sorted(cl["cids"]), 2):
            ra, rb = meta.get(a), meta.get(b)
            if not ra or not rb or (a, b) in seen_pairs:
                continue
            seen_pairs.add((a, b))
            ca, cb = ra.get("location_country"), rb.get("location_country")
            same_country = ca and cb and ca == cb
            if same_country and _name_sim(ra.get("name"), rb.get("name")) >= MERGE_NAME_MIN:
                uf.union(a, b)
            elif (frozenset(ra.get("architect_canonical_ids") or [])
                  & frozenset(rb.get("architect_canonical_ids") or [])):
                arch_review.append({"a": a, "b": b,
                                    "name_a": ra.get("name"), "name_b": rb.get("name"),
                                    "country": ca})

    comp: dict = defaultdict(list)
    for cid in uf.p:
        comp[uf.find(cid)].append(cid)
    loser_cids = set(uf.p) - set(comp)

    survivor_patch: dict = {}
    for surv, members in comp.items():
        merged = dict(meta[surv])
        for lc in (m for m in members if m != surv):
            loser = meta.get(lc)
            if loser is None:
                continue
            sr = dict(merged.get("source_refs") or {})
            for s, ids in (loser.get("source_refs") or {}).items():
                sr[s] = _union(sr.get(s), ids)
            merged["source_refs"] = sr
            su = dict(loser.get("source_urls") or {})
            su.update(merged.get("source_urls") or {})
            merged["source_urls"] = su
            for f in _ABSORB_UNION:
                merged[f] = _union(merged.get(f), loser.get(f))
            for f in _ABSORB_IF_EMPTY:
                if _is_empty(merged.get(f)) and not _is_empty(loser.get(f)):
                    merged[f] = loser.get(f)
        n = len(merged.get("source_refs") or {})
        merged["n_sources"] = n
        merged["confidence_tier"] = _confidence_tier(n)
        survivor_patch[surv] = merged

    # pass 2 — stream, strip bad images, apply merges, write C13
    counts: Counter = Counter()
    spam: list = []
    n_in = n_out = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C12):
            n_in += 1
            cid = row.get("canonical_bld_id")
            if cid in loser_cids:
                counts["merge_loser_removed"] += 1
                continue
            if cid in survivor_patch:
                row = survivor_patch[cid]
                counts["merge_survivor"] += 1
            stripped, lost = _strip_bad_images(row)
            if stripped:
                counts["images_stripped"] += 1
            if lost:
                counts["now_unpublishable"] += 1
            if row.get("is_publishable") and _SPAM_RE.search(str(row.get("name") or "")):
                spam.append({"canonical_bld_id": cid, "name": row.get("name")})
            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    removed = sorted(loser_cids)
    cumulative = sorted(set(removed) | set(
        json.loads(C10_REPORT.read_text(encoding="utf-8")).get("removed_canonical_ids", [])
        if C10_REPORT.exists() else []))
    SPAM.write_text(json.dumps(spam, ensure_ascii=False, indent=2), encoding="utf-8")
    with ARCH_REVIEW.open("w", encoding="utf-8") as f:
        for r in arch_review:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C12.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "merge_components": len(comp),
        "merge_losers_removed": len(removed),
        "row_count_ok": n_out == n_in - len(removed),
        "counts": dict(counts),
        "arch_review_pairs": len(arch_review),
        "spam_candidates": len(spam),
        "removed_canonical_ids": cumulative,
        "removed_this_pass": removed,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C13 image-dedup remediation [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("removed_canonical_ids", "removed_this_pass")},
                     ensure_ascii=False, indent=2))
    print(f"  spam candidates -> {SPAM.relative_to(ROOT)} ({len(spam)})")
    print(f"  arch-review pairs -> {ARCH_REVIEW.relative_to(ROOT)} ({len(arch_review)})")
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
