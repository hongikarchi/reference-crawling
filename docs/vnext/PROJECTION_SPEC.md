# vNext Neon projection specification v1

상태: WP0c 확정 명세. 이 문서는 DDL/loader 구현 기준이며 지금 Neon에 적용하지 않는다.

## 1. artifact

S6 `vnext_projection_v*`는 같은 manifest 아래 다음 두 rowset을 고정한다.

- `canonical_v2_buildings`: S1+S2+S4+S5에서 만든 parent
- `building_images`: S2+S3+S4에서 만든 ordered gallery child

태그 3형제는 현행 parent 컬럼인 `typology_tags`, `architectural_elements`,
`source_categories`를 뜻한다. D4에 따라 별도 Neon evidence/lineage/tag 테이블은 만들지
않는다. manifest에는 모든 입력 artifact SHA, builder/version, row count, ordered rowset SHA,
계약/DDL SHA를 넣는다.

## 2. parent field ownership

| 필드군 | source |
|---|---|
| serving ID, source_refs/URLs, identity_source, confidence_tier, n_sources | S1 |
| name/aliases/location/year/year_kind/architect linkage | S1 + S5 consolidation |
| program/style/color/atmosphere/material/description/형태 필드 | S5 text analysis |
| embedding | S5, publishable building 기준 384-dim |
| all_images, best_image_per_cluster | S2 canonical gallery |
| covers_by_type, cover_image_url_default, display_cover_url | S4 |
| typology_tags, architectural_elements, source_categories | S5/tag derivation, 같은 parent row |
| is_publishable, reasons, needs_image_derived_backfill | projection의 결정론 규칙 |

`canonical_v2_buildings`의 컬럼 모양은 바꾸지 않는다.

- S1 crosswalk가 명확한 1:1이면 legacy PK를 유지한다. 그 외에는 신규 serving ID다.
- `all_images`는 S2 representative URL을 `sort_order` 순서로 투영하며 occurrence 중복을
  넣지 않는다.
- `covers_by_type`은 5개 typed slot, `cover_image_url_default`와
  `display_cover_url`은 default selection에서 만든다. 선택 없음은 NULL로 남긴다.
- P6가 보류된 동안 legacy 1:1 행의 `image_derived`는 현재 값을 변경 없이 보존한다.
  신규/비1:1 행은 `{}`이며 `needs_image_derived_backfill`은 advisory다. publishability를
  막지 않는다.
- R2 재호스팅은 범위 밖이다. 선택 이미지와 정확히 대응하는 검증된 기존 CDN 값이
  없으면 `cover_image_cdn_url`/`cover_blurhash`를 임의 생성하거나 이월하지 않는다.

## 3. child projection

`building_images`는 gallery representative당 정확히 1행이다. source occurrence와
분류 evidence는 로컬 S2/S3에만 둔다.

- `sort_order`는 건물별 0부터 연속이다.
- local S3의 `not_applicable`은 SQL NULL로 투영하고 `unknown`은 그대로 보존한다.
- `semantic_type`과 `cover_slot`은 non-exclusive array다. 같은 asset을 슬롯마다 복제하지 않는다.
- default/typed cover flag는 S4 selection과 정확히 일치해야 한다.
- URL은 v1에서 검증된 source CDN URL이며 R2 rewrite를 하지 않는다.

실행 DDL은 `BUILDING_IMAGES_DDL.sql`과 byte SHA가 일치해야 한다.

## 4. validator

업로드 전 독립 validator가 최소한 다음을 검사한다.

- parent ID set = S1 active component set = child FK의 superset
- legacy 1:1 ID 보존 및 non-1:1 신규 ID 규칙
- parent별 child 수 = S2 gallery representative 수
- child PK/URL/sort_order 유일성, 0-based 연속 순서
- medium applicability와 semantic tag 재파생 일치
- S4 default/5 slot과 child flag의 양방향 일치, 슬롯별 최대 1개
- `all_images`, `covers_by_type`, default/display URL과 child projection 일치
- tag 3형제, embedding 차원, publishability 규칙
- parent/child rowset canonical SHA와 manifest SHA

validator FAIL이면 dry-run이나 Neon 쓰기로 넘어가지 않는다.

## 5. 배포와 롤백

1. S6 build + independent validation
2. read-only 현행 snapshot과 exact-set diff 산출
3. Neon dry-run transaction 후 ROLLBACK; insert/update/delete parent·child 수를 사용자에게 제시
4. 사용자 명시 승인 뒤 한 transaction에서 parent, building_images, 태그 3형제를
   exact-set으로 동기화하고 FK/row count를 재검사한 뒤 COMMIT
5. 최종 publishable building에서 `canonical_v2_architects`를 재유도·검증
6. make_web 벤치마크 10문항 PASS 확인

단순 upsert로 stale row를 남기지 않는다. exact-set delete도 dry-run diff와 사용자 승인을
받은 P7에서만 수행한다. 롤백은 동일 절차로 직전 S6 artifact를 다시 exact-set 적용한다.

Neon/R2 write, 스키마 적용, user_data/swipe migration은 WP0c에서 실행하지 않는다.
