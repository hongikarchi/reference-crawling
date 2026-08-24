# WP2 실행 기록 — vNext identity 후보 생성 + 블라인드 N10

상태: **PASS — lane C gate 통과**

지시서: `20260824_vnext_wp2_identity_candidates_order.md`
run: `wp2-876b65a0a8a778964f4ab2b0`

## 범위와 비용

- Divisare↔Architizer cross-source 후보와 N10 gold만 생성했다.
- 실제 merge, registry/component/crosswalk 생성, `vnx_bld_*` ID 발급은 하지 않았다.
- Stage A network 0 / Stage B HTTP 37 / Vision 0 / LLM 0
- Neon write 0 / R2 write 0 / legacy snapshot read 0 / 유료 비용 `$0`
- 구현 에이전트 동시성 1을 유지했다.

## 구현·검증

- `canonical/vnext_identity_candidates.py`: 세 입력 계열 union, SHA-pin matcher
  blocking replay, 6-family 평가, 4상태 proposal, 층화 N10 동결
- `canonical/vnext_identity_review.py`: side당 최대 2개 asset, bounded HTTP,
  current derivative, blind sheet, review lock, adjudication과 lane C gate
- `canonical/vnext_identity_candidates_validator.py`: 생산 모듈을 import하지 않고
  Stage A universe/판정과 Stage B E1/E2 lineage·asset 선택·cache·sheet·gold·gate를 재생
- runner/validator CLI와 `tests/test_vnext_identity_wp2.py`
- 구현 커밋: `5de28c0`, `aadd246`
- 테스트: `10 passed`; 최종 독립 validator: **67/67 PASS**

`data/id_registry_architects.json`은 동결 workspace에 없었다. 따라서 기존 matcher
SHA를 고정한 뒤 curated population의 exact strong normalized firm key로 blocking을
결정론적으로 재생했다. 이 제한은 manifest에도 기록했다.

## 입력 pin

| 입력 | bytes | SHA-256 |
|---|---:|---|
| Divisare curated | 2,225,299,456 | `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f` |
| Architizer curated | 8,767,438,848 | `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc` |
| E2 full v5 | 10,164,682,752 | `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19` |
| Divisare E1 v1.2 | 2,646,114,304 | `869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819` |
| Architizer E1 v1.2 | 4,373,962,752 | `58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937` |
| identity contract | 6,598 | `1ee13f1a2a7ffe7dcaf5b7c1cbaf419c5999d53bcd473311499b74070aabeca6` |
| building matcher | 26,812 | `3cf9592a9e54cd4e07512461ca08dc5ca7f8dd5e6cd97b2d6c118dd0b265d87b` |
| pHash matcher | 5,450 | `3dcc73152d822fe2b8eaccbb333625672378a3f6cfa59d3099cda0ca28e64fe7` |

모든 SQLite 입력은 pre/post/current SHA가 일치하고 WAL/SHM/journal이 없다.

## Stage A 결과

| 항목 | 값 |
|---|---:|
| source building | 91,803 (Divisare 29,891 / Architizer 61,912) |
| candidate pair union | 68,049 |
| 6-family row | 408,294 |
| direct image evidence row | 59,187 |
| auto-merge-eligible | 4,677 |
| Stage A logical SHA | `21643bbdfa9c702c6e7cc4ef5e8a742aa4cf69d0016f695c750155ffc72fcefd` |

origin count는 중복을 포함해 E2 image 9,026 / E2 metadata 6,754 /
matcher architect-year 62,739 / matcher pass2 157이다.

| proposal | 수 |
|---|---:|
| merge_candidate | 4,677 |
| related_but_separate | 121 |
| reject | 61,029 |
| defer | 2,222 |

hard conflict는 verified-pHash veto 60,857 / country-site 1,170 /
project-phase 646 / matcher architect-year gate 209다. Legacy snapshot은 후보·증거 어느
쪽에도 사용하지 않았고 transitive image evidence도 만들지 않았다.

## Stage B와 블라인드 gate

- 층화 표본: strong merge 4 / metadata-only 2 / image-only 2 / conflict-boundary 2
- selected asset 37; cache hit 0; logical fetch 37; 실제 HTTP request 37
- current pixel: exact 34 / source_changed 3 / failed 0
- source_changed 3건은 모두 Architizer:
  `atz_asset_5e7226caaa44c00faf451011`,
  `atz_asset_c08ea55546a37d924ab6bddb`,
  `atz_asset_d56c775eed6137395f425c52`
- 점수·proposal·rank·stratum·candidate ID를 숨긴 10개 sheet만 보고 리뷰를 먼저
  잠근 뒤 unblind packet으로 재확인했다.

| stratum | proposal | blind / final gold |
|---|---|---|
| strong merge 4 | merge_candidate 4 | same 4 / same 4 |
| metadata-only 2 | merge_candidate 2 | same 2 / same 2 |
| image-only 2 | merge_candidate 2 | same 2 / same 2 |
| conflict-boundary 2 | reject 2 | same 2 / same 2 |

- reviewer↔adjudicator disagreement: 0
- reviewer `different` ∩ auto-merge-eligible: 0 → false merge 0
- auto-merge-eligible hard-conflict violation: 0
- final gold: `same` 10
- **lane C gate: PASS**

### 발견된 false-reject 신호

conflict-boundary 2건은 실제로 같은 프로젝트였지만 정책은 둘 다 reject했다.
원인은 `Turkey`와 `Türkiye`를 같은 국가로 canonicalize하지 않은 것이다.

- `Cabin in Woods`: Bergama = Pergamon이며 name/firm/year/image가 일치
- `G House`: Göcek 권역 locality, name/firm, 1년 차이와 image가 일치

이는 이번 gate의 false-merge 조건은 위반하지 않으므로 PASS를 바꾸지는 않는다.
다만 N100 전에 country alias를 수정하고 Stage A/N10을 새 버전으로 재생해야 한다.
동결 표본과 기존 결과는 사후 교체하지 않았다.

## 산출물 pin

- Stage A DB SHA before Stage B:
  `0a76607dbd4954ddd894677db98e9a5bcf47164dce5d99a266e6efbbb7633ebd`
- Stage A logical SHA는 Stage B/리뷰 후에도 동일하다.

| 산출물 | bytes | SHA-256 |
|---|---:|---|
| `data/enrichment/vnext_identity_candidates_v1.db` | 511,156,224 | `0541b4c86bb2def88bbadaf53eec33c62d8f14e62800f1ceb8d96761f3aea5c4` |
| Stage A manifest | 7,346 | `4d919284be8fa76e18877ac303adc6fc6dbf080ff778d9fbf5a65fd1e5f34e9b` |
| Stage A report | 2,059 | `c47b77361fce31cac2df2264183a45fce66703590dfb8f7356da5f0ad0f222a5` |
| Stage A validation | 13,346,311 | `14efbb1a1f315893515ad2207430178020914df171fdaef2bd933eec732acbe6` |
| Stage B manifest | 1,645 | `3d5fde4fae30ddc794f8fb094fb09912d4dcbb9bd7078e748aa14c3e6f045c1e` |
| Stage B report | 517 | `382c276ea2621fe3b0e438830b1af55714448fc5634fb74913edae32c261777f` |
| blind review form | 3,887 | `1f1c3fa237154e09202fabe94e196abbe5f2147b51f596791a04bc53576bb118` |
| adjudication packet | 46,492 | `ccd07a53704d3b9224fcf170998e54de312ef3dc1471cb48beefe07fd8d4af6c` |
| adjudication form | 4,448 | `6350360a7c77057d4cedda10972ac175b34da0aee47abbe320bd80473eb454e1` |
| lane C gate report | 498 | `c1b7f007758a20ef0b9f2c0db8b285b2b93c9de2c202f41284a7e00a3b43325c` |
| final validation | 19,108 | `348cd263eb5e26181edd4d3d6aec4e2263f730fda149a24414858647caa417b8` |

blind sheet 10개의 개별 SHA/bytes는 DB `review_sheets`에 pin했고 독립 validator가
실제 파일과 전부 대조했다.

## 결론·다음 단계

WP2의 후보 생성과 lane C precision gate는 PASS로 종결한다. 그러나 N10의
conflict-boundary가 2/2 false reject였고 실제 `different` gold가 하나도 없어,
바로 S1 registry를 발행하기보다 **country alias 결함 수정 → 새 버전 Stage A/N10
재생 → N100 확대** 순서를 권고한다. S1은 N100에서 merge/defer/reject 분리 품질을
확인한 뒤 사람 승인 병합 큐로 발행한다.
