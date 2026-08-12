#!/usr/bin/env python
"""Run or exactly resume the approved frozen cross-source semantic N100."""

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


from canonical.cross_source_image_selection import canonical_json, canonical_sha256  # noqa: E402
from canonical.cross_source_semantic_coverage_n100 import (  # noqa: E402
    SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN,
)
from canonical.cross_source_semantic_coverage_sources import (  # noqa: E402
    DEFAULT_E2_RELATIVE_PATH,
    DEFAULT_E3_RELATIVE_PATH,
)
from canonical.cross_source_semantic_vision_n100_sidecar import (  # noqa: E402
    FROZEN_N10_SIDECAR_SHA256,
    MAX_TOTAL_INPUT_TOKENS,
    MAX_TOTAL_OUTPUT_TOKENS,
    MAX_TOTAL_TOKENS,
    MAX_TOTAL_VISION_RETRIES,
    RuntimeContract,
    run_semantic_vision_n100,
)
from canonical.cross_source_semantic_vision_n100_validator import (  # noqa: E402
    validate_frozen_n100_manifest_preflight,
)
from canonical.divisare_vision_runtime import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
)


DEFAULT_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n100_v1.json")
DEFAULT_N10_SIDECAR = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n10_v1.db"
)
DEFAULT_N10_MANIFEST = Path(
    "data/reports/cross_source_semantic_coverage_n10_v1.json"
)
DEFAULT_OUTPUT = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n100_v1.db"
)
DEFAULT_REPORT = Path(
    "data/reports/divisare_architizer_semantic_vision_n100_v1.md"
)
DEFAULT_REVIEW_CACHE = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n100_v1_review_cache"
)

CANONICAL_MANIFEST_BYTE_SHA256 = (
    "37d917dc2c5b7c89308641b97035255e1fab5e469a0fc86935bf716559ead540"
)
CANONICAL_MANIFEST_SELF_SHA256 = (
    "9d34f68ceba5dc561169ed5c3b6fecfc9284dc36fb6ffd544be9198f9faf80ec"
)
CANONICAL_SAMPLE_SEED = "archibe-semantic-coverage-n100-v1"
CANONICAL_BUILDING_COUNT = 100
CANONICAL_OCCURRENCE_COUNT = 562
CANONICAL_E1_IDENTITY_COUNT = 562
CANONICAL_ORDERED_BUILDING_MANIFEST_SHA256 = (
    "edfc89773225828378c56e7cb7c9a9ef8bb5768ed91c50dc03ae464ec6299802"
)
CANONICAL_ORDERED_OCCURRENCE_MANIFEST_SHA256 = (
    "f8492352487b33540dd25861f299f0ea55079f4fd9395899257d52de7c487e8b"
)
CANONICAL_ORDERED_FETCH_URL_MANIFEST_SHA256 = (
    "c43fd188322ace59378bb23d0e0b14bca4da172c3109b25273451a60d69e71a7"
)
CANONICAL_ORDERED_E1_PIXEL_MANIFEST_SHA256 = (
    "9d1332006d72a59b40e1d3c1a66e5abe8cd0f719207e7be0ef765058b4654b62"
)

CONTRACT = RuntimeContract(
    manifest_byte_sha256=CANONICAL_MANIFEST_BYTE_SHA256,
    manifest_self_sha256=CANONICAL_MANIFEST_SELF_SHA256,
    sample_seed=CANONICAL_SAMPLE_SEED,
    building_count=CANONICAL_BUILDING_COUNT,
    occurrence_count=CANONICAL_OCCURRENCE_COUNT,
    ordered_building_manifest_sha256=CANONICAL_ORDERED_BUILDING_MANIFEST_SHA256,
    ordered_occurrence_manifest_sha256=CANONICAL_ORDERED_OCCURRENCE_MANIFEST_SHA256,
)
CANONICAL_PLANNED_BATCH_COUNT = 113
CANONICAL_MAX_TOTAL_VISION_RETRIES = 12
CANONICAL_MAX_APPROVED_MODEL_CALLS = 125
CANONICAL_MAX_TOTAL_INPUT_TOKENS = 2_600_000
CANONICAL_MAX_TOTAL_OUTPUT_TOKENS = 100_000
CANONICAL_MAX_TOTAL_TOKENS = 2_700_000
MAX_APPROVED_MODEL_CALLS = CANONICAL_MAX_APPROVED_MODEL_CALLS


def _assert_approved_envelope() -> None:
    actual = {
        "planned_batches": CONTRACT.planned_batch_count,
        "vision_retry_cap": MAX_TOTAL_VISION_RETRIES,
        "model_call_cap": CONTRACT.planned_batch_count + MAX_TOTAL_VISION_RETRIES,
        "input_tokens": MAX_TOTAL_INPUT_TOKENS,
        "output_tokens": MAX_TOTAL_OUTPUT_TOKENS,
        "total_tokens": MAX_TOTAL_TOKENS,
    }
    expected = {
        "planned_batches": CANONICAL_PLANNED_BATCH_COUNT,
        "vision_retry_cap": CANONICAL_MAX_TOTAL_VISION_RETRIES,
        "model_call_cap": CANONICAL_MAX_APPROVED_MODEL_CALLS,
        "input_tokens": CANONICAL_MAX_TOTAL_INPUT_TOKENS,
        "output_tokens": CANONICAL_MAX_TOTAL_OUTPUT_TOKENS,
        "total_tokens": CANONICAL_MAX_TOTAL_TOKENS,
    }
    if actual != expected:
        raise RuntimeError(
            "N100 approved call/token envelope mismatch: "
            + json.dumps(
                {"actual": actual, "expected": expected}, sort_keys=True
            )
        )


def _verify_frozen_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    byte_sha = hashlib.sha256(raw).hexdigest()
    if byte_sha != CANONICAL_MANIFEST_BYTE_SHA256:
        raise ValueError(
            f"frozen N100 manifest byte SHA mismatch: {byte_sha}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("frozen N100 manifest is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("frozen N100 manifest root is not an object")
    if raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise ValueError("frozen N100 manifest is not canonical JSON plus LF")
    body = dict(payload)
    stored_self = body.pop("semantic_coverage_manifest_sha256", None)
    replayed_self = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN, "manifest": body}
    )
    pins = {
        "self": (stored_self, CANONICAL_MANIFEST_SELF_SHA256),
        "replayed_self": (replayed_self, CANONICAL_MANIFEST_SELF_SHA256),
        "seed": (payload.get("sample_seed"), CANONICAL_SAMPLE_SEED),
        "buildings": (
            payload.get("sample_size_buildings"),
            CANONICAL_BUILDING_COUNT,
        ),
        "occurrences": (
            payload.get("planned_occurrence_count"),
            CANONICAL_OCCURRENCE_COUNT,
        ),
        "unique_e1": (
            payload.get("planned_unique_e1_pixel_count"),
            CANONICAL_E1_IDENTITY_COUNT,
        ),
        "ordered_buildings": (
            payload.get("ordered_building_manifest_sha256"),
            CANONICAL_ORDERED_BUILDING_MANIFEST_SHA256,
        ),
        "ordered_occurrences": (
            payload.get("ordered_occurrence_manifest_sha256"),
            CANONICAL_ORDERED_OCCURRENCE_MANIFEST_SHA256,
        ),
        "ordered_urls": (
            payload.get("ordered_fetch_url_manifest_sha256"),
            CANONICAL_ORDERED_FETCH_URL_MANIFEST_SHA256,
        ),
        "ordered_e1_pixels": (
            payload.get("ordered_e1_pixel_manifest_sha256"),
            CANONICAL_ORDERED_E1_PIXEL_MANIFEST_SHA256,
        ),
    }
    mismatches = {
        key: {"actual": actual, "expected": expected}
        for key, (actual, expected) in pins.items()
        if actual != expected
    }
    if mismatches:
        raise ValueError(
            "frozen N100 manifest pins mismatch: "
            + json.dumps(mismatches, ensure_ascii=True, sort_keys=True)
        )
    _assert_approved_envelope()
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch the frozen 562-image/100-building N100, verify exact E1 and "
            "N10-disjoint derivative identities, and run metadata-blind Vision."
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--e2", type=Path, default=DEFAULT_E2_RELATIVE_PATH)
    parser.add_argument("--e3", type=Path, default=DEFAULT_E3_RELATIVE_PATH)
    parser.add_argument("--n10-sidecar", type=Path, default=DEFAULT_N10_SIDECAR)
    parser.add_argument(
        "--n10-manifest", type=Path, default=DEFAULT_N10_MANIFEST
    )
    parser.add_argument("--output-db", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--review-cache-dir", type=Path, default=DEFAULT_REVIEW_CACHE
    )
    parser.add_argument("--codex-bin", type=Path, default=Path("codex"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--service-tier", default=DEFAULT_SERVICE_TIER)
    parser.add_argument("--cli-version")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume only the exact running .partial sidecar, or validate an "
            "already completed output with zero HTTP/Vision requests."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _assert_approved_envelope()
        _verify_frozen_manifest(args.manifest)
        preflight = validate_frozen_n100_manifest_preflight(
            args.manifest,
            e2_path=args.e2,
            e3_path=args.e3,
            n10_manifest_path=args.n10_manifest,
            n10_sidecar_path=args.n10_sidecar,
        )
        result = run_semantic_vision_n100(
            contract=CONTRACT,
            manifest_path=args.manifest,
            e2_path=args.e2,
            e3_path=args.e3,
            n10_sidecar_path=args.n10_sidecar,
            output_db=args.output_db,
            report_path=args.report,
            codex_bin=args.codex_bin,
            model=args.model,
            reasoning=args.reasoning,
            service_tier=args.service_tier,
            cli_version=args.cli_version,
            resume=args.resume,
            review_cache_dir=args.review_cache_dir,
            expected_n10_sidecar_sha256=FROZEN_N10_SIDECAR_SHA256,
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
                "approved_envelope": {
                    "input_tokens": MAX_TOTAL_INPUT_TOKENS,
                    "model_calls": MAX_APPROVED_MODEL_CALLS,
                    "output_tokens": MAX_TOTAL_OUTPUT_TOKENS,
                    "total_tokens": MAX_TOTAL_TOKENS,
                },
                "logical_sha256": result.logical_sha256,
                "manifest_preflight_passed": preflight["passed"],
                "metrics": result.metrics,
                "output_db": str(result.output_db),
                "report": str(result.report_path),
                "requests_made": result.requests_made,
                "resumed": result.resumed,
                "review_cache_dir": str(args.review_cache_dir),
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
