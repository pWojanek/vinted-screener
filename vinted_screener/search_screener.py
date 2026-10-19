"""Tryb wyszukiwania: wpisujesz markę albo typ rzeczy ("Nike bluza", "kurtka puchowa"), a cenę odniesienia
każdego ogłoszenia screener bierze z najlepszego dostępnego źródła.

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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import groupby

from vinted_screener.ceneo import CeneoPrices
from vinted_screener.client import VintedClient
from vinted_screener.models import Deal, Listing, RetailPrice, RetailSource, condition_status_id, normalize
from vinted_screener.products import declared_retail, guess_category, is_specific, product_name, product_tokens
from vinted_screener.retail import RetailAIError, RetailFinder
from vinted_screener.screener import Filters, ItemCallback, ScanResult, deal_score, describe_funnel, parse_listings, run_scan
from vinted_screener.state import State
from vinted_screener.watchlist import Brand, Settings, WatchItem, globally_excluded, size_matches

# szacunek bywa niedokładny, więc dokładniej sprawdzamy też rzeczy trochę powyżej progu rabatu
CANDIDATE_SLACK = 1.25
MIN_PRODUCT_SAMPLES = 3  # mediana pokazywana obok ceny retail
MIN_MARKET_GROUP = 6  # mediana jako jedyne odniesienie — potrzeba więcej ofert
NEW_STATUS_IDS = (6, 1)  # nowy z metką / bez metki — tu sprzedający najczęściej podają cenę sklepową
DESCRIPTION_TTL_HOURS = 14 * 24
MAX_SEARCH_LENGTH = 80
SHORT_ALIAS = 2  # "On" rozpoznajemy tylko jako całą frazę, bo inaczej łapałoby zwykłe słowa
# podpowiedzi w wyszukiwarce na stronie (obok znanych marek)
TYPE_SUGGESTIONS = (
    "bluza", "bluza z kapturem", "sweter", "kurtka", "kurtka puchowa", "kurtka przejściowa", "płaszcz", "kamizelka",
    "sneakersy", "buty", "koszulka", "koszula", "spodnie", "jeansy", "dresy", "szorty", "plecak", "torba", "czapka",
)


def _span(words: list[str], compact: str, whole_only: bool = False) -> tuple[int, int] | None:
    """Gdzie w słowach frazy jest nazwa marki; porównanie bez spacji, więc "cp company" = "C.P. Company"."""
    for i in range(len(words)):
        joined = ""
        for j in range(i, len(words)):
            joined += words[j]
            if joined == compact and (not whole_only or (i, j + 1) == (0, len(words))):
                return i, j + 1
            if len(joined) >= len(compact):
                break
    return None


def find_brands(text: str, brands: Iterable[Brand]) -> list[tuple[Brand, int, int]]:
    """Znane marki we frazie i ich miejsca (zakres słów po normalize()); przy nakładaniu wygrywa dłuższa nazwa.

    "Nike Air Jordan 1" to Jordan i Nike — sprzedający wpisują jedną albo drugą markę.
    """
    words = normalize(text).split()
    candidates = []
    for brand in brands:
        for alias in dict.fromkeys((brand.name, *brand.aliases)):
            compact = normalize(alias).replace(" ", "")
            if compact and (span := _span(words, compact, whole_only=len(compact) <= SHORT_ALIAS)):
                candidates.append((len(compact), brand, *span))
    found: list[tuple[Brand, int, int]] = []
    for _, brand, start, end in sorted(candidates, key=lambda c: -c[0]):
        if all(b is not brand and (end <= s or start >= e) for b, s, e in found):
            found.append((brand, start, end))
    return sorted(found, key=lambda f: f[1])  # w kolejności z frazy


def mentions_brand(text: str, brand: str) -> bool:
    """Czy marka z ogłoszenia jest wpisana we frazie ("Zara sukienka" → Zara), także gdy nie ma jej na liście."""
    compact = normalize(brand).replace(" ", "")
    return bool(compact) and _span(normalize(text).split(), compact, whole_only=len(compact) <= SHORT_ALIAS) is not None


def vinted_query(text: str, found: list[tuple[Brand, int, int]]) -> str:
    """Fraza dla Vinted: nazwy marek zamienione na Brand.query ("TNF" → "the north face"), reszta w oryginalnej pisowni."""
    labelled, position = [], 0
    for token in text.split():
        size = len(normalize(token).split())
        if not size:
            continue  # sama interpunkcja
        span = next((f for f in found if f[1] <= position and position + size <= f[2]), None)
        labelled.append((token, span))
        position += size
    parts: list[str] = []
    for span, group in groupby(labelled, key=lambda item: item[1]):
        tokens = [token for token, _ in group]
        if span is None:
            parts += tokens
            continue
        said, query = normalize(" ".join(tokens)).split(), normalize(span[0].query).split()
        # dłuższa nazwa, która zawiera Brand.query ("Air Jordan", "Polo Ralph Lauren"), zostaje, bo zawęża wyniki
        keep = len(said) > len(query) and any(said[i : i + len(query)] == query for i in range(len(said)))
        parts += tokens if keep else [span[0].query]
    return " ".join(parts)


@dataclass(frozen=True)
class Search:
    """Jedna fraza z wyszukiwarki. Znana marka we frazie zawęża wyniki do niej, a sam typ rzeczy bierze wszystkie marki."""

    text: str  # tak, jak wpisał użytkownik
    query: str  # fraza dla Vinted
    brands: tuple[Brand, ...] = ()  # znane marki we frazie; puste = sam typ rzeczy
    only_known_brands: bool = True  # przy frazie bez marki: tylko marki z listy [[brand]]

    @property
    def name(self) -> str:
        return self.text

    @property
    def scope(self) -> str:
        """Które marki wchodzą w grę — do postępu na stronie i w terminalu."""
        if self.brands:
            return ("marka " if len(self.brands) == 1 else "marki ") + ", ".join(b.name for b in self.brands)
        return "znane marki" if self.only_known_brands else "wszystkie marki"

    @classmethod
    def parse(cls, text: str, known_brands: Sequence[Brand], only_known_brands: bool = True) -> Search:
        text = " ".join(text.split())
        found = find_brands(text, known_brands)
        return cls(text, vinted_query(text, found), tuple(b for b, _, _ in found), only_known_brands)


def parse_searches(texts: Iterable[str], known_brands: Sequence[Brand], only_known_brands: bool = True) -> list[Search]:
    """Frazy bez pustych i powtórzeń (wielkość liter i polskie znaki bez znaczenia)."""
    unique: dict[str, str] = {}
    for text in texts:
        if normalize(text):
            unique.setdefault(normalize(text), text)
    return [Search.parse(t, known_brands, only_known_brands) for t in unique.values()]


@dataclass
class _Product:
    listing: Listing
    brand: Brand
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


class SearchScreener:
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
        known_brands: Sequence[Brand] = (),
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
        self.known_brands = list(known_brands)
        self._other_brands: dict[str, Brand] = {}
        self.ai_error: str | None = None
        self.lookups: Counter[str] = Counter()

    @property
    def min_discount(self) -> float:
        return self.filters.min_discount if self.filters.min_discount is not None else self.settings.min_discount

    def _disable_ai(self, exc: Exception) -> None:
        self.ai_error = str(exc)
        self.finder = None
        self.log(f"    AI wyłączone: {exc}")

    # ─── 0. które ogłoszenia i jakiej marki ──────────────────────────────────────

    def _known_brand(self, brand: str) -> Brand | None:
        return next((b for b in self.known_brands if b.owns(brand)), None)

    def _brand_of(self, search: Search, listing: Listing) -> Brand:
        """Marka do nazwy produktu, Ceneo i progu podejrzanej ceny: z frazy, z listy albo z samego ogłoszenia."""
        if search.brands:
            return next((b for b in search.brands if b.owns(listing.brand)), search.brands[0])
        known = self._known_brand(listing.brand)
        if known:
            return known
        key = normalize(listing.brand)
        if key not in self._other_brands:
            self._other_brands[key] = Brand.custom(listing.brand.strip(), self.settings)
        return self._other_brands[key]

    def _accepts(self, search: Search) -> Callable[[Listing], bool]:
        if search.brands:
            return lambda listing: any(b.matches(listing) for b in search.brands)

        def accept(listing: Listing) -> bool:
            # bez marki nie da się ustalić ceny sklepowej, a no-name'y zasypałyby wyniki
            if not listing.brand.strip() or globally_excluded(normalize(f"{listing.title} {listing.brand}")):
                return False
            if not search.only_known_brands or mentions_brand(search.text, listing.brand):
                return True
            return self._known_brand(listing.brand) is not None

        return accept

    # ─── 1. jaki to produkt ──────────────────────────────────────────────────────

    def _products(self, search: Search, listings: list[Listing], funnel: Counter[str]) -> list[_Product]:
        """Watchlista (za darmo), AI jeśli jest, a reszta po słowach z tytułu."""
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
            brand = self._brand_of(search, listing)
            aliases = brand.aliases or (brand.name,)
            if w := watch.get(listing.id):
                tokens = product_tokens(w.name, aliases)
                products.append(_Product(listing, brand, w.name, w.category, tokens, w.retail, w))
            elif ident := idents.get(listing.id):
                if not ident.product:
                    funnel["unknown"] += 1  # AI: to nie jest produkt tej marki / podróbka / rzecz dziecięca
                    continue
                tokens = product_tokens(ident.product, aliases) if ident.confidence != "low" else []
                est = ident.est_retail if ident.confidence != "low" else None
                products.append(_Product(listing, brand, ident.product, ident.category, tokens, est))
            else:
                tokens = product_tokens(listing.title, aliases)
                category = guess_category(listing.title)
                if is_specific(tokens):
                    name = product_name(brand.name, tokens)
                else:
                    name = f"{brand.name} — {category.lower()}" if category != "Inne" else brand.name
                products.append(_Product(listing, brand, name, category, tokens))

        groups: dict[tuple, list[_Product]] = {}
        for p in products:
            if p.specific:
                groups.setdefault((normalize(p.brand.name), p.category, tuple(p.tokens[:2])), []).append(p)
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

    def _ai_prices(self, by_key: dict[str, list[_Product]]) -> dict[str, RetailPrice]:
        if not self.finder:
            return {}
        requests = [
            (m[0].name, m[0].brand.name, m[0].est_retail) for m in by_key.values() if m[0].est_retail and self._could_be_deal(m)
        ]
        try:
            found = self.finder.lookup_many(requests) if requests else {}
        except RetailAIError as exc:
            self._disable_ai(exc)
            return {}
        return {
            normalize(name): price for (name, brand, _) in requests if (price := found.get(RetailFinder.cache_key(name, brand)))
        }

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
            if self.ceneo.lookups - start >= self.settings.max_ceneo_per_search:
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
                if fetched >= self.settings.max_descriptions_per_search:
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

    def screen_search(self, search: Search) -> tuple[list[Deal], Counter[str]]:
        raw = self.client.search(
            search.query,
            status_ids=self.settings.status_ids,
            price_to=self.filters.max_price,
            pages=self.settings.search_pages,
            per_page=self.settings.per_page,
        )
        funnel: Counter[str] = Counter(fetched=len(raw))
        sizes = self.filters.sizes or self.settings.sizes
        listings, seen = [], set()
        for listing in parse_listings(raw, self.settings, self.settings.status_ids, self._accepts(search), funnel):
            if listing.id in seen:
                funnel["duplicate"] += 1
            elif not size_matches(listing.size, sizes):
                funnel["size"] += 1
            else:
                seen.add(listing.id)
                listings.append(listing)

        products = self._products(search, listings, funnel)
        by_key: dict[str, list[_Product]] = {}
        for p in products:
            by_key.setdefault(p.key, []).append(p)
        ai = self._ai_prices(by_key)
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
            floor = p.watch_item.suspicious_below if p.watch_item else p.brand.suspicious_below
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
            f"{search.name} (Vinted: {search.query!r}, {search.scope}): "
            f"pobrano {len(raw)}, produktów {len(by_key)}, ceny: AI {len(ai)}, Ceneo {len(ceneo)}, "
            f"opisy {len(seller)}; okazji {len(deals)}; odpadło: {describe_funnel(funnel) or '—'}"
        )
        return deals, funnel

    def screen(
        self,
        searches: list[Search],
        on_item: ItemCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ScanResult:
        def on_search(i: int, search: Search, result: ScanResult) -> None:
            self._update_result(result)
            if on_item:
                on_item(i, search, result)

        result = run_scan(searches, self.screen_search, self.log, on_search, should_stop)
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
