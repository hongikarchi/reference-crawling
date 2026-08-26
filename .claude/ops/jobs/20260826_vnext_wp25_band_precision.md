# WP2.5 밴드별 정밀도 측정

- 상태: **COMPLETE — PASS** (측정만 완료, 티어링 미실행)
- 범위: S1~S5 221쌍 정밀도 측정만 수행
- 금지: merge/ID 발급/Neon/R2 쓰기, 유료 API
- 입력 지시서: `20260826_vnext_wp25_band_precision_order.md`
- 프로토콜 overlay: `docs/vnext/IDENTITY_BAND_PRECISION_PROTOCOL.md`

## 착수 전 quota gate

- 활성/pinned/runtime 계정: multi-auth index `0`
- 직접 응답 header 최종 관측: 2026-08-26 09:23 KST, weekly used 6%, remaining 94%
- multi-auth cache 최종 성공 관측: 2026-08-24, active-account weekly used 37%, remaining 63%
- `/status` CLI: 이 실행 환경에는 Codex CLI가 없어 조회 불가
- gate 적용값: 두 가용 신호 중 보수적인 **remaining 63%**
- 판정: 30% 초과이므로 신규 착수 허용. 20% 이하가 확인되면 전면 중단.
- 예상 부하: N100 리뷰의 약 2배, HTTP 최대 884회, paid API 예상 비용 $0

관측 시각이 다른 두 신호의 한계를 숨기지 않고 기록하며, 각 리뷰 wave 전 최신 가용
신호를 다시 확인한다.

## 고정 정책

- 플래그 3종은 표본·일괄 승인 후보에서 제외하고 개별 리뷰 대상으로 유지한다.
- seed: `vnext-wp25-band-precision-v1-20260826`
- 표본: S1 60 / S2 60 / S3 40 / S4 25 / S5 36 전수
- S1~S4 오류 환산: `error_count * 60 / n`의 실수값을 그대로 사용한다.
  `<=1` bulk 후보, `>1 and <5` agent 1차, `>=5` 직접 리뷰.
- `different >= 1`이면 bulk 후보 자동 제외. S5 `same >= 1`이면 FAIL.

## 입력 pin (사전감사)

| 입력 | bytes | SHA-256 |
|---|---:|---|
| WP2.5 order | 3,517 | `4f4eab39bb9b08746ad8200418d48cc734a9b647456e2e7e810bb335aa684567` |
| v4 DB | 792,211,456 | `5ab133138c26ca6d6df44a0eeec46ad413f3deb06dd2c75b3f9f799f3752d713` |
| v4 manifest | 140,345 | `6fd7b5077803666e25fd28e4f44d8e62d4680db3ed5cbf43f319c5c5e7a3881d` |
| v3 DB | 580,710,400 | `c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f` |
| N100 DB | 2,183,168 | `ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b` |
| identity contract | 9,879 | `502e6258b9c7803ea58bc415ddbe73fbcc50faa0005d8b9aaaaaeab04838e3e8` |
| N100 protocol | 2,735 | `30e6c981ab257b39438769a49e4865309bb958b9db8fcdbf46530bf4d7390f1f` |

## 실행 기록

- 2026-08-26: 지시서·입력 pin·모집단을 읽기 전용 사전감사.
- 플래그는 754 unique(236/229/289), 기리뷰 focal union은 135 unique.
- fresh frame: S1 2,203 / S2 1,485 / S3 464 / S4 97 / S5 36.
- producer/독립 validator 테스트: 63/63 PASS.
- 최신 pristine fixture: sample validator PASS 51/51, 221 unique, logical SHA replay 일치.
- offline fetch/resume E2E: 852 asset occurrences / 823 logical attempts / 442 blind sheets.
- 중단 복구 E2E: r1 lock, unblind, terminal locked/report/manifest를 삭제한 뒤 동일 바이트 복구 PASS.
- 최종 manifest는 sample/fetch ledger·두 reviewer·unblind·adjudication·report 14개 역할의
  path/size/SHA를 결합한다.
- 최종 통계는 스트라텀별 gold 분포, `same/n` 정밀도와 two-sided 95%
  Clopper-Pearson CI, exact 비율 환산 등급을 기록한다.
- production sample freeze PASS:
  - run: `wp25-7886f7ef2935450e31a2bc9d`
  - rows: 221 unique
  - logical SHA: `cf79e2d8a980625b2c1f48be07f1b5d0a57a4b2ec55e578745f4ad35cba507e0`
  - pre-freeze conservative quota gate: 63% remaining, PASS
- production independent sample validator: **PASS 51/51**.
- pre-prepare quota gate: conservative 63% remaining, PASS.
- production current-pixel fetch: **PASS**.
  - asset occurrences 852 / unique 823 / logical 823 / wire 823
  - exact 701 / source-changed 122 / failed 0
  - redirect 0 / retry 0 / cache hit 0
  - read-only cache 4종 before/after tree SHA 불변
  - blind sheets 442, lane별 form 221, hidden-field 문자열 누출 0
- 독립 blind review 2개 완료 및 ledger 동결 후 unblind:
  - R1: same 185 / different 36 / related 0 / insufficient 0
  - R2: same 185 / different 36 / related 0 / insufficient 0
  - pair-level disagreement 0, lock-before-unblind 순서 PASS
- 제3 adjudicator 전수 221/221 완료:
  - same 185 / different 36 / related 0 / insufficient 0
  - unresolved disagreement 0
  - reviewer sheet 442개 path/opaque ID/SHA binding PASS
- quota gate 6회(pre-freeze, prepare, R1, R2, adjudication, finalize):
  모두 conservative remaining 63%, PASS.
- release audit 보강:
  - quota phase/order/freshness fail-closed 및 caller 우회 제거
  - HTTP 요청 전 durable attempt journal, crash 시 no-refetch
  - fetch-attempt 양방향 1:1·status/hash·cache role/path/tree 독립 검증
  - input/cache pin 변경·삭제 방지 trigger

## 최종 측정 결과

| stratum | gold 분포 | precision | Clopper-Pearson 95% CI | 오류 환산(60) | 사전 선언 등급 |
|---|---|---:|---:|---:|---|
| S1 (n=60) | same 60 | 100% | 94.04–100% | 0 | bulk candidate |
| S2 (n=60) | same 60 | 100% | 94.04–100% | 0 | bulk candidate |
| S3 (n=40) | same 40 | 100% | 91.19–100% | 0 | bulk candidate |
| S4 (n=25) | same 25 | 100% | 86.28–100% | 0 | bulk candidate |
| S5 (n=36) | different 36 | same 0% | 0–9.74% | 60 | R1 rule guard PASS |

- WP2.5 terminal status: **PASS**.
- production independent final validator: **PASS 80/80**.
- S1~S4에서 `different` 0, 따라서 자동 bulk exclusion 없음.
- S5 `same` 0: R1의 ≥6년 규칙 PASS, S1 hold 없음.
- 위 등급은 사용자 결정을 위한 후보일 뿐이며 승인·merge·ID 발급을 실행하지 않았다.
- paid API $0, Vision/LLM 0, Neon/R2 write 0.
