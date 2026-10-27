# Retail Deals Scraper API

A FastAPI service that searches **Walmart** and **Lowe's**, normalizes the results, and surfaces
major discounts (rollbacks, clearance, price cuts), ranked by a deal score.

It also keeps a **price history** of every product it sees (from scraping, parsed pages, or imported
data feeds) and flags **real price drops**: prices well below what that product has actually sold for,
instead of trusting the retailer's often-inflated "was" price.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# interactive docs: http://localhost:8000/docs
pytest
```

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/deals?q=drill&q=mower` | Searches each term on each retailer and returns only discounted items, best first |
| GET | `/search?q=drill&retailer=lowes` | All products for a term, discounted or not |
| GET | `/product/{walmart\|lowes}?id=...` or `?url=...` | One product page |
| POST | `/parse/{walmart\|lowes}` | Parse HTML you send (body = raw page HTML, `Content-Type: text/html`) |
| GET | `/drops` | Real price drops based on recorded history, best first |
| GET | `/history/{retailer}/{product_id}` | Recorded prices for one product, plus how today's price compares |
| POST | `/ingest/feed?retailer=walmart&source=affiliate` | Import a CSV or JSON Lines product feed (`&format=jsonl`) |
| POST | `/ingest/scrape` | Scrape a JSON list of targets and record the results |
| GET | `/health` | Liveness and history database counts |

### `/deals` filters

- `retailer`: `all` (default), `walmart`, or `lowes`
- `min_discount`: minimum % off (default `20`)
- `min_savings`: minimum $ off
- `max_price`: highest current price to include
- `in_stock_only`: drop items marked out of stock
- `flag`: only items with these badges (repeatable): `clearance`, `rollback`, `reduced_price`, `special_buy`, `flash_deal`.
  An item with one of these badges is included even when the page shows no was-price.
- `pages`: result pages to scan per term (1–5)
- `limit`: maximum number of results

Example: `GET /deals?q=dewalt&q=pressure%20washer&min_discount=40&flag=clearance&in_stock_only=true`

Each deal has `current_price`, `was_price`, `savings`, `discount_pct`, `flags`, `in_stock`, `rating`,
`url`, and `deal_score` (= % off + min($ saved / 10, 20) + badge bonus: clearance +10, flash deal +7,
rollback/special buy +5).

Partial failures don't fail the request: if one retailer blocks or errors, the other's results are still
returned and the problem shows up under `errors`.

## Price history and drop detection

Everything the API sees is stored in a SQLite database (`data/prices.db`): `/search`, `/deals`,
`/product`, `/parse`, and both `/ingest` endpoints. An observation identical to the previous one is
skipped if it arrives within 30 minutes, so repeated runs don't bloat the database.

`/drops` compares each product's latest price with its own history over the last 90 days and reports these
signals:

| Signal | Meaning |
|---|---|
| `below_median` | At least `min_drop_pct` (default 20%) below the product's median daily price. Needs `min_history_days` (default 3) of history |
| `price_cut` | At least `min_drop_pct` below the previous observation |
| `new_low` | More than 1% below the lowest recorded price. Needs `min_history_days` of history |
| `new_clearance`, `new_rollback`, ... | The deal badge appeared since the previous observation |

A product counts as a drop when it has `below_median`, `price_cut` or `new_clearance`. `new_low` raises the
score. The median uses one closing price per day, so checking a product ten times on one day doesn't
outweigh the other days. Only products observed in the last `seen_within_hours` (default 48) are included,
so you don't get stale results.

`drop_score` = the larger of % below median and % below previous price, + min($ drop / 10, 20),
+10 for a new low, +10 for newly marked clearance.

Other `/drops` filters: `retailer`, `max_price`, `in_stock_only`, `signal` (repeatable), `lookback_days`,
and `limit`.

### Data sources

**Feeds (recommended for full catalogs).** Affiliate catalog exports and retail-data vendors deliver
CSV or JSON Lines files. Import them with:

```bash
python -m app.cli ingest-feed walmart_catalog.csv --retailer walmart --source walmart-affiliate
# or: curl --data-binary @feed.csv -H 'Content-Type: text/csv' 'localhost:8000/ingest/feed?retailer=walmart&source=vendor'
```

Column names are matched flexibly, case-insensitively, with spaces treated as underscores:

| Field | Accepted column names |
|---|---|
| id | `id`, `sku`, `item_id`, `itemId`, `usItemId`, `omniItemId` |
| title | `name`, `title`, `product_name` |
| price | `price`, `sale_price`, `salePrice`, `selling_price` |
| was price | `msrp`, `list_price`, `was_price`, `regular_price`, `retail_price` |
| other | `url`/`link`, `brand`, `image_url`, `availability`/`in_stock`, `rating`, `review_count`, `badges`/`flags`, `retailer` |

Bad rows are reported and skipped; they don't stop the import. To support another feed's column names,
add them to `FEED_COLUMNS` in `app/sources.py`.

**Targeted scraping.** List the deal-heavy pages worth watching (clearance searches, rollback pages,
favorite brands) in a targets file. See `targets.example.json`:

```bash
python -m app.cli scrape targets.json
```

The same blocking limits described below apply here.

**Full-catalog crawling.** This covers every product, not just deal pages. See
[Full-catalog crawling](#full-catalog-crawling) below.

### Scheduling

Run imports and scrapes on a schedule, for example with cron:

```cron
15 */6 * * *  cd /srv/scraper-api && python -m app.cli scrape targets.json
45 5 * * *    cd /srv/scraper-api && python -m app.cli ingest-feed /feeds/latest.csv --source vendor
0 7 * * *     cd /srv/scraper-api && python -m app.cli drops --min-drop 25 --limit 50
```

`python -m app.cli drops --json` outputs the results as JSON, so you can pipe them into email, Slack, etc.

### Scale

On a single machine, importing a 200,000-product feed takes about 15 seconds, and a drop scan over
3 days of history (600,000 observations) takes about 30 seconds. The database grows by about 30 MB per
200,000 observations. That works for hundreds of thousands of products refreshed several times a day.
For tens of millions, move the store to Postgres. `PriceStore` is the only module that touches the
database.

## Full-catalog crawling

Crawling has two steps, and both are resumable:

```bash
python -m app.cli discover walmart --filter ip     # sitemaps -> every product URL, saved in the database
python -m app.cli crawl walmart --max-minutes 55    # visit product pages, record prices
python -m app.cli crawl-status                      # coverage: known URLs, crawled in last 24h, retired
```

- **discover** reads the retailer's sitemaps (the lists of pages a site publishes for crawlers), found
  through robots.txt, `WALMART_SITEMAPS`/`LOWES_SITEMAPS`, or `--sitemap URL`. It follows sitemap indexes
  and gzip files and keeps only product page URLs (`/ip/...` on Walmart, `/pd/...` on Lowe's).
  `--filter` follows only child sitemaps whose URL contains that text, for example the product
  sitemaps. Re-run it daily to pick up new products; already-known URLs are skipped.
- **crawl** visits never-crawled URLs first, then the ones crawled longest ago. Running it repeatedly
  works through the whole catalog, then starts over. Each run stops at `--max-pages`, `--max-minutes`, an
  empty queue, or after `SCRAPER_CRAWL_BLOCK_LIMIT` (default 20) blocked responses in a row. Blocked pages
  aren't retried with any workaround; they stay first in line for the next run. A URL that fails 3 times
  (removed product, unparseable page, disallowed by robots.txt) is retired.
- Every product crawled goes into the price history, so `/drops` covers the whole catalog.

How often each product gets refreshed depends on your speed settings:
`pages per day ≈ 86,400 / interval`, with enough concurrency to keep that pace. For example,
`WALMART_MIN_INTERVAL=0.05` and `WALMART_CONCURRENCY=10` is about 1.7M pages a day. Raise these only as far
as the retailer has approved. Unapproved traffic at these speeds gets blocked quickly.

Example schedule that runs back-to-back hourly crawl passes:

```cron
5 3 * * *   cd /srv/scraper-api && python -m app.cli discover walmart --filter ip && python -m app.cli discover lowes
0 * * * *   cd /srv/scraper-api && python -m app.cli crawl walmart --max-minutes 55
0 * * * *   cd /srv/scraper-api && python -m app.cli crawl lowes --max-minutes 55
```

## Approved access

If a retailer has approved your traffic, configure it per retailer with the env vars below. Use the
`WALMART_` or `LOWES_` prefix:

| Var | Example | Purpose |
|---|---|---|
| `<RETAILER>_HEADERS` | `{"X-Partner-Token": "..."}` | Extra headers on every request to that retailer (API keys, partner tokens) |
| `<RETAILER>_COOKIES` | `{"session": "..."}` | Cookies on every request to that retailer |
| `<RETAILER>_PROXY_URL` | `http://user:pass@10.0.0.5:3128` | Fixed outbound proxy, for example the static IP the retailer allowlisted |
| `<RETAILER>_MIN_INTERVAL` | `0.1` | Seconds between request starts (default: `SCRAPER_MIN_INTERVAL`) |
| `<RETAILER>_CONCURRENCY` | `10` | Requests in flight at once (default 1) |
| `<RETAILER>_SITEMAPS` | `https://www.walmart.com/sitemap_ip.xml` | Comma-separated sitemap URLs for `discover` |

Headers and cookies are only sent to their own retailer's domain. Ask your contact at each retailer which
method they use (an allowlisted IP, a token, a header). Without it, both sites block automated traffic,
whatever the speed settings.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `SCRAPER_MIN_INTERVAL` | `2` | Default seconds between request starts to the same site |
| `SCRAPER_CACHE_TTL` | `900` | Seconds to cache a fetched page |
| `SCRAPER_TIMEOUT` | `20` | Request timeout in seconds |
| `SCRAPER_USER_AGENT` | Chrome UA | User-Agent header |
| `SCRAPER_PROXY_URL` | none | Outbound HTTP proxy (`HTTPS_PROXY` also works) |
| `LOWES_STORE_ID` | none | Lowe's store number. Lowe's prices and clearance vary by store |
| `SCRAPER_DB` | `data/prices.db` | Price history database path |
| `SCRAPER_RECORD_HISTORY` | `1` | Set to `0` to stop `/search`, `/deals`, etc. from recording history |
| `SCRAPER_MIN_RECORD_INTERVAL` | `1800` | Seconds within which an identical repeat observation is skipped |
| `SCRAPER_RESPECT_ROBOTS` | `1` | Skip pages robots.txt disallows. Set to `0` only if the retailer told you to ignore it |
| `SCRAPER_CRAWL_BLOCK_LIMIT` | `20` | Stop a crawl or discovery run after this many blocked responses in a row |

## How it works

- **Walmart** pages include a `__NEXT_DATA__` JSON blob. The parser walks it for item records
  (`usItemId` + `priceInfo`) and reads `currentPrice`, `wasPrice`, `priceDisplayCodes`
  (rollback/clearance/reduced price), badges, and availability. Search, browse/category, clearance, and
  product pages all use the same item shape, so `/parse` works on any of them.
- **Lowe's** pages include `window.__PRELOADED_STATE__`. The parser finds records keyed by `omniItemId`
  and reads selling price, was/retail price, clearance markers, and stock status from each one.

## Known limitations

- **Bot protection.** Both sites block automated traffic, especially from cloud and datacenter IPs. When
  that happens, the API returns a `BlockedError` and does not try to get around it. If you're blocked:
  run it from a normal residential connection, slow down with `SCRAPER_MIN_INTERVAL`, or save pages from
  your browser and POST them to `/parse`.
- **Official data source.** Walmart has an affiliate product API (Walmart I/O) that returns prices
  without scraping, which is more reliable for heavy use.
- **Terms of service.** Both retailers' terms limit automated access. Check them before you use this at
  scale.
- **Page layout changes.** The parsers read the sites' embedded JSON and look for several alternative key
  names, but a site redesign can still break them. When that happens you get a `ParseError` rather than
  wrong data.
