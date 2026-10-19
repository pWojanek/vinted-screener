# vinted-screener

Screener okazji na Vinted (Python 3.11+, CLI + lokalna strona `serve`). Szczegóły modułów są w `architecture.md`, a użycie w `README.md`.

## Komendy

- Instalacja: `pip install -e '.[dev]'`
- Testy: `pytest` (bez sieci, klient Vinted jest podmieniany)
- Uruchomienie: `python -m vinted_screener serve` (strona z wyborem modeli) / `scan` / `watch` / `list`

## Konwencje

- Komunikaty dla użytkownika, README i komentarze są po polsku. Identyfikatory w kodzie są po angielsku.
- Procenty w `watchlist.toml` i CLI to liczby 0–99. Wewnątrz kodu to ułamki 0–1.
- Kwoty są w walucie z `[settings].currency` (domyślnie PLN). Porównania idą na `Listing.total_price`.
- Tylko `client.py` zna adresy API Vinted. Kształt odpowiedzi parsuje tylko `Listing.from_api`.
- Nowy model dodajesz w `watchlist.toml`. Każda pozycja musi mieć `brands`, bo test to wymusza.
- Zmiana fraz dopasowania wymaga testu w `tests/test_watchlist.py` z realistycznym tytułem.

## Uwagi

- API Vinted jest nieoficjalne. W środowisku chmurowym Claude vinted.pl jest zablokowane przez proxy,
  więc zmian w kliencie nie da się tam sprawdzić na żywo. Weryfikacja idzie przez testy na fixture'ach.
- Gdy Vinted zmieni API, punktem odniesienia jest aktualna wersja biblioteki `vinted_scraper` z PyPI
  (pliki `utils/_constants.py`, `utils/_headers.py`). Użytkownik diagnozuje u siebie przez `scan --debug`.
- Ceny retail w `watchlist.toml` są orientacyjne.
