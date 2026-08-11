# Cross-source semantic Vision N10

## Status

`N10_COMPLETE_PASS_WITH_OPEN_QA; N100_NOT_RUN_AWAITING_APPROVAL`

The fixed Divisare + Architizer semantic Vision N10 completed and passed its
technical gate. Blind review found no clear scope or medium errors, but found
six uncertainty false negatives. The result is therefore calibration evidence,
not production accuracy evidence and not an authoritative hero selection.

The N100 network/model run has **not** been executed. It remains gated on a
new disjoint fixed manifest, a reviewed cost projection, and explicit user
approval.

## Scope and immutable inputs

This run consumed the previously frozen semantic-coverage manifest. It did not
change E1, E2, E3, curated/source databases, canonical metadata, or any image
selection contract.

| Artifact | Bytes | Byte SHA-256 | Logical/self SHA-256 |
|---|---:|---|---|
| E2 evidence v5 | 10,164,682,752 | `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19` | `795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc` |
| E3 selection Full v1 | 10,236,592,128 | `8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4` | `6b99e4cda9af7c877213a0708f8ba08b1e3780ba3b75c88b7eb9177fc953d3ce` |
| Fixed N10 manifest | 147,921 | `81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f` | self `bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b` |
| Independent manifest replay | 147,921 | `81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f` | byte-identical to fixed manifest |

Manifest lineage:

- seed: `archibe-semantic-coverage-n10-v1`
- ordered building SHA: `8a23bde765b2eee340a950b430371468342bca938cdf9ade90dbb3047b75048b`
- ordered occurrence SHA: `e7fb4750fff10b544c3e62e7c3694978bdcb665275454fd7605556be9cd78e49`
- population: 10 buildings, Architizer 5 / Divisare 5
- selected occurrences: 57
- E1 exact normalized-pixel groups: 57; exact reuse: 0

## Actual fixed N10 execution

- run ID: `semn10-bf2318bd8942ab13ea28c121`
- status: `complete`
- contract: `cross-source-image-semantics-v1.0.0`
- prompt: `cross-source-image-semantics-prompt-v1.0.0`
- model: `gpt-5.6-sol`
- started: `2026-08-11T09:46:26.536703Z`
- completed: `2026-08-11T09:50:36.777247Z`
- wall time: 250.241 seconds
- buildings / selected occurrences / successful results: 10 / 57 / 57
- fetch attempts: 57; all 57 fixed inputs succeeded without replacement
- downloaded bytes: 6,658,781 bytes, or 6.66 MB decimal
- Vision calls: 12, using eleven five-image batches and one two-image batch
- tokens: input 222,983 / cached input 13,056 / output 7,050
- non-double-counted input + output total: 230,033 tokens
- summed Vision-call latency: 171.776 seconds
- run logical SHA-256:
  `10de6fc2a4678c0566beebd93774e97776c2e28239a48f01f4a0ef02001e65dc`

The sample remained frozen: an error would have stayed attached to its selected
occurrence; no failed or changed image could be substituted.

## Artifacts

| Artifact | Bytes | Byte SHA-256 | Role |
|---|---:|---|---|
| `data/enrichment/divisare_architizer_semantic_vision_n10_v1.db` | 835,584 | `30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a` | immutable semantic sidecar |
| `data/reports/divisare_architizer_semantic_vision_n10_v1.md` | 508 | `32b30a95e9292b2c419d11a228de668fd83d1810e48cff98c446c5972f8b12f0` | compact run report |
| `data/reports/divisare_architizer_semantic_vision_n10_v1_blind_qa.md` | 3,907 | `fc8cd53b3577924e2829c2b7f780b7f3dffe6ea4b74f1b1e7674f0442b334f23` | blind QA record |

The SQLite sidecar records metadata and hashes, not permanent downloaded image
files. Temporary downloaded images and generated review sheets used for blind
QA were removed after review. No temporary image artifact is retained as an
input to a later stage.

## Independent validator and read-only inspector

The independent validator replayed the fixed manifest lineage and checked:

- fixed population, input state, result, and attempt accounting;
- manifest byte/self SHA and ordered building/occurrence SHA;
- payload and semantic-derivation integrity;
- no pending work in the terminal run;
- SQLite `quick_check`, `integrity_check`, and foreign keys;
- E2/E3 input SHA before/after and absence of SQLite sidecars.

All required checks passed. The read-only inspector opened the result as
`mode=ro&immutable=1`, independently aggregated the semantic fields, hero tiers,
coverage slots, runtime, and N100 projection, and recorded zero network, model,
Vision, and database-write operations. It also supports explicit missing-result
and missing-hero accounting for legitimate future `complete_with_failures`
runs; this completed N10 has neither kind of missing row.

## Hero and coverage observations

Hero tiers across 57 results:

| Tier | Count |
|---|---:|
| preferred | 37 |
| eligible | 8 |
| fallback | 9 |
| qa_only | 2 |
| rejected | 1 |

Nine non-QA P1 rank-one anchors were evaluated; eight were in scope. The wider
gallery probe added 15 building-slot pairs outside the P2 top three for 7 of 10
buildings:

| Newly observed slot | Buildings |
|---|---:|
| interior | 5 |
| detail | 3 |
| drawing plan | 2 |
| exterior overall | 2 |
| drawing other | 1 |
| drawing section | 1 |
| model/render | 1 |

This demonstrates that P2 top three alone does not preserve all useful project
views. It does not yet establish the optimal number of images for full Vision.

## Blind QA and open rows

Blind QA inspected all 57 images without source or project metadata. There were
zero clear scope errors and zero clear medium errors. The model emitted zero
uncertain axes and zero resolution-insufficient flags, while the reviewer found
six defensible but borderline rows that should have surfaced uncertainty:

| Inference ID | Open QA |
|---|---|
| `semv_000013` | Landscape-dominant rejection is acceptable, but tiny distant buildings make scope versus very-low legibility uncertain. This rejected P1 anchor must not become a hero. |
| `semv_000019` | Rendering is defensible, but the polished office view is photograph-like enough to require medium uncertainty. |
| `semv_000035` | Architecture is readable through trees, but obstruction and multiple buildings make scope/legibility borderline. |
| `semv_000036` | Rooftop terrace and canopy can be read as threshold or exterior; spatial context should be uncertain. |
| `semv_000054` | Glazed edge and deck make threshold defensible, but exterior element detail is also plausible. |
| `semv_000055` | Glazed facade and exterior ledge create the same threshold/exterior boundary. |

Disposition: `PASS_WITH_OPEN_QA`. The six rows are uncertainty-calibration
false negatives, not proven primary-label errors. Their 6/57 rate must be
measured on N100 before any production or selective-full decision.

## Proposed N100; not executed

N100 must use a deterministic, population-shaped sample disjoint from N10 and
must retain the v1.0 contract for a comparable measurement. The current
empirical projection is:

| Measure | N100 projection |
|---|---:|
| Buildings / images | 100 / approximately 570 |
| Vision calls | 114 theoretical five-image batches; up to 120 by direct N10 scaling |
| Download | approximately 66.6 MB decimal |
| Input / output tokens | approximately 2,229,830 / 70,500 |
| Total input + output | approximately 2.30 million tokens |
| Wall time | approximately 35-50 minutes |

The 35-50 minute range brackets the direct ten-times wall estimate of about
41.7 minutes. Token cost and weekly-quota percentage cannot be derived reliably
from the runtime and must not be fabricated. Before N100, present the frozen
manifest, exact planned image/call count, and cost/quota estimate for explicit
user approval. If zero or near-zero model uncertainty repeats, revise the
prompt in a new version and re-smoke rather than proceeding to selective full.

## Post-run runner hardening

The immutable N10 artifact records runner v1.0.0 and completed without an
interruption. A post-run static review found no corruption in that path, but
found weaknesses in crash resume, optional review-cache provenance, lock-file
removal, and two-file publication. The code was hardened offline as runner and
retry policy v1.1 without rerunning or modifying the artifact:

- a committed `ready` input resumes from a SHA-checked durable spool with zero
  additional fetch attempt or retry-budget use;
- cache publication is atomic, cache path/retention is resume-provenanced, and
  file accounting fails closed;
- the advisory lock file remains persistent, avoiding unlink/inode races;
- DB/report hard-link publication rolls back the newly linked DB on any report
  link failure, and new reports are content-bound to their DB;
- logical fetch attempts are process-wide paced at 2 requests/second;
- the N10 CLI pins literal byte, self, population, building-order, and
  occurrence-order identities before entering the network/model runner;
- validator retry replay and inspector `complete_with_failures` accounting are
  explicit; stdout/stderr retention is bounded and credential-redacted.

The legacy v1.0.0 artifact remains readable and independently passes. New
v1.1 artifacts enforce the stronger report and resume rules. Residual limits:
redirect hops inside one logical fetch are not paced separately; cache publish
and SQLite commit cannot be one filesystem transaction; DB and report use
rollback rather than a cross-file atomic transaction.

## Tests and safety

- focused semantic selection, fetch, sidecar, runner, validator, inspector,
  and review tooling: `112 passed`
- full repository pytest: `966 passed, 22 skipped, 1,453 subtests passed`
  in 191.07 seconds
- pytest environment: existing `.venv`; no packages installed; inaccessible
  legacy `.pytest_cache` isolated with `-p no:cacheprovider`
- network fetches in the completed N10: 57 fixed image requests
- Vision/model calls in the completed N10: 12
- additional network/model calls during validation, inspection, and blind QA:
  0
- retained downloaded/review images: 0
- E1/E2/E3, source/curated DB, Neon, R2, vector DB modifications: 0
- N100, N1000, selective full, and full Vision runs: 0
