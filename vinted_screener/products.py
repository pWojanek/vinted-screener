"""Rozpoznawanie produktu bez AI: słowa modelu z tytułu, kategoria i cena retail podana przez sprzedającego."""

from __future__ import annotations

import re
import string

from vinted_screener.models import normalize

# słowa, które nic nie mówią o modelu (stan, zachęty, rozmiarówka, spójniki), po normalize()
STOPWORDS = set(
    """
    nowe nowa nowy nowi nowka nowosc uzywane uzywany uzywana stan idealny idealne idealna bardzo dobry dobra dobre
    jak oryginal oryginalne oryginalny oryginalna orginalne org authentic super tanio okazja hit sale sprzedam
    sprzedaz polecam piekne piekny piekna fajne fajny modne damskie damski damska meskie meski meska unisex
    rozmiar rozm roz size eu us uk cm z ze bez metka metki metkami tag tags w we na do dla i oraz lub a o od po
    tylko raz razy zalozone noszone noszony nienoszone uzyte stylowe limitowane limited edition kolekcja collection
    original men women mens womens woman man kids with the and of in new used size sz para pary sztuki szt
    """.split()
) | set(
    # cechy, które ktoś wpisze w wyszukiwarkę ("kurtka puchowa"), a które nie są nazwą modelu
    """
    puchowa puchowy puchowe zimowa zimowy zimowe przejsciowa przejsciowy przejsciowe letnia letni letnie
    jesienna jesienny wiosenna wiosenny ocieplana ocieplany ocieplane wodoodporna wodoodporny wodoodporne
    skorzana skorzany skorzane jeansowa jeansowy jeansowe dzinsowa dzinsowy pikowana pikowany kapturem kaptur
    rozpinana rozpinany oversize oversized dluga dlugi dlugie krotka krotki krotkie sportowa sportowy sportowe
    biegania bieganie treningowe treningowy codzienne codzienny casual
    swag y2k streetwear vintage hype drip unikat rarytas
    """.split()
)

COLORS = set(
    """
    czarne czarny czarna czarnych biale bialy biala bialych szare szary szara granatowe granatowy granatowa
    niebieskie niebieski niebieska zielone zielony zielona czerwone czerwony czerwona zolte zolty zolta brazowe
    brazowy brazowa bezowe bezowy bezowa rozowe rozowy rozowa fioletowe fioletowy fioletowa pomaranczowe
    pomaranczowy pomaranczowa kremowe kremowy kremowa khaki oliwkowe oliwkowy kolorowe kolor
    black white grey gray navy blue green red yellow brown beige pink purple orange cream olive multicolor
    """.split()
)

SIZE_LETTERS = {"xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl", "2xl", "3xl", "4xl", "os", "onesize"}

# słowo typu rzeczy → kategoria (te same nazwy co w watchliście)
TYPE_WORDS = {
    "buty": "Buty", "botki": "Buty", "kozaki": "Buty", "sandaly": "Buty", "klapki": "Buty", "glany": "Buty",
    "trapery": "Buty", "mokasyny": "Buty", "kapcie": "Buty",
    "sneakersy": "Sneakersy", "sneakers": "Sneakersy", "adidasy": "Sneakersy", "trampki": "Sneakersy", "tenisowki": "Sneakersy",
    "bluza": "Bluzy i swetry", "bluzy": "Bluzy i swetry", "hoodie": "Bluzy i swetry", "sweter": "Bluzy i swetry",
    "sweterek": "Bluzy i swetry", "polar": "Bluzy i swetry", "kardigan": "Bluzy i swetry", "crewneck": "Bluzy i swetry",
    "sweatshirt": "Bluzy i swetry", "golf": "Bluzy i swetry",
    "kurtka": "Kurtki", "kurtki": "Kurtki", "jacket": "Kurtki", "plaszcz": "Kurtki", "kamizelka": "Kurtki",
    "bezrekawnik": "Kurtki", "parka": "Kurtki", "puchowka": "Kurtki", "wiatrowka": "Kurtki", "marynarka": "Kurtki",
    "koszulka": "Koszulki", "tshirt": "Koszulki", "shirt": "Koszulki", "tee": "Koszulki", "polowka": "Koszulki", "koszula": "Koszulki", "top": "Koszulki",
    "spodnie": "Spodnie i legginsy", "jeansy": "Spodnie i legginsy", "dzinsy": "Spodnie i legginsy",
    "legginsy": "Spodnie i legginsy", "szorty": "Spodnie i legginsy", "spodenki": "Spodnie i legginsy",
    "dresy": "Spodnie i legginsy", "joggery": "Spodnie i legginsy", "spodnica": "Spodnie i legginsy",
    "plecak": "Akcesoria", "torba": "Akcesoria", "torebka": "Akcesoria", "nerka": "Akcesoria", "czapka": "Akcesoria",
    "okulary": "Akcesoria", "pasek": "Akcesoria", "portfel": "Akcesoria", "szalik": "Akcesoria", "rekawiczki": "Akcesoria",
    "sukienka": "Inne", "kombinezon": "Inne",
}

# rozmiary zapisane w tytule: "rozm. 42", "42,5", "40 2/3", "W32 L34" — wycinamy przed normalize()
_SIZE_PATTERNS = [
    re.compile(r"\b(?:rozm(?:iar)?|roz|size|eu|us|uk|r)\.?\s*\d{1,2}(?:[.,]\d)?(?:\s*[123]/3)?\b", re.IGNORECASE),
    re.compile(r"\b\d{2}\s*[12]/3\b"),
    re.compile(r"\b\d{2}[.,]5\b"),
    re.compile(r"\bw\s?\d{2}\s*l\s?\d{2}\b", re.IGNORECASE),
]
MIN_MODEL_TOKENS = 2
KEY_TOKENS = 3


def _is_size_number(token: str) -> bool:
    return token.isdigit() and 28 <= int(token) <= 50


def product_tokens(title: str, brand_aliases: tuple[str, ...] = ()) -> list[str]:
    """Słowa opisujące model, w kolejności z tytułu: "Nike Dunk Low Retro Panda 42 nowe" → dunk, low, retro, panda."""
    text = title
    for pattern in _SIZE_PATTERNS:
        text = pattern.sub(" ", text)
    brand_words = {w for alias in brand_aliases for w in normalize(alias).split()}
    tokens = []
    for token in normalize(text).split():
        if (
            token in brand_words
            or token in STOPWORDS
            or token in COLORS
            or token in SIZE_LETTERS
            or token in TYPE_WORDS
            or _is_size_number(token)
            or (len(token) == 1 and not token.isdigit())
            or token in tokens
        ):
            continue
        tokens.append(token)
    return tokens


def is_specific(tokens: list[str]) -> bool:
    """Czy z tytułu da się ustalić model (a nie tylko "bluza Nike"); sam numer modelu ("550", "1460") wystarcza."""
    if len(tokens) >= MIN_MODEL_TOKENS:
        return True
    return len(tokens) == 1 and len(tokens[0]) >= 3 and any(ch.isdigit() for ch in tokens[0])


def contains_all(name: str, tokens: list[str]) -> bool:
    """Czy nazwa produktu ze sklepu zawiera wszystkie słowa modelu (całe słowa, bez polskich znaków)."""
    text = normalize(name)
    return all(re.search(rf"\b{re.escape(t)}\b", text) for t in tokens)


def product_name(brand: str, tokens: list[str]) -> str:
    return f"{brand} {string.capwords(' '.join(tokens[:KEY_TOKENS]))}"


def guess_category(title: str) -> str:
    words = normalize(title).split()
    for word in words:
        if word in TYPE_WORDS:
            return TYPE_WORDS[word]
    return "Inne"


EUR_TO_PLN = 4.3
_DECLARED_RE = re.compile(
    r"(?P<ctx>cen[ay]? (?:regularn\w*|katalogow\w*|sklepow\w*|w sklepie|nowe\w*|oryginaln\w*|detaliczn\w*)|"
    r"w sklepie|sklepowa|retail|regularna cena|kupion\w* za|kupi\w*\s+za|zapłaci\w*|kosztował\w*|kosztuj\w*|kosztuje)"
    r"[^0-9\n]{0,25}?(?P<amount>\d{1,3}(?:[ .\u00a0]\d{3})+(?:,\d{1,2})?|\d{2,5}(?:,\d{1,2})?)\s*"
    r"(?P<cur>zł|zl|pln|złotych|eur|euro|€)",
    re.IGNORECASE,
)


def declared_retail(text: str, listing_price: float) -> tuple[float, str] | None:
    """Cena sklepowa podana w opisie ("cena w sklepie 499 zł", "kupione za 600 zł") i cytat; tylko wyraźnie wyższa niż oferta."""
    best: tuple[float, str] | None = None
    for match in _DECLARED_RE.finditer(text or ""):
        raw = match.group("amount").replace(" ", "").replace("\u00a0", "").replace(".", "").replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            continue
        if match.group("cur").lower() in ("eur", "euro", "€"):
            amount *= EUR_TO_PLN
        if not listing_price * 1.15 <= amount <= 50_000:
            continue  # "kosztuje 150 zł" o samej ofercie albo absurdalna kwota
        quote = " ".join(match.group(0).split())
        if best is None or amount > best[0]:
            best = (round(amount, 2), quote)
    return best
