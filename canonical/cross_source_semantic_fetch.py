"""Bounded, source-neutral HTTP fetch primitive for semantic Vision inputs.

The public :func:`fetch_once` function performs one *logical* fetch attempt.
It follows a bounded number of manually validated redirects, but it never
retries, sleeps, or applies backoff.  A sidecar runner can therefore commit the
returned payload or :class:`FetchFailure` before deciding whether another
attempt is allowed.

Only the two image delivery hosts already frozen by E1 are accepted.  This
module does not run Vision, persist image bytes, or modify any source artifact.
"""

from __future__ import annotations

import hashlib
import io
import math
import time
import warnings
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlsplit


FETCH_CONTRACT_VERSION = "archibe-cross-source-semantic-fetch-v1"
DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_SOURCE_PIXELS = 80_000_000
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_REDIRECTS = 5
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
REDIRECT_HTTP_STATUSES = frozenset({301, 302, 303, 307, 308})
SOURCE_HOSTS: Mapping[str, str] = {
    "architizer": "architizer-prod.imgix.net",
    "divisare": "images.divisare.com",
}


class _Response(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def iter_content(self, chunk_size: int) -> Any: ...

    def close(self) -> None: ...


class _Session(Protocol):
    headers: Any

    def get(self, url: str, **kwargs: Any) -> _Response: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RedirectHop:
    request_url: str
    http_status: int
    location: str
    next_url: str


@dataclass(frozen=True)
class DecodedImageInfo:
    decoded_format: str
    width: int
    height: int
    mode: str
    frame_count: int


@dataclass(frozen=True)
class FetchPayload:
    """One successfully downloaded and decoded image response."""

    request_url: str
    final_url: str
    http_status: int
    content_type: str
    body: bytes
    raw_response_sha256: str
    decoded_image: DecodedImageInfo
    elapsed_seconds: float
    request_count: int
    redirect_chain: tuple[RedirectHop, ...]


class FetchFailure(RuntimeError):
    """Typed evidence for one failed logical fetch attempt."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool,
        request_url: str,
        final_url: str | None = None,
        http_status: int | None = None,
        content_type: str | None = None,
        response_bytes: int | None = None,
        raw_response_sha256: str | None = None,
        retry_after_seconds: float | None = None,
        elapsed_seconds: float = 0.0,
        request_count: int = 0,
        redirect_chain: tuple[RedirectHop, ...] = (),
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.request_url = request_url
        self.final_url = final_url
        self.http_status = http_status
        self.content_type = content_type
        self.response_bytes = response_bytes
        self.raw_response_sha256 = raw_response_sha256
        self.retry_after_seconds = retry_after_seconds
        self.elapsed_seconds = elapsed_seconds
        self.request_count = request_count
        self.redirect_chain = redirect_chain


def validate_fetch_url(source: str, url: str) -> str:
    """Return ``url`` when it is HTTPS on the source's exact delivery host."""

    expected_host = SOURCE_HOSTS.get(source)
    if expected_host is None:
        raise ValueError(f"unsupported semantic image source: {source!r}")
    if not isinstance(url, str) or not url:
        raise ValueError("fetch URL must be a non-empty string")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("fetch URL is malformed") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or host != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path
        or parsed.fragment
    ):
        raise ValueError(
            f"fetch URL must be fragment-free HTTPS on {expected_host} without credentials or port"
        )
    return url


def parse_retry_after(
    value: str | None,
    *,
    wall_time: Callable[[], float] = time.time,
) -> float | None:
    """Parse delta-seconds or an HTTP date into a non-negative delay."""

    if not value or not value.strip():
        return None
    stripped = value.strip()
    try:
        seconds = float(stripped)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(stripped)
            if parsed.tzinfo is None:
                from datetime import timezone

                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = parsed.timestamp() - wall_time()
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _content_type(headers: Mapping[str, str]) -> str | None:
    value = headers.get("Content-Type")
    if value is None:
        return None
    normalized = str(value).split(";", 1)[0].strip().casefold()
    return normalized or None


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def validate_decodable_image(
    raw: bytes,
    *,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
) -> DecodedImageInfo:
    """Decode enough of ``raw`` to reject block pages and corrupt images."""

    from PIL import Image, UnidentifiedImageError

    if not raw:
        raise ValueError("image response is empty")
    if max_source_pixels < 1:
        raise ValueError("max_source_pixels must be positive")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise ValueError("decoded image has non-positive dimensions")
                if width * height > max_source_pixels:
                    raise ValueError(
                        f"decoded image exceeds {max_source_pixels} source pixels"
                    )
                try:
                    frame_count = int(getattr(image, "n_frames", 1) or 1)
                except (EOFError, OSError, TypeError, ValueError):
                    frame_count = 1
                image.seek(0)
                image.load()
                return DecodedImageInfo(
                    decoded_format=str(image.format or "").upper(),
                    width=width,
                    height=height,
                    mode=str(image.mode),
                    frame_count=frame_count,
                )
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError(f"image decompression-bomb guard rejected response: {exc}") from exc
    except (UnidentifiedImageError, EOFError, OSError, SyntaxError) as exc:
        raise ValueError(f"image decode failed: {exc}") from exc


def _failure(
    *,
    kind: str,
    message: str,
    retryable: bool,
    original_url: str,
    current_url: str | None,
    started: float,
    clock: Callable[[], float],
    request_count: int,
    redirects: list[RedirectHop],
    http_status: int | None = None,
    content_type: str | None = None,
    response_bytes: int | None = None,
    raw_response_sha256: str | None = None,
    retry_after_seconds: float | None = None,
) -> FetchFailure:
    return FetchFailure(
        kind,
        message,
        retryable=retryable,
        request_url=original_url,
        final_url=current_url,
        http_status=http_status,
        content_type=content_type,
        response_bytes=response_bytes,
        raw_response_sha256=raw_response_sha256,
        retry_after_seconds=retry_after_seconds,
        elapsed_seconds=max(0.0, clock() - started),
        request_count=request_count,
        redirect_chain=tuple(redirects),
    )


def fetch_once(
    source: str,
    request_url: str,
    *,
    session: _Session | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    clock: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], float] = time.time,
) -> FetchPayload:
    """Fetch exactly once, returning evidence or raising :class:`FetchFailure`.

    Redirect requests belong to the same logical attempt and are exposed in
    ``request_count`` and ``redirect_chain``.  A retryable failure is only a
    recommendation to the caller; this function never performs the retry.
    """

    if max_response_bytes < 1 or max_source_pixels < 1:
        raise ValueError("response byte and source-pixel caps must be positive")
    if max_redirects < 0:
        raise ValueError("max_redirects must be non-negative")
    if (
        not math.isfinite(connect_timeout_seconds)
        or connect_timeout_seconds <= 0
        or not math.isfinite(read_timeout_seconds)
        or read_timeout_seconds <= 0
    ):
        raise ValueError("connect and read timeouts must be positive finite values")

    import requests

    started = clock()
    redirects: list[RedirectHop] = []
    request_count = 0
    current_url: str | None = request_url
    try:
        current_url = validate_fetch_url(source, request_url)
    except ValueError as exc:
        raise _failure(
            kind="invalid_url",
            message=str(exc),
            retryable=False,
            original_url=request_url,
            current_url=current_url,
            started=started,
            clock=clock,
            request_count=0,
            redirects=redirects,
        ) from exc

    owned_session = session is None
    active_session: _Session = session if session is not None else requests.Session()
    if owned_session:
        active_session.headers.update(
            {
                "Accept": "image/*",
                "User-Agent": "Archibe-Semantic-Vision-Fetch/1.0",
            }
        )
    try:
        while True:
            request_count += 1
            try:
                response = active_session.get(
                    current_url,
                    timeout=(connect_timeout_seconds, read_timeout_seconds),
                    stream=True,
                    allow_redirects=False,
                )
            except requests.Timeout as exc:
                raise _failure(
                    kind="timeout",
                    message=str(exc) or "HTTP request timed out",
                    retryable=True,
                    original_url=request_url,
                    current_url=current_url,
                    started=started,
                    clock=clock,
                    request_count=request_count,
                    redirects=redirects,
                ) from exc
            except requests.ConnectionError as exc:
                raise _failure(
                    kind="connection",
                    message=str(exc) or "HTTP connection failed",
                    retryable=True,
                    original_url=request_url,
                    current_url=current_url,
                    started=started,
                    clock=clock,
                    request_count=request_count,
                    redirects=redirects,
                ) from exc
            except requests.RequestException as exc:
                raise _failure(
                    kind="request",
                    message=str(exc) or "HTTP request failed",
                    retryable=False,
                    original_url=request_url,
                    current_url=current_url,
                    started=started,
                    clock=clock,
                    request_count=request_count,
                    redirects=redirects,
                ) from exc

            try:
                status = int(response.status_code)
                content_type = _content_type(response.headers)
                declared_length = _content_length(response.headers)
                if status in REDIRECT_HTTP_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise _failure(
                            kind="redirect_missing_location",
                            message="redirect response has no Location header",
                            retryable=False,
                            original_url=request_url,
                            current_url=current_url,
                            started=started,
                            clock=clock,
                            request_count=request_count,
                            redirects=redirects,
                            http_status=status,
                            content_type=content_type,
                        )
                    if len(redirects) >= max_redirects:
                        raise _failure(
                            kind="redirect_limit",
                            message="redirect limit exceeded",
                            retryable=False,
                            original_url=request_url,
                            current_url=current_url,
                            started=started,
                            clock=clock,
                            request_count=request_count,
                            redirects=redirects,
                            http_status=status,
                            content_type=content_type,
                        )
                    next_url = urljoin(current_url, str(location))
                    try:
                        validate_fetch_url(source, next_url)
                    except ValueError as exc:
                        raise _failure(
                            kind="redirect_host",
                            message=str(exc),
                            retryable=False,
                            original_url=request_url,
                            current_url=next_url,
                            started=started,
                            clock=clock,
                            request_count=request_count,
                            redirects=redirects,
                            http_status=status,
                            content_type=content_type,
                        ) from exc
                    redirects.append(
                        RedirectHop(current_url, status, str(location), next_url)
                    )
                    current_url = next_url
                    continue

                if not 200 <= status < 300:
                    raise _failure(
                        kind=f"http_{status}",
                        message=f"HTTP {status}",
                        retryable=status in RETRYABLE_HTTP_STATUSES,
                        original_url=request_url,
                        current_url=current_url,
                        started=started,
                        clock=clock,
                        request_count=request_count,
                        redirects=redirects,
                        http_status=status,
                        content_type=content_type,
                        response_bytes=declared_length,
                        retry_after_seconds=parse_retry_after(
                            response.headers.get("Retry-After"), wall_time=wall_time
                        ),
                    )
                if content_type is None or not content_type.startswith("image/"):
                    raise _failure(
                        kind="invalid_content_type",
                        message=f"expected image/* Content-Type, got {content_type!r}",
                        retryable=False,
                        original_url=request_url,
                        current_url=current_url,
                        started=started,
                        clock=clock,
                        request_count=request_count,
                        redirects=redirects,
                        http_status=status,
                        content_type=content_type,
                        response_bytes=declared_length,
                    )
                if declared_length is not None and declared_length > max_response_bytes:
                    raise _failure(
                        kind="oversize",
                        message="Content-Length exceeds response byte cap",
                        retryable=False,
                        original_url=request_url,
                        current_url=current_url,
                        started=started,
                        clock=clock,
                        request_count=request_count,
                        redirects=redirects,
                        http_status=status,
                        content_type=content_type,
                        response_bytes=declared_length,
                    )

                body = bytearray()
                try:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        body.extend(chunk)
                        if len(body) > max_response_bytes:
                            raise _failure(
                                kind="oversize",
                                message="stream exceeds response byte cap",
                                retryable=False,
                                original_url=request_url,
                                current_url=current_url,
                                started=started,
                                clock=clock,
                                request_count=request_count,
                                redirects=redirects,
                                http_status=status,
                                content_type=content_type,
                                response_bytes=len(body),
                            )
                except FetchFailure:
                    raise
                except requests.RequestException as exc:
                    raise _failure(
                        kind="stream",
                        message=str(exc) or "response stream failed",
                        retryable=True,
                        original_url=request_url,
                        current_url=current_url,
                        started=started,
                        clock=clock,
                        request_count=request_count,
                        redirects=redirects,
                        http_status=status,
                        content_type=content_type,
                        response_bytes=len(body),
                    ) from exc

                raw = bytes(body)
                raw_sha = hashlib.sha256(raw).hexdigest()
                if not raw:
                    raise _failure(
                        kind="empty_response",
                        message="successful HTTP response is empty",
                        retryable=False,
                        original_url=request_url,
                        current_url=current_url,
                        started=started,
                        clock=clock,
                        request_count=request_count,
                        redirects=redirects,
                        http_status=status,
                        content_type=content_type,
                        response_bytes=0,
                        raw_response_sha256=raw_sha,
                    )
                try:
                    decoded = validate_decodable_image(
                        raw, max_source_pixels=max_source_pixels
                    )
                except ValueError as exc:
                    raise _failure(
                        kind="decode_failed",
                        message=str(exc),
                        retryable=False,
                        original_url=request_url,
                        current_url=current_url,
                        started=started,
                        clock=clock,
                        request_count=request_count,
                        redirects=redirects,
                        http_status=status,
                        content_type=content_type,
                        response_bytes=len(raw),
                        raw_response_sha256=raw_sha,
                    ) from exc
                return FetchPayload(
                    request_url=request_url,
                    final_url=current_url,
                    http_status=status,
                    content_type=content_type,
                    body=raw,
                    raw_response_sha256=raw_sha,
                    decoded_image=decoded,
                    elapsed_seconds=max(0.0, clock() - started),
                    request_count=request_count,
                    redirect_chain=tuple(redirects),
                )
            finally:
                response.close()
    finally:
        if owned_session:
            active_session.close()


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MAX_SOURCE_PIXELS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DecodedImageInfo",
    "FETCH_CONTRACT_VERSION",
    "FetchFailure",
    "FetchPayload",
    "REDIRECT_HTTP_STATUSES",
    "RETRYABLE_HTTP_STATUSES",
    "RedirectHop",
    "SOURCE_HOSTS",
    "fetch_once",
    "parse_retry_after",
    "validate_decodable_image",
    "validate_fetch_url",
]
