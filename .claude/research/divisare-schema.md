# Divisare Schema (Phase 0 reconnaissance — completed 2026-04-25)

Authenticated reconnaissance with logged-in paid-member session.
All findings verified against fetched HTML (saved in `data/divisare_samples/`).

---

## Authentication

- **Mode used:** `login` (Rails-style `person[email]`/`person[password]` POST to `/people/login`)
- **Session file:** `data/.divisare_session.json` (chmod 0600; gitignored via `data/`)
- **Cookies set after login:** `_divisare_com_session`, `remember_person_token`
- **Session duration:** unknown empirically; `remember_person_token` suggests "remember me" semantics → likely weeks-to-months. Re-run `divisare_auth.py login` if `verify` fails.
- **No Cloudflare challenge** observed for authenticated sessions with the browser-style UA in `config.DIVISARE_USER_AGENT`. Public/unauthenticated requests would face full 403 walls.

## robots.txt

```
# See http://www.robotstxt.org/robotstxt.html ...
# To ban all spiders from the entire site uncomment the next two lines:
# User-agent: *
# Disallow: /
```

→ **Empty / permissive.** No `Disallow` rules. ToS still applies; respectful rate is on us.

## URL patterns

| Path | Purpose | Notes |
|---|---|---|
| `/projects/{numeric_id}-{slug}` | Single project page | id is the canonical project key (e.g. `556458`) |
| `/authors/{numeric_id}-{slug}` | Author / firm page (the "designer") | id is canonical architect key (e.g. `2144695353`) |
| `/authors/{numeric_id}-{slug}/projects/built` | Architect's built projects | paginated, 11-20 per page |
| `/authors/{numeric_id}-{slug}/projects/unbuilt` | Architect's unbuilt projects | (untested but inferred) |
| `/{tag-slug}` | Flat tag/album page (e.g. `/chapels`, `/extra-small`, `/airports`) | 20 projects per page; paginated via `?page=N` |
| `/projects` | Mislabeled "General Index" — actually a designer/photographer index by region | useful for crawl discovery |
| `/designers/{region}` | Designer index for region (europe, asia, americas, africa, oceania) | counts: europe=120, asia=40, americas=40, africa=8, oceania=3 (per /projects sample) |
| `/photographers/{region}` | Photographer index | not directly useful |
| `/login` | Login form (GET) | session + CSRF |
| `/people/login` | Login submit (POST) | success → 302 to `/` |
| **No `/sitemap.xml`** | 404 even authenticated | discovery must walk indexes |
| **No `/tags`, `/categories`** | 404 | enumerate tags via project-page tag links |

## Discovery strategy (Phase 1)

The `/projects` General Index lists **designers grouped by region**, not projects directly. Two viable enumeration paths:

```
A) Designer-driven (recommended)
   /projects → harvest /authors/{id}-{slug} URLs
              → for each author: /authors/{id}-{slug}/projects/built (paginate)
              → harvest /projects/{id}-{slug} URLs
   Side benefit: gives canonical architect IDs as a side-effect.

B) Tag-driven (supplementary)
   homepage / project-page tag links → flat /{tag-slug} pages
   → harvest /projects/{id}-{slug} URLs from each tag page (paginate)
   Side benefit: gives the tag taxonomy + per-project tag membership.
```

Recommended: **A as primary** (canonical architect IDs are precious). B as secondary to fill gaps.

## Project page schema

Sample: `https://divisare.com/projects/556458-s-ar-oratory-chapel`

Top-level structure:
```
<article>
  <div class="project">
    <div class="row">
      <div class="small-12 columns">
        <div class="header">             ← title block
          <div class="designers">…</div>     ← architect name (text) [+ link?]
          <h1>Project Name</h1>              ← canonical project name
          <div class="abstract">…</div>      ← short description
        </div>
      </div>
    </div>
    <div class="row">
      <div class="small-12 medium-9 columns">
        <div class="description">…</div>     ← full body / long description
        <div class="image">…</div>           ← image gallery (lazy-loaded)
        <div class="info">…</div>            ← additional credits/info
      </div>
      <div class="small-12 medium-3 columns">
        <div class="sidebar">                ← STRUCTURED METADATA HERE
          <div class="divider first"><div class="text">Published on April 21, 2026</div></div>
          <div class="content">
            <div class="section location">Location</div>
            <div>Mexico - Santiago</div>
          </div>
          <div class="content">
            <div class="section">Designer</div>
            <div><div class="designers"><a href="/authors/2144754813-s-ar">S-AR</a></div></div>
          </div>
          <div class="content">
            <div class="section">Project Year</div>
            <div>2024</div>
          </div>
          <div class="divider"><div class="text">Atlas of Architecture</div></div>
          <div class="content">
            <ul class="tags">
              <li><a href="https://divisare.com/chapels">Chapels</a></li>
              <li><a href="https://divisare.com/extra-small">Extra Small</a></li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  </div>
</article>
```

### Extractable fields (with concrete selectors)

| Field | Selector | Notes |
|---|---|---|
| `divisare_id` | `re.match(r'/projects/(\d+)-', url)` | from URL — canonical PK |
| `slug` | URL after the id | display-friendly |
| `name` | `div.project div.header h1` | the project title |
| `architect_name` | `div.project div.header div.designers` (text) | display-friendly |
| `architect_id` | `div.sidebar div.section[Designer] + div div.designers a[href]` → parse `/authors/(\d+)-…` | canonical FK; an array if multi-author |
| `architect_slug` | from same href | |
| `abstract` | `div.project div.header div.abstract` | one-line description |
| `description` | `div.project div.description` | long body text |
| `published_date` | `div.sidebar div.divider.first div.text` → strip "Published on " | ISO-parseable |
| `location_country` + `city` | `div.sidebar div.section.location + div` (text) → split on " - " (left=country, right=city) | format observed: "Mexico - Santiago" |
| `project_year` | `div.sidebar div.section[Project Year] + div` (text strip) | int |
| `album_name` | `div.sidebar div.divider:not(.first) div.text` | e.g. "Atlas of Architecture" |
| `tags` | `div.sidebar ul.tags li a` | list of `(href, text)`; href is `https://divisare.com/{slug}` |
| `cover_image` | `<meta property="og:image">` | direct CDN URL |

### Fields NOT consistently present (per S-AR sample)

- `area_sqm` — not in sidebar of this sample
- `photographer` — not in sidebar; may be in `.info` block on photo-credit-rich projects
- `materials` — no labeled section; likely embedded in description text only

These may appear in other projects. Phase 1 parser should handle them as `Optional[…]` and skip when absent.

### Image gallery

- Cover image always available via `og:image` meta
- Inline `<img>` in `.image` div mostly **lazy-loaded** (only 1 of N visible in HTML on first load)
- Image URLs follow `https://images.divisare.com//images/f_auto,q_auto,w_auto/v{ts}/{uuid}/{slug}.jpg`
- **Per the plan: we do NOT download Divisare images** (ToS + we already have metalocus images). We may store the URLs as references for buildings without metalocus matches.

## Architect page schema

Sample: `https://divisare.com/authors/2144695353-serie-architects`

| Field | Selector | Notes |
|---|---|---|
| `divisare_architect_id` | URL `/authors/(\d+)-` | canonical PK |
| `slug` | URL tail | |
| `name` | `<h1>` (skip the two "divisare" branding h1s) | |
| `description_body` | `.description` | "Serie Architects is an architectural practice based in London, United Kingdom." |
| `address` | `.sidebar` text — patterns: "Islington, United Kingdom", "London, United Kingdom" | unstructured; parse with care |
| `phone` | regex `Phone:\s*([^\s]+)` in `.sidebar` | |
| `email` | regex `Email:\s*[\[\]\w]+` (Divisare obfuscates as `[email protected]`) | mostly useless to us |
| `website` | regex `www\.[^\s]+` | useful for cross-validation |
| `project_count_visible` | `len(re.findall(r'/projects/\d+', html))` | only first 15 projects shown; full list under `/projects/built` |

## Tag/album page schema

Sample: `https://divisare.com/chapels`

| Field | Selector | Notes |
|---|---|---|
| `tag_slug` | URL last segment | |
| `tag_name` | `<h1>` | "Chapels" |
| `tag_curated` | `<title>` says "...A collection curated by Divisare" | confirms album is editorially curated |
| `projects` | `<a href="/projects/{id}-{slug}">` | 20 per page |
| `pagination` | `?page=N` query param | check by walking until empty |

## Sitemap

- `/sitemap.xml` returns **404** even authenticated.
- No alternate sitemap discovered.
- Conclusion: enumerate via designer-walk (strategy A).

## Rate-limit observations

| Test | Outcome |
|---|---|
| 5 sequential project fetches with `time.sleep(2)` between | All 200 in 0.5-2s response time; no throttling |
| Mixed: project + author + tag fetches at ~3s intervals | All 200 |
| No 429s observed |  |

Recommended rate (in `config.DIVISARE_REQUEST_DELAY_SECONDS`): **3.0 sec**. Conservative for paid-member crawl; can tune down if no throttling observed at scale.

## Tag taxonomy notes

- Tag links from a single project page: 2 (typology + size)
- Tag links from the global homepage / nav: 693 unique flat slugs (e.g. `/aarhus`, `/airports`, `/african-houses`, `/alvaro-siza-time-is-the-best-architect`, …)
- Mix of: cities (`/aarhus`), typologies (`/airports`, `/apartment-blocks`), sizes (`/extra-small`), albums (`/atlas-of-architecture`), curated themes (`/alvaro-siza-time-is-the-best-architect`)
- Many tags appear to be auto-generated cities or typology buckets; others are curated essays/exhibitions
- Phase 1 will harvest tag membership PER PROJECT (each project page reveals its specific tags) rather than enumerate all tags upfront

## Mapping to make_db's existing vocabulary (`vocab.py`)

To define in Phase 2 (`vocab.DIVISARE_TYPOLOGY_TO_PROGRAM`). Initial guesses based on observed tags:

| Divisare tag | → make_db `program` |
|---|---|
| Chapels | Religion |
| Apartment Blocks | Housing |
| Airports | Transport |
| Administrative Centers | Public |
| African Houses | Housing |
| (etc.) | (build dictionary as we encounter tags) |

The mapping table grows as we crawl. `vocab.py` audit log records every typology→program rewrite.

## Open questions for Phase 1

1. **Pagination cap** — does `/authors/{id}/projects/built?page=999` 404 or return empty? Use to bound the walk.
2. **Multi-architect projects** — the sidebar shows "Designer" once. If a project has multiple, are there multiple `<a>` inside `.designers`, or multiple `.section[Designer]` blocks? Untested; check on a co-designed project (e.g., Serie + Multiply Architects collab).
3. **Total scope estimate** — by walking `/designers/{region}` indexes once, count total designers. Multiply by avg projects per designer (~10-30). Calibrate the Phase 5 cost gate.
4. **Architect ID stability** — the IDs (`2144754813-s-ar`) look like timestamps. Are they truly stable across years? Spot-check by re-fetching after a few months. (Low priority — assume stable for now.)
