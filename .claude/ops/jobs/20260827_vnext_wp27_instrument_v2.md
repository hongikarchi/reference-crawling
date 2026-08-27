# WP2.7 리뷰 계측기 v2 동일-seed 대조 probe

- 상태: **COMPLETE — 계측 gate FAIL**
- 범위: Stage A 계측기 v2 사양 → Stage B 동일 24 seed 동결 → Stage C gate 사전 동결 → blind 측정
- 금지: 티어 승격, merge, ID 발급, Neon/R2 write, 외부 Vision/LLM·유료 API
- 지시서: `20260827_vnext_wp27_instrument_v2_order.md`
- 프로토콜: `docs/vnext/IDENTITY_INSTRUMENT_V2_PROTOCOL.md`

## 착수 quota gate

- 최신 확인 가능한 direct response header: 2026-08-26 20:09:40 KST
- plan/account: Pro, active multi-auth index `0`
- weekly window: 10,080분, used 21%, **remaining 79%**
- 확인 시각 기준 24시간 freshness 안이며 2026-08-27 20:09:40 KST에 만료
- 한계: 현재 stream의 header는 종료 전 노출되지 않고 Codex CLI도 PATH에 없어,
  동일 환경에서 확인 가능한 최신 성공 응답을 사용한다.
- 판정: 30% 초과이므로 착수 허용. 30% 이하는 새 wave 금지, 20% 이하는 sticky stop.
- 예상 비용: 외부 API $0, WP2.7 incremental HTTP 0

## Stage 해석

지시서의 A→B→C 순서와 C의 “측정 전 선언”을 함께 만족시키기 위해 다음으로 고정한다.

1. A: controlled enum 6개 + 필수 `basis`, 파생 규칙과 reviewer 지시문 동결
2. B: WP2.6의 동일 24행과 48 sheet binding만 동결; 아직 form 배포·리뷰 0
3. C: 네 threshold를 별도 immutable event로 동결
4. 그 뒤에만 R1 → R2 → 축-only adjudication → gold/v1 reveal·score

## 동일 입력 사전감사

- WP2.6 run: `wp26-250a8cefe5396b87377ad55d`
- source sample version: `vnext-identity-instrument-probe-v1`
- source seed: `vnext-wp26-instrument-probe-v1-20260827`
- 24행 logical SHA:
  `784a567458227a737c78b31320eb79fdfbca0e7cf5909d08bb87c38f12e2b208`
- sheet inventory: r1/r2 각 24, 총 48, bytes 24,517,369
- canonical inventory SHA:
  `0cdf2b6b42b9a4866a2e9ee0cb37e5efb9c045915ce83e67d350569749bbe99e`
- full packet binding SHA `(lane, order, opaque ID, sample ID, path, SHA, bytes)`:
  `b1f0491852d68648405c032e1a8486ed41a939b18eb655cea1aab92ab07b39e5`
- source fetch: 93 unique/occurrence, cache hit 76, connection failed 17,
  logical/wire 17/17, retry/redirect 0
- WP2.7에서는 실패 17건도 재시도하지 않는다. incremental fetch/wire/cache write 0.
- 제안된 DB/report/form/lock 경로는 전부 FREE, 기존 `instrument_probe_v1*`은 read-only input.

## 주요 입력 pin

| 입력 | bytes | SHA-256 |
|---|---:|---|
| WP2.7 order | 4,508 | `38618519893c15199ca38c817ceb6bc0a5e0f47e275544274d6d8310cd5c8599` |
| WP2.7 protocol (Stage A freeze target) | 7,411 | `ea610cf3fdc86f1bc6c65852fde0c47fc5a4ec92fae48055ea545c183da7660a` |
| WP2.6 terminal DB | 716,800 | `a0949e7d75069d869e665a10872fe13a3988897cfc810d36149508950594347a` |
| WP2.6 sample manifest | 20,523 | `6e925f7f07c21fe29d66c0e92a498db81616b189a2b33d5cf2520281d20df06a` |
| WP2.6 adjudication lock | 13,415 | `9bc4f38a4e18933e3ab77fc3e812ab29dd1bd7c4d79af30da760dab27c42a80a` |
| WP2.6 report | 19,671 | `b1c04f394488624d248ce31965a6fbd01345bb5ffb51cfdfa2f9053e925980ab` |
| WP2.6 manifest | 11,232 | `162be33c250638248e3b03f80644adb883a7a562cba0a36b99fde350c801859f` |
| fetch correction | 2,523 | `3a2540bb0ab3adf79cd119bfdd42f769c6a21bf8817ab4600e5d033e7c4301c5` |
| WP2.6 producer | 122,514 | `53ab52464aa0990e0748c1177544007ba97a8930a98d3e16263a72c5d3e93449` |

## 사전 선언 gate

- D same ≥13/15
- P1 non-same ≥2/3
- N100 insufficient 7행 중 v2 insufficient ≥4/7
- N100 related 2행 중 v2 related ≥1/2
- 네 조건 AND. 하나라도 미달하면 terminal FAIL 및 자동 후속 0.

## 실행 기록

- 2026-08-27: 지시서, WP2.6 terminal·sample·sheet·cache·correction read-only preflight 완료.
- Stage A: protocol 7,411 bytes / SHA
  `ea610cf3fdc86f1bc6c65852fde0c47fc5a4ec92fae48055ea545c183da7660a`와
  label-free 6축 + 필수 `basis` 계측기를 `instrument_frozen`으로 동결.
- Stage B: WP2.6의 동일 24행, 48 sheet, 93 asset, 17 fetch attempt, 5 cache pin을
  read-only replay. packet/inventory SHA와 sheet 24,517,369 bytes 일치, 증분 HTTP 0.
- Stage C: form 발행 전 `pre_form_count=0`, `pre_review_count=0`,
  `pre_adjudication_count=0`에서 네 gate를 SHA
  `d6df9fbb9cb480638c460c210878e3fe4ea5d8f4ffaf8b207f08d3742934c80b`로 동결.
- R1/R2는 서로 격리된 무상태 reviewer가 각 24/24 sheet를 전수 확인. 판독 불가 0,
  actor distinct, 공란·금지 label 필드 0. 이후 별도 무상태 adjudicator가 동일 24행의
  축만 재확인하고 ledger 동결.
- 세 ledger 동결 전 `control_reveals=0`, `gate_scores=0` 확인 후에만 gold/v1 reveal·score.

## 최종 판정

| gate | actual | threshold | 결과 |
|---|---:|---:|---|
| D same | 10/15 | ≥13/15 | **FAIL** |
| P1 non-same | 3/3 | ≥2/3 | PASS |
| gold insufficient → v2 insufficient | 5/7 | ≥4/7 | PASS |
| gold related → v2 related | 2/2 | ≥1/2 | PASS |

네 조건의 AND이므로 terminal **FAIL**이다. label 분포는 v1
`same 17 / related 4 / different 3 / insufficient 0`에서 v2
`same 10 / related 5 / different 0 / insufficient 9`로 바뀌었다. v1의 기권 불능은
해소됐지만 D 과교정 방지 gate를 통과하지 못했다.

실패 원인은 결정론 규칙 1의 과도한 veto다. D의 gold `same` 15행 중 4행은 한쪽
이미지가 전부 unavailable이라 firm/name 등 메타데이터가 강하게 일치해도
`image=inconclusive → insufficient`가 우선 적용됐다. 나머지 1행은 동일 사진을 공유하지만
`McGee Art Pavilion` 대 상위 `School of Art & Design`으로 관찰되어
`scope=component_vs_umbrella → related`가 됐다. 따라서 abstention 회복과 related 분리는
개선됐으나 same recall은 10/15로 낮아졌다. 지시서에 따라 사후 threshold 변경, 재리뷰,
추가 probe, WP2.8 자동 진행은 하지 않는다.

## 검증

- sample validator 최초 진단: 20개 check 중 18개 PASS, overall FAIL. 원인은 validator가 Stage C까지 동결된 sample
  상태에서 A/B 2개 event만 기대하고, WP2.6의 역사적 `write_only` cache provenance를
  WP2.7 write로 오인한 것. 데이터·review 변경 없이 validator만 수정 후
  `*_validation_v2.json` **20/20 PASS**.
- final validator 최초 진단: 31개 check 중 30개 PASS, overall FAIL. 원인은 protocol이 요구한 manifest의
  `inherited_fetch` provenance를 validator 예상 payload가 빠뜨린 것. terminal report/DB를
  변경하지 않고 validator만 수정 후 `*_final_validation_v2.json` **31/31 PASS**.
- release audit에서 향후 실행 경계 사례 4건을 추가 보강했다: 동일 phase quota 재확인,
  actor 비정규 문자열 거부, review 출력 경로 alias의 DB mutation 전 차단, 손상 DB·malformed
  reveal의 fail-closed validator 보고. 이 보강은 terminal DB/event/report/manifest를 변경하지 않았다.
- 보강된 독립 validator version은 `vnext-identity-instrument-v2-probe-independent-validator-v2`이며,
  authoritative `*_final_validation_v4.json`은 **31/31 PASS**다.
- 별도 정적 release 재감사도 네 경계 계약의 producer/validator 일치와 남은 blocker 0을 확인했다.
- WP2.5 + WP2.6 + WP2.7 producer/validator 회귀: **168 passed**, py_compile PASS.
- 테스트는 workspace 내부의 새 basetemp와 cache 비활성화로 실행해 기존 ACL 잔여물에 영향을 주지 않았다.

## 핵심 산출물

| 산출물 | bytes | SHA-256 |
|---|---:|---|
| terminal DB | 1,695,744 | `f31ee6558e43c9b0b412f2c3cda964100acffe0763f9995d301fb32865dece65` |
| sample manifest | 30,058 | `bf78b15d00e66f6e20bcc891a70dc4055e5c96d8bdb54c988bb0ae351fb89e4d` |
| terminal report | 118,309 | `c880ee628023e9d8386ebe41bb14b551bf6e6e61c9424e8b084e31b132c7d7ac` |
| terminal manifest | 8,868 | `477357a1a752e83768c505f764028e9be67fbfa5624c2d006798ea47bfc82525` |
| authoritative sample validation | 2,865 | `8170403eadf21ee0f742222e8c6f72356849e05266d1f254413d94787420b2f6` |
| authoritative final validation v4 | 5,405 | `112d88edf98af4293bca8ea8a790368d100c32ea16d561261a3845a6670b3f35` |

## 비용·외부 변경

- quota observation 5회: 모두 account index 0, remaining 79%, freshness 만료 전.
- WP2.7 incremental HTTP/wire/retry/redirect/cache write/renderer substitution: 전부 0.
- Vision/LLM/paid API request, merge, ID issuance, tier promotion, Neon/R2 write: 전부 0.
- 다음 단계는 별도 지시가 있어야 하며 이 job에서는 정지.
