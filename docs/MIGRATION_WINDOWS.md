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

## Next — DONE (cover re-picks applied)

The 164-cover deliverable was re-reviewed (273 pairs, stronger judge + human review app)
and **145 `display_cover_url` swaps were applied to Neon on 2026-07-06** (user-gated,
QC PASS, reversal sidecar kept). The result was baked into the artifact
**`completeness_c27_cover2026q3`**, which is the current production release and the
loader's `DEFAULT_INPUT`. Job card: `.claude/ops/jobs/20260705_cover_repick_rereview.md`.
Full-census cover re-pick remains an optional, unscheduled follow-up.

## Status of this Windows machine (checked 2026-10-16)

- **Use the project venv:** `.venv/Scripts/python.exe <tool>` — the system `python`
  lacks `psycopg2`, so Neon tools fail outside the venv.
- **Not present locally:** the c27 artifact
  (`data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c27_cover2026q3.json`)
  and the R4 sidecar `data/canonical/r4_results.merged.jsonl` (the c26 file is also absent).
  Live Neon is the source of the current state; any re-upsert from this machine needs
  those two files copied in first. Read-only inspection works without them.
- Current work is vNext (`docs/VNEXT_REBUILD_PLAN.md`) and the archi-tinder handoff
  (`docs/HANDOFF_ARCHIBE_DB.md`).

Tools: `cover_repick.py` (--sample), `cover_repick_confirm.py`, `cover_classify_siglip.py`,
`cover_repick_review_app.py`.
