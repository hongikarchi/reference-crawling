#!/usr/bin/env python3
"""Deterministic normalization of `image_derived` style/color_tone to v2 vocab.

The D-2 vision stage wrote `image_derived.style` / `.color_tone` without vocab
enforcement (audit 2026-05: ~9,405 rows out of vocabulary). This module maps
those values back onto `core/vocab.py` with no LLM:

  1. exact vocab match            -> kept
  2. case-only mismatch           -> the correctly-cased vocab term
  3. known non-vocab synonym      -> a fixed remap (modern->Modernist, ...)
  4. anything else                -> None (flagged)

Pure + importable; `normalize_image_derived(d) -> (new_d, changed)`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import vocab  # noqa: E402

_STYLE = {str(v) for v in vocab.STYLE}
_COLOR = {str(v) for v in vocab.COLOR_TONE}
_STYLE_CF = {s.casefold(): s for s in _STYLE}
_COLOR_CF = {c.casefold(): c for c in _COLOR}

# non-vocab terms the D-2 prompt allowed -> nearest v2 vocab term (None = drop)
STYLE_REMAP = {
    "modern": "Modernist",
    "traditional": "Vernacular",
    "classical": "Neo-Classical",
    "other": None,
}
COLOR_REMAP = {
    "earthy": "Earth",
    "colorful": "Vibrant",
    "other": None,
}


def _norm_one(value, exact: set, cf_map: dict, remap: dict):
    """Return (normalized_value, changed)."""
    if value is None:
        return None, False
    if value in exact:
        return value, False
    cf = str(value).strip().casefold()
    if cf in cf_map:
        return cf_map[cf], True          # case-only fix
    if cf in remap:
        return remap[cf], True           # known synonym remap
    return None, True                    # unrecognised -> drop


def normalize_image_derived(image_derived):
    """Normalize `style` and `color_tone`. Returns (new_dict, changed_bool)."""
    if not isinstance(image_derived, dict):
        return image_derived, False
    out = dict(image_derived)
    changed = False
    if "style" in out:
        nv, ch = _norm_one(out["style"], _STYLE, _STYLE_CF, STYLE_REMAP)
        if ch:
            out["style"] = nv
            changed = True
    if "color_tone" in out:
        nv, ch = _norm_one(out["color_tone"], _COLOR, _COLOR_CF, COLOR_REMAP)
        if ch:
            out["color_tone"] = nv
            changed = True
    return out, changed


if __name__ == "__main__":
    # smoke test
    cases = [
        {"style": "Contemporary", "color_tone": "Light"},   # already valid
        {"style": "contemporary", "color_tone": "light"},   # case fix
        {"style": "modern", "color_tone": "earthy"},        # remap
        {"style": "other", "color_tone": "colorful"},       # remap (other->None)
        {"style": "frobnicate"},                            # unknown -> None
    ]
    for c in cases:
        out, ch = normalize_image_derived(c)
        print(f"{c}  ->  {out}  (changed={ch})")
