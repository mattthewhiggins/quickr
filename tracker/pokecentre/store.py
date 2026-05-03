from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    url TEXT PRIMARY KEY,
    title TEXT,
    price TEXT,
    in_stock INTEGER,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    last_changed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sitemap_urls (
    url TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    listed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_products_last_seen ON products(last_seen);
"""


@dataclass
class ProductRow:
    url: str
    title: str | None
    price: str | None
    in_stock: bool | None
    first_seen: int
    last_seen: int
    last_changed: int


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_product(self, url: str) -> ProductRow | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM products WHERE url = ?", (url,)).fetchone()
            return ProductRow(**dict(row)) if row else None

    def upsert_product(
        self,
        url: str,
        title: str | None,
        price: str | None,
        in_stock: bool | None,
    ) -> tuple[ProductRow | None, ProductRow]:
        """Returns (previous_state_or_None, new_state). Stock changes update last_changed."""
        now = int(time.time())
        prev = self.get_product(url)
        with self._conn() as c:
            if prev is None:
                c.execute(
                    "INSERT INTO products (url, title, price, in_stock, first_seen, last_seen, last_changed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (url, title, price, _b(in_stock), now, now, now),
                )
            else:
                changed = (
                    prev.in_stock != _b(in_stock)
                    or (prev.price != price and price is not None)
                )
                c.execute(
                    "UPDATE products SET title = COALESCE(?, title), price = ?, in_stock = ?, "
                    "last_seen = ?, last_changed = ? WHERE url = ?",
                    (
                        title,
                        price,
                        _b(in_stock),
                        now,
                        now if changed else prev.last_changed,
                        url,
                    ),
                )
        new = self.get_product(url)
        assert new is not None
        return prev, new

    def record_sitemap_urls(self, urls: list[str]) -> list[str]:
        """Insert any unseen URLs. Returns list of newly added URLs."""
        if not urls:
            return []
        now = int(time.time())
        new_urls: list[str] = []
        with self._conn() as c:
            for url in urls:
                row = c.execute(
                    "SELECT 1 FROM sitemap_urls WHERE url = ?", (url,)
                ).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO sitemap_urls (url, first_seen, listed) VALUES (?, ?, 0)",
                        (url, now),
                    )
                    new_urls.append(url)
        return new_urls

    def sitemap_is_empty(self) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM sitemap_urls LIMIT 1").fetchone()
        return row is None

    def mark_listed(self, url: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE sitemap_urls SET listed = 1 WHERE url = ?", (url,))


def _b(v: bool | None) -> int | None:
    return None if v is None else int(bool(v))
