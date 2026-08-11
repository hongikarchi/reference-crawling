from __future__ import annotations

import hashlib
import io
from collections import deque
from datetime import datetime, timezone

import pytest
import requests
from PIL import Image

from canonical.cross_source_semantic_fetch import (
    FetchFailure,
    fetch_once,
    parse_retry_after,
    validate_fetch_url,
)


def _png() -> bytes:
    image = Image.new("RGB", (12, 8), (20, 80, 140))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class FakeResponse:
    def __init__(
        self,
        status: int,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int):
        assert chunk_size == 64 * 1024
        yield from (self.chunks if self.chunks is not None else [self.body])

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, outcomes) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def test_validate_fetch_url_is_source_specific_and_https_only() -> None:
    divisare = "https://images.divisare.com/images/example.jpg"
    architizer = "https://architizer-prod.imgix.net/example.jpg?w=1024"
    assert validate_fetch_url("divisare", divisare) == divisare
    assert validate_fetch_url("architizer", architizer) == architizer

    invalid = [
        ("divisare", "http://images.divisare.com/images/example.jpg"),
        ("divisare", architizer),
        ("architizer", divisare),
        ("architizer", "https://architizer-prod.imgix.net:443/example.jpg"),
        ("divisare", "https://user@images.divisare.com/example.jpg"),
        ("divisare", "https://images.divisare.com/example.jpg#fragment"),
        ("other", divisare),
    ]
    for source, url in invalid:
        with pytest.raises(ValueError):
            validate_fetch_url(source, url)


def test_success_returns_bounded_decoded_payload_without_retry() -> None:
    raw = _png()
    response = FakeResponse(
        200,
        body=raw,
        headers={"Content-Type": "image/png", "Content-Length": str(len(raw))},
    )
    session = FakeSession([response])
    url = "https://images.divisare.com/images/example.png"

    payload = fetch_once("divisare", url, session=session)

    assert payload.request_url == url
    assert payload.final_url == url
    assert payload.http_status == 200
    assert payload.content_type == "image/png"
    assert payload.body == raw
    assert payload.raw_response_sha256 == hashlib.sha256(raw).hexdigest()
    assert (payload.decoded_image.width, payload.decoded_image.height) == (12, 8)
    assert payload.decoded_image.decoded_format == "PNG"
    assert payload.request_count == 1
    assert payload.redirect_chain == ()
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert response.closed is True
    assert session.closed is False


def test_manual_same_source_redirect_is_recorded() -> None:
    raw = _png()
    first = FakeResponse(302, headers={"Location": "/images/final.png"})
    second = FakeResponse(200, body=raw, headers={"Content-Type": "image/png"})
    session = FakeSession([first, second])

    result = fetch_once(
        "divisare",
        "https://images.divisare.com/images/start.png",
        session=session,
    )

    assert result.final_url == "https://images.divisare.com/images/final.png"
    assert result.request_count == 2
    assert len(result.redirect_chain) == 1
    assert result.redirect_chain[0].http_status == 302
    assert first.closed and second.closed


def test_redirect_cannot_cross_to_other_allowed_source() -> None:
    response = FakeResponse(
        302,
        headers={"Location": "https://architizer-prod.imgix.net/example.jpg"},
    )
    session = FakeSession([response])

    with pytest.raises(FetchFailure) as captured:
        fetch_once(
            "divisare",
            "https://images.divisare.com/images/start.jpg",
            session=session,
        )

    failure = captured.value
    assert failure.kind == "redirect_host"
    assert failure.retryable is False
    assert failure.request_count == 1
    assert len(session.calls) == 1
    assert response.closed


def test_retryable_http_reports_retry_after_and_does_not_retry() -> None:
    response = FakeResponse(
        429,
        headers={"Content-Type": "text/html", "Retry-After": "17"},
    )
    session = FakeSession(
        [response, FakeResponse(200, body=_png(), headers={"Content-Type": "image/png"})]
    )

    with pytest.raises(FetchFailure) as captured:
        fetch_once(
            "architizer",
            "https://architizer-prod.imgix.net/example.jpg",
            session=session,
        )

    failure = captured.value
    assert failure.kind == "http_429"
    assert failure.retryable is True
    assert failure.http_status == 429
    assert failure.retry_after_seconds == 17.0
    assert failure.request_count == 1
    assert len(session.calls) == 1


def test_http_date_retry_after_and_timeout_are_classified() -> None:
    target = datetime(2026, 8, 11, 12, 0, 30, tzinfo=timezone.utc)
    now = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    assert parse_retry_after(
        target.strftime("%a, %d %b %Y %H:%M:%S GMT"), wall_time=lambda: now
    ) == 30.0

    session = FakeSession([requests.Timeout("slow"), FakeResponse(200)])
    with pytest.raises(FetchFailure) as captured:
        fetch_once(
            "divisare",
            "https://images.divisare.com/images/example.jpg",
            session=session,
        )
    assert captured.value.kind == "timeout"
    assert captured.value.retryable is True
    assert captured.value.request_count == 1
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("response", "expected_kind"),
    [
        (
            FakeResponse(200, body=b"<html>blocked</html>", headers={"Content-Type": "text/html"}),
            "invalid_content_type",
        ),
        (
            FakeResponse(200, body=b"not a jpeg", headers={"Content-Type": "image/jpeg"}),
            "decode_failed",
        ),
    ],
)
def test_content_type_and_decode_gates(response: FakeResponse, expected_kind: str) -> None:
    session = FakeSession([response])
    with pytest.raises(FetchFailure) as captured:
        fetch_once(
            "architizer",
            "https://architizer-prod.imgix.net/example.jpg",
            session=session,
        )
    failure = captured.value
    assert failure.kind == expected_kind
    assert failure.retryable is False
    if expected_kind == "decode_failed":
        assert failure.raw_response_sha256 == hashlib.sha256(b"not a jpeg").hexdigest()


def test_declared_and_streamed_oversize_are_bounded() -> None:
    declared = FakeResponse(
        200,
        headers={"Content-Type": "image/jpeg", "Content-Length": "11"},
    )
    with pytest.raises(FetchFailure) as captured:
        fetch_once(
            "architizer",
            "https://architizer-prod.imgix.net/example.jpg",
            session=FakeSession([declared]),
            max_response_bytes=10,
        )
    assert captured.value.kind == "oversize"
    assert captured.value.response_bytes == 11

    streamed = FakeResponse(
        200,
        headers={"Content-Type": "image/jpeg"},
        chunks=[b"123456", b"78901"],
    )
    with pytest.raises(FetchFailure) as captured:
        fetch_once(
            "architizer",
            "https://architizer-prod.imgix.net/example.jpg",
            session=FakeSession([streamed]),
            max_response_bytes=10,
        )
    assert captured.value.kind == "oversize"
    assert captured.value.response_bytes == 11


def test_invalid_initial_url_makes_zero_requests() -> None:
    session = FakeSession([FakeResponse(200)])
    with pytest.raises(FetchFailure) as captured:
        fetch_once("divisare", "https://example.com/image.jpg", session=session)
    assert captured.value.kind == "invalid_url"
    assert captured.value.request_count == 0
    assert session.calls == []
