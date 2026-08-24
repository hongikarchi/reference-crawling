# WP1 작업 지시 — P1 결정론 이미지 identity + S2 초안 (2026-08-24, Claude 발행)

전제: WP0 게이트 전부 PASS (`20260824_vnext_wp0_gate_review.md`). 유료 LLM/Vision 0,
Neon/R2 쓰기 0인 WP다. 기준 계약: `docs/vnext/CONTRACT_GALLERY_SEMANTICS.md` §1~§3.

## 단위와 범위

- **작업 단위는 source building** `(source, source_building_id)`, 모집단 91,803.
  S1(vnx registry)은 WP2 산출물이므로 아직 없다 — 이 WP의 산출물은 **S2 "초안"**이며,
  모든 행이 source building 키를 보존해 P3에서 vnx 단위 union이 순수 re-key가 되게 한다.
- 이번 WP는 **N10 smoke까지만**: 건물군 10개(소스 균형, 일반 8 + 리스크 2 — 대형 갤러리
  1, 중복 다발 1). full 확장은 별도 게이트 후.

## Stage A — offline 그룹핑 (network 0)

입력: E2 full_v5 + E3 full_v1 (동결 SHA를 열기 전·후 검증, read-only/immutable).

1. 건물별 exact normalized-pixel SHA 동치류 생성.
2. 동치류 대표에 direct edge만 star 부착(E2 저장 엣지만; transitive closure 금지 —
   계약 §2). member↔대표 direct edge 없으면 같은 group 금지.
3. exclusion ledger(계약 §3) + **회계 불변조건**: gallery + exclusion + unresolved ==
   입력 representative 모수, 건물별 정확 일치.
4. 대표 변형 선정: 검증 성공 > usable resolution > crop/overlay 손실 > E3 quality,
   tie는 stable image key. score/config는 manifest에 버전·SHA 고정.
5. 산출: `data/enrichment/vnext_gallery_draft_n10_v1.db` + 독립 validator + report.

## Stage B — verified-pixel pass (HTTP만, Vision/LLM 0)

N10 gallery member에 한정(수십 장 규모). 검증된 census 패턴 재사용.

1. 고정 manifest의 E1 URL fetch, raw response SHA + normalized-pixel SHA 검증.
   terminal 상태 구분(exact/delivery_changed/source_changed/failed), fail-closed,
   결과 관찰 후 표본 교체 금지.
2. 파생본 로컬 캐시(P4B와 공유할 경로/키 규약 명시).
3. crop-resistant local feature 추출을 이 pass에서 함께 smoke(같은 픽셀 1회 사용 원칙).
   feature 알고리즘·버전은 manifest 고정. **feature는 direct pair evidence로만 기록.**

## 수용 기준 (게이트)

- 독립 validator 전항 PASS + 회계 불변조건 건물별 정확 일치
- **false collapse 0**: 10개 건물 갤러리를 opaque sheet로 렌더해 블라인드 1인 리뷰
  (서로 다른 이미지가 한 group으로 접힌 사례 0)
- Stage A network 0 / Stage B HTTP만·Vision/LLM 0 / 완료 아티팩트 resume 시 요청 0
- 동결 입력 SHA 전·후 불변, WAL/SHM/journal 미생성
- job card + 산출물 SHA pin

## 금지·주의

- 신규 파일은 `vnext_*` namespace만. 동결 증거·기존 파일 수정 금지.
- 구현 에이전트 동시 1개, 장출력 억제(§5 수칙).
- v2.x식 과잉 세리머니 금지: 자식 continuation 아티팩트·2인 리뷰 불요(N10은 1인).
- 실패 시 FAIL 리포트 후 정지 — 재설계는 Claude 판정.
