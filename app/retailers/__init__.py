from ..models import Retailer
from .base import ParseError, RetailerScraper
from .lowes import LowesScraper
from .walmart import WalmartScraper

SCRAPERS: dict[Retailer, RetailerScraper] = {
    Retailer.walmart: WalmartScraper(),
    Retailer.lowes: LowesScraper(),
}

__all__ = ["SCRAPERS", "ParseError", "RetailerScraper"]
