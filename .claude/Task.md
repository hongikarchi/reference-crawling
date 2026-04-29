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

- **Phase 8 (Archello full crawl)** — PID 19814, started 2026-04-28
  ~23:59 KST. 22,887 / ~135K projects done; ~111K pending. ETA ~10 days.
  Resume-friendly via `pending_projects.status`. No further action
  needed unless it dies.

---

## Resolved

(rolling window — most recent ~12)

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

## Research Ready

Queue for the researcher agent. Each entry is a concrete question with
context, not an open-ended prompt.

- *(none yet)*
