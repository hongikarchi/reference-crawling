# vNext WP0 게이트 리뷰 (2026-08-24, Claude)

대상: Codex 커밋 213c263, 1f38ada, 5d65015, cb2b3de (WP0a/WP0b/WP0c).
방식: 병렬 적대적 리뷰 5종 (계약 3건, 익스포터 안전성, 동결 manifest 감사).

## 판정 요약

| 산출물 | 판정 |
|---|---|
| WP0a 동결 manifest | PASS-with-issues — 6개 기준 SHA 문서값 일치, 독립 재해시 2건 일치, validator 38체크 PASS, 삭제 0건. 단 F6 |
| WP0b 익스포터 | PASS-with-issues — **read-only 확인**(SELECT 2문뿐, 세션 readonly 서버 강제 + fail-closed 검증, no-clobber `.partial`→rename, 자격증명 로깅 없음). 단 F4를 첫 실행 **전에** 수정 |
| CONTRACT_IDENTITY | PASS-with-issues — 증거 2계열+무충돌, 사람 승인 병합, 4분류, transitive 금지, crosswalk 모두 적합. 단 F5, F13 |
| CONTRACT_GALLERY_SEMANTICS | **FAIL** — F1 blocker. 그 외(축소 라벨 9필드, drawing 적용성, perspective 강등, uncertainty advisory, exclusion ledger)는 적합 |
| CONTRACT_COVERS + DDL + PROJECTION_SPEC | PASS-with-issues — 비교선택·슬롯 규칙·롤백·user-gate 적합. 단 F2, F3, F9~F12 |
| 테스트 | 신규 13개 전부 PASS 재현 (7+4+2) |

## 수정 지시 (Codex 실행)

### Blocker
- **F1** `CONTRACT_GALLERY_SEMANTICS.md` §7: 퍼널 원칙을 계획 §2 P4B 그대로 복원 —
  "local 엔진 전수 + 저신뢰(이중판정 불일치·low score)·커버 후보만 remote 승격.
  **1.43M 모집단 전수 원격 Vision은 WP3 결과와 무관하게 금지.** WP3 N10은 엔진
  선택·임계값 캘리브레이션만 결정한다." 현재 문구는 이 결정을 미결로 되돌려 놓음.

### Major
- **F2** `PROJECTION_SPEC.md`: "태그 3형제"를 부모 컬럼 3개로 재정의한 부분 삭제.
  배포 시퀀스에 Neon `canonical_v2_tag_stats/_tag_centroids/_tag_vocabulary` 재빌드
  (`tools/canonical_v2_tag_stats_build.py --with-r4`, 동일 릴리스, 동일 배포 사이클) 명시.
- **F3** `PROJECTION_SPEC.md` §2 소유권 표에 R4 5축 추가: era는 project_year에서 항상
  재계산; scale/structural/roof/facade는 1:1 legacy 행은 스냅샷(또는 r4 sidecar)에서
  carry-over, 신규 행은 NULL(미해결) — 향후 R4 pass 별도.
- **F4** `export/validate_vnext_legacy_neon_snapshot.py`: 내보내기 컬럼을
  **embedding 제외 전 컬럼**으로 확장(EXPECTED_KEYS 동기화). PASS 리포트 JSON을
  아티팩트 옆에 영속화. 권장: `.env.make-web`의 SELECT-only 롤로 접속(심층 방어).
  ※ 파일명이 no-clobber로 소각되므로 반드시 첫 실행 전에.
- **F5** `CONTRACT_IDENTITY.md` §1: `canonical/match_buildings_sequential.py`,
  `canonical/match_phash_check.py`를 SHA-pin해 입력 목록에 추가. 이름 증거의 positive
  임계는 검증된 strict_sim ≥90 계보에 결박(빌더 manifest는 강화만 가능, 완화 불가).
- **F6** WP0a job card 부록: `git status`의 unreadable 디렉토리 경고 259건(카운트+목록)
  기록. WP0a 테스트가 만든 `data/reports/vnext_pytest_wp0a_tmp_20260824_{1,2}` ACL 잔여물
  2건을 사용자 게이트 정리 목록에 등록. manifest 재빌드는 불필요(validator PASS 유지).

### Minor (F-batch로 일괄, 개별 재리뷰 불요)
- **F7** 갤러리 §1/§2: transitive closure 없는 그룹 구성 규칙 명시(정확 normalized-pixel
  SHA 동치류 + 선정 대표에의 direct-edge star 부착, E3 P2 방식) + 엣지 출처로 E2 full_v5 명시.
- **F8** 시맨틱 §5: 엔진 출력이 적용성 규칙 위반 시 fail-closed(실패 시도로 보존, 기본값
  강제 변환 금지); drawing_kind는 구체값 원칙 유지하되 `unknown`+advisory 불확실 플래그 허용.
- **F9** 커버/프로젝션: covers_by_type는 항상 정확히 5키(값 NULL 허용, 키 생략 금지);
  default 커버 NULL ⇒ `is_publishable=false`+review_required(기존 업로드 validator 정합);
  building_images 동기화는 트랜잭션 내 건물 단위 delete-then-insert.
- **F10** S2/S3/S4 로컬 store의 키는 `vnx_bld_*`; `canonical_bld_id`는 S6 projection에서만.
- **F11** DDL: `idx_building_images_building_sort` 중복 인덱스 제거. 추가 시맨틱 컬럼
  6종(spatial_context 등)은 **D4 승인 범위 내로 판정, 유지 승인**.
- **F12** `validate_vnext_wp0c_contracts.py`: 금지문 스캔의 행 앵커 우회(`; DROP …`) 보강.
- **F13** identity §8: gold 어휘 same/related/different/insufficient ↔
  merge/related_but_separate/reject/defer 매핑 + N10 블라인드 1인 리뷰 프로토콜 명시.

## 이후 순서

1. Codex가 F1~F13 반영(전부 무비용) → Claude 재확인(F1~F5 중심)
2. 사용자: `.env`(+`.env.make-web`) 저장소 루트 복구 → `python tools/verify_env.py`
3. Codex WP0b 실행(read-only 덤프 + 독립 검증) → Claude 게이트 → WP1 착수

## 재확인 판정 (2026-08-24, 대상 ad9f99f) — **PASS**

- F1~F13 전부 파일 원문에서 직접 검증:
  - F1 갤러리 계약 §7 — 전수 원격 금지 원문 + "WP3에서 재미결화 불가" 명시
  - F2/F3 프로젝션 §1·§2·§5 — 태그 3테이블 재빌드 복원, R4 소유권(1:1 carry-over/신규 NULL)
  - F4 익스포터 — 45컬럼 live exact-set fail-closed, embedding 제외 44컬럼 export,
    `.env.make-web` SELECT-only 우선(+connection-level read-only) / writer fallback도
    read-only 강제, report 2종 영속화
  - F5 identity §1 — matcher 2파일 size/SHA pin, strict_sim ≥90 강화만 허용
  - F13 identity §8 — gold 4어휘 매핑, 블라인드 1인+판정자, 불일치→defer
  - F6~F12 minor 일괄 확인(중복 인덱스 제거, statement-boundary 스캔 등)
- 독립 재현: pytest 18/18 PASS(전용 basetemp), 계약 validator PASS(파일 SHA가
  fixes job card 값과 전부 일치), freeze manifest SHA 무변경
- **WP0b 실행 승인.** 조건: read-only export + 독립 validator, 산출물 게이트는
  39,478행 × 44컬럼 + report/validation JSON 2종 제시. 이후 게이트 통과 시 WP1.
