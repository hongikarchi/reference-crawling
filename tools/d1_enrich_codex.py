"""Stage D-1 text enrichment runner using codex exec.

Reads the current 4-source canonical cluster artifact, gathers source-side
description text, and asks Codex/GPT-5.5 to classify one canonical cluster at a
time into the controlled enrichment fields used by downstream upload.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.vocab import ATMOSPHERE, COLOR_TONE, PROGRAM, STYLE  # noqa: E402


CANONICAL_PATH = ROOT / "data/canonical/canonical_buildings_4source.json"
RESULTS_PATH = ROOT / "data/canonical/d1_results.jsonl"
FAILURES_PATH = ROOT / "data/canonical/d1_failures.json"

DIVISARE_DB = ROOT / "data/crawl/divisare.db"
ARCHITIZER_DB = ROOT / "data/crawl/architizer.db"
ARCHELLO_DB = ROOT / "data/crawl/archello.db"
METALOCUS_DB = ROOT / "data/crawl/metalocus.db"
METALOCUS_FINAL = ROOT / "data/enrich/4_buildings_final.json"

MAX_DESC_LEN = 1500
CODEX_TIMEOUT_SECONDS = 180
PROCESS_BACKOFFS = (5, 15, 45)
PROGRESS_EVERY = 100


@dataclass(frozen=True)
class SourceRecord:
    source: str
    source_id: str
    text: str
    name: str | None = None
    architect_names: tuple[str, ...] = ()
    city: str | None = None
    country: str | None = None
    year: int | None = None
    typology: str | None = None


@dataclass
class RunStats:
    total: int
    already_done: int
    started_at: float = field(default_factory=time.time)
    successes: int = 0
    failures: int = 0

    @property
    def completed(self) -> int:
        return self.already_done + self.successes + self.failures

    def rate_per_minute(self) -> float:
        elapsed = max(time.time() - self.started_at, 1.0)
        return (self.successes + self.failures) / elapsed * 60.0


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())


class FailureLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def append(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.rows.append(row)
            self._write_locked()

    def clear_cid(self, cid: str) -> None:
        with self.lock:
            next_rows = [row for row in self.rows if row.get("cid") != cid]
            if len(next_rows) == len(self.rows):
                return
            self.rows = next_rows
            self._write_locked()

    def _write_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.rows, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self.path)


def _connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _coerce_year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", str(value))
    return int(m.group(1)) if m else None


def _clean_text(*parts: Any) -> str:
    text = " ".join(str(p) for p in parts if p)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_DESC_LEN]


def _json_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, list):
        return tuple(str(x) for x in value if x)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return (str(value),)
    if isinstance(parsed, list):
        return tuple(str(x) for x in parsed if x)
    return (str(parsed),) if parsed else ()


def load_source_records() -> dict[tuple[str, str], SourceRecord]:
    records: dict[tuple[str, str], SourceRecord] = {}

    with _connect(DIVISARE_DB) as conn:
        for row in conn.execute(
            "SELECT id, name, architect_names, location_city, location_country, "
            "project_year, tag_slugs, description, abstract FROM divisare_projects "
            "WHERE description IS NOT NULL OR abstract IS NOT NULL"
        ):
            text = _clean_text(row[7], row[8])
            if text:
                sid = str(row[0])
                records[("divisare", sid)] = SourceRecord(
                    source="divisare",
                    source_id=sid,
                    text=text,
                    name=row[1],
                    architect_names=_json_list(row[2]),
                    city=row[3],
                    country=row[4],
                    year=_coerce_year(row[5]),
                    typology=", ".join(_json_list(row[6])) or None,
                )

    with _connect(ARCHITIZER_DB) as conn:
        for row in conn.execute(
            "SELECT id, name, firm_name, location_city, location_country, "
            "completion_year, categories, description, description_short "
            "FROM architizer_projects "
            "WHERE description IS NOT NULL OR description_short IS NOT NULL"
        ):
            text = _clean_text(row[7], row[8])
            if text:
                sid = str(row[0])
                records[("architizer", sid)] = SourceRecord(
                    source="architizer",
                    source_id=sid,
                    text=text,
                    name=row[1],
                    architect_names=(str(row[2]),) if row[2] else (),
                    city=row[3],
                    country=row[4],
                    year=_coerce_year(row[5]),
                    typology=", ".join(_json_list(row[6])) or None,
                )

    with _connect(ARCHELLO_DB) as conn:
        for row in conn.execute(
            "SELECT id, name, architect_name, location_city, location_country, "
            "project_year, category, description FROM archello_projects "
            "WHERE description IS NOT NULL"
        ):
            text = _clean_text(row[7])
            if text:
                sid = str(row[0])
                records[("archello", sid)] = SourceRecord(
                    source="archello",
                    source_id=sid,
                    text=text,
                    name=row[1],
                    architect_names=(str(row[2]),) if row[2] else (),
                    city=row[3],
                    country=row[4],
                    year=_coerce_year(row[5]),
                    typology=row[6],
                )

    with _connect(METALOCUS_DB) as conn:
        slug_rows = {
            row[0]: row
            for row in conn.execute(
                "SELECT articles.slug, buildings.title, buildings.architects, "
                "buildings.city, buildings.country, buildings.year, "
                "buildings.building_type, buildings.description "
                "FROM buildings JOIN articles ON articles.id = buildings.article_id "
                "WHERE buildings.description IS NOT NULL"
            )
        }

    with METALOCUS_FINAL.open(encoding="utf-8") as f:
        metalocus = json.load(f)
    for row in metalocus:
        sid = str(row.get("building_id") or "")
        if not sid:
            continue
        db_row = next((slug_rows.get(slug) for slug in row.get("source_slugs") or []), None)
        db_text = _clean_text(db_row[7]) if db_row else ""
        text = db_text or _clean_text(row.get("description"))
        if not text:
            continue
        records[("metalocus", sid)] = SourceRecord(
            source="metalocus",
            source_id=sid,
            text=text,
            name=row.get("name_en") or row.get("project_name") or (db_row[1] if db_row else None),
            architect_names=(str(row["architect"]),) if row.get("architect") else (),
            city=row.get("city") or (db_row[3] if db_row else None),
            country=row.get("location_country") or (db_row[4] if db_row else None),
            year=_coerce_year(row.get("year") or (db_row[5] if db_row else None)),
            typology=row.get("program") or (db_row[6] if db_row else None),
        )

    return records


def load_clusters(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    clusters = data.get("clusters")
    if not isinstance(clusters, list):
        raise ValueError(f"{path} must contain a 'clusters' list")
    return clusters


def _primary_name(cluster: dict[str, Any]) -> str:
    names = cluster.get("names") or []
    return (
        cluster.get("primary_name")
        or cluster.get("canonical_name")
        or (names[0] if names else None)
        or cluster["canonical_bld_id"]
    )


def build_entries(
    clusters: list[dict[str, Any]],
    source_records: dict[tuple[str, str], SourceRecord],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for cluster in clusters:
        used_records: list[SourceRecord] = []
        descriptions = []
        for source, source_ids in (cluster.get("source_refs") or {}).items():
            for source_id in source_ids:
                record = source_records.get((source, str(source_id)))
                if record:
                    used_records.append(record)
                    descriptions.append({"source": source, "text": record.text})
                    break

        primary_name = _primary_name(cluster)
        if not descriptions:
            descriptions.append(
                {
                    "source": "canonical",
                    "text": (
                        "No source description was available. Classify from canonical metadata: "
                        f"name={primary_name}; names={cluster.get('names') or []}; "
                        f"source_refs={cluster.get('source_refs') or {}}."
                    )[:MAX_DESC_LEN],
                }
            )

        arch_names = []
        for record in used_records:
            for name in record.architect_names:
                if name and name not in arch_names:
                    arch_names.append(name)

        entries.append(
            {
                "cid": cluster["canonical_bld_id"],
                "primary_name": primary_name,
                "arch_names": arch_names[:3],
                "city": cluster.get("city") or _first_value(used_records, "city"),
                "country": cluster.get("country") or _first_value(used_records, "country"),
                "year": cluster.get("year") or _first_value(used_records, "year"),
                "typology": cluster.get("typology") or _first_value(used_records, "typology"),
                "descriptions": descriptions,
            }
        )
    return entries


def _first_value(records: list[SourceRecord], attr: str) -> Any:
    for record in records:
        value = getattr(record, attr)
        if value:
            return value
    return None


def read_done_cids(path: Path) -> set[str]:
    done = set()
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                print(f"[d1] warning: skipping malformed result line {line_no}", file=sys.stderr)
                continue
            cid = row.get("cid")
            if cid:
                done.add(str(cid))
    return done


def build_prompt(entry: dict[str, Any], retry_note: str | None = None) -> str:
    descriptions = "\n\n".join(
        f"Source: {d['source']}\nText: {d['text']}" for d in entry["descriptions"]
    )
    retry = f"\n\nPrevious output was invalid: {retry_note}\nReturn valid JSON only." if retry_note else ""
    return f"""You are classifying architecture projects for a recommendation database.

Return exactly one JSON object and no Markdown. Use only the allowed vocabulary values.

Required JSON schema:
{{
  "program": one of {sorted(PROGRAM)},
  "style": one of {sorted(STYLE)},
  "color_tone": one of {sorted(COLOR_TONE)},
  "atmosphere": one of {sorted(ATMOSPHERE)},
  "material_visual": ["concrete", "glass", "..."],
  "visual_description": "40-80 words describing spatial character, massing, materials, and setting"
}}

Rules:
- Infer from text only. Do not invent facts that are not supported.
- Choose the closest allowed value when the source wording is more specific than the vocabulary.
- material_visual must be 1-6 lowercase material words or short phrases visible/architectural in nature.
- visual_description must be concise, factual, and useful for visual similarity search.
- Do not include cid, source text, comments, code fences, or extra keys.

Cluster:
cid: {entry["cid"]}
primary_name: {entry["primary_name"]}
architects: {entry.get("arch_names") or []}
city: {entry.get("city")}
country: {entry.get("country")}
year: {entry.get("year")}
typology_hint: {entry.get("typology")}

Source descriptions:
{descriptions}{retry}
"""


def run_codex(prompt: str) -> str:
    base_command = [
        "codex",
        "exec",
        "--skip-git-check",
        "-c",
        "model=gpt-5.5",
        "-c",
        "model_reasoning_effort=xhigh",
        "-c",
        "service_tier=fast",
        prompt,
    ]
    fallback_command = ["codex", "exec", "--skip-git-repo-check", *base_command[3:]]
    last_error = ""
    for attempt, backoff in enumerate(PROCESS_BACKOFFS, 1):
        try:
            proc = subprocess.run(
                base_command,
                capture_output=True,
                text=True,
                timeout=CODEX_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {CODEX_TIMEOUT_SECONDS}s"
        else:
            if proc.returncode == 0:
                return proc.stdout
            stderr = proc.stderr.strip()
            stdout = proc.stdout.strip()
            last_error = f"codex exit {proc.returncode}: {stderr or stdout}"
            if "unexpected argument '--skip-git-check'" in last_error:
                try:
                    fallback = subprocess.run(
                        fallback_command,
                        capture_output=True,
                        text=True,
                        timeout=CODEX_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    last_error = f"timeout after {CODEX_TIMEOUT_SECONDS}s"
                else:
                    if fallback.returncode == 0:
                        return fallback.stdout
                    stderr = fallback.stderr.strip()
                    stdout = fallback.stdout.strip()
                    last_error = f"codex exit {fallback.returncode}: {stderr or stdout}"

        if attempt < len(PROCESS_BACKOFFS):
            time.sleep(backoff)

    raise RuntimeError(last_error)


def extract_json(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    block = _first_balanced_object(text)
    if block:
        cleaned = re.sub(r",\s*([}\]])", r"\1", block)
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in codex stdout")


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
    return None


def validate_result(cid: str, row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    required = ("program", "style", "color_tone", "atmosphere", "material_visual", "visual_description")
    missing = [field_name for field_name in required if field_name not in row]
    if missing:
        return None, f"missing fields: {missing}"

    checks = {
        "program": PROGRAM,
        "style": STYLE,
        "color_tone": COLOR_TONE,
        "atmosphere": ATMOSPHERE,
    }
    for field_name, valid_values in checks.items():
        value = row.get(field_name)
        if value not in valid_values:
            return None, f"{field_name}={value!r} not in allowed vocabulary"

    materials = row.get("material_visual")
    if not isinstance(materials, list):
        return None, "material_visual must be a list"
    material_visual = [str(x).strip().lower() for x in materials if str(x).strip()]
    material_visual = material_visual[:6]
    if not material_visual:
        return None, "material_visual must contain at least one material"

    visual_description = str(row.get("visual_description") or "").strip()
    if len(visual_description.split()) < 8:
        return None, "visual_description is too short"

    return (
        {
            "cid": cid,
            "program": row["program"],
            "style": row["style"],
            "color_tone": row["color_tone"],
            "atmosphere": row["atmosphere"],
            "material_visual": material_visual,
            "visual_description": visual_description,
        },
        None,
    )


def enrich_one(entry: dict[str, Any]) -> dict[str, Any]:
    retry_note = None
    last_error = ""
    for _ in range(3):
        prompt = build_prompt(entry, retry_note)
        try:
            stdout = run_codex(prompt)
            parsed = extract_json(stdout)
        except Exception as exc:  # noqa: BLE001 - persisted into failure ledger
            last_error = str(exc)
            retry_note = last_error
            continue

        result, error = validate_result(entry["cid"], parsed)
        if result:
            return result
        last_error = error or "unknown validation error"
        retry_note = last_error

    raise ValueError(last_error)


def worker(
    entry: dict[str, Any],
    writer: JsonlWriter,
    failures: FailureLedger,
    stats: RunStats,
    stats_lock: threading.Lock,
) -> None:
    cid = entry["cid"]
    try:
        result = enrich_one(entry)
    except Exception as exc:  # noqa: BLE001 - continue batch after per-row failure
        failures.append({"cid": cid, "error": str(exc), "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        with stats_lock:
            stats.failures += 1
            maybe_print_progress(stats)
        return

    writer.append(result)
    failures.clear_cid(result["cid"])
    with stats_lock:
        stats.successes += 1
        maybe_print_progress(stats)


def maybe_print_progress(stats: RunStats) -> None:
    if stats.completed % PROGRESS_EVERY == 0 or stats.completed == stats.total:
        print(
            f"[d1] {stats.completed}/{stats.total} done, "
            f"{stats.failures} failures, {stats.rate_per_minute():.1f} cids/min",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-input", type=Path, default=CANONICAL_PATH)
    parser.add_argument("--results", type=Path, default=RESULTS_PATH)
    parser.add_argument("--failures", type=Path, default=FAILURES_PATH)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N pending entries")
    parser.add_argument("--dry-run", action="store_true", help="Build entries and print one sample without codex calls")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("[d1] loading source descriptions", flush=True)
    source_records = load_source_records()
    print(f"[d1] {len(source_records)} source descriptions loaded", flush=True)

    print("[d1] loading canonical clusters", flush=True)
    clusters = load_clusters(args.canonical_input)
    entries = build_entries(clusters, source_records)
    fallback_entries = sum(
        1 for entry in entries if entry["descriptions"][0]["source"] == "canonical"
    )
    if fallback_entries:
        print(f"[d1] warning: {fallback_entries} clusters use canonical metadata fallback", file=sys.stderr)
    print(f"[d1] {len(entries)}/{len(clusters)} clusters ready for enrichment", flush=True)

    done = read_done_cids(args.results)
    pending = [entry for entry in entries if entry["cid"] not in done]
    if args.limit is not None:
        pending = pending[: args.limit]

    if args.dry_run:
        sample = pending[0] if pending else None
        print(
            json.dumps(
                {
                    "clusters": len(clusters),
                    "entries": len(entries),
                    "already_done": len(done),
                    "pending": len(pending),
                    "sample": sample,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if not pending:
        print("[d1] nothing to do", flush=True)
        return 0

    writer = JsonlWriter(args.results)
    failures = FailureLedger(args.failures)
    stats = RunStats(total=len(entries), already_done=len(done))
    stats_lock = threading.Lock()
    print(
        f"[d1] starting {len(pending)} pending clusters with {args.workers} workers "
        f"({len(done)} already done)",
        flush=True,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(worker, entry, writer, failures, stats, stats_lock)
            for entry in pending
        ]
        for future in concurrent.futures.as_completed(futures):
            future.result()

    print(
        f"[d1] complete: {stats.successes} successes, {stats.failures} failures, "
        f"{len(done)} skipped",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
