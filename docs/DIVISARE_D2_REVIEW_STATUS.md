# Divisare D2 duplicate review status

## Current state

The immutable metadata v2.1 artifact contains 286 Divisare-internal duplicate
candidate pairs:

- 66 `auto_clustered` pairs remain confirmed under the strict v1.5 rule.
- 220 pairs remain pending.
- No new manual merge was approved, so v2.1 created no redirect.

The 220 pending pairs were re-audited using names, architects, location, year,
tags, cleaned text, and article-kind state. Images and pHash were not used.

| Candidate kind | Pairs | Score range |
|---|---:|---:|
| `exact_name_location_review` | 181 | `0.72-0.90` |
| `fuzzy_name_same_architect_country` | 39 | `0.9150-0.9451` |

Within the 181 exact-name/location candidates:

- 125 have disjoint architect sets.
- 50 have the same architect set.
- 6 have a partial architect overlap.
- 81 have the same recorded year.
- 100 have a different or missing year.

## Why threshold automation is unsafe

No score, tag, architect, or text-similarity threshold is reliable enough to
resolve the pending set automatically.

- High fuzzy scores include separate phases or siblings such as FASE I/III,
  Residence A/B, and Vallecas 11/51.
- Tag Jaccard at least `0.75` occurs in 31 pairs, but includes separate works
  from the same series.
- Text-token Jaccard at least `0.75` occurs in 28 pairs, but repeated editorial
  and credit text inflates similarity.
- Only two pairs have identical cleaned-text checksums; both are separate
  `Structures of Landscape` works with reused photography credit text.
- Of the 304 articles involved in pending pairs, 303 have cleaned text, but it
  is still marked `ui_removed_caption_residue_possible`.
- 133 of 220 pairs have unresolved article kind on both sides. The remainder
  have only candidate or ambiguous kind; none has confirmed kind evidence.

Therefore the 220 rows remain pending. This is an intentional abstention, not a
failed duplicate-detection run.

## First metadata-only review batch

The pending graph has 134 connected components:

| Component size | Components |
|---:|---:|
| 2 articles | 110 |
| 3 articles | 18 |
| 4 articles | 4 |
| 7 articles | 2 |

Six event or series components are suitable for the first human reject review:

| Component | Articles | Candidate pairs |
|---|---:|---:|
| BUS:STOP Krumbach | 7 | 21 |
| Vatican Chapel | 7 | 21 |
| XXI Triennale di Milano. Pavilion | 4 | 6 |
| Summer House | 4 | 6 |
| The Snow Show | 4 | 6 |
| Serralves Pavilion | 4 | 6 |
| Total | 30 | 66 |

These 66 pairs are strong reject-review candidates because the shared
event/series title groups distinct architects' designs. They must still be
confirmed component by component. A global `different architect = reject`
rule is unsafe because one building can credit different architects or roles.

## Strong pair-level merge review

The following pairs have strong non-image evidence, but still require an
explicit reviewer decision:

- `96467 / 343892`: Centro Galego/Gallego de Arte Contemporanea
- `268679 / 383882`: Long Museum West Bund
- `346047 / 396651`: Rolex Learning Centre/Center
- `112411 / 343271`: Sihlholzli spelling variation and substantially matching
  text
- `348479 / 348989`: Murphy's House/Murphy House at the same Hart Street site
- `317455 / 328691`: TER, reordered architect attribution and substantially
  matching text

These examples do not define a safe general-purpose merge rule.

## Decision workflow

1. Review the six event/series components and record pair-level reject
   decisions with reviewer, timestamp, and reason.
2. Review strong merge candidates individually against both source URLs and
   current HTML prose.
3. Keep all other pairs pending until the full HTML recrawl adds cleaner
   location, year, area, and description evidence.
4. Use pHash and image comparison later as supporting evidence, never as the
   sole building-identity key.
5. Build a new immutable metadata artifact only from an explicitly approved,
   versioned decision file.

The current builder accepts decisions only for the existing 286 D2 candidates.
New pairs discovered later by pHash or cross-site work require an explicit
input-contract extension. A rejected v1.5 auto cluster also requires a future
split policy or a v1 rebuild.
