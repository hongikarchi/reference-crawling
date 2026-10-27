#!/usr/bin/env python3
"""vNext WP3 N10: fetch every manifest image once (<=2 rps), save JPEG max side 1024.

Run in the vision venv (needs Pillow). Reads
data/smoke_vnext/wp3_n10_v1/sample_manifest.json and writes
images/<idx>_<sha8>.jpg (sha8 = first 8 hex of sha256 of the fetched bytes) and
fetch_log.jsonl (one row per manifest image; failures are explicit rows).
Resume: an idx whose success row + file already exist is not re-requested; a retryable
failure is retried at most once (the failed attempt is kept in prior_attempts).

Fetch profile (fetch_profile_version vnext-wp3-fetch-v1):
  Architizer: URL query replaced by ?width=1024&height=1024&fit=max&quality=85&format=jpg
  Divisare:   URL as-is
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
WP = ROOT / "data/smoke_vnext/wp3_n10_v1"
MANIFEST = WP / "sample_manifest.json"
IMG_DIR = WP / "images"
LOG = WP / "fetch_log.jsonl"
PROFILE = "vnext-wp3-fetch-v1"
ATZ_QUERY = "width=1024&height=1024&fit=max&quality=85&format=jpg"
MAX_SIDE = 1024
MIN_INTERVAL = 0.5  # seconds between requests => <= 2 rps
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def fetch_url(stratum: str, url: str) -> str:
    p = urlsplit(url)
    path = quote(p.path, safe="/%,:@!$&'()*+;=-._~")
    query = ATZ_QUERY if stratum == "architizer" else p.query
    return urlunsplit((p.scheme, p.netloc, path, query, ""))


def main() -> int:
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    done: dict[int, dict] = {}
    prior_fail: dict[int, dict] = {}
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r["status"] == "ok" and (ROOT / r["image_path"]).exists():
                    done[r["idx"]] = r
                elif r["status"] == "failed":
                    if r.get("retryable") and not r.get("prior_attempts"):
                        prior_fail[r["idx"]] = r  # retried once
                    else:
                        done[r["idx"]] = r  # terminal failure, kept as explicit row
    rows, last, n_req = [], 0.0, 0
    for b in m["buildings"]:
        for im in b["images"]:
            idx = im["idx"]
            if idx in done:
                rows.append(done[idx])
                continue
            furl = fetch_url(b["stratum"], im["url"])
            row = {"idx": idx, "canonical_bld_id": b["canonical_bld_id"], "stratum": b["stratum"],
                   "image_asset_id": im["image_asset_id"], "manifest_url": im["url"], "fetch_url": furl,
                   "fetch_profile": PROFILE}
            if idx in prior_fail:
                pf = {k: v for k, v in prior_fail[idx].items() if k in ("status", "error_code", "error", "http_status", "retryable")}
                row["prior_attempts"] = [pf]
            wait = MIN_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            n_req += 1
            try:
                req = urllib.request.Request(furl, headers={
                    "User-Agent": UA, "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.5"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    row["http_status"] = resp.status
                    row["content_type"] = resp.headers.get("Content-Type")
            except urllib.error.HTTPError as e:
                row.update(status="failed", error_code=f"http_{e.code}", http_status=e.code, retryable=e.code >= 500)
                rows.append(row)
                continue
            except Exception as e:  # network/timeout
                row.update(status="failed", error_code="network_error", error=repr(e)[:300], retryable=True)
                rows.append(row)
                continue
            row["bytes"] = len(data)
            raw_sha = hashlib.sha256(data).hexdigest()
            row["fetched_sha256"] = raw_sha
            try:
                img = Image.open(io.BytesIO(data))
                row["source_format"] = img.format
                img.load()
                row["orig_w"], row["orig_h"] = img.size
                if img.mode != "RGB":
                    if img.mode in ("RGBA", "LA", "P"):
                        img = img.convert("RGBA")
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[-1])
                        img = bg
                    else:
                        img = img.convert("RGB")
                if max(img.size) > MAX_SIDE:
                    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                out = IMG_DIR / f"{idx:03d}_{raw_sha[:8]}.jpg"
                if row["source_format"] == "JPEG" and max(row["orig_w"], row["orig_h"]) <= MAX_SIDE and Image.open(io.BytesIO(data)).mode == "RGB":
                    out.write_bytes(data)
                    row["transform"] = "verbatim_jpeg"
                else:
                    img.save(out, "JPEG", quality=92)
                    row["transform"] = "pil_convert_rgb_thumbnail1024_q92"
                row["w"], row["h"] = img.size
                row["saved_sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
                row["image_path"] = str(out.relative_to(ROOT)).replace("\\", "/")
                row["status"] = "ok"
            except Exception as e:
                row.update(status="failed", error_code="decode_error", error=repr(e)[:300], retryable=False)
            rows.append(row)
            print(f"{idx} {row['status']} {row.get('http_status')} {row.get('source_format')} {row.get('w')}x{row.get('h')}", flush=True)
    with open(LOG, "w", encoding="utf-8") as f:
        for r in sorted(rows, key=lambda r: r["idx"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    ok = sum(r["status"] == "ok" for r in rows)
    print(json.dumps({"rows": len(rows), "ok": ok, "failed": len(rows) - ok, "requests_this_run": n_req}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
