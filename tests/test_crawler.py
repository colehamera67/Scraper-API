import asyncio
import gzip

import httpx
import pytest

from app.config import RetailerAccess, Settings
from app.crawler import CrawlError, CrawlQueue, crawl, discover, parse_sitemap
from app.fetcher import Fetcher
from app.history import PriceStore
from app.models import Retailer

from .conftest import fixture

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.walmart.com/sitemap_ip_1.xml.gz</loc></sitemap>
  <sitemap><loc>https://www.walmart.com/sitemap_category.xml</loc></sitemap>
  <sitemap><loc>https://evil.example/sitemap.xml</loc></sitemap>
</sitemapindex>"""
PRODUCTS = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.walmart.com/ip/Drill/111</loc><lastmod>2026-10-01</lastmod></url>
  <url><loc>https://www.walmart.com/ip/Saw/222</loc></url>
  <url><loc>https://www.walmart.com/ip/Gone/404</loc></url>
  <url><loc>https://www.walmart.com/ip/Secret/999</loc></url>
  <url><loc>https://www.walmart.com/browse/tools</loc></url>
</urlset>"""
CATEGORIES = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.walmart.com/cp/tools/1</loc></url>
</urlset>"""
ROBOTS = "User-agent: *\nDisallow: /ip/Secret/\nSitemap: https://www.walmart.com/sitemap_index.xml\n"


def handler(request):
    url = str(request.url)
    if url.endswith("/robots.txt"):
        return httpx.Response(200, text=ROBOTS)
    if url.endswith("sitemap_index.xml"):
        return httpx.Response(200, content=INDEX)
    if url.endswith("sitemap_ip_1.xml.gz"):
        return httpx.Response(200, content=gzip.compress(PRODUCTS), headers={"Content-Type": "application/x-gzip"})
    if url.endswith("sitemap_category.xml"):
        return httpx.Response(200, content=CATEGORIES)
    if url.endswith("/404"):
        return httpx.Response(404)
    if "/ip/" in url:
        return httpx.Response(200, text=fixture("walmart_product.html"))
    return httpx.Response(404)


def blocked(request):
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return httpx.Response(403)


@pytest.fixture
def env(tmp_path):
    queue, store = CrawlQueue(tmp_path / "p.db"), PriceStore(tmp_path / "p.db")
    yield queue, store
    queue.close()
    store.close()


def fetcher(h=handler):
    return Fetcher(Settings(min_interval=0, cache_ttl=0, access={}), transport=httpx.MockTransport(h))


def run(coro_fn, h=handler):
    async def go():
        f = fetcher(h)
        try:
            return await coro_fn(f)
        finally:
            await f.aclose()

    return asyncio.run(go())


def test_parse_sitemap_index_urlset_and_gzip():
    children, pages = parse_sitemap(INDEX)
    assert len(children) == 3 and pages == []
    children, pages = parse_sitemap(gzip.compress(PRODUCTS))
    assert children == [] and pages[0] == ("https://www.walmart.com/ip/Drill/111", "2026-10-01")
    with pytest.raises(CrawlError):
        parse_sitemap(b"<html>blocked</html")


def test_discover_from_robots(env):
    queue, _ = env
    report = run(lambda f: discover(f, queue, Retailer.walmart))
    assert report.sitemaps_read == 3            # index, product sitemap, category sitemap; foreign one skipped
    assert report.product_urls_found == 4 and report.new_urls == 4
    assert report.errors == {}
    again = run(lambda f: discover(f, queue, Retailer.walmart))
    assert again.new_urls == 0 and queue.status()["walmart"]["known_urls"] == 4


def test_discover_filter_and_explicit_sitemaps(env):
    queue, _ = env
    report = run(lambda f: discover(f, queue, Retailer.walmart, sitemaps=["https://www.walmart.com/sitemap_index.xml"],
                                   sitemap_filter="_ip_"))
    assert report.sitemaps_read == 2


def test_discover_without_sitemaps_errors(env):
    queue, _ = env
    with pytest.raises(CrawlError):
        run(lambda f: discover(f, queue, Retailer.lowes), h=lambda r: httpx.Response(404))


def test_crawl_records_prices_and_tracks_outcomes(env):
    queue, store = env
    run(lambda f: discover(f, queue, Retailer.walmart))
    report = run(lambda f: crawl(f, queue, store, Retailer.walmart))
    assert report.stopped_because == "queue empty"
    assert (report.pages_visited, report.products, report.failed, report.skipped_by_robots) == (3, 2, 1, 1)
    assert report.observations_recorded == 1   # both pages are the same fixture product
    assert store.history(Retailer.walmart, "111")[0].source == "crawl"
    status = queue.status()["walmart"]
    assert status["never_crawled"] == 0 and status["crawled_last_24h"] == 4


def test_failing_urls_retire(env):
    queue, store = env
    queue.add(Retailer.walmart, [("https://www.walmart.com/ip/Gone/404", "404", None)])
    for _ in range(3):
        run(lambda f: crawl(f, queue, store, Retailer.walmart, respect_robots=False))
    assert queue.next_batch(Retailer.walmart, 10) == []
    assert queue.status()["walmart"]["retired"] == 1


def test_crawl_order_and_max_pages(env):
    queue, store = env
    queue.add(Retailer.walmart, [(f"https://www.walmart.com/ip/x/{i}", str(i), None) for i in range(5)])
    report = run(lambda f: crawl(f, queue, store, Retailer.walmart, max_pages=2, respect_robots=False))
    assert report.pages_visited == 2 and report.stopped_because == "max pages reached"
    # the three never-crawled URLs come before the two just crawled
    assert queue.next_batch(Retailer.walmart, 3) == [f"https://www.walmart.com/ip/x/{i}" for i in (2, 3, 4)]


def test_crawl_stops_when_blocked_and_keeps_urls_queued(env):
    queue, store = env
    queue.add(Retailer.walmart, [(f"https://www.walmart.com/ip/x/{i}", str(i), None) for i in range(50)])
    report = run(lambda f: crawl(f, queue, store, Retailer.walmart, block_limit=5), h=blocked)
    assert report.stopped_because == "blocked 5 times in a row"
    assert report.products == 0 and report.blocked >= 5
    assert queue.status()["walmart"]["never_crawled"] == 50


def test_crawl_status_endpoint(client):
    body = client.get("/crawl/status").json()
    assert body["walmart"]["known_urls"] == 0


def test_discover_stops_when_blocked(env):
    queue, _ = env
    calls = []

    def h(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=INDEX) if request.url.path == "/idx.xml" else httpx.Response(403)

    many = [f"https://www.walmart.com/s{i}.xml" for i in range(10)]
    report = run(lambda f: discover(f, queue, Retailer.walmart, sitemaps=many, block_limit=3), h=h)
    assert report.stopped_because == "blocked 3 times in a row" and len(calls) == 3
