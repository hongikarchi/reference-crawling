"""Stage blinded 1024-pixel inputs for the Divisare axes review.

The development manifest intentionally stores reviewer-safe rows separately
from source audit data.  This module joins those views only long enough to
fetch and prepare each image, then publishes a directory whose names and JSON
contain opaque review IDs and image hashes only.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from canonical import divisare_vision_axes_devset as devset
from canonical import divisare_vision_axes_holdout_selection as holdout_selection
from canonical.divisare_image_smoke import FetchPayload, network_fetch
from canonical.divisare_vision_benchmark import (
    _pixel_sha256,
    decode_source,
    prepare_derivative,
)
from canonical.divisare_vision_gold import SOURCE_PROFILE
from canonical.divisare_vision_gold_finalize import (
    canonical_json_bytes,
    parse_json_strict,
)


EXPECTED_MANIFEST_FILENAME = "divisare_vision_axes_dev_n50_candidates_v1.json"
EXPECTED_MANIFEST_FILE_SHA256 = (
    "8bddc0cf1210c0fc63390943b8802ca194c927ff2454d292851b9ed87cb1cc5a"
)
EXPECTED_MANIFEST_LOGICAL_SHA256 = (
    "7acf3e0cb18fb951511ef08c2b24a1e16bcb179f11d10a697d7ad06e06353913"
)
EXPECTED_MANIFEST_SHA256 = (
    "414f74db5530e320013a62b7e0056f29af8de5ff62a9db49c6daf672f9341a29"
)
EXPECTED_HOLDOUT_MANIFEST_FILENAME = (
    "divisare_vision_axes_holdout_n50_candidates_v1_1.json"
)
EXPECTED_HOLDOUT_MANIFEST_FILE_SHA256 = (
    "60c66722a5dc4f133687ec0b7d665487d7889c901256694e22d7d0e23dcb7fcb"
)
EXPECTED_HOLDOUT_MANIFEST_LOGICAL_SHA256 = (
    "d8a2666f187ddfec563a170bdd7a6497ff88c21373e9090b932b76013f880422"
)
EXPECTED_HOLDOUT_MANIFEST_SHA256 = (
    "415715652524c5b7714335c5065563e9fd48283aa629fa74c09c2f1020ef6ea7"
)
REVIEW_INPUTS_FILENAME = "review_inputs.json"
LANE = "long1024"
MAX_LONG_EDGE = 1024
SUBSET_LIMITS = {"n10": 10, "n20": 20, "n50": 50, "all": 50}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVIEW_ID_RE = re.compile(r"^axis-[0-9a-f]{12}$")
_PUBLIC_TOP_LEVEL_FIELDS = frozenset(
    {
        "manifest_file_sha256",
        "manifest_logical_sha256",
        "review_rows",
    }
)
_PUBLIC_ROW_FIELDS = frozenset(
    {
        "review_rank",
        "review_id",
        "file_name",
        "width",
        "height",
        "encoded_sha256",
        "pixel_sha256",
    }
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _require_divisare_url(value: Any, name: str, *, frozen_profile: bool) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "images.divisare.com"
        or parsed.netloc != "images.divisare.com"
    ):
        raise ValueError(f"{name} must use the Divisare HTTPS image host")
    if frozen_profile and SOURCE_PROFILE not in parsed.path.split("/"):
        raise ValueError(f"{name} must use the frozen max2048 profile")
    return value


def _load_frozen_manifest(path: Path) -> tuple[dict[str, Any], str, str]:
    path = path.resolve()
    if path.name == EXPECTED_MANIFEST_FILENAME:
        expected_file_sha = EXPECTED_MANIFEST_FILE_SHA256
        expected_logical_sha = EXPECTED_MANIFEST_LOGICAL_SHA256
        expected_manifest_sha = EXPECTED_MANIFEST_SHA256
        validator = devset.validate_devset_manifest
        label = "candidate development manifest"
    elif path.name == EXPECTED_HOLDOUT_MANIFEST_FILENAME:
        expected_file_sha = EXPECTED_HOLDOUT_MANIFEST_FILE_SHA256
        expected_logical_sha = EXPECTED_HOLDOUT_MANIFEST_LOGICAL_SHA256
        expected_manifest_sha = EXPECTED_HOLDOUT_MANIFEST_SHA256
        validator = holdout_selection.validate_selection_manifest
        label = "fresh holdout candidate manifest"
    else:
        raise ValueError(
            "manifest filename is not an approved frozen axes-review manifest"
        )
    raw = path.read_bytes()
    file_sha = _sha256_bytes(raw)
    if file_sha != expected_file_sha:
        raise ValueError(f"{label} file SHA mismatch")
    payload = parse_json_strict(raw, label=path.name)
    validator(payload)
    logical_sha = _require_sha(
        payload.get("logical_sha256"), f"{label} logical SHA"
    )
    manifest_sha = _require_sha(
        payload.get("manifest_sha256"), f"{label} SHA"
    )
    if logical_sha != expected_logical_sha:
        raise ValueError(f"{label} logical SHA mismatch")
    if manifest_sha != expected_manifest_sha:
        raise ValueError(f"{label} SHA mismatch")
    return payload, file_sha, logical_sha


def _selected_rows(
    payload: Mapping[str, Any], subset: str
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    try:
        limit = SUBSET_LIMITS[subset]
    except KeyError as exc:
        raise ValueError("subset must be one of n10, n20, n50, or all") from exc

    audit_samples = payload["audit_samples"]
    public_rows = payload["review_rows"]
    selected = {
        str(sample["review_id"]): sample for sample in audit_samples[:limit]
    }
    if len(selected) != limit:
        raise ValueError("selected sample prefix contains duplicate review IDs")
    ordered = [
        (row, selected[str(row["review_id"])])
        for row in public_rows
        if str(row["review_id"]) in selected
    ]
    if len(ordered) != limit or {str(row[0]["review_id"]) for row in ordered} != set(
        selected
    ):
        raise ValueError("review rows do not cover the selected sample prefix")
    return ordered


def _validate_written_image(path: Path, row: Mapping[str, Any]) -> None:
    raw = path.read_bytes()
    if _sha256_bytes(raw) != row["encoded_sha256"]:
        raise ValueError(f"staged image encoded SHA mismatch: {row['review_id']}")
    decoded = decode_source(raw)
    if decoded.decoded_format != "JPEG":
        raise ValueError(f"staged image is not JPEG: {row['review_id']}")
    if (decoded.width, decoded.height) != (row["width"], row["height"]):
        raise ValueError(f"staged image dimensions changed: {row['review_id']}")
    if _pixel_sha256(decoded.image) != row["pixel_sha256"]:
        raise ValueError(f"staged image pixel SHA mismatch: {row['review_id']}")


def validate_public_review_inputs(
    payload: Mapping[str, Any],
    *,
    expected_file_sha: str,
    expected_logical_sha: str,
    expected_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Validate the intentionally tiny reviewer-facing JSON contract."""
    if set(payload) != _PUBLIC_TOP_LEVEL_FIELDS:
        raise ValueError("public review input fields changed")
    if payload.get("manifest_file_sha256") != expected_file_sha:
        raise ValueError("public review inputs bind the wrong manifest file SHA")
    if payload.get("manifest_logical_sha256") != expected_logical_sha:
        raise ValueError("public review inputs bind the wrong manifest logical SHA")
    _require_sha(expected_file_sha, "manifest file SHA")
    _require_sha(expected_logical_sha, "manifest logical SHA")
    rows = payload.get("review_rows")
    if not isinstance(rows, list) or len(rows) != len(expected_rows):
        raise ValueError("public review input row count mismatch")

    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for index, (row, expected) in enumerate(zip(rows, expected_rows), 1):
        if not isinstance(row, Mapping) or set(row) != _PUBLIC_ROW_FIELDS:
            raise ValueError(f"public review row {index} fields changed")
        review_id = row.get("review_id")
        if not isinstance(review_id, str) or not _REVIEW_ID_RE.fullmatch(review_id):
            raise ValueError(f"public review row {index} has invalid review ID")
        if row.get("review_rank") != expected.get("review_rank"):
            raise ValueError(f"public review row {index} rank changed")
        if review_id != expected.get("review_id"):
            raise ValueError(f"public review row {index} order changed")
        file_name = row.get("file_name")
        if file_name != f"{review_id}.jpg":
            raise ValueError(f"public review row {index} file name is not opaque")
        width = row.get("width")
        height = row.get("height")
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
            or min(width, height) < 1
            or max(width, height) > MAX_LONG_EDGE
        ):
            raise ValueError(f"public review row {index} dimensions are invalid")
        _require_sha(row.get("encoded_sha256"), f"public row {index} encoded SHA")
        _require_sha(row.get("pixel_sha256"), f"public row {index} pixel SHA")
        if review_id in seen_ids or file_name in seen_files:
            raise ValueError("public review inputs contain duplicate IDs or files")
        seen_ids.add(review_id)
        seen_files.add(file_name)


def _validate_staging_directory(
    directory: Path,
    public_payload: Mapping[str, Any],
    *,
    expected_rows: Sequence[Mapping[str, Any]],
    manifest_file_sha: str,
    manifest_logical_sha: str,
) -> None:
    validate_public_review_inputs(
        public_payload,
        expected_file_sha=manifest_file_sha,
        expected_logical_sha=manifest_logical_sha,
        expected_rows=expected_rows,
    )
    expected_names = {REVIEW_INPUTS_FILENAME} | {
        str(row["file_name"]) for row in public_payload["review_rows"]
    }
    actual_names = {path.name for path in directory.iterdir()}
    if actual_names != expected_names or any(not path.is_file() for path in directory.iterdir()):
        raise ValueError("staging directory contains unexpected or missing files")
    parsed = parse_json_strict(
        (directory / REVIEW_INPUTS_FILENAME).read_bytes(),
        label=REVIEW_INPUTS_FILENAME,
    )
    if parsed != public_payload:
        raise ValueError("written public review input JSON changed")
    for row in public_payload["review_rows"]:
        _validate_written_image(directory / row["file_name"], row)


def stage_review_inputs(
    *,
    manifest_path: Path,
    output_dir: Path,
    subset: str = "all",
    fetcher: Callable[[str], FetchPayload] = network_fetch,
) -> dict[str, Any]:
    """Create a new, blinded review directory using all-or-nothing publish."""
    manifest_path = manifest_path.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"review output directory already exists: {output_dir}")
    if not output_dir.parent.exists() or not output_dir.parent.is_dir():
        raise FileNotFoundError(
            f"review output parent directory does not exist: {output_dir.parent}"
        )
    payload, manifest_file_sha, manifest_logical_sha = _load_frozen_manifest(
        manifest_path
    )
    selected = _selected_rows(payload, subset)
    expected_public_rows = [row for row, _sample in selected]

    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.partial-", dir=output_dir.parent)
    )
    published = False
    try:
        public_rows: list[dict[str, Any]] = []
        for public_row, sample in selected:
            review_id = str(public_row["review_id"])
            source = sample["source_identity"]
            evidence = sample["image_evidence"]
            request_url = _require_divisare_url(
                source["request_url"], f"{review_id} request_url", frozen_profile=True
            )
            fetched = fetcher(request_url)
            if not isinstance(fetched, FetchPayload):
                raise TypeError(f"fetcher returned an invalid payload for {review_id}")
            if not 200 <= fetched.http_status < 300:
                raise ValueError(f"fetcher returned HTTP {fetched.http_status} for {review_id}")
            _require_divisare_url(
                fetched.final_url, f"{review_id} final_url", frozen_profile=False
            )
            actual_content_sha = _sha256_bytes(fetched.raw)
            expected_content_sha = _require_sha(
                evidence["content_sha256"], f"{review_id} frozen content SHA"
            )
            if actual_content_sha != expected_content_sha:
                raise ValueError(f"frozen response SHA mismatch for {review_id}")

            derivative = prepare_derivative(
                decode_source(fetched.raw), LANE, MAX_LONG_EDGE
            )
            file_name = f"{review_id}.jpg"
            image_path = staging / file_name
            with image_path.open("xb") as handle:
                handle.write(derivative.encoded_bytes)
            row = {
                "review_rank": public_row["review_rank"],
                "review_id": review_id,
                "file_name": file_name,
                "width": derivative.width,
                "height": derivative.height,
                "encoded_sha256": derivative.encoded_sha256,
                "pixel_sha256": derivative.pixel_sha256,
            }
            _validate_written_image(image_path, row)
            public_rows.append(row)

        public_payload: dict[str, Any] = {
            "manifest_file_sha256": manifest_file_sha,
            "manifest_logical_sha256": manifest_logical_sha,
            "review_rows": public_rows,
        }
        review_json_path = staging / REVIEW_INPUTS_FILENAME
        with review_json_path.open("xb") as handle:
            handle.write(canonical_json_bytes(public_payload) + b"\n")
        _validate_staging_directory(
            staging,
            public_payload,
            expected_rows=expected_public_rows,
            manifest_file_sha=manifest_file_sha,
            manifest_logical_sha=manifest_logical_sha,
        )
        if output_dir.exists():
            raise FileExistsError(
                f"review output directory appeared during staging: {output_dir}"
            )
        # On the supported Windows workspace, directory rename is atomic and
        # fails rather than replacing a destination that wins this race.
        os.rename(staging, output_dir)
        published = True
        return {
            "output_dir": str(output_dir),
            "subset": subset,
            "image_count": len(public_rows),
            "manifest_file_sha256": manifest_file_sha,
            "manifest_logical_sha256": manifest_logical_sha,
        }
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)
