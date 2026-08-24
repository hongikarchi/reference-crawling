# WP2 작업 지시 — entity resolution 후보 생성 + N10 블라인드 리뷰 (2026-08-24, Claude 발행)

## WP1 종결 판정 (선행)

- WP1.1 **PASS 인정** (Claude 독립 재계산: overlay SHA `ed108eda…b9ac`, v1 불변
  `d63b7a5a…17cc7` 일치; validator 56/56; 계약 §2 개정 1줄 확인).
- 드리프트 지견 기록: **Architizer 단독 17/72(23.6%), Divisare 0/97** — imgix 파라미터
  경로. refresh ledger는 Architizer 우선·Divisare 저빈도로 설계한다(full P1 전 별도 설계).

## WP2 범위

기준 계약: `docs/vnext/CONTRACT_IDENTITY.md` (job card에 SHA pin). 대상은
**cross-source(divisare↔architizer) 건물 쌍**이다. within-source 중복은 curated의
provisional cluster를 그대로 신뢰하고 이번 WP에서 재판정하지 않는다.
유료 파이프라인 호출 0(Vision/LLM API), Neon/R2 쓰기 0.

## Stage A — 후보 생성 (offline, network 0)

1. 후보 universe = 계약 §2의 union만: E2 `metadata_building_pairs`(6,754) +
   `cross_source_building_candidates`(9,026) + **SHA-pin된 기존 matcher의
   architect/year blocking** (curated 모집단 위에서 재계산). 후보 밖 전수 비교 금지.
   legacy 스냅샷은 후보 증거로 쓰지 않는다(단방향 정보 흐름).
2. 쌍별 증거 family 6종 평가(§3), hard conflict 탐지, 제안 상태 4분류 산출.
   이름 positive 임계는 strict_sim ≥90 계보(강화만 허용). threshold/normalizer는
   builder manifest에 버전·SHA 고정.
3. 산출: `data/enrichment/vnext_identity_candidates_v1.db` + 독립 validator
   (universe 재산출, 증거 결정론 replay, 비전이성, component 불변조건 §4).
4. report 통계: 후보쌍 수(출처별), family 분포, 제안 상태 분포,
   auto-merge-eligible(2 family+ & conflict 0) 수, hard conflict 유형 분포.

## Stage B — N10 블라인드 리뷰

1. 표본: **후보 neighbourhood 10개**, 층화 — merge_candidate 강증거 4 /
   metadata-only 2 / image-evidence-only 2 / conflict·경계 2. seed manifest로
   리뷰 전 동결, 사후 교체 금지.
2. 리뷰 프로토콜 = 계약 §8: 점수·제안 상태·rank 은닉, 리뷰어는 소스 메타데이터 +
   직접 이미지 증거만 보고 `same/related/different/insufficient` + 근거 기록.
   판정자 재확인, 불일치는 자동 합의 없이 `defer`.
3. 리뷰용 이미지 fetch는 HTTP만·bounded(WP1 캐시 재사용 우선). 드리프트 이미지는
   **현재 픽셀을 그대로 보여주되 상태를 표기**한다 — 건물 동일성 판단에는 현재
   픽셀도 유효 증거다. fetch 상태 전건 기록.
4. **게이트 (lane C)**: 리뷰어가 `different`로 판정한 쌍이 auto-merge-eligible
   제안에 존재하면 false merge → FAIL. hard conflict 위반 0. `insufficient`/`defer`
   비율은 게이트가 아니라 보고 항목.

## 금지·주의

- 신규 파일 `vnext_identity_*` namespace만. 동결 입력 전·후 SHA 검증, 수정 금지.
- 실제 merge 실행·vnx ID 발급·registry 생성은 이번 WP 범위 밖(후보와 gold까지만).
- 구현 에이전트 동시 1, 장출력 억제, 실패 시 FAIL 리포트 후 정지.

## 게이트 통과 시

Claude가 후보 분포를 보고 다음을 발행: N100 확대 또는 S1 registry 구축(사람 승인
병합 큐 포함). full P1 확대는 refresh ledger 설계 확정 후 별도.
