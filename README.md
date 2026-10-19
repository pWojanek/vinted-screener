# vinted-screener

Screener okazji na Vinted. Pokazuje rzeczy wystawione **dużo poniżej ceny sklepowej** (domyślnie
co najmniej 50% taniej), w stanie nowym z metką, nowym albo bardzo dobrym. Przy każdej okazji
widać, **skąd jest cena odniesienia**.

Działa w dwóch trybach:

- **Po markach (główny, na stronie `serve`).** Wybierasz marki, np. Nike, Carhartt WIP czy Ralph Lauren.
  Screener bierze najnowsze ogłoszenia tych marek i sam szuka ceny. **Za darmo, bez AI.** Klucz API
  Claude jest opcjonalny i daje dokładniejsze ceny dla rzeczy, których nie ma na Ceneo.
- **Po modelach z `watchlist.toml`.** Ręczna lista 47 modeli z orientacyjnymi cenami. W trybie marek
  ta lista też służy jako jedno ze źródeł.

## Skąd jest cena (tryb marek)

Dla każdego ogłoszenia screener najpierw ustala, jaki to produkt, czyli wyciąga z tytułu słowa modelu.
Np. z „Nike Dunk Low Retro Panda 42 nowe” robi „Nike Dunk Low Retro”, a numery modeli typu 550, 1460
czy XT-6 zostają. Potem bierze pierwsze dostępne źródło ceny, w tej kolejności:

| Źródło | Etykieta na karcie | Uwagi |
|---|---|---|
| AI z wyszukiwarką (opcjonalne) | „retail wg nike.com ↗” | Cena regularna ze sklepu. Link sprawdzony w wynikach wyszukiwania. |
| **Ceneo.pl** | „wg Ceneo ↗ · najniższa cena w sklepach” | Najtańsza oferta nowego produktu. Przyjmowana tylko, gdy nazwa na Ceneo zawiera wszystkie słowa modelu. |
| Watchlista | „retail wg watchlisty (orientacyjnie)” | Ręczne ceny z `watchlist.toml`. |
| **Opis sprzedającego** | „wg sprzedającego: „Cena w sklepie 349 zł”” | Cytat z opisu ogłoszenia. Działa także dla ogólnych tytułów typu „Bluza Nike”. |
| Szacunek AI (opcjonalny) | „⚠ retail to szacunek AI” | Gdy AI rozpoznało produkt, ale nie znalazło ceny. |
| **Mediana Vinted** | „⚠ brak ceny retail — porównanie z: mediana Vinted (7 ofert)” | Ostatnia deska ratunku, gdy jest min. 6 podobnych ofert. To cena rynkowa, nie sklepowa. |

Pomarańczowe etykiety oznaczają cenę przybliżoną. W wynikach jest filtr **„Źródło ceny”**:
„ze sklepów (Ceneo, AI)” albo „bez szacunków i median”.

Żeby nie zasypać serwisów zapytaniami, Ceneo dostaje najwyżej 25 nowych zapytań na markę, a opisów
pobiera się najwyżej 10 na markę. W pierwszej kolejności idą nowe rzeczy i te najtańsze względem
podobnych. Wyniki są pamiętane w `.vinted-screener/state.json`: Ceneo 7 dni, opisy 14 dni. Kolejne skany
są więc szybsze i obejmują więcej produktów.

Rabat liczony jest od pełnej ceny dla kupującego (cena + ochrona kupujących, bez wysyłki). Wynik 0–100
łączy rabat od ceny odniesienia (65%) i rabat względem mediany podobnych ofert na Vinted (35%),
pomnożone przez stan. Ceny poniżej 20–40% odniesienia (zależnie od tego, jak często marka jest
podrabiana) są ukryte jako podejrzane. Repliki, same pudełka, rzeczy dziecięce, zamiany i uszkodzone
rzeczy odpadają zawsze.

## Instalacja

Wymagany Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Zależności to `curl_cffi` (udaje Chrome, bo zwykłe `requests` Vinted i Ceneo blokują)
i `anthropic` (używany tylko, gdy ustawisz klucz API).

### Klucz API (opcjonalnie)

Bez klucza wszystko działa za darmo na Ceneo, opisach sprzedających, watchliście i medianie Vinted.
Z kluczem API Anthropic dochodzi AI. Rozpoznaje produkty także z nietypowych tytułów i szuka ceny
regularnej w oficjalnych sklepach. Klucz utworzysz na https://console.anthropic.com (Settings → API Keys),
a płaci się za użycie.

```powershell
# Windows PowerShell — tylko w tym oknie
$env:ANTHROPIC_API_KEY = "sk-ant-..."
# albo na stałe (zadziała w nowo otwartym oknie)
setx ANTHROPIC_API_KEY "sk-ant-..."
```

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # Linux / macOS
```

**Koszt AI.** Rozpoznanie 40 tytułów to ok. $0,05, a sprawdzenie ceny jednego produktu w sieci
ok. $0,05–0,10. Pierwszy skan 3–5 marek to zwykle $1–3, kolejne są tańsze dzięki pamięci.
Każdy skan ma limit kosztu (domyślnie $2, do zmiany na stronie albo `max_ai_cost` w `watchlist.toml`).
Po jego osiągnięciu reszta okazji dostaje etykietę „szacunek AI”. Strona i terminal pokazują
faktyczny koszt skanu.

Model to Claude Opus 5.5. W rzadkim przypadku odmowy odpowiedzi API samo przełącza się na model
zapasowy (opcja `fallbacks="default"`).

## Użycie

### Strona w przeglądarce (najwygodniej)

```bash
python -m vinted_screener serve
```

Otwiera się strona `http://127.0.0.1:8765`.

1. Klikasz marki albo dopisujesz własną w polu „inna marka”.
2. Opcjonalnie zawężasz wyszukiwanie: w polu „Tylko rzeczy” wpisujesz np. „bluza”, możesz też ustawić
   rozmiary, rabat i źródła cen (Ceneo, opisy sprzedających, limit kosztu AI).
3. Klikasz **Skanuj**.

Okazje pojawiają się na bieżąco, a kliknięcie karty otwiera ogłoszenie na Vinted. Pierwszy skan
jednej marki trwa ok. 1–2 minuty (zapytania do Ceneo i opisy), kolejne są szybsze dzięki pamięci. Strona zapamiętuje Twój wybór. Serwer działa tylko lokalnie
i kończysz go przez Ctrl+C w terminalu.

### Terminal

```bash
vinted-screener scan --brand nike --brand "carhartt wip"   # tryb marek → terminal + okazje.html
vinted-screener scan --brand nike --no-seller --debug      # bez opisów; pokaż zapytania do Vinted i Ceneo
vinted-screener scan --brand "ralph lauren" --query sweter --max-ai-cost 1
vinted-screener scan                         # tryb modeli z watchlisty (bez AI)
vinted-screener scan --size 43 --size L      # tylko Twoje rozmiary
vinted-screener scan --category kurtki --min-discount 60
vinted-screener watch --brand nike --interval 15   # co ~15 min, wypisuje tylko NOWE okazje
vinted-screener list                         # modele i marki z watchlisty
```

Na Windowsie z Pythonem ze Sklepu Microsoft komenda `vinted-screener` często nie jest w PATH,
wtedy używaj `python -m vinted_screener`.

Raport `okazje.html` (zapisywany też po każdym skanie w `serve`) otwierasz w przeglądarce.
`--json plik.json` zapisuje wyniki do dalszej obróbki.

### Powiadomienia na Discordzie

```bash
export VINTED_DISCORD_WEBHOOK="https://discord.com/api/webhooks/…"
vinted-screener watch --brand nike --size 43
```

Pierwszy cykl `watch` tylko zapamiętuje okazje, które już wiszą. Powiadomienia przychodzą
o nowych. Żeby dostać też obecne, dodaj `--notify-existing`.

## Dostosowanie

Wszystko jest w `watchlist.toml`, a każde pole ma opis w komentarzach. Marki do wyboru na stronie
to sekcje `[[brand]]`. Dla krótkich nazw ustaw `query` (np. On → `on running`), a dla marek
często podrabianych wyższy `suspicious_below`. Modele z cenami to sekcje `[[item]]`.
W `[settings]` ustawisz próg rabatu, stany, liczbę stron na markę (`brand_pages`) i limit kosztu AI.

## Ograniczenia

- **Rozpoznanie z tytułu jest przybliżone.** Bez AI tytuł „Nike Air Max” (bez numeru) da ogólny
  produkt. Ceneo zwraca najniższą cenę nowego produktu, więc może to być cena promocyjna albo innej
  kolorystyki. Dlatego przy każdej cenie jest źródło z linkiem, do sprawdzenia jednym kliknięciem.
- **Ceneo i Vinted mogą zmienić stronę albo zablokować zapytania.** Ceneo wtedy wyłącza się do końca
  skanu z komunikatem, a skan działa dalej na pozostałych źródłach. `--debug` pokazuje, co zwróciło Ceneo.
- **API Vinted jest nieoficjalne.** Może się zmienić albo zacząć blokować (HTTP 403/429).
  Nie zmniejszaj `delay` i nie skanuj częściej niż co kilka minut. Przy błędach uruchom
  `scan --only dunk --debug`, wtedy widać zapytania i początek odpowiedzi Vinted.
- **Rozpoznanie działa tylko po tytule i marce,** bo opis ogłoszenia nie przychodzi w wynikach
  wyszukiwania. Zawsze obejrzyj zdjęcia, opinie sprzedawcy i metki.

## Testy

```bash
pip install -e '.[dev]'
pytest
```

Testy nie łączą się z Vinted, Ceneo ani Claude: wszystkie klienty są podmieniane.
