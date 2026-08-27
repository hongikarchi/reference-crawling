"""WP2.8-R packet-repair adapter for the immutable v2.1 instrument.

Only the packet input and its provenance are replaced.  Observation values,
instructions, derivation, actor ledgers, blind ordering, and the predeclared
WP2.8 gate are imported from :mod:`vnext_identity_instrument_v21_probe`.
This module deliberately lives beside that frozen implementation instead of
modifying it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical import vnext_identity_instrument_v21_probe as v21


REPAIR_VERSION = "vnext-identity-instrument-v21-packet-repair-v2"
PROJECTION_VERSION = "vnext-identity-instrument-v21-repaired-source-v1"
BASE_GATE_SHA256 = "a5adf46a63c5ce141f528be269a041f1ae9c492d4d6007afc42e6f8eed9ac18b"
EXPECTED_REPAIR_ORDER_SIZE = 3_532
EXPECTED_REPAIR_ORDER_SHA256 = (
    "6411c7498b2c9ac2840723340b29bcdce26d38adaeb322e7c130f910c11647bf"
)
EXPECTED_EXCLUDED_SAMPLE = "probe_440b72d0af93f2c5b0fbd38e"
READABLE_ASSET_STATUSES = frozenset(
    {"cache_hit", "exact", "delivery_changed", "source_changed"}
)

DEFAULT_REPAIRED_SOURCE_DB = Path(
    "data/enrichment/vnext_identity_instrument_probe_v2.db"
)
DEFAULT_REPAIRED_SAMPLE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v2_sample_manifest.json"
)
DEFAULT_REPAIRED_FETCH_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v2_fetch_manifest.json"
)
DEFAULT_REPAIRED_FETCH_REPORT = Path(
    "data/reports/vnext_identity_instrument_probe_v2_fetch_report.json"
)
DEFAULT_ORIGINAL_WP26_DB = Path(
    "data/enrichment/vnext_identity_instrument_probe_v1.db"
)
DEFAULT_ORIGINAL_WP27_DB = Path(
    "data/enrichment/vnext_identity_instrument_v2_probe_v1.db"
)
DEFAULT_ORIGINAL_WP28_DB = Path(
    "data/enrichment/vnext_identity_instrument_v21_probe_v1.db"
)
DEFAULT_PROJECTION_DB = Path(
    "data/enrichment/vnext_identity_instrument_v21_probe_v2_source.db"
)
DEFAULT_PROJECTION_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_source_manifest.json"
)
DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_instrument_v21_probe_v2.db")
DEFAULT_SAMPLE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_sample_manifest.json"
)
DEFAULT_FETCH_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_fetch_manifest.json"
)
DEFAULT_FETCH_REPORT = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_fetch_report.json"
)
DEFAULT_R1_FORM = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_reviewer_r1_draft.json"
)
DEFAULT_R2_FORM = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_reviewer_r2_draft.json"
)
DEFAULT_R1_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_reviewer_r1_locked.json"
)
DEFAULT_R2_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_reviewer_r2_locked.json"
)
DEFAULT_ADJUDICATION_PACKET = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_adjudication_packet.json"
)
DEFAULT_ADJUDICATION_FORM = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_adjudication_draft.json"
)
DEFAULT_ADJUDICATION_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_adjudication_locked.json"
)
DEFAULT_CORE_REPORT = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_core_report.json"
)
DEFAULT_CORE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_core_manifest.json"
)
DEFAULT_REPORT = Path("data/reports/vnext_identity_instrument_v21_probe_v2_report.json")
DEFAULT_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_manifest.json"
)
DEFAULT_REPAIR_ORDER = Path(
    ".claude/ops/jobs/20260827_vnext_wp28r_packet_repair_order.md"
)


FROZEN_INSTRUMENT_FILES: Mapping[str, tuple[Path, str]] = {
    "wp28_order": (
        Path(".claude/ops/jobs/20260827_vnext_wp28_instrument_v21_order.md"),
        "1c57a6f2dca384c1ea84db671c8fbb0f150adf76e37707cee5871a1de3267ff9",
    ),
    "v21_protocol": (
        Path("docs/vnext/IDENTITY_INSTRUMENT_V21_PROTOCOL.md"),
        "a459ebdde66389dc15d983cd97597ba57929a30c71ac02435da987cb60cbdf44",
    ),
    "v21_producer": (
        Path("canonical/vnext_identity_instrument_v21_probe.py"),
        "94241af175c40b5dab903ba04b38314f74cd3a64e6c07fe5095b06a64e984939",
    ),
    "v21_validator": (
        Path("canonical/vnext_identity_instrument_v21_probe_validator.py"),
        "b6b62b4bb1c441cd3ebe533097ae0aaed9ad0ab42174cfec55c0cacf2f4e1aec",
    ),
    "v21_runner": (
        Path("tools/run_vnext_identity_instrument_v21_probe.py"),
        "a8f898b85eb8afffc3d0c6c44ae5b41f7787ff38e5e7d20658a31d5a030c7e43",
    ),
    "v21_validator_cli": (
        Path("tools/validate_vnext_identity_instrument_v21_probe.py"),
        "3749db5c6da822fdfe2112f5ea6528f65834da1355680e55bcbfdc035a5b25ef",
    ),
    "v21_producer_tests": (
        Path("tests/test_vnext_identity_instrument_v21_probe.py"),
        "721130894dd841fe6979a29f4e8bfbd5cff2ae5abdf679899f386acd930eff7a",
    ),
    "v21_validator_tests": (
        Path("tests/test_vnext_identity_instrument_v21_probe_validator.py"),
        "c0311d9104402a6bdb27451f67dcd42a47098b2df74dabc44824b711bd835481",
    ),
}

FROZEN_RUNTIME_FILES: Mapping[str, tuple[Path, str]] = {
    "v2_actor_ledger": (
        Path("canonical/vnext_identity_instrument_v2_probe.py"),
        "e5d8b2f1c01b48b80ac51defe912fe6d54ea0cd77ad82175f38a3438fc3658b1",
    ),
    "wp26_packet_builder": (
        Path("canonical/vnext_identity_instrument_probe.py"),
        "53ab52464aa0990e0748c1177544007ba97a8930a98d3e16263a72c5d3e93449",
    ),
    "wp25_packet_renderer": (
        Path("canonical/vnext_identity_band_precision.py"),
        "fe0072b9440eb080f039990c65c6a882d635df1bf0021f5d6a98bf977a558bfa",
    ),
    "review_renderer_support": (
        Path("canonical/vnext_identity_review.py"),
        "ac22002e0cca43c28a91daee8a2d8780cc5f606143e854780f45d502c44054de",
    ),
    "fetch_primitive": (
        Path("canonical/cross_source_semantic_fetch.py"),
        "54cbd5dbce7c602d4bcfc458b611a655f36045adb98451bc481284251e4d79e6",
    ),
    "selection_support": (
        Path("canonical/cross_source_image_selection.py"),
        "73e73e60de70ac934d3b9601ef9a73b57faa539e3c305cf31e1bc377ca182b46",
    ),
    "derivative_renderer": (
        Path("canonical/divisare_vision_benchmark.py"),
        "f3ac879e667cf903e3a333951d02d40a7a05825b6ec0261a29ab1da025bb801e",
    ),
    "fingerprint_support": (
        Path("canonical/image_fingerprint.py"),
        "a72f5fec6b669a3e0e391af121c433e42395fe6e87c737225abab0a706cb47f3",
    ),
}

FROZEN_REPAIR_FILES: Mapping[str, tuple[Path, str]] = {
    "wp28r_order": (DEFAULT_REPAIR_ORDER, EXPECTED_REPAIR_ORDER_SHA256),
}

EXPECTED_SEMANTIC_SHA256: Mapping[str, str] = {
    "axis_instructions": "2ec1bfb8c91d7d5d0b845c268d04e93a690a6779a74584bfaf37ac305b2b28a0",
    "controlled_values": "27e38d1bb313d9e9be370bbdfa641252706d96d6e065d8a1ad45881f0a19dd28",
    "consensus_priority": "d345e7455891c63784da587912ba28308a38149794fc9f644cd581feb0ce9ab0",
    "derivation_rules": "343a3c3c2f81bb0042ece2dda0806ce6ee9201c9be140443671d44c74501b26c",
}


class IdentityInstrumentV21RepairError(RuntimeError):
    """A fail-closed WP2.8-R contract violation."""


@dataclass(frozen=True)
class ProjectionContract:
    path: Path
    run_id: str
    source_run_id: str
    source_sample_version: str
    source_seed: str
    source_logical_sha256: str
    packet_binding_sha256: str
    visual_inventory_sha256: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class InitializeResult:
    artifact_path: Path
    projection_path: Path
    run_id: str
    status: str
    excluded_sample_ids: tuple[str, ...]


@dataclass(frozen=True)
class RepairRevealResult:
    artifact_path: Path
    report_path: Path
    manifest_path: Path
    run_id: str
    status: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path(root: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (root / value).resolve()


def _path_text(root: Path, value: Path) -> str:
    resolved = value.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_text(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(value: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(value))


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _atomic_json(path: Path, value: Any, *, allow_identical: bool = False) -> None:
    raw = _json_bytes(value)
    if path.exists():
        if allow_identical and path.read_bytes() == raw:
            return
        raise IdentityInstrumentV21RepairError(f"refusing to clobber artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{hashlib.sha256(raw).hexdigest()[:16]}.tmp")
    if temporary.exists():
        raise IdentityInstrumentV21RepairError(f"stale temporary artifact: {temporary}")
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def _open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _one_run(connection: sqlite3.Connection) -> sqlite3.Row:
    rows = connection.execute("SELECT * FROM runs").fetchall()
    if len(rows) != 1:
        raise IdentityInstrumentV21RepairError("source must contain exactly one run")
    return rows[0]


def _file_proofs(root: Path) -> list[dict[str, Any]]:
    proofs: list[dict[str, Any]] = []
    for family, values in (
        ("instrument", FROZEN_INSTRUMENT_FILES),
        ("runtime_dependency", FROZEN_RUNTIME_FILES),
        ("repair_order", FROZEN_REPAIR_FILES),
    ):
        for role, (relative, expected) in values.items():
            path = _path(root, relative)
            actual = sha256_file(path) if path.is_file() else None
            proofs.append(
                {
                    "family": family,
                    "role": role,
                    "path": _path_text(root, path),
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                    "matched": int(actual == expected),
                }
            )
    return proofs


def _semantic_proofs() -> list[dict[str, Any]]:
    actual = {
        "axis_instructions": canonical_sha256(v21._axis_instructions()),
        "controlled_values": canonical_sha256(
            {axis: list(v21.CONTROLLED_VALUES[axis]) for axis in v21.CONTROLLED_KEYS}
        ),
        "consensus_priority": canonical_sha256(
            {axis: list(v21.CONSENSUS_PRIORITY[axis]) for axis in v21.CONTROLLED_KEYS}
        ),
        "derivation_rules": canonical_sha256(v21.derivation_rules()),
    }
    return [
        {
            "name": name,
            "expected_sha256": EXPECTED_SEMANTIC_SHA256[name],
            "actual_sha256": digest,
            "matched": int(digest == EXPECTED_SEMANTIC_SHA256[name]),
        }
        for name, digest in sorted(actual.items())
    ]


def assert_frozen_contract(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files, semantics = _file_proofs(root), _semantic_proofs()
    failures = [row["role"] for row in files if not row["matched"]]
    failures.extend(row["name"] for row in semantics if not row["matched"])
    if failures:
        raise IdentityInstrumentV21RepairError(
            "frozen v2.1 contract changed: " + ", ".join(failures)
        )
    repair_order = _path(root, DEFAULT_REPAIR_ORDER)
    if repair_order.stat().st_size != EXPECTED_REPAIR_ORDER_SIZE:
        raise IdentityInstrumentV21RepairError(
            "WP2.8-R order byte length changed"
        )
    return files, semantics


PROJECTION_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE runs(
 run_id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL,
 source_sample_version TEXT NOT NULL, source_seed TEXT NOT NULL,
 source_logical_sha256 TEXT NOT NULL, source_packet_binding_sha256 TEXT NOT NULL,
 source_visual_inventory_sha256 TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE input_pins(
 run_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, usage TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,role)
);
CREATE TABLE origin_pins(
 run_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, usage TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,role)
);
CREATE TABLE source_samples(
 run_id TEXT NOT NULL, sample_rank INTEGER NOT NULL, sample_id TEXT NOT NULL,
 seed_rank_sha256 TEXT NOT NULL, control_commitment_sha256 TEXT NOT NULL,
 snapshot_sha256 TEXT NOT NULL, source_pair_record_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_rank), UNIQUE(run_id,sample_id)
);
CREATE TABLE source_sheets(
 run_id TEXT NOT NULL, lane TEXT NOT NULL, display_order INTEGER NOT NULL,
 opaque_review_id TEXT NOT NULL, sample_id TEXT NOT NULL, sheet_path TEXT NOT NULL,
 sheet_sha256 TEXT NOT NULL, sheet_bytes INTEGER NOT NULL,
 source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,lane,opaque_review_id), UNIQUE(run_id,lane,display_order)
);
CREATE TABLE source_assets(
 run_id TEXT NOT NULL, sample_id TEXT NOT NULL, side TEXT NOT NULL,
 asset_order INTEGER NOT NULL, asset_key TEXT NOT NULL, source TEXT NOT NULL,
 source_building_id TEXT NOT NULL, source_asset_id TEXT NOT NULL,
 selection_reason TEXT NOT NULL, source_record_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id,side,asset_order)
);
CREATE TABLE source_fetch_assets(
 run_id TEXT NOT NULL, asset_key TEXT NOT NULL, source TEXT NOT NULL,
 source_asset_id TEXT NOT NULL, expected_url TEXT, expected_raw_sha256 TEXT,
 expected_pixel_sha256 TEXT, status TEXT NOT NULL, cache_role TEXT, cache_path TEXT,
 cache_file_sha256 TEXT, actual_final_url TEXT, actual_raw_sha256 TEXT,
 actual_pixel_sha256 TEXT, http_status INTEGER, response_bytes INTEGER,
 request_count INTEGER NOT NULL, error_kind TEXT, error_message TEXT,
 source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,asset_key)
);
CREATE TABLE source_fetch_attempts(
 run_id TEXT NOT NULL, asset_key TEXT NOT NULL, attempt_no INTEGER NOT NULL,
 status TEXT NOT NULL, request_count INTEGER NOT NULL,
 redirect_chain_json TEXT NOT NULL, started_at TEXT NOT NULL,
 completed_at TEXT NOT NULL, error_kind TEXT, source_record_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,asset_key,attempt_no)
);
CREATE TABLE source_cache_pins(
 run_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL,
 transform_version TEXT NOT NULL, access_mode TEXT NOT NULL,
 file_count_before INTEGER NOT NULL, total_bytes_before INTEGER NOT NULL,
 tree_sha256_before TEXT NOT NULL, file_count_after INTEGER,
 total_bytes_after INTEGER, tree_sha256_after TEXT,
 source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,role)
);
CREATE TABLE control_reveals(
 run_id TEXT NOT NULL, sample_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
 group_code TEXT NOT NULL, n100_sample_id TEXT NOT NULL,
 n100_gold_label TEXT NOT NULL, v1_probe_label TEXT NOT NULL,
 v2_derived_label TEXT NOT NULL, derivation_rule_no INTEGER NOT NULL,
 commitment_sha256 TEXT NOT NULL, revealed_at TEXT NOT NULL,
 reveal_payload_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,sample_id)
);
"""


def _projection_rows(
    root: Path,
    repaired: sqlite3.Connection,
    controls: sqlite3.Connection,
    projection_run_id: str,
) -> dict[str, list[dict[str, Any]]]:
    samples: list[dict[str, Any]] = []
    snapshots = {
        str(row["sample_id"]): row
        for row in repaired.execute("SELECT * FROM candidate_snapshots")
    }
    commitments = {
        str(row["sample_id"]): row
        for row in repaired.execute("SELECT * FROM control_commitments")
    }
    for row in repaired.execute("SELECT * FROM sample_pairs ORDER BY sample_rank"):
        sample_id = str(row["sample_id"])
        snapshot, commitment = snapshots.get(sample_id), commitments.get(sample_id)
        if snapshot is None or commitment is None:
            raise IdentityInstrumentV21RepairError("repaired sample binding is incomplete")
        payload = {
            "run_id": projection_run_id,
            "sample_rank": int(row["sample_rank"]),
            "sample_id": sample_id,
            "seed_rank_sha256": str(row["seed_rank_sha256"]),
            "control_commitment_sha256": str(commitment["gold_commitment_sha256"]),
            "snapshot_sha256": str(snapshot["snapshot_sha256"]),
            "source_pair_record_sha256": str(row["record_sha256"]),
        }
        samples.append({**payload, "record_sha256": _record(payload)})

    sheets = []
    for row in repaired.execute("SELECT * FROM review_items ORDER BY lane,display_order"):
        sheet = _resolve_text(root, str(row["sheet_path"]))
        if (
            not sheet.is_file()
            or sheet.stat().st_size != int(row["sheet_bytes"])
            or sha256_file(sheet) != str(row["sheet_sha256"])
        ):
            raise IdentityInstrumentV21RepairError("repaired review sheet missing or changed")
        payload = {
            "run_id": projection_run_id,
            **{
                key: row[key]
                for key in (
                    "lane",
                    "display_order",
                    "opaque_review_id",
                    "sample_id",
                    "sheet_path",
                    "sheet_sha256",
                    "sheet_bytes",
                )
            },
            "source_record_sha256": str(row["record_sha256"]),
        }
        sheets.append({**payload, "record_sha256": _record(payload)})

    assets = []
    for row in repaired.execute(
        "SELECT * FROM sample_assets ORDER BY sample_id,side,asset_order"
    ):
        payload = {
            "run_id": projection_run_id,
            **{
                key: row[key]
                for key in (
                    "sample_id",
                    "side",
                    "asset_order",
                    "asset_key",
                    "source",
                    "source_building_id",
                    "source_asset_id",
                    "selection_reason",
                )
            },
            "source_record_sha256": str(row["record_sha256"]),
        }
        assets.append({**payload, "record_sha256": _record(payload)})

    fetch_assets = []
    fetch_columns = (
        "asset_key",
        "source",
        "source_asset_id",
        "expected_url",
        "expected_raw_sha256",
        "expected_pixel_sha256",
        "status",
        "cache_role",
        "cache_path",
        "actual_final_url",
        "actual_raw_sha256",
        "actual_pixel_sha256",
        "http_status",
        "response_bytes",
        "request_count",
        "error_kind",
        "error_message",
    )
    for row in repaired.execute("SELECT * FROM fetch_assets ORDER BY asset_key"):
        cache_sha: str | None = None
        if row["cache_path"] is not None:
            cache = _resolve_text(root, str(row["cache_path"]))
            if not cache.is_file():
                raise IdentityInstrumentV21RepairError("repaired cache asset is missing")
            cache_sha = sha256_file(cache)
        # Keep insertion order identical to ``source_fetch_assets``.  The
        # compatibility projection adds the cache byte pin between cache_path
        # and actual_final_url; the repaired WP2.6 table does not have it.
        payload = {
            "run_id": projection_run_id,
            **{key: row[key] for key in fetch_columns[:9]},
            "cache_file_sha256": cache_sha,
            **{key: row[key] for key in fetch_columns[9:]},
            "source_record_sha256": str(row["record_sha256"]),
        }
        fetch_assets.append({**payload, "record_sha256": _record(payload)})

    attempts = []
    for row in repaired.execute(
        "SELECT * FROM fetch_attempts ORDER BY asset_key,attempt_no"
    ):
        payload = {
            "run_id": projection_run_id,
            **{
                key: row[key]
                for key in (
                    "asset_key",
                    "attempt_no",
                    "status",
                    "request_count",
                    "redirect_chain_json",
                    "started_at",
                    "completed_at",
                    "error_kind",
                )
            },
            "source_record_sha256": str(row["record_sha256"]),
        }
        attempts.append({**payload, "record_sha256": _record(payload)})

    caches = []
    for row in repaired.execute("SELECT * FROM cache_pins ORDER BY role"):
        payload = {
            "run_id": projection_run_id,
            **{
                key: row[key]
                for key in (
                    "role",
                    "path",
                    "transform_version",
                    "access_mode",
                    "file_count_before",
                    "total_bytes_before",
                    "tree_sha256_before",
                    "file_count_after",
                    "total_bytes_after",
                    "tree_sha256_after",
                )
            },
            "source_record_sha256": str(row["record_sha256"]),
        }
        caches.append({**payload, "record_sha256": _record(payload)})

    controls_by_sample = {
        str(row["sample_id"]): row
        for row in controls.execute("SELECT * FROM control_reveals ORDER BY sample_id")
    }
    if set(controls_by_sample) != {row["sample_id"] for row in samples}:
        raise IdentityInstrumentV21RepairError("repaired samples/control reveals differ")
    reveal_rows = []
    for sample in samples:
        old = controls_by_sample[str(sample["sample_id"])]
        if str(old["commitment_sha256"]) != str(sample["control_commitment_sha256"]):
            raise IdentityInstrumentV21RepairError("control commitment changed during repair")
        payload = {
            "run_id": projection_run_id,
            **{
                key: old[key]
                for key in (
                    "sample_id",
                    "candidate_id",
                    "group_code",
                    "n100_sample_id",
                    "n100_gold_label",
                    "v1_probe_label",
                    "v2_derived_label",
                    "derivation_rule_no",
                    "commitment_sha256",
                    "revealed_at",
                    "reveal_payload_sha256",
                )
            },
        }
        reveal_rows.append({**payload, "record_sha256": _record(payload)})
    return {
        "source_samples": samples,
        "source_sheets": sheets,
        "source_assets": assets,
        "source_fetch_assets": fetch_assets,
        "source_fetch_attempts": attempts,
        "source_cache_pins": caches,
        "control_reveals": reveal_rows,
    }


def _projection_contract_from_db(path: Path) -> ProjectionContract:
    connection = _open_ro(path)
    try:
        run = _one_run(connection)
        return ProjectionContract(
            path=path,
            run_id=str(run["run_id"]),
            source_run_id=str(run["source_run_id"]),
            source_sample_version=str(run["source_sample_version"]),
            source_seed=str(run["source_seed"]),
            source_logical_sha256=str(run["source_logical_sha256"]),
            packet_binding_sha256=str(run["source_packet_binding_sha256"]),
            visual_inventory_sha256=str(run["source_visual_inventory_sha256"]),
            size_bytes=path.stat().st_size,
            sha256=sha256_file(path),
        )
    finally:
        connection.close()


def _projection_manifest_payload(
    root: Path,
    contract: ProjectionContract,
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    repaired_source: Path,
    control_source: Path,
) -> dict[str, Any]:
    statuses = Counter(str(row["status"]) for row in rows["source_fetch_assets"])
    return {
        "status": "PASS",
        "version": PROJECTION_VERSION,
        "run_id": contract.run_id,
        "projection": {
            "path": _path_text(root, contract.path),
            "size_bytes": contract.size_bytes,
            "sha256": contract.sha256,
        },
        "origins": {
            "repaired_wp26": {
                "path": _path_text(root, repaired_source),
                "size_bytes": repaired_source.stat().st_size,
                "sha256": sha256_file(repaired_source),
            },
            "immutable_wp27_controls": {
                "path": _path_text(root, control_source),
                "size_bytes": control_source.stat().st_size,
                "sha256": sha256_file(control_source),
            },
        },
        "source_contract": {
            "source_run_id": contract.source_run_id,
            "source_sample_version": contract.source_sample_version,
            "source_seed": contract.source_seed,
            "source_logical_sha256": contract.source_logical_sha256,
            "packet_binding_sha256": contract.packet_binding_sha256,
            "visual_inventory_sha256": contract.visual_inventory_sha256,
        },
        "counts": {
            "samples": len(rows["source_samples"]),
            "sheets": len(rows["source_sheets"]),
            "asset_occurrences": len(rows["source_assets"]),
            "unique_assets": len(rows["source_fetch_assets"]),
            "fetch_attempts": len(rows["source_fetch_attempts"]),
            "cache_pins": len(rows["source_cache_pins"]),
            "controls": len(rows["control_reveals"]),
            "fetch_statuses": dict(sorted(statuses.items())),
        },
        "network": {
            "inherited_logical": len(rows["source_fetch_attempts"]),
            "inherited_wire": sum(
                int(row["request_count"]) for row in rows["source_fetch_attempts"]
            ),
            "incremental_logical": 0,
            "incremental_wire": 0,
            "retries": 0,
            "redirects": 0,
        },
        "vision_requests": 0,
        "llm_requests": 0,
        "paid_api_requests": 0,
        "merge_executions": 0,
        "vnext_id_issuances": 0,
        "tier_promotions": 0,
        "neon_writes": 0,
        "r2_writes": 0,
    }


def _assert_exact_repair_source_delta(
    original: sqlite3.Connection, repaired: sqlite3.Connection
) -> None:
    """Prove that the repaired packet changed only the 17 failed fetches/sheets."""

    def table_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    for table in (
        "sample_pairs",
        "candidate_snapshots",
        "control_commitments",
        "sample_assets",
    ):
        before = sorted(table_rows(original, table), key=canonical_json)
        after = sorted(table_rows(repaired, table), key=canonical_json)
        if before != after:
            raise IdentityInstrumentV21RepairError(
                f"repair changed frozen non-fetch table: {table}"
            )

    old_fetch = {
        str(row["asset_key"]): row for row in table_rows(original, "fetch_assets")
    }
    new_fetch = {
        str(row["asset_key"]): row for row in table_rows(repaired, "fetch_assets")
    }
    if set(old_fetch) != set(new_fetch):
        raise IdentityInstrumentV21RepairError("repair changed the asset-key inventory")
    old_failed = {
        key for key, row in old_fetch.items() if str(row["status"]) == "failed"
    }
    changed = {key for key in old_fetch if old_fetch[key] != new_fetch[key]}
    if len(old_failed) != 17 or changed != old_failed:
        raise IdentityInstrumentV21RepairError(
            "repair delta is not exactly the original failed 17"
        )
    immutable_fetch_fields = (
        "asset_key",
        "source",
        "source_asset_id",
        "expected_url",
        "expected_raw_sha256",
        "expected_pixel_sha256",
    )
    for key in sorted(old_failed):
        before, after = old_fetch[key], new_fetch[key]
        if any(before[field] != after[field] for field in immutable_fetch_fields):
            raise IdentityInstrumentV21RepairError(
                "repair changed immutable fetch identity/expectations"
            )
        if (
            str(after["status"]) not in READABLE_ASSET_STATUSES
            or int(after["request_count"]) != 1
            or after["cache_path"] is None
        ):
            raise IdentityInstrumentV21RepairError("repaired failed fetch is not usable")

    stable_review_fields = ("lane", "display_order", "opaque_review_id", "sample_id")
    before_reviews = sorted(
        [tuple(row[field] for field in stable_review_fields)
         for row in table_rows(original, "review_items")]
    )
    after_reviews = sorted(
        [tuple(row[field] for field in stable_review_fields)
         for row in table_rows(repaired, "review_items")]
    )
    if before_reviews != after_reviews or len(before_reviews) != 48:
        raise IdentityInstrumentV21RepairError("repair changed blind review identities")


def _assert_no_path_collisions(named: Mapping[str, Path]) -> None:
    resolved: dict[Path, str] = {}
    for role, path in named.items():
        target = path.resolve()
        if target in resolved:
            raise IdentityInstrumentV21RepairError(
                f"path collision: {resolved[target]} and {role}"
            )
        resolved[target] = role


def build_source_projection(
    *,
    repo_root: Path,
    repaired_source_db_path: Path = DEFAULT_REPAIRED_SOURCE_DB,
    original_source_db_path: Path = DEFAULT_ORIGINAL_WP26_DB,
    control_source_db_path: Path = DEFAULT_ORIGINAL_WP27_DB,
    projection_path: Path = DEFAULT_PROJECTION_DB,
    manifest_path: Path = DEFAULT_PROJECTION_MANIFEST,
    enforce_production_contract: bool = True,
) -> ProjectionContract:
    """Create the no-clobber WP26-repair to v2.1 source projection."""
    root = repo_root.resolve()
    repaired_path = _path(root, repaired_source_db_path)
    original_path = _path(root, original_source_db_path)
    controls_path = _path(root, control_source_db_path)
    output = _path(root, projection_path)
    manifest = _path(root, manifest_path)
    _assert_no_path_collisions(
        {
            "original_wp26": original_path,
            "repaired_wp26": repaired_path,
            "control_source": controls_path,
            "projection": output,
            "projection_manifest": manifest,
        }
    )
    if output.exists():
        contract = _projection_contract_from_db(output)
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise IdentityInstrumentV21RepairError(
                    "projection manifest is unreadable"
                ) from exc
            expected_projection = {
                "path": _path_text(root, contract.path),
                "size_bytes": contract.size_bytes,
                "sha256": contract.sha256,
            }
            if (
                payload.get("status") != "PASS"
                or payload.get("version") != PROJECTION_VERSION
                or payload.get("run_id") != contract.run_id
                or payload.get("projection") != expected_projection
                or payload.get("source_contract", {}).get("packet_binding_sha256")
                != contract.packet_binding_sha256
                or payload.get("source_contract", {}).get("visual_inventory_sha256")
                != contract.visual_inventory_sha256
            ):
                raise IdentityInstrumentV21RepairError(
                    "projection manifest recovery mismatch"
                )
            return contract
        if manifest.exists():
            raise IdentityInstrumentV21RepairError(
                "projection manifest recovery target is not a file"
            )
        if (
            not repaired_path.is_file()
            or not controls_path.is_file()
            or (enforce_production_contract and not original_path.is_file())
        ):
            raise IdentityInstrumentV21RepairError(
                "projection recovery source is missing"
            )
        original_recovery = (
            _open_ro(original_path) if enforce_production_contract else None
        )
        repaired_recovery, controls_recovery = (
            _open_ro(repaired_path),
            _open_ro(controls_path),
        )
        projection_recovery = _open_ro(output)
        try:
            if original_recovery is not None:
                _assert_exact_repair_source_delta(
                    original_recovery, repaired_recovery
                )
            rows = _projection_rows(
                root,
                repaired_recovery,
                controls_recovery,
                contract.run_id,
            )
            for table, expected_rows in rows.items():
                actual_rows = [
                    dict(row)
                    for row in projection_recovery.execute(f"SELECT * FROM {table}")
                ]
                if sorted(actual_rows, key=canonical_json) != sorted(
                    expected_rows, key=canonical_json
                ):
                    raise IdentityInstrumentV21RepairError(
                        "projection DB changed before manifest recovery"
                    )
            payload = _projection_manifest_payload(
                root, contract, rows, repaired_path, controls_path
            )
            _atomic_json(manifest, payload)
            return contract
        finally:
            if original_recovery is not None:
                original_recovery.close()
            repaired_recovery.close()
            controls_recovery.close()
            projection_recovery.close()
    if manifest.exists():
        raise IdentityInstrumentV21RepairError("projection manifest exists without DB")
    if (
        (enforce_production_contract and not original_path.is_file())
        or not repaired_path.is_file()
        or not controls_path.is_file()
    ):
        raise IdentityInstrumentV21RepairError("projection source DB is missing")

    original = _open_ro(original_path) if enforce_production_contract else None
    repaired, controls = _open_ro(repaired_path), _open_ro(controls_path)
    try:
        if original is not None:
            _assert_exact_repair_source_delta(original, repaired)
        repaired_run, control_run = _one_run(repaired), _one_run(controls)
        if enforce_production_contract and (
            str(repaired_run["run_id"]) != v21.SOURCE_RUN_ID
            or str(repaired_run["sample_version"]) != v21.SOURCE_SAMPLE_VERSION
            or str(repaired_run["seed"]) != v21.SOURCE_SEED
            or str(repaired_run["status"]) != "review_ready"
            or str(control_run["run_id"]) != v21.WP27_RUN_ID
        ):
            raise IdentityInstrumentV21RepairError("production source run contract changed")
        source_logical = str(repaired_run["sample_logical_sha256"])
        provisional_id = "wp28r-source-" + hashlib.sha256(
            (
                f"{PROJECTION_VERSION}\0{sha256_file(repaired_path)}\0"
                f"{sha256_file(controls_path)}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        rows = _projection_rows(root, repaired, controls, provisional_id)
        packet_rows = [
            {
                key: row[key]
                for key in (
                    "lane",
                    "display_order",
                    "opaque_review_id",
                    "sample_id",
                    "sheet_path",
                    "sheet_sha256",
                    "sheet_bytes",
                )
            }
            for row in rows["source_sheets"]
        ]
        packet_sha = canonical_sha256(packet_rows)
        visual_sha = canonical_sha256(
            [
                {
                    "lane": row["lane"],
                    "display_order": row["display_order"],
                    "path": row["sheet_path"],
                    "sha256": row["sheet_sha256"],
                    "bytes": row["sheet_bytes"],
                }
                for row in rows["source_sheets"]
            ]
        )
        statuses = Counter(str(row["status"]) for row in rows["source_fetch_assets"])
        per_side = Counter(
            (str(row["sample_id"]), str(row["side"])) for row in rows["source_assets"]
        )
        if enforce_production_contract and (
            tuple(len(rows[name]) for name in (
                "source_samples",
                "source_sheets",
                "source_assets",
                "source_fetch_assets",
                "source_fetch_attempts",
                "source_cache_pins",
                "control_reveals",
            ))
            != (24, 48, 93, 93, 17, 5, 24)
            or statuses != Counter({"cache_hit": 76, "source_changed": 17})
            or not per_side
            or max(per_side.values()) > 2
        ):
            raise IdentityInstrumentV21RepairError("repaired production counts changed")
        correction = controls.execute(
            "SELECT path,size_bytes,sha256,usage FROM input_pins "
            "WHERE role='wp26_fetch_correction'"
        ).fetchone()
        if correction is None:
            raise IdentityInstrumentV21RepairError("WP2.6 correction provenance is missing")
        created_at = str(repaired_run["created_at"])
        run_payload = {
            "run_id": provisional_id,
            "source_run_id": str(repaired_run["run_id"]),
            "source_sample_version": str(repaired_run["sample_version"]),
            "source_seed": str(repaired_run["seed"]),
            "source_logical_sha256": source_logical,
            "source_packet_binding_sha256": packet_sha,
            "source_visual_inventory_sha256": visual_sha,
            "status": "projection_frozen",
            "created_at": created_at,
        }
        origin_pins = [
            {
                "run_id": provisional_id,
                "role": "repaired_wp26_source",
                "path": _path_text(root, repaired_path),
                "size_bytes": repaired_path.stat().st_size,
                "sha256": sha256_file(repaired_path),
                "usage": "repaired_packet_and_sheets",
            },
            {
                "run_id": provisional_id,
                "role": "immutable_wp27_controls",
                "path": _path_text(root, controls_path),
                "size_bytes": controls_path.stat().st_size,
                "sha256": sha256_file(controls_path),
                "usage": "committed_control_mapping_only",
            },
        ]
        correction_payload = {
            "run_id": provisional_id,
            "role": "wp26_fetch_correction",
            "path": str(correction["path"]),
            "size_bytes": int(correction["size_bytes"]),
            "sha256": str(correction["sha256"]),
            "usage": str(correction["usage"]),
        }

        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".building")
        if temporary.exists():
            raise IdentityInstrumentV21RepairError("stale projection build exists")
        database = sqlite3.connect(temporary)
        try:
            database.execute("PRAGMA journal_mode=DELETE")
            database.execute("PRAGMA synchronous=FULL")
            database.executescript(PROJECTION_SCHEMA)
            database.execute(
                "INSERT INTO runs VALUES(" + ",".join("?" for _ in range(10)) + ")",
                (*run_payload.values(), _record(run_payload)),
            )
            database.execute(
                "INSERT INTO input_pins VALUES(?,?,?,?,?,?,?)",
                (*correction_payload.values(), _record(correction_payload)),
            )
            for pin in origin_pins:
                database.execute(
                    "INSERT INTO origin_pins VALUES(?,?,?,?,?,?,?)",
                    (*pin.values(), _record(pin)),
                )
            for table, table_rows in rows.items():
                for row in table_rows:
                    database.execute(
                        f"INSERT INTO {table} VALUES("
                        + ",".join("?" for _ in row)
                        + ")",
                        tuple(row.values()),
                    )
            database.commit()
        except Exception:
            database.rollback()
            database.close()
            if temporary.exists():
                temporary.unlink()
            raise
        else:
            database.close()
            os.replace(temporary, output)
    finally:
        if original is not None:
            original.close()
        repaired.close()
        controls.close()

    contract = _projection_contract_from_db(output)
    payload = _projection_manifest_payload(
        root, contract, rows, repaired_path, controls_path
    )
    _atomic_json(manifest, payload)
    return contract


@contextmanager
def _repaired_source_context(contract: ProjectionContract) -> Iterable[None]:
    """Patch only source-binding globals; all instrument behavior stays frozen."""
    replacements = {
        "SOURCE_RUN_ID": contract.source_run_id,
        "SOURCE_SAMPLE_VERSION": contract.source_sample_version,
        "SOURCE_SEED": contract.source_seed,
        "SOURCE_LOGICAL_SHA256": contract.source_logical_sha256,
        "SOURCE_PACKET_BINDING_SHA256": contract.packet_binding_sha256,
        "SOURCE_VISUAL_INVENTORY_SHA256": contract.visual_inventory_sha256,
        "WP27_RUN_ID": contract.run_id,
        "WP27_DB_SIZE": contract.size_bytes,
        "WP27_DB_SHA256": contract.sha256,
    }
    previous = {name: getattr(v21, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(v21, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(v21, name, value)


REPAIR_SCHEMA = """
CREATE TABLE IF NOT EXISTS repair_contract(
 run_id TEXT PRIMARY KEY, repair_version TEXT NOT NULL,
 repair_order_path TEXT NOT NULL, repair_order_sha256 TEXT NOT NULL,
 original_wp26_path TEXT NOT NULL, original_wp26_sha256 TEXT NOT NULL,
 repaired_wp26_path TEXT NOT NULL, repaired_wp26_sha256 TEXT NOT NULL,
 projection_path TEXT NOT NULL, projection_sha256 TEXT NOT NULL,
 original_wp28_path TEXT NOT NULL, original_wp28_sha256 TEXT NOT NULL,
 base_gate_sha256 TEXT NOT NULL, excluded_sample_ids_json TEXT NOT NULL,
 created_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repair_file_proofs(
 run_id TEXT NOT NULL, family TEXT NOT NULL, role TEXT NOT NULL,
 path TEXT NOT NULL, expected_sha256 TEXT NOT NULL, actual_sha256 TEXT,
 matched INTEGER NOT NULL CHECK(matched IN (0,1)), record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,role)
);
CREATE TABLE IF NOT EXISTS repair_semantic_proofs(
 run_id TEXT NOT NULL, name TEXT NOT NULL, expected_sha256 TEXT NOT NULL,
 actual_sha256 TEXT NOT NULL, matched INTEGER NOT NULL CHECK(matched IN (0,1)),
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,name)
);
CREATE TABLE IF NOT EXISTS asset_recovery(
 run_id TEXT NOT NULL, asset_key TEXT NOT NULL, sample_id TEXT NOT NULL,
 side TEXT NOT NULL, asset_order INTEGER NOT NULL, source TEXT NOT NULL,
 source_asset_id TEXT NOT NULL, old_status TEXT NOT NULL,
 old_error_kind TEXT, old_error_message TEXT, new_status TEXT NOT NULL,
 new_final_url TEXT, new_raw_sha256 TEXT, new_pixel_sha256 TEXT,
 new_cache_path TEXT, new_cache_sha256 TEXT, request_count INTEGER NOT NULL,
 recovered INTEGER NOT NULL CHECK(recovered IN (0,1)),
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,asset_key)
);
CREATE TABLE IF NOT EXISTS asset_completeness(
 run_id TEXT NOT NULL, sample_id TEXT NOT NULL, side TEXT NOT NULL,
 selected_count INTEGER NOT NULL, usable_count INTEGER NOT NULL,
 asset_keys_json TEXT NOT NULL, statuses_json TEXT NOT NULL,
 excluded_no_assets INTEGER NOT NULL CHECK(excluded_no_assets IN (0,1)),
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id,side)
);
CREATE TABLE IF NOT EXISTS sample_exclusions(
 run_id TEXT NOT NULL, sample_id TEXT NOT NULL, reason TEXT NOT NULL,
 excluded_from_gate_scoring INTEGER NOT NULL CHECK(excluded_from_gate_scoring=1),
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id)
);
CREATE TABLE IF NOT EXISTS effective_gate_contract(
 run_id TEXT NOT NULL, predicate TEXT NOT NULL, base_gate_sha256 TEXT NOT NULL,
 original_denominator INTEGER NOT NULL, original_threshold INTEGER NOT NULL,
 effective_denominator INTEGER NOT NULL, effective_threshold INTEGER NOT NULL,
 ratio_numerator INTEGER NOT NULL, ratio_denominator INTEGER NOT NULL,
 rule TEXT NOT NULL, excluded_sample_ids_json TEXT NOT NULL,
 declared_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,predicate)
);
CREATE TABLE IF NOT EXISTS score_authority(
 run_id TEXT PRIMARY KEY, base_gate_sha256 TEXT NOT NULL,
 core_score_role TEXT NOT NULL,
 core_decision_authority INTEGER NOT NULL CHECK(core_decision_authority=0),
 effective_gate_sha256 TEXT NOT NULL, effective_score_role TEXT NOT NULL,
 effective_decision_authority INTEGER NOT NULL CHECK(effective_decision_authority=1),
 excluded_sample_ids_json TEXT NOT NULL, declared_at TEXT NOT NULL,
 record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effective_gate_scores(
 run_id TEXT NOT NULL, predicate TEXT NOT NULL, actual INTEGER NOT NULL,
 denominator INTEGER NOT NULL, threshold INTEGER NOT NULL,
 passed INTEGER NOT NULL CHECK(passed IN (0,1)), record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,predicate)
);
CREATE TABLE IF NOT EXISTS repair_terminal(
 run_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('pass','fail')),
 base_status TEXT NOT NULL CHECK(base_status IN ('pass','fail')),
 effective_gate_sha256 TEXT NOT NULL, completed_at TEXT NOT NULL,
 record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repair_events(
 run_id TEXT NOT NULL, event_no INTEGER NOT NULL, event TEXT NOT NULL,
 detail_json TEXT NOT NULL, occurred_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,event_no)
);
CREATE TRIGGER IF NOT EXISTS repair_contract_no_update BEFORE UPDATE ON repair_contract
 BEGIN SELECT RAISE(ABORT,'repair contract frozen'); END;
CREATE TRIGGER IF NOT EXISTS repair_contract_no_delete BEFORE DELETE ON repair_contract
 BEGIN SELECT RAISE(ABORT,'repair contract frozen'); END;
CREATE TRIGGER IF NOT EXISTS recovery_no_update BEFORE UPDATE ON asset_recovery
 BEGIN SELECT RAISE(ABORT,'recovery frozen'); END;
CREATE TRIGGER IF NOT EXISTS recovery_no_delete BEFORE DELETE ON asset_recovery
 BEGIN SELECT RAISE(ABORT,'recovery frozen'); END;
CREATE TRIGGER IF NOT EXISTS completeness_no_update BEFORE UPDATE ON asset_completeness
 BEGIN SELECT RAISE(ABORT,'completeness frozen'); END;
CREATE TRIGGER IF NOT EXISTS completeness_no_delete BEFORE DELETE ON asset_completeness
 BEGIN SELECT RAISE(ABORT,'completeness frozen'); END;
CREATE TRIGGER IF NOT EXISTS exclusions_no_update BEFORE UPDATE ON sample_exclusions
 BEGIN SELECT RAISE(ABORT,'exclusions frozen'); END;
CREATE TRIGGER IF NOT EXISTS exclusions_no_delete BEFORE DELETE ON sample_exclusions
 BEGIN SELECT RAISE(ABORT,'exclusions frozen'); END;
CREATE TRIGGER IF NOT EXISTS effective_gate_no_update BEFORE UPDATE ON effective_gate_contract
 BEGIN SELECT RAISE(ABORT,'effective gate frozen'); END;
CREATE TRIGGER IF NOT EXISTS effective_gate_no_delete BEFORE DELETE ON effective_gate_contract
 BEGIN SELECT RAISE(ABORT,'effective gate frozen'); END;
CREATE TRIGGER IF NOT EXISTS score_authority_no_update BEFORE UPDATE ON score_authority
 BEGIN SELECT RAISE(ABORT,'score authority frozen'); END;
CREATE TRIGGER IF NOT EXISTS score_authority_no_delete BEFORE DELETE ON score_authority
 BEGIN SELECT RAISE(ABORT,'score authority frozen'); END;
CREATE TRIGGER IF NOT EXISTS effective_scores_no_update BEFORE UPDATE ON effective_gate_scores
 BEGIN SELECT RAISE(ABORT,'effective scores frozen'); END;
CREATE TRIGGER IF NOT EXISTS effective_scores_no_delete BEFORE DELETE ON effective_gate_scores
 BEGIN SELECT RAISE(ABORT,'effective scores frozen'); END;
CREATE TRIGGER IF NOT EXISTS repair_terminal_no_update BEFORE UPDATE ON repair_terminal
 BEGIN SELECT RAISE(ABORT,'repair terminal frozen'); END;
CREATE TRIGGER IF NOT EXISTS repair_terminal_no_delete BEFORE DELETE ON repair_terminal
 BEGIN SELECT RAISE(ABORT,'repair terminal frozen'); END;
"""


def _old_failure_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
    SELECT f.asset_key,f.source,f.source_asset_id,f.status,f.error_kind,f.error_message,
           a.sample_id,a.side,a.asset_order
      FROM source_fetch_assets f JOIN source_assets a USING(asset_key)
     WHERE f.status='failed'
     ORDER BY f.asset_key
    """
    return [dict(row) for row in connection.execute(query)]


def _usable_asset(root: Path, row: Mapping[str, Any]) -> tuple[bool, str | None]:
    if str(row["status"]) not in READABLE_ASSET_STATUSES or row["cache_path"] is None:
        return False, None
    path = _resolve_text(root, str(row["cache_path"]))
    if not path.is_file():
        return False, None
    return True, sha256_file(path)


def _recovery_and_completeness(
    root: Path,
    repaired: sqlite3.Connection,
    original: sqlite3.Connection,
    run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    old_failures = _old_failure_rows(original)
    new_fetch = {
        str(row["asset_key"]): dict(row)
        for row in repaired.execute("SELECT * FROM fetch_assets ORDER BY asset_key")
    }
    recovery: list[dict[str, Any]] = []
    for old in old_failures:
        new = new_fetch.get(str(old["asset_key"]))
        if new is None:
            raise IdentityInstrumentV21RepairError("failed asset vanished during repair")
        usable, cache_sha = _usable_asset(root, new)
        payload = {
            "run_id": run_id,
            "asset_key": str(old["asset_key"]),
            "sample_id": str(old["sample_id"]),
            "side": str(old["side"]),
            "asset_order": int(old["asset_order"]),
            "source": str(old["source"]),
            "source_asset_id": str(old["source_asset_id"]),
            "old_status": str(old["status"]),
            "old_error_kind": old["error_kind"],
            "old_error_message": old["error_message"],
            "new_status": str(new["status"]),
            "new_final_url": new["actual_final_url"],
            "new_raw_sha256": new["actual_raw_sha256"],
            "new_pixel_sha256": new["actual_pixel_sha256"],
            "new_cache_path": new["cache_path"],
            "new_cache_sha256": cache_sha,
            "request_count": int(new["request_count"]),
            "recovered": int(usable),
        }
        recovery.append({**payload, "record_sha256": _record(payload)})

    asset_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in repaired.execute(
        "SELECT a.*,f.status,f.cache_path FROM sample_assets a "
        "JOIN fetch_assets f USING(run_id,asset_key) "
        "ORDER BY a.sample_id,a.side,a.asset_order"
    ):
        asset_rows[(str(row["sample_id"]), str(row["side"]))].append(dict(row))
    samples = [
        str(row[0])
        for row in repaired.execute("SELECT sample_id FROM sample_pairs ORDER BY sample_rank")
    ]
    completeness: list[dict[str, Any]] = []
    excluded: set[str] = set()
    for sample_id in samples:
        for side in ("left", "right"):
            rows = asset_rows.get((sample_id, side), [])
            usable = [row for row in rows if _usable_asset(root, row)[0]]
            no_assets = not usable
            if no_assets:
                excluded.add(sample_id)
            payload = {
                "run_id": run_id,
                "sample_id": sample_id,
                "side": side,
                "selected_count": len(rows),
                "usable_count": len(usable),
                "asset_keys_json": canonical_json([str(row["asset_key"]) for row in rows]),
                "statuses_json": canonical_json([str(row["status"]) for row in rows]),
                "excluded_no_assets": int(no_assets),
            }
            completeness.append({**payload, "record_sha256": _record(payload)})
    return recovery, completeness, sorted(excluded)


def _insert_repair_event(
    connection: sqlite3.Connection, run_id: str, event: str, detail: Mapping[str, Any]
) -> None:
    event_no = int(
        connection.execute(
            "SELECT coalesce(max(event_no),0)+1 FROM repair_events WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    occurred_at = utc_now()
    payload = {
        "run_id": run_id,
        "event_no": event_no,
        "event": event,
        "detail_json": canonical_json(detail),
        "occurred_at": occurred_at,
    }
    connection.execute(
        "INSERT INTO repair_events VALUES(?,?,?,?,?,?)",
        (*payload.values(), _record(payload)),
    )


def install_repair_overlay(
    *,
    repo_root: Path,
    artifact_path: Path,
    projection: ProjectionContract,
    repaired_source_db_path: Path = DEFAULT_REPAIRED_SOURCE_DB,
    original_wp26_db_path: Path = DEFAULT_ORIGINAL_WP26_DB,
    original_wp27_db_path: Path = DEFAULT_ORIGINAL_WP27_DB,
    original_wp28_db_path: Path = DEFAULT_ORIGINAL_WP28_DB,
    repair_order_path: Path = DEFAULT_REPAIR_ORDER,
    enforce_production_contract: bool = True,
) -> tuple[str, ...]:
    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    repaired_path = _path(root, repaired_source_db_path)
    original26_path = _path(root, original_wp26_db_path)
    original27_path = _path(root, original_wp27_db_path)
    original28_path = _path(root, original_wp28_db_path)
    order_path = _path(root, repair_order_path)
    files, semantics = assert_frozen_contract(root)
    if (
        order_path.stat().st_size != EXPECTED_REPAIR_ORDER_SIZE
        or sha256_file(order_path) != EXPECTED_REPAIR_ORDER_SHA256
    ):
        raise IdentityInstrumentV21RepairError("WP2.8-R repair order pin changed")
    connection = sqlite3.connect(artifact)
    connection.row_factory = sqlite3.Row
    repaired, original = _open_ro(repaired_path), _open_ro(original27_path)
    try:
        run = _one_run(connection)
        run_id = str(run["run_id"])
        connection.executescript(REPAIR_SCHEMA)
        existing = connection.execute("SELECT * FROM repair_contract").fetchone()
        if existing is not None:
            excluded_existing = tuple(
                json.loads(str(existing["excluded_sample_ids_json"]))
            )
            expected_existing = {
                "repair_version": REPAIR_VERSION,
                "repair_order_path": _path_text(root, order_path),
                "repair_order_sha256": EXPECTED_REPAIR_ORDER_SHA256,
                "original_wp26_path": _path_text(root, original26_path),
                "original_wp26_sha256": sha256_file(original26_path),
                "repaired_wp26_path": _path_text(root, repaired_path),
                "repaired_wp26_sha256": sha256_file(repaired_path),
                "projection_path": _path_text(root, projection.path),
                "projection_sha256": projection.sha256,
                "original_wp28_path": _path_text(root, original28_path),
                "original_wp28_sha256": sha256_file(original28_path),
                "base_gate_sha256": BASE_GATE_SHA256,
                "excluded_sample_ids_json": canonical_json(excluded_existing),
            }
            if any(existing[key] != value for key, value in expected_existing.items()):
                raise IdentityInstrumentV21RepairError(
                    "existing repair contract/source pin changed"
                )
            stored_files = {
                str(row["role"]): row
                for row in connection.execute(
                    "SELECT * FROM repair_file_proofs WHERE run_id=?", (run_id,)
                )
            }
            current_files = {str(row["role"]): row for row in files}
            stored_semantics = {
                str(row["name"]): row
                for row in connection.execute(
                    "SELECT * FROM repair_semantic_proofs WHERE run_id=?", (run_id,)
                )
            }
            current_semantics = {str(row["name"]): row for row in semantics}
            if set(stored_files) != set(current_files) or set(stored_semantics) != set(
                current_semantics
            ):
                raise IdentityInstrumentV21RepairError(
                    "existing repair proof set changed"
                )
            if any(
                str(stored_files[role]["actual_sha256"])
                != str(row["actual_sha256"])
                or int(stored_files[role]["matched"]) != 1
                for role, row in current_files.items()
            ) or any(
                str(stored_semantics[name]["actual_sha256"])
                != str(row["actual_sha256"])
                or int(stored_semantics[name]["matched"]) != 1
                for name, row in current_semantics.items()
            ):
                raise IdentityInstrumentV21RepairError(
                    "existing repair proof value changed"
                )
            if enforce_production_contract and (
                excluded_existing != (EXPECTED_EXCLUDED_SAMPLE,)
                or int(
                    connection.execute("SELECT count(*) FROM asset_recovery").fetchone()[0]
                )
                != 17
                or int(
                    connection.execute("SELECT coalesce(sum(recovered),0) FROM asset_recovery")
                    .fetchone()[0]
                )
                != 17
                or int(
                    connection.execute("SELECT count(*) FROM asset_completeness").fetchone()[0]
                )
                != 48
            ):
                raise IdentityInstrumentV21RepairError(
                    "existing production repair overlay changed"
                )
            return excluded_existing
        if str(run["status"]) != "sample_frozen":
            raise IdentityInstrumentV21RepairError(
                "repair overlay requires the frozen repaired sample"
            )
        recovery, completeness, excluded = _recovery_and_completeness(
            root, repaired, original, run_id
        )
        if enforce_production_contract and (
            len(recovery) != 17
            or sum(int(row["recovered"]) for row in recovery) != 17
            or len(completeness) != 48
            or excluded != [EXPECTED_EXCLUDED_SAMPLE]
        ):
            raise IdentityInstrumentV21RepairError(
                "production recovery/completeness contract changed"
            )
        for row in files:
            payload = {"run_id": run_id, **row}
            connection.execute(
                "INSERT INTO repair_file_proofs VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    row["family"],
                    row["role"],
                    row["path"],
                    row["expected_sha256"],
                    row["actual_sha256"],
                    int(row["matched"]),
                    _record(payload),
                ),
            )
        for row in semantics:
            payload = {"run_id": run_id, **row}
            connection.execute(
                "INSERT INTO repair_semantic_proofs VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    row["name"],
                    row["expected_sha256"],
                    row["actual_sha256"],
                    int(row["matched"]),
                    _record(payload),
                ),
            )
        for row in recovery:
            connection.execute(
                "INSERT INTO asset_recovery VALUES("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
        for row in completeness:
            connection.execute(
                "INSERT INTO asset_completeness VALUES("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
        for sample_id in excluded:
            payload = {
                "run_id": run_id,
                "sample_id": sample_id,
                "reason": "at_least_one_side_has_zero_usable_assets",
                "excluded_from_gate_scoring": 1,
            }
            connection.execute(
                "INSERT INTO sample_exclusions VALUES(?,?,?,?,?)",
                (*payload.values(), _record(payload)),
            )
        repair_payload = {
            "run_id": run_id,
            "repair_version": REPAIR_VERSION,
            "repair_order_path": _path_text(root, order_path),
            "repair_order_sha256": EXPECTED_REPAIR_ORDER_SHA256,
            "original_wp26_path": _path_text(root, original26_path),
            "original_wp26_sha256": sha256_file(original26_path),
            "repaired_wp26_path": _path_text(root, repaired_path),
            "repaired_wp26_sha256": sha256_file(repaired_path),
            "projection_path": _path_text(root, projection.path),
            "projection_sha256": projection.sha256,
            "original_wp28_path": _path_text(root, original28_path),
            "original_wp28_sha256": sha256_file(original28_path),
            "base_gate_sha256": BASE_GATE_SHA256,
            "excluded_sample_ids_json": canonical_json(excluded),
            "created_at": utc_now(),
        }
        connection.execute(
            "INSERT INTO repair_contract VALUES("
            + ",".join("?" for _ in range(16))
            + ")",
            (*repair_payload.values(), _record(repair_payload)),
        )
        _insert_repair_event(
            connection,
            run_id,
            "source_projection_frozen",
            {
                "projection_sha256": projection.sha256,
                "packet_binding_sha256": projection.packet_binding_sha256,
                "visual_inventory_sha256": projection.visual_inventory_sha256,
                "instrument_files_unchanged": True,
            },
        )
        _insert_repair_event(
            connection,
            run_id,
            "completeness_frozen",
            {
                "recovery_rows": len(recovery),
                "recovered": sum(int(row["recovered"]) for row in recovery),
                "side_rows": len(completeness),
                "excluded_sample_ids": excluded,
            },
        )
        connection.commit()
        return tuple(excluded)
    except Exception:
        connection.rollback()
        raise
    finally:
        repaired.close()
        original.close()
        connection.close()


_GATE_RATIOS: Mapping[str, tuple[int, int, str]] = {
    "d_image_same_object": (14, 15, "15 * actual >= 14 * denominator"),
    "d_scope_same": (14, 15, "15 * actual >= 14 * denominator"),
    "n100_image_non_same_object": (1, 2, "2 * actual > denominator"),
    "p1_non_same_label": (2, 3, "3 * actual >= 2 * denominator"),
}


def conservative_threshold(predicate: str, denominator: int) -> int:
    """Return the least integer that satisfies the frozen ratio predicate."""
    if denominator < 0:
        raise IdentityInstrumentV21RepairError("gate denominator cannot be negative")
    if predicate in {"d_image_same_object", "d_scope_same"}:
        return math.ceil(14 * denominator / 15)
    if predicate == "n100_image_non_same_object":
        return denominator // 2 + 1
    if predicate == "p1_non_same_label":
        return math.ceil(2 * denominator / 3)
    raise IdentityInstrumentV21RepairError(f"unknown gate predicate: {predicate}")


def _effective_gate_semantic(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "repair_version": REPAIR_VERSION,
        "base_gate_sha256": BASE_GATE_SHA256,
        "combiner": "all",
        "predicates": [
            {
                key: row[key]
                for key in (
                    "predicate",
                    "original_denominator",
                    "original_threshold",
                    "effective_denominator",
                    "effective_threshold",
                    "ratio_numerator",
                    "ratio_denominator",
                    "rule",
                    "excluded_sample_ids_json",
                )
            }
            for row in sorted(rows, key=lambda value: str(value["predicate"]))
        ],
    }


def freeze_effective_gate(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
    projection_path: Path = DEFAULT_PROJECTION_DB,
    enforce_production_contract: bool = True,
) -> dict[str, Any]:
    """Freeze denominator exclusions after the base gate and before actor forms."""
    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    projection_file = _path(root, projection_path)
    connection = sqlite3.connect(artifact)
    connection.row_factory = sqlite3.Row
    source: sqlite3.Connection | None = None
    try:
        run = _one_run(connection)
        run_id, status = str(run["run_id"]), str(run["status"])
        existing = connection.execute(
            "SELECT * FROM effective_gate_contract WHERE run_id=? ORDER BY predicate",
            (run_id,),
        ).fetchall()
        if existing:
            semantic = _effective_gate_semantic([dict(row) for row in existing])
            gate_sha = canonical_sha256(semantic)
            authority = connection.execute(
                "SELECT * FROM score_authority WHERE run_id=?", (run_id,)
            ).fetchone()
            if authority is None or (
                str(authority["base_gate_sha256"]) != BASE_GATE_SHA256
                or str(authority["core_score_role"])
                != "counterfactual_unadjusted_raw"
                or int(authority["core_decision_authority"]) != 0
                or str(authority["effective_gate_sha256"]) != gate_sha
                or str(authority["effective_score_role"]) != "wp28r_authoritative"
                or int(authority["effective_decision_authority"]) != 1
            ):
                raise IdentityInstrumentV21RepairError(
                    "score authority recovery mismatch"
                )
            return {**semantic, "effective_gate_sha256": gate_sha}
        if status != "criteria_frozen":
            raise IdentityInstrumentV21RepairError(
                "effective gate requires the predeclared base criteria"
            )
        gate = connection.execute(
            "SELECT * FROM gate_contract WHERE run_id=?", (run_id,)
        ).fetchone()
        if gate is None or str(gate["gate_sha256"]) != BASE_GATE_SHA256:
            raise IdentityInstrumentV21RepairError("WP2.8 base gate SHA changed")
        late_counts = {
            "forms": int(
                connection.execute(
                    "SELECT count(*) FROM artifacts WHERE role IN "
                    "('r1_form','r2_form','adjudication_packet','adjudication_form')"
                ).fetchone()[0]
            ),
            "reviews": int(
                connection.execute("SELECT count(*) FROM review_observations").fetchone()[0]
            ),
            "adjudications": int(
                connection.execute(
                    "SELECT count(*) FROM adjudicated_observations"
                ).fetchone()[0]
            ),
        }
        if any(late_counts.values()):
            raise IdentityInstrumentV21RepairError(
                "effective denominator declaration is too late"
            )
        exclusions = {
            str(row[0])
            for row in connection.execute(
                "SELECT sample_id FROM sample_exclusions "
                "WHERE run_id=? AND excluded_from_gate_scoring=1",
                (run_id,),
            )
        }
        source = _open_ro(projection_file)
        groups = {
            str(row["sample_id"]): str(row["group_code"])
            for row in source.execute(
                "SELECT sample_id,group_code FROM control_reveals ORDER BY sample_id"
            )
        }
        if len(groups) != 24:
            raise IdentityInstrumentV21RepairError("control group mapping must contain 24 rows")
        # Group ownership is the frozen reveal mapping.  ``sample_pairs.stratum``
        # is intentionally never consulted (all repaired WP2.6 rows say probe).
        d_ids = {sample_id for sample_id, group in groups.items() if group == "D"}
        p1_ids = {sample_id for sample_id, group in groups.items() if group == "P1"}
        eligible = set(json.loads(str(gate["eligible_sample_ids_json"])))
        original = {
            "d_image_same_object": (len(d_ids), int(gate["d_image_min"])),
            "d_scope_same": (len(d_ids), int(gate["d_scope_min"])),
            "n100_image_non_same_object": (
                len(eligible),
                int(gate["n100_image_non_same_min"]),
            ),
            "p1_non_same_label": (len(p1_ids), int(gate["p1_min_non_same"])),
        }
        populations = {
            "d_image_same_object": d_ids - exclusions,
            "d_scope_same": d_ids - exclusions,
            "n100_image_non_same_object": eligible - exclusions,
            "p1_non_same_label": p1_ids - exclusions,
        }
        expected_original = {
            "d_image_same_object": (15, 14),
            "d_scope_same": (15, 14),
            "n100_image_non_same_object": (5, 3),
            "p1_non_same_label": (3, 2),
        }
        expected_effective = {
            "d_image_same_object": (15, 14),
            "d_scope_same": (15, 14),
            "n100_image_non_same_object": (4, 3),
            "p1_non_same_label": (2, 2),
        }
        if enforce_production_contract and (
            original != expected_original
            or exclusions != {EXPECTED_EXCLUDED_SAMPLE}
            or EXPECTED_EXCLUDED_SAMPLE not in eligible
            or EXPECTED_EXCLUDED_SAMPLE not in p1_ids
        ):
            raise IdentityInstrumentV21RepairError(
                "production denominator/exclusion ownership changed"
            )
        declared_at = utc_now()
        rows: list[dict[str, Any]] = []
        for predicate in sorted(_GATE_RATIOS):
            numerator, denominator, rule = _GATE_RATIOS[predicate]
            original_n, original_threshold = original[predicate]
            effective_n = len(populations[predicate])
            payload = {
                "run_id": run_id,
                "predicate": predicate,
                "base_gate_sha256": BASE_GATE_SHA256,
                "original_denominator": original_n,
                "original_threshold": original_threshold,
                "effective_denominator": effective_n,
                "effective_threshold": conservative_threshold(predicate, effective_n),
                "ratio_numerator": numerator,
                "ratio_denominator": denominator,
                "rule": rule,
                "excluded_sample_ids_json": canonical_json(
                    sorted(exclusions & ({
                        "d_image_same_object": d_ids,
                        "d_scope_same": d_ids,
                        "n100_image_non_same_object": eligible,
                        "p1_non_same_label": p1_ids,
                    }[predicate]))
                ),
                "declared_at": declared_at,
            }
            rows.append(payload)
        actual_effective = {
            row["predicate"]: (
                int(row["effective_denominator"]),
                int(row["effective_threshold"]),
            )
            for row in rows
        }
        if enforce_production_contract and actual_effective != expected_effective:
            raise IdentityInstrumentV21RepairError(
                "production effective gate denominators changed"
            )
        for row in rows:
            connection.execute(
                "INSERT INTO effective_gate_contract VALUES("
                + ",".join("?" for _ in range(13))
                + ")",
                (*row.values(), _record(row)),
            )
        semantic = _effective_gate_semantic(rows)
        gate_sha = canonical_sha256(semantic)
        authority_payload = {
            "run_id": run_id,
            "base_gate_sha256": BASE_GATE_SHA256,
            "core_score_role": "counterfactual_unadjusted_raw",
            "core_decision_authority": 0,
            "effective_gate_sha256": gate_sha,
            "effective_score_role": "wp28r_authoritative",
            "effective_decision_authority": 1,
            "excluded_sample_ids_json": canonical_json(sorted(exclusions)),
            "declared_at": declared_at,
        }
        connection.execute(
            "INSERT INTO score_authority VALUES(?,?,?,?,?,?,?,?,?,?)",
            (*authority_payload.values(), _record(authority_payload)),
        )
        _insert_repair_event(
            connection,
            run_id,
            "effective_gate_frozen",
            {
                "base_gate_sha256": BASE_GATE_SHA256,
                "effective_gate_sha256": gate_sha,
                "original": expected_original if enforce_production_contract else original,
                "effective": actual_effective,
                "score_authority": {
                    "core": "counterfactual_unadjusted_raw",
                    "effective": "wp28r_authoritative",
                },
                "late_counts": late_counts,
            },
        )
        connection.commit()
        return {**semantic, "effective_gate_sha256": gate_sha}
    except Exception:
        connection.rollback()
        raise
    finally:
        if source is not None:
            source.close()
        connection.close()


def initialize_repair(
    *,
    repo_root: Path,
    quota_remaining_percent: float,
    quota_observed_at: str,
    quota_source: str,
    quota_account_index: int = 0,
    quota_limitation: str = "",
    repaired_source_db_path: Path = DEFAULT_REPAIRED_SOURCE_DB,
    repaired_sample_manifest_path: Path = DEFAULT_REPAIRED_SAMPLE_MANIFEST,
    repaired_fetch_manifest_path: Path = DEFAULT_REPAIRED_FETCH_MANIFEST,
    repaired_fetch_report_path: Path = DEFAULT_REPAIRED_FETCH_REPORT,
    projection_path: Path = DEFAULT_PROJECTION_DB,
    projection_manifest_path: Path = DEFAULT_PROJECTION_MANIFEST,
    artifact_path: Path = DEFAULT_OUTPUT,
    sample_manifest_path: Path = DEFAULT_SAMPLE_MANIFEST,
    enforce_production_contract: bool = True,
) -> InitializeResult:
    """Run Stage B and freeze Stage C criteria without creating actor forms."""
    root = repo_root.resolve()
    _assert_no_path_collisions(
        {
            "repaired_wp26": _path(root, repaired_source_db_path),
            "repaired_sample_manifest": _path(root, repaired_sample_manifest_path),
            "repaired_fetch_manifest": _path(root, repaired_fetch_manifest_path),
            "repaired_fetch_report": _path(root, repaired_fetch_report_path),
            "original_wp26": _path(root, DEFAULT_ORIGINAL_WP26_DB),
            "original_wp27": _path(root, DEFAULT_ORIGINAL_WP27_DB),
            "original_wp28": _path(root, DEFAULT_ORIGINAL_WP28_DB),
            "n100_source": _path(root, v21.DEFAULT_N100_DB),
            "projection": _path(root, projection_path),
            "projection_manifest": _path(root, projection_manifest_path),
            "artifact": _path(root, artifact_path),
            "sample_manifest": _path(root, sample_manifest_path),
        }
    )
    assert_frozen_contract(root)
    projection = build_source_projection(
        repo_root=root,
        repaired_source_db_path=repaired_source_db_path,
        projection_path=projection_path,
        manifest_path=projection_manifest_path,
        enforce_production_contract=enforce_production_contract,
    )
    with _repaired_source_context(projection):
        v21.extract_vocabulary(
            repo_root=root,
            quota_remaining_percent=quota_remaining_percent,
            quota_observed_at=quota_observed_at,
            quota_source=quota_source,
            quota_account_index=quota_account_index,
            quota_limitation=quota_limitation,
            order_path=FROZEN_INSTRUMENT_FILES["wp28_order"][0],
            output_path=artifact_path,
            enforce_source_contract=enforce_production_contract,
        )
        v21.verify_derivation(
            repo_root=root,
            artifact_path=artifact_path,
            enforce_expected_replay=enforce_production_contract,
        )
        v21.freeze_instrument(
            repo_root=root,
            protocol_path=FROZEN_INSTRUMENT_FILES["v21_protocol"][0],
            source_db_path=projection_path,
            source_report_path=repaired_fetch_report_path,
            source_manifest_path=repaired_fetch_manifest_path,
            source_validation_path=repaired_sample_manifest_path,
            artifact_path=artifact_path,
            enforce_source_contract=False,
        )
        v21.freeze_sample(
            repo_root=root,
            artifact_path=artifact_path,
            sample_manifest_path=sample_manifest_path,
            enforce_source_contract=False,
        )
    excluded = install_repair_overlay(
        repo_root=root,
        artifact_path=artifact_path,
        projection=projection,
        repaired_source_db_path=repaired_source_db_path,
        enforce_production_contract=enforce_production_contract,
    )
    with _repaired_source_context(projection):
        base_gate = v21.declare_gate(repo_root=root, artifact_path=artifact_path)
    if str(base_gate["gate_sha256"]) != BASE_GATE_SHA256:
        raise IdentityInstrumentV21RepairError("base gate did not replay byte-identically")
    freeze_effective_gate(
        repo_root=root,
        artifact_path=artifact_path,
        projection_path=projection_path,
        enforce_production_contract=enforce_production_contract,
    )
    connection = _open_ro(_path(root, artifact_path))
    try:
        run = _one_run(connection)
        return InitializeResult(
            artifact_path=_path(root, artifact_path),
            projection_path=projection.path,
            run_id=str(run["run_id"]),
            status=str(run["status"]),
            excluded_sample_ids=excluded,
        )
    finally:
        connection.close()


def _load_projection(root: Path, value: Path) -> ProjectionContract:
    contract = _projection_contract_from_db(_path(root, value))
    connection = _open_ro(contract.path)
    try:
        run = _one_run(connection)
        if str(run["status"]) != "projection_frozen":
            raise IdentityInstrumentV21RepairError("source projection is not frozen")
    finally:
        connection.close()
    return contract


def _assert_projection_bound(
    root: Path, artifact_path: Path, contract: ProjectionContract
) -> None:
    connection = _open_ro(_path(root, artifact_path))
    try:
        repair_row = connection.execute("SELECT * FROM repair_contract").fetchone()
        source_pin = connection.execute(
            "SELECT path,size_bytes,sha256 FROM input_pins WHERE role='wp27_source_db'"
        ).fetchone()
        if repair_row is None or source_pin is None:
            raise IdentityInstrumentV21RepairError("repair source binding is missing")
        if (
            _resolve_text(root, str(repair_row["projection_path"])) != contract.path
            or str(repair_row["projection_sha256"]) != contract.sha256
            or _resolve_text(root, str(source_pin["path"])) != contract.path
            or int(source_pin["size_bytes"]) != contract.size_bytes
            or str(source_pin["sha256"]) != contract.sha256
        ):
            raise IdentityInstrumentV21RepairError("repair source projection binding changed")
    finally:
        connection.close()


def record_quota_observation(
    *,
    repo_root: Path,
    phase: str,
    remaining_percent: float,
    source: str,
    observed_at: str,
    account_index: int = 0,
    limitation: str = "",
    artifact_path: Path = DEFAULT_OUTPUT,
    projection_path: Path = DEFAULT_PROJECTION_DB,
) -> dict[str, Any]:
    root = repo_root.resolve()
    projection = _load_projection(root, projection_path)
    _assert_projection_bound(root, artifact_path, projection)
    with _repaired_source_context(projection):
        return v21.record_quota_observation(
            repo_root=root,
            phase=phase,
            remaining_percent=remaining_percent,
            source=source,
            observed_at=observed_at,
            account_index=account_index,
            limitation=limitation,
            artifact_path=artifact_path,
        )


def prepare_review(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
    projection_path: Path = DEFAULT_PROJECTION_DB,
    r1_form_path: Path = DEFAULT_R1_FORM,
    r2_form_path: Path = DEFAULT_R2_FORM,
    fetch_manifest_path: Path = DEFAULT_FETCH_MANIFEST,
    fetch_report_path: Path = DEFAULT_FETCH_REPORT,
) -> Any:
    root = repo_root.resolve()
    projection = _load_projection(root, projection_path)
    _assert_projection_bound(root, artifact_path, projection)
    with _repaired_source_context(projection):
        return v21.prepare_review(
            repo_root=root,
            artifact_path=artifact_path,
            r1_form_path=r1_form_path,
            r2_form_path=r2_form_path,
            fetch_manifest_path=fetch_manifest_path,
            fetch_report_path=fetch_report_path,
        )


def lock_review(
    *,
    repo_root: Path,
    lane: str,
    completed_form_path: Path,
    locked_output_path: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
    projection_path: Path = DEFAULT_PROJECTION_DB,
) -> Path:
    root = repo_root.resolve()
    projection = _load_projection(root, projection_path)
    _assert_projection_bound(root, artifact_path, projection)
    with _repaired_source_context(projection):
        return v21.lock_review(
            repo_root=root,
            lane=lane,
            completed_form_path=completed_form_path,
            locked_output_path=locked_output_path,
            artifact_path=artifact_path,
        )


def prepare_adjudication(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
    projection_path: Path = DEFAULT_PROJECTION_DB,
    packet_path: Path = DEFAULT_ADJUDICATION_PACKET,
    adjudication_form_path: Path = DEFAULT_ADJUDICATION_FORM,
) -> tuple[Path, Path]:
    root = repo_root.resolve()
    projection = _load_projection(root, projection_path)
    _assert_projection_bound(root, artifact_path, projection)
    with _repaired_source_context(projection):
        return v21.prepare_adjudication(
            repo_root=root,
            artifact_path=artifact_path,
            packet_path=packet_path,
            adjudication_form_path=adjudication_form_path,
        )


def lock_adjudication(
    *,
    repo_root: Path,
    completed_form_path: Path,
    locked_output_path: Path = DEFAULT_ADJUDICATION_LOCKED,
    artifact_path: Path = DEFAULT_OUTPUT,
    projection_path: Path = DEFAULT_PROJECTION_DB,
) -> Path:
    root = repo_root.resolve()
    projection = _load_projection(root, projection_path)
    _assert_projection_bound(root, artifact_path, projection)
    with _repaired_source_context(projection):
        return v21.lock_adjudication(
            repo_root=root,
            completed_form_path=completed_form_path,
            locked_output_path=locked_output_path,
            artifact_path=artifact_path,
        )


def _effective_score(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    rows = {
        str(row["sample_id"]): row
        for row in connection.execute(
            "SELECT * FROM adjudicated_observations WHERE run_id=? ORDER BY sample_id",
            (run_id,),
        )
    }
    controls = {
        str(row["sample_id"]): row
        for row in connection.execute(
            "SELECT * FROM control_reveals WHERE run_id=? ORDER BY sample_id", (run_id,)
        )
    }
    if len(rows) != 24 or len(controls) != 24 or set(rows) != set(controls):
        raise IdentityInstrumentV21RepairError("terminal observations/reveals are incomplete")
    exclusions = {
        str(row[0])
        for row in connection.execute(
            "SELECT sample_id FROM sample_exclusions "
            "WHERE run_id=? AND excluded_from_gate_scoring=1",
            (run_id,),
        )
    }
    gate = connection.execute(
        "SELECT eligible_sample_ids_json FROM gate_contract WHERE run_id=?", (run_id,)
    ).fetchone()
    eligible = set(json.loads(str(gate[0]))) - exclusions
    populations = {
        "d_image_same_object": {
            sample_id
            for sample_id, control in controls.items()
            if str(control["group_code"]) == "D" and sample_id not in exclusions
        },
        "d_scope_same": {
            sample_id
            for sample_id, control in controls.items()
            if str(control["group_code"]) == "D" and sample_id not in exclusions
        },
        "n100_image_non_same_object": eligible,
        "p1_non_same_label": {
            sample_id
            for sample_id, control in controls.items()
            if str(control["group_code"]) == "P1" and sample_id not in exclusions
        },
    }
    actual = {
        "d_image_same_object": sum(
            str(rows[sample_id]["image"]) == "same_object"
            for sample_id in populations["d_image_same_object"]
        ),
        "d_scope_same": sum(
            str(rows[sample_id]["scope"]) == "same"
            for sample_id in populations["d_scope_same"]
        ),
        "n100_image_non_same_object": sum(
            str(rows[sample_id]["image"]) != "same_object"
            for sample_id in populations["n100_image_non_same_object"]
        ),
        "p1_non_same_label": sum(
            str(rows[sample_id]["derived_label"]) != "same"
            for sample_id in populations["p1_non_same_label"]
        ),
    }
    contracts = {
        str(row["predicate"]): row
        for row in connection.execute(
            "SELECT * FROM effective_gate_contract WHERE run_id=? ORDER BY predicate",
            (run_id,),
        )
    }
    if set(contracts) != set(_GATE_RATIOS):
        raise IdentityInstrumentV21RepairError("effective gate contract is incomplete")
    predicates: dict[str, dict[str, Any]] = {}
    for predicate in sorted(contracts):
        contract = contracts[predicate]
        n = len(populations[predicate])
        threshold = conservative_threshold(predicate, n)
        if (
            n != int(contract["effective_denominator"])
            or threshold != int(contract["effective_threshold"])
        ):
            raise IdentityInstrumentV21RepairError("effective gate population drifted")
        value = int(actual[predicate])
        if predicate in {"d_image_same_object", "d_scope_same"}:
            passed = 15 * value >= 14 * n
        elif predicate == "n100_image_non_same_object":
            passed = 2 * value > n
        else:
            passed = 3 * value >= 2 * n
        predicates[predicate] = {
            "actual": value,
            "denominator": n,
            "threshold": threshold,
            "passed": bool(passed),
            "sample_ids": sorted(populations[predicate]),
        }
    status = "pass" if all(row["passed"] for row in predicates.values()) else "fail"
    semantic = _effective_gate_semantic([dict(row) for row in contracts.values()])
    return {
        "status": status,
        "effective_gate_sha256": canonical_sha256(semantic),
        "predicates": predicates,
    }


def _row_changes(
    old: sqlite3.Connection, current: sqlite3.Connection, run_id: str
) -> list[dict[str, Any]]:
    old_rows = {
        str(row["sample_id"]): row
        for row in old.execute("SELECT * FROM adjudicated_observations ORDER BY sample_id")
    }
    new_rows = {
        str(row["sample_id"]): row
        for row in current.execute(
            "SELECT * FROM adjudicated_observations WHERE run_id=? ORDER BY sample_id",
            (run_id,),
        )
    }
    if len(old_rows) != 24 or set(old_rows) != set(new_rows):
        raise IdentityInstrumentV21RepairError("WP2.8/WP2.8-R row sets differ")
    result = []
    for sample_id in sorted(new_rows):
        before = {
            key: str(old_rows[sample_id][key]) for key in v21.OBSERVATION_KEYS
        }
        after = {key: str(new_rows[sample_id][key]) for key in v21.OBSERVATION_KEYS}
        changed = [key for key in v21.OBSERVATION_KEYS if before[key] != after[key]]
        result.append(
            {
                "sample_id": sample_id,
                "wp28": {
                    "observation": before,
                    "derived_label": str(old_rows[sample_id]["derived_label"]),
                },
                "wp28r": {
                    "observation": after,
                    "derived_label": str(new_rows[sample_id]["derived_label"]),
                },
                "changed_axes": changed,
                "derived_label_changed": str(old_rows[sample_id]["derived_label"])
                != str(new_rows[sample_id]["derived_label"]),
            }
        )
    return result


def _dict_rows(
    connection: sqlite3.Connection, query: str, parameters: Sequence[Any] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, parameters)]


def _outer_payloads(
    *,
    root: Path,
    artifact: Path,
    projection: ProjectionContract,
    report: Path,
    core_report: Path,
    core_manifest: Path,
    score: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = _open_ro(artifact)
    old: sqlite3.Connection | None = None
    try:
        run = _one_run(connection)
        run_id = str(run["run_id"])
        repair = connection.execute(
            "SELECT * FROM repair_contract WHERE run_id=?", (run_id,)
        ).fetchone()
        if repair is None:
            raise IdentityInstrumentV21RepairError("repair contract is missing")
        old_path = _resolve_text(root, str(repair["original_wp28_path"]))
        old = _open_ro(old_path)
        changes = _row_changes(old, connection, run_id)
        recoveries = _dict_rows(
            connection,
            "SELECT * FROM asset_recovery WHERE run_id=? ORDER BY asset_key",
            (run_id,),
        )
        completeness = _dict_rows(
            connection,
            "SELECT * FROM asset_completeness WHERE run_id=? ORDER BY sample_id,side",
            (run_id,),
        )
        exclusions = _dict_rows(
            connection,
            "SELECT * FROM sample_exclusions WHERE run_id=? ORDER BY sample_id",
            (run_id,),
        )
        effective_contract = _dict_rows(
            connection,
            "SELECT * FROM effective_gate_contract WHERE run_id=? ORDER BY predicate",
            (run_id,),
        )
        score_authority_row = connection.execute(
            "SELECT * FROM score_authority WHERE run_id=?", (run_id,)
        ).fetchone()
        if score_authority_row is None:
            raise IdentityInstrumentV21RepairError("score authority is missing")
        score_authority = dict(score_authority_row)
        base_gate = connection.execute(
            "SELECT * FROM gate_contract WHERE run_id=?", (run_id,)
        ).fetchone()
        base_scores = {
            str(row["predicate"]): {
                "actual": int(row["actual"]),
                "denominator": int(row["denominator"]),
                "threshold": int(row["threshold"]),
                "passed": bool(row["passed"]),
            }
            for row in connection.execute(
                "SELECT * FROM gate_scores WHERE run_id=? ORDER BY predicate", (run_id,)
            )
        }
        file_proofs = _dict_rows(
            connection,
            "SELECT * FROM repair_file_proofs WHERE run_id=? ORDER BY family,role",
            (run_id,),
        )
        live_files = {row["role"]: row for row in _file_proofs(root)}
        for row in file_proofs:
            row["current_sha256"] = live_files[str(row["role"])]["actual_sha256"]
            row["still_matched"] = bool(
                row["current_sha256"] == row["expected_sha256"] == row["actual_sha256"]
            )
        semantic_proofs = _dict_rows(
            connection,
            "SELECT * FROM repair_semantic_proofs WHERE run_id=? ORDER BY name",
            (run_id,),
        )
        live_semantics = {row["name"]: row for row in _semantic_proofs()}
        for row in semantic_proofs:
            row["current_sha256"] = live_semantics[str(row["name"])]["actual_sha256"]
            row["still_matched"] = bool(
                row["current_sha256"] == row["expected_sha256"] == row["actual_sha256"]
            )
        original_pins = {
            "wp26": {
                "path": str(repair["original_wp26_path"]),
                "expected_sha256": str(repair["original_wp26_sha256"]),
                "current_sha256": sha256_file(
                    _resolve_text(root, str(repair["original_wp26_path"]))
                ),
            },
            "wp28": {
                "path": str(repair["original_wp28_path"]),
                "expected_sha256": str(repair["original_wp28_sha256"]),
                "current_sha256": sha256_file(old_path),
            },
            "repaired_wp26": {
                "path": str(repair["repaired_wp26_path"]),
                "expected_sha256": str(repair["repaired_wp26_sha256"]),
                "current_sha256": sha256_file(
                    _resolve_text(root, str(repair["repaired_wp26_path"]))
                ),
            },
            "source_projection": {
                "path": str(repair["projection_path"]),
                "expected_sha256": str(repair["projection_sha256"]),
                "current_sha256": sha256_file(
                    _resolve_text(root, str(repair["projection_path"]))
                ),
            },
        }
        for pin in original_pins.values():
            pin["unchanged"] = pin["expected_sha256"] == pin["current_sha256"]
        if not all(row["still_matched"] for row in file_proofs + semantic_proofs):
            raise IdentityInstrumentV21RepairError("instrument proof changed after review")
        if not all(pin["unchanged"] for pin in original_pins.values()):
            raise IdentityInstrumentV21RepairError("protected source artifact changed")
        quota = _dict_rows(
            connection,
            "SELECT phase,account_index,observed_at,source,remaining_percent,"
            "start_allowed,stop_required,limitation FROM quota_observations "
            "WHERE run_id=? ORDER BY observation_no",
            (run_id,),
        )
        state_events = _dict_rows(
            connection,
            "SELECT event_no,from_status,to_status,occurred_at FROM state_events "
            "WHERE run_id=? ORDER BY event_no",
            (run_id,),
        )
        review_counts = {
            "observations": int(
                connection.execute(
                    "SELECT count(*) FROM review_observations WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            ),
            "review_locks": int(
                connection.execute(
                    "SELECT count(*) FROM review_locks WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            ),
            "adjudicated": int(
                connection.execute(
                    "SELECT count(*) FROM adjudicated_observations WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            ),
        }
        repair_cache_row = connection.execute(
            "SELECT role,path,file_count_before,total_bytes_before,tree_sha256_before,"
            "file_count_after,total_bytes_after,tree_sha256_after "
            "FROM source_cache_pins WHERE run_id=? AND role='wp25_output'",
            (run_id,),
        ).fetchone()
        repair_cache = dict(repair_cache_row) if repair_cache_row is not None else None
        completed_at = str(
            connection.execute(
                "SELECT completed_at FROM repair_terminal WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        core_report_payload = json.loads(core_report.read_text(encoding="utf-8"))
        core_manifest_payload = json.loads(core_manifest.read_text(encoding="utf-8"))
        report_payload = {
            "status": str(score["status"]).upper(),
            "terminal_status": str(score["status"]),
            "run_id": run_id,
            "repair_version": REPAIR_VERSION,
            "completed_at": completed_at,
            "measurement_only": True,
            "authorizes_wp29": str(score["status"]) == "pass",
            "authorizes_tiering": False,
            "failure_is_terminal": str(score["status"]) == "fail",
            "repair_scope": "missing_packet_assets_only",
            "decision_authority": "effective_gate_only",
            "instrument_integrity": {
                "base_gate_sha256": str(base_gate["gate_sha256"]),
                "base_gate_matches_wp28": str(base_gate["gate_sha256"])
                == BASE_GATE_SHA256,
                "repair_order": {
                    "path": _path_text(root, _path(root, DEFAULT_REPAIR_ORDER)),
                    "expected_size_bytes": EXPECTED_REPAIR_ORDER_SIZE,
                    "actual_size_bytes": _path(root, DEFAULT_REPAIR_ORDER).stat().st_size,
                    "expected_sha256": EXPECTED_REPAIR_ORDER_SHA256,
                    "actual_sha256": sha256_file(_path(root, DEFAULT_REPAIR_ORDER)),
                    "matched": True,
                },
                "file_proofs": file_proofs,
                "semantic_proofs": semantic_proofs,
                "protected_original_artifacts": original_pins,
            },
            "asset_repair": {
                "inventory_rows": len(recoveries),
                "recovered_rows": sum(int(row["recovered"]) for row in recoveries),
                "network_logical": sum(int(row["request_count"] > 0) for row in recoveries),
                "network_wire": sum(int(row["request_count"]) for row in recoveries),
                "retries": 0,
                "redirects": 0,
                "repair_cache_tree": repair_cache,
                "rows": recoveries,
            },
            "asset_completeness": {
                "side_rows": len(completeness),
                "complete_side_rows": sum(int(row["usable_count"] > 0) for row in completeness),
                "excluded_side_rows": sum(int(row["excluded_no_assets"]) for row in completeness),
                "excluded_samples": [row["sample_id"] for row in exclusions],
                "rows": completeness,
            },
            "wp28_to_wp28r_changes": {
                "rows": len(changes),
                "changed_rows": sum(
                    int(bool(row["changed_axes"]) or row["derived_label_changed"])
                    for row in changes
                ),
                "comparison": changes,
            },
            "gate": {
                "combiner": "all",
                "original_contract": {
                    "gate_sha256": str(base_gate["gate_sha256"]),
                    "gate": json.loads(str(base_gate["gate_json"])),
                    "scores": base_scores,
                    "status": str(core_report_payload["status"]),
                    "score_role": "counterfactual_unadjusted_raw",
                    "decision_authority": False,
                    "note": (
                        "Immutable WP2.8 raw replay; not a WP2.8-R decision. "
                        "It intentionally retains the excluded no-assets row."
                    ),
                },
                "score_authority": score_authority,
                "authoritative_decision_source": "effective_gate_scores",
                "effective_contract_sha256": str(score["effective_gate_sha256"]),
                "effective_contract": effective_contract,
                "effective_predicates": score["predicates"],
                "effective_status": str(score["status"]),
            },
            "blindness": {
                "state_events": state_events,
                "base_sequence_preserved": True,
                "reviews_before_reveal": review_counts,
                "actors_distinct": bool(core_report_payload["review"]["actors_distinct"]),
                "control_reveal_rows": 24,
                "single_reveal": True,
            },
            "quota_observations": quota,
            "source_projection": {
                "path": _path_text(root, projection.path),
                "size_bytes": projection.size_bytes,
                "sha256": projection.sha256,
                "packet_binding_sha256": projection.packet_binding_sha256,
                "visual_inventory_sha256": projection.visual_inventory_sha256,
            },
            "core_terminal": {
                "status": str(core_report_payload["status"]),
                "report_path": _path_text(root, core_report),
                "report_sha256": sha256_file(core_report),
                "manifest_path": _path_text(root, core_manifest),
                "manifest_sha256": sha256_file(core_manifest),
                "manifest_status": str(core_manifest_payload["status"]),
            },
            "incremental_instrument_network": {
                "logical": 0,
                "wire": 0,
                "retries": 0,
                "redirects": 0,
            },
            "vision_requests": 0,
            "llm_requests": 0,
            "paid_api_requests": 0,
            "merge_executions": 0,
            "vnext_id_issuances": 0,
            "tier_promotions": 0,
            "neon_writes": 0,
            "r2_writes": 0,
        }
        report_raw = _json_bytes(report_payload)
        manifest_payload = {
            "status": report_payload["status"],
            "terminal_status": report_payload["terminal_status"],
            "run_id": run_id,
            "repair_version": REPAIR_VERSION,
            "completed_at": completed_at,
            "artifacts": {
                "database": {
                    "path": _path_text(root, artifact),
                    "size_bytes": artifact.stat().st_size,
                    "sha256": sha256_file(artifact),
                },
                "source_projection": report_payload["source_projection"],
                "core_report": {
                    "path": _path_text(root, core_report),
                    "size_bytes": core_report.stat().st_size,
                    "sha256": sha256_file(core_report),
                },
                "core_manifest": {
                    "path": _path_text(root, core_manifest),
                    "size_bytes": core_manifest.stat().st_size,
                    "sha256": sha256_file(core_manifest),
                },
                "report": {
                    "path": _path_text(root, report),
                    "size_bytes": len(report_raw),
                    "sha256": hashlib.sha256(report_raw).hexdigest(),
                },
            },
            "counts": {
                "samples": 24,
                "sheets": 48,
                "asset_occurrences": 93,
                "unique_assets": 93,
                "repair_inventory": len(recoveries),
                "recovered_assets": sum(int(row["recovered"]) for row in recoveries),
                "side_completeness_rows": len(completeness),
                "excluded_samples": len(exclusions),
            },
            "gate": {
                "base_gate_sha256": BASE_GATE_SHA256,
                "effective_gate_sha256": str(score["effective_gate_sha256"]),
                "score_authority": score_authority,
                "authoritative_decision_source": "effective_gate_scores",
                "predicates": score["predicates"],
            },
            "network": {
                "repair_logical": report_payload["asset_repair"]["network_logical"],
                "repair_wire": report_payload["asset_repair"]["network_wire"],
                "instrument_incremental_logical": 0,
                "instrument_incremental_wire": 0,
                "retries": 0,
                "redirects": 0,
            },
            "vision_requests": 0,
            "llm_requests": 0,
            "paid_api_requests": 0,
            "merge_executions": 0,
            "vnext_id_issuances": 0,
            "tier_promotions": 0,
            "neon_writes": 0,
            "r2_writes": 0,
        }
        return report_payload, manifest_payload
    finally:
        if old is not None:
            old.close()
        connection.close()


def reveal_score(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
    projection_path: Path = DEFAULT_PROJECTION_DB,
    core_report_path: Path = DEFAULT_CORE_REPORT,
    core_manifest_path: Path = DEFAULT_CORE_MANIFEST,
    report_path: Path = DEFAULT_REPORT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> RepairRevealResult:
    """Perform the one allowed reveal, then score the frozen effective gate."""
    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    core_report = _path(root, core_report_path)
    core_manifest = _path(root, core_manifest_path)
    report = _path(root, report_path)
    manifest = _path(root, manifest_path)
    _assert_no_path_collisions(
        {
            "database": artifact,
            "source_projection": _path(root, projection_path),
            "core_report": core_report,
            "core_manifest": core_manifest,
            "report": report,
            "manifest": manifest,
        }
    )
    projection = _load_projection(root, projection_path)
    _assert_projection_bound(root, artifact_path, projection)
    assert_frozen_contract(root)
    with _repaired_source_context(projection):
        core = v21.reveal_score(
            repo_root=root,
            artifact_path=artifact_path,
            report_path=core_report_path,
            manifest_path=core_manifest_path,
        )
    connection = sqlite3.connect(artifact)
    connection.row_factory = sqlite3.Row
    try:
        run = _one_run(connection)
        run_id = str(run["run_id"])
        terminal = connection.execute(
            "SELECT * FROM repair_terminal WHERE run_id=?", (run_id,)
        ).fetchone()
        if terminal is None:
            if report.exists() or manifest.exists():
                raise IdentityInstrumentV21RepairError(
                    "outer artifacts predate the repair terminal"
                )
            score = _effective_score(connection, run_id)
            for predicate, row in sorted(score["predicates"].items()):
                payload = {
                    "run_id": run_id,
                    "predicate": predicate,
                    "actual": int(row["actual"]),
                    "denominator": int(row["denominator"]),
                    "threshold": int(row["threshold"]),
                    "passed": int(row["passed"]),
                }
                connection.execute(
                    "INSERT INTO effective_gate_scores VALUES(?,?,?,?,?,?,?)",
                    (*payload.values(), _record(payload)),
                )
            completed_at = utc_now()
            terminal_payload = {
                "run_id": run_id,
                "status": str(score["status"]),
                "base_status": str(core.status).lower(),
                "effective_gate_sha256": str(score["effective_gate_sha256"]),
                "completed_at": completed_at,
            }
            connection.execute(
                "INSERT INTO repair_terminal VALUES(?,?,?,?,?,?)",
                (*terminal_payload.values(), _record(terminal_payload)),
            )
            _insert_repair_event(
                connection,
                run_id,
                "effective_terminal_scored",
                {
                    "status": score["status"],
                    "base_status": str(core.status).lower(),
                    "effective_gate_sha256": score["effective_gate_sha256"],
                    "predicates": score["predicates"],
                    "retry_allowed": False,
                },
            )
            connection.commit()
        else:
            score = _effective_score(connection, run_id)
            if (
                str(terminal["status"]) != str(score["status"])
                or str(terminal["effective_gate_sha256"])
                != str(score["effective_gate_sha256"])
            ):
                raise IdentityInstrumentV21RepairError("repair terminal recovery mismatch")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    report_payload, manifest_payload = _outer_payloads(
        root=root,
        artifact=artifact,
        projection=projection,
        report=report,
        core_report=core_report,
        core_manifest=core_manifest,
        score=score,
    )
    report_raw, manifest_raw = _json_bytes(report_payload), _json_bytes(manifest_payload)
    if report.exists() and (not report.is_file() or report.read_bytes() != report_raw):
        raise IdentityInstrumentV21RepairError("outer report recovery payload changed")
    if manifest.exists() and (
        not manifest.is_file() or manifest.read_bytes() != manifest_raw
    ):
        raise IdentityInstrumentV21RepairError("outer manifest recovery payload changed")
    if not report.exists():
        _atomic_json(report, report_payload)
    if not manifest.exists():
        _atomic_json(manifest, manifest_payload)
    return RepairRevealResult(
        artifact_path=artifact,
        report_path=report,
        manifest_path=manifest,
        run_id=str(report_payload["run_id"]),
        status=str(report_payload["status"]),
    )


__all__ = [
    "BASE_GATE_SHA256",
    "DEFAULT_ADJUDICATION_FORM",
    "DEFAULT_ADJUDICATION_LOCKED",
    "DEFAULT_ADJUDICATION_PACKET",
    "DEFAULT_CORE_MANIFEST",
    "DEFAULT_CORE_REPORT",
    "DEFAULT_FETCH_MANIFEST",
    "DEFAULT_FETCH_REPORT",
    "DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT",
    "DEFAULT_PROJECTION_DB",
    "DEFAULT_PROJECTION_MANIFEST",
    "DEFAULT_R1_FORM",
    "DEFAULT_R1_LOCKED",
    "DEFAULT_R2_FORM",
    "DEFAULT_R2_LOCKED",
    "DEFAULT_REPORT",
    "DEFAULT_SAMPLE_MANIFEST",
    "EXPECTED_REPAIR_ORDER_SHA256",
    "EXPECTED_REPAIR_ORDER_SIZE",
    "IdentityInstrumentV21RepairError",
    "InitializeResult",
    "ProjectionContract",
    "RepairRevealResult",
    "assert_frozen_contract",
    "build_source_projection",
    "conservative_threshold",
    "freeze_effective_gate",
    "initialize_repair",
    "lock_adjudication",
    "lock_review",
    "prepare_adjudication",
    "prepare_review",
    "record_quota_observation",
    "reveal_score",
]
