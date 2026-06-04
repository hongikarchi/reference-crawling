# 20260605 Manual Review Dashboard Goal

- status: completed
- scope: full make_db code/DB audit plus local manual-review dashboard workflow
- operator: Codex
- writes: local code, tests, report JSON/MD, snapshot JSON, decisions JSON, patch report
- no committed external writes: no Neon commit, no R2 write, no crawl, no LLM, no vision
- cost: $0 LLM/API spend; Neon operations were SELECT-only except one rollback dry-run transaction

## Inputs

- C23 production truth:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c23_final.json`
- C23 strict twin:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c23_final.json`
- local SQLite:
  `data/crawl/*.db`, `data/enrich/tasks.db`
- architect artifact:
  `data/canonical/canonical_architects_v2.json`
- Neon archi_data:
  `canonical_v2_buildings`, `canonical_v2_architects`
- C23 sidecars:
  country, year, series, gallery phash, SEO
- existing cover review snapshot/decisions:
  `data/reports/audit_2026-05-27/`

## Outputs

- tool:
  `tools/manual_review_workflow.py`
- tests:
  `tests/test_manual_review_workflow.py`
- dashboard snapshot:
  `data/reports/manual_review_20260605/manual_review_snapshot.json`
- durable decisions:
  `data/reports/manual_review_20260605/review_decisions.json`
- patch/dry-run report:
  `data/reports/manual_review_20260605/manual_review_c24_patch_report.json`
- DB audit:
  `data/reports/manual_review_20260605/db_audit.json`
  `data/reports/manual_review_20260605/db_audit.md`
- code audit:
  `data/reports/manual_review_20260605/code_audit.json`
  `data/reports/manual_review_20260605/code_audit.md`
- verification reports:
  `data/reports/manual_review_20260605/c23_upload_validator_full.json`
  `data/reports/manual_review_20260605/c23_full_reaudit.json`
  `data/reports/manual_review_20260605/c23_full_reaudit.md`
  `data/reports/manual_review_20260605/qc_benchmark.json`
  `data/reports/manual_review_20260605/qc_benchmark.md`
  `data/reports/manual_review_20260605/c23_neon_dry_run_upsert.json`

## Dashboard Queue

- total cases: 10,018
- architect: 28
- country: 40
- cover: 207
- crawl/source gaps: 13
- material/noise: 9,606
- SEO/name: 60
- series/merge/split: 16
- year: 48
- D1/D2: no per-item D1 uncertainty or D2 style/color/program OOV found in C23 audit

## Audit Results

- local SQLite audit: PASS, 26 tables, 1,079,322 rows
- C23 artifact: 39,478 rows, 36,864 publishable, 2,614 nonpublishable
- architects artifact: 14,216 rows, 4,357 recommendable
- Neon read audit: PASS
  - buildings: 39,478 total, 36,864 publishable, 0 missing embeddings
  - architects: 14,216 total, 4,357 recommendable, 0 missing embeddings
  - artifact vs Neon selected critical fields: 0 mismatches
- code audit flagged stale C8/resume10 references in README, docs/dashboard, build_dashboard, and docs/REFERENCE.

## Verification

- `python3 -m pytest tests/test_manual_review_workflow.py`: PASS, 4 tests
- `python3 -m pytest tests/test_manual_review_workflow.py tests/test_canonical_v2_upload_validator.py`: PASS, 10 tests
- `python3 -m pytest`: PASS, 83 tests
- `python3 tools/canonical_v2_upload_validator.py --input ...c23_final.json`: PASS, 39,478 rows
- `python3 tools/canonical_v2_full_reaudit.py --input ...c23_final.json --strict ...c23_final.json`: PASS, hard_total 0
- `python3 tools/canonical_v2_qc_benchmark.py`: PASS 8, WARN 1, FAIL 1, INFO 1
  - FAIL is known material-noise rule: 8,761 Neon rows with non-material pollution
- `python3 tools/canonical_v2_neon_loader.py --dry-run-upsert --input ...c23_final.json`: PASS, 39,478 rows loaded in transaction, rolled back
- dashboard HTTP smoke:
  - `/` served HTML
  - `/api/snapshot` served 10,018-case snapshot
  - `/api/decisions` served empty decision file

## Notes

- The manual-review app never writes Neon/R2.
- The applier rejects incomplete merge/split decisions and records structural actions for explicit follow-up instead of auto-mutating identity structure.
- Current `review_decisions.json` has 0 decisions; `apply-decisions` therefore produced a zero-change patch report and did not write a C24 artifact.
- Local dashboard server command:
  `python3 tools/manual_review_workflow.py serve --snapshot data/reports/manual_review_20260605/manual_review_snapshot.json --decisions data/reports/manual_review_20260605/review_decisions.json --host 127.0.0.1 --port 8765`
