# 2026-07-05 — Cover re-pick Fable 5 re-review + visual review app

Plan: re-judge all 273 repick_chunk1k pairs with a stronger judge (Fable 5 session
subagents) because the Haiku rubric misses the interior-as-architecture exception;
ship a localhost review app so the user can approve/reject fast. NO Neon writes.
Goal: turn the pending "approve the 164" into a precision-filtered, easy decision.

## Scope

- Input: `data/reports/cover_audit/repick_chunk1k/confirmed.jsonl` (273 rows; Haiku 164 true / 109 false)
- Everything local: Neon touched SELECT-only (meta via `.env.make-web` read-only role)
- Machine: Windows box (post 2026-07-05 migration), CPU-only, judges = in-session subagents (no API $)

## Built

- `tools/cover_repick_recheck_prep.py` — `--fetch-meta` (273/273 from Neon reader),
  `--fetch-images` (546 imgs → scratchpad, manifest, retry/backoff, 0 failures),
  `--merge-recheck` (fragment merge + pass-2 resolution + validation → `fable_recheck.jsonl`)
- `tools/cover_repick_review_app.py` — `--build` (deterministic bucketing) / `--check`
  (re-derive + compare, PASS) / `--serve` (port 8766, auto-open, keyboard-first UI,
  atomic decisions JSON) / `--report` (agreement matrix JSON)
- Judge harness: Workflow subagents, batch 8 pairs/agent, waves of 4 (concurrency law),
  blind to Haiku; pass-2 independent re-judge on triggers (conflict/low_conf/category);
  action-level disagreement → user_review. Rubric adds `interior_exception` + `both_bad`.

## Phases

- Smoke (batch 1, 8 pairs): operator eyeballed 3 pairs vs verdicts — 3/3 match,
  incl. correctly NOT applying the interior exception to a bland chapel interior. GATE PASS.
- Full: 47 agents (34 pass1 + 13 pass2), 3.35M subagent tokens, 33 min, 0 errors.
- Merge: 273/273 validated; pass2 on 100 (92 conflict / 7 category / 1 low_conf), self-agree 90/100.

## Results

- Agreement matrix (Fable final vs Haiku): haiku_true 164 → swap 101 / keep 55 / interior_exception 6 / both_bad 2;
  haiku_false 109 → swap 29 / keep 74 / interior_exception 2 / both_bad 4. Action agreement 66%.
- Buckets: auto_approve 101 · rescued 28 · demoted 129 (real_demotion 53 + confirmed_reject 76)
  · user_review 10 · both_bad 5. Interior exceptions caught: 8 (6 were Haiku-approved swaps —
  e.g. POLIN Museum wavy lobby, Brühlsche Terrasse exhibition hall).
- If recommendations are confirmed as-is: apply set = 129 swaps (Haiku's 164 −53 blocked +28 rescued).
- Reports: `fable_recheck_2026q3.{md,json}` (md in Korean for the user), `review_cases.json`,
  blank `cover_repick_decisions.json`.

## COST (measured)

- LLM: 0 USD API (session subagents). ~3.4M subagent tokens incl. smoke; wall ~35 min.
- One earlier haiku CLI smoke call during env audit: $0.03.

## Verified

- prep: meta 273/273; images 546/546 (manifest pairs_ok=273, failed=0)
- merge validator: 273 unique ids, enums, reasons — PASS; `--check` re-derivation — PASS
- app e2e: GET /, /api/cases (273), POST decision (server derives new_display_cover_url),
  disk persistence, POST null undo, invalid decision → 400, bulk apply 101 then re-bulk 0
  (no overwrite), decisions reset to blank for the user.

## PENDING USER

1. Run `PYTHONUTF8=1 python tools/cover_repick_review_app.py --serve` and decide the 273
   (focus: user_review 10 → rescued 28 → real_demotion 53; bulk the rest).
2. Approve building the gated apply tool (`apply_cover_repick_neon.py`, dry-run first,
   `--apply --confirm-db-write`, `IS NOT DISTINCT FROM old` guard) + c26→c27 override
   parity (per-sidecar column sets — display_cover_url deliberately not in today's COLS).

## Notes

- `data/` is gitignored: fable_recheck.jsonl, review_cases.json, decisions, reports stay local.
- Judge images live in the session scratchpad (`repick_imgs/`) — disposable; review app uses
  the public Divisare CDN URLs directly, so the scratchpad is not needed for the human review.
