# Divisare + Architizer shared E1 full

## Status

- state: `COMPLETE`
- launched: `2026-08-07T16:20:40+09:00`
- Divisare terminal validation completed: `2026-08-10T08:01:58+09:00`
- Architizer terminal validation completed: `2026-08-10T07:37:44+09:00`
- external validation and resume-zero closeout completed:
  `2026-08-10T08:25:41+09:00`
- runner commit: `3ac03cb01be9f3e93b655cdc494dd0a38243b262`
- mode: two source-specific processes in parallel
- per source: `8 workers`, site-wide `6 requests/second`
- retry: max attempts `3`
- circuit breaker: `8` consecutive 429/5xx
- overload cooldown: `30 seconds` plus `Retry-After`
- Vision/LLM calls: `0`
- downloaded image retention: `0`

## Inputs

### Divisare

- DB: `data/curated/divisare_metadata_v2_4.db`
- bytes: `2,225,299,456`
- SHA-256:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- inventory: `547,252 = 547,229 eligible + 23 excluded`

### Architizer

- DB: `data/curated/architizer_curated_v2_0.db`
- bytes: `8,767,438,848`
- SHA-256:
  `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc`
- inventory: `884,773 = 884,317 eligible + 456 excluded`

Both inputs had no WAL, SHM, or journal sidecar at launch.

## Rate calibration

The original 4-worker/2-rps full point estimates were too slow. No runner or
fingerprint contract was changed; only existing bounded runtime controls were
calibrated.

| Source | N100 setting | Result | 429 | 5xx | Retry | Effective rate |
|---|---|---:|---:|---:|---:|---:|
| Divisare | 8 workers / 4 rps | 100/100 | 0 | 0 | 0 | 2.109 rps |
| Architizer | 8 workers / 4 rps | 100/100 | 0 | 0 | 0 | 3.017 rps |
| Divisare | 8 workers / 6 rps | 100/100 | 0 | 0 | 0 | 2.604 rps |
| Architizer | 8 workers / 6 rps | 100/100 | 0 | 0 | 0 | 3.990 rps |

The 6-rps samples used a new deterministic seed and both completed all required
runner validations with unchanged source SHA.

## Runtime artifacts

### Divisare

- output:
  `data/enrichment/divisare_image_fingerprints_e1_full_v1.db`
- stdout: `data/enrichment/logs/divisare_e1_full_v1.stdout.log`
- stderr: `data/enrichment/logs/divisare_e1_full_v1.stderr.log`
- launcher PID: `45424`
- interpreter PID at launch: `29952`

### Architizer

- output:
  `data/enrichment/architizer_image_fingerprints_e1_full_v1.db`
- stdout: `data/enrichment/logs/architizer_e1_full_v1.stdout.log`
- stderr: `data/enrichment/logs/architizer_e1_full_v1.stderr.log`
- launcher PID: `28880`
- interpreter PID at launch: `65668`

The `.db.lock` files were confirmed OS-locked after launch. The database is
written as `.db.partial` and published without clobber only after terminal
validation.

## Updated estimate

- Divisare rate6 point estimate: `58.4 hours`
- Architizer rate6 point estimate: `61.6 hours`
- parallel planning range: `64-72 hours` (`2.7-3.0 days`)
- expected persistent sidecars: about `7.634 GB` combined
- expected response transfer: about `176 GB`; response bytes are discarded
- LLM/Vision tokens and API cost: `0`

The machine had 20 logical CPUs, 33.58 GB RAM, and 697.75 GB free on C: before
launch. AC sleep is disabled. Windows Update can still reboot outside active
hours, so resume is part of the operating plan.

## Recovery commands

Run only after confirming the corresponding writer process and OS lock are no
longer active.

```powershell
.\.venv\Scripts\python.exe tools/run_image_fingerprints.py --source divisare --source-db data/curated/divisare_metadata_v2_4.db --output data/enrichment/divisare_image_fingerprints_e1_full_v1.db --n full --workers 8 --requests-per-second 6 --max-attempts 3 --circuit-breaker-threshold 8 --cooldown-seconds 30 --resume

.\.venv\Scripts\python.exe tools/run_image_fingerprints.py --source architizer --source-db data/curated/architizer_curated_v2_0.db --output data/enrichment/architizer_image_fingerprints_e1_full_v1.db --n full --workers 8 --requests-per-second 6 --max-attempts 3 --circuit-breaker-threshold 8 --cooldown-seconds 30 --resume
```

If a circuit breaker opens, preserve pending rows and resume at a lower
`--requests-per-second` value. Do not delete or overwrite partial sidecars.

## Completion gate

For each source:

1. background process exits successfully;
2. final `.db` exists and `.partial` is absent;
3. independent validator passes;
4. SQLite quick/integrity/FK checks pass;
5. source DB SHA before and after is unchanged;
6. complete-run resume makes zero network requests;
7. failure and exclusion accounting is documented.

## Final result

Both source runs published immutable final databases with
`complete_with_failures` status. All required built-in validations and a
separate external validator invocation passed. Complete-run resume returned
`already_complete=true` and `network_requests=0` for both sources.

### Divisare

- final DB:
  `data/enrichment/divisare_image_fingerprints_e1_full_v1.db`
- bytes: `2,785,714,176`
- SHA-256:
  `2a048548afee92d7b222655682a3082ddba535778772b200c674efc6523b1919`
- selected: `547,229`
- success: `544,915`
- failed: `2,314`
  - `http_404`: `2,261`
  - `decode:decode`: `52`
  - `response_too_large`: `1`
- HTTP attempts: `547,230`
- retries: `1`
- response bytes: `51,389,120,776`
- HTTP 429 / 5xx: `0 / 0`
- ordered selection manifest SHA-256:
  `9153184643c9c42929c854c15f02bafc3f5a5f902546453c4535530eb6e5bf4b`
- independent validator: pass
- quick / integrity / FK: `ok / ok / 0`
- input SHA before and after: unchanged
- completed resume network requests: `0`

### Architizer

- final DB:
  `data/enrichment/architizer_image_fingerprints_e1_full_v1.db`
- bytes: `4,424,044,544`
- SHA-256:
  `6e9c13c2f2265f56cc6fbbaa55a83b0c275d571fe9e0034d300faf0d36c3889c`
- selected: `884,317`
- success: `884,248`
- failed: `69`
  - `empty_response`: `52`
  - `http_422`: `13`
  - `decode:decode`: `3`
  - `http_424`: `1`
- HTTP attempts: `884,331`
- retries: `14`
- response bytes: `136,254,608,403`
- HTTP 429 / 5xx: `0 / 6`; the 5xx attempts did not remain terminal failures
- ordered selection manifest SHA-256:
  `69b7c70d4d269643d7a73a144688c6097e76f96e1009cf9b736804fa3bc7ddeb`
- independent validator: pass
- quick / integrity / FK: `ok / ok / 0`
- input SHA before and after: unchanged
- completed resume network requests: `0`

### Combined accounting

- selected: `1,431,546`
- success: `1,429,163`
- failed: `2,383`
- HTTP attempts: `1,431,561`
- retries beyond first attempt: `15`
- response transfer: `187,643,729,179 bytes`
- final sidecar bytes: `7,209,758,720`
- final `.partial`, WAL, SHM, journal: none
- active writer / held OS lock after closeout: none
- retained downloaded image files: none
