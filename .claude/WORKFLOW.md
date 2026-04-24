# make_db — Workflow

How the agent layer operates. Five operational cases cover ~all real work.
Anything that doesn't fit goes through orchestrator for routing.

## Agents (6)

| Agent | Model | Role |
|---|---|---|
| `orchestrator` | opus | Top-level router. Reads Goal/Task/REPORT on every invocation. Dispatches sub-agents. Manages the quality iteration loop (max 2 cycles). Never runs pipeline or upload directly. |
| `batch-worker` | sonnet | Runs `pipeline_harness` on a new batch. Monitors progress. Reports counts + quarantined buildings back. Does not interpret quality. |
| `quality-reviewer` | sonnet | Runs `run.py quality {review,rate,diagnose}`. Interprets results. Decides: ship / iterate / re-process subset. Emits fix orders with concrete building-id lists. |
| `reporter` | sonnet | Updates `.claude/REPORT.md` after state-changing operations. Moves completed Task.md items from In Progress → Resolved. Keeps the rolling window trimmed. |
| `researcher` | opus | Investigates ambiguous decisions (vocab expansion, eval thresholds, sub-style hierarchy). `WebSearch` + `WebFetch` tools. Writes to `.claude/research/`. Does not touch code or data. |
| `upload-guard` | sonnet | Pre-upload review gate. Verifies quality rating + audit reports + explicit user approval. Runs `upload.py --dry-run`. Gates but never runs the real upload — user runs it after `UPLOAD-READY` appears in Handoffs. |

## Terminal Model

Single-terminal is the default for make_db. Unlike make_web, there's no
frontend/backend concurrency that warrants a 4-terminal split. Research
*can* run in a second terminal if investigation is long-running (e.g., vocab
evolution), but most of the time it's:

```
main terminal ── orchestrator ── sub-agents (as dispatched)
```

## Cases

### Case 1 — New batch processing
*"Process batch 8's 408 buildings through enrichment/analysis."*

```
orchestrator
  ├─ batch-worker: `run.py harness --enqueue-only` → verify count
  ├─ batch-worker: `run.py harness` → drain queue
  ├─ quality-reviewer: `review` + `rate` → judgment
  │   ├─ pass/warning → reporter → Handoffs: BATCH-DONE
  │   └─ fail        → see Case 2
  └─ orchestrator → Task.md: mark batch resolved
```

### Case 2 — Quality iteration (after Case 1 fails)
*"Quality rating says visual_depth < 70%. Fix, don't upload."*

```
orchestrator
  ├─ quality-reviewer: identify weakness (which field, which building_ids)
  ├─ if auto-fixable (vocab, naming) → run `quality fix` → re-rate
  ├─ else (missing content)          → targeted reprocess (Case 3)
  └─ max 2 iterations; if still failing after 2, append Handoffs: ESCALATE
     and hand back to user for judgment call.
```

### Case 3 — Targeted re-processing
*"2,784 atmosphere-drift records need re-analysis."*

```
orchestrator
  ├─ researcher (optional): should we expand vocab instead of re-processing?
  │   └─ answer: expand vocab [cheaper] OR re-process [accept Sonnet's current values]
  ├─ quality-reviewer: produce building-id list + --dry-run reprocess plan
  ├─ user approval gate (cost-bearing work) → Handoffs: REPROCESS-APPROVED
  ├─ orchestrator: `reprocess.py --from-vocab-migration [--limit N] --apply`
  ├─ batch-worker: drain the re-enqueued tasks
  ├─ quality-reviewer: post-reprocess rating
  └─ reporter: update REPORT.md with delta
```

### Case 4 — Prompt tuning
*"Can we break the 96/100 quality ceiling?"*

```
orchestrator
  ├─ label_golden.py --source opus → seed golden set (≥30 entries, stratified)
  ├─ eval.py → baseline score with current prompt_version
  ├─ user or researcher proposes prompt change (few-shot, multi-pass, etc.)
  ├─ edit prompts/schema → prompt_version auto-bumps
  ├─ eval.py again → delta
  ├─ if positive delta: keep; if negative or zero: revert
  └─ reporter: Handoffs: PROMPT-VERSION-BUMPED: <old> → <new>, delta=<score>
```

### Case 5 — Vocabulary evolution
*"Should atmosphere include 'communal' and 'historic'?"*

```
orchestrator → Task.md Research Ready: appends specific question
researcher (usually second terminal, long-running)
  ├─ WebSearch + WebFetch: architecture vocabulary references
  ├─ reads vocab.py + sample of affected buildings
  ├─ writes `.claude/research/atmosphere-expansion.md`
  └─ Handoffs: RESEARCH-COMPLETE: atmosphere-expansion
user reviews research document → approves or rejects
if approved:
  orchestrator
    ├─ edits vocab.py + LEGACY_MIGRATIONS
    ├─ migrate_vocab.py --apply (with fresh audit report)
    ├─ stage3_embed.py (re-embed affected records)
    └─ reporter updates REPORT.md with vocab version bump
```

### Case 6 — Upload approval gate
*"We're ready. Push to Neon + R2."*

```
orchestrator
  ├─ quality-reviewer: final `quality rate` — judgment must be 'Ready'
  ├─ upload-guard:
  │   ├─ verify rating_report.json / review_report.json / fix_report.json current
  │   ├─ verify no pending tasks in tasks.db
  │   ├─ verify no quarantined buildings without explanation
  │   ├─ `upload.py --dry-run` → inspect counts, row shape
  │   └─ Handoffs: UPLOAD-READY (count=N, quality=X/100)
  └─ USER manually runs `python3 upload.py` after reading UPLOAD-READY.
     The agent layer never runs upload.py. Ever.
```

## Handoff Signals (Task.md § Handoffs)

Append-only. Each signal is a single line: `<SIGNAL>: <payload>`. Recognized:

- `BATCH-DONE: N` — Case 1 completed; N new buildings processed.
- `REPROCESS-APPROVED: <scope>` — user OK'd a re-processing run.
- `REPROCESS-DONE: <scope>, delta=<score>` — re-processing complete.
- `PROMPT-VERSION-BUMPED: <old> → <new>, delta=<score>` — Case 4 outcome.
- `RESEARCH-REQUESTED: <topic>` — orchestrator wants researcher to investigate.
- `RESEARCH-COMPLETE: <topic>` — researcher done; findings at `.claude/research/<topic>.md`.
- `UPLOAD-READY: count=<N>, quality=<X>/100` — upload-guard cleared; user may run upload.
- `ESCALATE: <reason>` — orchestrator hit the 2-iteration limit; hands back to user.

## Non-goals for this layer

- No automated `git push`. No automated `upload.py`. No automated schema edits
  to `vocab.py` without explicit approval (migrations are a user decision).
- No parallel agents chasing the same batch. If two terminals are open,
  coordinate via Handoffs — last write wins on Task.md sections that aren't
  append-only (Open/In Progress/Resolved).
- No shadow state. Everything an agent "knows" lives in Goal.md / Task.md /
  REPORT.md / the actual data files. No in-memory assumptions between invocations.
