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


def test_parse_legacy_string_price_falls_back_to_service_fee():
    raw = raw_item("Dunk", 100)
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
