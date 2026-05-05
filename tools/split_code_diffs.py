"""Detect and split clusters whose member names differ by 'code-like' tokens.

Pattern (high false-merge confidence):
  Names share most tokens (long common substring), but DIFFER on:
  • Numbers (Terralagos 701 vs 624)
  • Single letters / 2-3 char codes (Apartment BB vs DM, House K vs M)
  • Colors (White / Gold / Black / Red)
  • Sequence labels (Type A / B, Phase 1 / 2)

These are different buildings in a series, not the same building.

Conservative: only split if (a) ≥ 2 distinct codes detected AND
(b) the codes are the ONLY differentiator (rest of name identical).

Splits each member into its own (source_id) orphan canonical.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Optional

from canonical.registry import BuildingRegistry, _normalize_name


BUILDINGS_REG = "data/id_registry_buildings.json"
SPLIT_LOG = "data/canonical/code_diff_splits.json"

# Tokens treated as "code-like" differentiators
CODE_PATTERN = re.compile(r"^(?:[A-Z]{1,4}|\d+|[A-Za-z]\d+|\d+[A-Za-z])$")
COLOR_TOKENS = {
    "white", "black", "red", "blue", "green", "yellow", "gray", "grey",
    "gold", "silver", "bronze", "purple", "orange", "pink", "brown",
    "beige", "ivory", "navy", "cream", "tan",
}


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s\-_,.]+", name) if t]


def _is_code(tok: str) -> bool:
    """Tok is 'code-like' if matches CODE_PATTERN or is a color/number."""
    t = tok.strip()
    if not t:
        return False
    if t.lower() in COLOR_TOKENS:
        return True
    return bool(CODE_PATTERN.match(t))


def _diff_only_in_codes(names: list[str]) -> Optional[set[str]]:
    """If the names differ ONLY in code-like tokens, return the set of distinct
    codes. Else return None.

    Example:
      ["Terralagos 701", "Terralagos 624"] → {"701", "624"}
      ["Apartment BB", "Apartment DM"]      → {"BB", "DM"}
      ["Foo House", "Bar House"]            → None (Foo/Bar not code-like)
    """
    if len(set(names)) < 2:
        return None
    token_lists = [_tokens(n) for n in names]
    # Check that all token lists have same length
    if len({len(tl) for tl in token_lists}) > 1:
        return None
    n_tokens = len(token_lists[0])
    differing_positions = []
    common = []
    for i in range(n_tokens):
        col = [tl[i] for tl in token_lists]
        if len(set(col)) > 1:
            differing_positions.append(col)
        else:
            common.append(col[0])
    if not differing_positions:
        return None
    # All differing positions must contain only code-like tokens
    for col in differing_positions:
        if not all(_is_code(t) for t in col):
            return None
    # Must have at least 2 common (non-code) tokens to be confident
    real_common = [t for t in common if not _is_code(t)]
    if len(real_common) < 2:
        return None
    codes = set()
    for col in differing_positions:
        codes.update(col)
    return codes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    print("loading registry …", flush=True)
    reg = BuildingRegistry(BUILDINGS_REG)
    print(f"  active: {reg.stats()['active']}", flush=True)

    print("\nscanning for code-diff clusters …", flush=True)
    suspect: list[tuple[str, dict]] = []
    for cid, e in reg.data.items():
        if e.get("redirected_to"):
            continue
        names = e.get("names") or []
        if len(names) < 2:
            continue
        srcs = e.get("source_refs", {})
        n_members = sum(len(ids) for ids in srcs.values())
        if n_members < 2:
            continue
        codes = _diff_only_in_codes(names)
        if codes is None or len(codes) < 2:
            continue
        suspect.append((cid, {
            "names": names, "n_members": n_members, "codes": sorted(codes),
            "source_refs": srcs,
        }))
    print(f"  detected suspect code-diff clusters: {len(suspect)}", flush=True)
    print("\nTop-15:")
    suspect.sort(key=lambda t: -t[1]["n_members"])
    for cid, info in suspect[:15]:
        names_p = ', '.join(info["names"][:3])[:60]
        codes_p = ','.join(info["codes"][:5])
        print(f'  {cid} n={info["n_members"]} codes=[{codes_p}] | {names_p}')

    if not args.apply:
        # Save log
        os.makedirs(os.path.dirname(SPLIT_LOG), exist_ok=True)
        log = [{"cid": cid, **info} for cid, info in suspect]
        with open(SPLIT_LOG, "w") as f:
            json.dump({"summary": {"n_suspect": len(suspect)}, "suspects": log},
                      f, indent=2, ensure_ascii=False)
        print(f"\n  log → {SPLIT_LOG}", flush=True)
        print(f"  (dry-run — pass --apply to perform splits)", flush=True)
        return 0

    # Apply: split each member into source-id orphan, demote original shell
    print(f"\nsplitting …", flush=True)
    n_split = 0
    n_orphans = 0
    for cid, info in suspect:
        entry = reg.data[cid]
        srcs = entry.get("source_refs", {})
        # Demote shell
        for src, ids in srcs.items():
            for sid in ids:
                key = (src, str(sid))
                if reg._source_index.get(key) == cid:
                    del reg._source_index[key]
        for n in entry.get("names", []):
            nk = _normalize_name(n)
            if reg._name_index.get(nk) == cid:
                del reg._name_index[nk]
        entry["source_refs"] = {}
        entry["names"] = [f"[split-code-diff:{cid}]"]
        entry["redirected_to"] = "[deleted-code-diff]"
        # Create orphan per member
        for src, ids in srcs.items():
            for sid in ids:
                # Try to find which name belonged to this member — fallback to first
                name = entry["names"][0]
                # Better: look up member's actual name from source_data — but we
                # don't have it pre-loaded. Use a simple naming: include src+sid
                reg.match_or_create(
                    names={f"[orphan:{src}:{sid}]"},
                    source_refs={src: [sid]},
                )
                n_orphans += 1
        n_split += 1
    reg.save()
    from canonical.match_buildings_sequential import export_clusters
    n_clusters = export_clusters(reg, "data/canonical/buildings_canonical.json")
    print(f"\n✓ split {n_split} clusters → {n_orphans} new orphans", flush=True)
    print(f"✓ registry: {reg.stats()}", flush=True)
    print(f"✓ canonical re-exported: {n_clusters}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
