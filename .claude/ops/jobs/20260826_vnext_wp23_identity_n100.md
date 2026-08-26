# WP2.3 identity N100 실행 기록 (2026-08-26)

## 범위와 결론

- 지시서: `.claude/ops/jobs/20260826_vnext_wp23_identity_n100_order.md`
- Run ID: `wp23-d0d4c9cdf8b969d7455483af`
- 최종 결과: **PASS**
- 표본은 총 110쌍이다. A70은 게이트용 정밀도 코어이고, B40은 보고 전용 보수성 진단이다.
- 기리뷰 쌍은 fresh 표본 105쌍에서 모두 제외했다. 지시서가 지정한 기존 신호 5쌍만 B의 carry-forward provenance로 포함했으며 새 리뷰에는 노출하지 않았다.
- 파이프라인의 별도 유료 Vision/LLM API 요청과 비용은 `0 / 0 / $0.00`이다. 블라인드 판정에는 이 Codex 세션의 격리 reviewer 2명과 별도 adjudicator를 사용했다.
- merge 실행 / `vnx_*` ID 발급 / registry / Neon / R2 / identity 규칙 변경: 모두 `0`.
- 이 PASS는 자동 병합을 허가하지 않는다. 기존 전건 사람 승인 규칙을 유지한다.

## 구현과 계약

- `canonical/vnext_identity_n100.py`: 표본 동결, HTTP 파생본, 두 리뷰 ledger lock, unblind, 조정, gate/report/manifest를 no-clobber sidecar에 기록한다.
- `canonical/vnext_identity_n100_validator.py`: producer를 import하지 않고 표본·asset 선택·form 원본/완료 hash·리뷰 lock·gold·gate·artifact hash를 독립 재생한다.
- `tools/run_vnext_identity_n100.py`, `tools/validate_vnext_identity_n100.py`: 단계별 실행 및 `sample|final` 검증 CLI다.
- `docs/vnext/IDENTITY_N100_PROTOCOL.md`: 110쌍 해석, B carry 처리, 블라인드/조정, A 게이트, HTTP cap, 불변 입력을 명문화했다.
- v3 후보 DB는 immutable input으로 열었다. 실행 전후 SHA-256은 모두 `c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f`다.
- N100 상태는 별도 sidecar `data/enrichment/vnext_identity_n100_v1.db`에만 기록했다.

## 표본 동결

- 결정론 seed: `vnext-wp23-identity-n100-v1-20260826`
- Sample logical SHA-256: `11e26ab9ff7cab8da264042669c69baa5b158bc5fc537a40f024abdac5d95ed7`
- v3 auto-merge-eligible 모집단: 4,169쌍. 기존 focal review union은 30쌍이며, 그중 19쌍이 A 모집단에서 제외되어 A frame은 4,150쌍이다.
- A70은 anchor 유형과 부수 positive-family mask에 대해 계층형 Hamilton 비례 배분했다.
  - both anchor 45쌍
  - image anchor 24쌍
  - name anchor 1쌍
- B 모집단과 표본:

| Stratum | Full pool | Fresh pool | Carry | Fresh sample |
|---|---:|---:|---:|---:|
| B1 year gate | 86 | 84 | 1 | 9 |
| B2 pHash veto + strict-name anchor | 734 | 732 | 1 | 9 |
| B3 fan-out defer | 223 | 220 | 3 | 7 |
| B4 high-name reject | 3,195 | 3,195 | 0 | 10 |

- 선택된 110쌍은 우연히 endpoint도 전부 분리됐다: distinct endpoints 220, reused endpoints 0. 이는 사후 제약이 아니라 진단값이다.
- 표본 단계 독립 검증: **51/51 PASS**.

## HTTP Stage B

- 105 fresh review pair에서 side당 최대 2개 asset을 선택했다.
- Asset occurrence / unique asset / logical HTTP / wire request: `417 / 417 / 417 / 417`.
- redirects / retries / failures: `0 / 0 / 0`.
- 결과: exact `337`, source_changed `80`.
- 리뷰 sheet: reviewer별 105개, 합계 210개.
- 기존 cache는 read-only SHA pin 전후가 일치했다.
  - WP1: 151 files / 26,909,641 bytes / tree `9bdc3b9b8b37808866d321420fc283a9270db4be021d561bfd756ed4f14983f6`
  - WP2 v1/v2: 73 files / 13,522,544 bytes / tree `b1356938dbbb4a011963cf78e3a3903e9e2ac4ba5d6f2616033f996353b65aa7`
  - WP2.2 v3: 35 files / 4,686,994 bytes / tree `4c99646c1ba084af1a54d44ee434e9b60997b8a78600d51e9b92193d159fe323`
- N100 전용 output cache: 417 files / 79,402,638 bytes / tree `742bac35d70be5eaacc2895b0818c914f86eb9295d6042af8fa6f074d85f78b2`.

## 블라인드 리뷰와 조정

- 격리된 두 reviewer가 proposal, score, rank, cohort, stratum, candidate ID가 숨겨진 105쌍 전체를 각각 판정했다.
- 두 105행 ledger가 모두 hash lock된 뒤에만 unblind했다. 다섯 carry row는 WP2.2 gold와 provenance만 승계했다.
- Reviewer 1: same 70 / related 1 / different 14 / insufficient 20.
- Reviewer 2: same 84 / related 1 / different 10 / insufficient 10.
- 일치 85쌍, 불일치 20쌍. 별도 조정자가 105쌍 전부를 재판정했고, 끝내 해소되지 않은 6쌍은 삭제하지 않고 `insufficient`로 확정했다.
- Fresh 105 최종 gold: same 80 / related 2 / different 10 / insufficient 13.
- 리뷰 완결성: R1 `105/105`, R2 `105/105`, adjudication `105/105`, carry provenance `5/5`.

## A70 게이트

| Gold | Count |
|---|---:|
| same | 66 |
| related | 1 |
| insufficient | 3 |
| different | 0 |

- false merge: `0`
- sampled hard-conflict violation: `0`
- unreviewable: `0`
- exact-identity precision: `66/70 = 94.2857%`
- two-sided Clopper-Pearson 95% CI: `86.0106%–98.4213%`
- Gate: **PASS**. 그러나 99% precision은 확립되지 않았고 `authorizes_auto_merge=false`, `human_approval_required=true`다.

## B40 보수성 진단

`same` 비율을 해당 차단 메커니즘의 false-reject율로 보고한다. Fresh rate가 추론 대상이고, carry를 합친 10쌍 수치는 알려진 신호를 의도적으로 포함한 descriptive 값이다.

| Stratum | Fresh gold | Fresh false-reject | Carry | Combined descriptive |
|---|---|---:|---:|---:|
| B1 year gate | same 8 / insufficient 1 | 8/9 = 88.89% | same 1 | 9/10 = 90% |
| B2 pHash veto | same 3 / different 4 / insufficient 2 | 3/9 = 33.33% | same 1 | 4/10 = 40% |
| B3 fan-out defer | same 3 / related 1 / insufficient 3 | 3/7 = 42.86% | same 3 | 6/10 = 60% |
| B4 high-name reject | different 6 / insufficient 4 | 0/10 = 0% | 0 | 0/10 = 0% |

- 쌍별 positive family, hard conflict, 두 reviewer 근거와 adjudicator 근거를 report의 `evidence_memo`에 보존했다.
- 이 결과만으로 year gate, pHash veto, fan-out 규칙을 바꾸지 않았다.

## 실패 시도와 복구 기록

1. 첫 `prepare`에서 representative asset을 붙일 때 이미 direct asset 두 개가 찬 side에 동일 cap을 다시 적용하지 않아 HTTP preflight가 중단됐다. 실제 network request는 `0`이었다.
   - 실패 DB와 manifest/validation을 `attempt1_prepare_asset_cap_bug` 이름으로 보존했다.
   - side별 cap을 두 선택 단계에 동일 적용하고 regression test를 추가했다.
   - 재생 후 Run ID와 sample logical SHA가 원래 동결값과 같아 표본 교체가 없음을 확인했다.
2. 첫 final validator는 86/92로 여섯 오탐을 냈다.
   - 원인은 validator의 old asset-cap replay, 생성 시점 blank form SHA와 완료 form SHA 혼동, 확장된 B 진단 및 `human_approval_required` 누락이었다.
   - 실패 리포트는 `vnext_identity_n100_v1_validation.attempt1_stale_validator.json`으로 보존했다.
   - producer와 final artifact는 수정하지 않고 validator 기대식만 동형화했다. 별도 read-only 포렌식에서 artifact tamper가 없음을 재확인했다.
   - 최종 독립 full-hash 검증: **92/92 PASS**.

## 검증

- 최종 independent validator: **92/92 PASS**.
- WP2/WP2.1/WP2.2/WP2.3 regression: **51 passed**.
- `py_compile`: producer, validator, 두 CLI 모두 PASS.
- 기존 `.pytest_cache` ACL warning 1건만 있었고 테스트 결과에는 영향이 없다.

## 주요 artifact

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `data/enrichment/vnext_identity_n100_v1.db` | 2,183,168 | `ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b` |
| `data/reports/vnext_identity_n100_v1_sample_manifest.json` | 50,788 | `99eea8c50970dae0e441cc88ac8091dadf03e25af056dfdc08f4056a18f654fc` |
| `data/reports/vnext_identity_n100_v1_sample_validation.json` | 114,039 | `59ec9aeaae0ae64060f23fa4cb57103fd03852541d34136a5bbc834fa0d3cf0d` |
| `data/reports/vnext_identity_n100_v1_fetch_manifest.json` | 198,068 | `f67be669e9d61c81a227e4fcf7284129bcd61fa54af16ddbd49f64ddaa6ec58f` |
| `data/reports/vnext_identity_n100_v1_reviewer_r1_locked.json` | 54,186 | `37918fc40b09752b460b940d9138f26aeb594894786d8430211dca67ed9f9fd6` |
| `data/reports/vnext_identity_n100_v1_reviewer_r2_locked.json` | 54,769 | `3d334833430ff9d1405143dee8861d01251eacd9776e49a9eba55f25d9fb72a1` |
| `data/reports/vnext_identity_n100_v1_adjudication_locked.json` | 63,713 | `c4fec6261cefb6a00c25baea9e36fccb9579e4832fbe52d80fd72fa36e9d3337` |
| `data/reports/vnext_identity_n100_v1_gate.json` | 608 | `d7621684fdd942aed891cd09003b180a7c50fc88ebf856eaf4138f279eabdeee` |
| `data/reports/vnext_identity_n100_v1_report.json` | 102,416 | `ecef9dd59fe7f033f6f510cbb81a3ee84253e4fa714918ac68a15312f65066f6` |
| `data/reports/vnext_identity_n100_v1_manifest.json` | 3,579 | `e4accff5634ce97712f5cb226e9a5557a75aef7699f6d532a6fcf514f2e3e9da` |
| `data/reports/vnext_identity_n100_v1_validation.json` | 519,213 | `bb064d5c9a5434a7560ebd64a9041cc80e6ec8c040b3cc08f04482f55bf188c3` |

## Stop condition / 다음 단계

WP2.3은 완료됐다. 이 실행에서는 규칙을 고치거나 병합하지 않는다. Claude/사용자가 B 진단을 보고 year gate·pHash veto·fan-out 예외의 계약 개정 여부를 결정한 뒤, 별도 지시로 S1 registry와 사람 승인 병합 큐를 설계한다.
