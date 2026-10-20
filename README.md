# vinted-screener

Screener okazji na Vinted. Pokazuje rzeczy wystawione **dużo poniżej ceny sklepowej** (domyślnie
co najmniej 50% taniej), w stanie nowym z metką, nowym albo bardzo dobrym. Przy każdej okazji
widać, **skąd jest cena odniesienia**.

Działa w dwóch trybach:

- **Wyszukiwarka (główny, na stronie `serve`).** Wpisujesz, czego szukasz: markę („Carhartt WIP”),
  typ rzeczy („kurtka puchowa”) albo jedno i drugie („Nike bluza”). Screener bierze najnowsze ogłoszenia
  dla tej frazy i sam szuka ceny sklepowej. **Za darmo, bez AI.** Klucz API Claude jest opcjonalny i daje
  dokładniejsze ceny dla rzeczy, których nie ma na Ceneo.
- **Po modelach z `watchlist.toml`.** Ręczna lista 47 modeli z orientacyjnymi cenami. W wyszukiwarce
  ta lista też służy jako jedno ze źródeł.

## Jak działa wyszukiwarka

- **Fraza z marką** („Nike bluza”, „north face nuptse”, „TNF”) bierze tylko ogłoszenia tej marki.
  Marka jest rozpoznawana z listy `[[brand]]` w `watchlist.toml`, także w innej pisowni („cp company”,
  „levis”, „Polo Ralph Lauren”). Fraza może mieć dwie marki: „Nike Air Jordan 1” bierze Nike i Jordan.
- **Sam typ rzeczy** („kurtka puchowa”, „sweter”) bierze ogłoszenia różnych marek. Domyślnie tylko
  **znanych marek** z listy, bo dla no-name'ów nie ma ceny sklepowej. Na stronie możesz to wyłączyć
  (`--any-brand` w terminalu). Marka spoza listy wpisana we frazę („Zara sukienka”) też przechodzi.
- **Kilka fraz naraz** oddzielasz przecinkiem: „Nike bluza, kurtka puchowa, Carhartt WIP”. Najwyżej 10.

Na stronie pole podpowiada marki i typy rzeczy, a pod nim są Twoje ostatnie frazy do kliknięcia.
W trakcie skanu widać, jak każda fraza została zrozumiana, np. „kurtka puchowa” → znane marki.

## Skąd jest cena

Dla każdego ogłoszenia screener najpierw ustala, jaki to produkt, czyli wyciąga z tytułu słowa modelu.
Np. z „Nike Dunk Low Retro Panda 42 nowe” robi „Nike Dunk Low Retro”, a numery modeli typu 550, 1460
czy XT-6 zostają. Potem bierze pierwsze dostępne źródło ceny, w tej kolejności:

| Źródło | Etykieta na karcie | Uwagi |
|---|---|---|
| AI z wyszukiwarką (opcjonalne) | „retail wg nike.com ↗” | Cena regularna ze sklepu. Link sprawdzony w wynikach wyszukiwania. |
| **Sklepy: oficjalny sklep marki, potem Zalando** | „retail wg nike.com ↗ · cena regularna, teraz w promocji” | Cena regularna, czyli sprzed promocji. Najpierw pytany jest oficjalny sklep marki (nike.com, adidas.pl), a gdy go nie ma albo nic nie znajdzie, Zalando. Lista sklepów jest w `watchlist.toml` (`[[shop]]`). |
| **Ceneo.pl** | „wg Ceneo ↗ · najniższa cena w sklepach” | Gdy sklepy nic nie znalazły. Najtańsza oferta nowego produktu. Przyjmowana tylko, gdy nazwa na Ceneo zawiera wszystkie słowa modelu. |
| Watchlista | „retail wg watchlisty (orientacyjnie)” | Ręczne ceny z `watchlist.toml`. |
| **Opis sprzedającego** | „wg sprzedającego: „Cena w sklepie 349 zł”” | Cytat z opisu ogłoszenia. Działa także dla ogólnych tytułów typu „Bluza Nike”. |
| Szacunek AI (opcjonalny) | „⚠ retail to szacunek AI” | Gdy AI rozpoznało produkt, ale nie znalazło ceny. |
| **Mediana Vinted** | „⚠ brak ceny retail — porównanie z: mediana Vinted (7 ofert)” | Ostatnia deska ratunku, gdy jest min. 6 podobnych ofert. To cena rynkowa, nie sklepowa. |

Pomarańczowe etykiety oznaczają cenę przybliżoną. W wynikach jest filtr **„Źródło ceny”**:
„ze sklepów (sklepy, Ceneo, AI)” albo „bez szacunków i median”.

**Jak screener czyta sklepy.** Wpisuje nazwę produktu w wyszukiwarkę sklepu i czyta dane produktów zapisane
w stronie wyników (nazwa, cena regularna, cena promocyjna, link). Gdy strona nie ma takich danych (Zalando),
czyta kafelki produktów: nazwę i ceny, a przekreśloną cenę pierwotną bierze jako regularną. Bierze produkt, którego nazwa zawiera
wszystkie słowa modelu. Gdy pasuje kilka, bierze najniższą cenę regularną, bo różne kolorystyki kosztują
różnie, a niższa cena nie zawyży rabatu. Rzeczy dziecięce odpadają. Słowo kolorystyki z Vinted
(„Dunk Low Panda”) może zostać pominięte, a numer modelu („Air Max 90”) nie.

**Własny sklep.** W `watchlist.toml` dopisz sekcję `[[shop]]`. Wyszukaj coś w sklepie, skopiuj adres
z paska przeglądarki i w miejsce wpisanej frazy wstaw `{q}`. Dla oficjalnego sklepu podaj też `brands`.
Przykład z wymyślonym adresem:

```toml
[[shop]]
name = "sklep-marki.pl"
search_url = "https://www.sklep-marki.pl/szukaj?q={q}"   # tu adres skopiowany z przeglądarki
brands = ["Carhartt WIP"]
```

Czy sklep działa, sprawdzisz komendą `scan --search "carhartt wip detroit" --debug`. Pokaże, ile produktów
odczytano i czy z danych (JSON), czy z kafelków (HTML). Przy zerze pokaże też budowę strony.

Żeby nie zasypać serwisów zapytaniami, sklepy dostają razem najwyżej 25 nowych zapytań na frazę, Ceneo też
25, a opisów pobiera się najwyżej 10 na frazę. W pierwszej kolejności idą nowe rzeczy i te najtańsze
względem podobnych. Wyniki są pamiętane w `.vinted-screener/state.json`: sklepy i Ceneo 7 dni, opisy
14 dni. Kolejne skany są więc szybsze i obejmują więcej produktów.

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

Zależności to `curl_cffi` (udaje Chrome, bo zwykłe `requests` Vinted, sklepy i Ceneo blokują)
i `anthropic` (używany tylko, gdy ustawisz klucz API).

### Klucz API (opcjonalnie)

Bez klucza wszystko działa za darmo na sklepach, Ceneo, opisach sprzedających, watchliście i medianie Vinted.
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
ok. $0,05–0,10. Pierwszy skan 3–5 fraz to zwykle $1–3, kolejne są tańsze dzięki pamięci.
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

1. Wpisujesz, czego szukasz, np. „Nike bluza” albo „kurtka puchowa, Carhartt WIP”.
2. Opcjonalnie ustawiasz rozmiary, rabat, źródła cen (sklepy, Ceneo, opisy sprzedających, limit kosztu AI)
   i to, czy przy samym typie rzeczy brać tylko znane marki. Min. rabat „dowolny” pokazuje wszystko, co ma
   cenę odniesienia, także rzeczy droższe od niej. Te mają pomarańczową plakietkę „+12%” i 0 pkt.
3. Klikasz **Szukaj okazji** albo wciskasz Enter.

Okazje pojawiają się na bieżąco, a kliknięcie karty otwiera ogłoszenie na Vinted. Pole „Filtruj wyniki”
nad kartami zawęża już znalezione okazje i nie wysyła nowych zapytań. Pierwszy skan jednej frazy trwa
ok. 1–2 minuty (zapytania do sklepów, Ceneo i opisy), kolejne są szybsze dzięki pamięci. Strona zapamiętuje
ostatnie frazy i opcje. Serwer działa tylko lokalnie i kończysz go przez Ctrl+C w terminalu.

### Terminal

```bash
vinted-screener scan --search "nike bluza" --search "carhartt wip"   # wyszukiwarka → terminal + okazje.html
vinted-screener scan --search "kurtka puchowa" --size L               # sam typ rzeczy, znane marki
vinted-screener scan --search "sukienka" --any-brand                  # sam typ rzeczy, każda marka
vinted-screener scan --search nike --no-seller --debug     # bez opisów; pokaż zapytania do Vinted, sklepów i Ceneo
vinted-screener scan --search nike --no-shops --no-ceneo   # bez sklepów i Ceneo (opisy, watchlista, mediana)
vinted-screener scan --search "ralph lauren sweter" --max-ai-cost 1
vinted-screener scan                         # tryb modeli z watchlisty (bez AI)
vinted-screener scan --size 43 --size L      # tylko Twoje rozmiary
vinted-screener scan --category kurtki --min-discount 60
vinted-screener scan --search "kurtka puchowa" --any-discount   # bez progu rabatu, z porównaniem do ceny
vinted-screener watch --search "nike bluza" --interval 15   # co ~15 min, wypisuje tylko NOWE okazje
vinted-screener list                         # modele z watchlisty, znane marki i sklepy
```

Na Windowsie z Pythonem ze Sklepu Microsoft komenda `vinted-screener` często nie jest w PATH,
wtedy używaj `python -m vinted_screener`.

Raport `okazje.html` (zapisywany też po każdym skanie w `serve`) otwierasz w przeglądarce.
`--json plik.json` zapisuje wyniki do dalszej obróbki.

### Powiadomienia na Discordzie

```bash
export VINTED_DISCORD_WEBHOOK="https://discord.com/api/webhooks/…"
vinted-screener watch --search "nike dunk" --size 43
```

Pierwszy cykl `watch` tylko zapamiętuje okazje, które już wiszą. Powiadomienia przychodzą
o nowych. Żeby dostać też obecne, dodaj `--notify-existing`.

## Dostosowanie

Wszystko jest w `watchlist.toml`, a każde pole ma opis w komentarzach. Znane marki, które wyszukiwarka
rozpoznaje we frazie i podpowiada, to sekcje `[[brand]]`. Inne nazwy marki dopisujesz w `aliases`
(np. The North Face → „North Face”, „TNF”). Dla krótkich nazw ustaw `query` (np. On → `on running`),
a dla marek często podrabianych wyższy `suspicious_below`. Modele z cenami to sekcje `[[item]]`.
Sklepy z cenami regularnymi to sekcje `[[shop]]` (opis wyżej, w „Skąd jest cena”).
W `[settings]` ustawisz próg rabatu, stany, liczbę stron na frazę (`search_pages`), limity zapytań do sklepów
i Ceneo oraz limit kosztu AI.

## Ograniczenia

- **Rozpoznanie z tytułu jest przybliżone.** Bez AI tytuł „Nike Air Max” (bez numeru) da ogólny
  produkt. Ceneo zwraca najniższą cenę nowego produktu, więc może to być cena promocyjna albo innej
  kolorystyki. Dlatego przy każdej cenie jest źródło z linkiem, do sprawdzenia jednym kliknięciem.
- **Krótka nazwa marki w środku frazy nie jest rozpoznawana.** „on bluza” to zwykłe słowa, więc
  wpisz „On Running bluza”. Samo „On” działa.
- **Sklepy, Ceneo i Vinted mogą zmienić stronę albo zablokować zapytania.** Zalando, nike.com i adidas.pl
  mają zabezpieczenia przed botami, więc bywa, że zwrócą HTTP 403. Taki serwis wyłącza się do końca skanu
  z komunikatem, a skan działa dalej na pozostałych źródłach. `--debug` pokazuje, co zwrócił każdy serwis.
- **Cena ze sklepu to najniższa cena regularna pasującego produktu.** Przy ogólnej nazwie („Nike Air Max”)
  może to być tańszy model z tej linii. Na karcie widać, jaki produkt został dopasowany, z linkiem.
- **API Vinted jest nieoficjalne.** Może się zmienić albo zacząć blokować (HTTP 403/429).
  Nie zmniejszaj `delay` i nie skanuj częściej niż co kilka minut. Przy błędach uruchom
  `scan --search "nike dunk" --debug`, wtedy widać zapytania i początek odpowiedzi Vinted.
- **Rozpoznanie działa tylko po tytule i marce,** bo opis ogłoszenia nie przychodzi w wynikach
  wyszukiwania. Zawsze obejrzyj zdjęcia, opinie sprzedawcy i metki.

## Testy

```bash
pip install -e '.[dev]'
pytest
```

Testy nie łączą się z Vinted, sklepami, Ceneo ani Claude: wszystkie klienty są podmieniane, a próba wyjścia
do sieci kończy test błędem (`tests/conftest.py::no_network`).
