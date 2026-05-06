# make_db — Workflow

How the agent layer operates. Eight operational cases cover ~all real
work. Anything that doesn't fit goes through orchestrator for routing.

> **For data flow** (sources → DBs → JSON → Neon/R2) see PROJECT.md §2.1.
> This file documents *what humans / agents do, in what order*.

## End-to-end workflow (DB-build process)

```mermaid
flowchart TD
    START(["New source decided<br/>OR existing source has new data"])

    subgraph PHASE_RECON["① RECON  &nbsp;(researcher agent)"]
        R1["robots.txt + sample fetch + ToS read"]
        R2["write .claude/research/&lt;source&gt;-schema.md"]
        R3{"ai-train=no?<br/>ToS scrape ban?"}
        R1 --> R2 --> R3
    end

    GATE_USER1{"User policy<br/>decision"}

    subgraph PHASE_CRAWL["② CRAWL  &nbsp;(stage 1)"]
        C1["scaffold crawl/&lt;source&gt;/{db,parsers,crawler}.py<br/>(mirror Architizer / Divisare / Archello)"]
        C2["smoke: --phase sitemap → --phase projects --limit 10"]
        C3["full crawl background (URL only, no image bytes)"]
        C1 --> C2 --> C3
    end

    subgraph PHASE_ENRICH["③ ENRICH  &nbsp;(stages 2-3)"]
        E1["run.py export-dedup<br/>SQLite → 1_buildings_raw.json"]
        E2["run.py harness<br/>text enrich + image_analysis (URL or disk)"]
        E3["run.py embed-rate<br/>SBERT 384-dim + quality review/fix/rate"]
        E1 --> E2 --> E3
    end

    GATE_QUAL{"quality ≥ 97?"}

    subgraph PHASE_CANONICAL["④ CANONICAL  &nbsp;(stage 4)"]
        K1["match_architects.py<br/>cluster → Divisare arch ID"]
        K2["match_buildings.py<br/>per-architect-pool name match"]
        K3["build.py --strict<br/>drop orphans + article-style names"]
        K4["canonical_qc.py<br/>9 invariant checks"]
        K1 --> K2 --> K3 --> K4
    end

    GATE_QC{"all PASS?"}

    subgraph PHASE_DEDUPE["④.5 IMAGE DEDUPE  &nbsp;(Phase 10, optional)"]
        D1["phash fingerprint per image URL"]
        D2["cluster within building (Hamming ≤8)"]
        D3["rank by dimensions / file_size / source"]
        D4["canonical_image_gallery.json"]
        D1 --> D2 --> D3 --> D4
    end

    subgraph PHASE_UPLOAD["⑤ UPLOAD  &nbsp;(stage 5 — manual gate)"]
        U1["upload-guard agent: dry-run + invariant checks"]
        U2["Handoffs: UPLOAD-READY (count=N, quality=X)"]
        U3["USER runs upload/neon_strict.py --confirm<br/>+ R2 cover image upload"]
        U1 --> U2 --> U3
    end

    GATE_USER2{"User OK?"}
    DONE(["Production live<br/>(Neon + R2)"])

    START --> PHASE_RECON
    R3 -- "clean OR<br/>negotiated" --> GATE_USER1
    R3 -- "blocked" --> SKIP1[/"skip source"/]
    GATE_USER1 -- approve --> PHASE_CRAWL
    GATE_USER1 -- defer --> PARK1[/"park / try later"/]
    PHASE_CRAWL --> PHASE_ENRICH
    PHASE_ENRICH --> GATE_QUAL
    GATE_QUAL -- yes --> PHASE_CANONICAL
    GATE_QUAL -- no --> CASE2["→ Case 2 quality iteration<br/>(or Case 3 reprocess)"]
    CASE2 --> PHASE_ENRICH
    PHASE_CANONICAL --> GATE_QC
    GATE_QC -- yes --> PHASE_DEDUPE
    GATE_QC -- no --> CASE2_2["fix + rebuild"]
    CASE2_2 --> PHASE_CANONICAL
    PHASE_DEDUPE --> PHASE_UPLOAD
    PHASE_UPLOAD --> GATE_USER2
    GATE_USER2 -- yes --> DONE
    GATE_USER2 -- no --> PARK2[/"hold;<br/>fix issue"/]
    PARK2 --> PHASE_ENRICH

    classDef gate fill:#fff5e6,stroke:#cc8a00,stroke-width:2px,color:#000
    classDef terminal fill:#e6f5e6,stroke:#2e7d32,stroke-width:2px,color:#000
    classDef phase fill:#f5f5f5,stroke:#666,stroke-width:1px,color:#000
    class GATE_USER1,GATE_USER2,GATE_QUAL,GATE_QC,R3 gate
    class START,DONE terminal
    class PHASE_RECON,PHASE_CRAWL,PHASE_ENRICH,PHASE_CANONICAL,PHASE_DEDUPE,PHASE_UPLOAD phase
```

**Reading the diagram:**
- ① Yellow diamonds = decision gates (require user OR auto-judgment).
- ② Each "phase box" maps to a code subpackage AND a Case (below).
- ③ Quality + canonical-QC failures bounce back to enrich; user-rejected
  upload bounces back to enrich. Linear forward path on the happy case.

The Cases section below describes the agent dispatch *inside* each
phase box (e.g., Case 7 details how recon happens; Case 1 details how a
batch enriches; Case 8 details canonical multi-source fold-in).

## Agents

### Phase 15+ team-routed model (current)

The active model is **4 cmux workspaces** mirroring make_web's MAIN/REVIEW/
BACK/FRONT pattern. Each team has its own workspace, its own running agent,
and its own Codex CLI (or Claude). DB-MAIN dispatches via `tools/dispatch.sh`
which wraps `cmux send`.

| cmux workspace | Inside | Agent file | Owns |
|---|---|---|---|
| **DB-MAIN** | `claude` (Opus) | `orchestrator.md` | Routing + Reviewer self-heal loop |
| **DB-CRAWLER** | `codex` | `team-crawler.md` | Stage 1 — 4 source crawlers |
| **DB-MATCHER** | `codex` | `team-matcher.md` | Stage A + B + E (architect/building/phash) |
| **DB-ENRICHER** | `codex` | `team-enricher.md` | Stage C + D (LLM text + image enrich) |
| **DB-REVIEWER** | `claude` (Opus) | `team-reviewer.md` | Blocking QC gate between every stage |

### Legacy in-session sub-agents (still used)

These run as Agent tool dispatches inside DB-MAIN, not as separate cmux tabs:

| Agent | Model | Role |
|---|---|---|
| `reporter` | sonnet | Updates `.claude/REPORT.md` after state-changing ops. Trims rolling window. |
| `researcher` | opus | Investigates ambiguous decisions (vocab expansion, eval thresholds, new-source recon). WebSearch + WebFetch. Writes to `.claude/research/`. |
| `upload-guard` | sonnet | Pre-upload gate. Runs `upload/*.py --dry-run`. Emits `UPLOAD-READY`; never runs the real upload. |
| `git-manager` | haiku | Commits per logical change. Pushes only on explicit user signal. |
| `quality-reviewer` | sonnet | **Deprecated** (folded into `team-reviewer`). Kept for legacy quality CLI invocations. |
| `batch-worker` | sonnet | **Deprecated** (folded into `team-enricher`). Kept for legacy harness invocations. |

## Terminal Model (Phase 15+)

5 cmux workspaces in one window. DB-MAIN orchestrates by reading
Task.md Handoffs and pushing instructions into the other 4 workspaces:

```
DB-MAIN ── orchestrator ──→ tools/dispatch.sh
                                ├─→ cmux send → DB-CRAWLER (codex)
                                ├─→ cmux send → DB-MATCHER (codex)
                                ├─→ cmux send → DB-ENRICHER (codex)
                                └─→ cmux send → DB-REVIEWER (claude)
                                
DB-MAIN ←── Handoffs ←── all teams append signals to .claude/Task.md
```

Setup (idempotent): `./tools/cmux_setup.sh`

## Cases

### Case 1 — New batch processing
*"Process the new ~3,422 metalocus buildings (or any source) through
enrichment/analysis."*

```
orchestrator
  ├─ batch-worker: `run.py harness --enqueue-only` → verify count
  ├─ batch-worker: `run.py harness` → drain queue
  │   (image_analysis uses URL-mode for new URL-only rows; disk-mode
  │    for legacy 3,465 metalocus rows — handled by the adapter in
  │    enrich/image_analysis.py)
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
  ├─ quality-reviewer: final `quality rate` + `canonical-qc` — both 'Ready'
  ├─ upload-guard:
  │   ├─ verify rating_report.json / review_report.json /
  │   │    canonical_qc.json current
  │   ├─ verify no pending tasks in tasks.db
  │   ├─ verify no quarantined buildings without explanation
  │   ├─ `python3 -m upload.neon --dry-run` (legacy) OR
  │   │  `python3 -m upload.neon_strict --dry-run` (canonical)
  │   │  → inspect counts, row shape
  │   └─ Handoffs: UPLOAD-READY (count=N, quality=X/100)
  └─ USER manually runs `python3 -m upload.neon[_strict] --confirm` after
     reading UPLOAD-READY. The agent layer never runs upload.py. Ever.
```

### Case 7 — Adding a new crawl source
*"Let's add archdaily / dezeen / domus."*

```
orchestrator
  ├─ researcher: write `.claude/research/<source>-schema.md`
  │   per the Phase-0 recon checklist (PROJECT.md §11 step 1)
  │   → Handoffs: RESEARCH-COMPLETE: <source>-schema
  ├─ user policy gate: review the recon — accept ai-train=no posture,
  │   accept ToS posture, decide whether to crawl
  ├─ orchestrator: scaffolds `crawl/<source>/{db,parsers,crawler}.py`
  │   mirroring an existing source (Architizer for sitemap-driven public,
  │   Divisare for authenticated). PROJECT.md §11 steps 3-5 are the
  │   checklist.
  ├─ batch-worker: runs `python3 run.py crawl-<source> --phase sitemap`
  │   then `--phase projects --limit 10` smoke
  ├─ quality-reviewer: spot-check 3-5 random rows for parser correctness
  ├─ orchestrator: launch full crawl in background; commit code
  │   (git-manager: `feat(crawl): Phase N — <source> crawler`)
  └─ when crawl finishes → Case 8 (canonical multi-source extension)
```

### Case 8 — Canonical multi-source rebuild
*"Architizer + Archello are crawled. Fold them into the canonical
artefact alongside metalocus + Divisare."*

```
orchestrator
  ├─ extend canonical/match_architects.py + match_buildings.py to
  │   read the new source's DB
  ├─ run match_architects → match_buildings → build_canonical --strict
  ├─ quality-reviewer: `python3 run.py canonical-qc data/canonical/canonical_buildings_strict.json`
  │   ├─ all PASS → Handoffs: CANONICAL-REBUILT (rows=N)
  │   └─ FAIL on any invariant → orchestrator decides patch vs roll back
  ├─ optional: Phase-10 image dedupe (cross-source phash cluster)
  │   → `data/canonical/canonical_image_gallery.json`
  └─ reporter: refresh REPORT.md §2 (canonical artefact) with new counts
```

## Handoff Signals (Task.md § Handoffs)

Append-only. Each signal is a single line: `<SIGNAL>: <payload>`. Recognized:

### Phase 15 team-routing signals

- `CRAWL-DONE: <source> v<n>` — DB-CRAWLER finished a crawl phase.
- `MATCH-DONE: <stage> v<n>` — DB-MATCHER finished Stage A/B/E (or Stage F build.py).
- `ENRICH-DONE: <scope> v<n>` — DB-ENRICHER finished Stage C/D for the given scope.
- `REVIEWER-PASS: <stage> v<n>` — DB-REVIEWER cleared the artefact.
- `REVIEWER-WARN: <stage> v<n> <reason>` — concern noted; may proceed.
- `REVIEWER-BLOCK: <stage> v<n> cycle <c>/5 — <one-line summary>`
  — full diagnosis at `.claude/escalations/<stage>_<ts>.md`.
- `<TEAM>-ESCALATE: <stage> exhausted self-heal` — cap (5 cycles or $20) hit; manual.
- `THRESHOLD-OVERRIDE-APPROVED: <param>=<value> <reason>` — user signed off on a matcher threshold change.
- `ENRICH-COST-APPROVED: <usd>` — user OK'd a > $5 enrichment task.
- `RE-ENRICH-APPROVED: <scope>` — user OK'd re-running enrichment on already-enriched rows.

### Legacy signals (still emitted by in-session sub-agents)

- `BATCH-DONE: N` — Case 1 completed.
- `REPROCESS-APPROVED: <scope>` — user OK'd a re-processing run.
- `REPROCESS-DONE: <scope>, delta=<score>` — re-processing complete.
- `PROMPT-VERSION-BUMPED: <old> → <new>, delta=<score>` — Case 4 outcome.
- `RESEARCH-REQUESTED: <topic>` — orchestrator wants researcher to investigate.
- `RESEARCH-COMPLETE: <topic>` — researcher done; `.claude/research/<topic>.md`.
- `IMPLEMENTATION-COMPLETE: <source>-crawler` — Case 7 step 6.
- `CANONICAL-REBUILT: rows=<N>` — Case 8 done.
- `UPLOAD-READY: count=<N>, quality=<X>/100` — upload-guard cleared.
- `ESCALATE: <reason>` — manual takeover.

## Phase 15 self-heal loop (between any two stages)

```
[team tab] runs stage X        → appends <TEAM>-DONE: X v<n>
[DB-MAIN] reads Handoffs       → ./tools/dispatch.sh reviewer "Review X v<n>"
[DB-REVIEWER] runs reviewer_gate.py
   ├─ PASS    → REVIEWER-PASS  → DB-MAIN dispatches next stage
   ├─ WARN    → REVIEWER-WARN  → DB-MAIN proceeds with logged concern
   └─ BLOCK   → REVIEWER-BLOCK + .claude/escalations/X_<ts>.md
                → DB-MAIN reads escalation, dispatches responsible team:
                   "Fix per .claude/escalations/<file>; re-run; cycle <c+1>/5"
                → team's Codex fixes root cause
                → re-runs the suspect slice (NOT a full pipeline re-run)
                → appends <TEAM>-DONE: X v<n+1>
                → loop back to "[DB-MAIN] reads Handoffs"

Hard cap per stage attempt: 5 cycles OR cumulative $20 (Codex + Reviewer + re-run)
At cap: <TEAM>-ESCALATE → DB-MAIN writes ESCALATE: <reason> → wait for user.
```

## Git policy (solo-dev, single-branch)

- **`git commit`:** orchestrator commits autonomously per logical change
  or phase — no need to ask. Co-author tag required. Delegated to
  `git-manager` agent.
- **`git push`:** requires explicit user signal ("push" / "푸시해" /
  "올려"). When the user gives that signal, `git-manager` may run plain
  `git push origin main` — never `--force`, never history-rewriting
  push. Without the signal, local commits accumulate.
- **Branches:** single `main` branch. No feature branches.

## Non-goals for this layer

- No automated `upload/neon{,_strict}.py` runs. Upload is always the
  user's command after reading `UPLOAD-READY`.
- No automated schema edits to `core/vocab.py` without explicit approval
  (vocabulary migrations are a user decision; researcher proposes,
  orchestrator only edits after user confirmation).
- No parallel agents chasing the same batch. If two terminals are open,
  coordinate via Handoffs — last write wins on Task.md sections that
  aren't append-only (Open/In Progress/Resolved).
- No shadow state. Everything an agent "knows" lives in Goal.md /
  Task.md / REPORT.md / the actual data files. No in-memory assumptions
  between invocations.
