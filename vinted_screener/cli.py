"""Interfejs wiersza poleceń: serve / scan / watch / list."""

from __future__ import annotations

import argparse
import dataclasses
import os
import random
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from vinted_screener.brand_screener import BrandScreener
from vinted_screener.ceneo import CeneoClient, CeneoPrices
from vinted_screener.client import VintedClient
from vinted_screener.models import normalize
from vinted_screener.notify import send_discord
from vinted_screener.report import SORT_KEYS, enable_windows_ansi, money, print_deals, sort_deals, write_html, write_json
from vinted_screener.screener import Filters, ScanResult, Screener, describe_funnel
from vinted_screener.retail import RetailFinder, make_anthropic_client
from vinted_screener.server import ScanService, serve
from vinted_screener.state import State
from vinted_screener.watchlist import Brand, Settings, WatchItem, WatchlistError, load_brands, load_watchlist


NO_AI_NOTE = "brak ANTHROPIC_API_KEY — AI wyłączone; ceny z Ceneo, opisów sprzedających i watchlisty (patrz README)"


def _percent(value: str) -> float:
    pct = float(value)
    if not 0 <= pct < 100:
        raise argparse.ArgumentTypeError("podaj procent z zakresu 0-99")
    return pct / 100


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-w", "--watchlist", default="watchlist.toml", help="plik z listą modeli (domyślnie: %(default)s)")
    common.add_argument("--only", action="append", default=[], metavar="TEKST", help="tylko modele, których nazwa zawiera TEKST (można powtarzać)")
    common.add_argument("--category", action="append", default=[], metavar="KAT", help="tylko wybrane kategorie (można powtarzać)")

    scan = argparse.ArgumentParser(add_help=False, parents=[common])
    scan.add_argument("--min-discount", type=_percent, metavar="PROC", help="minimalny rabat od retailu w %% (nadpisuje watchlistę)")
    scan.add_argument("--min-market-discount", type=_percent, metavar="PROC", help="minimalny rabat względem mediany cen na Vinted w %%")
    scan.add_argument("--max-price", type=float, metavar="KWOTA", help="maksymalna cena")
    scan.add_argument("--size", action="append", default=[], metavar="ROZM", help="rozmiar, np. 42 albo M (można powtarzać)")
    scan.add_argument("--pages", type=int, help="ile stron (po 96 ogłoszeń) przeglądać na model")
    scan.add_argument("--no-market", action="store_true", help="nie licz mediany cen na Vinted (o połowę mniej zapytań)")
    scan.add_argument("--show-suspicious", action="store_true", help="pokaż też podejrzanie tanie ogłoszenia")
    scan.add_argument("--sort", choices=sorted(SORT_KEYS), default="score")
    scan.add_argument("--limit", type=int, default=40, help="ile okazji wypisać w terminalu (domyślnie: %(default)s)")
    scan.add_argument("--html", default="okazje.html", metavar="PLIK", help="raport HTML (domyślnie: %(default)s, pusty = bez raportu)")
    scan.add_argument("--json", metavar="PLIK", help="zapisz okazje do JSON")
    scan.add_argument("--state", default=".vinted-screener/state.json", metavar="PLIK", help="plik stanu (cache median, widziane oferty)")
    scan.add_argument("-q", "--quiet", action="store_true", help="bez postępu na stderr")
    scan.add_argument("--debug", action="store_true", help="wypisuj zapytania do Vinted i początek odpowiedzi")
    scan.add_argument("--brand", action="append", default=[], metavar="MARKA",
                      help="tryb marek: skanuj wszystkie rzeczy marki, cenę retail ustala AI (można powtarzać)")
    scan.add_argument("--query", default="", metavar="FRAZA", help="tryb marek: dopisz frazę do wyszukiwania, np. bluza")
    scan.add_argument("--max-ai-cost", type=float, metavar="USD", help="tryb marek: limit kosztu AI na skan (domyślnie z watchlisty)")
    scan.add_argument("--no-ceneo", action="store_true", help="tryb marek: nie szukaj cen na Ceneo.pl")
    scan.add_argument("--no-seller", action="store_true", help="tryb marek: nie czytaj cen z opisów sprzedających")

    parser = argparse.ArgumentParser(
        prog="vinted-screener", description="Szuka na Vinted fajnych rzeczy wystawionych dużo poniżej ceny retail."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", parents=[scan], help="jednorazowe skanowanie")
    watch = sub.add_parser("watch", parents=[scan], help="skanuj co N minut i powiadamiaj o nowych okazjach")
    watch.add_argument("--interval", type=float, default=10, metavar="MIN", help="co ile minut skanować (domyślnie: %(default)s)")
    watch.add_argument("--webhook", default=os.environ.get("VINTED_DISCORD_WEBHOOK"), help="URL webhooka Discorda (lub zmienna VINTED_DISCORD_WEBHOOK)")
    watch.add_argument("--notify-existing", action="store_true", help="przy pierwszym skanie wyślij też okazje, które już wiszą")
    sub.add_parser("list", parents=[common], help="pokaż modele z watchlisty i progi cenowe")
    serve = sub.add_parser("serve", parents=[common], help="strona w przeglądarce: wybierz, co skanować, i oglądaj wyniki na żywo")
    serve.add_argument("--port", type=int, default=8765, help="port lokalnego serwera (domyślnie: %(default)s)")
    serve.add_argument("--no-browser", action="store_true", help="nie otwieraj przeglądarki automatycznie")
    serve.add_argument("--html", default="okazje.html", metavar="PLIK", help="po każdym skanie zapisz też raport statyczny (pusty = nie)")
    serve.add_argument("--state", default=".vinted-screener/state.json", metavar="PLIK", help="plik stanu (cache median)")
    serve.add_argument("-q", "--quiet", action="store_true", help="bez postępu w terminalu")
    serve.add_argument("--debug", action="store_true", help="wypisuj zapytania do Vinted i początek odpowiedzi")
    return parser


def select_items(items: list[WatchItem], only: list[str], categories: list[str]) -> list[WatchItem]:
    only_n = [normalize(o) for o in only]
    cats_n = [normalize(c) for c in categories]
    return [
        i
        for i in items
        if (not only_n or any(o in normalize(i.name) for o in only_n))
        and (not cats_n or any(c in normalize(i.category) for c in cats_n))
    ]


def cmd_list(settings: Settings, items: list[WatchItem], brands: list[Brand]) -> int:
    print(f"{'Model':<34} {'Kategoria':<14} {'Retail':>9} {'Okazja do':>10} {'Podejrz. <':>10}")
    for i in items:
        print(
            f"{i.name:<34.34} {i.category:<14.14} {money(i.retail, settings.currency):>9}"
            f" {money(i.max_price, settings.currency):>10} {money(i.suspicious_price, settings.currency):>10}"
        )
    print(f"\nMarki (tryb marek, --brand): {', '.join(b.name for b in brands)}")
    return 0


def _loggers(args: argparse.Namespace) -> tuple[Callable[[str], None], Callable[[str], None] | None]:
    log = (lambda msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))
    debug = (lambda msg: print(f"  [debug] {msg}", file=sys.stderr, flush=True)) if args.debug else None
    return log, debug


def _make_runner(
    args: argparse.Namespace, settings: Settings, items: list[WatchItem], brands: list[Brand], state: State
) -> Callable[[], ScanResult]:
    """Skan po modelach z watchlisty albo — z --brand — po markach z cenami z AI."""
    if args.pages:
        settings = dataclasses.replace(settings, pages=args.pages, brand_pages=args.pages)
    filters = Filters(
        min_discount=args.min_discount,
        min_market_discount=args.min_market_discount,
        max_price=args.max_price,
        sizes=tuple(args.size),
        show_suspicious=args.show_suspicious,
    )
    log, debug = _loggers(args)
    client = VintedClient(domain=settings.domain, delay=settings.delay, debug=debug)
    if not args.brand:
        screener = Screener(client, settings, filters=filters, state=state, use_market=not args.no_market, log=log, debug=debug)
        return lambda: screener.screen(items)

    known = {normalize(b.name): b for b in brands}
    chosen = [known.get(normalize(name)) or Brand.custom(name, settings) for name in args.brand]
    ai_client = make_anthropic_client()
    if ai_client is None:
        print(NO_AI_NOTE, file=sys.stderr)
    max_cost = settings.max_ai_cost if args.max_ai_cost is None else args.max_ai_cost
    ceneo_client = CeneoClient(delay=settings.delay, debug=debug) if settings.ceneo and not args.no_ceneo else None

    def run_brands() -> ScanResult:
        # nowy licznik kosztu na każdy skan (w trybie watch: na każdy cykl); zapamiętane ceny zostają w State
        finder = RetailFinder(ai_client, state, max_usd=max_cost, debug=debug) if ai_client else None
        screener = BrandScreener(
            client, settings, finder, items, filters=filters, log=log, debug=debug, state=state,
            ceneo=CeneoPrices(ceneo_client, state, debug=debug) if ceneo_client else None,
            read_descriptions=settings.seller_prices and not args.no_seller,
        )
        return screener.screen(chosen, args.query)

    return run_brands


def _write_outputs(args: argparse.Namespace, result: ScanResult) -> None:
    deals = sort_deals(result.deals, args.sort)
    if args.html:
        meta = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scanned_items": result.scanned_items,
            "listings_seen": result.listings_seen,
            "errors": len(result.errors),
        }
        write_html(deals, args.html, meta)
    if args.json:
        write_json(deals, args.json)


def _summary(result: ScanResult, args: argparse.Namespace) -> str:
    text = f"{len(result.deals)} okazji · {result.listings_seen} ogłoszeń · {result.scanned_items} modeli"
    if result.errors:
        text += f" · {len(result.errors)} błędów"
    if result.ai_cost_usd:
        text += f" · AI: ${result.ai_cost_usd:.2f}, {result.ai_searches} wyszukiwań"
    if result.lookups:
        text += f" · Ceneo: {result.lookups['ceneo']} zapytań, opisy: {result.lookups['opisy']}"
    if args.html:
        text += f" · raport: {Path(args.html).resolve()}"
    if funnel := describe_funnel(result.funnel):
        text += f"\nodpadło: {funnel}"
    if result.listings_seen and not result.deals and not args.debug:
        text += "\nbrak okazji — przykładowe ogłoszenia z API pokaże: scan --only <model> --debug"
    return text


def cmd_scan(args: argparse.Namespace, settings: Settings, items: list[WatchItem], brands: list[Brand]) -> int:
    state = State(args.state)
    result = _make_runner(args, settings, items, brands, state)()
    state.save()
    print_deals(sort_deals(result.deals, args.sort)[: args.limit])
    _write_outputs(args, result)
    if args.quiet:  # bez --quiet błędy widać już w postępie
        for name, error in result.errors.items():
            print(f"błąd [{name}]: {error}", file=sys.stderr)
    print(_summary(result, args), file=sys.stderr)
    return 1 if result.errors and not result.scanned_items else 0


def cmd_watch(args: argparse.Namespace, settings: Settings, items: list[WatchItem], brands: list[Brand]) -> int:
    state = State(args.state)
    run = _make_runner(args, settings, items, brands, state)
    first_cycle = not state.seen
    try:
        while True:
            result = run()
            new = [d for d in result.deals if not state.is_seen(d.listing.id)]
            for d in result.deals:
                state.mark_seen(d.listing.id)
            state.save()
            _write_outputs(args, result)

            stamp = datetime.now().strftime("%H:%M")
            if first_cycle:
                print(f"[{stamp}] start: {len(new)} aktualnych okazji — kolejne skany pokażą tylko nowe", file=sys.stderr)
            elif new:
                print(f"[{stamp}] {len(new)} nowych okazji!", file=sys.stderr)
            else:
                print(f"[{stamp}] brak nowych okazji · {_summary(result, args)}", file=sys.stderr)
            print_deals(sort_deals(new, args.sort)[: args.limit])

            if args.webhook and new and (not first_cycle or args.notify_existing):
                try:
                    send_discord(args.webhook, sort_deals(new, args.sort))
                except OSError as exc:
                    print(f"nie udało się wysłać powiadomienia: {exc}", file=sys.stderr)
            first_cycle = False
            time.sleep(args.interval * 60 * random.uniform(0.85, 1.15))
    except KeyboardInterrupt:
        state.save()
        return 0


def cmd_serve(args: argparse.Namespace, settings: Settings, items: list[WatchItem], brands: list[Brand]) -> int:
    log, debug = _loggers(args)
    client = VintedClient(domain=settings.domain, delay=settings.delay, debug=debug)
    ai_client = make_anthropic_client()
    if ai_client is None:
        print(NO_AI_NOTE, file=sys.stderr)
    service = ScanService(
        settings, brands, client, State(args.state), ai_client=ai_client,
        ceneo_client=CeneoClient(delay=settings.delay, debug=debug),
        watch_items=items, html_path=args.html or None, log=log, debug=debug,
    )
    try:
        serve(service, port=args.port, open_browser=not args.no_browser)
    except OSError as exc:
        print(f"nie udało się uruchomić serwera na porcie {args.port}: {exc} (spróbuj --port 8766)", file=sys.stderr)
        return 1
    return 0


def _prepare_console() -> None:
    # Konsola Windows bez tego pokazuje kody kolorów jako śmieci, a przekierowane wyjście
    # w cp1250 wywala się na znakach typu ♥/⚠.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if os.name == "nt":
        enable_windows_ansi()


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    args = build_parser().parse_args(argv)
    try:
        settings, items = load_watchlist(args.watchlist)
        brands = load_brands(args.watchlist, settings)
    except WatchlistError as exc:
        print(f"watchlista: {exc}", file=sys.stderr)
        return 2
    items = select_items(items, args.only, args.category)
    if not items:
        print("żaden model z watchlisty nie pasuje do --only/--category", file=sys.stderr)
        return 2
    if args.command == "list":
        return cmd_list(settings, items, brands)
    if args.command == "scan":
        return cmd_scan(args, settings, items, brands)
    if args.command == "serve":
        return cmd_serve(args, settings, items, brands)
    return cmd_watch(args, settings, items, brands)
