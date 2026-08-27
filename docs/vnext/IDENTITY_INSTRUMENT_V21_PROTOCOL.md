# Identity Instrument v2.1 Protocol

Version: `vnext-identity-instrument-v21`

이 문서는 WP2.8의 관찰층 보정 계약이다. 리뷰어와 조정자는 최종 identity label을
선택하지 않고 여섯 controlled observation과 필수 자유서술 `basis`만 기록한다.
각 controlled value는 동등하게 유효한 관찰값이며 특정 값을 선호하도록 유도하지 않는다.

## 실행 순서와 정지선

1. Stage A: immutable N100 `blind_reviews.rationale` 210행에서 실제 어휘와 빈도를 추출한다.
2. Stage B: 두 reviewer 축을 고정 합의 함수로 105개 pair-level 축으로 만든 뒤,
   first-match 파생 함수가 N100 gold를 재현하는지 오프라인 검증한다.
3. Gate B가 `100/105` 이상일 때만 Stage C의 이 지시문과 instrument contract를 동결한다.
4. Stage D: WP2.6/WP2.7과 동일한 24 seed, 48 sheet, 93 asset occurrence를
   byte-for-byte read-only로 재사용한다. actor form 발행 전에 4개 gate를 동결한다.
5. Gate B 또는 Stage D gate가 실패하면 정지한다. 사후 threshold 변경, 재리뷰,
   재설계 및 후속 작업 자동 실행은 금지한다.

## Stage A — N100 실제 어휘

여기서 7개 관찰 필드는 여섯 controlled 축과 자유서술 `basis`를 뜻한다.

| 축 | 허용값 |
|---|---|
| `image` | `same_object`, `different_objects`, `inconclusive`, `unavailable` |
| `name` | `agree`, `alias`, `conflict`, `unclear` |
| `firm` | `agree`, `overlap`, `conflict`, `unclear` |
| `place` | `agree`, `conflict`, `unclear` |
| `year` | `compatible`, `conflict`, `unclear` |
| `scope` | `same`, `related_separate`, `unclear`, `unrelated` |
| `basis` | trim된 비어 있지 않은 관찰 근거 |

파서는 선두의 여섯 `key=value; ` 구간을 고정 순서로 읽고 나머지 전체를 `basis`로
보존한다. `basis` 내부의 세미콜론은 구분자로 다시 해석하지 않는다. 위 표에 없는
controlled value를 추가하거나 동의어로 바꾸지 않는다.

## Stage B — pair-level 합의와 파생

N100에는 별도 adjudicated 축이 없으므로 두 reviewer 값이 같으면 그 값을 유지하고,
다르면 다음의 순서불변 고정 우선순위에서 먼저 오는 값을 사용한다.

- image: `unavailable > inconclusive > different_objects > same_object`
- name: `conflict > alias > agree > unclear`
- firm: `conflict > overlap > agree > unclear`
- place: `conflict > agree > unclear`
- year: `conflict > compatible > unclear`
- scope: `unrelated > related_separate > same > unclear`

합의된 여섯 축에서 label은 다음 규칙을 위에서부터 처음 일치하는 하나로 파생한다.

1. `image`가 `unavailable` 또는 `inconclusive`이면 `insufficient`.
2. `image=different_objects` 또는 `scope=unrelated`이면 `different`.
3. `scope=related_separate`이면 `related`.
4. `image=same_object`이고 `scope=same`이면 `same`.
5. 나머지는 `insufficient`.

이 함수는 사람이 읽을 수 있는 고정 규칙이며 학습·가중치 최적화·행별 예외를 사용하지
않는다. Gate B denominator는 carry-forward를 제외한 fresh adjudicated 105쌍이다.

## Stage C — actor 관찰 지시문

모든 축을 독립적으로 관찰하고 `basis`에 화면에서 확인한 근거를 적는다. label, score,
candidate group 또는 이전 판단을 추론하거나 선택하지 않는다.

### Image

- `same_object`: 사용 가능한 이미지로 동일한 물리적 대상이 식별된다.
- `different_objects`: 양쪽에 비교 가능한 건물 형상이 있고 서로 다른 물리적 대상으로
  식별된다.
- `inconclusive`: 사용 가능한 이미지가 있지만 비교 가능한 건물 형상이 없다. 도면이나
  다이어그램만 있는 경우, 출판물 지면만 있는 경우, 또는 판독할 수 없는 경우가 해당한다.
- `unavailable`: 한쪽에 사용할 수 있는 이미지가 전혀 없다.

부분 뷰만 있거나, 각도가 다르거나, 이미지 수가 적다는 것 자체는 `inconclusive`가
아니다. 그러한 차이에도 동일 대상이 식별되면 `same_object`다.

### Metadata axes

- `name`: 동일 표기면 `agree`, 같은 대상을 가리키는 변형·번역·축약이면 `alias`,
  서로 양립하지 않으면 `conflict`, 판단할 정보가 부족하면 `unclear`.
- `firm`: 크레딧이 일치하면 `agree`, 팀 일부가 겹치면 `overlap`, 서로 양립하지 않으면
  `conflict`, 판단할 정보가 부족하면 `unclear`.
- `place`: 같은 장소면 `agree`, 양립하지 않으면 `conflict`, 부족하면 `unclear`.
- `year`: 동일하거나 프로젝트 기록상 양립 가능한 차이면 `compatible`, 양립하지 않으면
  `conflict`, 부족하면 `unclear`.
- `scope`: 같은 프로젝트 범위면 `same`, 연관됐지만 별도 범위면 `related_separate`,
  연관되지 않으면 `unrelated`, 판단할 수 없으면 `unclear`.

사진 일치는 피사체 공유를 보여주지만 프로젝트 범위까지 자동으로 같게 만들지는 않는다.
조정자는 두 reviewer가 기록한 축과 sheet를 대조해 축만 확정하며 label은 위 Stage B
함수로 자동 재파생한다.

## Stage D — 동일 packet, blindness, gate

- packet builder·renderer 결과와 side당 asset 상한 2개를 변경하지 않는다.
- 기존 sheet path, bytes, SHA-256을 참조하며 HTTP fetch, 재시도, redirect, cache write,
  renderer substitution은 전부 0이다.
- 두 reviewer와 조정자는 서로 다른 무상태 actor다.
- N100 축·gold, candidate/group, v1/v2 label과 gate score는 두 review ledger와
  adjudication ledger가 모두 hash-lock된 뒤에만 공개한다.
- 공개는 `adjudication_locked` 이후 24개 control commitment가 일치할 때 한 번 수행한다.

4개 조건은 AND로 결합한다.

1. D 15쌍 중 v2.1 final `image=same_object`가 `>=14/15`.
2. D 15쌍 중 v2.1 final `scope=same`이 `>=14/15`.
3. Stage B 합의 N100 image가 `inconclusive|unavailable`인 고정 5 seed 중 v2.1 final
   image가 non-`same_object`인 것이 `>=3/5`. non-`same_object`는
   `different_objects|inconclusive|unavailable`이다.
4. P1 3쌍 중 v2.1 파생 label이 non-`same`인 것이 `>=2/3`.

## Quota와 외부 변경

- weekly remaining이 30%를 초과할 때만 새 wave를 시작한다.
- 30% 이하는 wave를 시작하지 않고, 20% 이하는 sticky stop으로 이후 활동을 막는다.
- 외부 pipeline Vision/LLM·유료 API, merge, ID 발급, tier promotion, Neon/R2 write는 0이다.

