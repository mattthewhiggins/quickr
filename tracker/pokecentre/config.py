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
class Config:
    site: SiteConfig
    tracking: TrackingConfig
    storage_db_path: str
    notify: NotifyConfig
    raw: dict = field(default_factory=dict)


def load(path: str | Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    site = SiteConfig(**data["site"])
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

    return Config(
        site=site,
        tracking=tracking,
        storage_db_path=storage_db_path,
        notify=notify,
        raw=data,
    )
