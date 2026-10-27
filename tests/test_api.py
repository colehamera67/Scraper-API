import httpx

from .conftest import fixture, make_client


def test_search_all(client):
    r = client.get("/search", params={"q": "drill"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 6 and body["errors"] == {}
    assert {p["retailer"] for p in body["products"]} == {"walmart", "lowes"}


def test_deals(client):
    r = client.get("/deals", params={"q": "drill", "min_discount": 40})
    body = r.json()
    ids = [d["product_id"] for d in body["deals"]]
    # 5003: 50% off but $300 saved + clearance outranks 333: 60% off, $60 saved
    assert ids == ["5003", "333", "5001", "111"]
    assert body["deals"][1]["discount_pct"] == 60.0
    assert [d["product_id"] for d in client.get("/deals", params={"q": "drill", "flag": "clearance"}).json()["deals"]] == ["5003", "333"]


def test_product_by_id_and_url(client):
    assert client.get("/product/walmart", params={"id": "111"}).json()["was_price"] == 179.0
    r = client.get("/product/lowes", params={"url": "https://www.lowes.com/pd/CRAFTSMAN-V20-Drill/5001"})
    assert r.json()["current_price"] == 59.0


def test_product_rejects_foreign_url(client):
    assert client.get("/product/lowes", params={"url": "https://evil.example/pd/1"}).status_code == 422
    assert client.get("/product/lowes", params={"url": "https://notlowes.com/pd/1"}).status_code == 422
    assert client.get("/product/lowes").status_code == 422


def test_parse_endpoint(client):
    r = client.post("/parse/walmart?min_discount=30", content=fixture("walmart_search.html"),
                    headers={"Content-Type": "text/html"})
    assert r.status_code == 200
    assert [d["product_id"] for d in r.json()["deals"]] == ["333", "111"]
    assert client.post("/parse/lowes", content="<html></html>", headers={"Content-Type": "text/html"}).status_code == 422


def test_blocked_is_reported_per_retailer():
    def handler(request):
        if "/blocked" in request.url.path:
            return httpx.Response(200, text="Robot or human?")
        if "walmart" in request.url.host:
            return httpx.Response(307, headers={"Location": "https://www.walmart.com/blocked?url=x"})
        return httpx.Response(200, text=fixture("lowes_search.html"))

    with make_client(handler) as c:
        body = c.get("/search", params={"q": "drill"}).json()
        assert body["count"] == 3
        assert "BlockedError" in body["errors"]["walmart"]
        assert c.get("/product/walmart", params={"id": "1"}).status_code == 503
