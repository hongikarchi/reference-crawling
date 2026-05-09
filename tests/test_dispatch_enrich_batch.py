import json
import subprocess
from datetime import datetime

from tools import dispatch_enrich_batch
from tools.d1_enrich_codex import SourceRecord


def test_d1_batch_builder_uses_existing_entry_shape_and_resume(tmp_path):
    canonical_path = tmp_path / "canonical.json"
    canonical_path.write_text(
        json.dumps(
            {
                "clusters": [
                    {
                        "canonical_bld_id": "bld_done",
                        "primary_name": "Done House",
                        "source_refs": {"divisare": ["1"]},
                    },
                    {
                        "canonical_bld_id": "bld_pending",
                        "primary_name": "Pending House",
                        "source_refs": {"divisare": ["2"]},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    source_records = {
        ("divisare", "1"): SourceRecord(source="divisare", source_id="1", text="Already enriched."),
        ("divisare", "2"): SourceRecord(
            source="divisare",
            source_id="2",
            text="A compact concrete house around a courtyard.",
            architect_names=("Studio Test",),
            city="Seoul",
            country="South Korea",
            year=2024,
            typology="house",
        ),
    }

    rows = dispatch_enrich_batch.build_d1_records(
        canonical_path=canonical_path,
        done_cids={"bld_done"},
        source_records=source_records,
    )

    assert [row["cid"] for row in rows] == ["bld_pending"]
    assert rows[0]["primary_name"] == "Pending House"
    assert rows[0]["arch_names"] == ["Studio Test"]
    assert rows[0]["descriptions"] == [
        {"source": "divisare", "text": "A compact concrete house around a courtyard."}
    ]


def test_d2_and_e2_batch_builders_from_e1_jsonl_with_resume(tmp_path):
    e1_path = tmp_path / "e1_clusters.jsonl"
    e1_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "cid": "bld_done",
                        "all_images": [{"url": "https://img.test/done.jpg", "kind": "cover"}],
                        "best_image_per_cluster": {"0": {"url": "https://img.test/done.jpg"}},
                    }
                ),
                json.dumps(
                    {
                        "cid": "bld_pending",
                        "all_images": [
                            {
                                "url": "https://img.test/gallery.jpg",
                                "kind": "gallery",
                                "image_order": 1,
                                "w": 2000,
                                "h": 1000,
                            },
                            {
                                "url": "https://img.test/cover.jpg",
                                "kind": "cover",
                                "image_order": 0,
                                "w": 800,
                                "h": 600,
                            },
                        ],
                        "best_image_per_cluster": {
                            "0": {
                                "url": "https://img.test/cover.jpg",
                                "kind": "cover",
                                "rank": 0,
                            }
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    d2_rows = dispatch_enrich_batch.build_d2_records(e1_path=e1_path, done_cids={"bld_done"})
    e2_rows = dispatch_enrich_batch.build_e2_records(e1_path=e1_path, done_cids={"bld_done"})

    assert d2_rows == [
        {
            "cid": "bld_pending",
            "cover_image_url": "https://img.test/cover.jpg",
            "cover": {"kind": "cover", "image_order": 0, "w": 800, "h": 600},
        }
    ]
    assert e2_rows == [
        {
            "cid": "bld_pending",
            "best_image_per_cluster": {"0": {"url": "https://img.test/cover.jpg", "kind": "cover", "rank": 0}},
        }
    ]


def test_extract_json_array_handles_clean_fenced_and_leading_prose():
    assert dispatch_enrich_batch.extract_json_array('[{"cid":"a"}]') == [{"cid": "a"}]
    assert dispatch_enrich_batch.extract_json_array('```json\n[{"cid":"b"}]\n```') == [{"cid": "b"}]
    assert dispatch_enrich_batch.extract_json_array('done\n[{"cid":"c"}]\ntokens used: 12') == [{"cid": "c"}]


def test_extract_json_array_returns_last_array_so_prompt_payload_is_ignored():
    text = 'Input JSON:\n[{"cid":"prompt"}]\nAnswer:\n[{"cid":"response"}]\ntokens used: 5'
    assert dispatch_enrich_batch.extract_json_array(text) == [{"cid": "response"}]


def test_extract_json_array_returns_none_for_malformed_json():
    assert dispatch_enrich_batch.extract_json_array("not json [{bad]") is None


def test_parse_usage_limit_until_with_same_day_and_rollover():
    same_day = dispatch_enrich_batch.parse_usage_limit_until(
        "You've hit your usage limit. Try again at 14:30.",
        now=datetime(2026, 5, 9, 14, 0),
    )
    rollover = dispatch_enrich_batch.parse_usage_limit_until(
        "You've hit your usage limit. Try again at 01:15.",
        now=datetime(2026, 5, 9, 23, 0),
    )

    assert same_day == datetime(2026, 5, 9, 14, 30)
    assert rollover == datetime(2026, 5, 10, 1, 15)


def test_dispatch_prompt_uses_cmux_dispatch_script():
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="sent", stderr="")

    dispatch_enrich_batch.dispatch_prompt("enricher", "hello", runner=fake_runner)

    assert calls == [
        (
            ["./tools/dispatch.sh", "enricher", "hello"],
            {"capture_output": True, "text": True, "check": True},
        )
    ]


def test_dispatch_prompt_preserves_surface_suffix():
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="sent", stderr="")

    dispatch_enrich_batch.dispatch_prompt("enricher:27", "hello", runner=fake_runner)

    assert calls == [
        (
            ["./tools/dispatch.sh", "enricher:27", "hello"],
            {"capture_output": True, "text": True, "check": True},
        )
    ]


def test_poll_screen_reads_mocked_cmux_output_and_extracts_json():
    def fake_runner(cmd, **kwargs):
        assert cmd == ["./tools/poll.sh", "enricher", "1200", "--scrollback"]
        return subprocess.CompletedProcess(cmd, 0, stdout='[{"cid":"bld_1"}]\ntokens used: 4\n›', stderr="")

    result = dispatch_enrich_batch.poll_screen(
        "enricher",
        timeout_seconds=1,
        poll_interval_seconds=0,
        sleeper=lambda _: None,
        runner=fake_runner,
    )

    assert result.rows == [{"cid": "bld_1"}]
    assert result.timed_out is False


def test_poll_screen_preserves_surface_suffix():
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout='[{"cid":"bld_1"}]\ntokens used: 4\n›', stderr="")

    result = dispatch_enrich_batch.poll_screen(
        "enricher:27",
        timeout_seconds=1,
        poll_interval_seconds=0,
        sleeper=lambda _: None,
        runner=fake_runner,
    )

    assert calls[0][0] == ["./tools/poll.sh", "enricher:27", "1200", "--scrollback"]
    assert result.rows == [{"cid": "bld_1"}]


def test_poll_screen_short_circuits_on_full_count_match():
    """When expected_count rows present, return immediately without waiting
    for tokens-used / placeholder marker — codex doesn't reliably print it."""
    def fake_runner(cmd, **kwargs):
        # codex output: 3 JSON rows (matching expected_count=3) + ` › Implement` placeholder,
        # but NO 'tokens used' / 'Token usage' marker. Old _looks_idle returns False.
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='[{"cid":"bld_1"},{"cid":"bld_2"},{"cid":"bld_3"}]\n\n› Implement {feature}\n  gpt-5.5 medium fast',
            stderr="",
        )

    result = dispatch_enrich_batch.poll_screen(
        "enricher",
        timeout_seconds=1,
        poll_interval_seconds=0,
        expected_count=3,
        sleeper=lambda _: None,
        runner=fake_runner,
    )

    assert result.rows == [{"cid": "bld_1"}, {"cid": "bld_2"}, {"cid": "bld_3"}]
    assert result.timed_out is False


def test_idle_detection_recognises_token_usage_phrase():
    """codex CLI prints 'Token usage:' (not 'tokens used') — _looks_idle must match both."""
    raw = "[{\"cid\":\"x\"}]\n\nToken usage: total=12345 input=11000 output=1345\n\n› Implement {feature}"
    assert dispatch_enrich_batch._looks_idle(raw) is True


def test_idle_detection_rejects_placeholder_alone():
    """The codex placeholder ('› Write tests', '› Implement') is ALWAYS
    visible at the bottom of the screen, even mid-response, so it cannot
    be used as an idle signal on its own. _looks_idle must require an
    actual footer marker ('tokens used' or 'Token usage')."""
    raw = "[{\"cid\": \"x\", \"program\": \"Office\"}]\n\n› Write tests for @filename\n  gpt-5.5 medium fast"
    assert dispatch_enrich_batch._looks_idle(raw) is False


def test_idle_detection_handles_long_response_with_footer_after_response():
    long_json = json.dumps(
        [
            {
                "cid": f"bld_{idx:06d}",
                "program": "Housing",
                "style": "Contemporary",
                "color_tone": "Neutral",
                "atmosphere": "Serene",
                "material_visual": ["concrete", "glass"],
                "visual_description": "A restrained building with clear massing, neutral materials, and calm daylight.",
            }
            for idx in range(30)
        ]
    )
    raw = f"{long_json}\ntokens used\n45123\n› Find and fix a bug in @filename\n"

    assert dispatch_enrich_batch._looks_idle(raw) is True


def test_idle_detection_false_during_processing():
    raw = "...processing spinner › still present...\ntokens used\n123\n... still working"

    assert dispatch_enrich_batch._looks_idle(raw) is False


def test_poll_screen_detects_usage_limit_from_mocked_response():
    def fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="You've hit your usage limit. Try again at 14:30.",
            stderr="",
        )

    result = dispatch_enrich_batch.poll_screen(
        "enricher",
        timeout_seconds=1,
        poll_interval_seconds=0,
        sleeper=lambda _: None,
        runner=fake_runner,
    )

    assert result.rows is None
    assert result.usage_limit_until is not None
    assert result.usage_limit_until.hour == 14
    assert result.usage_limit_until.minute == 30


def test_validate_batch_checks_vocab_and_required_fields():
    rows = [
        {
            "cid": "bld_1",
            "style_image": "Contemporary",
            "color_tone_image": "Neutral",
            "material_visual_image": ["Concrete", "Glass"],
            "visual_description_image": (
                "A contemporary building presents a restrained facade with concrete and glass surfaces "
                "set along a compact urban edge."
            ),
        }
    ]

    normalized, error = dispatch_enrich_batch.validate_batch("d2", ["bld_1"], rows)

    assert error is None
    assert normalized[0]["material_visual_image"] == ["concrete", "glass"]
