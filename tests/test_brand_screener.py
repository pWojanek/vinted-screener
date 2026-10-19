from __future__ import annotations

from conftest import make_item, raw_item

from vinted_screener.brand_screener import BrandScreener
from vinted_screener.models import RetailSource
from vinted_screener.retail import CostMeter, Identification, RetailAIError, RetailFinder, RetailPrice
from vinted_screener.screener import Filters, describe_funnel
from vinted_screener.watchlist import Brand, Settings

SETTINGS = Settings(delay=0)
NIKE = Brand(name="Nike", query="nike", aliases=("Nike",), suspicious_below=0.2)


class FakeVinted:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def search(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return self.items


class FakeFinder:
    """Udaje RetailFinder: rozpoznania i ceny z gotowych słowników."""

    def __init__(self, idents=None, prices=None, error=None):
        self.idents = idents or {}
        self.prices = prices or {}
        self.error = error
        self.cost = CostMeter()
        self.over_budget = False
        self.identified: list[int] = []
        self.looked_up: list[tuple] = []

    def identify(self, listings):
        if self.error:
            raise self.error
        self.identified += [l.id for l in listings]
        return {l.id: self.idents[l.id] for l in listings if l.id in self.idents}

    def lookup_many(self, requests):
        self.looked_up += requests
        return {RetailFinder.cache_key(p, b): self.prices.get(p) for p, b, _ in requests}


def web(price, product="Nike Dunk Low Retro"):
    return RetailPrice(price, RetailSource(kind="web", label="nike.com", url="https://www.nike.com/pl/x", product=product))


def ident(product, est, confidence="high", category="Sneakersy"):
    return Identification(product, category, est, confidence)


def test_web_price_with_source_becomes_the_retail():
    vinted = FakeVinted([raw_item("Nike dunk low panda", 200, id=1)])
    finder = FakeFinder({1: ident("Nike Dunk Low Retro", 450)}, {"Nike Dunk Low Retro": web(499)})
    deals, funnel = BrandScreener(vinted, SETTINGS, finder).screen_brand(NIKE)
    assert len(deals) == 1
    deal = deals[0]
    assert deal.model == "Nike Dunk Low Retro" and deal.category == "Sneakersy"
    assert deal.retail == 499 and deal.retail_source.kind == "web" and deal.retail_source.url
    assert finder.looked_up == [("Nike Dunk Low Retro", "Nike", 450)]
    assert vinted.calls[0]["text"] == "nike" and vinted.calls[0]["pages"] == SETTINGS.brand_pages


def test_extra_query_and_only_candidates_go_to_web_lookup():
    vinted = FakeVinted([raw_item("Nike dunk", 200, id=1), raw_item("Nike air max", 600, id=2)])
    finder = FakeFinder({1: ident("Nike Dunk Low Retro", 450), 2: ident("Nike Air Max 90", 649)})
    screener = BrandScreener(vinted, SETTINGS, finder)
    deals, funnel = screener.screen_brand(NIKE, extra_query="buty")
    assert vinted.calls[0]["text"] == "nike buty"
    assert [p for p, _, _ in finder.looked_up] == ["Nike Dunk Low Retro"], "Air Max za 600 zł nie jest kandydatem"
    assert deals[0].retail_source.kind == "estimate" and "nie znalazło" in deals[0].retail_source.note
    assert funnel["price"] == 1


def test_watchlist_match_skips_identification_and_is_fallback_source():
    dunk = make_item(name="Nike Dunk Low", retail=500)
    vinted = FakeVinted([raw_item("Nike Dunk Low Panda", 200, id=1)])
    finder = FakeFinder()
    deals, _ = BrandScreener(vinted, SETTINGS, finder, watch_items=[dunk]).screen_brand(NIKE)
    assert finder.identified == []
    assert finder.looked_up == [("Nike Dunk Low", "Nike", 500)]
    assert deals[0].retail == 500 and deals[0].retail_source.kind == "watchlist"


def test_unknown_and_low_confidence_products_are_skipped():
    vinted = FakeVinted([raw_item("bluza nike", 50, id=1), raw_item("coś", 50, id=2)])
    finder = FakeFinder({1: ident("Nike bluza", 300, confidence="low")})
    deals, funnel = BrandScreener(vinted, SETTINGS, finder).screen_brand(NIKE)
    assert deals == [] and funnel["unknown"] == 2
    assert "nierozpoznany produkt 2" in describe_funnel(funnel)


def test_without_ai_only_watchlist_prices_are_used():
    dunk = make_item(name="Nike Dunk Low", retail=500)
    vinted = FakeVinted([raw_item("Nike Dunk Low", 200, id=1), raw_item("Nike Air Max 90", 100, id=2)])
    deals, funnel = BrandScreener(vinted, SETTINGS, None, watch_items=[dunk]).screen_brand(NIKE)
    assert [d.model for d in deals] == ["Nike Dunk Low"] and funnel["unknown"] == 1


def test_bad_api_key_disables_ai_but_scan_continues():
    dunk = make_item(name="Nike Dunk Low", retail=500)
    vinted = FakeVinted([raw_item("Nike Dunk Low", 200, id=1), raw_item("Nike Air Max 90", 100, id=2)])
    finder = FakeFinder(error=RetailAIError("Claude API odrzuciło klucz"))
    screener = BrandScreener(vinted, SETTINGS, finder, watch_items=[dunk])
    result = screener.screen([NIKE])
    assert [d.model for d in result.deals] == ["Nike Dunk Low"]
    assert "klucz" in result.errors["AI"] and screener.finder is None


def test_brand_filter_global_excludes_sizes_and_suspicious():
    vinted = FakeVinted([
        raw_item("Adidas samba", 100, id=1, brand="adidas"),
        raw_item("Nike dunk replika", 100, id=2),
        raw_item("Nike dunk low", 150, id=3, size="44"),
        raw_item("Nike dunk low", 40, id=4),
        raw_item("Nike dunk low", 150, id=5),
    ])
    finder = FakeFinder({i: ident("Nike Dunk Low Retro", 450) for i in (3, 4, 5)}, {"Nike Dunk Low Retro": web(499)})
    deals, funnel = BrandScreener(vinted, SETTINGS, finder, filters=Filters(sizes=("42",))).screen_brand(NIKE)
    assert [d.listing.id for d in deals] == [5]
    assert funnel["model"] == 2 and funnel["size"] == 1 and funnel["suspicious"] == 1


def test_market_median_from_same_product_in_this_scan():
    prices = [150, 300, 320, 340]
    vinted = FakeVinted([raw_item("Nike dunk low", p, id=i + 1) for i, p in enumerate(prices)])
    finder = FakeFinder({i + 1: ident("Nike Dunk Low Retro", 499) for i in range(4)}, {"Nike Dunk Low Retro": web(499)})
    deals, _ = BrandScreener(vinted, SETTINGS, finder).screen_brand(NIKE)
    cheapest = next(d for d in deals if d.listing.id == 1)
    assert cheapest.market_median is not None and cheapest.market_discount > 0.3


def test_scan_reports_ai_cost():
    finder = FakeFinder({1: ident("Nike Dunk Low Retro", 450)}, {"Nike Dunk Low Retro": web(499)})
    finder.cost.usd, finder.cost.searches = 0.42, 3
    result = BrandScreener(FakeVinted([raw_item("Nike dunk", 200, id=1)]), SETTINGS, finder).screen([NIKE])
    assert result.ai_cost_usd == 0.42 and result.ai_searches == 3 and len(result.deals) == 1
