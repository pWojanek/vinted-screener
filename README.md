# vinted-screener

Screener okazji na Vinted. Pokazuje rzeczy wystawione **dużo poniżej ceny sklepowej** (domyślnie
co najmniej 50% taniej), w stanie nowym z metką, nowym albo bardzo dobrym. Przy każdej okazji
widać, **skąd jest cena retail**.

Działa w dwóch trybach:

- **Po markach (główny, na stronie `serve`).** Wybierasz marki, np. Nike, Carhartt WIP czy Ralph Lauren.
  Screener bierze najnowsze ogłoszenia tych marek, a cenę retail ustala AI (Claude Opus 5.5): rozpoznaje
  produkt z tytułu i szuka jego ceny regularnej w sieci. Nie trzeba wpisywać modeli ani cen.
- **Po modelach z `watchlist.toml`.** Ręczna lista 47 modeli z orientacyjnymi cenami. Jest darmowa
  i działa bez AI. W trybie marek ta lista służy też jako darmowy szacunek i zapasowe źródło ceny.

## Skąd jest cena retail (tryb marek)

1. **Rozpoznanie.** Tytuły ogłoszeń idą do Claude partiami po 40. Dla każdego Claude zwraca nazwę
   produktu (np. „Carhartt WIP Hooded Chase Sweat”), kategorię i szacunek ceny. Ogłoszenia pasujące
   do modelu z watchlisty pomijają ten krok.
2. **Kandydaci.** Dalej idą tylko rzeczy, które według szacunku mogą być okazją. Ogłoszenia
   z ogólnym tytułem (np. „bluza Nike”) odpadają jako „nierozpoznany produkt”.
3. **Cena w sieci.** Dla każdego kandydata Claude szuka ceny regularnej: najpierw w oficjalnym
   sklepie marki, potem u autoryzowanych sprzedawców (Zalando, Answear, Footshop…). Nie bierze cen
   z rynku wtórnego. Podaje adres strony z ceną, a screener sprawdza, że ten adres naprawdę był
   w wynikach wyszukiwania.
4. **Źródło na karcie.** Każda okazja ma jedną z trzech etykiet:
   - **„retail wg nike.com ↗”** to cena znaleziona w sieci, z linkiem do strony,
   - **„retail wg watchlisty (orientacyjnie)”** to cena z `watchlist.toml`, gdy w sieci się nie udało,
   - **„⚠ retail to szacunek AI”** to cena bez źródła (pomarańczowa etykieta), z powodem, np. limit kosztu.

   W wynikach jest filtr **„Źródło ceny: tylko potwierdzone w sieci”**.
5. **Pamięć.** Rozpoznania są pamiętane 14 dni, a ceny produktów 30 dni (`.vinted-screener/state.json`).
   Kolejne skany płacą tylko za nowe ogłoszenia i nowe produkty.

Rabat liczony jest od pełnej ceny dla kupującego (cena + ochrona kupujących, bez wysyłki).
Wynik 0–100 łączy rabat od retailu (65%) i rabat względem mediany tego samego produktu na Vinted
(35%), pomnożone przez stan. Ceny poniżej 20–40% retailu (zależnie od tego, jak często marka jest
podrabiana) są ukryte jako podejrzane. Repliki, same pudełka, rzeczy dziecięce, zamiany i uszkodzone
rzeczy odpadają zawsze.

## Instalacja

Wymagany Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Zależności to `curl_cffi` (udaje Chrome, bo zwykłe `requests` Vinted blokuje) i `anthropic` (Claude).

### Klucz API

Tryb marek potrzebuje klucza API Anthropic. Utworzysz go na https://console.anthropic.com
(Settings → API Keys), a płaci się za użycie. Bez klucza strona działa, ale ceny retail są tylko
dla modeli z watchlisty.

```powershell
# Windows PowerShell — tylko w tym oknie
$env:ANTHROPIC_API_KEY = "sk-ant-..."
# albo na stałe (zadziała w nowo otwartym oknie)
setx ANTHROPIC_API_KEY "sk-ant-..."
```

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # Linux / macOS
```

**Koszt.** Rozpoznanie 40 tytułów to ok. $0,05, a sprawdzenie ceny jednego produktu w sieci
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
2. Opcjonalnie zawężasz wyszukiwanie: w polu „Tylko rzeczy” wpisujesz np. „bluza”, możesz też ustawić rozmiary, rabat i limit kosztu AI.
3. Klikasz **Skanuj**.

Okazje pojawiają się na bieżąco, a kliknięcie karty otwiera ogłoszenie na Vinted. Skan jednej
marki z AI trwa zwykle 1–2 minuty. Strona zapamiętuje Twój wybór. Serwer działa tylko lokalnie
i kończysz go przez Ctrl+C w terminalu.

### Terminal

```bash
vinted-screener scan --brand nike --brand "carhartt wip"   # tryb marek → terminal + okazje.html
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

- **AI może źle rozpoznać produkt** albo trafić na inną kolorystykę z inną ceną. Dlatego przy każdej
  cenie jest źródło, a link pozwala to sprawdzić jednym kliknięciem.
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

Testy nie łączą się ani z Vinted, ani z Claude: oba klienty są podmieniane.
