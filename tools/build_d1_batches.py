"""Stage D-1 text enrichment batch builder.

For each canonical building, gather concatenated descriptions from all
source DBs (divisare, architizer, archello, metalocus). Output batches
that Sonnet sub-agents can process.

Per canonical:
  {
    "i": <int>,
    "cid": "bld_NNNNNN",
    "primary_name": str,
    "arch_names": [str, ...],
    "city": str | null,
    "country": str | null,
    "year": int | null,
    "typology": str | null,
    "descriptions": [
      {"source": "divisare", "text": "..."},
      {"source": "architizer", "text": "..."},
      ...
    ]
  }

Sonnet agent then classifies each into:
  - program (PROGRAM enum)
  - style (STYLE enum)
  - color_tone (COLOR_TONE enum)
  - atmosphere (ATMOSPHERE enum)
  - material_visual (list of strings)
  - visual_description (str, ~80 words)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


CANONICAL_PATH = "data/canonical/canonical_buildings_4source.json"
DIVISARE_DB    = "data/crawl/divisare.db"
ARCHITIZER_DB  = "data/crawl/architizer.db"
ARCHELLO_DB    = "data/crawl/archello.db"
METALOCUS_FINAL = "data/enrich/4_buildings_final.json"
BATCH_DIR      = "data/canonical/d1_batches"

MAX_DESC_LEN = 1500   # truncate per-source descriptions to keep prompt manageable


def _load_descriptions() -> dict[tuple, str]:
    """Returns {(source, source_id): description_text}."""
    out: dict[tuple, str] = {}

    conn = sqlite3.connect(DIVISARE_DB)
    for r in conn.execute(
        "SELECT id, description, abstract FROM divisare_projects "
        "WHERE description IS NOT NULL OR abstract IS NOT NULL"
    ):
        text = (r[1] or "") + " " + (r[2] or "")
        if text.strip():
            out[("divisare", str(r[0]))] = text.strip()[:MAX_DESC_LEN]
    conn.close()

    conn = sqlite3.connect(ARCHITIZER_DB)
    for r in conn.execute(
        "SELECT id, description, description_short FROM architizer_projects "
        "WHERE description IS NOT NULL OR description_short IS NOT NULL"
    ):
        text = (r[1] or "") + " " + (r[2] or "")
        if text.strip():
            out[("architizer", str(r[0]))] = text.strip()[:MAX_DESC_LEN]
    conn.close()

    conn = sqlite3.connect(ARCHELLO_DB)
    for r in conn.execute(
        "SELECT id, description FROM archello_projects WHERE description IS NOT NULL"
    ):
        if r[1] and r[1].strip():
            out[("archello", str(r[0]))] = r[1].strip()[:MAX_DESC_LEN]
    conn.close()

    final = json.load(open(METALOCUS_FINAL))
    for b in final:
        if b.get("description") and str(b["description"]).strip():
            out[("metalocus", str(b["building_id"]))] = str(b["description"]).strip()[:MAX_DESC_LEN]

    return out


def _load_arch_names() -> dict[str, str]:
    """Returns {canonical_arch_id: name}."""
    arch = json.load(open("data/canonical/architects_canonical.json"))
    return {a["canonical_arch_id"]: a["canonical_name"] for a in arch["clusters"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope",
                    choices=["t1", "t2", "all", "multi"],
                    default="t1",
                    help="t1 = 3+ source, t2 = 2-source, multi = 2+ source, all = everything")
    ap.add_argument("--batch-size", type=int, default=30)
    ap.add_argument("--out-dir", default=BATCH_DIR)
    args = ap.parse_args()

    print("loading descriptions …", flush=True)
    desc = _load_descriptions()
    print(f"  {len(desc)} source-side descriptions", flush=True)
    arch_names = _load_arch_names()

    print("loading canonical buildings …", flush=True)
    data = json.load(open(CANONICAL_PATH))
    b = data["buildings"]

    # Filter by scope
    if args.scope == "t1":
        targets = [x for x in b if x["n_sources"] >= 3]
    elif args.scope == "t2":
        targets = [x for x in b if x["n_sources"] == 2]
    elif args.scope == "multi":
        targets = [x for x in b if x["n_sources"] >= 2]
    else:
        targets = b
    print(f"  scope={args.scope}: {len(targets)} canonicals", flush=True)

    # Build batch entries
    entries = []
    for x in targets:
        descs = []
        for src, ids in x["source_refs"].items():
            for sid in ids:
                t = desc.get((src, str(sid)))
                if t:
                    descs.append({"source": src, "text": t})
                    break  # one desc per source — no need for multiple
        if not descs:
            continue  # skip if no description from any source

        arch_n = [arch_names.get(a, a) for a in x["canonical_arch_ids"][:3]]
        entries.append({
            "cid":          x["canonical_bld_id"],
            "primary_name": x["primary_name"],
            "arch_names":   arch_n,
            "city":         x["city"],
            "country":      x["country"],
            "year":         x["year"],
            "typology":     x["typology"],
            "descriptions": descs,
        })

    print(f"  with descriptions: {len(entries)}", flush=True)

    # Split into batches
    os.makedirs(args.out_dir, exist_ok=True)
    n_batches = (len(entries) + args.batch_size - 1) // args.batch_size
    for n in range(n_batches):
        chunk = entries[n*args.batch_size:(n+1)*args.batch_size]
        # Add local index for cross-ref
        for i, e in enumerate(chunk):
            e["i"] = i
        path = os.path.join(args.out_dir, f"batch_{n:03d}.json")
        with open(path, "w") as f:
            json.dump(chunk, f, indent=2, ensure_ascii=False)

    print(f"\n✓ {n_batches} batches × ~{args.batch_size} entries → {args.out_dir}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
