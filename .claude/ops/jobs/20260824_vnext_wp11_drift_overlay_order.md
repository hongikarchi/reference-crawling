# WP1.1 작업 지시 — source_changed fail-closed overlay (2026-08-24, Claude 발행)

## WP1 v1 판정

- **FAIL은 유효한 fail-closed 증거로 수리한다.** 실행·정지 모두 계약대로였다.
  v1 아티팩트(`vnext_gallery_draft_n10_v1.db`, SHA `d63b7a5a…`)는 불변 보존.
- 원인은 코드 결함이 아니라 **소스 드리프트**: E1 pin(08-10) 대비 17/169(10.1%)의
  현재 픽셀이 변경됨. 참고 추세: 08-11 표본 1.2% → 08-21 16% → 08-24 10.1%.
  E1 픽셀 pin은 시간이 갈수록 부식된다 — full P1을 미룰수록 나빠진다.

## 설계 결정 (Claude)

report의 3안 중: **(a) 보수적 split 채택.** (b) frozen 파생본 복구는 E1이 바이트를
보관하지 않아 일반적으로 불가능. (c) E1 전면 재동결은 과중하고 또 부식된다 —
대신 아래 "refresh ledger"로 점진 해결한다.

1. **검증 edge의 정의 강화** (`CONTRACT_GALLERY_SEMANTICS.md` §2 개정, 1줄):
   direct-edge star 부착은 **양 끝점이 같은 run의 verified-pixel pass에서
   `exact_match`일 때만** 허용한다. 어느 한쪽이라도 source_changed/failed면 부착하지
   않고 `unverifiable_edge_source_changed` reason으로 split, 증거는 보존한다.
2. **exact normalized-pixel SHA 동치류는 유지한다** — 동결 시점 바이트 동일성은
   리뷰가 필요 없는 확정 증거다. 단 대표는 `exact_match` 검증 멤버만 가능(계약 §2
   기존 규칙과 일치). 검증 멤버가 없는 동치류는 unresolved로 계상.
3. **드리프트 멤버 자체는 제외하지 않는다** — gallery 행에 `pixel_unverified` 플래그로
   보류(대표 불가·서빙 불가). 처리 방침은 아래 refresh ledger가 담당한다.
4. **asset refresh ledger (full P1 전 설계 항목, 이번 WP 범위 아님):**
   `vnext_asset_refresh_v*` — 드리프트 자산의 현재 응답을 `(source, source_asset_id)`에
   묶인 **새 픽셀 버전**(raw SHA/normalized SHA/pHash)으로 기록. 이후 그룹핑 리뷰·P4B
   시맨틱 입력·서빙은 최신 검증 버전을 쓰고, frozen E1은 v1 증거로 남는다.
   (coverage 문서가 요구한 "refresh/reconciliation policy"의 vNext 구현.)

## WP1.1 실행 지시 (Codex)

- v1 아티팩트를 참조하는 **overlay 아티팩트** `vnext_gallery_draft_n10_v1_1`을 만든다.
  v1 수정 금지, v1 DB SHA를 pin.
- 위 결정 1~3을 적용해 group을 재산출: 4개 unverifiable pair split, 예상 group 수와
  변화 내역을 report에 명시. `pixel_unverified` 17건 플래그.
- 블라인드 1인 리뷰를 **admissible 부착 전수**(현재 기준 2쌍)에 재수행 —
  by construction unreviewable 0. 게이트: admissible 부착 100%에서 false collapse 0.
- **network 0**: v1의 검증 픽셀 cache(151개)만 사용. HTTP/Vision/LLM/Neon/R2 = 0.
- report에 드리프트 17건의 **소스별 분해**(divisare/architizer)와 URL 패턴 요약 추가
  (refresh ledger 설계 입력).
- 계약 §2 개정(결정 1)을 반영하고 새 계약 SHA를 job card에 pin.
- 독립 validator + job card + SHA pin. 실패 시 정지 후 FAIL 리포트.

## 게이트 통과 시

WP1은 v1(FAIL 증거) + v1.1(PASS)의 쌍으로 종결. 다음은 WP2(entity resolution 후보
생성) 발행 — full P1 확장은 refresh ledger 설계 확정 후.
