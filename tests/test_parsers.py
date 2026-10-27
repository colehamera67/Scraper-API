import pytest

from app.retailers import ParseError
from app.retailers.lowes import LowesScraper
from app.retailers.walmart import WalmartScraper

from .conftest import fixture


def by_id(products):
    return {p.product_id: p for p in products}


def test_walmart_search():
    products = by_id(WalmartScraper().parse_search(fixture("walmart_search.html")))
    assert set(products) == {"111", "222", "333"}

    drill = products["111"]
    assert drill.title == "DEWALT 20V Drill Kit"
    assert drill.url == "https://www.walmart.com/ip/DEWALT-20V-Drill-Kit/111"
    assert (drill.current_price, drill.was_price) == (99.0, 179.0)
    assert drill.savings == 80.0 and drill.discount_pct == 44.7
    assert drill.flags == ["rollback"] and drill.in_stock is True
    assert drill.review_count == 1234

    assert products["222"].was_price is None and products["222"].discount_pct is None

    clearance = products["333"]
    assert (clearance.current_price, clearance.was_price) == (40.0, 100.0)
    assert clearance.flags == ["clearance"] and clearance.in_stock is False


def test_walmart_product():
    p = WalmartScraper().parse_product(fixture("walmart_product.html"))
    assert p.product_id == "111" and p.discount_pct == 44.7 and p.in_stock is True


def test_lowes_search():
    products = by_id(LowesScraper().parse_search(fixture("lowes_search.html")))
    assert set(products) == {"5001", "5002", "5003"}

    drill = products["5001"]
    assert drill.url == "https://www.lowes.com/pd/CRAFTSMAN-V20-Drill/5001"
    assert (drill.current_price, drill.was_price) == (59.0, 129.0)
    assert drill.flags == ["reduced_price"] and drill.in_stock is True
    assert drill.image_url == "https://mobileimages.lowes.com/5001.jpg"
    assert (drill.rating, drill.review_count) == (4.5, 88)

    # retailPrice equal to sellingPrice is not a discount
    assert products["5002"].was_price is None and products["5002"].flags == []
    assert products["5003"].flags == ["clearance"] and products["5003"].discount_pct == 50.0


def test_lowes_product():
    p = LowesScraper().parse_product(fixture("lowes_product.html"))
    assert p.product_id == "5001" and (p.current_price, p.was_price) == (59.0, 129.0)


@pytest.mark.parametrize("scraper", [WalmartScraper(), LowesScraper()])
def test_unparseable_page_raises(scraper):
    with pytest.raises(ParseError):
        scraper.parse_search("<html>nothing here</html>")
    with pytest.raises(ParseError):
        scraper.parse_product("<html>nothing here</html>")


def test_urls():
    assert WalmartScraper().search_url("cordless drill", 2) == "https://www.walmart.com/search?q=cordless+drill&page=2"
    assert LowesScraper().search_url("drill", 3) == "https://www.lowes.com/search?searchTerm=drill&offset=48"
