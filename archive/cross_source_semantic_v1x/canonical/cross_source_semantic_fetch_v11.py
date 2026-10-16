"""Reservation-aware HTTP transport for semantic Vision v1.1 runs.

The frozen v1 transport deliberately exposes one *logical* fetch attempt.  A
logical attempt may, however, contain several physical requests when redirects
are followed.  This adapter keeps the frozen decoder/host policy intact while
making every physical ``Session.get`` visible to a durable reservation ledger.

The callback contract is intentionally storage-neutral.  ``reserve`` must
durably commit before it returns; ``complete`` must durably commit the outcome
before it returns.  A process death between those calls therefore leaves an
open reservation which a runner can conservatively close as
``interrupted_unknown`` without repeating it invisibly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from canonical.cross_source_semantic_fetch import FetchPayload, fetch_once


FETCH_CONTRACT_VERSION = "archibe-cross-source-semantic-fetch-v1.1"


@dataclass(frozen=True)
class PhysicalRequestCompletion:
    """The observable outcome of one physical HTTP invocation."""

    reservation_id: int
    request_url: str
    hop_no: int
    elapsed_seconds: float
    http_status: int | None
    outcome: str
    error_kind: str | None = None
    error_message: str | None = None


class ReservationCallbacks(Protocol):
    """Durable callbacks used immediately around each physical request."""

    def reserve(self, request_url: str, hop_no: int) -> int: ...

    def complete(self, completion: PhysicalRequestCompletion) -> None: ...


class _ReservedSession:
    """Small requests-compatible proxy that accounts every ``get`` call."""

    def __init__(
        self,
        session: Any,
        callbacks: ReservationCallbacks,
        *,
        clock: Callable[[], float],
    ) -> None:
        self._session = session
        self._callbacks = callbacks
        self._clock = clock
        self._hop_no = 0
        self.headers = session.headers

    def get(self, url: str, **kwargs: Any) -> Any:
        self._hop_no += 1
        hop_no = self._hop_no
        reservation_id = self._callbacks.reserve(url, hop_no)
        started = self._clock()
        try:
            response = self._session.get(url, **kwargs)
        except BaseException as exc:
            # ``complete`` is deliberately attempted even for BaseException.
            # A hard process kill may still prevent it; the open reservation is
            # then authoritative evidence that an unknown call may have run.
            self._callbacks.complete(
                PhysicalRequestCompletion(
                    reservation_id=reservation_id,
                    request_url=url,
                    hop_no=hop_no,
                    elapsed_seconds=max(0.0, self._clock() - started),
                    http_status=None,
                    outcome="exception",
                    error_kind=type(exc).__name__,
                    error_message=str(exc)[:2000] or type(exc).__name__,
                )
            )
            raise
        self._callbacks.complete(
            PhysicalRequestCompletion(
                reservation_id=reservation_id,
                request_url=url,
                hop_no=hop_no,
                elapsed_seconds=max(0.0, self._clock() - started),
                http_status=int(response.status_code),
                outcome="response_received",
            )
        )
        return response

    def close(self) -> None:
        self._session.close()


def fetch_once_reserved(
    source: str,
    request_url: str,
    *,
    callbacks: ReservationCallbacks,
    session: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    **kwargs: Any,
) -> FetchPayload:
    """Run one logical fetch while reserving and pacing every physical hop.

    The caller owns retry policy.  When ``session`` is omitted this function
    owns and closes one requests session.  An injected session is never closed.
    """

    if callbacks is None:
        raise ValueError("durable reservation callbacks are required")
    owned = session is None
    if session is None:
        import requests

        session = requests.Session()
        session.headers.update(
            {
                "Accept": "image/*",
                "User-Agent": "Archibe-Semantic-Vision-Fetch/1.1",
            }
        )
    proxy = _ReservedSession(session, callbacks, clock=clock)
    try:
        return fetch_once(
            source,
            request_url,
            session=proxy,
            clock=clock,
            **kwargs,
        )
    finally:
        if owned:
            proxy.close()


__all__ = [
    "FETCH_CONTRACT_VERSION",
    "PhysicalRequestCompletion",
    "ReservationCallbacks",
    "fetch_once_reserved",
]
