#!/usr/bin/env python3
"""Collapse metalocus's raw architect strings into canonical clusters.

Pipeline (per `~/.claude/plans/db-fuzzy-lerdorf.md` Stage A):

  1. Extract architect strings from 4_buildings_final.json
  2. Split multi-firm strings on common separators (+, ',', '&', ' and ')
  3. Per mention: strip role suffixes, strip generic tokens (architects, studio, …),
     extract a substantive "core" (the identifying tokens)
  4. Cluster on core:
       • exact core match                  → auto-merge
       • token_sort_ratio(core) >= 95      → auto-merge
       • subset core relation (BIG ⊂ BIG-Bjarke-Ingels) and ratio >= 90 → auto-merge
       • 85 <= ratio < 95 with shared rare token → LLM tiebreak (forced tool_use)
  5. Pick canonical name per cluster (longest substantive variant)
  6. Output data/metalocus_architect_clusters.json — clusters + building_to_canonical map

Multi-architect buildings map to N canonical_ids. Cost design target: <$1 in LLM calls.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

from dotenv import load_dotenv
from rapidfuzz import fuzz, process

import quality

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

INPUT_PATH = "data/4_buildings_final.json"
OUTPUT_PATH = "data/metalocus_architect_clusters.json"

AUTO_MERGE_RATIO = 95.0
SUBSET_MERGE_RATIO = 90.0
LLM_MIN_RATIO = 85.0

# Separators used by metalocus to glue multi-firm collaborations.
# Deliberately EXCLUDED:
#   '&', ' and ' — far more often part of firm names than collaboration glue
#                  (Herzog & de Meuron, Neri & Hu, Smith and Jones Architects).
#                  Splitting them shatters famous firms across multiple clusters.
#   ',' — common inside legal-form firm names ("Skidmore, Owings & Merrill").
_SPLIT_PATTERN = re.compile(
    r"\s*(?:"
    r"\+|"
    r"\b(?:in collaboration with|en collaboration avec)\b|"
    r";|"
    r"·"
    r")\s*",
    flags=re.IGNORECASE,
)
# Suffix to drop after a sentence-ending period (role annotations)
_ROLE_SUFFIX_RE = re.compile(
    r"\.\s*(?:Lead|Co-author|Local|Associate|Collaborat|Consultant|Landscape|Interior|"
    r"Executive|Project|Structural|Civil|Mechanical|Technical|Design team|Team|"
    r"Architects?$|Main|Coordinator|Founding|Partner|Principal|Principals|"
    r"Quantity|Chief|Site|Junior|Senior|Co\b|Author|Author\(s\)|Concept|"
    r"Author\.|Author,|Author$).*",
    flags=re.IGNORECASE,
)
# A second pass that strips role-only segments anywhere (not just after a period)
_ROLE_INLINE_RE = re.compile(
    r"\b(?:Principals?[\s-]+(?:in[\s-]+)?Charge|Principal[\s-]+Partners?[\s-]+in[\s-]+Charge|"
    r"Partners?[\s-]+in[\s-]+Charge|in[\s-]+Charge|Lead[\s-]+Architects?|"
    r"Design[\s-]+Team|Project[\s-]+Team|Founding[\s-]+Partners?|"
    r"Chief[\s-]+Architects?)\b\.?",
    flags=re.IGNORECASE,
)

# Generic tokens that appear across firms — irrelevant for identity matching.
# Order matters for spotting compound endings like 'architecten' before 'arch'.
GENERIC_TOKENS = {
    "architects", "architect", "architectes", "architecte", "architecten",
    "architekti", "architekt", "architekten", "arquitectos", "arquitectes",
    "arquiteto", "arquitetos", "arquitectura", "arquitetura", "architectures",
    "architecture", "architectural", "atelier", "studio", "estudio", "studios",
    "ateliers", "design", "designs", "designstudio", "office", "bureau",
    "associates", "associati", "associes", "asociados", "partner", "partners",
    "group", "grupo", "gruppe", "compagnia", "company", "co", "llp", "ltd",
    "srl", "gmbh", "sa", "sl", "inc", "spa", "ag", "kg", "et", "and", "the",
    "de", "da", "do", "di", "di.", "del", "des", "der", "die", "das",
    "consultants", "consulting", "associes", "atelier-r", "atelier-1",
    "planning", "planners", "urbanism", "urbanisme", "urbanistes", "urbanists",
    "construction", "engineering", "engineers", "engineer", "interiors",
    "lab", "studio.", "projetos", "projectos", "proyectos", "workshop",
}

# Tokens too short / pronoun-like to count as 'rare'
_STOPLIKE = {"a", "i", "o", "e", "n", "s", "t", "r", "y"}

# Strings that survive cleaning but are non-firm artefacts; cluster on their own
NOISE_PATTERNS = [
    r"^lead architect",
    r"^design team",
    r"^project team",
    r"^architect[s]?$",
    r"^collaborator",
    r"^consultant",
    r"^author\b",
    r"^team$",
    r"^landscape$",
    r"^landscape architect",
    r"^landscape designer",
    r"^interior$",
    r"^interiors$",
    r"^interior designer",
    r"^urbanis[mt]",
    r"^engineering$",
    r"^engineer$",
    r"^principal[s]?$",
    r"^partner[s]?$",
    r"^associ[eé]s?$",
    r"^asociados?$",
    r"^arquitectos?$",
    r"^arquitectes$",
    r"^architecten$",
    r"^architekti$",
    r"^architekten$",
]


def _strip_role_suffix(value: str) -> str:
    s = _ROLE_SUFFIX_RE.sub("", value)
    s = _ROLE_INLINE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".,").strip()
    return s


# Locations / cities that appear as residue after splitting; never a firm name
_CITY_RESIDUE = {
    "rotterdam", "madrid", "barcelona", "vienna", "berlin", "paris", "london",
    "tokyo", "milano", "milan", "lisbon", "lisboa", "porto", "amsterdam",
    "copenhagen", "stockholm", "oslo", "helsinki", "warsaw", "prague", "zurich",
    "munich", "munchen", "frankfurt", "hamburg", "rome", "roma", "athens",
    "beijing", "shanghai", "seoul", "singapore", "sydney", "melbourne", "ny",
    "nyc", "new york", "los angeles", "la", "chicago", "boston", "san francisco",
}


def _looks_like_city_residue(normalized: str) -> bool:
    toks = normalized.split()
    if not toks:
        return True
    # All tokens are city names or country codes
    return all(t in _CITY_RESIDUE or len(t) <= 2 for t in toks)


def _split_multi_firm(value: str) -> list[str]:
    """Break collaboration strings into single-firm mentions.

    Avoids splitting inside obvious firm-name contexts (e.g. 'Foster + Partners'
    where '+' is part of the firm name). Heuristic: don't split if the segment
    immediately after the separator is empty, or is a tiny token that looks like
    part of the same name ('+ Partners', '+ Architects').
    """
    if not value:
        return []
    candidates = [s.strip() for s in _SPLIT_PATTERN.split(value) if s.strip()]
    if len(candidates) <= 1:
        return [value.strip()]
    # Reattach trailing tiny stub-only segments (those whose only words are GENERIC_TOKENS)
    merged: list[str] = []
    for c in candidates:
        toks = re.findall(r"\w+", c.lower())
        is_stub = bool(toks) and all(t in GENERIC_TOKENS for t in toks)
        if is_stub and merged:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)
    return merged


def _normalize_for_core(value: str) -> str:
    """Lowercase + remove punctuation → space-separated tokens."""
    s = re.sub(r"[^\w\s]", " ", value)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _extract_core(value: str) -> str:
    """Substantive identifying tokens, generic noise removed."""
    norm = _normalize_for_core(value)
    toks = [t for t in norm.split() if t not in GENERIC_TOKENS and t not in _STOPLIKE]
    return " ".join(toks)


def _is_noise(normalized: str) -> bool:
    return any(re.match(p, normalized) for p in NOISE_PATTERNS)


def _has_shared_rare_token(core_a: str, core_b: str, *, min_len: int = 5) -> bool:
    a = {t for t in core_a.split() if len(t) >= min_len}
    b = {t for t in core_b.split() if len(t) >= min_len}
    return bool(a & b)


def _is_subset_relation(core_a: str, core_b: str) -> bool:
    a = set(core_a.split())
    b = set(core_b.split())
    if not a or not b:
        return False
    return a.issubset(b) or b.issubset(a)


def _extract_mentions(buildings: list[dict]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Returns (mentions, building_to_mentions).

    mentions: {mention_text: {core, normalized, countries, building_ids, raw_full,
                              is_noise}} — keyed by the post-split, post-role-strip text
    building_to_mentions: {building_id: [mention_text, ...]}
    """
    mentions: dict[str, dict] = {}
    building_to_mentions: dict[str, list[str]] = defaultdict(list)
    for b in buildings:
        raw = (b.get("architect") or "").strip()
        if not raw:
            continue
        bid = b["building_id"]
        country = b.get("location_country") or ""
        # First strip role suffix on the WHOLE string, then split — role markers
        # often live at the end and would otherwise contaminate every fragment.
        prelim = _strip_role_suffix(raw)
        for piece in _split_multi_firm(prelim):
            mention = _strip_role_suffix(quality._clean_architect(piece) or piece)
            if not mention:
                continue
            normalized = _normalize_for_core(mention)
            if not normalized:
                continue
            if _looks_like_city_residue(normalized):
                continue
            core = _extract_core(mention)
            rec = mentions.setdefault(mention, {
                "normalized": normalized,
                "core": core,
                "countries": set(),
                "building_ids": [],
                "raw_full_strings": set(),
                "is_noise": _is_noise(normalized),
            })
            if country:
                rec["countries"].add(country)
            rec["building_ids"].append(bid)
            rec["raw_full_strings"].add(raw)
            building_to_mentions[bid].append(mention)
    return mentions, dict(building_to_mentions)


def _core_too_weak_to_merge(core: str) -> bool:
    """Cores like 'b', 'ad', 'pk' are too generic to support an auto-merge —
    they collide across unrelated firms (e.g. 'AD ARCHITECTURE' vs 'Y.ad studio').
    Demand at least one substantive token of length >= 3, or 2+ tokens total.
    """
    toks = core.split()
    if len(toks) >= 2:
        return False
    return not toks or len(toks[0]) < 3


def _classify_pair(core_a: str, core_b: str) -> tuple[str, float]:
    """Returns ('auto'|'llm'|'reject', similarity_score)."""
    if not core_a or not core_b:
        return ("reject", 0.0)
    if _core_too_weak_to_merge(core_a) or _core_too_weak_to_merge(core_b):
        return ("reject", 0.0)
    if core_a == core_b:
        return ("auto", 100.0)
    sort_ratio = float(fuzz.token_sort_ratio(core_a, core_b))
    if sort_ratio >= AUTO_MERGE_RATIO:
        return ("auto", sort_ratio)
    if _is_subset_relation(core_a, core_b) and sort_ratio >= SUBSET_MERGE_RATIO:
        return ("auto", sort_ratio)
    if sort_ratio >= LLM_MIN_RATIO and _has_shared_rare_token(core_a, core_b):
        return ("llm", sort_ratio)
    return ("reject", sort_ratio)


def _pairwise(mentions: dict[str, dict]) -> tuple[list[tuple[str, str, float]],
                                                  list[tuple[str, str, float]]]:
    items = sorted(mentions.keys())
    cores = [mentions[m]["core"] for m in items]
    is_noise = [mentions[m]["is_noise"] for m in items]

    auto, llm_q = [], []
    for i in range(len(items)):
        if is_noise[i] or not cores[i]:
            continue
        for j in range(i + 1, len(items)):
            if is_noise[j] or not cores[j]:
                continue
            verdict, sim = _classify_pair(cores[i], cores[j])
            if verdict == "auto":
                auto.append((items[i], items[j], sim))
            elif verdict == "llm":
                llm_q.append((items[i], items[j], sim))
    return auto, llm_q


class _UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self):
        out = defaultdict(set)
        for x in self.parent:
            out[self.find(x)].add(x)
        return list(out.values())


def _llm_tiebreak(a: str, b: str, model: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    tool = {
        "name": "decide_firm_match",
        "description": "Decide whether two architect strings name the same firm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "is_same_firm": {"type": "boolean"},
                "canonical_name": {"type": "string",
                                   "description": "Best canonical form (only if is_same_firm)."},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
            },
            "required": ["is_same_firm", "confidence"],
        },
    }
    system = [{
        "type": "text",
        "text": (
            "You are an architecture-database deduplicator. Decide whether two architect strings "
            "refer to the same firm.\n"
            "Identity patterns to recognise as SAME firm:\n"
            "  • abbreviation vs. full name (BIG = Bjarke Ingels Group)\n"
            "  • reordered/translated tokens (KAAN architecten = KAAN architects)\n"
            "  • founder name only vs founder + firm (Norman Foster ≈ Foster + Partners)\n"
            "  • country/city suffix added (KAAN architecten = KAAN architecten Rotterdam)\n"
            "Recognise as DIFFERENT firms:\n"
            "  • shared common token only (Foo Architects ≠ Bar Architects)\n"
            "  • similar surname but different firm (Smith Studio ≠ Smith Designs LLC unless explicit)\n"
            "  • collaboration listings (A + B + C are 3 different firms, not one)\n"
            "Always call decide_firm_match. Be conservative: when uncertain, set is_same_firm=false."
        ),
        "cache_control": {"type": "ephemeral"},
    }]
    message = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        tools=[{**tool, "cache_control": {"type": "ephemeral"}}],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": f"A: {a!r}\nB: {b!r}\n\nSame firm?"}],
    )
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "decide_firm_match":
            return block.input
    raise ValueError(f"LLM did not call decide_firm_match for ({a!r}, {b!r})")


def _resolve_with_llm(uf: _UnionFind, queue: list[tuple[str, str, float]],
                      model: str, *, progress_every: int = 25) -> list[dict]:
    decisions = []
    sorted_q = sorted(queue, key=lambda t: -t[2])
    for idx, (a, b, sim) in enumerate(sorted_q, 1):
        if uf.find(a) == uf.find(b):
            decisions.append({"a": a, "b": b, "similarity": sim,
                              "skipped": "already_in_same_cluster"})
            continue
        try:
            decision = _llm_tiebreak(a, b, model)
        except Exception as exc:
            decisions.append({"a": a, "b": b, "similarity": sim,
                              "error": f"{type(exc).__name__}: {exc}"})
            continue
        merged = (decision.get("is_same_firm") and
                  decision.get("confidence", 0.0) >= 0.7)
        if merged:
            uf.union(a, b)
        decisions.append({"a": a, "b": b, "similarity": sim,
                          "decision": decision, "merged": bool(merged)})
        if idx % progress_every == 0:
            print(f"  llm tiebreak: {idx}/{len(sorted_q)} processed")
    return decisions


def _name_quality_score(name: str) -> tuple:
    """Higher = better canonical candidate.

    Penalize: parens/brackets, role-suffix residue, all-lowercase, all-caps.
    Reward: title-case, longer length (more descriptive), absence of '.', no '(' '['.
    """
    has_paren = "(" in name or "[" in name
    has_period = "." in name
    is_all_lower = name == name.lower()
    is_all_upper = name == name.upper() and any(c.isalpha() for c in name)
    has_role_word = any(w in name.lower() for w in
                        ("partner", "principal", "lead", "director", "design team",
                         "in charge", "associate", "founder"))
    return (
        not has_role_word,   # prefer no role words
        not has_paren,       # prefer no parens
        not has_period,      # prefer no period (= clean name)
        not is_all_lower,    # prefer some uppercase
        not is_all_upper,    # but not ALL uppercase either
        len(name),           # longer = more descriptive
        name,                # lex tiebreak
    )


def _pick_canonical_name(cluster: set[str], decisions: list[dict]) -> str:
    suggested = []
    for d in decisions:
        if not d.get("merged"):
            continue
        if d["a"] in cluster or d["b"] in cluster:
            cn = (d.get("decision") or {}).get("canonical_name")
            if cn:
                suggested.append(cn)
    if suggested:
        return Counter(suggested).most_common(1)[0][0]
    return max(cluster, key=_name_quality_score)


def consolidate(input_path: str = INPUT_PATH, output_path: str = OUTPUT_PATH,
                *, use_llm: bool = True, dry_run: bool = False,
                model: str = "claude-haiku-4-5-20251001") -> dict:
    with open(input_path) as f:
        buildings = json.load(f)

    mentions, building_to_mentions = _extract_mentions(buildings)
    print(f"buildings with architect: {len(building_to_mentions)}")
    print(f"unique mentions (post-split, post-role-strip): {len(mentions)}")
    noise_n = sum(1 for r in mentions.values() if r["is_noise"])
    print(f"  of which noise (skipped from clustering): {noise_n}")

    auto, llm_q = _pairwise(mentions)
    print(f"auto-merge candidates (sort_ratio ≥ {AUTO_MERGE_RATIO} on cores): {len(auto)}")
    print(f"llm tiebreak candidates ({LLM_MIN_RATIO} ≤ sort_ratio < {AUTO_MERGE_RATIO}, "
          f"shared rare token): {len(llm_q)}")

    uf = _UnionFind(list(mentions.keys()))
    for a, b, _ in auto:
        uf.union(a, b)

    pre_llm = uf.groups()
    print(f"clusters after auto-merge only: {len(pre_llm)}")

    decisions = []
    if dry_run or not use_llm:
        if llm_q:
            print(f"[dry-run] would call LLM for {len(llm_q)} pairs "
                  f"(~${len(llm_q) * 0.005:.2f} estimated)")
    elif llm_q:
        print(f"calling LLM for {len(llm_q)} ambiguous pairs...")
        decisions = _resolve_with_llm(uf, llm_q, model)
        merged_n = sum(1 for d in decisions if d.get("merged"))
        print(f"  merged via LLM: {merged_n}/{len(decisions)}")

    final_clusters = uf.groups()
    final_clusters.sort(key=lambda c: -sum(len(mentions[m]["building_ids"]) for m in c))

    output_clusters = []
    mention_to_canonical: dict[str, str] = {}
    for i, cluster in enumerate(final_clusters):
        cid = f"metaloc_arch_{i:04d}"
        canonical_name = _pick_canonical_name(cluster, decisions)
        all_bids: list[str] = []
        all_countries: set[str] = set()
        all_full_raws: set[str] = set()
        for m in cluster:
            mention_to_canonical[m] = cid
            all_bids.extend(mentions[m]["building_ids"])
            all_countries |= mentions[m]["countries"]
            all_full_raws |= mentions[m]["raw_full_strings"]
        decided_via_llm = any(
            d.get("merged") and (d["a"] in cluster or d["b"] in cluster)
            for d in decisions
        )
        is_noise = all(mentions[m]["is_noise"] for m in cluster)
        output_clusters.append({
            "canonical_id": cid,
            "canonical_name": canonical_name,
            "raw_aliases": sorted(cluster),
            "raw_full_source_strings_sample": sorted(all_full_raws)[:5],
            "building_ids": sorted(set(all_bids)),
            "building_count": len(set(all_bids)),
            "countries": sorted(all_countries),
            "decided_by": "llm" if decided_via_llm else "auto",
            "is_noise": is_noise,
        })

    bld_to_canonical: dict[str, list[str]] = {}
    for bid, ms in building_to_mentions.items():
        cids = sorted({mention_to_canonical[m] for m in ms if m in mention_to_canonical})
        bld_to_canonical[bid] = cids

    summary = {
        "input_file": input_path,
        "buildings_with_architect": len(building_to_mentions),
        "unique_mentions": len(mentions),
        "noise_mentions_excluded": noise_n,
        "auto_merge_pairs": len(auto),
        "llm_candidate_pairs": len(llm_q),
        "llm_calls_made": sum(1 for d in decisions if "decision" in d),
        "llm_merged_pairs": sum(1 for d in decisions if d.get("merged")),
        "final_cluster_count": len(output_clusters),
        "noise_clusters": sum(1 for c in output_clusters if c["is_noise"]),
        "buildings_with_n_canonical_architects": dict(Counter(len(v) for v in bld_to_canonical.values())),
        "clusters": output_clusters,
        "building_to_canonical": bld_to_canonical,
        "llm_decision_log": decisions,
    }

    if not dry_run:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"\n✓ saved → {output_path}")

    return summary


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--input", default=INPUT_PATH)
    p.add_argument("--output", default=OUTPUT_PATH)
    p.add_argument("--dry-run", action="store_true",
                   help="estimate LLM cost without calling")
    p.add_argument("--no-llm", action="store_true",
                   help="auto-merge only; skip LLM tiebreak")
    p.add_argument("--model", default="claude-haiku-4-5-20251001")
    args = p.parse_args(argv)
    consolidate(args.input, args.output,
                use_llm=not args.no_llm, dry_run=args.dry_run,
                model=args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
