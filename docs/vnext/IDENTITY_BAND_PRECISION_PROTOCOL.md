# Identity band-precision review protocol v1

This overlay extends `CONTRACT_IDENTITY.md` section 8 and
`IDENTITY_N100_PROTOCOL.md` without changing either pinned input.

- The immutable v4 candidate DB is the measurement source.  All rows carrying
  `fanout_review_required`, `phash_veto_review_required`, or `anchor_missing`
  are excluded before sampling and remain direct-review rows.
- The fixed seed is `vnext-wp25-band-precision-v1-20260826`.  Rank is
  `SHA256(sample_version + NUL + seed + NUL + stratum + NUL + candidate_id)`,
  with `candidate_id` as the tie break.  S1/S2/S3/S4 contain 60/60/40/25
  seed-ranked rows; S5 is the complete 36-row census.  Every selected row is
  disjoint from the 135 previously reviewed focal pairs.  The 221 selected
  candidate IDs must also be globally unique.  No post-freeze replacement is
  permitted.
- S4's 119 rows are its raw v3-to-v4 transition universe.  The global flag
  policy is applied before its sample is drawn; raw, flagged, prior-reviewed,
  and final-frame counts are reported separately.  S3 retains its literal
  `anchor_2` plus `no_anchor_2plus` frame; it is not silently redefined.
- Two isolated reviewers receive independently shuffled packets hiding the
  candidate ID, proposal, score, policy flags, band, rank, and stratum.  Both
  complete ledgers are locked before any unblind artifact is produced.  A
  third actor adjudicates all rows.  Gold labels are `same`, `related`,
  `different`, and `insufficient`; an unresolved disagreement is forced to
  `insufficient`.
- At most two assets per side are frozen.  The global maximum is 884 asset
  occurrences, logical HTTP attempts, and wire requests.  Redirects and
  retries are disabled.  WP1, WP2, WP2.2, and WP2.3 caches are SHA-pinned
  read-only inputs; only the WP2.5 cache may receive current-pixel derivatives.
  Every logical attempt is recorded.  Pixel drift is shown to reviewers and
  reported, but is not an exclusion reason.
- S1--S4 precision is `same / n`; the two-sided 95% interval is
  Clopper--Pearson.  `error_count = related + different + insufficient` and
  `equivalent_errors_60 = error_count * 60 / n`.  The pre-declared candidate
  grade is `bulk_candidate` when the equivalent count is at most 1,
  `agent_first_pass` when it is greater than 1 but less than 5, and
  `direct_review` when it is at least 5.  Any `different` forces
  `bulk_excluded=true` regardless of the numerical grade.  These formulas are
  immutable after sample freeze.
- If S5 contains one or more final `same` labels, the R1 six-year rule and the
  WP2.5 run fail and any S1 start is held.  Otherwise the run may pass, but all
  grades remain measurement-only recommendations requiring a later user
  decision.  This run never merges rows, issues IDs, or writes Neon/R2.
- Weekly quota is recorded before freeze.  A new review wave may not start at
  30% or less remaining; all review activity stops at 20% or less remaining.
