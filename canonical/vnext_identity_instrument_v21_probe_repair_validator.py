"""Independent, fail-closed validator for the WP2.8-R repair probe.

The repair producer and every frozen WP2.5/WP2.6/WP2.8 module are deliberately
not imported.  This module reconstructs the source, instrument, blindness, and
gate contracts from pinned artifacts with only the Python standard library.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence


VALIDATOR_VERSION = "vnext-identity-instrument-v21-probe-repair-independent-validator-v1"
APPLICATION_ID = 0x57503238  # frozen WP2.8 core container
USER_VERSION = 1

TOTAL_SAMPLE = 24
TOTAL_SHEETS = 48
TOTAL_ASSETS = 93
RECOVERY_ASSETS = 17
MAX_ASSETS_PER_SIDE = 2
GROUP_COUNTS = {"D": 15, "P1": 3, "P2": 4, "P3": 2}
USABLE_FETCH_STATUSES = frozenset(
    {"cache_hit", "exact", "delivery_changed", "source_changed"}
)
EXPECTED_EXCLUDED_SAMPLE = "probe_440b72d0af93f2c5b0fbd38e"
REPAIR_VERSION = "vnext-identity-instrument-v21-packet-repair-v2"
PROJECTION_VERSION = "vnext-identity-instrument-v21-repaired-source-v1"
CONTROLLED_KEYS = ("image", "name", "firm", "place", "year", "scope")
OBSERVATION_KEYS = (*CONTROLLED_KEYS, "basis")
CONTROLLED_VALUES: dict[str, tuple[str, ...]] = {
    "image": ("same_object", "different_objects", "inconclusive", "unavailable"),
    "name": ("agree", "alias", "conflict", "unclear"),
    "firm": ("agree", "overlap", "conflict", "unclear"),
    "place": ("agree", "conflict", "unclear"),
    "year": ("compatible", "conflict", "unclear"),
    "scope": ("same", "related_separate", "unclear", "unrelated"),
}
EXPECTED_UNCERTAIN_N100_IDS = frozenset({
    "n100_2c6e8b236183ac67d52480e6",
    "n100_3173cc9314cd34bdab869d1c",
    "n100_1809907a641b07169a0d2436",
    "n100_3fde31b883d60c402c002057",
    "n100_215d784c23c71801b8742720",
})

ORIGINAL_GATE_SHA256 = "a5adf46a63c5ce141f528be269a041f1ae9c492d4d6007afc42e6f8eed9ac18b"
ORIGINAL_INSTRUMENT_VERSION = "vnext-identity-instrument-v21"
ORIGINAL_SAMPLE_VERSION = "vnext-identity-instrument-v21-probe-v1"
REPAIR_SAMPLE_VERSION = "vnext-identity-instrument-v21-probe-v2"
SOURCE_RUN_ID = "wp26-250a8cefe5396b87377ad55d"
SOURCE_SAMPLE_VERSION = "vnext-identity-instrument-probe-v1"
SOURCE_SEED = "vnext-wp26-instrument-probe-v1-20260827"

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_instrument_v21_probe_v2.db")
DEFAULT_SOURCE_V1 = Path("data/enrichment/vnext_identity_instrument_probe_v1.db")
DEFAULT_SOURCE_V2 = Path("data/enrichment/vnext_identity_instrument_probe_v2.db")
DEFAULT_COMPAT_SOURCE = Path("data/enrichment/vnext_identity_instrument_v21_probe_v2_source.db")
DEFAULT_PROJECTION_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_source_manifest.json"
)
DEFAULT_WP28_DB = Path("data/enrichment/vnext_identity_instrument_v21_probe_v1.db")
DEFAULT_WP27_DB = Path("data/enrichment/vnext_identity_instrument_v2_probe_v1.db")
DEFAULT_WP28_REPORT = Path("data/reports/vnext_identity_instrument_v21_probe_v1_report.json")
DEFAULT_WP28_MANIFEST = Path("data/reports/vnext_identity_instrument_v21_probe_v1_manifest.json")
DEFAULT_CORE_REPORT = Path("data/reports/vnext_identity_instrument_v21_probe_v2_core_report.json")
DEFAULT_CORE_MANIFEST = Path("data/reports/vnext_identity_instrument_v21_probe_v2_core_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_instrument_v21_probe_v2_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_instrument_v21_probe_v2_manifest.json")
DEFAULT_VALIDATION_REPORT = Path(
    "data/reports/vnext_identity_instrument_v21_probe_v2_validation.json"
)
DEFAULT_REPAIR_ORDER = Path(
    ".claude/ops/jobs/20260827_vnext_wp28r_packet_repair_order.md"
)
DEFAULT_FETCH_CORRECTION = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_cap_correction.json"
)

PINNED_ARTIFACTS: dict[str, tuple[Path, int, str]] = {
    "wp28r_order": (
        DEFAULT_REPAIR_ORDER,
        3_532,
        "6411c7498b2c9ac2840723340b29bcdce26d38adaeb322e7c130f910c11647bf",
    ),
    "wp26_fetch_correction": (
        DEFAULT_FETCH_CORRECTION,
        2_523,
        "3a2540bb0ab3adf79cd119bfdd42f769c6a21bf8817ab4600e5d033e7c4301c5",
    ),
    "wp26_original_db": (
        DEFAULT_SOURCE_V1,
        716_800,
        "a0949e7d75069d869e665a10872fe13a3988897cfc810d36149508950594347a",
    ),
    "wp28_terminal_db": (
        DEFAULT_WP28_DB,
        1_929_216,
        "93cb178e0992d29b6eb413b28193d6796497c28abfca576ff0fcacfe5ac58b85",
    ),
    "wp27_control_db": (
        DEFAULT_WP27_DB,
        1_695_744,
        "f31ee6558e43c9b0b412f2c3cda964100acffe0763f9995d301fb32865dece65",
    ),
    "wp28_report": (
        DEFAULT_WP28_REPORT,
        154_082,
        "e057da7297a58cbd1fbbe380d876b15ab311d6fa2ee5385a331c963026544596",
    ),
    "wp28_manifest": (
        DEFAULT_WP28_MANIFEST,
        7_722,
        "c7293876927b993bcf7528166857c9b16321d73ccf8706f0af49b8e5ab5c6001",
    ),
}

FROZEN_FILES: dict[str, tuple[Path, int, str]] = {
    "wp28_order": (
        Path(".claude/ops/jobs/20260827_vnext_wp28_instrument_v21_order.md"),
        4_291,
        "1c57a6f2dca384c1ea84db671c8fbb0f150adf76e37707cee5871a1de3267ff9",
    ),
    "v21_protocol": (
        Path("docs/vnext/IDENTITY_INSTRUMENT_V21_PROTOCOL.md"),
        6_541,
        "a459ebdde66389dc15d983cd97597ba57929a30c71ac02435da987cb60cbdf44",
    ),
    "v21_producer": (
        Path("canonical/vnext_identity_instrument_v21_probe.py"),
        98_886,
        "94241af175c40b5dab903ba04b38314f74cd3a64e6c07fe5095b06a64e984939",
    ),
    "v21_validator": (
        Path("canonical/vnext_identity_instrument_v21_probe_validator.py"),
        69_329,
        "b6b62b4bb1c441cd3ebe533097ae0aaed9ad0ab42174cfec55c0cacf2f4e1aec",
    ),
    "v21_runner": (
        Path("tools/run_vnext_identity_instrument_v21_probe.py"),
        9_287,
        "a8f898b85eb8afffc3d0c6c44ae5b41f7787ff38e5e7d20658a31d5a030c7e43",
    ),
    "v21_validator_cli": (
        Path("tools/validate_vnext_identity_instrument_v21_probe.py"),
        2_039,
        "3749db5c6da822fdfe2112f5ea6528f65834da1355680e55bcbfdc035a5b25ef",
    ),
    "v21_producer_tests": (
        Path("tests/test_vnext_identity_instrument_v21_probe.py"),
        11_784,
        "721130894dd841fe6979a29f4e8bfbd5cff2ae5abdf679899f386acd930eff7a",
    ),
    "v21_validator_tests": (
        Path("tests/test_vnext_identity_instrument_v21_probe_validator.py"),
        7_057,
        "c0311d9104402a6bdb27451f67dcd42a47098b2df74dabc44824b711bd835481",
    ),
}

FROZEN_RUNTIME_FILES: dict[str, tuple[Path, int, str]] = {
    "v2_actor_ledger": (
        Path("canonical/vnext_identity_instrument_v2_probe.py"),
        133_207,
        "e5d8b2f1c01b48b80ac51defe912fe6d54ea0cd77ad82175f38a3438fc3658b1",
    ),
    "wp26_packet_builder": (
        Path("canonical/vnext_identity_instrument_probe.py"),
        122_514,
        "53ab52464aa0990e0748c1177544007ba97a8930a98d3e16263a72c5d3e93449",
    ),
    "wp25_packet_renderer": (
        Path("canonical/vnext_identity_band_precision.py"),
        153_786,
        "fe0072b9440eb080f039990c65c6a882d635df1bf0021f5d6a98bf977a558bfa",
    ),
    "review_renderer_support": (
        Path("canonical/vnext_identity_review.py"),
        53_818,
        "ac22002e0cca43c28a91daee8a2d8780cc5f606143e854780f45d502c44054de",
    ),
    "fetch_primitive": (
        Path("canonical/cross_source_semantic_fetch.py"),
        22_341,
        "54cbd5dbce7c602d4bcfc458b611a655f36045adb98451bc481284251e4d79e6",
    ),
    "selection_support": (
        Path("canonical/cross_source_image_selection.py"),
        36_566,
        "73e73e60de70ac934d3b9601ef9a73b57faa539e3c305cf31e1bc377ca182b46",
    ),
    "derivative_renderer": (
        Path("canonical/divisare_vision_benchmark.py"),
        63_720,
        "f3ac879e667cf903e3a333951d02d40a7a05825b6ec0261a29ab1da025bb801e",
    ),
    "fingerprint_support": (
        Path("canonical/image_fingerprint.py"),
        10_259,
        "a72f5fec6b669a3e0e391af121c433e42395fe6e87c737225abab0a706cb47f3",
    ),
}

EXPECTED_SEMANTIC_SHA256: dict[str, str] = {
    "axis_instructions": "2ec1bfb8c91d7d5d0b845c268d04e93a690a6779a74584bfaf37ac305b2b28a0",
    "controlled_values": "27e38d1bb313d9e9be370bbdfa641252706d96d6e065d8a1ad45881f0a19dd28",
    "consensus_priority": "d345e7455891c63784da587912ba28308a38149794fc9f644cd581feb0ce9ab0",
    "derivation_rules": "343a3c3c2f81bb0042ece2dda0806ce6ee9201c9be140443671d44c74501b26c",
}

QUOTA_PHASES = (
    "pre_freeze",
    "pre_r1_review",
    "pre_r2_review",
    "pre_adjudication_review",
    "pre_reveal_score",
)
PRE_TERMINAL_STATES = (
    "vocabulary_frozen",
    "derivation_verified",
    "instrument_frozen",
    "sample_frozen",
    "criteria_frozen",
    "review_ready",
    "reviews_partial",
    "reviews_locked",
    "adjudication_locked",
)
TERMINAL_STATES = frozenset({"pass", "fail"})


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


def _path_text(root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, tuple(parameters))]


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _check(checks: list[Check], name: str, expected: Any, actual: Any) -> None:
    checks.append(Check(name=name, passed=expected == actual, expected=expected, actual=actual))


def _iso(value: Any) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return parsed


def observation_failures(value: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if set(map(str, value)) != set(OBSERVATION_KEYS):
        failures.append("keys")
    for axis in CONTROLLED_KEYS:
        if value.get(axis) not in CONTROLLED_VALUES[axis]:
            failures.append(axis)
    basis = value.get("basis")
    if not isinstance(basis, str) or not basis.strip() or basis != basis.strip():
        failures.append("basis")
    return sorted(set(failures))


def derive_label(observation: Mapping[str, Any]) -> tuple[str, int]:
    for axis in CONTROLLED_KEYS:
        if observation.get(axis) not in CONTROLLED_VALUES[axis]:
            raise ValueError(f"invalid {axis}")
    image = str(observation["image"])
    scope = str(observation["scope"])
    if image in {"unavailable", "inconclusive"}:
        return "insufficient", 1
    if image == "different_objects" or scope == "unrelated":
        return "different", 2
    if scope == "related_separate":
        return "related", 3
    if image == "same_object" and scope == "same":
        return "same", 4
    return "insufficient", 5


def adjusted_threshold(numerator: int, denominator: int, effective_n: int) -> int:
    """Return the smallest integer preserving the frozen minimum ratio."""

    if numerator < 0 or denominator <= 0 or numerator > denominator or effective_n < 0:
        raise ValueError("invalid ratio")
    return math.ceil(numerator * effective_n / denominator)


def effective_gate_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    excluded_sample_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Replay the four WP2.8 gates after the permanent no-assets exclusion."""

    if len(rows) != TOTAL_SAMPLE:
        raise ValueError("frozen 24-row sample")
    if Counter(str(row.get("group_code", row.get("group", ""))) for row in rows) != GROUP_COUNTS:
        raise ValueError("frozen group composition")
    by_id = {str(row.get("sample_id")): row for row in rows}
    if len(by_id) != TOTAL_SAMPLE or "" in by_id:
        raise ValueError("unique sample IDs")
    excluded = set(map(str, excluded_sample_ids))
    if not excluded <= set(by_id):
        raise ValueError("unknown excluded sample")
    eligible = [row for sample_id, row in by_id.items() if sample_id not in excluded]
    d_rows = [row for row in eligible if str(row.get("group_code", row.get("group"))) == "D"]
    uncertain = [row for row in eligible if bool(row.get("n100_uncertain"))]
    p1_rows = [row for row in eligible if str(row.get("group_code", row.get("group"))) == "P1"]
    raw_uncertain = [row for row in rows if bool(row.get("n100_uncertain"))]
    if len(raw_uncertain) != 5:
        raise ValueError("frozen uncertain subset")
    denominators = {
        "d_image_same_object": len(d_rows),
        "d_scope_same": len(d_rows),
        "n100_image_non_same_object": len(uncertain),
        "p1_non_same_label": len(p1_rows),
    }
    thresholds = {
        "d_image_same_object": adjusted_threshold(14, 15, len(d_rows)),
        "d_scope_same": adjusted_threshold(14, 15, len(d_rows)),
        "n100_image_non_same_object": len(uncertain) // 2 + 1,
        "p1_non_same_label": adjusted_threshold(2, 3, len(p1_rows)),
    }
    counts = {
        "d_image_same_object": sum(row.get("image") == "same_object" for row in d_rows),
        "d_scope_same": sum(row.get("scope") == "same" for row in d_rows),
        "n100_image_non_same_object": sum(row.get("image") != "same_object" for row in uncertain),
        "p1_non_same_label": sum(row.get("derived_label") != "same" for row in p1_rows),
    }
    predicates = {
        code: {
            "actual": int(counts[code]),
            "denominator": int(denominators[code]),
            "threshold": int(thresholds[code]),
            "passed": bool(counts[code] >= thresholds[code] and denominators[code] > 0),
        }
        for code in counts
    }
    return {
        "excluded_sample_ids": sorted(excluded),
        "counts": counts,
        "predicates": predicates,
        "status": "pass" if all(row["passed"] for row in predicates.values()) else "fail",
    }


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
    forbidden = (
        "label",
        "gold",
        "candidate",
        "group",
        "cohort",
        "stratum",
        "score",
        "proposal",
        "reveal",
        "derivation",
    )
    failures: list[str] = []
    for location, key, child in _walk_json(value):
        normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        if any(fragment in normalized for fragment in forbidden):
            failures.append(location)
        if isinstance(child, str) and child in GROUP_COUNTS:
            failures.append(location + ":hidden_group_value")
    serialized = _canonical_json(value)
    if any(token and token in serialized for token in hidden_identifiers):
        failures.append("hidden_identifier_value")
    return sorted(set(failures))


def _sqlite_sidecars(path: Path) -> list[str]:
    return [
        Path(str(path) + suffix).name
        for suffix in ("-wal", "-shm", "-journal")
        if Path(str(path) + suffix).exists()
    ]


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


def _file_spec(path: Path) -> tuple[int, str]:
    return path.stat().st_size, _sha256_file(path)


def _cache_tree(path: Path) -> tuple[int, int, str]:
    if path.exists() and not path.is_dir():
        raise ValueError("cache root missing")
    rows: list[dict[str, Any]] = []
    total = 0
    files = (
        sorted(
            (item for item in path.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(path).as_posix(),
        )
        if path.exists()
        else []
    )
    for file in files:
        size = file.stat().st_size
        rows.append(
            {
                "path": file.relative_to(path).as_posix(),
                "size": size,
                "sha256": _sha256_file(file),
            }
        )
        total += size
    return len(rows), total, _record_sha(rows)


def _verify_static_pins(root: Path, *, verify_full_hashes: bool) -> tuple[dict[str, Any], list[str]]:
    snapshots: dict[str, Any] = {}
    failures: list[str] = []
    for role, (relative, size, digest) in {
        **FROZEN_FILES,
        **FROZEN_RUNTIME_FILES,
        **PINNED_ARTIFACTS,
    }.items():
        path = _resolve(root, relative)
        if not path.is_file():
            failures.append(role + ":missing")
            continue
        actual_size = path.stat().st_size
        actual_sha = _sha256_file(path) if verify_full_hashes else digest
        snapshots[role] = {
            "path": relative.as_posix(),
            "size_bytes": actual_size,
            "sha256": actual_sha,
        }
        if actual_size != size:
            failures.append(role + ":bytes")
        if verify_full_hashes and actual_sha != digest:
            failures.append(role + ":sha256")
    return snapshots, sorted(set(failures))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_record_hash_failures(
    *,
    samples: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    commitments: Sequence[Mapping[str, Any]],
    assets: Sequence[Mapping[str, Any]],
    sheets: Sequence[Mapping[str, Any]],
    fetch_assets: Sequence[Mapping[str, Any]],
    fetch_attempts: Sequence[Mapping[str, Any]],
    cache_pins: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Replay WP2.6 row hashes without importing its frozen implementation."""

    failures: list[str] = []

    def compare(table: str, rows: Sequence[Mapping[str, Any]], payloads: Sequence[Mapping[str, Any]]) -> None:
        for index, (row, payload) in enumerate(zip(rows, payloads, strict=True)):
            if row.get("record_sha256") != _record_sha(payload):
                failures.append(f"{table}[{index}]")

    compare(
        "sample_pairs",
        samples,
        [
            {
                "sample_rank": row["sample_rank"],
                "sample_id": row["sample_id"],
                "stratum": row["stratum"],
                "candidate_id": row["candidate_id"],
                "seed_rank": row["seed_rank_sha256"],
            }
            for row in samples
        ],
    )
    compare(
        "candidate_snapshots",
        candidates,
        [
            {"sample_id": row["sample_id"], "snapshot_sha256": row["snapshot_sha256"]}
            for row in candidates
        ],
    )
    compare(
        "control_commitments",
        commitments,
        [
            {
                "sample_id": row["sample_id"],
                "gold_commitment_sha256": row["gold_commitment_sha256"],
                "source_record_commitment_sha256": row["source_record_commitment_sha256"],
            }
            for row in commitments
        ],
    )
    compare(
        "sample_assets",
        assets,
        [{key: value for key, value in row.items() if key not in {"run_id", "record_sha256"}} for row in assets],
    )
    compare(
        "review_items",
        sheets,
        [{key: value for key, value in row.items() if key not in {"run_id", "record_sha256"}} for row in sheets],
    )
    fetch_payloads: list[dict[str, Any]] = []
    for row in fetch_assets:
        if int(row.get("request_count") or 0) == 0:
            # Cache hits were frozen before the post-HTTP fields existed in the
            # ledger payload.  Requested rows use the complete outcome payload.
            keys = (
                "asset_key",
                "source",
                "source_asset_id",
                "expected_url",
                "expected_raw_sha256",
                "expected_pixel_sha256",
                "status",
                "cache_role",
                "cache_path",
                "error_kind",
                "error_message",
            )
        else:
            keys = tuple(
                key for key in row if key not in {"run_id", "record_sha256"}
            )
        fetch_payloads.append({key: row.get(key) for key in keys})
    compare("fetch_assets", fetch_assets, fetch_payloads)
    attempt_payloads: list[dict[str, Any]] = []
    for row in fetch_attempts:
        try:
            redirects = json.loads(str(row.get("redirect_chain_json")))
        except json.JSONDecodeError:
            redirects = "invalid"
        attempt_payloads.append(
            {
                "asset_key": row["asset_key"],
                "attempt_no": row["attempt_no"],
                "status": row["status"],
                "request_count": row["request_count"],
                "redirect_chain": redirects,
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "error_kind": row["error_kind"],
            }
        )
    compare("fetch_attempts", fetch_attempts, attempt_payloads)
    compare(
        "cache_pins",
        cache_pins,
        [
            {key: value for key, value in row.items() if key not in {"run_id", "record_sha256"}}
            for row in cache_pins
        ],
    )
    return sorted(set(failures))


def _source_snapshot(
    root: Path,
    path_value: Path,
    *,
    verify_full_hashes: bool,
    verify_files: bool,
) -> dict[str, Any]:
    path = _resolve(root, path_value)
    if not path.is_file():
        raise ValueError(f"source DB missing: {path_value}")
    if _sqlite_sidecars(path):
        raise ValueError(f"source DB mutable sidecar: {path_value}")
    # These sidecars are small and their hash is itself part of the repair
    # lineage.  ``verify_full_hashes`` only relaxes large referenced-file and
    # image scans; it never relaxes the DB binding.
    database_sha = _sha256_file(path)
    connection = _open_readonly(path)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("source DB integrity")
        required = {
            "runs",
            "sample_pairs",
            "candidate_snapshots",
            "control_commitments",
            "sample_assets",
            "review_items",
            "fetch_assets",
            "fetch_attempts",
            "cache_pins",
        }
        if not required <= _tables(connection):
            raise ValueError("source DB tables")
        run_rows = _rows(connection, "SELECT * FROM runs")
        samples = _rows(connection, "SELECT * FROM sample_pairs ORDER BY sample_rank")
        candidates = _rows(
            connection,
            "SELECT * FROM candidate_snapshots ORDER BY sample_id",
        )
        commitments = _rows(
            connection,
            "SELECT * FROM control_commitments ORDER BY sample_id",
        )
        assets = _rows(
            connection,
            "SELECT * FROM sample_assets ORDER BY sample_id,side,asset_order",
        )
        sheets = _rows(connection, "SELECT * FROM review_items ORDER BY lane,display_order")
        fetch_assets = _rows(connection, "SELECT * FROM fetch_assets ORDER BY asset_key")
        fetch_attempts = _rows(
            connection,
            "SELECT * FROM fetch_attempts ORDER BY asset_key,attempt_no",
        )
        cache_pins = _rows(connection, "SELECT * FROM cache_pins ORDER BY role")
    finally:
        connection.close()
    if len(run_rows) != 1:
        raise ValueError("source run count")
    source_hash_failures = _source_record_hash_failures(
        samples=samples,
        candidates=candidates,
        commitments=commitments,
        assets=assets,
        sheets=sheets,
        fetch_assets=fetch_assets,
        fetch_attempts=fetch_attempts,
        cache_pins=cache_pins,
    )
    if source_hash_failures:
        raise ValueError("source record hashes: " + ",".join(source_hash_failures))
    run = run_rows[0]
    expected_run = {
        "run_id": SOURCE_RUN_ID,
        "sample_version": SOURCE_SAMPLE_VERSION,
        "seed": SOURCE_SEED,
        "sample_count": TOTAL_SAMPLE,
        "asset_occurrence_count": TOTAL_ASSETS,
        "unique_asset_count": TOTAL_ASSETS,
    }
    if any(run.get(key) != value for key, value in expected_run.items()):
        raise ValueError("source run contract")
    if (
        len(samples),
        len(candidates),
        len(commitments),
        len(sheets),
        len(assets),
        len(fetch_assets),
    ) != (
        TOTAL_SAMPLE,
        TOTAL_SAMPLE,
        TOTAL_SAMPLE,
        TOTAL_SHEETS,
        TOTAL_ASSETS,
        TOTAL_ASSETS,
    ):
        raise ValueError("source row counts")
    if Counter(str(row["lane"]) for row in sheets) != {"r1": 24, "r2": 24}:
        raise ValueError("source sheet lanes")
    # WP2.6 is deliberately group-blind: every source row is ``probe``.  The
    # D/P1/P2/P3 composition is revealed only from the frozen WP2.8 ledger.
    if {str(row["stratum"]) for row in samples} != {"probe"}:
        raise ValueError("source sample blind stratum")
    if len({str(row["sample_id"]) for row in samples}) != TOTAL_SAMPLE:
        raise ValueError("source sample IDs")
    if len({str(row["asset_key"]) for row in assets}) != TOTAL_ASSETS:
        raise ValueError("source asset IDs")
    if any(
        count > MAX_ASSETS_PER_SIDE
        for count in Counter((str(row["sample_id"]), str(row["side"])) for row in assets).values()
    ):
        raise ValueError("source builder per-side cap")
    if any(str(row["side"]) not in {"left", "right"} for row in assets):
        raise ValueError("source asset side")
    sample_ids = {str(row["sample_id"]) for row in samples}
    if {str(row["sample_id"]) for row in sheets} != sample_ids:
        raise ValueError("source sheet sample set")
    if {str(row["sample_id"]) for row in assets} - sample_ids:
        raise ValueError("source asset sample set")
    fetch_by_key = {str(row["asset_key"]): row for row in fetch_assets}
    if set(fetch_by_key) != {str(row["asset_key"]) for row in assets}:
        raise ValueError("source fetch asset set")
    if verify_files:
        for row in sheets:
            file = _resolve(root, str(row["sheet_path"]))
            if not file.is_file() or file.stat().st_size != int(row["sheet_bytes"]):
                raise ValueError("source sheet file binding")
            if verify_full_hashes and _sha256_file(file) != row["sheet_sha256"]:
                raise ValueError("source sheet SHA")
        for row in fetch_assets:
            if str(row["status"]) not in USABLE_FETCH_STATUSES:
                continue
            cache_path = str(row.get("cache_path") or "")
            cache_file = _resolve(root, cache_path)
            if not cache_path or not cache_file.is_file() or cache_file.stat().st_size <= 0:
                raise ValueError("source usable cache binding")
            pixel_sha = str(row.get("actual_pixel_sha256") or row.get("expected_pixel_sha256") or "")
            if pixel_sha and not cache_file.name.startswith(pixel_sha + "."):
                raise ValueError("source cache/pixel binding")
        if verify_full_hashes:
            for row in cache_pins:
                expected_tree = (
                    int(row["file_count_after"]),
                    int(row["total_bytes_after"]),
                    str(row["tree_sha256_after"]),
                )
                if _cache_tree(_resolve(root, str(row["path"]))) != expected_tree:
                    raise ValueError("source cache tree pin")
    packet = [
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
        for row in sheets
    ]
    inventory = [
        {
            "lane": row["lane"],
            "display_order": int(row["display_order"]),
            "path": row["sheet_path"],
            "sha256": row["sheet_sha256"],
            "bytes": int(row["sheet_bytes"]),
        }
        for row in sheets
    ]
    return {
        "path": path,
        "size_bytes": path.stat().st_size,
        "sha256": database_sha,
        "run": run,
        "samples": samples,
        "candidates": candidates,
        "commitments": commitments,
        "assets": assets,
        "sheets": sheets,
        "fetch_assets": fetch_assets,
        "fetch_attempts": fetch_attempts,
        "cache_pins": cache_pins,
        "fetch_by_key": fetch_by_key,
        "packet_sha256": _record_sha(packet),
        "inventory_sha256": _record_sha(inventory),
        "sheet_bytes": sum(int(row["sheet_bytes"]) for row in sheets),
    }


def _semantic_rows(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    specs: dict[str, str] = {
        "instrument_contract": "SELECT * FROM instrument_contract",
        "vocabulary_contract": "SELECT * FROM vocabulary_contract",
        "vocabulary_counts": "SELECT * FROM vocabulary_counts ORDER BY axis,value",
        "consensus_contract": "SELECT * FROM consensus_contract",
        "n100_review_axes": "SELECT * FROM n100_review_axes ORDER BY lane,opaque_review_id",
        "n100_consensus_axes": "SELECT * FROM n100_consensus_axes ORDER BY n100_sample_id",
        "derivation_contract": "SELECT * FROM derivation_contract",
        "gate_contract": "SELECT * FROM gate_contract",
    }
    result: dict[str, list[dict[str, Any]]] = {}
    tables = _tables(connection)
    for table, query in specs.items():
        if table not in tables:
            raise ValueError("missing semantic table: " + table)
        result[table] = _rows(connection, query)
    return result


SEMANTIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "instrument_contract": (
        "protocol_path",
        "protocol_sha256",
        "order_path",
        "order_sha256",
    ),
    "vocabulary_contract": (
        "version",
        "parser_regex",
        "row_count",
        "vocabulary_json",
        "vocabulary_sha256",
        "basis_nonblank_count",
    ),
    "vocabulary_counts": (
        "axis",
        "value",
        "frequency",
        "r1_frequency",
        "r2_frequency",
    ),
    "consensus_contract": ("version", "priority_json", "priority_sha256"),
    "n100_review_axes": (
        "lane",
        "opaque_review_id",
        "n100_sample_id",
        "image",
        "name",
        "firm",
        "place",
        "year",
        "scope",
        "basis",
        "source_record_sha256",
    ),
    "n100_consensus_axes": (
        "n100_sample_id",
        "image",
        "name",
        "firm",
        "place",
        "year",
        "scope",
        "r1_observation_sha256",
        "r2_observation_sha256",
        "consensus_sha256",
    ),
    "derivation_contract": (
        "version",
        "rules_json",
        "rules_sha256",
        "numerator",
        "denominator",
        "minimum_matches",
        "passed",
        "rule_counts_json",
        "mismatches_json",
    ),
    "gate_contract": (
        "gate_json",
        "gate_sha256",
        "d_image_min",
        "d_image_n",
        "d_scope_min",
        "d_scope_n",
        "n100_image_non_same_min",
        "n100_image_non_same_n",
        "p1_min_non_same",
        "p1_n",
        "eligible_sample_ids_json",
    ),
}


def _semantic_payloads(
    values: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table, rows in values.items():
        payloads = [{key: row.get(key) for key in SEMANTIC_COLUMNS[table]} for row in rows]
        if table == "instrument_contract":
            for payload, row in zip(payloads, rows, strict=True):
                try:
                    contract = json.loads(str(row.get("contract_json")))
                except (TypeError, json.JSONDecodeError):
                    contract = {"invalid": True}
                if isinstance(contract, dict):
                    contract.pop("source", None)
                payload["contract_without_source"] = contract
        result[table] = payloads
    return result


def _wp28_snapshot(root: Path, *, verify_full_hashes: bool) -> dict[str, Any]:
    path = _resolve(root, DEFAULT_WP28_DB)
    if not path.is_file():
        raise ValueError("WP2.8 DB missing")
    if verify_full_hashes and _file_spec(path) != PINNED_ARTIFACTS["wp28_terminal_db"][1:]:
        raise ValueError("WP2.8 DB pin")
    if _sqlite_sidecars(path):
        raise ValueError("WP2.8 mutable sidecar")
    connection = _open_readonly(path)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("WP2.8 integrity")
        runs = _rows(connection, "SELECT * FROM runs")
        semantics = _semantic_rows(connection)
        source_samples = _rows(connection, "SELECT * FROM source_samples ORDER BY sample_rank")
        source_assets = _rows(
            connection,
            "SELECT * FROM source_assets ORDER BY sample_id,side,asset_order",
        )
        source_sheets = _rows(connection, "SELECT * FROM source_sheets ORDER BY lane,display_order")
        reveals = _rows(connection, "SELECT * FROM control_reveals ORDER BY sample_id")
        artifacts = _rows(connection, "SELECT * FROM artifacts ORDER BY role")
    finally:
        connection.close()
    if len(runs) != 1 or runs[0].get("instrument_version") != ORIGINAL_INSTRUMENT_VERSION:
        raise ValueError("WP2.8 run contract")
    if len(source_samples) != 24 or len(source_assets) != 93 or len(source_sheets) != 48:
        raise ValueError("WP2.8 source counts")
    if len(reveals) != 24:
        raise ValueError("WP2.8 reveals")
    gates = semantics["gate_contract"]
    if len(gates) != 1 or gates[0].get("gate_sha256") != ORIGINAL_GATE_SHA256:
        raise ValueError("WP2.8 original gate SHA")
    semantic_payloads = _semantic_payloads(semantics)
    semantic_sha = {table: _record_sha(rows) for table, rows in semantic_payloads.items()}
    wp27_path = _resolve(root, DEFAULT_WP27_DB)
    if not wp27_path.is_file() or (
        verify_full_hashes
        and _file_spec(wp27_path) != PINNED_ARTIFACTS["wp27_control_db"][1:]
    ):
        raise ValueError("WP2.7 control DB pin")
    wp27 = _open_readonly(wp27_path)
    try:
        wp27_reveals = _rows(wp27, "SELECT * FROM control_reveals ORDER BY sample_id")
    finally:
        wp27.close()
    if len(wp27_reveals) != 24:
        raise ValueError("WP2.7 control reveal count")
    return {
        "path": path,
        "run": runs[0],
        "semantics": semantics,
        "semantic_payloads": semantic_payloads,
        "semantic_sha": semantic_sha,
        "samples": source_samples,
        "assets": source_assets,
        "sheets": source_sheets,
        "reveals": reveals,
        "wp27_reveals": wp27_reveals,
        "artifacts": artifacts,
    }


def _semantic_identity(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    return [{key: row.get(key) for key in keys} for row in rows]


def _compare_source_versions(v1: Mapping[str, Any], v2: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    # These ledgers are outside the permitted repair surface.  Compare every
    # persisted field (including their row hashes), not merely their IDs.
    for name in ("samples", "candidates", "commitments", "assets"):
        if v1[name] != v2[name]:
            failures.append(name + ":immutable_rows")

    run_immutable = (
        "run_id",
        "schema_version",
        "runner_version",
        "sample_version",
        "review_version",
        "adjudication_version",
        "gate_version",
        "seed",
        "sample_count",
        "asset_occurrence_count",
        "unique_asset_count",
        "logical_http_fetches",
        "wire_http_requests",
    )
    if any(v1["run"].get(key) != v2["run"].get(key) for key in run_immutable):
        failures.append("run:immutable_fields")
    expected_repaired_run = {
        "status": "review_ready",
        "r1_status": "pending",
        "r2_status": "pending",
        "unblind_status": "pending",
        "adjudication_status": "pending",
        "gate_status": "declared",
        "completed_at": None,
    }
    if any(v2["run"].get(key) != value for key, value in expected_repaired_run.items()):
        failures.append("run:repaired_state")

    # Sheets are deterministically re-rendered because repaired pixels are now
    # present.  Only their file binding may change; all blind identities stay.
    sheet_identity = ("lane", "display_order", "opaque_review_id", "sample_id")
    if _semantic_identity(v1["sheets"], sheet_identity) != _semantic_identity(v2["sheets"], sheet_identity):
        failures.append("sheet_identity")

    old_caches = {str(row["role"]): row for row in v1["cache_pins"]}
    new_caches = {str(row["role"]): row for row in v2["cache_pins"]}
    if set(old_caches) != set(new_caches) or len(old_caches) != 5:
        failures.append("cache_pins:set")
    cache_mutable = {
        "path",
        "file_count_after",
        "total_bytes_after",
        "tree_sha256_after",
        "record_sha256",
    }
    for role in sorted(set(old_caches) | set(new_caches)):
        old_cache, new_cache = old_caches.get(role, {}), new_caches.get(role, {})
        changed = {key for key in set(old_cache) | set(new_cache) if old_cache.get(key) != new_cache.get(key)}
        allowed = cache_mutable if role == "wp25_output" else set()
        if not changed <= allowed:
            failures.append("cache_pins:forbidden_delta:" + role)

    failed = {
        str(row["asset_key"])
        for row in v1["fetch_assets"]
        if str(row.get("status")) == "failed"
    }
    requested = {
        str(row["asset_key"])
        for row in v2["fetch_assets"]
        if int(row.get("request_count") or 0) > 0
    }
    attempts = [row for row in v2["fetch_attempts"] if int(row.get("request_count") or 0) > 0]
    if len(failed) != RECOVERY_ASSETS:
        failures.append("original_failed_count")
    if failed != requested:
        failures.append("requested_equals_failed")
    if {str(row["asset_key"]) for row in attempts} != failed or len(attempts) != RECOVERY_ASSETS:
        failures.append("attempt_set")
    if sum(int(row.get("request_count") or 0) for row in v2["fetch_assets"]) != RECOVERY_ASSETS:
        failures.append("logical_http_count")
    if sum(int(row.get("request_count") or 0) for row in attempts) != RECOVERY_ASSETS:
        failures.append("wire_http_count")
    if any(
        int(row.get("attempt_no") or -1) != 1
        or int(row.get("request_count") or 0) != 1
        or row.get("redirect_chain_json") != "[]"
        for row in attempts
    ):
        failures.append("retry_redirect_contract")

    old_fetch = {str(row["asset_key"]): row for row in v1["fetch_assets"]}
    new_fetch = {str(row["asset_key"]): row for row in v2["fetch_assets"]}
    if set(old_fetch) != set(new_fetch):
        failures.append("fetch_assets:set")
    fetch_repair_fields = {
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
        "record_sha256",
    }
    for asset_key in sorted(set(old_fetch) | set(new_fetch)):
        old_row, new_row = old_fetch.get(asset_key, {}), new_fetch.get(asset_key, {})
        changed = {key for key in set(old_row) | set(new_row) if old_row.get(key) != new_row.get(key)}
        if asset_key not in failed and changed:
            failures.append("successful_fetch_changed:" + asset_key)
        if asset_key in failed and not changed <= fetch_repair_fields:
            failures.append("failed_fetch_forbidden_delta:" + asset_key)

    old_attempts = {str(row["asset_key"]): row for row in v1["fetch_attempts"]}
    new_attempts = {str(row["asset_key"]): row for row in v2["fetch_attempts"]}
    if set(old_attempts) != failed or set(new_attempts) != failed:
        failures.append("fetch_attempts:set")
    attempt_repair_fields = {
        "status",
        "started_at",
        "completed_at",
        "error_kind",
        "record_sha256",
    }
    for asset_key in sorted(failed):
        old_row, new_row = old_attempts.get(asset_key, {}), new_attempts.get(asset_key, {})
        changed = {key for key in set(old_row) | set(new_row) if old_row.get(key) != new_row.get(key)}
        if not changed <= attempt_repair_fields:
            failures.append("fetch_attempt_forbidden_delta:" + asset_key)

    for asset_key in failed:
        old = old_fetch.get(asset_key, {})
        new = new_fetch.get(asset_key, {})
        if (
            old.get("source") != "architizer"
            or old.get("status") != "failed"
            or old.get("error_kind") != "connection"
            or "WinError 10013" not in str(old.get("error_message"))
        ):
            failures.append("original_failure_contract:" + asset_key)
        if (
            new.get("status") not in USABLE_FETCH_STATUSES
            or int(new.get("request_count") or 0) != 1
            or not new.get("actual_raw_sha256")
            or not new.get("actual_pixel_sha256")
            or not new.get("cache_path")
            or new.get("error_kind") is not None
        ):
            failures.append("recovery_usable:" + asset_key)
    return sorted(set(failures))


def _expected_completeness(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    samples = {str(row["sample_id"]): row for row in source["samples"]}
    assets_by_side: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in source["assets"]:
        assets_by_side[(str(row["sample_id"]), str(row["side"]))].append(row)
    result: list[dict[str, Any]] = []
    for sample_id in sorted(samples):
        for side in ("left", "right"):
            selected = assets_by_side[(sample_id, side)]
            usable = [
                row
                for row in selected
                if source["fetch_by_key"][str(row["asset_key"])].get("status")
                in USABLE_FETCH_STATUSES
            ]
            result.append(
                {
                    "sample_id": sample_id,
                    "side": side,
                    "selected_asset_count": len(selected),
                    "usable_asset_count": len(usable),
                    "complete": int(bool(usable)),
                }
            )
    return result


def _core_source_failures(
    connection: sqlite3.Connection,
    source: Mapping[str, Any],
    old: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    samples = _rows(connection, "SELECT * FROM source_samples ORDER BY sample_rank")
    sheets = _rows(connection, "SELECT * FROM source_sheets ORDER BY lane,display_order")
    assets = _rows(
        connection,
        "SELECT * FROM source_assets ORDER BY sample_id,side,asset_order",
    )
    fetch_assets = _rows(connection, "SELECT * FROM source_fetch_assets ORDER BY asset_key")
    fetch_attempts = _rows(
        connection,
        "SELECT * FROM source_fetch_attempts ORDER BY asset_key,attempt_no",
    )
    cache_pins = _rows(connection, "SELECT * FROM source_cache_pins ORDER BY role")
    if (len(samples), len(sheets), len(assets), len(fetch_assets)) != (24, 48, 93, 93):
        failures.append("row_counts")
    sample_keys = ("sample_rank", "sample_id", "seed_rank_sha256")
    if _semantic_identity(samples, sample_keys) != _semantic_identity(source["samples"], sample_keys):
        failures.append("samples")
    asset_keys = (
        "sample_id",
        "side",
        "asset_order",
        "asset_key",
        "source",
        "source_building_id",
        "source_asset_id",
        "selection_reason",
    )
    if _semantic_identity(assets, asset_keys) != _semantic_identity(source["assets"], asset_keys):
        failures.append("assets")
    sheet_keys = (
        "lane",
        "display_order",
        "opaque_review_id",
        "sample_id",
        "sheet_path",
        "sheet_sha256",
        "sheet_bytes",
    )
    if _semantic_identity(sheets, sheet_keys) != _semantic_identity(source["sheets"], sheet_keys):
        failures.append("sheets")
    common_fetch = tuple(
        key
        for key in source["fetch_assets"][0]
        if key not in {"run_id", "record_sha256"} and key in _columns(connection, "source_fetch_assets")
    )
    if _semantic_identity(fetch_assets, common_fetch) != _semantic_identity(
        source["fetch_assets"], common_fetch
    ):
        failures.append("fetch_assets")
    common_attempt = tuple(
        key
        for key in source["fetch_attempts"][0]
        if key not in {"run_id", "record_sha256"}
        and key in _columns(connection, "source_fetch_attempts")
    )
    if _semantic_identity(fetch_attempts, common_attempt) != _semantic_identity(
        source["fetch_attempts"], common_attempt
    ):
        failures.append("fetch_attempts")
    common_cache = tuple(
        key
        for key in source["cache_pins"][0]
        if key not in {"run_id", "record_sha256"} and key in _columns(connection, "source_cache_pins")
    )
    if _semantic_identity(cache_pins, common_cache) != _semantic_identity(
        source["cache_pins"], common_cache
    ):
        failures.append("cache_pins")
    old_sample_ids = {str(row["sample_id"]) for row in old["samples"]}
    if {str(row["sample_id"]) for row in samples} != old_sample_ids:
        failures.append("wp28_sample_ids")
    old_asset_ids = {
        (
            str(row["sample_id"]),
            str(row["side"]),
            int(row["asset_order"]),
            str(row["asset_key"]),
        )
        for row in old["assets"]
    }
    current_asset_ids = {
        (
            str(row["sample_id"]),
            str(row["side"]),
            int(row["asset_order"]),
            str(row["asset_key"]),
        )
        for row in assets
    }
    if current_asset_ids != old_asset_ids:
        failures.append("wp28_asset_ids")
    return sorted(set(failures))


def _artifact_failures(
    root: Path,
    connection: sqlite3.Connection,
    old: Mapping[str, Any],
    *,
    phase: str,
) -> tuple[dict[str, Path], list[str]]:
    failures: list[str] = []
    paths: dict[str, Path] = {}
    artifacts = _rows(connection, "SELECT * FROM artifacts ORDER BY role")
    bindings = {
        str(row["role"]): row for row in _rows(connection, "SELECT * FROM artifact_bindings")
    }
    pointers = {
        str(row["role"]): row for row in _rows(connection, "SELECT * FROM artifact_paths")
    }
    old_artifacts = {str(row["role"]): row for row in old["artifacts"]}
    reveals = old["reveals"]
    hidden = {
        str(row.get(key) or "")
        for row in reveals
        for key in (
            "sample_id",
            "candidate_id",
            "n100_sample_id",
        )
        if row.get(key)
    }
    blind_roles = {
        "r1_form",
        "r2_form",
        "r1_completed",
        "r1_locked",
        "r2_completed",
        "r2_locked",
        "adjudication_packet",
        "adjudication_form",
        "adjudication_completed",
        "adjudication_locked",
    }
    for row in artifacts:
        role = str(row.get("role"))
        path = _resolve(root, str(row.get("path") or ""))
        paths[role] = path
        try:
            path.relative_to(root)
        except ValueError:
            failures.append(role + ":external_path")
        try:
            raw = bytes.fromhex(str(row.get("payload_hex") or ""))
        except ValueError:
            failures.append(role + ":payload_hex")
            continue
        if len(raw) != int(row.get("size_bytes") or -1):
            failures.append(role + ":embedded_bytes")
        if hashlib.sha256(raw).hexdigest() != row.get("sha256"):
            failures.append(role + ":embedded_sha")
        if not path.is_file() or path.read_bytes() != raw:
            failures.append(role + ":file_binding")
        binding = bindings.get(role)
        pointer = pointers.get(role)
        if binding is None or any(
            binding.get(key) != row.get(key) for key in ("path", "size_bytes", "sha256", "stage")
        ):
            failures.append(role + ":binding")
        if pointer is None or any(
            pointer.get(key) != row.get(key) for key in ("path", "stage")
        ):
            failures.append(role + ":path")
        if role not in blind_roles:
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            failures.append(role + ":json")
            continue
        leaks = blind_json_leaks(payload, hidden_identifiers=hidden)
        if leaks:
            failures.append(role + ":blind_leak")
        old_row = old_artifacts.get(role)
        if old_row is not None:
            try:
                old_payload = json.loads(bytes.fromhex(str(old_row["payload_hex"])).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                failures.append(role + ":old_json")
            else:
                for key in ("instructions", "review_version", "adjudication_version"):
                    if key in old_payload and payload.get(key) != old_payload.get(key):
                        failures.append(role + ":frozen_" + key)
    run = _rows(connection, "SELECT status FROM runs")
    status = str(run[0]["status"]) if len(run) == 1 else ""
    required = {"sample_manifest"}
    if status != "criteria_frozen":
        required |= {"r1_form", "r2_form"}
    if phase == "final":
        required |= blind_roles
    if not required <= set(paths):
        failures.append("required_roles")
    return paths, sorted(set(failures))


def _actor_failures(connection: sqlite3.Connection) -> list[str]:
    failures: list[str] = []
    reviews = _rows(connection, "SELECT * FROM review_observations ORDER BY lane,display_order")
    if len(reviews) != 48 or Counter(str(row.get("lane")) for row in reviews) != {
        "r1": 24,
        "r2": 24,
    }:
        failures.append("review_counts")
    for row in reviews:
        observation = {key: row.get(key) for key in OBSERVATION_KEYS}
        if observation_failures(observation):
            failures.append("review_axes")
        semantic = {
            "lane": row.get("lane"),
            "opaque_review_id": row.get("opaque_review_id"),
            "sample_id": row.get("sample_id"),
            **observation,
            "reviewer": row.get("reviewer"),
            "reviewed_at": row.get("reviewed_at"),
        }
        if row.get("observation_sha256") != _record_sha(semantic):
            failures.append("review_observation_sha")
    locks = _rows(connection, "SELECT * FROM review_locks ORDER BY lane")
    reviewers = [str(row.get("reviewer") or "").strip().casefold() for row in locks]
    if len(locks) != 2 or len(set(reviewers)) != 2 or any(not value for value in reviewers):
        failures.append("reviewers_distinct")
    adjudications = _rows(
        connection,
        "SELECT * FROM adjudicated_observations ORDER BY sample_id",
    )
    if len(adjudications) != 24:
        failures.append("adjudication_count")
    for row in adjudications:
        observation = {key: row.get(key) for key in OBSERVATION_KEYS}
        if observation_failures(observation):
            failures.append("adjudication_axes")
            continue
        label, rule = derive_label(observation)
        if row.get("derived_label") != label or int(row.get("derivation_rule") or -1) != rule:
            failures.append("adjudication_derivation")
        semantic = {
            "opaque_adjudication_id": row.get("opaque_adjudication_id"),
            "sample_id": row.get("sample_id"),
            **observation,
            "derived_label": label,
            "derivation_rule": rule,
            "axis_adjustments": json.loads(str(row.get("axis_adjustments_json"))),
            "adjudicator": row.get("adjudicator"),
            "adjudicated_at": row.get("adjudicated_at"),
        }
        if row.get("observation_sha256") != _record_sha(semantic):
            failures.append("adjudication_observation_sha")
    adjudication_lock = _rows(connection, "SELECT * FROM adjudication_lock")
    adjudicator = (
        str(adjudication_lock[0].get("adjudicator") or "").strip().casefold()
        if len(adjudication_lock) == 1
        else ""
    )
    if not adjudicator or adjudicator in set(reviewers):
        failures.append("adjudicator_distinct")
    return sorted(set(failures))


def _state_and_barrier_failures(connection: sqlite3.Connection, *, phase: str) -> list[str]:
    failures: list[str] = []
    events = _rows(connection, "SELECT * FROM state_events ORDER BY event_no")
    states = [str(row.get("to_status")) for row in events]
    expected = list(PRE_TERMINAL_STATES)
    if phase == "final":
        expected.append(states[-1] if states and states[-1] in TERMINAL_STATES else "terminal")
    else:
        expected = expected[: len(states)] if len(states) in {5, 6} else expected[:6]
    if states != expected:
        failures.append("state_sequence")
    if [int(row.get("event_no") or -1) for row in events] != list(range(1, len(events) + 1)):
        failures.append("event_numbers")
    previous: datetime | None = None
    event_times: dict[str, datetime] = {}
    for index, row in enumerate(events):
        if row.get("from_status") != ("none" if index == 0 else states[index - 1]):
            failures.append(f"event_{index + 1}_from")
        try:
            current = _iso(row.get("occurred_at"))
            if previous is not None and current < previous:
                failures.append("event_time_order")
            previous = current
            event_times[str(row.get("to_status"))] = current
        except (TypeError, ValueError):
            failures.append("event_timestamp")
    if len(events) >= 5:
        try:
            detail = json.loads(str(events[4]["detail_json"]))
        except (TypeError, json.JSONDecodeError):
            failures.append("criteria_detail")
        else:
            if {key: detail.get(key) for key in ("form_artifacts", "reviews", "adjudication")} != {
                "form_artifacts": 0,
                "reviews": 0,
                "adjudication": 0,
            }:
                failures.append("criteria_zero_counts")
    criteria = event_times.get("criteria_frozen")
    lock = event_times.get("adjudication_locked")
    gates = _rows(connection, "SELECT declared_at FROM gate_contract")
    try:
        if len(gates) != 1 or criteria is None or _iso(gates[0]["declared_at"]) > criteria:
            failures.append("gate_before_criteria")
    except (TypeError, ValueError):
        failures.append("gate_before_criteria")
    for row in _rows(connection, "SELECT role,created_at FROM artifacts"):
        if str(row["role"]) == "sample_manifest":
            continue
        try:
            if criteria is None or _iso(row["created_at"]) < criteria:
                failures.append("form_before_criteria")
        except (TypeError, ValueError):
            failures.append("artifact_timestamp")
    for row in _rows(connection, "SELECT sample_id,revealed_at FROM control_reveals"):
        try:
            if lock is None or _iso(row["revealed_at"]) < lock:
                failures.append("reveal_before_adjudication_lock")
        except (TypeError, ValueError):
            failures.append("reveal_timestamp")
    return sorted(set(failures))


def _quota_failures(connection: sqlite3.Connection, *, phase: str) -> list[str]:
    rows = _rows(connection, "SELECT * FROM quota_observations ORDER BY observation_no")
    run = _rows(connection, "SELECT status FROM runs")
    status = str(run[0]["status"]) if len(run) == 1 else ""
    if phase == "final":
        expected = QUOTA_PHASES
    elif status == "criteria_frozen":
        expected = QUOTA_PHASES[:1]
    else:
        expected = QUOTA_PHASES[:2]
    failures: list[str] = []
    previous_time: datetime | None = None
    stopped = False
    for index, row in enumerate(rows):
        if int(row.get("observation_no") or -1) != index + 1:
            failures.append("observation_numbers")
        if index >= len(expected) or row.get("phase") != expected[index]:
            failures.append("phase_order")
        try:
            current = _iso(row.get("observed_at"))
            if previous_time is not None and current < previous_time:
                failures.append("timestamp_order")
            previous_time = current
        except (TypeError, ValueError):
            failures.append("timestamp")
        try:
            remaining = float(row.get("remaining_percent"))
        except (TypeError, ValueError):
            failures.append("remaining")
            continue
        start, stop = int(remaining > 30), int(remaining <= 20)
        if int(row.get("start_allowed") or 0) != start:
            failures.append("start_gate")
        if int(row.get("stop_required") or 0) != stop:
            failures.append("stop_gate")
        if stopped:
            failures.append("activity_after_stop")
        stopped = stopped or bool(stop)
    if len(rows) != len(expected):
        failures.append("phase_count")
    return sorted(set(failures))


def _projection_snapshot(
    root: Path,
    *,
    path_value: Path = DEFAULT_COMPAT_SOURCE,
    manifest_path_value: Path = DEFAULT_PROJECTION_MANIFEST,
    source: Mapping[str, Any],
    old: Mapping[str, Any],
    verify_full_hashes: bool,
) -> dict[str, Any]:
    path = _resolve(root, path_value)
    if not path.is_file() or _sqlite_sidecars(path):
        raise ValueError("repair projection missing or mutable")
    connection = _open_readonly(path)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("projection integrity")
        required = {
            "runs",
            "input_pins",
            "origin_pins",
            "source_samples",
            "source_sheets",
            "source_assets",
            "source_fetch_assets",
            "source_fetch_attempts",
            "source_cache_pins",
            "control_reveals",
        }
        if _tables(connection) != required:
            raise ValueError("projection table contract")
        if _record_hash_failures(connection):
            raise ValueError("projection record hashes")
        runs = _rows(connection, "SELECT * FROM runs")
        samples = _rows(connection, "SELECT * FROM source_samples ORDER BY sample_rank")
        sheets = _rows(connection, "SELECT * FROM source_sheets ORDER BY lane,display_order")
        assets = _rows(
            connection,
            "SELECT * FROM source_assets ORDER BY sample_id,side,asset_order",
        )
        fetch_assets = _rows(connection, "SELECT * FROM source_fetch_assets ORDER BY asset_key")
        attempts = _rows(
            connection,
            "SELECT * FROM source_fetch_attempts ORDER BY asset_key,attempt_no",
        )
        caches = _rows(connection, "SELECT * FROM source_cache_pins ORDER BY role")
        reveals = _rows(connection, "SELECT * FROM control_reveals ORDER BY sample_id")
        input_pins = _rows(connection, "SELECT * FROM input_pins ORDER BY role")
        origin_pins = _rows(connection, "SELECT * FROM origin_pins ORDER BY role")
    finally:
        connection.close()
    if len(runs) != 1:
        raise ValueError("projection run count")
    run = runs[0]
    if (
        run.get("source_run_id") != SOURCE_RUN_ID
        or run.get("source_sample_version") != SOURCE_SAMPLE_VERSION
        or run.get("source_seed") != SOURCE_SEED
        or run.get("source_logical_sha256") != source["run"].get("sample_logical_sha256")
        or run.get("status") != "projection_frozen"
    ):
        raise ValueError("projection run contract")
    if (len(samples), len(sheets), len(assets), len(fetch_assets), len(attempts), len(caches), len(reveals)) != (
        24,
        48,
        93,
        93,
        17,
        5,
        24,
    ):
        raise ValueError("projection counts")
    sample_keys = ("sample_rank", "sample_id", "seed_rank_sha256")
    if _semantic_identity(samples, sample_keys) != _semantic_identity(source["samples"], sample_keys):
        raise ValueError("projection samples")
    old_samples = {str(row["sample_id"]): row for row in old["samples"]}
    for row in samples:
        prior = old_samples.get(str(row["sample_id"]), {})
        for key in ("control_commitment_sha256", "snapshot_sha256"):
            if row.get(key) != prior.get(key):
                raise ValueError("projection sample commitment")
        source_row = next(
            (value for value in source["samples"] if value["sample_id"] == row["sample_id"]),
            {},
        )
        if row.get("source_pair_record_sha256") != source_row.get("record_sha256"):
            raise ValueError("projection sample source record")
    sheet_keys = (
        "lane",
        "display_order",
        "opaque_review_id",
        "sample_id",
        "sheet_path",
        "sheet_sha256",
        "sheet_bytes",
    )
    if _semantic_identity(sheets, sheet_keys) != _semantic_identity(source["sheets"], sheet_keys):
        raise ValueError("projection sheets")
    if any(
        projected.get("source_record_sha256") != source_row.get("record_sha256")
        for projected, source_row in zip(sheets, source["sheets"], strict=True)
    ):
        raise ValueError("projection sheet source record")
    asset_keys = (
        "sample_id",
        "side",
        "asset_order",
        "asset_key",
        "source",
        "source_building_id",
        "source_asset_id",
        "selection_reason",
    )
    if _semantic_identity(assets, asset_keys) != _semantic_identity(source["assets"], asset_keys):
        raise ValueError("projection assets")
    if any(
        projected.get("source_record_sha256") != source_row.get("record_sha256")
        for projected, source_row in zip(assets, source["assets"], strict=True)
    ):
        raise ValueError("projection asset source record")
    for projected, source_row in zip(fetch_assets, source["fetch_assets"], strict=True):
        for key in source_row:
            if key not in {"run_id", "record_sha256"} and key in projected:
                if projected.get(key) != source_row.get(key):
                    raise ValueError("projection fetch asset")
        if projected.get("source_record_sha256") != source_row.get("record_sha256"):
            raise ValueError("projection fetch asset source record")
        cache = _resolve(root, str(projected.get("cache_path") or ""))
        if not cache.is_file() or (
            verify_full_hashes and _sha256_file(cache) != projected.get("cache_file_sha256")
        ):
            raise ValueError("projection cache pin")
    for projected, source_row in zip(attempts, source["fetch_attempts"], strict=True):
        for key in source_row:
            if key not in {"run_id", "record_sha256"} and key in projected:
                if projected.get(key) != source_row.get(key):
                    raise ValueError("projection fetch attempt")
        if projected.get("source_record_sha256") != source_row.get("record_sha256"):
            raise ValueError("projection fetch attempt source record")
    for projected, source_row in zip(caches, source["cache_pins"], strict=True):
        for key in source_row:
            if key not in {"run_id", "record_sha256"} and projected.get(key) != source_row.get(key):
                raise ValueError("projection cache pin")
        if projected.get("source_record_sha256") != source_row.get("record_sha256"):
            raise ValueError("projection cache source record")
    old_reveals = {str(row["sample_id"]): row for row in old["wp27_reveals"]}
    reveal_keys = (
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
    for row in reveals:
        prior = old_reveals.get(str(row["sample_id"]), {})
        if any(row.get(key) != prior.get(key) for key in reveal_keys):
            raise ValueError("projection control reveal")
    packet = [
        {key: row[key] for key in sheet_keys}
        for row in sheets
    ]
    inventory = [
        {
            "lane": row["lane"],
            "display_order": row["display_order"],
            "path": row["sheet_path"],
            "sha256": row["sheet_sha256"],
            "bytes": row["sheet_bytes"],
        }
        for row in sheets
    ]
    packet_sha = _record_sha(packet)
    inventory_sha = _record_sha(inventory)
    if (
        run.get("source_packet_binding_sha256") != packet_sha
        or run.get("source_visual_inventory_sha256") != inventory_sha
    ):
        raise ValueError("projection packet hashes")
    source_size, source_sha = _file_spec(source["path"])
    origins = {str(row["role"]): row for row in origin_pins}
    repaired_pin = origins.get("repaired_wp26_source", {})
    if (
        repaired_pin.get("path") != _path_text(root, source["path"])
        or repaired_pin.get("size_bytes") != source_size
        or repaired_pin.get("sha256") != source_sha
    ):
        raise ValueError("projection repaired source pin")
    control_pin = origins.get("immutable_wp27_controls", {})
    if (
        control_pin.get("path") != DEFAULT_WP27_DB.as_posix()
        or control_pin.get("size_bytes") != PINNED_ARTIFACTS["wp27_control_db"][1]
        or control_pin.get("sha256") != PINNED_ARTIFACTS["wp27_control_db"][2]
    ):
        raise ValueError("projection control source pin")
    if len(input_pins) != 1 or input_pins[0].get("role") != "wp26_fetch_correction":
        raise ValueError("projection correction pin")
    correction = input_pins[0] if input_pins else {}
    correction_file = _resolve(root, str(correction.get("path") or ""))
    correction_spec = PINNED_ARTIFACTS["wp26_fetch_correction"]
    if (
        correction.get("path") != correction_spec[0].as_posix()
        or correction.get("size_bytes") != correction_spec[1]
        or correction.get("sha256") != correction_spec[2]
        or correction.get("usage") != "terminal_fetch_cap_correction_overlay"
        or not correction_file.is_file()
        or (verify_full_hashes and _file_spec(correction_file) != correction_spec[1:])
    ):
        raise ValueError("projection correction file binding")
    manifest_path = _resolve(root, manifest_path_value)
    if not manifest_path.is_file():
        raise ValueError("projection manifest missing")
    try:
        manifest = _read_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("projection manifest JSON") from exc
    expected_manifest = {
        "status": "PASS",
        "version": PROJECTION_VERSION,
        "run_id": run.get("run_id"),
        "projection": {
            "path": _path_text(root, path),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        },
        "origins": {
            "repaired_wp26": {
                "path": _path_text(root, source["path"]),
                "size_bytes": source_size,
                "sha256": source_sha,
            },
            "immutable_wp27_controls": {
                "path": DEFAULT_WP27_DB.as_posix(),
                "size_bytes": PINNED_ARTIFACTS["wp27_control_db"][1],
                "sha256": PINNED_ARTIFACTS["wp27_control_db"][2],
            },
        },
        "source_contract": {
            "source_run_id": SOURCE_RUN_ID,
            "source_sample_version": SOURCE_SAMPLE_VERSION,
            "source_seed": SOURCE_SEED,
            "source_logical_sha256": source["run"].get("sample_logical_sha256"),
            "packet_binding_sha256": packet_sha,
            "visual_inventory_sha256": inventory_sha,
        },
        "counts": {
            "samples": 24,
            "sheets": 48,
            "asset_occurrences": 93,
            "unique_assets": 93,
            "fetch_attempts": 17,
            "cache_pins": 5,
            "controls": 24,
            "fetch_statuses": dict(
                sorted(Counter(str(row["status"]) for row in fetch_assets).items())
            ),
        },
        "network": {
            "inherited_logical": 17,
            "inherited_wire": 17,
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
    if manifest != expected_manifest:
        raise ValueError("projection manifest contract")
    return {
        "path": path,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        "run": run,
        "samples": samples,
        "sheets": sheets,
        "assets": assets,
        "fetch_assets": fetch_assets,
        "attempts": attempts,
        "caches": caches,
        "reveals": reveals,
        "packet_sha256": packet_sha,
        "inventory_sha256": inventory_sha,
        "manifest_path": manifest_path,
        "manifest_sha256": _sha256_file(manifest_path),
    }


def _repair_overlay_failures(
    root: Path,
    connection: sqlite3.Connection,
    *,
    source_v1: Mapping[str, Any],
    source_v2: Mapping[str, Any],
    projection: Mapping[str, Any],
    old: Mapping[str, Any],
    phase: str,
    verify_full_hashes: bool,
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    contracts = _rows(connection, "SELECT * FROM repair_contract")
    if len(contracts) != 1:
        return ["repair_contract:row_count"], []
    contract = contracts[0]
    expected_contract = {
        "repair_version": REPAIR_VERSION,
        "repair_order_path": DEFAULT_REPAIR_ORDER.as_posix(),
        "repair_order_sha256": PINNED_ARTIFACTS["wp28r_order"][2],
        "original_wp26_path": _path_text(root, source_v1["path"]),
        "original_wp26_sha256": PINNED_ARTIFACTS["wp26_original_db"][2],
        "repaired_wp26_path": _path_text(root, source_v2["path"]),
        "repaired_wp26_sha256": source_v2["sha256"],
        "projection_path": _path_text(root, projection["path"]),
        "projection_sha256": projection["sha256"],
        "original_wp28_path": _path_text(root, old["path"]),
        "original_wp28_sha256": PINNED_ARTIFACTS["wp28_terminal_db"][2],
        "base_gate_sha256": ORIGINAL_GATE_SHA256,
        "excluded_sample_ids_json": _canonical_json([EXPECTED_EXCLUDED_SAMPLE]),
    }
    for key, value in expected_contract.items():
        if contract.get(key) != value:
            failures.append("repair_contract:" + key)
    order = _resolve(root, str(contract.get("repair_order_path") or ""))
    if not order.is_file() or (
        verify_full_hashes and _sha256_file(order) != contract.get("repair_order_sha256")
    ):
        failures.append("repair_contract:order_pin")
    file_rows = _rows(connection, "SELECT * FROM repair_file_proofs ORDER BY role")
    expected_files = {
        **FROZEN_FILES,
        **FROZEN_RUNTIME_FILES,
        "wp28r_order": PINNED_ARTIFACTS["wp28r_order"],
    }
    if len(file_rows) != len(expected_files) or {str(row["role"]) for row in file_rows} != set(expected_files):
        failures.append("file_proofs:set")
    for row in file_rows:
        spec = expected_files.get(str(row["role"]))
        if spec is None:
            continue
        relative, _size, digest = spec
        if str(row["role"]) == "wp28r_order":
            expected_family = "repair_order"
        elif str(row["role"]) in FROZEN_FILES:
            expected_family = "instrument"
        else:
            expected_family = "runtime_dependency"
        if (
            row.get("family") != expected_family
            or row.get("path") != relative.as_posix()
            or row.get("expected_sha256") != digest
            or row.get("actual_sha256") != digest
            or int(row.get("matched") or 0) != 1
        ):
            failures.append("file_proofs:" + str(row["role"]))
    semantic_rows = _rows(connection, "SELECT * FROM repair_semantic_proofs ORDER BY name")
    if len(semantic_rows) != 4 or {str(row["name"]) for row in semantic_rows} != set(EXPECTED_SEMANTIC_SHA256):
        failures.append("semantic_proofs:set")
    for row in semantic_rows:
        expected = EXPECTED_SEMANTIC_SHA256.get(str(row["name"]))
        if (
            row.get("expected_sha256") != expected
            or row.get("actual_sha256") != expected
            or int(row.get("matched") or 0) != 1
        ):
            failures.append("semantic_proofs:" + str(row["name"]))
    recovery = _rows(connection, "SELECT * FROM asset_recovery ORDER BY asset_key")
    old_failed = {
        str(row["asset_key"]): row
        for row in source_v1["fetch_assets"]
        if row.get("status") == "failed"
    }
    old_assets = {str(row["asset_key"]): row for row in source_v1["assets"]}
    new_fetch = source_v2["fetch_by_key"]
    if len(recovery) != 17 or {str(row["asset_key"]) for row in recovery} != set(old_failed):
        failures.append("recovery:set")
    for row in recovery:
        key = str(row["asset_key"])
        prior, asset, new = old_failed.get(key, {}), old_assets.get(key, {}), new_fetch.get(key, {})
        cache_path = _resolve(root, str(new.get("cache_path") or ""))
        cache_sha = _sha256_file(cache_path) if cache_path.is_file() and verify_full_hashes else row.get("new_cache_sha256")
        expected = {
            "sample_id": asset.get("sample_id"),
            "side": asset.get("side"),
            "asset_order": asset.get("asset_order"),
            "source": prior.get("source"),
            "source_asset_id": prior.get("source_asset_id"),
            "old_status": "failed",
            "old_error_kind": prior.get("error_kind"),
            "old_error_message": prior.get("error_message"),
            "new_status": new.get("status"),
            "new_final_url": new.get("actual_final_url"),
            "new_raw_sha256": new.get("actual_raw_sha256"),
            "new_pixel_sha256": new.get("actual_pixel_sha256"),
            "new_cache_path": new.get("cache_path"),
            "new_cache_sha256": cache_sha,
            "request_count": 1,
            "recovered": 1,
        }
        for field, value in expected.items():
            if row.get(field) != value:
                failures.append(f"recovery:{key}:{field}")
    expected_complete = _expected_completeness(source_v2)
    actual_complete = _rows(
        connection,
        "SELECT * FROM asset_completeness ORDER BY sample_id,side",
    )
    if len(actual_complete) != 48:
        failures.append("completeness:count")
    expected_by_side = {(row["sample_id"], row["side"]): row for row in expected_complete}
    source_assets_by_side: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in source_v2["assets"]:
        source_assets_by_side[(str(row["sample_id"]), str(row["side"]))].append(row)
    for row in actual_complete:
        key = (str(row["sample_id"]), str(row["side"]))
        expected = expected_by_side.get(key, {})
        assets = source_assets_by_side.get(key, [])
        asset_keys = [str(asset["asset_key"]) for asset in assets]
        statuses = [str(source_v2["fetch_by_key"][asset_key]["status"]) for asset_key in asset_keys]
        values = {
            "selected_count": expected.get("selected_asset_count"),
            "usable_count": expected.get("usable_asset_count"),
            "excluded_no_assets": int(not bool(expected.get("complete"))),
            "asset_keys_json": _canonical_json(asset_keys),
            "statuses_json": _canonical_json(statuses),
        }
        for field, value in values.items():
            if row.get(field) != value:
                failures.append(f"completeness:{key[0]}:{key[1]}:{field}")
    exclusions = _rows(connection, "SELECT * FROM sample_exclusions ORDER BY sample_id")
    excluded_ids = [str(row["sample_id"]) for row in exclusions]
    if excluded_ids != [EXPECTED_EXCLUDED_SAMPLE]:
        failures.append("exclusions:set")
    elif (
        exclusions[0].get("reason") != "at_least_one_side_has_zero_usable_assets"
        or int(exclusions[0].get("excluded_from_gate_scoring") or 0) != 1
    ):
        failures.append("exclusions:contract")
    effective = _rows(connection, "SELECT * FROM effective_gate_contract ORDER BY predicate")
    if len(effective) != 4:
        failures.append("effective_gate:count")
    effective_specs = {
        "d_image_same_object": (
            15,
            14,
            15,
            14,
            14,
            15,
            "15 * actual >= 14 * denominator",
            [],
        ),
        "d_scope_same": (
            15,
            14,
            15,
            14,
            14,
            15,
            "15 * actual >= 14 * denominator",
            [],
        ),
        "n100_image_non_same_object": (
            5,
            3,
            4,
            3,
            1,
            2,
            "2 * actual > denominator",
            [EXPECTED_EXCLUDED_SAMPLE],
        ),
        "p1_non_same_label": (
            3,
            2,
            2,
            2,
            2,
            3,
            "3 * actual >= 2 * denominator",
            [EXPECTED_EXCLUDED_SAMPLE],
        ),
    }
    by_predicate = {str(row["predicate"]): row for row in effective}
    if set(by_predicate) != set(effective_specs):
        failures.append("effective_gate:set")
    for code, values in effective_specs.items():
        row = by_predicate.get(code, {})
        expected = dict(
            zip(
                (
                    "original_denominator",
                    "original_threshold",
                    "effective_denominator",
                    "effective_threshold",
                    "ratio_numerator",
                    "ratio_denominator",
                    "rule",
                ),
                values[:7],
                strict=True,
            )
        )
        expected["base_gate_sha256"] = ORIGINAL_GATE_SHA256
        expected["excluded_sample_ids_json"] = _canonical_json(values[7])
        for field, value in expected.items():
            if row.get(field) != value:
                failures.append(f"effective_gate:{code}:{field}")
        try:
            _iso(row.get("declared_at"))
        except (TypeError, ValueError):
            failures.append(f"effective_gate:{code}:declared_at")
    effective_sha = _record_sha(_effective_gate_semantic(effective)) if len(effective) == 4 else ""
    authorities = _rows(connection, "SELECT * FROM score_authority")
    if len(authorities) != 1:
        failures.append("score_authority:count")
    else:
        authority = authorities[0]
        expected_authority = {
            "base_gate_sha256": ORIGINAL_GATE_SHA256,
            "core_score_role": "counterfactual_unadjusted_raw",
            "core_decision_authority": 0,
            "effective_gate_sha256": effective_sha,
            "effective_score_role": "wp28r_authoritative",
            "effective_decision_authority": 1,
            "excluded_sample_ids_json": _canonical_json([EXPECTED_EXCLUDED_SAMPLE]),
        }
        for field, value in expected_authority.items():
            if authority.get(field) != value:
                failures.append("score_authority:" + field)
        declared_values = {str(row.get("declared_at")) for row in effective}
        if declared_values != {str(authority.get("declared_at"))}:
            failures.append("score_authority:declared_at")
        try:
            authority_time = _iso(authority.get("declared_at"))
            forms = _rows(
                connection,
                "SELECT created_at FROM artifacts WHERE role IN ('r1_form','r2_form')",
            )
            if any(_iso(row.get("created_at")) < authority_time for row in forms):
                failures.append("score_authority:late_declaration")
        except (TypeError, ValueError):
            failures.append("score_authority:timestamp")
    return sorted(set(failures)), excluded_ids


def _core_contract_failures(
    connection: sqlite3.Connection,
    *,
    projection: Mapping[str, Any],
    old: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    runs = _rows(connection, "SELECT * FROM runs")
    if len(runs) != 1:
        return ["run_count"]
    run = runs[0]
    expected = {
        "instrument_version": ORIGINAL_INSTRUMENT_VERSION,
        "sample_version": ORIGINAL_SAMPLE_VERSION,
        "source_sample_version": SOURCE_SAMPLE_VERSION,
        "source_seed": SOURCE_SEED,
        "source_logical_sha256": projection["run"]["source_logical_sha256"],
        "source_run_id": SOURCE_RUN_ID,
        "source_packet_binding_sha256": projection["packet_sha256"],
        "source_visual_inventory_sha256": projection["inventory_sha256"],
        "sample_count": 24,
        "sheet_count": 48,
        "asset_occurrence_count": 93,
        "unique_asset_count": 93,
        "inherited_logical_http_fetches": 17,
        "inherited_wire_http_requests": 17,
        "incremental_logical_http_fetches": 0,
        "incremental_wire_http_requests": 0,
        "incremental_retries": 0,
        "incremental_redirects": 0,
        "incremental_renderer_substitutions": 0,
        "incremental_cache_writes": 0,
    }
    for key, value in expected.items():
        if run.get(key) != value:
            failures.append("run:" + key)
    semantics = _semantic_rows(connection)
    actual_payloads = _semantic_payloads(semantics)
    for table, expected_rows in old["semantic_payloads"].items():
        if actual_payloads.get(table) != expected_rows:
            failures.append("semantic_contract:" + table)
    gates = semantics["gate_contract"]
    if len(gates) != 1 or gates[0].get("gate_sha256") != ORIGINAL_GATE_SHA256:
        failures.append("original_gate_sha")
    return sorted(set(failures))


def _gate_replay(
    connection: sqlite3.Connection,
    *,
    old: Mapping[str, Any],
    excluded_sample_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    failures: list[str] = []
    old_reveals = {str(row["sample_id"]): row for row in old["reveals"]}
    current_reveals = {
        str(row["sample_id"]): row
        for row in _rows(connection, "SELECT * FROM control_reveals ORDER BY sample_id")
    }
    finals = {
        str(row["sample_id"]): row
        for row in _rows(
            connection,
            "SELECT * FROM adjudicated_observations ORDER BY sample_id",
        )
    }
    if set(current_reveals) != set(old_reveals) or set(finals) != set(old_reveals):
        return {}, {}, ["sample_sets"]
    rows: list[dict[str, Any]] = []
    frozen_fields = (
        "candidate_id",
        "group_code",
        "n100_sample_id",
        "n100_gold_label",
        "v1_probe_label",
        "v2_derived_label",
        "commitment_sha256",
    )
    for sample_id in sorted(old_reveals):
        prior = old_reveals[sample_id]
        reveal = current_reveals[sample_id]
        final = finals[sample_id]
        if any(reveal.get(key) != prior.get(key) for key in frozen_fields):
            failures.append("reveal_frozen:" + sample_id)
        if reveal.get("n100_axes_json") != prior.get("n100_axes_json"):
            failures.append("reveal_n100_axes:" + sample_id)
        observation = {key: final.get(key) for key in OBSERVATION_KEYS}
        try:
            label, rule = derive_label(observation)
        except ValueError:
            failures.append("derivation:" + sample_id)
            continue
        if (
            reveal.get("v21_derived_label") != label
            or int(reveal.get("derivation_rule_no") or -1) != rule
        ):
            failures.append("reveal_derivation:" + sample_id)
        try:
            n100_axes = json.loads(str(reveal.get("n100_axes_json")))
        except (TypeError, json.JSONDecodeError):
            failures.append("reveal_n100_json:" + sample_id)
            n100_axes = None
        reveal_semantic = {
            "sample_id": sample_id,
            "candidate_id": prior["candidate_id"],
            "group_code": prior["group_code"],
            "n100_sample_id": prior["n100_sample_id"],
            "n100_axes": n100_axes,
            "n100_gold_label": prior["n100_gold_label"],
            "v1_probe_label": prior["v1_probe_label"],
            "v2_derived_label": prior["v2_derived_label"],
            "v21_derived_label": label,
            "derivation_rule_no": rule,
            "control_commitment_sha256": prior["commitment_sha256"],
            "revealed_at": reveal.get("revealed_at"),
        }
        if reveal.get("reveal_payload_sha256") != _record_sha(reveal_semantic):
            failures.append("reveal_payload_sha:" + sample_id)
        rows.append(
            {
                "sample_id": sample_id,
                "group_code": prior["group_code"],
                "n100_uncertain": str(prior["n100_sample_id"])
                in EXPECTED_UNCERTAIN_N100_IDS,
                "image": final["image"],
                "scope": final["scope"],
                "derived_label": label,
            }
        )
    if Counter(str(row["group_code"]) for row in rows) != GROUP_COUNTS:
        failures.append("group_counts")
    uncertain_ids = {
        str(old_reveals[row["sample_id"]]["n100_sample_id"])
        for row in rows
        if row["n100_uncertain"]
    }
    if uncertain_ids != EXPECTED_UNCERTAIN_N100_IDS:
        failures.append("uncertain_set")
    try:
        core_gate = effective_gate_from_rows(rows)
        effective_gate = effective_gate_from_rows(
            rows,
            excluded_sample_ids=excluded_sample_ids,
        )
    except ValueError as exc:
        return {}, {}, failures + ["gate:" + str(exc)]
    score_rows = {
        str(row["predicate"]): row
        for row in _rows(connection, "SELECT * FROM gate_scores ORDER BY predicate")
    }
    if set(score_rows) != set(core_gate["predicates"]):
        failures.append("core_gate_scores:set")
    for code, expected in core_gate["predicates"].items():
        row = score_rows.get(code, {})
        for key in ("actual", "denominator", "threshold"):
            if row.get(key) != expected[key]:
                failures.append(f"core_gate_scores:{code}:{key}")
        if bool(row.get("passed")) != expected["passed"]:
            failures.append(f"core_gate_scores:{code}:passed")
    terminal = _rows(connection, "SELECT * FROM terminal")
    if len(terminal) != 1 or terminal[0].get("status") != core_gate["status"]:
        failures.append("core_terminal_status")
    persisted = {
        str(row["predicate"]): row
        for row in _rows(
            connection,
            "SELECT * FROM effective_gate_contract ORDER BY predicate",
        )
    }
    originals = {
        "d_image_same_object": (15, 14, 14, 15),
        "d_scope_same": (15, 14, 14, 15),
        "n100_image_non_same_object": (5, 3, 1, 2),
        "p1_non_same_label": (3, 2, 2, 3),
    }
    if set(persisted) != set(originals):
        failures.append("effective_gate_contract:set")
    for code, (original_n, original_min, ratio_num, ratio_den) in originals.items():
        row = persisted.get(code, {})
        replay = effective_gate["predicates"][code]
        predicate_exclusions = (
            [EXPECTED_EXCLUDED_SAMPLE]
            if code in {"n100_image_non_same_object", "p1_non_same_label"}
            else []
        )
        expected = {
            "base_gate_sha256": ORIGINAL_GATE_SHA256,
            "original_denominator": original_n,
            "original_threshold": original_min,
            "effective_denominator": replay["denominator"],
            "effective_threshold": replay["threshold"],
            "ratio_numerator": ratio_num,
            "ratio_denominator": ratio_den,
            "excluded_sample_ids_json": _canonical_json(predicate_exclusions),
        }
        for key, value in expected.items():
            if row.get(key) != value:
                failures.append(f"effective_gate_contract:{code}:{key}")
    return core_gate, effective_gate, sorted(set(failures))


def _effective_gate_semantic(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "repair_version": REPAIR_VERSION,
        "base_gate_sha256": ORIGINAL_GATE_SHA256,
        "combiner": "all",
        "predicates": [
            {
                key: row.get(key)
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
            for row in sorted(rows, key=lambda value: str(value.get("predicate")))
        ],
    }


def terminal_consistency_failures(
    effective_gate: Mapping[str, Any],
    *,
    persisted_status: str,
    report_status: str,
    manifest_status: str,
) -> list[str]:
    """A measured PASS or FAIL is valid when every terminal binding agrees."""

    expected = str(effective_gate.get("status", ""))
    failures: list[str] = []
    if expected not in TERMINAL_STATES:
        failures.append("invalid_effective_status")
    if persisted_status != expected:
        failures.append("persisted_status")
    if report_status.casefold() != expected:
        failures.append("report_status")
    if manifest_status.casefold() != expected:
        failures.append("manifest_status")
    return failures


def _effective_terminal_failures(
    connection: sqlite3.Connection,
    *,
    core_gate: Mapping[str, Any],
    effective_gate: Mapping[str, Any],
    phase: str,
) -> tuple[str | None, list[str]]:
    failures: list[str] = []
    contracts = _rows(connection, "SELECT * FROM effective_gate_contract ORDER BY predicate")
    semantic = _effective_gate_semantic(contracts)
    gate_sha = _record_sha(semantic)
    score_rows = {
        str(row["predicate"]): row
        for row in _rows(connection, "SELECT * FROM effective_gate_scores ORDER BY predicate")
    }
    terminals = _rows(connection, "SELECT * FROM repair_terminal")
    if phase != "final":
        if score_rows or terminals:
            failures.append("terminal_rows_before_final")
        return None, failures
    if set(score_rows) != set(effective_gate.get("predicates", {})):
        failures.append("effective_scores:set")
    for code, expected in effective_gate.get("predicates", {}).items():
        row = score_rows.get(code, {})
        for key in ("actual", "denominator", "threshold"):
            if row.get(key) != expected[key]:
                failures.append(f"effective_scores:{code}:{key}")
        if bool(row.get("passed")) != expected["passed"]:
            failures.append(f"effective_scores:{code}:passed")
    if len(terminals) != 1:
        failures.append("repair_terminal:count")
        return None, failures
    terminal = terminals[0]
    if terminal.get("status") != effective_gate.get("status"):
        failures.append("repair_terminal:status")
    if terminal.get("base_status") != core_gate.get("status"):
        failures.append("repair_terminal:base_status")
    if terminal.get("effective_gate_sha256") != gate_sha:
        failures.append("repair_terminal:gate_sha")
    try:
        _iso(terminal.get("completed_at"))
    except (TypeError, ValueError):
        failures.append("repair_terminal:timestamp")
    return gate_sha, sorted(set(failures))


def _repair_event_failures(connection: sqlite3.Connection, *, phase: str) -> list[str]:
    failures: list[str] = []
    rows = _rows(connection, "SELECT * FROM repair_events ORDER BY event_no")
    expected = [
        "source_projection_frozen",
        "completeness_frozen",
        "effective_gate_frozen",
    ]
    if phase == "final":
        expected.append("effective_terminal_scored")
    if [str(row.get("event")) for row in rows] != expected:
        failures.append("event_sequence")
    if [int(row.get("event_no") or -1) for row in rows] != list(range(1, len(rows) + 1)):
        failures.append("event_numbers")
    previous: datetime | None = None
    for row in rows:
        try:
            current = _iso(row.get("occurred_at"))
            if previous is not None and current < previous:
                failures.append("event_time_order")
            previous = current
            detail = json.loads(str(row.get("detail_json")))
            if not isinstance(detail, dict):
                failures.append("event_detail")
        except (TypeError, ValueError, json.JSONDecodeError):
            failures.append("event_payload")
    event_by_name = {str(row.get("event")): row for row in rows}
    gate_rows = _rows(connection, "SELECT * FROM effective_gate_contract ORDER BY predicate")
    if len(gate_rows) == 4:
        expected_sha = _record_sha(_effective_gate_semantic(gate_rows))
        try:
            detail = json.loads(
                str(event_by_name.get("effective_gate_frozen", {}).get("detail_json"))
            )
        except (TypeError, json.JSONDecodeError):
            failures.append("effective_gate_event_detail")
        else:
            if (
                detail.get("base_gate_sha256") != ORIGINAL_GATE_SHA256
                or detail.get("effective_gate_sha256") != expected_sha
                or detail.get("score_authority")
                != {
                    "core": "counterfactual_unadjusted_raw",
                    "effective": "wp28r_authoritative",
                }
                or detail.get("late_counts")
                != {"forms": 0, "reviews": 0, "adjudications": 0}
            ):
                failures.append("effective_gate_event_contract")
    gates = _rows(connection, "SELECT declared_at FROM effective_gate_contract")
    artifacts = _rows(
        connection,
        "SELECT created_at FROM artifacts WHERE role IN ('r1_form','r2_form') ORDER BY role",
    )
    try:
        if len(gates) != 4 or len({str(row["declared_at"]) for row in gates}) != 1:
            failures.append("effective_declared_at")
        elif artifacts:
            declared = _iso(gates[0]["declared_at"])
            if any(_iso(row["created_at"]) < declared for row in artifacts):
                failures.append("forms_before_effective_gate")
    except (TypeError, ValueError):
        failures.append("effective_gate_timestamp")
    return sorted(set(failures))


ZERO_OPERATION_KEYS = (
    "vision_requests",
    "llm_requests",
    "paid_api_requests",
    "merge_executions",
    "vnext_id_issuances",
    "tier_promotions",
    "neon_writes",
    "r2_writes",
)


def _core_terminal_artifact_failures(
    root: Path,
    connection: sqlite3.Connection,
    *,
    core_report_path: Path,
    core_manifest_path: Path,
    core_gate: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    failures: list[str] = []
    try:
        report_raw = core_report_path.read_bytes()
        manifest_raw = core_manifest_path.read_bytes()
        report = json.loads(report_raw)
        manifest = json.loads(manifest_raw)
    except (OSError, json.JSONDecodeError):
        return None, None, ["core_terminal_json"]
    rows = _rows(connection, "SELECT * FROM terminal")
    if len(rows) != 1:
        return report, manifest, ["core_terminal_row_count"]
    row = rows[0]
    expected = {
        "status": core_gate.get("status"),
        "report_sha256": hashlib.sha256(report_raw).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "report_json": _canonical_json(report),
        "manifest_json": _canonical_json(manifest),
    }
    for key, value in expected.items():
        if row.get(key) != value:
            failures.append("core_terminal:" + key)
    if _resolve(root, str(row.get("report_path") or "")) != core_report_path.resolve():
        failures.append("core_terminal:report_path")
    if _resolve(root, str(row.get("manifest_path") or "")) != core_manifest_path.resolve():
        failures.append("core_terminal:manifest_path")
    if report.get("gate") != {
        "counts": core_gate.get("counts"),
        "predicates": core_gate.get("predicates"),
        "status": core_gate.get("status"),
    }:
        failures.append("core_report:gate")
    network = manifest.get("network")
    if not isinstance(network, Mapping) or any(
        network.get(key) != 0
        for key in ("incremental_logical", "incremental_wire", "redirects", "retries")
    ):
        failures.append("core_manifest:incremental_network")
    for name, payload in (("core_report", report), ("core_manifest", manifest)):
        if any(payload.get(key) != 0 for key in ZERO_OPERATION_KEYS):
            failures.append(name + ":zero_operations")
    return report, manifest, sorted(set(failures))


def _comparison_rows(
    connection: sqlite3.Connection,
    old: Mapping[str, Any],
) -> list[dict[str, Any]]:
    old_path = old["path"]
    old_connection = _open_readonly(old_path)
    try:
        prior = {
            str(row["sample_id"]): row
            for row in _rows(
                old_connection,
                "SELECT * FROM adjudicated_observations ORDER BY sample_id",
            )
        }
    finally:
        old_connection.close()
    current = {
        str(row["sample_id"]): row
        for row in _rows(
            connection,
            "SELECT * FROM adjudicated_observations ORDER BY sample_id",
        )
    }
    if set(prior) != set(current) or len(prior) != 24:
        raise ValueError("comparison row set")
    result: list[dict[str, Any]] = []
    for sample_id in sorted(prior):
        before = {key: str(prior[sample_id][key]) for key in OBSERVATION_KEYS}
        after = {key: str(current[sample_id][key]) for key in OBSERVATION_KEYS}
        result.append(
            {
                "sample_id": sample_id,
                "wp28": {
                    "observation": before,
                    "derived_label": str(prior[sample_id]["derived_label"]),
                },
                "wp28r": {
                    "observation": after,
                    "derived_label": str(current[sample_id]["derived_label"]),
                },
                "changed_axes": [key for key in OBSERVATION_KEYS if before[key] != after[key]],
                "derived_label_changed": str(prior[sample_id]["derived_label"])
                != str(current[sample_id]["derived_label"]),
            }
        )
    return result


def _outer_terminal_failures(
    root: Path,
    connection: sqlite3.Connection,
    *,
    artifact_path: Path,
    projection: Mapping[str, Any],
    old: Mapping[str, Any],
    core_report: Mapping[str, Any],
    core_manifest: Mapping[str, Any],
    core_gate: Mapping[str, Any],
    effective_gate: Mapping[str, Any],
    effective_gate_sha256: str,
    report_path: Path,
    manifest_path: Path,
    core_report_path: Path,
    core_manifest_path: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    failures: list[str] = []
    try:
        report_raw = report_path.read_bytes()
        manifest_raw = manifest_path.read_bytes()
        report = json.loads(report_raw)
        manifest = json.loads(manifest_raw)
    except (OSError, json.JSONDecodeError):
        return None, None, ["outer_terminal_json"]
    terminals = _rows(connection, "SELECT * FROM repair_terminal")
    if len(terminals) != 1:
        return report, manifest, ["repair_terminal_row_count"]
    terminal = terminals[0]
    failures.extend(
        terminal_consistency_failures(
            effective_gate,
            persisted_status=str(terminal.get("status")),
            report_status=str(report.get("status", "")),
            manifest_status=str(manifest.get("status", "")),
        )
    )
    expected_top = {
        "terminal_status": effective_gate["status"],
        "run_id": terminal["run_id"],
        "repair_version": REPAIR_VERSION,
        "completed_at": terminal["completed_at"],
        "measurement_only": True,
        "authorizes_wp29": effective_gate["status"] == "pass",
        "authorizes_tiering": False,
        "failure_is_terminal": effective_gate["status"] == "fail",
        "repair_scope": "missing_packet_assets_only",
        "decision_authority": "effective_gate_only",
    }
    for key, value in expected_top.items():
        if report.get(key) != value:
            failures.append("report:" + key)
    integrity = report.get("instrument_integrity")
    if not isinstance(integrity, Mapping):
        failures.append("report:instrument_integrity")
    else:
        if (
            integrity.get("base_gate_sha256") != ORIGINAL_GATE_SHA256
            or integrity.get("base_gate_matches_wp28") is not True
        ):
            failures.append("report:base_gate")
        repair_order = integrity.get("repair_order")
        if not isinstance(repair_order, Mapping) or repair_order != {
            "path": DEFAULT_REPAIR_ORDER.as_posix(),
            "expected_size_bytes": PINNED_ARTIFACTS["wp28r_order"][1],
            "actual_size_bytes": PINNED_ARTIFACTS["wp28r_order"][1],
            "expected_sha256": PINNED_ARTIFACTS["wp28r_order"][2],
            "actual_sha256": PINNED_ARTIFACTS["wp28r_order"][2],
            "matched": True,
        }:
            failures.append("report:repair_order")
        proofs = integrity.get("file_proofs")
        semantic = integrity.get("semantic_proofs")
        if not isinstance(proofs, list) or len(proofs) != len(FROZEN_FILES) + len(FROZEN_RUNTIME_FILES) + 1:
            failures.append("report:file_proofs")
        elif any(
            row.get("still_matched") is not True
            or row.get("expected_sha256") != row.get("actual_sha256")
            or row.get("actual_sha256") != row.get("current_sha256")
            for row in proofs
        ):
            failures.append("report:file_proofs_current")
        if not isinstance(semantic, list) or len(semantic) != 4:
            failures.append("report:semantic_proofs")
        elif any(row.get("still_matched") is not True for row in semantic):
            failures.append("report:semantic_proofs_current")
        pins = integrity.get("protected_original_artifacts")
        if not isinstance(pins, Mapping) or any(
            not isinstance(value, Mapping) or value.get("unchanged") is not True
            for value in pins.values()
        ):
            failures.append("report:protected_pins")
    recovery = _rows(connection, "SELECT * FROM asset_recovery ORDER BY asset_key")
    repair_section = report.get("asset_repair")
    if not isinstance(repair_section, Mapping) or (
        repair_section.get("inventory_rows"),
        repair_section.get("recovered_rows"),
        repair_section.get("network_logical"),
        repair_section.get("network_wire"),
        repair_section.get("retries"),
        repair_section.get("redirects"),
        repair_section.get("rows"),
    ) != (17, 17, 17, 17, 0, 0, recovery):
        failures.append("report:asset_repair")
    completeness = _rows(
        connection,
        "SELECT * FROM asset_completeness ORDER BY sample_id,side",
    )
    exclusions = _rows(connection, "SELECT * FROM sample_exclusions ORDER BY sample_id")
    complete_section = report.get("asset_completeness")
    if not isinstance(complete_section, Mapping) or (
        complete_section.get("side_rows"),
        complete_section.get("complete_side_rows"),
        complete_section.get("excluded_side_rows"),
        complete_section.get("excluded_samples"),
        complete_section.get("rows"),
    ) != (48, 47, 1, [EXPECTED_EXCLUDED_SAMPLE], completeness):
        failures.append("report:asset_completeness")
    try:
        changes = _comparison_rows(connection, old)
    except ValueError:
        changes = []
        failures.append("report:comparison_replay")
    change_section = report.get("wp28_to_wp28r_changes")
    changed_count = sum(
        int(bool(row["changed_axes"]) or bool(row["derived_label_changed"])) for row in changes
    )
    if not isinstance(change_section, Mapping) or (
        change_section.get("rows"),
        change_section.get("changed_rows"),
        change_section.get("comparison"),
    ) != (24, changed_count, changes):
        failures.append("report:comparison")
    gate_section = report.get("gate")
    authority_rows = _rows(connection, "SELECT * FROM score_authority")
    authority = authority_rows[0] if len(authority_rows) == 1 else None
    persisted_contract = _rows(
        connection,
        "SELECT * FROM effective_gate_contract ORDER BY predicate",
    )
    persisted_scores = {
        str(row["predicate"]): {
            "actual": int(row["actual"]),
            "denominator": int(row["denominator"]),
            "threshold": int(row["threshold"]),
            "passed": bool(row["passed"]),
        }
        for row in _rows(connection, "SELECT * FROM gate_scores ORDER BY predicate")
    }
    if not isinstance(gate_section, Mapping):
        failures.append("report:gate")
    else:
        original = gate_section.get("original_contract")
        if not isinstance(original, Mapping) or (
            original.get("gate_sha256") != ORIGINAL_GATE_SHA256
            or original.get("scores") != persisted_scores
            or str(original.get("status", "")).casefold() != core_gate["status"]
            or original.get("score_role") != "counterfactual_unadjusted_raw"
            or original.get("decision_authority") is not False
        ):
            failures.append("report:original_gate")
        if (
            gate_section.get("combiner") != "all"
            or gate_section.get("effective_contract_sha256") != effective_gate_sha256
            or gate_section.get("effective_contract") != persisted_contract
            or gate_section.get("effective_status") != effective_gate["status"]
            or gate_section.get("score_authority") != authority
            or gate_section.get("authoritative_decision_source")
            != "effective_gate_scores"
        ):
            failures.append("report:effective_gate_contract")
        observed_predicates = gate_section.get("effective_predicates")
        if not isinstance(observed_predicates, Mapping):
            failures.append("report:effective_predicates")
        else:
            for code, expected in effective_gate["predicates"].items():
                observed = observed_predicates.get(code, {})
                if any(observed.get(key) != expected[key] for key in expected):
                    failures.append("report:effective_predicate:" + code)
    projection_section = report.get("source_projection")
    if not isinstance(projection_section, Mapping) or (
        projection_section.get("path") != _path_text(root, projection["path"])
        or projection_section.get("size_bytes") != projection["size_bytes"]
        or projection_section.get("sha256") != projection["sha256"]
        or projection_section.get("packet_binding_sha256") != projection["packet_sha256"]
        or projection_section.get("visual_inventory_sha256") != projection["inventory_sha256"]
    ):
        failures.append("report:source_projection")
    core_section = report.get("core_terminal")
    if not isinstance(core_section, Mapping) or (
        str(core_section.get("status", "")).casefold() != core_gate["status"]
        or core_section.get("report_sha256") != _sha256_file(core_report_path)
        or core_section.get("manifest_sha256") != _sha256_file(core_manifest_path)
        or str(core_section.get("manifest_status", "")).casefold() != core_gate["status"]
    ):
        failures.append("report:core_terminal")
    incremental = report.get("incremental_instrument_network")
    if not isinstance(incremental, Mapping) or any(
        incremental.get(key) != 0 for key in ("logical", "wire", "retries", "redirects")
    ):
        failures.append("report:incremental_network")
    for name, payload in (("report", report), ("manifest", manifest)):
        if any(payload.get(key) != 0 for key in ZERO_OPERATION_KEYS):
            failures.append(name + ":zero_operations")
    expected_manifest_top = {
        "terminal_status": effective_gate["status"],
        "run_id": terminal["run_id"],
        "repair_version": REPAIR_VERSION,
        "completed_at": terminal["completed_at"],
    }
    for key, value in expected_manifest_top.items():
        if manifest.get(key) != value:
            failures.append("manifest:" + key)
    counts = manifest.get("counts")
    if counts != {
        "samples": 24,
        "sheets": 48,
        "asset_occurrences": 93,
        "unique_assets": 93,
        "repair_inventory": 17,
        "recovered_assets": 17,
        "side_completeness_rows": 48,
        "excluded_samples": 1,
    }:
        failures.append("manifest:counts")
    network = manifest.get("network")
    if network != {
        "repair_logical": 17,
        "repair_wire": 17,
        "instrument_incremental_logical": 0,
        "instrument_incremental_wire": 0,
        "retries": 0,
        "redirects": 0,
    }:
        failures.append("manifest:network")
    manifest_gate = manifest.get("gate")
    if not isinstance(manifest_gate, Mapping) or (
        manifest_gate.get("base_gate_sha256") != ORIGINAL_GATE_SHA256
        or manifest_gate.get("effective_gate_sha256") != effective_gate_sha256
        or manifest_gate.get("score_authority") != authority
        or manifest_gate.get("authoritative_decision_source")
        != "effective_gate_scores"
    ):
        failures.append("manifest:gate")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        failures.append("manifest:artifacts")
    else:
        specs = {
            "database": (artifact_path, artifact_path.stat().st_size, _sha256_file(artifact_path)),
            "source_projection": (
                projection["path"],
                projection["size_bytes"],
                projection["sha256"],
            ),
            "core_report": (
                core_report_path,
                core_report_path.stat().st_size,
                _sha256_file(core_report_path),
            ),
            "core_manifest": (
                core_manifest_path,
                core_manifest_path.stat().st_size,
                _sha256_file(core_manifest_path),
            ),
            "report": (report_path, len(report_raw), hashlib.sha256(report_raw).hexdigest()),
        }
        for role, (path, size, digest) in specs.items():
            row = artifacts.get(role)
            if not isinstance(row, Mapping) or (
                _resolve(root, str(row.get("path") or "")) != Path(path).resolve()
                or row.get("size_bytes") != size
                or row.get("sha256") != digest
            ):
                failures.append("manifest:artifact:" + role)
    return report, manifest, sorted(set(failures))


REQUIRED_CORE_TABLES = frozenset(
    {
        "runs",
        "input_pins",
        "instrument_contract",
        "source_samples",
        "source_sheets",
        "source_assets",
        "source_fetch_assets",
        "source_fetch_attempts",
        "source_cache_pins",
        "quota_observations",
        "gate_contract",
        "artifact_paths",
        "artifact_bindings",
        "json_payloads",
        "artifacts",
        "review_observations",
        "review_locks",
        "review_lock_payloads",
        "adjudication_items",
        "adjudicated_observations",
        "adjudication_lock",
        "adjudication_lock_payload",
        "control_reveals",
        "gate_scores",
        "state_events",
        "terminal",
        "n100_review_axes",
        "vocabulary_counts",
        "vocabulary_contract",
        "consensus_contract",
        "n100_consensus_axes",
        "derivation_contract",
    }
)
REQUIRED_REPAIR_TABLES = frozenset(
    {
        "repair_contract",
        "repair_file_proofs",
        "repair_semantic_proofs",
        "asset_recovery",
        "asset_completeness",
        "sample_exclusions",
        "effective_gate_contract",
        "score_authority",
        "effective_gate_scores",
        "repair_terminal",
        "repair_events",
    }
)

REPAIR_TRIGGER_TARGETS: dict[str, tuple[str, str]] = {
    "repair_contract_no_update": ("UPDATE", "repair_contract"),
    "repair_contract_no_delete": ("DELETE", "repair_contract"),
    "recovery_no_update": ("UPDATE", "asset_recovery"),
    "recovery_no_delete": ("DELETE", "asset_recovery"),
    "completeness_no_update": ("UPDATE", "asset_completeness"),
    "completeness_no_delete": ("DELETE", "asset_completeness"),
    "exclusions_no_update": ("UPDATE", "sample_exclusions"),
    "exclusions_no_delete": ("DELETE", "sample_exclusions"),
    "effective_gate_no_update": ("UPDATE", "effective_gate_contract"),
    "effective_gate_no_delete": ("DELETE", "effective_gate_contract"),
    "score_authority_no_update": ("UPDATE", "score_authority"),
    "score_authority_no_delete": ("DELETE", "score_authority"),
    "effective_scores_no_update": ("UPDATE", "effective_gate_scores"),
    "effective_scores_no_delete": ("DELETE", "effective_gate_scores"),
    "repair_terminal_no_update": ("UPDATE", "repair_terminal"),
    "repair_terminal_no_delete": ("DELETE", "repair_terminal"),
}


def _repair_trigger_failures(connection: sqlite3.Connection) -> list[str]:
    rows = {
        str(row["name"]): str(row["sql"] or "")
        for row in _rows(
            connection,
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name",
        )
    }
    failures: list[str] = []
    for name, (operation, table) in REPAIR_TRIGGER_TARGETS.items():
        sql = re.sub(r"\s+", " ", rows.get(name, "")).upper()
        if not sql:
            failures.append(name + ":missing")
            continue
        if (
            f"BEFORE {operation} ON {table}".upper() not in sql
            or "RAISE(ABORT" not in sql
            or "FROZEN" not in sql
        ):
            failures.append(name + ":contract")
    return sorted(failures)


def _write_validation_report(
    path: Path,
    *,
    checks: Sequence[Check],
    metrics: Mapping[str, Any],
) -> None:
    payload = {
        "status": "PASS" if all(check.passed for check in checks) else "FAIL",
        "validator_version": VALIDATOR_VERSION,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "expected": check.expected,
                "actual": check.actual,
            }
            for check in checks
        ],
        "metrics": metrics,
    }
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != raw:
        raise FileExistsError(f"refusing to clobber validation report: {path}")
    path.write_bytes(raw)


def validate_identity_instrument_v21_probe_repair(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_ARTIFACT,
    source_v1_path: Path = DEFAULT_SOURCE_V1,
    source_v2_path: Path = DEFAULT_SOURCE_V2,
    projection_path: Path = DEFAULT_COMPAT_SOURCE,
    projection_manifest_path: Path = DEFAULT_PROJECTION_MANIFEST,
    report_path: Path = DEFAULT_REPORT,
    manifest_path: Path = DEFAULT_MANIFEST,
    core_report_path: Path = DEFAULT_CORE_REPORT,
    core_manifest_path: Path = DEFAULT_CORE_MANIFEST,
    validation_report_path: Path | None = DEFAULT_VALIDATION_REPORT,
    phase: str = "final",
    verify_full_hashes: bool = True,
) -> ValidationResult:
    """Validate WP2.8-R without importing or executing its producer."""

    if phase not in {"sample", "final"}:
        raise ValueError("phase must be sample or final")
    root = repo_root.resolve()
    artifact = _resolve(root, artifact_path)
    report_file = _resolve(root, report_path)
    manifest_file = _resolve(root, manifest_path)
    core_report_file = _resolve(root, core_report_path)
    core_manifest_file = _resolve(root, core_manifest_path)
    output = _resolve(root, validation_report_path) if validation_report_path is not None else None
    effective_projection_manifest = projection_manifest_path
    if (
        projection_path != DEFAULT_COMPAT_SOURCE
        and projection_manifest_path == DEFAULT_PROJECTION_MANIFEST
    ):
        projection_value = Path(projection_path)
        effective_projection_manifest = projection_value.with_name(
            projection_value.stem + "_manifest.json"
        )
    checks: list[Check] = []
    metrics: dict[str, Any] = {"phase": phase}

    static, static_failures = _verify_static_pins(root, verify_full_hashes=verify_full_hashes)
    metrics["static_pins"] = static
    _check(checks, "frozen_and_original_file_pins", [], static_failures)

    try:
        source_v1 = _source_snapshot(
            root,
            source_v1_path,
            verify_full_hashes=verify_full_hashes,
            verify_files=True,
        )
        _check(checks, "original_wp26_snapshot", True, True)
    except Exception as exc:  # fail-closed diagnostic boundary
        source_v1 = None
        _check(checks, "original_wp26_snapshot", "valid", f"{type(exc).__name__}: {exc}")
    try:
        source_v2 = _source_snapshot(
            root,
            source_v2_path,
            verify_full_hashes=verify_full_hashes,
            verify_files=True,
        )
        _check(checks, "repaired_wp26_snapshot", True, True)
    except Exception as exc:
        source_v2 = None
        _check(checks, "repaired_wp26_snapshot", "valid", f"{type(exc).__name__}: {exc}")
    if source_v1 is not None and source_v2 is not None:
        source_failures = _compare_source_versions(source_v1, source_v2)
        _check(checks, "repair_is_exact_failed_17_only", [], source_failures)
        metrics["repair_source"] = {
            "samples": len(source_v2["samples"]),
            "sheets": len(source_v2["sheets"]),
            "assets": len(source_v2["assets"]),
            "attempts": len(source_v2["fetch_attempts"]),
            "logical_http": sum(
                int(row.get("request_count") or 0) for row in source_v2["fetch_assets"]
            ),
            "wire_http": sum(
                int(row.get("request_count") or 0) for row in source_v2["fetch_attempts"]
            ),
        }
    else:
        _check(checks, "repair_is_exact_failed_17_only", "snapshots available", False)

    try:
        old = _wp28_snapshot(root, verify_full_hashes=verify_full_hashes)
        _check(checks, "wp28_frozen_semantic_snapshot", True, True)
    except Exception as exc:
        old = None
        _check(checks, "wp28_frozen_semantic_snapshot", "valid", f"{type(exc).__name__}: {exc}")
    if source_v2 is not None and old is not None:
        try:
            projection = _projection_snapshot(
                root,
                path_value=projection_path,
                manifest_path_value=effective_projection_manifest,
                source=source_v2,
                old=old,
                verify_full_hashes=verify_full_hashes,
            )
            _check(checks, "repaired_source_projection", True, True)
        except Exception as exc:
            projection = None
            _check(checks, "repaired_source_projection", "valid", f"{type(exc).__name__}: {exc}")
    else:
        projection = None
        _check(checks, "repaired_source_projection", "source snapshots available", False)

    if not artifact.is_file():
        _check(checks, "repair_artifact_exists", True, False)
    elif _sqlite_sidecars(artifact):
        _check(checks, "repair_artifact_sidecars", [], _sqlite_sidecars(artifact))
    else:
        connection: sqlite3.Connection | None = None
        try:
            connection = _open_readonly(artifact)
            _check(
                checks,
                "repair_artifact_integrity",
                "ok",
                connection.execute("PRAGMA integrity_check").fetchone()[0],
            )
            _check(
                checks,
                "repair_sqlite_identity",
                (APPLICATION_ID, USER_VERSION),
                (
                    connection.execute("PRAGMA application_id").fetchone()[0],
                    connection.execute("PRAGMA user_version").fetchone()[0],
                ),
            )
            tables = _tables(connection)
            _check(
                checks,
                "repair_schema_tables",
                [],
                sorted((REQUIRED_CORE_TABLES | REQUIRED_REPAIR_TABLES) - tables),
            )
            _check(
                checks,
                "repair_immutability_triggers",
                [],
                _repair_trigger_failures(connection),
            )
            _check(checks, "repair_record_hashes", [], _record_hash_failures(connection))
            if projection is not None and old is not None:
                _check(
                    checks,
                    "core_instrument_contract",
                    [],
                    _core_contract_failures(connection, projection=projection, old=old),
                )
                _check(
                    checks,
                    "core_repaired_source_copy",
                    [],
                    _core_source_failures(connection, source_v2, old),
                )
            else:
                _check(checks, "core_instrument_contract", "reference snapshots available", False)
                _check(checks, "core_repaired_source_copy", "reference snapshots available", False)
            if all(value is not None for value in (source_v1, source_v2, projection, old)):
                overlay_failures, excluded = _repair_overlay_failures(
                    root,
                    connection,
                    source_v1=source_v1,
                    source_v2=source_v2,
                    projection=projection,
                    old=old,
                    phase=phase,
                    verify_full_hashes=verify_full_hashes,
                )
                _check(checks, "repair_overlay", [], overlay_failures)
            else:
                excluded = []
                _check(checks, "repair_overlay", "reference snapshots available", False)
            _check(
                checks,
                "base_blind_state_and_barriers",
                [],
                _state_and_barrier_failures(connection, phase=phase),
            )
            _check(checks, "repair_event_sequence", [], _repair_event_failures(connection, phase=phase))
            _check(checks, "quota_ledger", [], _quota_failures(connection, phase=phase))
            if old is not None:
                _, artifact_failures = _artifact_failures(
                    root,
                    connection,
                    old,
                    phase=phase,
                )
                _check(checks, "blind_artifacts", [], artifact_failures)
            else:
                _check(checks, "blind_artifacts", "WP2.8 snapshot available", False)

            run_rows = _rows(connection, "SELECT * FROM runs")
            run_status = str(run_rows[0].get("status")) if len(run_rows) == 1 else "invalid"
            if phase == "sample":
                _check(
                    checks,
                    "sample_run_status",
                    True,
                    run_status in {"criteria_frozen", "review_ready"},
                )
                forbidden_counts = {
                    table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                    for table in (
                        "review_observations",
                        "review_locks",
                        "adjudicated_observations",
                        "adjudication_lock",
                        "control_reveals",
                        "gate_scores",
                        "terminal",
                        "effective_gate_scores",
                        "repair_terminal",
                    )
                }
                _check(
                    checks,
                    "sample_pre_actor_zero_rows",
                    {key: 0 for key in forbidden_counts},
                    forbidden_counts,
                )
            elif old is not None:
                _check(checks, "three_isolated_actors", [], _actor_failures(connection))
                core_gate, effective_gate, gate_failures = _gate_replay(
                    connection,
                    old=old,
                    excluded_sample_ids=excluded,
                )
                _check(checks, "independent_gate_replay", [], gate_failures)
                effective_sha, terminal_failures = _effective_terminal_failures(
                    connection,
                    core_gate=core_gate,
                    effective_gate=effective_gate,
                    phase=phase,
                )
                _check(checks, "effective_terminal", [], terminal_failures)
                if core_gate and effective_gate and effective_sha:
                    core_report, core_manifest, core_failures = _core_terminal_artifact_failures(
                        root,
                        connection,
                        core_report_path=core_report_file,
                        core_manifest_path=core_manifest_file,
                        core_gate=core_gate,
                    )
                    _check(checks, "core_terminal_artifacts", [], core_failures)
                    if core_report is not None and core_manifest is not None and projection is not None:
                        outer_report, outer_manifest, outer_failures = _outer_terminal_failures(
                            root,
                            connection,
                            artifact_path=artifact,
                            projection=projection,
                            old=old,
                            core_report=core_report,
                            core_manifest=core_manifest,
                            core_gate=core_gate,
                            effective_gate=effective_gate,
                            effective_gate_sha256=effective_sha,
                            report_path=report_file,
                            manifest_path=manifest_file,
                            core_report_path=core_report_file,
                            core_manifest_path=core_manifest_file,
                        )
                        _check(checks, "outer_terminal_artifacts", [], outer_failures)
                        if outer_report is not None:
                            metrics["terminal_status"] = outer_report.get("terminal_status")
                            metrics["gate"] = effective_gate
                    else:
                        _check(checks, "outer_terminal_artifacts", "terminal inputs available", False)
                else:
                    _check(checks, "core_terminal_artifacts", "gate replay available", False)
                    _check(checks, "outer_terminal_artifacts", "gate replay available", False)
        except Exception as exc:
            _check(checks, "validator_execution", "no exception", f"{type(exc).__name__}: {exc}")
        finally:
            if connection is not None:
                connection.close()

    if output is not None:
        try:
            _write_validation_report(output, checks=checks, metrics=metrics)
        except Exception as exc:
            checks.append(
                Check(
                    name="validation_report_write",
                    passed=False,
                    expected="written without clobber",
                    actual=f"{type(exc).__name__}: {exc}",
                )
            )
            output = None
    return ValidationResult(
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
        metrics=metrics,
        report_path=output,
    )


__all__ = [
    "DEFAULT_ARTIFACT",
    "DEFAULT_COMPAT_SOURCE",
    "DEFAULT_CORE_MANIFEST",
    "DEFAULT_CORE_REPORT",
    "DEFAULT_MANIFEST",
    "DEFAULT_REPORT",
    "DEFAULT_SOURCE_V1",
    "DEFAULT_SOURCE_V2",
    "DEFAULT_VALIDATION_REPORT",
    "ValidationResult",
    "adjusted_threshold",
    "blind_json_leaks",
    "derive_label",
    "effective_gate_from_rows",
    "terminal_consistency_failures",
    "validate_identity_instrument_v21_probe_repair",
]
