# Architektura

```
watchlist.toml ──► watchlist.py ──► WatchItem ([[item]]: model + retail) i Brand ([[brand]]: znane marki)

Wyszukiwarka (serve, scan --search):  search_screener.py — dla każdej frazy:
   0. Search.parse: znane marki we frazie (find_brands) → zapytanie Vinted (vinted_query) i zakres marek
   1. client.search(Search.query, stany, search_pages)                → ogłoszenia
   2. parse_listings + akceptacja: marka z frazy (Brand.matches) albo, przy samym typie rzeczy, znana marka
      / marka wpisana we frazę / każda (only_known_brands=False) + globalne wykluczenia + size_matches
   3. produkt: marka ogłoszenia (_brand_of); WatchItem.matches → watchlista; jest AI → RetailFinder.identify;
      inaczej products.product_tokens (słowa modelu z tytułu); grupy (marka, kategoria, 2 słowa) → mediana Vinted
   4. ceny: AI web (kandydaci) → shops.ShopPrices (oficjalny sklep marki, potem Zalando; limit) →
      ceneo.CeneoPrices (produkty bez ceny, limit) → opisy sprzedających
      (client.item_description + products.declared_retail, limit) → watchlista / szacunek AI / mediana
   5. Deal(model, kategoria, retail, RetailSource, rabat, mediana grupy, score)

Tryb modeli (scan, watch):  screener.py — dla każdego WatchItem:
   1. client.search(query, okno cenowe, stany) → 2. parse_listings + WatchItem.matches
   3. market_median (osobne zapytanie, cache) → 4. Deal(..., RetailSource "watchlista")

Oba tryby: screener.run_scan (postęp, zatrzymanie, przerwanie po błędach Vinted) → ScanResult
           → report.py (terminal / JSON / HTML) · notify.py (Discord) · server.py (status na żywo)
```

| Moduł | Odpowiedzialność |
|---|---|
| `client.py` | HTTP do Vinted, a także wspólne `browser_session()` i `Throttle` dla innych serwisów. `item_description` czyta `og:description` ze strony ogłoszenia. Wyszukiwanie to `https://api.<domena>/svc-catalogue/items` (od 09.2026; stare `www…/api/v2/catalog/items` daje 404). Tożsamość ze strony głównej: ciasteczko `access_token_web` + nagłówek `X-Anon-Id` (+ `<html lang>` jako `Locale`). Token wysyłany jawnie w `Cookie`, słoik curl_cffi czyszczony, żeby nie było dwóch nagłówków. Throttling, retry 429/5xx, odświeżenie sesji przy 401/403, paginacja, `--debug`. Jedyne miejsce, które zna URL-e API. |
| `retail.py` | AI (Claude Opus 5.5, `client.beta.messages.create` z `fallbacks="default"`). `identify`: partie po 40 tytułów, odpowiedź w JSON (`output_config.format`, krótkie klucze `i/p/c/r/q`), cache 14 dni. `lookup`: `web_search_20260209` (max 4, lokalizacja PL) + narzędzie `report_retail_price` (`strict`), obsługa `pause_turn`, cache 30 dni (brak ceny 7 dni). `verify_source` przyjmuje link tylko wtedy, gdy ten adres albo domena była w wynikach wyszukiwania. `CostMeter` liczy USD z `usage`. Limit `max_usd` na skan. Zły klucz daje `RetailAIError`, a inne błędy SDK zwracają `None` dla jednego produktu. |
| `search_screener.py` | Wyszukiwarka, kroki w schemacie wyżej. `Search.parse` rozpoznaje znane marki we frazie po nazwie i `aliases`, porównując bez spacji i interpunkcji („cp company” = „C.P. Company”). Krótkie nazwy (≤2 znaki, „On”) liczą się tylko jako cała fraza. Przy nakładaniu wygrywa dłuższa nazwa, a kilka różnych marek („Nike Air Jordan 1”) wchodzi razem. Nazwa marki w zapytaniu zmienia się w `Brand.query`, chyba że dłuższa nazwa ją zawiera („Air Jordan”). Marka ogłoszenia ustala nazwę produktu, zapytanie Ceneo i próg podejrzanej ceny. `_reference` wybiera źródło: AI-web, potem Ceneo, watchlista, opis sprzedającego, szacunek AI, a na końcu mediana Vinted (min. 6 ofert). Limity zapytań na frazę są w ustawieniach. Po `RetailAIError` wyłącza AI, a po blokadzie Ceneo wyłącza Ceneo, i w obu przypadkach jedzie dalej. |
| `products.py` | Bez AI: `contains_all` (wspólne dopasowanie słów modelu dla Ceneo i sklepów), `product_tokens` (słowa modelu bez marki, rozmiarów, kolorów, stanu, wypełniaczy i cech typu „puchowa”, „z kapturem”; numery modeli zostają), `is_specific`, `guess_category`, `declared_retail` („cena w sklepie 499 zł” → kwota + cytat, tylko wyraźnie powyżej ceny oferty). |
| `shops.py` | Sklepy z `[[shop]]` (`watchlist.Shop`: adres wyszukiwarki z `{q}`, `brands` dla oficjalnego sklepu, `currency`, `link_base`). Bez parsera na sklep: `parse_products` czyta każdy JSON osadzony w stronie wyników (`__NEXT_DATA__`, `window.__hydrationDataConsume(...)`, `JSON.parse("...")`, JSON-LD, odpowiedź API) i bierze obiekty z nazwą i ceną. Gdy w JSON-ie nic nie ma (Zalando), `parse_tile_products` czyta kafelki z HTML bez nazw klas: dzieli stronę po linkach do produktów (`.html`, `/p/`, `/t/`), a z kawałka bierze tekst i ceny w zł; najwyższa to cena regularna. Przy zerze produktów `--debug` wypisuje budowę strony (największe skrypty, linki, tekst przy cenie). Cena regularna (`fullPrice`, `initialPrice`, `original`, `compare_at_price`…, a przy `salePrice` także `price`) ma pierwszeństwo. Całkowite `amount` bez tekstu jest pomijane, bo bywa w groszach. `ShopPrices`: najpierw oficjalny sklep marki, potem wielomarkowe, ścisłe dopasowanie słów modelu (jedno słowo kolorystyki można pominąć, numeru modelu nie), marka musi się zgadzać, dziecięce odpadają, najniższa cena regularna. Cache 7 dni / brak 3 dni (kubełek `shop`), wyłączenie sklepu po 401/403/429. |
| `ceneo.py` | `CeneoClient.search` (`/;szukaj-…`, curl_cffi, `Throttle`), `parse_search` (najpierw JSON-LD, potem kafelki `data-productid` + `value`/`penny`), `CeneoPrices` (ścisłe dopasowanie słów modelu, cache 7 dni / brak 3 dni, wyłączenie po 403/429). Jedyne miejsce, które zna adresy Ceneo. |
| `models.py` | `Listing.from_api`: parsuje oba formaty ceny (obiekt i string). Markę, stan i rozmiar bierze z `item_box.accessibility_label` (od 09.2026 API nie zwraca `brand_title`/`status`/`size_title`), a stary kształt nadal obsługuje. Do tego `Deal` (model, kategoria, retail i `RetailSource`: skąd jest cena), `normalize()` (bez polskich znaków i interpunkcji) i wagi stanów. |
| `watchlist.py` | Wczytanie i walidacja TOML (`load_watchlist`, `load_brands`), DSL fraz (`a\|b`, `prefix*`, sklejanie słów), `brand_in` (dopasowanie marki z prefiksem), `globally_excluded` (repliki, pudełka, dzieci…), `size_matches`, `Brand.owns` (czy marka ogłoszenia to ta marka), `Brand.custom` dla marek spoza listy. Stare nazwy ustawień (`brand_pages`, `max_*_per_brand`) nadal są czytane. |
| `screener.py` | Logika okazji: okno cenowe, filtry, mediana rynkowa, `deal_score`. Liczy lejek odrzuceń (`ScanResult.funnel`, pokazywany w podsumowaniu) i w `--debug` wypisuje pola oraz przykłady surowych ogłoszeń. Klient wstrzykiwany, więc testy go podmieniają. |
| `state.py` | `.vinted-screener/state.json`: widziane ID (tryb watch, TTL 14 dni) i kubełki cache z TTL (`market`, `identify`, `retail`, `shop`, `ceneo`, `description`), bezpieczne wątkowo. |
| `report.py` | Wypisywanie w terminalu, JSON i `render_page()`, czyli `page.html` z wstrzykniętym `BOOT`. |
| `page.html` | Jedna strona w dwóch trybach. `{"mode": "static", deals, meta}` to raport `okazje.html`, a `{"mode": "app"}` to wyszukiwarka w `serve`: pole z podpowiedziami (`<datalist>` liczony dla frazy po ostatnim przecinku, z marek, typów i „marka + typ”), ostatnie frazy jako chipy i opcje. Strona odpytuje `/api/status` co sekundę i pokazuje, jak każda fraza została zrozumiana. Karta ma osobny link do ogłoszenia i linię „retail wg …” z linkiem do źródła; zagnieżdżony `<a>` byłby niepoprawny. Frazy, ostatnie wyszukiwania i opcje pamięta `localStorage`. |
| `server.py` | `serve`: `ThreadingHTTPServer` tylko na 127.0.0.1. `ScanService` puszcza jeden skan naraz w wątku, z nowym `RetailFinder` na każdy skan (limit kosztu z formularza), i aktualizuje stan po każdej frazie, razem z kosztem AI. Endpointy: `GET /api/config` (znane marki i typy do podpowiedzi, sklepy, czy AI włączone), `GET /api/status` (z `searches`: jak zrozumiano frazy), `POST /api/scan` (`queries` — najwyżej 10 po 80 znaków, `only_known_brands`, `sizes`, `min_discount` — procent albo `"any"` = `screener.NO_MIN_DISCOUNT`, `max_ai_cost`, `show_suspicious`, `shops`, `ceneo`, `seller`), `POST /api/stop`. Obce nagłówki `Host` dostają 403, a POST bez JSON dostaje 415 (ochrona przed DNS rebinding i CSRF). |
| `notify.py` | Payloady Discorda (maks. 10 embedów na wiadomość). |

## Zasady

- **Cena do porównań to `total_item_price`,** czyli cena z ochroną kupujących. Okno cenowe w zapytaniu
  działa na cenie bez opłaty, dlatego po pobraniu jest jeszcze filtr na pełnej cenie.
- **Mediana rynkowa** to mediana najnowszych pasujących ofert bez podejrzanie tanich. Potrzeba co najmniej 5 próbek,
  inaczej `None`. Liczona tylko wtedy, gdy model ma jakichś kandydatów, co oszczędza zapytania.
- **Stan filtrowany dwa razy:** `attribute_ids[status]` w zapytaniu i jeszcze raz po tekście stanu (`condition_status_id`),
  gdyby API zignorowało filtr.
- **Link do ogłoszenia budujemy z ID** (`<site_url>/items/<id>`), bo pole `url` z API prowadziło w złe miejsce.
- **Źródło ceny jest zawsze jawne:** `RetailSource.kind` to `web`, `shop`, `ceneo`, `watchlist`, `seller`, `estimate` albo `market`. `estimate` i `market` to przybliżenia: na stronie są pomarańczowe, z `~` przy cenie. Linku spoza wyników wyszukiwania AI nie pokazujemy.
- **Darmowe źródła są domyślne:** AI to dodatek. Bez `ANTHROPIC_API_KEY` wyszukiwarka działa na sklepach, Ceneo, opisach, watchliście i medianie.
- **Cena ze sklepu jest ostrożna:** regularna (nie promocyjna) i najniższa z pasujących produktów, żeby nie zawyżać rabatu.
- **Sam typ rzeczy bierze domyślnie tylko znane marki:** bez marki nie ma ceny sklepowej, a no-name'y zasypałyby wyniki medianami. Ogłoszenia bez marki odpadają zawsze.
- **Koszt AI jest ograniczony:** AI dostaje tylko to, czego nie ma w cache. Najpierw tanie rozpoznanie partiami, wyszukiwarka tylko dla kandydatów, a po przekroczeniu `max_usd` nie ma nowych wywołań.
- **Brak sieci w testach:** `tests/conftest.py::raw_item` odwzorowuje kształt odpowiedzi API Vinted, `tests/test_shops.py` ma strony w kształcie Nike, Zalando, adidas, JSON-LD i Shopify, a `tests/test_retail.py::FakeAnthropic` udaje klienta Claude (`text_response`, `search_response`). Autouse `no_network` podmienia `browser_session`, więc test, który próbuje wyjść do sieci, kończy się błędem.
