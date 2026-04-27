# Crawl Targets — Cross-Site Comparison & Priority

Recon completed 2026-04-28 across 4 candidate sources. This document
synthesizes the per-site `.claude/research/<site>-schema.md` files and
proposes an order. It does NOT replace those — read them for details.

---

## TL;DR

> **Architizer is the only "no decision needed" target.** The other three
> require a user policy or legal call before any code is written. ArchDaily
> is the most powerful technically, the most restrictive legally. Archello
> is the most architecturally interesting (product/material specs) but
> legally same-flag as Arquitectura Viva (EU `ai-train=no`). Arquitectura
> Viva is currently unverified — schema is inferred from search snippets.

Suggested execution order:

1. **Architizer first** — fully unblocked. Awards-driven ingest is a
   high-value cohort independent of the decisions on the other three.
2. **ArchDaily second**, *only after* a ToS-posture call (permission email,
   personal-use framing, or cross-validation-only).
3. **Archello third**, *only after* the EU `ai-train=no` policy decision +
   browser-UA verification of the product-spec join keys end-to-end.
4. **Arquitectura Viva last** — same `ai-train=no` flag AND the unique
   value (magazine-issue → project linkage) is unverified; needs ~30 min
   browser-UA recon before a build/skip call.

---

## Comparison matrix

| Dimension | ArchDaily | Architizer | Archello | Arquitectura Viva |
|---|---|---|---|---|
| **Verdict** | conditional | **easy** | moderate | moderate (unverified) |
| **Scale (projects)** | ~50,000 | ~10,785 | ~135,000 | unknown (≪ above) |
| **Discovery path** | sitemap (gzipped, 18 sub) | sitemap (175 sub) | sitemap (1,142 sub) | inferred (sitemap unconfirmed) |
| **Cloudflare / WAF** | none observed | passthrough w/ Mozilla UA | edge-blocks `ClaudeBot`; Chrome UA passes | edge-blocks `ClaudeBot` (UA whitelist) |
| **JS rendering** | server-rendered (project) | server-rendered | server-rendered | unknown |
| **Auth required** | no | no | no (BIM downloads gated) | no |
| **robots.txt — `User-agent: *`** | permissive; ToS overrides | bans `GPTBot` only | declares `ai-train=no` (EU DSM) | declares `ai-train=no` (EU DSM); bans `ClaudeBot` + 7 others |
| **ToS — automated copy** | **prohibits scraping** w/o written permission, personal non-commercial only | not flagged in recon | not surfaced separately from robots policy | not surfaced separately |
| **Parsing strategy** | `<meta name="cXenseParse:project-*">` tags | embedded JSON in `data-data='{...}'` on `.editable` divs | `<div data-key='{"brand_id":N,"project_id":M}'>` | unverified |
| **Architect pages** | `/office/{slug}` (slug only; **NOT in sitemap**) | firm pages in sitemap (~2,802) | `/brand/{slug}` shared w/ manufacturers (disambiguation needed) | `/tags/architects` is a list view; profile URL pattern unknown |
| **Rate (recommended)** | 2.0 s/req | 2.0 s/req | 2-3 s/req | unknown |
| **Full crawl walltime** | ~28 h | ~7 h | ≫ 75 h (135K @ 2 s) | unknown |
| **Unique value vs Divisare** | clean meta-tag schema (richer per-project location/photographer/manufacturer); huge English coverage | A+Awards 2013-2025 × 4 tracks (curated quality cohort, ~1-2K) | per-project product/material join (BIM-spec layer) | AV-magazine issue → project linkage (editorial provenance) |
| **Compounding risk** | legal | low | legal + namespace (firms / manufacturers conflated) | legal + unverified schema |
| **Engineering cost** | ~1-2 days | ~1 day | ~3-4 days (incl. brand-namespace disambiguation) | ~2 days (after schema verification) |

---

## Decision tree

```mermaid
flowchart TD
    START([Start: which sources next?])
    START --> ARCHITIZER

    ARCHITIZER{Architizer crawl}
    ARCHITIZER -- low-risk, high-value --> A1[Build crawl/architizer/<br/>awards-first, sitemap-second]
    A1 --> POLICY

    POLICY{User decides:<br/>EU ai-train=no policy<br/>+ ArchDaily ToS}
    POLICY -- accept ai-train + ArchDaily ToS --> ALL[Build crawl/{archdaily,archello,arquitectura-viva}/]
    POLICY -- accept ai-train only --> NOAD[Build crawl/{archello,arquitectura-viva}/<br/>skip ArchDaily]
    POLICY -- reject ai-train, accept ArchDaily ToS<br/>via partnership email --> ONLYAD[Build crawl/archdaily/<br/>skip Archello + AV]
    POLICY -- conservative --> STOP[Stop at Architizer<br/>3 sources total]

    classDef decision fill:#fff5e6,stroke:#cc8a00,stroke-width:2px,color:#000
    classDef action fill:#e6f5e6,stroke:#2e7d32,stroke-width:2px,color:#000
    class POLICY,ARCHITIZER decision
    class A1,ALL,NOAD,ONLYAD,STOP action
```

---

## Open decisions for the user

1. **EU `ai-train=no` Content-Signal** (Archello + Arquitectura Viva).
   Both sites declare a Cloudflare-managed reservation under EU Directive
   2019/790 Art. 4 explicitly refusing AI-training use. Our pipeline runs
   metalocus rows through Anthropic for enrichment; whether their data is
   permitted under this policy is a deliberate choice you make, not a
   technical issue. Options:
   - **(a) Respect the flag** — skip both, lose product-specs + AV-magazine angle.
   - **(b) Override knowingly** — proceed with full understanding it's a
     stated reservation; this is what we'd record as a deliberate choice
     in `.claude/Goal.md` non-goals/policy section.
   - **(c) Negotiate** — email each site's editorial contact, propose
     limited academic/personal use; framing matches our actual use.

2. **ArchDaily ToS** (auto-tool prohibition). Materially stronger than the
   `ai-train=no` flag — ToS prohibits scraping outright. Options:
   - **(a) Email partnerships@archdaily.com** for explicit permission;
     framing as personal architecture-research DB is honest.
   - **(b) Personal-private DB framing** — ToS allows personal,
     non-commercial use; if our DB never goes public, we're inside the
     letter of the ToS. (Risk: ambiguous; "scraping" prohibition is
     separate from "personal use" allowance.)
   - **(c) Cross-validation only** — fetch on-demand for buildings already
     known from Divisare/metalocus to enrich the canonical, avoid bulk
     mirror. ~720 fetches once instead of 50,000 + ongoing.
   - **(d) Skip ArchDaily** — accept the loss; Architizer fills similar
     niche legally.

3. **Architizer A+Awards as primary cohort.** A+Awards is curated quality
   that no other source provides. Even at minimum scope, we should ingest
   A+Awards 2013-2025 winners as a quality signal layer (a `is_a_plus_award`
   boolean + `award_year` + `award_track` per matched project). This is
   independent of any other decision. **Recommendation: yes, do this.**

4. **Crawl-source-of-truth ordering.** Once we have N sources, who wins
   when names conflict? Current canonical uses Divisare as spine. Adding
   sources requires a precedence policy. Suggested: Divisare > Architizer
   > ArchDaily > Archello > Arquitectura Viva (by editorial curation
   strength + structured-data quality). To be ratified when source 3 is
   added, not now.

---

## Per-source value snapshot

(Order: highest unique-value-per-engineering-hour first)

| Source | Unique value | Engineering hours | Risk |
|---|---|---|---|
| **Architizer A+Awards only** | Curated quality cohort (~1-2K projects) | ~6 h (small slice) | None |
| Architizer full sitemap | ~10,785 firm-uploaded projects | ~10 h + 7 h crawl | None |
| ArchDaily | ~50,000 server-rendered projects, cleanest meta tags, biggest English coverage | ~12 h + 28 h crawl | ToS hostile |
| Archello | Per-project product/material join (BIM-spec layer; unique data type) | ~25 h + ≫75 h crawl | EU `ai-train=no` policy + namespace disambiguation |
| Arquitectura Viva | AV-magazine-issue editorial provenance | ~16 h + unknown crawl | EU `ai-train=no` policy + schema unverified |

---

## Implementation note — folder structure (when greenlit)

Each new source slots into the existing 5-stage layout exactly the way
Divisare did, no special-casing:

```
crawl/
├── metalocus/         (existing)
├── divisare/          (existing)
├── architizer/        ← propose first
├── archdaily/         ← conditional on ToS posture
├── archello/          ← conditional on ai-train policy
└── arquitectura-viva/ ← conditional on ai-train policy + schema verification

data/crawl/
├── metalocus.db
├── divisare.db
├── architizer.db
└── ... (one DB per source)

.claude/research/
├── divisare-schema.md         (existing)
├── architizer-schema.md       ✓
├── archdaily-schema.md        ✓
├── archello-schema.md         ✓
├── arquitectura-viva-schema.md ✓
└── _crawl-targets.md          ← this file
```

---

## Next steps

1. **User reads this** + the four per-site schema docs.
2. **User answers Open Decisions 1-3** (`ai-train=no` posture; ArchDaily
   ToS posture; A+Awards yes/no).
3. **Architizer A+Awards crawler is built** as a starter — independent
   of every decision. ~6 hours engineering, 1 hour crawl.
4. **Re-evaluate** the other three sources when canonical_buildings_strict
   is uploaded and we see how much value the Divisare + Architizer
   combination already covers.
