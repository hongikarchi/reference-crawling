"""WP2 cross-source building identity candidate builder.

The builder is intentionally offline.  It reads only the byte-frozen curated
Divisare/Architizer databases and E2, plus the two pinned legacy matcher source
files.  It creates candidate *proposals* and a frozen N10 review sample; it
never joins components, issues ``vnx_bld_*`` IDs, or reads the legacy Neon
snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rapidfuzz import fuzz


CONTRACT_VERSION = "vnext-identity-contract-wp0c-v1"
BUILDER_VERSION = "vnext-identity-candidates-builder-v1"
NORMALIZER_VERSION = "vnext-identity-unicode-firm-core-v1"
POLICY_VERSION = "vnext-identity-six-family-policy-v1"
BLOCKER_VERSION = "vnext-pinned-matcher-curated-blocking-v1"
SAMPLE_VERSION = "vnext-identity-stratified-neighbourhood-n10-v1"
LOGICAL_VERSION = "vnext-identity-stage-a-logical-v1"
SAMPLE_SEED = "vnext-wp2-identity-n10-20260824"
APPLICATION_ID = 0x564E5832
SCHEMA_VERSION = 1

DEFAULT_FREEZE = Path("data/reports/vnext_workspace_freeze_v1.json")
DEFAULT_DIVISARE = Path("data/curated/divisare_metadata_v2_4.db")
DEFAULT_ARCHITIZER = Path("data/curated/architizer_curated_v2_0.db")
DEFAULT_E2 = Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db")
DEFAULT_CONTRACT = Path("docs/vnext/CONTRACT_IDENTITY.md")
DEFAULT_MATCHER = Path("canonical/match_buildings_sequential.py")
DEFAULT_PHASH_MATCHER = Path("canonical/match_phash_check.py")
DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_candidates_v1.db")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_candidates_v1_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_candidates_v1_stage_a_report.json")

INPUT_SPECS: Mapping[str, tuple[Path, int, str]] = {
    "divisare_curated_metadata": (
        DEFAULT_DIVISARE,
        2_225_299_456,
        "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f",
    ),
    "architizer_curated_metadata": (
        DEFAULT_ARCHITIZER,
        8_767_438_848,
        "605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc",
    ),
    "cross_source_e2_full_v5": (
        DEFAULT_E2,
        10_164_682_752,
        "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19",
    ),
}
FILE_PINS: Mapping[str, tuple[Path, int, str]] = {
    "identity_contract": (
        DEFAULT_CONTRACT,
        6_598,
        "1ee13f1a2a7ffe7dcaf5b7c1cbaf419c5999d53bcd473311499b74070aabeca6",
    ),
    "building_matcher": (
        DEFAULT_MATCHER,
        26_812,
        "3cf9592a9e54cd4e07512461ca08dc5ca7f8dd5e6cd97b2d6c118dd0b265d87b",
    ),
    "phash_matcher": (
        DEFAULT_PHASH_MATCHER,
        5_450,
        "3dcc73152d822fe2b8eaccbb333625672378a3f6cfa59d3099cda0ca28e64fe7",
    ),
}

EXPECTED_SOURCE_COUNTS = {"divisare": 29_891, "architizer": 61_912}
EXPECTED_SOURCE_TOTAL = 91_803
EXPECTED_E2_METADATA_PAIRS = 6_754
EXPECTED_E2_CANDIDATES = 9_026

NAME_POSITIVE_THRESHOLD = 90.0
FIRM_POSITIVE_THRESHOLD = 90.0
MATCHER_YEAR_TOLERANCE = 2
PASS2_YEAR_TOLERANCE = 1
PASS2_MIN_TOKEN_OVERLAP = 2
YEAR_POSITIVE_TOLERANCE = 1
PHASE_HARD_GAP = 10
PHASH_THRESHOLD = 8
PHASH_STRONG_MIN_IMAGES = 3
PHASH_THIN_MIN_IMAGES = 2

EVIDENCE_FAMILIES = (
    "name_alias_identity",
    "architect_firm_identity",
    "location_site_identity",
    "year_phase_compatibility",
    "source_url_external_stable_id",
    "verified_pixel_image_overlap",
)
PROPOSALS = frozenset({"merge_candidate", "related_but_separate", "reject", "defer"})

GENERIC_FIRM_TOKENS = frozenset(
    {
        "architects", "architect", "architectes", "architecte", "architecten",
        "architekti", "architekt", "architekten", "arquitectos", "arquitectes",
        "arquiteto", "arquitetos", "arquitectura", "arquitetura", "architectures",
        "architecture", "architectural", "atelier", "studio", "estudio", "studios",
        "ateliers", "design", "designs", "designstudio", "office", "bureau",
        "associates", "associati", "associes", "asociados", "partner", "partners",
        "group", "grupo", "gruppe", "compagnia", "company", "co", "llp", "ltd",
        "srl", "gmbh", "sa", "sl", "inc", "spa", "ag", "kg", "et", "and", "the",
        "de", "da", "do", "di", "del", "des", "der", "die", "das", "consultants",
        "consulting", "planning", "planners", "urbanism", "urbanisme", "urbanistes",
        "urbanists", "construction", "engineering", "engineers", "engineer", "interiors",
        "lab", "projetos", "projectos", "proyectos", "workshop", "architizer",
    }
)
GENERIC_BUILDING_TOKENS = frozenset(
    {
        "house", "home", "office", "tower", "building", "project", "studio", "the",
        "a", "an", "and", "of", "for", "by", "with", "in", "on", "centre", "center",
        "store", "shop", "park", "garden", "plaza", "school", "library", "museum",
        "hotel", "restaurant", "cafe", "bar", "residence", "residential", "apartment",
        "apartments", "complex",
    }
)
COUNTRY_ALIASES = {
    "usa": "united states",
    "u s a": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "u k": "united kingdom",
    "great britain": "united kingdom",
    "england": "united kingdom",
    "scotland": "united kingdom",
    "wales": "united kingdom",
    "czech republic": "czechia",
    "russian federation": "russia",
    "republic of korea": "south korea",
    "korea south": "south korea",
    "viet nam": "vietnam",
}
ASCII_FOLD = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "æ": "ae", "œ": "oe", "ß": "ss"})

NORMALIZER_SPEC = {
    "version": NORMALIZER_VERSION,
    "unicode": "NFKD+casefold+combining-mark-removal",
    "punctuation": "unicode-nonword-to-space",
    "ascii_fold": dict(sorted({chr(k): v for k, v in ASCII_FOLD.items()}.items())),
    "firm_generic_tokens": sorted(GENERIC_FIRM_TOKENS),
    "building_generic_tokens": sorted(GENERIC_BUILDING_TOKENS),
    "country_aliases": dict(sorted(COUNTRY_ALIASES.items())),
    "firm_key_strength": "one token length>=3 or at least two tokens",
}
POLICY_SPEC = {
    "version": POLICY_VERSION,
    "candidate_union": ["e2_metadata", "e2_candidate", "matcher_architect_year", "matcher_pass2"],
    "name_positive_threshold": NAME_POSITIVE_THRESHOLD,
    "firm_positive_threshold": FIRM_POSITIVE_THRESHOLD,
    "matcher_year_tolerance": MATCHER_YEAR_TOLERANCE,
    "pass2_year_tolerance": PASS2_YEAR_TOLERANCE,
    "pass2_min_distinctive_token_overlap": PASS2_MIN_TOKEN_OVERLAP,
    "year_positive_tolerance": YEAR_POSITIVE_TOLERANCE,
    "phase_hard_gap": PHASE_HARD_GAP,
    "phash_threshold": PHASH_THRESHOLD,
    "phash_strong_min_unique_pixels": PHASH_STRONG_MIN_IMAGES,
    "phash_thin_min_unique_pixels": PHASH_THIN_MIN_IMAGES,
    "auto_merge_rule": "positive_families>=2 and hard_conflicts=0",
    "legacy_snapshot_candidate_evidence": False,
    "transitive_image_evidence": False,
    "merge_execution": False,
}


class IdentityCandidateError(RuntimeError):
    """Raised when a WP2 input or invariant fails closed."""


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    manifest_path: Path
    report_path: Path
    run_id: str
    logical_sha256: str
    source_building_count: int
    candidate_count: int
    auto_merge_eligible_count: int
    sample_count: int
    elapsed_seconds: float


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 16 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def normalize_text(value: Any, *, ascii_fold: bool = False) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    if ascii_fold:
        text = text.translate(ASCII_FOLD)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(token.strip("_") for token in text.split() if token.strip("_"))


def normalize_country(value: Any) -> str:
    normalized = normalize_text(value, ascii_fold=True)
    return COUNTRY_ALIASES.get(normalized, normalized)


def firm_core(value: Any, *, ascii_fold: bool = False) -> str:
    tokens = [
        token
        for token in normalize_text(value, ascii_fold=ascii_fold).split()
        if token not in GENERIC_FIRM_TOKENS and (len(token) >= 2 or token.isdigit())
    ]
    return " ".join(tokens)


def _strong_firm_key(value: str) -> bool:
    tokens = value.split()
    return bool(tokens) and (len(tokens) >= 2 or len(tokens[0]) >= 3)


def firm_keys(values: Iterable[Any]) -> tuple[str, ...]:
    keys: set[str] = set()
    for raw in values:
        if not raw:
            continue
        text = str(raw).removesuffix(" - Architizer").replace("-", " ")
        for folded in (False, True):
            core = firm_core(text, ascii_fold=folded)
            if _strong_firm_key(core):
                keys.add(core)
    return tuple(sorted(keys))


def distinctive_name_tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_text(value, ascii_fold=True).split()
        if len(token) >= 3 and token not in GENERIC_BUILDING_TOKENS
    }


def _similarity(left: str, right: str) -> tuple[float, float, float]:
    if not left or not right:
        return 0.0, 0.0, 0.0
    sort_score = float(fuzz.token_sort_ratio(left, right))
    set_score = float(fuzz.token_set_ratio(left, right))
    return min(sort_score, set_score), max(sort_score, set_score), set_score


def best_similarity(left_values: Sequence[str], right_values: Sequence[str]) -> tuple[float, float, float, str, str]:
    best = (0.0, 0.0, 0.0, "", "")
    for left in left_values:
        for right in right_values:
            strict, loose, set_score = _similarity(left, right)
            candidate = (strict, loose, set_score, left, right)
            if candidate[:3] > best[:3]:
                best = candidate
    return best


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _assert_no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            raise IdentityCandidateError(f"frozen input has forbidden SQLite sidecar: {sidecar}")


def _atomic_json(path: Path, payload: Mapping[str, Any], *, no_clobber: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if no_clobber and path.exists():
        raise IdentityCandidateError(f"refusing to clobber existing artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise IdentityCandidateError(f"stale temporary artifact exists: {temporary}")
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _load_freeze(freeze_path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    result = {str(row["role"]): dict(row) for row in payload.get("frozen_inputs", [])}
    for role, (path, size, digest) in INPUT_SPECS.items():
        row = result.get(role)
        if row is None:
            raise IdentityCandidateError(f"workspace freeze does not pin {role}")
        if (str(row.get("path")), int(row.get("size_bytes", -1)), str(row.get("sha256"))) != (
            path.as_posix(), size, digest,
        ):
            raise IdentityCandidateError(f"workspace freeze pin mismatch: {role}")
    return result


def _verify_pins(repo_root: Path, freeze_path: Path, *, full_hashes: bool) -> dict[str, dict[str, Any]]:
    _load_freeze(freeze_path)
    pins: dict[str, dict[str, Any]] = {}
    for role, (relative, size, expected) in {**INPUT_SPECS, **FILE_PINS}.items():
        path = repo_root / relative
        if not path.is_file() or path.stat().st_size != size:
            raise IdentityCandidateError(f"missing or size-mismatched frozen input: {relative}")
        if role in INPUT_SPECS:
            _assert_no_sidecars(path)
        actual = sha256_file(path) if full_hashes or role in FILE_PINS else expected
        if actual != expected:
            raise IdentityCandidateError(f"SHA mismatch for frozen input: {role}")
        pins[role] = {
            "role": role,
            "path": relative.as_posix(),
            "size_bytes": size,
            "sha256_before": actual,
            "sha256_after": None,
        }
    freeze_digest = sha256_file(freeze_path)
    pins["workspace_freeze_manifest"] = {
        "role": "workspace_freeze_manifest",
        "path": freeze_path.relative_to(repo_root).as_posix() if freeze_path.is_relative_to(repo_root) else freeze_path.as_posix(),
        "size_bytes": freeze_path.stat().st_size,
        "sha256_before": freeze_digest,
        "sha256_after": None,
    }
    return pins


def _year_gap(left: Mapping[str, Any], right: Mapping[str, Any]) -> int | None:
    years_left = [value for value in (left.get("year_min"), left.get("year_max")) if value is not None]
    years_right = [value for value in (right.get("year_min"), right.get("year_max")) if value is not None]
    if not years_left or not years_right:
        return None
    lmin, lmax = min(years_left), max(years_left)
    rmin, rmax = min(years_right), max(years_right)
    if lmax < rmin:
        return int(rmin - lmax)
    if rmax < lmin:
        return int(lmin - rmax)
    return 0


def _record_sha(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(payload))


def _new_building(source: str, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source": source,
        "source_building_id": str(row["source_building_id"]),
        "name": str(row["name"] or ""),
        "aliases": {str(row["name"] or "")},
        "firms": set(),
        "country": str(row["country"] or ""),
        "locality": str(row["locality"] or ""),
        "year_min": row["completion_year_min"],
        "year_max": row["completion_year_max"],
        "source_urls": set(),
        "external_ids": set(),
        "e2_source_record_sha256": str(row["source_record_sha256"]),
        "e2_metadata_json": str(row["metadata_json"]),
    }


def load_source_buildings(
    divisare: sqlite3.Connection,
    architizer: sqlite3.Connection,
    e2: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load the curated population and assert exact E2 membership."""

    e2_rows = {
        (str(row["source"]), str(row["source_building_id"])): _new_building(str(row["source"]), row)
        for row in e2.execute(
            "SELECT source,source_building_id,name,country,locality,completion_year_min,completion_year_max,"
            "source_record_sha256,metadata_json FROM source_buildings ORDER BY source,source_building_id"
        )
    }
    if len(e2_rows) != EXPECTED_SOURCE_TOTAL:
        raise IdentityCandidateError(f"E2 source population mismatch: {len(e2_rows)}")

    div_ids = {str(row[0]) for row in divisare.execute("SELECT building_id FROM buildings")}
    atz_ids = {str(row[0]) for row in architizer.execute("SELECT building_id FROM buildings")}
    if len(div_ids) != EXPECTED_SOURCE_COUNTS["divisare"] or len(atz_ids) != EXPECTED_SOURCE_COUNTS["architizer"]:
        raise IdentityCandidateError("curated population count mismatch")
    if div_ids != {key[1] for key in e2_rows if key[0] == "divisare"}:
        raise IdentityCandidateError("Divisare curated/E2 membership mismatch")
    if atz_ids != {key[1] for key in e2_rows if key[0] == "architizer"}:
        raise IdentityCandidateError("Architizer curated/E2 membership mismatch")

    for row in divisare.execute(
        "SELECT b.building_id,b.name,b.location_country,b.location_city,b.project_year,"
        "sa.article_id,sa.name_raw,sa.source_url "
        "FROM buildings b LEFT JOIN building_articles ba ON ba.building_id=b.building_id "
        "LEFT JOIN source_articles sa ON sa.article_id=ba.article_id ORDER BY b.building_id,sa.article_id"
    ):
        building = e2_rows[("divisare", str(row["building_id"]))]
        building.update(
            name=str(row["name"] or building["name"]),
            country=str(row["location_country"] or building["country"]),
            locality=str(row["location_city"] or building["locality"]),
            year_min=row["project_year"] if row["project_year"] is not None else building["year_min"],
            year_max=row["project_year"] if row["project_year"] is not None else building["year_max"],
        )
        if row["name_raw"]:
            building["aliases"].add(str(row["name_raw"]))
        if row["source_url"]:
            building["source_urls"].add(str(row["source_url"]))
        if row["article_id"] is not None:
            building["external_ids"].add("divisare_article:" + str(row["article_id"]))

    for row in divisare.execute(
        "SELECT ba.building_id,aa.architect_name,sa.slug,sa.name FROM building_articles ba "
        "JOIN article_architects aa ON aa.article_id=ba.article_id "
        "LEFT JOIN source_architects sa ON sa.architect_id=aa.architect_id "
        "ORDER BY ba.building_id,aa.position"
    ):
        building = e2_rows[("divisare", str(row["building_id"]))]
        building["firms"].update(str(value) for value in (row["architect_name"], row["slug"], row["name"]) if value)

    for row in architizer.execute(
        "SELECT b.building_id,b.preferred_name,b.location_country,b.location_city,b.completion_year,"
        "sp.source_project_id,sp.name,sp.source_url,sp.global_id FROM buildings b "
        "LEFT JOIN building_projects bp ON bp.building_id=b.building_id "
        "LEFT JOIN source_projects sp ON sp.source_project_id=bp.source_project_id "
        "ORDER BY b.building_id,sp.source_project_id"
    ):
        building = e2_rows[("architizer", str(row["building_id"]))]
        building.update(
            name=str(row["preferred_name"] or building["name"]),
            country=str(row["location_country"] or building["country"]),
            locality=str(row["location_city"] or building["locality"]),
            year_min=row["completion_year"] if row["completion_year"] is not None else building["year_min"],
            year_max=row["completion_year"] if row["completion_year"] is not None else building["year_max"],
        )
        if row["name"]:
            building["aliases"].add(str(row["name"]))
        if row["source_url"]:
            building["source_urls"].add(str(row["source_url"]))
        if row["global_id"]:
            building["external_ids"].add("architizer_global:" + str(row["global_id"]))

    for row in architizer.execute(
        "SELECT bp.building_id,pf.raw_firm_name,pf.source_firm_slug,sf.source_name "
        "FROM building_projects bp JOIN project_firms pf ON pf.source_project_id=bp.source_project_id "
        "LEFT JOIN source_firms sf ON sf.source_firm_slug=pf.source_firm_slug "
        "ORDER BY bp.building_id,pf.source_firm_slug"
    ):
        building = e2_rows[("architizer", str(row["building_id"]))]
        building["firms"].update(str(value) for value in (row["raw_firm_name"], row["source_firm_slug"], row["source_name"]) if value)

    for building in e2_rows.values():
        e2_metadata = json.loads(building["e2_metadata_json"])
        building["firms"].update(
            str(value) for value in (e2_metadata.get("firm_name"), e2_metadata.get("firm_slug")) if value
        )
        building["alias_norms"] = tuple(sorted({normalize_text(value, ascii_fold=True) for value in building["aliases"] if value}))
        building["firm_keys"] = firm_keys(building["firms"])
        building["country_norm"] = normalize_country(building["country"])
        building["locality_norm"] = normalize_text(building["locality"], ascii_fold=True)
        building["aliases"] = tuple(sorted(value for value in building["aliases"] if value))
        building["firms"] = tuple(sorted(value for value in building["firms"] if value))
        building["source_urls"] = tuple(sorted(building["source_urls"]))
        building["external_ids"] = tuple(sorted(building["external_ids"]))
    return e2_rows


def load_image_stats(e2: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"asset_count": 0, "successful_asset_count": 0, "pixels": set(), "representative": None}
    )
    query = """
        SELECT ba.source,ba.source_building_id,ba.source_asset_id,a.fingerprint_status,
               a.canonical_url,a.fetch_url,a.final_url,a.raw_response_sha256,
               a.normalized_pixel_sha256,a.phash_hex,a.original_width,a.original_height,
               a.source_record_sha256
        FROM building_assets ba JOIN assets a
          ON a.run_id=ba.run_id AND a.source=ba.source AND a.source_asset_id=ba.source_asset_id
        ORDER BY ba.source,ba.source_building_id,ba.source_asset_id
    """
    for row in e2.execute(query):
        key = (str(row["source"]), str(row["source_building_id"]))
        state = stats[key]
        state["asset_count"] += 1
        if row["fingerprint_status"] == "success":
            state["successful_asset_count"] += 1
            state["pixels"].add(str(row["normalized_pixel_sha256"]))
        width, height = int(row["original_width"] or 0), int(row["original_height"] or 0)
        rep_key = (
            0 if row["fingerprint_status"] == "success" else 1,
            -(width * height),
            -min(width, height),
            str(row["source_asset_id"]),
        )
        current = state["representative"]
        if current is None or rep_key < current[0]:
            state["representative"] = (
                rep_key,
                {
                    "source_asset_id": str(row["source_asset_id"]),
                    "fingerprint_status": str(row["fingerprint_status"]),
                    "canonical_url": row["canonical_url"],
                    "fetch_url": row["fetch_url"],
                    "final_url": row["final_url"],
                    "raw_response_sha256": row["raw_response_sha256"],
                    "normalized_pixel_sha256": row["normalized_pixel_sha256"],
                    "phash_hex": row["phash_hex"],
                    "original_width": row["original_width"],
                    "original_height": row["original_height"],
                    "e2_asset_record_sha256": row["source_record_sha256"],
                },
            )
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for key, state in stats.items():
        result[key] = {
            "asset_count": int(state["asset_count"]),
            "successful_asset_count": int(state["successful_asset_count"]),
            "unique_pixel_count": len(state["pixels"]),
            "representative": state["representative"][1] if state["representative"] else None,
        }
    return result


def candidate_key(left_id: str, right_id: str) -> tuple[str, str]:
    return left_id, right_id


def candidate_id(left_id: str, right_id: str) -> str:
    return "idc_" + stable_hash("vnext-identity-candidate-v1", "divisare", left_id, "architizer", right_id)[:32]


def _e2_endpoint_pair(row: sqlite3.Row) -> tuple[str, str]:
    if row["left_source"] == "divisare" and row["right_source"] == "architizer":
        return str(row["left_source_building_id"]), str(row["right_source_building_id"])
    if row["left_source"] == "architizer" and row["right_source"] == "divisare":
        return str(row["right_source_building_id"]), str(row["left_source_building_id"])
    raise IdentityCandidateError("E2 candidate is not a Divisare/Architizer pair")


def build_candidate_universe(
    buildings: Mapping[tuple[str, str], Mapping[str, Any]],
    e2: sqlite3.Connection,
) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str], dict[str, Any]]]:
    origins: dict[tuple[str, str], set[str]] = defaultdict(set)
    e2_summary: dict[tuple[str, str], dict[str, Any]] = {}

    metadata_rows = list(
        e2.execute(
            "SELECT left_source,left_source_building_id,right_source,right_source_building_id,"
            "metadata_pair_id,metadata_record_sha256 FROM metadata_building_pairs ORDER BY metadata_pair_id"
        )
    )
    if len(metadata_rows) != EXPECTED_E2_METADATA_PAIRS:
        raise IdentityCandidateError("E2 metadata pair count mismatch")
    for row in metadata_rows:
        pair = _e2_endpoint_pair(row)
        origins[pair].add("e2_metadata")
        e2_summary.setdefault(pair, {}).update(
            metadata_pair_id=str(row["metadata_pair_id"]),
            e2_metadata_record_sha256=str(row["metadata_record_sha256"]),
        )

    candidate_rows = list(
        e2.execute(
            "SELECT * FROM cross_source_building_candidates ORDER BY building_candidate_id"
        )
    )
    if len(candidate_rows) != EXPECTED_E2_CANDIDATES:
        raise IdentityCandidateError("E2 cross-source candidate count mismatch")
    for row in candidate_rows:
        pair = _e2_endpoint_pair(row)
        origins[pair].add("e2_candidate")
        e2_summary.setdefault(pair, {}).update(
            e2_building_candidate_id=str(row["building_candidate_id"]),
            metadata_pair_id=row["metadata_pair_id"],
            exact_asset_pair_count=int(row["exact_asset_pair_count"]),
            identical_phash_pair_count=int(row["identical_phash_pair_count"]),
            phash_le8_pair_count=int(row["phash_le8_pair_count"]),
            phash_9_16_pair_count=int(row["phash_9_16_pair_count"]),
            min_phash_distance=row["min_phash_distance"],
            e2_candidate_record_sha256=str(row["candidate_record_sha256"]),
        )

    divs = [row for key, row in buildings.items() if key[0] == "divisare"]
    atzs = [row for key, row in buildings.items() if key[0] == "architizer"]
    by_firm: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for building in divs:
        for key in building["firm_keys"]:
            by_firm[key].append(building)
    for right in atzs:
        seen: dict[str, Mapping[str, Any]] = {}
        for key in right["firm_keys"]:
            for left in by_firm.get(key, ()):
                seen[str(left["source_building_id"])] = left
        for left in seen.values():
            gap = _year_gap(left, right)
            if gap is None or gap <= MATCHER_YEAR_TOLERANCE:
                origins[candidate_key(str(left["source_building_id"]), str(right["source_building_id"]))].add(
                    "matcher_architect_year"
                )

    geo_token_index: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    div_by_id = {str(row["source_building_id"]): row for row in divs}
    for left in divs:
        year = left["year_min"]
        if left["country_norm"] and year is not None:
            for token in distinctive_name_tokens(str(left["name"])):
                geo_token_index[(str(left["country_norm"]), int(year), token)].add(str(left["source_building_id"]))
    for right in atzs:
        year = right["year_min"]
        if not right["country_norm"] or year is None:
            continue
        hits: Counter[str] = Counter()
        for candidate_year in range(int(year) - PASS2_YEAR_TOLERANCE, int(year) + PASS2_YEAR_TOLERANCE + 1):
            for token in distinctive_name_tokens(str(right["name"])):
                for left_id in geo_token_index.get((str(right["country_norm"]), candidate_year, token), ()):
                    hits[left_id] += 1
        for left_id, overlap in hits.items():
            if overlap < PASS2_MIN_TOKEN_OVERLAP:
                continue
            left = div_by_id[left_id]
            strict, _loose, _set_score = _similarity(
                normalize_text(left["name"], ascii_fold=True),
                normalize_text(right["name"], ascii_fold=True),
            )
            if strict >= NAME_POSITIVE_THRESHOLD:
                origins[candidate_key(left_id, str(right["source_building_id"]))].add("matcher_pass2")

    for left_id, right_id in origins:
        if ("divisare", left_id) not in buildings or ("architizer", right_id) not in buildings:
            raise IdentityCandidateError("candidate endpoint is absent from frozen population")
    return dict(origins), e2_summary


def _image_veto(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    left_stats: Mapping[str, Any],
    right_stats: Mapping[str, Any],
    strong_overlap_count: int,
) -> tuple[bool, str | None]:
    if strong_overlap_count:
        return False, None
    left_n, right_n = int(left_stats.get("unique_pixel_count", 0)), int(right_stats.get("unique_pixel_count", 0))
    if left_n >= PHASH_STRONG_MIN_IMAGES and right_n >= PHASH_STRONG_MIN_IMAGES:
        return True, "strong_3x3_zero_direct_overlap"
    primary_set_score = float(
        fuzz.token_set_ratio(
            normalize_text(left["name"], ascii_fold=True),
            normalize_text(right["name"], ascii_fold=True),
        )
    )
    gap = _year_gap(left, right)
    if (
        left_n >= PHASH_THIN_MIN_IMAGES
        and right_n >= PHASH_THIN_MIN_IMAGES
        and primary_set_score < NAME_POSITIVE_THRESHOLD
        and gap is not None
        and gap > PASS2_YEAR_TOLERANCE
    ):
        return True, "thin_2x2_text_confirmed_zero_direct_overlap"
    return False, None


def evaluate_candidate(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    origins: set[str],
    e2_row: Mapping[str, Any],
    left_stats: Mapping[str, Any],
    right_stats: Mapping[str, Any],
) -> dict[str, Any]:
    name_strict, name_loose, name_set, name_left, name_right = best_similarity(
        left["alias_norms"], right["alias_norms"]
    )
    firm_strict, firm_loose, _firm_set, firm_left, firm_right = best_similarity(
        left["firm_keys"], right["firm_keys"]
    )
    exact_firm_keys = sorted(set(left["firm_keys"]) & set(right["firm_keys"]))
    firm_positive = bool(exact_firm_keys) or firm_strict >= FIRM_POSITIVE_THRESHOLD
    gap = _year_gap(left, right)
    exact_count = int(e2_row.get("exact_asset_pair_count", 0) or 0)
    identical_count = int(e2_row.get("identical_phash_pair_count", 0) or 0)
    le8_count = int(e2_row.get("phash_le8_pair_count", 0) or 0)
    weak_count = int(e2_row.get("phash_9_16_pair_count", 0) or 0)
    strong_image_count = exact_count + identical_count + le8_count
    phash_veto, phash_veto_reason = _image_veto(
        left, right, left_stats, right_stats, strong_image_count
    )

    conflicts: list[dict[str, Any]] = []
    if left["country_norm"] and right["country_norm"] and left["country_norm"] != right["country_norm"]:
        conflicts.append(
            {
                "conflict_type": "country_site_incompatible",
                "detail": {"left_country": left["country"], "right_country": right["country"]},
            }
        )
    if firm_positive and gap is not None and gap > MATCHER_YEAR_TOLERANCE:
        conflicts.append(
            {
                "conflict_type": "matcher_architect_year_gate_block",
                "detail": {"year_gap": gap, "tolerance": MATCHER_YEAR_TOLERANCE},
            }
        )
    if gap is not None and gap > PHASE_HARD_GAP:
        conflicts.append(
            {
                "conflict_type": "project_phase_incompatible",
                "detail": {"year_gap": gap, "hard_gap": PHASE_HARD_GAP},
            }
        )
    if phash_veto:
        conflicts.append(
            {
                "conflict_type": "verified_phash_veto",
                "detail": {
                    "reason": phash_veto_reason,
                    "left_unique_pixels": int(left_stats.get("unique_pixel_count", 0)),
                    "right_unique_pixels": int(right_stats.get("unique_pixel_count", 0)),
                    "direct_overlap_count": strong_image_count,
                },
            }
        )

    site_positive = bool(
        left["country_norm"]
        and left["country_norm"] == right["country_norm"]
        and left["locality_norm"]
        and left["locality_norm"] == right["locality_norm"]
    )
    site_negative = any(row["conflict_type"] == "country_site_incompatible" for row in conflicts)
    year_positive = gap is not None and gap <= YEAR_POSITIVE_TOLERANCE
    year_negative = gap is not None and gap > PHASE_HARD_GAP

    families = [
        {
            "family": "name_alias_identity",
            "status": "positive" if name_strict >= NAME_POSITIVE_THRESHOLD else "unknown",
            "score": round(name_strict, 6),
            "detail": {
                "strict": round(name_strict, 6), "loose": round(name_loose, 6),
                "token_set": round(name_set, 6), "left_alias": name_left, "right_alias": name_right,
                "threshold": NAME_POSITIVE_THRESHOLD,
            },
        },
        {
            "family": "architect_firm_identity",
            "status": "positive" if firm_positive else "unknown",
            "score": round(firm_strict, 6),
            "detail": {
                "strict": round(firm_strict, 6), "loose": round(firm_loose, 6),
                "left_key": firm_left, "right_key": firm_right, "exact_keys": exact_firm_keys,
                "threshold": FIRM_POSITIVE_THRESHOLD,
            },
        },
        {
            "family": "location_site_identity",
            "status": "negative" if site_negative else ("positive" if site_positive else "unknown"),
            "score": 1.0 if site_positive else (0.0 if site_negative else None),
            "detail": {
                "left_country": left["country"], "right_country": right["country"],
                "left_locality": left["locality"], "right_locality": right["locality"],
                "rule": "canonical-country+exact-locality",
            },
        },
        {
            "family": "year_phase_compatibility",
            "status": "negative" if year_negative else ("positive" if year_positive else "unknown"),
            "score": None if gap is None else float(gap),
            "detail": {
                "left_year_min": left["year_min"], "left_year_max": left["year_max"],
                "right_year_min": right["year_min"], "right_year_max": right["year_max"],
                "year_gap": gap, "positive_tolerance": YEAR_POSITIVE_TOLERANCE,
            },
        },
        {
            "family": "source_url_external_stable_id",
            "status": "unknown",
            "score": None,
            "detail": {
                "left_source_urls": list(left["source_urls"]), "right_source_urls": list(right["source_urls"]),
                "left_external_ids": list(left["external_ids"]), "right_external_ids": list(right["external_ids"]),
                "reason": "only source-namespaced IDs/URLs are present; no independent shared stable ID",
            },
        },
        {
            "family": "verified_pixel_image_overlap",
            "status": "negative" if phash_veto else ("positive" if strong_image_count else "unknown"),
            "score": float(strong_image_count) if strong_image_count else None,
            "detail": {
                "exact_asset_pair_count": exact_count,
                "identical_phash_pair_count": identical_count,
                "phash_le8_pair_count": le8_count,
                "phash_9_16_review_only_count": weak_count,
                "direct_only": True,
                "phash_veto": phash_veto,
                "phash_veto_reason": phash_veto_reason,
            },
        },
    ]
    positive = sum(row["status"] == "positive" for row in families)
    negative = sum(row["status"] == "negative" for row in families)
    hard_count = len(conflicts)
    if hard_count == 0 and positive >= 2:
        proposal = "merge_candidate"
    elif hard_count:
        conflict_types = {row["conflict_type"] for row in conflicts}
        related_signal = (
            name_strict >= NAME_POSITIVE_THRESHOLD
            and (firm_positive or site_positive)
            and bool(conflict_types & {"matcher_architect_year_gate_block", "project_phase_incompatible"})
            and "country_site_incompatible" not in conflict_types
        )
        proposal = "related_but_separate" if related_signal else "reject"
    else:
        proposal = "defer"
    if proposal not in PROPOSALS:
        raise AssertionError(proposal)
    return {
        "origins": sorted(origins),
        "name_strict": name_strict,
        "name_loose": name_loose,
        "name_set": name_set,
        "firm_strict": firm_strict,
        "year_gap": gap,
        "strong_image_count": strong_image_count,
        "weak_image_count": weak_count,
        "families": families,
        "conflicts": conflicts,
        "positive_family_count": positive,
        "negative_family_count": negative,
        "hard_conflict_count": hard_count,
        "proposal": proposal,
        "auto_merge_eligible": int(positive >= 2 and hard_count == 0),
    }


SCHEMA_SQL = """
PRAGMA foreign_keys=ON;
CREATE TABLE runs(
  run_id TEXT PRIMARY KEY,
  contract_version TEXT NOT NULL,
  builder_version TEXT NOT NULL,
  blocker_version TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  normalizer_version TEXT NOT NULL,
  sample_version TEXT NOT NULL,
  sample_seed TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('building','stage_a_complete','stage_b_complete','pass','fail')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  normalizer_sha256 TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL,
  source_building_count INTEGER,
  candidate_count INTEGER,
  stage_a_logical_sha256 TEXT,
  stage_a_network_requests INTEGER NOT NULL DEFAULT 0 CHECK(stage_a_network_requests=0),
  stage_a_vision_requests INTEGER NOT NULL DEFAULT 0 CHECK(stage_a_vision_requests=0),
  stage_a_llm_requests INTEGER NOT NULL DEFAULT 0 CHECK(stage_a_llm_requests=0),
  legacy_snapshot_reads INTEGER NOT NULL DEFAULT 0 CHECK(legacy_snapshot_reads=0),
  merge_executions INTEGER NOT NULL DEFAULT 0 CHECK(merge_executions=0),
  vnx_id_issuances INTEGER NOT NULL DEFAULT 0 CHECK(vnx_id_issuances=0),
  stage_b_status TEXT NOT NULL DEFAULT 'not_started',
  stage_b_http_requests INTEGER NOT NULL DEFAULT 0,
  stage_b_vision_requests INTEGER NOT NULL DEFAULT 0 CHECK(stage_b_vision_requests=0),
  stage_b_llm_requests INTEGER NOT NULL DEFAULT 0 CHECK(stage_b_llm_requests=0),
  blind_review_status TEXT NOT NULL DEFAULT 'not_started',
  adjudication_status TEXT NOT NULL DEFAULT 'not_started',
  gate_status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE input_pins(
  run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, sha256_before TEXT NOT NULL, sha256_after TEXT,
  usage TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,role)
);
CREATE TABLE source_buildings(
  run_id TEXT NOT NULL REFERENCES runs(run_id), source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, name TEXT NOT NULL, aliases_json TEXT NOT NULL,
  firms_json TEXT NOT NULL, firm_keys_json TEXT NOT NULL, country TEXT,
  country_normalized TEXT, locality TEXT, locality_normalized TEXT,
  completion_year_min INTEGER, completion_year_max INTEGER,
  source_urls_json TEXT NOT NULL, external_ids_json TEXT NOT NULL,
  e2_source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id),
  CHECK(source IN ('divisare','architizer'))
);
CREATE TABLE building_image_stats(
  run_id TEXT NOT NULL REFERENCES runs(run_id), source TEXT NOT NULL,
  source_building_id TEXT NOT NULL, asset_count INTEGER NOT NULL,
  successful_asset_count INTEGER NOT NULL, unique_pixel_count INTEGER NOT NULL,
  representative_source_asset_id TEXT, representative_json TEXT,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,source,source_building_id),
  FOREIGN KEY(run_id,source,source_building_id)
    REFERENCES source_buildings(run_id,source,source_building_id)
);
CREATE TABLE candidate_pairs(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  left_source TEXT NOT NULL CHECK(left_source='divisare'), left_source_building_id TEXT NOT NULL,
  right_source TEXT NOT NULL CHECK(right_source='architizer'), right_source_building_id TEXT NOT NULL,
  origin_e2_metadata INTEGER NOT NULL CHECK(origin_e2_metadata IN (0,1)),
  origin_e2_candidate INTEGER NOT NULL CHECK(origin_e2_candidate IN (0,1)),
  origin_matcher_architect_year INTEGER NOT NULL CHECK(origin_matcher_architect_year IN (0,1)),
  origin_matcher_pass2 INTEGER NOT NULL CHECK(origin_matcher_pass2 IN (0,1)),
  origin_count INTEGER NOT NULL CHECK(origin_count>0),
  e2_metadata_pair_id TEXT, e2_building_candidate_id TEXT,
  exact_asset_pair_count INTEGER NOT NULL, identical_phash_pair_count INTEGER NOT NULL,
  phash_le8_pair_count INTEGER NOT NULL, phash_9_16_pair_count INTEGER NOT NULL,
  min_phash_distance INTEGER, name_strict_score REAL NOT NULL, name_loose_score REAL NOT NULL,
  name_token_set_score REAL NOT NULL, firm_strict_score REAL NOT NULL, year_gap INTEGER,
  positive_family_count INTEGER NOT NULL, negative_family_count INTEGER NOT NULL,
  hard_conflict_count INTEGER NOT NULL, proposal TEXT NOT NULL,
  auto_merge_eligible INTEGER NOT NULL CHECK(auto_merge_eligible IN (0,1)),
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id),
  UNIQUE(run_id,left_source_building_id,right_source_building_id),
  FOREIGN KEY(run_id,left_source,left_source_building_id)
    REFERENCES source_buildings(run_id,source,source_building_id),
  FOREIGN KEY(run_id,right_source,right_source_building_id)
    REFERENCES source_buildings(run_id,source,source_building_id),
  CHECK(proposal IN ('merge_candidate','related_but_separate','reject','defer')),
  CHECK(auto_merge_eligible=(positive_family_count>=2 AND hard_conflict_count=0)),
  CHECK((proposal='merge_candidate')=auto_merge_eligible)
);
CREATE INDEX candidate_left_idx ON candidate_pairs(run_id,left_source_building_id);
CREATE INDEX candidate_right_idx ON candidate_pairs(run_id,right_source_building_id);
CREATE INDEX candidate_proposal_idx ON candidate_pairs(run_id,proposal,auto_merge_eligible);
CREATE TABLE candidate_origins(
  run_id TEXT NOT NULL, candidate_id TEXT NOT NULL, origin TEXT NOT NULL,
  origin_detail_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id,origin),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id),
  CHECK(origin IN ('e2_metadata','e2_candidate','matcher_architect_year','matcher_pass2'))
);
CREATE TABLE evidence_families(
  run_id TEXT NOT NULL, candidate_id TEXT NOT NULL, family TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('positive','negative','unknown')),
  score REAL, evidence_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id,family),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id)
);
CREATE TABLE hard_conflicts(
  run_id TEXT NOT NULL, candidate_id TEXT NOT NULL, conflict_type TEXT NOT NULL,
  detail_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id,conflict_type),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id)
);
CREATE TABLE direct_image_evidence(
  run_id TEXT NOT NULL, candidate_id TEXT NOT NULL, e2_evidence_id TEXT NOT NULL,
  evidence_kind TEXT NOT NULL, left_source_asset_id TEXT NOT NULL,
  right_source_asset_id TEXT NOT NULL, phash_distance INTEGER,
  e2_evidence_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,e2_evidence_id),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id),
  CHECK(evidence_kind IN ('exact_pixel','identical_phash','phash_le8','phash_9_16'))
);
CREATE INDEX direct_image_candidate_idx ON direct_image_evidence(run_id,candidate_id,evidence_kind);
CREATE TABLE sample_neighbourhoods(
  run_id TEXT NOT NULL, sample_rank INTEGER NOT NULL, stratum TEXT NOT NULL,
  candidate_id TEXT NOT NULL, opaque_review_id TEXT NOT NULL, frozen_at TEXT NOT NULL,
  seed_rank_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,sample_rank), UNIQUE(run_id,candidate_id), UNIQUE(run_id,opaque_review_id),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id),
  CHECK(stratum IN ('strong_merge','metadata_only','image_evidence_only','conflict_boundary'))
);
CREATE TABLE sample_neighbourhood_pairs(
  run_id TEXT NOT NULL, opaque_review_id TEXT NOT NULL, display_order INTEGER NOT NULL,
  candidate_id TEXT NOT NULL, relationship TEXT NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,opaque_review_id,display_order),
  FOREIGN KEY(run_id,opaque_review_id) REFERENCES sample_neighbourhoods(run_id,opaque_review_id),
  FOREIGN KEY(run_id,candidate_id) REFERENCES candidate_pairs(run_id,candidate_id),
  CHECK(relationship IN ('focal','shares_left','shares_right'))
);
CREATE TABLE review_assets(
  run_id TEXT NOT NULL, opaque_review_id TEXT NOT NULL, side TEXT NOT NULL,
  asset_order INTEGER NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, selection_reason TEXT NOT NULL,
  expected_url TEXT, expected_raw_sha256 TEXT, expected_pixel_sha256 TEXT,
  status TEXT NOT NULL DEFAULT 'pending', cache_relative_path TEXT,
  actual_final_url TEXT, actual_raw_sha256 TEXT, actual_pixel_sha256 TEXT,
  http_status INTEGER, response_bytes INTEGER, request_count INTEGER NOT NULL DEFAULT 0,
  error_kind TEXT, error_message TEXT, record_sha256 TEXT,
  PRIMARY KEY(run_id,opaque_review_id,side,asset_order),
  FOREIGN KEY(run_id,opaque_review_id) REFERENCES sample_neighbourhoods(run_id,opaque_review_id),
  CHECK(side IN ('left','right'))
);
CREATE TABLE blind_reviews(
  run_id TEXT NOT NULL, opaque_review_id TEXT NOT NULL, label TEXT NOT NULL,
  rationale TEXT NOT NULL, hard_conflict_observed TEXT NOT NULL,
  reviewer TEXT NOT NULL, review_version TEXT NOT NULL, reviewed_at TEXT NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,opaque_review_id),
  FOREIGN KEY(run_id,opaque_review_id) REFERENCES sample_neighbourhoods(run_id,opaque_review_id),
  CHECK(label IN ('same','related','different','insufficient'))
);
CREATE TABLE adjudications(
  run_id TEXT NOT NULL, opaque_review_id TEXT NOT NULL, reviewer_label TEXT NOT NULL,
  adjudicator_label TEXT NOT NULL, agrees INTEGER NOT NULL CHECK(agrees IN (0,1)),
  final_gold_label TEXT NOT NULL, mapped_decision TEXT NOT NULL,
  rationale TEXT NOT NULL, adjudicator TEXT NOT NULL, adjudication_version TEXT NOT NULL,
  adjudicated_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,opaque_review_id),
  FOREIGN KEY(run_id,opaque_review_id) REFERENCES sample_neighbourhoods(run_id,opaque_review_id),
  CHECK(final_gold_label IN ('same','related','different','insufficient')),
  CHECK(mapped_decision IN ('merge','related_but_separate','reject','defer'))
);
CREATE TABLE validations(
  run_id TEXT NOT NULL, phase TEXT NOT NULL, check_name TEXT NOT NULL,
  passed INTEGER NOT NULL CHECK(passed IN (0,1)), expected_json TEXT NOT NULL,
  actual_json TEXT NOT NULL, detail_json TEXT NOT NULL, checked_at TEXT NOT NULL,
  PRIMARY KEY(run_id,phase,check_name)
);
CREATE TABLE metrics(
  run_id TEXT NOT NULL, phase TEXT NOT NULL, metric_name TEXT NOT NULL,
  metric_value INTEGER NOT NULL,
  PRIMARY KEY(run_id,phase,metric_name)
);
"""

LOGICAL_TABLES: tuple[tuple[str, str], ...] = (
    ("input_pins", "role"),
    ("source_buildings", "source,source_building_id"),
    ("building_image_stats", "source,source_building_id"),
    ("candidate_pairs", "left_source_building_id,right_source_building_id"),
    ("candidate_origins", "candidate_id,origin"),
    ("evidence_families", "candidate_id,family"),
    ("hard_conflicts", "candidate_id,conflict_type"),
    ("direct_image_evidence", "e2_evidence_id"),
    ("sample_neighbourhoods", "sample_rank"),
    ("sample_neighbourhood_pairs", "opaque_review_id,display_order"),
)


def stage_a_logical_sha256(connection: sqlite3.Connection, run_id: str) -> str:
    digest = hashlib.sha256()
    digest.update((LOGICAL_VERSION + "\n").encode("utf-8"))
    for table, order_by in LOGICAL_TABLES:
        digest.update((table + "\n").encode("utf-8"))
        for row in connection.execute(
            f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order_by}", (run_id,)
        ):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _insert_source_rows(
    database: sqlite3.Connection,
    run_id: str,
    buildings: Mapping[tuple[str, str], Mapping[str, Any]],
    image_stats: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    for key in sorted(buildings):
        row = buildings[key]
        payload = {
            "source": row["source"], "source_building_id": row["source_building_id"],
            "name": row["name"], "aliases": list(row["aliases"]), "firms": list(row["firms"]),
            "firm_keys": list(row["firm_keys"]), "country": row["country"],
            "country_normalized": row["country_norm"], "locality": row["locality"],
            "locality_normalized": row["locality_norm"], "completion_year_min": row["year_min"],
            "completion_year_max": row["year_max"], "source_urls": list(row["source_urls"]),
            "external_ids": list(row["external_ids"]),
            "e2_source_record_sha256": row["e2_source_record_sha256"],
        }
        database.execute(
            "INSERT INTO source_buildings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,row["source"],row["source_building_id"],row["name"],canonical_json(list(row["aliases"])),
                canonical_json(list(row["firms"])),canonical_json(list(row["firm_keys"])),row["country"],
                row["country_norm"],row["locality"],row["locality_norm"],row["year_min"],row["year_max"],
                canonical_json(list(row["source_urls"])),canonical_json(list(row["external_ids"])),
                row["e2_source_record_sha256"],_record_sha(payload),
            ),
        )
        stats = image_stats.get(key, {"asset_count": 0, "successful_asset_count": 0, "unique_pixel_count": 0, "representative": None})
        representative = stats.get("representative")
        stats_payload = {
            "source": key[0], "source_building_id": key[1], "asset_count": int(stats["asset_count"]),
            "successful_asset_count": int(stats["successful_asset_count"]),
            "unique_pixel_count": int(stats["unique_pixel_count"]), "representative": representative,
        }
        database.execute(
            "INSERT INTO building_image_stats VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,key[0],key[1],stats_payload["asset_count"],stats_payload["successful_asset_count"],
                stats_payload["unique_pixel_count"],
                representative.get("source_asset_id") if representative else None,
                canonical_json(representative) if representative else None,_record_sha(stats_payload),
            ),
        )


def _insert_candidates(
    database: sqlite3.Connection,
    run_id: str,
    buildings: Mapping[tuple[str, str], Mapping[str, Any]],
    image_stats: Mapping[tuple[str, str], Mapping[str, Any]],
    universe: Mapping[tuple[str, str], set[str]],
    e2_summaries: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    evaluated: dict[str, dict[str, Any]] = {}
    for left_id, right_id in sorted(universe):
        pair = (left_id, right_id)
        cid = candidate_id(left_id, right_id)
        left = buildings[("divisare", left_id)]
        right = buildings[("architizer", right_id)]
        e2_row = e2_summaries.get(pair, {})
        result = evaluate_candidate(
            left,right,set(universe[pair]),e2_row,
            image_stats.get(("divisare",left_id),{}),image_stats.get(("architizer",right_id),{}),
        )
        origins = set(result["origins"])
        counts = {
            "exact": int(e2_row.get("exact_asset_pair_count", 0) or 0),
            "identical": int(e2_row.get("identical_phash_pair_count", 0) or 0),
            "le8": int(e2_row.get("phash_le8_pair_count", 0) or 0),
            "weak": int(e2_row.get("phash_9_16_pair_count", 0) or 0),
        }
        candidate_payload = {
            "candidate_id": cid,"left_source_building_id":left_id,"right_source_building_id":right_id,
            "origins":sorted(origins),"e2_metadata_pair_id":e2_row.get("metadata_pair_id"),
            "e2_building_candidate_id":e2_row.get("e2_building_candidate_id"),"image_counts":counts,
            "min_phash_distance":e2_row.get("min_phash_distance"),
            "name_strict_score":round(result["name_strict"],6),"name_loose_score":round(result["name_loose"],6),
            "name_token_set_score":round(result["name_set"],6),"firm_strict_score":round(result["firm_strict"],6),
            "year_gap":result["year_gap"],"positive_family_count":result["positive_family_count"],
            "negative_family_count":result["negative_family_count"],"hard_conflict_count":result["hard_conflict_count"],
            "proposal":result["proposal"],"auto_merge_eligible":result["auto_merge_eligible"],
        }
        database.execute(
            "INSERT INTO candidate_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,cid,"divisare",left_id,"architizer",right_id,
                int("e2_metadata" in origins),int("e2_candidate" in origins),
                int("matcher_architect_year" in origins),int("matcher_pass2" in origins),len(origins),
                e2_row.get("metadata_pair_id"),e2_row.get("e2_building_candidate_id"),
                counts["exact"],counts["identical"],counts["le8"],counts["weak"],e2_row.get("min_phash_distance"),
                round(result["name_strict"],6),round(result["name_loose"],6),round(result["name_set"],6),
                round(result["firm_strict"],6),result["year_gap"],result["positive_family_count"],
                result["negative_family_count"],result["hard_conflict_count"],result["proposal"],
                result["auto_merge_eligible"],_record_sha(candidate_payload),
            ),
        )
        for origin in sorted(origins):
            detail = {
                "origin": origin,
                "pinned_matcher_sha256": FILE_PINS["building_matcher"][2] if origin.startswith("matcher_") else None,
                "e2_metadata_record_sha256": e2_row.get("e2_metadata_record_sha256") if origin == "e2_metadata" else None,
                "e2_candidate_record_sha256": e2_row.get("e2_candidate_record_sha256") if origin == "e2_candidate" else None,
            }
            payload = {"candidate_id":cid,"origin":origin,"detail":detail}
            database.execute(
                "INSERT INTO candidate_origins VALUES(?,?,?,?,?)",
                (run_id,cid,origin,canonical_json(detail),_record_sha(payload)),
            )
        for family in result["families"]:
            payload = {"candidate_id":cid,"family":family["family"],"status":family["status"],"score":family["score"],"detail":family["detail"]}
            database.execute(
                "INSERT INTO evidence_families VALUES(?,?,?,?,?,?,?)",
                (run_id,cid,family["family"],family["status"],family["score"],canonical_json(family["detail"]),_record_sha(payload)),
            )
        for conflict in result["conflicts"]:
            payload = {"candidate_id":cid,"conflict_type":conflict["conflict_type"],"detail":conflict["detail"]}
            database.execute(
                "INSERT INTO hard_conflicts VALUES(?,?,?,?,?)",
                (run_id,cid,conflict["conflict_type"],canonical_json(conflict["detail"]),_record_sha(payload)),
            )
        evaluated[cid] = {**result,"left_id":left_id,"right_id":right_id,"candidate_id":cid}
    return evaluated


def _insert_direct_image_evidence(
    database: sqlite3.Connection,
    run_id: str,
    e2: sqlite3.Connection,
    e2_summaries: Mapping[tuple[str, str], Mapping[str, Any]],
) -> int:
    e2_to_candidate = {
        str(summary["e2_building_candidate_id"]): candidate_id(pair[0], pair[1])
        for pair, summary in e2_summaries.items()
        if summary.get("e2_building_candidate_id")
    }
    count = 0
    for row in e2.execute(
        "SELECT evidence_id,building_candidate_id,evidence_kind,left_source,left_source_asset_id,"
        "right_source,right_source_asset_id,phash_distance,evidence_record_sha256 "
        "FROM candidate_image_evidence ORDER BY evidence_id"
    ):
        cid = e2_to_candidate.get(str(row["building_candidate_id"]))
        if cid is None:
            raise IdentityCandidateError("direct image evidence references unknown E2 candidate")
        if row["left_source"] == "divisare":
            left_asset, right_asset = str(row["left_source_asset_id"]), str(row["right_source_asset_id"])
        else:
            left_asset, right_asset = str(row["right_source_asset_id"]), str(row["left_source_asset_id"])
        payload = {
            "candidate_id":cid,"e2_evidence_id":str(row["evidence_id"]),"evidence_kind":str(row["evidence_kind"]),
            "left_source_asset_id":left_asset,"right_source_asset_id":right_asset,
            "phash_distance":row["phash_distance"],"e2_evidence_record_sha256":str(row["evidence_record_sha256"]),
        }
        database.execute(
            "INSERT INTO direct_image_evidence VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,cid,payload["e2_evidence_id"],payload["evidence_kind"],left_asset,right_asset,
                payload["phash_distance"],payload["e2_evidence_record_sha256"],_record_sha(payload),
            ),
        )
        count += 1
    return count


def choose_n10(evaluated: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used_left: set[str] = set()
    used_right: set[str] = set()

    def add_from_pool(stratum: str, count: int, predicate: Any, preference: Any) -> None:
        pool = [row for row in evaluated.values() if predicate(row)]
        pool.sort(key=lambda row: (preference(row), stable_hash(SAMPLE_SEED,stratum,str(row["candidate_id"]))))
        for row in pool:
            if row["left_id"] in used_left or row["right_id"] in used_right:
                continue
            selected.append({"stratum":stratum,**row})
            used_left.add(str(row["left_id"]));used_right.add(str(row["right_id"]))
            if sum(item["stratum"] == stratum for item in selected) == count:
                return
        raise IdentityCandidateError(f"insufficient distinct candidates for N10 stratum: {stratum}")

    add_from_pool(
        "strong_merge",4,
        lambda row: row["proposal"] == "merge_candidate" and row["strong_image_count"] > 0 and row["name_strict"] >= 90,
        lambda row: (-int(row["positive_family_count"]),-int(row["strong_image_count"])),
    )
    add_from_pool(
        "metadata_only",2,
        lambda row: "e2_metadata" in row["origins"] and row["strong_image_count"] == 0,
        lambda row: (-int(row["positive_family_count"]),int(row["hard_conflict_count"])),
    )
    add_from_pool(
        "image_evidence_only",2,
        lambda row: "e2_candidate" in row["origins"] and "e2_metadata" not in row["origins"] and row["strong_image_count"] > 0,
        lambda row: (int("matcher_architect_year" in row["origins"] or "matcher_pass2" in row["origins"]),-int(row["strong_image_count"])),
    )
    add_from_pool(
        "conflict_boundary",2,
        lambda row: row["hard_conflict_count"] > 0 or (row["proposal"] == "defer" and row["positive_family_count"] == 1),
        lambda row: (0 if row["hard_conflict_count"] > 0 else 1,int(row["hard_conflict_count"]),-int(row["positive_family_count"])),
    )
    if len(selected) != 10:
        raise AssertionError(len(selected))
    return selected


def _insert_sample(database: sqlite3.Connection, run_id: str, selected: Sequence[Mapping[str, Any]]) -> None:
    frozen_at = utc_now()
    for rank, row in enumerate(selected, 1):
        cid = str(row["candidate_id"])
        opaque = "blind_" + stable_hash(SAMPLE_SEED, "opaque", cid)[:24]
        seed_rank = stable_hash(SAMPLE_SEED, str(row["stratum"]), cid)
        payload = {"sample_rank":rank,"stratum":row["stratum"],"candidate_id":cid,"opaque_review_id":opaque,"seed_rank_sha256":seed_rank}
        database.execute(
            "INSERT INTO sample_neighbourhoods VALUES(?,?,?,?,?,?,?,?)",
            (run_id,rank,row["stratum"],cid,opaque,frozen_at,seed_rank,_record_sha(payload)),
        )
        focal = database.execute(
            "SELECT left_source_building_id,right_source_building_id FROM candidate_pairs WHERE run_id=? AND candidate_id=?",
            (run_id,cid),
        ).fetchone()
        neighbours: list[tuple[str,str]] = [(cid,"focal")]
        rows = database.execute(
            "SELECT candidate_id,left_source_building_id,right_source_building_id FROM candidate_pairs "
            "WHERE run_id=? AND candidate_id<>? AND (left_source_building_id=? OR right_source_building_id=?)",
            (run_id,cid,focal[0],focal[1]),
        ).fetchall()
        contextual = []
        for candidate in rows:
            relationship = "shares_left" if candidate[1] == focal[0] else "shares_right"
            contextual.append((stable_hash(SAMPLE_SEED,opaque,str(candidate[0])),str(candidate[0]),relationship))
        for _score, neighbour_cid, relationship in sorted(contextual)[:4]:
            neighbours.append((neighbour_cid,relationship))
        for order,(neighbour_cid,relationship) in enumerate(neighbours,1):
            pair_payload = {"opaque_review_id":opaque,"display_order":order,"candidate_id":neighbour_cid,"relationship":relationship}
            database.execute(
                "INSERT INTO sample_neighbourhood_pairs VALUES(?,?,?,?,?,?)",
                (run_id,opaque,order,neighbour_cid,relationship,_record_sha(pair_payload)),
            )


def _distribution(connection: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in connection.execute(query, params)}


def build_identity_candidates(
    *,
    repo_root: Path,
    output_path: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST,
    report_path: Path = DEFAULT_REPORT,
    freeze_path: Path = DEFAULT_FREEZE,
    verify_full_hashes: bool = True,
) -> BuildResult:
    started_clock = time.monotonic()
    root = repo_root.resolve()
    output = output_path if output_path.is_absolute() else root / output_path
    manifest = manifest_path if manifest_path.is_absolute() else root / manifest_path
    report = report_path if report_path.is_absolute() else root / report_path
    freeze = freeze_path if freeze_path.is_absolute() else root / freeze_path
    for path in (output,manifest,report):
        if path.exists():
            raise IdentityCandidateError(f"refusing to clobber existing artifact: {path}")
    output.parent.mkdir(parents=True,exist_ok=True);manifest.parent.mkdir(parents=True,exist_ok=True);report.parent.mkdir(parents=True,exist_ok=True)
    temporary = output.with_name(output.name + ".building")
    if temporary.exists():
        raise IdentityCandidateError(f"stale temporary artifact exists: {temporary}")

    pins = _verify_pins(root,freeze,full_hashes=verify_full_hashes)
    normalizer_sha = canonical_sha256(NORMALIZER_SPEC)
    policy_sha = canonical_sha256(POLICY_SPEC)
    run_material = {
        "builder_version":BUILDER_VERSION,"normalizer_sha256":normalizer_sha,"policy_sha256":policy_sha,
        "sample_seed":SAMPLE_SEED,"input_sha256":{role:row["sha256_before"] for role,row in sorted(pins.items())},
    }
    run_id = "wp2-" + canonical_sha256(run_material)[:24]
    started_at = utc_now()
    database: sqlite3.Connection | None = None
    source_connections: list[sqlite3.Connection] = []
    try:
        divisare = _open_immutable(root / DEFAULT_DIVISARE)
        architizer = _open_immutable(root / DEFAULT_ARCHITIZER)
        e2 = _open_immutable(root / DEFAULT_E2)
        source_connections = [divisare,architizer,e2]
        buildings = load_source_buildings(divisare,architizer,e2)
        image_stats = load_image_stats(e2)
        universe,e2_summaries = build_candidate_universe(buildings,e2)

        database = sqlite3.connect(temporary)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(SCHEMA_SQL)
        database.execute(
            "INSERT INTO runs(run_id,contract_version,builder_version,blocker_version,policy_version,normalizer_version,"
            "sample_version,sample_seed,status,started_at,normalizer_sha256,policy_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id,CONTRACT_VERSION,BUILDER_VERSION,BLOCKER_VERSION,POLICY_VERSION,NORMALIZER_VERSION,SAMPLE_VERSION,SAMPLE_SEED,"building",started_at,normalizer_sha,policy_sha),
        )
        for role,row in sorted(pins.items()):
            usage = "candidate_and_evidence" if role in INPUT_SPECS or role in {"building_matcher","phash_matcher","identity_contract"} else "pin_manifest_only"
            payload = {"role":role,"path":row["path"],"size_bytes":row["size_bytes"],"sha256_before":row["sha256_before"],"usage":usage}
            database.execute(
                "INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)",
                (run_id,role,row["path"],row["size_bytes"],row["sha256_before"],None,usage,_record_sha(payload)),
            )
        _insert_source_rows(database,run_id,buildings,image_stats)
        evaluated = _insert_candidates(database,run_id,buildings,image_stats,universe,e2_summaries)
        direct_count = _insert_direct_image_evidence(database,run_id,e2,e2_summaries)
        selected = choose_n10(evaluated)
        _insert_sample(database,run_id,selected)
        database.commit()

        if len(buildings) != EXPECTED_SOURCE_TOTAL or len(evaluated) != len(universe):
            raise IdentityCandidateError("Stage A accounting mismatch")
        if database.execute("SELECT count(*) FROM evidence_families WHERE run_id=?",(run_id,)).fetchone()[0] != len(evaluated)*len(EVIDENCE_FAMILIES):
            raise IdentityCandidateError("six-family accounting mismatch")
        if database.execute("SELECT count(*) FROM sample_neighbourhoods WHERE run_id=?",(run_id,)).fetchone()[0] != 10:
            raise IdentityCandidateError("N10 sample accounting mismatch")
        if database.execute("SELECT count(*) FROM candidate_pairs WHERE auto_merge_eligible=1 AND hard_conflict_count<>0").fetchone()[0]:
            raise IdentityCandidateError("auto-merge eligibility violates hard conflict gate")

        for connection in source_connections:
            connection.close()
        source_connections=[]
        for role,(relative,_size,expected) in INPUT_SPECS.items():
            actual = sha256_file(root / relative) if verify_full_hashes else expected
            if actual != pins[role]["sha256_before"]:
                raise IdentityCandidateError(f"frozen input changed during build: {role}")
            pins[role]["sha256_after"] = actual
        for role in FILE_PINS:
            actual = sha256_file(root / FILE_PINS[role][0])
            if actual != pins[role]["sha256_before"]:
                raise IdentityCandidateError(f"pinned contract/matcher changed during build: {role}")
            pins[role]["sha256_after"] = actual
        freeze_after = sha256_file(freeze)
        if freeze_after != pins["workspace_freeze_manifest"]["sha256_before"]:
            raise IdentityCandidateError("workspace freeze manifest changed during build")
        pins["workspace_freeze_manifest"]["sha256_after"] = freeze_after
        for role,row in sorted(pins.items()):
            usage = database.execute("SELECT usage FROM input_pins WHERE run_id=? AND role=?",(run_id,role)).fetchone()[0]
            payload = {"role":role,"path":row["path"],"size_bytes":row["size_bytes"],"sha256_before":row["sha256_before"],"sha256_after":row["sha256_after"],"usage":usage}
            database.execute(
                "UPDATE input_pins SET sha256_after=?,record_sha256=? WHERE run_id=? AND role=?",
                (row["sha256_after"],_record_sha(payload),run_id,role),
            )
        logical = stage_a_logical_sha256(database,run_id)
        auto_count = int(database.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=? AND auto_merge_eligible=1",(run_id,)).fetchone()[0])
        completed_at = utc_now()
        database.execute(
            "UPDATE runs SET status='stage_a_complete',completed_at=?,source_building_count=?,candidate_count=?,stage_a_logical_sha256=? WHERE run_id=?",
            (completed_at,len(buildings),len(evaluated),logical,run_id),
        )
        for metric,value in (
            ("source_buildings",len(buildings)),("candidate_pairs",len(evaluated)),("direct_image_evidence",direct_count),
            ("auto_merge_eligible",auto_count),("n10_sample",10),("network_requests",0),("vision_requests",0),
            ("llm_requests",0),("legacy_snapshot_reads",0),("merge_executions",0),("vnx_id_issuances",0),
        ):
            database.execute("INSERT INTO metrics VALUES(?,?,?,?)",(run_id,"stage_a",metric,value))
        database.commit()
        integrity = str(database.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise IdentityCandidateError(f"output integrity_check failed: {integrity}")
        database.close();database=None
        os.replace(temporary,output)

        manifest_payload = {
            "status":"PASS","run_id":run_id,"contract_version":CONTRACT_VERSION,"builder_version":BUILDER_VERSION,
            "blocker_version":BLOCKER_VERSION,"policy_version":POLICY_VERSION,"policy_sha256":policy_sha,
            "normalizer_version":NORMALIZER_VERSION,"normalizer_sha256":normalizer_sha,
            "normalizer_spec":NORMALIZER_SPEC,"policy_spec":POLICY_SPEC,"sample_version":SAMPLE_VERSION,
            "sample_seed":SAMPLE_SEED,"stage_a_logical_sha256":logical,
            "input_pins":pins,
            "architect_registry_note":"No id_registry_architects artifact is present in the frozen workspace. The pinned building matcher blocking is replayed on the curated population with exact, strong normalized firm keys; the normalizer and matcher SHA are both fixed above.",
            "forbidden_actions":{"network_requests":0,"vision_requests":0,"llm_requests":0,"legacy_snapshot_reads":0,"merge_executions":0,"vnx_id_issuances":0},
        }
        _atomic_json(manifest,manifest_payload)

        check = _open_immutable(output)
        report_payload = {
            "status":"PASS","run_id":run_id,"stage":"WP2 Stage A","stage_a_logical_sha256":logical,
            "population":{"divisare":EXPECTED_SOURCE_COUNTS["divisare"],"architizer":EXPECTED_SOURCE_COUNTS["architizer"],"total":EXPECTED_SOURCE_TOTAL},
            "candidate_count":len(evaluated),
            "origin_counts":_distribution(check,"SELECT origin,count(*) FROM candidate_origins WHERE run_id=? GROUP BY origin ORDER BY origin",(run_id,)),
            "origin_exclusive_counts":{
                "e2_metadata_only":int(check.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=? AND origin_e2_metadata=1 AND origin_matcher_architect_year=0 AND origin_matcher_pass2=0",(run_id,)).fetchone()[0]),
                "e2_image_only":int(check.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=? AND origin_e2_metadata=0 AND origin_e2_candidate=1 AND origin_matcher_architect_year=0 AND origin_matcher_pass2=0",(run_id,)).fetchone()[0]),
                "matcher_only":int(check.execute("SELECT count(*) FROM candidate_pairs WHERE run_id=? AND origin_e2_candidate=0",(run_id,)).fetchone()[0]),
            },
            "family_status_counts":{
                family:_distribution(check,"SELECT status,count(*) FROM evidence_families WHERE run_id=? AND family=? GROUP BY status ORDER BY status",(run_id,family))
                for family in EVIDENCE_FAMILIES
            },
            "proposal_counts":_distribution(check,"SELECT proposal,count(*) FROM candidate_pairs WHERE run_id=? GROUP BY proposal ORDER BY proposal",(run_id,)),
            "auto_merge_eligible_count":auto_count,
            "hard_conflict_type_counts":_distribution(check,"SELECT conflict_type,count(*) FROM hard_conflicts WHERE run_id=? GROUP BY conflict_type ORDER BY conflict_type",(run_id,)),
            "n10_strata":_distribution(check,"SELECT stratum,count(*) FROM sample_neighbourhoods WHERE run_id=? GROUP BY stratum ORDER BY stratum",(run_id,)),
            "accounting":{
                "e2_metadata_pairs":EXPECTED_E2_METADATA_PAIRS,"e2_cross_source_candidates":EXPECTED_E2_CANDIDATES,
                "six_family_rows":len(evaluated)*6,"direct_image_evidence_rows":direct_count,
                "component_edges_created":0,"registries_created":0,"new_ids_issued":0,
            },
            "cost":{"network_requests":0,"paid_api_requests":0,"vision_requests":0,"llm_requests":0,"estimated_usd":0.0},
        }
        check.close()
        _atomic_json(report,report_payload)
        return BuildResult(
            output_path=output,manifest_path=manifest,report_path=report,run_id=run_id,
            logical_sha256=logical,source_building_count=len(buildings),candidate_count=len(evaluated),
            auto_merge_eligible_count=auto_count,sample_count=10,elapsed_seconds=time.monotonic()-started_clock,
        )
    except Exception:
        if database is not None:
            database.close()
        for connection in source_connections:
            connection.close()
        if temporary.exists():
            temporary.unlink()
        raise


__all__ = [
    "BLOCKER_VERSION","BUILDER_VERSION","CONTRACT_VERSION","DEFAULT_FREEZE","DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT","DEFAULT_REPORT","EVIDENCE_FAMILIES","LOGICAL_TABLES","NORMALIZER_SPEC",
    "NORMALIZER_VERSION","POLICY_SPEC","POLICY_VERSION","SAMPLE_SEED","SAMPLE_VERSION","SCHEMA_SQL",
    "BuildResult","IdentityCandidateError","best_similarity","build_candidate_universe",
    "build_identity_candidates","candidate_id","choose_n10","distinctive_name_tokens","evaluate_candidate",
    "firm_core","firm_keys","normalize_country","normalize_text","sha256_file","stage_a_logical_sha256",
]
