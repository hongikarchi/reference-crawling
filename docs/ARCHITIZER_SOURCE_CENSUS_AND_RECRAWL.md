# Architizer source census 및 recrawl v2

기준일은 2026-07-31이다. 이 문서는 immutable curated v1.3 이후 수행한
Architizer 원천 범위 조사, legacy crawler 감사, sidecar recrawl 검증을
기록한다.

## 판정

- commit `6f80cf2`의 curated v1.3은 2026-04-28 raw snapshot을 재가공한
  fixed-snapshot 산출물로서 완료 상태다.
- curated v1.3은 Architizer 원천 전체 수집 완료를 뜻하지 않는다.
- legacy `architizer.db`의 10,632 project row와 10,636 queue URL은 당시
  약 1년 sitemap window의 snapshot이다. 전체 corpus로 볼 수 없다.
- 현재 공식 sitemap도 전체 corpus가 아니라 최근 publish/modify URL을 담는
  약 1년 rolling window일 가능성이 높다.
- 그러므로 현재 sitemap에서 사라진 legacy URL을 삭제하거나 tombstone으로
  해석하지 않는다.
- 신규·변경·복구·관계 seed를 대상으로 한 sidecar recrawl이 필요하다.
  전체 network refresh는 이 문서의 preview 수치에 대한 별도 사용자 승인을
  받기 전에는 실행하지 않는다.

보존 기준:

| Artifact | SHA-256 |
|---|---|
| `data/crawl/architizer.db` | `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985` |
| `data/curated/architizer_curated_v1_3.db` | `5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089` |

두 파일의 SHA는 census, N10, N100 전후 동일했다. curated v1.3 DB와 report는
재생성하거나 덮어쓰지 않았다.

## 공식 sitemap census

`https://architizer.com/sitemap.xml`과 그 index에 실제 등록된 project 12개,
firm 3개 child sitemap만 요청했다. 임의의 `?p=1..N` 범위는 만들지 않았다.
검증된 소형 manifest는
`docs/manifests/architizer_source_census_20260731.json`이다.

| 항목 | Project | Firm |
|---|---:|---:|
| Legacy queue | 10,636 | 2,802 |
| 현재 distinct URL | 11,303 | 2,545 |
| 현재 entry occurrence | 11,325 | 2,545 |
| Legacy/current overlap | 7,992 | 1,807 |
| 현재에만 있는 URL | 3,311 | 738 |
| Legacy에만 있는 URL | 2,644 | 995 |
| Overlap 중 lastmod 변경 | 535 | 66 |
| 현재 URL이나 entity row 없음 | 3,314 | 738 |
| 현재 lastmod 최소 | 2025-08-01 | 2025-07-31 |
| 현재 lastmod 최대 | 2026-07-29 | 2026-07-28 |

Project sitemap은 page boundary에서 같은 URL 22개가 중복됐다. 중복 URL의
lastmod는 서로 같았다. Firm 중복은 없었다.

현재에만 있는 project 3,311개는 “legacy snapshot 대비 first-seen”이다.
생성일을 의미하지 않는다. 현재 lastmod 기준 분해는 다음과 같다.

- 2026-04-28 이후: 3,135
- 2026-04-28 당일: 87
- 2026-04-28 이전: 89

Overlap의 lastmod 변경 project 535개와 firm 66개는 모두 현재 값이 더
최근이었다. lastmod가 어떤 source field 변경을 뜻하는지는 공식 설명이 없어
open QA다.

### Rolling window 근거

- project 최소 lastmod는 관측일 약 364일 전이다.
- firm 최소 lastmod는 관측일 정확히 365일 전이다.
- legacy sitemap도 project와 firm 모두 약 1년 범위를 담았다.
- 공식 프로필의 `55,000+ projects / 14,000+ firms` 문구는 sitemap URL
  수보다 훨씬 크다. 이 문구는 비전체성의 보조 근거일 뿐 최신 cardinality로
  사용하지 않는다.

따라서 legacy에만 있는 project 2,644개와 firm 995개는 삭제 후보가 아니다.
window 밖 이동, 숨김, 실제 삭제를 sitemap만으로 구분할 수 없다.

## Legacy DB 및 award census

### Recovery 대상

Legacy project queue는 done 10,633, failed 3이다. project row는 10,632개다.

| 상태 | URL | 판정 |
|---|---|---|
| failed | `/projects/gryphons-honour-wall/` | 최신 페이지에서 복구 가능 |
| failed | `/projects/the-butterfly/` | 최신 페이지에서 복구 가능 |
| failed | `/projects/waterloo-park-wayfinding/` | 최신 페이지에서 복구 가능 |
| done-row-mismatch | `/projects/requiem-for-ruins-2/` | project가 아니며 현재 firm으로 이동 |

마지막 URL은 현재
`/firms/multitude-of-sins/?notfound_project=1`로 이동하고 global ID도
`firms.firm.183312`다. project row로 복구하지 않고 known identity redirect로
보존한다.

Legacy project가 참조하는 distinct firm slug는 3,450개이고, 그중 firm
table에 없는 slug는 1,951개다. 5,851 project row가 이 stub을 참조한다.

Legacy awards는 13,978 rows, 2013–2025 범위이며 2026이 없다.

- unresolved project: 4,983 slugs / 6,055 rows
- unresolved firm: 2,835 slugs / 6,232 rows
- award에만 있는 firm seed: 2,117 slugs
- 공식 2026 gallery에서 발견한 direct seed: project 725, firm 722
- 2026 공식 track: `Firm`, `Plus`, `Products`, `Sustainability`, `Typology`

Award 링크는 discovery seed일 뿐 정상 entity 존재를 보장하지 않는다.
실제로 N100에서 firm seed 하나가 공식 `?notfound=1`로 종료됐다.

## Legacy crawler 감사

| Severity | 문제 | 영향 |
|---|---|---|
| HIGH | 공식 index가 아니라 고정 page range 순회 | shard 추가·제거와 현재 등록 범위를 놓침 |
| HIGH | queue discovery가 `INSERT OR IGNORE` | 기존 URL의 lastmod와 done 상태가 갱신되지 않음 |
| HIGH | done lastmod 변경 재예약 없음 | 수정 project를 다시 받지 않음 |
| HIGH | failed가 terminal이며 retry selector 없음 | 일시적 parser 실패를 복구하지 못함 |
| HIGH | raw HTML/embedded JSON snapshot 없음 | parser 변경 때 재다운로드 필요 |
| HIGH | final URL/content type/abnormal 200 검사 없음 | login, block, soft-404, wrong entity 저장 가능 |
| HIGH | project identity cross-check 없음 | 실제 DB에 article/firm global ID 4건 혼입 |
| HIGH | upsert가 sparse parse의 NULL로 기존 값 clobber 가능 | 기존 정상 metadata 손실 가능 |
| HIGH | award 연도와 track 하드코딩 | 2026, `Firm`, `Sustainability` 등 누락 |
| HIGH | project/firm/award discovery 단절 | firm stub과 award-only slug가 후속 queue로 이어지지 않음 |
| MEDIUM | atomic claim/second-process lock 없음 | 중복 처리와 상태 경합 가능 |
| MEDIUM | circuit breaker 없음, retry pacing 불완전 | 차단 상태에서 요청 지속 가능 |
| MEDIUM | parser가 제한된 single-quoted JSON regex와 H2 heuristic에 의존 | 최신 DOM 변화에 취약 |

Legacy DB의 identity contamination은 다음 global ID prefix로 확인됐다.
`articles.article` 1건, `firms.firm` 3건이다. sidecar parser는 URL slug,
canonical, embedded slug, entity별 numeric global ID, project PK 일치를 함께
검증한다.

## Recrawl v2 구조

Legacy DB를 수정하지 않고 다음 runtime sidecar를 사용한다.

- `data/enrichment/architizer_source_recrawl_v2.db`
- `data/enrichment/architizer_html_snapshots_v2/`
- `data/reports/architizer_source_recrawl_v2_*.md`

이 runtime artifact는 Git에 포함하지 않는다.

Sidecar가 보존하는 핵심 lineage:

- sitemap snapshot, 등록 child URL, 발견·요청 시각, URL/lastmod
- immutable source DB path, size, before/after SHA
- target URL, entity type, discovery source/reason, priority
- attempt 수, HTTP status, final URL, content type, retryability, block signal
- response SHA-256과 content-addressed atomic gzip snapshot
- parser/metadata version, embedded JSON raw blob, DOM-derived 값
- identity status와 오류, field별 raw/normalized value·parse status·quality
- resolved/conflict value를 분리한 metadata version
- valid identity와 resolved relation에서만 파생한 firm/project seed
- run별 target과 metadata version 연결

State DB는 하나의 immutable source path/SHA/size에 결속된다. source DB,
state DB, snapshot 경로 alias는 write 전에 거부한다. O_EXCL process lock,
명시적 stale-lock 검사·복구, interrupted run resume, changed-lastmod
rescheduling, retryable failure, no-clobber current field를 구현했다.

Snapshot은 같은 디렉터리의 임시 파일에 write, flush, fsync한 뒤 atomic
publish한다. 중단으로 남은 truncated final snapshot은 SHA 검증 후 안전하게
복구한다.

### Parser 의미

`architizer-source-parser-v2.2.0`은 embedded JSON과 DOM observation을
분리 보존한다. 다음 항목을 표본에서 검증했다.

- project PK/global ID/slug/name
- firm relation
- location, completion year, construction status, size bucket
- full/short description
- category/tag
- cover/gallery URL과 image global ID
- published/modified time
- firm name/description/office/project/social relation

두 source가 충돌하면 임의 선택하지 않고 `conflict`로 둔다. gallery 전체
이미지의 의미를 article/category에서 추정하지 않는다. identity가 valid가
아니거나 relation field가 conflict이면 relation target을 만들지 않는다.

## 우선순위와 full universe

Target 우선순위:

1. 현재 sitemap 신규 URL
2. overlap 중 lastmod 변경 URL
3. legacy failed 3건과 done-row-mismatch 1건
4. legacy project의 missing firm stub
5. award-only project/firm slug
6. unchanged deterministic quality sample

현재 sitemap에서 사라진 URL은 삭제하지 않는다.

Full은 승인 시점의 URL universe와 SHA를 freeze한다. 실행 중 valid relation에서
새 target이 생기면 승인 대상에 몰래 추가하지 않는다. run은
`completed_with_pending_discoveries`로 끝나며 새 count와 URL-set SHA를
보고한 뒤 다음 phase 승인을 요구한다.

## Smoke ladder

Offline test는 sitemap/project/login/block/error fixture, parser·snapshot
integrity, state resume/idempotency, process lock/stale recovery,
changed-lastmod, failed retry, source binding, no-clobber, relation scheduling,
full freeze, quality-gate forgery 방지를 포함한다.

- Architizer recrawl: 30/30 PASS
- Architizer recrawl + curated 회귀: 47/47 PASS
- 전체 repository `pytest`: 190 passed, 9 subtests passed, 1 failed
- 유일한 실패는 기존 Divisare
  `test_unapproved_merge_decision_is_rejected`의 Windows SQLite handle
  teardown 오류다. 금지 범위이므로 Divisare 파일은 수정하지 않았다.

Quality gate `architizer-smoke-gate-v2`는 저장된 `gate_passed`를 신뢰하지
않고 실제 summary metric과 HTTP final URL evidence를 다시 계산한다.

- 모든 selected URL의 final HTTP success, snapshot, metadata version
- input DB SHA 불변
- block/login/rate signal 0
- 필수 6개 selection type 존재
- unexpected identity/no-content 0
- name/slug coverage 각각 95% 이상
- exact known redirect 또는 source의 명시적 `?notfound=1`만 예외
- 검증된 source absence는 최대 5%

### N10

최종 run은 census 9 이후 run 12다.

- 10/10 HTTP success, snapshot, metadata version, valid identity
- block/login signal 0, physical attempt 10
- 신규 2, modified 2, legacy recovery 2, firm stub 2, award 1, unchanged 1
- parse: complete 3, partial 4, conflict 3, no_content 0
- elapsed 19.271초, median response 1.3165초
- gate v2: PASS
- legacy source SHA: unchanged

### N100

최종 run은 run 13이다.

- 100/100 HTTP success, snapshot, metadata version
- identity: valid 99, exact known project→firm redirect 1
- block/login/rate/error signal 0, physical attempt 100
- 신규 25, modified 20, legacy recovery 2, firm stub 20, award 15,
  unchanged 10, priority fill 8
- parse: complete 51, partial 30, conflict 18, no_content 1
- elapsed 199.629초, mean response 1.1669초, median 1.117초,
  max 2.174초
- response 평균 213,653 bytes, gzip snapshot 평균 41,869 bytes
- runtime 저장공간 증분 11,866,917 bytes
- gate v2: PASS
- legacy source SHA: unchanged

필드 coverage는 entity별 적용 가능한 분모를 사용했다.

| Field | Coverage |
|---|---:|
| name | 98/100 (98.0%) |
| slug | 99/100 (99.0%) |
| description | 76/100 (76.0%) |
| project ID/global ID | 75/76 (98.7%) |
| firm relation | 75/76 (98.7%) |
| location | 65/76 (85.5%) |
| completion year | 67/76 (88.2%) |
| construction status | 74/76 (97.4%) |
| size bucket | 56/76 (73.7%) |
| short description | 75/76 (98.7%) |
| category/tag | 75/76 (98.7%) |
| cover/gallery/image global ID | 75/76 (98.7%) |
| published/modified time | 75/76 (98.7%) |
| firm project/social relation | 24/24 (100.0%) |

최종 표본의 `no_content` 1건은 이미 알려진
`requiem-for-ruins-2`의 project→firm identity redirect다. 앞선 gate 보정
표본에서는 `yuanbo-jia-zhijun-lei`가 같은 firm entity의 공식
`/firms/?notfound=1`로 종료됨을 확인했다. 두 URL 모두 정상 entity로
억지 저장하지 않고 terminal source outcome으로 남겼다.

## Full preview와 승인 경계

최종 gate-v2 N100 이후 read-only `preview-full` 결과는 다음과 같다.

- frozen 대상 universe: 23,623 URL
  - project 16,988
  - firm 6,635
- 이미 완료: 233
- terminal source outcome으로 제외: 2
- full network 잔여 대상: 23,388
- 2초 start-to-start delay와 N100 실측 wall time 1.99629초/target을
  적용한 예상시간: 46,776초, 즉 12시간 59분 36초
- N100의 snapshot+SQLite 실측 증분과 2배 안전계수를 적용한 runtime
  저장공간: 2,775,430,572–5,550,861,144 bytes
  (약 2.78–5.55 GB, 2.58–5.17 GiB)
- 권장 delay: 2.0초

예상시간은 request delay와 응답시간을 더하지 않는다. HTTP client의 delay가
요청 시작 간 최소 간격이고 N100 wall time에 응답·parse·snapshot·SQLite
비용이 이미 포함되므로, 두 값 중 큰 값으로 산정했다.
Full runner는 N100 권장값과 보수적 하한 2.0초 중 큰 값을 최소 delay로
강제하므로 CLI에서 더 작은 값을 넘겨도 network fetch 전에 중단한다.

Terminal 목록:

- `https://architizer.com/projects/requiem-for-ruins-2/`
- `https://architizer.com/firms/yuanbo-jia-zhijun-lei/`

실행 명령은 다음과 같지만 사용자 승인 전에는 실행하지 않는다.

```powershell
python -B tools/recrawl_architizer_source_v2.py full `
  --confirm-full-network-crawl
```

## Curated DB 후속 단계

Full이 승인·완료되기 전에는 curated v1.3을 다시 만들지 않는다. 이후 별도
단계에서 curated v1.3, immutable raw DB, recrawl sidecar 세 입력을
reconciliation하여 새 immutable version을 만든다. 기존 v1.3은 덮어쓰지
않으며 신규·수정·복구 project, firm stub/award unresolved 감소, coverage,
taxonomy claim, duplicate candidate, input/output SHA, N10/N100/full validation을
새로 보고한다.

## Open QA

- sitemap absence는 삭제, 숨김, rolling-window 이탈을 구분하지 못한다.
- Architizer lastmod의 정확한 변경 의미는 확인되지 않았다.
- 날짜 단위 tie와 1,000-entry offset 경계가 누락을 만드는지는 반복 census가
  필요하다.
- award seed가 현재 project/firm으로 유효한지는 entity fetch 전 확정할 수
  없다.
- source의 `?notfound=1`과 project→firm 이동은 삭제가 아니라 terminal source
  outcome으로만 기록한다.
