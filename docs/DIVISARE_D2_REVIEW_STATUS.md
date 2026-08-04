# Divisare D2 duplicate review status

## Final metadata-only state

The immutable v2.2 artifact contains 286 Divisare-internal duplicate candidate
pairs:

- 66 strict v1.5 pairs were already confirmed in the parent artifact.
- All 220 previously pending or deferred pairs now have an approved v1 review.
- The review approves 8 merges, 128 separate-project decisions, and 84
  abstentions.
- No image content, pHash, embedding, vision model, or cross-site record was
  used.

The authoritative ledger is
`canonical/divisare_d2_decisions_v1.json`. It pins the exact v2.2 parent SHA,
all 220 candidate pairs, the 304 source-article guards, evidence, reviewer,
decision time, and reason code.

## Identity scope

D2 asks whether two Divisare articles represent the same architectural project
and intervention. It does not ask whether they share a site, client, event,
series, or physical complex.

- A photo essay, drawing article, detail article, and project article may merge
  when they identify the same project/intervention.
- Separate houses, blocks, phases, later interventions, competition entries,
  and event installations remain separate buildings.
- Supported context such as `same_complex`, `same_event`, or
  `successive_intervention` is stored as a relation instead of being used to
  collapse identity.

## Merge gate

A merge requires all of the following:

1. At least two independent identity-evidence families.
2. No hard conflict in address/site, brief, geometry, quantity, phase, or
   intervention scope.
3. Exact article, parent-building, parser, source-row, prose/abstract, and HTML
   guards.
4. A component-safe union: no reject or defer edge may collapse transitively
   through another merge.

Name or slug similarity, the same city/country, the same architect alone, and
tag similarity are candidate signals only. Repeated credits, copyright lines,
publication UI, and photographer boilerplate do not count as substantive text
evidence.

## Final decisions

| Decision | Pairs | Effect |
|---|---:|---|
| `merge` | 8 | Union the two parent building identities |
| `reject` | 128 | Keep separate; optionally retain a related-project edge |
| `defer` | 84 | Approved abstention; keep separate without asserting different identity |
| Total | 220 | Exact parent pending/deferred pair snapshot |

Reject relations:

| Relation | Pairs |
|---|---:|
| Distinct event entry | 86 |
| Distinct sibling building | 22 |
| Distinct same-name project | 11 |
| Distinct phase/intervention | 9 |

## Regression examples

The pairs that previously exposed unsafe title-based matching are explicitly
guarded:

| Pair | Decision | Reason |
|---|---|---|
| Residence A/B `260144/260145` | reject, `same_complex` | Two separate family houses |
| FASE I/III `235013/235152` | reject, `successive_intervention` | Showroom bays versus a later Corten entrance intervention |
| Vallecas 11/51 `110876/110882` | reject, `same_complex` | 35-unit/5,174.23 sqm block versus 123-unit/14,934 sqm block |
| Valencia Apartments `430452/437795` | reject, unrelated | Same template title, but El Carmen versus El Cabanyal and different briefs |
| CEPT `381279/381465` | defer | Campus label spans ambiguous 1962/2012 intervention scopes |

## Approved merges

The eight approved metadata-only merges are:

- `96467/343892`: CGAC, Santiago de Compostela
- `112411/343271`: Sihlhölzli sports facilities
- `237243/339073`: MUSE drawing/detail and project articles
- `317455/328691`: TER
- `339186/380335`: Alcalá duplex renovation
- `346253/449455`: Lindower 22 planned/completed coverage
- `348479/348989`: Murphy House
- `478764/536572`: Green Kilometer planned/completed coverage

Each has two or more independent evidence families recorded in the canonical
ledger. These decisions are examples, not a new automatic matching rule.

## Deferred likely duplicates

Long Museum `268679/383882` and Rolex Learning Center `346047/396651` are
likely the same buildings, but remain deferred. In each pair the sparse article
lacks enough project-specific prose, and the articles share no exact asset key,
materialized URL, raw URL, or Divisare album membership. Image content hashes
are not yet available.

These pairs can be reconsidered in the later image stage. pHash or visual
similarity may provide supporting evidence, but must never be the sole building
identity key.

## Runtime behavior

The v2.3 builder applies only the versioned ledger. It validates all 304 unique
article guards against the immutable v2.2 database, materializes redirects and
memberships for the eight approved merges, preserves related-project edges,
and leaves all reject/defer pairs separate. New candidates require a new
versioned decision ledger rather than an in-place edit.
