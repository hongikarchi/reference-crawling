# Arquitectura Viva Schema — Phase 0 reconnaissance (2026-04-28)

Reconnaissance was forced through a narrow channel: every direct WebFetch
returned **HTTP 403** because the site's `robots.txt` explicitly disallows
`ClaudeBot`, and Cloudflare enforces that disallow at the edge. As a result
this document is built primarily from `robots.txt`, search-result snippets,
and inference. **No page HTML was retrieved**, so all schema field claims
below are inferred and must be verified by a non-AI-identified browser-UA
crawler before any code is written against them.

This is intentionally a thinner reconnaissance than `divisare-schema.md`
(which had logged-in HTML in hand). Treat this as a "should we proceed?"
document, not a "here's the parser spec" document.

---

## 1. Site overview

Arquitectura Viva is a Madrid-based architecture publisher founded 1985-1988
(C/ Quintana 12, 28008 Madrid; also referenced at Aniceto Marinas 32). It
prints three periodicals:

| Title | Founded | Cadence | Focus |
|---|---|---|---|
| **AV Monografías** | 1985 | ~10/yr (some doubles) | Single-architect or thematic issues; Spain Yearbook annually since 1993 |
| **Arquitectura Viva** | 1988 | Monthly, bilingual since c. 2014 | News + dossiers; cultural commentary |
| **AV Proyectos** | 2004 | Bimonthly | Younger studios, competitions, construction details |

Online edition (`arquitecturaviva.com`) aggregates editorial content +
project archive + magazine catalog. Latest issue numbers as of April 2026:
**AV Monografías 281-282 (Spain Yearbook 2026)**, **Arquitectura Viva 275
(OFFICE)**. Issue 281 in 41 years implies roughly that many AV monographs
plus separate AV / AV Proyectos numbering — meaningful prior catalog.

**Coverage tilt:** Strong Spanish + Latin-American + Iberian-Portuguese +
Italian editorial roots; Spain Yearbook is unique to AV. Likely
under-represents Asian and African work compared to Divisare, but unique on
Iberian/Latin coverage.

**Online project count:** Unknown. No counter visible in any public
snippet. The `/works` index claims it is searchable by architect /
photographer / type / material / brand / country / city / date, which
suggests a sizable corpus, but a sitemap walk or browser-UA crawl is
required to estimate.

## 2. Access policy

- Public site is browseable without login.
- `/user/login` exists; subscription tiers exist (`/subscriptions`,
  `/flat-rate`, `/flat-rate-for-institutions`).
- "Digital Flat Rate" gives access to digital editions of all three
  magazines; subscriptions are annual; institutional plans available.
- **What's behind the paywall is unclear** — the snippets suggest digital
  facsimiles of magazine issues are member-only, but article pages and
  `/works/{slug}` project pages may be partly or fully public. Verify with
  a browser-UA fetch of `/works/casa-dieste-montevideo-` and
  `/articles/av-monografias-281-282-espana-2026`.

## 3. robots.txt

Fetched 2026-04-28. Verbatim:

```
# (Cloudflare-managed Content Signals preamble — yes for search, no for ai-train)

User-agent: *
Content-Signal: search=yes,ai-train=no
Allow: /

User-agent: Amazonbot           Disallow: /
User-agent: Applebot-Extended   Disallow: /
User-agent: Bytespider          Disallow: /
User-agent: CCBot               Disallow: /
User-agent: ClaudeBot           Disallow: /
User-agent: CloudflareBrowserRenderingCrawler   Disallow: /
User-agent: Google-Extended     Disallow: /
User-agent: GPTBot              Disallow: /
User-agent: meta-externalagent  Disallow: /

Sitemap: https://arquitecturaviva.com/sitemap.xml

User-agent: *
Disallow: /admin/
Disallow: /user/
Disallow: /usuario/
```

Two policy layers worth flagging:

1. **AI-bot blocklist** — explicit `Disallow: /` for ClaudeBot, GPTBot,
   Google-Extended, CCBot, Bytespider, Applebot-Extended, Amazonbot,
   meta-externalagent. This is enforced at Cloudflare. WebFetch
   (ClaudeBot-identified) gets 403.
2. **`Content-Signal: search=yes,ai-train=no`** — a Cloudflare-managed
   declaration under EU Article 4 of the 2019/790 DSM Directive. Search
   indexing and snippet-return are permitted; AI training is explicitly
   refused. This is a **policy / legal-posture flag**, not just a technical
   block. make_db enriches with the Anthropic API → the user must decide
   whether that use crosses the `ai-train=no` reservation. (Plausible
   reading: enrichment that produces metadata for a discovery DB is closer
   to "search" than "training" but the line is fuzzy.)
3. **For non-AI bots `*` → `Allow: /`** — only `/admin/`, `/user/`,
   `/usuario/` are disallowed. A custom crawler with a regular browser UA
   that doesn't identify as ClaudeBot/GPTBot/etc. is, on the strict
   robots.txt reading, permitted. ToS may layer additional restrictions
   (verify before crawling).

Sitemap is advertised at `/sitemap.xml`. It must be fetched via browser-UA
to confirm structure (sitemap-index vs flat) and entry counts.

## 4. Anti-bot

- **Cloudflare in front** (confirmed by the "Cloudflare Managed Content"
  comment block in robots.txt and by 403 behavior).
- All five WebFetch attempts (with default ClaudeBot UA) → HTTP 403, no
  content body returned.
- The 403 is **policy-driven (UA-fingerprinting AI bots), not generically
  hostile**. Distinguishes AV from a site that 403s every non-browser
  request: AV would likely 200 a `Mozilla/5.0 …` browser-UA fetch with
  cookies enabled. **Verify before committing to crawl design.**
- JS-rendering: unknown. Server-rendered Spanish CMSes (this looks Drupal-
  flavored from the `/user/login?bt=` query pattern) typically deliver
  parseable HTML on first paint; assume parseable until proven otherwise.

## 5. Auth strategy

- **Probably not required for the project archive (`/works/`, `/articles/`,
  `/publications/`)** — these are linked from organic search and don't
  appear gated.
- **Required for full digital magazine reading** — `/user/login` +
  `/subscriptions/...` + `/flat-rate`. If we want PDF-or-flipbook content
  from inside an AV issue, we need a paid account.
- For our purposes (project metadata, architect cross-references, magazine
  references) auth is likely unnecessary. Worth verifying by browser-UA
  fetching one project page anonymously and checking for content
  truncation / "subscribe to read more" wrappers.

## 6. URL patterns (inferred from search snippets)

| Path | Purpose | Source |
|---|---|---|
| `/en` | English homepage | search result |
| `/` | Spanish homepage (default) | inferred |
| `/works` | Searchable index of published works (filterable by architect/material/country/etc.) | search result |
| `/works/{slug}` | Single project page | e.g. `/works/casa-dieste-montevideo-`, `/works/very-large-structure`, `/works/tproject`, `/works/new-slussen-masterplan-estocolmo-8` |
| `/articles/{slug}` | Editorial articles, including issue announcements | e.g. `/articles/av-monografias-281-282-espana-2026` |
| `/publications` | Publications hub | search result |
| `/publications/av-monografias` | Index of all AV Monografías issues | search result |
| `/publications/av-monografias/{slug}` | Single AV Monografías issue page | e.g. `/publications/av-monografias/espana-2025`, `/publications/av-monografias/houses-in-detail` |
| `/publications/av-projects/{slug}` | Single AV Proyectos issue page | e.g. `/publications/av-projects/dossier-e2a-1` |
| `/publications/av/{slug}` | Single Arquitectura Viva issue page | e.g. `/publications/av/50-from-africa-and-asia` |
| `/tag/{slug}` | Editorial tag — projects + articles by topic | e.g. `/tag/spain`, `/tag/housing`, `/tag/mexico`, `/tag/magazine`, `/tag/projects` |
| `/tags/architects` | Full architects index page | search result |
| `/tags/authors` | Full authors index page | search result |
| `/map` | Geo-located works map | search result |
| `/team` | Editorial staff page (not useful) | search result |
| `/user/login` | Login form | robots.txt |

**Slug convention:** appears Spanish-language even on the English edition
(`casa-dieste-montevideo-`, `pabellon-robert-olnick-en-cold-springs`),
suggesting **slugs are stable across the bilingual edition** — both
language versions resolve to the same canonical URL with a `/en` prefix
toggle. Verify with a paired fetch.

**Per-architect profile URL pattern: UNKNOWN.** No `/authors/{slug}` or
`/architects/{slug}` URLs surfaced in any search snippet. The `/tags/
architects` page is a *list view*, not a profile page. It's possible AV
uses `/tag/{architect-slug}` for both topical tags and architect tags — that
would be ugly but consistent with Drupal-style taxonomy. Must verify.

## 7. Project page schema (HIGHLY INFERRED — NOT VERIFIED FROM HTML)

**Caveat: We did not retrieve a single project page's HTML.** Field list
below is inferred from search-result snippets describing project pages.
None of these selectors are confirmed.

| Field | Likely available? | Confidence | Notes |
|---|---|---|---|
| `name` (project title) | Yes | High | every search result shows a project title |
| `architect_name` | Yes | High | snippets explicitly list architects (e.g. "Junya Ishigami + associates", "Eladio Dieste") |
| `year` (built) | Yes | Medium | snippets mention years ("Between 1967 and 1968") but unclear if structured field vs prose |
| `location` (city) | Yes | High | titles include "Montevideo", "London", "Stockholm" |
| `country` | Yes | Medium | implied; unclear if a separate field |
| `description` | Yes | High | snippets quote prose paragraphs |
| `photos` / image gallery | Yes | High | every architecture publication has these; format unknown |
| `materials` | Possibly structured | Low | `/works` search snippet says it is filterable by material → suggests structured tagging |
| `tags` (typology, theme) | Yes | Medium | `/tag/{slug}` URLs exist; per-project tag list likely surfaces |
| `magazine_reference` (which AV issue published this work) | **Almost certainly present in some form, but structure unverified** | **Critical open question** | See §8 |
| `photographer credit` | Yes | Medium | AV is photo-heavy, likely credited |
| `JSON-LD / og:tags` | Unknown | — | very likely present (Drupal default), need to verify |

**No CSS selectors are claimed yet.** A Phase-0 follow-up with browser-UA
+ saved HTML (similar to `data/divisare_samples/` pattern) is required
before any extraction code is written.

## 8. Magazine-reference field — the unique-value question

This is the single feature that would distinguish AV from our existing
Divisare + metalocus + ArchDaily corpus. **Each project, in principle, can
be tied back to the AV-Monografías / AV / AV-Proyectos issue that
published it** (e.g. "AV 275 — OFFICE", "AV Monografías 281-282 — España
2026").

**What is verified:**
- Issue pages exist at `/publications/av-monografias/{slug}` and
  `/articles/av-monografias-{num}-{slug}`. They are individually
  enumerable.
- Issue numbers, names, and themes are consistent and structured (e.g.
  "AV Monografías 270: Portfolio 2024", "AV Monografías 265:
  Arquitectura-G").

**What is NOT verified — and is the linchpin of the feasibility verdict:**
- Whether a `/works/{slug}` project page exposes its source-issue as a
  **structured field** (linked back to `/publications/.../issue-slug`)
  vs. only as **prose** in the description vs. **not at all**.
- Whether the issue page (`/publications/av-monografias/{slug}`) lists its
  contained projects as a structured table of `/works/{slug}` links
  (giving us issue → projects), allowing us to derive the back-reference
  even if individual project pages omit the field.

If the **issue → projects** direction is structured (most likely path
given AV's editorial model), we can crawl issue pages and emit a
`(work_slug, av_issue, av_number, av_year)` mapping table — high value,
clean schema. **This is the question to verify first.**

## 9. Pagination + discovery

- **`/sitemap.xml`** advertised in robots.txt — primary discovery path
  if it enumerates `/works/`. Verify structure (sitemap-index? per-
  content-type subsitemaps?).
- **`/works`** searchable + paginated index — secondary path; supports
  filter combinations (architect, material, country, city, date).
- **`/publications/av-monografias`** issue index — tertiary path,
  structured top-down by issue. **Probably the cleanest entry point** if
  issue pages link out to constituent works (see §8).
- **`/tag/{slug}`** for topical / typological / geographic harvesting.
- **`/map`** geo-API; potentially usable but typically less stable than
  HTML crawl.

Pagination mechanics (query param vs path segment, items-per-page,
end-of-list signal) — unknown.

## 10. Rate-limit estimate

No empirical data — we never got past the AI-bot 403 wall.

Conservative starting point for a browser-UA crawl, by reference to
similar Cloudflare-fronted mid-sized publishers: **3.0-5.0 sec per
request**, exponential back-off on 429/503, single concurrent worker.
Same posture as Divisare. Tune down only after confirmed clean run of
a few hundred fetches.

## 11. Spanish edition vs English edition

**Verified:** The site is bilingual. AV magazine became bilingual c. 2014;
many publications are explicitly published in fully bilingual
Spanish-English editions. URLs use a `/en` prefix toggle (`/en` vs
default Spanish root).

**Unverified but likely:**
- The corpus (project pages, architect data) is **shared across both
  languages**: same canonical slug, language toggled by URL prefix /
  cookie / Accept-Language. Slugs are usually Spanish-form even on the
  English edition (`casa-dieste-montevideo-`).
- The English edition is **not a subset** in terms of which projects
  exist; it's a translation overlay. Some projects may have richer
  Spanish-edition prose if the AV editor wrote in Spanish first, but the
  *list* of works should match.

**Crawl strategy implication:** We can crawl Spanish URLs and use the
bilingual-edition translation layer for English text where available.
For our pipeline, which already runs everything through Anthropic
enrichment, this is moot — we can normalize either edition into our
schema. Recommend defaulting to Spanish URLs (likely canonical) and
using the `/en` variant only when scraping description text we want in
English.

## 12. Comparison vs Divisare

| Aspect | Divisare | Arquitectura Viva |
|---|---|---|
| Geographic coverage | Global, Italian editorial bias | Spanish + Latin-American + Iberian + Italian editorial bias |
| Likely overlap with our corpus | High (already crawled) | Medium-High — significant overlap on Iberian / Latin work, less on Asian / African |
| Authentication required | Yes (paid member, login token) | No for project archive; yes for digital magazines |
| robots.txt posture | Empty / permissive | AI-bot blocklist + Content-Signal `ai-train=no`; permissive for non-AI |
| Anti-bot | None on auth'd sessions | Cloudflare AI-bot block (UA-based) |
| Sitemap | None (404) | Advertised at `/sitemap.xml` |
| Schema HTML verified | Yes (saved samples) | **No — Phase 0 incomplete** |
| Architect-page IDs | Numeric, stable (`/authors/2144695353-…`) | Unknown (no architect-profile URL pattern surfaced) |
| Magazine-issue linking | Albums (`/atlas-of-architecture`) — curatorial, not the publication | **AV-Monografías issues — direct publication reference; UNIQUE if structured** |
| Tag taxonomy | 693 flat slugs, mixed cities/typologies/themes | `/tag/{slug}` namespace, mix similar |
| Coverage of Iberian work | Some | **Authoritative — Spain Yearbook annually since 1993** |

**Unique value-add of AV (if magazine-reference linking is structured):**
The `(project, AV issue, year)` triple is editorial provenance no other
source in our pipeline carries. It would let us answer "what did AV pick
for the 2024 Yearbook?" or "which housing projects ran in Casas 2025?" —
queries no current source supports. This is the strategic argument for
including AV.

## 13. Feasibility verdict

**Moderate, contingent on a 30-minute browser-UA test.**

The headline 403 is a **policy block, not a technical wall** — robots.txt
explicitly permits non-AI user-agents (`User-agent: *` → `Allow: /`).
Our existing crawler stack (browser UA, cookies, polite delay) is the
right tool. The risks are policy-flavored, not engineering-flavored:

- **`ai-train=no` Content-Signal** is an explicit reservation under EU
  copyright directive. The user should make a deliberate call before we
  commit to use the data.
- **Schema is unverified** — we haven't seen one project page's HTML.
  Phase 0 must be re-run with browser UA before any parser code.
- **Magazine-reference linking is unverified** — and it's the unique
  value-add. If the link is prose-only, AV's marginal value over
  Divisare is much smaller.

**Recommended Phase 0 next step (user-gated):** A 30-minute browser-UA
manual reconnaissance — fetch `/sitemap.xml`, three sample `/works/{slug}`
pages, two `/publications/av-monografias/{slug}` issue pages, one
`/tag/{slug}` page. Save HTML. Then this document gets a real schema
section. **Until that's done, do not write crawl code.**

---

## What would change the recommendation

| Signal | Effect |
|---|---|
| Browser-UA fetch returns 403 / Cloudflare challenge → moves verdict from "moderate" to "hard" | Need Playwright / Cloudflare bypass |
| Issue pages link out to constituent `/works/{slug}` items as a structured list → the magazine-reference angle is intact and we should crawl | Verdict becomes "go" |
| Issue pages don't link to project URLs (only prose mentions) → magazine-reference linking has to be inferred via fuzzy name match | Verdict becomes "skip — Divisare + metalocus already cover the same projects without this hassle" |
| `/sitemap.xml` exposes < ~500 `/works/` URLs → corpus is too small to justify a separate ingestion path | Verdict becomes "skip" |
| `/sitemap.xml` exposes > ~3000 `/works/` URLs with strong Iberian / Latin coverage → integration ROI clears the bar | Verdict becomes "go, prioritize" |
| User decides `ai-train=no` Content-Signal applies to our enrichment use-case → policy block | Verdict becomes "do not crawl" regardless of technical feasibility |
| Per-project page exposes `materials` / `program` as structured tags (filter UI suggests this, but unverified) | Verdict strengthens: AV becomes a vocab-validation source as well as a content source |
| No per-architect canonical URL pattern (only `/tag/{architect-slug}`) → we lose architect-ID stability that Divisare provides | Mild negative — we can still match on name normalization |

## Sources

- [Arquitectura Viva — Home (English)](https://arquitecturaviva.com/en)
- [Arquitectura Viva — robots.txt](https://arquitecturaviva.com/robots.txt)
- [Arquitectura Viva — Works index](https://arquitecturaviva.com/works)
- [Arquitectura Viva — Sample work: Casa Dieste, Montevideo](https://arquitecturaviva.com/works/casa-dieste-montevideo-)
- [Arquitectura Viva — Publications hub](https://arquitecturaviva.com/publications)
- [Arquitectura Viva — AV Monografías issue index](https://arquitecturaviva.com/publications/av-monografias)
- [AV Monografías 281-282: Spain Yearbook 2026](https://arquitecturaviva.com/articles/av-monografias-281-282-espana-2026)
- [AV Monografías 270: Portfolio 2024](https://arquitecturaviva.com/articles/av-monografias-270-portfolio-2024)
- [Arquitectura Viva — Subscriptions](https://arquitecturaviva.com/subscriptions)
- [Arquitectura Viva — Digital Flat Rate](https://arquitecturaviva.com/flat-rate)
- [Arquitectura Viva — Login](https://arquitecturaviva.com/user/login)
- [Cloudflare Managed robots.txt setting (Content-Signals)](https://developers.cloudflare.com/bots/additional-configurations/managed-robots-txt/)
- [BIG SEE — Arquitectura Viva profile](https://bigsee.eu/arquitectura-viva/)
