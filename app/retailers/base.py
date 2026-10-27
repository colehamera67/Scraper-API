from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

from ..models import Product, Retailer


class ParseError(Exception):
    """Page fetched fine but didn't contain the data we expected (layout change or wrong page)."""


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("price", "value", "amount"):
            if key in value:
                return to_float(value[key])
        return None
    if isinstance(value, str):
        m = re.search(r"\d[\d,]*\.?\d*", value)
        return float(m.group().replace(",", "")) if m else None
    return None


def to_int(value: Any) -> int | None:
    f = to_float(value)
    return int(f) if f is not None else None


def first(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def walk(obj: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere inside obj."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def extract_json_assignment(html: str, marker: str) -> Any:
    """Parse the JSON object that follows `marker` (e.g. `window.__STATE__ =`) in a page."""
    idx = html.find(marker)
    if idx == -1:
        raise ParseError(f"marker {marker!r} not found")
    start = html.find("{", idx + len(marker))
    if start == -1:
        raise ParseError(f"no JSON object after {marker!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, start)
    except json.JSONDecodeError as e:
        raise ParseError(f"invalid JSON after {marker!r}: {e}") from e
    return obj


def extract_script_json(html: str, script_id: str) -> Any:
    m = re.search(rf'<script[^>]*id="{re.escape(script_id)}"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ParseError(f"<script id={script_id!r}> not found")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ParseError(f"invalid JSON in #{script_id}: {e}") from e


class RetailerScraper(ABC):
    retailer: Retailer
    base_url: str
    domain: str
    # Matches a product page URL; group 1 is the product id.
    product_url_re: re.Pattern

    def is_product_url(self, url: str) -> bool:
        return self.owns_url(url) and bool(self.product_url_re.search(urlparse(url).path))

    def product_id_from_url(self, url: str) -> str | None:
        m = self.product_url_re.search(urlparse(url).path)
        return m.group(1) if m else None

    def owns_url(self, url: str) -> bool:
        """True for https links on this retailer's domain (guards against fetching arbitrary URLs)."""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return parsed.scheme == "https" and (host == self.domain or host.endswith("." + self.domain))

    def cookies(self) -> dict[str, str] | None:
        return None

    @abstractmethod
    def search_url(self, query: str, page: int = 1) -> str: ...

    @abstractmethod
    def product_url(self, product_id: str) -> str: ...

    @abstractmethod
    def parse_search(self, html: str) -> list[Product]: ...

    @abstractmethod
    def parse_product(self, html: str) -> Product: ...

    def parse_any(self, html: str) -> list[Product]:
        """Parse a page whose type is unknown (search, category, clearance, or product page)."""
        try:
            products = self.parse_search(html)
            if products:
                return products
        except ParseError:
            pass
        return [self.parse_product(html)]
