# Data Quality Benchmark — canonical_v2 (external-standards QC)

External benchmark + QC checklist for `canonical_v2_buildings` (36,864
publishable rows) and `canonical_v2_architects`, derived from a 2026-06-04 deep
research pass over primary standards (not SEO blogs). Runnable via
`tools/canonical_v2_qc_benchmark.py` (read-only).

## Why this exists

`qc_strict` + `canonical_v2_upload_validator` enforce *our own* schema/vocab.
This benchmark instead asks: **does the data meet external, citable
cultural-heritage + recommendation-systems standards** for a credible
architecture DB powering an image-first swipe/recommend app? It is the
"outside view" gate, complementary to the internal validators.

## Standards basis (primary sources)

| Domain | Standard | What it gives us |
|---|---|---|
| Schema / required fields | **Getty CDWA** core categories | Object, Classification, Title, Creation (creator+date+place), Materials/Techniques, Subject, Current Location, Authority links — the minimal credible record. Materials defined strictly as *substances used in fabrication*. |
| Field → entity (not free text) | **Wikidata** `architectural structure` Q811979 | Property set: architect P84, style P149, material P186, location P625, country P17, use/program P366. Item-datatype ⇒ values are entities, not strings. |
| Controlled vocab | **Getty AAT** (7 facets) + **ULAN** | Separate facets for Styles, Materials, Objects. "garden"(aat 300008090), "landscape"(300008626) are **Objects**, "lighting" Activity, "vegetation" organism — **none in the Materials facet** ⇒ standards basis for material-noise exclusion. ULAN = architect/firm authority. |
| Image-vs-record | **VRA Core 4.0** (LoC) | Separates Image / Work / Collection — maps to cover-image vs building-record; basis for per-image quality+dedup metadata. |
| Image dedup | MDPI Electronics 15(7):1493 (2026); McKeown & Buchanan, DFRWS EU 2023 (arXiv 2212.08035) | pHash near-dup at normalized Hamming similarity **S_hash ≥ 0.93** (high precision); CNN embedding cosine **> 0.85** transform-robust. PDQ inter-image mean 0.5000. **ColourHash/WaveHash unusable at scale** (chance-collision classes of thousands). |
| Cold-start | Hansen et al., SIGIR 2020 (arXiv 2006.00617) | Pure CF cannot represent zero-interaction items; content/portfolio embedding required. Validates our mean-of-building-embeddings architect vector. |

**Caveats (from the research):** Wikidata P186 does **not** enforce a
material value-type (that claim was refuted 0-3) — the material-exclusion
basis is Getty AAT facet separation, not Wikidata. CDWA "core" + Wikidata
constraints are *soft* guidelines, so "required" = strong completeness
default, not absolute. Dedup thresholds (0.93 / 0.85) were measured on
generic-object datasets (UKBench, ABO) — **re-calibrate on building imagery**
before locking as hard gates.

## QC checklist (11 rules, pass/fail thresholds)

Thresholds are engineering defaults to operationalize the standards — tune
against the real table. Severity: **FAIL** < threshold; **WARN** within 5pp.

| # | Rule | Metric | Threshold (publishable rows) | Maps to |
|---|---|---|---|---|
| R1 | Presence/completeness | non-null per field | name 100% · architect ≥99% · year ≥95% · country ≥85% · program ≥90% · ≥1 image 100% | CDWA core |
| R2 | Architect validity | resolves to person/firm id (not placeholder), `architect_canonical_ids` ⊆ id_registry | ≥99% resolvable, 0 placeholders | CDWA Authority / Wikidata P84 |
| R3 | Year sanity | `project_year` ∈ [1500, year+10]; `year_kind` consistent | 0 out-of-range; 0 completed-with-future-year | CDWA Creation date |
| R4 | Style controlled-vocab | `style` ∈ vocab.STYLE (12) | OOV < 5% | AAT Styles / Wikidata P149 |
| R5 | Material noise filter | `material_visual` ∩ MATERIAL_TAXONOMY_NOISE = ∅; not-all-empty | non-material pollution = 0; empty < 5% | AAT Materials facet |
| R6 | Image dedup (cover) | distinct cover per publishable building | dup-cover rate < 1% | pHash ≥0.93 / VRA |
| R7 | Hash-method choice | pHash/PDQ/NeuralHash only, never ColourHash/WaveHash | informational (process) | DFRWS 2023 |
| R8 | Cover selection | every publishable row has 1 non-null, non-dup cover | 100% | VRA Image-vs-Work |
| R9 | Cross-source dedup | residual dup projects (norm name + country + cover) | < 2% | data-quality uniqueness |
| R10 | Encoding/language | no mojibake / U+FFFD / unresolved entities in text fields | 0 rows | data-quality validity |
| R11 | Cold-start coverage | recommendable architect has non-null 384-dim portfolio embedding | 100% | SIGIR 2020 |

## Open calibration questions

1. Re-calibrate dedup thresholds (0.93 hash / 0.85 embedding) on our own
   crawl imagery before locking the cross-source gate.
2. Crosswalk `core/vocab.py` STYLE/PROGRAM ↔ AAT/Wikidata IDs to detect drift
   (vocab is user-owned — never auto-edit).
3. Operationalize concept-vs-built `year_kind` from crawl + LLM signals;
   define acceptable false-built rate.
4. Cold-start fallback for architects with zero publishable buildings
   (popularity/recency vs ULAN-bio features vs exclusion).

_Source: deep-research run 2026-06-04 (wd162d4i9), 28 primary sources, 24/25
claims confirmed. Full transcript in session subagents dir._
