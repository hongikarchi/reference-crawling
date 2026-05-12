"""Phase 19: D-2/E-2 image enrichment via Claude Sonnet sub-agent.

External world model:
  - Claude (this shell, Opus) = orchestrator. Drives the loop.
  - Sonnet sub-agent (via Agent tool) = vision analysis. Reads local
    image files (Read tool / multimodal) and returns JSON.
  - Bash side (this script) = image download + result append.

This script does NOT call Anthropic SDK. It only handles file IO so
Claude can compose Agent calls in between Bash invocations.

CLI:
  python -m enrich.sonnet_image_runner d2 prepare [--batch-size 30]
  python -m enrich.sonnet_image_runner d2 append --result-json /tmp/d2_out.json
  python -m enrich.sonnet_image_runner e2 prepare [--batch-size 30]
  python -m enrich.sonnet_image_runner e2 append --result-json /tmp/e2_out.json
  python -m enrich.sonnet_image_runner stats

`prepare` downloads images for the next batch and prints a JSON manifest
(for Claude to feed to the Agent prompt). `append` validates the
sub-agent's JSON output and appends rows to the canonical jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "data" / "canonical"
E1_PATH = DATA / "e1_clusters.jsonl"
D2_OUT = DATA / "d2_results.jsonl"
E2_OUT = DATA / "e2_image_types.jsonl"
D2_FAILURES = DATA / "d2_failures.jsonl"
E2_FAILURES = DATA / "e2_failures.jsonl"

IMAGE_TYPES = ("exterior", "interior", "drawing", "aerial", "detail")
STYLE = ("Brutalist", "Contemporary", "Deconstructivist", "High-Tech", "Industrial",
         "Minimalist", "Modernist", "Neo-Classical", "Organic", "Parametric",
         "Postmodern", "Vernacular")
COLOR_TONE = ("Cool", "Dark", "Earth", "Light", "Monochrome", "Neutral", "Vibrant", "Warm")

_DRAWING_RE = re.compile(r"(drawing|plan|section|elevation)", re.I)
_AERIAL_RE = re.compile(r"(aerial|drone|birds[-_ ]?eye|bird[-_ ]?s[-_ ]?eye)", re.I)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------- iter helpers ----------

def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _done_cids(path: Path) -> set[str]:
    done: set[str] = set()
    for row in _iter_jsonl(path):
        if isinstance(row, dict) and row.get("cid"):
            done.add(str(row["cid"]))
    return done


def _filename_heuristic(url: str, kind: str | None = None) -> str | None:
    text = " ".join(p for p in (urlparse(url).path, url, kind or "") if p)
    if kind == "drawing" or _DRAWING_RE.search(text):
        return "drawing"
    if _AERIAL_RE.search(text):
        return "aerial"
    return None


# ---------- download ----------

def _download(url: str, dest: Path, *, timeout: int = 30, max_dim: int = 1800) -> None:
    """Raises on any failure (caller decides how to record).

    After download, resizes the image so max(width, height) ≤ max_dim. The
    Sonnet sub-agent (Read tool, multi-image) rejects images >2000px in
    many-image batches with 'dimension limit for many-image requests'.
    1800 leaves headroom and rarely loses meaningful detail for vision
    classification."""
    headers = {"User-Agent": UA}
    resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    try:
        from PIL import Image
        im = Image.open(dest)
        # GIF or multi-frame: take first frame only.
        if getattr(im, "is_animated", False):
            im.seek(0)
        # Force RGB (drops alpha / palette modes that the Vision API
        # sometimes rejects in many-image batches).
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGB")
        else:
            im = im.convert("RGB")
        # Resize to ≤ max_dim. Sonnet many-image batch rejects > 2000px;
        # 1500 leaves comfortable headroom.
        im.thumbnail((max_dim, max_dim))
        # Always re-save as JPEG (small file size, predictable format).
        im.save(dest, "JPEG", quality=85)
    except Exception:
        # If PIL can't open (corrupt / unsupported format), leave as-is.
        # Caller will see Vision Read failure and fallback per spec.
        pass


# ---------- D-2 ----------

def _build_d2_batch(batch_size: int) -> list[dict[str, Any]]:
    done = _done_cids(D2_OUT)
    pending: list[dict[str, Any]] = []
    for row in _iter_jsonl(E1_PATH):
        cid = str(row.get("cid") or "")
        if not cid or cid in done:
            continue
        # Pick the first cluster's best image as cover (matches D-2's existing
        # convention; the upstream e1 already ranked it).
        clusters = row.get("best_image_per_cluster") or {}
        cover = None
        for image in clusters.values():
            if isinstance(image, dict) and image.get("url"):
                cover = image
                break
        if not cover:
            continue
        pending.append({"cid": cid, "url": cover["url"], "source": cover.get("source")})
        if len(pending) >= batch_size:
            break
    return pending


def cmd_d2_prepare(batch_size: int) -> int:
    batch = _build_d2_batch(batch_size)
    if not batch:
        print(json.dumps({"status": "empty", "manifest": []}))
        return 0
    tmpdir = Path(tempfile.mkdtemp(prefix="d2_batch_"))
    manifest = []
    failures = []
    for entry in batch:
        cid = entry["cid"]
        url = entry["url"]
        suffix = Path(urlparse(url).path).suffix or ".jpg"
        dest = tmpdir / f"{cid}{suffix}"
        try:
            _download(url, dest)
            manifest.append({"cid": cid, "path": str(dest), "url": url})
        except Exception as exc:
            failures.append({
                "cid": cid,
                "reason": f"download_failed: {exc.__class__.__name__}: {exc}",
                "url": url,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
    if failures:
        D2_FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with D2_FAILURES.open("a", encoding="utf-8") as f:
            for row in failures:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({
        "status": "ok",
        "tmpdir": str(tmpdir),
        "manifest": manifest,
        "downloaded": len(manifest),
        "failed": len(failures),
        "schema": {
            "cid": "string",
            "style_image": list(STYLE),
            "color_tone_image": list(COLOR_TONE),
            "material_visual_image": "list[1-6 lowercase material words]",
            "visual_description_image": "string 40-90 words present tense",
        },
    }, ensure_ascii=False))
    return 0


def _validate_d2_row(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    cid = str(row.get("cid") or "")
    if not cid:
        return None, "missing cid"
    style = row.get("style_image")
    if style not in STYLE:
        return None, f"style_image={style!r} not in vocab"
    color = row.get("color_tone_image")
    if color not in COLOR_TONE:
        return None, f"color_tone_image={color!r} not in vocab"
    materials = row.get("material_visual_image")
    if not isinstance(materials, list):
        return None, "material_visual_image must be list"
    materials = [str(m).strip().lower() for m in materials if str(m).strip()][:6]
    if not materials:
        return None, "material_visual_image empty"
    desc = str(row.get("visual_description_image") or "").strip()
    if len(desc.split()) < 8:
        return None, "visual_description_image too short"
    return {
        "cid": cid,
        "style_image": style,
        "color_tone_image": color,
        "material_visual_image": materials,
        "visual_description_image": desc,
    }, None


def cmd_d2_append(result_json_path: Path) -> int:
    raw = result_json_path.read_text(encoding="utf-8")
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "error", "reason": f"json parse: {exc}"}))
        return 1
    if not isinstance(rows, list):
        print(json.dumps({"status": "error", "reason": "expected list"}))
        return 1
    appended = 0
    failed = []
    out_rows = []
    for row in rows:
        if not isinstance(row, dict):
            failed.append({"row": row, "reason": "not dict"})
            continue
        clean, err = _validate_d2_row(row)
        if err:
            failed.append({"cid": row.get("cid"), "reason": err})
            continue
        out_rows.append(clean)
        appended += 1
    if out_rows:
        D2_OUT.parent.mkdir(parents=True, exist_ok=True)
        with D2_OUT.open("a", encoding="utf-8") as f:
            for r in out_rows:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    if failed:
        D2_FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with D2_FAILURES.open("a", encoding="utf-8") as f:
            for fr in failed:
                fr["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                f.write(json.dumps(fr, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "ok", "appended": appended, "failed": len(failed)}))
    return 0


# ---------- E-2 ----------

def _build_e2_batch(batch_size: int) -> list[dict[str, Any]]:
    """Per-cid TOP 5 candidate images sorted by (rank, image_order, area, source)."""
    done = _done_cids(E2_OUT)
    pending: list[dict[str, Any]] = []
    for row in _iter_jsonl(E1_PATH):
        cid = str(row.get("cid") or "")
        if not cid or cid in done:
            continue
        clusters = row.get("best_image_per_cluster") or {}
        candidates = []
        for cluster_id, image in clusters.items():
            if not isinstance(image, dict) or not image.get("url"):
                continue
            candidates.append({
                "cluster_id": str(cluster_id),
                **{k: image.get(k) for k in ("url", "source", "kind", "image_order", "rank", "w", "h") if k in image},
            })

        def _key(c: dict) -> tuple:
            area = (c.get("w") or 0) * (c.get("h") or 0)
            return (int(c.get("rank") or 999), int(c.get("image_order") or 999), -area)

        candidates.sort(key=_key)
        pending.append({"cid": cid, "candidates": candidates[:5]})
        if len(pending) >= batch_size:
            break
    return pending


def cmd_e2_prepare(batch_size: int) -> int:
    batch = _build_e2_batch(batch_size)
    if not batch:
        print(json.dumps({"status": "empty", "manifest": []}))
        return 0
    tmpdir = Path(tempfile.mkdtemp(prefix="e2_batch_"))
    manifest = []
    failures = []
    for entry in batch:
        cid = entry["cid"]
        cid_paths = []
        # Apply filename heuristic: pre-classify drawing/aerial without Vision.
        prelabeled: dict[str, str] = {}
        for idx, cand in enumerate(entry["candidates"]):
            url = cand["url"]
            heuristic = _filename_heuristic(url, cand.get("kind"))
            if heuristic and heuristic not in prelabeled:
                prelabeled[heuristic] = url
                continue  # skip Vision for this candidate
            suffix = Path(urlparse(url).path).suffix or ".jpg"
            dest = tmpdir / f"{cid}_{idx}{suffix}"
            try:
                _download(url, dest)
                cid_paths.append({"path": str(dest), "url": url, "cluster_id": cand.get("cluster_id")})
            except Exception as exc:
                failures.append({
                    "cid": cid,
                    "url": url,
                    "reason": f"download_failed: {exc.__class__.__name__}: {exc}",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
        manifest.append({"cid": cid, "images": cid_paths, "prelabeled": prelabeled})
    if failures:
        E2_FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with E2_FAILURES.open("a", encoding="utf-8") as f:
            for row in failures:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({
        "status": "ok",
        "tmpdir": str(tmpdir),
        "manifest": manifest,
        "cids": len(manifest),
        "failed_downloads": len(failures),
        "schema": {
            "cid": "string",
            "covers_by_type": {t: "url string OR null" for t in IMAGE_TYPES},
        },
        "instructions": (
            "For each cid in manifest: classify each image in 'images' as one of "
            "(exterior|interior|detail). 'prelabeled' already covers drawing/aerial "
            "via filename heuristic — do not override. Choose the BEST representative "
            "URL for each type from the candidates and prelabeled map. Output "
            "covers_by_type with all 5 keys (null if no candidate matches)."
        ),
    }, ensure_ascii=False))
    return 0


def _validate_e2_row(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    cid = str(row.get("cid") or "")
    if not cid:
        return None, "missing cid"
    covers = row.get("covers_by_type")
    if not isinstance(covers, dict):
        return None, "covers_by_type not dict"
    clean: dict[str, str | None] = {t: None for t in IMAGE_TYPES}
    for k, v in covers.items():
        key = str(k).strip().lower()
        if key not in IMAGE_TYPES:
            return None, f"covers_by_type[{k!r}] invalid type"
        if v in (None, "", "null"):
            clean[key] = None
        elif isinstance(v, str) and v.startswith(("http://", "https://")):
            clean[key] = v
        else:
            return None, f"covers_by_type[{k!r}]={v!r} not URL"
    return {"cid": cid, "covers_by_type": clean}, None


def cmd_e2_append(result_json_path: Path) -> int:
    raw = result_json_path.read_text(encoding="utf-8")
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "error", "reason": f"json parse: {exc}"}))
        return 1
    if not isinstance(rows, list):
        print(json.dumps({"status": "error", "reason": "expected list"}))
        return 1
    appended = 0
    failed = []
    out_rows = []
    for row in rows:
        clean, err = _validate_e2_row(row if isinstance(row, dict) else {})
        if err:
            failed.append({"cid": row.get("cid") if isinstance(row, dict) else None, "reason": err})
            continue
        out_rows.append(clean)
        appended += 1
    if out_rows:
        E2_OUT.parent.mkdir(parents=True, exist_ok=True)
        with E2_OUT.open("a", encoding="utf-8") as f:
            for r in out_rows:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    if failed:
        E2_FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with E2_FAILURES.open("a", encoding="utf-8") as f:
            for fr in failed:
                fr["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                f.write(json.dumps(fr, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "ok", "appended": appended, "failed": len(failed)}))
    return 0


# ---------- stats ----------

def cmd_stats() -> int:
    print(json.dumps({
        "d1_done": sum(1 for _ in _iter_jsonl(DATA / "d1_results.jsonl")),
        "d2_done": sum(1 for _ in _iter_jsonl(D2_OUT)),
        "e2_done": sum(1 for _ in _iter_jsonl(E2_OUT)),
        "e1_total": sum(1 for _ in _iter_jsonl(E1_PATH)),
    }))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 19 D-2/E-2 image runner")
    parser.add_argument("stage", choices=["d2", "e2", "stats"])
    parser.add_argument("action", nargs="?", choices=["prepare", "append"], default=None)
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--result-json", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.stage == "stats":
        return cmd_stats()
    if args.action is None:
        print("error: action required for d2/e2", file=sys.stderr)
        return 2
    if args.stage == "d2":
        if args.action == "prepare":
            return cmd_d2_prepare(args.batch_size)
        if args.action == "append":
            if not args.result_json:
                print("error: --result-json required", file=sys.stderr)
                return 2
            return cmd_d2_append(args.result_json)
    if args.stage == "e2":
        if args.action == "prepare":
            return cmd_e2_prepare(args.batch_size)
        if args.action == "append":
            if not args.result_json:
                print("error: --result-json required", file=sys.stderr)
                return 2
            return cmd_e2_append(args.result_json)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
