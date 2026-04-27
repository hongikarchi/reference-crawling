#!/usr/bin/env python3
"""Pipeline harness — queue-driven, crash-safe AI orchestrator (Phase 3).

Drives new buildings through the pipeline using a SQLite-backed task queue
(`tasks_db`) that makes processing idempotent and resumable.

Flow:
  1_buildings_raw.json
    → [enqueue]              → pending `enrich` tasks in tasks.db
    → [worker + agent_llm_parser] → 2_buildings_enriched.json
    → [enqueue]              → pending `analyze` tasks in tasks.db
    → [worker + agent_image_analysis] → 3_buildings_analyzed.json
    → [qc pass]              → re-enqueue failures
    → [stage3_embed]         → 4_buildings_final.json

Crash-safety:
  - `tasks_db.mark_done` commits BEFORE JSON append, so a mid-write crash
    doesn't cause the worker to repeat the API call on restart.
  - `reset_stale` reclaims running tasks abandoned by dead workers.
  - JSON writes use atomic tmp+replace.

Usage:
    python3 run.py harness                     # enqueue + drain + qc + embed
    python3 run.py harness --limit 10          # cap worker iterations
    python3 run.py harness --status            # show tasks.db state
    python3 run.py harness --no-embed          # skip stage3_embed at end
    python3 run.py harness --enqueue-only      # populate queue without running
"""

import json
import os
import time
from datetime import datetime, timezone

from core import config
from enrich import tasks_db
from core import vocab
from enrich.llm_parser import enrich_building, prompt_version as enrich_prompt_version
from enrich.image_analysis import analyze_building, prompt_version as analyze_prompt_version
from core.vocab import check_building


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: str, data) -> None:
    """Write JSON atomically via tmp file + os.replace + fsync."""
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json_list(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_json_by_id(path: str) -> dict:
    return {b["building_id"]: b for b in load_json_list(path)}


def _append_to_json(path: str, building: dict) -> None:
    """Append one building to a JSON list file if its building_id is not present."""
    buildings = load_json_list(path)
    existing_ids = {b["building_id"] for b in buildings}
    if building["building_id"] in existing_ids:
        # Update in place — later runs may re-process with fresher output.
        buildings = [b for b in buildings if b["building_id"] != building["building_id"]]
    buildings.append(building)
    _atomic_write(path, buildings)


def _remove_from_json(path: str, building_id: str) -> None:
    buildings = load_json_list(path)
    buildings = [b for b in buildings if b["building_id"] != building_id]
    _atomic_write(path, buildings)


def _append_failed_log(building: dict, stage: str, error: str) -> None:
    log = []
    if os.path.exists(config.FAILED_LOG_JSON):
        with open(config.FAILED_LOG_JSON, encoding="utf-8") as f:
            log = json.load(f)
    log.append({
        "building_id": building.get("building_id", "?"),
        "slug": building.get("slug", ""),
        "stage": stage,
        "error": error[:500],
        "quarantined_at": _now_iso(),
    })
    _atomic_write(config.FAILED_LOG_JSON, log)


# ---------------------------------------------------------------------------
# Enqueue (scan JSONs, insert pending tasks for buildings missing output)
# ---------------------------------------------------------------------------

def enqueue_batch(limit: int = None) -> dict:
    """Populate tasks.db with pending enrich/analyze tasks for buildings that
    don't yet appear in the corresponding downstream JSON file.

    Idempotent: re-running with the same inputs creates no new rows (UNIQUE
    constraint on building_id + task_type + prompt_version + input_hash).
    """
    tasks_db.init()

    raw = load_json_list(config.RAW_JSON)
    enriched_ids = {b["building_id"] for b in load_json_list(config.ENRICHED_JSON)}
    analyzed_ids = {b["building_id"] for b in load_json_list(config.ANALYZED_JSON)}

    enrich_pv = enrich_prompt_version()
    enrich_added = 0
    for b in raw:
        bid = b.get("building_id")
        if not bid or bid in enriched_ids:
            continue
        input_hash = tasks_db.compute_input_hash_enrich(b)
        if tasks_db.enqueue(
            building_id=bid, task_type="enrich",
            input_hash=input_hash, prompt_version=enrich_pv,
            vocab_version=vocab.VOCAB_VERSION,
        ):
            enrich_added += 1
        if limit and enrich_added >= limit:
            break

    enriched = load_json_list(config.ENRICHED_JSON)
    analyze_added = 0
    for b in enriched:
        bid = b.get("building_id")
        if not bid or bid in analyzed_ids:
            continue
        include_atmosphere = not bool(b.get("atmosphere"))
        pv = analyze_prompt_version(include_atmosphere)
        input_hash = tasks_db.compute_input_hash_analyze(b)
        if tasks_db.enqueue(
            building_id=bid, task_type="analyze",
            input_hash=input_hash, prompt_version=pv,
            vocab_version=vocab.VOCAB_VERSION,
        ):
            analyze_added += 1
        if limit and analyze_added >= limit:
            break

    return {"enrich": enrich_added, "analyze": analyze_added}


# ---------------------------------------------------------------------------
# Worker (drain pending tasks, call API, write output)
# ---------------------------------------------------------------------------

def _handle_enrich(task: dict, raw_by_id: dict) -> bool:
    """Run one enrich task. Returns True on success (mark_done called)."""
    building = raw_by_id.get(task["building_id"])
    if not building:
        tasks_db.mark_failed(task["id"], "building not found in RAW_JSON")
        return False

    result = enrich_building(building)
    output = {
        "name_en":     result.name_en,
        "program":     result.program,
        "material":    result.material,
        "atmosphere":  result.atmosphere,
    }

    # mark_done BEFORE JSON append — if we crash between these lines,
    # the worker will not re-call the API on restart.
    tasks_db.mark_done(task["id"], output)

    enriched = dict(building)
    enriched.update(output)
    if result.material is None and building.get("material"):
        # Preserve original raw material if enrichment chose to omit it.
        enriched["material"] = building.get("material")
    enriched["vocab_version"]  = vocab.VOCAB_VERSION
    enriched["prompt_version"] = task["prompt_version"]
    _append_to_json(config.ENRICHED_JSON, enriched)
    print(f"      ✓ {result.name_en!r} / {result.program} / {result.atmosphere}")
    return True


def _handle_analyze(task: dict, enriched_by_id: dict) -> bool:
    """Run one analyze task. Returns True on success."""
    building = enriched_by_id.get(task["building_id"])
    if not building:
        # Enrich hasn't completed for this building yet; re-queue for later.
        tasks_db.mark_pending(task["id"], "blocked: awaiting enrichment")
        return False

    result = analyze_building(building)
    output = {
        "style":              result.style,
        "color_tone":         result.color_tone,
        "material_visual":    result.material_visual,
        "visual_description": result.visual_description,
        "atmosphere":         result.atmosphere,
    }
    tasks_db.mark_done(task["id"], output)

    analyzed = dict(building)
    analyzed["style"]              = result.style
    analyzed["color_tone"]         = result.color_tone
    analyzed["material_visual"]    = result.material_visual
    analyzed["visual_description"] = result.visual_description
    if result.atmosphere:
        analyzed["atmosphere"] = result.atmosphere
    analyzed["vocab_version"]  = vocab.VOCAB_VERSION
    analyzed["prompt_version"] = task["prompt_version"]
    _append_to_json(config.ANALYZED_JSON, analyzed)
    print(f"      ✓ {result.style} / {result.color_tone}")
    return True


def run_worker(limit: int = None) -> dict:
    """Drain pending tasks from the queue until empty or limit reached.

    Exponential backoff on transient failures. Permanent failures (no_images,
    missing building) mark the task as 'failed' without retry.
    """
    import anthropic as _anthropic

    tasks_db.init()

    reset = tasks_db.reset_stale(older_than_minutes=10)
    if reset:
        print(f"  Reset {reset} stale running tasks to pending")

    raw_by_id      = load_json_by_id(config.RAW_JSON)
    enriched_by_id = load_json_by_id(config.ENRICHED_JSON)

    processed = done = failed = 0

    while True:
        if limit and processed >= limit:
            break

        task = tasks_db.claim_next("enrich") or tasks_db.claim_next("analyze")
        if task is None:
            break

        processed += 1
        bid = task["building_id"]
        ttype = task["task_type"]
        attempts = task["attempts"]
        print(f"    [{ttype}] {bid} (attempt {attempts})")

        try:
            if ttype == "enrich":
                if _handle_enrich(task, raw_by_id):
                    enriched_by_id = load_json_by_id(config.ENRICHED_JSON)
                    done += 1
            else:
                if _handle_analyze(task, enriched_by_id):
                    done += 1

        except _anthropic.RateLimitError as e:
            err = f"RateLimitError: {e}"
            tasks_db.mark_pending(task["id"], err)
            print(f"      [rate limit] sleeping 60s, will retry")
            time.sleep(60)

        except ValueError as e:
            msg = str(e)
            if msg == "no_images":
                tasks_db.mark_failed(task["id"], "no_images")
                building = raw_by_id.get(bid) or enriched_by_id.get(bid) or {"building_id": bid}
                _append_failed_log(building, ttype, "no_images")
                print(f"      [quarantine] no images on disk")
                failed += 1
            elif attempts >= config.HARNESS_MAX_RETRIES:
                tasks_db.mark_failed(task["id"], msg)
                building = raw_by_id.get(bid) or enriched_by_id.get(bid) or {"building_id": bid}
                _append_failed_log(building, ttype, msg)
                print(f"      [quarantine] {msg}")
                failed += 1
            else:
                tasks_db.mark_pending(task["id"], msg)
                backoff = min(2 ** attempts, 60)
                print(f"      [retry] {msg} — sleeping {backoff}s")
                time.sleep(backoff)

        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if attempts >= config.HARNESS_MAX_RETRIES:
                tasks_db.mark_failed(task["id"], err)
                building = raw_by_id.get(bid) or enriched_by_id.get(bid) or {"building_id": bid}
                _append_failed_log(building, ttype, err)
                print(f"      [quarantine] {err}")
                failed += 1
            else:
                tasks_db.mark_pending(task["id"], err)
                backoff = min(2 ** attempts, 60)
                print(f"      [retry] {err} — sleeping {backoff}s")
                time.sleep(backoff)

    return {"processed": processed, "done": done, "failed": failed}


# ---------------------------------------------------------------------------
# QC pass — pure Python, re-enqueues failures
# ---------------------------------------------------------------------------

def run_qc() -> dict:
    """Run QC on all buildings in ANALYZED_JSON. Re-enqueue failures."""
    analyzed = load_json_list(config.ANALYZED_JSON)
    if not analyzed:
        return {"passed": 0, "requeued": 0, "quarantined": 0}

    print(f"  QC: checking {len(analyzed)} analyzed buildings...")
    passed = requeued = quarantined = 0

    for building in analyzed:
        bid = building["building_id"]
        result = check_building(building)

        if result.passed:
            passed += 1
            continue

        qc_attempts = building.get("_qc_attempts", 0) + 1
        if qc_attempts >= config.HARNESS_MAX_RETRIES:
            _append_failed_log(building, "qc", "; ".join(result.issues))
            quarantined += 1
            print(f"    {bid} ✗ QC failed {qc_attempts}x — quarantined")
            continue

        print(f"    {bid} ✗ QC failed: {'; '.join(result.issues[:2])}")

        if result.re_queue_to == "enrichment":
            _remove_from_json(config.ANALYZED_JSON, bid)
            _remove_from_json(config.ENRICHED_JSON, bid)
            # The next enqueue_batch will re-insert pending tasks.
            print(f"      → re-queued to enrichment")
        else:
            _remove_from_json(config.ANALYZED_JSON, bid)
            print(f"      → re-queued to image analysis")

        requeued += 1

    return {"passed": passed, "requeued": requeued, "quarantined": quarantined}


# ---------------------------------------------------------------------------
# Main orchestrator + status
# ---------------------------------------------------------------------------

def run_harness(limit: int = None, run_embed: bool = True,
                enqueue_only: bool = False) -> None:
    print("\n=== Pipeline Harness (queue-driven) ===\n")

    tasks_db.init()

    print("  Enqueuing pending work...")
    added = enqueue_batch(limit)
    print(f"    added: enrich={added['enrich']}  analyze={added['analyze']}")

    if enqueue_only:
        print_harness_status()
        return

    print("\n  Draining worker queue...")
    counts = run_worker(limit)
    print(f"    processed={counts['processed']}  done={counts['done']}  failed={counts['failed']}")

    print()
    qc = run_qc()
    print(f"  QC: passed={qc['passed']}  requeued={qc['requeued']}  quarantined={qc['quarantined']}")

    if qc["requeued"]:
        # One more enqueue to pick up the removed-and-now-missing buildings.
        added2 = enqueue_batch()
        if added2["enrich"] or added2["analyze"]:
            print(f"  Re-enqueued: enrich={added2['enrich']}  analyze={added2['analyze']}")

    print()
    if run_embed and qc["passed"] > 0:
        print("Running stage3_embed...")
        import stage3_embed
        stage3_embed.main()


def print_harness_status() -> None:
    tasks_db.init()

    raw_count      = len(load_json_list(config.RAW_JSON))
    enriched_count = len(load_json_list(config.ENRICHED_JSON))
    analyzed_count = len(load_json_list(config.ANALYZED_JSON))
    final_count    = len(load_json_list(config.FINAL_JSON))

    print("\n=== Harness Status ===\n")
    print(f"  1_buildings_raw.json:      {raw_count}")
    print(f"  2_buildings_enriched.json: {enriched_count}")
    print(f"  3_buildings_analyzed.json: {analyzed_count}")
    print(f"  4_buildings_final.json:    {final_count}")
    print()

    counts = tasks_db.counts_by_status()
    if not counts:
        print("  tasks.db: empty (run `run.py harness` or `run.py harness --enqueue-only`)")
        return

    print("  tasks.db:")
    by_type = {}
    for (task_type, status), cnt in counts.items():
        by_type.setdefault(task_type, {})[status] = cnt

    for task_type in sorted(by_type):
        row = by_type[task_type]
        parts = [f"{s}={row.get(s, 0)}" for s in ("pending", "running", "done", "failed")]
        print(f"    {task_type:10s} {'  '.join(parts)}")

    failed = tasks_db.list_failed(limit=5)
    if failed:
        print("\n  Recent failures:")
        for f in failed:
            print(f"    {f['building_id']} ({f['task_type']}, {f['attempts']} attempts): {f['last_error']}")
    print()


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv(os.path.join(config.BASE_DIR, ".env"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)
    run_harness()
