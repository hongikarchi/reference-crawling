# Parallel source-curation handoff

## Objective

Publish the completed Divisare source-specific implementation and a safe
handoff contract so a second PC can build the Architizer curated SQLite in
parallel without touching the active Divisare recrawl.

## Included

- Track the Divisare curated v1/v1.5 and metadata v2.1 implementation
- Track the resumable Divisare HTML recrawler v2.4.1 and tests
- Update the running recrawl checkpoint and safety notes
- Add the two-PC ownership and data-transfer contract
- Add a paste-ready Architizer Codex prompt
- Commit and push the checkpoint on `main`
- Tag the checkpoint as `handoff-divisare-20260731`

## Excluded

- Copying or committing any SQLite, WAL/SHM, HTML snapshot, log, image or secret
- Starting another Divisare crawler
- Architizer implementation or artifact creation on this PC
- pHash, image semantics, cross-site matching, vector DB, Neon or R2 work
- Unrelated July 13 Neon/R4/material/junk worktree files

## Runtime checkpoint

At `2026-07-31 00:21:05 KST`:

- Divisare queue: `29,955`
- Fetch: success `11,635`, pending `18,308`, running `1`, failed `10`,
  not_found `1`
- Parse: `11,167 success / 15 partial / 453 no_content / 0 failed`
- Active run: `run_id=6`, no current error
- Crawler/parser: `v2.4.1 / v2.3`
- Remaining estimate: approximately `15.3 hours`
- External API/LLM cost: `$0`

## Architizer input manifest

- Dropbox source: `data/crawl/architizer.db` in the migration bundle
- Size: `90,918,912 bytes`
- SHA-256:
  `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985`
- Git policy: input and generated artifacts remain ignored

## Validation

- Divisare regression suite: `51 / 51 PASS` with `unittest`
- Divisare checkpoint `git diff --cached --check`: PASS
- Remote main/tag publication: performed after this document is committed

Final commit IDs and push results are reported in the interactive completion
message.
