# Cross-source semantic Vision N10 v1.1

## Status

`N10_COMPLETE; RUNTIME_VALIDATION_PASS; BLIND_QA_FAIL; V1.1_N100_AND_FULL_NOT_RUN`

이 작업은 Divisare + Architizer 이미지의 의미를 픽셀만 보고 분류하는
semantic Vision prompt v1.1을 새 고정 표본에서 검증한 N10 smoke다. E2는 이미지
evidence inventory, E3는 deterministic representative/coverage selection이며, 이
N10은 그중 10 buildings / 53 occurrences를 읽는 calibration artifact다. 최종
semantic corpus나 cross-source building merge가 아니다.

기술 실행과 immutable artifact 검증은 모두 통과했다. 그러나 source-blind
reference와 비교한 semantic quality gate는 `FAIL`이다. 따라서 v1.1 N100 및
Full은 실행하지 않았다.

## Why prompt v1.1

이전 v1 N100은 555 results를 만들었지만 다음이 측정됐다.

- scope clear error 10건, medium clear error 28건;
- reference uncertainty 70/555 rows에 비해 model uncertainty 6/555 rows;
- uncertainty axis recall `0.0395`;
- model hero가 usable이라고 본 critical false positive 26건.

v1.1은 output schema와 1024px JPEG transform을 유지하고 prompt/contract만 새
immutable version으로 만들었다.

- product/art/person 대 project dominance와 low-legibility scope 경계를 명시;
- rendering은 visible synthesis evidence가 있을 때만 선택하고 polished photo와
  혼동되면 `medium` uncertainty를 요구;
- interior/threshold/exterior, framing, camera angle, state, legibility를 각각
  독립 점검;
- second plausible value가 남으면 tie가 아니어도 uncertainty를 표시;
- `unknown`과 `resolution_insufficient`의 사용 조건을 fail-closed로 구체화;
- source, project/name, URL, filename, rank, prior label은 prompt에 노출하지 않음.

Contract/prompt:

- contract: `cross-source-image-semantics-v1.1.0`
- prompt: `cross-source-image-semantics-prompt-v1.1.0`
- prompt template SHA-256:
  `42b43a9168ea3315809182da5ff6a5ecdf280cbaabbfa2a369a88806cc7d50ba`
- output schema SHA-256:
  `e72058e372e7883fc8ca80d690728fb16b9a7fdd469037f13311a8e04069eff4`
- transform: `cross-source-vision-input-1024-jpeg-v1.0.0`

## Frozen production selection

Manifest:
`data/reports/cross_source_semantic_coverage_n10_v1_1.json`

- seed: `archibe-semantic-coverage-n10-v1.1`
- selection rule: `population-core8-plus-risk2-v1`
- 10 buildings / 53 occurrences / 53 unique planned E1 pixels;
- source: Architizer 7 buildings / 35 occurrences, Divisare 3 / 18;
- population core 8: Architizer ordinary 5 + cross-source candidate 1,
  Divisare ordinary 2;
- bounded risk sentinel 2: Architizer cover-quality risk 1, Divisare
  gallery-fallback 1;
- eligible population 91,183; prior-collision exclusions 111; sampling frame
  91,072;
- selection은 pre-existing E3 strata만 사용했고 prior semantic result/error ID를
  사용한 cherry-pick이 아님.

Prior semantic exclusion ledger:

- prior N10 + N100 buildings: 110;
- expected E1 pixels: 619;
- successful actual derivative pixels: 612;
- unique prior URLs: 1,238;
- seven earlier `source_changed` rows have expected identity only; fabricated
  derivative identity를 만들지 않음;
- selected overlap: building 0 / expected E1 0 / fetch URL 0 /
  successful derivative pixel 0.

Manifest identities:

- bytes: 922,575;
- byte SHA-256:
  `3315a48188aec4c9461867235999ac7e526b8b011a869cafdd3be9d3cd1cc5b6`
- self SHA-256:
  `91957a822915ae70f56fd1a5f82244098a4508274cc5dbbdd91e1d6bf03ad8c9`
- ordered building SHA-256:
  `7b4c9558682f231e4fce29d0e915666fe1968707e1fea177d58ac0ad514af55d`
- ordered occurrence SHA-256:
  `0be0449356388c117e59856caacdf14900b694b0c1ee811f807b956c9449fcb6`
- ordered fetch URL SHA-256:
  `9de60a38b4ef5e05744c0761c13e84d0a33fe88df6e09ea4b8c9ea24ac527c43`
- ordered E1 pixel SHA-256:
  `0ec3a756a3a55480b077e6eb114f4b2df9d1497f0b4dd015e53008e1783324a8`

Independent planner replay regenerated the complete canonical manifest
byte-for-byte. Validation was 7/7 PASS with network/Vision/LLM requests 0.

## Runner hardening

The v1.1 runner is a new schema/retry/runtime version; prior v1 artifacts were
not mutated.

- every physical HTTP hop and Vision call is reserved and committed before the
  external call, then paired with a durable completion;
- all 53 fixed inputs must materialize before the first Vision reservation;
- current response SHA and E1 pixel SHA must both equal the manifest;
- `source_changed` is one fixed failure with no retry-by-replacement;
- actual 1024 derivative pixels must be unique within N10 and disjoint from the
  prior 612 before any Vision call;
- fixed batches of five, two Vision attempts per batch, maximum 13 model-call
  reservations;
- observed stop thresholds: input 260,000 / output 10,000 / total 270,000.
  These are post-call observed thresholds, not a server hard cap; unknown usage
  blocks the next paid call;
- retry/circuit state, crash-open reservations, advisory lock, initialization,
  terminal two-file publication, and completed resume are recoverable;
- fresh output/report/cache and partial artifacts are no-clobber;
- independent validator replays manifest rows, request ledgers, raw JSONL,
  normalization, derived hero/coverage rows, schema objects, logical SHA, report,
  SQLite checks, and retained derivative cache;
- Codex executable discovery is version-gated at `>=0.146.0`. Production used
  the explicit npm binary because desktop auto-discovery found unsupported
  `0.133.0`.

Frozen runtime pins:

- runner: `cross-source-semantic-vision-n10-v1.1-runner-v2.0.0`
- retry: `cross-source-semantic-vision-n10-v1.1-retry-v2.0.0`
- schema version: 3
- schema SQL SHA-256:
  `85b12261ce7c720c21bbad74669c7bc89b0c7e185ba259ff9a552e15608df31e`
- SQLite schema objects SHA-256:
  `959f95fe3a3ae9f6daa6ee9978eedc9abc00a5bad6c7fe6ef577ac03c02c3fe1`
- Codex CLI: `0.147.0`
- model / reasoning / service tier / image detail:
  `gpt-5.6-sol` / `low` / `fast` / `high`.

## Actual N10 run

- run ID: `semn10v11-b1c3c595b0810a2c91e02a61`
- status: `complete`
- stored UTC start/completion:
  `2026-08-12T06:25:24.826360Z` / `2026-08-12T06:29:34.575823Z`
  (about 249.75 seconds);
- HTTP reservations/completions: 53/53;
- fetch attempts: 53, all first-attempt exact matches;
- response SHA exact: 53/53; E1 pixel exact: 53/53;
- redirects / retries / fixed-input failures / replacements: 0/0/0/0;
- downloaded: 7,350,365 bytes;
- actual derivatives: 53 distinct, prior derivative collisions 0;
- derivative gate event: one, with 53 ready inputs, prior count 612, and zero
  Vision reservations before gate;
- Vision batches: 11/11 first-attempt success (10 x 5 + 1 x 3);
- semantic results and same-occurrence links: 53/53;
- input / cached-input / output tokens:
  207,042 / 39,168 / 6,981;
- non-double-counted input + output: 214,023 tokens;
- unknown-usage attempts: 0.

Outputs:

- DB: `data/enrichment/divisare_architizer_semantic_vision_n10_v1_1.db`
- DB bytes: 888,832
- DB byte SHA-256:
  `03d99eb3b624a9211154643a3c29fca337f763442fb086b76d25bca2ee106172`
- logical SHA-256:
  `912a51380ce57947bd304057849e9d1dbdcc4fc36c3491e2e6c6614bc691ef3e`
- report: `data/reports/divisare_architizer_semantic_vision_n10_v1_1.md`
- report byte SHA-256:
  `665f7f3977342a00229f65ef4b6d371a0c9567ae9af547cfe2c286c81c406fd3`
- retained review cache: 53 exact JPEG derivatives.

## Independent validation and resume

Independent terminal validation passed every error-severity check. Counts were
10 buildings / 53 occurrences / 53 results, with 53 HTTP and 11 model-call
reservations, no open reservation, `quick_check=ok`, `integrity_check=ok`, zero
foreign-key violations, and no WAL/SHM/journal. Report and logical SHA replayed
exactly. A completed `--resume` made HTTP 0 / Vision 0 requests.

## Source-blind review and quality verdict

Review images were rendered as nine sheets with opaque inference IDs. The
reference was completed before semantic DB unblinding. It is a source-blind
Codex agent reference, **not independent human gold**.

- review manifest:
  `data/enrichment/divisare_architizer_semantic_vision_n10_v1_1_review_sheets/manifest.json`
- review manifest bytes / byte SHA-256: 27,176 /
  `614b43ba051893ad692862e1eb95025ee221e52bcdac334be84f89858e0f5530`
- review manifest self SHA-256:
  `d5b326c393a8c1f6a6900af7dc47e7974ab12d4ddbba77b6d5ce4b4fe464cfee`
- frozen ledger:
  `data/reports/divisare_architizer_semantic_vision_n10_v1_1_agent_reference_frozen.json`
- frozen ledger bytes / byte SHA-256: 41,712 /
  `e08a23c05d891f53121746b53b0f624d7585cf5cd210e564c57bbb828d1c70c6`
- frozen ledger semantic SHA-256:
  `67060b941dba655e7241a0033ebb24ecf657dadaf60b571b315ddc1e1a8278e9`
- inspection:
  `data/reports/divisare_architizer_semantic_vision_n10_v1_1_inspection.json`
- inspection bytes / byte SHA-256: 13,492 /
  `76e5b3b70981a22fef8efeda909235c154c0e9c09a2f4773c82efa94e04c3520`
- inspection semantic SHA-256:
  `02dcf9abc26cac70b3579fbdd81980ffc45fcf7ab011f19b76a9f00919abcf2e`

Measured QA:

- exact population accounting: PASS;
- nine-field acceptable agreement: 457/477 = `0.958071` (PASS >= 0.90);
- scope clear errors: 0 (PASS);
- medium clear errors: 1, `semv11_000018` photograph vs model mixed (FAIL);
- framing acceptable agreement: `0.886792`, six clear errors;
- project-legibility acceptable agreement: `0.849057`, eight clear errors;
- spatial-context agreement: `0.943396`; camera-angle: `0.962264`;
- uncertainty reference/model rows: 11 / 5;
- uncertainty row precision / recall / F1:
  `0.2` / `0.090909` / `0.125`;
- uncertainty axis TP / FP / FN: 0 / 7 / 12; precision/recall/F1 all `0.0`;
- resolution-insufficient TP / FP / FN: 0 / 1 / 1;
- critical hero false positives: 0 (PASS);
- usable hero retention: 30/30 = 1.0 (PASS).

Final inspection verdict: `FAIL`. v1.1 improved important scope behavior in
this small disjoint sample, but it did not meet the zero-medium-error gate and
still failed uncertainty sensitivity. A new prompt version and another disjoint
N10 are required before any v1.1-style N100 proposal.

An additional 18-row blind corroboration for sheets 001-003 was produced after
the frozen reference and is not substituted into its identity:
`data/reports/cross_source_semantic_vision_n10_v1_1_blind_review_chunk_sheets_001_003.json`,
byte SHA-256
`c15a1a8f1037db70e1a9b5d7b2ac616ab9b38804be38d9d412ead65785d70c69`.

## Immutable inputs and prohibited work

- E2: `data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db`
  - bytes: 10,164,682,752
  - SHA-256:
    `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19`
- E3: `data/enrichment/divisare_architizer_image_selection_e3_full_v1.db`
  - bytes: 10,236,592,128
  - SHA-256:
    `8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4`

E2/E3 size and SHA were identical before/after. Review inspection also proved
the semantic DB unchanged. No E1/E2/E3, curated/source DB, Divisare/Architizer
metadata, common schema/vocab, Neon, R2, vector DB, or prior semantic artifact
was modified. No v1.1 N100, Full, selective Vision queue, building merge, or
upload was run.

## Implementation map

Core implementation is isolated in:

- `canonical/cross_source_semantic_coverage_n10_v11.py`
- `canonical/cross_source_semantic_coverage_n10_v11_validator.py`
- `canonical/cross_source_semantic_fetch_v11.py`
- `canonical/cross_source_semantic_vision_v11.py`
- `canonical/cross_source_semantic_vision_n10_v11_sidecar.py`
- `canonical/cross_source_semantic_vision_n10_v11_validator.py`
- `tools/plan_cross_source_semantic_coverage_n10_v11.py`
- `tools/validate_cross_source_semantic_coverage_n10_v11.py`
- `tools/run_cross_source_semantic_vision_n10_v11.py`
- `tools/validate_cross_source_semantic_vision_n10_v11.py`
- `tools/render_cross_source_semantic_review_sheets_n10_v11.py`
- `tools/inspect_cross_source_semantic_vision_n10_v11.py`
- matching `tests/test_cross_source_semantic_*v11*.py` tests.

## Tests and Git handoff

- selection planner/validator focused tests: PASS;
- runtime/fetch/validator/CLI focused subset after hardening: 20 passed;
- integrated v1.1 focused suite observed before production: 42 passed;
- production manifest independent validation: 7/7 PASS;
- production terminal independent validation: PASS, failed error checks 0;
- **final repository pytest:** `1055 passed, 22 skipped` in 222.37 seconds;
- **final py_compile:** PASS for all 21 new Python files;
- **final git diff --check:** PASS;
- production terminal validator rerun: 30/30 PASS, HTTP/model requests 0;
- test temporary artifacts: retained and explicitly excluded from staging because
  repository policy requires user confirmation before directory deletion;
- Git commit: recorded in the final handoff; this job card is part of that same
  logical commit;
- origin/main push: not performed because this task did not explicitly request
  a push;
- staging policy: only the v1.1 canonical/tools/tests, this job card, and the
  semantic coverage document; no `data/` or temporary artifact paths.
