from __future__ import annotations

from conftest import make_item, raw_item

from vinted_screener.client import VintedError
from vinted_screener.screener import Filters, Screener, deal_score, describe_funnel
from vinted_screener.state import State
from vinted_screener.watchlist import Settings


class FakeClient:
    """Zwraca `candidates` dla zapytań z price_to i `market` dla zapytań o medianę."""

    def __init__(self, candidates: list[dict], market: list[dict] | None = None, fail: bool = False) -> None:
        self.candidates = candidates
        self.market = market or []
        self.fail = fail
        self.calls: list[dict] = []

    def search(self, text: str, **kwargs) -> list[dict]:
        self.calls.append({"text": text, **kwargs})
        if self.fail:
            raise VintedError("HTTP 403")
        return self.candidates if kwargs.get("price_to") is not None else self.market


MARKET = [raw_item(f"Nike Dunk Low {i}", p) for i, p in enumerate([300, 320, 340, 360, 380, 50])]


def screener(client, **kw) -> Screener:
    return Screener(client, Settings(), **kw)


def test_finds_deals_below_threshold(dunk):
    client = FakeClient(
        [
            raw_item("Nike Dunk Low Panda", 150, id=1),
            raw_item("Nike Dunk Low Grey", 260, id=2),  # 260 + opłata > 250 → nie okazja
            raw_item("Nike Dunk High", 120, id=3),  # inny model
        ],
        MARKET,
    )
    deals, funnel = screener(client).screen_item(dunk)
    assert funnel == {"fetched": 3, "price": 1, "model": 1}
    assert [d.listing.id for d in deals] == [1]
    deal = deals[0]
    assert round(deal.retail_discount, 3) == round(1 - 158.2 / 500, 3)
    # mediana z 5 ofert powyżej progu podejrzanych (50 zł odrzucone)
    assert deal.market_median == round((340 * 1.05 + 0.7), 2)
    assert deal.market_discount > 0
    assert not deal.suspicious


def test_query_uses_price_window(dunk):
    client = FakeClient([])
    screener(client).screen_item(dunk)
    call = client.calls[0]
    assert call["price_from"] == 100 and call["price_to"] == 250
    assert call["status_ids"] == (6, 1, 2)
    assert len(client.calls) == 1, "bez kandydatów nie pytamy o medianę"


def test_suspicious_listings_hidden_unless_requested(dunk):
    candidates = [raw_item("Nike Dunk Low", 60, id=7)]
    assert screener(FakeClient(candidates)).screen_item(dunk)[0] == []

    client = FakeClient(candidates)
    deals, _ = screener(client, filters=Filters(show_suspicious=True)).screen_item(dunk)
    assert client.calls[0]["price_from"] is None
    assert len(deals) == 1 and deals[0].suspicious


def test_filters(dunk):
    candidates = [raw_item("Nike Dunk Low", 150, id=1, size="42"), raw_item("Nike Dunk Low", 200, id=2, size="44")]
    deals, _ = screener(FakeClient(candidates), filters=Filters(sizes=("44",)), use_market=False).screen_item(dunk)
    assert [d.listing.id for d in deals] == [2]

    client = FakeClient(candidates)
    screener(client, filters=Filters(max_price=180, min_discount=0.6), use_market=False).screen_item(dunk)
    assert client.calls[0]["price_to"] == 180

    deals, _ = screener(FakeClient(candidates, MARKET), filters=Filters(min_market_discount=0.5)).screen_item(dunk)
    assert [d.listing.id for d in deals] == [1]


def test_foreign_currency_and_duplicates_skipped(dunk):
    eur = raw_item("Nike Dunk Low", 50, id=1)
    eur["price"]["currency_code"] = "EUR"
    dup = raw_item("Nike Dunk Low", 150, id=2)
    deals, _ = screener(FakeClient([eur, dup, dup]), use_market=False).screen_item(dunk)
    assert [d.listing.id for d in deals] == [2]


def test_market_median_is_cached_in_state(dunk, tmp_path):
    state = State(tmp_path / "state.json")
    candidates = [raw_item("Nike Dunk Low", 150, id=1)]
    first = FakeClient(candidates, MARKET)
    screener(first, state=state).screen_item(dunk)
    assert len(first.calls) == 2
    state.save()

    second = FakeClient(candidates, [])
    deals, _ = screener(second, state=State(tmp_path / "state.json")).screen_item(dunk)
    assert len(second.calls) == 1
    assert deals[0].market_median is not None


def test_too_few_market_samples_gives_no_median(dunk):
    deals, _ = screener(FakeClient([raw_item("Nike Dunk Low", 150)], MARKET[:2])).screen_item(dunk)
    assert deals[0].market_median is None and deals[0].market_discount is None


def test_screen_collects_errors_and_sorts_by_score():
    good = make_item(name="A")
    other = make_item(name="B", retail=1000)
    client = FakeClient([raw_item("Nike Dunk Low", 200, id=1)])
    result = screener(client, use_market=False).screen([good, other])
    assert [d.item.name for d in result.deals] == ["B", "A"]
    assert result.scanned_items == 2

    result = screener(FakeClient([], fail=True)).screen([good])
    assert result.errors == {"A": "HTTP 403"} and result.scanned_items == 0


def test_screen_aborts_after_consecutive_errors():
    items = [make_item(name=str(i)) for i in range(6)]
    client = FakeClient([], fail=True)
    result = screener(client).screen(items)
    assert len(client.calls) == 3
    assert "(przerwano)" in result.errors and "pominięto 3" in result.errors["(przerwano)"]


def test_deal_score():
    assert deal_score(0.6, None, "Nowy z metką") == 60.0
    assert deal_score(0.6, None, "Bardzo dobry") == 45.0
    assert deal_score(0.6, 0.4, "Nowy z metką") > deal_score(0.6, 0.0, "Nowy z metką") > deal_score(0.6, -0.3, "Nowy z metką")
    assert deal_score(0.5, -5.0, "Nowy z metką") == round(100 * (0.65 * 0.5 - 0.35 * 0.5), 1)


def test_condition_outside_status_ids_is_skipped_even_if_api_ignores_filter(dunk):
    candidates = [
        raw_item("Nike Dunk Low", 150, id=1, status="Dobry"),
        raw_item("Nike Dunk Low", 150, id=2, status="Bardzo dobry"),
        raw_item("Nike Dunk Low", 150, id=3, status="Jakiś nowy stan"),
    ]
    deals, _ = screener(FakeClient(candidates), use_market=False).screen_item(dunk)
    assert sorted(d.listing.id for d in deals) == [2, 3]


def test_funnel_explains_why_nothing_matched(dunk):
    eur = raw_item("Nike Dunk Low", 50, id=1)
    eur["price"]["currency_code"] = "EUR"
    candidates = [eur, raw_item("Nike Dunk Low", 60, id=2), raw_item("Nike Dunk Low", 150, id=3, size="44")]
    lines: list[str] = []
    result = screener(
        FakeClient(candidates), filters=Filters(sizes=("42",)), use_market=False, debug=lines.append
    ).screen([dunk])
    assert result.deals == []
    assert describe_funnel(result.funnel) == "waluta EUR 1 · podejrzanie tanie 1 · inny rozmiar 1"
    assert any("okno cen 100–250" in l and "pobrano 3" in l for l in lines)
    assert any(l.startswith("pola ogłoszenia w API:") and "item_box" in l for l in lines)
    assert any(l.startswith("pola item_box:") and "accessibility_label" in l for l in lines)
    assert any("przykład: 'Nike Dunk Low' | marka='Nike' | stan='Nowy z metką' | rozmiar='42'" in l for l in lines)
