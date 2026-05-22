#!/usr/bin/env python3
"""Build the completeness_c9 canonical artifact — post-audit data corrections.

Applies three deterministic corrections to the c8 embedded artifact (audit
2026-05, data/reports/db_quality_audit.md):
  1a. image_derived.style/.color_tone -> v2 vocab (tools/normalize_image_derived)
  1b. garbage `name` rows -> append 'name_needs_review' to publishability_reasons
      (never fabricates a name)
  1c. project_year < 1850 -> NULL (contemporary-architecture DB; the audit
      confirmed these are 4-digit numbers lifted from prose, not project years)

Streaming I/O — never materializes the 1.5 GB artifact. Writes c9 embedded +
c9 strict + a c9 affected-rows file (loader-ready) + an apply report.
Read-only w.r.t. Neon.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402
from tools.normalize_image_derived import normalize_image_derived  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
C8 = CCR / "canonical_buildings_strict_embedded.completeness_c8.json"
OUT_EMB = CCR / "canonical_buildings_strict_embedded.completeness_c9.json"
OUT_STRICT = CCR / "canonical_buildings_strict.completeness_c9.json"
OUT_AFFECTED = CCR / "canonical_buildings_strict_embedded.completeness_c9_affected.json"
REPORT = ROOT / "data/reports/canonical_v2_completeness_c9_apply_report.json"

YEAR_FLOOR = 1850
_LOC_NAME = re.compile(r"^[A-Z][A-Za-z .'\-]+ - [A-Z][A-Za-z .'\-]+$")
_JUNK_NAMES = {"test", "arch a", "social dwellings", "untitled"}


def is_garbage_name(name, city, country) -> bool:
    if not name or not str(name).strip():
        return True
    n = str(name).strip()
    if n.casefold() in _JUNK_NAMES:
        return True
    if _LOC_NAME.match(n) and not city and not country:
        return True
    if len(n) > 120:
        return True
    return False


def main() -> int:
    if not C8.exists():
        print(f"FATAL: c8 artifact missing: {C8}", file=sys.stderr)
        return 2

    counts = {"rows_total": 0, "image_derived_normalized": 0,
              "names_flagged": 0, "years_nulled": 0, "affected_rows": 0}
    flagged_names: list[dict] = []
    nulled_years: list[dict] = []

    f_emb = OUT_EMB.open("w", encoding="utf-8")
    f_str = OUT_STRICT.open("w", encoding="utf-8")
    f_aff = OUT_AFFECTED.open("w", encoding="utf-8")
    for f in (f_emb, f_str, f_aff):
        f.write('{"buildings":[')
    n_emb = n_str = n_aff = 0

    try:
        for row in iter_buildings(C8):
            counts["rows_total"] += 1
            cid = row.get("canonical_bld_id")
            changed = False

            # 1a — image_derived vocab normalization
            idv = row.get("image_derived")
            if isinstance(idv, dict):
                new_idv, ch = normalize_image_derived(idv)
                if ch:
                    row["image_derived"] = new_idv
                    counts["image_derived_normalized"] += 1
                    changed = True

            # 1b — garbage name flag (never fabricates)
            if is_garbage_name(row.get("name"), row.get("location_city"),
                               row.get("location_country")):
                reasons = list(row.get("publishability_reasons") or [])
                if "name_needs_review" not in reasons:
                    reasons.append("name_needs_review")
                    row["publishability_reasons"] = reasons
                    counts["names_flagged"] += 1
                    flagged_names.append({"canonical_bld_id": cid, "name": row.get("name")})
                    changed = True

            # 1c — implausible project_year (< 1850) NOT corroborated by the
            # name -> NULL. A year that also appears in the name is treated as
            # a genuine date (e.g. "Palm House, 1848"), not a prose-grab.
            py = row.get("project_year")
            if (isinstance(py, int) and py < YEAR_FLOOR
                    and str(py) not in str(row.get("name") or "")):
                nulled_years.append({"canonical_bld_id": cid, "old_project_year": py,
                                     "name": row.get("name")})
                row["project_year"] = None
                counts["years_nulled"] += 1
                changed = True

            blob = json.dumps(row, ensure_ascii=False)
            f_emb.write(("," if n_emb else "") + blob)
            n_emb += 1
            strict_row = {k: v for k, v in row.items() if k != "embedding"}
            f_str.write(("," if n_str else "") + json.dumps(strict_row, ensure_ascii=False))
            n_str += 1
            if changed:
                f_aff.write(("," if n_aff else "") + blob)
                n_aff += 1
    finally:
        for f in (f_emb, f_str, f_aff):
            f.write("]}")
            f.close()

    counts["affected_rows"] = n_aff
    report = {
        "status": "PASS",
        "base": str(C8.relative_to(ROOT)),
        "outputs": {
            "embedded": str(OUT_EMB.relative_to(ROOT)),
            "strict": str(OUT_STRICT.relative_to(ROOT)),
            "affected": str(OUT_AFFECTED.relative_to(ROOT)),
        },
        "counts": counts,
        "flagged_name_rows": flagged_names,
        "nulled_year_rows": nulled_years,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "counts": counts,
                      "names_flagged_sample": flagged_names[:10],
                      "years_nulled": nulled_years}, ensure_ascii=False, indent=2))
    print(f"report: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
