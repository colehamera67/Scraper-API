from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, computed_field


class Retailer(str, Enum):
    walmart = "walmart"
    lowes = "lowes"


OfferKind = Literal["buy_x_get_y", "multi_for_price", "qty_percent_off", "qty_dollar_off", "free_gift"]


class Offer(BaseModel):
    """A multi-buy promotion such as "Buy 1 Get 1 Free", "Buy 2 Get 1 50% Off" or "3 for $10"."""

    kind: OfferKind
    text: str = Field(description="The promotion text as the retailer shows it.")
    min_qty: int | None = Field(default=None, description="Units you have to buy to get the deal.")
    buy_qty: int | None = None
    get_qty: int | None = None
    get_discount_pct: float | None = Field(default=None, description="Discount on the 'get' units; 100 = free.")
    bundle_qty: int | None = None
    bundle_price: float | None = None
    percent_off: float | None = None
    dollar_off: float | None = None
    effective_discount_pct: float | None = Field(
        default=None, description="Average % off per unit when you buy min_qty; None when it can't be computed."
    )


class Product(BaseModel):
    retailer: Retailer
    product_id: str
    title: str
    url: str | None = None
    brand: str | None = None
    image_url: str | None = None
    current_price: float | None = None
    was_price: float | None = Field(
        default=None, description="Previous / list price the retailer shows next to the current price."
    )
    in_stock: bool | None = None
    rating: float | None = None
    review_count: int | None = None
    flags: list[str] = Field(
        default_factory=list,
        description="Deal badges, e.g. clearance, rollback, reduced_price, bogo, multi_buy, free_gift.",
    )
    offers: list[Offer] = Field(default_factory=list, description="Multi-buy promotions on this item.")

    @computed_field
    @property
    def savings(self) -> float | None:
        if self.current_price is None or self.was_price is None or self.was_price <= self.current_price:
            return None
        return round(self.was_price - self.current_price, 2)

    @computed_field
    @property
    def discount_pct(self) -> float | None:
        if self.savings is None or not self.was_price:
            return None
        return round(self.savings / self.was_price * 100, 1)

    @computed_field
    @property
    def best_discount_pct(self) -> float | None:
        """The larger of the markdown (vs was-price) and the best multi-buy offer's per-unit discount."""
        candidates = [self.discount_pct] + [o.effective_discount_pct for o in self.offers]
        values = [c for c in candidates if c is not None]
        return max(values) if values else None


class SearchResponse(BaseModel):
    query: str
    count: int
    products: list[Product]
    errors: dict[str, str] = Field(default_factory=dict)


class Deal(Product):
    deal_score: float


class DealsResponse(BaseModel):
    queries: list[str]
    count: int
    deals: list[Deal]
    errors: dict[str, str] = Field(default_factory=dict)
