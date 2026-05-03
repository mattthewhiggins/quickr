from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from .config import EmailConfig, NotifyConfig, TelegramConfig

log = logging.getLogger(__name__)


@dataclass
class Alert:
    kind: str  # "restock" | "new_listing" | "new_sku"
    title: str
    body: str
    url: str


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    def send(self, alert: Alert) -> None:
        for ch in self.cfg.channels:
            try:
                if ch == "console":
                    _send_console(alert)
                elif ch == "telegram":
                    _send_telegram(self.cfg.telegram, alert)
                elif ch == "email":
                    _send_email(self.cfg.email, alert)
                else:
                    log.warning("unknown notify channel: %s", ch)
            except Exception as e:
                log.exception("notify channel %s failed: %s", ch, e)


def _send_console(a: Alert) -> None:
    print(f"\n[{a.kind}] {a.title}\n{a.body}\n{a.url}\n", flush=True)


def _send_telegram(cfg: TelegramConfig, a: Alert) -> None:
    if not cfg.bot_token or not cfg.chat_id:
        log.warning("telegram not configured; skipping")
        return
    text = f"*[{_md(a.kind)}]* {_md(a.title)}\n{_md(a.body)}\n{a.url}"
    r = httpx.post(
        f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage",
        data={
            "chat_id": cfg.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "false",
        },
        timeout=15.0,
    )
    r.raise_for_status()


def _md(s: str) -> str:
    # Telegram Markdown escape for the legacy parser.
    return s.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


def _send_email(cfg: EmailConfig, a: Alert) -> None:
    if not cfg.smtp_host or not cfg.to_addr or not cfg.from_addr:
        log.warning("email not configured; skipping")
        return
    msg = EmailMessage()
    msg["Subject"] = f"[{a.kind}] {a.title}"
    msg["From"] = cfg.from_addr
    msg["To"] = cfg.to_addr
    msg.set_content(f"{a.body}\n\n{a.url}")
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as s:
        s.starttls()
        if cfg.smtp_user:
            s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)
