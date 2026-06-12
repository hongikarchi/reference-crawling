# 2026-05-31 — cover/image duplicate manual review app

## Scope

Replace the static `cover_phash_review.html` + manual CSV workflow with a local
click-based review app for ambiguous image duplicate cases.

The app captures decisions only. It does not write Neon, R2, or canonical data.

## Inputs

- Existing audit verdict: `data/reports/audit_2026-05-27/verdict.md`
- Existing static review HTML/CSV were treated as stale reference only.
- Source issue CSV under `/private/tmp/make_web_db_audit/...` was missing, so
  the new snapshot is regenerated from current Neon `canonical_v2_buildings`
  using read-only `SELECT`.

## Outputs

- Tool: `tools/cover_review_app.py`
- Snapshot: `data/reports/audit_2026-05-27/cover_review_snapshot.json`
- Decisions: `data/reports/audit_2026-05-27/cover_review_decisions.json`

## Behavior

- Serves a stdlib-only localhost app.
- Review unit is one flagged target case, not a phash group.
- Evidence rows/images are context only.
- Actions: `keep`, `set_cover_to_image`, `unpublish`, `merge`, `unsure`.
- `set_cover_to_image` validates that the selected URL belongs to the target
  row's `all_images`.

## Result

Snapshot generated from current Neon:

```text
total_cases: 42
COVER_PHASH_SHARED_ACROSS_BUILDINGS: 40
GALLERY_IMAGE_SHARED_ACROSS_BUILDINGS: 2
```

This differs from the old 29-cover-row audit count because the original audit
CSV is unavailable and the new app rebuilds cases from current DB truth. The
old static CSV mixed decision targets and sibling evidence rows.

Validation:

```text
python3 -B tools/cover_review_app.py --check
status: PASS
failure_count: 0
```

Local server started at:

```text
http://127.0.0.1:8765/
```
