# make_db DB Ops Mode

Updated: 2026-05-12

This is the current default operating model for finishing the make_db
pipeline. It supersedes the old always-on "Claude DB-MAIN dispatches to
5 cmux teams" pattern for day-to-day work. The old 5-team layout remains
available as legacy/expanded mode when a stage truly needs it.

## Goal

Finish and maintain a high-quality architecture recommendation database:
accurate canonical identity, useful enrichment, strong image coverage, and
safe manual upload to Neon/R2.

The operating model optimizes for:

- Long-running batch work that may take hours.
- Frequent structural and semantic quality checks.
- Low token waste.
- Recoverable state after terminal clears, context compaction, crashes, or
model handoff.

## Default Roles

| Workspace | Runs | Role |
|---|---|---|
| `DB-CODEX-OPS` | Codex | Operational control plane: status restore, job cards, code, smoke, validation, commits. |
| `DB-RUNNER` | shell | Long-running Python processes only. Writes PID/log/run records. No LLM agent. |
| `DB-MONITOR` | shell | Log tails, counts, progress checks. No LLM agent. |
| `DB-CLAUDE-GATE` | Claude Code | Semantic and architecture checkpoint review only. Reads compact review packets. |
| `DB-CODEX-WORKER` | Codex optional | Bounded side work: 1-2 file patch, diff review, code search. Not always running. |

Legacy workspaces (`DB-CRAWLER`, `DB-MATCHER`, `DB-ENRICHER`,
`DB-REVIEWER`) are not deleted automatically. Snapshot them first, then
use them only when expanded cmux team mode is explicitly useful.

## Source Of Truth

Terminal memory is not source of truth. Durable state lives in files:

| Path | Purpose |
|---|---|
| `.claude/Goal.md` | Product mission and quality targets. |
| `.claude/REPORT.md` | Current pipeline state. |
| `.claude/Task.md` | Handoffs and notable state transitions. |
| `.claude/ops/jobs/` | Job cards: scope, owner, smoke ladder, abort rules. |
| `.claude/ops/runs/` | Run records: command, PID, log, output, status. |
| `.claude/ops/reviews/` | Claude Gate review packets and verdicts. |
| `.claude/ops/snapshots/` | cmux screen snapshots before clears/restarts. |
| `.claude/ops/decisions.md` | User approvals and large decisions. |

Agents communicate through these files plus terse handoff lines. Screen
polling is only a convenience.

For cmux screen I/O in DB Ops Mode, use `tools/db_ops_send.sh` and
`tools/db_ops_poll.sh`. The old `tools/dispatch.sh` / `tools/poll.sh`
remain for legacy `DB-MATCHER` / `DB-ENRICHER` / `DB-CRAWLER` teams.

## Start Of Session

Codex Ops starts every session with:

```bash
tools/db_ops_status.sh
```

Before restarting or clearing old cmux workspaces:

```bash
tools/db_ops_snapshot_cmux.sh
```

Then create the ops layout if needed:

```bash
tools/db_ops_cmux_setup.sh
```

Optional screen I/O:

```bash
tools/db_ops_send.sh claude-gate "Review .claude/ops/reviews/<packet>.md"
tools/db_ops_poll.sh claude-gate 80
```

## Job Workflow

Every non-trivial stage uses a job card.

1. Create job card:
   ```bash
   tools/db_job_card.py d1-resume --owner ENRICHER --stage D-1 \
     --write-scope "enrich/, tools/d1_*, data/canonical/d1_results*.jsonl" \
     --input "data/canonical/canonical_buildings_4source.json" \
     --output "data/canonical/d1_results.jsonl"
   ```

2. Run `N=10` smoke:
   - Verify output schema.
   - Inspect sample quality.
   - Measure tokens/cid when LLM calls are involved.
   - Record result in the job card or `.claude/Task.md`.

3. Run `N=100` smoke:
   - Measure failure rate.
   - Extrapolate full cost.
   - Stop and ask user if projected weekly burn is high or quality fails.

4. Launch full run only after smokes pass and required approvals exist.

5. Record long runs:
   ```bash
   tools/db_run_record.py d1-resume \
     --cmd "python3 -m tools.d1_enrich_codex --resume" \
     --pid "$PID" \
     --log "logs/d1_resume_YYYYMMDD_HHMMSS.log" \
     --stage D-1
   ```

6. Monitor with logs and counts. Do not keep an LLM agent waiting on a
   long process.

7. Generate a review packet for Claude Gate only when semantic judgment or
   architecture checkpoint review is needed.

8. Promote artifacts only after structural validation and required Claude
   Gate/user approvals.

## Claude Gate Rules

Claude is used for expensive judgment, not routine operations.

Use Claude Gate for:

- Final or pre-full semantic sample checks.
- Ambiguous canonical merge/split judgment.
- Visual description faithfulness.
- Vocab/threshold/user-level decisions.
- Upload readiness review.

Do not use Claude Gate for:

- pytest/lint/schema checks.
- Full log reading.
- Broad source scans.
- Long-running process monitoring.
- Repeated dispatch/poll orchestration.

Review packets must be compact and task-specific:

```text
stage:
question:
artifact:
sample rows:
known risks:
requested verdict: PASS | WARN | BLOCK
```

Allowed verdict lines:

```text
CLAUDE-GATE-PASS: <stage> <one-line reason>
CLAUDE-GATE-WARN: <stage> <row_ids> <one-line reason>
CLAUDE-GATE-BLOCK: <stage> <row_ids> <one-line reason>
```

## Codex Rules

Codex Ops owns code, validation, and process coordination.

- Default reasoning: low.
- Use medium/high only for hard root-cause analysis or architecture.
- Use subagents only for bounded side tasks:
  - code search,
  - diff review,
  - 1-2 file patch.
- Do not run full batch LLM work without smoke ladder.
- Do not edit `core/vocab.py`.
- Do not delete or truncate `data/id_registry_*.json`.
- Do not modify or run `upload/`.
- Do not push.

## Parallel Work Rules

Parallelize processes, not judgment.

Safe in parallel:

- A long D/E Python run in `DB-RUNNER`.
- Log/count monitoring in `DB-MONITOR`.
- Validator or packet generation in `DB-CODEX-OPS`.
- A bounded worker patch in a disjoint write scope.

Unsafe in parallel:

- Two writers touching the same artifact.
- Full D-1/D-2 runs without smokes.
- Multiple semantic reviewers using different criteria.
- Upload work before explicit user approval.
- Vocab or threshold changes without explicit user decision.

## User Approval Gates

Ask the user before:

- Full run with material LLM cost.
- Weekly remaining quota below configured safety threshold.
- Any vocab change.
- Lowering matcher thresholds.
- Re-enriching already completed rows.
- Upload dry-run or upload execution.
- Clearing old cmux terminals before snapshot.

## Existing Terminal Memory

Never clear old terminals first. Convert their memory to artifacts:

1. Snapshot cmux screens into `.claude/ops/snapshots/<timestamp>/`.
2. Ask each live agent for a 10-line handoff only when useful.
3. Cross-check `git status`, logs, PID files, Task handoffs.
4. Only then clear/restart stale terminals.

## Caveman Usage

- Status and handoffs: `ultra`.
- Plans and risk explanations: `full` or normal clarity.
- JSON, schema, commands, and prompts: no compression.
- Do not compress `AGENTS.md` or `CLAUDE.md`; these hold guardrails.
