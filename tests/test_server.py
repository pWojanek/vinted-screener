from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from conftest import make_item, raw_item

from vinted_screener.server import ScanService, make_handler
from vinted_screener.state import State
from vinted_screener.watchlist import Settings


class FakeClient:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.calls: list[dict] = []

    def search(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        if self.gate:
            self.gate.wait(5)
        if kwargs.get("price_to") is None:
            return []
        return [raw_item(f"{text} okazja", 150, id=hash(text) & 0xFFFFFF)]


@pytest.fixture
def server(tmp_path):
    def start(client=None):
        items = [
            make_item(name="Nike Dunk Low", query="nike dunk low"),
            make_item(name="Dunk 2", category="Inne", query="nike dunk low 2"),
        ]
        service = ScanService(Settings(delay=0), items, client or FakeClient(), State(tmp_path / "s.json"),
                              html_path=str(tmp_path / "okazje.html"))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", service

    started: list = []
    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def call(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def wait_finished(base):
    for _ in range(100):
        status = json.loads(call(base + "/api/status")[1])
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError("skan się nie skończył")


def test_page_and_watchlist(server):
    base, _ = server()
    code, page = call(base + "/")
    assert code == 200 and '"mode": "app"' in page
    code, body = call(base + "/api/watchlist")
    data = json.loads(body)
    assert [i["name"] for i in data["items"]] == ["Nike Dunk Low", "Dunk 2"]
    assert data["items"][0]["max_price"] == 250 and data["currency"] == "PLN"


def test_scan_selected_items_in_background(server, tmp_path):
    base, _ = server()
    code, body = call(base + "/api/scan", {"items": ["Nike Dunk Low"], "sizes": ["42"], "min_discount": 60,
                                          "use_market": False, "show_suspicious": False})
    assert code == 202, body
    status = wait_finished(base)
    assert status["done"] == 1 and status["total"] == 1
    assert [d["model"] for d in status["deals"]] == ["Nike Dunk Low"]
    assert status["deals"][0]["url"].startswith("https://www.vinted.pl/items/")
    assert status["finished_at"] and (tmp_path / "okazje.html").exists()


def test_scan_passes_options_to_screener(server):
    client = FakeClient()
    base, _ = server(client)
    call(base + "/api/scan", {"items": ["Nike Dunk Low"], "min_discount": 60, "use_market": False})
    wait_finished(base)
    assert len(client.calls) == 1, "bez porównania z rynkiem tylko jedno zapytanie"
    assert client.calls[0]["price_to"] == pytest.approx(200)


def test_second_scan_is_rejected_while_running_and_stop_works(server):
    gate = threading.Event()
    base, _ = server(FakeClient(gate))
    assert call(base + "/api/scan", {"items": ["Nike Dunk Low", "Dunk 2"], "use_market": False})[0] == 202
    code, body = call(base + "/api/scan", {"items": ["Nike Dunk Low"]})
    assert code == 409 and "trwa" in body
    assert call(base + "/api/stop", {})[0] == 200
    gate.set()
    status = wait_finished(base)
    assert status["done"] == 1 and "(przerwano)" in status["errors"]


@pytest.mark.parametrize(
    "body, error",
    [({"items": []}, "co najmniej jeden"), ({"items": ["Nieznany"]}, "nieznane"),
     ({"items": ["Nike Dunk Low"], "min_discount": 150}, "min_discount"),
     ({"items": ["Nike Dunk Low"], "sizes": "42"}, "sizes")],
)
def test_scan_validation(server, body, error):
    base, _ = server()
    code, text = call(base + "/api/scan", body)
    assert code == 400 and error in text


def test_rejects_foreign_host_and_non_json_post(server):
    base, _ = server()
    assert call(base + "/api/status", headers={"Host": "evil.example"})[0] == 403
    req = urllib.request.Request(base + "/api/scan", data=b"items=x", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 415
