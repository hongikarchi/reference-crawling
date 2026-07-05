#!/usr/bin/env python3
"""Prep + merge steps for the Fable re-review of the repick_chunk1k cover swaps.

Three modes, all Neon-write-free:

  --fetch-meta     SELECT-only building metadata for the 273 confirmed.jsonl ids
                   via the read-only make_web role (.env.make-web)
                   -> data/reports/cover_audit/repick_chunk1k/building_meta.json
  --fetch-images   download the 273 A/B pairs (546 images) into --dest with a
                   durable status manifest -> image_manifest.json
                   (pairs with a failed side are excluded from LLM judging and
                   routed to the user_review bucket downstream)
  --merge-recheck  merge per-batch judge fragment JSONLs from --fragments,
                   resolve pass-2 arbitration, validate
                   -> fable_recheck.jsonl

The judge itself runs as in-session subagents (see job card
20260705_cover_repick_rereview.md), not through this tool.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_DIR = ROOT / "data/reports/cover_audit/repick_chunk1k"
CONFIRMED_PATH = REPORT_DIR / "confirmed.jsonl"
META_PATH = REPORT_DIR / "building_meta.json"
MANIFEST_PATH = REPORT_DIR / "image_manifest.json"
RECHECK_PATH = REPORT_DIR / "fable_recheck.jsonl"

VERDICTS = {"swap", "keep", "interior_exception", "both_bad"}
CONFIDENCES = {"high", "medium", "low"}
# no_swap ordering when two judges disagree within the same action:
# interior_exception is the informative tag, both_bad the most drastic claim.
NO_SWAP_PREFERENCE = ["interior_exception", "keep", "both_bad"]
CONF_RANK = {"low": 0, "medium": 1, "high": 2}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        tmp = Path(f.name)
    tmp.replace(path)


def load_confirmed() -> list[dict]:
    rows = [json.loads(l) for l in CONFIRMED_PATH.read_text(encoding="utf-8").splitlines() if l]
    ids = [r["canonical_bld_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("confirmed.jsonl has duplicate canonical_bld_id rows")
    return rows


def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# --------------------------------------------------------------------- meta
def fetch_meta() -> int:
    import psycopg2

    rows = load_confirmed()
    ids = [r["canonical_bld_id"] for r in rows]
    env = read_env_file(ROOT / ".env.make-web")
    conn = psycopg2.connect(
        host=env["BUILDINGS_DB_HOST"],
        dbname=env["BUILDINGS_DB_NAME"],
        user=env["BUILDINGS_DB_USER"],
        password=env["BUILDINGS_DB_PASSWORD"],
        sslmode=env.get("BUILDINGS_DB_SSLMODE", "require"),
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT canonical_bld_id, name, typology_primary, program,
                   location_city, location_country, project_year
            FROM canonical_v2_buildings
            WHERE canonical_bld_id = ANY(%s)
            """,
            (ids,),
        )
        meta = {
            r[0]: {
                "name": r[1],
                "typology_primary": r[2],
                "program": r[3],
                "location_city": r[4],
                "location_country": r[5],
                "project_year": r[6],
            }
            for r in cur.fetchall()
        }
    finally:
        conn.close()

    missing = sorted(set(ids) - set(meta))
    atomic_write_json(META_PATH, {
        "generated_at": now_iso(),
        "source": "canonical_v2_buildings via .env.make-web (SELECT only)",
        "requested": len(ids),
        "found": len(meta),
        "missing_ids": missing,
        "rows": meta,
    })
    print(f"meta: {len(meta)}/{len(ids)} found -> {META_PATH}")
    if missing:
        print(f"MISSING ids: {missing}", file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------- images
def _download_one(url: str, dest: Path, attempts: int = 3) -> tuple[str, int, str]:
    """Return (status, bytes, error). Skips if dest already exists non-empty."""
    import requests

    if dest.exists() and dest.stat().st_size > 0:
        return "ok", dest.stat().st_size, ""
    delays = [2, 5, 15]
    last_err = ""
    for i in range(attempts):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                                timeout=30, allow_redirects=True, stream=True)
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            if tmp.stat().st_size == 0:
                raise RuntimeError("empty body")
            tmp.replace(dest)
            return "ok", dest.stat().st_size, ""
        except Exception as exc:  # noqa: BLE001 — every failure retries the same way
            last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
            if i < attempts - 1:
                time.sleep(delays[i])
    return "failed", 0, last_err


def fetch_images(dest_dir: Path, workers: int) -> int:
    rows = load_confirmed()
    dest_dir.mkdir(parents=True, exist_ok=True)

    jobs = []  # (bld_id, side, url, dest_path)
    for r in rows:
        for side, url in (("A", r["current_cover"]), ("B", r["proposed_cover"])):
            ext = Path(urlparse(url).path).suffix or ".jpg"
            jobs.append((r["canonical_bld_id"], side, url, dest_dir / f"{r['canonical_bld_id']}_{side}{ext}"))

    pairs: dict[str, dict] = {r["canonical_bld_id"]: {} for r in rows}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_download_one, url, dest): (bld, side, dest)
                for (bld, side, url, dest) in jobs}
        for fut in as_completed(futs):
            bld, side, dest = futs[fut]
            status, nbytes, err = fut.result()
            key = side.lower()
            pairs[bld][f"{key}_status"] = status
            pairs[bld][f"{key}_path"] = str(dest)
            pairs[bld][f"{key}_bytes"] = nbytes
            if err:
                pairs[bld][f"{key}_error"] = err
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(jobs)} downloaded")

    ok_pairs = sum(1 for p in pairs.values()
                   if p.get("a_status") == "ok" and p.get("b_status") == "ok")
    failed_ids = sorted(b for b, p in pairs.items()
                        if p.get("a_status") != "ok" or p.get("b_status") != "ok")
    atomic_write_json(MANIFEST_PATH, {
        "generated_at": now_iso(),
        "dest": str(dest_dir),
        "pairs_total": len(pairs),
        "pairs_ok": ok_pairs,
        "pairs_failed": len(failed_ids),
        "failed_ids": failed_ids,
        "pairs": pairs,
    })
    print(f"images: {ok_pairs}/{len(pairs)} pairs complete -> {MANIFEST_PATH}")
    if failed_ids:
        print(f"failed pairs (will route to user_review): {failed_ids}", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ merge
def _action(verdict: str) -> str:
    return "swap" if verdict == "swap" else "no_swap"


def _load_fragments(frag_dir: Path, prefix: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(frag_dir.glob(f"{prefix}_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            bid = rec["canonical_bld_id"]
            if bid in out:
                raise SystemExit(f"{prefix}: duplicate judgement for {bid} (in {path.name})")
            rec["_batch"] = path.stem
            out[bid] = rec
    return out


def _validate_judgement(rec: dict, where: str) -> None:
    if rec.get("verdict") not in VERDICTS:
        raise SystemExit(f"{where}: bad verdict {rec.get('verdict')!r} for {rec.get('canonical_bld_id')}")
    if rec.get("confidence") not in CONFIDENCES:
        raise SystemExit(f"{where}: bad confidence {rec.get('confidence')!r} for {rec.get('canonical_bld_id')}")
    if not (rec.get("reason_ko") or "").strip():
        raise SystemExit(f"{where}: empty reason_ko for {rec.get('canonical_bld_id')}")


def _trigger(haiku_better: bool, p1: dict) -> str | None:
    if (haiku_better and _action(p1["verdict"]) != "swap") or \
       (not haiku_better and _action(p1["verdict"]) == "swap"):
        return "conflict"
    if p1["confidence"] == "low":
        return "low_conf"
    if p1["verdict"] in ("interior_exception", "both_bad"):
        return "category"
    return None


def _resolve(p1: dict, p2: dict | None) -> dict:
    if p2 is None:
        return {
            "verdict": p1["verdict"], "action": _action(p1["verdict"]),
            "confidence": p1["confidence"], "judges_agree": None,
            "reason_ko": p1["reason_ko"],
        }
    a1, a2 = _action(p1["verdict"]), _action(p2["verdict"])
    if a1 != a2:
        return {
            "verdict": p1["verdict"], "action": a1,
            "confidence": "low", "judges_agree": False,
            "reason_ko": f"판정 불일치 — 1차: {p1['reason_ko']} / 2차: {p2['reason_ko']}",
        }
    if p1["verdict"] == p2["verdict"]:
        verdict, reason = p1["verdict"], p1["reason_ko"]
    else:  # same action (no_swap), different tag — take the preferred tag's reason
        verdict = min((p1["verdict"], p2["verdict"]), key=NO_SWAP_PREFERENCE.index)
        reason = p1["reason_ko"] if p1["verdict"] == verdict else p2["reason_ko"]
    conf = min((p1["confidence"], p2["confidence"]), key=lambda c: CONF_RANK[c])
    return {
        "verdict": verdict, "action": a1, "confidence": conf,
        "judges_agree": True, "reason_ko": reason,
    }


def merge_recheck(frag_dir: Path) -> int:
    confirmed = load_confirmed()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    pass1 = _load_fragments(frag_dir, "pass1")
    pass2 = _load_fragments(frag_dir, "pass2")

    lines = []
    merged_at = now_iso()
    for row in confirmed:
        bid = row["canonical_bld_id"]
        pair = manifest["pairs"].get(bid) or {}
        img_status = {"current": pair.get("a_status", "failed"),
                      "proposed": pair.get("b_status", "failed")}
        unjudgeable = img_status["current"] != "ok" or img_status["proposed"] != "ok"

        if unjudgeable:
            if bid in pass1:
                raise SystemExit(f"{bid}: judged despite failed image download")
            rec = {
                "canonical_bld_id": bid, "pass1": None, "pass2": None,
                "second_pass_trigger": None,
                "final": {"verdict": "unjudgeable", "action": "unjudgeable",
                          "confidence": None, "judges_agree": None,
                          "reason_ko": "이미지 다운로드 실패 — 직접 확인 필요"},
                "image_status": img_status, "batch_id": None, "judged_at": merged_at,
            }
            lines.append(rec)
            continue

        p1 = pass1.get(bid)
        if p1 is None:
            raise SystemExit(f"{bid}: missing pass1 judgement")
        _validate_judgement(p1, "pass1")
        trigger = _trigger(bool(row["better"]), p1)
        p2 = pass2.get(bid)
        if trigger and p2 is None:
            raise SystemExit(f"{bid}: trigger={trigger} but no pass2 judgement")
        if p2 is not None:
            _validate_judgement(p2, "pass2")

        def slim(rec: dict | None) -> dict | None:
            if rec is None:
                return None
            return {"verdict": rec["verdict"], "confidence": rec["confidence"],
                    "reason_ko": rec["reason_ko"]}

        lines.append({
            "canonical_bld_id": bid,
            "pass1": slim(p1), "pass2": slim(p2),
            "second_pass_trigger": trigger,
            "final": _resolve(p1, p2),
            "image_status": img_status,
            "batch_id": p1["_batch"],
            "judged_at": merged_at,
        })

    ids = [l["canonical_bld_id"] for l in lines]
    if len(ids) != len(set(ids)) or len(ids) != len(confirmed):
        raise SystemExit(f"merge produced {len(ids)} rows for {len(confirmed)} inputs")

    RECHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECHECK_PATH.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    tmp.replace(RECHECK_PATH)

    from collections import Counter
    verdicts = Counter(l["final"]["verdict"] for l in lines)
    print(f"merged {len(lines)} rows -> {RECHECK_PATH}")
    print(f"final verdicts: {dict(verdicts)}")
    print(f"pass2 run on {sum(1 for l in lines if l['pass2'])} rows; "
          f"judges disagreed on {sum(1 for l in lines if l['final']['judges_agree'] is False)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch-meta", action="store_true")
    ap.add_argument("--fetch-images", action="store_true")
    ap.add_argument("--dest", type=Path, help="image download dir (with --fetch-images)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--merge-recheck", action="store_true")
    ap.add_argument("--fragments", type=Path, help="judge fragment dir (with --merge-recheck)")
    args = ap.parse_args()

    if args.fetch_meta:
        return fetch_meta()
    if args.fetch_images:
        if not args.dest:
            ap.error("--fetch-images requires --dest")
        return fetch_images(args.dest, args.workers)
    if args.merge_recheck:
        if not args.fragments:
            ap.error("--merge-recheck requires --fragments")
        return merge_recheck(args.fragments)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
