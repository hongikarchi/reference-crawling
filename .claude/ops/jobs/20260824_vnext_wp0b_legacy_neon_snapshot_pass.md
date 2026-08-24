# vNext WP0b — legacy Neon full snapshot PASS

- Date: 2026-08-24 KST
- Result: **PASS**
- Pipeline LLM/model calls: 0; token cost: $0
- Successful Neon connections: 1 (`make_web` SELECT-only)
- SQL writes: 0; Neon/R2/schema mutations: 0
- Deletions/overwrites: 0

## Scope

게이트 리뷰 F4를 반영한 커밋 `ad9f99f`의 exporter/validator로 live
`archi_data.public.canonical_v2_buildings` 39,478행을 repeatable-read/read-only
transaction에서 읽었다. live 45컬럼 exact-set을 확인한 뒤 `embedding`만 제외한
44컬럼을 canonical ID-order JSONL로 고정하고 독립 validator로 전량 재검증했다.

## Inputs and safety proof

- authoritative plan SHA-256:
  `751a81288ff13cd659b6834752007b65601770fc8f333395fdd1e8ff5e77c9fc`
- exporter SHA-256:
  `5f33ec6565baa3d05e67edbc4a6cf41a18fe3e76de4d6621cba4d8e97332b2c1`
- validator SHA-256:
  `cfbd6aa2cc72969c660f74613e567198ea0523710d7e1abef46d19c80d0354af`
- credential profile: `.env.make-web` `BUILDINGS_DB_*`; 값은 출력·기록하지 않음
- database user: `make_web`
- `role_can_select=true`, `role_can_write_table=false`
- `transaction_read_only=on`
- transaction snapshot: `912387:912387:`
- live rows = distinct PK = 39,478
- live column count 45; export column count 44; excluded column exactly `embedding`

첫 sandbox 연결은 예상된 WinError 10013(TCP 5432 permission denied)으로 connection 전
중단됐다. 이 시도는 output/partial/report를 만들지 않았다. 사용자 승인 범위에서 sandbox
밖으로 동일 명령을 재실행해 SELECT-only 연결에 성공했다.

`tools/verify_env.py`는 UTF-8 모드에서 `.env`, `.env.make-web`, `psycopg2`를 확인했다.
해당 도구가 보고한 torch/transformers/ijson 및 별도 must-copy 데이터 누락은 WP0b가
import하거나 읽는 입력이 아니므로 이번 dump의 blocker가 아니다.

## Outputs

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `data/canonical/legacy_neon_snapshot_2026q3.jsonl` | 728,602,371 | `17e8da1d74f1508b80580b40a2992aa536621214992747ed39978eba1f04527f` |
| `data/canonical/legacy_neon_snapshot_2026q3.report.json` | 3,037 | `c1b6f699a5e9b2e37f71ef3a489ead96635a4885c6c4f73da27cfa9f6e972319` |
| `data/canonical/legacy_neon_snapshot_2026q3.validation.json` | 1,662 | `576dc4893eb5c642ed18354d23e52c2c661b68cd8fb2e179987e34a90eb7fd91` |

- first ID: `bld_000000`
- last ID: `bld_039775`
- `source_refs` root: object 39,478/39,478
- output/report/validation `.partial`: 모두 없음
- no-clobber target을 한 번만 승격했으며 재실행하지 않음

## Independent validation

- status: **PASS**
- rows: 39,478
- expected keys: 44/44 on every row
- canonical UTF-8 JSONL, LF, strict ID ordering/uniqueness: PASS
- UTC `created_at`/`updated_at`: PASS
- errors: 0
- validator file SHA = exporter file SHA = direct post-run rehash:
  `17e8da1d74f1508b80580b40a2992aa536621214992747ed39978eba1f04527f`

## Baseline observations

아래 수치는 snapshot 보존 확인용이며 값을 고치거나 production 품질을 재판정한 것이 아니다.

| Field | non-NULL/true/non-empty rows |
|---|---:|
| `is_publishable=true` | 36,673 |
| non-empty `image_derived` | 39,478 |
| `era` | 38,185 |
| `scale` | 39,478 |
| `structural_system` | 34,856 |
| `roof_type` | 23,646 |
| `facade_pattern` | 34,816 |

따라서 legacy 1:1 crosswalk, serving baseline, `image_derived`, cover/CDN 상태와 R4 5축을
후속 projection에서 보존할 WP0b 입력이 준비됐다.

## Gate

WP0b는 완료됐다. 다음 단계는 이 artifact에 대한 Claude gate 확인이며, 그 전에는 WP1을
시작하거나 Neon/R2 write를 수행하지 않는다.
