"""ID registry for canonical entities (architects + buildings).

Provides stable IDs across runs: a cluster matched to an existing entry
keeps its old canonical_id; a new cluster gets a new ID; manual splits/
merges are recorded in a separate log so downstream lookups can follow
redirects.

Usage:
    reg = ArchitectRegistry()           # loads existing or empty
    cid, status = reg.match_or_create(  # 'matched_by_source' | 'matched_by_name' | 'new'
        names={"Foster + Partners"},
        source_refs={"divisare": ["8909"]},
    )
    reg.append_source(cid, "architizer", "foster-partners")
    reg.save()
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Optional


REGISTRY_DIR = "data"
ARCHITECTS_PATH = os.path.join(REGISTRY_DIR, "id_registry_architects.json")
BUILDINGS_PATH  = os.path.join(REGISTRY_DIR, "id_registry_buildings.json")


def _normalize_name(s: str) -> str:
    return s.strip().lower() if s else ""


class _Registry:
    """Base — file-backed dict of canonical_id → entry.

    Entry shape:
      {
        "names":         [str, ...],            # all observed display names
        "source_refs":   {source: [id, ...]},   # which entries from each source
        "first_seen":    "YYYY-MM-DD",
        "last_seen":     "YYYY-MM-DD",
        "redirected_to": str | None,            # if merged into another ID
      }
    """
    id_prefix: str = "ent"

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict] = {}
        self._name_index: dict[str, str] = {}    # normalized_name → canonical_id
        self._source_index: dict[tuple, str] = {} # (source, source_id) → canonical_id
        if os.path.exists(path):
            with open(path) as f:
                self.data = json.load(f)
            self._rebuild_indices()

    def _rebuild_indices(self) -> None:
        self._name_index.clear()
        self._source_index.clear()
        for cid, entry in self.data.items():
            if entry.get("redirected_to"):
                continue
            for n in entry.get("names", []):
                self._name_index[_normalize_name(n)] = cid
            for src, ids in entry.get("source_refs", {}).items():
                for sid in ids:
                    self._source_index[(src, str(sid))] = cid

    def follow(self, cid: str) -> str:
        """Follow redirect chain to terminal canonical_id."""
        seen = set()
        while cid in self.data and self.data[cid].get("redirected_to"):
            if cid in seen:
                break  # cycle guard
            seen.add(cid)
            cid = self.data[cid]["redirected_to"]
        return cid

    def _next_id(self) -> str:
        # Find max existing numeric suffix and +1 (handles existing registry growth)
        max_n = -1
        for cid in self.data:
            try:
                n = int(cid.split("_")[-1])
                if n > max_n:
                    max_n = n
            except (ValueError, IndexError):
                pass
        return f"{self.id_prefix}_{max_n + 1:06d}"

    def match_or_create(
        self,
        *,
        names: set[str],
        source_refs: dict[str, list[str]],
    ) -> tuple[str, str]:
        """Match this candidate against existing registry. Returns (id, status).

        status ∈ {'matched_by_source', 'matched_by_name', 'new'}.

        Match priority:
          1. source_refs overlap (1+ source_id in common with existing entry)
          2. name overlap (1+ exact normalized name match)
          3. new — assign fresh ID + register
        """
        # 1. Source-id overlap (most reliable signal)
        for src, ids in source_refs.items():
            for sid in ids:
                cid = self._source_index.get((src, str(sid)))
                if cid:
                    return self.follow(cid), "matched_by_source"
        # 2. Name overlap (exact normalized match)
        for n in names:
            cid = self._name_index.get(_normalize_name(n))
            if cid:
                return self.follow(cid), "matched_by_name"
        # 3. Brand new
        cid = self._next_id()
        today = date.today().isoformat()
        self.data[cid] = {
            "names":          sorted(names),
            "source_refs":    {src: list(ids) for src, ids in source_refs.items()},
            "first_seen":     today,
            "last_seen":      today,
            "redirected_to":  None,
        }
        for n in names:
            self._name_index[_normalize_name(n)] = cid
        for src, ids in source_refs.items():
            for sid in ids:
                self._source_index[(src, str(sid))] = cid
        return cid, "new"

    def append(self, cid: str, *, names: Optional[set[str]] = None,
               source_refs: Optional[dict[str, list[str]]] = None) -> None:
        """Add new names / source_refs to an existing entry."""
        cid = self.follow(cid)
        entry = self.data[cid]
        entry["last_seen"] = date.today().isoformat()
        if names:
            for n in names:
                if n not in entry["names"]:
                    entry["names"].append(n)
                    self._name_index[_normalize_name(n)] = cid
            entry["names"] = sorted(set(entry["names"]))
        if source_refs:
            for src, ids in source_refs.items():
                existing = set(entry["source_refs"].get(src, []))
                for sid in ids:
                    sid_s = str(sid)
                    if sid_s not in existing:
                        entry["source_refs"].setdefault(src, []).append(sid_s)
                        self._source_index[(src, sid_s)] = cid

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False, sort_keys=True)

    def stats(self) -> dict:
        active = sum(1 for e in self.data.values() if not e.get("redirected_to"))
        redirected = len(self.data) - active
        with_n_sources = {}
        for e in self.data.values():
            if e.get("redirected_to"):
                continue
            n = len(e["source_refs"])
            with_n_sources[n] = with_n_sources.get(n, 0) + 1
        return {
            "total":         len(self.data),
            "active":        active,
            "redirected":    redirected,
            "by_n_sources":  with_n_sources,
        }


class ArchitectRegistry(_Registry):
    id_prefix = "arch"
    def __init__(self, path: str = ARCHITECTS_PATH) -> None:
        super().__init__(path)


class BuildingRegistry(_Registry):
    id_prefix = "bld"
    def __init__(self, path: str = BUILDINGS_PATH) -> None:
        super().__init__(path)
