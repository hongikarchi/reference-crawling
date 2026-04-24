#!/usr/bin/env python3
"""Targeted re-processing of existing buildings (Phase 6).

Identifies buildings that the new architecture flags as needing re-analysis,
deletes their stale task rows, removes them from `3_buildings_analyzed.json`
(so the worker can re-add fresh output), and enqueues new pending analyze
tasks in `tasks.db`. Optionally the same for enrich.

Flagging sources (combine with OR):
  --from-vocab-migration   data/reports/vocab_migration.json — the buildings
                           whose atmosphere/style/color_tone was flagged as
                           legacy_dropped or unknown_value. Phase 1's
                           `needs_reanalysis_all` field.
  --from-eval-report       data/reports/eval_report.json — optional. Buildings
                           whose analyze score is below `--eval-threshold`.
  --from-ids-file PATH     a JSON file with either [<ids>] or {"ids": [<ids>]}.
                           Manual override / one-off lists.

Usage:
    python3 reprocess.py --from-vocab-migration --dry-run
    python3 reprocess.py --from-vocab-migration --limit 50
    python3 reprocess.py --from-vocab-migration --task both
    python3 reprocess.py --from-ids-file ~/to-redo.json --task analyze
    python3 reprocess.py --from-eval-report --eval-threshold 0.7

Output:
    data/reports/reprocess_plan.json   (always — audit of what will/did happen)

Does NOT call the API. Run `python3 run.py harness` afterward to drain the
queue. Separation is deliberate: you preview here, commit to spend there.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

import config
import tasks_db
import vocab


VOCAB_MIGRATION_PATH = os.path.join(config.REPORTS_DIR, "vocab_migration.json")
EVAL_REPORT_PATH     = os.path.join(config.REPORTS_DIR, "eval_report.json")
REPROCESS_PLAN_PATH  = os.path.join(config.REPORTS_DIR, "reprocess_plan.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------

def ids_from_vocab_migration() -> tuple[list, list]:
    """Returns (ids_for_analyze, reasons_per_id)."""
    if not os.path.exists(VOCAB_MIGRATION_PATH):
        return [], []
    with open(VOCAB_MIGRATION_PATH, encoding="utf-8") as f:
        report = json.load(f)

    ids = report.get("needs_reanalysis_all") or report.get("needs_reanalysis_examples") or []
    if "events" in report:
        reason_by_id: dict = {}
        for ev in report["events"]:
            if ev.get("reason") not in ("legacy_dropped", "unknown_value"):
                continue
            bid = ev["building_id"]
            reason_by_id.setdefault(bid, []).append(f"{ev['field']}:{ev['reason']}")
        reasons = [
            {"building_id": bid, "reasons": reason_by_id.get(bid, ["unknown"])}
            for bid in ids
        ]
    else:
        reasons = [{"building_id": bid, "reasons": ["vocab_migration"]} for bid in ids]
    return list(ids), reasons


def ids_from_eval_report(threshold: float) -> tuple[list, list]:
    """Buildings whose analyze scores average below the threshold."""
    if not os.path.exists(EVAL_REPORT_PATH):
        return [], []
    with open(EVAL_REPORT_PATH, encoding="utf-8") as f:
        report = json.load(f)

    flagged: list = []
    reasons: list = []
    for entry in report.get("per_building", []):
        analyze = entry.get("analyze") or {}
        scores = analyze.get("scores")
        if not scores:
            continue
        numeric = [v for v in scores.values() if isinstance(v, (int, float))]
        if not numeric:
            continue
        mean = sum(numeric) / len(numeric)
        if mean < threshold:
            flagged.append(entry["building_id"])
            reasons.append({"building_id": entry["building_id"],
                            "reasons": [f"eval_avg={mean:.3f}<{threshold}"]})
    return flagged, reasons


def ids_from_file(path: str) -> tuple[list, list]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        ids = data.get("ids") or []
    elif isinstance(data, list):
        ids = data
    else:
        raise ValueError(f"unsupported shape in {path}")
    return list(ids), [{"building_id": bid, "reasons": ["manual_list"]} for bid in ids]


# ---------------------------------------------------------------------------
# JSON manipulation
# ---------------------------------------------------------------------------

def _remove_from_json(path: str, ids: set) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        buildings = json.load(f)
    before = len(buildings)
    buildings = [b for b in buildings if b.get("building_id") not in ids]
    removed = before - len(buildings)
    if removed:
        _atomic_write(path, buildings)
    return removed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enqueue targeted re-processing of flagged buildings."
    )
    parser.add_argument("--from-vocab-migration", action="store_true",
                        help="Flag buildings listed in data/reports/vocab_migration.json")
    parser.add_argument("--from-eval-report", action="store_true",
                        help="Flag buildings whose eval scores fall below --eval-threshold")
    parser.add_argument("--eval-threshold", type=float, default=0.7,
                        help="Mean-of-numeric-scores threshold (default 0.7)")
    parser.add_argument("--from-ids-file", type=str, default=None,
                        help="JSON file with a flat id list or {\"ids\": [...]}")
    parser.add_argument("--task", choices=["enrich", "analyze", "both"], default="analyze",
                        help="Which task type to re-enqueue (default analyze — the"
                             " atmosphere-drift use case)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap on number of buildings to re-enqueue this run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only — do not touch tasks.db or JSON files")
    args = parser.parse_args()

    if not (args.from_vocab_migration or args.from_eval_report or args.from_ids_file):
        print("ERROR: specify at least one of --from-vocab-migration / "
              "--from-eval-report / --from-ids-file")
        return 1

    tasks_db.init()

    sources: list = []
    all_ids: list = []
    all_reasons: dict = {}

    if args.from_vocab_migration:
        ids, reasons = ids_from_vocab_migration()
        sources.append(("vocab_migration", len(ids)))
        for r in reasons:
            all_reasons.setdefault(r["building_id"], []).extend(r["reasons"])
        all_ids.extend(ids)

    if args.from_eval_report:
        ids, reasons = ids_from_eval_report(args.eval_threshold)
        sources.append(("eval_report", len(ids)))
        for r in reasons:
            all_reasons.setdefault(r["building_id"], []).extend(r["reasons"])
        all_ids.extend(ids)

    if args.from_ids_file:
        ids, reasons = ids_from_file(args.from_ids_file)
        sources.append(("ids_file", len(ids)))
        for r in reasons:
            all_reasons.setdefault(r["building_id"], []).extend(r["reasons"])
        all_ids.extend(ids)

    # Dedupe while preserving order.
    seen: set = set()
    unique_ids: list = []
    for bid in all_ids:
        if bid in seen:
            continue
        seen.add(bid)
        unique_ids.append(bid)
    if args.limit:
        unique_ids = unique_ids[: args.limit]

    task_types = ["enrich", "analyze"] if args.task == "both" else [args.task]
    print(f"Re-process plan:")
    print(f"  Sources: {sources}")
    print(f"  Unique buildings: {len(unique_ids)} (limit={args.limit})")
    print(f"  Tasks per building: {task_types}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'APPLY'}")
    print()

    actions: list = []
    deleted_tasks = 0
    removed_from_analyze = 0
    removed_from_enrich = 0

    if not args.dry_run:
        id_set = set(unique_ids)
        if "analyze" in task_types:
            removed_from_analyze = _remove_from_json(config.ANALYZED_JSON, id_set)
        if "enrich" in task_types:
            # Dropping from ENRICHED forces analyze to re-flow after re-enrich
            # (the worker's _handle_analyze checks enriched_by_id).
            removed_from_enrich = _remove_from_json(config.ENRICHED_JSON, id_set)
            # Also remove the downstream analyzed entry so the chain re-runs.
            removed_from_analyze += _remove_from_json(config.ANALYZED_JSON, id_set)

        for bid in unique_ids:
            for tt in task_types:
                n = tasks_db.delete_for_reprocess(bid, tt)
                deleted_tasks += n
                actions.append({"building_id": bid, "task_type": tt,
                                "deleted_rows": n,
                                "reasons": all_reasons.get(bid, [])})
    else:
        for bid in unique_ids:
            for tt in task_types:
                actions.append({"building_id": bid, "task_type": tt,
                                "deleted_rows": 0,
                                "reasons": all_reasons.get(bid, [])})

    # Re-run the enqueue pass so new pending tasks appear — but only in apply mode.
    added = {"enrich": 0, "analyze": 0}
    if not args.dry_run:
        import pipeline_harness
        added = pipeline_harness.enqueue_batch()

    reason_hist = Counter(r for reasons in all_reasons.values() for r in reasons)

    plan = {
        "run_id":    _now_iso(),
        "mode":      "dry_run" if args.dry_run else "apply",
        "vocab_version": vocab.VOCAB_VERSION,
        "sources":   [{"source": s, "count": c} for s, c in sources],
        "task_types": task_types,
        "limit":     args.limit,
        "buildings_targeted": len(unique_ids),
        "reasons_histogram": dict(reason_hist.most_common()),
        "deleted_task_rows": deleted_tasks,
        "removed_from_enriched_json": removed_from_enrich,
        "removed_from_analyzed_json": removed_from_analyze,
        "pending_after_enqueue": added,
        "actions": actions,
    }

    _atomic_write(REPROCESS_PLAN_PATH, plan)
    print(f"Plan written → {REPROCESS_PLAN_PATH}")
    print(f"  Buildings targeted: {len(unique_ids)}")
    print(f"  Reasons histogram: {dict(reason_hist.most_common(5))}")
    if not args.dry_run:
        print(f"  Task rows deleted: {deleted_tasks}")
        print(f"  Removed from 3_analyzed.json: {removed_from_analyze}")
        if "enrich" in task_types:
            print(f"  Removed from 2_enriched.json: {removed_from_enrich}")
        print(f"  New pending after enqueue: enrich={added['enrich']} analyze={added['analyze']}")
        print()
        print("Next: run `python3 run.py harness` to drain the new pending tasks.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
