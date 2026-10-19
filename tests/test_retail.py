from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from conftest import raw_item

from vinted_screener import retail
from vinted_screener.models import Listing
from vinted_screener.retail import CostMeter, RetailAIError, RetailFinder, verify_source
from vinted_screener.state import State


def usage(tokens_in=1000, tokens_out=500, searches=0):
    return SimpleNamespace(
        input_tokens=tokens_in, output_tokens=tokens_out, cache_creation_input_tokens=0, cache_read_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=0),
    )


def text_response(payload, stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload), citations=None)],
                           stop_reason=stop_reason, usage=usage())


def search_response(report, urls=("https://www.nike.com/pl/t/dunk-low-retro",), stop_reason="tool_use", searches=2):
    content = [
        SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "nike dunk low cena"}),
        SimpleNamespace(type="web_search_tool_result", content=[
            {"type": "web_search_result", "url": u, "title": "Nike Dunk Low", "page_age": None} for u in urls]),
        SimpleNamespace(type="text", text="Znalazłem cenę.", citations=None),
    ]
    if report is not None:
        content.append(SimpleNamespace(type="tool_use", name="report_retail_price", input=report))
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage(5000, 800, searches))


class FakeAnthropic:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def listings(*titles):
    return [Listing.from_api(raw_item(t, 150, id=i + 1)) for i, t in enumerate(titles)]


REPORT = {"found": True, "price_pln": 499, "source_url": "https://www.nike.com/pl/t/dunk-low-retro?color=x",
          "shop": "nike.com", "matched_product": "Nike Dunk Low Retro", "note": None}


def test_identify_in_batches_with_structured_output_and_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(retail, "IDENTIFY_BATCH", 2)
    client = FakeAnthropic(
        text_response({"items": [{"i": 0, "p": "Nike Dunk Low Retro", "c": "Sneakersy", "r": 499, "q": "high"},
                                 {"i": 1, "p": "Nike bluza", "c": "Bluzy i swetry", "r": 300, "q": "low"}]}),
        text_response({"items": [{"i": 0, "p": None, "c": "Inne", "r": None, "q": "low"}]}),
    )
    state = State(tmp_path / "s.json")
    finder = RetailFinder(client, state)
    found = finder.identify(listings("Nike dunk low panda", "Bluza nike", "coś"))
    assert found[1].product == "Nike Dunk Low Retro" and found[1].est_retail == 499 and found[1].confidence == "high"
    assert found[2].confidence == "low" and found[3].product is None
    assert len(client.calls) == 2
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert json.loads(call["messages"][0]["content"])[0] == {"i": 0, "t": "Nike dunk low panda", "b": "Nike", "s": "42"}
    assert finder.cost.calls == 2 and finder.cost.usd == pytest.approx(2 * (1000 * 4 + 500 * 20) / 1e6)

    state.save()
    again = RetailFinder(FakeAnthropic(), State(tmp_path / "s.json")).identify(listings("Nike dunk low panda"))
    assert again[1].product == "Nike Dunk Low Retro", "drugi skan bierze rozpoznanie z cache, bez wywołania AI"


@pytest.mark.parametrize("response", [
    text_response({"items": []}, stop_reason="refusal"),
    SimpleNamespace(content=[SimpleNamespace(type="text", text="nie json", citations=None)], stop_reason="end_turn", usage=usage()),
])
def test_identify_survives_refusal_and_bad_json(response):
    assert RetailFinder(FakeAnthropic(response), State()).identify(listings("x")) == {}


def test_lookup_returns_verified_web_source_and_caches(tmp_path):
    client = FakeAnthropic(search_response(REPORT))
    state = State(tmp_path / "s.json")
    finder = RetailFinder(client, state)
    price = finder.lookup("Nike Dunk Low Retro", "Nike", 499)
    assert price.price == 499
    assert price.source.kind == "web" and price.source.label == "nike.com"
    assert price.source.url.startswith("https://www.nike.com/pl/t/dunk-low-retro")
    assert price.source.product == "Nike Dunk Low Retro" and price.source.checked_at
    call = client.calls[0]
    assert [t.get("type", t.get("name")) for t in call["tools"]] == ["web_search_20260209", "report_retail_price"]
    assert "Nike Dunk Low Retro" in call["messages"][0]["content"] and "499" in call["messages"][0]["content"]
    assert finder.cost.searches == 2
    assert finder.cost.usd == pytest.approx((5000 * 4 + 800 * 20) / 1e6 + 2 * 0.01)

    state.save()
    cached = RetailFinder(FakeAnthropic(), State(tmp_path / "s.json")).lookup("Nike  dunk low retro", "nike")
    assert cached.price == 499 and cached.source.url == price.source.url


def test_lookup_rejects_source_outside_search_results():
    report = {**REPORT, "source_url": "https://zmyslony-sklep.pl/dunk", "shop": "zmyślony sklep"}
    price = RetailFinder(FakeAnthropic(search_response(report)), State()).lookup("Nike Dunk Low Retro", "Nike")
    assert price.price == 499
    assert price.source.kind == "estimate" and price.source.url is None and "niezweryfikowany" in price.source.label


def test_lookup_resumes_after_pause_turn():
    paused = search_response(None, stop_reason="pause_turn")
    client = FakeAnthropic(paused, search_response(REPORT, urls=()))
    price = RetailFinder(client, State()).lookup("Nike Dunk Low Retro", "Nike")
    assert len(client.calls) == 2
    second = client.calls[1]["messages"]
    assert second[-1] == {"role": "assistant", "content": paused.content}
    assert price.source.kind == "web", "adres z pierwszej (wstrzymanej) odpowiedzi też się liczy"


def test_lookup_not_found_is_cached_shorter():
    state = State()
    report = {**REPORT, "found": False, "price_pln": None, "source_url": None}
    finder = RetailFinder(FakeAnthropic(search_response(report)), state)
    assert finder.lookup("Coś", "Nike") is None
    assert RetailFinder(FakeAnthropic(), state).lookup("Coś", "Nike") is None, "bez ponownego pytania AI"


def test_budget_limit_stops_new_calls():
    finder = RetailFinder(FakeAnthropic(), State(), max_usd=0)
    assert finder.identify(listings("x")) == {}
    assert finder.lookup("Nike Dunk Low Retro", "Nike") is None


def test_bad_api_key_raises_retail_ai_error():
    response = httpx2.Response(401, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    error = anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)
    with pytest.raises(RetailAIError, match="klucz"):
        RetailFinder(FakeAnthropic(error), State()).identify(listings("x"))


def test_lookup_many_dedupes_products():
    client = FakeAnthropic(search_response(REPORT))
    prices = RetailFinder(client, State()).lookup_many([("Nike Dunk Low Retro", "Nike", 499), ("nike dunk low retro", "Nike", 450)])
    assert len(client.calls) == 1 and list(prices) == ["nike|nike dunk low retro"]


def test_verify_source():
    seen = ["https://www.zalando.pl/nike-dunk-low.html", "https://nike.com/pl/t/x"]
    assert verify_source("https://zalando.pl/nike-dunk-low.html/", seen)
    assert verify_source("https://www.nike.com/pl/inna-strona", seen), "ta sama domena co wynik wyszukiwania"
    assert not verify_source("https://inny.pl/x", seen)
    assert not verify_source(None, seen) and not verify_source("nie-url", seen)


def test_cost_meter_handles_missing_usage_fields():
    meter = CostMeter()
    meter.add(SimpleNamespace(input_tokens=1_000_000, output_tokens=0))
    assert meter.usd == pytest.approx(4.0) and meter.searches == 0


def test_unexpected_sdk_error_for_one_product_does_not_stop_others():
    response = httpx2.Response(500, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client = FakeAnthropic(anthropic.InternalServerError("overloaded", response=response, body=None))
    assert RetailFinder(client, State()).lookup("Nike Dunk Low Retro", "Nike") is None
