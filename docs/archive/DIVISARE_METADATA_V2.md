# Divisare Metadata SQLite v2.1

상태: 구현 및 N=10/N=100/full 검증 완료
Parent artifact: Divisare curated SQLite `v1.5` / schema `2`
Output artifact: Divisare metadata `v2.1` / schema `4`

## 1. 목적

이 단계는 다른 사이트와 비교하거나 합치기 전에 Divisare 내부 데이터만
정리하는 source-specific metadata 단계다.

핵심 목표는 다음과 같다.

- Divisare 원본 tag와 article 관계를 손실 없이 보존한다.
- tag를 program, typology, material, location 등의 정규화 claim으로
  투영하되 근거의 독립성을 엄격하게 계산한다.
- 같은 실제 건물을 다룬 여러 article을 연결한 뒤에도 복합 program과
  typology가 사라지지 않게 한다.
- drawing, photo, model, ideas, topics article을 tag나 제목만으로 확정하지
  않는다.
- 기존 D2 결과를 보존하고 승인된 manual decision만 새 redirect로 만든다.
- 결과를 parent와 분리된 불변 SQLite artifact로 발행한다.

구현 version은 다음과 같다.

- Builder: `divisare-metadata-v2-builder-v2.1`
- Metadata: `divisare-metadata-v2.1`
- SQLite `PRAGMA user_version`: `4`
- Evidence, facet, article-kind, primary-value policy: 모두 `v2.1`

## 2. 범위

### 포함

- raw tag와 v1 provenance 보존
- tag claim의 evidence family 및 independence key 계산
- program/typology를 포함한 building facet 재해석
- abstention-first article-kind 후보와 상태
- D2 review state와 승인 기반 redirect
- redirect 후 membership, image gallery, facet, core metadata 재계산
- metadata recrawl queue
- 다른 사이트 가공 전에 사용할 Divisare 전용 export view

### 제외

- exterior/interior/drawing/detail 등 이미지별 의미 판정
- 이미지 모델 호출과 article tag의 gallery 전체 전파
- 이미지 다운로드, SHA-256 또는 pHash 계산
- embedding 또는 vector DB 생성
- Divisare와 다른 사이트 사이의 건물 병합
- `core/vocab.py` 변경
- 원본 credit payload 추가 수집

v1.5에 이미 있는 이미지 URL, occurrence, asset, hash 작업 상태는 복제하지만
metadata v2.1이 새로 판정하거나 갱신하지 않는다.

## 3. 불변 Artifact 흐름

```text
pristine v1.5 SQLite (read-only)
  -> parent 검증 및 SHA-256 계산
  -> SQLite Connection.backup()으로 독립 임시 DB 생성
  -> schema 4 overlay table/view 생성
  -> v2.1 materialization 및 35개 validation
  -> parent SHA-256 재검사
  -> report를 먼저 no-clobber 발행
  -> DB를 최종 commit marker로 no-clobber 발행
```

### Parent 검증

- SQLite URI `mode=ro`와 `PRAGMA query_only=ON`으로 parent를 연다.
- 필수 v1.5 table 존재와 v2 overlay 부재를 확인한다.
- `PRAGMA quick_check = ok`를 확인한다.
- completed build run의 builder가 `divisare-curated-builder-v1.5`인지 확인한다.
- parent `user_version`이 `2`인지 확인한다.
- build 전후 parent SHA-256이 같아야 한다.
- output에서는 `PRAGMA integrity_check`와 foreign-key 검사를 실행한다.

parent를 hard link한 뒤 수정해서는 안 된다. 실제 데이터 복제에는 SQLite
backup API를 사용한다. parent와 output은 서로 다른 실제 파일이며,
기존 output/report 경로는 덮어쓰지 않는다. `--replace`는 제공하지 않는다.

발행 시 report를 먼저 연결하고 DB를 마지막 commit marker로 연결한다.
DB 경로가 존재한다는 것은 DB와 report가 모두 완성되어 발행 단계에
도달했다는 뜻이다.

## 4. Raw Data 보존

v1.5의 기존 table과 view는 이름과 row 의미를 바꾸지 않는다. 특히 다음
source-level 데이터는 v2.1 해석 결과와 분리해 그대로 둔다.

- `source_articles`
- `source_tags`
- `article_tags`
- album 및 membership provenance
- `attribute_claims`
- `source_image_occurrences`
- `article_image_occurrences`
- `image_urls`
- `image_assets`
- `buildings`
- `building_articles`
- `article_match_candidates`
- 기존 building facet, claim, event 및 QA table

tag의 album, slug, 표시명, source ID와 article membership은 raw provenance다.
mapping이 없거나 나중에 바뀌어도 raw row를 삭제하거나 덮어쓰지 않는다.
v2.1 해석은 suffix가 `_v2`인 별도 객체에 기록한다.

## 5. Tag 투영 원칙

| Divisare 계열 | v2.1 용도 | 기본 처리 |
|---|---|---|
| `types` | program 또는 typology | 검토된 정확 mapping은 direct 가능 |
| `houses` | program, typology, material, location, context | 하위 tag별 명시 mapping |
| `plans-details` | article-kind 후보, `plans-of-*` program/typology 보조 근거 | supporting |
| `materiality` | 실제 material과 color | 검토된 정확 mapping은 direct 가능 |
| `cities` 및 국가 tag | country/city 후보와 location 보강 | structured location과 교차 검토 |
| `elements` | structure, facade, roof, 식별력 있는 feature | 하위 tag별 명시 mapping |
| `private/public-interiors` | room/interior context, public program 보조 근거 | 주로 supporting |
| `topics` | intervention, status, context, style, media 후보 | supporting 또는 source topic |
| `ideas` | editorial/source topic과 article-kind 후보 | supporting |

추가 규칙:

- `plans-of-*`는 program/typology의 보조 근거지만 단독 확정 근거는 아니다.
- `stairs`, `columns` 같은 일반 요소는 canonical feature로 자동 승격하지
  않는다.
- `outdoor-stair`, `wooden-column`처럼 식별력이 명시된 요소만 검토된
  mapping을 통해 feature 후보가 될 수 있다.
- `columns + wooden-structures`처럼 서로 다른 tag를 합성해 source에 없던
  관계를 만들지 않는다.
- `topics`와 `ideas`는 raw source topic으로 보존하지만 이름만으로 building
  program이나 article kind를 확정하지 않는다.
- tag 부재는 부정 근거가 아니다.
- vocabulary 변경은 별도 사용자 결정이며 이 단계에서 `core/vocab.py`를
  자동 수정하지 않는다.

## 6. 실제 Overlay Schema

### Table

| 이름 | 역할 |
|---|---|
| `artifact_lineage_v2` | parent/output 및 policy version, decision file provenance |
| `claim_evidence_v2` | v1 claim별 evidence family, independence key, mapping kind |
| `article_kind_evidence_v2` | article-kind 후보 근거 |
| `article_kind_resolution_v2` | article별 kind, 상태, confidence와 resolver 결과 |
| `article_match_reviews_v2` | 모든 v1 D2 candidate와 manual decision provenance |
| `building_redirects_v2` | 승인된 merge의 terminal redirect와 decision IDs |
| `active_building_membership_v2` | redirect 적용 후 materialized article membership |
| `building_images_materialized_v2` | active building별 asset-deduplicated gallery |
| `building_article_roles_v2` | article의 identity/semantic role |
| `building_facets_v2` | building별 candidate/confirmed/rejected facet |
| `building_facet_claims_v2` | facet과 원본 claim의 provenance link |
| `building_attributes_v2` | active/redirect 상태, core metadata와 canonical arrays |
| `article_recrawl_queue_v2` | HTML metadata 재수집 queue |
| `metadata_build_metrics_v2` | build 측정값 |
| `metadata_validation_v2` | validation 결과 |

### View

- `v_active_building_articles_v2`
- `v_article_kind_review_queue_v2`
- `v_metadata_d2_review_queue_v2`
- `v_search_facets_v2`
- `v_building_images_v2`
- `v_metadata_recrawl_queue_v2`
- `v_divisare_buildings_export_v2`

### 주요 Index

- `idx_active_membership_building_v2`
  - `(building_id, article_id)`
- `idx_building_images_order_v2`
  - `(building_id, role_rank, first_position, asset_key)`
- `idx_building_facets_building_v2`
  - `(building_id, axis, status)`
- `idx_building_facets_search_v2`
  - `(axis, value, status, search_tier)`
- `idx_claim_evidence_family_v2`
  - `(evidence_family, independence_key)`
- `idx_article_kind_status_v2`
- `idx_match_review_status_v2`
- `idx_redirect_target_v2`
- `idx_recrawl_priority_v2`

기존 v1.5 객체는 유지되므로 같은 artifact 안에서 v1/v2 결과를 비교할 수
있다.

## 7. Evidence Independence

### Independence key

source tag에서 나온 building-level claim의 실제 기본 key 형식은 다음과 같다.

```text
divisare:article:<article_id>:taxonomy
```

같은 article의 `house`, `residential`, `plans-of-houses`가 같은 값을
지지해도 taxonomy evidence는 하나의 독립 key로만 계산된다. tag slug,
mapping rule 또는 rerun version을 바꿔 같은 source assertion을 새 근거로
세지 않는다.

### 확정 규칙

`direct` claim:

- direct confidence가 `0.85` 이상이면 confirmed 가능
- 충돌 및 axis별 scalar 규칙은 별도로 적용

`supporting` claim:

- 동일한 `axis + normalized value`에 independence key가 2개 이상
- 서로 다른 source article이 2개 이상
- aggregate confidence가 `0.75` 이상

세 조건을 모두 만족해야 confirmed가 된다. 하나라도 부족하면 삭제하지
않고 candidate로 남긴다. `building_facets_v2`는 `article_count`와
`independence_group_count`를 모두 저장하고 validation에서도 두 조건을
확인한다.

redirect로 서로 다른 article이 같은 active building에 모이면 facet도
active membership 기준으로 다시 계산한다. 이때만 서로 다른 article의
supporting evidence가 하나의 building fact를 공동으로 지지할 수 있다.

## 8. Program과 Typology

canonical 값은 confirmed facet 전체를 담은 정렬되고 중복 없는 JSON 배열이다.

- `building_attributes_v2.programs_json`
- `building_attributes_v2.typologies_json`

export에서는 각각 `programs`, `typology_tags`로 제공한다. 복합 용도나 복합
typology는 정상 상태이며 배열 값이 여러 개라는 이유만으로 conflict로
취급하지 않는다.

호환용 scalar 규칙:

| confirmed 배열 길이 | `program_primary` / `typology_primary` |
|---:|---|
| 0 | `NULL` |
| 1 | 배열의 유일한 값 |
| 2 이상 | `NULL` |

따라서 `program` scalar와 `typology_primary`는 canonical source가 아니라
단일 값일 때만 제공되는 compatibility projection이다. downstream은 배열을
우선 사용해야 한다.

`mixed_use`와 `multi_typology`는 confirmed 배열 길이가 2 이상일 때 `1`이다.
style, structural system, roof type, facade pattern/system 같은 scalar axis는
확정값 충돌 시 primary를 만들지 않고 `facet_conflicts_json`에 남긴다.

## 9. Article Kind

가능한 값:

- `project`
- `drawing_feature`
- `photo_feature`
- `model_feature`
- `concept_editorial`
- `mixed_feature`
- `unresolved`

상태:

- `confirmed`
- `candidate`
- `ambiguous`
- `unresolved`

### 후보와 확정의 경계

다음 metadata는 모두 candidate 근거다.

- `plans-details`, `ideas`, `topics` album/tag
- `content_hint`
- title/slug lexical match

tag와 title/slug가 같은 kind를 지지해 confidence가 높아져도 metadata만으로
confirmed가 되지 않는다. confirmed에는 다음 authoritative evidence 중
하나가 strong 상태로 필요하다.

- explicit HTML DOM evidence, `html_explicit`
- 승인된 manual evidence, `manual`

현재 v1.5 parent에는 이 authoritative evidence가 없으므로 N=10/N=100
결과에는 confirmed article kind가 없다. HTML recrawl이나 별도 승인 입력이
추가되기 전까지 metadata 결과는 candidate, ambiguous 또는 unresolved다.

### Explicit unresolved

근거가 없으면 `article_kind='unresolved'`와 `status='unresolved'`를 함께
저장한다. legacy 기본값 `project`로 강제 변환하지 않는다. ambiguous인
경우 표시 값은 `mixed_feature`지만 상태가 사실 여부를 명확히 구분한다.

### Confirmed-only semantic role

`building_article_roles_v2.article_role`에서 `drawing_feature`,
`photo_feature`, `model_feature`, `concept_editorial`, `mixed_feature` 같은
semantic role은 article kind가 confirmed일 때만 쓴다.

- canonical primary article은 identity role인 `primary`
- confirmed가 아닌 나머지 article은 `supporting_project`
- 후보 kind와 상태는 별도 column에 그대로 보존

article kind를 gallery의 모든 image classification으로 전파하지 않는다.
이미지별 의미 판정은 후속 E2 단계다.

## 10. D2 Decision과 Redirect

Full v1.5 parent의 고정 baseline:

- `auto_clustered = 66`
- `open = 220`

기존 auto-cluster 66건은 그대로 보존한다. open 220건은 review queue에
유지하며 metadata score나 article-kind 후보만으로 자동 merge하지 않는다.

decision input의 action은 `merge`, `reject`, `defer`다. `merge`에는 다음이
모두 필요하다.

- `approved: true`
- 비어 있지 않은 `reviewer`
- 비어 있지 않은 `reviewed_at`
- 비어 있지 않은 `reason`
- versioned decision file과 SHA-256

예시:

```json
{
  "schema_version": 1,
  "version": "divisare-d2-review-v1",
  "decisions": [
    {
      "decision_id": "divisare-d2-96467-343892-v1",
      "article_id_a": 96467,
      "article_id_b": 343892,
      "decision": "merge",
      "approved": true,
      "reviewer": "reviewer-id",
      "reviewed_at": "2026-07-28T00:00:00+09:00",
      "reason": {"identity_basis": "manual source-page comparison"}
    }
  ]
}
```

`decision_id`를 생략하면 decision version과 article pair에서 안정적으로
생성한다. D2 candidate set에 없는 pair, 중복 pair, merge/reject가 충돌하는
component는 build를 실패시킨다.

승인된 merge만 union-find component와 `building_redirects_v2`를 만든다.
survivor는 component의 최소 stable building ID다. 모든 redirect는 terminal
survivor를 가리키며 cycle과 self redirect를 허용하지 않는다. `reject`와
`defer`는 기존 building을 합치지 않는다.

decision 입력이 바뀌면 기존 DB를 수정하지 않고 새 versioned v2 artifact를
만든다.

### 현재 D2 입력계약의 한계

- `reject`는 기존 `open` pair를 합치지 않는 결정이다. v1.5에서 이미
  `auto_clustered`가 된 pair를 다시 분리하는 기능은 아니다.
- 잘못된 auto cluster를 분리하려면 v1 단계부터 다시 빌드하거나, 명시적인
  split decision과 building ID 재할당 정책을 별도로 설계해야 한다.
- 현재 decision 입력은 v1.5의 286개 D2 candidate pair
  (`auto_clustered 66 + open 220`)만 받는다. 이 집합 밖에서 새로 발견한
  중복 후보를 처리하려면 candidate provenance를 포함하는 입력계약 확장이
  필요하다.
- 이번 full artifact에는 승인된 manual merge decision이 없으므로 새
  redirect는 `0`개다. 기존 strict auto cluster 66건만 유지한다.

## 11. Materialized Membership과 Gallery

redirect가 확정된 뒤 `active_building_membership_v2`를 생성한다.

- article 하나당 정확히 한 row
- 원래 `source_building_id` 보존
- terminal active `building_id` 저장
- `(building_id, article_id)` index 제공

`v_active_building_articles_v2`는 이 materialized table의 단순 projection이다.
export의 source refs, architect, tag 집계는 계산식 `COALESCE` view가 아니라
indexed active membership을 사용한다.

그 다음 `building_images_materialized_v2`를 생성한다.

- active building과 `asset_key` 조합으로 중복 제거
- cover 우선, position, URL ID 순으로 대표 URL 선택
- `(building_id, asset_key)` primary key
- gallery 순서용 `(building_id, role_rank, first_position, asset_key)` index

`v_building_images_v2`는 이 table의 단순 projection이다. export 조회 때마다
전체 image occurrence에 window function을 다시 실행하지 않는다.

## 12. Redirect 후 Core Metadata

redirect되지 않은 active building은 v1.5의 primary와 core metadata를 그대로
보존한다.

manual merge target은 active member 전체를 기준으로 다시 계산한다.

- primary article: description 존재, content score, image count, tag count,
  낮은 article ID 순으로 선택
- display name과 description: 선택된 primary article 기준
- normalized name이 여러 개면 `core_conflicts_json.name`에 기록
- country/city: case-insensitive 단일 consensus만 채움
- project year: 서로 다른 non-null 값이 하나일 때만 채움
- area: 서로 다른 non-null 값이 하나일 때만 채움
- country/city/year/area가 충돌하면 scalar는 `NULL`로 abstain
- 충돌값은 `core_conflicts_json`에 보존
- core conflict가 있으면 `metadata_needs_review=1`

facets는 redirect 후 active membership으로 재집계한다. 따라서 program,
typology와 다른 facet arrays, scalar primary도 merge component 전체 근거를
반영한다.

redirect source building은 `building_attributes_v2.is_active=0`이고
`redirect_to`를 가진다. export에는 active target만 나타난다.

## 13. Export 계약

`v_divisare_buildings_export_v2`는 다음을 제공한다.

- terminal active building만 한 row
- `building_attributes_v2`에서 재계산한 primary와 core metadata
- 모든 active source article을 담은 `source_refs.divisare`
- active article 전체의 architect와 raw `source_categories`
- confirmed `programs`와 `typology_tags`
- cardinality 규칙을 지킨 호환용 `program`, `typology_primary`
- confirmed material, color, facade, element, context, intervention facets
- materialized asset-deduplicated gallery
- primary article의 cover와 description
- primary article kind 값과 상태
- `core_conflicts_json`, `facet_conflicts_json`, `needs_review`

candidate facet은 `v_search_facets_v2`에서 검토할 수 있지만 canonical export
배열에는 confirmed 값만 들어간다. unresolved article kind를 project로
암묵 변환하지 않는다.

다른 사이트와의 비교/통합 단계는 이 view와 raw provenance를 입력으로
사용한다.

## 14. Validation

schema 4 build는 현재 35개 automated check를 통과해야 발행된다. 핵심 검사는
다음과 같다.

- parent core table row count 보존
- claim evidence, article-kind resolution, article role, recrawl queue 완전성
- explicit unresolved가 project로 변환되지 않음
- confirmed가 아닌 article에 semantic role이 부여되지 않음
- active membership의 완전성과 article별 유일성
- materialized gallery의 `(building_id, asset_key)` 완전성
- redirect가 terminal이며 승인된 decision만 참조
- supporting confirmation이 independence key 2개와 article 2개를 모두 충족
- direct confirmation threshold `0.85` 준수
- article-kind confirmed에 HTML/manual authoritative evidence 존재
- program/typology array와 scalar cardinality 일관성
- active primary가 active membership에 포함
- core conflict scalar abstention
- redirect되지 않은 building의 core metadata 보존
- canonical array가 confirmed facet만 포함
- D2 auto-cluster, pending/deferred 분리, active export 일관성
- output SQLite integrity와 foreign key

## 15. Smoke 결과

외부 API와 LLM을 사용하지 않았으며 API 비용은 `$0`이다.

### N=10

- Parent:
  `data/curated/smoke/divisare_curated_n10_v1_5.db`
- Parent SHA-256:
  `0edc5efd0979489979751e9c4ec907b0f8a474a687141747fbabdef8691205e2`
- Output:
  `data/curated/smoke/divisare_metadata_n10_v2_1.db`
- Output SHA-256:
  `9754a31f3a092aa03246196a8129200faf8378d4ff8e07b556df86b829cefb31`
- Articles / active buildings: `10 / 10`
- Confirmed / candidate facets: `20 / 8`
- v1 confirmed facets downgraded: `0`
- Program / typology compatibility primary: `4 / 2`
- Article-kind status: `unresolved 8`, `candidate 2`
- Validation: `35 passed / 0 failed`
- Elapsed: `0.12s`

### N=100

- Parent:
  `data/curated/smoke/divisare_curated_n100_v1_5.db`
- Parent SHA-256:
  `4c3d6d24f1815fb03b40234124d913eb8bfa72bf5e41cb39a97be10959f29f58`
- Output:
  `data/curated/smoke/divisare_metadata_n100_v2_1.db`
- Output SHA-256:
  `44b14203b262ea94e0f4c78f58f0d5996523becd5a7811fb5a288636df043d3b`
- Articles / active buildings: `100 / 100`
- Confirmed / candidate facets: `224 / 66`
- v1 confirmed facets downgraded: `4`
- Program / typology compatibility primary: `46 / 35`
- Article-kind status: `unresolved 71`, `candidate 27`, `ambiguous 2`
- Validation: `35 passed / 0 failed`
- Elapsed: `0.20s`

### Full

- Parent: `data/curated/divisare_curated_v1_5.db`
- Parent SHA-256:
  `0939b15c55e6151e61be022893e1c86e6397455416bc1a113e3d0aa008277737`
- Output: `data/curated/divisare_metadata_v2_1.db`
- Output SHA-256:
  `8186f49eac8199e0a5cfbd671c952169646b8829840ba9b8b6f85c2244b9deca`
- Output size: `1,621,966,848 bytes`
- Articles / active buildings: `29,955 / 29,891`
- Confirmed / candidate facets: `93,425 / 28,285`
- v1 confirmed facets downgraded: `1,309`
- Program / typology compatibility primary: `16,599 / 15,228`
- Multi-program / multi-typology buildings: `842 / 1,182`
- Article-kind status:
  `unresolved 22,450`, `candidate 6,508`, `ambiguous 997`
- Article-kind values:
  `drawing_feature 4,434`, `photo_feature 698`, `model_feature 556`,
  `concept_editorial 820`, `mixed_feature 997`, `unresolved 22,450`
- D2 result: `66 confirmed / 220 pending / 0 redirects`
- Metadata recrawl queue: `29,955`
- Validation: `35 passed / 0 failed`
- Elapsed: `70.02s`
- API/LLM cost: `$0`
- Current combined regression suite after the v2.4 recrawler: `51 passed`
- Job card:
  `.claude/ops/jobs/20260728_divisare_metadata_v2_1.md`

## 16. 다음 단계

1. HTML recrawl sidecar에서 article template/DOM 근거와 core metadata를
   보강한다.
2. open D2 후보는 versioned manual decision을 통해서만 처리한다.
   현재 220건의 재감사 결과와 우선 검토 묶음은
   `docs/DIVISARE_D2_REVIEW_STATUS.md`에 기록했다.
3. 기존 auto cluster의 false positive를 발견하면 v1 재빌드 또는 별도 split
   policy를 먼저 확정한다.
4. Divisare metadata가 고정된 뒤 이미지 의미 판정, pHash, vector 단계를
   별도 artifact로 진행한다.
5. Divisare source-specific 검증이 끝난 뒤 다른 사이트별 가공과 cross-site
   비교를 시작한다.
