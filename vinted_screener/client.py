"""Klient nieoficjalnego API katalogu Vinted (api.<domena>/svc-catalogue/items).

Od września 2026 wyszukiwarka siedzi na hoście api.* (stare www.*/api/v2/catalog/items zwraca 404).
Tożsamość dla API daje strona główna: ciasteczko access_token_web i nagłówek X-Anon-Id.
Vinted blokuje klientów o "nieprzeglądarkowym" odcisku TLS — dlatego curl_cffi z impersonacją Chrome.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

PER_PAGE_MAX = 96
SEARCH_PATH = "/svc-catalogue/items"
SESSION_COOKIE = "access_token_web"
BOOTSTRAP_ATTEMPTS = 3

_HTML_LANG_RE = re.compile(r"<html\b[^>]*?\blang=[\"']([A-Za-z]{2}(?:-[A-Za-z0-9]+)?)[\"']", re.IGNORECASE)
_CSRF_RE = re.compile(r'CSRF_TOKEN\\?"\s*:\s*\\?"([0-9a-fA-F-]{36})')


class VintedError(RuntimeError):
    pass


def _default_session() -> Any:
    from curl_cffi import requests

    return requests.Session(impersonate="chrome")


def _snippet(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


class VintedClient:
    def __init__(
        self,
        domain: str = "www.vinted.pl",
        delay: float = 1.5,
        max_retries: int = 4,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        host = domain.removeprefix("www.")
        self.site_url = f"https://www.{host}"
        self.api_url = f"https://api.{host}"
        self.delay = delay
        self.max_retries = max_retries
        self._session = session or _default_session()
        self._sleep = sleep
        self._clock = clock
        self._debug = debug or (lambda msg: None)
        self._last_request = float("-inf")
        self._token: str | None = None
        self._anon_id: str | None = None
        self._csrf: str | None = None
        self._locale = "pl-PL" if host.endswith(".pl") else "en-US"

    def _throttle(self) -> None:
        wait = self._last_request + self.delay * random.uniform(1.0, 1.5) - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    def _absorb_cookies(self) -> None:
        """Przejmuje token z ciasteczek sesji i czyści je.

        Token idzie jawnie w nagłówku Cookie; gdyby został też w ciasteczkach curl_cffi,
        do API poleciałyby dwa nagłówki Cookie.
        """
        for cookie in self._session.cookies.jar:
            if cookie.name == SESSION_COOKIE and cookie.value:
                self._token = cookie.value
        self._session.cookies.clear()

    def _bootstrap(self) -> None:
        """Wejście na stronę główną daje token sesji, anonimowe ID i język rynku."""
        self._token = self._anon_id = self._csrf = None
        problem = ""
        for attempt in range(BOOTSTRAP_ATTEMPTS):
            if attempt:
                self._sleep(2**attempt)
            self._session.cookies.clear()
            self._throttle()
            try:
                resp = self._session.get(self.site_url + "/", timeout=20)
            except OSError as exc:
                problem = f"brak połączenia: {exc}"
                continue
            self._debug(f"GET {self.site_url}/ → HTTP {resp.status_code}")
            if resp.status_code != 200:
                problem = f"HTTP {resp.status_code}"
                continue
            self._absorb_cookies()
            self._anon_id = (resp.headers.get("X-Anon-Id") or "").strip() or None
            html = resp.text or ""
            if lang := _HTML_LANG_RE.search(html):
                self._locale = lang.group(1)
            if csrf := _CSRF_RE.search(html):
                self._csrf = csrf.group(1)
            self._debug(
                f"sesja: token={'tak' if self._token else 'BRAK'} anon_id={self._anon_id or 'BRAK'}"
                f" locale={self._locale} csrf={'tak' if self._csrf else 'nie'}"
            )
            if self._token:
                return
            problem = f"strona główna nie ustawiła ciasteczka {SESSION_COOKIE}"
        raise VintedError(f"nie udało się otworzyć sesji na {self.site_url} ({problem})")

    def _api_headers(self) -> dict[str, str | None]:
        headers: dict[str, str | None] = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{self._locale},en;q=0.5",
            "Origin": self.site_url,
            "Referer": self.site_url + "/",
            "Locale": self._locale,
            "Platform": "web",
            "X-Next-App": "marketplace-web",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            # domyślne nagłówki nawigacji z impersonacji Chrome — fetch() ich nie wysyła
            "Sec-Fetch-User": None,
            "Upgrade-Insecure-Requests": None,
            "Cookie": f"{SESSION_COOKIE}={self._token}",
        }
        if self._anon_id:
            headers["X-Anon-Id"] = self._anon_id
        if self._csrf:
            headers["X-Csrf-Token"] = self._csrf
        return headers

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._token:
            self._bootstrap()
        url = self.api_url + path
        refreshed = False
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(url, params=params, headers=self._api_headers(), timeout=20)
            except OSError as exc:
                last_error = str(exc)
                self._sleep(2**attempt)
                continue
            self._absorb_cookies()
            self._debug(f"GET {url} {params} → HTTP {resp.status_code}: {_snippet(resp.text or '', 300)}")
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise VintedError(f"{path}: odpowiedź to nie JSON: {_snippet(resp.text or '')}") from None
            last_error = f"HTTP {resp.status_code}"
            if body := _snippet(resp.text or "", 120):
                last_error += f" ({body})"
            if resp.status_code in (401, 403) and not refreshed:
                # wygasła sesja — pobierz nową tożsamość i spróbuj jeszcze raz
                refreshed = True
                self._bootstrap()
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                self._sleep(5 * 2**attempt)
                continue
            break
        hint = ""
        if last_error.startswith(("HTTP 403", "HTTP 429")):
            hint = " — Vinted blokuje zapytania, zwiększ delay albo spróbuj później"
        elif last_error.startswith("HTTP 404"):
            hint = " — Vinted prawdopodobnie zmienił API, uruchom z --debug i zgłoś wynik"
        raise VintedError(f"zapytanie {path} nie powiodło się: {last_error}{hint}")

    def search(
        self,
        text: str,
        *,
        status_ids: Iterable[int] = (),
        price_from: float | None = None,
        price_to: float | None = None,
        order: str = "newest_first",
        pages: int = 1,
        per_page: int = PER_PAGE_MAX,
    ) -> list[dict[str, Any]]:
        """Surowe ogłoszenia z kolejnych stron wyszukiwania (domyślnie od najnowszych)."""
        params: dict[str, Any] = {
            "search_text": text,
            "order": order,
            "per_page": min(per_page, PER_PAGE_MAX),
        }
        if status_ids:
            params["attribute_ids[status]"] = ",".join(str(s) for s in status_ids)
        if price_from is not None:
            params["price_from"] = int(price_from)
        if price_to is not None:
            params["price_to"] = int(price_to)

        items: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            data = self._get_json(SEARCH_PATH, {**params, "page": page})
            page_items = data.get("items") or []
            items.extend(page_items)
            total_pages = (data.get("pagination") or {}).get("total_pages")
            if len(page_items) < params["per_page"] or (total_pages is not None and page >= total_pages):
                break
        return items
