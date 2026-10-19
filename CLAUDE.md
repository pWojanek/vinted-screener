# vinted-screener

Screener okazji na Vinted (Python 3.11+, CLI + lokalna strona `serve`). Szczegóły modułów są w `architecture.md`, a użycie w `README.md`.

## Komendy

- Instalacja: `pip install -e '.[dev]'`
- Testy: `pytest` (bez sieci, klient Vinted jest podmieniany)
- Uruchomienie: `python -m vinted_screener serve` (strona z wyszukiwarką) / `scan [--search FRAZA]` / `watch` / `list`

## Konwencje

- Komunikaty dla użytkownika, README i komentarze są po polsku. Identyfikatory w kodzie są po angielsku.
- Procenty w `watchlist.toml` i CLI to liczby 0–99. Wewnątrz kodu to ułamki 0–1.
- Kwoty są w walucie z `[settings].currency` (domyślnie PLN). Porównania idą na `Listing.total_price`.
- Tylko `client.py` zna adresy Vinted (`Listing.from_api` parsuje kształt odpowiedzi), a tylko `ceneo.py` adresy Ceneo.
- Nowy model dodajesz w `watchlist.toml`. Każda pozycja musi mieć `brands`, bo test to wymusza.
- Nową znaną markę (rozpoznawaną w wyszukiwarce) dodajesz jako `[[brand]]`. Test sprawdza, że „<marka> bluza”
  ją rozpoznaje, a podpowiedzi typów (`TYPE_SUGGESTIONS`) nie udają marek.
- Zmiana fraz dopasowania wymaga testu w `tests/test_watchlist.py` z realistycznym tytułem.

## Uwagi

- API Vinted jest nieoficjalne. W środowisku chmurowym Claude vinted.pl jest zablokowane przez proxy,
  więc zmian w kliencie nie da się tam sprawdzić na żywo. Weryfikacja idzie przez testy na fixture'ach.
- Gdy Vinted zmieni API, punktem odniesienia jest aktualna wersja biblioteki `vinted_scraper` z PyPI
  (pliki `utils/_constants.py`, `utils/_headers.py`). Użytkownik diagnozuje u siebie przez `scan --debug`.
- Ceny retail w `watchlist.toml` są orientacyjne.
- Wyszukiwarka (`search_screener.py`) może używać Claude (`retail.py`, model `claude-opus-5-5`, wybrany przez użytkownika) i to kosztuje.
  Nowe wywołania AI muszą iść przez `RetailFinder._create` (fallback, licznik kosztu, limit) i mieć cache w `State`.
  Każda cena retail ma `RetailSource`; nie pokazujemy ceny bez źródła ani linku spoza wyników wyszukiwania.
- Klucz API: zmienna `ANTHROPIC_API_KEY`. Bez niej wyszukiwarka działa za darmo: Ceneo, opisy sprzedających,
  watchlista i mediana Vinted (`search_screener._reference` ustala kolejność).
- Ceneo jest z chmury Claude zablokowane, więc parser `ceneo.parse_search` pisany jest bez podglądu strony.
  Gdy przestanie działać, użytkownik puszcza `scan --search X --debug`, a debug pokazuje tytuł/fragment strony.
