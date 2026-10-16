# Divisare metadata review v2.3

## Purpose

`divisare_metadata_v2_3.db` is the final Divisare-only SQLite artifact for
non-image-semantic metadata review. It copies the immutable v2.2 artifact and
applies three versioned, hash-guarded review ledgers:

- partial project text
- ambiguous Built Surface values
- Divisare-internal D2 identity candidates

This stage does not download images, compute pHash, classify image content,
generate vectors, compare other sites, or write Neon/R2.

## Immutable inputs

| Input | SHA-256 |
|---|---|
| `data/curated/divisare_metadata_v2_2.db` | `ee7bcd55fedf38fe8cb9a49f51e8f12f69493aef68ff1d201d2fa1e5be8ec95c` |
| `canonical/divisare_partial_text_decisions_v2.json` | `de7e965e5a8127706fe3fba0067644fc096c2662b5319d86b5df2dfead5ae723` |
| `canonical/divisare_area_decisions_v1.json` | `a8c73d0f83cc1c6fcd64782b74c579ff600cf1a541e03027341f4add650c7061` |
| `canonical/divisare_d2_decisions_v1.json` | `dcc33813a31d8e0e1a3d452798cee15139180519f35b130346848ee2550f86a0` |

The builder opens the parent read-only for validation, copies it to a staging
file, applies the overlay, and publishes only after all checks pass. Inputs are
re-hashed after the run.

## Review results

### Partial text

The exact 21-row partial-text snapshot is closed:

- 9 accepted project descriptions
- 12 rejected boilerplate or non-project descriptions
- 0 remaining review rows

Articles `261731` and `261740` were the two v2.2 review rows. Both are rejected
as shared exhibition-section boilerplate. The other 19 decisions are
byte-equivalent to v1.

### Area

All 125 v2.2 area-review rows were examined individually:

| Decision | Rows | Generic `area_sqm` effect |
|---|---:|---|
| Accept generic project/building area | 10 | Published as `area_sqm` |
| Keep scope-qualified candidate | 15 | Candidate only; never coalesced into `area_sqm` |
| Keep null because values conflict or are multi-component | 21 | Null |
| Reject non-area value | 79 | Null |

123 decisions are final. Two remain deliberately open with no numeric value:

- `14079` Baiyun International Convention Center: Divisare `300,000`, project
  prose `210,000`, and current architect record `272,000 sqm` conflict.
- `465382` Cordemais cultural center: Divisare `1,702` conflicts with source
  values `1,532`, `1,534`, and `1,562 sqm`.

Article `16857` CCTV remains a `575,000 sqm` scope-qualified development
program candidate because the figure covers the headquarters, TVCC, service
building, and parking rather than one generic headquarters area.

Numeric area is never inferred from photographs or drawings.

### D2 identity

All 220 previously pending/deferred D2 pairs have approved decisions:

- 8 merge
- 128 reject as separate projects/interventions
- 84 approved abstentions (`defer`)

Together with the 66 inherited strict confirmations, the output review table
contains 74 confirmed, 128 rejected, and 84 deferred pairs. The eight merges
create eight terminal redirects and reduce active buildings from 29,891 to
29,883. A further 117 reject decisions retain supported related-project edges.

See `docs/DIVISARE_D2_REVIEW_STATUS.md` for the identity gate, merge list, and
regression cases.

## Output

- DB: `data/curated/divisare_metadata_v2_3.db`
- report: `data/reports/divisare_metadata_v2_3.md`
- `PRAGMA user_version = 6`
- size: `2,115,321,856 bytes`
- byte SHA-256:
  `7c263f430709dbfe4a2747407d736268331976cd77f66fe25d7037b77fd68038`
- logical SHA-256:
  `ce55d8c9d91005b0554ba9cb483661613d66c7e936e3edfb54bc0adf2c50e557`
- build time: `102.38 seconds`

Key populations:

- 29,955 article resolutions and memberships
- 29,883 active building rows
- 1,429 articles with generic `area_sqm`
- 93,414 confirmed and 28,284 candidate building facets
- 547,222 materialized building-image asset links, URL/asset data only
- 8 redirects and 117 related-project edges

The consumer view is `v_divisare_buildings_export_v2_3`.

## Added v2.3 surfaces

- `metadata_review_lineage_v2_3`
- `article_partial_text_decisions_v2_3`
- `article_area_decisions_v2_3`
- `article_d2_decisions_v2_3`
- `article_metadata_resolution_v2_3`
- `article_match_reviews_v2_3`
- `building_redirects_v2_3`
- `active_building_membership_v2_3`
- `building_facets_v2_3` and `building_facet_claims_v2_3`
- `building_images_materialized_v2_3`
- `building_core_reconciled_v2_3`
- `building_article_roles_v2_3`
- `building_related_projects_v2_3`
- `metadata_review_metrics_v2_3`
- `metadata_review_validation_v2_3`

All v2.2 user tables and schema objects remain unchanged in the copied
artifact. D2-affected building-level facets, image memberships, core fields,
primary article, and article roles are rebuilt from the final membership.

## Verification

- N=10 fixture: two full-path builds, byte/logical determinism PASS
- N=100 fixture: two full-path builds, byte/logical determinism PASS
- fixture no-clobber and input/manifest immutability PASS
- transitive component reject/defer conflict gate PASS
- production `--validate-only` PASS
- full artifact validations: 86 passed / 0 failed
- SQLite `quick_check`: `ok`
- foreign-key violations: 0
- Divisare regression suite: 138 passed

External API, LLM, Vision, Neon, and R2 cost: `$0`.

## Remaining work

Image work is intentionally separate. `image_hashes` contains 547,222 pending
`phash-256` task rows, but `hash_hex` and `computed_at` are null for every row.
Image classifications and image-match candidates are both empty.

The later image stage should download or recover assets, compute validated
pHash values, measure false-positive/false-negative behavior, and use image
evidence only as support for deferred identity decisions and cross-site
deduplication. It must not overwrite this metadata artifact in place.
