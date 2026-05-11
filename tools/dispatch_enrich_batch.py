"""Dispatch batched enrichment work to a live Codex cmux tab.

This replaces the expensive one-Codex-process-per-cid pattern used by the
legacy D-1/D-2/E-2 scripts. DB-MAIN runs this process; the DB-ENRICHER Codex
tab receives one prompt per batch and returns a JSON array.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from core.vocab import ATMOSPHERE, COLOR_TONE, MATERIAL_VISUAL_HINTS, PROGRAM, STYLE
from tools import d1_enrich_codex
from tools.d2_cover_vision import _load_e1_best_covers
from tools.e1_phash_dedup import DEFAULT_OUTPUT_PATH as DEFAULT_E1_PATH
from tools.e2_vision_5type import _iter_jsonl
from tools.image_dedup_5type import IMAGE_TYPES, PROJECT_ROOT
from tools.image_dedup_5type import _download_to_tmp as download_image_to_tmp


DEFAULT_CANONICAL_PATH = PROJECT_ROOT / "data" / "canonical" / "canonical_buildings_4source.json"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_POLL_INTERVAL_SECONDS = 10

STAGE_OUTPUTS = {
    "d1": PROJECT_ROOT / "data" / "canonical" / "d1_results.jsonl",
    "d2": PROJECT_ROOT / "data" / "canonical" / "d2_results.jsonl",
    "e2": PROJECT_ROOT / "data" / "canonical" / "e2_image_types.jsonl",
}

STAGE_FAILURES = {
    "d1": PROJECT_ROOT / "data" / "canonical" / "d1_failures.jsonl",
    "d2": PROJECT_ROOT / "data" / "canonical" / "d2_failures.jsonl",
    "e2": PROJECT_ROOT / "data" / "canonical" / "e2_failures.jsonl",
}

STAGE_METRICS = {
    "d1": PROJECT_ROOT / "data" / "canonical" / "d1_metrics.jsonl",
    "d2": PROJECT_ROOT / "data" / "canonical" / "d2_metrics.jsonl",
    "e2": PROJECT_ROOT / "data" / "canonical" / "e2_metrics.jsonl",
}


@dataclass(frozen=True)
class PollResult:
    rows: list[dict[str, Any]] | None
    raw: str
    usage_limit_until: datetime | None = None
    timed_out: bool = False
    failure_reason: str | None = None
    returncode: int | None = None
    stderr: str | None = None


@dataclass(frozen=True)
class TokenUsage:
    total: int
    input: int
    output: int


@dataclass(frozen=True)
class ModelMeta:
    model: str | None
    reasoning: str | None
    fast: str | None


def read_done_cids(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                print(f"[dispatch] warning: malformed resume line {line_no} in {path}", file=sys.stderr)
                continue
            if isinstance(row, dict) and row.get("cid"):
                done.add(str(row["cid"]))
    return done


def build_d1_records(
    *,
    canonical_path: Path = DEFAULT_CANONICAL_PATH,
    done_cids: set[str] | None = None,
    limit: int | None = None,
    source_records: dict[tuple[str, str], d1_enrich_codex.SourceRecord] | None = None,
) -> list[dict[str, Any]]:
    done = done_cids or set()
    clusters = d1_enrich_codex.load_clusters(canonical_path)
    records = source_records if source_records is not None else d1_enrich_codex.load_source_records()
    entries = d1_enrich_codex.build_entries(clusters, records)
    pending = [entry for entry in entries if str(entry.get("cid")) not in done]
    return pending[:limit] if limit is not None else pending


def build_d2_records(
    *,
    e1_path: Path = DEFAULT_E1_PATH,
    done_cids: set[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    done = done_cids or set()
    covers = _load_e1_best_covers(e1_path)
    rows = [
        {
            "cid": cid,
            "cover_image_url": image.get("url"),
            "cover": {
                key: image.get(key)
                for key in ("source", "source_id", "kind", "image_order", "w", "h", "bytes")
                if key in image
            },
        }
        for cid, image in sorted(covers.items())
        if cid not in done and image.get("url")
    ]
    return rows[:limit] if limit is not None else rows


def build_e2_records(
    *,
    e1_path: Path = DEFAULT_E1_PATH,
    done_cids: set[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    done = done_cids or set()
    rows: list[dict[str, Any]] = []
    for row in _iter_jsonl(e1_path):
        cid = str(row.get("cid") or "")
        if not cid or cid in done:
            continue
        # E-2 lightweight: per-cid TOP 5 candidate images (not all clusters).
        # Sort by (rank asc, image_order asc, source priority desc, area desc).
        # filename heuristic (drawing/aerial keywords) is applied codex-side
        # in the prompt — it skips Vision for those candidates.
        candidates = []
        for cluster_id, image in (row.get("best_image_per_cluster") or {}).items():
            if not isinstance(image, dict) or not image.get("url"):
                continue
            candidates.append({
                "cluster_id": str(cluster_id),
                **{
                    key: image.get(key)
                    for key in ("url", "source", "source_id", "kind", "image_order", "rank", "w", "h", "bytes")
                    if key in image
                },
            })

        def _sort_key(c: dict) -> tuple:
            area = (c.get("w") or 0) * (c.get("h") or 0)
            return (
                int(c.get("rank") or 999),
                int(c.get("image_order") or 999),
                -area,
            )

        candidates.sort(key=_sort_key)
        rows.append({"cid": cid, "candidates": candidates[:5]})
        if limit is not None and len(rows) >= limit:
            break
    return rows


def chunked(rows: Sequence[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    if size < 1:
        raise ValueError("batch size must be >= 1")
    for start in range(0, len(rows), size):
        yield list(rows[start : start + size])


def compose_prompt(stage: str, batch: list[dict[str, Any]], retry_note: str | None = None) -> str:
    payload = json.dumps(batch, ensure_ascii=False, indent=2)
    retry = f"\nPrevious response was invalid: {retry_note}\nReturn only the JSON array.\n" if retry_note else ""

    if stage == "d1":
        return f"""Process these {len(batch)} buildings text -> controlled vocab + visual description.
Output ONLY a JSON array of {len(batch)} objects, no Markdown fences, no prose.
Each object schema:
{{"cid": "...", "program": one of {sorted(PROGRAM)}, "style": one of {sorted(STYLE)}, "color_tone": one of {sorted(COLOR_TONE)}, "atmosphere": one of {sorted(ATMOSPHERE)}, "material_visual": ["..."], "visual_description": "..."}}

Rules:
- Return exactly one object for every input cid.
- Use only the listed controlled vocabulary values for program/style/color_tone/atmosphere.
- material_visual must be 1-6 lowercase material words or short phrases. Good examples: {list(MATERIAL_VISUAL_HINTS)}.
- visual_description must be 40-90 words and describe spatial character, massing, materials, and setting.
{retry}
Input JSON:
{payload}
"""

    if stage == "d2":
        return f"""Vision-analyze {len(batch)} cover images and output a JSON array.

For each input row, you must look at the actual image pixels (not just the URL string). Use your background terminal to:
1. Download each cover_image_url to a tmpfile (curl, wget, or python requests). Use a unique tmpfile path per cid (e.g. /tmp/d2_<cid>.jpg).
2. Run a single codex Vision subprocess with all {len(batch)} images attached at once:
   `codex exec --json --skip-git-repo-check -c model=gpt-5.5 -i /tmp/d2_<cid1>.jpg -i /tmp/d2_<cid2>.jpg ... -- '<vision-prompt>'`
   The vision-prompt should ask for a JSON array of {len(batch)} objects with the schema below, mapping each attached image to its cid in order.
3. Parse the codex exec stdout for the JSON array, then output that exact array in your chat reply (no other text).

Each object schema:
{{"cid": "...", "style_image": one of {sorted(STYLE)}, "color_tone_image": one of {sorted(COLOR_TONE)}, "material_visual_image": ["..."], "visual_description_image": "..."}}

Rules:
- Return exactly one object for every input cid in the SAME order.
- Use only the listed controlled vocabulary values for style_image and color_tone_image.
- material_visual_image must be 1-6 lowercase visible material words or short phrases.
- visual_description_image must be 40-90 words, present tense, describing what is actually visible in the image. NEVER fabricate; if you could not analyze the image, report a failure for that cid instead.
- Output ONLY the JSON array in your final chat reply. No file writes, no progress prose, no explanations.
- Cleanup tmpfiles after.
{retry}
Input JSON:
{payload}
"""

    if stage == "e2":
        return f"""Vision-classify candidate images into architectural image types and output a JSON array.

For each input row, look at the actual image pixels via codex Vision. Process via your background terminal:
1. For each cid, download all candidate image URLs to tmpfiles (e.g. /tmp/e2_<cid>_<idx>.jpg).
2. Run a codex Vision subprocess per cid with that cid's candidate images attached:
   `codex exec --json --skip-git-repo-check -c model=gpt-5.5 -i /tmp/e2_<cid>_0.jpg -i /tmp/e2_<cid>_1.jpg ... -- 'Classify each attached image as exterior|interior|drawing|aerial|detail. Return JSON: {{covers_by_type: {{<type>: <best matching url among candidates>}}}}'`
3. Parse output → covers_by_type dict. Skip Vision entirely for any candidate already labeled by filename heuristic (drawing/aerial keywords).

Output ONLY a JSON array in your chat reply, one object per cid:
{{"cid": "...", "covers_by_type": {{"exterior": url|null, "interior": url|null, "drawing": url|null, "aerial": url|null, "detail": url|null}}}}

Rules:
- Return exactly one object for every input cid in the SAME order.
- covers_by_type keys ∈ {list(IMAGE_TYPES)}.
- Each value is either a candidate URL (the best matching image of that type) or null if no candidate matches.
- NEVER fabricate. If a Vision call failed for a cid, return that cid with all null covers and append a 'reason' field.
- Output ONLY the JSON array in your final chat reply. Cleanup tmpfiles after.
{retry}
Input JSON:
{payload}
"""

    raise ValueError(f"unknown stage: {stage}")


def compose_d2_vision_prompt(batch: list[dict[str, Any]], retry_note: str | None = None) -> str:
    mapping = [
        {
            "image_index": idx,
            "cid": str(row.get("cid")),
            "cover_image_url": row.get("cover_image_url"),
            "cover": row.get("cover") or {},
        }
        for idx, row in enumerate(batch, 1)
    ]
    retry = f"\nPrevious response was invalid: {retry_note}\nReturn only the JSON array.\n" if retry_note else ""
    return f"""Analyze the {len(batch)} attached cover images as architecture images.
Each attached image corresponds to the same-numbered item in Image mapping JSON.
Output ONLY a JSON array of {len(batch)} objects, no Markdown fences, no prose.
Each object schema:
{{"cid": "...", "style_image": one of {sorted(STYLE)}, "color_tone_image": one of {sorted(COLOR_TONE)}, "material_visual_image": ["..."], "visual_description_image": "..."}}

Rules:
- Return exactly one object for every input cid.
- Use only the listed controlled vocabulary values for style_image and color_tone_image.
- material_visual_image must be 1-6 lowercase visible material words or short phrases.
- visual_description_image must be 40-90 words, present tense, based on the attached image.
- If the image is ambiguous, choose the closest controlled vocabulary value and describe only visible evidence.
{retry}
Image mapping JSON:
{json.dumps(mapping, ensure_ascii=False, indent=2)}
"""


def dispatch_prompt(
    tab: str,
    prompt: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    backoffs = (0.5, 1.5, 3.0)
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(len(backoffs) + 1):
        try:
            runner(["./tools/dispatch.sh", tab, prompt], capture_output=True, text=True, check=True)
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            print(
                f"[dispatch] dispatch_prompt failed attempt={attempt + 1}/{len(backoffs) + 1} "
                f"tab={tab} returncode={exc.returncode}",
                file=sys.stderr,
                flush=True,
            )
            if stdout:
                print(f"[dispatch] dispatch_prompt stdout: {stdout[-2000:]}", file=sys.stderr, flush=True)
            if stderr:
                print(f"[dispatch] dispatch_prompt stderr: {stderr[-2000:]}", file=sys.stderr, flush=True)
            if attempt < len(backoffs):
                sleeper(backoffs[attempt])
    if last_error is not None:
        raise last_error


def read_screen_raw(
    tab: str,
    *,
    lines: int = 120,
    scrollback: bool = True,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    cmd = ["./tools/poll.sh", tab, str(lines)]
    if scrollback:
        cmd.append("--scrollback")
    proc = runner(cmd, capture_output=True, text=True, check=False)
    return (proc.stdout or proc.stderr or "").strip()


def poll_screen(
    tab: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    expected_count: int | None = None,
    expected_cids: tuple[str, ...] | None = None,
    must_have_keys: tuple[str, ...] = (),
    sleeper: Callable[[float], None] = time.sleep,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> PollResult:
    deadline = time.time() + timeout_seconds
    last_raw = ""

    while time.time() < deadline:
        proc = runner(
            ["./tools/poll.sh", tab, "1200", "--scrollback"],
            capture_output=True,
            text=True,
            check=False,
        )
        raw = (proc.stdout or proc.stderr or "").strip()
        last_raw = raw

        limit_until = parse_usage_limit_until(raw)
        if limit_until:
            return PollResult(rows=None, raw=raw, usage_limit_until=limit_until)

        rows = extract_json_array(raw, must_have_keys=must_have_keys, expected_cids=expected_cids)
        # Only completion signal we trust: codex's response array of
        # expected_count rows. _looks_idle / placeholder-only / leftover
        # 'Token usage' from previous /clear all produce false-positives
        # that cause poll_screen to return prematurely with rows=None.
        # If we have the right number of rows, codex is done. Otherwise
        # keep polling until timeout.
        if rows is not None and expected_count is not None and len(rows) == expected_count:
            return PollResult(rows=rows, raw=raw)
        # Legacy path: if expected_count not provided AND idle marker is
        # present (true 'Token usage' followed by '›'), trust it.
        if expected_count is None and rows is not None and _looks_idle(raw):
            return PollResult(rows=rows, raw=raw)

        sleeper(poll_interval_seconds)

    return PollResult(rows=None, raw=last_raw, timed_out=True)


def parse_token_usage(text: str) -> TokenUsage | None:
    matches = re.findall(
        r"Token usage:\s*total=(\d+)\s+input=(\d+)(?:\s+\(\+\s*\d+\s+cached\))?\s+output=(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    if not matches:
        return None
    total, input_tokens, output = matches[-1]
    return TokenUsage(total=int(total), input=int(input_tokens), output=int(output))


def parse_model_meta(text: str) -> ModelMeta:
    matches = re.findall(r"\b(gpt-[\w.-]+)\s+(\w+)\s+(fast|slow|standard)\b", text, flags=re.IGNORECASE)
    if not matches:
        return ModelMeta(model=None, reasoning=None, fast=None)
    model, reasoning, fast = matches[-1]
    return ModelMeta(model=model, reasoning=reasoning, fast=fast)


def read_model_meta(
    tab: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[ModelMeta, TokenUsage | None, str]:
    raw = read_screen_raw(tab, runner=runner)
    return parse_model_meta(raw), parse_token_usage(raw), raw


def _codex_exec_model_args(model_meta: ModelMeta) -> list[str]:
    args: list[str] = ["-m", model_meta.model or "gpt-5.5"]
    if model_meta.reasoning:
        args.extend(["-c", f"model_reasoning_effort={model_meta.reasoning}"])
    if model_meta.fast:
        args.extend(["-c", f"service_tier={model_meta.fast}"])
    return args


def parse_codex_exec_json_output(text: str) -> tuple[Any, TokenUsage | None]:
    final_message = None
    usage = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                final_message = str(item.get("text") or "")
        elif event.get("type") == "turn.completed":
            event_usage = event.get("usage")
            if isinstance(event_usage, dict):
                input_tokens = int(event_usage.get("input_tokens") or 0)
                output_tokens = int(event_usage.get("output_tokens") or 0)
                usage = TokenUsage(total=input_tokens + output_tokens, input=input_tokens, output=output_tokens)
    return final_message, usage


def _token_usage_line(usage: TokenUsage | None, *, cached_input_tokens: int | None = None) -> str:
    if usage is None:
        return ""
    cached = f" (+ {cached_input_tokens} cached)" if cached_input_tokens else ""
    return f"Token usage: total={usage.total} input={usage.input}{cached} output={usage.output}"


def _codex_exec_usage_line_from_json(text: str) -> str:
    for line in reversed(text.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            event_usage = event.get("usage")
            if isinstance(event_usage, dict):
                input_tokens = int(event_usage.get("input_tokens") or 0)
                output_tokens = int(event_usage.get("output_tokens") or 0)
                cached = int(event_usage.get("cached_input_tokens") or 0)
                return _token_usage_line(
                    TokenUsage(total=input_tokens + output_tokens, input=input_tokens, output=output_tokens),
                    cached_input_tokens=cached,
                )
    return ""


def run_d2_vision_batch(
    batch: list[dict[str, Any]],
    *,
    model_meta: ModelMeta,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    retry_note: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    downloader: Callable[[str], tuple[Any, bool]] = download_image_to_tmp,
) -> PollResult:
    image_paths: list[Path] = []
    delete_paths: list[Path] = []
    try:
        for row in batch:
            url = str(row.get("cover_image_url") or "")
            if not url:
                reason = f"download_failed: cid={row.get('cid')} url={url} missing cover_image_url"
                return PollResult(rows=None, raw=reason, failure_reason=reason)
            try:
                image_path, should_delete = downloader(url)
            except Exception as exc:
                reason = (
                    f"download_failed: cid={row.get('cid')} url={url} "
                    f"{exc.__class__.__name__}: {exc}"
                )
                return PollResult(rows=None, raw=reason, failure_reason=reason)
            image_paths.append(image_path)
            if should_delete:
                delete_paths.append(image_path)

        prompt = compose_d2_vision_prompt(batch, retry_note=retry_note)
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", *_codex_exec_model_args(model_meta)]
        for image_path in image_paths:
            cmd.extend(["-i", str(image_path)])
        cmd.extend(["--", prompt])
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=False)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        raw_events = (stdout or stderr).strip()
        if proc.returncode and proc.returncode != 0:
            raw = "\n".join(
                part for part in (stdout, stderr, f"returncode={proc.returncode}") if part
            ).strip()
            reason = f"codex_exec_failed: returncode={proc.returncode} stderr={stderr or stdout or ''}".rstrip()
            if proc.returncode == 124:
                reason = f"timeout: codex_exec returncode=124 stderr={stderr or stdout or ''}".rstrip()
            return PollResult(
                rows=None,
                raw=raw,
                timed_out=(proc.returncode == 124),
                failure_reason=reason,
                returncode=proc.returncode,
                stderr=stderr,
            )
        final_message, _ = parse_codex_exec_json_output(raw_events)
        usage_line = _codex_exec_usage_line_from_json(raw_events)
        raw = "\n".join(part for part in (final_message, usage_line, raw_events) if part).strip()
        expected_cids = tuple(str(row["cid"]) for row in batch)
        rows = extract_json_array(
            raw,
            must_have_keys=_STAGE_RESPONSE_KEYS["d2"],
            expected_cids=expected_cids,
        )
        if rows is None:
            reason = "parse_failed: response did not contain a valid JSON array"
            return PollResult(
                rows=None,
                raw=raw,
                timed_out=False,
                failure_reason=reason,
                returncode=proc.returncode,
                stderr=stderr,
            )
        return PollResult(rows=rows, raw=raw, timed_out=False, returncode=proc.returncode, stderr=stderr)
    except subprocess.TimeoutExpired as exc:
        raw = "\n".join(part for part in (str(exc.stdout or ""), str(exc.stderr or ""), str(exc)) if part).strip()
        reason = f"timeout: subprocess exceeded {timeout_seconds}s"
        return PollResult(rows=None, raw=raw, timed_out=True, failure_reason=reason, stderr=str(exc.stderr or "").strip())
    finally:
        for image_path in delete_paths:
            try:
                image_path.unlink()
            except FileNotFoundError:
                pass


def _looks_idle(raw: str) -> bool:
    """Return True when codex has completed a response and is awaiting input.

    codex CLI prints either 'tokens used' or 'Token usage' in its footer
    after each turn (varies by session state). Both markers are followed
    by '›' once codex re-renders the input prompt. Either marker confirms
    the turn is fully complete.

    Note: the '› Implement {feature}' / '› Write tests for @filename'
    placeholder text is ALWAYS visible at the bottom of the screen, even
    during codex's processing — it cannot be used as an idle signal on
    its own. Use poll_screen's expected_count short-circuit when the
    response is a known-size JSON array."""
    lower = raw.lower()
    tokens_idx = max(lower.rfind("tokens used"), lower.rfind("token usage"))
    if tokens_idx == -1:
        return False
    return "›" in raw[tokens_idx:]


def _unwrap_terminal(text: str) -> str:
    """Collapse '\\n  ' (terminal line wraps: newline + indent) into a
    single space. cmux/codex display wraps long JSON string values across
    multiple terminal lines, breaking strict JSON parsing. Whitespace is
    legal anywhere outside JSON strings, so this is safe for the
    structural newlines too. Strings can't legally contain literal
    newlines anyway, so the only cost is loss of intentional whitespace
    formatting (which we don't care about — we only care about parsing)."""
    return re.sub(r'\n\s+', ' ', text)


def extract_json_array(
    text: str,
    must_have_keys: tuple[str, ...] = (),
    expected_cids: tuple[str, ...] | None = None,
) -> list[dict[str, Any]] | None:
    """Return the largest JSON array of cid-keyed objects embedded in text.

    The screen text typically contains BOTH the dispatched prompt's input
    array (rows have cid + primary_name + descriptions...) AND codex's
    response array (rows have cid + program + style + visual_description...).
    Plus, when batches accumulate without /clear, the scrollback contains
    PRIOR batches' responses too — without `expected_cids`, an old batch's
    full array would beat the current batch's mid-flight partial.

    Pass `must_have_keys=('program',)` for d1 / `('style_image',)` for d2
    / `('image_types',)` for e2 to filter for response-shape arrays only.

    Pass `expected_cids=(cid1, cid2, ...)` to restrict matches to arrays
    whose cid set EXACTLY equals the expected set. This prevents
    cross-batch confusion in long codex sessions.

    The terminal wraps long string values, so attempts to parse the raw
    text usually fail. We retry each candidate after _unwrap_terminal()
    collapses wrap-induced newlines.

    Among matching arrays, prefer the largest; on ties, the last one
    (codex's response is rendered after the echoed prompt)."""
    candidates: list[list[dict[str, Any]]] = []
    decoder = json.JSONDecoder()
    cleaned = _strip_markdown_fences(text)
    expected_set = set(expected_cids) if expected_cids else None

    for start, char in enumerate(cleaned):
        if char != "[":
            continue
        value: Any = None
        try:
            value, _ = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError:
            # Try with terminal-wrap unwrap
            chunk = cleaned[start:start + 80000]
            unwrapped = _unwrap_terminal(chunk)
            block = _balanced_array(unwrapped)
            if block is None:
                continue
            try:
                value = json.loads(re.sub(r",\s*([}\]])", r"\1", block))
            except json.JSONDecodeError:
                continue
        if not isinstance(value, list) or not value:
            continue
        if not all(isinstance(row, dict) and "cid" in row for row in value):
            continue
        if must_have_keys and not all(all(k in row for k in must_have_keys) for row in value):
            continue
        if expected_set is not None and {str(row["cid"]) for row in value} != expected_set:
            continue
        candidates.append(value)

    if not candidates:
        return None
    candidates.sort(key=lambda arr: len(arr))
    return candidates[-1]


_STAGE_RESPONSE_KEYS: dict[str, tuple[str, ...]] = {
    "d1": ("program", "visual_description"),
    "d2": ("style_image", "visual_description_image"),
    "e2": ("covers_by_type",),
}


def _strip_markdown_fences(text: str) -> str:
    return re.sub(r"```(?:json)?\s*|\s*```", "", text.strip(), flags=re.IGNORECASE)


def _balanced_array(text: str) -> str | None:
    if not text.startswith("["):
        return None
    depth = 0
    in_string = False
    escape = False
    for idx, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return text[: idx + 1]
    return None


def parse_usage_limit_until(text: str, *, now: datetime | None = None) -> datetime | None:
    if "usage limit" not in text.lower():
        return None
    match = re.search(r"try again at\s+(\d{1,2}):(\d{2})(?:\s*([AP]M))?", text, re.IGNORECASE)
    if not match:
        return (now or datetime.now()) + timedelta(minutes=30)

    base = now or datetime.now()
    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3)
    if ampm:
        marker = ampm.upper()
        if marker == "PM" and hour != 12:
            hour += 12
        elif marker == "AM" and hour == 12:
            hour = 0

    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:
        target += timedelta(days=1)
    return target


def validate_batch(stage: str, expected_cids: Sequence[str], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    if len(rows) != len(expected_cids):
        return [], f"expected {len(expected_cids)} rows, got {len(rows)}"

    by_cid = {str(row.get("cid")): row for row in rows}
    if set(by_cid) != set(expected_cids):
        return [], f"cid mismatch: expected {list(expected_cids)}, got {sorted(by_cid)}"

    normalized = []
    for cid in expected_cids:
        row = by_cid[cid]
        clean, error = validate_row(stage, cid, row)
        if error:
            return [], f"{cid}: {error}"
        normalized.append(clean)
    return normalized, None


def validate_row(stage: str, cid: str, row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if stage == "d1":
        return d1_enrich_codex.validate_result(cid, row)
    if stage == "d2":
        required = ("style_image", "color_tone_image", "material_visual_image", "visual_description_image")
        missing = [field for field in required if field not in row]
        if missing:
            return {}, f"missing fields: {missing}"
        if row.get("style_image") not in STYLE:
            return {}, f"style_image={row.get('style_image')!r} not in allowed vocabulary"
        if row.get("color_tone_image") not in COLOR_TONE:
            return {}, f"color_tone_image={row.get('color_tone_image')!r} not in allowed vocabulary"
        materials = row.get("material_visual_image")
        if not isinstance(materials, list):
            return {}, "material_visual_image must be a list"
        material_visual = [str(value).strip().lower() for value in materials if str(value).strip()][:6]
        if not material_visual:
            return {}, "material_visual_image must contain at least one material"
        description = str(row.get("visual_description_image") or "").strip()
        if len(description.split()) < 8:
            return {}, "visual_description_image is too short"
        return {
            "cid": cid,
            "style_image": row["style_image"],
            "color_tone_image": row["color_tone_image"],
            "material_visual_image": material_visual,
            "visual_description_image": description,
        }, None
    if stage == "e2":
        # E-2 lightweight: per-cid covers_by_type dict (5 type → URL or null).
        covers = row.get("covers_by_type")
        if not isinstance(covers, dict):
            return {}, "covers_by_type must be an object"
        clean: dict[str, str | None] = {t: None for t in IMAGE_TYPES}
        for image_type, value in covers.items():
            key = str(image_type).strip().lower()
            if key not in IMAGE_TYPES:
                return {}, f"covers_by_type[{image_type!r}] is not a valid type"
            if value in (None, "", "null"):
                clean[key] = None
            elif isinstance(value, str) and value.startswith(("http://", "https://")):
                clean[key] = value
            else:
                return {}, f"covers_by_type[{image_type!r}]={value!r} must be a URL or null"
        return {"cid": cid, "covers_by_type": clean}, None
    return {}, f"unknown stage: {stage}"


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()


def append_metric_jsonl(path: Path, row: dict[str, Any]) -> None:
    append_jsonl(path, [row])


def log_failure(path: Path, *, cids: Sequence[str], reason: str, raw: str) -> None:
    rows = [
        {
            "cid": cid,
            "reason": reason,
            "raw_response": raw[-12000:],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        for cid in cids
    ]
    append_jsonl(path, rows)


def build_records_for_stage(stage: str, *, canonical_path: Path, e1_path: Path, done_cids: set[str]) -> list[dict[str, Any]]:
    if stage == "d1":
        return build_d1_records(canonical_path=canonical_path, done_cids=done_cids)
    if stage == "d2":
        return build_d2_records(e1_path=e1_path, done_cids=done_cids)
    if stage == "e2":
        return build_e2_records(e1_path=e1_path, done_cids=done_cids)
    raise ValueError(f"unknown stage: {stage}")


def metric_row(
    *,
    batch_idx: int,
    stage: str,
    surface: str,
    model_meta: ModelMeta,
    expected_cids: Sequence[str],
    start_ts: str,
    end_ts: str,
    wallclock_s: float,
    usage: TokenUsage | None,
    tokens_delta: int | None,
    success: bool,
    failure_reason: str | None,
    codex_returncode: int | None = None,
    codex_stderr: str | None = None,
) -> dict[str, Any]:
    return {
        "batch_idx": batch_idx,
        "stage": stage,
        "surface": surface,
        "model": model_meta.model,
        "reasoning": model_meta.reasoning,
        "fast": model_meta.fast,
        "cids_first": expected_cids[0] if expected_cids else None,
        "cids_last": expected_cids[-1] if expected_cids else None,
        "cids_count": len(expected_cids),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "wallclock_s": round(wallclock_s, 3),
        "tokens_total": usage.total if usage else None,
        "tokens_input": usage.input if usage else None,
        "tokens_output": usage.output if usage else None,
        "tokens_delta": tokens_delta,
        "codex_returncode": codex_returncode,
        "codex_stderr": codex_stderr,
        "success": success,
        "failure_reason": failure_reason,
    }


def _surface_label(tab: str) -> str:
    return tab if ":" in tab else f"{tab}:first"


def run(args: argparse.Namespace) -> int:
    output_path = args.output or STAGE_OUTPUTS[args.stage]
    resume_path = args.resume_from or output_path
    failure_path = args.failures or STAGE_FAILURES[args.stage]
    metrics_path = getattr(args, "metrics", None) or STAGE_METRICS[args.stage]
    done_cids = read_done_cids(resume_path)
    rows = build_records_for_stage(
        args.stage,
        canonical_path=args.canonical,
        e1_path=args.e1,
        done_cids=done_cids,
    )
    model_meta, previous_usage, _ = read_model_meta(args.tab)

    print(f"[dispatch] stage={args.stage} pending={len(rows)} skipped_done={len(done_cids)} tab={args.tab}", flush=True)
    if not rows:
        return 0

    batches_run = 0
    for batch in chunked(rows, args.batch_size):
        if args.max_batches is not None and batches_run >= args.max_batches:
            break
        batch_idx = batches_run + 1
        expected_cids = [str(row["cid"]) for row in batch]
        retry_note = None
        delivered = False
        success = False
        failure_reason = None
        result: PollResult | None = None
        start_dt = datetime.now()
        start_ts = start_dt.isoformat(timespec="seconds")
        start_monotonic = time.monotonic()
        for attempt in range(2):
            # All stages (d1, d2, e2) now use the same cmux dispatch path.
            # The codex tab is responsible for vision processing in d2/e2:
            # the codex agent reads the prompt and uses its background
            # terminal to download images + spawn codex exec -i Vision
            # subprocesses, then returns the JSON array in chat.
            prompt = compose_prompt(args.stage, batch, retry_note=retry_note)
            dispatch_prompt(args.tab, prompt)
            result = poll_screen(
                args.tab,
                timeout_seconds=args.timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
                expected_count=len(batch),
                expected_cids=tuple(expected_cids),
                must_have_keys=_STAGE_RESPONSE_KEYS.get(args.stage, ()),
            )

            if result.usage_limit_until:
                wait_seconds = max(0, (result.usage_limit_until - datetime.now()).total_seconds()) + 30
                print(f"[dispatch] usage limit; sleeping {wait_seconds:.0f}s before retry", flush=True)
                time.sleep(wait_seconds)
                retry_note = "usage limit reached; retrying same batch"
                continue

            if result.timed_out:
                failure_reason = result.failure_reason or "timeout: unknown"
                log_failure(failure_path, cids=expected_cids, reason=failure_reason, raw=result.raw)
                delivered = True
                break

            if result.rows is None:
                retry_note = result.failure_reason or "parse_failed: unknown"
                if attempt == 1:
                    failure_reason = result.failure_reason or retry_note
                    log_failure(failure_path, cids=expected_cids, reason=failure_reason, raw=result.raw)
                    delivered = True
                    break
                continue

            normalized, error = validate_batch(args.stage, expected_cids, result.rows)
            if not error:
                append_jsonl(output_path, normalized)
                delivered = True
                success = True
                break

            retry_note = error
            if attempt == 1:
                failure_reason = error
                log_failure(failure_path, cids=expected_cids, reason=failure_reason, raw=result.raw)
                delivered = True

        batches_run += 1
        end_dt = datetime.now()
        wallclock_s = time.monotonic() - start_monotonic
        usage = parse_token_usage(result.raw) if result else None
        tokens_delta = usage.total - previous_usage.total if usage and previous_usage else None
        if usage:
            previous_usage = usage
        append_metric_jsonl(
            metrics_path,
            metric_row(
                batch_idx=batch_idx,
                stage=args.stage,
                surface=_surface_label(args.tab),
                model_meta=model_meta,
                expected_cids=expected_cids,
                start_ts=start_ts,
                end_ts=end_dt.isoformat(timespec="seconds"),
                wallclock_s=wallclock_s,
                usage=usage,
                tokens_delta=tokens_delta,
                success=success,
                failure_reason=failure_reason,
                codex_returncode=result.returncode if result else None,
                codex_stderr=result.stderr if result else None,
            ),
        )
        model_display = "/".join(value or "unknown" for value in (model_meta.model, model_meta.reasoning, model_meta.fast))
        token_display = tokens_delta if tokens_delta is not None else "unknown"
        print(
            f"[dispatch] batch {batches_run} done cids={expected_cids[0]}..{expected_cids[-1]} "
            f"time={wallclock_s:.1f}s tokens={token_display} model={model_display}",
            flush=True,
        )
        if args.smoke:
            break
        if not delivered:
            break

    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch enrichment batches to a live Codex cmux tab.")
    parser.add_argument("--stage", choices=("d1", "d2", "e2"), required=True)
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--tab", default="enricher")
    parser.add_argument("--resume-from", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--failures", type=Path, default=None)
    parser.add_argument("--metrics", type=Path, default=None)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL_PATH)
    parser.add_argument("--e1", type=Path, default=DEFAULT_E1_PATH)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--poll-interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
