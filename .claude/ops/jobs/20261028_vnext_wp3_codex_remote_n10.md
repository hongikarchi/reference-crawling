# vNext WP3 remote half: codex compact labeler N10 (2026-10-28)

상태: **codex_v1 225장 완료. lane A FAIL (pooled 96.6% ≥ 90%이나 scope 오류 7 + medium 오류 1), lane B PASS
(comparative cover 10/10, critical 0).** Neon/R2 0, git commit 0, 동결 입력·기존 WP3 출력 수정 0.
Claude 이미지 열람 0건 (이미지 판독은 전부 codex).

## 1. 범위 / 결정

- 사용자 결정 2026-10-28: (1) remote 측정 + 학습 pool 진행, 이미지 판독은 Claude가 아니라 codex로 (2) 계약 v1.4
  (legibility 2단계, camera 경계 `unknown`). 계약 sha `40b5470a…b35e`. prompt 동결 직전에 다시 확인함.
- 이번 작업 = 도구 + N10 smoke + 225장 eval. **학습 pool 라벨링(8.0M cap)은 실행하지 않음.**
- 모델: 지시된 `gpt-5.5`는 ChatGPT 계정 codex에서 거부됨(400 `model_not_found`) → **`gpt-6.1-sol`, effort low,
  default tier**(fast/priority 안 씀). codex-cli 0.163.0.

## 2. 도구

- `tools/vnext_s3_codex_engine.py` (engine `vnext_s3_codex_compact` 0.2.0), `tests/test_vnext_s3_codex_engine.py` (4 tests)
  - batch 호출. 라벨 768px / 커버 512px JPEG q80 temp dir은 호출 후 삭제(잔존 0 확인). 파일명은 opaque(`01.jpg`).
  - codex harness 최소화: `--ignore-user-config`, base instructions 교체, agent tool 21종 disable, 빈 cwd, `--ephemeral`,
    read-only. 호출당 overhead 17.5k → **약 5.2k token**.
  - `--output-schema` strict enum + 자체 vocab·§5 applicability 검증. invalid 행은 그 batch의 invalid 이미지만 1회
    재시도하고, 그래도 invalid면 `failed` 행(원문·error code 보존).
  - resume 시 완료 batch/건물 skip, 재실행 request 0 (smoke_b8로 확인).
  - stage ledger(`codex_v1/ledger_eval225.json`, cap 1.5M 누적). 이미지당 token >6,000 또는 retry율 >5%이면 정지.
  - `quota` 서브커맨드: 작은 non-ephemeral 호출을 보내고, rollout에서 `rate_limits`를 읽음.
- prompt sha:
  - label `d30e629e…43da` (smoke 0.1.0 = `7ede239e…46e6`)
  - cover `1866fafe…4222`
  - instructions `c180194f…5e08`
- `tools/vnext_wp3_codex_eval_v1.py`: v1.4 채점은 기존 `tools/vnext_wp3_eval_v14.py` → v1 scorer를 그대로 import해서 씀.

## 3. 결과 (`data/smoke_vnext/wp3_n10_v1/codex_v1/eval.md`)

| 단계 | 결과 |
|---|---|
| smoke N10 (1장/동, 5+5) | b4 2,147 tok/img, b8 1,631 tok/img. 두 batch 크기의 출력은 90/90 동일, retry 0. scope 오류 2(인테리어 over-reject) → in_scope 문구 1회 명확화 후 동결 (그 10장은 경미한 leakage → 215장 수치 별도) |
| 225 labels (b8, 3 workers) | 225/225 ok, retry 0, 281,931 tok (**1,233 tok/img** full batch), 78 s wall |
| 225 lane A (v1.4) | **pooled 96.6%** (smoke 제외 215장 96.5%). 전 필드 ≥91.6% (legib 91.6%, 나머지 ≥96%) |
| scope/medium 명백 오류 | scope **7** (전부 bld_031919: 인테리어 5장을 isolated_product로 reject + out-of-scope 2장 accept), medium **1** (idx 129 photo→rendering) → **lane A FAIL** |
| 비교 local combo | 92.6%, scope 9 / medium 13 |
| cover (comparative 1 call/동, 512px) | **top-1 10/10, top-3 10/10, critical 0 → lane B PASS**. 103,172 tok. codex 라벨 기반 결정론 default는 8/10 crit 2 → 비교 호출이 필요함 |
| local vs codex 일치 신호 | 일치하는 이미지에서 local 정답률 99.5–100% (모든 필드). local scope 9 / medium 13 오류는 **전부 불일치 집합**에 있음. scope/medium 불일치 15.1% → funnel 95.8% |

- 주의: 일치 신호를 쓰려면 codex 라벨이 먼저 있어야 함. 그래서 모집단 라우터로는 쓸 수 없음(그렇게 하면 전수 원격이 되는데 계약 §7이 금지).
  이 신호는 local margin 임계 캘리브레이션과 오류율 추정에만 씀.
- 남은 codex 오류는 1동(bld_031919)의 product/가구 인테리어 경계에 몰려 있음. gold·정의 모호 가능성이 있어 판정자 재확인 필요 (미실시).

## 4. 비용 / 외삽

- eval-225 stage ledger: **481,636 / 1,500,000 token**. 내역:
  - harness probe 49,608
  - smoke 37,784
  - labels 281,931
  - covers 103,172
  - quota probe 2회 9,141
- quota (pro, 주간 window): 사용률 5% → 5%, 크레딧 잔액 변화 0. 측정 간격 동안 385k token을 썼는데 정수 표시값이 그대로임 → 주간 한도는 대략 ≥39M token (반올림 최악 ≥19M). 대략적인 수치임.
- 외삽 (b8, 측정 9,862 tok/call · 8.05 s/call · 3 workers에서 10.3k img/h):
  - cover top-3 확인, 39,248동: 5,673 tok/동 → **222.7M token** (768px면 263.2M). 순차 56 h / 3 workers 19 h
  - 699,038장 중 X% 라우팅:
    - 10% → 86M token, 6.8 h
    - 13% → 112M
    - 20% → 172M
    - 56% → 483M
    - 100% → 862M (계약 §7상 금지)
  - 8.0M pool cap ≈ 6,489장
- 유료 API $0 (ChatGPT pro codex quota 사용)

## 5. 다음 단계 권고

1. bld_031919 scope 7건 + idx 129 판정자 재확인. 이 중 gold 모호가 확인되면 acceptable 보강으로 lane A가 재판정될 수 있음. 아니면 product/가구 인테리어 정의를 계약에 명시해야 함. 정의는 사용자 결정 사항.
2. cover lane: 모집단 top-3 비교 호출은 약 223M token. 주간 한도 추정치(≥19–39M)의 수 배라서 **여러 주에 걸쳐 분할 실행**해야 함.
   또는 결정론 1위 margin이 큰 동은 생략(계약 COVERS §2-3)하고 근소한 동만 비교하는 방식 검토.
3. 학습 pool: 8.0M cap ≈ 6.5k장이므로 rare-class 보강 pool(1–2k장)은 cap 안에 들어감. 승인되면 같은 도구로 실행 가능.

## 6. 산출물

- `tools/vnext_s3_codex_engine.py`
- `tools/vnext_wp3_codex_eval_v1.py`
- `tests/test_vnext_s3_codex_engine.py`
- `data/smoke_vnext/wp3_n10_v1/codex_v1/`:
  - `labels.jsonl`, `cover.jsonl`, `batches.jsonl`, `calls.jsonl`, `run.json`
  - `eval.{md,json}`
  - `ledger_eval225.json`, `quota_probes.jsonl`
  - `smoke_idx.json`, `smoke_b4/`, `smoke_b8/`
  - `stdout_*.txt`
