"""Tryb serve: lokalna strona, na której wybiera się modele do skanowania i ogląda wyniki na żywo."""

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

from vinted_screener.client import VintedClient
from vinted_screener.report import deal_to_dict, render_page, sort_deals, write_html
from vinted_screener.screener import Filters, ScanResult, Screener, describe_funnel
from vinted_screener.state import State
from vinted_screener.watchlist import Settings, WatchItem

MAX_BODY = 64 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class ScanService:
    """Trzyma watchlistę, klienta Vinted i stan bieżącego skanu (jeden skan naraz)."""

    def __init__(
        self,
        settings: Settings,
        items: list[WatchItem],
        client: VintedClient,
        state: State,
        html_path: str | None = None,
        log: Callable[[str], None] = lambda msg: None,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.items = {i.name: i for i in items}
        self.client = client
        self.state = state
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
            "running": False, "done": 0, "total": 0, "current": None, "deals": [], "errors": {},
            "funnel": "", "listings_seen": 0, "started_at": None, "finished_at": None,
        }

    def watchlist(self) -> dict[str, Any]:
        return {
            "currency": self.settings.currency,
            "seconds_per_request": round(self.settings.delay * 1.25 + 0.5, 2),
            "items": [
                {"name": i.name, "category": i.category, "retail": i.retail, "max_price": round(i.max_price)}
                for i in self.items.values()
            ],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._job, "deals": list(self._job["deals"]), "errors": dict(self._job["errors"])}

    def start(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        names = request.get("items")
        if not isinstance(names, list) or not names:
            return 400, {"error": "wybierz co najmniej jeden model"}
        unknown = [n for n in names if n not in self.items]
        if unknown:
            return 400, {"error": f"nieznane modele: {', '.join(map(str, unknown))}"}
        min_discount = request.get("min_discount")
        if min_discount is not None and (not isinstance(min_discount, (int, float)) or not 0 <= min_discount < 100):
            return 400, {"error": "min_discount musi być procentem 0-99"}
        sizes = request.get("sizes") or []
        if not isinstance(sizes, list) or not all(isinstance(s, str) for s in sizes):
            return 400, {"error": "sizes musi być listą tekstów"}

        filters = Filters(
            min_discount=None if min_discount is None else min_discount / 100,
            sizes=tuple(sizes),
            show_suspicious=bool(request.get("show_suspicious")),
        )
        items = [self.items[n] for n in names]
        with self._lock:
            if self._job["running"]:
                return 409, {"error": "skan już trwa"}
            self._job = {**self._empty_job(), "running": True, "total": len(items), "current": items[0].name,
                         "started_at": datetime.now(timezone.utc).isoformat()}
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(items, filters, bool(request.get("use_market", True))), daemon=True
        )
        self._thread.start()
        return 202, {"started": len(items)}

    def stop(self) -> None:
        self._stop.set()

    def _run(self, items: list[WatchItem], filters: Filters, use_market: bool) -> None:
        screener = Screener(
            self.client, self.settings, filters=filters, state=self.state, use_market=use_market, log=self.log, debug=self.debug
        )

        def on_item(i: int, item: WatchItem, result: ScanResult) -> None:
            snapshot = {
                "done": i,
                "current": items[i].name if i < len(items) else None,
                "deals": [deal_to_dict(d) for d in result.deals],
                "errors": dict(result.errors),
                "funnel": describe_funnel(result.funnel),
                "listings_seen": result.listings_seen,
            }
            with self._lock:
                self._job.update(snapshot)

        result = None
        try:
            result = screener.screen(items, on_item=on_item, should_stop=self._stop.is_set)
            self.state.save()
            if self.html_path:
                meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "scanned_items": result.scanned_items,
                        "listings_seen": result.listings_seen, "errors": len(result.errors)}
                write_html(sort_deals(result.deals, "score"), self.html_path, meta)
        except Exception as exc:  # granica wątku: błąd ma trafić na stronę, a nie zniknąć po cichu
            traceback.print_exc(file=sys.stderr)
            with self._lock:
                self._job["errors"] = {**self._job["errors"], "(błąd)": f"{type(exc).__name__}: {exc}"}
        finally:
            with self._lock:
                if result is not None:
                    self._job["errors"] = dict(result.errors)
                    self._job["deals"] = [deal_to_dict(d) for d in result.deals]
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
            elif path == "/api/watchlist":
                self._json(200, service.watchlist())
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
