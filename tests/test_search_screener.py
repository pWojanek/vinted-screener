from __future__ import annotations

from collections import Counter
from pathlib import Path

from conftest import make_item, raw_item

from vinted_screener.models import Listing, RetailSource
from vinted_screener.retail import CostMeter, Identification, RetailAIError, RetailFinder, RetailPrice
from vinted_screener.screener import Filters, describe_funnel
from vinted_screener.search_screener import TYPE_SUGGESTIONS, Search, SearchScreener, find_brands, mentions_brand, parse_searches
from vinted_screener.watchlist import Brand, Settings, load_brands, load_watchlist

SETTINGS = Settings(delay=0)
NIKE = Brand(name="Nike", query="nike", aliases=("Nike",), suspicious_below=0.2)
NIKE_SEARCH = Search("Nike", "nike", (NIKE,))


class FakeVinted:
    def __init__(self, items, descriptions=None):
        self.items = items
        self.descriptions = descriptions or {}
        self.calls = []
        self.described: list[int] = []

    def search(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return self.items

    def item_description(self, item_id):
        self.described.append(item_id)
        return self.descriptions.get(item_id)


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
    deals, funnel = SearchScreener(vinted, SETTINGS, finder).screen_search(NIKE_SEARCH)
    assert len(deals) == 1
    deal = deals[0]
    assert deal.model == "Nike Dunk Low Retro" and deal.category == "Sneakersy"
    assert deal.retail == 499 and deal.retail_source.kind == "web" and deal.retail_source.url
    assert finder.looked_up == [("Nike Dunk Low Retro", "Nike", 450)]
    assert vinted.calls[0]["text"] == "nike" and vinted.calls[0]["pages"] == SETTINGS.search_pages


def test_brand_with_type_and_only_candidates_go_to_web_lookup():
    vinted = FakeVinted([raw_item("Nike dunk", 200, id=1), raw_item("Nike air max", 600, id=2)])
    finder = FakeFinder({1: ident("Nike Dunk Low Retro", 450), 2: ident("Nike Air Max 90", 649)})
    screener = SearchScreener(vinted, SETTINGS, finder)
    deals, funnel = screener.screen_search(Search.parse("Nike buty", [NIKE]))
    assert vinted.calls[0]["text"] == "nike buty"
    assert [p for p, _, _ in finder.looked_up] == ["Nike Dunk Low Retro"], "Air Max za 600 zł nie jest kandydatem"
    assert deals[0].retail_source.kind == "estimate" and "nie znalazło" in deals[0].retail_source.note
    assert funnel["price"] == 1


def test_watchlist_match_skips_identification_and_is_fallback_source():
    dunk = make_item(name="Nike Dunk Low", retail=500)
    vinted = FakeVinted([raw_item("Nike Dunk Low Panda", 200, id=1)])
    finder = FakeFinder()
    deals, _ = SearchScreener(vinted, SETTINGS, finder, watch_items=[dunk]).screen_search(NIKE_SEARCH)
    assert finder.identified == []
    assert finder.looked_up == [("Nike Dunk Low", "Nike", 500)]
    assert deals[0].retail == 500 and deals[0].retail_source.kind == "watchlist"


def test_generic_and_low_confidence_products_without_reference_are_skipped():
    vinted = FakeVinted([raw_item("bluza nike", 50, id=1), raw_item("coś", 50, id=2), raw_item("Nike x", 50, id=3)])
    finder = FakeFinder({1: ident("Nike bluza", 300, confidence="low"), 3: ident(None, None)})
    deals, funnel = SearchScreener(vinted, SETTINGS, finder).screen_search(NIKE_SEARCH)
    assert deals == [] and funnel["noref"] == 2 and funnel["unknown"] == 1
    assert "bez ceny odniesienia 2" in describe_funnel(funnel)


def test_without_ai_only_watchlist_prices_are_used():
    dunk = make_item(name="Nike Dunk Low", retail=500)
    vinted = FakeVinted([raw_item("Nike Dunk Low", 200, id=1), raw_item("Nike Air Max 90", 100, id=2)])
    deals, funnel = SearchScreener(vinted, SETTINGS, None, watch_items=[dunk]).screen_search(NIKE_SEARCH)
    assert [d.model for d in deals] == ["Nike Dunk Low"] and funnel["noref"] == 1


def test_bad_api_key_disables_ai_but_scan_continues():
    dunk = make_item(name="Nike Dunk Low", retail=500)
    vinted = FakeVinted([raw_item("Nike Dunk Low", 200, id=1), raw_item("Nike Air Max 90", 100, id=2)])
    finder = FakeFinder(error=RetailAIError("Claude API odrzuciło klucz"))
    screener = SearchScreener(vinted, SETTINGS, finder, watch_items=[dunk])
    result = screener.screen([NIKE_SEARCH])
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
    deals, funnel = SearchScreener(vinted, SETTINGS, finder, filters=Filters(sizes=("42",))).screen_search(NIKE_SEARCH)
    assert [d.listing.id for d in deals] == [5]
    assert funnel["model"] == 2 and funnel["size"] == 1 and funnel["suspicious"] == 1


def test_market_median_from_same_product_in_this_scan():
    prices = [150, 300, 320, 340]
    vinted = FakeVinted([raw_item("Nike dunk low", p, id=i + 1) for i, p in enumerate(prices)])
    finder = FakeFinder({i + 1: ident("Nike Dunk Low Retro", 499) for i in range(4)}, {"Nike Dunk Low Retro": web(499)})
    deals, _ = SearchScreener(vinted, SETTINGS, finder).screen_search(NIKE_SEARCH)
    cheapest = next(d for d in deals if d.listing.id == 1)
    assert cheapest.market_median is not None and cheapest.market_discount > 0.3


def test_scan_reports_ai_cost():
    finder = FakeFinder({1: ident("Nike Dunk Low Retro", 450)}, {"Nike Dunk Low Retro": web(499)})
    finder.cost.usd, finder.cost.searches = 0.42, 3
    result = SearchScreener(FakeVinted([raw_item("Nike dunk", 200, id=1)]), SETTINGS, finder).screen([NIKE_SEARCH])
    assert result.ai_cost_usd == 0.42 and result.ai_searches == 3 and len(result.deals) == 1


class FakeCeneo:
    """Udaje CeneoPrices: ceny z gotowego słownika (klucz = nazwa produktu)."""

    def __init__(self, prices=None):
        self.prices = prices or {}
        self.lookups = 0
        self.asked: list[tuple[str, list[str]]] = []
        self.disabled_reason = None

    def price_for(self, name, tokens):
        self.lookups += 1
        self.asked.append((name, tokens))
        return self.prices.get(name)


def ceneo_price(price, product="Nike Dunk Low Retro DD1391"):
    return RetailPrice(price, RetailSource(kind="ceneo", label="Ceneo", url="https://www.ceneo.pl/1", product=product,
                                           note="najniższa cena nowego w sklepach"))


def test_free_mode_uses_ceneo_price_with_link():
    vinted = FakeVinted([raw_item("Nike Dunk Low Retro Panda 42", 150, id=1)])
    ceneo = FakeCeneo({"Nike Dunk Low Retro": ceneo_price(479)})
    deals, _ = SearchScreener(vinted, SETTINGS, None, ceneo=ceneo).screen_search(NIKE_SEARCH)
    assert ceneo.asked == [("Nike Dunk Low Retro", ["dunk", "low", "retro"])]
    assert deals[0].retail == 479 and deals[0].retail_source.kind == "ceneo" and deals[0].retail_source.url


def test_ceneo_beats_watchlist_and_verified_ai_beats_ceneo():
    dunk = make_item(name="Nike Dunk Low", retail=500)
    vinted = FakeVinted([raw_item("Nike Dunk Low Panda", 150, id=1)])
    ceneo = FakeCeneo({"Nike Dunk Low": ceneo_price(450)})
    deals, _ = SearchScreener(vinted, SETTINGS, None, watch_items=[dunk], ceneo=ceneo).screen_search(NIKE_SEARCH)
    assert deals[0].retail_source.kind == "ceneo"

    finder = FakeFinder(prices={"Nike Dunk Low": web(499)})
    ceneo = FakeCeneo({"Nike Dunk Low": ceneo_price(450)})
    deals, _ = SearchScreener(vinted, SETTINGS, finder, watch_items=[dunk], ceneo=ceneo).screen_search(NIKE_SEARCH)
    assert deals[0].retail_source.kind == "web" and ceneo.asked == [], "potwierdzona cena z AI — Ceneo niepotrzebne"


def test_ceneo_lookups_are_capped_and_skip_hopeless_products():
    import dataclasses
    settings = dataclasses.replace(SETTINGS, max_ceneo_per_search=1)
    vinted = FakeVinted([raw_item("Nike Air Max 90", 150, id=1), raw_item("Nike Air Max 90", 160, id=2),
                         raw_item("Nike Blazer Mid", 150, id=3), raw_item("Nike Dunk Low", 480, id=4)])
    ceneo = FakeCeneo()
    SearchScreener(vinted, settings, None, ceneo=ceneo).screen_search(NIKE_SEARCH)
    assert [name for name, _ in ceneo.asked] == ["Nike Air Max 90"], "najpierw produkt z największą liczbą ofert"

    dunk = make_item(name="Nike Dunk Low", retail=500)
    ceneo = FakeCeneo()
    SearchScreener(FakeVinted([raw_item("Nike Dunk Low", 480, id=4)]), SETTINGS, None, [dunk], ceneo=ceneo).screen_search(NIKE_SEARCH)
    assert ceneo.asked == [], "480 zł przy retailu 500 zł nie może być okazją — szkoda zapytania"


def test_seller_declared_price_from_description_is_cached(tmp_path):
    from vinted_screener.state import State
    state = State(tmp_path / "s.json")
    vinted = FakeVinted([raw_item("Bluza Nike nowa", 150, id=1), raw_item("Bluza Nike", 140, id=2, status="Bardzo dobry")],
                        descriptions={1: "Nowa z metką, cena w sklepie 449 zł. Wysyłka OLX"})
    screener = SearchScreener(vinted, SETTINGS, None, read_descriptions=True, state=state)
    deals, funnel = screener.screen_search(NIKE_SEARCH)
    assert vinted.described == [1, 2], "najpierw nowe rzeczy"
    deal = deals[0]
    assert deal.listing.id == 1 and deal.retail == 449 and deal.model == "Nike — bluzy i swetry"
    assert deal.retail_source.kind == "seller" and "cena w sklepie 449 zł" in deal.retail_source.note
    assert funnel["noref"] == 1 and screener.lookups["opisy"] == 2

    again = FakeVinted(vinted.items)
    SearchScreener(again, SETTINGS, None, read_descriptions=True, state=state).screen_search(NIKE_SEARCH)
    assert again.described == [], "opisy z pamięci"


def test_description_fetches_are_capped():
    import dataclasses
    settings = dataclasses.replace(SETTINGS, max_descriptions_per_search=2)
    vinted = FakeVinted([raw_item(f"Bluza Nike {i}", 100, id=i) for i in range(1, 6)])
    SearchScreener(vinted, settings, None, read_descriptions=True).screen_search(NIKE_SEARCH)
    assert len(vinted.described) == 2


def test_market_median_is_last_resort_reference():
    prices = [90, 300, 310, 320, 330, 340, 350]
    vinted = FakeVinted([raw_item("Nike Air Max 90 nowe", p, id=i + 1) for i, p in enumerate(prices)])
    deals, funnel = SearchScreener(vinted, SETTINGS, None).screen_search(NIKE_SEARCH)
    assert [d.listing.id for d in deals] == [1]
    source = deals[0].retail_source
    assert source.kind == "market" and source.label == "mediana Vinted (7 ofert)" and "brak ceny retail" in source.note
    assert funnel["price"] == 6

    deals, funnel = SearchScreener(FakeVinted(vinted.items[:5]), SETTINGS, None).screen_search(NIKE_SEARCH)
    assert deals == [] and funnel["noref"] == 5, "z 5 ofert mediana to za mało, żeby była jedynym odniesieniem"


def test_scan_reports_lookups_and_ceneo_block():
    ceneo = FakeCeneo()
    ceneo.disabled_reason = "Ceneo blokuje zapytania (HTTP 403)"
    result = SearchScreener(FakeVinted([raw_item("Nike Air Max 90", 150, id=1)]), SETTINGS, None, ceneo=ceneo).screen([NIKE_SEARCH])
    assert "blokuje" in result.errors["Ceneo"]
    assert result.lookups["ceneo"] == 1


# ─── wyszukiwarka: marka albo typ rzeczy ─────────────────────────────────────

TNF = Brand(name="The North Face", query="the north face", aliases=("The North Face", "North Face", "TNF"), suspicious_below=0.25)
JORDAN = Brand(name="Jordan", query="jordan", aliases=("Jordan", "Air Jordan"), suspicious_below=0.3)
ON = Brand(name="On", query="on running", aliases=("On", "On Running"), suspicious_below=0.2)
RL = Brand(name="Ralph Lauren", query="ralph lauren", aliases=("Ralph Lauren", "Polo Ralph Lauren"), suspicious_below=0.25)
CP = Brand(name="C.P. Company", query="cp company", aliases=("C.P. Company",), suspicious_below=0.35)
KNOWN = [NIKE, TNF, JORDAN, ON, RL, CP]


def test_phrase_with_brand_narrows_to_that_brand_and_keeps_the_rest():
    search = Search.parse("  Nike   bluza ", KNOWN)
    assert search.brands == (NIKE,) and search.query == "nike bluza" and search.scope == "marka Nike"
    assert Search.parse("bluza TNF", KNOWN).query == "bluza the north face"
    assert Search.parse("north face nuptse", KNOWN).query == "the north face nuptse"
    assert Search.parse("C.P. Company", KNOWN).query == "cp company"
    assert Search.parse("cp company kurtka", KNOWN).brands == (CP,)
    assert Search.parse("polo ralph lauren sweter", KNOWN).query == "polo ralph lauren sweter", "dłuższa nazwa zawęża — zostaje"


def test_short_brand_name_only_as_the_whole_phrase():
    assert Search.parse("On", KNOWN).query == "on running" and Search.parse("On", KNOWN).brands == (ON,)
    assert Search.parse("on bluza", KNOWN).brands == (), "„on” w zdaniu to nie marka"
    assert Search.parse("kurtka on running", KNOWN).brands == (ON,)


def test_phrase_can_name_two_brands():
    search = Search.parse("Nike Air Jordan 1", KNOWN)
    assert search.brands == (NIKE, JORDAN) and search.query == "nike Air Jordan 1"
    vinted = FakeVinted([raw_item("Air Jordan 1 Mid", 150, id=1, brand="Jordan"),
                         raw_item("Air Jordan 1 Low", 150, id=2, brand="Nike"),
                         raw_item("Air Jordan 1 a'la", 150, id=3, brand="Inna marka")])
    deals, funnel = SearchScreener(vinted, SETTINGS, None, known_brands=KNOWN).screen_search(search)
    assert funnel["model"] == 1 and funnel["noref"] == 2


def test_type_phrase_takes_known_brands_and_names_products_by_listing_brand():
    [search] = parse_searches(["kurtka puchowa", "Kurtka  puchowa", " "], KNOWN)
    assert search.brands == () and search.query == "kurtka puchowa" and search.scope == "znane marki"
    vinted = FakeVinted([
        raw_item("Kurtka puchowa The North Face Nuptse 700", 450, id=1, brand="The North Face"),
        raw_item("Kurtka puchowa Nike Sportswear", 200, id=2),
        raw_item("Kurtka puchowa", 50, id=3, brand="Reserved"),
    ])
    ceneo = FakeCeneo({"The North Face Nuptse 700": ceneo_price(1299, "The North Face 1996 Retro Nuptse 700")})
    deals, funnel = SearchScreener(vinted, SETTINGS, None, ceneo=ceneo, known_brands=KNOWN).screen_search(search)
    assert vinted.calls[0]["text"] == "kurtka puchowa"
    assert [d.model for d in deals] == ["The North Face Nuptse 700"] and deals[0].retail_source.kind == "ceneo"
    assert ceneo.asked == [("The North Face Nuptse 700", ["nuptse", "700"])], "słowa z frazy („puchowa”) to nie model"
    assert funnel["model"] == 1, "Reserved nie jest na liście znanych marek"


def test_any_brand_mode_and_brand_typed_in_phrase():
    items = [raw_item("Sukienka midi Zara", 30, id=1, brand="Zara"), raw_item("Sukienka", 30, id=2, brand="", legacy=True)]
    for text, only_known, accepted in [("sukienka", True, 0), ("sukienka", False, 1), ("Zara sukienka", True, 1)]:
        funnel = SearchScreener(FakeVinted(items), SETTINGS, None, known_brands=KNOWN).screen_search(
            Search.parse(text, KNOWN, only_known_brands=only_known))[1]
        assert funnel["model"] == 2 - accepted, (text, only_known)
    assert mentions_brand("zara sukienka", "ZARA") and not mentions_brand("sukienka", "Zara")
    assert [b.name for b, _, _ in find_brands("nike i north face", KNOWN)] == ["Nike", "The North Face"]


def test_market_median_groups_do_not_mix_brands():
    items = [raw_item("Bluza Tech Fleece", 100 + i, id=i, brand="Nike") for i in range(1, 4)]
    items += [raw_item("Bluza Tech Fleece", 300 + i, id=i, brand="The North Face") for i in range(4, 7)]
    screener = SearchScreener(FakeVinted(items), SETTINGS, None, known_brands=KNOWN)
    products = screener._products(Search.parse("bluza", KNOWN), [Listing.from_api(raw) for raw in items], Counter())
    assert {p.brand.name: p.group_size for p in products} == {"Nike": 3, "The North Face": 3}


def test_suspicious_floor_follows_listing_brand_in_type_search():
    items = [raw_item("Kurtka TNF Nuptse 700", 300, id=1, brand="The North Face")]
    ceneo = FakeCeneo({"The North Face Nuptse 700": ceneo_price(1299)})
    deals, funnel = SearchScreener(FakeVinted(items), SETTINGS, None, ceneo=ceneo, known_brands=KNOWN).screen_search(
        Search.parse("kurtka", KNOWN))
    assert deals == [] and funnel["suspicious"] == 1, "300 zł to mniej niż 25% z 1299 zł (próg TNF)"


def test_every_bundled_brand_is_recognised_in_a_phrase():
    path = Path(__file__).resolve().parent.parent / "watchlist.toml"
    brands = load_brands(path, load_watchlist(path)[0])
    for brand in brands:
        text = brand.name if len(brand.name) <= 2 else f"{brand.name} bluza"
        assert Search.parse(text, brands).brands == (brand,), brand.name
    assert Search.parse("kurtka puchowa", brands).brands == ()
    for text in TYPE_SUGGESTIONS:
        assert Search.parse(text, brands).brands == (), f"{text}: zwykłe słowa nie mogą udawać marek"


class FakeShops:
    """Udaje ShopPrices: ceny z gotowego słownika (klucz = nazwa produktu)."""

    def __init__(self, prices=None, disabled=None):
        self.prices = prices or {}
        self.lookups = 0
        self.asked: list[tuple[str, str]] = []
        self.disabled = disabled or {}

    def price_for(self, brand, name, tokens):
        self.lookups += 1
        self.asked.append((brand.name, name))
        return self.prices.get(name)


def shop_price(price, label="nike.com"):
    return RetailPrice(price, RetailSource(kind="shop", label=label, url="https://www.nike.com/pl/t/x", product="Nike Dunk Low Retro",
                                           note="cena regularna, teraz w promocji"))


def test_shop_regular_price_goes_before_ceneo_and_ceneo_fills_the_rest():
    vinted = FakeVinted([raw_item("Nike Dunk Low Retro Panda", 150, id=1), raw_item("Nike Air Max 90", 150, id=2)])
    shops = FakeShops({"Nike Dunk Low Retro": shop_price(499)})
    ceneo = FakeCeneo({"Nike Dunk Low Retro": ceneo_price(429), "Nike Air Max 90": ceneo_price(599, "Nike Air Max 90")})
    screener = SearchScreener(vinted, SETTINGS, None, ceneo=ceneo, shops=shops)
    deals, _ = screener.screen_search(NIKE_SEARCH)
    by_id = {d.listing.id: d for d in deals}
    assert by_id[1].retail == 499 and by_id[1].retail_source.kind == "shop"
    assert by_id[2].retail == 599 and by_id[2].retail_source.kind == "ceneo"
    assert shops.asked == [("Nike", "Nike Air Max 90"), ("Nike", "Nike Dunk Low Retro")]
    assert [name for name, _ in ceneo.asked] == ["Nike Air Max 90"], "Dunk ma już cenę ze sklepu"
    assert screener.lookups == {"sklepy": 2, "ceneo": 1}


def test_shop_lookups_are_capped_and_blocked_shops_are_reported():
    import dataclasses
    settings = dataclasses.replace(SETTINGS, max_shop_per_search=1)
    vinted = FakeVinted([raw_item("Nike Air Max 90", 150, id=1), raw_item("Nike Dunk Low", 150, id=2)])
    shops = FakeShops(disabled={"Zalando": "Zalando blokuje zapytania (HTTP 403)"})
    result = SearchScreener(vinted, settings, None, shops=shops).screen([NIKE_SEARCH])
    assert len(shops.asked) == 1
    assert result.errors["Zalando"].startswith("Zalando blokuje") and result.lookups["sklepy"] == 1
