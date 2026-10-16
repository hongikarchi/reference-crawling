# Semantic Vision N10 v2.2.1 selective continuation

- Date: 2026-08-13
- Scope: new child-only continuation for `semv22_000056..000059`; immutable v2.2 parent retains results 1–55.
- Parent: `data/enrichment/divisare_architizer_semantic_vision_n10_v2_2.db.partial`, 1,286,144 bytes, SHA-256 `21d72c72689a187f773c3e29b3b9d40d5a5f03a75aa2568feba5554fa6020c4b`.
- Safety: acquire parent lock then child lock; no semantic HTTP; one initial four-image call and at most one exact strict-invalid subset retry; never recall accepted parent records; no resume.
- Caps: child output at most 1,752; cumulative output at most 14,000; cumulative input+output at most 312,000; cached input remains a subset and is not double-counted.
- Outputs: child DB 421,888 bytes / SHA-256 `1715ad58bdb63165bdc8dbf227d0628ef73d30fe101d307a1abe82cabcea088f`; report 4,419 bytes / SHA-256 `15f9d625fd3be9ead1591566b736db7aecba6a4fc4a4881611038d1fa89f5ee0`; child cache 4 files / 444,933 bytes / tree SHA-256 `0fb15df1d6963b90c7f04c9d03d08e0848f799f39f71792d993e67ed9aa8d353`.
- Implementation: `canonical/cross_source_semantic_vision_n10_v221_sidecar.py`, independent `canonical/cross_source_semantic_vision_n10_v221_validator.py`, run/validate CLIs, and focused tests.
- Verification before execution: production parent audit and preflight passed; v2.2+v2.2.1 offline suite 51/51 passed.
- Approved execution: one Vision call; 19,341 input, 0 cached-input subset, 921 output, 20,262 input+output tokens. No retry and no semantic HTTP request.
- Combined terminal: 59 results; cumulative 12 calls, 238,889 input, 55,040 cached-input subset, 13,169 output, 252,058 input+output tokens; logical SHA-256 `9e23253a70bff742bb6411455b1a81e7b7eedc09a63128134b1e95c20cf89528`.
- Result: independent 10-check terminal validator passed. Parent bytes remain unchanged at the pinned SHA-256.
