"""Tryb serve: lokalna strona z wyszukiwarką (marka albo typ rzeczy) i wynikami na żywo."""

from __future__ import annotations

import json
import sys
import threading
import traceback
import webbrowser
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from vinted_screener.ceneo import CeneoClient, CeneoPrices
from vinted_screener.client import VintedClient
from vinted_screener.report import deal_to_dict, render_page, sort_deals, write_html
from vinted_screener.retail import MODEL, RetailFinder
from vinted_screener.screener import Filters, ScanResult, describe_funnel
from vinted_screener.search_screener import MAX_SEARCH_LENGTH, TYPE_SUGGESTIONS, Search, SearchScreener, parse_searches
from vinted_screener.shops import ShopClient, ShopPrices
from vinted_screener.state import State
from vinted_screener.watchlist import Brand, Settings, WatchItem

MAX_BODY = 64 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
MAX_SEARCHES = 10
MAX_AI_COST = 50.0  # górna granica limitu z formularza, w USD
# orientacyjny czas na frazę przy pierwszym skanie (potem krócej, bo ceny są zapamiętane):
# strony z Vinted + sklepy + Ceneo + opisy, a z AI dodatkowo rozpoznanie i wyszukiwanie cen
SECONDS_PER_SEARCH_AI, SECONDS_PER_SEARCH_PLAIN = 100, 60


class ScanService:
    """Trzyma znane marki, klienta Vinted, opcjonalnego klienta Claude i stan bieżącego skanu (jeden skan naraz)."""

    def __init__(
        self,
        settings: Settings,
        brands: list[Brand],
        client: VintedClient,
        state: State,
        ai_client: Any = None,
        ceneo_client: CeneoClient | None = None,
        shop_clients: list[ShopClient] = (),
        watch_items: list[WatchItem] = (),
        html_path: str | None = None,
        log: Callable[[str], None] = lambda msg: None,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.brands = list(brands)
        self.client = client
        self.state = state
        self.ai_client = ai_client
        self.ceneo_client = ceneo_client
        self.shop_clients = list(shop_clients)
        self.watch_items = list(watch_items)
        self.html_path = html_path
        self.log = log
        self.debug = debug
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._job: dict[str, Any] = self._empty_job()

    @staticmethod
    def _empty_job() -> dict[str, Any]:
        return {
            "running": False, "done": 0, "total": 0, "current": None, "deals": [], "errors": {}, "funnel": "",
            "listings_seen": 0, "ai_cost_usd": 0.0, "ai_searches": 0, "lookups": {}, "searches": [],
            "started_at": None, "finished_at": None,
        }

    def config(self) -> dict[str, Any]:
        ai = self.ai_client is not None
        return {
            "currency": self.settings.currency,
            "ai_enabled": ai,
            "ai_model": MODEL if ai else None,
            "max_ai_cost": self.settings.max_ai_cost,
            "min_discount": round(self.settings.min_discount * 100),
            "shops": [c.shop.name for c in self.shop_clients],
            "shops_default": self.settings.shops,
            "ceneo_available": self.ceneo_client is not None,
            "ceneo_default": self.settings.ceneo,
            "seller_default": self.settings.seller_prices,
            "seconds_per_search": SECONDS_PER_SEARCH_AI if ai else SECONDS_PER_SEARCH_PLAIN,
            "max_searches": MAX_SEARCHES,
            "brands": [b.name for b in self.brands],
            "types": list(TYPE_SUGGESTIONS),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._job, "deals": list(self._job["deals"]), "errors": dict(self._job["errors"])}

    def start(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        texts = request.get("queries")
        if isinstance(texts, list):
            texts = [t for t in texts if not isinstance(t, str) or t.strip()]
        if not isinstance(texts, list) or not texts:
            return 400, {"error": "wpisz, czego szukać, np. markę albo typ rzeczy"}
        if len(texts) > MAX_SEARCHES or not all(isinstance(t, str) and len(t.strip()) <= MAX_SEARCH_LENGTH for t in texts):
            return 400, {"error": f"najwyżej {MAX_SEARCHES} fraz po {MAX_SEARCH_LENGTH} znaków"}
        min_discount = request.get("min_discount")
        if min_discount is not None and (not isinstance(min_discount, (int, float)) or not 0 <= min_discount < 100):
            return 400, {"error": "min_discount musi być procentem 0-99"}
        sizes = request.get("sizes") or []
        if not isinstance(sizes, list) or not all(isinstance(s, str) for s in sizes):
            return 400, {"error": "sizes musi być listą tekstów"}
        max_cost = request.get("max_ai_cost", self.settings.max_ai_cost)
        if not isinstance(max_cost, (int, float)) or not 0 <= max_cost <= MAX_AI_COST:
            return 400, {"error": f"limit kosztu AI musi być liczbą 0-{MAX_AI_COST:.0f} USD"}

        filters = Filters(
            min_discount=None if min_discount is None else min_discount / 100,
            sizes=tuple(sizes),
            show_suspicious=bool(request.get("show_suspicious")),
        )
        sources = {
            "shops": bool(request.get("shops", self.settings.shops)) and bool(self.shop_clients),
            "ceneo": bool(request.get("ceneo", self.settings.ceneo)) and self.ceneo_client is not None,
            "seller": bool(request.get("seller", self.settings.seller_prices)),
        }
        searches = parse_searches(texts, self.brands, only_known_brands=request.get("only_known_brands", True) is not False)
        if not searches:
            return 400, {"error": "fraza musi zawierać litery albo cyfry"}
        described = [self._describe(s) for s in searches]
        with self._lock:
            if self._job["running"]:
                return 409, {"error": "skan już trwa"}
            self._job = {**self._empty_job(), "running": True, "total": len(searches), "current": searches[0].name,
                         "searches": described, "started_at": datetime.now(timezone.utc).isoformat()}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(searches, filters, float(max_cost), sources), daemon=True)
        self._thread.start()
        return 202, {"started": len(searches), "searches": described}

    @staticmethod
    def _describe(search: Search) -> dict[str, Any]:
        """Jak fraza została zrozumiana — strona pokazuje to przy postępie."""
        return {"text": search.text, "query": search.query, "brands": [b.name for b in search.brands], "scope": search.scope}

    def stop(self) -> None:
        self._stop.set()

    def _snapshot(self, result: ScanResult) -> dict[str, Any]:
        return {
            "deals": [deal_to_dict(d) for d in result.deals],
            "errors": dict(result.errors),
            "funnel": describe_funnel(result.funnel),
            "listings_seen": result.listings_seen,
            "ai_cost_usd": result.ai_cost_usd,
            "ai_searches": result.ai_searches,
            "lookups": dict(result.lookups),
        }

    def _run(self, searches: list[Search], filters: Filters, max_cost: float, sources: dict[str, bool]) -> None:
        finder = RetailFinder(self.ai_client, self.state, max_usd=max_cost, debug=self.debug) if self.ai_client else None
        ceneo = CeneoPrices(self.ceneo_client, self.state, debug=self.debug) if sources["ceneo"] else None
        shops = ShopPrices(self.shop_clients, self.state, debug=self.debug) if sources["shops"] else None
        screener = SearchScreener(
            self.client, self.settings, finder, self.watch_items, filters=filters, log=self.log, debug=self.debug,
            ceneo=ceneo, read_descriptions=sources["seller"], state=self.state, known_brands=self.brands, shops=shops,
        )

        def on_search(i: int, search: Search, result: ScanResult) -> None:
            snapshot = {**self._snapshot(result), "done": i, "current": searches[i].name if i < len(searches) else None}
            with self._lock:
                self._job.update(snapshot)

        result = None
        try:
            result = screener.screen(searches, on_item=on_search, should_stop=self._stop.is_set)
            if self.html_path:
                meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "scanned_items": result.scanned_items,
                        "listings_seen": result.listings_seen, "errors": len(result.errors)}
                write_html(sort_deals(result.deals, "score"), self.html_path, meta)
        except Exception as exc:  # granica wątku: błąd ma trafić na stronę, a nie zniknąć po cichu
            traceback.print_exc(file=sys.stderr)
            with self._lock:
                self._job["errors"] = {**self._job["errors"], "(błąd)": f"{type(exc).__name__}: {exc}"}
        finally:
            self.state.save()  # zapamiętane ceny i rozpoznania przydadzą się także po przerwanym skanie
            with self._lock:
                if result is not None:
                    self._job.update(self._snapshot(result))
                self._job.update(running=False, current=None, finished_at=datetime.now(timezone.utc).isoformat())


def make_handler(service: ScanService) -> type[BaseHTTPRequestHandler]:
    page = render_page({"mode": "app"}).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "vinted-screener"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: dict[str, Any]) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            # chroni przed DNS rebinding — obca strona nie przejmie lokalnego serwera
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host in ALLOWED_HOSTS:
                return True
            self._json(403, {"error": "niedozwolony host"})
            return False

        def do_GET(self) -> None:
            if not self._host_ok():
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/config":
                self._json(200, service.config())
            elif path == "/api/status":
                self._json(200, service.status())
            else:
                self._json(404, {"error": "nie ma takiej strony"})

        def do_POST(self) -> None:
            if not self._host_ok():
                return
            # wymóg JSON wymusza preflight CORS, więc inne strony nie wyślą tu formularza
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(415, {"error": "oczekiwano application/json"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(413, {"error": "za duże zapytanie"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "niepoprawny JSON"})
                return
            path = urlsplit(self.path).path
            if path == "/api/scan" and isinstance(body, dict):
                self._json(*service.start(body))
            elif path == "/api/stop":
                service.stop()
                self._json(200, {"stopping": True})
            else:
                self._json(404, {"error": "nie ma takiej akcji"})

        def log_message(self, format: str, *args: Any) -> None:
            pass  # bez logu każdego zapytania (strona odpytuje status co sekundę)

    return Handler


def serve(service: ScanService, port: int = 8765, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(service))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"vinted-screener działa: {url}  (Ctrl+C kończy)", file=sys.stderr, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        httpd.server_close()
        service.state.save()
