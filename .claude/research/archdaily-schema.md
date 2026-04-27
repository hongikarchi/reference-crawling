# ArchDaily Schema (Phase 0 reconnaissance — completed 2026-04-28)

Unauthenticated reconnaissance with browser-style UA. 5 sample fetches; no
heavy crawling. All findings verified against fetched HTML / sitemap data.

> **Two-axis verdict up front.** Technically: **easy** (no Cloudflare, public
> sitemaps, server-rendered HTML, cXenseParse structured-meta layer).
> Legally: **restrictive** (ToS explicitly prohibits "automatic device...
> to copy or 'scrape'" without written permission; personal non-commercial
> use only). See §2, §11.

---

## 1. Site overview

- **Type:** architecture publication. Mix of curated project case studies
  ("Selected Projects"), news, competitions, op-eds, product catalog.
  Founded 2008 (Chile); 2020 acquired by Swiss NZZ Mediengruppe.
- **Scale:** ~102K URLs of shape `/{numeric_id}/{slug}` across sitemap1+2+3
  (49,922 + 50,000 + 2,487). A 20-URL random sample from sitemap1 looked
  roughly **half projects, half articles/news/op-eds** → project subset
  estimate **~50K** (not verified count). vs Divisare ~25K, metalocus ~3.5K.
- **Languages:** English (canonical, on `www.archdaily.com`), plus Spanish
  (`/cl/`, `/co/`, `/mx/`, `/pe/`), Portuguese (`/br/`), Chinese (`/cn/`),
  US (`/us/`). All localized prefixes are robots-disallowed → we naturally
  consume the canonical English layer with no cross-edition dedup.
- **Geographic focus:** global — 17.9M monthly readers (Wikipedia, 2022).
  LATAM-strong, then EU/Asia.

## 2. Access policy

- **Public, free, no registration** for reading project pages, architect
  profiles, sitemap, ToS — all `200 OK` anonymous in our tests.
- "Save to favorites" / personalized feeds require login (`my.archdaily.com`)
  but no public-page fields are login-gated.
- **ToS (`/content/terms-of-use`) explicitly prohibits scraping:** "use [of]
  an automatic device (such as a robot or spider) or manual process to copy
  or 'scrape' the Website or Website Content for any purpose without the
  express written permission of ArchDaily." Narrow carve-out for public
  search engines only. Use limited to "personal, non-commercial."
- **Implication:** crawl is technically trivial but legally requires either
  (a) written permission, (b) personal/private framing with no public
  republishing of derivative content, or (c) ToS-violation risk acceptance.
  Materially more restrictive than Divisare's empty robots.

## 3. robots.txt

```
User-agent: *
Disallow: *?replytocom
Allow: /
Disallow: /cl/
Disallow: /catalog/cl/
Disallow: /cn/
Disallow: /br/
Disallow: /catalog/br/
Disallow: /us/
Disallow: /mx/
Disallow: /catalog/mx/
Disallow: /co/
Disallow: /catalog/co/
Disallow: /pe/
Disallow: /catalog/pe/
Disallow: /1021178/AD

User-agent: AhrefsBot
Crawl-delay: 300

User-agent: msnbot
Crawl-delay: 10

Sitemap: https://www.archdaily.com/sitemap.xml
```

→ Permissive for `*` on the canonical English site. Localized subdirs blocked
(those are translations of the same projects — fine to skip). One-off
disallow `/1021178/AD` (a single article). No `Crawl-delay` for `*` —
implicit "be reasonable".

## 4. Anti-bot

- **No Cloudflare.** No JS challenge, no `cf-ray`, no "Just a moment...".
- Stack: nginx + AWS CloudFront (`server: nginx/1.14.2`, `via: ... CloudFront`).
  Cache-aggressive: `cache-control: s-maxage=604800` (7-day CDN cache) — kind
  to crawlers (most fetches `x-cache: Hit from cloudfront`).
- **Server-rendered HTML on project pages,** ~790 KB per page (verified).
  Gallery thumbnails lazy-load but `<a>` hrefs + meta tags are all present
  on first response.
- **No UA discrimination.** Identical 200 + identical etag for
  `Mozilla/5.0 ... Chrome/120` and a custom `archi-tinder-research/0.1` UA.
- **Caveat:** `/search/projects*` listing UIs return only a nav shell to a
  simple HTTP fetcher — results load client-side via XHR. Listings are
  **not directly scrapable** without JS. The sitemap path bypasses this.

## 5. Auth strategy

Not applicable. No login needed for any field we want. `my.archdaily.com`
exists for user accounts but no project-page fields are login-gated.

## 6. URL patterns

| Path | Purpose | Notes |
|---|---|---|
| `/{numeric_id}/{slug}` | Project OR article/news/op-ed | id is canonical PK (e.g. `1040705`). **Same shape for both** — distinguish via `archdaily:type` meta (see §7). IDs run ~33000 (2009) → 1,040,000+ today, ascending. |
| `/office/{slug}` | Architect/firm profile | slug-only, **no numeric ID**, **not in sitemap** — harvest from project-page anchors. |
| `/photographer/{slug}` | Photographer profile | slug-only, e.g. `/photographer/ema-peter-photography`. |
| `/search/projects[/categories/{slug}\|/country/{slug}]` | Listings, facets | **Client-rendered**. Bypass via sitemap. |
| `/sitemap.xml` | Sitemap index | gzipped `<sitemapindex>` of 18 child sitemaps. |
| `/{cl,cn,br,us,mx,co,pe}/...` | Localized editions | robots-disallowed; ignore. |

## 7. Data shape (project page)

Sample: `https://www.archdaily.com/1040705/passive-house-forest-retreat-stark`
("Passive House Forest Retreat / Stark", 2025, Pemberton, Canada).

The **headline extraction surface is the cXenseParse meta layer** —
ArchDaily's CMS emits a clean structured-data block of `<meta>` tags that
mirror most sidebar fields. This is *cleaner* than Divisare's CSS-selector-
on-sidebar approach. JSON-LD exists but is **empty** (`<script type='application/ld+json'>{}</script>` — verified). OpenGraph + cXenseParse are
the real source of truth.

### cXenseParse meta block (verbatim from sample)

```html
<meta content='1040705' name='cXenseParse:articleid'>
<meta content='2026-04-27T10:00:00+00:00' name='cXenseParse:publishtime'>
<meta content='Stark' data-separator=',' name='cXenseParse:project-office'>
<meta content='Pemberton,British Columbia,Canada' data-separator=','
      name='cXenseParse:project-location'>
<meta content='2025' name='cXenseParse:project-year'>
<meta content='Residential Architecture' name='cXenseParse:project-category-tier-1'>
<meta content='Ema Peter Photography' name='cXenseParse:project-photographer'>
<meta content='Hana Abdel' name='cXenseParse:project-curator'>
<meta content='Bocci' name='cXenseParse:project-manufacturer'>
<meta content='Miele' name='cXenseParse:project-manufacturer'>     <!-- repeats -->
<meta content='projects/residential-architecture' name='cXenseParse:taxonomy'>
<meta content='article' name='cXenseParse:pageclass'>
<meta content='Selected Projects' property='archdaily:type'>      <!-- filter key -->
```

### Field map

| Field | Source |
|---|---|
| `archdaily_id` | URL `/(\d+)/` or `cXenseParse:articleid` (canonical PK) |
| `slug` | URL tail |
| `name` | `og:title` (format: "Project Name / Firm") |
| `is_project` | `archdaily:type == 'Selected Projects'` — **filter at parse to drop articles/news** |
| `architect_name` (multi) | `cXenseParse:project-office`, comma-split |
| `architect_slug` | DOM `<a href="…/office/{slug}">` (no numeric ID exists) |
| `location` (city, region, country) | `cXenseParse:project-location`, comma-split |
| `project_year` | `cXenseParse:project-year` |
| `category` | `cXenseParse:project-category-tier-1` |
| `taxonomy` | `cXenseParse:taxonomy` (e.g. `projects/residential-architecture`) |
| `keywords_tags` | `<meta name="keywords">`, comma-split |
| `photographer` | `cXenseParse:project-photographer` |
| `curator` | `cXenseParse:project-curator` (AD editor — provenance) |
| `manufacturers` (multi) | repeated `cXenseParse:project-manufacturer` |
| `published_at` | `article:published_time` (ISO 8601) |
| `cover_image` | `og:image` (`images.adsttc.com` CDN) |
| `description_short` | `og:description` |
| `canonical_url` | `<link rel="canonical">` |
| `description_full` | **DOM body**, after "Text description provided by the architects." |
| `area` | **DOM specs block**, labeled, e.g. "4110 ft²" — **mixed units (ft²/m²), needs normalization** |
| `credits` (design team, engineers, contractor) | **DOM specs block** — freeform key:value pairs, e.g. "Lead Architects: STARK Architecture & Interiors", "Structural: Ikon Engineering" |
| `gallery_urls` | DOM gallery anchors + lazy `<img>`, ~25 imgs on sample at `images.adsttc.com/media/images/{hash}/{size}_jpg/...` |

## 8. Pagination + discovery

**Discovery flow:**

```
1) GET /sitemap.xml (gzipped sitemap-index, 389 bytes) → 18 child sitemaps:
     sitemap1.xml.gz   (49,922 URLs, oldest)
     sitemap2.xml.gz   (50,000 URLs, mid)
     sitemap3.xml.gz   (2,487 URLs, recent additions)
     sitemap-news.xml.gz                    (Google News feed)
     sitemap-images{1..11}.xml.gz           (image manifests; skip for metadata)
     catalog/.../sitemap.xml.gz             (product catalog; skip)
     my.archdaily.com/.../sitemap.xml.gz    (user pages; skip)
2) Decompress each, extract <loc> of shape /{numeric_id}/{slug} → ~102K
   URL candidates (dedup by id).
3) Per candidate: fetch HTML, check `archdaily:type` meta:
     'Selected Projects' → extract per §7 field map
     other               → skip (articles/news/op-eds)
4) Architect discovery (out-of-band): harvest /office/{slug} anchors from
   each project page; queue unique slugs for separate /office/{slug} fetches.
   Sitemaps contain ZERO /office/ URLs (verified).
```

- No project-page pagination — sitemap is ground truth.
- /feed RSS is news-style, not useful for project discovery.
- Sitemap-index `lastmod` was 2026-04-24 in our recon (~4 days fresh).
  Per-URL `<lastmod>` enables incremental updates without re-fetching.

## 9. Rate limit estimate

- robots.txt has no Crawl-delay for `*` (only AhrefsBot:300, msnbot:10).
- 3 sequential project fetches at 500ms gap → all 200 in 1.4-2.2s, no
  throttling, no 429. Most served from CloudFront cache (`x-cache: Hit`).
- No public reports of AD rate-limiting scrapers (WebSearch).

**Recommendation: 2.0 sec/request.** More conservative than Divisare's 3s
because there's no auth, but the ToS hostility argues for being invisible.
~50K projects × 2s ≈ **~28 hours** single-thread for a full crawl.

## 10. Comparison vs Divisare

| Axis | ArchDaily | Divisare |
|---|---|---|
| Projects | ~50K | ~25K |
| Auth / cost | none / free | login / €60/yr |
| robots.txt | permissive (canonical), blocks i18n subdirs | empty |
| ToS | **explicitly forbids scraping**, personal non-commercial | silent on scraping; personal/research only |
| Anti-bot | nginx + CloudFront, no challenge | none observed |
| Rendering | project pages server-rendered; `/search/*` JS-rendered | server-rendered |
| Sitemap | gzipped 18-child index, ~102K URLs | **404** — walk designer index |
| Headline metadata | **`cXenseParse:project-*`** meta layer | sidebar CSS selectors |
| JSON-LD | empty `{}` (present, unused) | not present |
| Architect IDs | slug-only `/office/{slug}` (less stable) | numeric `/authors/{id}-{slug}` |
| Architect discovery | harvest from project pages | walk `/designers/{region}` |
| Geo skew | global, LATAM-strong | global, EU-strong |
| Image CDN | `images.adsttc.com/.../{size}_jpg/...` | `images.divisare.com/.../v{ts}/...` |

**Stylistic similarity to Divisare:** both are project-page-centric with
sidebar/labelled metadata + body description + image gallery + tags.
A parser written for Divisare would port to ArchDaily with mostly selector
swaps; the **cXenseParse layer makes ArchDaily slightly easier mechanically**.

## 11. Feasibility verdict

- **Technical: easy.** No Cloudflare, server-rendered project HTML, clean
  gzipped sitemap-index, `cXenseParse:project-*` structured meta covers the
  headline fields without brittle CSS selectors. Realistic engineering:
  **~1-2 days** for parser + scheduler + dedup.
- **Legal: hostile.** ToS explicitly bans scraping by automatic device
  without written permission, limits use to personal non-commercial.
  Publicly readable, Google-indexed, but ToS is unambiguous. **Real
  conflict the user must decide.**

**One-line read:** the site rolls out the welcome mat (sitemap, clean meta,
no anti-bot); the ToS is a no-trespassing sign. Decide legal posture
*before* engineering starts.

---

## Next-step gates (for orchestrator, if user proceeds)

1. **ToS posture** — pick one: (a) email partnerships@archdaily.com for
   research permission; (b) personal/private DB framing (no public
   republication of AD-derived content; URLs/IDs only); (c) cross-validation
   only (on-demand fetch for already-known buildings, no bulk mirror).
2. **Scope** — full ~50K, or subset (year ≥ 2010, by category)?
3. **Image policy** — same as Divisare: store URLs, don't rehost pixels.

## Open questions for Phase 1

1. Project vs article ratio — 20-URL random sample looked ~50/50; tighten
   with a 200-URL filter pass on sitemap1.
2. Architect-slug stability — `/office/{slug}` is slug-only; test by
   re-fetching a known firm in a few months.
3. Multi-architect — verify `cXenseParse:project-office` comma-split on a
   co-design (e.g., `gardiner-museum-montgomery-sisam-architects-plus-andrew-jones-design`).
4. Area unit distribution — sample showed `4110 ft²`; spot-check 20 more
   to estimate sq-ft vs sq-m by country.
5. `<lastmod>` semantics — does it advance on edit, or only at publish?
