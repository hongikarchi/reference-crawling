# WP2.9 작업 지시 — S1 registry + 규칙 티어 병합 + 이월 기록 (2026-08-27, Claude 발행)

사용자 결정(2026-08-27): **규칙 티어만 병합하고 나머지는 이월.** 계측기 기반 일괄 승인은
종료됐다(`20260827_vnext_wp28r_gate_verdict_terminate.md`).
이 WP는 vNext에서 **처음으로 병합을 실행**한다. 단 **로컬 S1 store에만** 반영하며
**Neon/R2 쓰기는 여전히 0**이다. 유료 API 0.

## 규칙 티어 정의 (확정, 변경 금지)

`name_strict_score ≥ 95` **AND** `firm_strict_score ≥ 95` **AND** `year_gap ≤ 1`
**AND** `(exact_asset_pair_count + identical_phash_pair_count) ≥ 1` **AND** 정책 플래그 없음
**AND** v4 proposal = `merge_candidate`.

근거는 A70 gold 검증(규칙 충족 25건 중 비-same 0, 알려진 비-same 4건이 전부 서로 다른
독립 사유로 규칙 밖). `phash d≤8`만 있는 쌍은 티어에 넣지 않는다(2,043 vs 1,534 차이는
의도적으로 보수 선택).

## Stage A — 티어 확정과 component 안전성 (offline, 무비용)

1. 규칙을 5,100 큐에 적용해 티어 집합을 산출한다(예상 1,534). 실제 수치를 보고한다.
2. 티어 쌍만 union-find해 component를 만든다.
3. **component 안전 검사 — 하나라도 걸리면 그 component 전체를 티어에서 제외**하고
   개별 리뷰로 보낸다(삭제 아님, 사유 기록):
   - component 내부의 어떤 쌍이라도 v4에서 `reject` / `related_but_separate` / `defer`
     (계약 §4 불변조건)
   - component 내부의 어떤 쌍이라도 정책 플래그 보유
   - component 크기가 **4개 건물 초과** (대형 component는 연쇄 병합 위험이 크다)
   - 한 source building이 서로 다른 component에 속하는 모순
4. 제외된 component 수·건물 수·사유별 분포를 전량 보고한다. **최종 티어 수치는 3번
   적용 후 값이다** — 1,534보다 줄어들 수 있으며 그것이 정상이다.

## Stage B — S1 registry 구축

계약 §5·§6대로 `vnext_identity_registry_v1`을 만든다.

- **91,803 source building 전량**에 `vnx_bld_*` 발급. 발급 순서는 동결 manifest의
  source key 정렬로 결정론적. 재사용·재번호화 금지.
- 티어 component는 최초 발급 ID가 survivor, 나머지는 alias.
- **이월분 전량 기록**: 티어 밖 merge_candidate(약 3,566), defer(2,192),
  related_but_separate, Stage A에서 제외된 component를 각각 상태·사유·증거와 함께
  edge로 보존한다. 다음 릴리스가 이 작업을 다시 하지 않아도 되게 하는 것이 목적이다.
- WP0b 스냅샷으로 legacy crosswalk 산출(`one_to_one` / `many_to_one` / `one_to_many` /
  `unmatched` / `conflict`). `one_to_one` 판정은 계약 §6의 양방향 유일성 + source
  membership 무모순을 전부 만족할 때만.
- 독립 validator: 91,803 membership 전량, component 불변조건, registry 안정성,
  crosswalk 양방향 cardinality, 티어 규칙 재적용 일치.
- 보고: vnx ID 수, 병합으로 줄어든 건물 수, crosswalk 상태별 분포, legacy PK 승계 가능 수.

## Stage C — 사용자 표본 감사 패킷

- 병합된 티어에서 **100쌍 층화 표본**(component 크기별, 증거 프로파일별). seed 고정.
- **로컬 리뷰 앱**을 만든다 — `tools/covers_by_type_review_app.py` 패턴 재사용.
  브라우저에서 소스 CDN URL을 직접 로드하고(원격 이미지 정상 표시), 좌우 나란히 +
  메타데이터(이름·설계사·연도·도시·소스 URL) + 버튼 4개(same / related / different /
  unclear) + 키보드 단축키. 판정은 JSONL로 저장, 재개 가능.
- **에이전트가 라벨을 미리 채우지 말 것.** 이것은 사용자 본인의 감사다. 규칙 점수는
  보여도 되지만 추천 라벨은 표시 금지.
- 예상 소요 30분~1시간.

## 사전 선언 판정 기준 (사후 변경 금지)

사용자 감사 100쌍 결과:

- `different` **0건** → 티어 확정, 병합 유지.
- `different` **1건 이상** → **정지**. 티어를 자동 롤백하지 않고 사용자 결정으로 상정한다
  (해당 사례를 Claude가 분석해 규칙 보강 또는 티어 축소를 제안).
- `related` **5건 이상** → 정지, 동일하게 사용자 결정으로 상정.

## 산출물

`vnext_identity_registry_v1` DB/manifest/report + 독립 validator + 감사 앱 + 표본 manifest
+ job card. v4 아티팩트 SHA pin, 불변 보존. 실패 시 정지 후 FAIL 리포트.

## 금지

Neon/R2 쓰기, canonical_v2 테이블 접근, 유료 API, 규칙 임의 변경, 티어 밖 쌍의 병합,
사용자 감사 결과 선점(에이전트 사전 라벨링).
