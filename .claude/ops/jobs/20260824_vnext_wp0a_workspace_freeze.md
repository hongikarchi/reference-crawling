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

## Appendix A — ACL unreadable 경로와 정리 게이트 (F6)

게이트 리뷰가 기록한 `git status` unreadable directory 경고는 **259건**이다.
현재 운영자 토큰에서는 같은 stderr가 재출력되지 않아, 동결 manifest의 untracked 경로와
현 filesystem에서 부모가 읽히다가 최초로 ACL이 끊기는 경계를 재계산했다. 결과는
리뷰 카운트와 정확히 일치했으며 목록은 다음과 같다.

- `.pytest_cache`
- `.pytest_tmp_v23_binding`
- `.pytest_tmp_v23_binding3`
- `.test-tmp-v231-audit`
- `.test-tmp-v231-audit-final2`
- `.test-tmp-v231-independent-final`
- `.test-tmp-v23-v231-independent-final2`
- `.test-tmp-v26-writer`
- `.test-tmp-v26-writer2`
- `.test-tmp-v26-writer3`
- `.test-tmp-v272-run`
- `.test-tmp-v272-run2`
- `.tmp/pytest_v24_cli_current_audit`
- `.tmp/pytest_v24_hardening_current_audit`
- `.tmp/pytest_v24_release_audit_frozen`
- `.tmp/pytest_v241_release_audit_final2`
- `.tmp/pytest_v25_prehttp`
- `.tmp/pytest_v251_fast3`
- `.tmp/pytest_v251_nolock`
- `.tmp/pytest_v251_offline`
- `.tmp/pytest_v251_offline2`
- `.tmp/pytest_v251_offline3`
- `.tmp/pytest_v251_offline4`
- `.tmp/pytest_v251_offline5`
- `.tmp/pytest_v251_offline6`
- `.tmp/pytest_v251_offline7`
- `.tmp/pytest_v251_pub`
- `.tmp/pytest-av23-root`
- `.tmp/pytest-v161-regression`
- `.tmp/pytest-v161-runtime`
- `.tmp/pytest-v161-runtime-final`
- `.tmp/pytest-v162-qa`
- `.tmp/pytest-v162-qa-rerun2`
- `.tmp/pytest-v18-preflight`
- `.tmp/pytest-v18-v19-combined`
- `.tmp/pytest-v19-all`
- `.tmp/pytest-v19-pinned`
- `.tmp/pytest-v19-postrun-qa`
- `.tmp/pytest-v19-prepin`
- `.tmp/pytest-v19-review`
- `.tmp/pytest-v20-v21-runtime-pinned-20260813a`
- `.tmp/pytest-v20-v21-runtime-regression-20260813a`
- `.tmp/pytest-v21-qa-postrun-20260813a`
- `.tmp/pytest-v21-runtime-pinned-20260813a`
- `.tmp/pytest-v21-runtime-prepin-20260813a`
- `.tmp/pytest-v21-runtime-prompt-20260813a`
- `.tmp/pytest-v221-20260813-a`
- `.tmp/pytest-v221-20260813-b`
- `.tmp/pytest-v221-regression-20260813`
- `.tmp/pytest-v23-root-finalpin`
- `.tmp/pytest-v23-root-preflight`
- `.tmp/pytest-v24-a241-integration-20260814-01`
- `.tmp/pytest-v24-a241-integration-20260814-02`
- `.tmp/pytest-v24-cli-hardening-20260814-01`
- `.tmp/pytest-v24-runtime-20260814-01`
- `.tmp/pytest-v24-runtime-20260814-02`
- `.tmp/pytest-v24-runtime-final-20260814-01`
- `.tmp/pytest-v24-runtime-final-20260814-02`
- `.tmp/pytest-v25-ca96de8471f747ddb2406b8c0bfc86cf`
- `.tmp/pytest-v25-cli-e9362f0239714e3cb16c74951210e6b3`
- `.tmp/pytest-v25-lock-199f0434e5b847d5bab3ad58c41aad08`
- `.tmp/pytest-v25-review-f3c6c4f42883493986654bdf51b80104`
- `.tmp/pytest-v25-runtime-0ddd4dbe791e48bfb58c67b8945f8657`
- `.tmp/pytest-v25-side-7aa634ca738d4d14a9ee5c10dea60854`
- `.tmp/pytest-v26-final-release-001`
- `.tmp/pytest-v26-release-001`
- `.tmp/pytest-v26-review`
- `.tmp/pytest-v26-review-r2`
- `.tmp/pytest-v26-sidecar-001`
- `.tmp/pytest-v26-sidecar-002`
- `.tmp/pytest-v26-sidecar-003`
- `.tmp/pytest-v26-sidecar-004`
- `.tmp/v161-all-1786602296943`
- `.tmp/v161-qa-1786602316324`
- `.tmp/v161-regression-1786602451095`
- `.tmp/v161-runtime-1786602227153`
- `.tmp/v161-sidecar-1786602154838`
- `.tmp/v162-focused-1786602865669`
- `.tmp/v162-qa-final-1786603084034`
- `.tmp/v162-regression-1786602955515`
- `.tmp_pytest_availability_v16_v23`
- `.tmp_pytest_v17_all`
- `.tmp_pytest_v17_final`
- `.tmp_pytest_v17_final2`
- `.tmp_pytest_v17_release`
- `.tmp_pytest_v17_release2`
- `.tmp_pytest_v17_retry`
- `.tmp_pytest_v17_v18_pins`
- `.tmp_pytest_v18_pins`
- `.tmp_pytest_v18_postrun`
- `.tmp_pytest_v21_prompt_a`
- `.tmp_pytest_v21_v22_release`
- `.tmp_pytest_v21_v22_runtime`
- `.tmp_pytest_v22_redteam`
- `.tmp_pytest_v22_release`
- `.tmp_pytest_v22_runtime`
- `.tmp_pytest_v22_runtime2`
- `.tmp_pytest_v22_runtime3`
- `.tmp_pytest_v221_audit_20260813`
- `.tmp_pytest_v221_review`
- `.tmp_pytest_v221_review_2`
- `.tmp_pytest_v23_binding_reaudit`
- `.tmp_pytest_v23_final`
- `.tmp_pytest_v23_frozen`
- `.tmp_pytest_v23_regression`
- `.tmp_pytest_v23_release_audit`
- `.tmp_pytest_v23_release_audit_final`
- `.tmp_pytest_v23_runtime`
- `.tmp_pytest_v23_runtime2`
- `.tmp_pytest_v23_runtime3`
- `.tmp_pytest_v23_runtime5`
- `.tmp_pytest_v23_runtime6`
- `.tmp_pytest_v23_runtime7`
- `.tmp_pytest_v23_terminal`
- `.tmp_pytest_v23_terminal2`
- `.tmp_pytest_v231`
- `.tmp_pytest_v231_blind_full`
- `.tmp_pytest_v231_regression`
- `.tmp_pytest_v231_regression2`
- `.tmp_pytest_v231_review`
- `.tmp_pytest_v231_review2`
- `.tmp_pytest_v231b`
- `.tmp_pytest_v231c`
- `.tmp_pytest_v231d`
- `.tmp_pytest_v231final`
- `.tmp_pytest_v231final2`
- `.tmp_pytest_v231final3`
- `.tmp_pytest_v231fix`
- `.tmp_pytest_v231hard`
- `.tmp_pytest_v231hard2`
- `.tmp_pytest_v231paths`
- `.tmp_pytest_v231retry`
- `.tmp_pytest_v24_av_offline`
- `.tmp_pytest_v24_av_offline2`
- `.tmp_pytest_v24_av_risk4`
- `.tmp_pytest_v24_release_audit_agent`
- `.tmp_pytest_v24_runtime2`
- `.tmp_pytest_v24_runtime3`
- `.tmp_pytest_v24_runtime4`
- `.tmp_pytest_v24_runtime5`
- `.tmp_pytest_v24_runtime6`
- `.tmp_pytest_v24_v241_auditgo`
- `.tmp_pytest_v24_v241_final`
- `.tmp_pytest_v24_v241_pinned`
- `.tmp_pytest_v241`
- `.tmp_pytest_v241_release_audit_draft`
- `.tmp_pytest_v241_release_audit_finalcode`
- `.tmp_pytest_v241_release_audit_ready`
- `.tmp_pytest_v25_cli2`
- `.tmp_pytest_v25_review2`
- `.tmp_pytest_v25_review3`
- `.tmp_pytest_v25_review4`
- `.tmp_pytest_v25_runtime_full1`
- `.tmp_pytest_v25_side3`
- `.tmp_pytest_v25_side4`
- `.tmp_pytest_v25_side5`
- `.tmp_pytest_v25_side6`
- `.tmp_pytest_v251_atomic_integration`
- `.tmp_pytest_v251_atomic_target`
- `.tmp_pytest_v251_atomic_target2`
- `.tmp_pytest_v251_atomic_target3`
- `.tmp_pytest_v251_atomic_target4`
- `.tmp_pytest_v251_atomic_target5`
- `.tmp_pytest_v251_final_batch_target1`
- `.tmp_pytest_v251_final_batch_target2`
- `.tmp_pytest_v251_final_batch_validator_target1`
- `.tmp_pytest_v251_final_batch_validator_target2`
- `.tmp_pytest_v251_final_producer_full`
- `.tmp_pytest_v251_final_target3`
- `.tmp_pytest_v251_final_target4`
- `.tmp_pytest_v251_final_validator_full`
- `.tmp_pytest_v251_freeze_producer`
- `.tmp_pytest_v251_freeze_validator`
- `.tmp_pytest_v251_handle_target2`
- `.tmp_pytest_v251_handle_target3`
- `.tmp_pytest_v251_handle_target4`
- `.tmp_pytest_v251_handle_target5`
- `.tmp_pytest_v251_ntlink_full`
- `.tmp_pytest_v251_ntlink_integration`
- `.tmp_pytest_v251_ntlink_target`
- `.tmp_pytest_v251_ntlink_target2`
- `.tmp_pytest_v251_ntlink_valfocus`
- `.tmp_pytest_v251_patch_producer_full`
- `.tmp_pytest_v251_patch_producer_full2`
- `.tmp_pytest_v251_patch_target1`
- `.tmp_pytest_v251_patch_target2`
- `.tmp_pytest_v251_patch_target3`
- `.tmp_pytest_v251_patch_target4`
- `.tmp_pytest_v251_patch_target6`
- `.tmp_pytest_v251_patch_validator_full`
- `.tmp_pytest_v251_patch_validator_smoke`
- `.tmp_pytest_v251_patch_validator_target2`
- `.tmp_pytest_v251_prod_allwriter`
- `.tmp_pytest_v251_prod_final`
- `.tmp_pytest_v251_prod_final2`
- `.tmp_pytest_v251_prod_final3`
- `.tmp_pytest_v251_redteam`
- `.tmp_pytest_v251_redteam2`
- `.tmp_pytest_v251_redteam3`
- `.tmp_pytest_v251_rollback_pair`
- `.tmp_pytest_v251_rollback_race`
- `.tmp_pytest_v251_staging_race`
- `.tmp_pytest_v251_staging_race2`
- `.tmp_pytest_v251_staging_target`
- `.tmp_pytest_v251_val_final`
- `.tmp_pytest_v251_val_final2`
- `.tmp_pytest_v251_val_final3`
- `.tmp_pytest_v251_val_final4`
- `.tmp_pytest_v26_full_root_20260820_2147`
- `.tmp_pytest_v26_review_root_20260820_2145`
- `.tmp_pytest_v26_sidecar_root_20260820_2131`
- `.tmp_pytest_v26_sidecar_root_20260820_2133`
- `.tmp_pytest_v26_sidecar_root_20260820_2136`
- `.tmp_pytest_v26_terminal_20260818`
- `.tmp_pytest_v26_terminal_pins_root_20260820`
- `.tmp_pytest_v26_validator_20260818`
- `.tmp_pytest_v26_validator_repair_cli`
- `.tmp_pytest_v26_writer_root_20260820_2110`
- `.tmp_pytest_v26_writer_root_20260820_2115`
- `.tmp_pytest_v26_writer_root_20260820_2120`
- `.tmp_pytest_v26_writer_root_20260820_2125`
- `.tmp_pytest_v27_availability`
- `.tmp_pytest_v27_availability_fix`
- `.tmp_pytest_v27_renderer_lineage_fix_20260821`
- `.tmp_pytest_v27_structural_agent2`
- `.tmp_pytest_v27_terminal_pins_20260821`
- `.tmp_pytest_v27_terminal_pins_20260821b`
- `.tmp_pytest_v271_report_fix_20260821`
- `.tmp_pytest_v272_release_20260821`
- `.tmp_pytest_v272_release_20260821b`
- `.tmp_pytest_v272_release_20260821c`
- `.tmp_pytest_v272_release_20260821d`
- `.tmp_pytest_v272_release_20260821e`
- `.tmp_pytest_v272_release_20260821f`
- `.tmp_v26_review_repair`
- `.tmp_v26_review_repair_20260820`
- `.tmp_v26_review_repair_20260820_b`
- `.tmp-pytest-v271-gate`
- `.tmp-pytest-v271-live`
- `.tmp-pytest-v271-success`
- `.tmp-pytest-v271-terminal-pins`
- `.tmp-pytest-v271-terminal-pins2`
- `.tmp-pytest-v271-terminal-pins3`
- `.tmp-pytest-v271-terminal-pins4`
- `.tmp-v251/pytest`
- `.tmp-v251/pytest10`
- `.tmp-v251/pytest2`
- `.tmp-v251/pytest3`
- `.tmp-v251/pytest4`
- `.tmp-v251/pytest5`
- `.tmp-v251/pytest6`
- `.tmp-v251/pytest7`
- `.tmp-v251/pytest8`
- `.tmp-v251/pytest9`
- `tmp/v24runtime-all-postrun-01`
- `tmp/v24runtime-cli-postrun-03`
- `tmp/v24runtime-focused-postrun-01`
- `tmp/v24runtime-review-postrun-01`
- `tmpg2bkxscj`

WP0a 테스트가 추가로 만든 아래 2개 ACL 잔여물도 사용자 게이트 정리 목록에 등록한다.

- `data/reports/vnext_pytest_wp0a_tmp_20260824_1`
- `data/reports/vnext_pytest_wp0a_tmp_20260824_2`

이 부록은 provenance 보강일 뿐 manifest를 재빌드하지 않는다. 위 경로의 ACL 변경·삭제와
기존 tmp/draft/WAL 정리는 모두 사용자 명시 승인 전까지 수행하지 않는다.
