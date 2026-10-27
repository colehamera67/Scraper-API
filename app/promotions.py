"""Recognise multi-buy promotions in retailer text and work out what they're worth per unit."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .jsonutil import walk
from .models import Offer, Product

NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6"}
# Product-level keys holding product copy rather than promotions; skipped to avoid false matches. Nested
# promotion objects often keep their text under "description" or "title", so those are read there.
SKIP_KEYS = {
    "name", "title", "description", "shortdescription", "longdescription", "productname", "brand",
    "url", "canonicalurl", "pdurl", "image", "imageurl", "thumbnailurl", "seotitle", "seodescription",
}
MAX_TEXT_LEN = 160
OFFER_FLAGS = {"buy_x_get_y": "bogo", "free_gift": "free_gift"}

_NUM = r"(\d+)"
_PRICE = r"\$\s?(\d+(?:\.\d{1,2})?)"
BOGO = re.compile(r"\bbogo\b(?:\s+(?:(\d+)\s*%\s*off|(free)|(half\s+off)))?", re.I)
BUY_GET = re.compile(
    rf"\bbuy\s+{_NUM}\s*(?:,|&|and)?\s*(?:get|and\s+get)\s+{_NUM}"
    rf"(?:\s+(?:more\s+|additional\s+|item\s+|items\s+)?"
    rf"(?:(free)|(half\s+off)|(?:at\s+)?(\d+)\s*%\s*off|for\s+{_PRICE}(?:\s+each)?))?",
    re.I,
)
MULTI_FOR = re.compile(rf"\b{_NUM}\s*(?:for|/)\s*{_PRICE}", re.I)
QTY_PCT = (
    re.compile(rf"\bbuy\s+{_NUM}\s*(?:\+|or\s+more)?\s*,?\s*(?:get|save|take)\s+(\d+(?:\.\d+)?)\s*%(?:\s*off)?", re.I),
    re.compile(rf"\b(?:save|get|take)\s+(\d+(?:\.\d+)?)\s*%\s*(?:off\s+)?when\s+you\s+buy\s+{_NUM}", re.I),
)
QTY_DOLLAR = (
    re.compile(rf"\bbuy\s+{_NUM}\s*(?:\+|or\s+more)?\s*,?\s*(?:get|save|take)\s+{_PRICE}(?:\s*off)?", re.I),
    re.compile(rf"\b(?:save|get|take)\s+{_PRICE}\s*(?:off\s+)?when\s+you\s+buy\s+{_NUM}", re.I),
)
FREE_GIFT = re.compile(r"\b(?:get|with)\s+(?:a|an)\s+free\b|\bfree\s+(?:gift|tool|battery)\b", re.I)


def _normalise(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", lambda m: NUMBER_WORDS[m.group(1).lower()], text, flags=re.I)


def _pct(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return round(min(value, 100.0), 1)


def _buy_get(text: str, buy: int, get: int, discount: float | None, get_price: float | None, unit: float | None) -> Offer:
    if discount is None and get_price is not None and unit:
        discount = max(0.0, (1 - get_price / unit) * 100)
    effective = _pct(get * discount / (buy + get)) if discount is not None and buy + get else None
    return Offer(
        kind="buy_x_get_y", text=text, min_qty=buy + get, buy_qty=buy, get_qty=get,
        get_discount_pct=round(discount, 1) if discount is not None else None, effective_discount_pct=effective,
    )


def parse_offer(raw: str, unit_price: float | None = None) -> Offer | None:
    """Parse one promotion string. `unit_price` is the item's current price, needed for "N for $X" deals."""
    if not raw or len(raw) > MAX_TEXT_LEN:
        return None
    text = raw.strip()
    norm = _normalise(text)

    if m := BUY_GET.search(norm):
        buy, get = int(m.group(1)), int(m.group(2))
        if buy < 1 or get < 1:
            return None
        free, half, pct, price = m.group(3), m.group(4), m.group(5), m.group(6)
        if half:
            discount = 50.0
        elif pct:
            discount = float(pct)
        elif price:
            discount = None
        else:
            # "Buy 1 Get 1" with no qualifier means the extra item is free.
            discount = 100.0
        return _buy_get(text, buy, get, discount, float(price) if price else None, unit_price)

    if m := BOGO.search(norm):
        pct, _free, half = m.groups()
        discount = float(pct) if pct else 50.0 if half else 100.0
        return _buy_get(text, 1, 1, discount, None, unit_price)

    for pattern, qty_first in zip(QTY_PCT, (True, False)):
        if m := pattern.search(norm):
            qty, pct = (m.group(1), m.group(2)) if qty_first else (m.group(2), m.group(1))
            return Offer(
                kind="qty_percent_off", text=text, min_qty=int(qty), percent_off=float(pct),
                effective_discount_pct=_pct(float(pct)),
            )

    for pattern, qty_first in zip(QTY_DOLLAR, (True, False)):
        if m := pattern.search(norm):
            qty, dollars = (int(m.group(1)), float(m.group(2))) if qty_first else (int(m.group(2)), float(m.group(1)))
            effective = _pct(dollars / (qty * unit_price) * 100) if unit_price and qty else None
            return Offer(
                kind="qty_dollar_off", text=text, min_qty=qty, dollar_off=dollars, effective_discount_pct=effective
            )

    if m := MULTI_FOR.search(norm):
        qty, price = int(m.group(1)), float(m.group(2))
        if qty < 2:
            return None
        effective = _pct((1 - price / (qty * unit_price)) * 100) if unit_price else None
        return Offer(
            kind="multi_for_price", text=text, min_qty=qty, bundle_qty=qty, bundle_price=price,
            effective_discount_pct=effective,
        )

    if FREE_GIFT.search(norm):
        return Offer(kind="free_gift", text=text)
    return None


def promo_texts(node: Any) -> list[str]:
    """Short strings anywhere in a product's JSON that could be promotion text."""
    product_level = {id(node)}
    if isinstance(node, dict) and isinstance(node.get("product"), dict):
        product_level.add(id(node["product"]))
    texts: list[str] = []
    for d in walk(node):
        skip = SKIP_KEYS if id(d) in product_level else set()
        for key, value in d.items():
            if isinstance(value, str) and key.lower() not in skip and 4 < len(value) <= MAX_TEXT_LEN:
                texts.append(value)
    return texts


def extract_offers(texts: Iterable[str], unit_price: float | None) -> list[Offer]:
    offers: dict[tuple, Offer] = {}
    for t in texts:
        if (o := parse_offer(t, unit_price)) is None:
            continue
        key = (o.kind, o.min_qty, o.get_qty, o.get_discount_pct, o.bundle_price, o.percent_off, o.dollar_off)
        offers.setdefault(key, o)
    return sorted(offers.values(), key=lambda o: o.effective_discount_pct or 0, reverse=True)


def offer_flags(offers: Iterable[Offer]) -> set[str]:
    return {OFFER_FLAGS.get(o.kind, "multi_buy") for o in offers}


def with_offers(product: Product, texts: Iterable[str]) -> Product:
    """Attach offers found in `texts` to the product and add their flags."""
    offers = extract_offers(texts, product.current_price)
    if not offers:
        return product
    flags = sorted(set(product.flags) | offer_flags(offers))
    return product.model_copy(update={"offers": offers, "flags": flags})
