# make_db — Task Board

Append-only coordination document. Agents read this on every invocation. The
orchestrator owns routing; any agent may append to `## Handoffs`.

## Open

### Phase 9 — Image hosting strategy: Path C (cover→R2, gallery→URLs only) [user-ratified, schema work pending]

**Origin**: handoff from make_web research terminal (2026-04-28). Full
memo at `make_web/research/infra/02-image-hosting-strategy.md` §15.
Hybrid hosting decision: Pinterest-style hotlinking for galleries,
R2 ownership for cover image only. ~80% storage drop, swipe UX preserved.

**Make DB scope** (this repo's pickup):
1. **New crawler behavior** (batch 8 onwards + ALL new sources):
   - Cover image → continue download → R2 (current upload/neon.py behavior)
   - Gallery photos + drawings → **stop downloading**; URLs only
2. **Schema additions** to `architecture_vectors` (additive ALTER):
   ```sql
   ADD COLUMN IF NOT EXISTS cover_image_cdn_url    TEXT,    -- R2 public URL (computed)
   ADD COLUMN IF NOT EXISTS gallery_image_urls     TEXT[],  -- source CDN URLs (hotlinked)
   ADD COLUMN IF NOT EXISTS cover_blurhash         TEXT;    -- ~30-byte placeholder hash
   ```
3. **BlurHash precompute** in image_analysis stage (Phase 2) — cover only,
   ~30ms/image, ~5-10 min for 500-building batch.
4. **Existing 3,465 metalocus production records**: stay as-is, no backfill.

**make_db responses to open questions** (a-d in memo):
- (a) **Naming**: agree with `cover_image_cdn_url` (clear it's our R2 CDN,
  not a source CDN). Source-side fallback URL goes in `gallery_image_urls[]`.
  Existing `cover_image_url_divisare` / `divisare_gallery_urls[]` kept as-is
  (legacy data); make_web normalizer handles both old + new names.
- (b) **BlurHash library**: `blurhash` (pure Python) — easier deploy, no C
  ext needed, 30ms vs 3ms doesn't matter at our pipeline rate.
- (c) **Metalocus uniformity**: agree — drop gallery R2 uploads for ALL
  new crawls regardless of source. Per-source bifurcation isn't worth the
  complexity; existing 3,465 metalocus rows are untouched (R2 keeps the
  full set already on disk).
- (d) **Thumbnail variants**: defer. Can add via Cloudflare Image Resizing
  on top of the cover_image_cdn_url later without re-uploading.

**Cross-repo signal commitment**: when this schema work lands + first batch
ships with the new pattern, append `Make DB Path C ready` line to
`make_web/.claude/Task.md ## Handoffs`.

**Implementation sketch** (~1 day):
- `upload/neon_strict.py`: ALTER + UPSERT new 3 columns
- `crawl/{source}/db.py`: rename `cover_image_url` → `cover_image_cdn_url`
  for the R2-uploaded copy; keep source URL in `gallery_image_urls[0]`
- `enrich/image_analysis.py`: compute blurhash for cover, write to row
- `core/utils.py` (or new `core/blurhash_helper.py`): `compute_blurhash(path) -> str`

**Status**: schema decision ratified. Implementation deferred until current
3 crawlers (Architizer projects, Archello full, Divisare deep-fetch) finish.

### Phase 10 — Cross-source image dedupe + quality ranking [proposed, after Phase 9]

**Goal**: same building from N sources → unified gallery of best-quality
deduped image URLs. Same drawing on Architizer + Archello = one row, not two.

**Pipeline** (post-canonical, after multi-source canonical extension):
1. **Fingerprint**: for each known image URL on a canonical building,
   fetch image bytes, compute `imagehash.phash(img, hash_size=16)` (256-bit),
   record `(url, phash, width, height, file_size)`. Discard bytes; keep
   metadata only (~100 B/image; 37K images for strict canonical 2,488
   buildings = ~4 MB).
2. **Cluster**: per building, pairwise Hamming distance on phashes.
   Threshold ≤8 → same image. Cluster.
3. **Quality rank within cluster**:
   1. larger `width × height` wins
   2. tie → larger `file_size` wins
   3. tie → editorial-floor source order: Divisare > Architizer > Archello > Archdaily
4. **Output**: `data/canonical/canonical_image_gallery.json` with per-building
   sorted, deduped `[{url, kind, w, h, sources: [src1, src2]}, ...]`.
5. **Update canonical**: `build.py` reads this and overwrites `image_paths`
   with the ranked best-quality URL list.

**Cost / risk**:
- Fingerprint: ~37K images × ~1s download + hash = 8-parallel → 2-4 hours
  on the strict canonical scope. Full multi-source 200K+ images would be
  ~8 hours parallel.
- pHash false positives on simple line drawings (similar empty space) —
  256-bit hash + threshold 8 mitigates; spot-check a sample.
- pHash misses cropped + watermarked variants (still treats as different).
  Acceptable — those probably ARE different images for our purposes.

**Implementation sketch** (~½ day):
- `canonical/image_dedup.py` (new): fingerprint + cluster + rank
- `canonical/build.py` (update): consume gallery JSON for `image_paths`
- `requirements.txt` (update): `imagehash`, `pillow`

**Status**: blocked on Phase 9 (need new schema + cover/gallery split first)
AND multi-source canonical extension (need Architizer/Archello matches folded
into canonical_buildings_strict before there are cross-source images to dedupe).

### Phase 6 — atmosphere drift re-processing [cost-gated, user-approval needed]
- `data/reports/vocab_migration.json` lists 2,784 buildings whose atmosphere
  value is not in V2 vocab (`organic`, `communal`, `historic`, …)
- Before mass re-processing: dispatch `researcher` to investigate whether V2
  atmosphere vocab should be expanded (cheaper than re-analyzing all 2,784)
- Then dispatch `orchestrator` → `reprocessor` workflow with cost estimate

### Vocabulary evolution — atmosphere
- Research question: does V2 atmosphere's 12-value enum (`Serene`, `Dynamic`,
  `Raw`, …) adequately describe architectural buildings, or does the fact
  that 80% of Sonnet's historical output fell outside it suggest the vocab
  is too narrow? Researcher owns the investigation; orchestrator decides.

## In Progress

*(none)*

## Resolved

(rolling window — most recent 10)

- **Phase 8A** — Python consolidation. 27 → 22 files. Created `quality.py`
  (review+fix+rate+diagnose merged); folded `agent_qc.py` into `vocab.py`;
  ported `pipeline.py` CLI into `run.py` (`crawl`, `export-dedup`, `embed-rate`);
  added `run.py` subcommands for `migrate-vocab`, `label-golden`, `eval`,
  `reprocess`, `quality`. Single CLI entry point. (2026-04-25)
- **Phase 8B** — Claude-native agent orchestration layer. 6 agents
  (orchestrator, batch-worker, quality-reviewer, reporter, researcher,
  upload-guard) + Goal.md + Task.md + WORKFLOW.md. (2026-04-25)
- **Phase 7** — Anthropic prompt caching added to both agents. Batches API
  deferred. (2026-04-25)
- **Phase 6 (tooling)** — `reprocess.py` ships; targets 2,784 atmosphere-drift
  records. Data re-processing deferred pending vocab-expansion research. (2026-04-25)
- **Phase 5 (infrastructure)** — Few-shot examples mechanism with
  auto-bumping `prompt_version`. No prompts changed yet. (2026-04-25)
- **Phase 4** — `eval.py` + `label_golden.py` (Opus or current-source). (2026-04-25)
- **Phase 3** — `tasks_db.py` SQLite ledger, `pipeline_harness.py` rewritten as
  queue worker, crash-safe. (2026-04-25)
- **Phase 2** — Tool-use structured output (zero `json.loads` on model output). (2026-04-25)
- **Phase 1** — `vocab.py` single source of truth. 7 callers consolidated.
  `review.py` V1-vocab bug fixed. 2,784 atmosphere-drift records surfaced. (2026-04-25)
## Handoffs

Append-only cross-agent signals. Rolling window — keep the last ~20 entries.

- *(no entries yet; Phase 8B is the first work routed through this board)*
- RESEARCH-COMPLETE: arquitectura-viva-schema — `.claude/research/arquitectura-viva-schema.md` (2026-04-28). Verdict: moderate, contingent on browser-UA reconnaissance. WebFetch blocked by Cloudflare AI-bot policy (ClaudeBot disallowed in robots.txt). robots.txt also flags `Content-Signal: ai-train=no` — user policy call needed. Magazine-issue → project linkage is the unique value-add but unverified from snippets. Recommend a 30-min browser-UA Phase 0 fetch (sitemap + 3 sample `/works/` + 2 issue pages) before any crawl code.
- RESEARCH-COMPLETE: architizer-schema — `.claude/research/architizer-schema.md` (2026-04-28). Verdict: **EASY**. Public read, no auth, published sitemap (~10,785 projects, ~2,802 firms), Cloudflare passthrough with normal Mozilla UA. Parsing unlock: every project page embeds full project state as JSON in `data-data='{...}'` on editable divs — single regex + `json.loads` yields PK, name, completion_date, building_size, constr_status, description, hero. The unique value-add is **A+Awards** (`winners.architizer.com/{year}/{Tier}/`) — a curated quality cohort (~1-2K projects across 12+ years × 4 tracks) with structured Jury / Popular Choice / Finalist / Special Mention tiers; no other source provides this. ToS yellow flag: robots.txt explicitly bans `GPTBot` (not us, but watch for generic AI-bot escalation). Recommend awards-driven ingest as primary, sitemap-driven as secondary. ~7 hours single-threaded full crawl at 2s/req.
- RESEARCH-COMPLETE: archello-schema — `.claude/research/archello-schema.md` (2026-04-28). Verdict: **MODERATE**. Public read, no auth required for spec metadata. Cloudflare blocks ClaudeBot (WebFetch 403) but passes Mozilla UA. Sitemap-driven discovery is clean (`/sitemaps/index.xml` lists 1142 child sitemaps: ~135K projects, ~64K products, ~178K brands). **Headline finding (user policy call needed before any code):** robots.txt declares `Content-Signal: search=yes, ai-train=no` citing EU Directive 2019/790 Art. 4 — explicit reservation of rights against AI training. Unique value-add is **structured per-project product specs** via `<div class="ah-project-details__item" data-key='{"brand_id":N,"project_id":M}'>` with title (role/category) + linked `/product/{slug}` + `/brand/{slug}`; numeric IDs are stable join keys. Binome sample: 10 specs incl. "Chair, stool, lighting → /product/piloti-bench, /product/floe-3, /product/elsie-chair-2 by /brand/appareil-atelier" — the BIM-source-list angle is real. Spec depth uneven: 3-10 items per project; award shortlist projects skew higher. BIM/CAD file downloads gated by lead-gen form (`DownloadCatalogueForm[name|email|location|profession|captcha]`) — spec-metadata-only is realistic scope. Recommend Option D (targeted enrichment of buildings already in metalocus/Divisare) as cheapest test before committing to full crawl.
- RESEARCH-COMPLETE: archdaily-schema — `.claude/research/archdaily-schema.md` (2026-04-28). Verdict: **technically EASY, legally HOSTILE** (split verdict — user must decide). Public read, no auth, no Cloudflare (nginx + AWS CloudFront passthrough), server-rendered HTML on project pages, ~790 KB per page. Sitemap-index is gzipped 18-child structure: sitemap1+2+3 hold ~102,000 `/{numeric_id}/{slug}` URLs of which roughly **half are projects** (~50K, estimate from 20-URL random sample) — others are articles/news/op-eds, distinguishable via `archdaily:type='Selected Projects'` meta tag. Headline extraction surface is the **`cXenseParse:project-*` meta layer** (project-office, project-location with comma-separated city,region,country, project-year, project-category-tier-1, project-photographer, project-curator, project-manufacturer multi-value) — cleaner than Divisare's CSS-selector-on-sidebar approach. JSON-LD block exists but is empty `{}`. Architect pages (`/office/{slug}`) are slug-only and **NOT in sitemap** — must be harvested out-of-band from project page anchors. `/search/projects` listing UIs are JS-client-rendered (sitemap path bypasses this). 3 sequential fetches at 0.5s gap all 200 in 1.4-2.2s, no throttling. **Critical legal finding:** ToS at `/content/terms-of-use` explicitly prohibits "automatic device (such as a robot or spider) ... to copy or 'scrape' the Website ... without the express written permission of ArchDaily" and limits use to "personal, non-commercial." Search-engine carve-out is narrow. Site itself rolls out the welcome mat (clean sitemap, structured meta, no anti-bot) but ToS is unambiguous. User must pick a posture: (a) email partnerships for permission, (b) frame as personal/private DB, or (c) cross-validation only (on-demand fetch for already-known buildings, no bulk mirror) **before** engineering starts. Tech effort: ~1-2 days parser+scheduler; full crawl ~28 hours single-threaded at 2s/req.

## Research Ready

Queue for the researcher agent. Each entry is a concrete question with
context, not an open-ended prompt.

- *(none yet)*
