"""Playwright-driven autobuy.

Imported lazily — Playwright is only required when autobuy.enabled = true.

Workflow:
  1. Eligibility checks (config flag, price cap, allowlist, daily cap, cool-down).
  2. Launch persistent Chromium context (cookies + saved card survive).
  3. Navigate to product, click Add to Bag, navigate to checkout.
  4. If dry_run: screenshot the final review page and stop.
     Else: click Place Order and watch for confirmation or 3DS.

Run `python -m pokecentre.autobuy login` to open a browser so you can log in,
save your card / Apple Pay, save your address. Cookies persist for next run.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AutoBuyConfig, SiteConfig, load
from .notify import Alert, Notifier
from .scraper import Product
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class AutobuyResult:
    status: str  # "success" | "dry_run" | "pending_3ds" | "failed" | "skipped"
    detail: str


def _price_to_float(price: str | None) -> float | None:
    if not price:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", price.replace(",", ""))
    return float(m.group(1)) if m else None


def _title_in_allowlist(title: str | None, allowlist: list[str], fallback: list[str]) -> bool:
    keywords = allowlist or fallback
    if not keywords:
        return False
    if not title:
        return False
    t = title.lower()
    return any(k.lower() in t for k in keywords)


class AutoBuyer:
    def __init__(
        self,
        cfg: AutoBuyConfig,
        site: SiteConfig,
        store: Store,
        notifier: Notifier,
        fallback_keywords: list[str],
    ):
        self.cfg = cfg
        self.site = site
        self.store = store
        self.notifier = notifier
        self.fallback_keywords = fallback_keywords

    def eligibility(self, product: Product) -> AutobuyResult | None:
        """Return a 'skipped' AutobuyResult to refuse, or None to proceed."""
        if not self.cfg.enabled:
            return AutobuyResult("skipped", "autobuy disabled")
        # Per-item opt-in via the web UI. Default off — only buys items the user has toggled.
        pref = self.store.get_preference(product.url)
        if pref is None or not pref.autobuy_enabled:
            return AutobuyResult("skipped", "not toggled on in UI")
        if product.in_stock is not True:
            return AutobuyResult("skipped", "not in stock")
        if self.store.autobuy_succeeded_for(product.url):
            return AutobuyResult("skipped", "already purchased")
        if self.store.autobuy_recent_attempt(
            product.url, self.cfg.retry_cool_down_minutes * 60
        ):
            return AutobuyResult("skipped", "in cool-down")
        if self.store.autobuy_successes_today_utc() >= self.cfg.max_orders_per_day:
            return AutobuyResult("skipped", "daily order cap reached")
        if not _title_in_allowlist(
            product.title, self.cfg.allowlist_keywords, self.fallback_keywords
        ):
            return AutobuyResult("skipped", "title not in allowlist")
        price = _price_to_float(product.price)
        if price is None:
            return AutobuyResult("skipped", "price unparseable; refusing to buy blindly")
        if price > self.cfg.max_price_gbp:
            return AutobuyResult("skipped", f"£{price} above cap £{self.cfg.max_price_gbp}")
        return None

    async def maybe_buy(self, product: Product) -> AutobuyResult:
        skip = self.eligibility(product)
        if skip is not None:
            log.info("autobuy skipped %s: %s", product.url, skip.detail)
            self.store.record_autobuy(product.url, "skipped", skip.detail)
            return skip

        log.info("autobuy attempting %s", product.url)
        try:
            result = await self._run(product)
        except Exception as e:
            log.exception("autobuy crashed for %s: %s", product.url, e)
            result = AutobuyResult("failed", f"exception: {e}")

        self.store.record_autobuy(product.url, result.status, result.detail)
        self.notifier.send(Alert(
            kind=f"autobuy:{result.status}",
            title=product.title or product.url,
            body=result.detail,
            url=product.url,
        ))
        return result

    async def _run(self, product: Product) -> AutobuyResult:
        try:
            from playwright.async_api import async_playwright, TimeoutError as PWTimeout
        except ImportError:
            return AutobuyResult(
                "failed",
                "Playwright not installed. Run: pip install playwright && playwright install chromium",
            )

        profile_dir = Path(self.cfg.profile_dir).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=self.cfg.headless,
                user_agent=self.site.user_agent,
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()
            try:
                await page.goto(product.url, wait_until="domcontentloaded", timeout=30000)

                # Add to bag
                add_btn = page.get_by_role(
                    "button", name=re.compile(self.cfg.add_to_bag_pattern, re.I)
                ).first
                try:
                    await add_btn.wait_for(state="visible", timeout=15000)
                except PWTimeout:
                    return AutobuyResult("failed", "Add to Bag button never appeared (out of stock?)")
                await add_btn.click()

                # Brief settle, then go straight to cart/checkout.
                await page.wait_for_timeout(1500)
                cart_url = self.site.base_url.rstrip("/") + "/cart"
                await page.goto(cart_url, wait_until="domcontentloaded", timeout=30000)

                checkout_btn = page.get_by_role(
                    "button", name=re.compile(self.cfg.checkout_pattern, re.I)
                ).first
                try:
                    await checkout_btn.wait_for(state="visible", timeout=15000)
                    await checkout_btn.click()
                except PWTimeout:
                    # Some stores expose checkout as a link instead.
                    link = page.get_by_role(
                        "link", name=re.compile(self.cfg.checkout_pattern, re.I)
                    ).first
                    await link.click(timeout=15000)

                place_btn = page.get_by_role(
                    "button", name=re.compile(self.cfg.place_order_pattern, re.I)
                ).first
                try:
                    await place_btn.wait_for(state="visible", timeout=45000)
                except PWTimeout:
                    return AutobuyResult("failed", "Place Order button never appeared")

                if self.cfg.dry_run:
                    shot = profile_dir.parent / f"dry-run-{int(time.time())}.png"
                    await page.screenshot(path=str(shot), full_page=True)
                    return AutobuyResult(
                        "dry_run",
                        f"Stopped at Place Order (dry_run=true). Screenshot: {shot}",
                    )

                await place_btn.click()

                # Wait for either a confirmation URL or 3DS challenge frame.
                deadline = self.cfg.post_pay_timeout_seconds
                try:
                    await page.wait_for_url(
                        re.compile(r"order|confirm|thank|success", re.I),
                        timeout=deadline * 1000,
                    )
                    return AutobuyResult("success", f"order page reached: {page.url}")
                except PWTimeout:
                    return AutobuyResult(
                        "pending_3ds",
                        "No confirmation within timeout — likely 3DS / step-up auth. "
                        "Check your banking app to approve.",
                    )
            finally:
                await ctx.close()


# --- Standalone login helper -------------------------------------------------

async def _login_helper(profile_dir: str, base_url: str, user_agent: str) -> None:
    """Open a browser at the site so the user can log in and save card details.
    The persistent profile keeps cookies for subsequent autobuy runs.
    """
    from playwright.async_api import async_playwright

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            user_agent=user_agent,
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await page.goto(base_url)
        print(
            "\nBrowser open. Log in, save your card / Apple Pay, save your address.\n"
            "Press ENTER here when done to close the browser and persist cookies.\n"
        )
        await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        await ctx.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pokecentre.autobuy")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login", help="Open a browser to log in and persist cookies")
    p_login.add_argument("-c", "--config", default="config.toml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    cfg = load(args.config)
    if args.cmd == "login":
        asyncio.run(_login_helper(cfg.autobuy.profile_dir, cfg.site.base_url, cfg.site.user_agent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
