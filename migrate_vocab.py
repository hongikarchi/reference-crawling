#!/usr/bin/env python3
"""Apply vocab.py migrations to existing data records.

Usage:
    python3 migrate_vocab.py              # dry-run analysis only
    python3 migrate_vocab.py --apply      # write changes to 4_buildings_final.json

Input:  data/4_buildings_final.json
Output: data/reports/vocab_migration.json  (always, audit log)
        data/4_buildings_final.json        (only with --apply)

Behavior:
  - Every record gains vocab_version (defaults to 'v1' if absent before migration,
    stamped to the current VOCAB_VERSION after migration).
  - migrate_strict is run on program, style, color_tone, atmosphere.
  - legacy_migrated values are rewritten in-place.
  - legacy_dropped values are set to None; the building is flagged for
    re-analysis in the audit log.
  - unknown_value (not a legacy, not canonical) is set to None for style /
    color_tone / atmosphere, or to "Other" for program.

No API calls, no embedding changes. Run stage3_embed.py separately if
embeddings need to reflect new values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import config
import vocab


_FIELDS_STRICT = ("style", "color_tone", "atmosphere")
_FIELDS_WITH_DEFAULT = {"program": "Other"}


def _migrate_building(b: dict, audit: list) -> dict:
    """Apply strict migrations to a single building. Returns mutated record."""
    building_id = b.get("building_id") or b.get("slug") or "?"

    for field, default in _FIELDS_WITH_DEFAULT.items():
        orig = b.get(field)
        fixed, reason = vocab.migrate_strict(field, orig)
        if fixed is None and orig:
            fixed = default
            reason = reason or "forced_default"
        if reason:
            audit.append({
                "building_id": building_id,
                "field": field,
                "from": orig,
                "to": fixed,
                "reason": reason,
            })
            b[field] = fixed

    for field in _FIELDS_STRICT:
        orig = b.get(field)
        fixed, reason = vocab.migrate_strict(field, orig)
        if reason:
            audit.append({
                "building_id": building_id,
                "field": field,
                "from": orig,
                "to": fixed,
                "reason": reason,
            })
            b[field] = fixed

    b["vocab_version"] = vocab.VOCAB_VERSION
    return b


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply vocab.py migrations to final JSON.")
    parser.add_argument("--apply", action="store_true",
                        help="Write changes in-place to 4_buildings_final.json")
    args = parser.parse_args()

    if not os.path.exists(config.FINAL_JSON):
        print(f"ERROR: {config.FINAL_JSON} not found.")
        return 1

    with open(config.FINAL_JSON, encoding="utf-8") as f:
        buildings = json.load(f)

    print(f"Migrating {len(buildings)} buildings to vocab {vocab.VOCAB_VERSION}...")
    print(f"  Mode: {'APPLY (in-place)' if args.apply else 'DRY RUN (no writes)'}")

    audit: list = []
    version_counter: Counter = Counter()
    for b in buildings:
        version_counter[b.get("vocab_version") or "<unstamped>"] += 1
        _migrate_building(b, audit)

    by_field_reason: Counter = Counter()
    for e in audit:
        by_field_reason[(e["field"], e["reason"])] += 1

    needs_reanalysis = sorted({
        e["building_id"] for e in audit
        if e["reason"] in ("legacy_dropped", "unknown_value")
    })

    report = {
        "vocab_version_target": vocab.VOCAB_VERSION,
        "applied": args.apply,
        "buildings_total": len(buildings),
        "prior_vocab_version_distribution": dict(version_counter),
        "rewrites_total": len(audit),
        "rewrites_by_field_reason": {
            f"{field}:{reason}": count
            for (field, reason), count in sorted(by_field_reason.items())
        },
        "needs_reanalysis_total": len(needs_reanalysis),
        "needs_reanalysis_examples": needs_reanalysis[:20],
        "needs_reanalysis_all": needs_reanalysis,
        "events": audit,
    }

    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(config.REPORTS_DIR, "vocab_migration.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  Report: {report_path}")

    if args.apply:
        with open(config.FINAL_JSON, "w", encoding="utf-8") as f:
            json.dump(buildings, f, ensure_ascii=False, indent=2)
        print(f"  Written: {config.FINAL_JSON}")

    print()
    print(f"Summary:")
    print(f"  Prior versions: {dict(version_counter)}")
    print(f"  Total rewrites: {len(audit)}")
    for (field, reason), count in sorted(by_field_reason.items()):
        print(f"    {field:12s} {reason:20s} {count}")
    print(f"  Needs re-analysis: {len(needs_reanalysis)} buildings")

    return 0


if __name__ == "__main__":
    sys.exit(main())
