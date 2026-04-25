# Divisare Schema (Phase 0 reconnaissance)

**Status:** Template — to be filled in once authenticated access is established and we
fetch 5 sample project pages.

---

## Authentication

- **Mode used:** _(import-cookie | login)_
- **Session file:** `data/.divisare_session.json`
- **Cookies present:** _(list cookie names — typically `_divisare_session`, possibly
  `cf_clearance` from Cloudflare)_
- **Observed expiry:** _(weeks? months? — we'll learn empirically)_

## URL patterns

| Path | Purpose | Pagination |
|---|---|---|
| `/projects` | Recent projects index | _(page param? cursor?)_ |
| `/projects/{numeric_id}-{slug}` | Single project page | — |
| `/authors/{numeric_id}-{slug}` | Architect / firm page | _(/projects subpath?)_ |
| `/authors/{numeric_id}-{slug}/projects/built` | Architect's built projects | _(?)_ |
| `/tags` or `/categories` | Tag taxonomy index | _(?)_ |
| `/sitemap.xml` | _(reachable when authed?)_ | — |

## Project page fields (from sample fetches)

For project URL `_____` (replace with real example):

| Field | CSS selector / DOM path | Type | Required? | Example value |
|---|---|---|---|---|
| `id` | URL or `<meta property="og:id">` _(?)_ | int | yes | _e.g._ `556458` |
| `name` | `<h1>` or `<title>` | string | yes | _e.g._ `S-AR Oratory Chapel` |
| `architect_ids` | `<a href="/authors/{id}-...">` (multiple possible) | list[int] | yes | `[2144695353]` |
| `architect_names` | (paired with above) | list[string] | yes | `["Serie Architects"]` |
| `location_country` | _(?)_ | string | yes | |
| `city` | _(?)_ | string | maybe | |
| `year` | _(?)_ | int | maybe | |
| `area_sqm` | _(?)_ | numeric | maybe | |
| `photographer` | _(?)_ | string | maybe | |
| `description` | _(?)_ | string | maybe (long?) | |
| `typology` | _(?)_ | string | yes (their program field) | |
| `materials` | _(?)_ | list[string] | maybe | |
| `tag_ids` (thematic albums) | `<a href="/tags/{slug}">` _(?)_ | list[int or slug] | maybe | |

**Notes from observed HTML:**
- _Mark whether content is server-rendered or JS-injected (matters for fetch strategy)._
- _Note any inline JSON blobs (e.g., `<script type="application/ld+json">`) that simplify parsing._
- _Note Cloudflare/anti-bot indicators (challenges, JS gates) — if present, may need additional handling._

## Architect page fields

For architect URL `_____`:

| Field | Selector | Type |
|---|---|---|
| `id` | URL | int |
| `name` | `<h1>` or `<title>` | string |
| `country` | _(?)_ | string |
| `project_count` | _(?)_ | int |
| `project_ids` | links to `/projects/...` | list[int] |
| `aliases` _(if Divisare lists alternate spellings)_ | _(?)_ | list[string] |

## Tag / thematic album taxonomy

The site organizes content into curated albums (per Phase 0 web recon report):
- Elements
- Cities
- Houses
- Ideas
- Materiality
- Plans / Details
- Private interiors
- Public interiors
- Topics
- Types

For each album, document:
- URL pattern (e.g., `/tags/album-name`)
- How many tags inside
- Whether tags map cleanly to our 14-program enum or our atmosphere vocab

## Discovery strategy

How we'll enumerate projects to crawl:

- **Sitemap?** Fetch `/sitemap.xml` (if reachable) and report structure.
- **Tag index?** If sitemap unavailable, walk thematic albums listing pages.
- **Architect index?** As alternative — walk all architects, then their built-projects pages.
- **Trade-off:** sitemap is exhaustive; tag/architect walks may miss un-tagged projects but are more controllable.

Recommend: enumerate via sitemap if possible (fastest), otherwise architect index (gives us the canonical architect IDs as a side-effect).

## Observed rate-limit behavior

| Test | Outcome |
|---|---|
| 10 sequential requests, 0s delay | _(429? 200?)_ |
| 10 sequential requests, 1s delay | |
| 10 sequential requests, 3s delay | |
| Hour-long crawl at 3s/req (~1200 reqs) | |

Conclusion: recommended `DIVISARE_REQUEST_DELAY_SECONDS = ?` (default 3.0 in `config.py`).

## Open questions (resolve in Phase 1 build)

1. JS rendering required for any field? If yes, we need a headless-browser fallback (Playwright already used in make_web's web-testing).
2. Are there JSON-LD blocks that bypass HTML parsing entirely?
3. Does Divisare have a paid-member API that bypasses scraping? Worth one quick check via the account dashboard.
4. Image URLs in the project HTML — are they on Divisare's CDN with any access control, or are they raw photographer-hosted? Affects "store image URL only" decision in Phase 5.
