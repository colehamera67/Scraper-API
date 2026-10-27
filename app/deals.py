from __future__ import annotations

from collections.abc import Iterable

from .models import Deal, Product

FLAG_BONUS = {"clearance": 10.0, "flash_deal": 7.0, "rollback": 5.0, "special_buy": 5.0, "reduced_price": 0.0}
COMPUTED_FIELDS = {"savings", "discount_pct", "best_discount_pct"}


def unit_savings(p: Product) -> float:
    """Dollars saved per unit, from the markdown or the best offer, whichever is larger."""
    if p.current_price is None:
        return 0.0
    markdown = p.savings or 0.0
    best_offer = max((o.effective_discount_pct or 0.0 for o in p.offers), default=0.0)
    return max(markdown, p.current_price * best_offer / 100)


def deal_score(p: Product) -> float:
    """Best % off (markdown or multi-buy offer) dominates; dollars saved and deal badges break ties."""
    pct = p.best_discount_pct or 0.0
    savings_bonus = min(unit_savings(p) / 10, 20.0)
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
        # A requested badge (e.g. clearance) on an item whose discount can't be computed still counts;
        # when the discount is known, min_discount applies.
        flagged_unknown = bool(required & set(p.flags)) and p.best_discount_pct is None
        if (p.best_discount_pct or 0.0) < min_discount and not flagged_unknown:
            continue
        if unit_savings(p) < min_savings:
            continue
        if max_price is not None and (p.current_price is None or p.current_price > max_price):
            continue
        if in_stock_only and p.in_stock is False:
            continue
        if required and not required & set(p.flags):
            continue
        deals.append(Deal(**p.model_dump(exclude=COMPUTED_FIELDS), deal_score=deal_score(p)))
    deals.sort(key=lambda d: d.deal_score, reverse=True)
    return deals
