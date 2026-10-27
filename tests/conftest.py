from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.fetcher import Fetcher
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def route(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "walmart.com/search" in url:
        return httpx.Response(200, text=fixture("walmart_search.html"))
    if "walmart.com/ip/" in url:
        return httpx.Response(200, text=fixture("walmart_product.html"))
    if "lowes.com/search" in url:
        return httpx.Response(200, text=fixture("lowes_search.html"))
    if "lowes.com/pd/" in url:
        return httpx.Response(200, text=fixture("lowes_product.html"))
    return httpx.Response(404)


def make_client(handler=route) -> TestClient:
    app.state.fetcher = Fetcher(Settings(min_interval=0, cache_ttl=0), transport=httpx.MockTransport(handler))
    return TestClient(app)


@pytest.fixture
def client():
    with make_client() as c:
        yield c
