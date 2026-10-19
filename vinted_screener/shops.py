"""Ceny regularne ze sklepów internetowych z [[shop]]: Zalando i oficjalne sklepy marek (nike.com, adidas.pl…).

Sklepy nie mają publicznego API, ale strona wyników wyszukiwania niesie dane produktów w osadzonym JSON-ie:
`__NEXT_DATA__` (Next.js, np. Nike), `window.__hydrationDataConsume({...})` (Zalando), `JSON.parse("...")`
albo JSON-LD. Zamiast osobnego parsera na sklep czytamy każdy taki JSON i bierzemy obiekty, które wyglądają
jak produkt: nazwa + cena (+ link). Cena regularna (sprzed promocji) ma pierwszeństwo przed bieżącą.
Gdy sklep zmieni stronę, `--debug` pokazuje, ile produktów udało się odczytać.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urljoin

from vinted_screener.client import Throttle, browser_session
from vinted_screener.models import RetailPrice, RetailSource, normalize
from vinted_screener.products import EUR_TO_PLN, contains_all, is_specific
from vinted_screener.state import State
from vinted_screener.watchlist import Brand, Shop, globally_excluded

FOUND_TTL_HOURS = 7 * 24
NOT_FOUND_TTL_HOURS = 3 * 24
MAX_PRODUCTS = 300  # z jednej strony wyników
MAX_SCRIPT = 8_000_000  # większych skryptów nie przeszukujemy

# klucze, pod którymi sklepy trzymają dane produktu (Nike, Zalando, adidas, Shopify, JSON-LD)
NAME_KEYS = ("name", "title", "displayName", "productName")
NAME_HOLDERS = ("copy",)
SUBTITLE_KEYS = ("subtitle", "subTitle")
REGULAR_KEYS = ("fullPrice", "initialPrice", "originalPrice", "original", "regularPrice", "listPrice",
                "compareAtPrice", "compare_at_price", "compare_at_price_max")
SALE_KEYS = ("salePrice", "promotional", "specialPrice", "finalPrice", "discountedPrice")
CURRENT_KEYS = ("currentPrice", "current", "price", "lowPrice", "price_min")
URL_KEYS = ("url", "uri", "link", "href", "pdpUrl", "productUrl", "canonicalUrl")
_IMAGE_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|svg)(?:\?|$)", re.IGNORECASE)

_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_JSON_START_RE = re.compile(r"[(=]\s*(?=[\[{])")  # foo({...}), x = {...}
_JSON_STRING_RE = re.compile(r"\(\s*(\"(?:[^\"\\]|\\.)*\")\s*\)")  # JSON.parse("{\"a\":1}")
_AMOUNT_RE = re.compile(r"\d[\d\s .,']*")
_TAG_RE = re.compile(r"<[^>]+>")
MAX_FAILED_STARTS = 5000


class ShopError(RuntimeError):
    """Sklep nie odpowiada albo blokuje — nie ma sensu pytać go dalej w tym skanie."""


@dataclass(frozen=True)
class ShopProduct:
    name: str
    brand: str
    subtitle: str
    price: float  # cena regularna, a gdy jej nie ma — bieżąca
    discounted: bool  # sklep sprzedaje teraz taniej niż price
    url: str

    @property
    def text(self) -> str:
        return f"{self.brand} {self.name} {self.subtitle}"


def parse_amount(value: Any) -> float | None:
    """499.99, "1 299,99 zł", "€27.00", {"formatted": "499,00 zł", "amount": 49900} → kwota w jednostkach głównych."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, dict):
        # liczba całkowita w "amount" bywa w groszach (Zalando: 49900 = 499 zł), więc bierzemy ją tylko jako tekst
        # albo ułamek; bez pewności co do jednostki lepiej nie mieć ceny niż mieć stukrotnie zawyżoną
        for key in ("formatted", "display", "value", "amount"):
            if key in value and not (key == "amount" and isinstance(value[key], int)):
                if found := parse_amount(value[key]):
                    return found
        for key in ("centAmount", "amountInCents"):
            if isinstance(value.get(key), int) and value[key] > 0:
                return value[key] / 100
        return None
    if not isinstance(value, str) or not (match := _AMOUNT_RE.search(value)):
        return None
    raw = re.sub(r"[\s ']", "", match.group(0)).rstrip(".,")
    whole, _, cents = re.fullmatch(r"(.*?)(?:([.,])(\d{1,2}))?", raw).groups()
    try:
        amount = float(f"{re.sub(r'[.,]', '', whole)}.{cents or '0'}")
    except ValueError:
        return None
    return amount if amount > 0 else None


def _json_blobs(page: str) -> Iterator[Any]:
    """Każdy JSON osadzony w stronie (albo cała odpowiedź, gdy to API)."""
    stripped = page.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            yield json.loads(stripped)
            return
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    for body in _SCRIPT_RE.findall(page):
        body = body.strip()
        if not body or len(body) > MAX_SCRIPT:
            continue
        if body[0] in "{[":
            try:
                yield json.loads(body)
                continue
            except ValueError:
                pass
        for match in _JSON_STRING_RE.finditer(body):
            try:
                text = json.loads(match.group(1))
                if text.lstrip()[:1] in ("{", "["):
                    yield json.loads(text)
            except ValueError:
                continue
        pos, failed = 0, 0
        while failed < MAX_FAILED_STARTS and (match := _JSON_START_RE.search(body, pos)):
            try:
                value, pos = decoder.raw_decode(body, match.end())
            except ValueError:
                pos, failed = match.end(), failed + 1
                continue
            if value:
                yield value


def _first(holders: list[dict[str, Any]], keys: tuple[str, ...]) -> float | None:
    for holder in holders:
        for key in keys:
            if key in holder and (amount := parse_amount(holder[key])):
                return amount
    return None


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("title")
    return html.unescape(_TAG_RE.sub(" ", value)).strip() if isinstance(value, str) else ""


def _link(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("url") or value.get("path") or value.get("href")
    if not isinstance(value, str) or _IMAGE_RE.search(value):
        return ""
    return value if value.startswith(("http", "/", "{")) or "/" in value else ""


def _absolute(url: str, shop: Shop) -> str:
    if url.startswith("{"):  # Nike: "{countryLang}/t/dunk-low-…"
        return shop.link_base.rstrip("/") + "/" + re.sub(r"^\{\w+\}/?", "", url)
    return urljoin(shop.search_url, url)


def _product(node: dict[str, Any], shop: Shop) -> ShopProduct | None:
    names = [node] + [node[k] for k in NAME_HOLDERS if isinstance(node.get(k), dict)]
    name = next((t for h in names for k in NAME_KEYS if 1 < len(t := _text(h.get(k))) <= 200), "")
    if not name:
        return None
    holders = [node]
    for key, value in node.items():  # "price", "prices", "displayPrice", "priceInfo", "offers"…
        if "price" not in key.lower() and key != "offers":
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            value = value[0]  # JSON-LD: pierwsza oferta
        if isinstance(value, dict):
            holders.append(value)
    regular, sale, current = _first(holders, REGULAR_KEYS), _first(holders, SALE_KEYS), _first(holders, CURRENT_KEYS)
    now = sale or current
    if regular is None and sale and current and current > sale:
        regular = current  # adidas: "price" to cena regularna, a "salePrice" promocyjna
    price = max(p for p in (regular, now) if p) if (regular or now) else None
    if not price:
        return None
    link = next((u for h in names for k in URL_KEYS if (u := _link(h.get(k)))), "")
    return ShopProduct(
        name=name,
        brand=_text(node.get("brand") or node.get("brandName")),
        subtitle=next((t for h in names for k in SUBTITLE_KEYS if (t := _text(h.get(k)))), ""),
        price=price,
        discounted=bool(now and now < price),
        url=_absolute(link, shop) if link else shop.search_url,
    )


def parse_products(page: str, shop: Shop) -> list[ShopProduct]:
    """Produkty z wyników wyszukiwania sklepu, w kolejności ze strony, bez powtórzeń."""
    found: dict[tuple[str, str], ShopProduct] = {}

    def walk(node: Any, depth: int = 0) -> None:
        if len(found) >= MAX_PRODUCTS or depth > 40:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, dict):
            if product := _product(node, shop):
                found.setdefault((normalize(product.text), product.url), product)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)

    for blob in _json_blobs(page):
        walk(blob)
    return list(found.values())


def _page_hint(page: str) -> str:
    title = re.search(r"<title>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    body = page.split("<body", 1)[-1]
    return f"tytuł {title.group(1).strip()[:80]!r}" if title else " ".join(_TAG_RE.sub(" ", body).split())[:200]


class ShopClient:
    def __init__(
        self,
        shop: Shop,
        delay: float = 1.5,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self.shop = shop
        self._session = session or browser_session()
        self._throttle = Throttle(delay, sleep).wait
        self._debug = debug or (lambda msg: None)

    def search(self, query: str) -> list[ShopProduct]:
        url = self.shop.search_url.replace("{q}", quote_plus(query))
        name = self.shop.name
        self._throttle()
        try:
            resp = self._session.get(url, timeout=20)
        except OSError as exc:
            raise ShopError(f"brak połączenia z {name}: {exc}") from exc
        if resp.status_code in (401, 403, 429):
            raise ShopError(f"{name} blokuje zapytania (HTTP {resp.status_code})")
        if resp.status_code != 200:
            self._debug(f"{name} {url} → HTTP {resp.status_code}")
            return []
        products = parse_products(resp.text or "", self.shop)
        if products:
            first = products[0]
            self._debug(f"{name} {query!r}: {len(products)} produktów, pierwszy: {first.brand} {first.name} {first.price:.0f}")
        else:
            self._debug(f"{name} {query!r}: 0 produktów — {_page_hint(resp.text or '')}")
        return products


class ShopPrices:
    """Cena regularna produktu: najpierw oficjalny sklep marki, potem sklepy wielomarkowe; cache w State.

    Bierzemy najniższą cenę regularną spośród produktów, których nazwa zawiera wszystkie słowa modelu
    (różne kolorystyki mają różne ceny, a niższa nie zawyży rabatu). Rzeczy dziecięce i podróbki odpadają.
    Gdy nic nie pasuje, próbujemy bez ostatniego słowa, jeśli to nie numer modelu: sklepy nie używają
    nazw kolorystyk z Vinted ("Dunk Low Panda" → "Dunk Low"), ale "Air Max 90" nie staje się "Air Max".
    """

    def __init__(self, clients: list[ShopClient], state: State, debug: Callable[[str], None] | None = None) -> None:
        self.clients = sorted(clients, key=lambda c: not c.shop.official)
        self.state = state
        self.lookups = 0
        self.disabled: dict[str, str] = {}  # nazwa sklepu → powód
        self._debug = debug or (lambda msg: None)

    def price_for(self, brand: Brand, name: str, tokens: list[str]) -> RetailPrice | None:
        if not tokens:
            return None
        for client in self.clients:
            shop = client.shop
            if not shop.sells(brand):
                continue
            key = f"{normalize(shop.name)}|{normalize(name)}"
            cached = self.state.get_cached("shop", key, FOUND_TTL_HOURS)
            if cached:
                if cached.get("found"):
                    return RetailPrice(cached["price"], RetailSource(**cached["source"]))
                if self.state.get_cached("shop", key, NOT_FOUND_TTL_HOURS):
                    continue
            if shop.name in self.disabled:
                continue
            try:
                products = client.search(name)
            except ShopError as exc:
                self.disabled[shop.name] = str(exc)
                continue
            self.lookups += 1
            price = self._best(shop, brand, products, tokens)
            if price:
                self.state.put_cached("shop", key, {"found": True, "price": price.price, "source": price.source.__dict__})
                return price
            self.state.put_cached("shop", key, {"found": False})
        return None

    @staticmethod
    def _best(shop: Shop, brand: Brand, products: list[ShopProduct], tokens: list[str]) -> RetailPrice | None:
        brand_words = normalize(brand.name).split()

        def fits(p: ShopProduct, words: list[str]) -> bool:
            text = normalize(p.text)
            # link też, bo rozmiary dziecięce bywają tylko w adresie ("…-junior-…")
            if not contains_all(text, words) or globally_excluded(f"{text} {normalize(p.url)}"):
                return False
            # w sklepie wielomarkowym produkt musi być tej marki ("Nike Sportswear" też jest Nike)
            return shop.official or (brand.owns(p.brand) if p.brand else all(w in text.split() for w in brand_words))

        variants = [tokens]
        if len(tokens) >= 3 and not any(ch.isdigit() for ch in tokens[-1]) and is_specific(tokens[:-1]):
            variants.append(tokens[:-1])
        matches = next((m for words in variants if (m := [p for p in products if fits(p, words)])), None)
        if not matches:
            return None
        best = min(matches, key=lambda p: p.price)
        rate = EUR_TO_PLN if shop.currency == "EUR" else 1.0
        notes = ["cena regularna, teraz w promocji" if best.discounted else "cena w sklepie"]
        if rate != 1.0:
            notes.append(f"przeliczono z EUR po {EUR_TO_PLN:.2f} zł".replace(".", ","))
        source = RetailSource(
            kind="shop",
            label=shop.name,
            url=best.url,
            product=best.name if normalize(best.brand) in normalize(best.name) else f"{best.brand} {best.name}",
            note=" · ".join(notes),
            checked_at=datetime.now(timezone.utc).date().isoformat(),
        )
        return RetailPrice(round(best.price * rate, 2), source)
