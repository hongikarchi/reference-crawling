# Make DB Response — Algo Support Precompute Tables (R1-R5)

| | |
|---|---|
| **From** | Make DB (`archi_data` owner) |
| **To** | Make Web (discovery-algorithm-dev) |
| **Status** | R1-R3 **BUILT — dry-run PASS**, deploy pending one user gate · R4 **smoke-tested** · R5 deferred |
| **Re** | Make Web request "추천 알고리즘 토대 강화 (Corpus Precompute)" |
| **Tooling** | `tools/canonical_v2_tag_stats_build.py` (build/QC), `data/canonical/tag_vocabulary_labels.json` (labels) |

## 1. Summary

R1-R3 are accepted and implemented as requested, with the schema below. The
full build ran against live Neon in a dry-run transaction (compute + QC +
ROLLBACK) and passed all 19 QC checks. Actual deployment is bundled into one
final user-gated transaction together with a pending data migration (see §4) —
partial application is impossible by construction.

## 2. Tables as implemented (deltas from your spec called out)

### 2.1 `canonical_v2_tag_stats` (R1)

```sql
axis TEXT CHECK (axis IN ('program','style','color_tone','atmosphere',
                          'material_visual','architectural_elements')),
tag TEXT, doc_freq INTEGER, total_n INTEGER, idf DOUBLE PRECISION,
corpus_version TEXT, computed_at TIMESTAMPTZ, PRIMARY KEY (axis, tag)
```

- `idf = ln(total_n / (doc_freq + 1))` as you suggested. Re-derivable from
  `doc_freq`/`total_n` if you want a different formula.
- **Delta 1 — 6th axis.** `architectural_elements` (14-term controlled vocab:
  Stair, Facade, Roof, Courtyard, Entrance, Corridor, Atrium, Terrace,
  Balcony, Garden, Fireplace, Column, Canopy, Skylight) ships in all three
  tables. A data migration deploying together moves element-type terms that
  were polluting `material_visual` (terrace/balcony/courtyard/…) into
  `architectural_elements`, so this axis is where those signals now live.
  Ignore it until you want a 6th question/matching axis.
- **Delta 2 — material tag space is large.** `material_visual` is free-text:
  **18,561 distinct tags** on publishable rows (12,985 are singletons,
  doc_freq = 1; only 116 tags have doc_freq ≥ 100). Your "전량 로드 수백 행"
  assumption holds only for the 5 controlled/clean axes. Recommended load:
  `WHERE axis <> 'material_visual' OR doc_freq >= 5` (or weight by
  `doc_freq`). All rows exist either way — nothing is silently dropped.

### 2.2 `canonical_v2_tag_centroids` (R2)

```sql
axis TEXT CHECK (...same...), tag TEXT, centroid VECTOR(384),
n_buildings INTEGER, corpus_version TEXT, computed_at TIMESTAMPTZ,
PRIMARY KEY (axis, tag)
```

- Mean of publishable-building embeddings per (axis, tag), **L2-normalized**
  (QC-verified `||centroid|| = 1 ± 1e-6`).
- **Caution:** `canonical_v2_buildings.embedding` vectors are **not**
  normalized (raw SentenceTransformer output, model
  `paraphrase-multilingual-MiniLM-L12-v2`). Use cosine (`<=>`) against
  centroids, or normalize the building vector before dot-product.
- `n_buildings == doc_freq` for every key (same GROUP BY, asserted in QC).
  Apply your `n_buildings < 20` confidence weighting as planned — 13k
  singleton material centroids are just that building's own embedding.
- Sanity check (dry-run): top-20 publishable buildings nearest the
  `('style','Brutalist')` centroid by cosine → 7 Brutalist (base rate ~3%),
  10 Contemporary (dominant class), 3 other. Signal is real.

### 2.3 `canonical_v2_tag_vocabulary` (R3)

```sql
axis TEXT CHECK (...same...), tag TEXT, display_ko TEXT, display_en TEXT,
is_generic BOOLEAN, sort_rank INTEGER, PRIMARY KEY (axis, tag)
```

- 147 curated entries (60 controlled-vocab terms + 87 material tags with
  doc_freq ≥ 100) carry human-reviewed `display_ko`/`display_en`/`is_generic`
  — review happens on our click-dashboard before deploy. Long-tail material
  tags get `display_en = tag`, `display_ko = NULL`, `is_generic = false`.
- `is_generic` seeds: `program/Other`, `style/Contemporary`,
  `color_tone/Neutral` + non-material noise that survives in material axis
  (`interior finishes`, `white walls`, `doors`, `railings`, `led lighting`,
  `unspecified materials`, …). Flagged for owner decision (not auto-set):
  `program/Housing` and `material_visual/glass` each cover ~40% of the corpus.
- `sort_rank` = per-axis rank by doc_freq DESC (alpha tiebreak); controlled
  terms with zero publishable occurrences rank last.
- **Join contract:** R1 keys == R2 keys; R1 ⊆ R3. R3 also contains
  unobserved controlled-vocab terms → LEFT JOIN R3 → R1.

## 3. Operating contract

| Item | Value |
|---|---|
| Source invariant | all stats computed over `is_publishable = true` only |
| Refresh | every crawl/canonical reload, one transaction (`DELETE` + `INSERT` + in-txn QC; FAIL → full ROLLBACK). Readers never see a partial state (MVCC). |
| `corpus_version` | `c23_final+matstrip` for the first deploy |
| Grants | `make_web` gets SELECT automatically (ALTER DEFAULT PRIVILEGES, 2026-05-24) — verified live in dry-run |
| Tag normalization | tags stored exactly as in `canonical_v2_buildings` columns (controlled axes Title Case, material lowercase free-text) — three tables join with buildings as-is |
| QC gate | 19 checks: total_n parity, per-axis doc_freq sums, OOV=0, idf formula, R1==R2 key parity, centroid norms, label coverage, grant probe |

## 4. Heads-up: numbers shift slightly at deploy

The same gated transaction applies the material-noise reclassify migration
(announced 2026-05-27, held until now per deploy-last policy):

- `is_publishable`: 36,864 → **36,673** (−191: 189 noise-only material rows +
  2 placeholder-architect rows)
- 9,606 rows lose noise terms from `material_visual`; 1,962 of them gain
  `architectural_elements` entries
- `canonical_v2_architects`: rebuilt — `is_recommendable` 4,357 → **4,348**,
  `top_materials` noise-stripped, `top_arch_elements` refreshed

All R1-R3 stats above are computed on the post-migration state.

## 5. R4 — new discriminative axes (smoke-tested, full run = separate scope)

- **era needs no LLM.** It derives deterministically from `project_year`
  (Pre-1900 / 1900-1945 / 1945-1980 / 1980-2000 / 2000-2015 / 2015+; coverage
  ≥ 99% of publishable rows since year presence is 99.67%). We can ship it as
  a generated column at near-zero cost whenever you want.
- The four visual/textual axes (scale / structural_system / roof_type /
  facade_pattern) were smoke-tested text-only through the same codex-exec
  path as D-1, with **proposed** vocabularies (pending owner approval — not
  yet in `core/vocab.py`):
  - scale: XS / S / M / L / XL
  - structural_system: Masonry / Reinforced Concrete / Steel Frame / Timber
    Frame / Hybrid / Shell-Membrane / Earth / Unknown
  - roof_type: Flat / Gabled / Hipped / Shed / Curved / Green Roof /
    Vaulted-Domed / Sawtooth / Unknown
  - facade_pattern: Grid / Louvered / Solid-Mass / Glazed Curtain /
    Perforated / Organic / Layered / Rhythmic Openings / Unknown

**Smoke results (N=100, text-only, seed-deterministic sample;
`data/reports/r4_smoke/report.N100.json`):**

| Metric | scale | structural_system | roof_type | facade_pattern |
|---|---|---|---|---|
| Unknown rate | **0%** | 52% | **69%** | 23% |
| Top values | S 38 · M 38 · XS 16 | Masonry 15 · RC 10 · Timber 9 | Flat 14 · Gabled 7 | Solid/Mass 31 · Layered 20 |

- Reliability: ok_rate 100/100, retry 0%, mean 4.8 s/item, ~482 in / 24 out
  tokens per item.
- Full-39k extrapolation (text-only, all 4 axes in one call): **~19M input
  tokens, ~53 h serial** (parallelizes like D-1 batches).
- era: 99% coverage from `project_year` alone.

**Verdict per axis:**
- `scale` — **ship text-only** (0% Unknown, sane distribution).
- `facade_pattern` — viable text-only (77% resolved); acceptable launch
  quality, top-up later.
- `structural_system` — marginal (52% Unknown): text + material heuristics
  could lift it, but expect sparse coverage.
- `roof_type` — **text fails (69% Unknown)**: needs a D-2-style vision pass
  over cover images, which is a different (order-of-magnitude larger) cost
  envelope.

Recommended R4 path: phase 1 = era (free) + scale + facade_pattern text run
(~2 of 4 axes at ~19M-token budget shared); phase 2 = vision pass for
roof_type/structural_system, costed separately. Full run starts only after a
separate cost approval and vocab sign-off (vocabularies above are proposals;
`core/vocab.py` is owner-gated).

## 6. R5 — multi-label confidence distributions: deferred

Agreed it is the right evolution, but it re-opens the D-1 enrichment contract
(prompt, schema, validator, canonical artifact, loader) for all 39k rows.
Proposal: revisit after R4 lands, reusing the same per-axis pipeline with
top-2 labels + weights rather than full distributions (cheaper, covers your
serendipity case). Not scheduled in this scope.

## 7. Query crib for make_web

```sql
-- TF-IDF base (boot-time load)
SELECT axis, tag, doc_freq, total_n, idf
FROM canonical_v2_tag_stats
WHERE axis <> 'material_visual' OR doc_freq >= 5;

-- kw_vec for a question answer (per request)
SELECT centroid FROM canonical_v2_tag_centroids
WHERE axis = %s AND tag = %s;

-- labels + blacklist (boot-time load)
SELECT axis, tag, display_ko, display_en, is_generic, sort_rank
FROM canonical_v2_tag_vocabulary;

-- freshness probe
SELECT DISTINCT corpus_version, max(computed_at) OVER ()
FROM canonical_v2_tag_stats LIMIT 1;
```
