from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from .models import Offer, Product, Retailer

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    retailer    TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT,
    brand       TEXT,
    image_url   TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (retailer, product_id)
);
CREATE TABLE IF NOT EXISTS observations (
    retailer     TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    price        REAL NOT NULL,
    was_price    REAL,
    in_stock     INTEGER,
    flags        TEXT NOT NULL DEFAULT '[]',
    source       TEXT NOT NULL,
    offers       TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS obs_product_time ON observations (retailer, product_id, observed_at);
CREATE INDEX IF NOT EXISTS products_last_seen ON products (last_seen);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_ts(dt: datetime) -> str:
    """Timestamps are stored as fixed-format UTC strings so they sort correctly as text."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Observation(BaseModel):
    observed_at: datetime
    price: float
    was_price: float | None = None
    in_stock: bool | None = None
    flags: list[str] = []
    offers: list[Offer] = []
    source: str


class TrackedProduct(BaseModel):
    retailer: Retailer
    product_id: str
    title: str
    url: str | None = None
    brand: str | None = None
    image_url: str | None = None
    first_seen: datetime
    last_seen: datetime


class PriceStore:
    """SQLite price history. Writes go through one locked connection; reads open their own (WAL mode lets
    them run alongside writes), so a long drop scan doesn't block ingestion."""

    def __init__(self, path: str | Path, min_record_interval: float = 1800):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(observations)")}
        if "offers" not in columns:  # databases created before offers were stored
            self._db.execute("ALTER TABLE observations ADD COLUMN offers TEXT NOT NULL DEFAULT '[]'")
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.min_record_interval = min_record_interval

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def record(self, products: Iterable[Product], source: str, observed_at: datetime | None = None) -> int:
        """Store a price observation for each product. Returns the number of observations written.

        Products without a current price only update the catalog entry. An observation identical to the
        product's previous one is skipped when it comes less than `min_record_interval` seconds later.
        """
        ts = to_ts(observed_at or utcnow())
        written = 0
        with self._lock, self._db:
            for p in products:
                key = (p.retailer.value, p.product_id)
                self._db.execute(
                    """INSERT INTO products (retailer, product_id, title, url, brand, image_url, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (retailer, product_id) DO UPDATE SET
                         title = excluded.title,
                         url = COALESCE(excluded.url, url),
                         brand = COALESCE(excluded.brand, brand),
                         image_url = COALESCE(excluded.image_url, image_url),
                         first_seen = MIN(first_seen, excluded.first_seen),
                         last_seen = MAX(last_seen, excluded.last_seen)""",
                    (*key, p.title, p.url, p.brand, p.image_url, ts, ts),
                )
                if p.current_price is None:
                    continue
                in_stock = None if p.in_stock is None else int(p.in_stock)
                flags = json.dumps(sorted(p.flags))
                offers = json.dumps([o.model_dump(exclude_none=True) for o in p.offers])
                last = self._db.execute(
                    """SELECT observed_at, price, was_price, in_stock, flags, offers FROM observations
                       WHERE retailer = ? AND product_id = ? AND observed_at <= ?
                       ORDER BY observed_at DESC LIMIT 1""",
                    (*key, ts),
                ).fetchone()
                if (
                    last
                    and (last["price"], last["was_price"], last["in_stock"], last["flags"], last["offers"])
                    == (p.current_price, p.was_price, in_stock, flags, offers)
                    and (from_ts(ts) - from_ts(last["observed_at"])).total_seconds() < self.min_record_interval
                ):
                    continue
                self._db.execute(
                    """INSERT INTO observations
                         (retailer, product_id, observed_at, price, was_price, in_stock, flags, offers, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*key, ts, p.current_price, p.was_price, in_stock, flags, offers, source),
                )
                written += 1
        return written

    def product(self, retailer: Retailer, product_id: str) -> TrackedProduct | None:
        with self._reader() as db:
            row = db.execute(
                "SELECT * FROM products WHERE retailer = ? AND product_id = ?", (retailer.value, product_id)
            ).fetchone()
        return _tracked(row) if row else None

    def history(self, retailer: Retailer, product_id: str, since: datetime | None = None) -> list[Observation]:
        with self._reader() as db:
            rows = db.execute(
                """SELECT * FROM observations WHERE retailer = ? AND product_id = ? AND observed_at >= ?
                   ORDER BY observed_at""",
                (retailer.value, product_id, to_ts(since) if since else ""),
            ).fetchall()
        return [_observation(r) for r in rows]

    def iter_series(
        self, *, retailer: Retailer | None = None, seen_within: timedelta | None = None, lookback: timedelta | None = None
    ) -> Iterator[tuple[TrackedProduct, list[Observation]]]:
        """Yield (product, observations oldest-first) for every product, streaming one product at a time.

        `seen_within` limits it to products observed recently; `lookback` drops observations older than that.
        """
        now = utcnow()
        params: list = [to_ts(now - lookback) if lookback else ""]
        where = ""
        if retailer:
            where += " AND p.retailer = ?"
            params.append(retailer.value)
        if seen_within:
            where += " AND p.last_seen >= ?"
            params.append(to_ts(now - seen_within))
        query = f"""
            SELECT p.retailer, p.product_id, p.title, p.url, p.brand, p.image_url, p.first_seen, p.last_seen,
                   o.observed_at, o.price, o.was_price, o.in_stock, o.flags, o.offers, o.source
            FROM products p JOIN observations o
              ON o.retailer = p.retailer AND o.product_id = p.product_id AND o.observed_at >= ?
            WHERE 1 = 1 {where}
            ORDER BY p.retailer, p.product_id, o.observed_at"""
        with self._reader() as db:
            yield from self._group(db.execute(query, params))

    @staticmethod
    def _group(rows: Iterable[sqlite3.Row]) -> Iterator[tuple[TrackedProduct, list[Observation]]]:
        current_key, current, series = None, None, []
        for row in rows:
            key = (row["retailer"], row["product_id"])
            if key != current_key:
                if current:
                    yield current, series
                current_key, current, series = key, _tracked(row), []
            series.append(_observation(row))
        if current:
            yield current, series

    def latest(self, *, retailer: Retailer | None = None, seen_within: timedelta = timedelta(hours=48)) -> list[Product]:
        """Each product's most recent observation (within `seen_within`), as a Product with its offers."""
        params: list = [to_ts(utcnow() - seen_within)]
        where = ""
        if retailer:
            where = " AND o.retailer = ?"
            params.append(retailer.value)
        query = f"""
            SELECT p.retailer, p.product_id, p.title, p.url, p.brand, p.image_url,
                   o.price, o.was_price, o.in_stock, o.flags, o.offers
            FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY retailer, product_id ORDER BY observed_at DESC) AS rn
                  FROM observations WHERE observed_at >= ?) o
            JOIN products p ON p.retailer = o.retailer AND p.product_id = o.product_id
            WHERE o.rn = 1 {where}"""
        with self._reader() as db:
            rows = db.execute(query, params).fetchall()
        return [
            Product(
                retailer=Retailer(r["retailer"]), product_id=r["product_id"], title=r["title"], url=r["url"],
                brand=r["brand"], image_url=r["image_url"], current_price=r["price"], was_price=r["was_price"],
                in_stock=None if r["in_stock"] is None else bool(r["in_stock"]),
                flags=json.loads(r["flags"]), offers=json.loads(r["offers"]),
            )
            for r in rows
        ]

    def stats(self) -> dict:
        with self._reader() as db:
            products = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            obs = db.execute("SELECT COUNT(*), MAX(observed_at) FROM observations").fetchone()
        return {"products": products, "observations": obs[0], "last_observation": obs[1]}


def _tracked(row: sqlite3.Row) -> TrackedProduct:
    return TrackedProduct(
        retailer=Retailer(row["retailer"]),
        product_id=row["product_id"],
        title=row["title"],
        url=row["url"],
        brand=row["brand"],
        image_url=row["image_url"],
        first_seen=from_ts(row["first_seen"]),
        last_seen=from_ts(row["last_seen"]),
    )


def _observation(row: sqlite3.Row) -> Observation:
    return Observation(
        observed_at=from_ts(row["observed_at"]),
        price=row["price"],
        was_price=row["was_price"],
        in_stock=None if row["in_stock"] is None else bool(row["in_stock"]),
        flags=json.loads(row["flags"]),
        offers=json.loads(row["offers"]),
        source=row["source"],
    )
