#!/usr/bin/env python3
"""Generate side-by-side HTML for the 29 COVER_PHASH_SHARED pairs.

For each pair of building rows that share a cover phash, render:
- side A:  cover image, title, architect, year, source, all_images urls
- side B:  same for the duplicate row
- proposed actions: swap_cover_A, swap_cover_B, unpublish_A, unpublish_B, keep

The HTML is static; user marks decisions in a separate CSV. No DB writes here.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from html import escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect  # noqa: E402

CSV_PATH = Path("/private/tmp/make_web_db_audit/docs/make-db-audit-2026-05-27/publishable_issues.csv")
OUT_HTML = ROOT / "data/reports/audit_2026-05-27/cover_phash_review.html"
OUT_DECISIONS_TEMPLATE = ROOT / "data/reports/audit_2026-05-27/cover_phash_decisions_template.csv"


def collect_groups() -> list[list[str]]:
    """Return list of phash-share groups (each group = sorted bid list)."""
    groups: dict[frozenset[str], list[str]] = {}
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["code"] != "COVER_PHASH_SHARED_ACROSS_BUILDINGS":
                continue
            src = row["canonical_bld_id"]
            ev = row["evidence"]
            others_part = ev.split("other_bids=", 1)[-1]
            others = [o.strip() for o in others_part.split(",") if o.strip()]
            members = frozenset([src, *others])
            if members not in groups:
                groups[members] = sorted(members)
    return list(groups.values())


def fetch_rows(conn, bids: list[str]) -> dict[str, dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT canonical_bld_id, name, architects_text, architect_names,
               project_year, year_kind, location_city, location_country,
               program, display_cover_url, cover_image_url_default,
               all_images, source_refs, is_publishable
        FROM canonical_v2_buildings
        WHERE canonical_bld_id = ANY(%s)
        """,
        (bids,),
    )
    cols = [d[0] for d in cur.description]
    out = {}
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        out[d["canonical_bld_id"]] = d
    cur.close()
    return out


def img_thumb(url: str | None, label: str) -> str:
    if not url:
        return f'<div class="img-missing">{escape(label)}: (no image)</div>'
    return (
        f'<figure class="thumb"><img loading="lazy" src="{escape(url)}" alt="{escape(label)}">'
        f'<figcaption>{escape(label)}</figcaption></figure>'
    )


def gallery_alt_url(all_images_json: Any, cover_url: str | None) -> str | None:
    if not all_images_json:
        return None
    imgs = all_images_json if isinstance(all_images_json, list) else json.loads(all_images_json)
    for img in imgs:
        u = img.get("url")
        if u and u != cover_url:
            return u
    return None


def render_side(row: dict[str, Any]) -> str:
    cover = row.get("display_cover_url") or row.get("cover_image_url_default")
    alt = gallery_alt_url(row.get("all_images"), cover)
    archs = row.get("architects_text") or ", ".join(row.get("architect_names") or [])
    src_refs = row.get("source_refs") or {}
    if isinstance(src_refs, str):
        src_refs = json.loads(src_refs)
    src_str = ", ".join(f"{k}:{','.join(v)}" for k, v in src_refs.items())
    pub = "✓" if row.get("is_publishable") else "✗"
    return f"""
    <div class="side">
      <div class="meta">
        <div class="bid"><code>{escape(row['canonical_bld_id'])}</code> (pub={pub})</div>
        <div class="title"><strong>{escape(row.get('name') or '')}</strong></div>
        <div class="archs">{escape(archs)}</div>
        <div class="yr">{escape(str(row.get('project_year') or '?'))} ({escape(row.get('year_kind') or '?')})</div>
        <div class="loc">{escape(row.get('location_city') or '')}, {escape(row.get('location_country') or '')}</div>
        <div class="prog">program: {escape(row.get('program') or '')}</div>
        <div class="src">sources: {escape(src_str)}</div>
      </div>
      <div class="images">
        {img_thumb(cover, 'cover')}
        {img_thumb(alt, 'gallery #2 (alt)')}
      </div>
    </div>
    """


HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Cover phash review — groups</title>
<style>
  body { font: 14px/1.4 system-ui, sans-serif; margin: 24px; background: #f6f6f6; }
  h1 { font-size: 20px; }
  .pair { background: #fff; border: 1px solid #ccc; border-radius: 8px;
          padding: 16px; margin: 16px 0; }
  .pair h2 { font-size: 15px; margin: 0 0 8px; color: #444; }
  .sides { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }
  .side { border: 1px solid #eee; border-radius: 6px; padding: 12px; }
  .meta > div { margin: 2px 0; font-size: 13px; }
  .meta .bid code { background: #eef; padding: 2px 4px; border-radius: 3px; }
  .meta .title { font-size: 14px; }
  .meta .src { color: #666; font-size: 11px; word-break: break-all; }
  .images { display: flex; gap: 8px; margin-top: 8px; }
  .thumb { margin: 0; flex: 1; }
  .thumb img { width: 100%; max-height: 200px; object-fit: cover;
               border: 1px solid #ddd; background: #f0f0f0; }
  .thumb figcaption { font-size: 11px; color: #666; text-align: center; margin-top: 2px; }
  .img-missing { font-size: 12px; color: #c00; padding: 8px; border: 1px dashed #c00; }
  .actions { font-size: 12px; color: #555; margin-top: 8px; padding: 6px;
             background: #f9f9f9; border-radius: 4px; }
  .decision-help { font-size: 12px; color: #444; background: #fffbe6;
                   padding: 8px 12px; border-radius: 4px; margin-bottom: 16px; }
</style>
</head>
<body>
<h1>Cover phash review — groups (publishable rows)</h1>
<div class="decision-help">
Decide per <strong>building</strong> (one row per <code>canonical_bld_id</code> in CSV):
<code>keep</code> (cover OK as-is),
<code>swap_cover</code> (replace display_cover_url with gallery #2),
<code>unpublish</code> (is_publishable=false),
<code>merge</code> (same building as group sibling — flag canonical un-merge, follow-up).
Groups below show all buildings that share a phash so you can compare side-by-side.
</div>
"""

HTML_TAIL = "</body></html>\n"


def main() -> int:
    groups = collect_groups()
    all_bids: set[str] = set()
    for g in groups:
        all_bids.update(g)
    conn = _connect()
    rows = fetch_rows(conn, sorted(all_bids))
    conn.close()

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    parts = [HTML_HEAD]
    bid_to_groups: dict[str, list[int]] = {}
    bid_to_siblings: dict[str, set[str]] = {}
    for idx, group in enumerate(groups, start=1):
        parts.append(
            f'<div class="pair"><h2>Group {idx} ({len(group)} buildings): '
            f'{escape(", ".join(group))}</h2>'
        )
        parts.append('<div class="sides">')
        for bid in group:
            parts.append(render_side(rows.get(bid) or {"canonical_bld_id": bid, "name": "(missing)"}))
            bid_to_groups.setdefault(bid, []).append(idx)
            bid_to_siblings.setdefault(bid, set()).update(b for b in group if b != bid)
        parts.append("</div>")
        parts.append("</div>")

    parts.append(HTML_TAIL)
    OUT_HTML.write_text("".join(parts), encoding="utf-8")

    decisions_rows: list[list[str]] = [
        ["canonical_bld_id", "groups", "name", "siblings", "decision", "notes"]
    ]
    for bid in sorted(bid_to_groups):
        row = rows.get(bid) or {}
        decisions_rows.append([
            bid,
            ",".join(str(g) for g in bid_to_groups[bid]),
            row.get("name") or "",
            ",".join(sorted(bid_to_siblings[bid])),
            "",
            "",
        ])

    with OUT_DECISIONS_TEMPLATE.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerows(decisions_rows)

    print(f"wrote {OUT_HTML} ({len(groups)} groups, {len(all_bids)} unique buildings)")
    print(f"wrote {OUT_DECISIONS_TEMPLATE} ({len(decisions_rows)-1} decision rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
