from datetime import timedelta

import pytest

from app.drops import analyze, find_drops
from app.history import utcnow
from app.models import Product, Retailer
from app.sources import FeedError, Target, load_targets, parse_feed


def prod(price, pid="1", retailer=Retailer.walmart, flags=(), in_stock=True, was=None):
    return Product(retailer=retailer, product_id=pid, title=f"Item {pid}", current_price=price,
                   was_price=was, flags=list(flags), in_stock=in_stock)


def record_daily(store, prices, pid="1", **kw):
    """Record one observation per day, ending now."""
    now = utcnow()
    for i, price in enumerate(prices):
        store.record([prod(price, pid, **kw)], "test", observed_at=now - timedelta(days=len(prices) - 1 - i))


def test_record_skips_identical_observation_within_interval(store):
    now = utcnow()
    assert store.record([prod(10)], "a", observed_at=now - timedelta(minutes=10)) == 1
    assert store.record([prod(10)], "a", observed_at=now) == 0          # same price, 10 min later
    assert store.record([prod(9)], "a", observed_at=now) == 1           # price changed
    assert store.record([prod(9)], "a", observed_at=now + timedelta(hours=1)) == 1  # interval elapsed
    assert store.record([prod(None)], "a") == 0                         # no price: catalog only
    assert len(store.history(Retailer.walmart, "1")) == 3
    assert store.stats()["products"] == 1


def test_product_keeps_first_seen_and_updates_title(store):
    now = utcnow()
    store.record([prod(10)], "a", observed_at=now - timedelta(days=5))
    p = prod(11)
    p.title = "Renamed"
    store.record([p], "a", observed_at=now)
    tracked = store.product(Retailer.walmart, "1")
    assert tracked.title == "Renamed"
    assert (now - tracked.first_seen).days == 5


def test_below_median_and_new_low(store):
    record_daily(store, [100, 102, 98, 100, 99, 60])
    [drop] = find_drops(store)
    assert drop.median_price == 100 and drop.lowest_price == 98
    assert drop.drop_vs_median_pct == 40.0
    assert set(drop.signals) == {"below_median", "price_cut", "new_low"}


def test_inflated_was_price_is_not_a_drop(store):
    # Always "on sale" against a fake was-price: history shows no real drop.
    record_daily(store, [50, 50, 50, 50], was=100)
    assert find_drops(store) == []


def test_needs_history_for_median_signal(store):
    record_daily(store, [100, 70])
    [drop] = find_drops(store)
    assert drop.signals == ["price_cut"]        # median signal needs 3 days by default
    assert find_drops(store, required_signals=["below_median"]) == []


def test_new_clearance_flag(store):
    record_daily(store, [40, 40, 40])
    store.record([prod(39, flags=["clearance"])], "test")
    [drop] = find_drops(store)
    assert "new_clearance" in drop.signals


def test_daily_close_median_ignores_sampling_frequency(store):
    now = utcnow()
    # Many samples at $10 on one day shouldn't drag the median down when other days were $100.
    for h in range(10):
        store.record([prod(10 + h * 0.01)], "t", observed_at=now - timedelta(days=4, hours=h))
    for d in (3, 2, 1):
        store.record([prod(100)], "t", observed_at=now - timedelta(days=d))
    store.record([prod(80)], "t", observed_at=now)
    [drop] = find_drops(store)
    assert drop.median_price == 100 and drop.history_days == 4


def test_filters_and_staleness(store):
    record_daily(store, [100, 100, 100, 50], pid="cheap")
    record_daily(store, [1000, 1000, 1000, 500], pid="pricey", in_stock=False)
    record_daily(store, [100, 100, 100, 50], pid="lowes", retailer=Retailer.lowes)
    assert [d.product_id for d in find_drops(store)][0] == "pricey"   # bigger $ drop scores higher
    assert {d.product_id for d in find_drops(store, max_price=100)} == {"cheap", "lowes"}
    assert {d.product_id for d in find_drops(store, in_stock_only=True)} == {"cheap", "lowes"}
    assert {d.product_id for d in find_drops(store, retailer=Retailer.lowes)} == {"lowes"}

    old = utcnow() - timedelta(days=10)
    for i, p in enumerate([100, 100, 100, 40]):
        store.record([prod(p, "stale")], "t", observed_at=old + timedelta(days=i))
    assert "stale" not in {d.product_id for d in find_drops(store)}


def test_analyze_single_observation(store):
    store.record([prod(10)], "t")
    tracked = store.product(Retailer.walmart, "1")
    a = analyze(tracked, store.history(Retailer.walmart, "1"))
    assert a.signals == [] and a.median_price is None


def test_parse_csv_feed():
    text = (
        "SKU,Name,Sale Price,MSRP,Availability,Retailer,Link,Badges\n"
        "A1,Drill,$59.00,$129.00,In Stock,Lowe's,https://www.lowes.com/pd/x/A1,Clearance\n"
        "A2,Saw,20,15,out of stock,Walmart.com,,\n"
        ",Missing id,1,,,,,\n"
    )
    rows = list(parse_feed(text, "csv"))
    (_, a), (_, b), (line, err) = rows
    assert (a.retailer, a.product_id, a.current_price, a.was_price) == (Retailer.lowes, "A1", 59.0, 129.0)
    assert a.in_stock is True and a.flags == ["clearance"] and a.url.endswith("/A1")
    assert b.retailer == Retailer.walmart and b.was_price is None and b.in_stock is False
    assert line == 4 and isinstance(err, FeedError)


def test_parse_jsonl_feed_with_default_retailer():
    text = '{"itemId": 5, "name": "Mower", "salePrice": 199.0, "msrp": 249.0}\n\nnot json\n'
    (_, p), (line, err) = list(parse_feed(text, "jsonl", Retailer.walmart))
    assert (p.product_id, p.current_price, p.was_price) == ("5", 199.0, 249.0)
    assert line == 3 and isinstance(err, FeedError)
    [(_, err)] = list(parse_feed('{"id": 1, "name": "x", "price": 1}', "jsonl"))
    assert "retailer" in str(err)


def test_targets():
    [t1, t2] = load_targets('{"targets": [{"retailer": "lowes", "query": "clearance", "pages": 2},'
                            '{"retailer": "walmart", "url": "https://www.walmart.com/browse/clearance"}]}')
    assert t1.urls() == ["https://www.lowes.com/search?searchTerm=clearance",
                         "https://www.lowes.com/search?searchTerm=clearance&offset=24"]
    assert t2.urls() == ["https://www.walmart.com/browse/clearance"]
    with pytest.raises(ValueError):
        Target(retailer="walmart", url="https://www.lowes.com/x")
    with pytest.raises(ValueError):
        Target(retailer="walmart")
