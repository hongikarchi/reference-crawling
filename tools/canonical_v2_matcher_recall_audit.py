#!/usr/bin/env python3
"""Matcher recall audit — read-only diagnosis of cross-source under-merge.

The production building matcher (`canonical/match_buildings_sequential.py`)
generates merge candidates architect-anchored: an incoming building only sees
pool candidates that share a `canonical_arch_id` — the country fallback fires
only when the incoming building has NO arch_ids. So a building whose architect
mis-clustered can never be paired with its true cross-source twin.

This tool quantifies the resulting under-merge. It re-blocks every source
building by normalized name (looser than the matcher's `name_core`), keeps
non-generic-name pairs that also agree on country and year (±2), drops pairs
the pHash gate hard-BLOCKs, and reports how many such high-confidence twins the
production matcher did NOT merge into one `canonical_bld_id` — split by reason:

  same_canonical                       — already merged (correct)
  different_canonical_no_shared_arch    — architect-cluster mismatch; production
                                          never paired them (candidate gen miss)
  different_canonical_shared_arch       — was a candidate; failed thresholds
  one_or_both_dropped                   — a source row not in any canonical
  other                                 — unclassified

Read-only: no registry / Neon / artifact writes. No LLM.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.assemble_4source import _load_source_data            # noqa: E402
from canonical.match_buildings_sequential import _name_tokens       # noqa: E402
from canonical.match_phash_check import has_phash_overlap, _normalize_name  # noqa: E402
from tools.build_strict_canonical import _normalize_country         # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings      # noqa: E402

C9 = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c9.json"
REPORT = ROOT / "data/reports/canonical_v2_matcher_recall_audit.json"
PAIRS = ROOT / "data/reports/canonical_v2_matcher_recall_pairs.jsonl"
YEAR_TOL = 2
HUGE_GROUP = 30  # a name shared by >30 cross-source rows is generic in practice

# building-type nouns in common non-English languages — a name that is only
# such a noun plus a short token ("Casa M") is too generic to confirm a twin;
# match_buildings_sequential.GENERIC_TOKENS only covers English.
_EXTRA_GENERIC = {
    "casa", "casas", "maison", "maisons", "haus", "wohnhaus", "huis", "hus",
    "talo", "dom", "edificio", "edificios", "edifici", "vivienda", "viviendas",
    "villa", "villas", "ville", "immeuble", "logements", "appartement",
    "appartements", "wohnung", "wohnungen",
}


def _load_canonical_index():
    """ref_to_cid[(source,str(sid))] -> cid; cid_arch[cid] -> frozenset(arch ids)."""
    ref_to_cid: dict = {}
    cid_arch: dict = {}
    for row in iter_buildings(C9):
        cid = row.get("canonical_bld_id")
        if not cid:
            continue
        cid_arch[cid] = frozenset(row.get("architect_canonical_ids") or [])
        for source, sids in (row.get("source_refs") or {}).items():
            for sid in sids or []:
                ref_to_cid[(source, str(sid))] = cid
    return ref_to_cid, cid_arch


def _year(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _years_compatible(ya, yb) -> bool:
    # both years required — exact name + country alone is too weak to confirm
    # a twin for short/common names (audit tightening -> conservative count).
    return ya is not None and yb is not None and abs(ya - yb) <= YEAR_TOL


def _sim_bucket(s: float) -> str:
    if s >= 100:
        return "100"
    if s >= 95:
        return "95-99"
    if s >= 90:
        return "90-94"
    if s >= 80:
        return "80-89"
    return "<80"


def main() -> int:
    if not C9.exists():
        print(f"FATAL: C9 artifact missing: {C9}", file=sys.stderr)
        return 2

    src = _load_source_data()
    ref_to_cid, cid_arch = _load_canonical_index()

    per_source = Counter(s for s, _ in src)
    name_groups: dict = defaultdict(list)
    for (source, sid), info in src.items():
        norm = _normalize_name(str(info.get("name") or ""))
        if norm:
            name_groups[norm].append((source, sid))

    reasons: Counter = Counter()
    by_pair: dict = defaultdict(Counter)
    sim_buckets: Counter = Counter()
    year_basis: Counter = Counter()
    examples: list = []
    missed_pairs: list = []
    confirmed = 0
    multi_source_groups = skipped_generic = skipped_huge = 0

    for norm, members in name_groups.items():
        if len({s for s, _ in members}) < 2:
            continue
        multi_source_groups += 1
        if not (_name_tokens(norm) - _EXTRA_GENERIC):  # generic-only name
            skipped_generic += 1
            continue
        if len(members) > HUGE_GROUP:  # effectively-generic name
            skipped_huge += 1
            continue
        for (sa, ida), (sb, idb) in combinations(members, 2):
            if sa == sb:
                continue
            ia, ib = src[(sa, ida)], src[(sb, idb)]
            ca = _normalize_country(ia.get("country"))
            cb = _normalize_country(ib.get("country"))
            if not ca or not cb or ca.casefold() != cb.casefold():
                continue
            ya, yb = _year(ia.get("year")), _year(ib.get("year"))
            if not _years_compatible(ya, yb):
                continue
            ph = has_phash_overlap([ida], [idb], sa, sb,
                                   name_a=ia.get("name"), name_b=ib.get("name"),
                                   year_a=ya, year_b=yb)
            if ph.get("verdict") == "BLOCK":
                continue  # genuinely different buildings, coincidental name

            confirmed += 1
            year_basis["both_years_present" if (ya is not None and yb is not None)
                       else "a_year_missing"] += 1
            cid_a = ref_to_cid.get((sa, ida))
            cid_b = ref_to_cid.get((sb, idb))
            if cid_a and cid_b and cid_a == cid_b:
                reason = "same_canonical"
            elif cid_a is None or cid_b is None:
                reason = "one_or_both_dropped"
            elif cid_arch.get(cid_a, frozenset()) & cid_arch.get(cid_b, frozenset()):
                reason = "different_canonical_shared_arch"
            elif cid_a in cid_arch and cid_b in cid_arch:
                reason = "different_canonical_no_shared_arch"
            else:
                reason = "other"
            reasons[reason] += 1
            by_pair["|".join(sorted((sa, sb)))][reason] += 1

            if reason != "same_canonical":
                strict = min(
                    fuzz.token_sort_ratio(ia.get("name") or "", ib.get("name") or ""),
                    fuzz.token_set_ratio(ia.get("name") or "", ib.get("name") or ""),
                )
                sim_buckets[_sim_bucket(strict)] += 1
                rec = {
                    "name": norm, "reason": reason,
                    "a": {"source": sa, "id": ida, "cid": cid_a, "year": ya},
                    "b": {"source": sb, "id": idb, "cid": cid_b, "year": yb},
                }
                missed_pairs.append(rec)
                if len(examples) < 60:
                    examples.append(rec)

    missed = confirmed - reasons["same_canonical"]
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "c9_artifact": str(C9.relative_to(ROOT)),
        "source_buildings_loaded": dict(per_source),
        "multi_source_name_groups": multi_source_groups,
        "skipped_generic_name_groups": skipped_generic,
        "skipped_huge_name_groups": skipped_huge,
        "confirmed_twin_pairs": confirmed,
        "missed_total": missed,
        "missed_rate": round(missed / confirmed, 4) if confirmed else 0.0,
        "reason_breakdown": dict(reasons),
        "by_source_pair": {k: dict(v) for k, v in sorted(by_pair.items())},
        "twin_year_basis": dict(year_basis),
        "missed_name_sim_distribution": dict(sim_buckets),
        "missed_examples": examples,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with PAIRS.open("w", encoding="utf-8") as f:
        for rec in missed_pairs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"matcher recall audit -> {REPORT.relative_to(ROOT)}")
    print(f"full missed pairs    -> {PAIRS.relative_to(ROOT)} ({len(missed_pairs):,})")
    print(f"  source buildings: {dict(per_source)}")
    print(f"  multi-source name groups: {multi_source_groups:,} "
          f"(generic skipped {skipped_generic:,}, huge skipped {skipped_huge:,})")
    print(f"  confirmed cross-source twin pairs: {confirmed:,}")
    print(f"  MISSED (not co-merged): {missed:,}  ({report['missed_rate']:.1%})")
    for reason, n in reasons.most_common():
        print(f"    {n:>7,}  {reason}")
    print("  by source pair:")
    for pair, rc in report["by_source_pair"].items():
        miss = sum(v for k, v in rc.items() if k != "same_canonical")
        print(f"    {pair:<24} confirmed {sum(rc.values()):>6,}  missed {miss:>6,}")
    print(f"  twin year basis: {dict(year_basis)}")
    print(f"  missed name-sim: {dict(sim_buckets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
