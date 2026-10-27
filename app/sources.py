"""Inputs to the price-history store: data feeds (CSV / JSON Lines) and scrape targets."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .fetcher import Fetcher, FetchError
from .models import Product, Retailer
from .promotions import with_offers
from .retailers import SCRAPERS, ParseError
from .retailers.base import to_float, to_int

# Product field -> accepted feed column names (common affiliate / data-vendor naming). Headers are lowercased
# and spaces/hyphens become underscores before matching, so "Sale Price" matches sale_price.
FEED_COLUMNS = {
    "product_id": ("product_id", "id", "item_id", "itemid", "usitemid", "sku", "omniitemid"),
    "title": ("title", "name", "product_name", "productname", "description"),
    "current_price": ("current_price", "price", "sale_price", "saleprice", "selling_price", "sellingprice"),
    "was_price": ("was_price", "wasprice", "list_price", "listprice", "msrp", "regular_price", "retail_price"),
    "url": ("url", "link", "product_url", "producturl"),
    "brand": ("brand", "brand_name", "brandname", "manufacturer"),
    "image_url": ("image_url", "imageurl", "image", "image_link", "thumbnail_image"),
    "in_stock": ("in_stock", "instock", "availability", "stock", "available"),
    "rating": ("rating", "average_rating", "customer_rating"),
    "review_count": ("review_count", "reviews", "num_reviews", "number_of_reviews"),
    "flags": ("flags", "badges", "deal_type"),
    "promotion": ("promotion", "promotions", "promo", "promo_text", "offer", "offers", "special_offer", "deal"),
    "retailer": ("retailer", "store", "merchant"),
}
IN_STOCK_WORDS = {"1", "true", "yes", "y", "in stock", "instock", "in_stock", "available", "limited stock"}
OUT_OF_STOCK_WORDS = {"0", "false", "no", "n", "out of stock", "outofstock", "out_of_stock", "unavailable"}


class FeedError(Exception):
    pass


def _lookup(lowered: dict, field: str):
    for name in FEED_COLUMNS[field]:
        value = lowered.get(name)
        if value not in (None, ""):
            return value
    return None


def _stock(value) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower() if value is not None else ""
    if text in IN_STOCK_WORDS:
        return True
    if text in OUT_OF_STOCK_WORDS:
        return False
    return None


def _flags(value) -> list[str]:
    if isinstance(value, list):
        items = value
    elif value:
        items = str(value).replace(";", ",").split(",")
    else:
        items = []
    return sorted({str(f).strip().lower().replace(" ", "_") for f in items if str(f).strip()})


def _retailer(value, default: Retailer | None) -> Retailer:
    if value:
        text = str(value).strip().lower().replace("'", "").replace("’", "")
        for r in Retailer:
            if r.value in text:
                return r
    if default:
        return default
    raise FeedError("row has no recognisable retailer column; pass a default retailer")


def row_to_product(row: dict, default_retailer: Retailer | None = None) -> Product:
    row = {re.sub(r"[\s-]+", "_", str(k).strip().lower()): v for k, v in row.items() if k is not None}
    product_id, title = _lookup(row, "product_id"), _lookup(row, "title")
    if product_id is None or title is None:
        raise FeedError("row is missing a product id or title")
    current, was = to_float(_lookup(row, "current_price")), to_float(_lookup(row, "was_price"))
    product = Product(
        retailer=_retailer(_lookup(row, "retailer"), default_retailer),
        product_id=str(product_id).strip(),
        title=str(title).strip(),
        url=_lookup(row, "url"),
        brand=_lookup(row, "brand"),
        image_url=_lookup(row, "image_url"),
        current_price=current,
        was_price=was if was is not None and current is not None and was > current else None,
        in_stock=_stock(_lookup(row, "in_stock")),
        rating=to_float(_lookup(row, "rating")),
        review_count=to_int(_lookup(row, "review_count")),
        flags=_flags(_lookup(row, "flags")),
    )
    promo = _lookup(row, "promotion")
    texts = promo if isinstance(promo, list) else str(promo).split("|") if promo else []
    return with_offers(product, [str(t) for t in texts])


def parse_feed(
    text: str, fmt: Literal["csv", "jsonl"], default_retailer: Retailer | None = None
) -> Iterator[tuple[int, Product | FeedError]]:
    """Yield (line number, product or the error for that row). Bad rows don't stop the import."""
    if fmt == "csv":
        rows = enumerate(csv.DictReader(io.StringIO(text)), start=2)
    else:
        rows = ((n, line) for n, line in enumerate(text.splitlines(), start=1) if line.strip())
    for n, row in rows:
        try:
            if fmt == "jsonl":
                row = json.loads(row)
                if not isinstance(row, dict):
                    raise FeedError("line is not a JSON object")
            yield n, row_to_product(row, default_retailer)
        except (FeedError, json.JSONDecodeError, ValueError) as e:
            yield n, FeedError(str(e))


class Target(BaseModel):
    """One page (or search) to scrape on each run."""

    retailer: Retailer
    query: str | None = None
    url: str | None = None
    pages: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def _check(self) -> Target:
        if bool(self.query) == bool(self.url):
            raise ValueError("a target needs exactly one of query or url")
        if self.url and not SCRAPERS[self.retailer].owns_url(self.url):
            raise ValueError(f"url must be an https {SCRAPERS[self.retailer].domain} link")
        return self

    def urls(self) -> list[str]:
        if self.url:
            return [self.url]
        return [SCRAPERS[self.retailer].search_url(self.query, pg) for pg in range(1, self.pages + 1)]


def load_targets(text: str) -> list[Target]:
    data = json.loads(text)
    return [Target(**t) for t in (data["targets"] if isinstance(data, dict) else data)]


async def scrape_targets(fetcher: Fetcher, targets: list[Target]) -> tuple[list[Product], dict[str, str]]:
    """Fetch every target page. Failures are collected per URL instead of aborting the run."""
    jobs = [(t, url) for t in targets for url in t.urls()]

    async def one(target: Target, url: str) -> list[Product]:
        scraper = SCRAPERS[target.retailer]
        return scraper.parse_any(await fetcher.get(url, cookies=scraper.cookies()))

    results = await asyncio.gather(*(one(t, u) for t, u in jobs), return_exceptions=True)
    products: list[Product] = []
    errors: dict[str, str] = {}
    for (_, url), res in zip(jobs, results):
        if isinstance(res, (FetchError, ParseError)):
            errors[url] = f"{type(res).__name__}: {res}"
        elif isinstance(res, BaseException):
            raise res
        else:
            products.extend(res)
    return products, errors
