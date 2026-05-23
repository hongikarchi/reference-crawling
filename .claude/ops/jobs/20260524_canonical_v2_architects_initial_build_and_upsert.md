# 2026-05-24 canonical_v2_architects initial build + Neon upsert

## Scope

New firm DB built from existing data. Powers make_web's "swipe buildings →
recommend firms" feature.

## Source data

- `data/id_registry_architects.json` — 39,633 active firms (arch_NNNNNN)
- `archello.db.archello_firms` (16,114) / `architizer.db.architizer_firms` (2,802)
  / `divisare.db.divisare_architects` (12,763)
- `canonical_v2_buildings.completeness_c23_final` (39,478 rows w/ 384-dim
  embeddings; reverse-indexed by `architect_canonical_ids`)

## Cycle

A1: initial build (Codex audit found 4 hard blockers — 18k issues total).
A2: sanitizer round 1 — name-suffix strip, social-brand URL blocklist,
    website https://, country/city validation. Reduced to 25 location residuals.
A3: portfolio-modal validation — country validated against KNOWN_COUNTRIES
    before modal selection; city modal restricted to in-country buildings;
    KosovoCommonAdd; trailing-punct trim; `<city> <country>` mashup extraction.
    Hard blockers: 0. Codex GO.

## Outputs

- `data/canonical/canonical_architects_v2.json` (14,216 firms; 25,417 registry
  entries without buildings skipped)
- `data/reports/canonical_v2_architects_build_report.json`
- New tools:
  - `tools/canonical_v2_architects_build.py`
  - `tools/canonical_v2_architects_neon_loader.py`
  - `tools/canonical_v2_architects_audit.py` (Codex quality gate)
- New doc: `docs/ARCHITECT_RECOMMENDATION.md` (schema + cold-start + cosine
  algorithm + SQL templates + provenance)

## Neon upsert

- Mode: `--upsert --confirm-db-write`
- Initial attempt failed at ~2000 rows: `psycopg2.OperationalError: SSL
  connection has been closed unexpectedly`. Root cause: single 14k-row
  transaction exceeded Neon idle timeout for vector(384) payload.
- Fix: `batch_size=100` + intermediate commits per 1000 rows. Retry committed
  cleanly.
- Final state:
  - 14,216 rows / 4,357 recommendable / 86 distinct countries
  - 0 mapping failures
  - HNSW index built on `portfolio_embedding`
- JOIN smoke: Foster + Partners (arch_000000) → 82 publishable buildings via
  `architect_canonical_ids @> array[arch_id]` LEFT JOIN.

## Decisions (user)

- Embedding: mean of building embeddings (re-uses building embedding space).
- Cohort: include all firms with ≥1 building; `is_recommendable` flag tags
  those with ≥3 publishable + has metadata.
- Source priority for conflicting fields: divisare > archello > architizer
  (with validation gate; portfolio-modal override for country/city).
- DB rename `neondb` → `architecture_data`: deferred. Documented mental
  model only (less risky than coordinated swap with make_web).

## Deferred

- 1,871 source_ref URL gaps (firms without slug in source firm tables —
  external enrichment cycle).
- 50 recommendable rows missing primary_country / 67 missing primary_city
  (Codex warnings; not blocking).
- Email column always null (no source has email).
- Logo coverage ~30% (archello-only); external fetch is separate cycle.
- make_web `profiles_office` migration to use `canonical_v2_architects` as FK
  source — make_web team.
