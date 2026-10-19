"""Tryb marek: skan wszystkich rzeczy wybranych marek; cenę odniesienia bierze z najlepszego dostępnego źródła.

Kolejność źródeł (każda okazja pokazuje, które zadziałało):
1. AI z wyszukiwarką — cena ze sklepu z linkiem (opcjonalnie, płatne),
2. Ceneo — najniższa cena nowego produktu w sklepach,
3. watchlista — ręczna, orientacyjna cena modelu,
4. opis sprzedającego — "cena w sklepie 499 zł",
5. AI bez potwierdzenia — szacunek,
6. mediana podobnych ofert na Vinted — gdy nic innego nie ma (to cena rynkowa, nie retail).
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from vinted_screener.ceneo import CeneoPrices
from vinted_screener.client import VintedClient
from vinted_screener.models import Deal, Listing, RetailPrice, RetailSource, condition_status_id, normalize
from vinted_screener.products import declared_retail, guess_category, is_specific, product_name, product_tokens
from vinted_screener.retail import RetailAIError, RetailFinder
from vinted_screener.screener import Filters, ItemCallback, ScanResult, deal_score, describe_funnel, parse_listings, run_scan
from vinted_screener.state import State
from vinted_screener.watchlist import Brand, Settings, WatchItem, size_matches

# szacunek bywa niedokładny, więc dokładniej sprawdzamy też rzeczy trochę powyżej progu rabatu
CANDIDATE_SLACK = 1.25
MIN_PRODUCT_SAMPLES = 3  # mediana pokazywana obok ceny retail
MIN_MARKET_GROUP = 6  # mediana jako jedyne odniesienie — potrzeba więcej ofert
NEW_STATUS_IDS = (6, 1)  # nowy z metką / bez metki — tu sprzedający najczęściej podają cenę sklepową
DESCRIPTION_TTL_HOURS = 14 * 24


@dataclass
class _Product:
    listing: Listing
    name: str
    category: str
    tokens: list[str]  # słowa modelu; puste = ogólny tytuł ("bluza Nike")
    est_retail: float | None = None  # watchlista albo szacunek AI
    watch_item: WatchItem | None = None
    group_median: float | None = field(default=None)
    group_size: int = 0

    @property
    def key(self) -> str:
        return normalize(self.name)

    @property
    def specific(self) -> bool:
        return is_specific(self.tokens)


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
        ceneo: CeneoPrices | None = None,
        read_descriptions: bool = False,
        state: State | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.finder = finder
        self.cost = finder.cost if finder else None  # zostaje, nawet gdy AI wyłączy się w trakcie skanu
        self.watch_items = list(watch_items)
        self.filters = filters
        self.log = log
        self.debug = debug or (lambda msg: None)
        self.ceneo = ceneo
        self.read_descriptions = read_descriptions
        self.state = state or State()  # tu pamiętamy ceny z opisów, żeby nie pobierać ich co skan
        self.ai_error: str | None = None
        self.lookups: Counter[str] = Counter()

    @property
    def min_discount(self) -> float:
        return self.filters.min_discount if self.filters.min_discount is not None else self.settings.min_discount

    def _disable_ai(self, exc: Exception) -> None:
        self.ai_error = str(exc)
        self.finder = None
        self.log(f"    AI wyłączone: {exc}")

    # ─── 1. jaki to produkt ──────────────────────────────────────────────────────

    def _products(self, brand: Brand, listings: list[Listing], funnel: Counter[str]) -> list[_Product]:
        """Watchlista (za darmo), AI jeśli jest, a reszta po słowach z tytułu."""
        aliases = brand.aliases or (brand.name,)
        watch = {l.id: w for l in listings if (w := next((w for w in self.watch_items if w.matches(l)), None))}
        idents = {}
        rest = [l for l in listings if l.id not in watch]
        if self.finder and rest:
            try:
                idents = self.finder.identify(rest)
            except RetailAIError as exc:
                self._disable_ai(exc)

        products = []
        for listing in listings:
            if w := watch.get(listing.id):
                tokens = product_tokens(w.name, aliases)
                products.append(_Product(listing, w.name, w.category, tokens, w.retail, w))
            elif ident := idents.get(listing.id):
                if not ident.product:
                    funnel["unknown"] += 1  # AI: to nie jest produkt tej marki / podróbka / rzecz dziecięca
                    continue
                tokens = product_tokens(ident.product, aliases) if ident.confidence != "low" else []
                est = ident.est_retail if ident.confidence != "low" else None
                products.append(_Product(listing, ident.product, ident.category, tokens, est))
            else:
                tokens = product_tokens(listing.title, aliases)
                category = guess_category(listing.title)
                if is_specific(tokens):
                    name = product_name(brand.name, tokens)
                else:
                    name = f"{brand.name} — {category.lower()}" if category != "Inne" else brand.name
                products.append(_Product(listing, name, category, tokens))

        groups: dict[tuple, list[_Product]] = {}
        for p in products:
            if p.specific:
                groups.setdefault((p.category, tuple(p.tokens[:2])), []).append(p)
        for members in groups.values():
            median = statistics.median(m.listing.total_price for m in members)
            for m in members:
                m.group_median, m.group_size = round(median, 2), len(members)
        return products

    # ─── 2. ceny ze źródeł ───────────────────────────────────────────────────────

    def _could_be_deal(self, members: list[_Product]) -> bool:
        est = members[0].est_retail
        cheapest = min(m.listing.total_price for m in members)
        return est is None or cheapest <= est * (1 - self.min_discount) * CANDIDATE_SLACK

    def _ai_prices(self, brand: Brand, by_key: dict[str, list[_Product]]) -> dict[str, RetailPrice]:
        if not self.finder:
            return {}
        requests = [(m[0].name, brand.name, m[0].est_retail) for m in by_key.values() if m[0].est_retail and self._could_be_deal(m)]
        try:
            found = self.finder.lookup_many(requests) if requests else {}
        except RetailAIError as exc:
            self._disable_ai(exc)
            return {}
        return {normalize(name): price for (name, _, _) in requests if (price := found.get(RetailFinder.cache_key(name, brand.name)))}

    def _ceneo_prices(self, by_key: dict[str, list[_Product]], have: set[str]) -> dict[str, RetailPrice]:
        if not self.ceneo:
            return {}
        # najpierw modele z watchlisty, potem produkty z największą liczbą ofert
        order = sorted(
            (k for k, m in by_key.items() if k not in have and m[0].specific and self._could_be_deal(m)),
            key=lambda k: (by_key[k][0].watch_item is None, -len(by_key[k]), k),
        )
        start = self.ceneo.lookups
        prices = {}
        for key in order:
            if self.ceneo.lookups - start >= self.settings.max_ceneo_per_brand:
                break
            first = by_key[key][0]
            if price := self.ceneo.price_for(first.name, first.tokens[:3]):
                prices[key] = price
        self.lookups["ceneo"] += self.ceneo.lookups - start
        return prices

    def _seller_prices(self, products: list[_Product], have: set[str]) -> dict[int, RetailPrice]:
        """Ceny z opisów — dla ogłoszeń bez ceny produktu; najpierw nowe i najtańsze względem podobnych."""
        if not self.read_descriptions:
            return {}

        def priority(p: _Product) -> tuple:
            new = condition_status_id(p.listing.condition) in NEW_STATUS_IDS
            ratio = p.listing.total_price / p.group_median if p.group_median else 1.0
            return (not new, ratio, -(p.listing.uploaded_at.timestamp() if p.listing.uploaded_at else 0))

        pool = sorted((p for p in products if p.key not in have and p.watch_item is None), key=priority)
        prices = {}
        fetched = 0
        for p in pool:
            cache_key = str(p.listing.id)
            cached = self.state.get_cached("description", cache_key, DESCRIPTION_TTL_HOURS)
            if cached is None:
                if fetched >= self.settings.max_descriptions_per_brand:
                    continue
                fetched += 1
                description = self.client.item_description(p.listing.id) or ""
                found = declared_retail(description, p.listing.total_price)
                cached = {"amount": found[0], "quote": found[1]} if found else {}
                self.state.put_cached("description", cache_key, cached)
            if cached.get("amount"):
                prices[p.listing.id] = RetailPrice(
                    cached["amount"],
                    RetailSource(kind="seller", label="sprzedający w opisie", product=p.name, note=f"„{cached['quote']}”"),
                )
        self.lookups["opisy"] += fetched
        return prices

    # ─── 3. okazje ───────────────────────────────────────────────────────────────

    def _reference(
        self, p: _Product, ai: dict[str, RetailPrice], ceneo: dict[str, RetailPrice], seller: dict[int, RetailPrice]
    ) -> RetailPrice | None:
        ai_price = ai.get(p.key)
        if ai_price and ai_price.source.kind == "web":
            return ai_price
        if p.key in ceneo:
            return ceneo[p.key]
        if p.watch_item:
            source = RetailSource(kind="watchlist", label="watchlista (orientacyjnie)", product=p.watch_item.name)
            return RetailPrice(p.watch_item.retail, source)
        if p.listing.id in seller:
            return seller[p.listing.id]
        if ai_price:
            return ai_price
        if p.est_retail:
            why = "AI nie znalazło ceny w sieci" if self.finder and not self.finder.over_budget else "nie sprawdzono w sieci"
            return RetailPrice(p.est_retail, RetailSource(kind="estimate", label="szacunek AI", product=p.name, note=why))
        if p.group_median and p.group_size >= MIN_MARKET_GROUP:
            source = RetailSource(
                kind="market", label=f"mediana Vinted ({p.group_size} ofert)", product=p.name,
                note="brak ceny retail — porównanie z podobnymi ofertami",
            )
            return RetailPrice(p.group_median, source)
        return None

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

        products = self._products(brand, listings, funnel)
        by_key: dict[str, list[_Product]] = {}
        for p in products:
            by_key.setdefault(p.key, []).append(p)
        ai = self._ai_prices(brand, by_key)
        have = {k for k, v in ai.items() if v.source.kind == "web"}
        ceneo = self._ceneo_prices(by_key, have)
        have |= set(ceneo)
        seller = self._seller_prices(products, have)

        deals = []
        for p in products:
            ref = self._reference(p, ai, ceneo, seller)
            if ref is None:
                funnel["noref"] += 1
                continue
            total = p.listing.total_price
            retail_discount = 1 - total / ref.price
            if retail_discount < self.min_discount:
                funnel["price"] += 1
                continue
            floor = p.watch_item.suspicious_below if p.watch_item else brand.suspicious_below
            suspicious = total < ref.price * floor
            if suspicious and not self.filters.show_suspicious:
                funnel["suspicious"] += 1
                continue
            median = p.group_median if p.group_size >= MIN_PRODUCT_SAMPLES else None
            market_discount = 1 - total / median if median else None
            if self.filters.min_market_discount is not None and (
                market_discount is None or market_discount < self.filters.min_market_discount
            ):
                funnel["market"] += 1
                continue
            deals.append(
                Deal(
                    listing=p.listing,
                    model=p.name,
                    category=p.category,
                    retail=ref.price,
                    retail_source=ref.source,
                    retail_discount=retail_discount,
                    market_median=median,
                    market_discount=market_discount,
                    score=deal_score(retail_discount, market_discount, p.listing.condition),
                    suspicious=suspicious,
                )
            )
        if self.ceneo and self.ceneo.disabled_reason:
            self.log(f"    Ceneo wyłączone: {self.ceneo.disabled_reason}")
        self.debug(
            f"{brand.name}: pobrano {len(raw)}, produktów {len(by_key)}, ceny: AI {len(ai)}, Ceneo {len(ceneo)}, "
            f"opisy {len(seller)}; okazji {len(deals)}; odpadło: {describe_funnel(funnel) or '—'}"
        )
        return deals, funnel

    def screen(
        self,
        brands: list[Brand],
        extra_query: str = "",
        on_item: ItemCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ScanResult:
        def on_brand(i: int, brand: Brand, result: ScanResult) -> None:
            self._update_result(result)
            if on_item:
                on_item(i, brand, result)

        result = run_scan(brands, lambda b: self.screen_brand(b, extra_query), self.log, on_brand, should_stop)
        self._update_result(result)
        return result

    def _update_result(self, result: ScanResult) -> None:
        if self.cost:
            result.ai_cost_usd = round(self.cost.usd, 4)
            result.ai_searches = self.cost.searches
        result.lookups = Counter(self.lookups)
        if self.ai_error:
            result.errors["AI"] = self.ai_error
        if self.ceneo and self.ceneo.disabled_reason:
            result.errors["Ceneo"] = self.ceneo.disabled_reason
