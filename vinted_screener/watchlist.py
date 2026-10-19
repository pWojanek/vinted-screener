"""Wczytywanie watchlisty (watchlist.toml) i dopasowywanie ogłoszeń do pozycji."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vinted_screener.models import Listing, normalize

# Odrzucane zawsze, niezależnie od pozycji: podróbki, same pudełka, rzeczy dziecięce, zamiany, uszkodzone.
GLOBAL_EXCLUDE = (
    "replika|replica|podrobka|fake|kopia|inspired|inspirowan*|a la|w stylu|typu|nie oryginal*|nieoryginal*",
    "samo pudelko|tylko pudelko|pudelko po|same sznurowki|tylko sznurowki|wkladki|brelok*|naszywka|naklejka|plakat|miniatura",
    "dzieci*|dziecko|dziecka|dla chlopca|dla dziewczynki|kids|junior|niemowl*|dla lalki",
    "zamiana|zamienie|wymiana|wymienie",
    "uszkodzone|uszkodzony|uszkodzona|do naprawy|zniszczone|dziura|dziurawe|z defektem|wadliw*",
)


class WatchlistError(ValueError):
    pass


def compile_term(term: str) -> re.Pattern[str]:
    """Fraza z alternatywami "a|b c"; słowa dopasowywane w całości, "*" na końcu = dowolna końcówka.

    Słowa frazy mogą być sklejone w tytule ("xt 6" pasuje do "XT-6" i "XT6").
    """
    alternatives = []
    for alt in term.split("|"):
        words = normalize(alt).split()
        if not words:
            continue
        body = r"\s?".join(re.escape(w) for w in words)
        tail = "" if alt.strip().endswith("*") else r"\b"
        alternatives.append(rf"\b{body}{tail}")
    if not alternatives:
        raise WatchlistError(f"pusta fraza: {term!r}")
    return re.compile("|".join(alternatives))


_GLOBAL_EXCLUDE_PATTERNS = tuple(compile_term(t) for t in GLOBAL_EXCLUDE)


@dataclass(frozen=True)
class Settings:
    domain: str = "www.vinted.pl"
    currency: str = "PLN"
    min_discount: float = 0.5
    suspicious_below: float = 0.2
    status_ids: tuple[int, ...] = (6, 1, 2)
    sizes: tuple[str, ...] = ()
    pages: int = 1
    per_page: int = 96
    delay: float = 1.5
    market_ttl_hours: float = 12.0

    @property
    def site_url(self) -> str:
        return "https://www." + self.domain.removeprefix("www.")


@dataclass(frozen=True)
class WatchItem:
    name: str
    category: str
    query: str
    retail: float
    min_discount: float
    suspicious_below: float
    status_ids: tuple[int, ...]
    brands: tuple[str, ...] = ()
    require: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    _brand_keys: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)
    _require: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)
    _exclude: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        brand_keys = tuple((normalize(b), normalize(b).replace(" ", "")) for b in self.brands)
        object.__setattr__(self, "_brand_keys", brand_keys)
        object.__setattr__(self, "_require", tuple(compile_term(t) for t in self.require))
        object.__setattr__(self, "_exclude", tuple(compile_term(t) for t in self.exclude) + _GLOBAL_EXCLUDE_PATTERNS)

    @property
    def max_price(self) -> float:
        """Najwyższa cena, która jeszcze jest okazją."""
        return self.retail * (1 - self.min_discount)

    @property
    def suspicious_price(self) -> float:
        """Poniżej tej ceny ogłoszenie jest podejrzane."""
        return self.retail * self.suspicious_below

    def brand_matches(self, brand: str) -> bool:
        if not self._brand_keys:
            return True
        spaced = normalize(brand)
        compact = spaced.replace(" ", "")
        # "Nike" pasuje też do "Nike SB" / "Nike ACG", ale "On" nie pasuje do "Only".
        return any(compact == b_compact or spaced.startswith(b_spaced + " ") for b_spaced, b_compact in self._brand_keys)

    def matches(self, listing: Listing) -> bool:
        if not self.brand_matches(listing.brand):
            return False
        haystack = normalize(f"{listing.title} {listing.brand}")
        if not all(p.search(haystack) for p in self._require):
            return False
        return not any(p.search(haystack) for p in self._exclude)


def size_matches(size: str, wanted: tuple[str, ...]) -> bool:
    """"42" pasuje do "42", "EU 42" i "M / 42", ale nie do "42,5"."""
    if not wanted:
        return True
    tokens = {t for t in re.split(r"[\s/|]+", size.lower().replace(",", ".")) if t}
    return any(w.lower().replace(",", ".") in tokens for w in wanted)


def _pct(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or not 0 <= value < 100:
        raise WatchlistError(f"{name} musi być liczbą procent z zakresu 0-99, jest {value!r}")
    return value / 100


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise WatchlistError(f"{name} musi być listą tekstów")
    return tuple(value)


def load_watchlist(path: str | Path) -> tuple[Settings, list[WatchItem]]:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WatchlistError(f"nie znaleziono pliku {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise WatchlistError(f"{path}: błąd składni TOML: {exc}") from None

    raw_settings = data.get("settings", {})
    defaults = Settings()
    settings = Settings(
        domain=raw_settings.get("domain", defaults.domain),
        currency=raw_settings.get("currency", defaults.currency),
        min_discount=_pct(raw_settings.get("min_discount", 50), "settings.min_discount"),
        suspicious_below=_pct(raw_settings.get("suspicious_below", 20), "settings.suspicious_below"),
        status_ids=tuple(raw_settings.get("status_ids", defaults.status_ids)),
        sizes=_str_tuple(raw_settings.get("sizes", []), "settings.sizes"),
        pages=int(raw_settings.get("pages", defaults.pages)),
        per_page=min(int(raw_settings.get("per_page", defaults.per_page)), 96),
        delay=float(raw_settings.get("delay", defaults.delay)),
        market_ttl_hours=float(raw_settings.get("market_ttl_hours", defaults.market_ttl_hours)),
    )

    items = []
    seen_names = set()
    for i, raw in enumerate(data.get("item", []), start=1):
        where = f"item #{i} ({raw.get('name', '?')})"
        for key in ("name", "query", "retail"):
            if key not in raw:
                raise WatchlistError(f"{where}: brak pola {key!r}")
        if raw["name"] in seen_names:
            raise WatchlistError(f"{where}: zdublowana nazwa")
        seen_names.add(raw["name"])
        if not isinstance(raw["retail"], (int, float)) or raw["retail"] <= 0:
            raise WatchlistError(f"{where}: retail musi być dodatnią liczbą")
        try:
            item = WatchItem(
                name=raw["name"],
                category=raw.get("category", "Inne"),
                query=raw["query"],
                retail=float(raw["retail"]),
                min_discount=(
                    _pct(raw["min_discount"], "min_discount") if "min_discount" in raw else settings.min_discount
                ),
                suspicious_below=(
                    _pct(raw["suspicious_below"], "suspicious_below")
                    if "suspicious_below" in raw
                    else settings.suspicious_below
                ),
                status_ids=tuple(raw.get("status_ids", settings.status_ids)),
                brands=_str_tuple(raw.get("brands", []), "brands"),
                require=_str_tuple(raw.get("require", []), "require"),
                exclude=_str_tuple(raw.get("exclude", []), "exclude"),
            )
        except WatchlistError as exc:
            raise WatchlistError(f"{where}: {exc}") from None
        if item.suspicious_below >= 1 - item.min_discount:
            raise WatchlistError(f"{where}: suspicious_below musi być niższe niż 100 - min_discount")
        items.append(item)

    if not items:
        raise WatchlistError(f"{path}: brak pozycji [[item]]")
    return settings, items
