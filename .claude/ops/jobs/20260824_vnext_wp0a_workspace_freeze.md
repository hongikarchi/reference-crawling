# vNext WP0a — workspace freeze

- Date: 2026-08-24 KST
- Result: **PASS**
- Cost: $0; LLM/API calls 0
- Writes: new vNext files only; frozen inputs/Neon/R2 unchanged; deletions 0

## Scope

`docs/VNEXT_REBUILD_PLAN.md` WP0a에 따라 Divisare+Architizer의 accepted 입력 6개를
읽기 전용으로 해시하고, Git untracked 상태와 `data/enrichment` 최상위 항목을
인벤토리했다. rejected/superseded draft, semantic 실험, partial, lock, WAL/SHM,
tmp/ACL 경로는 목록·크기만 기록했으며 내용 해시·정리·삭제하지 않았다.

## Inputs

- Git HEAD: `c3513dfbd0e8b88ef407a85b2defc2e47ff8c138` (`main`)
- Frozen bytes: 38,414,090,240
- Frozen SHA-256: `data/reports/vnext_workspace_freeze_v1.json`의
  `frozen_inputs` 6행에 기록
- 계획 작성 시 untracked 374와 달리 실행 시점 `git status -u normal`은 553행,
  `-u all`의 untracked path는 188,919개였다. 현 상태를 그대로 동결했다.

## Outputs

- `data/reports/vnext_workspace_freeze_v1.json`
  - logical SHA-256: `65ec4d1a93854b6d365e4d0ad81fffe0b380484a36260b95eae5b49ffaec711e`
  - file SHA-256: `cd787a0088161fd0e8dddfe23e1943c0a41992f1e1a214b67fcce5d3ae17dcc3`
- `tools/build_vnext_workspace_freeze.py`
- `tools/validate_vnext_workspace_freeze.py`
- `tests/test_vnext_workspace_freeze.py`

## Inventory summary

- `data/enrichment`: 204 entries (153 files, 51 directories)
- accepted frozen inputs: 4 entries / 27,421,351,936 bytes
- rejected or superseded drafts: 10 / 42,449,326,080 bytes
- partial/runtime residue: 65 / 31,883,578,343 bytes
- semantic experimental evidence: 85 / 59,297,792 bytes
- other inventory-only: 40 / 11,931,714,938 bytes

## Verification

- `pytest tests/test_vnext_workspace_freeze.py`: **7 passed**
- Creation-time strict validator: **PASS**, 985 checks, 0 failures
- Post-commit durable validator: **PASS**, 38 checks, 0 failures. Frozen HEAD and
  workspace inventory are point-in-time provenance; optional
  `--require-live-head` / `--require-live-workspace` detect later checkout drift.
- All six accepted files matched the expected byte size and SHA-256 and their
  SQLite schema metadata was independently re-read.

## Notes

- 기본 Windows pytest temp 경로는 기존 ACL 오류로 접근하지 못했다. 테스트 전용
  `data/reports/vnext_pytest_wp0a_tmp_20260824_*` 경로를 사용했다.
- ACL 깨진 tmp와 draft WAL 정리는 계획의 명시적 보류 항목이므로 수행하지 않았다.
