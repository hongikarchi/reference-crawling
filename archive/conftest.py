# Archived code (2026-10-16 cleanup). Its tests import modules that were moved
# alongside them and are not importable from the live package layout, so a plain
# `pytest` from the repo root must not collect anything under archive/.
collect_ignore_glob = ["*"]
