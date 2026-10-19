"""Ceny nowych produktów z porównywarki Ceneo.pl — darmowe źródło ceny sklepowej, z linkiem do produktu.

Parser czyta dane strukturalne (JSON-LD), a gdy ich nie ma — kafelki listy produktów (data-productid + cena).
Ceneo bywa zmieniane, więc przy zerze wyników `--debug` pokazuje fragment strony.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from vinted_screener.client import Throttle, browser_session
from vinted_screener.models import RetailPrice, RetailSource, normalize
from vinted_screener.state import State

BASE_URL = "https://www.ceneo.pl"
FOUND_TTL_HOURS = 7 * 24  # ceny w sklepach zmieniają się szybciej niż ceny regularne
NOT_FOUND_TTL_HOURS = 3 * 24

_LD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_PRODUCT_START_RE = re.compile(r"<(?:div|article|li)\b[^>]*\bdata-(?:productid|pid)=\"(\d+)\"[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""\bdata-(productname|gaproductname|productminprice|price)="([^"]*)\"""", re.IGNORECASE)
_NAME_RE = re.compile(
    r"class=\"[^\"]*(?:cat-prod-row__name|cat-prod-box__name|product-name)[^\"]*\"[^>]*>(.*?)</(?:strong|a|h2|h3|div)>",
    re.IGNORECASE | re.DOTALL,
)
_PRICE_RE = re.compile(
    r"class=\"value\"[^>]*>\s*([\d\s ]+)\s*</span>\s*(?:<span[^>]*class=\"penny\"[^>]*>\s*,(\d{2}))?", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")


class CeneoError(RuntimeError):
    """Ceneo nie odpowiada albo blokuje — nie ma sensu pytać dalej w tym skanie."""


@dataclass(frozen=True)
class CeneoProduct:
    name: str
    price: float
    url: str


def _to_price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(" ", "").replace(" ", "").replace("zł", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _products_from_ld(node: Any, out: list[CeneoProduct]) -> None:
    if isinstance(node, list):
        for item in node:
            _products_from_ld(item, out)
    elif isinstance(node, dict):
        if node.get("@type") == "Product" and node.get("name"):
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = _to_price(offers.get("lowPrice") or offers.get("price"))
            url = node.get("url") or offers.get("url") or ""
            if price and url:
                out.append(CeneoProduct(html.unescape(node["name"]), price, url if url.startswith("http") else BASE_URL + url))
        for key in ("itemListElement", "item", "@graph", "mainEntity"):
            if key in node:
                _products_from_ld(node[key], out)


def parse_search(page: str) -> list[CeneoProduct]:
    """Produkty z wyników wyszukiwania Ceneo, w kolejności trafności."""
    products: list[CeneoProduct] = []
    for block in _LD_RE.findall(page):
        try:
            _products_from_ld(json.loads(block), products)
        except json.JSONDecodeError:
            continue
    if products:
        return products

    starts = list(_PRODUCT_START_RE.finditer(page))
    seen = set()
    for n, match in enumerate(starts):
        product_id = match.group(1)
        if product_id in seen:
            continue
        seen.add(product_id)
        end = starts[n + 1].start() if n + 1 < len(starts) else match.start() + 8000
        block = page[match.start() : end]
        attrs = {k.lower(): html.unescape(v) for k, v in _ATTR_RE.findall(match.group(0))}
        name = attrs.get("productname") or attrs.get("gaproductname")
        if not name and (found := _NAME_RE.search(block)):
            name = html.unescape(" ".join(_TAG_RE.sub(" ", found.group(1)).split()))
        price = _to_price(attrs.get("productminprice") or attrs.get("price"))
        if price is None and (found := _PRICE_RE.search(block)):
            price = _to_price(f"{found.group(1)}.{found.group(2) or '00'}")
        if name and price:
            products.append(CeneoProduct(name, price, f"{BASE_URL}/{product_id}"))
    return products


def _page_hint(page: str) -> str:
    title = re.search(r"<title>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    body = page.split("<body", 1)[-1]
    return f"tytuł {title.group(1).strip()[:80]!r}" if title else " ".join(_TAG_RE.sub(" ", body).split())[:200]


class CeneoClient:
    def __init__(
        self,
        delay: float = 1.5,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session or browser_session()
        self._throttle = Throttle(delay, sleep).wait
        self._debug = debug or (lambda msg: None)

    def search(self, query: str) -> list[CeneoProduct]:
        url = f"{BASE_URL}/;szukaj-{quote_plus(query)}"
        self._throttle()
        try:
            resp = self._session.get(url, timeout=20)
        except OSError as exc:
            raise CeneoError(f"brak połączenia z Ceneo: {exc}") from exc
        if resp.status_code in (403, 429):
            raise CeneoError(f"Ceneo blokuje zapytania (HTTP {resp.status_code})")
        if resp.status_code != 200:
            self._debug(f"Ceneo {url} → HTTP {resp.status_code}")
            return []
        products = parse_search(resp.text or "")
        if products:
            self._debug(f"Ceneo {query!r}: {len(products)} produktów, pierwszy: {products[0].name} {products[0].price:.0f} zł")
        else:
            self._debug(f"Ceneo {query!r}: 0 produktów — {_page_hint(resp.text or '')}")
        return products


def _contains_all(name: str, tokens: list[str]) -> bool:
    text = normalize(name)
    return all(re.search(rf"\b{re.escape(t)}\b", text) for t in tokens)


class CeneoPrices:
    """Cena sklepowa produktu z Ceneo, z cache w State; tylko produkt, którego nazwa zawiera wszystkie słowa modelu."""

    def __init__(self, client: CeneoClient, state: State, debug: Callable[[str], None] | None = None) -> None:
        self.client = client
        self.state = state
        self.lookups = 0
        self.disabled_reason: str | None = None
        self._debug = debug or (lambda msg: None)

    def price_for(self, name: str, tokens: list[str]) -> RetailPrice | None:
        key = normalize(name)
        cached = self.state.get_cached("ceneo", key, FOUND_TTL_HOURS)
        if cached:
            if cached.get("found"):
                return RetailPrice(cached["price"], RetailSource(**cached["source"]))
            if self.state.get_cached("ceneo", key, NOT_FOUND_TTL_HOURS):
                return None
        if self.disabled_reason or not tokens:
            return None
        try:
            products = self.client.search(name)
        except CeneoError as exc:
            self.disabled_reason = str(exc)
            return None
        self.lookups += 1
        match = next((p for p in products if p.price > 0 and _contains_all(p.name, tokens)), None)
        if not match:
            self.state.put_cached("ceneo", key, {"found": False})
            return None
        source = RetailSource(
            kind="ceneo",
            label="Ceneo",
            url=match.url,
            product=match.name,
            note="najniższa cena nowego w sklepach",
            checked_at=datetime.now(timezone.utc).date().isoformat(),
        )
        self.state.put_cached("ceneo", key, {"found": True, "price": match.price, "source": source.__dict__})
        return RetailPrice(match.price, source)
