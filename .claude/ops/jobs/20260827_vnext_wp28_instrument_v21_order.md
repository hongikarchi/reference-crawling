# WP2.8 작업 지시 — 계측기 v2.1 (관찰층 보정) (2026-08-27, Claude 발행)

전제: WP2.7 판정(`20260827_vnext_wp27_gate_verdict.md`). 파생 규칙은 옳고 관찰층이
어긋났다. 근인은 지시문의 기권 권장 문구와 어휘 발명(둘 다 Claude 귀책)이다.
**v2.1은 마지막 계측기 반복이다** — 실패 시 계측기 작업 종료.
측정·교정만. 티어 승격·merge·ID 발급·Neon/R2·유료 API 0.

## Stage A — 어휘 추출 (오프라인, 무비용)

N100 DB `blind_reviews.rationale`에서 7축의 **실제 사용 어휘를 전량 추출**한다
(`image`/`name`/`firm`/`place`/`year`/`scope` 각 축의 관측된 값과 빈도).
추출된 어휘만 v2.1의 controlled value로 쓴다. **새 값을 발명하지 말 것.**
`component_vs_umbrella`, `phase_or_extension` 등 WP2.7이 도입한 값은 폐기한다.

## Stage B — 파생 함수 오프라인 검증 (무비용, 리뷰 없음)

N100 105행의 **조정된 축 관찰 → 최종 gold 라벨** 매핑을 재현하는 결정론 함수를 만든다.

- 두 리뷰어 축이 갈리는 행은 N100 조정 결과를 정답으로 삼아, 조정된 축 집합을 입력으로
  본다(조정 축이 저장돼 있지 않으면 그 사실을 보고하고, 두 리뷰어 축의 합의 규칙을
  명시적으로 정의해 사용한다).
- 함수는 `if` 규칙 목록으로 표현하고 각 규칙의 적용 건수를 보고한다. 학습·피팅 금지,
  사람이 읽을 수 있는 규칙이어야 한다.
- **게이트 B**: 이 함수가 N100 105행의 gold 라벨을 **≥95% 재현**해야 한다.
  실패 시 재현 실패 행을 전량 나열하고 정지 — 리뷰 실행으로 넘어가지 않는다.
- 참고 실측(전량 대조표는 직접 재계산할 것): `inconclusive`→insufficient 12/12,
  `unavailable`→insufficient 3/3, `different_objects`→different 20·insufficient 4,
  `same_object`→same 160·related 4·insufficient 7.

## Stage C — 관찰 지시문 교정

- WP2.7 지시문에서 **기권을 권장하는 문구를 전부 삭제**한다. 네 값이 동등하게 정당한
  관찰임만 중립적으로 기술한다.
- `image` 각 값을 N100 용법에 맞춰 정의하고, 다음 명확화를 **반드시 포함**한다:
  **부분 뷰만 있거나, 각도가 다르거나, 이미지 수가 적다는 것 자체는 `inconclusive`가
  아니다. 동일 대상이 식별되면 `same_object`다.** `inconclusive`는 비교 가능한 건물
  형상이 이미지에 없을 때(도면·다이어그램만, 출판물 지면, 판독 불가)이고,
  `unavailable`은 한쪽에 사용 가능한 이미지가 아예 없을 때다.
- 패킷 빌더·렌더러·자산 상한(측당 2장)·블라인드 절차는 계속 동일하게 유지한다.
- 조정자는 라벨이 아니라 **축을 조정**하고, 라벨은 Stage B 함수로 재파생한다.

## Stage D — 동일 24 seed 재probe와 판정 기준 (측정 전 선언, 사후 변경 금지)

WP2.6/2.7과 동일한 24 seed 재사용(리뷰어는 무상태 격리). 기존 gold·이전 probe 결과는
ledger 동결 후 공개.

**관찰층 게이트(주 판정)** — N100 축 관찰과 직접 대조:

1. D 15쌍(N100 30개 관찰 만장일치 `image=same_object`): v2.1 조정 축이
   **≥14/15에서 `same_object`**
2. D 15쌍의 `scope`: **≥14/15에서 `same`**
3. N100이 `inconclusive` 또는 `unavailable`로 관찰한 seed: v2.1이 **과반에서
   `same_object`가 아닌 값**으로 관찰
4. P1 3쌍: 파생 라벨이 **≥2쌍에서 비-`same`**

네 조건 모두 충족해야 PASS. 하나라도 미달이면 FAIL이며 **정지**한다(재시도·재설계 금지).

## 산출물

`vnext_identity_instrument_v21_probe_v1` manifest/DB/report + 독립 validator + job card +
프로토콜 v2.1 문서. report에 Stage A 어휘표, Stage B 규칙별 적용 건수와 105행 재현율·
불일치 행 목록, **행별 4자 대조표**(N100 축·gold / v1 라벨 / v2 라벨 / v2.1 축·파생 라벨),
게이트별 결과, 쿼터 기록 포함.

## 다음 (Claude)

PASS 시 WP2.9(계측기 v2.1로 S1~S4 fresh disjoint 재측정, false merge / related /
insufficient **세 비율 분리** 보고) 발행. FAIL 시 계측기 종료 후 대안을 사용자 결정으로 상정.
