# Identity N100 review protocol v1

This overlay extends `CONTRACT_IDENTITY.md` section 8 without changing the
WP2.2-pinned contract bytes.

- The frozen sample contains 110 pairs: precision core A70 and diagnostic
  B1/B2/B3/B4 with 10 pairs each.
- A70 is drawn from v3 `merge_candidate` rows with
  `auto_merge_eligible=1`, after excluding every prior focal review.  Allocation
  is hierarchical Hamilton apportionment: anchor type first
  (`name`, `image`, `both`), then the positive auxiliary-family mask.
- B40 contains five explicitly named WP2.2 carry-forward sentinels and 35
  fresh, seed-ranked pairs.  Fresh pairs exclude every prior focal review;
  carry-forward and fresh diagnostic rates are reported separately.
  B1 isolates the exact architect/year-gate conflict with at least two positive
  families and a name-or-image anchor.  B2 isolates the exact pHash veto with
  at least two positives plus a strict name anchor (`name_pos` and score >=90).
  B3 requires both the fan-out flag and exact fan-out conflict on a defer row;
  B4 is a strict-name reject after excluding the complete B1/B2/B3 pools.
- Two isolated reviewers receive independently shuffled, proposal/score/rank/
  cohort/stratum/candidate-ID-blind packets for the 105 fresh rows.  Each
  105-row ledger is locked before either ledger can be unblinded.  A distinct
  adjudicator records final gold for all 105 fresh rows; unresolved
  disagreements become `insufficient`.  The five carry-forward rows inherit
  immutable WP2.2 v3 gold with its run, opaque-ID, reviewer/adjudicator, mapped
  decision, record-hash, and source-DB provenance; they are never placed on a
  new reviewer sheet.
- `unreviewable=0` means no missing sheet, reviewer row, or adjudication row.
  A valid final `insufficient` label is not unreviewable.
- Core A fails on one final `different`, one sampled hard-conflict violation,
  or any unreviewable row.  B never changes the gate.
- Exact-identity precision is `same / 70` with a two-sided 95% Clopper-Pearson
  interval.  Even 70/70 does not establish 99% precision, so this run cannot
  authorize automatic merge or remove the existing human-approval rule.
- At most two assets per side are frozen for each of the 105 fresh rows.
  Global asset deduplication gives at most 420 logical HTTP attempts; redirects
  and retries are disabled, so wire requests are also at most 420 and always
  below the hard cap of 500.
- WP1/WP2 caches are SHA-pinned read-only inputs.  Only
  `vnext_identity_n100_cache_v1` may receive new derivatives.
- The v3 DB is opened immutable and is never changed.  Sample, fetch, review,
  unblind, adjudication, and report state lives only in the no-clobber N100
  sidecar and its bound artifacts.
