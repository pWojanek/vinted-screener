from __future__ import annotations

import itertools
from typing import Any

import pytest

from vinted_screener.watchlist import WatchItem

_ids = itertools.count(1000)


def raw_item(title: str, price: float, *, brand: str = "Nike", size: str = "42", status: str = "Nowy z metką", **extra: Any) -> dict:
    """Ogłoszenie w kształcie zwracanym przez /api/v2/catalog/items."""
    item_id = extra.pop("id", next(_ids))
    fee = round(price * 0.05 + 0.7, 2)
    data = {
        "id": item_id,
        "title": title,
        "price": {"amount": f"{price:.1f}", "currency_code": "PLN"},
        "is_visible": True,
        "brand_title": brand,
        "path": f"/items/{item_id}-x",
        "user": {"id": 1, "login": "sprzedawca", "profile_url": "https://www.vinted.pl/member/1"},
        "url": f"https://www.vinted.pl/items/{item_id}-x",
        "photo": {
            "id": 1,
            "url": f"https://images1.vinted.net/t/{item_id}/f800/x.jpeg",
            "high_resolution": {"id": "x", "timestamp": 1760880000, "orientation": None},
        },
        "favourite_count": 3,
        "service_fee": {"amount": f"{fee:.2f}", "currency_code": "PLN"},
        "total_item_price": {"amount": f"{price + fee:.2f}", "currency_code": "PLN"},
        "view_count": 0,
        "size_title": size,
        "status": status,
    }
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
