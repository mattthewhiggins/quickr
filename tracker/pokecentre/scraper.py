from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from .config import SiteConfig

log = logging.getLogger(__name__)


@dataclass
class Product:
    url: str
    title: str | None
    price: str | None
    in_stock: bool | None
    image_url: str | None = None
    description: str | None = None


class Scraper:
    def __init__(self, site: SiteConfig):
        self.site = site
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": site.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            },
            timeout=20.0,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Scraper":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def fetch(self, url: str) -> str:
        await asyncio.sleep(self.site.request_delay_seconds)
        r = await self._client.get(url)
        r.raise_for_status()
        return r.text

    async def category_product_urls(self, category_url: str) -> list[str]:
        """Extract product page URLs from a category listing page."""
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
        """Walk sitemap (and sitemap-index) and return product-shaped URLs."""
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
        except httpx.HTTPError as e:
            log.warning("sitemap fetch failed for %s: %s", url, e)
            return
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            log.warning("sitemap parse failed for %s: %s", url, e)
            return
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        # Sitemap index
        for sm in root.findall("sm:sitemap/sm:loc", ns):
            if sm.text:
                await self._walk_sitemap(sm.text.strip(), seen, out)
        # URL set
        for loc in root.findall("sm:url/sm:loc", ns):
            if loc.text:
                out.append(loc.text.strip())


def _looks_like_product_url(url: str, base_url: str) -> bool:
    if not url.startswith(base_url):
        return False
    # Pokemon Centre product pages typically include "/product/" in the path.
    return "/product/" in url


_PRICE_RE = re.compile(r"£\s?\d+(?:[.,]\d{2})?")


def parse_product(url: str, html: str) -> Product:
    """Best-effort parse: prefer JSON-LD, fall back to meta + visible text."""
    tree = HTMLParser(html)
    title: str | None = None
    price: str | None = None
    in_stock: bool | None = None
    image_url: str | None = None
    description: str | None = None

    # JSON-LD product blocks (most reliable when present)
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for entry in _iter_jsonld(data):
            if not isinstance(entry, dict):
                continue
            t = entry.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" not in types:
                continue
            title = title or entry.get("name")
            description = description or entry.get("description")
            img = entry.get("image")
            if img and image_url is None:
                image_url = img[0] if isinstance(img, list) else img
            offers = entry.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers] if offers else []
            for o in offer_list:
                if not isinstance(o, dict):
                    continue
                p = o.get("price") or o.get("lowPrice")
                if p and price is None:
                    price = f"£{p}" if not str(p).startswith("£") else str(p)
                avail = o.get("availability") or ""
                if isinstance(avail, str) and avail:
                    if "InStock" in avail or "PreOrder" in avail:
                        in_stock = True
                    elif "OutOfStock" in avail or "SoldOut" in avail or "Discontinued" in avail:
                        in_stock = False

    # Title fallback: og:title or <title>
    if not title:
        og = tree.css_first('meta[property="og:title"]')
        if og:
            title = og.attributes.get("content")
    if not title:
        t = tree.css_first("title")
        if t:
            title = (t.text() or "").strip() or None

    # Image fallback: og:image
    if not image_url:
        og_img = tree.css_first('meta[property="og:image"]')
        if og_img:
            image_url = og_img.attributes.get("content")

    # Description fallback: og:description / meta description
    if not description:
        for sel in ('meta[property="og:description"]', 'meta[name="description"]'):
            n = tree.css_first(sel)
            if n:
                description = n.attributes.get("content")
                if description:
                    break

    # Price fallback: scan body for £ pattern
    if not price:
        body = tree.body
        if body is not None:
            text = body.text(separator=" ")
            m = _PRICE_RE.search(text)
            if m:
                price = m.group(0).replace(" ", "")

    # Stock fallback: look for sold-out / add-to-cart cues
    if in_stock is None:
        body_text = (tree.body.text(separator=" ").lower() if tree.body else "")
        if any(s in body_text for s in ("sold out", "out of stock", "notify me when")):
            in_stock = False
        elif any(s in body_text for s in ("add to cart", "add to bag", "buy now")):
            in_stock = True

    return Product(
        url=url,
        title=title,
        price=price,
        in_stock=in_stock,
        image_url=image_url,
        description=(description.strip()[:500] if description else None),
    )


def _iter_jsonld(node):
    if isinstance(node, list):
        for x in node:
            yield from _iter_jsonld(x)
    elif isinstance(node, dict):
        yield node
        if "@graph" in node:
            yield from _iter_jsonld(node["@graph"])
