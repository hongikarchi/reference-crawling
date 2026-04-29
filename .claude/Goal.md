# make_db — Goal

## Vision

Build and maintain a high-quality vector database of architecture projects for
the ArchiTinder swipe recommendation engine. The database's utility is judged
by one question:

> **Will an architect get meaningful, visually accurate recommendations from
> this database?**

Everything downstream — scoring, review loops, re-processing decisions —
serves this judgment, not metric optimization for its own sake.

## Scope

- Data pipeline only. No serving, no UI.
- Bilingual-friendly (enrichment produces English names but source data is
  international; the embedding model is multilingual).
- Production target: Neon Postgres + Cloudflare R2, behind manual upload
  approval.

## Quality Targets (guidance, not hard gates)

```
minimum_buildings:    500 (currently 3,465; scale is solved)
required 100%:        building_id, name_en, program, embedding
required  90%:        architect, description, atmosphere, style
required  80%:        visual_description, material_visual, color_tone
program balance:      no single program > 35% of total
visual coverage:      avg >= 1 photo per building
description length:   avg >= 200 chars
```

Claude judgment matters more than any specific number. A 72/100 with known
weaknesses is more useful than 96/100 with silent drift.

## Non-goals

- **No metric-gaming.** If a fix raises `quality rate` without improving the
  recommendation experience, it's not a fix.
- **No silent vocab migration.** Every rewrite is logged to an audit channel.
  See `vocab.py`.
- **No speculative refactoring.** The 27 Python files are too many — but
  consolidation is scheduled work, not a drive-by cleanup.
- **No unapproved uploads.** `upload.py` runs only when the user explicitly
  confirms. Every agent respects this.

## Operating Principles

1. **Batch-oriented thinking.** Work is sized in batches (50–500 buildings).
   Per-building operations are the exception, not the rule.
2. **Quality loop > single pass.** `review` → `fix` → `rate` → re-run the
   part that needs it, don't re-run everything.
3. **Idempotency by default.** `tasks_db.py` ensures re-running costs nothing
   when inputs and prompts are unchanged. Agents should never hesitate to
   re-invoke a step.
4. **Cost caution on re-processing.** The vision API is not cheap; the 2,784
   atmosphere-drift records are a real cost pool. Any full-population
   re-process needs explicit approval and a cost estimate.
5. **Surface signals truthfully.** If the golden set has `atmosphere=None` in
   80% of entries, the eval harness says so. No polished scores on broken
   baselines.

## Current State Reference

- `.claude/REPORT.md` — live per-source counts, canonical state, running processes
- `.claude/PROJECT.md` — schema + vocabularies + tool specs + 5-stage architecture
  + new-source runbook (§11) + phase-vs-stage distinction (§12)
- `.claude/Task.md` — open/in-progress/resolved work + cross-agent handoffs
- `.claude/WORKFLOW.md` — 8 operational Cases (per-batch, per-source, per-upload)
- `.claude/research/<source>-schema.md` — per-source recon for each crawled site
- `~/.claude/plans/db-fuzzy-lerdorf.md` — multi-phase roadmap with per-phase
  outcomes (Phases 0-11+; sequential, flat numbering)
