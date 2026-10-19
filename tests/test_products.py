import pytest

from vinted_screener.products import declared_retail, guess_category, is_specific, product_name, product_tokens


@pytest.mark.parametrize(
    "title, aliases, tokens",
    [
        ("Nike Dunk Low Retro Panda 42 nowe", ("Nike",), ["dunk", "low", "retro", "panda"]),
        ("Buty Nike Air Force 1 '07 białe rozm. 43", ("Nike",), ["air", "force", "1", "07"]),
        ("Salomon XT-6 Advanced 44,5", ("Salomon",), ["xt", "6", "advanced"]),
        ("New Balance 550 białe 40 2/3", ("New Balance",), ["550"]),
        ("Bluza Carhartt WIP Hooded Chase szara M", ("Carhartt WIP",), ["hooded", "chase"]),
        ("Levi's 501 W32 L32", ("Levi's",), ["501"]),
        ("Air Jordan 4 Retro Military Black 44", ("Jordan", "Air Jordan"), ["4", "retro", "military"]),
        ("Bluza Nike czarna L", ("Nike",), []),
        ("Kurtka puchowa The North Face Nuptse 700", ("The North Face", "North Face"), ["nuptse", "700"]),
        ("Kurtka przejściowa z kapturem Carhartt WIP Nimbus", ("Carhartt WIP", "Carhartt"), ["nimbus"]),
    ],
)
def test_product_tokens_drop_brand_sizes_colors_and_filler(title, aliases, tokens):
    assert product_tokens(title, aliases) == tokens


def test_specific_and_name():
    assert is_specific(["dunk", "low"]) and is_specific(["550"]) and is_specific(["2002r"])
    assert not is_specific([]) and not is_specific(["logo"])
    assert product_name("Nike", ["dunk", "low", "retro", "panda"]) == "Nike Dunk Low Retro"


@pytest.mark.parametrize(
    "title, category",
    [("Bluza Carhartt WIP", "Bluzy i swetry"), ("Kurtka TNF Nuptse", "Kurtki"), ("t-shirt Stussy", "Koszulki"),
     ("Glany Dr Martens 1460", "Buty"), ("Nike Dunk Low", "Inne"), ("Plecak Kånken", "Akcesoria")],
)
def test_guess_category(title, category):
    assert guess_category(title) == category


@pytest.mark.parametrize(
    "text, price, expected",
    [
        ("Sprzedam nowe buty, cena w sklepie 499 zł, rozmiar 42", 200, 499),
        ("Kupione za 650,00 zł w Zalando, noszone 2 razy", 300, 650),
        ("Cena regularna: 1 299 zł. Stan idealny", 500, 1299),
        ("cena w sklepie 1.099,99 zł", 300, 1099.99),
        ("Nowe kosztują 120 EUR", 250, 516),
        ("Kurtka kosztuje 150 zł + wysyłka", 150, None),  # to cena oferty, nie sklepu
        ("Cena sklepowa 349 zł, kupiłam za 299 zł", 100, 349),  # wyższa z kwot
        ("bez ceny w opisie", 100, None),
    ],
)
def test_declared_retail(text, price, expected):
    found = declared_retail(text, price)
    if expected is None:
        assert found is None
    else:
        assert found[0] == pytest.approx(expected) and found[1] in text
