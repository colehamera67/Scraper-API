"""Full-catalog crawling: discover every product URL from the retailer's sitemaps, then visit them in
least-recently-crawled order so repeated runs cycle through the whole catalog."""

from __future__ import annotations

import asyncio
import gzip
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from urllib.robotparser import RobotFileParser

from pydantic import BaseModel

from .fetcher import BlockedError, Fetcher, FetchError
from .history import PriceStore, to_ts, utcnow
from .models import Product, Retailer
from .retailers import SCRAPERS, ParseError, RetailerScraper

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_urls (
    retailer      TEXT NOT NULL,
    url           TEXT NOT NULL,
    product_id    TEXT,
    discovered_at TEXT NOT NULL,
    lastmod       TEXT,
    last_crawled  TEXT,
    last_status   TEXT,
    failures      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (retailer, url)
);
CREATE INDEX IF NOT EXISTS crawl_order ON crawl_urls (retailer, last_crawled);
"""
# A URL is retired after this many failed visits (removed product, page that never parses).
MAX_FAILURES = 3
DB_BATCH = 1000


class CrawlError(Exception):
    pass


class CrawlQueue:
    """Every known product URL per retailer, and when each was last visited. Lives in the history database."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def add(self, retailer: Retailer, urls: Iterable[tuple[str, str | None, str | None]]) -> int:
        """Add (url, product_id, lastmod) rows. Returns how many URLs were new."""
        now = to_ts(utcnow())
        rows = [(retailer.value, url, pid, now, lastmod) for url, pid, lastmod in urls]
        with self._lock, self._db:
            new = self._db.executemany(
                """INSERT INTO crawl_urls (retailer, url, product_id, discovered_at, lastmod) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (retailer, url) DO NOTHING""",
                rows,
            ).rowcount
            self._db.executemany(
                "UPDATE crawl_urls SET lastmod = ? WHERE retailer = ? AND url = ? AND lastmod IS NOT ?",
                [(lastmod, r, url, lastmod) for r, url, _, _, lastmod in rows if lastmod],
            )
        return new

    def next_batch(self, retailer: Retailer, limit: int) -> list[str]:
        """Never-visited URLs first, then the ones visited longest ago."""
        with self._lock:
            rows = self._db.execute(
                """SELECT url FROM crawl_urls WHERE retailer = ? AND failures < ?
                   ORDER BY last_crawled IS NOT NULL, last_crawled LIMIT ?""",
                (retailer.value, MAX_FAILURES, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def mark(self, retailer: Retailer, results: Iterable[tuple[str, str]]) -> None:
        """Record (url, status) outcomes. 'ok' resets failures; anything else but 'blocked' counts as a failure,
        so removed or disallowed pages retire after MAX_FAILURES visits. 'blocked' leaves the URL first in line."""
        now = to_ts(utcnow())
        with self._lock, self._db:
            for url, status in results:
                if status == "blocked":
                    self._db.execute(
                        "UPDATE crawl_urls SET last_status = ? WHERE retailer = ? AND url = ?",
                        (status, retailer.value, url),
                    )
                else:
                    self._db.execute(
                        """UPDATE crawl_urls SET last_crawled = ?, last_status = ?,
                             failures = CASE WHEN ? = 'ok' THEN 0 ELSE failures + 1 END
                           WHERE retailer = ? AND url = ?""",
                        (now, status, status, retailer.value, url),
                    )

    def status(self) -> dict[str, dict]:
        day_ago = to_ts(utcnow() - timedelta(days=1))
        out = {}
        with self._lock:
            for r in Retailer:
                row = self._db.execute(
                    """SELECT COUNT(*),
                              SUM(last_crawled IS NULL),
                              SUM(last_crawled >= ?),
                              SUM(failures >= ?),
                              MIN(last_crawled)
                       FROM crawl_urls WHERE retailer = ?""",
                    (day_ago, MAX_FAILURES, r.value),
                ).fetchone()
                out[r.value] = {
                    "known_urls": row[0],
                    "never_crawled": row[1] or 0,
                    "crawled_last_24h": row[2] or 0,
                    "retired": row[3] or 0,
                    "oldest_crawl": row[4],
                }
        return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(body: bytes) -> tuple[list[str], list[tuple[str, str | None]]]:
    """Return (child sitemap URLs, [(page url, lastmod)]) from a sitemap or sitemap index (gzip or plain)."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise CrawlError(f"not a sitemap XML document: {e}") from e
    children, pages = [], []
    for entry in root:
        fields = {_local(child.tag): (child.text or "").strip() for child in entry}
        if not fields.get("loc"):
            continue
        if _local(entry.tag) == "sitemap":
            children.append(fields["loc"])
        elif _local(entry.tag) == "url":
            pages.append((fields["loc"], fields.get("lastmod") or None))
    return children, pages


class Robots:
    def __init__(self, text: str | None):
        """`text` None means robots.txt doesn't exist, which allows everything."""
        self._parser = RobotFileParser()
        self._parser.parse((text or "").splitlines())
        self.sitemaps: list[str] = (self._parser.site_maps() or []) if text else []

    def allows(self, user_agent: str, url: str) -> bool:
        return self._parser.can_fetch(user_agent, url)


async def load_robots(fetcher: Fetcher, scraper: RetailerScraper) -> Robots:
    try:
        return Robots(await fetcher.get(f"{scraper.base_url}/robots.txt", use_cache=False))
    except FetchError as e:
        if e.status_code in (404, 410):
            return Robots(None)
        raise CrawlError(f"couldn't read {scraper.domain} robots.txt: {e}") from e


class DiscoverReport(BaseModel):
    retailer: Retailer
    sitemaps_read: int
    product_urls_found: int
    new_urls: int
    stopped_because: str | None = None
    errors: dict[str, str] = {}


async def discover(
    fetcher: Fetcher,
    queue: CrawlQueue,
    retailer: Retailer,
    *,
    sitemaps: Iterable[str] = (),
    sitemap_filter: str | None = None,
    max_sitemaps: int | None = None,
    block_limit: int = 5,
) -> DiscoverReport:
    """Walk the retailer's sitemaps and add every product URL to the crawl queue.

    Sitemaps come from `sitemaps`, else the retailer's configured list, else robots.txt. `sitemap_filter`
    skips child sitemaps whose URL doesn't contain that text (e.g. "ip" to follow only product sitemaps).
    Stops after `block_limit` blocked sitemap requests in a row.
    """
    scraper = SCRAPERS[retailer]
    access = fetcher.access_for(scraper.domain)
    pending = list(sitemaps) or list(access.sitemaps)
    if not pending:
        pending = (await load_robots(fetcher, scraper)).sitemaps
        if not pending:
            raise CrawlError(f"{scraper.domain} robots.txt lists no sitemaps; pass sitemap URLs explicitly")

    seen: set[str] = set()
    read = found = new = blocks_in_row = 0
    errors: dict[str, str] = {}
    stopped_because = None
    while pending and (max_sitemaps is None or read < max_sitemaps):
        if blocks_in_row >= block_limit:
            stopped_because = f"blocked {block_limit} times in a row"
            break
        url = pending.pop(0)
        if url in seen or not scraper.owns_url(url):
            continue
        seen.add(url)
        try:
            children, pages = parse_sitemap(await fetcher.get_bytes(url))
        except (FetchError, CrawlError, OSError, EOFError) as e:
            errors[url] = f"{type(e).__name__}: {e}"
            blocks_in_row = blocks_in_row + 1 if isinstance(e, BlockedError) else 0
            continue
        blocks_in_row = 0
        read += 1
        pending += [c for c in children if not sitemap_filter or sitemap_filter in c]
        products = [(u, scraper.product_id_from_url(u), lastmod) for u, lastmod in pages if scraper.is_product_url(u)]
        found += len(products)
        for i in range(0, len(products), DB_BATCH):
            new += queue.add(retailer, products[i : i + DB_BATCH])
    return DiscoverReport(
        retailer=retailer, sitemaps_read=read, product_urls_found=found, new_urls=new,
        stopped_because=stopped_because, errors=errors,
    )


class CrawlReport(BaseModel):
    retailer: Retailer
    pages_visited: int = 0
    products: int = 0
    observations_recorded: int = 0
    blocked: int = 0
    failed: int = 0
    skipped_by_robots: int = 0
    stopped_because: str = "queue empty"
    seconds: float = 0.0
    pages_per_minute: float = 0.0
    sample_errors: dict[str, str] = {}


async def crawl(
    fetcher: Fetcher,
    queue: CrawlQueue,
    store: PriceStore,
    retailer: Retailer,
    *,
    max_pages: int | None = None,
    max_minutes: float | None = None,
    respect_robots: bool = True,
    block_limit: int = 20,
) -> CrawlReport:
    """Visit queued product pages (least recently crawled first), recording every product's price.

    Stops at `max_pages`, after `max_minutes`, when the queue runs out, or after `block_limit` blocked
    responses in a row. A blocked page isn't retried with any workaround; it stays first in line for the
    next run.
    """
    scraper = SCRAPERS[retailer]
    access = fetcher.access_for(scraper.domain)
    robots = await load_robots(fetcher, scraper) if respect_robots else None
    report = CrawlReport(retailer=retailer)
    started = time.monotonic()
    deadline = started + max_minutes * 60 if max_minutes else None
    batch_size = max(access.concurrency * 4, 20)
    blocks_in_row = 0
    visited_this_run: set[str] = set()

    async def visit(url: str) -> tuple[str, str, Product | None]:
        nonlocal blocks_in_row
        if robots and not robots.allows(fetcher.settings.user_agent, url):
            return url, "robots_disallowed", None
        if blocks_in_row >= block_limit or (deadline and time.monotonic() > deadline):
            return url, "not_attempted", None
        try:
            html = await fetcher.get(url, cookies=scraper.cookies(), use_cache=False)
            product = scraper.parse_product(html)
        except BlockedError as e:
            blocks_in_row += 1
            _note(report, url, e)
            return url, "blocked", None
        except FetchError as e:
            blocks_in_row = 0
            _note(report, url, e)
            return url, "gone" if e.status_code in (404, 410) else "error", None
        except ParseError as e:
            blocks_in_row = 0
            _note(report, url, e)
            return url, "parse_error", None
        blocks_in_row = 0
        return url, "ok", product

    while True:
        if max_pages is not None and report.pages_visited >= max_pages:
            report.stopped_because = "max pages reached"
            break
        if deadline and time.monotonic() > deadline:
            report.stopped_because = "time limit reached"
            break
        limit = batch_size if max_pages is None else min(batch_size, max_pages - report.pages_visited)
        urls = [u for u in queue.next_batch(retailer, limit) if u not in visited_this_run]
        if not urls:
            break
        visited_this_run.update(urls)
        results = await asyncio.gather(*(visit(u) for u in urls))

        products = [p for _, _, p in results if p]
        if products:
            report.observations_recorded += store.record(products, "crawl")
        queue.mark(retailer, [(u, s) for u, s, _ in results if s != "not_attempted"])
        for _, status, _ in results:
            if status == "not_attempted":
                continue
            if status == "robots_disallowed":
                report.skipped_by_robots += 1
                continue
            report.pages_visited += 1
            if status == "ok":
                report.products += 1
            elif status == "blocked":
                report.blocked += 1
            else:
                report.failed += 1
        if blocks_in_row >= block_limit:
            report.stopped_because = f"blocked {block_limit} times in a row"
            break

    report.seconds = round(time.monotonic() - started, 1)
    if report.seconds:
        report.pages_per_minute = round(report.pages_visited / report.seconds * 60, 1)
    return report


def _note(report: CrawlReport, url: str, error: Exception) -> None:
    if len(report.sample_errors) < 20:
        report.sample_errors[url] = f"{type(error).__name__}: {error}"
