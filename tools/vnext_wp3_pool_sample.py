#!/usr/bin/env python3
"""vNext WP3 training-pool sample (vnext-wp3-pool-v1): N100 holdout list + ~1,500-image pool.

No labelling here. No Neon/R2. Network = image fetch only (<= 4 rps total). Writes ONLY under
data/smoke_vnext/wp3_pool_v1/. Frozen inputs (S6-lite jsonl, N10 manifest, legacy snapshot,
config v2) are read-only.

Stages (run in the vision venv C:/Users/user/venvs/vnext-vision from the repo root):
  plan       deterministic, no network. Writes holdout_n100_manifest.json and pool_plan.json
             (random-part images + prefilter candidate images, each with a plan_no).
  fetch      fetch every plan image once (random part + candidates), <= 4 rps total, resume-
             capable, one row per plan image in fetch_log.jsonl (failures are explicit rows;
             a retryable failure is retried once within the run).
  prefilter  local SigLIP-2 zero-shot, config v2 base (tools/vnext_s3_local_engine_config_v2.json,
             prompt_set_choice scope=v2 / medium=v2, NO tuned offsets, v2 blank rule) over every
             fetched plan image -> prefilter_scores.jsonl. Used ONLY to choose enrichment images.
  select     choose enrichment images per rare class from the scored candidates, write
             pool_manifest.json, delete fetched candidate files not selected.

Selection rule (rule_version vnext-wp3-pool-rule-v1), fixed before any fetch:
  0. Exclusions: the 10 N10 eval buildings (data/smoke_vnext/wp3_n10_v1/sample_manifest.json),
     the N100 holdout buildings, and any image whose representative_pixel_sha256 occurs in an
     N10 or N100 building (cross-building pixel duplicates cannot leak into the pool).
  1. N100 holdout (seed vnext-wp3-n100-holdout-v1): same eligibility as N10 sample rule v1
     (8<=image_count<=30, one host images.divisare.com|images.architizer.com, asset-id prefix
     agrees), N10 excluded, hash key sha256(seed|bld) ascending, 50 per stratum. Listed only.
  2. Random part (seed vnext-wp3-pool-v1): buildings with >=1 image whose images are all on
     the two hosts, not excluded; hash key sha256(seed|bld) ascending per stratum (building
     stratum = host of all its images; mixed-host buildings skipped); strata alternate
     divisare, architizer, ...; each building contributes its images in sort_order, capped at
     30; stop when >= RANDOM_MIN_BUILDINGS buildings AND >= RANDOM_TARGET_IMAGES images.
  3. Prefilter candidates (buildings not in N10/N100/random part): per rare class, images whose
     asset-filename tokens (same tokeniser as the N10 sample rule) hit the class hint list, in
     hash order sha256(seed|'cand'|asset_id), <= CAND_PER_BLD_PER_CLASS per building per class,
     <= CAND_CAP[class] per class; plus an unhinted random slice (hash order, <= 2 per
     building). Deduplicated by image_asset_id and pixel sha; hard cap MAX_CANDIDATES = 6,000.
  4. Enrichment (select): class order physical_model, rendering, out_of_scope, drawing. A
     candidate qualifies for a class iff the zero-shot v2 decision gives: rendering /
     physical_model / drawing = in_scope AND medium argmax == class; out_of_scope = not
     in_scope with reject_reason in OOS_REASONS. Rank by class score (medium prob for media,
     logit(best_reject)-logit(in_scope) for out_of_scope), tie by hash; <= ENRICH_PER_BLD per
     building per class; take ENRICH_TARGET per class; out_of_scope picks round-robin over
     reasons to spread product/people/text/other. An image is used for at most one class.
  Pool = random part (all its images, failures kept as explicit rows) + enrichment images.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import vnext_wp3_n10_sample as n10s  # noqa: E402  (asset_filename / tokens / eligibility constants)

S6 = ROOT / "data/canonical/vnext_s6_lite_building_images_v1.jsonl"
LEGACY = ROOT / "data/canonical/legacy_neon_snapshot_2026q3.jsonl"
N10_MANIFEST = ROOT / "data/smoke_vnext/wp3_n10_v1/sample_manifest.json"
CFG2 = ROOT / "tools/vnext_s3_local_engine_config_v2.json"
OUT = ROOT / "data/smoke_vnext/wp3_pool_v1"
IMG_DIR = OUT / "images"
HOLDOUT = OUT / "holdout_n100_manifest.json"
PLAN = OUT / "pool_plan.json"
FETCH_LOG = OUT / "fetch_log.jsonl"
SCORES = OUT / "prefilter_scores.jsonl"
MANIFEST = OUT / "pool_manifest.json"

SEED = "vnext-wp3-pool-v1"
HOLDOUT_SEED = "vnext-wp3-n100-holdout-v1"
RULE_VERSION = "vnext-wp3-pool-rule-v1"
HOLDOUT_PER_STRATUM = 50
RANDOM_MIN_BUILDINGS = 60
RANDOM_TARGET_IMAGES = 1250
CAP_PER_BUILDING = 30
MAX_CANDIDATES = 6000
CAND_PER_BLD_PER_CLASS = 3
CAND_CAP = {"rendering": 1100, "physical_model": 1100, "drawing": 900, "out_of_scope": 1400}
CAND_RANDOM_CAP = 2600
CAND_RANDOM_PER_BLD = 2
ENRICH_TARGET = 65
ENRICH_PER_BLD = 4
CLASS_ORDER = ["physical_model", "rendering", "out_of_scope", "drawing"]
OOS_REASONS = ["isolated_product_artwork_or_sample", "people_or_event", "text_or_logo_only",
               "non_architectural_subject", "no_project_visible", "multi_panel_without_dominant_image"]
OOS_TARGET_GROUPS = [["isolated_product_artwork_or_sample"], ["people_or_event"], ["text_or_logo_only"],
                     ["non_architectural_subject", "no_project_visible", "multi_panel_without_dominant_image"]]
HINTS = {
    "rendering": sorted({"render", "renders", "rendering", "renderings", "rendu", "rendus", "visualization",
                         "visualizations", "visualisation", "visualisations", "visualizacion", "vis", "viz",
                         "cgi", "infografia", "infografias", "infografie", "vista3d", "dvisual"}),
    "physical_model": sorted({"model", "models", "modello", "modelli", "maqueta", "maquetas", "maquette",
                              "maquettes", "maquete", "maquetes", "modell", "modelle", "plastico", "plastici",
                              "modelo", "modelos", "maketa", "mockup"}),
    "drawing": sorted(set(n10s.DRAWING_HINT_TOKENS)),
    "out_of_scope": sorted({"logo", "logos", "portrait", "portraits", "team", "staff", "chair", "chairs",
                            "lamp", "lamps", "text", "poster", "posters", "book", "books", "event", "events",
                            "opening", "ceremony", "party", "product", "products", "furniture", "people",
                            "collage", "moodboard", "brochure", "cover", "award", "awards", "invitation",
                            "flyer", "sofa", "table", "stool", "vase", "food", "dish", "ritratto", "equipo",
                            "retrato", "inauguration", "inaugurazione", "inauguracion", "workshop", "lecture"}),
}
HOSTS = n10s.HOSTS
ATZ_QUERY = "width=1024&height=1024&fit=max&quality=85&format=jpg"
FETCH_PROFILE = "vnext-wp3-fetch-v1"  # identical profile to N10 (tools/vnext_wp3_fetch.py)
MAX_SIDE = 1024
MIN_INTERVAL = 0.25  # global => <= 4 rps
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def hkey(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ====================================================================== plan
def stage_plan(args) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    by_bld: dict[str, list[dict]] = defaultdict(list)
    n_rows = 0
    with open(S6, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            n_rows += 1
            by_bld[r["canonical_bld_id"]].append({
                "image_asset_id": r["image_asset_id"], "url": r["url"], "sort_order": r["sort_order"],
                "representative_pixel_sha256": (r.get("provenance") or {}).get("representative_pixel_sha256"),
                "record_sha256": r.get("record_sha256")})
    for imgs in by_bld.values():
        imgs.sort(key=lambda i: (i["sort_order"], i["image_asset_id"]))

    n10 = load_json(N10_MANIFEST)
    n10_ids = [b["canonical_bld_id"] for b in n10["buildings"]]
    assert len(n10_ids) == 10

    def stratum_of(imgs):
        s = {HOSTS.get(urlsplit(i["url"]).hostname or "") for i in imgs}
        if len(s) != 1 or None in s:
            return None
        st = s.pop()
        if not all(i["image_asset_id"].startswith(n10s.ASSET_PREFIX[st]) for i in imgs):
            return None
        return st

    strata = {b: stratum_of(imgs) for b, imgs in by_bld.items()}

    # ---- N100 holdout (N10 sample rule v1 eligibility, pure hash order, 50/50)
    elig = {"divisare": [], "architizer": []}
    for b, imgs in by_bld.items():
        st = strata[b]
        if st and n10s.MIN_IMAGES <= len(imgs) <= n10s.MAX_IMAGES and b not in n10_ids:
            elig[st].append((hkey(HOLDOUT_SEED, b), b))
    hold = []
    for st in ("divisare", "architizer"):
        for k, b in sorted(elig[st])[:HOLDOUT_PER_STRATUM]:
            hold.append((st, k, b))
    hold_ids = [b for _, _, b in hold]
    assert len(set(hold_ids)) == 100 and not set(hold_ids) & set(n10_ids)

    legacy = {}
    want = set(hold_ids)
    with open(LEGACY, encoding="utf-8") as f:
        for line in f:
            m = re.search(r'"canonical_bld_id":\s*"(bld_\d+)"', line)
            if m and m.group(1) in want:
                d = json.loads(line)
                legacy[d["canonical_bld_id"]] = {k: d.get(k) for k in (
                    "name", "location_country", "location_city", "program", "typology_primary",
                    "project_year", "identity_source", "is_publishable", "display_cover_url")}
    input_shas = {rel(S6): sha256_file(S6), rel(N10_MANIFEST): sha256_file(N10_MANIFEST),
                  rel(LEGACY): sha256_file(LEGACY)}
    holdout_doc = {
        "manifest_version": "vnext-wp3-n100-holdout-v1", "rule_version": "vnext-wp3-n100-holdout-rule-v1",
        "seed": HOLDOUT_SEED, "builder": "tools/vnext_wp3_pool_sample.py plan",
        "builder_sha256": sha256_file(Path(__file__)),
        "rule": {"eligible": "N10 sample rule v1 eligibility: 8<=image_count<=30; all image URLs on one host in "
                             "['images.architizer.com','images.divisare.com']; asset-id prefix agrees",
                 "excluded": "the 10 N10 eval buildings",
                 "hash_key": f"sha256('{HOLDOUT_SEED}|'+canonical_bld_id) hex ascending",
                 "per_stratum": HOLDOUT_PER_STRATUM, "drawing_hint_reservation": "none (pure hash order)",
                 "fetch": "NOT fetched (list only); no_post_hoc_replacement"},
        "inputs": input_shas,
        "population_eligible": {k: len(v) for k, v in elig.items()},
        "summary": {"buildings": len(hold), "images": sum(len(by_bld[b]) for b in hold_ids),
                    "per_stratum_buildings": dict(Counter(st for st, _, _ in hold)),
                    "per_stratum_images": {st: sum(len(by_bld[b]) for s2, _, b in hold if s2 == st)
                                           for st in ("divisare", "architizer")}},
        "buildings": [{"canonical_bld_id": b, "stratum": st, "hash_key": k, "image_count": len(by_bld[b]),
                       **legacy.get(b, {}), "legacy_found": b in legacy,
                       "images": [{"image_asset_id": i["image_asset_id"], "url": i["url"], "sort_order": i["sort_order"],
                                   "representative_pixel_sha256": i["representative_pixel_sha256"]}
                                  for i in by_bld[b]]}
                      for st, k, b in hold],
    }
    if HOLDOUT.exists():
        old = load_json(HOLDOUT)
        if [b["canonical_bld_id"] for b in old["buildings"]] != hold_ids:
            print("REFUSE: existing holdout manifest has a different selection", file=sys.stderr)
            return 2
    else:
        write_json(HOLDOUT, holdout_doc)

    # ---- exclusions
    excl_blds = set(n10_ids) | set(hold_ids)
    excl_pix = {i["representative_pixel_sha256"] for b in excl_blds for i in by_bld[b]
                if i["representative_pixel_sha256"]}

    def pix_ok(i):
        return not (i["representative_pixel_sha256"] and i["representative_pixel_sha256"] in excl_pix)

    # ---- random part
    order = {"divisare": [], "architizer": []}
    for b, imgs in by_bld.items():
        st = strata[b]
        if st and b not in excl_blds:
            order[st].append((hkey(SEED, b), b))
    for st in order:
        order[st].sort()
    random_blds, n_img, it = [], 0, {"divisare": 0, "architizer": 0}
    seen_pix: set[str] = set()
    plan_items = []
    sts = ["divisare", "architizer"]
    turn = 0
    while len(random_blds) < RANDOM_MIN_BUILDINGS or n_img < RANDOM_TARGET_IMAGES:
        st = sts[turn % 2]
        turn += 1
        k, b = order[st][it[st]]
        it[st] += 1
        imgs = [i for i in by_bld[b] if pix_ok(i)]
        dropped_pix = len(by_bld[b]) - len(imgs)
        uniq = []
        for i in imgs:
            ps = i["representative_pixel_sha256"]
            if ps and ps in seen_pix:
                continue
            uniq.append(i)
            if ps:
                seen_pix.add(ps)
        take = uniq[:CAP_PER_BUILDING]
        random_blds.append({"canonical_bld_id": b, "stratum": st, "hash_key": k,
                            "image_count_total": len(by_bld[b]), "image_count_taken": len(take),
                            "dropped_excluded_pixel": dropped_pix,
                            "dropped_dup_pixel_in_pool": len(imgs) - len(uniq),
                            "capped": len(uniq) > CAP_PER_BUILDING})
        for i in take:
            plan_items.append({"plan_no": len(plan_items), "part": "random_building", "canonical_bld_id": b,
                               "stratum": st, **{k2: i[k2] for k2 in ("image_asset_id", "url", "sort_order",
                                                                      "representative_pixel_sha256")},
                               "selection_reason": "random_building"})
        n_img += len(take)
    random_ids = {x["canonical_bld_id"] for x in random_blds}

    # ---- prefilter candidates
    cand_pool = []  # (stratum, bld, image, tokens)
    for b, imgs in by_bld.items():
        st = strata[b]
        if not st or b in excl_blds or b in random_ids:
            continue
        for i in imgs:
            if not pix_ok(i):
                continue
            cand_pool.append((st, b, i, n10s.tokens(n10s.asset_filename(st, i["url"]))))
    chosen: dict[str, dict] = {}  # asset key -> cand item

    def add_cand(st, b, i, reason, toks):
        key = f"{b}|{i['image_asset_id']}"
        ps = i["representative_pixel_sha256"]
        if key in chosen:
            chosen[key]["candidate_reasons"].append(reason)
            return False
        if ps and ps in seen_pix:
            return False
        if len(chosen) >= MAX_CANDIDATES:
            return False
        if ps:
            seen_pix.add(ps)
        chosen[key] = {"canonical_bld_id": b, "stratum": st, "image_asset_id": i["image_asset_id"],
                       "url": i["url"], "sort_order": i["sort_order"],
                       "representative_pixel_sha256": ps, "candidate_reasons": [reason],
                       "hint_tokens": sorted(toks & set().union(*HINTS.values()))}
        return True

    cand_stats = {}
    for cls in CLASS_ORDER:
        hs = set(HINTS[cls])
        hits = [(hkey(SEED, "cand", b, i["image_asset_id"]), st, b, i, t) for st, b, i, t in cand_pool if t & hs]
        hits.sort(key=lambda x: x[0])
        per_b, n = Counter(), 0
        for _, st, b, i, t in hits:
            if n >= CAND_CAP[cls]:
                break
            if per_b[b] >= CAND_PER_BLD_PER_CLASS:
                continue
            if add_cand(st, b, i, f"hint:{cls}", t):
                per_b[b] += 1
                n += 1
        cand_stats[cls] = {"hinted_population": len(hits), "added": n, "buildings": len(per_b)}
    rnd = sorted(((hkey(SEED, "candrand", b, i["image_asset_id"]), st, b, i, t) for st, b, i, t in cand_pool),
                 key=lambda x: x[0])
    per_b, n = Counter(), 0
    for _, st, b, i, t in rnd:
        if n >= CAND_RANDOM_CAP or len(chosen) >= MAX_CANDIDATES:
            break
        if per_b[b] >= CAND_RANDOM_PER_BLD:
            continue
        if add_cand(st, b, i, "random_unhinted", t):
            per_b[b] += 1
            n += 1
    cand_stats["random_unhinted"] = {"population": len(rnd), "added": n, "buildings": len(per_b)}
    assert len(chosen) <= MAX_CANDIDATES
    for c in sorted(chosen.values(), key=lambda c: hkey(SEED, "cand", c["canonical_bld_id"], c["image_asset_id"])):
        plan_items.append({"plan_no": len(plan_items), "part": "prefilter_candidate", **c})

    plan = {"plan_version": "vnext-wp3-pool-plan-v1", "rule_version": RULE_VERSION, "seed": SEED,
            "holdout_seed": HOLDOUT_SEED, "builder_sha256": sha256_file(Path(__file__)),
            "inputs": input_shas, "constants": {
                "random_min_buildings": RANDOM_MIN_BUILDINGS, "random_target_images": RANDOM_TARGET_IMAGES,
                "cap_per_building": CAP_PER_BUILDING, "max_candidates": MAX_CANDIDATES,
                "cand_per_bld_per_class": CAND_PER_BLD_PER_CLASS, "cand_cap": CAND_CAP,
                "cand_random_cap": CAND_RANDOM_CAP, "cand_random_per_bld": CAND_RANDOM_PER_BLD,
                "enrich_target": ENRICH_TARGET, "enrich_per_bld": ENRICH_PER_BLD, "class_order": CLASS_ORDER,
                "oos_reasons": OOS_REASONS, "hints": HINTS},
            "excluded": {"n10_buildings": n10_ids, "n100_buildings": len(hold_ids),
                         "excluded_pixel_shas": len(excl_pix)},
            "random_buildings": random_blds, "candidate_stats": cand_stats,
            "summary": {"random_buildings": len(random_blds), "random_images": n_img,
                        "random_per_stratum_buildings": dict(Counter(x["stratum"] for x in random_blds)),
                        "candidates": len(chosen), "plan_items": len(plan_items)},
            "items": plan_items}
    if PLAN.exists():
        old = load_json(PLAN)
        if [(x["canonical_bld_id"], x["image_asset_id"]) for x in old["items"]] != \
           [(x["canonical_bld_id"], x["image_asset_id"]) for x in plan_items]:
            print("REFUSE: existing pool plan differs", file=sys.stderr)
            return 2
        print("plan exists with identical items; not rewritten")
    else:
        write_json(PLAN, plan)
    print(json.dumps(holdout_doc["summary"]))
    print(json.dumps(plan["summary"]))
    print(json.dumps(cand_stats))
    return 0


# ====================================================================== fetch
def fetch_url(stratum: str, url: str) -> str:
    from urllib.parse import quote, urlunsplit
    p = urlsplit(url)
    path = quote(p.path, safe="/%,:@!$&'()*+;=-._~")
    query = ATZ_QUERY if stratum == "architizer" else p.query
    return urlunsplit((p.scheme, p.netloc, path, query, ""))


class RateLimiter:
    def __init__(self, interval: float):
        self.interval, self.lock, self.next = interval, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.interval
        d = t - time.monotonic()
        if d > 0:
            time.sleep(d)


def fetch_one(item: dict, rl: RateLimiter) -> dict:
    from PIL import Image
    furl = fetch_url(item["stratum"], item["url"])
    row = {"plan_no": item["plan_no"], "part": item["part"], "canonical_bld_id": item["canonical_bld_id"],
           "stratum": item["stratum"], "image_asset_id": item["image_asset_id"], "manifest_url": item["url"],
           "fetch_url": furl, "fetch_profile": FETCH_PROFILE}
    rl.wait()
    row["requested_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        req = urllib.request.Request(furl, headers={
            "User-Agent": UA, "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.5"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            row["http_status"] = resp.status
            row["content_type"] = resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        row.update(status="failed", error_code=f"http_{e.code}", http_status=e.code, retryable=e.code >= 500 or e.code == 429)
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
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        out = IMG_DIR / f"p{item['plan_no']:05d}_{raw_sha[:8]}.jpg"
        if row["source_format"] == "JPEG" and max(row["orig_w"], row["orig_h"]) <= MAX_SIDE and orig_mode == "RGB":
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
    if FETCH_LOG.exists():
        for line in FETCH_LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["plan_no"]] = r  # last row wins
    return out


def stage_fetch(args) -> int:
    plan = load_json(PLAN)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    log = read_log()
    todo = []
    for it in plan["items"]:
        r = log.get(it["plan_no"])
        if r and r["status"] == "ok" and (ROOT / r["image_path"]).exists():
            continue
        if r and r["status"] == "failed" and (not r.get("retryable") or r.get("prior_attempts")):
            continue  # terminal
        todo.append(it)
    if args.limit:
        todo = todo[:args.limit]
    print(f"plan items {len(plan['items'])}; to fetch {len(todo)}", flush=True)
    rl = RateLimiter(MIN_INTERVAL)
    lock = threading.Lock()
    t0 = time.monotonic()
    n_done = Counter()

    def work(it):
        prev = log.get(it["plan_no"])
        row = fetch_one(it, rl)
        if prev and prev["status"] == "failed":
            row["prior_attempts"] = [{k: v for k, v in prev.items() if k in (
                "status", "error_code", "error", "http_status", "retryable", "requested_at")}]
        with lock:
            with open(FETCH_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            log[it["plan_no"]] = row
            n_done[row["status"]] += 1
            tot = sum(n_done.values())
            if tot % 200 == 0:
                el = time.monotonic() - t0
                print(f"{tot}/{len(todo)} {dict(n_done)} {tot / el:.2f} req/s", flush=True)
        return row

    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(work, todo))
    # in-run single retry of retryable failures
    retry = [it for it, r in zip(todo, rows) if r["status"] == "failed" and r.get("retryable")]
    if retry:
        print(f"retrying {len(retry)} retryable failures once", flush=True)
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(work, retry))
    el = time.monotonic() - t0
    final = read_log()
    st = Counter(r["status"] for r in final.values())
    print(json.dumps({"logged": len(final), "status": dict(st), "requests_this_run": sum(n_done.values()),
                      "elapsed_s": round(el, 1), "mean_rps": round(sum(n_done.values()) / max(el, 1e-9), 2)}))
    return 0


# ====================================================================== prefilter
def stage_prefilter(args) -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import numpy as np
    import torch
    import vnext_s3_local_engine as eng
    from vnext_wp3_exp_v2_embed import pixel_stats

    plan = load_json(PLAN)
    log = read_log()
    cfg = load_json(CFG2)
    assert cfg["decision"]["prompt_set_choice"]["scope"] == "v2" and cfg["decision"]["prompt_set_choice"]["medium"] == "v2"
    done = set()
    if SCORES.exists():
        for line in SCORES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["plan_no"])
    items = [(it, log[it["plan_no"]]) for it in plan["items"]
             if it["plan_no"] in log and log[it["plan_no"]]["status"] == "ok" and it["plan_no"] not in done]
    print(f"to score {len(items)}", flush=True)
    if not items:
        return 0
    device, dtype = "cuda", torch.float16
    model_id, proc, model, _ = eng.load_model(cfg, device, dtype)
    scale, bias = float(model.logit_scale.exp()), float(model.logit_bias)
    sub = {"scope": cfg["fields"]["scope"], "medium": cfg["fields"]["medium"]}
    bank, _ = eng.build_text_bank({**cfg, "fields": sub}, proc, model, device)
    br = cfg["decision"]["blank_rule"]
    rejects = [r for r in eng.REJECTS if r in bank["scope"]]  # v2: SigLIP 'unreadable' removed
    assert "unreadable_or_blank" not in rejects
    cfg_sha = sha256_file(CFG2)
    bs = 32
    with open(SCORES, "a", encoding="utf-8") as fo:
        for s in range(0, len(items), bs):
            chunk = items[s:s + bs]
            arrs, stats = [], []
            for it, r in chunk:
                p = ROOT / r["image_path"]
                a = eng.load_resized(str(p))
                arrs.append(a)
                stats.append(pixel_stats(a, str(p)))
            feats = eng.image_features(model, eng.to_pixel_values(arrs, cfg, device, dtype))
            for (it, r), f, ps in zip(chunk, feats, stats):
                sl = eng.value_logits(f, bank["scope"], scale, bias)
                ml = eng.value_logits(f, bank["medium"], scale, bias)
                blank = ps["gray_std"] <= br["gray_std_max"] and ps["edge_mean"] <= br["edge_mean_max"]
                best_rej = max(rejects, key=lambda k: (sl[k], k))
                if blank:
                    in_scope, reason = False, "unreadable_or_blank"
                else:
                    in_scope = sl["in_scope"] >= sl[best_rej] - cfg["decision"]["scope_reject_logit_margin"]
                    reason = "none" if in_scope else best_rej
                mp = eng.softmax(ml)
                med = max(mp, key=lambda k: (mp[k], k))
                fo.write(json.dumps({
                    "plan_no": it["plan_no"], "part": it["part"], "canonical_bld_id": it["canonical_bld_id"],
                    "image_asset_id": it["image_asset_id"], "pixel_sha256": r["saved_sha256"],
                    "in_scope": bool(in_scope), "reject_reason": reason, "best_reject": best_rej,
                    "scope_logit_in_scope": round(sl["in_scope"], 3), "scope_logit_best_reject": round(sl[best_rej], 3),
                    "scope_reject_logits": {k: round(sl[k], 3) for k in rejects},
                    "medium_argmax": med, "medium_probs": {k: round(v, 4) for k, v in mp.items()},
                    "blank_rule": bool(blank), "pixel_stats": ps,
                    "model": model_id, "config_sha256": cfg_sha,
                    "scorer": "zs-v2-base(scope=v2,medium=v2,no offsets,blank rule)"}) + "\n")
            if (s // bs) % 20 == 0:
                print(f"scored {s + len(chunk)}/{len(items)}", flush=True)
    print("prefilter done")
    return 0


# ====================================================================== select
def stage_select(args) -> int:
    plan = load_json(PLAN)
    log = read_log()
    scores = {}
    for line in SCORES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            scores[r["plan_no"]] = r
    items = {it["plan_no"]: it for it in plan["items"]}
    cands = [it for it in plan["items"] if it["part"] == "prefilter_candidate"]
    cand_ok = [it for it in cands if log.get(it["plan_no"], {}).get("status") == "ok"]
    assert all(it["plan_no"] in scores for it in cand_ok), "prefilter incomplete"

    def qual(cls, sc):
        if cls == "out_of_scope":
            if sc["in_scope"] or sc["reject_reason"] not in OOS_REASONS:
                return None
            return sc["scope_logit_best_reject"] - sc["scope_logit_in_scope"]
        if not sc["in_scope"] or sc["medium_argmax"] != cls:
            return None
        return sc["medium_probs"][cls]

    used: set[int] = set()
    picks: dict[str, list[int]] = {}
    pick_stats = {}
    for cls in CLASS_ORDER:
        q = []
        for it in cand_ok:
            v = qual(cls, scores[it["plan_no"]])
            if v is not None:
                q.append((-v, hkey(SEED, "sel", it["image_asset_id"]), it["plan_no"]))
        q.sort()
        per_b, out = Counter(), []
        if cls == "out_of_scope":
            queues = []
            for grp in OOS_TARGET_GROUPS:
                queues.append([x for x in q if scores[x[2]]["reject_reason"] in grp])
            ptr = [0] * len(queues)
            while len(out) < ENRICH_TARGET and any(ptr[g] < len(queues[g]) for g in range(len(queues))):
                for g in range(len(queues)):
                    while ptr[g] < len(queues[g]):
                        _, _, pn = queues[g][ptr[g]]
                        ptr[g] += 1
                        b = items[pn]["canonical_bld_id"]
                        if pn in used or per_b[b] >= ENRICH_PER_BLD:
                            continue
                        out.append(pn)
                        used.add(pn)
                        per_b[b] += 1
                        break
                    if len(out) >= ENRICH_TARGET:
                        break
        else:
            for _, _, pn in q:
                if len(out) >= ENRICH_TARGET:
                    break
                b = items[pn]["canonical_bld_id"]
                if pn in used or per_b[b] >= ENRICH_PER_BLD:
                    continue
                out.append(pn)
                used.add(pn)
                per_b[b] += 1
        picks[cls] = out
        pick_stats[cls] = {"qualifying_candidates": len(q), "selected": len(out), "buildings": len(per_b),
                           "min_score": round(min((qual(cls, scores[p]) for p in out), default=float("nan")), 4),
                           "hinted_selected": sum(any(r.startswith("hint:") for r in items[p]["candidate_reasons"]) for p in out)}
        if cls == "out_of_scope":
            pick_stats[cls]["by_reason"] = dict(Counter(scores[p]["reject_reason"] for p in out))

    # ---- manifest
    images = []
    reason_of = {p: f"enrich_{cls}" for cls, ps in picks.items() for p in ps}
    order_pn = [it["plan_no"] for it in plan["items"] if it["part"] == "random_building"]
    for cls in CLASS_ORDER:
        order_pn.extend(sorted(picks[cls], key=lambda p: (items[p]["canonical_bld_id"], items[p]["sort_order"], p)))
    for idx, pn in enumerate(order_pn):
        it, r = items[pn], log.get(pn)
        sc = scores.get(pn)
        row = {"idx": idx, "plan_no": pn, "canonical_bld_id": it["canonical_bld_id"], "stratum": it["stratum"],
               "image_asset_id": it["image_asset_id"], "url": it["url"], "sort_order": it["sort_order"],
               "representative_pixel_sha256": it["representative_pixel_sha256"],
               "selection_reason": reason_of.get(pn, "random_building"),
               "fetch_status": (r or {}).get("status", "not_fetched"),
               "path": (r or {}).get("image_path") if (r or {}).get("status") == "ok" else None,
               "pixel_sha256": (r or {}).get("saved_sha256"),
               "fetch_url": (r or {}).get("fetch_url"), "w": (r or {}).get("w"), "h": (r or {}).get("h")}
        if row["fetch_status"] != "ok":
            row["fetch_error_code"] = (r or {}).get("error_code", "missing_fetch_row")
        if it["part"] == "prefilter_candidate":
            row["candidate_reasons"] = it["candidate_reasons"]
            row["hint_tokens"] = it["hint_tokens"]
        if sc:
            row["zs_prefilter_advisory"] = {"in_scope": sc["in_scope"], "reject_reason": sc["reject_reason"],
                                            "medium_argmax": sc["medium_argmax"],
                                            "medium_top_prob": sc["medium_probs"][sc["medium_argmax"]]}
        images.append(row)

    # ---- delete unselected candidate files
    keep = {r["path"] for r in images if r["path"]}
    deleted, del_bytes = 0, 0
    if not args.no_delete:
        for it in cands:
            r = log.get(it["plan_no"])
            if r and r.get("status") == "ok" and r["image_path"] not in keep:
                p = ROOT / r["image_path"]
                if p.exists():
                    del_bytes += p.stat().st_size
                    p.unlink()
                    deleted += 1
    for r in images:
        if r["path"]:
            p = ROOT / r["path"]
            assert p.exists() and sha256_file(p) == r["pixel_sha256"], f"pixel drift {r['path']}"

    fetch_rows = list(log.values())
    disk = sum((ROOT / r["path"]).stat().st_size for r in images if r["path"])
    on_disk_files = sum(1 for _ in IMG_DIR.glob("*.jpg"))
    blds = sorted({r["canonical_bld_id"] for r in images})
    zs_adv = Counter()
    for r in images:
        a = r.get("zs_prefilter_advisory")
        if a:
            zs_adv["out_of_scope" if not a["in_scope"] else a["medium_argmax"]] += 1
    summary = {
        "buildings": len(blds), "images": len(images),
        "images_ok": sum(r["fetch_status"] == "ok" for r in images),
        "images_failed": sum(r["fetch_status"] != "ok" for r in images),
        "per_source_images": dict(Counter(r["stratum"] for r in images)),
        "per_source_buildings": dict(Counter(st for st in ({r["canonical_bld_id"]: r["stratum"] for r in images}.values()))),
        "per_selection_reason_images": dict(Counter(r["selection_reason"] for r in images)),
        "per_selection_reason_buildings": {k: len({r["canonical_bld_id"] for r in images if r["selection_reason"] == k})
                                           for k in sorted({r["selection_reason"] for r in images})},
        "random_part_buildings": plan["summary"]["random_buildings"],
        "prefilter_scanned_candidates": len(cand_ok), "prefilter_candidates_planned": len(cands),
        "prefilter_candidate_fetch_failed": len(cands) - len(cand_ok),
        "fetch_requests_logged_rows": len(fetch_rows),
        "fetch_failures_total": sum(r["status"] != "ok" for r in fetch_rows),
        "fetch_failures_by_code": dict(Counter(r.get("error_code") for r in fetch_rows if r["status"] != "ok")),
        "fetch_failures_random_part": sum(1 for r in fetch_rows if r["status"] != "ok" and r["part"] == "random_building"),
        "fetch_retried_rows": sum(1 for r in fetch_rows if r.get("prior_attempts")),
        "enrichment": pick_stats,
        "zs_v2_advisory_distribution_of_pool_images": dict(zs_adv),
        "deleted_unselected_candidate_files": deleted, "deleted_bytes": del_bytes,
        "disk_bytes_pool_images": disk, "disk_mb_pool_images": round(disk / 2**20, 1),
        "image_files_in_dir": on_disk_files,
    }
    manifest = {
        "manifest_version": "vnext-wp3-pool-v1", "rule_version": RULE_VERSION, "seed": SEED,
        "holdout_seed": HOLDOUT_SEED, "builder": "tools/vnext_wp3_pool_sample.py",
        "builder_sha256": sha256_file(Path(__file__)),
        "rule": (__doc__ or "").split("Selection rule", 1)[1].strip(),
        "constants": plan["constants"],
        "inputs": {**plan["inputs"], rel(CFG2): sha256_file(CFG2), rel(HOLDOUT): sha256_file(HOLDOUT),
                   rel(PLAN): sha256_file(PLAN)},
        "fetch_log": rel(FETCH_LOG), "fetch_log_sha256": sha256_file(FETCH_LOG),
        "prefilter_scores": rel(SCORES), "prefilter_scores_sha256": sha256_file(SCORES),
        "fetch_profile": {"version": FETCH_PROFILE, "architizer_query": ATZ_QUERY, "divisare": "url as-is",
                          "max_side": MAX_SIDE, "max_rps": round(1 / MIN_INTERVAL, 2)},
        "prefilter_scorer": "SigLIP-2 so400m zero-shot, config v2 base (scope=v2, medium=v2 prompt sets, no tuned offsets, v2 blank rule); advisory only, used only to choose enrichment images",
        "excluded": plan["excluded"],
        "candidate_stats": plan["candidate_stats"],
        "summary": summary, "random_buildings": plan["random_buildings"], "images": images,
    }
    write_json(MANIFEST, manifest)
    print(json.dumps(summary, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["plan", "fetch", "prefilter", "select"])
    ap.add_argument("--limit", type=int, default=0, help="fetch: only the first N pending items (smoke)")
    ap.add_argument("--no-delete", action="store_true")
    args = ap.parse_args()
    return {"plan": stage_plan, "fetch": stage_fetch, "prefilter": stage_prefilter, "select": stage_select}[args.stage](args)


if __name__ == "__main__":
    raise SystemExit(main())
