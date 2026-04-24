# make_db

Agent-driven pipeline: crawl metalocus.es → enrich + analyze → review → upload to Neon + R2.

## How to start a session

Non-trivial work routes through the **orchestrator** agent
(`.claude/agents/orchestrator.md`). It reads Goal/Task/REPORT/WORKFLOW first,
then dispatches sub-agents per the Cases in `.claude/WORKFLOW.md`.

For one-off scripted work (e.g., "just check status"), invoke the relevant
script directly via the CLI below — agent layer is for multi-step operations.

## Documents

- `.claude/Goal.md` — vision + quality targets + non-goals (read first by every agent)
- `.claude/Task.md` — open / in-progress / resolved + handoff signals
- `.claude/WORKFLOW.md` — 6 operational Cases + handoff signal vocabulary
- `.claude/REPORT.md` — live system state (counts, quality, known gotchas)
- `.claude/PROJECT.md` — schemas + vocabularies + tool specs (technical reference)
- `.claude/agents/*.md` — 6 sub-agent definitions (orchestrator, batch-worker,
  quality-reviewer, reporter, researcher, upload-guard)
- `~/.claude/plans/db-fuzzy-lerdorf.md` — full 7-phase architecture roadmap

## Rules

- Read `.claude/Goal.md` before non-trivial decisions.
- `.claude/PROJECT.md` is the single source of truth for schemas; `vocab.py` is
  the single source of truth for vocabularies. `.claude/PROJECT.md` §5 mirrors
  `vocab.py` for human reading — code wins on conflict.
- All modules live at project root — no subdirectories.
- Numbered filenames (`1_`, `2_`, `3_`, `4_`) must be consistent everywhere.
- **`data/id_registry.json` — NEVER delete.** Stable building_id assignments live
  here; losing it breaks every downstream join.
- **Never run `upload.py` without explicit user approval.** `upload-guard`
  agent gates it; the user runs it.
- Agents NEVER edit `vocab.py` on their own judgment — vocabulary changes are
  user decisions, grounded by the `researcher` agent.
- **Git workflow (solo dev, single branch):**
  - All work happens on `main`. No feature branches.
  - The `orchestrator` agent commits autonomously per logical change or phase
    (no need to ask the user "should I commit now?").
  - **`git push` is user-initiated only.** No agent ever runs `git push`,
    `git push --force`, or any remote-modifying command. Local commits stack
    until the user explicitly requests a push.

## CLI

All operations route through `run.py` (12 subcommands, organized in 4 groups):

```bash
# Build / extend the dataset
python3 run.py make-db [--limit N]   # crawl + export + dedup
python3 run.py crawl --articles 500  # crawl only (resume image downloads)
python3 run.py export-dedup          # SQLite → 1_buildings_raw.json + dedup
python3 run.py harness               # enrich + analyze + QC (Anthropic API)
python3 run.py embed                 # final embeddings → 4_buildings_final.json
python3 run.py embed-rate            # embed + quality review/fix/rate

# Quality + auditing
python3 run.py quality review        # validate fields, vocab, embeddings
python3 run.py quality fix           # auto-normalize + clean
python3 run.py quality rate          # 6-dim quality score
python3 run.py quality diagnose      # distribution analysis
python3 run.py stats                 # crawler + pipeline + rating summary
python3 run.py harness --status      # tasks.db + JSON counts (no API key needed)

# Vocab / eval / re-processing
python3 run.py migrate-vocab [--apply]
python3 run.py label-golden --sample 30 [--source opus]
python3 run.py eval [--limit N] [--only enrich|analyze]
python3 run.py reprocess --from-vocab-migration [--apply]

# Upload (manual gate — agents never run this)
python3 upload.py --dry-run
python3 upload.py
```

## Reference

- **File structure + data layout** — see `.claude/PROJECT.md` §3
- **Pipeline tool specs (stage1-3, quality, agents, upload)** — see `.claude/PROJECT.md` §7
- **Schema (PostgreSQL `architecture_vectors`)** — see `.claude/PROJECT.md` §4
- **Controlled vocabularies** — `vocab.py` is canonical; `.claude/PROJECT.md` §5 mirrors

## Known Gotchas

- `DB_PASSWORD` excluded from required-fields check — Neon needs it but check is bypassed.
- Images live at `images/{building_id}/` after dedup (reorganized from `images/{slug}/`).
- Don't re-run `stage2_dedup.py` on already-processed buildings.
- `python3 upload.py --reset` only on first upload or after schema changes (drops the table — be sure).
- 2,784 / 3,465 production records have out-of-V2 atmosphere values (`organic`, `communal`, …). See `data/reports/vocab_migration.json`. Re-processing requires user cost approval — see `quality-reviewer` agent's atmosphere-drift playbook.
