# Identity instrument v2 contrast protocol

This protocol changes only the review instrument.  It reuses the frozen WP2.6
24-row sample and visual evidence so that v1 and v2 are measured on identical
inputs.  It performs no merge, ID issuance, tier promotion, Neon/R2 write,
Vision/LLM API call, or paid API call.

## Stage order and measurement boundary

1. Stage A freezes this instrument contract and its byte hash.
2. Stage B freezes the exact WP2.6 sample and 48 evidence-sheet bindings.  It
   does not start a review and issues no HTTP request.
3. Stage C freezes the four gate predicates.  Reviewer forms may be handed off
   only after this event.
4. Two isolated reviewers record observations, then a third isolated actor
   adjudicates observations.  N100 gold and the WP2.6 v1 label remain hidden
   until all three ledgers are immutable.
5. The producer reveals the controls, derives labels, scores the frozen gate,
   and stops at terminal `PASS` or `FAIL`.

The persisted state sequence is
`instrument_frozen -> sample_frozen -> criteria_frozen -> review_ready ->
reviews_partial -> reviews_locked -> adjudication_locked -> pass|fail`.
The criteria event is therefore after input identity is known but before the
first reviewer can see a form.

## Observation contract

Every review and adjudication row contains exactly six controlled observations
and one mandatory free-text basis:

- `image`: `same_object`, `different_objects`, `inconclusive`
- `name`: `agree`, `alias`, `conflict`, `unclear`
- `firm`: `agree`, `overlap`, `conflict`, `unknown`
- `place`: `agree`, `unclear`, `conflict`
- `year`: `compatible`, `conflict`, `unclear`
- `scope`: `same`, `component_vs_umbrella`, `phase_or_extension`, `unclear`,
  `unrelated`
- `basis`: trimmed non-empty free text describing the visible evidence

No synonym, missing value, null, case variant, or extra observation key is
accepted.  `label`, `allowed_labels`, `final_label`, `adjudicator_label`,
`rationale`, and `hard_conflict_observed` are forbidden input fields.

`image=inconclusive` is required when one side contains only drawings or
diagrams, a publication page or cover, or no image that permits comparison of
the building form.  Inability to compare is a normal and desirable observation,
not a failed review.

Two scope instructions are mandatory:

- Matching photographs prove a shared depicted object, not equal project
  scope.
- If one name denotes a village, campus, complex, masterplan, park, festival,
  or another umbrella while the other denotes one constituent object, use
  `scope=component_vs_umbrella`.

## Deterministic derivation

The first matching rule wins:

1. `image=inconclusive` -> `insufficient`
2. `image=different_objects` or `scope=unrelated` -> `different`
3. `scope` is `component_vs_umbrella` or `phase_or_extension` -> `related`
4. `image=same_object` and `scope=same` -> `same`
5. otherwise -> `insufficient`

`name`, `firm`, `place`, and `year` are mandatory audit observations but do not
change this v2 label.  Reviewers and the adjudicator never choose a label.  The
adjudicator resolves the six controlled observations and basis; the label and
rule number are calculated only from the locked final observations.

## Identical-input contract

- Source sample version: `vnext-identity-instrument-probe-v1`
- Source seed: `vnext-wp26-instrument-probe-v1-20260827`
- Source logical SHA:
  `784a567458227a737c78b31320eb79fdfbca0e7cf5909d08bb87c38f12e2b208`
- Source rows: 24, with the original sample ID, rank, seed rank, and control
  commitment unchanged.
- Source visual evidence: WP2.6's 48 pinned PNG sheets and 93-asset lineage.
  WP2.7 incremental asset fetches, HTTP attempts, retries, redirects, cache
  writes, and renderer substitutions are all zero.
- The authoritative packet binding is the 48-row ordered tuple
  `(lane, display_order, opaque_review_id, sample_id, sheet_path,
  sheet_sha256, sheet_bytes)` replayed from the terminal WP2.6 database.  Its
  canonical compact, sorted-key JSON SHA-256 is
  `b1f0491852d68648405c032e1a8486ed41a939b18eb655cea1aab92ab07b39e5`.
  The separate visual-file inventory SHA-256 is
  `0cdf2b6b42b9a4866a2e9ee0cb37e5efb9c045915ce83e67d350569749bbe99e`.
  A reordered or remapped packet fails even when the same PNG set remains.

The reviewer receives only its v2 form, the referenced frozen sheets, and this
instrument guidance.  It receives no WP2.6 completed/locked review, terminal
report, N100 artifact, group, gold, score, flag, or v1 label.

The adjudicator receives only an axis-only v2 form, the two immutable reviewer
axis ledgers, their bound sheets, and this guidance.  It receives no candidate
or group identifier, proposal, gold or v1 label, score, flag, N100 artifact, or
other WP2.6 review/report artifact.  Pre-reveal artifacts may contain only
commitments to hidden controls, never their plaintext.  Reveal may start only
from a committed `adjudication_locked` event and records its own timestamp and
payload hash.

## Predeclared gate

All predicates must pass:

1. At least 13 of the 15 frozen D rows derive `same`.
2. At least 2 of the 3 frozen P1 rows derive a non-`same` label.
3. At least 4 of the 7 rows whose frozen N100 gold is `insufficient` derive
   exactly `insufficient`.
4. At least 1 of the 2 rows whose frozen N100 gold is `related` derives exactly
   `related`.

Failure of one predicate makes the run terminal `FAIL`; no automatic redesign
or follow-up measurement is allowed.  Passing validates only this instrument
contrast and does not authorize WP2.8, tiering, or a merge by itself.

Here non-`same` means exactly one of `related`, `different`, or `insufficient`.

## Required artifacts and terminal report

The run publishes the no-clobber
`vnext_identity_instrument_v2_probe_v1` SQLite database, sample/fetch/report/
terminal manifests, label-free review and adjudication forms and locks, this
protocol, the job card, and an independent validation result.  The independent
validator must not import the producer module.

The terminal report contains all 24 rows and, for each row, the N100 gold
label, WP2.6 v1 probe label, v2 derived label, six final controlled axes,
mandatory basis, applied derivation rule, and the R1/R2/final per-axis
adjustment audit.  It also contains the four gate numerators, denominators,
thresholds and booleans; an independent axes-to-label recomputation audit;
quota observations; the inherited WP2.6 source fetch counts (including 17
failed fetches and its correction overlay); and separate WP2.7 incremental
HTTP, wire, retry, redirect, renderer substitution, and cache-write counts,
all zero.  Manifest and report record zero merge executions, ID issuance,
tier promotions, Neon/R2 writes, Vision/LLM requests, and paid API requests.

## Operational limits

Weekly quota remaining must be greater than 30% before each new review wave.
An observation at or below 20% creates a sticky stop after which no additional
event or artifact may be produced.  The frozen evidence has at most two assets
per side and an inherited 96-occurrence ceiling, but WP2.7 itself must record
zero HTTP and zero cache writes.  All external JSON is no-clobber and
DB-first recoverable; existing non-identical bytes always fail closed.  The
same no-clobber and byte-identical DB-first recovery contract covers the SQLite
database, PNGs, form copies, manifests, reports, and every other run artifact.
