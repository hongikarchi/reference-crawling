# WP2.8 계측기 v2.1 관찰층 보정

- 상태: **COMPLETE — terminal FAIL**
- 지시서: `.claude/ops/jobs/20260827_vnext_wp28_instrument_v21_order.md`
- 범위: Stage A 어휘 추출 → Stage B 오프라인 파생 검증 → Stage C 중립 지시문 동결 → Stage D 동일 24 seed blind 재probe
- 금지: 티어 승격, merge, ID 발급, Neon/R2 write, 외부 Vision/LLM·유료 API, 증분 HTTP/cache write/renderer substitution

## 착수 사전감사

- 최신 확인 가능한 direct response header: 2026-08-26 20:09:40 KST
- plan/account: Pro, active multi-auth index `0`
- weekly window: 10,080분, used 21%, **remaining 79%**
- 2026-08-27 14:37 KST 기준 24시간 freshness 안이다.
- quota 계약: `>30%` 새 wave 허용, `<=30%` 금지, `<=20%` sticky stop.
- 예상 외부 비용: `$0`; WP2.8 incremental network `0`.

| 입력 | bytes | SHA-256 |
|---|---:|---|
| WP2.8 order | 4,291 | `1c57a6f2dca384c1ea84db671c8fbb0f150adf76e37707cee5871a1de3267ff9` |
| N100 DB | 2,183,168 | `ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b` |
| N100 manifest | 3,579 | `e4accff5634ce97712f5cb226e9a5557a75aef7699f6d532a6fcf514f2e3e9da` |
| N100 R1 lock | 54,186 | `37918fc40b09752b460b940d9138f26aeb594894786d8430211dca67ed9f9fd6` |
| N100 R2 lock | 54,769 | `3d334833430ff9d1405143dee8861d01251eacd9776e49a9eba55f25d9fb72a1` |
| N100 adjudication lock | 63,713 | `c4fec6261cefb6a00c25baea9e36fccb9579e4832fbe52d80fd72fa36e9d3337` |
| WP2.7 terminal DB | 1,695,744 | `f31ee6558e43c9b0b412f2c3cda964100acffe0763f9995d301fb32865dece65` |
| WP2.7 report | 118,309 | `c880ee628023e9d8386ebe41bb14b551bf6e6e61c9424e8b084e31b132c7d7ac` |
| WP2.7 manifest | 8,868 | `477357a1a752e83768c505f764028e9be67fbfa5624c2d006798ea47bfc82525` |

입력 SQLite에 WAL/SHM/journal sidecar는 없고, 계획된 `vnext_identity_instrument_v21_probe_v1*` 경로는 모두 비어 있다.

## 측정 전 해석 동결

- N100에는 adjudicated 축이 별도 저장되지 않았으므로 두 reviewer 축의 순서불변 합의 함수를 Stage B 입력으로 사용한다.
- 두 값이 같으면 유지한다. 다르면 축별 고정 우선순위로 하나를 고른다.
  - image: `unavailable > inconclusive > different_objects > same_object`
  - name: `conflict > alias > agree > unclear`
  - firm: `conflict > overlap > agree > unclear`
  - place: `conflict > agree > unclear`
  - year: `conflict > compatible > unclear`
  - scope: `unrelated > related_separate > same > unclear`
- 파생 함수는 다음 first-match 5규칙이다.
  1. image가 `unavailable|inconclusive`면 `insufficient`
  2. image가 `different_objects` 또는 scope가 `unrelated`면 `different`
  3. scope가 `related_separate`면 `related`
  4. image가 `same_object`이고 scope가 `same`이면 `same`
  5. 나머지는 `insufficient`
- Gate B denominator는 fresh adjudicated pair 105개이며 PASS 최소치는 `100/105`다. carry 5행과 raw reviewer 관찰 210개는 denominator가 아니다.
- Stage D gate 3은 위 합의축에서 N100 image가 `inconclusive|unavailable`인 동일 seed를 사용한다. 고정 모집단은 5쌍, threshold는 non-`same_object` `>=3/5`다.
- non-`same_object`는 `different_objects|inconclusive|unavailable` 세 값이다.
- Stage C 및 actor form 발행은 Gate B PASS 뒤에만 가능하다.

## 실행 기록

- 2026-08-27: order/N100/WP2.7 입력 pin, N100 rationale 210행 구조, 출력 경로, quota를 read-only 사전감사했다.
- 실행 run ID: `wp28-9127e9f8cddfc8a5c40bc188`
- 최종 시각: 2026-08-27 15:08:09 KST (`2026-08-27T06:08:09.514410Z`)

## Stage A — 실제 어휘 추출

- N100 `blind_reviews.rationale`를 고정 parser로 전수 읽었다: **210/210행**, nonblank
  `basis` **210/210행**.
- 아래 값 외에는 controlled vocabulary로 추가하지 않았다.

| 축 | 실제 값별 빈도 |
|---|---|
| image | `same_object 171`, `different_objects 24`, `inconclusive 12`, `unavailable 3` |
| name | `agree 129`, `alias 73`, `conflict 4`, `unclear 4` |
| firm | `agree 147`, `overlap 24`, `conflict 37`, `unclear 2` |
| place | `agree 25`, `conflict 1`, `unclear 184` |
| year | `compatible 162`, `conflict 31`, `unclear 17` |
| scope | `same 155`, `related_separate 2`, `unclear 28`, `unrelated 25` |

## Stage B — 파생 함수 검증

- 고정 합의 함수와 first-match 5규칙을 N100 fresh gold 105쌍에 오프라인 재생했다.
- 결과: **102/105 = 97.1429%**, 최소 `100/105`를 충족해 **Gate B PASS**.
- 규칙별 적용 건수: `1: 11`, `2: 10`, `3: 1`, `4: 83`, `5: 0`.
- 불일치 3쌍은 행별 예외 없이 그대로 보존했다.
  - `n100_5c76a0ad9d55e899c66ffb8a`: predicted `same`, gold `related`
  - `n100_b8398cb6c0618c78b88a1576`: predicted `same`, gold `insufficient`
  - `n100_c8789d856720d9cb7c7c2a5d`: predicted `same`, gold `insufficient`

## Stage C — 관찰 지시문 동결

- Gate B PASS 뒤에만 v2.1 protocol을 동결했다.
- 여섯 축을 중립적으로 정의했고, 부분 뷰·각도 차이·적은 이미지 수만으로는
  `inconclusive`가 아니며, 한쪽에 사용 가능한 이미지가 전혀 없을 때만
  `unavailable`이라고 명시했다.
- protocol: 6,541 bytes / SHA-256
  `a459ebdde66389dc15d983cd97597ba57929a30c71ac02435da987cb60cbdf44`

## Stage D — 동일 24 seed 재probe

- WP2.6/WP2.7의 동일 sample/sheet/asset을 byte-for-byte read-only 재사용했다:
  **24 seed / 48 sheet / 93 asset occurrence**.
- source packet binding SHA-256:
  `b1f0491852d68648405c032e1a8486ed41a939b18eb655cea1aab92ab07b39e5`
- visual inventory SHA-256:
  `0cdf2b6b42b9a4866a2e9ee0cb37e5efb9c045915ce83e67d350569749bbe99e`
- R1, R2, 축 판정자는 서로 다른 격리 actor이며 각각 24/24행을 잠갔다.
  - R1 lock: `da66b58eeef39a10d6959779aa7aec61d8887a143d5378b0ab2356a5836d3d4c`
  - R2 lock: `e6ddee9cb0cad331260d9a347e2ad881d362b1604c19617022c66233349ed728`
  - adjudication lock: `15a5931b56bf821a407917400428c5d2a441cecad693e07e6a422863acc3c983`
- 공개 직전 상태는 `adjudication_locked`, review observation 48행, adjudicated
  observation 24행, control reveal 0행, gate score 0행이었다. 그 뒤 한 번만 공개했다.

| 사전 선언 게이트 | 결과 | 기준 | 판정 |
|---|---:|---:|---|
| D image `same_object` | **11/15** | >=14/15 | **FAIL** |
| D scope `same` | 14/15 | >=14/15 | PASS |
| N100 uncertain seed image non-`same_object` | 5/5 | >=3/5 | PASS |
| P1 파생 label non-`same` | 3/3 | >=2/3 | PASS |

네 조건을 AND로 결합하므로 최종 판정은 **FAIL**이다. 사후 threshold 변경, 재리뷰,
재설계 또는 WP2.9 자동 실행은 하지 않았다.

## 실패 원인

실패한 D image 4쌍은 다음과 같다.

- `probe_0559ad035e3a8c449d1373fb`
- `probe_0d669cc6f66a20a8487822bb`
- `probe_8e91548b412fc597c0fa9c92`
- `probe_b315bad119ab283981b82a07`

네 쌍 모두 왼쪽 Divisare 이미지 2장은 cache hit였지만 오른쪽 Architizer 이미지
2장씩, 총 8개가 WP2.6 source-generation 때 `WinError 10013` connection failure를
기록했다. 따라서 동결 sheet의 오른쪽에는 사용 가능한 이미지가 전혀 없었다. v2.1
정의에 따라 네 쌍을 `image=unavailable`로 판정한 것은 일관되며, 이 때문에 D image의
달성 가능한 값이 이번 packet에서는 11/15였다. WP2.8은 network 0 계약이므로 누락
이미지를 다시 받지 않았다.

즉 이번 FAIL은 점수 계산 오류가 아니라 **동결 packet의 이미지 가용성 결손과 새
`unavailable` 정의가 사전 게이트에서 충돌한 측정 결과**다.

## 검증

- 독립 validator: **PASS 23/23**
- 별도 read-only release audit: **FINAL PASS**, terminal FAIL 판정 유효, 구현 결함 없음
- WP2.5~WP2.8 회귀 suite 8개: **183 passed in 12.61s**
- producer/validator/CLI 4파일 `py_compile`: **PASS**
- incremental HTTP/wire request/retry/redirect/cache write/renderer substitution: **0**
- Vision/LLM/paid API/merge/ID 발급/tier promotion/Neon write/R2 write: 모두 **0**
- 기존 N100 DB SHA와 WP2.7 DB SHA는 공개 직전 pin과 동일했다.

## 최종 산출물

| 파일 | bytes | SHA-256 |
|---|---:|---|
| `data/enrichment/vnext_identity_instrument_v21_probe_v1.db` | 1,929,216 | `93cb178e0992d29b6eb413b28193d6796497c28abfca576ff0fcacfe5ac58b85` |
| `data/reports/vnext_identity_instrument_v21_probe_v1_report.json` | 154,082 | `e057da7297a58cbd1fbbe380d876b15ab311d6fa2ee5385a331c963026544596` |
| `data/reports/vnext_identity_instrument_v21_probe_v1_manifest.json` | 7,722 | `c7293876927b993bcf7528166857c9b16321d73ccf8706f0af49b8e5ab5c6001` |
| `data/reports/vnext_identity_instrument_v21_probe_v1_validation.json` | 4,220 | `6d9af40e6e9b469631cafd42fcbfcfec36ac6744a125b13b344b2491bb9c751c` |

데이터 산출물은 `data/` ignore 정책에 따라 커밋 대상이 아니며 로컬 durable artifact로
보존한다.
