# vNext identity contract v1

상태: WP0c 확정 계약. 대상은 Divisare 29,891건 + Architizer 61,912건,
총 91,803 source building이다. 기존 canonical 39,478건은 정답 집합이 아니라
legacy ID 호환·비교용이다.

## 1. 안정 키와 입력

- source building 키: `(source, source_building_id)`
- source image 키: `(source, source_asset_id)`
- vNext 내부 건물 키: `vnx_bld_*`
- 입력은 WP0a가 고정한 curated 2개, E1 v1.2 2개, E2 full_v5, E3 full_v1과
  WP0b legacy snapshot뿐이다. 입력 SHA가 달라지면 fail-closed한다.
- E2의 `metadata_building_pairs` 6,754행과
  `cross_source_building_candidates` 9,026행은 후보·증거다. E2 자체의
  `no_identity_decision`을 존중하며 이를 곧바로 merge 판정으로 읽지 않는다.
- 기존 검증 matcher도 입력으로 고정한다.
  - `canonical/match_buildings_sequential.py`: 26,812 bytes,
    SHA-256 `3cf9592a9e54cd4e07512461ca08dc5ca7f8dd5e6cd97b2d6c118dd0b265d87b`
  - `canonical/match_phash_check.py`: 5,450 bytes,
    SHA-256 `3dcc73152d822fe2b8eaccbb333625672378a3f6cfa59d3099cda0ca28e64fe7`
- 이름 positive 증거는 위 matcher에서 검증된 `strict_sim >= 90` 계보에 결박한다.
  builder manifest는 이 임계값을 강화할 수만 있고 완화할 수 없다. architect+exact year
  gate와 pass-2의 country/year±1·token-overlap 조건도 해당 matcher SHA와 함께 버전 고정한다.

## 2. 후보 생성과 판정은 분리한다

후보 생성은 아래 union으로만 한다.

1. E2 metadata pair
2. E2 cross-source image candidate
3. 버전이 고정된 기존 matcher의 architect/year blocking 결과

후보 밖 두 건물을 전수 비교하지 않는다. 각 후보는 다음 네 상태 중 하나를 갖는다.

| 상태 | 의미 | component 결합 |
|---|---|---|
| `merge` | 동일 건물/동일 프로젝트 레코드 | 사람 승인 뒤에만 결합 |
| `related_but_separate` | 같은 단지·단계·계열일 수 있으나 별도 건물. 계획의 `related`를 정규화한 값 | 결합 금지 |
| `reject` | 서로 다른 건물 | 결합 금지 |
| `defer` | 증거 부족 또는 충돌 미해결 | 결합 금지 |

규칙이 만드는 것은 `merge_candidate`다. vNext 첫 릴리스의 실제 `merge`는 예외 없이
사람 승인(`reviewer`, `reviewed_at`, `review_version`)이 있어야 한다.

## 3. 독립 증거와 hard conflict

독립 증거 family는 다음과 같다.

- 이름·별칭 identity
- architect/firm identity
- 위치·site identity
- 연도·phase compatibility
- source URL 또는 외부 stable ID
- 검증된 pixel/image overlap

같은 family 안의 여러 점수나 여러 pHash 쌍은 증거 1종으로 센다. merge 후보는
독립 family 2종 이상이 양성이며 hard conflict가 0일 때만 가능하다. 정확한 threshold와
normalizer는 builder manifest에 버전·SHA로 고정하며 계약 변경 없이 조정하지 않는다.

hard conflict에는 최소한 다음이 포함된다.

- 기존 matcher의 architect/year gate가 명시적으로 차단
- 충분한 양쪽 이미지 집합에서 검증된 pHash veto가 차단
- 서로 양립할 수 없는 국가/site 또는 프로젝트 phase
- `reject`로 확정된 두 source building을 같은 component에 넣는 경로

pHash/crop은 pair의 직접 증거 또는 veto로만 사용한다. A-B, B-C가 가깝다는 이유로
A-C image/building 관계를 추론하지 않는다.

## 4. component 불변조건

- 승인된 `merge` edge만 union한다.
- `related_but_separate`, `reject`, `defer` edge는 union-find 입력이 아니다.
- component 내부에 `reject` 또는 `defer` pair가 생기면 산출물은 FAIL이며 배포하지 않는다.
- source building은 정확히 한 active component에만 속한다.
- decision, evidence, review row는 삭제하지 않고 supersession 관계로 버전 관리한다.

## 5. vNext registry

- registry에서 발급한 `vnx_bld_*`는 재사용·재번호화하지 않는다.
- 최초 발급 순서는 고정 manifest의 source key 정렬로 결정한다.
- component 병합 시 가장 먼저 발급된 ID가 survivor이고 나머지는 alias가 된다.
- component 분할은 새 버전·사람 승인 없이는 금지한다. 분할 시 원 ID의 survivor와
  새 ID의 provenance를 모두 남긴다.

S1은 최소한 registry, source membership, candidate edge, decision, evidence,
human review, alias/supersession, legacy crosswalk를 보존한다. 실패·미판정도 행이다.

## 6. legacy crosswalk와 serving ID

crosswalk 상태는 `one_to_one`, `many_to_one`, `one_to_many`, `unmatched`, `conflict`로
구분한다.

- legacy ID 하나가 vNext 하나에만 대응하고, 그 vNext도 legacy 하나에만 대응하며
  source membership이 모순 없을 때만 `one_to_one`이다.
- `one_to_one`이면 Neon projection PK로 legacy `canonical_bld_id`를 유지한다.
- 나머지는 임의의 legacy ID를 고르지 않고 vNext 기반 신규 serving ID를 쓴다.
- 기존 swipe 연결 영향은 crosswalk에 명시하지만, `user_data` 수정은 이 계약 범위가 아니다.

## 7. QA와 중단

- N10: false merge 0, hard conflict 0
- N100: accepted edge precision 99% 이상. 표본이 부족하면 사람 승인 유지
- false merge 1건, component conflict 1건, 입력 SHA drift, 독립 validator FAIL이면 즉시 중단
- validator는 91,803 source membership 전량, edge 양끝 존재, component 불변조건,
  registry 안정성, crosswalk 양방향 cardinality를 독립 재계산한다.

Archello/Metalocus 추가, 자동 merge 승격, legacy swipe migration은 v1 범위 밖이다.

## 8. gold 어휘와 N10 리뷰 프로토콜

identity gold의 관찰 어휘와 S1 결정 어휘는 다음처럼 단일 매핑한다.

| gold | S1 decision |
|---|---|
| `same` | `merge` |
| `related` | `related_but_separate` |
| `different` | `reject` |
| `insufficient` | `defer` |

계획이나 과거 아티팩트의 짧은 값 `related`는 ingest 시
`related_but_separate`로 정규화하며 새 decision row에는 쓰지 않는다.

N10은 후보 순서·source rank·matcher 점수·자동 결정 제안을 가린 블라인드 1인 리뷰로
gold를 만든다. 리뷰어는 원본 메타데이터와 직접 이미지 증거만 보고 네 값 중 하나를
선택하며 근거와 hard conflict를 기록한다. 이후 판정자가 reviewer label과 증거를 다시
열어 계약 매핑·증거 family·conflict 적용을 재확인한다. 불일치는 자동 합의하지 않고
`defer`로 남긴다. N100/승격의 2인+조정 프로토콜과 혼용하지 않는다.
