"""Phase 14b (2/2) — Agent-driven enrich orchestration.

Replaces the API-based enrich/harness.py for users on Claude Max
subscription (no per-token cost). Designed to be invoked from inside
a Claude Code session — the orchestrator (Claude) loads the canonical,
chunks pending rows into batches, dispatches `batch-enricher` subagents,
and merges results back.

This module exposes pure-Python helpers; the actual agent dispatch
happens via the orchestrator's Agent tool. There is no SDK call here.

Typical session flow:

    >>> from enrich import harness_agent as h
    >>> pending = h.list_pending_rows()
    >>> print(f"{len(pending)} pending → {h.estimate_batches(pending, batch=25)} batches")
    >>> # for each batch:
    >>> input_path, output_path, rows = h.write_batch(pending[:25], batch_idx=0)
    >>> # orchestrator dispatches Agent(subagent_type='batch-enricher',
    >>> #     prompt=f"Process input_path={input_path} → output_path={output_path}")
    >>> # then:
    >>> updated = h.apply_batch_output(output_path)
    >>> # write updated canonical:
    >>> h.save_canonical(updated)

For multi-batch parallelism, the orchestrator dispatches several
agents concurrently; each writes to its own output_path. Then
apply_batch_output is called for each in turn.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from core import config

CANONICAL_PATH = os.path.join(
    config.DATA_DIR, "canonical", "canonical_buildings_strict.json"
)

# Per-batch input/output staging area (gitignored via data/)
BATCH_STAGE_DIR = os.path.join(config.DATA_DIR, "canonical", "_batch_stage")

# Fields the agent is expected to fill / overwrite per row.
# `None` from the agent means "leave unchanged" — useful when image
# analysis is skipped (no cover_image_url).
_AGENT_OUTPUT_FIELDS = (
    "name_en",
    "program",
    "material",
    "atmosphere",
    "style",
    "color_tone",
    "material_visual",
    "visual_description",
)

# Fields the agent NEEDS as input. We trim to these to keep batch
# input files small (canonical rows have many fields the agent
# doesn't need to see).
_AGENT_INPUT_FIELDS = (
    "metalocus_building_id",
    "name",
    "architect_names",
    "location_country",
    "location_city",
    "project_year",
    "description_per_source",
    "cover_image_url",
)


# ---------------------------------------------------------------------------
# Pending-row identification
# ---------------------------------------------------------------------------

def _has_enrichment(row: dict) -> bool:
    """A row is considered enriched if it has both vocab-validated
    program AND at least one of the image-derived fields. Permissive on
    purpose — rows that already have any enrich content shouldn't be
    re-enriched (cost waste)."""
    if not row.get("program"):
        return False
    image_signals = (row.get("style"), row.get("color_tone"),
                     row.get("material_visual"), row.get("visual_description"))
    return any(image_signals)


def load_canonical(path: str = CANONICAL_PATH) -> list[dict]:
    """Load the canonical artefact. Returns a list (mutable in place)."""
    with open(path) as f:
        return json.load(f)


def save_canonical(rows: list[dict], path: str = CANONICAL_PATH) -> None:
    """Atomic write back to the canonical artefact."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, path)


def list_pending_rows(
    rows: Optional[list[dict]] = None,
    *,
    force_all: bool = False,
    only_tier: Optional[str] = None,
) -> list[dict]:
    """Return the subset of canonical rows that need enrichment.

    Args:
      rows: canonical list (loaded if None)
      force_all: ignore _has_enrichment check; return EVERY row.
        Use when re-enriching to apply Decision D2 (concat all-source
        descriptions) on rows that already have legacy enrich values
        from the per-source pipeline.
      only_tier: if set ('T1'|'T2'|'T3'), restrict to rows of that tier
        — e.g. only_tier='T1' to re-enrich only the cross-source-confirmed
        rows (cheaper test of the agent path).
    """
    if rows is None:
        rows = load_canonical()
    candidates = rows if force_all else [r for r in rows if not _has_enrichment(r)]
    if only_tier:
        candidates = [r for r in candidates if r.get("confidence_tier") == only_tier]
    return candidates


def estimate_batches(pending: list[dict], batch: int = 25) -> int:
    return (len(pending) + batch - 1) // batch


# ---------------------------------------------------------------------------
# Batch staging
# ---------------------------------------------------------------------------

def _ensure_stage_dir() -> None:
    os.makedirs(BATCH_STAGE_DIR, exist_ok=True)


def _trim_input_row(row: dict) -> dict:
    """Strip the row down to the fields the agent needs."""
    return {k: row.get(k) for k in _AGENT_INPUT_FIELDS}


def write_batch(rows: list[dict], batch_idx: int) -> tuple[str, str, list[dict]]:
    """Write batch input + reserve output path. Returns
    (input_path, output_path, trimmed_rows). The orchestrator passes
    the two paths to the batch-enricher agent."""
    _ensure_stage_dir()
    timestamp = int(time.time())
    input_path = os.path.join(
        BATCH_STAGE_DIR, f"batch_{batch_idx:04d}_{timestamp}_in.json"
    )
    output_path = os.path.join(
        BATCH_STAGE_DIR, f"batch_{batch_idx:04d}_{timestamp}_out.json"
    )
    trimmed = [_trim_input_row(r) for r in rows]
    with open(input_path, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)
    return input_path, output_path, trimmed


def chunk_pending(pending: list[dict], batch_size: int = 25) -> list[list[dict]]:
    return [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]


# ---------------------------------------------------------------------------
# Apply batch output back to canonical
# ---------------------------------------------------------------------------

def apply_batch_output(
    output_path: str,
    canonical: Optional[list[dict]] = None,
    canonical_path: str = CANONICAL_PATH,
) -> tuple[list[dict], dict]:
    """Read agent output, apply field updates to the canonical rows.
    Returns (updated_canonical, stats_dict).

    stats_dict keys: applied, missing, validation_errors.
    """
    from core import vocab

    with open(output_path) as f:
        agent_output = json.load(f)

    if canonical is None:
        canonical = load_canonical(canonical_path)

    by_id = {r.get("metalocus_building_id"): r for r in canonical
             if r.get("metalocus_building_id")}

    applied = 0
    missing = []
    validation_errors = []
    for out_row in agent_output:
        bid = out_row.get("metalocus_building_id")
        if not bid or bid not in by_id:
            missing.append(bid)
            continue

        target = by_id[bid]
        for field in _AGENT_OUTPUT_FIELDS:
            value = out_row.get(field)
            if value is None or value == "":
                continue
            # Vocab validation for vocab fields (best-effort; agent
            # might emit subtly invalid values)
            if field in ("program", "atmosphere", "style", "color_tone"):
                if not vocab.is_valid(field, value):
                    validation_errors.append((bid, field, value))
                    continue
            target[field] = value
        target["vocab_version"] = vocab.VOCAB_VERSION
        target["prompt_version"] = "agent-v1"
        applied += 1

    return canonical, {
        "applied": applied,
        "missing": missing,
        "validation_errors": validation_errors,
    }


# ---------------------------------------------------------------------------
# Convenience: full status snapshot
# ---------------------------------------------------------------------------

def status_snapshot(rows: Optional[list[dict]] = None) -> dict:
    rows = rows or load_canonical()
    pending = list_pending_rows(rows)
    enriched = len(rows) - len(pending)
    return {
        "total_rows": len(rows),
        "enriched": enriched,
        "pending": len(pending),
        "estimated_batches_25": estimate_batches(pending, 25),
        "estimated_batches_50": estimate_batches(pending, 50),
    }


# ---------------------------------------------------------------------------
# Stage cleanup
# ---------------------------------------------------------------------------

def cleanup_stage(keep_n: int = 0) -> int:
    """Remove batch stage files older than the most recent `keep_n`
    pairs. Returns count deleted. Pass 0 to wipe everything."""
    if not os.path.isdir(BATCH_STAGE_DIR):
        return 0
    files = sorted(os.listdir(BATCH_STAGE_DIR), reverse=True)
    deleted = 0
    if keep_n == 0:
        for f in files:
            os.remove(os.path.join(BATCH_STAGE_DIR, f))
            deleted += 1
    else:
        # Pairs (in/out) per batch_idx; keep the newest keep_n batch indices
        kept_idx = set()
        for f in files:
            # batch_NNNN_<timestamp>_<in|out>.json
            try:
                idx = f.split("_")[1]
                kept_idx.add(idx)
                if len(kept_idx) > keep_n:
                    os.remove(os.path.join(BATCH_STAGE_DIR, f))
                    deleted += 1
            except (IndexError, OSError):
                continue
    return deleted


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """python3 -m enrich.harness_agent --status
    Prints the pending count + batch estimate. Does not call any agent."""
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--status", action="store_true",
                   help="show pending count + batch estimate")
    p.add_argument("--canonical", default=CANONICAL_PATH,
                   help="canonical JSON path")
    p.add_argument("--cleanup", action="store_true",
                   help="wipe data/canonical/_batch_stage/")
    p.add_argument("--force-all", action="store_true",
                   help="show what --force-all would queue (Decision D3 reprocess)")
    p.add_argument("--only-tier", choices=["T1", "T2", "T3"],
                   help="restrict status to a specific confidence tier")
    args = p.parse_args(argv)

    if args.cleanup:
        n = cleanup_stage(0)
        print(f"deleted {n} batch stage files")
        return 0

    rows = load_canonical(args.canonical)
    if args.force_all or args.only_tier:
        candidates = list_pending_rows(rows, force_all=args.force_all,
                                       only_tier=args.only_tier)
        print(json.dumps({
            "force_all": args.force_all,
            "only_tier": args.only_tier,
            "candidate_rows": len(candidates),
            "estimated_batches_25": estimate_batches(candidates, 25),
            "estimated_batches_50": estimate_batches(candidates, 50),
        }, indent=2))
    else:
        snap = status_snapshot(rows)
        print(json.dumps(snap, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
