#!/usr/bin/env python
"""Run or exactly resume the approved fixed cross-source semantic Vision N10."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_coverage_sources import (  # noqa: E402
    DEFAULT_E2_RELATIVE_PATH,
    DEFAULT_E3_RELATIVE_PATH,
)
from canonical.cross_source_semantic_vision_sidecar import (  # noqa: E402
    FROZEN_MANIFEST_BYTE_SHA256 as SIDECAR_MANIFEST_BYTE_SHA256,
    FROZEN_MANIFEST_SELF_SHA256 as SIDECAR_MANIFEST_SELF_SHA256,
    run_semantic_vision_n10,
)
from canonical.divisare_vision_runtime import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
)


DEFAULT_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n10_v1.json")
DEFAULT_OUTPUT = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1.db")
DEFAULT_REPORT = Path("data/reports/divisare_architizer_semantic_vision_n10_v1.md")

CANONICAL_MANIFEST_BYTE_SHA256 = (
    "81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f"
)
CANONICAL_MANIFEST_SELF_SHA256 = (
    "bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b"
)
CANONICAL_SAMPLE_SEED = "archibe-semantic-coverage-n10-v1"
CANONICAL_BUILDING_COUNT = 10
CANONICAL_OCCURRENCE_COUNT = 57
CANONICAL_E1_IDENTITY_COUNT = 57
CANONICAL_POPULATION_MANIFEST_SHA256 = (
    "63b859f6db5992bbc4d604743420ab3ae27a6efda266080545a03fdcdbd2f4e6"
)
CANONICAL_ORDERED_BUILDING_MANIFEST_SHA256 = (
    "8a23bde765b2eee340a950b430371468342bca938cdf9ade90dbb3047b75048b"
)
CANONICAL_ORDERED_OCCURRENCE_MANIFEST_SHA256 = (
    "e7fb4750fff10b544c3e62e7c3694978bdcb665275454fd7605556be9cd78e49"
)
MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-manifest-v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _verify_canonical_manifest(path: Path) -> dict[str, Any]:
    """Reject every manifest except the byte-exact approved N10 population."""

    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    byte_sha256 = hashlib.sha256(raw).hexdigest()
    if byte_sha256 != CANONICAL_MANIFEST_BYTE_SHA256:
        raise ValueError(
            "canonical N10 manifest byte SHA mismatch: "
            f"expected {CANONICAL_MANIFEST_BYTE_SHA256}, got {byte_sha256}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canonical N10 manifest is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("canonical N10 manifest root must be an object")
    if raw != (_canonical_json(payload) + "\n").encode("utf-8"):
        raise ValueError("canonical N10 manifest encoding is not canonical JSON + LF")

    body = dict(payload)
    stored_self_sha256 = body.pop("semantic_coverage_manifest_sha256", None)
    replayed_self_sha256 = _canonical_sha256(
        {"domain": MANIFEST_DOMAIN, "manifest": body}
    )
    population = payload.get("population")
    population_sha256 = (
        population.get("inventory_manifest_sha256")
        if isinstance(population, dict)
        else None
    )
    identities = {
        "semantic_coverage_manifest_sha256": (
            stored_self_sha256,
            CANONICAL_MANIFEST_SELF_SHA256,
        ),
        "replayed_semantic_coverage_manifest_sha256": (
            replayed_self_sha256,
            CANONICAL_MANIFEST_SELF_SHA256,
        ),
        "sample_seed": (payload.get("sample_seed"), CANONICAL_SAMPLE_SEED),
        "sample_size_buildings": (
            payload.get("sample_size_buildings"),
            CANONICAL_BUILDING_COUNT,
        ),
        "planned_occurrence_count": (
            payload.get("planned_occurrence_count"),
            CANONICAL_OCCURRENCE_COUNT,
        ),
        "planned_unique_e1_pixel_count": (
            payload.get("planned_unique_e1_pixel_count"),
            CANONICAL_E1_IDENTITY_COUNT,
        ),
        "population.inventory_manifest_sha256": (
            population_sha256,
            CANONICAL_POPULATION_MANIFEST_SHA256,
        ),
        "ordered_building_manifest_sha256": (
            payload.get("ordered_building_manifest_sha256"),
            CANONICAL_ORDERED_BUILDING_MANIFEST_SHA256,
        ),
        "ordered_occurrence_manifest_sha256": (
            payload.get("ordered_occurrence_manifest_sha256"),
            CANONICAL_ORDERED_OCCURRENCE_MANIFEST_SHA256,
        ),
    }
    mismatches = {
        name: {"actual": actual, "expected": expected}
        for name, (actual, expected) in identities.items()
        if actual != expected
    }
    if mismatches:
        raise ValueError(
            "canonical N10 manifest identity mismatch: "
            + json.dumps(mismatches, ensure_ascii=True, sort_keys=True)
        )

    selected_buildings = payload.get("selected_buildings")
    if not isinstance(selected_buildings, list) or len(selected_buildings) != CANONICAL_BUILDING_COUNT:
        raise ValueError("canonical N10 manifest selected-building population mismatch")
    try:
        occurrence_count = sum(
            len(row["selected_building"]["coverage_plan"]["selected_occurrences"])
            for row in selected_buildings
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("canonical N10 manifest occurrence population is malformed") from exc
    if occurrence_count != CANONICAL_OCCURRENCE_COUNT:
        raise ValueError("canonical N10 manifest selected-occurrence population mismatch")

    if SIDECAR_MANIFEST_BYTE_SHA256 != CANONICAL_MANIFEST_BYTE_SHA256:
        raise RuntimeError("runner and CLI frozen manifest byte SHA constants diverged")
    if SIDECAR_MANIFEST_SELF_SHA256 != CANONICAL_MANIFEST_SELF_SHA256:
        raise RuntimeError("runner and CLI frozen manifest self SHA constants diverged")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch exactly the frozen 57-image/10-building semantic N10, "
            "verify E1 byte and pixel identities, and run metadata-blind Vision."
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--e2", type=Path, default=DEFAULT_E2_RELATIVE_PATH)
    parser.add_argument("--e3", type=Path, default=DEFAULT_E3_RELATIVE_PATH)
    parser.add_argument("--output-db", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--codex-bin", type=Path, default=Path("codex"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--service-tier", default=DEFAULT_SERVICE_TIER)
    parser.add_argument("--cli-version")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume only the exact running .partial sidecar, or inspect an already completed output with zero requests.",
    )
    parser.add_argument(
        "--review-cache-dir",
        type=Path,
        help=(
            "Optionally retain verified 1024px derivatives under opaque names "
            "for the approved blind manual audit. The path is not stored in SQLite."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _verify_canonical_manifest(args.manifest)
        result = run_semantic_vision_n10(
            manifest_path=args.manifest,
            e2_path=args.e2,
            e3_path=args.e3,
            output_db=args.output_db,
            report_path=args.report,
            codex_bin=args.codex_bin,
            model=args.model,
            reasoning=args.reasoning,
            service_tier=args.service_tier,
            cli_version=args.cli_version,
            resume=args.resume,
            review_cache_dir=args.review_cache_dir,
            expected_manifest_byte_sha256=CANONICAL_MANIFEST_BYTE_SHA256,
            expected_manifest_self_sha256=CANONICAL_MANIFEST_SELF_SHA256,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "status": "error",
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "already_complete": result.already_complete,
                "logical_sha256": result.logical_sha256,
                "metrics": result.metrics,
                "output_db": str(result.output_db),
                "report": str(result.report_path),
                "requests_made": result.requests_made,
                "resumed": result.resumed,
                "status": result.status,
                "vision_requests": result.vision_requests,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
