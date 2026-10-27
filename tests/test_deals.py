from app.deals import deal_score, find_deals
from app.models import Product, Retailer


def make(pid, current, was=None, flags=(), in_stock=True):
    return Product(retailer=Retailer.walmart, product_id=pid, title=pid, current_price=current,
                   was_price=was, flags=list(flags), in_stock=in_stock)


def test_filters_and_sorting():
    items = [
        make("small", 90, 100),            # 10%
        make("big", 50, 100),              # 50%
        make("bigger", 30, 100),           # 70%
        make("none", 20),
        make("oos", 10, 100, in_stock=False),
        make("big", 50, 100),              # duplicate
    ]
    deals = find_deals(items, min_discount=20)
    assert [d.product_id for d in deals] == ["oos", "bigger", "big"]
    assert [d.product_id for d in find_deals(items, min_discount=20, in_stock_only=True)] == ["bigger", "big"]
    assert [d.product_id for d in find_deals(items, max_price=40)] == ["oos", "bigger"]
    assert [d.product_id for d in find_deals(items, min_savings=60)] == ["oos", "bigger"]


def test_flag_filter_includes_badged_items_without_was_price():
    items = [make("clr", 10, flags=["clearance"]), make("roll", 50, 100, flags=["rollback"])]
    assert [d.product_id for d in find_deals(items, required_flags=["clearance"])] == ["clr"]


def test_clearance_bonus():
    assert deal_score(make("a", 50, 100, flags=["clearance"])) > deal_score(make("b", 50, 100))
