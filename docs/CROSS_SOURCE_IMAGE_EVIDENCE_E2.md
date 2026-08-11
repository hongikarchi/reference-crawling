# Divisare–Architizer cross-source image evidence E2

## 상태와 목적

E2는 Divisare와 Architizer의 frozen metadata DB 및 E1 image fingerprint
sidecar를 결합해, 두 소스 사이의 **직접 이미지 근거와 보수적인 metadata
block 근거**를 immutable SQLite로 보존하는 offline 단계다.

E2는 동일 건축물을 최종 판정하는 단계가 아니다. 다음 항목은 의도적으로
후속 policy gate로 미뤘다.

- 대표 이미지 선정
- Vision 처리 대상 큐 생성
- Vision/LLM 호출
- pHash graph의 connected component를 이용한 동일성 추론
- project 또는 building merge
- 최종 cross-source match 판정

따라서 E2 artifact에는 `representatives`, `representative_images`,
`vision_queue`, `vision_tasks`, `final_matches`, `merge_decisions` table이
없어야 한다. 독립 validator는 이 table들이 없는지도 검사한다.

## 단계 경계

| E2가 하는 일 | E2가 하지 않는 일 |
|---|---|
| Frozen E1 hash와 source relation을 읽는다 | 이미지를 다시 다운로드하지 않는다 |
| Exact normalized-pixel equality를 기록한다 | Exact image equality를 building identity로 해석하지 않는다 |
| 256-bit pHash의 직접 Hamming distance를 계산한다 | pHash의 transitive path를 동일성으로 승격하지 않는다 |
| 보수적인 metadata block 안에서 넓은 pHash 후보를 검사한다 | Metadata name equality만으로 building을 합치지 않는다 |
| Project/building/asset 관계와 직접 근거를 보존한다 | 대표 이미지와 Vision 대상을 선택하지 않는다 |
| Source/input/output lineage와 검증 결과를 저장한다 | Neon, R2, vector DB를 변경하지 않는다 |

네트워크, Vision, LLM 요청 수는 모두 0이어야 한다. E2는 E1에서 저장한
hash와 curated DB의 관계만 사용하는 offline build다.

## 입력

기본 build는 아래 네 파일을 immutable input으로 요구한다.

| 역할 | 경로 | 기대 크기 | 기대 SHA-256 |
|---|---|---:|---|
| Divisare curated metadata | `data/curated/divisare_metadata_v2_4.db` | 2,225,299,456 | `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f` |
| Architizer curated metadata | `data/curated/architizer_curated_v2_0.db` | 8,767,438,848 | `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc` |
| Divisare E1 | `data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db` | 2,646,114,304 | `869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819` |
| Architizer E1 | `data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db` | 4,373,962,752 | `58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937` |

Builder와 validator는 입력별 path, byte size, SHA-256, SQLite
`application_id`, `user_version`, schema manifest를 기록한다. Build 전후
SHA가 같아야 하고, input에 WAL/SHM/journal이 있으면 immutable input으로
인정하지 않는다.

최종 승인 구현은 pipeline version
`archibe-e2-cross-source-image-evidence-pipeline-v5`다. Pipeline 구현 version과
SQLite evidence/schema contract version은 서로 다른 수명 주기를 가지며,
evidence/schema contract는 v1을 유지한다.

E1 raster/hash의 의미와 제한은
[`IMAGE_FINGERPRINT_METHOD.md`](IMAGE_FINGERPRINT_METHOD.md)를 따른다. E2는
1024px 요청, 512px local normalization, response SHA, pixel SHA, 256-bit
pHash 계약을 변경하지 않는다.

## 출력과 수명 주기

이번 실행에서 인수한 full output은 다음 경로다.

```text
data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db
```

Smoke와 full은 서로 다른 파일에 쓴다. Output이 이미 있으면 builder는
덮어쓰지 않는다. Build 중에는 advisory lock과 WAL을 사용하고, terminal
상태가 되면 WAL을 checkpoint한 뒤 DELETE journal mode로 되돌린다.

한 artifact에는 정확히 하나의 run만 존재한다. Run 상태는 다음과 같다.

- `building`: 아직 terminal이 아니며 재개 가능한 상태
- `complete`: 모든 필수 validation을 통과한 terminal artifact
- `failed_validation`: validation 오류를 보존한 terminal artifact

Terminal table은 trigger로 불변이며, 완료 artifact는 immutable read-only로
검사한다. 실패한 E1 asset도 삭제하지 않고 hash가 없는 명시적 상태로
보존한다.

## Evidence 생성 규칙

### Exact normalized-pixel evidence

`normalized_pixel_sha256`가 같은 성공 asset을 exact cluster로 묶는다. 이
값은 동일한 E1 local raster pixels라는 뜻이다. 동일 project 또는 동일
building이라는 뜻은 아니다.

- 중복 pixel SHA를 가진 asset만 `exact_pixel_clusters`에 들어간다.
- 모든 해당 asset은 정확히 한 `exact_pixel_cluster_members` 행을 갖는다.
- Cluster의 source/project/building 수를 원본 관계에서 다시 집계한다.
- 두 소스 asset이 같은 cluster에 있을 때만 direct `exact_pixel` evidence를
  만든다.

### pHash direct evidence

성공 asset은 `phash_hex` 값별 node로 모은다. 서로 다른 pHash node의 전역
후보 생성은 256비트를 서로 겹치지 않는 9개 interleaved band로 나눠
수행한다. 거리 8 이하인 두 hash는 pigeonhole principle에 따라 적어도 한
band를 반드시 공유한다. Band match는 후보 생성용일 뿐이며, 저장 전 전체
256-bit Hamming distance를 다시 계산한다.

| 직접 거리 | E2 처리 |
|---:|---|
| 0 | 같은 pHash node. Cross-source member pair를 `identical_phash` evidence로 기록 |
| 1–8 | `global_le8` direct edge 및 `phash_le8` evidence |
| 9–16 | Frozen metadata block 안에서만 `metadata_9_16` direct review edge 및 `phash_9_16` evidence |
| 17 이상 | Direct edge로 채택하지 않음. 서로 다른 건축물이라는 증거도 아님 |

`A–B`와 `B–C`가 각각 가까워도 `A–C`를 자동으로 가깝거나 동일하다고 보지
않는다. E2는 저장된 직접 edge만 근거로 취급하며, graph component 또는
transitive closure를 building merge에 사용하지 않는다.

### Metadata block evidence

현재 metadata discovery는 두 source building의 보수적으로 정규화한 이름이
정확히 같은 경우를 block으로 사용한다. 이 block은 비교량을 제한하기 위한
discovery seed이지 identity 판정이 아니다.

각 pair에는 name key와 함께 country/locality equality, year evidence 및
두 building의 distinct pHash node Cartesian accounting을 보존한다. 해당
Cartesian product에서 거리 9–16인 직접 pair만 넓은 review evidence가 된다.
Metadata가 같더라도 이미지 근거가 없을 수 있으며, 그 사실 자체도 후보
행에 명시한다.

### Building과 project evidence

`candidate_image_evidence`는 항상 Divisare asset과 Architizer asset의 직접
pair다. 각 행은 evidence kind, exact cluster 또는 pHash edge, 거리,
source-record SHA와 QA detail을 보존한다.

`cross_source_building_candidates`와
`cross_source_project_image_evidence`는 직접 asset evidence를 source
building/project pair별로 집계한다. 이름에 `candidate`가 포함된 이유는 이
행들이 match decision이 아니기 때문이다. Exact/pHash count가 많아도 E2는
두 source entity를 합치지 않는다.

Low-information E1 flag가 포함된 근거는 제거하지 않고 `qa_only` detail로
격리한다.

## SQLite table 구조

| 영역 | Table | 역할 |
|---|---|---|
| Run/lineage | `e2_runs` | 단일 run, version, mode, terminal 상태 |
| Run/lineage | `e2_inputs` | 입력 path/size/SHA/schema와 전후 불변성 |
| Run/lineage | `build_checkpoints` | bounded batch 진행 및 재개 cursor |
| Run/lineage | `e2_metrics`, `e2_validations` | 측정값과 build-time 검증 ledger |
| Source graph | `source_projects`, `source_buildings` | 소스별 entity와 metadata snapshot |
| Source graph | `source_project_buildings` | source project–building membership |
| Asset graph | `assets` | E1 성공/실패/제외 상태와 hash provenance |
| Asset graph | `project_asset_occurrences` | role/ordinal을 보존한 원본 occurrence |
| Asset graph | `project_assets`, `building_assets` | project/building별 asset 집계 관계 |
| Exact | `exact_pixel_clusters` | 중복 normalized pixel SHA cluster |
| Exact | `exact_pixel_cluster_members` | exact cluster의 source asset member |
| pHash | `phash_nodes`, `phash_node_members` | distinct pHash와 source asset member |
| pHash | `phash_candidates` | band 또는 metadata block으로 발견한 직접 비교 후보와 재계산 거리 |
| pHash | `phash_edges` | threshold를 통과한 직접 pHash edge |
| Metadata | `metadata_building_pairs` | 보수적 building block과 Cartesian 회계 |
| Evidence | `candidate_image_evidence` | cross-source direct asset-pair evidence |
| Evidence | `cross_source_building_candidates` | building pair별 direct evidence 집계 |
| Evidence | `cross_source_project_image_evidence` | project pair별 direct evidence 집계 |
| Smoke | `smoke_manifests`, `smoke_manifest_items` | N10/N100 ordered selection과 item-record SHA |

대표 이미지, Vision 작업, merge 또는 final-match policy table은 위 schema에
포함되지 않는다.

## CLI

### Deterministic smoke

아래는 인수된 v5 smoke의 실행 형태다. 같은 명령을 재현할 때는 반드시
`--output`에 존재하지 않는 새 경로를 지정한다. 기존 artifact는 덮어쓰지
않는다.

```powershell
python tools/build_cross_source_image_evidence_e2.py `
  --output data/enrichment/divisare_architizer_image_evidence_e2_smoke_n10_v5.db `
  --sample-size 10

python tools/build_cross_source_image_evidence_e2.py `
  --output data/enrichment/divisare_architizer_image_evidence_e2_smoke_n100_v5.db `
  --sample-size 100
```

`--sample-seed`를 바꾸지 않으면 같은 frozen input에서 ordered selection이
결정론적으로 재현된다. `--batch-size`는 memory/commit 단위를 조정하지만
logical evidence를 바꾸면 안 된다.

### Full offline build

```powershell
python tools/build_cross_source_image_evidence_e2.py `
  --output data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db `
  --full
```

이 명령도 네트워크나 Vision을 사용하지 않는다. 기존 output을 재사용하거나
덮어쓰지 않는다.

### 독립 read-only validation

```powershell
python tools/validate_cross_source_image_evidence_e2.py `
  data/enrichment/divisare_architizer_image_evidence_e2_smoke_n10_v5.db

python tools/validate_cross_source_image_evidence_e2.py `
  data/enrichment/divisare_architizer_image_evidence_e2_smoke_n10_v5.db `
  --json --compact
```

Validator exit code는 `0=PASS`, `1=검증 실패`, `2=실행 오류`다. JSON report의
set-like 값은 안정 정렬된 list로 출력한다.

## 검증 계약

독립 validator는 pipeline 구현을 호출하지 않고 다음을 다시 계산한다.

- Artifact WAL/SHM/journal 부재
- SQLite `quick_check`, `integrity_check`, `foreign_key_check`
- 정확히 하나의 `complete` run과 evidence contract version
- 금지된 representative/Vision/final-match/merge table 부재
- 입력 4개의 현재 path, size, SHA 및 sidecar 부재
- 성공 asset의 pHash node 전수 귀속과 non-success hash 부재
- 중복 pixel asset의 exact cluster 전수 귀속과 source/project/building count
- 모든 pHash candidate/edge의 stable ID, Hamming distance, threshold와 record SHA
- Metadata building pair별 distinct-node Cartesian accounting
- Direct asset evidence의 cluster/edge member 및 building attachment
- Building candidate의 evidence-kind count와 최소 거리
- Ordered selection 및 smoke manifest의 rank, score, item SHA
- Evidence logical manifest와 artifact에 기록된 logical SHA

필수 검증 중 하나라도 실패하면 run을 정상 `complete` artifact로 인수하지
않는다.

## 재현성

SQLite byte layout은 page allocation이나 실행 환경에 따라 달라질 수 있으므로
byte SHA만으로 logical equality를 판단하지 않는다. E2 logical manifest는
evidence table을 primary-key 순서로 읽고, runtime timestamp를 제외한 각 행을
canonical JSON으로 직렬화해 table별 SHA와 최종 logical SHA를 계산한다.

재현 조건은 다음과 같다.

1. 네 입력의 byte SHA가 동일하다.
2. Contract, schema, pipeline, pHash band/pair, metadata normalization, sample
   version이 동일하다.
3. Ordered selection manifest가 동일하다.
4. 독립 validator가 계산한 logical SHA가 artifact의 recorded SHA와 동일하다.
5. 입력 DB의 build 전후 SHA가 동일하고 SQLite sidecar가 없다.

## Smoke 및 full 상태

| Run | 상태 | 시간 | Logical SHA | 독립 validator |
|---|---|---:|---|---:|
| N10 v5 | 완료 | 84.9494 s | `e23fa1957e80ed00d7bf2f309d039c70ededd06d9d03d60a0cb9ebd92de6bdbe` | 31/31 PASS |
| N100 v5 | 완료 | 105.3693 s | `beaa2fdca162883df6c3ef4bc509df0c1bbae491f800571ccbe9d68b5c3e31ba` | 31/31 PASS |
| Full v5 | 완료 | 3,466.4432 s | `795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc` | 31/31 PASS |

세 실행은 모두 offline build이며 network/Vision/LLM 요청은 각각 0이다.
Full v5 run ID는 `e2-e61327cad29ba08b272febe3`, SQLite 본체 크기는
10,164,682,752 bytes, byte SHA-256은
`4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19`다.

### Full v5 전수 회계

| 항목 | 건수 |
|---|---:|
| Source assets | 1,432,025 |
| Source image occurrences | 1,524,434 |
| Project / building asset relations | 1,432,604 / 1,432,588 |
| Distinct pHash nodes / node members | 1,406,740 / 1,429,576 |
| Exact pixel clusters / members | 6,420 / 13,488 |
| Global pHash candidate ledger | 89,636 |
| Accepted direct pHash edges at distance 1–8 | 50,580 |
| Rejected global pHash candidates | 39,056 |
| Metadata building pairs | 6,754 |
| Metadata-constrained pHash edges at distance 9–16 | 2,341 |
| Cross-source building candidates | 9,026 |
| Cross-source project image-evidence pairs | 4,932 |

Metadata block에서는 2,520,561 distinct node pair를 회계했고, 전역/metadata
경로를 합쳐 실제 비교한 distinct node pair는 2,545,879건이다. 이 수치는
이미지 또는 building을 merge한 건수가 아니라 직접 비교와 후보 evidence의
회계다.

### Rejected draft 이력

`full_v1`부터 `full_v4`까지는 삭제하거나 덮어쓰지 않고 진단 근거로 보존한다.
최종 인수 대상은 `full_v5`뿐이다.

| Draft | 인수하지 않은 이유 |
|---|---|
| v1 | Exact cluster 집계의 비효율적인 실행 계획 |
| v2 | pHash node-pair join의 quadratic 실행 계획 |
| v3 | Batch 경계에서 candidate/edge FK 순서가 뒤바뀐 validation 실패 |
| v4 | Metadata building join의 quadratic 실행 계획 |

## 후속 policy gate 후보 — 아직 미결정

E2가 끝난 뒤에도 대표 이미지와 Vision 대상은 자동으로 정하지 않는다. 다음
항목을 별도 N10/N100 정책 실험으로 비교할 수 있다.

정책 실험에 사용할 수 있도록 E2는 원본 occurrence의 role/ordinal,
source-record SHA, E1 품질·실패 flag, exact cluster, direct pHash edge,
metadata block 및 project/building/asset 관계를 보존한다. 이 값들은 선택
근거이지 선택 결과가 아니다.

### 대표 이미지 policy 후보

- Source가 명시한 cover 역할과 gallery ordinal을 우선하는 방식
- 해상도, low-information/parse flag와 결측을 품질 gate로 쓰는 방식
- Exact/pHash 중복군에서 한 장만 남겨 시각적 중복을 줄이는 방식
- Exterior/interior/detail 등 시각적 다양성을 보장하는 방식. 이 의미 정보는
  E2 hash만으로 확정할 수 없으므로 별도 Vision 또는 human label이 필요하다.
- 소스별 1장, building별 복수 장, evidence cluster별 1장 등 서로 다른 예산
  단위
- 대표 이미지가 실패하거나 placeholder 의심일 때의 deterministic fallback

어떤 항목을 우선할지는 제품 목적, source credit, 사용자 카드 UI, 중복 허용량
및 Vision 예산을 함께 정한 뒤 결정해야 한다.

### Vision queue policy 후보

- Curated building마다 대표 후보 1장
- Exact/pHash family마다 중복 제거한 1장
- Cross-source candidate building pair에 연결된 image만 처리
- Metadata와 image evidence가 충돌하거나 부족한 uncertainty case만 처리
- 의미 category/품질 측정을 위한 deterministic stratified sample
- 한 번 분석한 normalized-pixel 또는 accepted image family의 결과를 안전한
  범위에서 재사용

Vision queue는 E2 artifact를 수정하지 않는 별도 immutable sidecar로 만들고,
선택 policy version, E2 logical SHA, ordered manifest, 예상 token/cost를 기록하는
방식이 적합하다. 실제 Vision full 전에는 다시 N10 → N100 smoke ladder와 비용
승인이 필요하다. 이 문서는 어떤 대표 이미지 또는 Vision queue policy도
선택하지 않는다.

## 해석상의 제한

- pHash는 시각적 유사성 신호이지 건축물 identity나 이미지 의미가 아니다.
- Crop, framing, overlay 차이는 같은 원본 이미지의 pHash 거리를 크게 만들 수
  있다.
- Exact pixel equality도 여러 project가 공유한 도면, logo, placeholder 또는
  재사용 사진일 수 있다.
- 보수적 normalized-name block에는 동명이거나 일반적인 이름의 building이
  함께 들어갈 수 있다.
- E1 실패/제외 asset은 명시적으로 보존되지만 hash evidence를 제공하지 않는다.
- E2는 이미지 bytes를 보존하거나 새 semantic label을 생성하지 않는다.

이 제한 때문에 최종 동일 건축물 판정은 E2의 direct evidence와 metadata,
향후 승인된 Vision/human evidence를 함께 사용하되, 별도 versioned policy에서
수행해야 한다.
