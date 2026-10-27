import asyncio
import time

import httpx
import pytest

from app.config import RetailerAccess, Settings, retailer_access_from_env
from app.fetcher import MAX_CACHE_ENTRIES, Fetcher


def settings(**access):
    return Settings(min_interval=0, cache_ttl=900, access=access)


def test_access_from_env(monkeypatch):
    monkeypatch.setenv("WALMART_HEADERS", '{"X-Partner-Token": "abc"}')
    monkeypatch.setenv("WALMART_COOKIES", '{"session": "s1"}')
    monkeypatch.setenv("WALMART_PROXY_URL", "http://static.example:8080")
    monkeypatch.setenv("WALMART_MIN_INTERVAL", "0.25")
    monkeypatch.setenv("WALMART_CONCURRENCY", "8")
    monkeypatch.setenv("WALMART_SITEMAPS", "https://www.walmart.com/a.xml, https://www.walmart.com/b.xml")
    a = retailer_access_from_env("WALMART")
    assert a.headers == {"X-Partner-Token": "abc"} and a.cookies == {"session": "s1"}
    assert (a.proxy_url, a.min_interval, a.concurrency) == ("http://static.example:8080", 0.25, 8)
    assert a.sitemaps == ("https://www.walmart.com/a.xml", "https://www.walmart.com/b.xml")
    assert retailer_access_from_env("LOWES") == RetailerAccess()

    monkeypatch.setenv("WALMART_HEADERS", "not json")
    with pytest.raises(ValueError, match="WALMART_HEADERS"):
        retailer_access_from_env("WALMART")


def test_headers_and_cookies_go_only_to_their_retailer():
    seen = {}

    def handler(request):
        seen[request.url.host] = request.headers
        return httpx.Response(200, text="ok")

    f = Fetcher(
        settings(**{"walmart.com": RetailerAccess(headers={"X-Partner-Token": "abc"}, cookies={"s": "1"})}),
        transport=httpx.MockTransport(handler),
    )

    async def run():
        await f.get("https://www.walmart.com/ip/1", cookies={"extra": "2"})
        await f.get("https://www.lowes.com/pd/1")
        await f.aclose()

    asyncio.run(run())
    assert seen["www.walmart.com"]["x-partner-token"] == "abc"
    assert seen["www.walmart.com"]["cookie"] == "s=1; extra=2"
    assert "x-partner-token" not in seen["www.lowes.com"] and "cookie" not in seen["www.lowes.com"]


def test_each_proxy_gets_its_own_client():
    f = Fetcher(settings(**{"walmart.com": RetailerAccess(proxy_url="http://p1:1")}))
    assert f.access_for("www.walmart.com").proxy_url == "http://p1:1"
    assert f.access_for("www.lowes.com").proxy_url is None
    assert f._client("http://p1:1") is f._client("http://p1:1")
    assert f._client("http://p1:1") is not f._client(None)
    asyncio.run(f.aclose())


def test_concurrency_and_interval_limits():
    in_flight = peak = 0
    starts = []

    async def handler(request):
        nonlocal in_flight, peak
        starts.append(time.monotonic())
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, text="ok")

    access = {"walmart.com": RetailerAccess(concurrency=3, min_interval=0.01)}
    f = Fetcher(settings(**access), transport=httpx.MockTransport(handler))

    async def run():
        t0 = time.monotonic()
        await asyncio.gather(*(f.get(f"https://www.walmart.com/ip/{i}", use_cache=False) for i in range(9)))
        await f.aclose()
        return t0

    t0 = asyncio.run(run())
    assert peak == 3
    # Request k may not start before k intervals have passed (individual gaps jitter with scheduling).
    for k, started in enumerate(sorted(starts)):
        assert started - t0 >= k * 0.01


def test_cache_is_bounded():
    f = Fetcher(settings(), transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")))

    async def run():
        for i in range(MAX_CACHE_ENTRIES + 5):
            await f.get(f"https://www.walmart.com/ip/{i}")
        await f.get("https://www.walmart.com/ip/x", use_cache=False)
        await f.aclose()

    asyncio.run(run())
    assert len(f._cache) == MAX_CACHE_ENTRIES and "https://www.walmart.com/ip/x" not in f._cache
