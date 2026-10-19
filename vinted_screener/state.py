"""Trwały stan między uruchomieniami: widziane ogłoszenia (tryb watch) i cache (mediany, ceny retail, rozpoznania AI)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

SEEN_TTL_SECONDS = 14 * 24 * 3600
CACHE_MAX_AGE_SECONDS = 60 * 24 * 3600  # starsze wpisy wyrzucamy przy wczytaniu, żeby plik nie puchł


class State:
    def __init__(self, path: str | Path | None = None, now: float | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.seen: dict[str, float] = {}
        self.caches: dict[str, dict[str, dict[str, Any]]] = {}
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            self.seen = {str(k): float(v) for k, v in data.get("seen", {}).items()}
            self.caches = data.get("caches", {})
        now = now or time.time()
        self.seen = {k: v for k, v in self.seen.items() if v >= now - SEEN_TTL_SECONDS}
        self.caches = {
            bucket: {k: v for k, v in entries.items() if now - v.get("ts", 0) < CACHE_MAX_AGE_SECONDS}
            for bucket, entries in self.caches.items()
        }

    def is_seen(self, listing_id: int) -> bool:
        return str(listing_id) in self.seen

    def mark_seen(self, listing_id: int, now: float | None = None) -> None:
        self.seen.setdefault(str(listing_id), now or time.time())

    def get_cached(self, bucket: str, key: str, ttl_hours: float, now: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            entry = self.caches.get(bucket, {}).get(key)
        if entry and (now or time.time()) - entry.get("ts", 0) < ttl_hours * 3600:
            return entry
        return None

    def put_cached(self, bucket: str, key: str, value: dict[str, Any], now: float | None = None) -> None:
        with self._lock:
            self.caches.setdefault(bucket, {})[key] = {**value, "ts": now or time.time()}

    def cached_market(self, key: str, ttl_hours: float, now: float | None = None) -> dict[str, Any] | None:
        return self.get_cached("market", key, ttl_hours, now)

    def store_market(self, key: str, median: float | None, samples: int, now: float | None = None) -> None:
        self.put_cached("market", key, {"median": median, "samples": samples}, now)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with self._lock:
            payload = json.dumps({"seen": self.seen, "caches": self.caches}, ensure_ascii=False, indent=1)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)
