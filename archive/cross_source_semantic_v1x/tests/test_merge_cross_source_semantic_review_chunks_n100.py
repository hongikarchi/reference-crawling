from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.merge_cross_source_semantic_review_chunks_n100 import merge_chunks


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _draft() -> dict[str, object]:
    return {
        "status": "draft",
        "reference_type": "agent_reference",
        "reviewer_id": "codex-agent-reference",
        "semantic_labels_read": False,
        "blind_review_completed_before_unblinding": False,
        "rows": [{"inference_id": "a"}, {"inference_id": "b"}],
    }


def test_merge_reorders_rows_and_attests_blind_completion(tmp_path: Path) -> None:
    draft = tmp_path / "draft.json"
    second = tmp_path / "second.json"
    first = tmp_path / "first.json"
    output = tmp_path / "merged.json"
    _write(draft, _draft())
    _write(second, {"reviewer_id": "codex-agent-reference", "blind": True, "sheet_start": 2, "sheet_end": 2, "rows": [{"inference_id": "b", "value": 2}]})
    _write(first, {"reviewer_id": "codex-agent-reference", "blind": True, "sheet_start": 1, "sheet_end": 1, "rows": [{"inference_id": "a", "value": 1}]})

    result = merge_chunks(draft_path=draft, chunk_paths=[second, first], output_path=output)
    merged = json.loads(output.read_text(encoding="utf-8"))
    assert result["row_count"] == 2
    assert [row["inference_id"] for row in merged["rows"]] == ["a", "b"]
    assert merged["blind_review_completed_before_unblinding"] is True
    assert merged["semantic_labels_read"] is False
    assert [row["sheet_start"] for row in merged["blind_chunk_inputs"]] == [1, 2]


@pytest.mark.parametrize("mode", ["missing", "duplicate", "not_blind"])
def test_merge_rejects_bad_accounting(tmp_path: Path, mode: str) -> None:
    draft = tmp_path / "draft.json"
    chunk = tmp_path / "chunk.json"
    output = tmp_path / "merged.json"
    _write(draft, _draft())
    rows = [{"inference_id": "a"}]
    blind = True
    paths = [chunk]
    if mode == "duplicate":
        other = tmp_path / "other.json"
        _write(other, {"reviewer_id": "codex-agent-reference", "blind": True, "sheet_start": 2, "sheet_end": 2, "rows": [{"inference_id": "a"}, {"inference_id": "b"}]})
        paths.append(other)
    elif mode == "not_blind":
        blind = False
        rows.append({"inference_id": "b"})
    _write(chunk, {"reviewer_id": "codex-agent-reference", "blind": blind, "sheet_start": 1, "sheet_end": 1, "rows": rows})

    with pytest.raises(ValueError):
        merge_chunks(draft_path=draft, chunk_paths=paths, output_path=output)
    assert not output.exists()


def test_merge_is_no_clobber(tmp_path: Path) -> None:
    draft = tmp_path / "draft.json"
    chunk = tmp_path / "chunk.json"
    output = tmp_path / "merged.json"
    _write(draft, _draft())
    _write(chunk, {"reviewer_id": "codex-agent-reference", "blind": True, "sheet_start": 1, "sheet_end": 1, "rows": [{"inference_id": "a"}, {"inference_id": "b"}]})
    merge_chunks(draft_path=draft, chunk_paths=[chunk], output_path=output)
    with pytest.raises(FileExistsError):
        merge_chunks(draft_path=draft, chunk_paths=[chunk], output_path=output)


def test_merge_expands_compact_transport_row(tmp_path: Path) -> None:
    draft = tmp_path / "draft.json"
    chunk = tmp_path / "chunk.json"
    output = tmp_path / "merged.json"
    value = [
        True,
        "none",
        "photograph",
        "exterior",
        "overall",
        "eye_level",
        "not_applicable",
        "visibly_finished",
        "high",
    ]
    base = _draft()
    base["rows"] = [{"inference_id": "a"}]
    _write(draft, base)
    _write(
        chunk,
        {
            "reviewer_id": "codex-agent-reference",
            "blind": True,
            "sheet_start": 1,
            "sheet_end": 1,
            "rows": [
                {
                    "id": "a",
                    "v": value,
                    "a": {"framing_scale": ["element_detail"]},
                    "u": ["framing_scale"],
                    "ri": False,
                    "why": "A complete building is visible.",
                }
            ],
        },
    )

    merge_chunks(draft_path=draft, chunk_paths=[chunk], output_path=output)
    row = json.loads(output.read_text(encoding="utf-8"))["rows"][0]
    assert row["primary"]["framing_scale"] == "overall"
    assert row["acceptable_values"]["framing_scale"] == [
        "overall",
        "element_detail",
    ]
