# make_db — AGENTS.md (for Codex CLI)

This file is the standard OpenAI Codex CLI baseline for any `codex`
session running inside the make_db repository. It is loaded
automatically when you start `codex` from this directory (Codex walks
upward from cwd and concatenates every `AGENTS.md` it finds).

If you are reading this in a Claude Code session, look at `CLAUDE.md`
instead — that is the Claude-specific entry point. The two files
intentionally cover the same project from different agent
perspectives.

---

## You are part of a 5-workspace cmux team

`make_db` runs as 5 cmux workspaces in one window. You are inside one of
the four "team" workspaces; the orchestrator lives in DB-MAIN. To know
which team you are, look at the cmux workspace title (`DB-CRAWLER`,
`DB-MATCHER`, or `DB-ENRICHER`) — `tools/cmux_setup.sh` sets this
automatically. Each team's full responsibilities, owned files, and hard
guardrails are documented in `.claude/agents/team-<team>.md` — **read
your team's file before any action.**

| Workspace | Team file | Owns |
|---|---|---|
| DB-CRAWLER | `.claude/agents/team-crawler.md` | Stage 1 — `crawl/<source>/*` |
| DB-MATCHER | `.claude/agents/team-matcher.md` | Stage A/B/E — `canonical/match_*.py` + `phash_cache.py` |
| DB-ENRICHER | `.claude/agents/team-enricher.md` | Stage C/D — `enrich/*` + harness |

(DB-REVIEWER runs Claude Code, not Codex. DB-MAIN runs Claude Code as
orchestrator.)

## How DB-MAIN sends you work

DB-MAIN runs `tools/dispatch.sh <team> "<message>"` which wraps
`cmux send` and types the message into your prompt followed by Enter.
You will see the message appear as if a user typed it. Treat each such
message as a task. Read it, decide what to do, do it, then append a
**handoff signal** to `.claude/Task.md` § Handoffs so DB-MAIN knows
you're done.

## Handoff signals you append (Task.md § Handoffs)

Append-only. One line per signal. Format: `<SIGNAL>: <payload>`.

- `CRAWL-DONE: <source> v<n>` — DB-CRAWLER completed a crawl phase.
- `MATCH-DONE: <stage> v<n>` — DB-MATCHER completed Stage A/B/E/F.
- `ENRICH-DONE: <scope> v<n>` — DB-ENRICHER completed Stage C/D for that scope.
- `<TEAM>-ESCALATE: <stage> exhausted self-heal` — you hit cap (5 cycles or $20); stop.

Full vocabulary in `.claude/WORKFLOW.md` § "Handoff Signals".

## Reviewer self-heal loop (your role inside it)

When DB-REVIEWER rejects your output:
1. DB-MAIN dispatches you with `"Fix per .claude/escalations/<file>; re-run; cycle <c+1>/5"`.
2. **Read** `.claude/escalations/<file>` — it has Reviewer's diagnosis +
   sample offending rows + a suggested fix.
3. **Diagnose root cause**, not symptom. The escalation usually points
   at one specific file or threshold.
4. Edit the code (you are codex — you have full edit/write/run access
   subject to the guardrails below).
5. Re-run **only the suspect data slice**, not the entire pipeline.
6. Append `<TEAM>-DONE: <stage> v<n+1>` to Handoffs.
7. DB-MAIN will re-dispatch DB-REVIEWER to re-evaluate.

Hard cap per stage attempt: **5 cycles OR $20 cumulative cost** (your
LLM + tool calls + Reviewer + re-runs). At cap, append
`<TEAM>-ESCALATE: <stage> exhausted self-heal` and **stop**. Do not
keep trying.

## HARD GUARDRAILS — never violated

You **never**:

1. Edit `core/vocab.py`. Vocabulary changes are user decisions only.
   If a Reviewer BLOCK suggests a vocab change, append the diagnosis
   to Handoffs as `RESEARCH-REQUESTED: vocab-<topic>` and stop.
2. Delete or truncate `data/id_registry_*.json`. Stable building/architect
   IDs live there; losing them breaks every downstream join.
3. Modify anything under `upload/` directory. Upload is the user's
   exclusive domain.
4. Run any `upload/*.py` script. Even `--dry-run`.
5. Run `git push`, `git push --force`, or any history-rewriting git
   command. You may `git commit` (per logical change), but never push.
6. Touch source code under another team's owner-set:
   - DB-CRAWLER does NOT touch `canonical/` or `enrich/`
   - DB-MATCHER does NOT touch `crawl/` or `enrich/`
   - DB-ENRICHER does NOT touch `crawl/` or `canonical/match_*.py`
7. Re-run already-completed enrichment without `RE-ENRICH-APPROVED:
   <scope>` in Handoffs (DB-ENRICHER specifically — wasted spend).
8. Lower a matcher quality threshold without `THRESHOLD-OVERRIDE-APPROVED:
   <param>=<value> <reason>` in Handoffs (DB-MATCHER specifically).

## Behavioral norms

- **Read first.** Before any non-trivial change, read your team file
  (`.claude/agents/team-<team>.md`), the relevant section of
  `.claude/PROJECT.md` (technical reference), and the latest 10 lines
  of `.claude/Task.md` Handoffs.
- **Diagnose before fixing.** Reviewer escalations are root-cause
  oriented. Don't paper over symptoms — fix the actual file/threshold/
  prompt the diagnosis points to.
- **Re-run narrowly.** A failure in 50 rows doesn't justify a
  full-pipeline re-run. Re-run only the affected slice. The data is
  expensive; your $20 cap reflects that.
- **Be terse in Handoffs.** One signal per state change. No essays.
- **Commit your code changes** (per logical change, not per session).
  HEREDOC commit message; co-author tag required:
  `Co-Authored-By: Codex CLI <noreply@openai.com>`.
- **When idle, wait.** Don't speculatively read files or run scans.
  DB-MAIN will dispatch you when there's work.

## Project anchors

- `.claude/Goal.md` — vision + non-goals (read once per session)
- `.claude/PROJECT.md` — schemas + vocabularies + tool specs (technical reference)
- `.claude/WORKFLOW.md` — operational Cases + Phase 15 self-heal loop
- `.claude/REPORT.md` — live system state (counts, quality, gotchas)
- `~/.claude/plans/db-fuzzy-lerdorf.md` — full architecture roadmap (Phases 0-15)
- `core/vocab.py` — vocabulary enums (read-only for you)
- `core/config.py` — Phase 15 caps: `CODEX_RETRY_CAP=5`, `CODEX_COST_CAP_USD=20`

## When in doubt

Append `<TEAM>-NEEDS-CLARIFICATION: <one-sentence question>` to Handoffs
and wait. Do not make assumptions about scope, cost, or correctness on
the user's behalf.
