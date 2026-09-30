"""Společný HTTP klient: timeouty, hlavičky, retry na 429/5xx/timeout, fallback UA při 403."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
JSON_ACCEPT = "application/json"


class FetchError(Exception):
    """Chyba stahování; kind = http_error / timeout / network."""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class _Retryable(Exception):
    def __init__(self, response: httpx.Response | None, exc: Exception | None = None):
        super().__init__(str(exc) if exc else f"HTTP {response.status_code if response else '?'}")
        self.response = response
        self.exc = exc


@dataclass
class HttpConfig:
    user_agent: str
    connect_timeout: float = 10.0
    read_timeout: float = 20.0
    max_retries: int = 2


def make_client(cfg: HttpConfig) -> httpx.Client:
    timeout = httpx.Timeout(connect=cfg.connect_timeout, read=cfg.read_timeout, write=20.0, pool=30.0)
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": cfg.user_agent, "Accept-Language": "cs,en;q=0.8"},
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )


def _retry_after_seconds(resp: httpx.Response) -> float:
    value = resp.headers.get("retry-after")
    if value and value.isdigit():
        return min(float(value), 30.0)
    return 0.0


def get(
    client: httpx.Client,
    url: str,
    *,
    accept: str = HTML_ACCEPT,
    params: dict | None = None,
    headers: dict | None = None,
    max_retries: int = 2,
    browser_fallback: bool = True,
) -> httpx.Response:
    """GET s retry (max. `max_retries` opakování) jen na 429/5xx/timeout a s fallbackem na prohlížečový UA při 403."""

    def attempt(ua: str | None) -> httpx.Response:
        hdrs = {"Accept": accept, **(headers or {})}
        if ua:
            hdrs["User-Agent"] = ua

        @retry(
            stop=stop_after_attempt(max_retries + 1),
            wait=wait_exponential(multiplier=1.5, min=1, max=20),
            retry=retry_if_exception(lambda e: isinstance(e, _Retryable)),
            reraise=False,
        )
        def _do() -> httpx.Response:
            try:
                resp = client.get(url, params=params, headers=hdrs)
            except (httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
                raise _Retryable(None, exc) from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                raw_after = resp.headers.get("retry-after", "")
                if raw_after.isdigit() and int(raw_after) > 60:
                    return resp  # dlouhé čekání (např. vyčerpaný denní limit) nemá smysl opakovat
                delay = _retry_after_seconds(resp)
                if delay:
                    time.sleep(delay)
                raise _Retryable(resp)
            return resp

        try:
            return _do()
        except RetryError as err:
            last = err.last_attempt.exception()
            if isinstance(last, _Retryable) and last.response is not None:
                return last.response
            if isinstance(last, _Retryable) and last.exc is not None:
                raise FetchError("timeout", f"Timeout/síť: {last.exc!s}"[:300]) from last.exc
            raise FetchError("network", str(last)[:300]) from last

    try:
        resp = attempt(None)
        if resp.status_code == 403 and browser_fallback:
            log.debug("403 z %s, zkouším prohlížečový User-Agent", url)
            resp = attempt(BROWSER_UA)
        if resp.status_code == 403 and browser_fallback:
            # Některé CDN (např. Pantheon/Fastly) odmítají klienta httpx bez ohledu na hlavičky,
            # ale standardní urllib projde. Poslední pokus proto přes stdlib.
            fallback = _urllib_get(url, params, {"Accept": accept, **(headers or {})},
                                   client.headers.get("User-Agent", BROWSER_UA), client.timeout.read or 20.0)
            if fallback is not None:
                resp = fallback
    except httpx.HTTPError as exc:
        raise FetchError("network", f"{type(exc).__name__}: {exc}"[:300]) from exc
    if resp.status_code >= 400:
        raise FetchError("http_error", f"HTTP {resp.status_code}", status=resp.status_code)
    return resp


def _urllib_get(url: str, params: dict | None, headers: dict, user_agent: str, timeout: float) -> httpx.Response | None:
    """Záložní GET přes urllib; vrací httpx.Response kvůli jednotnému rozhraní, nebo None při chybě."""
    import urllib.error
    import urllib.request
    from urllib.parse import urlencode

    full = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}" if params else url
    req = urllib.request.Request(full, headers={**headers, "User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:  # noqa: S310 - URL z konfigurace
            body = fh.read()
            hdrs = {k: v for k, v in fh.headers.items()
                    if k.lower() not in ("content-encoding", "transfer-encoding", "content-length")}
            return httpx.Response(fh.status, headers=hdrs, content=body,
                                  request=httpx.Request("GET", fh.geturl()))
    except urllib.error.HTTPError as exc:
        return httpx.Response(exc.code, request=httpx.Request("GET", full))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.debug("urllib fallback selhal pro %s: %s", url, exc)
        return None
