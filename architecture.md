# Architektura

```
watchlist.toml ──► watchlist.py ──► WatchItem (retail, marki, frazy require/exclude, progi)
                                        │
cli.py (scan/watch/list) ──► screener.py ── dla każdego WatchItem:
                                        │   1. client.search(query, okno cenowe, stany)       → kandydaci
                                        │   2. Listing.from_api + WatchItem.matches + size_matches
                                        │   3. market_median (osobne zapytanie bez okna cen, cache w State)
                                        │   4. Deal(rabat od retailu, rabat od mediany, score, suspicious)
                                        ▼
                        report.py (terminal / JSON / HTML)   notify.py (Discord webhook, tryb watch)
```

| Moduł | Odpowiedzialność |
|---|---|
| `client.py` | HTTP do Vinted. Wyszukiwanie to `https://api.<domena>/svc-catalogue/items` (od 09.2026; stare `www…/api/v2/catalog/items` daje 404). Tożsamość ze strony głównej: ciasteczko `access_token_web` + nagłówek `X-Anon-Id` (+ `<html lang>` jako `Locale`). Token wysyłany jawnie w `Cookie`, słoik curl_cffi czyszczony, żeby nie było dwóch nagłówków. Throttling, retry 429/5xx, odświeżenie sesji przy 401/403, paginacja, `--debug`. Jedyne miejsce, które zna URL-e API. |
| `models.py` | `Listing.from_api`: parsuje oba formaty ceny (obiekt i string). Markę, stan i rozmiar bierze z `item_box.accessibility_label` (od 09.2026 API nie zwraca `brand_title`/`status`/`size_title`), a stary kształt nadal obsługuje. Do tego `Deal`, `normalize()` (bez polskich znaków i interpunkcji) i wagi stanów. |
| `watchlist.py` | Wczytanie i walidacja TOML, DSL fraz (`a\|b`, `prefix*`, sklejanie słów), dopasowanie marki, globalne wykluczenia (repliki, pudełka, dzieci…), `size_matches`. |
| `screener.py` | Logika okazji: okno cenowe, filtry, mediana rynkowa, `deal_score`. Liczy lejek odrzuceń (`ScanResult.funnel`, pokazywany w podsumowaniu) i w `--debug` wypisuje pola oraz przykłady surowych ogłoszeń. Klient wstrzykiwany, więc testy go podmieniają. |
| `state.py` | `.vinted-screener/state.json`: widziane ID (tryb watch, TTL 14 dni) i cache median (TTL z ustawień). |
| `report.py` | Wypisywanie w terminalu, JSON i samodzielny HTML (dane jako JSON w `<script>`, renderowane w JS). |
| `notify.py` | Payloady Discorda (maks. 10 embedów na wiadomość). |

## Zasady

- **Cena do porównań to `total_item_price`,** czyli cena z ochroną kupujących. Okno cenowe w zapytaniu
  działa na cenie bez opłaty, dlatego po pobraniu jest jeszcze filtr na pełnej cenie.
- **Mediana rynkowa** to mediana najnowszych pasujących ofert bez podejrzanie tanich. Potrzeba co najmniej 5 próbek,
  inaczej `None`. Liczona tylko wtedy, gdy model ma jakichś kandydatów, co oszczędza zapytania.
- **Stan filtrowany dwa razy:** `attribute_ids[status]` w zapytaniu i jeszcze raz po tekście stanu (`condition_status_id`),
  gdyby API zignorowało filtr.
- **Brak sieci w testach:** `tests/conftest.py::raw_item` odwzorowuje kształt odpowiedzi API.
