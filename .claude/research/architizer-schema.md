# Architizer Schema (Phase 0 reconnaissance — completed 2026-04-28)

Unauthenticated reconnaissance with normal Mozilla UA.
All findings verified against fetched HTML / sitemap responses.

---

## TL;DR — the parsing unlock

**Every project page embeds full project state as JSON in
`data-data='{...}'` on `<div class="editable">` elements.** A single regex
+ `html.unescape` + `json.loads` yields PK, name, completion_date,
building_size, constr_status, description, hero, etc. — no per-selector
scraping needed for the core record. Easier than Divisare.

## Site overview

| Attribute | Value |
|---|---|
| Type | Firm-led project gallery + awards platform + product directory + journal |
| Projects | ~**10,785** (10×1000 + 785 in `sitemap-projects.xml` p1-p11) |
| Firms | ~**2,802** (`sitemap-firms.xml` p1-p3) |
| Brands / Products | 70 / 612 |
| Images (sitemap-listed) | 7,700 |
| Journal "ideas" | ~175,000 (175 sub-sitemaps; not interesting for us) |
| Subdomains | `winners.architizer.com` (awards results), `awards.architizer.com` (login-gated submission portal) |
| Hosting | Heroku behind Cloudflare CDN |
| Frontend | Server-rendered Django + jQuery + webpack (no React/Next) |

**What's distinct:** the **A+Awards** — a curated annual competition with
structured Jury/Popular Choice/Finalist tiers across ~80 typology
categories. SOM's profile shows `"Winner (24), Finalist (42), Special
Mention (2)"` inline. This is the **unique signal** Architizer adds vs
Divisare/metalocus.

## Access policy

- **Public read** — projects, firms, brands, awards, sitemap all return
  HTTP 200 without auth. No paywall on read.
- **Login required** for upload, firm dashboard, A+Award submissions
  (`/dashboard`, `/login`, `awards.architizer.com/a/account/`).
- **No public developer API.** `/api/v3` 404s; `api.architizer.com` 301s
  to a 404. Only `/api/v3.0/track-image-action` (internal tracking) is
  exposed. → must scrape HTML.
- **ToS yellow flag:** `User-agent: GPTBot / Disallow: /` — they
  explicitly ban AI crawlers. Our crawler isn't GPTBot, but the intent
  is clear.

## robots.txt

```
Sitemap: https://architizer.com/sitemap.xml
User-agent: *
Disallow: /admin
Disallow: /browse/images/*/
Disallow: /dashboard
Disallow: /*/metrics
Disallow: /search
Disallow: /login   /register   /logout   /collections
Disallow: /image-download/*
Disallow: /blog/tag/*
Disallow: /brands/*/edit/*
Disallow: /projects/create/
User-agent: GPTBot
Disallow: /
```

→ `/projects/`, `/firms/`, `/brands/{slug}`, `/products/` **not**
disallowed. No `Crawl-delay`. Sitemap published.

## Anti-bot

Cloudflare in near-passthrough mode for browser-UA. 5 sequential project
fetches: all 200, 0.31-0.61s, `cf-cache-status: DYNAMIC`. No Turnstile or
challenge HTML observed. JS not required — full HTML rendered server-side
in the initial response. (Verified at low volume only; could escalate at
scale — see Open Question 5.) If firm-edit ever needed: standard
email+password POST to `/login`, CSRF in `csrftoken` cookie.

## URL patterns

| Path | Purpose | Notes |
|---|---|---|
| `/projects/{slug}/` | Single project | hyphenated name (e.g. `lg-corporation-headquarters`) |
| `/projects/q/type:commercial,office/?page=N` | Faceted filter, paginated | comma-joined sub-types |
| `/firms/{slug}/` | Firm profile | e.g. `skidmore-owings-merrill`, `foster-partners` |
| `/brands/{slug}` | Product brand (no trailing slash) | e.g. `/brands/lladro` |
| `winners.architizer.com/{year}/Typology/` | A+Awards typology winners | hierarchical: `Commercial > Office`, etc. |
| `winners.architizer.com/{year}/Firms/` | A+Awards firm winners | links to `architizer.com/firms/{slug}/` |
| `winners.architizer.com/{year}/Products/` `Plus/` | Other award tracks | |
| `/sitemap-projects.xml?p=1..11` | Project sitemap (caps at p=11) | useful stop condition |

## Sitemap

```
https://architizer.com/sitemap.xml  ← root sitemap-index, 175 sub-sitemaps
├── sitemap-projects.xml   (×11 pages, ~10,785 projects)
├── sitemap-firms.xml      (×3 pages,  ~2,802 firms)
├── sitemap-brands.xml     (~70 brands)
├── sitemap-products.xml   (~612 products)
├── sitemap-images.xml     (~7,700 images)
└── sitemap-ideas.xml      (×175 pages, ~175K journal — skip)
```

Each `<url>` has `<loc>`, `<lastmod>` (ISO date), `<priority>`. Server-cached
24h. `lastmod` spans 2025-2026 — actively maintained.

## Project page schema

Sample: `https://architizer.com/projects/lg-corporation-headquarters/` (SOM, NY)

### Extraction recipe

```python
m = re.search(r"data-data='(\{[^']{200,30000})'", html)
record = json.loads(html_module.unescape(m.group(1)))
```

Decoded `data-data` (truncated):
```json
{"global_id":"projects.project.279741","pk":279741,"name":"LG Corporation Headquarters",
 "absolute_url":"/projects/lg-corporation-headquarters/",
 "description":"Upon its completion in 1986…",
 "completion_date":"2024-01-01T00:00:00","building_size":"sqft_100_300",
 "size":"100,000 sqft - 300,000 sqft","constr_status":"built","budget":0.0,
 "featured":0,"hero":{"id":4478504,"global_id":"media.mediaitemattribution.4478504",…}}
```

### Field map

| Field | Source | Notes |
|---|---|---|
| `architizer_pk` | `data-data.pk` | int, canonical PK (e.g. `279741`) |
| `global_id` | `data-data.global_id` | `projects.project.{pk}` (Django-style) |
| `slug` | URL path | display key |
| `name` | `data-data.name` / `<meta property='og:title'>` | |
| `description` | `data-data.description` | full multi-paragraph text |
| `description_short` | `<meta property='og:description'>` | ~155-char truncated |
| `completion_date` | `data-data.completion_date` | ISO `YYYY-01-01T00:00:00` (year-only) |
| `constr_status` | `data-data.constr_status` | enum: `built`, `concept`, … |
| `building_size` | `data-data.building_size` | enum slug: `sqft_100_300` |
| `size` (display) | `data-data.size` | `"100,000 sqft - 300,000 sqft"` |
| `budget` | `data-data.budget` | float, often `0.0` (unfilled) |
| `firm_url` | `<meta property='article:author'>` | `https://architizer.com/firms/{slug}/` |
| `firm_name` | `<title>` after `" by "` | |
| `categories` | `<meta property='article:tag'>` (multi) + `article:section` | `Commercial`, `Office` |
| `category_links` | HTML `a[href^="/projects/q/type:"]` | structured filter URLs |
| `location` | `<h2>` in project header | `"New York, NY, United States"` — split needed |
| `cover_image` | first `<meta property='og:image'>` | imgix CDN |
| `gallery_images` | all `<meta property='og:image'>` | typically 10-30 per project |
| `image_global_ids` | `data-globalid="media.mediaitemattribution.{id}"` | for image-level joins |
| `published_time` / `modified_time` | `<meta property='article:published_time/modified_time'>` | ISO datetimes |

### Fields not consistently present (LG-HQ sample)

`latitude/longitude` (no geo), award badges (cross-walk via firm/winners),
photographer credits (varies; in image attribution objects), materials/products
("Were your products used?" panel mostly empty for firm-led uploads).

## Firm page schema

Sample: `https://architizer.com/firms/skidmore-owings-merrill/`

| Field | Source | Notes |
|---|---|---|
| `slug` / `name` | URL / `<h1>` | `Skidmore, Owings & Merrill` |
| `office_locations` | sidebar | `"New York, NY"`, `"Chicago, IL"` |
| `description` / `team` | About + Team sections | team only on large firms |
| `awards_summary` | header badge | `"Winner (24), Finalist (42), Special Mention (2)"` |
| `projects_grid` | thumbnail grid (no pagination) | SOM has 119 on a single page |
| `social/website/phone/email` | sidebar | varies (small firms emphasize contact, large firms emphasize projects) |

**Quality variance**: SOM has 119 projects + locations + team. Thai
Obayashi (random sitemap entry) has 1 project + boilerplate mission.
Firm-led upload → uneven coverage.

## A+Awards crawl strategy (the unique value)

```
winners.architizer.com/{2013..2025}/{Typology|Firms|Products|Plus}/
   → cards link to /projects/{slug}/ or /firms/{slug}/
   → tier annotation: Jury Winner / Popular Choice Winner / Finalist / Special Mention
```

12+ years × ~4 tracks × ~80 typology categories. ~1-2K high-confidence
projects/firms total. Tag with `award_year + award_tier + award_category`
in our DB; cross-reference with main-site project records. **This is the
curated quality cohort the open `/projects/` firehose can't match.**

## Discovery strategy

```
A) Sitemap-driven (full coverage)
   sitemap-projects.xml{?p=1..11} → 10,785 URLs → fetch → parse data-data

B) Awards-driven (quality-first, RECOMMENDED)
   winners.architizer.com/{years}/{tracks}/ → harvest project + firm slugs
   → tag with award_year + award_tier → fetch → parse data-data

C) Firm-driven (supplementary)
   sitemap-firms.xml → 2,802 firms → harvest project lists + awards counts
```

**Recommended: B as primary** (curated), A as secondary (long tail where
quality permits).

## Rate-limit observations

5 sequential fetches: 0.31-0.61s, all 200. No 429, no challenges, no JS
gates. **Recommended `ARCHITIZER_REQUEST_DELAY_SECONDS = 2.0`** —
conservative for ~10K-page crawl; tune down if no throttling at scale.

## Comparison vs Divisare

| Dimension | Divisare | Architizer |
|---|---|---|
| Auth | **Required** (paid member) | **None** (public read) |
| Sitemap | 404 — must walk indexes | published, 175 sub-sitemaps |
| Project count | ~25-50K (designer-walk estimate) | **~10,785** |
| Curation | editorial floor | firm-led upload (variable) |
| Structured data | manual sidebar parsing | **single JSON blob in `data-data`** |
| Awards signal | none | **A+Awards** — biggest unique value |
| Description quality | high (curator-written) | mixed (firm copy ranges polished → boilerplate) |

**Architizer is a complement, not replacement.** Divisare = editorial
curation floor; Architizer = awards taxonomy + larger firm roster + firm-led
recent uploads. Combine for: (a) award-tier signal we lack, (b) firm
canonical IDs, (c) fresh firm-uploaded recent work. A+Awards continues
annually (14th edition closes April 2026); editorial weight lower than
Divisare but platform stable, increasingly awards-platform-first.

## Feasibility verdict

**EASY.** Public read, no auth, published sitemap, server-rendered HTML
with full project state in a single JSON blob, sub-second responses, no
Cloudflare challenges with normal browser UA, structured awards subdomain.
~13K page fetches × 2s = **~7 hours single-threaded**, well under a day.

## Open questions for Phase 1

1. **`data-data` JSON consistency** — verified only on LG-HQ. Confirm
   shape on 5-10 random + 5-10 award-winner samples before locking parser.
2. **Photographer / awards / materials structure** — re-parse an
   A+Awards-winning project from `winners.architizer.com/2024/` to find
   awards badges + photo credits in HTML.
3. **`/projects/q/?page=N` pagination cap** — does it 404 or return empty
   when exceeded? Bounds infinite-scroll filter walks.
4. **Image rights** — imgix URLs are sized CDN URLs. Check ToS before
   downloading to disk; safer default is reference-only.
5. **Anti-bot escalation** — Cloudflare config can change. Crawler should
   detect 403/429/challenge HTML and pause+back off, not blindly retry.

## What would change the recommendation

- **If Architizer adds a generic AI-bot block** (robots.txt or Cloudflare
  WAF) → stop and reconsider. Currently only GPTBot is banned.
- **If `data-data` JSON varies wildly** across samples → fall back to
  per-selector parsing; effort ~3×.
- **If A+Awards data isn't structured enough** to lift cleanly → the
  awards-first appeal collapses; defer.
- **If firm-led upload quality skew dominates** (Thai Obayashi-style
  placeholders >> SOM-style rich) → narrow to A+Awards cohort (~1-2K)
  and skip the long tail.
