"""Ceny retail z AI (Claude): rozpoznanie produktu z tytułu ogłoszenia i cena regularna znaleziona w sieci, ze źródłem.

Dwa etapy, żeby było tanio:
1. identify — partiami po kilkadziesiąt tytułów, bez wyszukiwarki: nazwa produktu, kategoria i szacunek ceny.
2. lookup — tylko dla kandydatów na okazję: Claude szuka ceny w sieci i zgłasza ją narzędziem report_retail_price
   razem z adresem strony. Adres musi pochodzić z wyników wyszukiwania, inaczej nie pokazujemy go jako źródła.
Oba wyniki są zapamiętywane w State, więc kolejne skany płacą tylko za nowe ogłoszenia i nowe produkty.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import anthropic

from vinted_screener.models import Listing, RetailPrice, RetailSource, normalize
from vinted_screener.state import State

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # przy odmowie API samo przełącza model (fallbacks="default")
# cennik Claude Opus 5.5 w USD: za 1M tokenów wejścia / wyjścia / zapisu do cache / odczytu z cache, i za wyszukiwanie
PRICE_INPUT, PRICE_OUTPUT, PRICE_CACHE_WRITE, PRICE_CACHE_READ = 4.0, 20.0, 5.0, 0.20
PRICE_PER_SEARCH = 0.01

IDENTIFY_BATCH = 40
MAX_WORKERS = 4
MAX_CONTINUATIONS = 3
IDENTIFY_TTL_HOURS = 14 * 24
RETAIL_TTL_HOURS = 30 * 24
NOT_FOUND_TTL_HOURS = 7 * 24
CATEGORIES = ["Sneakersy", "Buty", "Kurtki", "Bluzy i swetry", "Koszulki", "Spodnie i legginsy", "Akcesoria", "Inne"]

_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}

IDENTIFY_SYSTEM = """Rozpoznajesz produkty z ogłoszeń na Vinted i szacujesz ich regularną cenę detaliczną w Polsce.

Dostajesz listę JSON ogłoszeń: i (numer), t (tytuł), b (marka), s (rozmiar). Dla każdego zwróć:
- p: nazwa konkretnego produktu: marka + linia/model, bez koloru, rozmiaru i stanu, np. "Nike Dunk Low Retro",
  "Carhartt WIP Hooded Chase Sweat", "The North Face 1996 Retro Nuptse". Jeśli tytuł nie pozwala ustalić modelu,
  podaj ogólny opis, np. "Nike bluza z kapturem", i q="low". Jeśli to nie jest produkt tej marki, rzecz dziecięca,
  podróbka albo nie da się nic ustalić, daj p=null.
- c: kategoria z listy.
- r: szacowana regularna cena nowego produktu w PLN w oficjalnych sklepach w Polsce (bez promocji i outletów).
- q: pewność rozpoznania modelu: high (konkretny model), medium (linia produktu), low (tylko typ rzeczy).
Zwróć wynik dla każdego ogłoszenia."""

IDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "p": _NULLABLE_STR,
                    "c": {"type": "string", "enum": CATEGORIES},
                    "r": _NULLABLE_NUM,
                    "q": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["i", "p", "c", "r", "q"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

LOOKUP_SYSTEM = """Ustalasz aktualną regularną cenę detaliczną produktu w Polsce i podajesz, skąd ją masz.

- Szukaj w sieci. Najlepsze źródło to oficjalny sklep marki w Polsce (np. nike.com/pl), potem autoryzowani
  sprzedawcy (np. Zalando, Answear, Footshop, Sizeer, Distance, eobuwie) albo porównywarka sklepów (Ceneo).
- Podaj cenę regularną: jeśli produkt jest przeceniony, weź cenę sprzed obniżki, gdy jest widoczna.
- Nie bierz cen z rynku wtórnego ani z ogłoszeń (Vinted, OLX, Allegro Lokalnie, StockX, eBay).
- Jeśli znajdziesz cenę tylko w EUR, przelicz po kursie 4,30 zł i napisz to w note.
- Jeśli nie da się ustalić konkretnego produktu albo nie ma wiarygodnej ceny, zgłoś found=false.
- Na koniec wywołaj report_retail_price dokładnie raz. source_url to adres strony z wyników wyszukiwania,
  na której widać cenę."""

REPORT_TOOL = {
    "name": "report_retail_price",
    "description": "Zgłoś wynik: regularna cena detaliczna produktu w Polsce i strona, na której ją znalazłeś. "
    "Wywołaj dokładnie raz, na końcu.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "price_pln": _NULLABLE_NUM,
            "source_url": _NULLABLE_STR,
            "shop": _NULLABLE_STR,
            "matched_product": _NULLABLE_STR,
            "note": _NULLABLE_STR,
        },
        "required": ["found", "price_pln", "source_url", "shop", "matched_product", "note"],
        "additionalProperties": False,
    },
}

WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 4,
    "user_location": {"type": "approximate", "country": "PL", "city": "Warszawa", "timezone": "Europe/Warsaw"},
}

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


class RetailAIError(RuntimeError):
    """Błąd, przy którym nie ma sensu pytać dalej (zły klucz API, brak uprawnień)."""


def make_anthropic_client() -> Any | None:
    """Klient Claude, jeśli jest klucz (zmienna ANTHROPIC_API_KEY); bez klucza tryb AI jest wyłączony."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    return anthropic.Anthropic(max_retries=3)


@dataclass(frozen=True)
class Identification:
    product: str | None
    category: str
    est_retail: float | None
    confidence: str  # "low" | "medium" | "high"

    @property
    def product_key(self) -> str | None:
        return normalize(self.product) if self.product else None


class CostMeter:
    """Koszt wywołań AI w USD, liczony z `usage` odpowiedzi według cennika Opus 5.5."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usd = 0.0
        self.searches = 0
        self.calls = 0

    def add(self, usage: Any) -> float:
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        cost = (
            tokens_in * PRICE_INPUT + tokens_out * PRICE_OUTPUT + cache_write * PRICE_CACHE_WRITE + cache_read * PRICE_CACHE_READ
        ) / 1_000_000 + searches * PRICE_PER_SEARCH
        with self._lock:
            self.usd += cost
            self.searches += searches
            self.calls += 1
        return cost


def _block_data(block: Any) -> Any:
    if hasattr(block, "model_dump"):
        return block.model_dump()
    if isinstance(block, dict):
        return block
    return getattr(block, "__dict__", block)


def _collect_urls(value: Any, found: set[str]) -> None:
    """Wszystkie adresy z wyników wyszukiwania, cytowań i wyników kodu (dynamiczne filtrowanie)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                found.add(item)
            else:
                _collect_urls(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_urls(item, found)
    elif isinstance(value, str) and "http" in value:
        found.update(_URL_RE.findall(value))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    return f"{_host(url)}{parts.path.rstrip('/')}"


def verify_source(url: str | None, seen_urls: Iterable[str]) -> bool:
    """Źródło jest wiarygodne, gdy ten sam adres (albo strona z tej samej domeny) był w wynikach wyszukiwania."""
    if not url or not _host(url):
        return False
    seen = list(seen_urls)
    return _canonical(url) in {_canonical(u) for u in seen} or _host(url) in {_host(u) for u in seen}


class RetailFinder:
    def __init__(
        self,
        client: Any,
        state: State,
        model: str = MODEL,
        max_usd: float = 2.0,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.model = model
        self.max_usd = max_usd
        self.cost = CostMeter()
        self._debug = debug or (lambda msg: None)

    @property
    def over_budget(self) -> bool:
        return self.cost.usd >= self.max_usd

    def _create(self, **kwargs: Any) -> Any:
        try:
            return self.client.beta.messages.create(
                model=self.model, betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise RetailAIError(f"Claude API odrzuciło klucz: {exc}") from exc

    # ─── etap 1: rozpoznanie produktów ───────────────────────────────────────────

    def identify(self, listings: list[Listing]) -> dict[int, Identification]:
        result: dict[int, Identification] = {}
        todo = []
        for listing in listings:
            cached = self.state.get_cached("identify", str(listing.id), IDENTIFY_TTL_HOURS)
            if cached:
                result[listing.id] = Identification(cached["product"], cached["category"], cached["est_retail"], cached["confidence"])
            else:
                todo.append(listing)
        batches = [todo[i : i + IDENTIFY_BATCH] for i in range(0, len(todo), IDENTIFY_BATCH)]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for found in pool.map(self._identify_batch, batches):
                result.update(found)
        return result

    def _identify_batch(self, batch: list[Listing]) -> dict[int, Identification]:
        if self.over_budget:
            return {}
        rows = [{"i": n, "t": l.title, "b": l.brand, "s": l.size} for n, l in enumerate(batch)]
        try:
            response = self._create(
                max_tokens=8000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": IDENTIFY_SCHEMA}},
                system=IDENTIFY_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(rows, ensure_ascii=False)}],
            )
        except anthropic.AnthropicError as exc:  # jeden nieudany produkt nie przerywa skanu
            self._debug(f"AI rozpoznanie: błąd {exc}")
            return {}
        cost = self.cost.add(response.usage)
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        if response.stop_reason in ("refusal", "max_tokens") or not text:
            self._debug(f"AI rozpoznanie: brak wyniku ({response.stop_reason})")
            return {}
        try:
            items = json.loads(text)["items"]
        except (json.JSONDecodeError, KeyError, TypeError):
            self._debug(f"AI rozpoznanie: niepoprawny JSON: {text[:200]}")
            return {}
        found: dict[int, Identification] = {}
        for item in items:
            n = item.get("i")
            if not isinstance(n, int) or not 0 <= n < len(batch):
                continue
            est = item.get("r")
            ident = Identification(
                product=item.get("p") or None,
                category=item.get("c") if item.get("c") in CATEGORIES else "Inne",
                est_retail=float(est) if isinstance(est, (int, float)) and est > 0 else None,
                confidence=item.get("q") if item.get("q") in ("low", "medium", "high") else "low",
            )
            found[batch[n].id] = ident
            self.state.put_cached("identify", str(batch[n].id), ident.__dict__)
        self._debug(f"AI rozpoznanie: {len(found)}/{len(batch)} ogłoszeń, ${cost:.3f}")
        return found

    # ─── etap 2: cena retail z sieci ─────────────────────────────────────────────

    @staticmethod
    def cache_key(product: str, brand: str) -> str:
        return f"{normalize(brand)}|{normalize(product)}"

    def lookup_many(self, requests: list[tuple[str, str, float | None]]) -> dict[str, RetailPrice | None]:
        """(produkt, marka, szacunek) → cena ze źródłem albo None; klucz wyniku to cache_key."""
        unique = {self.cache_key(p, b): (p, b, est) for p, b, est in requests}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            prices = pool.map(lambda args: self.lookup(*args), unique.values())
            return dict(zip(unique, prices))

    def lookup(self, product: str, brand: str, est_retail: float | None = None) -> RetailPrice | None:
        key = self.cache_key(product, brand)
        cached = self.state.get_cached("retail", key, RETAIL_TTL_HOURS)
        if cached:
            if cached.get("found"):
                return RetailPrice(cached["price"], RetailSource(**cached["source"]))
            if self.state.get_cached("retail", key, NOT_FOUND_TTL_HOURS):
                return None
        if self.over_budget:
            return None

        hint = f"ok. {est_retail:.0f} zł" if est_retail else "brak"
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"Produkt: {product}\nMarka: {brand}\nWstępny szacunek ceny: {hint}"}
        ]
        seen_urls: set[str] = set()
        report = None
        cost = 0.0
        for _ in range(MAX_CONTINUATIONS + 1):
            try:
                response = self._create(
                    max_tokens=6000,
                    output_config={"effort": "medium"},
                    system=LOOKUP_SYSTEM,
                    tools=[WEB_SEARCH_TOOL, REPORT_TOOL],
                    messages=messages,
                )
            except anthropic.AnthropicError as exc:  # jeden nieudany produkt nie przerywa skanu
                self._debug(f"AI cena {product!r}: błąd {exc}")
                return None
            cost += self.cost.add(response.usage)
            for block in response.content:
                data = _block_data(block)
                if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == REPORT_TOOL["name"]:
                    report = block.input
                else:
                    _collect_urls(data, seen_urls)
            if report is not None or response.stop_reason != "pause_turn":
                break
            # długie wyszukiwanie zatrzymane po stronie serwera — wysyłamy dotychczasową odpowiedź, serwer wznawia
            messages.append({"role": "assistant", "content": response.content})

        price = self._price_from_report(report, product, seen_urls)
        if price:
            self.state.put_cached("retail", key, {"found": True, "price": price.price, "source": price.source.__dict__})
            self._debug(f"AI cena {product!r}: {price.price:.0f} zł wg {price.source.label}, ${cost:.3f}")
        else:
            self.state.put_cached("retail", key, {"found": False})
            self._debug(f"AI cena {product!r}: nie znaleziono, ${cost:.3f}")
        return price

    @staticmethod
    def _price_from_report(report: dict[str, Any] | None, product: str, seen_urls: set[str]) -> RetailPrice | None:
        if not report or not report.get("found"):
            return None
        value = report.get("price_pln")
        if not isinstance(value, (int, float)) or not 0 < value < 100_000:
            return None
        url = report.get("source_url")
        verified = verify_source(url, seen_urls)
        label = report.get("shop") or (_host(url) if url else "") or "sieć"
        note = report.get("note") or None
        if not verified:
            # link spoza wyników wyszukiwania — nie pokazujemy go jako źródła
            url = None
            label = "AI (link niezweryfikowany)"
        source = RetailSource(
            kind="web" if verified else "estimate",
            label=label,
            url=url,
            product=report.get("matched_product") or product,
            note=note,
            checked_at=datetime.now(timezone.utc).date().isoformat(),
        )
        return RetailPrice(float(value), source)
