from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from conftest import raw_item

from vinted_screener.client import VintedClient, VintedError

HOMEPAGE_HTML = '<!doctype html><html lang="pl-PL"><head><script>{"CSRF_TOKEN":"12345678-aaaa-bbbb-cccc-1234567890ab"}</script>'


class FakeResponse:
    def __init__(self, status_code: int, data: dict | None = None, text: str | None = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._data = data
        self.text = text if text is not None else json.dumps(data or {})
        self.headers = headers or {}

    def json(self) -> dict:
        if self._data is None:
            raise ValueError("not json")
        return self._data


@dataclass
class FakeCookie:
    name: str
    value: str


class FakeCookies:
    def __init__(self) -> None:
        self.jar: list[FakeCookie] = []

    def clear(self) -> None:
        self.jar.clear()


class FakeSession:
    """Strona główna ustawia ciasteczko i X-Anon-Id; API odpowiada kolejnymi `api_responses`."""

    def __init__(self, api_responses: list, homepage: FakeResponse | None = None, token: str | None = "TOKEN1") -> None:
        self.api_responses = list(api_responses)
        self.homepage = homepage or FakeResponse(200, text=HOMEPAGE_HTML, headers={"X-Anon-Id": "anon-1"})
        self.token = token
        self.cookies = FakeCookies()
        self.calls: list[dict] = []

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, **kwargs) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "headers": headers, "jar": list(self.cookies.jar)})
        if url.endswith(".pl/"):
            if self.token and self.homepage.status_code == 200:
                self.cookies.jar.append(FakeCookie("access_token_web", self.token))
            return self.homepage
        response = self.api_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def api_calls(self) -> list[dict]:
        return [c for c in self.calls if "/svc-catalogue/" in c["url"]]


def make_client(responses: list, **session_kw) -> tuple[VintedClient, FakeSession, list[float]]:
    session = FakeSession(responses, **session_kw)
    sleeps: list[float] = []
    client = VintedClient(delay=0, session=session, sleep=sleeps.append)
    return client, session, sleeps


def page(n: int, total_pages: int = 5) -> FakeResponse:
    return FakeResponse(200, {"items": [raw_item(f"item {i}", 100) for i in range(n)], "pagination": {"total_pages": total_pages}})


def test_search_uses_api_host_session_identity_and_params():
    client, session, _ = make_client([page(3)])
    items = client.search("nike dunk", status_ids=(6, 1), price_from=99.9, price_to=250.5, per_page=96)
    assert len(items) == 3
    assert session.calls[0]["url"] == "https://www.vinted.pl/"

    call = session.api_calls()[0]
    assert call["url"] == "https://api.vinted.pl/svc-catalogue/items"
    assert call["params"] == {
        "search_text": "nike dunk",
        "order": "newest_first",
        "per_page": 96,
        "attribute_ids[status]": "6,1",
        "price_from": 99,
        "price_to": 250,
        "page": 1,
    }
    headers = call["headers"]
    assert headers["Cookie"] == "access_token_web=TOKEN1"
    assert headers["X-Anon-Id"] == "anon-1"
    assert headers["X-Csrf-Token"] == "12345678-aaaa-bbbb-cccc-1234567890ab"
    assert headers["Locale"] == "pl-PL"
    assert headers["Origin"] == "https://www.vinted.pl"
    assert headers["Sec-Fetch-Mode"] == "cors" and headers["Sec-Fetch-User"] is None
    assert call["jar"] == [], "token idzie w nagłówku — ciasteczka curl_cffi dodałyby drugi nagłówek Cookie"


def test_domain_without_www_and_locale_from_html():
    session = FakeSession([page(1)], homepage=FakeResponse(200, text='<html class="x" lang="de-AT">', headers={"X-Anon-Id": "a"}))
    client = VintedClient(domain="vinted.pl", delay=0, session=session, sleep=lambda s: None)
    client.search("x")
    assert session.calls[0]["url"] == "https://www.vinted.pl/"
    assert session.api_calls()[0]["headers"]["Locale"] == "de-AT"


def test_search_paginates_until_short_page():
    client, session, _ = make_client([page(2), page(2), page(1)])
    items = client.search("x", pages=5, per_page=2)
    assert len(items) == 5
    assert [c["params"]["page"] for c in session.api_calls()] == [1, 2, 3]


def test_search_respects_total_pages():
    client, _, _ = make_client([page(2, total_pages=1)])
    assert len(client.search("x", pages=3, per_page=2)) == 2


def test_expired_session_is_refreshed_once():
    client, session, _ = make_client([FakeResponse(401, {"code": 100}), page(1)])
    assert len(client.search("x")) == 1
    assert len([c for c in session.calls if c["url"].endswith(".pl/")]) == 2


def test_token_refreshed_by_api_response_is_used_next_time():
    client, session, _ = make_client([page(1), page(1)])
    client.search("x")
    session.cookies.jar.append(FakeCookie("access_token_web", "TOKEN2"))  # Set-Cookie z odpowiedzi API
    client._absorb_cookies()
    client.search("y")
    assert session.api_calls()[-1]["headers"]["Cookie"] == "access_token_web=TOKEN2"


def test_rate_limit_and_network_errors_are_retried_with_backoff():
    client, _, sleeps = make_client([FakeResponse(429, {}), OSError("reset"), page(1)])
    assert len(client.search("x")) == 1
    assert 5 in sleeps and 2 in sleeps


def test_persistent_block_raises_helpful_error():
    client, _, _ = make_client([FakeResponse(403, text="blocked"), FakeResponse(403, text="blocked")])
    with pytest.raises(VintedError, match="403.*blocked.*blokuje"):
        client.search("x")


def test_not_found_suggests_debug():
    client, _, _ = make_client([FakeResponse(404, text="<html>Not found</html>")])
    with pytest.raises(VintedError, match="404.*--debug"):
        client.search("x")


def test_non_json_200_is_reported():
    client, _, _ = make_client([FakeResponse(200, text="<html>captcha</html>")])
    with pytest.raises(VintedError, match="nie JSON.*captcha"):
        client.search("x")


def test_failed_bootstrap_raises_after_retries():
    client, session, sleeps = make_client([], homepage=FakeResponse(503, text=""))
    with pytest.raises(VintedError, match="sesj.*HTTP 503"):
        client.search("x")
    assert len(session.calls) == 3


def test_bootstrap_without_token_raises():
    client, _, _ = make_client([], token=None)
    with pytest.raises(VintedError, match="access_token_web"):
        client.search("x")


def test_debug_log_shows_requests():
    lines: list[str] = []
    session = FakeSession([page(1)])
    VintedClient(delay=0, session=session, sleep=lambda s: None, debug=lines.append).search("x")
    assert any("anon_id=anon-1" in l for l in lines)
    assert any("svc-catalogue/items" in l and "HTTP 200" in l for l in lines)


def test_item_description_from_og_meta():
    page = '<head><meta content="Nowe, cena w sklepie 499 zł &amp; metki" property="og:description"></head>'
    session = FakeSession([FakeResponse(200, text=page)])
    client = VintedClient(delay=0, session=session, sleep=lambda s: None)
    client._token = "TOKEN1"
    assert client.item_description(123) == "Nowe, cena w sklepie 499 zł & metki"
    assert session.calls[-1]["url"] == "https://www.vinted.pl/items/123"
    assert session.calls[-1]["headers"] == {"Cookie": "access_token_web=TOKEN1"}


@pytest.mark.parametrize("response", [FakeResponse(404, text=""), FakeResponse(200, text="<head></head>"), OSError("reset")])
def test_item_description_missing_or_blocked(response):
    client = VintedClient(delay=0, session=FakeSession([response]), sleep=lambda s: None)
    assert client.item_description(1) is None
