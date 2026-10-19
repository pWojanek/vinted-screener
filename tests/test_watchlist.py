from pathlib import Path

import pytest
from conftest import make_item, raw_item

from vinted_screener.models import Listing, normalize
from vinted_screener.watchlist import Brand, Settings, WatchlistError, compile_term, load_brands, load_shops, load_watchlist, size_matches

ROOT = Path(__file__).resolve().parent.parent


def listing(title: str, brand: str = "Nike", **kw) -> Listing:
    return Listing.from_api(raw_item(title, 200, brand=brand, **kw))


@pytest.mark.parametrize("title", ["Salomon XT-6", "salomon xt6 gore-tex", "XT 6 advanced"])
def test_term_matches_glued_and_hyphenated_words(title):
    assert compile_term("xt 6").search(normalize(title))


def test_term_respects_word_boundaries_and_prefix_wildcard():
    assert not compile_term("low").search("dunk yellow")
    assert compile_term("low").search("dunk low panda")
    assert compile_term("warkocz*").search("sweter w warkocze")
    assert not compile_term("warkocz").search("sweter w warkocze")
    assert compile_term("high|mid").search("jordan 1 mid")


def test_item_matching(dunk):
    assert dunk.matches(listing("Nike Dunk Low Panda"))
    assert dunk.matches(listing("Dunk Low Retro", brand="Nike SB"))
    assert not dunk.matches(listing("Nike Dunk High"))
    assert not dunk.matches(listing("Dunk Low", brand="Inna marka"))
    assert not dunk.matches(listing("Nike Air Force 1 Low"))


@pytest.mark.parametrize(
    "title",
    [
        "Nike Dunk Low replika 1:1",
        "Dunk Low a'la Nike",
        "Samo pudełko Nike Dunk Low",
        "Nike Dunk Low dziecięce",
        "Dunk Low zamienię",
        "Nike Dunk Low uszkodzone",
        "Nike Dunk Low nieoryginalne",
    ],
)
def test_global_excludes(dunk, title):
    assert not dunk.matches(listing(title))


@pytest.mark.parametrize(
    "title",
    ["Nike Dunk Low Baby Blue", "Nike Dunk Low jak nowe", "Dunk Low bez uszkodzeń", "Dunk Low z pudełkiem"],
)
def test_global_excludes_spare_genuine_titles(dunk, title):
    assert dunk.matches(listing(title))


def test_brand_prefix_does_not_overmatch():
    on = make_item(name="On", brands=("On", "On Running"), require=("cloudmonster",), exclude=())
    assert on.matches(listing("Cloudmonster 42", brand="On Running"))
    assert not on.matches(listing("Cloudmonster 42", brand="Only"))
    arc = make_item(name="Arc", brands=("Arc'teryx",), require=("beta lt",), exclude=())
    assert arc.matches(listing("Kurtka Beta LT", brand="Arc’teryx"))
    assert arc.matches(listing("Beta LT gore-tex", brand="ARCTERYX"))


def test_size_matches():
    assert size_matches("42", ("42",))
    assert size_matches("EU 42", ("42",))
    assert size_matches("M / 38 / 10", ("m",))
    assert size_matches("42,5", ("42.5",))
    assert not size_matches("42,5", ("42",))
    assert size_matches("cokolwiek", ())


def test_bundled_watchlist_is_valid():
    settings, items = load_watchlist(ROOT / "watchlist.toml")
    assert settings.currency == "PLN"
    assert len(items) >= 30
    for item in items:
        assert item.suspicious_price < item.max_price < item.retail
        assert item.brands, f"{item.name}: bez filtra marki łatwo o podróbki"


def test_load_watchlist_errors(tmp_path):
    path = tmp_path / "w.toml"
    path.write_text('[[item]]\nname = "A"\nquery = "a"\n')
    with pytest.raises(WatchlistError, match="retail"):
        load_watchlist(path)

    path.write_text('[[item]]\nname = "A"\nquery = "a"\nretail = 100\n[[item]]\nname = "A"\nquery = "b"\nretail = 100\n')
    with pytest.raises(WatchlistError, match="zdublowana"):
        load_watchlist(path)

    path.write_text('[[item]]\nname = "A"\nquery = "a"\nretail = 100\nmin_discount = 50\nsuspicious_below = 60\n')
    with pytest.raises(WatchlistError, match="suspicious_below"):
        load_watchlist(path)

    path.write_text("[settings]\n")
    with pytest.raises(WatchlistError, match="brak pozycji"):
        load_watchlist(path)


@pytest.mark.parametrize(
    "model, title, brand, expected",
    [
        ("Carhartt WIP bluza", "Bluza Carhartt WIP z kapturem hooded chase", "Carhartt WIP", True),
        ("Carhartt WIP bluza", "Carhartt WIP Detroit jacket", "Carhartt WIP", False),
        ("Carhartt WIP bluza", "Bluza Carhartt workwear", "Carhartt", False),
        ("Stüssy bluza", "Stussy hoodie basic logo", "Stüssy", True),
        ("Ralph Lauren bluza", "Bluza z kapturem Polo Ralph Lauren", "Polo Ralph Lauren", True),
        ("Ralph Lauren bluza", "Sweter Ralph Lauren cable knit", "Polo Ralph Lauren", False),
        ("C.P. Company bluza", "CP Company goggle hoodie", "C.P. Company", True),
        ("lululemon Scuba", "Lululemon Scuba oversized half zip", "lululemon", True),
        ("Patagonia Synchilla", "Polar Patagonia Synchilla Snap-T", "Patagonia", True),
        ("Ralph Lauren koszulka polo", "Koszulka polo Ralph Lauren custom slim fit", "Polo Ralph Lauren", True),
        ("Ralph Lauren koszulka polo", "Polówka Ralph Lauren granatowa", "Ralph Lauren", True),
        ("Lacoste koszulka polo", "Lacoste koszulka polo L1212 biała", "Lacoste", True),
        ("Lacoste koszulka polo", "Bluza Lacoste polo", "Lacoste", False),
        ("Carhartt WIP Double Knee", "Spodnie Carhartt WIP Double Knee pant", "Carhartt WIP", True),
        ("Napapijri Rainforest / Skidoo", "Kurtka Napapijri Rainforest winter", "Napapijri", True),
    ],
)
def test_bundled_clothing_matching(model, title, brand, expected):
    _, items = load_watchlist(ROOT / "watchlist.toml")
    item = next(i for i in items if i.name == model)
    assert item.matches(listing(title, brand=brand)) is expected


def test_bundled_brands_load():
    settings, _ = load_watchlist(ROOT / "watchlist.toml")
    brands = {b.name: b for b in load_brands(ROOT / "watchlist.toml", settings)}
    assert len(brands) >= 30
    assert brands["On"].query == "on running"
    assert brands["Moncler"].suspicious_below == 0.4
    assert brands["adidas"].suspicious_below == settings.suspicious_below


@pytest.mark.parametrize(
    "brand_name, listing_brand, title, expected",
    [
        ("Nike", "Nike SB", "Dunk low", True),
        ("Ralph Lauren", "Polo Ralph Lauren", "Sweter", True),
        ("On", "Only", "Kurtka", False),
        ("On", "On Running", "Cloudmonster", True),
        ("The North Face", "North Face", "Kurtka", True),
        ("Carhartt WIP", "Carhartt", "Kurtka", True),
        ("Nike", "Nike", "Bluza nike replika", False),
        ("Nike", "Nike", "Bluza dla dziecka", False),
    ],
)
def test_brand_matching(brand_name, listing_brand, title, expected):
    settings, _ = load_watchlist(ROOT / "watchlist.toml")
    brand = {b.name: b for b in load_brands(ROOT / "watchlist.toml", settings)}[brand_name]
    assert brand.matches(listing(title, brand=listing_brand)) is expected


def test_bundled_shops_load():
    shops = {s.name: s for s in load_shops(ROOT / "watchlist.toml")}
    assert {"Zalando", "nike.com", "adidas.pl"} <= set(shops)
    brands = {b.name: b for b in load_brands(ROOT / "watchlist.toml", Settings())}
    assert shops["nike.com"].sells(brands["Jordan"]) and not shops["nike.com"].sells(brands["adidas"])
    assert shops["Zalando"].sells(brands["Carhartt WIP"]) and not shops["Zalando"].official
    assert shops["adidas.pl"].link_base == "https://www.adidas.pl"


@pytest.mark.parametrize(
    "toml, error",
    [('name = "A"\nsearch_url = "https://a.pl/szukaj"', "{q}"), ('name = "A"\nsearch_url = "http://a.pl/?q={q}"', "https://"),
     ('search_url = "https://a.pl/?q={q}"', "name"),
     ('name = "A"\nsearch_url = "https://a.pl/?q={q}"\ncurrency = "USD"', "currency")],
)
def test_shop_errors(tmp_path, toml, error):
    path = tmp_path / "w.toml"
    path.write_text(f"[[shop]]\n{toml}\n")
    with pytest.raises(WatchlistError, match=error.replace("{", r"\{").replace("}", r"\}")):
        load_shops(path)


def test_old_setting_names_from_brand_mode_still_work(tmp_path):
    path = tmp_path / "w.toml"
    path.write_text('[settings]\nbrand_pages = 5\nmax_ceneo_per_brand = 7\nmax_descriptions_per_search = 4\n'
                    '[[item]]\nname = "A"\nquery = "a"\nretail = 100\n')
    settings, _ = load_watchlist(path)
    assert (settings.search_pages, settings.max_ceneo_per_search, settings.max_descriptions_per_search) == (5, 7, 4)


def test_custom_brand_and_brand_errors(tmp_path):
    custom = Brand.custom("Zara Home", Settings())
    assert custom.query == "zara home" and custom.matches(listing("Poduszka", brand="Zara Home"))
    path = tmp_path / "w.toml"
    path.write_text('[[brand]]\nname = "Nike"\n[[brand]]\nname = "nike"\n')
    with pytest.raises(WatchlistError, match="zdublowana"):
        load_brands(path, Settings())
