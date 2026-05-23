# 2026-05-23 C23 final cleanup + Neon re-upsert

## Scope

Final polish cycle after C22 Neon upsert. Codex full reaudit confirmed hard
blockers 0; this addresses residual quality warnings.

## Changes

- Phase A: 6 name+country+city+year auto-merge (arch overlap required)
  - Mercedes-Benz Museum (Germany/Stuttgart/2006)
  - Hortus Conclusus (Italy/Benevento/1992)
  - Soulages Museum (France/Rodez/2014)
  - Alvaro Siza neighborhood (Italy/Venezia/2016)
  - Domus Aurea (Mexico/Monterrey/2016)
  - Arena du Pays d'Aix (France/Aix-en-Provence/2017)
- Phase B: country dirty extraction (4 → 0 mismatches via last-country-token
  parsing for "<city> <country>" raw source values; 51 sources cleaned).
- Phase C: 86 suspicious city nulled (street/postal/desc patterns).
- Phase D: sidecar refresh — 16 series groups (BUS:STOP/Vatican Chapel/etc),
  60 SEO candidates (all legit, sidecar-only), 165 gallery phash, 40 country
  real (down from 237 — 86% drop via native alias + dirty extraction),
  48 year.
- Phase E: `tools/canonical_v2_full_reaudit.py` (Codex re-audit tool) committed.

## Quality

- qc_strict: 10/10 PASS, 39,478 rows
- upload validator: 0 failures
- 16 series groups documented for manual review

## Neon

- Mode: `--upsert --confirm-db-write`
- DELETE 6 (C23 merge losers)
- UPSERT 39,478 rows; 0 mapping failures
- Result: `writes: committed`

| | C22 (Neon) | C23 (Neon) |
|---|---|---|
| Rows | 39,484 | **39,478** |
| Publishable | 36,870 | **36,864** |
| Tiers | T1 329 / T2 9,540 / T3 29,615 | T1 329 / T2 9,540 / T3 29,609 |

cumulative removed_canonical_ids: 292 → **298**

## Outputs

- `canonical_buildings_strict.completeness_c23_final.json`
- `canonical_buildings_strict_embedded.completeness_c23_final.json`
- `data/reports/canonical_v2_c23_final_report.json`
- 5 sidecars in `data/reports/canonical_v2_c23_*.jsonl`
- `data/reports/canonical_v2_c23_cumulative_removed.json`
- `data/reports/canonical_v2_c23_upload_validation.json`

## Deferred

- 40 country real / 48 year / 16 series / 165 gallery phash / 60 SEO sidecars
  (manual review)
- 222k gallery image phash missing (separate image pipeline)
- 1,159 gt60 / 234 lt3 image-count warnings (UX-only)
