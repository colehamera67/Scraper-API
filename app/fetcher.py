from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx

from .config import RetailerAccess, Settings, settings as default_settings

BLOCK_MARKERS = ("Robot or human", "Access Denied", "px-captcha", "/blocked?")
MAX_CACHE_ENTRIES = 1000


class FetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BlockedError(FetchError):
    """The retailer refused the request (bot protection, rate limit, geo block)."""


class _HostGate:
    """Caps requests in flight and spaces request starts at least `interval` seconds apart."""

    def __init__(self, concurrency: int, interval: float):
        self.interval = interval
        self._sem = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._sem:
            async with self._lock:
                now = time.monotonic()
                start = max(now, self._next_start)
                self._next_start = start + self.interval
            if start > now:
                await asyncio.sleep(start - now)
            yield


class Fetcher:
    """Polite HTTP fetcher: per-retailer access settings and rate limits, a small TTL cache, block detection."""

    def __init__(self, settings: Settings = default_settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._gates: dict[str, _HostGate] = {}

    def access_for(self, host: str) -> RetailerAccess:
        for domain, access in self.settings.access.items():
            if host == domain or host.endswith("." + domain):
                return access
        return RetailerAccess()

    def _client(self, proxy: str | None) -> httpx.AsyncClient:
        if proxy not in self._clients:
            self._clients[proxy] = httpx.AsyncClient(
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=self.settings.timeout,
                follow_redirects=True,
                proxy=proxy,
                transport=self._transport,
            )
        return self._clients[proxy]

    def _gate(self, host: str, access: RetailerAccess) -> _HostGate:
        if host not in self._gates:
            interval = self.settings.min_interval if access.min_interval is None else access.min_interval
            self._gates[host] = _HostGate(access.concurrency, interval)
        return self._gates[host]

    async def _request(self, url: str, cookies: dict[str, str] | None) -> httpx.Response:
        host = urlparse(url).netloc
        access = self.access_for(urlparse(url).hostname or "")
        client = self._client(access.proxy_url or self.settings.proxy_url)
        headers = dict(access.headers)
        if jar := {**access.cookies, **(cookies or {})}:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        async with self._gate(host, access).slot():
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as e:
                raise FetchError(f"request to {host} failed: {e}") from e

        if resp.status_code in (403, 429) or "/blocked" in resp.url.path:
            raise BlockedError(f"{host} blocked the request (HTTP {resp.status_code}, {resp.url})", resp.status_code)
        if resp.status_code >= 400:
            raise FetchError(f"{host} returned HTTP {resp.status_code} for {url}", resp.status_code)
        return resp

    async def get(self, url: str, cookies: dict[str, str] | None = None, use_cache: bool = True) -> str:
        if use_cache and (cached := self._cache.get(url)) and time.monotonic() - cached[0] < self.settings.cache_ttl:
            return cached[1]

        resp = await self._request(url, cookies)
        text = resp.text
        if len(text) < 20_000 and any(m in text for m in BLOCK_MARKERS):
            raise BlockedError(f"{urlparse(url).netloc} served a bot-check page instead of content")

        if use_cache and self.settings.cache_ttl > 0:
            self._cache[url] = (time.monotonic(), text)
            self._cache.move_to_end(url)
            while len(self._cache) > MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return text

    async def get_bytes(self, url: str, cookies: dict[str, str] | None = None) -> bytes:
        """Raw body, uncached (sitemaps, which may be gzip files)."""
        return (await self._request(url, cookies)).content

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
