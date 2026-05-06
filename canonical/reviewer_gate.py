"""Phase 15 reviewer gate — blocking QC between pipeline stages.

Run after each stage produces an artefact. Verdict ∈ {PASS, WARN, BLOCK}.
On BLOCK, writes a diagnosis file at .claude/escalations/<stage>_<ts>.md
that the responsible team's Codex session can read to fix the root cause.

Usage:
    python3 -m canonical.reviewer_gate --stage A --artefact data/canonical/architects_canonical.json
    python3 -m canonical.reviewer_gate --stage B --artefact data/canonical/canonical_buildings_4source.json
    python3 -m canonical.reviewer_gate --stage D --artefact data/canonical/d1_results_t2/
    python3 -m canonical.reviewer_gate --stage F --artefact data/canonical/canonical_buildings_strict.json

Exit codes:
    0  PASS  — artefact may proceed to next stage
    1  WARN  — proceed with logged concern
    2  BLOCK — must NOT proceed
    3  error — gate itself failed (treat as BLOCK by orchestrator)

Stage invariants are documented in .claude/agents/team-reviewer.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


ESCALATION_DIR = ".claude/escalations"
PHASH_CACHE_PATH = "data/canonical/phash_cache.json"


@dataclass
class Finding:
    """A single invariant result."""

    invariant: str
    verdict: str          # PASS | WARN | BLOCK
    summary: str
    samples: list[Any] = field(default_factory=list)   # offending row ids
    suggested_fix: str = ""


@dataclass
class GateResult:
    stage: str
    cycle: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(f.verdict == "BLOCK" for f in self.findings):
            return "BLOCK"
        if any(f.verdict == "WARN" for f in self.findings):
            return "WARN"
        return "PASS"


# ---------------------------------------------------------------------------
# Stage B invariants
# ---------------------------------------------------------------------------


def _stage_b_year_span(buildings: list[dict]) -> Finding:
    """Multi-source clusters with year_span > 2 are likely series-merge.
    Catches Serpentine Pavilion 2015/2016/2017 etc. where all share
    architect + venue but are different annual buildings."""
    bad: list[dict] = []
    for b in buildings:
        if b.get("n_sources", 0) < 2:
            continue
        years = b.get("years_per_source") or {}
        ys = [y for y in years.values() if isinstance(y, int)]
        if len(ys) < 2:
            continue
        if max(ys) - min(ys) > 2:
            bad.append({"cid": b.get("canonical_bld_id"),
                        "name": b.get("primary_name"),
                        "years": years})
    if bad:
        return Finding(
            invariant="stage_b/year_span_max_2",
            verdict="BLOCK",
            summary=f"{len(bad)} multi-source clusters span > 2 years (likely series-merge)",
            samples=bad[:10],
            suggested_fix=("In canonical/match_buildings_sequential.py: when "
                           "candidate clusters have year diff > 2, require additional "
                           "signal (city EXACT match AND name diff > token swap) "
                           "before auto-accept. Treat as separate buildings otherwise."),
        )
    return Finding(
        invariant="stage_b/year_span_max_2",
        verdict="PASS",
        summary="all multi-source clusters within 2-year span",
    )


def _stage_b_city_consistency(buildings: list[dict]) -> Finding:
    """Multi-source clusters where source-claimed cities are different
    (string-normalized) signal a likely false-merge across cities."""
    def norm(s: Optional[str]) -> str:
        return (s or "").strip().lower()

    bad: list[dict] = []
    for b in buildings:
        if b.get("n_sources", 0) < 2:
            continue
        cities = b.get("cities_per_source") or {}
        unique = {norm(c) for c in cities.values() if c}
        unique.discard("")
        if len(unique) > 1:
            bad.append({"cid": b.get("canonical_bld_id"),
                        "name": b.get("primary_name"),
                        "cities_per_source": cities})
    if bad:
        return Finding(
            invariant="stage_b/city_consistency",
            verdict="BLOCK",
            summary=f"{len(bad)} multi-source clusters have conflicting city per source",
            samples=bad[:10],
            suggested_fix=("Likely architect-name false-merge across continents. "
                           "In match_buildings_sequential.py, gate auto-accept on city "
                           "string equality (case-insensitive, after normalization). "
                           "Allow exception only when name is a globally-unique "
                           "architect-firm phrase (e.g. 'Burj Khalifa')."),
        )
    return Finding(
        invariant="stage_b/city_consistency",
        verdict="PASS",
        summary="all multi-source cluster cities consistent",
    )


def _stage_b_phash_overlap(buildings: list[dict]) -> Finding:
    """If phash_cache.json exists, every multi-source cluster must have
    at least one phash overlap between its source image sets. Zero
    overlap with non-trivial source image counts on both sides = BLOCK.

    NOTE: requires canonical/match_phash_check.py and a built phash_cache.
    Until the cache is built, this invariant returns WARN-skip."""
    if not os.path.exists(PHASH_CACHE_PATH):
        return Finding(
            invariant="stage_b/phash_overlap",
            verdict="WARN",
            summary=f"phash cache not built ({PHASH_CACHE_PATH} missing) — skipping",
            suggested_fix=("Run: python3 -m canonical.phash_cache --build  "
                           "(one-time, ~8h, $0)"),
        )
    try:
        from canonical.match_phash_check import has_phash_overlap
    except Exception as exc:
        return Finding(
            invariant="stage_b/phash_overlap",
            verdict="WARN",
            summary=f"match_phash_check not importable ({exc}) — skipping",
        )

    bad: list[dict] = []
    for b in buildings:
        if b.get("n_sources", 0) < 2:
            continue
        srcs = b.get("source_refs") or {}
        names = list(srcs.keys())
        # Check pairwise overlap; need ≥1 PASS-or-insufficient verdict per pair
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                res = has_phash_overlap(srcs[names[i]], srcs[names[j]],
                                        src_a=names[i], src_b=names[j])
                if res.get("verdict") == "BLOCK":
                    bad.append({"cid": b.get("canonical_bld_id"),
                                "name": b.get("primary_name"),
                                "pair": (names[i], names[j]),
                                "overlap": res.get("overlap"),
                                "a_n": res.get("a_n"),
                                "b_n": res.get("b_n")})
                    break  # one BLOCK per cluster is enough
            else:
                continue
            break
    if bad:
        return Finding(
            invariant="stage_b/phash_overlap",
            verdict="BLOCK",
            summary=f"{len(bad)} multi-source clusters have 0 phash overlap (likely false-merge)",
            samples=bad[:10],
            suggested_fix=("Cluster's sources show no shared images — strongly "
                           "suggests different buildings. Split via "
                           "canonical/registry.py and re-run match_buildings_sequential."),
        )
    return Finding(
        invariant="stage_b/phash_overlap",
        verdict="PASS",
        summary="all multi-source clusters have ≥1 phash overlap (or insufficient images)",
    )


# ---------------------------------------------------------------------------
# Stage D invariants (text enrichment vocab compliance)
# ---------------------------------------------------------------------------


def _stage_d_vocab_compliance(results_dir: str) -> list[Finding]:
    """Every batch_*.json in results_dir must have all rows pass vocab."""
    try:
        from core.vocab import PROGRAM, STYLE, COLOR_TONE, ATMOSPHERE
    except Exception as exc:
        return [Finding("stage_d/vocab_load",
                        "BLOCK", f"core.vocab not importable: {exc}")]

    sets = [("program", PROGRAM), ("style", STYLE),
            ("color_tone", COLOR_TONE), ("atmosphere", ATMOSPHERE)]

    bad: list[dict] = []
    for fn in sorted(os.listdir(results_dir)):
        if not fn.startswith("batch_") or not fn.endswith(".json"):
            continue
        try:
            rows = json.load(open(os.path.join(results_dir, fn)))
        except Exception:
            continue
        for r in rows:
            for k, vset in sets:
                v = r.get(k)
                if v is None:
                    bad.append({"file": fn, "cid": r.get("cid"),
                                "field": k, "value": None, "issue": "null"})
                elif v not in vset:
                    bad.append({"file": fn, "cid": r.get("cid"),
                                "field": k, "value": v, "issue": "out-of-vocab"})

    findings = []
    if bad:
        findings.append(Finding(
            invariant="stage_d/vocab_compliance",
            verdict="BLOCK",
            summary=f"{len(bad)} field violations across {len({b['file'] for b in bad})} batches",
            samples=bad[:10],
            suggested_fix=("Edit prompt in tools/build_d1_batches.py header to "
                           "ENFORCE vocab. Re-run only the failing batches "
                           "(reuse cid → batch mapping)."),
        ))
    else:
        findings.append(Finding(
            invariant="stage_d/vocab_compliance",
            verdict="PASS",
            summary="100% of fields ∈ vocab enums",
        ))
    return findings


def _stage_d_visual_description_length(results_dir: str) -> Finding:
    """visual_description should be 50-200 words. Outside → BLOCK."""
    bad: list[dict] = []
    for fn in sorted(os.listdir(results_dir)):
        if not fn.startswith("batch_") or not fn.endswith(".json"):
            continue
        try:
            rows = json.load(open(os.path.join(results_dir, fn)))
        except Exception:
            continue
        for r in rows:
            vd = r.get("visual_description") or ""
            n = len(vd.split())
            if not (50 <= n <= 200):
                bad.append({"file": fn, "cid": r.get("cid"), "words": n})
    if bad:
        return Finding(
            invariant="stage_d/visual_description_length",
            verdict="BLOCK",
            summary=f"{len(bad)} entries outside [50,200] word range",
            samples=bad[:10],
            suggested_fix="Re-prompt the failing entries with explicit length floor/ceiling.",
        )
    return Finding(
        invariant="stage_d/visual_description_length",
        verdict="PASS",
        summary="all visual_descriptions within [50,200] word range",
    )


# ---------------------------------------------------------------------------
# Stage A + F (lighter for now; expand in iterations)
# ---------------------------------------------------------------------------


def _stage_a_basic(arch: list[dict]) -> list[Finding]:
    """Stage A: architect canonical clusters."""
    findings = []
    # Members ≥3 from a single source AND no shared name token → BLOCK
    bad: list[dict] = []
    for c in arch:
        members = c.get("members") or []
        if len(members) < 3:
            continue
        per_src = Counter(m.get("source") for m in members)
        if max(per_src.values()) >= 3:
            tokens_per_member = [
                set((m.get("name") or "").lower().split())
                for m in members
            ]
            shared = set.intersection(*tokens_per_member) if tokens_per_member else set()
            shared.discard("")
            if not shared:
                bad.append({"canonical_arch_id": c.get("canonical_arch_id"),
                            "primary_name": c.get("canonical_name"),
                            "n_members": len(members)})
    if bad:
        findings.append(Finding(
            invariant="stage_a/single_source_no_token_overlap",
            verdict="BLOCK",
            summary=f"{len(bad)} clusters have ≥3 same-source members with no shared name token",
            samples=bad[:10],
            suggested_fix=("Likely over-merge from a single source's lazy alias linking. "
                           "In match_architects: require name-similarity > 75 in ADDITION "
                           "to source-side alias signal."),
        ))
    else:
        findings.append(Finding(
            invariant="stage_a/single_source_no_token_overlap",
            verdict="PASS",
            summary="no over-merged single-source clusters detected",
        ))
    return findings


def _stage_f_qc(artefact_path: str) -> list[Finding]:
    """Stage F: run canonical/qc.py 10 invariants."""
    try:
        from canonical.qc import run_all_checks
    except Exception as exc:
        return [Finding("stage_f/qc_import", "BLOCK",
                        f"canonical.qc not importable: {exc}")]
    try:
        report = run_all_checks(artefact_path)
    except Exception as exc:
        return [Finding("stage_f/qc_run", "BLOCK", f"qc.run_all_checks raised: {exc}")]
    findings = []
    for check_name, result in (report.get("checks") or {}).items():
        ok = result.get("pass") if isinstance(result, dict) else result
        findings.append(Finding(
            invariant=f"stage_f/qc_{check_name}",
            verdict="PASS" if ok else "BLOCK",
            summary=str(result),
        ))
    return findings


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def run_stage(stage: str, artefact: str, cycle: int = 1) -> GateResult:
    res = GateResult(stage=stage, cycle=cycle)

    if stage == "A":
        try:
            data = json.load(open(artefact))
            clusters = data.get("clusters") if isinstance(data, dict) else data
        except Exception as exc:
            res.findings.append(Finding("load", "BLOCK", f"cannot load artefact: {exc}"))
            return res
        res.findings.extend(_stage_a_basic(clusters or []))

    elif stage == "B":
        try:
            data = json.load(open(artefact))
            buildings = data.get("buildings") if isinstance(data, dict) else data
        except Exception as exc:
            res.findings.append(Finding("load", "BLOCK", f"cannot load artefact: {exc}"))
            return res
        res.findings.append(_stage_b_year_span(buildings or []))
        res.findings.append(_stage_b_city_consistency(buildings or []))
        res.findings.append(_stage_b_phash_overlap(buildings or []))

    elif stage == "D":
        if not os.path.isdir(artefact):
            res.findings.append(Finding("load", "BLOCK",
                                        f"artefact must be a directory of batch_*.json, got {artefact}"))
            return res
        res.findings.extend(_stage_d_vocab_compliance(artefact))
        res.findings.append(_stage_d_visual_description_length(artefact))

    elif stage == "F":
        res.findings.extend(_stage_f_qc(artefact))

    else:
        res.findings.append(Finding("dispatch", "BLOCK", f"unknown stage: {stage}"))

    return res


def write_escalation(res: GateResult, artefact: str) -> str:
    os.makedirs(ESCALATION_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fn = f"{ESCALATION_DIR}/stage_{res.stage}_{ts}.md"
    with open(fn, "w") as f:
        f.write(f"# Reviewer escalation — Stage {res.stage} cycle {res.cycle}\n\n")
        f.write(f"**Verdict:** {res.overall}\n\n")
        f.write(f"**Artefact:** `{artefact}`\n\n")
        f.write(f"**Timestamp:** {ts}\n\n")
        f.write("## Findings\n\n")
        for fnd in res.findings:
            f.write(f"### {fnd.invariant} — {fnd.verdict}\n\n")
            f.write(f"{fnd.summary}\n\n")
            if fnd.samples:
                f.write("**Sample offending rows:**\n\n```json\n")
                f.write(json.dumps(fnd.samples, indent=2, ensure_ascii=False))
                f.write("\n```\n\n")
            if fnd.suggested_fix:
                f.write(f"**Suggested fix:**\n\n{fnd.suggested_fix}\n\n")
    return fn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["A", "B", "D", "F"])
    ap.add_argument("--artefact", required=True)
    ap.add_argument("--cycle", type=int, default=1)
    args = ap.parse_args()

    res = run_stage(args.stage, args.artefact, args.cycle)
    print(f"Stage {res.stage} cycle {res.cycle}: {res.overall}")
    for fnd in res.findings:
        print(f"  [{fnd.verdict}] {fnd.invariant} — {fnd.summary}")

    if res.overall == "BLOCK":
        path = write_escalation(res, args.artefact)
        print(f"\n→ escalation written: {path}")
        return 2
    if res.overall == "WARN":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
