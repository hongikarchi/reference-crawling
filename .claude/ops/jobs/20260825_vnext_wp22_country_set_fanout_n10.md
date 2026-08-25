# WP2.2 ISO country set + anchor/fan-out guarded identity N10 (2026-08-25)

## 범위와 결론

- 지시서: `.claude/ops/jobs/20260825_vnext_wp22_country_set_fanout_order.md`
- Run ID: `wp22-63d372259c4e0599da8302d6`
- 결과: **PASS**
- N100: **실행하지 않음**. 별도 지시를 기다린다.
- merge 실행 / `vnx_*` ID 발급 / registry / Neon / R2: 모두 **0**.
- Vision / LLM / paid API / 예상 비용: `0 / 0 / 0 / $0.00`.

## 계약과 구현

- `canonical/vnext_country_norm.py`에 ISO 3166-1 정적 snapshot을 내장했다. 런타임 `pycountry` 의존성은 없다.
  - ISO rows / official names / common names: `249 / 173 / 11`
  - 실행 가능한 non-ambiguous alias: `424`
  - known canonical: `248` (`Georgia`는 고정 ambiguous라 제외)
  - legacy known canonical: `89`; additions: canonical `159`, alias `398`
  - `Senegal`, `Ecuador`는 known이고 `Georgia`는 계속 ambiguous다.
  - ISO 원본 `iso3166-1.json`: `43,284` bytes / SHA-256 `f01b812b57fba9f31ff621bf33e7c7570a01964dbeb5be2167e94decf538c89f`.
- `docs/vnext/CONTRACT_IDENTITY.md` §3에 다음을 고정했다.
  - 양쪽이 known이고 canonical country가 다를 때만 country hard conflict.
  - `merge_candidate` 제안과 auto-merge eligibility를 분리하고, auto eligibility에는 strict name 또는 direct verified image anchor가 필수다.
  - fan-out은 post-country/post-anchor frozen snapshot에서 계산하며, same-source counterpart의 모든 쌍이 직접 관련된 clique일 때만 예외다.
  - transitive closure, sequential recomputation, legacy snapshot evidence는 사용하지 않는다.
- v3 producer와 독립 validator를 추가했다. Validator는 v3 producer를 import하지 않고 country, six evidence families, anchor, fan-out, transition, N10, Stage B, gate를 전수 재생한다.
- `.gitattributes`에서 manifest가 exact-byte pin한 tracked text 10개(계약, country normalizer, v1/v2 builder, matcher 2개, legacy country provenance 4개)를 모두 `-text`로 고정하고 현재 검증 바이트를 index에 반영했다. Windows `core.autocrlf` 또는 타 OS checkout에서도 manifest SHA가 변하지 않는다.
- review CLI에 v3 전용 `--cache-root`를 추가해 WP1 cache를 오염시키지 않도록 했다.

## Stage A offline replay

- Network / Vision / LLM / paid API: 모두 `0`.
- Source buildings: `91,803`; candidate pairs: `68,049`; six-family rows: `408,294`.
- Direct image evidence rows: `59,187`.
- Country status: known `37,854`, unknown `53,889`, ambiguous `60`.
- ISO 확장으로 변한 source buildings / pairs: `646 / 778`; 새 country conflicts: `35`.
- Origin 변화: `7`; 전부 `matcher_pass2` 추가이고 제거는 `0`.
- Anchor-ineligible pairs: `289`; 최종 auto-merge-eligible: `4,169`.
- Fan-out groups: conflict `117`, clique-exempt `47`; unique demoted pairs: `223`.
- Decision changes: `560`; proposal changes: `229`.

| Proposal | v2 | v3 | Delta |
|---|---:|---:|---:|
| merge_candidate | 4,685 | 4,458 | -227 |
| defer | 2,224 | 2,445 | +221 |
| reject | 61,019 | 61,025 | +6 |
| related_but_separate | 121 | 121 | 0 |

상태 전이는 `merge_candidate->defer 223`, `merge_candidate->reject 4`, `defer->reject 2`이며 나머지는 proposal 유지다. 지시서의 4쌍은 모두 v2 `merge_candidate/eligible`에서 v3 `reject/not eligible`로 바뀌었다.

- France-Senegal: `idc_016b59163550fcd3bfe2dcfde694745a`, `idc_31831329e77427910c173d3142b4dabe`, `idc_55b2f5fc5c91de7d9239c392bc3f08b3`
- Mexico-Ecuador: `idc_4142468fdbf954126c43359abad4e3b3`

Stage A logical SHA: `1a6983afb4abea52580f7be14211cf863aab65c28edc20d15f46e8e9493573d3`.

Full logical SHA: `6ff307b0bc251813607019d404f14142d9eb1f2fda3cf063ee30923b7b911825`.

## Fresh disjoint N10 + Stage B

- v1/v2 reviewed pair union과 겹치는 pair: `0`; endpoint도 모두 disjoint다.
- Strata: new-known-country-conflict `2`, fan-out-demotion `1`, anchor-ineligible `1`, strong-merge `2`, metadata-only `1`, image-evidence-only `1`, conflict-boundary `2`.
- HTTP-only Stage B: selected/logical HTTP/request count `35/35/35`; exact `28`, source_changed `7`, failed `0`.
- Review sheets: `10`; Vision / LLM / paid API: 모두 `0`.
- Blind reviewer labels: same `7`, different `2`, insufficient `1`.
- Adjudicator labels도 동일해 disagreement는 `0`이다.

Unblind 후 관찰한 보수성 신호:

- 새 country-conflict 표본 2쌍은 모두 실제 `different`이고 `reject/not eligible`였다.
- Anchor-ineligible 표본은 `insufficient`; 제안은 유지되지만 auto eligibility는 `0`이었다.
- 실제 `same` 3쌍이 fan-out guard로 `defer`됐다. 이는 false merge가 아니라 수동 review 비용이다.
- 실제 `same` 1쌍은 기존 verified-pHash veto로, 1쌍은 기존 matcher/year gate로 `reject`됐다. 별도 N100 설계에서 false-reject strata로 다뤄야 한다.
- Strong-merge 2쌍은 모두 `same`이고 auto eligible이었다.

## Gate와 검증

- Lane-C gate: **PASS**
  - reviewer `different` ∩ auto-merge-eligible: `0`
  - auto-merge-eligible with hard conflict: `0`
  - reviewer/adjudicator disagreement: `0`
  - final gold: same `7`, different `2`, insufficient `1`
- Independent final validation: **111/111 PASS**, full input/artifact SHA 포함.
- Regression tests: **27 passed** (`WP2`, `WP2.1`, `WP2.2`); 기존 `.pytest_cache` ACL warning 1건만 남았다.
- Git EOL audit: exact-pinned tracked text `10/10`의 staged blob bytes/SHA가 manifest pin과 일치. `-text whitespace=cr-at-eol` 고정 후 일반 `git diff --cached --check` PASS.

## 시도 기록

1. 첫 Stage A PASS artifact는 실제 v2/v3 origin 차이 7건이 report에서 별도 집계되지 않은 것을 audit에서 발견했다. 삭제하지 않고 `attempt1_origin_unreported`로 보존한 뒤, transition schema에 v2/v3 origins와 `origins_changed`를 추가해 v3.1을 전수 재생했다.
2. Blind form의 `hard_conflict_observed`를 처음 boolean으로 채워 `apply-review`가 DB write 전에 거부했다. 설명 문자열로 수정한 뒤 한 번만 lock했다.
3. Pre-Stage-B validator report 저장이 Stage B write와 겹친 race report는 실패 증거와 구분해 보존했다. 최종 validator는 gate 뒤 단독 실행해 111/111 PASS했다.

## 주요 artifact

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `data/enrichment/vnext_identity_candidates_v2.db` | 581,292,032 | `08e28e4e5a1ab5aba9ea5ddd53ca615f417458a6693cdfb1aefc822d57b5b0c4` |
| `data/enrichment/vnext_identity_candidates_v3.db` (final) | 580,710,400 | `c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f` |
| `data/reports/vnext_identity_candidates_v3_manifest.json` | 137,906 | `5bdbfb140f05fe60ea9fb986cb732792b10399df8f29b7f93484288c6737dc68` |
| `data/reports/vnext_identity_candidates_v3_stage_a_report.json` | 2,512,255 | `6ad06068b4385f3f1382bb5eaee30df3de900b0a54f232a0e3fa6b82c754f235` |
| `data/reports/vnext_identity_country_fanout_n10_v3_stage_b_manifest.json` | 1,646 | `d477ce08db5929e34a3d74b89dc9b082e735f99cc595f418835d54ed0338ea66` |
| `data/reports/vnext_identity_country_fanout_n10_v3_stage_b_report.json` | 518 | `631b1b06477bb71a410c8a8779e432d1b6bcec79b8d6ac53855581b948be798b` |
| `data/reports/vnext_identity_candidates_v3_n10_gate.json` | 541 | `cf3b176e539697bebf00687cbc35ae85ae979f868de3004d65e1bb822960fcbf` |
| `data/reports/vnext_identity_candidates_v3_validation.json` | 80,592 | `3ca3b5936e04d4e0bf1ef906888069dca9e28bb45bb373c5c0562b594546ac71` |

보존된 attempt artifact:

- `data/enrichment/vnext_identity_candidates_v3.attempt1_origin_unreported.db`: `576,884,736` bytes / `afe3ce32be9710422a25133fe84c97f579b71368ad36be60760e188d6a68ac1a`.
- `data/reports/vnext_identity_candidates_v3_validation.race_during_stage_b.json`: `152,177,467` bytes / `33b49cf00e6926d774efb7a7c84b161ef058c1c7f94000b3aa24d49610ec4545` (최종 PASS report가 아님).

## Stop condition / next action

WP2.2는 완료했다. N100은 자동 진행하지 않는다. Claude가 v3 분포와 위 false-reject/fan-out review 비용을 반영한 별도 N100 표본 지시서를 발행할 때까지 대기한다.
