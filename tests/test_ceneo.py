from __future__ import annotations

from types import SimpleNamespace

import pytest

from vinted_screener.ceneo import CeneoClient, CeneoError, CeneoPrices, parse_search
from vinted_screener.state import State

ROWS_HTML = """<html><head><title>Nike dunk low - Ceneo.pl</title></head><body>
<div class="cat-prod-row js_category-list-item" data-productid="140123456" data-pid="140123456">
  <strong class="cat-prod-row__name"><a href="/140123456"><span>Nike Dunk Low Retro </span><span>DD1391-100</span></a></strong>
  <span class="price-format"><span class="price"><span class="value">479</span><span class="penny">,99</span></span></span>
</div>
<div class="cat-prod-row" data-productid="140999" data-gaproductname="Nike Dunk High &amp; Retro">
  <span class="price"><span class="value">1 049</span><span class="penny">,00</span></span>
</div></body></html>"""

LD_HTML = """<script type="application/ld+json">{"@type": "ItemList", "itemListElement": [
  {"@type": "ListItem", "item": {"@type": "Product", "name": "Salomon XT-6", "url": "/123",
   "offers": {"@type": "AggregateOffer", "lowPrice": "799.00"}}}]}</script>"""


def test_parse_product_rows():
    products = parse_search(ROWS_HTML)
    assert [(p.name, p.price, p.url) for p in products] == [
        ("Nike Dunk Low Retro DD1391-100", 479.99, "https://www.ceneo.pl/140123456"),
        ("Nike Dunk High & Retro", 1049.0, "https://www.ceneo.pl/140999"),
    ]


def test_parse_json_ld_first():
    assert [(p.name, p.price, p.url) for p in parse_search(LD_HTML + ROWS_HTML)] == [
        ("Salomon XT-6", 799.0, "https://www.ceneo.pl/123")
    ]


def test_parse_garbage():
    assert parse_search("<html><body>Brak wyników</body></html>") == []


class FakeSession:
    def __init__(self, status=200, text=ROWS_HTML):
        self.status, self.text = status, text
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return SimpleNamespace(status_code=self.status, text=self.text)


def client(session):
    return CeneoClient(delay=0, session=session, sleep=lambda s: None, debug=None)


def test_search_url_and_block():
    session = FakeSession()
    assert len(client(session).search("Nike Dunk Low Retro")) == 2
    assert session.urls == ["https://www.ceneo.pl/;szukaj-Nike+Dunk+Low+Retro"]
    with pytest.raises(CeneoError, match="blokuje"):
        client(FakeSession(status=403)).search("x")


def test_debug_hint_when_nothing_parsed():
    lines = []
    CeneoClient(delay=0, session=FakeSession(text="<title>Sprawdzamy, czy jesteś człowiekiem</title>"),
                sleep=lambda s: None, debug=lines.append).search("x")
    assert "0 produktów" in lines[0] and "człowiekiem" in lines[0]


def test_prices_need_all_model_words_and_are_cached(tmp_path):
    session = FakeSession()
    state = State(tmp_path / "s.json")
    prices = CeneoPrices(client(session), state)
    found = prices.price_for("Nike Dunk Low Retro", ["dunk", "low", "retro"])
    assert found.price == 479.99
    assert found.source.kind == "ceneo" and found.source.url == "https://www.ceneo.pl/140123456"
    assert found.source.product == "Nike Dunk Low Retro DD1391-100" and found.source.checked_at
    assert prices.price_for("Nike Dunk Low Panda", ["dunk", "low", "panda"]) is None, "żaden produkt nie ma słowa panda"
    assert prices.lookups == 2

    state.save()
    again = CeneoPrices(client(FakeSession(status=500)), State(tmp_path / "s.json"))
    assert again.price_for("nike dunk low retro", ["dunk", "low", "retro"]).price == 479.99
    assert again.price_for("Nike Dunk Low Panda", ["dunk", "low", "panda"]) is None
    assert again.lookups == 0, "obie odpowiedzi z pamięci"


def test_block_disables_ceneo_for_the_scan():
    session = FakeSession(status=429)
    prices = CeneoPrices(client(session), State())
    assert prices.price_for("Nike Dunk Low", ["dunk", "low"]) is None
    assert "blokuje" in prices.disabled_reason
    assert prices.price_for("Nike Air Max", ["air", "max"]) is None and len(session.urls) == 1
