"""Prezentacja wyników: terminal, JSON i samodzielny raport HTML."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from vinted_screener.models import Deal

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


def format_age(uploaded_at: datetime | None, now: datetime | None = None) -> str:
    if uploaded_at is None:
        return "?"
    minutes = max(0, int(((now or datetime.now(timezone.utc)) - uploaded_at).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} dni"


def deal_to_dict(deal: Deal) -> dict[str, Any]:
    l = deal.listing
    return {
        "id": l.id,
        "model": deal.item.name,
        "category": deal.item.category,
        "title": l.title,
        "brand": l.brand,
        "size": l.size,
        "condition": l.condition,
        "price": l.price,
        "total_price": round(l.total_price, 2),
        "currency": l.currency,
        "retail": deal.item.retail,
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
            f"{c('bold', score)}  {c('bold', f'-{d.retail_discount:.0%}')}  {c('bold', money(l.total_price, l.currency))}"
            f"  {c('dim', f'(retail {money(d.item.retail, l.currency)}{market})')}{warn}\n"
            f"       {c('cyan', d.item.name)} · rozm. {l.size or '?'} · {l.condition or '?'}"
            f" · {format_age(l.uploaded_at, now)} temu · ♥ {l.favourites}\n"
            f"       {l.title}\n"
            f"       {c('dim', l.url)}\n\n"
        )


def write_json(deals: list[Deal], path: str | Path) -> None:
    Path(path).write_text(json.dumps([deal_to_dict(d) for d in deals], ensure_ascii=False, indent=1), encoding="utf-8")


def write_html(deals: list[Deal], path: str | Path, meta: dict[str, Any]) -> None:
    payload = json.dumps(
        {"deals": [deal_to_dict(d) for d in deals], "meta": meta}, ensure_ascii=False
    ).replace("</", "<\\/")
    Path(path).write_text(_HTML_TEMPLATE.replace("__DATA__", payload), encoding="utf-8")


_HTML_TEMPLATE = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Okazje na Vinted</title>
<style>
:root {
  --bg: #f6f6f3; --card: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --line: #e3e3dd;
  --accent: #007782; --good: #1a7f37; --warn: #b54708; --bad: #c4320a;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #141413; --card: #1f1f1d; --text: #ecece8; --muted: #9a9a93; --line: #33332f;
          --accent: #3fb6c0; --good: #4ac26b; --warn: #f0a24a; --bad: #ff7a59; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { padding: 20px 16px 8px; max-width: 1280px; margin: 0 auto; }
h1 { margin: 0 0 4px; font-size: 24px; }
.meta { color: var(--muted); font-size: 13px; }
.controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
            padding: 12px 16px; max-width: 1280px; margin: 0 auto; }
.controls label { font-size: 13px; color: var(--muted); display: flex; gap: 6px; align-items: center; }
select, input[type=search] { font: inherit; padding: 6px 8px; border: 1px solid var(--line);
                             border-radius: 8px; background: var(--card); color: var(--text); }
input[type=search] { min-width: 180px; }
main { display: grid; gap: 14px; padding: 8px 16px 40px; max-width: 1280px; margin: 0 auto;
       grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
@media (max-width: 520px) { main { grid-template-columns: 1fr 1fr; gap: 10px; } .price { font-size: 17px; } }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
        display: flex; flex-direction: column; text-decoration: none; color: inherit; }
.card:hover { border-color: var(--accent); }
.photo { aspect-ratio: 3 / 4; background: var(--line) center / cover no-repeat; position: relative; }
.badge { position: absolute; top: 8px; left: 8px; background: var(--good); color: #fff; font-weight: 700;
         padding: 3px 8px; border-radius: 999px; font-size: 14px; }
.score { position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,.65); color: #fff;
         padding: 3px 8px; border-radius: 999px; font-size: 12px; }
.sus { position: absolute; bottom: 8px; left: 8px; right: 8px; background: var(--bad); color: #fff;
       font-size: 12px; padding: 3px 8px; border-radius: 6px; text-align: center; }
.body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 4px; }
.model { font-size: 12px; color: var(--accent); font-weight: 600; text-transform: uppercase; letter-spacing: .02em; }
.title { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.price { font-size: 20px; font-weight: 700; }
.price s { font-size: 13px; color: var(--muted); font-weight: 400; margin-left: 6px; }
.small { font-size: 12px; color: var(--muted); }
.mk-good { color: var(--good); } .mk-bad { color: var(--warn); }
.empty { grid-column: 1 / -1; text-align: center; color: var(--muted); padding: 60px 0; }
</style>
</head>
<body>
<header>
  <h1>Okazje na Vinted</h1>
  <div class="meta" id="meta"></div>
</header>
<div class="controls">
  <input type="search" id="q" placeholder="Szukaj w tytule…" aria-label="Szukaj">
  <label>Kategoria <select id="cat"><option value="">wszystkie</option></select></label>
  <label>Model <select id="model"><option value="">wszystkie</option></select></label>
  <label>Min. rabat <select id="disc">
    <option value="0">dowolny</option><option value="0.5">50%</option><option value="0.6">60%</option>
    <option value="0.7">70%</option><option value="0.8">80%</option></select></label>
  <label>Sortuj <select id="sort">
    <option value="score">wynik</option><option value="discount">rabat</option>
    <option value="price">cena</option><option value="newest">najnowsze</option></select></label>
  <label><input type="checkbox" id="sus"> pokaż podejrzane</label>
</div>
<main id="grid"></main>
<script>
const DATA = __DATA__;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const zl = (v, cur) => v == null ? "—" : Math.round(v).toLocaleString("pl-PL") + (cur === "PLN" ? " zł" : " " + cur);
const pct = (v) => Math.round(v * 100) + "%";
function age(iso) {
  if (!iso) return "";
  const min = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 60000));
  if (min < 60) return min + " min temu";
  if (min < 2880) return Math.floor(min / 60) + " h temu";
  return Math.floor(min / 1440) + " dni temu";
}
function fill(sel, values) {
  [...new Set(values)].sort().forEach((v) => sel.insertAdjacentHTML("beforeend", `<option>${esc(v)}</option>`));
}
fill($("cat"), DATA.deals.map((d) => d.category));
fill($("model"), DATA.deals.map((d) => d.model));
const m = DATA.meta;
$("meta").textContent = `Wygenerowano ${new Date(m.generated_at).toLocaleString("pl-PL")} · ` +
  `${DATA.deals.length} okazji z ${m.listings_seen} przejrzanych ogłoszeń · ${m.scanned_items} modeli` +
  (m.errors ? ` · błędy: ${m.errors}` : "");

const sorters = {
  score: (a, b) => b.score - a.score,
  discount: (a, b) => b.retail_discount - a.retail_discount,
  price: (a, b) => a.total_price - b.total_price,
  newest: (a, b) => (b.uploaded_at || "").localeCompare(a.uploaded_at || ""),
};
function render() {
  const q = $("q").value.trim().toLowerCase(), cat = $("cat").value, model = $("model").value;
  const minDisc = parseFloat($("disc").value), showSus = $("sus").checked;
  const rows = DATA.deals.filter((d) =>
    (!cat || d.category === cat) && (!model || d.model === model) && d.retail_discount >= minDisc &&
    (showSus || !d.suspicious) && (!q || (d.title + " " + d.model).toLowerCase().includes(q))
  ).sort(sorters[$("sort").value]);
  $("grid").innerHTML = rows.length ? rows.map((d) => {
    let market = "";
    if (d.market_median != null) {
      const cls = d.market_discount > 0 ? "mk-good" : "mk-bad";
      market = `<div class="small">mediana Vinted ${zl(d.market_median, d.currency)}` +
        (d.market_discount != null ? ` · <span class="${cls}">${d.market_discount > 0 ? "−" : "+"}${pct(Math.abs(d.market_discount))}</span>` : "") +
        `</div>`;
    }
    return `<a class="card" href="${esc(d.url)}" target="_blank" rel="noopener">
      <div class="photo" style="background-image:url('${esc(d.photo || "")}')">
        <span class="badge">−${pct(d.retail_discount)}</span><span class="score">${d.score.toFixed(0)} pkt</span>
        ${d.suspicious ? '<span class="sus">⚠ podejrzanie tanio — sprawdź autentyczność</span>' : ""}
      </div>
      <div class="body">
        <div class="model">${esc(d.model)}</div>
        <div class="title" title="${esc(d.title)}">${esc(d.title)}</div>
        <div class="price">${zl(d.total_price, d.currency)}<s>${zl(d.retail, d.currency)}</s></div>
        ${market}
        <div class="small">${esc(d.size || "?")} · ${esc(d.condition)} · ♥ ${d.favourites}</div>
        <div class="small">${esc(d.seller)} · ${age(d.uploaded_at)}</div>
      </div></a>`;
  }).join("") : '<div class="empty">Brak okazji dla wybranych filtrów.</div>';
}
["q", "cat", "model", "disc", "sort", "sus"].forEach((id) => $(id).addEventListener("input", render));
render();
</script>
</body>
</html>
"""
