# make_db — Current State Report

*Updated: 2026-05-22 KST (canonical_v2 C6.4 narrow apply committed)*

---

## 2026-05-17 D-2 Completion Snapshot

- D-2 image-derived backfill is complete for the publishable affected scope:
  23,007 rows completed plus 1 real image-unavailable row marked
  nonpublishable.
- Final D-2 JSONL:
  `data/canonical/country_conflict_refresh/d2_results.patched.resume10_complete.jsonl`
  has 39,776 rows.
- Final strict artifact:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict.resume10_complete.json`.
- Final embedded artifact:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`.
- Strict build coverage: D-1/E-1/E-2/D-2 all 39,776/39,776,
  `needs_image_derived_backfill=0`.
- Publishability: 39,736 publishable, 40 nonpublishable
  (39 missing all images/display cover, 1 real image unavailable).
- `python3 tools/qc_strict.py --input ...resume10_complete.json` returns
  `OVERALL: PASS`.
- Upload validator on embedded resume10 artifact returns `PASS`, 39,776
  unique PKs, no failures.
- Generic merge audit with E-1 evidence + waivers returns `PASS`,
  `review_required=0`.
- Integrity audit is `COMPLETE`; remaining metadata gaps are source-level or
  ambiguous candidates, not upload blockers.
- Legacy `canonical.reviewer_gate --stage F` is incompatible with the
  strict_v2 schema and produced a false BLOCK; use `tools/qc_strict.py` for
  this artifact family.
- Post-shutdown recovery confirmed no live canonical/crawl/enrich/matcher
  runner was still active.
- Tool defaults that still pointed at superseded `.patched` strict artifacts
  were updated to the resume10 complete artifact family.
- Superseded strict/embedded partial artifacts from the earlier D-2 resumes
  were deleted after approval, reclaiming about 5.1GiB estimated disk space.
- Latest pre-upload packet:
  `data/reports/canonical_v2_preupload_qc.resume10_complete.md`.
- Latest Claude Gate review packet:
  `.claude/ops/reviews/20260517_upload_readiness_resume10.md`.
- Neon `canonical_v2_buildings` full upsert was committed after explicit user
  approval:
  - input:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`
  - report:
    `data/reports/canonical_v2_neon_loader_upsert_full.json`
  - rows loaded: 39,776
  - unique PKs: 39,776
  - publishable/nonpublishable: 39,736/40
  - missing embedding: 0
  - `needs_image_derived_backfill`: 0
- C1/C2 completeness analysis completed read-only:
  - rows inspected: 39,776
  - high-confidence deterministic backfill candidates: 0
  - review-needed candidates: 217
  - year candidates from descriptions: 136
  - raw `location_full` candidates: 81
  - reports:
    `data/reports/canonical_v2_gap_inventory.md`,
    `data/reports/canonical_v2_backfill_candidates_high_confidence.json`,
    `data/reports/canonical_v2_backfill_candidates_review_needed.json`,
    `data/reports/canonical_v2_manual_review_queue.md`
- C2.5 review-rule classification completed read-only:
  - total review-needed items: 217
  - safe after explicit policy approval: 91
  - keep review: 126
  - safe candidates: 91 `project_year` rows with exactly one non-future
    description year
  - safe `location_city` candidates: 0 under conservative parser
  - reports:
    `data/reports/canonical_v2_review_rule_candidates.md`,
    `data/reports/canonical_v2_review_rule_candidates.json`
- C2.6 LLM-style semantic location classification completed read-only:
  - location candidates classified: 81/81
  - apply-city candidates: 38
  - country-only strings blocked from city: 38
  - other locality/airport/region/national-park cases kept for review: 5
  - reports:
    `data/reports/canonical_v2_llm_location_adjudication.md`,
    `data/reports/canonical_v2_llm_location_adjudication.json`
- C3 completeness apply completed:
  - new strict artifact:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c3.json`
  - new embedded artifact:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3.json`
  - affected-row embedded subset:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3_affected.json`
  - field updates:
    - `project_year`: 91
    - `location_city`: 38
  - strict QC: PASS
  - upload validator: PASS
  - gap inventory review-needed items: 88, down from 217
  - Neon affected-row upsert committed:
    `data/reports/canonical_v2_neon_loader_upsert_completeness_c3_affected.json`
  - Neon rows loaded: 129
  - Neon unique PKs remain: 39,776
- Remaining candidate verification completed read-only:
  - remaining items inspected: 88
  - additional project-year policy apply candidates: 15
  - project-year keep/manual review: 30
  - location strings verified as country-only / keep city null: 38
  - location manual review: 5
  - report:
    `data/reports/canonical_v2_remaining_review_verdict.md`
- C4 completeness apply completed:
  - new strict artifact:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c4.json`
  - new embedded artifact:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json`
  - field updates:
    - `project_year`: 15
  - strict QC: PASS
  - upload validator: PASS
  - gap inventory review-needed items: 73, down from 88
  - missing `project_year`: 1,304, down from 1,319
  - Neon affected-row upsert committed:
    `data/reports/canonical_v2_neon_loader_upsert_completeness_c4_affected.json`
  - Neon rows loaded: 15
  - Neon unique PKs remain: 39,776
- C5 crawler gap audit completed read-only:
  - input:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json`
  - local source DBs inspected:
    `divisare`, `architizer`, `archello`, `metalocus`
  - local structured source backfill candidates found: 0
  - remaining no-local-candidate gaps:
    - `country_no_local_candidate`: 1,967
    - `city_no_local_candidate`: 2,010
    - `year_no_local_candidate`: 1,273
  - narrow local parser/semantic candidates:
    - `city_raw_location_candidate`: 2
    - `year_text_completion_signal_candidate`: 7
    - `year_text_noncompletion_candidate`: 24
  - report:
    `data/reports/canonical_v2_crawler_gap_audit.md`
  - Claude review packet:
    `.claude/ops/reviews/20260518_crawler_gap_audit_c5.md`
- C5.1 local candidate verdict completed read-only:
  - high-value local candidates inspected: 9
  - safe apply candidates: 1
  - keep-null candidates: 8
  - only apply candidate:
    `bld_038824.project_year = 1989`
  - report:
    `data/reports/canonical_v2_c5_local_candidate_verdict.md`
- C6 web-search smoke completed read-only:
  - sample rows: 10
  - likely safe apply after source review: 3
  - partial evidence: 3
  - unresolved: 2
  - conflict/manual: 1
  - keep-null policy: 1
  - conclusion: targeted web search can recover a subset, but bulk auto-fill is
    unsafe without exact-source matching, source ranking, and conflict handling
  - report:
    `data/reports/canonical_v2_c6_web_search_smoke.md`
- C6.1 candidate queue completed read-only:
  - rows with any remaining C6 field gap: 2,163
  - `c5_local_apply_ready`: 1
  - `c6_seed_apply_review`: 3
  - `policy_null_review`: 177
  - `web_search_location`: 769
  - `web_search_location_year`: 1,065
  - `web_search_year`: 148
  - deterministic N=100 smoke queue generated
  - reports:
    - `data/reports/canonical_v2_c6_candidate_queue.md`
    - `data/reports/canonical_v2_c6_n100_smoke_queue.md`
- C6.2 N=100 web-search smoke completed read-only:
  - sample size: 100
  - apply candidates after exact-source review: 34
  - partial candidates: 20
  - conflict/manual candidates: 4
  - policy-null/manual-null candidates: 9
  - unresolved candidates: 33
  - conclusion: web search is useful enough to justify C6.3, but direct bulk
    auto-apply is unsafe
  - report:
    `data/reports/canonical_v2_c6_n100_web_search_smoke.md`
- C6.3 source-ranked apply queue completed read-only:
  - local apply-ready candidates: 1
  - C6 seed candidates from N=10 smoke: 3
  - C6 N=100 apply-after-exact-source candidates: 34
  - combined mutation-candidate pool with seed overlap: 38
  - unique mutation-candidate pool: 35
  - ready for direct mutation: 1
  - requires exact-source review before mutation: 34
  - report:
    `data/reports/canonical_v2_c6_source_ranked_apply_queue.md`
- C6.4 narrow apply prep completed read-only:
  - direct apply candidate prepared:
    `bld_038824.project_year = 1989`
  - web-derived candidates blocked until exact-source evidence review: 34
  - actual canonical/Neon mutation remains user-gated
  - report:
    `data/reports/canonical_v2_c6_narrow_apply_prep.md`
- C6.4 narrow apply completed after explicit user approval:
  - applied update:
    `bld_038824.project_year = 1989`
  - new strict artifact:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c6.json`
  - new embedded artifact:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c6.json`
  - affected-row embedded subset:
    `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c6_affected.json`
  - strict QC: PASS
  - upload validator: PASS
  - post-C6 gap inventory: PASS
  - gap inventory review-needed items: 72, down from 73
  - missing `project_year`: 1,303, down from 1,304
  - Neon affected-row upsert committed:
    `data/reports/canonical_v2_neon_loader_upsert_completeness_c6_affected.json`
  - Neon rows loaded: 1
  - Neon unique PKs remain: 39,776

Active blocker:

- C5 did not find a broad local structured crawler/canonical propagation bug.
- R2 mutation, legacy table mutation, and make_web cutover remain user-gated.

Next action:

- Continue Codex-side work before Claude review.
- Next Codex step is C6.4 narrow apply preparation. Direct mutation is safe only
  for the C5 local row (`bld_038824.project_year = 1989`) unless exact-source
  evidence is captured for C6 rows first.
- Next Codex step is C6.5 exact-source review for the 37 web-derived C6
  candidates. Keep it read-only until a new mutation approval is given.
- Prepare make_web cutover/read-path validation against
  `canonical_v2_buildings` when user approves product-side validation.
- Remaining completeness review queue after C4 is 73 items:
  - 30 `project_year` items should remain manual/null unless source-specific
    evidence is added.
  - 38 `location_city` items are country-only and should keep city null.
  - 5 `location_city` items require manual/source review.
- Do not mutate legacy production upload paths, R2, or make_web production
  routing without a separate user-approved job.

---

## 2026-05-13 Recovery Snapshot

Claude stopped after the pipeline had moved beyond the older D-1 resume state.
The stale 2026-05-08 sections below are historical context and should not be
used as the active work queue.

Current verified baseline before repair:

- Stage B bug fix + re-enrichment completed in the captured Claude session.
- D-1 text enrichment: `data/canonical/d1_results.jsonl` has 39,759 rows,
  39,759 unique `cid`, 0 bad JSON.
- D-2 vision enrichment: `data/canonical/d2_results.jsonl` has 39,759 rows,
  39,759 unique `cid`, 0 bad JSON.
- E-1 image clustering: `data/canonical/e1_clusters.jsonl` has 39,759 rows,
  39,759 unique `cid`, 0 bad JSON.
- E-2 image type coverage: `data/canonical/e2_image_types.jsonl` has 39,759
  rows, 39,759 unique `cid`, 0 bad JSON.
- F strict canonical: `data/canonical/canonical_buildings_strict.json` has
  39,759 `canonical_bld_id`.
- G embeddings: `data/canonical/canonical_buildings_strict_embedded.json` has
  39,759 `canonical_bld_id`, 39,759 `embedding`, and no null/empty embedding
  pattern.
- Strict QC: `python3 tools/qc_strict.py --input
  data/canonical/canonical_buildings_strict.json` returns `OVERALL: WARN`.
  The only warning is `image_derived`; schema, PK uniqueness, field coverage,
  architect links, vocab validity, covers, source refs, and architect
  consistency pass.

2026-05-13 generic merge repair:

- Applied 14 high-confidence code-name overmerge repairs to
  `data/id_registry_buildings.json` and
  `data/canonical/canonical_buildings_4source.json`.
- Applied 1 high-confidence country-conflict false merge repair:
  `bld_018178` Office Building was split into the France/Muoto row and the
  Netherlands/B+O Architects row after source metadata plus phash evidence
  showed different architects, different countries, and zero cross-source image
  clusters.
- Backups created at
  `data/backups/code_name_split_20260513_013027/`.
- `data/backups/code_name_split_20260513_022108/`.
- Canonical count moved from 39,759 to 39,776. Source refs lost/duplicated:
  0/0.
- Recomputed affected downstream rows only:
  - D-1: 32 affected rows refreshed via local batch `codex exec`.
  - E-1: 32 affected rows recomputed deterministically, 857 images.
  - E-2: 32 affected rows refreshed via local batch Vision.
  - D-2: 32 affected rows refreshed via local batch Vision.
- Patched strict artifact:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict.patched.json`.
  Coverage is 39,776/39,776 for D-1, E-1, E-2, D-2, and identity.
- Patched embedded artifact:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.patched.json`.
  Embeddings copied from staged patched artifacts and encoded for the affected
  rows; missing embeddings: 0.
- `build_strict_canonical.py` now resolves architect canonical IDs from
  source project architect IDs. Upload dry-run warnings for missing architect
  IDs are back to 3,257 rows, not 39,775 rows.
- Strict QC on the patched strict artifact returns `OVERALL: WARN`; only
  `image_derived` remains WARN. Schema, PK uniqueness, field coverage,
  architect links, vocab validity, covers, source refs, and architect
  consistency pass.
- Upload dry-run on the patched embedded artifact returns `PASS` with
  39,776 unique primary keys and no failures.
- Generic code-name overmerge flag count is now 0.
- Generic merge audit with E-1 image evidence and explicit waivers returns
  `PASS`: 67 country-conflict flags remain as documented source
  country-field noise/aliases, but `review_required=0`.

Active blocker:

- Live upload is still user-gated.
- U3 fresh-table upload path is still the recommended path.
- No remaining automated QC blocker for the staged patched artifact. Remaining
  live-write decision: user approval for Neon/R2 upload path.

Next action:

- Do not run D-1 resume. Do not live-upload yet.
- Prepare U3 fresh-table upload against
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.patched.json`
  only after explicit user approval.
- Any live Neon/R2 write remains user-gated.

---

## −2. Pipeline state snapshot (2026-05-08 11:00 KST)

**Stage B (matching) — DONE.** v5 promoted to production
`canonical_buildings_4source.json`: 38,295 clusters, 7,817 multi-source,
3 hard-blocks (1/3 justified). Convergence 4-cycle self-heal trajectory
v2→v5: 0/20 → 0/10 → 0/5 → 1/3 hard-block precision. Recall 4,787 →
7,817 (+63%).

**Stage D-1 (text enrich) — PAUSED at 52%.**
- 17,666 successes preserved in `data/canonical/d1_results.jsonl`
- 5,880 + 1,934 burst failures (codex usage-limit cooldown tail)
- 18,629 untouched
- Codex CLI hit ChatGPT plan quota; **resume after 2026-05-13 09:32 KST**
- Per-call switched to `model_reasoning_effort=low` (was xhigh) for ~4x
  token reduction once we resume; structured task does not need xhigh.

**Stage E-1 (phash dedup) — RUNNING, ~1-2h ETA.**
- PID 41745 background, no LLM (Python imagehash + cross-source cluster)
- 28,100 / 38,295 (73%); rate 100 rows/min
- Codex usage-limit doesn't affect this stage.

**Stage E-2 / D-2 (Vision) — BLOCKED by codex limit.**
- Code written and tested (commits 31b679b + d7a46fa)
- Will dispatch immediately when codex returns (5/13 09:32).

**Stage F (assembly) — partially executable now.**
- New `tools/build_strict_canonical.py` joins v5 + D-1 + E-1 (+ E-2 + D-2
  when ready). Schema-tolerant: missing inputs leave fields None, so the
  same script runs at every pipeline-completion stage.
- Dry-run output: 38,295 buildings, 20,040 with D-1 (52%), 30,667 with
  E-1 (80%). Identity name 100%. Still need to populate
  `canonical_arch_ids` (Stage B never wrote them; F-side lookup TODO).

**Stage G (upload) — NEEDS refactor.**
- `upload/neon_strict.py` is still metalocus-centric (assumes
  `bid = metalocus_building_id`, requires embedding). Won't work as-is
  for 4-source canonical. Refactor task queued for codex when it returns.

---

## −1. Phase 15: multi-team QC refactor (2026-05-06, in progress)

**Why:** 232 false-merge cluster splits to date were all caught by user
spot-check post-canonical, never blocked in-pipeline. Today's discovery —
**bld_026977** (Austin Maynard's Terrace House Brunswick + Terracotta
House Fitzroy auto-merged via shared architect + ±1 year + 95% name token
similarity) — confirmed the structural QC gap: no blocking gate at the
matcher's decision point, no perceptual-image sanity check.

**What landed:**

* **5-workspace cmux layout**: DB-MAIN (claude orchestrator), DB-CRAWLER /
  DB-MATCHER / DB-ENRICHER (codex, write+fix code), DB-REVIEWER (claude,
  blocking QC). Setup via `./tools/cmux_setup.sh` (idempotent).
* **Inter-tab plumbing**: `tools/dispatch.sh <team> "<msg>"` (cmux send +
  Enter×2 to handle Claude Code paste-mode), `tools/poll.sh <team> [N]`
  (read-screen on the team's surface).
* **AGENTS.md** (project root): Codex CLI baseline, mirrors CLAUDE.md.
  Documents 4-team mapping, handoff signals, hard guardrails (never edit
  vocab.py, never delete registry, never push, never run upload).
* **`canonical/reviewer_gate.py`**: blocking QC for stages A/B/D/F.
  Verdict ∈ {PASS, WARN, BLOCK}; on BLOCK writes
  `.claude/escalations/<stage>_<ts>.md` with diagnosis + sample rows
  + suggested fix.
* **`canonical/match_phash_check.py`** + 4 unit tests: pure-image false-
  merge gate. BLOCK iff both sides have ≥2 images AND zero cross-source
  phash cluster overlap (Hamming ≤ 8). Golden-set test on bld_026977
  fixture: BLOCK as required.
* **`canonical/phash_cache.py`** + smoke test: one-time + incremental
  builder for `data/canonical/phash_cache.json`. CLI:
  `python3 -m canonical.phash_cache --build [--limit N] [--source <name>]
  [--workers 8]`. Resume-friendly (per-row progress file, atomic flush
  every 100 rows). Reuses `canonical/image_dedup.fetch_image_metadata`.
* **Self-heal loop verified**: 2 dispatch + 2 reviewer cycles, 0 user
  intervention. DB-MAIN read DB-MATCHER's commits + handoffs via poll.sh,
  routed to DB-REVIEWER, observed REVIEWER-PASS verdicts in Task.md.
* **Hard caps in core/config.py**: `CODEX_RETRY_CAP = 5`,
  `CODEX_COST_CAP_USD = 20`. At cap → escalate to user, no further retry.

**Currently running (background, PID 21376):**
`python3 -m canonical.phash_cache --build --workers 8` against all 4
source DBs. Scope: ~175K source rows × ~4 image URLs = ~700K fetches at
8-parallel. **Realistic ETA ~24h** (Plan 15 estimate of 8h was
optimistic — archello at 127K projects is dominant). Disk impact ~45 MB
JSON; image bytes are not stored. Cost $0.

**Suspended:**
T2 enrichment (Stage D-1 wave-of-4 sub-agent flow) at 90/288 batches.
Will be re-run via team-enricher against the post-phash-gate canonical;
result files preserved at `data/canonical/d1_results_t2/` for cost
comparison.

**Known QC issues this refactor will surface (next step #35):**
- bld_026977 Terrace/Terracotta false-merge (golden-set BLOCK case)
- Likely 50–300 other phash-rejected merges (estimate; actual count
  emerges after first reviewer_gate run on Stage B with phash gate active)

---

## 0. Pipeline state — Stage A → B (redesigned) → F complete

**Final canonical artifact**:
  `data/canonical/canonical_buildings_4source.json`
  **38,553** unified building records (div+arc base + met/arc enrichment)

**Architectural redesign (2026-05-05, user-driven)**:
  Old design merged all 4 sources equally → 146K canonical, but archello's
  poor data quality (1,365 multi-listing canonicals + supplier brands as
  architects + placeholder image URLs) caused systemic false-merges.
  New design uses divisare + architizer as base (clean 1-source-id-per-
  building), with metalocus + archello as MATCH-OR-DROP enrichment.
  Result: 146K → 38K canonicals, but data integrity dramatically improved.

| Phase | Action | Result |
|---|---|---|
| 1 | Divisare base | 28,903 canonicals |
| 2 | Architizer match-or-create | 38,295 canonicals (+9,392 arc-only) |
| 3 | Metalocus MATCH-OR-DROP | 804 matched, 2,661 dropped |
| 4 | Archello MATCH-OR-DROP | 12,853 matched, 114K dropped |
| Tiebreak | 6,106 pairs (rule + Sonnet) | 2,826 SAME merges |
| Multi-arch split | ≥3 architects = false-merge | 42 clusters split |
| Demote no-base | met/arc-only orphans removed | 2,500 demoted |

**Final breakdown**:
- 1-source: 28,379
- 2-source: 9,783
- 3-source: 385
- 4-source: 6
- Multi-source total: **10,174**

| Stage | What | Output | Commit |
|---|---|---|---|
| **A** | 4-source architect matching | 39,438 canonical architects (6,640 multi-source) | `0507bdc` → `dbb07c8` |
| **B (redesigned)** | div+arc base + met/arc match-or-drop | 38,553 canonical buildings | (pending commit) |
| **F** | 4-source assembly | unified records with all source URLs preserved | `2f6ac56` |

(Old 146K canonical info below kept for history.)

---

## OLD STATE (pre-redesign, 146K canonical) — for reference

**Final canonical artifact**:
  `data/canonical/canonical_buildings_4source.json`
  146,432 unified building records across 4 sources

| Stage | What | Output | Commit |
|---|---|---|---|
| **A** | 4-source architect matching | 39,423 canonical architects (6,640 multi-source, 172 four-source) | `0507bdc` → `dbb07c8` |
| **B** | 4-source building matching (Pass 1+2 + tiebreak + Hybrid feedback) | 146,432 canonical buildings (10,583 multi-source, 6 four-source) | `8576f46` |
| **F** | 4-source assembly | unified records with all source URLs preserved | `2f6ac56` |
| handoff doc | make_web schema, image policy, confidence tiers | `.claude/MAKE_WEB_HANDOFF.md` | `f3f12fe` |

**Field coverage in canonical_buildings_4source.json**:
  arch_id 95.9% / country 95.7% / city 90.8% / year 85.0% / typology 99.7%
  cover URLs: 28,620 div + 9,826 arz + 115,689 arc + 3,127 met = 157,262

**Quality safeguards enforced**:
- Conservative bias: TYPE_C ambiguous → DIFFERENT default in Sonnet tiebreak
- name_sim ≥ 75 gate on Hybrid feedback (filtered 178 collab/supplier
  false-merges like Barovier&Toso ↔ L&L Luce&Light)
- registry safety: skip pairs whose source_id is already attached to any
  canonical (no source-id move between canonicals)
- Post-fix splits (commit `544cdae` + 2026-05-05 follow-up):
  - 61 generic-name + multi-city false-merges split (e.g., 15 different
    "Private Residence" buildings across USA/Lebanon/Russia/Taiwan/UK)
  - 94 code-diff series false-merges split (e.g., Serpentine Pavilion
    2015/2016/2017/2018 were one cluster — split into 4)
  - +33 short-prefix false-merges split (PK/FT/RF Apartment etc — 2-letter
    client codes wrongly merged because outer "Apartment" word matched)
  - +1 architect placeholder split: arch_025729 "ARCHITECTS OFFICE -
    Architizer" was 16 different firms wrongly merged via shared generic
    name → split into 16 individual canonicals with their actual firm names
  - **Total false-merge fixes: 188 building clusters + 1 architect cluster**
- **2026-05-05 user-flagged 3rd round** (44 multi-arch splits):
  - User spot-check found "Private Residence Philadelphia" (5 archs),
    "Single-family home in Rodersdorf" (5 archs), "Forest House
    Oostvoorne" (4 archs) STILL merged because previous detector required
    "ALL tokens generic" — but city/proper-noun tokens disqualified them.
  - Stronger detector: any building canonical with ≥3 distinct
    canonical_arch_ids + ≥3 source members = false-merge (real collabs
    are typically 1-2 architects).
  - 44 clusters split → 203 new orphans. Top 6-arch / 5-arch / 4-arch
    clusters all eliminated. Post-fix: 0 clusters remain with ≥3 archs.
  - **Cumulative split count: 232 building clusters + 1 architect cluster**
- Spot-check (post-fix): ~13% suspect rate, mostly borderline (Innhouse vs
  Innhouse Kunming type), no egregious false-merges remain
- Final building count: **146,524** active canonicals (10,127 + 385 + 6
  multi-source = 10,518)

**Known gaps (NOT addressed this session)**:
- 3,913 metalocus buildings crawled but not in 4_buildings_final.json
  (Phase 11 enrichment pending) — excluded from Stage B; future work
- Bilbao Guggenheim, Lotus Temple, Apple Cupertino NOT FOUND in any
  source (data gap, not a matching error)
- Some borderline same-firm-same-city series may still be mis-merged
  (~5-10% remaining) — Stage D enrichment can verify per-canonical

**Remaining stages (out-of-scope this session)**:
- Stage D: text + image enrichment (LLM heavy, ~$30-50)
- Stage E: phash image dedup (network-heavy, ~hours)
- Stage G: Neon upload (manual gate, requires user approval)

---

> **Reading order**: this doc tells you "what data we currently have +
> what's running right now." For "how the pipeline is structured" see
> `.claude/PROJECT.md`. For "what work is open / blocked / done" see
> `.claude/Task.md`. For "the multi-phase roadmap that produced this
> state" see `~/.claude/plans/db-fuzzy-lerdorf.md`.

---

## 1. Per-source crawl state

The pipeline is multi-source (PROJECT.md §2.1). Each source has its own
SQLite DB under `data/crawl/`. URL-only at stage 1; cover selection +
R2 happens at stage 4-5.

| Source | DB | Projects (rows) | Auxiliary | Crawl status |
|---|---|---|---|---|
| **metalocus** | `data/crawl/metalocus.db` | **7,295 buildings** completed (was 3,873; +3,422 from Phase 11) | 110,054 images on disk (legacy 3,465); 11,897 skipped (post-Phase 11 URL-only); 6,416 articles skipped via `is_building_project` filter | resume in progress (PID 19591 finished earlier today; queue drained) |
| **divisare** | `data/crawl/divisare.db` | **29,936 projects** (29,905 with deep-fetched description) | 12,759 architects | deep-fetch ~99% done (29,930/29,937), background; 7 fetch failures |
| **architizer** | `data/crawl/architizer.db` | **10,632 projects** | 2,802 firms; **14,975 A+Award entries** (2013-2025 × Typology/Firms/Plus tracks) | full crawl complete |
| **archello** | `data/crawl/archello.db` | **22,887 projects** + 71,652 BIM-spec detail rows | brands_seen log populated | full crawl in progress (PID 19814; 22,887/135K done, ~111K pending; ~10 days remaining at 2.5s/req) |

**Image bytes on disk** (`images/`): **60 GB**, all metalocus legacy
(3,465 production rows × ~17 images each). New crawls store URLs only;
cover download + R2 upload is a stage-5 concern (Task.md Phase 9).

---

## 2. Canonical artefact

| Artefact | Path | Size | Built from |
|---|---|---|---|
| Strict canonical | `data/canonical/canonical_buildings_strict.json` | 36 MB / **2,488 records** | metalocus + divisare (architect + building matches) |
| Architect clusters | `data/canonical/metalocus_architect_clusters.json` | 1.2 MB / **2,188 clusters** (from 226 raw alias variants) | metalocus architect strings (Stage A consolidation) |
| Architect↔Divisare matches | `data/canonical/match/metalocus_architect_to_divisare.json` | 2.8 MB | Stage B-1 (rapidfuzz + exact-core + substring rules) |
| Building↔Divisare matches | `data/canonical/match/metalocus_to_divisare_buildings.json` | 3.1 MB | Stage B-2 (architect-scoped fuzzy + year/country signal) |

### Canonical 2,488 breakdown
- **720** full match (Divisare project + metalocus content) — Stage B-2 confident
- **1,768** arch-only match (Divisare canonical architect, metalocus building name + content)
- 928 pure orphans **dropped** in strict mode (no Divisare attribution at all)
- 49 article-style entries dropped (e.g. `"Foster + Partners win the competition for ..."`)

### Match coverage (against the source 3,465 metalocus buildings)
| Stage | Confident matches | Coverage |
|---|---|---|
| Architect cluster → Divisare architect | 1,489 of 2,188 (68.2%) | 76% of buildings |
| Building → Divisare project | 720 of 3,465 (20.8%) | (the 21% ceiling is `metalocus ∩ divisare`, not crawler limit — Divisare publishes a different building set per architect than metalocus does) |

**Note**: canonical artefact is currently **2-source only** (metalocus +
divisare). Architizer/Archello matches are not yet folded in — that's
Phase 9.5 (Task.md), blocked on the Archello crawl finishing.

---

## 3. Live processes

```
PID    Elapsed    Crawler
19814  15h+       run.py crawl-archello --phase projects --limit 200000
                  (Archello full crawl, ~10 days remaining)
```

(Earlier today: Architizer projects/firms/awards finished, Divisare
deep-fetch finished, metalocus URL-only resume finished. Archello is the
only long-running one left.)

---

## 4. Last upload to production (Neon + R2)

| What | Snapshot |
|---|---|
| PostgreSQL `architecture_vectors` | **3,465 buildings** (last full upload from `4_buildings_final.json`) |
| R2 `archi-tinder` bucket | ~17,303 images / ~7.09 GB |
| Last quality rating | 96/100 ("Ready") on the 3,465-row snapshot |
| Last upload date | (pre-canonical work; the strict canonical 2,488 has not been uploaded) |

**Upload of the strict canonical** is gated by user approval; the
script `upload/neon_strict.py` exists with `--dry-run` / `--confirm`
flags. Path C image hosting changes (Task.md Phase 9) should land
before the next upload.

---

## 5. Open work (full list in `.claude/Task.md`)

| Task | Status |
|---|---|
| **Phase 9** — Image hosting Path C (cover→R2, gallery→URLs only) schema work | Open, ratified by user; implementation deferred until Archello crawl finishes |
| **Phase 9.5** — Multi-source canonical extension (fold Architizer/Archello matches into canonical_buildings_strict) | Open, blocked on Archello |
| **Phase 10** — Cross-source image dedupe + quality ranking (phash → cluster → rank → unified gallery JSON) | Open, blocked on Phase 9.5 |
| **Phase 11c** — metalocus downstream (export-dedup → harness enrich + image_analysis URL-mode → embed-rate) | Pending — Phase 11a finished today; runs once user OKs the LLM cost (~$10-20) |
| **Atmosphere drift re-processing** (2,784 / 3,465 records out of V2 vocab) | Open, cost-gated |
| **Vocabulary evolution — atmosphere** | Open, researcher-routed |

---

## 6. Known gotchas (carries forward)

- `DB_PASSWORD` excluded from required-fields check in `core/config.py`
  — Neon needs it but the check is bypassed.
- Existing 3,465 metalocus images live at `images/{building_id}/` (post
  stage2_dedup reorganization from `images/{slug}/`). New metalocus
  crawls (Phase 11+) and Architizer/Archello/Divisare store URLs only.
- Don't re-run `enrich/dedup.py` on already-processed buildings.
- `python3 -m upload.neon --reset` only on first upload or after schema
  changes (drops the table — be sure).
- Atmosphere V2 drift: 2,784 / 3,465 production records have non-V2
  values (`organic`, `communal`, `historic`, …). See
  `data/reports/vocab_migration.json`. Re-processing requires user cost
  approval — see `quality-reviewer` agent's playbook.

---

## 7. How this report stays fresh

The `reporter` agent should rewrite this file at the end of any
state-changing operation: a crawl batch finishing, a canonical rebuild,
an upload. Current freshness owner: whoever runs the orchestrator next.

For day-to-day SQL counts (e.g. "is the Archello crawl still going?"),
prefer querying directly:

```bash
sqlite3 data/crawl/archello.db "SELECT status, COUNT(*) FROM pending_projects GROUP BY status;"
sqlite3 data/crawl/divisare.db "SELECT status, COUNT(*) FROM pending_projects GROUP BY status;"
sqlite3 data/crawl/metalocus.db "SELECT status, COUNT(*) FROM articles GROUP BY status;"
ps -axo pid,etime,command | grep "run.py" | grep -v grep
```

---

## 8. Environment

```
PostgreSQL: Neon (ap-southeast-1)
R2 bucket:  archi-tinder (~7.09 GB / 10 GB free tier)
Python:     3.9
Claude:     Max plan subscription (for LLM enrichment + image analysis)
Disk free:  ~7.6 GiB on /
```

## 2026-05-22 — canonical_v2 C7 local apply

- C7 local artifact apply: PASS.
- Applied C6.5 exact-source safe updates to 33 affected rows.
- Field updates: `location_country` 29, `location_city` 26, `project_year` 4; skipped `project_year` same-value 12 and existing-value 2.
- Strict QC PASS on 39,776 rows.
- Upload dry-run PASS on 39,776 rows / 39,776 unique PKs.
- Gap inventory PASS: high-confidence candidates 0, review-needed items 72 (`location_full` 43, `description_year` 29).
- Neon C7 affected-row upsert is not run; pending explicit approval.

## 2026-05-22 — canonical_v2 C7 Neon affected-row upsert

- C7 affected-row Neon upsert: PASS.
- Input: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c7_affected.json`.
- Rows loaded in transaction: 33.
- DB counts seen in transaction: total_rows=39,776, unique_pk=39,776, publishable_rows=39,736, nonpublishable_rows=40, missing_embedding=0, missing_display_cover_url=39, needs_image_derived_backfill=0.
- Writes committed.

## 2026-05-22 — canonical_v2 C8 final local pin

- C8 web/LLM review complete: reviewed 72 manual-review candidates.
- Safe local updates: 27 rows, field updates `location_country` 8, `location_city` 18, `project_year` 8.
- Manual/no-update rows: 45, due to country-only evidence, concept/future/historical-year context, locality conflicts, or insufficient exact-match web evidence.
- C8 local artifact apply: PASS.
- Strict QC PASS on 39,776 rows.
- Upload dry-run PASS on 39,776 rows / 39,776 unique PKs.
- Gap inventory PASS: high-confidence candidates 0, review-needed items 48 (`location_full` 27, `description_year` 21).
- Final local release candidate pinned as `completeness_c8`.
- Release manifest: `data/reports/canonical_v2_release_manifest.completeness_c8.json`.
- Final quality report: `data/reports/canonical_v2_final_quality_report.completeness_c8.md`.
- Neon C8 affected-row upsert is not run; pending explicit approval.
- Disk cleanup: after explicit user approval, C7 이전 superseded canonical full artifacts were deleted to recover space; C7/C8 artifacts, affected files, reports, job cards, and handoff logs retained.

## 2026-05-22 — canonical_v2 C8 Neon affected-row upsert

- C8 affected-row Neon upsert: PASS.
- Rows loaded in transaction: 27.
- DB counts seen in transaction: total_rows=39,776, unique_pk=39,776, publishable_rows=39,736, nonpublishable_rows=40, missing_embedding=0, missing_display_cover_url=39, needs_image_derived_backfill=0.
- Writes committed.
- Loader input: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8_affected_loader.json`.
- Loader wrapper note: same 27 affected rows as `canonical_buildings_strict_embedded.completeness_c8_affected.json`, wrapped under top-level `buildings` for loader compatibility.

## 2026-05-22 — Codex to Claude handoff packet

- Created Claude handoff packet: `data/reports/codex_to_claude_handoff_20260522.md`.
- Created full C8 data audit: `data/reports/canonical_v2_full_data_audit.completeness_c8.md` and `.json`.
- Data audit scanned 39,776 rows and found 39,776 unique canonical IDs.
- Created cleanup inventory: `data/reports/make_db_cleanup_inventory_20260522.md` and `.json`.
- Cleanup inventory scanned 1,426 files, 15.4GiB, and 23 files over 50MiB.
- Created cleanup classification proposal: `data/reports/make_db_cleanup_action_classification_20260522.md`.
- No deletion/move/archive and no Neon write were performed in this handoff phase.
