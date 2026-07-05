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

## APPLIED (2026-07-06, user-gated)

- Human review COMPLETE: 273/273 decided, 0 defer/undecided. approve_swap **145** /
  reject 128; 21 overrides of the recommendation (6 rec-approve→reject,
  15 rec-reject→approve incl. 2 interior_exception the user chose to swap anyway).
- User decision: display_cover_url ONLY — covers_by_type sync deferred to make_web
  Task #19 / full-census repick.
- `tools/apply_cover_repick_neon.py` (new): adversarial 3-lens review (0 BLOCKER,
  5 WARN all fixed: snapshot pinning vs review_cases.json, nonzero exit on any QC FAIL,
  sidecar path from --decisions parent + crash-safe, --limit >= 1, KeyError path).
  Dry-run ladder 10 → 145: rows_affected == expected, 0 stale guards, QC PASS.
- **LIVE COMMIT (user-approved): 145 rows, QC PASS in-txn**, reversal sidecar
  `applied_cover_swaps_2026-07-05.jsonl` (UTC date). Post-commit spot-check 6/6 live.
- Artifact parity: `apply_overrides_to_artifact.py` extended with --src/--out/
  --overrides/--cols (per-sidecar column sets); sidecar
  `data/canonical/cover_repick_overrides_2026q3.jsonl` (145) baked onto c26 →
  **c27_cover2026q3** (39,478 rows; independent ijson verify: 145/145 swapped,
  128/128 rejected untouched). Upload validator vs c27: **PASS**. Loader + validator
  `DEFAULT_INPUT` repointed c26 → c27; CLAUDE.md production-dataset note updated.
- No tag rebuild needed (no tag axes touched); no R2 change (Divisare CDN URLs).

## Remaining / deferred

- both_bad 5 (2 user-swapped anyway, 3 kept): candidates for a proper re-pick when the
  full-census run happens (crawl-integrated, per 20260625 deferral).
- covers_by_type.exterior sync: revisit when make_web Task #19 (intent-based cover
  serving) starts.

## Notes

- `data/` is gitignored: fable_recheck.jsonl, review_cases.json, decisions, reports stay local.
- Judge images live in the session scratchpad (`repick_imgs/`) — disposable; review app uses
  the public Divisare CDN URLs directly, so the scratchpad is not needed for the human review.
