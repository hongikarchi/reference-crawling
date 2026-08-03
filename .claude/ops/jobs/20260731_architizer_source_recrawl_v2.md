# 2026-07-31 Architizer source census + recrawl v2

## Scope

- Architizer 공식 sitemap census와 legacy crawler read-only 감사
- immutable raw/curated DB를 건드리지 않는 recrawl sidecar 구현
- offline fixture, N10, N100 검증
- preview 뒤 사용자 명시적 승인을 받아 frozen full network run 14 수행
- run 중 발견된 follow-up target은 별도 승인 전 fetch하지 않음
- Divisare, 공통 schema/vocab, Neon/R2는 범위 밖

## Immutable inputs

| Input | SHA-256 |
|---|---|
| `data/crawl/architizer.db` | `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985` |
| `data/curated/architizer_curated_v1_3.db` | `5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089` |

두 SHA는 census, N10, N100 및 full run 14 전후 동일했다. commit
`6f80cf2`의 curated v1.3과 기존 report는 수정·재생성하지 않았다.

## Census result

- 공식 root: `https://architizer.com/sitemap.xml`
- root에 등록된 child만 사용: project 12, firm 3
- project: legacy queue 10,636 / current distinct 11,303 / overlap 7,992 /
  current-new 3,311 / legacy-only 2,644 / changed lastmod 535
- firm: legacy queue 2,802 / current distinct 2,545 / overlap 1,807 /
  current-new 738 / legacy-only 995 / changed lastmod 66
- current lastmod: project 2025-08-01–2026-07-29,
  firm 2025-07-31–2026-07-28
- current-new project 중 current lastmod가 2026-04-28 이후인 URL: 3,135
- legacy/current 범위와 약 1년의 lastmod 폭을 근거로 rolling-window
  가능성이 높다고 판정했다. sitemap absence는 삭제로 취급하지 않는다.

Legacy recovery:

- failed 3건은 최신 project page에서 복구 가능
- done-row-mismatch `requiem-for-ruins-2`는 현재 firm으로 이동하므로
  project로 복구하지 않음
- project firm stub 1,951 slugs, 5,851 project references
- award 13,978 rows, 2013–2025; 2026 누락
- unresolved award project 4,983 slugs / firm 2,835 slugs
- 공식 2026 gallery direct seed: project 725 / firm 722

## Legacy crawler audit

HIGH:

- 공식 index 대신 고정 page range 사용
- `INSERT OR IGNORE`로 lastmod와 done 상태 동결
- changed-lastmod 재예약과 failed retry 없음
- raw HTML/JSON snapshot, final URL/content-type/abnormal-200 검사 없음
- entity identity 검증 부재로 article 1건, firm 3건이 project에 혼입
- sparse parse가 기존 값을 NULL로 덮을 수 있음
- award 연도/track 하드코딩, project/firm/award discovery 단절

MEDIUM:

- second-process lock와 atomic claim 부재
- circuit breaker와 retry pacing 불충분
- 제한된 regex/H2 heuristic parser

## Implementation

- `crawl/architizer/recrawl_v2.py`
- `tools/audit_architizer_source.py`
- `tools/recrawl_architizer_source_v2.py`
- `tools/inspect_architizer_recrawl.py`
- `tests/test_architizer_recrawl_v2.py`
- `docs/ARCHITIZER_SOURCE_CENSUS_AND_RECRAWL.md`
- `docs/manifests/architizer_source_census_20260731.json`

Sidecar는 source lineage, sitemap/HTTP metadata, attempt와 retry 상태,
content-addressed atomic gzip snapshot, parser/metadata version, embedded
JSON과 DOM observation, identity/field status와 conflict를 분리 저장한다.
Source DB read-only binding, no-clobber, resume/idempotency, changed-lastmod
scheduling, circuit breaker, exclusive process lock, frozen full universe와
명시적 full 승인 gate를 구현했다.

Runtime only, Git 제외:

- `data/enrichment/architizer_source_recrawl_v2.db`
- `data/enrichment/architizer_html_snapshots_v2/`
- `data/reports/architizer_source_recrawl_v2_*.md`
- stdout/stderr log, SQLite WAL/SHM

## Validation

Offline:

- Architizer recrawl tests 33/33 PASS
- Architizer recrawl + curated regression 50/50 PASS, 3 subtests
- 전체 repository `pytest`: 193 passed, 9 subtests passed, 1 failed
- 유일한 실패는 기존 Divisare
  `test_unapproved_merge_decision_is_rejected`의 Windows SQLite handle
  teardown 오류이며 금지 범위이므로 수정하지 않음
- fixture 범위: sitemap, project, login/block/error, parser/snapshot
  integrity, resume/idempotency, lock/stale recovery, changed-lastmod,
  failed retry, source binding, no-clobber, relation scheduling, full freeze,
  full approval/zero-fetch/delay gate, quality-gate evidence 재검산
- parity 추가 검증: NULL-aware incoming-lastmod semantics와 read-only preview /
  actual expansion eligible count·URL SHA 일치, preview no-write

N10 final (census run 9 / run 12):

- gate `architizer-smoke-gate-v2` PASS
- HTTP/snapshot/metadata/identity 10/10, attempts 10, block/login signal 0
- 신규 2, modified 2, legacy recovery 2, firm stub 2, award 1, unchanged 1
- complete 3 / partial 4 / conflict 3 / no_content 0
- 19.271초, median response 1.3165초

N100 final (run 13):

- gate `architizer-smoke-gate-v2` PASS
- HTTP/snapshot/metadata 100/100, attempts 100, block/login signal 0
- identity valid 99 + exact known redirect 1
- complete 51 / partial 30 / conflict 18 / no_content 1
- name 98%, slug 99%, location 85.5%, year 88.2%,
  description 76%, category/image 98.7%
- 199.629초, median response 1.117초, runtime 증분 11,866,917 bytes

Gate calibration 중 run 11에서 award-derived firm
`yuanbo-jia-zhijun-lei`가 공식 `/firms/?notfound=1`로 종료되는 사례를
발견했다. 원래 reason label을 신뢰하지 않고 final URL, entity path,
HTTP/parse evidence를 재검산하며, 검증된 source absence를 표본의 최대 5%만
허용하도록 gate v2를 고정한 뒤 N10/N100을 새 표본으로 재실행했다.

## Full preview, approval, and run 14

- universe 23,623: project 16,988 / firm 6,635
- already done 233
- terminal source outcomes excluded 2
- remaining network targets 23,388
- delay 2.0초, N100 observed wall time 1.99629초/target
- 예상 46,776초 = 12시간 59분 36초
- 예상 runtime storage 2,775,430,572–5,550,861,144 bytes
  (2.78–5.55 GB)
- full runner는 N100 권장 delay와 2.0초 하한 중 큰 값을 강제함

사용자 명시적 승인 뒤 다음 command를 실행했다.

```powershell
python -B tools/recrawl_architizer_source_v2.py full `
  --confirm-full-network-crawl
```

Preview 23,388건과 실제 frozen 23,389건 사이의 1건은
`archmondo-piotr-kowalczyk-1`이다. target의 NULL source lastmod가 full
expansion에서 sitemap 값 `2025-12-01`로 채워지며 changed-lastmod 정책에 따라
안전하게 재예약됐다. NULL-aware lastmod와 in-memory expansion planning을
preview/full 공용 helper로 만들고 read-only/eligible count·hash parity를
regression test로 고정했다.

Full run 14:

- status: `completed_with_pending_discoveries`
- started/finished: 2026-08-03T05:54:22Z–2026-08-03T19:00:50Z
- measured runtime: 47,178.479초
- frozen/selected: 23,389
- frozen URL SHA-256:
  `07EA289999CD5349750CB94D3733F50369D5265C50624BE3745FAC2FED7A0EB0`
- HTTP/snapshot/metadata: 23,389/23,389
- physical attempts 23,392, transient retry 3건 모두 회복
- identity valid 23,259, terminal no_content 130
- parse: complete 9,384 / conflict 4,642 / partial 9,233 /
  no_content 130
- final URL/identity 기반 operational grouping: project→firm notfound 92 /
  firm notfound 23 / project notfound 12 / anomaly 3. Strict internal
  source-absence classifier label과 동일하다는 뜻은 아님
- block/login/rate signal 0
- mean/median/max response: 1.2536초 / 1.204초 / 30.084초
- run-reported storage after 2,836,493,250 bytes, delta 2,746,741,471 bytes
  - snapshot 976,679,874 bytes
  - state DB 1,859,813,376 bytes; post-close file 1,859,895,296 bytes
  - post-close combined 2,836,575,170 bytes
- raw input SHA before/after 동일
- post-close sidecar SHA-256:
  `A78F5C7AC31BBE8250073C2F8C213B86BB1E841F3062C345AE5BD3A830DBF4A5`
- SQLite quick/integrity check `ok`, foreign-key violation 0
- run 14 snapshot 23,389건 모두 존재·readable·content SHA 일치;
  manifest SHA `E32CE83FFE66EE44128A6035AC4922EDABD45940F870AD40844191FE8FDF9BB1`
- sidecar 전체 unique snapshot 23,760건도 전부 검증, missing/corrupt/mismatch 0;
  manifest SHA `B14C7DB6C76B85E5A9165B5260540CDA62B19BC4226A28357C6FC8EDD528186D`
- process/lock/WAL/SHM 없음

새 pending discovery는 38,827건(project 38,694 / firm 133), URL-set SHA-256은
`122E20961BB1ED096A5E6DCA23CB6AD371712F2A1A4B561C65194C80789CC5A3`이다.
Frozen 승인 범위에 자동 편입하지 않았으며, follow-up은 별도 사용자 승인
전에는 실행하지 않는다. curated v1.3 재빌드와 reconciliation도 실행하지
않았다.

Parity 보정 후 read-only follow-up preview:

- remaining 38,827 / already done 23,491 / terminal excluded 132
- remaining URL SHA `122E...5A3`, would-insert 0, would-reschedule 0
- 예상 77,654초(21시간 34분 14초), delay 2.0초
- 예상 추가 runtime storage 4,607,561,263–9,215,122,526 bytes
- preview 전후 sidecar SHA `A78F...F4A5`로 동일
- preview는 단일 read transaction으로 census/target snapshot을 고정하며,
  priority/reason/retryable/terminal parity도 regression test로 검증함
- 기존 immutable run 14 Markdown에는 pending gate가 없지만 renderer를 보정해
  후속 report에는 frozen/pending count·type·SHA와 추가 승인 여부를 직접 기록함

## Cost and result

- 유료 LLM/API token 비용: `$0`
- 네트워크 범위: 공식 sitemap/award discovery, N10/N100 smoke, 사용자 승인된
  frozen full run 14
- 결론: curated v1.3은 fixed-snapshot 가공 완료지만 source 전체 수집 완료는
  아니다. 승인된 full 23,389건은 완료됐지만 relation-derived follow-up
  38,827건은 별도 승인 대기 상태다.
- Runtime DB/snapshot/report/log는 Git에 포함하지 않는다.
- Commit/push: 이 job card를 포함한 atomic `main` commit의 Git history와
  최종 완료 보고에서 기록한다.
