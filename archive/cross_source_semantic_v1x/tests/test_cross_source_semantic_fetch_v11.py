from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from PIL import Image
import io

from canonical.cross_source_semantic_fetch_v11 import (
    PhysicalRequestCompletion,
    fetch_once_reserved,
)


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (23, 45, 67)).save(buffer, format="JPEG")
    return buffer.getvalue()


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status_code = status
        self.headers = headers
        self._body = body

    def iter_content(self, chunk_size: int):
        yield self._body

    def close(self) -> None:
        pass


class _Session:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.headers: dict[str, str] = {}
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.urls.append(url)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None:
        pass


@dataclass
class _Ledger:
    reservations: list[tuple[int, str, int]] = field(default_factory=list)
    completions: list[PhysicalRequestCompletion] = field(default_factory=list)

    def reserve(self, request_url: str, hop_no: int) -> int:
        value = len(self.reservations) + 1
        self.reservations.append((value, request_url, hop_no))
        return value

    def complete(self, completion: PhysicalRequestCompletion) -> None:
        self.completions.append(completion)


def test_every_redirect_hop_is_reserved_before_get_and_completed() -> None:
    first = "https://images.divisare.com/a.jpg"
    second = "https://images.divisare.com/b.jpg"
    session = _Session(
        [
            _Response(302, {"Location": second}),
            _Response(200, {"Content-Type": "image/jpeg"}, _jpeg()),
        ]
    )
    ledger = _Ledger()

    payload = fetch_once_reserved(
        "divisare", first, callbacks=ledger, session=session
    )

    assert payload.request_count == 2
    assert ledger.reservations == [(1, first, 1), (2, second, 2)]
    assert [row.reservation_id for row in ledger.completions] == [1, 2]
    assert [row.outcome for row in ledger.completions] == [
        "response_received",
        "response_received",
    ]
    assert [row.http_status for row in ledger.completions] == [302, 200]


def test_request_exception_still_completes_its_reservation() -> None:
    import requests

    url = "https://images.divisare.com/a.jpg"
    ledger = _Ledger()
    session = _Session([requests.Timeout("late")])

    with pytest.raises(Exception):
        fetch_once_reserved("divisare", url, callbacks=ledger, session=session)

    assert ledger.reservations == [(1, url, 1)]
    assert len(ledger.completions) == 1
    assert ledger.completions[0].outcome == "exception"
    assert ledger.completions[0].error_kind == "Timeout"


class _HardKill(BaseException):
    pass


def test_completion_callback_is_attempted_for_baseexception() -> None:
    url = "https://images.divisare.com/a.jpg"
    ledger = _Ledger()

    with pytest.raises(_HardKill):
        fetch_once_reserved(
            "divisare", url, callbacks=ledger, session=_Session([_HardKill()])
        )

    assert len(ledger.reservations) == 1
    assert len(ledger.completions) == 1
    assert ledger.completions[0].outcome == "exception"
    assert ledger.completions[0].error_kind == "_HardKill"
