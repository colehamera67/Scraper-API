from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx

from .config import Settings, settings as default_settings

BLOCK_MARKERS = ("Robot or human", "Access Denied", "px-captcha", "/blocked?")


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """The retailer refused the request (bot protection, rate limit, geo block)."""


class Fetcher:
    """Polite HTTP fetcher: per-host rate limiting, in-memory TTL cache, block detection."""

    def __init__(self, settings: Settings = default_settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=settings.timeout,
            follow_redirects=True,
            proxy=settings.proxy_url,
            transport=transport,
        )
        self._cache: dict[str, tuple[float, str]] = {}
        self._last_hit: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, url: str, cookies: dict[str, str] | None = None) -> str:
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self.settings.cache_ttl:
            return cached[1]

        host = urlparse(url).netloc
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self.settings.min_interval - (time.monotonic() - self._last_hit.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._client.get(url, cookies=cookies)
            except httpx.HTTPError as e:
                raise FetchError(f"request to {host} failed: {e}") from e
            finally:
                self._last_hit[host] = time.monotonic()

        if resp.status_code in (403, 429) or "/blocked" in resp.url.path:
            raise BlockedError(f"{host} blocked the request (HTTP {resp.status_code}, {resp.url})")
        if resp.status_code >= 400:
            raise FetchError(f"{host} returned HTTP {resp.status_code} for {url}")
        text = resp.text
        if len(text) < 20_000 and any(m in text for m in BLOCK_MARKERS):
            raise BlockedError(f"{host} served a bot-check page instead of content")

        self._cache[url] = (time.monotonic(), text)
        return text

    async def aclose(self) -> None:
        await self._client.aclose()
