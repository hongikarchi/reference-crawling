# 20260621 — Hygiene + program-contradiction diagnosis & targeted re-derive

Follow-on to the 2026-Q2 accuracy eval (`a3d3ca9`). The DB is healthy (typology real-error
2.7% ≤ target, Tier-1 GREEN, tag tables in-sync, make_web contracts all delivered). A
next-work survey (6-area workflow) ruled out the two big-looking options as inflated:
architect "location unlock" is **8 architects** (not 800–1,500 — the gate is the 3-building
threshold, not location), and corpus "backlog +39k" is a **phantom** (pending_projects ≈
the already-consumed discovery queue; real growth needs a fresh crawl).

User chose: **hygiene + program re-derive** (diagnose-then-fix-only-genuine; ~$50 cap).

## A. Hygiene (local, safe — DONE)
- `tools/canonical_v2_upload_validator.py` DEFAULT_INPUT/REPORT: `resume10_complete` →
  `completeness_c26_rem2026q2` (was drifted vs loader; default-run validated stale artifact).
- `docs/dashboard.html` regenerated (`tools/build_dashboard.py`) — was 7 days stale.
- year_kind 507-row drift normalization: deferred into the Phase-2 gated Neon batch.

## B. Program contradiction diagnosis (measure-first; user rule "진단 후 진짜만 재도출")
Context: 2026-Q2 typology re-derive raised typology↔program contradictions 828→906 (live
checkable = 833) because the COARSE program axis now lags the more-precise typology. Three
root causes, must be split before any fix:
1. **map_artifact** — program is defensible; the `TYP_PROGRAM_OK` acceptable-set is too narrow
   (e.g. Religious Building→Mixed Use for a church+sports complex). NOT a data error.
2. **program_error** — program is genuinely wrong/lazy (e.g. Gallery→Other, Office→Other). FIX.
3. **typology_error** — rare (typology just re-derived).

- Queue: `tools/program_diag_queue.py` → 833 contradiction rows, ALL with source-prose
  evidence (0 skipped). Pre-split 34×25 → `/tmp/prog_diag_batches/`.
- Classify: `tools/program_diag_wf.js` (Sonnet, source prose) → category + suggested_program.
- Verify (program_error subset): **independent Opus blind A/B** (stored vs suggested,
  randomized, blind to incumbent) — closes circularity per the judge-reliability lesson.
- Map-widening for map_artifact: reported as a **metric refinement**, NOT silently applied
  (avoid gaming the contradiction gate); presented to user separately.

## Phase 2 — Apply (USER-GATED, pending)
Only confirmed program_error rows → Neon (dry-run + counts first). Then: rebuild tag tables
(`--with-r4`), refresh architect aggregates, durability fold (override + c26 + changed-only
sidecars), benchmark must hold 10 PASS/0 FAIL. Reversible (old program retained).

## OUTCOME — COMPLETE (2026-06-22, user-gated writes approved)
Diagnosis of 833 contradiction rows (Sonnet classify, source prose):
**map_artifact 415 / program_error 364 / typology_error 38 / ambiguous 16.**
Independent **Opus blind A/B** verified the 364 program_error candidates (363/364, 1 stream-
timeout recovered via resume): **223 CONFIRMED** (61.4% precision) — 90 rejected + 50 equal =
**140 Sonnet over-corrections filtered out by the independent judge** (exactly the judge-
reliability lesson: single judge inflates; independent verify is the real gate). All 223
confirmed NEW programs are in the typology's acceptable set → each resolves its own
contradiction, none new.

**Applied to Neon (approved):** `apply_program_corrections_neon.py --confirm-db-write` →
223 rows, program OOV 0. **Contradictions 833→610 (2.68%→1.94%).** Top transitions:
Hospitality→Housing 22, Other→Office 14, Infrastructure→Public 11, Landscape→Housing 11.
- Tag rebuild: `--with-r4`, corpus `c23_final+matstrip+r4+rem2026q2+prog2026q2`, 15,655 rows,
  all QC PASS (OOV 0 all 11 axes).
- Architect `top_programs` refresh: new `refresh_architect_programs.py`, **212 rows**
  (build-matched all-buildings counting — NOT pub-gated; the pub-gated variant flipped 1,336
  rows by changing semantics, avoided to isolate the change to the 223 corrections).
- Durability: override sidecar +`program` col (39,478), c26 re-synced (parity 223/223 verified,
  validator **PASS** 0 failures), changed-only sidecar 24,824. Re-upsert + c11-rebuild safe.
- **Benchmark 10 PASS / 0 FAIL.** Reversible (`program_corrections.jsonl` holds old values).

**Hygiene (A) done:** validator DEFAULT_INPUT/REPORT → c26; dashboard regenerated; **year_kind
"507 drift" was a metric-rule artifact, not data** — audit rule hardcoded `>= 2026 → future`
while c15 stores `> CURRENT_YEAR(2026)`; aligned audit to `> 2026` → drift now **0**, no data
write needed.

## typology micro-fix (user asked to review the 38; approved a clean subset)
Independent Opus check of the 38 typology_error candidates: **35/38 genuinely mis-typed**
(landscape/installation projects labelled as buildings). Suggestions split Park 17 /
Pavilion 17 / Office 1. Caveats surfaced: (a) **vocab gap** — no Garden/Landscape typology,
so Park was a lossy proxy → the 17 Park HELD pending a user vocab decision (core/vocab.py is
user-owned); (b) **Pavilion is a known over-call trap** → ran a SKEPTICAL second Opus pass
(refute-by-default) on the 17 Pavilion + 1 Office: **13 confirmed, 4 refuted** (1 real
permanent spa building + 3 non-building junk: sculptural furniture, a light fixture, a
rendering-studio spam ad). **Applied 13** typology→Pavilion (approved):
`apply_typology_corrections_neon.py --source typ_fix_prog_contra_2026q2`, OOV 0, primary∈tags
0. typology is NOT a tag axis → no tag rebuild needed; architect top_typologies refresh **7
rows**; durability re-synced (override/c26 parity 13/13/changed 24,827). Reversible
(`typology_corrections_prog_contra.jsonl`). **Contradictions 610→597 (1.90%).**

**Findings deferred:** (1) **map_artifact 415** — defensible coarsenings the narrow
`TYP_PROGRAM_OK` map flags; optional metric refinement (widen map) is judgment-adjacent →
user decision, not auto-applied. (2) **Park-17 typology** — blocked on a vocab decision (add
Garden/Landscape). (3) **3 non-building junk rows** (furniture/light/spam) — a
publishability/filter decision, out of scope.

## Cost / status
Budget cap ~$50; actual **~$25** (program: classify ~$4 + verify ~$13 + diagnosis ~$3;
typology micro-fix: Opus verify 38 + skeptical recheck 18 ~$4). Smoke ladder N=25→full 34
classify; verify smoke 1→full 19 (+resume). FINAL c26 re-verified after the typology pass:
program 223/223 + typology 13/13 parity, validator PASS (0 failures), benchmark 10 PASS/0 FAIL.
STATUS: **COMPLETE**.
