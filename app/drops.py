from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from statistics import median

from pydantic import BaseModel

from .history import Observation, PriceStore, TrackedProduct, utcnow
from .models import Offer, Retailer

DEAL_FLAGS = ("clearance", "rollback", "flash_deal", "special_buy", "reduced_price", "bogo", "multi_buy", "free_gift")
# Signals that on their own make a product worth reporting.
DROP_SIGNALS = {"below_median", "price_cut", "new_clearance", "new_bogo", "new_multi_buy"}
# A new low has to beat the previous low by at least this fraction to count.
LOW_THRESHOLD = 0.01


class PriceAnalysis(BaseModel):
    retailer: Retailer
    product_id: str
    title: str
    url: str | None = None
    brand: str | None = None
    image_url: str | None = None
    current_price: float
    observed_at: datetime
    in_stock: bool | None = None
    flags: list[str] = []
    offers: list[Offer] = []
    was_price: float | None = None
    previous_price: float | None = None
    median_price: float | None = None
    lowest_price: float | None = None
    history_days: int = 0
    drop_vs_median_pct: float | None = None
    drop_vs_previous_pct: float | None = None
    new_low: bool = False
    new_flags: list[str] = []
    signals: list[str] = []
    drop_score: float = 0.0


def _pct_below(reference: float | None, current: float) -> float | None:
    if not reference or reference <= 0:
        return None
    return round((reference - current) / reference * 100, 1)


def daily_closes(observations: Iterable[Observation]) -> list[float]:
    """Last price seen each UTC day, so frequent sampling on one day doesn't skew the median."""
    by_day: dict = {}
    for o in observations:
        by_day[o.observed_at.date()] = o.price
    return list(by_day.values())


def analyze(
    product: TrackedProduct, series: list[Observation], *, min_drop_pct: float = 20, min_history_days: int = 3
) -> PriceAnalysis:
    """Compare the latest observation to the product's own price history. `series` is oldest-first."""
    latest, prior = series[-1], series[:-1]
    a = PriceAnalysis(
        **product.model_dump(include={"retailer", "product_id", "title", "url", "brand", "image_url"}),
        current_price=latest.price,
        observed_at=latest.observed_at,
        in_stock=latest.in_stock,
        flags=latest.flags,
        offers=latest.offers,
        was_price=latest.was_price,
    )
    if not prior:
        return a

    closes = daily_closes(prior)
    a.history_days = len(closes)
    a.previous_price = prior[-1].price
    a.median_price = round(median(closes), 2)
    a.lowest_price = min(o.price for o in prior)
    a.drop_vs_median_pct = _pct_below(a.median_price, latest.price)
    a.drop_vs_previous_pct = _pct_below(a.previous_price, latest.price)
    a.new_flags = sorted(f for f in set(latest.flags) - set(prior[-1].flags) if f in DEAL_FLAGS)

    enough_history = a.history_days >= min_history_days
    a.new_low = enough_history and latest.price < a.lowest_price * (1 - LOW_THRESHOLD)

    if enough_history and (a.drop_vs_median_pct or 0) >= min_drop_pct:
        a.signals.append("below_median")
    if (a.drop_vs_previous_pct or 0) >= min_drop_pct:
        a.signals.append("price_cut")
    if a.new_low:
        a.signals.append("new_low")
    a.signals += [f"new_{f}" for f in a.new_flags]

    offer_pct = max((o.effective_discount_pct or 0 for o in latest.offers), default=0)
    pct = max(a.drop_vs_median_pct or 0, a.drop_vs_previous_pct or 0, offer_pct if a.new_flags else 0, 0)
    dollars = max((a.median_price or 0) - latest.price, (a.previous_price or 0) - latest.price, 0)
    bonus = (10 if a.new_low else 0) + (10 if "clearance" in a.new_flags else 0)
    a.drop_score = round(pct + min(dollars / 10, 20) + bonus, 2)
    return a


def is_drop(a: PriceAnalysis) -> bool:
    return bool(DROP_SIGNALS & set(a.signals))


def find_drops(
    store: PriceStore,
    *,
    retailer: Retailer | None = None,
    min_drop_pct: float = 20,
    min_history_days: int = 3,
    lookback_days: int = 90,
    seen_within_hours: float = 48,
    max_price: float | None = None,
    in_stock_only: bool = False,
    required_signals: Iterable[str] = (),
    limit: int = 100,
) -> list[PriceAnalysis]:
    """Products whose latest price is a real drop compared with their own recorded history."""
    required = set(required_signals)
    seen_within = timedelta(hours=seen_within_hours)
    fresh_after = utcnow() - seen_within
    found: list[PriceAnalysis] = []
    for product, series in store.iter_series(
        retailer=retailer, seen_within=seen_within, lookback=timedelta(days=lookback_days) + seen_within
    ):
        if series[-1].observed_at < fresh_after:
            continue
        a = analyze(product, series, min_drop_pct=min_drop_pct, min_history_days=min_history_days)
        if not is_drop(a) or (required and not required & set(a.signals)):
            continue
        if max_price is not None and a.current_price > max_price:
            continue
        if in_stock_only and a.in_stock is False:
            continue
        found.append(a)
    found.sort(key=lambda a: a.drop_score, reverse=True)
    return found[:limit]
