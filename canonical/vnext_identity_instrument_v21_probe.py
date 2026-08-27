"""WP2.8 identity instrument v2.1 observation-layer calibration.

This module is deliberately offline.  It parses the immutable N100 reviewer
rationales, freezes a deterministic consensus/derivation contract, and then
replays the already-rendered WP2.6/WP2.7 24-row packet without fetching,
rendering, or mutating any upstream artifact.

The mature WP2.7 actor-ledger implementation is reused only for the generic
review/adjudication mechanics.  Every v2.1 vocabulary, instruction, derivation,
state, gate, reveal, and report contract is defined here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical import vnext_identity_instrument_v2_probe as v2


SCHEMA_VERSION = 1
APPLICATION_ID = 0x57503238  # WP28
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
N100_DB_SIZE = 2_183_168
N100_DB_SHA256 = "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b"
N100_REVIEW_ROWS = 210
N100_GOLD_ROWS = 105
EXPECTED_DERIVATION_MATCHES = 102
DERIVATION_MIN_MATCHES = 100

SOURCE_SAMPLE_VERSION = v2.SOURCE_SAMPLE_VERSION
SOURCE_SEED = v2.SOURCE_SEED
SOURCE_LOGICAL_SHA256 = v2.SOURCE_LOGICAL_SHA256
SOURCE_RUN_ID = v2.SOURCE_RUN_ID
SOURCE_PACKET_BINDING_SHA256 = v2.SOURCE_PACKET_BINDING_SHA256
SOURCE_VISUAL_INVENTORY_SHA256 = v2.SOURCE_VISUAL_INVENTORY_SHA256
WP27_RUN_ID = "wp27-d49c5111f27a5f59ad3fd754"
WP27_DB_SIZE = 1_695_744
WP27_DB_SHA256 = "f31ee6558e43c9b0b412f2c3cda964100acffe0763f9995d301fb32865dece65"

TOTAL_COUNT = 24
SOURCE_SHEET_COUNT = 48
SOURCE_ASSET_OCCURRENCES = 93
MAX_ASSETS_PER_SIDE = 2
MAX_ASSET_OCCURRENCES = 96
MAX_LOGICAL_HTTP_FETCHES = 96
MAX_WIRE_HTTP_REQUESTS = 96

OBSERVATION_KEYS = ("image", "name", "firm", "place", "year", "scope", "basis")
CONTROLLED_KEYS = OBSERVATION_KEYS[:-1]
CONTROLLED_VALUES: Mapping[str, tuple[str, ...]] = {
    "image": ("same_object", "different_objects", "inconclusive", "unavailable"),
    "name": ("agree", "alias", "conflict", "unclear"),
    "firm": ("agree", "overlap", "conflict", "unclear"),
    "place": ("agree", "conflict", "unclear"),
    "year": ("compatible", "conflict", "unclear"),
    "scope": ("same", "related_separate", "unclear", "unrelated"),
}
CONSENSUS_PRIORITY: Mapping[str, tuple[str, ...]] = {
    "image": ("unavailable", "inconclusive", "different_objects", "same_object"),
    "name": ("conflict", "alias", "agree", "unclear"),
    "firm": ("conflict", "overlap", "agree", "unclear"),
    "place": ("conflict", "agree", "unclear"),
    "year": ("conflict", "compatible", "unclear"),
    "scope": ("unrelated", "related_separate", "same", "unclear"),
}
EXPECTED_VOCAB_COUNTS: Mapping[str, Mapping[str, int]] = {
    "image": {"different_objects": 24, "inconclusive": 12,
              "same_object": 171, "unavailable": 3},
    "name": {"agree": 129, "alias": 73, "conflict": 4, "unclear": 4},
    "firm": {"agree": 147, "conflict": 37, "overlap": 24, "unclear": 2},
    "place": {"agree": 25, "conflict": 1, "unclear": 184},
    "year": {"compatible": 162, "conflict": 31, "unclear": 17},
    "scope": {"related_separate": 2, "same": 155, "unclear": 28,
              "unrelated": 25},
}
EXPECTED_RULE_COUNTS = {"1": 11, "2": 10, "3": 1, "4": 83, "5": 0}
EXPECTED_MISMATCHES = (
    ("n100_5c76a0ad9d55e899c66ffb8a", "same", "related"),
    ("n100_b8398cb6c0618c78b88a1576", "same", "insufficient"),
    ("n100_c8789d856720d9cb7c7c2a5d", "same", "insufficient"),
)

ALLOWED_LABELS = frozenset({"same", "related", "different", "insufficient"})
FORBIDDEN_INPUT_FIELDS = frozenset({
    "label", "allowed_labels", "final_label", "adjudicator_label", "rationale",
    "hard_conflict_observed", "derived_label", "derivation_rule", "candidate_id",
    "group", "group_code", "n100_sample_id", "n100_gold_label", "gold",
    "v1_probe_label", "v2_derived_label", "v21_derived_label", "gate", "score",
})
QUOTA_PHASES = v2.QUOTA_PHASES
QUOTA_INDEX = v2.QUOTA_INDEX

STATE_SEQUENCE = (
    "vocabulary_frozen", "derivation_verified", "instrument_frozen",
    "sample_frozen", "criteria_frozen", "review_ready", "reviews_partial",
    "reviews_locked", "adjudication_locked", "pass", "fail",
)

DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_INSTRUMENT_V21_PROTOCOL.md")
DEFAULT_ORDER = Path(".claude/ops/jobs/20260827_vnext_wp28_instrument_v21_order.md")
DEFAULT_N100_DB = Path("data/enrichment/vnext_identity_n100_v1.db")
DEFAULT_SOURCE_DB = Path("data/enrichment/vnext_identity_instrument_v2_probe_v1.db")
DEFAULT_SOURCE_REPORT = Path("data/reports/vnext_identity_instrument_v2_probe_v1_report.json")
DEFAULT_SOURCE_MANIFEST = Path("data/reports/vnext_identity_instrument_v2_probe_v1_manifest.json")
DEFAULT_SOURCE_VALIDATION = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_final_validation_v4.json"
)
DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_instrument_v21_probe_v1.db")
DEFAULT_SAMPLE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_sample_manifest.json"
)
DEFAULT_FETCH_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_fetch_manifest.json"
)
DEFAULT_FETCH_REPORT = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_fetch_report.json"
)
DEFAULT_R1_FORM = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_reviewer_r1_draft.json"
)
DEFAULT_R2_FORM = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_reviewer_r2_draft.json"
)
DEFAULT_R1_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_reviewer_r1_locked.json"
)
DEFAULT_R2_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_reviewer_r2_locked.json"
)
DEFAULT_ADJUDICATION_PACKET = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_adjudication_packet.json"
)
DEFAULT_ADJUDICATION_FORM = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_adjudication_draft.json"
)
DEFAULT_ADJUDICATION_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v1_adjudication_locked.json"
)
DEFAULT_REPORT = Path("data/reports/vnext_identity_instrument_v21_probe_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_instrument_v21_probe_v1_manifest.json")


class IdentityInstrumentV21Error(RuntimeError):
    """A fail-closed WP2.8 contract violation."""


@dataclass(frozen=True)
class StageResult:
    artifact_path: Path
    run_id: str
    status: str


SampleResult = v2.SampleResult
PrepareResult = v2.PrepareResult
RevealResult = v2.RevealResult


RATIONALE_RE = re.compile(
    r"\Aimage=(?P<image>[a-z_]+); name=(?P<name>[a-z_]+); "
    r"firm=(?P<firm>[a-z_]+); place=(?P<place>[a-z_]+); "
    r"year=(?P<year>[a-z_]+); scope=(?P<scope>[a-z_]+); "
    r"basis=(?P<basis>.+)\Z"
)


def _replace_table(schema: str, table: str, replacement: str) -> str:
    start = schema.index(f"CREATE TABLE {table}(")
    end = schema.index("\n);", start) + 3
    return schema[:start] + replacement.strip() + schema[end:]


_schema = v2.SCHEMA_SQL.replace(
    "'instrument_frozen','sample_frozen',\n  'criteria_frozen','review_ready','reviews_partial','reviews_locked',\n  'adjudication_locked','pass','fail'",
    "'vocabulary_frozen','derivation_verified','derivation_failed',\n  'instrument_frozen','sample_frozen','criteria_frozen','review_ready',\n  'reviews_partial','reviews_locked','adjudication_locked','pass','fail'",
)
_schema = _replace_table(_schema, "gate_contract", """
CREATE TABLE gate_contract(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), gate_json TEXT NOT NULL,
 gate_sha256 TEXT NOT NULL,
 d_image_min INTEGER NOT NULL CHECK(d_image_min=14),
 d_image_n INTEGER NOT NULL CHECK(d_image_n=15),
 d_scope_min INTEGER NOT NULL CHECK(d_scope_min=14),
 d_scope_n INTEGER NOT NULL CHECK(d_scope_n=15),
 n100_image_non_same_min INTEGER NOT NULL CHECK(n100_image_non_same_min=3),
 n100_image_non_same_n INTEGER NOT NULL CHECK(n100_image_non_same_n=5),
 p1_min_non_same INTEGER NOT NULL CHECK(p1_min_non_same=2),
 p1_n INTEGER NOT NULL CHECK(p1_n=3),
 eligible_sample_ids_json TEXT NOT NULL, declared_at TEXT NOT NULL,
 record_sha256 TEXT NOT NULL
);""")
_schema = _replace_table(_schema, "control_reveals", """
CREATE TABLE control_reveals(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
 candidate_id TEXT NOT NULL, group_code TEXT NOT NULL,
 n100_sample_id TEXT NOT NULL, n100_axes_json TEXT NOT NULL,
 n100_gold_label TEXT NOT NULL, v1_probe_label TEXT NOT NULL,
 v2_derived_label TEXT NOT NULL, v21_derived_label TEXT NOT NULL,
 derivation_rule_no INTEGER NOT NULL, commitment_sha256 TEXT NOT NULL,
 revealed_at TEXT NOT NULL, reveal_payload_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id)
);""")
SCHEMA_SQL = _schema + r"""
CREATE TABLE n100_review_axes(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL,
 opaque_review_id TEXT NOT NULL, n100_sample_id TEXT NOT NULL,
 image TEXT NOT NULL, name TEXT NOT NULL, firm TEXT NOT NULL,
 place TEXT NOT NULL, year TEXT NOT NULL, scope TEXT NOT NULL, basis TEXT NOT NULL,
 source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,lane,opaque_review_id), UNIQUE(run_id,lane,n100_sample_id)
);
CREATE TABLE vocabulary_counts(
 run_id TEXT NOT NULL REFERENCES runs(run_id), axis TEXT NOT NULL, value TEXT NOT NULL,
 frequency INTEGER NOT NULL, r1_frequency INTEGER NOT NULL, r2_frequency INTEGER NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,axis,value)
);
CREATE TABLE vocabulary_contract(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), version TEXT NOT NULL,
 parser_regex TEXT NOT NULL, row_count INTEGER NOT NULL,
 vocabulary_json TEXT NOT NULL, vocabulary_sha256 TEXT NOT NULL,
 basis_nonblank_count INTEGER NOT NULL, frozen_at TEXT NOT NULL,
 record_sha256 TEXT NOT NULL
);
CREATE TABLE consensus_contract(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), version TEXT NOT NULL,
 priority_json TEXT NOT NULL, priority_sha256 TEXT NOT NULL,
 frozen_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE n100_consensus_axes(
 run_id TEXT NOT NULL REFERENCES runs(run_id), n100_sample_id TEXT NOT NULL,
 image TEXT NOT NULL, name TEXT NOT NULL, firm TEXT NOT NULL,
 place TEXT NOT NULL, year TEXT NOT NULL, scope TEXT NOT NULL,
 r1_observation_sha256 TEXT NOT NULL, r2_observation_sha256 TEXT NOT NULL,
 consensus_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,n100_sample_id)
);
CREATE TABLE derivation_contract(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), version TEXT NOT NULL,
 rules_json TEXT NOT NULL, rules_sha256 TEXT NOT NULL,
 numerator INTEGER NOT NULL, denominator INTEGER NOT NULL,
 minimum_matches INTEGER NOT NULL, passed INTEGER NOT NULL CHECK(passed IN (0,1)),
 rule_counts_json TEXT NOT NULL, mismatches_json TEXT NOT NULL,
 verified_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TRIGGER n100_axes_no_update BEFORE UPDATE ON n100_review_axes BEGIN SELECT RAISE(ABORT,'N100 axes frozen'); END;
CREATE TRIGGER n100_axes_no_delete BEFORE DELETE ON n100_review_axes BEGIN SELECT RAISE(ABORT,'N100 axes frozen'); END;
CREATE TRIGGER vocabulary_counts_no_update BEFORE UPDATE ON vocabulary_counts BEGIN SELECT RAISE(ABORT,'vocabulary frozen'); END;
CREATE TRIGGER vocabulary_counts_no_delete BEFORE DELETE ON vocabulary_counts BEGIN SELECT RAISE(ABORT,'vocabulary frozen'); END;
CREATE TRIGGER vocabulary_contract_no_update BEFORE UPDATE ON vocabulary_contract BEGIN SELECT RAISE(ABORT,'vocabulary contract frozen'); END;
CREATE TRIGGER vocabulary_contract_no_delete BEFORE DELETE ON vocabulary_contract BEGIN SELECT RAISE(ABORT,'vocabulary contract frozen'); END;
CREATE TRIGGER consensus_contract_no_update BEFORE UPDATE ON consensus_contract BEGIN SELECT RAISE(ABORT,'consensus frozen'); END;
CREATE TRIGGER consensus_contract_no_delete BEFORE DELETE ON consensus_contract BEGIN SELECT RAISE(ABORT,'consensus frozen'); END;
CREATE TRIGGER consensus_axes_no_update BEFORE UPDATE ON n100_consensus_axes BEGIN SELECT RAISE(ABORT,'consensus axes frozen'); END;
CREATE TRIGGER consensus_axes_no_delete BEFORE DELETE ON n100_consensus_axes BEGIN SELECT RAISE(ABORT,'consensus axes frozen'); END;
CREATE TRIGGER derivation_contract_no_update BEFORE UPDATE ON derivation_contract BEGIN SELECT RAISE(ABORT,'derivation frozen'); END;
CREATE TRIGGER derivation_contract_no_delete BEFORE DELETE ON derivation_contract BEGIN SELECT RAISE(ABORT,'derivation frozen'); END;
"""


def _path(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def _path_text(root: Path, value: Path) -> str:
    try:
        return value.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return value.resolve().as_posix()


def _resolve_text(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _record(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(payload))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _atomic_bytes(path: Path, raw: bytes, *, allow_identical: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if allow_identical and path.is_file() and path.read_bytes() == raw:
            return
        raise IdentityInstrumentV21Error(f"refusing to clobber artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise IdentityInstrumentV21Error(f"stale temporary artifact: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            failed = path.with_name(path.name + ".building.failed")
            if not failed.exists():
                os.replace(temporary, failed)
        raise


def _assert_no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(path) + suffix).exists():
            raise IdentityInstrumentV21Error(f"mutable SQLite sidecar exists: {path}{suffix}")


def _open_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise IdentityInstrumentV21Error(f"source DB is missing: {path}")
    _assert_no_sidecars(path)
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise IdentityInstrumentV21Error(f"WP2.8 artifact is missing: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _one_run(connection: sqlite3.Connection) -> sqlite3.Row:
    rows = connection.execute("SELECT * FROM runs").fetchall()
    if len(rows) != 1:
        raise IdentityInstrumentV21Error("artifact must contain exactly one run")
    return rows[0]


def _refresh_run_record(connection: sqlite3.Connection, run_id: str) -> None:
    row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise IdentityInstrumentV21Error("run disappeared")
    payload = {key: row[key] for key in row.keys() if key != "record_sha256"}
    connection.execute("UPDATE runs SET record_sha256=? WHERE run_id=?", (_record(payload), run_id))


def _insert_event(connection: sqlite3.Connection, run_id: str, from_status: str,
                  to_status: str, detail: Mapping[str, Any]) -> None:
    event_no = int(connection.execute(
        "SELECT coalesce(max(event_no),0)+1 FROM state_events WHERE run_id=?", (run_id,)
    ).fetchone()[0])
    occurred_at = utc_now()
    safe_detail = dict(detail)
    if to_status not in {"pass", "fail"}:
        safe_detail.update({"n100_gold_revealed": False, "v1_label_revealed": False,
                            "v2_label_revealed": False})
    detail_json = canonical_json(safe_detail)
    payload = {"run_id": run_id, "event_no": event_no, "from_status": from_status,
               "to_status": to_status, "detail_json": detail_json, "occurred_at": occurred_at}
    connection.execute("INSERT INTO state_events VALUES(?,?,?,?,?,?,?)",
                       (*payload.values(), _record(payload)))


def _file_pin(root: Path, path: Path, usage: str) -> dict[str, Any]:
    absolute = _path(root, path)
    if not absolute.is_file():
        raise IdentityInstrumentV21Error(f"pinned input is missing: {absolute}")
    return {"path": _path_text(root, absolute), "size_bytes": absolute.stat().st_size,
            "sha256": sha256_file(absolute), "usage": usage}


def _insert_pin(connection: sqlite3.Connection, run_id: str, role: str,
                pin: Mapping[str, Any]) -> None:
    payload = {"run_id": run_id, "role": role, **dict(pin)}
    connection.execute("INSERT INTO input_pins VALUES(?,?,?,?,?,?,?)",
                       (*payload.values(), _record(payload)))


def _verify_pins(connection: sqlite3.Connection, root: Path, run_id: str) -> None:
    for row in connection.execute("SELECT * FROM input_pins WHERE run_id=?", (run_id,)):
        path = _resolve_text(root, str(row["path"]))
        if (not path.is_file() or path.stat().st_size != int(row["size_bytes"])
                or sha256_file(path) != str(row["sha256"])):
            raise IdentityInstrumentV21Error(f"pinned input changed: {row['role']}")


def _register_path(connection: sqlite3.Connection, root: Path, run_id: str,
                   role: str, path: Path, stage: str) -> None:
    text = _path_text(root, path)
    payload = {"run_id": run_id, "role": role, "path": text, "stage": stage}
    connection.execute("INSERT INTO artifact_paths VALUES(?,?,?,?,?)",
                       (*payload.values(), _record(payload)))


def _bind_values(connection: sqlite3.Connection, root: Path, run_id: str,
                 role: str, path: Path, stage: str, raw: bytes) -> None:
    text = _path_text(root, path); digest = hashlib.sha256(raw).hexdigest()
    binding = {"run_id": run_id, "role": role, "path": text, "size_bytes": len(raw),
               "sha256": digest, "stage": stage}
    connection.execute("INSERT INTO artifact_bindings VALUES(?,?,?,?,?,?,?)",
                       (*binding.values(), _record(binding)))
    created_at = utc_now()
    artifact = {"run_id": run_id, "role": role, "path": text, "stage": stage,
                "size_bytes": len(raw), "sha256": digest, "payload_hex": raw.hex(),
                "created_at": created_at}
    connection.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                       (*artifact.values(), _record(artifact)))


def _store_json_payload(connection: sqlite3.Connection, run_id: str,
                        role: str, payload: Mapping[str, Any]) -> bytes:
    raw = _json_bytes(payload)
    row = {"run_id": run_id, "role": role, "payload_json": canonical_json(dict(payload))}
    connection.execute("INSERT INTO json_payloads VALUES(?,?,?,?)",
                       (*row.values(), _record(row)))
    return raw


def _recover_artifact(connection: sqlite3.Connection, root: Path, run_id: str,
                      role: str, expected_path: Path) -> bytes:
    row = connection.execute(
        "SELECT * FROM artifacts WHERE run_id=? AND role=?", (run_id, role)
    ).fetchone()
    if row is None or _resolve_text(root, str(row["path"])).resolve() != expected_path.resolve():
        raise IdentityInstrumentV21Error(f"artifact recovery mismatch: {role}")
    try:
        raw = bytes.fromhex(str(row["payload_hex"]))
    except ValueError as exc:
        raise IdentityInstrumentV21Error(f"stored artifact bytes invalid: {role}") from exc
    if len(raw) != int(row["size_bytes"]) or hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise IdentityInstrumentV21Error(f"stored artifact binding changed: {role}")
    _atomic_bytes(expected_path, raw, allow_identical=True)
    return raw


def parse_n100_rationale(value: Any) -> dict[str, str]:
    if not isinstance(value, str) or value != value.strip():
        raise IdentityInstrumentV21Error("N100 rationale must be trimmed text")
    match = RATIONALE_RE.fullmatch(value)
    if match is None:
        raise IdentityInstrumentV21Error("N100 rationale does not match the exact six-tag+basis grammar")
    result = match.groupdict()
    if not result["basis"].strip() or result["basis"] != result["basis"].strip():
        raise IdentityInstrumentV21Error("N100 basis must be trimmed non-empty text")
    return result


def validate_observation(value: Mapping[str, Any]) -> dict[str, str]:
    if set(value) != set(OBSERVATION_KEYS):
        raise IdentityInstrumentV21Error("observation keys must be exact")
    result: dict[str, str] = {}
    for axis in CONTROLLED_KEYS:
        raw = value[axis]
        if not isinstance(raw, str) or raw not in CONTROLLED_VALUES[axis]:
            raise IdentityInstrumentV21Error(f"invalid controlled observation: {axis}")
        result[axis] = raw
    basis = value["basis"]
    if not isinstance(basis, str) or not basis.strip() or basis != basis.strip():
        raise IdentityInstrumentV21Error("basis must be trimmed non-empty text")
    result["basis"] = basis
    return result


def consensus_axes(r1: Mapping[str, str], r2: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for axis in CONTROLLED_KEYS:
        left, right = str(r1[axis]), str(r2[axis])
        priority = CONSENSUS_PRIORITY[axis]
        if left not in priority or right not in priority:
            raise IdentityInstrumentV21Error(f"consensus received unknown {axis} value")
        result[axis] = left if left == right else min(
            (left, right), key=lambda item: priority.index(item)
        )
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
        if str(observation.get(axis, "")) not in CONTROLLED_VALUES[axis]:
            raise IdentityInstrumentV21Error(f"invalid controlled observation: {axis}")
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
            "different_objects": (
                "Comparable usable building-form evidence identifies different physical objects."
            ),
            "inconclusive": (
                "Usable imagery exists, but no comparable building form is present, for example "
                "drawings or diagrams only, a publication page, or unreadable imagery."
            ),
            "unavailable": "At least one side has no usable image at all.",
            "identifiable_same_object": (
                "If the same object is identifiable despite view angle, crop, or image-count "
                "differences, record same_object."
            ),
        },
        "scope_guidance": {
            "photo_identity_is_not_scope_identity": (
                "Matching photographs establish a shared depicted object, not necessarily equal scope."
            ),
            "related_separate": (
                "Use related_separate only when the records are related but describe distinct scope."
            ),
        },
    }


def _quota_time(value: Any, *, require_fresh: bool = True) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise IdentityInstrumentV21Error("quota observed_at must be timezone ISO8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IdentityInstrumentV21Error("quota observed_at must include a timezone")
    observed = parsed.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if require_fresh and (observed > now + v2.MAX_QUOTA_FUTURE_SKEW
                          or now - observed > v2.MAX_QUOTA_AGE):
        raise IdentityInstrumentV21Error("quota observation is stale or implausibly future")
    return observed


def _require_not_stopped(connection: sqlite3.Connection, run_id: str) -> None:
    row = connection.execute("SELECT sticky_stop FROM runs WHERE run_id=?", (run_id,)).fetchone()
    stopped = connection.execute(
        "SELECT 1 FROM quota_observations WHERE run_id=? AND stop_required=1 LIMIT 1", (run_id,)
    ).fetchone()
    if row is None or int(row[0]) or stopped is not None:
        raise IdentityInstrumentV21Error("sticky quota stop forbids further activity")


def extract_vocabulary(*, repo_root: Path, quota_remaining_percent: float,
                       quota_observed_at: str, quota_source: str,
                       quota_account_index: int = 0, quota_limitation: str = "",
                       n100_db_path: Path = DEFAULT_N100_DB,
                       order_path: Path = DEFAULT_ORDER,
                       output_path: Path = DEFAULT_OUTPUT,
                       enforce_source_contract: bool = True) -> StageResult:
    """Stage A: parse and immutably freeze all 210 N100 axis rationales."""
    root = repo_root.resolve(); output = _path(root, output_path)
    _quota_time(quota_observed_at)
    if not 0 <= quota_remaining_percent <= 100 or quota_remaining_percent <= 30:
        raise IdentityInstrumentV21Error("pre-freeze quota must be greater than 30%")
    if not quota_source.strip():
        raise IdentityInstrumentV21Error("quota source is required")
    n100_path = _path(root, n100_db_path)
    pins = {
        "n100_source_db": _file_pin(root, n100_db_path, "vocabulary_and_gold_source"),
        "wp28_order": _file_pin(root, order_path, "work_order"),
    }
    if enforce_source_contract and (
        int(pins["n100_source_db"]["size_bytes"]) != N100_DB_SIZE
        or pins["n100_source_db"]["sha256"] != N100_DB_SHA256
    ):
        raise IdentityInstrumentV21Error("N100 source DB pin changed")
    source = _open_ro(n100_path)
    try:
        source_run = source.execute("SELECT * FROM runs").fetchone()
        review_rows = source.execute(
            "SELECT * FROM blind_reviews ORDER BY lane,opaque_review_id"
        ).fetchall()
        if source_run is None or (enforce_source_contract and source_run["run_id"] != N100_RUN_ID):
            raise IdentityInstrumentV21Error("N100 source run mismatch")
        if len(review_rows) != N100_REVIEW_ROWS:
            raise IdentityInstrumentV21Error("N100 source must contain 210 blind reviews")
        parsed: list[tuple[sqlite3.Row, dict[str, str]]] = []
        frequencies = {lane: {axis: Counter() for axis in CONTROLLED_KEYS}
                       for lane in ("r1", "r2", "all")}
        for row in review_rows:
            observation = parse_n100_rationale(row["rationale"])
            lane = str(row["lane"])
            if lane not in {"r1", "r2"}:
                raise IdentityInstrumentV21Error("unexpected N100 review lane")
            for axis in CONTROLLED_KEYS:
                frequencies[lane][axis][observation[axis]] += 1
                frequencies["all"][axis][observation[axis]] += 1
            parsed.append((row, observation))
        observed = {axis: dict(sorted(frequencies["all"][axis].items()))
                    for axis in CONTROLLED_KEYS}
        if enforce_source_contract and observed != {
            axis: dict(sorted(values.items())) for axis, values in EXPECTED_VOCAB_COUNTS.items()
        }:
            raise IdentityInstrumentV21Error("N100 vocabulary/frequency table changed")
        if any(set(observed[axis]) != set(CONTROLLED_VALUES[axis]) for axis in CONTROLLED_KEYS):
            raise IdentityInstrumentV21Error("controlled values differ from extracted N100 vocabulary")
    finally:
        source.close()
    vocabulary_payload = {
        "version": VOCABULARY_VERSION, "source_run_id": N100_RUN_ID,
        "parser_regex": RATIONALE_RE.pattern, "row_count": len(parsed),
        "controlled_values": {axis: list(CONTROLLED_VALUES[axis]) for axis in CONTROLLED_KEYS},
        "frequencies": observed, "basis": {"nonblank": len(parsed), "required": True},
    }
    vocabulary_sha = canonical_sha256(vocabulary_payload)
    run_id = "wp28-" + hashlib.sha256(
        f"{SAMPLE_VERSION}\0{pins['n100_source_db']['sha256']}\0{vocabulary_sha}".encode("utf-8")
    ).hexdigest()[:24]
    if output.exists():
        connection = _connect(output)
        try:
            run = _one_run(connection)
            contract = connection.execute("SELECT * FROM vocabulary_contract").fetchone()
            if run["run_id"] != run_id or contract is None or contract["vocabulary_sha256"] != vocabulary_sha:
                raise IdentityInstrumentV21Error("vocabulary extraction recovery mismatch")
            _verify_pins(connection, root, run_id)
            return StageResult(output, run_id, str(run["status"]))
        finally:
            connection.close()
    temporary = output.with_name(output.name + ".building")
    failed = output.with_name(output.name + ".building.failed")
    if temporary.exists() or failed.exists():
        raise IdentityInstrumentV21Error("stale vocabulary build artifact exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary); connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.executescript(SCHEMA_SQL)
        created = utc_now()
        run_payload = {
            "run_id": run_id, "schema_version": SCHEMA_VERSION,
            "runner_version": RUNNER_VERSION, "instrument_version": INSTRUMENT_VERSION,
            "sample_version": SAMPLE_VERSION, "review_version": REVIEW_VERSION,
            "adjudication_version": ADJUDICATION_VERSION, "gate_version": GATE_VERSION,
            "source_sample_version": SOURCE_SAMPLE_VERSION, "source_seed": SOURCE_SEED,
            "source_logical_sha256": SOURCE_LOGICAL_SHA256, "source_run_id": SOURCE_RUN_ID,
            "source_packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
            "source_visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
            "status": "vocabulary_frozen", "created_at": created,
        }
        columns = tuple(run_payload)
        connection.execute(
            "INSERT INTO runs(" + ",".join(columns) + ",record_sha256) VALUES(" +
            ",".join("?" for _ in range(len(columns) + 1)) + ")",
            (*run_payload.values(), _record(run_payload)),
        )
        for role, pin in sorted(pins.items()):
            _insert_pin(connection, run_id, role, pin)
        for row, observation in parsed:
            payload = {"run_id": run_id, "lane": str(row["lane"]),
                       "opaque_review_id": str(row["opaque_review_id"]),
                       "n100_sample_id": str(row["sample_id"]), **observation,
                       "source_record_sha256": str(row["record_sha256"])}
            connection.execute(
                "INSERT INTO n100_review_axes VALUES(" + ",".join("?" for _ in range(13)) + ")",
                (*payload.values(), _record(payload)),
            )
        for axis in CONTROLLED_KEYS:
            for value in sorted(observed[axis]):
                payload = {"run_id": run_id, "axis": axis, "value": value,
                           "frequency": frequencies["all"][axis][value],
                           "r1_frequency": frequencies["r1"][axis][value],
                           "r2_frequency": frequencies["r2"][axis][value]}
                connection.execute("INSERT INTO vocabulary_counts VALUES(?,?,?,?,?,?,?)",
                                   (*payload.values(), _record(payload)))
        frozen_at = utc_now()
        vocabulary_json = canonical_json(vocabulary_payload)
        contract_payload = {"run_id": run_id, "version": VOCABULARY_VERSION,
                            "parser_regex": RATIONALE_RE.pattern, "row_count": len(parsed),
                            "vocabulary_json": vocabulary_json,
                            "vocabulary_sha256": vocabulary_sha,
                            "basis_nonblank_count": len(parsed), "frozen_at": frozen_at}
        connection.execute("INSERT INTO vocabulary_contract VALUES(?,?,?,?,?,?,?,?,?)",
                           (*contract_payload.values(), _record(contract_payload)))
        quota_payload = {"run_id": run_id, "observation_no": 1, "phase": "pre_freeze",
                         "account_index": quota_account_index,
                         "observed_at": quota_observed_at.strip(), "source": quota_source.strip(),
                         "remaining_percent": float(quota_remaining_percent),
                         "start_allowed": 1, "stop_required": 0,
                         "limitation": quota_limitation.strip()}
        connection.execute("INSERT INTO quota_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (*quota_payload.values(), _record(quota_payload)))
        _insert_event(connection, run_id, "none", "vocabulary_frozen",
                      {"stage": "A", "rows": len(parsed),
                       "vocabulary_sha256": vocabulary_sha, "controlled_value_rows": 22})
        _refresh_run_record(connection, run_id); connection.commit(); connection.close()
        connection = None; os.replace(temporary, output)
        return StageResult(output, run_id, "vocabulary_frozen")
    except Exception:
        if connection is not None:
            connection.close()
        if temporary.exists() and not failed.exists():
            os.replace(temporary, failed)
        raise


def verify_derivation(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                      enforce_expected_replay: bool = True) -> StageResult:
    """Stage B: freeze consensus axes and verify the human-readable rules."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    connection = _connect(artifact); source: sqlite3.Connection | None = None
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        existing = connection.execute("SELECT * FROM derivation_contract").fetchone()
        if existing is not None:
            return StageResult(artifact, run_id, status)
        if status != "vocabulary_frozen":
            raise IdentityInstrumentV21Error("derivation verification requires vocabulary_frozen")
        _require_not_stopped(connection, run_id); _verify_pins(connection, root, run_id)
        pin = connection.execute(
            "SELECT path FROM input_pins WHERE run_id=? AND role='n100_source_db'", (run_id,)
        ).fetchone()
        source = _open_ro(_resolve_text(root, str(pin[0])))
        gold_rows = source.execute(
            "SELECT sample_id,final_gold_label,record_sha256 FROM adjudications ORDER BY sample_id"
        ).fetchall()
        if len(gold_rows) != N100_GOLD_ROWS:
            raise IdentityInstrumentV21Error("N100 must contain 105 adjudicated gold rows")
        reviews: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
        for row in connection.execute(
            "SELECT * FROM n100_review_axes WHERE run_id=? ORDER BY n100_sample_id,lane", (run_id,)
        ):
            reviews[str(row["n100_sample_id"])][str(row["lane"])] = row
        priority_payload = {axis: list(CONSENSUS_PRIORITY[axis]) for axis in CONTROLLED_KEYS}
        priority_json = canonical_json(priority_payload); priority_sha = canonical_sha256(priority_payload)
        frozen_at = utc_now()
        consensus_payload = {"run_id": run_id, "version": CONSENSUS_VERSION,
                             "priority_json": priority_json, "priority_sha256": priority_sha,
                             "frozen_at": frozen_at}
        connection.execute("INSERT INTO consensus_contract VALUES(?,?,?,?,?,?)",
                           (*consensus_payload.values(), _record(consensus_payload)))
        rule_counts: Counter[int] = Counter(); mismatches: list[dict[str, Any]] = []
        matches = 0
        for gold in gold_rows:
            sample_id = str(gold["sample_id"]); pair = reviews.get(sample_id, {})
            if set(pair) != {"r1", "r2"}:
                raise IdentityInstrumentV21Error("N100 gold row lacks two parsed reviews")
            r1_axes = {axis: str(pair["r1"][axis]) for axis in CONTROLLED_KEYS}
            r2_axes = {axis: str(pair["r2"][axis]) for axis in CONTROLLED_KEYS}
            final = consensus_axes(r1_axes, r2_axes)
            predicted, rule = derive_label(final); rule_counts[rule] += 1
            actual = str(gold["final_gold_label"])
            if predicted == actual:
                matches += 1
            else:
                mismatches.append({"n100_sample_id": sample_id, "predicted_label": predicted,
                                   "gold_label": actual, "derivation_rule": rule,
                                   "consensus_axes": final})
            semantic = {"n100_sample_id": sample_id, **final}
            payload = {"run_id": run_id, **semantic,
                       "r1_observation_sha256": canonical_sha256({"lane": "r1", **r1_axes}),
                       "r2_observation_sha256": canonical_sha256({"lane": "r2", **r2_axes}),
                       "consensus_sha256": canonical_sha256(semantic)}
            connection.execute(
                "INSERT INTO n100_consensus_axes VALUES(" + ",".join("?" for _ in range(12)) + ")",
                (*payload.values(), _record(payload)),
            )
        rule_counts_payload = {str(rule): int(rule_counts[rule]) for rule in range(1, 6)}
        expected_mismatch_triples = tuple(
            (row["n100_sample_id"], row["predicted_label"], row["gold_label"])
            for row in mismatches
        )
        if enforce_expected_replay and (
            matches != EXPECTED_DERIVATION_MATCHES
            or rule_counts_payload != EXPECTED_RULE_COUNTS
            or expected_mismatch_triples != EXPECTED_MISMATCHES
        ):
            raise IdentityInstrumentV21Error("Stage B exact 102/105 replay changed")
        passed = matches >= DERIVATION_MIN_MATCHES
        verified_at = utc_now(); rules = derivation_rules()
        rules_json = canonical_json(rules); mismatches_json = canonical_json(mismatches)
        derivation_payload = {"run_id": run_id, "version": DERIVATION_VERSION,
                              "rules_json": rules_json, "rules_sha256": canonical_sha256(rules),
                              "numerator": matches, "denominator": len(gold_rows),
                              "minimum_matches": DERIVATION_MIN_MATCHES, "passed": int(passed),
                              "rule_counts_json": canonical_json(rule_counts_payload),
                              "mismatches_json": mismatches_json, "verified_at": verified_at}
        connection.execute("INSERT INTO derivation_contract VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (*derivation_payload.values(), _record(derivation_payload)))
        next_status = "derivation_verified" if passed else "derivation_failed"
        completed = None if passed else verified_at
        connection.execute("UPDATE runs SET status=?,completed_at=? WHERE run_id=?",
                           (next_status, completed, run_id))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "vocabulary_frozen", next_status,
                      {"stage": "B", "matches": matches, "denominator": len(gold_rows),
                       "minimum_matches": DERIVATION_MIN_MATCHES,
                       "rule_counts": rule_counts_payload,
                       "mismatch_count": len(mismatches),
                       "mismatches_sha256": canonical_sha256(mismatches)})
        connection.commit()
        return StageResult(artifact, run_id, next_status)
    except Exception:
        connection.rollback(); raise
    finally:
        if source is not None:
            source.close()
        connection.close()


def _instrument_payload(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    vocabulary = connection.execute(
        "SELECT vocabulary_sha256 FROM vocabulary_contract WHERE run_id=?", (run_id,)
    ).fetchone()
    derivation = connection.execute(
        "SELECT rules_sha256,numerator,denominator,minimum_matches FROM derivation_contract WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return {
        "instrument_version": INSTRUMENT_VERSION,
        "observation_keys": list(OBSERVATION_KEYS),
        "controlled_values": {axis: list(CONTROLLED_VALUES[axis]) for axis in CONTROLLED_KEYS},
        "basis": {"type": "trimmed_non_empty_text", "required": True},
        "instructions": _axis_instructions(),
        "forbidden_input_fields": sorted(FORBIDDEN_INPUT_FIELDS),
        "vocabulary_contract_sha256": str(vocabulary[0]),
        "derivation_contract": {"rules_sha256": str(derivation[0]),
                                "matches": int(derivation[1]),
                                "denominator": int(derivation[2]),
                                "minimum_matches": int(derivation[3])},
        "source": {"run_id": SOURCE_RUN_ID, "sample_version": SOURCE_SAMPLE_VERSION,
                   "seed": SOURCE_SEED, "logical_sha256": SOURCE_LOGICAL_SHA256,
                   "packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
                   "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
                   "rows": TOTAL_COUNT, "sheets": SOURCE_SHEET_COUNT,
                   "asset_occurrences": SOURCE_ASSET_OCCURRENCES},
        "limits": {"max_assets_per_side": 2, "max_asset_occurrences": 96,
                   "incremental_http": 0, "incremental_wire": 0,
                   "incremental_retries": 0, "incremental_redirects": 0,
                   "incremental_renderer_substitutions": 0, "incremental_cache_writes": 0},
    }


def freeze_instrument(*, repo_root: Path, protocol_path: Path = DEFAULT_PROTOCOL,
                      source_db_path: Path = DEFAULT_SOURCE_DB,
                      source_report_path: Path = DEFAULT_SOURCE_REPORT,
                      source_manifest_path: Path = DEFAULT_SOURCE_MANIFEST,
                      source_validation_path: Path = DEFAULT_SOURCE_VALIDATION,
                      artifact_path: Path = DEFAULT_OUTPUT,
                      enforce_source_contract: bool = True) -> StageResult:
    """Stage C: pin the v2.1 protocol and freeze the neutral actor contract."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    connection = _connect(artifact); source: sqlite3.Connection | None = None
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        existing = connection.execute("SELECT * FROM instrument_contract").fetchone()
        if existing is not None:
            _verify_pins(connection, root, run_id)
            return StageResult(artifact, run_id, status)
        if status != "derivation_verified":
            raise IdentityInstrumentV21Error("instrument freeze requires derivation_verified")
        _require_not_stopped(connection, run_id)
        pins = {
            "wp28_protocol": _file_pin(root, protocol_path, "instrument_contract"),
            "wp27_source_db": _file_pin(root, source_db_path, "24_row_packet_and_reveal_source"),
            "wp27_final_report": _file_pin(root, source_report_path, "v2_comparison_source"),
            "wp27_final_manifest": _file_pin(root, source_manifest_path, "source_provenance"),
            "wp27_final_validation": _file_pin(root, source_validation_path, "independent_validation"),
        }
        if enforce_source_contract and (
            pins["wp27_source_db"]["size_bytes"] != WP27_DB_SIZE
            or pins["wp27_source_db"]["sha256"] != WP27_DB_SHA256
        ):
            raise IdentityInstrumentV21Error("WP2.7 source DB pin changed")
        source = _open_ro(_path(root, source_db_path))
        source_run = source.execute("SELECT * FROM runs").fetchone()
        if source_run is None or (enforce_source_contract and str(source_run["run_id"]) != WP27_RUN_ID):
            raise IdentityInstrumentV21Error("WP2.7 source run mismatch")
        correction = source.execute(
            "SELECT path,size_bytes,sha256,usage FROM input_pins WHERE role='wp26_fetch_correction'"
        ).fetchone()
        if correction is None:
            raise IdentityInstrumentV21Error("WP2.7 correction provenance is missing")
        correction_pin = {key: correction[key] for key in ("path", "size_bytes", "sha256", "usage")}
        correction_path = _resolve_text(root, str(correction_pin["path"]))
        if not correction_path.is_file() or sha256_file(correction_path) != correction_pin["sha256"]:
            raise IdentityInstrumentV21Error("WP2.6 correction overlay pin changed")
        pins["wp26_fetch_correction"] = dict(correction_pin)
        for role, pin in sorted(pins.items()):
            _insert_pin(connection, run_id, role, pin)
        contract = _instrument_payload(connection, run_id); contract_sha = canonical_sha256(contract)
        frozen_at = utc_now(); protocol_pin = pins["wp28_protocol"]
        order_pin = connection.execute(
            "SELECT path,sha256 FROM input_pins WHERE run_id=? AND role='wp28_order'", (run_id,)
        ).fetchone()
        payload = {"run_id": run_id, "contract_json": canonical_json(contract),
                   "contract_sha256": contract_sha,
                   "protocol_path": protocol_pin["path"],
                   "protocol_sha256": protocol_pin["sha256"],
                   "order_path": str(order_pin["path"]), "order_sha256": str(order_pin["sha256"]),
                   "frozen_at": frozen_at}
        connection.execute("INSERT INTO instrument_contract VALUES(?,?,?,?,?,?,?,?,?)",
                           (*payload.values(), _record(payload)))
        connection.execute("UPDATE runs SET status='instrument_frozen' WHERE run_id=?", (run_id,))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "derivation_verified", "instrument_frozen",
                      {"stage": "C", "instrument_contract_sha256": contract_sha,
                       "protocol_sha256": protocol_pin["sha256"]})
        connection.commit(); return StageResult(artifact, run_id, "instrument_frozen")
    except Exception:
        connection.rollback(); raise
    finally:
        if source is not None:
            source.close()
        connection.close()


def _sample_manifest_payload(connection: sqlite3.Connection, root: Path,
                             run_id: str, artifact: Path) -> dict[str, Any]:
    samples = [{key: row[key] for key in (
        "sample_rank", "sample_id", "seed_rank_sha256",
        "control_commitment_sha256", "snapshot_sha256")}
        for row in connection.execute(
            "SELECT * FROM source_samples WHERE run_id=? ORDER BY sample_rank", (run_id,))]
    sheets = [{key: row[key] for key in (
        "lane", "display_order", "opaque_review_id", "sample_id",
        "sheet_path", "sheet_sha256", "sheet_bytes")}
        for row in connection.execute(
            "SELECT * FROM source_sheets WHERE run_id=? ORDER BY lane,display_order", (run_id,))]
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    instrument_sha = connection.execute(
        "SELECT contract_sha256 FROM instrument_contract WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    return {
        "status": "PASS", "run_id": run_id, "stage": "D_sample_freeze",
        "versions": {"runner": RUNNER_VERSION, "instrument": INSTRUMENT_VERSION,
                     "sample": SAMPLE_VERSION, "source_sample": SOURCE_SAMPLE_VERSION},
        "source": {"wp27_run_id": WP27_RUN_ID, "wp26_run_id": SOURCE_RUN_ID,
                   "seed": SOURCE_SEED, "logical_sha256": SOURCE_LOGICAL_SHA256,
                   "packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
                   "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256},
        "instrument_contract_sha256": str(instrument_sha),
        "counts": {"samples": int(run["sample_count"]), "sheets": int(run["sheet_count"]),
                   "asset_occurrences": int(run["asset_occurrence_count"]),
                   "unique_assets": int(run["unique_asset_count"]),
                   "post_freeze_replacements": 0},
        "incremental_network": {"logical": 0, "wire": 0, "redirects": 0, "retries": 0},
        "incremental_cache_writes": 0, "incremental_renderer_substitutions": 0,
        "samples": samples, "sheets": sheets,
        "sidecar": {"path": _path_text(root, artifact),
                    "logical_sha256": canonical_sha256(samples)},
        "forbidden_operations": {"vision": 0, "llm": 0, "paid_api": 0,
                                 "merge": 0, "id_issuance": 0, "tier_promotion": 0,
                                 "neon_write": 0, "r2_write": 0},
    }


def freeze_sample(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                  sample_manifest_path: Path = DEFAULT_SAMPLE_MANIFEST,
                  enforce_source_contract: bool = True) -> SampleResult:
    """Stage D: byte-for-byte replay the WP2.7 copy of the WP2.6 packet."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    manifest_path = _path(root, sample_manifest_path)
    connection = _connect(artifact); source: sqlite3.Connection | None = None
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        existing = connection.execute(
            "SELECT 1 FROM artifacts WHERE run_id=? AND role='sample_manifest'", (run_id,)
        ).fetchone()
        if existing is not None:
            _recover_artifact(connection, root, run_id, "sample_manifest", manifest_path)
            return SampleResult(artifact, manifest_path, run_id, TOTAL_COUNT,
                                SOURCE_SHEET_COUNT, SOURCE_ASSET_OCCURRENCES)
        if status != "instrument_frozen":
            raise IdentityInstrumentV21Error("sample freeze requires instrument_frozen")
        _require_not_stopped(connection, run_id); _verify_pins(connection, root, run_id)
        if manifest_path.exists():
            raise IdentityInstrumentV21Error("refusing to clobber sample manifest")
        pin = connection.execute(
            "SELECT path FROM input_pins WHERE run_id=? AND role='wp27_source_db'", (run_id,)
        ).fetchone()
        source = _open_ro(_resolve_text(root, str(pin[0])))
        source_run = source.execute("SELECT * FROM runs").fetchone()
        if source_run is None or (enforce_source_contract and (
            str(source_run["run_id"]) != WP27_RUN_ID
            or str(source_run["source_run_id"]) != SOURCE_RUN_ID
            or str(source_run["source_sample_version"]) != SOURCE_SAMPLE_VERSION
            or str(source_run["source_seed"]) != SOURCE_SEED
            or str(source_run["source_logical_sha256"]) != SOURCE_LOGICAL_SHA256
            or str(source_run["source_packet_binding_sha256"]) != SOURCE_PACKET_BINDING_SHA256
            or str(source_run["source_visual_inventory_sha256"]) != SOURCE_VISUAL_INVENTORY_SHA256
        )):
            raise IdentityInstrumentV21Error("WP2.7 frozen source contract mismatch")
        sample_rows = source.execute("SELECT * FROM source_samples ORDER BY sample_rank").fetchall()
        sheet_rows = source.execute("SELECT * FROM source_sheets ORDER BY lane,display_order").fetchall()
        asset_rows = source.execute(
            "SELECT * FROM source_assets ORDER BY sample_id,side,asset_order"
        ).fetchall()
        fetch_rows = source.execute("SELECT * FROM source_fetch_assets ORDER BY asset_key").fetchall()
        attempt_rows = source.execute(
            "SELECT * FROM source_fetch_attempts ORDER BY asset_key,attempt_no"
        ).fetchall()
        cache_rows = source.execute("SELECT * FROM source_cache_pins ORDER BY role").fetchall()
        statuses = Counter(str(row["status"]) for row in fetch_rows)
        if (len(sample_rows), len(sheet_rows), len(asset_rows), len(fetch_rows),
                len(attempt_rows), len(cache_rows)) != (24, 48, 93, 93, 17, 5):
            raise IdentityInstrumentV21Error("WP2.7 24/48/93/93/17/5 lineage changed")
        if enforce_source_contract and statuses != Counter({"cache_hit": 76, "failed": 17}):
            raise IdentityInstrumentV21Error("WP2.7 fetch status projection changed")
        packet_rows = [{key: row[key] for key in (
            "lane", "display_order", "opaque_review_id", "sample_id", "sheet_path",
            "sheet_sha256", "sheet_bytes")} for row in sheet_rows]
        packet_sha = canonical_sha256(packet_rows)
        visual_inventory = [{"lane": row["lane"], "display_order": row["display_order"],
                             "path": row["sheet_path"], "sha256": row["sheet_sha256"],
                             "bytes": row["sheet_bytes"]} for row in sheet_rows]
        inventory_sha = canonical_sha256(visual_inventory)
        if enforce_source_contract and (
            packet_sha != SOURCE_PACKET_BINDING_SHA256
            or inventory_sha != SOURCE_VISUAL_INVENTORY_SHA256
            or sum(int(row["sheet_bytes"]) for row in sheet_rows) != 24_517_369
        ):
            raise IdentityInstrumentV21Error("authoritative packet/visual inventory changed")
        per_side = Counter((str(row["sample_id"]), str(row["side"])) for row in asset_rows)
        if not per_side or max(per_side.values()) > MAX_ASSETS_PER_SIDE:
            raise IdentityInstrumentV21Error("source per-side asset cap exceeded")
        for row in sample_rows:
            payload = {"run_id": run_id, **{key: row[key] for key in (
                "sample_rank", "sample_id", "seed_rank_sha256", "control_commitment_sha256",
                "snapshot_sha256")}, "source_pair_record_sha256": str(row["record_sha256"])}
            connection.execute("INSERT INTO source_samples VALUES(?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        for row in sheet_rows:
            sheet = _resolve_text(root, str(row["sheet_path"]))
            if (not sheet.is_file() or sheet.stat().st_size != int(row["sheet_bytes"])
                    or sha256_file(sheet) != str(row["sheet_sha256"])):
                raise IdentityInstrumentV21Error("frozen source sheet missing or changed")
            payload = {"run_id": run_id, **{key: row[key] for key in (
                "lane", "display_order", "opaque_review_id", "sample_id", "sheet_path",
                "sheet_sha256", "sheet_bytes")},
                "source_record_sha256": str(row["record_sha256"])}
            connection.execute("INSERT INTO source_sheets VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        for row in asset_rows:
            payload = {"run_id": run_id, **{key: row[key] for key in (
                "sample_id", "side", "asset_order", "asset_key", "source",
                "source_building_id", "source_asset_id", "selection_reason")},
                "source_record_sha256": str(row["record_sha256"])}
            connection.execute("INSERT INTO source_assets VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        fetch_columns = (
            "asset_key", "source", "source_asset_id", "expected_url", "expected_raw_sha256",
            "expected_pixel_sha256", "status", "cache_role", "cache_path", "cache_file_sha256",
            "actual_final_url", "actual_raw_sha256", "actual_pixel_sha256", "http_status",
            "response_bytes", "request_count", "error_kind", "error_message",
        )
        for row in fetch_rows:
            payload = {"run_id": run_id, **{key: row[key] for key in fetch_columns},
                       "source_record_sha256": str(row["record_sha256"])}
            connection.execute(
                "INSERT INTO source_fetch_assets VALUES(" + ",".join("?" for _ in range(21)) + ")",
                (*payload.values(), _record(payload)),
            )
        for row in attempt_rows:
            payload = {"run_id": run_id, **{key: row[key] for key in (
                "asset_key", "attempt_no", "status", "request_count", "redirect_chain_json",
                "started_at", "completed_at", "error_kind")},
                "source_record_sha256": str(row["record_sha256"])}
            connection.execute("INSERT INTO source_fetch_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        for row in cache_rows:
            payload = {"run_id": run_id, **{key: row[key] for key in (
                "role", "path", "transform_version", "access_mode", "file_count_before",
                "total_bytes_before", "tree_sha256_before", "file_count_after",
                "total_bytes_after", "tree_sha256_after")},
                "source_record_sha256": str(row["record_sha256"])}
            connection.execute("INSERT INTO source_cache_pins VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        inherited_logical = len(attempt_rows)
        inherited_wire = sum(int(row["request_count"]) for row in attempt_rows)
        if inherited_logical > 96 or inherited_wire > 96:
            raise IdentityInstrumentV21Error("inherited network exceeds native 96 caps")
        connection.execute(
            "UPDATE runs SET status='sample_frozen',sample_count=?,sheet_count=?,"
            "asset_occurrence_count=?,unique_asset_count=?,inherited_logical_http_fetches=?,"
            "inherited_wire_http_requests=? WHERE run_id=?",
            (len(sample_rows), len(sheet_rows), len(asset_rows), len(fetch_rows),
             inherited_logical, inherited_wire, run_id),
        )
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "instrument_frozen", "sample_frozen",
                      {"stage": "D", "samples": 24, "sheets": 48,
                       "asset_occurrences": 93, "source_packet_binding_sha256": packet_sha,
                       "source_visual_inventory_sha256": inventory_sha,
                       "incremental_http": 0, "incremental_cache_writes": 0,
                       "incremental_renderer_substitutions": 0})
        payload = _sample_manifest_payload(connection, root, run_id, artifact)
        raw = _json_bytes(payload)
        _register_path(connection, root, run_id, "sample_manifest", manifest_path, "sample_freeze")
        _bind_values(connection, root, run_id, "sample_manifest", manifest_path,
                     "sample_freeze", raw)
        _store_json_payload(connection, run_id, "sample_manifest", payload)
        connection.commit(); _atomic_bytes(manifest_path, raw)
        return SampleResult(artifact, manifest_path, run_id, 24, 48, 93)
    except Exception:
        connection.rollback(); raise
    finally:
        if source is not None:
            source.close()
        connection.close()


def _gate_payload(eligible_sample_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "gate_version": GATE_VERSION,
        "predicates": [
            {"code": "d_image_same_object", "denominator": 15, "threshold": 14,
             "condition": "group=D AND v21_final_image=same_object"},
            {"code": "d_scope_same", "denominator": 15, "threshold": 14,
             "condition": "group=D AND v21_final_scope=same"},
            {"code": "n100_image_non_same_object", "denominator": 5, "threshold": 3,
             "condition": "frozen_N100_image IN (inconclusive,unavailable) AND "
                          "v21_final_image!=same_object"},
            {"code": "p1_non_same_label", "denominator": 3, "threshold": 2,
             "condition": "group=P1 AND v21_derived_label!=same"},
        ],
        "eligible_sample_ids": list(eligible_sample_ids), "combiner": "all",
    }


def declare_gate(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    """Freeze all four Stage-D predicates before any actor artifact exists."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    connection = _connect(artifact); source: sqlite3.Connection | None = None
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        existing = connection.execute("SELECT * FROM gate_contract").fetchone()
        if existing is not None:
            return {**json.loads(str(existing["gate_json"])),
                    "gate_sha256": str(existing["gate_sha256"]),
                    "declared_at": str(existing["declared_at"])}
        if status != "sample_frozen":
            raise IdentityInstrumentV21Error("gate declaration requires sample_frozen")
        _require_not_stopped(connection, run_id); _verify_pins(connection, root, run_id)
        pre_counts = {
            "form_artifacts": int(connection.execute(
                "SELECT count(*) FROM artifacts WHERE role IN "
                "('r1_form','r2_form','adjudication_packet','adjudication_form')"
            ).fetchone()[0]),
            "reviews": sum(int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                           for table in ("review_observations", "review_locks",
                                         "review_lock_payloads")),
            "adjudication": sum(int(connection.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0]) for table in (
                    "adjudication_items", "adjudicated_observations", "adjudication_lock",
                    "adjudication_lock_payload", "control_reveals", "gate_scores")),
        }
        if any(pre_counts.values()):
            raise IdentityInstrumentV21Error("gate declaration is too late")
        pin = connection.execute(
            "SELECT path FROM input_pins WHERE run_id=? AND role='wp27_source_db'", (run_id,)
        ).fetchone()
        source = _open_ro(_resolve_text(root, str(pin[0])))
        reveals = source.execute(
            "SELECT sample_id,group_code,n100_sample_id FROM control_reveals ORDER BY sample_id"
        ).fetchall()
        if len(reveals) != TOTAL_COUNT:
            raise IdentityInstrumentV21Error("WP2.7 reveal mapping must contain 24 rows")
        group_counts = Counter(str(row["group_code"]) for row in reveals)
        if group_counts != Counter({"D": 15, "P2": 4, "P1": 3, "P3": 2}):
            raise IdentityInstrumentV21Error("frozen group composition changed")
        d_n100_ids = {str(row["n100_sample_id"]) for row in reveals
                      if str(row["group_code"]) == "D"}
        d_observations = connection.execute(
            "SELECT n100_sample_id,image,scope FROM n100_review_axes WHERE run_id=?",
            (run_id,),
        ).fetchall()
        d_observations = [row for row in d_observations
                          if str(row["n100_sample_id"]) in d_n100_ids]
        if (len(d_n100_ids) != 15 or len(d_observations) != 30
                or any(str(row["image"]) != "same_object"
                       or str(row["scope"]) != "same" for row in d_observations)):
            raise IdentityInstrumentV21Error(
                "D controls must replay N100 30/30 image=same_object and scope=same"
            )
        consensus = {str(row["n100_sample_id"]): str(row["image"])
                     for row in connection.execute(
                         "SELECT n100_sample_id,image FROM n100_consensus_axes WHERE run_id=?",
                         (run_id,))}
        eligible = sorted(str(row["sample_id"]) for row in reveals
                          if consensus.get(str(row["n100_sample_id"]))
                          in {"inconclusive", "unavailable"})
        if len(eligible) != 5:
            raise IdentityInstrumentV21Error("N100 image gate must freeze exactly five seeds")
        gate = _gate_payload(eligible); gate_json = canonical_json(gate)
        gate_sha = canonical_sha256(gate); declared_at = utc_now()
        payload = {"run_id": run_id, "gate_json": gate_json, "gate_sha256": gate_sha,
                   "d_image_min": 14, "d_image_n": 15, "d_scope_min": 14,
                   "d_scope_n": 15, "n100_image_non_same_min": 3,
                   "n100_image_non_same_n": 5, "p1_min_non_same": 2, "p1_n": 3,
                   "eligible_sample_ids_json": canonical_json(eligible),
                   "declared_at": declared_at}
        connection.execute(
            "INSERT INTO gate_contract VALUES(" + ",".join("?" for _ in range(14)) + ")",
            (*payload.values(), _record(payload)),
        )
        connection.execute("UPDATE runs SET status='criteria_frozen',gate_status='declared' "
                           "WHERE run_id=?", (run_id,))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "sample_frozen", "criteria_frozen",
                      {"stage": "D", "gate_sha256": gate_sha, **pre_counts,
                       "eligible_count": len(eligible),
                       "eligible_commitment_sha256": canonical_sha256(eligible)})
        connection.commit()
        return {**gate, "gate_sha256": gate_sha, "declared_at": declared_at}
    except Exception:
        connection.rollback(); raise
    finally:
        if source is not None:
            source.close()
        connection.close()


def _source_fetch_payloads(connection: sqlite3.Connection,
                           run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    statuses = dict(sorted(Counter(str(row[0]) for row in connection.execute(
        "SELECT status FROM source_fetch_assets WHERE run_id=?", (run_id,))).items()))
    correction = connection.execute(
        "SELECT path,size_bytes,sha256 FROM input_pins WHERE run_id=? "
        "AND role='wp26_fetch_correction'", (run_id,)
    ).fetchone()
    correction_binding = {"path": str(correction["path"]),
                          "size_bytes": int(correction["size_bytes"]),
                          "sha256": str(correction["sha256"])}
    base = {
        "status": "PASS", "run_id": run_id,
        "evidence_reuse": "read_only_wp27_of_wp26_frozen_packet",
        "counts": {"samples": int(run["sample_count"]), "sheets": int(run["sheet_count"]),
                   "asset_occurrences": int(run["asset_occurrence_count"]),
                   "unique_assets": int(run["unique_asset_count"]),
                   "status_counts": statuses},
        "source_generation_network": {
            "logical_http_fetches": int(run["inherited_logical_http_fetches"]),
            "wire_http_requests": int(run["inherited_wire_http_requests"]),
            "redirects": 0, "retries": 0},
        "incremental_network": {"logical_http_fetches": 0, "wire_http_requests": 0,
                                "redirects": 0, "retries": 0},
        "cache_writes": 0, "renderer_substitutions": 0,
        "correction_overlay_provenance": correction_binding,
        "hard_caps": {"asset_occurrences": 96, "logical_http_fetches": 96,
                      "wire_http_requests": 96, "redirects": 0, "retries": 0},
    }
    return ({**base, "artifact": "fetch_manifest"},
            {**base, "artifact": "fetch_report", "measurement_only": True})


@contextmanager
def _v21_actor_context() -> Iterable[None]:
    """Temporarily bind the generic v2 actor ledger to v2.1 contracts."""
    replacements = {
        "IdentityInstrumentV2Error": IdentityInstrumentV21Error,
        "INSTRUMENT_VERSION": INSTRUMENT_VERSION,
        "REVIEW_VERSION": REVIEW_VERSION,
        "ADJUDICATION_VERSION": ADJUDICATION_VERSION,
        "CONTROLLED_VALUES": CONTROLLED_VALUES,
        "FORBIDDEN_INPUT_FIELDS": FORBIDDEN_INPUT_FIELDS,
        "_axis_instructions": _axis_instructions,
        "validate_observation": validate_observation,
        "derive_label": derive_label,
        "_path": _path, "_path_text": _path_text, "_resolve_text": _resolve_text,
        "_connect": _connect, "_one_run": _one_run,
        "_refresh_run_record": _refresh_run_record, "_insert_event": _insert_event,
        "_verify_pins": _verify_pins, "_require_not_stopped": _require_not_stopped,
        "_quota_time": _quota_time, "_register_path": _register_path,
        "_bind_values": _bind_values, "_store_json_payload": _store_json_payload,
        "_recover_artifact": _recover_artifact, "_atomic_bytes": _atomic_bytes,
        "sha256_file": sha256_file, "_source_fetch_payloads": _source_fetch_payloads,
    }
    previous = {name: getattr(v2, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(v2, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(v2, name, value)


def record_quota_observation(*, repo_root: Path, phase: str, remaining_percent: float,
                             source: str, observed_at: str, account_index: int = 0,
                             limitation: str = "", artifact_path: Path = DEFAULT_OUTPUT
                             ) -> dict[str, Any]:
    with _v21_actor_context():
        return v2.record_quota_observation(
            repo_root=repo_root, phase=phase, remaining_percent=remaining_percent,
            source=source, observed_at=observed_at, account_index=account_index,
            limitation=limitation, artifact_path=artifact_path,
        )


def prepare_review(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                   r1_form_path: Path = DEFAULT_R1_FORM,
                   r2_form_path: Path = DEFAULT_R2_FORM,
                   fetch_manifest_path: Path = DEFAULT_FETCH_MANIFEST,
                   fetch_report_path: Path = DEFAULT_FETCH_REPORT) -> PrepareResult:
    with _v21_actor_context():
        return v2.prepare_review(
            repo_root=repo_root, artifact_path=artifact_path,
            r1_form_path=r1_form_path, r2_form_path=r2_form_path,
            fetch_manifest_path=fetch_manifest_path, fetch_report_path=fetch_report_path,
        )


def lock_review(*, repo_root: Path, lane: str, completed_form_path: Path,
                locked_output_path: Path, artifact_path: Path = DEFAULT_OUTPUT) -> Path:
    with _v21_actor_context():
        return v2.lock_review(
            repo_root=repo_root, lane=lane, completed_form_path=completed_form_path,
            locked_output_path=locked_output_path, artifact_path=artifact_path,
        )


def prepare_adjudication(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                         packet_path: Path = DEFAULT_ADJUDICATION_PACKET,
                         adjudication_form_path: Path = DEFAULT_ADJUDICATION_FORM
                         ) -> tuple[Path, Path]:
    with _v21_actor_context():
        return v2.prepare_adjudication(
            repo_root=repo_root, artifact_path=artifact_path, packet_path=packet_path,
            adjudication_form_path=adjudication_form_path,
        )


def lock_adjudication(*, repo_root: Path, completed_form_path: Path,
                      locked_output_path: Path = DEFAULT_ADJUDICATION_LOCKED,
                      artifact_path: Path = DEFAULT_OUTPUT) -> Path:
    with _v21_actor_context():
        return v2.lock_adjudication(
            repo_root=repo_root, completed_form_path=completed_form_path,
            locked_output_path=locked_output_path, artifact_path=artifact_path,
        )


def _require_quota(connection: sqlite3.Connection, run_id: str, phase: str) -> None:
    _require_not_stopped(connection, run_id)
    rows = connection.execute(
        "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no", (run_id,)
    ).fetchall()
    if not rows or str(rows[-1]["phase"]) != phase:
        raise IdentityInstrumentV21Error(f"quota gate missing for {phase}")
    row = rows[-1]; _quota_time(row["observed_at"])
    if not int(row["start_allowed"]) or int(row["stop_required"]):
        raise IdentityInstrumentV21Error(f"quota gate denied for {phase}")


def _score_rows(rows: Sequence[Mapping[str, Any]],
                eligible: set[str]) -> dict[str, Any]:
    actual = {
        "d_image_same_object": sum(
            row["group_code"] == "D" and row["v21_image"] == "same_object" for row in rows
        ),
        "d_scope_same": sum(
            row["group_code"] == "D" and row["v21_scope"] == "same" for row in rows
        ),
        "n100_image_non_same_object": sum(
            row["sample_id"] in eligible and row["v21_image"] != "same_object" for row in rows
        ),
        "p1_non_same_label": sum(
            row["group_code"] == "P1" and row["v21_derived_label"] != "same" for row in rows
        ),
    }
    specification = {
        "d_image_same_object": (15, 14), "d_scope_same": (15, 14),
        "n100_image_non_same_object": (5, 3), "p1_non_same_label": (3, 2),
    }
    predicates = {code: {"actual": int(actual[code]), "denominator": denominator,
                         "threshold": threshold, "passed": int(actual[code]) >= threshold}
                  for code, (denominator, threshold) in specification.items()}
    return {"counts": actual, "predicates": predicates,
            "status": "pass" if all(item["passed"] for item in predicates.values()) else "fail"}


def _quota_report(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    keys = ("observation_no", "phase", "account_index", "observed_at", "source",
            "remaining_percent", "start_allowed", "stop_required", "limitation")
    return [{key: row[key] for key in keys} for row in connection.execute(
        "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no", (run_id,))]


def _stage_a_report(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    contract = connection.execute(
        "SELECT * FROM vocabulary_contract WHERE run_id=?", (run_id,)
    ).fetchone()
    rows = [{key: row[key] for key in (
        "axis", "value", "frequency", "r1_frequency", "r2_frequency")}
        for row in connection.execute(
            "SELECT * FROM vocabulary_counts WHERE run_id=? ORDER BY axis,value", (run_id,))]
    return {"version": str(contract["version"]), "source_rows": int(contract["row_count"]),
            "basis_nonblank_count": int(contract["basis_nonblank_count"]),
            "parser_regex": str(contract["parser_regex"]),
            "vocabulary_sha256": str(contract["vocabulary_sha256"]),
            "controlled_value_rows": rows}


def _stage_b_report(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    derivation = connection.execute(
        "SELECT * FROM derivation_contract WHERE run_id=?", (run_id,)
    ).fetchone()
    consensus = connection.execute(
        "SELECT * FROM consensus_contract WHERE run_id=?", (run_id,)
    ).fetchone()
    return {
        "consensus_version": str(consensus["version"]),
        "consensus_priority": json.loads(str(consensus["priority_json"])),
        "consensus_priority_sha256": str(consensus["priority_sha256"]),
        "derivation_version": str(derivation["version"]),
        "rules": json.loads(str(derivation["rules_json"])),
        "rules_sha256": str(derivation["rules_sha256"]),
        "numerator": int(derivation["numerator"]),
        "denominator": int(derivation["denominator"]),
        "reproduction_rate": int(derivation["numerator"]) / int(derivation["denominator"]),
        "minimum_matches": int(derivation["minimum_matches"]),
        "passed": bool(derivation["passed"]),
        "rule_counts": json.loads(str(derivation["rule_counts_json"])),
        "mismatches": json.loads(str(derivation["mismatches_json"])),
    }


def _four_way_rows(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    result = []
    for reveal in connection.execute(
        "SELECT * FROM control_reveals WHERE run_id=? ORDER BY sample_id", (run_id,)
    ):
        final = connection.execute(
            "SELECT * FROM adjudicated_observations WHERE run_id=? AND sample_id=?",
            (run_id, reveal["sample_id"]),
        ).fetchone()
        result.append({
            "sample_id": str(reveal["sample_id"]),
            "candidate_id": str(reveal["candidate_id"]),
            "group": str(reveal["group_code"]),
            "n100_sample_id": str(reveal["n100_sample_id"]),
            "n100": {"axes": json.loads(str(reveal["n100_axes_json"])),
                     "gold_label": str(reveal["n100_gold_label"])},
            "v1_probe_label": str(reveal["v1_probe_label"]),
            "v2_derived_label": str(reveal["v2_derived_label"]),
            "v21": {"axes": {key: str(final[key]) for key in OBSERVATION_KEYS},
                    "derived_label": str(reveal["v21_derived_label"]),
                    "derivation_rule_no": int(reveal["derivation_rule_no"])},
        })
    return result


def _axis_adjustment_audit(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    result = []
    for final in connection.execute(
        "SELECT * FROM adjudicated_observations WHERE run_id=? ORDER BY sample_id", (run_id,)
    ):
        reviews = {str(row["lane"]): row for row in connection.execute(
            "SELECT * FROM review_observations WHERE run_id=? AND sample_id=? ORDER BY lane",
            (run_id, final["sample_id"]))}
        axes = {key: str(final[key]) for key in OBSERVATION_KEYS}
        label, rule = derive_label(axes)
        result.append({"sample_id": str(final["sample_id"]),
                       "r1": {key: str(reviews["r1"][key]) for key in OBSERVATION_KEYS},
                       "r2": {key: str(reviews["r2"][key]) for key in OBSERVATION_KEYS},
                       "final": axes,
                       "adjustments": json.loads(str(final["axis_adjustments_json"])),
                       "derivation_replay": {"label": label, "rule_no": rule,
                                             "matches_persisted":
                                                 label == str(final["derived_label"])
                                                 and rule == int(final["derivation_rule"])}})
    return result


def _report_payload(connection: sqlite3.Connection, run_id: str, status: str,
                    completed_at: str) -> dict[str, Any]:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    scores = {str(row["predicate"]): {"actual": int(row["actual"]),
              "denominator": int(row["denominator"]), "threshold": int(row["threshold"]),
              "passed": bool(row["passed"])} for row in connection.execute(
                  "SELECT * FROM gate_scores WHERE run_id=? ORDER BY predicate", (run_id,))}
    status_counts = dict(sorted(Counter(str(row[0]) for row in connection.execute(
        "SELECT status FROM source_fetch_assets WHERE run_id=?", (run_id,))).items()))
    adjustments = _axis_adjustment_audit(connection, run_id)
    disagreements = {key: sum(
        int(bool(row["adjustments"][key]["reviewers_disagreed"])) for row in adjustments
    ) for key in OBSERVATION_KEYS}
    return {
        "status": status, "run_id": run_id, "completed_at": completed_at,
        "measurement_only": True, "authorizes_tiering": False,
        "authorizes_wp29": status == "pass", "instrument_version": INSTRUMENT_VERSION,
        "source_contract": {"wp27_run_id": WP27_RUN_ID, "wp26_run_id": SOURCE_RUN_ID,
                            "sample_version": SOURCE_SAMPLE_VERSION, "seed": SOURCE_SEED,
                            "logical_sha256": SOURCE_LOGICAL_SHA256,
                            "packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
                            "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256},
        "stage_a_vocabulary": _stage_a_report(connection, run_id),
        "stage_b_derivation": _stage_b_report(connection, run_id),
        "four_way_comparison": _four_way_rows(connection, run_id),
        "axis_adjustment_audit": adjustments,
        "axis_adjustment_summary": {"reviewer_disagreements": disagreements, "rows": 24},
        "gate": {"counts": {key: value["actual"] for key, value in scores.items()},
                 "predicates": scores, "status": status},
        "review": {"r1_rows": 24, "r2_rows": 24, "adjudicated_rows": 24,
                   "actors_distinct": True},
        "fetch": {"asset_occurrences": int(run["asset_occurrence_count"]),
                  "unique_assets": int(run["unique_asset_count"]),
                  "status_counts": status_counts,
                  "source_generation_network": {
                      "logical_http_fetches": int(run["inherited_logical_http_fetches"]),
                      "wire_http_requests": int(run["inherited_wire_http_requests"]),
                      "redirects": 0, "retries": 0},
                  "incremental_network": {"logical_http_fetches": 0,
                                          "wire_http_requests": 0,
                                          "redirects": 0, "retries": 0},
                  "cache_writes": 0, "renderer_substitutions": 0,
                  "hard_caps": {"asset_occurrences": 96, "logical_http_fetches": 96,
                                "wire_http_requests": 96, "redirects": 0, "retries": 0}},
        "quota_observations": _quota_report(connection, run_id),
        "vision_requests": 0, "llm_requests": 0, "paid_api_requests": 0,
        "merge_executions": 0, "vnext_id_issuances": 0, "tier_promotions": 0,
        "neon_writes": 0, "r2_writes": 0,
    }


def _manifest_payload(connection: sqlite3.Connection, root: Path, run_id: str, status: str,
                      completed_at: str, report_path: Path, report_sha: str) -> dict[str, Any]:
    pins = {str(row["role"]): {key: row[key] for key in
            ("path", "size_bytes", "sha256", "usage")} for row in connection.execute(
                "SELECT * FROM input_pins WHERE run_id=? ORDER BY role", (run_id,))}
    artifacts = {str(row["role"]): {key: row[key] for key in
                 ("path", "size_bytes", "sha256", "stage")} for row in connection.execute(
                     "SELECT * FROM artifact_bindings WHERE run_id=? "
                     "AND role<>'final_manifest' ORDER BY role", (run_id,))}
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return {
        "status": status, "run_id": run_id, "completed_at": completed_at,
        "versions": {"runner": RUNNER_VERSION, "instrument": INSTRUMENT_VERSION,
                     "vocabulary": VOCABULARY_VERSION, "consensus": CONSENSUS_VERSION,
                     "derivation": DERIVATION_VERSION, "sample": SAMPLE_VERSION,
                     "review": REVIEW_VERSION, "adjudication": ADJUDICATION_VERSION,
                     "gate": GATE_VERSION},
        "source_contract": {"wp27_run_id": WP27_RUN_ID, "wp26_run_id": SOURCE_RUN_ID,
                            "seed": SOURCE_SEED, "logical_sha256": SOURCE_LOGICAL_SHA256,
                            "packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
                            "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256},
        "input_pins": pins, "artifacts": artifacts,
        "report": {"path": _path_text(root, report_path), "sha256": report_sha},
        "counts": {"samples": 24, "sheets": 48, "asset_occurrences": 93,
                   "unique_assets": 93},
        "network": {"inherited_logical": int(run["inherited_logical_http_fetches"]),
                    "inherited_wire": int(run["inherited_wire_http_requests"]),
                    "incremental_logical": 0, "incremental_wire": 0,
                    "redirects": 0, "retries": 0},
        "cache_writes": 0, "renderer_substitutions": 0,
        "vision_requests": 0, "llm_requests": 0, "paid_api_requests": 0,
        "merge_executions": 0, "vnext_id_issuances": 0, "tier_promotions": 0,
        "neon_writes": 0, "r2_writes": 0,
    }


def reveal_score(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                 report_path: Path = DEFAULT_REPORT,
                 manifest_path: Path = DEFAULT_MANIFEST) -> RevealResult:
    """Reveal N100/v1/v2 controls only after the v2.1 adjudication ledger is locked."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    report = _path(root, report_path); manifest = _path(root, manifest_path)
    if report.resolve() == manifest.resolve():
        raise IdentityInstrumentV21Error("report and manifest paths must differ")
    connection = _connect(artifact); source: sqlite3.Connection | None = None
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        terminal = connection.execute("SELECT * FROM terminal WHERE run_id=?", (run_id,)).fetchone()
        if terminal is not None:
            if status not in {"pass", "fail"}:
                raise IdentityInstrumentV21Error("terminal/run status mismatch")
            if (_resolve_text(root, str(terminal["report_path"])).resolve() != report.resolve()
                    or _resolve_text(root, str(terminal["manifest_path"])).resolve()
                    != manifest.resolve()):
                raise IdentityInstrumentV21Error("terminal recovery path changed")
            report_raw = _json_bytes(json.loads(str(terminal["report_json"])))
            manifest_raw = _json_bytes(json.loads(str(terminal["manifest_json"])))
            if (hashlib.sha256(report_raw).hexdigest() != str(terminal["report_sha256"])
                    or hashlib.sha256(manifest_raw).hexdigest()
                    != str(terminal["manifest_sha256"])):
                raise IdentityInstrumentV21Error("terminal recovery payload changed")
            _atomic_bytes(report, report_raw, allow_identical=True)
            _atomic_bytes(manifest, manifest_raw, allow_identical=True)
            return RevealResult(artifact, report, manifest, run_id, status.upper())
        if status != "adjudication_locked":
            raise IdentityInstrumentV21Error("reveal requires adjudication_locked")
        _require_quota(connection, run_id, "pre_reveal_score")
        if report.exists() or manifest.exists():
            raise IdentityInstrumentV21Error("refusing to clobber terminal report/manifest")
        _verify_pins(connection, root, run_id)
        pin = connection.execute(
            "SELECT path FROM input_pins WHERE run_id=? AND role='wp27_source_db'", (run_id,)
        ).fetchone()
        source = _open_ro(_resolve_text(root, str(pin[0])))
        upstream = {str(row["sample_id"]): row for row in source.execute(
            "SELECT * FROM control_reveals ORDER BY sample_id")}
        samples = connection.execute(
            "SELECT * FROM source_samples WHERE run_id=? ORDER BY sample_id", (run_id,)
        ).fetchall()
        lock_event = connection.execute(
            "SELECT occurred_at FROM state_events WHERE run_id=? "
            "AND to_status='adjudication_locked'", (run_id,)
        ).fetchone()
        if len(upstream) != 24 or len(samples) != 24 or lock_event is None:
            raise IdentityInstrumentV21Error("locked reveal cardinality/event mismatch")
        contrast: list[dict[str, Any]] = []
        for sample in samples:
            sample_id = str(sample["sample_id"]); reveal = upstream.get(sample_id)
            final = connection.execute(
                "SELECT * FROM adjudicated_observations WHERE run_id=? AND sample_id=?",
                (run_id, sample_id),
            ).fetchone()
            if reveal is None or final is None:
                raise IdentityInstrumentV21Error("source reveal/final observation missing")
            if str(reveal["commitment_sha256"]) != str(sample["control_commitment_sha256"]):
                raise IdentityInstrumentV21Error("control commitment mismatch")
            observation = {key: str(final[key]) for key in OBSERVATION_KEYS}
            label, rule = derive_label(observation)
            if label != str(final["derived_label"]) or rule != int(final["derivation_rule"]):
                raise IdentityInstrumentV21Error("stored v2.1 derivation mismatch")
            n100_id = str(reveal["n100_sample_id"])
            n100_reviews = {str(row["lane"]): {key: str(row[key]) for key in OBSERVATION_KEYS}
                            for row in connection.execute(
                                "SELECT * FROM n100_review_axes WHERE run_id=? "
                                "AND n100_sample_id=? ORDER BY lane", (run_id, n100_id))}
            consensus_row = connection.execute(
                "SELECT * FROM n100_consensus_axes WHERE run_id=? AND n100_sample_id=?",
                (run_id, n100_id),
            ).fetchone()
            if set(n100_reviews) != {"r1", "r2"} or consensus_row is None:
                raise IdentityInstrumentV21Error("N100 axes missing at reveal")
            n100_axes = {"r1": n100_reviews["r1"], "r2": n100_reviews["r2"],
                         "consensus": {axis: str(consensus_row[axis])
                                       for axis in CONTROLLED_KEYS}}
            revealed_at = utc_now()
            if _quota_time(revealed_at, require_fresh=False) < _quota_time(
                    lock_event[0], require_fresh=False):
                raise IdentityInstrumentV21Error("control reveal predates adjudication lock")
            semantic = {"sample_id": sample_id, "candidate_id": str(reveal["candidate_id"]),
                        "group_code": str(reveal["group_code"]), "n100_sample_id": n100_id,
                        "n100_axes": n100_axes,
                        "n100_gold_label": str(reveal["n100_gold_label"]),
                        "v1_probe_label": str(reveal["v1_probe_label"]),
                        "v2_derived_label": str(reveal["v2_derived_label"]),
                        "v21_derived_label": label, "derivation_rule_no": rule,
                        "control_commitment_sha256": str(reveal["commitment_sha256"]),
                        "revealed_at": revealed_at}
            payload = {"run_id": run_id, "sample_id": sample_id,
                       "candidate_id": semantic["candidate_id"],
                       "group_code": semantic["group_code"],
                       "n100_sample_id": n100_id,
                       "n100_axes_json": canonical_json(n100_axes),
                       "n100_gold_label": semantic["n100_gold_label"],
                       "v1_probe_label": semantic["v1_probe_label"],
                       "v2_derived_label": semantic["v2_derived_label"],
                       "v21_derived_label": label, "derivation_rule_no": rule,
                       "commitment_sha256": semantic["control_commitment_sha256"],
                       "revealed_at": revealed_at,
                       "reveal_payload_sha256": canonical_sha256(semantic)}
            connection.execute(
                "INSERT INTO control_reveals VALUES(" + ",".join("?" for _ in range(15)) + ")",
                (*payload.values(), _record(payload)),
            )
            contrast.append({**payload, "v21_image": observation["image"],
                             "v21_scope": observation["scope"]})
        group_counts = Counter(row["group_code"] for row in contrast)
        if group_counts != Counter({"D": 15, "P2": 4, "P1": 3, "P3": 2}):
            raise IdentityInstrumentV21Error("revealed group composition changed")
        gate_row = connection.execute("SELECT * FROM gate_contract WHERE run_id=?", (run_id,)).fetchone()
        eligible = set(json.loads(str(gate_row["eligible_sample_ids_json"])))
        gate = _score_rows(contrast, eligible); terminal_status = str(gate["status"])
        for code, item in gate["predicates"].items():
            payload = {"run_id": run_id, "predicate": code, "actual": item["actual"],
                       "denominator": item["denominator"], "threshold": item["threshold"],
                       "passed": int(item["passed"])}
            connection.execute("INSERT INTO gate_scores VALUES(?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        completed_at = utc_now()
        report_payload = _report_payload(connection, run_id, terminal_status, completed_at)
        report_raw = _json_bytes(report_payload); report_sha = hashlib.sha256(report_raw).hexdigest()
        _register_path(connection, root, run_id, "final_report", report, "reveal_score")
        _bind_values(connection, root, run_id, "final_report", report, "reveal_score", report_raw)
        manifest_payload = _manifest_payload(
            connection, root, run_id, terminal_status, completed_at, report, report_sha
        )
        manifest_raw = _json_bytes(manifest_payload)
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        _register_path(connection, root, run_id, "final_manifest", manifest, "reveal_score")
        _bind_values(connection, root, run_id, "final_manifest", manifest,
                     "reveal_score", manifest_raw)
        terminal_payload = {"run_id": run_id, "status": terminal_status,
                            "report_path": _path_text(root, report),
                            "report_sha256": report_sha,
                            "manifest_path": _path_text(root, manifest),
                            "manifest_sha256": manifest_sha,
                            "report_json": canonical_json(report_payload),
                            "manifest_json": canonical_json(manifest_payload),
                            "completed_at": completed_at}
        connection.execute("INSERT INTO terminal VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (*terminal_payload.values(), _record(terminal_payload)))
        connection.execute("UPDATE runs SET status=?,completed_at=? WHERE run_id=?",
                           (terminal_status, completed_at, run_id))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "adjudication_locked", terminal_status,
                      {"n100_gold_revealed": True, "v1_label_revealed": True,
                       "v2_label_revealed": True, "gate": gate})
        connection.commit(); _atomic_bytes(report, report_raw); _atomic_bytes(manifest, manifest_raw)
        return RevealResult(artifact, report, manifest, run_id, terminal_status.upper())
    except Exception:
        connection.rollback(); raise
    finally:
        if source is not None:
            source.close()
        connection.close()


__all__ = [
    "ADJUDICATION_VERSION", "CONSENSUS_PRIORITY", "CONTROLLED_VALUES",
    "DEFAULT_ADJUDICATION_FORM", "DEFAULT_ADJUDICATION_LOCKED",
    "DEFAULT_ADJUDICATION_PACKET", "DEFAULT_FETCH_MANIFEST", "DEFAULT_FETCH_REPORT",
    "DEFAULT_MANIFEST", "DEFAULT_N100_DB", "DEFAULT_ORDER", "DEFAULT_OUTPUT",
    "DEFAULT_PROTOCOL", "DEFAULT_R1_FORM", "DEFAULT_R1_LOCKED", "DEFAULT_R2_FORM",
    "DEFAULT_R2_LOCKED", "DEFAULT_REPORT", "DEFAULT_SAMPLE_MANIFEST",
    "DEFAULT_SOURCE_DB", "DEFAULT_SOURCE_MANIFEST", "DEFAULT_SOURCE_REPORT",
    "DEFAULT_SOURCE_VALIDATION", "DERIVATION_VERSION", "GATE_VERSION",
    "IdentityInstrumentV21Error", "INSTRUMENT_VERSION", "OBSERVATION_KEYS",
    "QUOTA_PHASES", "REVIEW_VERSION", "RUNNER_VERSION", "SAMPLE_VERSION",
    "consensus_axes", "declare_gate", "derive_label", "derivation_rules",
    "extract_vocabulary", "freeze_instrument", "freeze_sample", "lock_adjudication",
    "lock_review", "parse_n100_rationale", "prepare_adjudication", "prepare_review",
    "record_quota_observation", "reveal_score", "utc_now", "validate_observation",
    "verify_derivation",
]
