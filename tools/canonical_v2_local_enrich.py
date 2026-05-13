#!/usr/bin/env python3
"""Local batch enrichment for code-name split repair CIDs.

No cmux dispatch. No silent fallback. D-1 uses text-only codex exec batches.
D-2/E-2 download images first, attach them to codex exec, and fail the batch if
any download or parse step fails.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import dispatch_enrich_batch as dispatch  # noqa: E402
from tools.image_dedup_5type import IMAGE_TYPES  # noqa: E402


REFRESH_DIR = ROOT / "data/canonical/code_name_split_refresh"
DEFAULT_AFFECTED = REFRESH_DIR / "affected_cids.json"
DEFAULT_CANONICAL = ROOT / "data/canonical/canonical_buildings_4source.json"
DEFAULT_E1 = REFRESH_DIR / "e1_clusters.patched.jsonl"
DEFAULT_OUTPUTS = {
    "d1": REFRESH_DIR / "d1_results.patched.jsonl",
    "e2": REFRESH_DIR / "e2_image_types.patched.jsonl",
    "d2": REFRESH_DIR / "d2_results.patched.jsonl",
}
DEFAULT_FAILURES = {
    "d1": REFRESH_DIR / "d1_failures.local.jsonl",
    "e2": REFRESH_DIR / "e2_failures.local.jsonl",
    "d2": REFRESH_DIR / "d2_failures.local.jsonl",
}
DEFAULT_METRICS = REFRESH_DIR / "local_enrich_metrics.jsonl"


def _load_affected(path: Path = DEFAULT_AFFECTED) -> set[str]:
    data = json.load(path.open())
    return {str(cid) for cid in data.get("affected_cids") or [] if str(cid)}


def _append_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()


def _chunked(rows: Sequence[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size < 1:
        raise ValueError("batch size must be >= 1")
    return [list(rows[idx : idx + size]) for idx in range(0, len(rows), size)]


def build_pending_records(
    stage: str,
    *,
    affected_cids: set[str],
    output_path: Path,
    canonical_path: Path = DEFAULT_CANONICAL,
    e1_path: Path = DEFAULT_E1,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    done = dispatch.read_done_cids(output_path)
    if stage == "d1":
        rows = dispatch.build_d1_records(canonical_path=canonical_path, done_cids=done)
    elif stage == "d2":
        rows = dispatch.build_d2_records(e1_path=e1_path, done_cids=done)
    elif stage == "e2":
        rows = dispatch.build_e2_records(e1_path=e1_path, done_cids=done)
    else:
        raise ValueError(f"unknown stage: {stage}")
    rows = [row for row in rows if str(row.get("cid")) in affected_cids]
    return rows[:limit] if limit is not None else rows


def _model_meta(model: str, reasoning: str, service_tier: str) -> dispatch.ModelMeta:
    return dispatch.ModelMeta(model=model, reasoning=reasoning, fast=service_tier)


def _codex_exec_args(model_meta: dispatch.ModelMeta) -> list[str]:
    args: list[str] = ["codex", "exec", "--json", "--skip-git-repo-check"]
    if model_meta.model:
        args.extend(["-m", model_meta.model])
    if model_meta.reasoning:
        args.extend(["-c", f"model_reasoning_effort={model_meta.reasoning}"])
    if model_meta.fast:
        args.extend(["-c", f"service_tier={model_meta.fast}"])
    return args


def run_d1_batch(
    batch: list[dict[str, Any]],
    *,
    model_meta: dispatch.ModelMeta,
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dispatch.PollResult:
    prompt = dispatch.compose_prompt("d1", batch)
    cmd = [*_codex_exec_args(model_meta), "--", prompt]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        raw = "\n".join(str(part or "") for part in (exc.stdout, exc.stderr, exc))
        return dispatch.PollResult(rows=None, raw=raw, timed_out=True, failure_reason="timeout: codex exec")
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    raw_events = stdout or stderr
    if proc.returncode:
        reason = f"codex_exec_failed: returncode={proc.returncode} stderr={stderr or stdout or ''}".rstrip()
        return dispatch.PollResult(rows=None, raw=raw_events, failure_reason=reason, returncode=proc.returncode, stderr=stderr)
    final_message, _ = dispatch.parse_codex_exec_json_output(raw_events)
    usage_line = dispatch._codex_exec_usage_line_from_json(raw_events)
    raw = "\n".join(part for part in (final_message, usage_line, raw_events) if part).strip()
    expected = tuple(str(row["cid"]) for row in batch)
    rows = dispatch.extract_json_array(raw, must_have_keys=dispatch._STAGE_RESPONSE_KEYS["d1"], expected_cids=expected)
    if rows is None:
        return dispatch.PollResult(rows=None, raw=raw, failure_reason="parse_failed: response did not contain a valid JSON array")
    return dispatch.PollResult(rows=rows, raw=raw, returncode=proc.returncode, stderr=stderr)


def compose_e2_vision_prompt(batch: list[dict[str, Any]], image_mapping: list[dict[str, Any]]) -> str:
    payload = {
        "rows": [
            {"cid": row["cid"], "candidate_count": len(row.get("candidates") or [])}
            for row in batch
        ],
        "image_mapping": image_mapping,
    }
    return f"""Classify attached architectural images into cover image types.
Output ONLY a JSON array of {len(batch)} objects, no Markdown fences, no prose.
Each output object:
{{"cid": "...", "covers_by_type": {{"exterior": url|null, "interior": url|null, "drawing": url|null, "aerial": url|null, "detail": url|null}}}}

Rules:
- Return exactly one object for every cid.
- Use only attached image pixels plus URL/kind hints in mapping.
- Each non-null covers_by_type value must be one of that cid's candidate URLs.
- Prefer best representative image per type. If no image matches a type, use null.
- Do not fabricate. If every image for a cid is unusable, return all null values for that cid.

Image mapping JSON:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def run_e2_vision_batch(
    batch: list[dict[str, Any]],
    *,
    model_meta: dispatch.ModelMeta,
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    downloader: Callable[[str], tuple[Any, bool]] = dispatch.download_image_to_tmp,
) -> dispatch.PollResult:
    image_paths: list[Path] = []
    delete_paths: list[Path] = []
    image_mapping: list[dict[str, Any]] = []
    allowed_by_cid: dict[str, set[str]] = {}
    try:
        image_index = 0
        for row in batch:
            cid = str(row.get("cid"))
            candidates = [c for c in row.get("candidates") or [] if c.get("url")]
            if not candidates:
                reason = f"download_failed: cid={cid} missing candidates"
                return dispatch.PollResult(rows=None, raw=reason, failure_reason=reason)
            allowed_by_cid[cid] = {str(c["url"]) for c in candidates}
            for candidate in candidates:
                url = str(candidate["url"])
                try:
                    image_path, should_delete = downloader(url)
                except Exception as exc:
                    reason = f"download_failed: cid={cid} url={url} {exc.__class__.__name__}: {exc}"
                    return dispatch.PollResult(rows=None, raw=reason, failure_reason=reason)
                image_index += 1
                image_paths.append(Path(image_path))
                if should_delete:
                    delete_paths.append(Path(image_path))
                image_mapping.append(
                    {
                        "image_index": image_index,
                        "cid": cid,
                        "url": url,
                        "cluster_id": candidate.get("cluster_id"),
                        "source": candidate.get("source"),
                        "kind": candidate.get("kind"),
                        "rank": candidate.get("rank"),
                    }
                )

        prompt = compose_e2_vision_prompt(batch, image_mapping)
        cmd = _codex_exec_args(model_meta)
        for image_path in image_paths:
            cmd.extend(["-i", str(image_path)])
        cmd.extend(["--", prompt])
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=False)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        raw_events = stdout or stderr
        if proc.returncode:
            reason = f"codex_exec_failed: returncode={proc.returncode} stderr={stderr or stdout or ''}".rstrip()
            return dispatch.PollResult(rows=None, raw=raw_events, failure_reason=reason, returncode=proc.returncode, stderr=stderr)
        final_message, _ = dispatch.parse_codex_exec_json_output(raw_events)
        usage_line = dispatch._codex_exec_usage_line_from_json(raw_events)
        raw = "\n".join(part for part in (final_message, usage_line, raw_events) if part).strip()
        expected = tuple(str(row["cid"]) for row in batch)
        rows = dispatch.extract_json_array(raw, must_have_keys=dispatch._STAGE_RESPONSE_KEYS["e2"], expected_cids=expected)
        if rows is None:
            return dispatch.PollResult(rows=None, raw=raw, failure_reason="parse_failed: response did not contain a valid JSON array")
        normalized, error = dispatch.validate_batch("e2", list(expected), rows)
        if error:
            return dispatch.PollResult(rows=None, raw=raw, failure_reason=error)
        for row in normalized:
            cid = str(row["cid"])
            for image_type in IMAGE_TYPES:
                value = row["covers_by_type"].get(image_type)
                if value is not None and value not in allowed_by_cid.get(cid, set()):
                    reason = f"{cid}: covers_by_type[{image_type}] not in candidates"
                    return dispatch.PollResult(rows=None, raw=raw, failure_reason=reason)
        return dispatch.PollResult(rows=normalized, raw=raw, returncode=proc.returncode, stderr=stderr)
    except subprocess.TimeoutExpired as exc:
        raw = "\n".join(str(part or "") for part in (exc.stdout, exc.stderr, exc))
        return dispatch.PollResult(rows=None, raw=raw, timed_out=True, failure_reason="timeout: codex exec")
    finally:
        for image_path in delete_paths:
            try:
                image_path.unlink()
            except FileNotFoundError:
                pass


def run_stage(
    stage: str,
    *,
    rows: list[dict[str, Any]],
    output_path: Path,
    failure_path: Path,
    metrics_path: Path,
    batch_size: int,
    model_meta: dispatch.ModelMeta,
    timeout_seconds: int,
    max_batches: int | None = None,
) -> dict[str, Any]:
    summary = {"stage": stage, "pending": len(rows), "batches": 0, "written": 0, "failures": 0}
    for batch in _chunked(rows, batch_size):
        if max_batches is not None and summary["batches"] >= max_batches:
            break
        remaining_batch = list(batch)

        summary["batches"] += 1
        while remaining_batch:
            expected = [str(row["cid"]) for row in remaining_batch]
            attempts = 2 if stage == "d2" else 1
            normalized = []
            error = None
            result: dispatch.PollResult | None = None
            for attempt in range(1, attempts + 1):
                started = time.monotonic()
                retry_note = error if attempt > 1 else None
                if stage == "d1":
                    result = run_d1_batch(remaining_batch, model_meta=model_meta, timeout_seconds=timeout_seconds)
                elif stage == "d2":
                    result = dispatch.run_d2_vision_batch(
                        remaining_batch,
                        model_meta=model_meta,
                        timeout_seconds=timeout_seconds,
                        retry_note=retry_note,
                    )
                elif stage == "e2":
                    result = run_e2_vision_batch(remaining_batch, model_meta=model_meta, timeout_seconds=timeout_seconds)
                else:
                    raise ValueError(f"unknown stage: {stage}")

                normalized = []
                error = result.failure_reason
                if result.rows is not None:
                    normalized, error = dispatch.validate_batch(stage, expected, result.rows)

                metric = {
                    "stage": stage,
                    "cids": expected,
                    "attempt": attempt,
                    "success": result.rows is not None and error is None,
                    "failure_reason": error,
                    "wallclock_s": round(time.monotonic() - started, 3),
                    "tokens": None,
                }
                usage = dispatch.parse_token_usage(result.raw)
                if usage:
                    metric["tokens"] = {"total": usage.total, "input": usage.input, "output": usage.output}
                _append_jsonl(metrics_path, [metric])

                if error is None:
                    break

            if error is None:
                _append_jsonl(output_path, normalized)
                summary["written"] += len(normalized)
                break

            bad_cid = _download_failed_cid(error)
            if stage == "d2" and bad_cid and bad_cid in expected:
                raw = result.raw if result is not None else ""
                dispatch.log_failure(failure_path, cids=[bad_cid], reason=error, raw=raw)
                summary["failures"] += 1
                remaining_batch = [row for row in remaining_batch if str(row["cid"]) != bad_cid]
                continue

            summary["failures"] += len(remaining_batch)
            raw = result.raw if result is not None else ""
            dispatch.log_failure(failure_path, cids=expected, reason=error, raw=raw)
            return summary
    return summary


def _download_failed_cid(reason: str | None) -> str | None:
    if not reason or "download_failed:" not in reason or "all cover candidates failed" not in reason:
        return None
    match = re.search(r"cid=([A-Za-z0-9_-]+)", reason)
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local batch enrichment for code split affected CIDs.")
    parser.add_argument("stage", choices=("d1", "d2", "e2"))
    parser.add_argument("--affected", type=Path, default=DEFAULT_AFFECTED)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--e1", type=Path, default=DEFAULT_E1)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--failures", type=Path, default=None)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--reasoning", default="low")
    parser.add_argument("--service-tier", default="fast")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    output_path = args.output or DEFAULT_OUTPUTS[args.stage]
    failure_path = args.failures or DEFAULT_FAILURES[args.stage]
    affected = _load_affected(args.affected)
    rows = build_pending_records(
        args.stage,
        affected_cids=affected,
        output_path=output_path,
        canonical_path=args.canonical,
        e1_path=args.e1,
        limit=args.limit,
    )
    estimate = {
        "stage": args.stage,
        "pending": len(rows),
        "batch_size": args.batch_size,
        "output": str(output_path),
        "mode": "dry-run" if args.dry_run else "run",
        "cost_math": (
            f"{len(rows)} cids / batch {args.batch_size}; codex exec calls ~= "
            f"{(len(rows) + args.batch_size - 1) // args.batch_size if rows else 0}; "
            "expected weekly burn << 1% for affected repair"
        ),
    }
    if args.dry_run:
        print(json.dumps({**estimate, "sample_cids": [row["cid"] for row in rows[:10]]}, indent=2, ensure_ascii=False))
        return 0

    summary = run_stage(
        args.stage,
        rows=rows,
        output_path=output_path,
        failure_path=failure_path,
        metrics_path=args.metrics,
        batch_size=args.batch_size,
        model_meta=_model_meta(args.model, args.reasoning, args.service_tier),
        timeout_seconds=args.timeout,
        max_batches=args.max_batches,
    )
    print(json.dumps({**estimate, **summary}, indent=2, ensure_ascii=False))
    return 0 if summary["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
