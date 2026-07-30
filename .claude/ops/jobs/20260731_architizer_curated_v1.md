# Architizer curated SQLite v1

## 목표와 범위

- `data/crawl/architizer.db`를 read-only source of truth로 사용해
  Architizer 전용 curated SQLite를 구축한다.
- project/firm/award/category/text/image occurrence provenance를 먼저
  보존하고, 검토된 category만 typed claim과 provisional building으로
  투영한다.
- Divisare, cross-site matching, production canonical, D1/D2/E2, pHash,
  embedding, Neon/R2는 범위 밖이다.

## 입력 manifest

- 입력: `data/crawl/architizer.db`
- 크기: `90,918,912 bytes`
- SHA-256:
  `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985`
- 연결: SQLite `mode=ro&immutable=1`, `PRAGMA query_only=ON`
- build 전후 입력 SHA가 다르면 publish 전에 실패한다.

## Read-only audit

- `quick_check=ok`, `integrity_check=ok`, `foreign_key_check=0`
- source rows:
  - projects `10,632`
  - firms `2,802`
  - awards `13,978`
  - pending_projects `10,636` (`done=10,633`, `failed=3`)
  - pending_firms `2,802` (`done=2,802`)
- project category는 유효한 JSON이며 78종(9 parent + 69 leaf),
  occurrence `28,142`, 한 project 내부 중복 0이다.
- category 배열은 `article:tag` 순서이며 award category와 별개다.
  parent와 leaf는 같은 category-path 근거로 취급한다.
- 일부 project는 category가 최대 73개여서 metadata 과수집 가능성이
  있다. 과부하 행은 raw로 보존하되 confirmed category claim을 만들지
  않는다.
- completion date는 연도 정밀도, size는 sqft bucket이다. budget 의미,
  award payload 파싱, gallery URL과 image global ID의 위치 대응은
  source만으로 확정할 수 없다.
- source SQLite에는 선언된 FK가 없어 curated 단계에서 관계 회계를
  별도로 검증한다.

## 구현

- 정책: `canonical/architizer_curated.py`
  - schema/policy/taxonomy:
    `v1.3` / `v1.4` / `v1.1`
  - 78개 raw category를 닫힌 inventory로 관리하며 unknown과 `Other`는
    명시적으로 unmapped 처리한다.
  - broad parent는 candidate, 검토된 leaf는 direct 또는 supporting
    program/typology/work-type claim으로 만든다.
  - category-only 근거로 material이나 image class를 추론하지 않는다.
- builder: `tools/build_architizer_curated.py`
  - builder `v1.6`, deterministic subset `v1.2`
  - build lineage/input SHA, raw source entities와 occurrence, category
    mapping/claim, image asset/URL occurrence, award link, duplicate review,
    provisional building/facet, completeness, QA/metric 테이블과 export/
    provenance/queue view를 생성한다.
  - project ID를 real building ID로 간주하지 않는다. 자동 cluster는
    exact normalized name + stable firm slug + country + city + 동일한
    non-null year + non-generic/non-phase 조건을 모두 만족할 때만 허용한다.
    나머지 exact/fuzzy 후보는 review queue에 남긴다.
  - scalar direct 값이 충돌하면 우선순위로 고르지 않고 export scalar를
    `NULL`로 둔다.
  - output DB와 report를 같은 파일시스템의 임시 파일에서 완성·검증한
    뒤 hard-link no-clobber 방식으로 함께 publish한다. 기존 경로와
    동시 생성 파일은 덮어쓰지 않는다.

## Smoke ladder

| 단계 | 최신 통과 artifact | 결과 |
|---|---|---|
| N10 | `data/curated/smoke/architizer_curated_n10_v1_r9.db` / `data/reports/smoke/architizer_curated_n10_v1_r9.md` | 10 projects, 10 accepted, 10 buildings, validation PASS, byte rerun PASS, 1.807s |
| N100 | `data/curated/smoke/architizer_curated_n100_v1_r3.db` / `data/reports/smoke/architizer_curated_n100_v1_r3.md` | 100 projects, 98 accepted, 97 buildings, strict cluster 1, validation PASS, byte rerun PASS, 2.804s |
| full | `data/curated/architizer_curated_v1_3.db` / `data/reports/architizer_curated_v1_3.md` | 10,632 projects, 10,628 accepted, 10,569 buildings, strict clusters 49, validation PASS, byte rerun PASS, 272.416s |

N10/N100은 category 과부하, non-project global ID, placeholder image,
strict duplicate, scalar conflict, missing location/year/firm을 포함하는
deterministic edge-case subset이다.

## 최종 산출물

- DB: `data/curated/architizer_curated_v1_3.db`
- report: `data/reports/architizer_curated_v1_3.md`
- output SHA-256:
  `5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089`
- logical SHA-256:
  `D9DD477FB748AB0C54FC2C7E095B5A19DA5AD4D07B75CFB3D8C8C1857F2BD7AF`
- bytes: `687,579,136`
- projects/buildings/firms/categories/claims/images/awards/QA count:
  `10,632 / 10,569 / 6,870 / 78 / 45,186 taxonomy claims /
  193,696 image occurrences / 13,978 / 13,964 open QA`
- taxonomy claims: confirmed `26,475`, candidate `18,519`, unmapped `192`
- duplicate pairs: strict auto-clustered `79`, exact review `280`,
  fuzzy review `16`
- strict clusters: `49` buildings / `108` source projects
- no-usable-image projects: `265`; placeholder occurrences: `710`
- elapsed: `272.416s`
- DB/report/WAL/SHM/log는 Git에 추가하지 않는다.

첫 `v1_2` full 호출은 실행기의 10초 대기 제한으로 publish 전에 중단되어
staging lock/temp만 남았다. 저장소의 삭제 확인 규칙에 따라 이를 지우지
않고, 충돌 없는 immutable `v1_3` 경로에서 clean full build를 완료했다.

## 검증과 테스트

- source SHA before/after 동일, output `integrity_check=ok`, FK violation 0
- source project accepted/excluded accounting, accepted membership 정확히 1,
  excluded membership 0
- raw category/image/global-ID/award occurrence 회계 일치
- unmapped category occurrence의 명시적 policy 존재
- confirmed claim/facet의 evidence link 존재, material 무근거 claim 0
- strict auto-cluster rule 위반 0, fuzzy/review candidate auto-merge 0
- export building ID unique, primary/member count 일치
- scalar conflict의 export primary 값 0, category→image 전파 0
- deterministic byte rerun과 immutable no-clobber 검증
- `tests/test_architizer_curated.py` unit + 실제 SQLite integration 결과:
  full artifact 검증 포함 `17 passed`
- 전체 UTF-8 pytest: `160 passed, 1 deselected, 6 subtests passed`.
  제외한 1건은 기존
  `DivisareV2BuilderTests.test_unapproved_merge_decision_is_rejected`가
  Windows/Python 3.14에서 예외 뒤 임시 `parent_v1_5.db` 핸들을 닫지 못하는
  teardown 실패다. 전체 실행에서는 `160 passed, 1 failed`로도 재현했으며,
  이번 범위 규칙에 따라 Divisare 코드는 수정하지 않았다.

## Open QA

- 3개 failed crawl queue 항목
- done queue인데 project row가 없는 URL `1`
- category metadata 과부하 `145`, parent-only category `15`
- scalar conflict `2,891`, `Other`/unmapped raw category occurrence `192`
- nullable award unique semantics, award category parser, unresolved project/
  firm award link (`project unresolved=6,055`, `firm stub-only=3,740`)
- project firm이 crawled firm index에 없는 source stub
- firm social link 및 project_count_seen의 heuristic 성격
- credit/team source field 부재
- image global ID 정렬 불명, placeholder occurrence `710`,
  usable image가 없는 project `265`, 향후 asset/pHash/classification
- suspicious city `293`, completion-year review `8`,
  missing construction status `5`
- budget 의미 미확정, description source cap, 잠재 mojibake `4`

## 비용

- network/API/LLM/Vision/embedding/Neon/R2 호출: `0`
- 외부 비용: `$0`
