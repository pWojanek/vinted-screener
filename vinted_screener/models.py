"""Modele danych i parsowanie ogłoszeń z API Vinted."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Vinted zwraca stan jako zlokalizowany tekst; w zapytaniu filtruje się po ID:
# 6 = nowy z metką, 1 = nowy bez metki, 2 = bardzo dobry, 3 = dobry, 4 = zadowalający.
CONDITION_STATUS_IDS = {
    "nowy z metka": 6,
    "new with tags": 6,
    "nowy bez metki": 1,
    "new without tags": 1,
    "bardzo dobry": 2,
    "very good": 2,
    "dobry": 3,
    "good": 3,
    "zadowalajacy": 4,
    "satisfactory": 4,
}
STATUS_FACTORS = {6: 1.0, 1: 0.9, 2: 0.75, 3: 0.5, 4: 0.3}
DEFAULT_CONDITION_FACTOR = 0.75

_EXTRA_CHARS = str.maketrans({"ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "ß": "ss", "æ": "ae", "’": "", "'": ""})


def normalize(text: str) -> str:
    """Małe litery, bez polskich znaków i interpunkcji, pojedyncze spacje."""
    text = unicodedata.normalize("NFKD", text.translate(_EXTRA_CHARS))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def condition_status_id(condition: str) -> int | None:
    return CONDITION_STATUS_IDS.get(normalize(condition))


def condition_factor(condition: str) -> float:
    return STATUS_FACTORS.get(condition_status_id(condition), DEFAULT_CONDITION_FACTOR)


def _money(value: Any) -> tuple[float | None, str | None]:
    """API zwraca cenę jako {"amount": "120.0", "currency_code": "PLN"} albo (starsze wersje) jako "120.0"."""
    if isinstance(value, dict):
        amount = value.get("amount")
        currency = value.get("currency_code")
    else:
        amount, currency = value, None
    try:
        return (float(amount) if amount is not None else None), currency
    except (TypeError, ValueError):
        return None, currency


# Od 09.2026 wyniki wyszukiwania nie mają brand_title/size_title/status — marka, stan i rozmiar są tylko
# w item_box.accessibility_label: "<tytuł>, Marka: Nike, Stan: Bardzo dobry, Rozmiar: 39, 229.00 zł, 243.35 zł"
# (etykiety w języku z nagłówka Locale), a marka dodatkowo w item_box.first_line.
_BOX_KEYS = {"marka": "brand", "brand": "brand", "stan": "condition", "condition": "condition", "rozmiar": "size", "size": "size"}
# wartość kończy się na ", " — przecinek bez spacji to część wartości (np. rozmiar "42,5")
_BOX_LABEL_RE = re.compile(r"\b(Marka|Stan|Rozmiar|Brand|Condition|Size): (.+?)(?=, |$)")


def _item_box_fields(raw: dict[str, Any]) -> dict[str, str]:
    box = raw.get("item_box") or {}
    label = box.get("accessibility_label") or ""
    title = raw.get("title") or ""
    if title and label.startswith(title):
        label = label[len(title) :]  # tytuł może sam zawierać "Marka: …"
    fields: dict[str, str] = {}
    for key, value in _BOX_LABEL_RE.findall(label):
        fields.setdefault(_BOX_KEYS[key.lower()], value.strip())
    if "brand" not in fields and box.get("first_line"):
        fields["brand"] = box["first_line"]
    return fields


@dataclass(frozen=True)
class Listing:
    id: int
    title: str
    brand: str
    size: str
    condition: str
    price: float
    total_price: float  # cena + opłata za ochronę kupujących (bez wysyłki)
    currency: str
    url: str
    photo_url: str | None
    seller: str
    favourites: int
    uploaded_at: datetime | None

    @classmethod
    def from_api(cls, raw: dict[str, Any], site_url: str = "https://www.vinted.pl") -> Listing | None:
        """Link budowany z ID (`/items/<id>` przekierowuje na ogłoszenie) — pole `url` z API bywa mylące."""
        price, currency = _money(raw.get("price"))
        if price is None or raw.get("id") is None:
            return None
        currency = currency or raw.get("currency") or ""

        total, _ = _money(raw.get("total_item_price"))
        if total is None:
            fee, _ = _money(raw.get("service_fee"))
            total = price + (fee or 0.0)

        photo = raw.get("photo") or {}
        uploaded_at = None
        timestamp = (photo.get("high_resolution") or {}).get("timestamp")
        if timestamp:
            uploaded_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)

        box = _item_box_fields(raw)
        return cls(
            id=int(raw["id"]),
            title=raw.get("title") or "",
            brand=raw.get("brand_title") or box.get("brand", ""),
            size=raw.get("size_title") or box.get("size", ""),
            condition=raw.get("status") or box.get("condition", ""),
            price=price,
            total_price=total,
            currency=currency,
            url=f"{site_url}/items/{int(raw['id'])}",
            photo_url=photo.get("url"),
            seller=(raw.get("user") or {}).get("login") or "",
            favourites=int(raw.get("favourite_count") or 0),
            uploaded_at=uploaded_at,
        )


@dataclass(frozen=True)
class RetailSource:
    """Skąd jest cena retail — pokazywane przy każdej okazji."""

    # "web" (AI znalazło w sieci, link sprawdzony), "shop" (cena regularna ze sklepu z [[shop]]),
    # "ceneo" (najniższa cena w sklepach), "watchlist" (watchlist.toml),
    # "seller" (sprzedający w opisie), "estimate" (szacunek AI bez źródła), "market" (mediana podobnych ofert na Vinted)
    kind: str
    label: str  # np. "nike.com", "watchlista", "szacunek AI"
    url: str | None = None
    product: str | None = None  # dla jakiego produktu jest ta cena
    note: str | None = None
    checked_at: str | None = None  # ISO, kiedy sprawdzono


@dataclass(frozen=True)
class RetailPrice:
    price: float
    source: RetailSource


@dataclass(frozen=True)
class Deal:
    listing: Listing
    model: str  # nazwa produktu, np. "Nike Dunk Low Retro"
    category: str
    retail: float
    retail_source: RetailSource
    retail_discount: float  # 0.6 = 60% taniej niż retail
    market_median: float | None  # mediana cen podobnych ofert na Vinted
    market_discount: float | None  # 0.3 = 30% taniej niż mediana
    score: float
    suspicious: bool  # podejrzanie tanio — możliwa podróbka lub "samo pudełko"
