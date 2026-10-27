# Retail Deals Scraper API

A FastAPI service that searches **Walmart** and **Lowe's**, normalizes the results, and surfaces
major discounts (rollbacks, clearance, price cuts), ranked by a deal score.

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
| GET | `/health` | Liveness |

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

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `SCRAPER_MIN_INTERVAL` | `2` | Minimum seconds between requests to the same site |
| `SCRAPER_CACHE_TTL` | `900` | Seconds to cache a fetched page |
| `SCRAPER_TIMEOUT` | `20` | Request timeout in seconds |
| `SCRAPER_USER_AGENT` | Chrome UA | User-Agent header |
| `SCRAPER_PROXY_URL` | none | Outbound HTTP proxy (`HTTPS_PROXY` also works) |
| `LOWES_STORE_ID` | none | Lowe's store number. Lowe's prices and clearance vary by store |

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
