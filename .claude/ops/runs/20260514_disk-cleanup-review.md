# Disk Cleanup Review

created: 2026-05-14 KST
status: review-only

## Current Disk

- filesystem free before resume7: about 4.5GiB
- dominant repo usage: `data/canonical` about 13GiB
- code files are not the disk problem; large generated JSON/DB artifacts are.

## Keep Until Final QC

These are active rebuild inputs or current partial outputs.

- `data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl` (~668MiB)
  - active E-1 image cluster input for strict rebuild.
- `data/canonical/country_conflict_refresh/d1_results.patched.jsonl` (~23MiB)
  - active D-1 input.
- `data/canonical/country_conflict_refresh/e2_image_types.patched.jsonl` (~19MiB)
  - active E-2 input.
- `data/canonical/country_conflict_refresh/d2_results.patched.resume7_partial.jsonl` (~22MiB)
  - latest cumulative D-2 partial after resume7.
- `data/canonical/country_conflict_refresh/d2_results.image_backfill.*.jsonl`
  - raw D-2 evidence until final combined D-2 and QC reports are complete.
- `data/canonical/canonical_buildings_4source.json`
  - canonical source cluster input.
- `data/id_registry_*.json`
  - never delete.

## Delete Candidates After Final Artifact Exists

These are superseded strict/embedded snapshots. They are expensive and can be
recreated from JSONL stage inputs, but should be deleted only after final strict,
embedded, upload validator, and integrity audit pass.

- `canonical_buildings_strict.patched.json` (~1005MiB)
  - pre-D-2-image-backfill strict snapshot; superseded by quota/resume partials.
- `canonical_buildings_strict_embedded.patched.json` (~1064MiB)
  - embedded version of the same superseded snapshot.
- `canonical_buildings_strict.quota_stop.json` (~1009MiB)
  - partial snapshot at 7,979 D-2 rows; superseded by resume6/resume7 partials.
- `canonical_buildings_strict_embedded.quota_stop.json` (~1068MiB)
  - embedded version of quota-stop snapshot.
- `canonical_buildings_strict.resume6_partial.json` (~1010MiB)
  - partial snapshot at 9,748 D-2 rows; superseded by resume7 partial D-2 JSONL.
- `canonical_buildings_strict.resume6_partial.publishability.json` (0B)
  - failed ENOSPC output; safe to delete.

Estimated reclaim after final QC: about 5.1GiB.

## Earlier Cleanup If Space Is Needed

These can be removed before final only with explicit approval, because they are
rollback/debug artifacts rather than active inputs.

- `data/backups/d2_resume5_sandbox_dns_20260513_2148/` (~228MiB)
  - false DNS-failure backup from sandbox-network issue; not real image failure.
- repeated `data/backups/divisare_*/*divisare.db` copies (~928MiB total)
  - source DB backups from architect/refetch repair. Keep if rollback is needed;
    otherwise archive/delete after current source DB is verified.

## Code Cleanup Notes

No code file is large enough to matter for disk. Do not delete code for space.

Stale default paths should be updated only after final artifact naming is settled:

- `tools/audit_canonical_data_integrity.py`
  - defaults still point to `canonical_buildings_strict.patched.json`.
- `tools/backfill_architizer_architect_registry.py`
  - default strict path still points to `canonical_buildings_strict.patched.json`.
- `tools/canonical_v2_embed_refresh.py`
  - default output still uses generic `canonical_buildings_strict_embedded.patched.json`.

Recommendation: after final D-2 completes, create stable final names and update
script defaults to those final names. Do not update defaults to partial/resume
artifacts.
