# Divisare Vision benchmark

## Purpose

This benchmark decides how much image resolution is needed for Divisare image
semantics before a full image run is designed. It is deliberately separate
from image identity and duplicate detection.

The current work has two phases:

1. N10 runtime calibration: verify download, decoding, local derivatives,
   Codex Vision schema, latency, token accounting, failure handling, and
   resume behavior.
2. Frozen human-gold N100: compare resolution accuracy on a balanced semantic
   sample. No accuracy or production-resolution claim is allowed before this
   phase is complete.

The immutable input is:

```text
data/curated/divisare_metadata_v2_4.db
```

The benchmark writes only a separate SQLite sidecar and Markdown report. It
does not update v2.4, Neon, R2, or a vector database.

## Image contract

For each selected asset, the runner performs one source request using a
Divisare/Cloudinary maximum-long-edge 2048 transform:

```text
c_limit,f_jpg,h_2048,q_92,w_2048
```

PDF inputs additionally use `pg_1`. The response is decoded once, EXIF
orientation is applied, the first frame is used, alpha is composited onto
white, and pixels are converted to RGB. Both comparison lanes are then made
from that same decoded source:

| Lane | Local long edge | Encoding |
|---|---:|---|
| `long1024` | at most 1024 px | JPEG, quality 92, 4:4:4, non-progressive |
| `long2048` | at most 2048 px | JPEG, quality 92, 4:4:4, non-progressive |

LANCZOS is used only when downsampling. Smaller source images are never
upscaled, so the two lanes can legitimately have identical dimensions and
pixels.

Images exist only in a batch-local temporary directory while Codex is called.
The directory is removed when the batch exits, including on an exception. The
sidecar stores URLs, dimensions, formats, byte counts, encoded/pixel SHA-256,
model outputs, diagnostics, and token usage, but not image bytes.

`raw_patch_count` is a simple 32-pixel-grid count for the upload derivative,
not the effective or billed image-token count. CLI `detail=high` may resize
internally, so this experiment compares 1024 and 2048 uploads processed under
the same CLI-high behavior; it does not claim every 2048 pixel reaches the
model unchanged.

### Why there is no original lane

The installed Codex CLI sends attached images with `detail=high`. That value
is observed and recorded as provenance; the CLI currently provides no working
control for `detail=original`. Attaching a larger file would therefore not be
a controlled original-detail experiment.

The first benchmark compares only 1024 and 2048 under the same CLI
`detail=high` behavior. If small architectural details remain unresolved, a
later experiment must use controlled high-resolution crops/tiles or a runtime
that explicitly supports original detail. That experiment needs its own smoke
test and cost approval.

## Semantic contract

The prompt receives the images and opaque ordered IDs such as `sample-0001`
only. Divisare tags, article hints, filenames, project names, source asset keys,
and candidate strata are not sent to the model. They may help discover a
varied benchmark sample, but they are not evidence for a Vision label.

Codex runs with its working root set to the batch temporary directory, sandbox
`read-only`, and user config/rules ignored. That directory contains only
opaque-named derivatives and the output schema, preventing repository content
from becoming non-pixel evidence and preventing workspace writes.

The strict result contains:

- `medium`: photograph, drawing, rendering, physical model, mixed, other, or
  unknown
- `view`: exterior, interior, aerial, detail, plan, section, elevation,
  axonometric, site plan, diagram, construction, portrait, object, mixed,
  other, or unknown
- controlled lists of visible materials and discriminative architectural
  elements
- `needs_detail_review`, confidence, and one short visible-evidence sentence

The legacy five-type value is derived from `medium` and `view`. Unclear input
stays `unknown`; it is never defaulted to `exterior`. Unsupported vocabulary,
missing assets, duplicate asset IDs, incomplete batches, parse errors, and
runtime errors remain explicit failures.

Materials and elements are collected during N10 to validate the schema. The
five-class N100 gold set described below validates media/view resolution. It
does not establish material or element accuracy; those multilabel fields need
a separate frozen annotation contract before quality claims are made.

## Phase 1: N10 runtime calibration

N10 uses a deterministic weak-prior selector. It rotates through diagnostic
cohorts such as convertible documents, filename/article media hints, special
media, interior, material/element, multiple-URL edges, cover, legacy, and
plain gallery assets. The hints are retained for audit only and are excluded
from the model prompt.

This sample is suitable for validating mechanics and estimating cost. It is
not semantic ground truth and lane agreement is not accuracy.

From the repository root, run targeted offline tests first:

```powershell
.venv-images\Scripts\python.exe -m pytest -q `
  tests\test_divisare_vision_benchmark.py `
  tests\test_divisare_vision_runtime.py
```

Then run N10 with fresh output paths:

```powershell
.venv-images\Scripts\python.exe tools\run_divisare_vision_benchmark.py `
  --source-db data\curated\divisare_metadata_v2_4.db `
  --output-db data\smoke\divisare_vision_resolution_n10_v2.db `
  --report data\reports\smoke\divisare_vision_resolution_n10_v2.md `
  --limit 10 `
  --batch-size 5
```

`--codex-bin <path>` or `CODEX_BIN` can override desktop CLI discovery, and
`--model` can override the default `gpt-5.6-sol`. CLI `0.146.0` or newer is
required; an older desktop-bundled `0.138.0-alpha.7` was rejected by the model
service. The runtime uses reasoning `low`, service tier `fast`, and a
600-second timeout per lane batch.

If a run stops after creating the partial sidecar, resume the exact same
contract:

```powershell
.venv-images\Scripts\python.exe tools\run_divisare_vision_benchmark.py `
  --source-db data\curated\divisare_metadata_v2_4.db `
  --output-db data\smoke\divisare_vision_resolution_n10_v2.db `
  --report data\reports\smoke\divisare_vision_resolution_n10_v2.md `
  --limit 10 `
  --batch-size 5 `
  --resume
```

Resume accepts only a `running` partial whose source path/SHA, sample manifest,
algorithm versions, lanes, batch size, model settings, CLI version, and CLI
detail match. Successful lanes are not called again. Because image bytes are
intentionally not retained, an asset may be downloaded and derived again when
its other lane still needs work. The response and both derivative hashes must
exactly reproduce retained evidence or resume aborts without mixing inputs.

### N10 gates

N10 passes only when:

- all 10 frozen sample rows fetch successfully; failed assets are not silently
  replaced
- both derivatives and strict Vision results exist for all 20 asset/lane rows
- every Vision batch has exact asset accounting and valid controlled values
- source SHA before and after is identical
- SQLite `quick_check` is `ok` and foreign-key violations are zero
- token counts, elapsed time, lane agreement, manifest SHA, and logical SHA are
  recorded in the sidecar/report
- token and elapsed-time totals are reported separately for 1024 and 2048
- agreement separates truly different inputs from small-source assets whose
  two derivative pixels are identical
- a stopped run can resume without repeating a completed lane

N10 lane agreement is reported only as a stability diagnostic. It must not be
used to select a production resolution.

### Observed N10 result (2026-08-04)

- 10/10 fetches, 20/20 derivatives, and 20/20 strict Vision rows succeeded
- four model calls used 86,293 input and 2,145 output tokens in 66.5 seconds
- 1024 used 39,258 input / 1,014 output tokens in 31.8 seconds
- 2048 used 47,035 input / 1,131 output tokens in 34.6 seconds
- exact medium+view agreement was 10/10; seven pairs had different derivative
  pixels and three small sources produced identical derivative pixels
- a temporary contact-sheet review found all ten coarse labels plausible, with
  one sheltered-interior-to-courtyard image treated as a boundary case
- visible-material set agreement was 7/10 and visible-element set agreement
  was 6/10; both sets agreed on only 4/10
- one of the three pixel-identical pairs still changed an element label, so
  fine-label differences cannot be attributed to resolution without repeats
- all temporary image and contact-sheet files were deleted after review

The completed artifacts are
`data/smoke/divisare_vision_resolution_n10_v2.db` and
`data/reports/smoke/divisare_vision_resolution_n10_v2.md`.

## Cost gate before N100

The N10 report records Codex input, cached-input, and output tokens and projects
the N100 token count from measured tokens per asset. It also records model wall
time. Weekly ChatGPT/Codex quota is not equivalent to API dollars and may not
expose an exact remaining percentage, so any quota percentage must be labeled
as an estimate.

Before any N100 model call:

1. Present measured N10 tokens, calls, wall time, failures, and the N100
   projection.
2. State the estimated dollar or weekly-quota impact and its uncertainty.
3. Obtain explicit user approval if the projection exceeds about USD 5 or is a
   meaningful fraction of the available quota.

No full Vision run is authorized from an unmeasured N10.

The measured two-lane N100 projection is 862,930 input plus 21,450 output
tokens across 40 batched calls. At the current GPT-5.6 Sol API list prices this
is about USD 4.96, but the actual run uses ChatGPT Codex quota rather than API
billing. Exact weekly-limit percentage is unavailable; treat the projection as
roughly ten times the completed N10 and obtain explicit approval before launch.

## Phase 2: frozen reviewed N100

The dedicated `tools/run_divisare_vision_n100.py` runner accepts only the
validated, immutable 100-row gold manifest. It re-fetches the frozen max-2048
response, verifies the probe-time content SHA before model input, creates both
local derivatives from one decode, and runs 20 counterbalanced batches per
lane. The weak-prior N10 selector is not used.

Before enabling N100, freeze a manifest with a content SHA and these sampling
rules:

| Gold class | Count |
|---|---:|
| exterior | 20 |
| interior | 20 |
| drawing | 20 |
| aerial | 20 |
| detail | 20 |

The 100 items are raster images, with 80 clear and 20 boundary examples. Each
class should contain 16 modern Cloudinary and 4 legacy assets. Use at most one
image per article/building and remove exact or likely visual duplicates before
freezing. Divisare tags and hints may produce candidates, but human review and
adjudication determine the gold label. Gold labels and discovery hints must
not enter the model prompt.

The manifest, labels, exclusions, reviewer decision, and adjudication notes
must be frozen before either resolution is run. A failed frozen asset remains
a failure; it is not replaced after results are known.

### N100 quality gates

Report confusion matrices and precision/recall/F1 by class for each lane.
Clear and boundary examples must also be reported separately. The initial
acceptance gate for the 80 clear examples is:

- macro F1 at least 0.90
- recall at least 0.85 for every class

Select the smaller 1024 lane only if it is within 0.03 macro F1 of 2048 and has
no more than two additional errors. Otherwise retain 2048 or run a separately
approved crop/tile experiment. A frozen repeat subset should measure run-to-run
stability before the production profile is accepted. Counterbalance lane order
across batches so a fixed 1024-first/2048-second order cannot masquerade as a
resolution effect.

Fine material/element quality is a separate multilabel benchmark and must not
be inferred from these coarse-class gates.

### Observed N100 result (2026-08-05)

The reviewed pool contained 560 blinded pixel decisions and the final manifest
contained exactly 20 samples per class, 80 clear and 20 boundary, with the
required modern/legacy balance. The reviewer identifier is
`codex-5.6-sol-blinded-pixel-panel-20260805`; this is not independent-human
accuracy.

The run completed all 100 fetches, 200 derivatives, 40 Vision calls, and 200
results with no transport, SHA, schema, SQLite, or FK failure. It used 883,422
input and 21,447 output tokens in 660.6 seconds of model time. Temporary images
were deleted.

| Lane | Clear accuracy | Clear macro-F1 | Minimum class recall | Result |
|---|---:|---:|---:|---:|
| 1024 | 87.50% | 0.8795 | 0.6875 (`detail`) | FAIL |
| 2048 | 82.50% | 0.8264 | 0.5625 (`detail`) | FAIL |

The quality gate correctly selected no lane. Do not interpret 2048's lower
single-run score as a causal resolution result: the lanes are separate
nondeterministic calls, and their primary labels agreed on 92/100 images.

The audit exposed a projection bug (`photograph + elevation` becomes
`drawing`), missing class-boundary definitions in the prompt, and overconfident
agent-gold clear labels. Correcting only the elevation projection would raise
the 1024 clear macro-F1 to 0.9043, but detail recall would remain 0.6875 and the
gate would still fail. Treat this N100 as development/audit data. Prompt or
taxonomy revisions require a fresh, disjoint, independently human-adjudicated
holdout for the next final accuracy claim.

The pre-result same-batch N50 subset is frozen at
`data/review/divisare_vision_stability_n50_subset_v1.json`. Because no lane
passed, its model run was not launched. It remains usable only to measure the
unchanged v1 prompt's repeatability, not to evaluate a prompt designed after
seeing this N100.

## Sidecar and publication safety

The sidecar tables retain the run contract, sample, fetch evidence, derivative
metadata, Vision attempts, normalized results, optional gold labels, and
validation results. Final output includes a logical SHA in addition to file and
source provenance.

Outputs are immutable and no-clobber:

- existing final DB or report paths cause immediate refusal
- an interrupted DB remains as `<output>.partial` for explicit `--resume`
- a stale partial report causes refusal
- final DB and report are published as a pair using hard links; existing paths
  are never overwritten
- partial files are removed only after both final links succeed

The benchmark performs no Neon/R2 writes. Any later upload remains separately
user-gated.

## Relationship to pHash

This benchmark does not compute pHash. The v2.4 image-identity/hash stage owns
normalized identity, pixel SHA, and 256-bit pHash work. pHash is a useful
candidate filter for resized/cropped/compressed near-duplicates, not sole proof
that two images are the same.

The derivative SHA values stored here establish exactly which 1024/2048 bytes
and decoded pixels were shown in this resolution experiment. They must not be
mistaken for the cross-site duplicate pipeline.
