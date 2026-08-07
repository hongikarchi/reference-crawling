# Divisare shared E1 full-runner N100

## Scope

공통 E1 이미지 fingerprint 실행기를 Divisare 전수 처리에 사용할 수 있도록
보강하고, offline 테스트와 실제 N10/N100 smoke까지만 검증했다.

포함 범위:

- 기본 worker 4개, 최대 8개의 제한된 병렬 fetch
- worker별 HTTP session과 main-thread 단일 SQLite writer
- worker/batch 크기에만 비례하는 bounded pending queue
- 사이트 전체 기본 2 requests/second 제한
- `Retry-After`, exponential backoff, cooldown 및 circuit breaker
- attempt별 durable commit, 누적 retry budget 및 정확한 resume
- OS advisory lock, stale lock 및 hot-journal recovery
- 5,000행 단위 resumable initialization
- O(1) 메모리의 정렬 ID 검증
- 행 단위 exclusion ledger와 독립 validator
- no-clobber, source DB 불변, 완료본 resume 요청 0

제외 범위:

- Vision/LLM 이미지 의미 분석
- N1000 및 Divisare full 실행
- Architizer 실제 이미지 다운로드
- Neon, R2, vector DB 및 curated DB 수정

외부 LLM/Vision 비용: `$0`.
LLM/Vision API 호출: `0`.

## Input

- 로컬 DB: `data/curated/divisare_metadata_v2_4.db`
- 크기: `2,225,299,456 bytes`
- SHA-256:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- source inventory 회계:
  `547,252 = 547,229 eligible + 23 excluded`
- inventory manifest SHA-256:
  `e802d8e84611b954ceae13c7f2960b32c7d227b78d6492b0175736c2c0d012bc`
- exclusion manifest SHA-256:
  `27e5e39662bfadd5d39055c359469f23c6ccc4a1c093a2183a5e10e7ff37772f`

모든 실제 smoke에서 source DB의 실행 전후 SHA는 기대 SHA와 같았다. 입력
DB는 read-only로 사용했으며 `VACUUM`, overwrite 또는 데이터 변경을 하지
않았다.

## Implementation summary

- 네트워크 fetch는 bounded worker에서 수행하고, SQLite mutation은 main
  thread 한 곳에서만 수행한다.
- main writer는 완료된 HTTP attempt batch 전체를 각각 별도 transaction으로
  먼저 commit한 뒤 fingerprint를 계산하고 terminal 결과를 기록한다. 따라서
  decode 중 프로세스가 종료돼도 attempt 번호와 누적 retry budget이 보존된다.
- `MAX(attempt_no)+1`과 immutable `max_attempts`/retry-policy provenance로
  resume하며, 저장된 retry deadline의 남은 시간도 복원한다.
- 전역 limiter는 mutex 밖에서 cancellable wait 후 재검사하여 다른 worker가
  추가한 cooldown을 반영한다. 연속 429/5xx가 임계값에 도달하면 stop event로
  신규 scheduling과 대기 중 retry를 중단하고 미처리 fingerprint를 pending으로
  유지한다.
- initialization 진행률과 inventory/exclusion ledger를 5,000행마다 commit하여
  중단 후 이어서 처리한다.
- 독립 validator가 single run, 필수 validation, source-record SHA, ordered
  selection manifest, source inventory 및 exclusion 회계를 다시 계산한다.
- terminal 상태는 `complete`, `complete_with_failures`,
  `failed_validation`으로 분리한다.
- source-neutral 계약과 Architizer adapter 호환성은 offline fixture로만
  검증했으며 Architizer 네트워크 요청은 실행하지 않았다.

1024px 요청, 로컬 512px RGB 정규화, response SHA-256, normalized-pixel
SHA-256 및 256-bit pHash 계약과 기준은 변경하지 않았다.

## Offline verification

- 관련 offline 테스트: `76 passed`
- 병렬 worker 상한과 단일 writer: pass
- fake clock 기반 2 rps 및 `Retry-After`/cooldown: pass
- 4-worker circuit breaker와 pending 보존: pass
- initialization 중단/재개: pass
- attempt 번호와 누적 retry budget 보존: pass
- stale advisory lock 및 hot-journal recovery: pass
- 10,000개 이상 inventory bounded-memory 처리: pass
- exclusion 전수 회계: pass
- ordered manifest 결정성과 변조 탐지: pass
- no-clobber 및 완료본 resume 요청 0: pass
- Architizer adapter offline compatibility: pass

초기 repository-wide pytest의 24건 실패는 E1 회귀가 아니라 ignored production
artifact 부재와 Windows 기본 인코딩/줄바꿈 차이였다. 다음처럼 환경을 고정했다.

- exact ignored production artifact가 없는 경우 그 artifact에 직접 묶인 22개
  테스트만 skip하고, synthetic 단위 테스트는 계속 수집·실행
- tracked frozen JSON 2개는 `.gitattributes`의 `-text`와 Git blob의 LF byte로 고정
- strict canonical JSON 입출력은 locale 기본값 대신 UTF-8을 명시

최종 repository-wide pytest는
`644 passed, 22 skipped, 1,453 subtests passed`로 통과했다. skip 22건은 원본
production artifact가 돌아오면 자동으로 다시 실행된다.

## Actual N10

- artifact: `data/smoke/divisare_e1_common_n10_fullrunner_v3.db`
- 크기: `196,608 bytes`
- artifact SHA-256:
  `c6e38af1073adc4d77e50991111bfdeb399a6c7f5de251ea5b43e25ab4849911`
- ordered selection manifest SHA-256:
  `c2278c6d124f9447940f07255e93bbd4925ddf86e943d5b1ae4f76d5d5367757`
- run status: `complete`
- fingerprint: `10 success / 0 failed / 0 skipped`
- HTTP: `10 × 200`, retry `0`
- network requests: `10`
- response bytes: `842,083`
- request span: `4.879 seconds`
- wall time: `248.7 seconds`
- SQLite `quick_check`: `ok`
- SQLite `integrity_check`: `ok`
- foreign-key violations: `0`
- independent required validator: pass
- source-record SHA 및 ordered manifest 재계산: pass
- source DB 실행 전후 SHA 동일: pass
- 완료본 resume network requests: `0`
- 영구 이미지 파일 보존: `0`

최종 v3 smoke의 네트워크 요청은 N10 10건 + N100 100건이다. 두 durability
수정 전에 수행한 superseded v1/v2 smoke가 각각 110건이므로 이번 작업 중 실제
이미지 HTTP 요청 총계는 330건이다. 모두 N10/N100 범위였고 모든 완료본 resume
요청은 0건이었다.

## Actual N100

- artifact: `data/smoke/divisare_e1_common_n100_fullrunner_v3.db`
- 크기: `684,032 bytes`
- artifact SHA-256:
  `e00b83c08438ccbdc58a17457d35b0e2e6b43639d5a6601d86ae78320372bac3`
- ordered selection manifest SHA-256:
  `bbb6455532837e9d31a7e566abd9ed924ab25208244307b9171c856949ccbba7`
- run status: `complete_with_failures`
- fingerprint: `99 success / 1 failed / 0 skipped`
- HTTP: `99 × 200`, `1 × 404`
- HTTP 404는 non-retryable이며 retry `0`
  - `divisare|524215|1615728785`
- network requests: `100`
- response bytes: `8,203,635`
- request span: `53.263 seconds` (`1.877 requests/second`)
- wall time: `294.6 seconds`
- SQLite `quick_check`: `ok`
- SQLite `integrity_check`: `ok`
- foreign-key violations: `0`
- independent required validator: pass
- source-record SHA 및 ordered manifest 재계산: pass
- inventory/exclusion ledger mismatch: `0`
- source DB 실행 전후 SHA 동일: pass
- 완료본 resume network requests: `0`
- 영구 이미지 파일 보존: `0`

## Full estimate and gate

Divisare eligible inventory 전체를 4 workers와 사이트 전체 2 requests/second
제한으로 실행할 경우의 현재 추정치는 다음과 같다.

- 기본 first-attempt 요청: `547,229 requests`
- 예상 소요 시간: 이론상 하한 `76.0 hours`, 최종 N100 request-span
  point estimate `81–82 hours`, 계획 범위 `81–84 hours`
- 예상 다운로드: `44.893 GB` (`41.810 GiB`)
- 예상 sidecar: `2.964 GB` (`2.760 GiB`)

요청 수는 redirect와 retry가 발생하면 늘어나고, `Retry-After`, cooldown 및
circuit-breaker 후 resume 시간은 위 추정에 추가된다. N1000과 full은 이번
작업에서 실행하지 않았으며 별도 사용자 승인 전에는 실행하지 않는다.

## Remaining risks

- 최종 N100의 HTTP 404 비율은 `1/100`; 앞선 동일 표본에서는 `2/100`이어서
  CDN/source 가변성이 확인됐다. 모집단 전체의 실제 실패율로 단정할 수
  없으며 full에서는 `complete_with_failures`와 exclusion/error 회계를 계속
  확인해야 한다.
- CDN 응답 크기, redirect, 429/5xx 및 `Retry-After` 분포에 따라 실제 시간과
  다운로드량이 달라질 수 있다.
- 2 requests/second는 이론상 약 76시간의 하한이므로 재시작, 검증 및 입력
  inventory/SHA 재계산 시간은 추가될 수 있다.
- sidecar 크기 추정은 N100 선형 외삽이며 index, 실패 상세 및 SQLite page
  배치 효과에 따라 달라질 수 있다. 실행 시 journal과 검증용 여유 공간도
  별도로 확보해야 한다.
- worker가 HTTP를 끝낸 직후부터 main writer가 결과를 harvest/commit하기 전까지
  프로세스가 hard-kill되면 최대 in-flight 4건은 ledger에 남지 않을 수 있다.
  harvested batch는 decode 전에 모두 durable commit된다.
- pHash는 duplicate candidate filter이며 이미지 의미 분류나 동일-building
  판정이 아니다. Vision/LLM 분석은 이번 결과에 포함되지 않는다.
