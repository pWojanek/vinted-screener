from __future__ import annotations

import io
import json
from pathlib import Path

from conftest import make_item, raw_item

from vinted_screener import cli
from vinted_screener.models import Deal, Listing
from vinted_screener.notify import discord_payloads
from vinted_screener.report import print_deals, write_html

ROOT = Path(__file__).resolve().parent.parent


def make_deal(title: str = "Nike Dunk Low Panda", price: float = 150, **kw) -> Deal:
    listing = Listing.from_api(raw_item(title, price, **kw))
    return Deal(make_item(), listing, 0.68, 320.0, 0.5, 72.5, suspicious=False)


def test_print_deals_plain_text():
    out = io.StringIO()
    print_deals([make_deal()], out=out, color=False)
    text = out.getvalue()
    assert "-68%" in text and "158 zł" in text and "retail 500 zł" in text
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
    out = capsys.readouterr().out
    assert "Nike Dunk Low" in out and "Jordan" not in out


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
