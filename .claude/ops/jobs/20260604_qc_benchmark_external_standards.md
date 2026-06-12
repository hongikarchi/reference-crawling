# 2026-06-04 — External-standards QC benchmark (A→B deep-research)

## Scope

User asked for a deep-research-driven verification that the DB is sound,
make_web-usable, and structurally correct as architecture data. Two-track plan
agreed (A→B):

- **A — deep research** (`wd162d4i9`, 111 agents, 28 primary sources, 24/25
  claims confirmed): build an external benchmark + QC checklist from citable
  standards, not internal validators.
- **B — apply it**: run the checklist as read-only SQL against live Neon and
  score pass/fail.

## Inputs / outputs

- Benchmark spec → `docs/DATA_QUALITY_BENCHMARK.md` (CDWA, Getty AAT/ULAN,
  Wikidata Q811979, VRA Core, MDPI/DFRWS dedup papers, SIGIR 2020 cold-start).
- Audit runner → `tools/canonical_v2_qc_benchmark.py` (READ-ONLY, SELECT only).
- Scorecard → `data/reports/qc_benchmark_2026-06-04/scorecard.{json,md}`.

## Result — 8 PASS / 1 WARN / 1 FAIL / 1 INFO (36,864 publishable rows)

| Rule | Status | Note |
|---|---|---|
| R1 presence | PASS | name/arch/program/country/image 100%, year 99.67% |
| R2 architect validity | WARN | 0 dangling refs; **2 rows architect='N/A'** (bld_008953 LJM House, bld_023187 WWI cemetery monument) |
| R3 year sanity | PASS | 0 bad (the 5 "2050/2045" are correctly year_kind='future') |
| R4 style vocab | PASS | 0 OOV vs 12-term STYLE |
| R5 material noise | **FAIL** | **8,761 rows carry MATERIAL_TAXONOMY_NOISE** — fix already built (`strip_material_noise_neon.py`), pending user approval |
| R6 cover dedup | PASS | 0 shared cover URLs |
| R8 cover selection | PASS | 100% have a cover |
| R7 hash method | INFO | pHash pipeline, no ColourHash/WaveHash |
| R9 cross-source dup | PASS | 1.74% name+country collisions, mostly legit (generic "Casa M", Krumbach 7-shelter project) |
| R10 encoding | PASS | 0 mojibake |
| R11 cold-start | PASS | 4,357 recommendable, 100% have derivable portfolio embedding |

## Verdict

DB passes external cultural-heritage + recommendation standards. The single
genuine FAIL (R5 material noise) already has a built, dry-run-verified fix
awaiting user approval. R2 WARN = 2 trivially-fixable 'N/A' architect rows.

## Open / next

- R5: run `strip_material_noise_neon.py --apply --confirm-db-write` (user-gated)
  — same migration in job card `20260527_material_noise_strip.md`.
- R2: decide 2 'N/A'-architect rows (leave / unpublish / backfill).
- Calibration TODOs in DATA_QUALITY_BENCHMARK.md (dedup thresholds on own
  imagery; vocab↔AAT crosswalk).
