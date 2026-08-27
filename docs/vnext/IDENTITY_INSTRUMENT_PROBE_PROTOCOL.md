# Identity instrument calibration probe protocol v1

This measurement checks whether the unchanged WP2.5 review instrument can
recover known non-`same` controls without over-correcting known `same` rows.

- Sample version: `vnext-identity-instrument-probe-v1`.
- Fixed seed: `vnext-wp26-instrument-probe-v1-20260827`.
- Rank: `SHA256(sample_version + NUL + seed + NUL + group + NUL + candidate_id)`,
  with `candidate_id` as the tie break.
- P1 is the complete set of three current-v4-unflagged, non-`same` A70 rows:
  two frozen N100 `insufficient` and one `related`.
- P2 is the complete four-row N100 B3 fanout control: three `insufficient`
  and one `related`.
- P3 is the complete two-row frozen N100 `B2_phash_veto`/`insufficient`
  control.  Membership comes from the frozen N100 stratum, not current v4
  flags.
- D is the first 15 seed-ranked rows from all 66 frozen N100 A70 `same` rows.
  The order does not add a current-flag filter to D.
- All 24 candidate IDs and N100 sample IDs are unique and must have zero
  intersection with the frozen WP2.5 sample of 221 rows.  No post-freeze
  replacement is permitted.

Step 0 is frozen before the sample.  N100 and WP2.5 use the same JSON form
keys and one free-text `rationale`; neither has separate axis columns.  The
operational difference is that every N100 rationale used the ordered tags
`image/name/firm/place/year/scope/basis`, while no WP2.5 rationale used any of
them.  The probe does not restore those tags because it is measuring the
unchanged WP2.5 instrument.

The WP2.5 packet/form builder, renderer, per-side asset cap of two, four-label
vocabulary, and two-lane lock-before-unblind procedure are reused from the
SHA-pinned producer.  Reviewer forms and sheets hide group, frozen gold,
candidate ID, proposal, score, flags, and sample rank.  Reviewers receive no
N100 or WP2.5 result artifact.

Both reviewer ledgers are locked before the adjudication packet exists.  The
third actor adjudicates all 24 rows from the same rendered evidence and the
two locked reviews; its packet and form still hide group, frozen N100 gold,
candidate ID, proposal, score, and flags.  Frozen N100 gold is opened only
after the completed adjudication has been validated, committed, published as
an immutable locked ledger, and hash-bound in the sidecar.

The pre-declared gate is immutable after sample freeze and passes only when
all three predicates hold:

1. P1 non-`same` detections are at least 2 of 3.
2. P2+P3 non-`same` detections are at least 3 of 6.
3. D `same` detections are at least 13 of 15.

Failure of any predicate makes the probe `FAIL` and invalidates the WP2.5
S1--S4 grades.  Passing only validates those grades for later human tiering;
it does not authorize or execute tier promotion, merge, ID issuance, Neon, or
R2 writes.

At most 96 asset occurrences, logical HTTP attempts, and wire requests are
allowed.  Redirects and retries are zero.  Existing pinned caches are
read-only and only the probe cache is writable.  Vision/LLM and paid API calls
are zero.  A review wave cannot start at 30% or less weekly quota remaining,
and all activity stops at 20% or less.
