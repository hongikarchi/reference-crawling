# canonical_v2 — C10 matcher-recall recovery + C11 taxonomy restoration

Date: 2026-05-23
Operator: Claude (local, single-operator)

## Scope

Two coupled improvements to `canonical_v2_buildings`, driven by the make_web
gap "Japanese library search returns nothing" and a standing doubt about
matcher recall:

1. **Matcher recall recovery (C10)** — the production building matcher
   under-merges (architect-anchored candidate generation; architizer/archello
   match-or-drop). Quantified, then the missed twins recovered.
2. **Fine-grained taxonomy restoration (C11)** — source-native taxonomy
   (divisare `tag_slugs`, architizer/archello `categories`) was collapsed to
   the 14-value `program` enum. Restored as four typed columns.

## Inputs

- `completeness_c9` strict artifact (39,776 rows) — base.
- 4 crawl DBs (`data/crawl/*.db`) — source taxonomy + twin re-blocking.
- `core/vocab.py` — extended with TYPOLOGY (35) + ARCHITECTURAL_ELEMENT (14).

## Tools created

- `tools/taxonomy_tag_inventory.py` — read-only crawl-DB tag census.
- `tools/canonical_v2_matcher_recall_audit.py` — read-only under-merge audit.
- `tools/canonical_v2_recover_dropped_twins.py` — C10 recovery build.
- `tools/build_typology_crosswalk.py` — source tag → vocab crosswalk generator.
- `tools/build_completeness_c11_taxonomy.py` — C11 taxonomy backfill.

## Outputs

- `completeness_c10_recovery` strict — 39,669 rows (107 merge losers removed).
- `completeness_c11_taxonomy` strict + embedded — 39,669 rows, 4 taxonomy
  fields + `typology_primary_source` provenance.
- `canonical/typology_crosswalk.json` — reviewable source-tag mapping.
- 5 new Neon columns wired into `canonical_v2_neon_loader.py` + validator.

## Key results

- Matcher recall audit: 6,137 high-confidence cross-source twins, **2,510
  (40.9%) missed** by production — 95% via source-row drops.
- C10 recovery: 2,119 dropped twins re-attached (~1,966 buildings move
  T3→T2/T1), 100 merge components, 107 duplicate rows removed. 268 twins
  unrecoverable (both sides absent from canonical).
- C11 taxonomy: typology coverage **0% → 92%** (36,515 of 39,669);
  `Japan ∩ Library` = 8 (was 0). Provenance: source_tags 32,319 / program
  3,814 / name 382. 3,133 publishable rows stay genuinely unclassifiable.
- QC: `qc_strict` PASS (10/10), upload validator PASS (0 failures).

## Cost

$0 LLM — all deterministic (crawl-DB joins, keyword crosswalk) + local
sentence-transformers re-embedding (no API).

## Result

PENDING — local build + QC complete; Neon migration + single upsert is
user-gated (additive ALTER for 5 columns, DELETE 107 merged ids, UPSERT
39,669 rows).
