from datetime import datetime, timezone

from conftest import raw_item

from vinted_screener.models import Listing, condition_factor, normalize


def test_normalize_strips_polish_chars_and_punctuation():
    assert normalize("Kurtka Arc'teryx — ŁÓDŹ, XT-6!") == "kurtka arcteryx lodz xt 6"
    assert normalize("Fjällräven Kånken") == "fjallraven kanken"


def test_condition_factor_handles_polish_and_english():
    assert condition_factor("Nowy z metką") == 1.0
    assert condition_factor("Very good") == 0.75
    assert condition_factor("cokolwiek") == 0.75


def test_parse_current_api_shape():
    listing = Listing.from_api(raw_item("Nike Dunk Low Panda", 200, id=42))
    assert listing.id == 42
    assert listing.price == 200.0
    assert listing.total_price == 210.7
    assert listing.currency == "PLN"
    assert listing.photo_url.endswith("x.jpeg")
    assert listing.uploaded_at == datetime.fromtimestamp(1760880000, tz=timezone.utc)
    assert listing.seller == "sprzedawca"


def test_parse_brand_condition_size_from_item_box():
    # prawdziwe ogłoszenie z api.vinted.pl/svc-catalogue/items (10.2026), skrócone
    raw = {
        "id": 10446365514,
        "title": "Nike dunk low retro biale czarne sneakersy 39",
        "item_box": {
            "accessibility_label": "Nike dunk low retro biale czarne sneakersy 39, Marka: Nike, Stan: Bardzo dobry,"
            " Rozmiar: 39, 229.00 zł, 243.35 zł",
            "first_line": "Nike",
            "item_id": 10446365514,
        },
        "price": {"amount": "229.00", "currency_code": "PLN"},
        "total_item_price": {"amount": "243.35", "currency_code": "PLN"},
    }
    listing = Listing.from_api(raw)
    assert (listing.brand, listing.condition, listing.size) == ("Nike", "Bardzo dobry", "39")
    assert listing.total_price == 243.35


def test_item_box_edge_cases():
    raw = raw_item("Sweter, Marka: Zara", 100, brand="Polo Ralph Lauren", size="42,5", status="Nowy z metką")
    listing = Listing.from_api(raw)
    assert (listing.brand, listing.size, listing.condition) == ("Polo Ralph Lauren", "42,5", "Nowy z metką")

    raw = raw_item("Plecak", 100)
    raw["item_box"]["accessibility_label"] = "Plecak, Marka: Fjällräven, Stan: Dobry, 100.00 zł, 105.70 zł"
    assert Listing.from_api(raw).size == ""

    raw["item_box"] = {"first_line": "Nike"}
    assert Listing.from_api(raw).brand == "Nike"


def test_parse_legacy_shape():
    listing = Listing.from_api(raw_item("Dunk", 100, brand="Nike", size="43", status="Dobry", legacy=True))
    assert (listing.brand, listing.size, listing.condition) == ("Nike", "43", "Dobry")


def test_parse_legacy_string_price_falls_back_to_service_fee():
    raw = raw_item("Dunk", 100, legacy=True)
    raw["price"] = "100.0"
    raw["currency"] = "PLN"
    del raw["total_item_price"]
    listing = Listing.from_api(raw)
    assert listing.price == 100.0
    assert listing.total_price == 105.7
    assert listing.currency == "PLN"


def test_parse_rejects_items_without_price():
    raw = raw_item("Dunk", 100)
    raw["price"] = None
    assert Listing.from_api(raw) is None


def test_parse_tolerates_missing_photo_and_user():
    raw = raw_item("Dunk", 100, photo=None, user=None)
    listing = Listing.from_api(raw)
    assert listing.photo_url is None and listing.uploaded_at is None and listing.seller == ""


def test_url_is_built_from_id_not_taken_from_api():
    raw = raw_item("Dunk", 100, id=10446365514, url="https://www.vinted.pl/catalog?search_text=dunk")
    assert Listing.from_api(raw).url == "https://www.vinted.pl/items/10446365514"
    assert Listing.from_api(raw, "https://www.vinted.de").url == "https://www.vinted.de/items/10446365514"
