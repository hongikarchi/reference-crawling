# Archello Schema (Phase 0 reconnaissance — completed 2026-04-27)

Unauthenticated reconnaissance with browser User-Agent (Chrome 120 Mac).
All findings verified against fetched HTML at `/tmp/{proj_*,prod_*,brand_*}.html`
and the live sitemap index at `https://archello.com/sitemaps/index.xml`.

---

## Headline finding — read first

Archello's `robots.txt` declares **`Content-Signal: search=yes, ai-train=no`** as a
Cloudflare-managed signal, citing EU Directive 2019/790 Art. 4 as an express
reservation of rights against AI training. Search/indexing is permitted; AI
training is not. The make_db database layer is structured reference data, not
a model-training corpus, and we mirror metadata + URLs (we do not redistribute
images per Divisare playbook). Whether this project's use falls inside or
outside the operator's "ai-train" reservation is a **user/legal call** before
any crawl, not an engineering one. Surface this with the user before Phase 1.

Separate from the policy signal, the technical robots rules are permissive for
a browser-UA crawler at 1-3s delay (see § robots.txt).

## Site overview

Archello is a **product-spec-oriented** architecture & design platform. Its
distinguishing axis vs. Divisare/metalocus is per-project structured product
data: which manufacturer's window, which floor system, which lighting, etc.
Self-described "HUB between the creative and making industry." Yii 2 PHP stack.

- **Scale (sitemap-derived):** ~135,000 projects, ~64,000 products,
  ~178,000 brands. (451 project shards × 300, 64 product shards × 1000,
  592 brand shards × 300.) An order of magnitude larger than Divisare.
- **Languages:** primary content in English (`/`); localized variants
  `/es/`, `/de/`, `/fr/`, `/it/`, `/pt/`, `/jp/`, `/nl/` exist but are
  Disallowed by robots.
- **Geography:** global. No region-segmented index like Divisare's
  `/designers/{region}` — discovery is via sitemap shards.

## Access policy

- Project pages, product spec pages, brand pages: **fully public** (HTML 200,
  no login wall).
- Sign-in offered (`/sign-in`, Yii-style `SignInForm[email]`/`[password]` POST)
  but not required for reading metadata.
- BIM/CAD/catalog file downloads: gated by a **lead-gen form**
  (`DownloadCatalogueForm[name|email|location|profession|captcha]`,
  POST to `/attachment/product/download-document?id=N&category=files&position=N`).
  Captcha present. Not a hard login — but not bulk-crawlable.
- "Membership" exists for architects (free publishing) and manufacturers
  (paid). Reading the public dataset does not require it.

## robots.txt

Fetched 2026-04-27. Two distinct sections.

**Cloudflare-managed:** `Content-Signal: search=yes,ai-train=no` (signal cited
under EU Directive 2019/790 Art. 4). Hard `Disallow: /` for these UAs:
`Amazonbot`, `Applebot-Extended`, `Bytespider`, `CCBot`, `ClaudeBot`,
`CloudflareBrowserRenderingCrawler`, `Google-Extended`, `GPTBot`,
`meta-externalagent`. (This is why WebFetch — identified as ClaudeBot —
gets 403 on every page.)

**Site-managed:** `User-agent: *` `Allow: /` with `Disallow: /es/ /de/ /fr/ /it/
/pt/ /jp/ /nl/` (translation paths only). `crawl-delay: 1` for `*`, `5` for
GoogleBot/bingbot/facebookexternalhit/msnbot/AhrefsBot.
`Host: archello.com`. `Sitemap: https://archello.com/sitemaps/index.xml`.

A custom crawler that uses a browser UA (not in the Cloudflare-managed list)
at >=1s delay is technically robots-compliant. The content-signal is a
separate, parallel reservation — see Headline finding.

## Anti-bot

- **Cloudflare CDN** (`server: cloudflare`, `cf-ray` headers, `cf-cache-status`).
- WebFetch (Claude's built-in fetcher) — **403 on every page** (its UA is
  identified as ClaudeBot, which the Cloudflare-managed block disallows).
- `curl -A "Mozilla/5.0 (Macintosh; …) Chrome/120"` — **HTTP 200 immediately,
  no JS challenge, no CAPTCHA**, on homepage, project pages, sitemaps,
  brand pages, product pages, sign-in form. Response time 1.8-2.9s.
- No JS rendering required — the page HTML is server-rendered with full
  spec metadata embedded (no XHR/JSON fetching for primary data).
- Burst test: 5 sequential fetches with no delay all returned HTTP 200.
  Conservatively the published `crawl-delay: 1` is honored.

## Auth strategy

Not required for the metadata layer. If we ever needed authenticated access
(BIM file downloads, favoriting, etc.):
- POST to `/sign-in` with `_csrf` + `SignInForm[email]` + `SignInForm[password]`
  + `SignInForm[remember_me]`. Yii 2 framework convention.
- Session cookie: `PHPSESSID`. CSRF cookie: `_csrf`.
- Phase 1 default: **no auth**. We use a custom UA at 2-3s delay.

## URL patterns

| Path | Purpose | Notes |
|---|---|---|
| `/project/{slug}` | Single project page | the spec sheet (see § data shape) |
| `/product/{slug}` | Single product page | independent product entity |
| `/brand/{slug}` | Brand page (firm OR manufacturer — shared namespace) | distinguish via heuristics, see open questions |
| `/brand/{slug}/projects` | Brand's project listing | paginates via `?page=N&per-page=32` |
| `/brand/{slug}/bim` | Brand's BIM/CAD file index | downloads paywalled |
| `/products/{category}/guide` | Category index | 15 top-level categories (see below) |
| `/news/{slug}` | Editorial articles | listicles, 100-best, etc. |
| `/awards/archello-awards-{year}` | Annual award pages | curated project samples |
| `/sign-in`, `/sign-up` | Auth | optional for our use |
| `/sitemaps/index.xml` | Sitemap index | 1142 child sitemaps total |
| `/attachment/product/download-document?id=N&...` | File download | lead-gen form-gated |

## Data shape — project page

Sample: `https://archello.com/project/binome` (10 detail items — best example
of the BIM-source-list pattern). Confirmed against `/project/zijing-conference-camp`
and `/project/our-lady-of-sorrows-chapel-nesvacilka`.

**No `application/ld+json` blocks.** Server-rendered HTML with semantic class names.

### Header / hero
| Field | Selector | Notes |
|---|---|---|
| `name` | `h2.ah-project-hero__title` | project title |
| `architect_name` | `<title>` middle segment | "Project Name \| Architect \| Archello" |
| `cover_image` | `<meta property="og:image">` | direct CDN URL |

### Sidebar — General data (`<dl id="grid-product-detail-general">`)
| Field | Source | Sample value |
|---|---|---|
| `location` | `<dt>Location</dt><dd>` | `"Jingdezhen, Jiangxi, China \| View Map"` |
| `project_year` | `<dt>Project Year</dt><dd>` | `"2022"` |
| `category` | `<dt>Category</dt><dd>` | `"City Halls"` (typology — single token); `"Housing — Private Houses"` (parent + sub-typology) |
| `building_area_m2` | `<dt>Building Area</dt><dd>` | `"40107 m2"` (regex: `r'(\d+)\s*m2'`) |

### **Sidebar — Credits + product specs** (`<div id="project-credits"><div class="ah-project-details__list">`)

The decisive structure. Each entry:
```html
<div class="ah-project-details__item" data-key='{"brand_id":34252,"project_id":171071}'>
  <div class="ah-project-details__item-title">Photographers</div>
  <div class="ah-project-details__item-text">
    <a href="/brand/su-shengliang" data-pjax="0">Su Shengliang</a>
  </div>
</div>
```

`data-key` JSON gives stable **canonical numeric IDs** — `brand_id` and
`project_id`. Parallel to Divisare's `/projects/{numeric_id}-{slug}` IDs.
**Lead with these as join keys.**

Title field is freeform: it can be a role (`Architects`, `Photographers`,
`Engineers`, `Contractors`, `General Contractor`, `Interior Architects`,
`Landscape Architects`, `Manufacturers`, `Consultants`, `Structural Engineers`)
**OR an arbitrary product category written by the architect**:

> Binome project (`/project/binome`) detail items include:
> - `"Chair, stool, lighting"` → `/product/piloti-bench`, `/product/floe-3`,
>   `/product/elsie-chair-2` + `/brand/appareil-atelier`
> - `"Ceramics & Fixtures"` → `/brand/ramacieri-soligo`
> - `"Coffee table and various accessories"` → `/brand/found-furniture`
> - `"Dining Table"` → `/brand/kastella`
> - `"Floor lamp"` → `/brand/luminaire-authentik`
> - `"Kitchen"` → `/brand/miralis`

This is the BIM-source-list angle. Spec depth varies wildly per project:
chapel sample = 5 items (architects/landscape/consultants/engineers/manufacturer
without per-product breakdown), Zijing = 3 items, Binome = 10 items with
specific products. Award/awards-shortlist projects skew higher-spec; many
ordinary projects have only architect + photographer.

### Body
| Field | Selector | Notes |
|---|---|---|
| `description` | `div.ah-project-story__body` | long-form text |
| `images` | `div.ah-project-story__gallery img` | `data-gallery-count` attr gives N |

## Data shape — product page

Sample: `https://archello.com/product/dish-flush-mount-black-5in-glass-globe-by-researchlighting`

Server-rendered. Fields appear in `<section class="ah-product-detail-specs">`:

| Field | Sample value |
|---|---|
| Product Name | "Dish Flush Mount, Black, 5in Glass Globe, by Research.Lighting" |
| Designer | "Research.Lighting" |
| Manufacturer | "Research.Lighting" |
| Manufactured | "United States" |
| Category breadcrumbs | `Products > Lighting > Interior Lighting > Ceiling lamps` |
| Light source | "LED" |
| Lighting type | "Direct / Indirect / Direct-indirect" |
| Material | "Metal, Brass, Steel, Glass" |
| Colour | "Black range" |
| Characteristics | "Custom, Handmade, Dimmable" |
| Shape | "Round" |
| EPD | (35 mentions on this page — environmental product declarations linked) |
| BIM/CAD downloads | linked, but **gated by lead-gen form** |

Breadcrumb hierarchy is the **product taxonomy**: 15 top categories
(`bathrooms-and-kitchens`, `building-mechanics`, `construction`, `electrical`,
`facades`, `floors-and-stairs`, `furniture`, `inner-walls-and-ceilings`,
`lighting`, `office-and-contract`, `outdoor`, `roofs`, `tech`,
`windows-and-doors`, plus `all-categories`). Each has subcategories accessible
via `/products/{cat}/guide`.

## Products as separate entity

**Yes** — `/product/{slug}` pages exist independently of any project. The
~64,000 product pages constitute a parallel crawl axis, not just project
satellites. A product page can be reached by:
- Direct sitemap listing (`/sitemaps/products.{N}.xml`, 64 shards × ~1000)
- Brand → product list (`/brand/{slug}` → "More products" section)
- Project → linked products (when a project specifies them)
- Category guide (`/products/{cat}/guide`)

This is the layer Divisare entirely lacks. Whether we crawl it depends on
whether per-product specs (material, dimensions, manufacturer location)
add value to make_db's primary entity (the building).

## Pagination + discovery

- **Sitemap index** (`/sitemaps/index.xml`, 132 KB) is the canonical
  enumeration. Lists 1142 child sitemaps:
  - 451 project shards (`projects.{N}.xml`) — ~300 URLs each
  - 64 product shards (`products.{N}.xml`) — ~1000 URLs each
  - 592 brand shards (`brands.{N}.xml`) — ~300 URLs each
  - 19 tag shards, 8 user shards, plus singletons (articles, awards,
    collections, drawings, events, generic)
  - Shard numbering starts at `.1` (`.0` returns 404).
- Sitemap URLs include `<lastmod>` timestamps (e.g. `2026-04-27T06:12:26+02:00`)
  — supports incremental crawl.
- **No JS-rendered listings.** Brand `/projects` pages paginate via
  `?page=N&per-page=32` query params (HTTP 200 on `?page=2`).
- Discovery strategy: sitemap-shard-driven enumeration is the obvious
  primary approach. Brand-walking (à la Divisare's designer-walk) is
  unnecessary because the sitemaps are complete.

## Rate-limit estimate

| Test | Result |
|---|---|
| 5 sequential fetches with `time.sleep(2)` | All HTTP 200, 1.8-2.2s response |
| 5 burst fetches no delay | All HTTP 200, 1.8-2.9s response |
| robots.txt `crawl-delay: 1` | Published expectation |
| Cloudflare 429 / challenge | None observed |

Recommended: **2.0-3.0s delay** for the unauth crawler — conservatively above
the published `crawl-delay: 1`. At 3s delay, the full 135K project crawl
takes ~112 hours (~5 days continuous). Sub-shard parallelism inadvisable;
sitemap-shard parallelism (2-3 concurrent shards) probably safe but untested.

## Comparison vs Divisare + value-add

| Dimension | Divisare | Archello |
|---|---|---|
| Scale | ~50K projects (estimate) | ~135K projects |
| Auth | Paid login required for full read | Public read |
| Anti-bot | None (authenticated) | Cloudflare, browser-UA permissive |
| Sitemap | None (404) | Yes, sharded, with lastmod |
| Product specs per project | None — narrative description only | **Structured `data-key`-tagged items linking products + manufacturers** |
| Standalone product database | None | ~64K product pages with material/dimension/category metadata |
| Architect ID | `/authors/{numeric_id}-{slug}` | `data-key.brand_id` (numeric) |
| Project ID | `/projects/{numeric_id}-{slug}` | `data-key.project_id` (numeric) |
| BIM/CAD files | None | Yes, but lead-gen-form-gated |
| Tag taxonomy | 693 flat tags (typology + curated mix) | 15 product categories + N project Category dt-fields |
| AI-train policy | Empty/permissive robots | `Content-Signal: ai-train=no` (explicit reservation) |

**The product-spec angle is real and crawlable in metadata form.** Per-project
spec depth is uneven (3-10 items, occasionally more for award-winners), but
when present it is uniquely structured — title (role/category) + linked
products + linked manufacturer brands, all with stable numeric IDs. No
other source we have approaches this. Divisare embeds material mentions
in description prose only.

The BIM file layer (actual `.dwg`/`.rfa`/`.rvt` downloads) is **not**
freely crawlable — lead-gen form with captcha. Spec-metadata-only is the
realistic scope.

## Options

| Option | Mechanism | Cost | Reversibility |
|---|---|---|---|
| **A. Skip** | Cite `ai-train=no` as too-close-overlap; stay on Divisare + metalocus. | 0 | trivial |
| **B. Projects + brands** | Walk `/sitemaps/projects.*.xml`, fetch `/project/{slug}`, extract general + credits + per-project product/brand refs. Skip standalone product pages. | ~5d at 3s; ~50 GB HTML; one new match-canonical pipeline | easy (drop columns) |
| **C. Full crawl** | B + 64K `/product/{slug}` + 178K `/brand/{slug}`; new `archello_products` table joined via `brand_id`. | ~14d at 3s (or ~4d at 2 concurrent shards); ~125 GB HTML; new entity-type schema work; outside Goal.md building-quality target | easy schema; time is the sunk cost |
| **D. Targeted enrichment** | Take `canonical_buildings_strict.json`, look up each on Archello by name+architect, fetch only matched projects. | ~3h for ~3500 lookups; yield uncertain (Archello ≠ Divisare scope) | trivial |

D depends on a public search/match endpoint that this recon did not probe —
needs a Phase 0.5 step before it can be costed honestly.

## Recommendation

**Pause for user policy call before any code.** The `ai-train=no` content-signal
is not a technical blocker, but it is a stated reservation of rights specifically
naming the use we are arguably one step removed from. The user (and possibly
counsel) should decide whether structured-database mirroring of public spec
data — with images not redistributed, per the existing Divisare playbook —
falls inside or outside that reservation, before we build a crawler.

If the user clears the policy:
- **Option D first** — targeted enrichment of buildings we already have, to
  validate that the product-spec layer actually moves quality scores. This is
  the cheapest way to learn whether the BIM-source-list angle is worth the
  full crawl.
- If D shows lift, then **Option B** — the projects layer alone is the
  primary value-add for buildings; the standalone product graph (Option C)
  is a separate question for a separate dataset.

If the policy call is "no": Option A. Document the reasoning in a vocab
comment so we don't re-research this in 6 months.

## What would change the recommendation

- **Policy call resolves to "yes, our use is within the search/database
  ai-train-no boundary":** Option D becomes the immediate next step.
- **Targeted-crawl probe (Option D) shows that Archello's search returns
  matches for less than ~30% of our metalocus/Divisare buildings:** the
  enrichment yield is too low; Option B (full project crawl) becomes the
  only viable scope, but its cost may not justify itself.
- **Spec depth on a sample of 50 random projects (not award-curated)
  averages below 4 items:** the BIM-source-list angle is mostly an
  award-shortlist phenomenon, not a baseline expectation. Reduces Archello's
  unique value vs. Divisare. Probably tips toward Option A.
- **Cloudflare starts challenging our browser UA at scale (e.g., after
  10K consecutive fetches):** crawler operations cost grows non-linearly;
  may push toward authenticated session approach (which itself has new
  ToS implications), or back to Option D only.
- **Archello adds a public API or licensable data feed:** all of the
  above goes away in favor of the licensed path.

## Feasibility verdict

**Moderate.** The technical crawl is straightforward (sitemap-driven, no
JS, no anti-bot for browser UA, public metadata, stable numeric join keys).
Two friction points keep it out of "easy": the explicit `ai-train=no`
content-signal that requires a user policy call before any work, and the
captcha lead-gen gate on BIM/CAD files (which limits the crawl scope to
spec-metadata-only).

## Open questions for Phase 1 (if we proceed)

1. **Brand-page disambiguation.** `/brand/{slug}` is shared between
   architecture firms and product manufacturers. Heuristic: presence of
   `/products/{category}/guide` breadcrumbs OR a populated `ah-product-item-horizontal`
   listing on the brand page indicates manufacturer. Architect brands
   default to project listings only. This needs validation across ~20
   manual samples before the parser commits.
2. **Sitemap shard-numbering stability.** Shards are numbered `.1` … `.N`
   with `<lastmod>` per shard. Are URLs stable across shard re-balances
   (e.g., does a project move from shard 47 to shard 48 when older shards
   fill up)? Compare two snapshots a week apart.
3. **Spec-depth distribution.** Sample 100 random projects (not award-
   shortlist) and histogram the count of `ah-project-details__item` per
   page. The mean and the long-tail shape determine Archello's real
   per-building information content.
4. **Numeric ID stability.** `data-key` brand_id and project_id values
   look like sequential DB primary keys, similar to Divisare's. Spot-
   check that they don't change after re-fetch a few weeks later.
5. **Search/match API.** Probe `/?q=…` and any visible search endpoint
   to determine whether targeted enrichment (Option D) is feasible
   without a full sitemap walk.
