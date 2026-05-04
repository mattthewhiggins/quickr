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
    image_url TEXT,
    description TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    last_changed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sitemap_urls (
    url TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    listed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS autobuy_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    attempted_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS product_preferences (
    url TEXT PRIMARY KEY,
    alert_enabled INTEGER NOT NULL DEFAULT 1,
    autobuy_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_last_seen ON products(last_seen);
CREATE INDEX IF NOT EXISTS idx_products_in_stock ON products(in_stock);
CREATE INDEX IF NOT EXISTS idx_autobuy_url_time ON autobuy_attempts(url, attempted_at);
CREATE INDEX IF NOT EXISTS idx_autobuy_status_time ON autobuy_attempts(status, attempted_at);
"""


# For backwards-compat with DBs created before image/description columns existed.
_PRODUCT_MIGRATIONS = [
    "ALTER TABLE products ADD COLUMN image_url TEXT",
    "ALTER TABLE products ADD COLUMN description TEXT",
]


@dataclass
class ProductRow:
    url: str
    title: str | None
    price: str | None
    in_stock: bool | None
    first_seen: int
    last_seen: int
    last_changed: int
    image_url: str | None = None
    description: str | None = None


@dataclass
class PreferenceRow:
    url: str
    alert_enabled: bool
    autobuy_enabled: bool
    updated_at: int


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            for stmt in _PRODUCT_MIGRATIONS:
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists

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
        image_url: str | None = None,
        description: str | None = None,
    ) -> tuple[ProductRow | None, ProductRow]:
        """Returns (previous_state_or_None, new_state). Stock changes update last_changed."""
        now = int(time.time())
        prev = self.get_product(url)
        with self._conn() as c:
            if prev is None:
                c.execute(
                    "INSERT INTO products (url, title, price, in_stock, image_url, description, "
                    "first_seen, last_seen, last_changed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (url, title, price, _b(in_stock), image_url, description, now, now, now),
                )
            else:
                changed = (
                    prev.in_stock != _b(in_stock)
                    or (prev.price != price and price is not None)
                )
                c.execute(
                    "UPDATE products SET title = COALESCE(?, title), price = ?, in_stock = ?, "
                    "image_url = COALESCE(?, image_url), description = COALESCE(?, description), "
                    "last_seen = ?, last_changed = ? WHERE url = ?",
                    (
                        title,
                        price,
                        _b(in_stock),
                        image_url,
                        description,
                        now,
                        now if changed else prev.last_changed,
                        url,
                    ),
                )
        new = self.get_product(url)
        assert new is not None
        return prev, new

    def all_products(self) -> list[ProductRow]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM products ORDER BY in_stock DESC, last_changed DESC"
            ).fetchall()
        return [ProductRow(**dict(r)) for r in rows]

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

    def record_autobuy(self, url: str, status: str, detail: str | None = None) -> None:
        """status: 'success' | 'dry_run' | 'pending_3ds' | 'failed' | 'skipped'."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO autobuy_attempts (url, status, detail, attempted_at) VALUES (?, ?, ?, ?)",
                (url, status, detail, int(time.time())),
            )

    def autobuy_succeeded_for(self, url: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM autobuy_attempts WHERE url = ? AND status = 'success' LIMIT 1",
                (url,),
            ).fetchone()
        return row is not None

    def autobuy_recent_attempt(self, url: str, within_seconds: int) -> bool:
        cutoff = int(time.time()) - within_seconds
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM autobuy_attempts WHERE url = ? AND attempted_at >= ? LIMIT 1",
                (url, cutoff),
            ).fetchone()
        return row is not None

    def get_preference(self, url: str) -> PreferenceRow | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM product_preferences WHERE url = ?", (url,)).fetchone()
        if not row:
            return None
        return PreferenceRow(
            url=row["url"],
            alert_enabled=bool(row["alert_enabled"]),
            autobuy_enabled=bool(row["autobuy_enabled"]),
            updated_at=int(row["updated_at"]),
        )

    def all_preferences(self) -> dict[str, PreferenceRow]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM product_preferences").fetchall()
        out: dict[str, PreferenceRow] = {}
        for r in rows:
            out[r["url"]] = PreferenceRow(
                url=r["url"],
                alert_enabled=bool(r["alert_enabled"]),
                autobuy_enabled=bool(r["autobuy_enabled"]),
                updated_at=int(r["updated_at"]),
            )
        return out

    def set_preference(
        self,
        url: str,
        alert_enabled: bool | None = None,
        autobuy_enabled: bool | None = None,
    ) -> PreferenceRow:
        existing = self.get_preference(url)
        ae = alert_enabled if alert_enabled is not None else (existing.alert_enabled if existing else True)
        ab = autobuy_enabled if autobuy_enabled is not None else (existing.autobuy_enabled if existing else False)
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO product_preferences (url, alert_enabled, autobuy_enabled, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET alert_enabled=excluded.alert_enabled, "
                "autobuy_enabled=excluded.autobuy_enabled, updated_at=excluded.updated_at",
                (url, int(ae), int(ab), now),
            )
        return PreferenceRow(url=url, alert_enabled=ae, autobuy_enabled=ab, updated_at=now)

    def autobuy_successes_today_utc(self) -> int:
        # Day boundary in UTC.
        import datetime as _dt
        midnight = int(_dt.datetime.now(_dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp())
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM autobuy_attempts WHERE status = 'success' AND attempted_at >= ?",
                (midnight,),
            ).fetchone()
        return int(row[0])


def _b(v: bool | None) -> int | None:
    return None if v is None else int(bool(v))
