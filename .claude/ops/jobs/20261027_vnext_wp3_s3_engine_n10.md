# vNext WP3: S3 local 엔진 N10 평가 (2026-10-27)

상태: **WP3 N10: lane A FAIL, lane B FAIL** (local half만, config v1 동결). Neon/R2 0, codex/유료 LLM·Vision 0
(**비용 $0**), 동결 입력 수정 0, 커밋 안 함. remote(compact Vision) half는 **미실행**(비용 추정 + 사용자 승인 필요).

## 1. 범위

- plan §2 P4B / §3 gate: local SigLIP zero-shot 엔진을 같은 N10 gold 표본으로 평가 → 엔진 선택·임계값 캘리브레이션 근거
- 계약: `CONTRACT_GALLERY_SEMANTICS.md` §3-§5(9 compact 필드, applicability), `CONTRACT_COVERS.md`(default/critical)
- 출력 dir: `data/smoke_vnext/wp3_n10_v1/`

## 2. 환경 / 엔진

| 항목 | 값 |
|---|---|
| vision venv | `C:/Users/user/venvs/vnext-vision` (repo `.venv`와 분리), Py 3.12.13, torch 2.11.0+cu128, transformers 5.21.0 |
| GPU | RTX 5070 Ti 16 GB (cap 12.0), fp16, batch 32, peak VRAM 2.77 GB |
| 모델 | `google/siglip2-so400m-patch14-384` snapshot `e8e48729…`, fallback 없음 |
| 엔진 | `tools/vnext_s3_local_engine.py` + config v1 sha `eed0a4f7…a43af` (튜닝 안 함) |
| 평가 | `tools/vnext_wp3_eval.py` (sha `c66c0db7…b12c`) → `eval_report.{json,md}` |

## 3. 표본 / gold

- manifest `vnext-wp3-n10-sample-rule-v1`: 10동 / 225장 (Divisare 5/119, Architizer 5/106), fetch 225/225 ok (retry 1건, 재실행 0-request)
- gold: 블라인드 1인 + 판정자 재확인(plan §3). 판정 404건 = gold_correct 377 / both_acceptable 27 / **local_correct 0**
- **결함**: 제출 gold에 chunk_1이 2회, chunk_4 누락 → **173장 / 8동만 채점**. bld_016671·bld_006875(52장) 미채점,
  local default 49·87은 미검증
- 입력 무결성: gold의 local_labels/local_run SHA = 현 파일 SHA (일치)

## 4. 필드별 결과 (173장, 수용 = gold primary 또는 판정 acceptable)

| 필드 | 수용 | 주 오류 (gold→local) | scope+medium 정답 130장 기준 |
|---|---:|---|---:|
| in_scope | 89.6% | in-scope를 `unreadable_or_blank`로 reject ×13 | - |
| reject_reason | 87.3% | none→unreadable_or_blank ×14 | - |
| medium | 80.9% | photograph→rendering ×23, drawing→rendering ×5 | - |
| spatial_context | 84.4% | interior→N/A ×12 (scope 오류 전파) | 95.4% |
| framing_scale | 73.4% | overall→element_detail ×15 | 84.6% |
| camera_angle | **45.1%** | eye_level→elevated ×72 | 52.3% |
| drawing_kind | 86.1% | N/A→perspective ×18 (medium 오류 전파) | 99.2% |
| project_state | 80.3% | visibly_finished→N/A ×30 (medium 전파), →ruin ×4 | 96.9% |
| project_legibility | **59.0%** | high→medium ×30 | 72.3% |
| **pooled 9축** | **76.2%** (1,187/1,557) | | |

## 5. 게이트 (plan §3 N10)

| lane | 기준 | 결과 | 판정 |
|---|---|---|---|
| A 라벨 | scope/medium 명백 오류 0, 9축 ≥90% | scope 18 + medium 33 (전부 판정자 gold_correct 확인), 76.2% | **FAIL** |
| B 커버 | critical 0, default acceptable 10/10 (top-3 병기) | critical **0**, top-1 6/10 (채점 6/8), top-3 6/10 | **FAIL** |

- lane B miss 2건 = 오선택이 아니라 NULL(review_required): bld_002180(외관 사진 11/12를 rendering 오분류),
  bld_031153(외관 overall 144/145/172를 element_detail+elevated 오분류). 나머지 2동은 gold 없음 → 10/10 입증 불가

## 6. margin 신뢰도 → remote 라우팅 (in-sample, 낙관적)

| 필드 | AUROC | auto ≥95% 위해 라우팅할 X |
|---|---:|---:|
| in_scope / reject_reason | 0.88 / 0.90 | 17.9% / 18.5% |
| project_state | 0.93 | 24.3% |
| spatial_context | 0.83 | 42.2% |
| medium | 0.73 | 74.6% |
| framing_scale | 0.75 | 80.9% |
| drawing_kind | 0.50 | 93.6% (chained min(scope,medium) 52.0%) |
| camera_angle / project_legibility | 0.59 / 0.54 | **도달 불가** (margin 무정보) |

- 이미지 단위 min-margin 라우팅: 90% 라우팅해도 pooled 88.3% → **도달 불가**
- 필드별 X95 합집합 98.3% (사실상 전수 원격 = 계약 §7 금지) / local-viable 4필드만이면 54.3%

## 7. 속도 / 전수 외삽 (stream-and-discard, 이미지 디스크 보관 안 함)

| 모수 | 장수 | GPU (73.35 img/s) | fetch 순차 1.3 s | 4 rps | 6 rps | 전송량 | 라벨 출력 |
|---|---:|---:|---:|---:|---:|---:|---:|
| S6-lite building_images | 699,038 | 2.65 h | 252 h | 48.5 h | 32.4 h | ~309 GB | 1.8 GB |
| S2 gallery groups | 1,416,641 | 5.36 h | 512 h | 98.4 h | 65.6 h | ~626 GB | 3.7 GB |

- 병목은 fetch (GPU 가동률 4-6 rps 기준 5-8%). 디스크 상주 = in-flight 버퍼(~100 MB) + 라벨 출력뿐
- 전송량은 표본 평균 442 KB 기준(Divisare 698 KB / Architizer 155 KB) — 실제 모수 비율로 재추정 필요
- remote 승격량(config v1, in-sample): local-viable 4필드 기준 54.3% → **~38만 S6장 / ~77만 S2 group** + 커버 후보
  39,248동 × top-3 = 117,744장. remote 단가는 미측정 ($ TBD)

## 8. 결론 / 다음 단계 권고

- **config v1은 퍼널 전제(local 전수 + 소수 승격)를 충족 못함.** 오류가 scope/medium에서 시작해 하류로 전파
  (scope+medium 정답 이미지에선 spatial 95% / project_state 97% / drawing_kind 99%)
- local 후보 필드: `in_scope`/`reject_reason`, `spatial_context`, `project_state`, `drawing_kind`(medium 정답 전제) — margin 라우팅 가능
- local 부적합: `camera_angle`, `project_legibility` (margin 무정보 → remote/compact Vision 또는 `unknown` 정책), `framing_scale`·`medium`은 승격률 75-81%로 비경제적
- plan §4 규칙: 프롬프트 재튜닝 반복 대신 **구조 변경** 권고 — SigLIP 임베딩 위 소형 linear probe(이 N10 gold를 dev로만 사용),
  photo vs rendering·unreadable 판정 분리, cover default는 photo+exterior+overall 후보 내 비교 Vision(P5)
- 선행 작업: ① chunk_4 gold(52장) 보충 → 2동 lane B 확인 ② WP3 remote half(compact Vision)를 **같은 173장**에 실행 —
  N10 비용 추정 + 사용자 승인 후 ③ **N100 제안**: 신규 건물 100동(N10과 disjoint, 같은 sample rule v1, stratum 50/50)
  을 holdout으로 v2 local + remote 비교, 2인+조정 gold(plan §3), 게이트 수용률 ≥95% + per-axis confusion
- 비용: **$0 유료** (로컬 GPU, 공개 CDN fetch 225건)

## 9. 산출물

- `tools/vnext_wp3_eval.py`, `tests/test_vnext_wp3_eval.py` (4 tests)
- `data/smoke_vnext/wp3_n10_v1/eval_report.json` / `eval_report.md`
- 기존: `sample_manifest.json`, `fetch_log.jsonl`, `local_labels.jsonl`, `local_run.json`, `gold_labels_{raw,final}.json`,
  `judge_decisions.jsonl`, `gold_chunks/`

## 후속 실험 (v2, 2026-10-27)

결론: **local 단독으로는 N10 bar 미달** (pooled ≥90%는 도달, scope/medium 명백 오류 0·cover 10/10은 불가).
비용 $0 (로컬 GPU + sklearn), Neon/R2 0, 유료 호출 0, 네트워크는 vision venv에 scikit-learn 1.9.1 설치뿐.
v1 동결 입출력 SHA 불변. 보고서 `data/smoke_vnext/wp3_n10_v1/exp_v2/report.md`, 수치 `exp_v2/results.json`.

| 엔진 (225장 / 10동, held-out 기준) | pooled | scope 오류 | medium 오류 | cover top1 / critical |
|---|---:|---:|---:|---|
| v1 동결 재채점 (baseline) | 74.7% | 22 | 49 | 7/10 / 1 |
| v2 프롬프트 base (미튜닝, in-sample) | 87.1% | 9 | 15 | 7/10 / 3 |
| **v2 cross-fold** (A→B 85.4%, B→A 92.4%) | **89.4%** | 9 | 18 | 8/10 / 2 |
| probe LOBO (emb / TTA / zs-logit / hybrid) | 87.6-88.2% | 16-17 | 12-13 | 6-8/10 / 1-3 |
| combo: scope=v2 cross-fold + 나머지=probe(zs-logit) *사후 선택, 탐색용* | **91.5%** | 9 | 13 | 9/10 / 1 |

- **baseline (225장)**: 76.2%(173장) → 74.7%. chunk_4 2동 추가로 scope 22 / medium 49, cover 7/10·critical 1(bld_006875 87).
- **embedding**: SigLIP2 so400m 225장 × {full, center-crop, flip} 캐시 (`exp_v2/emb*.npy`). 캐시로 v1을 재현하면 frozen labels와 **0 mismatch**. TTA 효과는 ±0.2pp로 미미함.
- **prompt v2**: fold는 hash 기준 A=5동(130장), B=5동(95장).
  - v1→v2의 주 개선은 구조적 변경에서 나옴. ① unknown floor 0.35 제거 ② SigLIP 'unreadable' 대신 픽셀 blank 규칙(225장 중 0건 발동) ③ 프롬프트 재작성.
  - fold별 offset 튜닝의 held-out 기여는 +1~3pp에 그침.
- **linear probe (LOBO, nested C)**: medium은 94-95%로 zero-shot보다 좋음. 그러나 scope 오류는 16-17건.
  - 원인은 희소 class가 한 동에 몰려 있다는 것. 학습셋에서 빠지면 학습할 수 없음.
    - out-of-scope 18장 중 14장이 bld_010481
    - rendering 7장 전부 bld_030001
    - physical_model 2장 전부 bld_007903
- **라우팅 (in-sample)**:
  - probe(zs-logit) medium 오류 전부를 잡으려면 하위 margin **13%**를 라우팅하면 됨.
  - scope 오류는 고신뢰 오답(margin 최대 0.998)이라 49-92%를 라우팅해야 함.
  - combo로 scope+medium 0을 만들려면 56%를 라우팅해야 함 → 퍼널로서 비경제적.
- **label budget**: 학습 이미지 수별 pooled는 25→82.3%, 50→86.9%, 100→88.4%, 150→88.7%, 202→89.1%.
  - 동 수별: 1동 77%, 5동 88%, 9동 89%. 곡선이 일찍 평탄해짐.
  - power-law 외삽(**rough guess**):
    - pooled 90%: 약 240장
    - pooled 95%: 약 5,400장 (비현실적)
    - medium 95%: 약 150-250장 (희소 class가 커버된다는 전제)
    - in_scope: 곡선이 평탄해 외삽 불가. 장수가 아니라 class 커버리지 문제임.
  - 실무 제안: gold 1,000-2,000장(≥50동). out-of-scope·rendering·physical_model·mixed는 각 ≥50장을 ≥15동에 분산해 의도적으로 보강하고, N100은 별도 holdout으로 둠.
- **어려운 필드 (계약 변경 없이 제안만)**:
  - legibility: gold 모호율 24%, 수용률 69-81%. 2단계(readable/low)로 줄이면 87.6-90.7%가 됨 → 2단계화 또는 advisory(게이트 제외) 제안.
  - camera: calibration만으로 87-91%(v1 44%). eye_level+elevated 병합 시 89.8-92% → 경계는 `unknown` 허용 또는 병합 제안.
  - framing: 모호율 24%.
- **권고**:
  1. scope는 calibrated zero-shot v2 + blank 규칙으로 처리하고, out-of-scope 예시 ≥50장 확보 후 supervised head를 붙임. 그 전까지 isolated_product/no_project_visible 판정과 저 margin은 remote로 보냄.
  2. medium/view/state는 zs-logit probe로 처리하고 medium은 margin 라우팅(약 13%).
  3. cover top-3는 계약 §7대로 항상 remote 검증. 남은 critical은 rendering이 default로 뽑힌 medium 오류임.
  4. lane A "명백 오류 0"은 local+승격 퍼널 출력으로만 충족 가능함 → 다음 단계는 ① WP3 remote half를 같은 225장에 실행(비용 추정 + 사용자 승인 필요) ② 희소 class를 보강한 학습용 gold pool 구축, 그 다음 N100.
- **주의**:
  - 10동·1인 gold 기준임.
  - v2 프롬프트는 v1 집계 보고(173장)를 본 뒤 작성함(경미한 leakage).
  - blank 임계는 product 컷 2장 오발동을 보고 1회 강화함(현재 0건 발동).
  - combo는 사후 선택이고, 라우팅 임계는 in-sample임.
- **산출물**:
  - `tools/vnext_s3_local_engine_config_v2.json`
  - `tools/vnext_wp3_exp_v2{,_embed,_baseline,_report}.py`
  - `exp_v2/` 파일들: baseline_v1_225_eval_report.*, emb*, text_emb*, config_v2_tuned_fold{A,B}.json, pred_*.jsonl, results.json, report.md

## 계약 v1.4 재채점 (2026-10-28)

- 사용자 결정 2026-10-28: legibility 3→2단계(`medium` 미출력, `medium`→`high`), camera eye_level/elevated 경계 `unknown` 허용.
- 계약 `docs/vnext/CONTRACT_GALLERY_SEMANTICS.md` §4.1 v1.4, sha `40b5470a62f4978ac60826b6717f3f3bc53f1367a84d7ffb24d3d656b209b35e`
  (v1.3 sha `81687d1b…c201`). DDL 변경 없음. wp0c contract validator PASS.
  주의: S2 full v1 run은 v1.3 sha를 기록 → `canonical/vnext_gallery_full_validator.py` 재실행 시 `contract_sha` 체크 불일치(앵커 체크는 통과 예상).
- 도구 `tools/vnext_wp3_eval_v14.py` (v1 scorer 미변경 import), `tests/test_vnext_wp3_eval_v14.py` (2 tests).
  출력 `exp_v2/rescore_v14.{json,md}`. v1.3 재채점 parity 8/8 일치. 비용 $0.
- 결과(225장): best combo zsScope+probe(zs-logit) pooled **92.6%** (v1.3 91.5%), zs v2 cross-fold 91.8%, probe 89.2-89.5%, v1 77.8%.
  scope/medium 명백 오류는 불변(9/13, 9/18, 17/12-13, 22/49) → **lane A 전 엔진 FAIL 유지**. lane B 불변.
- camera는 기존 예측이 `unknown`을 내지 않아 변화 0. in-sample abstain what-if: 하위 margin 10-30% → +0.5~2pp.

## codex teacher + probe (2026-10-28)

상태: **probe v1 (hybrid emb+zs2, codex 라벨 학습) eval-225 pooled 96.9% (codex 96.6%, exp_v2 combo 92.6%), scope 오류 4 / medium 오류 2,
cover 8/10 crit 2 → lane A FAIL, lane B FAIL (probe 단독).** codex cover 호출 병행 시 lane B 10/10.
Neon/R2 0, git commit 0, Claude 이미지 열람 0, 동결 입력 변경 0 (gold `252c798a`, local_labels `95bccccb`, codex_v1 labels `a84344fd`).

### 1. 계약 / teacher

- 계약 v1.4 (§4.1): legibility `high|low` (2단계), camera eye_level/elevated 경계 `unknown` 허용. 채점 = 기존 `tools/vnext_wp3_eval_v14.py` → v1 scorer 그대로.
- teacher = `tools/vnext_s3_codex_engine.py` 0.2.0, gpt-6.1-sol / effort low / default tier, label prompt `d30e629e…43da`(동결, eval과 동일), b8, 768px, 3 workers.
- codex eval-225 (이전 단계, `wp3_n10_v1/codex_v1/eval.md`): pooled 96.6%, scope 7 (전부 bld_031919) / medium 1 (idx 129), cover 비교 호출 10/10 crit 0.

### 2. pool 라벨링 (`data/smoke_vnext/wp3_pool_v1/`)

- pool: 278동 / 1,513장 (Divisare 139 / Architizer 139동), eval 10동·holdout 100동과 건물·pixel 중복 0 (학습 스크립트에서 재확인).
- 입력 어댑터 `tools/vnext_wp3_pool_codex_prep.py` → `codex_input/` (engine 형식 manifest + fetch log, pixel sha 재검증).
- slice 먼저: engine batch 13개 = 104장 (sha 순서 선택, 전체 run과 같은 batch라 resume 대상) → **1,229.5 tok/img, retry 0**, 예상 총량 ≈1.87M ≤ 8.0M → 전체 진행.
- 전체: **1,512/1,512 ok, retry 0, invalid 0**, 1,855,387 tok (**1,227 tok/img**), 189 calls, 남은 176 calls wall 524.6 s (≈9.7k img/h).
  fetch 실패 1장(idx 1179, 404)은 `codex_labels.jsonl`에 `not_run` 행으로 명시. 결과 `codex_labels.jsonl` sha `b5a617e3…829a`.
- teacher 분포: in_scope 1,454 / out 58 (product 31, text 10, multi_panel 8, people 4, non_arch 3, no_project 2);
  medium photo 1,118 / drawing 175 / rendering 135 / model 71 / mixed 9 / other 4; legib high 1,414 / low 40; camera unknown 4.
- enrichment 확인: rendering 픽 65/65 → codex rendering (우려했던 photo 오인 없음), model 64/65, drawing 65/65, out-of-scope 픽 65 중 44 out / 20 photo / 1 rendering.
  prefilter(zs v2) vs codex 불일치: scope 114/1,512, medium 81.

### 3. token 사용 (실측)

| stage | token | 비고 |
|---|---:|---|
| eval-225 stage (이전) | 481,636 / 1.5M cap | harness 49,608 · smoke 37,784 · labels 281,931 · covers 103,172 · quota 9,141 |
| pool stage | **1,864,527 / 8.0M cap** | slice 127,863 · 나머지 1,727,524 · quota probe 2회 9,140 |
| 합계 | 2,346,163 | 유료 API $0 (ChatGPT pro codex quota) |

- quota: 4회 측정 모두 주간 사용률 **5.0%**, credits 63,125.37 불변 (reset 2026-11-02 08:21 UTC).
  첫 측정 이후 ≈2.29M token 사용에도 표시 변화 없음 → 주간 한도 ≳ 약 230M token (1% 해상도 최악 가정, 대략적인 하한).
- 이미지당 token >6,000 / retry >5% 정지 조건 미발동.

### 4. 임베딩 / probe

- `tools/vnext_wp3_pool_embed.py` (vision venv): SigLIP-2 so400m, exp_v2와 동일 transform → `emb.npy` 1,512×1152, 45 s.
  parity: eval 이미지 32장 재임베딩 vs `exp_v2/emb.npy` max|diff| 0.0.
- `tools/vnext_wp3_probe_v1.py`: head 9개 (+ 선택 `oos9` 결합 scope head), StandardScaler + LR(class_weight=balanced),
  C는 pool 건물 5-fold GroupKFold로 codex 일치도 기준 선택, §5 applicability는 예측 후 적용.
  camera `unknown` 임계 t는 pool OOF에서만 선택 (t=0.6 = grid 상한; eval에서 unknown 7/225장뿐, t=0이면 pooled 96.8%).
- 동결 모델: `probe_v1/{emb,zs2,hybrid_emb_zs2}[+oos9].pkl` + `probe_v1/manifest.json` (파일별 sha).

| engine (eval-225, v1.4) | pooled | scope err | medium err | cover top1/top3/crit | 비고 |
|---|---:|---:|---:|---|---|
| **probe hybrid_emb_zs2** (사전 지정 primary) | **96.9%** | 4 | 2 | 8/10 / 10/10 / 2 | 최저 필드 framing 92.4%, legib 94.7% |
| probe hybrid_emb_zs2+oos9 | 96.7% | 3 | 2 | 8/10 / 10/10 / 2 | |
| probe emb | 96.4% | 3 | 4 | 8/10 / 9/10 / 2 | |
| probe zs2 | 95.3% | 5 | 9 | 7/10 / 8/10 / 2 | |
| exp_v2 combo (gold LOBO 학습) | 92.6% | 9 | 13 | 9/10 / 9/10 / 1 | |
| codex_v1 (teacher) | 96.6% | 7 | 1 | 10/10 / 10/10 / 0 | cover = 비교 호출 |

- primary 오류: scope 4 = 인테리어/패널을 reject (idx 17, 129, 130, 197), medium 2 = idx 130 mixed→rendering, idx 192 photo→drawing.
  cover miss 2 = bld_006875 / bld_033333에서 element_detail 사진을 default로 선택 (critical).
- probe pool OOF 일치도(vs codex) 94.3%. 정답 기준 eval에서는 probe가 teacher와 같거나 약간 높게 나옴(10동이라 차이는 노이즈 수준).
  codex의 bld_031919 오류는 probe가 따라 하지 않음.

### 5. 라우팅 (하위 margin X% → codex 라벨로 교체, eval-225)

- scope/medium margin 기준: X=10% (22장)에서 probe 쪽(미라우팅) scope/medium 오류 **0/0**, pooled 96.8%.
  in-sample 최소 = 5.8% (13장). pooled ≥95%는 X=0에서 이미 충족.
- **정직한 임계 (pool OOF에서 고정, eval 미사용)**: probe-vs-codex scope/medium 불일치 90% 포착 임계 0.9347
  → pool 13.7% / eval 20.9% 라우팅, pooled 96.0%, 미라우팅 오류 0/0.
- 하지만 라우팅해도 **eval 총 scope/medium 오류는 0이 안 됨** (4/1 ~ 7/1): 남는 건 전부 라우팅된 이미지에 대한 codex 자신의 오류
  (bld_031919 product/가구 인테리어 경계 + idx 129). 즉 lane A 0-오류 바의 남은 병목은 teacher/gold 정의 불일치 → 판정자 재확인 필요 (미실시, 사용자 결정 사항).
- 권고 라우팅: **scope/medium margin < 0.9347 (pool 기준 ≈14%, eval 분포에선 ≈21%)**. all-fields margin 라우팅은 58–60%가 필요해 비효율.

### 6. 전수 실행 예상 (S6-lite 699,038장 / 39,248동)

| 단계 | 규모 | token | 시간 |
|---|---:|---:|---:|
| local 전수 (임베딩 + probe) | 699,038장 | 0 | GPU 2.65 h; fetch가 병목 48.5 h @4 rps (§7) |
| codex 라우팅 (margin < 0.9347) | 95.8k–146.1k장 (13.7–20.9%) | 117.5M–179.3M (1,227 tok/img) | 9.9–15.1 h (3 workers, 9.7k img/h) |
| codex cover 비교 호출 | 39,248동 | 222.7M (512px, 5,673 tok/동) | ≈19 h (3 workers) |
| 합계 codex | | **≈340–402M** | ≈29–34 h |

- 주간 한도 하한 ≈230M(대략) 기준 최소 2주 이상으로 분할 필요. 라우팅 이미지는 local pass에서 바로 codex 큐로 보내거나(디스크 임시 보관) 재fetch 필요.
- cover: probe 단독은 8/10 crit 2라 비교 호출을 대체 못 함. probe tier-1 후보가 뚜렷한 동은 비교 호출을 생략하는 방안은 미측정.

### 7. 산출물

- 도구: `tools/vnext_wp3_pool_codex_prep.py`, `tools/vnext_wp3_pool_codex_finalize.py`, `tools/vnext_wp3_pool_embed.py`, `tools/vnext_wp3_probe_v1.py`;
  테스트 `tests/test_vnext_wp3_probe_v1.py` (3 tests)
- 데이터 (`data/smoke_vnext/wp3_pool_v1/`): `codex_input/`, `codex_label_run/` (labels/calls/batches/run.json, slice 스냅샷 `*_slice104*`),
  `codex_labels.jsonl`, `codex_labels_finalize.json`, `ledger_pool.json`, `codex_quota_probes.jsonl`, `emb.npy`, `emb_index.json`,
  `probe_v1/`, `pred_probe_v1_*.jsonl`, `eval_probe_v1.{md,json}`, `probe_v1_run.log`, `embed_run.log`
