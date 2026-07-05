#!/usr/bin/env python3
"""R4 vision tagging: roof_type / structural_system / facade_pattern from the
display cover image, for rows the TEXT pass could not resolve.

Target set = publishable rows whose text sidecar says roof_type=Unknown OR
structural_system=Unknown (the merge policy in tools/r4_axis_merge.py decides
which source wins per axis). codex exec `-i <image>` like tools/
d2_cover_vision.py; worker/resume skeleton like tools/d1_enrich_codex.py.

Smoke ladder: --limit 10 -> --limit 100 (report measures Unknown reduction,
text-vision agreement, tok/item) -> full run. Nothing touches Neon.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.claude_cli import CLAUDE_BIN  # noqa: E402
from tools.d1_enrich_codex import FailureLedger, JsonlWriter, extract_json  # noqa: E402
from tools.image_dedup_5type import _download_to_tmp  # noqa: E402
from tools.r4_axis_merge import (  # noqa: E402
    TEXT_SIDECAR,
    VISION_AXES,
    VISION_SIDECAR,
    load_sidecar,
    normalize_axis_value,
)
from tools.r4_axis_smoke import AXES  # noqa: E402  (vocab+Unknown tuples)

FAILURES_PATH = ROOT / "data/canonical/r4_failures.vision.json"
REPORT_PATH = ROOT / "data/reports/r4_smoke/vision_report.json"
CODEX_TIMEOUT_SECONDS = 120


def build_prompt(row: dict) -> str:
    return f"""Classify the building shown in this photo for an architecture database.

Return exactly one JSON object, no Markdown:
{{
  "roof_type": one of {list(AXES["roof_type"])},
  "structural_system": one of {list(AXES["structural_system"])},
  "facade_pattern": one of {list(AXES["facade_pattern"])}
}}

Rules:
- Classify only what is visible in the photo. The roof is often invisible in
  eye-level shots — answer Unknown rather than guessing Flat.
- structural_system: infer from exposed structure/materials only when the
  photo clearly supports it; the materials hint below may corroborate but
  never overrides what you see.
- No comments, code fences, or extra keys.

Context:
name: {row["name"]}
typology: {row.get("typology_primary")}
materials (text-derived hint): {row.get("material_visual") or []}
"""


CLAUDE_VISION_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_VISION_SYS = (
    "You are a headless image classifier. Read the given image with the Read "
    "tool and emit exactly one JSON object. Never ask questions, never explain."
)


def run_codex_vision(prompt: str, image_path: Path) -> str:
    proc = subprocess.run(
        [
            "codex", "exec", "--skip-git-repo-check",
            "-c", "model=gpt-5.5",
            "-c", "model_reasoning_effort=low",
            "-c", "service_tier=fast",
            "-i", str(image_path),
            "--", prompt,
        ],
        capture_output=True,
        text=True,
        timeout=CODEX_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"codex exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


def run_claude_vision(prompt: str, image_path: Path) -> str:
    # `claude -p` reads the local image via the Read tool (renders visually).
    # Run from a neutral cwd so the repo CLAUDE.md doesn't hijack the prompt;
    # --add-dir grants the temp image location; reference the path in-prompt.
    proc = subprocess.run(
        [
            CLAUDE_BIN, "-p", "--model", CLAUDE_VISION_MODEL,
            "--allowedTools", "Read",
            "--add-dir", str(image_path.parent),
            "--append-system-prompt", CLAUDE_VISION_SYS,
            f"Read the image file {image_path}. " + prompt,
        ],
        capture_output=True,
        text=True,
        timeout=CODEX_TIMEOUT_SECONDS,
        check=False,
        cwd=tempfile.gettempdir(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


VISION_ENGINES = {"codex": run_codex_vision, "claude": run_claude_vision}


def run_batch_claude_vision(rows: list[dict]) -> list[dict]:
    """One `claude -p` call reads N cover images (Read tool) and classifies all.

    Amortizes the ~18.7k agent overhead across the batch. Returns one result
    dict per input row (download failures handled per-item). Tracks cost via
    --output-format json; cost is attached to the first ok row as `batch_cost_usd`.
    """
    import subprocess
    from pathlib import Path as _P

    downloaded = []   # (idx, row, local_path, should_delete)
    results_by_idx: dict[int, dict] = {}
    for i, r in enumerate(rows):
        base = {"canonical_bld_id": r["canonical_bld_id"], "name": r["name"],
                "engine": "claude", "elapsed_s": 0.0}
        url = r.get("display_cover_url")
        if not url:
            base["status"] = "no_cover"
            results_by_idx[i] = base
            continue
        try:
            p, dele = _download_to_tmp(url)
            downloaded.append((i, r, p, dele))
        except Exception as exc:  # noqa: BLE001
            base.update(status="download_error", error=str(exc)[:200])
            results_by_idx[i] = base
    try:
        if downloaded:
            lines = "\n".join(
                f'Image {i}: {p} (name="{r["name"]}", '
                f'typology={r.get("typology_primary")}, '
                f'materials={r.get("material_visual") or []})'
                for (i, r, p, _d) in downloaded
            )
            prompt = (
                f"Read each of these building images with the Read tool, then "
                f"classify each. Per image choose: roof_type "
                f"{list(AXES['roof_type'])}; structural_system "
                f"{list(AXES['structural_system'])}; facade_pattern "
                f"{list(AXES['facade_pattern'])}. Classify only what is visible; "
                f"the roof is often invisible at eye level -> Unknown. Output ONLY "
                f"a JSON array, one object per image, keyed by its Image index: "
                f'[{{"i":<idx>,"roof_type":..,"structural_system":..,'
                f'"facade_pattern":..}}, ...]\n\n{lines}'
            )
            add_dirs = sorted({str(_P(p).parent) for (_i, _r, p, _d) in downloaded})
            cmd = [CLAUDE_BIN, "-p", "--model", CLAUDE_VISION_MODEL,
                   "--output-format", "json", "--allowedTools", "Read"]
            for d in add_dirs:
                cmd += ["--add-dir", d]
            cmd += ["--append-system-prompt", CLAUDE_VISION_SYS, prompt]
            t0 = time.time()
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=600, check=False,
                                  cwd=tempfile.gettempdir())
            elapsed = round((time.time() - t0) / max(1, len(downloaded)), 2)
            parsed, cost = {}, 0.0
            try:
                envelope = json.loads(proc.stdout)
                cost = float(envelope.get("total_cost_usd") or 0.0)
                res = envelope.get("result", "")
                arr = json.loads(res[res.find("["): res.rfind("]") + 1])
                for obj in arr:
                    if isinstance(obj, dict) and "i" in obj:
                        parsed[int(obj["i"])] = obj
            except Exception as exc:  # noqa: BLE001
                for (i, r, _p, _d) in downloaded:
                    results_by_idx[i] = {**{"canonical_bld_id": r["canonical_bld_id"],
                                            "name": r["name"], "engine": "claude"},
                                         "status": "parse_error", "error": str(exc)[:200],
                                         "elapsed_s": elapsed}
            cost_attached = False
            for (i, r, _p, _d) in downloaded:
                if i in results_by_idx:
                    continue
                base = {"canonical_bld_id": r["canonical_bld_id"], "name": r["name"],
                        "engine": "claude", "elapsed_s": elapsed}
                obj = parsed.get(i)
                if obj is None:
                    base.update(status="parse_error", error="missing index in batch reply")
                else:
                    tags = {axis: obj.get(axis) for axis in VISION_AXES}
                    errs = validate(tags)
                    if errs:
                        base.update(status="vocab_error", error="; ".join(errs))
                    else:
                        base.update(status="ok", tags=tags, attempts=1)
                if not cost_attached and base.get("status") == "ok":
                    base["batch_cost_usd"] = round(cost, 4)
                    cost_attached = True
                results_by_idx[i] = base
    finally:
        for (_i, _r, p, dele) in downloaded:
            if dele:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
    return [results_by_idx[i] for i in range(len(rows))]


def validate(obj: dict) -> list[str]:
    errors = []
    for axis in VISION_AXES:
        if obj.get(axis) not in AXES[axis]:
            errors.append(f"{axis}={obj.get(axis)!r} not in vocab")
    return errors


def run_one(row: dict, engine: str = "codex") -> dict:
    t0 = time.time()
    out: dict = {"canonical_bld_id": row["canonical_bld_id"], "name": row["name"],
                 "engine": engine}
    url = row.get("display_cover_url")
    if not url:
        out.update(status="no_cover", elapsed_s=0.0)
        return out
    try:
        image_path, should_delete = _download_to_tmp(url)
    except Exception as exc:  # noqa: BLE001
        out.update(status="download_error", error=str(exc)[:200],
                   elapsed_s=round(time.time() - t0, 2))
        return out
    call = VISION_ENGINES[engine]
    try:
        prompt = build_prompt(row)
        out["prompt_chars"] = len(prompt)
        for attempt in (1, 2):
            try:
                stdout = call(prompt, image_path)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                out.update(status="codex_error", error=str(exc)[:300])
                continue
            out["output_chars"] = len(stdout)
            try:
                obj = extract_json(stdout)
            except Exception as exc:  # noqa: BLE001
                out.update(status="parse_error", error=str(exc)[:200])
                continue
            errors = validate(obj)
            if errors:
                out.update(status="vocab_error", error="; ".join(errors))
                continue
            out.update(status="ok", tags={axis: obj[axis] for axis in VISION_AXES},
                       attempts=attempt)
            break
    finally:
        if should_delete:
            try:
                image_path.unlink()
            except FileNotFoundError:
                pass
    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


def fetch_targets() -> list[dict]:
    """Publishable rows whose TEXT result left roof or structural unresolved."""
    text = load_sidecar(TEXT_SIDECAR)
    target_cids = sorted(
        cid for cid, tags in text.items()
        if tags.get("roof_type") is None or tags.get("structural_system") is None
    )
    if not target_cids:
        return []
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT canonical_bld_id, name, typology_primary, material_visual,
               display_cover_url
        FROM canonical_v2_buildings
        WHERE is_publishable AND canonical_bld_id = ANY(%s)
        ORDER BY canonical_bld_id
        """,
        (target_cids,),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.rollback()
    conn.close()
    return rows


def read_done_cids(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") in ("ok", "no_cover") and row.get("canonical_bld_id"):
                done.add(row["canonical_bld_id"])
    return done


def build_report(n_this_run: int, status_counts: Counter, elapsed_s: float) -> dict:
    """Aggregate the whole vision sidecar + agreement vs text."""
    text = load_sidecar(TEXT_SIDECAR)
    dist: dict[str, Counter] = {axis: Counter() for axis in VISION_AXES}
    agree = Counter()
    both = Counter()
    resolved = Counter()
    n_ok = 0
    prompt_chars = output_chars = 0
    with VISION_SIDECAR.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") != "ok":
                continue
            n_ok += 1
            prompt_chars += r.get("prompt_chars", 0)
            output_chars += r.get("output_chars", 0)
            tags = r.get("tags") or {}
            text_tags = text.get(r["canonical_bld_id"]) or {}
            for axis in VISION_AXES:
                raw = tags.get(axis)
                dist[axis][raw or "Unknown"] += 1
                v = normalize_axis_value(axis, raw)
                t = text_tags.get(axis)
                if v is not None and t is None:
                    resolved[axis] += 1
                if v is not None and t is not None:
                    both[axis] += 1
                    if v == t:
                        agree[axis] += 1
    return {
        "mode": "r4-vision",
        "generated": datetime.now(timezone.utc).isoformat(),
        "processed_this_run": dict(status_counts),
        "n_this_run": n_this_run,
        "ok_total_in_sidecar": n_ok,
        "elapsed_min_this_run": round(elapsed_s / 60, 1),
        "distributions": {axis: dict(c.most_common()) for axis, c in dist.items()},
        "unknown_rate_vision": {
            axis: round(dist[axis].get("Unknown", 0) / n_ok, 3) if n_ok else None
            for axis in VISION_AXES
        },
        "resolved_text_unknowns": {axis: resolved.get(axis, 0) for axis in VISION_AXES},
        "text_vision_agreement": {
            axis: round(agree[axis] / both[axis], 3) if both.get(axis) else None
            for axis in VISION_AXES
        },
        "est_tokens_per_item": {
            # text side only — image input tokens are NOT visible here; scale
            # the projection from the D-2 history (~3,738 tok/cid total).
            "prompt_chars/4": round(prompt_chars / 4 / n_ok) if n_ok else None,
            "output_chars/4": round(output_chars / 4 / n_ok) if n_ok else None,
            "d2_history_total_per_item": 3738,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--limit", type=int, help="cap pending items (smoke ladder)")
    ap.add_argument("--engine", choices=("codex", "claude"), default="codex",
                    help="claude = headless `claude -p` Haiku vision fallback")
    ap.add_argument("--batch", type=int, default=1,
                    help="claude only: images per call (amortizes agent overhead)")
    ap.add_argument("--max-cost-usd", type=float, default=None,
                    help="claude only: stop after cumulative batch cost reaches this "
                         "(subscription-quota guard; resume-safe)")
    ap.add_argument("--abort-streak", type=int, default=30,
                    help="abort after N consecutive failures (quota-exhaustion guard)")
    ap.add_argument("--count", action="store_true",
                    help="print pending target count and exit (supervisor probe)")
    args = ap.parse_args()

    targets = fetch_targets()
    done = read_done_cids(VISION_SIDECAR)
    pending = [r for r in targets if r["canonical_bld_id"] not in done]
    if args.count:
        print(len(pending))
        return 0
    if args.limit:
        pending = pending[: args.limit]
    print(f"targets={len(targets)} done={len(done)} pending={len(pending)} "
          f"workers={args.workers}", file=sys.stderr)

    VISION_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    writer = JsonlWriter(VISION_SIDECAR)
    ledger = FailureLedger(FAILURES_PATH)
    status_counts: Counter[str] = Counter()
    t0 = time.time()
    n_done = 0

    batched = args.engine == "claude" and args.batch > 1
    if batched:
        units = [pending[i:i + args.batch] for i in range(0, len(pending), args.batch)]
        def work(unit):
            return run_batch_claude_vision(unit)
    else:
        units = [[r] for r in pending]
        def work(unit):
            return [run_one(unit[0], args.engine)]

    fail_streak = 0
    aborted = False
    cost_usd = 0.0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, unit): unit for unit in units}
        for future in as_completed(futures):
            unit = futures[future]
            if future.cancelled():
                continue
            try:
                results = future.result()
            except Exception as exc:  # noqa: BLE001
                results = [{"canonical_bld_id": r["canonical_bld_id"],
                            "status": "worker_error", "error": str(exc)[:300]}
                           for r in unit]
            for result in results:
                cid = result["canonical_bld_id"]
                cost_usd += float(result.pop("batch_cost_usd", 0.0) or 0.0)
                # download_error is image-host flakiness, not quota — don't count it
                if result["status"] in ("ok", "no_cover"):
                    fail_streak = 0
                elif result["status"] != "download_error":
                    fail_streak += 1
                    if fail_streak >= args.abort_streak and not aborted:
                        aborted = True
                        print(f"ABORT: {fail_streak} consecutive failures — engine "
                              f"quota likely exhausted (resume-safe)", file=sys.stderr)
                        for f in futures:
                            f.cancel()
                writer.append(result)
                status_counts[result["status"]] += 1
                try:
                    if result["status"] in ("ok", "no_cover"):
                        ledger.clear_cid(cid)
                    else:
                        ledger.append({"cid": cid, "error": result.get("error", ""),
                                       "ts": datetime.now(timezone.utc).isoformat()})
                except OSError:
                    pass
                n_done += 1
            # subscription-quota guard: stop cleanly once the budget is reached
            if args.max_cost_usd and cost_usd >= args.max_cost_usd and not aborted:
                aborted = True
                print(f"BUDGET STOP: ${cost_usd:.2f} >= --max-cost-usd "
                      f"${args.max_cost_usd} (resume-safe)", file=sys.stderr)
                for f in futures:
                    f.cancel()
            if n_done % 100 < len(results) or n_done == len(pending):
                elapsed = time.time() - t0
                rate = n_done / elapsed * 60 if elapsed else 0
                eta_min = (len(pending) - n_done) / rate if rate else 0
                print(f"  {n_done}/{len(pending)} {dict(status_counts)} "
                      f"{rate:.0f}/min ${cost_usd:.2f} eta {eta_min:.0f}min", file=sys.stderr)

    report = build_report(len(pending), status_counts, time.time() - t0)
    report["claude_cost_usd_this_run"] = round(cost_usd, 2)
    report["budget_stopped"] = bool(args.max_cost_usd and cost_usd >= args.max_cost_usd)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if status_counts.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
