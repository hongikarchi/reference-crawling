# 20261016 local data slimming (data_cold)

- **Trigger:** user: "푸시해줘. 그리고 감사하고 있을테니까, 그동안 대용량 로컬 데이터 정리 좀 하고 있어봐" (while auditing WP2.9-R merge pairs).
- **Scope:** MOVE ONLY (no deletes) of cold `data/` items to `C:/Users/user/reference-crawling_archive_20261016/data_cold/<repo path>`, same convention as the morning pass (653 items / ~75 GB in `data_superseded/`, `scratch/`, etc.). No Neon / R2 / LLM. Untouched: `tools/`, `data/enrichment/vnext_identity_registry_v1.db`, `data/reports/vnext_identity_registry_v1*`, `data/id_registry*.json`.
- **Plan:** `data_cold_PLAN.tsv` drafted 380 rows / 17.60 GB, graded [A] no ref / [B] finished-builder default path. Challenge pass (grep over code/docs/job cards + SHA-256 match vs kept JSONs, case-insensitive) removed 48 rows / 8.65 GB: architizer recrawl DB + HTML snapshots (raw crawl), ID-matching probe chain, semantic vision v1.x/n100 set (pinned by `tools/build_current_image_handoff_manifest.py`), files pinned by `architizer_curated_v2_0.db.READY.json` and v2_7_2 terminal pins.
- **Executed:** 332/332 moved OK, 8,953,081,720 B. 0 skipped, 0 locked, 0 moved back. Log `.move_log_data_cold.tsv`; MANIFEST.md `data_cold` section has restore commands.
  - enrichment 114 (7.24 GB), curated 11 (1.36 GB), reports 183 (0.35 GB), smoke 24 (0.01 GB).
- **data/ size:** 61.97 GB -> 53.02 GB (enrichment 46.92->39.68, curated 13.04->11.68, reports 1.18->0.83, smoke 0.01->0).
- **Verification:**
  - `pytest tests/ -q`: 1290 passed, 23 skipped (skips = symlink privilege + artifacts never on this machine: `data/review/*`, `divisare_metadata_v2_2.db`).
  - `tools/validate_vnext_identity_registry.py --stage c` (run via a scratchpad wrapper that redirects its `write()` so pinned outputs and `r_FAIL.json` are not created): PASS, 2025 checks.
  - `--stage b` (same wrapper): FAIL at check 9 `sha_pin:canonical/vnext_identity_registry.py` - pre-existing: manifest pins the 48,605 B version; committed file (f99cdcb, 15:29 KST) is 50,640 B; the pinned stage_b PASS was 15:16 KST. Not caused by the move; none of the 332 moved paths is referenced by registry reports/manifests/validator/producer. A run waiving just that check was blocked by the permission classifier and not pursued. Decision for user: re-pin after the audit.
  - `tools/canonical_v2_neon_loader.py --help`: exit 0.
  - Known: `tools/validate_vnext_workspace_freeze.py` live-workspace check already failed after the morning pass; this move removes 125 more of its listed paths. `frozen_inputs` unaffected.
- **Docs:** `docs/DATA_LAYOUT.md` updated with actual numbers and verification. Not committed.
- **Cost:** $0 (no LLM / network).
