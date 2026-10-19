from __future__ import annotations

import io
import json

import pytest
from pathlib import Path

from conftest import make_item, raw_item

from vinted_screener import cli
from vinted_screener.models import Deal, Listing, RetailSource
from vinted_screener.notify import discord_payloads
from vinted_screener.retail import CostMeter
from vinted_screener.report import print_deals, write_html

ROOT = Path(__file__).resolve().parent.parent


def make_deal(title: str = "Nike Dunk Low Panda", price: float = 150, **kw) -> Deal:
    listing = Listing.from_api(raw_item(title, price, **kw))
    item = make_item()
    source = RetailSource(kind="watchlist", label="watchlista", product=item.name)
    return Deal(listing, item.name, item.category, item.retail, source, 0.68, 320.0, 0.5, 72.5, suspicious=False)


def test_print_deals_plain_text():
    out = io.StringIO()
    print_deals([make_deal()], out=out, color=False)
    text = out.getvalue()
    assert "-68%" in text and "158 zł" in text and "retail 500 zł wg watchlista" in text
    assert "mediana Vinted 320 zł (-50%)" in text
    assert "Nike Dunk Low Panda" in text and "https://www.vinted.pl/items/" in text
    assert "\033[" not in text


def test_html_report_embeds_data_safely(tmp_path):
    path = tmp_path / "r.html"
    write_html([make_deal(title="Dunk </script><b>x")], path, {"generated_at": "2026-10-19T12:00:00+00:00"})
    page = path.read_text()
    assert "</script><b>" not in page
    data = json.loads(page.split("const BOOT = ", 1)[1].split(";\n", 1)[0].replace("<\\/", "</"))
    assert data["mode"] == "static"
    assert data["deals"][0]["title"] == "Dunk </script><b>x"
    assert data["deals"][0]["retail_discount"] == 0.68


def test_discord_payloads_are_chunked():
    payloads = discord_payloads([make_deal() for _ in range(12)])
    assert [len(p["embeds"]) for p in payloads] == [10, 2]
    assert payloads[0]["embeds"][0]["url"].startswith("https://www.vinted.pl/items/")


def test_cli_list(capsys):
    assert cli.main(["list", "-w", str(ROOT / "watchlist.toml"), "--only", "dunk"]) == 0
    models, brands = capsys.readouterr().out.split("Znane marki", 1)
    assert "Nike Dunk Low" in models and "Jordan" not in models
    assert "Nike" in brands and "Carhartt WIP" in brands


def test_cli_scan_end_to_end(tmp_path, monkeypatch, capsys):
    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def search(self, text, **kwargs):
            if "dunk" in text and kwargs.get("price_to") is not None:
                return [raw_item("Nike Dunk Low Panda", 150, id=99)]
            return []

    monkeypatch.setattr(cli, "VintedClient", FakeClient)
    html = tmp_path / "okazje.html"
    code = cli.main([
        "scan", "-w", str(ROOT / "watchlist.toml"), "--category", "sneakersy",
        "--html", str(html), "--json", str(tmp_path / "o.json"), "--state", str(tmp_path / "s.json"), "-q",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "Nike Dunk Low Panda" in captured.out
    assert "1 okazji" in captured.err
    assert json.loads((tmp_path / "o.json").read_text())[0]["id"] == 99
    assert html.exists()


def test_cli_rejects_bad_watchlist(tmp_path, capsys):
    assert cli.main(["scan", "-w", str(tmp_path / "brak.toml")]) == 2
    assert "nie znaleziono" in capsys.readouterr().err


def test_cli_search_without_api_key_uses_watchlist(tmp_path, monkeypatch, capsys):
    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def search(self, text, **kwargs):
            assert text == "nike buty" and kwargs["pages"] == 3
            return [raw_item("Nike Dunk Low Panda", 150, id=7), raw_item("Nike bluza", 50, id=8)]

    monkeypatch.setattr(cli, "VintedClient", FakeClient)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    code = cli.main([
        "scan", "-w", str(ROOT / "watchlist.toml"), "--search", "nike buty", "--no-ceneo", "--no-seller",
        "--html", "", "--json", str(tmp_path / "o.json"), "--state", str(tmp_path / "s.json"), "-q",
    ])
    assert code == 0
    err = capsys.readouterr().err
    assert "brak ANTHROPIC_API_KEY" in err and "bez ceny odniesienia 1" in err
    deals = json.loads((tmp_path / "o.json").read_text())
    assert [d["id"] for d in deals] == [7]
    assert deals[0]["retail_source"]["kind"] == "watchlist"


def test_watch_search_gets_fresh_ai_budget_each_cycle(tmp_path, monkeypatch):
    budgets = []

    class FakeFinder:
        def __init__(self, client, state, max_usd, debug=None):
            budgets.append(max_usd)
            self.cost = CostMeter()

    class StopAfterTwo(Exception):
        pass

    def fake_screen(self, searches, **kwargs):
        if len(budgets) == 2:
            raise StopAfterTwo
        from vinted_screener.screener import ScanResult
        return ScanResult()

    monkeypatch.setattr(cli, "VintedClient", lambda **kw: None)
    monkeypatch.setattr(cli, "make_anthropic_client", lambda: object())
    monkeypatch.setattr(cli, "RetailFinder", FakeFinder)
    monkeypatch.setattr(cli.SearchScreener, "screen", fake_screen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    with pytest.raises(StopAfterTwo):
        cli.main(["watch", "-w", str(ROOT / "watchlist.toml"), "--brand", "nike", "--max-ai-cost", "1.5",
                  "--html", "", "--state", str(tmp_path / "s.json"), "-q"])
    assert budgets == [1.5, 1.5], "każdy cykl watch ma własny limit kosztu"
