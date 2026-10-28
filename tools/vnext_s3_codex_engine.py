#!/usr/bin/env python3
"""vNext S3 compact remote labeler via `codex exec` (WP3 remote half).

Labels the 9 compact fields of `docs/vnext/CONTRACT_GALLERY_SEMANTICS.md` §4 (v1.4 output
values, §4.1) with §5 applicability, plus a per-building default-cover pick
(`docs/vnext/CONTRACT_COVERS.md` §1/§3). Designed for low tokens:

- several images per codex call (`--batch-size`), each downscaled to max side 768 (labels) or
  512 (cover) JPEG q80 into a temp dir that is deleted after the call; opaque file names
- short frozen prompt + strict JSON schema (`--output-schema`), keyed by idx, no explanations
- minimal codex harness: `--ignore-user-config`, replaced base instructions, agent tools disabled,
  cwd = the empty temp dir (no AGENTS.md), `--ephemeral`, read-only sandbox
- every output row is validated against the controlled vocab + applicability; invalid rows get
  ONE retry (only the invalid images of that batch), then an explicit `failed` row
  (contract §5: no coercion to defaults; failed attempts kept with raw text + error codes)
- resume-safe: a batch/building whose final record exists is skipped (0 requests on re-run)
- per call log: images, tokens (input / cached / output / reasoning / total), seconds
- hard guards: stage token cap (cumulative ledger, incl. earlier runs that share the ledger),
  stop if mean total tokens/image > --max-tokens-per-image or retry rate > --max-retry-rate

The engine never reads gold or local labels; it only receives image files.

Subcommands: `label`, `cover`, `quota` (tiny non-ephemeral call -> rate_limits from the rollout).
No Neon/R2. Images are read from local files only (no network fetch here).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = "vnext_s3_codex_compact"
ENGINE_VERSION = "0.2.0"  # 0.1.0 = smoke prompt (prompt_label sha 7ede239e...); 0.2.0 = in_scope wording after smoke
CONTRACT_VERSION = "vnext-gallery-semantics-contract-v1.4"
CONTRACT_PATH = ROOT / "docs" / "vnext" / "CONTRACT_GALLERY_SEMANTICS.md"
DEFAULT_MODEL = "gpt-6.1-sol"   # gpt-5.5 is rejected for ChatGPT-account codex (400 model_not_found)
DEFAULT_EFFORT = "low"
LABEL_MAX_SIDE = 768
COVER_MAX_SIDE = 512
JPEG_Q = 80
TRANSFORM_LABEL = f"vnext-codex-jpeg-max{LABEL_MAX_SIDE}-q{JPEG_Q}-v1"
TRANSFORM_COVER = f"vnext-codex-jpeg-max{COVER_MAX_SIDE}-q{JPEG_Q}-v1"
CALL_TIMEOUT_S = 300

FIELDS = ["in_scope", "reject_reason", "medium", "spatial_context", "framing_scale",
          "camera_angle", "drawing_kind", "project_state", "project_legibility"]
NA = "not_applicable"
REJECT = ["none", "unreadable_or_blank", "non_architectural_subject", "people_or_event",
          "isolated_product_artwork_or_sample", "text_or_logo_only", "no_project_visible",
          "multi_panel_without_dominant_image", "other"]
MEDIUM = ["photograph", "drawing", "rendering", "physical_model", "mixed", "other", "unknown"]
SPATIAL = ["exterior", "interior", "threshold", NA, "unknown"]
FRAMING = ["site_context", "overall", "element_detail", "material_detail", NA, "unknown"]
CAMERA = ["eye_level", "elevated", "aerial_oblique", "aerial_top_down", NA, "unknown"]
DRAWING = ["plan", "site_plan", "section", "elevation", "axonometric", "perspective", "detail",
           "diagram", "sketch", "composite", "other", NA, "unknown"]
STATE = ["visibly_finished", "construction_visible", "ruin_or_abandoned_visible",
         "demolition_visible", NA, "unknown"]
LEGIB_V14 = ["high", "low", "none", "unknown"]  # v1.4: `medium` is no longer emitted
VOCAB = {"reject_reason": REJECT, "medium": MEDIUM, "spatial_context": SPATIAL,
         "framing_scale": FRAMING, "camera_angle": CAMERA, "drawing_kind": DRAWING,
         "project_state": STATE, "project_legibility": LEGIB_V14}
AERIAL = ("aerial_oblique", "aerial_top_down")

# ------------------------------------------------------------------ frozen prompts (SHA recorded)
INSTRUCTIONS = ("You are an image classification function for an architecture database. "
                "Answer with the requested JSON only. Do not use tools. Do not explain.\n")

PROMPT_LABEL = """Classify each attached image of an architecture project gallery. Image #k has idx k. Judge every image on its own. Return {"rows":[...]} with exactly one row per image.

in_scope: true whenever the project's architecture (building, space, or a part of it) is visible as a photo, drawing, rendering or model, even if people, furniture or products are in the space. false only if the architecture is not shown; then reject_reason is one of unreadable_or_blank, non_architectural_subject, people_or_event (people or an event fill the image and hide the architecture), isolated_product_artwork_or_sample (a product, furniture piece, artwork or material sample shot on its own, no surrounding space), text_or_logo_only, no_project_visible, multi_panel_without_dominant_image, other. If in_scope is true, reject_reason = none.
medium: photograph | drawing (plan, section, elevation, diagram, sketch, any line or graphic drawing) | rendering (CGI visualisation) | physical_model (photo of a scale model) | mixed | other | unknown.
spatial_context: exterior | interior | threshold (entrance, porch, loggia, view across the inside/outside boundary) | unknown.
framing_scale: site_context (building within its surroundings or landscape) | overall (whole building, whole facade, or a whole room or space) | element_detail (a part: stair, window, joint, fixture, corner) | material_detail (surface or texture close-up) | unknown.
camera_angle: eye_level (standing height) | elevated (clearly above standing height, e.g. from an upper floor) | aerial_oblique (drone or aircraft, oblique) | aerial_top_down (vertical) | unknown (between eye_level and elevated, not clear). Aerial only with spatial_context exterior or unknown.
drawing_kind: plan | site_plan | section | elevation | axonometric | perspective | detail | diagram | sketch | composite | other | unknown.
project_state: visibly_finished | construction_visible | ruin_or_abandoned_visible | demolition_visible | unknown.
project_legibility: high (the project's architecture can be read) | low (hard to read: tiny, dark, blurred, cropped or obstructed) | unknown.

Applicability, otherwise use not_applicable:
- in_scope false: spatial_context, framing_scale, camera_angle, drawing_kind, project_state = not_applicable; project_legibility = none.
- photograph: spatial_context, framing_scale, camera_angle, project_state apply; drawing_kind = not_applicable.
- drawing: drawing_kind applies; spatial_context, framing_scale, camera_angle, project_state = not_applicable.
- rendering: spatial_context, framing_scale, camera_angle, drawing_kind apply; project_state = not_applicable.
- physical_model, mixed, other, unknown: spatial_context, framing_scale, camera_angle, drawing_kind, project_state = not_applicable.
"""

PROMPT_COVER = """All attached images belong to one architecture project. Image #k has idx k. Choose the default cover image.
Acceptable: a photograph of the building exterior that shows the whole building (overall) and reads well. Prefer eye-level or slightly elevated views; an aerial overall photograph only if it explains the building better than every ground-level exterior.
Never acceptable: drawing, rendering, physical model, interior, close-up or detail, partial facade fragment, people, product or other non-architectural image.
Return {"ranked":[up to 3 acceptable idx, best first],"acceptable":[every acceptable idx]}. Both lists empty if no image is acceptable.
"""


def _enum(vals):
    return {"type": "string", "enum": list(vals)}


SCHEMA_LABEL = {
    "type": "object", "additionalProperties": False, "required": ["rows"],
    "properties": {"rows": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["idx"] + FIELDS,
        "properties": {"idx": {"type": "integer"}, "in_scope": {"type": "boolean"},
                       **{f: _enum(VOCAB[f]) for f in FIELDS[1:]}}}}},
}
SCHEMA_COVER = {
    "type": "object", "additionalProperties": False, "required": ["ranked", "acceptable"],
    "properties": {"ranked": {"type": "array", "items": {"type": "integer"}},
                   "acceptable": {"type": "array", "items": {"type": "integer"}}},
}

DISABLE_FEATURES = ["apps", "browser_use", "browser_use_external", "computer_use", "image_generation",
                    "goals", "hooks", "multi_agent", "multi_agent_v2", "plugins", "remote_plugin",
                    "shell_tool", "unified_exec", "skill_search", "sleep_tool", "tool_suggest",
                    "view_image", "in_app_browser", "workspace_dependencies", "worktrees",
                    "shell_snapshot"]
EXTRA_CONFIG = ["web_search=disabled", "include_permissions_instructions=false",
                "include_environment_context=false", "skills.bundled.enabled=false",
                "include_apps_instructions=false", "include_collaboration_mode_instructions=false"]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


def frozen_shas() -> dict:
    return {"instructions": sha256_bytes(INSTRUCTIONS.encode()),
            "prompt_label": sha256_bytes(PROMPT_LABEL.encode()),
            "prompt_cover": sha256_bytes(PROMPT_COVER.encode()),
            "schema_label": sha256_bytes(json.dumps(SCHEMA_LABEL, sort_keys=True).encode()),
            "schema_cover": sha256_bytes(json.dumps(SCHEMA_COVER, sort_keys=True).encode())}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


# ------------------------------------------------------------------ validation (§4 v1.4 + §5)
def validate_row(r: dict) -> list[str]:
    """Return a list of error codes ([] = valid). Never coerces values."""
    errs = []
    if not isinstance(r, dict):
        return ["row_not_object"]
    missing = [f for f in FIELDS if f not in r]
    if missing:
        return ["missing:" + ",".join(missing)]
    if not isinstance(r["in_scope"], bool):
        errs.append("in_scope_not_bool")
    for f in FIELDS[1:]:
        if r[f] not in VOCAB[f]:
            errs.append(f"vocab:{f}={r[f]}")
    if errs:
        return errs
    ins, med = r["in_scope"], r["medium"]
    view = ("spatial_context", "framing_scale", "camera_angle")
    if not ins:
        if r["reject_reason"] == "none":
            errs.append("out_of_scope_with_reject_none")
        for f in view + ("drawing_kind", "project_state"):
            if r[f] != NA:
                errs.append(f"appl:out_of_scope:{f}={r[f]}")
        if r["project_legibility"] != "none":
            errs.append(f"appl:out_of_scope:legibility={r['project_legibility']}")
        return errs
    if r["reject_reason"] != "none":
        errs.append(f"in_scope_with_reject={r['reject_reason']}")
    if r["project_legibility"] == "none":
        errs.append("appl:in_scope:legibility=none")
    if med in ("photograph", "rendering"):
        for f in view:
            if r[f] == NA:
                errs.append(f"appl:{med}:{f}=not_applicable")
        if r["camera_angle"] in AERIAL and r["spatial_context"] not in ("exterior", "unknown"):
            errs.append(f"appl:aerial_with_spatial={r['spatial_context']}")
    else:
        for f in view:
            if r[f] != NA:
                errs.append(f"appl:{med}:{f}={r[f]}")
    if med in ("drawing", "rendering"):
        if r["drawing_kind"] == NA:
            errs.append(f"appl:{med}:drawing_kind=not_applicable")
    elif r["drawing_kind"] != NA:
        errs.append(f"appl:{med}:drawing_kind={r['drawing_kind']}")
    if med == "photograph":
        if r["project_state"] == NA:
            errs.append("appl:photograph:project_state=not_applicable")
    elif r["project_state"] != NA:
        errs.append(f"appl:{med}:project_state={r['project_state']}")
    return errs


def derived_types(lab: dict) -> list[str]:
    """§6 non-exclusive tags, deterministic from core fields."""
    if not lab or not lab.get("in_scope"):
        return []
    t = []
    sp, fr, ca, med = lab["spatial_context"], lab["framing_scale"], lab["camera_angle"], lab["medium"]
    if sp == "exterior" and fr == "overall":
        t.append("exterior_overall")
    if sp == "exterior" and fr == "site_context":
        t.append("exterior_context")
    if sp == "interior":
        t.append("interior")
    if sp == "threshold":
        t.append("threshold")
    if med in ("drawing", "rendering", "physical_model"):
        t.append(med)
    if ca in AERIAL:
        t.append("aerial")
    if fr in ("element_detail", "material_detail"):
        t.append("detail")
    if lab["project_state"] in ("construction_visible", "demolition_visible"):
        t.append("construction_archive")
    return t


def validate_cover(obj: dict, n: int) -> list[str]:
    errs = []
    if not isinstance(obj, dict) or not isinstance(obj.get("ranked"), list) or not isinstance(obj.get("acceptable"), list):
        return ["cover_shape"]
    for k in ("ranked", "acceptable"):
        for v in obj[k]:
            if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= n):
                errs.append(f"cover_{k}_out_of_range={v}")
        if len(set(obj[k])) != len(obj[k]):
            errs.append(f"cover_{k}_duplicate")
    if len(obj["ranked"]) > 3:
        errs.append("cover_ranked_gt3")
    if not set(obj["ranked"]) <= set(obj["acceptable"]):
        errs.append("cover_ranked_not_subset_of_acceptable")
    return errs


# ------------------------------------------------------------------ inputs
def load_items(manifest: Path, fetch_log: Path) -> list[dict]:
    """[{idx, canonical_bld_id, stratum, image_asset_id, sort_order, image_path, pixel_sha256}]"""
    man = json.loads(manifest.read_text(encoding="utf-8"))
    fl = {r["idx"]: r for r in read_jsonl(fetch_log) if r.get("status") == "ok"}
    items = []
    for b in man["buildings"]:
        for im in b["images"]:
            f = fl.get(im["idx"])
            items.append({"idx": im["idx"], "canonical_bld_id": b["canonical_bld_id"], "stratum": b["stratum"],
                          "image_asset_id": im["image_asset_id"], "sort_order": im["sort_order"],
                          "image_path": f["image_path"] if f else None,
                          "pixel_sha256": f.get("saved_sha256") if f else None})
    return sorted(items, key=lambda x: x["idx"])


def prep_images(paths: list[str], max_side: int, tmpdir: Path) -> list[dict]:
    from PIL import Image  # repo .venv has Pillow
    out = []
    for k, p in enumerate(paths, 1):
        with Image.open(ROOT / p) as im:
            im = im.convert("RGB")
            w0, h0 = im.size
            im.thumbnail((max_side, max_side), Image.LANCZOS)
            dst = tmpdir / f"{k:02d}.jpg"
            im.save(dst, "JPEG", quality=JPEG_Q)
            out.append({"k": k, "path": dst, "src_wh": [w0, h0], "sent_wh": list(im.size),
                        "sent_bytes": dst.stat().st_size})
    return out


# ------------------------------------------------------------------ codex call
def codex_bin() -> str:
    b = shutil.which("codex")
    if not b:
        raise SystemExit("codex CLI not found on PATH")
    return b


def run_codex(prompt: str, schema: dict, images: list[Path], tmpdir: Path, model: str, effort: str,
              ephemeral: bool = True) -> dict:
    instr = tmpdir / "_instructions.txt"
    instr.write_text(INSTRUCTIONS, encoding="utf-8")
    sch = tmpdir / "_schema.json"
    sch.write_text(json.dumps(schema), encoding="utf-8")
    cmd = [codex_bin(), "exec", "--json", "--ignore-user-config", "-s", "read-only",
           "--skip-git-repo-check", "-C", str(tmpdir), "-c", f"model={model}",
           "-c", f"model_reasoning_effort={effort}", "-c", f"model_instructions_file={instr.as_posix()}",
           "--output-schema", str(sch)]
    if ephemeral:
        cmd.append("--ephemeral")
    for f in DISABLE_FEATURES:
        cmd += ["--disable", f]
    for c in EXTRA_CONFIG:
        cmd += ["-c", c]
    for p in images:
        cmd += ["-i", str(p)]
    cmd += ["--", "-"]  # prompt on stdin (avoids cmd.exe newline mangling of the .cmd shim)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=CALL_TIMEOUT_S, cwd=str(tmpdir))
        out, err, rc = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as e:
        out, err, rc = (e.stdout or "") if isinstance(e.stdout, str) else "", "timeout", -9
    secs = round(time.monotonic() - t0, 2)
    usage, text, errors, thread_id = None, None, [], None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "thread.started":
            thread_id = ev.get("thread_id")
        elif t == "turn.completed":
            usage = ev.get("usage")
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            text = ev["item"].get("text")
        elif t in ("error", "turn.failed") or (t == "item.completed" and (ev.get("item") or {}).get("type") == "error"):
            errors.append(str(ev.get("message") or ev.get("error") or ev.get("item"))[:500])
    u = usage or {}
    tok = {"input": u.get("input_tokens"), "cached_input": u.get("cached_input_tokens"),
           "output": u.get("output_tokens"), "reasoning_output": u.get("reasoning_output_tokens")}
    tok["total"] = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) if usage else None
    return {"rc": rc, "seconds": secs, "tokens": tok, "text": text, "errors": errors,
            "thread_id": thread_id, "stderr_tail": err[-400:] if rc else ""}


def parse_json_text(text):
    if not text:
        return None, "empty_output"
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e < s:
        return None, "no_json_object"
    try:
        return json.loads(text[s:e + 1]), None
    except json.JSONDecodeError:
        return None, "json_decode_error"


# ------------------------------------------------------------------ ledger / guards
class Ledger:
    """Stage token ledger shared by every run that points at the same file (cap is cumulative)."""

    def __init__(self, path: Path, cap: int, max_tpi: float, max_retry_rate: float, min_calls_for_rate: int):
        self.path, self.cap, self.max_tpi, self.max_rr, self.min_calls = path, cap, max_tpi, max_retry_rate, min_calls_for_rate
        self.lock = threading.Lock()
        self.d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "cap_total_tokens": cap, "total_tokens": 0, "calls": 0, "images_sent": 0, "runs": []}
        self.d["cap_total_tokens"] = cap
        self.run_calls = self.run_retry_calls = self.run_first_calls = 0
        self.run_tokens = self.run_images = 0
        self.stop_reason = None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.d, indent=1), encoding="utf-8")

    def can_spend(self, est: int) -> bool:
        with self.lock:
            if self.stop_reason:
                return False
            if self.d["total_tokens"] + est > self.cap:
                self.stop_reason = f"token_cap: ledger {self.d['total_tokens']} + est {est} > cap {self.cap}"
                return False
            return True

    def record(self, tokens: int | None, n_images: int, is_retry: bool, count_tpi: bool = True):
        with self.lock:
            t = tokens or 0
            self.d["total_tokens"] += t
            self.d["calls"] += 1
            self.d["images_sent"] += n_images
            self.run_calls += 1
            self.run_tokens += t
            self.run_images += n_images
            if is_retry:
                self.run_retry_calls += 1
            else:
                self.run_first_calls += 1
            self.save()
            if count_tpi and self.run_images and self.run_tokens / self.run_images > self.max_tpi and self.run_calls >= 2:
                self.stop_reason = (f"tokens_per_image {self.run_tokens / self.run_images:.0f} > {self.max_tpi}")
            if self.run_first_calls >= self.min_calls and self.run_retry_calls / self.run_first_calls > self.max_rr:
                self.stop_reason = (f"retry_rate {self.run_retry_calls}/{self.run_first_calls} > {self.max_rr:.0%}")


# ------------------------------------------------------------------ label stage
def label_batch(batch: dict, args, ledger: Ledger, calls_fh, lock) -> dict:
    """One batch: attempt 1 on all images, attempt 2 only on invalid images; returns final record."""
    rows_final: dict[int, dict] = {}
    attempts = []
    todo = list(batch["items"])
    for attempt in (1, 2):
        if not todo:
            break
        est = int(args.est_tokens_per_image * len(todo) + args.est_overhead)
        if not ledger.can_spend(est):
            break
        tmp = Path(tempfile.mkdtemp(prefix="vnx_cx_"))
        try:
            prepped = prep_images([it["image_path"] for it in todo], LABEL_MAX_SIDE, tmp)
            res = run_codex(PROMPT_LABEL, SCHEMA_LABEL, [p["path"] for p in prepped], tmp, args.model, args.effort)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        ledger.record(res["tokens"]["total"], len(todo), attempt > 1)
        obj, perr = parse_json_text(res["text"])
        local_to_item = {k: it for k, it in enumerate(todo, 1)}
        row_errs, got = {}, {}
        if obj is not None:
            rows = obj.get("rows") if isinstance(obj, dict) else None
            if not isinstance(rows, list):
                perr = "rows_missing"
            else:
                for r in rows:
                    k = r.get("idx") if isinstance(r, dict) else None
                    if k not in local_to_item:
                        row_errs.setdefault("_extra", []).append(f"unknown_idx={k}")
                        continue
                    if k in got:
                        row_errs[k] = ["duplicate_idx"]
                        continue
                    got[k] = r
        bad = []
        for k, it in local_to_item.items():
            if perr:
                e = [perr]
            elif k in row_errs:
                e = row_errs[k]
            elif k not in got:
                e = ["row_missing"]
            else:
                e = validate_row(got[k])
            if e:
                bad.append(it)
                rows_final[it["idx"]] = {"status": "invalid", "errors": e, "attempt": attempt,
                                         "raw": got.get(k)}
            else:
                lab = {f: got[k][f] for f in FIELDS}
                rows_final[it["idx"]] = {"status": "ok", "labels": lab, "attempt": attempt}
        call = {"ts": now(), "stage": "label", "batch_id": batch["batch_id"], "attempt": attempt,
                "idx": [it["idx"] for it in todo], "n_images": len(todo),
                "sent": [{"idx": local_to_item[p["k"]]["idx"], "src_wh": p["src_wh"], "sent_wh": p["sent_wh"],
                          "sent_bytes": p["sent_bytes"]} for p in prepped],
                "tokens": res["tokens"], "seconds": res["seconds"], "rc": res["rc"],
                "parse_error": perr, "invalid_idx": [it["idx"] for it in bad],
                "row_errors": {str(local_to_item[k]["idx"]) if isinstance(k, int) else k: v for k, v in row_errs.items()},
                "codex_errors": res["errors"], "stderr_tail": res["stderr_tail"],
                "raw_text": res["text"] if (perr or bad) else None}
        attempts.append({"attempt": attempt, "n_images": len(todo), "tokens_total": res["tokens"]["total"],
                         "seconds": res["seconds"], "invalid": len(bad)})
        with lock:
            calls_fh.write(json.dumps(call, ensure_ascii=False) + "\n")
            calls_fh.flush()
        todo = bad
    complete = all(it["idx"] in rows_final for it in batch["items"]) and (
        not todo or len(attempts) == 2)
    for it in batch["items"]:
        r = rows_final.get(it["idx"])
        if r is None:
            rows_final[it["idx"]] = {"status": "not_run", "errors": ["stopped_before_call"]}
        elif r["status"] == "invalid":
            r["status"] = "failed"
    return {"batch_id": batch["batch_id"], "idx": [it["idx"] for it in batch["items"]],
            "complete": complete, "attempts": attempts,
            "rows": {str(k): v for k, v in sorted(rows_final.items())}}


def make_batches(items: list[dict], size: int) -> list[dict]:
    out = []
    for i in range(0, len(items), size):
        chunk = items[i:i + size]
        bid = f"b{size}_" + "-".join(str(x["idx"]) for x in chunk)
        out.append({"batch_id": bid, "items": chunk})
    return out


def cmd_label(args) -> int:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = load_items(Path(args.manifest), Path(args.fetch_log))
    if args.idx:
        want = [int(x) for x in args.idx.split(",")]
        by = {it["idx"]: it for it in items}
        items = [by[i] for i in want]
    items = [it for it in items if it["image_path"]]
    batches = make_batches(items, args.batch_size)
    bpath = out / "batches.jsonl"
    done = {b["batch_id"] for b in read_jsonl(bpath) if b.get("complete")}
    todo = [b for b in batches if b["batch_id"] not in done]
    ledger = Ledger(Path(args.ledger), args.token_cap, args.max_tokens_per_image, args.max_retry_rate,
                    args.min_calls_for_rate)
    run_meta = {"started": now(), "out_dir": str(out), "batches_total": len(batches), "batches_skipped_done": len(done),
                "batches_todo": len(todo), "batch_size": args.batch_size}
    print(json.dumps(run_meta), flush=True)
    lock = threading.Lock()
    t0 = time.monotonic()
    with open(out / "calls.jsonl", "a", encoding="utf-8") as cfh, open(bpath, "a", encoding="utf-8") as bfh:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {}
            pending = list(todo)
            # submit lazily so a stop condition halts new work
            while pending or futs:
                while pending and len(futs) < args.workers and not ledger.stop_reason:
                    b = pending.pop(0)
                    futs[ex.submit(label_batch, b, args, ledger, cfh, lock)] = b
                if not futs:
                    break
                for f in as_completed(list(futs)):
                    futs.pop(f)
                    rec = f.result()
                    with lock:
                        bfh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        bfh.flush()
                    print(json.dumps({"batch": rec["batch_id"], "complete": rec["complete"], "attempts": rec["attempts"],
                                      "ledger_total": ledger.d["total_tokens"]}), flush=True)
                    break
                if ledger.stop_reason and pending:
                    print(json.dumps({"stop": ledger.stop_reason, "pending_batches": len(pending)}), flush=True)
                    pending = []
    wall = round(time.monotonic() - t0, 1)
    ledger.d["runs"].append({"ts": now(), "stage": "label", "out_dir": str(out), "calls": ledger.run_calls,
                             "tokens": ledger.run_tokens, "images_sent": ledger.run_images,
                             "stop_reason": ledger.stop_reason})
    ledger.save()
    write_labels(out, items, args, wall, ledger)
    return 0 if not ledger.stop_reason else 2


def write_labels(out: Path, items: list[dict], args, wall: float, ledger: Ledger):
    """Rebuild labels.jsonl + run.json from batches.jsonl/calls.jsonl (deterministic, no requests)."""
    recs = {}
    for b in read_jsonl(out / "batches.jsonl"):
        for k, v in b["rows"].items():
            if v["status"] in ("ok", "failed") or int(k) not in recs:
                recs[int(k)] = v
    shas = frozen_shas()
    lines = []
    for it in items:
        r = recs.get(it["idx"], {"status": "not_run", "errors": ["no_batch_record"]})
        lab = r.get("labels")
        lines.append({"idx": it["idx"], "canonical_bld_id": it["canonical_bld_id"], "stratum": it["stratum"],
                      "image_asset_id": it["image_asset_id"], "sort_order": it["sort_order"],
                      "engine": ENGINE, "engine_version": ENGINE_VERSION, "model": args.model,
                      "reasoning_effort": args.effort, "contract_version": CONTRACT_VERSION,
                      "prompt_sha256": shas["prompt_label"], "transform_version": TRANSFORM_LABEL,
                      "pixel_sha256": it["pixel_sha256"], "status": r["status"], "attempt": r.get("attempt"),
                      "errors": r.get("errors", []), "labels": lab, "scores": {},
                      "derived_semantic_types": derived_types(lab) if lab else []})
    with open(out / "labels.jsonl", "w", encoding="utf-8") as fh:
        for x in lines:
            fh.write(json.dumps(x, ensure_ascii=False) + "\n")
    calls = [c for c in read_jsonl(out / "calls.jsonl") if c.get("stage") == "label"]
    write_run(out, "label", calls, lines, args, wall, ledger)


def summarize_calls(calls: list[dict]) -> dict:
    tot = sum((c["tokens"].get("total") or 0) for c in calls)
    imgs = sum(c["n_images"] for c in calls)
    first = [c for c in calls if c["attempt"] == 1]
    retry = [c for c in calls if c["attempt"] > 1]
    return {"calls": len(calls), "first_attempt_calls": len(first), "retry_calls": len(retry),
            "retry_rate": round(len(retry) / len(first), 4) if first else None,
            "images_sent": imgs, "tokens_total": tot,
            "tokens_input": sum((c["tokens"].get("input") or 0) for c in calls),
            "tokens_cached_input": sum((c["tokens"].get("cached_input") or 0) for c in calls),
            "tokens_output": sum((c["tokens"].get("output") or 0) for c in calls),
            "tokens_reasoning_output": sum((c["tokens"].get("reasoning_output") or 0) for c in calls),
            "mean_tokens_per_image_sent": round(tot / imgs, 1) if imgs else None,
            "mean_seconds_per_call": round(sum(c["seconds"] for c in calls) / len(calls), 2) if calls else None,
            "sum_call_seconds": round(sum(c["seconds"] for c in calls), 1),
            "calls_without_usage": sum(1 for c in calls if c["tokens"].get("total") is None)}


def write_run(out: Path, stage: str, calls, lines, args, wall, ledger):
    from collections import Counter
    st = Counter(x["status"] for x in lines)
    prev = {}
    rp = out / "run.json"
    if rp.exists():
        prev = json.loads(rp.read_text(encoding="utf-8"))
    run = dict(prev)
    run.update({
        "engine": ENGINE, "engine_version": ENGINE_VERSION, "tool": "tools/vnext_s3_codex_engine.py",
        "tool_sha256": sha256_file(Path(__file__)), "contract_version": CONTRACT_VERSION,
        "contract_sha256": sha256_file(CONTRACT_PATH), "model": args.model, "reasoning_effort": args.effort,
        "service_tier": "default (no fast/priority)", "codex_cli": codex_version(),
        "frozen_sha256": frozen_shas(), "harness": {"ignore_user_config": True, "ephemeral": True,
                                                    "sandbox": "read-only", "disabled_features": DISABLE_FEATURES,
                                                    "extra_config": EXTRA_CONFIG, "cwd": "per-call empty temp dir"},
        "inputs": {"manifest": args.manifest, "manifest_sha256": sha256_file(Path(args.manifest)),
                   "fetch_log": args.fetch_log, "fetch_log_sha256": sha256_file(Path(args.fetch_log))},
        "ledger": {"path": str(args.ledger), "cap_total_tokens": ledger.cap, "ledger_total_tokens": ledger.d["total_tokens"],
                   "stop_reason_this_invocation": ledger.stop_reason},
        "guards": {"max_tokens_per_image": args.max_tokens_per_image, "max_retry_rate": args.max_retry_rate,
                   "min_calls_for_rate": args.min_calls_for_rate},
    })
    run[stage] = {"updated": now(), "batch_size": getattr(args, "batch_size", None),
                  "transform_version": TRANSFORM_LABEL if stage == "label" else TRANSFORM_COVER,
                  "records": len(lines), "status_counts": dict(st), "calls": summarize_calls(calls),
                  "last_invocation_wall_s": wall, "workers": args.workers,
                  "ledger_this_invocation": {"calls": ledger.run_calls, "tokens": ledger.run_tokens}}
    rp.write_text(json.dumps(run, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({stage: run[stage]}, indent=1), flush=True)


_CV = None


def codex_version():
    global _CV
    if _CV is None:
        try:
            _CV = subprocess.run([codex_bin(), "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception as e:  # noqa: BLE001
            _CV = f"unknown ({e})"
    return _CV


# ------------------------------------------------------------------ cover stage
def cover_building(bld: str, its: list[dict], args, ledger: Ledger, calls_fh, lock) -> dict:
    n = len(its)
    result, attempts = None, []
    for attempt in (1, 2):
        est = int(args.est_tokens_per_image * n + args.est_overhead)
        if not ledger.can_spend(est):
            break
        tmp = Path(tempfile.mkdtemp(prefix="vnx_cx_"))
        try:
            prepped = prep_images([it["image_path"] for it in its], COVER_MAX_SIDE, tmp)
            res = run_codex(PROMPT_COVER, SCHEMA_COVER, [p["path"] for p in prepped], tmp, args.model, args.effort)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        ledger.record(res["tokens"]["total"], n, attempt > 1)
        obj, perr = parse_json_text(res["text"])
        errs = [perr] if perr else validate_cover(obj, n)
        call = {"ts": now(), "stage": "cover", "batch_id": f"cover_{bld}", "canonical_bld_id": bld,
                "attempt": attempt, "idx": [it["idx"] for it in its], "n_images": n,
                "sent": [{"idx": its[p["k"] - 1]["idx"], "sent_wh": p["sent_wh"], "sent_bytes": p["sent_bytes"]}
                         for p in prepped],
                "tokens": res["tokens"], "seconds": res["seconds"], "rc": res["rc"], "errors": errs,
                "codex_errors": res["errors"], "raw_text": res["text"]}
        with lock:
            calls_fh.write(json.dumps(call, ensure_ascii=False) + "\n")
            calls_fh.flush()
        attempts.append({"attempt": attempt, "tokens_total": res["tokens"]["total"], "seconds": res["seconds"],
                         "errors": errs})
        if not errs:
            m = {k: its[k - 1]["idx"] for k in range(1, n + 1)}
            result = {"ranked_idx": [m[k] for k in obj["ranked"]], "acceptable_idx": sorted(m[k] for k in obj["acceptable"])}
            break
    complete = result is not None or len(attempts) == 2
    rec = {"canonical_bld_id": bld, "n_images": n, "complete": complete, "attempts": attempts,
           "status": "ok" if result else ("failed" if complete else "not_run"),
           "engine": ENGINE, "engine_version": ENGINE_VERSION, "model": args.model,
           "prompt_sha256": frozen_shas()["prompt_cover"], "transform_version": TRANSFORM_COVER}
    if result:
        rec.update(result)
        rec["default_cover_idx"] = result["ranked_idx"][0] if result["ranked_idx"] else None
        rec["review_required"] = not result["ranked_idx"]
    return rec


def cmd_cover(args) -> int:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = [it for it in load_items(Path(args.manifest), Path(args.fetch_log)) if it["image_path"]]
    by = {}
    for it in items:
        by.setdefault(it["canonical_bld_id"], []).append(it)
    blds = sorted(by)
    if args.buildings:
        blds = [b for b in blds if b in set(args.buildings.split(","))]
    cpath = out / "cover.jsonl"
    done = {r["canonical_bld_id"] for r in read_jsonl(cpath) if r.get("complete")}
    todo = [b for b in blds if b not in done]
    ledger = Ledger(Path(args.ledger), args.token_cap, args.max_tokens_per_image, args.max_retry_rate,
                    args.min_calls_for_rate)
    print(json.dumps({"buildings": len(blds), "skipped_done": len(done), "todo": len(todo)}), flush=True)
    lock = threading.Lock()
    t0 = time.monotonic()
    with open(out / "calls.jsonl", "a", encoding="utf-8") as cfh, open(cpath, "a", encoding="utf-8") as ofh:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = [ex.submit(cover_building, b, by[b], args, ledger, cfh, lock) for b in todo]
            for f in as_completed(futs):
                rec = f.result()
                with lock:
                    ofh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    ofh.flush()
                print(json.dumps({k: rec.get(k) for k in ("canonical_bld_id", "status", "ranked_idx", "attempts")}), flush=True)
    wall = round(time.monotonic() - t0, 1)
    ledger.d["runs"].append({"ts": now(), "stage": "cover", "out_dir": str(out), "calls": ledger.run_calls,
                             "tokens": ledger.run_tokens, "images_sent": ledger.run_images,
                             "stop_reason": ledger.stop_reason})
    ledger.save()
    lines = [r for r in read_jsonl(cpath) if r.get("complete")]
    calls = [c for c in read_jsonl(out / "calls.jsonl") if c.get("stage") == "cover"]
    write_run(out, "cover", calls, lines, args, wall, ledger)
    return 0 if not ledger.stop_reason else 2


# ------------------------------------------------------------------ quota probe
def cmd_quota(args) -> int:
    """Tiny non-ephemeral call; read rate_limits from its rollout file (~/.codex/sessions)."""
    tmp = Path(tempfile.mkdtemp(prefix="vnx_cxq_"))
    try:
        res = run_codex('Return {"rows":[]}.', SCHEMA_LABEL, [], tmp, args.model, args.effort, ephemeral=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rl = None
    tid = res.get("thread_id")
    if tid:
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
        for p in home.rglob(f"*{tid}*.jsonl"):
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"rate_limits"' in line:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pl = ev.get("payload") or ev
                    if isinstance(pl, dict) and pl.get("rate_limits"):
                        rl = pl["rate_limits"]
    rec = {"ts": now(), "label": args.label, "model": args.model, "thread_id": tid, "tokens": res["tokens"],
           "rate_limits": rl}
    if rl and (rl.get("primary") or {}).get("resets_at"):
        rec["primary_resets_at_utc"] = datetime.fromtimestamp(rl["primary"]["resets_at"], timezone.utc).isoformat()
    print(json.dumps(rec, indent=1))
    if args.out:
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    if args.ledger:
        led = Ledger(Path(args.ledger), args.token_cap, 1e12, 1.0, 10 ** 9)
        led.record(res["tokens"]["total"], 0, False, count_tpi=False)
        led.d["runs"].append({"ts": now(), "stage": "quota_probe", "tokens": res["tokens"]["total"]})
        led.save()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--effort", default=DEFAULT_EFFORT)
        p.add_argument("--ledger", required=True, help="stage token ledger json (cap is cumulative)")
        p.add_argument("--token-cap", type=int, required=True)

    for name in ("label", "cover"):
        p = sub.add_parser(name)
        common(p)
        p.add_argument("--manifest", required=True)
        p.add_argument("--fetch-log", required=True)
        p.add_argument("--out-dir", required=True)
        p.add_argument("--workers", type=int, default=3)
        p.add_argument("--max-tokens-per-image", type=float, default=6000)
        p.add_argument("--max-retry-rate", type=float, default=0.05)
        p.add_argument("--min-calls-for-rate", type=int, default=10)
        p.add_argument("--est-tokens-per-image", type=float, default=1500)
        p.add_argument("--est-overhead", type=float, default=5000)
        if name == "label":
            p.add_argument("--batch-size", type=int, default=8)
            p.add_argument("--idx", default=None, help="comma list of idx (smoke subset)")
        else:
            p.add_argument("--buildings", default=None)
    q = sub.add_parser("quota")
    common(q)
    q.add_argument("--label", default="")
    q.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return {"label": cmd_label, "cover": cmd_cover, "quota": cmd_quota}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
