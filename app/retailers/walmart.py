from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from ..models import Product, Retailer
from ..promotions import promo_texts, with_offers
from .base import ParseError, RetailerScraper, extract_script_json, first, to_float, to_int, walk

# Walmart badge / display-code names normalised to our flag vocabulary.
FLAG_ALIASES = {
    "clearance": "clearance",
    "rollback": "rollback",
    "reducedprice": "reduced_price",
    "reduced_price": "reduced_price",
    "specialbuy": "special_buy",
    "special_buy": "special_buy",
    "flashdeal": "flash_deal",
    "flash_deal": "flash_deal",
}


def _norm_flag(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return FLAG_ALIASES.get(re.sub(r"[\s-]", "", raw).lower())


class WalmartScraper(RetailerScraper):
    retailer = Retailer.walmart
    base_url = "https://www.walmart.com"
    domain = "walmart.com"
    product_url_re = re.compile(r"^/ip/(?:[^/]+/)?(\d+)/?$")

    def search_url(self, query: str, page: int = 1) -> str:
        url = f"{self.base_url}/search?q={quote_plus(query)}"
        return url + (f"&page={page}" if page > 1 else "")

    def product_url(self, product_id: str) -> str:
        return f"{self.base_url}/ip/{quote_plus(product_id)}"

    def _next_data(self, html: str) -> dict:
        return extract_script_json(html, "__NEXT_DATA__")

    def parse_search(self, html: str) -> list[Product]:
        data = self._next_data(html)
        initial = data.get("props", {}).get("pageProps", {}).get("initialData") or data
        products: dict[str, Product] = {}
        for node in walk(initial):
            if "usItemId" in node and "priceInfo" in node and node.get("name"):
                p = self._to_product(node)
                if p and p.product_id not in products:
                    products[p.product_id] = p
        return list(products.values())

    def parse_product(self, html: str) -> Product:
        data = self._next_data(html)
        node = (
            data.get("props", {}).get("pageProps", {}).get("initialData", {}).get("data", {}).get("product")
        )
        if not node:
            node = next((n for n in walk(data) if "usItemId" in n and "priceInfo" in n), None)
        product = self._to_product(node) if node else None
        if not product:
            raise ParseError("no Walmart product data found on page")
        return product

    def _to_product(self, n: dict) -> Product | None:
        item_id = n.get("usItemId")
        if not item_id:
            return None
        price_info = n.get("priceInfo") or {}

        current = to_float(first(price_info, "currentPrice", "linePrice", "itemPrice")) or to_float(n.get("price"))
        was = to_float(first(price_info, "wasPrice", "listPrice", "strikethroughPrice"))
        if was is not None and current is not None and was <= current:
            was = None

        flags: set[str] = set()
        for code, on in (price_info.get("priceDisplayCodes") or {}).items():
            if on is True and (f := _norm_flag(code)):
                flags.add(f)
        for badge in ((n.get("badges") or {}).get("flags") or []):
            if isinstance(badge, dict) and (f := _norm_flag(badge.get("key") or badge.get("text"))):
                flags.add(f)
        for key in ("flag", "badge"):
            if f := _norm_flag(n.get(key)):
                flags.add(f)
        if was is not None and not flags:
            flags.add("reduced_price")

        avail = n.get("availabilityStatusV2") or n.get("availabilityStatus")
        if isinstance(avail, dict):
            avail = avail.get("value")
        in_stock = None if avail is None else str(avail).upper() == "IN_STOCK"

        image = (n.get("imageInfo") or {}).get("thumbnailUrl") or n.get("image")
        url = n.get("canonicalUrl")

        product = Product(
            retailer=self.retailer,
            product_id=str(item_id),
            title=n.get("name", "").strip(),
            url=urljoin(self.base_url, url) if url else self.product_url(str(item_id)),
            brand=n.get("brand") or None,
            image_url=image if isinstance(image, str) else None,
            current_price=current,
            was_price=was,
            in_stock=in_stock,
            rating=to_float(n.get("averageRating")),
            review_count=to_int(n.get("numberOfReviews")),
            flags=sorted(flags),
        )
        return with_offers(product, promo_texts(n))
