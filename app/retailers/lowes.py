from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from ..config import settings
from ..models import Product, Retailer
from .base import ParseError, RetailerScraper, extract_json_assignment, first, to_float, to_int, walk

CURRENT_PRICE_KEYS = ("sellingPrice", "finalPrice", "currentPrice", "salePrice", "price")
WAS_PRICE_KEYS = ("wasPrice", "retailPrice", "originalPrice", "regularPrice", "listPrice", "strikePrice")
BADGE_TEXT = {
    "clearance": "clearance",
    "save now": "reduced_price",
    "special value": "special_buy",
    "deal of the day": "flash_deal",
}


def _find_number(entry: dict, keys: tuple[str, ...]) -> float | None:
    """First positive number found under any of `keys` (in priority order) anywhere inside entry."""
    nodes = list(walk(entry))
    for key in keys:
        for node in nodes:
            if key in node and (v := to_float(node[key])) and v > 0:
                return v
    return None


def _flags(entry: dict) -> set[str]:
    flags: set[str] = set()
    for node in walk(entry):
        for key, value in node.items():
            if value is True and "clearance" in key.lower():
                flags.add("clearance")
            elif isinstance(value, str) and (f := BADGE_TEXT.get(value.strip().lower())):
                flags.add(f)
    return flags


def _in_stock(entry: dict) -> bool | None:
    for node in walk(entry):
        for key in ("isInStock", "inStock", "isAvailable"):
            if isinstance(node.get(key), bool):
                return node[key]
    return None


class LowesScraper(RetailerScraper):
    retailer = Retailer.lowes
    base_url = "https://www.lowes.com"
    domain = "lowes.com"
    product_url_re = re.compile(r"^/pd/(?:[^/]+/)?(\d+)/?$")
    page_size = 24

    def cookies(self) -> dict[str, str] | None:
        return {"sn": settings.lowes_store_id} if settings.lowes_store_id else None

    def search_url(self, query: str, page: int = 1) -> str:
        url = f"{self.base_url}/search?searchTerm={quote_plus(query)}"
        return url + (f"&offset={(page - 1) * self.page_size}" if page > 1 else "")

    def product_url(self, product_id: str) -> str:
        return f"{self.base_url}/pd/{quote_plus(product_id)}"

    def _state(self, html: str) -> dict:
        state = extract_json_assignment(html, "__PRELOADED_STATE__")
        if not isinstance(state, dict):
            raise ParseError("Lowe's page state is not an object")
        return state

    def _entries(self, root: Any) -> dict[str, dict]:
        """Map omniItemId -> the enclosing record (product info + pricing/location siblings)."""
        entries: dict[str, dict] = {}
        for node in walk(root):
            prod = node.get("product")
            if isinstance(prod, dict) and prod.get("omniItemId"):
                entries.setdefault(str(prod["omniItemId"]), node)
        if not entries:
            for node in walk(root):
                if node.get("omniItemId") and first(node, "description", "title", "productName"):
                    entries.setdefault(str(node["omniItemId"]), node)
        return entries

    def parse_search(self, html: str) -> list[Product]:
        state = self._state(html)
        root = state.get("itemList") or state
        return [p for p in (self._to_product(e) for e in self._entries(root).values()) if p]

    def parse_product(self, html: str) -> Product:
        state = self._state(html)
        entries = self._entries(state.get("productDetails") or state)
        for entry in entries.values():
            if p := self._to_product(entry):
                return p
        raise ParseError("no Lowe's product data found on page")

    def _to_product(self, entry: dict) -> Product | None:
        info = entry["product"] if isinstance(entry.get("product"), dict) else entry
        item_id = info.get("omniItemId")
        title = first(info, "description", "title", "productName")
        if not item_id or not title:
            return None

        current = _find_number(entry, CURRENT_PRICE_KEYS)
        was = _find_number(entry, WAS_PRICE_KEYS)
        if was is not None and (current is None or was <= current):
            was = None
        flags = _flags(entry)
        if was is not None and not flags:
            flags.add("reduced_price")

        image = first(info, "imageUrl", "image")
        if not image and isinstance(info.get("imageUrls"), list) and info["imageUrls"]:
            image = info["imageUrls"][0]
        if isinstance(image, dict):
            image = first(image, "value", "url")
        url = first(info, "pdURL", "pdUrl", "url")

        return Product(
            retailer=self.retailer,
            product_id=str(item_id),
            title=str(title).strip(),
            url=urljoin(self.base_url, url) if url else self.product_url(str(item_id)),
            brand=info.get("brand") or None,
            image_url=image if isinstance(image, str) else None,
            current_price=current,
            was_price=was,
            in_stock=_in_stock(entry),
            rating=to_float(first(info, "rating", "averageRating") or first(entry, "rating", "averageRating")),
            review_count=to_int(first(info, "reviewCount", "totalReviews") or first(entry, "reviewCount", "totalReviews")),
            flags=sorted(flags),
        )
