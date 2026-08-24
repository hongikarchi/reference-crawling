# vNext WP0 gate fixes F1–F13

- Date: 2026-08-24 KST
- Result: **PASS — F1 blocker, F2–F6 major, F7–F13 minor 반영 완료**
- Input review: `20260824_vnext_wp0_gate_review.md` at Git `c409289`
- Authoritative plan SHA-256:
  `751a81288ff13cd659b6834752007b65601770fc8f333395fdd1e8ff5e77c9fc`
- Pipeline model/API/network calls: 0; DB connections: 0; SQL writes: 0
- Neon/R2/schema/data mutation and deletion: 0

## 판정

| Finding | 반영 결과 |
|---|---|
| F1 | local 전수 + 저신뢰·커버 후보 remote 승격, 1.43M 전수 remote 금지, WP3 역할을 계약 원문으로 고정 |
| F2 | parent 컬럼과 Neon 태그 3테이블을 분리하고 동일 release/deploy cycle의 `--with-r4` 재빌드 복원 |
| F3 | era 항상 재계산, R4 나머지 4축 legacy 1:1 carry-over, 신규/비1:1 NULL 소유권 고정 |
| F4 | WP0b를 embedding 제외 44컬럼으로 확장; live 45컬럼 exact-set gate, SELECT-only 우선, build/validation report 영속화 |
| F5 | 기존 matcher 2개 size/SHA pin 및 `strict_sim >= 90` 하한 고정 |
| F6 | WP0a card에 ACL 최초 차단 경계 259건 전량과 신규 잔여물 2개 사용자-gated cleanup 등록 |
| F7 | normalized-pixel SHA 동치류 + 대표 direct-edge star, transitive closure 금지, E2 full_v5 출처 고정 |
| F8 | applicability 위반 attempt fail-closed 보존; drawing_kind `unknown`+advisory 허용 |
| F9 | `covers_by_type` 정확히 5키, default NULL publish 차단, child 건물별 delete-then-insert |
| F10 | S2/S3/S4 로컬 key는 `vnx_bld_*`; serving ID는 S6에서만 |
| F11 | redundant `(canonical_bld_id, sort_order)` 보조 index 제거 |
| F12 | SQL comment/literal 제거 후 statement-boundary scan으로 `; DROP` 우회 차단 |
| F13 | gold 4어휘 매핑과 N10 blind 1인 review + adjudicator recheck 고정 |

## WP0b F4 안전 계약

- live schema: 알려진 45컬럼 exact-set이 아니면 DB row를 쓰기 전에 FAIL
- export: `embedding`만 제외한 44컬럼; `source_refs`, serving baseline,
  `image_derived`, cover/CDN, publishability, tag parent fields, R4 5축, timestamps 포함
- preferred credential: `.env.make-web`의 `BUILDINGS_DB_*` SELECT-only role
- fallback credential: `.env` writer role도 repeatable-read + server-enforced read-only
- SELECT 권한 확인, make_web role의 DML privilege 부재 확인, exact 39,478 row/PK 확인
- canonical ID-order JSONL + no-clobber `.partial` promotion
- build PASS report: `data/canonical/legacy_neon_snapshot_2026q3.report.json`
- independent report: `data/canonical/legacy_neon_snapshot_2026q3.validation.json`

현재 `.env`, `.env.make-web`, snapshot, `.partial`, 두 report는 모두 없다. 따라서 얇은
아티팩트가 planned filename을 점유한 적이 없고 WP0b는 여전히 **connection 전 BLOCKED**다.

## Matcher pins

| File | Bytes | SHA-256 |
|---|---:|---|
| `canonical/match_buildings_sequential.py` | 26,812 | `3cf9592a9e54cd4e07512461ca08dc5ca7f8dd5e6cd97b2d6c118dd0b265d87b` |
| `canonical/match_phash_check.py` | 5,450 | `3dcc73152d822fe2b8eaccbb333625672378a3f6cfa59d3099cda0ca28e64fe7` |

## Output hashes

| File | SHA-256 |
|---|---|
| `docs/vnext/CONTRACT_IDENTITY.md` | `1ee13f1a2a7ffe7dcaf5b7c1cbaf419c5999d53bcd473311499b74070aabeca6` |
| `docs/vnext/CONTRACT_GALLERY_SEMANTICS.md` | `eaba317ef330ce7bed94f5b57e9b28bd702282eec1ef0e4d46fadaa8ffb75e1c` |
| `docs/vnext/CONTRACT_COVERS.md` | `671ac19733463abbff1ce4c11a23eef1b592c8aebddf3af4c9acc28c8431c0e6` |
| `docs/vnext/PROJECTION_SPEC.md` | `90beffec1d25afed9c6bddaa5f42ef4077ed8c6019c1764a308f2b8f140bbfd5` |
| `docs/vnext/BUILDING_IMAGES_DDL.sql` | `d924143d1c8816ccd26ea73128ca125e7fa0f853531912c87939fe6b8e585aea` |
| `tools/export_vnext_legacy_neon_snapshot.py` | `5f33ec6565baa3d05e67edbc4a6cf41a18fe3e76de4d6621cba4d8e97332b2c1` |
| `tools/validate_vnext_legacy_neon_snapshot.py` | `cfbd6aa2cc72969c660f74613e567198ea0523710d7e1abef46d19c80d0354af` |
| `tools/validate_vnext_wp0c_contracts.py` | `67bf1a897f6e91b16252479050036833f44ff7fe2d5d46a309622f675e6279e0` |

WP0a job card hash는 259행 appendix 추가로 변경되므로 해당 tracked file 자체와 이 card의
SHA는 Git commit이 provenance를 담당한다.

## Verification

- `validate_vnext_workspace_freeze.py`: **PASS**, 38 checks, 0 failures;
  frozen manifest SHA unchanged
  `cd787a0088161fd0e8dddfe23e1943c0a41992f1e1a214b67fcce5d3ae17dcc3`
- `validate_vnext_wp0c_contracts.py`: **PASS**, 5 files, 0 errors,
  network/LLM/DB 0
- pytest WP0a+WP0b+WP0c: **18 passed**
- regression includes thin/extra-embedding row rejection, live schema drift rejection,
  report no-clobber, UTC timestamp normalization, `SELECT 1; DROP ...` rejection,
  banned words in comment/string false-positive 방지
- `git diff --check`: whitespace errors 0

첫 pytest는 알려진 system `pytest-of-user` ACL 때문에 tmp fixture 6개가 setup ERROR였고,
계약 anchor 1개가 줄바꿈을 가로지른 문제를 발견했다. anchor를 안정 문자열로 수정하고
repo 밖 전용 basetemp에서 최종 18/18 PASS했다. repo 안 테스트 residue는 추가하지 않았다.

## Resume gate

1. F1~F5 중심 Claude 재확인
2. 사용자가 repo root에 `.env`와 권장 `.env.make-web` 복구
3. `python tools/verify_env.py`
4. WP0b read-only export + independent validation; PASS card와 row/SHA 제시
5. 다음 게이트 승인 전 WP1/Neon/R2 write 금지
