# 20261019 vNext asset refresh ledger: Architizer new-host probe (N20 + N172)

- **Trigger:** user "이제 서버 작업 다시 이어서 진행해보자" (continue vNext). The S2/P1 full expansion needs the
  asset refresh ledger (WP1.1 order) and the Architizer host moved (imgix 410 -> images.architizer.com).
- **Scope:** read-only E1/E2/gallery DBs + HTTP GET to images.architizer.com. No Neon/R2/LLM. Cost $0.
  Scripts/outputs in session scratchpad (`probe/probe.py`, `probe/n100.py`, `probe/n100.jsonl`); not artifacts.

## Survey (3 read-only agents)
- All 884,317 E1 Architizer assets (884,122 unique gallery assets) were fetched from
  `architizer-prod.imgix.net...?auto=compress&fit=max&fm=jpg&h=1024&q=85&w=1024` -> every one needs a refresh,
  not only the WP1.1 "drifted" 24%. Divisare (images.divisare.com): no measured drift; only 404s.
- `source_asset_id` hashes the old host into the key -> keep frozen IDs, map the host; never re-key.
- `vnext_gallery_verified_pixels.py` fetches frozen E1 `fetch_url` (would 410) and `cross_source_semantic_fetch.SOURCE_HOSTS`
  allowlists only the imgix host; `source_changed` bodies and pHash are discarded. Host mapper exists in
  `canonical/vnext_identity_registry.py` (`HOST_MIGRATIONS`, `migrate_image_url`).

## Probe results
- Profile `?width=1024&height=1024&fit=max&quality=85&format=jpg` on images.architizer.com reproduces E1 dimensions
  (172/172 within 1px). Plain `width=1024&quality=85` upsizes portrait images and sometimes returns PNG -> rejected.
- N172 = 100 hash-seeded random + all 72 Architizer rows of WP1 N10 (55 `exact` + 17 `source_changed` in Aug):
  - HTTP 200: 172/172. Avg 154 KB, ~1.3 s/request sequential.
  - normalized pixel SHA equal to E1: **0/172** (new encoder) -> SHA continuity is impossible for Architizer.
  - pHash-256 Hamming new vs E1: random max 24 (91% <= 8); aug_exact max 18; aug_source_changed max 18 (5 at 0).
    -> the Aug "source_changed" 17 were re-encodes of the same picture, not content changes.
  - Distinct-pixel siblings in the same building: min distance often 0-6 (near-duplicate double posts), typical ~100+.
    So pHash cannot separate an asset from its near-duplicate siblings, but continuity only asks whether the
    same URL path still serves the same picture, so this does not matter.

## Design implications (proposal, pending user decisions)
- Ledger `vnext_asset_refresh_v1`: per (source, source_asset_id, fetch_profile) a new pixel version (raw SHA,
  pixel SHA, pHash, dims, request URL, host-migration provenance). Continuity = same asset ID + dims within 1px
  + pHash <= T (T proposed 32/256) -> `reencode_continuous`; otherwise `content_changed` (excluded from
  grouping/serving until reviewed). Frozen E1 SHA classes stay the grouping evidence; the ledger supplies
  verification + pixels. Needs a contract §2 revision.
- Full Architizer pass: ~884k GETs, ~136 GB transfer, ~41 h at 6 rps (~61 h at 4 rps); 1024 derivative cache ~160 GB.
  Disk free 362 GB.
