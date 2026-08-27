# WP2.7 게이트 판정 (2026-08-27, Claude) — **FAIL 수용, 근인 확정**

실행은 충실했다(동일 24 seed·48 sheets·93 asset 재사용, 증분 HTTP 0, validator 31/31,
회귀 168). 정지 판단도 지시대로였다. FAIL은 유효 증거로 수리한다.

## 결과

| 게이트 | 결과 | 기준 |
|---|---|---|
| `insufficient` 복원 | 5/7 | ≥4 PASS |
| P1 비-same | 3/3 | ≥2 PASS |
| `related` 분리 | 2/2 | ≥1 PASS |
| D `same` | **10/15** | ≥13 **FAIL** |

v1의 결함(기권 불가, 245행 0건)은 확실히 고쳐졌다(`insufficient` 9건 산출). 그러나
대조군에서 과잉 기권이 발생했다.

## 근인 — 임계값 문제가 아니다

N100 DB에서 축 관찰값을 직접 추출해 대조한 결과:

1. **D 15쌍의 N100 관찰은 30개 전부 만장일치 `image=same_object`, `scope=same`이다.**
   경계 사례가 아니다. v2가 그중 4건을 `inconclusive`로, 1건을 척도 관계로 뒤집었다.
2. **파생 규칙은 옳다.** N100 105행 실측에서 `image=inconclusive` → `insufficient`
   **12/12**, `unavailable` → `insufficient` **3/3**, `different_objects` → different 20,
   `same_object` → same 160/related 4/insufficient 7. 내 규칙 1은 신뢰 프로토콜의
   실제 행동과 일치한다. **문제는 파생층이 아니라 관찰층이다.**
3. **원인 A — 지시문 편향(Claude 귀책).** 내가 WP2.7 지시서에 쓴
   "비교할 수 없음은 정상적이고 바람직한 관찰이며 실패가 아니다"는 기권을 **권장**하는
   문구였다. v1의 붕괴를 고치려다 반대 방향으로 밀어버렸다.
4. **원인 B — 어휘 불일치(Claude 귀책).** N100의 실제 어휘는
   `image ∈ {same_object, different_objects, inconclusive, unavailable}`,
   `scope ∈ {same, unclear, related_separate, unrelated}`다. 나는 `unavailable`을 빼먹고
   `component_vs_umbrella`·`phase_or_extension`을 새로 발명했다. 신뢰 프로토콜이 한 번도
   하지 않은 세분화를 강요했고, 그것이 D의 `related` 이탈을 만들었다.

## 구조 변경 (튜닝 아님)

v1·v2 연속 실패이므로 자체 중단 규칙("같은 hard failure 2회 → 구조 재설계")을 적용한다.
문구를 고쳐 재실행하는 것이 아니라 **검증 구조를 바꾼다**:

- **파생층과 관찰층을 분리 검증한다.** 파생 함수는 N100 105행의 조정된 축에서
  **오프라인·무비용으로** 재현성을 증명한다. 리뷰 실행이 전혀 필요 없다.
- **계측은 라벨이 아니라 축에서 보정한다.** N100이 만장일치로 관찰한 값을 v2.1이
  재현하는지가 게이트다. 라벨 일치는 파생의 결과일 뿐이다.
- **어휘는 발명하지 않고 N100 DB에서 추출해 그대로 쓴다.**

## 한계선 (사전 선언)

**v2.1이 마지막 계측기 반복이다.** 실패하면 계측기 작업을 종료하고 일괄 승인 티어를
포기한다. 그 경우 대안은 사용자용 개별 리뷰 도구 + 범위 축소이며, 별도 결정으로 상정한다.
v2.x Vision 사가의 12회 그라인드를 반복하지 않는다.
