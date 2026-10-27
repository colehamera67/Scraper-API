"""Command-line entry points for scheduled runs (cron, systemd timers, CI schedules).

    python -m app.cli ingest-feed walmart_feed.csv --retailer walmart --source walmart-affiliate
    python -m app.cli scrape targets.json
    python -m app.cli drops --min-drop 25 --limit 20
    python -m app.cli deals --flag bogo --min-discount 30
    python -m app.cli discover walmart
    python -m app.cli crawl walmart --max-minutes 60
    python -m app.cli crawl-status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

from .config import settings
from .crawler import CrawlError, CrawlQueue, crawl, discover
from .deals import find_deals
from .drops import find_drops
from .fetcher import Fetcher
from .history import PriceStore
from .models import Retailer
from .sources import FeedError, load_targets, parse_feed, scrape_targets

BATCH_SIZE = 5000


def _store() -> PriceStore:
    return PriceStore(settings.db_path, settings.min_record_interval)


def cmd_ingest_feed(args) -> int:
    path = Path(args.file)
    fmt = args.format or ("jsonl" if path.suffix in (".jsonl", ".ndjson") else "csv")
    retailer = Retailer(args.retailer) if args.retailer else None
    store = _store()
    batch, products, recorded, bad = [], 0, 0, 0
    for line, res in parse_feed(path.read_text(encoding="utf-8-sig"), fmt, retailer):
        if isinstance(res, FeedError):
            bad += 1
            if bad <= 20:
                print(f"line {line}: {res}", file=sys.stderr)
            continue
        batch.append(res)
        if len(batch) >= BATCH_SIZE:
            recorded += store.record(batch, f"feed:{args.source}")
            products += len(batch)
            batch = []
    if batch:
        recorded += store.record(batch, f"feed:{args.source}")
        products += len(batch)
    store.close()
    print(f"{products} products, {recorded} new price observations, {bad} bad rows")
    return 0 if products or not bad else 1


def cmd_scrape(args) -> int:
    targets = load_targets(Path(args.targets).read_text())

    products, errors = _run_with_fetcher(lambda f: scrape_targets(f, targets))
    store = _store()
    recorded = store.record(products, "scrape:targets") if products else 0
    store.close()
    for url, err in errors.items():
        print(f"{url}: {err}", file=sys.stderr)
    print(f"{len(products)} products, {recorded} new price observations, {len(errors)} failed pages")
    return 0 if products or not errors else 1


def cmd_drops(args) -> int:
    store = _store()
    found = find_drops(
        store,
        retailer=Retailer(args.retailer) if args.retailer else None,
        min_drop_pct=args.min_drop,
        min_history_days=args.min_history_days,
        max_price=args.max_price,
        in_stock_only=args.in_stock_only,
        limit=args.limit,
    )
    store.close()
    if args.json:
        print(json.dumps([d.model_dump(mode="json") for d in found], indent=2))
        return 0
    for d in found:
        ref = d.median_price if "below_median" in d.signals else d.previous_price
        print(
            f"{d.drop_score:6.1f}  {d.retailer.value:7}  ${d.current_price:>9.2f}  (was ~${ref or 0:,.2f})  "
            f"{','.join(d.signals):32}  {d.title[:60]}  {d.url or ''}"
        )
    if not found:
        print("no drops found")
    return 0


def _deal_line(d) -> str:
    offer = d.offers[0].text if d.offers else ""
    was = f"was ${d.was_price:,.2f}" if d.was_price else ""
    return (
        f"{d.deal_score:6.1f}  {d.retailer.value:7}  ${d.current_price or 0:>9.2f}  {was:16}  "
        f"{(d.best_discount_pct or 0):5.1f}% off  {','.join(d.flags):28}  {offer[:40]:40}  {d.title[:50]}  {d.url or ''}"
    )


def cmd_deals(args) -> int:
    store = _store()
    products = store.latest(
        retailer=Retailer(args.retailer) if args.retailer else None, seen_within=timedelta(hours=args.seen_within_hours)
    )
    store.close()
    found = find_deals(
        products, min_discount=args.min_discount, max_price=args.max_price,
        in_stock_only=args.in_stock_only, required_flags=args.flag,
    )[: args.limit]
    if args.json:
        print(json.dumps([d.model_dump(mode="json") for d in found], indent=2))
    else:
        for d in found:
            print(_deal_line(d))
        if not found:
            print("no deals found")
    return 0


def _run_with_fetcher(make_coro):
    async def run():
        fetcher = Fetcher()
        try:
            return await make_coro(fetcher)
        finally:
            await fetcher.aclose()

    return asyncio.run(run())


def cmd_discover(args) -> int:
    queue = CrawlQueue(settings.db_path)
    try:
        report = _run_with_fetcher(
            lambda f: discover(
                f, queue, Retailer(args.retailer),
                sitemaps=args.sitemap, sitemap_filter=args.filter, max_sitemaps=args.max_sitemaps,
                block_limit=settings.crawl_block_limit,
            )
        )
    except CrawlError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        queue.close()
    for url, err in report.errors.items():
        print(f"{url}: {err}", file=sys.stderr)
    print(
        f"{report.sitemaps_read} sitemaps read, {report.product_urls_found} product URLs, "
        f"{report.new_urls} new, {len(report.errors)} sitemap errors"
        + (f" (stopped: {report.stopped_because})" if report.stopped_because else "")
    )
    return 0 if report.sitemaps_read else 1


def cmd_crawl(args) -> int:
    queue, store = CrawlQueue(settings.db_path), _store()
    try:
        report = _run_with_fetcher(
            lambda f: crawl(
                f, queue, store, Retailer(args.retailer),
                max_pages=args.max_pages, max_minutes=args.max_minutes,
                respect_robots=settings.respect_robots, block_limit=settings.crawl_block_limit,
            )
        )
    except CrawlError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        queue.close()
        store.close()
    for url, err in report.sample_errors.items():
        print(f"{url}: {err}", file=sys.stderr)
    print(json.dumps(report.model_dump(mode="json", exclude={"sample_errors"}), indent=2))
    return 0 if report.products or not report.pages_visited else 1


def cmd_crawl_status(args) -> int:
    queue = CrawlQueue(settings.db_path)
    print(json.dumps(queue.status(), indent=2))
    queue.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest-feed", help="import a CSV / JSON Lines product feed into price history")
    p.add_argument("file")
    p.add_argument("--format", choices=["csv", "jsonl"], help="default: from file extension")
    p.add_argument("--retailer", choices=[r.value for r in Retailer], help="for feeds without a retailer column")
    p.add_argument("--source", default="feed", help="label stored with each observation")
    p.set_defaults(func=cmd_ingest_feed)

    p = sub.add_parser("scrape", help="scrape the pages listed in a targets JSON file")
    p.add_argument("targets")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("drops", help="list current price drops")
    p.add_argument("--retailer", choices=[r.value for r in Retailer])
    p.add_argument("--min-drop", type=float, default=20, help="minimum %% drop (default 20)")
    p.add_argument("--min-history-days", type=int, default=3)
    p.add_argument("--max-price", type=float)
    p.add_argument("--in-stock-only", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_drops)

    retailers = [r.value for r in Retailer]
    p = sub.add_parser("deals", help="best current deals (markdowns, clearance, BOGO, multi-buy) from recorded data")
    p.add_argument("--retailer", choices=retailers)
    p.add_argument("--min-discount", type=float, default=20, help="minimum %% off, markdown or per-unit offer")
    p.add_argument("--flag", action="append", default=[], help="require a flag: clearance, rollback, bogo, multi_buy, ...")
    p.add_argument("--max-price", type=float)
    p.add_argument("--in-stock-only", action="store_true")
    p.add_argument("--seen-within-hours", type=float, default=48)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_deals)

    p = sub.add_parser("discover", help="find every product URL in a retailer's sitemaps and queue it")
    p.add_argument("retailer", choices=retailers)
    p.add_argument("--sitemap", action="append", default=[], help="sitemap URL (repeatable); default: config or robots.txt")
    p.add_argument("--filter", help="only follow child sitemaps whose URL contains this text")
    p.add_argument("--max-sitemaps", type=int, help="stop after reading this many sitemap files")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("crawl", help="visit queued product pages, least recently crawled first")
    p.add_argument("retailer", choices=retailers)
    p.add_argument("--max-pages", type=int)
    p.add_argument("--max-minutes", type=float)
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("crawl-status", help="catalog coverage per retailer")
    p.set_defaults(func=cmd_crawl_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
