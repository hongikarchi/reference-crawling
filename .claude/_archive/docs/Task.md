# make_db — Task Board

Append-only coordination document. Agents read this on every invocation.
The orchestrator owns routing; any agent may append to `## Handoffs`.

> **Phase numbering convention.** Sequential, flat. New work = next
> integer. PROJECT.md §12 explains why phases are orthogonal to the
> 5-stage pipeline; one phase usually touches one stage but cross-stage
> phases (e.g. Phase 11) are normal. Don't reuse old numbers.

## Open

### Phase 11c — metalocus downstream pipeline (post-resume)

**Trigger**: Phase 11a (metalocus URL-only article crawl) finished
2026-04-29 with 7,295 buildings completed (was 3,873; +3,422 new from
the 9,206 pending queue, of which 6,416 articles were skipped via
`is_building_project` filter and 95 failed). The new buildings have
their cover/gallery/drawing URLs persisted on `buildings` rows but no
on-disk image files (Phase 11.0 / 5-stage compliance).

**Steps** (each runnable independently, resume-friendly):
1. `python3 run.py export-dedup` — refresh `data/enrich/1_buildings_raw.json`
   to include the new ~3,422 buildings (URL fields included via the
   Phase 11.0 export.py update).
2. `python3 run.py harness` — text enrich + image analysis for the new
   buildings. Image analysis uses URL-mode for new rows (HTTP-fetch →
   base64) per the Phase 11.0 enrich/image_analysis.py adapter.
   Estimated cost: ~$10-20 Anthropic API.
3. `python3 run.py embed-rate` — refresh `4_buildings_final.json` and
   quality rating. Pass criterion: ≥97/100.

**Cost gate**: step 2 is the only paid leg. Pause after step 1 if
budget approval is needed.

**Status**: ready to launch; waiting on user OK for the LLM cost.

---

### Phase 9 — Image hosting strategy: Path C (cover→R2, gallery→URLs only) [user-ratified, schema work pending]

**Origin**: handoff from make_web research terminal (2026-04-28). Full
memo at `make_web/research/infra/02-image-hosting-strategy.md` §15.
Hybrid hosting decision: Pinterest-style hotlinking for galleries,
R2 ownership for cover image only. ~80% storage drop, swipe UX preserved.

**Make DB scope** (this repo's pickup):
1. **New crawler behavior** (already in place via Phase 11.0 for
   metalocus; Architizer/Archello/Divisare already URL-only):
   - Cover image → continue download → R2 (current upload behavior)
   - Gallery photos + drawings → URL only
2. **Schema additions** to `architecture_vectors` (additive ALTER):
   ```sql
   ADD COLUMN IF NOT EXISTS cover_image_cdn_url    TEXT,    -- R2 public URL
   ADD COLUMN IF NOT EXISTS gallery_image_urls     TEXT[],  -- source CDN URLs
   ADD COLUMN IF NOT EXISTS cover_blurhash         TEXT;    -- ~30-byte placeholder
   ```
3. **BlurHash precompute** in `enrich/image_analysis.py` — cover only,
   ~30ms/image, ~5-10 min for a 500-building batch.
4. **Existing 3,465 metalocus production records**: stay as-is, no
   backfill.

**make_db responses to memo's open questions**:
- (a) **Naming**: agree with `cover_image_cdn_url`. Source-side fallback
  URL goes in `gallery_image_urls[]`. Existing
  `cover_image_url_divisare` / `divisare_gallery_urls[]` kept as-is
  (legacy data); make_web normalizer handles both.
- (b) **BlurHash library**: `blurhash` (pure Python).
- (c) **Metalocus uniformity**: agree — drop gallery R2 uploads for
  ALL new crawls regardless of source (already implemented for
  metalocus via Phase 11.0).
- (d) **Thumbnail variants**: defer. Cloudflare Image Resizing on top
  of `cover_image_cdn_url` later.

**Cross-repo signal commitment**: when this schema work lands + first
batch ships with the new pattern, append `Make DB Path C ready` line
to `make_web/.claude/Task.md ## Handoffs`.

**Implementation sketch** (~1 day):
- `upload/neon_strict.py`: ALTER + UPSERT new 3 columns
- `enrich/image_analysis.py`: compute blurhash for cover, write to row
- `core/utils.py` (or new `core/blurhash_helper.py`):
  `compute_blurhash(url_or_bytes) -> str`

**Status**: schema decision ratified. Implementation deferred until the
running Archello full crawl finishes (~10 days) so we don't churn
schema while Archello is writing. Phase 11c can run before/after Phase 9
— independent.

---

### Phase 9.5 — Multi-source canonical extension

**Goal**: extend `canonical_buildings_strict.json` from 2-source
(metalocus + divisare) to 4-source (+ architizer + archello). Today's
2,488 records grow as Architizer/Archello matches are folded in.

**Steps**:
1. Extend `canonical/match_architects.py` to also match against
   `architizer_firms` (firm slug as canonical ID) and
   `archello_brands_seen` (brand_id as canonical ID).
2. Extend `canonical/match_buildings.py` to also match against
   `architizer_projects.id` and `archello_projects.id`.
3. Extend `canonical/build.py` to fold the additional matches into
   `canonical_buildings_strict.json`, with provenance per field.
4. Update `canonical/qc.py` invariants if multi-source introduces new
   nullability patterns.

**Implementation sketch** (~1-2 days):
- New verdict tier: `accept_multi` when ≥2 sources confirm a building
- Source precedence for conflicting fields: Divisare > Architizer >
  Archello > metalocus (editorial floor; subject to tweak)

**Status**: blocked on Archello full crawl completing AND on Phase 9
schema decision (so the new schema columns land before we re-build
the canonical artefact and re-upload).

---

### Phase 10 — Cross-source image dedupe + quality ranking [proposed, after Phase 9.5]

**Goal**: same building from N sources → unified gallery of
best-quality deduped image URLs. Same drawing on Architizer + Archello
= one row, not two.

**Pipeline** (post-canonical, after multi-source canonical extension):
1. **Fingerprint**: for each known image URL on a canonical building,
   fetch image bytes, compute `imagehash.phash(img, hash_size=16)`
   (256-bit), record `(url, phash, width, height, file_size)`. Discard
   bytes; keep metadata only (~100 B/image; 37K images for strict
   canonical 2,488 buildings = ~4 MB).
2. **Cluster**: per building, pairwise Hamming distance on phashes.
   Threshold ≤8 → same image. Cluster.
3. **Quality rank within cluster**:
   1. larger `width × height` wins
   2. tie → larger `file_size` wins
   3. tie → editorial-floor source order: Divisare > Architizer > Archello > Archdaily
4. **Output**: `data/canonical/canonical_image_gallery.json` with
   per-building sorted, deduped
   `[{url, kind, w, h, sources: [src1, src2]}, ...]`.
5. **Update canonical**: `build.py` reads this and overwrites
   `image_paths` with the ranked best-quality URL list.

**Cost / risk**:
- Fingerprint: ~37K images × ~1s download + hash = 8-parallel → 2-4
  hours on the strict canonical scope. Full multi-source 200K+ images
  would be ~8 hours parallel.
- pHash false positives on simple line drawings (similar empty space)
  — 256-bit hash + threshold 8 mitigates; spot-check a sample.

**Implementation sketch** (~½ day):
- `canonical/image_dedup.py` (new): fingerprint + cluster + rank
- `canonical/build.py` (update): consume gallery JSON for `image_paths`
- `requirements.txt` (update): `imagehash`, `pillow`

**Status**: blocked on Phase 9 + Phase 9.5.

---

### Phase 12 — Atmosphere drift re-processing [cost-gated, user-approval needed]

> Renamed from "Phase 6 (atmosphere)" to avoid collision with the
> roadmap's Phase 6 (Upload).

- `data/reports/vocab_migration.json` lists 2,784 buildings whose
  atmosphere value is not in V2 vocab (`organic`, `communal`,
  `historic`, …)
- Before mass re-processing: dispatch `researcher` to investigate
  whether V2 atmosphere vocab should be expanded (cheaper than
  re-analyzing all 2,784) — see "Vocabulary evolution" below.
- Then dispatch `orchestrator` → `reprocessor` workflow with cost estimate.

**Status**: open, waiting on the vocabulary research outcome.

---

### Vocabulary evolution — atmosphere
- Research question: does V2 atmosphere's 12-value enum (`Serene`,
  `Dynamic`, `Raw`, …) adequately describe architectural buildings, or
  does the fact that 80% of Sonnet's historical output fell outside it
  suggest the vocab is too narrow?
- Researcher owns the investigation; orchestrator decides.

---

## In Progress

- **Phase 15 (multi-team QC refactor)** — cmux 5-workspace layout +
  AGENTS.md + reviewer_gate.py + match_phash_check.py + phash_cache.py
  all landed and self-heal-loop verified end-to-end (2 dispatch + 2
  reviewer cycles, 0 user intervention). Currently running:
  `python3 -m canonical.phash_cache --build --workers 8` in background
  (PID 21376, ETA ~24h, ~700K image fetches across 4 sources, $0).
  Next on completion: dispatch matcher to re-run Stage A/B with phash
  gate (#35).
- **Phase 8 (Archello full crawl)** — PID 19814, started 2026-04-28
  ~23:59 KST. 22,887 / ~135K projects done; ~111K pending. ETA ~10 days.
  Resume-friendly via `pending_projects.status`. No further action
  needed unless it dies.

---

## Resolved

(rolling window — most recent ~12)

- **Phase 15 infra (2026-05-06)** — cmux 5-workspace multi-team
  architecture (`tools/cmux_setup.sh` + `dispatch.sh` + `poll.sh`),
  AGENTS.md baseline for codex sessions, 4 team-* agent files
  (crawler/matcher/enricher/reviewer), `canonical/reviewer_gate.py`
  blocking QC for Stage A/B/D/F, `canonical/match_phash_check.py`
  + 4 unit tests (golden bld_026977 BLOCK case),
  `canonical/phash_cache.py` + 1 unit test, `dispatch.sh` paste-mode
  fix (Enter ×2). Self-heal hard caps in core/config.py
  (CODEX_RETRY_CAP=5, CODEX_COST_CAP_USD=20). Commits `79c2f88`,
  `7a370f5`, `810af85`, `3b1cb11`, `6e987b5`, `b6f7730`, `2aeb2ce`,
  `3c26e6f`. Suspended T2 enrichment at 90/288 batches (will be
  re-run via team-enricher post-phash-gate). Reviewer caught
  pre-existing OOV (style="Futuristic" in 2 batches) + 1824
  visual_description length issues that prior per-batch validation
  missed — confirms gate's value before going wide.
- **Phase 11.0 + 11a — metalocus URL-only crawler conversion + resume**
  (2026-04-29): added `METALOCUS_DOWNLOAD_IMAGES = False` flag, new
  `buildings.cover_image_url` / `gallery_image_urls` /
  `drawing_image_urls` columns, `attach_image_urls` helper,
  `enrich/export.py` URL fields, `enrich/image_analysis.py` URL fetch
  adapter. Resumed crawl: 9,203 articles processed, 3,422 new buildings
  added (3,873 → 7,295). 11,897 pending images marked skipped (Path C).
  Disk +0 GB. Commit `27d6796`.
- **fix(utils)** (2026-04-28): `core.utils.fetch_page` returns None on
  terminal HTTP error instead of raising — fixed Archello full crawl
  dying on TooManyRedirects.
- **Phase 8 — Archello crawler** (2026-04-28, code; full crawl runs in
  background): `crawl/archello/{db,parsers,crawler}.py` mirror of
  Architizer pattern + per-project BIM-spec details
  (`archello_project_details`). Sitemap walk: 135,065 URLs. Pilot 1,000
  done; full crawl launched. Commit `8b4afaf`.
- **Phase 7 — Architizer crawler** (2026-04-28): 5 phases, sitemap +
  projects + firms + A+Awards. 10,632 projects + 2,802 firms + 14,975
  award entries (2013-2025). `data-data` JSON regex parser. Commits
  `718008c`, `cf6a035`, `932a3c5`.
- **Research** (2026-04-28): 4-site recon (Architizer, ArchDaily,
  Archello, Arquitectura Viva) + cross-target priority. All 5 schema
  docs in `.claude/research/`. Commit `b6c5073`.
- **Refactor** (2026-04-28): codebase organized into 5 stage-aligned
  subpackages (`core/`, `crawl/{metalocus,divisare}/`, `enrich/`,
  `canonical/`, `upload/`, `tools/`). 32 file moves with `git mv`. CLI
  surface unchanged (`run.py <subcmd>`). Commit `4d51eea`.
- **upload_strict** (2026-04-28): in-place migration script for
  `canonical_buildings_strict.json` → `architecture_vectors`. ALTER +
  DELETE 977 + UPSERT 2,488. `--dry-run` / `--confirm` /
  `--skip-delete` safety flags. Commit `06509cf`.
- **build_canonical --strict** (2026-04-28): drops pure orphans (928)
  + article-style entries (49) + name cleanup ("X by Y" suffix +
  editorial hook). Result 2,488 records. Commit `2cad94d`.
- **Stage B — matching pipeline** (2026-04-26): match_architects.py
  (1,489 confident architect matches), match_buildings.py (720
  confident project matches), build_canonical.py (canonical artefact
  assembly with per-field provenance). Commit `d8e94b1`.
- **Stage A — metalocus architect alias consolidation** + canonical QC
  (2026-04-25): 226 raw alias variants → 2,188 canonical clusters.
  Commit `4a7e9f6`.
- **Phase 1 (Divisare crawler)** (Apr 2026): 4-phase crawler mirror of
  metalocus pattern; 29,936 lite projects + 12,759 architects (~99%
  deep-fetched as of 2026-04-29).

---

## Handoffs

Append-only cross-agent signals. Rolling window — keep the last ~20 entries.

- RESEARCH-COMPLETE: arquitectura-viva-schema — `.claude/research/arquitectura-viva-schema.md` (2026-04-28). Verdict: moderate, contingent on browser-UA reconnaissance. WebFetch blocked by Cloudflare AI-bot policy. ai-train=no flag — user policy call needed. Schema unverified from snippets.
- RESEARCH-COMPLETE: architizer-schema — `.claude/research/architizer-schema.md` (2026-04-28). Verdict: **EASY**. Public read, sitemap, Cloudflare passthrough, JSON in `data-data` div. A+Awards = unique value. ~7h full crawl.
- RESEARCH-COMPLETE: archello-schema — `.claude/research/archello-schema.md` (2026-04-28). Verdict: **MODERATE**. Public read, ai-train=no flag, Cloudflare blocks ClaudeBot but passes browser UA. Per-project BIM-spec via `data-key` JSON = unique value. ~135K projects.
- RESEARCH-COMPLETE: archdaily-schema — `.claude/research/archdaily-schema.md` (2026-04-28). Verdict: **technically EASY, legally HOSTILE** (split — user must decide). Sitemap, no Cloudflare, `cXenseParse:project-*` meta cleaner than Divisare. ToS prohibits scraping; user posture decision required.
MATCH-DONE: phash_check v1
- REVIEWER-PASS: phash_check v1 — 4/4 unit tests pass; scope clean (canonical/match_phash_check.py + tests/ only); BLOCK fires iff a_n>=2 AND b_n>=2 AND zero cross-source phash cluster overlap (Hamming<=8); golden bld_026977-style fixture BLOCKs as required.
MATCH-DONE: phash_cache_code v1
- REVIEWER-PASS: phash_cache_code v1 — 1/1 test passes (build + resume); scope clean (canonical/phash_cache.py + tests/); cache format `{"<source>:<source_id>": [<phash_hex>, …]}` matches match_phash_check reader; CLI `--build [--limit N] [--source <name>] [--workers 8]` present; resume via data/canonical/phash_cache_progress.json + per-row skip + write_every=100 atomic flush; reuses canonical/image_dedup.fetch_image_metadata (no duplication); per-future try/except + fetcher=None tolerated; imagehash.phash hash_size=16 (256-bit) inherited from image_dedup.
MATCH-ESCALATE: phash_cache_smoke fetch all-fails: 100 rows processed, 0 rows with phashes
MATCH-DONE: matcher_phash_integration v1
MATCH-DONE: phash_cache_perf v1
- REVIEWER-PASS: phash_cache_perf v1 — 2/2 tests pass (resume + inter-row parallelism, 50 rows × 4 URLs in <5s @ 50ms latency); scope clean (canonical/phash_cache.py + tests/); per-row `_fetch_phashes` removed, replaced by `_fetch_chunk` that submits all `(key, idx)→url` futures from a chunk to a single ThreadPoolExecutor (phash_cache.py:271-318); rows yielded only after `state[key]["remaining"] == 0` so done-set integrity preserved; write_every=100 flush + final flush retained; `_iter_pending_chunks` streams via sqlite cursor in chunks (default 1000) — 175K rows not loaded at once; workers default = 32 (build_cache:325, CLI:387); on-disk cache format unchanged — spot-check live cache shows `divisare:100046 → ['f3c01e96…' (64-char hex)]`, 1,655 keys so far, readable by match_phash_check unchanged.
MATCH-DONE: phash_cache_scope_filter v1
MATCH-ESCALATE: phash_build_codex_blocked process died within 8s; launch emitted "zsh:1: nice(5) failed: operation not permitted"; ps sandbox blocked, escalated ps found no process
MATCH-ESCALATE: stage_b_codex_blocked nohup matcher PID 56397 died within 8s; logs/match_buildings_v2.log empty; manual backup data/id_registry_buildings.before_phash.json created
MATCH-DONE: phash_gate_tiebreaker v1

## Research Ready

Queue for the researcher agent. Each entry is a concrete question with
context, not an open-ended prompt.

- *(none yet)*

MATCH-DONE: phash_cache_scope_filter v1 — commit 1470965
MATCH-ESCALATE: phash_build_codex_blocked — codex sandbox blocks nohup nice() syscall; long-running background ops must run from DB-MAIN
MATCH-DONE: phash_cache_running v2 (PID=58924, DB-MAIN nohup, --canonical-only --workers=32, ETA ~1.5h)
MATCH-DONE: stage_b_phash_running v2 (PID=58304, DB-MAIN nohup, phash gate active, --canonical-output _v2.json --reset, ETA 30m-1h)
- REVIEWER-BLOCK: stage_b_v2 cycle 1/5 — phash gate over-aggressive: gate codified invariants PASS + multi-source spot-check 10/10 same-building + golden bld_026977 correctly split, BUT phash_blocks 0/20 justified (every sampled BLOCK is a real same-building pair; ~3,230 likely false-negatives = 40% of attempted multi-source joins destroyed). Likely root cause: cross-source CDN re-encoding pushes phash drift > Hamming 8 even on identical photos AND/OR sources curate disjoint photo selections. Diagnosis + 4 fix options in `.claude/escalations/stage_b_v2_20260506_185654.md`. Recommended first try: raise cross-source Hamming threshold 8 → 16, OR demote gate from absolute veto to tie-breaker (require name+architect+year disagreement before BLOCK).
- REVIEWER-PASS: stage_b_v3 cycle 2/5 — tiebreaker fix substantially recovers recall (multi_source 4,787 → 7,685, +60%) while preserving golden case (bld_026977 now correctly multi-source: divisare:516837 + archello:116669, both "Terrace House" Melbourne 2021 by Austin Maynard Architects; Terracotta House Melbourne 2020 archello:105709 not falsely merged). phash_blocks=3,296 split 3,152 tiebreaker_pass / 144 block. Spot-checks: tiebreaker_pass 10/10 true-positive same-building ✓, multi-source 9/10 ✓ (= 27/30 codified threshold). Open House regression fixed (bld_012954 = divisare:329562 + archello:42564). 2 small WARNs for matcher to consider next cycle, neither blocking: (a) verdict=block residual 0/10 justified — every sampled hard-block has matching name + matching architect but archello side has city=None / year=None; tiebreaker appears to count missing metadata as "disagreement" (should treat null as no-signal); scale small (~144 entries); (b) bld_021496 "Asilo nido" multi-source merge: divisare:423914 (2007, Studio Brambilla Orsoni, Grandate) + architizer:273210 (2025, Archiplan Studio, San Giacomo) — 18-year span violates year_span_max_2 yet reviewer_gate passes vacuously because v3 cluster shape lacks per-source year fields (Reviewer-side gate code may need patch in a later turn). Artefact may proceed.
MATCH-DONE: phash_tiebreaker_null_signal v1
REVIEWER-STATIC-BLOCK: stage_b_v4 — tests/counts/phash block spot-checks pass, but golden archello:105709 is absent from v4/current registry rather than present in a different cluster
MATCH-DONE: stage_b_phash_running v4 (PID per /tmp/matcher_pid.txt, DB-MAIN nohup, null-tolerant tiebreaker, ETA 30m-1h)
ENRICH-DONE: d1_batches_v4_ready v1
- REVIEWER-PASS: stage_b_v4 cycle 3/5 — tiebreaker_pass 10/10 justified ✓, multi-source 10/10 justified ✓ (= 30/30 well above 27/30 codified threshold), hard-block 0/5 justified (residual gate-precision issue at trivial scale). Recall continues to grow: multi_source 4,787 → 7,685 → 7,799; phash_blocks=3,297 split 3,275 tiebreaker_pass / 22 hard-block (down from 144 in v3). Null-tolerant tiebreaker fix verified: bld_025274 "16 | office building in italy" (archello city=None, year=None) now correctly merges, was wrongly hard-blocked in v3. Override of prior STATIC-BLOCK acknowledged — archello:105709 (Terracotta House) lives in `tiebreak_pairs_v4.json` mid-band manual queue, not a missing artefact. One non-blocking WARN for next cycle: residual 22 hard-blocks all share the same defect — matcher's "name disagree" signal is case-sensitive and doesn't normalize Unicode whitespace; all 5 sampled blocks have name+architect+city agreement modulo casing/whitespace ("Blank"/"BLANK", "Naked\\xa0House"/"Naked House", "FENDI Factory"/"Fendi Factory") and trip only because year_diff=2 lands just over `>1` threshold while the buggy name signal also fires. One-line fix in matcher (casefold + Unicode whitespace collapse before name comparison) likely dissolves all 22. Trivial scale (~0.06% of 38,295 clusters) and accept-side decisions are 100% accurate — artefact may proceed.
MATCH-DONE: phash_name_normalize v1
REVIEWER-STATIC-PASS: stage_b_v5 — tests 11/11 pass; clusters=38,295 multi_source=7,817; phash_blocks=3,298 (3,295 tiebreaker_pass / 3 block); all 3 hard-blocks have zero phash overlap, normalized name token_set_ratio<90, and both years present with diff>1; bld_026977 golden intact and archello:105709 remains in tiebreak_pairs_v5.
- REVIEWER-PASS: stage_b_v5 cycle 4/5 — tiebreaker_pass 10/10 justified ✓, multi-source 10/10 justified ✓ (30/30 well above 27/30 codified threshold), hard-block 1/3 justified (only bld_000477 "Casa N" vs "Casa T" by Agraz Arquitectos is unambiguously different — distinct letter-named houses; the other 2 are borderline-likely-same: bld_004008 "Ecohotel «Friend House»" vs "Ecohotel \"Friend House\"" by Ryntovt Design Ukraine — same branded eco-hotel, year_diff=2, Dnepropertrovsk vs Kirovskoye village; bld_018882 "house#05" vs "HOUSE—05" by andrea rubini — same numbering, year_diff=2, Milano vs F636+R2 Plus Code). **Convergence trajectory:** v2 0/20 of 3,230 (structural) → v3 0/10 of 144 (boundary) → v4 0/5 of 22 (name normalization) → v5 1/3 of 3 (noise floor, ~0.008% of 38,295 clusters). Recall continues climbing: 4,787 → 7,685 → 7,799 → 7,817. Accept-side decisions remain 100% accurate. Artefact ships. Optional non-blocking polish for matcher (don't re-run for this alone): both borderline blocks share year_diff=2 + same architect + minor name punctuation variance; raising year-diff threshold from `>1` to `>3` would likely dissolve them. Cycle 4/5 used; no further cycle recommended.
MATCH-DONE: e_image_dedup_code v1 (commit 6da8838) — code ready, request DB-MAIN nohup launch
MATCH-DONE: e1_e2_d2_split_code v2 (commit 31b679b) — ready for DB-MAIN nohup launches
ENRICH-DONE: d1_enrich_codex_running v1 (PID=3798, log=logs/d1_enrich_codex.log, results=data/canonical/d1_results.jsonl, 183/38295, failures=0)
MATCH-DONE: e_image_dedup_running v1 (PID=16021, DB-MAIN nohup, --workers 32, log=logs/e_image_dedup.log)
ENRICH-DONE: d1_enrich_resume2 (PID=96789, low reasoning, 17666 done resume)
MATCH-DONE: e2_vision_running (PID=96902, codex 5-type)
ENRICH-DONE: d2_cover_vision_running (PID=97062, codex cover Vision)
MATCH-PAUSED: e2_vision_5type — too many Vision calls (60 cluster/row × 38K rows = 3M, 700h ETA). Killed PID 12893 at 326 rows. Will re-implement as lightweight: filename heuristic + dimension-based default + Vision only on cid-level best image per type (5 calls/cid = 190K total vs 3M).
MATCH-DONE: hybrid_precommit_v1
ENRICH-DONE: dispatch_enrich_batch_v1
ENRICH-DONE: looks_idle_fix v1
ENRICH-DONE: d1_batch_20260509-201823 bld_023354..bld_023365 rows=10
ENRICH-DONE: d1_batch_20260509-202638 bld_023366..bld_023401 rows=30
ENRICH-NEEDS-CLARIFICATION: dispatch /tmp/dispatch-enricher-20260509-210502.md contains no executable plan, acceptance criteria, handoff format, or Input JSON
ENRICH-DONE: d1_batch_20260509-210954 bld_023402..bld_023438 rows=30
ENRICH-DONE: d1_batch_20260509-211340 bld_023440..bld_023483 rows=30
ENRICH-NEEDS-CLARIFICATION: D-1 resume loop dispatch missing Rule 1 smoke results, Rule 2 quota status, Rule 3 codex investigation reference, Rule 4 cost math, and ENRICH-COST-APPROVED handoff
ENRICH-DONE: d1_loop_122346 bld_023485..bld_023532 rows=30
ENRICH-DONE: d1_loop_122514 bld_023533..bld_023569 rows=30
ENRICH-DONE: d1_loop_122514 bld_023570..bld_023606 rows=30
ENRICH-DONE: d1_loop_122514 bld_023607..bld_023643 rows=30
ENRICH-DONE: d1_loop_122514 bld_023645..bld_023682 rows=30
ENRICH-DONE: d1_loop_122514 bld_023684..bld_023726 rows=30
ENRICH-DONE: d1_loop_122515 bld_023727..bld_023764 rows=30
ENRICH-DONE: d1_loop_122515 bld_023765..bld_023806 rows=30
ENRICH-DONE: d1_loop_122515 bld_023807..bld_023850 rows=30
ENRICH-DONE: d1_loop_122515 bld_023851..bld_023889 rows=30
ENRICH-DONE: d1_loop_122515 bld_023890..bld_023929 rows=30
ENRICH-DONE: d1_loop_122515 bld_023930..bld_023975 rows=30
ENRICH-DONE: d1_loop_122515 bld_023976..bld_024011 rows=30
ENRICH-DONE: d1_loop_122515 bld_024012..bld_024048 rows=30
ENRICH-DONE: d1_loop_122515 bld_024049..bld_024092 rows=30
ENRICH-DONE: d1_loop_122515 bld_024095..bld_024138 rows=30
ENRICH-DONE: d1_loop_122515 bld_024139..bld_024180 rows=30
ENRICH-DONE: d1_loop_122515 bld_024181..bld_024222 rows=30
ENRICH-DONE: d1_loop_122516 bld_024223..bld_024257 rows=30
ENRICH-DONE: d1_loop_122516 bld_024258..bld_024292 rows=30
ENRICH-DONE: d1_loop_122516 bld_024293..bld_024332 rows=30
ENRICH-DONE: d1_loop_122516 bld_024333..bld_024368 rows=30
ENRICH-DONE: d1_loop_122516 bld_024369..bld_024405 rows=30
ENRICH-DONE: d1_loop_122516 bld_024406..bld_024445 rows=30
ENRICH-DONE: d1_loop_122516 bld_024449..bld_024491 rows=30
ENRICH-DONE: d1_loop_122516 bld_024492..bld_024532 rows=30
ENRICH-DONE: d1_loop_122516 bld_024533..bld_024573 rows=30
ENRICH-DONE: d1_loop_122516 bld_024574..bld_024608 rows=30
ENRICH-DONE: d1_loop_122516 bld_024609..bld_024649 rows=30
ENRICH-DONE: d1_loop_122516 bld_024651..bld_024689 rows=30
ENRICH-DONE: d1_loop_122517 bld_024690..bld_024727 rows=30
ENRICH-DONE: d1_loop_122517 bld_024728..bld_024763 rows=30
ENRICH-DONE: d1_loop_122517 bld_024764..bld_024804 rows=30
ENRICH-DONE: d1_loop_122517 bld_024805..bld_024847 rows=30
ENRICH-DONE: d1_loop_122517 bld_024849..bld_029258 rows=30
ENRICH-DONE: d1_loop_122517 bld_029260..bld_029297 rows=30
ENRICH-DONE: d1_loop_122517 bld_029298..bld_029336 rows=30
ENRICH-DONE: d1_loop_122517 bld_029337..bld_029378 rows=30
ENRICH-DONE: d1_loop_122517 bld_029379..bld_029420 rows=30
ENRICH-DONE: d1_loop_122517 bld_029423..bld_029459 rows=30
ENRICH-DONE: d1_loop_122517 bld_029460..bld_029506 rows=30
ENRICH-DONE: d1_loop_122517 bld_029507..bld_029551 rows=30
ENRICH-DONE: d1_loop_122517 bld_029552..bld_029601 rows=30
ENRICH-DONE: d1_loop_122518 bld_029602..bld_029644 rows=30
ENRICH-DONE: d1_loop_122518 bld_029645..bld_029677 rows=30
ENRICH-DONE: d1_loop_122518 bld_029678..bld_029720 rows=30
ENRICH-DONE: d1_loop_122518 bld_029721..bld_029761 rows=30
ENRICH-DONE: d1_loop_122518 bld_029762..bld_029796 rows=30
ENRICH-DONE: d1_loop_122518 bld_029797..bld_029828 rows=30
ENRICH-DONE: d1_loop_122518 bld_029829..bld_029863 rows=30
ENRICH-DONE: d1_loop_122518 bld_029864..bld_029897 rows=30
ENRICH-DONE: d1_loop_122518 bld_029898..bld_029929 rows=30
ENRICH-DONE: d1_loop_122518 bld_029930..bld_029970 rows=30
ENRICH-DONE: d1_loop_122518 bld_029971..bld_030004 rows=30
ENRICH-DONE: d1_loop_122518 bld_030005..bld_030038 rows=30
ENRICH-DONE: d1_loop_122519 bld_030039..bld_030075 rows=30
ENRICH-DONE: d1_loop_122519 bld_030076..bld_030112 rows=30
ENRICH-DONE: d1_loop_122519 bld_030114..bld_030145 rows=30
ENRICH-DONE: d1_loop_122519 bld_030146..bld_030182 rows=30
ENRICH-DONE: d1_loop_122519 bld_030183..bld_030213 rows=30
ENRICH-DONE: d1_loop_122519 bld_030214..bld_030245 rows=30
ENRICH-DONE: d1_loop_122519 bld_030246..bld_030280 rows=30
ENRICH-DONE: d1_loop_122519 bld_030281..bld_030314 rows=30
ENRICH-DONE: d1_loop_122519 bld_030316..bld_030353 rows=30
ENRICH-DONE: d1_loop_122519 bld_030354..bld_030391 rows=30
ENRICH-DONE: d1_loop_122519 bld_030392..bld_030442 rows=30
ENRICH-DONE: d1_loop_122519 bld_030443..bld_030482 rows=30
ENRICH-DONE: d1_loop_122520 bld_030483..bld_030532 rows=30
ENRICH-DONE: d1_loop_122520 bld_030533..bld_030568 rows=30
ENRICH-DONE: d1_loop_122520 bld_030569..bld_030602 rows=30
ENRICH-DONE: d1_loop_122520 bld_030603..bld_030638 rows=30
ENRICH-DONE: d1_loop_122520 bld_030639..bld_030676 rows=30
ENRICH-DONE: d1_loop_122520 bld_030677..bld_030721 rows=30
ENRICH-DONE: d1_loop_122520 bld_030723..bld_030764 rows=30
ENRICH-DONE: d1_loop_122520 bld_030765..bld_030811 rows=30
ENRICH-DONE: d1_loop_122520 bld_030815..bld_030854 rows=30
ENRICH-DONE: d1_loop_122520 bld_030855..bld_030894 rows=30
ENRICH-DONE: d1_loop_122520 bld_030895..bld_030936 rows=30
ENRICH-DONE: d1_loop_122520 bld_030937..bld_030978 rows=30
ENRICH-DONE: d1_loop_122521 bld_030979..bld_031023 rows=30
ENRICH-DONE: d1_loop_122521 bld_031024..bld_031057 rows=30
ENRICH-DONE: d1_loop_122521 bld_031058..bld_031094 rows=30
ENRICH-DONE: d1_loop_122521 bld_031095..bld_031130 rows=30
ENRICH-DONE: d1_loop_122521 bld_031131..bld_031164 rows=30
ENRICH-DONE: d1_loop_122521 bld_031165..bld_031198 rows=30
ENRICH-DONE: d1_loop_122521 bld_031199..bld_031236 rows=30
ENRICH-DONE: d1_loop_122521 bld_031237..bld_031270 rows=30
ENRICH-DONE: d1_loop_122521 bld_031272..bld_031304 rows=30
ENRICH-DONE: d1_loop_122521 bld_031305..bld_031343 rows=30
ENRICH-DONE: d1_loop_122521 bld_031344..bld_031376 rows=30
ENRICH-DONE: d1_loop_122521 bld_031377..bld_031409 rows=30
ENRICH-DONE: d1_loop_122522 bld_031410..bld_031443 rows=30
ENRICH-DONE: d1_loop_122522 bld_031444..bld_031487 rows=30
ENRICH-DONE: d1_loop_122522 bld_031488..bld_031523 rows=30
ENRICH-DONE: d1_loop_122522 bld_031524..bld_031557 rows=30
ENRICH-DONE: d1_loop_122522 bld_031558..bld_031600 rows=30
ENRICH-DONE: d1_loop_122522 bld_031602..bld_031646 rows=30
ENRICH-DONE: d1_loop_122522 bld_031647..bld_031687 rows=30
ENRICH-DONE: d1_loop_122522 bld_031689..bld_031723 rows=30
ENRICH-DONE: d1_loop_122522 bld_031724..bld_031761 rows=30
ENRICH-DONE: surface_dispatch_v1
ENRICH-DONE: dispatch_metrics_v1
ENRICH-DONE: d2_vision_path_v1
ENRICH-DONE: d1_failure_analysis_v1
ENRICH-DONE: d1_dispatch_resilience_v1
ENRICH-NEEDS-CLARIFICATION: d2_self_smoke_v1 reason=response did not contain a valid JSON array
ENRICH-DONE: d2_failure_analysis_v1
ENRICH-NEEDS-CLARIFICATION: d2_self_smoke_v2 reason=download_failed
ENRICH-NEEDS-CLARIFICATION: d1_resume_v2 nohup_pid=2520 exited_no_log_no_rows log=/Users/kms_laptop/Documents/archi-tinder/make_db/logs/d1_resume_20260511_011731.log
ENRICH-NEEDS-CLARIFICATION: d1_resume_v3 reason=pid_exited_immediately_no_log_no_rows pid=44596 log=/Users/kms_laptop/Documents/archi-tinder/make_db/logs/d1_resume_20260511_013852.log
ENRICH-DONE: codex_bg_diag_v1
MATCH-DONE: code_name_split_repair v1 rows=39775 created=16 source_refs_lost=0 source_refs_duplicated=0
ENRICH-DONE: code_name_split_refresh v1 d1=30 e1=30 e2=30 d2=30 embeddings=30
REVIEW-BLOCK: country_conflict_triage v1 review_required=68 noise_likely=35 semantic_review=33
MATCH-DONE: country_conflict_split v1 bld_018178 created=1 source_refs_lost=0 source_refs_duplicated=0
ENRICH-DONE: country_conflict_refresh v1 d1=2 e1=2 e2=2 d2=2 embeddings=2
REVIEW-BLOCK: country_conflict_triage v2 review_required=67 image_supported=64 semantic_review_no_image_link=3
REVIEW-PASS: generic_merge_audit_final v1 review_required=0 country_conflict_flags=67 evidence_supported_or_waived=true
CRAWL-DONE: dispatch-test-short v1
ENRICH-NEEDS-CLARIFICATION: d2_image_backfill_resume5 quota_stop weekly=5% rows_new=5080 unique_backfill_cids=7979 bad_json=0 run=.claude/ops/runs/20260513_233321-d2-image-backfill-resume5-quota-stop.md
ENRICH-DONE: d2_image_backfill_quota_stop_partial rows=7979/23008 remaining=15029 strict_qc=WARN upload_validator=PASS integrity=COMPLETE publishable_missing_image_or_cover=0
ENRICH-NEEDS-CLARIFICATION: d2_resume6 blocked usage_limit_and_disk rows=1769 cumulative_d2=9748 retry_usage_limit=40 image_unavailable=1 run=.claude/ops/runs/20260514_000139-d2-image-backfill-resume6.md
ENRICH-NEEDS-CLARIFICATION: d2_resume7 blocked usage_limit rows=370 cumulative_d2=10118 remaining_d2=12890 retry_usage_limit=40 run=.claude/ops/runs/20260514_d2-image-backfill-resume7.md
ENRICH-NEEDS-CLARIFICATION: d2_resume8 blocked usage_limit rows=3680 cumulative_d2=13798 remaining_d2=9209 retry_usage_limit=10 run=.claude/ops/runs/20260514_d2-image-backfill-resume8.md
ENRICH-NEEDS-CLARIFICATION: d2_resume9 stopped_network rows=2345 cumulative_d2=16143 remaining_d2=6864 retry_network=4375 guard=transient_download_abort run=.claude/ops/runs/20260516_d2-image-backfill-resume9.md
ENRICH-DONE: d2_resume10_complete rows=6864 cumulative_d2=23007 image_unavailable=1 remaining_d2=0 strict_qc=PASS upload_validator=PASS generic_merge_audit=PASS integrity=COMPLETE publishable=39736 nonpublishable=40 run=.claude/ops/runs/20260516_d2-image-backfill-resume10.md
OPS-DONE: post_d2_recovery_defaults v1 final_artifact=resume10_complete live_upload=blocked_user_gate cleanup_delete=blocked_user_gate job=.claude/ops/jobs/20260517_post_d2_recovery_defaults.md
OPS-DONE: post_d2_disk_cleanup v1 deleted_superseded_strict_artifacts est_reclaimed=5.1GiB final_artifact=resume10_complete live_upload=blocked_user_gate job=.claude/ops/jobs/20260517_post_d2_disk_cleanup.md
OPS-DONE: upload_readiness_packet_resume10 v1 strict_qc=PASS upload_validator=PASS generic_merge_audit=PASS integrity=COMPLETE review_packet=.claude/ops/reviews/20260517_upload_readiness_resume10.md live_upload=blocked_user_gate
OPS-DONE: canonical_v2_neon_loader_prepared v1 tool=tools/canonical_v2_neon_loader.py schema=data/reports/canonical_v2_neon_schema.sql input=resume10_complete live_write_requires=--confirm-db-write job=.claude/ops/jobs/20260517_canonical_v2_neon_loader.md
OPS-DONE: canonical_v2_neon_loader_smoke_fix v1 root_cause=existing_table_missing_created_at fix=additive_schema_evolution live_write=none dry_run_rolled_back
OPS-DONE: canonical_v2_neon_loader_n10_dry_run PASS rows=10 table_count_seen=39776 writes=rolled_back report=data/reports/canonical_v2_neon_loader_dry_run_n10.json
OPS-DONE: canonical_v2_neon_loader_inspect_fix v1 root_cause=datetime_report_serialization live_write=none
OPS-DONE: canonical_v2_neon_table_inspect v1 rows=39776 unique_pk=39776 publishable=39737 nonpublishable=39 needs_image_derived_backfill=23008 status=STALE_PRE_RESUME10 report=data/reports/canonical_v2_neon_table_inspect.json
OPS-DONE: canonical_v2_neon_loader_n100_fix v1 root_cause=tuple_count_report_conversion live_write=none dry_run_rolled_back
OPS-DONE: canonical_v2_neon_loader_n100_dry_run PASS rows=100 unique_pk=39776 needs_image_derived_backfill_seen=22913 writes=rolled_back report=data/reports/canonical_v2_neon_loader_dry_run_n100.json
OPS-DONE: canonical_v2_neon_upsert_full PASS rows=39776 unique_pk=39776 publishable=39736 nonpublishable=40 missing_embedding=0 needs_image_derived_backfill=0 writes=committed report=data/reports/canonical_v2_neon_loader_upsert_full.json
OPS-DONE: canonical_v2_completeness_c1_c2_started read_only=true job=.claude/ops/jobs/20260517_canonical_v2_completeness_c1_c2.md
OPS-DONE: canonical_v2_completeness_c1_c2 PASS rows=39776 high_confidence_candidates=0 review_needed=217 year_text=136 location_full=81 writes=reports_only job=.claude/ops/jobs/20260517_canonical_v2_completeness_c1_c2.md
OPS-DONE: canonical_v2_completeness_c25_started read_only=true job=.claude/ops/jobs/20260517_canonical_v2_completeness_c25.md
OPS-DONE: canonical_v2_completeness_c25 PASS review_items=217 safe_after_policy=91 keep_review=126 safe_project_year=91 safe_location_city=0 writes=reports_only job=.claude/ops/jobs/20260517_canonical_v2_completeness_c25.md
OPS-DONE: canonical_v2_completeness_c26_llm_location_started read_only=true approved_by_user=true job=.claude/ops/jobs/20260518_canonical_v2_completeness_c26_llm_location.md
OPS-DONE: canonical_v2_completeness_c26_llm_location PASS location_items=81 classified=81 apply_city=38 country_only_blocked=38 writes=reports_only report=data/reports/canonical_v2_llm_location_adjudication.json
OPS-DONE: canonical_v2_completeness_c3_started apply_scope=project_year91_location_city38 two_phase=true job=.claude/ops/jobs/20260518_canonical_v2_completeness_c3_apply.md
OPS-DONE: canonical_v2_completeness_c3 PASS affected_cids=129 project_year=91 location_city=38 strict_qc=PASS upload_validator=PASS review_needed_after=88 neon_rows_upserted=129 writes=committed job=.claude/ops/jobs/20260518_canonical_v2_completeness_c3_apply.md
OPS-DONE: canonical_v2_remaining_candidate_verification_started read_only=true remaining=88 job=.claude/ops/jobs/20260518_canonical_v2_remaining_candidate_verification.md
OPS-DONE: canonical_v2_remaining_candidate_verification PASS remaining=88 project_year_apply_candidates=15 project_year_keep=30 location_keep_city_null=38 location_manual=5 writes=reports_only report=data/reports/canonical_v2_remaining_review_verdict.json
OPS-DONE: canonical_v2_completeness_c4_started apply_scope=project_year15 two_phase=true job=.claude/ops/jobs/20260518_canonical_v2_completeness_c4_apply.md
OPS-DONE: canonical_v2_completeness_c4 PASS affected_cids=15 project_year=15 strict_qc=PASS upload_validator=PASS review_needed_after=73 neon_rows_upserted=15 writes=committed job=.claude/ops/jobs/20260518_canonical_v2_completeness_c4_apply.md
OPS-DONE: canonical_v2_crawler_gap_audit_c5_started read_only=true job=.claude/ops/jobs/20260518_canonical_v2_crawler_gap_audit_c5.md
OPS-DONE: canonical_v2_crawler_gap_audit_c5_complete status=PASS structured_candidates=0 city_raw=2 year_completion_signal=7 no_local_country=1967 no_local_city=2010 no_local_year=1273 review=.claude/ops/reviews/20260518_crawler_gap_audit_c5.md
OPS-DONE: canonical_v2_c5_local_candidate_verdict_complete status=PASS candidates=9 apply=1 keep_null=8 apply_candidate=bld_038824.project_year:1989 report=data/reports/canonical_v2_c5_local_candidate_verdict.md
OPS-DONE: canonical_v2_c6_web_search_smoke_complete status=PASS n=10 likely_safe=3 partial=3 unresolved=2 conflict=1 keep_null=1 report=data/reports/canonical_v2_c6_web_search_smoke.md
OPS-DONE: canonical_v2_c6_candidate_queue_complete status=PASS rows_missing_any=2163 c5_local_apply_ready=1 c6_seed_apply_review=3 policy_null_review=177 web_search_location=769 web_search_location_year=1065 web_search_year=148 n100=100 report=data/reports/canonical_v2_c6_candidate_queue.md
OPS-DONE: canonical_v2_c6_n100_web_search_smoke_complete status=PASS n=100 apply_after_exact_source_review=34 partial=20 conflict_manual=4 policy_null_or_manual_null=9 unresolved=33 report=data/reports/canonical_v2_c6_n100_web_search_smoke.md
OPS-DONE: canonical_v2_c6_source_ranked_apply_queue_complete status=PASS local_apply_ready=1 c6_seed=3 c6_n100_candidates=34 combined_pool=38 ready_direct=1 requires_exact_source_review=37 report=data/reports/canonical_v2_c6_source_ranked_apply_queue.md
OPS-DONE: canonical_v2_c6_narrow_apply_prep_complete status=PASS direct_apply=1 candidate=bld_038824.project_year:1989 web_candidates_blocked=37 live_mutation=blocked_user_gate report=data/reports/canonical_v2_c6_narrow_apply_prep.md
OPS-DONE: canonical_v2_c6_apply_complete status=PASS affected_cids=1 project_year=1 strict_qc=PASS upload_validator=PASS review_needed_after=72 neon_rows_upserted=1 writes=committed job=.claude/ops/jobs/20260522_canonical_v2_c6_apply.md
OPS-DONE: canonical_v2_c6_source_ranked_apply_queue_correction status=PASS reason=seed_candidates_overlap_n100 combined_with_overlap=38 unique_pool=35
OPS-DONE: canonical_v2_c6_source_ranked_apply_queue_correction2 status=PASS unique_requires_exact_source_review=34 c6_prep_blocked_web_candidates=34
OPS-DONE: canonical_v2_c6_exact_source_review_complete status=PASS reviewed=34 safe_rows=33 manual_only=1 field_updates=73 country=29 city=26 project_year=18 report=data/reports/canonical_v2_c6_exact_source_review.md
OPS-DONE: canonical_v2_c7_local_apply_complete status=PASS affected_cids=33 country=29 city=26 project_year=4 strict_qc=PASS upload_validator=PASS review_needed_after=72 neon=pending_approval job=.claude/ops/jobs/20260522_canonical_v2_c7_local_apply.md
OPS-DONE: canonical_v2_c7_neon_upsert_complete status=PASS mode=upsert rows_loaded=33 total_rows=39776 unique_pk=39776 writes=committed report=data/reports/canonical_v2_neon_loader_upsert_completeness_c7_affected.json
OPS-DONE: canonical_v2_c8_local_final_pin_complete status=PASS reviewed=72 affected_cids=27 country=8 city=18 project_year=8 strict_qc=PASS upload_validator=PASS review_needed_after=48 high_confidence=0 neon=pending_approval manifest=data/reports/canonical_v2_release_manifest.completeness_c8.json report=data/reports/canonical_v2_final_quality_report.completeness_c8.md
OPS-DONE: canonical_v2_c8_neon_upsert_complete status=PASS mode=upsert rows_loaded=27 total_rows=39776 unique_pk=39776 writes=committed report=data/reports/canonical_v2_neon_loader_upsert_completeness_c8_affected.json
OPS-DONE: codex_to_claude_handoff_packet_complete status=PASS data_audit_rows=39776 unique_ids=39776 cleanup_files=1426 cleanup_size=15.4GiB large_files=23 deletion=none neon_write=none handoff=data/reports/codex_to_claude_handoff_20260522.md
