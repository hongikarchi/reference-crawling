# Divisare N100 human review

`tools/divisare_n100_review.py` is a local, pixel-first review tool for the
Divisare Vision candidate pool. It does not download, proxy, cache, or persist
image bytes. The browser renders the manifest's HTTPS `request_url` directly.

## Input contract

The candidate manifest is immutable JSON with these required top-level fields:

- `manifest_version` = `divisare-vision-gold-candidates-v1.0.0`
- `source_db_sha256`
- `manifest_sha256`
- non-empty `contract` object
- non-empty `items` or `candidates`

Each item requires an opaque `candidate_id` such as `candidate-0001` and
unique `candidate_id`, `asset_key`, `article_id`, and
`building_id` values and an HTTPS `request_url`. Delivery and discovery
metadata may be included. The first view shows only the opaque candidate ID
and rank. Asset/article/building IDs, role, generation group, `weak_hints`, and
legacy discovery fields are isolated behind a collapsed audit control so the
first judgment is pixels-only. This matters because a legacy `asset_key` may
itself contain a descriptive original filename.

`manifest_sha256` is lowercase SHA-256 of canonical JSON for the complete root
object after removing only the root `manifest_sha256` field. Canonical JSON
uses UTF-8, sorted object keys, no whitespace, and no NaN values. Any edit to
an item, URL, source SHA, or selection contract invalidates the manifest.

## Review

Run from the repository root:

```powershell
.venv-images\Scripts\python.exe tools\divisare_n100_review.py `
  --manifest data\review\divisare_vision_n100_candidate_pool_v1.json `
  --draft data\review\divisare_vision_n100_review_draft_v1.json `
  --reviewer local-human `
  serve --port 8768
```

Open `http://127.0.0.1:8768`. The draft is updated atomically and may be
resumed only with the exact same manifest SHA.

Review fields are:

- primary `gold_label`: exterior, interior, drawing, aerial, or detail
- `clarity`: clear or boundary
- `acceptable_labels`: exactly the primary label for clear items; at least two
  labels including the primary label for boundary items
- exclude, for unusable or duplicate candidates
- reviewer notes

Apply the five classes in this precedence order:

1. Any architectural plan, section, elevation, site plan, or diagram is
   `drawing`, even when its subject is a detail or aerial view.
2. A high or top-down view of a whole building or site is `aerial`.
3. A tight component, material, or joint crop is `detail`.
4. An enclosed or mostly enclosed space is `interior`.
5. An ordinary outside building view is `exterior`.

Renderings, physical models, mixed composites, portraits, construction-only
images, object-only images, and non-architecture should normally be excluded
instead of forced into a class. Courtyards, atria, facade crops, and oblique
rooftop views may be marked boundary after assigning a primary class.

Keyboard controls are `1`-`5` for labels, `B` for boundary, `X` for exclude,
left/right arrows for navigation, and `Ctrl+Enter` to save and continue.
Export and import are also available in the browser. Imports must carry the
same manifest SHA and a valid `reviewed_pool_sha256`; conflicting existing
decisions are rejected rather than overwritten.

## Immutable export

Check progress without writing a final artifact:

```powershell
.venv-images\Scripts\python.exe tools\divisare_n100_review.py `
  --manifest data\review\divisare_vision_n100_candidate_pool_v1.json `
  --draft data\review\divisare_vision_n100_review_draft_v1.json `
  status
```

Publish the reviewed decisions to a fresh path:

```powershell
.venv-images\Scripts\python.exe tools\divisare_n100_review.py `
  --manifest data\review\divisare_vision_n100_candidate_pool_v1.json `
  --draft data\review\divisare_vision_n100_review_draft_v1.json `
  --reviewer local-human `
  export --output data\review\divisare_vision_reviewed_pool_v1.json
```

The CLI refuses an immutable export until every candidate has a decision. The
browser export remains available as a manifest-bound progress backup that can
be imported later.

The output version is `divisare-vision-reviewed-pool-v1.0.0`. Export is strict
no-clobber and contains `candidate_manifest_sha256`, source SHA, reviewer,
ordered decisions, completion counts, and a self-verifying
`reviewed_pool_sha256`. It contains no weak hints, discovery scores, project
names, or source text. This artifact is not the final gold N100. A separate
deterministic finalizer selects the balanced 20-per-class N100 and publishes
`divisare-vision-gold-manifest-v1.0.0` with `gold_manifest_sha256`.
