"""Rdzeń: dla każdej pozycji watchlisty pobiera ogłoszenia i wybiera okazje."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol, TypeAlias, TypeVar
from dataclasses import dataclass, field

from vinted_screener.client import VintedClient, VintedError
from vinted_screener.models import Deal, Listing, RetailSource, condition_factor, condition_status_id
from vinted_screener.state import State
from vinted_screener.watchlist import Settings, WatchItem, size_matches

MIN_MARKET_SAMPLES = 5
MAX_CONSECUTIVE_ERRORS = 3  # Vinted niedostępny albo blokuje — nie ma sensu męczyć kolejnych modeli

# Etapy, na których odpadają ogłoszenia — pokazywane w podsumowaniu, żeby było widać, czemu nie ma okazji.
REJECT_LABELS = {
    "unparsed": "bez ceny",
    "condition": "inny stan",
    "model": "inny model/marka",
    "duplicate": "duplikat",
    "size": "inny rozmiar",
    "price": "za drogie",
    "suspicious": "podejrzanie tanie",
    "market": "za drogie vs Vinted",
    "unknown": "nierozpoznany produkt",
    "noref": "bez ceny odniesienia",
}


def describe_funnel(funnel: Counter[str]) -> str:
    parts = []
    for key, count in funnel.most_common():
        if key.startswith("currency:"):
            parts.append(f"waluta {key.split(':', 1)[1]} {count}")
        elif key in REJECT_LABELS:
            parts.append(f"{REJECT_LABELS[key]} {count}")
    return " · ".join(parts)


def _sample(raw: dict) -> str:
    listing = Listing.from_api(raw)
    if listing is None:
        return f"nieczytelne: {str(raw)[:150]}"
    return (
        f"{listing.title!r} | marka={listing.brand!r} | stan={listing.condition!r}"
        f" | rozmiar={listing.size!r} | cena={listing.total_price:.2f} {listing.currency} | url z API={raw.get('url')!r}"
    )


def deal_score(retail_discount: float, market_discount: float | None, condition: str) -> float:
    """0-100: rabat od retailu (65%) + jak tanio na tle innych ofert na Vinted (35%), ważone stanem.

    Bez danych rynkowych liczy się tylko rabat od retailu.
    """
    if market_discount is None:
        base = retail_discount
    else:
        base = 0.65 * retail_discount + 0.35 * max(-0.5, min(market_discount, 1.0))
    return round(100 * condition_factor(condition) * max(base, 0.0), 1)


@dataclass
class ScanResult:
    deals: list[Deal] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    scanned_items: int = 0
    listings_seen: int = 0
    funnel: Counter[str] = field(default_factory=Counter)
    ai_cost_usd: float = 0.0  # wyszukiwarka
    ai_searches: int = 0
    lookups: Counter[str] = field(default_factory=Counter)  # wyszukiwarka: zapytania do Ceneo, pobrane opisy


class ScanUnit(Protocol):
    name: str


U = TypeVar("U", bound=ScanUnit)
# wołane po każdym modelu/marce: (numer od 1, model albo marka, wynik do tej pory)
ItemCallback: TypeAlias = Callable[[int, Any, ScanResult], None]


def parse_listings(
    raw_items: list[dict],
    settings: Settings,
    status_ids: tuple[int, ...],
    accept: Callable[[Listing], bool],
    funnel: Counter[str],
) -> list[Listing]:
    """Ogłoszenia w dobrej walucie i stanie, które `accept` uznał za pasujące; resztę liczy w lejku."""
    listings = []
    for raw in raw_items:
        listing = Listing.from_api(raw, settings.site_url)
        if listing is None:
            funnel["unparsed"] += 1
        elif listing.currency and listing.currency != settings.currency:
            funnel[f"currency:{listing.currency}"] += 1
        # zabezpieczenie na wypadek, gdyby API zignorowało filtr stanu
        elif (status_id := condition_status_id(listing.condition)) is not None and status_id not in status_ids:
            funnel["condition"] += 1
        elif not accept(listing):
            funnel["model"] += 1
        else:
            listings.append(listing)
    return listings


def run_scan(
    units: list[U],
    scan_one: Callable[[U], tuple[list[Deal], Counter[str]]],
    log: Callable[[str], None],
    on_item: ItemCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ScanResult:
    """Pętla skanu wspólna dla modeli i fraz wyszukiwarki: postęp, zatrzymanie, przerwanie po serii błędów Vinted."""
    result = ScanResult()
    consecutive_errors = 0
    for i, unit in enumerate(units, start=1):
        if should_stop and should_stop():
            result.errors["(przerwano)"] = f"zatrzymano, pominięto {len(units) - i + 1}"
            break
        log(f"[{i}/{len(units)}] {unit.name}…")
        try:
            deals, funnel = scan_one(unit)
        except VintedError as exc:
            result.errors[unit.name] = str(exc)
            log(f"    błąd: {exc}")
            consecutive_errors += 1
            if on_item:
                on_item(i, unit, result)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS and i < len(units):
                result.errors["(przerwano)"] = f"{consecutive_errors} błędy z rzędu, pominięto {len(units) - i}"
                break
            continue
        consecutive_errors = 0
        result.scanned_items += 1
        result.listings_seen += funnel["fetched"]
        result.funnel.update(funnel)
        result.deals.extend(deals)
        if on_item:
            on_item(i, unit, result)
    result.deals.sort(key=lambda d: d.score, reverse=True)
    return result


NO_MIN_DISCOUNT = -math.inf  # bez progu rabatu: wszystko, co ma cenę odniesienia, także droższe od niej


@dataclass(frozen=True)
class Filters:
    min_discount: float | None = None  # nadpisuje próg z watchlisty; NO_MIN_DISCOUNT = bez progu
    min_market_discount: float | None = None
    max_price: float | None = None
    sizes: tuple[str, ...] = ()
    show_suspicious: bool = False


class Screener:
    def __init__(
        self,
        client: VintedClient,
        settings: Settings,
        filters: Filters = Filters(),
        state: State | None = None,
        use_market: bool = True,
        log: Callable[[str], None] = lambda msg: None,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.filters = filters
        self.state = state or State()
        self.use_market = use_market
        self.log = log
        self.debug = debug
        self._shown_keys = False

    def _parse(self, item: WatchItem, raw_items: list[dict], funnel: Counter[str] | None = None) -> list[Listing]:
        return parse_listings(raw_items, self.settings, item.status_ids, item.matches, Counter() if funnel is None else funnel)

    def market_median(self, item: WatchItem) -> float | None:
        """Mediana cen najnowszych pasujących ofert (bez podejrzanie tanich) — "typowa cena na Vinted"."""
        # klucz z zapytaniem i stanami, żeby edycja watchlisty nie zostawiała nieaktualnej mediany
        key = f"{item.name}|{item.query}|{','.join(map(str, item.status_ids))}"
        cached = self.state.cached_market(key, self.settings.market_ttl_hours)
        if cached is not None:
            return cached["median"]
        raw = self.client.search(
            item.query,
            status_ids=item.status_ids,
            per_page=self.settings.per_page,
        )
        prices = [l.total_price for l in self._parse(item, raw) if l.total_price >= item.suspicious_price]
        median = round(statistics.median(prices), 2) if len(prices) >= MIN_MARKET_SAMPLES else None
        self.state.store_market(key, median, len(prices))
        return median

    def screen_item(self, item: WatchItem) -> tuple[list[Deal], Counter[str]]:
        """Okazje dla jednego modelu + lejek: ile pobrano ("fetched") i ile odpadło na którym etapie."""
        min_discount = self.filters.min_discount if self.filters.min_discount is not None else item.min_discount
        price_to = item.retail * (1 - min_discount) if math.isfinite(min_discount) else None
        if self.filters.max_price is not None:
            price_to = self.filters.max_price if price_to is None else min(price_to, self.filters.max_price)
        price_from = None if self.filters.show_suspicious else item.suspicious_price

        raw = self.client.search(
            item.query,
            status_ids=item.status_ids,
            price_from=price_from,
            price_to=price_to,
            pages=self.settings.pages,
            per_page=self.settings.per_page,
        )
        funnel: Counter[str] = Counter(fetched=len(raw))
        listings = self._parse(item, raw, funnel)
        median = self.market_median(item) if self.use_market and listings else None
        sizes = self.filters.sizes or self.settings.sizes

        deals = []
        seen_ids = set()
        for listing in listings:
            if listing.id in seen_ids:
                funnel["duplicate"] += 1
                continue
            seen_ids.add(listing.id)
            if not size_matches(listing.size, sizes):
                funnel["size"] += 1
                continue
            if price_to is not None and listing.total_price > price_to:
                funnel["price"] += 1
                continue
            retail_discount = 1 - listing.total_price / item.retail
            suspicious = listing.total_price < item.suspicious_price
            if suspicious and not self.filters.show_suspicious:
                funnel["suspicious"] += 1
                continue
            market_discount = 1 - listing.total_price / median if median else None
            if self.filters.min_market_discount is not None and (
                market_discount is None or market_discount < self.filters.min_market_discount
            ):
                funnel["market"] += 1
                continue
            deals.append(
                Deal(
                    listing=listing,
                    model=item.name,
                    category=item.category,
                    retail=item.retail,
                    retail_source=RetailSource(kind="watchlist", label="watchlista", product=item.name),
                    retail_discount=retail_discount,
                    market_median=median,
                    market_discount=market_discount,
                    score=deal_score(retail_discount, market_discount, listing.condition),
                    suspicious=suspicious,
                )
            )
        if self.debug:
            self._debug_item(item, raw, funnel, len(deals), price_from, price_to)
        return deals, funnel

    def _debug_item(
        self, item: WatchItem, raw: list[dict], funnel: Counter[str], deals: int, price_from: float | None, price_to: float
    ) -> None:
        assert self.debug is not None
        window = f"{price_from or 0:.0f}–{price_to:.0f}"
        self.debug(f"{item.name}: okno cen {window}, pobrano {len(raw)}, okazji {deals}; odpadło: {describe_funnel(funnel) or '—'}")
        if raw and not self._shown_keys:
            self._shown_keys = True
            self.debug(f"pola ogłoszenia w API: {', '.join(sorted(raw[0]))}")
            if isinstance(raw[0].get("item_box"), dict):
                self.debug(f"pola item_box: {', '.join(sorted(raw[0]['item_box']))}")
        for sample in raw[:3]:
            self.debug(f"  przykład: {_sample(sample)}")

    def screen(
        self,
        items: list[WatchItem],
        on_item: ItemCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ScanResult:
        return run_scan(items, self.screen_item, self.log, on_item, should_stop)
