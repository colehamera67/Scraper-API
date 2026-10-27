from __future__ import annotations

from collections.abc import Iterable

from .models import Deal, Product

FLAG_BONUS = {"clearance": 10.0, "flash_deal": 7.0, "rollback": 5.0, "special_buy": 5.0, "reduced_price": 0.0}


def deal_score(p: Product) -> float:
    """Discount % dominates; absolute savings and retailer deal badges break ties."""
    pct = p.discount_pct or 0.0
    savings_bonus = min((p.savings or 0.0) / 10, 20.0)
    flag_bonus = max((FLAG_BONUS.get(f, 0.0) for f in p.flags), default=0.0)
    return round(pct + savings_bonus + flag_bonus, 2)


def find_deals(
    products: Iterable[Product],
    *,
    min_discount: float = 20.0,
    min_savings: float = 0.0,
    max_price: float | None = None,
    in_stock_only: bool = False,
    required_flags: Iterable[str] = (),
) -> list[Deal]:
    required = set(required_flags)
    deals: list[Deal] = []
    seen: set[tuple[str, str]] = set()
    for p in products:
        key = (p.retailer.value, p.product_id)
        if key in seen:
            continue
        seen.add(key)
        # Products flagged clearance etc. without a visible was-price still count when flags are requested.
        if (p.discount_pct or 0.0) < min_discount and not (required and required & set(p.flags)):
            continue
        if (p.savings or 0.0) < min_savings:
            continue
        if max_price is not None and (p.current_price is None or p.current_price > max_price):
            continue
        if in_stock_only and p.in_stock is False:
            continue
        if required and not required & set(p.flags):
            continue
        deals.append(Deal(**p.model_dump(exclude={"savings", "discount_pct"}), deal_score=deal_score(p)))
    deals.sort(key=lambda d: d.deal_score, reverse=True)
    return deals
