"""Prezentacja wyników: terminal, JSON i samodzielny raport HTML."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from importlib import resources
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from vinted_screener.models import Deal, RetailSource

SORT_KEYS = {
    "score": lambda d: -d.score,
    "discount": lambda d: -d.retail_discount,
    "price": lambda d: d.listing.total_price,
    "newest": lambda d: -(d.listing.uploaded_at.timestamp() if d.listing.uploaded_at else 0),
}


def sort_deals(deals: list[Deal], key: str) -> list[Deal]:
    return sorted(deals, key=SORT_KEYS[key])


def money(value: float | None, currency: str = "PLN") -> str:
    if value is None:
        return "—"
    unit = "zł" if currency == "PLN" else currency
    return f"{value:,.0f} {unit}".replace(",", " ")


def discount_text(discount: float) -> str:
    """"-68%" dla okazji, "+12%" gdy oferta jest droższa niż cena odniesienia (skan bez progu rabatu).

    Zwykły myślnik, bo konsola Windows w cp1250 nie ma znaku minusa (U+2212).
    """
    return f"-{discount:.0%}" if discount >= 0 else f"+{-discount:.0%}"


def format_age(uploaded_at: datetime | None, now: datetime | None = None) -> str:
    if uploaded_at is None:
        return "?"
    minutes = max(0, int(((now or datetime.now(timezone.utc)) - uploaded_at).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} dni"


def source_text(source: RetailSource) -> str:
    text = source.label
    if source.url:
        text += f" {source.url}"
    return text


def deal_to_dict(deal: Deal) -> dict[str, Any]:
    l = deal.listing
    return {
        "id": l.id,
        "model": deal.model,
        "category": deal.category,
        "title": l.title,
        "brand": l.brand,
        "size": l.size,
        "condition": l.condition,
        "price": l.price,
        "total_price": round(l.total_price, 2),
        "currency": l.currency,
        "retail": deal.retail,
        "retail_source": dataclasses.asdict(deal.retail_source),
        "retail_discount": round(deal.retail_discount, 4),
        "market_median": deal.market_median,
        "market_discount": round(deal.market_discount, 4) if deal.market_discount is not None else None,
        "score": deal.score,
        "suspicious": deal.suspicious,
        "url": l.url,
        "photo": l.photo_url,
        "seller": l.seller,
        "favourites": l.favourites,
        "uploaded_at": l.uploaded_at.isoformat() if l.uploaded_at else None,
    }


_ANSI = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
_ansi_ok = os.name != "nt"


def enable_windows_ansi() -> None:
    """Włącza obsługę kodów ANSI w konsoli Windows (ENABLE_VIRTUAL_TERMINAL_PROCESSING)."""
    global _ansi_ok
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        _ansi_ok = bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))


def print_deals(deals: list[Deal], out: TextIO | None = None, color: bool | None = None, now: datetime | None = None) -> None:
    out = out or sys.stdout
    enabled = (_ansi_ok and out.isatty()) if color is None else color

    def c(style: str, text: str) -> str:
        return f"\033[{_ANSI[style]}m{text}\033[0m" if enabled else text

    for d in deals:
        l = d.listing
        score = c("green" if d.score >= 60 else "yellow", f"{d.score:5.1f}")
        market = ""
        if d.market_median is not None:
            market = f" · mediana Vinted {money(d.market_median, l.currency)}"
            if d.market_discount is not None:
                market += f" ({-d.market_discount:+.0%})"
        warn = c("red", " ⚠ PODEJRZANIE TANIO") if d.suspicious else ""
        out.write(
            f"{c('bold', score)}  {c('bold', discount_text(d.retail_discount))}  {c('bold', money(l.total_price, l.currency))}"
            f"  {c('dim', f'(retail {money(d.retail, l.currency)} wg {source_text(d.retail_source)}{market})')}{warn}\n"
            f"       {c('cyan', d.model)} · rozm. {l.size or '?'} · {l.condition or '?'}"
            f" · {format_age(l.uploaded_at, now)} temu · ♥ {l.favourites}\n"
            f"       {l.title}\n"
            f"       {c('dim', l.url)}\n\n"
        )


def write_json(deals: list[Deal], path: str | Path) -> None:
    Path(path).write_text(json.dumps([deal_to_dict(d) for d in deals], ensure_ascii=False, indent=1), encoding="utf-8")


def render_page(boot: dict[str, Any]) -> str:
    """Strona z page.html: {"mode": "static", "deals": [...], "meta": {...}} = raport, {"mode": "app"} = serve."""
    template = resources.files("vinted_screener").joinpath("page.html").read_text(encoding="utf-8")
    payload = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("__BOOT__", payload)


def write_html(deals: list[Deal], path: str | Path, meta: dict[str, Any]) -> None:
    page = render_page({"mode": "static", "deals": [deal_to_dict(d) for d in deals], "meta": meta})
    Path(path).write_text(page, encoding="utf-8")
