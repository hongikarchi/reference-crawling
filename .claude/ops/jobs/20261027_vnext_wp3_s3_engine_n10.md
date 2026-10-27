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
