# Make DB Response — Algo Support Precompute Tables (R1-R5)

| | |
|---|---|
| **From** | Make DB (`archi_data` owner) |
| **To** | Make Web (discovery-algorithm-dev) |
| **Status** | R1-R3 **DEPLOYED 2026-06-12** · R4 **DEPLOYED 2026-06-14** (5 new axes live on `canonical_v2_buildings`, tag tables now 11-axis) · R5 deferred |
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
| `corpus_version` | `c23_final+matstrip+r4` (current; was `c23_final+matstrip` at the R1-R3 deploy) |
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

## 5. R4 — new discriminative axes (SHIPPED 2026-06-14)

Five new axes are live as columns on `canonical_v2_buildings` and as tag rows
in all three precompute tables (now **11 axes**, 18,655 rows each,
`corpus_version = c23_final+matstrip+r4`). Vocabularies are owner-approved and
in `core/vocab.py`. **`Unknown` is stored as `NULL`** — never the string
`'Unknown'`.

```sql
era               TEXT  -- Pre-1900 / 1900-1945 / 1945-1980 / 1980-2000 / 2000-2015 / 2015+
scale             TEXT  -- XS / S / M / L / XL
structural_system TEXT  -- Masonry / Reinforced Concrete / Steel Frame / Timber Frame / Hybrid / Shell/Membrane / Earth
roof_type         TEXT  -- Flat / Gabled / Hipped / Shed / Curved / Green Roof / Vaulted/Domed / Sawtooth
facade_pattern    TEXT  -- Grid / Louvered / Solid/Mass / Glazed Curtain / Perforated / Organic / Layered / Rhythmic Openings
```

**How they were produced.** `era` is derived deterministically from
`project_year` (zero LLM cost). The other four were LLM-tagged: a full
text pass over all 39,478 rows, then a vision pass over cover images for the
three visually-resolvable axes. The two signals were merged per axis:

| axis | merge policy | rationale (validated in review §below) |
|---|---|---|
| `scale` | text only | text sees the program; one photo doesn't |
| `structural_system` | text wins, vision fills gaps | prose ("CLT structure") beats a photo guess — review: text 86% vs vision 14% on disagreements |
| `facade_pattern` | text wins, vision fills gaps | review: text 90% vs vision 10% on disagreements |
| `roof_type` | vision wins, text fills gaps | purely visual — review: vision 67% vs text 33% on disagreements |

**Deployed coverage (publishable, 36,673 rows; `NULL` = could not resolve):**

| axis | era | scale | structural_system | roof_type | facade_pattern |
|---|---|---|---|---|---|
| non-NULL | 99.7% | 100% | 91.6% | **63.2%** | 90.9% |

**Quality gate (human review, N=200 sample, deterministic seed;
`data/reports/r4_review/accuracy.json`):** per-axis approve rate on the random
stratum — scale 100%, roof_type 100%, structural_system 96.4%, facade_pattern
94.9%. All four clear the ≥90% gate.

**Contract notes for make_web:**
- These axes behave like the other controlled axes in the tag tables — same
  `tag_stats` / `tag_centroids` / `tag_vocabulary` schema, same join key.
- **`sum(doc_freq)` for an R4 axis equals the non-NULL row count, NOT
  `total_n`** (36,673). roof_type especially: ~37% of publishable rows are
  NULL. Do not assume every row contributes to every axis.
- Labels (ko/en) for all 34 new tag values are in `tag_vocabulary`; none are
  `is_generic`. Generic-share INFO candidates (corpus share > 25%) surfaced at
  build: `era/2015+`, `scale/S`, `scale/M`, `structural_system/Reinforced
  Concrete`, `roof_type/Flat`, `facade_pattern/Solid/Mass` — left
  non-generic pending an owner call, flag them in your weighting if needed.

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

-- R4 per-building axes (NULL = unresolved; filter, don't COALESCE to a fake value)
SELECT canonical_bld_id, era, scale, structural_system, roof_type, facade_pattern
FROM canonical_v2_buildings
WHERE is_publishable AND facade_pattern = %s;   -- any R4 axis is a plain WHERE filter
```
