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

## Research Ready

Queue for the researcher agent. Each entry is a concrete question with
context, not an open-ended prompt.

- *(none yet)*
