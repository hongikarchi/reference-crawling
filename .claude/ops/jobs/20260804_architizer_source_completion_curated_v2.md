# 2026-08-04 Architizer source completion + curated v2

## 상태와 범위

- 상태: 2026-07-31 공식 sitemap + 승인된 relation-discovery closure 범위의
  reparse, awards, reconciliation, curated v2 출판 완료
- 범위 제한: Architizer 역사적 all-time inventory 전체 수집 여부는 미확정
  open QA이며 이 job의 완료 범위가 아니다.
- 목표: census/run 14 이후 source completion을 검증하고 raw, curated v1.3,
  recrawl sidecar, structured awards를 reconciliation해 별도 immutable
  curated v2를 만든다.
- 기준 문서:
  `docs/ARCHITIZER_SOURCE_COMPLETION_AND_CURATED_V2.md`
- 기존 `docs/ARCHITIZER_SOURCE_CENSUS_AND_RECRAWL.md`와
  `.claude/ops/jobs/20260731_architizer_source_recrawl_v2.md`는 수정하지 않는다.

## Immutable inputs

| Input | SHA-256 |
|---|---|
| `data/crawl/architizer.db` | `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985` |
| `data/curated/architizer_curated_v1_3.db` | `5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089` |

두 입력과 기존 v1.3 report/commit은 read-only baseline이다. 기존 경로를
수정하거나 덮어쓰지 않는다.

## 확정 근거

- 공식 sitemap census: project current 11,303, firm current 2,545;
  rolling-window 가능성이 높아 sitemap absence를 삭제 근거로 쓰지 않는다.
- frozen full run 14: 23,389 selected, HTTP/snapshot/metadata 23,389,
  status `completed_with_pending_discoveries`.
- run 14 이후 별도 승인 범위로 남긴 discovery: 38,827 URLs,
  URL-set SHA-256
  `122E20961BB1ED096A5E6DCA23CB6AD371712F2A1A4B561C65194C80789CC5A3`.
- current recrawl contract:
  `architizer-source-parser-v2.3.0`, state schema `2.2`, metadata `v2.3`.
- current awards contract:
  store/schema `v2.3.0`, READY `v3`.
- Architizer release tests: 173 passed, 190 subtests passed.
- repository tests: 316 passed, 196 subtests passed, 기존 Divisare failure 1건.

## 사용자 완료보고 11항목

1. v1.3은 fixed-snapshot 가공 완료이며 source 전체 완료는 아니었음을 명시한다.
2. 공식 sitemap census의 project/firm overlap, 신규, legacy-only,
   changed-lastmod와 rolling-window 판정을 기록한다.
3. legacy crawler의 HIGH/MEDIUM 위험과 v2 대응을 기록한다.
4. run 14와 이후 run 15/후속 wave/reparse의 범위와 terminal 상태를 기록한다.
5. source-specific code, tools, tests, docs만 구현 파일로 열거한다.
6. offline/release/repository test 결과와 known Divisare failure를 분리한다.
7. N10의 HTTP, identity, parse, timing과 input SHA 불변을 기록한다.
8. N100의 failure/coverage/timing/storage와 input SHA 불변을 기록한다.
9. full 및 follow-up의 대상, 실측 시간, 저장공간, URL-set SHA를 기록한다.
10. 후속 network 승인 근거와 reparse→awards→reconciliation→curated v2
    출판 gate를 기록한다.
11. 의도 파일만 stage한 atomic commit SHA와 main push 상태를 기록한다.

## 구현 및 검증 경계

- runtime DB, gzip snapshot, report, log, WAL/SHM은 Git에 포함하지 않는다.
- Divisare 파일·data, `core/vocab.py`, 공통 schema, cross-site matching,
  pHash, Vision, image classification, embedding/vector DB, Neon/R2를
  건드리지 않는다.
- `.env`, cookie, credential을 출력하거나 commit하지 않는다.
- `git add .`, force-push, 기존 immutable artifact overwrite를 금지한다.
- 이 job card에는 승인된 Architizer network run과 검증된 runtime artifact의
  결과만 기록하며 runtime DB/report/snapshot 자체는 Git에 포함하지 않는다.

## 출판 gate

| Gate | 완료 증거 |
|---|---|
| run 15 | `completed_with_pending_discoveries (offline postprocess recovery; original summary error preserved)`, `38,827`, `38,827`, `122E20961BB1ED096A5E6DCA23CB6AD371712F2A1A4B561C65194C80789CC5A3`, `12C099F1CDE143AE5197983329140E4678E67D7FFEF5E648C1CF8FDA49AA063E`, `78,223.0 seconds`, `7,475,106,438 bytes` |
| follow-up waves | `runs 15-25 terminal; final run 25 completed; pending=0`, `11`, `45,753`, `45,753`, `0`, `32043EEF83B3B344BE16DD4B759A2EA86D9FE9AC08E8451883A5FD98C7C44DFE`, `AB378A879865CE189F384F77ECD22674CC55E5E3D4A18404678F88710F083BF3`, `92,157.609 seconds`, `8,218,001,897 bytes (pre-reparse)`, `explicit continuation approval in the current thread on 2026-08-04` |
| snapshot reparse | `28`, `completed; gate passed`, `6,606`, `{"entity_types":{"firm":6604,"project":2},"identity":{"valid":6606},"parse_status":{"complete":987,"conflict":4478,"partial":1141}}`, `346008D0C366EEF103C7B9A0C69A8D53E9B50246D92C1D031089F275FCF1568E`, `346008D0C366EEF103C7B9A0C69A8D53E9B50246D92C1D031089F275FCF1568E`, `DD9DF6090FBEE8D8B454B9394C51F774DC8BB9F5843321A9559448D4681CB735` |
| final shared sidecar | `data/enrichment/architizer_source_recrawl_v2.db`, `7BB98F789CBC779A3DA3F7A94D9E092A05883B23EC6441119A67FE11573742B8`, `5,543,510,016`, `2.2`, `awards READY, reconciliation manifest/READY, and curated v2 lineage all pin sidecar 7BB98F789CBC779A3DA3F7A94D9E092A05883B23EC6441119A67FE11573742B8 / 5,543,510,016 bytes / schema 2.2 and raw 35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985` |
| sidecar storage health | `absent`, `absent`, `absent`, `ok`, `ok`, `0` |
| awards | `1,030`, `{"Firm":104,"Plus":211,"Products":153,"Sustainability":90,"Typology":472}`, `E237C2AD03669126B64FBAB418BCABE412E21CDCE09E20B0599A5B07A4320E4D`, `7,995,392`, `1B90C3E7AF09937A576278401814621E4932D300048FD8788D425020180A8D1D`, `1,599`, `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"logical_pages":6,"physical_gzip_snapshots":5,"typology_final_url":"https://winners.architizer.com/2026/Typology/","root_alias_tracks":[]}` |
| reconciliation | `{"baseline_retained":103754,"confirmed_same":139622,"new_from_recrawl":878937,"recrawl_filled":17455,"recrawl_updated":9347,"unresolved_missing":405085}`, `779EF04A3749D07387E0E772C51D8F15346DAF1FF31F9939513B865CEA4D0A04`, `B101A0C469C509B9A4CE9D82F44B35FAE830A79031C16AD08CEA442AADF59A49`, `6,341,955,584`, `C4D5B0BD7340AB650B4FC60E06191FC1C8F6A0C9F211A0A1E1B05B667818A54C`, `3,638`, `1C820AD772C68C4FF9B75FEA64085EF3B2B9E155935143EDA15D718CA19764D1`, `2,096`, `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"publication_eligibility":"eligible_materialization_input","selected_projects":64258,"included_projects":61970,"qa_only_projects":2288,"selected_firms":8474,"pending_targets":0}` |
| curated v2 | `atzv2_157158c15a947e6ee188625b`, `61,970`, `8,486`, `13,978`, `1,030`, `880`, `14,858`, `605F4F534FC74267D49EA0B7B3F9B3BED6B55ACC3A44A18AE7CAFDC53633FBBC`, `228F3EB3B2CE5DCB129F3F550C9A9B555C82EDF19C99FB7BB98571AB86C1428F`, `8,767,438,848`, `10171AF3F2F7C7147E1C12B812180AC7370249205F0418449775698C04A0D7FA`, `0F215AE7B918C244BF22925046EE6C8648727126998B4482F53356530A7F394E`, `9,142.557 seconds`, `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"deterministic_verified":true,"journal_mode":"delete","synchronous":2,"sidecars_absent":true,"baseline_contract_objects":52}` |
| Git | 자기 참조 SHA는 문서에 넣지 않고 최종 사용자 보고에서 commit SHA와 push parity를 기록 |

`RUN15_*`는 run 15 단일 실행 값이다. `FOLLOWUP_*`는 run 15를 포함해 run 14
이후 승인된 모든 wave의 distinct frozen URL union/terminal count, elapsed
합계, 마지막 post-close storage와 sidecar identity다. Reparse 이후에는
`FINAL_SIDECAR_*` identity를 새로 계산하며 awards와 reconciliation이 이를
공유해야 한다. Reparse의 eligible URL SHA는 최초 eligible universe identity다.
Frozen URL SHA와 frozen descriptor SHA는 run arguments 값을 실제
selected/frozen input에서 재계산해 각각 일치시킨다.

## Curated v2 operational delta 계약

| 지표 | 완료 증거와 의미 |
|---|---|
| source recovery | `51,342`, `3,422`, `3`, `1`, `3`; snapshot reparse 성공 4건 중 partial이며 included corpus 밖인 `lineweights-la-forum-solo-exhibition`을 제외한 curated included metric이 3 |
| firm stub | `{"before":{"award_stub":2117,"project_stub":1951,"total_union":4068},"after":{"award_stub":7,"project_stub":14,"total_union":21},"net_decrease":{"award_stub":2110,"project_stub":1937,"total_union":4047},"promoted_to_crawled":{"from_award_stub":2108,"from_project_stub":1951,"total_union":4059},"new_stubs":{"award_stub":0,"project_stub":12,"total_union":12}}`: project_stub/award_stub/union의 before/after/net/promoted/new |
| award unresolved | `{"legacy_link_rows":{"before":9795,"after":171,"net_decrease":9624},"legacy_distinct_target_slugs":{"before":7100,"after":161},"legacy_resolved_transition_count":8533,"legacy_still_unresolved_key_count":167,"legacy_missing_after_key_count":0,"legacy_newly_unresolved_key_count":0,"structured_2026_unresolved":{"link_rows":4,"distinct_target_slugs":3}}`: legacy link row·distinct slug before/after/net과 structured 2026 link/slug |
| field coverage | `{"project_count":{"before":10632,"after":61970},"average_completeness_score":{"before":0.9003323300727365,"after":0.7850303910494325,"delta":-0.11530193902330399},"project_rates":{"category":{"before":1.0,"after":0.9924640955300952},"confirmed_year":{"before":0.7673062452972159,"after":0.6826367597224463},"description":{"before":0.9236267870579383,"after":0.9184282717443925},"firm":{"before":1.0,"after":1.0},"image":{"before":0.9750752445447705,"after":0.9904308536388575},"location":{"before":0.7359857035364936,"after":0.1262223656608036}},"firm_entities":{"before":6870,"after":8486},"firm_name_rate":{"before":0.6918486171761281,"after":0.9991751119490926},"firm_description_rate":{"before":0.4078602620087336,"after":0.545604525100165},"firm_office_location_rate":{"before":0.0,"after":0.5850813103935895}}`: project/firm core field before/after와 평균 completeness delta; corpus 확대에 따른 분모 변경이므로 단순 품질 개선·악화로 해석하지 않음 |
| taxonomy | `{"comparison_status":"comparable_full","delta":{"area_bucket:confirmed":32009,"area_bucket:review":12,"completion_year:candidate":8011,"completion_year:confirmed":34145,"completion_year:review":11,"location_city:candidate":-3,"location_city:review":3,"location_country:candidate":-1,"location_country:review":1,"program:candidate":75861,"program:confirmed":57974,"project_status:confirmed":44440,"raw_category:unmapped":1343,"typology:candidate":7687,"typology:confirmed":66399,"work_type:candidate":11750,"work_type:confirmed":7321}}`: axis/status별 delta |
| duplicate | `{"before":{"exact_review:review":280,"fuzzy_review:review":16,"strict:auto_clustered":79},"after":{"exact_review:review":4546,"fuzzy_review:review":16,"strict:auto_clustered":78},"delta":{"exact_review:review":4266,"fuzzy_review:review":0,"strict:auto_clustered":-1},"candidate_ids_added":4267,"candidate_ids_removed":2}`: kind/status별 before/after/delta와 ID added/removed |

Source recovery 완료 값은 reconciliation 구현의 동명 metric에 일대일로
대응한다. legacy failed 3건과 done-row-mismatch 1건을 별도로 회계하며,
new/updated-or-filled/recovery category가 서로 배타적이라고 가정하거나 임의로
합산하지 않는다.

Award count는 2013–2025를 포함하는 legacy `source_awards`, structured attribution,
project/firm 2026 projection, 최종 `source_awards` 합계를 서로 다른 값으로
보고한다. Product/brand와 conflict/partial evidence를 project/firm projection
count에 섞지 않는다.
Reconciliation selected firm 8,474와 최종 firm 8,486의 차이는 새 project
relation에서 materialization 중 추가된 project stub 12건이다. 최종
post-reparse combined storage는 8,430,559,721 bytes(DB 5,543,510,016 +
snapshot 2,887,049,705)이며 reparse 신규 HTTP 요청은 0건이다.

## Open QA

- legacy-only URL이 rolling window 이탈, 비공개, 이동, 실제 삭제 중 무엇인지
  sitemap만으로 결정하지 않는다.
- `lastmod` 변경이 어느 source field 변경을 뜻하는지는 미확정이다.

## 테스트 기록

- Architizer 8-module release suite: `173 passed`, `190 subtests passed`
- independent DB/VACUUM/READY self-reseal tamper cases: 15/15 reject
- repository suite: `316 passed`, `196 subtests passed`, known Divisare failure 1
- final runtime artifact validation: `{"quick_check":"ok","integrity_check":"ok","foreign_key_violations":0,"deterministic_verified":true,"journal_mode":"delete","synchronous":2,"sidecars_absent":true,"baseline_contract_objects":52}`

## 비용

- 문서화·최종 오프라인 검증 단계의 추가 network 요청: `0`; HTTP crawl은 위
  runs 14–25 실측에 포함한다.
- paid API/LLM/Vision/embedding/Neon/R2 비용: `0`
- 최종 runtime 비용·시간은 측정값만 기록하고 추정으로 채우지 않는다.

## 최종 치환 체크리스트

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
