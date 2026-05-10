# dispatch_enrich_batch Surface Assignments

Keep enrichment stages pinned to separate Codex surfaces so scrollback,
model footer state, and token baselines do not mix across stages.

- D-1: `enricher:9`
- D-2: `enricher:28`
- E-2: `enricher:27`

Do not dispatch a stage to a different surface unless DB-MAIN records an
explicit handoff approving the context move.

D-1 resume launches should pass the surface explicitly:

```bash
python3 -m tools.dispatch_enrich_batch --stage d1 --tab enricher:9
```
