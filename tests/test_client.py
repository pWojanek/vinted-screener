from __future__ import annotations

import pytest
from conftest import raw_item

from vinted_screener.client import VintedClient, VintedError


class FakeResponse:
    def __init__(self, status_code: int, data: dict | None = None) -> None:
        self.status_code = status_code
        self._data = data or {}

    def json(self) -> dict:
        return self._data


class FakeCookies:
    def __init__(self) -> None:
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1


class FakeSession:
    def __init__(self, api_responses: list) -> None:
        self.api_responses = list(api_responses)
        self.cookies = FakeCookies()
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url: str, params: dict | None = None, **kwargs) -> FakeResponse:
        self.calls.append((url, params))
        if url.endswith(".pl/"):
            return FakeResponse(200)
        response = self.api_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(responses: list) -> tuple[VintedClient, FakeSession, list[float]]:
    session = FakeSession(responses)
    sleeps: list[float] = []
    client = VintedClient(delay=0, session=session, sleep=sleeps.append)
    return client, session, sleeps


def page(n: int, total_pages: int = 5) -> FakeResponse:
    return FakeResponse(200, {"items": [raw_item(f"item {i}", 100) for i in range(n)], "pagination": {"total_pages": total_pages}})


def test_search_bootstraps_session_and_builds_params():
    client, session, _ = make_client([page(3)])
    items = client.search("nike dunk", status_ids=(6, 1), price_from=99.9, price_to=250.5, per_page=96)
    assert len(items) == 3
    assert session.calls[0][0] == "https://www.vinted.pl/"
    url, params = session.calls[1]
    assert url == "https://www.vinted.pl/api/v2/catalog/items"
    assert params == {
        "search_text": "nike dunk",
        "order": "newest_first",
        "per_page": 96,
        "currency": "PLN",
        "status_ids": "6,1",
        "price_from": 99,
        "price_to": 250,
        "page": 1,
    }


def test_search_paginates_until_short_page():
    client, session, _ = make_client([page(2), page(2), page(1)])
    items = client.search("x", pages=5, per_page=2)
    assert len(items) == 5
    assert [p["page"] for _, p in session.calls[1:]] == [1, 2, 3]


def test_search_respects_total_pages():
    client, session, _ = make_client([page(2, total_pages=1)])
    assert len(client.search("x", pages=3, per_page=2)) == 2


def test_expired_session_is_refreshed_once():
    client, session, _ = make_client([FakeResponse(401), page(1)])
    assert len(client.search("x")) == 1
    homepage_hits = [c for c in session.calls if c[0].endswith(".pl/")]
    assert len(homepage_hits) == 2


def test_rate_limit_and_network_errors_are_retried_with_backoff():
    client, _, sleeps = make_client([FakeResponse(429), OSError("reset"), page(1)])
    assert len(client.search("x")) == 1
    assert 5 in sleeps and 2 in sleeps


def test_persistent_block_raises_helpful_error():
    client, _, _ = make_client([FakeResponse(403), FakeResponse(403)])
    with pytest.raises(VintedError, match="403.*blokuje"):
        client.search("x")


def test_failed_bootstrap_raises():
    session = FakeSession([])
    session.get = lambda url, **kw: FakeResponse(503)
    client = VintedClient(delay=0, session=session, sleep=lambda s: None)
    with pytest.raises(VintedError, match="sesj"):
        client.search("x")
