"""Rdzeń: dla każdej pozycji watchlisty pobiera ogłoszenia i wybiera okazje."""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field

from vinted_screener.client import VintedClient, VintedError
from vinted_screener.models import Deal, Listing, condition_factor
from vinted_screener.state import State
from vinted_screener.watchlist import Settings, WatchItem, size_matches

MIN_MARKET_SAMPLES = 5
MAX_CONSECUTIVE_ERRORS = 3  # Vinted niedostępny albo blokuje — nie ma sensu męczyć kolejnych modeli


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


@dataclass(frozen=True)
class Filters:
    min_discount: float | None = None  # nadpisuje próg z watchlisty
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
    ) -> None:
        self.client = client
        self.settings = settings
        self.filters = filters
        self.state = state or State()
        self.use_market = use_market
        self.log = log

    def _parse(self, item: WatchItem, raw_items: list[dict]) -> list[Listing]:
        listings = []
        for raw in raw_items:
            listing = Listing.from_api(raw)
            if listing is None or (listing.currency and listing.currency != self.settings.currency):
                continue
            if item.matches(listing):
                listings.append(listing)
        return listings

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
            currency=self.settings.currency,
            per_page=self.settings.per_page,
        )
        prices = [l.total_price for l in self._parse(item, raw) if l.total_price >= item.suspicious_price]
        median = round(statistics.median(prices), 2) if len(prices) >= MIN_MARKET_SAMPLES else None
        self.state.store_market(key, median, len(prices))
        return median

    def screen_item(self, item: WatchItem) -> tuple[list[Deal], int]:
        min_discount = self.filters.min_discount if self.filters.min_discount is not None else item.min_discount
        price_to = item.retail * (1 - min_discount)
        if self.filters.max_price is not None:
            price_to = min(price_to, self.filters.max_price)
        price_from = None if self.filters.show_suspicious else item.suspicious_price

        raw = self.client.search(
            item.query,
            status_ids=item.status_ids,
            price_from=price_from,
            price_to=price_to,
            currency=self.settings.currency,
            pages=self.settings.pages,
            per_page=self.settings.per_page,
        )
        listings = self._parse(item, raw)
        median = self.market_median(item) if self.use_market and listings else None
        sizes = self.filters.sizes or self.settings.sizes

        deals = []
        seen_ids = set()
        for listing in listings:
            if listing.id in seen_ids or not size_matches(listing.size, sizes):
                continue
            seen_ids.add(listing.id)
            if listing.total_price > price_to:
                continue
            retail_discount = 1 - listing.total_price / item.retail
            suspicious = listing.total_price < item.suspicious_price
            if suspicious and not self.filters.show_suspicious:
                continue
            market_discount = 1 - listing.total_price / median if median else None
            if self.filters.min_market_discount is not None and (
                market_discount is None or market_discount < self.filters.min_market_discount
            ):
                continue
            deals.append(
                Deal(
                    item=item,
                    listing=listing,
                    retail_discount=retail_discount,
                    market_median=median,
                    market_discount=market_discount,
                    score=deal_score(retail_discount, market_discount, listing.condition),
                    suspicious=suspicious,
                )
            )
        return deals, len(raw)

    def screen(self, items: list[WatchItem]) -> ScanResult:
        result = ScanResult()
        consecutive_errors = 0
        for i, item in enumerate(items, start=1):
            self.log(f"[{i}/{len(items)}] {item.name}…")
            try:
                deals, fetched = self.screen_item(item)
            except VintedError as exc:
                result.errors[item.name] = str(exc)
                self.log(f"    błąd: {exc}")
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS and i < len(items):
                    result.errors["(przerwano)"] = f"{consecutive_errors} błędy z rzędu, pominięto {len(items) - i} modeli"
                    break
                continue
            consecutive_errors = 0
            result.scanned_items += 1
            result.listings_seen += fetched
            result.deals.extend(deals)
        result.deals.sort(key=lambda d: d.score, reverse=True)
        return result
