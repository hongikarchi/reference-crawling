# vNext cover + analysis bundle contract v1

상태: WP0c 확정 계약. 커버는 단일 이미지의 `hero` 점수에서 파생하지 않고,
한 건물의 후보들을 슬롯 목적에 맞게 비교해 선택한다.

## 1. 슬롯

건물마다 `default`와 사용자 선택용 5종을 독립적으로 가진다.

| 슬롯 | 1차 eligibility |
|---|---|
| `default` | photograph + exterior + overall 우선, photograph + aerial + overall 차선 |
| `exterior` | photograph + exterior + overall, non-aerial 우선 |
| `interior` | photograph + interior/threshold + overall |
| `drawing` | medium=`drawing`; `perspective`를 포함한 drawing_kind는 subtype |
| `aerial` | photograph + exterior + aerial angle + overall/site_context |
| `detail` | photograph + element_detail/material_detail |

- rendering은 photo slot(`default`, exterior, interior, aerial, detail)에 들어갈 수 없다.
- physical model, mixed, other는 gallery에는 남지만 v1 cover slot의 강제 fallback이 아니다.
- 자격 이미지가 없으면 NULL + `review_required`; 다른 medium을 억지로 넣지 않는다.
- 슬롯은 non-exclusive다. 동일 이미지가 여러 슬롯에서 실제 최선이면 재사용할 수 있다.
- `covers_by_type`은 항상 `exterior`, `interior`, `drawing`, `aerial`, `detail` 정확히
  5개 키를 가진다. 후보가 없는 슬롯은 값을 JSON `null`로 두며 키를 생략하지 않는다.
- default가 NULL이면 `review_required=true`이고 serving projection의
  `is_publishable=false`다. 임의 fallback으로 publishable을 유지하지 않는다.

## 2. 3단계 선택

1. 결정론 filter로 부적격·duplicate·exclusion 이미지를 제거한다.
2. legibility, verified pixel quality, framing 적합성, E3 canonical rerank와
   slot별 reason code로 후보를 정렬한다. score/config는 manifest에 고정한다.
3. 1위가 명확하면 확정한다. margin이 근소하면 같은 건물의 상위 후보만 비교 Vision에
   제공한다. 그래도 애매하면 `review_required`로 보낸다.

비교 Vision은 target slot과 opaque candidate ID만 보고 후보 간 상대 적합성을 판정한다.
다른 건물 후보, source rank, 파일명, 저장된 정답은 보지 않는다. 반환값은 winner 또는
`insufficient`와 acceptable candidate set이며 긴 건물 설명을 만들지 않는다.

S4 selection 행은 로컬 키 `vnx_bld_id`, slot, nullable selected image, ordered candidates,
score/reason/margin, decision method(`deterministic`, `comparative_vision`, `human`),
engine/version, `review_required`, supersession을 보존한다.
`canonical_bld_id`는 S6 projection 단계에서 crosswalk 규칙으로만 붙인다.

## 3. default와 gallery

- default는 건물 전체 외관을 읽을 수 있는 exterior overall을 최우선으로 한다.
- 일반 외관이 없고 aerial overall이 건물 전체를 더 잘 설명할 때만 차선으로 쓴다.
- 도면·detail·interior·비건축 이미지는 default critical error다.
- gallery 순서는 S2/E3 canonical rerank 결과이며 cover 선정 때문에 이미지를 삭제하거나
  중복 삽입하지 않는다.

## 4. analysis bundle

analysis bundle은 cover와 별개인, 미래 P6 건물 분석의 입력 목록이다.

- S2 gallery와 이미 계산된 S3 compact label만 이용해 서로 다른 시각 정보를 고른다.
- 가능한 경우 exterior overall/aerial, interior, drawing, detail/site context를 다양하게
  포함하되 없는 역할을 억지로 채우지 않는다.
- duplicate representative를 두 번 넣지 않고 각 member에 역할·선정 reason을 기록한다.
- bundle 조립 자체는 추가 Vision을 호출하지 않는다. P6가 승인되면 이 묶음을 한 번
  종합 분석하므로 shallow 분류와 deep 분석의 목적·출력이 겹치지 않는다.
- P6가 보류되어도 cover와 gallery는 release할 수 있다.

## 5. QA와 중단

- N10: critical cover error 0, 건물 10/10에서 선택값이 acceptable top-3 안에 있음
- N100: top-1 95% 이상, top-3 99% 이상, P0 baseline 대비 회귀 없음
- gold는 단일 정답이 아니라 slot별 acceptable set을 가진다.
- 비건축/도면 default 등 critical error 1건, 입력 SHA drift, validator FAIL이면 즉시 중단한다.
- uncertainty 자기보고는 cover 게이트가 아니다. semantic lane이 실패해도 cover lane은
  독립 판정하며, 필요한 후보만 직접 비교할 수 있다.
