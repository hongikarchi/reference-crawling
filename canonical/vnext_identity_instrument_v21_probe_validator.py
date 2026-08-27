"""Independent, fail-closed validator for the WP2.8 v2.1 probe.

The producer module is intentionally not imported.  Every vocabulary,
consensus, derivation, source-reuse, blind-review, and gate calculation used
below is restated from immutable inputs with the Python standard library.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence


VALIDATOR_VERSION = "vnext-identity-instrument-v21-probe-independent-validator-v1"
APPLICATION_ID = 0x57503238
USER_VERSION = 1
RUNNER_VERSION = "vnext-identity-instrument-v21-probe-v1.0.0"
INSTRUMENT_VERSION = "vnext-identity-instrument-v21"
SAMPLE_VERSION = "vnext-identity-instrument-v21-probe-v1"
REVIEW_VERSION = "vnext-identity-instrument-v21-review-v1"
ADJUDICATION_VERSION = "vnext-identity-instrument-v21-axis-adjudication-v1"
GATE_VERSION = "vnext-identity-instrument-v21-predeclared-gate-v1"
VOCABULARY_VERSION = "vnext-identity-instrument-v21-n100-vocabulary-v1"
CONSENSUS_VERSION = "vnext-identity-instrument-v21-consensus-v1"
DERIVATION_VERSION = "vnext-identity-instrument-v21-derivation-v1"

N100_RUN_ID = "wp23-d0d4c9cdf8b969d7455483af"
N100_SPEC = (2_183_168, "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b")
WP27_RUN_ID = "wp27-d49c5111f27a5f59ad3fd754"
WP27_SPEC = (1_695_744, "f31ee6558e43c9b0b412f2c3cda964100acffe0763f9995d301fb32865dece65")
SOURCE_RUN_ID = "wp26-250a8cefe5396b87377ad55d"
SOURCE_SAMPLE_VERSION = "vnext-identity-instrument-probe-v1"
SOURCE_SEED = "vnext-wp26-instrument-probe-v1-20260827"
SOURCE_LOGICAL_SHA256 = "784a567458227a737c78b31320eb79fdfbca0e7cf5909d08bb87c38f12e2b208"
SOURCE_PACKET_SHA256 = "b1f0491852d68648405c032e1a8486ed41a939b18eb655cea1aab92ab07b39e5"
SOURCE_INVENTORY_SHA256 = "0cdf2b6b42b9a4866a2e9ee0cb37e5efb9c045915ce83e67d350569749bbe99e"
SOURCE_SHEET_BYTES = 24_517_369
TOTAL_SAMPLE = 24
SOURCE_SHEET_COUNT = 48
SOURCE_ASSET_COUNT = 93

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_instrument_v21_probe_v1.db")
DEFAULT_REPORT = Path("data/reports/vnext_identity_instrument_v21_probe_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_instrument_v21_probe_v1_manifest.json")
DEFAULT_VALIDATION_REPORT = Path("data/reports/vnext_identity_instrument_v21_probe_v1_validation.json")
DEFAULT_N100_DB = Path("data/enrichment/vnext_identity_n100_v1.db")
DEFAULT_WP27_DB = Path("data/enrichment/vnext_identity_instrument_v2_probe_v1.db")
DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_INSTRUMENT_V21_PROTOCOL.md")
DEFAULT_ORDER = Path(".claude/ops/jobs/20260827_vnext_wp28_instrument_v21_order.md")

OBSERVATION_KEYS = ("image", "name", "firm", "place", "year", "scope", "basis")
CONTROLLED_KEYS = OBSERVATION_KEYS[:-1]
CONTROLLED_VALUES: dict[str, tuple[str, ...]] = {
    "image": ("same_object", "different_objects", "inconclusive", "unavailable"),
    "name": ("agree", "alias", "conflict", "unclear"),
    "firm": ("agree", "overlap", "conflict", "unclear"),
    "place": ("agree", "conflict", "unclear"),
    "year": ("compatible", "conflict", "unclear"),
    "scope": ("same", "related_separate", "unclear", "unrelated"),
}
CONSENSUS_PRIORITY: dict[str, tuple[str, ...]] = {
    "image": ("unavailable", "inconclusive", "different_objects", "same_object"),
    "name": ("conflict", "alias", "agree", "unclear"),
    "firm": ("conflict", "overlap", "agree", "unclear"),
    "place": ("conflict", "agree", "unclear"),
    "year": ("conflict", "compatible", "unclear"),
    "scope": ("unrelated", "related_separate", "same", "unclear"),
}
EXPECTED_VOCAB_COUNTS: dict[str, dict[str, int]] = {
    "image": {"different_objects": 24, "inconclusive": 12, "same_object": 171, "unavailable": 3},
    "name": {"agree": 129, "alias": 73, "conflict": 4, "unclear": 4},
    "firm": {"agree": 147, "conflict": 37, "overlap": 24, "unclear": 2},
    "place": {"agree": 25, "conflict": 1, "unclear": 184},
    "year": {"compatible": 162, "conflict": 31, "unclear": 17},
    "scope": {"related_separate": 2, "same": 155, "unclear": 28, "unrelated": 25},
}
EXPECTED_RULE_COUNTS = {"1": 11, "2": 10, "3": 1, "4": 83, "5": 0}
EXPECTED_MISMATCHES = (
    ("n100_5c76a0ad9d55e899c66ffb8a", "same", "related"),
    ("n100_b8398cb6c0618c78b88a1576", "same", "insufficient"),
    ("n100_c8789d856720d9cb7c7c2a5d", "same", "insufficient"),
)
EXPECTED_GOLD_COUNTS = {"different": 10, "insufficient": 13, "related": 2, "same": 80}
EXPECTED_IMAGE_GOLD = {
    "different_objects": {"different": 20, "insufficient": 4},
    "inconclusive": {"insufficient": 12},
    "same_object": {"insufficient": 7, "related": 4, "same": 160},
    "unavailable": {"insufficient": 3},
}
EXPECTED_UNCERTAIN_N100_IDS = frozenset({
    "n100_2c6e8b236183ac67d52480e6", "n100_3173cc9314cd34bdab869d1c",
    "n100_1809907a641b07169a0d2436", "n100_3fde31b883d60c402c002057",
    "n100_215d784c23c71801b8742720",
})
GROUP_COUNTS = {"D": 15, "P1": 3, "P2": 4, "P3": 2}
GATE_THRESHOLDS = {
    "d_image_same_object": (14, 15), "d_scope_same": (14, 15),
    "n100_image_non_same_object": (3, 5), "p1_non_same_label": (2, 3),
}
QUOTA_PHASES = ("pre_freeze", "pre_r1_review", "pre_r2_review", "pre_adjudication_review", "pre_reveal_score")
PRE_TERMINAL_STATES = (
    "vocabulary_frozen", "derivation_verified", "instrument_frozen", "sample_frozen",
    "criteria_frozen", "review_ready", "reviews_partial", "reviews_locked", "adjudication_locked",
)
TERMINAL_STATES = frozenset({"pass", "fail"})
ALLOWED_LABELS = frozenset({"same", "related", "different", "insufficient"})

RATIONALE_RE = re.compile(
    r"\Aimage=(?P<image>[a-z_]+); name=(?P<name>[a-z_]+); "
    r"firm=(?P<firm>[a-z_]+); place=(?P<place>[a-z_]+); "
    r"year=(?P<year>[a-z_]+); scope=(?P<scope>[a-z_]+); basis=(?P<basis>.+)\Z"
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    checks: tuple[Check, ...]
    metrics: Mapping[str, Any]
    report_path: Path | None

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _rows(connection: sqlite3.Connection, query: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, tuple(parameters))]


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _check(checks: list[Check], name: str, expected: Any, actual: Any) -> None:
    checks.append(Check(name, expected == actual, expected, actual))


def _iso(value: Any) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return parsed


def parse_n100_rationale(value: Any) -> dict[str, str]:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("N100 rationale must be trimmed text")
    match = RATIONALE_RE.fullmatch(value)
    if match is None:
        raise ValueError("N100 rationale grammar")
    result = match.groupdict()
    for axis in CONTROLLED_KEYS:
        if result[axis] not in CONTROLLED_VALUES[axis]:
            raise ValueError(f"unknown {axis} value")
    if not result["basis"].strip() or result["basis"] != result["basis"].strip():
        raise ValueError("basis must be trimmed non-empty text")
    return result


def observation_failures(value: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    keys = {str(key) for key in value}
    if keys != set(OBSERVATION_KEYS):
        failures.append("keys")
    for axis in CONTROLLED_KEYS:
        if value.get(axis) not in CONTROLLED_VALUES[axis]:
            failures.append(axis)
    basis = value.get("basis")
    if not isinstance(basis, str) or not basis.strip() or basis != basis.strip():
        failures.append("basis")
    return sorted(set(failures))


def consensus_axes(r1: Mapping[str, Any], r2: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for axis in CONTROLLED_KEYS:
        left, right = str(r1.get(axis, "")), str(r2.get(axis, ""))
        priority = CONSENSUS_PRIORITY[axis]
        if left not in priority or right not in priority:
            raise ValueError(f"unknown consensus value for {axis}")
        result[axis] = left if left == right else min((left, right), key=priority.index)
    return result


def derivation_rules() -> list[dict[str, Any]]:
    return [
        {"rule": 1, "when": "image IN (unavailable,inconclusive)", "label": "insufficient"},
        {"rule": 2, "when": "image=different_objects OR scope=unrelated", "label": "different"},
        {"rule": 3, "when": "scope=related_separate", "label": "related"},
        {"rule": 4, "when": "image=same_object AND scope=same", "label": "same"},
        {"rule": 5, "when": "otherwise", "label": "insufficient"},
    ]


def derive_label(observation: Mapping[str, Any]) -> tuple[str, int]:
    for axis in CONTROLLED_KEYS:
        if observation.get(axis) not in CONTROLLED_VALUES[axis]:
            raise ValueError(f"invalid {axis}")
    image, scope = str(observation["image"]), str(observation["scope"])
    if image in {"unavailable", "inconclusive"}:
        return "insufficient", 1
    if image == "different_objects" or scope == "unrelated":
        return "different", 2
    if scope == "related_separate":
        return "related", 3
    if image == "same_object" and scope == "same":
        return "same", 4
    return "insufficient", 5


def _axis_instructions() -> dict[str, Any]:
    return {
        "version": "vnext-identity-instrument-v21-axis-guidance-v1",
        "actor_contract": (
            "Record or adjudicate only the six controlled observations and mandatory basis; "
            "do not choose or infer a label. Every controlled value is equally legitimate."
        ),
        "observation_keys": list(OBSERVATION_KEYS),
        "controlled_values": {axis: list(CONTROLLED_VALUES[axis]) for axis in CONTROLLED_KEYS},
        "basis": {"required": True, "format": "trimmed_non_empty_text"},
        "image_guidance": {
            "same_object": (
                "The usable images identify the same depicted physical object. A partial view, "
                "different angle, or fewer images alone is not inconclusive."
            ),
            "different_objects": "Comparable usable building-form evidence identifies different physical objects.",
            "inconclusive": (
                "Usable imagery exists, but no comparable building form is present, for example "
                "drawings or diagrams only, a publication page, or unreadable imagery."
            ),
            "unavailable": "At least one side has no usable image at all.",
            "identifiable_same_object": (
                "If the same object is identifiable despite view angle, crop, or image-count differences, "
                "record same_object."
            ),
        },
        "scope_guidance": {
            "photo_identity_is_not_scope_identity": (
                "Matching photographs establish a shared depicted object, not necessarily equal scope."
            ),
            "related_separate": "Use related_separate only when the records are related but describe distinct scope.",
        },
    }


def gate_from_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(rows) != TOTAL_SAMPLE or Counter(str(row.get("group_code", row.get("group", ""))) for row in rows) != GROUP_COUNTS:
        raise ValueError("frozen 24-row group composition")
    uncertain = [row for row in rows if bool(row.get("n100_uncertain"))]
    if len(uncertain) != 5:
        raise ValueError("frozen uncertain subset")
    counts = {
        "d_image_same_object": sum(str(r.get("group_code", r.get("group", ""))) == "D" and r.get("image") == "same_object" for r in rows),
        "d_scope_same": sum(str(r.get("group_code", r.get("group", ""))) == "D" and r.get("scope") == "same" for r in rows),
        "n100_image_non_same_object": sum(r.get("image") != "same_object" for r in uncertain),
        "p1_non_same_label": sum(str(r.get("group_code", r.get("group", ""))) == "P1" and r.get("derived_label") != "same" for r in rows),
    }
    predicates = {
        name: {"actual": counts[name], "threshold": threshold, "denominator": denominator,
               "passed": counts[name] >= threshold}
        for name, (threshold, denominator) in GATE_THRESHOLDS.items()
    }
    return {"counts": counts, "predicates": predicates,
            "status": "pass" if all(item["passed"] for item in predicates.values()) else "fail"}


def _walk_json(value: Any, path: str = "$") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            location = f"{path}.{key}"
            yield location, str(key), child
            yield from _walk_json(child, location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{path}[{index}]")


def blind_json_leaks(value: Any, *, hidden_identifiers: Iterable[str] = ()) -> list[str]:
    forbidden_fragments = ("label", "gold", "candidate", "group", "cohort", "stratum", "score", "proposal", "reveal", "derivation")
    failures: list[str] = []
    for location, key, child in _walk_json(value):
        normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        if any(fragment in normalized for fragment in forbidden_fragments):
            failures.append(location)
        if isinstance(child, str) and child in {"P1", "P2", "P3", "D"}:
            failures.append(location + ":hidden_group_value")
    serialized = _canonical_json(value)
    if any(token and token in serialized for token in hidden_identifiers):
        failures.append("hidden_identifier_value")
    return sorted(set(failures))


def quota_ledger_failures(rows: Sequence[Mapping[str, Any]], *, expected_phases: Sequence[str] = QUOTA_PHASES) -> list[str]:
    failures: list[str] = []
    seen: list[str] = []
    previous_phase = -1
    previous_time: datetime | None = None
    stopped = False
    latest_authorized = True
    latest_phase = ""
    for number, row in enumerate(rows, 1):
        if int(row.get("observation_no", -1)) != number:
            failures.append(f"{number}:observation_order")
        phase = str(row.get("phase", ""))
        if phase not in QUOTA_PHASES:
            failures.append(f"{number}:phase")
        else:
            index = QUOTA_PHASES.index(phase)
            if index < previous_phase or index > previous_phase + 1:
                failures.append(f"{number}:phase_order")
            if index != previous_phase:
                if latest_phase and not latest_authorized:
                    failures.append(latest_phase + ":wave_not_authorized")
                seen.append(phase)
                previous_phase = index
            latest_phase = phase
        try:
            when = _iso(row.get("observed_at"))
            if previous_time is not None and when < previous_time:
                failures.append(f"{number}:timestamp_order")
            previous_time = when
        except (TypeError, ValueError):
            failures.append(f"{number}:timestamp")
        try:
            remaining = float(row.get("remaining_percent"))
            if not 0 <= remaining <= 100:
                raise ValueError
        except (TypeError, ValueError):
            remaining = -1
            failures.append(f"{number}:remaining")
        start, stop = int(remaining > 30), int(remaining <= 20)
        if row.get("start_allowed") not in (0, 1) or int(row["start_allowed"]) != start:
            failures.append(f"{number}:start_gate")
        if row.get("stop_required") not in (0, 1) or int(row["stop_required"]) != stop:
            failures.append(f"{number}:stop_gate")
        if stopped:
            failures.append(f"{number}:activity_after_stop")
        stopped = stopped or bool(stop)
        latest_authorized = bool(start)
    if latest_phase and not latest_authorized:
        failures.append(latest_phase + ":wave_not_authorized")
    if tuple(seen) != tuple(expected_phases):
        failures.append("phase_set")
    return sorted(set(failures))


CRITICAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "runs": (
        "run_id", "schema_version", "runner_version", "instrument_version", "sample_version",
        "review_version", "adjudication_version", "gate_version", "source_sample_version",
        "source_seed", "source_logical_sha256", "source_run_id",
        "source_packet_binding_sha256", "source_visual_inventory_sha256", "status",
        "sticky_stop", "sample_count", "sheet_count", "asset_occurrence_count",
        "unique_asset_count", "inherited_logical_http_fetches", "inherited_wire_http_requests",
        "incremental_logical_http_fetches", "incremental_wire_http_requests",
        "incremental_retries", "incremental_redirects", "incremental_renderer_substitutions",
        "incremental_cache_writes", "r1_status", "r2_status", "adjudication_status",
        "gate_status", "created_at", "completed_at", "record_sha256",
    ),
    "n100_review_axes": (
        "run_id", "lane", "opaque_review_id", "n100_sample_id", "image", "name", "firm",
        "place", "year", "scope", "basis", "source_record_sha256", "record_sha256",
    ),
    "vocabulary_counts": (
        "run_id", "axis", "value", "frequency", "r1_frequency", "r2_frequency", "record_sha256",
    ),
    "vocabulary_contract": (
        "run_id", "version", "parser_regex", "row_count", "vocabulary_json",
        "vocabulary_sha256", "basis_nonblank_count", "frozen_at", "record_sha256",
    ),
    "consensus_contract": (
        "run_id", "version", "priority_json", "priority_sha256", "frozen_at", "record_sha256",
    ),
    "n100_consensus_axes": (
        "run_id", "n100_sample_id", "image", "name", "firm", "place", "year", "scope",
        "r1_observation_sha256", "r2_observation_sha256", "consensus_sha256", "record_sha256",
    ),
    "derivation_contract": (
        "run_id", "version", "rules_json", "rules_sha256", "numerator", "denominator",
        "minimum_matches", "passed", "rule_counts_json", "mismatches_json", "verified_at",
        "record_sha256",
    ),
    "gate_contract": (
        "run_id", "gate_json", "gate_sha256", "d_image_min", "d_image_n", "d_scope_min",
        "d_scope_n", "n100_image_non_same_min", "n100_image_non_same_n",
        "p1_min_non_same", "p1_n", "eligible_sample_ids_json", "declared_at", "record_sha256",
    ),
    "control_reveals": (
        "run_id", "sample_id", "candidate_id", "group_code", "n100_sample_id",
        "n100_axes_json", "n100_gold_label", "v1_probe_label", "v2_derived_label",
        "v21_derived_label", "derivation_rule_no", "commitment_sha256", "revealed_at",
        "reveal_payload_sha256", "record_sha256",
    ),
    "review_observations": (
        "run_id", "lane", "display_order", "opaque_review_id", "sample_id", "image", "name",
        "firm", "place", "year", "scope", "basis", "reviewer", "reviewed_at",
        "observation_sha256", "record_sha256",
    ),
    "adjudicated_observations": (
        "run_id", "opaque_adjudication_id", "sample_id", "image", "name", "firm", "place",
        "year", "scope", "basis", "derived_label", "derivation_rule", "axis_adjustments_json",
        "adjudicator", "adjudicated_at", "observation_sha256", "record_sha256",
    ),
}
REQUIRED_TABLES = frozenset({
    "runs", "input_pins", "instrument_contract", "source_samples", "source_sheets",
    "source_assets", "source_fetch_assets", "source_fetch_attempts", "source_cache_pins",
    "quota_observations", "gate_contract", "artifact_paths", "artifact_bindings",
    "json_payloads", "artifacts", "review_observations", "review_locks",
    "review_lock_payloads", "adjudication_items", "adjudicated_observations",
    "adjudication_lock", "adjudication_lock_payload", "control_reveals", "gate_scores",
    "state_events", "terminal", "n100_review_axes", "vocabulary_counts",
    "vocabulary_contract", "consensus_contract", "n100_consensus_axes", "derivation_contract",
})


def _sqlite_sidecars(path: Path) -> list[str]:
    return [Path(str(path) + suffix).name for suffix in ("-wal", "-shm", "-journal")
            if Path(str(path) + suffix).exists()]


def _record_hash_failures(connection: sqlite3.Connection) -> list[str]:
    failures: list[str] = []
    checked = 0
    for table in sorted(_tables(connection)):
        if "record_sha256" not in _columns(connection, table):
            continue
        for index, row in enumerate(_rows(connection, f'SELECT * FROM "{table}"')):
            checked += 1
            actual = row.pop("record_sha256", None)
            if actual != _record_sha(row):
                failures.append(f"{table}[{index}]")
    return failures if checked else ["no_record_hashes"]


def _n100_snapshot(root: Path, *, verify_full_hashes: bool) -> dict[str, Any]:
    path = _resolve(root, DEFAULT_N100_DB)
    if not path.is_file():
        raise ValueError("N100 DB missing")
    if verify_full_hashes and (path.stat().st_size, _sha256_file(path)) != N100_SPEC:
        raise ValueError("N100 DB pin")
    if _sqlite_sidecars(path):
        raise ValueError("N100 mutable sidecar")
    connection = _open_readonly(path)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("N100 integrity")
        run = dict(connection.execute("SELECT * FROM runs").fetchone())
        if run.get("run_id") != N100_RUN_ID:
            raise ValueError("N100 run")
        reviews = _rows(connection, "SELECT * FROM blind_reviews ORDER BY sample_id,lane")
        gold_rows = _rows(connection, "SELECT * FROM adjudications ORDER BY sample_id")
    finally:
        connection.close()
    if len(reviews) != 210 or len(gold_rows) != 105:
        raise ValueError("N100 row counts")
    if Counter(row["lane"] for row in reviews) != {"r1": 105, "r2": 105}:
        raise ValueError("N100 lane counts")
    by_sample: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    frequencies = {lane: {axis: Counter() for axis in CONTROLLED_KEYS}
                   for lane in ("r1", "r2", "all")}
    for row in reviews:
        parsed = parse_n100_rationale(row["rationale"])
        lane, sample_id = str(row["lane"]), str(row["sample_id"])
        by_sample[sample_id][lane] = parsed
        by_key[(lane, str(row["opaque_review_id"]))] = {**row, **parsed}
        for axis in CONTROLLED_KEYS:
            frequencies[lane][axis][parsed[axis]] += 1
            frequencies["all"][axis][parsed[axis]] += 1
    observed = {axis: dict(sorted(frequencies["all"][axis].items())) for axis in CONTROLLED_KEYS}
    if observed != EXPECTED_VOCAB_COUNTS or any(set(pair) != {"r1", "r2"} for pair in by_sample.values()):
        raise ValueError("N100 vocabulary or paired reviews")
    gold = {str(row["sample_id"]): str(row["final_gold_label"]) for row in gold_rows}
    if set(gold) != set(by_sample) or dict(sorted(Counter(gold.values()).items())) != EXPECTED_GOLD_COUNTS:
        raise ValueError("N100 gold")
    image_gold: dict[str, Counter[str]] = defaultdict(Counter)
    consensus: dict[str, dict[str, str]] = {}
    mismatches: list[dict[str, Any]] = []
    rule_counts: Counter[int] = Counter()
    for sample_id, pair in sorted(by_sample.items()):
        for lane in ("r1", "r2"):
            image_gold[pair[lane]["image"]][gold[sample_id]] += 1
        final = consensus_axes(pair["r1"], pair["r2"])
        consensus[sample_id] = final
        predicted, rule = derive_label(final)
        rule_counts[rule] += 1
        if predicted != gold[sample_id]:
            mismatches.append({"n100_sample_id": sample_id, "predicted_label": predicted,
                               "gold_label": gold[sample_id], "derivation_rule": rule,
                               "consensus_axes": final})
    if {key: dict(sorted(value.items())) for key, value in sorted(image_gold.items())} != EXPECTED_IMAGE_GOLD:
        raise ValueError("N100 reference image/gold crosstab")
    counts = {str(rule): int(rule_counts[rule]) for rule in range(1, 6)}
    triples = tuple((row["n100_sample_id"], row["predicted_label"], row["gold_label"])
                    for row in mismatches)
    if counts != EXPECTED_RULE_COUNTS or triples != EXPECTED_MISMATCHES:
        raise ValueError("N100 102/105 replay")
    return {"run": run, "reviews": reviews, "by_sample": by_sample, "by_key": by_key,
            "frequencies": frequencies, "observed": observed, "gold": gold,
            "consensus": consensus, "mismatches": mismatches, "rule_counts": counts,
            "matches": 105 - len(mismatches)}


def _wp27_snapshot(root: Path, n100: Mapping[str, Any], *, verify_full_hashes: bool) -> dict[str, Any]:
    path = _resolve(root, DEFAULT_WP27_DB)
    if not path.is_file():
        raise ValueError("WP2.7 DB missing")
    if verify_full_hashes and (path.stat().st_size, _sha256_file(path)) != WP27_SPEC:
        raise ValueError("WP2.7 DB pin")
    if _sqlite_sidecars(path):
        raise ValueError("WP2.7 mutable sidecar")
    connection = _open_readonly(path)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("WP2.7 integrity")
        run = dict(connection.execute("SELECT * FROM runs").fetchone())
        samples = _rows(connection, "SELECT * FROM source_samples ORDER BY sample_rank")
        sheets = _rows(connection, "SELECT * FROM source_sheets ORDER BY lane,display_order")
        assets = _rows(connection, "SELECT * FROM source_assets ORDER BY sample_id,side,asset_order")
        fetch_assets = _rows(connection, "SELECT * FROM source_fetch_assets ORDER BY asset_key")
        fetch_attempts = _rows(connection, "SELECT * FROM source_fetch_attempts ORDER BY asset_key,attempt_no")
        cache_pins = _rows(connection, "SELECT * FROM source_cache_pins ORDER BY role")
        reveals = _rows(connection, "SELECT * FROM control_reveals ORDER BY sample_id")
    finally:
        connection.close()
    expected_run = {
        "run_id": WP27_RUN_ID, "source_run_id": SOURCE_RUN_ID,
        "source_sample_version": SOURCE_SAMPLE_VERSION, "source_seed": SOURCE_SEED,
        "source_logical_sha256": SOURCE_LOGICAL_SHA256,
        "source_packet_binding_sha256": SOURCE_PACKET_SHA256,
        "source_visual_inventory_sha256": SOURCE_INVENTORY_SHA256,
    }
    if any(run.get(key) != value for key, value in expected_run.items()):
        raise ValueError("WP2.7 run contract")
    if (len(samples), len(sheets), len(assets), len(fetch_assets), len(reveals)) != (24, 48, 93, 93, 24):
        raise ValueError("WP2.7 source counts")
    packet = [{key: row[key] for key in ("lane", "display_order", "opaque_review_id", "sample_id",
                                          "sheet_path", "sheet_sha256", "sheet_bytes")}
              for row in sheets]
    inventory = [{"lane": row["lane"], "display_order": int(row["display_order"]),
                  "path": row["sheet_path"], "sha256": row["sheet_sha256"],
                  "bytes": int(row["sheet_bytes"])} for row in sheets]
    if _record_sha(packet) != SOURCE_PACKET_SHA256 or _record_sha(inventory) != SOURCE_INVENTORY_SHA256:
        raise ValueError("WP2.7 packet/inventory SHA")
    if sum(int(row["sheet_bytes"]) for row in sheets) != SOURCE_SHEET_BYTES:
        raise ValueError("WP2.7 sheet bytes")
    for row in sheets:
        file = _resolve(root, str(row["sheet_path"]))
        if not file.is_file() or file.stat().st_size != int(row["sheet_bytes"]):
            raise ValueError("WP2.7 sheet file")
        if verify_full_hashes and _sha256_file(file) != row["sheet_sha256"]:
            raise ValueError("WP2.7 sheet SHA")
    if any(count > 2 for count in Counter((row["sample_id"], row["side"]) for row in assets).values()):
        raise ValueError("WP2.7 per-side asset cap")
    if Counter(str(row["group_code"]) for row in reveals) != GROUP_COUNTS:
        raise ValueError("WP2.7 group composition")
    uncertain = {str(row["n100_sample_id"]) for row in reveals
                 if any(n100["by_sample"][str(row["n100_sample_id"])][lane]["image"]
                        in {"inconclusive", "unavailable"} for lane in ("r1", "r2"))}
    if uncertain != EXPECTED_UNCERTAIN_N100_IDS:
        raise ValueError("frozen uncertain subset")
    return {"run": run, "samples": samples, "sheets": sheets, "assets": assets,
            "fetch_assets": fetch_assets, "fetch_attempts": fetch_attempts,
            "cache_pins": cache_pins, "reveals": reveals, "uncertain": uncertain}


def _validate_n100_persistence(connection: sqlite3.Connection, run_id: str,
                               n100: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    rows = _rows(connection, "SELECT * FROM n100_review_axes ORDER BY lane,opaque_review_id")
    if len(rows) != 210:
        failures.append("review_axes:row_count")
    for row in rows:
        source = n100["by_key"].get((str(row.get("lane")), str(row.get("opaque_review_id"))))
        if source is None:
            failures.append("review_axes:key")
            continue
        expected = {"n100_sample_id": source["sample_id"],
                    **{key: source[key] for key in OBSERVATION_KEYS},
                    "source_record_sha256": source["record_sha256"]}
        for key, value in expected.items():
            if row.get(key) != value:
                failures.append(f"review_axes:{row.get('opaque_review_id')}:{key}")
    count_rows = _rows(connection, "SELECT * FROM vocabulary_counts ORDER BY axis,value")
    expected_counts = []
    for axis in CONTROLLED_KEYS:
        for value in sorted(EXPECTED_VOCAB_COUNTS[axis]):
            expected_counts.append({
                "axis": axis, "value": value,
                "frequency": EXPECTED_VOCAB_COUNTS[axis][value],
                "r1_frequency": n100["frequencies"]["r1"][axis][value],
                "r2_frequency": n100["frequencies"]["r2"][axis][value],
            })
    expected_counts.sort(key=lambda row: (str(row["axis"]), str(row["value"])))
    actual_counts = [{key: row.get(key) for key in expected_counts[0]} for row in count_rows]
    if actual_counts != expected_counts:
        failures.append("vocabulary_counts")
    vocabulary_payload = {
        "version": VOCABULARY_VERSION, "source_run_id": N100_RUN_ID,
        "parser_regex": RATIONALE_RE.pattern, "row_count": 210,
        "controlled_values": {axis: list(CONTROLLED_VALUES[axis]) for axis in CONTROLLED_KEYS},
        "frequencies": EXPECTED_VOCAB_COUNTS,
        "basis": {"nonblank": 210, "required": True},
    }
    vocab = _rows(connection, "SELECT * FROM vocabulary_contract")
    if len(vocab) != 1:
        failures.append("vocabulary_contract:row_count")
    else:
        expected = {
            "version": VOCABULARY_VERSION, "parser_regex": RATIONALE_RE.pattern,
            "row_count": 210, "vocabulary_json": _canonical_json(vocabulary_payload),
            "vocabulary_sha256": _record_sha(vocabulary_payload), "basis_nonblank_count": 210,
        }
        for key, value in expected.items():
            if vocab[0].get(key) != value:
                failures.append("vocabulary_contract:" + key)
    priority_payload = {axis: list(CONSENSUS_PRIORITY[axis]) for axis in CONTROLLED_KEYS}
    consensus_contract = _rows(connection, "SELECT * FROM consensus_contract")
    if len(consensus_contract) != 1:
        failures.append("consensus_contract:row_count")
    else:
        expected = {"version": CONSENSUS_VERSION, "priority_json": _canonical_json(priority_payload),
                    "priority_sha256": _record_sha(priority_payload)}
        for key, value in expected.items():
            if consensus_contract[0].get(key) != value:
                failures.append("consensus_contract:" + key)
    persisted = {str(row["n100_sample_id"]): row for row in
                 _rows(connection, "SELECT * FROM n100_consensus_axes ORDER BY n100_sample_id")}
    if set(persisted) != set(n100["consensus"]):
        failures.append("consensus_axes:sample_set")
    for sample_id, final in n100["consensus"].items():
        row = persisted.get(sample_id, {})
        for axis, value in final.items():
            if row.get(axis) != value:
                failures.append(f"consensus_axes:{sample_id}:{axis}")
        pair = n100["by_sample"][sample_id]
        expected_hashes = {
            "r1_observation_sha256": _record_sha({"lane": "r1", **{a: pair["r1"][a] for a in CONTROLLED_KEYS}}),
            "r2_observation_sha256": _record_sha({"lane": "r2", **{a: pair["r2"][a] for a in CONTROLLED_KEYS}}),
            "consensus_sha256": _record_sha({"n100_sample_id": sample_id, **final}),
        }
        for key, value in expected_hashes.items():
            if row.get(key) != value:
                failures.append(f"consensus_axes:{sample_id}:{key}")
    derivation = _rows(connection, "SELECT * FROM derivation_contract")
    if len(derivation) != 1:
        failures.append("derivation_contract:row_count")
    else:
        expected = {
            "version": DERIVATION_VERSION, "rules_json": _canonical_json(derivation_rules()),
            "rules_sha256": _record_sha(derivation_rules()), "numerator": 102,
            "denominator": 105, "minimum_matches": 100, "passed": 1,
            "rule_counts_json": _canonical_json(EXPECTED_RULE_COUNTS),
            "mismatches_json": _canonical_json(n100["mismatches"]),
        }
        for key, value in expected.items():
            if derivation[0].get(key) != value:
                failures.append("derivation_contract:" + key)
    return sorted(set(failures))


def _expected_instrument_payload(vocabulary_sha: str) -> dict[str, Any]:
    return {
        "instrument_version": INSTRUMENT_VERSION,
        "observation_keys": list(OBSERVATION_KEYS),
        "controlled_values": {axis: list(CONTROLLED_VALUES[axis]) for axis in CONTROLLED_KEYS},
        "basis": {"type": "trimmed_non_empty_text", "required": True},
        "instructions": _axis_instructions(),
        "forbidden_input_fields": sorted({
            "label", "allowed_labels", "final_label", "adjudicator_label", "rationale",
            "hard_conflict_observed", "derived_label", "derivation_rule", "candidate_id",
            "group", "group_code", "n100_sample_id", "n100_gold_label", "gold",
            "v1_probe_label", "v2_derived_label", "v21_derived_label", "gate", "score",
        }),
        "vocabulary_contract_sha256": vocabulary_sha,
        "derivation_contract": {"rules_sha256": _record_sha(derivation_rules()),
                                "matches": 102, "denominator": 105, "minimum_matches": 100},
        "source": {"run_id": SOURCE_RUN_ID, "sample_version": SOURCE_SAMPLE_VERSION,
                   "seed": SOURCE_SEED, "logical_sha256": SOURCE_LOGICAL_SHA256,
                   "packet_binding_sha256": SOURCE_PACKET_SHA256,
                   "visual_inventory_sha256": SOURCE_INVENTORY_SHA256,
                   "rows": 24, "sheets": 48, "asset_occurrences": 93},
        "limits": {"max_assets_per_side": 2, "max_asset_occurrences": 96,
                   "incremental_http": 0, "incremental_wire": 0, "incremental_retries": 0,
                   "incremental_redirects": 0, "incremental_renderer_substitutions": 0,
                   "incremental_cache_writes": 0},
    }


def _validate_contract_pins(root: Path, connection: sqlite3.Connection,
                            verify_full_hashes: bool) -> list[str]:
    failures: list[str] = []
    pins = _rows(connection, "SELECT * FROM input_pins ORDER BY role")
    by_role = {str(row.get("role")): row for row in pins}
    for role, row in by_role.items():
        path = _resolve(root, str(row.get("path", "")))
        if not path.is_file():
            failures.append(f"pin:{role}:missing")
            continue
        if path.stat().st_size != int(row.get("size_bytes", -1)):
            failures.append(f"pin:{role}:bytes")
        if verify_full_hashes and _sha256_file(path) != row.get("sha256"):
            failures.append(f"pin:{role}:sha")
    fixed = {
        "n100_source_db": (*N100_SPEC, DEFAULT_N100_DB.as_posix()),
        "wp27_source_db": (*WP27_SPEC, DEFAULT_WP27_DB.as_posix()),
        "wp28_order": (4_291, "1c57a6f2dca384c1ea84db671c8fbb0f150adf76e37707cee5871a1de3267ff9",
                       DEFAULT_ORDER.as_posix()),
    }
    for role, (size, digest, path) in fixed.items():
        row = by_role.get(role)
        if row is None or (row.get("path"), row.get("size_bytes"), row.get("sha256")) != (path, size, digest):
            failures.append("pin:" + role)
    contracts = _rows(connection, "SELECT * FROM instrument_contract")
    vocab = _rows(connection, "SELECT vocabulary_sha256 FROM vocabulary_contract")
    if len(contracts) != 1 or len(vocab) != 1:
        failures.append("instrument_contract:row_count")
    else:
        expected = _expected_instrument_payload(str(vocab[0]["vocabulary_sha256"]))
        row = contracts[0]
        if row.get("contract_json") != _canonical_json(expected):
            failures.append("instrument_contract:json")
        if row.get("contract_sha256") != _record_sha(expected):
            failures.append("instrument_contract:sha")
        protocol = by_role.get("wp28_protocol")
        if protocol is None or row.get("protocol_path") != protocol.get("path") or row.get("protocol_sha256") != protocol.get("sha256"):
            failures.append("instrument_contract:protocol_pin")
        serialized = _canonical_json(expected)
        for invented in ("component_vs_umbrella", "phase_or_extension", "unknown"):
            if invented in serialized:
                failures.append("invented_vocab:" + invented)
    return sorted(set(failures))


def _validate_source_copies(connection: sqlite3.Connection, run_id: str,
                            source: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    specs = {
        "source_samples": (source["samples"], ("sample_rank", "sample_id", "seed_rank_sha256",
                            "control_commitment_sha256", "snapshot_sha256"), "source_pair_record_sha256"),
        "source_sheets": (source["sheets"], ("lane", "display_order", "opaque_review_id",
                           "sample_id", "sheet_path", "sheet_sha256", "sheet_bytes"), "source_record_sha256"),
        "source_assets": (source["assets"], ("sample_id", "side", "asset_order", "asset_key",
                           "source", "source_building_id", "source_asset_id", "selection_reason"), "source_record_sha256"),
        "source_fetch_assets": (source["fetch_assets"], tuple(key for key in source["fetch_assets"][0]
                                if key not in {"run_id", "record_sha256", "source_record_sha256"}), "source_record_sha256"),
        "source_fetch_attempts": (source["fetch_attempts"], tuple(key for key in source["fetch_attempts"][0]
                                  if key not in {"run_id", "record_sha256", "source_record_sha256"}), "source_record_sha256"),
        "source_cache_pins": (source["cache_pins"], tuple(key for key in source["cache_pins"][0]
                              if key not in {"run_id", "record_sha256", "source_record_sha256"}), "source_record_sha256"),
    }
    for table, (source_rows, keys, source_sha_key) in specs.items():
        dest_rows = _rows(connection, f'SELECT * FROM "{table}"')
        if len(dest_rows) != len(source_rows):
            failures.append(table + ":row_count")
            continue
        def identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
            return tuple(row.get(key) for key in keys)
        source_by = {identity(row): row for row in source_rows}
        for row in dest_rows:
            original = source_by.get(identity(row))
            if original is None or row.get(source_sha_key) != original.get("record_sha256"):
                failures.append(table + ":lineage")
    return sorted(set(failures))


def _expected_gate_payload(eligible: Sequence[str]) -> dict[str, Any]:
    return {
        "gate_version": GATE_VERSION,
        "predicates": [
            {"code": "d_image_same_object", "denominator": 15, "threshold": 14,
             "condition": "group=D AND v21_final_image=same_object"},
            {"code": "d_scope_same", "denominator": 15, "threshold": 14,
             "condition": "group=D AND v21_final_scope=same"},
            {"code": "n100_image_non_same_object", "denominator": 5, "threshold": 3,
             "condition": "frozen_N100_image IN (inconclusive,unavailable) AND v21_final_image!=same_object"},
            {"code": "p1_non_same_label", "denominator": 3, "threshold": 2,
             "condition": "group=P1 AND v21_derived_label!=same"},
        ],
        "eligible_sample_ids": list(eligible), "combiner": "all",
    }


def _validate_state_quota(connection: sqlite3.Connection, *, phase: str) -> list[str]:
    failures: list[str] = []
    events = _rows(connection, "SELECT * FROM state_events ORDER BY event_no")
    states = [str(row.get("to_status", "")) for row in events]
    if phase == "sample":
        expected = PRE_TERMINAL_STATES[:5]
    else:
        terminal = _rows(connection, "SELECT status FROM terminal")
        status = str(terminal[0]["status"]) if len(terminal) == 1 else ""
        expected = (*PRE_TERMINAL_STATES, status)
        if status not in TERMINAL_STATES:
            failures.append("terminal_status")
    if tuple(states) != tuple(expected):
        failures.append("state_sequence")
    if [int(row.get("event_no", -1)) for row in events] != list(range(1, len(events) + 1)):
        failures.append("event_numbers")
    for index, row in enumerate(events):
        expected_from = "none" if index == 0 else states[index - 1]
        if row.get("from_status") != expected_from:
            failures.append(f"event:{index + 1}:from")
    if len(events) >= 5:
        try:
            detail = json.loads(str(events[4]["detail_json"]))
        except (TypeError, json.JSONDecodeError):
            detail = {}
            failures.append("criteria_detail_json")
        if {key: detail.get(key) for key in ("form_artifacts", "reviews", "adjudication")} != {
            "form_artifacts": 0, "reviews": 0, "adjudication": 0,
        }:
            failures.append("criteria_zero_counts")
    run = _rows(connection, "SELECT * FROM runs")
    if len(run) != 1 or not states or run[0].get("status") != states[-1]:
        failures.append("run_status")
    previous: datetime | None = None
    for index, row in enumerate(events, 1):
        try:
            current = _iso(row.get("occurred_at"))
            if previous is not None and current < previous:
                failures.append(f"event:{index}:time")
            previous = current
        except (TypeError, ValueError):
            failures.append(f"event:{index}:time")
    quota = _rows(connection, "SELECT * FROM quota_observations ORDER BY observation_no")
    expected_phases = QUOTA_PHASES if phase == "final" else QUOTA_PHASES[:1]
    failures.extend("quota:" + item for item in quota_ledger_failures(quota, expected_phases=expected_phases))
    return sorted(set(failures))


def _validate_artifacts(root: Path, connection: sqlite3.Connection, *, phase: str,
                        hidden_ids: Iterable[str], verify_full_hashes: bool) -> tuple[dict[str, Path], list[str]]:
    failures: list[str] = []
    paths: dict[str, Path] = {}
    rows = _rows(connection, "SELECT * FROM artifacts ORDER BY role")
    bindings = {str(row["role"]): row for row in _rows(connection, "SELECT * FROM artifact_bindings")}
    path_rows = {str(row["role"]): row for row in _rows(connection, "SELECT * FROM artifact_paths")}
    blind_roles = {"r1_form", "r2_form", "r1_completed", "r1_locked", "r2_completed", "r2_locked",
                   "adjudication_packet", "adjudication_form", "adjudication_completed",
                   "adjudication_locked"}
    for row in rows:
        role = str(row.get("role"))
        path = _resolve(root, str(row.get("path", "")))
        paths[role] = path
        try:
            path.relative_to(root)
        except ValueError:
            failures.append(role + ":external_path")
        try:
            raw = bytes.fromhex(str(row.get("payload_hex", "")))
        except ValueError:
            failures.append(role + ":payload_hex")
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != int(row.get("size_bytes", -1)) or digest != row.get("sha256"):
            failures.append(role + ":embedded_binding")
        if not path.is_file() or path.read_bytes() != raw:
            failures.append(role + ":file_bytes")
        binding = bindings.get(role)
        pointer = path_rows.get(role)
        if binding is None or any(binding.get(key) != row.get(key) for key in ("path", "size_bytes", "sha256", "stage")):
            failures.append(role + ":binding")
        if pointer is None or pointer.get("path") != row.get("path") or pointer.get("stage") != row.get("stage"):
            failures.append(role + ":path_row")
        if role in blind_roles:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                failures.append(role + ":json")
            else:
                leaks = blind_json_leaks(payload, hidden_identifiers=hidden_ids)
                if leaks:
                    failures.append(role + ":leaks:" + ",".join(leaks[:5]))
                instructions = payload.get("instructions") if isinstance(payload, Mapping) else None
                if instructions is not None and instructions != _axis_instructions():
                    failures.append(role + ":instructions")
    return paths, sorted(set(failures))


def _validate_actor_rows(connection: sqlite3.Connection) -> list[str]:
    failures: list[str] = []
    reviews = _rows(connection, "SELECT * FROM review_observations ORDER BY lane,display_order")
    if len(reviews) != 48 or Counter(str(row.get("lane")) for row in reviews) != {"r1": 24, "r2": 24}:
        failures.append("reviews:counts")
    for row in reviews:
        observation = {key: row.get(key) for key in OBSERVATION_KEYS}
        if observation_failures(observation):
            failures.append("reviews:axes")
        semantic = {"lane": row.get("lane"), "opaque_review_id": row.get("opaque_review_id"),
                    "sample_id": row.get("sample_id"), **observation,
                    "reviewer": row.get("reviewer"), "reviewed_at": row.get("reviewed_at")}
        if row.get("observation_sha256") != _record_sha(semantic):
            failures.append("reviews:observation_sha")
    review_locks = _rows(connection, "SELECT * FROM review_locks ORDER BY lane")
    actors = [str(row.get("reviewer", "")).strip().casefold() for row in review_locks]
    if len(review_locks) != 2 or len(set(actors)) != 2 or any(not actor for actor in actors):
        failures.append("reviewers:distinct")
    finals = _rows(connection, "SELECT * FROM adjudicated_observations ORDER BY sample_id")
    if len(finals) != 24:
        failures.append("adjudication:count")
    for row in finals:
        observation = {key: row.get(key) for key in OBSERVATION_KEYS}
        if observation_failures(observation):
            failures.append("adjudication:axes")
            continue
        label, rule = derive_label(observation)
        if row.get("derived_label") != label or int(row.get("derivation_rule", -1)) != rule:
            failures.append("adjudication:derivation")
        try:
            adjustments = json.loads(str(row.get("axis_adjustments_json")))
        except json.JSONDecodeError:
            failures.append("adjudication:adjustments")
            adjustments = None
        semantic = {"opaque_adjudication_id": row.get("opaque_adjudication_id"),
                    "sample_id": row.get("sample_id"), **observation,
                    "derived_label": label, "derivation_rule": rule,
                    "axis_adjustments": adjustments, "adjudicator": row.get("adjudicator"),
                    "adjudicated_at": row.get("adjudicated_at")}
        if row.get("observation_sha256") != _record_sha(semantic):
            failures.append("adjudication:observation_sha")
    adjudication_lock = _rows(connection, "SELECT * FROM adjudication_lock")
    adjudicator = str(adjudication_lock[0].get("adjudicator", "")).strip().casefold() if len(adjudication_lock) == 1 else ""
    if not adjudicator or adjudicator in set(actors):
        failures.append("adjudicator:distinct")
    return sorted(set(failures))


def _validate_gate_reveal(connection: sqlite3.Connection, source: Mapping[str, Any],
                          n100: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    source_by_sample = {str(row["sample_id"]): row for row in source["reveals"]}
    final_by_sample = {str(row["sample_id"]): row for row in
                       _rows(connection, "SELECT * FROM adjudicated_observations")}
    reveals = _rows(connection, "SELECT * FROM control_reveals ORDER BY sample_id")
    if len(reveals) != 24 or set(str(row["sample_id"]) for row in reveals) != set(source_by_sample):
        failures.append("reveals:sample_set")
    gate_rows: list[dict[str, Any]] = []
    for row in reveals:
        sample_id = str(row.get("sample_id")); old = source_by_sample.get(sample_id, {})
        final = final_by_sample.get(sample_id, {})
        observation = {key: final.get(key) for key in OBSERVATION_KEYS}
        try:
            label, rule = derive_label(observation)
        except ValueError:
            continue
        n100_id = str(old.get("n100_sample_id", ""))
        pair = n100["by_sample"].get(n100_id, {})
        n100_axes = {
            "r1": {key: pair.get("r1", {}).get(key) for key in OBSERVATION_KEYS},
            "r2": {key: pair.get("r2", {}).get(key) for key in OBSERVATION_KEYS},
            "consensus": n100["consensus"].get(n100_id, {}),
        }
        expected = {
            "candidate_id": old.get("candidate_id"), "group_code": old.get("group_code"),
            "n100_sample_id": n100_id, "n100_axes_json": _canonical_json(n100_axes),
            "n100_gold_label": old.get("n100_gold_label"), "v1_probe_label": old.get("v1_probe_label"),
            "v2_derived_label": old.get("v2_derived_label"), "v21_derived_label": label,
            "derivation_rule_no": rule, "commitment_sha256": old.get("commitment_sha256"),
        }
        for key, value in expected.items():
            if row.get(key) != value:
                failures.append(f"reveal:{sample_id}:{key}")
        semantic = {
            "sample_id": sample_id, "candidate_id": expected["candidate_id"],
            "group_code": expected["group_code"], "n100_sample_id": n100_id,
            "n100_axes": n100_axes, "n100_gold_label": expected["n100_gold_label"],
            "v1_probe_label": expected["v1_probe_label"],
            "v2_derived_label": expected["v2_derived_label"],
            "v21_derived_label": label, "derivation_rule_no": rule,
            "control_commitment_sha256": expected["commitment_sha256"],
            "revealed_at": row.get("revealed_at"),
        }
        if row.get("reveal_payload_sha256") != _record_sha(semantic):
            failures.append(f"reveal:{sample_id}:payload_sha")
        gate_rows.append({"group_code": old.get("group_code"), "image": final.get("image"),
                          "scope": final.get("scope"), "derived_label": label,
                          "n100_uncertain": n100_id in EXPECTED_UNCERTAIN_N100_IDS})
    try:
        gate = gate_from_rows(gate_rows)
    except ValueError as exc:
        return {}, failures + ["gate:" + str(exc)]
    eligible = sorted(sample_id for sample_id, row in source_by_sample.items()
                      if str(row["n100_sample_id"]) in EXPECTED_UNCERTAIN_N100_IDS)
    contract = _rows(connection, "SELECT * FROM gate_contract")
    expected_gate_payload = _expected_gate_payload(eligible)
    if len(contract) != 1:
        failures.append("gate_contract:row_count")
    else:
        expected_scalars = {
            "gate_json": _canonical_json(expected_gate_payload), "gate_sha256": _record_sha(expected_gate_payload),
            "d_image_min": 14, "d_image_n": 15, "d_scope_min": 14, "d_scope_n": 15,
            "n100_image_non_same_min": 3, "n100_image_non_same_n": 5,
            "p1_min_non_same": 2, "p1_n": 3,
            "eligible_sample_ids_json": _canonical_json(eligible),
        }
        for key, value in expected_scalars.items():
            if contract[0].get(key) != value:
                failures.append("gate_contract:" + key)
    score_rows = {str(row["predicate"]): row for row in _rows(connection, "SELECT * FROM gate_scores")}
    if set(score_rows) != set(GATE_THRESHOLDS):
        failures.append("gate_scores:predicates")
    for code, expected in gate["predicates"].items():
        row = score_rows.get(code, {})
        for key in ("actual", "denominator", "threshold"):
            if row.get(key) != expected[key]:
                failures.append(f"gate_scores:{code}:{key}")
        if bool(row.get("passed")) != bool(expected["passed"]):
            failures.append(f"gate_scores:{code}:passed")
    terminal = _rows(connection, "SELECT status FROM terminal")
    if len(terminal) != 1 or terminal[0].get("status") != gate["status"]:
        failures.append("terminal:gate_status")
    return gate, sorted(set(failures))


def _validate_terminal(root: Path, connection: sqlite3.Connection,
                       report_path: Path, manifest_path: Path,
                       gate: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    try:
        report_raw, manifest_raw = report_path.read_bytes(), manifest_path.read_bytes()
        report, manifest = json.loads(report_raw), json.loads(manifest_raw)
    except (OSError, json.JSONDecodeError):
        return ["terminal_json"]
    terminal = _rows(connection, "SELECT * FROM terminal")
    if len(terminal) != 1:
        return ["terminal_row_count"]
    row = terminal[0]
    expected = {
        "report_sha256": hashlib.sha256(report_raw).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "report_json": _canonical_json(report), "manifest_json": _canonical_json(manifest),
    }
    for key, value in expected.items():
        if row.get(key) != value:
            failures.append("terminal:" + key)
    if _resolve(root, str(row.get("report_path", ""))) != report_path.resolve():
        failures.append("terminal:report_path")
    if _resolve(root, str(row.get("manifest_path", ""))) != manifest_path.resolve():
        failures.append("terminal:manifest_path")
    if report.get("gate") != gate:
        failures.append("report:gate")
    if report.get("instrument_version") != INSTRUMENT_VERSION:
        failures.append("report:instrument_version")
    stage_a = report.get("stage_a_vocabulary", {})
    if not isinstance(stage_a, Mapping) or stage_a.get("source_rows") != 210 or stage_a.get("basis_nonblank_count") != 210:
        failures.append("report:stage_a")
    stage_b = report.get("stage_b_derivation", {})
    if not isinstance(stage_b, Mapping) or (
        stage_b.get("numerator"), stage_b.get("denominator"), stage_b.get("minimum_matches"),
        stage_b.get("rule_counts")
    ) != (102, 105, 100, EXPECTED_RULE_COUNTS):
        failures.append("report:stage_b")
    if not isinstance(report.get("four_way_comparison"), list) or len(report["four_way_comparison"]) != 24:
        failures.append("report:four_way")
    if manifest.get("counts") != {"samples": 24, "sheets": 48, "asset_occurrences": 93,
                                  "unique_assets": 93}:
        failures.append("manifest:counts")
    network = manifest.get("network")
    if not isinstance(network, Mapping) or any(network.get(key) != 0 for key in
            ("incremental_logical", "incremental_wire", "redirects", "retries")):
        failures.append("manifest:network")
    for payload_name, payload in (("report", report), ("manifest", manifest)):
        for key in ("vision_requests", "llm_requests", "paid_api_requests", "merge_executions",
                    "vnext_id_issuances", "tier_promotions", "neon_writes", "r2_writes"):
            if payload.get(key) != 0:
                failures.append(f"{payload_name}:{key}")
    serialized_manifest = _canonical_json(manifest)
    for token in ("n100_gold_label", "v1_probe_label", "v2_derived_label", "candidate_id"):
        if token in serialized_manifest:
            failures.append("manifest:reveal:" + token)
    return sorted(set(failures))


def _validate_time_barriers(connection: sqlite3.Connection) -> list[str]:
    failures: list[str] = []
    events = _rows(connection, "SELECT * FROM state_events ORDER BY event_no")
    event_times: dict[str, datetime] = {}
    for row in events:
        try:
            event_times[str(row["to_status"])] = _iso(row["occurred_at"])
        except (TypeError, ValueError):
            pass
    criteria = event_times.get("criteria_frozen")
    lock_event = event_times.get("adjudication_locked")
    gate = _rows(connection, "SELECT declared_at FROM gate_contract")
    try:
        if len(gate) != 1 or criteria is None or _iso(gate[0]["declared_at"]) > criteria:
            failures.append("gate_before_criteria")
    except (TypeError, ValueError):
        failures.append("gate_before_criteria")
    for row in _rows(connection, "SELECT role,created_at FROM artifacts"):
        role = str(row["role"])
        if role == "sample_manifest":
            continue
        try:
            if criteria is None or _iso(row["created_at"]) < criteria:
                failures.append(role + ":before_criteria")
        except (TypeError, ValueError):
            failures.append(role + ":timestamp")
    for row in _rows(connection, "SELECT sample_id,revealed_at FROM control_reveals"):
        try:
            if lock_event is None or _iso(row["revealed_at"]) < lock_event:
                failures.append(str(row["sample_id"]) + ":reveal_before_lock")
        except (TypeError, ValueError):
            failures.append(str(row["sample_id"]) + ":reveal_time")
    return sorted(set(failures))


def _validate_operational_zero(connection: sqlite3.Connection,
                               report: Mapping[str, Any] | None = None,
                               manifest: Mapping[str, Any] | None = None) -> list[str]:
    failures: list[str] = []
    run = _rows(connection, "SELECT * FROM runs")
    if len(run) != 1:
        return ["run_count"]
    for key in ("incremental_logical_http_fetches", "incremental_wire_http_requests",
                "incremental_retries", "incremental_redirects", "incremental_renderer_substitutions",
                "incremental_cache_writes"):
        if run[0].get(key) != 0:
            failures.append("run:" + key)
    for name, payload in (("report", report), ("manifest", manifest)):
        if payload is None:
            continue
        for key in ("vision_requests", "llm_requests", "paid_api_requests", "merge_executions",
                    "vnext_id_issuances", "tier_promotions", "neon_writes", "r2_writes"):
            if payload.get(key) != 0:
                failures.append(name + ":" + key)
    return sorted(set(failures))


def _write_validation_report(path: Path, *, checks: Sequence[Check], metrics: Mapping[str, Any]) -> None:
    payload = {
        "status": "PASS" if all(check.passed for check in checks) else "FAIL",
        "validator_version": VALIDATOR_VERSION,
        "checks": [{"name": check.name, "passed": check.passed,
                    "expected": check.expected, "actual": check.actual} for check in checks],
        "metrics": metrics,
    }
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != raw:
        raise FileExistsError(f"refusing to clobber validation report: {path}")
    path.write_bytes(raw)


def validate_identity_instrument_v21_probe(
    *, repo_root: Path, artifact_path: Path = DEFAULT_ARTIFACT,
    report_path: Path = DEFAULT_REPORT, manifest_path: Path = DEFAULT_MANIFEST,
    validation_report_path: Path | None = DEFAULT_VALIDATION_REPORT,
    phase: str = "final", verify_full_hashes: bool = True,
) -> ValidationResult:
    """Validate one v2.1 sidecar without importing or executing its producer."""

    if phase not in {"sample", "final"}:
        raise ValueError("phase must be sample or final")
    root = repo_root.resolve()
    artifact = _resolve(root, artifact_path)
    report_file = _resolve(root, report_path)
    manifest_file = _resolve(root, manifest_path)
    output = _resolve(root, validation_report_path) if validation_report_path is not None else None
    checks: list[Check] = []
    metrics: dict[str, Any] = {}
    if not artifact.is_file():
        _check(checks, "artifact_exists", True, False)
        if output is not None:
            _write_validation_report(output, checks=checks, metrics=metrics)
        return ValidationResult(False, tuple(checks), metrics, output)
    _check(checks, "artifact_exists", True, True)
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_readonly(artifact)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        _check(checks, "sqlite_integrity", "ok", integrity)
        if integrity != "ok":
            raise ValueError("artifact integrity")
        _check(checks, "sqlite_application_id", APPLICATION_ID,
               int(connection.execute("PRAGMA application_id").fetchone()[0]))
        _check(checks, "sqlite_user_version", USER_VERSION,
               int(connection.execute("PRAGMA user_version").fetchone()[0]))
        _check(checks, "sqlite_sidecars_absent", [], _sqlite_sidecars(artifact))
        tables = _tables(connection)
        _check(checks, "required_schema_tables", [], sorted(REQUIRED_TABLES - tables))
        column_failures = [table for table, columns in CRITICAL_COLUMNS.items()
                           if table in tables and _columns(connection, table) != columns]
        _check(checks, "critical_schema_columns_exact", [], column_failures)
        if not REQUIRED_TABLES <= tables:
            raise ValueError("required schema missing")
        _check(checks, "record_hashes_full_row_replay", [], _record_hash_failures(connection))
        foreign = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        _check(checks, "foreign_key_check", [], foreign)
    except Exception as exc:
        _check(checks, "validator_execution", "completed", f"{type(exc).__name__}:{exc}")
        if connection is not None:
            connection.close()
        if output is not None:
            _write_validation_report(output, checks=checks, metrics=metrics)
        return ValidationResult(False, tuple(checks), metrics, output)

    try:
        n100 = _n100_snapshot(root, verify_full_hashes=verify_full_hashes)
        _check(checks, "n100_pin_integrity_210_exact_parse_vocab_gold_replay", "valid", "valid")
        source = _wp27_snapshot(root, n100, verify_full_hashes=verify_full_hashes)
        _check(checks, "wp27_exact_24_48_93_packet_inventory_sheet_files", "valid", "valid")
        run_rows = _rows(connection, "SELECT * FROM runs")
        run = run_rows[0] if len(run_rows) == 1 else {}
        run_id = str(run.get("run_id", ""))
        expected_run = {
            "schema_version": 1, "runner_version": RUNNER_VERSION,
            "instrument_version": INSTRUMENT_VERSION, "sample_version": SAMPLE_VERSION,
            "review_version": REVIEW_VERSION, "adjudication_version": ADJUDICATION_VERSION,
            "gate_version": GATE_VERSION, "source_sample_version": SOURCE_SAMPLE_VERSION,
            "source_seed": SOURCE_SEED, "source_logical_sha256": SOURCE_LOGICAL_SHA256,
            "source_run_id": SOURCE_RUN_ID, "source_packet_binding_sha256": SOURCE_PACKET_SHA256,
            "source_visual_inventory_sha256": SOURCE_INVENTORY_SHA256,
        }
        run_failures = [key for key, value in expected_run.items() if run.get(key) != value]
        _check(checks, "run_versions_and_source_contract", [], run_failures)
        _check(checks, "n100_persisted_axes_vocab_consensus_102_of_105",
               [], _validate_n100_persistence(connection, run_id, n100))
        _check(checks, "input_pins_protocol_hash_neutral_four_image_definitions",
               [], _validate_contract_pins(root, connection, verify_full_hashes))
        _check(checks, "wp27_source_rows_exact_copy_lineage",
               [], _validate_source_copies(connection, run_id, source))
        _check(checks, "stage_order_criteria_zero_and_quota_sticky",
               [], _validate_state_quota(connection, phase=phase))
        hidden_ids = [str(row["candidate_id"]) for row in source["reveals"]]
        hidden_ids.extend(str(row["n100_sample_id"]) for row in source["reveals"])
        paths, artifact_failures = _validate_artifacts(
            root, connection, phase=phase, hidden_ids=hidden_ids,
            verify_full_hashes=verify_full_hashes,
        )
        _check(checks, "artifact_file_byte_sha_and_blind_leak_bindings", [], artifact_failures)
        _check(checks, "incremental_http_cache_renderer_and_external_writes_zero",
               [], _validate_operational_zero(connection))
        if phase == "final":
            _check(checks, "actors_distinct_axes_exact_locks_and_observation_hashes",
                   [], _validate_actor_rows(connection))
            _check(checks, "criteria_and_reveal_after_adjudication_lock",
                   [], _validate_time_barriers(connection))
            gate, gate_failures = _validate_gate_reveal(connection, source, n100)
            _check(checks, "four_d_gates_independent_replay", [], gate_failures)
            terminal_failures = _validate_terminal(root, connection, report_file, manifest_file, gate)
            _check(checks, "report_manifest_embedded_json_byte_sha", [], terminal_failures)
            try:
                report = json.loads(report_file.read_text(encoding="utf-8"))
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = manifest = None
            _check(checks, "terminal_operational_zero", [],
                   _validate_operational_zero(connection, report, manifest))
            metrics["gate"] = gate
        metrics.update({"n100_matches": n100["matches"], "n100_mismatch_count": 3,
                        "n100_rule_counts": EXPECTED_RULE_COUNTS,
                        "source_counts": {"samples": 24, "sheets": 48, "assets": 93}})
    except Exception as exc:
        _check(checks, "validator_execution", "completed", f"{type(exc).__name__}:{exc}")
    finally:
        connection.close()
    if output is not None:
        _write_validation_report(output, checks=checks, metrics=metrics)
    return ValidationResult(all(check.passed for check in checks), tuple(checks), metrics, output)


__all__ = [
    "Check", "ValidationResult", "CONTROLLED_VALUES", "CONSENSUS_PRIORITY",
    "EXPECTED_RULE_COUNTS", "EXPECTED_MISMATCHES", "GATE_THRESHOLDS",
    "parse_n100_rationale", "observation_failures", "consensus_axes", "derive_label",
    "derivation_rules", "gate_from_rows", "blind_json_leaks", "quota_ledger_failures",
    "validate_identity_instrument_v21_probe",
]
