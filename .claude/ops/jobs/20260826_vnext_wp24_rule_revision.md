# WP2.4 N100 진단 기반 identity 규칙 개정 실행 기록 (2026-08-26)

## 범위와 결론

- 지시서: `.claude/ops/jobs/20260826_vnext_wp24_rule_revision_order.md`
  - 5,585 bytes
  - SHA-256 `e118437909c7b8cbcf5842c6eadecea39a84b958af569bbe2801d03b38a31b2a`
- Run ID: `wp24-c6199e102c3da9ccc4e2b225`
- 최종 결과: **PASS**
- `vnext_identity_candidates_v4` Stage A를 오프라인으로 전수 재생했다.
- v4가 만든 것은 병합 후보와 사람 승인 큐다. 실제 merge, registry, `vnx_*` ID 발급은
  수행하지 않았으며 모든 `merge_candidate`는 계속 사람 승인이 필요하다.
- 신규 N10, network, Vision, LLM, 유료 API, Neon, R2는 모두 `0`이다.

## 계약 개정과 SHA pin

`docs/vnext/CONTRACT_IDENTITY.md`를 v1.1로 개정했다.

- 9,879 bytes
- SHA-256 `502e6258b9c7803ea58bc415ddbe73fbcc50faa0005d8b9aaaaaeab04838e3e8`

| 규칙 | 반영 내용 |
|---|---|
| R1 | matcher architect/year blocking을 hard conflict에서 제거. gap ≤1 positive, 2–5 neutral, ≥6 weak negative. gap ≥6이면서 image와 firm이 모두 positive가 아닐 때만 `project_phase_incompatible` |
| R2 | name+firm positive인 pHash veto를 `phash_veto_review_required`로 강등. `merge_candidate`, eligible=false. 다른 pHash veto는 hard conflict 유지 |
| R3 | qualifying fan-out을 `merge_candidate` + `fanout_review_required`, eligible=false로 전환. 앵커 포함 positive 2계열 미만만 defer. fan-out hard conflict는 생성하지 않음 |
| R4 | 모든 `merge_candidate`와 앵커 포함 positive 2계열 이상 defer를 사람 승인 큐에 포함 |
| R5 | name-only 밴드, known-country conflict, anchor 요건 유지 |

## 실행 1 — Stage A 전수 재생

- v3 후보 universe 68,049쌍을 그대로 재생했다.
- v3 artifact는 비교 전용이며 후보 증거로 사용하지 않았다.
- N100 artifact는 기존 gold 회귀 binding에만 사용했다.
- v3 artifact는 실행 전후 580,710,400 bytes / SHA-256
  `c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f`로 불변이다.
- manifest의 동결 입력 19개는 full-hash validator에서 전부 before=after로 확인됐다.
- Stage A logical SHA-256:
  `f4f264e4d3299ff24d464a6a7b7f03aabdec1e2195808a4523f88060c022e899`
- Full logical SHA-256:
  `fd0c9e32a6781fc0842314ba3e90168f831d75b86d0c86a96f08e1799fb1b486`

## 실행 2 — v3→v4 전이와 볼륨

### 최종 상태 증감

| Proposal | v3 | v4 | 증감 |
|---|---:|---:|---:|
| `merge_candidate` | 4,458 | 5,100 | +642 |
| `defer` | 2,445 | 2,192 | -253 |
| `reject` | 61,025 | 60,753 | -272 |
| `related_but_separate` | 121 | 4 | -117 |

- 최종 `auto_merge_eligible`: 4,169 → 4,346, **+177**
- 전체 decision 변경: **1,801쌍**
- 최종 전이:
  - defer→defer 2,186 / defer→merge 223 / defer→reject 36
  - merge→merge 4,458
  - reject→defer 6 / reject→merge 300 / reject→reject 60,717 /
    reject→related 2
  - related→merge 119 / related→related 2
- eligibility 전이: 0→0 63,697 / 0→1 183 / 1→0 6 / 1→1 4,163

### 규칙별 영향

| 규칙 | 변경쌍 | 핵심 전이·효과 |
|---|---:|---|
| R1 | 1,362 | eligible 0→1 190. matcher year conflict 209건 제거, 기존 phase conflict 18건 제거, 새 조건부 phase conflict 1,039건 생성 |
| R2 | 229 | reject→merge 229, `verified_phash_veto` 229건 제거, `phash_veto_review_required` 229건 생성, 전부 eligible=false |
| R3 | 236 | merge→merge 236, eligible 1→0 236, `fanout_review_required` 236건 생성, fan-out hard conflict 0 |

R1 proposal 전이는 defer→defer 82, defer→reject 36, merge→merge 8,
reject→defer 6, reject→merge 90, reject→reject 1,019, reject→related 2,
related→merge 100, related→reject 19다. 규칙별 변경은 같은 후보에서 겹칠 수 있으므로
R1/R2/R3 변경쌍의 단순 합은 전체 decision 변경 수와 같지 않다.

### 사람 승인 큐

- 예상 규모: **5,100쌍**
- 현재 데이터에서는 5,100건 모두 `merge_candidate`이며 R4 추가 defer는 0건이다.
  R4 구조와 validator는 앵커 포함 2계열 defer가 생기면 누락 없이 포함하도록 고정됐다.
- 증거 강도 밴드:
  - 이름+firm+이미지 3계열: 2,402
  - 기타 앵커 포함 3계열 이상: 1,799
  - 앵커 포함 2계열: 610
  - 앵커 없는 2계열 이상: 289
- 겹침 가능한 policy flag 부분집합:
  - `phash_veto_review_required`: 229
  - `fanout_review_required`: 236
  - `anchor_missing`: 289
- fan-out group: conflict 123 / exempt 49

## 실행 3 — 기존 gold 회귀 게이트

신규 표본이나 리뷰를 만들지 않고 N100의 동결 gold만 재사용했다. 4개 게이트,
총 33쌍이 모두 PASS했다.

| 게이트 | 결과 |
|---|---|
| B4 name-only 10쌍 reject 유지 | 10/10 PASS |
| WP2.2 지정 국가 오류 4쌍 reject + country conflict 유지 | 4/4 PASS |
| B2 different 4쌍 pHash hard conflict 유지 | 4/4 PASS |
| B1 9쌍 + B3 same 6쌍 사람 승인 큐 승격 | 15/15 PASS |

false relaxation, 미승격, failed case는 모두 0이다.

## 실행 4 — 독립 검증과 테스트

- 독립 validator는 v4 producer를 import하지 않고 v3/E2/N100에서 R1–R5,
  전이, fan-out, 승인 큐, 4개 회귀 게이트를 다시 계산한다.
- 최초 production validation은 **107/111 FAIL**이었다. FAIL 보고서는 보존했다.
  - 111개 전이에서 `r1_before.proposal`만 달랐다. validator가 legacy
    `matcher_architect_year_gate_block`의 `related_but_separate` 분기를 누락했다.
  - WP2.2 4개 binding에서 validator 기대값이
    `country_site_incompatible` hard conflict를 누락했다.
  - 두 validator 오류가 전이/rule-impact와 binding/result 4개 검사를 연쇄 실패시켰다.
- producer, v4 DB, manifest, volume report, gate는 수정하지 않았다. 두 독립 감사도
  산출물이 맞고 validator-only 수정이 안전하다고 확인했다.
- 수정 후 새 no-clobber 보고서로 full-hash validation **111/111 PASS**,
  `failed_checks=[]`를 기록했다.
- WP2/WP2.1/WP2.2/N100/WP2.4 regression: **77 passed**.
- producer, validator, 두 CLI `py_compile`: PASS.

## 실행 5 — fresh N10 금지와 비용

- fresh N10: 0회 / 0쌍
- network / paid API / Vision / LLM: `0 / 0 / 0 / 0`
- 추정 비용: `$0.00`
- merge execution / registry / `vnx_*` ID issuance / Neon / R2: 모두 0

## 주요 artifact

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `data/enrichment/vnext_identity_candidates_v4.db` | 792,211,456 | `5ab133138c26ca6d6df44a0eeec46ad413f3deb06dd2c75b3f9f799f3752d713` |
| `data/reports/vnext_identity_candidates_v4_manifest.json` | 140,345 | `6fd7b5077803666e25fd28e4f44d8e62d4680db3ed5cbf43f319c5c5e7a3881d` |
| `data/reports/vnext_identity_candidates_v4_stage_a_report.json` | 1,646,549 | `60c36e5c1fcee7ab8930cb3b040778751e2dffcb549edae62bc3567e02dfb0a2` |
| `data/reports/vnext_identity_candidates_v4_regression_gate.json` | 3,676 | `d8e37578b94cfef0891e2984b6fc781006ce552b46df6c361d3820fbb0f03242` |
| `data/reports/vnext_identity_candidates_v4_validation.json` (최초 validator 오탐 FAIL 보존) | 208,523 | `888bde767893513e671c4e6c984c184f18bf87143feaff2ab2920f7f48d257aa` |
| `data/reports/vnext_identity_candidates_v4_validation_v2.json` (최종) | 205,936 | `77a9dbbf17c78d6f70b0a7607830b418a81f1d19d0bba52b064841df6300ff29` |

## Stop condition / 다음 단계

WP2.4는 완료됐다. 자동 병합은 허가하지 않는다. 다음 단계는 Claude가 5,100건의 사람
승인 큐 볼륨과 증거 강도 밴드를 검토한 뒤, 필요하면 티어링 방식을 사용자 결정으로
상정하고 S1 registry + 승인 큐 설계를 별도 지시하는 것이다.
