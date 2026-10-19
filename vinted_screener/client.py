"""Klient nieoficjalnego API katalogu Vinted (/api/v2/catalog/items).

Vinted wymaga ciasteczka sesji, które dostaje się wchodząc na stronę główną,
i blokuje klientów o "nieprzeglądarkowym" odcisku TLS — dlatego curl_cffi z impersonacją Chrome.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from typing import Any

PER_PAGE_MAX = 96


class VintedError(RuntimeError):
    pass


def _default_session() -> Any:
    from curl_cffi import requests

    return requests.Session(impersonate="chrome")


class VintedClient:
    def __init__(
        self,
        domain: str = "www.vinted.pl",
        delay: float = 1.5,
        max_retries: int = 4,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = f"https://{domain}"
        self.delay = delay
        self.max_retries = max_retries
        self._session = session or _default_session()
        self._sleep = sleep
        self._clock = clock
        self._last_request = float("-inf")
        self._has_session = False

    def _throttle(self) -> None:
        wait = self._last_request + self.delay * random.uniform(1.0, 1.5) - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    def _bootstrap(self) -> None:
        """Wejście na stronę główną ustawia ciasteczka sesji (access_token_web)."""
        self._session.cookies.clear()
        self._throttle()
        try:
            resp = self._session.get(self.base_url + "/", timeout=20)
        except OSError as exc:
            raise VintedError(f"brak połączenia z {self.base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise VintedError(f"nie udało się otworzyć sesji na {self.base_url} (HTTP {resp.status_code})")
        self._has_session = True

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._has_session:
            self._bootstrap()
        refreshed = False
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(
                    self.base_url + path,
                    params=params,
                    headers={"Accept": "application/json", "Referer": self.base_url + "/catalog"},
                    timeout=20,
                )
            except OSError as exc:
                last_error = str(exc)
                self._sleep(2**attempt)
                continue
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code in (401, 403) and not refreshed:
                # wygasła sesja — pobierz nowe ciasteczka i spróbuj jeszcze raz
                refreshed = True
                self._bootstrap()
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                self._sleep(5 * 2**attempt)
                continue
            break
        hint = " (Vinted blokuje zapytania — zwiększ delay albo spróbuj później)" if last_error in ("HTTP 403", "HTTP 429") else ""
        raise VintedError(f"zapytanie {path} nie powiodło się: {last_error}{hint}")

    def search(
        self,
        text: str,
        *,
        status_ids: Iterable[int] = (),
        price_from: float | None = None,
        price_to: float | None = None,
        currency: str = "PLN",
        order: str = "newest_first",
        pages: int = 1,
        per_page: int = PER_PAGE_MAX,
    ) -> list[dict[str, Any]]:
        """Surowe ogłoszenia z kolejnych stron wyszukiwania (domyślnie od najnowszych)."""
        params: dict[str, Any] = {
            "search_text": text,
            "order": order,
            "per_page": min(per_page, PER_PAGE_MAX),
            "currency": currency,
        }
        if status_ids:
            params["status_ids"] = ",".join(str(s) for s in status_ids)
        if price_from is not None:
            params["price_from"] = int(price_from)
        if price_to is not None:
            params["price_to"] = int(price_to)

        items: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            data = self._get_json("/api/v2/catalog/items", {**params, "page": page})
            page_items = data.get("items") or []
            items.extend(page_items)
            total_pages = (data.get("pagination") or {}).get("total_pages")
            if len(page_items) < params["per_page"] or (total_pages is not None and page >= total_pages):
                break
        return items
