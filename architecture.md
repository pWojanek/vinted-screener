# Architektura

```
watchlist.toml ──► watchlist.py ──► WatchItem ([[item]]: model + retail) i Brand ([[brand]])

Tryb marek (serve, scan --brand):  brand_screener.py — dla każdej marki:
   1. client.search(marka [+ fraza], stany, brand_pages)             → ogłoszenia marki
   2. parse_listings + Brand.matches (marka, globalne wykluczenia) + size_matches
   3. szacunek: WatchItem.matches → cena z watchlisty, reszta → retail.RetailFinder.identify (AI, partiami)
   4. kandydaci (cena ≤ szacunek × (1 − rabat) × 1,25) → RetailFinder.lookup_many (AI + web_search)
   5. Deal(model, kategoria, retail, RetailSource, rabat, mediana tego produktu z tego skanu, score)

Tryb modeli (scan, watch):  screener.py — dla każdego WatchItem:
   1. client.search(query, okno cenowe, stany) → 2. parse_listings + WatchItem.matches
   3. market_median (osobne zapytanie, cache) → 4. Deal(..., RetailSource "watchlista")

Oba tryby: screener.run_scan (postęp, zatrzymanie, przerwanie po błędach Vinted) → ScanResult
           → report.py (terminal / JSON / HTML) · notify.py (Discord) · server.py (status na żywo)
```

| Moduł | Odpowiedzialność |
|---|---|
| `client.py` | HTTP do Vinted. Wyszukiwanie to `https://api.<domena>/svc-catalogue/items` (od 09.2026; stare `www…/api/v2/catalog/items` daje 404). Tożsamość ze strony głównej: ciasteczko `access_token_web` + nagłówek `X-Anon-Id` (+ `<html lang>` jako `Locale`). Token wysyłany jawnie w `Cookie`, słoik curl_cffi czyszczony, żeby nie było dwóch nagłówków. Throttling, retry 429/5xx, odświeżenie sesji przy 401/403, paginacja, `--debug`. Jedyne miejsce, które zna URL-e API. |
| `retail.py` | AI (Claude Opus 5.5, `client.beta.messages.create` z `fallbacks="default"`). `identify`: partie po 40 tytułów, odpowiedź w JSON (`output_config.format`, krótkie klucze `i/p/c/r/q`), cache 14 dni. `lookup`: `web_search_20260209` (max 4, lokalizacja PL) + narzędzie `report_retail_price` (`strict`), obsługa `pause_turn`, cache 30 dni (brak ceny 7 dni). `verify_source` przyjmuje link tylko wtedy, gdy ten adres albo domena była w wynikach wyszukiwania. `CostMeter` liczy USD z `usage`. Limit `max_usd` na skan. Zły klucz daje `RetailAIError`, a inne błędy SDK zwracają `None` dla jednego produktu. |
| `brand_screener.py` | Tryb marek, kroki w schemacie wyżej. `_retail` wybiera źródło: sieć, potem watchlista, potem szacunek AI z powodem. Po `RetailAIError` wyłącza AI i jedzie dalej. |
| `models.py` | `Listing.from_api`: parsuje oba formaty ceny (obiekt i string). Markę, stan i rozmiar bierze z `item_box.accessibility_label` (od 09.2026 API nie zwraca `brand_title`/`status`/`size_title`), a stary kształt nadal obsługuje. Do tego `Deal` (model, kategoria, retail i `RetailSource`: skąd jest cena), `normalize()` (bez polskich znaków i interpunkcji) i wagi stanów. |
| `watchlist.py` | Wczytanie i walidacja TOML (`load_watchlist`, `load_brands`), DSL fraz (`a\|b`, `prefix*`, sklejanie słów), `brand_in` (dopasowanie marki z prefiksem), `globally_excluded` (repliki, pudełka, dzieci…), `size_matches`, `Brand.custom` dla marek wpisanych na stronie. |
| `screener.py` | Logika okazji: okno cenowe, filtry, mediana rynkowa, `deal_score`. Liczy lejek odrzuceń (`ScanResult.funnel`, pokazywany w podsumowaniu) i w `--debug` wypisuje pola oraz przykłady surowych ogłoszeń. Klient wstrzykiwany, więc testy go podmieniają. |
| `state.py` | `.vinted-screener/state.json`: widziane ID (tryb watch, TTL 14 dni) i kubełki cache z TTL (`market`, `identify`, `retail`), bezpieczne wątkowo. |
| `report.py` | Wypisywanie w terminalu, JSON i `render_page()`, czyli `page.html` z wstrzykniętym `BOOT`. |
| `page.html` | Jedna strona w dwóch trybach. `{"mode": "static", deals, meta}` to raport `okazje.html`, a `{"mode": "app"}` to panel wyboru marek w `serve` (chipy, własne marki, fraza, limit kosztu AI), który odpytuje `/api/status` co sekundę. Karta ma osobny link do ogłoszenia i linię „retail wg …” z linkiem do źródła; zagnieżdżony `<a>` byłby niepoprawny. Wybór i opcje pamięta `localStorage`. |
| `server.py` | `serve`: `ThreadingHTTPServer` tylko na 127.0.0.1. `ScanService` puszcza jeden skan marek naraz w wątku, z nowym `RetailFinder` na każdy skan (limit kosztu z formularza), i aktualizuje stan po każdej marce, razem z kosztem AI. Endpointy: `GET /api/watchlist` (marki, czy AI włączone), `GET /api/status`, `POST /api/scan` (`brands`, `query`, `sizes`, `min_discount`, `max_ai_cost`, `show_suspicious`), `POST /api/stop`. Obce nagłówki `Host` dostają 403, a POST bez JSON dostaje 415 (ochrona przed DNS rebinding i CSRF). |
| `notify.py` | Payloady Discorda (maks. 10 embedów na wiadomość). |

## Zasady

- **Cena do porównań to `total_item_price`,** czyli cena z ochroną kupujących. Okno cenowe w zapytaniu
  działa na cenie bez opłaty, dlatego po pobraniu jest jeszcze filtr na pełnej cenie.
- **Mediana rynkowa** to mediana najnowszych pasujących ofert bez podejrzanie tanich. Potrzeba co najmniej 5 próbek,
  inaczej `None`. Liczona tylko wtedy, gdy model ma jakichś kandydatów, co oszczędza zapytania.
- **Stan filtrowany dwa razy:** `attribute_ids[status]` w zapytaniu i jeszcze raz po tekście stanu (`condition_status_id`),
  gdyby API zignorowało filtr.
- **Link do ogłoszenia budujemy z ID** (`<site_url>/items/<id>`), bo pole `url` z API prowadziło w złe miejsce.
- **Źródło ceny jest zawsze jawne:** `RetailSource.kind` to `web` (link sprawdzony w wynikach wyszukiwania), `watchlist` albo `estimate` (szacunek AI z `note` z powodem). Linku spoza wyników wyszukiwania nie pokazujemy.
- **Koszt AI jest ograniczony:** AI dostaje tylko to, czego nie ma w cache. Najpierw tanie rozpoznanie partiami, wyszukiwarka tylko dla kandydatów, a po przekroczeniu `max_usd` nie ma nowych wywołań.
- **Brak sieci w testach:** `tests/conftest.py::raw_item` odwzorowuje kształt odpowiedzi API Vinted, a `tests/test_retail.py::FakeAnthropic` udaje klienta Claude (`text_response`, `search_response`).
