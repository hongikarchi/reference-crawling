# Machine migration — Mac → Windows (2026-07)

**For the assistant:** the user moved this project from a MacBook to a Windows
desktop. When they say "I moved environments / 환경 옮겼어", do the setup below,
run `python tools/verify_env.py`, fix whatever it reports MISSING, then resume from
**Current state → Next**. The repo (code + `CLAUDE.md` + `.claude/ops/jobs/`) carries
full context; read the latest job card. Everything here is cross-platform (tools use
`pathlib`; no shell-specific paths).

## Setup (once)

1. **Code** — already here via `git clone`. Confirm you're on `main`, latest commit
   is the `step1-hybrid` checkpoint (or newer).
2. **Python deps** — `pip install -r requirements.txt -r requirements-eval.txt`
   (torch, transformers, sentence-transformers, sentencepiece, ijson, psycopg2, …).
3. **Secrets** (gitignored — user copies manually from Mac via USB, NEVER git):
   `.env` (Neon writer + R2 + Divisare) and `.env.make-web`. Place at repo root.
4. **Data** (gitignored — user copies these two from Mac; the rest is re-derivable):
   - `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c26_rem2026q2.json` (the 1 GB source)
   - `data/reports/cover_audit/repick_chunk1k/confirmed.jsonl` (the 164-cover deliverable)
   Rebuild the derivable artifacts anytime: `python tools/neighbor_eval_prep.py`
   then `python tools/extract_all_images.py`.
5. **Claude Code CLI** — install + log in (same account). The LLM tools shell out to
   `claude -p --output-format json --model <opus|sonnet|haiku>` — verify `claude` is
   on PATH.
6. **GPU** — Mac used MPS. On Windows the tools auto-fall back: CUDA if an NVIDIA GPU
   is present (install a CUDA torch build for speed), else CPU (works, slower — fine
   for these sample-scale runs). No code change needed.
7. **Verify** — `python tools/verify_env.py`. It reports deps / secrets / data / Neon
   reachability and prints READY or the exact blockers.

> Note: Neon + R2 are cloud. The same `.env` connects from any machine — the "real"
> state lives in the cloud DB, not the laptop. Local `data/` is mostly cache/build.
> Auto-memory (`~/.claude/projects/.../memory/`) is machine-local and won't transfer,
> but the repo's job cards capture the durable state.

## Current state (as of the migration)

2026-Q2 embedding-space quality investigation + cover work is **done and committed**
(job card `.claude/ops/jobs/20260625_embedding_space_quality_eval.md`). Summary:
- ① text embedding taste-coherence ≈ 0.52-0.65 (a *proxy*, upstream of make_web's
  MMR/K-Means — not end-to-end recommender quality).
- ② visual-embedding switch: **no clear win** (the "+33%" was a judge-modality
  artifact; a text-only judge reversed it). Do NOT switch modality on this evidence.
- ② visual_description re-caption (Haiku): negative but **underpowered** (N=30,
  p=0.45) — discarded, but the "improve text fields" lever is NOT closed.
- ③ **cover audit: ~53% of covers non-representative.** The one real pre-launch win.
- Steps 3-4 (junk sweep, filter labels): low-yield / already-good — deferred.
- **Neon was never written. Reversible throughout.**

## Next (pending user decision — resume here)

**The deliverable awaiting approval:** `data/reports/cover_audit/repick_chunk1k/confirmed.jsonl`
— **164 Haiku-confirmed cover re-picks** (current→proposed `display_cover_url`, with
scores + reasons), from a 1,000-building sample.

1. User reviews the 164 (spot-check a few before/after). **Watch for the edge case:**
   buildings whose architecture *is* the interior (chapels, galleries, religious/
   cultural spaces) — the "good = exterior" rule may wrongly swap a deliberate interior
   cover for a blander exterior. Flag those, don't rubber-stamp.
2. On approval → apply the `display_cover_url` swaps (old value preserved in
   confirmed.jsonl = reversible) → re-upload via the loader. **User-gated Neon write —
   dry-run + present counts first, per project rules.**
3. Optional scale-up to full census (~6k expected): run streaming (do NOT blast 150k
   downloads at divisare) or fold cover re-pick into the crawl/enrich pipeline where
   images are already local.

Tools: `cover_repick.py` (--sample), `cover_repick_confirm.py`, `cover_classify_siglip.py`.
