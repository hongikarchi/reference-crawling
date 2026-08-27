"""WP2.6 review-instrument calibration probe.

The probe reuses the WP2.5 evidence renderer, asset selection, bounded HTTP
wave, blind form, and reviewer-lock implementation.  Frozen N100 labels are
committed during sampling but are not materialized in this sidecar or any
blind deliverable.  They are opened only by :func:`reveal_score` after a
distinct adjudicator's 24-row ledger is immutable and hash-bound.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import types
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping, Sequence

from canonical import vnext_identity_band_precision as wp25
from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_fetch import FetchPayload, fetch_once


SCHEMA_VERSION = 1
APPLICATION_ID = 0x57503236  # WP26
RUNNER_VERSION = "vnext-identity-instrument-probe-v1.0.0"
SAMPLE_VERSION = "vnext-identity-instrument-probe-v1"
REVIEW_VERSION = "vnext-identity-instrument-probe-wp25-instrument-v1"
ADJUDICATION_VERSION = "vnext-identity-instrument-probe-blind-adjudication-v1"
GATE_VERSION = "vnext-identity-instrument-probe-predeclared-gate-v1"
FETCH_CAP_CORRECTION_VERSION = (
    "vnext-identity-instrument-probe-fetch-cap-correction-v1"
)
SEED = "vnext-wp26-instrument-probe-v1-20260827"

GROUP_QUOTAS: Mapping[str, int] = {"P1": 3, "P2": 4, "P3": 2, "D": 15}
TOTAL_COUNT = 24
D_UNIVERSE_COUNT = 66
MAX_ASSETS_PER_SIDE = 2
MAX_ASSET_OCCURRENCES = 96
MAX_LOGICAL_HTTP_FETCHES = 96
MAX_WIRE_HTTP_REQUESTS = 96
MAX_WORKERS = 4
OUTPUT_CACHE_ROLE = "wp25_output"
CORRECTED_FETCH_HARD_CAPS: Mapping[str, int] = {
    "asset_occurrences": MAX_ASSET_OCCURRENCES,
    "logical_http_fetches": MAX_LOGICAL_HTTP_FETCHES,
    "wire_http_requests": MAX_WIRE_HTTP_REQUESTS,
    "redirects": 0,
    "retries": 0,
}
LEGACY_FETCH_HARD_CAPS: Mapping[str, int] = {
    "asset_occurrences": 884,
    "logical_http_fetches": 884,
    "wire_http_requests": 884,
    "redirects": 0,
    "retries": 0,
}
WP25_PRODUCER_SPEC = (
    153_786,
    "fe0072b9440eb080f039990c65c6a882d635df1bf0021f5d6a98bf977a558bfa",
)
ALLOWED_LABELS = wp25.ALLOWED_LABELS

QUOTA_PHASES: tuple[str, ...] = (
    "pre_freeze", "pre_prepare", "pre_r1_review", "pre_r2_review",
    "pre_adjudication_review", "pre_reveal_score",
)
QUOTA_PHASE_INDEX = {phase: index for index, phase in enumerate(QUOTA_PHASES)}
QUOTA_PHASE_RUN_STATUSES: Mapping[str, frozenset[str]] = {
    "pre_prepare": frozenset({"sample_frozen", "fetching", "review_ready"}),
    "pre_r1_review": frozenset({"review_ready"}),
    "pre_r2_review": frozenset({"reviews_partial"}),
    "pre_adjudication_review": frozenset({"reviews_locked"}),
    "pre_reveal_score": frozenset({"adjudication_locked"}),
}

GATE_THRESHOLDS: Mapping[str, int] = {
    "p1_min_non_same": 2,
    "p23_min_non_same": 3,
    "d_min_same": 13,
}

DEFAULT_V4 = Path("data/enrichment/vnext_identity_candidates_v4.db")
DEFAULT_N100 = Path("data/enrichment/vnext_identity_n100_v1.db")
DEFAULT_N100_MANIFEST = Path("data/reports/vnext_identity_n100_v1_manifest.json")
DEFAULT_N100_LOCKED = Path("data/reports/vnext_identity_n100_v1_adjudication_locked.json")
DEFAULT_WP25 = Path("data/enrichment/vnext_identity_band_precision_v1.db")
DEFAULT_WP25_MANIFEST = Path("data/reports/vnext_identity_band_precision_v1_manifest.json")
DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_INSTRUMENT_PROBE_PROTOCOL.md")
DEFAULT_ORDER = Path(".claude/ops/jobs/20260827_vnext_wp26_instrument_calibration_order.md")
DEFAULT_WP25_PRODUCER = Path("canonical/vnext_identity_band_precision.py")
DEFAULT_WP25_PROTOCOL = Path("docs/vnext/IDENTITY_BAND_PRECISION_PROTOCOL.md")

DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_instrument_probe_v1.db")
DEFAULT_OUTPUT_CACHE = Path("data/enrichment/vnext_identity_instrument_probe_v1_cache")
DEFAULT_SAMPLE_MANIFEST = Path("data/reports/vnext_identity_instrument_probe_v1_sample_manifest.json")
DEFAULT_FETCH_MANIFEST = Path("data/reports/vnext_identity_instrument_probe_v1_fetch_manifest.json")
DEFAULT_FETCH_REPORT = Path("data/reports/vnext_identity_instrument_probe_v1_fetch_report.json")
DEFAULT_SHEETS_ROOT = Path("data/reports/vnext_identity_instrument_probe_v1_blind")
DEFAULT_R1_FORM = Path("data/reports/vnext_identity_instrument_probe_v1_reviewer_r1_draft.json")
DEFAULT_R2_FORM = Path("data/reports/vnext_identity_instrument_probe_v1_reviewer_r2_draft.json")
DEFAULT_R1_LOCKED = Path("data/reports/vnext_identity_instrument_probe_v1_reviewer_r1_locked.json")
DEFAULT_R2_LOCKED = Path("data/reports/vnext_identity_instrument_probe_v1_reviewer_r2_locked.json")
DEFAULT_ADJUDICATION_PACKET = Path("data/reports/vnext_identity_instrument_probe_v1_adjudication_packet.json")
DEFAULT_ADJUDICATION_FORM = Path("data/reports/vnext_identity_instrument_probe_v1_adjudication_draft.json")
DEFAULT_ADJUDICATION_LOCKED = Path("data/reports/vnext_identity_instrument_probe_v1_adjudication_locked.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_instrument_probe_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_instrument_probe_v1_manifest.json")
DEFAULT_FETCH_CAP_CORRECTION = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_cap_correction.json"
)

V4_SPEC = wp25.V4_SPEC
N100_SPEC = (2_183_168, "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b")
N100_MANIFEST_SPEC = (3_579, "e4accff5634ce97712f5cb226e9a5557a75aef7699f6d532a6fcf514f2e3e9da")
N100_LOCKED_SPEC = (63_713, "c4fec6261cefb6a00c25baea9e36fccb9579e4832fbe52d80fd72fa36e9d3337")
WP25_SPEC = (7_491_584, "8ada59fc36c026c15b54afb5815fae240e98eea3083a52ff1e933770ce4fa85a")
WP25_MANIFEST_SPEC = (11_768, "a7b29eeb48f1dfcbae0c6831080011d9e4d706e02628e91a14ab9186049ff129")
RUNTIME_PRODUCER_SPEC = (
    109_175,
    "8e12aa23980ad17c1f7c724f8a99eb47f4e2844eee660a2d374a3436c032285e",
)
FETCH_CAP_CORRECTION_CAUSE = (
    "WP2.6 reused the pinned WP2.5 prepare writer, whose fetch manifest "
    "retained the WP2.5 literal 884 while WP2.6 enforcement, protocol, "
    "and terminal reporting used 96."
)


class IdentityInstrumentProbeError(wp25.IdentityBandPrecisionError):
    """A fail-closed WP2.6 contract violation."""


ERROR_BASE = wp25.IdentityBandPrecisionError


@dataclass(frozen=True)
class BuildResult:
    artifact_path: Path
    manifest_path: Path
    run_id: str
    sample_count: int
    sample_logical_sha256: str


@dataclass(frozen=True)
class RevealResult:
    artifact_path: Path
    report_path: Path
    manifest_path: Path
    run_id: str
    status: str
    p1_non_same: int
    p23_non_same: int
    d_same: int


@dataclass(frozen=True)
class FetchCapCorrectionResult:
    output_path: Path
    run_id: str
    corrected_projection_sha256: str
    output_sha256: str


_BASE_SCHEMA = (wp25.SCHEMA_SQL
    .replace(
        "'reviews_locked','unblinded','pass','fail','failed'",
        "'reviews_locked','unblinded','adjudication_locked','pass','fail','failed'",
    )
    .replace(
        "CHECK(stratum IN ('S1','S2','S3','S4','S5'))",
        "CHECK(stratum='probe')",
    ))

PROBE_SCHEMA_SQL = _BASE_SCHEMA + r"""
CREATE TABLE form_comparison(
 run_id TEXT NOT NULL REFERENCES runs(run_id), comparison_key TEXT NOT NULL,
 n100_value_json TEXT NOT NULL, wp25_value_json TEXT NOT NULL,
 finding TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,comparison_key)
);
CREATE TABLE control_commitments(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
 gold_commitment_sha256 TEXT NOT NULL,
 source_record_commitment_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id)
);
CREATE TABLE gate_contract(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
 p1_min_non_same INTEGER NOT NULL, p23_min_non_same INTEGER NOT NULL,
 d_min_same INTEGER NOT NULL, p1_n INTEGER NOT NULL, p23_n INTEGER NOT NULL,
 d_n INTEGER NOT NULL, contract_sha256 TEXT NOT NULL,
 declared_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE probe_adjudication_items(
 run_id TEXT NOT NULL REFERENCES runs(run_id), display_order INTEGER NOT NULL,
 opaque_probe_id TEXT NOT NULL, sample_id TEXT NOT NULL, sheet_path TEXT NOT NULL,
 sheet_sha256 TEXT NOT NULL, r1_label TEXT NOT NULL, r2_label TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,opaque_probe_id),
 UNIQUE(run_id,display_order), UNIQUE(run_id,sample_id)
);
CREATE TABLE probe_adjudications(
 run_id TEXT NOT NULL REFERENCES runs(run_id), opaque_probe_id TEXT NOT NULL,
 sample_id TEXT NOT NULL, r1_label TEXT NOT NULL, r2_label TEXT NOT NULL,
 adjudicator_label TEXT NOT NULL, final_probe_label TEXT NOT NULL,
 unresolved_disagreement INTEGER NOT NULL CHECK(unresolved_disagreement IN (0,1)),
 rationale TEXT NOT NULL, adjudicator TEXT NOT NULL, adjudicated_at TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,opaque_probe_id),
 UNIQUE(run_id,sample_id)
);
CREATE TABLE probe_adjudication_lock(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), adjudicator TEXT NOT NULL,
 row_count INTEGER NOT NULL, completed_form_path TEXT NOT NULL,
 completed_form_sha256 TEXT NOT NULL, locked_output_path TEXT NOT NULL,
 locked_output_sha256 TEXT NOT NULL, ordered_ledger_sha256 TEXT NOT NULL,
 locked_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE probe_adjudication_lock_payload(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), payload_json TEXT NOT NULL,
 record_sha256 TEXT NOT NULL
);
CREATE TABLE probe_scores(
 run_id TEXT NOT NULL REFERENCES runs(run_id), group_code TEXT NOT NULL,
 sample_n INTEGER NOT NULL, probe_same INTEGER NOT NULL,
 probe_non_same INTEGER NOT NULL, threshold INTEGER NOT NULL,
 passed INTEGER NOT NULL CHECK(passed IN (0,1)), record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,group_code)
);
CREATE TABLE probe_reveals(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
 candidate_id TEXT NOT NULL, group_code TEXT NOT NULL,
 n100_sample_id TEXT NOT NULL, n100_gold_label TEXT NOT NULL,
 final_probe_label TEXT NOT NULL, commitment_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id)
);
CREATE TABLE probe_terminal(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), status TEXT NOT NULL,
 report_path TEXT NOT NULL, report_sha256 TEXT NOT NULL,
 manifest_path TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 completed_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TRIGGER form_comparison_no_update BEFORE UPDATE ON form_comparison BEGIN SELECT RAISE(ABORT,'form comparison is frozen'); END;
CREATE TRIGGER form_comparison_no_delete BEFORE DELETE ON form_comparison BEGIN SELECT RAISE(ABORT,'form comparison is frozen'); END;
CREATE TRIGGER commitment_no_update BEFORE UPDATE ON control_commitments BEGIN SELECT RAISE(ABORT,'gold commitment is frozen'); END;
CREATE TRIGGER commitment_no_delete BEFORE DELETE ON control_commitments BEGIN SELECT RAISE(ABORT,'gold commitment is frozen'); END;
CREATE TRIGGER gate_no_update BEFORE UPDATE ON gate_contract BEGIN SELECT RAISE(ABORT,'gate contract is frozen'); END;
CREATE TRIGGER gate_no_delete BEFORE DELETE ON gate_contract BEGIN SELECT RAISE(ABORT,'gate contract is frozen'); END;
CREATE TRIGGER gate_only_after_sample BEFORE INSERT ON gate_contract WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'sample_frozen' BEGIN SELECT RAISE(ABORT,'gate declaration state invalid'); END;
CREATE TRIGGER probe_adj_items_no_update BEFORE UPDATE ON probe_adjudication_items BEGIN SELECT RAISE(ABORT,'adjudication items are frozen'); END;
CREATE TRIGGER probe_adj_items_no_delete BEFORE DELETE ON probe_adjudication_items BEGIN SELECT RAISE(ABORT,'adjudication items are frozen'); END;
CREATE TRIGGER probe_adj_no_update BEFORE UPDATE ON probe_adjudications BEGIN SELECT RAISE(ABORT,'probe adjudication is frozen'); END;
CREATE TRIGGER probe_adj_no_delete BEFORE DELETE ON probe_adjudications BEGIN SELECT RAISE(ABORT,'probe adjudication is frozen'); END;
CREATE TRIGGER probe_adj_lock_no_update BEFORE UPDATE ON probe_adjudication_lock BEGIN SELECT RAISE(ABORT,'adjudication lock is frozen'); END;
CREATE TRIGGER probe_adj_lock_no_delete BEFORE DELETE ON probe_adjudication_lock BEGIN SELECT RAISE(ABORT,'adjudication lock is frozen'); END;
CREATE TRIGGER probe_adj_payload_no_update BEFORE UPDATE ON probe_adjudication_lock_payload BEGIN SELECT RAISE(ABORT,'adjudication lock payload is frozen'); END;
CREATE TRIGGER probe_adj_payload_no_delete BEFORE DELETE ON probe_adjudication_lock_payload BEGIN SELECT RAISE(ABORT,'adjudication lock payload is frozen'); END;
CREATE TRIGGER probe_score_no_update BEFORE UPDATE ON probe_scores BEGIN SELECT RAISE(ABORT,'probe score is frozen'); END;
CREATE TRIGGER probe_score_no_delete BEFORE DELETE ON probe_scores BEGIN SELECT RAISE(ABORT,'probe score is frozen'); END;
CREATE TRIGGER probe_reveal_no_update BEFORE UPDATE ON probe_reveals BEGIN SELECT RAISE(ABORT,'probe reveal is frozen'); END;
CREATE TRIGGER probe_reveal_no_delete BEFORE DELETE ON probe_reveals BEGIN SELECT RAISE(ABORT,'probe reveal is frozen'); END;
CREATE TRIGGER probe_reveal_only_locked BEFORE INSERT ON probe_reveals WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'adjudication_locked' BEGIN SELECT RAISE(ABORT,'probe reveal state invalid'); END;
CREATE TRIGGER probe_terminal_no_update BEFORE UPDATE ON probe_terminal BEGIN SELECT RAISE(ABORT,'probe terminal is frozen'); END;
CREATE TRIGGER probe_terminal_no_delete BEFORE DELETE ON probe_terminal BEGIN SELECT RAISE(ABORT,'probe terminal is frozen'); END;
"""

def _path(root: Path, value: Path) -> Path:
    return wp25._path(root, value)


def _path_text(root: Path, value: Path) -> str:
    return wp25._path_text(root, value)


def _resolve_text(root: Path, value: str) -> Path:
    return wp25._resolve_text(root, value)


def _record(payload: Mapping[str, Any]) -> str:
    return wp25._record(payload)


_PRE_REVEAL_HIDDEN_KEYS = frozenset({
    "candidate_id", "real_candidate_id", "group", "group_code", "stratum",
    "n100_sample_id", "n100_gold_label", "final_gold_label", "gold_label",
    "proposal", "v3_proposal", "v4_proposal", "score", "numeric_score",
    "policy_flags", "candidate_flags", "seed_rank", "group_seed_rank",
    "auto_merge_eligible",
})


def _assert_blind_payload(payload: Any, label: str) -> None:
    """Reject producer-owned hidden fields while preserving reviewer prose."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            normalized = str(key).casefold()
            if normalized in _PRE_REVEAL_HIDDEN_KEYS:
                raise IdentityInstrumentProbeError(
                    f"{label} leaks hidden field {key}")
            _assert_blind_payload(value, label)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _assert_blind_payload(value, label)


def _clone_function(function: Callable[..., Any],
                    isolated_globals: dict[str, Any]) -> Callable[..., Any]:
    """Clone pinned WP2.5 bytecode without mutating its module globals."""
    cloned = types.FunctionType(
        function.__code__, isolated_globals, function.__name__,
        function.__defaults__, function.__closure__)
    cloned.__kwdefaults__ = function.__kwdefaults__
    cloned.__annotations__ = dict(getattr(function, "__annotations__", {}))
    cloned.__doc__ = function.__doc__
    cloned.__module__ = function.__module__
    cloned.__qualname__ = function.__qualname__
    return cloned


def _source_tuple(candidate: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return tuple(str(candidate[key]) for key in (
        "left_source", "left_source_building_id",
        "right_source", "right_source_building_id",
    ))  # type: ignore[return-value]


class _ProbeJsonAdapter:
    """Add the opaque control ID only to the in-memory renderer payload."""

    def __init__(self, opaque_by_source: Mapping[tuple[str, str, str, str], str]):
        self._opaque_by_source = dict(opaque_by_source)

    def loads(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        payload = json.loads(value, *args, **kwargs)
        if isinstance(payload, dict) and "candidate_id" not in payload:
            keys = {"left_source", "left_source_building_id",
                    "right_source", "right_source_building_id"}
            if keys.issubset(payload):
                source_tuple = _source_tuple(payload)
                opaque = self._opaque_by_source.get(source_tuple)
                if opaque is None:
                    raise IdentityInstrumentProbeError(
                        "scrubbed renderer tuple is outside the frozen probe")
                payload = {**payload, "candidate_id": opaque}
        return payload

    def __getattr__(self, name: str) -> Any:
        return getattr(json, name)


class _V4ConnectionAdapter:
    """Translate an opaque in-memory control ID for pinned asset lookup only."""

    def __init__(self, connection: sqlite3.Connection,
                 real_by_opaque: Mapping[str, str]):
        self._connection = connection
        self._real_by_opaque = dict(real_by_opaque)

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        normalized = " ".join(sql.casefold().split())
        values = tuple(parameters)
        if "from direct_image_evidence where candidate_id=?" in normalized:
            if len(values) != 1 or str(values[0]) not in self._real_by_opaque:
                raise IdentityInstrumentProbeError(
                    "asset lookup used an unknown opaque control ID")
            values = (self._real_by_opaque[str(values[0])],)
        return self._connection.execute(sql, values)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _correct_fetch_manifest_payload(
        payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Correct only the inherited WP2.5 fetch-cap literal."""
    hard_caps = payload.get("hard_caps")
    if not isinstance(hard_caps, Mapping):
        raise IdentityInstrumentProbeError("fetch manifest hard_caps are missing")
    normalized = {str(key): value for key, value in hard_caps.items()}
    if normalized == dict(CORRECTED_FETCH_HARD_CAPS):
        return payload
    if normalized != dict(LEGACY_FETCH_HARD_CAPS):
        raise IdentityInstrumentProbeError("unexpected inherited fetch manifest hard_caps")
    corrected = dict(payload)
    corrected["hard_caps"] = dict(CORRECTED_FETCH_HARD_CAPS)
    return corrected


def _fetch_manifest_atomic_writer(
        fetch_manifest: Path) -> Callable[..., None]:
    """Wrap the pinned writer without changing any non-target payload."""
    target = fetch_manifest.resolve()

    def atomic_json(path: Path, payload: Mapping[str, Any], *,
                    allow_identical: bool = False) -> None:
        output = Path(path)
        if output.resolve() == target:
            payload = _correct_fetch_manifest_payload(payload)
        wp25._atomic_json(output, payload, allow_identical=allow_identical)

    return atomic_json


def _assert_wp25_producer_pin(root: Path) -> None:
    producer = _path(root, DEFAULT_WP25_PRODUCER)
    current = (producer.stat().st_size, wp25.sha256_file(producer))
    if current != WP25_PRODUCER_SPEC:
        raise IdentityInstrumentProbeError("pinned WP2.5 producer drift")


def _control_identity_maps(artifact: Path, root: Path) -> tuple[
        Path, dict[tuple[str, str, str, str], str], dict[str, str]]:
    sidecar = sqlite3.connect(artifact); sidecar.row_factory = sqlite3.Row
    try:
        run = _one_run(sidecar); run_id = str(run["run_id"])
        pin = sidecar.execute(
            "SELECT path FROM input_pins WHERE run_id=? AND role='v4_candidates_final'",
            (run_id,),
        ).fetchone()
        if pin is None:
            raise IdentityInstrumentProbeError("v4 input pin is missing")
        rows = sidecar.execute(
            "SELECT p.candidate_id,s.candidate_json FROM sample_pairs p "
            "JOIN candidate_snapshots s USING(run_id,sample_id) "
            "WHERE p.run_id=? ORDER BY p.sample_rank", (run_id,),
        ).fetchall()
    finally:
        sidecar.close()
    if len(rows) != TOTAL_COUNT:
        raise IdentityInstrumentProbeError("renderer identity map is not 24 rows")
    v4_path = _resolve_text(root, str(pin["path"])); v4 = _open_ro(v4_path)
    opaque_by_source: dict[tuple[str, str, str, str], str] = {}
    real_by_opaque: dict[str, str] = {}
    try:
        for row in rows:
            candidate = json.loads(str(row["candidate_json"]))
            source_tuple = _source_tuple(candidate)
            matches = v4.execute(
                "SELECT candidate_id FROM candidate_pairs WHERE left_source=? "
                "AND left_source_building_id=? AND right_source=? "
                "AND right_source_building_id=?", source_tuple,
            ).fetchall()
            if len(matches) != 1 or source_tuple in opaque_by_source:
                raise IdentityInstrumentProbeError(
                    "frozen source tuple does not uniquely resolve in pinned v4")
            opaque = str(row["candidate_id"])
            if not opaque.startswith("ctl_") or opaque in real_by_opaque:
                raise IdentityInstrumentProbeError("opaque control ID invariant failed")
            opaque_by_source[source_tuple] = opaque
            real_by_opaque[opaque] = str(matches[0][0])
    finally:
        v4.close()
    return v4_path, opaque_by_source, real_by_opaque


def _wp25_adapter(*, root: Path, artifact: Path, prepare: bool,
                  fetch_manifest_path: Path | None = None,
                  ) -> dict[str, Callable[..., Any]]:
    """Return isolated, parameterized clones of the pinned WP2.5 instrument."""
    _assert_wp25_producer_pin(root)
    globals_copy = dict(vars(wp25))
    globals_copy.update({
        "TOTAL_COUNT": TOTAL_COUNT,
        "SAMPLE_VERSION": SAMPLE_VERSION,
        "REVIEW_VERSION": REVIEW_VERSION,
        "SEED": SEED,
        "MAX_ASSETS_PER_SIDE": MAX_ASSETS_PER_SIDE,
        "MAX_ASSET_OCCURRENCES": MAX_ASSET_OCCURRENCES,
        "MAX_LOGICAL_HTTP_FETCHES": MAX_LOGICAL_HTTP_FETCHES,
        "MAX_WIRE_HTTP_REQUESTS": MAX_WIRE_HTTP_REQUESTS,
        "MAX_WORKERS": MAX_WORKERS,
    })
    if prepare:
        if fetch_manifest_path is None:
            raise IdentityInstrumentProbeError(
                "prepare adapter requires the fetch manifest path")
        globals_copy["_atomic_json"] = _fetch_manifest_atomic_writer(
            _path(root, fetch_manifest_path))
        v4_path, opaque_by_source, real_by_opaque = _control_identity_maps(
            artifact, root)
        globals_copy["json"] = _ProbeJsonAdapter(opaque_by_source)
        original_open = wp25._open_immutable

        def isolated_open(path: Path) -> Any:
            connection = original_open(path)
            if Path(path).resolve() == v4_path.resolve():
                return _V4ConnectionAdapter(connection, real_by_opaque)
            return connection

        globals_copy["_open_immutable"] = isolated_open
    for name in (
            "_logical_sha", "_blind_lane_items", "_form_payload",
            "_validate_completed_review_form", "_choose_assets"):
        globals_copy[name] = _clone_function(getattr(wp25, name), globals_copy)
    return {
        "prepare_review": _clone_function(wp25.prepare_review, globals_copy),
        "lock_review": _clone_function(wp25.lock_review, globals_copy),
        "logical_sha": globals_copy["_logical_sha"],
    }


def _seed_rank(group: str, candidate_id: str) -> str:
    raw = f"{SAMPLE_VERSION}\0{SEED}\0{group}\0{candidate_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _commitment(candidate_id: str, group: str, n100_sample_id: str, gold: str,
                adjudication_record_sha256: str) -> str:
    return canonical_sha256({
        "candidate_id": candidate_id,
        "group": group,
        "n100_sample_id": n100_sample_id,
        "final_gold_label": gold,
        "adjudication_record_sha256": adjudication_record_sha256,
    })


def _pin(root: Path, path: Path, usage: str, expected: tuple[int, str] | None,
         verify: bool) -> dict[str, Any]:
    return wp25._pin_file(root, path, usage, expected if verify else None)


def _logical_sha(connection: sqlite3.Connection, run_id: str) -> str:
    globals_copy = dict(vars(wp25))
    globals_copy["SAMPLE_VERSION"] = SAMPLE_VERSION
    return _clone_function(wp25._logical_sha, globals_copy)(connection, run_id)


def _refresh(connection: sqlite3.Connection, run_id: str) -> None:
    wp25._refresh_run_record(connection, run_id)


def _freeze_rank(candidate_id: str) -> str:
    """Neutral persisted order; it cannot disclose the hidden control group."""
    raw = f"{SAMPLE_VERSION}\0{SEED}\0probe\0{candidate_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _open_ro(path: Path) -> sqlite3.Connection:
    return wp25._open_immutable(path)


def _one_run(connection: sqlite3.Connection) -> sqlite3.Row:
    rows = connection.execute("SELECT * FROM runs").fetchall()
    if len(rows) != 1:
        raise IdentityInstrumentProbeError("input must contain exactly one run")
    return rows[0]


def _form_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _forms_from_locks(root: Path, connection: sqlite3.Connection,
                      label: str) -> dict[str, tuple[Path, dict[str, Any]]]:
    rows = connection.execute(
        "SELECT lane,completed_form_path FROM review_locks ORDER BY lane"
    ).fetchall()
    if len(rows) != 2 or {str(row["lane"]) for row in rows} != {"r1", "r2"}:
        raise IdentityInstrumentProbeError(f"{label} must have two reviewer locks")
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    for row in rows:
        lane = str(row["lane"])
        path = _form_path(root, str(row["completed_form_path"]))
        if not path.is_file():
            raise IdentityInstrumentProbeError(f"{label} completed form missing: {lane}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        result[lane] = (path, payload)
    return result


_AXIS_TAGS = ("image=", "name=", "firm=", "place=", "year=", "scope=", "basis=")


def _ordered_axis_tags(value: str) -> bool:
    positions = [value.find(tag) for tag in _AXIS_TAGS]
    return all(position >= 0 for position in positions) and positions == sorted(positions)


def _step0_rows(root: Path, n100: sqlite3.Connection,
                band: sqlite3.Connection,
                n100_forms: Mapping[str, tuple[Path, Mapping[str, Any]]],
                band_forms: Mapping[str, tuple[Path, Mapping[str, Any]]]
                ) -> list[dict[str, Any]]:
    def form_schema(forms: Mapping[str, tuple[Path, Mapping[str, Any]]]) -> dict[str, Any]:
        top: set[str] = set()
        item: set[str] = set()
        for _, payload in forms.values():
            top.update(str(key) for key in payload)
            items = payload.get("items", [])
            if not isinstance(items, list) or not items:
                raise IdentityInstrumentProbeError("completed reviewer form has no items")
            for value in items:
                if not isinstance(value, Mapping):
                    raise IdentityInstrumentProbeError("review form item is not an object")
                item.update(str(key) for key in value)
        return {"top_level_keys": sorted(top), "item_keys": sorted(item)}

    def review_stats(connection: sqlite3.Connection) -> dict[str, Any]:
        records = connection.execute(
            "SELECT lane,label,rationale,hard_conflict_observed FROM blind_reviews ORDER BY lane,opaque_review_id"
        ).fetchall()
        if not records:
            raise IdentityInstrumentProbeError("frozen review payload is empty")
        lengths: dict[str, list[int]] = {"r1": [], "r2": []}
        labels: Counter[str] = Counter()
        conflicts: Counter[str] = Counter()
        tagged = 0
        for record in records:
            lane, rationale = str(record["lane"]), str(record["rationale"])
            lengths.setdefault(lane, []).append(len(rationale))
            labels[str(record["label"])] += 1
            conflicts[str(record["hard_conflict_observed"])] += 1
            tagged += int(_ordered_axis_tags(rationale))
        return {
            "row_count": len(records), "axis_tagged": tagged,
            "rationale_lengths": {
                lane: {"min": min(values), "max": max(values)}
                for lane, values in sorted(lengths.items()) if values
            },
            "labels": dict(sorted(labels.items())),
            "hard_conflicts": dict(sorted(conflicts.items())),
        }

    n_schema, b_schema = form_schema(n100_forms), form_schema(band_forms)
    n_stats, b_stats = review_stats(n100), review_stats(band)
    n_columns = [str(row[1]) for row in n100.execute("PRAGMA table_info(blind_reviews)")]
    b_columns = [str(row[1]) for row in band.execute("PRAGMA table_info(blind_reviews)")]
    axes = [tag[:-1] for tag in _AXIS_TAGS]
    n_axis_columns = sorted(set(n_columns) & set(axes))
    b_axis_columns = sorted(set(b_columns) & set(axes))

    band_blank_distinct = True
    for lane, (completed_path, _) in band_forms.items():
        row = band.execute(
            "SELECT path FROM artifact_paths WHERE role=?", (f"{lane}_blank_form",)
        ).fetchone()
        if row is None or _form_path(root, str(row[0])) == completed_path:
            band_blank_distinct = False
    rows = [
        ("top_level_json_keys", n_schema["top_level_keys"], b_schema["top_level_keys"],
         "same top-level form structure"),
        ("item_json_keys", n_schema["item_keys"], b_schema["item_keys"],
         "same item form structure"),
        ("axis_json_or_db_columns", n_axis_columns, b_axis_columns,
         "neither producer has separate axis columns"),
        ("ordered_axis_tags", {"tagged": n_stats["axis_tagged"], "rows": n_stats["row_count"]},
         {"tagged": b_stats["axis_tagged"], "rows": b_stats["row_count"]},
         "N100 used ordered tags operationally; WP2.5 did not"),
        ("rationale_lengths", n_stats["rationale_lengths"], b_stats["rationale_lengths"],
         "WP2.5 rationale is materially shorter"),
        ("actual_label_usage", n_stats["labels"], b_stats["labels"],
         "WP2.5 did not exercise the intermediate labels"),
        ("hard_conflict_payload", n_stats["hard_conflicts"], b_stats["hard_conflicts"],
         "WP2.5 retained a weaker yes/no operational value"),
        ("blank_completed_provenance", {"blank_distinct": False},
         {"blank_distinct": band_blank_distinct},
         "WP2.5 preserves distinct blank and completed forms"),
    ]
    return [{"comparison_key": key, "n100": left, "wp25": right, "finding": finding}
            for key, left, right, finding in rows]


def _candidate_flags(v4: sqlite3.Connection, candidate_id: str) -> list[str]:
    return [str(row[0]) for row in v4.execute(
        "SELECT flag_type FROM candidate_policy_flags WHERE candidate_id=? ORDER BY flag_type",
        (candidate_id,),
    )]


def _select_controls(n100: sqlite3.Connection, v4: sqlite3.Connection,
                     band: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [dict(row) for row in n100.execute(
        "SELECT p.sample_rank,p.sample_id,p.candidate_id,p.cohort,p.stratum,"
        "a.final_gold_label,a.record_sha256 AS adjudication_record_sha256 "
        "FROM sample_pairs p JOIN adjudications a USING(run_id,sample_id) "
        "ORDER BY p.sample_rank"
    )]
    wp25_ids = {str(row[0]) for row in band.execute("SELECT candidate_id FROM sample_pairs")}
    if len(wp25_ids) != 221:
        raise IdentityInstrumentProbeError("pinned WP2.5 sample is not 221 unique pairs")

    a_non_same = [row for row in rows
                  if row["cohort"] == "A" and row["final_gold_label"] != "same"]
    p1 = [row for row in a_non_same if not _candidate_flags(v4, str(row["candidate_id"]))]
    p2 = [row for row in rows if row["stratum"] == "B3_fanout_defer"
          and row["final_gold_label"] in {"related", "insufficient"}]
    p3 = [row for row in rows if row["stratum"] == "B2_phash_veto"
          and row["final_gold_label"] == "insufficient"]
    d_universe = [row for row in rows
                  if row["cohort"] == "A" and row["final_gold_label"] == "same"]
    d = sorted(d_universe, key=lambda row: (
        _seed_rank("D", str(row["candidate_id"])), str(row["candidate_id"])))[:15]

    if len(a_non_same) != 4 or len(p1) != 3:
        raise IdentityInstrumentProbeError("P1 universe/current-v4 flag accounting drift")
    if Counter(str(row["final_gold_label"]) for row in p1) != Counter(
            {"insufficient": 2, "related": 1}):
        raise IdentityInstrumentProbeError("P1 frozen gold composition drift")
    if len(p2) != 4 or Counter(str(row["final_gold_label"]) for row in p2) != Counter(
            {"insufficient": 3, "related": 1}):
        raise IdentityInstrumentProbeError("P2 frozen gold composition drift")
    if len(p3) != 2 or Counter(str(row["final_gold_label"]) for row in p3) != Counter(
            {"insufficient": 2}):
        raise IdentityInstrumentProbeError("P3 frozen gold composition drift")
    if len(d_universe) != D_UNIVERSE_COUNT or len(d) != 15:
        raise IdentityInstrumentProbeError("D frozen same universe drift")

    selected: list[dict[str, Any]] = []
    for group, values in (("P1", p1), ("P2", p2), ("P3", p3), ("D", d)):
        for row in values:
            selected.append({**row, "group": group,
                             "group_seed_rank": _seed_rank(group, str(row["candidate_id"])),
                             "freeze_rank": _freeze_rank(str(row["candidate_id"]))})
    candidate_ids = {str(row["candidate_id"]) for row in selected}
    n100_sample_ids = {str(row["sample_id"]) for row in selected}
    if len(selected) != TOTAL_COUNT or len(candidate_ids) != TOTAL_COUNT or len(n100_sample_ids) != TOTAL_COUNT:
        raise IdentityInstrumentProbeError("24-row control uniqueness invariant failed")
    overlap = candidate_ids & wp25_ids
    if overlap:
        raise IdentityInstrumentProbeError("probe overlaps the frozen WP2.5 sample")
    missing = [candidate_id for candidate_id in candidate_ids if v4.execute(
        "SELECT 1 FROM candidate_pairs WHERE candidate_id=?", (candidate_id,)).fetchone() is None]
    if missing:
        raise IdentityInstrumentProbeError("selected N100 candidate is absent from current v4")
    selected.sort(key=lambda row: (str(row["freeze_rank"]), str(row["candidate_id"])))
    metrics = {
        "p1_raw_non_same": len(a_non_same), "p1_eligible": len(p1),
        "p2_eligible": len(p2), "p3_eligible": len(p3),
        "d_universe": len(d_universe), "d_selected": len(d),
        "wp25_population": len(wp25_ids), "wp25_overlap": 0,
    }
    return selected, metrics


def _input_pins(root: Path, *, v4_path: Path, n100_path: Path,
                n100_manifest_path: Path, n100_locked_path: Path,
                wp25_path: Path, wp25_manifest_path: Path,
                protocol_path: Path, order_path: Path,
                wp25_producer_path: Path, wp25_protocol_path: Path,
                verify_pins: bool) -> dict[str, dict[str, Any]]:
    specs = {
        "v4_candidates_final": (v4_path, "immutable_candidate_population", V4_SPEC),
        "wp23_n100": (n100_path, "frozen_control_gold_source", N100_SPEC),
        "wp23_n100_manifest": (n100_manifest_path, "n100_release_manifest", N100_MANIFEST_SPEC),
        "wp23_n100_locked": (n100_locked_path, "n100_locked_gold_ledger", N100_LOCKED_SPEC),
        "wp25_sample": (wp25_path, "frozen_disjoint_sample", WP25_SPEC),
        "wp25_manifest": (wp25_manifest_path, "wp25_release_manifest", WP25_MANIFEST_SPEC),
        "wp26_protocol": (protocol_path, "probe_protocol", None),
        "wp26_order": (order_path, "job_order", None),
        "wp25_producer": (wp25_producer_path, "reused_producer", WP25_PRODUCER_SPEC),
        "wp25_protocol": (wp25_protocol_path, "reused_instrument_protocol", None),
    }
    return {role: _pin(root, path, usage, expected, verify_pins)
            for role, (path, usage, expected) in specs.items()}


def _sample_manifest_payload(connection: sqlite3.Connection, root: Path,
                             artifact: Path, run_id: str) -> dict[str, Any]:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise IdentityInstrumentProbeError("probe run missing")
    comparison = [{
        "comparison_key": str(row["comparison_key"]),
        "n100": json.loads(str(row["n100_value_json"])),
        "wp25": json.loads(str(row["wp25_value_json"])),
        "finding": str(row["finding"]),
    } for row in connection.execute(
        "SELECT * FROM form_comparison WHERE run_id=? ORDER BY comparison_key", (run_id,))]
    samples = [{
        "sample_rank": int(row["sample_rank"]), "sample_id": str(row["sample_id"]),
        "seed_rank_sha256": str(row["seed_rank_sha256"]),
        "control_commitment_sha256": str(row["gold_commitment_sha256"]),
    } for row in connection.execute(
        "SELECT p.sample_rank,p.sample_id,p.seed_rank_sha256,c.gold_commitment_sha256 "
        "FROM sample_pairs p JOIN control_commitments c USING(run_id,sample_id) "
        "WHERE p.run_id=? ORDER BY p.sample_rank", (run_id,))]
    pins = {str(row["role"]): {
        "path": str(row["path"]), "size_bytes": int(row["size_bytes"]),
        "sha256_before": str(row["sha256_before"]), "sha256_after": str(row["sha256_after"]),
        "usage": str(row["usage"]),
    } for row in connection.execute("SELECT * FROM input_pins WHERE run_id=? ORDER BY role", (run_id,))}
    frames = {str(row["stratum"]): {
        "raw": int(row["raw_count"]), "flagged": int(row["flagged_count"]),
        "eligible": int(row["eligible_count"]), "quota": int(row["quota"]),
    } for row in connection.execute(
        "SELECT * FROM population_frames WHERE run_id=? ORDER BY stratum", (run_id,))}
    quota = connection.execute(
        "SELECT * FROM quota_observations WHERE run_id=? AND phase='pre_freeze'", (run_id,)
    ).fetchone()
    return {
        "status": "PASS", "run_id": run_id,
        "versions": {"runner": RUNNER_VERSION, "sample": SAMPLE_VERSION,
                     "review": REVIEW_VERSION, "gate": GATE_VERSION},
        "seed": SEED, "counts": {"total": TOTAL_COUNT, "groups": dict(GROUP_QUOTAS),
                                  "post_freeze_replacements": 0},
        "population_frames": frames, "step0_form_comparison": comparison,
        "samples": samples, "sample_logical_sha256": str(run["sample_logical_sha256"]),
        "quota_pre_freeze": {
            "observed_at": str(quota["observed_at"]),
            "remaining_percent": float(quota["gate_basis_remaining_percent"]),
            "limitation": str(quota["limitation"]),
        },
        "sidecar": {"path": _path_text(root, artifact),
                    "size_bytes": artifact.stat().st_size,
                    "sha256": wp25.sha256_file(artifact)},
        "input_pins": pins, "network_requests": 0, "vision_requests": 0,
        "llm_requests": 0, "paid_api_requests": 0, "merge_executions": 0,
        "vnext_id_issuances": 0, "tier_promotions": 0,
        "neon_writes": 0, "r2_writes": 0,
    }


def build_sample(*, repo_root: Path, quota_remaining_percent: float,
                 quota_observed_at: str, quota_source: str,
                 quota_account_index: int, quota_limitation: str,
                 v4_path: Path = DEFAULT_V4, n100_path: Path = DEFAULT_N100,
                 n100_manifest_path: Path = DEFAULT_N100_MANIFEST,
                 n100_locked_path: Path = DEFAULT_N100_LOCKED,
                 wp25_path: Path = DEFAULT_WP25,
                 wp25_manifest_path: Path = DEFAULT_WP25_MANIFEST,
                 protocol_path: Path = DEFAULT_PROTOCOL,
                 order_path: Path = DEFAULT_ORDER,
                 wp25_producer_path: Path = DEFAULT_WP25_PRODUCER,
                 wp25_protocol_path: Path = DEFAULT_WP25_PROTOCOL,
                 output_path: Path = DEFAULT_OUTPUT,
                 manifest_path: Path = DEFAULT_SAMPLE_MANIFEST,
                 verify_pins: bool = True) -> BuildResult:
    """Step 0 then Step 1: freeze 24 controls without persisting group/gold mapping."""
    root = repo_root.resolve()
    if not 0 <= quota_remaining_percent <= 100 or quota_remaining_percent <= 30:
        raise IdentityInstrumentProbeError("pre-freeze quota must be above 30%")
    if not quota_observed_at.strip() or not quota_source.strip() or not quota_limitation.strip():
        raise IdentityInstrumentProbeError("explicit quota evidence is required")
    wp25._quota_time(quota_observed_at)
    output, manifest = _path(root, output_path), _path(root, manifest_path)
    if output.exists() and not manifest.exists():
        return recover_sample_manifest(repo_root=root, output_path=output,
                                       manifest_path=manifest)
    if output.exists() or manifest.exists():
        raise IdentityInstrumentProbeError("refusing to clobber probe sample artifact")
    temporary = output.with_name(output.name + ".building")
    failed = output.with_name(output.name + ".building.failed")
    if temporary.exists() or failed.exists():
        raise IdentityInstrumentProbeError("stale probe build artifact exists")

    paths = {"v4": _path(root, v4_path), "n100": _path(root, n100_path),
             "band": _path(root, wp25_path)}
    pins = _input_pins(
        root, v4_path=v4_path, n100_path=n100_path,
        n100_manifest_path=n100_manifest_path, n100_locked_path=n100_locked_path,
        wp25_path=wp25_path, wp25_manifest_path=wp25_manifest_path,
        protocol_path=protocol_path, order_path=order_path,
        wp25_producer_path=wp25_producer_path,
        wp25_protocol_path=wp25_protocol_path, verify_pins=verify_pins)
    v4 = _open_ro(paths["v4"]); n100 = _open_ro(paths["n100"]); band = _open_ro(paths["band"])
    database: sqlite3.Connection | None = None
    try:
        n100_run, band_run, v4_run = _one_run(n100), _one_run(band), _one_run(v4)
        if str(n100_run["status"]) != "pass" or str(band_run["status"]) != "pass":
            raise IdentityInstrumentProbeError("N100 and WP2.5 must be terminal PASS inputs")
        if str(v4_run["status"]) not in {"stage_a_complete", "pass"}:
            raise IdentityInstrumentProbeError("v4 must be a completed immutable input")
        n100_forms = _forms_from_locks(root, n100, "N100")
        band_forms = _forms_from_locks(root, band, "WP2.5")
        for prefix, forms in (("n100", n100_forms), ("wp25", band_forms)):
            for lane, (path, _) in forms.items():
                pins[f"{prefix}_{lane}_completed_form"] = _pin(
                    root, path, f"step0_{prefix}_{lane}_payload", None, False)
        comparison = _step0_rows(root, n100, band, n100_forms, band_forms)
        selected, metrics = _select_controls(n100, v4, band)

        output.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(temporary); database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(PROBE_SCHEMA_SQL)
        run_id = "wp26-" + hashlib.sha256(
            f"{SAMPLE_VERSION}\0{SEED}\0{pins['v4_candidates_final']['sha256_before']}\0"
            f"{pins['wp23_n100']['sha256_before']}\0{pins['wp25_sample']['sha256_before']}".encode()
        ).hexdigest()[:24]
        created = wp25.utc_now()
        run_payload = {"run_id": run_id, "schema_version": SCHEMA_VERSION,
                       "runner_version": RUNNER_VERSION, "sample_version": SAMPLE_VERSION,
                       "review_version": REVIEW_VERSION,
                       "adjudication_version": ADJUDICATION_VERSION,
                       "gate_version": GATE_VERSION, "seed": SEED,
                       "status": "building", "created_at": created}
        database.execute(
            "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
            "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, SCHEMA_VERSION, RUNNER_VERSION, SAMPLE_VERSION, REVIEW_VERSION,
             ADJUDICATION_VERSION, GATE_VERSION, SEED, "building", created, _record(run_payload)))
        for role, pin in sorted(pins.items()):
            payload = {"role": role, **pin}
            database.execute("INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)",
                             (run_id, role, pin["path"], pin["size_bytes"],
                              pin["sha256_before"], None, pin["usage"], _record(payload)))
        wp25._register_artifact_path(database, root, run_id, "sample_manifest", manifest, "freeze")
        signals = [{"source": quota_source.strip(), "account_index": quota_account_index,
                    "observed_at": quota_observed_at.strip(),
                    "remaining_percent": float(quota_remaining_percent)}]
        quota_payload = {"observation_no": 1, "phase": "pre_freeze",
                         "account_index": quota_account_index,
                         "observed_at": quota_observed_at.strip(), "signals": signals,
                         "gate_basis_remaining_percent": float(quota_remaining_percent),
                         "start_allowed": 1, "stop_required": 0,
                         "limitation": quota_limitation.strip()}
        database.execute("INSERT INTO quota_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, 1, "pre_freeze", quota_account_index,
                          quota_observed_at.strip(), canonical_json(signals),
                          float(quota_remaining_percent), 1, 0,
                          quota_limitation.strip(), _record(quota_payload)))
        for row in comparison:
            payload = {"comparison_key": row["comparison_key"], "n100": row["n100"],
                       "wp25": row["wp25"], "finding": row["finding"]}
            database.execute("INSERT INTO form_comparison VALUES(?,?,?,?,?,?)",
                             (run_id, row["comparison_key"], canonical_json(row["n100"]),
                              canonical_json(row["wp25"]), row["finding"], _record(payload)))
        wp25._insert_event(
            database, run_id, "building", "building",
            {"step": 0, "comparison_rows": len(comparison),
             "comparison_contract_sha256": canonical_sha256(comparison)},
        )
        frame_rows = {
            "P1": (metrics["p1_raw_non_same"], 1, metrics["p1_eligible"], 3),
            "P2": (metrics["p2_eligible"], 0, metrics["p2_eligible"], 4),
            "P3": (metrics["p3_eligible"], 0, metrics["p3_eligible"], 2),
            "D": (metrics["d_universe"], 0, metrics["d_universe"], 15),
        }
        for group, (raw, flagged, eligible, quota) in frame_rows.items():
            payload = {"stratum": group, "raw": raw, "flagged": flagged,
                       "ex_related": 0, "prior": 0, "eligible": eligible,
                       "quota": quota, "predicate_version": "wp26-control-frame-v1"}
            database.execute("INSERT INTO population_frames VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (run_id, group, raw, flagged, 0, 0, eligible, quota,
                              payload["predicate_version"], _record(payload)))
        for rank, control in enumerate(selected, 1):
            candidate_id = str(control["candidate_id"])
            opaque_control_id = "ctl_" + hashlib.sha256(
                f"{run_id}\0opaque-control\0{candidate_id}".encode()).hexdigest()[:32]
            sample_id = "probe_" + hashlib.sha256(
                f"{run_id}\0{opaque_control_id}".encode()).hexdigest()[:24]
            persisted_rank = str(control["freeze_rank"])
            pair_payload = {"sample_rank": rank, "sample_id": sample_id,
                            "stratum": "probe", "candidate_id": opaque_control_id,
                            "seed_rank": persisted_rank}
            database.execute("INSERT INTO sample_pairs VALUES(?,?,?,?,?,?,?)",
                             (run_id, rank, sample_id, "probe", opaque_control_id,
                              persisted_rank, _record(pair_payload)))
            context = wp25._candidate_context(v4, candidate_id)
            candidate = context["candidate"]
            scrubbed = {
                "candidate": {
                    key: candidate[key] for key in (
                        "left_source", "left_source_building_id",
                        "right_source", "right_source_building_id")
                },
                "left": context["left"], "right": context["right"],
                "origins": [], "families": [], "conflicts": [],
                "policy_flags": [], "transition": {},
            }
            snapshot_sha = canonical_sha256(scrubbed)
            database.execute("INSERT INTO candidate_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (run_id, sample_id, canonical_json(scrubbed["candidate"]),
                              canonical_json(scrubbed["left"]), canonical_json(scrubbed["right"]),
                              "[]", "[]", "[]", "[]", "{}", snapshot_sha,
                              _record({"sample_id": sample_id, "snapshot_sha256": snapshot_sha})))
            commitment = _commitment(
                candidate_id, str(control["group"]), str(control["sample_id"]),
                str(control["final_gold_label"]),
                str(control["adjudication_record_sha256"]))
            source_commitment = canonical_sha256({
                "namespace": "wp26-source-record-commitment-v1",
                "candidate_id": candidate_id,
                "source_adjudication_record_sha256":
                    str(control["adjudication_record_sha256"]),
            })
            commit_payload = {"sample_id": sample_id,
                              "gold_commitment_sha256": commitment,
                              "source_record_commitment_sha256": source_commitment}
            database.execute("INSERT INTO control_commitments VALUES(?,?,?,?,?)",
                             (run_id, sample_id, commitment,
                              source_commitment,
                              _record(commit_payload)))
        for role, pin in sorted(pins.items()):
            current = wp25.sha256_file(_resolve_text(root, pin["path"]))
            if current != pin["sha256_before"]:
                raise IdentityInstrumentProbeError(f"input drift during freeze: {role}")
            pin["sha256_after"] = current
            database.execute("UPDATE input_pins SET sha256_after=?,record_sha256=? WHERE run_id=? AND role=?",
                             (current, _record({"role": role, **pin}), run_id, role))
        logical = _logical_sha(database, run_id)
        database.execute("UPDATE runs SET status='sample_frozen',sample_count=?,sample_logical_sha256=? WHERE run_id=?",
                         (TOTAL_COUNT, logical, run_id)); _refresh(database, run_id)
        wp25._insert_event(database, run_id, "building", "sample_frozen",
                           {"step": 1, "sample_count": TOTAL_COUNT,
                            "sample_logical_sha256": logical,
                            "post_freeze_replacements": 0})
        database.commit(); database.close(); database = None
        v4.close(); n100.close(); band.close()
        os.replace(temporary, output)
        connection = sqlite3.connect(output); connection.row_factory = sqlite3.Row
        payload = _sample_manifest_payload(connection, root, output, run_id)
        connection.close()
        wp25._atomic_json(manifest, payload)
        return BuildResult(output, manifest, run_id, TOTAL_COUNT, logical)
    except Exception:
        if database is not None:
            database.close()
        for connection in (v4, n100, band):
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if temporary.exists() and not failed.exists():
            os.replace(temporary, failed)
        raise


def recover_sample_manifest(*, repo_root: Path, output_path: Path = DEFAULT_OUTPUT,
                            manifest_path: Path = DEFAULT_SAMPLE_MANIFEST) -> BuildResult:
    root = repo_root.resolve(); output, manifest = _path(root, output_path), _path(root, manifest_path)
    if not output.is_file() or manifest.exists():
        raise IdentityInstrumentProbeError("sample recovery requires DB present and manifest absent")
    connection = sqlite3.connect(output); connection.row_factory = sqlite3.Row
    try:
        run = _one_run(connection); run_id = str(run["run_id"])
        if str(run["status"]) != "sample_frozen" or int(run["sample_count"]) != TOTAL_COUNT:
            raise IdentityInstrumentProbeError("sample recovery state is invalid")
        logical = _logical_sha(connection, run_id)
        if logical != str(run["sample_logical_sha256"]):
            raise IdentityInstrumentProbeError("sample recovery logical SHA mismatch")
        payload = _sample_manifest_payload(connection, root, output, run_id)
    finally:
        connection.close()
    wp25._atomic_json(manifest, payload)
    return BuildResult(output, manifest, run_id, TOTAL_COUNT, logical)


def _gate_payload() -> dict[str, Any]:
    return {
        **GATE_THRESHOLDS, "p1_n": 3, "p23_n": 6, "d_n": 15,
        "gate_version": GATE_VERSION,
        "predicate": (
            "P1 non-same >= 2/3 AND P2+P3 non-same >= 3/6 "
            "AND D same >= 13/15"
        ),
    }


def declare_gate(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                 sample_manifest_path: Path = DEFAULT_SAMPLE_MANIFEST) -> dict[str, Any]:
    """Step 2: durably declare the immutable gate after sample freeze."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    sample_manifest = _path(root, sample_manifest_path)
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        expected = _gate_payload(); contract_sha = canonical_sha256(expected)
        existing = connection.execute(
            "SELECT * FROM gate_contract WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing is not None:
            if (status != "sample_frozen" or str(run["gate_status"]) != "declared"
                    or str(existing["contract_sha256"]) != contract_sha):
                raise IdentityInstrumentProbeError("gate declaration recovery mismatch")
            if (_logical_sha(connection, run_id) != str(run["sample_logical_sha256"])
                    or not sample_manifest.is_file()):
                raise IdentityInstrumentProbeError("gate declaration recovery inputs changed")
            wp25._bind_artifact(connection, root, run_id, "sample_manifest",
                                sample_manifest, "freeze")
            return {**expected, "contract_sha256": contract_sha,
                    "declared_at": str(existing["declared_at"])}
        if status != "sample_frozen":
            raise IdentityInstrumentProbeError("gate declaration requires sample_frozen")
        if _logical_sha(connection, run_id) != str(run["sample_logical_sha256"]):
            raise IdentityInstrumentProbeError("sample changed before gate declaration")
        if not sample_manifest.is_file():
            raise IdentityInstrumentProbeError("sample manifest is missing")
        wp25._bind_artifact(connection, root, run_id, "sample_manifest",
                            sample_manifest, "freeze")
        declared = wp25.utc_now()
        row_payload = {**expected, "contract_sha256": contract_sha,
                       "declared_at": declared}
        connection.execute(
            "INSERT INTO gate_contract VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, expected["p1_min_non_same"], expected["p23_min_non_same"],
             expected["d_min_same"], expected["p1_n"], expected["p23_n"],
             expected["d_n"], contract_sha, declared, _record(row_payload)))
        connection.execute("UPDATE runs SET gate_status='declared' WHERE run_id=?",
                           (run_id,)); _refresh(connection, run_id)
        wp25._insert_event(connection, run_id, "sample_frozen", "sample_frozen",
                           {"step": 2, "gate_contract_sha256": contract_sha,
                            "logical_stage": "criteria_declared",
                            "thresholds": dict(GATE_THRESHOLDS)})
        connection.commit()
        return row_payload
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def _validate_quota_ledger(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no", (run_id,)
    ).fetchall()
    previous_index = -1
    previous_time = None
    for position, row in enumerate(rows):
        phase = str(row["phase"]); index = QUOTA_PHASE_INDEX.get(phase)
        if index is None or (position == 0 and phase != "pre_freeze") or (position and phase == "pre_freeze"):
            raise IdentityInstrumentProbeError("quota ledger phase is invalid")
        if index < previous_index or index > previous_index + 1:
            raise IdentityInstrumentProbeError("quota ledger order is invalid")
        observed = wp25._quota_time(row["observed_at"], require_fresh=False)
        if previous_time is not None and observed < previous_time:
            raise IdentityInstrumentProbeError("quota observation times are not monotonic")
        previous_index, previous_time = index, observed
    return rows


def record_quota_observation(*, repo_root: Path, phase: str,
                             remaining_percent: float, source: str,
                             observed_at: str, account_index: int = 0,
                             limitation: str = "",
                             artifact_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    if phase not in QUOTA_PHASE_INDEX or phase == "pre_freeze":
        raise IdentityInstrumentProbeError("quota phase is not an allowed runtime phase")
    if not 0 <= remaining_percent <= 100 or not source.strip() or not observed_at.strip():
        raise IdentityInstrumentProbeError("valid quota value/source/time are required")
    observed = wp25._quota_time(observed_at)
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        if status in {"pass", "fail", "failed"}:
            raise IdentityInstrumentProbeError("quota observation requires an active probe")
        _require_gate(connection, run_id)
        if status not in QUOTA_PHASE_RUN_STATUSES[phase]:
            raise IdentityInstrumentProbeError(
                f"quota phase {phase} is invalid while run status is {status}")
        rows = _validate_quota_ledger(connection, run_id)
        if not rows:
            raise IdentityInstrumentProbeError("quota ledger lacks pre_freeze")
        latest = rows[-1]; latest_index = QUOTA_PHASE_INDEX[str(latest["phase"])]
        requested_index = QUOTA_PHASE_INDEX[phase]
        if requested_index not in {latest_index, latest_index + 1}:
            raise IdentityInstrumentProbeError("quota phase is out of order")
        if observed < wp25._quota_time(latest["observed_at"], require_fresh=False):
            raise IdentityInstrumentProbeError("quota observation times are not monotonic")
        number = int(connection.execute(
            "SELECT coalesce(max(observation_no),0)+1 FROM quota_observations WHERE run_id=?",
            (run_id,),
        ).fetchone()[0])
        stop_required, start_allowed = remaining_percent <= 20, remaining_percent > 30
        signals = [{"source": source.strip(), "account_index": account_index,
                    "observed_at": observed_at.strip(),
                    "remaining_percent": float(remaining_percent)}]
        payload = {"observation_no": number, "phase": phase,
                   "account_index": account_index, "observed_at": observed_at.strip(),
                   "signals": signals, "gate_basis_remaining_percent": float(remaining_percent),
                   "start_allowed": int(start_allowed), "stop_required": int(stop_required),
                   "limitation": limitation.strip()}
        connection.execute("INSERT INTO quota_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id, number, phase, account_index, observed_at.strip(),
                            canonical_json(signals), float(remaining_percent),
                            int(start_allowed), int(stop_required), limitation.strip(),
                            _record(payload)))
        connection.commit()
        if stop_required:
            raise IdentityInstrumentProbeError("weekly quota is at or below 20%; stop")
        if not start_allowed:
            raise IdentityInstrumentProbeError("weekly quota is at or below 30%; wave denied")
        return {"run_id": run_id, **payload}
    finally:
        connection.close()


def _require_gate(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM gate_contract WHERE run_id=?", (run_id,)).fetchone()
    run = connection.execute("SELECT gate_status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    expected = _gate_payload()
    if (row is None or run is None or str(run["gate_status"]) != "declared"
            or str(row["contract_sha256"]) != canonical_sha256(expected)):
        raise IdentityInstrumentProbeError("immutable gate contract is missing or changed")
    for key in ("p1_min_non_same", "p23_min_non_same", "d_min_same", "p1_n", "p23_n", "d_n"):
        if int(row[key]) != int(expected[key]):
            raise IdentityInstrumentProbeError("gate threshold drift")
    return row


def prepare_review(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                   output_cache_path: Path = DEFAULT_OUTPUT_CACHE,
                   sheets_root: Path = DEFAULT_SHEETS_ROOT,
                   r1_form_path: Path = DEFAULT_R1_FORM,
                   r2_form_path: Path = DEFAULT_R2_FORM,
                   fetch_manifest_path: Path = DEFAULT_FETCH_MANIFEST,
                   fetch_report_path: Path = DEFAULT_FETCH_REPORT,
                   e2_spec: tuple[Path, int, str] | None = None,
                   e1_specs: Mapping[str, tuple[Path, int, str]] | None = None,
                   read_caches: Sequence[wp25.CacheSpec] = wp25.DEFAULT_READ_CACHES,
                   workers: int = 4, fetcher: Callable[..., FetchPayload] = fetch_once,
                   verify_source_pins: bool = True,
                   verify_cache_pins: bool = True) -> wp25.PrepareResult:
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    try:
        run = _one_run(connection); _require_gate(connection, str(run["run_id"]))
        if str(run["status"]) not in {"sample_frozen", "fetching", "review_ready"}:
            raise IdentityInstrumentProbeError("prepare requires a declared frozen probe")
    finally:
        connection.close()
    prepare = _wp25_adapter(
        root=root, artifact=artifact, prepare=True,
        fetch_manifest_path=fetch_manifest_path,
    )["prepare_review"]
    return prepare(
        repo_root=root, artifact_path=artifact_path,
        output_cache_path=output_cache_path, sheets_root=sheets_root,
        r1_form_path=r1_form_path, r2_form_path=r2_form_path,
        fetch_manifest_path=fetch_manifest_path, fetch_report_path=fetch_report_path,
        e2_spec=e2_spec, e1_specs=e1_specs, read_caches=read_caches,
        workers=workers, fetcher=fetcher, verify_source_pins=verify_source_pins,
        verify_cache_pins=verify_cache_pins)


def lock_review(*, repo_root: Path, lane: str, completed_form_path: Path,
                locked_output_path: Path,
                artifact_path: Path = DEFAULT_OUTPUT) -> Path:
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    try:
        run = _one_run(connection); _require_gate(connection, str(run["run_id"]))
    finally:
        connection.close()
    lock = _wp25_adapter(root=root, artifact=artifact, prepare=False)["lock_review"]
    return lock(
        repo_root=repo_root, lane=lane, completed_form_path=completed_form_path,
        locked_output_path=locked_output_path, artifact_path=artifact_path)


def _require_probe_quota(connection: sqlite3.Connection, run_id: str, phase: str) -> None:
    rows = _validate_quota_ledger(connection, run_id)
    if not rows:
        raise IdentityInstrumentProbeError(f"quota gate missing for {phase}")
    latest = rows[-1]
    if (str(latest["phase"]) != phase or not int(latest["start_allowed"])
            or int(latest["stop_required"])):
        raise IdentityInstrumentProbeError(f"quota gate missing for {phase}")
    wp25._quota_time(latest["observed_at"])


def _adjudication_payloads(connection: sqlite3.Connection, root: Path,
                           run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ranked: list[tuple[str, str, str]] = []
    for row in connection.execute(
            "SELECT sample_id FROM sample_pairs WHERE run_id=?", (run_id,)):
        sample_id = str(row[0])
        opaque = "adj_" + hashlib.sha256(
            f"{ADJUDICATION_VERSION}\0{run_id}\0{sample_id}".encode()).hexdigest()[:24]
        order = hashlib.sha256(
            f"{SEED}\0adjudication-order\0{sample_id}".encode()).hexdigest()
        ranked.append((order, opaque, sample_id))
    if len(ranked) != TOTAL_COUNT:
        raise IdentityInstrumentProbeError("adjudication packet requires 24 rows")
    items: list[dict[str, Any]] = []
    form_items: list[dict[str, Any]] = []
    for display_order, (_, opaque, sample_id) in enumerate(sorted(ranked), 1):
        reviews = {str(row["lane"]): row for row in connection.execute(
            "SELECT * FROM blind_reviews WHERE run_id=? AND sample_id=? ORDER BY lane",
            (run_id, sample_id),
        )}
        review_items = {str(row["lane"]): row for row in connection.execute(
            "SELECT * FROM review_items WHERE run_id=? AND sample_id=? ORDER BY lane",
            (run_id, sample_id),
        )}
        if set(reviews) != {"r1", "r2"} or set(review_items) != {"r1", "r2"}:
            raise IdentityInstrumentProbeError("adjudication row lacks two reviews")
        sheet_path = str(review_items["r1"]["sheet_path"])
        sheet_sha = str(review_items["r1"]["sheet_sha256"])
        sheet = _resolve_text(root, sheet_path)
        if not sheet.is_file() or wp25.sha256_file(sheet) != sheet_sha:
            raise IdentityInstrumentProbeError("adjudication evidence sheet changed")
        review_payload = {
            lane: {key: str(reviews[lane][key]) for key in (
                "opaque_review_id", "label", "rationale",
                "hard_conflict_observed", "reviewer")}
            for lane in ("r1", "r2")
        }
        item = {"display_order": display_order, "opaque_probe_id": opaque,
                "sheet_path": sheet_path, "sheet_sha256": sheet_sha,
                "reviews": review_payload}
        items.append(item)
        form_items.append({
            "display_order": display_order, "opaque_probe_id": opaque,
            "sheet_path": sheet_path, "sheet_sha256": sheet_sha,
            "r1_label": str(reviews["r1"]["label"]),
            "r2_label": str(reviews["r2"]["label"]),
            "adjudicator_label": "", "unresolved_disagreement": False,
            "rationale": "",
        })
        payload = {"display_order": display_order, "opaque_probe_id": opaque,
                   "sample_id": sample_id, "sheet_path": sheet_path,
                   "sheet_sha256": sheet_sha,
                   "r1_label": str(reviews["r1"]["label"]),
                   "r2_label": str(reviews["r2"]["label"])}
        connection.execute(
            "INSERT INTO probe_adjudication_items VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, display_order, opaque, sample_id, sheet_path, sheet_sha,
             payload["r1_label"], payload["r2_label"], _record(payload)))
    packet = {"adjudication_version": ADJUDICATION_VERSION, "run_id": run_id,
              "row_count": TOTAL_COUNT, "items": items}
    contract = {"adjudication_version": ADJUDICATION_VERSION, "run_id": run_id,
                "items": [{key: item[key] for key in (
                    "display_order", "opaque_probe_id", "sheet_path", "sheet_sha256",
                    "r1_label", "r2_label")}
                    for item in form_items]}
    form = {**contract, "form_contract_sha256": canonical_sha256(contract),
            "adjudicator": "", "adjudicated_at": "",
            "allowed_labels": sorted(ALLOWED_LABELS), "items": form_items}
    return packet, form


def prepare_adjudication(*, repo_root: Path,
                         artifact_path: Path = DEFAULT_OUTPUT,
                         packet_path: Path = DEFAULT_ADJUDICATION_PACKET,
                         adjudication_form_path: Path = DEFAULT_ADJUDICATION_FORM
                         ) -> tuple[Path, Path]:
    """Reveal only the two locked probe reviews, never controls or N100 gold."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    packet, form_path = _path(root, packet_path), _path(root, adjudication_form_path)
    if packet.resolve() == form_path.resolve():
        raise IdentityInstrumentProbeError("packet and adjudication form must be distinct")
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        run = _one_run(connection); run_id = str(run["run_id"])
        existing = connection.execute(
            "SELECT * FROM unblind_artifacts WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing is not None:
            stored = connection.execute(
                "SELECT * FROM unblind_payloads WHERE run_id=?", (run_id,)
            ).fetchone()
            if stored is None:
                raise IdentityInstrumentProbeError("adjudication recovery payload missing")
            packet_payload = json.loads(str(stored["packet_json"])); form_payload = json.loads(str(stored["form_json"]))
            packet_raw, form_raw = wp25._json_bytes(packet_payload), wp25._json_bytes(form_payload)
            if (_resolve_text(root, str(existing["packet_path"])).resolve() != packet.resolve()
                    or _resolve_text(root, str(existing["form_path"])).resolve() != form_path.resolve()
                    or hashlib.sha256(packet_raw).hexdigest() != str(existing["packet_sha256"])
                    or hashlib.sha256(form_raw).hexdigest() != str(existing["form_sha256"])):
                raise IdentityInstrumentProbeError("adjudication recovery binding mismatch")
            wp25._atomic_bytes(packet, packet_raw, allow_identical=True)
            wp25._atomic_bytes(form_path, form_raw, allow_identical=True)
            return packet, form_path
        if packet.exists() or form_path.exists():
            raise IdentityInstrumentProbeError(
                "refusing to clobber adjudication packet/form")
        if str(run["status"]) != "reviews_locked":
            raise IdentityInstrumentProbeError("both reviewer locks are required")
        _require_probe_quota(connection, run_id, "pre_adjudication_review")
        locks = connection.execute(
            "SELECT * FROM review_locks WHERE run_id=? ORDER BY lane", (run_id,)
        ).fetchall()
        if (len(locks) != 2 or {str(row["lane"]) for row in locks} != {"r1", "r2"}
                or any(int(row["row_count"]) != TOTAL_COUNT for row in locks)):
            raise IdentityInstrumentProbeError("two complete 24-row reviewer locks required")
        if wp25._normalized_actor(locks[0]["reviewer"]) == wp25._normalized_actor(locks[1]["reviewer"]):
            raise IdentityInstrumentProbeError("reviewers must be distinct")
        for lock in locks:
            locked_path = _resolve_text(root, str(lock["locked_output_path"]))
            if not locked_path.is_file() or wp25.sha256_file(locked_path) != str(lock["locked_output_sha256"]):
                raise IdentityInstrumentProbeError("reviewer lock missing or changed")
        if int(connection.execute(
                "SELECT count(*) FROM blind_reviews WHERE run_id=?", (run_id,)
        ).fetchone()[0]) != TOTAL_COUNT * 2:
            raise IdentityInstrumentProbeError("blind review ledger incomplete")
        packet_payload, form_payload = _adjudication_payloads(connection, root, run_id)
        _assert_blind_payload(
            {"packet": packet_payload, "form": form_payload},
            "adjudication payload")
        packet_raw, form_raw = wp25._json_bytes(packet_payload), wp25._json_bytes(form_payload)
        packet_sha, form_sha = hashlib.sha256(packet_raw).hexdigest(), hashlib.sha256(form_raw).hexdigest()
        wp25._register_artifact_path(connection, root, run_id, "unblind_packet", packet, "adjudication_prepare")
        wp25._register_artifact_path(connection, root, run_id, "adjudication_blank_form", form_path, "adjudication_prepare")
        connection.execute("INSERT INTO unblind_payloads VALUES(?,?,?,?)",
                           (run_id, canonical_json(packet_payload), canonical_json(form_payload),
                            _record({"packet_json": canonical_json(packet_payload),
                                     "form_json": canonical_json(form_payload)})))
        created = wp25.utc_now(); lock_map = {str(row["lane"]): row for row in locks}
        payload = {"packet_path": _path_text(root, packet), "packet_sha256": packet_sha,
                   "form_path": _path_text(root, form_path), "form_sha256": form_sha,
                   "r1_ledger_sha256": str(lock_map["r1"]["ordered_ledger_sha256"]),
                   "r2_ledger_sha256": str(lock_map["r2"]["ordered_ledger_sha256"]),
                   "created_at": created}
        connection.execute("INSERT INTO unblind_artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                           (run_id, payload["packet_path"], packet_sha, payload["form_path"],
                            form_sha, payload["r1_ledger_sha256"], payload["r2_ledger_sha256"],
                            created, _record(payload)))
        wp25._bind_artifact_values(connection, root, run_id, "unblind_packet", packet,
                                   "adjudication_prepare", len(packet_raw), packet_sha)
        wp25._bind_artifact_values(connection, root, run_id, "adjudication_blank_form", form_path,
                                   "adjudication_prepare", len(form_raw), form_sha)
        connection.execute("UPDATE runs SET status='unblinded',unblind_status='complete',adjudication_status='ready' WHERE run_id=?",
                           (run_id,)); _refresh(connection, run_id)
        wp25._insert_event(connection, run_id, "reviews_locked", "unblinded",
                           {"rows": TOTAL_COUNT, "gold_revealed": False})
        connection.commit()
        wp25._atomic_bytes(packet, packet_raw)
        wp25._atomic_bytes(form_path, form_raw)
        return packet, form_path
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


unblind_reviews = prepare_adjudication


def _validate_adjudication_form(connection: sqlite3.Connection, root: Path,
                                run_id: str, form: Mapping[str, Any]
                                ) -> tuple[str, str, list[dict[str, Any]]]:
    if form.get("adjudication_version") != ADJUDICATION_VERSION or form.get("run_id") != run_id:
        raise IdentityInstrumentProbeError("adjudication form version/run mismatch")
    adjudicator = str(form.get("adjudicator", "")).strip()
    wp25._normalized_actor(adjudicator)
    adjudicated_at = str(form.get("adjudicated_at", "")).strip() or wp25.utc_now()
    raw_items = form.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != TOTAL_COUNT:
        raise IdentityInstrumentProbeError("completed adjudication requires 24 rows")
    expected = {str(row["opaque_probe_id"]): row for row in connection.execute(
        "SELECT * FROM probe_adjudication_items WHERE run_id=?", (run_id,))}
    if len(expected) != TOTAL_COUNT:
        raise IdentityInstrumentProbeError("adjudication-item ledger incomplete")
    seen: set[str] = set(); rows: list[dict[str, Any]] = []; contract_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise IdentityInstrumentProbeError("adjudication row must be an object")
        opaque = str(item.get("opaque_probe_id", "")); frozen = expected.get(opaque)
        if frozen is None or opaque in seen:
            raise IdentityInstrumentProbeError("unknown/duplicate adjudication opaque ID")
        seen.add(opaque)
        for key in ("display_order", "sheet_path", "sheet_sha256", "r1_label", "r2_label"):
            actual = int(item.get(key, -1)) if key == "display_order" else item.get(key)
            expected_value = int(frozen[key]) if key == "display_order" else frozen[key]
            if actual != expected_value:
                raise IdentityInstrumentProbeError("adjudication binding changed")
        sheet = _resolve_text(root, str(frozen["sheet_path"]))
        if not sheet.is_file() or wp25.sha256_file(sheet) != str(frozen["sheet_sha256"]):
            raise IdentityInstrumentProbeError("adjudication sheet missing/changed")
        label = str(item.get("adjudicator_label", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        unresolved = item.get("unresolved_disagreement")
        if label not in ALLOWED_LABELS or not rationale or not isinstance(unresolved, bool):
            raise IdentityInstrumentProbeError("adjudication row incomplete")
        agreement = str(frozen["r1_label"]) == str(frozen["r2_label"])
        if agreement and unresolved:
            raise IdentityInstrumentProbeError("agreeing reviewers cannot be unresolved")
        final_probe = "insufficient" if (not agreement and unresolved) else label
        contract_item = {key: (int(frozen[key]) if key == "display_order" else str(frozen[key]))
                         for key in ("display_order", "opaque_probe_id", "sheet_path", "sheet_sha256",
                                     "r1_label", "r2_label")}
        contract_items.append(contract_item)
        rows.append({**contract_item, "sample_id": str(frozen["sample_id"]),
                     "adjudicator_label": label, "final_probe_label": final_probe,
                     "unresolved_disagreement": unresolved, "rationale": rationale})
    if seen != set(expected):
        raise IdentityInstrumentProbeError("adjudication form omits frozen rows")
    contract_items.sort(key=lambda row: row["display_order"])
    contract = {"adjudication_version": ADJUDICATION_VERSION, "run_id": run_id,
                "items": contract_items}
    if form.get("form_contract_sha256") != canonical_sha256(contract):
        raise IdentityInstrumentProbeError("adjudication form contract changed")
    return adjudicator, adjudicated_at, sorted(rows, key=lambda row: row["display_order"])


def lock_adjudication(*, repo_root: Path, completed_form_path: Path,
                      locked_output_path: Path = DEFAULT_ADJUDICATION_LOCKED,
                      artifact_path: Path = DEFAULT_OUTPUT) -> Path:
    """Freeze the distinct third actor's ledger without reading N100."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    completed, locked = _path(root, completed_form_path), _path(root, locked_output_path)
    if not completed.is_file() or completed.resolve() == locked.resolve():
        raise IdentityInstrumentProbeError("completed/locked adjudication paths invalid")
    form = json.loads(completed.read_text(encoding="utf-8"))
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        run = _one_run(connection); run_id = str(run["run_id"])
        existing = connection.execute(
            "SELECT * FROM probe_adjudication_lock WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing is not None:
            stored = connection.execute(
                "SELECT payload_json FROM probe_adjudication_lock_payload WHERE run_id=?", (run_id,)
            ).fetchone()
            if stored is None:
                raise IdentityInstrumentProbeError("adjudication lock recovery payload missing")
            payload = json.loads(str(stored[0])); raw = wp25._json_bytes(payload)
            if (_resolve_text(root, str(existing["completed_form_path"])).resolve() != completed.resolve()
                    or _resolve_text(root, str(existing["locked_output_path"])).resolve() != locked.resolve()
                    or wp25.sha256_file(completed) != str(existing["completed_form_sha256"])
                    or hashlib.sha256(raw).hexdigest() != str(existing["locked_output_sha256"])):
                raise IdentityInstrumentProbeError("adjudication lock recovery mismatch")
            wp25._atomic_bytes(locked, raw, allow_identical=True)
            return locked
        if locked.exists():
            raise IdentityInstrumentProbeError(
                "refusing to clobber adjudication locked ledger")
        if str(run["status"]) != "unblinded":
            raise IdentityInstrumentProbeError("adjudication lock requires prepared packet")
        blank = connection.execute(
            "SELECT form_path,form_sha256 FROM unblind_artifacts WHERE run_id=?", (run_id,)
        ).fetchone()
        if blank is None:
            raise IdentityInstrumentProbeError("blank adjudication form is unbound")
        blank_path = _resolve_text(root, str(blank["form_path"]))
        if (blank_path.resolve() == completed.resolve() or not blank_path.is_file()
                or wp25.sha256_file(blank_path) == wp25.sha256_file(completed)):
            raise IdentityInstrumentProbeError("completed form must differ from blank form")
        adjudicator, adjudicated_at, rows = _validate_adjudication_form(
            connection, root, run_id, form)
        reviewer_names = [str(row[0]) for row in connection.execute(
            "SELECT reviewer FROM review_locks WHERE run_id=?", (run_id,))]
        if len(reviewer_names) != 2 or any(
                wp25._normalized_actor(name) == wp25._normalized_actor(adjudicator)
                for name in reviewer_names):
            raise IdentityInstrumentProbeError("adjudicator must be distinct from both reviewers")
        ledger: list[dict[str, Any]] = []
        for row in rows:
            payload = {"opaque_probe_id": row["opaque_probe_id"],
                       "sample_id": row["sample_id"], "r1_label": row["r1_label"],
                       "r2_label": row["r2_label"],
                       "adjudicator_label": row["adjudicator_label"],
                       "final_probe_label": row["final_probe_label"],
                       "unresolved_disagreement": int(row["unresolved_disagreement"]),
                       "rationale": row["rationale"], "adjudicator": adjudicator,
                       "adjudicated_at": adjudicated_at}
            connection.execute(
                "INSERT INTO probe_adjudications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, payload["opaque_probe_id"], payload["sample_id"],
                 payload["r1_label"], payload["r2_label"], payload["adjudicator_label"],
                 payload["final_probe_label"], payload["unresolved_disagreement"],
                 payload["rationale"], adjudicator, adjudicated_at, _record(payload)))
            ledger.append({key: value for key, value in payload.items() if key != "sample_id"})
        ordered_sha = canonical_sha256(ledger)
        locked_payload = {"adjudication_version": ADJUDICATION_VERSION, "run_id": run_id,
                          "adjudicator": adjudicator, "adjudicated_at": adjudicated_at,
                          "row_count": TOTAL_COUNT, "ordered_ledger_sha256": ordered_sha,
                          "items": ledger}
        _assert_blind_payload(locked_payload, "adjudication lock")
        raw = wp25._json_bytes(locked_payload); digest = hashlib.sha256(raw).hexdigest()
        locked_at = wp25.utc_now()
        wp25._bind_artifact(connection, root, run_id, "adjudication_blank_form", blank_path,
                            "adjudication_prepare")
        wp25._register_artifact_path(connection, root, run_id, "adjudication_completed_form",
                                     completed, "adjudication_lock")
        wp25._bind_artifact(connection, root, run_id, "adjudication_completed_form", completed,
                            "adjudication_lock")
        wp25._register_artifact_path(connection, root, run_id, "adjudication_locked", locked,
                                     "adjudication_lock")
        connection.execute("INSERT INTO probe_adjudication_lock_payload VALUES(?,?,?)",
                           (run_id, canonical_json(locked_payload),
                            _record({"payload_json": canonical_json(locked_payload)})))
        lock_payload = {"adjudicator": adjudicator, "row_count": TOTAL_COUNT,
                        "completed_form_path": _path_text(root, completed),
                        "completed_form_sha256": wp25.sha256_file(completed),
                        "locked_output_path": _path_text(root, locked),
                        "locked_output_sha256": digest,
                        "ordered_ledger_sha256": ordered_sha, "locked_at": locked_at}
        connection.execute("INSERT INTO probe_adjudication_lock VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (run_id, adjudicator, TOTAL_COUNT,
                            lock_payload["completed_form_path"], lock_payload["completed_form_sha256"],
                            lock_payload["locked_output_path"], digest, ordered_sha, locked_at,
                            _record(lock_payload)))
        wp25._bind_artifact_values(connection, root, run_id, "adjudication_locked", locked,
                                   "adjudication_lock", len(raw), digest)
        connection.execute("UPDATE runs SET status='adjudication_locked',adjudication_status='locked' WHERE run_id=?",
                           (run_id,)); _refresh(connection, run_id)
        wp25._insert_event(connection, run_id, "unblinded", "adjudication_locked",
                           {"rows": TOTAL_COUNT, "ordered_ledger_sha256": ordered_sha,
                            "gold_revealed": False})
        connection.commit()
        wp25._atomic_bytes(locked, raw)
        return locked
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def _score_gate(labels_by_group: Mapping[str, Sequence[str]],
                thresholds: Mapping[str, int] = GATE_THRESHOLDS) -> dict[str, Any]:
    if {key: len(labels_by_group.get(key, ())) for key in GROUP_QUOTAS} != dict(GROUP_QUOTAS):
        raise IdentityInstrumentProbeError("gate input does not contain the frozen group sizes")
    p1_non_same = sum(label != "same" for label in labels_by_group["P1"])
    p23_non_same = sum(label != "same" for group in ("P2", "P3")
                       for label in labels_by_group[group])
    d_same = sum(label == "same" for label in labels_by_group["D"])
    predicates = {
        "p1_non_same": {"actual": p1_non_same,
                        "threshold": int(thresholds["p1_min_non_same"]),
                        "passed": p1_non_same >= int(thresholds["p1_min_non_same"])},
        "p23_non_same": {"actual": p23_non_same,
                         "threshold": int(thresholds["p23_min_non_same"]),
                         "passed": p23_non_same >= int(thresholds["p23_min_non_same"])},
        "d_same": {"actual": d_same, "threshold": int(thresholds["d_min_same"]),
                   "passed": d_same >= int(thresholds["d_min_same"])},
    }
    return {"status": "PASS" if all(value["passed"] for value in predicates.values()) else "FAIL",
            "predicates": predicates}


def _verify_pinned_inputs(connection: sqlite3.Connection, root: Path, run_id: str) -> None:
    for row in connection.execute("SELECT * FROM input_pins WHERE run_id=?", (run_id,)):
        path = _resolve_text(root, str(row["path"]))
        if (not path.is_file() or path.stat().st_size != int(row["size_bytes"])
                or wp25.sha256_file(path) != str(row["sha256_before"])
                or str(row["sha256_after"]) != str(row["sha256_before"])):
            raise IdentityInstrumentProbeError(f"pinned input changed: {row['role']}")


def _pin_path(connection: sqlite3.Connection, root: Path, run_id: str, role: str) -> Path:
    row = connection.execute(
        "SELECT path FROM input_pins WHERE run_id=? AND role=?", (run_id, role)
    ).fetchone()
    if row is None:
        raise IdentityInstrumentProbeError(f"missing input pin: {role}")
    return _resolve_text(root, str(row[0]))


def _reveal_controls(connection: sqlite3.Connection, root: Path,
                     run_id: str) -> list[dict[str, Any]]:
    """Open pinned gold and re-derive group membership after adjudication lock."""
    v4_path = _pin_path(connection, root, run_id, "v4_candidates_final")
    n100_path = _pin_path(connection, root, run_id, "wp23_n100")
    band_path = _pin_path(connection, root, run_id, "wp25_sample")
    v4, n100, band = _open_ro(v4_path), _open_ro(n100_path), _open_ro(band_path)
    try:
        selected, _ = _select_controls(n100, v4, band)
        expected = {str(row["candidate_id"]): row for row in selected}
        if len(expected) != TOTAL_COUNT:
            raise IdentityInstrumentProbeError("replayed controls are not unique")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in connection.execute(
            "SELECT p.sample_rank,p.sample_id,p.candidate_id AS opaque_control_id,"
            "s.candidate_json,c.gold_commitment_sha256,c.source_record_commitment_sha256,"
            "a.r1_label,a.r2_label,a.final_probe_label,a.unresolved_disagreement,a.rationale "
            "FROM sample_pairs p JOIN candidate_snapshots s USING(run_id,sample_id) "
            "JOIN control_commitments c USING(run_id,sample_id) "
            "JOIN probe_adjudications a USING(run_id,sample_id) "
            "WHERE p.run_id=? ORDER BY p.sample_rank", (run_id,)):
            candidate = json.loads(str(row["candidate_json"]))
            matches = v4.execute(
                "SELECT candidate_id FROM candidate_pairs WHERE left_source=? "
                "AND left_source_building_id=? AND right_source=? "
                "AND right_source_building_id=?",
                (candidate["left_source"], candidate["left_source_building_id"],
                 candidate["right_source"], candidate["right_source_building_id"]),
            ).fetchall()
            if len(matches) != 1:
                raise IdentityInstrumentProbeError("scrubbed source tuple is not a unique v4 join")
            candidate_id = str(matches[0][0]); control = expected.get(candidate_id)
            if control is None or candidate_id in seen:
                raise IdentityInstrumentProbeError("frozen opaque controls do not replay exactly")
            seen.add(candidate_id)
            source_commitment = canonical_sha256({
                "namespace": "wp26-source-record-commitment-v1",
                "candidate_id": candidate_id,
                "source_adjudication_record_sha256":
                    str(control["adjudication_record_sha256"]),
            })
            if str(row["source_record_commitment_sha256"]) != source_commitment:
                raise IdentityInstrumentProbeError("N100 source-record commitment changed")
            commitment = _commitment(
                candidate_id, str(control["group"]), str(control["sample_id"]),
                str(control["final_gold_label"]), str(control["adjudication_record_sha256"]))
            if commitment != str(row["gold_commitment_sha256"]):
                raise IdentityInstrumentProbeError("control commitment mismatch")
            result.append({
                "sample_rank": int(row["sample_rank"]), "sample_id": str(row["sample_id"]),
                "opaque_control_id": str(row["opaque_control_id"]),
                "candidate_id": candidate_id, "group": str(control["group"]),
                "n100_sample_id": str(control["sample_id"]),
                "n100_gold_label": str(control["final_gold_label"]),
                "final_probe_label": str(row["final_probe_label"]),
                "r1_label": str(row["r1_label"]), "r2_label": str(row["r2_label"]),
                "unresolved_disagreement": bool(row["unresolved_disagreement"]),
                "adjudicator_rationale": str(row["rationale"]),
                "commitment_sha256": commitment,
            })
        if seen != set(expected) or len(result) != TOTAL_COUNT:
            raise IdentityInstrumentProbeError("reveal replay omitted controls")
        return result
    finally:
        v4.close(); n100.close(); band.close()


def _comparison_rows(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [{"comparison_key": str(row["comparison_key"]),
             "n100": json.loads(str(row["n100_value_json"])),
             "wp25": json.loads(str(row["wp25_value_json"])),
             "finding": str(row["finding"])}
            for row in connection.execute(
                "SELECT * FROM form_comparison WHERE run_id=? ORDER BY comparison_key", (run_id,))]


def _quota_rows(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [{"observation_no": int(row["observation_no"]), "phase": str(row["phase"]),
             "observed_at": str(row["observed_at"]),
             "remaining_percent": float(row["gate_basis_remaining_percent"]),
             "start_allowed": bool(row["start_allowed"]),
             "stop_required": bool(row["stop_required"]),
             "limitation": str(row["limitation"])}
            for row in connection.execute(
                "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no", (run_id,))]


def _report_payload(connection: sqlite3.Connection, run_id: str, status: str,
                    completed_at: str) -> dict[str, Any]:
    reveals = [dict(row) for row in connection.execute(
        "SELECT * FROM probe_reveals WHERE run_id=? ORDER BY sample_id", (run_id,))]
    labels: dict[str, list[str]] = {group: [] for group in GROUP_QUOTAS}
    for row in reveals:
        labels[str(row["group_code"])].append(str(row["final_probe_label"]))
    gate = _score_gate(labels)
    if gate["status"] != status:
        raise IdentityInstrumentProbeError("stored terminal status differs from gate replay")
    group_metrics = {}
    for group, values in labels.items():
        counts = Counter(values)
        group_metrics[group] = {"n": len(values), "labels": dict(sorted(counts.items())),
                                "same": counts["same"],
                                "non_same": len(values) - counts["same"]}
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    fetch_counts = dict(Counter(str(row[0]) for row in connection.execute(
        "SELECT status FROM fetch_assets WHERE run_id=?", (run_id,))))
    disagreements = int(connection.execute(
        "SELECT count(*) FROM probe_adjudications WHERE run_id=? AND r1_label<>r2_label", (run_id,)
    ).fetchone()[0])
    unresolved = int(connection.execute(
        "SELECT count(*) FROM probe_adjudications WHERE run_id=? AND unresolved_disagreement=1", (run_id,)
    ).fetchone()[0])
    gold_comparison = [{
        "sample_id": str(row["sample_id"]), "candidate_id": str(row["candidate_id"]),
        "group": str(row["group_code"]), "n100_sample_id": str(row["n100_sample_id"]),
        "n100_gold_label": str(row["n100_gold_label"]),
        "probe_label": str(row["final_probe_label"]),
        "commitment_sha256": str(row["commitment_sha256"]),
    } for row in reveals]
    return {
        "status": status, "run_id": run_id, "completed_at": completed_at,
        "measurement_only": True, "authorizes_tiering": False,
        "wp25_s1_s4_grades_valid": status == "PASS",
        "gate": gate, "group_metrics": group_metrics,
        "gold_comparison": gold_comparison,
        "step0_form_comparison": _comparison_rows(connection, run_id),
        "review": {"r1_rows": TOTAL_COUNT, "r2_rows": TOTAL_COUNT,
                   "adjudicated_rows": TOTAL_COUNT, "disagreements": disagreements,
                   "unresolved_disagreements": unresolved},
        "fetch": {"asset_occurrences": int(run["asset_occurrence_count"]),
                  "unique_assets": int(run["unique_asset_count"]),
                  "logical_http_fetches": int(run["logical_http_fetches"]),
                  "wire_http_requests": int(run["wire_http_requests"]),
                  "status_counts": fetch_counts, "redirects": 0, "retries": 0,
                  "hard_caps": {"asset_occurrences": 96,
                                "logical_http_fetches": 96,
                                "wire_http_requests": 96}},
        "quota_observations": _quota_rows(connection, run_id),
        "vision_requests": 0, "llm_requests": 0, "paid_api_requests": 0,
        "merge_executions": 0, "vnext_id_issuances": 0, "tier_promotions": 0,
        "neon_writes": 0, "r2_writes": 0,
    }


def _manifest_payload(connection: sqlite3.Connection, run_id: str, status: str,
                      completed_at: str, report_path: Path | str,
                      report_sha256: str, *,
                      legacy_windows_report_path: bool = False) -> dict[str, Any]:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    artifacts = {str(row["role"]): {
        "path": str(row["path"]), "size_bytes": int(row["size_bytes"]),
        "sha256": str(row["sha256"]), "stage": str(row["stage"]),
    } for row in connection.execute(
        "SELECT * FROM artifact_bindings WHERE run_id=? AND role<>'final_manifest' ORDER BY role",
        (run_id,))}
    pins = {str(row["role"]): {"path": str(row["path"]),
            "size_bytes": int(row["size_bytes"]), "sha256": str(row["sha256_before"])}
            for row in connection.execute("SELECT * FROM input_pins WHERE run_id=? ORDER BY role", (run_id,))}
    caches = [{key: row[key] for key in (
        "role", "path", "transform_version", "access_mode", "file_count_before",
        "total_bytes_before", "tree_sha256_before", "file_count_after",
        "total_bytes_after", "tree_sha256_after")}
        for row in connection.execute("SELECT * FROM cache_pins WHERE run_id=? ORDER BY role", (run_id,))]
    manifest_report_path = (
        str(PureWindowsPath(str(report_path)))
        if legacy_windows_report_path
        else Path(report_path).as_posix()
    )
    return {
        "status": status, "run_id": run_id, "completed_at": completed_at,
        "versions": {"runner": RUNNER_VERSION, "sample": SAMPLE_VERSION,
                     "review": REVIEW_VERSION, "adjudication": ADJUDICATION_VERSION,
                     "gate": GATE_VERSION},
        "sample_logical_sha256": str(run["sample_logical_sha256"]),
        "report": {"path": manifest_report_path, "sha256": report_sha256},
        "artifacts": artifacts, "input_pins": pins, "cache_pins": caches,
        "counts": {"sample": TOTAL_COUNT, "r1": TOTAL_COUNT, "r2": TOTAL_COUNT,
                   "adjudicated": TOTAL_COUNT, "post_freeze_replacements": 0},
        "network": {"logical": int(run["logical_http_fetches"]),
                    "wire": int(run["wire_http_requests"]),
                    "redirects": 0, "retries": 0},
        "vision_requests": 0, "llm_requests": 0, "paid_api_requests": 0,
        "merge_executions": 0, "vnext_id_issuances": 0, "tier_promotions": 0,
        "neon_writes": 0, "r2_writes": 0,
    }


def _file_binding(root: Path, path: Path, label: str) -> dict[str, Any]:
    absolute = _path(root, path)
    if not absolute.is_file():
        raise IdentityInstrumentProbeError(f"{label} is missing")
    return {
        "path": _path_text(root, absolute),
        "size_bytes": absolute.stat().st_size,
        "sha256": wp25.sha256_file(absolute),
    }


def _canonical_json_file(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityInstrumentProbeError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or wp25._json_bytes(payload) != raw:
        raise IdentityInstrumentProbeError(f"{label} bytes are not canonical")
    return payload, raw


def _actual_fetch_counts(connection: sqlite3.Connection,
                         run: sqlite3.Row) -> dict[str, int]:
    attempts = connection.execute(
        "SELECT asset_key,attempt_no,request_count,redirect_chain_json "
        "FROM fetch_attempts WHERE run_id=? ORDER BY asset_key,attempt_no",
        (str(run["run_id"]),),
    ).fetchall()
    asset_keys: set[str] = set()
    wire = 0
    redirects = 0
    for row in attempts:
        asset_keys.add(str(row["asset_key"]))
        wire += int(row["request_count"])
        try:
            chain = json.loads(str(row["redirect_chain_json"]))
        except json.JSONDecodeError as exc:
            raise IdentityInstrumentProbeError(
                "fetch attempt redirect chain is invalid") from exc
        if not isinstance(chain, list):
            raise IdentityInstrumentProbeError(
                "fetch attempt redirect chain is not a list")
        redirects += len(chain)
    retries = len(attempts) - len(asset_keys)
    counts = {
        "asset_occurrences": int(run["asset_occurrence_count"]),
        "logical_http_fetches": len(asset_keys),
        "wire_http_requests": wire,
        "redirects": redirects,
        "retries": retries,
    }
    if counts["logical_http_fetches"] != int(run["logical_http_fetches"]):
        raise IdentityInstrumentProbeError("DB logical HTTP count replay failed")
    if counts["wire_http_requests"] != int(run["wire_http_requests"]):
        raise IdentityInstrumentProbeError("DB wire HTTP count replay failed")
    return counts


def create_fetch_cap_correction(
        *, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
        fetch_manifest_path: Path = DEFAULT_FETCH_MANIFEST,
        report_path: Path = DEFAULT_REPORT,
        manifest_path: Path = DEFAULT_MANIFEST,
        output_path: Path = DEFAULT_FETCH_CAP_CORRECTION,
        current_producer_path: Path = DEFAULT_WP25_PRODUCER.parent
        / "vnext_identity_instrument_probe.py",
        ) -> FetchCapCorrectionResult:
    """Publish a no-clobber overlay for the terminal run's inherited cap typo."""
    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    fetch_manifest = _path(root, fetch_manifest_path)
    report = _path(root, report_path)
    manifest = _path(root, manifest_path)
    output = _path(root, output_path)
    current_producer = _path(root, current_producer_path)
    protected = {path.resolve() for path in (
        artifact, fetch_manifest, report, manifest, current_producer)}
    if output.resolve() in protected:
        raise IdentityInstrumentProbeError(
            "correction output must be distinct from every bound artifact")
    wp25._assert_no_sidecars(artifact)
    source_bindings = {
        "database": _file_binding(root, artifact, "terminal database"),
        "fetch_manifest": _file_binding(root, fetch_manifest, "fetch manifest"),
        "report": _file_binding(root, report, "terminal report"),
        "manifest": _file_binding(root, manifest, "terminal manifest"),
        "current_producer": _file_binding(
            root, current_producer, "current producer"),
    }
    fetch_payload, _ = _canonical_json_file(fetch_manifest, "fetch manifest")
    recorded_caps = fetch_payload.get("hard_caps")
    if recorded_caps == dict(CORRECTED_FETCH_HARD_CAPS):
        raise IdentityInstrumentProbeError(
            "fetch manifest already has the corrected hard caps")
    if recorded_caps != dict(LEGACY_FETCH_HARD_CAPS):
        raise IdentityInstrumentProbeError(
            "fetch manifest does not contain the frozen 884 literal")

    connection = wp25._open_immutable(artifact)
    try:
        run = _one_run(connection); run_id = str(run["run_id"])
        terminal = connection.execute(
            "SELECT * FROM probe_terminal WHERE run_id=?", (run_id,)
        ).fetchone()
        if terminal is None or str(run["status"]) not in {"pass", "fail"}:
            raise IdentityInstrumentProbeError(
                "fetch-cap correction requires a terminal probe")
        if str(terminal["status"]).casefold() != str(run["status"]).casefold():
            raise IdentityInstrumentProbeError("terminal status binding changed")
        artifact_row = connection.execute(
            "SELECT path,size_bytes,sha256 FROM artifact_bindings "
            "WHERE run_id=? AND role='fetch_manifest'", (run_id,),
        ).fetchone()
        if artifact_row is None:
            raise IdentityInstrumentProbeError(
                "terminal fetch manifest binding is missing")
        if (_resolve_text(root, str(artifact_row["path"])).resolve()
                != fetch_manifest.resolve()
                or int(artifact_row["size_bytes"])
                != int(source_bindings["fetch_manifest"]["size_bytes"])
                or str(artifact_row["sha256"])
                != str(source_bindings["fetch_manifest"]["sha256"])):
            raise IdentityInstrumentProbeError(
                "terminal fetch manifest binding changed")
        if (_resolve_text(root, str(terminal["report_path"])).resolve()
                != report.resolve()
                or str(terminal["report_sha256"])
                != str(source_bindings["report"]["sha256"])
                or _resolve_text(root, str(terminal["manifest_path"])).resolve()
                != manifest.resolve()
                or str(terminal["manifest_sha256"])
                != str(source_bindings["manifest"]["sha256"])):
            raise IdentityInstrumentProbeError(
                "terminal report/manifest binding changed")
        actual_counts = _actual_fetch_counts(connection, run)
    finally:
        connection.close()

    manifest_counts = {
        "asset_occurrences": fetch_payload.get("asset_occurrence_count"),
        "logical_http_fetches": fetch_payload.get("logical_http_fetches"),
        "wire_http_requests": fetch_payload.get("wire_http_requests"),
    }
    if (str(fetch_payload.get("run_id")) != run_id
            or any(manifest_counts[key] != actual_counts[key]
                   for key in manifest_counts)):
        raise IdentityInstrumentProbeError(
            "fetch manifest counts do not replay from the terminal DB")
    within_hard_caps = all(
        actual_counts[key] <= int(CORRECTED_FETCH_HARD_CAPS[key])
        for key in CORRECTED_FETCH_HARD_CAPS
    )
    if not within_hard_caps:
        raise IdentityInstrumentProbeError(
            "terminal fetch counts exceed the corrected contract")

    corrected_projection = dict(fetch_payload)
    corrected_projection["hard_caps"] = dict(CORRECTED_FETCH_HARD_CAPS)
    projection_sha = canonical_sha256(corrected_projection)
    producer_path = _path_text(root, current_producer)
    payload = {
        "status": "CORRECTION",
        "correction_version": FETCH_CAP_CORRECTION_VERSION,
        "run_id": run_id,
        "scope": "fetch_manifest.hard_caps",
        "cause": {
            "code": "reused_wp25_fetch_manifest_literal",
            "detail": FETCH_CAP_CORRECTION_CAUSE,
        },
        "original_fetch_manifest": {
            **source_bindings["fetch_manifest"],
            "recorded_hard_caps": dict(LEGACY_FETCH_HARD_CAPS),
        },
        "corrected_contract": {
            "hard_caps": dict(CORRECTED_FETCH_HARD_CAPS),
            "actual_counts": actual_counts,
            "within_hard_caps": within_hard_caps,
        },
        "corrected_projection": {
            "canonicalization": "canonical-json-v1",
            "operation": "replace-hard_caps-only",
            "sha256": projection_sha,
        },
        "terminal_bindings": {
            "database": source_bindings["database"],
            "report": source_bindings["report"],
            "manifest": source_bindings["manifest"],
        },
        "producer_bindings": {
            "runtime": {
                "path": producer_path,
                "size_bytes": RUNTIME_PRODUCER_SPEC[0],
                "sha256": RUNTIME_PRODUCER_SPEC[1],
            },
            "current": source_bindings["current_producer"],
        },
    }
    raw = wp25._json_bytes(payload)
    wp25._atomic_bytes(output, raw, allow_identical=True)
    for role, path in (
            ("database", artifact), ("fetch_manifest", fetch_manifest),
            ("report", report), ("manifest", manifest),
            ("current_producer", current_producer)):
        if _file_binding(root, path, role) != source_bindings[role]:
            raise IdentityInstrumentProbeError(
                f"bound source changed while publishing correction: {role}")
    return FetchCapCorrectionResult(
        output, run_id, projection_sha, hashlib.sha256(raw).hexdigest())


def _bind_existing_artifacts(connection: sqlite3.Connection, root: Path,
                             run_id: str) -> None:
    required = {"sample_manifest", "fetch_manifest", "fetch_report",
                "r1_blank_form", "r2_blank_form", "r1_completed_form",
                "r2_completed_form", "r1_locked", "r2_locked", "unblind_packet",
                "adjudication_blank_form", "adjudication_completed_form",
                "adjudication_locked"}
    for row in connection.execute(
            "SELECT role,path,stage FROM artifact_paths WHERE run_id=? ORDER BY role", (run_id,)):
        role, path = str(row["role"]), _resolve_text(root, str(row["path"]))
        if role == "sheets_root":
            continue
        if path.is_file():
            wp25._bind_artifact(connection, root, run_id, role, path, str(row["stage"]))
    for row in connection.execute(
            "SELECT role,path,size_bytes,sha256 FROM artifact_bindings "
            "WHERE run_id=? ORDER BY role", (run_id,)):
        path = _resolve_text(root, str(row["path"]))
        if (not path.is_file() or path.stat().st_size != int(row["size_bytes"])
                or wp25.sha256_file(path) != str(row["sha256"])):
            raise IdentityInstrumentProbeError(
                f"bound artifact is missing or changed: {row['role']}")
    bound = {str(row[0]) for row in connection.execute(
        "SELECT role FROM artifact_bindings WHERE run_id=?", (run_id,))}
    missing = required - bound
    if missing:
        raise IdentityInstrumentProbeError(f"required artifacts are unbound: {sorted(missing)}")


def reveal_score(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                 report_path: Path = DEFAULT_REPORT,
                 manifest_path: Path = DEFAULT_MANIFEST) -> RevealResult:
    """Open N100 gold only after the immutable adjudication lock, then score."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    report, manifest = _path(root, report_path), _path(root, manifest_path)
    if report.resolve() == manifest.resolve():
        raise IdentityInstrumentProbeError("report and manifest paths must be distinct")
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        run = _one_run(connection); run_id, run_status = str(run["run_id"]), str(run["status"])
        terminal = connection.execute("SELECT * FROM probe_terminal WHERE run_id=?", (run_id,)).fetchone()
        if terminal is not None:
            if run_status not in {"pass", "fail"}:
                raise IdentityInstrumentProbeError("terminal row/run status mismatch")
            if (_resolve_text(root, str(terminal["report_path"])).resolve() != report.resolve()
                    or _resolve_text(root, str(terminal["manifest_path"])).resolve() != manifest.resolve()):
                raise IdentityInstrumentProbeError("terminal recovery path changed")
            report_payload = _report_payload(connection, run_id, run_status.upper(),
                                             str(terminal["completed_at"]))
            report_raw = wp25._json_bytes(report_payload)
            report_sha = hashlib.sha256(report_raw).hexdigest()
            if report_sha != str(terminal["report_sha256"]):
                raise IdentityInstrumentProbeError("terminal recovery bytes changed")
            manifest_payload = _manifest_payload(
                connection, run_id, run_status.upper(), str(terminal["completed_at"]),
                str(terminal["report_path"]), report_sha)
            manifest_raw = wp25._json_bytes(manifest_payload)
            if hashlib.sha256(manifest_raw).hexdigest() != str(terminal["manifest_sha256"]):
                # Runs terminalized by the original Windows producer serialized
                # Path(relative) with backslashes.  Preserve those immutable
                # bytes only when their SHA is the one frozen in the terminal row.
                manifest_payload = _manifest_payload(
                    connection, run_id, run_status.upper(),
                    str(terminal["completed_at"]), str(terminal["report_path"]),
                    report_sha, legacy_windows_report_path=True)
                manifest_raw = wp25._json_bytes(manifest_payload)
                if hashlib.sha256(manifest_raw).hexdigest() != str(terminal["manifest_sha256"]):
                    raise IdentityInstrumentProbeError("terminal recovery bytes changed")
            wp25._atomic_bytes(report, report_raw, allow_identical=True)
            wp25._atomic_bytes(manifest, manifest_raw, allow_identical=True)
            gate = report_payload["gate"]["predicates"]
            return RevealResult(artifact, report, manifest, run_id, run_status.upper(),
                                int(gate["p1_non_same"]["actual"]),
                                int(gate["p23_non_same"]["actual"]),
                                int(gate["d_same"]["actual"]))
        if report.exists() or manifest.exists():
            raise IdentityInstrumentProbeError(
                "refusing to clobber terminal report/manifest")
        if run_status != "adjudication_locked":
            raise IdentityInstrumentProbeError("reveal requires immutable adjudication lock")
        _require_probe_quota(connection, run_id, "pre_reveal_score")
        _require_gate(connection, run_id); _verify_pinned_inputs(connection, root, run_id)
        if _logical_sha(connection, run_id) != str(run["sample_logical_sha256"]):
            raise IdentityInstrumentProbeError("sample changed before reveal")
        for item in connection.execute(
                "SELECT sheet_path,sheet_sha256 FROM review_items WHERE run_id=?",
                (run_id,)):
            sheet = _resolve_text(root, str(item["sheet_path"]))
            if not sheet.is_file() or wp25.sha256_file(sheet) != str(item["sheet_sha256"]):
                raise IdentityInstrumentProbeError("review sheet changed before reveal")
        for pin in connection.execute(
                "SELECT * FROM cache_pins WHERE run_id=? ORDER BY role", (run_id,)):
            if any(pin[key] is None for key in (
                    "file_count_after", "total_bytes_after", "tree_sha256_after")):
                raise IdentityInstrumentProbeError(
                    f"review cache final pin is incomplete: {pin['role']}")
            expected = (int(pin["file_count_after"]), int(pin["total_bytes_after"]),
                        str(pin["tree_sha256_after"]))
            if wp25.cache_tree(_resolve_text(root, str(pin["path"]))) != expected:
                raise IdentityInstrumentProbeError(
                    f"review cache changed before reveal: {pin['role']}")
        lock = connection.execute(
            "SELECT * FROM probe_adjudication_lock WHERE run_id=?", (run_id,)
        ).fetchone()
        if lock is None or int(lock["row_count"]) != TOTAL_COUNT:
            raise IdentityInstrumentProbeError("adjudication lock row missing")
        locked_path = _resolve_text(root, str(lock["locked_output_path"]))
        if not locked_path.is_file() or wp25.sha256_file(locked_path) != str(lock["locked_output_sha256"]):
            raise IdentityInstrumentProbeError("adjudication lock SHA mismatch")
        reveals = _reveal_controls(connection, root, run_id)
        labels: dict[str, list[str]] = {group: [] for group in GROUP_QUOTAS}
        for item in reveals:
            labels[item["group"]].append(item["final_probe_label"])
        gate = _score_gate(labels); status = str(gate["status"])
        for item in reveals:
            payload = {key: item[key] for key in (
                "sample_id", "candidate_id", "group", "n100_sample_id",
                "n100_gold_label", "final_probe_label", "commitment_sha256")}
            connection.execute("INSERT INTO probe_reveals VALUES(?,?,?,?,?,?,?,?,?)",
                               (run_id, item["sample_id"], item["candidate_id"], item["group"],
                                item["n100_sample_id"], item["n100_gold_label"],
                                item["final_probe_label"], item["commitment_sha256"],
                                _record(payload)))
        score_rows = (
            ("P1", 3, gate["predicates"]["p1_non_same"]["actual"], 2),
            ("P23", 6, gate["predicates"]["p23_non_same"]["actual"], 3),
            ("D", 15, 15 - gate["predicates"]["d_same"]["actual"], 13),
        )
        for group, n, non_same, threshold in score_rows:
            same = n - int(non_same)
            passed = (int(non_same) >= threshold) if group != "D" else (same >= threshold)
            payload = {"group_code": group, "sample_n": n, "probe_same": same,
                       "probe_non_same": int(non_same), "threshold": threshold,
                       "passed": int(passed)}
            connection.execute("INSERT INTO probe_scores VALUES(?,?,?,?,?,?,?,?)",
                               (run_id, group, n, same, int(non_same), threshold,
                                int(passed), _record(payload)))
        completed = wp25.utc_now()
        _bind_existing_artifacts(connection, root, run_id)
        wp25._register_artifact_path(connection, root, run_id, "final_report", report, "reveal_score")
        wp25._register_artifact_path(connection, root, run_id, "final_manifest", manifest, "reveal_score")
        report_payload = _report_payload(connection, run_id, status, completed)
        report_raw = wp25._json_bytes(report_payload); report_sha = hashlib.sha256(report_raw).hexdigest()
        wp25._bind_artifact_values(connection, root, run_id, "final_report", report,
                                   "reveal_score", len(report_raw), report_sha)
        manifest_payload = _manifest_payload(connection, run_id, status, completed,
                                             Path(_path_text(root, report)), report_sha)
        manifest_raw = wp25._json_bytes(manifest_payload); manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        wp25._bind_artifact_values(connection, root, run_id, "final_manifest", manifest,
                                   "reveal_score", len(manifest_raw), manifest_sha)
        terminal_payload = {"status": status, "report_path": _path_text(root, report),
                            "report_sha256": report_sha, "manifest_path": _path_text(root, manifest),
                            "manifest_sha256": manifest_sha, "completed_at": completed}
        connection.execute("INSERT INTO probe_terminal VALUES(?,?,?,?,?,?,?,?)",
                           (run_id, status, terminal_payload["report_path"], report_sha,
                            terminal_payload["manifest_path"], manifest_sha, completed,
                            _record(terminal_payload)))
        connection.execute("UPDATE runs SET status=?,gate_status=?,completed_at=? WHERE run_id=?",
                           (status.casefold(), status.casefold(), completed, run_id)); _refresh(connection, run_id)
        wp25._insert_event(connection, run_id, "adjudication_locked", status.casefold(),
                           {"gold_revealed": True, "gate": gate})
        connection.commit()
        wp25._atomic_bytes(report, report_raw)
        wp25._atomic_bytes(manifest, manifest_raw)
        predicates = gate["predicates"]
        return RevealResult(artifact, report, manifest, run_id, status,
                            int(predicates["p1_non_same"]["actual"]),
                            int(predicates["p23_non_same"]["actual"]),
                            int(predicates["d_same"]["actual"]))
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()
