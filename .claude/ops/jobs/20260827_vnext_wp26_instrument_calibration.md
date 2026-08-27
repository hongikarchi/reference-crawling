# WP2.6 리뷰 계측기 교정 probe

- 상태: **COMPLETE — 계측 gate FAIL**
- 범위: Step 0 form 비교 → Step 1 seeded 24행 → Step 2 사전 선언 gate
- 금지: 티어 승격, merge, ID 발급, Neon/R2 write, 유료 API
- 지시서: `20260827_vnext_wp26_instrument_calibration_order.md`
- 프로토콜: `docs/vnext/IDENTITY_INSTRUMENT_PROBE_PROTOCOL.md`

## 착수 전 quota gate

- 실행 환경의 최신 직접 response header: 2026-08-26 20:09:40 KST
- plan/account: Pro, 기존 active multi-auth index `0`
- weekly window: 10,080분, used 21%, **remaining 79%**
- Codex CLI: 현재 PATH에 없어 `/status` 직접 조회 불가
- 한계: 현재 turn 자체의 header는 스트림 종료 전 노출되지 않아, 동일 실행 환경에서
  확인 가능한 최신 성공 응답을 사용한다.
- 판정: 30% 초과이므로 착수 허용. 20% 이하 확인 시 전면 중단.
- 최대 HTTP 96회, paid API 예상 비용 $0

## Step 0 — 실제 form/payload 비교

| 항목 | N100 실제값 | WP2.5 실제값 | 결론 |
|---|---|---|---|
| top-level JSON keys | 8개 공통 keys | 동일 | 구조 차이 없음 |
| item JSON keys | display/order/opaque/sheet + label/rationale/conflict | 동일 | 구조 차이 없음 |
| 축별 JSON key/DB column | 없음 | 없음 | 7축은 독립 필드가 아님 |
| 실제 rationale의 7축 태그 | 210/210 모두 7개 순서대로 존재 | 0/442 | 운영 작성 규율이 소실됨 |
| rationale 길이 | R1 175–310, R2 190–311자 | R1 56–110, R2 51–144자 | WP2.5가 훨씬 짧은 자유서술 |
| 실제 label 사용 | 네 라벨 모두 사용 | same 370 / different 72, 중간 라벨 0 | 중간 라벨 감도 의심 |
| hard conflict 값 | `none` 또는 축:상세 | `yes`/`no` | WP2.5 audit 정보가 더 약함 |
| blank/completed 보존 | draft 경로가 completed로 사용됨 | blank와 completed 분리 | WP2.5가 provenance는 더 강함 |

실제 공통 item schema는 `display_order`, `opaque_review_id`, `sheet_path`,
`sheet_sha256`, `label`, `rationale`, `hard_conflict_observed`이다. Form contract는
앞의 네 binding 필드만 묶고, 두 validator 모두 `rationale`의 7축 태그를 강제하지 않는다.
따라서 원인 가설은 **컬럼 누락이 아니라 N100에서 사용한 축별 작성 규율의 소실**이다.
이번 probe는 WP2.5 계측기 자체를 재므로 이 규율을 복원하지 않는다.

## Step 1/2 사전 동결

- sample version: `vnext-identity-instrument-probe-v1`
- seed: `vnext-wp26-instrument-probe-v1-20260827`
- rank: `SHA256(sample_version + NUL + seed + NUL + group + NUL + candidate_id)`
- P1/P2/P3 = 지시서의 완전 control set, D = A70 same 66건의 seed 상위 15건
- WP2.5 221쌍과 교집합 0, 24 candidate/sample ID 전역 unique, 사후 교체 0
- PASS: P1 non-same ≥2/3 AND P2+P3 non-same ≥3/6 AND D same ≥13/15
- 결과 공개 순서: R1 lock → R2 lock → blind adjudication lock → N100 gold reveal/score

## 입력 pin 사전감사

| 입력 | bytes | SHA-256 |
|---|---:|---|
| WP2.6 order | 2,948 | `e7a2446c2798e86ac196fa081a41bfb3c6882b0b6922672c7ab83d37634f38c1` |
| WP2.5 producer | 153,786 | `fe0072b9440eb080f039990c65c6a882d635df1bf0021f5d6a98bf977a558bfa` |
| WP2.5 protocol | 2,952 | `fdc4f91334c81115b001caa69923cbdbfeec025a956a27f29804f6cb8541f725` |
| v4 DB | 792,211,456 | `5ab133138c26ca6d6df44a0eeec46ad413f3deb06dd2c75b3f9f799f3752d713` |
| N100 DB | 2,183,168 | `ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b` |
| N100 manifest | 3,579 | `e4accff5634ce97712f5cb226e9a5557a75aef7699f6d532a6fcf514f2e3e9da` |
| N100 adjudication lock | 63,713 | `c4fec6261cefb6a00c25baea9e36fccb9579e4832fbe52d80fd72fa36e9d3337` |
| WP2.5 DB | 7,491,584 | `8ada59fc36c026c15b54afb5815fae240e98eea3083a52ff1e933770ce4fa85a` |
| WP2.5 manifest | 11,768 | `a7b29eeb48f1dfcbae0c6831080011d9e4d706e02628e91a14ab9186049ff129` |

## 실행 기록

- 2026-08-27: Step 0 actual JSON/SQLite payload audit 완료, network 0.
- 2026-08-27: P1/P2/P3/D 모집단 read-only 재계산 완료. P1=3, P2=4,
  P3=2, D universe=66, WP2.5 overlap=0.
- 2026-08-27: Step 1 sample freeze 완료. run
  `wp26-250a8cefe5396b87377ad55d`, 24행, logical SHA
  `784a567458227a737c78b31320eb79fdfbca0e7cf5909d08bb87c38f12e2b208`.
- 2026-08-27: Step 2 gate를 sample freeze 뒤 별도 명령으로 동결. contract SHA
  `649b156b0d7b767bf89bce96324ae4ca15940d1cced42867979fb39b6e3f1f44`.
- 2026-08-27: blind evidence 준비. asset occurrence/unique 93/93, 기존 cache hit 76,
  HTTP logical/wire 17/17. 17건은 모두 `connection` 실패로 image unavailable 처리,
  redirect/retry 0, 상한 96 미만.
- 2026-08-27: R1 lock → R2 lock → 제3자 adjudication lock 순서 완료.
  R1=`same 17 / related 4 / different 3 / insufficient 0`,
  R2=`same 19 / related 3 / different 1 / insufficient 1`,
  최종=`same 17 / related 4 / different 3 / insufficient 0`.
  R1/R2 불일치 3행, unresolved 0행.
- 2026-08-27: 모든 ledger lock 뒤 N100 gold reveal 및 사전 gate 판정 완료.

## 최종 판정

| 그룹 | frozen N100 gold → 최종 probe | gate 집계 | 기준 | 결과 |
|---|---|---:|---:|---|
| P1 | insufficient→same 2, related→related 1 | non-same 1/3 | ≥2/3 | **FAIL** |
| P2 | insufficient→different 1, insufficient→related 2, related→related 1 | non-same 4/4 | P2+P3 합산 | PASS |
| P3 | insufficient→different 2 | non-same 2/2 | P2+P3 ≥3/6 | PASS |
| D | same→same 15 | same 15/15 | ≥13/15 | PASS |

전체 gate는 세 조건의 AND이므로 **FAIL**이다. `wp25_s1_s4_grades_valid=false`,
`authorizes_tiering=false`로 기록됐으며 WP2.5 S1–S4 등급은 무효다.

지시서에 따라 후속 실행은 자동 시작하지 않았다. 다음 작업은 별도 발행 시에만
WP2.5 instrument v2를 만들고, 최소 `scope=` 관찰 강제, 네 label 정의 재명시,
“사진 일치만으로 동일 건축물 판정 금지”를 포함한 뒤 기존 221행을 재사용하지 않는
fresh disjoint 표본으로 S1–S4를 다시 측정한다.

## 검증·산출물

- sample freeze + gate 독립 validator: 47/47 PASS
- prepared evidence 독립 validator: 47/47 PASS
- terminal full-hash 독립 validator: **72/72 PASS**
  (`data/reports/vnext_identity_instrument_probe_v1_final_validation_v4.json`)
- 최종 회귀: WP2.6 producer/validator + pristine WP2.5 producer/validator
  **111 passed**, `py_compile` PASS
- terminal DB SHA:
  `a0949e7d75069d869e665a10872fe13a3988897cfc810d36149508950594347a`
- report SHA:
  `b1c04f394488624d248ce31965a6fbd01345bb5ffb51cfdfa2f9053e925980ab`
- manifest SHA:
  `162be33c250638248e3b03f80644adb883a7a562cba0a36b99fde350c801859f`
- 실행 당시 producer SHA:
  `8e12aa23980ad17c1f7c724f8a99eb47f4e2844eee660a2d374a3436c032285e`
- 실행 후 Windows manifest 경로의 POSIX canonicalization, legacy terminal recovery,
  future fetch-manifest 상한 96 최초 기록을 코드에 보강. 현재 producer SHA:
  `53ab52464aa0990e0748c1177544007ba97a8930a98d3e16263a72c5d3e93449`.
  terminal DB/report/manifest는 수정하지 않았다.
- release audit에서 intermediate fetch manifest가 재사용한 WP2.5 literal 상한 884를
  기록한 것을 발견했다. 실제 enforcement·DB·terminal report는 96이었다. 원본을
  덮지 않고 `data/reports/vnext_identity_instrument_probe_v1_fetch_cap_correction.json`
  overlay를 발행했다. overlay SHA는
  `3a2540bb0ab3adf79cd119bfdd42f769c6a21bf8817ab4600e5d033e7c4301c5`,
  corrected projection SHA는
  `439426d0706c7eac1b922af9fb8f042fb7bf3cb514cda809363daef7c0fc72b3`이다.
  validator는 legacy 884일 때 이 overlay의 원본/terminal/producer/count/projection
  binding이 모두 맞아야만 통과한다.
- validator 수정 전 생성된 `*_validation.json`, `*_prepared_validation.json`,
  `*_final_validation.json`은 진단용 FAIL 기록이다. `*_prepared_validation_v2.json`은
  prepare 수정을 확인했고, `*_final_validation_v4.json`이 현재 코드와 full-hash
  기준의 권위 있는 최종 검증이다.

## 비용·외부 변경

- quota 관찰 6회 모두 remaining 79%로 기록. 현재 stream header 비노출 때문에
  2026-08-26 20:09:40 KST의 동일 active account index `0` 관찰을 재사용했다.
- Vision/LLM/paid API 0, merge 0, ID 발급 0, tier promotion 0,
  Neon/R2 write 0.
