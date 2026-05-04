"""Playwright-backed scraper that mirrors the httpx Scraper interface.

Used when site.use_browser = true. Handy when Cloudflare starts 403-ing the
plain httpx requests, since a real Chromium with a logged-in profile passes
most fingerprinting checks. Reuses the persistent profile dir from autobuy
config so cookies / session survive.

Slower (1-3s per page vs <100ms) and ~300-500MB resident, so don't enable
unless plain httpx is failing.
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from .config import SiteConfig
from .scraper import Product, _looks_like_product_url, parse_product

log = logging.getLogger(__name__)


class BrowserScraper:
    def __init__(self, site: SiteConfig, profile_dir: str):
        self.site = site
        self.profile_dir = profile_dir
        self._pw = None
        self._ctx = None
        self._page = None

    async def __aenter__(self) -> "BrowserScraper":
        from playwright.async_api import async_playwright

        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            headless=True,
            user_agent=self.site.user_agent,
            viewport={"width": 1280, "height": 900},
        )
        self._page = await self._ctx.new_page()
        return self

    async def __aexit__(self, *_) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def close(self) -> None:
        await self.__aexit__()

    async def fetch(self, url: str) -> str:
        await asyncio.sleep(self.site.request_delay_seconds)
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return await self._page.content()

    async def category_product_urls(self, category_url: str) -> list[str]:
        html = await self.fetch(category_url)
        tree = HTMLParser(html)
        urls: set[str] = set()
        for a in tree.css("a[href]"):
            href = a.attributes.get("href") or ""
            if not href:
                continue
            full = urljoin(category_url, href)
            if _looks_like_product_url(full, self.site.base_url):
                urls.add(full.split("#")[0])
        return sorted(urls)

    async def fetch_product(self, url: str) -> Product:
        html = await self.fetch(url)
        return parse_product(url, html)

    async def sitemap_urls(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        await self._walk_sitemap(self.site.sitemap_url, seen, out)
        return [u for u in out if _looks_like_product_url(u, self.site.base_url)]

    async def _walk_sitemap(self, url: str, seen: set[str], out: list[str]) -> None:
        if url in seen:
            return
        seen.add(url)
        try:
            text = await self.fetch(url)
        except Exception as e:
            log.warning("sitemap fetch failed for %s: %s", url, e)
            return
        # When Playwright loads an XML doc, the content is wrapped — strip the wrapper.
        # If parsing fails, give up on this leaf.
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            # Try grabbing the raw XML out of the rendered DOM
            cleaned = _strip_xml_wrapper(text)
            try:
                root = ET.fromstring(cleaned)
            except ET.ParseError as e:
                log.warning("sitemap parse failed for %s: %s", url, e)
                return
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for sm in root.findall("sm:sitemap/sm:loc", ns):
            if sm.text:
                await self._walk_sitemap(sm.text.strip(), seen, out)
        for loc in root.findall("sm:url/sm:loc", ns):
            if loc.text:
                out.append(loc.text.strip())


def _strip_xml_wrapper(html: str) -> str:
    """Playwright wraps raw XML in <html><body>...</body></html>. Recover the XML."""
    tree = HTMLParser(html)
    # Try to find the original XML inside the body
    body = tree.body
    if body is None:
        return html
    return body.text(separator="\n")
