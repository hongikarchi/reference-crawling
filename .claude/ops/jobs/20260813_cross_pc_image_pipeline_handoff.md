# Cross-PC image-pipeline handoff

## Status

`LOCAL_PAYLOAD_COMPLETE_AND_VERIFIED; DROPBOX_SYNC_PENDING; GITHUB_PUSH_NOT_RUN`

The current Divisare + Architizer image-pipeline state was packaged for safe
continuation on another Windows PC. The source artifacts were copied, not moved
or modified. No crawl, Vision/model request, Neon/R2/vector operation, or DB
build was run.

## Git state at artifact freeze

- branch: `main`
- artifact baseline HEAD: `7e909764e1997ef73aa8c0839d1fde27f529ba36`
- locally observed `origin/main` before this handoff:
  `73f7c0b026738fdf7880ac4253bdce8bcf0b4c16`
- local commits ahead at the start: 3
- tracked/staged diff before handoff implementation: 0/0
- untracked files at the start were test-only residues:
  `.test-tmp/` 2,215 files and `.tmp-tests-v11/` 689 files
- no fetch or push was run

The final handoff implementation commit and Git bundle identity are recorded
below after publication. GitHub remains unchanged until the user explicitly
requests a push.

## Transfer scope

The payload is an explicit allowlist with 151 files and no directory-wide
copy. It includes:

- Divisare and Architizer E1 v1.2 immutable SQLite outputs;
- E2 image evidence full v5 and E3 image selection full v1;
- semantic Vision v1 N10, v1 N100, v1.1 N10, and v1.2 N10 sidecars;
- the three required one-byte persistent advisory-lock files;
- frozen selection manifests, compact reports, reference ledgers, and
  inspection reports;
- v1.1/v1.2 review caches and rendered blind-review sheets.

Payload total: `27,463,043,343 bytes` (`27.463 GB`, approximately `25.577 GiB`).

The two curated inputs are external requirements because exact copies already
exist and are in sync under the prior Dropbox migration bundle:

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `data/curated/divisare_metadata_v2_4.db` | 2,225,299,456 | `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f` |
| `data/curated/architizer_curated_v2_0.db` | 8,767,438,848 | `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc` |

## Dropbox destination

New versioned path; no prior handoff was overwritten:

```text
C:\Users\user\Dropbox\06_Archibe\reference-crawling-handoff\
  20260813-image-pipeline-handoff-v1\
```

The payload was copied under `payload/`. The first invocation was safely
interrupted when Dropbox temporarily held one completed JPEG partial open on
Windows (`WinError 32`). The copier was hardened to retry only Windows sharing
and lock violations (`32`/`33`) for a bounded interval. The second invocation
resumed the exact verified prefix and completed:

- first invocation: 16 files finalized before safe stop;
- second invocation: 135 copied, 16 already-equal skipped;
- resumed prefix: 160,928 bytes;
- final manifest verification: 151/151 passed;
- partial files after verification: 0.

Every immutable SQLite source and destination was rejected if a WAL, SHM, or
journal sidecar existed. None existed for the eight DBs in this payload.

## Safety and exclusions

The transfer excludes all `.env` files, cookies, session files, OAuth/API
credentials, global Codex config/auth, logs, rejected drafts, smoke databases,
WAL/SHM/journal files, partial outputs, virtual environments, and test temp
trees. Existing sensitive material observed in older Dropbox migration bundles
was not read or copied.

Dropbox is a transport only. The destination PC must hydrate and SHA-verify the
handoff, copy it to a local repository path, verify it again, and use exactly
one writable machine/output path at a time.

## Implementation

- `tools/build_current_image_handoff_manifest.py`
  - pinned primary identities;
  - explicit recursive review-material inventory;
  - per-file size/SHA-256;
  - secret/transient/link rejection;
  - no-clobber manifest publication.
- `tools/copy_cross_pc_handoff.py`
  - strict manifest allowlist;
  - path/link/junction escape rejection;
  - no-clobber final files;
  - verified `.handoff-partial` resume;
  - fsync, full SHA, and exclusive atomic promotion;
  - bounded Dropbox/Windows sharing-lock retry;
  - immutable SQLite sidecar gate and verify-only mode.
- `docs/CROSS_PC_HANDOFF.md`
  - source and destination procedures;
  - credential exclusions;
  - single-writer boundary.
- `.gitignore`
  - ignores only local pytest/test-temporary directories.

## Validation

- focused handoff tests after integration: `22 passed, 1 skipped, 3 subtests`
- Windows promotion-retry focused tests: `20 passed, 1 skipped`
- full repository suite before the final small retry patch:
  `1,137 passed, 23 skipped, 1,456 subtests passed`
- final full repository suite:
  `1,140 passed, 23 skipped, 1,456 subtests passed` in 256.89 seconds
- `py_compile`: passed
- `git diff --check`: passed
- destination payload verify-only: `151/151 passed`

The one focused skip is the symlink/junction construction test on a Windows
account without symlink creation privilege. The rejection code path remains
covered where the platform permits constructing the fixture.

## Continuation state

E1, E2, and E3 are complete. Semantic Vision v1.2 N10 is immutable
`complete_with_failures`, with 45/50 semantic results, and its final QA verdict
is `FAIL`. The destination PC must not proceed directly to N100 or Full. The
next paid step requires a new prompt/runtime decision and another fresh,
prior-disjoint N10 with explicit cost approval.

## Final publication identity

The exact handoff implementation commit, Git bundle filename/SHA-256, handoff
manifest SHA-256, READY marker identity, and final Dropbox Cloud Files sync
observation are recorded in the external `HANDOFF_READY.json`. This avoids a
self-referential tracked document whose own commit or bundle hash could not be
embedded in the same immutable object.

GitHub push was not authorized and was not run. The Git bundle is therefore the
authoritative cross-PC code transport until the user explicitly requests a
push.
