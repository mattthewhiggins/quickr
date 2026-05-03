from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import Config, load
from .notify import Alert, Notifier
from .scraper import Product, Scraper
from .store import Store

log = logging.getLogger("pokecentre")


def _matches_keywords(text: str | None, cfg) -> bool:
    if not text:
        return False
    t = text.lower()
    if cfg.tracking.exclude_keywords and any(k.lower() in t for k in cfg.tracking.exclude_keywords):
        return False
    if not cfg.tracking.title_keywords:
        return True
    return any(k.lower() in t for k in cfg.tracking.title_keywords)


async def run_once(cfg: Config) -> None:
    store = Store(cfg.storage_db_path)
    notifier = Notifier(cfg.notify)
    autobuyer = None
    if cfg.autobuy.enabled:
        from .autobuy import AutoBuyer
        autobuyer = AutoBuyer(
            cfg=cfg.autobuy,
            site=cfg.site,
            store=store,
            notifier=notifier,
            fallback_keywords=cfg.tracking.title_keywords,
        )

    async with Scraper(cfg.site) as sc:
        # 1. Sitemap sweep — find new SKUs before they hit category pages.
        # First run is silent: we only want alerts on URLs that appear *after* bootstrap.
        try:
            bootstrap = store.sitemap_is_empty()
            sitemap = await sc.sitemap_urls()
            new_sitemap = store.record_sitemap_urls(sitemap)
            log.info(
                "sitemap: %d urls, %d new%s",
                len(sitemap), len(new_sitemap),
                " (bootstrap, suppressing alerts)" if bootstrap else "",
            )
            if not bootstrap:
                for url in new_sitemap:
                    # Apply keyword filter against URL slug to suppress non-card noise.
                    slug = url.rsplit("/", 1)[-1].replace("-", " ")
                    if not _matches_keywords(slug, cfg):
                        continue
                    notifier.send(Alert(
                        kind="new_sku",
                        title="New SKU discovered in sitemap",
                        body="URL appeared in sitemap before being publicly listed.",
                        url=url,
                    ))
        except Exception as e:
            log.exception("sitemap sweep failed: %s", e)

        # 2. Category sweep — collect candidate product URLs.
        candidates: set[str] = set()
        for cat in cfg.tracking.category_urls:
            try:
                urls = await sc.category_product_urls(cat)
                log.info("category %s -> %d products", cat, len(urls))
                candidates.update(urls)
            except Exception as e:
                log.exception("category fetch failed for %s: %s", cat, e)

        # 3. Inspect each candidate product.
        for url in sorted(candidates):
            try:
                product = await sc.fetch_product(url)
            except Exception as e:
                log.warning("fetch product failed %s: %s", url, e)
                continue

            if not _matches_keywords(product.title, cfg):
                continue

            prev, new = store.upsert_product(
                url=product.url,
                title=product.title,
                price=product.price,
                in_stock=product.in_stock,
            )
            store.mark_listed(product.url)

            became_available = (prev is None and product.in_stock is True) or (
                prev is not None and _became_in_stock(prev.in_stock, product.in_stock)
            )

            if prev is None:
                notifier.send(Alert(
                    kind="new_listing",
                    title=product.title or url,
                    body=_describe(product),
                    url=url,
                ))
            elif _became_in_stock(prev.in_stock, product.in_stock):
                notifier.send(Alert(
                    kind="restock",
                    title=product.title or url,
                    body=_describe(product),
                    url=url,
                ))

            if became_available and autobuyer is not None:
                await autobuyer.maybe_buy(product)


def _describe(p: Product) -> str:
    parts = []
    if p.price:
        parts.append(p.price)
    if p.in_stock is True:
        parts.append("IN STOCK")
    elif p.in_stock is False:
        parts.append("out of stock")
    return " · ".join(parts) if parts else "(no details)"


def _became_in_stock(prev: int | None, current: bool | None) -> bool:
    return current is True and prev != 1


async def run_loop(cfg: Config, interval_seconds: int) -> None:
    while True:
        try:
            await run_once(cfg)
        except Exception as e:
            log.exception("run_once error: %s", e)
        log.info("sleeping %ds", interval_seconds)
        await asyncio.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pokecentre", description="Pokemon Centre UK stock tracker")
    p.add_argument("-c", "--config", default="config.toml", help="Path to config TOML")
    p.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                   help="Run in a loop with this interval. 0 = single run.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"config not found: {cfg_path}. Copy config.example.toml to {cfg_path}.", file=sys.stderr)
        return 2
    cfg = load(cfg_path)

    if args.loop > 0:
        asyncio.run(run_loop(cfg, args.loop))
    else:
        asyncio.run(run_once(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
