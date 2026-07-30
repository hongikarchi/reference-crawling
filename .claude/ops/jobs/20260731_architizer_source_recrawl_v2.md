# 2026-07-31 Architizer source census + recrawl v2

## Scope

- Architizer 공식 sitemap census와 legacy crawler read-only 감사
- immutable raw/curated DB를 건드리지 않는 recrawl sidecar 구현
- offline fixture, N10, N100 검증
- full network crawl은 preview까지만 수행하고 사용자 승인 대기
- Divisare, 공통 schema/vocab, Neon/R2는 범위 밖

## Immutable inputs

| Input | SHA-256 |
|---|---|
| `data/crawl/architizer.db` | `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985` |
| `data/curated/architizer_curated_v1_3.db` | `5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089` |

두 SHA는 census, N10, N100 전후 동일했다. commit `6f80cf2`의 curated
v1.3과 기존 report는 수정·재생성하지 않았다.

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

- Architizer recrawl tests 30/30 PASS
- Architizer recrawl + curated regression 47/47 PASS
- 전체 repository `pytest`: 190 passed, 9 subtests passed, 1 failed
- 유일한 실패는 기존 Divisare
  `test_unapproved_merge_decision_is_rejected`의 Windows SQLite handle
  teardown 오류이며 금지 범위이므로 수정하지 않음
- fixture 범위: sitemap, project, login/block/error, parser/snapshot
  integrity, resume/idempotency, lock/stale recovery, changed-lastmod,
  failed retry, source binding, no-clobber, relation scheduling, full freeze,
  full approval/zero-fetch/delay gate, quality-gate evidence 재검산

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

## Full preview and stop boundary

- universe 23,623: project 16,988 / firm 6,635
- already done 233
- terminal source outcomes excluded 2
- remaining network targets 23,388
- delay 2.0초, N100 observed wall time 1.99629초/target
- 예상 46,776초 = 12시간 59분 36초
- 예상 runtime storage 2,775,430,572–5,550,861,144 bytes
  (2.78–5.55 GB)
- full runner는 N100 권장 delay와 2.0초 하한 중 큰 값을 강제함

Full command는 구현·차단되어 있으나 실행하지 않았다.

```powershell
python -B tools/recrawl_architizer_source_v2.py full `
  --confirm-full-network-crawl
```

## Cost and result

- 유료 LLM/API token 비용: `$0`
- 네트워크 범위: 공식 sitemap/award discovery와 N10/N100 smoke
- 결론: curated v1.3은 fixed-snapshot 가공 완료지만 source 전체 수집 완료는
  아니다. Recrawl은 필요하며 full 23,388건은 사용자 승인 대기 상태다.
- Commit/push: 이 job card를 포함한 atomic `main` commit의 Git history와
  최종 완료 보고에서 기록한다.
