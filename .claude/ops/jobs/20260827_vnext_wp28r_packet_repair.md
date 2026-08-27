# WP2.8-R 패킷 복구 후 동일 계측기 재probe

- 상태: **종료 — TERMINAL FAIL**
- 지시서: `.claude/ops/jobs/20260827_vnext_wp28r_packet_repair_order.md`
- 범위: Stage A 결손 17건 진단·복구 → Stage B 자산 완전성 사전 검증 →
  Stage C 동일 24 seed blind 재probe → Stage D WP2.8 동일 게이트
- 예상 네트워크: 기존 cache hit 76건, 재fetch 최대 **17 logical / 17 wire HTTP**,
  redirect 0, retry 0
- 예상 유료 비용: **$0**
- 금지: 계측기 v2.1 파일·계약·게이트·builder·renderer 변경, 유료 API,
  merge, ID 발급, tier promotion, Neon/R2 write

## 착수 사전감사

- 최신 사용 가능한 direct response header: `2026-08-27T07:07:12Z`
- plan/account: Pro, active/pinned multi-auth index `0`
- weekly window: 10,080분, used 40%, **remaining 60%**
- 착수 시점에 24시간 freshness 안이며 `>30%` wave gate를 통과한다.
- WP2.6 원본 DB/cache/sheet는 read-only로 보존하고 복구본은 `v2` 새 경로만 쓴다.
- 계획된 source-repair/final-v2 경로는 착수 전 모두 비어 있었다.

## 불변 계측기·입력 pin

| 파일 | bytes | SHA-256 |
|---|---:|---|
| WP2.8-R order | 3,532 | `6411c7498b2c9ac2840723340b29bcdce26d38adaeb322e7c130f910c11647bf` |
| v2.1 protocol | 6,541 | `a459ebdde66389dc15d983cd97597ba57929a30c71ac02435da987cb60cbdf44` |
| v2.1 producer | 98,886 | `94241af175c40b5dab903ba04b38314f74cd3a64e6c07fe5095b06a64e984939` |
| v2.1 validator | 69,329 | `b6b62b4bb1c441cd3ebe533097ae0aaed9ad0ab42174cfec55c0cacf2f4e1aec` |
| v2.1 runner CLI | 9,287 | `a8f898b85eb8afffc3d0c6c44ae5b41f7787ff38e5e7d20658a31d5a030c7e43` |
| v2.1 validator CLI | 2,039 | `3749db5c6da822fdfe2112f5ea6528f65834da1355680e55bcbfdc035a5b25ef` |
| frozen WP2.5 builder/renderer | 153,786 | `fe0072b9440eb080f039990c65c6a882d635df1bf0021f5d6a98bf977a558bfa` |
| frozen WP2.6 packet producer | 122,514 | `53ab52464aa0990e0748c1177544007ba97a8930a98d3e16263a72c5d3e93449` |
| WP2.6 original DB | 716,800 | `a0949e7d75069d869e665a10872fe13a3988897cfc810d36149508950594347a` |
| WP2.8 terminal DB | 1,929,216 | `93cb178e0992d29b6eb413b28193d6796497c28abfca576ff0fcacfe5ac58b85` |
| WP2.8 report | 154,082 | `e057da7297a58cbd1fbbe380d876b15ab311d6fa2ee5385a331c963026544596` |
| WP2.8 manifest | 7,722 | `c7293876927b993bcf7528166857c9b16321d73ccf8706f0af49b8e5ab5c6001` |

## Stage A 진단 동결

- WP2.6 failed fetch는 **17/17 모두** Architizer 우측 자산이다.
- 모두 `request_count=1`, redirect `0`, retry `0`, HTTP 응답 없음,
  `error_kind=connection`, `WinError 10013`이다.
- 13 seed에 분포한다: D 14자산/10 seed, P2 2자산/2 seed, P3 1자산/1 seed.
- WP2.8 D image 실패를 만든 완전 결손 4 seed는 `probe_0559...`,
  `probe_0d66...`, `probe_8e91...`, `probe_b315...`다.
- `probe_440b72...` P1은 fetch 실패가 아니라 builder가 우측 자산을 0개 선택한
  행이므로 복구 대상이 아니며 Stage B에서 `excluded_no_assets`로 처리한다.

## Stage A — 17건 복구

- 새 source packet DB: `data/enrichment/vnext_identity_instrument_probe_v2.db`
- 새 cache: `data/enrichment/vnext_identity_instrument_probe_v2_cache/`
- 원본 WP2.6 DB/cache/sheet는 쓰지 않고 새 경로에만 생성했다.
- 기존 76건은 cache hit, 실패했던 17건만 HTTP로 다시 받았다.
- 결과: `source_changed 17/17`, 잔여 실패 0, pixel drift 17건은 지시서대로 사용.
- 네트워크: logical 17 / wire 17 / redirect 0 / retry 0.
- 17건은 모두 Architizer 우측 자산이고 원래 실패는 `connection`,
  `WinError 10013`, `request_count=1`, HTTP 응답 없음이었다.

| group | 복구 자산 | 영향 seed |
|---|---:|---:|
| D | 14 | 10 |
| P2 | 2 | 2 |
| P3 | 1 | 1 |
| 합계 | **17** | **13** |

복구 자산의 seed별 목록은 최종 report의 `asset_recovery` 17행에 asset key,
source asset ID, side, 원래 오류, 새 fetch 상태 및 새 raw/pixel/cache SHA와 함께 전수 싣는다.

## Stage B — 자산 완전성 사전 검증

- 24 seed × 좌/우 = 48측을 전수 검사했다.
- 47측은 사용 가능 자산이 1개 이상이다.
- `probe_440b72d0af93f2c5b0fbd38e`의 우측만 selected 0 / usable 0이다.
  fetch 결손이 아니라 원래 builder 선택 결과이므로 자산을 임의 추가하지 않았다.
- 이 seed는 행 자체를 유지하고 `excluded_no_assets`로만 기록하며 모든 gate 채점에서 제외한다.
- 유효 분모/기존 비율의 최소 통과 수: D image 15/14, D scope 15/14,
  N100 uncertain 4/3(과반), P1 2/2(기존 2/3 비율의 보수적 정수 적용).
- base gate payload SHA `a5adf46a63c5ce141f528be269a041f1ae9c492d4d6007afc42e6f8eed9ac18b`
  자체는 변경하지 않고, 분모 조정은 별도 repair overlay로 동결한다.

정식 Stage B 실행:

- final run ID: `wp28-9127e9f8cddfc8a5c40bc188`
- 상태 전이: `vocabulary_frozen → derivation_verified → instrument_frozen →
  sample_frozen → criteria_frozen`
- base gate는 `counterfactual_unadjusted_raw` / `decision_authority=0`,
  effective gate는 `wp28r_authoritative` / `decision_authority=1`로 폼 발행 전에 동결했다.
- WP2.8-R order도 3,532 bytes / SHA
  `6411c7498b2c9ac2840723340b29bcdce26d38adaeb322e7c130f910c11647bf`로 고정했다.
- 초기화 후 독립 validator: **PASS 19/19**.
- recovery 재실행, no-clobber, v1→v2 허용 delta whitelist, 경로 충돌 검사를 포함한
  producer focused tests: **9 passed**.

## Stage C — 동일 24 seed blind 재probe

- `pre_r1_review` quota 60%를 기록한 뒤 24행씩 R1/R2 폼을 발행했다.
- 폼 발행 상태에서 독립 validator: **PASS 19/19**.
- reviewer actor는 이전 결과를 전달받지 않은 별도 무상태 격리 세션이며,
  protocol + 각 lane draft + 해당 24개 sheet만 열람한다.

정식 blind sequence:

1. `pre_r1_review` quota 기록 → R1 24/24 작성·잠금
2. `pre_r2_review` quota 기록 → R2 24/24 작성·잠금
3. `pre_adjudication_review` quota 기록 → adjudication 24/24 작성·잠금
4. 잠금 전수감사 → `pre_reveal_score` quota 기록 → reveal 1회

- R1 actor: `codex-wp28r-r1-isolated`; unreadable 0
- R2 actor: `codex-wp28r-r2-isolated`; unreadable 0
- adjudicator actor: `codex-wp28r-adjudicator-isolated`; unreadable 0
- 세 actor는 서로 다르고, reveal 전 reviewer observation 48행 / review lock 2개 /
  adjudication 24행 / adjudication lock 1개가 모두 존재했다.
- R1 lock SHA: `8bcef9130c8713fd06b2fe2195a2cf989d734cfef12cfb9568a537ca351fc0ab`
- R2 lock SHA: `6d1b95db31f430fa6ee751eed09220d01629a6ba7e31d9528cbd5b48db6af9f5`
- adjudication lock SHA:
  `6f78e2f17bfb9ab816175b0c2d6b7a9e8c816d1d63717fea7ba3dcd79e0fb390`

운영상 참고: 최초 R2 worker 한 세션을 `pre_r2_review` 기록 전에 잘못 시작했지만,
완료 파일이나 lock을 만들기 전에 즉시 중단하고 결과를 폐기했다. 그 뒤 quota를 먼저 기록하고
새 격리 actor로 R2를 처음부터 수행했으며, DB와 최종 report에는 이 정식 결과만 수용했다.

## Stage D — WP2.8 동일 게이트

| authoritative predicate | actual / denominator | threshold | 결과 |
|---|---:|---:|---|
| D image `same_object` | 15 / 15 | 14 | PASS |
| D scope `same` | 14 / 15 | 14 | PASS |
| N100 uncertain image non-same | 4 / 4 | 3 | PASS |
| P1 derived label non-same | **1 / 2** | **2** | **FAIL** |

- 최종 authoritative status: **FAIL**.
- `probe_440b72d0af93f2c5b0fbd38e`는 `excluded_no_assets`이며 모든 authoritative
  predicate 모집단에서 일관되게 제외됐다.
- 남은 P1 두 행 중 `probe_5250172d158dc8aee6af02e3`만 `related`이고,
  `probe_c519ad58a048859bb6d3570d`는 `same`으로 판정되어 non-same은 1/2다.
- 제외 행을 포함하는 immutable core raw replay는 2/3으로 PASS지만
  `counterfactual_unadjusted_raw`, `decision_authority=0`이다.
- 자산 완전성에 맞춘 effective gate만 `wp28r_authoritative`,
  `decision_authority=1`이며 그 결과가 위 FAIL이다.
- WP2.8의 D image 11/15는 결손 복구 후 15/15로 회복됐다. 따라서 이번 실패는
  패킷 결손이 아니라 완전 패킷에서 드러난 P1 계측 결과다.
- `failure_is_terminal=true`, `authorizes_wp29=false`, `authorizes_tiering=false`.
  지시서대로 재시도하지 않았고 WP2.9를 생성·실행하지 않았다. 계측기 작업은 여기서 종료한다.

## 불변성·비용·외부 쓰기

- v2.1 protocol / producer / validator SHA는 착수 pin과 최종 독립 validator의
  static pin에서 각각 `a459ebdd...`, `94241af1...`, `b6b62b4b...`로 동일하다.
- WP2.5 renderer와 WP2.6 packet builder도 각각 `fe0072b9...`, `53ab5246...`로 동일하다.
- WP2.6 원본 DB는 716,800 bytes /
  `a0949e7d75069d869e665a10872fe13a3988897cfc810d36149508950594347a`로 불변이다.
- repair fetch만 logical 17 / wire 17 HTTP를 사용했다. 계측 단계의 추가 네트워크는 0이다.
- LLM 0, Vision 0, paid API 0, merge 0, ID 발급 0, tier promotion 0,
  Neon write 0, R2 write 0. 비용은 **$0**이다.

## 최종 산출물

| 산출물 | bytes | SHA-256 |
|---|---:|---|
| repaired WP2.6 DB | 614,400 | `298ee5ad6727f16bb113fe76cc3a080d53ab88d69ca028b74a1b31bbad35de16` |
| repair fetch manifest | 47,665 | `c33f8da1a9f9b34b97aaede317fca723bdd86af80b3205419b17e658e13b1c39` |
| v2.1 source projection DB | 299,008 | `40be7ae30b7f0a176f416b2241f25fc12aa036dd049ad1509dfb00fb7aef48a0` |
| source projection manifest | 1,922 | `18347d6eaf0dc0b6f00cf04781c8d8b43221589f413315c8e52a371651636b6a` |
| final probe DB | 1,994,752 | `355f89d6898d9b129772e1e8a99e21959184452ad67bf56f31d4a3e78bc15c4e` |
| sample manifest | 30,126 | `ad0b79813d7a03edd47c0cfd788f5a83a433f59d683b119472d1332f794263f2` |
| core report | 142,321 | `67cd8f0d940683d5ea3690253d77db2ffd1f9aef28015d573fa9735f2eb4a141` |
| core manifest | 7,745 | `e9309b8675cf99677d9b1e2e1a01fce508217af4f313dc2f5a0f5c7ece6c4e79` |
| authoritative report | 115,777 | `0d93787c833f10577a6baeec873ae5f6d277d7963e3a83ab3def9be31b8d7255` |
| authoritative manifest | 5,371 | `79865ff4669c4542921663e6c6df811aa17c51af71121a5a06c0a8dfbbd750ed` |
| independent validation | 9,170 | `4c6fcc00321e18956aa4557e77e7babe4ddf2948b5b62a0806f5898f174c8c5b` |

## 검증

- repair producer focused tests: **9 passed**
- repair validator focused tests: **12 passed**
- WP2.5~WP2.8 및 WP2.8-R 전체 회귀: **204 passed**
- 최종 독립 validator: **PASS 23/23**
- SQLite `integrity_check=ok`, foreign-key violation 0
- 별도 read-only terminal audit: **GO** — DB/report/manifest의 authoritative FAIL,
  gate 재계산, blind 순서, quota 순서, 불변 SHA, zero-operation ledger가 모두 일치했다.

## 결론

Stage A의 결손 17건은 허용된 경로에서 정확히 복구됐고 Stage B 완전성·Stage C blind
절차도 통과했다. 그러나 완전 패킷을 WP2.8과 동일한 계측기로 재측정한 Stage D에서
P1 정밀도 게이트가 1/2로 실패했다. 이는 지시서가 정의한 **terminal FAIL**이며,
추가 계측기 개정·재probe·티어 승격은 승인하지 않는다.
