# make_db

Agent-driven pipeline: crawl metalocus.es → enrich + analyze → review → upload to Neon + R2.

## How to start a session

**Current default: DB Ops Mode.** make_db now runs with Codex as the
operational control plane and Claude as a checkpoint reviewer. Read
`.claude/DB_OPS.md` first. Claude should not resume the old always-on
DB-MAIN dispatcher pattern unless the user explicitly asks for legacy
5-team mode.

Default terminal layout:
- `DB-CODEX-OPS` — Codex operational main: status, job cards, smoke,
  code, validators, commits.
- `DB-RUNNER` — shell only: long-running Python processes.
- `DB-MONITOR` — shell only: logs, counts, progress.
- `DB-CLAUDE-GATE` — Claude Code: semantic/architecture checkpoint review
  from compact packets under `.claude/ops/reviews/`.
- `DB-CODEX-WORKER` — optional Codex worker for bounded side tasks.

Setup/status helpers:
```bash
tools/db_ops_status.sh
tools/db_ops_snapshot_cmux.sh
tools/db_ops_cmux_setup.sh
tools/db_ops_send.sh claude-gate "Review .claude/ops/reviews/<packet>.md"
tools/db_ops_poll.sh claude-gate 80
```

**Legacy Phase 15+ multi-team mode:** make_db can still run across **5 cmux
workspaces** in one window — DB-MAIN, DB-CRAWLER, DB-MATCHER, DB-ENRICHER,
DB-REVIEWER. This mode is for expanded team operations, not the default.
In that mode DB-MAIN dispatches via `./tools/dispatch.sh <team> "<msg>"`
which wraps `cmux send`.

**Codex-first principle.** The user pays per-token for both Anthropic and
OpenAI; **anything that costs LLM tokens runs on Codex by default.** Claude
keeps only routing (DB-MAIN) and semantic spot-checks (DB-REVIEWER). Code
writing, static review, and long-running scripts go to a codex tab. See
`AGENTS.md` § "Codex-first principle" for the routing table.

To set up the 5-workspace layout (idempotent):
```bash
./tools/cmux_setup.sh
```

Each non-MAIN workspace runs either `codex` (CRAWLER/MATCHER/ENRICHER —
writes/fixes pipeline code) or `claude` (REVIEWER — blocking QC gate).
See `.claude/agents/team-*.md` for per-team responsibilities and
`.claude/WORKFLOW.md` § "Phase 15 self-heal loop" for the full cycle.

For one-off scripted work (e.g., "just check status"), invoke the relevant
script directly via the CLI below — multi-team layer is for multi-step
quality-gated operations.

## Documents

- `.claude/Goal.md` — vision + quality targets + non-goals (read first by every agent)
- `.claude/Task.md` — open / in-progress / resolved + handoff signals
- `.claude/WORKFLOW.md` — operational Cases + handoff signal vocabulary + Phase 15 self-heal loop
- `.claude/REPORT.md` — live system state (counts, quality, known gotchas)
- `.claude/PROJECT.md` — schemas + vocabularies + tool specs (technical reference)
- `.claude/DB_OPS.md` — current Codex Ops + Claude Gate operating model
- `.claude/ops/` — job cards, run records, review packets, cmux snapshots
- `.claude/agents/*.md` — agent definitions:
  - **team layer (Phase 15+, in own cmux workspace):** orchestrator, team-crawler, team-matcher, team-enricher, team-reviewer
  - **in-session sub-agents:** reporter, researcher, upload-guard, git-manager
- `.claude/escalations/` — Reviewer BLOCK diagnoses (gitignored)
- `~/.claude/plans/db-fuzzy-lerdorf.md` — full architecture roadmap (Phases 0-15)

## Rules

- Read `.claude/Goal.md` before non-trivial decisions.
- `.claude/PROJECT.md` is the single source of truth for schemas;
  `core/vocab.py` is the single source of truth for vocabularies.
  `.claude/PROJECT.md` §5 mirrors `core/vocab.py` for human reading —
  code wins on conflict.
- **Code lives in 5-stage subpackages**, mirroring the workflow:
  `core/` (vocab, config, utils) → `crawl/<source>/` (per-site crawlers,
  e.g. `crawl/metalocus/`, `crawl/divisare/`) → `enrich/` (LLM text +
  image enrichment, harness, embed, quality) → `canonical/` (matching,
  consolidation, build, QC) → `upload/` (Neon + R2). `run.py` stays at
  root as the CLI dispatcher; `tools/` holds dev utilities (gallery
  preview, etc.). New crawl source = new directory under `crawl/`.
- Data files (`data/`, `images/`) stay at project root regardless of
  the code subpackage that produced them.
- **`data/id_registry.json` — NEVER delete.** Stable building_id
  assignments live here; losing it breaks every downstream join.
- **Never run upload scripts without explicit user approval.**
  `upload-guard` agent gates them; the user runs them. The current upload
  scripts are `upload/neon.py` (legacy) and `upload/neon_strict.py`
  (strict canonical → in-place migration).
- Agents NEVER edit `vocab.py` on their own judgment — vocabulary changes are
  user decisions, grounded by the `researcher` agent.
- **Git workflow (solo dev, single branch):**
  - All work happens on `main`. No feature branches.
  - The `orchestrator` agent commits autonomously per logical change or phase
    (no need to ask the user "should I commit now?").
  - **`git push` requires explicit user request.** Agents may run `git push`
    only when the user explicitly says so ("push", "푸시해", "올려", etc.).
    Without that signal, local commits stack and the agent never reaches for
    the remote on its own. `git push --force` and any history-rewriting push
    is never allowed; pushes are always plain `git push origin main`.
- **Plan mode workflow (사용자 명시 룰 — 모든 세션에서 반드시 지킬 것):**
  - **한국어로 짧고 구조화된 설명** — 표 / 불릿 / 짧은 문단으로 핵심만.
    영어 긴 줄로 한 번에 plan 던지지 않는다.
  - **결정사항은 주제별로 하나씩 차근차근** — 한 번에 여러 결정 묶지 X.
    각 결정마다 배경 짧게 설명 + `AskUserQuestion`으로 객관식 제시 + 사용자 응답
    받은 후 다음 결정으로.
  - **모든 결정 컨펌 후에야 정식 plan 파일 작성** — 결정 진행 중에는 plan
    파일에 짧은 한국어 임시 메모만. 사용자 컨펌 끝난 후에 정식 plan 작성.
  - **plan 파일도 짧게** — 200줄 영어 monster 금지. 작업 단위별 핵심만.
  - 영어는 plan 파일 본문 / 코드 주석 / 커밋 메시지에서는 OK; 사용자와의
    plan-mode 대화는 한국어.

## CLI

All operations route through `run.py`. Subcommands in 5 groups:

```bash
# Build / extend the dataset (metalocus side)
python3 run.py make-db [--limit N]   # crawl + export + dedup
python3 run.py crawl --articles 500  # crawl only (resume image downloads)
python3 run.py export-dedup          # SQLite → 1_buildings_raw.json + dedup
python3 run.py harness               # enrich + analyze + QC (Anthropic API)
python3 run.py embed                 # final embeddings → 4_buildings_final.json
python3 run.py embed-rate            # embed + quality review/fix/rate

# Canonical (Divisare-first rebuild)
python3 run.py crawl-divisare        # authenticated Divisare crawler
python3 run.py consolidate-architects [--dry-run] [--no-llm]
                                     # collapse metalocus architect aliases → clusters
python3 run.py match-canonical       # metalocus building → Divisare project mapping
python3 run.py canonical-qc [PATH]   # 9 invariant checks on canonical_buildings.json

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
python3 -m upload.neon --dry-run             # legacy 4_buildings_final → architecture_vectors
python3 -m upload.neon                       # only after explicit approval
python3 -m upload.neon_strict --dry-run      # strict canonical → architecture_vectors
python3 -m upload.neon_strict --confirm      # only after explicit approval
```

## Reference

- **File structure + data layout** — see `.claude/PROJECT.md` §3
- **Pipeline tool specs (stage1-3, quality, agents, upload)** — see `.claude/PROJECT.md` §7
- **Schema (PostgreSQL `architecture_vectors`)** — see `.claude/PROJECT.md` §4
- **Controlled vocabularies** — `core/vocab.py` is canonical; `.claude/PROJECT.md` §5 mirrors

## Known Gotchas

- `DB_PASSWORD` excluded from required-fields check — Neon needs it but check is bypassed.
- Images live at `images/{building_id}/` after dedup (reorganized from `images/{slug}/`).
- Don't re-run `enrich/dedup.py` on already-processed buildings.
- `python3 -m upload.neon --reset` only on first upload or after schema changes (drops the table — be sure).
- 2,784 / 3,465 production records have out-of-V2 atmosphere values (`organic`, `communal`, …). See `data/reports/vocab_migration.json`. Re-processing requires user cost approval — see `quality-reviewer` agent's atmosphere-drift playbook.
