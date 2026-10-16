# Architizer source-specific curated SQLite

이 문서는 `data/crawl/architizer.db`를 읽기 전용 원본으로 삼아 만드는
Architizer 전용 curated SQLite의 스키마, provenance, 보수적 정규화 정책과
재현 절차를 설명한다. 구현은 `canonical/architizer_curated.py`와
`tools/build_architizer_curated.py`가 기준이다.

이 산출물은 cross-site canonical DB가 아니다. Architizer project ID는 source
record ID이며 실제 건물 ID로 간주하지 않는다. 생성되는 building ID도
Architizer 내부 근거만 이용한 provisional ID다.

## 릴리스 경로

- 최종 DB: `data/curated/architizer_curated_v1_3.db`
- 최종 보고서: `data/reports/architizer_curated_v1_3.md`
- 입력 DB: `data/crawl/architizer.db`
- 입력 크기: `90,918,912 bytes`
- 입력 SHA-256:
  `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985`
- builder/schema/policy:
  `architizer-curated-builder-v1.6` /
  `architizer-curated-schema-v1.3` /
  `architizer-curation-policy-v1.4`
- taxonomy/asset/cluster/resolver/selection:
  `architizer-article-tag-taxonomy-v1.1` /
  `architizer-host-path-asset-v1` /
  `architizer-strict-internal-cluster-v2` /
  `architizer-facet-resolver-v1.0` /
  `architizer-deterministic-subset-v1.2`

### 최종 full 결과

- Build ID: `atz_build_b87cf5bf46f2f98628dbd96c`
- Output SHA-256:
  `5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089`
- Logical SHA-256:
  `D9DD477FB748AB0C54FC2C7E095B5A19DA5AD4D07B75CFB3D8C8C1857F2BD7AF`
- 크기 / 소요시간: `687,579,136 bytes` / `272.416s`
- 결정성 shadow rebuild: byte SHA-256 동일, `PASS`
- project: 전체 `10,632`, accepted `10,628`, excluded `4`
- provisional building: `10,569`; strict cluster `49`개에 project `108`개
- firm: 전체 `6,870` (`crawled=2,802`, `project_stub=1,951`,
  `award_stub=2,117`)
- category: raw vocabulary `78`, occurrence `28,142`,
  `mapped=77`, `unmapped=1`; taxonomy claim은
  `confirmed=26,475`, `candidate=18,519`, `unmapped=192`
- location: city claim `candidate=7,825`, `review=293`;
  country claim `candidate=8,118`
- image: raw occurrence `193,696`, asset/raw URL `172,283`,
  global-ID occurrence `479,583`, placeholder occurrence `710`
- award: raw row `13,978`, logical-duplicate raw row `2,992`,
  unresolved/stub link `9,795`
- duplicate pair: strict auto-clustered `79`, exact review `280`,
  fuzzy review `16`
- open QA: `13,964`건 / `23` issue codes
- application table/view: `30` / `10`

## 원본 read-only audit 기준선

Builder는 입력을 SQLite URI `mode=ro&immutable=1`로 열고
`PRAGMA query_only=ON`을 강제한다. 빌드 전후 SHA-256이 다르면 실패한다.
네트워크, LLM, Vision, embedding, Neon, R2 호출은 없다.

### 원본 schema와 key/index 의미

원본에는 사용자 table 5개, view 0개, 명시적 index 7개가 있다.
SQLite가 PK/UNIQUE를 위해 만든 auto-index는 6개다.

| 원본 table | PK·UNIQUE | column |
|---|---|---|
| `architizer_projects` | `id` PK; `global_id`, `slug` 각각 UNIQUE | `id`, `global_id`, `slug`, `name`, `firm_slug`, `firm_name`, `description`, `description_short`, `completion_year`, `building_size_slug`, `building_size_display`, `constr_status`, `budget`, `location_full`, `location_country`, `location_city`, `categories`, `cover_image_url`, `gallery_image_urls`, `image_global_ids`, `published_time`, `modified_time`, `fetched_at` |
| `architizer_firms` | `slug` PK | `slug`, `name`, `office_locations`, `description`, `awards_summary`, `project_count_seen`, `social_links`, `fetched_at` |
| `architizer_awards` | `id` INTEGER PK; 아래 6개 column의 복합 UNIQUE | `id`, `award_year`, `award_track`, `award_category`, `award_tier`, `project_slug`, `firm_slug`, `source_url`, `fetched_at` |
| `pending_projects` | `url` PK | `url`, `source_url`, `lastmod`, `status`, `discovered_at`, `fetched_at`, `error` |
| `pending_firms` | `url` PK | `url`, `source_url`, `lastmod`, `status`, `discovered_at`, `fetched_at`, `error` |

명시적 index는 project의 `firm_slug`, `location_country`,
`completion_year`, award의 `project_slug`, `firm_slug`, 두 queue의
`status`에 있다. 선언된 FK는 없다. Award 복합 UNIQUE는
`award_year, award_track, award_category, award_tier, project_slug,
firm_slug` 순서인데, SQLite에서 NULL끼리는 같다고 보지 않으므로 NULL이
포함된 논리 중복을 막지 못한다.

### 행 수, coverage와 품질

- `quick_check=ok`, `integrity_check=ok`, `foreign_key_check=0`
- 원본에는 선언된 foreign key가 없으므로 관계 무결성은 curated builder가
  별도로 검증한다.
- projects `10,632`, firms `2,802`, awards `13,978`
- pending projects `10,636`: done `10,633`, failed `3`
- pending firms `2,802`: 전부 done
- done queue지만 project row가 없는 URL `1`건
- project `global_id`가 `projects.project.<id>`가 아닌 행 `4`건
- category JSON은 전부 유효하며 `28,142` occurrences / `78` raw values
- gallery URL `183,064` occurrences; exact default-image-only source pattern은
  `257`건이고, 정책상 usable image가 없는 project는 `265`건
- image attribution global ID `479,583` occurrences. gallery 배열과 길이가
  같은 project는 `34`개뿐이므로 두 배열에는 positional join 근거가 없다.
- raw firm table에 연결되지 않는 project firm slug `5,851`건
- 2024 award는 `1,496` logical keys가 각각 두 번 수집되어 `2,992` raw
  rows를 이룬다.
- project coverage: name·firm slug·cover `10,632`(100%),
  firm name `10,629`, country/city `8,118`(76.35%),
  completion year `9,093`(85.52%), size bucket `8,068`(75.88%),
  full description `9,820`(92.36%), short description `10,632`(100%)
- construction status: `built=8,166`, `concept=1,534`,
  `under-construction=927`, missing/blank `5`
- source year 범위는 `1887..2588`이고 `2026` 초과가 `300`건이다.
  concept/under-construction 미래 연도는 candidate로 둘 수 있지만,
  `2588` 같은 값은 review/NULL이다.
- size는 exact area가 아니라 sqft bucket이다. Budget은 반복되는
  sentinel-like 값을 포함해 통화·단위·의미를 확인할 수 없었다.
- category/gallery/global-ID JSON malformed `0`, URL parse malformed `0`.
  Builder는 향후 malformed/non-list JSON도 exact source text와 한 개 이상의
  occurrence로 보존하고 QA를 남긴다.
  full description 중앙 길이는 약 `1,858`자이고 short description은
  대부분 `160`자 cap을 보인다. mojibake 가능 text는 `4`건이다.
- cover `10,632`와 gallery `183,064`를 합친 raw image occurrence는
  `193,696`이다. Cover는 모두 gallery에도 있으며, 정규화 asset은
  `172,283`, gallery 내부 추가 중복 occurrence는 `10,511`이다.
- award table에는 image URL/global-ID column이 없으므로 award와
  project/gallery image 사이의 관계를 만들거나 추정하지 않는다.
- crawler firm row가 없는 project firm은 `5,851`행 /
  `1,951` distinct slug다. Award project link `6,055`건은 unresolved,
  award firm link `3,740`건은 crawled firm 대신 stub으로 남는다.
- raw strict-key audit는 52 groups / 114 rows였다. Generic/phase와
  entity-mismatch를 제외한 최종 strict policy는 49 clusters / 108
  projects만 자동 결합하며 나머지는 exact/fuzzy review로 남긴다.
- credit/team 상세 payload와 URL별 image attribution payload는 원본
  schema에 없다. 이 의미는 복원하거나 추측하지 않고 open QA로 둔다.

원본의 비정상·불완전 상태는 삭제하지 않는다. raw row/occurrence를 먼저
보존하고 acceptance, link, claim, QA 상태로 구분한다.

## 스키마

모든 curated table은 `STRICT`이고 foreign key를 활성화한다. JSON column은
가능한 곳에서 `json_valid()` CHECK를 사용한다.

### Lineage와 원본 entity

| Table | 역할 |
|---|---|
| `build_runs` | build ID, 모든 정책 버전, 입력 SHA/크기, subset limit, 검증 JSON |
| `source_snapshots` | 안정화된 입력 label, 빌드 전후 SHA, WAL/journal=0, integrity/FK/query-only 결과 |
| `source_queue_summary` | pending project/firm의 status별 count |
| `source_firms` | crawled firm과 project/award에서 만든 provenance stub. `record_origin`으로 구분 |
| `firm_office_occurrences` | office JSON의 ordinal별 raw occurrence와 parse 상태 |
| `firm_social_links` | platform key, raw URL, source field 보존 |
| `source_projects` | 원본 project scalar, exact JSON source text, 유효한 보존 JSON, acceptance/exclusion, occurrence count |
| `project_firms` | `article:author` 기반 source-primary-author 관계 |
| `project_text_versions` | full description과 OG short description, text hash와 품질 상태 |
| `source_awards` | award raw row 전체와 logical duplicate group size |
| `award_entity_links` | award slug의 project/firm resolution 또는 stub/unresolved 상태 |

`source_projects`의 PK는 `source_project_id`이고 `global_id`, `slug`,
`source_url`은 각각 unique다. 잘못된 entity type의 project도 raw row로
남지만 `acceptance_status='excluded'`와 명시적 exclusion reason을 갖는다.

### Category와 claim

| Table | 역할 |
|---|---|
| `source_categories` | raw `article:tag` vocabulary, occurrence/project count, mapping 상태 |
| `project_category_occurrences` | project별 tag 순서와 raw value |
| `category_mappings` | axis/value, rule, confidence, evidence, status |
| `attribute_claims` | category와 structured field에서 나온 project/building claim 전체 |
| `building_facets` | building별 resolved value와 confirmed/candidate/conflict/review 상태 |
| `building_facet_claims` | facet에서 원 claim으로 돌아가는 provenance join |

### Image

| Table | 역할 |
|---|---|
| `image_assets` | 보수적으로 정규화한 source asset identity |
| `image_urls` | raw URL과 asset의 다대일 관계 |
| `source_image_occurrences` | project/role/ordinal별 cover·gallery occurrence |
| `project_image_global_id_occurrences` | attribution global ID 원순서. URL alignment는 항상 unresolved |
| `image_work_queue` | 향후 pHash/classification용 pending queue. 현재 network call은 항상 0 |

### Building, duplicate, QA

| Table | 역할 |
|---|---|
| `duplicate_candidates` | strict/exact/fuzzy 후보, score breakdown, evidence, decision |
| `buildings` | singleton 또는 strict cluster로 만든 provisional building |
| `building_projects` | building membership와 primary project, rule/evidence |
| `cluster_events` | singleton/strict cluster 생성 이력 |
| `project_completeness` | firm/location/year/description/category/image 6개 신호 |
| `building_completeness` | primary project 기준 score와 missing fields |
| `qa_issues` | entity별 issue code, severity, open/resolved/ignored 상태 |
| `build_metrics` | 보고서와 검증에 쓰는 deterministic metric JSON |

## Views

| View | 용도 |
|---|---|
| `v_project_category_provenance` | raw tag occurrence → mapping/rule/evidence 추적 |
| `v_building_project_provenance` | building → source project/membership 근거 추적 |
| `v_building_images` | building별 asset dedup와 occurrence count |
| `v_search_facets` | confirmed/candidate/conflict/review facet |
| `v_duplicate_review_queue` | auto-cluster되지 않은 exact/fuzzy 후보 |
| `v_unmapped_categories` | unmapped/review raw category |
| `v_qa_open` | 열린 QA |
| `v_image_hash_queue` | 향후 pHash 대상 |
| `v_image_classification_queue` | 향후 image classification 대상 |
| `v_architizer_buildings_export` | downstream용 1 building = 1 row export |

Export의 `completion_year`는 `year_status='confirmed'`일 때만 값이 있다.
`program_primary`, `typology_primary`, `area_bucket`, `project_status`는
confirmed value가 정확히 하나일 때만 값이 있고, 복수 confirmed value가
충돌하면 `NULL`이다. `program_tags_json`, `typology_tags_json`,
`work_type_tags_json`과 `taxonomy_status`가 충돌 및 다중 값을 보존한다.
`cover_image_url`과 `image_urls_json`은 malformed/placeholder candidate를
제외한다. candidate/review facet 전체는 `v_search_facets`에서 조회한다.

## Provenance 원칙

- project, firm, award, text, category, image URL과 image global ID의 raw
  occurrence를 정규화 결과보다 먼저 저장한다.
- JSON-list source field는 exact source text와 유효한 정규화 JSON을
  분리해 저장하므로 malformed container도 원문을 잃지 않는다.
- 모든 normalized claim은 `rule_id`, confidence, status, evidence ref/JSON,
  policy version을 가진다.
- firm 또는 award target이 현재 crawled corpus에 없어도 stub 또는
  unresolved link로 남긴다.
- 2024 award logical duplicate를 물리적으로 삭제하지 않는다.
  `source_composite_key`와 `logical_duplicate_group_size`로 식별한다.
- 원본에는 credit/team detail과 image attribution payload table이 없다.
  global ID만으로 사람·회사 credit을 추정하지 않는다.

## 정책

### Category와 facet

- 입력 category는 순서가 있는 `article:tag` occurrence다.
- source taxonomy는 9개 broad parent와 69개 leaf를 명시적으로 관리한다.
  `Other`, blank, 새로 등장한 unknown tag는 raw occurrence를 보존한 채
  unmapped로 둔다.
- broad parent는 supporting candidate(`0.68`)이며 scalar default를 만들지
  않는다.
- `Bicycles`, `Bus`, `Nursery`는 의미가 모호한 leaf라 candidate(`0.72`)다.
- 검토된 direct leaf는 confirmed(`0.95`) claim을 만들 수 있다.
- expected parent가 없는 leaf는 candidate로 낮추고 QA를 남긴다.
- tag가 10개 초과이거나 broad parent가 4개 이상이면 overloaded QA다.
  soft/severe 여부와 무관하게 overloaded row의 direct scalar claim은
  candidate로 격리하며 confirmed category claim을 만들지 않는다.
- parent와 leaf는 같은 category-path evidence group으로 묶여 독립 근거
  두 개로 세지 않는다.
- material은 source evidence가 없으므로 절대 추론하지 않는다.
- scalar axis에 confirmed value가 여러 개면 모두 `conflict`로 보존하고
  primary value를 선택하지 않는다.

### Location

- `location_full`, crawler가 분리한 city/country raw value를 모두 보존한다.
- comma-separated header의 마지막 token과 일치하는 country도 source
  header만으로 의미가 완전히 검증되지 않으므로 candidate다.
- 첫 token과 일치하는 city는 의미가 완전히 검증되지 않아 candidate다.
- header shape 불일치, parser mismatch, 불완전한 country token, 문자 없는
  city token, 두 글자 대문자 행정구역 약어는 normalized 값을 만들지 않고
  review/QA로 둔다.

### Year와 construction status

- 정밀도는 year뿐이다.
- built이면서 `1800..2026`인 연도만 confirmed다.
- concept/under-construction의 `1800..2036` 연도는 candidate다.
- built의 미래 연도, 1800 미만, 2036 초과, 비정상 형식은 raw 값만
  보존하고 review한다. 예를 들어 source의 `2588`은 확정하지 않는다.
- status가 미검토 값이면 review, 값이 없으면 missing이다.

### Size

- `building_size_slug/display`는 정확 면적이 아니라 sqft range다.
- 알려진 10개 slug를 min/max sqft로 해석하되 exact area claim은 만들지
  않는다.
- slug와 display bounds가 일치하면 confirmed, display만 파싱되면
  candidate, 서로 다르거나 파싱 불가하면 review다.
- `sqft_1000`은 1,000,000 sqft 이상인 open-ended range다.

### Image

- raw cover와 gallery occurrence는 role/ordinal을 포함해 모두 보존한다.
- 허용 source host는 `architizer-prod.imgix.net`과
  `static-web-prod.arc.ht`다.
- asset identity는 host/path와 알 수 없는 identity query를 사용한다.
  알려진 Imgix transform query만 제거한다.
- 같은 URL의 반복 occurrence와 동일 asset은 서로 다른 개념이다.
- default/social/placeholder path는 placeholder candidate로 표시하고
  실제 건축 이미지로 확정하지 않는다.
- `image_global_ids`는 DOM 전체에서 별도로 수집된 값이다. gallery URL과
  길이·순서 정렬 근거가 없으므로 항상 `alignment_status='unresolved'`다.
- article/category tag를 gallery 전체 image type으로 전파하지 않는다.
- 다운로드, pHash, Vision, image classification은 이 단계에서 실행하지
  않고 queue만 만든다.

### Project acceptance와 duplicate

- `global_id == projects.project.<id>`인 source row만 provisional building
  membership 대상이다. 다른 4개 row는 raw 보존 후 entity-type mismatch로
  제외한다.
- 자동 cluster는 normalized exact name, 같은 stable firm slug, 같은
  country/city, 같은 non-null year가 모두 필요하다.
- generic name 또는 phase/stage/extension marker가 있으면 자동 merge하지
  않는다. `House A`, `Office B`, `Pavilion II` 같은 짧은 alpha/Roman
  suffix도 generic으로 막으며 phase marker는 name과 slug를 모두 본다.
- exact name이지만 강한 결합 근거가 부족한 후보는 review queue에 둔다.
- fuzzy candidate는 같은 firm/location/year block 안에서 name similarity
  `>=0.88`인 pair만 만들며 항상 review다.
- pHash와 Vision은 merge 근거로 사용하지 않는다. 모든 후보에 score
  breakdown과 양쪽 source row evidence를 저장한다.

## 빌드와 검증

Builder는 임시 DB를 완성하고 검증한 뒤 최종 경로에 hard-link로
publish한다. 기존 output/report를 덮어쓰지 않는다. 아래 명령은 대상 경로가
존재하지 않는 clean run에서 실행해야 한다. 재실행할 때는 새 version suffix를
사용한다.

SQLite `3.37+`가 필요하다. 입력은 non-zero WAL/journal이 있으면 immutable
read를 거부하고, output의 기존 WAL/SHM/journal namespace도 충돌로 거부한다.
현재 입력의 WAL/journal은 0이고 read-only SHM은 32,768 bytes다. SHM 크기는
호스트별 runtime 상태이므로 deterministic DB lineage payload에는 넣지 않고
build report에만 기록한다.

먼저 정책·SQLite integration test를 실행한다.

```powershell
python -m pytest tests/test_architizer_curated.py -q
```

N10:

```powershell
python tools/build_architizer_curated.py `
  --source-db data/crawl/architizer.db `
  --limit 10 `
  --output-db data/curated/smoke/architizer_curated_n10_v1_r9.db `
  --report data/reports/smoke/architizer_curated_n10_v1_r9.md `
  --verify-deterministic
```

N100은 N10이 통과한 뒤 실행한다.

```powershell
python tools/build_architizer_curated.py `
  --source-db data/crawl/architizer.db `
  --limit 100 `
  --output-db data/curated/smoke/architizer_curated_n100_v1_r3.db `
  --report data/reports/smoke/architizer_curated_n100_v1_r3.md `
  --verify-deterministic
```

Full은 N100이 통과한 뒤 실행한다. CLI default 경로와 무관하게 release
경로를 명시한다.

```powershell
python tools/build_architizer_curated.py `
  --source-db data/crawl/architizer.db `
  --output-db data/curated/architizer_curated_v1_3.db `
  --report data/reports/architizer_curated_v1_3.md `
  --verify-deterministic
```

`--verify-deterministic`는 두 번째 temporary DB를 만들고 byte SHA-256까지
동일한지 확인한다. 각 단계는 다음 조건을 자동 검증한다.

- output `integrity_check=ok`, FK violation 0
- 입력 SHA가 빌드 전후 동일
- source project/category/image/global-ID/award occurrence 회계 일치
- category master occurrence/project count가 실제 occurrence와 일치
- accepted project는 정확히 하나의 provisional membership 보유
- excluded project는 membership 0
- building마다 primary project 정확히 하나
- parsed image에는 asset과 URL identity 존재
- confirmed claim/facet에는 evidence 존재
- fuzzy review candidate의 auto merge 0
- category overload에서 confirmed scalar claim 0
- scalar conflict가 export primary로 새지 않음
- material claim 0
- export building ID unique 및 building row와 1:1

## 알려진 제한사항

- source snapshot에는 project crawl 실패 3건과 done queue/row mismatch 1건이
  있다.
- firm corpus는 project의 모든 firm을 포함하지 않는다. stub은 provenance를
  보존하지만 crawled firm profile과 동일한 정보량을 갖지 않는다.
- firm office location은 현재 전부 빈 배열이다.
- firm `project_count_seen`, social platform key, awards summary는 heuristic
  parser 결과이며 일부 기본 계정·UI 문구가 섞여 있다.
- award corpus는 project/firm corpus보다 넓어 unresolved/stub link가 많고,
  `award_category` ancestor-text parser는 taxonomy로 신뢰할 수 없다.
- `description_short`는 대부분 160자 OG snippet이므로 full description을
  대체하지 않는다.
- `budget`에는 0과 음수 sentinel-like 값이 많고 currency/unit 의미가
  검증되지 않아 normalized claim을 만들지 않는다.
- usable image가 없는 project `265`건의 publishability는 미결정이다.
- pHash/image classification, credit/team 복원, external source matching은
  후속 Architizer-only 검토 범위다.
- 이 DB는 production canonical rebuild 또는 production upload를 수행하지
  않는다.
