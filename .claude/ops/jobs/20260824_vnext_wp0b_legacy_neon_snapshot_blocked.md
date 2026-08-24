# vNext WP0b — legacy Neon snapshot (blocked before connection)

- Date: 2026-08-24 KST
- Result: **BLOCKED — repo-root `.env` absent**
- LLM/model cost: $0; model calls 0
- Neon connections: 0; SQL writes: 0; Neon/R2 mutations: 0
- Snapshot/partial output: both absent

## Original intended scope (F4로 실행 전 superseded)

Read `canonical_bld_id, source_refs` from all 39,478 rows of
`canonical_v2_buildings`, ordered by ID in a repeatable-read/read-only transaction,
then roll back. The immutable target is
`data/canonical/legacy_neon_snapshot_2026q3.jsonl`.

## Implemented

- `tools/export_vnext_legacy_neon_snapshot.py`
  - SHA-256 `afe03ef287fd3521406f1d89b086b2adaf6ad9c59aa361cd1fcc13b489f516a9`
  - no-clobber partial → complete promotion, exact 39,478 row/PK gate,
    canonical JSONL, server cursor, read-only transaction, explicit rollback
- `tools/validate_vnext_legacy_neon_snapshot.py`
  - SHA-256 `45baf7db543dbd6e3438b41995acc4057b2e60640814c80917618b0abd372261`
  - independent UTF-8/canonical encoding, strict ID order/uniqueness,
    exact row count, shape and file SHA validation
- `tests/test_vnext_legacy_neon_snapshot.py`
  - SHA-256 `611d1a6f9757a367f7eb3f1e675f03be5692a810e9af86c42fab3c84317ffa9c`
  - result: **4 passed**

`requirements.txt` already declares `psycopg2-binary>=2.9.11`; the incomplete
Windows `.venv` was repaired with `psycopg2-binary 2.9.12`. The first sandboxed
package download received WinError 10013; the approved external retry succeeded.

## Blocker evidence

- loader result: `missing DB env keys: DB_HOST, DB_PORT, DB_USER, DB_NAME`
- no `DATABASE_URL`, `DB_URL`, or discrete `DB_*` keys exist in the process
- no `.env*` file exists at the repo root, user home root, or immediate home child repo
- `docs/CROSS_PC_HANDOFF.md` explicitly excludes all `.env` files from the transfer
- `docs/MIGRATION_WINDOWS.md` says the user must separately place `.env`
  (Neon writer/R2/Divisare) at repo root

Old migration-bundle secrets were not searched, copied, or printed. Since connection
construction failed locally, no Neon network request or SQL transaction began.

## Resume condition

Place the current make_db `.env` at the repository root with `DATABASE_URL`/`DB_URL`
or the existing loader's `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_NAME`, `DB_PASSWORD`
keys. Then run exporter → independent validator. A successful resume must append a
new PASS job card with row count, DB read-only proof, JSONL SHA, and validator result.

WP1 must not start until this artifact exists and validates.

## F4 pre-execution amendment

게이트 리뷰 F4에 따라 위 2컬럼 구현은 **실행 전에 폐기**했다. 현재 exporter/validator는
live `canonical_v2_buildings`의 알려진 45컬럼 exact-set을 먼저 확인하고 `embedding`만
제외한 44컬럼 전부를 저장한다. `.env.make-web` SELECT-only 연결을 우선하며, immutable
`legacy_neon_snapshot_2026q3.report.json`과 독립
`legacy_neon_snapshot_2026q3.validation.json`을 아티팩트 옆에 남긴다.

이 amendment 시점에도 `.env`/`.env.make-web`, snapshot, `.partial`, report는 모두 없고
Neon connection/SQL write는 0이다. 상세 SHA와 검증은
`20260824_vnext_wp0_gate_fixes_f1_f13.md`를 따른다.
