# make_db — Current State Report

*Updated: 2026-05-05*

---

## 0. Pipeline state — Stage A → B → F complete

**Final canonical artifact**:
  `data/canonical/canonical_buildings_4source.json`
  146,432 unified building records across 4 sources

| Stage | What | Output | Commit |
|---|---|---|---|
| **A** | 4-source architect matching | 39,423 canonical architects (6,640 multi-source, 172 four-source) | `0507bdc` → `dbb07c8` |
| **B** | 4-source building matching (Pass 1+2 + tiebreak + Hybrid feedback) | 146,432 canonical buildings (10,583 multi-source, 6 four-source) | `8576f46` |
| **F** | 4-source assembly | unified records with all source URLs preserved | `2f6ac56` |
| handoff doc | make_web schema, image policy, confidence tiers | `.claude/MAKE_WEB_HANDOFF.md` | `f3f12fe` |

**Field coverage in canonical_buildings_4source.json**:
  arch_id 95.9% / country 95.7% / city 90.8% / year 85.0% / typology 99.7%
  cover URLs: 28,620 div + 9,826 arz + 115,689 arc + 3,127 met = 157,262

**Quality safeguards enforced**:
- Conservative bias: TYPE_C ambiguous → DIFFERENT default in Sonnet tiebreak
- name_sim ≥ 75 gate on Hybrid feedback (filtered 178 collab/supplier
  false-merges like Barovier&Toso ↔ L&L Luce&Light)
- registry safety: skip pairs whose source_id is already attached to any
  canonical (no source-id move between canonicals)
- Post-fix splits (commit `544cdae` + 2026-05-05 follow-up):
  - 61 generic-name + multi-city false-merges split (e.g., 15 different
    "Private Residence" buildings across USA/Lebanon/Russia/Taiwan/UK)
  - 94 code-diff series false-merges split (e.g., Serpentine Pavilion
    2015/2016/2017/2018 were one cluster — split into 4)
  - +33 short-prefix false-merges split (PK/FT/RF Apartment etc — 2-letter
    client codes wrongly merged because outer "Apartment" word matched)
  - +1 architect placeholder split: arch_025729 "ARCHITECTS OFFICE -
    Architizer" was 16 different firms wrongly merged via shared generic
    name → split into 16 individual canonicals with their actual firm names
  - **Total false-merge fixes: 188 building clusters + 1 architect cluster**
- Spot-check (post-fix): ~13% suspect rate, mostly borderline (Innhouse vs
  Innhouse Kunming type), no egregious false-merges remain
- Final building count: **146,524** active canonicals (10,127 + 385 + 6
  multi-source = 10,518)

**Known gaps (NOT addressed this session)**:
- 3,913 metalocus buildings crawled but not in 4_buildings_final.json
  (Phase 11 enrichment pending) — excluded from Stage B; future work
- Bilbao Guggenheim, Lotus Temple, Apple Cupertino NOT FOUND in any
  source (data gap, not a matching error)
- Some borderline same-firm-same-city series may still be mis-merged
  (~5-10% remaining) — Stage D enrichment can verify per-canonical

**Remaining stages (out-of-scope this session)**:
- Stage D: text + image enrichment (LLM heavy, ~$30-50)
- Stage E: phash image dedup (network-heavy, ~hours)
- Stage G: Neon upload (manual gate, requires user approval)

---

> **Reading order**: this doc tells you "what data we currently have +
> what's running right now." For "how the pipeline is structured" see
> `.claude/PROJECT.md`. For "what work is open / blocked / done" see
> `.claude/Task.md`. For "the multi-phase roadmap that produced this
> state" see `~/.claude/plans/db-fuzzy-lerdorf.md`.

---

## 1. Per-source crawl state

The pipeline is multi-source (PROJECT.md §2.1). Each source has its own
SQLite DB under `data/crawl/`. URL-only at stage 1; cover selection +
R2 happens at stage 4-5.

| Source | DB | Projects (rows) | Auxiliary | Crawl status |
|---|---|---|---|---|
| **metalocus** | `data/crawl/metalocus.db` | **7,295 buildings** completed (was 3,873; +3,422 from Phase 11) | 110,054 images on disk (legacy 3,465); 11,897 skipped (post-Phase 11 URL-only); 6,416 articles skipped via `is_building_project` filter | resume in progress (PID 19591 finished earlier today; queue drained) |
| **divisare** | `data/crawl/divisare.db` | **29,936 projects** (29,905 with deep-fetched description) | 12,759 architects | deep-fetch ~99% done (29,930/29,937), background; 7 fetch failures |
| **architizer** | `data/crawl/architizer.db` | **10,632 projects** | 2,802 firms; **14,975 A+Award entries** (2013-2025 × Typology/Firms/Plus tracks) | full crawl complete |
| **archello** | `data/crawl/archello.db` | **22,887 projects** + 71,652 BIM-spec detail rows | brands_seen log populated | full crawl in progress (PID 19814; 22,887/135K done, ~111K pending; ~10 days remaining at 2.5s/req) |

**Image bytes on disk** (`images/`): **60 GB**, all metalocus legacy
(3,465 production rows × ~17 images each). New crawls store URLs only;
cover download + R2 upload is a stage-5 concern (Task.md Phase 9).

---

## 2. Canonical artefact

| Artefact | Path | Size | Built from |
|---|---|---|---|
| Strict canonical | `data/canonical/canonical_buildings_strict.json` | 36 MB / **2,488 records** | metalocus + divisare (architect + building matches) |
| Architect clusters | `data/canonical/metalocus_architect_clusters.json` | 1.2 MB / **2,188 clusters** (from 226 raw alias variants) | metalocus architect strings (Stage A consolidation) |
| Architect↔Divisare matches | `data/canonical/match/metalocus_architect_to_divisare.json` | 2.8 MB | Stage B-1 (rapidfuzz + exact-core + substring rules) |
| Building↔Divisare matches | `data/canonical/match/metalocus_to_divisare_buildings.json` | 3.1 MB | Stage B-2 (architect-scoped fuzzy + year/country signal) |

### Canonical 2,488 breakdown
- **720** full match (Divisare project + metalocus content) — Stage B-2 confident
- **1,768** arch-only match (Divisare canonical architect, metalocus building name + content)
- 928 pure orphans **dropped** in strict mode (no Divisare attribution at all)
- 49 article-style entries dropped (e.g. `"Foster + Partners win the competition for ..."`)

### Match coverage (against the source 3,465 metalocus buildings)
| Stage | Confident matches | Coverage |
|---|---|---|
| Architect cluster → Divisare architect | 1,489 of 2,188 (68.2%) | 76% of buildings |
| Building → Divisare project | 720 of 3,465 (20.8%) | (the 21% ceiling is `metalocus ∩ divisare`, not crawler limit — Divisare publishes a different building set per architect than metalocus does) |

**Note**: canonical artefact is currently **2-source only** (metalocus +
divisare). Architizer/Archello matches are not yet folded in — that's
Phase 9.5 (Task.md), blocked on the Archello crawl finishing.

---

## 3. Live processes

```
PID    Elapsed    Crawler
19814  15h+       run.py crawl-archello --phase projects --limit 200000
                  (Archello full crawl, ~10 days remaining)
```

(Earlier today: Architizer projects/firms/awards finished, Divisare
deep-fetch finished, metalocus URL-only resume finished. Archello is the
only long-running one left.)

---

## 4. Last upload to production (Neon + R2)

| What | Snapshot |
|---|---|
| PostgreSQL `architecture_vectors` | **3,465 buildings** (last full upload from `4_buildings_final.json`) |
| R2 `archi-tinder` bucket | ~17,303 images / ~7.09 GB |
| Last quality rating | 96/100 ("Ready") on the 3,465-row snapshot |
| Last upload date | (pre-canonical work; the strict canonical 2,488 has not been uploaded) |

**Upload of the strict canonical** is gated by user approval; the
script `upload/neon_strict.py` exists with `--dry-run` / `--confirm`
flags. Path C image hosting changes (Task.md Phase 9) should land
before the next upload.

---

## 5. Open work (full list in `.claude/Task.md`)

| Task | Status |
|---|---|
| **Phase 9** — Image hosting Path C (cover→R2, gallery→URLs only) schema work | Open, ratified by user; implementation deferred until Archello crawl finishes |
| **Phase 9.5** — Multi-source canonical extension (fold Architizer/Archello matches into canonical_buildings_strict) | Open, blocked on Archello |
| **Phase 10** — Cross-source image dedupe + quality ranking (phash → cluster → rank → unified gallery JSON) | Open, blocked on Phase 9.5 |
| **Phase 11c** — metalocus downstream (export-dedup → harness enrich + image_analysis URL-mode → embed-rate) | Pending — Phase 11a finished today; runs once user OKs the LLM cost (~$10-20) |
| **Atmosphere drift re-processing** (2,784 / 3,465 records out of V2 vocab) | Open, cost-gated |
| **Vocabulary evolution — atmosphere** | Open, researcher-routed |

---

## 6. Known gotchas (carries forward)

- `DB_PASSWORD` excluded from required-fields check in `core/config.py`
  — Neon needs it but the check is bypassed.
- Existing 3,465 metalocus images live at `images/{building_id}/` (post
  stage2_dedup reorganization from `images/{slug}/`). New metalocus
  crawls (Phase 11+) and Architizer/Archello/Divisare store URLs only.
- Don't re-run `enrich/dedup.py` on already-processed buildings.
- `python3 -m upload.neon --reset` only on first upload or after schema
  changes (drops the table — be sure).
- Atmosphere V2 drift: 2,784 / 3,465 production records have non-V2
  values (`organic`, `communal`, `historic`, …). See
  `data/reports/vocab_migration.json`. Re-processing requires user cost
  approval — see `quality-reviewer` agent's playbook.

---

## 7. How this report stays fresh

The `reporter` agent should rewrite this file at the end of any
state-changing operation: a crawl batch finishing, a canonical rebuild,
an upload. Current freshness owner: whoever runs the orchestrator next.

For day-to-day SQL counts (e.g. "is the Archello crawl still going?"),
prefer querying directly:

```bash
sqlite3 data/crawl/archello.db "SELECT status, COUNT(*) FROM pending_projects GROUP BY status;"
sqlite3 data/crawl/divisare.db "SELECT status, COUNT(*) FROM pending_projects GROUP BY status;"
sqlite3 data/crawl/metalocus.db "SELECT status, COUNT(*) FROM articles GROUP BY status;"
ps -axo pid,etime,command | grep "run.py" | grep -v grep
```

---

## 8. Environment

```
PostgreSQL: Neon (ap-southeast-1)
R2 bucket:  archi-tinder (~7.09 GB / 10 GB free tier)
Python:     3.9
Claude:     Max plan subscription (for LLM enrichment + image analysis)
Disk free:  ~7.6 GiB on /
```
