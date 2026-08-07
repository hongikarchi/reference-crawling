# Architizer shared E1 N100

## 범위와 판정

Architizer metadata는 immutable 완료본
`data/curated/architizer_curated_v2_0.db`를 그대로 사용했다. metadata DB를
재빌드하거나 수정하지 않았고, 공통 E1 image fingerprint 실행기의 실제
Architizer N10/N100만 새 sidecar 경로로 검증했다.

이번 단계에서 N1000, full, Vision/LLM 의미 분석, Neon, R2, vector DB는
실행하지 않았다. 판정은 **전수 실행 직전 준비 완료**다.

## 입력과 inventory

- DB 크기: `8,767,438,848 bytes`
- DB SHA-256:
  `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc`
- metadata corpus: project `61,970`, building `61,912`, firm `8,486`
- image source total: `884,773`
- eligible: `884,317`
- excluded: `456`
- source inventory manifest SHA-256:
  `f9d1db8f600996419518210e323b74fb0ff124c52b2da6fdecfe647251913741`
- exclusion manifest SHA-256:
  `fc6e8916421b17a8089f4f5bdddba85fd04a4c9ffc8c7121fb56025be90b62ce`

456건은 운영 DB의 `placeholder_candidate` 규칙에 따른 candidate exclusion이다.
모두 시각적으로 확정된 placeholder라는 뜻은 아니며, 행 단위 ledger에
보존하고 source-policy open QA로 남긴다. eligible URL은 strict Imgix host와
1024px fetch 변환 계약을 모두 통과했다.

## Offline 검증

- 실제 `STRICT` Architizer 이미지 테이블의 부가 열을 반영한 fixture 추가
- E1 관련 테스트: `76 passed`
- bounded worker와 단일 writer: pass
- 전역 rate limiter, Retry-After, cooldown, circuit breaker: pass
- attempt 즉시 commit과 누적 retry budget resume: pass
- 5,000행 initialization resume: pass
- stale lock, process-death lock release, hot journal recovery: pass
- 10,000개 이상 inventory bounded-memory 처리: pass
- exclusion 전수 회계, ordered manifest 결정성, 변조 탐지: pass
- no-clobber와 완료본 resume 요청 0: pass
- Architizer adapter 운영 DB 전수 순회:
  `884,773 = 884,317 eligible + 456 excluded`, 정렬/중복 오류 `0`
- repository-wide pytest:
  `644 passed, 22 skipped, 1,453 subtests passed`
- `py_compile` 및 `git diff --check`: pass

## 실제 N10

- artifact: `data/smoke/architizer_e1_common_n10_fullrunner_v1.db`
- 크기: `1,159,168 bytes`
- artifact SHA-256:
  `ad394ef178c9ba9d8024edf5fec69f27810d7fd9dccc5027451d00064f1f57a0`
- ordered selection manifest SHA-256:
  `04e74bb53958c84c6007f6e151f94a8d681476ef6d6a71f5ae78ccd3d68ca0da`
- run status: `complete`
- fingerprint: `10 success / 0 failed`
- HTTP: `10 × 200`, retry `0`
- response bytes: `1,227,860`
- elapsed: 평균 `613.8 ms`, 최소 `366 ms`, 최대 `1,151 ms`
- request-start span: `5.478 seconds`
- runner wall time: `465.5 seconds`
- 독립 validator wall time: `99.8 seconds`
- SQLite quick/integrity/FK와 required validation: pass
- source DB 실행 전후 SHA 동일: pass
- 완료본 resume: `already_complete=true`, network requests `0`
- 영구 이미지 파일: `0`

## 실제 N100

- artifact: `data/smoke/architizer_e1_common_n100_fullrunner_v1.db`
- 크기: `1,634,304 bytes`
- artifact SHA-256:
  `ca5b0bca181ddbb341c7bb119c3ec4e30c9e8961db8a35759665949814785052`
- ordered selection manifest SHA-256:
  `fe9fef768dbde90384c45b874c92c7ea2c69ecfe6b59a4e51919d67e07be8aa8`
- run status: `complete`
- fingerprint: `100 success / 0 failed`
- HTTP: `100 × 200`, retry `0`
- response bytes: `14,819,157`
- elapsed: 평균 `527.1 ms`, 최소 `6 ms`, 최대 `1,064 ms`
- request-start span: `54.175 seconds` (`1.827 requests/second`)
- worker 분배: `25 / 25 / 25 / 25`
- runner wall time: `514.3 seconds`
- 독립 validator wall time: `99.1 seconds`
- SQLite quick/integrity/FK와 required validation: pass
- inventory/exclusion/source-record/manifest mismatch: `0`
- source DB 실행 전후 SHA 동일: pass
- 완료본 resume: `already_complete=true`, network requests `0`
- 영구 이미지 파일: `0`

## 전수 추정과 gate

4 workers, 사이트 전체 2 requests/second 조건의 N100 실측을 사용했다.

- first-attempt 요청: `884,317`
- 이론적 rate-limit floor: `122.8 hours`
- N100 request-span point estimate: `134.4 hours`
- 운영 계획 범위: `135-140 hours` (`5.6-5.8 days`)
- 예상 다운로드: `131.048 GB` (`122.048 GiB`)
- 예상 sidecar: `4.670 GB` (`4.349 GiB`)

retry, redirect, 429/5xx cooldown, response-size tail, SQLite page/index 성장과
terminal 검증 때문에 실제 값은 증가할 수 있다. 입력 SHA와 inventory를 매번
재검산하는 고정 비용도 smoke에서 수분으로 관찰됐다.

Architizer와 Divisare 모두 metadata 입력, 공통 E1 offline 계약, 실제 N10,
실제 N100, 독립 validator, 완료본 resume 요청 0까지 완료됐다. 두 source 모두
N1000/full은 수행하지 않았으며, 다음 단계는 사용자 승인 후 source별 전수
실행이다.

## 금지 범위 확인

- metadata/curated DB 수정: `0`
- Vision/LLM 호출: `0`
- N1000/full 실행: `0`
- Neon/R2/vector DB 작업: `0`
- 영구 이미지 저장: `0`
