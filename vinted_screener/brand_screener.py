"""Tryb marek: skan wszystkich rzeczy wybranych marek; cenę retail ustala AI i podaje jej źródło."""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from vinted_screener.client import VintedClient
from vinted_screener.models import Deal, Listing, RetailSource, normalize
from vinted_screener.retail import RetailAIError, RetailFinder
from vinted_screener.screener import Filters, ItemCallback, ScanResult, deal_score, describe_funnel, parse_listings, run_scan
from vinted_screener.watchlist import Brand, Settings, WatchItem, size_matches

# szacunek AI bywa niedokładny, więc w sieci sprawdzamy też rzeczy trochę powyżej progu rabatu
CANDIDATE_SLACK = 1.25
MIN_PRODUCT_SAMPLES = 3  # mediana tego samego produktu z bieżącego skanu, jeśli jest z czego liczyć


@dataclass(frozen=True)
class _Estimate:
    listing: Listing
    product: str
    category: str
    est_retail: float
    watch_item: WatchItem | None


class BrandScreener:
    def __init__(
        self,
        client: VintedClient,
        settings: Settings,
        finder: RetailFinder | None,
        watch_items: Sequence[WatchItem] = (),
        filters: Filters = Filters(),
        log: Callable[[str], None] = lambda msg: None,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.finder = finder
        self.cost = finder.cost if finder else None  # zostaje, nawet gdy AI wyłączy się w trakcie skanu
        self.watch_items = list(watch_items)
        self.filters = filters
        self.log = log
        self.debug = debug or (lambda msg: None)
        self.ai_error: str | None = None

    @property
    def min_discount(self) -> float:
        return self.filters.min_discount if self.filters.min_discount is not None else self.settings.min_discount

    def _estimates(self, brand: Brand, listings: list[Listing], funnel: Counter[str]) -> list[_Estimate]:
        """Szacunek ceny dla każdego ogłoszenia: z watchlisty (za darmo), a resztę rozpoznaje AI."""
        watch = {l.id: w for l in listings if (w := next((w for w in self.watch_items if w.matches(l)), None))}
        rest = [l for l in listings if l.id not in watch]
        idents = {}
        if self.finder and rest:
            try:
                idents = self.finder.identify(rest)
            except RetailAIError as exc:
                self._disable_ai(exc)
        estimates = []
        for listing in listings:
            if w := watch.get(listing.id):
                estimates.append(_Estimate(listing, w.name, w.category, w.retail, w))
            elif (ident := idents.get(listing.id)) and ident.product and ident.est_retail and ident.confidence != "low":
                estimates.append(_Estimate(listing, ident.product, ident.category, ident.est_retail, None))
            else:
                funnel["unknown"] += 1
        return estimates

    def _disable_ai(self, exc: Exception) -> None:
        self.ai_error = str(exc)
        self.finder = None
        self.log(f"    AI wyłączone: {exc}")

    def screen_brand(self, brand: Brand, extra_query: str = "") -> tuple[list[Deal], Counter[str]]:
        raw = self.client.search(
            f"{brand.query} {extra_query}".strip(),
            status_ids=self.settings.status_ids,
            price_to=self.filters.max_price,
            pages=self.settings.brand_pages,
            per_page=self.settings.per_page,
        )
        funnel: Counter[str] = Counter(fetched=len(raw))
        sizes = self.filters.sizes or self.settings.sizes
        listings, seen = [], set()
        for listing in parse_listings(raw, self.settings, self.settings.status_ids, brand.matches, funnel):
            if listing.id in seen:
                funnel["duplicate"] += 1
            elif not size_matches(listing.size, sizes):
                funnel["size"] += 1
            else:
                seen.add(listing.id)
                listings.append(listing)

        estimates = self._estimates(brand, listings, funnel)
        by_product: dict[str, list[float]] = {}
        for e in estimates:
            by_product.setdefault(normalize(e.product), []).append(e.listing.total_price)

        threshold = 1 - self.min_discount
        candidates = [e for e in estimates if e.listing.total_price <= e.est_retail * threshold * CANDIDATE_SLACK]
        funnel["price"] += len(estimates) - len(candidates)

        prices = {}
        if self.finder and candidates:
            try:
                prices = self.finder.lookup_many([(e.product, brand.name, e.est_retail) for e in candidates])
            except RetailAIError as exc:
                self._disable_ai(exc)

        deals = []
        for e in candidates:
            retail, source = self._retail(e, brand, prices)
            total = e.listing.total_price
            retail_discount = 1 - total / retail
            if retail_discount < self.min_discount:
                funnel["price"] += 1
                continue
            floor = e.watch_item.suspicious_below if e.watch_item else brand.suspicious_below
            suspicious = total < retail * floor
            if suspicious and not self.filters.show_suspicious:
                funnel["suspicious"] += 1
                continue
            peers = by_product.get(normalize(e.product), [])
            median = round(statistics.median(peers), 2) if len(peers) >= MIN_PRODUCT_SAMPLES else None
            market_discount = 1 - total / median if median else None
            if self.filters.min_market_discount is not None and (
                market_discount is None or market_discount < self.filters.min_market_discount
            ):
                funnel["market"] += 1
                continue
            deals.append(
                Deal(
                    listing=e.listing,
                    model=e.product,
                    category=e.category,
                    retail=retail,
                    retail_source=source,
                    retail_discount=retail_discount,
                    market_median=median,
                    market_discount=market_discount,
                    score=deal_score(retail_discount, market_discount, e.listing.condition),
                    suspicious=suspicious,
                )
            )
        self.debug(
            f"{brand.name}: pobrano {len(raw)}, rozpoznano {len(estimates)}, sprawdzono w sieci {len(candidates)}, "
            f"okazji {len(deals)}; odpadło: {describe_funnel(funnel) or '—'}"
        )
        return deals, funnel

    def _retail(self, e: _Estimate, brand: Brand, prices: dict) -> tuple[float, RetailSource]:
        """Cena z sieci, jeśli AI ją znalazło; inaczej watchlista albo szacunek AI — zawsze z opisem źródła."""
        found = prices.get(RetailFinder.cache_key(e.product, brand.name))
        if found:
            return found.price, found.source
        if e.watch_item:
            return e.watch_item.retail, RetailSource(
                kind="watchlist", label="watchlista (orientacyjnie)", product=e.watch_item.name
            )
        if self.finder is None:
            why = "AI wyłączone"
        elif self.finder.over_budget:
            why = "nie sprawdzono w sieci: osiągnięto limit kosztu AI"
        else:
            why = "AI nie znalazło ceny w sieci"
        return e.est_retail, RetailSource(kind="estimate", label="szacunek AI", product=e.product, note=why)

    def screen(
        self,
        brands: list[Brand],
        extra_query: str = "",
        on_item: ItemCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ScanResult:
        def on_brand(i: int, brand: Brand, result: ScanResult) -> None:
            self._update_cost(result)
            if on_item:
                on_item(i, brand, result)

        result = run_scan(brands, lambda b: self.screen_brand(b, extra_query), self.log, on_brand, should_stop)
        self._update_cost(result)
        return result

    def _update_cost(self, result: ScanResult) -> None:
        if self.cost:
            result.ai_cost_usd = round(self.cost.usd, 4)
            result.ai_searches = self.cost.searches
        if self.ai_error:
            result.errors["AI"] = self.ai_error
