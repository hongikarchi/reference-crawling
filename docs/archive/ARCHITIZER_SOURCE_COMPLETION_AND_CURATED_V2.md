# Architizer source completion 및 curated v2

기준일은 2026-08-05이다. 이 문서는 Architizer source census와 recrawl 이후
source-specific curated SQLite v2를 출판한 완료 보고다. 모든 runtime 수치는
실제 artifact와 READY/hash/SQLite 검증을 통과한 값만 기록한다.
여기서 “완료”는 2026-07-31 공식 sitemap과 그 수집 결과에서 승인된
relation-discovery closure 범위를 뜻한다. Architizer의 역사적 all-time
inventory 전체를 수집했다는 뜻은 아니며, 그 범위는 계속 open QA다.

## 현재 단계

| 단계 | 상태 | 출판 조건 |
|---|---|---|
| immutable raw 및 curated v1.3 | 보존 완료 | SHA 불변 |
| 공식 sitemap census | 완료 | 등록 child sitemap만 사용 |
| frozen full run 14 | 완료 | snapshot/lineage 검증 완료 |
| run 15 및 후속 discovery wave | 완료 | runs 15–25 terminal, pending 0 |
| snapshot reparse | 완료 | run 28, 6,606/6,606 valid |
| post-reparse shared sidecar | frozen | `7BB98F789CBC779A3DA3F7A94D9E092A05883B23EC6441119A67FE11573742B8` |
| structured 2026 awards | 완료 | 1,030 attributions, 5 official tracks |
| reconciliation | 완료 | 61,970 included project + 2,288 QA-only project |
| immutable curated v2 | 완료 | `605F4F534FC74267D49EA0B7B3F9B3BED6B55ACC3A44A18AE7CAFDC53633FBBC` |

## 1. 기존 curated v1.3에 대한 판정

- `data/crawl/architizer.db`는 2026-04-28 fixed snapshot이며 SHA-256은
  `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985`이다.
- `data/curated/architizer_curated_v1_3.db`는 이 snapshot을 보수적으로
  가공한 immutable 결과이며 SHA-256은
  `5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089`이다.
- 따라서 v1.3의 fixed-snapshot 가공은 완료됐지만, 당시 Architizer source
  전체가 수집됐다는 뜻은 아니다. v1.3 DB·report·commit은 덮어쓰지 않는다.
- 새 결과는 raw, v1.3, recrawl sidecar, awards DB, reconciliation plan을
  명시적으로 결속한 별도 immutable curated v2로 출판한다.

## 2. 공식 sitemap census 결과

2026-07-31 census는 `https://architizer.com/sitemap.xml`에 실제 등록된
project child 12개와 firm child 3개만 사용했다. 임의의 `?p=1..N` 범위는
사용하지 않았다.

| 항목 | Project | Firm |
|---|---:|---:|
| legacy queue | 10,636 | 2,802 |
| 현재 distinct URL | 11,303 | 2,545 |
| legacy/current overlap | 7,992 | 1,807 |
| 현재에만 존재 | 3,311 | 738 |
| legacy에만 존재 | 2,644 | 995 |
| overlap 중 lastmod 변경 | 535 | 66 |
| 현재 URL이나 entity row 없음 | 3,314 | 738 |
| 현재 lastmod 최소 | 2025-08-01 | 2025-07-31 |
| 현재 lastmod 최대 | 2026-07-29 | 2026-07-28 |

Project sitemap에는 shard 경계 중복 22건이 있었다. legacy/current의 약
1년 lastmod 범위와 사이트가 표방하는 corpus 규모를 함께 보면 공식
sitemap은 전체 inventory보다 rolling window일 가능성이 높다. 현재
sitemap에서 사라졌다는 이유만으로 legacy record를 삭제하거나 tombstone으로
판정하지 않는다. legacy awards는 2013–2025 범위라 2026 awards가 빠져 있었다.

Open QA로 남기는 source 의미는 다음과 같다.

- legacy-only project 2,644개와 firm 995개가 rolling window 이탈, 비공개,
  URL 이동, 실제 삭제 중 무엇인지는 sitemap만으로 구분할 수 없다.
- sitemap `lastmod` 변경이 어느 project/firm field의 변경을 뜻하는지 공식
  설명이 없다. 변경 URL 수를 곧바로 metadata 변경 수로 해석하지 않는다.

## 3. 기존 crawler의 문제점

주요 HIGH 위험은 다음과 같다.

- sitemap index 대신 고정 page range를 순회했다.
- `INSERT OR IGNORE`로 기존 lastmod와 done 상태가 고착됐다.
- changed-lastmod 재예약과 failed retry selector가 없었다.
- 원문 HTML/embedded JSON snapshot이 없어 parser 변경 시 재다운로드가
  필요했다.
- final URL, content type, login/block/soft-404와 entity identity를 충분히
  검증하지 않았다.
- sparse parse upsert가 정상 기존 값을 `NULL`로 덮을 수 있었다.
- awards 연도·track과 project/firm/award discovery가 분리돼 있었다.

MEDIUM 위험은 atomic claim/second-process lock, circuit breaker, retry pacing,
resume 계약 부족이었다. recrawl v2는 immutable source binding, atomic gzip,
attempt lineage, retry/circuit breaker, exclusive lock, no-clobber와 frozen
universe를 적용한다.

## 4. recrawl 필요 범위와 진행 상태

legacy recovery 대상은 failed project 3건과 done-row-mismatch 1건이었다.
missing firm stub은 1,951 slugs/5,851 project references였고, award-only seed도
별도 discovery 근거로 보존했다.

승인된 frozen full run 14의 확정 결과는 다음과 같다.

- 상태 `completed_with_pending_discoveries`, frozen/selected 23,389
- frozen URL SHA-256
  `07EA289999CD5349750CB94D3733F50369D5265C50624BE3745FAC2FED7A0EB0`
- HTTP/snapshot/metadata 23,389/23,389, physical attempts 23,392
- transient retry 3건 모두 회복, block/login/rate signal 0
- identity valid 23,259, terminal `no_content` 130
- parse: complete 9,384 / conflict 4,642 / partial 9,233 / no_content 130
- elapsed 47,178.479초, post-close runtime storage 2,836,575,170 bytes
- post-close sidecar SHA-256
  `A78F5C7AC31BBE8250073C2F8C213B86BB1E841F3062C345AE5BD3A830DBF4A5`
- run 14 snapshot manifest SHA-256
  `E32CE83FFE66EE44128A6035AC4922EDABD45940F870AD40844191FE8FDF9BB1`
- run 중 발견됐으나 frozen 범위에 자동 편입하지 않은 URL 38,827,
  URL-set SHA-256
  `122E20961BB1ED096A5E6DCA23CB6AD371712F2A1A4B561C65194C80789CC5A3`

run 15는 수집은 끝났지만 summary SQL variable 오류로 최초 capture에서
`failed`였고, network 요청 없는 offline finalizer가 이를
`completed_with_pending_discoveries`로 복구했다. 이후 closure 결과는 다음과
같다.

| 범위 | selected / metadata / 새 pending | URL-set SHA-256 | 종료 sidecar SHA-256 | elapsed / storage |
|---|---|---|---|---|
| run 15 | 38,827 / 38,827 / 1,820 | `122E20961BB1ED096A5E6DCA23CB6AD371712F2A1A4B561C65194C80789CC5A3` | `12C099F1CDE143AE5197983329140E4678E67D7FFEF5E648C1CF8FDA49AA063E` | 78,223.0초 / 7,475,106,438 bytes |
| runs 15–25 union | 45,753 / 45,753 / 0 | `32043EEF83B3B344BE16DD4B759A2EA86D9FE9AC08E8451883A5FD98C7C44DFE` | `AB378A879865CE189F384F77ECD22674CC55E5E3D4A18404678F88710F083BF3` | 92,157.609초 / 8,218,001,897 bytes |

11개 wave는 모두 terminal이며 final run 25는 `completed`다. closure
manifest의 done 69,116 / failed 150은 follow-up union 통계가 아니라 capture
시점의 global sidecar 통계다. sitemap absence는 계속 삭제 근거로 사용하지
않는다.

## 5. 구현·수정 파일

핵심 구현은 source-specific 파일에 한정한다.

- crawl/state: `crawl/architizer/recrawl_v2.py`
- awards parse/store: `crawl/architizer/awards_v2.py`,
  `crawl/architizer/awards_store_v2.py`
- policy/schema: `canonical/architizer_reconciliation.py`,
  `canonical/architizer_curated_v2.py`
- tools: `tools/audit_architizer_source.py`,
  `tools/recrawl_architizer_source_v2.py`,
  `tools/inspect_architizer_recrawl.py`,
  `tools/build_architizer_awards_v2.py`,
  `tools/build_architizer_reconciliation_manifest.py`,
  `tools/reconcile_architizer_curated_v2.py`,
  `tools/build_architizer_curated_v2.py`
- tests: Architizer recrawl, snapshot reparse, reconciliation, awards,
  curated v1/v2의 8개 test module

현재 source parser/state/metadata 계약은
`architizer-source-parser-v2.3.0` / `2.2` /
`architizer-source-metadata-v2.3`이다. Awards 계약은
`architizer-awards-store-v2.3.0` /
`architizer-awards-source-v2.3.0` /
`architizer-awards-ready-v3`이다. 실제 awards, reconciliation, curated v2
runtime artifact는 이 버전 문자열만으로 완료 판정하지 않는다.

독립 코드 검토 후에는 snapshot gzip의 root/content/hash 검증, run-scoped
historical summary, strict slug/entity-type 검증, immutable input의 publish
전·후 재해시와 lock owner token을 추가했다. 정상 no-race 출력의 row/schema,
materializer version과 frozen artifact bytes는 바꾸지 않았다.

## 6. offline test 결과

- Architizer release 8개 module: `173 passed`, `190 subtests passed`
- 독립 변조 검증: DB 직접 변조, `VACUUM`, READY 재봉인 15/15 reject
- repository 전체: `316 passed`, `196 subtests passed`, 기존 Divisare failure 1건
- 알려진 failure:
  `DivisareV2BuilderTests.test_unapproved_merge_decision_is_rejected`
- parser/snapshot integrity, resume/idempotency, second-process lock,
  changed-lastmod, failed retry, no-clobber, source lineage, reparse lineage,
  awards parent parity와 release self-reseal을 포함한다.

Divisare 파일·data, `core/vocab.py`, 공통 canonical schema, pHash, Vision,
image classification, cross-site matching, embedding/vector DB, Neon/R2는
수정하거나 실행하지 않았다. 이 문서 작성 과정에서도 network와 runtime
artifact를 건드리지 않았다.

## 7. N10 결과

최종 network N10은 parser v2.3의 run 16이며 gate
`architizer-smoke-gate-v2`를 통과했다.

- HTTP/snapshot/metadata/identity 10/10, physical attempts 10
- 신규 2 / modified 2 / legacy recovery 2 / firm stub 2 / award 1 /
  unchanged 1
- complete 5 / partial 3 / conflict 2 / no_content 0
- block/login signal 0, elapsed 22.506초, median response 1.4255초
- legacy raw SHA before/after 동일

Curated v2 N10도 별도 immutable artifact에서 project/firm/award 10/10/10,
byte determinism, quick/integrity/FK gate를 통과했다. DB SHA-256은
`5C3473BFB00B3FBC81F0892CAC586F6D80483F1E94D229EF0B6457DA01E2119F`이다.

## 8. N100 결과

최종 network N100은 parser v2.3의 run 17이며 동일 gate를 통과했다.

- HTTP/snapshot/metadata 100/100, physical attempts 100
- identity valid 99, known project→firm redirect 1
- complete 41 / partial 38 / conflict 20 / no_content 1
- block/login/rate/error signal 0
- elapsed 203.024초, median response 1.2045초
- coverage: name 97%, slug 99%, location 81.1%, completion year 90.5%,
  description 76%, category/image 98.6%
- runtime 증가 11,988,983 bytes, legacy raw SHA before/after 동일

Curated v2 N100도 project/award 100/100, firm 98, byte determinism,
quick/integrity/FK gate를 통과했다. DB SHA-256은
`709B38DA6057B317BD4308F54C043AA638BF8365B44E699AC43C1E8CA3A39CD3`이다.

## 9. full 대상 건수·시간·저장공간

run 14는 frozen target 23,389건을 47,178.479초에 처리했고 post-close
runtime storage는 2,836,575,170 bytes였다. 이는 확정 실측값이며 후속 wave
예측치로 재사용하지 않는다.

후속 실행의 실측 대상·완료·pending·시간·저장공간은 각각
`45,753`, `45,753`,
`0`, `92,157.609 seconds`,
`8,218,001,897 bytes (pre-reparse)`로 확정한다. snapshot reparse는
`28`, `completed; gate passed`,
`6,606`,
`{"entity_types":{"firm":6604,"project":2},"identity":{"valid":6606},"parse_status":{"complete":987,"conflict":4478,"partial":1141}}`,
`346008D0C366EEF103C7B9A0C69A8D53E9B50246D92C1D031089F275FCF1568E`,
`346008D0C366EEF103C7B9A0C69A8D53E9B50246D92C1D031089F275FCF1568E`,
`DD9DF6090FBEE8D8B454B9394C51F774DC8BB9F5843321A9559448D4681CB735`를 검증한 뒤 완료로 표시한다.
마지막 두 SHA는 run arguments의 frozen 값과 실제 selected URL/descriptor
재계산값이 각각 일치할 때만 기록한다.
post-reparse 최종 combined storage는 8,430,559,721 bytes이며, sidecar DB
5,543,510,016 bytes와 gzip snapshot 2,887,049,705 bytes의 합이다.
`source_http_attempt_count=6,606`은 저장 snapshot의 원 network lineage 수이고,
reparse 자체의 신규 HTTP 요청은 0건이다.

`RUN15_*`는 run 15 한 번의 frozen target, wall-clock elapsed, post-close
combined runtime storage를 뜻한다. `FOLLOWUP_*`는 run 14 이후 승인된 모든
wave의 합계이며 run 15를 포함한다. target은 wave별 frozen URL의 distinct
union, completed는 그 union 중 terminal metadata를 가진 URL, URL-set SHA는
정렬한 union의 canonical SHA, elapsed는 wave elapsed의 합, storage는 마지막
wave 종료 후 combined runtime bytes로 정의한다. `runs 15-25 terminal; final run 25 completed; pending=0`는
모든 wave의 terminal 상태와 최종 pending 유무를 함께 요약해야 한다.

## 10. 승인 gate와 curated v2 출판 순서

run 14는 사용자 승인 후 수행됐다. 이 문서는 새로운 대규모 network crawl
승인을 대신하지 않는다. 후속 wave 승인 근거는
`explicit continuation approval in the current thread on 2026-08-04`로 기록한다.

출판 순서는 다음과 같다.

1. run 15/후속 wave의 terminal 상태와 URL-set SHA를 고정한다.
2. 저장된 snapshot만 사용해 parser v2.3 reparse를 완료한다.
3. structured 2026 awards DB를 no-overwrite로 만들고
   `1,030`, `{"Firm":104,"Plus":211,"Products":153,"Sustainability":90,"Typology":472}`,
   `E237C2AD03669126B64FBAB418BCABE412E21CDCE09E20B0599A5B07A4320E4D`, `7,995,392`,
   `1B90C3E7AF09937A576278401814621E4932D300048FD8788D425020180A8D1D`, `1,599`,
   `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"logical_pages":6,"physical_gzip_snapshots":5,"typology_final_url":"https://winners.architizer.com/2026/Typology/","root_alias_tracks":[]}`을 검증한다.
4. raw/v1.3/sidecar/awards를 reconciliation하고
   `{"baseline_retained":103754,"confirmed_same":139622,"new_from_recrawl":878937,"recrawl_filled":17455,"recrawl_updated":9347,"unresolved_missing":405085}`,
   `779EF04A3749D07387E0E772C51D8F15346DAF1FF31F9939513B865CEA4D0A04`,
   `B101A0C469C509B9A4CE9D82F44B35FAE830A79031C16AD08CEA442AADF59A49`,
   `6,341,955,584`,
   `C4D5B0BD7340AB650B4FC60E06191FC1C8F6A0C9F211A0A1E1B05B667818A54C`,
   `3,638`,
   `1C820AD772C68C4FF9B75FEA64085EF3B2B9E155935143EDA15D718CA19764D1`,
   `2,096`,
   `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"publication_eligibility":"eligible_materialization_input","selected_projects":64258,"included_projects":61970,"qa_only_projects":2288,"selected_firms":8474,"pending_targets":0}`을 검증한다.
5. 기존 v1.3을 덮어쓰지 않는 curated v2를 만들고
   `61,970`, `8,486`,
   `13,978`,
   `1,030`,
   `880`,
   `14,858`,
   `atzv2_157158c15a947e6ee188625b`, `605F4F534FC74267D49EA0B7B3F9B3BED6B55ACC3A44A18AE7CAFDC53633FBBC`,
   `228F3EB3B2CE5DCB129F3F550C9A9B555C82EDF19C99FB7BB98571AB86C1428F`, `8,767,438,848`,
   `10171AF3F2F7C7147E1C12B812180AC7370249205F0418449775698C04A0D7FA`, `0F215AE7B918C244BF22925046EE6C8648727126998B4482F53356530A7F394E`,
   `9,142.557 seconds`,
   `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"deterministic_verified":true,"journal_mode":"delete","synchronous":2,"sidecars_absent":true,"baseline_contract_objects":52}`을 기록한다.

Reparse 이후 awards와 reconciliation이 공유해야 하는 최종 sidecar identity는
`data/enrichment/architizer_source_recrawl_v2.db`, `7BB98F789CBC779A3DA3F7A94D9E092A05883B23EC6441119A67FE11573742B8`,
`5,543,510,016`, `2.2`이다.
종료 시 `absent`,
`absent`, `absent`,
`ok`,
`ok`,
`0`를 확인하고, awards와
reconciliation READY/lineage가 이 identity 및 immutable raw SHA를 정확히
공유했는지는 `awards READY, reconciliation manifest/READY, and curated v2 lineage all pin sidecar 7BB98F789CBC779A3DA3F7A94D9E092A05883B23EC6441119A67FE11573742B8 / 5,543,510,016 bytes / schema 2.2 and raw 35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985`으로 기록한다.

Award count 의미는 분리한다.

- `1,030`: immutable awards DB의
  `award_attributions` row 수
- `1,030`: curated v2의
  `v2_structured_award_attributions` row 수
- `880`: complete
  project/firm 2026 tier 중 v1-compatible `source_awards`에 투영된 row 수
- `13,978`: 보존된 2025년까지
  포함하는 2013–2025 legacy `source_awards` row 수
- `14,858`: 투영 후 최종
  `source_awards` 전체 row 수

Curated v2 완료 보고에는 다음 operational delta를 각각 기록한다.

| 지표 | 완료 값과 정확한 의미 |
|---|---|
| 신규 project | `51,342`: 구현 metric `new_included_project_count` |
| 수정·보충 project | `3,422`: 구현 metric `baseline_project_recrawl_updated_or_filled_count` |
| legacy failed 복구 | `3`: 구현 metric `recovered_legacy_failed_retry_valid_included_count`; legacy failed 3건의 최종 회계 |
| mismatch 미복구 | `1`: 구현 metric `unrecovered_legacy_done_row_mismatch_terminal_count`; 기존 mismatch 1건의 최종 회계 |
| parser regression 복구 | snapshot reparse 성공은 4건, 그중 curated included metric `recovered_project_parser_regression_reparse_count`는 `3`; `lineweights-la-forum-solo-exhibition`은 partial이며 included corpus 밖이다. |
| firm stub | `{"before":{"award_stub":2117,"project_stub":1951,"total_union":4068},"after":{"award_stub":7,"project_stub":14,"total_union":21},"net_decrease":{"award_stub":2110,"project_stub":1937,"total_union":4047},"promoted_to_crawled":{"from_award_stub":2108,"from_project_stub":1951,"total_union":4059},"new_stubs":{"award_stub":0,"project_stub":12,"total_union":12}}`: project_stub/award_stub/union의 before, after, net_decrease, promoted_to_crawled, new_stubs JSON |
| award unresolved | `{"legacy_link_rows":{"before":9795,"after":171,"net_decrease":9624},"legacy_distinct_target_slugs":{"before":7100,"after":161},"legacy_resolved_transition_count":8533,"legacy_still_unresolved_key_count":167,"legacy_missing_after_key_count":0,"legacy_newly_unresolved_key_count":0,"structured_2026_unresolved":{"link_rows":4,"distinct_target_slugs":3}}`: legacy unresolved link rows와 distinct target slugs의 before/after/net, resolved/still/missing/new key, structured 2026 unresolved link/slug JSON |
| field coverage | `{"project_count":{"before":10632,"after":61970},"average_completeness_score":{"before":0.9003323300727365,"after":0.7850303910494325,"delta":-0.11530193902330399},"project_rates":{"category":{"before":1.0,"after":0.9924640955300952},"confirmed_year":{"before":0.7673062452972159,"after":0.6826367597224463},"description":{"before":0.9236267870579383,"after":0.9184282717443925},"firm":{"before":1.0,"after":1.0},"image":{"before":0.9750752445447705,"after":0.9904308536388575},"location":{"before":0.7359857035364936,"after":0.1262223656608036}},"firm_entities":{"before":6870,"after":8486},"firm_name_rate":{"before":0.6918486171761281,"after":0.9991751119490926},"firm_description_rate":{"before":0.4078602620087336,"after":0.545604525100165},"firm_office_location_rate":{"before":0.0,"after":0.5850813103935895}}`: project·firm core field의 before/after와 평균 completeness delta JSON |
| taxonomy claims | `{"comparison_status":"comparable_full","delta":{"area_bucket:confirmed":32009,"area_bucket:review":12,"completion_year:candidate":8011,"completion_year:confirmed":34145,"completion_year:review":11,"location_city:candidate":-3,"location_city:review":3,"location_country:candidate":-1,"location_country:review":1,"program:candidate":75861,"program:confirmed":57974,"project_status:confirmed":44440,"raw_category:unmapped":1343,"typology:candidate":7687,"typology:confirmed":66399,"work_type:candidate":11750,"work_type:confirmed":7321}}`: axis/status별 claim delta JSON; 의미 개선으로 임의 해석하지 않음 |
| duplicate candidates | `{"before":{"exact_review:review":280,"fuzzy_review:review":16,"strict:auto_clustered":79},"after":{"exact_review:review":4546,"fuzzy_review:review":16,"strict:auto_clustered":78},"delta":{"exact_review:review":4266,"fuzzy_review:review":0,"strict:auto_clustered":-1},"candidate_ids_added":4267,"candidate_ids_removed":2}`: kind/status별 before/after/delta와 candidate ID added/removed JSON |

위 source recovery metric은 서로 배타적이라고 가정하지 않는다. 따라서 합계를
임의로 더해 별도의 “복구 project 총수”를 만들지 않고 각 구현 metric과
대상 URL/entity 근거를 함께 보고한다.
reconciliation의 selected firm 8,474와 최종 firm 8,486의 차이는
materialization 중 새 project relation에서 추가된 project stub 12건이다.
coverage 수치의 하락은 project corpus가 10,632건에서 61,970건으로 넓어져
분모가 달라진 결과이므로, source 품질의 단순 개선·악화로 해석하지 않는다.

## 11. Git commit·push 상태

Git에는 code, tests, 이 문서, job card와 작은 manifest만 포함한다. DB,
snapshot, report, WAL/SHM, log, image, secret은 포함하지 않는다. 의도한
Architizer 파일만 명시적으로 stage하며 `git add .`와 force-push를 사용하지
않는다. 자기 참조가 되는 commit SHA와 main push parity는 이 파일 안에
미리 넣지 않고 최종 사용자 완료 보고에서 실제 값으로 기록한다.

### 최종 치환 체크리스트

- [x] `completed_with_pending_discoveries (offline postprocess recovery; original summary error preserved)`
- [x] `38,827`
- [x] `38,827`
- [x] `122E20961BB1ED096A5E6DCA23CB6AD371712F2A1A4B561C65194C80789CC5A3`
- [x] `12C099F1CDE143AE5197983329140E4678E67D7FFEF5E648C1CF8FDA49AA063E`
- [x] `78,223.0 seconds`
- [x] `7,475,106,438 bytes`
- [x] `11`
- [x] `runs 15-25 terminal; final run 25 completed; pending=0`
- [x] `45,753`
- [x] `45,753`
- [x] `0`
- [x] `32043EEF83B3B344BE16DD4B759A2EA86D9FE9AC08E8451883A5FD98C7C44DFE`
- [x] `AB378A879865CE189F384F77ECD22674CC55E5E3D4A18404678F88710F083BF3`
- [x] `92,157.609 seconds`
- [x] `8,218,001,897 bytes (pre-reparse)`
- [x] `explicit continuation approval in the current thread on 2026-08-04`
- [x] `28`
- [x] `completed; gate passed`
- [x] `6,606`
- [x] `{"entity_types":{"firm":6604,"project":2},"identity":{"valid":6606},"parse_status":{"complete":987,"conflict":4478,"partial":1141}}`
- [x] `346008D0C366EEF103C7B9A0C69A8D53E9B50246D92C1D031089F275FCF1568E`
- [x] `346008D0C366EEF103C7B9A0C69A8D53E9B50246D92C1D031089F275FCF1568E`
- [x] `DD9DF6090FBEE8D8B454B9394C51F774DC8BB9F5843321A9559448D4681CB735`
- [x] `data/enrichment/architizer_source_recrawl_v2.db`
- [x] `7BB98F789CBC779A3DA3F7A94D9E092A05883B23EC6441119A67FE11573742B8`
- [x] `5,543,510,016`
- [x] `2.2`
- [x] `absent`
- [x] `absent`
- [x] `absent`
- [x] `ok`
- [x] `ok`
- [x] `0`
- [x] `awards READY, reconciliation manifest/READY, and curated v2 lineage all pin sidecar 7BB98F789CBC779A3DA3F7A94D9E092A05883B23EC6441119A67FE11573742B8 / 5,543,510,016 bytes / schema 2.2 and raw 35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985`
- [x] `1,030`
- [x] `{"Firm":104,"Plus":211,"Products":153,"Sustainability":90,"Typology":472}`
- [x] `E237C2AD03669126B64FBAB418BCABE412E21CDCE09E20B0599A5B07A4320E4D`
- [x] `7,995,392`
- [x] `1B90C3E7AF09937A576278401814621E4932D300048FD8788D425020180A8D1D`
- [x] `1,599`
- [x] `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"logical_pages":6,"physical_gzip_snapshots":5,"typology_final_url":"https://winners.architizer.com/2026/Typology/","root_alias_tracks":[]}`
- [x] `{"baseline_retained":103754,"confirmed_same":139622,"new_from_recrawl":878937,"recrawl_filled":17455,"recrawl_updated":9347,"unresolved_missing":405085}`
- [x] `779EF04A3749D07387E0E772C51D8F15346DAF1FF31F9939513B865CEA4D0A04`
- [x] `B101A0C469C509B9A4CE9D82F44B35FAE830A79031C16AD08CEA442AADF59A49`
- [x] `6,341,955,584`
- [x] `C4D5B0BD7340AB650B4FC60E06191FC1C8F6A0C9F211A0A1E1B05B667818A54C`
- [x] `3,638`
- [x] `1C820AD772C68C4FF9B75FEA64085EF3B2B9E155935143EDA15D718CA19764D1`
- [x] `2,096`
- [x] `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"publication_eligibility":"eligible_materialization_input","selected_projects":64258,"included_projects":61970,"qa_only_projects":2288,"selected_firms":8474,"pending_targets":0}`
- [x] `61,970`
- [x] `8,486`
- [x] `13,978`
- [x] `1,030`
- [x] `880`
- [x] `14,858`
- [x] `atzv2_157158c15a947e6ee188625b`
- [x] `605F4F534FC74267D49EA0B7B3F9B3BED6B55ACC3A44A18AE7CAFDC53633FBBC`
- [x] `228F3EB3B2CE5DCB129F3F550C9A9B555C82EDF19C99FB7BB98571AB86C1428F`
- [x] `8,767,438,848`
- [x] `10171AF3F2F7C7147E1C12B812180AC7370249205F0418449775698C04A0D7FA`
- [x] `0F215AE7B918C244BF22925046EE6C8648727126998B4482F53356530A7F394E`
- [x] `9,142.557 seconds`
- [x] `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"deterministic_verified":true,"journal_mode":"delete","synchronous":2,"sidecars_absent":true,"baseline_contract_objects":52}`
- [x] `51,342`
- [x] `3,422`
- [x] `3`
- [x] `1`
- [x] `3`
- [x] `{"before":{"award_stub":2117,"project_stub":1951,"total_union":4068},"after":{"award_stub":7,"project_stub":14,"total_union":21},"net_decrease":{"award_stub":2110,"project_stub":1937,"total_union":4047},"promoted_to_crawled":{"from_award_stub":2108,"from_project_stub":1951,"total_union":4059},"new_stubs":{"award_stub":0,"project_stub":12,"total_union":12}}`
- [x] `{"legacy_link_rows":{"before":9795,"after":171,"net_decrease":9624},"legacy_distinct_target_slugs":{"before":7100,"after":161},"legacy_resolved_transition_count":8533,"legacy_still_unresolved_key_count":167,"legacy_missing_after_key_count":0,"legacy_newly_unresolved_key_count":0,"structured_2026_unresolved":{"link_rows":4,"distinct_target_slugs":3}}`
- [x] `{"project_count":{"before":10632,"after":61970},"average_completeness_score":{"before":0.9003323300727365,"after":0.7850303910494325,"delta":-0.11530193902330399},"project_rates":{"category":{"before":1.0,"after":0.9924640955300952},"confirmed_year":{"before":0.7673062452972159,"after":0.6826367597224463},"description":{"before":0.9236267870579383,"after":0.9184282717443925},"firm":{"before":1.0,"after":1.0},"image":{"before":0.9750752445447705,"after":0.9904308536388575},"location":{"before":0.7359857035364936,"after":0.1262223656608036}},"firm_entities":{"before":6870,"after":8486},"firm_name_rate":{"before":0.6918486171761281,"after":0.9991751119490926},"firm_description_rate":{"before":0.4078602620087336,"after":0.545604525100165},"firm_office_location_rate":{"before":0.0,"after":0.5850813103935895}}`
- [x] `{"comparison_status":"comparable_full","delta":{"area_bucket:confirmed":32009,"area_bucket:review":12,"completion_year:candidate":8011,"completion_year:confirmed":34145,"completion_year:review":11,"location_city:candidate":-3,"location_city:review":3,"location_country:candidate":-1,"location_country:review":1,"program:candidate":75861,"program:confirmed":57974,"project_status:confirmed":44440,"raw_category:unmapped":1343,"typology:candidate":7687,"typology:confirmed":66399,"work_type:candidate":11750,"work_type:confirmed":7321}}`
- [x] `{"before":{"exact_review:review":280,"fuzzy_review:review":16,"strict:auto_clustered":79},"after":{"exact_review:review":4546,"fuzzy_review:review":16,"strict:auto_clustered":78},"delta":{"exact_review:review":4266,"fuzzy_review:review":0,"strict:auto_clustered":-1},"candidate_ids_added":4267,"candidate_ids_removed":2}`
