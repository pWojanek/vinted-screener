from __future__ import annotations

import itertools
from typing import Any

import pytest

from vinted_screener.watchlist import WatchItem

_ids = itertools.count(1000)


def raw_item(
    title: str,
    price: float,
    *,
    brand: str = "Nike",
    size: str = "42",
    status: str = "Nowy z metką",
    legacy: bool = False,
    **extra: Any,
) -> dict:
    """Ogłoszenie w kształcie zwracanym przez api.vinted.pl/svc-catalogue/items (stan na 10.2026).

    Marka, stan i rozmiar są tylko w item_box; legacy=True daje stary kształt z /api/v2/catalog/items.
    """
    item_id = extra.pop("id", next(_ids))
    fee = round(price * 0.05 + 0.7, 2)
    total = price + fee
    data = {
        "content_source": "search",
        "favourite_count": 3,
        "id": item_id,
        "is_favourite": False,
        "item_box": {
            "accessibility_label": f"{title}, Marka: {brand}, Stan: {status}, Rozmiar: {size}, {price:.2f} zł, {total:.2f} zł",
            "exposures": [],
            "ff_exposures": [],
            "first_line": brand,
            "item_id": item_id,
        },
        "photo": {
            "id": 1,
            "url": f"https://images1.vinted.net/t/{item_id}/f800/x.jpeg",
            "high_resolution": {"id": "x", "timestamp": 1760880000, "orientation": None},
        },
        "photos": [],
        "price": {"amount": f"{price:.2f}", "currency_code": "PLN"},
        "promoted": False,
        "search_tracking_params": {},
        "service_fee": {"amount": f"{fee:.2f}", "currency_code": "PLN"},
        "title": title,
        "total_item_price": {"amount": f"{total:.2f}", "currency_code": "PLN"},
        "url": f"https://www.vinted.pl/items/{item_id}-x",
        "user": {"id": 1, "login": "sprzedawca", "profile_url": "https://www.vinted.pl/member/1"},
        "view_count": 0,
    }
    if legacy:
        del data["item_box"]
        data.update(brand_title=brand, size_title=size, status=status)
    data.update(extra)
    return data


def make_item(**overrides: Any) -> WatchItem:
    fields = dict(
        name="Nike Dunk Low",
        category="Sneakersy",
        query="nike dunk low",
        retail=500.0,
        min_discount=0.5,
        suspicious_below=0.2,
        status_ids=(6, 1, 2),
        brands=("Nike",),
        require=("dunk", "low"),
        exclude=("high",),
    )
    fields.update(overrides)
    return WatchItem(**fields)


@pytest.fixture
def dunk() -> WatchItem:
    return make_item()
