"""Powiadomienia o nowych okazjach przez webhook Discorda."""

from __future__ import annotations

import json
import urllib.request

from vinted_screener.models import Deal
from vinted_screener.report import discount_text, money, source_text

EMBEDS_PER_MESSAGE = 10  # limit Discorda


def discord_payloads(deals: list[Deal]) -> list[dict]:
    embeds = []
    for d in deals:
        l = d.listing
        lines = [
            f"**{money(l.total_price, l.currency)}** zamiast {money(d.retail, l.currency)} ({discount_text(d.retail_discount)})",
            f"retail wg {source_text(d.retail_source)}",
            f"{l.size or '?'} · {l.condition or '?'} · wynik {d.score:.0f}",
        ]
        if d.market_median is not None:
            lines.append(f"mediana Vinted: {money(d.market_median, l.currency)}")
        if d.suspicious:
            lines.append("⚠ podejrzanie tanio — sprawdź autentyczność")
        embed = {"title": f"{d.model}: {l.title}"[:256], "url": l.url, "description": "\n".join(lines)}
        if l.photo_url:
            embed["thumbnail"] = {"url": l.photo_url}
        embeds.append(embed)
    return [{"embeds": embeds[i : i + EMBEDS_PER_MESSAGE]} for i in range(0, len(embeds), EMBEDS_PER_MESSAGE)]


def send_discord(webhook_url: str, deals: list[Deal]) -> None:
    for payload in discord_payloads(deals):
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "vinted-screener"},
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
