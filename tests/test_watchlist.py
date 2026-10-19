from pathlib import Path

import pytest
from conftest import make_item, raw_item

from vinted_screener.models import Listing, normalize
from vinted_screener.watchlist import WatchlistError, compile_term, load_watchlist, size_matches

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
