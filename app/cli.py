"""Command-line entry points for scheduled runs (cron, systemd timers, CI schedules).

    python -m app.cli ingest-feed walmart_feed.csv --retailer walmart --source walmart-affiliate
    python -m app.cli scrape targets.json
    python -m app.cli drops --min-drop 25 --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import settings
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

    async def run():
        fetcher = Fetcher()
        try:
            return await scrape_targets(fetcher, targets)
        finally:
            await fetcher.aclose()

    products, errors = asyncio.run(run())
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
