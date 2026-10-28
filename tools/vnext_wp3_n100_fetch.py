#!/usr/bin/env python3
"""vNext WP3 N100: fetch the 100 holdout buildings once (<= 4 rps), same fetch profile as the WP3 pool/N10.

Input  (read-only): data/smoke_vnext/wp3_pool_v1/holdout_n100_manifest.json (100 buildings / 1,656 images).
Output (new):       data/smoke_vnext/wp3_n100_v1/images/h<idx:04d>_<sha8>.jpg + fetch_log.jsonl
                    + fetch_run.json (counts, rps, failures).

idx rule (pre-registered, data/smoke_vnext/wp3_n100_v1/preregistration.json): idx = 0-based position of
the image in manifest order (buildings in listed order, images in listed order within each building).
Fetch profile `vnext-wp3-fetch-v1` (identical to tools/vnext_wp3_pool_sample.py / tools/vnext_wp3_fetch.py):
  Architizer: URL query replaced by ?width=1024&height=1024&fit=max&quality=85&format=jpg; Divisare: URL as-is;
  saved JPEG max side 1024 (verbatim when the source is an RGB JPEG <= 1024, else PIL RGB thumbnail q92).
Rate: one global limiter, 0.25 s between request starts (<= 4 rps), 6 worker threads.
Failures are explicit rows; a retryable failure (5xx / 429 / network) is retried once in the run
(prior attempt kept in prior_attempts). Resume: an idx with an ok row + file is never re-requested;
terminal failures are not re-requested. No post-hoc replacement of failed images.
Run in the vision venv (Pillow). No Neon/R2, no model calls.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import vnext_wp3_pool_sample as ps  # noqa: E402  (fetch_url / RateLimiter / profile constants, unchanged)

MANIFEST = ROOT / "data/smoke_vnext/wp3_pool_v1/holdout_n100_manifest.json"
OUT = ROOT / "data/smoke_vnext/wp3_n100_v1"
IMG_DIR = OUT / "images"
LOG = OUT / "fetch_log.jsonl"
PREREG = OUT / "preregistration.json"
PREREG_SHA = OUT / "preregistration.sha256"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


def items_from_manifest() -> list[dict]:
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    out, idx = [], 0
    for b in m["buildings"]:
        for im in b["images"]:
            out.append({"idx": idx, "canonical_bld_id": b["canonical_bld_id"], "stratum": b["stratum"],
                        "image_asset_id": im["image_asset_id"], "url": im["url"], "sort_order": im["sort_order"],
                        "representative_pixel_sha256": im.get("representative_pixel_sha256")})
            idx += 1
    return out


def fetch_one(it: dict, rl) -> dict:
    from PIL import Image
    furl = ps.fetch_url(it["stratum"], it["url"])
    row = {"idx": it["idx"], "canonical_bld_id": it["canonical_bld_id"], "stratum": it["stratum"],
           "image_asset_id": it["image_asset_id"], "sort_order": it["sort_order"], "manifest_url": it["url"],
           "fetch_url": furl, "fetch_profile": ps.FETCH_PROFILE}
    rl.wait()
    row["requested_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        req = urllib.request.Request(furl, headers={
            "User-Agent": ps.UA, "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.5"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            row["http_status"] = resp.status
            row["content_type"] = resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        row.update(status="failed", error_code=f"http_{e.code}", http_status=e.code,
                   retryable=e.code >= 500 or e.code == 429)
        return row
    except Exception as e:  # network / timeout
        row.update(status="failed", error_code="network_error", error=repr(e)[:300], retryable=True)
        return row
    row["bytes"] = len(data)
    raw_sha = hashlib.sha256(data).hexdigest()
    row["fetched_sha256"] = raw_sha
    try:
        img = Image.open(io.BytesIO(data))
        row["source_format"] = img.format
        img.load()
        row["orig_w"], row["orig_h"] = img.size
        orig_mode = img.mode
        if img.mode != "RGB":
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
        if max(img.size) > ps.MAX_SIDE:
            img.thumbnail((ps.MAX_SIDE, ps.MAX_SIDE), Image.Resampling.LANCZOS)
        out = IMG_DIR / f"h{it['idx']:04d}_{raw_sha[:8]}.jpg"
        if row["source_format"] == "JPEG" and max(row["orig_w"], row["orig_h"]) <= ps.MAX_SIDE and orig_mode == "RGB":
            out.write_bytes(data)
            row["transform"] = "verbatim_jpeg"
        else:
            img.save(out, "JPEG", quality=92)
            row["transform"] = "pil_convert_rgb_thumbnail1024_q92"
        row["w"], row["h"] = img.size
        row["saved_sha256"] = sha256_file(out)
        row["saved_bytes"] = out.stat().st_size
        row["image_path"] = rel(out)
        row["status"] = "ok"
    except Exception as e:
        row.update(status="failed", error_code="decode_error", error=repr(e)[:300], retryable=False)
    return row


def read_log() -> dict[int, dict]:
    out = {}
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["idx"]] = r  # last row wins
    return out


def main() -> int:
    # fail closed: the pre-registration must exist and match its recorded SHA before any holdout request
    assert PREREG.exists() and PREREG_SHA.exists(), "preregistration missing - refuse to fetch"
    rec = PREREG_SHA.read_text(encoding="utf-8").split()[0]
    assert sha256_file(PREREG) == rec, "preregistration SHA drift - refuse to fetch"
    items = items_from_manifest()
    assert len(items) == 1656, len(items)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    log = read_log()
    todo = []
    for it in items:
        r = log.get(it["idx"])
        if r and r["status"] == "ok" and (ROOT / r["image_path"]).exists():
            continue
        if r and r["status"] == "failed" and (not r.get("retryable") or r.get("prior_attempts")):
            continue
        todo.append(it)
    print(f"items {len(items)}; to fetch {len(todo)}", flush=True)
    rl = ps.RateLimiter(ps.MIN_INTERVAL)
    lock = threading.Lock()
    t0 = time.monotonic()
    n_done = Counter()

    def work(it):
        prev = log.get(it["idx"])
        row = fetch_one(it, rl)
        if prev and prev["status"] == "failed":
            row["prior_attempts"] = [{k: v for k, v in prev.items() if k in (
                "status", "error_code", "error", "http_status", "retryable", "requested_at")}]
        with lock:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            log[it["idx"]] = row
            n_done[row["status"]] += 1
            tot = sum(n_done.values())
            if tot % 200 == 0:
                print(f"{tot}/{len(todo)} {dict(n_done)} {tot / (time.monotonic() - t0):.2f} req/s", flush=True)
        return row

    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(work, todo))
    retry = [it for it, r in zip(todo, rows) if r["status"] == "failed" and r.get("retryable")]
    if retry:
        print(f"retrying {len(retry)} retryable failures once", flush=True)
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(work, retry))
    el = time.monotonic() - t0
    final = read_log()
    fails = [r for r in final.values() if r["status"] != "ok"]
    doc = {"version": "vnext-wp3-n100-fetch-v1", "tool": "tools/vnext_wp3_n100_fetch.py",
           "tool_sha256": sha256_file(Path(__file__)), "manifest": rel(MANIFEST), "manifest_sha256": sha256_file(MANIFEST),
           "preregistration_sha256": rec, "fetch_profile": ps.FETCH_PROFILE, "max_rps": round(1 / ps.MIN_INTERVAL, 2),
           "items": len(items), "logged": len(final), "status": dict(Counter(r["status"] for r in final.values())),
           "per_stratum_ok": dict(Counter(r["stratum"] for r in final.values() if r["status"] == "ok")),
           "requests_this_run": sum(n_done.values()), "retried_rows": sum(1 for r in final.values() if r.get("prior_attempts")),
           "elapsed_s_this_run": round(el, 1), "mean_rps_this_run": round(sum(n_done.values()) / max(el, 1e-9), 2),
           "failures": [{k: r.get(k) for k in ("idx", "canonical_bld_id", "error_code", "http_status", "fetch_url")}
                        for r in sorted(fails, key=lambda r: r["idx"])],
           "bytes_saved_total": sum(r.get("saved_bytes", 0) for r in final.values() if r["status"] == "ok"),
           "fetch_log_sha256_at_end": sha256_file(LOG)}
    (OUT / "fetch_run.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: doc[k] for k in ("logged", "status", "requests_this_run", "elapsed_s_this_run",
                                          "mean_rps_this_run", "retried_rows")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
