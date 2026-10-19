"""Trwały stan między uruchomieniami: widziane ogłoszenia (tryb watch) i cache median rynkowych."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SEEN_TTL_SECONDS = 14 * 24 * 3600


class State:
    def __init__(self, path: str | Path | None = None, now: float | None = None) -> None:
        self.path = Path(path) if path else None
        self.seen: dict[str, float] = {}
        self.market: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            self.seen = {str(k): float(v) for k, v in data.get("seen", {}).items()}
            self.market = data.get("market", {})
        cutoff = (now or time.time()) - SEEN_TTL_SECONDS
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}

    def is_seen(self, listing_id: int) -> bool:
        return str(listing_id) in self.seen

    def mark_seen(self, listing_id: int, now: float | None = None) -> None:
        self.seen.setdefault(str(listing_id), now or time.time())

    def cached_market(self, key: str, ttl_hours: float, now: float | None = None) -> dict[str, Any] | None:
        entry = self.market.get(key)
        if entry and (now or time.time()) - entry.get("ts", 0) < ttl_hours * 3600:
            return entry
        return None

    def store_market(self, key: str, median: float | None, samples: int, now: float | None = None) -> None:
        self.market[key] = {"median": median, "samples": samples, "ts": now or time.time()}

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": self.seen, "market": self.market}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
