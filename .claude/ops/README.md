# DB Ops Artifacts

This directory holds durable operating state for the Codex Ops + Claude Gate
workflow described in `.claude/DB_OPS.md`.

## Directories

| Directory | Contents |
|---|---|
| `jobs/` | Job cards with scope, owner, inputs, outputs, smoke ladder, abort rules. |
| `runs/` | Long-running process records with command, PID, log, status. |
| `reviews/` | Compact packets for Claude Gate and resulting verdicts. |
| `snapshots/` | cmux screen snapshots taken before clearing or restarting terminals. |

## Helpers

```bash
tools/db_ops_status.sh
tools/db_ops_snapshot_cmux.sh
tools/db_ops_cmux_setup.sh
tools/db_ops_send.sh claude-gate "Review .claude/ops/reviews/<packet>.md"
tools/db_ops_poll.sh claude-gate 80
tools/db_job_card.py <slug> --owner ENRICHER --stage D-1
tools/db_run_record.py <slug> --cmd "<command>" --pid <pid> --log <log>
tools/db_review_packet.py <slug> --stage D-1 --question "<question>"
```

## Rules

- These files are append-friendly and human-readable.
- Do not store secrets, API keys, or full raw logs here.
- Link to log paths instead of pasting large logs.
- Prefer one job/run/review file per stage attempt.
- `.claude/Task.md` still carries terse handoff lines; this directory carries
  the detailed context behind them.
