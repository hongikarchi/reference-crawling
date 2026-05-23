#!/usr/bin/env python3
"""C15 make_web polish — pre-upsert quality gate on top of C14.

Addresses Codex's read-only make_web audits before any Neon upsert:
  B. HTTPS normalize — http://architizer-prod.imgix.net → https:// on all
     image URL fields (~9,375 publishable covers).
  C. Internal image dedupe — drop duplicate phash / url image objects within
     a row (~21k rows with internal dups, ~27k excess objects).
  D. Cross-card cover swap — 10 exact + 13 near display-cover phash dup
     groups: lower-confidence row gets cover re-picked from a non-dup phash.
     No merge (user decision); split-candidate sidecar for manual review.
  E. SEO / article / generic-name unpublish — explicit Codex actionable lists
     + generic-name regex; user decision C (aggressive).
  F. Location cleanup — publishable rows missing country -> unpublish;
     suspicious city values (pure numeric, year-prefix, street address) ->
     null-out city (no unpublish).
  G. `year_kind` field added — completed / future / unknown derived from
     project_year (vs 2026 cutoff). Future-year rows STAY publishable.
  H. String hygiene — outer whitespace, multi-space, CP1252 mojibake,
     control chars on name / architects_text / location_city.

Streaming, strict artifact. Read-only w.r.t. Neon.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_strict_canonical import _display_cover_url  # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C14 = CCR / "canonical_buildings_strict.completeness_c14_finalize.json"
ACTIONABLE = ROOT / "data/reports/canonical_v2_c14_make_web_actionable_issues.codex_audit.json"
GATE = ROOT / "data/reports/canonical_v2_c14_make_web_quality_gate.codex_audit.json"
CLUSTERS = ROOT / "data/reports/canonical_v2_image_dup_clusters.jsonl"

OUT = CCR / "canonical_buildings_strict.completeness_c15_make_web_polish.json"
REPORT = ROOT / "data/reports/canonical_v2_c15_make_web_polish_report.json"
SPLIT_SIDECAR = ROOT / "data/reports/canonical_v2_c15_split_candidates.jsonl"
HYGIENE_DIFF = ROOT / "data/reports/canonical_v2_c15_string_hygiene_diff.json"

CURRENT_YEAR = 2026

# --- patterns ---
_HTTP_HOST_REWRITE = (
    ("http://architizer-prod.imgix.net", "https://architizer-prod.imgix.net"),
)
_GENERIC_NAME_EXACT = {
    "office", "house", "home", "project name", "untitled", "project",
    "building", "apartment", "renovation", "hotel", "school", "museum",
    "library", "shop", "cafe", "restaurant", "studio", "villa", "warehouse",
    "factory", "arch a", "blah", "tbd", "n/a",
}
# narrower than Codex's wider gate; only obviously broken city values
_SUSPICIOUS_CITY = re.compile(
    r"^\s*("
    r"\d+[a-z]?"                      # "31", "8b", "12a"
    r"|[A-Za-z]"                      # "O", "A" single letter
    r"|(19|20)\d{2}\s*[-–]"           # year-prefix "2010 - Bova"
    r"|.*,\s*(via|rua|street|st\.?|avenue|ave|road|rd\.?)\s+\S+"  # ", via Mazzini 48"
    r")\s*$",
    re.I,
)
# CP1252 mojibake mapping when source was decoded as Latin-1
_CP1252_REPLACEMENTS = {
    "\x91": "'", "\x92": "'", "\x93": '"', "\x94": '"',
    "\x95": "*", "\x96": "-", "\x97": "-", "\x98": "~", "\x99": "TM",
    "\x9a": "s", "\x9c": "oe", "\x9e": "z", "\x9f": "Y",
    "\x80": "EUR", "\x82": ",", "\x84": '"', "\x85": "...",
    "\x86": "+", "\x87": "++", "\x89": "%", "\x8b": "<", "\x8c": "OE",
    "\x8e": "Z",
}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")


# --- helpers ---
def _https_url(url):
    if not isinstance(url, str):
        return url
    for src, dst in _HTTP_HOST_REWRITE:
        if url.startswith(src):
            return dst + url[len(src):]
    return url


def _dedup_images(images):
    if not isinstance(images, list):
        return images, 0
    seen_phash = set()
    seen_url = set()
    out = []
    excess = 0
    for im in images:
        if not isinstance(im, dict):
            out.append(im)
            continue
        ph = im.get("phash")
        url = im.get("url")
        if ph and ph in seen_phash:
            excess += 1
            continue
        if url and url in seen_url:
            excess += 1
            continue
        if ph:
            seen_phash.add(ph)
        if url:
            seen_url.add(url)
        out.append(im)
    return out, excess


def _strip_str(s):
    if not isinstance(s, str):
        return s, False
    orig = s
    for c, repl in _CP1252_REPLACEMENTS.items():
        if c in s:
            s = s.replace(c, repl)
    s = _CONTROL_RE.sub("", s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    s = s.strip()
    return s, s != orig


def _name_key(s):
    return " ".join(str(s or "").casefold().split())


def _year_kind(year):
    if year is None:
        return "unknown"
    try:
        y = int(year)
    except (TypeError, ValueError):
        return "unknown"
    if y > CURRENT_YEAR:
        return "future"
    return "completed"


def _is_suspicious_city(city):
    return isinstance(city, str) and bool(_SUSPICIOUS_CITY.match(city))


def _https_normalize_row(row):
    """Returns count of URLs rewritten."""
    n = 0
    for fld in ("display_cover_url", "cover_image_url_default"):
        v = row.get(fld)
        nv = _https_url(v)
        if nv != v:
            row[fld] = nv
            n += 1
    cbt = row.get("covers_by_type")
    if isinstance(cbt, dict):
        for k, v in list(cbt.items()):
            nv = _https_url(v)
            if nv != v:
                cbt[k] = nv
                n += 1
    bipc = row.get("best_image_per_cluster")
    if isinstance(bipc, dict):
        for k, im in bipc.items():
            if isinstance(im, dict):
                nv = _https_url(im.get("url"))
                if nv != im.get("url"):
                    im["url"] = nv
                    n += 1
    for im in row.get("all_images") or []:
        if isinstance(im, dict):
            nv = _https_url(im.get("url"))
            if nv != im.get("url"):
                im["url"] = nv
                n += 1
    return n


def _hygiene_row(row, counts):
    """Apply string hygiene to name/architects_text/location_city. Mutates row."""
    diff = {}
    for fld in ("name", "architects_text", "location_city"):
        v = row.get(fld)
        nv, changed = _strip_str(v)
        if changed:
            diff[fld] = {"before": v, "after": nv}
            row[fld] = nv
            counts[f"hygiene_{fld}"] += 1
    return diff


# --- cross-card cover-swap planning ---
def _load_swap_targets():
    """Return: dict cid → set of phashes to AVOID for display_cover, +
    pair_meta list for sidecar."""
    avoid = defaultdict(set)
    sidecar = []
    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    exact_groups = actionable["issues"]["cross_card_display_phash_duplicate_groups"]
    for g in exact_groups:
        ph = g["phash"]
        rows = g["rows"]
        # rank rows by confidence (n_sources desc, then n_images, lower=loser)
        ranked = sorted(rows, key=lambda r: (
            -len(r.get("source_refs") or {}),
            -sum(len(v or []) for v in (r.get("source_refs") or {}).values()),
        ))
        winner = ranked[0]["cid"]
        for r in ranked[1:]:
            avoid[r["cid"]].add(ph)
        sidecar.append({
            "kind": "exact",
            "phash": ph,
            "winner_cid": winner,
            "swap_cids": [r["cid"] for r in ranked[1:]],
            "rows": [{"cid": r["cid"], "name": r.get("name"),
                      "country": r.get("country"), "city": r.get("city"),
                      "year": r.get("year"), "arch": r.get("architect_ids")}
                     for r in rows],
        })
    # near pairs from image_dup_clusters
    for line in CLUSTERS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if c.get("kind") != "near":
            continue
        # only include pairs where BOTH cover phashes are in this near-cluster
        cids = c.get("cids") or []
        if len(cids) < 2:
            continue
        # For near-pair cover dup: avoid both phashes on lower-confidence cids
        phashes = c.get("phash") or []
        # we don't have meta here; sidecar list only — actual cover-swap for
        # near pairs handled via gate report below
    # near pairs (from gate report — has display_cover near samples)
    gate = json.loads(GATE.read_text(encoding="utf-8"))
    near_samples = gate["cross_card_image_duplicates"].get("near_display_cover_samples", [])
    for s in near_samples:
        phashes = s.get("phashes") or []
        rows = s.get("rows") or []
        if len(rows) < 2:
            continue
        ranked = sorted(rows, key=lambda r: (
            -len(r.get("source_refs") or {}),
        ))
        winner = ranked[0]["cid"]
        for r in ranked[1:]:
            for ph in phashes:
                avoid[r["cid"]].add(ph)
        sidecar.append({
            "kind": "near",
            "phashes": phashes,
            "hamming": s.get("hamming"),
            "winner_cid": winner,
            "swap_cids": [r["cid"] for r in ranked[1:]],
            "rows": [{"cid": r["cid"], "name": r.get("name"),
                      "country": r.get("country"), "city": r.get("city"),
                      "year": r.get("year"), "arch": list(r.get("arch") or [])}
                     for r in rows],
        })
    return avoid, sidecar


def _swap_cover(row, avoid_phashes, counts):
    """Re-derive display_cover excluding the dup phashes."""
    images = row.get("all_images") or []
    filtered = [im for im in images
                if not (isinstance(im, dict) and im.get("phash") in avoid_phashes)]
    cbt = {k: v for k, v in (row.get("covers_by_type") or {}).items()}
    # also blank typed covers if their phash matches avoid (URL lookup)
    avoid_urls = {im.get("url") for im in images
                  if isinstance(im, dict) and im.get("phash") in avoid_phashes}
    cbt = {k: (None if v in avoid_urls else v) for k, v in cbt.items()}
    cidu = row.get("cover_image_url_default")
    if cidu in avoid_urls:
        cidu = None
    new_dcu = _display_cover_url(
        covers_by_type=cbt,
        cover_image_url_default=cidu,
        all_images=filtered,
    )
    if new_dcu and new_dcu != row.get("display_cover_url"):
        row["display_cover_url"] = new_dcu
        counts["cover_swapped"] += 1
        return "swapped"
    if not new_dcu:
        counts["cover_swap_failed_no_alternative"] += 1
        return "no_alternative"
    counts["cover_swap_noop"] += 1
    return "noop"


# --- unpublish target loading ---
def _load_seo_set():
    actionable = json.loads(ACTIONABLE.read_text(encoding="utf-8"))
    seo_spam = {r["cid"] for r in actionable["issues"]["publishable_seo_or_spam_name"]}
    article = {r["cid"] for r in actionable["issues"]["publishable_article_or_marketing_name"]}
    return seo_spam | article


def _is_generic_exact_name(name):
    return _name_key(name) in _GENERIC_NAME_EXACT


# --- main ---
def main() -> int:
    for p in (C14, ACTIONABLE, GATE, CLUSTERS):
        if not p.exists():
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 2

    avoid_phashes, swap_sidecar = _load_swap_targets()
    seo_set = _load_seo_set()

    SPLIT_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    with SPLIT_SIDECAR.open("w", encoding="utf-8") as f:
        for entry in swap_sidecar:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    counts: Counter = Counter()
    hygiene_diffs: list = []
    n_in = n_out = 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fout:
        fout.write('{"buildings":[')
        for row in iter_buildings(C14):
            n_in += 1
            cid = row.get("canonical_bld_id")

            # B — HTTPS normalize
            n_https = _https_normalize_row(row)
            if n_https:
                counts["https_rewrites_total"] += n_https
                counts["https_rows_touched"] += 1

            # C — internal image dedupe
            new_imgs, excess = _dedup_images(row.get("all_images") or [])
            if excess:
                row["all_images"] = new_imgs
                counts["internal_dedupe_rows"] += 1
                counts["internal_dedupe_excess_removed"] += excess
            bipc = row.get("best_image_per_cluster")
            if isinstance(bipc, dict):
                seen_ph = set()
                new_bipc = {}
                bipc_excess = 0
                for k, im in bipc.items():
                    if isinstance(im, dict):
                        ph = im.get("phash")
                        if ph and ph in seen_ph:
                            bipc_excess += 1
                            continue
                        if ph:
                            seen_ph.add(ph)
                    new_bipc[k] = im
                if bipc_excess:
                    row["best_image_per_cluster"] = new_bipc
                    counts["bipc_dedupe_rows"] += 1

            # D — cross-card cover swap
            if cid in avoid_phashes:
                _swap_cover(row, avoid_phashes[cid], counts)

            # H — string hygiene (do BEFORE name-based unpublish checks)
            diff = _hygiene_row(row, counts)
            if diff:
                hygiene_diffs.append({"cid": cid, **diff})

            # G — year_kind
            yk = _year_kind(row.get("project_year"))
            row["year_kind"] = yk
            counts[f"year_kind_{yk}"] += 1

            # F-2 — suspicious city null-out
            city = row.get("location_city")
            if _is_suspicious_city(city):
                row["location_city"] = None
                counts["suspicious_city_nulled"] += 1

            # E + F-1 — unpublish gates (apply only if currently publishable)
            if row.get("is_publishable"):
                reasons = list(row.get("publishability_reasons") or [])
                changed = False
                # F-1 missing country
                if not row.get("location_country"):
                    if "missing_country" not in reasons:
                        reasons.append("missing_country")
                    counts["unpublish_missing_country"] += 1
                    changed = True
                # E SEO / article / generic
                if cid in seo_set or _is_generic_exact_name(row.get("name")):
                    if "seo_or_generic_title" not in reasons:
                        reasons.append("seo_or_generic_title")
                    counts["unpublish_seo_or_generic"] += 1
                    changed = True
                if changed:
                    row["publishability_reasons"] = reasons
                    row["is_publishable"] = False
                    counts["rows_unpublished_in_c15"] += 1

            fout.write(("," if n_out else "") + json.dumps(row, ensure_ascii=False))
            n_out += 1
        fout.write("]}")

    # write reports
    HYGIENE_DIFF.write_text(
        json.dumps({"rows_changed": len(hygiene_diffs),
                    "field_counts": {
                        "name": sum(1 for d in hygiene_diffs if "name" in d),
                        "architects_text": sum(1 for d in hygiene_diffs if "architects_text" in d),
                        "location_city": sum(1 for d in hygiene_diffs if "location_city" in d),
                    },
                    "samples": hygiene_diffs[:50]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base": str(C14.relative_to(ROOT)),
        "output": str(OUT.relative_to(ROOT)),
        "rows_in": n_in,
        "rows_out": n_out,
        "row_count_ok": n_in == n_out,
        "counts": dict(counts),
        "swap_sidecar_entries": len(swap_sidecar),
        "seo_set_size": len(seo_set),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    status = "PASS" if report["row_count_ok"] else "FAIL"
    print(f"C15 make_web polish [{status}] -> {OUT.relative_to(ROOT)}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"  split sidecar -> {SPLIT_SIDECAR.relative_to(ROOT)}")
    print(f"  hygiene diff  -> {HYGIENE_DIFF.relative_to(ROOT)}")
    return 0 if report["row_count_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
