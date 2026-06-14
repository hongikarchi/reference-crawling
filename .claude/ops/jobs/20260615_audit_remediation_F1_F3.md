# 20260615 — Audit remediation F1 (typology) + F3 (material), committed to Neon

Follow-up to `20260614_full_census_audit.md`. User approved sequential remediation +
authorized autonomous completion ("loop, self-verify against criteria, I'm asleep").

## Committed to live Neon (2026-06-15)
1. **F1 typology** — LLM full re-derivation (Haiku, 440 batches × 75 over 32,933
   source_tag rows; ~8.9M tok / ~$13). `typology_primary` + `typology_tags` reconciled
   (primary ∈ tags; demoted-tag culprits removed). **9,403 rows** updated,
   `typology_primary_source='llm_rederive_2026q2'`. Contradictions 2,825→828 (−71%);
   83% agreement vs the 200 audit verdicts. Artifact `typology_corrections_full.json`.
2. **F1 OOV fix** — 23 rows where the LLM emitted program-vocab/invented typologies
   remapped to vocab (ambiguous→NULL). `tools/fix_typology_oov.py`. 0 OOV after.
3. **F3 material** — LLM-classified 5,578 non-singleton terms M/E/N; stripped pure-noise,
   promoted vocab-matching elements, **preserved material-root terms** (advisor catch),
   emptied→`['unspecified']`. **14,654 rows** updated. `material_corrections.json`.
4. **Tag tables rebuilt** — `canonical_v2_tag_stats_build.py --build --with-r4`, corpus
   `c23_final+matstrip+r4+rem2026q2`; in-txn QC all PASS; **18,655→15,655 rows** (junk
   facet cleanup); added `unspecified` material label.

## Verified on live (completeness criteria — see remediation_log_2026Q2.md)
C1 vocab OOV 0 · C2 primary∈tags 0 viol · C3 contradictions −71% · C4 material 0-empty ·
C5 tag QC PASS · C6 11-rule benchmark **10 PASS/0 FAIL** · C7 structural untouched.

## C8 + durability fold (DONE 2026-06-15, user-approved)
- **C8 architects** top_typologies/top_arch_elements: COMMITTED (6,899 rows,
  `refresh_architect_typologies.py`) from live post-remediation buildings.
- **Durability fold** (advisor-reviewed twice): (1) TWO sidecars from live Neon —
  `remediation_typmat_changed_rem2026q2.jsonl` (changed-only 20,778, typology+material, for
  the c11 build fold so other rows aren't frozen) + `remediation_overrides_rem2026q2.jsonl`
  (full 39,478, 8-col incl publishability, for artifact sync); (2) `build_completeness_c11_taxonomy.py`
  folded — `_DEMOTE_TAGS` + fixed `_pick_primary` tie-break (new rows) + applies changed-only
  sidecar (existing ids); (3) artifact synced → `completeness_c26_rem2026q2`, **verified ==
  live Neon** (publishable 36,673, matstrip correct), **upload-validator PASS**, loader
  `DEFAULT_INPUT` → c26. c26 base lacks R4 cols BY DESIGN — loader overlays R4 at upsert from
  `r4_results.merged.jsonl` (present). New tools: `apply_overrides_to_artifact.py`.
  (Initial c26 had a stale-publishability bug → 119 false empty-material FAIL; fixed by
  carrying live is_publishable/publishability_reasons — c26 & Neon now both 0 bad-material.)

## NOT done (out of scope / decision)
- **F2 style** over-generality: re-enrichment empirically unreliable (32% vs 91% retention,
  no ground truth) → NOT shipped; it's a make_web product decision. (remediation_log F2.)
- **F1 unflagged-bulk:** 5,326 non-contradiction primary changes are net-positive (+~32.5%)
  but ~20% degraded a fine label; kept. Optional: description-judge all 5,326, revert
  `old_better`/`both_wrong` (~$3-5) for strictly-better. (remediation_log honesty check.)
- **Residual 828 contradictions** ≈ program-side errors (typology now right, program wrong).

## Cost
F1 ~$13 + F3 ~$1.5 + smokes ≈ **~$15-18** total (Haiku-tiered). Each write dry-run'd first.

## Tooling (new)
`fix_typology_primary.py`, `typology_rederive_*` (workflows), `fix_typology_oov.py`,
`audit_label_sample.py`, `audit_fetch_images.py`, `apply_remediation_neon.py`,
`refresh_architect_typologies.py`. Input/raw: `/tmp/typ_rows.jsonl`, `/tmp/typ_out/`,
`/tmp/mat_terms.jsonl`, `/tmp/mat_out/`.
