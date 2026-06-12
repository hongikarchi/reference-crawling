#!/usr/bin/env python3
"""R4 smoke: measure cost/quality of tagging NEW discriminative axes via LLM.

make_web's algo-support request R4 asks for new vocab-gated axes on
canonical_v2_buildings: scale / structural_system / roof_type / facade_pattern
/ era. This script smoke-tests the LLM tagging step (smoke ladder: N=10 ->
N=100 -> decision) so the full 39k run can be costed before any commitment.

- era is NOT tagged by LLM: it derives deterministically from project_year
  (bucketing below) — zero LLM cost, reported alongside.
- The four LLM axes use PROPOSED vocabularies defined here. They are a
  proposal for user review only — core/vocab.py is user-owned and untouched.
- Input is read-only Neon (publishable rows, deterministic md5-ordered
  sample). Text-only inference (name/program/typology/materials/
  visual_description), same codex-exec path as Stage D-1. "Unknown" is an
  allowed value: its rate per axis is the key quality metric (it measures
  how often text alone cannot resolve a visual attribute — high Unknown on
  roof/facade argues for a D-2-style vision pass instead).

Output: data/reports/r4_smoke/results.N<k>.jsonl + report JSON with per-axis
distributions, Unknown rate, parse-failure rate, wall-time and token estimate.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.d1_enrich_codex import extract_json, run_codex  # noqa: E402

REPORT_DIR = ROOT / "data/reports/r4_smoke"

# --- PROPOSED vocabularies (user decision pending; NOT in core/vocab.py) ----
SCALE = ("XS", "S", "M", "L", "XL")
STRUCTURAL_SYSTEM = (
    "Masonry", "Reinforced Concrete", "Steel Frame", "Timber Frame",
    "Hybrid", "Shell/Membrane", "Earth", "Unknown",
)
ROOF_TYPE = (
    "Flat", "Gabled", "Hipped", "Shed", "Curved", "Green Roof",
    "Vaulted/Domed", "Sawtooth", "Unknown",
)
FACADE_PATTERN = (
    "Grid", "Louvered", "Solid/Mass", "Glazed Curtain", "Perforated",
    "Organic", "Layered", "Rhythmic Openings", "Unknown",
)
AXES = {
    "scale": SCALE,
    "structural_system": STRUCTURAL_SYSTEM,
    "roof_type": ROOF_TYPE,
    "facade_pattern": FACADE_PATTERN,
}

ERA_BUCKETS = (
    (1900, "Pre-1900"), (1945, "1900-1945"), (1980, "1945-1980"),
    (2000, "1980-2000"), (2015, "2000-2015"), (9999, "2015+"),
)


def era_from_year(year) -> str | None:
    if not isinstance(year, int):
        return None
    for upper, label in ERA_BUCKETS:
        if year < upper:
            return label
    return None


def fetch_sample(n: int, seed: str) -> list[dict]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT canonical_bld_id, name, architect_names, location_city,
               location_country, project_year, program, typology_primary,
               style, material_visual, visual_description
        FROM canonical_v2_buildings
        WHERE is_publishable
        ORDER BY md5(canonical_bld_id || %s)
        LIMIT %s
        """,
        (seed, n),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.rollback()
    conn.close()
    return rows


def build_prompt(row: dict, retry_note: str | None = None) -> str:
    retry = f"\n\nPrevious output was invalid: {retry_note}\nReturn valid JSON only." if retry_note else ""
    return f"""You are classifying architecture projects for a recommendation database.

Return exactly one JSON object and no Markdown. Use only the allowed vocabulary values.

Required JSON schema:
{{
  "scale": one of {list(SCALE)},
  "structural_system": one of {list(STRUCTURAL_SYSTEM)},
  "roof_type": one of {list(ROOF_TYPE)},
  "facade_pattern": one of {list(FACADE_PATTERN)}
}}

Definitions:
- scale: XS pavilion/installation; S single house or small unit; M mid-size
  building (school, office floor-plate, apartment block); L large complex
  (hospital, stadium, shopping centre); XL urban-scale / masterplan.
- structural_system: the primary load-bearing system. Use Unknown when the
  text does not support a confident choice.
- roof_type / facade_pattern: dominant visible form. Use Unknown rather than
  guessing.

Rules:
- Infer from the provided text only. Do not invent unsupported facts.
- Unknown is a valid, preferred answer when evidence is missing.
- No cid, comments, code fences, or extra keys.

Project:
name: {row["name"]}
architects: {row.get("architect_names") or []}
location: {row.get("location_city")}, {row.get("location_country")}
year: {row.get("project_year")}
program: {row.get("program")} / typology: {row.get("typology_primary")}
style: {row.get("style")}
materials: {row.get("material_visual") or []}
description: {row.get("visual_description") or "(none)"}{retry}
"""


def validate(obj: dict) -> list[str]:
    errors = []
    for axis, vocab in AXES.items():
        value = obj.get(axis)
        if value not in vocab:
            errors.append(f"{axis}={value!r} not in vocab")
    extra = set(obj) - set(AXES)
    if extra:
        errors.append(f"extra keys: {sorted(extra)}")
    return errors


def run_one(row: dict) -> dict:
    t0 = time.time()
    prompt = build_prompt(row)
    out: dict = {
        "canonical_bld_id": row["canonical_bld_id"],
        "name": row["name"],
        "era": era_from_year(row.get("project_year")),
        "prompt_chars": len(prompt),
    }
    retry_note = None
    for attempt in (1, 2):
        try:
            stdout = run_codex(build_prompt(row, retry_note) if retry_note else prompt)
        except RuntimeError as exc:
            out.update(status="codex_error", error=str(exc)[:300])
            break
        out["output_chars"] = len(stdout)
        try:
            obj = extract_json(stdout)
        except Exception as exc:  # noqa: BLE001
            retry_note = f"no JSON object found ({exc})"
            out.update(status="parse_error", error=retry_note)
            continue
        errors = validate(obj)
        if errors:
            retry_note = "; ".join(errors)
            out.update(status="vocab_error", error=retry_note)
            continue
        out.update(status="ok", tags=obj, attempts=attempt)
        break
    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=10, help="sample size (smoke ladder: 10 -> 100)")
    ap.add_argument("--seed", default="r4smoke", help="sample seed (deterministic md5 order)")
    args = ap.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = REPORT_DIR / f"results.N{args.n}.jsonl"
    report_path = REPORT_DIR / f"report.N{args.n}.json"

    rows = fetch_sample(args.n, args.seed)
    print(f"sampled {len(rows)} publishable rows (seed={args.seed})", file=sys.stderr)

    results = []
    with results_path.open("w", encoding="utf-8") as fout:
        for i, row in enumerate(rows, 1):
            result = run_one(row)
            results.append(result)
            fout.write(json.dumps(result, ensure_ascii=False) + "\n")
            fout.flush()
            print(f"  {i}/{len(rows)} {result['status']} {result['elapsed_s']}s "
                  f"{result.get('tags', result.get('error', ''))}", file=sys.stderr)

    ok = [r for r in results if r["status"] == "ok"]
    dist = {axis: dict(Counter(r["tags"][axis] for r in ok).most_common()) for axis in AXES}
    unknown_rate = {
        axis: round(sum(1 for r in ok if r["tags"][axis] == "Unknown") / len(ok), 3) if ok else None
        for axis in AXES
    }
    elapsed = [r["elapsed_s"] for r in results]
    # rough token estimate: chars/4 per side
    in_tokens = sum(r.get("prompt_chars", 0) for r in results) / 4
    out_tokens = sum(r.get("output_chars", 0) for r in results) / 4
    report = {
        "mode": "r4-smoke",
        "n": len(rows),
        "seed": args.seed,
        "generated": datetime.now(timezone.utc).isoformat(),
        "status_counts": dict(Counter(r["status"] for r in results)),
        "ok_rate": round(len(ok) / len(results), 3) if results else None,
        "retry_rate": round(sum(1 for r in ok if r.get("attempts", 1) > 1) / len(ok), 3) if ok else None,
        "distributions": dist,
        "unknown_rate": unknown_rate,
        "era_coverage": round(
            sum(1 for r in results if r.get("era")) / len(results), 3) if results else None,
        "elapsed_s": {
            "total": round(sum(elapsed), 1),
            "mean": round(statistics.mean(elapsed), 2) if elapsed else None,
            "p90": round(sorted(elapsed)[int(0.9 * (len(elapsed) - 1))], 2) if elapsed else None,
        },
        "est_tokens_per_item": {
            "in": round(in_tokens / len(results)) if results else None,
            "out": round(out_tokens / len(results)) if results else None,
        },
        "extrapolation_39k": {
            "items": 39478,
            "est_total_in_tokens_M": round(in_tokens / len(results) * 39478 / 1e6, 1) if results else None,
            "est_total_hours_serial": round(
                statistics.mean(elapsed) * 39478 / 3600, 1) if elapsed else None,
        },
        "results_path": str(results_path.relative_to(ROOT)),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
