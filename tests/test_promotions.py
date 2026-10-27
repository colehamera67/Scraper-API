import json
from datetime import timedelta

import pytest

from app.deals import find_deals
from app.drops import find_drops
from app.history import utcnow
from app.models import Product, Retailer
from app.promotions import parse_offer, promo_texts, with_offers
from app.retailers.lowes import LowesScraper
from app.retailers.walmart import WalmartScraper
from app.sources import parse_feed


@pytest.mark.parametrize(
    "text, unit, kind, effective",
    [
        ("Buy 1, Get 1 Free", None, "buy_x_get_y", 50.0),
        ("Buy One Get One FREE", None, "buy_x_get_y", 50.0),
        ("BOGO", None, "buy_x_get_y", 50.0),
        ("BOGO 50% off", None, "buy_x_get_y", 25.0),
        ("Buy 1 Get 2 Free", None, "buy_x_get_y", 66.7),
        ("Buy 2 Get 1 Free", None, "buy_x_get_y", 33.3),
        ("Buy 2, Get 1 50% Off", None, "buy_x_get_y", 16.7),
        ("Buy 1 Get 1 Half Off", None, "buy_x_get_y", 25.0),
        ("Buy 1 Get 1 for $1", 10.0, "buy_x_get_y", 45.0),
        ("3 for $10", 5.0, "multi_for_price", 33.3),
        ("2/$5", 2.5, "multi_for_price", None),          # no saving vs single price
        ("2 for $5", None, "multi_for_price", None),     # unknown unit price
        ("Buy 2 or more, save 10%", None, "qty_percent_off", 10.0),
        ("Save 15% when you buy 2", None, "qty_percent_off", 15.0),
        ("Buy 2, Save $10", 25.0, "qty_dollar_off", 20.0),
        ("Save $5 when you buy 3", 10.0, "qty_dollar_off", 16.7),
        ("Buy a drill, get a free battery", 99.0, "free_gift", None),
    ],
)
def test_parse_offer(text, unit, kind, effective):
    offer = parse_offer(text, unit)
    assert offer is not None and offer.kind == kind
    assert offer.effective_discount_pct == effective
    assert offer.text == text


@pytest.mark.parametrize("text", ["Free shipping", "Rollback", "Clearance", "Save with Walmart+", "1 for $5", ""])
def test_not_offers(text):
    assert parse_offer(text, 5.0) is None


def test_buy_get_quantities():
    o = parse_offer("Buy 2 Get 1 Free")
    assert (o.buy_qty, o.get_qty, o.min_qty, o.get_discount_pct) == (2, 1, 3, 100.0)


def test_product_copy_is_ignored_but_nested_promo_description_is_read():
    node = {
        "name": "Snacks Buy 1 Get 1 bundle",          # product title: ignored
        "product": {"description": "3 for $10 value pack"},  # product copy: ignored
        "promotion": {"description": "Buy 2 Get 1 Free"},
    }
    assert promo_texts(node) == ["Buy 2 Get 1 Free"]


def _walmart_page(item):
    data = {"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{"items": [item]}]}}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def test_walmart_item_with_bogo_badge():
    item = {
        "usItemId": "9", "name": "Paper Towels", "priceInfo": {"currentPrice": {"price": 10.0}},
        "badges": {"groups": [{"members": [{"text": "Buy 1, Get 1 Free"}]}]},
    }
    [p] = WalmartScraper().parse_search(_walmart_page(item))
    assert p.flags == ["bogo"] and p.discount_pct is None and p.best_discount_pct == 50.0
    assert p.offers[0].min_qty == 2


def test_lowes_item_with_promotion():
    state = {"itemList": [{
        "product": {"omniItemId": "7", "description": "Mulch 2-cu ft"},
        "location": {"price": {"sellingPrice": 4.0, "wasPrice": 5.0}},
        "promotions": [{"title": "Special Offer", "description": "5 for $10"}],
    }]}
    [p] = LowesScraper().parse_search(f"<script>window['__PRELOADED_STATE__'] = {json.dumps(state)}</script>")
    assert p.discount_pct == 20.0
    assert p.offers[0].kind == "multi_for_price" and p.offers[0].effective_discount_pct == 50.0
    assert p.best_discount_pct == 50.0 and p.flags == ["multi_buy", "reduced_price"]


def prod(pid, price, was=None, promo=()):
    return with_offers(Product(retailer=Retailer.lowes, product_id=pid, title=pid, current_price=price, was_price=was), promo)


def test_deals_rank_offers_and_markdowns_together():
    items = [
        prod("markdown30", 70, 100),
        prod("bogo", 20, promo=["Buy 1 Get 1 Free"]),
        prod("b2g1", 20, promo=["Buy 2 Get 1 Free"]),
        prod("plain", 20),
        prod("gift", 99, promo=["Get a free battery"]),
    ]
    deals = find_deals(items, min_discount=20)
    assert [d.product_id for d in deals] == ["bogo", "b2g1", "markdown30"]
    assert [d.product_id for d in find_deals(items, required_flags=["free_gift"])] == ["gift"]
    assert [d.product_id for d in find_deals(items, required_flags=["bogo"], min_discount=40)] == ["bogo"]
    assert deals[0].best_discount_pct == 50.0 and deals[0].offers[0].kind == "buy_x_get_y"


def test_history_keeps_offers_and_flags_new_bogo(store):
    now = utcnow()
    for d in (3, 2, 1):
        store.record([prod("L1", 10)], "t", observed_at=now - timedelta(days=d))
    store.record([prod("L1", 10, promo=["Buy 1 Get 1 Free"])], "t", observed_at=now)

    [latest] = store.latest()
    assert latest.offers[0].effective_discount_pct == 50.0 and "bogo" in latest.flags

    [drop] = find_drops(store)
    assert "new_bogo" in drop.signals and drop.drop_score >= 50


def test_feed_promotion_column():
    [(_, p)] = list(parse_feed("id,name,price,promotion\n1,Mulch,4,Buy 4 Get 1 Free\n", "csv", Retailer.lowes))
    assert p.offers[0].effective_discount_pct == 20.0 and p.flags == ["bogo"]


def test_current_deals_endpoint(client):
    client.get("/search", params={"q": "drill"})     # records the fixture products
    body = client.get("/deals/current", params={"min_discount": 40}).json()
    assert [d["product_id"] for d in body["deals"]] == ["5003", "333", "5001", "111"]
    assert client.get("/deals/current", params={"retailer": "lowes", "flag": "clearance"}).json()["count"] == 1
