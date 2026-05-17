import json
import subprocess

from tools import canonical_v2_local_enrich as local_enrich
from tools import dispatch_enrich_batch as dispatch


def _codex_json_response(payload, input_tokens=1000, output_tokens=200):
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "test"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": 50,
                        "output_tokens": output_tokens,
                    },
                }
            ),
        ]
    )


def test_run_d1_batch_uses_single_codex_exec_and_parses_rows():
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        payload = [
            {
                "cid": "bld_1",
                "program": "Housing",
                "style": "Contemporary",
                "color_tone": "Neutral",
                "atmosphere": "Serene",
                "material_visual": ["concrete"],
                "visual_description": "A compact residential building uses concrete surfaces and restrained massing in a calm urban setting.",
            }
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout=_codex_json_response(payload), stderr="")

    result = local_enrich.run_d1_batch(
        [{"cid": "bld_1", "descriptions": []}],
        model_meta=dispatch.ModelMeta(model="gpt-5.5", reasoning="low", fast="fast"),
        timeout_seconds=30,
        runner=fake_runner,
    )

    assert result.rows is not None
    assert result.rows[0]["cid"] == "bld_1"
    cmd, kwargs = calls[0]
    assert cmd[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert cmd.count("--") == 1
    assert kwargs["check"] is False


def test_run_e2_vision_batch_downloads_candidates_and_validates_urls(tmp_path):
    downloaded = []
    calls = []

    def fake_downloader(url):
        downloaded.append(url)
        path = tmp_path / f"image_{len(downloaded)}.jpg"
        path.write_bytes(b"fake")
        return path, True

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        image_args = [cmd[idx + 1] for idx, value in enumerate(cmd) if value == "-i"]
        assert len(image_args) == 2
        payload = [
            {
                "cid": "bld_1",
                "covers_by_type": {
                    "exterior": "https://img.test/exterior.jpg",
                    "interior": "https://img.test/interior.jpg",
                    "drawing": None,
                    "aerial": None,
                    "detail": None,
                },
            }
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout=_codex_json_response(payload), stderr="")

    batch = [
        {
            "cid": "bld_1",
            "candidates": [
                {"url": "https://img.test/exterior.jpg", "cluster_id": "0"},
                {"url": "https://img.test/interior.jpg", "cluster_id": "1"},
            ],
        }
    ]

    result = local_enrich.run_e2_vision_batch(
        batch,
        model_meta=dispatch.ModelMeta(model="gpt-5.5", reasoning="low", fast="fast"),
        timeout_seconds=30,
        runner=fake_runner,
        downloader=fake_downloader,
    )

    assert downloaded == ["https://img.test/exterior.jpg", "https://img.test/interior.jpg"]
    assert result.rows is not None
    assert result.rows[0]["covers_by_type"]["exterior"] == "https://img.test/exterior.jpg"
    assert calls[0][0].count("-i") == 2


def test_run_e2_vision_batch_rejects_url_not_in_candidates(tmp_path):
    def fake_downloader(url):
        path = tmp_path / "image.jpg"
        path.write_bytes(b"fake")
        return path, True

    def fake_runner(cmd, **kwargs):
        payload = [
            {
                "cid": "bld_1",
                "covers_by_type": {
                    "exterior": "https://img.test/not-a-candidate.jpg",
                    "interior": None,
                    "drawing": None,
                    "aerial": None,
                    "detail": None,
                },
            }
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout=_codex_json_response(payload), stderr="")

    result = local_enrich.run_e2_vision_batch(
        [{"cid": "bld_1", "candidates": [{"url": "https://img.test/exterior.jpg"}]}],
        model_meta=dispatch.ModelMeta(model="gpt-5.5", reasoning="low", fast="fast"),
        timeout_seconds=30,
        runner=fake_runner,
        downloader=fake_downloader,
    )

    assert result.rows is None
    assert "not in candidates" in result.failure_reason


def test_large_run_requires_ops_job_card(tmp_path):
    try:
        local_enrich._validate_ops_job_card(
            None,
            pending_count=101,
            dry_run=False,
            jobs_dir=tmp_path,
        )
    except ValueError as exc:
        assert "--ops-job-card" in str(exc)
    else:
        raise AssertionError("large run without job card should fail")


def test_ops_job_card_must_live_under_ops_jobs(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("owner: ENRICHER\nstage: D-2\n## Smoke Ladder\n## Abort Conditions\n", encoding="utf-8")

    try:
        local_enrich._validate_ops_job_card(
            outside,
            pending_count=101,
            dry_run=False,
            jobs_dir=tmp_path / "jobs",
        )
    except ValueError as exc:
        assert "must live under" in str(exc)
    else:
        raise AssertionError("outside job card should fail")


def test_valid_ops_job_card_allows_large_run(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    card = jobs / "d2.md"
    card.write_text("owner: ENRICHER\nstage: D-2\n## Smoke Ladder\n## Abort Conditions\n", encoding="utf-8")

    assert local_enrich._validate_ops_job_card(
        card,
        pending_count=101,
        dry_run=False,
        jobs_dir=jobs,
    ) == card.resolve()


def test_run_stage_aborts_transient_d2_download_failure_without_skipping_cids(tmp_path, monkeypatch):
    calls = []

    def fake_d2_batch(batch, **kwargs):
        calls.append([row["cid"] for row in batch])
        return dispatch.PollResult(
            rows=None,
            raw="network down",
            failure_reason=(
                "download_failed: cid=bld_1 all cover candidates failed: "
                "ConnectionError: Failed to establish a new connection: "
                "[Errno 8] nodename nor servname provided, or not known"
            ),
        )

    monkeypatch.setattr(dispatch, "run_d2_vision_batch", fake_d2_batch)

    summary = local_enrich.run_stage(
        "d2",
        rows=[{"cid": "bld_1"}, {"cid": "bld_2"}],
        output_path=tmp_path / "out.jsonl",
        failure_path=tmp_path / "failures.jsonl",
        metrics_path=tmp_path / "metrics.jsonl",
        batch_size=2,
        model_meta=dispatch.ModelMeta(model="gpt-5.5", reasoning="low", fast="fast"),
        timeout_seconds=30,
    )

    assert calls == [["bld_1", "bld_2"]]
    assert summary["written"] == 0
    assert summary["failures"] == 2

    failures = [json.loads(line) for line in (tmp_path / "failures.jsonl").read_text().splitlines()]
    assert [row["cid"] for row in failures] == ["bld_1", "bld_2"]
