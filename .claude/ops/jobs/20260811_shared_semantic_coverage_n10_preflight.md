# Shared semantic-coverage N10 preflight

## Status

`OFFLINE_N10_PLAN_COMPLETE_VALIDATED; VISION_N10_AWAITING_APPROVAL`

## Objective and boundary

Freeze a source-balanced sample that can compare the E3 P2 representative
top-three against a wider gallery probe. This job performs no image download,
Vision/LLM call, final hero choice, cross-source building merge, or database
write outside a new ignored JSON manifest.

## Inputs

| Artifact | Bytes | Byte SHA-256 | Logical SHA-256 |
|---|---:|---|---|
| E2 evidence v5 | 10,164,682,752 | `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19` | `795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc` |
| E3 selection Full v1 | 10,236,592,128 | `8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4` | `6b99e4cda9af7c877213a0708f8ba08b1e3780ba3b75c88b7eb9177fc953d3ce` |

Both inputs were opened `mode=ro&immutable=1`, had no WAL/SHM/journal/lock,
and retained the same byte SHA before and after planner and validator runs.

## Frozen planner

- seed: `archibe-semantic-coverage-n10-v1`
- 10 buildings: Architizer 5 / Divisare 5
- guard order: Architizer QA fallback; both-source P1 rank-one change; both-
  source P2 top-three set change; both-source gallery fallback; both-source
  cross-source candidate; Divisare ordinary long-gallery control
- per building: P2 ranks 1-3, then non-redundant gallery early/middle/late,
  maximum six occurrences
- redundancy: exact normalized pixels, identical pHash, or direct Hamming
  distance <=8 against an already selected anchor/probe only
- pHash semantic reuse and transitive closure: forbidden
- E2 join pins raw response SHA, normalized pixel SHA, pHash, URL, dimensions,
  quality flags, role, ordinal, and relation/source-record lineage

Population audit: 91,803 total = 91,183 eligible + 620 no-success. P1 rank-one
changes are Architizer 25 / Divisare 30; P2 top-three set changes are 283 / 29;
QA fallbacks are 14 / 0.

## Fixed N10 result

| Rank | Guard | Source | Building | Occurrences |
|---:|---|---|---|---:|
| 1 | QA fallback | Architizer | Town Hall in San Michele al Tagliamento | 6 |
| 2 | P1 rank-one changed | Architizer | High school | 6 |
| 3 | P1 rank-one changed | Divisare | Villa 'under' Extension | 6 |
| 4 | P2 top-three changed | Architizer | Swivel | 5 |
| 5 | P2 top-three changed | Divisare | The Room @ Technicolor | 4 |
| 6 | gallery fallback | Architizer | Shared space | 6 |
| 7 | gallery fallback | Divisare | New roof top element Nibelungengasse | 6 |
| 8 | cross-source candidate | Architizer | F.LOT | 6 |
| 9 | cross-source candidate | Divisare | Termitary House | 6 |
| 10 | ordinary long gallery | Divisare | H1 HOUSE | 6 |

- occurrence memberships: 57
- provisional E1 exact-pixel groups: 57
- provisional exact reuse savings: 0
- manifest: `data/reports/cross_source_semantic_coverage_n10_v1.json`
- manifest bytes / file SHA: 147,921 /
  `81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f`
- manifest self SHA:
  `bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b`
- ordered building SHA:
  `8a23bde765b2eee340a950b430371468342bca938cdf9ade90dbb3047b75048b`
- ordered occurrence SHA:
  `e7fb4750fff10b544c3e62e7c3694978bdcb665275454fd7605556be9cd78e49`

Independent replay rebuilt the population, all guards, each candidate choice,
and every selected E2 join without calling the planner selection functions:
18/18 checks passed. Focused semantic tests: 14 passed. A second no-clobber
output rebuilt from the same inputs was byte-identical at 147,921 bytes and
the same file SHA. The full offline repository suite passed with 868 tests,
22 skips, and 1,453 subtests; the only warning was the known inaccessible
`.pytest_cache` path.

## Future Vision N10 estimate; not executed

Using the prior fresh Divisare axes N50 observation only as a calibration
baseline (205,382 input and 7,091 output tokens for 50 images), 57 images would
project about 234,135 input and 8,084 output tokens. At five images per batch,
the upper request count is 12. The old download rate projects roughly 17.8 MB.
The new prompt/schema can change all of these values, so N10 must record actual
tokens, bytes, latency, and retries before any N100 estimate is accepted.

The Codex runtime does not expose a reliable token-to-weekly-quota conversion
or actual dollar charge. Those values must remain unknown rather than be
fabricated. The actual N10 requires explicit user approval.

## Provisional full-population sizing; not a queue

A read-only algorithm prototype over all eligible buildings produced 521,575
occurrence selections and 520,436 E1 exact-pixel groups. This is a planning
ceiling, not approval to fetch or analyze those images. Actual Vision reuse is
allowed only after the later 1024px Vision-input pixel identity, dimensions,
prompt/schema, and model all match.

## Safety result

- network/image fetch: 0
- Vision/LLM calls and tokens spent by this job: 0
- source/canonical/Neon/R2/vector writes: 0
- image files retained: 0
- E1/E2/E3 and curated DB modifications: 0
