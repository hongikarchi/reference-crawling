from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest
from PIL import Image

import canonical.image_fingerprint_pipeline as pipeline
from canonical.image_fingerprint_adapters import SourceAsset
from canonical.image_fingerprint_pipeline import (
    FetchFailure,
    FetchResponse,
    PipelineError,
    PipelineResult,
    run_image_fingerprint_pipeline,
)
from canonical.image_fingerprint_sidecar import (
    SidecarSchemaError,
    initialize_sidecar,
    open_sidecar,
)
from canonical.image_fingerprint_validator import validate_image_fingerprint_sidecar
from tools import run_image_fingerprints as cli


def _png() -> bytes:
    image = Image.new("RGB", (96, 64), (35, 90, 170))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _source_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES('read-only source')")
        connection.commit()
    finally:
        connection.close()


def _source_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_divisare_inventory_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE image_assets(
              asset_key TEXT PRIMARY KEY,
              original_filename TEXT,
              url_generation TEXT NOT NULL
            );
            CREATE TABLE image_urls(
              url_id INTEGER PRIMARY KEY,
              asset_key TEXT NOT NULL,
              url TEXT NOT NULL,
              transform_signature TEXT,
              url_generation TEXT NOT NULL
            );
            CREATE TABLE source_image_occurrences(
              article_id INTEGER NOT NULL,
              role TEXT NOT NULL,
              position INTEGER NOT NULL,
              raw_url TEXT NOT NULL,
              parse_status TEXT NOT NULL,
              asset_key TEXT
            );
            """
        )
        assets: list[tuple[str, str | None, str]] = []
        urls: list[tuple[int, str, str, str | None, str]] = []
        occurrences: list[tuple[int, str, int, str, str, str]] = []
        for index in range(10):
            key = f"divisare|asset-{index:03d}|v1"
            url = (
                "https://images.divisare.com/images/f_auto,q_auto,w_auto/"
                f"v1/asset-{index:03d}/project.jpg"
            )
            assets.append((key, "project.jpg", "cloudinary_public_id"))
            urls.append((index + 1, key, url, "f_auto", "cloudinary_public_id"))
            occurrences.append((1000 + index, "gallery", 0, url, "parsed", key))
        video_key = "divisare|asset-010-video|v1"
        video_url = (
            "https://images.divisare.com/images/f_auto/v1/"
            "project_images/10/movie.mp4/project.jpg"
        )
        assets.append((video_key, "movie.mp4", "project_images"))
        urls.append((11, video_key, video_url, "f_auto", "project_images"))
        occurrences.append((1010, "gallery", 0, video_url, "parsed", video_key))
        bad_key = "divisare|asset-011-unsupported|v1"
        bad_url = "https://example.test/not-divisare.jpg"
        assets.append((bad_key, "image.jpg", "cloudinary_public_id"))
        urls.append((12, bad_key, bad_url, None, "cloudinary_public_id"))
        occurrences.append((1011, "gallery", 0, bad_url, "parsed", bad_key))
        connection.executemany("INSERT INTO image_assets VALUES(?,?,?)", assets)
        connection.executemany("INSERT INTO image_urls VALUES(?,?,?,?,?)", urls)
        connection.executemany(
            "INSERT INTO source_image_occurrences VALUES(?,?,?,?,?,?)", occurrences
        )
        connection.commit()
    finally:
        connection.close()


def _make_architizer_inventory_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE image_assets(
              asset_id TEXT PRIMARY KEY,
              asset_key TEXT NOT NULL,
              normalized_url TEXT NOT NULL,
              host TEXT NOT NULL,
              path TEXT NOT NULL,
              is_placeholder_candidate INTEGER NOT NULL,
              asset_key_version TEXT NOT NULL
            ) STRICT;
            CREATE TABLE image_urls(
              image_url_id TEXT PRIMARY KEY,
              asset_id TEXT NOT NULL,
              raw_url TEXT NOT NULL,
              normalized_url TEXT NOT NULL,
              source_host TEXT NOT NULL
            ) STRICT;
            CREATE TABLE source_image_occurrences(
              occurrence_id TEXT PRIMARY KEY,
              source_project_id INTEGER NOT NULL,
              role TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              raw_url TEXT NOT NULL,
              image_url_id TEXT,
              asset_id TEXT,
              parse_status TEXT NOT NULL,
              parse_error TEXT,
              source_field TEXT NOT NULL,
              image_type TEXT
            ) STRICT;
            """
        )
        assets: list[tuple[object, ...]] = []
        urls: list[tuple[object, ...]] = []
        occurrences: list[tuple[object, ...]] = []
        for index in range(10):
            asset_id = f"atz-asset-{index:03d}"
            raw_url = f"https://architizer-prod.imgix.net/media/{asset_id}.jpg?w=800"
            normalized = f"https://architizer-prod.imgix.net/media/{asset_id}.jpg"
            image_url_id = f"url-{index:03d}"
            assets.append(
                (
                    asset_id,
                    f"key-{index:03d}",
                    normalized,
                    "architizer-prod.imgix.net",
                    f"/media/{asset_id}.jpg",
                    0,
                    "architizer-host-path-asset-v1",
                )
            )
            urls.append((image_url_id, asset_id, raw_url, normalized, "architizer-prod.imgix.net"))
            occurrences.append(
                (
                    f"occ-{index:03d}",
                    2000 + index,
                    "gallery",
                    0,
                    raw_url,
                    image_url_id,
                    asset_id,
                    "parsed",
                    None,
                    "og:image:gallery",
                    None,
                )
            )
        placeholder_id = "atz-asset-010-placeholder"
        placeholder = "https://facebook.com/static/placeholder.jpg"
        assets.append(
            (
                placeholder_id,
                "key-placeholder",
                placeholder,
                "facebook.com",
                "/static/placeholder.jpg",
                1,
                "architizer-host-path-asset-v1",
            )
        )
        urls.append(("url-placeholder", placeholder_id, placeholder, placeholder, "facebook.com"))
        occurrences.append(
            (
                "occ-placeholder",
                2010,
                "gallery",
                0,
                placeholder,
                "url-placeholder",
                placeholder_id,
                "placeholder_candidate",
                None,
                "og:image:gallery",
                None,
            )
        )
        video_id = "atz-asset-011-video"
        video = "https://architizer-prod.imgix.net/media/movie.mp4"
        assets.append(
            (
                video_id,
                "key-video",
                video,
                "architizer-prod.imgix.net",
                "/media/movie.mp4",
                0,
                "architizer-host-path-asset-v1",
            )
        )
        urls.append(("url-video", video_id, video, video, "architizer-prod.imgix.net"))
        occurrences.append(
            (
                "occ-video",
                2011,
                "gallery",
                0,
                video,
                "url-video",
                video_id,
                "parsed",
                None,
                "og:image:gallery",
                None,
            )
        )
        connection.executemany("INSERT INTO image_assets VALUES(?,?,?,?,?,?,?)", assets)
        connection.executemany("INSERT INTO image_urls VALUES(?,?,?,?,?)", urls)
        connection.executemany(
            "INSERT INTO source_image_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            occurrences,
        )
        connection.commit()
    finally:
        connection.close()


def _asset(number: int, *, source: str = "divisare") -> SourceAsset:
    asset_id = f"asset-{number:06d}"
    if source == "divisare":
        base = f"https://images.divisare.com/images/v1/{asset_id}/image.jpg"
        fetch = (
            "https://images.divisare.com/images/"
            f"c_limit,f_jpg,h_1024,q_85,w_1024/v1/{asset_id}/image.jpg"
        )
    else:
        base = f"https://architizer-prod.imgix.net/media/{asset_id}.jpg"
        fetch = base + "?auto=compress&fit=max&fm=jpg&h=1024&q=85&w=1024"
    return SourceAsset(
        source=source,
        source_asset_id=asset_id,
        source_asset_key=asset_id,
        normalized_url=base,
        selected_raw_url=base,
        effective_fetch_url=fetch,
        source_urls=(base,),
        occurrence_count=1,
        parent_count=1,
        roles=("gallery",),
        format_lane="raster",
        fetch_profile_version="fixture-max1024-v1",
    )


def _cli_args(*extra: str) -> list[str]:
    return [
        "--source",
        "divisare",
        "--source-db",
        "source.db",
        "--output",
        "result.db",
        "--n",
        "10",
        *extra,
    ]


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


class _ConcurrentFetchState:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.instances = 0
        self.instance_threads: dict[int, set[int]] = {}

    def factory(self):
        with self.lock:
            self.instances += 1
            instance_no = self.instances
            self.instance_threads[instance_no] = set()
        state = self

        class WorkerFetcher:
            network_requests = 0

            def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
                del attempt_no
                thread_id = threading.get_ident()
                with state.lock:
                    state.instance_threads[instance_no].add(thread_id)
                    state.active += 1
                    state.peak = max(state.peak, state.active)
                    self.network_requests += 1
                try:
                    time.sleep(0.025)
                    return FetchResponse(
                        200, asset.effective_fetch_url, "image/png", state.body
                    )
                finally:
                    with state.lock:
                        state.active -= 1

            def close(self) -> None:
                return None

        return WorkerFetcher()


def test_cli_full_runner_defaults_and_bounds() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(_cli_args())
    assert args.workers == 4
    assert args.requests_per_second == 2.0
    assert args.circuit_breaker_threshold == 8
    assert args.cooldown_seconds == 30.0
    assert args.batch_size is None

    tuned = parser.parse_args(
        _cli_args(
            "--workers",
            "8",
            "--requests-per-second",
            "3.5",
            "--circuit-breaker-threshold",
            "12",
            "--cooldown-seconds",
            "0",
            "--batch-size",
            "17",
        )
    )
    assert (
        tuned.workers,
        tuned.requests_per_second,
        tuned.circuit_breaker_threshold,
        tuned.cooldown_seconds,
        tuned.batch_size,
    ) == (8, 3.5, 12, 0.0, 17)

    for invalid in (
        ("--workers", "0"),
        ("--workers", "9"),
        ("--requests-per-second", "0"),
        ("--requests-per-second", "nan"),
        ("--requests-per-second", "inf"),
        ("--circuit-breaker-threshold", "0"),
        ("--cooldown-seconds", "-1"),
        ("--cooldown-seconds", "nan"),
        ("--connect-timeout", "-1"),
        ("--read-timeout", "inf"),
        ("--batch-size", "0"),
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(_cli_args(*invalid))


def test_cli_forwards_full_runner_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    def fake_pipeline(**kwargs: object) -> PipelineResult:
        captured.update(kwargs)
        return PipelineResult(
            output_path=Path(str(kwargs["output"])),
            source="divisare",
            source_sha256="a" * 64,
            selection_manifest_sha256="b" * 64,
            selected_assets=10,
            run_status="complete",
            status_counts={"success": 10},
            network_requests=10,
            resumed=False,
            already_complete=False,
        )

    monkeypatch.setattr(cli, "run_image_fingerprint_pipeline", fake_pipeline)
    source = tmp_path / "source.db"
    output = tmp_path / "output.db"
    assert (
        cli.main(
            [
                "--source",
                "divisare",
                "--source-db",
                str(source),
                "--output",
                str(output),
                "--n",
                "10",
                "--workers",
                "6",
                "--requests-per-second",
                "1.5",
                "--circuit-breaker-threshold",
                "5",
                "--cooldown-seconds",
                "7.5",
                "--batch-size",
                "23",
            ]
        )
        == 0
    )
    assert captured["workers"] == 6
    assert captured["requests_per_second"] == 1.5
    assert captured["circuit_breaker_threshold"] == 5
    assert captured["cooldown_seconds"] == 7.5
    assert captured["batch_size"] == 23
    assert json.loads(capsys.readouterr().out)["network_requests"] == 10


def test_fake_clock_enforces_two_rps_and_retry_after_cooldown() -> None:
    clock = _FakeClock()
    limiter = pipeline._GlobalRateLimiter(
        2.0, clock=clock.monotonic, sleep=clock.sleep
    )

    limiter.acquire()
    assert clock.now == 0.0
    limiter.acquire()
    assert clock.now == pytest.approx(0.5)
    limiter.acquire()
    assert clock.now == pytest.approx(1.0)

    assert pipeline._parse_retry_after("3", wall_time=lambda: 10.0) == 3.0
    assert pipeline._parse_retry_after(
        "Thu, 01 Jan 1970 00:00:15 GMT", wall_time=lambda: 10.0
    ) == 5.0
    assert pipeline._parse_retry_after("120", wall_time=lambda: 10.0) == 120.0
    assert pipeline._parse_retry_after("inf", wall_time=lambda: 10.0) is None
    failure = FetchFailure(
        "http_429",
        "HTTP 429",
        retryable=True,
        http_status=429,
        retry_after_seconds=3.0,
    )
    assert pipeline._retry_delay(failure, 1) == 3.0
    limiter.defer(pipeline._retry_delay(failure, 1))
    limiter.acquire()
    assert clock.now == pytest.approx(4.0)
    assert clock.sleeps == pytest.approx([0.5, 0.5, 3.0])


def test_rate_limiter_cooldown_can_extend_an_already_waiting_worker() -> None:
    class BlockingClock:
        def __init__(self) -> None:
            self.now = 0.0
            self.lock = threading.Lock()
            self.first_sleep = True
            self.sleep_entered = threading.Event()
            self.release_sleep = threading.Event()

        def monotonic(self) -> float:
            with self.lock:
                return self.now

        def sleep(self, seconds: float) -> None:
            if self.first_sleep:
                self.first_sleep = False
                self.sleep_entered.set()
                assert self.release_sleep.wait(timeout=5)
            with self.lock:
                self.now += seconds

    clock = BlockingClock()
    limiter = pipeline._GlobalRateLimiter(
        2.0, clock=clock.monotonic, sleep=clock.sleep
    )
    limiter.acquire()
    worker = threading.Thread(target=limiter.acquire)
    worker.start()
    assert clock.sleep_entered.wait(timeout=5)
    limiter.defer(5.0)
    clock.release_sleep.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert clock.monotonic() == pytest.approx(5.0)


def test_persisted_retry_schedule_restores_only_remaining_delay() -> None:
    assert pipeline._remaining_scheduled_delay(
        "1970-01-01T00:00:10Z",
        120.0,
        wall_time=lambda: 20.0,
    ) == pytest.approx(110.0)
    assert pipeline._remaining_scheduled_delay(
        "1970-01-01T00:00:10Z",
        120.0,
        wall_time=lambda: 140.0,
    ) == 0.0


def test_advisory_lock_ignores_stale_file_but_rejects_live_holder(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "runner.lock"
    lock_path.write_text("pid=dead\n", encoding="ascii")

    descriptor = pipeline._acquire_lock(lock_path)
    try:
        with pytest.raises(PipelineError, match="lock is held"):
            pipeline._acquire_lock(lock_path)
    finally:
        pipeline._release_lock(lock_path, descriptor)

    reacquired = pipeline._acquire_lock(lock_path)
    pipeline._release_lock(lock_path, reacquired)


def test_advisory_lock_is_released_when_holder_process_dies(tmp_path: Path) -> None:
    lock_path = tmp_path / "process.lock"
    script = (
        "import os,pathlib,sys\n"
        "from canonical.image_fingerprint_pipeline import _acquire_lock\n"
        "fd=_acquire_lock(pathlib.Path(sys.argv[1]))\n"
        "print('LOCKED', flush=True)\n"
        "sys.stdin.readline()\n"
        "os._exit(0)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "LOCKED"
        with pytest.raises(PipelineError, match="lock is held"):
            pipeline._acquire_lock(lock_path)
    finally:
        if process.stdin is not None:
            process.stdin.write("exit\n")
            process.stdin.flush()
            process.stdin.close()
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    descriptor = pipeline._acquire_lock(lock_path)
    pipeline._release_lock(lock_path, descriptor)


def test_hot_journal_is_recovered_before_immutable_validation(tmp_path: Path) -> None:
    sidecar = tmp_path / "hot.db"
    connection = initialize_sidecar(sidecar)
    connection.close()
    script = (
        "import os,sqlite3,sys\n"
        "c=sqlite3.connect(sys.argv[1])\n"
        "c.execute('PRAGMA journal_mode=DELETE')\n"
        "c.execute('PRAGMA synchronous=FULL')\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "c.execute('PRAGMA user_version=999')\n"
        "os._exit(23)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(sidecar)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )
    assert completed.returncode == 23
    assert Path(str(sidecar) + "-journal").exists()

    pipeline._recover_partial(sidecar)

    readonly = open_sidecar(sidecar)
    try:
        assert readonly.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        readonly.close()


def test_parallel_workers_are_bounded_and_sqlite_has_one_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "parallel.db"
    _source_db(source)
    assets = tuple(_asset(index) for index in range(10))
    state = _ConcurrentFetchState(_png())
    writer_threads: set[int] = set()
    runner_thread = threading.get_ident()
    original_persist = pipeline._persist_attempt
    original_finish = pipeline._finish_asset

    def record_writer(connection: sqlite3.Connection, row: tuple[object, ...]) -> None:
        writer_threads.add(threading.get_ident())
        original_persist(connection, row)

    def record_terminal_writer(*args: object, **kwargs: object) -> None:
        writer_threads.add(threading.get_ident())
        original_finish(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_persist_attempt", record_writer)
    monkeypatch.setattr(pipeline, "_finish_asset", record_terminal_writer)
    result = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        asset_factory=lambda: iter(assets),
        fetcher_factory=state.factory,
        workers=4,
        requests_per_second=10_000,
        circuit_breaker_threshold=8,
        cooldown_seconds=0,
        batch_size=8,
    )

    assert result.run_status == "complete"
    assert result.status_counts == {"success": 10}
    assert 2 <= state.peak <= 4
    assert state.instances == 4
    assert all(len(thread_ids) == 1 for thread_ids in state.instance_threads.values())
    assert writer_threads == {runner_thread}

    connection = open_sidecar(output)
    try:
        worker_rows = connection.execute(
            "SELECT count(DISTINCT worker_no),min(worker_no),max(worker_no) "
            "FROM fetch_attempts"
        ).fetchone()
        assert tuple(worker_rows) == (4, 1, 4)
    finally:
        connection.close()


def test_default_http_fetcher_owns_one_session_per_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests

    source = tmp_path / "source.db"
    output = tmp_path / "sessions.db"
    _source_db(source)
    assets = tuple(_asset(index) for index in range(10))
    body = _png()
    lock = threading.Lock()
    sessions: list[object] = []
    owner_threads: dict[int, set[int]] = {}
    closed: set[int] = set()

    class FakeResponse:
        status_code = 200
        headers = {
            "Content-Type": "image/png",
            "Content-Length": str(len(body)),
        }

        def iter_content(self, chunk_size: int):
            assert chunk_size > 0
            yield body

        def close(self) -> None:
            return None

    class FakeSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            with lock:
                self.session_no = len(sessions) + 1
                sessions.append(self)
                owner_threads[self.session_no] = set()

        def get(self, *_args, **_kwargs) -> FakeResponse:
            with lock:
                owner_threads[self.session_no].add(threading.get_ident())
            time.sleep(0.025)
            return FakeResponse()

        def close(self) -> None:
            with lock:
                closed.add(self.session_no)

    monkeypatch.setattr(requests, "Session", FakeSession)
    result = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        asset_factory=lambda: iter(assets),
        workers=4,
        requests_per_second=10_000,
        cooldown_seconds=0,
        batch_size=8,
    )

    assert result.run_status == "complete"
    assert len(sessions) == 4
    assert all(len(thread_ids) == 1 for thread_ids in owner_threads.values())
    assert closed == {1, 2, 3, 4}


def test_circuit_breaker_preserves_pending_then_healthy_resume(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "circuit.db"
    _source_db(source)
    assets = tuple(_asset(index) for index in range(10))

    class OverloadedFetcher:
        network_requests = 0

        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            del asset, attempt_no
            self.network_requests += 1
            raise FetchFailure(
                "http_503",
                "HTTP 503",
                retryable=True,
                http_status=503,
                retry_after_seconds=120,
            )

    first_clock = _FakeClock()
    with pytest.raises(PipelineError, match="circuit breaker opened"):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=10,
            asset_factory=lambda: iter(assets),
            fetcher_factory=OverloadedFetcher,
            workers=1,
            requests_per_second=10_000,
            circuit_breaker_threshold=2,
            cooldown_seconds=30,
            batch_size=10,
            clock=first_clock.monotonic,
            sleep=first_clock.sleep,
        )

    partial = Path(str(output) + ".partial")
    connection = open_sidecar(partial)
    try:
        assert connection.execute(
            "SELECT status FROM fingerprint_runs"
        ).fetchone()[0] == "running"
        assert connection.execute(
            "SELECT count(*) FROM fetch_attempts"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM fingerprints WHERE status='pending'"
        ).fetchone()[0] == 10
        assert connection.execute(
            "SELECT min(scheduled_delay_seconds) FROM fetch_attempts"
        ).fetchone()[0] == pytest.approx(120.0)
    finally:
        connection.close()


    healthy = _ConcurrentFetchState(_png())
    resume_sleeps: list[float] = []
    result = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        resume=True,
        asset_factory=lambda: iter(assets),
        fetcher_factory=healthy.factory,
        workers=4,
        requests_per_second=10_000,
        circuit_breaker_threshold=8,
        cooldown_seconds=0,
        batch_size=10,
        sleep=resume_sleeps.append,
    )
    assert result.run_status == "complete"
    assert result.status_counts == {"success": 10}
    assert result.network_requests == 10
    assert max(resume_sleeps) > 100.0
    connection = open_sidecar(output)
    try:
        attempts = connection.execute(
            "SELECT source_asset_id,max(attempt_no) "
            "FROM fetch_attempts GROUP BY source_asset_id"
        ).fetchall()
        assert len(attempts) == 10
        assert sum(int(row[1]) == 2 for row in attempts) == 2
        assert max(int(row[1]) for row in attempts) == 2
    finally:
        connection.close()


def test_interrupted_retry_keeps_attempt_numbers_and_cumulative_budget(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "retry.db"
    _source_db(source)
    assets = tuple(_asset(index) for index in range(10))

    class InterruptFetcher:
        network_requests = 0

        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            del asset, attempt_no
            self.network_requests += 1
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=10,
            asset_factory=lambda: iter(assets),
            fetcher_factory=InterruptFetcher,
            workers=1,
            requests_per_second=10_000,
            cooldown_seconds=0,
            batch_size=10,
        )

    partial = Path(str(output) + ".partial")
    connection = open_sidecar(partial)
    try:
        interrupted_id = str(
            connection.execute(
                "SELECT source_asset_id FROM fetch_attempts WHERE error_kind='interrupted'"
            ).fetchone()[0]
        )
        assert connection.execute(
            "SELECT max(attempt_no) FROM fetch_attempts WHERE source_asset_id=?",
            (interrupted_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


    never_called = False

    def changed_budget_fetcher() -> FetchResponse:
        nonlocal never_called
        never_called = True
        raise AssertionError("resume provenance drift must fail before fetch")

    with pytest.raises(PipelineError, match="max_attempts"):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=10,
            resume=True,
            asset_factory=lambda: iter(assets),
            fetcher_factory=changed_budget_fetcher,
            workers=1,
            requests_per_second=10_000,
            cooldown_seconds=0,
            batch_size=10,
            max_attempts=4,
        )
    assert not never_called


    class ResumeFetcher:
        network_requests = 0

        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            self.network_requests += 1
            if asset.source_asset_id == interrupted_id and attempt_no == 2:
                raise FetchFailure(
                    "http_503", "HTTP 503", retryable=True, http_status=503
                )
            return FetchResponse(200, asset.effective_fetch_url, "image/png", _png())

    result = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        resume=True,
        asset_factory=lambda: iter(assets),
        fetcher_factory=ResumeFetcher,
        workers=1,
        requests_per_second=10_000,
        circuit_breaker_threshold=8,
        cooldown_seconds=0,
        batch_size=10,
        max_attempts=3,
        sleep=lambda _seconds: None,
    )
    assert result.run_status == "complete"
    connection = open_sidecar(output)
    try:
        rows = connection.execute(
            "SELECT attempt_no,outcome,error_kind FROM fetch_attempts "
            "WHERE source_asset_id=? ORDER BY attempt_no",
            (interrupted_id,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "failed", "interrupted"),
            (2, "failed", "http_503"),
            (3, "success", None),
        ]
        assert connection.execute(
            "SELECT max(attempt_no) FROM fetch_attempts"
        ).fetchone()[0] == 3
    finally:
        connection.close()


def test_decode_interruption_keeps_durable_http_attempt_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "decode-interruption.db"
    _source_db(source)
    assets = tuple(_asset(index) for index in range(10))
    first_calls: list[tuple[str, int]] = []

    class FirstFetcher:
        network_requests = 0

        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            self.network_requests += 1
            first_calls.append((asset.source_asset_id, attempt_no))
            return FetchResponse(
                200, asset.effective_fetch_url, "image/png", _png()
            )

    original_fingerprint_bytes = pipeline.fingerprint_bytes

    def interrupt_decode(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("simulated process interruption during decode")

    monkeypatch.setattr(pipeline, "fingerprint_bytes", interrupt_decode)
    with pytest.raises(KeyboardInterrupt, match="during decode"):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=10,
            asset_factory=lambda: iter(assets),
            fetcher_factory=FirstFetcher,
            workers=1,
            requests_per_second=10_000,
            cooldown_seconds=0,
            batch_size=10,
            max_attempts=3,
        )

    assert len(first_calls) == 1
    interrupted_id = first_calls[0][0]
    partial = Path(str(output) + ".partial")
    connection = open_sidecar(partial)
    try:
        attempts = connection.execute(
            "SELECT source_asset_id,attempt_no,outcome FROM fetch_attempts"
        ).fetchall()
        assert [tuple(row) for row in attempts] == [
            (interrupted_id, 1, "success")
        ]
        assert tuple(
            connection.execute(
                "SELECT status,selected_attempt_no FROM fingerprints "
                "WHERE source_asset_id=?",
                (interrupted_id,),
            ).fetchone()
        ) == ("pending", None)
    finally:
        connection.close()

    resume_calls: list[tuple[str, int]] = []

    class ResumeFetcher:
        network_requests = 0

        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            self.network_requests += 1
            resume_calls.append((asset.source_asset_id, attempt_no))
            return FetchResponse(
                200, asset.effective_fetch_url, "image/png", _png()
            )

    monkeypatch.setattr(pipeline, "fingerprint_bytes", original_fingerprint_bytes)
    result = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        resume=True,
        asset_factory=lambda: iter(assets),
        fetcher_factory=ResumeFetcher,
        workers=1,
        requests_per_second=10_000,
        cooldown_seconds=0,
        batch_size=10,
        max_attempts=3,
    )

    assert result.run_status == "complete"
    assert result.network_requests == 10
    assert len(resume_calls) == 10
    assert dict(resume_calls)[interrupted_id] == 2
    assert sum(attempt_no == 1 for _, attempt_no in resume_calls) == 9
    connection = open_sidecar(output)
    try:
        rows = connection.execute(
            "SELECT attempt_no,outcome FROM fetch_attempts "
            "WHERE source_asset_id=? ORDER BY attempt_no",
            (interrupted_id,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "success"),
            (2, "success"),
        ]
        assert tuple(
            connection.execute(
                "SELECT status,selected_attempt_no FROM fingerprints "
                "WHERE source_asset_id=?",
                (interrupted_id,),
            ).fetchone()
        ) == ("success", 2)
    finally:
        connection.close()


def test_decode_interruption_persists_entire_completed_worker_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "decode-batch-interruption.db"
    _source_db(source)
    assets = tuple(_asset(index) for index in range(10))
    first_calls: list[tuple[str, int]] = []
    call_lock = threading.Lock()
    fetch_barrier = threading.Barrier(4)
    first_attempt_committed = threading.Event()
    partial = Path(str(output) + ".partial")

    class BatchFetcher:
        network_requests = 0

        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            self.network_requests += 1
            with call_lock:
                first_calls.append((asset.source_asset_id, attempt_no))
                call_position = len(first_calls)
            fetch_barrier.wait(timeout=5)
            if call_position != 1:
                assert first_attempt_committed.wait(timeout=5)
            return FetchResponse(
                200, asset.effective_fetch_url, "image/png", _png()
            )

    original_persist_attempt = pipeline._persist_attempt

    def release_slow_workers_after_first_commit(
        connection: sqlite3.Connection,
        row: tuple[object, ...],
    ) -> None:
        original_persist_attempt(connection, row)
        first_attempt_committed.set()

    original_fingerprint_bytes = pipeline.fingerprint_bytes

    def interrupt_decode(*_args: object, **_kwargs: object) -> None:
        connection = open_sidecar(partial)
        try:
            assert connection.execute(
                "SELECT count(*) FROM fetch_attempts"
            ).fetchone()[0] == 4
        finally:
            connection.close()
        raise KeyboardInterrupt("simulated first decode interruption")

    monkeypatch.setattr(
        pipeline, "_persist_attempt", release_slow_workers_after_first_commit
    )
    monkeypatch.setattr(pipeline, "fingerprint_bytes", interrupt_decode)
    with pytest.raises(KeyboardInterrupt, match="first decode interruption"):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=10,
            asset_factory=lambda: iter(assets),
            fetcher_factory=BatchFetcher,
            workers=4,
            requests_per_second=10_000,
            cooldown_seconds=0,
            batch_size=10,
            max_attempts=3,
        )

    assert len(first_calls) == 4
    completed_ids = {asset_id for asset_id, _ in first_calls}
    connection = open_sidecar(partial)
    try:
        rows = connection.execute(
            "SELECT source_asset_id,attempt_no,outcome FROM fetch_attempts"
        ).fetchall()
        assert len(rows) == 4
        assert {str(row[0]) for row in rows} == completed_ids
        assert {(int(row[1]), str(row[2])) for row in rows} == {
            (1, "success")
        }
        assert connection.execute(
            "SELECT count(*) FROM fingerprints WHERE status='pending'"
        ).fetchone()[0] == 10
    finally:
        connection.close()

    resume_calls: list[tuple[str, int]] = []

    class ResumeFetcher:
        network_requests = 0

        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            self.network_requests += 1
            with call_lock:
                resume_calls.append((asset.source_asset_id, attempt_no))
            return FetchResponse(
                200, asset.effective_fetch_url, "image/png", _png()
            )

    monkeypatch.setattr(pipeline, "_persist_attempt", original_persist_attempt)
    monkeypatch.setattr(pipeline, "fingerprint_bytes", original_fingerprint_bytes)
    result = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        resume=True,
        asset_factory=lambda: iter(assets),
        fetcher_factory=ResumeFetcher,
        workers=4,
        requests_per_second=10_000,
        cooldown_seconds=0,
        batch_size=10,
        max_attempts=3,
    )

    assert result.run_status == "complete"
    assert result.network_requests == 10
    assert len(resume_calls) == 10
    assert {
        asset_id for asset_id, attempt_no in resume_calls if attempt_no == 2
    } == completed_ids
    assert sum(attempt_no == 1 for _, attempt_no in resume_calls) == 6
    connection = open_sidecar(output)
    try:
        assert connection.execute(
            "SELECT count(*) FROM fetch_attempts"
        ).fetchone()[0] == 14
        selected_second = {
            str(row[0])
            for row in connection.execute(
                "SELECT source_asset_id FROM fingerprints "
                "WHERE status='success' AND selected_attempt_no=2"
            )
        }
        assert selected_second == completed_ids
    finally:
        connection.close()


def test_default_four_worker_breaker_cancels_sleeping_retry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "circuit-four-workers.db"
    _source_db(source)
    assets = tuple(_asset(index) for index in range(10))
    call_count = 0
    call_lock = threading.Lock()

    class StaggeredOverloadFetcher:
        network_requests = 0

        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            nonlocal call_count
            self.network_requests += 1
            with call_lock:
                call_count += 1
            if attempt_no == 1 and int(asset.source_asset_id.rsplit("-", 1)[1]) >= 7:
                time.sleep(0.15)
            raise FetchFailure(
                "http_503",
                "HTTP 503",
                retryable=True,
                http_status=503,
                retry_after_seconds=0,
            )

    started = time.monotonic()
    with pytest.raises(PipelineError, match="circuit breaker opened"):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=10,
            asset_factory=lambda: iter(assets),
            fetcher_factory=StaggeredOverloadFetcher,
            workers=4,
            requests_per_second=10_000,
            circuit_breaker_threshold=8,
            cooldown_seconds=0,
            batch_size=10,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 0.75
    assert call_count == 8
    connection = open_sidecar(Path(str(output) + ".partial"))
    try:
        assert connection.execute(
            "SELECT count(*) FROM fetch_attempts"
        ).fetchone()[0] == 8
        assert connection.execute(
            "SELECT count(*) FROM fingerprints WHERE status='pending'"
        ).fetchone()[0] == 10
    finally:
        connection.close()


def test_no_clobber_completed_resume_zero_requests_and_no_image_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "complete.db"
    _source_db(source)
    before = _source_sha(source)
    assets = tuple(_asset(index) for index in range(10))
    healthy = _ConcurrentFetchState(_png())

    first = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        asset_factory=lambda: iter(assets),
        fetcher_factory=healthy.factory,
        workers=4,
        requests_per_second=10_000,
        cooldown_seconds=0,
    )
    assert first.run_status == "complete"
    assert _source_sha(source) == before

    def never_factory():
        pytest.fail("completed resume must not create a network fetcher")

    resumed = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        resume=True,
        asset_factory=lambda: iter(assets),
        fetcher_factory=never_factory,
        workers=4,
        requests_per_second=10_000,
        cooldown_seconds=0,
    )
    assert resumed.already_complete
    assert resumed.network_requests == 0

    with pytest.raises(FileExistsError, match="clobber"):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=10,
            asset_factory=lambda: iter(assets),
            fetcher_factory=never_factory,
            workers=4,
            requests_per_second=10_000,
            cooldown_seconds=0,
        )
    assert _source_sha(source) == before
    assert not any(
        path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_large_sorted_inventory_plan_has_bounded_memory() -> None:
    count = 10_001
    padding = "x" * 2048

    def factory():
        for index in range(count):
            base = _asset(index)
            long_id = f"{index:06d}-{padding}"
            yield SourceAsset(
                source=base.source,
                source_asset_id=long_id,
                source_asset_key=long_id,
                normalized_url=base.normalized_url,
                selected_raw_url=base.selected_raw_url,
                effective_fetch_url=base.effective_fetch_url,
                source_urls=base.source_urls,
                occurrence_count=base.occurrence_count,
                parent_count=base.parent_count,
                roles=base.roles,
                format_lane=base.format_lane,
                fetch_profile_version=base.fetch_profile_version,
            )

    tracemalloc.start()
    try:
        plan = pipeline._selection_plan(
            factory, "divisare", 10, "bounded-v1", ordered=True
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert plan.count == 10
    assert plan.eligible_inventory_count == count
    assert peak < 8 * 1024 * 1024


def test_initialization_commits_5000_rows_and_resumes_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "initialization.db"
    _source_db(source)
    count = 10_001

    def asset_factory():
        for index in range(count):
            yield _asset(index)

    original_commit = pipeline._commit_initialization_batch
    commit_calls = 0

    def interrupt_after_commit(
        connection: sqlite3.Connection,
        run_id: str,
        inventory_count: int,
        selected_count: int,
        excluded_count: int,
    ) -> None:
        nonlocal commit_calls
        original_commit(
            connection,
            run_id,
            inventory_count,
            selected_count,
            excluded_count,
        )
        commit_calls += 1
        if commit_calls == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        pipeline, "_commit_initialization_batch", interrupt_after_commit
    )
    with pytest.raises(KeyboardInterrupt):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=None,
            asset_factory=asset_factory,
            fetcher=lambda *_args: pytest.fail("initialization reached network"),
            requests_per_second=10_000,
            cooldown_seconds=0,
        )

    partial = Path(str(output) + ".partial")
    connection = open_sidecar(partial)
    try:
        progress = connection.execute(
            "SELECT status,initialized_inventory_count,"
            "initialized_selected_count,initialized_excluded_count "
            "FROM fingerprint_runs"
        ).fetchone()
        assert tuple(progress) == ("initializing", 5_000, 5_000, 0)
        assert connection.execute(
            "SELECT count(*) FROM source_assets"
        ).fetchone()[0] == 5_000
        assert connection.execute(
            "SELECT count(*) FROM fingerprints"
        ).fetchone()[0] == 5_000
    finally:
        connection.close()

    monkeypatch.setattr(pipeline, "_commit_initialization_batch", original_commit)

    class StopAtFirstFetch:
        def __call__(self, asset: SourceAsset, attempt_no: int) -> FetchResponse:
            del asset, attempt_no
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=output,
            sample_size=None,
            resume=True,
            asset_factory=asset_factory,
            fetcher=StopAtFirstFetch(),
            requests_per_second=10_000,
            cooldown_seconds=0,
        )
    connection = open_sidecar(partial)
    try:
        progress = connection.execute(
            "SELECT status,initialized_inventory_count,"
            "initialized_selected_count,initialized_excluded_count "
            "FROM fingerprint_runs"
        ).fetchone()
        assert tuple(progress) == ("running", count, count, 0)
        assert connection.execute(
            "SELECT count(*) FROM source_assets"
        ).fetchone()[0] == count
        assert connection.execute(
            "SELECT count(*) FROM fingerprints"
        ).fetchone()[0] == count
        assert connection.execute(
            "SELECT count(*) FROM fetch_attempts"
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "assets",
    [
        (_asset(2), _asset(1)),
        (_asset(1), _asset(1)),
    ],
)
def test_inventory_ids_must_be_strictly_increasing(
    assets: tuple[SourceAsset, ...],
) -> None:
    with pytest.raises(PipelineError, match="strictly increasing"):
        pipeline._selection_plan(
            lambda: iter(assets), "divisare", 10, "order-v1", ordered=True
        )


def test_architizer_common_runner_offline_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "architizer.db"
    output = tmp_path / "architizer-e1.db"
    _make_architizer_inventory_db(source)
    before = _source_sha(source)
    fetch_state = _ConcurrentFetchState(_png())

    result = run_image_fingerprint_pipeline(
        source="architizer",
        source_db=source,
        output=output,
        sample_size=10,
        fetcher_factory=fetch_state.factory,
        workers=4,
        requests_per_second=10_000,
        cooldown_seconds=0,
        batch_size=8,
    )

    assert result.run_status == "complete"
    assert result.status_counts == {"success": 10}
    assert result.network_requests == 10
    assert _source_sha(source) == before
    connection = open_sidecar(output)
    try:
        assert connection.execute(
            "SELECT count(*) FROM source_assets"
        ).fetchone()[0] == 10
        assert dict(
            connection.execute(
                "SELECT reason_code,count(*) FROM source_asset_exclusions "
                "GROUP BY reason_code ORDER BY reason_code"
            ).fetchall()
        ) == {"hard_skip_extension": 1, "placeholder_candidate": 1}
    finally:
        connection.close()
    validation = validate_image_fingerprint_sidecar(output, source)
    assert validation.passed, validation.to_dict()


def test_exclusion_ledger_and_independent_manifest_tamper_detection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "divisare.db"
    output = tmp_path / "divisare-e1.db"
    _make_divisare_inventory_db(source)
    before = _source_sha(source)
    fetch_state = _ConcurrentFetchState(_png())
    result = run_image_fingerprint_pipeline(
        source="divisare",
        source_db=source,
        output=output,
        sample_size=10,
        fetcher_factory=fetch_state.factory,
        workers=4,
        requests_per_second=10_000,
        cooldown_seconds=0,
        batch_size=8,
    )
    assert result.run_status == "complete"
    assert _source_sha(source) == before

    connection = open_sidecar(output)
    try:
        run = connection.execute(
            "SELECT source_total_count,eligible_count,excluded_count "
            "FROM fingerprint_runs"
        ).fetchone()
        assert tuple(run) == (12, 10, 2)
        exclusions = connection.execute(
            "SELECT reason_code,source_record_sha256,provenance_json,detail_json "
            "FROM source_asset_exclusions ORDER BY inventory_rank"
        ).fetchall()
        assert [row[0] for row in exclusions] == [
            "hard_skip_extension",
            "unsupported_source_url",
        ]
        for row in exclusions:
            assert row[1] == hashlib.sha256(row[2].encode("ascii")).hexdigest()
            assert json.loads(row[3])["reason_code"] == row[0]
    finally:
        connection.close()

    valid = validate_image_fingerprint_sidecar(output, source)
    assert valid.passed, valid.to_dict()

    exclusion_tampered = tmp_path / "exclusion-tampered.db"
    shutil.copyfile(output, exclusion_tampered)
    connection = sqlite3.connect(exclusion_tampered)
    try:
        trigger_rows = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name='source_asset_exclusions'"
        ).fetchall()
        for trigger_name, _trigger_sql in trigger_rows:
            escaped = str(trigger_name).replace('"', '""')
            connection.execute(f'DROP TRIGGER "{escaped}"')
        connection.execute(
            "UPDATE source_asset_exclusions SET detail_json='{}' "
            "WHERE inventory_rank=(SELECT min(inventory_rank) "
            "FROM source_asset_exclusions)"
        )
        for _trigger_name, trigger_sql in trigger_rows:
            connection.execute(str(trigger_sql))
        connection.commit()
    finally:
        connection.close()

    resume_fetches = 0

    def forbidden_factory():
        nonlocal resume_fetches
        resume_fetches += 1
        raise AssertionError("completed resume must validate before fetching")

    with pytest.raises(PipelineError, match="valid complete run"):
        run_image_fingerprint_pipeline(
            source="divisare",
            source_db=source,
            output=exclusion_tampered,
            sample_size=10,
            resume=True,
            fetcher_factory=forbidden_factory,
            workers=4,
            requests_per_second=10_000,
            cooldown_seconds=0,
            batch_size=8,
        )
    assert resume_fetches == 0

    tampered = tmp_path / "tampered.db"
    shutil.copyfile(output, tampered)
    connection = sqlite3.connect(tampered)
    try:
        trigger_rows = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name IN ('source_assets','source_asset_exclusions')"
        ).fetchall()
        for trigger_name, _trigger_sql in trigger_rows:
            escaped = str(trigger_name).replace('"', '""')
            connection.execute(f'DROP TRIGGER "{escaped}"')
        connection.execute(
            "UPDATE source_asset_exclusions SET detail_json='{}' "
            "WHERE inventory_rank=(SELECT min(inventory_rank) "
            "FROM source_asset_exclusions)"
        )
        first_two = connection.execute(
            "SELECT source_asset_id,selection_rank FROM source_assets "
            "ORDER BY selection_rank LIMIT 2"
        ).fetchall()
        first_id, first_rank = first_two[0]
        second_id, second_rank = first_two[1]
        connection.execute(
            "UPDATE source_assets SET selection_rank=999999 WHERE source_asset_id=?",
            (first_id,),
        )
        connection.execute(
            "UPDATE source_assets SET selection_rank=? WHERE source_asset_id=?",
            (first_rank, second_id),
        )
        connection.execute(
            "UPDATE source_assets SET selection_rank=? WHERE source_asset_id=?",
            (second_rank, first_id),
        )
        for _trigger_name, trigger_sql in trigger_rows:
            connection.execute(str(trigger_sql))
        connection.commit()
    finally:
        connection.close()

    invalid = validate_image_fingerprint_sidecar(tampered, source)
    assert not invalid.passed
    checks = {check.name: check.passed for check in invalid.checks}
    assert checks["exclusion_ledger_accounting"] is False
    assert checks["ordered_selection_manifest"] is False

    missing_trigger = tmp_path / "missing-trigger.db"
    shutil.copyfile(output, missing_trigger)
    connection = sqlite3.connect(missing_trigger)
    try:
        connection.execute("DROP TRIGGER fetch_attempts_retry_budget_insert")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SidecarSchemaError, match="missing sidecar triggers"):
        open_sidecar(missing_trigger)
