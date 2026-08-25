# WP2.1 작업 지시 — 국가 정규화 수정 + 후보 v2 재생 (2026-08-25, Claude 발행)

## WP2 판정

- **lane C 게이트 PASS 인정** (gate json 확인: false merge 0, hard conflict 위반 0,
  재판정 불일치 0, merge/ID 발급 0, 유료 $0. validator 67/67).
- 단 **체계적 false reject 결함 확인**: conflict-boundary 2쌍이 실제 동일 건물인데
  Turkey/Türkiye 문자열 불일치로 국가 hard conflict가 발화해 reject됨. 게이트(정밀도)는
  지켰지만 재현율 결함이며 원인이 체계적이므로 **Codex 권고(정규화 수정 → v2 재생 →
  fresh N10 → 이후 N100) 승인**. S1 registry 진행은 이르다는 판단도 동의.

## 결정 1 — 정규화 소스는 재발명하지 않는다

레거시가 같은 버그 클래스를 이미 해결했다("source country conflict 216건",
`tools/canonical_v2_c17_make_web_polish.py` 도입부 → 최종 구현
`tools/canonical_v2_c21_make_web_polish.py:129 _normalize_country_full`, Türkiye/
Deutschland 등 포함).

- `_normalize_country_full` + alias/known-country 테이블을 **verbatim 복사**해
  `canonical/vnext_country_norm.py`로 독립 모듈화(레거시 스크립트 import 금지).
  원본 파일 SHA를 provenance로 pin하고, 대표 alias 표본에서 c21과 출력 동일성을
  테스트로 증명한다. `core/vocab.py`는 건드리지 않는다.

## 결정 2 — conflict 규칙 (CONTRACT_IDENTITY §3 개정 1줄 + 새 SHA pin)

- 국가 hard conflict는 **양쪽 모두 known canonical 국가로 정규화되고 그 값이 다를
  때만** 발화한다.
- 어느 한쪽이 정규화 불가·모호(예: "Georgia"류 국가/주 중의어 — 모호 목록은 builder
  manifest에 고정)이면 국가 family는 **중립**(positive도 conflict도 아님) — 다른
  증거로 판정하고 부족하면 defer.
- 위치 positive 증거 family도 동일 정규화 값을 사용한다.

## 실행 지시

1. `vnext_identity_candidates_v2` Stage A 재생 (offline, network 0, 유료 0).
   v1 아티팩트 불변 보존, v1 DB SHA pin.
2. report에 **v1→v2 상태 전이 전수 집계**: 특히 reject→merge_candidate/defer 목록과
   건수, 정규화로 상태가 바뀐 모든 쌍의 명세. Turkey/Türkiye 2쌍이 올바르게
   재분류됐는지 명시 확인.
3. **fresh disjoint N10**: 새 seed, v1 리뷰 쌍 제외. 층화에 **상태 전이 쌍 ≥2**를
   포함(정규화가 새 false merge를 만들지 않는지 회귀 확인). 리뷰 프로토콜·게이트는
   WP2와 동일(계약 §8, lane C).
4. 독립 validator(전이 replay 포함) + 테스트 + job card + SHA pin.
5. 실패 시 정지 후 FAIL 리포트. 성공 시에도 N100은 **별도 발행 대기** — Claude가
   v2 분포(특히 merge_candidate 수 변화)를 보고 N100 표본 설계를 확정한다.

## 금지·주의

- merge 실행·vnx ID 발급·Neon/R2 작업 0. 신규 파일 `vnext_*` namespace만.
- 정규화 테이블에 c21에 없는 항목을 추가하려면 manifest에 명시적 추가 목록으로
  분리 기록(무단 확장 금지, Claude 재확인 대상).
