from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from .config import settings
from .crawler import CrawlQueue
from .deals import find_deals
from .drops import PriceAnalysis, analyze, find_drops
from .fetcher import BlockedError, Fetcher, FetchError
from .history import Observation, PriceStore, TrackedProduct, utcnow
from .models import DealsResponse, Product, Retailer, SearchResponse
from .retailers import SCRAPERS, ParseError
from .sources import FeedError, Target, parse_feed, scrape_targets

RetailerChoice = Literal["all", "walmart", "lowes"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tests install their own fetcher/store before startup.
    if not hasattr(app.state, "fetcher"):
        app.state.fetcher = Fetcher()
    if not hasattr(app.state, "store"):
        app.state.store = PriceStore(settings.db_path, settings.min_record_interval)
    yield
    await app.state.fetcher.aclose()
    app.state.store.close()
    del app.state.fetcher, app.state.store


app = FastAPI(
    title="Retail Deals Scraper API",
    description="Search Walmart and Lowe's, track price history, and surface real price drops.",
    version="2.0.0",
    lifespan=lifespan,
)


def _retailers(choice: RetailerChoice) -> list[Retailer]:
    return list(Retailer) if choice == "all" else [Retailer(choice)]


def _error_message(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def _record(request: Request, products: list[Product], source: str) -> int:
    if not settings.record_history or not products:
        return 0
    return request.app.state.store.record(products, source)


async def _search_one(fetcher: Fetcher, retailer: Retailer, query: str, page: int) -> list[Product]:
    scraper = SCRAPERS[retailer]
    html = await fetcher.get(scraper.search_url(query, page), cookies=scraper.cookies())
    return scraper.parse_search(html)


async def _gather(
    fetcher: Fetcher, retailers: list[Retailer], queries: list[str], pages: int
) -> tuple[list[Product], dict[str, str]]:
    jobs = [(r, q, pg) for q in queries for r in retailers for pg in range(1, pages + 1)]
    results = await asyncio.gather(
        *(_search_one(fetcher, r, q, pg) for r, q, pg in jobs), return_exceptions=True
    )
    products: list[Product] = []
    errors: dict[str, str] = {}
    for (r, q, pg), res in zip(jobs, results):
        if isinstance(res, BaseException):
            if not isinstance(res, (FetchError, ParseError)):
                raise res
            errors[f"{r.value}:{q}:page{pg}"] = _error_message(res)
        else:
            products.extend(res)
    return products, errors


@app.get("/crawl/status")
def crawl_status(request: Request) -> dict:
    """Catalog coverage per retailer: known product URLs, how many were crawled in the last 24h, etc."""
    queue = CrawlQueue(request.app.state.store.path)
    try:
        return queue.status()
    finally:
        queue.close()


@app.get("/health")
async def health(request: Request) -> dict:
    return {"status": "ok", "history": request.app.state.store.stats()}


@app.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: Annotated[str, Query(min_length=1, description="Search term")],
    retailer: RetailerChoice = "all",
    page: Annotated[int, Query(ge=1, le=20)] = 1,
) -> SearchResponse:
    """All products for a query, deals or not."""
    fetcher: Fetcher = request.app.state.fetcher
    jobs = _retailers(retailer)
    results = await asyncio.gather(*(_search_one(fetcher, r, q, page) for r in jobs), return_exceptions=True)
    products: list[Product] = []
    errors: dict[str, str] = {}
    for r, res in zip(jobs, results):
        if isinstance(res, (FetchError, ParseError)):
            errors[r.value] = _error_message(res)
        elif isinstance(res, BaseException):
            raise res
        else:
            products.extend(res)
    _record(request, products, "scrape:search")
    return SearchResponse(query=q, count=len(products), products=products, errors=errors)


@app.get("/deals", response_model=DealsResponse)
async def deals(
    request: Request,
    q: Annotated[list[str], Query(min_length=1, description="One or more search terms (repeat ?q=)")],
    retailer: RetailerChoice = "all",
    pages: Annotated[int, Query(ge=1, le=5, description="Result pages to scan per query")] = 1,
    min_discount: Annotated[float, Query(ge=0, le=100, description="Minimum % off")] = 20,
    min_savings: Annotated[float, Query(ge=0, description="Minimum $ off")] = 0,
    max_price: Annotated[float | None, Query(gt=0)] = None,
    in_stock_only: bool = False,
    flag: Annotated[
        list[str], Query(description="Only items with these badges: clearance, rollback, reduced_price, ...")
    ] = [],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> DealsResponse:
    """Search one or more terms and return items discounted against the retailer's was-price, best first."""
    products, errors = await _gather(request.app.state.fetcher, _retailers(retailer), q, pages)
    _record(request, products, "scrape:search")
    found = find_deals(
        products,
        min_discount=min_discount,
        min_savings=min_savings,
        max_price=max_price,
        in_stock_only=in_stock_only,
        required_flags=flag,
    )[:limit]
    return DealsResponse(queries=q, count=len(found), deals=found, errors=errors)


@app.get("/product/{retailer}", response_model=Product)
async def product(
    request: Request,
    retailer: Retailer,
    id: Annotated[str | None, Query(description="Retailer item id (Walmart usItemId / Lowe's item id)")] = None,
    url: Annotated[str | None, Query(description="Full product page URL (alternative to id)")] = None,
) -> Product:
    scraper = SCRAPERS[retailer]
    if url:
        if not scraper.owns_url(url):
            raise HTTPException(422, f"url must be an https {scraper.domain} link")
    elif id:
        url = scraper.product_url(id)
    else:
        raise HTTPException(422, "provide id or url")
    try:
        html = await request.app.state.fetcher.get(url, cookies=scraper.cookies())
        result = scraper.parse_product(html)
    except BlockedError as e:
        raise HTTPException(503, str(e))
    except FetchError as e:
        raise HTTPException(502, str(e))
    except ParseError as e:
        raise HTTPException(502, f"could not parse product page: {e}")
    _record(request, [result], "scrape:product")
    return result


@app.post("/parse/{retailer}", response_model=DealsResponse)
async def parse_html(
    request: Request,
    retailer: Retailer,
    html: Annotated[str, Body(media_type="text/html", description="Raw HTML of a search, category or product page")],
    min_discount: Annotated[float, Query(ge=0, le=100)] = 0,
) -> DealsResponse:
    """Parse a page you fetched yourself (saved from your browser, another tool, etc.)."""
    try:
        products = SCRAPERS[retailer].parse_any(html)
    except ParseError as e:
        raise HTTPException(422, f"could not parse page: {e}")
    _record(request, products, "parse")
    found = find_deals(products, min_discount=min_discount)
    return DealsResponse(queries=[], count=len(found), deals=found)


class IngestResult(BaseModel):
    products: int
    observations_recorded: int
    errors: dict[str, str] = {}


MAX_REPORTED_ERRORS = 50


@app.post("/ingest/feed", response_model=IngestResult)
async def ingest_feed(
    request: Request,
    body: Annotated[str, Body(media_type="text/csv", description="CSV with a header row, or JSON Lines")],
    format: Literal["csv", "jsonl"] = "csv",
    retailer: Annotated[Retailer | None, Query(description="Retailer for rows without a retailer column")] = None,
    source: Annotated[str, Query(max_length=64)] = "feed",
) -> IngestResult:
    """Import a product/price feed (affiliate catalog export, data-vendor file, etc.) into price history."""
    products: list[Product] = []
    errors: dict[str, str] = {}
    for line, res in parse_feed(body, format, retailer):
        if isinstance(res, FeedError):
            if len(errors) < MAX_REPORTED_ERRORS:
                errors[f"line {line}"] = str(res)
        else:
            products.append(res)
    recorded = request.app.state.store.record(products, f"feed:{source}") if products else 0
    return IngestResult(products=len(products), observations_recorded=recorded, errors=errors)


@app.post("/ingest/scrape", response_model=IngestResult)
async def ingest_scrape(
    request: Request, targets: Annotated[list[Target], Body(description="Searches or page URLs to scrape")]
) -> IngestResult:
    """Scrape a list of targets (e.g. clearance and deals pages) and record every product found."""
    products, errors = await scrape_targets(request.app.state.fetcher, targets)
    recorded = request.app.state.store.record(products, "scrape:targets") if products else 0
    return IngestResult(products=len(products), observations_recorded=recorded, errors=errors)


class DropsResponse(BaseModel):
    count: int
    drops: list[PriceAnalysis]


@app.get("/drops", response_model=DropsResponse)
def drops(
    request: Request,
    retailer: RetailerChoice = "all",
    min_drop_pct: Annotated[float, Query(ge=0, le=100, description="Minimum % below median or previous price")] = 20,
    min_history_days: Annotated[int, Query(ge=1, description="Days of history needed for median and new-low signals")] = 3,
    lookback_days: Annotated[int, Query(ge=1, le=730)] = 90,
    seen_within_hours: Annotated[float, Query(gt=0, description="Only products observed this recently")] = 48,
    max_price: Annotated[float | None, Query(gt=0)] = None,
    in_stock_only: bool = False,
    signal: Annotated[
        list[str], Query(description="Require any of: below_median, price_cut, new_low, new_clearance, new_rollback")
    ] = [],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> DropsResponse:
    """Real price drops: products whose latest price is well below their own recorded history."""
    found = find_drops(
        request.app.state.store,
        retailer=None if retailer == "all" else Retailer(retailer),
        min_drop_pct=min_drop_pct,
        min_history_days=min_history_days,
        lookback_days=lookback_days,
        seen_within_hours=seen_within_hours,
        max_price=max_price,
        in_stock_only=in_stock_only,
        required_signals=signal,
        limit=limit,
    )
    return DropsResponse(count=len(found), drops=found)


class HistoryResponse(BaseModel):
    product: TrackedProduct
    analysis: PriceAnalysis | None
    observations: list[Observation]


@app.get("/history/{retailer}/{product_id}", response_model=HistoryResponse)
def history(
    request: Request, retailer: Retailer, product_id: str, days: Annotated[int, Query(ge=1, le=730)] = 90
) -> HistoryResponse:
    """Recorded prices for one product, plus how its latest price compares with that history."""
    store: PriceStore = request.app.state.store
    tracked = store.product(retailer, product_id)
    if not tracked:
        raise HTTPException(404, "product has no recorded history")
    observations = store.history(retailer, product_id, since=utcnow() - timedelta(days=days))
    return HistoryResponse(
        product=tracked,
        analysis=analyze(tracked, observations) if observations else None,
        observations=observations,
    )
