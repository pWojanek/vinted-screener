from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from conftest import raw_item
from test_retail import FakeAnthropic, search_response, text_response

from vinted_screener.server import ScanService, make_handler
from vinted_screener.state import State
from vinted_screener.watchlist import Brand, Settings

SETTINGS = Settings(delay=0)
BRANDS = [Brand("Nike", "nike", ("Nike",), 0.2), Brand("adidas", "adidas", ("adidas",), 0.2)]
REPORT = {"found": True, "price_pln": 499, "source_url": "https://www.nike.com/pl/t/dunk-low-retro",
          "shop": "nike.com", "matched_product": "Nike Dunk Low Retro", "note": None}


class FakeVinted:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.calls: list[dict] = []

    def search(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        if self.gate:
            self.gate.wait(5)
        if text.startswith("nike"):
            return [raw_item("Nike dunk low panda", 150, id=1, size="42")]
        if text.startswith("adidas"):
            return [raw_item("Adidas samba", 150, id=2, brand="adidas")]
        return [raw_item("Sneakersy Nike Dunk Low", 150, id=3), raw_item("Sneakersy", 50, id=4, brand="Reserved")]

    def item_description(self, item_id):
        self.calls.append({"description": item_id})
        return None


def ai_client():
    identify = text_response({"items": [{"i": 0, "p": "Nike Dunk Low Retro", "c": "Sneakersy", "r": 450, "q": "high"}]})
    return FakeAnthropic(identify, search_response(REPORT))


@pytest.fixture
def server(tmp_path):
    started: list = []

    def start(vinted=None, ai=None):
        service = ScanService(SETTINGS, BRANDS, vinted or FakeVinted(), State(tmp_path / "s.json"), ai_client=ai,
                              html_path=str(tmp_path / "okazje.html"))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", service

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


def test_page_and_search_suggestions(server):
    base, _ = server(ai=ai_client())
    code, page = call(base + "/")
    assert code == 200 and '"mode": "app"' in page and 'id="s-input"' in page
    data = json.loads(call(base + "/api/config")[1])
    assert data["brands"] == ["Nike", "adidas"] and "kurtka puchowa" in data["types"] and data["max_searches"] == 10
    assert data["ai_enabled"] is True and data["ai_model"] == "claude-opus-5-5"
    assert data["max_ai_cost"] == SETTINGS.max_ai_cost and data["min_discount"] == 50


def test_config_says_when_ai_is_off(server):
    base, _ = server()
    data = json.loads(call(base + "/api/config")[1])
    assert data["ai_enabled"] is False and data["ai_model"] is None


def test_search_with_ai_retail_and_source(server, tmp_path):
    vinted = FakeVinted()
    base, _ = server(vinted, ai_client())
    code, body = call(base + "/api/scan", {"queries": ["Nike buty"], "sizes": ["42"], "min_discount": 50, "max_ai_cost": 1.5})
    assert code == 202, body
    assert json.loads(body)["searches"] == [{"text": "Nike buty", "query": "nike buty", "brands": ["Nike"], "scope": "marka Nike"}]
    status = wait_finished(base)
    assert vinted.calls[0]["text"] == "nike buty"
    assert status["done"] == 1 and status["total"] == 1 and status["searches"][0]["scope"] == "marka Nike"
    deal = status["deals"][0]
    assert deal["model"] == "Nike Dunk Low Retro" and deal["retail"] == 499
    assert deal["retail_source"]["kind"] == "web" and deal["retail_source"]["url"].startswith("https://www.nike.com/")
    assert deal["url"] == "https://www.vinted.pl/items/1"
    assert status["ai_cost_usd"] > 0 and status["ai_searches"] == 2
    assert (tmp_path / "okazje.html").exists() and (tmp_path / "s.json").exists()


def test_type_search_takes_only_known_brands_unless_switched_off(server):
    vinted = FakeVinted()
    base, _ = server(vinted)
    assert call(base + "/api/scan", {"queries": ["sneakersy", "SNEAKERSY", " "], "seller": False})[0] == 202
    status = wait_finished(base)
    assert [c["text"] for c in vinted.calls] == ["sneakersy"], "powtórzona fraza idzie raz"
    assert status["searches"][0]["scope"] == "znane marki" and "inny model/marka 1" in status["funnel"]

    call(base + "/api/scan", {"queries": ["sneakersy"], "only_known_brands": False, "seller": False})
    status = wait_finished(base)
    assert status["searches"][0]["scope"] == "wszystkie marki" and "inny model/marka" not in status["funnel"]


def test_second_scan_is_rejected_while_running_and_stop_works(server):
    gate = threading.Event()
    base, _ = server(FakeVinted(gate))
    assert call(base + "/api/scan", {"queries": ["Nike", "adidas"]})[0] == 202
    code, body = call(base + "/api/scan", {"queries": ["Nike"]})
    assert code == 409 and "trwa" in body
    assert call(base + "/api/stop", {})[0] == 200
    gate.set()
    status = wait_finished(base)
    assert status["done"] == 1 and "(przerwano)" in status["errors"]


@pytest.mark.parametrize(
    "body, error",
    [({"queries": []}, "wpisz, czego szukać"), ({"queries": [" "]}, "wpisz, czego szukać"), ({}, "wpisz"),
     ({"queries": ["x" * 81]}, "80 znaków"), ({"queries": ["a"] * 11}, "najwyżej 10"), ({"queries": [5]}, "fraz"),
     ({"queries": ["?!"]}, "litery albo cyfry"),
     ({"queries": ["Nike"], "min_discount": 150}, "min_discount"),
     ({"queries": ["Nike"], "sizes": "42"}, "sizes"),
     ({"queries": ["Nike"], "max_ai_cost": 500}, "limit kosztu")],
)
def test_scan_validation(server, body, error):
    base, _ = server()
    code, text = call(base + "/api/scan", body)
    assert code == 400 and error in text


def test_rejects_foreign_host_and_non_json_post(server):
    base, _ = server()
    assert call(base + "/api/status", headers={"Host": "evil.example"})[0] == 403
    req = urllib.request.Request(base + "/api/scan", data=b"brands=x", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 415


def test_free_sources_can_be_switched_off_per_scan(server):
    vinted = FakeVinted()
    base, _ = server(vinted)
    data = json.loads(call(base + "/api/config")[1])
    assert data["ceneo_available"] is False and data["seller_default"] is True
    call(base + "/api/scan", {"queries": ["Nike"], "seller": False})
    wait_finished(base)
    assert not any("description" in c for c in vinted.calls)

    call(base + "/api/scan", {"queries": ["Nike"]})
    status = wait_finished(base)
    assert any("description" in c for c in vinted.calls)
    assert status["lookups"] == {"opisy": 1}
