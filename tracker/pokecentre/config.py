from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class SiteConfig:
    base_url: str
    sitemap_url: str
    user_agent: str
    request_delay_seconds: float
    use_browser: bool = False


@dataclass
class TrackingConfig:
    category_urls: list[str]
    title_keywords: list[str]
    exclude_keywords: list[str]


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_addr: str
    to_addr: str


@dataclass
class NotifyConfig:
    channels: list[str]
    telegram: TelegramConfig
    email: EmailConfig


@dataclass
class AutoBuyConfig:
    enabled: bool
    dry_run: bool
    profile_dir: str
    headless: bool
    max_price_gbp: float
    max_orders_per_day: int
    retry_cool_down_minutes: int
    allowlist_keywords: list[str]
    add_to_bag_pattern: str
    checkout_pattern: str
    place_order_pattern: str
    post_pay_timeout_seconds: int


@dataclass
class Config:
    site: SiteConfig
    tracking: TrackingConfig
    storage_db_path: str
    notify: NotifyConfig
    autobuy: AutoBuyConfig
    raw: dict = field(default_factory=dict)


def load(path: str | Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    site_data = dict(data["site"])
    site_data.setdefault("use_browser", False)
    site = SiteConfig(**site_data)
    tracking = TrackingConfig(**data["tracking"])
    storage_db_path = data["storage"]["db_path"]

    notify_block = data.get("notify", {})
    tg = notify_block.get("telegram", {})
    em = notify_block.get("email", {})
    notify = NotifyConfig(
        channels=notify_block.get("channels", ["console"]),
        telegram=TelegramConfig(
            bot_token=tg.get("bot_token", ""),
            chat_id=str(tg.get("chat_id", "")),
        ),
        email=EmailConfig(
            smtp_host=em.get("smtp_host", ""),
            smtp_port=int(em.get("smtp_port", 587)),
            smtp_user=em.get("smtp_user", ""),
            smtp_password=em.get("smtp_password", ""),
            from_addr=em.get("from_addr", ""),
            to_addr=em.get("to_addr", ""),
        ),
    )

    ab = data.get("autobuy", {})
    autobuy = AutoBuyConfig(
        enabled=bool(ab.get("enabled", False)),
        dry_run=bool(ab.get("dry_run", True)),
        profile_dir=ab.get("profile_dir", ".chromium-profile"),
        headless=bool(ab.get("headless", False)),
        max_price_gbp=float(ab.get("max_price_gbp", 200.0)),
        max_orders_per_day=int(ab.get("max_orders_per_day", 2)),
        retry_cool_down_minutes=int(ab.get("retry_cool_down_minutes", 60)),
        allowlist_keywords=list(ab.get("allowlist_keywords", [])),
        add_to_bag_pattern=ab.get("add_to_bag_pattern", "add to bag|add to cart"),
        checkout_pattern=ab.get("checkout_pattern", "checkout|proceed to checkout"),
        place_order_pattern=ab.get("place_order_pattern", "place order|pay now|complete order"),
        post_pay_timeout_seconds=int(ab.get("post_pay_timeout_seconds", 90)),
    )

    return Config(
        site=site,
        tracking=tracking,
        storage_db_path=storage_db_path,
        notify=notify,
        autobuy=autobuy,
        raw=data,
    )
