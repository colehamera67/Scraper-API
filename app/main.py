from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Query, Request

from .deals import find_deals
from .fetcher import BlockedError, Fetcher, FetchError
from .models import DealsResponse, Product, Retailer, SearchResponse
from .retailers import SCRAPERS, ParseError

RetailerChoice = Literal["all", "walmart", "lowes"]
ALLOWED_HOSTS = {Retailer.walmart: "walmart.com", Retailer.lowes: "lowes.com"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.fetcher = getattr(app.state, "fetcher", None) or Fetcher()
    yield
    await app.state.fetcher.aclose()


app = FastAPI(
    title="Retail Deals Scraper API",
    description="Search Walmart and Lowe's and surface major discounts, clearance, and rollbacks.",
    version="1.0.0",
    lifespan=lifespan,
)


def _retailers(choice: RetailerChoice) -> list[Retailer]:
    return list(Retailer) if choice == "all" else [Retailer(choice)]


def _error_message(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


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
    """Search one or more terms and return only discounted items, best deals first."""
    products, errors = await _gather(request.app.state.fetcher, _retailers(retailer), q, pages)
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
        host = urlparse(url).hostname or ""
        if urlparse(url).scheme != "https" or not (
            host == ALLOWED_HOSTS[retailer] or host.endswith("." + ALLOWED_HOSTS[retailer])
        ):
            raise HTTPException(422, f"url must be an https {ALLOWED_HOSTS[retailer]} link")
    elif id:
        url = scraper.product_url(id)
    else:
        raise HTTPException(422, "provide id or url")
    try:
        html = await request.app.state.fetcher.get(url, cookies=scraper.cookies())
        return scraper.parse_product(html)
    except BlockedError as e:
        raise HTTPException(503, str(e))
    except FetchError as e:
        raise HTTPException(502, str(e))
    except ParseError as e:
        raise HTTPException(502, f"could not parse product page: {e}")


@app.post("/parse/{retailer}", response_model=DealsResponse)
async def parse_html(
    retailer: Retailer,
    html: Annotated[str, Body(media_type="text/html", description="Raw HTML of a search, category or product page")],
    min_discount: Annotated[float, Query(ge=0, le=100)] = 0,
) -> DealsResponse:
    """Parse a page you fetched yourself (saved from your browser, another tool, etc.)."""
    try:
        products = SCRAPERS[retailer].parse_any(html)
    except ParseError as e:
        raise HTTPException(422, f"could not parse page: {e}")
    found = find_deals(products, min_discount=min_discount)
    return DealsResponse(queries=[], count=len(found), deals=found)
