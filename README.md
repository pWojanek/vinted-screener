# vinted-screener

Screener okazji na Vinted. Bierze listę fajnych modeli z cenami retail (`watchlist.toml`),
przegląda najnowsze ogłoszenia i pokazuje tylko te wystawione **dużo poniżej ceny sklepowej**.
Domyślny próg to co najmniej 50% taniej, a stan nowy z metką, nowy albo bardzo dobry.

Na start watchlista ma 47 modeli w 7 kategoriach: sneakersy (Dunki, Jordany, Samby, New Balance,
Salomony…), buty, kurtki (Nuptse, Arc'teryx, Barbour…), bluzy i swetry (Carhartt WIP, Stüssy,
Ralph Lauren, Stone Island, C.P. Company, Tech Fleece…), koszulki polo, spodnie i akcesoria.

## Co robi z każdym ogłoszeniem

1. **Szuka** na Vinted (`api.vinted.pl/svc-catalogue/items`, od najnowszych) w oknie cenowym
   od „podejrzanie tanio” do „retail − próg rabatu”.
2. **Dopasowuje** ogłoszenie do modelu. Marka musi się zgadzać, a tytuł musi zawierać wymagane
   frazy (np. `dunk` + `low`, bez `high`). To odsiewa „Inna marka”, wersje „a'la” i inne modele.
3. **Odrzuca śmieci:** repliki, same pudełka i sznurówki, rzeczy dziecięce, zamiany i uszkodzone.
4. **Liczy rabat** od retailu na pełnej cenie dla kupującego (cena + ochrona kupujących, bez wysyłki).
5. **Porównuje z rynkiem,** czyli z medianą cen najnowszych podobnych ofert na Vinted.
   Rzecz 60% poniżej retailu, ale droższa niż typowa oferta na Vinted, nie jest prawdziwą okazją.
6. **Wystawia wynik 0–100:** 65% rabat od retailu, 35% rabat względem mediany Vinted,
   pomnożone przez stan (nowy z metką 1.0, nowy 0.9, bardzo dobry 0.75).
7. **Flaguje podejrzane:** ogłoszenia tańsze niż 20–35% retailu (zależnie od tego, jak często
   marka jest podrabiana) są domyślnie ukryte jako możliwa podróbka albo scam.

## Instalacja

Wymagany Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Jedyna zależność to `curl_cffi`. Podszywa się pod Chrome, bo zwykłe `requests` Vinted często blokuje.

## Użycie

### Strona w przeglądarce (najwygodniej)

```bash
python -m vinted_screener serve
```

Otwiera się strona `http://127.0.0.1:8765`. Klikasz modele albo całe kategorie, które chcesz
skanować, ustawiasz rozmiary i minimalny rabat, a potem klikasz **Skanuj**. Okazje pojawiają
się na bieżąco, a kliknięcie karty otwiera ogłoszenie na Vinted. Strona zapamiętuje Twój wybór.
Serwer działa tylko lokalnie i kończysz go przez Ctrl+C w terminalu.

### Terminal

```bash
vinted-screener list                         # modele z watchlisty i progi cenowe
vinted-screener scan                         # pełny skan → terminal + okazje.html
vinted-screener scan --size 43 --size L      # tylko Twoje rozmiary
vinted-screener scan --category kurtki --min-discount 60
vinted-screener scan --only jordan --only dunk --sort newest
vinted-screener scan --min-market-discount 25   # tylko ≥25% taniej niż mediana na Vinted
vinted-screener scan --show-suspicious       # pokaż też podejrzanie tanie
vinted-screener watch --interval 10          # co ~10 min, wypisuje tylko NOWE okazje
```

Zamiast `vinted-screener` można też uruchomić `python -m vinted_screener`. Na Windowsie z Pythonem
ze Sklepu Microsoft komenda `vinted-screener` często nie jest w PATH, wtedy używaj `python -m vinted_screener`.

Raport `okazje.html` otwierasz w przeglądarce. Ma zdjęcia, filtry (kategoria, model, rabat,
szukajka) i sortowanie. `--json plik.json` zapisuje wyniki do dalszej obróbki.

### Powiadomienia na Discordzie

```bash
export VINTED_DISCORD_WEBHOOK="https://discord.com/api/webhooks/…"
vinted-screener watch --size 43
```

Pierwszy cykl `watch` tylko zapamiętuje okazje, które już wiszą. Powiadomienia przychodzą
o nowych. Żeby dostać też obecne, dodaj `--notify-existing`.

## Dostosowanie

Wszystko jest w `watchlist.toml`. Każde pole ma opis w komentarzu na górze pliku. Dodanie modelu wygląda tak:

```toml
[[item]]
name = "Nike Air Max 95"
category = "Sneakersy"
query = "nike air max 95"
retail = 849
brands = ["Nike"]
require = ["air max 95|am95"]
suspicious_below = 25
```

W `[settings]` ustawisz domyślne rozmiary (`sizes = ["43", "L"]`), próg rabatu, stany i tempo zapytań.

## Ograniczenia

- **Ceny retail są orientacyjne.** To ceny regularne PLN dla popularnych kolorystyk, a limitowane
  kolorystyki kosztują więcej. Popraw je, jeśli znasz aktualne.
- **API Vinted jest nieoficjalne.** Może się zmienić albo zacząć blokować (HTTP 403/429).
  Nie zmniejszaj `delay` i nie skanuj częściej niż co kilka minut. Przy błędach uruchom
  `scan --only dunk --debug`, wtedy widać zapytania i początek odpowiedzi Vinted.
- **Dopasowanie działa tylko po tytule i marce,** bo opis ogłoszenia nie przychodzi w wynikach
  wyszukiwania. Zawsze obejrzyj zdjęcia, opinie sprzedawcy i metki. Niska cena przy markach
  takich jak Stone Island, Nuptse czy Jordan to częsty sygnał podróbki.
- Marki podrabiane masowo (Moncler, Canada Goose) są celowo pominięte.

## Testy

```bash
pip install -e '.[dev]'
pytest
```
