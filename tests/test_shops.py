from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from vinted_screener.shops import ShopClient, ShopError, ShopPrices, parse_amount, parse_products
from vinted_screener.state import State
from vinted_screener.watchlist import Brand, Shop

NIKE = Brand(name="Nike", query="nike", aliases=("Nike",), suspicious_below=0.25)
ADIDAS = Brand(name="adidas", query="adidas", aliases=("adidas",), suspicious_below=0.2)
ZALANDO = Shop("Zalando", "https://www.zalando.pl/katalog/?q={q}")
NIKE_COM = Shop("nike.com", "https://www.nike.com/pl/w?q={q}", brands=("Nike", "Jordan"), link_base="https://www.nike.com/pl")
ADIDAS_PL = Shop("adidas.pl", "https://www.adidas.pl/search?q={q}", brands=("adidas",))

# kształt strony wyników nike.com (Next.js): produkty w __NEXT_DATA__, link z {countryLang}
NIKE_PAGE = """<html><head><title>Dunk Low. Nike PL</title></head><body><div id="__next"></div>
<script id="__NEXT_DATA__" type="application/json">""" + json.dumps({"props": {"pageProps": {"initialState": {"Wall": {
    "products": [
        {"title": "Nike Dunk Low Retro", "subtitle": "Buty męskie", "url": "{countryLang}/t/buty-dunk-low-retro-abc/DD1391-100",
         "price": {"currency": "PLN", "currentPrice": 399.99, "fullPrice": 499.99, "discounted": True}},
        {"title": "Nike Dunk Low", "subtitle": "Buty dla małych dzieci", "url": "{countryLang}/t/dunk-kids/DH9761",
         "price": {"currency": "PLN", "currentPrice": 249.99, "fullPrice": 249.99}},
        {"title": "Nike Dunk Low Retro Premium", "subtitle": "Buty męskie", "url": "{countryLang}/t/dunk-premium/DV0833",
         "price": {"currency": "PLN", "currentPrice": 549.99, "fullPrice": 549.99}},
    ]}}}}}) + """</script></body></html>"""

# Zalando: dane w wywołaniu window.__hydrationDataConsume(...), ceny jako displayPrice.original/promotional
ZALANDO_PAGE = """<html><body><script>window.__hydrationDataConsume && window.__hydrationDataConsume(""" + json.dumps({
    "graphqlCache": {"{\"id\":\"1\"}": {"data": {"product": {
        "name": "DUNK LOW RETRO - Sneakersy niskie", "brand": {"name": "Nike Sportswear"},
        "uri": "https://www.zalando.pl/nike-sportswear-dunk-low-retro-ni112o0x5-a11.html",
        "displayPrice": {"trackingCurrentAmount": 399, "original": {"formatted": "499,00 zł", "amount": 49900},
                         "promotional": {"formatted": "399,00 zł", "amount": 39900}, "previous": None}}}},
        "{\"id\":\"2\"}": {"data": {"product": {
            "name": "DUNK LOW - Sneakersy niskie", "brand": {"name": "Nike Sportswear"},
            "uri": "https://www.zalando.pl/nike-dunk-low-a12.html", "displayPrice": {"original": {"amount": 44900}}}}},
        "{\"id\":\"3\"}": {"data": {"product": {
            "name": "DUNK LOW RETRO - Sneakersy", "brand": {"name": "Nike Sportswear"}, "uri": "/nike-dunk-junior-a13.html",
            "displayPrice": {"original": {"formatted": "329,00 zł"}}}}}},
}) + """);</script></body></html>"""

# adidas: JSON jako tekst w JSON.parse("..."); "price" to cena regularna, "salePrice" promocyjna
ADIDAS_PAGE = """<script>window.DATA_STORE = JSON.parse(""" + json.dumps(json.dumps({"plp": {"itemList": {"items": [
    {"displayName": "Buty Samba OG", "price": 479, "salePrice": 359, "link": "/buty-samba-og/B75806.html"},
    {"displayName": "Buty Gazelle", "price": 449, "link": "/buty-gazelle/BB5476.html"},
]}}})) + """);</script>"""

LD_PAGE = """<script type="application/ld+json">{"@type": "ItemList", "itemListElement": [{"@type": "ListItem",
  "item": {"@type": "Product", "name": "The North Face Nuptse 1996", "url": "https://shop.example/p/1",
  "image": "https://shop.example/img/1.jpg", "offers": [{"@type": "Offer", "price": "1 299,00", "priceCurrency": "PLN"}]}}]}
</script>"""

SHOPIFY_JSON = json.dumps({"resources": {"results": {"products": [
    {"title": "Basic Stock Hoodie", "url": "/products/basic-stock-hoodie", "price": "150.00", "compare_at_price_max": "0.00"},
]}}})


# Zalando bez danych w JSON-ie: kafelki w HTML z generowanymi klasami; marka i nazwa w <h3>, ceny w <span>,
# przy promocji cena pierwotna, obecna i najniższa z 30 dni
ZALANDO_TILES = """<html><head><title>Nike Dunk - Kup online | Katalog | Zalando</title>
<script>window.feature = {a: 1}; (function(){ var x = 1; })();</script></head><body>
<header><a href="https://www.zalando.pl/mezczyzni-home/">Mężczyźni</a><a href="/faq.html">Pomoc</a></header>
<div class="_5qdMrS _75qWlu iOzucJ">
 <article class="z5x6ht _0xLoFW JT3_zV mo6ZnF _78xIQ-" role="link">
  <a href="https://www.zalando.pl/nike-sportswear-dunk-low-retro-sneakersy-niskie-whiteblack-ni112o0ss-a11.html" class="_LM tCiGa7">
   <img src="https://img01.ztat.net/article/x.jpg?imwidth=300" alt="Nike Sportswear DUNK LOW RETRO"></a>
  <span class="lystZ1">Sponsorowane</span>
  <header><div class="cYylcv">
   <h3 class="FtrEr_ lystZ1 FxZV-M">Nike Sportswear</h3>
   <h3 class="sDq_FX lystZ1 FxZV-M">DUNK LOW RETRO - Sneakersy niskie - white/black</h3></div>
   <section class="_0xLoFW"><p><span class="sDq_FX _4sa1cA dgII7d">Cena pierwotna:</span>
    <span class="sDq_FX _4sa1cA dgII7d Km7l2y">599,00&nbsp;zł</span></p>
    <p><span class="sDq_FX _4sa1cA FxZV-M">399,00&nbsp;zł</span><span class="lystZ1">-33%</span></p>
    <p><span class="lystZ1">Najniższa cena z 30 dni: 449,00 zł</span></p></section></header>
  <a href="https://www.zalando.pl/nike-sportswear-dunk-low-retro-sneakersy-niskie-whiteblack-ni112o0ss-a11.html">Rozmiary: 40 41 42</a>
 </article>
 <article class="z5x6ht _0xLoFW JT3_zV mo6ZnF _78xIQ-" role="link">
  <a href="/nike-sportswear-dunk-low-unisex-sneakersy-niskie-ni114d0ag-a11.html?size=38"><img src="/i.jpg"></a>
  <h3 class="FtrEr_">Nike Sportswear</h3><h3 class="sDq_FX">DUNK LOW UNISEX - Sneakersy niskie</h3>
  <p><span class="sDq_FX">1 049,00 zł</span></p>
 </article>
 <article class="z5x6ht"><a href="https://www.zalando.pl/nike-sportswear-dunk-low-sneakersy-dla-dzieci-a13.html"></a>
  <h3>Nike Sportswear</h3><h3>DUNK LOW - Sneakersy niskie dla dzieci</h3><span>299,00 zł</span></article>
</div><footer><a href="https://www.zalando.pl/regulamin.html">Regulamin</a></footer></body></html>"""


def names_prices(products):
    return [(p.name, p.price, p.discounted, p.url) for p in products]


def test_parse_nike_next_data_uses_full_price_and_country_links():
    assert names_prices(parse_products(NIKE_PAGE, NIKE_COM)) == [
        ("Nike Dunk Low Retro", 499.99, True, "https://www.nike.com/pl/t/buty-dunk-low-retro-abc/DD1391-100"),
        ("Nike Dunk Low", 249.99, False, "https://www.nike.com/pl/t/dunk-kids/DH9761"),
        ("Nike Dunk Low Retro Premium", 549.99, False, "https://www.nike.com/pl/t/dunk-premium/DV0833"),
    ]


def test_parse_zalando_hydration_prefers_formatted_original_price():
    products = parse_products(ZALANDO_PAGE, ZALANDO)
    assert names_prices(products) == [
        ("DUNK LOW RETRO - Sneakersy niskie", 499.0, True, "https://www.zalando.pl/nike-sportswear-dunk-low-retro-ni112o0x5-a11.html"),
        ("DUNK LOW RETRO - Sneakersy", 329.0, False, "https://www.zalando.pl/nike-dunk-junior-a13.html"),
    ], "kwota 44900 bez tekstu może być w groszach — bez pewności nie ma ceny"
    assert products[0].brand == "Nike Sportswear"


def test_parse_zalando_tiles_without_json():
    products = parse_products(ZALANDO_TILES, ZALANDO)
    assert names_prices(products) == [
        ("Nike Sportswear DUNK LOW RETRO - Sneakersy niskie - white/black", 599.0, True,
         "https://www.zalando.pl/nike-sportswear-dunk-low-retro-sneakersy-niskie-whiteblack-ni112o0ss-a11.html"),
        ("Nike Sportswear DUNK LOW UNISEX - Sneakersy niskie", 1049.0, False,
         "https://www.zalando.pl/nike-sportswear-dunk-low-unisex-sneakersy-niskie-ni114d0ag-a11.html?size=38"),
        ("Nike Sportswear DUNK LOW - Sneakersy niskie dla dzieci", 299.0, False,
         "https://www.zalando.pl/nike-sportswear-dunk-low-sneakersy-dla-dzieci-a13.html"),
    ], "przekreślona cena pierwotna to cena regularna; linki bez cen (menu, stopka) odpadają"
    assert "Rozmiary" in products[0].subtitle


def test_zalando_tiles_give_regular_price_without_kids():
    session = FakeSession({"www.zalando.pl": ZALANDO_TILES})
    price = ShopPrices(clients(session, ZALANDO), State()).price_for(NIKE, "Nike Dunk Low", ["dunk", "low"])
    assert price.price == 599.0 and price.source.label == "Zalando"
    assert price.source.product == "Nike Sportswear DUNK LOW RETRO - Sneakersy niskie - white/black"
    assert price.source.note == "cena regularna, teraz w promocji"


def test_debug_shows_page_structure_when_nothing_parsed():
    lines = []
    page = "<title>Zalando</title><script id='x' type='text/plain'>window.__state = 'abc'</script><p>Brak</p>"
    ShopClient(ZALANDO, delay=0, session=FakeSession({"www.zalando.pl": page}), sleep=lambda s: None,
               debug=lines.append).search("nike")
    assert "0 produktów" in lines[0]
    assert "skryptów 1" in lines[1] and "window.__state" in lines[1] and "brak cen w zł" in lines[1]

    lines.clear()
    ShopClient(ZALANDO, delay=0, session=FakeSession({"www.zalando.pl": ZALANDO_TILES}), sleep=lambda s: None,
               debug=lines.append).search("nike")
    assert lines == ["Zalando 'nike': 3 produktów (kafelki HTML), pierwszy: "
                     "Nike Sportswear DUNK LOW RETRO - Sneakersy niskie - white/black 599"]


def test_parse_adidas_json_parse_string_with_sale_price():
    assert names_prices(parse_products(ADIDAS_PAGE, ADIDAS_PL)) == [
        ("Buty Samba OG", 479.0, True, "https://www.adidas.pl/buty-samba-og/B75806.html"),
        ("Buty Gazelle", 449.0, False, "https://www.adidas.pl/buty-gazelle/BB5476.html"),
    ]


def test_parse_json_ld_and_shopify_api():
    shop = Shop("Sklep", "https://shop.example/search?q={q}")
    assert names_prices(parse_products(LD_PAGE, shop)) == [("The North Face Nuptse 1996", 1299.0, False, "https://shop.example/p/1")]
    assert names_prices(parse_products(SHOPIFY_JSON, shop)) == [
        ("Basic Stock Hoodie", 150.0, False, "https://shop.example/products/basic-stock-hoodie")]


def test_parse_garbage():
    assert parse_products("<html><script>var a = {b: 1}; f({c: [1, 2]})</script>Brak</html>", ZALANDO) == []
    assert parse_products("", ZALANDO) == []


@pytest.mark.parametrize(
    "value, amount",
    [(499.99, 499.99), ("1 299,99 zł", 1299.99), ("€27.00", 27.0), ("1.049,00", 1049.0), ("1,299.99", 1299.99),
     ("2 599", 2599.0), ({"formatted": "499,00 zł", "amount": 49900}, 499.0), ({"amount": 49900}, None),
     ({"amount": "499.0", "currencyCode": "PLN"}, 499.0), ({"centAmount": 49900}, 499.0), (0, None), (True, None), ("brak", None)],
)
def test_parse_amount(value, amount):
    assert parse_amount(value) == amount


class FakeSession:
    def __init__(self, pages=None, status=200):
        self.pages, self.status = pages or {}, status
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        host = url.split("/")[2]
        return SimpleNamespace(status_code=self.status, text=self.pages.get(host, "<html></html>"))


def clients(session, *shops):
    return [ShopClient(s, delay=0, session=session, sleep=lambda s: None) for s in shops]


def test_official_shop_first_with_regular_price_and_link(tmp_path):
    session = FakeSession({"www.nike.com": NIKE_PAGE, "www.zalando.pl": ZALANDO_PAGE})
    state = State(tmp_path / "s.json")
    prices = ShopPrices(clients(session, ZALANDO, NIKE_COM), state)
    price = prices.price_for(NIKE, "Nike Dunk Low Retro", ["dunk", "low", "retro"])
    assert price.price == 499.99, "Premium droższe, a dziecięce odpada"
    assert price.source.kind == "shop" and price.source.label == "nike.com"
    assert price.source.url == "https://www.nike.com/pl/t/buty-dunk-low-retro-abc/DD1391-100"
    assert price.source.note == "cena regularna, teraz w promocji" and price.source.checked_at
    assert session.urls == ["https://www.nike.com/pl/w?q=Nike+Dunk+Low+Retro"] and prices.lookups == 1

    state.save()
    again = ShopPrices(clients(FakeSession(status=500), ZALANDO, NIKE_COM), State(tmp_path / "s.json"))
    assert again.price_for(NIKE, "nike dunk low retro", ["dunk", "low", "retro"]).price == 499.99
    assert again.lookups == 0, "z pamięci"


def test_colorway_word_from_vinted_can_be_dropped_but_model_number_cannot():
    session = FakeSession({"www.nike.com": NIKE_PAGE})
    prices = ShopPrices(clients(session, NIKE_COM), State())
    price = prices.price_for(NIKE, "Nike Dunk Low Panda", ["dunk", "low", "panda"])
    assert price.price == 499.99 and price.source.product == "Nike Dunk Low Retro"
    assert prices.price_for(NIKE, "Nike Dunk Low 90", ["dunk", "low", "90"]) is None
    assert prices.price_for(NIKE, "Nike Dunk Panda", ["dunk", "panda"]) is None, "samo „dunk” to za mało"


def test_multibrand_shop_when_official_has_nothing_and_brand_must_match():
    session = FakeSession({"www.zalando.pl": ZALANDO_PAGE})
    prices = ShopPrices(clients(session, NIKE_COM, ZALANDO), State())
    price = prices.price_for(NIKE, "Nike Dunk Low Retro", ["dunk", "low", "retro"])
    assert price.price == 499.0 and price.source.label == "Zalando"
    assert price.source.product == "Nike Sportswear DUNK LOW RETRO - Sneakersy niskie"
    assert prices.lookups == 2, "nike.com nic nie zwróciło, potem Zalando"
    assert ShopPrices(clients(session, ZALANDO), State()).price_for(ADIDAS, "adidas Dunk Low", ["dunk", "low"]) is None, \
        "w Zalando to Nike, a nie adidas"


def test_official_shop_is_only_asked_for_its_brands():
    session = FakeSession({"www.nike.com": NIKE_PAGE})
    assert ShopPrices(clients(session, NIKE_COM), State()).price_for(ADIDAS, "adidas Samba", ["samba"]) is None
    assert session.urls == []


def test_block_disables_only_that_shop():
    session = FakeSession(status=403)
    prices = ShopPrices(clients(session, NIKE_COM, ZALANDO), State())
    assert prices.price_for(NIKE, "Nike Dunk Low", ["dunk", "low"]) is None
    assert set(prices.disabled) == {"nike.com", "Zalando"} and "blokuje" in prices.disabled["Zalando"]
    assert prices.price_for(NIKE, "Nike Air Max", ["air", "max"]) is None and len(session.urls) == 2


def test_eur_shop_is_converted():
    shop = Shop("stussy.com", "https://eu.stussy.com/search/suggest.json?q={q}", brands=("Stüssy",), currency="EUR")
    stussy = Brand(name="Stüssy", query="stussy", aliases=("Stüssy",), suspicious_below=0.3)
    price = ShopPrices(clients(FakeSession({"eu.stussy.com": SHOPIFY_JSON}), shop), State()).price_for(
        stussy, "Stüssy Basic Stock Hoodie", ["basic", "stock"])
    assert price.price == 645.0 and "przeliczono z EUR po 4,30 zł" in price.source.note


def test_client_reports_nothing_parsed():
    lines = []
    ShopClient(ZALANDO, delay=0, session=FakeSession({"www.zalando.pl": "<title>Access Denied</title>"}),
               sleep=lambda s: None, debug=lines.append).search("nike")
    assert "0 produktów" in lines[0] and "Access Denied" in lines[0]
    with pytest.raises(ShopError, match="blokuje"):
        ShopClient(ZALANDO, delay=0, session=FakeSession(status=429), sleep=lambda s: None).search("x")
