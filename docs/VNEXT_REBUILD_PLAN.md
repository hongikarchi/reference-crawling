# vNext DB 재구축 계획 (2026-08-24 확정)

Claude(설계·게이트 판정) / Codex(WP 단위 실행) 분업 계약. 이 문서가 기준이며,
여기 없는 확장은 실행하지 않는다. 배경 검토: 12회 유료 시맨틱 N10 전부 FAIL의
원인은 라벨 품질(수용률 91~97%, 합격권)이 아니라 ① uncertainty 자기보고 하드
게이트 ② 단일 이미지 라벨→hero 파생 규칙이었다. 이 계획은 그 구조를 교체한다.

## 0. 확정 결정 (사용자 승인 2026-08-24)

| # | 결정 | 내용 |
|---|---|---|
| D1 | SoT | 로컬 불변 아티팩트(E1/E2/E3 패턴). Neon은 serving projection만 |
| D2 | Entity resolution | curated 91,803(div+atz) 기준 **전체 재해석**. 기존 canonical 39,478은 legacy 호환·크로스워크용 |
| D3 | 소스 범위 | divisare+architizer로 시작. archello/metalocus는 이후 별도 E1부터 additive |
| D4 | 서빙 | 기존 canonical_v2_buildings 모양 유지(값 갱신) + `building_images` 테이블 1개 신설. 증거·계보는 로컬만 |
| D5 | QA 게이트 | 재설계: uncertainty는 advisory 강등, 커버는 건물 내 비교선택 + acceptable top-3, 시맨틱/커버 lane 분리 |

## 1. 데이터 계층

### 동결 증거 (수정 금지, 읽기 전용)
- curated: `divisare_metadata_v2_4.db`, `architizer_curated_v2_0.db`
- E1 v1.2 ×2 / E2 full_v5 / E3 full_v1 (SHA는 docs/CROSS_SOURCE_IMAGE_SEMANTIC_COVERAGE.md)
- 시맨틱 v1.0~v2.7 실험 일체 — 증거로만 보존, 어떤 결과도 production 승격 금지

### 신규 derived store (전부 로컬, 버전별 불변, builder+독립 validator 쌍)
| Store | 내용 |
|---|---|
| S1 `vnext_identity_v*.db` | vNext 건물 registry(vnx_bld_*), 병합 판정(merge/related/reject/defer + 증거), legacy crosswalk |
| S2 `vnext_gallery_v*.db` | 건물별 canonical gallery: dedup 멤버, 대표 변형, source occurrence 전량 보존, exclusion ledger |
| S3 `vnext_semantics_v*.db` | 이미지별 compact 라벨(engine·버전 기록), 이중판정 불일치 플래그 |
| S4 `vnext_covers_v*.db` | default+5종 커버, analysis bundle, review_required 큐 |
| S5 `vnext_text_v*.db` | 병합 건물 다중 소스 텍스트 분석(D-1 vNext) + embedding |
| S6 projection artifact | S1~S5 → Neon 업서트 입력 JSON(c27 계열의 후계) |

원칙: `(source, source_asset_id)` 안정 이미지 ID, pHash/crop 관계는 direct edge만
(transitive closure 병합 금지), 실패는 삭제 아닌 명시 행, 완료 아티팩트 resume=요청 0.

### Neon (serving projection)
- `canonical_v2_buildings`: 스키마 유지, 값은 vNext 재생성. PK는 crosswalk이 1:1
  명확하면 legacy `canonical_bld_id` 유지(스와이프 이력 보존), 아니면 신규 ID
- `building_images` 신설: bld_id FK, url, sort_order, semantic_type(exterior_overall/
  interior/drawing/aerial/detail/…), medium, is_cover_default, cover_slot, engine/버전
- 부모+자식+태그 3형제를 같은 아티팩트에서 한 트랜잭션 재생성 → FK drift 원천 차단
- 롤백 = 이전 projection artifact 재업서트(추가 기계장치 없음). Neon 쓰기는 전부 user-gated

## 2. Phase 구성

```
P0 동결·계약 → P1 이미지 identity(결정론) → P2 entity resolution
→ P3 gallery 조립 → P4A 텍스트 ∥ P4B shallow 시맨틱 → P5 커버 선택
→ (P6 심층 bundle 분석: 보류, 별도 승인) → P7 projection·배포
```

- **P1**: E2 direct edge 기반 건물 내 dedup + 대표 선정. 크롭/변형 local feature는
  전수 아님 — 갤러리 후보와 cross-source 후보쌍에만. 픽셀 재다운로드는 1회
  verified-pixel pass(E1 SHA 검증, 파생본 로컬 캐시)로 P4B와 공유
- **P2**: 블로킹 = E2 `metadata_building_pairs`(6,754) + `cross_source_building_candidates`(9,026)
  + 기존 matcher의 architect/year 게이트·pHash veto(검증된 규칙 재사용). 독립 증거 2종
  이상 + hard conflict 0만 auto-merge 후보. **첫 릴리스 병합은 전건 사람 승인**.
  reject/defer가 component 안에 접히면 배포 중단
- **P3**: gallery 확정, E3 순위를 canonical 건물 기준 재계산, placeholder/로고/비건축은
  exclusion ledger로. 여기서 building_images 모수 확정
- **P4B 엔진 결정**: 동일 gold 표본에 ① local(SigLIP: `tools/covers_by_type_prep.py`
  계열) ② compact remote(v2.7 계약 축소판) 비교 N10 → 원칙: local 전수 + 저신뢰·커버
  후보만 remote 승격. **1.43M 전수 원격 Vision 금지.** 긴 설명문 생성 금지
- **P5**: 결정론 필터·랭킹 → 명확하면 확정 / 근소하면 건물 내 후보 나열 비교 Vision
  / 그래도 애매하면 review_required(억지 fallback 금지). rendering은 photo slot 불가,
  perspective는 drawing subtype. Default=exterior overall 우선, aerial overall 차선
- **P6(보류)**: bundle 심층 분석은 v1 릴리스 필수 아님. 기존 `image_derived`는
  legacy_cover_vision으로 동결
- **P7**: projection 빌드 → validator → dry-run 행수 제시 → 사용자 승인 업서트 →
  태그 3형제 재빌드 → architects 재유도. 벤치마크 10문항 PASS 확인

## 3. QA 게이트 (재설계)

기술 게이트(전 lane 공통, 기존 유지): 고정 manifest·exact pixel fail-closed·독립
validator·resume 0-request·표본 사후 교체 금지.

| Lane | N10 (건물군 10) | N100 |
|---|---|---|
| A 시맨틱 라벨 | scope/medium 명백 오류 0, 9축 수용률 ≥90% | 수용률 ≥95%, per-axis confusion 보고 |
| B 커버 선택 | critical 오류 0(비건축·도면이 default 등), acceptable top-3 10/10 | top-1 ≥95%, top-3 ≥99%, P0 baseline 대비 회귀 없음 |
| C identity/병합 | false merge·hard conflict 0 | accepted edge precision ≥99%; 표본 부족 시 병합은 사람 승인 유지 |

- **uncertainty 자기보고는 게이트 아님** — advisory 리포트만. 불확실성 신호는
  이중 판정 불일치(local vs remote, 필요시 remote 2회)로 측정하고 escalation에 사용
- lane A 통과가 lane B를 막지 않도록 독립 판정(라벨 lane은 통과 시 즉시 확정 가능)
- gold: N10은 블라인드 1인 리뷰(+판정자 재확인), 2인+조정 프로토콜은 N100·승격
  게이트에만(리뷰 비용이 본 실행을 초과하는 v2.x 병폐 방지). 커버는 acceptable set,
  entity는 same/related/different/insufficient 구분

## 4. 비용·중단 규칙 (기존 합의 승계·강화)

- smoke ladder 필수: N10 → N100 → 1% → 10% → 100%. 각 단계 전 예상 호출·토큰·시간
  제시 + 사용자 승인. quota 소진이 목표가 아님
- fresh N10 사이클당 최대 3개. **같은 hard failure 2회 → 프롬프트 튜닝 중단, 구조
  변경으로 전환**(이번에는 실제 집행 — v2.x에서 이 규칙이 ~10회 무시됨)
- 즉시 중단: false merge 1건 / critical 커버 오류 1건 / 입력 SHA drift 무단 수용 /
  validator FAIL / retry >1% / 승인 상한 초과
- 주간 quota ≤30% 신규 유료 금지, ≤20% 전면 중단
- 오케스트레이션 캡: 구현 에이전트 동시 1개, 장출력 억제, 상태는 durable 파일에서만
  재구성(v2.6의 1시간 27%p quota 증발 재발 방지)

## 5. Codex 실행 수칙

1. WP 단위로만 실행. WP마다 입력 SHA, 산출물 경로, validator 결과, job card
   (`.claude/ops/jobs/`) 기록
2. 산출물은 `vnext_*` namespace 신규 파일만. 동결 증거·기존 파일 수정 금지
3. 계약 밖 확장·임의 재설계 금지. 실패하면 FAIL 리포트 작성 후 정지 — 다음 버전
   설계는 Claude가 결정
4. 유료 호출 전 반드시: 예상 토큰 상한 + quota 잔량 확인 + 사용자 승인 확인
5. v2.x식 과잉 세리머니 축소: N10에서 2인 리뷰·자식 continuation 아티팩트·전 이력
   재검증을 기본으로 하지 않는다(§3 gold 규칙 준수)

## 6. 착수 순서 (WP0~, 전부 무비용)

| WP | 내용 | 산출물 |
|---|---|---|
| WP0a | 워크스페이스 동결 manifest: untracked 374 + data/enrichment 인벤토리(accepted만; rejected draft·partial은 목록만). ※ ACL 깨진 ~200개 tmp 디렉토리와 E2 v1/v2/v4 WAL ~32GB 정리는 **사용자 결정 후** 별도 실행 | `data/reports/vnext_workspace_freeze_v1.json` |
| WP0b | Neon 현행 read-only 스냅샷 덤프: 39,478행 × **embedding 제외 전 컬럼**(crosswalk·serving-state baseline·R4 보존에 필요; source_refs만으로는 부족 — 2026-08-24 게이트 리뷰 F4) | `data/canonical/legacy_neon_snapshot_2026q3.jsonl` |
| WP0c | 계약 문서 3개: identity(병합·증거 규칙), gallery+semantic(포함·제외, compact 라벨 필드=v2.7 축소판), cover(슬롯·선택·bundle 규칙) + `building_images` DDL + projection 명세 | `docs/vnext/CONTRACT_*.md` |
| WP1 | P1 결정론 identity 빌더+validator, 건물군 N10 smoke | S2 초안 |
| WP2 | P2 resolution 후보 생성 → N10 사람 검토 → 확대 | S1 |
| WP3 | P4B 엔진 비교 N10 설계(gold 표본 manifest, local/remote 비용 실측) — **유료 호출은 여기서 처음, 승인 후** | 비교 리포트 |

이후 WP는 WP3 결과를 보고 Claude가 발행한다.

## 7. 명시적 보류·미결

- P6 심층 bundle 분석(별도 승인) / archello·metalocus E1(별도 사이클)
- tmp ACL 정리·E2 draft WAL 삭제(~60GB 회수 가능) — 사용자 결정 대기
- R2 재호스팅: 현재 전 URL이 소스 CDN 직결. vNext v1 범위 밖, 이후 결정
- 기존 pending: architect top_typologies refresh, TYP_PROGRAM_OK 확장, Garden/Landscape
  typology, junk-row 스윕 — 본 계획과 독립, 기존 상태 유지
