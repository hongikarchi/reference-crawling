"""Phase 14a — Cheap Haiku tiebreaker for ambiguous decisions.

Two use cases (currently — more may follow when Phase 9.5 multi-source
matching lands):

  classify_reality(building) — L4 of reality_filter. For rows that
    survived L2 (no obvious article pattern) but failed L3 (no
    structural metadata) AND have a verified architect link.
    Returns "yes" (real building → KEEP T3) or "no" (article/event → DROP).

  classify_match_pair(...) — TODO Phase 9.5: tiebreak architect /
    building name matches in the sim 70-90 ambiguous band. Single Haiku
    call per ambiguous pair: "are these the same firm/building?" yes/no.

Design:

  • All calls are SINGLE-token completions (yes/no) → minimum tokens out.
  • Prompt is tight (~50 tokens in) → minimum tokens in.
  • Per-key on-disk cache (data/canonical/_haiku_cache.json) — re-runs
    are free for unchanged inputs.
  • Cost cap: HAIKU_MAX_NEW_CALLS env var (or config). Refuses to make
    more than N net-new calls in a single run; raises HaikuCostCapHit
    so the caller can fall back to default behaviour.
  • Graceful fallback when ANTHROPIC_API_KEY is missing or the SDK is
    not installed: returns the configured default (caller passes it).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

# Cache lives under data/canonical/ (gitignored via data/)
_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "canonical", "_haiku_cache.json"
)

# Default cost cap (override via env HAIKU_MAX_NEW_CALLS or kwarg).
# 10K @ ~$0.0005 per call → ~$5 per single-shot run. Enough for the
# entire 4-source borderline set without painful surprises.
DEFAULT_MAX_NEW_CALLS = 10_000

HAIKU_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HaikuCostCapHit(RuntimeError):
    """Raised when a run exceeds its allowed net-new-call budget."""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE: Optional[dict] = None


def _load_cache() -> dict:
    global _CACHE
    if _CACHE is None:
        if os.path.exists(_CACHE_PATH):
            try:
                with open(_CACHE_PATH) as f:
                    _CACHE = json.load(f)
            except (json.JSONDecodeError, OSError):
                _CACHE = {}
        else:
            _CACHE = {}
    return _CACHE


def _save_cache() -> None:
    global _CACHE
    if _CACHE is None:
        return
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    tmp = _CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_CACHE, f, ensure_ascii=False, indent=0)
    os.replace(tmp, _CACHE_PATH)


def _cache_key(prompt: str) -> str:
    """Stable hash of the prompt (so re-runs with same input are free)."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Anthropic SDK lazy import + missing-key guard
# ---------------------------------------------------------------------------

_CLIENT = None


def _get_client():
    """Returns an Anthropic client or None if SDK not installed / key missing.
    Callers MUST handle None by falling back to the default behaviour."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        import anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Try .env via python-dotenv (matches the harness)
        try:
            from dotenv import load_dotenv
            from core import config as _cfg
            load_dotenv(os.path.join(_cfg.BASE_DIR, ".env"))
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        except ImportError:
            pass
    if not api_key:
        return None
    _CLIENT = anthropic.Anthropic(api_key=api_key)
    return _CLIENT


# ---------------------------------------------------------------------------
# Reality-filter L4 classifier
# ---------------------------------------------------------------------------

_REALITY_PROMPT = """Is this a real architecture project / building (yes), or an article / event / award / interview about architects (no)?

Name: {name}
Architect: {architect}
Location: {location}
Year: {year}
Tags: {tags}

Answer one word: yes or no."""


def classify_reality(
    building: dict,
    *,
    default: str = "no",
    new_call_budget: Optional[int] = None,
) -> str:
    """Returns 'yes' or 'no'. Uses cache; on cache miss, calls Haiku.
    On any failure (no key, SDK missing, network error), returns `default`.
    Raises HaikuCostCapHit if `new_call_budget` is exhausted.
    """
    name = building.get("name") or building.get("name_en") or building.get("project_name") or "?"
    architect = (building.get("architect")
                 or (", ".join(building.get("architect_names")) if building.get("architect_names") else None)
                 or "?")
    location = building.get("location_country") or building.get("country") or "?"
    year = (building.get("year") or building.get("project_year")
            or building.get("completion_year") or "?")
    tags_raw = building.get("tags") or building.get("tag_slugs") or building.get("categories") or []
    if isinstance(tags_raw, str):
        try:
            tags_raw = json.loads(tags_raw)
        except json.JSONDecodeError:
            tags_raw = [tags_raw]
    tags = ", ".join(str(t) for t in tags_raw[:5]) if tags_raw else "?"

    prompt = _REALITY_PROMPT.format(
        name=name, architect=architect, location=location, year=year, tags=tags,
    )

    cache = _load_cache()
    key = _cache_key(prompt)
    if key in cache:
        return cache[key]

    # Cost cap check (only counts NEW calls, not cache hits)
    _NEW_CALL_TRACKER["count"] += 1
    if new_call_budget is not None and _NEW_CALL_TRACKER["count"] > new_call_budget:
        raise HaikuCostCapHit(
            f"new-call budget {new_call_budget} exhausted "
            f"(this run made {_NEW_CALL_TRACKER['count']-1} live Haiku calls so far)"
        )

    client = _get_client()
    if client is None:
        # No API key / no SDK — fall back. Don't cache the fallback answer
        # (so the next run, if key is set, will retry).
        return default

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=4,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (response.content[0].text or "").strip().lower()
        answer = "yes" if text.startswith("y") else "no"
    except Exception as e:
        # Network / API error — fall back, don't cache.
        print(f"  [haiku.classify_reality fallback] {type(e).__name__}: {e}")
        return default

    cache[key] = answer
    _save_cache()
    return answer


# ---------------------------------------------------------------------------
# Cost / cache stats
# ---------------------------------------------------------------------------

_NEW_CALL_TRACKER = {"count": 0}


def reset_call_counter() -> None:
    """Caller resets before a batch run if it wants to re-cap."""
    _NEW_CALL_TRACKER["count"] = 0


def cache_stats() -> dict:
    cache = _load_cache()
    return {
        "cached_entries": len(cache),
        "new_calls_this_run": _NEW_CALL_TRACKER["count"],
        "estimated_cost_usd": round(_NEW_CALL_TRACKER["count"] * 0.0005, 3),
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """Smoke test: classify 5 hand-crafted cases. Requires ANTHROPIC_API_KEY."""
    import argparse
    p = argparse.ArgumentParser(description="Haiku reality-classifier smoke test")
    p.add_argument("--default", default="no",
                   help="Default answer when API unavailable (default: no)")
    p.add_argument("--budget", type=int, default=10,
                   help="Max new Haiku calls (default 10)")
    args = p.parse_args(argv)

    cases = [
        {"name": "Crystal Palace by Norman Foster", "architect": "Foster + Partners",
         "location_country": "UK", "year": 2023},
        {"name": "Pritzker Prize 2025 awarded to Riken Yamamoto",
         "architect": "Riken Yamamoto", "location_country": None, "year": 2025},
        {"name": "Foster Foundation 2024 lecture series",
         "architect": "Foster + Partners", "location_country": "UK", "year": 2024},
        {"name": "Casa Lucernas",  # no other meta — should be 'yes' if AI knows
         "architect": "01arq", "location_country": "Argentina"},
        {"name": "Norman Foster: A Retrospective",
         "architect": "Foster + Partners", "location_country": None},
    ]

    reset_call_counter()
    print(f"=== Haiku classify_reality smoke (default={args.default!r}) ===\n")
    for c in cases:
        try:
            answer = classify_reality(c, default=args.default,
                                      new_call_budget=args.budget)
        except HaikuCostCapHit as e:
            print(f"  COST CAP HIT: {e}")
            break
        print(f"  {c['name']!r:60s}  → {answer}")

    print(f"\n{json.dumps(cache_stats(), indent=2)}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
