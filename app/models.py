from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, computed_field


class Retailer(str, Enum):
    walmart = "walmart"
    lowes = "lowes"


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
        default_factory=list, description="Retailer deal badges, e.g. clearance, rollback, reduced_price."
    )

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
