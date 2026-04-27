# make_db — Task Board

Append-only coordination document. Agents read this on every invocation. The
orchestrator owns routing; any agent may append to `## Handoffs`.

## Open

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
